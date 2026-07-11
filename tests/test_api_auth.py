"""API token protection tests."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from ui.app import _inject_api_token
from ui.auth import api_token, require_api_token


def test_require_api_token_open_when_unset(monkeypatch):
    monkeypatch.delenv("MOUSEVISION_API_TOKEN", raising=False)
    require_api_token(x_mousevision_token=None)


def test_require_api_token_rejects_missing_header(monkeypatch):
    monkeypatch.setenv("MOUSEVISION_API_TOKEN", "edge-secret")
    with pytest.raises(HTTPException) as exc:
        require_api_token(x_mousevision_token=None)
    assert exc.value.status_code == 401


def test_require_api_token_accepts_matching_header(monkeypatch):
    monkeypatch.setenv("MOUSEVISION_API_TOKEN", "edge-secret")
    require_api_token(x_mousevision_token="edge-secret")


def test_inject_api_token_meta(monkeypatch):
    monkeypatch.setenv("MOUSEVISION_API_TOKEN", "edge-secret")
    html = "<html><head></head><body></body></html>"
    injected = _inject_api_token(html)
    assert api_token() == "edge-secret"
    assert 'name="mousevision-api-token" content="edge-secret"' in injected
