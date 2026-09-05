"""TenantContext 解析契约（合同 §4.3 / §12 / §15-B2）。

解析顺序：用户会话 → 设备凭证 → legacy 共享令牌 → 401（fail-closed）。
客户端传的 tenant/project/cage 字段一律不参与解析。

B2 范围——本批结束时应全部转绿（身份解析层本身，不含业务 handler 接线）。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import tenant_fixture as tf
from tenant_fixture import LEGACY_TOKEN
from tenant_fixture import ctl, world  # noqa: F401 - pytest fixture 注册

LEGACY_TENANT_ID = tf.LEGACY_TENANT_ID


@pytest.fixture()
def resolver(ctl, tmp_path):
    from ui.tenant_context import ContextResolver

    return ContextResolver(ctl, tmp_path / "output")


def _request(
    *,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    query: str = "",
):
    """构造最小 starlette Request（resolver 只应看 headers/cookies/query）。"""
    raw_headers = []
    for key, value in (headers or {}).items():
        raw_headers.append((key.lower().encode(), value.encode()))
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        raw_headers.append((b"cookie", cookie_str.encode()))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": query.encode(),
        "headers": raw_headers,
        "client": ("test", 123),
        "server": ("test", 80),
    }
    return Request(scope)


def _seed_tenant(ctl, name: str = "T"):
    account = ctl.create_account(f"acct-{name}")
    return ctl.create_tenant(account["id"], name)


def _seed_member(ctl, tenant_id: str, role: str, username: str):
    user = ctl.create_user(username, "password-123")
    ctl.add_membership(user["id"], tenant_id, role)
    return user


def _seed_session(ctl, user_id: str, *, active_tenant: str | None = None) -> str:
    account_id = None
    accounts = ctl.accounts_for_user(user_id)
    if accounts:
        account_id = accounts[0]["id"]
    return ctl.create_session(user_id, account_id=account_id, active_tenant_id=active_tenant)


# ------------------------------------------------------------------ #
# ① 用户会话
# ------------------------------------------------------------------ #
def test_session_context_with_active_tenant(resolver, ctl, tmp_path):
    tenant = _seed_tenant(ctl, "Ts")
    user = _seed_member(ctl, tenant["id"], "operator", "sess-op")
    token = _seed_session(ctl, user["id"], active_tenant=tenant["id"])
    ctx = resolver.resolve(_request(cookies={"mv_session": token}))
    assert ctx.tenant_id == tenant["id"]
    assert ctx.account_id == tenant["account_id"]
    assert ctx.actor_type == "user"
    assert ctx.actor_id == user["id"]
    assert ctx.roles == frozenset({"operator"})
    assert ctx.output_root == tmp_path / "output" / "tenants" / tenant["id"]


def test_session_context_membership_role_sets_roles(resolver, ctl):
    for role in ("tenant_admin", "viewer"):
        tenant = _seed_tenant(ctl, f"Tr-{role}")
        user = _seed_member(ctl, tenant["id"], role, f"sess-{role}")
        token = _seed_session(ctl, user["id"], active_tenant=tenant["id"])
        ctx = resolver.resolve(_request(cookies={"mv_session": token}))
        assert ctx.roles == frozenset({role})


def test_parent_owner_without_active_tenant_is_account_level(resolver, ctl):
    tenant = _seed_tenant(ctl, "Tp")
    parent = ctl.create_user("parent-ctx", "password-123")
    ctl.add_account_owner(parent["id"], tenant["account_id"])
    token = _seed_session(ctl, parent["id"])
    ctx = resolver.resolve(_request(cookies={"mv_session": token}))
    assert ctx.tenant_id == ""  # account 级：不允许碰业务 store
    assert ctx.account_id == tenant["account_id"]
    assert ctx.roles == frozenset()  # 无业务角色 → 只读/汇总类 API 用


def test_platform_admin_without_active_tenant_is_platform_level(resolver, ctl):
    admin = ctl.get_user_by_username("admin")
    token = _seed_session(ctl, admin["id"])
    ctx = resolver.resolve(_request(cookies={"mv_session": token}))
    assert ctx.tenant_id == ""
    assert ctx.actor_type == "platform"
    assert ctx.roles == frozenset({"platform_admin"})


def test_revoked_membership_falls_back_to_account_level(resolver, ctl):
    """成员被移除后会话不得再带租户角色（fail-closed）。"""
    tenant = _seed_tenant(ctl, "Tm")
    user = _seed_member(ctl, tenant["id"], "operator", "sess-removed")
    token = _seed_session(ctl, user["id"], active_tenant=tenant["id"])
    ctl.remove_membership(user["id"], tenant["id"])
    ctx = resolver.resolve(_request(cookies={"mv_session": token}))
    assert ctx.roles == frozenset()
    assert ctx.tenant_id == ""


# ------------------------------------------------------------------ #
# ② 设备凭证
# ------------------------------------------------------------------ #
def test_device_credential_via_bearer_and_legacy_header(resolver, ctl):
    tenant = _seed_tenant(ctl, "Td")
    issued = ctl.issue_device_credential(tenant["id"], device_label="phone-ctx")
    for headers in (
        {"Authorization": f"Bearer {issued['token']}"},
        {"X-MouseVision-Token": issued["token"]},
    ):
        ctx = resolver.resolve(_request(headers=headers))
        assert ctx.tenant_id == tenant["id"]
        assert ctx.actor_type == "device"
        assert ctx.account_id == tenant["account_id"]
        # 设备只拥有本租户的业务写角色（operator 级），绝无 tenant_admin
        assert ctx.roles == frozenset({"device", "operator"})


def test_unknown_mvdev_token_fails_closed_without_legacy_fallback(resolver, monkeypatch):
    """mvdev_ 前缀 token 查无此凭证 → 401，不得回退到 legacy 共享令牌。"""
    monkeypatch.setenv("MOUSEVISION_API_TOKEN", LEGACY_TOKEN)
    with pytest.raises(HTTPException) as exc:
        resolver.resolve(_request(headers={"Authorization": "Bearer mvdev_not-a-credential"}))
    assert exc.value.status_code == 401


# ------------------------------------------------------------------ #
# ③ legacy 共享令牌
# ------------------------------------------------------------------ #
def test_legacy_token_maps_to_fixed_legacy_tenant(resolver, ctl, monkeypatch):
    monkeypatch.setenv("MOUSEVISION_API_TOKEN", LEGACY_TOKEN)
    legacy = ctl.get_tenant(LEGACY_TENANT_ID)
    ctx = resolver.resolve(_request(headers={"X-MouseVision-Token": LEGACY_TOKEN}))
    assert ctx.tenant_id == LEGACY_TENANT_ID == legacy["id"]
    assert ctx.actor_type == "legacy_token"
    # 角色只含业务写所需的最小集（operator），不得含 tenant_admin/platform_admin
    assert ctx.roles == frozenset({"operator"})


def test_legacy_token_via_bearer_header(resolver, ctl, monkeypatch):
    monkeypatch.setenv("MOUSEVISION_API_TOKEN", LEGACY_TOKEN)
    ctx = resolver.resolve(_request(headers={"Authorization": f"Bearer {LEGACY_TOKEN}"}))
    assert ctx.tenant_id == LEGACY_TENANT_ID


def test_legacy_token_fail_closed_when_unset(resolver, monkeypatch):
    """未配置共享令牌：新解析层绝不产出匿名上下文（云版 open mode 关闭）。"""
    monkeypatch.delenv("MOUSEVISION_API_TOKEN", raising=False)
    with pytest.raises(HTTPException) as exc:
        resolver.resolve(_request(headers={"X-MouseVision-Token": "anything"}))
    assert exc.value.status_code == 401


def test_wrong_token_401(resolver, monkeypatch):
    monkeypatch.setenv("MOUSEVISION_API_TOKEN", LEGACY_TOKEN)
    with pytest.raises(HTTPException) as exc:
        resolver.resolve(_request(headers={"X-MouseVision-Token": "not-the-token"}))
    assert exc.value.status_code == 401


def test_no_credentials_401(resolver):
    with pytest.raises(HTTPException) as exc:
        resolver.resolve(_request())
    assert exc.value.status_code == 401


# ------------------------------------------------------------------ #
# 身份不可伪造
# ------------------------------------------------------------------ #
def test_client_supplied_fields_never_change_context(world):
    """tenant/project/cage 出现在 query/body/form/cookie 任何位置都不能改变 TenantContext。"""
    from fastapi import HTTPException as _HTTPException
    from fastapi.responses import JSONResponse

    app_mod = world.app_mod

    def _tenant_probe(request: Request):  # noqa: ANN001 - 测试探针
        try:
            ctx = world.factory.context_from_request(request)
        except _HTTPException as exc:
            return JSONResponse({"status": exc.status_code}, status_code=exc.status_code)
        return {
            "tenant_id": ctx.tenant_id,
            "actor_type": ctx.actor_type,
            "roles": sorted(ctx.roles),
        }

    app_mod.app.get("/api/_tenant_probe")(_tenant_probe)
    app_mod.app.post("/api/_tenant_probe")(_tenant_probe)

    forge_query = (
        f"?tenant_id={world.tid('b1')}&project_id=evil&cage_id=EVIL-1"
    )

    # legacy 令牌上下文：带着 B 租户的 id 也只能落到 legacy-default
    # （用无会话 cookie 的干净客户端，避免会话优先级干扰）
    from fastapi.testclient import TestClient as _TC

    c = _TC(world.app)
    r = c.get(
        f"/api/_tenant_probe{forge_query}",
        headers={"X-MouseVision-Token": LEGACY_TOKEN},
        cookies={"tenant_id": world.tid("b1"), "cage_id": "EVIL-1"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["tenant_id"] == LEGACY_TENANT_ID

    # 设备凭证上下文：绑 A 的设备带 B 的 id 仍解析为 A
    r = c.post(
        "/api/_tenant_probe",
        data={
            "tenant_id": world.tid("b1"),
            "project_id": "evil-project",
            "cage_id": "EVIL-2",
        },
        headers=world.device_headers("a1"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["tenant_id"] == world.tid("a1")
    assert r.json()["actor_type"] == "device"

    # 无凭证 + 伪造字段 → 仍然 401（不产出匿名上下文）
    r = c.get(f"/api/_tenant_probe{forge_query}")
    assert r.status_code == 401


# ------------------------------------------------------------------ #
# 作用域角色检查
# ------------------------------------------------------------------ #
def _ctx(roles, tenant_id="t"):
    from pathlib import Path

    from ui.tenant_context import TenantContext

    return TenantContext(
        tenant_id=tenant_id,
        account_id="a",
        actor_type="user",
        actor_id="u",
        roles=frozenset(roles),
        output_root=Path("/tmp/x"),
    )


def test_require_role_matrix():
    from fastapi import HTTPException

    from ui.tenant_stores import TenantStoreFactory

    require_role = TenantStoreFactory.require_role
    assert require_role(_ctx({"operator"}), "operator", "tenant_admin") is None
    assert require_role(_ctx({"tenant_admin"}), "tenant_admin") is None
    with pytest.raises(HTTPException) as exc:
        require_role(_ctx({"viewer"}), "operator")
    assert exc.value.status_code == 403
    # parent_owner 是只读作用域：不得通过需要写角色的检查
    with pytest.raises(HTTPException):
        require_role(_ctx({"parent_owner"}), "operator")
    # platform_admin 不是租户角色（§4.2 权限不重叠）
    with pytest.raises(HTTPException):
        require_role(_ctx({"platform_admin"}), "tenant_admin")
    # account 级上下文（tenant_id 为空）不能过任何租户角色检查
    with pytest.raises(HTTPException):
        require_role(_ctx({"operator"}, tenant_id=""), "operator")


def test_disabled_user_session_rejected(resolver, ctl):
    tenant = _seed_tenant(ctl, "Tdis")
    user = _seed_member(ctl, tenant["id"], "operator", "sess-disabled")
    token = _seed_session(ctl, user["id"], active_tenant=tenant["id"])
    ctl.update_user(user["id"], disabled=True)
    with pytest.raises(HTTPException) as exc:
        resolver.resolve(_request(cookies={"mv_session": token}))
    assert exc.value.status_code == 401


def test_expired_session_rejected(resolver, ctl):
    from datetime import datetime, timedelta

    tenant = _seed_tenant(ctl, "Texp")
    user = _seed_member(ctl, tenant["id"], "viewer", "sess-expired")
    token = _seed_session(ctl, user["id"], active_tenant=tenant["id"])
    conn = ctl._connect()
    try:
        past = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE sessions SET expires_at=? WHERE token_hash IN (SELECT token_hash FROM sessions WHERE user_id=?)",
            (past, user["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(HTTPException):
        resolver.resolve(_request(cookies={"mv_session": token}))
