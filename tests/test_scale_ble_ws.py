"""Server-side tests for the K797 BLE scale WebSocket integration.

Covers the must-test matrix from the HarmonyOS K797 integration plan §12 that
is verifiable without a real scale or BLE hardware:

  * non-BLE sessions ignore ``scale_reading`` (and never let it take over OCR);
  * NaN / Infinity / negative / out-of-range / non-monotonic payloads are
    rejected and never reach the engine cache;
  * stale BLE readings (age > ``ble_stale_s``) do not enter the stable window
    and surface a ``scale_stale`` hint instead of a fake ``0 g``;
  * a BLE session never calls the OCR reader for weight;
  * the state machine resumes after a broadcast gap;
  * retry clears the old-epoch stable evidence (the same mouse is not
    re-announced from pre-retry reads);
  * finalized records stamp ``weight_source="ble_k797"`` into ``record.json``
    and the manifest;
  * the legacy OCR browser contract is unchanged (OCR sessions keep working
    and ``scale_reading`` is a no-op there).

Two layers: an API/WS layer driven through FastAPI's ``TestClient`` (mirrors
``test_realtime_contract.py``) and an engine layer driving
:class:`mousevision.realtime.RealtimeSession` directly with a ``FakeReader``
(mirrors ``test_realtime.py``).
"""

from __future__ import annotations

import json
import struct
import time
from typing import Any

import cv2
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ui.realtime_api as realtime_api
from mousevision.fusion.temporal import TemporalFusionConfig, TemporalWeightFusion
from mousevision.reader.template import LcdBox
from mousevision.realtime import (
    Attempt,
    QualityHint,
    RealtimeConfig,
    RealtimeFrameResult,
    RealtimeSession,
    RealtimeState,
)


# --------------------------------------------------------------------------- #
# Fakes — API layer (stubbed engine, like test_realtime_contract._StubEngine)
# --------------------------------------------------------------------------- #


class _RecordingEngine:
    """Minimal engine stand-in that records every ingest_scale_reading call.

    Unlike the OCR stub, this one exposes ``ingest_scale_reading`` so the WS
    handler's validation path actually runs against it. All other engine
    methods return benign canned results so the socket stays usable.
    """

    def __init__(self) -> None:
        self.state = RealtimeState.WEIGHING
        self.ingested: list[dict[str, Any]] = []
        self._last_sequence = -1
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

    def ingest_scale_reading(
        self,
        *,
        grams,
        raw,
        sequence,
        received_at_epoch_ms,
        stable=None,
        rssi=None,
    ) -> bool:
        # Mirror the real engine's monotonicity + range contract so the WS
        # handler's fire-and-forget path is exercised against the same rules.
        import math

        if isinstance(grams, bool) or not isinstance(grams, (int, float)) or not math.isfinite(float(grams)):
            raise ValueError(f"grams must be a finite number, got {grams!r}")
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValueError(f"raw must be int, got {raw!r}")
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise ValueError(f"sequence must be int, got {sequence!r}")
        if not (0.0 <= float(grams) <= 6553.5):
            raise ValueError(f"grams out of range: {grams}")
        if not (0 <= raw <= 65535):
            raise ValueError(f"raw out of range: {raw}")
        if abs(float(grams) - raw / 10.0) > 0.05:
            raise ValueError(f"grams/raw mismatch: {grams} {raw}")
        if sequence <= self._last_sequence:
            return False
        self._last_sequence = sequence
        self.ingested.append(
            {
                "grams": float(grams),
                "raw": int(raw),
                "sequence": int(sequence),
                "received_at_epoch_ms": int(received_at_epoch_ms),
                "stable": stable,
                "rssi": rssi,
            }
        )
        return True


def _make_ble_app(tmp_path, monkeypatch, *, weight_source: str) -> TestClient:
    """Build an app whose engine factory returns a shared _RecordingEngine.

    ``weight_source`` selects whether the session is OCR or BLE. The stub is
    reachable on the session so tests can assert on ``ingested``.
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("device_id: scale01\nweight_roi: {x:1,y:1,w:1,h:1}\n")
    realtime_api.configure(str(cfg_path), str(tmp_path))

    engine = _RecordingEngine()
    monkeypatch.setattr(
        realtime_api,
        "_create_engine",
        lambda config, *, weight_source=weight_source: engine,
    )
    monkeypatch.setattr(realtime_api, "_check_ws_token", lambda token: True)
    # Stash the stub on the module so the test can grab it after create_session.
    monkeypatch.setattr(realtime_api, "_test_last_engine", engine, raising=False)

    app = FastAPI()
    app.include_router(realtime_api.router)
    return app


def _create_session(client: TestClient, *, weight_source: str = "ble_k797") -> str:
    r = client.post(
        "/api/realtime/session",
        json={"cage_id": "C1", "project_id": "default", "weight_source": weight_source},
    )
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _scale_reading_msg(**overrides: Any) -> dict[str, Any]:
    """A canonical, valid scale_reading payload (26.3 g)."""
    base = {
        "type": "scale_reading",
        "source": "ble_k797",
        "grams": 26.3,
        "raw": 263,
        "sequence": 1,
        "received_at_epoch_ms": 1_700_000_000_000,
        "stable": True,
        "rssi": -49,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Fakes — engine layer (real RealtimeSession + FakeReader, like test_realtime)
# --------------------------------------------------------------------------- #


class _FakeReader:
    """Stand-in reader that counts weight reads (to prove BLE skips OCR)."""

    def __init__(self, weight=None, confidence=0.8, lcd_found=True):
        self.weight = weight
        self.confidence = confidence
        self.lcd_found = lcd_found
        self.weight_read_count = 0

    def lcd_box(self, image):
        if not self.lcd_found:
            return None
        return LcdBox(x=100, y=700, w=400, h=100)

    def read_weight(self, image, *, lcd_box=None):
        self.weight_read_count += 1
        return self.weight, self.confidence


def _good_frame():
    return np.zeros((1280, 720, 3), dtype=np.uint8) + 128


def _make_ble_session(monkeypatch, *, mouse=True, **config_kw):
    """Build a RealtimeSession wired for BLE (weight_source='ble_k797')."""
    mouse_state = {"present": mouse}

    def fake_detect(image, lcd, **kw):
        return (100, 100, 200, 200) if mouse_state["present"] else None

    monkeypatch.setattr("mousevision.realtime.detect_mouse_box", fake_detect)

    reader = _FakeReader(confidence=0.9, lcd_found=True)
    fusion = TemporalWeightFusion(TemporalFusionConfig(window_size=8, min_agree=3))

    defaults = dict(
        calibrate_min_frames=2,
        enter_sustain_frames=2,
        stable_min_frames=3,
        stable_min_raw_reads=3,
        stable_confirm_raw_reads=1,
        stable_min_span_ms=0.0,
        stable_max_age_s=1.6,
        enter_min=1.0,
        leave_max=0.30,
        empty_max=0.15,
        min_confidence=0.50,
        min_brightness=30.0,
        max_glare_ratio=0.15,
        mouse_smooth_window=1,
        mouse_advisory=True,
        stable_weight_tol=0.10,
        announce_hold_s=3.0,
        clear_timeout_s=30.0,
        ble_stale_s=10.0,
    )
    defaults.update(config_kw)
    config = RealtimeConfig(**defaults)

    session = RealtimeSession(config, reader, fusion, weight_source="ble_k797")
    return session, reader, mouse_state


# =========================================================================== #
# API / WS layer
# =========================================================================== #


def test_ble_session_accepts_valid_scale_reading(tmp_path, monkeypatch) -> None:
    """A valid scale_reading on a BLE session reaches the engine cache."""
    client = TestClient(_make_ble_app(tmp_path, monkeypatch, weight_source="ble_k797"))
    sid = _create_session(client, weight_source="ble_k797")
    engine = realtime_api._test_last_engine  # type: ignore[attr-defined]

    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        ws.receive_text()  # hello
        ws.send_text(json.dumps(_scale_reading_msg()))
        # Fire-and-forget: no ACK. Give the event loop a beat, then close.
        ws.close()

    assert len(engine.ingested) == 1
    got = engine.ingested[0]
    assert got["grams"] == 26.3
    assert got["raw"] == 263
    assert got["sequence"] == 1
    assert got["stable"] is True


def test_non_ble_session_ignores_scale_reading(tmp_path, monkeypatch) -> None:
    """§12: a non-BLE (OCR) session must reject/ignore scale_reading entirely."""
    client = TestClient(_make_ble_app(tmp_path, monkeypatch, weight_source="ocr"))
    sid = _create_session(client, weight_source="ocr")
    engine = realtime_api._test_last_engine  # type: ignore[attr-defined]

    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        ws.receive_text()  # hello
        ws.send_text(json.dumps(_scale_reading_msg()))
        ws.close()

    assert engine.ingested == []


@pytest.mark.parametrize(
    "overrides, field",
    [
        ({"grams": float("nan")}, "nan grams"),
        ({"grams": float("inf")}, "inf grams"),
        ({"grams": -1.0}, "negative grams"),
        ({"grams": 9999.9}, "grams out of range"),
        ({"raw": 70000}, "raw out of range"),
        ({"raw": -5}, "negative raw"),
        ({"grams": 26.3, "raw": 999}, "grams/raw mismatch"),
        ({"sequence": -1}, "negative sequence"),
        ({"grams": "abc"}, "non-numeric grams"),
        ({"raw": None}, "missing raw"),
        ({"source": "ocr"}, "wrong source tag"),
    ],
)
def test_invalid_scale_reading_payloads_rejected(
    tmp_path, monkeypatch, overrides, field
) -> None:
    """§12: NaN/Inf/negative/out-of-range/mismatched/bad-source payloads are
    rejected and never reach the engine cache."""
    client = TestClient(_make_ble_app(tmp_path, monkeypatch, weight_source="ble_k797"))
    sid = _create_session(client, weight_source="ble_k797")
    engine = realtime_api._test_last_engine  # type: ignore[attr-defined]

    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        ws.receive_text()  # hello
        ws.send_text(json.dumps(_scale_reading_msg(**overrides)))
        ws.close()

    assert engine.ingested == [], f"{field} should have been rejected"


def test_non_monotonic_sequence_dropped_not_cached(tmp_path, monkeypatch) -> None:
    """A reading whose sequence is not strictly greater than the last is dropped.

    The engine keeps the newer reading (seq=5); an older/duplicate (seq=5 again,
    then seq=3) must not overwrite it."""
    client = TestClient(_make_ble_app(tmp_path, monkeypatch, weight_source="ble_k797"))
    sid = _create_session(client, weight_source="ble_k797")
    engine = realtime_api._test_last_engine  # type: ignore[attr-defined]

    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        ws.receive_text()  # hello
        ws.send_text(json.dumps(_scale_reading_msg(sequence=5, grams=26.3)))
        ws.send_text(json.dumps(_scale_reading_msg(sequence=5, grams=99.9, raw=999)))
        ws.send_text(json.dumps(_scale_reading_msg(sequence=3, grams=11.1, raw=111)))
        ws.close()

    assert len(engine.ingested) == 1
    assert engine.ingested[0]["sequence"] == 5
    assert engine.ingested[0]["grams"] == 26.3


def test_numeric_string_and_int_grams_coerced(tmp_path, monkeypatch) -> None:
    """ grams/raw as numeric strings or ints are coerced like JSON-tolerant input."""
    client = TestClient(_make_ble_app(tmp_path, monkeypatch, weight_source="ble_k797"))
    sid = _create_session(client, weight_source="ble_k797")
    engine = realtime_api._test_last_engine  # type: ignore[attr-defined]

    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        ws.receive_text()  # hello
        ws.send_text(
            json.dumps(
                _scale_reading_msg(grams="26.3", raw="263", sequence="7", received_at_epoch_ms="1700000000001")
            )
        )
        ws.close()

    assert len(engine.ingested) == 1
    assert engine.ingested[0]["grams"] == 26.3
    assert engine.ingested[0]["raw"] == 263
    assert engine.ingested[0]["sequence"] == 7


def test_scale_reading_does_not_close_socket(tmp_path, monkeypatch) -> None:
    """A rejected scale_reading must not break the socket: a subsequent valid
    retry command still receives an ack."""
    client = TestClient(_make_ble_app(tmp_path, monkeypatch, weight_source="ble_k797"))
    sid = _create_session(client, weight_source="ble_k797")

    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        ws.receive_text()  # hello
        ws.send_text(json.dumps(_scale_reading_msg(grams=float("nan"))))
        # Socket still alive — send a frame to prove it.
        img = np.full((64, 64, 3), 128, dtype=np.uint8)
        ok, jpeg = cv2.imencode(".jpg", img)
        assert ok
        ws.send_bytes(struct.pack("<II", 1, 100) + jpeg.tobytes())
        # A per-frame state message should come back (socket survived).
        msg = json.loads(ws.receive_text())
        assert msg["type"] in {"state", "ack"}


# =========================================================================== #
# Engine layer
# =========================================================================== #


def test_ble_session_never_calls_ocr_for_weight(monkeypatch) -> None:
    """§12: a BLE session must not invoke the OCR reader.read_weight at all.
    The LCD locator is still allowed (needed for the mouse ROI), but weight
    reads are zero."""
    session, reader, _ = _make_ble_session(monkeypatch)
    img = _good_frame()

    # Feed a few frames in CALIBRATING/ARMED/WEIGHING territory. With no BLE
    # reading ingested, weight comes back None but read_weight is never called.
    for i in range(5):
        session.process_frame(img, frame_seq=i, client_ts_ms=float(i * 200))

    assert reader.weight_read_count == 0


def test_stale_ble_reading_surfaces_scale_stale_not_zero(monkeypatch) -> None:
    """§12 / §14: a stale (age > ble_stale_s) BLE cache yields a scale_stale
    hint and a None weight — never a fake 0 g."""
    session, reader, _ = _make_ble_session(
        monkeypatch, ble_stale_s=1.0, announce_hold_s=60.0
    )

    now = [0.0]
    monkeypatch.setattr(session, "_clock", lambda: now[0])

    img = _good_frame()
    # Drive into a weight-consuming state (ARMED/WEIGHING) with fresh reads,
    # stopping as soon as we leave CALIBRATING. announce_hold_s=60 prevents an
    # accidental announce during this short ramp.
    seq = 0
    for i in range(8):
        if session.state in {RealtimeState.ARMED, RealtimeState.WEIGHING}:
            break
        seq += 1
        session.ingest_scale_reading(
            grams=26.3, raw=263, sequence=seq,
            received_at_epoch_ms=1_700_000_000_000 + seq,
        )
        now[0] = i * 0.1
        session.process_frame(img, frame_seq=i, client_ts_ms=float(i * 200))

    assert session.state in {
        RealtimeState.ARMED,
        RealtimeState.WEIGHING,
    }, f"expected a weight-consuming state, got {session.state}"

    # Expire the BLE cache: advance the clock well past ble_stale_s (1.0s).
    now[0] = 5.0
    res = session.process_frame(img, frame_seq=99, client_ts_ms=9900.0)

    codes = {h.code for h in res.quality_hints}
    assert "scale_stale" in codes
    # No fabricated zero from the stale read.
    assert res.weight_candidate is None or res.weight_candidate != 0.0


def test_stale_reading_does_not_enter_stable_window(monkeypatch) -> None:
    """§12: with no fresh BLE reading (stale/missing), the engine never
    accumulates stable evidence and never reaches ANNOUNCED."""
    session, reader, _ = _make_ble_session(
        monkeypatch, ble_stale_s=1.0, announce_hold_s=0.0
    )

    now = [0.0]
    monkeypatch.setattr(session, "_clock", lambda: now[0])

    img = _good_frame()
    # Calibrate (LCD found) without ever injecting a BLE reading: the cache is
    # empty, so every frame sees a stale/missing read (None weight).
    for i in range(40):
        now[0] = i * 0.2  # advance past ble_stale_s on frame 5+
        session.process_frame(img, frame_seq=i, client_ts_ms=float(i * 200))

    # Never announced, because no fresh weight ever entered the stable window.
    assert session.state != RealtimeState.ANNOUNCED

    # In a weight-consuming state, the stale condition must be surfaced.
    res = session.process_frame(img, frame_seq=999, client_ts_ms=99900.0)
    if session.state in {
        RealtimeState.ARMED,
        RealtimeState.WEIGHING,
        RealtimeState.WAIT_CLEAR,
        RealtimeState.RETRY_REQUESTED,
    }:
        assert "scale_stale" in {h.code for h in res.quality_hints}


def test_broadcast_resume_keeps_state_machine_working(monkeypatch) -> None:
    """§12: after a stale gap, a fresh broadcast resumes evidence collection
    and the machine can reach ANNOUNCED again."""
    session, reader, _ = _make_ble_session(
        monkeypatch, ble_stale_s=2.0, announce_hold_s=0.0
    )

    now = [0.0]
    monkeypatch.setattr(session, "_clock", lambda: now[0])

    seq = 0
    img = _good_frame()
    # Drive to ARMED/WEIGHING with fresh reads.
    for i in range(6):
        seq += 1
        session.ingest_scale_reading(
            grams=26.3, raw=263, sequence=seq,
            received_at_epoch_ms=1_700_000_000_000 + seq,
        )
        now[0] = i * 0.1
        session.process_frame(img, frame_seq=i, client_ts_ms=float(i * 200))

    # A long stale gap.
    now[0] = 50.0
    for i in range(5):
        session.process_frame(img, frame_seq=50 + i, client_ts_ms=float((50 + i) * 200))

    # Resume fresh broadcasts — enough stable reads to announce.
    for i in range(10):
        seq += 1
        session.ingest_scale_reading(
            grams=26.3, raw=263, sequence=seq,
            received_at_epoch_ms=1_700_000_001_000 + seq,
        )
        now[0] = 60.0 + i * 0.1
        session.process_frame(img, frame_seq=200 + i, client_ts_ms=float((200 + i) * 200))
        if session.state == RealtimeState.ANNOUNCED:
            break

    assert session.state == RealtimeState.ANNOUNCED


def test_retry_clears_old_epoch_ble_evidence(monkeypatch) -> None:
    """§12 / §8.3: a retry bumps the weighing epoch so the stable window built
    before the retry cannot re-announce the same mouse. Fresh evidence must be
    collected in the new epoch before announcing again."""
    session, reader, _ = _make_ble_session(
        monkeypatch, ble_stale_s=30.0, announce_hold_s=0.0
    )

    now = [0.0]
    monkeypatch.setattr(session, "_clock", lambda: now[0])

    seq = 0
    img = _good_frame()
    # Reach ANNOUNCED.
    for i in range(20):
        seq += 1
        session.ingest_scale_reading(
            grams=26.3, raw=263, sequence=seq,
            received_at_epoch_ms=1_700_000_000_000 + seq,
        )
        now[0] = i * 0.1
        session.process_frame(img, frame_seq=i, client_ts_ms=float(i * 200))
        if session.state == RealtimeState.ANNOUNCED:
            break
    assert session.state == RealtimeState.ANNOUNCED
    epoch_before = session.weighing_epoch

    # Operator rejects -> retry. This must clear the stable window.
    info = session.request_retry()
    assert info["applied"] is True
    assert session.weighing_epoch == epoch_before + 1
    assert session.state == RealtimeState.WEIGHING

    # A single frame right after retry must not re-announce from the old window.
    seq += 1
    session.ingest_scale_reading(
        grams=26.3, raw=263, sequence=seq,
        received_at_epoch_ms=1_700_000_000_000 + seq,
    )
    now[0] += 0.1
    res = session.process_frame(img, frame_seq=100, client_ts_ms=10000.0)
    assert res.state != RealtimeState.ANNOUNCED


# =========================================================================== #
# Finalization provenance
# =========================================================================== #


def _inject_announced_attempt(engine: RealtimeSession, *, aid: str, weight: float) -> Attempt:
    """Attach a synthetic in-flight ANNOUNCED attempt to a real engine.

    Used by finalize tests to exercise record persistence without driving the
    full detection pipeline. The attempt is registered both as the engine's
    current attempt and in its attempt log, then accepted.
    """
    att = Attempt(
        attempt_id=aid,
        weight_g=weight,
        confidence=1.0,
        frame_seq=1,
        client_ts_ms=200.0,
        state="announced",
        created_at=time.time(),
    )
    engine._attempts.append(att)  # type: ignore[attr-defined]
    engine._current_attempt = att  # type: ignore[attr-defined]
    engine._state = RealtimeState.ANNOUNCED  # type: ignore[attr-defined]
    accepted = engine.accept_weight()
    assert accepted is not None
    return accepted


def _make_finalize_app(tmp_path, monkeypatch) -> TestClient:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("device_id: scale01\nweight_roi: {x:1,y:1,w:1,h:1}\n")
    realtime_api.configure(str(cfg_path), str(tmp_path))
    # Real engine, but monkeypatch the mouse detector + reader so no OCR/ML.
    monkeypatch.setattr("mousevision.realtime.detect_mouse_box", lambda *a, **k: None)
    monkeypatch.setattr(realtime_api, "_check_ws_token", lambda token: True)
    app = FastAPI()
    app.include_router(realtime_api.router)
    return TestClient(app)


def test_ble_session_final_record_stamps_ble_k797(tmp_path, monkeypatch) -> None:
    """§16: a BLE session's finalized record.json + manifest carry
    weight_source='ble_k797'."""
    client = _make_finalize_app(tmp_path, monkeypatch)
    sid = _create_session(client, weight_source="ble_k797")

    import pathlib

    # The real mouse detector is stubbed off, so inject a synthetic accepted
    # attempt to exercise the finalize provenance path.
    sess = realtime_api._get_session(sid)  # type: ignore[attr-defined]
    _inject_announced_attempt(sess.engine, aid="att-ble-1", weight=26.3)

    r = client.post(f"/api/realtime/session/{sid}/finish", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["finalize"]["count"] == 1

    run_dirs = list(pathlib.Path(str(tmp_path)).glob("run_*"))
    assert len(run_dirs) == 1
    rec = json.loads((run_dirs[0] / "mouse_001" / "record.json").read_text())
    assert rec["weight"] == 26.3
    assert rec["weight_source"] == "ble_k797"
    manifest = json.loads((run_dirs[0] / "manifest.json").read_text())
    assert manifest["weight_source"] == "ble_k797"

    # The journal meta must also carry the source for recovery.
    jpath = realtime_api.journal_path(str(tmp_path), sid)
    summary = realtime_api.AttemptJournal.read(jpath)
    assert summary["meta"]["weight_source"] == "ble_k797"


# =========================================================================== #
# OCR contract unchanged
# =========================================================================== #


def test_ocr_session_default_weight_source_is_ocr(tmp_path, monkeypatch) -> None:
    """§12: a legacy OCR browser session (no weight_source) defaults to 'ocr'
    and is fully unaffected — scale_reading is a no-op, finalize tags 'ocr'."""
    client = _make_finalize_app(tmp_path, monkeypatch)
    r = client.post(
        "/api/realtime/session",
        json={"cage_id": "C1", "project_id": "default"},
    )
    assert r.status_code == 200
    sid = r.json()["session_id"]
    assert r.json()["weight_source"] == "ocr"

    # scale_reading on an OCR session is ignored.
    with client.websocket_connect(f"/api/realtime/ws?session_id={sid}") as ws:
        ws.receive_text()  # hello
        ws.send_text(json.dumps(_scale_reading_msg()))
        ws.close()

    # Finalize with a synthetic accepted attempt -> record stamped 'ocr'.
    sess = realtime_api._get_session(sid)  # type: ignore[attr-defined]
    _inject_announced_attempt(sess.engine, aid="att-ocr-1", weight=21.4)

    import pathlib

    r = client.post(f"/api/realtime/session/{sid}/finish", json={})
    assert r.status_code == 200
    run_dirs = list(pathlib.Path(str(tmp_path)).glob("run_*"))
    rec = json.loads((run_dirs[0] / "mouse_001" / "record.json").read_text())
    assert rec["weight_source"] == "ocr"
