"""Tests for format_suspect Held isolation, release/reject APIs, and reconciliation."""

from __future__ import annotations

import json
import importlib
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


def _make_suspect_run(tmp_path: Path, run_id: str = "run-test-001") -> Path:
    """Create a run dir with one format_suspect Held record."""
    run_dir = tmp_path / "output" / f"run_20260716_{run_id[:8]}"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({
        "run_id": run_id, "cage_id": "C57-023", "status": "completed",
    }), encoding="utf-8")
    mouse_dir = run_dir / "mouse_001"
    mouse_dir.mkdir()
    rec = {
        "record_id": "rec-suspect-1",
        "run_id": run_id,
        "cage_id": "C57-023",
        "ordinal": 1,
        "weight": 16.5,
        "format_suspect": True,
        "format_suspect_reason": "视频格式异常",
    }
    (mouse_dir / "record.json").write_text(json.dumps(rec), encoding="utf-8")
    (mouse_dir / "photo.jpg").write_bytes(b"x")
    return run_dir


def _make_normal_run(tmp_path: Path, run_id: str = "run-normal-002") -> Path:
    """Create a normal (non-suspect) run."""
    run_dir = tmp_path / "output" / f"run_20260716_{run_id[:8]}"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({
        "run_id": run_id, "cage_id": "C57-023", "status": "completed",
        "postflight_passed": True,
    }), encoding="utf-8")
    mouse_dir = run_dir / "mouse_001"
    mouse_dir.mkdir()
    rec = {
        "record_id": "rec-normal-1",
        "run_id": run_id,
        "cage_id": "C57-023",
        "ordinal": 1,
        "weight": 17.2,
        "format_suspect": False,
    }
    (mouse_dir / "record.json").write_text(json.dumps(rec), encoding="utf-8")
    (mouse_dir / "photo.jpg").write_bytes(b"x")
    return run_dir


def _login(c: TestClient) -> None:
    login = c.post("/api/login", json={"username": "admin", "password": "test-admin"})
    assert login.status_code == 200
    changed = c.post(
        "/api/me/password",
        json={"current_password": "test-admin", "new_password": "test-admin-ok"},
    )
    assert changed.status_code == 200


@pytest.fixture()
def app_client(tmp_path: Path, monkeypatch):
    """Reload ui.app against an isolated output root + password."""
    monkeypatch.setenv("MOUSEVISION_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("MOUSEVISION_ADMIN_PASSWORD", "test-admin")
    monkeypatch.delenv("MOUSEVISION_API_TOKEN", raising=False)
    import ui.app as app_mod

    importlib.reload(app_mod)
    with TestClient(app_mod.app) as c:
        yield c, app_mod


class TestSuspectHeldIsolation:
    def test_suspect_record_stays_held(self, tmp_path):
        """Held records with format_suspect are not in list_pending."""
        from mousevision.upload_queue import UploadQueue

        run_dir = _make_suspect_run(tmp_path)
        q = UploadQueue(tmp_path / "queue.db")
        q.enqueue(
            {"record_id": "rec-suspect-1", "cage_id": "C57-023", "weight": 16.5},
            record_path=run_dir / "mouse_001" / "record.json",
        )
        assert len(q.list_pending()) == 0

    def test_release_held_promotes_to_pending(self, tmp_path):
        from mousevision.upload_queue import UploadQueue

        run_dir = _make_suspect_run(tmp_path)
        q = UploadQueue(tmp_path / "queue.db")
        q.enqueue(
            {"record_id": "rec-suspect-1", "cage_id": "C57-023"},
            record_path=run_dir / "mouse_001" / "record.json",
        )
        n = q.release_held(["rec-suspect-1"])
        assert n == 1
        pending = q.list_pending()
        assert len(pending) == 1
        assert pending[0]["status"] == "Pending"


class TestReconciliation:
    def test_reconcile_reholds_suspect_pending(self, tmp_path):
        """A Pending row with format_suspect should be re-held on startup."""
        from mousevision.upload_queue import UploadQueue
        from mousevision.jobs import AnalysisJobManager, JobStore

        run_dir = _make_suspect_run(tmp_path)
        q = UploadQueue(tmp_path / "queue.db")
        q.enqueue(
            {"record_id": "rec-suspect-1", "cage_id": "C57-023"},
            record_path=run_dir / "mouse_001" / "record.json",
            status="Pending",
        )
        assert len(q.list_pending()) == 1

        store = JobStore(tmp_path / "jobs.db")
        manager = AnalysisJobManager(
            store,
            output_root=tmp_path / "output",
            config_path=tmp_path / "config.yaml",
            templates_dir=tmp_path / "templates",
            analysis_fn=lambda _: {},
            upload_queue=q,
        )
        manager._reconcile_held()
        assert len(q.list_pending()) == 0

    def test_reconcile_keeps_passed_pending(self, tmp_path):
        """A Pending row from a postflight_passed run should stay Pending."""
        from mousevision.upload_queue import UploadQueue
        from mousevision.jobs import AnalysisJobManager, JobStore

        run_dir = _make_normal_run(tmp_path)
        q = UploadQueue(tmp_path / "queue.db")
        q.enqueue(
            {"record_id": "rec-normal-1", "cage_id": "C57-023"},
            record_path=run_dir / "mouse_001" / "record.json",
            status="Pending",
        )
        assert len(q.list_pending()) == 1

        store = JobStore(tmp_path / "jobs.db")
        manager = AnalysisJobManager(
            store,
            output_root=tmp_path / "output",
            config_path=tmp_path / "config.yaml",
            templates_dir=tmp_path / "templates",
            analysis_fn=lambda _: {},
            upload_queue=q,
        )
        manager._reconcile_held()
        assert len(q.list_pending()) == 1

    def test_operator_release_survives_restart_reconcile(self, tmp_path):
        """After operator release, postflight_passed must keep Pending across reconcile.

        Minimal repro of the a8f6098 regression:
          release clears format_suspect + promotes Held→Pending but without
          writing postflight_passed, so _reconcile_held re-Holds on restart.
        """
        from mousevision.upload_queue import UploadQueue
        from mousevision.jobs import AnalysisJobManager, JobStore
        from mousevision.run import load_manifest, write_manifest

        run_dir = _make_suspect_run(tmp_path)
        q = UploadQueue(tmp_path / "queue.db")
        q.enqueue(
            {"record_id": "rec-suspect-1", "cage_id": "C57-023"},
            record_path=run_dir / "mouse_001" / "record.json",
        )
        assert len(q.list_pending()) == 0  # Held

        # Simulate the durable release protocol (what the API must do).
        manifest = load_manifest(run_dir) or {}
        manifest["postflight_passed"] = True
        manifest["suspect_resolution"] = "operator_released"
        write_manifest(run_dir, manifest)
        rec_path = run_dir / "mouse_001" / "record.json"
        raw = json.loads(rec_path.read_text(encoding="utf-8"))
        raw["format_suspect"] = False
        raw["format_suspect_reason"] = ""
        rec_path.write_text(json.dumps(raw), encoding="utf-8")
        q.release_held(["rec-suspect-1"])
        assert len(q.list_pending()) == 1
        pending_before_restart = len(q.list_pending())

        # Startup reconciliation (simulates process restart).
        store = JobStore(tmp_path / "jobs.db")
        manager = AnalysisJobManager(
            store,
            output_root=tmp_path / "output",
            config_path=tmp_path / "config.yaml",
            templates_dir=tmp_path / "templates",
            analysis_fn=lambda _: {},
            upload_queue=q,
        )
        manager._reconcile_held()

        assert len(q.list_pending()) == pending_before_restart
        assert q.counts().get("Held", 0) == 0
        assert q.counts().get("Pending", 0) == 1

    def test_reconcile_releases_held_when_postflight_passed(self, tmp_path):
        """Crash window: postflight_passed written, release_held never ran.

        Reconciliation must promote those Held rows to Pending; otherwise
        records permanently stuck Held (only Pending was scanned before).
        """
        from mousevision.upload_queue import UploadQueue
        from mousevision.jobs import AnalysisJobManager, JobStore
        from mousevision.run import load_manifest, write_manifest

        run_dir = _make_normal_run(tmp_path, run_id="run-crash-mid-release")
        # _make_normal_run already sets postflight_passed=True, format_suspect=False
        q = UploadQueue(tmp_path / "queue.db")
        q.enqueue(
            {"record_id": "rec-normal-1", "cage_id": "C57-023"},
            record_path=run_dir / "mouse_001" / "record.json",
        )
        assert q.counts() == {"Held": 1}
        assert len(q.list_pending()) == 0

        manifest = load_manifest(run_dir) or {}
        assert manifest.get("postflight_passed") is True

        store = JobStore(tmp_path / "jobs.db")
        manager = AnalysisJobManager(
            store,
            output_root=tmp_path / "output",
            config_path=tmp_path / "config.yaml",
            templates_dir=tmp_path / "templates",
            analysis_fn=lambda _: {},
            upload_queue=q,
        )
        manager._reconcile_held()

        assert q.counts().get("Held", 0) == 0
        assert q.counts().get("Pending", 0) == 1
        assert len(q.list_pending()) == 1

    def test_reconcile_keeps_held_when_record_json_corrupt(self, tmp_path):
        """Held + postflight_passed but corrupt record.json must NOT go Pending."""
        from mousevision.upload_queue import UploadQueue
        from mousevision.jobs import AnalysisJobManager, JobStore
        from mousevision.run import write_manifest

        run_dir = tmp_path / "output" / "run_corrupt_held"
        run_dir.mkdir(parents=True)
        write_manifest(run_dir, {
            "run_id": "run-corrupt-held",
            "cage_id": "C57-023",
            "postflight_passed": True,
        })
        mouse = run_dir / "mouse_001"
        mouse.mkdir()
        rec_path = mouse / "record.json"
        rec_path.write_text("{not-json", encoding="utf-8")

        q = UploadQueue(tmp_path / "queue.db")
        q.enqueue(
            {"record_id": "rec-corrupt-1", "cage_id": "C57-023"},
            record_path=rec_path,
        )
        assert q.counts() == {"Held": 1}

        manager = AnalysisJobManager(
            JobStore(tmp_path / "jobs.db"),
            output_root=tmp_path / "output",
            config_path=tmp_path / "config.yaml",
            templates_dir=tmp_path / "templates",
            analysis_fn=lambda _: {},
            upload_queue=q,
        )
        manager._reconcile_held()

        assert q.counts().get("Held", 0) == 1
        assert q.counts().get("Pending", 0) == 0

    def test_reconcile_reholds_pending_when_record_json_corrupt(self, tmp_path):
        """Pending + corrupt record.json must be re-held, not left for sync."""
        from mousevision.upload_queue import UploadQueue
        from mousevision.jobs import AnalysisJobManager, JobStore
        from mousevision.run import write_manifest

        run_dir = tmp_path / "output" / "run_corrupt_pending"
        run_dir.mkdir(parents=True)
        write_manifest(run_dir, {
            "run_id": "run-corrupt-pending",
            "cage_id": "C57-023",
            "postflight_passed": True,
        })
        mouse = run_dir / "mouse_001"
        mouse.mkdir()
        rec_path = mouse / "record.json"
        rec_path.write_text("{broken", encoding="utf-8")

        q = UploadQueue(tmp_path / "queue.db")
        q.enqueue(
            {"record_id": "rec-corrupt-p", "cage_id": "C57-023"},
            record_path=rec_path,
            status="Pending",
        )
        assert len(q.list_pending()) == 1

        manager = AnalysisJobManager(
            JobStore(tmp_path / "jobs.db"),
            output_root=tmp_path / "output",
            config_path=tmp_path / "config.yaml",
            templates_dir=tmp_path / "templates",
            analysis_fn=lambda _: {},
            upload_queue=q,
        )
        manager._reconcile_held()

        assert len(q.list_pending()) == 0
        assert q.counts().get("Held", 0) == 1

    def test_reconcile_reholds_pending_when_record_json_missing(self, tmp_path):
        from mousevision.upload_queue import UploadQueue
        from mousevision.jobs import AnalysisJobManager, JobStore
        from mousevision.run import write_manifest

        run_dir = tmp_path / "output" / "run_missing_rec"
        run_dir.mkdir(parents=True)
        write_manifest(run_dir, {
            "run_id": "run-missing",
            "cage_id": "C57-023",
            "postflight_passed": True,
        })
        mouse = run_dir / "mouse_001"
        mouse.mkdir()
        rec_path = mouse / "record.json"  # never written

        q = UploadQueue(tmp_path / "queue.db")
        q.enqueue(
            {"record_id": "rec-missing", "cage_id": "C57-023"},
            record_path=rec_path,
            status="Pending",
        )

        manager = AnalysisJobManager(
            JobStore(tmp_path / "jobs.db"),
            output_root=tmp_path / "output",
            config_path=tmp_path / "config.yaml",
            templates_dir=tmp_path / "templates",
            analysis_fn=lambda _: {},
            upload_queue=q,
        )
        manager._reconcile_held()
        assert len(q.list_pending()) == 0
        assert q.counts().get("Held", 0) == 1


class TestFindRunDirById:
    def test_find_run_dir_by_manifest_id(self, app_client, tmp_path):
        """_find_run_dir_by_id finds run by manifest.run_id, not directory name."""
        c, app_mod = app_client
        run_id = "abc-123-456"
        # Directory name uses timestamp + shortid prefix, not full run_id.
        run_dir = Path(app_mod.DEFAULT_OUTPUT) / "run_20260716_abc-123-"
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text(
            json.dumps({"run_id": run_id, "cage_id": "C57-023"}),
            encoding="utf-8",
        )
        assert "abc-123-456" not in run_dir.name

        found = app_mod._find_run_dir_by_id(run_id)
        assert found is not None
        assert found.resolve() == run_dir.resolve()

        assert app_mod._find_run_dir_by_id("no-such-run") is None


class TestReleaseRejectAPI:
    def test_release_suspect_persists_postflight_and_survives_reconcile(
        self, app_client, tmp_path
    ):
        """POST /release-suspect must write postflight_passed so restart keeps Pending."""
        c, app_mod = app_client
        _login(c)

        run_id = "run-api-rel-001"
        run_dir = Path(app_mod.DEFAULT_OUTPUT) / f"run_20260716_{run_id[:8]}"
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text(
            json.dumps({"run_id": run_id, "cage_id": "C57-023", "status": "completed"}),
            encoding="utf-8",
        )
        mouse_dir = run_dir / "mouse_001"
        mouse_dir.mkdir()
        rec = {
            "record_id": "rec-api-1",
            "run_id": run_id,
            "cage_id": "C57-023",
            "ordinal": 1,
            "format_suspect": True,
            "format_suspect_reason": "truncated",
        }
        rec_path = mouse_dir / "record.json"
        rec_path.write_text(json.dumps(rec), encoding="utf-8")
        (mouse_dir / "photo.jpg").write_bytes(b"x")

        app_mod.upload_queue.enqueue(
            {"record_id": "rec-api-1", "cage_id": "C57-023"},
            record_path=rec_path,
        )
        assert len(app_mod.upload_queue.list_pending()) == 0

        resp = c.post(f"/api/runs/{run_id}/release-suspect")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["released"] == 1

        # Durable allow-sync flag must be present before any restart.
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest.get("postflight_passed") is True
        assert manifest.get("suspect_resolution") == "operator_released"

        raw = json.loads(rec_path.read_text(encoding="utf-8"))
        assert raw.get("format_suspect") is False

        assert len(app_mod.upload_queue.list_pending()) == 1

        # Simulate restart reconciliation.
        from mousevision.jobs import AnalysisJobManager, JobStore

        store = JobStore(tmp_path / "jobs-restart.db")
        manager = AnalysisJobManager(
            store,
            output_root=app_mod.DEFAULT_OUTPUT,
            config_path=tmp_path / "config.yaml",
            templates_dir=tmp_path / "templates",
            analysis_fn=lambda _: {},
            upload_queue=app_mod.upload_queue,
        )
        manager._reconcile_held()
        assert len(app_mod.upload_queue.list_pending()) == 1
        assert app_mod.upload_queue.counts().get("Held", 0) == 0

    def test_reject_suspect_deletes_dir_and_queue(self, app_client, tmp_path):
        """POST /reject-suspect removes queue row, metadata, and verifies rmtree."""
        c, app_mod = app_client
        _login(c)

        run_id = "run-api-rej-001"
        run_dir = Path(app_mod.DEFAULT_OUTPUT) / f"run_20260716_{run_id[:8]}"
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text(
            json.dumps({"run_id": run_id, "cage_id": "C57-023"}),
            encoding="utf-8",
        )
        mouse_dir = run_dir / "mouse_001"
        mouse_dir.mkdir()
        rec_path = mouse_dir / "record.json"
        rec_path.write_text(json.dumps({
            "record_id": "rec-rej-1",
            "run_id": run_id,
            "cage_id": "C57-023",
            "ordinal": 1,
            "format_suspect": True,
        }), encoding="utf-8")
        (mouse_dir / "photo.jpg").write_bytes(b"x")

        app_mod.upload_queue.enqueue(
            {"record_id": "rec-rej-1", "cage_id": "C57-023"},
            record_path=rec_path,
        )

        resp = c.post(f"/api/runs/{run_id}/reject-suspect")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["deleted"] == 1
        assert not mouse_dir.exists()
        assert app_mod.upload_queue.counts().get("Held", 0) == 0
        assert len(app_mod.upload_queue.list_pending()) == 0

    def test_reject_suspect_reports_rmtree_failure(self, app_client, tmp_path):
        """If rmtree fails, API returns 500 AND restores disk + queue consistency."""
        c, app_mod = app_client
        _login(c)

        run_id = "run-api-rej-fail"
        run_dir = Path(app_mod.DEFAULT_OUTPUT) / f"run_20260716_{run_id[:8]}"
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text(
            json.dumps({"run_id": run_id, "cage_id": "C57-023"}),
            encoding="utf-8",
        )
        mouse_dir = run_dir / "mouse_001"
        mouse_dir.mkdir()
        rec_path = mouse_dir / "record.json"
        rec_path.write_text(json.dumps({
            "record_id": "rec-rej-fail",
            "run_id": run_id,
            "cage_id": "C57-023",
            "ordinal": 1,
            "format_suspect": True,
        }), encoding="utf-8")

        app_mod.upload_queue.enqueue(
            {"record_id": "rec-rej-fail", "cage_id": "C57-023"},
            record_path=rec_path,
        )
        assert app_mod.upload_queue.get_payload("rec-rej-fail") is not None
        assert app_mod.upload_queue.counts().get("Held", 0) == 1

        with patch("mousevision.reject_recovery.shutil.rmtree", side_effect=OSError("busy")):
            resp = c.post(f"/api/runs/{run_id}/reject-suspect")

        assert resp.status_code == 500, resp.text
        # Directory restored under original name — no false success.
        assert mouse_dir.exists()
        # Queue row must still exist so retry can complete (no irreversible drop).
        assert app_mod.upload_queue.get_payload("rec-rej-fail") is not None
        assert app_mod.upload_queue.counts().get("Held", 0) == 1
        # No leftover quarantine dirs or journal item blocking retry.
        assert not list(run_dir.glob(".rejecting_*"))

    def test_reject_queue_delete_failure_recoverable_via_journal(self, app_client, tmp_path):
        """If queue delete fails after disk gone, journal + recovery finish the job."""
        c, app_mod = app_client
        _login(c)

        run_id = "run-api-rej-qfail"
        run_dir = Path(app_mod.DEFAULT_OUTPUT) / f"run_20260716_{run_id[:8]}"
        run_dir.mkdir(parents=True)
        from mousevision.run import write_manifest
        write_manifest(run_dir, {"run_id": run_id, "cage_id": "C57-023"})
        mouse_dir = run_dir / "mouse_001"
        mouse_dir.mkdir()
        rec_path = mouse_dir / "record.json"
        rec_path.write_text(json.dumps({
            "record_id": "rec-qfail",
            "run_id": run_id,
            "cage_id": "C57-023",
            "ordinal": 1,
            "format_suspect": True,
        }), encoding="utf-8")
        app_mod.upload_queue.enqueue(
            {"record_id": "rec-qfail", "cage_id": "C57-023"},
            record_path=rec_path,
        )

        real_delete = app_mod.upload_queue.delete_by_record_id
        calls = {"n": 0}

        def flaky_delete(rid: str) -> int:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("db locked")
            return real_delete(rid)

        with patch.object(app_mod.upload_queue, "delete_by_record_id", side_effect=flaky_delete):
            resp = c.post(f"/api/runs/{run_id}/reject-suspect")
        assert resp.status_code == 500, resp.text
        assert not mouse_dir.exists()  # disk already committed
        # Journal must record disk_gone so recovery can finish.
        from mousevision.reject_recovery import load_reject_journal, recover_reject_state
        journal = load_reject_journal(run_dir)
        assert journal is not None
        phases = [i.get("phase") for i in journal.get("items", [])]
        assert "disk_gone" in phases or "queue_gone" in phases or "done" in phases

        # Startup recovery completes queue delete.
        recover_reject_state(
            app_mod.DEFAULT_OUTPUT,
            upload_queue=app_mod.upload_queue,
            mark_meta_deleted=lambda rid: app_mod.records_meta.update(
                rid, status="deleted", operator="system"
            ),
        )
        assert app_mod.upload_queue.get_payload("rec-qfail") is None
        assert load_reject_journal(run_dir) is None

    def test_reject_orphan_quarantine_recovered_on_startup(self, tmp_path):
        """Orphan .rejecting_* is journalized then finished (not rmtree-blind)."""
        from mousevision.upload_queue import UploadQueue
        from mousevision.reject_recovery import load_reject_journal, recover_reject_state
        from mousevision.run import write_manifest

        output = tmp_path / "output"
        run_dir = output / "run_orphan_rej"
        run_dir.mkdir(parents=True)
        write_manifest(run_dir, {"run_id": "run-orphan", "cage_id": "C57-023"})
        qdir = run_dir / ".rejecting_mouse_001_deadbeef"
        qdir.mkdir()
        (qdir / "record.json").write_text(json.dumps({
            "record_id": "rec-orphan",
            "format_suspect": True,
        }), encoding="utf-8")

        q = UploadQueue(tmp_path / "queue.db")
        q.enqueue(
            {"record_id": "rec-orphan", "cage_id": "C57-023"},
            record_path=run_dir / "mouse_001" / "record.json",
        )
        assert q.get_payload("rec-orphan") is not None

        meta_deleted: list[str] = []
        stats = recover_reject_state(
            output,
            upload_queue=q,
            mark_meta_deleted=lambda rid: meta_deleted.append(rid),
        )
        assert stats["orphans_removed"] == 1
        assert not qdir.exists()
        assert q.get_payload("rec-orphan") is None
        assert "rec-orphan" in meta_deleted
        assert load_reject_journal(run_dir) is None

    def test_reject_orphan_without_record_id_restores_mouse_dir(self, tmp_path):
        """Unidentifiable orphan quarantine is renamed back, not hard-deleted."""
        from mousevision.reject_recovery import recover_reject_state
        from mousevision.run import write_manifest

        output = tmp_path / "output"
        run_dir = output / "run_orphan_restore"
        run_dir.mkdir(parents=True)
        write_manifest(run_dir, {"run_id": "run-orphan-r", "cage_id": "C57-023"})
        qdir = run_dir / ".rejecting_mouse_002_aabbccdd"
        qdir.mkdir()
        (qdir / "photo.jpg").write_bytes(b"x")  # no record.json

        stats = recover_reject_state(output, upload_queue=None)
        assert stats["orphans_restored"] == 1
        assert not qdir.exists()
        restored = run_dir / "mouse_002"
        assert restored.is_dir()
        assert (restored / "photo.jpg").exists()

    def test_queue_gone_without_meta_callback_stays_in_journal(self, tmp_path):
        """Manager start without mark_meta_deleted must not clear queue_gone items."""
        from mousevision.upload_queue import UploadQueue
        from mousevision.reject_recovery import (
            PHASE_QUEUE_GONE,
            load_reject_journal,
            recover_reject_state,
            save_reject_journal,
        )
        from mousevision.run import write_manifest

        output = tmp_path / "output"
        run_dir = output / "run_qg"
        run_dir.mkdir(parents=True)
        write_manifest(run_dir, {"run_id": "run-qg", "cage_id": "C57-023"})
        save_reject_journal(run_dir, {
            "run_id": "run-qg",
            "actor": "admin",
            "items": [{
                "original_name": "mouse_001",
                "quarantine_name": ".rejecting_mouse_001_aaaaaaaa",
                "record_id": "rec-qg",
                "phase": PHASE_QUEUE_GONE,
            }],
        })
        q = UploadQueue(tmp_path / "queue.db")
        # Simulate queue already deleted.
        assert q.get_payload("rec-qg") is None

        # First pass: like AnalysisJobManager.start (no meta callback).
        recover_reject_state(output, upload_queue=q, mark_meta_deleted=None)
        journal = load_reject_journal(run_dir)
        assert journal is not None
        assert journal["items"][0]["phase"] == PHASE_QUEUE_GONE

        # Second pass with callback finishes.
        meta: list[str] = []
        recover_reject_state(
            output,
            upload_queue=q,
            mark_meta_deleted=lambda rid: meta.append(rid),
        )
        assert meta == ["rec-qg"]
        assert load_reject_journal(run_dir) is None

    def test_quarantined_rollback_does_not_delete_queue(self, tmp_path):
        """quarantined + original exists + quarantine missing = rolled-back rmtree."""
        from mousevision.upload_queue import UploadQueue
        from mousevision.reject_recovery import (
            PHASE_QUARANTINED,
            load_reject_journal,
            recover_reject_state,
            save_reject_journal,
        )
        from mousevision.run import write_manifest

        output = tmp_path / "output"
        run_dir = output / "run_rb"
        run_dir.mkdir(parents=True)
        write_manifest(run_dir, {"run_id": "run-rb", "cage_id": "C57-023"})
        mouse = run_dir / "mouse_001"
        mouse.mkdir()
        rec_path = mouse / "record.json"
        rec_path.write_text(json.dumps({
            "record_id": "rec-rb",
            "format_suspect": True,
        }), encoding="utf-8")

        # Journal still says quarantined, but rmtree failed and renamed back.
        save_reject_journal(run_dir, {
            "run_id": "run-rb",
            "actor": "admin",
            "items": [{
                "original_name": "mouse_001",
                "quarantine_name": ".rejecting_mouse_001_bbbbbbbb",
                "record_id": "rec-rb",
                "phase": PHASE_QUARANTINED,
            }],
        })
        q = UploadQueue(tmp_path / "queue.db")
        q.enqueue(
            {"record_id": "rec-rb", "cage_id": "C57-023"},
            record_path=rec_path,
        )
        assert mouse.exists()
        assert q.get_payload("rec-rb") is not None

        # rollback-before
        assert mouse.exists() and q.get_payload("rec-rb") is not None

        meta: list[str] = []
        recover_reject_state(
            output,
            upload_queue=q,
            mark_meta_deleted=lambda rid: meta.append(rid),
        )

        # rollback-after: original + queue kept; journal cleared; meta untouched.
        assert mouse.exists()
        assert q.get_payload("rec-rb") is not None
        assert load_reject_journal(run_dir) is None
        assert meta == []

    def test_reject_non_suspect_returns_400(self, app_client):
        c, app_mod = app_client
        _login(c)

        run_id = "run-normal-ok"
        run_dir = Path(app_mod.DEFAULT_OUTPUT) / f"run_20260716_{run_id[:8]}"
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text(
            json.dumps({"run_id": run_id, "postflight_passed": True}),
            encoding="utf-8",
        )
        mouse_dir = run_dir / "mouse_001"
        mouse_dir.mkdir()
        (mouse_dir / "record.json").write_text(json.dumps({
            "record_id": "rec-n",
            "format_suspect": False,
        }), encoding="utf-8")

        resp = c.post(f"/api/runs/{run_id}/reject-suspect")
        assert resp.status_code == 400


class TestRejectFailClosedAndLocks:
    def test_reject_corrupt_record_json_keeps_dir_and_queue(self, tmp_path):
        """No reliable record_id → no rename/rmtree; queue row stays Held."""
        from mousevision.reject_recovery import (
            load_reject_journal,
            new_reject_journal,
            reject_mouse_dir,
            resolve_record_id,
        )
        from mousevision.run import write_manifest
        from mousevision.upload_queue import UploadQueue

        run_dir = tmp_path / "output" / "run_corrupt_rej"
        run_dir.mkdir(parents=True)
        write_manifest(run_dir, {"run_id": "run-cr", "cage_id": "C57-023"})
        mouse = run_dir / "mouse_001"
        mouse.mkdir()
        rec_path = mouse / "record.json"
        rec_path.write_text("{not-json", encoding="utf-8")

        q = UploadQueue(tmp_path / "queue.db")
        # Queue points at the path but we cannot trust JSON; path lookup still works.
        q.enqueue(
            {"record_id": "rec-via-path", "cage_id": "C57-023"},
            record_path=rec_path,
        )
        assert resolve_record_id(mouse, upload_queue=q) == "rec-via-path"

        # Path lookup succeeds → reject may proceed. Use a separate corrupt case
        # with NO queue mapping for fail-closed.
        mouse2 = run_dir / "mouse_002"
        mouse2.mkdir()
        (mouse2 / "record.json").write_text("{broken", encoding="utf-8")
        assert resolve_record_id(mouse2, upload_queue=q) == ""

        journal = new_reject_journal(run_id="run-cr", actor="admin")
        with pytest.raises(RuntimeError, match="record_id"):
            reject_mouse_dir(
                run_dir,
                mouse2,
                journal=journal,
                upload_queue=q,
                mark_meta_deleted=lambda rid, operator="s": None,
            )
        assert mouse2.exists()
        assert (mouse2 / "record.json").exists()
        # No destructive journal item left for the failed dir.
        remaining = [
            i for i in journal.get("items", [])
            if i.get("original_name") == "mouse_002" and i.get("phase") != "done"
        ]
        # Item should not have been appended, or if planned-only never renamed —
        # reject_mouse_dir raises before append on empty rid.
        assert not any(i.get("original_name") == "mouse_002" for i in journal.get("items", []))
        assert load_reject_journal(run_dir) is None or not any(
            i.get("original_name") == "mouse_002"
            for i in (load_reject_journal(run_dir) or {}).get("items", [])
        )

    def test_reject_api_corrupt_only_run_keeps_evidence(self, app_client):
        """API reject of corrupt record.json without path identity must 500 and keep dir."""
        c, app_mod = app_client
        _login(c)

        run_id = "run-api-corrupt"
        run_dir = Path(app_mod.DEFAULT_OUTPUT) / f"run_20260716_{run_id[:8]}"
        run_dir.mkdir(parents=True)
        from mousevision.run import write_manifest
        write_manifest(run_dir, {"run_id": run_id, "cage_id": "C57-023"})
        # Readable format_suspect sibling so endpoint is allowed.
        good = run_dir / "mouse_001"
        good.mkdir()
        good_rec = good / "record.json"
        good_rec.write_text(json.dumps({
            "record_id": "rec-good",
            "format_suspect": True,
        }), encoding="utf-8")
        app_mod.upload_queue.enqueue(
            {"record_id": "rec-good", "cage_id": "C57-023"},
            record_path=good_rec,
        )
        # Corrupt sibling with its own queue row but unreadable JSON — path lookup
        # still resolves, so delete is OK. Use corrupt WITHOUT queue for fail-closed.
        bad = run_dir / "mouse_002"
        bad.mkdir()
        (bad / "record.json").write_text("{nope", encoding="utf-8")
        # No queue enqueue for mouse_002

        resp = c.post(f"/api/runs/{run_id}/reject-suspect")
        # good deleted; bad fails → overall 500 with partial deleted
        assert resp.status_code == 500, resp.text
        assert bad.exists()
        assert not good.exists()

    def test_recovery_preserves_journal_actor(self, tmp_path):
        from mousevision.reject_recovery import (
            PHASE_QUEUE_GONE,
            recover_reject_state,
            save_reject_journal,
        )
        from mousevision.run import write_manifest

        output = tmp_path / "output"
        run_dir = output / "run_actor"
        run_dir.mkdir(parents=True)
        write_manifest(run_dir, {"run_id": "run-actor"})
        save_reject_journal(run_dir, {
            "run_id": "run-actor",
            "actor": "operator_alice",
            "items": [{
                "original_name": "mouse_001",
                "quarantine_name": ".rejecting_mouse_001_cccccccc",
                "record_id": "rec-act",
                "actor": "operator_alice",
                "phase": PHASE_QUEUE_GONE,
            }],
        })
        seen: list[tuple[str, str]] = []

        def mark(rid: str, operator: str = "system") -> None:
            seen.append((rid, operator))

        # queue_gone only needs meta; upload_queue may be None.
        recover_reject_state(output, upload_queue=None, mark_meta_deleted=mark)
        assert seen == [("rec-act", "operator_alice")]

    def test_concurrent_reject_serialized_by_run_lock(self, app_client):
        """Two concurrent reject requests on the same run must not corrupt state."""
        import threading
        c, app_mod = app_client
        _login(c)

        run_id = "run-conc-rej"
        run_dir = Path(app_mod.DEFAULT_OUTPUT) / f"run_20260716_{run_id[:8]}"
        run_dir.mkdir(parents=True)
        from mousevision.run import write_manifest
        write_manifest(run_dir, {"run_id": run_id, "cage_id": "C57-023"})
        mouse = run_dir / "mouse_001"
        mouse.mkdir()
        rec = mouse / "record.json"
        rec.write_text(json.dumps({
            "record_id": "rec-conc",
            "format_suspect": True,
        }), encoding="utf-8")
        app_mod.upload_queue.enqueue(
            {"record_id": "rec-conc", "cage_id": "C57-023"},
            record_path=rec,
        )

        results: list[int] = []

        def worker():
            from fastapi.testclient import TestClient
            with TestClient(app_mod.app) as tc:
                assert tc.post(
                    "/api/login",
                    json={"username": "admin", "password": "test-admin-ok"},
                ).status_code == 200
                r = tc.post(f"/api/runs/{run_id}/reject-suspect")
                results.append(r.status_code)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert sorted(results) in ([200, 400], [200, 200], [200, 409], [400, 200])
        # Exactly one successful delete of the mouse dir / queue gone.
        assert not mouse.exists()
        assert app_mod.upload_queue.get_payload("rec-conc") is None
        from mousevision.reject_recovery import load_reject_journal
        assert load_reject_journal(run_dir) is None

    def test_run_dir_lock_blocks_second_holder(self, tmp_path):
        from mousevision.run_lock import RunLockTimeout, run_dir_lock
        import threading

        run_dir = tmp_path / "run_lock"
        run_dir.mkdir()
        entered = threading.Event()
        release = threading.Event()
        errors: list[str] = []

        def holder():
            with run_dir_lock(run_dir, timeout_sec=5):
                entered.set()
                release.wait(timeout=5)

        t = threading.Thread(target=holder)
        t.start()
        assert entered.wait(timeout=2)
        try:
            with run_dir_lock(run_dir, timeout_sec=0.2):
                errors.append("should-not-acquire")
        except RunLockTimeout:
            pass
        else:
            errors.append("missing-timeout")
        release.set()
        t.join()
        assert errors == []


class TestAtomicManifest:
    def test_write_manifest_atomic_replace_keeps_old_on_write_failure(self, tmp_path):
        """If temp write fails mid-way, existing manifest.json stays intact."""
        from mousevision.run import load_manifest, write_manifest, atomic_write_text
        import mousevision.run as run_mod

        run_dir = tmp_path / "run_atomic"
        run_dir.mkdir()
        write_manifest(run_dir, {"run_id": "r1", "postflight_passed": False, "v": 1})
        assert load_manifest(run_dir)["v"] == 1

        # Simulate failure after temp is created but before replace: inject
        # os.replace that raises; old file must remain valid.
        real_replace = run_mod.os.replace

        def boom_replace(src, dst):
            raise OSError("replace failed")

        with patch.object(run_mod.os, "replace", side_effect=boom_replace):
            with pytest.raises(OSError):
                write_manifest(run_dir, {"run_id": "r1", "postflight_passed": True, "v": 2})

        # Old complete manifest still readable.
        m = load_manifest(run_dir)
        assert m is not None
        assert m.get("v") == 1
        assert m.get("postflight_passed") is False

        # Successful write updates atomically.
        write_manifest(run_dir, {"run_id": "r1", "postflight_passed": True, "v": 2})
        m2 = load_manifest(run_dir)
        assert m2["v"] == 2
        assert m2["postflight_passed"] is True
        # No leftover temp files.
        assert not list(run_dir.glob(".manifest.json.*.tmp"))

    def test_atomic_write_text_roundtrip(self, tmp_path):
        from mousevision.run import atomic_write_text

        path = tmp_path / "x.json"
        atomic_write_text(path, '{"a": 1}')
        assert path.read_text(encoding="utf-8") == '{"a": 1}'


class TestOrdinalReleaseOnGenericException:
    def test_analysis_raise_without_records_releases_ordinal(self, tmp_path):
        """Early analysis_fn raise + empty disk → release (gap preferred over leak)."""
        from mousevision.jobs import AnalysisJobManager, JobStore

        store = JobStore(tmp_path / "jobs.db")
        video = tmp_path / "source.mp4"
        video.write_bytes(b"video-placeholder")
        job = store.create_job(
            project_id="p",
            cage_id="C57-023",
            original_filename="source.mp4",
            content_type="video/mp4",
            requested_ordinal=7,
        )
        store.update(job["job_id"], video_path=str(video), size_bytes=1, stage="uploaded")

        released: list[tuple[str, int]] = []

        def broken_analysis(_: dict) -> dict:
            raise RuntimeError("boom")

        manager = AnalysisJobManager(
            store,
            output_root=tmp_path / "output",
            config_path=tmp_path / "config.yaml",
            templates_dir=tmp_path / "templates",
            analysis_fn=broken_analysis,
            release_ordinals=lambda c, o: released.append((c, o)),
        )
        manager.start()
        try:
            manager.submit(job["job_id"])
            from tests.test_jobs import _wait_for_terminal
            _wait_for_terminal(store, job["job_id"])
        finally:
            manager.stop()

        assert ("C57-023", 7) in released

    def test_analysis_raise_after_disk_records_does_not_release(self, tmp_path):
        """If records already occupy the ordinal on disk, do not release it."""
        from mousevision.jobs import AnalysisJobManager, JobStore

        store = JobStore(tmp_path / "jobs.db")
        video = tmp_path / "source.mp4"
        video.write_bytes(b"video-placeholder")
        job = store.create_job(
            project_id="p",
            cage_id="C57-023",
            original_filename="source.mp4",
            content_type="video/mp4",
            requested_ordinal=3,
        )
        store.update(job["job_id"], video_path=str(video), size_bytes=1, stage="uploaded")

        # Pre-seed a persisted record for the reserved ordinal (simulates
        # exception after run_video wrote records but before a clean return).
        run_dir = tmp_path / "output" / "run_partial"
        mouse = run_dir / "mouse_003"
        mouse.mkdir(parents=True)
        (mouse / "record.json").write_text(json.dumps({
            "record_id": "rec-partial",
            "cage_id": "C57-023",
            "ordinal": 3,
        }), encoding="utf-8")

        released: list[tuple[str, int]] = []

        def boom(_: dict) -> dict:
            raise RuntimeError("crash after persist")

        manager = AnalysisJobManager(
            store,
            output_root=tmp_path / "output",
            config_path=tmp_path / "config.yaml",
            templates_dir=tmp_path / "templates",
            analysis_fn=boom,
            release_ordinals=lambda c, o: released.append((c, o)),
        )
        manager.start()
        try:
            manager.submit(job["job_id"])
            from tests.test_jobs import _wait_for_terminal
            failed = _wait_for_terminal(store, job["job_id"])
        finally:
            manager.stop()

        assert failed["status"] == "failed"
        assert ("C57-023", 3) not in released

    def test_dict_result_with_record_count_does_not_use_dot_records(self, tmp_path):
        """analysis_fn returns dict; AttributeError on .records must not force release."""
        from mousevision.jobs import AnalysisJobManager, JobStore

        store = JobStore(tmp_path / "jobs.db")
        video = tmp_path / "source.mp4"
        video.write_bytes(b"video-placeholder")
        job = store.create_job(
            project_id="p",
            cage_id="C57-023",
            original_filename="source.mp4",
            content_type="video/mp4",
            requested_ordinal=5,
        )
        store.update(job["job_id"], video_path=str(video), size_bytes=1, stage="uploaded")

        # Persist records on disk matching the ordinal, then return a dict and
        # fail inside the worker after analysis_fn (via a poisoned store.update
        # is hard). Instead: analysis_fn writes then raises — disk check covers it.
        # Here we unit-test the helper directly for dict vs AttributeError path.
        manager = AnalysisJobManager(
            store,
            output_root=tmp_path / "output",
            config_path=tmp_path / "config.yaml",
            templates_dir=tmp_path / "templates",
            analysis_fn=lambda _: {"run_id": "r", "record_count": 2},
        )
        # Dict with positive record_count → persisted, even with empty disk.
        assert manager._records_persisted_for_job(
            {"run_id": "r", "record_count": 2}, "C57-023", 5
        ) is True
        # Dict with zero count + empty disk → not persisted.
        assert manager._records_persisted_for_job(
            {"run_id": "r", "record_count": 0}, "C57-023", 5
        ) is False
        # No result (raised) + empty disk → not persisted.
        assert manager._records_persisted_for_job(None, "C57-023", 5) is False

    def test_partial_mouse_dir_without_record_json_is_occupancy(self, tmp_path):
        """Recorder crash before record.json must not free the ordinal."""
        from mousevision.jobs import AnalysisJobManager, JobStore

        store = JobStore(tmp_path / "jobs.db")
        output = tmp_path / "output"
        run_dir = output / "run_partial_slot"
        mouse = run_dir / "mouse_003"
        mouse.mkdir(parents=True)
        # Partial write: photo/curve exist, record.json missing (Recorder order).
        (mouse / "photo.jpg").write_bytes(b"x")
        (run_dir / "manifest.json").write_text(
            json.dumps({"run_id": "r-partial", "cage_id": "C57-023"}),
            encoding="utf-8",
        )

        manager = AnalysisJobManager(
            store,
            output_root=output,
            config_path=tmp_path / "config.yaml",
            templates_dir=tmp_path / "templates",
            analysis_fn=lambda _: {},
        )
        assert manager._ordinal_occupied_on_disk("C57-023", 3) is True
        assert manager._records_persisted_for_job(None, "C57-023", 3) is True

        released: list[tuple[str, int]] = []
        video = tmp_path / "source.mp4"
        video.write_bytes(b"video-placeholder")
        job = store.create_job(
            project_id="p",
            cage_id="C57-023",
            original_filename="source.mp4",
            content_type="video/mp4",
            requested_ordinal=3,
        )
        store.update(job["job_id"], video_path=str(video), size_bytes=1, stage="uploaded")

        def boom(_: dict) -> dict:
            raise RuntimeError("crash after partial save")

        mgr = AnalysisJobManager(
            store,
            output_root=output,
            config_path=tmp_path / "config.yaml",
            templates_dir=tmp_path / "templates",
            analysis_fn=boom,
            release_ordinals=lambda c, o: released.append((c, o)),
        )
        mgr.start()
        try:
            mgr.submit(job["job_id"])
            from tests.test_jobs import _wait_for_terminal
            _wait_for_terminal(store, job["job_id"])
        finally:
            mgr.stop()
        assert ("C57-023", 3) not in released

    def test_corrupt_record_json_is_occupancy(self, tmp_path):
        """Unreadable record.json under matching mouse_NNN is fail-closed occupancy."""
        from mousevision.jobs import AnalysisJobManager, JobStore

        store = JobStore(tmp_path / "jobs.db")
        output = tmp_path / "output"
        run_dir = output / "run_corrupt"
        mouse = run_dir / "mouse_004"
        mouse.mkdir(parents=True)
        (mouse / "record.json").write_text("{not-json", encoding="utf-8")
        (run_dir / "manifest.json").write_text(
            json.dumps({"run_id": "r-corrupt", "cage_id": "C57-023"}),
            encoding="utf-8",
        )
        manager = AnalysisJobManager(
            store,
            output_root=output,
            config_path=tmp_path / "config.yaml",
            templates_dir=tmp_path / "templates",
            analysis_fn=lambda _: {},
        )
        assert manager._ordinal_occupied_on_disk("C57-023", 4) is True
        assert manager._ordinal_occupied_on_disk("OTHER", 4) is False
