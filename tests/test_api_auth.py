"""API token protection tests."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from ui.auth import require_api_token


def test_require_api_token_rejects_when_unset(monkeypatch):
    """B4（合同 §4.3/§15-B4）：open mode 关闭 —— 未配置 token 不再匿名放行。
    （行为按合同有意变更：旧断言为未配置时静默通过。）"""
    monkeypatch.delenv("MOUSEVISION_API_TOKEN", raising=False)
    with pytest.raises(HTTPException) as exc:
        require_api_token(x_mousevision_token=None)
    assert exc.value.status_code == 401


def test_require_api_token_rejects_missing_header(monkeypatch):
    monkeypatch.setenv("MOUSEVISION_API_TOKEN", "edge-secret")
    with pytest.raises(HTTPException) as exc:
        require_api_token(x_mousevision_token=None)
    assert exc.value.status_code == 401


def test_require_api_token_accepts_matching_header(monkeypatch):
    monkeypatch.setenv("MOUSEVISION_API_TOKEN", "edge-secret")
    require_api_token(x_mousevision_token="edge-secret")


def test_html_no_longer_injects_token_meta(monkeypatch):
    """B5（合同 §6.1/§15-B5，有意变更）：共享 token 不再注入任何 HTML——
    ``_inject_api_token`` 已删除（旧断言为 /legacy、/mobile 注入 meta）。
    云版 H5 改经设备凭证（/api/control/devices/bind|login）获得身份。"""
    import ui.app as app_mod

    monkeypatch.setenv("MOUSEVISION_API_TOKEN", "edge-secret")
    assert not hasattr(app_mod, "_inject_api_token")
