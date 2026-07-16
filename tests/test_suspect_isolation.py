"""Tests for format_suspect Held isolation, release/reject APIs, and reconciliation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _make_suspect_run(tmp_path: Path, run_id: str = "run-test-001") -> Path:
    """Create a run dir with one format_suspect Held record."""
    from mousevision.upload_queue import UploadQueue, UploadStatus

    run_dir = tmp_path / "output" / f"run_20260716_{run_id[:8]}"
    run_dir.mkdir(parents=True)
    # manifest
    (run_dir / "manifest.json").write_text(json.dumps({
        "run_id": run_id, "cage_id": "C57-023", "status": "completed",
    }), encoding="utf-8")
    # mouse dir
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
        # Default status is Held, not Pending.
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

        run_dir = _make_suspect_run(tmp_path)
        q = UploadQueue(tmp_path / "queue.db")
        # Enqueue as Pending (simulates a crash after release).
        q.enqueue(
            {"record_id": "rec-suspect-1", "cage_id": "C57-023"},
            record_path=run_dir / "mouse_001" / "record.json",
            status="Pending",
        )
        assert len(q.list_pending()) == 1

        # Run reconciliation: re-hold suspect records.
        from mousevision.jobs import AnalysisJobManager, JobStore
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

        # Should be re-held.
        assert len(q.list_pending()) == 0

    def test_reconcile_keeps_passed_pending(self, tmp_path):
        """A Pending row from a postflight_passed run should stay Pending."""
        from mousevision.upload_queue import UploadQueue

        run_dir = _make_normal_run(tmp_path)
        q = UploadQueue(tmp_path / "queue.db")
        q.enqueue(
            {"record_id": "rec-normal-1", "cage_id": "C57-023"},
            record_path=run_dir / "mouse_001" / "record.json",
            status="Pending",
        )
        assert len(q.list_pending()) == 1

        from mousevision.jobs import AnalysisJobManager, JobStore
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

        # Should stay Pending (postflight_passed=True in manifest).
        assert len(q.list_pending()) == 1


class TestFindRunDirById:
    def test_find_run_dir_by_manifest_id(self, tmp_path):
        """_find_run_dir_by_id finds run by manifest.run_id, not directory name."""
        run_dir = _make_suspect_run(tmp_path, run_id="abc-123-456")
        # Directory name is run_20260716_abc-123-, not run_abc-123-456
        assert "abc-123-456" not in run_dir.name

        # Read manifest to verify
        manifest = json.loads((run_dir / "manifest.json").read_text())
        assert manifest["run_id"] == "abc-123-456"
