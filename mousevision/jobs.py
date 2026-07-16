"""Persistent upload and analysis jobs for the mobile web workflow."""

from __future__ import annotations

import json
import queue
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from mousevision.capture_geom import validate_canvas_video_geometry
from mousevision.pipeline import WeighingPipeline, load_config
from mousevision.run import renumber_records
from mousevision.source.video import VideoFileSource, VideoFormatError


JOB_STATUSES = frozenset(
    {"uploading", "queued", "processing", "completed", "failed", "canceled"}
)

# Keep every uploaded source video this long after its job reaches a terminal
# state, so the clip can be re-inspected later (e.g. a 0-detect or failed run).
# The worker prunes expired clips opportunistically; expired == completed_at
# older than this many days.
VIDEO_RETENTION_DAYS = 14

# Terminal statuses whose source video is eligible for retention-based prune.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "canceled"})


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _now_dt() -> datetime:
    return datetime.now()


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
            "decoded_frames",
            "recorded_duration_sec",
            "preview_crop",
            "capture_mode",
            "capture_meta",
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
                        decoded_frames INTEGER NOT NULL DEFAULT 0,
                        recorded_duration_sec INTEGER NOT NULL DEFAULT 0,
                        preview_crop TEXT,
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
            "decoded_frames": "INTEGER NOT NULL DEFAULT 0",
            "recorded_duration_sec": "INTEGER NOT NULL DEFAULT 0",
            "preview_crop": "TEXT",
            "capture_mode": "TEXT",
            "capture_meta": "TEXT",
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

    def list_prunable(self, before_dt: datetime) -> list[dict[str, Any]]:
        """Terminal jobs whose source video should be pruned: completed_at is
        set and strictly older than ``before_dt``. Only terminal jobs are
        candidates so an in-flight upload is never deleted out from under the
        worker.
        """
        cutoff = before_dt.isoformat(timespec="seconds")
        placeholders = ",".join("?" for _ in _TERMINAL_STATUSES)
        with self.lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"""
                    SELECT * FROM analysis_jobs
                    WHERE status IN ({placeholders})
                      AND completed_at IS NOT NULL
                      AND completed_at != ''
                      AND completed_at < ?
                    """,
                    (*_TERMINAL_STATUSES, cutoff),
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
                        error = '服务重启导致任务中断，请重新提交',
                        completed_at = ?, updated_at = ?
                    WHERE status IN ('uploading', 'processing')
                    """,
                    (now, now),
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


def _parse_preview_crop(raw: Any) -> dict[str, float] | None:
    """Decode a persisted ``preview_crop`` JSON blob into {x,y,w,h} floats.

    Stored as TEXT in the job row. Returns ``None`` for missing/invalid input
    so the pipeline falls back to full-frame analysis (legacy behaviour). Each
    value is clamped to [0,1]; the rectangle must have positive area and must
    lie fully within the frame (x+w <= 1, y+h <= 1). An overhanging rectangle
    such as {x:0.9, w:0.9} is rejected rather than silently truncated, so the
    analysed region always matches the persisted metadata.
    """
    if not raw:
        return None
    try:
        obj = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    try:
        vals = {
            k: float(obj[k])
            for k in ("x", "y", "w", "h")
        }
    except (KeyError, TypeError, ValueError):
        return None
    for k in vals:
        vals[k] = max(0.0, min(vals[k], 1.0))
    if vals["w"] <= 0.0 or vals["h"] <= 0.0:
        return None
    if vals["x"] + vals["w"] > 1.0 + 1e-9 or vals["y"] + vals["h"] > 1.0 + 1e-9:
        return None
    return vals


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
        release_ordinals: Callable[[str, int], None] | None = None,
        upload_queue: "UploadQueue | None" = None,
    ) -> None:
        self.store = store
        self.output_root = Path(output_root)
        self.config_path = Path(config_path)
        self.templates_dir = Path(templates_dir)
        self.analysis_fn = analysis_fn or self._run_pipeline
        # (cage_id, count, project_id) -> first reserved ordinal
        self.reserve_ordinals = reserve_ordinals
        # (cage_id, ordinal) -> release a reserved ordinal (best-effort, tail-only)
        self.release_ordinals = release_ordinals
        # Shared upload queue; injected so renumber refresh is authoritative.
        self.upload_queue = upload_queue
        self._pipeline: WeighingPipeline | None = None
        if analysis_fn is None:
            config = load_config(self.config_path)
            self._pipeline = WeighingPipeline(config, self.templates_dir)
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # Opportunistic-prune throttle: only sweep the jobs table every N jobs
        # processed, instead of on every single completion.
        self._prune_interval = 8
        self._jobs_since_prune = 0

    def start(self) -> None:
        self._reconcile_held()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self.store.fail_interrupted()
            # Drop source clips that aged out during downtime.
            try:
                self.prune_uploads()
            except Exception:
                pass
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
            cage_id = str(job.get("cage_id") or "")
            requested_ordinal = job.get("requested_ordinal")
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
                decoded_frames = int(result.get("decoded_frames") or 0)
                # Zero-detect: the reserved slot detected nothing. Release it
                # (best-effort, tail-only) so a run of empty videos does not
                # permanently consume ordinals (design §3.5.2 rule 3).
                if (
                    count == 0
                    and self.release_ordinals is not None
                    and requested_ordinal is not None
                ):
                    self.release_ordinals(cage_id, int(requested_ordinal))
                self.store.update(
                    job_id,
                    status="completed",
                    stage="completed",
                    progress=1.0,
                    run_id=result.get("run_id"),
                    record_count=count,
                    decoded_frames=decoded_frames,
                    message=("未检出鼠只" if count == 0 else f"分析完成，共检出 {count} 只"),
                    error=None,
                    completed_at=_now(),
                )
            except VideoFormatError as exc:
                # Corrupt/truncated upload. Ordinal release is handled entirely
                # inside _run_pipeline: the zero-decode path releases the
                # requested ordinal directly (nothing was persisted), and the
                # truncation path rolls back persisted records and releases
                # ordinals only when the rollback is confirmed clean. Releasing
                # here too would either double-release (harmless) or, worse,
                # release against a rollback shortfall (leaving orphan records
                # that could collide with a reused ordinal). So this branch does
                # NOT release — it only records the failure.
                self.store.update(
                    job_id,
                    status="failed",
                    stage="failed",
                    progress=1.0,
                    message="视频格式异常",
                    error=str(exc),
                    completed_at=_now(),
                )
            except Exception as exc:
                # Analysis failed: release the reserved ordinal so failed jobs
                # do not permanently occupy a slot (design §3.5.2 rule 3).
                if (
                    self.release_ordinals is not None
                    and requested_ordinal is not None
                ):
                    try:
                        self.release_ordinals(cage_id, int(requested_ordinal))
                    except Exception:
                        pass
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
                # Source videos are retained for VIDEO_RETENTION_DAYS so a
                # 0-detect or failed run can be re-inspected. Prune
                # opportunistically but not on every single job (avoid scanning
                # the whole table each time).
                self._maybe_prune_uploads()

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
        # Mobile capture modes:
        # - canvas: client already recorded a 720x1280 center-cropped canvas
        #   stream — do NOT apply preview_crop; optionally normalize size.
        # - css_crop / unset: legacy path using preview_crop coordinates.
        # - system: legacy system-camera upload; no crop; full-frame analysis.
        capture_mode = str(job.get("capture_mode") or "").strip().lower()
        if capture_mode == "canvas":
            # Do not trust the client label alone: verify structured meta and
            # a decoded first-frame aspect near 9:16 before skipping crop /
            # stretching to 720x1280.
            try:
                first = next(
                    iter(VideoFileSource(video_path, max_frames=1).frames()),
                    None,
                )
            except VideoFormatError:
                self._release_ordinals(cage_id, [start_ordinal] if requested else [])
                raise
            if first is None:
                self._release_ordinals(cage_id, [start_ordinal] if requested else [])
                raise VideoFormatError(
                    "视频格式异常：无法解码任何帧（疑似录制分片损坏，请重录）"
                )
            fh, fw = first.image.shape[:2]
            try:
                validate_canvas_video_geometry(
                    fw, fh, capture_meta=job.get("capture_meta")
                )
            except ValueError as exc:
                self._release_ordinals(cage_id, [start_ordinal] if requested else [])
                raise VideoFormatError(str(exc)) from exc
            crop = None
            normalize = True
        else:
            crop = _parse_preview_crop(job.get("preview_crop"))
            normalize = False
        # run_video can raise VideoFormatError itself when the video is
        # completely unopenable (VideoFileSource.frames() raises on
        # isOpened()==False). At that point no records, queue entries, or extra
        # ordinals have been created, so there is nothing to roll back — but the
        # requested ordinal MUST be released or the cage permanently skips a
        # number. Catch, release, re-raise.
        #
        # Note: run_video creates the run directory BEFORE opening the video,
        # so an unopenable clip leaves an empty run_<...>/ dir on disk (marked
        # status="empty" by its finally). The job is not linked to this run_id
        # (we never reach the return), so it is an orphan directory. It is tiny
        # and can be swept by a future orphan-run-dir cleaner; it is not cleaned
        # here to keep the format-error path simple and focused on the ordinal.
        try:
            result = self._pipeline.run_video(
                str(video_path),
                cage_id=cage_id,
                output_root=self.output_root,
                stop_after_first=False,
                create_run=True,
                start_ordinal=start_ordinal,
                project_id=project_id,
                upload_queue=self.upload_queue,
                crop=crop,
                normalize_to_reference=normalize,
            )
        except VideoFormatError:
            self._release_ordinals(cage_id, [start_ordinal] if requested else [])
            raise
        decoded_frames = int(result.samples or 0)
        # Zero-decode guard: OpenCV opened the file but decoded zero frames.
        # This is the fingerprint of a completely unopenable/empty container
        # (distinct from truncation, which decodes SOME frames). No records
        # were persisted (the driver saved nothing), so there is nothing to
        # roll back — just release the requested ordinal and fail. This check
        # lives here (not in the worker) so ALL VideoFormatError paths own
        # their own ordinal release and the worker's except branch never
        # double-releases or releases against a shortfall.
        if decoded_frames == 0:
            self._release_ordinals(cage_id, [start_ordinal] if requested else [])
            raise VideoFormatError(
                "视频格式异常：无法解码任何帧（疑似录制分片损坏，请重录）"
            )
        records = result.records or []
        count = len(records)
        # Track ordinals reserved for a multi-detect so a later truncation
        # failure can release them as part of the rollback.
        extra_ordinals: list[int] = []
        # Multi-detect: a job reserved 1 slot produced N>1 records. Reserve
        # the extra N-1 ordinals now and renumber the trailing records so they
        # never overlap another cage's numbers (design §3.5.2 rule 4).
        # A renumber failure is NOT swallowed — it must fail the job so the
        # colliding ordinals are never presented as a successful result, and
        # any half-applied rename is rolled back by renumber_records itself.
        # The extra reserved range is also released on failure so it does not
        # leak (note: with tail-only release the range may still leave a gap if
        # another reservation advanced next_ordinal — see design §3.5.2).
        if count > 1 and self.reserve_ordinals is not None and result.run_dir is not None:
            extra_base = int(self.reserve_ordinals(cage_id, count - 1, project_id))
            extra_ordinals = list(range(extra_base, extra_base + count - 1))
            new_ordinals = [start_ordinal] + list(extra_ordinals)
            try:
                renumber_records(result.run_dir, new_ordinals)
            except Exception:
                # Renumber failed (rolled back by renumber_records). Release the
                # extra reserved range so it is not permanently leaked.
                self._release_ordinals(cage_id, extra_ordinals)
                raise
            # Renumber moved mouse_NNN dirs and rewrote record.json; refresh the
            # upload queue so sync sees the final paths and ordinals, not the
            # pre-renumber ones the driver enqueued mid-analysis.
            self._refresh_queue_after_renumber(result.run_dir)
            # records dicts are stale post-renumber; re-read for the return path.
            records = self._read_records(result.run_dir)
        # Truncation guard: a MediaRecorder-timeslice clip often opens and
        # decodes the FIRST fragmented-MP4 shard (~2 s), so decoded_frames > 0
        # and the zero-frame guard above does not catch it. If the client
        # declared a recording length, compare the decoded duration against it;
        # a clip that decodes far less than recorded is corrupt/truncated.
        #
        # By this point run_video may have persisted records, written to the
        # upload queue, and reserved extra ordinals. If the clip is truncated we
        # must roll ALL of that back so a failed job leaves no orphan records
        # that sync could later push. The rollback returns a diagnostic if any
        # cleanup step fell short (e.g. run_dir could not be deleted) — in that
        # case ordinals are deliberately NOT released, and the diagnostic is
        # folded into the error so an operator can clean up manually.
        try:
            self._check_truncation(video_path, decoded_frames, job)
        except VideoFormatError as exc:
            # v3: do NOT rollback/delete. Keep all records Held (already
            # enqueued as Held during analysis) and mark the run as
            # format_suspect for manual confirmation.
            run_dir = Path(getattr(result, "run_dir", None) or "")
            self._mark_run_format_suspect(run_dir, str(exc))
            raise
        # P0-b: postflight passed - release Held queue rows for this run.
        run_dir = Path(getattr(result, "run_dir", None) or "")
        if not run_dir:
            run_dir = Path(getattr(result, "output_root", "") or "")
        if run_dir and run_dir.is_dir():
            self._release_held_for_run(run_dir)
        elif self.upload_queue is not None:
            self.upload_queue.release_held(None)
        return {
            "run_id": result.run_id,
            "record_count": count,
            "decoded_frames": decoded_frames,
        }



    def _reconcile_held(self) -> None:
        """Startup reconciliation: if any Pending queue rows belong to runs
        that never passed postflight (format_suspect or crashed), re-hold them.
        """
        if self.upload_queue is None:
            return
        try:
            pending = self.upload_queue.list_pending(limit=10000)
            to_hold: list[str] = []
            for row in pending:
                record_path = Path(row.get("record_path") or "")
                if not record_path.exists():
                    continue
                import json as _json
                raw = _json.loads(record_path.read_text(encoding="utf-8"))
                if raw.get("format_suspect"):
                    to_hold.append(str(raw.get("record_id") or ""))
            if to_hold:
                self.upload_queue.hold_pending(to_hold)
        except Exception:
            pass

    def _mark_run_format_suspect(self, run_dir: Path, reason: str) -> None:
        """Mark all records under run_dir as format_suspect (stays Held)."""
        run_dir = Path(run_dir)
        if not run_dir.is_dir():
            return
        import json as _json
        for rec_path in sorted(run_dir.glob("mouse_*/record.json")):
            try:
                raw = _json.loads(rec_path.read_text(encoding="utf-8"))
                raw["format_suspect"] = True
                raw["format_suspect_reason"] = reason
                rec_path.write_text(
                    _json.dumps(raw, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:
                pass

    def _release_held_for_run(self, run_dir: Path) -> None:
        """Promote Held queue rows for records under run_dir to Pending.

        Called after successful postflight (truncation/codec checks). Idempotent.
        """
        if self.upload_queue is None:
            return
        run_dir = Path(run_dir)
        if not run_dir.is_dir():
            return
        record_ids: list[str] = []
        for rec_path in sorted(run_dir.glob("mouse_*/record.json")):
            try:
                import json as _json
                raw = _json.loads(rec_path.read_text(encoding="utf-8"))
                rid = str(raw.get("record_id") or "")
                if rid:
                    record_ids.append(rid)
            except Exception:
                continue
        if record_ids:
            self.upload_queue.release_held(record_ids)

    def _check_truncation(
        self, video_path: Path, decoded_frames: int, job: dict[str, Any]
    ) -> None:
        """Raise VideoFormatError if the clip decodes far less than recorded.

        Only consulted when the client sent ``recorded_duration_sec``. The
        decoded duration is measured from the real first/last showinfo PTS
        of the decoded source stream (via ``VideoFileSource.decoded_duration_sec``),
        which is accurate for VFR videos. When PTS is unavailable, falls back
        to ``decoded_frames / fps`` with a 15fps default. A clip is truncated
        when its decoded duration is under half the recorded length - the
        original fragmented MP4 bug typically exposes only the first ~2s
        shard, so a 10–15s recording decoding to ~2s trips this cleanly.
        """
        recorded = int(job.get("recorded_duration_sec") or 0)
        if recorded <= 0 or decoded_frames <= 0:
            return
        # Use real PTS-based duration; fallback to fps estimate inside.
        try:
            decoded_duration = VideoFileSource(video_path).decoded_duration_sec()
        except Exception:
            decoded_duration = 0.0
        if decoded_duration <= 0:
            # Fallback: decoded_frames / fps (unified 15fps default).
            try:
                fps = float(VideoFileSource(video_path).probe().get("fps") or 0.0)
            except Exception:
                fps = 0.0
            if fps <= 1e-3:
                fps = 15.0
            decoded_duration = decoded_frames / fps
        # Allow generous slack (variable fps, early stop), but flag a clip that
        # decodes to under half its recorded length.
        if decoded_duration < recorded * 0.5:
            raise VideoFormatError(
                f"视频格式异常：可解码时长约 {decoded_duration:.1f}s，"
                f"远短于录制时长 {recorded}s（疑似录制分片损坏，请重录）"
            )

    def _release_ordinals(self, cage_id: str, ordinals: list[int]) -> None:
        """Best-effort release of a set of reserved ordinals (used by rollback
        and the renumber-failure path).

        ``release_ordinal`` is tail-only: it reclaims ``ordinal`` only when
        ``ordinal == next_ordinal - 1``. To reclaim a contiguous reserved
        range ``[1,2,3,4]`` with ``next_ordinal=5`` the ordinals MUST be
        released in DESCENDING order (4 first, then 3, 2, 1) so each release
        exposes the next tail. Releasing in ascending order leaves every
        non-tail ordinal as a permanent gap. Duplicates are de-duplicated
        before sorting.
        """
        if not ordinals or self.release_ordinals is None:
            return
        # De-duplicate, then release highest-first so tail-only reclaim can
        # cascade back through the whole contiguous range.
        for ord_val in sorted(set(int(o) for o in ordinals), reverse=True):
            try:
                self.release_ordinals(cage_id, ord_val)
            except Exception:
                pass

    def _rollback_persisted_run(
        self,
        result: Any,
        cage_id: str,
        extra_ordinals: list[int],
        requested_ordinal: int | None,
    ) -> str | None:
        """Undo the side effects of run_video when a post-run guard (e.g.
        truncation) fails after records were already persisted.

        Removes, in order:
          1. upload-queue entries for each record_id (so sync never pushes
             orphan records from a failed job),
          2. the on-disk run directory (record.json / photo.jpg / mouse_NNN),
          3. reserved ordinals (extra multi-detect + the requested slot) —
             ONLY if the run directory is confirmed gone and every queue entry
             was confirmed deleted. Releasing ordinals while orphan records
             still exist on disk would let a new job reuse the same ordinal and
             collide with the stale data.

        Returns a diagnostic string describing any rollback shortfall (queue
        delete failures / run_dir still present), or ``None`` if the rollback
        fully succeeded. The caller folds the diagnostic into the job error so
        the original VideoFormatError still propagates but the shortfall is
        visible for manual cleanup. Rollback never raises.
        """
        records = list(result.records or []) if result is not None else []
        shortfalls: list[str] = []

        # 1. upload queue: delete by record_id. Track any that failed.
        queue_ok = True
        if self.upload_queue is not None:
            for rec in records:
                rid = str(rec.get("record_id") or "")
                if not rid:
                    continue
                try:
                    self.upload_queue.delete_by_record_id(rid)
                except Exception as exc:
                    queue_ok = False
                    shortfalls.append(f"queue delete failed for {rid}: {exc}")
        # If there were records but no queue to clean, that's fine (test paths).

        # 2. run directory on disk. Confirm removal rather than fire-and-forget.
        run_dir = getattr(result, "run_dir", None)
        dir_removed = True
        if run_dir:
            rd = Path(run_dir)
            try:
                shutil.rmtree(rd)
            except FileNotFoundError:
                pass  # already gone — counts as removed
            except Exception as exc:
                shortfalls.append(f"rmtree failed for {rd}: {exc}")
            if rd.exists():
                dir_removed = False
                shortfalls.append(f"run_dir still exists: {rd}")

        # 3. reserved ordinals — ONLY when the persisted data is confirmed gone.
        # Releasing ordinals while records remain would let a new job reuse the
        # same ordinal and collide with stale data. Skip release on shortfall
        # and surface it so an operator can clean up + release manually.
        to_release = list(extra_ordinals)
        if requested_ordinal is not None:
            to_release.append(int(requested_ordinal))
        if dir_removed and queue_ok:
            self._release_ordinals(cage_id, to_release)
        else:
            shortfalls.append(
                "ordinals NOT released (orphan data may remain); "
                f"cage={cage_id} ordinals={sorted(set(int(o) for o in to_release))}"
            )

        return "; ".join(shortfalls) if shortfalls else None

    @staticmethod
    def _read_records(run_dir: Path) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for rec_path in sorted(Path(run_dir).glob("mouse_*/record.json")):
            try:
                out.append(json.loads(rec_path.read_text(encoding="utf-8")))
            except Exception:
                continue
        return out

    def _refresh_queue_after_renumber(self, run_dir: Path) -> None:
        """Update each record's queue entry (path + payload) post-renumber.

        Re-reads the final record.json files and pushes fresh path/payload into
        the upload queue keyed by record_id, so later WiFi sync sees the renamed
        directories and final ordinals instead of the pre-renumber snapshot.

        This is NOT best-effort: if the queue is unavailable or any record fails
        to refresh, an exception propagates so the job is marked failed rather
        than leaving stale (now-invalid) paths in the sync queue.
        """
        if self.upload_queue is None:
            # No shared queue (e.g. unit tests with a custom analysis_fn). Nothing
            # to refresh — the driver's internal queue was never populated.
            return
        missing: list[str] = []
        for rec_path in sorted(Path(run_dir).glob("mouse_*/record.json")):
            rec = json.loads(rec_path.read_text(encoding="utf-8"))
            rid = rec.get("record_id")
            if not rid:
                continue
            photo = rec_path.parent / "photo.jpg"
            updated = self.upload_queue.update_by_record_id(
                rid,
                rec,
                rec_path,
                photo if photo.exists() else None,
            )
            if not updated:
                missing.append(rid)
        if missing:
            raise RuntimeError(
                f"upload queue refresh failed for record_ids: {missing}"
            )

    # -- source-video retention ------------------------------------------------
    def prune_uploads(self, retention_days: int = VIDEO_RETENTION_DAYS) -> int:
        """Delete source videos for terminal jobs older than ``retention_days``.

        Safe to call from the worker thread or at startup. Returns the number
        of clips removed. Only the on-disk upload file is deleted (plus its
        directory if empty); the job row stays in analysis_jobs for history.
        """
        cutoff = _now_dt() - timedelta(days=int(retention_days))
        removed = 0
        for job in self.store.list_prunable(cutoff):
            _cleanup_upload(job.get("video_path"))
            removed += 1
        return removed

    def _maybe_prune_uploads(self) -> None:
        """Throttled prune called from the worker after each job completes.

        Runs at most once every ``self._prune_interval`` jobs so the table is
        not scanned on every single completion.
        """
        self._jobs_since_prune += 1
        if self._jobs_since_prune < self._prune_interval:
            return
        self._jobs_since_prune = 0
        try:
            self.prune_uploads()
        except Exception:
            # Prune is best-effort housekeeping; never fail a job because of it.
            pass
