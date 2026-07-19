"""Tests for local evidence attachment (agent path)."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from mousevision.agent_evidence import (
    attach_agent_evidence,
    pick_photo_for_session,
    resolve_session_window_ms,
    sample_video_frames,
)
from mousevision.agent_weigh import AgentSession


def _write_synthetic_video(
    path: Path, *, n_frames: int = 30, fps: float = 10.0, size=(64, 48)
) -> Path:
    """Solid-color frames; each frame slightly brighter so they're distinguishable."""
    w, h = size
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, fps, (w, h))
    try:
        for i in range(n_frames):
            img = np.full((h, w, 3), (i * 7) % 200, dtype=np.uint8)
            writer.write(img)
    finally:
        writer.release()
    return path


def test_resolve_session_window_priority_both_ends() -> None:
    sess = AgentSession(1, 16.0, 0.9, "x", t_start_s=3.0, t_end_s=7.0, t_stable_s=5.0)
    start, end, stable = resolve_session_window_ms(
        sess, video_duration_ms=60000.0, session_index=0, n_sessions=1, pad_s=1.0
    )
    # 3.0s - 1.0s pad = 2000ms; 7.0s + 1.0s pad = 8000ms; stable clamped mid.
    assert start == pytest.approx(2000.0)
    assert end == pytest.approx(8000.0)
    assert stable == pytest.approx(5000.0)


def test_resolve_session_window_only_stable() -> None:
    sess = AgentSession(1, 16.0, 0.9, "x", t_stable_s=10.0)
    start, end, stable = resolve_session_window_ms(
        sess,
        video_duration_ms=60000.0,
        session_index=0,
        n_sessions=1,
        pad_s=1.0,
        default_window_s=4.0,
    )
    assert start == pytest.approx(8000.0)
    assert end == pytest.approx(12000.0)
    assert stable == pytest.approx(10000.0)


def test_resolve_session_window_only_start() -> None:
    sess = AgentSession(1, 16.0, 0.9, "x", t_start_s=5.0)
    start, end, stable = resolve_session_window_ms(
        sess,
        video_duration_ms=60000.0,
        session_index=0,
        n_sessions=1,
        pad_s=1.0,
        default_window_s=6.0,
    )
    assert start == pytest.approx(5000.0)
    assert end == pytest.approx(11000.0)
    assert stable == pytest.approx(8000.0)


def test_resolve_session_window_no_times_even_partition() -> None:
    # 60s video, 3 sessions, no anchors → 20s partitions each.
    s1 = AgentSession(1, 16.0, 0.9, "x")
    s2 = AgentSession(2, 17.0, 0.9, "x")
    s3 = AgentSession(3, 18.0, 0.9, "x")
    sessions = [s1, s2, s3]
    starts = []
    ends = []
    for i, sess in enumerate(sessions):
        start, end, _ = resolve_session_window_ms(
            sess, video_duration_ms=60000.0, session_index=i, n_sessions=3
        )
        starts.append(start)
        ends.append(end)
    assert starts == [pytest.approx(v) for v in (0.0, 20000.0, 40000.0)]
    assert ends == [pytest.approx(v) for v in (20000.0, 40000.0, 60000.0)]


def test_resolve_session_window_clamp_and_min_width() -> None:
    # t_start and t_end very close → must widen to >= 300ms.
    sess = AgentSession(1, 16.0, 0.9, "x", t_start_s=5.0, t_end_s=5.05)
    start, end, _ = resolve_session_window_ms(
        sess, video_duration_ms=60000.0, session_index=0, n_sessions=1, pad_s=0.0
    )
    assert end - start >= 300.0


def test_sample_video_frames_basic(tmp_path: Path) -> None:
    video = _write_synthetic_video(tmp_path / "clip.mp4", n_frames=30, fps=10.0)
    frames = sample_video_frames(video, interval_ms=200.0)
    assert len(frames) > 0
    # 30 frames at 10fps = 3s; 200ms interval → expect ~15-16 frames.
    assert len(frames) <= 30
    ts_first = frames[0][0]
    ts_last = frames[-1][0]
    assert ts_last >= ts_first
    # Each frame carries a non-empty image.
    for _ts, _idx, img in frames:
        assert img is not None and img.size > 0


def test_sample_video_frames_missing_file(tmp_path: Path) -> None:
    frames = sample_video_frames(tmp_path / "does_not_exist.mp4")
    assert frames == []


def test_pick_photo_for_session_returns_midpoint_no_mouse(tmp_path: Path) -> None:
    # Solid-color synthetic frames; no mouse/OCR → score 0, midpoint selection.
    video = _write_synthetic_video(tmp_path / "clip.mp4", n_frames=30, fps=10.0)
    frames = sample_video_frames(video, interval_ms=200.0)
    assert frames
    pick = pick_photo_for_session(
        frames,
        (1000.0, 2000.0, 1500.0),
        target_weight=16.0,
        weight_tol=0.25,
        reader=None,
        mouse_detect_cfg={},
    )
    assert pick is not None
    assert pick["timestamp_ms"] >= 1000.0 and pick["timestamp_ms"] <= 2000.0
    assert pick["selection"] == "window"
    assert pick["mouse_detected"] is False
    assert pick["platform_start_ms"] == 1000.0
    assert pick["platform_end_ms"] == 2000.0


def test_pick_photo_for_session_no_frames_returns_none() -> None:
    pick = pick_photo_for_session(
        [],
        (1000.0, 2000.0, 1500.0),
        target_weight=16.0,
        weight_tol=0.25,
        reader=None,
        mouse_detect_cfg={},
    )
    assert pick is None


def test_attach_agent_evidence_creates_photo_and_updates_record(
    tmp_path: Path,
) -> None:
    video = _write_synthetic_video(tmp_path / "src.mp4", n_frames=40, fps=10.0)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    mouse_dir = run_dir / "mouse_001"
    mouse_dir.mkdir()
    record = {
        "box_id": "0001",
        "weight": 16.0,
        "confidence": 0.9,
        "ordinal": 1,
        "photo": "photo.jpg",
    }
    (mouse_dir / "record.json").write_text(json.dumps(record), encoding="utf-8")

    sessions = [AgentSession(1, 16.0, 0.9, "ok", t_stable_s=2.0)]
    out = attach_agent_evidence(
        records=[record],
        sessions=sessions,
        video_path=video,
        run_dir=run_dir,
        config={},
        templates_dir=None,
    )
    assert len(out) == 1
    photo = mouse_dir / "photo.jpg"
    assert photo.is_file(), "photo.jpg should be written"
    rec = json.loads((mouse_dir / "record.json").read_text(encoding="utf-8"))
    assert rec["photo_saved"] is True
    assert "platform_start_ms" in rec
    assert "platform_end_ms" in rec
    assert isinstance(rec["platform_start_ms"], float)
    assert isinstance(rec["platform_end_ms"], float)
    assert rec["platform_end_ms"] > rec["platform_start_ms"]
    assert "clip_start_ms" in rec
    assert "agent_photo_score" in rec
    # In-memory record is also updated.
    assert out[0]["photo_saved"] is True
    assert out[0]["platform_end_ms"] > 0.0


def test_attach_agent_evidence_no_times_still_writes_photo(tmp_path: Path) -> None:
    video = _write_synthetic_video(tmp_path / "src.mp4", n_frames=30, fps=10.0)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    mouse_dir = run_dir / "mouse_001"
    mouse_dir.mkdir()
    record = {"box_id": "0001", "weight": 16.0, "confidence": 0.9, "ordinal": 1}
    (mouse_dir / "record.json").write_text(json.dumps(record), encoding="utf-8")

    sessions = [AgentSession(1, 16.0, 0.9, "ok")]  # no time anchors
    out = attach_agent_evidence(
        records=[record],
        sessions=sessions,
        video_path=video,
        run_dir=run_dir,
        config={},
        templates_dir=None,
    )
    assert (mouse_dir / "photo.jpg").is_file()
    rec = json.loads((mouse_dir / "record.json").read_text(encoding="utf-8"))
    assert rec["photo_saved"] is True
    assert rec["platform_start_ms"] == pytest.approx(0.0, abs=1e-6)


def test_attach_agent_evidence_missing_video_soft_fail(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    record = {"box_id": "0001", "weight": 16.0, "confidence": 0.9, "ordinal": 1}
    out = attach_agent_evidence(
        records=[record],
        sessions=[AgentSession(1, 16.0, 0.9, "ok")],
        video_path=tmp_path / "missing.mp4",
        run_dir=run_dir,
        config={},
        templates_dir=None,
    )
    # Should return records unchanged, no exception, no photo.
    assert len(out) == 1
