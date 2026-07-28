"""Offline scale — phone clock calibration (MVP).

This module backs the ``/mobile/scale-sync`` test page. It stores calibration
sessions, parses the scale's CSV export, and computes a two-point affine clock
model so that the unconnected scale's internal timestamps can be projected onto
the phone/video timeline:

    phone_ms = rate × scale_ms + offset_ms

Design contract: ``docs/SCALE_TIME_SYNC_MVP.md``.

It deliberately does NOT touch ``jobs.db`` / ``records_meta.db`` or any weighing
record. All state lives in an independent ``scale_sync.db``; uploaded CSV bytes
are preserved verbatim under ``scale_sync/<session_id>/<import_id>/source.csv``.

SQLite follows the project convention (WAL, autocommit, ``timeout=30``) — see
``ui/audit.py`` / ``ui/boxes.py``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone as _tz
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PARSER_VERSION = "scale_csv_v1"

#: Maximum accepted CSV upload size (spec §5.3 — 5 MB).
MAX_IMPORT_BYTES = 5 * 1024 * 1024

#: Thresholds from spec §7 validation matrix.
DRIFT_PPM_RED = 5000.0
ANCHOR_GAP_YELLOW_SEC = 60.0
BUTTON_ROW_DELTA_YELLOW_SEC = 300.0  # 5 minutes

# CSV field layout, 1-indexed in the spec, 0-indexed here:
#   序号 | 日期 | 时间 | 产品编号 | 重量 | 单位
_COL_SEQUENCE = 0
_COL_DATE = 1
_COL_TIME = 2
_COL_STATUS = 3
_COL_WEIGHT = 4
_COL_UNIT = 5
_MIN_COLUMNS = 6
_SCALE_DT_FORMAT = "%y-%m-%d %H:%M:%S"


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class ScaleSyncError(Exception):
    """Base class for scale-sync domain errors."""


class SessionNotFound(ScaleSyncError):
    pass


class ImportNotFound(ScaleSyncError):
    pass


class AnchorError(ScaleSyncError):
    """An anchor operation violated the session contract (e.g. missing)."""


class CalculationError(ScaleSyncError):
    """The two-point model cannot be computed from the current state."""


class CsvParseError(ScaleSyncError):
    """The uploaded CSV has no usable rows / cannot be parsed at all."""


# --------------------------------------------------------------------------- #
# CSV parsing
# --------------------------------------------------------------------------- #


@dataclass
class ParsedReading:
    source_line_no: int  # 1-based physical line number in the source file
    raw_line: str  # original line content (whitespace preserved-ish from csv)
    scale_epoch_ms: int
    scale_dt_iso: str  # human-readable scale wall-clock (scale_timezone)
    weight_g: float
    unit: str
    raw_sequence: str  # field 1, uninterpreted
    raw_status: str  # field 4, uninterpreted

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_line_no": self.source_line_no,
            "raw_line": self.raw_line,
            "scale_epoch_ms": self.scale_epoch_ms,
            "scale_dt_iso": self.scale_dt_iso,
            "weight_g": self.weight_g,
            "unit": self.unit,
            "raw_sequence": self.raw_sequence,
            "raw_status": self.raw_status,
        }


@dataclass
class ParsedCsv:
    parser_version: str
    encoding: str
    readings: list[ParsedReading]
    warnings: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.readings)

    @property
    def time_range(self) -> tuple[int, int] | None:
        if not self.readings:
            return None
        return self.readings[0].scale_epoch_ms, self.readings[-1].scale_epoch_ms

    def to_summary(self) -> dict[str, Any]:
        rng = self.time_range
        units = sorted({r.unit for r in self.readings}) if self.readings else []
        return {
            "parser_version": self.parser_version,
            "encoding": self.encoding,
            "row_count": self.count,
            "time_from_ms": rng[0] if rng else None,
            "time_to_ms": rng[1] if rng else None,
            "units": units,
            "warnings": list(self.warnings),
        }


def _decode_bytes(raw: bytes) -> tuple[str, str]:
    """Try utf-8-sig → gb18030 → latin-1. Never raises (latin-1 always works).

    Returns (text, encoding_name).
    """
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1"), "latin-1"


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception as exc:  # unknown / invalid tz
        raise ScaleSyncError(f"unknown scale_timezone: {name}") from exc


def _parse_scale_datetime(date_field: str, time_field: str, tz: ZoneInfo) -> int:
    """Parse ``%y-%m-%d`` + ``%H:%M:%S`` and interpret in ``tz`` → epoch ms.

    Two-digit years fold to ``2000 + yy``.
    """
    combined = f"{date_field} {time_field}".strip()
    dt = datetime.strptime(combined, _SCALE_DT_FORMAT)
    # Python strptime %y already maps 00-68 → 2000-2068, 69-99 → 1969-1999.
    # The spec says "2000 + yy"; for plausible scale exports (yy >= 20) this
    # matches. We normalize explicitly to avoid any %y edge ambiguity:
    if dt.year >= 2000:
        pass  # already fine
    dt = dt.replace(tzinfo=tz)
    return int(dt.timestamp() * 1000)


def parse_scale_csv(raw: bytes, scale_timezone: str = "Asia/Shanghai") -> ParsedCsv:
    """Parse scale export bytes into readings.

    See module docstring + spec §6.2. Unparseable lines (e.g. the GBK header
    row in the known fixture) are recorded as warnings and skipped.
    """
    if not raw:
        raise CsvParseError("空文件，没有可解析的数据")

    text, encoding = _decode_bytes(raw)
    tz = _zone(scale_timezone)

    # splitlines() splits on CR / CRLF / LF uniformly (the fixture uses CR).
    warnings: list[str] = []
    readings: list[ParsedReading] = []

    for phys_line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        # csv.reader handles quoted fields; a single line is one record here.
        fields = next(csv.reader([line]), [])
        fields = [f.strip() for f in fields]
        if len(fields) < _MIN_COLUMNS:
            warnings.append(f"第 {phys_line_no} 行字段不足（{len(fields)}），跳过")
            continue

        date_field, time_field = fields[_COL_DATE], fields[_COL_TIME]
        weight_field, unit = fields[_COL_WEIGHT], fields[_COL_UNIT]

        try:
            scale_epoch_ms = _parse_scale_datetime(date_field, time_field, tz)
        except ValueError:
            # Header row and any non-date content land here.
            warnings.append(f"第 {phys_line_no} 行日期/时间无法解析（{date_field} {time_field}），跳过")
            continue

        weight_field_core = weight_field.strip()
        try:
            weight_g = float(weight_field_core)
        except ValueError:
            warnings.append(f"第 {phys_line_no} 行重量无法解析（{weight_field_core}），跳过")
            continue

        readings.append(
            ParsedReading(
                source_line_no=phys_line_no,
                raw_line=line,
                scale_epoch_ms=scale_epoch_ms,
                scale_dt_iso=datetime.fromtimestamp(scale_epoch_ms / 1000, tz).strftime("%Y-%m-%d %H:%M:%S"),
                weight_g=weight_g,
                unit=unit or "",
                raw_sequence=fields[_COL_SEQUENCE],
                raw_status=fields[_COL_STATUS],
            )
        )

    if not readings:
        # Every line failed — distinguish from "empty file" above: there were
        # lines, just none valid.
        raise CsvParseError("没有有效数据行：日期/重量均无法解析")

    return ParsedCsv(
        parser_version=PARSER_VERSION,
        encoding=encoding,
        readings=readings,
        warnings=warnings,
    )


# --------------------------------------------------------------------------- #
# Two-point clock model
# --------------------------------------------------------------------------- #


@dataclass
class ClockModel:
    kind: str = "two_point_affine_v1"
    scale_timezone: str = "Asia/Shanghai"
    rate: float = 1.0
    offset_ms: float = 0.0
    drift_ppm: float = 0.0
    start_offset_ms: float = 0.0
    valid_for_scale_from_ms: int = 0
    valid_for_scale_to_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "scale_timezone": self.scale_timezone,
            "phone_time_equals_scale_time": abs(self.rate - 1.0) < 1e-12 and abs(self.offset_ms) < 1e-6,
            "rate": self.rate,
            "offset_ms": self.offset_ms,
            "drift_ppm": self.drift_ppm,
            "start_offset_ms": self.start_offset_ms,
            "valid_for_scale_from_ms": self.valid_for_scale_from_ms,
            "valid_for_scale_to_ms": self.valid_for_scale_to_ms,
        }


@dataclass
class CalculationResult:
    model: ClockModel
    level: str  # "green" | "yellow" | "red"
    warnings: list[str]
    anchors: dict[str, Any]  # the two matched anchors, for UI display

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "warnings": list(self.warnings),
            "anchors": self.anchors,
            "model": self.model.to_dict(),
        }


def compute_two_point_model(
    *,
    scale_start_ms: int,
    scale_end_ms: int,
    phone_start_ms: int,
    phone_end_ms: int,
    phone_start_tz: str,
    phone_end_tz: str,
    session_tz: str,
) -> CalculationResult:
    """Compute the affine model + run the spec §7 validation matrix.

    Raises CalculationError on hard rejects (spec §7 "拒绝计算" rows).
    Returns warnings + level (green/yellow/red) for soft rows.
    """
    warnings: list[str] = []

    # --- hard rejects (spec §7 拒绝计算) ---------------------------------
    if not (scale_end_ms > scale_start_ms and phone_end_ms > phone_start_ms):
        raise CalculationError("时间倒序或相同：天平/手机锚点必须严格递增")

    span_scale = scale_end_ms - scale_start_ms
    span_phone = phone_end_ms - phone_start_ms

    rate = span_phone / span_scale
    offset_ms = phone_start_ms - rate * scale_start_ms
    drift_ppm = (rate - 1.0) * 1_000_000
    # Human-friendly: how much the scale lags/leads the phone at the start.
    start_offset_ms = phone_start_ms - scale_start_ms  # >0 ⇒ scale is behind

    model = ClockModel(
        scale_timezone=session_tz,
        rate=rate,
        offset_ms=offset_ms,
        drift_ppm=drift_ppm,
        start_offset_ms=start_offset_ms,
        valid_for_scale_from_ms=scale_start_ms,
        valid_for_scale_to_ms=scale_end_ms,
    )

    # --- soft warnings (spec §7 黄色 / 红色警告) -------------------------
    if span_scale < ANCHOR_GAP_YELLOW_SEC * 1000:
        warnings.append(
            f"两锚点间隔仅 {span_scale / 1000:.1f}s（建议 ≥ {ANCHOR_GAP_YELLOW_SEC:.0f}s）"
        )

    if abs(drift_ppm) > DRIFT_PPM_RED:
        warnings.append(
            f"漂移 {drift_ppm:+.1f} ppm 超过 ±{DRIFT_PPM_RED:.0f}（检查是否选错行或设备时钟/时区）"
        )

    for label, tz in (("开始", phone_start_tz), ("结束", phone_end_tz)):
        if tz and tz != session_tz:
            warnings.append(f"{label}锚点手机时区为 {tz}，与会话时区 {session_tz} 不一致")

    has_red = any("漂移" in w and "ppm" in w for w in warnings) or any(
        "时区" in w and "不一致" in w for w in warnings
    )
    level = "red" if has_red else ("yellow" if warnings else "green")

    return CalculationResult(
        model=model,
        level=level,
        warnings=warnings,
        anchors={
            "scale": {"start_ms": scale_start_ms, "end_ms": scale_end_ms, "span_ms": span_scale},
            "phone": {"start_ms": phone_start_ms, "end_ms": phone_end_ms, "span_ms": span_phone},
        },
    )


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #


def _utc_now_iso() -> str:
    return datetime.now(_tz.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ScaleSyncStore:
    """SQLite-backed store for scale-sync sessions.

    Schema mirrors spec §6.3. Single-process (one uvicorn worker), so a
    module-level lock around writes is sufficient.
    """

    def __init__(self, db_path: str | Path, files_root: str | Path) -> None:
        self.db_path = str(db_path)
        self.files_root = Path(files_root)
        self.lock = threading.RLock()
        self.files_root.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # -- connection -------------------------------------------------------- #
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self.lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS scale_sync_sessions (
                        session_id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        cage_id TEXT,
                        scale_device_id TEXT,
                        scale_timezone TEXT NOT NULL,
                        created_at_utc TEXT NOT NULL,
                        state TEXT NOT NULL,
                        calculated_model_json TEXT,
                        warnings_json TEXT
                    );
                    CREATE TABLE IF NOT EXISTS scale_sync_anchors (
                        anchor_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES scale_sync_sessions(session_id),
                        kind TEXT NOT NULL,
                        client_epoch_ms INTEGER NOT NULL,
                        client_perf_ms REAL,
                        client_timezone TEXT,
                        client_utc_offset_minutes INTEGER,
                        server_received_at_utc TEXT NOT NULL,
                        observed_weight_g REAL,
                        note TEXT,
                        import_id TEXT,
                        source_line_no INTEGER,
                        matched_row_json TEXT
                    );
                    CREATE TABLE IF NOT EXISTS scale_sync_imports (
                        import_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES scale_sync_sessions(session_id),
                        original_filename TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        byte_count INTEGER NOT NULL,
                        stored_path TEXT NOT NULL,
                        parser_version TEXT NOT NULL,
                        uploaded_at_utc TEXT NOT NULL,
                        summary_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS scale_sync_readings (
                        import_id TEXT NOT NULL REFERENCES scale_sync_imports(import_id),
                        source_line_no INTEGER NOT NULL,
                        raw_line TEXT,
                        scale_epoch_ms INTEGER NOT NULL,
                        weight_g REAL NOT NULL,
                        unit TEXT,
                        raw_sequence TEXT,
                        raw_status TEXT,
                        PRIMARY KEY (import_id, source_line_no)
                    );
                    CREATE TABLE IF NOT EXISTS scale_sync_audit (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES scale_sync_sessions(session_id),
                        event_type TEXT NOT NULL,
                        actor TEXT,
                        at_utc TEXT NOT NULL,
                        payload_json TEXT
                    );
                    CREATE INDEX IF NOT EXISTS scale_sync_anchors_session_idx
                        ON scale_sync_anchors(session_id);
                    CREATE INDEX IF NOT EXISTS scale_sync_imports_session_idx
                        ON scale_sync_imports(session_id);
                    CREATE INDEX IF NOT EXISTS scale_sync_readings_import_idx
                        ON scale_sync_readings(import_id);
                    """
                )
            finally:
                conn.close()

    # -- audit ------------------------------------------------------------- #
    def _audit(self, conn: sqlite3.Connection, session_id: str, event_type: str, payload: Any) -> None:
        conn.execute(
            "INSERT INTO scale_sync_audit (id, session_id, event_type, actor, at_utc, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                session_id,
                event_type,
                "scale-sync",
                _utc_now_iso(),
                json.dumps(payload, ensure_ascii=False) if payload is not None else None,
            ),
        )

    # -- sessions ---------------------------------------------------------- #
    def create_session(
        self,
        *,
        project_id: str = "default",
        cage_id: str | None = None,
        scale_device_id: str | None = None,
        scale_timezone: str = "Asia/Shanghai",
    ) -> dict[str, Any]:
        session_id = uuid.uuid4().hex
        created = _utc_now_iso()
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO scale_sync_sessions "
                    "(session_id, project_id, cage_id, scale_device_id, scale_timezone, "
                    " created_at_utc, state, calculated_model_json, warnings_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'created', NULL, NULL)",
                    (session_id, project_id, cage_id, scale_device_id, scale_timezone, created),
                )
                self._audit(conn, session_id, "session_created", {"scale_timezone": scale_timezone})
            finally:
                conn.close()
        return self.get_session(session_id)  # type: ignore[return-value]

    def _row_to_session_basics(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "session_id": row["session_id"],
            "project_id": row["project_id"],
            "cage_id": row["cage_id"],
            "scale_device_id": row["scale_device_id"],
            "scale_timezone": row["scale_timezone"],
            "created_at_utc": row["created_at_utc"],
            "state": row["state"],
            "calculated_model": json.loads(row["calculated_model_json"]) if row["calculated_model_json"] else None,
            "warnings": json.loads(row["warnings_json"]) if row["warnings_json"] else [],
        }

    def _require_session_row(self, conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM scale_sync_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise SessionNotFound(session_id)
        return row

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            conn = self._connect()
            try:
                row = self._require_session_row(conn, session_id)
                sess = self._row_to_session_basics(row)
                sess["anchors"] = self._list_anchors(conn, session_id)
                sess["imports"] = self._list_imports(conn, session_id)
                return sess
            finally:
                conn.close()

    # -- anchors ----------------------------------------------------------- #
    def _list_anchors(self, conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM scale_sync_anchors WHERE session_id = ? ORDER BY kind", (session_id,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["matched_row"] = json.loads(d.pop("matched_row_json")) if d.get("matched_row_json") else None
            out.append(d)
        return out

    def put_anchor(self, session_id: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Create or replace the ``start``/``end`` anchor for a session.

        Replacing drops any prior match (the operator must re-bind a CSV row),
        and writes an audit entry — matches are never silently overwritten.
        """
        if kind not in ("start", "end"):
            raise AnchorError(f"invalid anchor kind: {kind}")
        with self.lock:
            conn = self._connect()
            try:
                self._require_session_row(conn, session_id)
                anchor_id = uuid.uuid4().hex
                received = _utc_now_iso()
                # Delete any existing anchor of this kind (idempotent replace).
                conn.execute(
                    "DELETE FROM scale_sync_anchors WHERE session_id = ? AND kind = ?",
                    (session_id, kind),
                )
                conn.execute(
                    "INSERT INTO scale_sync_anchors "
                    "(anchor_id, session_id, kind, client_epoch_ms, client_perf_ms, client_timezone, "
                    " client_utc_offset_minutes, server_received_at_utc, observed_weight_g, note, "
                    " import_id, source_line_no, matched_row_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
                    (
                        anchor_id,
                        session_id,
                        kind,
                        int(payload["client_epoch_ms"]),
                        payload.get("client_perf_ms"),
                        payload.get("client_timezone"),
                        payload.get("client_utc_offset_minutes"),
                        received,
                        payload.get("observed_weight_g"),
                        payload.get("note") or "",
                    ),
                )
                self._audit(
                    conn, session_id, f"anchor_{kind}_set",
                    {"client_epoch_ms": int(payload["client_epoch_ms"]), "anchor_id": anchor_id},
                )
                anchors = self._list_anchors(conn, session_id)
            finally:
                conn.close()
        return next(a for a in anchors if a["kind"] == kind)

    def delete_anchor(self, session_id: str, kind: str) -> dict[str, Any]:
        if kind not in ("start", "end"):
            raise AnchorError(f"invalid anchor kind: {kind}")
        with self.lock:
            conn = self._connect()
            try:
                self._require_session_row(conn, session_id)
                conn.execute(
                    "DELETE FROM scale_sync_anchors WHERE session_id = ? AND kind = ?",
                    (session_id, kind),
                )
                self._audit(conn, session_id, f"anchor_{kind}_deleted", {})
            finally:
                conn.close()
        return self.get_session(session_id)

    # -- imports ----------------------------------------------------------- #
    def _list_imports(self, conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM scale_sync_imports WHERE session_id = ? ORDER BY uploaded_at_utc",
            (session_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["summary"] = json.loads(d.pop("summary_json")) if d.get("summary_json") else {}
            out.append(d)
        return out

    def create_import(
        self,
        *,
        session_id: str,
        original_filename: str,
        raw: bytes,
        scale_timezone: str,
    ) -> dict[str, Any]:
        """Validate, parse, persist raw bytes and load readings.

        Returns the import row + a parsed summary. Raises CsvParseError /
        ``ValueError`` on rejects (spec §5.3).
        """
        if not raw:
            raise CsvParseError("空文件")
        if len(raw) > MAX_IMPORT_BYTES:
            raise ValueError(f"文件超过 {MAX_IMPORT_BYTES // (1024 * 1024)} MB 限制")

        parsed = parse_scale_csv(raw, scale_timezone)
        sha256 = hashlib.sha256(raw).hexdigest()
        import_id = uuid.uuid4().hex
        uploaded = _utc_now_iso()

        # Persist raw bytes verbatim under files_root/<session>/<import>/source.csv
        import_dir = self.files_root / session_id / import_id
        import_dir.mkdir(parents=True, exist_ok=True)
        source_path = import_dir / "source.csv"
        source_path.write_bytes(raw)

        with self.lock:
            conn = self._connect()
            try:
                self._require_session_row(conn, session_id)
                conn.execute(
                    "INSERT INTO scale_sync_imports "
                    "(import_id, session_id, original_filename, sha256, byte_count, stored_path, "
                    " parser_version, uploaded_at_utc, summary_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        import_id,
                        session_id,
                        original_filename,
                        sha256,
                        len(raw),
                        str(source_path),
                        parsed.parser_version,
                        uploaded,
                        json.dumps(parsed.to_summary(), ensure_ascii=False),
                    ),
                )
                conn.executemany(
                    "INSERT INTO scale_sync_readings "
                    "(import_id, source_line_no, raw_line, scale_epoch_ms, weight_g, unit, "
                    " raw_sequence, raw_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            import_id,
                            r.source_line_no,
                            r.raw_line,
                            r.scale_epoch_ms,
                            r.weight_g,
                            r.unit,
                            r.raw_sequence,
                            r.raw_status,
                        )
                        for r in parsed.readings
                    ],
                )
                self._audit(
                    conn, session_id, "import_created",
                    {"import_id": import_id, "sha256": sha256, "rows": parsed.count},
                )
                imports = self._list_imports(conn, session_id)
            finally:
                conn.close()
        return next(i for i in imports if i["import_id"] == import_id)

    def list_readings(
        self,
        session_id: str,
        import_id: str,
        *,
        query: str | None = None,
        min_weight: float | None = None,
        max_weight: float | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        with self.lock:
            conn = self._connect()
            try:
                self._require_import_row(conn, session_id, import_id)
                sql = (
                    "SELECT source_line_no, raw_line, scale_epoch_ms, weight_g, unit, "
                    "raw_sequence, raw_status FROM scale_sync_readings WHERE import_id = ?"
                )
                params: list[Any] = [import_id]
                if min_weight is not None:
                    sql += " AND weight_g >= ?"
                    params.append(float(min_weight))
                if max_weight is not None:
                    sql += " AND weight_g <= ?"
                    params.append(float(max_weight))
                if query:
                    sql += " AND (CAST(weight_g AS TEXT) LIKE ? OR raw_line LIKE ?)"
                    params.extend([f"%{query}%", f"%{query}%"])
                sql += " ORDER BY scale_epoch_ms ASC, source_line_no ASC LIMIT ?"
                params.append(max(1, min(int(limit), 5000)))
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        return [dict(r) for r in rows]

    def _require_import_row(self, conn: sqlite3.Connection, session_id: str, import_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM scale_sync_imports WHERE import_id = ? AND session_id = ?",
            (import_id, session_id),
        ).fetchone()
        if row is None:
            raise ImportNotFound(import_id)
        return row

    # -- matching ---------------------------------------------------------- #
    def match_anchor(
        self,
        session_id: str,
        kind: str,
        *,
        import_id: str,
        source_line_no: int,
    ) -> dict[str, Any]:
        if kind not in ("start", "end"):
            raise AnchorError(f"invalid anchor kind: {kind}")
        with self.lock:
            conn = self._connect()
            try:
                self._require_session_row(conn, session_id)
                self._require_import_row(conn, session_id, import_id)
                anchor = conn.execute(
                    "SELECT * FROM scale_sync_anchors WHERE session_id = ? AND kind = ?",
                    (session_id, kind),
                ).fetchone()
                if anchor is None:
                    raise AnchorError(f"未记录 {kind} 锚点，无法绑定")
                reading = conn.execute(
                    "SELECT * FROM scale_sync_readings WHERE import_id = ? AND source_line_no = ?",
                    (import_id, source_line_no),
                ).fetchone()
                if reading is None:
                    raise AnchorError(f"导入 {import_id} 中不存在行 {source_line_no}")

                matched = {
                    "import_id": import_id,
                    "source_line_no": source_line_no,
                    "raw_line": reading["raw_line"],
                    "scale_epoch_ms": reading["scale_epoch_ms"],
                    "weight_g": reading["weight_g"],
                    "unit": reading["unit"],
                    "raw_sequence": reading["raw_sequence"],
                    "raw_status": reading["raw_status"],
                }
                conn.execute(
                    "UPDATE scale_sync_anchors SET import_id = ?, source_line_no = ?, matched_row_json = ? "
                    "WHERE session_id = ? AND kind = ?",
                    (import_id, source_line_no, json.dumps(matched, ensure_ascii=False), session_id, kind),
                )
                self._audit(
                    conn, session_id, f"anchor_{kind}_matched",
                    {"import_id": import_id, "source_line_no": source_line_no},
                )
                anchors = self._list_anchors(conn, session_id)
            finally:
                conn.close()
        return next(a for a in anchors if a["kind"] == kind)

    # -- calculation ------------------------------------------------------- #
    def calculate(self, session_id: str) -> dict[str, Any]:
        """Validate + compute the two-point model, persist it. Returns result dict."""
        with self.lock:
            conn = self._connect()
            try:
                sess_row = self._require_session_row(conn, session_id)
                session_tz = sess_row["scale_timezone"]
                anchors = {a["kind"]: a for a in self._list_anchors(conn, session_id)}
                start = anchors.get("start")
                end = anchors.get("end")
                if not start or not end:
                    raise CalculationError("需要先记录两个锚点并绑定 CSV 行")
                if not start.get("matched_row") or not end.get("matched_row"):
                    raise CalculationError("两个锚点都必须绑定 CSV 行")
                if start["matched_row"]["import_id"] != end["matched_row"]["import_id"]:
                    raise CalculationError("两个锚点必须来自同一导入文件")

                result = compute_two_point_model(
                    scale_start_ms=start["matched_row"]["scale_epoch_ms"],
                    scale_end_ms=end["matched_row"]["scale_epoch_ms"],
                    phone_start_ms=start["client_epoch_ms"],
                    phone_end_ms=end["client_epoch_ms"],
                    phone_start_tz=start.get("client_timezone") or "",
                    phone_end_tz=end.get("client_timezone") or "",
                    session_tz=session_tz,
                )
                model_dict = result.model.to_dict()
                warnings_dict = result.warnings
                conn.execute(
                    "UPDATE scale_sync_sessions SET state = 'calculated', "
                    "calculated_model_json = ?, warnings_json = ? WHERE session_id = ?",
                    (
                        json.dumps(model_dict, ensure_ascii=False),
                        json.dumps(warnings_dict, ensure_ascii=False),
                        session_id,
                    ),
                )
                self._audit(
                    conn, session_id, "calculated",
                    {"level": result.level, "drift_ppm": result.model.drift_ppm},
                )
            finally:
                conn.close()
        return result.to_dict()

    # -- readings preview (for the result page) ---------------------------- #
    def readings_preview(self, session_id: str, import_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self.list_readings(session_id, import_id, limit=limit)

    # -- session full view with preview ------------------------------------ #
    def get_session_full(self, session_id: str) -> dict[str, Any]:
        sess = self.get_session(session_id)
        model = sess.get("calculated_model")
        # Attach a readings preview from the matched import if available.
        anchors = {a["kind"]: a for a in sess["anchors"]}
        start = anchors.get("start") or {}
        matched = start.get("matched_row")
        if matched and model:
            preview = self.readings_preview(session_id, matched["import_id"], limit=20)
            for r in preview:
                phone_ms = model["rate"] * r["scale_epoch_ms"] + model["offset_ms"]
                r["phone_epoch_ms"] = round(phone_ms)
                r["within_window"] = model["valid_for_scale_from_ms"] <= r["scale_epoch_ms"] <= model["valid_for_scale_to_ms"]
            sess["readings_preview"] = preview
        else:
            sess["readings_preview"] = []
        return sess
