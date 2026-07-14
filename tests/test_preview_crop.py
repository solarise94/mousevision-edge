"""Tests for the per-frame preview crop in VideoFileSource.

Mobile records a landscape stream but previews a portrait center crop. The
crop parameter reproduces the visible region so analysis only sees what the
operator framed. crop=None keeps the legacy full-frame behaviour.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from mousevision.jobs import _parse_preview_crop
from mousevision.source.video import VideoFileSource


def _solid_video(path: Path, w: int, h: int, frames: int = 4) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, 10.0, (w, h))
    # Each frame is a horizontal gradient so the left/centre/right columns are
    # distinguishable after cropping.
    for _ in range(frames):
        img = np.zeros((h, w, 3), dtype=np.uint8)
        for x in range(w):
            img[:, x] = (x * 255 // max(1, w - 1), 0, 0)
        writer.write(img)
    writer.release()
    return path


def test_no_crop_yields_full_frame(tmp_path: Path):
    video = _solid_video(tmp_path / "clip.mp4", w=80, h=40)
    src = VideoFileSource(video)
    frames = list(src.frames())
    assert len(frames) == 4
    assert frames[0].image.shape[:2] == (40, 80)


def test_center_crop_reduces_width(tmp_path: Path):
    video = _solid_video(tmp_path / "clip.mp4", w=80, h=40)
    # A portrait center slice: keep full height, centre 50% of the width.
    crop = {"x": 0.25, "y": 0.0, "w": 0.5, "h": 1.0}
    src = VideoFileSource(video, crop=crop)
    frames = list(src.frames())
    assert len(frames) == 4
    h, w = frames[0].image.shape[:2]
    assert h == 40
    assert w == 40  # 50% of 80
    # The crop is centred on a horizontal gradient, so the leftmost column of
    # the cropped frame should be brighter than the full frame's leftmost column.
    full = next(VideoFileSource(video).frames()).image
    assert frames[0].image[0, 0, 0] > full[0, 0, 0]


def test_timestamps_and_indices_unaffected_by_crop(tmp_path: Path):
    video = _solid_video(tmp_path / "clip.mp4", w=80, h=40, frames=4)
    crop = {"x": 0.25, "y": 0.0, "w": 0.5, "h": 1.0}
    cropped = list(VideoFileSource(video, crop=crop).frames())
    full = list(VideoFileSource(video).frames())
    assert [f.index for f in cropped] == [f.index for f in full]
    assert [f.timestamp_ms for f in cropped] == [f.timestamp_ms for f in full]


def test_target_size_resizes_after_crop(tmp_path: Path):
    # The cropped frame (40x40) must be resized to the reference geometry so
    # fixed-pixel detector thresholds stay valid. Without target_size the
    # cropped frame stays at native resolution.
    video = _solid_video(tmp_path / "clip.mp4", w=80, h=40)
    crop = {"x": 0.25, "y": 0.0, "w": 0.5, "h": 1.0}
    src = VideoFileSource(video, crop=crop, target_size=(720, 1280))
    frames = list(src.frames())
    assert len(frames) == 4
    h, w = frames[0].image.shape[:2]
    assert (w, h) == (720, 1280)


def test_target_size_ignored_without_crop(tmp_path: Path):
    # target_size only applies alongside a crop; a full-frame source must not be
    # resized (legacy behaviour for CLI / playback).
    video = _solid_video(tmp_path / "clip.mp4", w=80, h=40)
    src = VideoFileSource(video, target_size=(720, 1280))
    frames = list(src.frames())
    assert frames[0].image.shape[:2] == (40, 80)



def test_parse_preview_crop_valid():
    out = _parse_preview_crop('{"x":0.34,"y":0,"w":0.32,"h":1}')
    assert out == {"x": 0.34, "y": 0.0, "w": 0.32, "h": 1.0}


def test_parse_preview_crop_clamps_out_of_range():
    out = _parse_preview_crop('{"x":-0.5,"y":0,"w":2,"h":1}')
    assert out == {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}


@pytest.mark.parametrize("raw", [None, "", "not-json", "{}", "[]",
                                  '{"x":0.1}', '{"x":"a","y":0,"w":1,"h":1}',
                                  '{"x":0,"y":0,"w":0,"h":1}',
                                  '{"x":0,"y":0,"w":1,"h":0}'])
def test_parse_preview_crop_invalid_returns_none(raw):
    assert _parse_preview_crop(raw) is None


@pytest.mark.parametrize("raw", [
    # Reviewer example: x=0.9, w=0.9 overhangs the right edge (0.9+0.9=1.8>1).
    # Must be rejected, NOT silently analysed as the rightmost 10%.
    '{"x":0.9,"y":0,"w":0.9,"h":1}',
    '{"x":0,"y":0.9,"w":1,"h":0.9}',
    '{"x":0.5,"y":0,"w":0.6,"h":1}',
    '{"x":0,"y":0.5,"w":1,"h":0.6}',
])
def test_parse_preview_crop_rejects_overhanging_rectangle(raw):
    assert _parse_preview_crop(raw) is None


def test_parse_preview_crop_edge_exactly_one_is_valid():
    # x+w == 1.0 exactly (after clamp) is the right edge - valid.
    assert _parse_preview_crop('{"x":0.5,"y":0,"w":0.5,"h":1}') == {
        "x": 0.5, "y": 0.0, "w": 0.5, "h": 1.0
    }
    assert _parse_preview_crop('{"x":0,"y":0.3,"w":1,"h":0.7}') == {
        "x": 0.0, "y": 0.3, "w": 1.0, "h": 0.7
    }


def test_job_store_roundtrips_preview_crop(tmp_path: Path):
    from mousevision.jobs import JobStore

    store = JobStore(tmp_path / "jobs.db")
    job = store.create_job(
        project_id="p", cage_id="C57-023",
        original_filename="x.mp4", content_type="video/mp4",
    )
    blob = '{"x":0.34,"y":0,"w":0.32,"h":1}'
    store.update(job["job_id"], preview_crop=blob)

    # A fresh store (simulating a restart) must read the column back.
    reopened = JobStore(tmp_path / "jobs.db")
    loaded = reopened.get(job["job_id"])
    assert loaded is not None
    assert loaded["preview_crop"] == blob
