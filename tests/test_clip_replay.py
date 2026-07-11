"""Clip boundary and video seek tests."""

from pathlib import Path

from mousevision.clip import clip_bounds_from_record
from mousevision.source.video import VideoFileSource


def test_clip_bounds_from_state_history():
    record = {
        "state_history": [
            {"current": "ENTER", "t_ms": 33000.0},
            {"current": "WEIGHING", "t_ms": 33266.0},
            {"current": "LEAVE", "t_ms": 36666.0},
            {"current": "ANALYZE", "t_ms": 36733.0},
        ]
    }
    start, end = clip_bounds_from_record(record, pad_before_ms=800, pad_after_ms=800)
    assert start == 32200.0
    assert end == 37533.0


def test_clip_bounds_prefers_explicit_fields():
    start, end = clip_bounds_from_record(
        {"clip_start_ms": 1000, "clip_end_ms": 2000, "state_history": []}
    )
    assert start == 1000
    assert end == 2000


def test_video_source_respects_end_ms():
    video = Path("RefVideo/9494224d488d6e735c0f108cc5562a2d.mp4")
    if not video.exists():
        return
    # Mouse #7 window roughly 32–37s; take a short slice.
    src = VideoFileSource(video, frame_stride=5, start_ms=33000, end_ms=34000)
    frames = list(src.frames())
    src.close()
    assert frames
    assert frames[0].timestamp_ms >= 32900
    assert frames[-1].timestamp_ms <= 34000 + 200
    # Must not start near t=0.
    assert frames[0].index > 100
