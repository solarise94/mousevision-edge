"""Tests for records metadata, auth sessions, and admin APIs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ui.audit import AuditStore, scrub_sensitive
from ui.records_meta import RecordsMetaStore


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MOUSEVISION_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("MOUSEVISION_ADMIN_PASSWORD", "test-admin")
    monkeypatch.delenv("MOUSEVISION_API_TOKEN", raising=False)
    import importlib
    import ui.app as app_mod

    importlib.reload(app_mod)
    with TestClient(app_mod.app) as c:
        yield c, app_mod


def _login(c) -> None:
    login = c.post("/api/login", json={"username": "admin", "password": "test-admin"})
    assert login.status_code == 200
    # Seeded admin always must change password before active APIs work.
    changed = c.post(
        "/api/me/password",
        json={"current_password": "test-admin", "new_password": "test-admin-ok"},
    )
    assert changed.status_code == 200
    assert changed.json()["user"]["must_change_password"] is False


def _seed_record(app_mod, record_id: str = "rec-test-001") -> None:
    output = Path(app_mod.DEFAULT_OUTPUT)
    run_dir = output / "run_20250712_test"
    mouse_dir = run_dir / "mouse_001"
    mouse_dir.mkdir(parents=True, exist_ok=True)
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


def test_scrub_sensitive_redacts_password():
    assert scrub_sensitive({"password": "secret", "role": "admin"}) == {
        "password": "***",
        "role": "admin",
    }


def test_audit_never_stores_plaintext_password(tmp_path: Path):
    store = AuditStore(str(tmp_path / "audit.db"))
    entry = store.log(
        actor="admin",
        action="user.update",
        detail={"password": "super-secret", "role": "operator"},
    )
    assert entry["detail"]["password"] == "***"
    listed = store.list(limit=1)
    assert listed["items"][0]["detail"]["password"] == "***"


def test_login_and_records_api(client):
    c, app_mod = client
    _login(c)
    _seed_record(app_mod)
    listed = c.get("/api/records")
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] >= 1
    assert body["stats"]["total_records"] >= 1


def test_records_api_requires_auth(client):
    c, _ = client
    assert c.get("/api/records").status_code == 401
    assert c.get("/api/overview").status_code == 401
    assert c.get("/api/mice-admin").status_code == 401


def test_shared_token_does_not_become_admin(client, monkeypatch):
    c, app_mod = client
    monkeypatch.setenv("MOUSEVISION_API_TOKEN", "edge-secret")
    import importlib

    importlib.reload(app_mod)
    with TestClient(app_mod.app) as fresh:
        # Token must not satisfy /api/me as an admin session.
        me = fresh.get("/api/me", headers={"X-MouseVision-Token": "edge-secret"})
        assert me.status_code == 200
        assert me.json()["authenticated"] is False
        # Token must not unlock PC write APIs.
        denied = fresh.delete(
            "/api/records/whatever",
            headers={"X-MouseVision-Token": "edge-secret"},
        )
        assert denied.status_code == 401


def test_pc_html_does_not_inject_api_token(client, monkeypatch):
    c, app_mod = client
    monkeypatch.setenv("MOUSEVISION_API_TOKEN", "edge-secret")
    import importlib

    importlib.reload(app_mod)
    with TestClient(app_mod.app) as fresh:
        pc = fresh.get("/pc")
        assert pc.status_code == 200
        assert "mousevision-api-token" not in pc.text
        entry = fresh.get("/")
        assert "mousevision-api-token" not in entry.text
        mobile = fresh.get("/mobile")
        assert 'name="mousevision-api-token" content="edge-secret"' in mobile.text


def test_must_change_password_blocks_admin_apis(client):
    c, _ = client
    login = c.post("/api/login", json={"username": "admin", "password": "test-admin"})
    assert login.status_code == 200
    assert login.json()["user"]["must_change_password"] is True
    blocked = c.get("/api/records")
    assert blocked.status_code == 403


def test_soft_delete_record(client):
    c, app_mod = client
    _login(c)
    _seed_record(app_mod, "rec-del-1")

    deleted = c.delete("/api/records/rec-del-1")
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "deleted"

    # Default detail/photo hidden
    assert c.get("/api/records/rec-del-1").status_code == 404
    assert c.get("/api/records/rec-del-1/photo").status_code == 404
    # Admin can opt-in
    assert c.get("/api/records/rec-del-1?include_deleted=true").status_code == 200

    # Mobile cage list hides soft-deleted records
    box_list = c.get("/api/boxes/C57-023/records")
    assert box_list.status_code == 200
    ids = [item.get("record_id") for item in box_list.json()["items"]]
    assert "rec-del-1" not in ids

    # File still on disk
    mouse_dirs = list(Path(app_mod.DEFAULT_OUTPUT).glob("run_*/mouse_*/record.json"))
    assert mouse_dirs

    restored = c.post("/api/records/rec-del-1/restore")
    assert restored.status_code == 200
    assert restored.json()["meta"]["status"] == "pending"
    assert c.get("/api/records/rec-del-1").status_code == 200


def test_publish_and_overview(client):
    c, app_mod = client
    _login(c)
    _seed_record(app_mod, "rec-pub-1")

    pub = c.post("/api/records/rec-pub-1/publish")
    assert pub.status_code == 200
    assert pub.json()["meta"]["status"] == "published"

    overview = c.get("/api/overview")
    assert overview.status_code == 200
    assert overview.json()["published_count"] >= 1


def test_export_csv(client):
    c, app_mod = client
    _login(c)
    _seed_record(app_mod, "rec-exp-1")

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
