"""路由分类策略（合同 §14.3 / §15-B1 附加项）。

每个 `/api/*` 路由必须登记分类：
public / account / tenant_user / tenant_device / legacy_default_only /
share_only / bind_code / not_yet_migrated。
未分类路由 → 本测试失败；注册表里的幽灵条目 → 同样失败。

初始分类（B2）：今天匿名可读或尚未接入 TenantContext 的业务路由一律
`not_yet_migrated`，B3/B4 逐条消化；本批新增的控制面路由为
account（bind 为 bind_code）。真正的 public 白名单收敛到最小集合。
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from starlette.routing import WebSocketRoute

import tenant_fixture as tf

KNOWN_CATEGORIES = {
    "public",
    "account",
    "tenant_user",
    "tenant_device",
    "tenant_admin_only",
    "legacy_default_only",
    "platform_tool",
    "share_only",
    "bind_code",
    "not_yet_migrated",
}


def test_not_yet_migrated_is_zero():
    """B4 终态：not_yet_migrated 归零（业务路由全部有终态分类）。"""
    rp = _policy()
    nym = {k: v for k, v in rp.ROUTE_CATEGORIES.items() if v == rp.NYM}
    assert not nym, f"仍有未迁移分类的路由: {sorted(map(str, nym))}"

PUBLIC_ALLOWLIST = {
    ("POST", "/api/login"),
    ("POST", "/api/logout"),
    ("GET", "/api/me"),
    ("GET", "/api/health"),
}


def _policy():
    """延迟导入路由分类注册表（B2 交付物；缺失时给出明确失败原因）。"""
    try:
        import ui.route_policy as rp
    except ImportError as exc:  # B2 之前：缺注册表
        raise AssertionError(f"B2 路由分类注册表尚未实现（缺租户能力）: {exc!r}") from exc
    return rp


@pytest.fixture(scope="module")
def api_routes(tmp_path_factory):
    """枚举 app 上全部 /api/* (method, path)（module 级重载一次，减少开销）。"""
    tmp_path = tmp_path_factory.mktemp("route-policy")
    import os

    old_out = os.environ.get("MOUSEVISION_OUTPUT_DIR")
    old_pw = os.environ.get("MOUSEVISION_ADMIN_PASSWORD")
    old_tok = os.environ.get("MOUSEVISION_API_TOKEN")
    os.environ["MOUSEVISION_OUTPUT_DIR"] = str(tmp_path / "output")
    os.environ["MOUSEVISION_ADMIN_PASSWORD"] = tf.PLATFORM_ADMIN_PW
    os.environ["MOUSEVISION_API_TOKEN"] = tf.LEGACY_TOKEN
    try:
        import importlib

        import ui.app as app_mod

        app_mod = importlib.reload(app_mod)
        found = set()
        for route in app_mod.app.routes:
            if isinstance(route, APIRoute):
                for method in route.methods - {"HEAD"}:
                    found.add((method, route.path))
            elif isinstance(route, WebSocketRoute):
                found.add(("WS", route.path))
        yield {pair for pair in found if pair[1].startswith("/api")}
    finally:
        for name, value in (
            ("MOUSEVISION_OUTPUT_DIR", old_out),
            ("MOUSEVISION_ADMIN_PASSWORD", old_pw),
            ("MOUSEVISION_API_TOKEN", old_tok),
        ):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_every_api_route_is_classified(api_routes):
    """反向覆盖：app 上的每个 /api/* (method, path) 都必须在注册表中。"""
    registry_keys = set(_policy().ROUTE_CATEGORIES)
    actual = set(api_routes)
    missing = sorted(f"{m} {p}" for m, p in actual - registry_keys)
    assert not missing, f"以下 /api/* 路由未登记分类（新增路由必须先分类）: {missing}"


def test_no_stale_entries_in_registry(api_routes):
    """正向覆盖：注册表里的每个条目都必须真实存在于 app。"""
    registry_keys = set(_policy().ROUTE_CATEGORIES)
    actual = set(api_routes)
    stale = sorted(f"{m} {p}" for m, p in registry_keys - actual)
    assert not stale, f"注册表存在幽灵条目（路由已删除或路径/方法不匹配）: {stale}"


def test_registry_categories_are_known():
    bad = {k: v for k, v in _policy().ROUTE_CATEGORIES.items() if v not in KNOWN_CATEGORIES}
    assert not bad, f"注册表出现未知分类: {bad}"


def test_public_allowlist_is_minimal():
    """public 只允许登录/登出/me/health；其余任何路由不得标 public。"""
    rp = _policy()
    public_entries = {k for k, v in rp.ROUTE_CATEGORIES.items() if v == rp.CATEGORY_PUBLIC}
    assert public_entries == PUBLIC_ALLOWLIST


def test_share_channel_stays_share_only():
    rp = _policy()
    assert rp.ROUTE_CATEGORIES[("POST", "/api/records/share")] == rp.CATEGORY_SHARE_ONLY


def test_reset_is_never_public():
    """POST /api/reset 是清盘能力，绝不允许 public/匿名。"""
    rp = _policy()
    assert rp.ROUTE_CATEGORIES[("POST", "/api/reset")] != rp.CATEGORY_PUBLIC


def test_control_plane_routes_are_account_scoped(api_routes):
    """本批新增的 /api/control/* 必须登记为 account 或 bind_code。"""
    rp = _policy()
    control_entries = {
        (m, p): cat
        for (m, p), cat in rp.ROUTE_CATEGORIES.items()
        if p.startswith("/api/control")
    }
    assert control_entries, "控制面路由未登记"
    for (m, p), cat in control_entries.items():
        assert cat in {rp.CATEGORY_ACCOUNT, rp.CATEGORY_BIND_CODE}, f"{m} {p} 分类错误: {cat}"
    # 且真实存在
    for (m, p) in control_entries:
        assert (m, p) in api_routes, f"{m} {p} 未挂载到 app"
    assert rp.ROUTE_CATEGORIES[("POST", "/api/control/devices/bind")] == rp.CATEGORY_BIND_CODE
