"""API tests for /api/jobs/{id}/analysis-preview."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _tstores(app_mod):
    """B3：业务 store 不再有模块级单例 —— 共享令牌通道等价于 legacy-default
    租户的 store 集（从 tenant_factory 按租户解析）。"""
    from ui.control_store import LEGACY_TENANT_ID

    return app_mod.tenant_factory.stores(LEGACY_TENANT_ID)


@pytest.fixture()
def preview_client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MOUSEVISION_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("MOUSEVISION_API_TOKEN", "preview-secret")
    import importlib
    import ui.app as app_mod

    importlib.reload(app_mod)
    with TestClient(app_mod.app) as c:
        yield c, app_mod


def _headers(token: str = "preview-secret") -> dict[str, str]:
    return {"X-MouseVision-Token": token}


def test_analysis_preview_rejects_missing_token(preview_client):
    c, _ = preview_client
    res = c.get("/api/jobs/does-not-exist/analysis-preview")
    assert res.status_code == 401


def test_analysis_preview_rejects_wrong_token(preview_client):
    c, _ = preview_client
    res = c.get(
        "/api/jobs/does-not-exist/analysis-preview",
        headers=_headers("wrong"),
    )
    assert res.status_code == 401


def test_analysis_preview_404_when_job_missing(preview_client):
    c, _ = preview_client
    res = c.get(
        "/api/jobs/00000000-0000-0000-0000-000000000000/analysis-preview",
        headers=_headers(),
    )
    assert res.status_code == 404
    assert "任务不存在" in res.json()["detail"]


def test_analysis_preview_404_when_run_not_ready(preview_client):
    c, app_mod = preview_client
    job = _tstores(app_mod).job_store.create_job(
        project_id="default",
        cage_id="C1",
        original_filename="a.mp4",
        content_type="video/mp4",
    )
    # Job exists but has no run_id yet (still uploading / queued).
    res = c.get(
        f"/api/jobs/{job['job_id']}/analysis-preview",
        headers=_headers(),
    )
    assert res.status_code == 404
    assert "尚不可用" in res.json()["detail"]


def test_analysis_preview_returns_jpeg(preview_client, tmp_path: Path):
    c, app_mod = preview_client
    job = _tstores(app_mod).job_store.create_job(
        project_id="default",
        cage_id="C1",
        original_filename="a.mp4",
        content_type="video/mp4",
    )
    run_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    # B3：legacy 令牌读的是 legacy-default 租户根；run 目录也须落在租户根内。
    run_dir = _tstores(app_mod).output_root / f"run_20260714_{run_id[:8]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(
        f'{{"run_id":"{run_id}","cage_id":"C1","status":"completed"}}',
        encoding="utf-8",
    )
    # Minimal JPEG (1x1 pixel).
    jpeg = bytes(
        [
            0xFF,
            0xD8,
            0xFF,
            0xE0,
            0x00,
            0x10,
            0x4A,
            0x46,
            0x49,
            0x46,
            0x00,
            0x01,
            0x01,
            0x00,
            0x00,
            0x01,
            0x00,
            0x01,
            0x00,
            0x00,
            0xFF,
            0xDB,
            0x00,
            0x43,
            0x00,
            *([0x08] * 64),
            0xFF,
            0xC0,
            0x00,
            0x0B,
            0x08,
            0x00,
            0x01,
            0x00,
            0x01,
            0x01,
            0x01,
            0x11,
            0x00,
            0xFF,
            0xC4,
            0x00,
            0x14,
            0x00,
            0x01,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x03,
            0xFF,
            0xC4,
            0x00,
            0x14,
            0x10,
            0x01,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0xFF,
            0xDA,
            0x00,
            0x08,
            0x01,
            0x01,
            0x00,
            0x00,
            0x3F,
            0x00,
            0x7F,
            0xFF,
            0xD9,
        ]
    )
    (run_dir / "analysis_preview.jpg").write_bytes(jpeg)
    _tstores(app_mod).job_store.update(job["job_id"], run_id=run_id, status="completed")

    res = c.get(
        f"/api/jobs/{job['job_id']}/analysis-preview",
        headers=_headers(),
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/jpeg")
    assert res.content[:2] == b"\xff\xd8"
    assert len(res.content) == len(jpeg)

    # Payload advertises the preview URL once run_id is set.
    # B4：jobs 读接口不再匿名（§6.1）——带共享令牌（映射 legacy-default）。
    status = c.get(f"/api/jobs/{job['job_id']}", headers=_headers())
    assert status.status_code == 200
    assert status.json()["analysis_preview_url"] == (
        f"/api/jobs/{job['job_id']}/analysis-preview"
    )
