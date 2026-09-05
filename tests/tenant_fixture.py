"""租户隔离契约测试共享夹具（B1/B2 批次）。

夹具拓扑（合同 docs/UPGRADE_TENANT_ISOLATION.md §15-B1）：
- 平台 seed admin：platform_admin，不挂任何租户成员身份（§4.2 权限不重叠）。
- account A（含 parent_owner 用户 ``parent-a``）与 account B（无 owner）。
- tenant a1、a2 挂 account A；tenant b1 挂 account B（越权探测目标）。
- a1 有 tenant_admin / operator / viewer 各一名；b1 有 tenant_admin 一名。
- 设备凭证 dev-a1 绑 a1、dev-b1 绑 b1（明文只在签发响应中出现一次）。

本文件不是测试模块；测试文件通过 ``import tenant_fixture`` 复用。
所有密码 / token 字面量均为测试夹具值。
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

# ---- 夹具字面量（仅测试用） ------------------------------------------ #
PLATFORM_ADMIN_PW = "ctl-platform-admin-pw"
PARENT_PW = "ctl-parent-owner-pw"
TENANT_ADMIN_PW = "ctl-tenant-admin-pw"
OPERATOR_PW = "ctl-operator-pw"
VIEWER_PW = "ctl-viewer-pw"
LEGACY_TOKEN = "ctl-legacy-shared-token"

# 合同 §16-G5 固定的 legacy-default 租户 UUID。
LEGACY_TENANT_ID = "00000000-0000-4000-8000-000000000001"


def reload_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    api_token: str | None = LEGACY_TOKEN,
):
    """把输出根指到 tmp 并重载 ui.app（既有测试的既定模式）。"""
    monkeypatch.setenv("MOUSEVISION_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("MOUSEVISION_ADMIN_PASSWORD", PLATFORM_ADMIN_PW)
    if api_token is None:
        monkeypatch.delenv("MOUSEVISION_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("MOUSEVISION_API_TOKEN", api_token)
    import ui.app as app_mod

    return importlib.reload(app_mod)


def seed_record(
    root: Path,
    run: str,
    record_id: str,
    *,
    weight: float,
    photo: bytes,
    cage: str = "C57-100",
    ordinal: int = 1,
) -> Path:
    """按 run_*/mouse_*/record.json 的现行布局落一份合成记录。"""
    mouse_dir = root / run / "mouse_001"
    mouse_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "record_id": record_id,
        "cage_id": cage,
        "ordinal": ordinal,
        "weight": weight,
        "confidence": 0.9,
        "timestamp": "2026-09-03T00:00:00",
        "run_id": run,
    }
    (mouse_dir / "record.json").write_text(json.dumps(record), encoding="utf-8")
    (mouse_dir / "photo.jpg").write_bytes(photo)
    return mouse_dir


class World:
    """一个已建好控制面拓扑的测试世界（account/tenant/成员/设备）。"""

    def __init__(self, app_mod, platform: TestClient) -> None:
        self.app_mod = app_mod
        self.app = app_mod.app
        self.platform = platform  # platform admin 的会话客户端
        self.control = app_mod.control_store
        self.factory = app_mod.tenant_factory
        self.output = Path(app_mod.DEFAULT_OUTPUT)
        self.accounts: dict[str, str] = {}
        self.tenants: dict[str, str] = {}
        self.devices: dict[str, dict[str, Any]] = {}
        self._clients: dict[str, TestClient] = {}

    # ---- 基础访问 ---------------------------------------------------- #
    def tid(self, slug: str) -> str:
        return self.tenants[slug]

    def tenant_dir(self, slug: str) -> Path:
        return self.output / "tenants" / self.tenants[slug]

    def control_db(self) -> Path:
        return self.output / "control" / "control.db"

    # ---- 登录 -------------------------------------------------------- #
    def login(self, key: str, username: str, password: str, activate: str | None = None) -> TestClient:
        """新开一个客户端登录；``activate`` 给定租户 slug 时设置 active_tenant_id。"""
        c = TestClient(self.app)
        r = c.post("/api/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        if activate is not None:
            r = c.post(
                "/api/control/session/tenant",
                json={"tenant_id": self.tenants[activate]},
            )
            assert r.status_code == 200, r.text
        self._clients[key] = c
        return c

    def client(self, key: str) -> TestClient:
        return self._clients[key]

    def member_client(self, key: str, username: str, password: str, slug: str) -> TestClient:
        return self.login(key, username, password, activate=slug)

    def parent_client(self) -> TestClient:
        """account A 的 parent_owner；默认不设 active_tenant（account 级）。"""
        return self.login("parent", "parent-a", PARENT_PW)

    # ---- 设备凭证 ---------------------------------------------------- #
    def device_token(self, slug: str) -> str:
        return self.devices[slug]["token"]

    def device_headers(self, slug: str, *, bearer: bool = False) -> dict[str, str]:
        token = self.device_token(slug)
        if bearer:
            return {"Authorization": f"Bearer {token}"}
        return {"X-MouseVision-Token": token}

    # ---- 合成数据 ---------------------------------------------------- #
    def seed_tenant_record(
        self,
        slug: str,
        record_id: str,
        *,
        weight: float,
        photo: bytes,
        cage: str = "C57-100",
        run: str = "run_tenant_shared",
    ) -> Path:
        return seed_record(self.tenant_dir(slug), run, record_id, weight=weight, photo=photo, cage=cage)

    def seed_global_record(
        self,
        record_id: str,
        *,
        weight: float,
        photo: bytes,
        cage: str = "C57-100",
        run: str = "run_global_pre_migration",
    ) -> Path:
        return seed_record(self.output, run, record_id, weight=weight, photo=photo, cage=cage)


def build_world(app_mod, platform: TestClient) -> World:
    """通过控制面 API 建立两 account / 三 tenant / 成员 / 两设备的完整拓扑。"""
    w = World(app_mod, platform)

    r = platform.post(
        "/api/control/accounts",
        json={"name": "Lab A", "owner_username": "parent-a", "owner_password": PARENT_PW},
    )
    assert r.status_code in (200, 201), r.text
    w.accounts["a"] = r.json()["id"]

    r = platform.post("/api/control/accounts", json={"name": "Lab B"})
    assert r.status_code in (200, 201), r.text
    w.accounts["b"] = r.json()["id"]

    for slug, acct, name in (("a1", "a", "Workspace A1"), ("a2", "a", "Workspace A2"), ("b1", "b", "Workspace B1")):
        r = platform.post(
            f"/api/control/accounts/{w.accounts[acct]}/tenants",
            json={"name": name, "slug": slug},
        )
        assert r.status_code in (200, 201), r.text
        w.tenants[slug] = r.json()["id"]

    members_a1 = (
        ("admin-a1", "tenant_admin", TENANT_ADMIN_PW),
        ("op-a1", "operator", OPERATOR_PW),
        ("view-a1", "viewer", VIEWER_PW),
    )
    for username, role, pw in members_a1:
        r = platform.post(
            f"/api/control/tenants/{w.tenants['a1']}/members",
            json={"username": username, "password": pw, "role": role},
        )
        assert r.status_code in (200, 201), r.text

    r = platform.post(
        f"/api/control/tenants/{w.tenants['b1']}/members",
        json={"username": "admin-b1", "password": TENANT_ADMIN_PW, "role": "tenant_admin"},
    )
    assert r.status_code in (200, 201), r.text

    r = platform.post(
        f"/api/control/tenants/{w.tenants['b1']}/members",
        json={"username": "op-b1", "password": OPERATOR_PW, "role": "operator"},
    )
    assert r.status_code in (200, 201), r.text

    for slug in ("a1", "b1"):
        r = platform.post(
            f"/api/control/tenants/{w.tenants[slug]}/devices",
            json={"device_label": f"phone-{slug}"},
        )
        assert r.status_code in (200, 201), r.text
        body = r.json()
        assert body.get("token"), "设备凭证明文必须在签发响应中返回一次"
        w.devices[slug] = body
    return w


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """完整拓扑世界：platform admin 已登录，account/tenant/成员/设备就绪。"""
    app_mod = reload_app(tmp_path, monkeypatch)
    with TestClient(app_mod.app) as platform:
        r = platform.post(
            "/api/login", json={"username": "admin", "password": PLATFORM_ADMIN_PW}
        )
        assert r.status_code == 200, r.text
        # seed admin 带 must_change_password，先改密以解锁 active API。
        r = platform.post(
            "/api/me/password",
            json={
                "current_password": PLATFORM_ADMIN_PW,
                "new_password": PLATFORM_ADMIN_PW + "-changed",
            },
        )
        assert r.status_code == 200, r.text
        yield build_world(app_mod, platform)


@pytest.fixture()
def ctl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """裸 ControlStore（不走 HTTP），用于 schema / token 哈希等存储级测试。"""
    monkeypatch.setenv("MOUSEVISION_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("MOUSEVISION_ADMIN_PASSWORD", PLATFORM_ADMIN_PW)
    monkeypatch.delenv("MOUSEVISION_API_TOKEN", raising=False)
    from ui.control_store import ControlStore

    return ControlStore(str(tmp_path / "output" / "control" / "control.db"))
