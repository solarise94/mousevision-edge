"""Authentication: shared API token + session cookies."""

from __future__ import annotations

import os
from typing import Any

from fastapi import Cookie, Header, HTTPException, Request

from ui.users import SESSION_COOKIE, UserStore

_user_store: UserStore | None = None


def set_user_store(store: UserStore) -> None:
    global _user_store
    _user_store = store


def user_store() -> UserStore:
    if _user_store is None:
        raise RuntimeError("UserStore not initialized")
    return _user_store


def api_token() -> str:
    return os.getenv("MOUSEVISION_API_TOKEN", "").strip()


def require_api_token(
    x_mousevision_token: str | None = Header(None, alias="X-MouseVision-Token"),
) -> None:
    expected = api_token()
    if not expected:
        return
    if x_mousevision_token != expected:
        raise HTTPException(status_code=401, detail="无效或缺少 API token")


def current_user(
    request: Request,
    mv_session: str | None = Cookie(None, alias=SESSION_COOKIE),
) -> dict[str, Any] | None:
    user = user_store().resolve_session(mv_session)
    if user:
        return user
    token = api_token()
    if token:
        header = request.headers.get("X-MouseVision-Token")
        if header == token:
            return {
                "id": "token",
                "username": "api-token",
                "role": "admin",
                "display_name": "API Token",
                "must_change_password": False,
            }
    return None


def require_user(
    request: Request,
    mv_session: str | None = Cookie(None, alias=SESSION_COOKIE),
) -> dict[str, Any]:
    user = current_user(request, mv_session)
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def require_role(*roles: str):
    def _dep(
        request: Request,
        mv_session: str | None = Cookie(None, alias=SESSION_COOKIE),
    ) -> dict[str, Any]:
        user = require_user(request, mv_session)
        if user["role"] not in roles and user["role"] != "admin":
            raise HTTPException(status_code=403, detail="权限不足")
        return user

    return _dep


def require_write_access(
    request: Request,
    x_mousevision_token: str | None = Header(None, alias="X-MouseVision-Token"),
    mv_session: str | None = Cookie(None, alias=SESSION_COOKIE),
) -> dict[str, Any] | None:
    """Accept session (operator+) or legacy API token for write endpoints."""
    user = current_user(request, mv_session)
    if user and user["role"] in {"admin", "operator"}:
        return user
    expected = api_token()
    if expected and x_mousevision_token == expected:
        return {
            "id": "token",
            "username": "api-token",
            "role": "admin",
            "display_name": "API Token",
        }
    if expected:
        raise HTTPException(status_code=401, detail="无效或缺少 API token")
    if user and user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="只读账号无写权限")
    raise HTTPException(status_code=401, detail="请先登录")
