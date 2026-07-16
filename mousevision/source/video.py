"""Video file frame source.

Production path uses a fixed ``ffmpeg`` binary (the one in the Linux
container image) to decode MP4 -> BGR frames so Mac / Linux do not diverge
on OpenCV/FFmpeg colour conversion or seek rounding. OpenCV
``VideoCapture`` remains an explicit fallback for unit tests and hosts
without ffmpeg.

Timestamps are derived from ffmpeg's ``showinfo`` filter ``pts_time``
(presentation timestamp) on the ffmpeg path, so variable-frame-rate (VFR)
videos produce correct wall-clock timestamps. When PTS is unavailable
(corrupt stream, stripped metadata), the source falls back to ``index/fps``
and marks each ``Frame.timestamp_source = "fallback_fps"`` so downstream
code can distinguish accurate from estimated timestamps.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import warnings
from collections import deque
from pathlib import Path
from typing import Iterator, Literal

import cv2
import numpy as np

from mousevision.types import Frame

BackendName = Literal["ffmpeg", "opencv", "auto"]

# Default sampling: ~7.5 fps, migrated from frame_stride=2 @ 15 fps target.
_DEFAULT_SAMPLE_INTERVAL_MS = 1000.0 / 7.5
# Bounded buffer for showinfo PTS entries; oversized videos should not OOM.
# At 30fps, 20000 entries ≈ 11 min, far beyond any mouse-weighing clip.
_PTS_BUFFER_LIMIT = 20000


class VideoFormatError(RuntimeError):
    """The video file exists but cannot be decoded into a usable stream.

    Raised by the video source when the chosen backend cannot open the file,
    or by the job worker when the file opens but decodes zero frames (e.g.
    multiple fragmented-MP4 shards concatenated by MediaRecorder timeslice
    recording). Surfaced to the user as "录像可能损坏，请重录" rather than a
    generic analysis failure or a misleading "no mouse detected".
    """


def _env_backend() -> BackendName:
    raw = (os.environ.get("MOUSEVISION_VIDEO_BACKEND") or "auto").strip().lower()
    if raw in {"ffmpeg", "opencv", "auto"}:
        return raw  # type: ignore[return-value]
    return "auto"


def resolve_video_backend(requested: BackendName | None = None) -> Literal["ffmpeg", "opencv"]:
    """Pick decode backend: env / explicit arg, ffmpeg when available."""
    choice: BackendName = requested or _env_backend()
    if choice == "opencv":
        return "opencv"
    if choice == "ffmpeg":
        if shutil.which(_ffmpeg_bin()) is None:
            raise RuntimeError(
                f"MOUSEVISION_VIDEO_BACKEND=ffmpeg but {_ffmpeg_bin()!r} not found on PATH"
            )
        return "ffmpeg"
    # auto
    return "ffmpeg" if shutil.which(_ffmpeg_bin()) else "opencv"


def _ffmpeg_bin() -> str:
    return os.environ.get("MOUSEVISION_FFMPEG") or "ffmpeg"


def _ffprobe_bin() -> str:
    return os.environ.get("MOUSEVISION_FFPROBE") or "ffprobe"


def _ffprobe_video(path: Path) -> dict[str, float]:
    """Container-level metadata via ffprobe (same binary family as ffmpeg decode)."""
    cmd = [
        _ffprobe_bin(),
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        "-select_streams",
        "v:0",
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {
            "frame_count": 0.0,
            "width": 0.0,
            "height": 0.0,
            "fps": 0.0,
            "duration_sec": 0.0,
        }
    if proc.returncode != 0 or not proc.stdout.strip():
        return {
            "frame_count": 0.0,
            "width": 0.0,
            "height": 0.0,
            "fps": 0.0,
            "duration_sec": 0.0,
        }
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "frame_count": 0.0,
            "width": 0.0,
            "height": 0.0,
            "fps": 0.0,
            "duration_sec": 0.0,
        }
    streams = data.get("streams") or []
    stream = streams[0] if streams else {}
    fmt = data.get("format") or {}

    def _fps(s: dict) -> float:
        for key in ("avg_frame_rate", "r_frame_rate"):
            raw = str(s.get(key) or "")
            if not raw or raw == "0/0":
                continue
            if "/" in raw:
                num, den = raw.split("/", 1)
                try:
                    n, d = float(num), float(den)
                    if d > 1e-9:
                        return n / d
                except ValueError:
                    continue
            else:
                try:
                    v = float(raw)
                    if v > 1e-9:
                        return v
                except ValueError:
                    continue
        return 0.0

    fps = _fps(stream)
    width = float(stream.get("width") or 0.0)
    height = float(stream.get("height") or 0.0)
    # nb_frames is often unset for fragmented MP4; fall back to duration * fps.
    frame_count = 0.0
    nb = stream.get("nb_frames")
    if nb not in (None, "N/A", ""):
        try:
            frame_count = float(nb)
        except (TypeError, ValueError):
            frame_count = 0.0
    duration_sec = 0.0
    for src in (stream.get("duration"), fmt.get("duration")):
        if src in (None, "N/A", ""):
            continue
        try:
            duration_sec = float(src)
            break
        except (TypeError, ValueError):
            continue
    if frame_count <= 0 and duration_sec > 0 and fps > 1e-3:
        frame_count = duration_sec * fps
    if duration_sec <= 0 and frame_count > 0 and fps > 1e-3:
        duration_sec = frame_count / fps
    return {
        "frame_count": frame_count,
        "width": width,
        "height": height,
        "fps": fps,
        "duration_sec": duration_sec,
    }


# --------------------------------------------------------------------------- #
# showinfo PTS reader
# --------------------------------------------------------------------------- #
# showinfo log lines look like:
#   [Parsed_showinfo_0 @ 0x...] n:   0 pts:      0 pts_time:0.000000 ...
# We extract the frame index (n) and presentation timestamp (pts_time).
_SHOWINFO_RE = re.compile(r"n:\s*(\d+)\s+pts:\s*\d+\s+pts_time:\s*([\d.]+)")


class _ShowinfoPtsReader(threading.Thread):
    """Drain ffmpeg stderr in a background thread, parsing showinfo lines.

    showinfo writes one info-level log line per decoded frame to stderr.
    If stderr is not continuously drained, the OS pipe buffer fills and
    ffmpeg blocks (deadlock). This thread reads stderr line by line,
    extracts (frame_index, pts_time_seconds) pairs into a bounded dict
    keyed by frame index, and signals when the stream ends.

    The main thread reads stdout rawvideo frames and looks up PTS by the
    same frame index ``n``.
    """

    def __init__(self, stderr: object, fps_fallback: float) -> None:
        super().__init__(daemon=True, name="showinfo-pts-reader")
        self._stderr = stderr  # type: ignore[assignment]
        self._fps_fallback = fps_fallback
        # frame_index -> pts_time_ms (before first-frame normalization)
        self._pts: dict[int, float] = {}
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._error: str | None = None

    @property
    def pts_map(self) -> dict[int, float]:
        with self._lock:
            return dict(self._pts)

    @property
    def error(self) -> str | None:
        return self._error

    def is_done(self) -> bool:
        return self._done.is_set()

    def get_pts_ms(self, frame_index: int, timeout: float = 2.0) -> float | None:
        """Return pts_time in ms for *frame_index*, or None if not available.

        Waits up to *timeout* seconds for the entry to appear (the stderr
        thread may lag slightly behind stdout).
        """
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if frame_index in self._pts:
                    return self._pts[frame_index]
            if self._done.is_set():
                # Stream ended; final check.
                with self._lock:
                    return self._pts.get(frame_index)
            time.sleep(0.001)
        with self._lock:
            return self._pts.get(frame_index)

    def run(self) -> None:
        assert self._stderr is not None
        try:
            for line_bytes in self._stderr:  # type: ignore[attr-defined]
                line = line_bytes.decode("utf-8", errors="replace") if isinstance(line_bytes, bytes) else line_bytes
                m = _SHOWINFO_RE.search(line)
                if m:
                    n = int(m.group(1))
                    pts_time = float(m.group(2))
                    with self._lock:
                        if len(self._pts) < _PTS_BUFFER_LIMIT:
                            self._pts[n] = pts_time * 1000.0
        except Exception as exc:
            self._error = str(exc)
        finally:
            self._done.set()


class VideoFileSource:
    def __init__(
        self,
        path: str | Path,
        *,
        frame_stride: int = 1,
        sample_interval_ms: float | None = None,
        analysis_fps: float | None = None,
        max_frames: int | None = None,
        start_ms: float | None = None,
        end_ms: float | None = None,
        crop: dict[str, float] | None = None,
        target_size: tuple[int, int] | None = None,
        backend: BackendName | None = None,
    ) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        # --- Sampling configuration ---
        # New code should use sample_interval_ms or analysis_fps for
        # time-based sampling (correct for VFR). Legacy frame_stride uses
        # index-based sampling (backward compatible, inaccurate for VFR but
        # preserves existing test expectations until configs are migrated).
        if sample_interval_ms is not None:
            self.sample_interval_ms = float(sample_interval_ms)
            self._use_time_sampling = True
        elif analysis_fps is not None and analysis_fps > 1e-3:
            self.sample_interval_ms = 1000.0 / float(analysis_fps)
            self._use_time_sampling = True
        elif frame_stride > 1:
            # Legacy stride: keep index-based sampling for backward compat.
            self.sample_interval_ms = (frame_stride * 1000.0) / 15.0
            self._use_time_sampling = False
            warnings.warn(
                f"frame_stride={frame_stride} is deprecated; "
                f"consider sample_interval_ms={self.sample_interval_ms:.1f} "
                "or analysis_fps for VFR-correct time-based sampling.",
                DeprecationWarning,
                stacklevel=2,
            )
        else:
            self.sample_interval_ms = 0.0  # emit every decoded frame
            self._use_time_sampling = True
        self.frame_stride = max(1, frame_stride)  # kept for external readers
        self.max_frames = max_frames
        self.start_ms = start_ms
        self.end_ms = end_ms
        self._cap: cv2.VideoCapture | None = None
        self._ffmpeg_proc: subprocess.Popen[bytes] | None = None
        self._pts_reader: _ShowinfoPtsReader | None = None
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
        # Optional (width, height) to resize each frame after optional crop.
        # Detector thresholds are tuned for the reference 720x1280 frame.
        # Applied whenever set - after crop for CSS-crop uploads, or alone for
        # canvas captures that need a light normalize to reference size.
        self.target_size = target_size
        self.backend = resolve_video_backend(backend)

    def probe(self) -> dict[str, float]:
        """Read container-level metadata without decoding frames.

        Returns fps, declared frame count, width, height and a nominal
        duration. Note that for a concatenated fragmented-MP4 the declared
        frame count/duration may reflect only the first shard (or be 0) — the
        authoritative readability signal is the decoded frame count from
        ``frames()``, not these container headers.
        """
        if self.backend == "ffmpeg":
            meta = _ffprobe_video(self.path)
            if meta["width"] > 0 and meta["height"] > 0:
                return meta
            # ffprobe missing / failed: fall through to OpenCV props.
        return self._probe_opencv()

    def _probe_opencv(self) -> dict[str, float]:
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

    def _apply_crop_resize(self, image: np.ndarray) -> np.ndarray:
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
            image = image[cy : cy + ch, cx : cx + cw]
        # Resize after optional crop. Used for CSS-crop mobile uploads
        # (restore 720x1280 after shrinking) and for canvas captures whose
        # encoder may emit a near-but-not-exact reference size.
        if self.target_size is not None:
            tw, th = self.target_size
            if image.shape[1] != tw or image.shape[0] != th:
                image = cv2.resize(image, (tw, th), interpolation=cv2.INTER_LINEAR)
        return image

    def frames(self) -> Iterator[Frame]:
        if self.backend == "ffmpeg":
            yield from self._frames_ffmpeg()
        else:
            yield from self._frames_opencv()

    def _should_emit(self, timestamp_ms: float, next_sample_ms: float) -> bool:
        """Time-based sampling: emit when timestamp >= next sample boundary."""
        return timestamp_ms >= next_sample_ms

    def _frames_opencv(self) -> Iterator[Frame]:
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
        next_sample_ms = float(self.start_ms or 0.0)
        interval = self.sample_interval_ms
        use_time = self._use_time_sampling
        while True:
            ok, image = self._cap.read()
            if not ok:
                break
            image = self._apply_crop_resize(image)
            timestamp_ms = (index / fps) * 1000.0
            if self.end_ms is not None and timestamp_ms > self.end_ms:
                break
            # Time-based sampling (VFR-correct) or legacy index-based sampling.
            should_emit = (
                interval <= 0
                or (use_time and self._should_emit(timestamp_ms, next_sample_ms))
                or (not use_time and index % self.frame_stride == 0)
            )
            if should_emit:
                yield Frame(
                    image=image,
                    timestamp_ms=timestamp_ms,
                    index=index,
                    timestamp_source="fallback_fps",
                )
                emitted += 1
                if use_time:
                    next_sample_ms = timestamp_ms + interval
                if self.max_frames is not None and emitted >= self.max_frames:
                    break
            index += 1

    def _frames_ffmpeg(self) -> Iterator[Frame]:
        """Decode via ffmpeg raw BGR24 pipe with showinfo PTS extraction.

        Timestamps come from the ``showinfo`` filter's ``pts_time``, extracted
        from stderr by a background thread and paired with rawvideo frames by
        decode index ``n``. This produces correct wall-clock timestamps even
        for variable-frame-rate (VFR) videos. When PTS is unavailable, falls
        back to ``index/fps`` and marks ``timestamp_source="fallback_fps"``.
        """
        meta = _ffprobe_video(self.path)
        width = int(meta["width"] or 0)
        height = int(meta["height"] or 0)
        fps = float(meta["fps"] or 0.0)
        if width <= 0 or height <= 0:
            # Last resort: OpenCV header props (still decode with ffmpeg).
            oc = self._probe_opencv()
            width = int(oc["width"] or 0)
            height = int(oc["height"] or 0)
            if fps <= 1e-3:
                fps = float(oc["fps"] or 0.0)
        if width <= 0 or height <= 0:
            raise VideoFormatError(f"无法打开视频文件：{self.path}")
        if fps <= 1e-3:
            fps = 30.0

        # Build ffmpeg command with showinfo filter for PTS extraction.
        # -loglevel info is required: showinfo writes at info level.
        # We filter stderr to showinfo lines only in the reader thread.
        cmd = [
            _ffmpeg_bin(),
            "-hide_banner",
            "-loglevel",
            "info",
            "-i",
            str(self.path),
            "-vf",
            "showinfo",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-vsync",
            "0",
            "pipe:1",
        ]
        try:
            self._ffmpeg_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise VideoFormatError(f"无法启动 ffmpeg 解码：{self.path}") from exc

        assert self._ffmpeg_proc.stdout is not None
        assert self._ffmpeg_proc.stderr is not None

        # Start background thread to drain stderr and parse showinfo PTS.
        self._pts_reader = _ShowinfoPtsReader(
            self._ffmpeg_proc.stderr, fps_fallback=fps
        )
        self._pts_reader.start()

        frame_nbytes = width * height * 3
        index = 0
        emitted = 0
        start_ms = float(self.start_ms or 0.0)
        next_sample_ms = start_ms
        interval = self.sample_interval_ms
        first_pts_ms: float | None = None  # for first-frame normalization
        last_emitted_ms = -1.0  # enforce strictly increasing timestamps

        try:
            while True:
                raw = self._ffmpeg_proc.stdout.read(frame_nbytes)
                if raw is None or len(raw) < frame_nbytes:
                    break
                # Look up PTS for this frame (may briefly lag).
                raw_pts_ms = self._pts_reader.get_pts_ms(index, timeout=2.0)
                if raw_pts_ms is not None:
                    # First-frame normalization: subtract the first frame's PTS
                    # so timestamps start at 0 (or at start_ms offset).
                    if first_pts_ms is None:
                        first_pts_ms = raw_pts_ms
                    pts_ms = raw_pts_ms - first_pts_ms
                    ts_source = "pts"
                else:
                    # PTS unavailable: fall back to index/fps.
                    pts_ms = (index / fps) * 1000.0
                    ts_source = "fallback_fps"

                # Apply start_ms offset for both PTS and fallback paths.
                if start_ms > 0 and pts_ms < start_ms:
                    index += 1
                    continue

                # Enforce strictly increasing timestamps (non-monotonic guard).
                if pts_ms <= last_emitted_ms:
                    pts_ms = last_emitted_ms + 0.001
                    ts_source = "fallback_fps"

                if self.end_ms is not None and pts_ms > self.end_ms:
                    break

                # Time-based sampling (VFR-correct) or legacy index-based.
                use_time = self._use_time_sampling
                should_emit = (
                    interval <= 0
                    or (use_time and self._should_emit(pts_ms, next_sample_ms))
                    or (not use_time and index % self.frame_stride == 0)
                )
                if should_emit:
                    image = (
                        np.frombuffer(raw, dtype=np.uint8)
                        .reshape((height, width, 3))
                        .copy()
                    )
                    image = self._apply_crop_resize(image)
                    yield Frame(
                        image=image,
                        timestamp_ms=pts_ms,
                        index=index,
                        timestamp_source=ts_source,
                    )
                    last_emitted_ms = pts_ms
                    emitted += 1
                    if self._use_time_sampling:
                        next_sample_ms = pts_ms + interval
                    if self.max_frames is not None and emitted >= self.max_frames:
                        break
                index += 1
        finally:
            self.close()

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        if self._ffmpeg_proc is not None:
            proc = self._ffmpeg_proc
            self._ffmpeg_proc = None
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
                if proc.stderr is not None:
                    proc.stderr.close()
            except Exception:
                pass
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        # Join the PTS reader thread so we don't leak threads on early close.
        if self._pts_reader is not None and self._pts_reader.is_alive():
            # Give it a moment to finish draining stderr, then give up.
            self._pts_reader.join(timeout=3)
        self._pts_reader = None

    def __enter__(self) -> "VideoFileSource":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def decoded_duration_sec(self) -> float:
        """Best-effort real decoded duration using first/last showinfo PTS.

        Decodes the full video (without emitting frames to a consumer) to
        collect showinfo PTS, then returns ``(last_pts - first_pts)`` in
        seconds. Falls back to ``index/fps`` when PTS is unavailable.

        Used by truncation checks (jobs.py) to avoid the inaccurate
        ``decoded_frames * stride / fps`` estimate on VFR videos.
        """
        meta = _ffprobe_video(self.path)
        width = int(meta["width"] or 0)
        height = int(meta["height"] or 0)
        fps = float(meta["fps"] or 0.0)
        if width <= 0 or height <= 0:
            oc = self._probe_opencv()
            width = int(oc["width"] or 0)
            height = int(oc["height"] or 0)
            if fps <= 1e-3:
                fps = float(oc["fps"] or 0.0)
        if width <= 0 or height <= 0:
            return 0.0
        if fps <= 1e-3:
            fps = 30.0

        cmd = [
            _ffmpeg_bin(),
            "-hide_banner",
            "-loglevel",
            "info",
            "-i",
            str(self.path),
            "-vf",
            "showinfo",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-vsync",
            "0",
            "pipe:1",
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError:
            return 0.0

        assert proc.stdout is not None
        assert proc.stderr is not None

        reader = _ShowinfoPtsReader(proc.stderr, fps_fallback=fps)
        reader.start()

        frame_nbytes = width * height * 3
        index = 0
        first_pts_ms: float | None = None
        last_pts_ms: float | None = None

        try:
            while True:
                raw = proc.stdout.read(frame_nbytes)
                if raw is None or len(raw) < frame_nbytes:
                    break
                pts_ms = reader.get_pts_ms(index, timeout=2.0)
                if pts_ms is not None:
                    if first_pts_ms is None:
                        first_pts_ms = pts_ms
                    last_pts_ms = pts_ms
                index += 1
        finally:
            try:
                if proc.stdout:
                    proc.stdout.close()
                if proc.stderr:
                    proc.stderr.close()
            except Exception:
                pass
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            reader.join(timeout=3)

        if first_pts_ms is not None and last_pts_ms is not None:
            return (last_pts_ms - first_pts_ms) / 1000.0
        # Fallback: use decoded frame count / fps.
        return (index / fps) if index > 0 else 0.0
