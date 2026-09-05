"""主账号 account 级汇总与跨工作区导出 API（合同 §4.2 / §15-B6；B6 批次）。

受众（§4.2）：
- ``parent_owner``：遍历**自己 account** 下 status=active 的租户；
- ``platform_admin``：遍历全部 active 租户（与 GET /api/control/tenants 一致）；
- 子账号（tenant 成员）一律 403 —— UI 隐藏之外 API 先行拒绝（§15-B6）。

端点：
- ``GET /api/account/summary``：每租户一行
  {tenant_id, tenant_name, account_id, account_name, status, boxes, records,
   pending_uploads, last_sync_at}；
- ``GET /api/account/export``：跨工作区 CSV/XLSX 只读导出，每行前置
  tenant_id / tenant_name 两列；复用 ui/records_api.collect_records 的过滤
  形状（按租户参数化调用），导出字段 = 现有单租户导出字段 + 租户两列。

扇出读取（§5）：逐租户只读打开其目录下的 SQLite 文件
（``sqlite3.connect("file:...?mode=ro", uri=True)``），即开即关、绝不 attach
成长连接、绝不写；租户目录/库缺失按 0 计。导出走
``TenantStoreFactory.stores(tenant_id)``（按租户缓存的既有 store 集）。

last_sync_at 选型：取「该租户 records_meta 库的最新 updated_at（记录首次
入库/最近生命周期操作的 DB 时间戳，等价最新记录活动时间）」与「该租户设备
凭证最近 last_used_at」两者的较大者。理由：两者都是单行聚合 DB 读，避免逐
record.json 文件扫描；设备 last_used_at 覆盖「有设备活动但记录尚未落库」的
工作区。两者皆空 → null。
"""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response

from ui.control_store import ControlStore
from ui.records_api import _EXPORT_FIELDS, collect_records
from ui.tenant_stores import TenantStoreFactory

router = APIRouter(prefix="/api/account", tags=["account"])

_control: ControlStore | None = None
_factory: TenantStoreFactory | None = None


def configure(control_store: ControlStore, tenant_factory: TenantStoreFactory) -> None:
    global _control, _factory
    _control = control_store
    _factory = tenant_factory


def _store() -> ControlStore:
    if _control is None:
        raise RuntimeError("account_api.configure() was not called")
    return _control


def _factory_inst() -> TenantStoreFactory:
    if _factory is None:
        raise RuntimeError("account_api.configure() was not called")
    return _factory


# ------------------------------------------------------------------ #
# 会话与可见租户
# ------------------------------------------------------------------ #
def _require_account_scoped_user(request: Request) -> dict[str, Any]:
    """platform_admin 或 parent_owner 才可访问 account 级汇总（其余 403）。"""
    from ui.users import SESSION_COOKIE

    token = request.cookies.get(SESSION_COOKIE)
    session = _store().resolve_session(token)
    if session is None:
        raise HTTPException(status_code=401, detail="请先登录")
    store = _store()
    user_id = session["user_id"]
    if store.is_platform_admin(user_id):
        return session
    if store.accounts_for_user(user_id):
        return session
    raise HTTPException(status_code=403, detail="需要平台管理员或主账号权限")


def _visible_active_tenants(request: Request) -> list[dict[str, Any]]:
    """platform → 全部 active 租户；parent_owner → 自己 account 的 active 租户。"""
    session = _require_account_scoped_user(request)
    store = _store()
    user_id = session["user_id"]
    if store.is_platform_admin(user_id):
        tenants = store.list_tenants()
    else:
        owned_ids = {str(a["id"]) for a in store.accounts_for_user(user_id)}
        tenants = [t for t in store.list_tenants() if str(t["account_id"]) in owned_ids]
    return [t for t in tenants if str(t.get("status") or "active") == "active"]


def _account_names() -> dict[str, str]:
    return {str(a["id"]): str(a.get("name") or "") for a in _store().list_accounts()}


# ------------------------------------------------------------------ #
# 逐租户只读扇出（§5：逐 tenant 目录/DB 读，不开可写大连接）
# ------------------------------------------------------------------ #
def _ro_query(db_path: Path, sql: str) -> list[tuple] | None:
    """只读打开租户 SQLite 文件执行一条聚合查询；文件缺失/表缺失 → None。

    ``mode=ro`` 保证绝不写（含不创建文件）；连接即开即关。
    """
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _scalar(db_path: Path, sql: str, default: Any = 0) -> Any:
    rows = _ro_query(db_path, sql)
    if not rows or rows[0][0] is None:
        return default
    return int(rows[0][0])


def _scalar_text(db_path: Path, sql: str) -> str | None:
    """取文本标量（如 MAX(updated_at)）；库/表缺失或空 → None。"""
    rows = _ro_query(db_path, sql)
    if not rows or rows[0][0] is None:
        return None
    return str(rows[0][0])


def _latest_device_use(tenant_id: str) -> str | None:
    stamps = [
        str(row["last_used_at"])
        for row in _store().list_device_credentials(tenant_id)
        if row.get("last_used_at")
    ]
    return max(stamps) if stamps else None


def _tenant_last_sync(tenant_dir: Path, tenant_id: str) -> str | None:
    """max(records_meta 最新 updated_at, 设备最近 last_used_at)——见模块 docstring 选型。"""
    meta_ts = _scalar_text(
        tenant_dir / "records_meta.db", "SELECT MAX(updated_at) FROM records_meta"
    )
    candidates = [meta_ts] if meta_ts else []
    device_ts = _latest_device_use(tenant_id)
    if device_ts:
        candidates.append(device_ts)
    return max(candidates) if candidates else None


def _tenant_summary_row(
    tenant: dict[str, Any], account_names: dict[str, str]
) -> dict[str, Any]:
    tenant_id = str(tenant["id"])
    tenant_dir = _factory_inst().tenants_root / tenant_id
    pending_uploads = _scalar(
        tenant_dir / "upload_queue.db",
        "SELECT COUNT(*) FROM upload_queue WHERE status IN ('Pending', 'Held', 'Retry')",
    )
    return {
        "tenant_id": tenant_id,
        "tenant_name": str(tenant.get("name") or ""),
        "account_id": str(tenant.get("account_id") or ""),
        "account_name": account_names.get(str(tenant.get("account_id") or ""), ""),
        "status": str(tenant.get("status") or "active"),
        "boxes": _scalar(tenant_dir / "boxes.db", "SELECT COUNT(*) FROM boxes"),
        # records = 该租户目录下 run_*/mouse_*/record.json 文件数（落盘即计数；
        # 不依赖 records_meta 行是否已被浏览/上报读路径触发建行）。glob 只读
        # 文件系统元数据、不解析 JSON 内容，跨库汇总不假设 record_id 全局唯一。
        "records": sum(1 for _ in tenant_dir.glob("run_*/mouse_*/record.json")),
        "pending_uploads": pending_uploads,
        "last_sync_at": _tenant_last_sync(tenant_dir, tenant_id),
    }


# ------------------------------------------------------------------ #
# 端点
# ------------------------------------------------------------------ #
@router.get("/summary")
def account_summary(request: Request) -> dict[str, Any]:
    """主账号工作区汇总：每 active 租户一行，行行带 tenant_id + tenant_name。"""
    tenants = _visible_active_tenants(request)
    account_names = _account_names()
    items = [_tenant_summary_row(t, account_names) for t in tenants]
    items.sort(key=lambda r: (r["account_name"], r["tenant_name"], r["tenant_id"]))
    return {"items": items, "total_tenants": len(items)}


@router.get("/export")
def account_export(
    request: Request,
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
    tab: str = Query("all"),
    strain: str | None = Query(None),
    cage_id: str | None = Query(None),
    q: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
) -> Response:
    """跨工作区只读导出：每租户调用既有 collect_records，行前置租户两列。"""
    tenants = _visible_active_tenants(request)
    factory = _factory_inst()
    show_deleted = tab == "deleted"
    rows: list[dict[str, Any]] = []
    for tenant in tenants:
        tenant_id = str(tenant["id"])
        try:
            stores = factory.stores(tenant_id)
        except KeyError:
            continue  # 租户行刚被删：跳过（导出不虚报）
        items = collect_records(
            stores.registry,
            stores.records_meta,
            stores.output_root,
            tab=tab if tab != "all" else None,
            strain=strain,
            cage_id=cage_id,
            q=q,
            date_from=date_from,
            date_to=date_to,
            include_deleted=show_deleted,
        )
        for rec in items:
            rows.append(
                {
                    "tenant_id": tenant_id,
                    "tenant_name": str(tenant.get("name") or ""),
                    **rec,
                }
            )
    fields = ["tenant_id", "tenant_name", *_EXPORT_FIELDS]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if format == "xlsx":
        content = _export_xlsx(rows, fields)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = f"mousevision_account_export_{stamp}.xlsx"
    else:
        content = _export_csv(rows, fields)
        media = "text/csv; charset=utf-8"
        filename = f"mousevision_account_export_{stamp}.csv"
    try:
        from ui.app import audit_store  # 延迟导入避免循环

        actor = "unknown"
        try:
            from ui.users import SESSION_COOKIE

            session = _store().resolve_session(request.cookies.get(SESSION_COOKIE))
            if session is not None:
                actor = session["user"]["username"]
        except Exception:  # noqa: BLE001
            pass
        audit_store.log(
            actor=actor,
            action="account.export",
            target_type="export",
            target_id=format,
            detail={"count": len(rows), "tenants": len(tenants)},
        )
    except Exception:  # noqa: BLE001 - 审计失败不阻塞导出
        pass
    return Response(
        content=content,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _export_csv(rows: list[dict[str, Any]], fields: list[str]) -> bytes:
    """与 ui.records_api.export_csv 同形状（utf-8-sig，DictWriter ignore extra）。"""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for rec in rows:
        writer.writerow(rec)
    return buf.getvalue().encode("utf-8-sig")


def _export_xlsx(rows: list[dict[str, Any]], fields: list[str]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "records"
    ws.append(fields)
    for rec in rows:
        ws.append([rec.get(h) for h in fields])
    ws2 = wb.create_sheet("说明")
    ws2.append(["字段", "说明"])
    ws2.append(["tenant_id", "记录所属工作区 ID（跨工作区导出，B6）"])
    ws2.append(["tenant_name", "记录所属工作区名称"])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
