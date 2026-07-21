"""Contract tests for the realtime WebSocket message format.

These verify that the server emits the exact message shape the mobile client
parses in ``handleServerMessage``, so a future backend change can't silently
break the phone-side announce/accept/quality-hint flow.

We use FastAPI's TestClient to open the WS and drive a stubbed engine, then
assert on the JSON messages the server actually sends.
"""

from __future__ import annotations

import json
import struct
from typing import Any

import cv2
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ui.realtime_api as realtime_api
from mousevision.realtime import (
    Attempt,
    QualityHint,
    RealtimeFrameResult,
    RealtimeSession,
    RealtimeState,
)


# --------------------------------------------------------------------------- #
# Stub engine: returns a canned RealtimeFrameResult so we don't need real OCR.
# --------------------------------------------------------------------------- #


class _StubEngine:
    """A minimal stand-in for RealtimeSession that returns scripted results."""

    def __init__(self, scripted_results: list[RealtimeFrameResult]) -> None:
        self._results = list(scripted_results)
        self._idx = 0
        self.state = RealtimeState.CALIBRATING
        self._accepted: list[Attempt] = []
        self._attempts: list[Attempt] = []
        self._current: Attempt | None = None

    def process_frame(self, image, *, frame_seq=0, client_ts_ms=0.0):
        if self._idx < len(self._results):
            res = self._results[self._idx]
            self._idx += 1
        else:
            res = RealtimeFrameResult(state=self.state, frame_seq=frame_seq)
        # Track the in-flight attempt so request_retry / accept_weight can
        # mirror the real engine's contract.
        if res.attempt is not None:
            self._attempts.append(res.attempt)
            self._current = res.attempt
            self.state = RealtimeState.ANNOUNCED
        elif res.accepted_weight is not None:
            if self._current is not None:
                self._current.state = "accepted"
                self._accepted.append(self._current)
                self._current = None
            self.state = RealtimeState.WAIT_CLEAR
        else:
            self.state = res.state
        return res

    def request_retry(self):
        if self._current is not None:
            self._current.state = "rejected"
        self._current = None
        self.state = RealtimeState.RETRY_REQUESTED

    def accept_weight(self):
        if self._current is None:
            return None
        self._current.state = "accepted"
        self._accepted.append(self._current)
        a = self._current
        self._current = None
        self.state = RealtimeState.WAIT_CLEAR
        return a

    def get_accepted_records(self):
        return list(self._accepted)

    def get_all_attempts(self):
        return list(self._attempts)


def _make_app(tmp_path, scripted_results, monkeypatch) -> TestClient:
    """Build an app with the realtime router and a stubbed engine factory."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("device_id: scale01\nweight_roi: {x:1,y:1,w:1,h:1}\n")

    realtime_api.configure(str(cfg_path), str(tmp_path))

    # Force _create_engine to return our stub regardless of the config.
    monkeypatch.setattr(
        realtime_api, "_create_engine", lambda config: _StubEngine(scripted_results)
    )
    # No token required for the test.
    monkeypatch.setattr(realtime_api, "_check_ws_token", lambda token: True)

    app = FastAPI()
    app.include_router(realtime_api.router)
    return TestClient(app)


def _make_frame_bytes(frame_seq: int, client_ts_ms: int) -> bytes:
    """Encode a minimal valid JPEG with the 8-byte binary header."""
    img = np.full((128, 128, 3), 128, dtype=np.uint8)
    ok, jpeg = cv2.imencode(".jpg", img)
    assert ok
    header = struct.pack("<II", frame_seq, client_ts_ms)
    return header + jpeg.tobytes()


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_state_message_has_required_fields(tmp_path, monkeypatch) -> None:
    """A per-frame state message must include the fields the client reads."""
    scripted = [
        RealtimeFrameResult(
            state=RealtimeState.WEIGHING,
            weight_candidate=23.40,
            confidence=0.82,
            mouse_present=True,
            quality_hints=[QualityHint("glare", "显示屏反光")],
            frame_seq=5,
        )
    ]
    client = _make_app(tmp_path, scripted, monkeypatch)

    # Create a session via REST.
    r = client.post(
        "/api/realtime/session",
        json={"cage_id": "C1", "project_id": "default"},
    )
    assert r.status_code == 200
    sid = r.json()["session_id"]

    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        hello = json.loads(ws.receive_text())
        assert hello["type"] == "hello"
        assert hello["session_id"] == sid

        # Send one binary frame.
        ws.send_bytes(_make_frame_bytes(5, 1000))
        msg = json.loads(ws.receive_text())

    # The contract: the client reads msg.type, msg.state, msg.weight_candidate,
    # msg.quality_hints (array of {code,message}), msg.attempt, msg.accepted_weight.
    assert msg["type"] == "state"
    assert msg["state"] == "weighing"
    assert msg["weight_candidate"] == 23.40
    assert msg["quality_hints"] == [{"code": "glare", "message": "显示屏反光"}]
    assert msg["mouse_present"] is True
    assert msg["frame_seq"] == 5
    # attempt and accepted_weight are nullable but must be present keys.
    assert "attempt" in msg
    assert "accepted_weight" in msg


def test_announced_message_carries_attempt(tmp_path, monkeypatch) -> None:
    """When the engine creates an attempt, the state message must include it
    so the client can trigger speech + show the retry/accept buttons."""
    attempt = Attempt(
        attempt_id="att1",
        weight_g=23.48,
        confidence=0.9,
        frame_seq=12,
        client_ts_ms=3400.0,
        state="announced",
        created_at=1700000000.0,
    )
    scripted = [
        RealtimeFrameResult(
            state=RealtimeState.ANNOUNCED,
            weight_candidate=23.48,
            confidence=0.9,
            mouse_present=True,
            attempt=attempt,
            frame_seq=12,
        )
    ]
    client = _make_app(tmp_path, scripted, monkeypatch)

    sid = client.post(
        "/api/realtime/session", json={"cage_id": "C1"}
    ).json()["session_id"]

    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        json.loads(ws.receive_text())  # hello
        ws.send_bytes(_make_frame_bytes(12, 3400))
        msg = json.loads(ws.receive_text())

    assert msg["type"] == "state"
    assert msg["state"] == "announced"
    assert msg["weight_candidate"] == 23.48
    assert msg["attempt"] is not None
    assert msg["attempt"]["attempt_id"] == "att1"
    assert msg["attempt"]["weight_g"] == 23.48


def test_accept_command_returns_ack_with_attempt(tmp_path, monkeypatch) -> None:
    """The accept command must produce an ack message whose `accepted` field
    the client reads to bump the mouse count."""
    attempt = Attempt(
        attempt_id="att1", weight_g=23.48, confidence=0.9, frame_seq=12,
        client_ts_ms=3400.0, state="announced", created_at=1700000000.0,
    )
    scripted = [
        RealtimeFrameResult(
            state=RealtimeState.ANNOUNCED, weight_candidate=23.48,
            confidence=0.9, attempt=attempt, frame_seq=12,
        )
    ]
    client = _make_app(tmp_path, scripted, monkeypatch)
    sid = client.post("/api/realtime/session", json={"cage_id": "C1"}).json()["session_id"]

    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        json.loads(ws.receive_text())  # hello
        ws.send_bytes(_make_frame_bytes(12, 3400))
        json.loads(ws.receive_text())  # announced state

        ws.send_text(json.dumps({"type": "accept"}))
        ack = json.loads(ws.receive_text())

    assert ack["type"] == "ack"
    assert ack["cmd"] == "accept"
    assert ack["accepted"] is not None
    assert ack["accepted"]["weight_g"] == 23.48
    assert ack["state"] == "wait_clear"


def test_finish_persists_accepted_record(tmp_path, monkeypatch) -> None:
    """finish must write a mouse_NNN/record.json for each accepted attempt."""
    attempt = Attempt(
        attempt_id="att1", weight_g=19.5, confidence=0.88, frame_seq=20,
        client_ts_ms=5000.0, state="announced", created_at=1700000001.0,
    )
    scripted = [
        RealtimeFrameResult(
            state=RealtimeState.ANNOUNCED, weight_candidate=19.5,
            confidence=0.88, attempt=attempt, frame_seq=20,
        )
    ]
    client = _make_app(tmp_path, scripted, monkeypatch)
    sid = client.post("/api/realtime/session", json={"cage_id": "C1"}).json()["session_id"]

    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        json.loads(ws.receive_text())
        ws.send_bytes(_make_frame_bytes(20, 5000))
        json.loads(ws.receive_text())  # announced

        ws.send_text(json.dumps({"type": "accept"}))
        json.loads(ws.receive_text())  # ack

    # Now call finish.
    r = client.post(
        f"/api/realtime/session/{sid}/finish",
        json={"video_upload_job_id": "job-xyz"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total_attempts"] == 1
    assert len(body["accepted"]) == 1
    assert body["accepted"][0]["weight_g"] == 19.5
    assert body["finalize"]["count"] == 1

    # A run dir with mouse_001/record.json must exist under output_root.
    import pathlib

    run_dirs = list(pathlib.Path(str(tmp_path)).glob("run_*"))
    assert len(run_dirs) == 1
    rec = json.loads((run_dirs[0] / "mouse_001" / "record.json").read_text())
    assert rec["weight"] == 19.5
    assert rec["weight_source"] == "realtime_announced"
    assert rec["cage_id"] == "C1"
    manifest = json.loads((run_dirs[0] / "manifest.json").read_text())
    assert manifest["video_upload_job_id"] == "job-xyz"
    assert manifest["mode"] == "realtime"
