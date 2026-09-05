"""B5 批次测试（合同 docs/UPGRADE_TENANT_ISOLATION.md §6.2 / §7 / §15-B5）。

覆盖：
- ``POST /api/control/devices/login``：子账号密码换设备凭证（单租户默认签发、
  多租户必须显式选择、viewer/parent_owner/越权租户拒绝、must_change_password
  拒绝、IP 失败限速、审计不记 token）。
- ``POST /api/control/devices/{device_id}/rotate``：凭证轮换（tenant_admin/
  平台可轮换、operator 拒绝、原子性=旧凭证立即失效、tenant_id 不变、404）。
- HTML 去注入（§6.1）：/legacy、/mobile 不再携带共享 token meta。
- ``POST /api/records/report`` 的 application/json 通道（legacy v1 outbox 兼容
  flush 载荷）：设备凭证/legacy 共享令牌均可落盘、照片 dataURL 解码、批次内
  tenant_id 不参与租户解析（§4.3）。

所有密码 / token 字面量均为测试夹具值。
"""

from __future__ import annotations

import base64
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

import tenant_fixture as tf
from tenant_fixture import world  # noqa: F401 - pytest fixture 注册


@pytest.fixture()
def w(world) -> "tf.World":
    return world


# --------------------------------------------------------------------------- #
# 设备登录
# --------------------------------------------------------------------------- #
def test_device_login_single_tenant_defaults_and_token_works(w):
    c = TestClient(w.app)
    res = c.post(
        "/api/control/devices/login",
        json={"username": "op-a1", "password": tf.OPERATOR_PW},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["token"].startswith("mvdev_"), "设备凭证必须以 mvdev_ 前缀签发"
    assert body["tenant_id"] == w.tid("a1"), "单租户成员默认发其唯一租户"
    assert body["tenant_name"] == "Workspace A1"
    assert body["device_id"]

    # 凭证立即可用：X-MouseVision-Token 先查设备表 → a1 租户业务上下文
    headers = {"X-MouseVision-Token": body["token"]}
    assert TestClient(w.app).get("/api/boxes", headers=headers).status_code == 200
    assert w.control.authenticate_device(body["token"])["tenant_id"] == w.tid("a1")


def test_device_login_response_has_no_plaintext_leak_in_audit(w):
    c = TestClient(w.app)
    res = c.post(
        "/api/control/devices/login",
        json={"username": "op-a1", "password": tf.OPERATOR_PW},
    )
    token = res.json()["token"]
    entries = w.app_mod.audit_store.list(action="control.device_login")["items"]
    assert entries, "设备登录必须写审计"
    dumped = json.dumps(entries, ensure_ascii=False)
    assert token not in dumped, "审计不得出现凭证明文"
    assert tf.OPERATOR_PW not in dumped


def test_device_login_multi_tenant_requires_explicit_tenant(w):
    # 新建一个同时在 a1 / a2 的 operator（a1 建号带密码；a2 复用既有用户）
    r = w.platform.post(
        f"/api/control/tenants/{w.tid('a1')}/members",
        json={"username": "op-multi", "password": tf.OPERATOR_PW, "role": "operator"},
    )
    assert r.status_code in (200, 201), r.text
    r = w.platform.post(
        f"/api/control/tenants/{w.tid('a2')}/members",
        json={"username": "op-multi", "role": "operator"},
    )
    assert r.status_code in (200, 201), r.text
    c = TestClient(w.app)
    res = c.post(
        "/api/control/devices/login",
        json={"username": "op-multi", "password": tf.OPERATOR_PW},
    )
    assert res.status_code == 400, res.status_code
    detail = res.json()["detail"]
    assert isinstance(detail, dict) and len(detail["tenants"]) == 2
    assert {t["tenant_id"] for t in detail["tenants"]} == {w.tid("a1"), w.tid("a2")}

    # 显式选择 a2 → 签发 a2 凭证
    res = c.post(
        "/api/control/devices/login",
        json={
            "username": "op-multi",
            "password": tf.OPERATOR_PW,
            "tenant_id": w.tid("a2"),
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["tenant_id"] == w.tid("a2")


def test_device_login_rejects_viewer_parent_wrong_tenant_and_bad_password(w):
    c = TestClient(w.app)
    # viewer：设备是写身份，不发凭证
    res = c.post(
        "/api/control/devices/login",
        json={"username": "view-a1", "password": tf.VIEWER_PW},
    )
    assert res.status_code == 403
    # parent_owner（无 membership）：不发凭证
    res = c.post(
        "/api/control/devices/login",
        json={"username": "parent-a", "password": tf.PARENT_PW},
    )
    assert res.status_code == 403
    # op-a1 显式指定不属于自己的 b1
    res = c.post(
        "/api/control/devices/login",
        json={
            "username": "op-a1",
            "password": tf.OPERATOR_PW,
            "tenant_id": w.tid("b1"),
        },
    )
    assert res.status_code == 403
    # 密码错误
    res = c.post(
        "/api/control/devices/login",
        json={"username": "op-a1", "password": "wrong-password"},
    )
    assert res.status_code == 401


def test_device_login_rejects_must_change_password(w):
    user = w.control.get_user_by_username("op-a1")
    w.control.update_user(user["id"], must_change_password=1)
    res = TestClient(w.app).post(
        "/api/control/devices/login",
        json={"username": "op-a1", "password": tf.OPERATOR_PW},
    )
    assert res.status_code == 403
    assert "修改密码" in res.json()["detail"]


def test_device_login_rate_limited_like_account_login(w, monkeypatch):
    """与 /api/login 同款 IP 失败限速（共享 ui.auth 的失败计数）。

    TestClient 的 client_ip 恒为 "testclient"；直接预填同一计数器（隔离到
    本测试专用的 defaultdict，避免污染其他测试）。
    """
    import time
    from collections import defaultdict

    import ui.auth as auth_mod

    fresh: defaultdict = defaultdict(list)
    monkeypatch.setattr(auth_mod, "_login_failures", fresh)
    fresh["testclient"] = [time.time()] * auth_mod.LOGIN_MAX_FAILURES
    res = TestClient(w.app).post(
        "/api/control/devices/login",
        json={"username": "op-a1", "password": tf.OPERATOR_PW},
    )
    assert res.status_code == 429


def test_bind_code_response_includes_tenant_name(w):
    r = w.platform.post(
        f"/api/control/tenants/{w.tid('a1')}/bind-codes", json={"ttl_seconds": 60}
    )
    code = r.json()["code"]
    res = TestClient(w.app).post(
        "/api/control/devices/bind",
        json={"code": code, "device_label": "phone-bind"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["tenant_name"] == "Workspace A1"
    assert body["token"].startswith("mvdev_")


# --------------------------------------------------------------------------- #
# 凭证轮换
# --------------------------------------------------------------------------- #
def _rotate(w, client, device_id):
    return client.post(f"/api/control/devices/{device_id}/rotate", json={})


def test_device_rotate_issues_new_and_revokes_old(w):
    old = w.devices["a1"]
    admin = w.login("ta-a1", "admin-a1", tf.TENANT_ADMIN_PW, activate="a1")
    res = _rotate(w, admin, old["device_id"])
    assert res.status_code == 200, res.status_code
    body = res.json()
    assert body["token"].startswith("mvdev_")
    assert body["token"] != old["token"], "轮换必须签发新明文（只出现一次）"
    assert body["device_id"] != old["device_id"]
    assert body["tenant_id"] == w.tid("a1"), "轮换不得改绑租户"
    assert body["rotated_from"] == old["device_id"]
    assert body["tenant_name"] == "Workspace A1"

    # 旧凭证立即失效；新凭证可用
    old_headers = {"X-MouseVision-Token": old["token"]}
    assert TestClient(w.app).get("/api/boxes", headers=old_headers).status_code == 401
    new_headers = {"X-MouseVision-Token": body["token"]}
    assert TestClient(w.app).get("/api/boxes", headers=new_headers).status_code == 200


def test_device_rotate_forbidden_for_operator_and_parent(w):
    old = w.devices["a1"]
    operator = w.login("op-sess", "op-a1", tf.OPERATOR_PW, activate="a1")
    assert _rotate(w, operator, old["device_id"]).status_code == 403
    parent = w.parent_client()
    assert _rotate(w, parent, old["device_id"]).status_code == 403


def test_device_rotate_allowed_for_platform(w):
    old = w.devices["b1"]
    res = _rotate(w, w.platform, old["device_id"])
    assert res.status_code == 200, res.text
    assert res.json()["tenant_id"] == w.tid("b1")


def test_device_rotate_unknown_and_revoked_404(w):
    admin = w.login("ta-a1", "admin-a1", tf.TENANT_ADMIN_PW, activate="a1")
    assert admin.post(
        "/api/control/devices/00000000-0000-4000-8000-ffffffffffff/rotate", json={}
    ).status_code == 404
    # 先撤销 → 轮换 404（不能轮换已撤销凭证）
    w.platform.delete(f"/api/control/devices/{w.devices['b1']['device_id']}")
    res = w.platform.post(
        f"/api/control/devices/{w.devices['b1']['device_id']}/rotate", json={}
    )
    assert res.status_code == 404


# --------------------------------------------------------------------------- #
# HTML 去注入（§6.1：/mobile 仍公开，但不再携带共享 token meta）
# --------------------------------------------------------------------------- #
def test_html_pages_no_longer_inject_shared_token_meta(w):
    c = TestClient(w.app)
    for path in ("/legacy", "/mobile", "/mobile/record", "/", "/pc"):
        res = c.get(path)
        assert res.status_code == 200, path
        assert "mousevision-api-token" not in res.text, f"{path} 不得注入共享 token meta"
        assert tf.LEGACY_TOKEN not in res.text, f"{path} 不得出现共享令牌明文"


# --------------------------------------------------------------------------- #
# /api/records/report 的 JSON 通道（legacy v1 outbox 兼容 flush 载荷）
# --------------------------------------------------------------------------- #
def _tiny_jpeg() -> str:
    import cv2

    img = np.full((8, 8, 3), 120, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")


def test_report_json_payload_device_credential_tenant_wins_over_body(w):
    """批次快照 tenant_id 只是客户端自证：租户只来自凭证（§4.3）。"""
    payload = {
        "tenant_id": w.tid("b1"),  # 冒报其他租户 → 必须被忽略
        "cage_id": "C57-900",
        "project_id": "default",
        "device_id": "dev-json",
        "records": [
            {"record_id": "rec-json-1", "ordinal": 1, "weight_g": 22.5},
        ],
    }
    res = TestClient(w.app).post(
        "/api/records/report",
        json=payload,
        headers=w.device_headers("a1"),
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["count"] == 1
    # 记录必须落在 a1 的租户目录，而不是 body 声称的 b1
    a1_root = w.tenant_dir("a1")
    hits = list(a1_root.glob("run_*/mouse_001/record.json"))
    assert hits, "记录必须落盘 a1 租户根"
    rec = json.loads(hits[0].read_text())
    assert rec["record_id"] == "rec-json-1"
    b1_root = w.tenant_dir("b1")
    assert not list(b1_root.glob("run_*/mouse_*/record.json"))


def test_report_json_payload_with_embedded_photo_dataurl(w):
    payload = {
        "tenant_id": w.tid("a1"),
        "cage_id": "C57-901",
        "records": [
            {
                "record_id": "rec-json-photo",
                "ordinal": 1,
                "weight_g": 20.0,
                "photo": _tiny_jpeg(),
            },
        ],
    }
    res = TestClient(w.app).post(
        "/api/records/report", json=payload, headers=w.device_headers("a1")
    )
    assert res.status_code == 201, res.text
    assert res.json()["photos_uploaded"] == 1
    photo = next((w.tenant_dir("a1")).glob("run_*/mouse_001/photo.jpg"))
    assert photo.read_bytes().startswith(b"\xff\xd8\xff"), "dataURL 解码后必须是原 JPEG 字节"
    # record.json 不泄漏 dataURL（_validate_records 白名单丢弃 photo 键）
    rec = json.loads(photo.parent.joinpath("record.json").read_text())
    assert "photo_data" not in json.dumps(rec)
    assert rec["photo_source"] == "device_capture"


def test_report_json_payload_legacy_token_lands_legacy_default(w):
    payload = {
        "tenant_id": tf.LEGACY_TENANT_ID,
        "cage_id": "C57-902",
        "records": [{"record_id": "rec-json-legacy", "ordinal": 1, "weight_g": 19.9}],
    }
    headers = {"X-MouseVision-Token": tf.LEGACY_TOKEN}
    res = TestClient(w.app).post("/api/records/report", json=payload, headers=headers)
    assert res.status_code == 201, res.text
    legacy_root = w.output / "tenants" / tf.LEGACY_TENANT_ID
    hits = list(legacy_root.glob("run_*/mouse_001/record.json"))
    assert hits and json.loads(hits[0].read_text())["record_id"] == "rec-json-legacy"


def test_report_json_bad_payloads_rejected(w):
    headers = w.device_headers("a1")
    c = TestClient(w.app)
    assert c.post(
        "/api/records/report",
        content=b"not-json{",
        headers={**headers, "Content-Type": "application/json"},
    ).status_code == 400
    assert c.post(
        "/api/records/report",
        json={"records": []},
        headers=headers,
    ).status_code == 400
    # 缺 records
    assert c.post(
        "/api/records/report",
        json={"cage_id": "C1"},
        headers=headers,
    ).status_code == 400


def test_report_multipart_still_works_after_json_branch(w):
    """既有 multipart 主通道零回归（照片走文件字段）。"""
    files = [("photos", ("rec-mp.jpg", b"\xff\xd8\xffgarbage", "image/jpeg"))]
    res = TestClient(w.app).post(
        "/api/records/report",
        data={
            "cage_id": "C57-903",
            "records": json.dumps(
                [{"record_id": "rec-mp", "ordinal": 1, "weight_g": 21.0}]
            ),
        },
        files=files,
        headers=w.device_headers("a1"),
    )
    assert res.status_code == 201, res.text
    # 非法 JPEG 魔数之外的字节 → 服务端占位兜底，记录仍落盘
    rec = next((w.tenant_dir("a1")).glob("run_*/mouse_001/record.json"))
    assert json.loads(rec.read_text())["record_id"] == "rec-mp"
