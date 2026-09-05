"""平台 / 主账号 / 子工作区 / 设备权限边界（合同 §4.2 / §9-3、4、8 / §15-B2）。

B2 范围：控制面权限测试本批转绿；
`test_device_credential_bound_tenant_wins_over_client_fields`（合同 §9-3）
依赖业务 handler 接线，红到 B3。
"""

from __future__ import annotations

import pytest

import tenant_fixture as tf
from tenant_fixture import LEGACY_TOKEN, TENANT_ADMIN_PW
from tenant_fixture import world  # noqa: F401 - pytest fixture 注册


# ------------------------------------------------------------------ #
# parent_owner（合同 §9-4）
# ------------------------------------------------------------------ #
def test_parent_owner_tenant_list_excludes_unbound_accounts(world):
    """主账号只看到自己 account 下的工作区；未绑定的 B account 不可见。"""
    parent = world.parent_client()
    r = parent.get("/api/control/tenants")
    assert r.status_code == 200, r.text
    ids = {item["tenant_id"] for item in r.json()["items"]}
    assert world.tid("a1") in ids
    assert world.tid("a2") in ids
    assert world.tid("b1") not in ids, "parent_owner 不得看到其他 account 的工作区"


def test_parent_owner_cannot_activate_foreign_tenant(world):
    """parent 对未绑定租户：列表不含、直接激活 → 403。"""
    parent = world.parent_client()
    r = parent.post(
        "/api/control/session/tenant", json={"tenant_id": world.tid("b1")}
    )
    assert r.status_code == 403, r.text
    # 激活失败后会话仍无租户上下文（account 级）
    r = parent.get("/api/control/session")
    assert r.status_code == 200
    assert r.json()["active_tenant_id"] is None


def test_parent_owner_can_activate_bound_tenant(world):
    parent = world.parent_client()
    r = parent.post(
        "/api/control/session/tenant", json={"tenant_id": world.tid("a1")}
    )
    assert r.status_code == 200, r.text
    # parent 只有只读作用域，不含写角色
    assert set(r.json()["roles"]) == {"parent_owner"}


# ------------------------------------------------------------------ #
# platform_admin 与 tenant_admin 权限不重叠（合同 §9-8）
# ------------------------------------------------------------------ #
def test_tenant_admin_cannot_list_or_create_accounts(world):
    admin = world.member_client(
        "admin-a1", "admin-a1", TENANT_ADMIN_PW, "a1"
    )
    r = admin.get("/api/control/accounts")
    assert r.status_code == 403, r.text
    r = admin.post("/api/control/accounts", json={"name": "Rogue Lab"})
    assert r.status_code == 403, r.text


def test_tenant_admin_cannot_create_tenants(world):
    admin = world.member_client("admin-a1", "admin-a1", TENANT_ADMIN_PW, "a1")
    r = admin.post(
        f"/api/control/accounts/{world.accounts['a']}/tenants",
        json={"name": "Rogue Tenant", "slug": "rogue"},
    )
    assert r.status_code == 403, r.text


def test_tenant_admin_cannot_touch_other_tenant(world):
    """a1 的 tenant_admin 不能列/管 b1 的成员与设备。"""
    admin_a1 = world.member_client("admin-a1", "admin-a1", TENANT_ADMIN_PW, "a1")
    r = admin_a1.get(f"/api/control/tenants/{world.tid('b1')}/members")
    assert r.status_code == 403, r.text
    r = admin_a1.post(
        f"/api/control/tenants/{world.tid('b1')}/members",
        json={"username": "rogue-b1", "password": "rogue-pw-123", "role": "tenant_admin"},
    )
    assert r.status_code == 403, r.text
    r = admin_a1.post(
        f"/api/control/tenants/{world.tid('b1')}/devices",
        json={"device_label": "rogue-device"},
    )
    assert r.status_code == 403, r.text
    # 也不能把自己的角色提为 B 租户成员
    r = admin_a1.post(
        f"/api/control/tenants/{world.tid('b1')}/members",
        json={"username": "admin-a1", "password": "x-password-1", "role": "operator"},
    )
    assert r.status_code == 403, r.text


def test_tenant_admin_cannot_modify_control_data(world):
    """tenant_admin 不能改 control.db 的平台对象（账号/主账号/平台管理员）。"""
    admin_a1 = world.member_client("admin-a1", "admin-a1", TENANT_ADMIN_PW, "a1")
    # 不能把任何用户提升为平台管理员（无此端点权限）
    users = world.platform.get("/api/users")
    assert users.status_code == 200
    platform_admin_id = next(
        u["id"] for u in users.json()["items"] if u["username"] == "admin"
    )
    r = admin_a1.patch(
        f"/api/users/{platform_admin_id}", json={"display_name": "hijacked"}
    )
    assert r.status_code == 403, r.text
    r = admin_a1.delete(f"/api/users/{platform_admin_id}")
    assert r.status_code == 403, r.text
    # 平台管理员未被改动
    assert world.control.is_platform_admin(platform_admin_id)


def test_platform_admin_has_no_tenant_roles(world):
    """platform_admin 不是子工作区管理员（§4.2）：上下文不含租户角色。"""
    from ui.tenant_context import ContextResolver
    from starlette.requests import Request

    admin = world.platform
    cookie = admin.cookies.get("mv_session")
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [(b"cookie", f"mv_session={cookie}".encode())],
        "client": ("test", 1),
        "server": ("test", 80),
    }
    resolver = ContextResolver(world.control, world.output)
    ctx = resolver.resolve(Request(scope))
    assert ctx.tenant_id == ""
    assert ctx.roles == frozenset({"platform_admin"})
    # 即便伪造 active tenant 也拿不到租户写角色：平台管理员无 membership
    assert world.control.get_membership(ctx.actor_id, world.tid("a1")) is None


def test_legacy_token_roles_limited_to_business_write(world):
    """legacy 令牌只产出 legacy-default + operator 角色，无其他越权。"""
    from ui.tenant_context import ContextResolver
    from starlette.requests import Request

    resolver = ContextResolver(world.control, world.output)
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [(b"x-mousevision-token", LEGACY_TOKEN.encode())],
        "client": ("test", 1),
        "server": ("test", 80),
    }
    ctx = resolver.resolve(Request(scope))
    assert ctx.tenant_id == tf.LEGACY_TENANT_ID
    assert ctx.roles == frozenset({"operator"})
    assert ctx.actor_type == "legacy_token"


# ------------------------------------------------------------------ #
# 成员角色约束
# ------------------------------------------------------------------ #
def test_member_role_vocabulary_enforced(world):
    r = world.platform.post(
        f"/api/control/tenants/{world.tid('a1')}/members",
        json={"username": "bad-role", "password": "bad-role-pw-1", "role": "root"},
    )
    assert r.status_code in (400, 422), r.text


def test_member_login_scoped_to_own_tenant(world):
    """viewer 能登录并激活自己租户，但角色只有 viewer。"""
    viewer = world.member_client("view-a1", "view-a1", tf.VIEWER_PW, "a1")
    r = viewer.get("/api/control/session")
    assert r.status_code == 200
    body = r.json()
    assert body["active_tenant_id"] == world.tid("a1")
    assert body["roles"] == ["viewer"]


# ------------------------------------------------------------------ #
# 设备凭证绑 A，客户端字段指 B → 仍写 A（合同 §9-3，红到 B3）
# ------------------------------------------------------------------ #
def test_device_credential_bound_tenant_wins_over_client_fields(world):
    dev_headers = world.device_headers("a1")
    form = {
        "cage_id": "C57-321",
        "project_id": "b-project",
        "tenant_id": world.tid("b1"),  # 客户端伪造字段
        "device_id": "phone-a1",
        "records": '[{"record_id": "rec-dev-a-1", "ordinal": 1, "weight_g": 21.5}]',
    }
    r = world.platform.post("/api/records/report", data=form, headers=dev_headers)
    # persist_report_records 的成功语义是 201（review 2 精度钉死：实际行为 201）。
    assert r.status_code == 201, (
        f"设备凭证应可通过业务鉴权并落盘：{r.status_code} {r.text}"
    )
    # 记录必须落在 A 租户目录，而不是 B，也绝不是全局根
    a_records = list(world.tenant_dir("a1").glob("run_*/**/record.json"))
    assert any("rec-dev-a-1" in p.read_text(encoding="utf-8") for p in a_records), (
        "设备凭证绑 A，即使 body 带B 的 tenant_id/project_id 也必须写 A"
    )
    b_records = list(world.tenant_dir("b1").glob("run_*/**/record.json"))
    assert not b_records, "不得串写 B 租户目录"


# ------------------------------------------------------------------ #
# legacy /api/users 权限提升链收紧（Review S3，有意变更）
#
# 旧形态：legacy-default tenant_admin 派生 admin 后可经 /api/users 列全平台
# 名录、重置任意用户密码、删除任意用户。收紧后：
# - GET  /api/users：platform_admin 看全表；其余 admin 派生身份只看到自己
#   membership 所在租户的成员名录；
# - POST/PATCH/DELETE /api/users：仅 platform_admin（本租户成员管理走
#   /api/control/tenants/{id}/members 通道）。
# ------------------------------------------------------------------ #
def _make_legacy_tenant_admin(world, username: str = "legacy-ta"):
    """经 platform 通道建一个 legacy-default tenant_admin（派生 role=admin）。"""
    r = world.platform.post(
        "/api/users",
        json={"username": username, "password": "legacy-ta-pw-1", "role": "admin"},
    )
    assert r.status_code == 201, r.text
    return world.login(f"client-{username}", username, "legacy-ta-pw-1")


def test_legacy_tenant_admin_cannot_reset_foreign_password(world):
    """legacy-default tenant_admin 不得重置任意用户密码（旧形态可）。"""
    ta = _make_legacy_tenant_admin(world)
    users = world.platform.get("/api/users").json()["items"]
    target = next(u for u in users if u["username"] == "op-a1")
    r = ta.patch(f"/api/users/{target['id']}", json={"password": "hijacked-pw-1"})
    assert r.status_code == 403, (
        f"legacy 派生 admin 不得改他人密码（实际 {r.status_code}，review S3）: {r.text}"
    )


def test_legacy_tenant_admin_user_list_scoped_to_own_tenant(world):
    """名录只含请求者 membership 所在租户（legacy-default）的成员，不见其他
    租户成员。seed admin 兼任 legacy-default tenant_admin（B2 seed 设计），
    故也在名录内；a1/b1 等其他工作区成员一律不可见。"""
    ta = _make_legacy_tenant_admin(world)
    r = ta.get("/api/users")
    assert r.status_code == 200, r.text
    names = {u["username"] for u in r.json()["items"]}
    assert names == {"admin", "legacy-ta"}, (
        f"legacy-default tenant_admin 只能看到 legacy-default 成员名录，实际 {sorted(names)}"
    )
    # 其他工作区成员与平台外账号绝不在名录
    assert "op-a1" not in names and "view-a1" not in names and "admin-b1" not in names
    # 对照：platform_admin 仍看全表（行为不变）
    all_names = {
        u["username"] for u in world.platform.get("/api/users").json()["items"]
    }
    assert {"admin", "op-a1", "view-a1", "legacy-ta"} <= all_names


def test_legacy_tenant_admin_cannot_create_or_delete_users(world):
    """建号（含 role 设置）与删除：仅 platform_admin。"""
    ta = _make_legacy_tenant_admin(world)
    r = ta.post(
        "/api/users",
        json={"username": "rogue-user", "password": "rogue-pw-123", "role": "admin"},
    )
    assert r.status_code == 403, f"legacy 派生 admin 不得建号（review S3）: {r.text}"

    users = world.platform.get("/api/users").json()["items"]
    target = next(u for u in users if u["username"] == "view-a1")
    assert ta.delete(f"/api/users/{target['id']}").status_code == 403
    # 其他账号级变更（改显示名/禁用）同样 403
    assert ta.patch(f"/api/users/{target['id']}", json={"disabled": True}).status_code == 403
    # 目标用户未被改动
    after = {u["username"]: u for u in world.platform.get("/api/users").json()["items"]}
    assert after["view-a1"]["disabled"] is False


def test_platform_admin_users_crud_unchanged(world):
    """platform_admin 的账号管理行为不变（S3 只收紧 legacy 派生链）。"""
    r = world.platform.post(
        "/api/users",
        json={"username": "temp-op", "password": "temp-op-pw-1", "role": "operator"},
    )
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    r = world.platform.patch(f"/api/users/{uid}", json={"display_name": "Temp OP"})
    assert r.status_code == 200, r.text
    assert r.json()["display_name"] == "Temp OP"
    listed = world.platform.get("/api/users")
    assert listed.status_code == 200
    assert "temp-op" in {u["username"] for u in listed.json()["items"]}
    assert world.platform.delete(f"/api/users/{uid}").json().get("ok") is True
    listed = world.platform.get("/api/users").json()["items"]
    assert "temp-op" not in {u["username"] for u in listed}
