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
    created_at: float
    last_frame_at: float = 0.0
    recording_t0_ms: float = 0.0  # client's recording start time (future use)
    # Serializes frame processing for this session across socket reconnects
    # or concurrent REST probes that feed frames.
    frame_lock: threading.Lock = field(default_factory=threading.Lock)


# Global session store (single uvicorn worker -> dict is safe under _sessions_lock).
_sessions: dict[str, ActiveSession] = {}
_sessions_lock = threading.Lock()


# --------------------------------------------------------------------- #
# Config plumbing
# --------------------------------------------------------------------- #

# Set by configure() / create_realtime_router() before the router is mounted.
_config_path: str | Path | None = None
_config_cache: dict[str, Any] | None = None
_config_lock = threading.Lock()


def configure(config_path: str | Path) -> None:
    """Set the YAML config path used by the module-level :data:`router`.

    Must be called once at startup, before the router handles requests.
    """
    global _config_path
    with _config_lock:
        _config_path = str(config_path)
        # Invalidate cache so the next request reloads from the new path.
        _invalidate_config_locked()


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

    rt_config = RealtimeConfig(
        enter_min=float(config.get("enter_min", 1.0)),
        empty_max=float(config.get("empty_max", 0.15)),
        leave_max=float(config.get("leave_max", 0.30)),
    )

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
) -> dict[str, Any]:
    """Build the JSON "state" message sent to the client."""
    return {
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
    }


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


# --------------------------------------------------------------------- #
# REST endpoints
# --------------------------------------------------------------------- #


def create_realtime_router(config_path: str | Path) -> APIRouter:
    """Build a fresh router bound to ``config_path``.

    Preferred for tests / multi-app setups. For the default single-app case,
    call :func:`configure` and use the module-level :data:`router`.
    """
    configure(config_path)
    return router


router = APIRouter(prefix="/api/realtime", tags=["realtime"])


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
    except Exception:  # noqa: BLE001 - surface as 500 with context, never crash the app
        log.exception("realtime: failed to build engine from config")
        raise HTTPException(status_code=500, detail="failed to initialize session")

    session_id = uuid.uuid4().hex
    now = time.time()
    session = ActiveSession(
        session_id=session_id,
        cage_id=req.cage_id,
        project_id=req.project_id,
        engine=engine,
        created_at=now,
        last_frame_at=0.0,
    )
    with _sessions_lock:
        _cleanup_expired_locked(now)
        _sessions[session_id] = session
    log.info(
        "realtime: created session %s (cage=%s project=%s)",
        session_id, req.cage_id, req.project_id,
    )
    return CreateSessionResponse(session_id=session_id, state=engine.state.value)


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
    # No-op outside ANNOUNCED; the engine guards this internally.
    session.engine.request_retry()
    return {
        "session_id": session_id,
        "state": session.engine.state.value,
        "ok": True,
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
    return {
        "session_id": session_id,
        "state": session.engine.state.value,
        "accepted": _attempt_to_dict(attempt),
    }


@router.post(
    "/session/{session_id}/finish",
    dependencies=[Depends(require_api_token)],
)
def session_finish(session_id: str) -> dict[str, Any]:
    """End the session and return a summary.

    The session is removed from the in-memory store; callers should persist
    the ``accepted`` list before/after this call.
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
    summary = {
        "session_id": session_id,
        "cage_id": session.cage_id,
        "project_id": session.project_id,
        "accepted": [_attempt_to_dict(a) for a in accepted],
        "rejected": [_attempt_to_dict(a) for a in rejected],
        "total_attempts": len(all_attempts),
    }
    log.info(
        "realtime: finished session %s (accepted=%d attempts=%d)",
        session_id, len(accepted), len(all_attempts),
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
) -> None:
    """Run the (blocking) engine on a worker thread and push the state."""
    # Serialize per-session so reconnects / probes don't interleave.
    with session.frame_lock:
        try:
            result = await run_in_threadpool(
                session.engine.process_frame,
                image,
                frame_seq=frame_seq,
                client_ts_ms=client_ts_ms,
            )
        except Exception:  # noqa: BLE001 - never let one bad frame kill the socket
            log.exception(
                "realtime: process_frame raised (session=%s seq=%d)",
                session.session_id, frame_seq,
            )
            return

    _touch(session)

    payload = _state_payload(result, accepted_weight=result.accepted_weight)
    payload["session_id"] = session.session_id
    await _send_state(websocket, payload)


async def _handle_ws_command(
    session: ActiveSession,
    websocket: WebSocket,
    cmd: dict[str, Any],
) -> None:
    """Dispatch a JSON text command (retry / accept)."""
    ctype = str(cmd.get("type", "")).strip().lower()
    if ctype == "retry":
        # Blocking call but cheap (no OCR) — run inline.
        session.engine.request_retry()
        await _send_state(
            websocket,
            {
                "type": "ack",
                "cmd": "retry",
                "session_id": session.session_id,
                "state": session.engine.state.value,
            },
        )
    elif ctype == "accept":
        attempt = session.engine.accept_weight()
        await _send_state(
            websocket,
            {
                "type": "ack",
                "cmd": "accept",
                "session_id": session.session_id,
                "state": session.engine.state.value,
                "accepted": _attempt_to_dict(attempt),
            },
        )
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

            image = await run_in_threadpool(_decode_jpeg, jpeg)
            if image is None:
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

            await _process_one_frame(
                session, websocket, image, int(frame_seq), float(client_ts_ms)
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
