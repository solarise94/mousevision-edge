"""Video file frame source for Mac PoC."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import cv2

from mousevision.types import Frame


class VideoFormatError(RuntimeError):
    """The video file exists but cannot be decoded into a usable stream.

    Raised by the video source when OpenCV cannot open the file at all, or by
    the job worker when the file opens but decodes zero frames (e.g. multiple
    fragmented-MP4 shards concatenated by MediaRecorder timeslice recording).
    Surfaced to the user as "录像可能损坏，请重录" rather than a generic
    analysis failure or a misleading "no mouse detected".
    """


class VideoFileSource:
    def __init__(
        self,
        path: str | Path,
        *,
        frame_stride: int = 1,
        max_frames: int | None = None,
        start_ms: float | None = None,
        end_ms: float | None = None,
        crop: dict[str, float] | None = None,
        target_size: tuple[int, int] | None = None,
    ) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        self.frame_stride = max(1, frame_stride)
        self.max_frames = max_frames
        self.start_ms = start_ms
        self.end_ms = end_ms
        self._cap: cv2.VideoCapture | None = None
        # Normalized crop region of the *full* recorded frame the client
        # actually saw on screen (keys x,y,w,h, each in [0,1]). Mobile records
        # a landscape stream but previews a portrait center crop via CSS
        # object-fit:cover; ``crop`` reproduces that visible region so OCR /
        # mouse detection only analyse what the operator framed - the rest of
        # the landscape clip (off-screen left/right) is excluded. ``None`` keeps
        # the legacy full-frame behaviour for CLI / tests. Resolved to absolute
        # pixel bounds on the first decoded frame.
        self.crop = crop
        self._crop_px: tuple[int, int, int, int] | None = None
        # Optional (width, height) to resize each frame to *after* cropping.
        # The LCD/mouse detectors use fixed pixel thresholds (lcd_detect.min_area,
        # mouse_detect.min_area, ...) tuned for the reference 720x1280 frame; a
        # center crop shrinks the frame and would drop LCD area below those
        # thresholds. Resizing the cropped frame back to the reference geometry
        # keeps the thresholds meaningful. Only applied when a crop is set;
        # ``None`` leaves the (already-cropped) frame at native resolution.
        self.target_size = target_size

    def probe(self) -> dict[str, float]:
        """Read container-level metadata without decoding frames.

        Returns fps, declared frame count, width, height and a nominal
        duration. Note that for a concatenated fragmented-MP4 the declared
        frame count/duration may reflect only the first shard (or be 0) — the
        authoritative readability signal is the decoded frame count from
        ``frames()``, not these container headers.
        """
        cap = cv2.VideoCapture(str(self.path))
        try:
            if not cap.isOpened():
                return {
                    "frame_count": 0.0,
                    "width": 0.0,
                    "height": 0.0,
                    "fps": 0.0,
                    "duration_sec": 0.0,
                }
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
            frame_count = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
            width = float(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0.0)
            height = float(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0.0)
            duration_sec = (frame_count / fps) if fps > 1e-3 else 0.0
            return {
                "frame_count": frame_count,
                "width": width,
                "height": height,
                "fps": fps,
                "duration_sec": duration_sec,
            }
        finally:
            cap.release()

    def frames(self) -> Iterator[Frame]:
        self._cap = cv2.VideoCapture(str(self.path))
        if not self._cap.isOpened():
            # Completely unopenable / unsupported / zero-byte file. This is the
            # same class of problem as a zero-decode clip, so it shares the
            # user-facing "录像可能损坏" message rather than being reported as a
            # generic analysis failure.
            raise VideoFormatError(f"无法打开视频文件：{self.path}")

        fps = float(self._cap.get(cv2.CAP_PROP_FPS) or 30.0)
        if fps <= 1e-3:
            fps = 30.0

        index = 0
        if self.start_ms is not None and self.start_ms > 0:
            index = max(0, int(round(self.start_ms / 1000.0 * fps)))
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, float(index))

        emitted = 0
        while True:
            ok, image = self._cap.read()
            if not ok:
                break
            # Resolve the absolute pixel crop box once, from the first decoded
            # frame's real dimensions. The recorded stream's width/height are
            # not knowable from container props alone for fragmented MP4, so we
            # defer to image.shape. Only the visible region the operator framed
            # is fed downstream; the full landscape clip is retained on disk.
            if self.crop is not None and self._crop_px is None:
                fh, fw = image.shape[:2]
                c = self.crop
                cx = max(0, min(int(round(float(c.get("x", 0.0)) * fw)), fw))
                cy = max(0, min(int(round(float(c.get("y", 0.0)) * fh)), fh))
                cw = max(0, min(int(round(float(c.get("w", 1.0)) * fw)), fw - cx))
                ch = max(0, min(int(round(float(c.get("h", 1.0)) * fh)), fh - cy))
                if cw <= 0 or ch <= 0:
                    # Degenerate crop: fall back to full frame rather than yield
                    # an empty image (which would break downstream shape checks).
                    self.crop = None
                else:
                    self._crop_px = (cx, cy, cw, ch)
            if self._crop_px is not None:
                cx, cy, cw, ch = self._crop_px
                image = image[cy:cy + ch, cx:cx + cw]
                # Restore the reference geometry so the fixed-pixel detector
                # thresholds (tuned for 720x1280) remain valid after the crop
                # shrank the frame. Uses INTER_LINEAR for speed; analysis does
                # not need sub-pixel fidelity here.
                if self.target_size is not None:
                    tw, th = self.target_size
                    if image.shape[1] != tw or image.shape[0] != th:
                        image = cv2.resize(image, (tw, th), interpolation=cv2.INTER_LINEAR)
            timestamp_ms = (index / fps) * 1000.0
            if self.end_ms is not None and timestamp_ms > self.end_ms:
                break
            if index % self.frame_stride == 0:
                yield Frame(image=image, timestamp_ms=timestamp_ms, index=index)
                emitted += 1
                if self.max_frames is not None and emitted >= self.max_frames:
                    break
            index += 1

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def __enter__(self) -> "VideoFileSource":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
