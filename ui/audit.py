"""Operation audit log store with sensitive-field scrubbing."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Any

SENSITIVE_KEYS = frozenset(
    {
        "password",
        "password_hash",
        "salt",
        "token",
        "secret",
        "api_token",
        "session",
        "mv_session",
        "x_mousevision_token",
    }
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def scrub_sensitive(value: Any) -> Any:
    """Recursively redact password/token/secret fields before persistence."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_KEYS or any(
                s in str(key).lower() for s in ("password", "secret", "token")
            ):
                out[key] = "***"
            else:
                out[key] = scrub_sensitive(item)
        return out
    if isinstance(value, list):
        return [scrub_sensitive(item) for item in value]
    return value


class AuditStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.lock = threading.Lock()
        self._init_db()
        self.scrub_existing_logs()

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

    def scrub_existing_logs(self) -> int:
        """Best-effort cleanup of previously logged plaintext secrets."""
        pattern = re.compile(
            r'"(password|password_hash|salt|token|secret|api_token|session)"\s*:\s*"[^"]*"',
            re.I,
        )
        updated = 0
        with self.lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id, detail FROM audit_logs WHERE detail IS NOT NULL"
                ).fetchall()
                for row in rows:
                    raw = row["detail"] or ""
                    try:
                        parsed = json.loads(raw)
                        scrubbed = scrub_sensitive(parsed)
                        new_text = json.dumps(scrubbed, ensure_ascii=False)
                    except json.JSONDecodeError:
                        new_text = pattern.sub(r'"\1":"***"', raw)
                    if new_text != raw:
                        conn.execute(
                            "UPDATE audit_logs SET detail = ? WHERE id = ?",
                            (new_text, row["id"]),
                        )
                        updated += 1
            finally:
                conn.close()
        return updated

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
        safe_detail = scrub_sensitive(detail) if detail is not None else None
        detail_text = (
            json.dumps(safe_detail, ensure_ascii=False) if safe_detail is not None else None
        )
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
            "detail": safe_detail,
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
                    item["detail"] = scrub_sensitive(json.loads(item["detail"]))
                except json.JSONDecodeError:
                    pass
            items.append(item)
        return {"items": items, "total": int(total_row["n"] if total_row else 0)}
