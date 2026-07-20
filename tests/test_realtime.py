"""Unit tests for the realtime weighing session engine.

These tests exercise :class:`mousevision.realtime.RealtimeSession` end-to-end
using a :class:`FakeReader` (no OCR / templates) and a monkeypatched
``detect_mouse_box`` (no real mouse detector). Frame pixels are synthesised
so the quality gates (brightness / glare) are fully controlled.
"""

import threading
import time

import numpy as np

from mousevision.fusion.temporal import TemporalFusionConfig, TemporalWeightFusion
from mousevision.reader.template import LcdBox
from mousevision.realtime import (
    RealtimeConfig,
    RealtimeSession,
    RealtimeState,
)


# --------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------- #


class FakeReader:
    """Stand-in for :class:`TemplateReader`.

    Returns canned ``lcd_box`` / ``read_weight`` results. Both attributes
    are mutable so a test can flip the weight mid-stream
    (e.g. ``reader.weight = 0.0`` to simulate the scale clearing).
    """

    def __init__(self, weight=None, confidence=0.8, lcd_found=True):
        self.weight = weight
        self.confidence = confidence
        self.lcd_found = lcd_found

    def lcd_box(self, image):
        if not self.lcd_found:
            return None
        return LcdBox(x=100, y=700, w=400, h=100)

    def read_weight(self, image):
        return self.weight, self.confidence


# --------------------------------------------------------------------- #
# Frame factories
# --------------------------------------------------------------------- #


def _good_frame():
    """Mid-brightness frame: passes both brightness and glare checks."""
    return np.zeros((1280, 720, 3), dtype=np.uint8) + 128


def _dark_frame():
    """All-black frame: mean brightness 0 < min_brightness -> 'too_dark'."""
    return np.zeros((1280, 720, 3), dtype=np.uint8)


def _glare_frame():
    """All-white frame: every pixel saturated -> saturated_ratio 1.0 > max -> 'glare'."""
    return np.full((1280, 720, 3), 255, dtype=np.uint8)


# --------------------------------------------------------------------- #
# Session builder
# --------------------------------------------------------------------- #


def make_session(
    monkeypatch,
    *,
    mouse=False,
    weight=None,
    confidence=0.8,
    lcd_found=True,
    **config_kw,
):
    """Build a :class:`RealtimeSession` wired to fakes.

    Returns ``(session, reader, mouse_state)``. ``mouse_state`` is a mutable
    dict so a test can flip mouse presence mid-stream::

        mouse_state["present"] = False

    The detector is monkeypatched at ``mousevision.realtime.detect_mouse_box``
    (the name the module actually calls).
    """
    mouse_state = {"present": mouse}

    def fake_detect(image, lcd, **kw):
        return (100, 100, 200, 200) if mouse_state["present"] else None

    monkeypatch.setattr("mousevision.realtime.detect_mouse_box", fake_detect)

    reader = FakeReader(weight=weight, confidence=confidence, lcd_found=lcd_found)
    # Fusion with a small window so stable consensus arrives in a few frames.
    fusion = TemporalWeightFusion(TemporalFusionConfig(window_size=8, min_agree=3))

    defaults = dict(
        calibrate_min_frames=2,
        enter_sustain_frames=2,
        stable_min_frames=3,
        enter_min=1.0,
        leave_max=0.30,
        empty_max=0.15,
        min_confidence=0.50,
        min_brightness=30.0,
        max_glare_ratio=0.15,
        # window=1 -> deque maxlen 1 -> smoothing short-circuits (len < 3),
        # so mouse presence is exactly the patched detector's per-frame answer.
        mouse_smooth_window=1,
        stable_weight_tol=0.10,
        announce_hold_s=3.0,
        clear_timeout_s=30.0,
    )
    defaults.update(config_kw)
    config = RealtimeConfig(**defaults)

    session = RealtimeSession(config, reader, fusion)
    return session, reader, mouse_state


def _reach_announced(monkeypatch, **config_kw):
    """Drive a fresh session all the way to ANNOUNCED.

    Returns ``(session, reader, mouse_state)``. Breaks on the exact frame
    that enters ANNOUNCED, so a small ``announce_hold_s`` override will not
    have fired auto-accept yet.
    """
    session, reader, mouse_state = make_session(
        monkeypatch,
        weight=22.5,
        confidence=0.9,
        mouse=True,
        lcd_found=True,
        **config_kw,
    )
    img = _good_frame()
    for i in range(15):
        session.process_frame(img, frame_seq=i, client_ts_ms=float(i * 100))
        if session.state == RealtimeState.ANNOUNCED:
            return session, reader, mouse_state
    raise AssertionError(f"never reached ANNOUNCED (state={session.state})")


# --------------------------------------------------------------------- #
# CALIBRATING
# --------------------------------------------------------------------- #


def test_calibrating_to_armed(monkeypatch):
    """N consecutive good frames (LCD found) -> ARMED."""
    session, _, _ = make_session(monkeypatch, lcd_found=True, calibrate_min_frames=2)
    img = _good_frame()

    r1 = session.process_frame(img, frame_seq=0, client_ts_ms=0.0)
    assert r1.state == RealtimeState.CALIBRATING

    r2 = session.process_frame(img, frame_seq=1, client_ts_ms=100.0)
    assert r2.state == RealtimeState.ARMED


def test_calibrating_lcd_not_found(monkeypatch):
    """LCD missing -> stays CALIBRATING with an 'lcd_not_found' hint."""
    session, _, _ = make_session(monkeypatch, lcd_found=False)
    r = session.process_frame(_good_frame(), frame_seq=0, client_ts_ms=0.0)

    assert r.state == RealtimeState.CALIBRATING
    assert "lcd_not_found" in {h.code for h in r.quality_hints}


def test_calibrating_too_dark(monkeypatch):
    """Dark frame -> stays CALIBRATING with a 'too_dark' hint."""
    session, _, _ = make_session(monkeypatch)
    r = session.process_frame(_dark_frame(), frame_seq=0, client_ts_ms=0.0)

    assert r.state == RealtimeState.CALIBRATING
    assert "too_dark" in {h.code for h in r.quality_hints}


def test_calibrating_glare(monkeypatch):
    """Saturated frame -> stays CALIBRATING with a 'glare' hint."""
    session, _, _ = make_session(monkeypatch)
    r = session.process_frame(_glare_frame(), frame_seq=0, client_ts_ms=0.0)

    assert r.state == RealtimeState.CALIBRATING
    assert "glare" in {h.code for h in r.quality_hints}


# --------------------------------------------------------------------- #
# ARMED -> WEIGHING
# --------------------------------------------------------------------- #


def test_armed_to_weighing(monkeypatch):
    """Sustained weight above enter_min for enter_sustain_frames -> WEIGHING."""
    session, _, _ = make_session(
        monkeypatch, weight=10.0, confidence=0.9, enter_sustain_frames=2
    )
    img = _good_frame()

    # Two good frames calibrate.
    session.process_frame(img, frame_seq=0, client_ts_ms=0.0)
    session.process_frame(img, frame_seq=1, client_ts_ms=100.0)
    assert session.state == RealtimeState.ARMED

    # Two sustained > enter_min frames cross into WEIGHING on the second.
    session.process_frame(img, frame_seq=2, client_ts_ms=200.0)
    r = session.process_frame(img, frame_seq=3, client_ts_ms=300.0)
    assert r.state == RealtimeState.WEIGHING


# --------------------------------------------------------------------- #
# WEIGHING -> ANNOUNCED
# --------------------------------------------------------------------- #


def test_weighing_to_announced(monkeypatch):
    """Stable weight + mouse present for stable_min_frames -> ANNOUNCED."""
    session, _, _ = make_session(
        monkeypatch,
        weight=22.5,
        confidence=0.9,
        mouse=True,
        stable_min_frames=3,
    )
    img = _good_frame()

    attempt = None
    for i in range(15):
        r = session.process_frame(img, frame_seq=i, client_ts_ms=float(i * 100))
        if r.attempt is not None:
            attempt = r.attempt
            break

    assert attempt is not None, "never produced an Attempt"
    assert session.state == RealtimeState.ANNOUNCED
    assert attempt.state == "announced"
    assert attempt.weight_g is not None
    assert abs(attempt.weight_g - 22.5) < 0.5


# --------------------------------------------------------------------- #
# ANNOUNCED accept / retry
# --------------------------------------------------------------------- #


def test_announced_accept(monkeypatch):
    """accept_weight() -> WAIT_CLEAR; attempt marked accepted."""
    session, _, _ = _reach_announced(monkeypatch)

    attempt = session.accept_weight()
    assert attempt is not None
    assert attempt.state == "accepted"
    assert session.state == RealtimeState.WAIT_CLEAR
    accepted = session.get_accepted_records()
    assert len(accepted) == 1
    assert accepted[0] is attempt


def test_announced_retry(monkeypatch):
    """request_retry() -> RETRY_REQUESTED; attempt marked rejected."""
    session, _, _ = _reach_announced(monkeypatch)

    session.request_retry()
    assert session.state == RealtimeState.RETRY_REQUESTED

    attempts = session.get_all_attempts()
    assert any(a.state == "rejected" for a in attempts)
    assert len(session.get_accepted_records()) == 0


# --------------------------------------------------------------------- #
# WAIT_CLEAR / RETRY clear-down
# --------------------------------------------------------------------- #


def test_wait_clear_to_accepted(monkeypatch):
    """Weight drops to ~0 in WAIT_CLEAR -> ACCEPTED -> next frame ARMED."""
    session, reader, _ = _reach_announced(monkeypatch)
    session.accept_weight()
    assert session.state == RealtimeState.WAIT_CLEAR

    reader.weight = 0.0  # empty scale
    img = _good_frame()
    r1 = session.process_frame(img, frame_seq=100, client_ts_ms=10000.0)
    assert r1.state == RealtimeState.ACCEPTED

    # ACCEPTED is transient: the very next frame returns to ARMED.
    r2 = session.process_frame(img, frame_seq=101, client_ts_ms=10100.0)
    assert r2.state == RealtimeState.ARMED


def test_retry_to_armed(monkeypatch):
    """In RETRY_REQUESTED, weight drops to ~0 -> ARMED."""
    session, reader, _ = _reach_announced(monkeypatch)
    session.request_retry()
    assert session.state == RealtimeState.RETRY_REQUESTED

    reader.weight = 0.0
    r = session.process_frame(_good_frame(), frame_seq=100, client_ts_ms=10000.0)
    assert r.state == RealtimeState.ARMED


# --------------------------------------------------------------------- #
# WEIGHING abort
# --------------------------------------------------------------------- #


def test_weighing_mouse_leaves(monkeypatch):
    """Weight drops below leave_max for enter_sustain_frames -> back to ARMED."""
    session, reader, _ = make_session(
        monkeypatch,
        weight=22.5,
        confidence=0.9,
        mouse=True,
        enter_sustain_frames=2,
    )
    img = _good_frame()

    # Reach WEIGHING (2 calibrate + 2 armed-sustain).
    for i in range(4):
        session.process_frame(img, frame_seq=i, client_ts_ms=float(i * 100))
    assert session.state == RealtimeState.WEIGHING

    # Mouse leaves: weight drops below leave_max for enter_sustain_frames.
    reader.weight = 0.0
    session.process_frame(img, frame_seq=10, client_ts_ms=1000.0)  # leave_count = 1
    r = session.process_frame(img, frame_seq=11, client_ts_ms=1100.0)  # leave_count = 2 -> ARMED
    assert r.state == RealtimeState.ARMED


# --------------------------------------------------------------------- #
# Auto-accept / clear timeout
# --------------------------------------------------------------------- #


def test_auto_accept(monkeypatch):
    """announce_hold_s > 0 and enough wall-clock elapsed -> auto-accept."""
    session, _, _ = _reach_announced(monkeypatch, announce_hold_s=0.05)
    assert session.state == RealtimeState.ANNOUNCED

    # Wait past the hold window; the next frame should auto-accept.
    time.sleep(0.06)
    r = session.process_frame(_good_frame(), frame_seq=100, client_ts_ms=10000.0)

    assert r.state == RealtimeState.WAIT_CLEAR
    assert r.accepted_weight is not None
    assert len(session.get_accepted_records()) == 1


def test_clear_timeout(monkeypatch):
    """WAIT_CLEAR past clear_timeout_s (with non-empty scale) -> ARMED."""
    session, reader, _ = _reach_announced(monkeypatch, clear_timeout_s=0.05)
    session.accept_weight()
    assert session.state == RealtimeState.WAIT_CLEAR

    # Keep the scale non-zero so it cannot clear naturally; only timeout fires.
    reader.weight = 5.0
    time.sleep(0.06)
    r = session.process_frame(_good_frame(), frame_seq=100, client_ts_ms=10000.0)
    assert r.state == RealtimeState.ARMED


# --------------------------------------------------------------------- #
# Full cycle
# --------------------------------------------------------------------- #


def test_multiple_mice(monkeypatch):
    """Two full cycles produce exactly two accepted records."""
    session, reader, _ = make_session(
        monkeypatch,
        weight=22.5,
        confidence=0.9,
        mouse=True,
    )
    img = _good_frame()

    for cycle in range(2):
        # Drive to ANNOUNCED, then accept.
        announced = False
        seq = 0
        for i in range(20):
            r = session.process_frame(img, frame_seq=i, client_ts_ms=float(i * 100))
            if not announced and session.state == RealtimeState.ANNOUNCED:
                assert r.attempt is not None
                session.accept_weight()
                announced = True
                seq = i + 1
                break
        assert announced, f"cycle {cycle}: never reached ANNOUNCED"

        # Clear the scale so WAIT_CLEAR -> ACCEPTED -> ARMED.
        reader.weight = 0.0
        armed_again = False
        for i in range(seq, seq + 10):
            session.process_frame(img, frame_seq=i, client_ts_ms=float(i * 100))
            if session.state == RealtimeState.ARMED:
                armed_again = True
                break
        assert armed_again, f"cycle {cycle}: never returned to ARMED"

        # Re-arm a heavier mouse for the next cycle.
        reader.weight = 22.5

    accepted = session.get_accepted_records()
    assert len(accepted) == 2
    assert all(a.state == "accepted" for a in accepted)


# --------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------- #


def test_thread_safety(monkeypatch):
    """Concurrent request_retry during process_frame must not crash."""
    session, _, _ = _reach_announced(monkeypatch)
    img = _good_frame()

    stop = threading.Event()
    errors = []

    def fire_retry():
        while not stop.is_set():
            try:
                session.request_retry()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                return
            time.sleep(0.001)

    t = threading.Thread(target=fire_retry)
    t.start()
    try:
        for i in range(50):
            try:
                session.process_frame(img, frame_seq=i, client_ts_ms=float(i * 100))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                break
            time.sleep(0.001)
    finally:
        stop.set()
        t.join(timeout=5.0)

    assert not errors
    assert not t.is_alive()
