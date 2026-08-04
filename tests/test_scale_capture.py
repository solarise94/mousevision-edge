"""Tests for POST /api/scale-capture (raw BLE reading capture).

The phone records every K797 reading verbatim and uploads it; the server
persists the payload to output/scale_captures/capture_<stamp>_<id>.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TOKEN = "capture-secret"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MOUSEVISION_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("MOUSEVISION_API_TOKEN", _TOKEN)
    import importlib

    import ui.app as app_mod

    importlib.reload(app_mod)
    with TestClient(app_mod.app) as c:
        yield c, app_mod


def _headers(token: str = _TOKEN) -> dict[str, str]:
    return {"X-MouseVision-Token": token}


def _readings(n: int = 5) -> list[dict]:
    out = []
    for i in range(n):
        out.append({
            "t_ms": i * 100,
            "grams": round(20.0 + i * 0.1, 1),
            "raw": i,
            "sequence": i,
            "rssi": -60 + (i % 3),
            "stable": i > 2,
            "receivedAtEpochMs": 1700000000000 + i * 100,
        })
    return out


def _payload(n: int = 5, *, device_id: str = "scale01") -> dict:
    return {
        "device_id": device_id,
        "started_at_epoch_ms": 1700000000000,
        "app": "h5-scale-capture",
        "readings": _readings(n),
    }


# --------------------------------------------------------------------------- #
# 1. Happy path: valid upload persists to disk and returns count.
# --------------------------------------------------------------------------- #
def test_capture_persists_and_returns_count(client):
    c, app_mod = client
    payload = _payload(5)
    res = c.post(
        "/api/scale-capture",
        data={
            "device_id": "scale01",
            "payload": json.dumps(payload),
        },
        headers=_headers(),
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["count"] == 5
    assert body["capture_id"].startswith("capture_")
    assert "scale_captures" in body["path"]
    assert body["path"].endswith(".json")

    # File actually exists on disk and contains the readings.
    out_root = Path(app_mod.DEFAULT_OUTPUT)
    fp = out_root / body["path"]
    assert fp.is_file()
    stored = json.loads(fp.read_text(encoding="utf-8"))
    assert stored["device_id"] == "scale01"
    assert isinstance(stored["readings"], list)
    assert len(stored["readings"]) == 5
    assert stored["readings"][0]["grams"] == 20.0
    # Server stamps receipt time.
    assert "received_at_epoch_ms" in stored
    assert stored["received_at_epoch_ms"] > 0


# --------------------------------------------------------------------------- #
# 2. Empty readings array is allowed (count=0) — still stored.
# --------------------------------------------------------------------------- #
def test_capture_empty_readings_allowed(client):
    c, _ = client
    payload = {**_payload(5), "readings": []}
    res = c.post(
        "/api/scale-capture",
        data={"payload": json.dumps(payload)},
        headers=_headers(),
    )
    assert res.status_code == 201, res.text
    assert res.json()["count"] == 0


# --------------------------------------------------------------------------- #
# 3. Bad JSON payload -> 400.
# --------------------------------------------------------------------------- #
def test_capture_bad_json_returns_400(client):
    c, _ = client
    res = c.post(
        "/api/scale-capture",
        data={"payload": "not-json{", "device_id": "scale01"},
        headers=_headers(),
    )
    assert res.status_code == 400


# --------------------------------------------------------------------------- #
# 4. Missing token -> 401.
# --------------------------------------------------------------------------- #
def test_capture_no_token_returns_401(client):
    c, _ = client
    res = c.post(
        "/api/scale-capture",
        data={"payload": json.dumps(_payload(1))},
        # no X-MouseVision-Token header
    )
    assert res.status_code == 401


# --------------------------------------------------------------------------- #
# 5. readings not an array -> 400.
# --------------------------------------------------------------------------- #
def test_capture_readings_not_array_returns_400(client):
    c, _ = client
    payload = {**_payload(5), "readings": {"not": "array"}}
    res = c.post(
        "/api/scale-capture",
        data={"payload": json.dumps(payload)},
        headers=_headers(),
    )
    assert res.status_code == 400


# --------------------------------------------------------------------------- #
# 6. payload not an object -> 400.
# --------------------------------------------------------------------------- #
def test_capture_payload_not_object_returns_400(client):
    c, _ = client
    res = c.post(
        "/api/scale-capture",
        data={"payload": json.dumps([1, 2, 3])},
        headers=_headers(),
    )
    assert res.status_code == 400
