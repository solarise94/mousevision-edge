"""Scale time-sync REST API (MVP).

Implements the contract in ``docs/SCALE_TIME_SYNC_MVP.md`` §5. The router is
mounted by ``ui/app.py`` after :func:`configure` binds the output root:

    import ui.scale_sync_api as scale_sync_api
    scale_sync_api.configure(str(DEFAULT_OUTPUT))
    app.include_router(scale_sync_api.router)

All endpoints are gated by ``require_api_token`` (spec §5 — MVP allows uniform
token auth to keep anonymous users out of weighing evidence).

The CSV parsing, storage and clock-model math live in
``mousevision.scale_sync``; this module is a thin HTTP layer over it.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from mousevision.scale_sync import (
    AnchorError,
    CalculationError,
    CsvParseError,
    ImportNotFound,
    MAX_IMPORT_BYTES,
    ScaleSyncError,
    ScaleSyncStore,
    SessionNotFound,
)
from ui.auth import require_api_token

# --------------------------------------------------------------------------- #
# Module-level wiring (mirrors ui/realtime_api.py's configure() pattern)
# --------------------------------------------------------------------------- #

_store: ScaleSyncStore | None = None
_store_lock = threading.Lock()


def configure(output_root: str | Path) -> None:
    """Bind (or rebind) the SQLite DB + file root. Call once before mount."""
    global _store
    out = Path(output_root)
    with _store_lock:
        _store = ScaleSyncStore(
            db_path=out / "scale_sync.db",
            files_root=out / "scale_sync",
        )


def _get_store() -> ScaleSyncStore:
    if _store is None:
        raise RuntimeError("scale_sync_api.configure() was not called")
    return _store


def make_store_for_test(db_path: str | Path, files_root: str | Path) -> ScaleSyncStore:
    """Build an isolated store for tests (bypassing the global singleton)."""
    return ScaleSyncStore(db_path=db_path, files_root=files_root)


router = APIRouter(prefix="/api/scale-sync", tags=["scale-sync"])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _err(detail: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


def _map_domain_error(exc: Exception) -> HTTPException:
    """Translate domain exceptions to HTTP, preserving semantics."""
    if isinstance(exc, (SessionNotFound, ImportNotFound)):
        return _err(str(exc) or "not found", 404)
    if isinstance(exc, (AnchorError, CalculationError, CsvParseError)):
        return _err(str(exc), 400)
    if isinstance(exc, ScaleSyncError):
        return _err(str(exc), 400)
    if isinstance(exc, ValueError):
        return _err(str(exc), 400)
    # Unexpected — let FastAPI render 500.
    raise exc


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #


class CreateSessionRequest(BaseModel):
    project_id: str = "default"
    cage_id: str | None = None
    scale_device_id: str | None = None
    scale_timezone: str = "Asia/Shanghai"


class AnchorRequest(BaseModel):
    """Spec §4.2 anchor payload. ``kind`` is set by the URL path, not the body."""

    client_epoch_ms: int = Field(..., description="手机系统时间（用于匹配视频的主时间）")
    client_perf_ms: float | None = None
    client_timezone: str | None = None
    client_utc_offset_minutes: int | None = None
    observed_weight_g: float | None = None
    note: str = ""


class MatchRequest(BaseModel):
    import_id: str
    source_line_no: int


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@router.post("/sessions", dependencies=[Depends(require_api_token)])
def create_session(req: CreateSessionRequest) -> dict[str, Any]:
    store = _get_store()
    warnings: list[str] = []
    if not (req.scale_device_id or "").strip():
        warnings.append("未填写 scale_device_id，现场无法区分多台天平")
    if not (req.cage_id or "").strip():
        warnings.append("未填写 cage_id")
    sess = store.create_session(
        project_id=req.project_id,
        cage_id=req.cage_id,
        scale_device_id=req.scale_device_id,
        scale_timezone=req.scale_timezone,
    )
    sess["warnings"] = warnings
    return sess


@router.put(
    "/sessions/{session_id}/anchors/{kind}",
    dependencies=[Depends(require_api_token)],
)
def put_anchor(session_id: str, kind: str, req: AnchorRequest) -> dict[str, Any]:
    store = _get_store()
    try:
        return store.put_anchor(session_id, kind, req.model_dump())
    except Exception as exc:  # noqa: BLE001
        raise _map_domain_error(exc) from exc


@router.delete(
    "/sessions/{session_id}/anchors/{kind}",
    dependencies=[Depends(require_api_token)],
)
def delete_anchor(session_id: str, kind: str) -> dict[str, Any]:
    store = _get_store()
    try:
        return store.delete_anchor(session_id, kind)
    except Exception as exc:  # noqa: BLE001
        raise _map_domain_error(exc) from exc


@router.post(
    "/sessions/{session_id}/imports",
    dependencies=[Depends(require_api_token)],
)
async def create_import(
    session_id: str,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    store = _get_store()
    name = (file.filename or "upload.csv").lower()
    if not (name.endswith(".csv") or (file.content_type or "").lower() in {"text/csv", "application/vnd.ms-excel"}):
        raise _err("只接受 .csv 文件", 400)

    raw = await file.read()
    if not raw:
        raise _err("空文件", 400)
    if len(raw) > MAX_IMPORT_BYTES:
        raise _err(f"文件超过 {MAX_IMPORT_BYTES // (1024 * 1024)} MB 限制", 413)

    try:
        return store.create_import(
            session_id=session_id,
            original_filename=file.filename or "upload.csv",
            raw=raw,
            # session timezone is read inside the store from the session row;
            # pass-through here is resolved there.
            scale_timezone=_session_tz(store, session_id),
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_domain_error(exc) from exc


def _session_tz(store: ScaleSyncStore, session_id: str) -> str:
    try:
        return store.get_session(session_id)["scale_timezone"]
    except Exception:  # noqa: BLE001
        return "Asia/Shanghai"


@router.get(
    "/sessions/{session_id}/imports/{import_id}/readings",
    dependencies=[Depends(require_api_token)],
)
def list_readings(
    session_id: str,
    import_id: str,
    query: str | None = Query(None),
    min_weight: float | None = Query(None),
    max_weight: float | None = Query(None),
    limit: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    store = _get_store()
    try:
        items = store.list_readings(
            session_id,
            import_id,
            query=query,
            min_weight=min_weight,
            max_weight=max_weight,
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_domain_error(exc) from exc
    return {"import_id": import_id, "count": len(items), "items": items}


@router.put(
    "/sessions/{session_id}/anchors/{kind}/match",
    dependencies=[Depends(require_api_token)],
)
def match_anchor(session_id: str, kind: str, req: MatchRequest) -> dict[str, Any]:
    store = _get_store()
    try:
        return store.match_anchor(
            session_id, kind, import_id=req.import_id, source_line_no=req.source_line_no
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_domain_error(exc) from exc


@router.post(
    "/sessions/{session_id}/calculate",
    dependencies=[Depends(require_api_token)],
)
def calculate(session_id: str) -> dict[str, Any]:
    store = _get_store()
    try:
        result = store.calculate(session_id)
    except Exception as exc:  # noqa: BLE001
        raise _map_domain_error(exc) from exc
    return {"session_id": session_id, "state": "calculated", **result}


@router.get(
    "/sessions/{session_id}",
    dependencies=[Depends(require_api_token)],
)
def get_session(session_id: str) -> dict[str, Any]:
    store = _get_store()
    try:
        return store.get_session_full(session_id)
    except Exception as exc:  # noqa: BLE001
        raise _map_domain_error(exc) from exc
