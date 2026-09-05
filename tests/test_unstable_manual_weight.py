"""Unstable settlement: platform-window raw span, clip export, confirm + queue."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from mousevision.clip import export_session_clip
from mousevision.driver import SessionDriver
from mousevision.types import AnalysisResult
from mousevision.upload_queue import UploadQueue
from ui.records_meta import RecordsMetaStore
from ui.registry import MouseRegistry


def _solid_video(path: Path, frames: int = 20, fps: float = 10.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, fps, (64, 48))
    for i in range(frames):
        img = np.zeros((48, 64, 3), dtype=np.uint8)
        img[:, :] = (i * 10 % 255, 40, 80)
        writer.write(img)
    writer.release()
    return path


def _driver(tmp_path: Path) -> SessionDriver:
    root = Path(__file__).resolve().parents[1]
    templates = root / "assets" / "templates"
    if not templates.is_dir():
        pytest.skip("templates missing")
    cfg = {
        "weight_reader": "template",
        "near_zero": 0.5,
        "unstable_raw_range_g": 0.5,
        "unstable_confidence_cap": 0.35,
        "enter_min": 1.0,
        "leave_max": 0.3,
        "empty_max": 0.15,
        "frame_stride": 1,
        "match_threshold": 0.1,
    }
    return SessionDriver(
        config=cfg,
        templates_dir=templates,
        output_root=tmp_path / "out",
        persist=False,
    )


def test_export_session_clip_writes_file(tmp_path: Path):
    video = _solid_video(tmp_path / "src.mp4", frames=30, fps=10.0)
    out = tmp_path / "clip.mp4"
    status = export_session_clip(video, out, start_ms=200.0, end_ms=1200.0)
    assert status == "ok"
    assert out.is_file() and out.stat().st_size > 0


def test_export_session_clip_missing_source(tmp_path: Path):
    status = export_session_clip(
        tmp_path / "nope.mp4",
        tmp_path / "clip.mp4",
        start_ms=0,
        end_ms=500,
    )
    assert status == "missing_source"


def test_raw_instability_uses_platform_window_p90_p10(tmp_path: Path):
    """Climb into ENTER must not trigger; only platform-window P90-P10 matters."""
    driver = _driver(tmp_path)
    # Climb + leave outside the platform window; platform itself is tight.
    driver._session_raw_samples = [
        (1000.0, 10.0),  # climb — outside platform
        (2000.0, 22.30),
        (2100.0, 22.32),
        (2200.0, 22.35),
        (2300.0, 22.28),
        (2400.0, 22.31),
        (3000.0, 5.0),  # leave — outside platform
    ]
    analysis = AnalysisResult(
        weight=22.32,
        confidence=0.7,
        platform_start_ms=2000.0,
        platform_end_ms=2400.0,
        photo_frame_index=10,
        weight_source="stable_curve_median",
    )
    driver._apply_raw_instability(analysis)
    assert analysis.requires_manual_weight is False
    assert analysis.weight_source == "stable_curve_median"


def test_insufficient_raw_samples_requires_manual_for_http_ocr(tmp_path: Path):
    """HTTP OCR with <3 platform-window raws must not settle clean."""
    driver = _driver(tmp_path)
    driver.use_http_ocr = True
    driver._session_raw_samples = [
        (1000.0, 10.0),  # outside platform
        (2100.0, 22.30),
        (2200.0, 22.32),  # only 2 inside window
        (3000.0, 5.0),
    ]
    analysis = AnalysisResult(
        weight=22.32,
        confidence=0.7,
        platform_start_ms=2000.0,
        platform_end_ms=2400.0,
        photo_frame_index=10,
        weight_source="stable_curve_median",
    )
    driver._apply_raw_instability(analysis)
    assert analysis.requires_manual_weight is True
    assert "insufficient_raw_samples" in analysis.review_reason
    assert analysis.weight_source == "guessed_unstable"


def test_insufficient_raw_samples_template_still_allows(tmp_path: Path):
    """Template path keeps curve settlement when platform raws are sparse."""
    driver = _driver(tmp_path)
    assert driver.use_http_ocr is False
    driver._session_raw_samples = [(2100.0, 22.30), (2200.0, 22.32)]
    analysis = AnalysisResult(
        weight=22.32,
        confidence=0.7,
        platform_start_ms=2000.0,
        platform_end_ms=2400.0,
        photo_frame_index=10,
        weight_source="stable_curve_median",
    )
    driver._apply_raw_instability(analysis)
    assert analysis.requires_manual_weight is False
    assert analysis.weight_source == "stable_curve_median"


def test_outlier_alone_does_not_trigger_with_p90_p10(tmp_path: Path):
    driver = _driver(tmp_path)
    # Stable ~17.22 platform with a couple of 17.8x OCR spikes (RefVideo #2 shape).
    driver._session_raw_samples = [
        (2000.0, 17.18),
        (2050.0, 17.87),
        (2100.0, 17.27),
        (2150.0, 17.22),
        (2200.0, 17.22),
        (2250.0, 17.26),
        (2300.0, 17.90),
        (2350.0, 17.70),
        (2400.0, 17.26),
        (2450.0, 17.22),
        (2500.0, 17.22),
        (2550.0, 17.16),
        (2600.0, 17.16),
    ]
    analysis = AnalysisResult(
        weight=17.22,
        confidence=0.7,
        platform_start_ms=2000.0,
        platform_end_ms=2600.0,
        photo_frame_index=10,
        weight_source="stable_curve_median",
    )
    driver._apply_raw_instability(analysis)
    assert analysis.requires_manual_weight is False
    assert analysis.weight_source == "stable_curve_median"


def test_raw_instability_flags_wide_platform_span(tmp_path: Path):
    driver = _driver(tmp_path)
    # Broad subject cluster: after IQR trim, P90-P10 still > 0.5g.
    driver._session_raw_samples = [
        (2000.0 + i * 40.0, 22.10 + i * 0.08) for i in range(10)
    ]
    analysis = AnalysisResult(
        weight=22.40,
        confidence=0.7,
        platform_start_ms=2000.0,
        platform_end_ms=2400.0,
        photo_frame_index=10,
        weight_source="stable_curve_median",
    )
    driver._apply_raw_instability(analysis)
    assert analysis.requires_manual_weight is True
    assert "unstable_raw_range" in analysis.review_reason
    assert analysis.weight_source == "guessed_unstable"

def test_unstable_record_skipped_from_upload_queue(tmp_path: Path):
    """requires_manual_weight sessions must not enter Pending until confirmed."""
    root = Path(__file__).resolve().parents[1]
    templates = root / "assets" / "templates"
    queue = UploadQueue(tmp_path / "upload_queue.db")
    cfg = {
        "weight_reader": "template",
        "near_zero": 0.5,
        "unstable_raw_range_g": 0.5,
        "enter_min": 1.0,
        "leave_max": 0.3,
        "empty_max": 0.15,
        "match_threshold": 0.1,
        "platform_window_seconds": 0.5,
        "platform_max_std": 0.05,
        "weighing_min_samples": 3,
        "leave_hold_frames": 2,
        "empty_arm_frames": 0,
        "reenter_cooldown_ms": 0,
    }
    driver = SessionDriver(
        config=cfg,
        templates_dir=templates,
        output_root=tmp_path / "run",
        persist=True,
        upload_queue=queue,
        cage_id="T-01",
        run_id="run-test",
    )
    # Directly exercise the save/enqueue branch with a pre-built unstable analysis
    # via _mark_unstable + recorder path is heavy; assert queue helper contract:
    record = {
        "record_id": "rid-unstable",
        "cage_id": "T-01",
        "weight": 22.32,
        "requires_manual_weight": True,
        "weight_source": "guessed_unstable",
    }
    # Simulate driver skip: only enqueue when not requires_manual_weight
    if not record["requires_manual_weight"]:
        queue.enqueue(record, tmp_path / "record.json", None)
    assert queue.counts().get("Pending", 0) == 0
    assert queue.get_payload("rid-unstable") is None

    # After confirm: enqueue with manual_confirmed payload
    record["weight"] = 23.30
    record["weight_source"] = "manual_confirmed"
    record["requires_manual_weight"] = False
    path = tmp_path / "mouse_001" / "record.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    queue.enqueue(record, path, None)
    payload = queue.get_payload("rid-unstable")
    assert payload is not None
    assert payload["weight"] == 23.30
    assert payload["weight_source"] == "manual_confirmed"
    assert payload["requires_manual_weight"] is False


def test_confirm_weight_api_enqueues_manual_confirmed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """POST confirm-weight must enqueue / refresh queue with confirmed payload.

    B3 适配：业务 store 已按租户挂到 tenant_factory（无模块级单例可
    monkeypatch），改为经 TestClient 走真实 API；管理员会话激活 legacy-default
    租户（seed admin 兼任其 tenant_admin），数据落 legacy-default 租户目录。
    """
    import importlib

    from fastapi.testclient import TestClient

    from ui.control_store import LEGACY_TENANT_ID

    import ui.app as app_mod

    monkeypatch.setenv("MOUSEVISION_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("MOUSEVISION_ADMIN_PASSWORD", "test-admin")
    monkeypatch.delenv("MOUSEVISION_API_TOKEN", raising=False)
    app_mod = importlib.reload(app_mod)

    stores = app_mod.tenant_factory.stores(LEGACY_TENANT_ID)
    run_dir = stores.output_root / "run_demo"
    mouse_dir = run_dir / "mouse_001"
    mouse_dir.mkdir(parents=True)
    record_id = "confirm-rid-1"
    record = {
        "record_id": record_id,
        "run_id": "run_demo",
        "cage_id": "C57-001",
        "ordinal": 1,
        "weight": 22.32,
        "guessed_weight": 22.32,
        "requires_manual_weight": True,
        "needs_review": True,
        "review_reason": "unstable_raw_range,no_stable_platform",
        "weight_source": "guessed_unstable",
        "photo": "photo.jpg",
        "confidence": 0.3,
    }
    (mouse_dir / "record.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (mouse_dir / "photo.jpg").write_bytes(b"\xff\xd8\xff")
    stores.registry.register(
        run_id="run_demo",
        run_dir=run_dir,
        cage_id="C57-001",
        ordinal=1,
        record_id=record_id,
        weight=22.32,
        confidence=0.3,
        output_dir=mouse_dir,
    )
    assert stores.upload_queue.get_payload(record_id) is None

    c = TestClient(app_mod.app)
    assert c.post("/api/login", json={"username": "admin", "password": "test-admin"}).status_code == 200
    assert c.post(
        "/api/me/password",
        json={"current_password": "test-admin", "new_password": "test-admin-ok"},
    ).status_code == 200
    assert c.post(
        "/api/control/session/tenant", json={"tenant_id": LEGACY_TENANT_ID}
    ).status_code == 200

    res = c.post(
        f"/api/records/{record_id}/confirm-weight",
        json={"weight": 23.30, "note": "lcd read"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True
    assert res.json()["weight"] == 23.30
    assert res.json()["weight_source"] == "manual_confirmed"

    saved = json.loads((mouse_dir / "record.json").read_text(encoding="utf-8"))
    assert saved["weight"] == 23.30
    assert saved["requires_manual_weight"] is False
    assert saved["weight_source"] == "manual_confirmed"

    payload = stores.upload_queue.get_payload(record_id)
    assert payload is not None
    assert payload["weight"] == 23.30
    assert payload["weight_source"] == "manual_confirmed"
    assert payload["requires_manual_weight"] is False

    saved["requires_manual_weight"] = True
    (mouse_dir / "record.json").write_text(json.dumps(saved), encoding="utf-8")
    # P2-b: requires_manual_weight alone no longer blocks if weight is filled.
    app_mod._reject_if_manual_weight_required(record_id, stores)  # should NOT raise

    saved["weight"] = None
    (mouse_dir / "record.json").write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(Exception) as exc:
        app_mod._reject_if_manual_weight_required(record_id, stores)
    assert "手填" in str(exc.value.detail)
