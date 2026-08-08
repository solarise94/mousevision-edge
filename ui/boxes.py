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
        self,
        cage_id: str,
        *,
        count: int = 1,
        project_id: str = "default",
        baseline_records: str | Path | None = None,
    ) -> int:
        """Atomically reserve `count` ordinals; return the first reserved number.

        Auto-creates the box when missing so manually typed cage ids work. When
        ``baseline_records`` points at the output root, a missing/stale box is
        seeded from existing run records so ordinals never collide with history.
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
                    # Seed from existing records when available so a freshly-
                    # created box never hands out ordinals already on disk.
                    if baseline_records is not None:
                        maxima = self._collect_record_maxima(Path(baseline_records))
                        existing = maxima.get(cage_id, 0)
                        if existing >= start:
                            start = existing + 1
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

    def sync_from_records(self, output_root: str | Path) -> dict[str, int]:
        """One-time upgrade: bump each box's next_ordinal past existing records.

        Scans ``run_*/mouse_*/record.json`` under ``output_root`` and, for each
        ``cage_id``, ensures ``next_ordinal >= max(actual_ordinal) + 1``. Boxes
        are auto-created when records exist for an unregistered cage. Idempotent
        and monotonic — never lowers an existing ``next_ordinal``.
        """
        output_root = Path(output_root)
        maxima = self._collect_record_maxima(output_root)
        bumped: dict[str, int] = {}
        for cage_id, max_ord in maxima.items():
            required = max_ord + 1
            with self.lock:
                conn = self._connect()
                try:
                    row = conn.execute(
                        "SELECT next_ordinal FROM boxes WHERE cage_id = ?",
                        (cage_id,),
                    ).fetchone()
                    if row is None:
                        now = _now()
                        conn.execute(
                            """
                            INSERT INTO boxes (
                                cage_id, project_id, strain, notes,
                                mouse_no_start, mouse_no_pad, next_ordinal,
                                created_at, updated_at
                            ) VALUES (?, 'default', ?, '', ?, 2, ?, ?, ?)
                            """,
                            (
                                cage_id,
                                strain_from_cage(cage_id),
                                max(1, max_ord),
                                required,
                                now,
                                now,
                            ),
                        )
                        bumped[cage_id] = required
                    else:
                        current = int(row["next_ordinal"])
                        if current < required:
                            conn.execute(
                                "UPDATE boxes SET next_ordinal = ?, updated_at = ? WHERE cage_id = ?",
                                (required, _now(), cage_id),
                            )
                            bumped[cage_id] = required
                    conn.commit()
                finally:
                    conn.close()
        return bumped

    @staticmethod
    def _collect_record_maxima(output_root: Path) -> dict[str, int]:
        """Map cage_id → max ordinal across all run directories.

        Scans ``run_*/**/record.json`` so both the current ``mouse_NNN/`` layout
        and the legacy ``<stamp>_<box>/`` nested layout are covered. The ordinal
        fallback chain mirrors ``MouseRegistry._mice_in_dir``:
        actual_ordinal → ordinal → session_index → 0.
        """
        maxima: dict[str, int] = {}
        if not output_root.exists():
            return maxima
        for rec_path in output_root.glob("run_*/**/record.json"):
            try:
                rec = json.loads(rec_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            cage = rec.get("cage_id") or rec.get("box_id")
            if not cage:
                continue
            ordinal = int(
                rec.get("actual_ordinal")
                or rec.get("ordinal")
                or rec.get("session_index")
                or 0
            )
            if ordinal > maxima.get(str(cage), 0):
                maxima[str(cage)] = ordinal
        return maxima

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

    def advance_next_ordinal(
        self, cage_id: str, at_least: int, project_id: str = "default"
    ) -> int:
        """服务端兜底推进箱子 ``next_ordinal`` 至少到 ``at_least``，返回新值。

        设备端称重上报（``/api/records/report``）落盘后调用，避免同箱下次
        录制从旧编号重号。与 :meth:`reserve_ordinal` 的语义互补：

        - 箱子存在 → 在 lock 内 ``UPDATE boxes SET next_ordinal = MAX(next_ordinal, at_least)``；
          单调推进，永不回退已分配的编号。
        - 箱子不存在 → 按 reserve_ordinal 的自动建箱逻辑建箱（mouse_no_start 保持
          默认 1，因为历史记录已在盘上；next_ordinal 直接设为 at_least，下一条
          录制从该编号开始，不与本次上报的记录重号）。

        注意：本方法不扫盘（不依赖 baseline_records），因为调用方已据本次写入的
        记录计算好 ``at_least``。共享端点（/api/records/share）不调用此方法，
        使共享数据不推进实验室箱子编号。
        """
        at_least = max(0, int(at_least))
        with self.lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT next_ordinal FROM boxes WHERE cage_id = ?", (cage_id,)
                ).fetchone()
                if row is None:
                    now = _now()
                    # 自动建箱：next_ordinal 设为 at_least（下一条录制从此编号起，
                    # 不与本次上报记录重号）。mouse_no_start 保持默认 1。
                    conn.execute(
                        """
                        INSERT INTO boxes (
                            cage_id, project_id, strain, notes,
                            mouse_no_start, mouse_no_pad, next_ordinal,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, '', 1, 2, ?, ?, ?)
                        """,
                        (
                            cage_id,
                            project_id,
                            strain_from_cage(cage_id),
                            at_least,
                            now,
                            now,
                        ),
                    )
                    conn.execute("COMMIT")
                    return at_least
                current = int(row["next_ordinal"])
                new_value = max(current, at_least)
                if new_value != current:
                    conn.execute(
                        "UPDATE boxes SET next_ordinal = ?, updated_at = ? WHERE cage_id = ?",
                        (new_value, _now(), cage_id),
                    )
                conn.execute("COMMIT")
                return new_value
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
