"""B3/B4 新增行为的最小回归（合同 §4.4 / §6.1 / §6.3 / §15-B4）。

覆盖：
- CSRF：Cookie 会话写请求的同源校验（Origin/Referer 与 Host 比对；
  非浏览器客户端无 Origin 放行；设备/令牌通道不受约束）。
- legacy deprecation：共享令牌响应带 X-MV-Deprecated-Token: 1（响应不回显 token）。
- QR v2：/api/boxes/{cage}/qr.svg 的 payload 带 tenant_id（v2）。
- 旧 /api/reset 墓碑 403；/api/tenants/{id}/reset 仅本租户 tenant_admin/平台。
- 匿名读收口：业务读（boxes/records/mice/{index}/lab compare）401。
- open mode 关闭：未配置 MOUSEVISION_API_TOKEN 时，旧令牌头/匿名写一律 401。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import tenant_fixture as tf
from tenant_fixture import LEGACY_TOKEN
from tenant_fixture import world  # noqa: F401 - pytest fixture 注册


# ------------------------------------------------------------------ #
# legacy deprecation 标记（不回显 token）
# ------------------------------------------------------------------ #
def test_legacy_token_response_has_deprecation_marker(world):
    c = TestClient(world.app)
    r = c.get("/api/boxes", headers={"X-MouseVision-Token": LEGACY_TOKEN})
    assert r.status_code == 200, r.text
    assert r.headers.get("X-MV-Deprecated-Token") == "1"
    # 响应体不得回显令牌本身
    assert LEGACY_TOKEN not in r.text


def test_device_and_session_responses_have_no_deprecation_marker(world):
    c = TestClient(world.app)
    r = c.get("/api/boxes", headers=world.device_headers("a1"))
    assert r.status_code == 200, r.text
    assert "X-MV-Deprecated-Token" not in r.headers

    op = world.member_client("op-a1", "op-a1", tf.OPERATOR_PW, "a1")
    r = op.get("/api/boxes")
    assert r.status_code == 200
    assert "X-MV-Deprecated-Token" not in r.headers


# ------------------------------------------------------------------ #
# QR v2（§4.4）
# ------------------------------------------------------------------ #
def test_qr_payload_v2_carries_tenant_id(world, monkeypatch):
    """/api/boxes/{cage}/qr.svg 以 v2 携带服务端租户 id（§4.4）。"""
    import ui.app as app_mod
    from ui.boxes import qr_payload as real_qr_payload

    op = world.member_client("op-a1", "op-a1", tf.OPERATOR_PW, "a1")
    cage = "C57-QR2"
    r = op.post(f"/api/boxes/{cage}/reserve-ordinal")
    assert r.status_code == 200, r.text

    captured: dict = {}

    def _spy(cage_id, project_id="default", version=1, tenant_id=None):
        captured["args"] = (cage_id, project_id, version, tenant_id)
        return real_qr_payload(cage_id, project_id, version=version, tenant_id=tenant_id)

    monkeypatch.setattr(app_mod, "qr_payload", _spy)
    r = op.get(f"/api/boxes/{cage}/qr.svg")
    assert r.status_code == 200, r.text
    cage_id, project_id, version, tenant_id = captured["args"]
    assert version == 2, "QR 必须升级为 v2（§4.4）"
    assert tenant_id == world.tid("a1"), "v2 必须携带服务端租户 id"
    assert cage_id == cage

    # 纯函数级：v2 结构 = {v, tenant_id, project_id, cage_id}；v1 兼容读保持。
    v2 = json.loads(real_qr_payload(cage, "proj", version=2, tenant_id="t-uuid"))
    assert v2 == {"v": 2, "tenant_id": "t-uuid", "project_id": "proj", "cage_id": cage}
    v1 = json.loads(real_qr_payload(cage, "proj"))
    assert v1 == {"v": 1, "project_id": "proj", "cage_id": cage}


# ------------------------------------------------------------------ #
# reset 语义
# ------------------------------------------------------------------ #
def test_old_global_reset_is_tombstoned(world):
    op = world.member_client("op-a1", "op-a1", tf.OPERATOR_PW, "a1")
    r = op.post("/api/reset")
    assert r.status_code == 403, "旧全局 reset 对任何主体都是 403 墓碑"
    # 平台管理员同样不得再清全盘
    r = world.platform.post("/api/reset")
    assert r.status_code == 403


def test_tenant_reset_forbidden_for_parent_and_foreign_admin(world):
    parent = world.parent_client()
    r = parent.post(f"/api/tenants/{world.tid('a1')}/reset")
    assert r.status_code == 403, "parent_owner 默认不可重置（§6.3）"
    foreign = world.member_client("admin-b1", "admin-b1", tf.TENANT_ADMIN_PW, "b1")
    r = foreign.post(f"/api/tenants/{world.tid('a1')}/reset")
    assert r.status_code == 403, "其他租户 tenant_admin 不可重置本租户"
    r = foreign.post("/api/tenants/not-a-uuid/reset")
    assert r.status_code == 404, "非法/不存在租户 → 404"


def test_platform_admin_can_reset_any_tenant(world):
    """平台运维保留租户 reset 能力（§6.3：tenant_admin 或平台）。"""
    world.seed_tenant_record("a1", "rec-pre-reset", weight=1.0, photo=b"x")
    r = world.platform.post(f"/api/tenants/{world.tid('a1')}/reset")
    assert r.status_code == 200, r.text
    assert list(world.tenant_dir("a1").glob("run_*")) == []


# ------------------------------------------------------------------ #
# 匿名读收口（§6.1 / B0.6 漏点）
# ------------------------------------------------------------------ #
def test_anonymous_business_reads_are_rejected(world):
    anon = TestClient(world.app)
    for path in (
        "/api/boxes",
        "/api/records",
        "/api/mice",
        "/api/mice/1",
        "/api/mice/1/photo",
        "/api/jobs",
        "/api/runs",
        "/api/upload-queue",
        "/api/status",
        "/api/overview",
        "/api/lab/compares",
    ):
        r = anon.get(path)
        assert r.status_code == 401, f"匿名 GET {path} 必须 401（现 {r.status_code}）"


def test_health_stays_public_without_business_counts(world):
    anon = TestClient(world.app)
    r = anon.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "active_jobs" not in body, "health 不得泄露业务计数（§6.1）"


# ------------------------------------------------------------------ #
# open mode 关闭（§4.3/§15-B4）
# ------------------------------------------------------------------ #
def test_write_without_any_credential_is_401(world):
    anon = TestClient(world.app)
    r = anon.post("/api/boxes", json={"cage_id": "ANON-1"})
    assert r.status_code == 401
    r = anon.post(
        "/api/records/report",
        data={"cage_id": "C57-X", "records": "[]"},
    )
    assert r.status_code == 401


# ------------------------------------------------------------------ #
# CSRF：Cookie 会话写请求的同源校验（§15-B4）
# ------------------------------------------------------------------ #
def test_csrf_cross_origin_cookie_write_blocked(world):
    op = world.member_client("op-a1", "op-a1", tf.OPERATOR_PW, "a1")
    cookie = op.cookies.get("mv_session")
    evil = TestClient(world.app)
    r = evil.post(
        "/api/boxes",
        json={"cage_id": "CSRF-1"},
        headers={
            "cookie": f"mv_session={cookie}",
            "origin": "https://evil.example",
        },
    )
    assert r.status_code == 403, "跨站 Origin 的 Cookie 写必须 403"
    # Referer 同样校验
    r = evil.post(
        "/api/boxes",
        json={"cage_id": "CSRF-2"},
        headers={
            "cookie": f"mv_session={cookie}",
            "referer": "https://evil.example/attack",
        },
    )
    assert r.status_code == 403


def test_csrf_same_origin_and_non_browser_writes_allowed(world):
    op = world.member_client("op-a1", "op-a1", tf.OPERATOR_PW, "a1")
    cookie = op.cookies.get("mv_session")
    host = "testserver"

    # 同源 Origin（Host == testserver）→ 放行
    same = TestClient(world.app)
    r = same.post(
        "/api/boxes",
        json={"cage_id": "CSRF-OK-1"},
        headers={"cookie": f"mv_session={cookie}", "origin": f"http://{host}"},
    )
    assert r.status_code == 201, r.text

    # 非浏览器客户端（无 Origin/Referer）→ 放行（理由见 app._same_origin_allowed：
    # CSRF 攻击面是浏览器自动附带的 Cookie；非浏览器不构成该威胁）。
    api = TestClient(world.app)
    r = api.post(
        "/api/boxes",
        json={"cage_id": "CSRF-OK-2"},
        headers={"X-MouseVision-Token": LEGACY_TOKEN},
    )
    assert r.status_code == 201, r.text


def test_csrf_does_not_gate_token_or_device_channels(world):
    """设备/令牌接口不受 CSRF 约束：带跨站 Origin 但无 Cookie → 正常。"""
    c = TestClient(world.app)
    r = c.post(
        "/api/boxes",
        json={"cage_id": "CSRF-DEV"},
        headers={**world.device_headers("a1"), "origin": "https://evil.example"},
    )
    assert r.status_code == 201, r.text
