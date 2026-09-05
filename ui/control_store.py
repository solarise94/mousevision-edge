"""控制面存储（合同 docs/UPGRADE_TENANT_ISOLATION.md §4.1 / §15-B2）。

全站一份 ``<output>/control/control.db``：accounts / tenants / users /
memberships / account_owners / sessions / device_credentials /
device_bind_codes（+ platform_admins，平台角色另表保存，§5.1.3）。

设计要点：
- schema_version 单调向前、事务化迁移、重复启动幂等；迁移语句必须可重复执行
  （CREATE TABLE IF NOT EXISTS / 带 IF NOT EXISTS 语义），半写库重开时自动补齐。
- 沿用 ui.users 的 SQLite 风格：每次操作独立连接、WAL、threading.Lock。
- 密码复用 ui.users 的 PBKDF2(260k) hash/verify（260k 迭代兼容）。
  （为避免 ui.users ↔ ui.control_store 循环导入，这里在函数内延迟导入。）
- 会话 / 设备凭证 / 绑定码 token 只存 salted SHA-256；明文仅在签发时返回一次。
  这些 token 都是 ≥192bit 随机值，快速哈希即可（PBKDF2 只用于低熵密码）。
- 设备凭证 tenant_id 固定不可改绑；绑定码短 TTL、单次消费。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# 合同 §16-G5 固定的 legacy-default 租户 UUID。
LEGACY_TENANT_ID = "00000000-0000-4000-8000-000000000001"
LEGACY_TENANT_SLUG = "legacy-default"

TENANT_ROLES = frozenset({"tenant_admin", "operator", "viewer"})
DEVICE_TOKEN_PREFIX = "mvdev_"
DEFAULT_BIND_CODE_TTL_SECONDS = 300
MAX_BIND_CODE_TTL_SECONDS = 600

SESSION_DAYS = 7


class ControlStoreError(RuntimeError):
    """控制面库级错误（schema 版本不匹配、迁移失败等），报错信息面向运维。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------ #
# token 哈希（只存哈希，明文仅签发一次）
# ------------------------------------------------------------------ #
# salt 由 token 本身派生（每个 token salt 不同）：仍防彩虹表，同时允许按
# 哈希 O(1) 直查（token 是 ≥192bit 随机值，暴力枚举不可行；PBKDF2 只用于
# 低熵密码，见 _hash_password）。
def _derived_salt(secret: str) -> str:
    return hashlib.sha256(b"mv-token-salt:" + secret.encode("utf-8")).hexdigest()[:16]


def new_token(prefix: str = "") -> str:
    return prefix + secrets.token_urlsafe(24)


def hash_token(secret: str, *, salt: str | None = None) -> tuple[str, str]:
    salt_val = salt or _derived_salt(secret)
    digest = hashlib.sha256(f"{salt_val}:{secret}".encode("utf-8")).hexdigest()
    return digest, salt_val


def verify_token(secret: str, token_hash: str, salt: str) -> bool:
    candidate, _ = hash_token(secret, salt=salt)
    return hmac.compare_digest(candidate, token_hash)


# ------------------------------------------------------------------ #
# schema 迁移（单调向前、幂等、事务化）
# ------------------------------------------------------------------ #
_SCHEMA_V1: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS accounts (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tenants (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id),
        name TEXT NOT NULL,
        slug TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        UNIQUE(account_id, slug)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        salt TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        must_change_password INTEGER NOT NULL DEFAULT 0,
        disabled INTEGER NOT NULL DEFAULT 0,
        secret_version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memberships (
        user_id TEXT NOT NULL REFERENCES users(id),
        tenant_id TEXT NOT NULL REFERENCES tenants(id),
        role TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(user_id, tenant_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS account_owners (
        user_id TEXT NOT NULL REFERENCES users(id),
        account_id TEXT NOT NULL REFERENCES accounts(id),
        role TEXT NOT NULL DEFAULT 'parent_owner',
        created_at TEXT NOT NULL,
        UNIQUE(user_id, account_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS platform_admins (
        user_id TEXT PRIMARY KEY REFERENCES users(id),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS device_credentials (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants(id),
        token_hash TEXT NOT NULL UNIQUE,
        token_salt TEXT NOT NULL,
        device_label TEXT NOT NULL DEFAULT '',
        revoked_at TEXT,
        last_used_at TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        token_hash TEXT PRIMARY KEY,
        token_salt TEXT NOT NULL,
        user_id TEXT NOT NULL REFERENCES users(id),
        account_id TEXT,
        active_tenant_id TEXT,
        user_secret_version INTEGER NOT NULL DEFAULT 1,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS device_bind_codes (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants(id),
        code_hash TEXT NOT NULL,
        code_salt TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at TEXT,
        created_by TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )
    """,
)

# 每个迁移都是 (version, 幂等语句)。追加 schema 时只增不改。
_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = ((1, _SCHEMA_V1),)
SCHEMA_VERSION = _MIGRATIONS[-1][0]

_EXPECTED_TABLES = frozenset(
    {
        "meta",
        "accounts",
        "tenants",
        "users",
        "memberships",
        "account_owners",
        "platform_admins",
        "device_credentials",
        "sessions",
        "device_bind_codes",
    }
)


class ControlStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self._migrate()
        self._seed()

    # ---- 连接 / 迁移 -------------------------------------------------- #
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _stored_version(self, conn: sqlite3.Connection) -> int:
        has_meta = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if has_meta is None:
            return 0
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row["value"]) if row else 0

    def _migrate(self) -> None:
        with self.lock:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                current = self._stored_version(conn)
                if current > SCHEMA_VERSION:
                    raise ControlStoreError(
                        f"control.db schema_version={current} 比当前程序支持的 "
                        f"{SCHEMA_VERSION} 更新：请先升级 MouseVision 再启动（拒绝降级运行）"
                    )
                # 幂等迁移：先补齐 current 之后的事务化迁移；
                # 再校验期望表齐全，缺表（半写库）则重放全部幂等语句修复。
                for version, statements in _MIGRATIONS:
                    if version <= current:
                        continue
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        for stmt in statements:
                            conn.execute(stmt)
                        conn.execute(
                            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (str(version),),
                        )
                        conn.execute("COMMIT")
                    except Exception as exc:
                        conn.execute("ROLLBACK")
                        raise ControlStoreError(
                            f"control.db 迁移到 v{version} 失败：{exc}"
                        ) from exc
                missing = self._missing_tables(conn)
                if missing:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        for _, statements in _MIGRATIONS:
                            for stmt in statements:
                                conn.execute(stmt)
                        conn.execute(
                            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (str(SCHEMA_VERSION),),
                        )
                        conn.execute("COMMIT")
                    except Exception as exc:
                        conn.execute("ROLLBACK")
                        raise ControlStoreError(
                            f"control.db 半写库修复失败（缺 {sorted(missing)}）：{exc}"
                        ) from exc
            finally:
                conn.close()

    def _missing_tables(self, conn: sqlite3.Connection) -> set[str]:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        present = {r["name"] for r in rows}
        return _EXPECTED_TABLES - present

    def schema_version(self) -> int:
        with self.lock:
            conn = self._connect()
            try:
                return self._stored_version(conn)
            finally:
                conn.close()

    # ------------------------------------------------------------------ #
    # users
    # ------------------------------------------------------------------ #
    @staticmethod
    def _user_public(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "username": row["username"],
            "display_name": row["display_name"] or row["username"],
            "must_change_password": bool(row["must_change_password"]),
            "disabled": bool(row["disabled"]),
            "secret_version": int(row["secret_version"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _hash_password(self, password: str) -> tuple[str, str]:
        # 延迟导入：ui.users 顶层 import 本模块（兼容门面），避免循环。
        from ui.users import hash_password

        return hash_password(password)

    def _verify_password(self, password: str, password_hash: str, salt: str) -> bool:
        from ui.users import verify_password

        return verify_password(password, password_hash, salt)

    def count(self) -> int:
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
                return int(row["n"] if row else 0)
            finally:
                conn.close()

    def create_user(
        self,
        username: str,
        password: str,
        *,
        display_name: str = "",
        must_change_password: int = 0,
    ) -> dict[str, Any]:
        user_id = str(uuid.uuid4())
        pw_hash, salt = self._hash_password(password)
        now = _now()
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO users (
                        id, username, password_hash, salt, display_name,
                        must_change_password, disabled, secret_version,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, 1, ?, ?)
                    """,
                    (
                        user_id,
                        username,
                        pw_hash,
                        salt,
                        display_name or username,
                        int(must_change_password),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise KeyError(f"username exists: {username}") from exc
            finally:
                conn.close()
        return self.get_user(user_id)  # type: ignore[return-value]

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
                return self._user_public(row) if row else None
            finally:
                conn.close()

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM users WHERE username = ?", (username,)
                ).fetchone()
                if row is None:
                    return None
                data = self._user_public(row)
                data["_password_hash"] = row["password_hash"]
                data["_salt"] = row["salt"]
                return data
            finally:
                conn.close()

    def list_users(self) -> list[dict[str, Any]]:
        with self.lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
                return [self._user_public(r) for r in rows]
            finally:
                conn.close()

    def update_user(
        self,
        user_id: str,
        *,
        password: str | None = None,
        display_name: str | None = None,
        disabled: bool | None = None,
        must_change_password: int | None = None,
    ) -> dict[str, Any]:
        """更新用户；改密或禁用 → secret_version+1 并吊销其全部会话。"""
        current = self.get_user(user_id)
        if current is None:
            raise KeyError(user_id)
        updates: dict[str, Any] = {}
        if display_name is not None:
            updates["display_name"] = display_name
        if must_change_password is not None:
            updates["must_change_password"] = int(must_change_password)
        bump_secret = False
        if disabled is not None:
            updates["disabled"] = int(disabled)
            if disabled:
                bump_secret = True
        if password is not None:
            pw_hash, salt = self._hash_password(password)
            updates["password_hash"] = pw_hash
            updates["salt"] = salt
            updates["must_change_password"] = 0
            bump_secret = True
        if not updates:
            return current
        updates["updated_at"] = _now()
        if bump_secret:
            updates["secret_version"] = int(current["secret_version"]) + 1
        with self.lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    "UPDATE users SET "
                    + ", ".join(f"{name} = ?" for name in updates)
                    + " WHERE id = ?",
                    (*updates.values(), user_id),
                )
                if cur.rowcount == 0:
                    conn.execute("ROLLBACK")
                    raise KeyError(user_id)
                if bump_secret:
                    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise
            finally:
                conn.close()
        return self.get_user(user_id)  # type: ignore[return-value]

    def delete_user(self, user_id: str) -> None:
        with self.lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM memberships WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM account_owners WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM platform_admins WHERE user_id = ?", (user_id,))
                cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
                if cur.rowcount == 0:
                    conn.execute("ROLLBACK")
                    raise KeyError(user_id)
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise
            finally:
                conn.close()

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        user = self.get_user_by_username(username)
        if user is None or user.get("disabled"):
            return None
        if not self._verify_password(password, user["_password_hash"], user["_salt"]):
            return None
        return {k: v for k, v in user.items() if not k.startswith("_")}

    # ---- 平台管理员 --------------------------------------------------- #
    def set_platform_admin(self, user_id: str, on: bool = True) -> None:
        with self.lock:
            conn = self._connect()
            try:
                if on:
                    conn.execute(
                        "INSERT OR IGNORE INTO platform_admins (user_id, created_at) VALUES (?, ?)",
                        (user_id, _now()),
                    )
                else:
                    conn.execute("DELETE FROM platform_admins WHERE user_id = ?", (user_id,))
            finally:
                conn.close()

    def is_platform_admin(self, user_id: str) -> bool:
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT 1 FROM platform_admins WHERE user_id = ?", (user_id,)
                ).fetchone()
                return row is not None
            finally:
                conn.close()

    # ------------------------------------------------------------------ #
    # accounts / account_owners
    # ------------------------------------------------------------------ #
    @staticmethod
    def _account_public(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def create_account(
        self, name: str, *, owner_user_id: str | None = None
    ) -> dict[str, Any]:
        account_id = str(uuid.uuid4())
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO accounts (id, name, status, created_at) VALUES (?, ?, 'active', ?)",
                    (account_id, name, _now()),
                )
                if owner_user_id is not None:
                    conn.execute(
                        "INSERT OR IGNORE INTO account_owners (user_id, account_id, role, created_at) "
                        "VALUES (?, ?, 'parent_owner', ?)",
                        (owner_user_id, account_id, _now()),
                    )
            finally:
                conn.close()
        return self.get_account(account_id)  # type: ignore[return-value]

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM accounts WHERE id = ?", (account_id,)
                ).fetchone()
                return self._account_public(row) if row else None
            finally:
                conn.close()

    def list_accounts(self) -> list[dict[str, Any]]:
        with self.lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT * FROM accounts ORDER BY created_at ASC").fetchall()
                return [self._account_public(r) for r in rows]
            finally:
                conn.close()

    def add_account_owner(
        self, user_id: str, account_id: str, *, role: str = "parent_owner"
    ) -> None:
        if role != "parent_owner":
            raise ValueError(f"invalid account-owner role: {role}")
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO account_owners (user_id, account_id, role, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (user_id, account_id, role, _now()),
                )
            finally:
                conn.close()

    def is_parent_owner(self, user_id: str, account_id: str) -> bool:
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT 1 FROM account_owners WHERE user_id = ? AND account_id = ? AND role = 'parent_owner'",
                    (user_id, account_id),
                ).fetchone()
                return row is not None
            finally:
                conn.close()

    def accounts_for_user(self, user_id: str) -> list[dict[str, Any]]:
        with self.lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT a.* FROM accounts a
                    JOIN account_owners o ON o.account_id = a.id
                    WHERE o.user_id = ? ORDER BY a.created_at ASC
                    """,
                    (user_id,),
                ).fetchall()
                return [self._account_public(r) for r in rows]
            finally:
                conn.close()

    # ------------------------------------------------------------------ #
    # tenants
    # ------------------------------------------------------------------ #
    @staticmethod
    def _tenant_public(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _validate_tenant_id(tenant_id: str) -> str:
        try:
            uuid.UUID(str(tenant_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError(f"invalid tenant id: {tenant_id!r}") from exc
        return str(tenant_id)

    def create_tenant(
        self,
        account_id: str,
        name: str,
        slug: str | None = None,
        *,
        tenant_id: str | None = None,
        status: str = "active",
    ) -> dict[str, Any]:
        tid = self._validate_tenant_id(tenant_id or str(uuid.uuid4()))
        slug = (slug or name).strip().lower().replace(" ", "-") or "tenant"
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO tenants (id, account_id, name, slug, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (tid, account_id, name, slug, status, _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise KeyError(f"tenant exists (same account/slug/id): {exc}") from exc
            finally:
                conn.close()
        return self.get_tenant(tid)  # type: ignore[return-value]

    def get_tenant(self, tenant_id: str) -> dict[str, Any] | None:
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM tenants WHERE id = ?", (tenant_id,)
                ).fetchone()
                return self._tenant_public(row) if row else None
            finally:
                conn.close()

    def list_tenants(self, account_id: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            conn = self._connect()
            try:
                if account_id is None:
                    rows = conn.execute(
                        "SELECT * FROM tenants ORDER BY created_at ASC"
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM tenants WHERE account_id = ? ORDER BY created_at ASC",
                        (account_id,),
                    ).fetchall()
                return [self._tenant_public(r) for r in rows]
            finally:
                conn.close()

    def ensure_legacy_default(self, account_id: str | None = None) -> dict[str, Any]:
        """幂等确保 legacy-default 租户存在（固定 UUID，§5.1）。"""
        existing = self.get_tenant(LEGACY_TENANT_ID)
        if existing is not None:
            return existing
        if account_id is None:
            accounts = self.list_accounts()
            if not accounts:
                account_id = self.create_account("MouseVision")["id"]
            else:
                account_id = accounts[0]["id"]
        try:
            return self.create_tenant(
                account_id,
                "Legacy Default",
                LEGACY_TENANT_SLUG,
                tenant_id=LEGACY_TENANT_ID,
            )
        except KeyError:
            existing = self.get_tenant(LEGACY_TENANT_ID)
            assert existing is not None
            return existing

    # ------------------------------------------------------------------ #
    # memberships
    # ------------------------------------------------------------------ #
    def set_tenant_status(self, tenant_id: str, status: str) -> None:
        """租户状态机（active|paused，租户 reset 窗口用，§15-B3）。

        paused 期间 ContextResolver 拒绝为该租户产出设备上下文、会话上下文
        回落 account 级 —— 从解析层阻止新写入。
        """
        tid = self._validate_tenant_id(tenant_id)
        if status not in {"active", "paused"}:
            raise ValueError(f"invalid tenant status: {status}")
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE tenants SET status = ? WHERE id = ?", (status, tid)
                )
                conn.commit()
            finally:
                conn.close()

    def add_membership(self, user_id: str, tenant_id: str, role: str) -> dict[str, Any]:
        if role not in TENANT_ROLES:
            raise ValueError(f"invalid tenant role: {role}")
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO memberships (user_id, tenant_id, role, created_at) VALUES (?, ?, ?, ?)",
                    (user_id, tenant_id, role, _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise KeyError(f"membership exists: {user_id} @ {tenant_id}") from exc
            finally:
                conn.close()
        return self.get_membership(user_id, tenant_id)  # type: ignore[return-value]

    def remove_membership(self, user_id: str, tenant_id: str) -> None:
        with self.lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "DELETE FROM memberships WHERE user_id = ? AND tenant_id = ?",
                    (user_id, tenant_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(f"membership missing: {user_id} @ {tenant_id}")
            finally:
                conn.close()

    def set_membership_role(self, user_id: str, tenant_id: str, role: str) -> None:
        if role not in TENANT_ROLES:
            raise ValueError(f"invalid tenant role: {role}")
        with self.lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "UPDATE memberships SET role = ? WHERE user_id = ? AND tenant_id = ?",
                    (role, user_id, tenant_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(f"membership missing: {user_id} @ {tenant_id}")
            finally:
                conn.close()

    def get_membership(self, user_id: str, tenant_id: str) -> dict[str, Any] | None:
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM memberships WHERE user_id = ? AND tenant_id = ?",
                    (user_id, tenant_id),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def list_user_tenants(self, user_id: str) -> list[dict[str, Any]]:
        """用户可进入的租户列表（membership ∪ 经 parent_owner 的 account 租户）。"""
        with self.lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT t.id AS tenant_id, t.name, t.slug, t.account_id, t.status,
                           m.role AS role, 'member' AS via
                    FROM memberships m JOIN tenants t ON t.id = m.tenant_id
                    WHERE m.user_id = ?
                    UNION
                    SELECT t.id AS tenant_id, t.name, t.slug, t.account_id, t.status,
                           'parent_owner' AS role, 'account' AS via
                    FROM tenants t
                    JOIN account_owners o ON o.account_id = t.account_id
                    WHERE o.user_id = ? AND o.role = 'parent_owner'
                    """,
                    (user_id, user_id),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def list_tenant_members(self, tenant_id: str) -> list[dict[str, Any]]:
        with self.lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT m.user_id, u.username, u.display_name, m.role, m.created_at
                    FROM memberships m JOIN users u ON u.id = m.user_id
                    WHERE m.tenant_id = ? ORDER BY m.created_at ASC
                    """,
                    (tenant_id,),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    # ------------------------------------------------------------------ #
    # sessions（token 只存哈希；user_secret_version 撤销链）
    # ------------------------------------------------------------------ #
    def create_session(
        self,
        user_id: str,
        *,
        account_id: str | None = None,
        active_tenant_id: str | None = None,
        ttl_days: int = SESSION_DAYS,
    ) -> str:
        user = self.get_user(user_id)
        if user is None:
            raise KeyError(user_id)
        token = new_token()
        token_hash, salt = hash_token(token)
        expires = (datetime.now() + timedelta(days=ttl_days)).isoformat(timespec="seconds")
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO sessions (
                        token_hash, token_salt, user_id, account_id,
                        active_tenant_id, user_secret_version, expires_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        token_hash,
                        salt,
                        user_id,
                        account_id,
                        active_tenant_id,
                        int(user["secret_version"]),
                        expires,
                        _now(),
                    ),
                )
            finally:
                conn.close()
        return token

    def resolve_session(self, token: str | None) -> dict[str, Any] | None:
        """按明文 token 解析会话；过期 / 禁用 / secret_version 不匹配 → None。"""
        if not token:
            return None
        token_hash, _ = hash_token(token)
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT s.token_hash, s.user_id, s.account_id,
                           s.active_tenant_id, s.user_secret_version, s.expires_at,
                           u.username, u.display_name, u.disabled,
                           u.must_change_password, u.secret_version AS current_secret_version
                    FROM sessions s JOIN users u ON u.id = s.user_id
                    WHERE s.token_hash = ?
                    """,
                    (token_hash,),
                ).fetchone()
            finally:
                conn.close()
        if row is None or not verify_token(token, row["token_hash"], _derived_salt(token)):
            return None
        if row["disabled"]:
            return None
        if int(row["user_secret_version"]) != int(row["current_secret_version"]):
            # 密码已改 / 账号曾被禁用：旧会话一律失效。
            self.delete_session_by_hash(row["token_hash"])
            return None
        try:
            expires = datetime.fromisoformat(row["expires_at"])
        except ValueError:
            return None
        if expires < datetime.now():
            self.delete_session_by_hash(row["token_hash"])
            return None
        return {
            "token_hash": row["token_hash"],
            "user_id": row["user_id"],
            "account_id": row["account_id"],
            "active_tenant_id": row["active_tenant_id"],
            "expires_at": row["expires_at"],
            "user": {
                "id": row["user_id"],
                "username": row["username"],
                "display_name": row["display_name"] or row["username"],
                "must_change_password": bool(row["must_change_password"]),
            },
        }

    def delete_session(self, token: str) -> None:
        token_hash, _ = hash_token(token)
        self.delete_session_by_hash(token_hash)

    def delete_session_by_hash(self, token_hash: str) -> None:
        with self.lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            finally:
                conn.close()

    def delete_sessions_for_user(
        self, user_id: str, *, keep_token: str | None = None
    ) -> int:
        keep_hash = hash_token(keep_token)[0] if keep_token else None
        with self.lock:
            conn = self._connect()
            try:
                if keep_hash:
                    cur = conn.execute(
                        "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                        (user_id, keep_hash),
                    )
                else:
                    cur = conn.execute(
                        "DELETE FROM sessions WHERE user_id = ?", (user_id,)
                    )
                return int(cur.rowcount)
            finally:
                conn.close()

    def set_session_tenant(self, token: str, tenant_id: str | None) -> bool:
        """设置（或清除）会话的 active_tenant_id。返回是否找到该会话。"""
        token_hash, _ = hash_token(token)
        with self.lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "UPDATE sessions SET active_tenant_id = ? WHERE token_hash = ?",
                    (tenant_id, token_hash),
                )
                return cur.rowcount > 0
            finally:
                conn.close()

    # ------------------------------------------------------------------ #
    # device credentials（tenant_id 固定不可改绑）
    # ------------------------------------------------------------------ #
    def issue_device_credential(
        self, tenant_id: str, *, device_label: str = ""
    ) -> dict[str, Any]:
        if self.get_tenant(tenant_id) is None:
            raise KeyError(f"tenant missing: {tenant_id}")
        device_id = str(uuid.uuid4())
        token = new_token(DEVICE_TOKEN_PREFIX)
        token_hash, salt = hash_token(token)
        created_at = _now()
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO device_credentials (
                        id, tenant_id, token_hash, token_salt, device_label,
                        revoked_at, last_used_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)
                    """,
                    (device_id, tenant_id, token_hash, salt, device_label, created_at),
                )
            finally:
                conn.close()
        return {
            "device_id": device_id,
            "tenant_id": tenant_id,
            "device_label": device_label,
            "token": token,  # 明文只在此返回一次
            "created_at": created_at,
        }

    def authenticate_device(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        token_hash, _ = hash_token(token)
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM device_credentials WHERE token_hash = ? AND revoked_at IS NULL",
                    (token_hash,),
                ).fetchone()
            finally:
                conn.close()
        if row is None or not verify_token(token, row["token_hash"], _derived_salt(token)):
            return None
        self._touch_device(row["id"])
        return {
            "device_id": row["id"],
            "tenant_id": row["tenant_id"],
            "device_label": row["device_label"],
        }

    def _touch_device(self, device_id: str) -> None:
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE device_credentials SET last_used_at = ? WHERE id = ?",
                    (_now(), device_id),
                )
            finally:
                conn.close()

    def revoke_device_credential(self, device_id: str) -> None:
        with self.lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "UPDATE device_credentials SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                    (_now(), device_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(f"device credential missing or already revoked: {device_id}")
            finally:
                conn.close()

    def rotate_device_credential(
        self, device_id: str, *, device_label: str | None = None
    ) -> dict[str, Any]:
        """凭证轮换（合同 §15-B5）：签发新凭证 + 撤旧，单事务原子完成。

        顺序（§15-B5：旧撤销前先插新）：先 INSERT 新行，再 UPDATE 旧行
        revoked_at —— 任一步失败整体回滚，不会出现「旧已撤、新未签」的空窗。
        tenant_id 从旧凭证行复制（凭证不可改绑，§6.2）；device_label 缺省沿用
        旧值。明文 token 只在本返回值中出现一次。
        旧凭证不存在或已撤销 → KeyError（API 层转 404）。
        """
        new_device_id = str(uuid.uuid4())
        token = new_token(DEVICE_TOKEN_PREFIX)
        token_hash, salt = hash_token(token)
        created_at = _now()
        with self.lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = conn.execute(
                        "SELECT tenant_id, device_label FROM device_credentials "
                        "WHERE id = ? AND revoked_at IS NULL",
                        (device_id,),
                    ).fetchone()
                    if row is None:
                        conn.execute("ROLLBACK")
                        raise KeyError(
                            f"device credential missing or already revoked: {device_id}"
                        )
                    label = device_label if device_label is not None else row["device_label"]
                    conn.execute(
                        """
                        INSERT INTO device_credentials (
                            id, tenant_id, token_hash, token_salt, device_label,
                            revoked_at, last_used_at, created_at
                        ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)
                        """,
                        (new_device_id, row["tenant_id"], token_hash, salt, label, created_at),
                    )
                    conn.execute(
                        "UPDATE device_credentials SET revoked_at = ? WHERE id = ?",
                        (_now(), device_id),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.OperationalError:
                        pass
                    raise
            finally:
                conn.close()
        return {
            "device_id": new_device_id,
            "tenant_id": row["tenant_id"],
            "device_label": label,
            "token": token,  # 明文只在此返回一次
            "rotated_from": device_id,
            "created_at": created_at,
        }

    def list_device_credentials(self, tenant_id: str) -> list[dict[str, Any]]:
        with self.lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT id, tenant_id, device_label, revoked_at, last_used_at, created_at
                    FROM device_credentials WHERE tenant_id = ? ORDER BY created_at ASC
                    """,
                    (tenant_id,),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def update_device(
        self, device_id: str, *, device_label: str | None = None, tenant_id: str | None = None
    ) -> dict[str, Any]:
        """更新设备标签；尝试改绑 tenant_id 一律拒绝（合同 §6.2）。"""
        if tenant_id is not None:
            raise ValueError("device credentials cannot be re-bound to another tenant")
        if device_label is None:
            raise ValueError("nothing to update")
        with self.lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "UPDATE device_credentials SET device_label = ? WHERE id = ?",
                    (device_label, device_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(device_id)
            finally:
                conn.close()
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT id, tenant_id, device_label, revoked_at, last_used_at, created_at "
                    "FROM device_credentials WHERE id = ?",
                    (device_id,),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    # ------------------------------------------------------------------ #
    # device bind codes（短 TTL、单次）
    # ------------------------------------------------------------------ #
    def create_bind_code(
        self,
        tenant_id: str,
        *,
        ttl_seconds: int = DEFAULT_BIND_CODE_TTL_SECONDS,
        created_by: str = "",
    ) -> dict[str, Any]:
        if self.get_tenant(tenant_id) is None:
            raise KeyError(f"tenant missing: {tenant_id}")
        ttl_seconds = max(1, min(int(ttl_seconds), MAX_BIND_CODE_TTL_SECONDS))
        code = new_token()
        code_hash, salt = hash_token(code)
        expires = (
            datetime.now() + timedelta(seconds=ttl_seconds)
        ).isoformat(timespec="seconds")
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO device_bind_codes (
                        id, tenant_id, code_hash, code_salt, expires_at,
                        used_at, created_by, created_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (str(uuid.uuid4()), tenant_id, code_hash, salt, expires, created_by, _now()),
                )
            finally:
                conn.close()
        return {"code": code, "expires_at": expires, "ttl_seconds": ttl_seconds, "tenant_id": tenant_id}

    def consume_bind_code(self, code: str) -> dict[str, Any] | None:
        """单次消费绑定码：原子标记 used_at；过期 / 已用 / 不存在 → None。"""
        if not code:
            return None
        code_hash, _ = hash_token(code)
        with self.lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = conn.execute(
                        "SELECT id, tenant_id, expires_at FROM device_bind_codes "
                        "WHERE code_hash = ? AND used_at IS NULL AND expires_at > ?",
                        (code_hash, _now()),
                    ).fetchone()
                    if row is None or not verify_token(code, code_hash, _derived_salt(code)):
                        conn.execute("ROLLBACK")
                        return None
                    cur = conn.execute(
                        "UPDATE device_bind_codes SET used_at = ? "
                        "WHERE id = ? AND used_at IS NULL AND expires_at > ?",
                        (_now(), row["id"], _now()),
                    )
                    if cur.rowcount != 1:
                        conn.execute("ROLLBACK")
                        return None
                    tenant_id = row["tenant_id"]
                    expires_at = row["expires_at"]
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()
        return {
            "tenant_id": tenant_id,
            "expires_at": expires_at,
            "tenant": self.get_tenant(tenant_id),
        }

    def consume_and_issue_device(
        self, code: str, *, device_label: str = ""
    ) -> dict[str, Any]:
        """消费绑定码并立即为对应租户签发设备凭证。"""
        consumed = self.consume_bind_code(code)
        if consumed is None:
            raise KeyError("bind code invalid, expired or already used")
        return self.issue_device_credential(consumed["tenant_id"], device_label=device_label)

    # ------------------------------------------------------------------ #
    # seed（空库引导；沿用 UserStore 的 MOUSEVISION_ADMIN_PASSWORD 语义）
    # ------------------------------------------------------------------ #
    def _seed(self) -> None:
        legacy = self.get_tenant(LEGACY_TENANT_ID)
        if self.count() == 0 and legacy is None:
            configured = os.getenv("MOUSEVISION_ADMIN_PASSWORD", "").strip()
            if configured:
                default_pw = configured
                print(
                    "[MouseVision] 已创建管理员 admin（使用 MOUSEVISION_ADMIN_PASSWORD），首次登录须改密",
                    flush=True,
                )
            else:
                default_pw = secrets.token_urlsafe(12)
                print(
                    "[MouseVision] 已创建管理员 admin，一次性随机密码："
                    f"{default_pw}（请立即登录并修改；也可设置 MOUSEVISION_ADMIN_PASSWORD）",
                    flush=True,
                )
            admin = self.create_user(
                "admin", default_pw, display_name="超级管理员", must_change_password=1
            )
            self.set_platform_admin(admin["id"], True)
            account = self.create_account("MouseVision", owner_user_id=admin["id"])
            legacy = self.create_tenant(
                account["id"],
                "Legacy Default",
                LEGACY_TENANT_SLUG,
                tenant_id=LEGACY_TENANT_ID,
            )
            self.add_membership(admin["id"], legacy["id"], "tenant_admin")
        elif legacy is None:
            # 非空库但缺 legacy-default（例如旧版本库升级）：幂等补建。
            self.ensure_legacy_default()
