"""TenantContext：冻结的请求级租户身份（合同 §4.3 / §12 / §15-B2）。

解析顺序（§4.3）：
1. 用户会话（sessions.active_tenant_id）→ 成员角色；parent_owner 无活跃租户时
   只允许 account 级（tenant_id 为空、角色为空）；platform_admin 无活跃租户
   产出 platform 上下文（同样不允许碰业务 store）。
2. 设备凭证（Authorization: Bearer mvdev_…；过渡头 X-MouseVision-Token 先查
   设备表）→ 凭证行上的 tenant_id，忽略客户端传的任何 tenant 字段。
3. 过渡期共享令牌（MOUSEVISION_API_TOKEN）→ 写死 legacy-default 租户，
   角色只有业务写所需的最小集（operator）。
4. 否则 401。新解析层 fail-closed：任何情况下不产出匿名写上下文；open mode
   只允许保留在尚未迁移的旧依赖（auth.require_token_or_operator）里，B4 关闭。

客户端上传的 tenant_id / project_id / cage_id 一律不参与解析——本模块只读
headers 与 cookies，从不读取 body/form。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from ui.control_store import (
    DEVICE_TOKEN_PREFIX,
    LEGACY_TENANT_ID,
    ControlStore,
)
from ui.users import SESSION_COOKIE

ROLE_TENANT_ADMIN = "tenant_admin"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"
ROLE_PARENT_OWNER = "parent_owner"
ROLE_PLATFORM_ADMIN = "platform_admin"
ROLE_DEVICE = "device"

# 设备凭证拥有本租户的业务写能力（称重上报 = operator 级），但没有管理角色。
_DEVICE_ROLES = frozenset({ROLE_DEVICE, ROLE_OPERATOR})
# legacy 共享令牌只映射业务写所需的最小角色集（合同 §15-B2：不含越权角色）。
_LEGACY_ROLES = frozenset({ROLE_OPERATOR})


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str          # 服务端解析；空串表示 account/平台级（不碰业务 store）
    account_id: str
    actor_type: str         # user | device | legacy_token | platform
    actor_id: str
    roles: frozenset[str] = field(default_factory=frozenset)
    output_root: Path = Path("/")

    @property
    def is_account_level(self) -> bool:
        return self.tenant_id == ""

    def has_role(self, *roles: str) -> bool:
        return not self.is_account_level and bool(self.roles & set(roles))


def _bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        return token or None
    return None


def _x_header_token(request: Request) -> str | None:
    token = request.headers.get("x-mousevision-token")
    return (token or "").strip() or None


class ContextResolver:
    """从请求解析不可伪造的 TenantContext（依赖控制面与租户根目录）。"""

    def __init__(self, control_store: ControlStore, output_root: str | Path) -> None:
        self.control = control_store
        self.output_root = Path(output_root)

    @property
    def tenants_root(self) -> Path:
        return self.output_root / "tenants"

    # ---------------------------------------------------------------- #
    def try_resolve(self, request: Request, *, extra_token: str | None = None) -> TenantContext | None:
        """解析成功返回上下文；无任何凭证返回 None；凭证明确无效时抛 401
        （fail-closed：绝不静默放行）。永不产出匿名上下文。

        ``extra_token``：WS 等无法携带 header 的通道经 query 参数传入的令牌，
        优先级排在 Cookie 会话与 header 凭证之后。
        """
        # ① 用户会话（会话优先于设备/legacy，§4.3）
        session_token = request.cookies.get(SESSION_COOKIE)
        if session_token:
            session = self.control.resolve_session(session_token)
            if session is not None:
                ctx = self._context_from_session(session)
                if not ctx.is_account_level:
                    return ctx
                # 会话存在但没有租户上下文（platform / parent / 无 active 租户）
                # 时，若请求同时显式出示了设备/共享令牌，则以令牌解析业务上下文
                # （§4.3②③；设备凭证只映射其绑定的租户，不构成越权）。
                device_token = (
                    _bearer_token(request)
                    or _x_header_token(request)
                    or (extra_token or "").strip()
                    or None
                )
                if device_token:
                    return self._resolve_token(device_token)
                return ctx

        device_token = (
            _bearer_token(request)
            or _x_header_token(request)
            or (extra_token or "").strip()
            or None
        )
        if device_token:
            return self._resolve_token(device_token)

        return None

    def _resolve_token(self, device_token: str) -> TenantContext:
        # ② 设备凭证：mvdev_ 前缀只走设备表（查无此凭证 → 401，不回退）。
        if device_token.startswith(DEVICE_TOKEN_PREFIX):
            return self._context_from_device(device_token)
        # 过渡头 X-MouseVision-Token：先查设备表，再查 legacy 共享令牌。
        device = self.control.authenticate_device(device_token)
        if device is not None:
            return self._device_context(device)
        return self._context_from_legacy(device_token)

    def resolve(self, request: Request) -> TenantContext:
        """解析失败 → 401（fail-closed）。"""
        ctx = self.try_resolve(request)
        if ctx is None:
            raise HTTPException(status_code=401, detail="请先登录或提供有效凭证")
        return ctx

    # ---------------------------------------------------------------- #
    def _context_from_session(self, session: dict[str, Any]) -> TenantContext:
        user_id = session["user_id"]
        active_tenant_id = session.get("active_tenant_id")
        if active_tenant_id:
            tenant = self.control.get_tenant(active_tenant_id)
            if tenant is not None and tenant.get("status", "active") == "active":
                return self._tenant_context_for_user(user_id, tenant, actor_type="user")
        # 无活跃租户：platform_admin → 平台上下文；其余（含 parent_owner）
        # → account 级上下文（tenant_id 为空、无租户角色，只能走列表/汇总）。
        if self.control.is_platform_admin(user_id):
            return TenantContext(
                tenant_id="",
                account_id="",
                actor_type="platform",
                actor_id=user_id,
                roles=frozenset({ROLE_PLATFORM_ADMIN}),
                output_root=self.output_root,
            )
        account_id = session.get("account_id") or ""
        return TenantContext(
            tenant_id="",
            account_id=account_id,
            actor_type="user",
            actor_id=user_id,
            roles=frozenset(),
            output_root=self.output_root,
        )

    def _tenant_context_for_user(
        self, user_id: str, tenant: dict[str, Any], *, actor_type: str
    ) -> TenantContext:
        tenant_id = str(tenant["id"])
        membership = self.control.get_membership(user_id, tenant_id)
        if membership is not None:
            roles = frozenset({str(membership["role"])})
        elif self.control.is_parent_owner(user_id, str(tenant["account_id"])):
            # 主账号默认只读：只给 parent_owner 标识，不给任何写角色（§4.2）。
            roles = frozenset({ROLE_PARENT_OWNER})
        else:
            # 无成员关系 → 不给租户上下文（fail-closed，回落 account 级）。
            return TenantContext(
                tenant_id="",
                account_id=str(tenant["account_id"]),
                actor_type=actor_type,
                actor_id=user_id,
                roles=frozenset(),
                output_root=self.output_root,
            )
        return TenantContext(
            tenant_id=tenant_id,
            account_id=str(tenant["account_id"]),
            actor_type=actor_type,
            actor_id=user_id,
            roles=roles,
            output_root=self.tenants_root / tenant_id,
        )

    def _context_from_device(self, token: str) -> TenantContext:
        device = self.control.authenticate_device(token)
        if device is None:
            raise HTTPException(status_code=401, detail="无效的设备凭证")
        return self._device_context(device)

    def _device_context(self, device: dict[str, Any]) -> TenantContext:
        tenant = self.control.get_tenant(device["tenant_id"])
        if tenant is None or tenant.get("status", "active") != "active":
            raise HTTPException(status_code=401, detail="设备凭证的工作区不可用")
        tenant_id = str(tenant["id"])
        return TenantContext(
            tenant_id=tenant_id,
            account_id=str(tenant["account_id"]),
            actor_type="device",
            actor_id=str(device["device_id"]),
            roles=frozenset(_DEVICE_ROLES),
            output_root=self.tenants_root / tenant_id,
        )

    def _context_from_legacy(self, token: str) -> TenantContext:
        """过渡期共享令牌 → 写死 legacy-default 工作区（§4.3）。"""
        import os

        expected = os.getenv("MOUSEVISION_API_TOKEN", "").strip()
        if not expected or not secrets.compare_digest(token, expected):
            raise HTTPException(status_code=401, detail="无效的 API token")
        legacy = self.control.get_tenant(LEGACY_TENANT_ID)
        if legacy is None:
            # seed 保证存在；真缺失说明控制面损坏 → fail closed。
            raise HTTPException(status_code=401, detail="legacy-default 工作区未初始化")
        return TenantContext(
            tenant_id=LEGACY_TENANT_ID,
            account_id=str(legacy["account_id"]),
            actor_type="legacy_token",
            actor_id="legacy-token",
            roles=frozenset(_LEGACY_ROLES),
            output_root=self.tenants_root / LEGACY_TENANT_ID,
        )
