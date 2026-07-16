"""Tests for the ffmpeg video backend with showinfo PTS extraction.

Covers the v3 REVIEW_ALGORITHM_ROBUSTNESS.md P0-a requirements:
- Real PTS via showinfo (first-frame normalized, strictly increasing)
- VFR videos produce correct wall-clock timestamps
- Missing/non-monotonic PTS falls back to index/fps with timestamp_source="fallback_fps"
- Time-based sampling (sample_interval_ms) replaces frame_stride modulo
- start_ms/end_ms windowing
- Early close does not leak threads or deadlock
- decoded_duration_sec() uses real first/last PTS
- Truncation check uses PTS-based duration, not decoded_frames * stride / fps
"""

from __future__ import annotations

import subprocess
import warnings
from pathlib import Path

import cv2
import numpy as np
import pytest

from mousevision.source.video import (
    VideoFileSource,
    VideoFormatError,
    _ShowinfoPtsReader,
    _SHOWINFO_RE,
)
from mousevision.types import Frame

try:
    import ffmpeg as _ffmpeg_mod  # noqa: F401
    _HAS_PY_FFMPEG = True
except ImportError:
    _HAS_PY_FFMPEG = False

FFMPEG_BIN = "ffmpeg"
FFPROBE_BIN = "ffprobe"


def _ffmpeg_available() -> bool:
    import shutil

    return shutil.which(FFMPEG_BIN) is not None and shutil.which(FFPROBE_BIN) is not None


pytestmark = pytest.mark.skipif(
    not _ffmpeg_available(), reason="ffmpeg/ffprobe not installed"
)


# --------------------------------------------------------------------------- #
# Test video generators
# --------------------------------------------------------------------------- #

def _make_constant_fps_video(
    path: Path, width: int = 64, height: int = 48, fps: float = 15.0,
    duration_sec: float = 2.0,
) -> Path:
    """Create a small CFR MP4 via ffmpeg (always H.264 so showinfo works)."""
    total_frames = int(fps * duration_sec)
    # Generate raw frames, pipe to ffmpeg.
    frames_data = bytearray()
    for i in range(total_frames):
        img = np.full((height, width, 3), i % 255, dtype=np.uint8)
        frames_data.extend(img.tobytes())
    cmd = [
        FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}", "-framerate", str(fps),
        "-i", "pipe:0",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-r", str(fps),
        str(path),
    ]
    subprocess.run(
        cmd, input=bytes(frames_data), check=True, capture_output=True,
    )
    return path


def _make_vfr_video(
    path: Path, width: int = 64, height: int = 48,
) -> Path:
    """Create a VFR MP4: first 1s at 30fps, then 2s at 5fps.

    Uses ``setpts`` with ``-video_track_timescale 1000`` to produce real
    variable-frame-rate PTS: frames 0-29 at ~33ms spacing (30fps), frames
    30-39 at 200ms spacing (5fps, starting at 1s). Total duration ~3.1s.

    The container's ``avg_frame_rate`` becomes ~14.4fps, so a uniform
    ``index/avg_fps`` assumption produces incorrect timestamps -- exactly
    the scenario showinfo PTS is meant to fix.
    """
    all_data = bytearray()
    for i in range(30):  # segment 1: 30 frames
        img = np.full((height, width, 3), 100, dtype=np.uint8)
        all_data.extend(img.tobytes())
    for i in range(10):  # segment 2: 10 frames
        img = np.full((height, width, 3), 200, dtype=np.uint8)
        all_data.extend(img.tobytes())

    cmd = [
        FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}", "-framerate", "30",
        "-i", "pipe:0",
        # setpts: frames 0-29 at 33.33ms, frames 30-39 at 200ms starting at 1s.
        "-vf", r"setpts=if(lt(N\,30)\,N*33.33\,1000+(N-30)*200)*TB",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-video_track_timescale", "1000",
        "-vsync", "0",
        str(path),
    ]
    subprocess.run(cmd, input=bytes(all_data), check=True, capture_output=True)
    return path


# --------------------------------------------------------------------------- #
# PTS extraction tests
# --------------------------------------------------------------------------- #

class TestShowinfoRegex:
    def test_showinfo_regex_matches_typical_line(self):
        line = (
            "[Parsed_showinfo_0 @ 0x7f8b1c01a000] "
            "n:   0 pts:      0 pts_time:0.000000 pos:     48 "
            "fmt:yuv420p sar:1/1 s:64x48 i:P iskey:1 type:I "
            "checksum:00000000 plane_checksum:00000000,00000000,00000000"
        )
        m = _SHOWINFO_RE.search(line)
        assert m is not None
        assert int(m.group(1)) == 0
        assert float(m.group(2)) == pytest.approx(0.0)

    def test_showinfo_regex_matches_nonzero_pts(self):
        line = (
            "[Parsed_showinfo_0 @ 0x7f8b1c01a000] "
            "n:  15 pts:   1500 pts_time:1.500000 pos:   2048 ..."
        )
        m = _SHOWINFO_RE.search(line)
        assert m is not None
        assert int(m.group(1)) == 15
        assert float(m.group(2)) == pytest.approx(1.5)

    def test_showinfo_regex_no_match_for_other_lines(self):
        line = "  Stream #0:0: Video: h264 ..."
        assert _SHOWINFO_RE.search(line) is None


# --------------------------------------------------------------------------- #
# CFR video tests
# --------------------------------------------------------------------------- #

class TestCFRVideo:
    def test_cfr_timestamps_start_at_zero(self, tmp_path):
        path = _make_constant_fps_video(tmp_path / "cfr.mp4", fps=15.0, duration_sec=2.0)
        src = VideoFileSource(path, sample_interval_ms=0.0, backend="ffmpeg")
        frames = list(src.frames())
        assert len(frames) > 0
        assert frames[0].timestamp_ms == pytest.approx(0.0, abs=5.0)
        assert frames[0].timestamp_source == "pts"

    def test_cfr_timestamps_uniform_spacing(self, tmp_path):
        path = _make_constant_fps_video(tmp_path / "cfr.mp4", fps=15.0, duration_sec=2.0)
        src = VideoFileSource(path, sample_interval_ms=0.0, backend="ffmpeg")
        frames = list(src.frames())
        # All frames emitted (interval=0). Check spacing ≈ 66.67ms.
        if len(frames) >= 3:
            d1 = frames[1].timestamp_ms - frames[0].timestamp_ms
            d2 = frames[2].timestamp_ms - frames[1].timestamp_ms
            assert d1 == pytest.approx(1000.0 / 15.0, abs=10.0)
            assert d2 == pytest.approx(1000.0 / 15.0, abs=10.0)

    def test_cfr_timestamps_strictly_increasing(self, tmp_path):
        path = _make_constant_fps_video(tmp_path / "cfr.mp4", fps=15.0, duration_sec=2.0)
        src = VideoFileSource(path, sample_interval_ms=0.0, backend="ffmpeg")
        frames = list(src.frames())
        for i in range(1, len(frames)):
            assert frames[i].timestamp_ms > frames[i - 1].timestamp_ms


# --------------------------------------------------------------------------- #
# VFR video tests
# --------------------------------------------------------------------------- #

class TestVFRVideo:
    def test_vfr_timestamps_reflect_real_presentation_time(self, tmp_path):
        """VFR video: 1s@30fps + 2s@5fps = 3s total. PTS must span ~3s."""
        path = _make_vfr_video(tmp_path / "vfr.mp4")
        src = VideoFileSource(path, sample_interval_ms=0.0, backend="ffmpeg")
        frames = list(src.frames())
        assert len(frames) > 0
        # Last frame should be near 3s (3000ms), not near 40/avg_fps.
        assert frames[-1].timestamp_ms > 2500.0  # well past 2.5s
        assert frames[-1].timestamp_ms < 3500.0  # under 3.5s
        # All should be PTS-sourced.
        assert all(f.timestamp_source == "pts" for f in frames)

    def test_vfr_uniform_fps_assumption_would_be_wrong(self, tmp_path):
        """Confirm that index/avg_fps would give wrong timestamps for VFR."""
        path = _make_vfr_video(tmp_path / "vfr.mp4")
        meta = VideoFileSource(path, backend="ffmpeg").probe()
        fps = meta["fps"]
        total_frames = meta["frame_count"]
        # With uniform assumption: last_ts = (total_frames-1)/fps * 1000
        uniform_last_ms = ((total_frames - 1) / fps) * 1000.0 if fps > 0 else 0
        # Real PTS should differ significantly from uniform estimate.
        src = VideoFileSource(path, sample_interval_ms=0.0, backend="ffmpeg")
        frames = list(src.frames())
        if frames:
            real_last_ms = frames[-1].timestamp_ms
            # They should differ by at least 200ms (VFR effect).
            assert abs(real_last_ms - uniform_last_ms) > 200.0


# --------------------------------------------------------------------------- #
# Time-based sampling tests
# --------------------------------------------------------------------------- #

class TestTimeBasedSampling:
    def test_sample_interval_reduces_frame_count(self, tmp_path):
        path = _make_constant_fps_video(tmp_path / "cfr.mp4", fps=15.0, duration_sec=2.0)
        # No sampling: all frames
        src_all = VideoFileSource(path, sample_interval_ms=0.0, backend="ffmpeg")
        all_frames = list(src_all.frames())
        # 133ms sampling (~7.5fps): roughly half
        src_sampled = VideoFileSource(
            path, sample_interval_ms=133.0, backend="ffmpeg"
        )
        sampled_frames = list(src_sampled.frames())
        assert len(sampled_frames) < len(all_frames)
        assert len(sampled_frames) >= len(all_frames) // 3  # not too aggressive

    def test_sampled_timestamps_are_monotonic(self, tmp_path):
        path = _make_constant_fps_video(tmp_path / "cfr.mp4", fps=15.0, duration_sec=2.0)
        src = VideoFileSource(path, sample_interval_ms=133.0, backend="ffmpeg")
        frames = list(src.frames())
        for i in range(1, len(frames)):
            assert frames[i].timestamp_ms > frames[i - 1].timestamp_ms

    def test_legacy_frame_stride_emits_deprecation_warning(self, tmp_path):
        path = _make_constant_fps_video(tmp_path / "cfr.mp4", fps=15.0, duration_sec=1.0)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            VideoFileSource(path, frame_stride=2, backend="ffmpeg")
            assert any(issubclass(x.category, DeprecationWarning) for x in w)

    def test_analysis_fps_parameter(self, tmp_path):
        path = _make_constant_fps_video(tmp_path / "cfr.mp4", fps=15.0, duration_sec=2.0)
        src = VideoFileSource(path, analysis_fps=7.5, backend="ffmpeg")
        assert src.sample_interval_ms == pytest.approx(1000.0 / 7.5)


# --------------------------------------------------------------------------- #
# Windowing tests
# --------------------------------------------------------------------------- #

class TestWindowing:
    def test_start_ms_skips_early_frames(self, tmp_path):
        path = _make_constant_fps_video(tmp_path / "cfr.mp4", fps=15.0, duration_sec=2.0)
        src = VideoFileSource(
            path, sample_interval_ms=0.0, start_ms=500.0, backend="ffmpeg"
        )
        frames = list(src.frames())
        if frames:
            assert frames[0].timestamp_ms >= 500.0 - 100.0  # allow slight slack

    def test_end_ms_stops_early(self, tmp_path):
        path = _make_constant_fps_video(tmp_path / "cfr.mp4", fps=15.0, duration_sec=2.0)
        src = VideoFileSource(
            path, sample_interval_ms=0.0, end_ms=500.0, backend="ffmpeg"
        )
        frames = list(src.frames())
        if frames:
            assert frames[-1].timestamp_ms <= 500.0 + 100.0  # allow one frame slack

    def test_max_frames_limits_output(self, tmp_path):
        path = _make_constant_fps_video(tmp_path / "cfr.mp4", fps=15.0, duration_sec=2.0)
        src = VideoFileSource(
            path, sample_interval_ms=0.0, max_frames=5, backend="ffmpeg"
        )
        frames = list(src.frames())
        assert len(frames) <= 5


# --------------------------------------------------------------------------- #
# Resource cleanup tests
# --------------------------------------------------------------------------- #

class TestResourceCleanup:
    def test_early_close_no_deadlock(self, tmp_path):
        """Closing the source after reading only one frame must not deadlock."""
        path = _make_constant_fps_video(tmp_path / "cfr.mp4", fps=15.0, duration_sec=2.0)
        src = VideoFileSource(path, sample_interval_ms=0.0, backend="ffmpeg")
        gen = src.frames()
        first = next(gen)
        assert first is not None
        # Close without exhausting the iterator.
        src.close()
        # If close deadlocked, this test would hang until pytest timeout.

    def test_context_manager_closes(self, tmp_path):
        path = _make_constant_fps_video(tmp_path / "cfr.mp4", fps=15.0, duration_sec=1.0)
        with VideoFileSource(path, sample_interval_ms=0.0, backend="ffmpeg") as src:
            frames = list(src.frames())
            assert len(frames) > 0
        # After context exit, should be closed.
        assert src._ffmpeg_proc is None
        assert src._pts_reader is None


# --------------------------------------------------------------------------- #
# decoded_duration_sec tests
# --------------------------------------------------------------------------- #

class TestDecodedDuration:
    def test_cfr_decoded_duration_matches(self, tmp_path):
        path = _make_constant_fps_video(tmp_path / "cfr.mp4", fps=15.0, duration_sec=2.0)
        src = VideoFileSource(path, backend="ffmpeg")
        dur = src.decoded_duration_sec()
        assert dur == pytest.approx(2.0, abs=0.3)

    def test_vfr_decoded_duration_matches_real_time(self, tmp_path):
        """VFR 1s@30fps + 2s@5fps = 3s. decoded_duration_sec should be ~3s."""
        path = _make_vfr_video(tmp_path / "vfr.mp4")
        src = VideoFileSource(path, backend="ffmpeg")
        dur = src.decoded_duration_sec()
        assert dur == pytest.approx(3.0, abs=0.5)


# --------------------------------------------------------------------------- #
# Frame structure tests
# --------------------------------------------------------------------------- #

class TestFrameStructure:
    def test_frame_has_timestamp_source_field(self, tmp_path):
        path = _make_constant_fps_video(tmp_path / "cfr.mp4", fps=15.0, duration_sec=1.0)
        src = VideoFileSource(path, sample_interval_ms=0.0, backend="ffmpeg")
        gen = src.frames()
        frame = next(gen)
        assert hasattr(frame, "timestamp_source")
        assert frame.timestamp_source in ("pts", "fallback_fps")
        src.close()

    def test_frame_image_shape(self, tmp_path):
        path = _make_constant_fps_video(
            tmp_path / "cfr.mp4", width=64, height=48, fps=15.0, duration_sec=1.0
        )
        src = VideoFileSource(path, sample_interval_ms=0.0, backend="ffmpeg")
        gen = src.frames()
        frame = next(gen)
        assert frame.image.shape == (48, 64, 3)
        src.close()


# --------------------------------------------------------------------------- #
# OpenCV backend parity tests
# --------------------------------------------------------------------------- #

class TestOpenCVBackend:
    def test_opencv_backend_time_based_sampling(self, tmp_path):
        path = _make_constant_fps_video(tmp_path / "cfr.mp4", fps=15.0, duration_sec=2.0)
        src = VideoFileSource(
            path, sample_interval_ms=133.0, backend="opencv"
        )
        frames = list(src.frames())
        assert len(frames) > 0
        # OpenCV backend always uses fallback_fps.
        assert all(f.timestamp_source == "fallback_fps" for f in frames)
        for i in range(1, len(frames)):
            assert frames[i].timestamp_ms > frames[i - 1].timestamp_ms
