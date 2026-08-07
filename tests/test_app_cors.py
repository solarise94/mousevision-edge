"""CORS tests for the packaged-app origin (app.miceautomatic.local).

The packaged Android app loads its H5 from a synthetic https origin
(app.miceautomatic.local) inside WebView and calls the API server cross-origin.
Only that origin is allowed, and tokens travel via the X-MouseVision-Token
header (no credentials), so preflight must not require credential sharing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

APP_ORIGIN = "https://app.miceautomatic.local"


@pytest.fixture()
def app_client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MOUSEVISION_OUTPUT_DIR", str(tmp_path / "output"))
    import importlib

    import ui.app as app_mod

    importlib.reload(app_mod)
    with TestClient(app_mod.app) as c:
        yield c


def test_preflight_allows_packaged_app_origin(app_client):
    res = app_client.options(
        "/api/boxes",
        headers={
            "Origin": APP_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-mousevision-token,content-type",
        },
    )
    assert res.status_code == 200
    assert res.headers.get("access-control-allow-origin") == APP_ORIGIN
    assert res.headers.get("access-control-allow-methods") is not None
    assert "x-mousevision-token" in res.headers.get("access-control-allow-headers", "").lower()


def test_preflight_rejects_unknown_origin(app_client):
    # Starlette's CORSMiddleware returns 400 (no CORS headers) for preflights
    # from origins outside the allowlist.
    res = app_client.options(
        "/api/boxes",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert res.status_code == 400
    assert "access-control-allow-origin" not in res.headers


def test_normal_request_unaffected_without_origin(app_client):
    res = app_client.get("/api/boxes")
    assert res.status_code == 200
    assert "access-control-allow-origin" not in res.headers
