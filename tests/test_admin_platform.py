"""Tests for records metadata, auth sessions, and admin APIs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ui.records_meta import RecordsMetaStore


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MOUSEVISION_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("MOUSEVISION_ADMIN_PASSWORD", "test-admin")
    # Force fresh app import with temp output dir.
    import importlib
    import ui.app as app_mod

    importlib.reload(app_mod)
    with TestClient(app_mod.app) as c:
        yield c, app_mod


def _seed_record(app_mod, tmp_path: Path, record_id: str = "rec-test-001") -> None:
    output = Path(app_mod.DEFAULT_OUTPUT)
    run_dir = output / "run_20250712_test"
    mouse_dir = run_dir / "mouse_001"
    mouse_dir.mkdir(parents=True)
    record = {
        "record_id": record_id,
        "cage_id": "C57-023",
        "ordinal": 1,
        "weight": 22.43,
        "confidence": 0.91,
        "timestamp": "2025-07-12T03:05:58",
        "run_id": "run-test",
    }
    (mouse_dir / "record.json").write_text(json.dumps(record), encoding="utf-8")
    (mouse_dir / "photo.jpg").write_bytes(b"fake-jpeg")


def test_records_meta_store_lifecycle(tmp_path: Path):
    store = RecordsMetaStore(str(tmp_path / "meta.db"))
    store.ensure("r1")
    assert store.effective_status("r1") == "pending"
    store.publish("r1", operator="admin")
    assert store.effective_status("r1") == "published"
    store.soft_delete("r1", operator="admin")
    assert store.effective_status("r1") == "deleted"
    store.restore("r1")
    assert store.effective_status("r1") == "pending"


def test_login_and_records_api(client):
    c, app_mod = client
    login = c.post("/api/login", json={"username": "admin", "password": "test-admin"})
    assert login.status_code == 200
    assert login.json()["user"]["username"] == "admin"

    _seed_record(app_mod, app_mod.DEFAULT_OUTPUT)
    listed = c.get("/api/records")
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] >= 1
    assert body["stats"]["total_records"] >= 1


def test_soft_delete_record(client):
    c, app_mod = client
    c.post("/api/login", json={"username": "admin", "password": "test-admin"})
    _seed_record(app_mod, app_mod.DEFAULT_OUTPUT, "rec-del-1")

    deleted = c.delete("/api/records/rec-del-1")
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "deleted"

    # File still on disk
    mouse_dirs = list((app_mod.DEFAULT_OUTPUT).glob("run_*/mouse_*/record.json"))
    assert mouse_dirs

    restored = c.post("/api/records/rec-del-1/restore")
    assert restored.status_code == 200
    assert restored.json()["meta"]["status"] == "pending"


def test_publish_and_overview(client):
    c, app_mod = client
    c.post("/api/login", json={"username": "admin", "password": "test-admin"})
    _seed_record(app_mod, app_mod.DEFAULT_OUTPUT, "rec-pub-1")

    pub = c.post("/api/records/rec-pub-1/publish")
    assert pub.status_code == 200
    assert pub.json()["meta"]["status"] == "published"

    overview = c.get("/api/overview")
    assert overview.status_code == 200
    assert overview.json()["published_count"] >= 1


def test_export_csv(client):
    c, app_mod = client
    c.post("/api/login", json={"username": "admin", "password": "test-admin"})
    _seed_record(app_mod, app_mod.DEFAULT_OUTPUT, "rec-exp-1")

    res = c.get("/api/export?format=csv")
    assert res.status_code == 200
    assert "record_id" in res.text


def test_entry_redirect(client):
    c, _ = client
    res = c.get("/?to=pc", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "/pc"


def test_legacy_route(client):
    c, _ = client
    res = c.get("/legacy")
    assert res.status_code == 200
    assert "MouseVision Edge" in res.text
