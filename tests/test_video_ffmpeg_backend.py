"""ffmpeg raw-BGR decode path for VideoFileSource."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import cv2
import numpy as np
import pytest

from mousevision.source.video import (
    VideoFileSource,
    resolve_video_backend,
)


def _solid_video(path: Path, w: int = 80, h: int = 40, frames: int = 6, fps: float = 10.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, fps, (w, h))
    for i in range(frames):
        img = np.zeros((h, w, 3), dtype=np.uint8)
        img[:, :] = (i * 40 % 255, 20, 80)
        writer.write(img)
    writer.release()
    return path


@pytest.fixture(scope="module")
def ffmpeg_available() -> bool:
    return shutil.which(os.environ.get("MOUSEVISION_FFMPEG") or "ffmpeg") is not None


def test_resolve_backend_respects_explicit_opencv():
    assert resolve_video_backend("opencv") == "opencv"


def test_resolve_backend_auto_prefers_ffmpeg_when_present(ffmpeg_available: bool):
    if not ffmpeg_available:
        assert resolve_video_backend("auto") == "opencv"
        return
    assert resolve_video_backend("auto") == "ffmpeg"


def test_ffmpeg_and_opencv_agree_on_frame_count_and_shape(tmp_path: Path, ffmpeg_available: bool):
    if not ffmpeg_available:
        pytest.skip("ffmpeg not installed")
    video = _solid_video(tmp_path / "clip.mp4", frames=6)
    ff = list(VideoFileSource(video, backend="ffmpeg").frames())
    oc = list(VideoFileSource(video, backend="opencv").frames())
    assert len(ff) == len(oc) == 6
    assert ff[0].image.shape == oc[0].image.shape == (40, 80, 3)
    assert [f.index for f in ff] == [f.index for f in oc]


def test_ffmpeg_respects_stride_and_window(tmp_path: Path, ffmpeg_available: bool):
    if not ffmpeg_available:
        pytest.skip("ffmpeg not installed")
    video = _solid_video(tmp_path / "clip.mp4", frames=10, fps=10.0)
    # 10 fps → frame i at i*100 ms; window 200–550 ms covers indices 2..5.
    src = VideoFileSource(
        video,
        backend="ffmpeg",
        frame_stride=2,
        start_ms=200,
        end_ms=550,
    )
    frames = list(src.frames())
    assert [f.index for f in frames] == [2, 4]
    assert frames[0].timestamp_ms == pytest.approx(200.0, abs=1.0)
    assert frames[-1].timestamp_ms <= 550.0 + 1.0


def test_ffmpeg_crop_and_target_size(tmp_path: Path, ffmpeg_available: bool):
    if not ffmpeg_available:
        pytest.skip("ffmpeg not installed")
    video = _solid_video(tmp_path / "clip.mp4", w=80, h=40, frames=3)
    crop = {"x": 0.25, "y": 0.0, "w": 0.5, "h": 1.0}
    frames = list(
        VideoFileSource(
            video,
            backend="ffmpeg",
            crop=crop,
            target_size=(720, 1280),
        ).frames()
    )
    assert len(frames) == 3
    assert frames[0].image.shape[:2] == (1280, 720)


def test_env_backend_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    video = _solid_video(tmp_path / "clip.mp4", frames=2)
    monkeypatch.setenv("MOUSEVISION_VIDEO_BACKEND", "opencv")
    src = VideoFileSource(video)
    assert src.backend == "opencv"
    assert len(list(src.frames())) == 2
