"""Persistent analysis job queue tests."""

from __future__ import annotations

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
