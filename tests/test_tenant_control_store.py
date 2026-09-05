"""控制面存储契约（合同 §4.1 / §15-B2）：schema、迁移幂等、seed、角色约束。

B2 范围——本批结束时应全部转绿。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import tenant_fixture as tf
from tenant_fixture import PLATFORM_ADMIN_PW
from tenant_fixture import ctl  # noqa: F401 - pytest fixture 注册

LEGACY_TENANT_ID = tf.LEGACY_TENANT_ID


def _meta_version(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def test_schema_created_with_version(ctl, tmp_path):
    """空库建表并写入 schema_version=1；核心表齐全。"""
    db_path = Path(ctl.db_path)
    assert _meta_version(db_path) == 1
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    finally:
        conn.close()
    for table in (
        "accounts",
        "tenants",
        "users",
        "memberships",
        "account_owners",
        "device_credentials",
        "sessions",
        "device_bind_codes",
        "platform_admins",
        "meta",
    ):
        assert table in tables, f"缺少控制面表 {table}"


def test_reopen_is_idempotent_same_version(ctl):
    """重复启动：版本不变、seed 不重复（幂等）。"""
    first = _meta_version(Path(ctl.db_path))
    users_first = ctl.count()
    from ui.control_store import ControlStore

    again = ControlStore(str(ctl.db_path))
    assert _meta_version(Path(ctl.db_path)) == first
    assert again.count() == users_first
    assert ctl.get_tenant(LEGACY_TENANT_ID) is not None
    assert again.get_tenant(LEGACY_TENANT_ID) is not None


def test_downgrade_rejected_with_clear_error(ctl, tmp_path):
    """库版本比代码新 → 明确报错，不静默继续。"""
    db_path = Path(ctl.db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        conn.commit()
    finally:
        conn.close()
    from ui.control_store import ControlStore, ControlStoreError

    with pytest.raises(ControlStoreError) as exc:
        ControlStore(str(db_path))
    assert "99" in str(exc.value)


def test_half_written_schema_repaired_on_reopen(ctl):
    """半写库（meta 存在但缺表）→ 重新打开时按幂等迁移补齐，不崩溃。"""
    db_path = Path(ctl.db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TABLE memberships")
        conn.commit()
    finally:
        conn.close()
    from ui.control_store import ControlStore

    again = ControlStore(str(db_path))
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    finally:
        conn.close()
    assert "memberships" in tables
    # 补齐后功能可用
    account = again.create_account("Repair Lab")
    tenant = again.create_tenant(account["id"], "T")
    assert tenant["account_id"] == account["id"]


def test_tenants_have_no_parent_column(ctl):
    """合同 §4：不建 parent_tenant_id / tenant_relations（两层固定）。"""
    conn = sqlite3.connect(Path(ctl.db_path))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tenants)").fetchall()}
    finally:
        conn.close()
    assert "parent_tenant_id" not in cols
    assert "account_id" in cols
    assert "slug" in cols
    assert "status" in cols
    conn = sqlite3.connect(Path(ctl.db_path))
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    finally:
        conn.close()
    assert "tenant_relations" not in tables


def test_seed_admin_bootstrap(ctl):
    """空库 seed：platform admin + seed account + 固定 UUID legacy-default 租户。"""
    admin = ctl.get_user_by_username("admin")
    assert admin is not None
    assert ctl.is_platform_admin(admin["id"])
    assert admin["must_change_password"] in (1, True)
    legacy = ctl.get_tenant(LEGACY_TENANT_ID)
    assert legacy is not None
    assert legacy["slug"] == "legacy-default"
    # seed admin 是 legacy-default 的 tenant_admin（合同 §5.1.3）
    membership = ctl.get_membership(admin["id"], LEGACY_TENANT_ID)
    assert membership is not None and membership["role"] == "tenant_admin"
    # seed admin 是 seed account 的 parent_owner
    accounts = ctl.accounts_for_user(admin["id"])
    assert any(a["id"] == legacy["account_id"] for a in accounts)
    # 密码可验证（PBKDF2 260k 兼容）
    assert ctl.authenticate("admin", PLATFORM_ADMIN_PW) is not None
    assert ctl.authenticate("admin", "wrong-password") is None


def test_seed_print_uses_configured_password_without_leaking(capsys, ctl):
    """配置了 MOUSEVISION_ADMIN_PASSWORD 时不打印随机密码。"""
    out = capsys.readouterr().out
    assert "已创建管理员 admin" in out
    assert PLATFORM_ADMIN_PW not in out


def test_membership_unique_and_roles_validated(ctl):
    account = ctl.create_account("M Lab")
    tenant = ctl.create_tenant(account["id"], "T1")
    user = ctl.create_user("member-1", "password-123")
    ctl.add_membership(user["id"], tenant["id"], "operator")
    with pytest.raises(KeyError):
        ctl.add_membership(user["id"], tenant["id"], "viewer")
    with pytest.raises(ValueError):
        ctl.add_membership(user["id"], ctl.create_tenant(account["id"], "T2")["id"], "root")


def test_device_credential_cannot_rebind_tenant(ctl):
    """凭证 tenant_id 固定不可改绑（合同 §6.2）。"""
    account = ctl.create_account("D Lab")
    t1 = ctl.create_tenant(account["id"], "T1")
    t2 = ctl.create_tenant(account["id"], "T2")
    issued = ctl.issue_device_credential(t1["id"], device_label="phone-1")
    with pytest.raises(ValueError):
        ctl.update_device(issued["device_id"], tenant_id=t2["id"])
    still = ctl.authenticate_device(issued["token"])
    assert still is not None and still["tenant_id"] == t1["id"]
