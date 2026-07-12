"""Operation audit log store."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Any


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class AuditStore:
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
                    CREATE TABLE IF NOT EXISTS audit_logs (
                        id TEXT PRIMARY KEY,
                        at TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        action TEXT NOT NULL,
                        target_type TEXT,
                        target_id TEXT,
                        detail TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS audit_logs_at_idx
                    ON audit_logs(at DESC)
                    """
                )
            finally:
                conn.close()

    def log(
        self,
        *,
        actor: str,
        action: str,
        target_type: str | None = None,
        target_id: str | None = None,
        detail: Any = None,
    ) -> dict[str, Any]:
        entry_id = str(uuid.uuid4())
        at = _now()
        detail_text = json.dumps(detail, ensure_ascii=False) if detail is not None else None
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO audit_logs (
                        id, at, actor, action, target_type, target_id, detail
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (entry_id, at, actor, action, target_type, target_id, detail_text),
                )
            finally:
                conn.close()
        return {
            "id": entry_id,
            "at": at,
            "actor": actor,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "detail": detail,
        }

    def list(self, *, limit: int = 50, offset: int = 0, action: str | None = None) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit), 200))
        safe_offset = max(0, int(offset))
        where = ""
        params: list[Any] = []
        if action:
            where = "WHERE action = ?"
            params.append(action)
        with self.lock:
            conn = self._connect()
            try:
                total_row = conn.execute(
                    f"SELECT COUNT(*) AS n FROM audit_logs {where}", params
                ).fetchone()
                rows = conn.execute(
                    f"""
                    SELECT * FROM audit_logs {where}
                    ORDER BY at DESC, id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (*params, safe_limit, safe_offset),
                ).fetchall()
            finally:
                conn.close()
        items = []
        for row in rows:
            item = dict(row)
            if item.get("detail"):
                try:
                    item["detail"] = json.loads(item["detail"])
                except json.JSONDecodeError:
                    pass
            items.append(item)
        return {"items": items, "total": int(total_row["n"] if total_row else 0)}
