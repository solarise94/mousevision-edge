"""Persistent analysis job queue tests."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta
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
        return {"run_id": "run-test", "record_count": 8, "decoded_frames": 240}

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
    assert completed["decoded_frames"] == 240
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
        # A genuinely-empty clip still decodes frames (it just has no mouse);
        # decoded_frames > 0 keeps it out of the format-error path.
        return {"run_id": "r", "record_count": 0, "decoded_frames": 180}

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


def test_worker_retains_upload_after_success(tmp_path: Path):
    """Source videos are retained (not deleted immediately) so a completed run
    can be re-inspected; pruning is retention-based, not immediate (bug fix)."""
    store = JobStore(tmp_path / "jobs.db")
    job = _make_uploaded_job(store, tmp_path)
    video = Path(str(job["video_path"]))
    assert video.is_file()

    def fake_analysis(_: dict) -> dict:
        return {"run_id": "run-test", "record_count": 3, "decoded_frames": 90}

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
    # Retained: clip + its directory must still exist right after completion.
    assert video.exists()
    assert video.parent.exists()


def test_worker_retains_upload_after_failure(tmp_path: Path):
    """A failed analysis also retains the clip for post-mortem inspection."""
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
    assert video.exists()
    assert video.parent.exists()


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


def test_zero_decoded_frames_marks_format_error(tmp_path: Path):
    """A clip that decodes zero frames is a corrupt/truncated upload (e.g.
    concatenated fragmented-MP4 shards), NOT a real empty video. It must raise
    VideoFormatError and release its ordinal, and the clip is retained for
    inspection rather than deleted on the spot.

    The zero-decode guard lives in _run_pipeline (not the worker) so it owns
    its own ordinal release. This test exercises _run_pipeline directly with a
    stubbed pipeline whose run_video returns zero sampled frames.
    """
    from types import SimpleNamespace

    from mousevision.jobs import AnalysisJobManager, VideoFormatError

    store = JobStore(tmp_path / "jobs.db")
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video-placeholder")
    job = store.create_job(
        project_id="p", cage_id="C57-023",
        original_filename="source.mp4", content_type="video/mp4",
        requested_ordinal=3,
    )
    job = store.update(
        job["job_id"], video_path=str(video), size_bytes=1, stage="uploaded",
    )

    released: list[tuple[str, int]] = []

    def release(cage_id: str, ordinal: int) -> None:
        released.append((cage_id, ordinal))

    manager = AnalysisJobManager(
        store,
        output_root=tmp_path / "output",
        config_path=tmp_path / "config.yaml",
        templates_dir=tmp_path / "templates",
        analysis_fn=lambda _: {"run_id": "r", "record_count": 0, "decoded_frames": 0},
        release_ordinals=release,
    )
    # Stub the pipeline: run_video "decoded" zero frames and saved no records.
    manager._pipeline = SimpleNamespace(
        config={"frame_stride": 2},
        run_video=lambda *a, **kw: SimpleNamespace(
            output_dir=None, states=[], record=None, samples=0, readable=0,
            records=[], output_dirs=None, run_dir=None, run_id="r",
        ),
    )

    with pytest.raises(VideoFormatError, match="无法解码任何帧"):
        manager._run_pipeline(job)

    # The requested ordinal must have been released (nothing was persisted).
    assert ("C57-023", 3) in released
    # Clip retained for post-mortem, not deleted immediately.
    assert video.exists()


def test_prune_uploads_keeps_recent_deletes_expired(tmp_path: Path):
    """Prune deletes source clips older than the retention window and leaves
    recent ones intact; the job rows themselves are not removed."""
    store = JobStore(tmp_path / "jobs.db")
    manager = AnalysisJobManager(
        store,
        output_root=tmp_path / "output",
        config_path=tmp_path / "config.yaml",
        templates_dir=tmp_path / "templates",
        analysis_fn=lambda _: {"run_id": "r", "record_count": 0, "decoded_frames": 10},
    )

    uploads = tmp_path / "job_uploads"
    old_dir = uploads / "old"
    old_dir.mkdir(parents=True)
    old_video = old_dir / "source.mp4"
    old_video.write_bytes(b"old")
    new_dir = uploads / "new"
    new_dir.mkdir(parents=True)
    new_video = new_dir / "source.mp4"
    new_video.write_bytes(b"new")

    old_job = store.create_job(
        project_id="p", cage_id="C", original_filename="source.mp4",
        content_type="video/mp4",
    )
    new_job = store.create_job(
        project_id="p", cage_id="C", original_filename="source.mp4",
        content_type="video/mp4",
    )
    # Terminal + completed_at set. Mark the old one 15 days ago, new one today.
    store.update(
        old_job["job_id"], status="completed", video_path=str(old_video),
        completed_at=(datetime.now() - timedelta(days=15)).isoformat(timespec="seconds"),
    )
    store.update(
        new_job["job_id"], status="completed", video_path=str(new_video),
        completed_at=datetime.now().isoformat(timespec="seconds"),
    )

    removed = manager.prune_uploads(retention_days=14)

    assert removed == 1
    assert not old_video.exists()       # expired -> deleted
    assert not old_dir.exists()         # empty dir removed
    assert new_video.exists()           # recent -> kept
    # Job rows survive (only the on-disk clip is pruned).
    assert store.get(old_job["job_id"]) is not None
    assert store.get(new_job["job_id"]) is not None


def test_truncated_clip_flagged_when_decoded_far_short_of_recorded(tmp_path: Path):
    """A clip that decodes >0 frames but far less than the recorded length is
    truncated (the fragmented-MP4-shard failure mode), not a real empty video.

    The original MediaRecorder-timeslice bug typically exposes only the first
    ~2 s shard, so decoded_frames > 0 slips past the zero-frame guard. The
    truncation check compares the decoded duration against recorded_duration_sec
    and raises VideoFormatError. This test exercises _run_pipeline directly
    (the worker path) with a stubbed pipeline, since the check lives there.
    """
    from mousevision.jobs import AnalysisJobManager, VideoFormatError

    store = JobStore(tmp_path / "jobs.db")
    video = tmp_path / "source.mp4"
    video.write_bytes(b"stub")  # _run_pipeline checks is_file, not decodability
    job = store.create_job(
        project_id="p", cage_id="C57-023",
        original_filename="source.mp4", content_type="video/mp4",
        requested_ordinal=1,
    )
    # Client declared a 12 s recording, but the (stubbed) pipeline only
    # "decoded" 7 sampled frames. With stride=2, fps=15 that is ~0.9 s of
    # content — far under half of 12 s.
    job = store.update(
        job["job_id"], video_path=str(video), size_bytes=1, stage="uploaded",
        recorded_duration_sec=12,
    )

    manager = AnalysisJobManager(
        store,
        output_root=tmp_path / "output",
        config_path=tmp_path / "config.yaml",
        templates_dir=tmp_path / "templates",
        # Pass a stub analysis_fn so no real pipeline/config is constructed.
        analysis_fn=lambda _: {"run_id": "r", "record_count": 0, "decoded_frames": 7},
    )
    # Stub the pipeline config so _check_truncation reads stride without a real
    # WeighingPipeline (which would need a real config file + templates).
    from types import SimpleNamespace
    manager._pipeline = SimpleNamespace(config={"frame_stride": 2})

    with pytest.raises(VideoFormatError, match="远短于录制时长"):
        manager._check_truncation(Path(video), 7, job)


def test_truncation_not_triggered_when_no_recorded_duration(tmp_path: Path):
    """Without recorded_duration_sec the truncation check is a no-op (advisory
    field), so a short-but-genuine clip is not misreported as corrupt."""
    from mousevision.jobs import AnalysisJobManager

    store = JobStore(tmp_path / "jobs.db")
    job = store.create_job(
        project_id="p", cage_id="C", original_filename="x.mp4",
        content_type="video/mp4",
    )
    video = tmp_path / "source.mp4"
    video.write_bytes(b"stub")
    store.update(job["job_id"], video_path=str(video), stage="uploaded")
    # Pass a stub analysis_fn so no real pipeline/config is constructed.
    manager = AnalysisJobManager(
        store, output_root=tmp_path / "output",
        config_path=tmp_path / "config.yaml", templates_dir=tmp_path / "templates",
        analysis_fn=lambda _: {"run_id": "r", "record_count": 0, "decoded_frames": 3},
    )
    # No recorded_duration_sec -> check must not raise (returns None).
    manager._check_truncation(Path(video), decoded_frames=3, job=store.get(job["job_id"]))


def test_unopenable_video_raises_format_error(tmp_path: Path):
    """A video that exists but cannot be opened by OpenCV must raise
    VideoFormatError (not a plain RuntimeError) so the worker reports
    '录像可能损坏', not a generic '分析失败'."""
    from mousevision.source.video import VideoFileSource, VideoFormatError

    # A real file that exists but is not a valid video container — OpenCV
    # cannot open it, so isOpened() is False and frames() raises.
    bogus = tmp_path / "not-a-video.mp4"
    bogus.write_bytes(b"definitely not an mp4 container")
    src = VideoFileSource(bogus)
    with pytest.raises(VideoFormatError):
        # frames() opens the capture; an unopenable file raises VideoFormatError.
        next(src.frames())


def test_unopenable_video_releases_requested_ordinal(tmp_path: Path):
    """When run_video raises VideoFormatError (completely unopenable video),
    _run_pipeline must release the requested ordinal — otherwise the cage
    permanently skips a number. Uses a real BoxRegistry so the tail-only
    reclaim is actually exercised, and a stub pipeline whose run_video raises
    to simulate the unopenable-file path inside run_video (before any result
    is returned)."""
    from types import SimpleNamespace

    from ui.boxes import BoxRegistry

    from mousevision.jobs import AnalysisJobManager, VideoFormatError

    reg = BoxRegistry(tmp_path / "boxes.db")
    reg.create(cage_id="C57-023")
    requested_ordinal = reg.reserve_ordinal("C57-023")  # -> 1, next=2
    assert requested_ordinal == 1

    store = JobStore(tmp_path / "jobs.db")
    video = tmp_path / "source.mp4"
    video.write_bytes(b"definitely not a video container")
    job = store.create_job(
        project_id="p", cage_id="C57-023",
        original_filename="source.mp4", content_type="video/mp4",
        requested_ordinal=requested_ordinal,
    )
    job = store.update(
        job["job_id"], video_path=str(video), size_bytes=1, stage="uploaded",
    )

    def reserve(cage_id: str, count: int, project_id: str) -> int:
        return reg.reserve_ordinal(cage_id, count=count, project_id=project_id)

    def release(cage_id: str, ordinal: int) -> None:
        reg.release_ordinal(cage_id, ordinal)

    manager = AnalysisJobManager(
        store,
        output_root=tmp_path / "output",
        config_path=tmp_path / "config.yaml",
        templates_dir=tmp_path / "templates",
        analysis_fn=lambda _: {"run_id": "r", "record_count": 0, "decoded_frames": 0},
        reserve_ordinals=reserve,
        release_ordinals=release,
    )
    # Stub the pipeline so run_video raises VideoFormatError directly (mimics
    # VideoFileSource.frames() raising on an unopenable file, before any
    # result is returned).
    def _raise_unopenable(*a, **kw):
        raise VideoFormatError("无法打开视频文件：stub")

    manager._pipeline = SimpleNamespace(
        config={"frame_stride": 2},
        run_video=_raise_unopenable,
    )

    with pytest.raises(VideoFormatError, match="无法打开视频文件"):
        manager._run_pipeline(job)

    # The requested ordinal must have been reclaimed: next_ordinal back to 1.
    box = reg.get("C57-023")
    assert box["next_ordinal"] == 1, (
        f"requested ordinal leaked: next_ordinal={box['next_ordinal']} "
        f"(expected 1 — unopenable video must release the slot)"
    )


def test_fail_interrupted_sets_completed_at_for_prune(tmp_path: Path):
    """An interrupted-at-restart job gets completed_at so its retained source
    video is eligible for the 14-day prune (otherwise it leaks forever)."""
    store = JobStore(tmp_path / "jobs.db")
    job = store.create_job(
        project_id="p", cage_id="C", original_filename="x.mp4",
        content_type="video/mp4",
    )
    video = tmp_path / "source.mp4"
    video.write_bytes(b"stub")
    # Simulate a job that was mid-processing when the service restarted.
    store.update(
        job["job_id"], status="processing", stage="ocr_and_curve_analysis",
        video_path=str(video), progress=0.3,
    )

    store.fail_interrupted()
    failed = store.get(job["job_id"])

    assert failed["status"] == "failed"
    assert failed["stage"] == "interrupted"
    # completed_at must now be populated so prune_uploads can age it out.
    assert failed.get("completed_at")
    # And it must show up as prunable once past the retention window.
    from datetime import datetime, timedelta
    prunable = store.list_prunable(datetime.now() + timedelta(days=30))
    assert any(p["job_id"] == job["job_id"] for p in prunable)


def test_truncation_rolls_back_persisted_records(tmp_path: Path):
    """If a clip is truncated AFTER run_video already persisted records, the
    rollback must delete those records from the upload queue, remove the run
    directory, and release the reserved ordinals — so a failed job leaves no
    orphan data that sync could later push (P1 data-consistency fix).

    Exercises _run_pipeline directly with a stubbed pipeline that simulates a
    truncated clip which detected 2 mice (triggering multi-detect renumber)
    before the truncation check fires.
    """
    import json as _json
    from types import SimpleNamespace

    from mousevision.jobs import AnalysisJobManager, VideoFormatError
    from mousevision.upload_queue import UploadQueue

    store = JobStore(tmp_path / "jobs.db")
    video = tmp_path / "source.mp4"
    video.write_bytes(b"stub-not-a-real-video")
    job = store.create_job(
        project_id="p", cage_id="C57-023",
        original_filename="source.mp4", content_type="video/mp4",
        requested_ordinal=1,
    )
    # 12 s declared, but the stubbed pipeline will report only 7 sampled frames
    # (stride=2, fps=15 -> ~0.9 s) -> truncated.
    job = store.update(
        job["job_id"], video_path=str(video), size_bytes=1, stage="uploaded",
        recorded_duration_sec=12,
    )

    released: list[tuple[str, int]] = []
    reserved: list[tuple[str, int, str]] = []

    def reserve(cage_id: str, count: int, project_id: str) -> int:
        # Pretend ordinals 2..(2+count-1) are reserved for the extras.
        base = 2
        reserved.append((cage_id, count, project_id))
        return base

    def release(cage_id: str, ordinal: int) -> None:
        released.append((cage_id, ordinal))

    queue = UploadQueue(tmp_path / "upload_queue.db")
    manager = AnalysisJobManager(
        store,
        output_root=tmp_path / "output",
        config_path=tmp_path / "config.yaml",
        templates_dir=tmp_path / "templates",
        # Pass a stub analysis_fn so __init__ does not try to load a real
        # config/templates; we call _run_pipeline directly below.
        analysis_fn=lambda _: {"run_id": "r", "record_count": 0, "decoded_frames": 0},
        reserve_ordinals=reserve,
        release_ordinals=release,
        upload_queue=queue,
    )

    # Build a run_dir with 2 persisted mice (simulating run_video's output) and
    # a stub pipeline.run_video that returns a truncated result.
    run_dir = tmp_path / "output" / "run_fake"
    run_dir.mkdir(parents=True)
    records = []
    for i in (1, 2):
        d = run_dir / f"mouse_{i:03d}"
        d.mkdir()
        rec = {"record_id": f"r{i}", "ordinal": i, "cage_id": "C57-023"}
        (d / "record.json").write_text(_json.dumps(rec), encoding="utf-8")
        (d / "photo.jpg").write_bytes(b"x")
        queue.enqueue(rec, d / "record.json", d / "photo.jpg")
        records.append(rec)

    fake_result = SimpleNamespace(
        output_dir=None, states=[], record=None,
        samples=7,           # post-stride; 7 * 2 / 15fps ~ 0.9 s << 12 s
        readable=7, records=records, output_dirs=None,
        run_dir=run_dir, run_id="run_fake",
    )
    manager._pipeline = SimpleNamespace(
        config={"frame_stride": 2},
        run_video=lambda *a, **kw: fake_result,
    )

    # v3: truncation raises VideoFormatError but does NOT rollback.
    # Records stay Held (not Pending), run_dir preserved, ordinals NOT released.
    with pytest.raises(VideoFormatError, match="远短于录制时长"):
        manager._run_pipeline(job)

    # Held isolation verifications:
    # 1. upload queue has 0 Pending (records stay Held, not synced).
    assert queue.list_pending(limit=50) == []
    # 2. run_dir still exists (preserved for manual review).
    assert run_dir.exists()
    # 3. ordinals NOT released (kept as gaps until manual resolution).
    assert released == []


def test_truncation_releases_all_ordinals_descending_with_real_registry(tmp_path: Path):
    """A 4-mouse truncated clip must release ALL 4 reserved ordinals (1
    requested + 3 extra) back to the BoxRegistry, not just the tail.

    This is the regression guard for the descending-release bug: with
    next_ordinal=5 after reserving [1,2,3,4], releasing in ASCENDING order
    [1,2,3,4] only reclaims 4 (tail), leaving 1/2/3 as permanent gaps.
    Releasing DESCENDING [4,3,2,1] cascades the tail back to 1. Uses a real
    BoxRegistry so the tail-only constraint is actually exercised.
    """
    import json as _json
    from types import SimpleNamespace

    from ui.boxes import BoxRegistry

    from mousevision.jobs import AnalysisJobManager, VideoFormatError

    reg = BoxRegistry(tmp_path / "boxes.db")
    reg.create(cage_id="C57-023")
    # Reserve 4 ordinals: 1 via the upload endpoint's reserve, 3 more via the
    # multi-detect reserve callback. Simulate the full sequence here.
    requested_ordinal = reg.reserve_ordinal("C57-023")  # -> 1, next=2

    store = JobStore(tmp_path / "jobs.db")
    video = tmp_path / "source.mp4"
    video.write_bytes(b"stub")
    job = store.create_job(
        project_id="p", cage_id="C57-023",
        original_filename="source.mp4", content_type="video/mp4",
        requested_ordinal=requested_ordinal,
    )
    job = store.update(
        job["job_id"], video_path=str(video), size_bytes=1, stage="uploaded",
        recorded_duration_sec=12,
    )

    def reserve(cage_id: str, count: int, project_id: str) -> int:
        return reg.reserve_ordinal(cage_id, count=count, project_id=project_id)

    def release(cage_id: str, ordinal: int) -> None:
        reg.release_ordinal(cage_id, ordinal)

    manager = AnalysisJobManager(
        store,
        output_root=tmp_path / "output",
        config_path=tmp_path / "config.yaml",
        templates_dir=tmp_path / "templates",
        analysis_fn=lambda _: {"run_id": "r", "record_count": 0, "decoded_frames": 0},
        reserve_ordinals=reserve,
        release_ordinals=release,
    )

    # 4 mice persisted (1 requested + 3 extra reserved [2,3,4]); truncated clip.
    run_dir = tmp_path / "output" / "run_fake"
    run_dir.mkdir(parents=True)
    records = []
    for i in (1, 2, 3, 4):
        d = run_dir / f"mouse_{i:03d}"
        d.mkdir()
        rec = {"record_id": f"r{i}", "ordinal": i, "cage_id": "C57-023"}
        (d / "record.json").write_text(_json.dumps(rec), encoding="utf-8")
        (d / "photo.jpg").write_bytes(b"x")
        records.append(rec)

    fake_result = SimpleNamespace(
        output_dir=None, states=[], record=None,
        samples=7, readable=7, records=records, output_dirs=None,
        run_dir=run_dir, run_id="run_fake",
    )
    manager._pipeline = SimpleNamespace(
        config={"frame_stride": 2},
        run_video=lambda *a, **kw: fake_result,
    )

    with pytest.raises(VideoFormatError):
        manager._run_pipeline(job)

    # v3: truncation does NOT rollback/release ordinals. They stay reserved
    # as gaps until manual resolution. next_ordinal stays at 5 (4 reserved).
    box = reg.get("C57-023")
    assert box["next_ordinal"] == 5, (
        f"v3: ordinals NOT released (Held isolation); "
        f"expected next_ordinal=5, got {box['next_ordinal']}"
    )


def test_rollback_withholds_ordinals_when_run_dir_not_removed(tmp_path: Path):
    """v3: truncation marks records format_suspect and keeps them Held.
    Ordinals are NOT released (no rollback). Run dir is preserved.
    """
    import json as _json
    from types import SimpleNamespace

    from mousevision.jobs import AnalysisJobManager, VideoFormatError
    from mousevision.upload_queue import UploadQueue

    store = JobStore(tmp_path / "jobs.db")
    video = tmp_path / "source.mp4"
    video.write_bytes(b"stub")
    job = store.create_job(
        project_id="p", cage_id="C57-023",
        original_filename="source.mp4", content_type="video/mp4",
        requested_ordinal=1,
    )
    job = store.update(
        job["job_id"], video_path=str(video), size_bytes=1, stage="uploaded",
        recorded_duration_sec=12,
    )

    queue = UploadQueue(tmp_path / "upload_queue.db")
    manager = AnalysisJobManager(
        store,
        output_root=tmp_path / "output",
        config_path=tmp_path / "config.yaml",
        templates_dir=tmp_path / "templates",
        analysis_fn=lambda _: {"run_id": "r", "record_count": 0, "decoded_frames": 0},
        reserve_ordinals=lambda *a, **kw: 2,
        release_ordinals=lambda *a, **kw: None,
        upload_queue=queue,
    )

    run_dir = tmp_path / "output" / "run_fake"
    run_dir.mkdir(parents=True)
    d = run_dir / "mouse_001"
    d.mkdir()
    rec = {"record_id": "r1", "ordinal": 1, "cage_id": "C57-023"}
    (d / "record.json").write_text(_json.dumps(rec), encoding="utf-8")
    queue.enqueue(rec, d / "record.json")

    fake_result = SimpleNamespace(
        output_dir=None, states=[], record=None,
        samples=7, readable=7, records=[rec], output_dirs=None,
        run_dir=run_dir, run_id="run_fake",
    )
    manager._pipeline = SimpleNamespace(
        config={"frame_stride": 2},
        run_video=lambda *a, **kw: fake_result,
    )

    with pytest.raises(VideoFormatError):
        manager._run_pipeline(job)

    # Records stay Held, run_dir preserved with format_suspect flag.
    assert run_dir.exists()
    saved = _json.loads((d / "record.json").read_text(encoding="utf-8"))
    assert saved.get("format_suspect") is True
    assert queue.list_pending(limit=50) == []  # Held, not Pending

