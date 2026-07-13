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
    ) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        self.frame_stride = max(1, frame_stride)
        self.max_frames = max_frames
        self.start_ms = start_ms
        self.end_ms = end_ms
        self._cap: cv2.VideoCapture | None = None

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
