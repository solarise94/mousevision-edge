"""Persistent analysis job queue tests."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from mousevision.jobs import AnalysisJobManager, JobStore


def _make_uploaded_job(store: JobStore, tmp_path: Path) -> dict:
    upload_dir = tmp_path / "job_uploads" / "job-1"
    upload_dir.mkdir(parents=True)
    video = upload_dir / "source.mp4"
    video.write_bytes(b"video-placeholder")
    job = store.create_job(
        project_id="study-a",
        cage_id="C57-023",
        original_filename="source.mp4",
        content_type="video/mp4",
    )
    return store.update(
        job["job_id"],
        video_path=str(video),
        size_bytes=video.stat().st_size,
        stage="uploaded",
    )


def _wait_for_terminal(store: JobStore, job_id: str) -> dict:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        job = store.get(job_id)
        assert job is not None
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_job_store_persists_metadata(tmp_path: Path):
    db = tmp_path / "jobs.db"
    store = JobStore(db)
    created = _make_uploaded_job(store, tmp_path)

    reopened = JobStore(db)
    loaded = reopened.get(created["job_id"])

    assert loaded is not None
    assert loaded["project_id"] == "study-a"
    assert loaded["cage_id"] == "C57-023"
    assert loaded["size_bytes"] > 0


def test_single_worker_completes_job(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.db")
    job = _make_uploaded_job(store, tmp_path)

    def fake_analysis(_: dict) -> dict:
        return {"run_id": "run-test", "record_count": 8}

    manager = AnalysisJobManager(
        store,
        output_root=tmp_path / "output",
        config_path=tmp_path / "config.yaml",
        templates_dir=tmp_path / "templates",
        analysis_fn=fake_analysis,
    )
    manager.start()
    try:
        manager.submit(job["job_id"])
        completed = _wait_for_terminal(store, job["job_id"])
    finally:
        manager.stop()

    assert completed["status"] == "completed"
    assert completed["run_id"] == "run-test"
    assert completed["record_count"] == 8
    assert completed["progress"] == 1.0


def test_worker_records_analysis_failure(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.db")
    job = _make_uploaded_job(store, tmp_path)

    def broken_analysis(_: dict) -> dict:
        raise RuntimeError("decoder unavailable")

    manager = AnalysisJobManager(
        store,
        output_root=tmp_path / "output",
        config_path=tmp_path / "config.yaml",
        templates_dir=tmp_path / "templates",
        analysis_fn=broken_analysis,
    )
    manager.start()
    try:
        manager.submit(job["job_id"])
        failed = _wait_for_terminal(store, job["job_id"])
    finally:
        manager.stop()

    assert failed["status"] == "failed"
    assert "decoder unavailable" in failed["error"]


def test_zero_detect_releases_ordinal(tmp_path: Path):
    """A job that detects zero mice must release its reserved ordinal (bug #5)."""
    store = JobStore(tmp_path / "jobs.db")
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video-placeholder")
    job = store.create_job(
        project_id="p",
        cage_id="C57-023",
        original_filename="source.mp4",
        content_type="video/mp4",
        requested_ordinal=1,
    )
    store.update(job["job_id"], video_path=str(video), size_bytes=1, stage="uploaded")

    released: list[tuple[str, int]] = []

    def fake_analysis(_: dict) -> dict:
        return {"run_id": "r", "record_count": 0}

    def release(cage_id: str, ordinal: int) -> None:
        released.append((cage_id, ordinal))

    manager = AnalysisJobManager(
        store,
        output_root=tmp_path / "output",
        config_path=tmp_path / "config.yaml",
        templates_dir=tmp_path / "templates",
        analysis_fn=fake_analysis,
        release_ordinals=release,
    )
    manager.start()
    try:
        manager.submit(job["job_id"])
        completed = _wait_for_terminal(store, job["job_id"])
    finally:
        manager.stop()

    assert completed["status"] == "completed"
    assert completed["record_count"] == 0
    assert ("C57-023", 1) in released


def test_analysis_failure_releases_ordinal(tmp_path: Path):
    """A failed analysis must release its reserved ordinal (bug #5)."""
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

    def release(cage_id: str, ordinal: int) -> None:
        released.append((cage_id, ordinal))

    manager = AnalysisJobManager(
        store,
        output_root=tmp_path / "output",
        config_path=tmp_path / "config.yaml",
        templates_dir=tmp_path / "templates",
        analysis_fn=broken_analysis,
        release_ordinals=release,
    )
    manager.start()
    try:
        manager.submit(job["job_id"])
        _wait_for_terminal(store, job["job_id"])
    finally:
        manager.stop()

    assert ("C57-023", 7) in released


def test_concurrent_release_leaves_gap_accepted(tmp_path: Path):
    """Tail-only release cannot reclaim a non-tail ordinal; gap is accepted.

    This documents the design choice (see MOBILE_WEB_APP_DESIGN.md §3.5.2):
    job A reserves 1, job B reserves 2, A releases 1 — but 1 is not the tail,
    so it stays as a permanent gap. The test verifies this is the actual
    behavior so a future regression (silent reuse) is caught.
    """
    from ui.boxes import BoxRegistry

    reg = BoxRegistry(tmp_path / "boxes.db")
    reg.create(cage_id="C57-023")
    a = reg.reserve_ordinal("C57-023")  # 1
    b = reg.reserve_ordinal("C57-023")  # 2
    assert (a, b) == (1, 2)
    reg.release_ordinal("C57-023", a)  # not tail -> no reclaim
    c = reg.reserve_ordinal("C57-023")  # 3, not 1
    assert c == 3
    # Gap at 1 is permanent and visible; no silent reuse.
    assert reg.get("C57-023")["next_ordinal"] == 4


def test_queue_refresh_failure_fails_job(tmp_path: Path):
    """If upload queue refresh fails post-renumber, the job must fail (P2)."""
    import json as _json

    store = JobStore(tmp_path / "jobs.db")
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video-placeholder")
    job = store.create_job(
        project_id="p",
        cage_id="C57-023",
        original_filename="source.mp4",
        content_type="video/mp4",
        requested_ordinal=1,
    )
    store.update(job["job_id"], video_path=str(video), size_bytes=1, stage="uploaded")

    class BrokenQueue:
        def update_by_record_id(self, *a, **kw):
            raise sqlite3.Error("db locked")

    output_root = tmp_path / "output"

    def analysis_that_triggers_refresh(job_dict):
        # Simulate a multi-detect run dir with records, then call refresh.
        from pathlib import Path

        run_dir = Path(output_root) / "run_fake"
        run_dir.mkdir(parents=True, exist_ok=True)
        for i in range(1, 4):
            d = run_dir / f"mouse_{i:03d}"
            d.mkdir()
            (d / "record.json").write_text(
                _json.dumps({"record_id": f"r{i}", "ordinal": i}), encoding="utf-8"
            )
        manager._refresh_queue_after_renumber(run_dir)
        return {"run_id": "r", "record_count": 3}

    manager = AnalysisJobManager(
        store,
        output_root=output_root,
        config_path=tmp_path / "config.yaml",
        templates_dir=tmp_path / "templates",
        analysis_fn=analysis_that_triggers_refresh,
        upload_queue=BrokenQueue(),
    )

    manager.start()
    try:
        manager.submit(job["job_id"])
        result = _wait_for_terminal(store, job["job_id"])
    finally:
        manager.stop()

    assert result["status"] == "failed"
    assert "db locked" in (result.get("error") or "").lower()


def test_worker_cleans_up_upload_after_success(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.db")
    job = _make_uploaded_job(store, tmp_path)
    video = Path(str(job["video_path"]))
    assert video.is_file()

    def fake_analysis(_: dict) -> dict:
        return {"run_id": "run-test", "record_count": 3}

    manager = AnalysisJobManager(
        store,
        output_root=tmp_path / "output",
        config_path=tmp_path / "config.yaml",
        templates_dir=tmp_path / "templates",
        analysis_fn=fake_analysis,
    )
    manager.start()
    try:
        manager.submit(job["job_id"])
        completed = _wait_for_terminal(store, job["job_id"])
    finally:
        manager.stop()

    assert completed["status"] == "completed"
    assert not video.exists()
    assert not video.parent.exists()  # job_uploads/<job_id>/ removed


def test_worker_cleans_up_upload_after_failure(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.db")
    job = _make_uploaded_job(store, tmp_path)
    video = Path(str(job["video_path"]))
    assert video.is_file()

    def broken_analysis(_: dict) -> dict:
        raise RuntimeError("pipeline error")

    manager = AnalysisJobManager(
        store,
        output_root=tmp_path / "output",
        config_path=tmp_path / "config.yaml",
        templates_dir=tmp_path / "templates",
        analysis_fn=broken_analysis,
    )
    manager.start()
    try:
        manager.submit(job["job_id"])
        failed = _wait_for_terminal(store, job["job_id"])
    finally:
        manager.stop()

    assert failed["status"] == "failed"
    assert not video.exists()
    assert not video.parent.exists()  # job_uploads/<job_id>/ removed


def test_submit_rejects_missing_video_path(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.db")
    job = store.create_job(
        project_id="study-a",
        cage_id="C57-023",
        original_filename="source.mp4",
        content_type="video/mp4",
    )
    manager = AnalysisJobManager(
        store,
        output_root=tmp_path / "output",
        config_path=tmp_path / "config.yaml",
        templates_dir=tmp_path / "templates",
        analysis_fn=lambda _: {"run_id": "run-test", "record_count": 1},
    )
    with pytest.raises(ValueError, match="video_path"):
        manager.submit(job["job_id"])
