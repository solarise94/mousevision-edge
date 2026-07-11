"""Box (cage) registry with atomic per-cage ordinal allocation.

`project_id` is stored as a task label only (see design §3.5.3); the aggregation
key is `cage_id`, which must be globally unique across the deployment.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

_STRAIN_RULES = (("C57", "C57BL/6"), ("BALB", "BALB/c"))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def strain_from_cage(cage_id: str) -> str:
    upper = cage_id.upper()
    for prefix, strain in _STRAIN_RULES:
        if upper.startswith(prefix):
            return strain
    return "其他"


def qr_payload(cage_id: str, project_id: str = "default", version: int = 1) -> str:
    """Structured QR content: {v, project_id, cage_id} (design §3.5.4)."""
    return json.dumps(
        {"v": version, "project_id": project_id, "cage_id": cage_id},
        ensure_ascii=False,
        separators=(",", ":"),
    )


class BoxRegistry:
    """SQLite-backed cage metadata; `reserve_ordinal` is transaction-atomic."""

    _UPDATE_FIELDS = frozenset({"strain", "notes", "project_id", "mouse_no_start", "mouse_no_pad"})

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.lock:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS boxes (
                        cage_id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL DEFAULT 'default',
                        strain TEXT NOT NULL DEFAULT '其他',
                        notes TEXT NOT NULL DEFAULT '',
                        mouse_no_start INTEGER NOT NULL DEFAULT 1,
                        mouse_no_pad INTEGER NOT NULL DEFAULT 2,
                        next_ordinal INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
            finally:
                conn.close()

    @staticmethod
    def _to_public(row: sqlite3.Row) -> dict[str, Any]:
        box = dict(row)
        box["qr_payload"] = qr_payload(box["cage_id"], box.get("project_id", "default"))
        return box

    def get(self, cage_id: str) -> dict[str, Any] | None:
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM boxes WHERE cage_id = ?", (cage_id,)
                ).fetchone()
                return self._to_public(row) if row is not None else None
            finally:
                conn.close()

    def list(self, *, strain: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 500))
        with self.lock:
            conn = self._connect()
            try:
                if strain:
                    rows = conn.execute(
                        "SELECT * FROM boxes WHERE strain = ? ORDER BY created_at DESC LIMIT ?",
                        (strain, safe_limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM boxes ORDER BY created_at DESC LIMIT ?",
                        (safe_limit,),
                    ).fetchall()
                return [self._to_public(r) for r in rows]
            finally:
                conn.close()

    def create(
        self,
        *,
        cage_id: str,
        strain: str | None = None,
        notes: str = "",
        project_id: str = "default",
        mouse_no_start: int = 1,
        mouse_no_pad: int = 2,
    ) -> dict[str, Any]:
        now = _now()
        resolved_strain = strain or strain_from_cage(cage_id)
        start = max(1, int(mouse_no_start))
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO boxes (
                        cage_id, project_id, strain, notes,
                        mouse_no_start, mouse_no_pad, next_ordinal,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cage_id,
                        project_id,
                        resolved_strain,
                        notes,
                        start,
                        max(1, int(mouse_no_pad)),
                        start,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise KeyError(f"box already exists: {cage_id}") from exc
            finally:
                conn.close()
        box = self.get(cage_id)
        assert box is not None
        return box

    def update(self, cage_id: str, **changes: Any) -> dict[str, Any]:
        unknown = set(changes) - self._UPDATE_FIELDS
        if unknown:
            raise ValueError(f"unsupported box fields: {sorted(unknown)}")
        if not changes:
            box = self.get(cage_id)
            if box is None:
                raise KeyError(cage_id)
            return box
        changes["updated_at"] = _now()
        columns = list(changes)
        assignments = ", ".join(f"{name} = ?" for name in columns)
        values = [changes[name] for name in columns]
        with self.lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    f"UPDATE boxes SET {assignments} WHERE cage_id = ?",
                    (*values, cage_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(cage_id)
            finally:
                conn.close()
        box = self.get(cage_id)
        assert box is not None
        return box

    def ensure(
        self, cage_id: str, *, project_id: str = "default"
    ) -> dict[str, Any]:
        """Return existing box or auto-create one (for ad-hoc manual cage ids)."""
        box = self.get(cage_id)
        if box is not None:
            return box
        try:
            return self.create(cage_id=cage_id, project_id=project_id)
        except KeyError:
            existing = self.get(cage_id)
            assert existing is not None
            return existing

    def reserve_ordinal(
        self, cage_id: str, *, count: int = 1, project_id: str = "default"
    ) -> int:
        """Atomically reserve `count` ordinals; return the first reserved number.

        Auto-creates the box when missing so manually typed cage ids work.
        """
        count = max(1, int(count))
        with self.lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT next_ordinal FROM boxes WHERE cage_id = ?", (cage_id,)
                ).fetchone()
                if row is None:
                    now = _now()
                    start = 1
                    conn.execute(
                        """
                        INSERT INTO boxes (
                            cage_id, project_id, strain, notes,
                            mouse_no_start, mouse_no_pad, next_ordinal,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, '', ?, 2, ?, ?, ?)
                        """,
                        (
                            cage_id,
                            project_id,
                            strain_from_cage(cage_id),
                            start,
                            start + count,
                            now,
                            now,
                        ),
                    )
                    conn.execute("COMMIT")
                    return start
                first = int(row["next_ordinal"])
                conn.execute(
                    "UPDATE boxes SET next_ordinal = ?, updated_at = ? WHERE cage_id = ?",
                    (first + count, _now(), cage_id),
                )
                conn.execute("COMMIT")
                return first
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def release_ordinal(self, cage_id: str, ordinal: int) -> None:
        """Best-effort rollback of a reserved ordinal (only if still the tail).

        Avoids gaps for the common single-phone sequential case; if another
        reservation advanced `next_ordinal` in between, we leave the gap.
        """
        with self.lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT next_ordinal FROM boxes WHERE cage_id = ?", (cage_id,)
                ).fetchone()
                if row is not None and int(row["next_ordinal"]) == int(ordinal) + 1:
                    conn.execute(
                        "UPDATE boxes SET next_ordinal = ?, updated_at = ? WHERE cage_id = ?",
                        (int(ordinal), _now(), cage_id),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
