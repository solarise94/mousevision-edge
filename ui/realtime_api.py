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
import math
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import cv2
import numpy as np
from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Query,
    Request,
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
from ui.tenant_context import TenantContext

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

    租户隔离（合同 §6.3/§15-B3）：``tenant_id`` 在 session 创建时固化（服务端
    从 TenantContext 解析，永不取自客户端字段）；``output_root`` 为该租户目录，
    journal 与 finalize 落盘只进这棵目录。
    """

    session_id: str
    cage_id: str
    project_id: str
    engine: RealtimeSession
    journal: AttemptJournal
    output_root: str
    created_at: float
    weight_source: str = "ocr"  # "ocr" | "ble_k797"
    last_frame_at: float = 0.0
    recording_t0_ms: float = 0.0  # client's recording start time (future use)
    device_id: str = "scale01"
    tenant_id: str = ""  # 创建时固化的租户（空 = 无 factory 的旧测试装配）
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
# TenantStoreFactory（B3）：生产 app 经 configure(config, factory=...) 注入。
# 为 None 时是"裸 router"测试装配（自身 FastAPI + configure(config, root)），
# 维持旧的开放语义（无租户校验），仅用于隔离的测试 app；生产 app 恒有 factory。
_factory: Any | None = None


def configure(
    config_path: str | Path,
    output_root: str | Path | None = None,
    factory: Any | None = None,
) -> None:
    """Set the YAML config path (and optionally the output root) used by the
    module-level :data:`router`.

    Must be called once at startup, before the router handles requests.
    ``output_root`` defaults to ``MOUSEVISION_OUTPUT_DIR`` env or ``./output``.
    ``factory``：TenantStoreFactory —— 会话创建时固化 tenant_id、按租户落盘
    journal 与 finalize（§6.3）。测试装配的裸 router 可不传（无租户语义）。
    """
    global _config_path, _output_root, _factory
    with _config_lock:
        _config_path = str(config_path)
        if output_root is None:
            import os as _os

            output_root = _os.getenv("MOUSEVISION_OUTPUT_DIR", "output")
        _output_root = str(output_root)
        _factory = factory
        # Invalidate cache so the next request reloads from the new path.
        _invalidate_config_locked()


def _get_tenant_ctx(request: Request) -> TenantContext | None:
    """请求级 TenantContext（fail-closed 401）；裸 router 装配返回 None。"""
    if _factory is None:
        # 旧 token 门（仅供隔离的测试 app）：配置了共享令牌则必须匹配。
        import os

        expected = os.getenv("MOUSEVISION_API_TOKEN", "").strip()
        if expected:
            supplied = (request.headers.get("x-mousevision-token") or "").strip()
            if supplied != expected:
                raise HTTPException(status_code=401, detail="无效或缺少 API token")
        return None
    ctx = _factory.context_from_request(request)
    if ctx.actor_type == "legacy_token":
        # B4 兼容窗口：legacy 令牌响应加可观测 deprecation 标记（Review S4；
        # 不记 token 本身）。WS 通道无 HTTP 响应头，打标记无害、跳过响应即可。
        request.state.mv_legacy_token = True
    return ctx


def _require_tenant_ctx(ctx: TenantContext | None) -> TenantContext:
    """业务写端点的租户上下文守卫（Review S1）。

    factory 模式下 ctx 为 account 级（platform / parent / 未激活会话）→ 403：
    落盘必须有激活工作区，绝不写总根/CWD。裸 router 测试装配（factory=None）
    不受影响。
    """
    if _factory is not None and (ctx is None or not ctx.tenant_id):
        raise HTTPException(
            status_code=403,
            detail="请先激活工作区后再进行该操作（当前会话无激活工作区）",
        )
    return ctx


def _session_upload_queue(session: ActiveSession) -> Any:
    """finalize 落盘后的云同步队列：按会话租户经 factory 解析（§6.3）。"""
    if _factory is not None and session.tenant_id:
        try:
            return _factory.stores(session.tenant_id).upload_queue
        except KeyError:
            log.error(
                "realtime: tenant store missing at finalize (tenant=%s session=%s)",
                session.tenant_id, session.session_id,
            )
            return None
    return _get_upload_queue()


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
        ble_stale_s=_f("ble_stale_s", 10.0),
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


def _create_engine(
    config: dict[str, Any], *, weight_source: str = "ocr"
) -> RealtimeSession:
    """Build a fresh :class:`RealtimeSession` from a parsed config dict.

    Each session gets its own reader + fusion instance — they are stateful
    (fusion maintains a sliding window) and must not be shared across
    concurrent sessions. ``weight_source`` selects whether weight comes from
    OCR (phone LCD) or the BLE K797 cache (ingest_scale_reading).
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
    return RealtimeSession(
        rt_config,
        reader,
        fusion,
        mouse_detect_config=mouse_cfg,
        weight_source=weight_source,
    )


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
    weight_source: str = "ocr",
) -> dict[str, Any]:
    """Build the JSON "state" message sent to the client."""
    payload: dict[str, Any] = {
        "type": "state",
        "state": result.state.value,
        "weight_source": weight_source,
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
        # BLE 原生稳定标志（仅供客户端展示，不参与后端稳定窗判定）。None 表示
        # 非 BLE 会话或本帧无读数。
        "ble_stable": getattr(result, "ble_stable", None),
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
        "weight_source": session.weight_source,
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


def _get_session(session_id: str, ctx: TenantContext | None = None) -> ActiveSession:
    """Fetch a session by id, running expiry first. Raises 404 if absent.

    跨租户的 session_id 一律按不存在处理（统一 404，不 403，合同 §6.1）：
    ctx 租户与会话固化的 tenant_id 不一致 → 404。
    """
    with _sessions_lock:
        _cleanup_expired_locked()
        session = _sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="realtime session not found")
    if (
        ctx is not None
        and session.tenant_id
        and ctx.tenant_id != session.tenant_id
    ):
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
    # 重量来源：OCR（手机拍 LCD，默认）/ ble_k797（天平蓝牙广播）/ manual（操作员手输）。
    weight_source: Literal["ocr", "ble_k797", "manual"] = "ocr"


class CreateSessionResponse(BaseModel):
    session_id: str
    state: str
    weight_source: Literal["ocr", "ble_k797", "manual"] = "ocr"
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


def _build_journal(
    session_id: str,
    cage_id: str,
    project_id: str,
    device_id: str,
    weight_source: str = "ocr",
    output_root: str | Path | None = None,
) -> AttemptJournal:
    """Create an append-only journal for this session and write its meta.

    ``output_root`` 传入会话租户目录（§6.3）；为 None 时退回全局根（裸 router
    测试装配）。"""
    out_root = str(output_root) if output_root is not None else _get_output_root()
    jpath = journal_path(out_root, session_id)
    j = AttemptJournal(jpath)
    j.write_meta(
        JournalMeta(
            session_id=session_id,
            cage_id=cage_id,
            project_id=project_id,
            created_at=time.time(),
            device_id=device_id,
            weight_source=weight_source,
        )
    )
    return j


@router.post(
    "/session",
    response_model=CreateSessionResponse,
)
def create_session(
    req: CreateSessionRequest,
    ctx: TenantContext | None = Depends(_get_tenant_ctx),
) -> CreateSessionResponse:
    """Create a new realtime weighing session.

    Returns the new ``session_id`` (the phone then opens the WebSocket with
    ``?session_id=...``) and the initial state (always ``calibrating``).

    会话创建即固化 ``tenant_id`` 与租户 output_root（§6.3）；无有效凭证 → 401；
    会话无激活工作区（account 级 ctx）→ 403（Review S1，journal/finalize 只落
    激活租户目录）。
    """
    ctx = _require_tenant_ctx(ctx)
    config = _get_config()
    try:
        engine = _create_engine(config, weight_source=req.weight_source)
    except ValueError as exc:
        log.error("realtime: invalid realtime config: %s", exc)
        raise HTTPException(status_code=400, detail=f"invalid realtime config: {exc}") from exc
    except Exception:  # noqa: BLE001 - surface as 500 with context, never crash the app
        log.exception("realtime: failed to build engine from config")
        raise HTTPException(status_code=500, detail="failed to initialize session") from exc

    session_id = uuid.uuid4().hex
    now = time.time()
    device_id = str(config.get("device_id", "scale01"))
    tenant_id = ctx.tenant_id if ctx is not None else ""
    out_root: str | Path = (
        ctx.output_root if ctx is not None else _get_output_root()
    )
    journal = _build_journal(
        session_id, req.cage_id, req.project_id, device_id, req.weight_source,
        output_root=out_root,
    )
    session = ActiveSession(
        session_id=session_id,
        cage_id=req.cage_id,
        project_id=req.project_id,
        engine=engine,
        journal=journal,
        output_root=str(out_root),
        created_at=now,
        weight_source=req.weight_source,
        last_frame_at=0.0,
        device_id=device_id,
        tenant_id=tenant_id,
    )
    with _sessions_lock:
        _cleanup_expired_locked(now)
        _sessions[session_id] = session
    log.info(
        "realtime: created session %s (cage=%s project=%s weight_source=%s tenant=%s)",
        session_id, req.cage_id, req.project_id, req.weight_source,
        tenant_id or "-",
    )
    return CreateSessionResponse(
        session_id=session_id,
        state=engine.state.value,
        weight_source=req.weight_source,
        client_config=_build_client_config(config),
    )


@router.get(
    "/session/{session_id}/status",
)
def session_status(
    session_id: str,
    ctx: TenantContext | None = Depends(_get_tenant_ctx),
) -> dict[str, Any]:
    """Poll the current session state (fallback when WS is disconnected)."""
    return _status_payload(_get_session(session_id, ctx))


@router.post(
    "/session/{session_id}/retry",
)
def session_retry(
    session_id: str,
    ctx: TenantContext | None = Depends(_get_tenant_ctx),
) -> dict[str, Any]:
    """Request a re-weigh of the currently announced attempt."""
    session = _get_session(session_id, ctx)
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
)
def session_accept(
    session_id: str,
    ctx: TenantContext | None = Depends(_get_tenant_ctx),
) -> dict[str, Any]:
    """Accept the currently announced weight and return the attempt."""
    session = _get_session(session_id, ctx)
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
)
def session_finish(
    session_id: str,
    req: FinishRequest | None = Body(None),
    ctx: TenantContext | None = Depends(_get_tenant_ctx),
) -> dict[str, Any]:
    """End the session, persist accepted attempts as durable records, and
    return a summary.

    Accepted attempts become the official records (one ``mouse_NNN`` per
    attempt under a new run dir). Rejected attempts are written to the run
    manifest so the offline pipeline can skip them. The in-memory session is
    then removed; the journal file remains for audit.

    finalize 只落会话固化的租户 output_root，云同步队列按会话租户经 factory
    解析（§6.3）。
    """
    with _sessions_lock:
        _cleanup_expired_locked()
        session = _sessions.get(session_id)
        if session is not None and (
            ctx is None
            or not session.tenant_id
            or ctx.tenant_id == session.tenant_id
        ):
            session = _sessions.pop(session_id)
        else:
            # 跨租户（或已过期消失）：不 pop——会话必须保留在原租户（Review B1
            # 修复：mismatch 时 session 引用不得残留，否则原租户数据被外部
            # finalize 并返回 200）。统一按不存在处理（404，§6.1）。
            session = None
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
            upload_queue=_session_upload_queue(session),
            video_upload_job_id=(req.video_upload_job_id if req else None),
            capture_meta=(req.capture_meta if req else None),
            timing_summary=timing_summary,
            weight_source=session.weight_source,
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


def stop_sessions_for_tenant(tenant_id: str) -> int:
    """弹出某租户的全部活动会话（租户 reset 前调用，§15-B3）。

    返回弹出的会话数；journal 文件保留在租户目录（随目录一起被 reset 清除）。
    """
    removed = 0
    with _sessions_lock:
        for sid in [
            sid
            for sid, s in _sessions.items()
            if s.tenant_id == tenant_id
        ]:
            _sessions.pop(sid, None)
            removed += 1
    if removed:
        log.info("realtime: dropped %d session(s) of tenant %s (reset)", removed, tenant_id)
    return removed


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
        result,
        accepted_weight=result.accepted_weight,
        timing=timing,
        weight_source=session.weight_source,
    )
    payload["session_id"] = session.session_id
    await _send_state(websocket, payload)


# Acceptable payload source tags on a ``scale_reading`` message. The page
# (scale-bridge.js) always sends ``"ble_k797"``; we tolerate the native
# abstractions (``ble``) but reject anything else so a stale page can't push
# OCR-shaped payloads through the BLE path.
_SCALE_READING_SOURCES = {"ble_k797", "ble"}


def _coerce_int(value: Any, *, name: str) -> int | None:
    """Best-effort int coercion for a scale_reading field.

    JSON ints decode as ``int`` already; we also accept numeric strings and
    floats with an integer value. ``bool`` is excluded (Python ``bool`` is an
    ``int`` subclass and must not slip through as 0/1). Returns ``None`` when
    the value is missing or not coercible.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            return None
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip(), 10)
        except ValueError:
            try:
                f = float(value.strip())
            except ValueError:
                return None
            if not math.isfinite(f) or f != int(f):
                return None
            return int(f)
    return None


def _coerce_float(value: Any, *, name: str) -> float | None:
    """Best-effort finite-float coercion for a scale_reading field.

    Accepts ``int``/``float``/numeric-string; rejects ``bool``, ``None``,
    non-finite (NaN/Inf) and unparseable strings. Returns ``None`` on rejection.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        f = float(value)
    elif isinstance(value, str):
        try:
            f = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return f if math.isfinite(f) else None


async def _handle_scale_reading(session: ActiveSession, cmd: dict[str, Any]) -> None:
    """Ingest one BLE scale reading pushed from the page (plan §8.2).

    Only honoured for ``weight_source="ble_k797"`` sessions; OCR sessions
    ignore it (and log) so a BLE payload can never silently take over an OCR
    weighing (plan §12). Runs in a worker thread because
    :meth:`ingest_scale_reading` takes the engine lock.

    Validation mirrors the contract documented in the engine: finite grams in
    [0, 6553.5], integer raw/sequence ≥ 0, |grams − raw/10| ≤ 0.05, and a
    monotonic sequence. Every rejection is fire-and-forget — no ACK — and the
    socket stays open so the page keeps streaming subsequent readings.
    """
    if session.weight_source != "ble_k797":
        log.debug(
            "realtime: ignoring scale_reading on non-BLE session %s (source=%s)",
            session.session_id,
            session.weight_source,
        )
        return

    source = str(cmd.get("source", "")).strip().lower()
    if source not in _SCALE_READING_SOURCES:
        log.debug(
            "realtime: scale_reading rejected: bad source=%r (session=%s)",
            cmd.get("source"),
            session.session_id,
        )
        return

    grams = _coerce_float(cmd.get("grams"), name="grams")
    raw = _coerce_int(cmd.get("raw"), name="raw")
    sequence = _coerce_int(cmd.get("sequence"), name="sequence")
    received_at_epoch_ms = _coerce_int(
        cmd.get("received_at_epoch_ms"), name="received_at_epoch_ms"
    )
    rssi = _coerce_int(cmd.get("rssi"), name="rssi")

    if grams is None or raw is None or sequence is None or received_at_epoch_ms is None:
        log.debug(
            "realtime: scale_reading rejected: missing/non-numeric fields "
            "(grams=%r raw=%r seq=%r ts=%r session=%s)",
            cmd.get("grams"), cmd.get("raw"), cmd.get("sequence"),
            cmd.get("received_at_epoch_ms"), session.session_id,
        )
        return

    if sequence < 0 or raw < 0 or received_at_epoch_ms < 0:
        log.debug(
            "realtime: scale_reading rejected: negative field (raw=%d seq=%d ts=%d session=%s)",
            raw, sequence, received_at_epoch_ms, session.session_id,
        )
        return

    if not (0.0 <= grams <= 6553.5):
        log.debug(
            "realtime: scale_reading rejected: grams out of range [0, 6553.5]: %s (session=%s)",
            grams, session.session_id,
        )
        return

    if not (0 <= raw <= 65535):
        log.debug(
            "realtime: scale_reading rejected: raw out of range [0, 65535]: %d (session=%s)",
            raw, session.session_id,
        )
        return

    if abs(grams - raw / 10.0) > 0.05:
        log.debug(
            "realtime: scale_reading rejected: grams/raw mismatch grams=%s raw=%d (session=%s)",
            grams, raw, session.session_id,
        )
        return

    # `stable` is advisory only (native-derived), never authoritative. Coerce
    # loosely: absent/None -> None, anything truthy/falsy -> bool.
    stable_raw = cmd.get("stable")
    stable: bool | None
    if stable_raw is None:
        stable = None
    elif isinstance(stable_raw, bool):
        stable = stable_raw
    else:
        stable = bool(stable_raw)

    def _ingest_locked() -> bool:
        with session.frame_lock:
            return session.engine.ingest_scale_reading(
                grams=grams,
                raw=raw,
                sequence=sequence,
                received_at_epoch_ms=received_at_epoch_ms,
                stable=stable,
                rssi=rssi,
            )

    try:
        accepted = await run_in_threadpool(_ingest_locked)
    except ValueError:
        # Engine's own range/consistency guard rejected it. Already logged at
        # debug above for the common cases; keep this defensive.
        log.debug(
            "realtime: scale_reading rejected by engine (raw=%d seq=%d session=%s)",
            raw, sequence, session.session_id,
        )
        return

    if not accepted:
        # Non-monotonic sequence: an older/duplicate reading arrived (e.g. a
        # buffered frame flushed after reconnect). Silently drop — the engine
        # already holds the newer reading.
        log.debug(
            "realtime: scale_reading dropped (non-monotonic seq=%d session=%s)",
            sequence, session.session_id,
        )


async def _handle_manual_weight(
    websocket: WebSocket, session: ActiveSession, cmd: dict[str, Any]
) -> None:
    """手动模式：操作员手输一只鼠的克数（plan §手动模式）。

    仅 ``weight_source="manual"`` 会话接受；校验 weight_g 为有限数且在
    [0, 6553.5]，调用引擎 ingest_manual_weight 合成已 accepted 的 attempt，
    回 ACK（含 accepted attempt）。非 manual 会话忽略并记录。
    """
    if session.weight_source != "manual":
        log.debug(
            "realtime: ignoring manual_weight on non-manual session %s (source=%s)",
            session.session_id, session.weight_source,
        )
        return

    weight_g = _coerce_float(cmd.get("weight_g"), name="weight_g")
    if weight_g is None or not math.isfinite(weight_g):
        log.debug(
            "realtime: manual_weight rejected: missing/non-numeric weight_g=%r (session=%s)",
            cmd.get("weight_g"), session.session_id,
        )
        return

    if weight_g < 0 or weight_g > 6553.5:
        log.debug(
            "realtime: manual_weight rejected: weight_g out of range [0, 6553.5]: %s (session=%s)",
            weight_g, session.session_id,
        )
        return

    def _ingest_locked() -> Any:
        with session.frame_lock:
            return session.engine.ingest_manual_weight(weight_g=weight_g)

    try:
        accepted = await run_in_threadpool(_ingest_locked)
    except Exception:  # noqa: BLE001
        log.exception("realtime: manual_weight ingest failed (session=%s)", session.session_id)
        return

    try:
        session.journal.record_decision(accepted.attempt_id, "accepted", accepted.weight_g)
    except Exception:  # noqa: BLE001
        log.exception("realtime: journal record_decision(manual) failed")

    await _send_state(
        websocket,
        {
            "type": "ack",
            "cmd": "manual_weight",
            "session_id": session.session_id,
            "state": session.engine.state.value,
            "accepted": _attempt_to_dict(accepted),
        },
    )


async def _handle_ws_command(
    session: ActiveSession,
    websocket: WebSocket,
    cmd: dict[str, Any],
) -> None:
    """Dispatch a JSON text command (retry / accept / scale_reading).

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
    elif ctype == "scale_reading":
        # BLE 天平读数（K797）。仅在会话声明 weight_source="ble_k797" 时接受；
        # OCR 会话收到则忽略并记录（计划 §12：非 BLE 会话不得静默接受天平读数）。
        await _handle_scale_reading(session, cmd)
    elif ctype == "manual_weight":
        # 手动模式：操作员手输一只鼠的克数。仅 manual 会话接受。
        await _handle_manual_weight(websocket, session, cmd)
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
    * Text messages: JSON commands ``{"type": "retry"}`` / ``{"type": "accept"}``
      / ``{"type": "scale_reading"}`` (BLE sessions only).
    * Server replies: JSON ``state`` / ``ack`` text messages.

    On disconnect the session is left in memory (the phone may reconnect);
    it is reaped after :data:`SESSION_TIMEOUT_S` of inactivity.

    租户校验（合同 §6.3 / §15-B3）：factory 模式下按 cookie/header/query 令牌
    解析 TenantContext；凭证租户与会话固化的租户不一致 → 关闭码 4403；
    无有效凭证 → 4401。
    """
    # Auth first — close with 4401 before accepting so the client sees a code.
    ctx: TenantContext | None = None
    if _factory is not None:
        try:
            ctx = _factory._resolver.try_resolve(websocket, extra_token=token)
        except HTTPException:
            ctx = None
        if ctx is None:
            await websocket.close(code=4401)
            return
    elif not _check_ws_token(token):
        await websocket.close(code=4401)
        return

    try:
        # 先取原始会话（不做租户 404 折叠），以便区分 4404 与跨租户 4403。
        session = _get_session(session_id)
    except HTTPException:
        await websocket.close(code=4404)
        return

    if (
        ctx is not None
        and session.tenant_id
        and ctx.tenant_id != session.tenant_id
    ):
        # 跨租户接管会话：以 4403 拒绝（合同 §9-7）。
        await websocket.close(code=4403)
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
