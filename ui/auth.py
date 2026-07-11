"""Minimal shared-token protection for write API endpoints."""

from __future__ import annotations

import os

from fastapi import Header, HTTPException


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
