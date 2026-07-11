"""Tests for resolving run source video and ordinal separation."""

from __future__ import annotations

import cv2
import numpy as np

from mousevision.run import create_run_dir, finish_run
from mousevision.upload_queue import UploadQueue
from ui.app import PlaybackEngine
from ui.registry import MouseRegistry


def _fake_video(path: Path, frames: int = 5) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, 10.0, (64, 64))
    for i in range(frames):
        img = np.zeros((64, 64, 3), dtype=np.uint8)
        img[:] = (i * 40) % 255
        writer.write(img)
    writer.release()
    return path


def test_resolve_run_video_from_manifest(tmp_path: Path):
    output = tmp_path / "output"
    video = _fake_video(tmp_path / "clips" / "batch_a.mp4")
    run_dir, man = create_run_dir(
        output, cage_id="C57-023", mode="video", source_id=str(video)
    )
    finish_run(run_dir)
    reg = MouseRegistry(tmp_path / "reg.json", output)
    reg.set_active_run(man["run_id"], run_dir)
    engine = PlaybackEngine(reg, UploadQueue(tmp_path / "q.db"))
    engine.output_root = output
    engine.video_path = tmp_path / "other_default.mp4"
    resolved = engine._resolve_run_video(man["run_id"])
    assert resolved is not None
    assert resolved.resolve() == video.resolve()


def test_resolve_run_video_missing_returns_none(tmp_path: Path):
    output = tmp_path / "output"
    run_dir, man = create_run_dir(
        output, cage_id="C57-023", source_id=str(tmp_path / "missing.mp4")
    )
    finish_run(run_dir)
    reg = MouseRegistry(tmp_path / "reg.json", output)
    engine = PlaybackEngine(reg, UploadQueue(tmp_path / "q.db"))
    engine.output_root = output
    assert engine._resolve_run_video(man["run_id"]) is None
