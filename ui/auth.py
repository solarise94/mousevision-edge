"""Authentication: session cookies for PC admin; shared API token for mobile/legacy only."""

from __future__ import annotations

import os
import time
from collections import defaultdict
from typing import Any

from fastapi import Cookie, Header, HTTPException, Request

from ui.users import SESSION_COOKIE, UserStore

_user_store: UserStore | None = None

# login rate limit: IP -> timestamps of recent failures
_login_failures: dict[str, list[float]] = defaultdict(list)
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SEC = 300


def set_user_store(store: UserStore) -> None:
    global _user_store
    _user_store = store


def user_store() -> UserStore:
    if _user_store is None:
        raise RuntimeError("UserStore not initialized")
    return _user_store


def api_token() -> str:
    return os.getenv("MOUSEVISION_API_TOKEN", "").strip()


def trust_proxy() -> bool:
    return os.getenv("MOUSEVISION_TRUST_PROXY", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def cookie_secure(request: Request | None = None) -> bool:
    """Use Secure cookies under HTTPS (env flag or trusted forwarded proto)."""
    if os.getenv("MOUSEVISION_HTTPS", "").strip().lower() in {"1", "true", "yes"}:
        return True
    if request is None:
        return False
    if trust_proxy():
        proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        if proto == "https":
            return True
    return request.url.scheme == "https"


def require_api_token(
    x_mousevision_token: str | None = Header(None, alias="X-MouseVision-Token"),
) -> None:
    """Machine/mobile write gate. Does NOT imply an admin user session."""
    expected = api_token()
    if not expected:
        return
    if x_mousevision_token != expected:
        raise HTTPException(status_code=401, detail="无效或缺少 API token")


def current_user(
    mv_session: str | None = Cookie(None, alias=SESSION_COOKIE),
) -> dict[str, Any] | None:
    """Resolve PC admin session only. Shared API token never maps to a user."""
    return user_store().resolve_session(mv_session)


def require_user(
    mv_session: str | None = Cookie(None, alias=SESSION_COOKIE),
) -> dict[str, Any]:
    user = current_user(mv_session)
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def require_active_user(
    mv_session: str | None = Cookie(None, alias=SESSION_COOKIE),
) -> dict[str, Any]:
    """Logged-in user who is not blocked on forced password change."""
    user = require_user(mv_session)
    if user.get("must_change_password"):
        raise HTTPException(
            status_code=403,
            detail="必须先修改密码",
            headers={"X-Must-Change-Password": "1"},
        )
    return user


def require_role(*roles: str):
    def _dep(
        mv_session: str | None = Cookie(None, alias=SESSION_COOKIE),
    ) -> dict[str, Any]:
        user = require_active_user(mv_session)
        if user["role"] not in roles and user["role"] != "admin":
            raise HTTPException(status_code=403, detail="权限不足")
        return user

    return _dep


def require_write_access(
    mv_session: str | None = Cookie(None, alias=SESSION_COOKIE),
) -> dict[str, Any]:
    """PC admin write gate: session operator+ only. Token is NOT accepted."""
    user = require_active_user(mv_session)
    if user["role"] not in {"admin", "operator"}:
        raise HTTPException(status_code=403, detail="只读账号无写权限")
    return user


def require_admin_session(
    mv_session: str | None = Cookie(None, alias=SESSION_COOKIE),
) -> dict[str, Any]:
    """System-level ops (reset, users, settings): admin session only.

    Machine tokens and operator sessions are explicitly rejected.
    """
    user = require_active_user(mv_session)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def require_token_or_operator(
    x_mousevision_token: str | None = Header(None, alias="X-MouseVision-Token"),
    mv_session: str | None = Cookie(None, alias=SESSION_COOKIE),
) -> dict[str, Any]:
    """Allow mobile/legacy API token OR an active admin/operator session.

    Token authentication is a machine gate only — it is never treated as an
    admin user/session. Prefer session when both are present.
    """
    user = user_store().resolve_session(mv_session)
    if user is not None:
        if user.get("must_change_password"):
            raise HTTPException(
                status_code=403,
                detail="必须先修改密码",
                headers={"X-Must-Change-Password": "1"},
            )
        if user["role"] not in {"admin", "operator"}:
            raise HTTPException(status_code=403, detail="只读账号无写权限")
        return {"auth": "session", **user}

    expected = api_token()
    if not expected:
        # Open mode (no token configured): allow for mobile/legacy compatibility.
        return {"auth": "open", "username": "anonymous", "role": "machine"}
    if x_mousevision_token == expected:
        return {"auth": "token", "username": "api-token", "role": "machine"}
    raise HTTPException(status_code=401, detail="请先登录或提供有效 API token")


def client_ip(request: Request) -> str:
    """Client IP for rate limiting.

    ``X-Forwarded-For`` is only trusted when ``MOUSEVISION_TRUST_PROXY`` is set
    (request is behind a reverse proxy that overwrites/strips client-supplied
    forwarding headers). Otherwise use the socket peer address.
    """
    if trust_proxy():
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            return forwarded
    if request.client:
        return request.client.host
    return "unknown"


def check_login_rate_limit(request: Request) -> None:
    ip = client_ip(request)
    now = time.time()
    recent = [t for t in _login_failures[ip] if now - t < LOGIN_WINDOW_SEC]
    _login_failures[ip] = recent
    if len(recent) >= LOGIN_MAX_FAILURES:
        raise HTTPException(status_code=429, detail="登录失败次数过多，请稍后再试")


def record_login_failure(request: Request) -> None:
    _login_failures[client_ip(request)].append(time.time())


def clear_login_failures(request: Request) -> None:
    _login_failures.pop(client_ip(request), None)
