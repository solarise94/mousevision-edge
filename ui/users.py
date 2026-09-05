"""User accounts, sessions, and role-based access.

B2 起 :class:`UserStore` 是 control.db（ui/control_store.py）之上的**兼容门面**：
公开方法签名不变，既有 handler（auth.py / app.py）继续工作。真实模型是
合同 §4.1 的 users + memberships + account_owners + platform_admins；
``users.role`` 概念由控制面推导：

- platform_admin                → 派生 legacy ``admin``
- legacy-default membership:
    - tenant_admin              → ``admin``
    - operator                  → ``operator``
    - viewer                    → ``viewer``
- 其余（其他租户成员 / 无成员）  → ``viewer``（最小权限，业务写走 B3 的
  TenantContext，不再经旧 role 通道）

旧 ``users.db`` 文件保留原位不读不删，由 B7 迁移工具消费；seed admin 的
MOUSEVISION_ADMIN_PASSWORD / 随机密码打印语义移入 ControlStore。
"""

from __future__ import annotations

from typing import Any

from ui.control_store import (
    LEGACY_TENANT_ID,
    ControlStore,
)

ROLES = frozenset({"admin", "operator", "viewer"})
SESSION_COOKIE = "mv_session"
SESSION_DAYS = 7
PBKDF2_ITERATIONS = 260_000

# 兼容 re-export：hash_password / verify_password 的唯一实现在本文件，
# control_store 反向延迟复用（避免循环导入）。


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def hash_password(password: str, *, salt: str | None = None) -> tuple[str, str]:
    import hashlib
    import secrets

    salt_val = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt_val.encode("utf-8"),
        PBKDF2_ITERATIONS,
    )
    return digest.hex(), salt_val


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    import secrets

    candidate, _ = hash_password(password, salt=salt)
    return secrets.compare_digest(candidate, password_hash)


_LEGACY_ROLE_BY_MEMBERSHIP = {
    "tenant_admin": "admin",
    "operator": "operator",
    "viewer": "viewer",
}


class UserStore:
    """control.db 兼容门面：旧签名进，旧形状出；不再是独立的登录真相源。"""

    def __init__(self, control_store: ControlStore) -> None:
        self.control = control_store
        self.lock = control_store.lock

    # ------------------------------------------------------------------ #
    # 派生 role
    # ------------------------------------------------------------------ #
    def _legacy_role(self, user_id: str) -> str:
        if self.control.is_platform_admin(user_id):
            return "admin"
        membership = self.control.get_membership(user_id, LEGACY_TENANT_ID)
        if membership is not None:
            return _LEGACY_ROLE_BY_MEMBERSHIP.get(str(membership["role"]), "viewer")
        return "viewer"

    def _public(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "username": row["username"],
            "role": self._legacy_role(row["id"]),
            "display_name": row["display_name"] or row["username"],
            "must_change_password": bool(row["must_change_password"]),
            "disabled": bool(row["disabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def count(self) -> int:
        return self.control.count()

    def get_by_id(self, user_id: str) -> dict[str, Any] | None:
        row = self.control.get_user(user_id)
        return self._public(row) if row else None

    def get_by_username(self, username: str) -> dict[str, Any] | None:
        row = self.control.get_user_by_username(username)
        if row is None:
            return None
        data = self._public(row)
        data["_password_hash"] = row["_password_hash"]
        data["_salt"] = row["_salt"]
        return data

    def list_users(self) -> list[dict[str, Any]]:
        return [self._public(u) for u in self.control.list_users()]

    def list_tenants_for_user(self, user_id: str) -> list[dict[str, Any]]:
        """登录响应附加字段：用户可进入的租户列表（含经 parent 的只读项）。"""
        return [
            {
                "tenant_id": item["tenant_id"],
                "account_id": item["account_id"],
                "name": item["name"],
                "slug": item["slug"],
                "role": item["role"],
                "status": item["status"],
            }
            for item in self.control.list_user_tenants(user_id)
        ]

    # ------------------------------------------------------------------ #
    # 用户 CRUD（legacy 语义：role 落在 legacy-default membership 上）
    # ------------------------------------------------------------------ #
    def create_user(
        self,
        *,
        username: str,
        password: str,
        role: str = "operator",
        display_name: str = "",
        must_change_password: int = 0,
    ) -> dict[str, Any]:
        if role not in ROLES:
            raise ValueError(f"invalid role: {role}")
        user = self.control.create_user(
            username,
            password,
            display_name=display_name,
            must_change_password=must_change_password,
        )
        legacy = self.control.ensure_legacy_default()
        membership_role = {"admin": "tenant_admin"}.get(role, role)
        try:
            self.control.add_membership(user["id"], legacy["id"], membership_role)
        except KeyError:
            self.control.set_membership_role(user["id"], legacy["id"], membership_role)
        user = self.control.get_user(user["id"])
        assert user is not None
        return self._public(user)

    def update_user(self, user_id: str, **changes: Any) -> dict[str, Any]:
        allowed = {"role", "display_name", "disabled", "must_change_password", "password"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported user fields: {sorted(unknown)}")
        if "role" in changes and changes["role"] not in ROLES:
            raise ValueError(f"invalid role: {changes['role']}")
        role = changes.pop("role", None)
        user = self.control.update_user(user_id, **changes)
        if role is not None:
            legacy = self.control.ensure_legacy_default()
            membership_role = {"admin": "tenant_admin"}.get(role, role)
            if self.control.get_membership(user_id, legacy["id"]) is None:
                self.control.add_membership(user_id, legacy["id"], membership_role)
            else:
                self.control.set_membership_role(user_id, legacy["id"], membership_role)
            user = self.control.get_user(user_id)
            assert user is not None
        return self._public(user)

    def delete_user(self, user_id: str) -> None:
        self.control.delete_user(user_id)

    # ------------------------------------------------------------------ #
    # 认证与会话
    # ------------------------------------------------------------------ #
    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        row = self.control.authenticate(username, password)
        return self._public(row) if row else None

    def create_session(self, user_id: str) -> str:
        accounts = self.control.accounts_for_user(user_id)
        account_id = accounts[0]["id"] if accounts else None
        return self.control.create_session(user_id, account_id=account_id)

    def resolve_session(self, token: str | None) -> dict[str, Any] | None:
        session = self.control.resolve_session(token)
        if session is None:
            return None
        row = self.control.get_user(session["user_id"])
        if row is None or row["disabled"]:
            return None
        public = self._public(row)
        return {
            "id": public["id"],
            "username": public["username"],
            "role": public["role"],
            "display_name": public["display_name"],
            "must_change_password": public["must_change_password"],
        }

    def delete_session(self, token: str) -> None:
        self.control.delete_session(token)

    def delete_sessions(
        self, user_id: str, *, keep_token: str | None = None
    ) -> int:
        return self.control.delete_sessions_for_user(user_id, keep_token=keep_token)
