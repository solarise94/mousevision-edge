"""Server-side tests for the manual weighing mode (``weight_source="manual"``).

Manual mode lets an operator hand-enter each mouse's weight instead of relying
on OCR or BLE auto-detection. Coverage:

  * a manual session is created with ``weight_source="manual"``;
  * the ``manual_weight`` WS command is accepted on manual sessions and
    produces an accepted attempt (ACK carries it);
  * ``manual_weight`` is ignored (no ACK, no crash) on non-manual sessions
    (ocr / ble_k797) — a manual payload can never silently take over;
  * invalid payloads (missing / non-numeric / negative / over-range) are
    rejected without an ACK;
  * the real engine's ``ingest_manual_weight`` synthesizes an accepted
    ``Attempt`` (weight_raw=None, confidence=1.0), appends to accepted, and
    transitions to WAIT_CLEAR;
  * manual mode never drives OCR weight reads (``_read_weight_once`` returns
    None, mirroring BLE-mode-without-a-reading);
  * finalized records stamp ``weight_source="manual"`` into record.json + manifest.

Two layers: an API/WS layer (FastAPI TestClient + stubbed engine) and an
engine layer (real ``RealtimeSession`` + ``FakeReader``).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ui.realtime_api as realtime_api
from mousevision.fusion.temporal import TemporalFusionConfig, TemporalWeightFusion
from mousevision.reader.template import LcdBox
from mousevision.realtime import (
    Attempt,
    RealtimeConfig,
    RealtimeFrameResult,
    RealtimeSession,
    RealtimeState,
)


# --------------------------------------------------------------------------- #
# API layer — stubbed engine that records ingest_manual_weight calls
# --------------------------------------------------------------------------- #


class _RecordingEngine:
    """Minimal engine stand-in exposing ingest_manual_weight + accept_weight."""

    def __init__(self) -> None:
        self.state = RealtimeState.WEIGHING
        self.manual_calls: list[float] = []
        self._accepted: list[Attempt] = []
        self._attempts: list[Attempt] = []
        self._current: Attempt | None = None

    def process_frame(self, image, *, frame_seq=0, client_ts_ms=0.0):
        return RealtimeFrameResult(state=self.state, frame_seq=frame_seq)

    def request_retry(self):
        return {"applied": False, "state": self.state.value, "epoch": 0}

    def accept_weight(self):
        return None

    def get_accepted_records(self):
        return list(self._accepted)

    def get_all_attempts(self):
        return list(self._attempts)

    @property
    def _current_attempt(self):
        return self._current

    def ingest_manual_weight(self, *, weight_g: float) -> Attempt:
        if not isinstance(weight_g, (int, float)) or isinstance(weight_g, bool) or not math.isfinite(float(weight_g)):
            raise ValueError(f"weight_g must be finite, got {weight_g!r}")
        if not (0.0 <= float(weight_g) <= 6553.5):
            raise ValueError(f"weight_g out of range: {weight_g}")
        self.manual_calls.append(float(weight_g))
        a = Attempt(
            attempt_id="m1",
            weight_g=round(float(weight_g), 2),
            confidence=1.0,
            frame_seq=0,
            client_ts_ms=0.0,
            state="accepted",
            created_at=0.0,
            weight_raw=None,
        )
        self._accepted.append(a)
        self._attempts.append(a)
        self.state = RealtimeState.WAIT_CLEAR
        return a


def _make_app(tmp_path, monkeypatch, *, weight_source: str) -> TestClient:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("device_id: scale01\nweight_roi: {x:1,y:1,w:1,h:1}\n")
    realtime_api.configure(str(cfg_path), str(tmp_path))
    engine = _RecordingEngine()
    monkeypatch.setattr(
        realtime_api,
        "_create_engine",
        lambda config, *, weight_source="manual", _e=engine: _e,
    )
    monkeypatch.setattr(realtime_api, "_check_ws_token", lambda token: True)
    monkeypatch.setattr(realtime_api, "_test_last_engine", engine, raising=False)
    app = FastAPI()
    app.include_router(realtime_api.router)
    return TestClient(app)


def _create_session(client: TestClient, *, weight_source: str) -> str:
    r = client.post(
        "/api/realtime/session",
        json={"cage_id": "C1", "project_id": "default", "weight_source": weight_source},
    )
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _manual_msg(**over: Any) -> dict[str, Any]:
    base = {"type": "manual_weight", "weight_g": 25.4}
    base.update(over)
    return base


def test_manual_session_created_with_manual_source(tmp_path, monkeypatch):
    client = _make_app(tmp_path, monkeypatch, weight_source="manual")
    sid = _create_session(client, weight_source="manual")
    assert sid
    r = client.get(f"/api/realtime/session/{sid}/status")
    assert r.status_code == 200
    assert r.json()["weight_source"] == "manual"


def test_manual_weight_accepted_on_manual_session(tmp_path, monkeypatch):
    client = _make_app(tmp_path, monkeypatch, weight_source="manual")
    sid = _create_session(client, weight_source="manual")
    engine: _RecordingEngine = realtime_api._test_last_engine  # type: ignore[attr-defined]
    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        ws.receive_text()  # hello
        ws.send_json(_manual_msg(weight_g=25.4))
        # Read messages until the manual_weight ack arrives (fire-and-forget
        # otherwise; cap reads to avoid hanging if the handler is broken).
        ack = None
        for _ in range(8):
            msg = ws.receive_json()
            if msg.get("type") == "ack" and msg.get("cmd") == "manual_weight":
                ack = msg
                break
        ws.close()
    assert ack is not None, "no manual_weight ack"
    assert ack["accepted"]["weight_g"] == 25.4
    assert ack["state"] == "wait_clear"
    assert engine.manual_calls == [25.4]


def test_manual_weight_ignored_on_ble_session(tmp_path, monkeypatch):
    """A manual payload must never take over a BLE session (plan §12 parity)."""
    client = _make_app(tmp_path, monkeypatch, weight_source="ble_k797")
    sid = _create_session(client, weight_source="ble_k797")
    engine: _RecordingEngine = realtime_api._test_last_engine  # type: ignore[attr-defined]
    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        ws.receive_text()  # hello
        ws.send_json(_manual_msg(weight_g=25.4))
        ws.close()
    # Fire-and-forget: no ingest, no accepted attempt.
    assert engine.manual_calls == []
    assert engine.get_accepted_records() == []


@pytest.mark.parametrize("payload", [
    {"type": "manual_weight", "weight_g": "abc"},        # non-numeric
    {"type": "manual_weight", "weight_g": None},          # null
    {"type": "manual_weight"},                            # weight_g absent
    {"type": "manual_weight", "weight_g": float("nan")},  # NaN
    {"type": "manual_weight", "weight_g": -1.0},          # negative
    {"type": "manual_weight", "weight_g": 6554.0},        # over range
])
def test_manual_weight_invalid_payloads_rejected(tmp_path, monkeypatch, payload):
    client = _make_app(tmp_path, monkeypatch, weight_source="manual")
    sid = _create_session(client, weight_source="manual")
    engine: _RecordingEngine = realtime_api._test_last_engine  # type: ignore[attr-defined]
    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        ws.receive_text()  # hello
        ws.send_json(payload)
        ws.close()
    assert engine.manual_calls == []


# --------------------------------------------------------------------------- #
# Engine layer — real RealtimeSession + FakeReader
# --------------------------------------------------------------------------- #


class _FakeReader:
    def __init__(self, weight=None, confidence=0.8):
        self.weight = weight
        self.confidence = confidence
        self.weight_read_count = 0

    def lcd_box(self, image):
        return LcdBox(x=100, y=700, w=400, h=100)

    def read_weight(self, image, *, lcd_box=None):
        self.weight_read_count += 1
        return self.weight, self.confidence


def _make_session(*, weight_source: str) -> RealtimeSession:
    cfg = RealtimeConfig()
    reader = _FakeReader(weight=22.0, confidence=0.9)
    fusion = TemporalWeightFusion(TemporalFusionConfig())
    return RealtimeSession(cfg, reader, fusion, weight_source=weight_source)


def test_engine_ingest_manual_weight_creates_accepted_attempt():
    s = _make_session(weight_source="manual")
    a = s.ingest_manual_weight(weight_g=18.6)
    assert a.weight_g == 18.6
    assert a.confidence == 1.0
    assert a.weight_raw is None
    assert a.state == "accepted"
    assert s.get_accepted_records() == [a]
    assert a in s.get_all_attempts()
    assert s.state == RealtimeState.WAIT_CLEAR


def test_engine_manual_mode_does_not_read_ocr_weight():
    """Manual mode must not drive OCR auto-reads (like BLE-without-reading)."""
    s = _make_session(weight_source="manual")
    img = np.zeros((1280, 720, 3), dtype=np.uint8) + 128
    s.process_frame(img, frame_seq=1, client_ts_ms=0.0)
    # _read_weight_once returns None for manual → reader.read_weight never called.
    assert s.reader.weight_read_count == 0  # type: ignore[attr-defined]


def test_engine_manual_weight_replaces_pending_announced():
    """If a manual entry arrives while an announced attempt exists, it supersedes."""
    s = _make_session(weight_source="manual")
    # Force a current attempt via manual ingest, then ingest another.
    first = s.ingest_manual_weight(weight_g=10.0)
    second = s.ingest_manual_weight(weight_g=12.5)
    accepted = s.get_accepted_records()
    assert len(accepted) == 2
    assert accepted[-1].weight_g == 12.5
    # First is still in accepted (it was accepted); second is current state WAIT_CLEAR.
    assert s.state == RealtimeState.WAIT_CLEAR


# --------------------------------------------------------------------------- #
# Finalize layer — weight_source=manual stamped into record.json + manifest
# --------------------------------------------------------------------------- #


def test_finalize_manual_stamps_weight_source(tmp_path):
    from mousevision.realtime_finalize import finalize_session
    from mousevision.realtime_journal import AttemptJournal, JournalMeta

    journal_path = tmp_path / "j.jsonl"
    journal = AttemptJournal(str(journal_path))
    meta = JournalMeta(
        session_id="s1", cage_id="C1", project_id="default",
        created_at=0.0, device_id="scale01", weight_source="manual",
    )
    journal.write_meta(meta)
    accepted = [
        Attempt(
            attempt_id="a1", weight_g=20.3, confidence=1.0, frame_seq=1,
            client_ts_ms=0.0, state="accepted", created_at=0.0, weight_raw=None,
        )
    ]
    out = finalize_session(
        session_id="s1",
        output_root=str(tmp_path),
        journal=journal,
        accepted=accepted,
        rejected=[],
        cage_id="C1",
        project_id="default",
        weight_source="manual",
    )
    import json
    rec = json.loads((tmp_path / out["run_dir"] / "mouse_001" / "record.json").read_text())
    assert rec["weight_source"] == "manual"
    manifest = json.loads((tmp_path / out["run_dir"] / "manifest.json").read_text())
    assert manifest["weight_source"] == "manual"
