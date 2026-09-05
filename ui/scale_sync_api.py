"""Scale time-sync REST API (MVP).

Implements the contract in ``docs/SCALE_TIME_SYNC_MVP.md`` §5. The router is
mounted by ``ui/app.py`` after :func:`configure` binds the tenant factory:

    import ui.scale_sync_api as scale_sync_api
    scale_sync_api.configure(tenant_factory)
    app.include_router(scale_sync_api.router)

租户隔离（合同 §5 / §15-B3）：每个租户一套 ScaleSyncStore（
``tenants/<tenant_id>/scale_sync.db`` + ``scale_sync/``），会话按
TenantContext 校验租户归属；跨租户 session_id 统一 404。

The CSV parsing, storage and clock-model math live in
``mousevision.scale_sync``; this module is a thin HTTP layer over it.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
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
from ui.tenant_context import TenantContext

# --------------------------------------------------------------------------- #
# Wiring（B3）：不再持有一次性 configure(output_root) 的全局业务写入语义；
# 生产 app 注入 TenantStoreFactory，store 按请求租户解析。
# factory 为 None 时是裸 router 测试装配（make_store_for_test / configure(root)）。
# --------------------------------------------------------------------------- #

_factory: Any = None
_bare_store: ScaleSyncStore | None = None
_store_lock = threading.Lock()


def configure(factory: Any | None = None, output_root: str | Path | None = None) -> None:
    """生产 app：``configure(factory)``。测试装配可传 output_root（单店模式）。

    兼容旧调用 ``configure("<root>")``：位置参数为无 stores 属性的字符串/路径
    时按裸装配的 output_root 处理，既有 scale-sync 测试无需改动挂载方式。
    """
    global _factory, _bare_store
    with _store_lock:
        if factory is not None and not hasattr(factory, "stores"):
            output_root = output_root or factory
            factory = None
        _factory = factory
        if output_root is not None:
            out = Path(output_root)
            _bare_store = ScaleSyncStore(
                db_path=out / "scale_sync.db",
                files_root=out / "scale_sync",
            )


def make_store_for_test(db_path: str | Path, files_root: str | Path) -> ScaleSyncStore:
    """Build an isolated store for tests (bypassing the global singleton)."""
    return ScaleSyncStore(db_path=db_path, files_root=files_root)


def _store_for(ctx: TenantContext | None) -> ScaleSyncStore:
    if ctx is not None and ctx.tenant_id:
        assert _factory is not None, "scale_sync_api.configure(factory) missing"
        stores = _factory.stores(ctx.tenant_id)
        cached = stores.extra.get("scale_sync")
        if cached is not None:
            return cached
        store = ScaleSyncStore(
            db_path=stores.output_root / "scale_sync.db",
            files_root=stores.output_root / "scale_sync",
        )
        stores.extra["scale_sync"] = store
        return store
    if _factory is not None:
        # Review S2 修复：factory 模式下 account 级 ctx（platform / parent /
        # 未激活 / paused 租户会话）显式 403，不再落 RuntimeError 500。
        raise HTTPException(
            status_code=403,
            detail="请先激活工作区后再进行该操作（当前会话无激活工作区）",
        )
    if _bare_store is not None:
        return _bare_store
    raise RuntimeError("scale_sync_api.configure() was not called")


def _resolve_ctx(request: Request) -> TenantContext | None:
    """fail-closed 的 TenantContext；裸 router 测试装配返回 None。"""
    if _factory is None:
        _legacy_token_gate(request)
        return None
    ctx = _factory.context_from_request(request)
    if getattr(ctx, "actor_type", "") == "legacy_token":
        # B4 兼容窗口：legacy 令牌响应加可观测 deprecation 标记（Review S4；
        # 不记 token 本身）。标记由 app 级中间件转成 X-MV-Deprecated-Token 响应头。
        request.state.mv_legacy_token = True
    return ctx


def _legacy_token_gate(request: Request) -> None:
    """裸 router（无 factory）装配的旧 token 门，仅供隔离的测试 app 使用。

    语义 = B4 之前的 require_api_token：配置了 MOUSEVISION_API_TOKEN 则必须
    匹配，未配置则放行。生产 app 恒走 factory 的 fail-closed 解析层。
    """
    import os

    expected = os.getenv("MOUSEVISION_API_TOKEN", "").strip()
    if not expected:
        return
    supplied = request.headers.get("x-mousevision-token", "").strip()
    if supplied != expected:
        raise HTTPException(status_code=401, detail="无效或缺少 API token")


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


@router.post("/sessions")
def create_session(
    req: CreateSessionRequest,
    ctx: TenantContext | None = Depends(_resolve_ctx),
) -> dict[str, Any]:
    store = _store_for(ctx)
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
)
def put_anchor(
    session_id: str,
    kind: str,
    req: AnchorRequest,
    ctx: TenantContext | None = Depends(_resolve_ctx),
) -> dict[str, Any]:
    store = _store_for(ctx)
    try:
        return store.put_anchor(session_id, kind, req.model_dump())
    except Exception as exc:  # noqa: BLE001
        raise _map_domain_error(exc) from exc


@router.delete(
    "/sessions/{session_id}/anchors/{kind}",
)
def delete_anchor(
    session_id: str,
    kind: str,
    ctx: TenantContext | None = Depends(_resolve_ctx),
) -> dict[str, Any]:
    store = _store_for(ctx)
    try:
        return store.delete_anchor(session_id, kind)
    except Exception as exc:  # noqa: BLE001
        raise _map_domain_error(exc) from exc


@router.post(
    "/sessions/{session_id}/imports",
)
async def create_import(
    session_id: str,
    file: UploadFile = File(...),
    ctx: TenantContext | None = Depends(_resolve_ctx),
) -> dict[str, Any]:
    store = _store_for(ctx)
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
)
def list_readings(
    session_id: str,
    import_id: str,
    query: str | None = Query(None),
    min_weight: float | None = Query(None),
    max_weight: float | None = Query(None),
    limit: int = Query(500, ge=1, le=5000),
    ctx: TenantContext | None = Depends(_resolve_ctx),
) -> dict[str, Any]:
    store = _store_for(ctx)
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
)
def match_anchor(
    session_id: str,
    kind: str,
    req: MatchRequest,
    ctx: TenantContext | None = Depends(_resolve_ctx),
) -> dict[str, Any]:
    store = _store_for(ctx)
    try:
        return store.match_anchor(
            session_id, kind, import_id=req.import_id, source_line_no=req.source_line_no
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_domain_error(exc) from exc


@router.post(
    "/sessions/{session_id}/calculate",
)
def calculate(
    session_id: str,
    ctx: TenantContext | None = Depends(_resolve_ctx),
) -> dict[str, Any]:
    store = _store_for(ctx)
    try:
        result = store.calculate(session_id)
    except Exception as exc:  # noqa: BLE001
        raise _map_domain_error(exc) from exc
    return {"session_id": session_id, "state": "calculated", **result}


@router.get(
    "/sessions/{session_id}",
)
def get_session(
    session_id: str,
    ctx: TenantContext | None = Depends(_resolve_ctx),
) -> dict[str, Any]:
    store = _store_for(ctx)
    try:
        return store.get_session_full(session_id)
    except Exception as exc:  # noqa: BLE001
        raise _map_domain_error(exc) from exc
