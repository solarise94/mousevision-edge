"""Persistent upload and analysis jobs for the mobile web workflow."""

from __future__ import annotations

import queue
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from mousevision.pipeline import WeighingPipeline, load_config
from mousevision.run import renumber_records


JOB_STATUSES = frozenset(
    {"uploading", "queued", "processing", "completed", "failed", "canceled"}
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _cleanup_upload(video_path: str | Path | None) -> None:
    """Remove uploaded source video after a job finishes to save disk space."""
    if not video_path:
        return
    path = Path(str(video_path))
    if not path.is_file():
        return
    try:
        path.unlink()
        parent = path.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass


class JobStore:
    """SQLite-backed job metadata; uploaded video bytes stay on disk."""

    _UPDATE_FIELDS = frozenset(
        {
            "status",
            "stage",
            "progress",
            "video_path",
            "original_filename",
            "content_type",
            "size_bytes",
            "run_id",
            "record_count",
            "message",
            "error",
            "requested_ordinal",
            "queued_at",
            "processing_started_at",
            "completed_at",
        }
    )

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.lock:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS analysis_jobs (
                        job_id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        cage_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        progress REAL NOT NULL DEFAULT 0,
                        original_filename TEXT,
                        content_type TEXT,
                        size_bytes INTEGER NOT NULL DEFAULT 0,
                        video_path TEXT,
                        run_id TEXT,
                        record_count INTEGER NOT NULL DEFAULT 0,
                        requested_ordinal INTEGER,
                        message TEXT,
                        error TEXT,
                        queued_at TEXT,
                        processing_started_at TEXT,
                        completed_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS analysis_jobs_created_idx
                    ON analysis_jobs(created_at DESC)
                    """
                )
                self._migrate(conn)
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(analysis_jobs)")}
        additions = {
            "requested_ordinal": "INTEGER",
            "queued_at": "TEXT",
            "processing_started_at": "TEXT",
            "completed_at": "TEXT",
        }
        for column, coltype in additions.items():
            if column not in existing:
                conn.execute(
                    f"ALTER TABLE analysis_jobs ADD COLUMN {column} {coltype}"
                )

    def create_job(
        self,
        *,
        project_id: str,
        cage_id: str,
        original_filename: str | None,
        content_type: str | None,
        requested_ordinal: int | None = None,
    ) -> dict[str, Any]:
        job_id = str(uuid.uuid4())
        now = _now()
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO analysis_jobs (
                        job_id, project_id, cage_id, status, stage, progress,
                        original_filename, content_type, requested_ordinal,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 'uploading', 'uploading', 0, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        project_id,
                        cage_id,
                        original_filename,
                        content_type,
                        requested_ordinal,
                        now,
                        now,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        job = self.get(job_id)
        assert job is not None
        return job

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM analysis_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                return dict(row) if row is not None else None
            finally:
                conn.close()

    def list_jobs(self, limit: int = 30) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        with self.lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT * FROM analysis_jobs
                    ORDER BY created_at DESC, job_id DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

    def list_by_status(self, status: str) -> list[dict[str, Any]]:
        with self.lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT * FROM analysis_jobs
                    WHERE status = ?
                    ORDER BY created_at ASC, job_id ASC
                    """,
                    (status,),
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        unknown = set(changes) - self._UPDATE_FIELDS
        if unknown:
            raise ValueError(f"unsupported job fields: {sorted(unknown)}")
        if "status" in changes and changes["status"] not in JOB_STATUSES:
            raise ValueError(f"invalid job status: {changes['status']}")
        if not changes:
            job = self.get(job_id)
            if job is None:
                raise KeyError(job_id)
            return job

        changes["updated_at"] = _now()
        columns = list(changes)
        assignments = ", ".join(f"{name} = ?" for name in columns)
        values = [changes[name] for name in columns]
        with self.lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    f"UPDATE analysis_jobs SET {assignments} WHERE job_id = ?",
                    (*values, job_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(job_id)
                conn.commit()
            finally:
                conn.close()
        job = self.get(job_id)
        assert job is not None
        return job

    def fail_interrupted(self) -> None:
        """Make interrupted uploads/analysis explicit after a service restart."""
        with self.lock:
            conn = self._connect()
            try:
                now = _now()
                conn.execute(
                    """
                    UPDATE analysis_jobs
                    SET status = 'failed', stage = 'interrupted', progress = 0,
                        error = '服务重启导致任务中断，请重新提交', updated_at = ?
                    WHERE status IN ('uploading', 'processing')
                    """,
                    (now,),
                )
                conn.commit()
            finally:
                conn.close()

    def active_count(self) -> int:
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM analysis_jobs
                    WHERE status IN ('uploading', 'queued', 'processing')
                    """
                ).fetchone()
                return int(row["n"] if row is not None else 0)
            finally:
                conn.close()

    def clear(self) -> None:
        with self.lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM analysis_jobs")
                conn.commit()
            finally:
                conn.close()

    def list_by_cage(self, cage_id: str, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 500))
        with self.lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT * FROM analysis_jobs
                    WHERE cage_id = ?
                    ORDER BY created_at ASC, job_id ASC
                    LIMIT ?
                    """,
                    (cage_id, safe_limit),
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

    def avg_duration_sec(self, sample: int = 10) -> float | None:
        """Sliding average of processing_started_at → completed_at (seconds)."""
        with self.lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT processing_started_at, completed_at
                    FROM analysis_jobs
                    WHERE status = 'completed'
                      AND processing_started_at IS NOT NULL
                      AND completed_at IS NOT NULL
                    ORDER BY completed_at DESC
                    LIMIT ?
                    """,
                    (max(1, int(sample)),),
                ).fetchall()
            finally:
                conn.close()
        durations: list[float] = []
        for row in rows:
            try:
                start = datetime.fromisoformat(row["processing_started_at"])
                end = datetime.fromisoformat(row["completed_at"])
            except (TypeError, ValueError):
                continue
            delta = (end - start).total_seconds()
            if delta >= 0:
                durations.append(delta)
        if not durations:
            return None
        return round(sum(durations) / len(durations), 1)


AnalysisFn = Callable[[dict[str, Any]], dict[str, Any]]


class AnalysisJobManager:
    """Single-process, single-worker analysis queue for a 2C/4G edge server.

    Concurrency model: one in-memory ``queue.Queue`` fed by one daemon thread.
    ``start()`` recovers queued jobs from SQLite once on startup; ``submit()``
    enqueues under a lock; ``stop()`` wakes the worker with a ``None`` sentinel.
    There is NO polling or orphan scan — the worker blocks on ``get()``.

    This only works with a single application process. Do NOT run multiple
    uvicorn/gunicorn workers (``--workers N``); for multi-process or
    multi-host deployment, replace this with Redis/RQ, Celery, or similar.
    """

    def __init__(
        self,
        store: JobStore,
        *,
        output_root: str | Path,
        config_path: str | Path,
        templates_dir: str | Path,
        analysis_fn: AnalysisFn | None = None,
        reserve_ordinals: Callable[[str, int, str], int] | None = None,
    ) -> None:
        self.store = store
        self.output_root = Path(output_root)
        self.config_path = Path(config_path)
        self.templates_dir = Path(templates_dir)
        self.analysis_fn = analysis_fn or self._run_pipeline
        # (cage_id, count, project_id) -> first reserved ordinal
        self.reserve_ordinals = reserve_ordinals
        self._pipeline: WeighingPipeline | None = None
        if analysis_fn is None:
            config = load_config(self.config_path)
            self._pipeline = WeighingPipeline(config, self.templates_dir)
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self.store.fail_interrupted()
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()
            queued = [job["job_id"] for job in self.store.list_by_status("queued")]
        for job_id in queued:
            self._queue.put(job_id)

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)

    def submit(self, job_id: str) -> dict[str, Any]:
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        raw_path = job.get("video_path")
        if not raw_path:
            raise ValueError("job video_path is missing")
        video_path = Path(str(raw_path))
        if not video_path.is_file():
            raise FileNotFoundError(video_path)
        with self._lock:
            queued = self.store.update(
                job_id,
                status="queued",
                stage="queued",
                progress=0.05,
                message="视频已上传，等待分析",
                error=None,
                queued_at=_now(),
            )
            self._queue.put(job_id)
        return queued

    def _worker(self) -> None:
        """Blocking consumer. Woken by submit() or the None sentinel in stop().

        No polling, no orphan scan. `start()` restores queued jobs once on
        startup; submit() enqueues under the lock. Requires a single process —
        do NOT run with multiple uvicorn workers (use Redis/RQ for that).
        """
        while not self._stop.is_set():
            job_id = self._queue.get()  # blocks until submit() or sentinel
            if job_id is None:
                break
            job = self.store.get(job_id)
            if job is None or job.get("status") != "queued":
                continue
            video_path = job.get("video_path")
            try:
                self.store.update(
                    job_id,
                    status="processing",
                    stage="ocr_and_curve_analysis",
                    progress=0.15,
                    message="正在识别称量过程",
                    error=None,
                    processing_started_at=_now(),
                )
                result = self.analysis_fn(job)
                count = int(result.get("record_count") or 0)
                self.store.update(
                    job_id,
                    status="completed",
                    stage="completed",
                    progress=1.0,
                    run_id=result.get("run_id"),
                    record_count=count,
                    message=f"分析完成，共检出 {count} 只",
                    error=None,
                    completed_at=_now(),
                )
            except Exception as exc:
                self.store.update(
                    job_id,
                    status="failed",
                    stage="failed",
                    progress=1.0,
                    message="分析失败",
                    error=str(exc),
                    completed_at=_now(),
                )
            finally:
                _cleanup_upload(video_path)

    def _run_pipeline(self, job: dict[str, Any]) -> dict[str, Any]:
        raw_path = job.get("video_path")
        if not raw_path:
            raise ValueError("job video_path is missing")
        video_path = Path(str(raw_path))
        if not video_path.is_file():
            raise FileNotFoundError(video_path)
        if self._pipeline is None:
            raise RuntimeError("analysis pipeline is not initialized")
        requested = job.get("requested_ordinal")
        start_ordinal = int(requested) if requested else 1
        cage_id = str(job["cage_id"])
        project_id = str(job.get("project_id") or "default")
        result = self._pipeline.run_video(
            str(video_path),
            cage_id=cage_id,
            output_root=self.output_root,
            stop_after_first=False,
            create_run=True,
            start_ordinal=start_ordinal,
            project_id=project_id,
        )
        records = result.records or []
        count = len(records)
        # Multi-detect: a job that reserved 1 slot produced N>1 records. Reserve
        # the extra N-1 ordinals now and renumber the trailing records so they
        # never overlap another cage's numbers (design §3.5.2 rule 4).
        if count > 1 and self.reserve_ordinals is not None and result.run_dir is not None:
            try:
                extra_base = int(self.reserve_ordinals(cage_id, count - 1, project_id))
                new_ordinals = [start_ordinal] + list(
                    range(extra_base, extra_base + count - 1)
                )
                renumber_records(result.run_dir, new_ordinals)
            except Exception:
                # Renumber is best-effort; keep original records on failure.
                pass
        return {
            "run_id": result.run_id,
            "record_count": count,
        }
