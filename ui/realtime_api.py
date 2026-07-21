"""Realtime weighing WebSocket + REST API.

Exposes a phone-friendly live weighing session on top of
:class:`mousevision.realtime.RealtimeSession`. The phone client opens a
WebSocket, streams JPEG frames, and receives state updates + announcement
events. REST endpoints cover session lifecycle (create / status / retry /
accept / finish) for clients that cannot keep a socket open.

Router wiring
-------------
``ui/app.py`` must tell this module where the YAML config lives before the
router is mounted. Two equivalent ways:

    # Option A — module-level (matches the rest of the UI package style):
    import ui.realtime_api as realtime_api
    realtime_api.configure(DEFAULT_CONFIG)
    app.include_router(realtime_api.router)

    # Option B — factory (cleaner for tests):
    app.include_router(realtime_api.create_realtime_router(DEFAULT_CONFIG))

Binary frame protocol (v1)
--------------------------
Each WebSocket binary message is::

    [4 bytes frame_seq     LE uint32]
    [4 bytes client_ts_ms  LE uint32]   # ms since recording start
    [N bytes JPEG]                       # the rest of the payload

Text messages are JSON commands: ``{"type": "retry"}`` / ``{"type": "accept"}``.

Server replies are always JSON text "state" messages (see ``_state_payload``).

Thread-safety
-------------
* The session store is guarded by :data:`_sessions_lock`.
* Each :class:`RealtimeSession` has its own internal lock, so a socket task
  streaming frames and a REST task calling ``accept`` can race safely.
* OCR work (``reader.read_weight``) is blocking, so it is dispatched to a
  worker thread via :func:`starlette.concurrency.run_in_threadpool` to avoid
  stalling the asyncio event loop.
"""

from __future__ import annotations

import json
import logging
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from mousevision.fusion.temporal import TemporalFusionConfig, TemporalWeightFusion
from mousevision.pipeline import load_config
from mousevision.reader.template import TemplateReader
from mousevision.realtime import (
    Attempt,
    QualityHint,
    RealtimeConfig,
    RealtimeFrameResult,
    RealtimeSession,
    RealtimeState,
)
from mousevision.realtime_journal import AttemptJournal, JournalMeta, journal_path
from mousevision.realtime_finalize import finalize_session
from ui.auth import require_api_token

log = logging.getLogger("realtime_api")

# --------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------- #

SESSION_TIMEOUT_S = 300  # 5 minutes without frames -> auto-cleanup
_HEADER_STRUCT = struct.Struct("<II")  # frame_seq, client_ts_ms (8 bytes)
_HEADER_SIZE = _HEADER_STRUCT.size


# --------------------------------------------------------------------- #
# Session store
# --------------------------------------------------------------------- #


@dataclass
class ActiveSession:
    """A live realtime session owned by a phone client.

    ``engine`` carries its own internal lock, so the only state here that
    needs :data:`_sessions_lock` is the dict membership itself plus
    ``last_frame_at`` (updated by the WS loop, read by the reaper).
    """

    session_id: str
    cage_id: str
    project_id: str
    engine: RealtimeSession
    journal: AttemptJournal
    output_root: str
    created_at: float
    last_frame_at: float = 0.0
    recording_t0_ms: float = 0.0  # client's recording start time (future use)
    device_id: str = "scale01"
    # Serializes frame processing for this session across socket reconnects
    # or concurrent REST probes that feed frames.
    frame_lock: threading.Lock = field(default_factory=threading.Lock)
    # Performance telemetry buffers (capped). Server timing per frame, and
    # client-reported timing samples (encode/RTT). Summarized at finish.
    server_timing_samples: list = field(default_factory=list)
    client_timing_samples: list = field(default_factory=list)
    # Untruncated totals so the finish summary can report real coverage even
    # when the rolling buffer (which only holds the tail) has evicted early
    # frames. Calibation / first-connect / early jitter frames otherwise get
    # silently dropped from P50/P95.
    server_timing_total: int = 0
    client_timing_total: int = 0


# Global session store (single uvicorn worker -> dict is safe under _sessions_lock).
_sessions: dict[str, ActiveSession] = {}
_sessions_lock = threading.Lock()


# --------------------------------------------------------------------- #
# Config plumbing
# --------------------------------------------------------------------- #

# Set by configure() / create_realtime_router() before the router is mounted.
_config_path: str | Path | None = None
_config_cache: dict[str, Any] | None = None
_output_root: str | Path | None = None
_config_lock = threading.Lock()


def configure(config_path: str | Path, output_root: str | Path | None = None) -> None:
    """Set the YAML config path (and optionally the output root) used by the
    module-level :data:`router`.

    Must be called once at startup, before the router handles requests.
    ``output_root`` defaults to ``MOUSEVISION_OUTPUT_DIR`` env or ``./output``.
    """
    global _config_path, _output_root
    with _config_lock:
        _config_path = str(config_path)
        if output_root is None:
            import os as _os

            output_root = _os.getenv("MOUSEVISION_OUTPUT_DIR", "output")
        _output_root = str(output_root)
        # Invalidate cache so the next request reloads from the new path.
        _invalidate_config_locked()


def _get_output_root() -> str:
    with _config_lock:
        if not _output_root:
            # Fall back to env if configure() was not given an explicit root.
            import os as _os

            return _os.getenv("MOUSEVISION_OUTPUT_DIR", "output")
        return _output_root


_upload_queue: Any | None = None
_upload_queue_lock = threading.Lock()


def set_upload_queue(queue: Any) -> None:
    """Inject the app's shared :class:`~mousevision.upload_queue.UploadQueue`.

    Optional: when not set, finalize still writes records but does not enqueue
    them for cloud sync.
    """
    global _upload_queue
    with _upload_queue_lock:
        _upload_queue = queue


def _get_upload_queue() -> Any:
    with _upload_queue_lock:
        return _upload_queue


def _invalidate_config_locked() -> None:
    """Clear the cached parsed config. Caller holds :data:`_config_lock`."""
    global _config_cache
    _config_cache = None


def _get_config() -> dict[str, Any]:
    """Return the parsed config dict, loading + caching on first use."""
    global _config_cache
    with _config_lock:
        if _config_cache is not None:
            return _config_cache
        if not _config_path:
            raise RuntimeError(
                "realtime_api.configure(path) was not called before serving "
                "requests; pass the config path to create_realtime_router() "
                "or call configure() at startup."
            )
        try:
            _config_cache = load_config(_config_path)
        except FileNotFoundError as exc:
            log.error("realtime config not found at %s: %s", _config_path, exc)
            raise RuntimeError(f"config file not found: {_config_path}") from exc
        return _config_cache


# --------------------------------------------------------------------- #
# Engine construction
# --------------------------------------------------------------------- #


def _build_realtime_config(config: dict[str, Any]) -> RealtimeConfig:
    """Load the dedicated ``realtime:`` YAML section with safe fallbacks.

    Falls back to top-level weight thresholds when a field is absent, so
    older configs keep working. Raises ``ValueError`` on out-of-range knobs
    (surfaced as HTTP 500 at session create time).
    """
    rt = config.get("realtime") or {}
    if not isinstance(rt, dict):
        rt = {}

    def _f(key: str, default: float, *fallbacks: str) -> float:
        if key in rt:
            return float(rt[key])
        for fb in fallbacks:
            if fb in config:
                return float(config[fb])
        return float(default)

    def _i(key: str, default: int, *fallbacks: str) -> int:
        if key in rt:
            return int(rt[key])
        for fb in fallbacks:
            if fb in config:
                return int(config[fb])
        return int(default)

    def _b(key: str, default: bool) -> bool:
        if key in rt:
            return bool(rt[key])
        return bool(default)

    return RealtimeConfig(
        calibrate_min_frames=_i("calibrate_min_frames", 5),
        enter_min=_f("enter_min", 1.0, "enter_min"),
        empty_max=_f("empty_max", 0.15, "empty_max"),
        leave_max=_f("leave_max", 0.30, "leave_max"),
        enter_sustain_frames=_i("enter_sustain_frames", 2, "enter_sustain_frames"),
        stable_min_frames=_i("stable_min_frames", 4),
        stable_min_raw_reads=_i("stable_min_raw_reads", 3),
        stable_confirm_raw_reads=_i("stable_confirm_raw_reads", 1),
        stable_min_span_ms=_f("stable_min_span_ms", 0.0),
        stable_max_age_s=_f("stable_max_age_s", 1.6),
        stable_weight_tol=_f("stable_weight_tol", 0.10),
        min_confidence=_f("min_confidence", 0.50),
        min_brightness=_f("min_brightness", 30.0),
        max_glare_ratio=_f("max_glare_ratio", 0.15),
        mouse_smooth_window=_i("mouse_smooth_window", 5),
        mouse_advisory=_b("mouse_advisory", True),
        frame_seq_dedupe=_b("frame_seq_dedupe", True),
        announce_hold_s=_f("announce_hold_s", 0.0),
        clear_timeout_s=_f("clear_timeout_s", 30.0),
    )


# Profiles the phone client is allowed to select from (must match the
# ENCODE_PROFILES map in mobile.js).
_VALID_ENCODE_PROFILES = ("high", "medium", "low")


def _build_client_config(config: dict[str, Any]) -> dict[str, Any]:
    """Build the client_config payload sent on session create.

    These knobs exist in the ``realtime:`` YAML section but were previously
    hard-coded in the client. The client clamps values to safe ranges before
    use, so this only needs to forward the configured (or default) values.
    """
    rt = config.get("realtime") or {}
    if not isinstance(rt, dict):
        rt = {}
    profile = str(rt.get("encode_profile", "high"))
    if profile not in _VALID_ENCODE_PROFILES:
        profile = "high"
    return {
        "max_fps": int(rt.get("max_fps", 5)),
        "frame_ack_timeout_ms": int(rt.get("frame_ack_timeout_ms", 3000)),
        "encode_profile": profile,
    }


def _create_engine(config: dict[str, Any]) -> RealtimeSession:
    """Build a fresh :class:`RealtimeSession` from a parsed config dict.

    Each session gets its own reader + fusion instance — they are stateful
    (fusion maintains a sliding window) and must not be shared across
    concurrent sessions.
    """
    templates_dir = config.get("templates_dir", "assets/templates")
    reader = TemplateReader(
        templates_dir,
        match_threshold=float(config.get("match_threshold", 0.50)),
        min_digit_confidence=float(config.get("min_digit_confidence", 0.45)),
        lcd_detect=config.get("lcd_detect"),
        weight_roi=config.get("weight_roi"),
        expected_digits=config.get("expected_digits", [3, 4]),
    )

    tc = config.get("temporal", {}) or {}
    fusion = TemporalWeightFusion(
        TemporalFusionConfig(
            window_size=int(tc.get("window_size", 8)),
            min_agree=int(tc.get("min_agree", 3)),
            weight_tol=float(tc.get("weight_tol", 0.08)),
            min_confidence=float(tc.get("min_confidence", 0.45)),
            stick_tol=float(tc.get("stick_tol", 0.08)),
            zero_hold_max_frames=int(tc.get("zero_hold_max_frames", 4)),
        )
    )

    rt_config = _build_realtime_config(config)
    mouse_cfg = config.get("mouse_detect", {}) or {}
    return RealtimeSession(rt_config, reader, fusion, mouse_detect_config=mouse_cfg)


# --------------------------------------------------------------------- #
# JPEG decoding
# --------------------------------------------------------------------- #


def _decode_jpeg(data: bytes) -> np.ndarray | None:
    """Decode JPEG bytes into a BGR uint8 ndarray, or ``None`` on failure."""
    if not data:
        return None
    try:
        arr = np.frombuffer(data, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return img  # BGR or None
    except Exception:  # noqa: BLE001 - corrupt JPEG / OOM, log + skip
        log.warning("JPEG decode failed (%d bytes)", len(data), exc_info=True)
        return None


# Canonical realtime frame size. The server's LCD / mouse-detection thresholds
# (lcd_detect.min_area, mouse_detect.min_area/max_area) are calibrated against
# 720×1280; any client encode profile (high/medium/low) is normalized back to
# this size before reaching the engine, so a smaller medium/low frame does not
# silently fall under the detection thresholds.
CANONICAL_FRAME_W = 720
CANONICAL_FRAME_H = 1280


def _normalize_to_canonical(image: np.ndarray) -> tuple[np.ndarray, bool]:
    """Resize an image to the canonical frame size if it differs.

    Returns ``(image, resized)``. ``resized`` is True when a copy was made.
    Detection algorithms always see 720×1280 regardless of client profile.
    """
    if image.ndim != 3:
        return image, False
    h, w = int(image.shape[0]), int(image.shape[1])
    if w == CANONICAL_FRAME_W and h == CANONICAL_FRAME_H:
        return image, False
    resized = cv2.resize(
        image, (CANONICAL_FRAME_W, CANONICAL_FRAME_H), interpolation=cv2.INTER_AREA
    )
    return resized, True


# Cap on buffered timing samples (per session, per side) so a long session
# cannot grow memory unbounded. 1200 frames at 5fps ≈ 4 minutes covers a full
# multi-mouse session; the untruncated *_total counters still record the true
# frame count when the rolling buffer eventually evicts the oldest samples.
_TIMING_SAMPLE_CAP = 1200
# Hard ceiling on the size of a single client_timing batch. A well-behaved
# client flushes ~10 samples per batch; anything larger is treated as junk.
_CLIENT_TIMING_BATCH_CAP = 50
# Plausible upper bounds for client-reported fields, used to reject garbage.
_CLIENT_TIMING_MAX_MS = 60_000.0      # 60s — encode or RTT above this is noise
_CLIENT_TIMING_MAX_BYTES = 5_000_000  # 5MB JPEG — anything larger is invalid


def _is_finite_number(value: Any) -> bool:
    """True only for ints/floats that are finite (not NaN / not Inf)."""
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    try:
        import math as _math
        return _math.isfinite(value)
    except (TypeError, ValueError):
        return False


def _clean_client_sample(raw: Any) -> dict[str, Any] | None:
    """Validate and normalize one client timing sample.

    Returns a clean dict, or ``None`` if the sample is malformed / out of
    range. Only the known fields are retained; unknown keys are dropped.
    """
    if not isinstance(raw, dict):
        return None
    frame_seq = raw.get("frame_seq")
    encode_ms = raw.get("encode_ms")
    rtt_ms = raw.get("rtt_ms")
    jpeg_bytes = raw.get("jpeg_bytes")

    # frame_seq is optional but if present must be a non-negative int.
    if frame_seq is not None:
        if isinstance(frame_seq, bool) or not isinstance(frame_seq, int) or frame_seq < 0:
            return None

    # Durations: must be finite, non-negative, below the noise ceiling.
    for v in (encode_ms, rtt_ms):
        if v is None:
            continue
        if not _is_finite_number(v) or v < 0 or v > _CLIENT_TIMING_MAX_MS:
            return None

    # jpeg_bytes: must be a non-negative int below the validity ceiling.
    if jpeg_bytes is not None:
        if isinstance(jpeg_bytes, bool) or not isinstance(jpeg_bytes, int) or jpeg_bytes < 0:
            return None
        if jpeg_bytes > _CLIENT_TIMING_MAX_BYTES:
            return None

    cleaned: dict[str, Any] = {}
    if frame_seq is not None:
        cleaned["frame_seq"] = int(frame_seq)
    if encode_ms is not None:
        cleaned["encode_ms"] = float(encode_ms)
    if rtt_ms is not None:
        cleaned["rtt_ms"] = float(rtt_ms)
    if jpeg_bytes is not None:
        cleaned["jpeg_bytes"] = int(jpeg_bytes)
    return cleaned or None


def _record_server_timing(session: "ActiveSession", timing: dict[str, Any]) -> None:
    """Append a server-side timing snapshot to the session buffer (capped)."""
    sample = {
        "frame_seq": timing.get("frame_seq"),
        "server_preprocess_wait_ms": timing.get("server_preprocess_wait_ms"),
        "decode_ms": timing.get("decode_ms"),
        "engine_ms": timing.get("engine_ms"),
        "total_ms": timing.get("total_ms"),
        "jpeg_bytes": timing.get("jpeg_bytes"),
        "resized": timing.get("resized"),
    }
    session.server_timing_samples.append(sample)
    session.server_timing_total += 1
    if len(session.server_timing_samples) > _TIMING_SAMPLE_CAP:
        del session.server_timing_samples[: len(session.server_timing_samples) - _TIMING_SAMPLE_CAP]


def _record_client_timing(session: "ActiveSession", samples: list[Any]) -> None:
    """Merge client-reported timing samples into the session buffer (capped).

    Each sample is validated: only dict samples with finite, in-range numeric
    fields are kept, and a batch is capped to reject junk floods. Invalid
    timing must NEVER affect the weighing records' durability.
    """
    if not samples:
        return
    cleaned: list[dict[str, Any]] = []
    for raw in samples[:_CLIENT_TIMING_BATCH_CAP]:
        c = _clean_client_sample(raw)
        if c is not None:
            cleaned.append(c)
    if not cleaned:
        return
    session.client_timing_samples.extend(cleaned)
    session.client_timing_total += len(cleaned)
    if len(session.client_timing_samples) > _TIMING_SAMPLE_CAP:
        del session.client_timing_samples[: len(session.client_timing_samples) - _TIMING_SAMPLE_CAP]


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile over an already-sorted list."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = rank - lo
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac)


def _summarize_samples(samples: list[Any], keys: list[str]) -> dict[str, Any]:
    """Build a {key: {p50, p95, n}} summary over a list of timing dicts.

    Per-sample extraction is defensive: any element that is not a dict, or
    whose value for ``key`` is missing / non-finite, is skipped — never
    raised — so a single malformed sample cannot break the finish summary.
    """
    summary: dict[str, Any] = {}
    for key in keys:
        values: list[float] = []
        for s in samples:
            if not isinstance(s, dict):
                continue
            v = s.get(key)
            if not _is_finite_number(v):
                continue
            values.append(float(v))
        if not values:
            continue
        values.sort()
        summary[key] = {
            "p50": round(_percentile(values, 50), 2),
            "p95": round(_percentile(values, 95), 2),
            "n": len(values),
        }
    return summary


def _build_session_timing_summary(session: "ActiveSession") -> dict[str, Any]:
    """Aggregate server + client timing samples into a single summary.

    ``samples_retained`` is the size of the rolling buffer used for P50/P95;
    ``*_total`` is the untruncated count of frames/samples observed across the
    whole session. When ``samples_retained < *_total`` the percentiles
    describe only the retained tail — callers must not treat them as the full
    session distribution.
    """
    return {
        "server": _summarize_samples(
            session.server_timing_samples,
            ["server_preprocess_wait_ms", "decode_ms", "engine_ms", "total_ms"],
        ),
        "jpeg_bytes": _summarize_samples(
            session.server_timing_samples, ["jpeg_bytes"]
        ),
        "client": _summarize_samples(
            session.client_timing_samples,
            ["encode_ms", "rtt_ms", "jpeg_bytes"],
        ),
        # Untruncated totals — the source of truth for session coverage.
        "frames_processed": session.server_timing_total,
        "client_samples": session.client_timing_total,
        # Rolling-buffer sizes actually used for the percentiles above.
        "samples_retained": {
            "server": len(session.server_timing_samples),
            "client": len(session.client_timing_samples),
        },
    }


# --------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------- #


def _attempt_to_dict(attempt: Attempt | None) -> dict[str, Any] | None:
    if attempt is None:
        return None
    return {
        "attempt_id": attempt.attempt_id,
        "weight_g": attempt.weight_g,
        "confidence": round(float(attempt.confidence), 4),
        "frame_seq": attempt.frame_seq,
        "client_ts_ms": attempt.client_ts_ms,
        "state": attempt.state,
        "created_at": attempt.created_at,
    }


def _hint_to_dict(hint: QualityHint) -> dict[str, str]:
    return {"code": hint.code, "message": hint.message}


def _state_payload(
    result: RealtimeFrameResult,
    *,
    accepted_weight: float | None,
    timing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the JSON "state" message sent to the client."""
    payload: dict[str, Any] = {
        "type": "state",
        "state": result.state.value,
        "weight_candidate": (
            round(float(result.weight_candidate), 2)
            if result.weight_candidate is not None
            else None
        ),
        "confidence": round(float(result.confidence), 4),
        "mouse_present": bool(result.mouse_present),
        "quality_hints": [_hint_to_dict(h) for h in result.quality_hints],
        "attempt": _attempt_to_dict(result.attempt),
        "accepted_weight": (
            round(float(accepted_weight), 2)
            if accepted_weight is not None
            else None
        ),
        "frame_seq": result.frame_seq,
        "epoch": int(getattr(result, "epoch", 0) or 0),
    }
    if timing:
        payload["timing"] = timing
    return payload


def _status_payload(session: ActiveSession) -> dict[str, Any]:
    """Snapshot for GET /status and the WS hello message."""
    engine = session.engine
    # Read accepted list via the engine's own lock (returns a copy).
    accepted = engine.get_accepted_records()
    attempts = engine.get_all_attempts()
    return {
        "session_id": session.session_id,
        "cage_id": session.cage_id,
        "project_id": session.project_id,
        "state": engine.state.value,
        "created_at": session.created_at,
        "last_frame_at": session.last_frame_at,
        "accepted_count": len(accepted),
        "attempts": [_attempt_to_dict(a) for a in attempts],
        "accepted": [_attempt_to_dict(a) for a in accepted],
    }


# --------------------------------------------------------------------- #
# Store helpers
# --------------------------------------------------------------------- #


def _cleanup_expired_locked(now: float | None = None) -> int:
    """Remove sessions idle longer than :data:`SESSION_TIMEOUT_S`.

    Caller must hold :data:`_sessions_lock`. Returns the number removed.
    Only called from inside the lock to keep the reaper cheap.
    """
    if now is None:
        now = time.time()
    deadline = now - SESSION_TIMEOUT_S
    expired = [
        sid
        for sid, s in _sessions.items()
        # A session that never received a frame is timed out from created_at.
        if (s.last_frame_at or s.created_at) < deadline
    ]
    for sid in expired:
        _sessions.pop(sid, None)
    if expired:
        log.info("realtime: reaped %d expired session(s)", len(expired))
    return len(expired)


def _get_session(session_id: str) -> ActiveSession:
    """Fetch a session by id, running expiry first. Raises 404 if absent."""
    with _sessions_lock:
        _cleanup_expired_locked()
        session = _sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="realtime session not found")
    return session


def _touch(session: ActiveSession) -> None:
    """Mark a session as freshly active (frame received)."""
    with _sessions_lock:
        session.last_frame_at = time.time()


# --------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------- #


class CreateSessionRequest(BaseModel):
    cage_id: str = Field(..., min_length=1, max_length=64)
    project_id: str = Field("default", max_length=64)


class CreateSessionResponse(BaseModel):
    session_id: str
    state: str
    # Tuning knobs the phone client should apply (P2). The client clamps
    # these to safe ranges before use; absent fields fall back to client
    # defaults so older clients keep working.
    client_config: dict[str, Any] | None = None


# --------------------------------------------------------------------- #
# REST endpoints
# --------------------------------------------------------------------- #


def create_realtime_router(
    config_path: str | Path, output_root: str | Path | None = None
) -> APIRouter:
    """Build a fresh router bound to ``config_path``.

    Preferred for tests / multi-app setups. For the default single-app case,
    call :func:`configure` and use the module-level :data:`router`.
    """
    configure(config_path, output_root)
    return router


router = APIRouter(prefix="/api/realtime", tags=["realtime"])


def _build_journal(session_id: str, cage_id: str, project_id: str, device_id: str) -> AttemptJournal:
    """Create an append-only journal for this session and write its meta."""
    out_root = _get_output_root()
    jpath = journal_path(out_root, session_id)
    j = AttemptJournal(jpath)
    j.write_meta(
        JournalMeta(
            session_id=session_id,
            cage_id=cage_id,
            project_id=project_id,
            created_at=time.time(),
            device_id=device_id,
        )
    )
    return j


@router.post(
    "/session",
    response_model=CreateSessionResponse,
    dependencies=[Depends(require_api_token)],
)
def create_session(req: CreateSessionRequest) -> CreateSessionResponse:
    """Create a new realtime weighing session.

    Returns the new ``session_id`` (the phone then opens the WebSocket with
    ``?session_id=...``) and the initial state (always ``calibrating``).
    """
    config = _get_config()
    try:
        engine = _create_engine(config)
    except ValueError as exc:
        log.error("realtime: invalid realtime config: %s", exc)
        raise HTTPException(status_code=400, detail=f"invalid realtime config: {exc}") from exc
    except Exception:  # noqa: BLE001 - surface as 500 with context, never crash the app
        log.exception("realtime: failed to build engine from config")
        raise HTTPException(status_code=500, detail="failed to initialize session")

    session_id = uuid.uuid4().hex
    now = time.time()
    device_id = str(config.get("device_id", "scale01"))
    journal = _build_journal(session_id, req.cage_id, req.project_id, device_id)
    session = ActiveSession(
        session_id=session_id,
        cage_id=req.cage_id,
        project_id=req.project_id,
        engine=engine,
        journal=journal,
        output_root=_get_output_root(),
        created_at=now,
        last_frame_at=0.0,
        device_id=device_id,
    )
    with _sessions_lock:
        _cleanup_expired_locked(now)
        _sessions[session_id] = session
    log.info(
        "realtime: created session %s (cage=%s project=%s)",
        session_id, req.cage_id, req.project_id,
    )
    return CreateSessionResponse(
        session_id=session_id,
        state=engine.state.value,
        client_config=_build_client_config(config),
    )


@router.get(
    "/session/{session_id}/status",
    dependencies=[Depends(require_api_token)],
)
def session_status(session_id: str) -> dict[str, Any]:
    """Poll the current session state (fallback when WS is disconnected)."""
    return _status_payload(_get_session(session_id))


@router.post(
    "/session/{session_id}/retry",
    dependencies=[Depends(require_api_token)],
)
def session_retry(session_id: str) -> dict[str, Any]:
    """Request a re-weigh of the currently announced attempt."""
    session = _get_session(session_id)
    # Snapshot the in-flight attempt so its rejection is durable BEFORE the
    # engine transitions out of ANNOUNCED.
    try:
        cur = session.engine._current_attempt  # type: ignore[attr-defined]
        if cur is not None:
            session.journal.record_decision(cur.attempt_id, "rejected", cur.weight_g)
    except Exception:  # noqa: BLE001
        log.exception("realtime: journal record_decision(retry) failed")
    retry_info = session.engine.request_retry()
    return {
        "session_id": session_id,
        "state": retry_info.get("state", session.engine.state.value),
        "ok": bool(retry_info.get("applied")),
        "applied": bool(retry_info.get("applied")),
        "epoch": int(retry_info.get("epoch", 0)),
    }


@router.post(
    "/session/{session_id}/accept",
    dependencies=[Depends(require_api_token)],
)
def session_accept(session_id: str) -> dict[str, Any]:
    """Accept the currently announced weight and return the attempt."""
    session = _get_session(session_id)
    attempt = session.engine.accept_weight()
    if attempt is None:
        # Not in ANNOUNCED state (nothing to accept, or already accepted).
        raise HTTPException(
            status_code=409,
            detail="no announced weight to accept (state=%s)" % session.engine.state.value,
        )
    try:
        session.journal.record_decision(attempt.attempt_id, "accepted", attempt.weight_g)
    except Exception:  # noqa: BLE001
        log.exception("realtime: journal record_decision(accept) failed")
    return {
        "session_id": session_id,
        "state": session.engine.state.value,
        "accepted": _attempt_to_dict(attempt),
    }


class FinishRequest(BaseModel):
    """Optional body for /finish: lets the client pass the uploaded video job id
    so finalize can link the run dir to the source video."""

    video_upload_job_id: str | None = None
    capture_meta: dict[str, Any] | None = None


@router.post(
    "/session/{session_id}/finish",
    dependencies=[Depends(require_api_token)],
)
def session_finish(
    session_id: str, req: FinishRequest | None = Body(None)
) -> dict[str, Any]:
    """End the session, persist accepted attempts as durable records, and
    return a summary.

    Accepted attempts become the official records (one ``mouse_NNN`` per
    attempt under a new run dir). Rejected attempts are written to the run
    manifest so the offline pipeline can skip them. The in-memory session is
    then removed; the journal file remains for audit.
    """
    with _sessions_lock:
        _cleanup_expired_locked()
        session = _sessions.pop(session_id, None)
    if session is None:
        raise HTTPException(status_code=404, detail="realtime session not found")

    engine = session.engine
    accepted = engine.get_accepted_records()
    all_attempts = engine.get_all_attempts()
    rejected = [a for a in all_attempts if a.state == "rejected"]

    # Persist accepted decisions as real records (P0 fix: the operator's
    # real-time decisions are the source of truth, not a re-analysis).
    timing_summary = _build_session_timing_summary(session)
    finalize_result: dict[str, Any] = {}
    finalize_error: str | None = None
    try:
        finalize_result = finalize_session(
            session_id=session_id,
            output_root=session.output_root,
            journal=session.journal,
            accepted=accepted,
            rejected=rejected,
            cage_id=session.cage_id,
            project_id=session.project_id,
            device_id=session.device_id,
            upload_queue=_get_upload_queue(),
            video_upload_job_id=(req.video_upload_job_id if req else None),
            capture_meta=(req.capture_meta if req else None),
            timing_summary=timing_summary,
        )
    except Exception:  # noqa: BLE001
        log.exception("realtime: finalize_session failed (session=%s)", session_id)
        finalize_error = "finalize failed; journal preserved for recovery"

    summary = {
        "session_id": session_id,
        "cage_id": session.cage_id,
        "project_id": session.project_id,
        "accepted": [_attempt_to_dict(a) for a in accepted],
        "rejected": [_attempt_to_dict(a) for a in rejected],
        "total_attempts": len(all_attempts),
        "finalize": finalize_result,
        "finalize_error": finalize_error,
        "timing_summary": timing_summary,
    }
    log.info(
        "realtime: finished session %s (accepted=%d attempts=%d run=%s)",
        session_id, len(accepted), len(all_attempts),
        finalize_result.get("run_dir", "-"),
    )
    return summary


# --------------------------------------------------------------------- #
# WebSocket endpoint
# --------------------------------------------------------------------- #


def _check_ws_token(token: str | None) -> bool:
    """Validate the API token supplied via the ``token`` query param.

    When no token is configured (open mode) the socket is allowed. This
    mirrors :func:`ui.auth.require_api_token` semantics.
    """
    from ui.auth import api_token

    expected = api_token()
    if not expected:
        return True  # open mode
    return bool(token) and token == expected


async def _send_state(
    websocket: WebSocket,
    payload: dict[str, Any],
) -> bool:
    """Send a JSON payload, returning False if the socket is dead."""
    try:
        await websocket.send_text(json.dumps(payload, ensure_ascii=False))
        return True
    except WebSocketDisconnect:
        return False
    except RuntimeError:
        # send after close / before accept
        return False
    except Exception:  # noqa: BLE001
        log.warning("realtime: send_text failed", exc_info=True)
        return False


async def _process_one_frame(
    session: ActiveSession,
    websocket: WebSocket,
    image: np.ndarray,
    frame_seq: int,
    client_ts_ms: float,
    *,
    jpeg_bytes: int = 0,
    received_at: float | None = None,
    decode_ms: float = 0.0,
    source_w: int | None = None,
    source_h: int | None = None,
    resized: bool = False,
) -> None:
    """Run the (blocking) engine on a worker thread and push the state."""
    # Run engine + journal append together inside the worker thread so the
    # threading.Lock is never held across an await (P1 fix).
    recv_at = float(received_at if received_at is not None else time.monotonic())

    def _process_and_journal() -> tuple[RealtimeFrameResult, dict[str, Any]]:
        processing_started = time.monotonic()
        with session.frame_lock:
            res = session.engine.process_frame(
                image, frame_seq=frame_seq, client_ts_ms=client_ts_ms
            )
            # Persist a newly-announced attempt before notifying the client,
            # so a crash between announce and push leaves the journal consistent.
            if res.attempt is not None:
                try:
                    session.journal.record_attempt(res.attempt)
                except Exception:  # noqa: BLE001 - journal failure must not break the loop
                    log.exception(
                        "realtime: journal record_attempt failed (session=%s)",
                        session.session_id,
                    )
        processing_completed = time.monotonic()
        engine_ms = round((processing_completed - processing_started) * 1000.0, 2)
        h, w = int(image.shape[0]), int(image.shape[1])
        timing = {
            "frame_seq": int(frame_seq),
            "epoch": int(getattr(res, "epoch", 0) or 0),
            "client_ts_ms": float(client_ts_ms),
            "received_at": recv_at,
            "processing_started_at": processing_started,
            "processing_completed_at": processing_completed,
            "jpeg_bytes": int(jpeg_bytes),
            "image_w": w,
            "image_h": h,
            # Renamed from frame_age_ms: this is the server-side wait between
            # WS message receipt and engine.process_frame() start (decode +
            # threadpool + frame_lock contention). It is NOT a true frame age
            # — phone encode + public-internet transit are not included.
            "server_preprocess_wait_ms": round(
                (processing_started - recv_at) * 1000.0, 2
            ),
            "decode_ms": round(float(decode_ms), 2),
            "engine_ms": engine_ms,
            "source_w": int(source_w) if source_w is not None else w,
            "source_h": int(source_h) if source_h is not None else h,
            "resized": bool(resized),
        }
        return res, timing

    try:
        result, timing = await run_in_threadpool(_process_and_journal)
    except Exception:  # noqa: BLE001 - never let one bad frame kill the socket
        log.exception(
            "realtime: process_frame raised (session=%s seq=%d)",
            session.session_id, frame_seq,
        )
        # Must ACK the frame so the client can release its single in-flight lock.
        await _send_state(
            websocket,
            {
                "type": "error",
                "code": "frame_processing_failed",
                "message": "本帧识别失败，正在重试",
                "frame_seq": int(frame_seq),
                "session_id": session.session_id,
            },
        )
        return

    _touch(session)

    timing["response_sent_at"] = time.monotonic()
    timing["total_ms"] = round(
        (timing["response_sent_at"] - recv_at) * 1000.0, 2
    )
    # Buffer server-side timing for the session summary (P50/P95 at finish).
    _record_server_timing(session, timing)
    payload = _state_payload(
        result, accepted_weight=result.accepted_weight, timing=timing
    )
    payload["session_id"] = session.session_id
    await _send_state(websocket, payload)


async def _handle_ws_command(
    session: ActiveSession,
    websocket: WebSocket,
    cmd: dict[str, Any],
) -> None:
    """Dispatch a JSON text command (retry / accept).

    Engine calls are blocking-but-cheap (no OCR); they run in a worker thread
    alongside the journal append so neither holds the asyncio loop and the
    per-session lock is never acquired across an await.
    """
    ctype = str(cmd.get("type", "")).strip().lower()

    def _retry_locked() -> dict[str, Any]:
        with session.frame_lock:
            return session.engine.request_retry()

    def _accept_locked() -> Attempt | None:
        with session.frame_lock:
            attempt = session.engine.accept_weight()
            if attempt is not None:
                try:
                    session.journal.record_decision(
                        attempt.attempt_id, "accepted", attempt.weight_g
                    )
                except Exception:  # noqa: BLE001
                    log.exception(
                        "realtime: journal record_decision(accept) failed (session=%s)",
                        session.session_id,
                    )
            return attempt

    if ctype == "retry":
        # Record the rejection of the in-flight attempt BEFORE transitioning.
        # Peek at the engine's current attempt (private but stable) so the
        # journal captures which announced weight the operator rejected.
        try:
            cur_attempt = session.engine._current_attempt  # type: ignore[attr-defined]
            if cur_attempt is not None:
                session.journal.record_decision(
                    cur_attempt.attempt_id, "rejected", cur_attempt.weight_g
                )
        except Exception:  # noqa: BLE001
            log.exception("realtime: journal record_decision(retry) failed")
        retry_info = await run_in_threadpool(_retry_locked)
        await _send_state(
            websocket,
            {
                "type": "ack",
                "cmd": "retry",
                "session_id": session.session_id,
                "applied": bool(retry_info.get("applied")),
                "state": retry_info.get("state", session.engine.state.value),
                "epoch": int(retry_info.get("epoch", 0)),
            },
        )
    elif ctype == "accept":
        accepted = await run_in_threadpool(_accept_locked)
        await _send_state(
            websocket,
            {
                "type": "ack",
                "cmd": "accept",
                "session_id": session.session_id,
                "state": session.engine.state.value,
                "accepted": _attempt_to_dict(accepted),
            },
        )
    elif ctype == "client_timing":
        # Client-reported per-ACK timing samples (encode_ms / rtt_ms / jpeg_bytes).
        # Fire-and-forget; no ACK is sent.
        samples = cmd.get("samples") if isinstance(cmd.get("samples"), list) else []
        if samples:
            try:
                _record_client_timing(session, samples)
            except Exception:  # noqa: BLE001
                log.debug("realtime: client_timing merge failed", exc_info=True)
    else:
        log.debug("realtime: ignoring unknown ws command %r", ctype)


@router.websocket("/ws")
async def realtime_ws(
    websocket: WebSocket,
    session_id: str = Query(..., description="session_id from POST /api/realtime/session"),
    token: str | None = Query(None, description="API token (query-param auth for WS)"),
) -> None:
    """Live weighing socket.

    * Binary messages: ``[frame_seq u32 LE][client_ts_ms u32 LE][JPEG]``.
    * Text messages: JSON commands ``{"type": "retry"}`` / ``{"type": "accept"}``.
    * Server replies: JSON ``state`` / ``ack`` text messages.

    On disconnect the session is left in memory (the phone may reconnect);
    it is reaped after :data:`SESSION_TIMEOUT_S` of inactivity.
    """
    # Auth first — close with 4401 before accepting so the client sees a code.
    if not _check_ws_token(token):
        await websocket.close(code=4401)
        return

    try:
        session = _get_session(session_id)
    except HTTPException:
        await websocket.close(code=4404)
        return

    try:
        await websocket.accept()
    except RuntimeError:
        # Already closed / duplicate accept — nothing to do.
        return

    log.info("realtime: ws connected (session=%s)", session_id)

    # Send an initial hello so the client knows the socket is live and can
    # paint the current state before the first frame arrives.
    hello = _status_payload(session)
    hello["type"] = "hello"
    if not await _send_state(websocket, hello):
        return

    try:
        while True:
            msg = await websocket.receive()

            # Starlette normalizes a WebSocket frame into {"type": ...}.
            if msg.get("type") == "websocket.disconnect":
                break

            data = msg.get("text") or msg.get("bytes")
            if data is None:
                continue

            # --- Text command -------------------------------------------------
            if isinstance(data, str):
                try:
                    cmd = json.loads(data)
                except json.JSONDecodeError:
                    log.debug("realtime: ignoring non-JSON text frame")
                    continue
                if not isinstance(cmd, dict):
                    continue
                await _handle_ws_command(session, websocket, cmd)
                continue

            # --- Binary frame -------------------------------------------------
            if len(data) < _HEADER_SIZE:
                log.debug(
                    "realtime: binary frame too short (%d bytes), ignoring",
                    len(data),
                )
                continue

            frame_seq, client_ts_ms = _HEADER_STRUCT.unpack_from(data, 0)
            jpeg = data[_HEADER_SIZE:]
            received_at = time.monotonic()

            decode_t0 = time.monotonic()
            decoded = await run_in_threadpool(_decode_jpeg, jpeg)
            decode_ms = (time.monotonic() - decode_t0) * 1000.0
            if decoded is None:
                # Skip unreadable frames but keep the socket open.
                await _send_state(
                    websocket,
                    {
                        "type": "error",
                        "code": "decode_failed",
                        "message": "无法解码该帧图片",
                        "frame_seq": int(frame_seq),
                    },
                )
                continue

            # Normalize to canonical 720×1280 before the engine so detection
            # thresholds calibrated for that size remain valid regardless of
            # the client's encode profile.
            source_h, source_w = int(decoded.shape[0]), int(decoded.shape[1])
            image, resized = await run_in_threadpool(_normalize_to_canonical, decoded)

            await _process_one_frame(
                session,
                websocket,
                image,
                int(frame_seq),
                float(client_ts_ms),
                jpeg_bytes=len(jpeg),
                received_at=received_at,
                decode_ms=decode_ms,
                source_w=source_w,
                source_h=source_h,
                resized=resized,
            )

    except WebSocketDisconnect:
        # Normal close (client navigated away, network drop). Keep the session
        # so the phone can reconnect and resume.
        log.info("realtime: ws disconnected (session=%s)", session_id)
    except Exception:  # noqa: BLE001
        log.exception(
            "realtime: ws loop crashed (session=%s)", session_id
        )
    finally:
        # Best-effort close; ignore errors if the socket is already gone.
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
