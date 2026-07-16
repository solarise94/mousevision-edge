"""Record lifecycle metadata overlay (status, publish, verify, notes)."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from typing import Any

RECORD_STATUSES = frozenset({"pending", "published", "deleted"})


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class RecordsMetaStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.lock:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS records_meta (
                        record_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL DEFAULT 'pending',
                        verified INTEGER NOT NULL DEFAULT 0,
                        published_at TEXT,
                        deleted_at TEXT,
                        operator TEXT,
                        notes TEXT NOT NULL DEFAULT '',
                        tags TEXT NOT NULL DEFAULT '',
                        detection_label TEXT,
                        original_weight REAL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS records_meta_status_idx
                    ON records_meta(status)
                    """
                )
                # P2-b: add detection_label / original_weight columns (idempotent).
                for col, decl in [("detection_label", "TEXT"), ("original_weight", "REAL")]:
                    try:
                        conn.execute(f"ALTER TABLE records_meta ADD COLUMN {col} {decl}")
                    except sqlite3.OperationalError:
                        pass  # column already exists
            finally:
                conn.close()

    def get(self, record_id: str) -> dict[str, Any] | None:
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM records_meta WHERE record_id = ?", (record_id,)
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def ensure(self, record_id: str) -> dict[str, Any]:
        existing = self.get(record_id)
        if existing:
            return existing
        now = _now()
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO records_meta (
                        record_id, status, verified, notes, tags, updated_at
                    ) VALUES (?, 'pending', 0, '', '', ?)
                    """,
                    (record_id, now),
                )
            finally:
                conn.close()
        row = self.get(record_id)
        assert row is not None
        return row

    def update(self, record_id: str, *, operator: str | None = None, **changes: Any) -> dict[str, Any]:
        allowed = {"status", "verified", "published_at", "deleted_at", "operator", "notes", "tags", "detection_label", "original_weight"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported meta fields: {sorted(unknown)}")
        if "status" in changes and changes["status"] not in RECORD_STATUSES:
            raise ValueError(f"invalid status: {changes['status']}")
        self.ensure(record_id)
        changes["updated_at"] = _now()
        if operator:
            changes["operator"] = operator
        columns = list(changes)
        assignments = ", ".join(f"{name} = ?" for name in columns)
        values = [changes[name] for name in columns]
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    f"UPDATE records_meta SET {assignments} WHERE record_id = ?",
                    (*values, record_id),
                )
            finally:
                conn.close()
        row = self.get(record_id)
        assert row is not None
        return row

    def effective_status(self, record_id: str) -> str:
        row = self.get(record_id)
        return row["status"] if row else "pending"

    def soft_delete(self, record_id: str, *, operator: str | None = None) -> dict[str, Any]:
        return self.update(
            record_id,
            status="deleted",
            deleted_at=_now(),
            operator=operator,
        )

    def restore(self, record_id: str, *, operator: str | None = None) -> dict[str, Any]:
        return self.update(
            record_id,
            status="pending",
            deleted_at=None,
            operator=operator,
        )

    def publish(self, record_id: str, *, operator: str | None = None) -> dict[str, Any]:
        return self.update(
            record_id,
            status="published",
            verified=1,
            published_at=_now(),
            operator=operator,
        )

    def unpublish(self, record_id: str, *, operator: str | None = None) -> dict[str, Any]:
        return self.update(
            record_id,
            status="pending",
            published_at=None,
            operator=operator,
        )

    def verify(self, record_id: str, *, operator: str | None = None) -> dict[str, Any]:
        return self.update(record_id, verified=1, operator=operator)

    def reject(self, record_id: str, *, operator: str | None = None) -> dict[str, Any]:
        return self.update(record_id, verified=0, operator=operator)

    def counts(self) -> dict[str, int]:
        with self.lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT status, COUNT(*) AS n FROM records_meta GROUP BY status"
                ).fetchall()
            finally:
                conn.close()
        out = {"pending": 0, "published": 0, "deleted": 0}
        for row in rows:
            out[str(row["status"])] = int(row["n"])
        return out
