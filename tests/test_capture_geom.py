"""Tests for canvas capture geometry and capture_mode pipeline wiring."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest

from mousevision.capture_geom import (
    GUIDE_MOUSE,
    GUIDE_WEIGHT,
    center_crop_source_rect,
    guide_pixel_roi,
    is_near_canvas_aspect,
    parse_capture_meta,
    validate_canvas_video_geometry,
)
from mousevision.jobs import AnalysisJobManager, JobStore
from mousevision.pipeline import WeighingPipeline
from mousevision.source.video import VideoFileSource, VideoFormatError


def _portrait_video(path: Path, w: int = 720, h: int = 1280, frames: int = 3) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, 10.0, (w, h))
    for i in range(frames):
        writer.write(np.full((h, w, 3), i * 40, dtype=np.uint8))
    writer.release()
    return path


def _canvas_meta() -> str:
    return (
        '{"client_version":"2026.07.14-canvas",'
        '"canvas_width":720,"canvas_height":1280,"source_width":1280}'
    )


def test_center_crop_landscape_to_portrait():
    # 1280x720 into 720x1280: crop left/right, full height.
    rect = center_crop_source_rect(1280, 720, 720, 1280)
    assert rect["sy"] == 0.0
    assert rect["sh"] == 720.0
    assert abs(rect["sw"] - 720 * (720 / 1280)) < 1e-6
    assert abs(rect["sx"] - (1280 - rect["sw"]) / 2) < 1e-6


def test_center_crop_already_portrait():
    # Exact 9:16 source: no crop needed.
    rect = center_crop_source_rect(720, 1280, 720, 1280)
    assert rect == {"sx": 0.0, "sy": 0.0, "sw": 720.0, "sh": 1280.0}


def test_center_crop_taller_than_destination():
    # Taller than 9:16: crop top/bottom.
    rect = center_crop_source_rect(720, 1600, 720, 1280)
    assert rect["sx"] == 0.0
    assert rect["sw"] == 720.0
    assert abs(rect["sh"] - 720 / (720 / 1280)) < 1e-6
    assert abs(rect["sy"] - (1600 - rect["sh"]) / 2) < 1e-6


def test_guide_pixel_roi_matches_css_fractions():
    mx, my, mw, mh = guide_pixel_roi(GUIDE_MOUSE)
    assert (mx, my, mw, mh) == (50, 77, 619, 614)
    wx, wy, ww, wh = guide_pixel_roi(GUIDE_WEIGHT)
    assert (wx, wy, ww, wh) == (144, 819, 432, 320)


def test_is_near_canvas_aspect():
    assert is_near_canvas_aspect(720, 1280)
    assert is_near_canvas_aspect(718, 1280)  # ~0.16% drift, within 1%
    assert not is_near_canvas_aspect(700, 1280)  # ~2.8% drift
    assert not is_near_canvas_aspect(1280, 720)
    assert not is_near_canvas_aspect(0, 1280)


def test_parse_capture_meta_requires_720x1280():
    assert parse_capture_meta(_canvas_meta()) is not None
    assert parse_capture_meta('{"canvas_width":1280,"canvas_height":720}') is None
    assert parse_capture_meta("not-json") is None
    assert parse_capture_meta(None) is None


def test_validate_canvas_rejects_landscape():
    with pytest.raises(ValueError, match="尺寸异常"):
        validate_canvas_video_geometry(1280, 720, capture_meta=_canvas_meta())


def test_validate_canvas_rejects_bad_meta():
    with pytest.raises(ValueError, match="元数据"):
        validate_canvas_video_geometry(720, 1280, capture_meta='{"canvas_width":1}')


def test_no_crop_720x1280_passthrough(tmp_path: Path):
    path = _portrait_video(tmp_path / "ref.mp4")
    frames = list(VideoFileSource(path).frames())
    assert frames[0].image.shape[:2] == (1280, 720)


def test_job_store_roundtrips_capture_mode(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.db")
    job = store.create_job(
        project_id="default",
        cage_id="C1",
        original_filename="a.mp4",
        content_type="video/mp4",
    )
    meta = _canvas_meta()
    store.update(
        job["job_id"],
        capture_mode="canvas",
        capture_meta=meta,
        preview_crop='{"x":0.1,"y":0,"w":0.8,"h":1}',
    )
    loaded = store.get(job["job_id"])
    assert loaded is not None
    assert loaded["capture_mode"] == "canvas"
    assert loaded["capture_meta"] == meta


def test_canvas_mode_ignores_preview_crop(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.db")
    video = _portrait_video(tmp_path / "clip.mp4")
    job = store.create_job(
        project_id="default",
        cage_id="C1",
        original_filename="clip.mp4",
        content_type="video/mp4",
        requested_ordinal=1,
    )
    store.update(
        job["job_id"],
        status="queued",
        video_path=str(video),
        capture_mode="canvas",
        capture_meta=_canvas_meta(),
        # Deliberately wrong crop — must NOT be forwarded when mode=canvas.
        preview_crop='{"x":0.9,"y":0,"w":0.1,"h":1}',
    )
    job = store.get(job["job_id"])
    assert job is not None

    captured: dict = {}

    class StubPipeline:
        def run_video(self, *args, **kwargs):
            captured.update(kwargs)
            result = MagicMock()
            result.samples = 2
            result.records = []
            result.run_dir = None
            result.run_id = None
            return result

    manager = AnalysisJobManager(
        store,
        output_root=tmp_path / "out",
        config_path=tmp_path / "missing.yaml",
        templates_dir=tmp_path,
        analysis_fn=lambda j: {"ok": True},
    )
    manager._pipeline = StubPipeline()  # type: ignore[assignment]
    manager._run_pipeline(job)

    assert captured.get("crop") is None
    assert captured.get("normalize_to_reference") is True


def test_canvas_mode_rejects_landscape_video(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.db")
    video = _portrait_video(tmp_path / "wide.mp4", w=1280, h=720)
    job = store.create_job(
        project_id="default",
        cage_id="C1",
        original_filename="wide.mp4",
        content_type="video/mp4",
        requested_ordinal=1,
    )
    store.update(
        job["job_id"],
        status="queued",
        video_path=str(video),
        capture_mode="canvas",
        capture_meta=_canvas_meta(),
    )
    job = store.get(job["job_id"])
    assert job is not None

    manager = AnalysisJobManager(
        store,
        output_root=tmp_path / "out",
        config_path=tmp_path / "missing.yaml",
        templates_dir=tmp_path,
        analysis_fn=lambda j: {"ok": True},
    )
    manager._pipeline = MagicMock()  # type: ignore[assignment]
    with pytest.raises(VideoFormatError, match="尺寸异常"):
        manager._run_pipeline(job)
    manager._pipeline.run_video.assert_not_called()


def test_css_crop_mode_still_forwards_crop(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.db")
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"stub")
    job = store.create_job(
        project_id="default",
        cage_id="C1",
        original_filename="clip.mp4",
        content_type="video/mp4",
        requested_ordinal=1,
    )
    crop_blob = '{"x":0.25,"y":0,"w":0.5,"h":1}'
    store.update(
        job["job_id"],
        status="queued",
        video_path=str(video),
        capture_mode="css_crop",
        preview_crop=crop_blob,
    )
    job = store.get(job["job_id"])
    assert job is not None

    captured: dict = {}

    class StubPipeline:
        def run_video(self, *args, **kwargs):
            captured.update(kwargs)
            result = MagicMock()
            result.samples = 2
            result.records = []
            result.run_dir = None
            result.run_id = None
            return result

    manager = AnalysisJobManager(
        store,
        output_root=tmp_path / "out",
        config_path=tmp_path / "missing.yaml",
        templates_dir=tmp_path,
        analysis_fn=lambda j: {"ok": True},
    )
    manager._pipeline = StubPipeline()  # type: ignore[assignment]
    manager._run_pipeline(job)

    assert captured.get("crop") == {"x": 0.25, "y": 0.0, "w": 0.5, "h": 1.0}
    assert captured.get("normalize_to_reference") is False


def test_pipeline_writes_analysis_preview(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    templates = root / "assets" / "templates"
    cfg_path = root / "configs" / "scale_refvideo.yaml"
    if not templates.is_dir() or not cfg_path.is_file():
        pytest.skip("templates/config missing")

    from mousevision.pipeline import load_config

    config = load_config(cfg_path)
    video = _portrait_video(tmp_path / "clip.mp4", frames=4)

    pipe = WeighingPipeline(config, templates)
    out = tmp_path / "runs"
    result = pipe.run_video(
        str(video),
        cage_id="T1",
        output_root=out,
        stop_after_first=True,
        create_run=True,
        persist=True,
        normalize_to_reference=True,
    )
    assert result.run_dir is not None
    preview = Path(result.run_dir) / "analysis_preview.jpg"
    assert preview.is_file()
    img = cv2.imread(str(preview))
    assert img is not None
    assert img.shape[1] == 720
    assert img.shape[0] == 1280
