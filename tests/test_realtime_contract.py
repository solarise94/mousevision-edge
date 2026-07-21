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
        # Captured for tests that assert on the image handed to the engine.
        self.last_image_shape: tuple[int, ...] | None = None
        self.seen_images: list[tuple[int, ...]] = []

    def process_frame(self, image, *, frame_seq=0, client_ts_ms=0.0):
        try:
            self.last_image_shape = tuple(int(d) for d in image.shape)
            self.seen_images.append(self.last_image_shape)
        except Exception:  # noqa: BLE001
            pass
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
        if self.state != RealtimeState.ANNOUNCED:
            return {
                "applied": False,
                "state": self.state.value,
                "epoch": 0,
            }
        if self._current is not None:
            self._current.state = "rejected"
        self._current = None
        self.state = RealtimeState.WEIGHING
        return {"applied": True, "state": self.state.value, "epoch": 1}

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

    @property
    def _current_attempt(self):
        return self._current



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


def _make_frame_bytes_sized(frame_seq: int, client_ts_ms: int, h: int, w: int) -> bytes:
    """Encode a JPEG of an arbitrary (h, w) size with the binary header."""
    img = np.full((h, w, 3), 128, dtype=np.uint8)
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


def test_decode_failed_error_carries_frame_seq(tmp_path, monkeypatch) -> None:
    """Corrupt JPEG must still ACK with the matching frame_seq."""
    client = _make_app(tmp_path, [], monkeypatch)
    sid = client.post("/api/realtime/session", json={"cage_id": "C1"}).json()["session_id"]

    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        json.loads(ws.receive_text())  # hello
        # Header only + garbage — not a valid JPEG.
        bad = struct.pack("<II", 42, 1000) + b"not-a-jpeg"
        ws.send_bytes(bad)
        msg = json.loads(ws.receive_text())

    assert msg["type"] == "error"
    assert msg["code"] == "decode_failed"
    assert msg["frame_seq"] == 42


def test_processing_failure_returns_frame_ack(tmp_path, monkeypatch) -> None:
    """OCR/engine exceptions must emit frame_processing_failed + frame_seq."""

    class BoomEngine(_StubEngine):
        def process_frame(self, image, *, frame_seq=0, client_ts_ms=0.0):
            raise RuntimeError("ocr boom")

    client = _make_app(tmp_path, [], monkeypatch)
    # Replace the stub with a boom engine after session create.
    sid = client.post("/api/realtime/session", json={"cage_id": "C1"}).json()["session_id"]
    with realtime_api._sessions_lock:
        realtime_api._sessions[sid].engine = BoomEngine([])

    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        json.loads(ws.receive_text())
        ws.send_bytes(_make_frame_bytes(7, 700))
        msg = json.loads(ws.receive_text())

    assert msg["type"] == "error"
    assert msg["code"] == "frame_processing_failed"
    assert msg["frame_seq"] == 7


def test_retry_ack_includes_applied_state_epoch(tmp_path, monkeypatch) -> None:
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
            attempt=attempt,
            frame_seq=12,
        )
    ]
    client = _make_app(tmp_path, scripted, monkeypatch)
    sid = client.post("/api/realtime/session", json={"cage_id": "C1"}).json()["session_id"]

    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        json.loads(ws.receive_text())
        ws.send_bytes(_make_frame_bytes(12, 3400))
        json.loads(ws.receive_text())  # announced

        ws.send_text(json.dumps({"type": "retry"}))
        ack = json.loads(ws.receive_text())

    assert ack["type"] == "ack"
    assert ack["cmd"] == "retry"
    assert ack["applied"] is True
    assert ack["state"] == "weighing"
    assert "epoch" in ack
    assert ack["epoch"] == 1


def test_retry_ack_applied_false_outside_announced(tmp_path, monkeypatch) -> None:
    client = _make_app(tmp_path, [], monkeypatch)
    sid = client.post("/api/realtime/session", json={"cage_id": "C1"}).json()["session_id"]

    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        json.loads(ws.receive_text())
        ws.send_text(json.dumps({"type": "retry"}))
        ack = json.loads(ws.receive_text())

    assert ack["type"] == "ack"
    assert ack["cmd"] == "retry"
    assert ack["applied"] is False
    # Must not look like a frame ACK.
    assert "frame_seq" not in ack or ack.get("frame_seq") is None


def test_accept_ack_is_not_frame_ack(tmp_path, monkeypatch) -> None:
    """accept/retry command ACK must not masquerade as a frame ACK."""
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
        json.loads(ws.receive_text())
        ws.send_bytes(_make_frame_bytes(12, 3400))
        state_msg = json.loads(ws.receive_text())
        assert state_msg["type"] == "state"
        assert state_msg["frame_seq"] == 12

        ws.send_text(json.dumps({"type": "accept"}))
        ack = json.loads(ws.receive_text())

    assert ack["type"] == "ack"
    assert ack["cmd"] == "accept"
    assert "frame_seq" not in ack


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


# --------------------------------------------------------------------------- #
# P0-3: server normalizes non-canonical frames to 720×1280 before the engine
# --------------------------------------------------------------------------- #


def test_non_canonical_frame_is_resized_before_engine(tmp_path, monkeypatch) -> None:
    """A medium-profile frame (540×960) must reach the engine as 720×1280 so
    the LCD/mouse detection thresholds calibrated for 720×1280 still apply."""
    scripted = [RealtimeFrameResult(state=RealtimeState.CALIBRATING, frame_seq=1)]
    client = _make_app(tmp_path, scripted, monkeypatch)
    # Reach the stub engine instance to read the captured image shape.
    sid = client.post("/api/realtime/session", json={"cage_id": "C1"}).json()["session_id"]
    engine = realtime_api._sessions[sid].engine

    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        json.loads(ws.receive_text())  # hello
        # Send a deliberately non-canonical 960x540 (h,w) frame.
        ws.send_bytes(_make_frame_bytes_sized(1, 1000, h=960, w=540))
        json.loads(ws.receive_text())  # state

    # The engine must have seen the canonical 720x1280 (shape is HxWxC).
    assert engine.last_image_shape is not None
    h, w = engine.last_image_shape[0], engine.last_image_shape[1]
    assert (w, h) == (720, 1280), f"engine saw {(w, h)}, expected (720, 1280)"


def test_canonical_frame_is_not_resized(tmp_path, monkeypatch) -> None:
    """A 720×1280 frame must pass through untouched (resized=False in timing)."""
    scripted = [RealtimeFrameResult(state=RealtimeState.CALIBRATING, frame_seq=1)]
    client = _make_app(tmp_path, scripted, monkeypatch)
    sid = client.post("/api/realtime/session", json={"cage_id": "C1"}).json()["session_id"]

    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        json.loads(ws.receive_text())
        ws.send_bytes(_make_frame_bytes_sized(1, 1000, h=1280, w=720))
        msg = json.loads(ws.receive_text())

    assert msg["type"] == "state"
    assert msg["timing"]["resized"] is False
    assert msg["timing"]["source_w"] == 720
    assert msg["timing"]["source_h"] == 1280


# --------------------------------------------------------------------------- #
# P1-3: timing telemetry + session summary
# --------------------------------------------------------------------------- #


def test_state_message_uses_renamed_timing_fields(tmp_path, monkeypatch) -> None:
    """server_preprocess_wait_ms replaces the misleading frame_age_ms; new
    decode_ms / engine_ms / total_ms fields are present."""
    scripted = [RealtimeFrameResult(state=RealtimeState.WEIGHING, frame_seq=1)]
    client = _make_app(tmp_path, scripted, monkeypatch)
    sid = client.post("/api/realtime/session", json={"cage_id": "C1"}).json()["session_id"]

    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        json.loads(ws.receive_text())
        ws.send_bytes(_make_frame_bytes(1, 1000))
        msg = json.loads(ws.receive_text())

    timing = msg["timing"]
    assert "frame_age_ms" not in timing, "frame_age_ms must be renamed"
    assert "server_preprocess_wait_ms" in timing
    assert "decode_ms" in timing
    assert "engine_ms" in timing
    assert "total_ms" in timing


def test_client_timing_message_is_merged(tmp_path, monkeypatch) -> None:
    """A client_timing text command must be merged into the session buffer."""
    client = _make_app(tmp_path, [], monkeypatch)
    sid = client.post("/api/realtime/session", json={"cage_id": "C1"}).json()["session_id"]
    session = realtime_api._sessions[sid]

    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        json.loads(ws.receive_text())
        ws.send_text(json.dumps({
            "type": "client_timing",
            "samples": [
                {"frame_seq": 1, "encode_ms": 12.3, "rtt_ms": 180.0, "jpeg_bytes": 42000},
                {"frame_seq": 2, "encode_ms": 11.1, "rtt_ms": 210.0, "jpeg_bytes": 41000},
            ],
        }))
        # Allow the server to process the command.
        import time as _time
        _time.sleep(0.05)

    assert len(session.client_timing_samples) == 2
    assert session.client_timing_samples[0]["rtt_ms"] == 180.0


def test_finish_includes_timing_summary_and_manifest(tmp_path, monkeypatch) -> None:
    """finish must return timing_summary and write it to manifest.json."""
    import pathlib

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
        # One processed frame -> one server timing sample.
        ws.send_bytes(_make_frame_bytes(20, 5000))
        json.loads(ws.receive_text())  # announced
        # One client timing batch.
        ws.send_text(json.dumps({
            "type": "client_timing",
            "samples": [
                {"frame_seq": 20, "encode_ms": 15.0, "rtt_ms": 250.0, "jpeg_bytes": 50000},
            ],
        }))
        ws.send_text(json.dumps({"type": "accept"}))
        json.loads(ws.receive_text())  # ack

    r = client.post(f"/api/realtime/session/{sid}/finish", json={})
    assert r.status_code == 200
    body = r.json()
    ts = body["timing_summary"]
    assert ts["frames_processed"] >= 1
    assert "engine_ms" in ts["server"]
    assert ts["server"]["engine_ms"]["n"] >= 1
    assert "rtt_ms" in ts["client"]

    run_dirs = list(pathlib.Path(str(tmp_path)).glob("run_*"))
    assert len(run_dirs) == 1
    manifest = json.loads((run_dirs[0] / "manifest.json").read_text())
    assert "timing_summary" in manifest
    assert manifest["timing_summary"]["frames_processed"] >= 1


# --------------------------------------------------------------------------- #
# P1-1: full accept cycle keeps processing frames (regression for the
# "stuck in WAIT_CLEAR after first mouse" P0-1 bug, server-side contract)
# --------------------------------------------------------------------------- #


def test_full_accept_cycle_resumes_frames(tmp_path, monkeypatch) -> None:
    """After accept → WAIT_CLEAR, the server must continue to accept and
    process subsequent frames (so the phone's resumed frame loop can drive
    the engine back to ARMED for the next mouse)."""
    attempt = Attempt(
        attempt_id="att1", weight_g=19.5, confidence=0.9, frame_seq=10,
        client_ts_ms=2000.0, state="announced", created_at=1700000000.0,
    )
    # Frame 1: announce. After accept, subsequent frames return WAIT_CLEAR.
    scripted = [
        RealtimeFrameResult(
            state=RealtimeState.ANNOUNCED, weight_candidate=19.5,
            confidence=0.9, attempt=attempt, frame_seq=10,
        ),
        RealtimeFrameResult(state=RealtimeState.WAIT_CLEAR, frame_seq=11),
        RealtimeFrameResult(state=RealtimeState.WAIT_CLEAR, frame_seq=12),
    ]
    client = _make_app(tmp_path, scripted, monkeypatch)
    sid = client.post("/api/realtime/session", json={"cage_id": "C1"}).json()["session_id"]

    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        json.loads(ws.receive_text())  # hello
        # Announce
        ws.send_bytes(_make_frame_bytes(10, 2000))
        announced = json.loads(ws.receive_text())
        assert announced["state"] == "announced"

        # Accept
        ws.send_text(json.dumps({"type": "accept"}))
        ack = json.loads(ws.receive_text())
        assert ack["cmd"] == "accept"
        assert ack["state"] == "wait_clear"

        # Crucial: the server MUST still process further frames after accept.
        ws.send_bytes(_make_frame_bytes(11, 3000))
        post1 = json.loads(ws.receive_text())
        assert post1["type"] == "state"
        assert post1["frame_seq"] == 11

        ws.send_bytes(_make_frame_bytes(12, 4000))
        post2 = json.loads(ws.receive_text())
        assert post2["frame_seq"] == 12


# --------------------------------------------------------------------------- #
# P2: create_session returns client_config
# --------------------------------------------------------------------------- #


def test_create_session_returns_client_config(tmp_path, monkeypatch) -> None:
    """session create must return client_config with fps / ack timeout / profile."""
    # Write a config with a non-default realtime section.
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "device_id: scale01\n"
        "weight_roi: {x:1,y:1,w:1,h:1}\n"
        "realtime:\n"
        "  max_fps: 4\n"
        "  frame_ack_timeout_ms: 2500\n"
        "  encode_profile: low\n"
    )
    realtime_api.configure(str(cfg_path), str(tmp_path))
    monkeypatch.setattr(
        realtime_api, "_create_engine", lambda config: _StubEngine([])
    )
    monkeypatch.setattr(realtime_api, "_check_ws_token", lambda token: True)
    app = FastAPI()
    app.include_router(realtime_api.router)
    client = TestClient(app)

    r = client.post("/api/realtime/session", json={"cage_id": "C1"})
    assert r.status_code == 200
    cc = r.json()["client_config"]
    assert cc["max_fps"] == 4
    assert cc["frame_ack_timeout_ms"] == 2500
    assert cc["encode_profile"] == "low"


def test_create_session_client_config_defaults_to_high(tmp_path, monkeypatch) -> None:
    """With no realtime section, client_config.encode_profile defaults to high."""
    client = _make_app(tmp_path, [], monkeypatch)
    r = client.post("/api/realtime/session", json={"cage_id": "C1"})
    cc = r.json()["client_config"]
    assert cc["encode_profile"] == "high"
    assert cc["max_fps"] == 5


def test_create_session_rejects_invalid_encode_profile(tmp_path, monkeypatch) -> None:
    """An unknown encode_profile in YAML falls back to high (not forwarded)."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "device_id: scale01\n"
        "weight_roi: {x:1,y:1,w:1,h:1}\n"
        "realtime:\n"
        "  encode_profile: ultra\n"
    )
    realtime_api.configure(str(cfg_path), str(tmp_path))
    monkeypatch.setattr(
        realtime_api, "_create_engine", lambda config: _StubEngine([])
    )
    monkeypatch.setattr(realtime_api, "_check_ws_token", lambda token: True)
    app = FastAPI()
    app.include_router(realtime_api.router)
    client = TestClient(app)

    r = client.post("/api/realtime/session", json={"cage_id": "C1"})
    cc = r.json()["client_config"]
    assert cc["encode_profile"] == "high"


# --------------------------------------------------------------------------- #
# P1: client_timing input validation — malformed samples must never break finish
# --------------------------------------------------------------------------- #


def test_malformed_client_timing_does_not_break_finish(tmp_path, monkeypatch) -> None:
    """A misbehaving client sending garbage client_timing must not crash the
    finish summary or block record finalization."""
    import pathlib

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

        # A battery of malformed client_timing payloads. None may raise.
        ws.send_text(json.dumps({"type": "client_timing", "samples": "not-a-list"}))
        ws.send_text(json.dumps({"type": "client_timing", "samples": ["bad"]}))
        ws.send_text(json.dumps({
            "type": "client_timing",
            "samples": [
                "string",
                42,
                None,
                {"encode_ms": "not-a-number"},
                {"encode_ms": float("nan")},
                {"encode_ms": float("inf")},
                {"encode_ms": -5.0},
                {"rtt_ms": 99999999},
                {"jpeg_bytes": "big"},
                {"jpeg_bytes": -1},
                {"jpeg_bytes": 999999999},
                {"unknown_field": "junk"},
                # One valid sample among the garbage.
                {"frame_seq": 20, "encode_ms": 12.0, "rtt_ms": 180.0, "jpeg_bytes": 42000},
            ],
        }))
        ws.send_text(json.dumps({"type": "accept"}))
        json.loads(ws.receive_text())  # ack

    # finish must succeed and records must still be written.
    r = client.post(f"/api/realtime/session/{sid}/finish", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["total_attempts"] == 1
    assert len(body["accepted"]) == 1
    # Only the one valid client sample survived.
    ts = body["timing_summary"]
    assert ts["client_samples"] == 1
    assert "rtt_ms" in ts["client"]
    assert ts["client"]["rtt_ms"]["n"] == 1
    assert ts["client"]["rtt_ms"]["p50"] == 180.0

    run_dirs = list(pathlib.Path(str(tmp_path)).glob("run_*"))
    assert len(run_dirs) == 1
    rec = json.loads((run_dirs[0] / "mouse_001" / "record.json").read_text())
    assert rec["weight"] == 19.5


def test_client_timing_batch_cap_rejects_flood(tmp_path, monkeypatch) -> None:
    """A huge client_timing batch is truncated; only the first cap is inspected."""
    client = _make_app(tmp_path, [], monkeypatch)
    sid = client.post("/api/realtime/session", json={"cage_id": "C1"}).json()["session_id"]
    session = realtime_api._sessions[sid]

    flood = [{"encode_ms": float(i), "rtt_ms": float(i)} for i in range(10000)]

    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        json.loads(ws.receive_text())
        ws.send_text(json.dumps({"type": "client_timing", "samples": flood}))
        import time as _time
        _time.sleep(0.05)

    # The batch cap (_CLIENT_TIMING_BATCH_CAP=50) limits how many were even
    # considered; the retained buffer must be well below the flood size.
    assert session.client_timing_total <= realtime_api._CLIENT_TIMING_BATCH_CAP


# --------------------------------------------------------------------------- #
# P2: totals vs retained — untruncated counters survive buffer eviction
# --------------------------------------------------------------------------- #


def test_timing_totals_track_untruncated_count(tmp_path, monkeypatch) -> None:
    """frames_processed must reflect every frame the server processed, not
    just the rolling-buffer tail. After exceeding _TIMING_SAMPLE_CAP the
    retained count drops but the total keeps climbing."""
    cap = realtime_api._TIMING_SAMPLE_CAP
    # Script enough results to overflow the cap.
    scripted = [
        RealtimeFrameResult(state=RealtimeState.CALIBRATING, frame_seq=i)
        for i in range(cap + 50)
    ]
    client = _make_app(tmp_path, scripted, monkeypatch)
    sid = client.post("/api/realtime/session", json={"cage_id": "C1"}).json()["session_id"]
    session = realtime_api._sessions[sid]

    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        json.loads(ws.receive_text())  # hello
        for seq in range(cap + 50):
            ws.send_bytes(_make_frame_bytes(seq, seq * 100))
            # Drain each state reply so the socket buffer doesn't back up.
            json.loads(ws.receive_text())

    # Untruncated total reflects every frame; retained buffer is capped.
    assert session.server_timing_total == cap + 50
    assert len(session.server_timing_samples) == cap
    assert session.server_timing_total > len(session.server_timing_samples)

    # finish surfaces both numbers.
    r = client.post(f"/api/realtime/session/{sid}/finish", json={})
    assert r.status_code == 200
    ts = r.json()["timing_summary"]
    assert ts["frames_processed"] == cap + 50
    assert ts["samples_retained"]["server"] == cap
    assert ts["samples_retained"]["server"] < ts["frames_processed"]
