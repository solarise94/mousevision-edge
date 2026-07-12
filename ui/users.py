"""User accounts, sessions, and role-based access."""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any

ROLES = frozenset({"admin", "operator", "viewer"})
SESSION_COOKIE = "mv_session"
SESSION_DAYS = 7
PBKDF2_ITERATIONS = 260_000


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def hash_password(password: str, *, salt: str | None = None) -> tuple[str, str]:
    salt_val = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt_val.encode("utf-8"),
        PBKDF2_ITERATIONS,
    )
    return digest.hex(), salt_val


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt=salt)
    return secrets.compare_digest(candidate, password_hash)


class UserStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.lock = threading.Lock()
        self._init_db()
        self._seed_admin()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.lock:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        id TEXT PRIMARY KEY,
                        username TEXT NOT NULL UNIQUE,
                        password_hash TEXT NOT NULL,
                        salt TEXT NOT NULL,
                        role TEXT NOT NULL DEFAULT 'operator',
                        display_name TEXT NOT NULL DEFAULT '',
                        must_change_password INTEGER NOT NULL DEFAULT 0,
                        disabled INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        token TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
            finally:
                conn.close()

    def _seed_admin(self) -> None:
        if self.count() > 0:
            return
        configured = os.getenv("MOUSEVISION_ADMIN_PASSWORD", "").strip()
        if configured:
            default_pw = configured
            generated = False
        else:
            default_pw = secrets.token_urlsafe(12)
            generated = True
        self.create_user(
            username="admin",
            password=default_pw,
            role="admin",
            display_name="超级管理员",
            must_change_password=1,
        )
        if generated:
            print(
                "[MouseVision] 已创建管理员 admin，一次性随机密码："
                f"{default_pw}（请立即登录并修改；也可设置 MOUSEVISION_ADMIN_PASSWORD）",
                flush=True,
            )
        else:
            print(
                "[MouseVision] 已创建管理员 admin（使用 MOUSEVISION_ADMIN_PASSWORD），首次登录须改密",
                flush=True,
            )

    def count(self) -> int:
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
                return int(row["n"] if row else 0)
            finally:
                conn.close()

    def _public(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "username": row["username"],
            "role": row["role"],
            "display_name": row["display_name"] or row["username"],
            "must_change_password": bool(row["must_change_password"]),
            "disabled": bool(row["disabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_by_id(self, user_id: str) -> dict[str, Any] | None:
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
                return self._public(row) if row else None
            finally:
                conn.close()

    def get_by_username(self, username: str) -> dict[str, Any] | None:
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM users WHERE username = ?", (username,)
                ).fetchone()
                if row is None:
                    return None
                data = self._public(row)
                data["_password_hash"] = row["password_hash"]
                data["_salt"] = row["salt"]
                return data
            finally:
                conn.close()

    def list_users(self) -> list[dict[str, Any]]:
        with self.lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM users ORDER BY created_at ASC"
                ).fetchall()
                return [self._public(r) for r in rows]
            finally:
                conn.close()

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
        user_id = str(uuid.uuid4())
        pw_hash, salt = hash_password(password)
        now = _now()
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO users (
                        id, username, password_hash, salt, role, display_name,
                        must_change_password, disabled, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        user_id,
                        username,
                        pw_hash,
                        salt,
                        role,
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
        user = self.get_by_id(user_id)
        assert user is not None
        return user

    def update_user(self, user_id: str, **changes: Any) -> dict[str, Any]:
        allowed = {"role", "display_name", "disabled", "must_change_password", "password"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported user fields: {sorted(unknown)}")
        if "role" in changes and changes["role"] not in ROLES:
            raise ValueError(f"invalid role: {changes['role']}")
        updates: dict[str, Any] = {}
        for key in ("role", "display_name", "disabled", "must_change_password"):
            if key in changes:
                updates[key] = changes[key]
        if "password" in changes:
            pw_hash, salt = hash_password(changes["password"])
            updates["password_hash"] = pw_hash
            updates["salt"] = salt
            updates["must_change_password"] = 0
        if not updates:
            user = self.get_by_id(user_id)
            if user is None:
                raise KeyError(user_id)
            return user
        updates["updated_at"] = _now()
        columns = list(updates)
        assignments = ", ".join(f"{name} = ?" for name in columns)
        values = [updates[name] for name in columns]
        with self.lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    f"UPDATE users SET {assignments} WHERE id = ?",
                    (*values, user_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(user_id)
                # Password changes invalidate all existing sessions.
                if "password_hash" in updates:
                    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            finally:
                conn.close()
        user = self.get_by_id(user_id)
        assert user is not None
        return user

    def delete_sessions(
        self, user_id: str, *, keep_token: str | None = None
    ) -> int:
        with self.lock:
            conn = self._connect()
            try:
                if keep_token:
                    cur = conn.execute(
                        "DELETE FROM sessions WHERE user_id = ? AND token != ?",
                        (user_id, keep_token),
                    )
                else:
                    cur = conn.execute(
                        "DELETE FROM sessions WHERE user_id = ?", (user_id,)
                    )
                return int(cur.rowcount)
            finally:
                conn.close()

    def delete_user(self, user_id: str) -> None:
        with self.lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
                cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
                if cur.rowcount == 0:
                    raise KeyError(user_id)
            finally:
                conn.close()

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        user = self.get_by_username(username)
        if user is None or user.get("disabled"):
            return None
        if not verify_password(password, user["_password_hash"], user["_salt"]):
            return None
        public = {k: v for k, v in user.items() if not k.startswith("_")}
        return public

    def create_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now()
        expires = (now + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
        with self.lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO sessions (token, user_id, expires_at, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (token, user_id, expires, _now()),
                )
            finally:
                conn.close()
        return token

    def resolve_session(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT s.token, s.expires_at, u.*
                    FROM sessions s
                    JOIN users u ON u.id = s.user_id
                    WHERE s.token = ?
                    """,
                    (token,),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        try:
            expires = datetime.fromisoformat(row["expires_at"])
        except ValueError:
            return None
        if expires < datetime.now():
            self.delete_session(token)
            return None
        if row["disabled"]:
            return None
        return {
            "id": row["id"],
            "username": row["username"],
            "role": row["role"],
            "display_name": row["display_name"] or row["username"],
            "must_change_password": bool(row["must_change_password"]),
        }

    def delete_session(self, token: str) -> None:
        with self.lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            finally:
                conn.close()
