"""控制面 API（合同 §4.1 / §15-B2；路由分类见 ui/route_policy.py）。

职责：平台建 account/tenant、tenant_admin 管成员与设备、签发/撤销设备凭证、
生成/消费绑定码、设置会话 active_tenant_id。业务数据的租户化改造属于 B3，
本模块不触碰任何业务 store。

权限模型（§4.2）：
- platform_admin：全部 /api/control/*
- parent_owner：查看自己 account 与其租户、切换到自己 account 的租户（只读）
- tenant_admin：本租户的成员 / 设备 / 绑定码
- operator / viewer：激活自己租户、查看自己的会话上下文

端点前缀 /api/control；``POST /api/control/devices/bind`` 以绑定码本身作凭证，
``POST /api/control/devices/login`` 以子账号密码换设备凭证——两者都是「无会话
凭证签发」通道，单独分类为 bind_code（§6.2 / §15-B5）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ui.control_store import (
    DEFAULT_BIND_CODE_TTL_SECONDS,
    TENANT_ROLES,
    ControlStore,
)
from ui.tenant_stores import TenantStoreFactory
from ui.users import SESSION_COOKIE

router = APIRouter(prefix="/api/control", tags=["control"])

# 设备登录可签发凭证的成员角色（§6.2：设备是写身份；viewer / parent_owner
# 不发设备凭证）。
_DEVICE_LOGIN_ROLES = frozenset({"operator", "tenant_admin"})

_control: ControlStore | None = None
_factory: TenantStoreFactory | None = None


def configure(control_store: ControlStore, tenant_factory: TenantStoreFactory) -> None:
    global _control, _factory
    _control = control_store
    _factory = tenant_factory


def _store() -> ControlStore:
    if _control is None:
        raise RuntimeError("control_api.configure() was not called")
    return _control


# ------------------------------------------------------------------ #
# 会话与权限
# ------------------------------------------------------------------ #
def _session(request: Request) -> dict[str, Any]:
    token = request.cookies.get(SESSION_COOKIE)
    session = _store().resolve_session(token)
    if session is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return session


def _require_platform(request: Request) -> dict[str, Any]:
    session = _session(request)
    if not _store().is_platform_admin(session["user_id"]):
        raise HTTPException(status_code=403, detail="需要平台管理员权限")
    return session


def _get_tenant_or_404(tenant_id: str) -> dict[str, Any]:
    tenant = _store().get_tenant(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    return tenant


def _require_tenant_admin(request: Request, tenant_id: str) -> dict[str, Any]:
    session = _session(request)
    tenant = _get_tenant_or_404(tenant_id)
    store = _store()
    user_id = session["user_id"]
    if store.is_platform_admin(user_id):
        return session
    membership = store.get_membership(user_id, str(tenant["id"]))
    if membership is not None and membership["role"] == "tenant_admin":
        return session
    raise HTTPException(status_code=403, detail="需要本工作区管理员权限")


def _require_tenant_visible(request: Request, tenant_id: str) -> dict[str, Any]:
    """platform / 本租户成员 / 租户所属 account 的 parent_owner 可见。"""
    session = _session(request)
    tenant = _get_tenant_or_404(tenant_id)
    store = _store()
    user_id = session["user_id"]
    if store.is_platform_admin(user_id):
        return session
    if store.get_membership(user_id, str(tenant["id"])) is not None:
        return session
    if store.is_parent_owner(user_id, str(tenant["account_id"])):
        return session
    raise HTTPException(status_code=403, detail="无权访问该工作区")


def _require_account_visible(request: Request, account_id: str) -> dict[str, Any]:
    session = _session(request)
    store = _store()
    if store.is_platform_admin(session["user_id"]):
        return session
    if store.get_account(account_id) is None:
        raise HTTPException(status_code=404, detail="主账号不存在")
    if store.is_parent_owner(session["user_id"], account_id):
        return session
    raise HTTPException(status_code=403, detail="无权访问该主账号")


# ------------------------------------------------------------------ #
# 请求体
# ------------------------------------------------------------------ #
class AccountCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    owner_username: str | None = Field(None, max_length=64)
    owner_password: str | None = Field(None, min_length=8, max_length=128)


class TenantCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    slug: str | None = Field(None, max_length=64)


class MemberCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str | None = Field(None, min_length=8, max_length=128)
    role: str
    display_name: str = Field("", max_length=128)


class DeviceCreate(BaseModel):
    device_label: str = Field("", max_length=128)


class DeviceBind(BaseModel):
    code: str = Field(..., min_length=8, max_length=128)
    device_label: str = Field("", max_length=128)


class DeviceLogin(BaseModel):
    """云版「登录子账号换设备凭证」（§6.2 首次绑定路径之二）。"""

    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)
    tenant_id: str | None = None
    device_label: str = Field("", max_length=128)


class DeviceRotate(BaseModel):
    device_label: str | None = Field(None, max_length=128)


class BindCodeCreate(BaseModel):
    ttl_seconds: int = Field(DEFAULT_BIND_CODE_TTL_SECONDS, ge=1, le=600)


class SessionTenantSet(BaseModel):
    tenant_id: str | None = None


# ------------------------------------------------------------------ #
# accounts / tenants（平台 + parent_owner 只读）
# ------------------------------------------------------------------ #
@router.post("/accounts")
def create_account(body: AccountCreate, request: Request) -> Any:
    session = _require_platform(request)
    store = _store()
    owner_user_id = None
    if body.owner_username:
        if not body.owner_password:
            raise HTTPException(status_code=400, detail="创建主账号用户需要初始密码")
        try:
            owner = store.create_user(body.owner_username, body.owner_password)
        except KeyError:
            raise HTTPException(status_code=409, detail="用户名已存在")
        owner_user_id = owner["id"]
    account = store.create_account(body.name, owner_user_id=owner_user_id)
    _audit(session, "control.account_create", target_type="account", target_id=account["id"], detail={"name": body.name})
    return account


@router.get("/accounts")
def list_accounts(request: Request) -> Any:
    """platform 看全部；parent_owner 看自己的 account；其余 403（§9-8）。"""
    session = _session(request)
    store = _store()
    if store.is_platform_admin(session["user_id"]):
        return {"items": store.list_accounts()}
    owned = store.accounts_for_user(session["user_id"])
    if owned:
        return {"items": owned}
    raise HTTPException(status_code=403, detail="需要平台管理员或主账号权限")


@router.post("/accounts/{account_id}/tenants")
def create_tenant(account_id: str, body: TenantCreate, request: Request) -> Any:
    session = _session(request)
    store = _store()
    if not store.is_platform_admin(session["user_id"]):
        # parent_owner 可以为自己的 account 建子工作区（绑定 = tenants.account_id，§4.1）
        if store.get_account(account_id) is None:
            raise HTTPException(status_code=404, detail="主账号不存在")
        if not store.is_parent_owner(session["user_id"], account_id):
            raise HTTPException(status_code=403, detail="需要平台管理员或主账号权限")
    try:
        tenant = store.create_tenant(account_id, body.name, body.slug)
    except KeyError:
        raise HTTPException(status_code=409, detail="工作区已存在（同账号同 slug）")
    _audit(session, "control.tenant_create", target_type="tenant", target_id=tenant["id"], detail={"name": body.name})
    return tenant


@router.get("/accounts/{account_id}/tenants")
def list_account_tenants(account_id: str, request: Request) -> Any:
    _require_account_visible(request, account_id)
    return {"items": _store().list_tenants(account_id)}


@router.get("/tenants")
def list_my_tenants(request: Request) -> Any:
    """platform：全部租户；其他用户：自己可进入的租户（成员 ∪ 主账号只读）。"""
    session = _session(request)
    store = _store()
    if store.is_platform_admin(session["user_id"]):
        items = store.list_tenants()
    else:
        items = [
            {
                "tenant_id": item["tenant_id"],
                "account_id": item["account_id"],
                "name": item["name"],
                "slug": item["slug"],
                "role": item["role"],
                "status": item["status"],
            }
            for item in store.list_user_tenants(session["user_id"])
        ]
    return {"items": items}


# ------------------------------------------------------------------ #
# 成员管理
# ------------------------------------------------------------------ #
@router.get("/tenants/{tenant_id}/members")
def list_members(tenant_id: str, request: Request) -> Any:
    _require_tenant_visible(request, tenant_id)
    return {"items": _store().list_tenant_members(tenant_id)}


@router.post("/tenants/{tenant_id}/members")
def add_member(tenant_id: str, body: MemberCreate, request: Request) -> Any:
    session = _require_tenant_admin(request, tenant_id)
    if body.role not in TENANT_ROLES:
        raise HTTPException(status_code=400, detail=f"无效角色: {body.role}")
    store = _store()
    user = store.get_user_by_username(body.username)
    if user is None:
        if not body.password:
            raise HTTPException(status_code=400, detail="新用户需要初始密码")
        try:
            user = store.create_user(body.username, body.password, display_name=body.display_name)
        except KeyError:
            raise HTTPException(status_code=409, detail="用户名已存在")
    elif body.password:
        # 已存在用户：不允许由成员管理通道改密（走 /api/users 或 /api/me/password）。
        raise HTTPException(status_code=409, detail="用户名已存在")
    try:
        membership = store.add_membership(user["id"], tenant_id, body.role)
    except KeyError:
        raise HTTPException(status_code=409, detail="该用户已是本工作区成员")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _audit(
        session,
        "control.member_add",
        target_type="user",
        target_id=user["id"],
        detail={"tenant_id": tenant_id, "role": body.role},
    )
    return {"user_id": user["id"], "username": user["username"], "role": membership["role"]}


@router.delete("/tenants/{tenant_id}/members/{user_id}")
def remove_member(tenant_id: str, user_id: str, request: Request) -> Any:
    session = _require_tenant_admin(request, tenant_id)
    try:
        _store().remove_membership(user_id, tenant_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="成员不存在")
    _audit(session, "control.member_remove", target_type="user", target_id=user_id, detail={"tenant_id": tenant_id})
    return {"ok": True}


# ------------------------------------------------------------------ #
# 设备凭证（明文只在签发响应中出现一次）
# ------------------------------------------------------------------ #
@router.get("/tenants/{tenant_id}/devices")
def list_devices(tenant_id: str, request: Request) -> Any:
    _require_tenant_visible(request, tenant_id)
    return {"items": _store().list_device_credentials(tenant_id)}


@router.post("/tenants/{tenant_id}/devices")
def issue_device(tenant_id: str, body: DeviceCreate, request: Request) -> Any:
    session = _require_tenant_admin(request, tenant_id)
    device = _store().issue_device_credential(tenant_id, device_label=body.device_label)
    _audit(session, "control.device_issue", target_type="device", target_id=device["device_id"], detail={"tenant_id": tenant_id})
    return device


def _find_device(device_id: str) -> dict[str, Any] | None:
    """全租户扫描设备凭证行（控制面设备量级小，线性扫描足够）。"""
    store = _store()
    for tenant in store.list_tenants():
        for row in store.list_device_credentials(tenant["id"]):
            if row["id"] == device_id:
                return row
    return None


def _require_device_admin(request: Request, device: dict[str, Any]) -> None:
    """revoke/rotate 的越权面统一（review nit，§6.1）。

    平台管理员 / 设备所在租户的 tenant_admin → 放行；同租户角色不足
    （operator/parent_owner）→ 保留 403；对设备所在租户无任何可见性
    （跨租户猜 device_id）→ 404，不泄露存在性。
    """
    session = _session(request)
    store = _store()
    tenant_id = str(device["tenant_id"])
    if not store.is_platform_admin(session["user_id"]):
        tenant = store.get_tenant(tenant_id)
        member = store.get_membership(session["user_id"], tenant_id)
        parent = (
            store.is_parent_owner(session["user_id"], str(tenant["account_id"]))
            if tenant is not None
            else False
        )
        if member is None and not parent:
            raise HTTPException(status_code=404, detail="设备凭证不存在")
    _require_tenant_admin(request, tenant_id)


@router.delete("/devices/{device_id}")
def revoke_device(device_id: str, request: Request) -> Any:
    session = _session(request)
    store = _store()
    device = _find_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="设备凭证不存在")
    _require_device_admin(request, device)
    store.revoke_device_credential(device_id)
    _audit(session, "control.device_revoke", target_type="device", target_id=device_id)
    return {"ok": True}


# ------------------------------------------------------------------ #
# 绑定码
# ------------------------------------------------------------------ #
@router.post("/tenants/{tenant_id}/bind-codes")
def create_bind_code(tenant_id: str, body: BindCodeCreate, request: Request) -> Any:
    session = _require_tenant_admin(request, tenant_id)
    row = _store().create_bind_code(
        tenant_id, ttl_seconds=body.ttl_seconds, created_by=session["user_id"]
    )
    _audit(session, "control.bind_code_create", target_type="tenant", target_id=tenant_id)
    return row


@router.post("/devices/bind")
def bind_device(body: DeviceBind) -> Any:
    """绑定码即凭证：消费成功 → 为码所属租户签发设备凭证（明文只出现一次）。"""
    try:
        device = _store().consume_and_issue_device(body.code, device_label=body.device_label)
    except KeyError:
        raise HTTPException(status_code=400, detail="绑定码无效、已过期或已被使用")
    tenant = _store().get_tenant(device["tenant_id"])
    return {
        "device_id": device["device_id"],
        "tenant_id": device["tenant_id"],
        "tenant_name": (tenant or {}).get("name", ""),
        "device_label": device["device_label"],
        "token": device["token"],
    }


# ------------------------------------------------------------------ #
# 设备登录（云版首次：登录子账号 → 签发设备凭证，§6.2）
# ------------------------------------------------------------------ #
@router.post("/devices/login")
def device_login(body: DeviceLogin, request: Request) -> Any:
    """子账号密码登录换取设备凭证（无会话、明文只签发一次）。

    - 与 /api/login 同款 IP 失败限速（共享 ui.auth 的失败计数）。
    - 只允许 operator / tenant_admin 成员绑定（设备是写身份）；viewer 与
      parent_owner 一律拒绝。
    - 单一可绑定工作区时默认签发；多工作区必须显式 tenant_id。
    """
    from ui.auth import check_login_rate_limit, clear_login_failures, record_login_failure

    check_login_rate_limit(request)
    store = _store()
    user = store.authenticate(body.username, body.password)
    if user is None:
        record_login_failure(request)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    clear_login_failures(request)
    if user.get("must_change_password"):
        raise HTTPException(status_code=403, detail="必须先修改密码再绑定设备")

    eligible = [
        item
        for item in store.list_user_tenants(user["id"])
        if item.get("role") in _DEVICE_LOGIN_ROLES and item.get("status", "active") == "active"
    ]
    tenant = None
    if body.tenant_id:
        tenant = _get_tenant_or_404(body.tenant_id)
        match = next((t for t in eligible if t["tenant_id"] == str(tenant["id"])), None)
        if match is None:
            raise HTTPException(status_code=403, detail="不是该工作区的可绑定成员（需要 operator/tenant_admin）")
        tenant = {**tenant, "role": match["role"]}
    elif len(eligible) == 1:
        item = eligible[0]
        tenant = {
            "id": item["tenant_id"],
            "name": item["name"],
            "account_id": item["account_id"],
        }
    elif len(eligible) > 1:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "该账号属于多个工作区，请选择要绑定的工作区",
                "tenants": [
                    {"tenant_id": t["tenant_id"], "name": t["name"], "role": t["role"]}
                    for t in eligible
                ],
            },
        )
    else:
        raise HTTPException(
            status_code=403,
            detail="没有可绑定的写身份成员资格（设备凭证只发给 operator/tenant_admin）",
        )

    device = store.issue_device_credential(
        str(tenant["id"]), device_label=body.device_label
    )
    _audit(
        {"user": {"username": user["username"]}},
        "control.device_login",
        target_type="device",
        target_id=device["device_id"],
        detail={"tenant_id": device["tenant_id"], "device_label": device["device_label"]},
    )
    return {
        "device_id": device["device_id"],
        "tenant_id": device["tenant_id"],
        "tenant_name": tenant.get("name", ""),
        "device_label": device["device_label"],
        "token": device["token"],
    }


@router.post("/devices/{device_id}/rotate")
def rotate_device(device_id: str, body: DeviceRotate, request: Request) -> Any:
    """凭证轮换：签发新凭证 + 撤旧（单事务原子，明文只返回一次）。

    权限 = 该设备所在工作区的 tenant_admin 或平台管理员（与撤销一致）。
    """
    session = _session(request)
    store = _store()
    device = _find_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="设备凭证不存在")
    _require_device_admin(request, device)
    try:
        rotated = store.rotate_device_credential(device_id, device_label=body.device_label)
    except KeyError:
        raise HTTPException(status_code=404, detail="设备凭证不存在或已撤销")
    _audit(
        session,
        "control.device_rotate",
        target_type="device",
        target_id=rotated["device_id"],
        detail={"tenant_id": rotated["tenant_id"], "rotated_from": device_id},
    )
    return {
        "device_id": rotated["device_id"],
        "tenant_id": rotated["tenant_id"],
        "tenant_name": (_get_tenant_or_404(str(rotated["tenant_id"])) or {}).get("name", ""),
        "device_label": rotated["device_label"],
        "token": rotated["token"],
        "rotated_from": rotated["rotated_from"],
    }


# ------------------------------------------------------------------ #
# 会话的 active_tenant_id
# ------------------------------------------------------------------ #
@router.get("/session")
def session_info(request: Request) -> Any:
    session = _session(request)
    store = _store()
    user_id = session["user_id"]
    roles: list[str] = []
    active = session.get("active_tenant_id")
    if active:
        membership = store.get_membership(user_id, active)
        if membership is not None:
            roles = [membership["role"]]
        elif store.is_parent_owner(user_id, str(store.get_tenant(active)["account_id"])):
            roles = ["parent_owner"]
    return {
        "user": session["user"],
        "active_tenant_id": active,
        "roles": roles,
        "tenants": [
            {
                "tenant_id": item["tenant_id"],
                "account_id": item["account_id"],
                "name": item["name"],
                "slug": item["slug"],
                "role": item["role"],
                "status": item["status"],
            }
            for item in store.list_user_tenants(user_id)
        ],
    }


@router.post("/session/tenant")
def set_session_tenant(body: SessionTenantSet, request: Request) -> Any:
    session = _session(request)
    store = _store()
    token = request.cookies.get(SESSION_COOKIE) or ""
    if body.tenant_id is None:
        store.set_session_tenant(token, None)
        return {"active_tenant_id": None, "roles": []}
    tenant = _get_tenant_or_404(body.tenant_id)
    user_id = session["user_id"]
    membership = store.get_membership(user_id, str(tenant["id"]))
    if membership is not None:
        roles = [membership["role"]]
    elif store.is_parent_owner(user_id, str(tenant["account_id"])):
        roles = ["parent_owner"]
    else:
        raise HTTPException(status_code=403, detail="不是该工作区成员或主账号")
    store.set_session_tenant(token, str(tenant["id"]))
    return {"active_tenant_id": str(tenant["id"]), "roles": roles}


@router.delete("/session/tenant")
def clear_session_tenant(request: Request) -> Any:
    session = _session(request)
    token = request.cookies.get(SESSION_COOKIE) or ""
    _store().set_session_tenant(token, None)
    return {"active_tenant_id": None, "roles": []}


# ------------------------------------------------------------------ #
def _audit(session: dict[str, Any], action: str, **kwargs: Any) -> None:
    """控制面审计走全局 audit store（B3 再补 tenant_id 列；此处尽力而为）。"""
    try:
        from ui.app import audit_store  # 延迟导入避免循环

        audit_store.log(actor=session["user"]["username"], action=action, **kwargs)
    except Exception:  # noqa: BLE001 - 审计失败不阻塞控制面操作
        pass
