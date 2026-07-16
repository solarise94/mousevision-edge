"""Local upload queue skeleton (SQLite). Server sync is phase-2."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


class UploadStatus(str, Enum):
    PENDING = "Pending"
    UPLOADED = "Uploaded"
    RETRY = "Retry"


class UploadQueue:
    """PoC skeleton: enqueue local records for later WiFi sync."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS upload_queue (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        record_id TEXT,
                        box_id TEXT NOT NULL,
                        record_path TEXT NOT NULL,
                        photo_path TEXT,
                        payload TEXT NOT NULL,
                        status TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                # Migrate older DBs that lack record_id.
                cols = {r[1] for r in conn.execute("PRAGMA table_info(upload_queue)").fetchall()}
                if "record_id" not in cols:
                    conn.execute("ALTER TABLE upload_queue ADD COLUMN record_id TEXT")
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_upload_record_id "
                    "ON upload_queue(record_id) WHERE record_id IS NOT NULL AND record_id != ''"
                )
                conn.commit()
            finally:
                conn.close()

    def enqueue(self, record: dict[str, Any], record_path: Path, photo_path: Path | None = None) -> int:
        """Insert pending item. Idempotent on record_id: returns existing id if already queued."""
        now = datetime.now().isoformat(timespec="seconds")
        record_id = str(record.get("record_id") or "")
        with self.lock:
            conn = self._connect()
            try:
                if record_id:
                    existing = conn.execute(
                        "SELECT id FROM upload_queue WHERE record_id = ?",
                        (record_id,),
                    ).fetchone()
                    if existing is not None:
                        return int(existing["id"])
                cur = conn.execute(
                    """
                    INSERT INTO upload_queue
                    (record_id, box_id, record_path, photo_path, payload, status, attempts, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        record_id or None,
                        str(record.get("cage_id") or record.get("box_id", "")),
                        str(record_path),
                        str(photo_path) if photo_path else None,
                        json.dumps(record, ensure_ascii=False),
                        UploadStatus.PENDING.value,
                        now,
                        now,
                    ),
                )
                conn.commit()
                return int(cur.lastrowid)
            finally:
                conn.close()

    def update_by_record_id(
        self, record_id: str, record: dict[str, Any], record_path: Path, photo_path: Path | None
    ) -> bool:
        """Refresh a queued item after its ordinal/path changed (e.g. renumber).

        Updates record_path, photo_path and payload for the given record_id.
        Returns True if a row was updated.
        """
        if not record_id:
            return False
        now = datetime.now().isoformat(timespec="seconds")
        with self.lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    UPDATE upload_queue
                    SET record_path = ?, photo_path = ?, payload = ?, updated_at = ?
                    WHERE record_id = ?
                    """,
                    (
                        str(record_path),
                        str(photo_path) if photo_path else None,
                        json.dumps(record, ensure_ascii=False),
                        now,
                        record_id,
                    ),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def delete_by_record_id(self, record_id: str) -> int:
        """Remove queued items for a record (e.g. when the record is deleted)."""
        if not record_id:
            return 0
        with self.lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "DELETE FROM upload_queue WHERE record_id = ?", (record_id,)
                )
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    def get_payload(self, record_id: str) -> dict[str, Any] | None:
        """Return the queued JSON payload for ``record_id``, or None."""
        if not record_id:
            return None
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT payload FROM upload_queue WHERE record_id = ?",
                    (record_id,),
                ).fetchone()
                if row is None:
                    return None
                return json.loads(str(row["payload"]))
            finally:
                conn.close()

    def list_pending(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT id, record_id, box_id, record_path, photo_path, status, attempts, created_at
                    FROM upload_queue
                    WHERE status IN (?, ?)
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (UploadStatus.PENDING.value, UploadStatus.RETRY.value, limit),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def mark_uploaded(self, item_id: int) -> None:
        self._set_status(item_id, UploadStatus.UPLOADED)

    def mark_retry(self, item_id: int) -> None:
        with self.lock:
            conn = self._connect()
            try:
                now = datetime.now().isoformat(timespec="seconds")
                conn.execute(
                    """
                    UPDATE upload_queue
                    SET status = ?, attempts = attempts + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (UploadStatus.RETRY.value, now, item_id),
                )
                conn.commit()
            finally:
                conn.close()

    def _set_status(self, item_id: int, status: UploadStatus) -> None:
        with self.lock:
            conn = self._connect()
            try:
                now = datetime.now().isoformat(timespec="seconds")
                conn.execute(
                    """
                    UPDATE upload_queue
                    SET status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status.value, now, item_id),
                )
                conn.commit()
            finally:
                conn.close()

    def counts(self) -> dict[str, int]:
        with self.lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT status, COUNT(*) AS n FROM upload_queue GROUP BY status"
                ).fetchall()
                return {str(r["status"]): int(r["n"]) for r in rows}
            finally:
                conn.close()
