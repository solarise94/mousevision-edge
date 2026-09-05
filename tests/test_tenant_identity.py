"""身份与凭证安全契约（合同 §4.1 / §6.2 / §15-B2）。

- 会话 / 设备 / 绑定码 token 在库里只有 salted hash（明文仅签发时返回一次）。
- user_secret_version：改密 / 禁用后旧会话失效；凭证撤销后旧设备 token 失效。
- 绑定码：短 TTL 过期失效、单次消费、重放拒绝。

B2 范围——本批结束时应全部转绿。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import tenant_fixture as tf
from tenant_fixture import ctl, world  # noqa: F401 - pytest fixture 注册


def _seed_tenant(ctl, name="TI"):
    account = ctl.create_account(f"acct-{name}")
    return ctl.create_tenant(account["id"], name)


def _dump_all_cells(db_path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        return [str(cell) for row in rows for cell in row if cell is not None]
    finally:
        conn.close()


# ------------------------------------------------------------------ #
# 只存哈希
# ------------------------------------------------------------------ #
def test_session_token_stored_hashed_only(ctl):
    tenant = _seed_tenant(ctl)
    user = ctl.create_user("hash-user-1", "password-123")
    token = ctl.create_session(user["id"], active_tenant_id=tenant["id"])
    assert token
    cells = _dump_all_cells(Path(ctl.db_path), "sessions")
    assert token not in cells, "会话明文 token 不得入库"
    # 解析仍可用
    resolved = ctl.resolve_session(token)
    assert resolved is not None and resolved["user"]["id"] == user["id"]


def test_device_token_stored_hashed_only(ctl):
    tenant = _seed_tenant(ctl)
    issued = ctl.issue_device_credential(tenant["id"], device_label="phone-hash")
    cells = _dump_all_cells(Path(ctl.db_path), "device_credentials")
    assert issued["token"] not in cells, "设备凭证明文不得入库"
    assert ctl.authenticate_device(issued["token"]) is not None


def test_bind_code_stored_hashed_only(ctl):
    tenant = _seed_tenant(ctl)
    code_row = ctl.create_bind_code(tenant["id"], ttl_seconds=300, created_by="admin-a1")
    cells = _dump_all_cells(Path(ctl.db_path), "device_bind_codes")
    assert code_row["code"] not in cells, "绑定码明文不得入库"
    assert ctl.consume_bind_code(code_row["code"]) is not None


def test_token_hashes_are_salted(ctl):
    """同一 token 值在不同行中应因 salt 不同而哈希不同（防彩虹表）。"""
    tenant = _seed_tenant(ctl)
    d1 = ctl.issue_device_credential(tenant["id"], device_label="d1")
    d2 = ctl.issue_device_credential(tenant["id"], device_label="d2")
    conn = sqlite3.connect(Path(ctl.db_path))
    try:
        rows = conn.execute(
            "SELECT token_hash, token_salt FROM device_credentials ORDER BY created_at"
        ).fetchall()
    finally:
        conn.close()
    salts = {r[1] for r in rows}
    assert len(rows) == 2 and len(salts) == 2, "每行应有独立随机 salt"
    assert d1["token"] != d2["token"]
    assert rows[0][0] != rows[1][0]


def test_pbkdf2_260k_password_compat(ctl):
    """control.db 密码沿用 ui.users 的 PBKDF2(260k) 实现。"""
    from ui.users import PBKDF2_ITERATIONS, hash_password, verify_password

    assert PBKDF2_ITERATIONS == 260_000
    user = ctl.create_user("pbkdf-user", "password-123")
    stored = ctl.get_user_by_username("pbkdf-user")
    ok = verify_password("password-123", stored["_password_hash"], stored["_salt"])
    assert ok
    # 与 ui.users.hash_password 产出的格式一致（可互验）
    pw_hash, salt = hash_password("password-123")
    assert verify_password("password-123", pw_hash, salt)
    assert user["id"] == stored["id"]


# ------------------------------------------------------------------ #
# user_secret_version 撤销链
# ------------------------------------------------------------------ #
def test_password_change_invalidates_old_session(ctl):
    tenant = _seed_tenant(ctl)
    user = ctl.create_user("pwrotate", "old-password-1")
    ctl.add_membership(user["id"], tenant["id"], "operator")
    token = ctl.create_session(user["id"], active_tenant_id=tenant["id"])
    before = ctl.get_user(user["id"])["secret_version"]

    ctl.update_user(user["id"], password="new-password-2")

    assert ctl.resolve_session(token) is None, "改密后旧会话必须失效"
    assert ctl.get_user(user["id"])["secret_version"] == before + 1
    # 新会话可用
    token2 = ctl.create_session(user["id"], active_tenant_id=tenant["id"])
    assert ctl.resolve_session(token2) is not None


def test_disable_user_invalidates_old_session(ctl):
    tenant = _seed_tenant(ctl)
    user = ctl.create_user("disable-me", "password-123")
    token = ctl.create_session(user["id"], active_tenant_id=tenant["id"])
    ctl.update_user(user["id"], disabled=True)
    assert ctl.resolve_session(token) is None, "禁用后旧会话必须失效"
    # 重新启用也不得复活旧会话（secret_version 已前移）
    ctl.update_user(user["id"], disabled=False)
    assert ctl.resolve_session(token) is None


def test_revoked_device_credential_rejected(ctl):
    tenant = _seed_tenant(ctl)
    issued = ctl.issue_device_credential(tenant["id"], device_label="phone-revoke")
    assert ctl.authenticate_device(issued["token"]) is not None
    ctl.revoke_device_credential(issued["device_id"])
    assert ctl.authenticate_device(issued["token"]) is None, "撤销后旧设备凭证必须失效"


def test_device_last_used_tracked(ctl):
    tenant = _seed_tenant(ctl)
    issued = ctl.issue_device_credential(tenant["id"], device_label="phone-lu")
    assert ctl.authenticate_device(issued["token"]) is not None
    conn = sqlite3.connect(Path(ctl.db_path))
    try:
        row = conn.execute(
            "SELECT last_used_at FROM device_credentials WHERE id=?", (issued["device_id"],)
        ).fetchone()
    finally:
        conn.close()
    assert row and row[0], "认证成功应记录 last_used_at"


# ------------------------------------------------------------------ #
# 绑定码：TTL / 单次消费 / 重放
# ------------------------------------------------------------------ #
def test_bind_code_single_use_and_replay_rejected(ctl):
    tenant = _seed_tenant(ctl)
    row = ctl.create_bind_code(tenant["id"], ttl_seconds=300, created_by="admin-a1")
    first = ctl.consume_bind_code(row["code"])
    assert first is not None and first["tenant_id"] == tenant["id"]
    assert ctl.consume_bind_code(row["code"]) is None, "绑定码重放必须被拒绝"
    assert ctl.consume_bind_code(row["code"]) is None


def test_bind_code_expired_rejected(ctl):
    tenant = _seed_tenant(ctl)
    row = ctl.create_bind_code(tenant["id"], ttl_seconds=300, created_by="admin-a1")
    # 直接把 expires_at 改到过去（避免真实 sleep）
    conn = sqlite3.connect(Path(ctl.db_path))
    try:
        conn.execute("UPDATE device_bind_codes SET expires_at='2000-01-01T00:00:00'")
        conn.commit()
    finally:
        conn.close()
    assert ctl.consume_bind_code(row["code"]) is None, "过期绑定码必须失效"


def test_bind_code_binds_device_to_its_tenant(ctl):
    """消费绑定码 → 签发的设备凭证落在码所属租户，与请求方无关。"""
    account = ctl.create_account("bind-acct")
    t1 = ctl.create_tenant(account["id"], "B1")
    t2 = ctl.create_tenant(account["id"], "B2")
    row = ctl.create_bind_code(t1["id"], ttl_seconds=300, created_by="admin-b1")
    # 消费方完全不需要（也不允许）声明 tenant_id
    issued = ctl.consume_and_issue_device(row["code"], device_label="phone-bind")
    assert issued["tenant_id"] == t1["id"]
    assert issued["tenant_id"] != t2["id"]
    assert ctl.authenticate_device(issued["token"])["tenant_id"] == t1["id"]


# ------------------------------------------------------------------ #
# API 层撤销链（经 /api/me/password 与控制面设备端点）
# ------------------------------------------------------------------ #
def test_change_password_api_revokes_old_session(world):
    op = world.member_client("op-a1", "op-a1", tf.OPERATOR_PW, "a1")
    old_cookie = op.cookies.get("mv_session")
    r = op.post(
        "/api/me/password",
        json={"current_password": tf.OPERATOR_PW, "new_password": "op-a1-new-password"},
    )
    assert r.status_code == 200, r.text

    fresh = TestClient(world.app)
    r = fresh.get("/api/me", cookies={"mv_session": old_cookie})
    assert r.status_code == 200
    assert r.json()["authenticated"] is False, "改密后旧会话必须失效（API 层）"


def test_revoke_device_api_kills_credential(world):
    r = world.platform.post(
        f"/api/control/tenants/{world.tid('a1')}/devices",
        json={"device_label": "phone-to-revoke"},
    )
    assert r.status_code in (200, 201), r.text
    device = r.json()
    r = world.platform.delete(f"/api/control/devices/{device['device_id']}")
    assert r.status_code == 200, r.text
    from ui.tenant_context import ContextResolver

    resolver = ContextResolver(world.control, world.output)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        resolver.resolve(_req_with_token(device["token"]))
    assert exc.value.status_code == 401


def _req_with_token(token: str):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [(b"x-mousevision-token", token.encode())],
        "client": ("test", 1),
        "server": ("test", 80),
    }
    return Request(scope)
