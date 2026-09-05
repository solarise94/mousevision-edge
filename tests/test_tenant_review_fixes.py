"""Review 修复批测试（account 级 ctx 写端点 403 + legacy deprecation 标记）。

覆盖第一轮提交前 review 的 should-fix 项：
- S1/S2：factory 模式下 account 级 ctx（platform / parent / 未激活 / paused
  租户会话）打业务写端点 → 显式 403（detail 提示需激活工作区），不再回落裸
  router 默认根（report run_* 落进程 CWD）、不再 RuntimeError 500（scale-sync）。
- S4：四个模块内解析 ctx 的通道（report / realtime / scale-sync / capture）
  对 legacy 共享令牌同样打 ``X-MV-Deprecated-Token: 1``（app 级中间件转响应头）。

跨租户 realtime finish（B1）见 tests/test_tenant_isolation_contract.py；
/api/users 权限收紧（S3）见 tests/test_tenant_permissions.py。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import tenant_fixture as tf
from tenant_fixture import LEGACY_TOKEN, world  # noqa: F401 - pytest fixture 注册

LEGACY_HEADERS = {"X-MouseVision-Token": LEGACY_TOKEN}


def _report_json(record_id: str) -> dict:
    return {
        "cage_id": "C57-REVIEW",
        "project_id": "default",
        "records": [{"record_id": record_id, "ordinal": 1, "weight_g": 20.0}],
    }


# ------------------------------------------------------------------ #
# S1：report —— account 级 ctx → 403（不再 201 + run 落 CWD/总根）
# ------------------------------------------------------------------ #
def test_platform_session_without_active_tenant_cannot_report(world):
    """platform_admin 登录不自动激活（B6）→ account 级 ctx → 403。"""
    before = sorted(p.name for p in world.output.iterdir())
    r = world.platform.post("/api/records/report", json=_report_json("rec-noactive-1"))
    assert r.status_code == 403, r.text
    assert "工作区" in r.json()["detail"]
    after = sorted(p.name for p in world.output.iterdir())
    assert before == after, "account 级会话上报不得在总根产生任何新目录/文件"


def test_parent_session_without_active_tenant_cannot_report(world):
    """parent_owner 未激活（account 级）同理 403。"""
    parent = world.parent_client()
    r = parent.post("/api/records/report", json=_report_json("rec-noactive-2"))
    assert r.status_code == 403, r.text


def test_activated_tenant_session_can_still_report(world):
    """对照组：激活租户的会话走同一端点不受影响（落激活租户目录）。"""
    op = world.member_client("op-a1", "op-a1", tf.OPERATOR_PW, "a1")
    r = op.post("/api/records/report", json=_report_json("rec-active-1"))
    assert r.status_code == 201, r.text
    runs = list(world.tenant_dir("a1").glob("run_*"))
    assert runs, "激活租户后上报必须落激活租户目录"


# ------------------------------------------------------------------ #
# S1：realtime —— account 级 ctx 建会话 → 403（journal 不落总根）
# ------------------------------------------------------------------ #
def test_platform_session_without_active_tenant_cannot_create_realtime(world):
    r = world.platform.post(
        "/api/realtime/session", json={"cage_id": "C57-REVIEW"}
    )
    assert r.status_code == 403, r.text
    assert "工作区" in r.json()["detail"]
    assert not (world.output / "realtime_journal").exists(), (
        "account 级会话建会话不得在总根写 realtime journal"
    )


def test_legacy_token_realtime_create_still_works(world):
    """对照组：legacy 令牌（写死 legacy-default）不受 account 级 403 影响。"""
    r = TestClient(world.app).post(
        "/api/realtime/session", json={"cage_id": "C57-REVIEW"}, headers=LEGACY_HEADERS
    )
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ #
# S2：scale-sync —— account 级 ctx → 403（不再 RuntimeError 500）
# ------------------------------------------------------------------ #
def test_platform_session_scale_sync_403_not_500(world):
    r = world.platform.post("/api/scale-sync/sessions", json={"project_id": "p"})
    assert r.status_code == 403, (
        f"account 级会话打 scale-sync 必须 403（不得 500）：{r.status_code} {r.text}"
    )
    assert "工作区" in r.json()["detail"]


def test_parent_session_scale_sync_403_not_500(world):
    parent = world.parent_client()
    r = parent.post("/api/scale-sync/sessions", json={"project_id": "p"})
    assert r.status_code == 403, r.text


def test_legacy_token_scale_sync_still_works(world):
    """对照组：legacy 令牌建 scale-sync 会话仍 200（落 legacy-default）。"""
    r = TestClient(world.app).post(
        "/api/scale-sync/sessions", json={"project_id": "p"}, headers=LEGACY_HEADERS
    )
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ #
# S4：四个模块内解析 ctx 的通道同样打 deprecation 标记
# ------------------------------------------------------------------ #
def test_legacy_token_report_has_deprecation_marker(world):
    r = TestClient(world.app).post(
        "/api/records/report",
        json={
            "cage_id": "C57-REVIEW",
            "records": [{"record_id": "rec-mk-report", "ordinal": 1, "weight_g": 19.9}],
        },
        headers=LEGACY_HEADERS,
    )
    assert r.status_code == 201, r.text
    assert r.headers.get("X-MV-Deprecated-Token") == "1"


def test_legacy_token_scale_sync_has_deprecation_marker(world):
    r = TestClient(world.app).post(
        "/api/scale-sync/sessions", json={"project_id": "p"}, headers=LEGACY_HEADERS
    )
    assert r.status_code == 200, r.text
    assert r.headers.get("X-MV-Deprecated-Token") == "1"


def test_legacy_token_realtime_rest_has_deprecation_marker(world):
    r = TestClient(world.app).post(
        "/api/realtime/session", json={"cage_id": "C57-REVIEW"}, headers=LEGACY_HEADERS
    )
    assert r.status_code == 200, r.text
    assert r.headers.get("X-MV-Deprecated-Token") == "1"


def test_legacy_token_scale_capture_has_deprecation_marker(world):
    r = TestClient(world.app).post(
        "/api/scale-capture",
        data={"payload": json.dumps({"readings": [1.0, 2.0]})},
        headers=LEGACY_HEADERS,
    )
    assert r.status_code == 201, r.text
    assert r.headers.get("X-MV-Deprecated-Token") == "1"


def test_device_and_session_channels_have_no_marker_on_module_routes(world):
    """非 legacy 通道（设备凭证/激活会话）走模块路由不得误打标记。"""
    device = TestClient(world.app).post(
        "/api/records/report",
        json=_report_json("rec-mk-device"),
        headers={"Authorization": f"Bearer {world.device_token('a1')}"},
    )
    assert device.status_code == 201, device.text
    assert "X-MV-Deprecated-Token" not in device.headers

    op = world.member_client("op-a1", "op-a1", tf.OPERATOR_PW, "a1")
    r = op.post("/api/scale-sync/sessions", json={"project_id": "p"})
    assert r.status_code == 200, r.text
    assert "X-MV-Deprecated-Token" not in r.headers


# ------------------------------------------------------------------ #
# review nit：revoke/rotate 跨租户猜 device_id → 404（§6.1 不泄露存在性）
# ------------------------------------------------------------------ #
def test_cross_tenant_device_revoke_rotate_404_not_403(world):
    """对他租户设备凭证撤销/轮换：无可见性 → 404；同租户角色不足 → 保留 403；
    被探测的设备不受影响。"""
    admin_b1 = world.member_client("admin-b1", "admin-b1", tf.TENANT_ADMIN_PW, "b1")
    a1_device = world.devices["a1"]["device_id"]
    assert admin_b1.delete(f"/api/control/devices/{a1_device}").status_code == 404
    assert admin_b1.post(
        f"/api/control/devices/{a1_device}/rotate", json={}
    ).status_code == 404

    # 对照：同租户 operator（a1 成员）角色不足 → 403 保留
    op = world.member_client("op-a1", "op-a1", tf.OPERATOR_PW, "a1")
    assert op.delete(f"/api/control/devices/{a1_device}").status_code == 403

    # 被探测设备未被撤销：a1 凭证仍可用
    r = TestClient(world.app).get(
        "/api/boxes", headers={"X-MouseVision-Token": world.device_token("a1")}
    )
    assert r.status_code == 200


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
