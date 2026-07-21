"""Unit tests for the realtime weighing session engine.

These tests exercise :class:`mousevision.realtime.RealtimeSession` end-to-end
using a :class:`FakeReader` (no OCR / templates) and a monkeypatched
``detect_mouse_box`` (no real mouse detector). Frame pixels are synthesised
so the quality gates (brightness / glare) are fully controlled.
"""

import threading
import time

import numpy as np
import pytest

from mousevision.fusion.temporal import TemporalFusionConfig, TemporalWeightFusion
from mousevision.reader.template import LcdBox
from mousevision.realtime import (
    RealtimeConfig,
    RealtimeSession,
    RealtimeState,
    validate_realtime_config,
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
        self.weights = None  # optional list consumed per read_weight call
        self._wi = 0

    def lcd_box(self, image):
        if not self.lcd_found:
            return None
        return LcdBox(x=100, y=700, w=400, h=100)

    def read_weight(self, image, *, lcd_box=None):
        if self.weights is not None:
            if self._wi < len(self.weights):
                w = self.weights[self._wi]
                self._wi += 1
                self.weight = w
            # else keep last self.weight
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
    for i in range(20):
        session.process_frame(img, frame_seq=i, client_ts_ms=float(i * 200))
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

    session.process_frame(img, frame_seq=0, client_ts_ms=0.0)
    session.process_frame(img, frame_seq=1, client_ts_ms=100.0)
    assert session.state == RealtimeState.ARMED

    session.process_frame(img, frame_seq=2, client_ts_ms=200.0)
    r = session.process_frame(img, frame_seq=3, client_ts_ms=300.0)
    assert r.state == RealtimeState.WEIGHING


def test_armed_evidence_carries_into_weighing(monkeypatch):
    """ARMED raw reads are kept; third consistent read can announce quickly."""
    session, reader, _ = make_session(
        monkeypatch,
        weight=22.5,
        confidence=0.9,
        mouse=True,
        enter_sustain_frames=2,
        stable_min_raw_reads=3,
    )
    img = _good_frame()
    # calibrate
    session.process_frame(img, frame_seq=0, client_ts_ms=0.0)
    session.process_frame(img, frame_seq=1, client_ts_ms=100.0)
    assert session.state == RealtimeState.ARMED

    # two ARMED sustains -> WEIGHING, evidence kept
    session.process_frame(img, frame_seq=2, client_ts_ms=200.0)
    session.process_frame(img, frame_seq=3, client_ts_ms=400.0)
    assert session.state == RealtimeState.WEIGHING
    assert len(session._raw_window) == 2

    # third consistent read -> pending candidate (NOT announced yet)
    r3 = session.process_frame(img, frame_seq=4, client_ts_ms=600.0)
    assert r3.state == RealtimeState.WEIGHING
    assert r3.attempt is None

    # fourth consistent read -> confirmation -> ANNOUNCED
    r = session.process_frame(img, frame_seq=5, client_ts_ms=800.0)
    assert r.state == RealtimeState.ANNOUNCED
    assert r.attempt is not None
    assert abs(r.attempt.weight_g - 22.5) < 0.01


# --------------------------------------------------------------------- #
# WEIGHING -> ANNOUNCED (raw stable suffix)
# --------------------------------------------------------------------- #


def test_weighing_to_announced(monkeypatch):
    """Stable raw reads -> ANNOUNCED with median weight."""
    session, _, _ = make_session(
        monkeypatch,
        weight=22.5,
        confidence=0.9,
        mouse=True,
        stable_min_raw_reads=3,
    )
    img = _good_frame()

    attempt = None
    for i in range(20):
        r = session.process_frame(img, frame_seq=i, client_ts_ms=float(i * 200))
        if r.attempt is not None:
            attempt = r.attempt
            break

    assert attempt is not None, "never produced an Attempt"
    assert session.state == RealtimeState.ANNOUNCED
    assert attempt.state == "announced"
    assert attempt.weight_g is not None
    assert abs(attempt.weight_g - 22.5) < 0.5


def test_platform_switch_does_not_announce_old_weight(monkeypatch):
    """16.14 × 3 -> 15.62 × 3 must not announce 16.14.

    Regression for the candidate-confirmation window. ARMED carries 2 reads
    of 16.14 into WEIGHING; the third 16.14 forms a *pending* candidate but
    must NOT announce. When the platform then switches to 15.62 the candidate
    is revoked, and only the new platform is announced.
    """
    session, reader, _ = make_session(
        monkeypatch,
        weight=16.14,
        confidence=0.9,
        mouse=True,
        enter_sustain_frames=2,
        stable_min_raw_reads=3,
        stable_confirm_raw_reads=1,
    )
    img = _good_frame()
    # calibrate
    for i in range(2):
        session.process_frame(img, frame_seq=i, client_ts_ms=float(i * 200))

    # Frame 2,3: ARMED sustain with 16.14 -> WEIGHING with 2 raw reads.
    session.process_frame(img, frame_seq=2, client_ts_ms=400.0)
    session.process_frame(img, frame_seq=3, client_ts_ms=600.0)
    assert session.state == RealtimeState.WEIGHING
    assert len(session._raw_window) == 2

    # Frame 4: third 16.14 -> pending candidate, but NO announcement.
    r4 = session.process_frame(img, frame_seq=4, client_ts_ms=800.0)
    assert r4.state == RealtimeState.WEIGHING, "3×16.14 must not announce"
    assert r4.attempt is None, "3×16.14 must only form a pending candidate"

    # Platform switch to 15.62 before a confirming read of 16.14 lands.
    reader.weight = 15.62
    announced_old = False
    for i, seq in enumerate(range(5, 12)):
        r = session.process_frame(img, frame_seq=seq, client_ts_ms=float(1000 + i * 200))
        if r.attempt is not None:
            # Must be the new platform, never 16.14
            assert abs(r.attempt.weight_g - 15.62) < 0.05, r.attempt.weight_g
            assert abs(r.attempt.weight_g - 16.14) > 0.1
            return
    raise AssertionError("never announced the new platform 15.62")


def test_three_consistent_reads_do_not_announce_immediately(monkeypatch):
    """With the candidate window, 3 consistent reads form a pending candidate
    but do not announce; a 4th confirming read is required."""
    session, reader, _ = make_session(
        monkeypatch,
        weight=20.0,
        confidence=0.9,
        mouse=True,
        enter_sustain_frames=2,
        stable_min_raw_reads=3,
        stable_confirm_raw_reads=1,
    )
    img = _good_frame()
    # calibrate
    session.process_frame(img, frame_seq=0, client_ts_ms=0.0)
    session.process_frame(img, frame_seq=1, client_ts_ms=100.0)
    # 2 ARMED sustain -> WEIGHING with 2 raw reads
    session.process_frame(img, frame_seq=2, client_ts_ms=200.0)
    session.process_frame(img, frame_seq=3, client_ts_ms=400.0)
    # third read -> pending candidate, no announce
    r = session.process_frame(img, frame_seq=4, client_ts_ms=600.0)
    assert r.attempt is None
    assert session.state == RealtimeState.WEIGHING
    assert session._pending_candidate is not None
    # fourth read -> confirm -> announce
    r = session.process_frame(img, frame_seq=5, client_ts_ms=800.0)
    assert r.attempt is not None
    assert session.state == RealtimeState.ANNOUNCED


def test_pending_candidate_revoked_on_platform_switch(monkeypatch):
    """3×16.14 forms pending; switching to 15.62 revokes it and restarts
    the candidate, which must announce 15.62 after enough confirming reads."""
    session, reader, _ = make_session(
        monkeypatch,
        weight=16.14,
        confidence=0.9,
        mouse=True,
        enter_sustain_frames=2,
        stable_min_raw_reads=3,
        stable_confirm_raw_reads=1,
    )
    img = _good_frame()
    session.process_frame(img, frame_seq=0, client_ts_ms=0.0)
    session.process_frame(img, frame_seq=1, client_ts_ms=100.0)
    session.process_frame(img, frame_seq=2, client_ts_ms=200.0)
    session.process_frame(img, frame_seq=3, client_ts_ms=400.0)
    assert session.state == RealtimeState.WEIGHING
    # third read -> pending 16.14
    session.process_frame(img, frame_seq=4, client_ts_ms=600.0)
    assert session._pending_candidate is not None
    assert abs(session._pending_candidate.median_weight - 16.14) < 0.01

    # Switch to 15.62. Once 3×15.62 form a new suffix, the candidate is
    # revoked (median mismatch) and rebuilt on the new platform; a fourth
    # 15.62 read confirms and announces 15.62 — never the stale 16.14.
    reader.weight = 15.62
    announced = None
    for i in range(5, 16):
        r = session.process_frame(img, frame_seq=i, client_ts_ms=float(800 + i * 200))
        if r.attempt is not None:
            announced = r.attempt
            break
    assert announced is not None, "should eventually announce 15.62"
    assert abs(announced.weight_g - 15.62) < 0.05
    assert abs(announced.weight_g - 16.14) > 0.1


def test_three_consistent_reads_announce_median(monkeypatch):
    session, reader, _ = make_session(
        monkeypatch,
        weight=20.0,
        confidence=0.9,
        mouse=True,
        enter_sustain_frames=2,
        stable_min_raw_reads=3,
    )
    img = _good_frame()
    reader.weights = [20.00, 20.10, 20.05, 20.08, 20.02]
    for i in range(10):
        r = session.process_frame(img, frame_seq=i, client_ts_ms=float(i * 200))
        if r.attempt is not None:
            assert 20.0 <= r.attempt.weight_g <= 20.10
            return
    raise AssertionError("never announced")


def test_duplicate_frame_seq_ignored(monkeypatch):
    session, _, _ = make_session(
        monkeypatch, weight=22.5, confidence=0.9, mouse=True, enter_sustain_frames=2
    )
    img = _good_frame()
    for i in range(2):
        session.process_frame(img, frame_seq=i, client_ts_ms=float(i * 200))
    session.process_frame(img, frame_seq=2, client_ts_ms=400.0)
    session.process_frame(img, frame_seq=3, client_ts_ms=600.0)
    assert session.state == RealtimeState.WEIGHING
    n = len(session._raw_window)
    # Duplicate seq must not grow the window.
    session.process_frame(img, frame_seq=3, client_ts_ms=700.0)
    assert len(session._raw_window) == n


def test_out_of_order_frame_seq_ignored(monkeypatch):
    session, _, _ = make_session(
        monkeypatch, weight=22.5, confidence=0.9, mouse=True, enter_sustain_frames=2
    )
    img = _good_frame()
    for i in range(2):
        session.process_frame(img, frame_seq=i, client_ts_ms=float(i * 200))
    session.process_frame(img, frame_seq=2, client_ts_ms=400.0)
    session.process_frame(img, frame_seq=3, client_ts_ms=600.0)
    n = len(session._raw_window)
    session.process_frame(img, frame_seq=1, client_ts_ms=800.0)
    assert len(session._raw_window) == n


def test_stale_epoch_reads_ignored_after_retry(monkeypatch):
    session, reader, _ = _reach_announced(monkeypatch)
    info = session.request_retry()
    assert info["applied"] is True
    assert session.state == RealtimeState.WEIGHING
    epoch = info["epoch"]
    # Manually inject a stale-epoch read into the window.
    from mousevision.realtime import RealtimeRawRead

    session._raw_window.append(
        RealtimeRawRead(
            frame_seq=999,
            client_ts_ms=99999.0,
            weight=99.9,
            confidence=0.99,
            epoch=epoch - 1,
        )
    )
    img = _good_frame()
    # Fresh reads at new epoch; stale must not participate in suffix.
    for i in range(5):
        r = session.process_frame(
            img, frame_seq=100 + i, client_ts_ms=float(10000 + i * 200)
        )
        if r.attempt is not None:
            assert abs(r.attempt.weight_g - 22.5) < 0.5
            assert abs(r.attempt.weight_g - 99.9) > 1.0
            return
    raise AssertionError("never re-announced after retry")


def test_2fps_stable_within_default_max_age(monkeypatch):
    """Three reads spaced 500ms (2fps) fit in default 1.6s max age."""
    session, _, _ = make_session(
        monkeypatch,
        weight=18.0,
        confidence=0.9,
        mouse=True,
        enter_sustain_frames=2,
        stable_min_raw_reads=3,
        stable_max_age_s=1.6,
    )
    img = _good_frame()
    # calibrate
    session.process_frame(img, frame_seq=0, client_ts_ms=0.0)
    session.process_frame(img, frame_seq=1, client_ts_ms=100.0)
    # 500ms spacing
    times = [0, 500, 1000, 1500, 2000]
    for i, t in enumerate(times):
        r = session.process_frame(img, frame_seq=2 + i, client_ts_ms=float(t))
        if r.attempt is not None:
            assert abs(r.attempt.weight_g - 18.0) < 0.05
            return
    raise AssertionError("2fps reads should stabilize within 1.6s")


def test_reads_older_than_max_age_are_pruned(monkeypatch):
    session, reader, _ = make_session(
        monkeypatch,
        weight=18.0,
        confidence=0.9,
        mouse=True,
        enter_sustain_frames=2,
        stable_min_raw_reads=3,
        stable_max_age_s=0.8,
    )
    img = _good_frame()
    session.process_frame(img, frame_seq=0, client_ts_ms=0.0)
    session.process_frame(img, frame_seq=1, client_ts_ms=100.0)
    # Two early reads
    session.process_frame(img, frame_seq=2, client_ts_ms=200.0)
    session.process_frame(img, frame_seq=3, client_ts_ms=400.0)
    assert session.state == RealtimeState.WEIGHING
    # Jump far ahead so early reads age out; need 3 fresh reads to form a
    # pending candidate, then a 4th confirming read to announce.
    for i in range(4):
        r = session.process_frame(
            img, frame_seq=10 + i, client_ts_ms=float(5000 + i * 200)
        )
        if i < 3:
            assert r.attempt is None
    # After 4 fresh reads within age, announce.
    assert session.state == RealtimeState.ANNOUNCED


def test_outlier_then_stable_suffix_recovers(monkeypatch):
    session, reader, _ = make_session(
        monkeypatch,
        weight=20.0,
        confidence=0.9,
        mouse=True,
        enter_sustain_frames=2,
        stable_min_raw_reads=3,
    )
    img = _good_frame()
    session.process_frame(img, frame_seq=0, client_ts_ms=0.0)
    session.process_frame(img, frame_seq=1, client_ts_ms=100.0)
    # two enter
    session.process_frame(img, frame_seq=2, client_ts_ms=200.0)
    session.process_frame(img, frame_seq=3, client_ts_ms=400.0)
    # outlier
    reader.weight = 25.0
    session.process_frame(img, frame_seq=4, client_ts_ms=600.0)
    # recover with 3×20.0
    reader.weight = 20.0
    for i in range(5, 10):
        r = session.process_frame(img, frame_seq=i, client_ts_ms=float(i * 200))
        if r.attempt is not None:
            assert abs(r.attempt.weight_g - 20.0) < 0.05
            return
    raise AssertionError("should recover after outlier")


def test_mouse_absent_advisory_does_not_clear_window(monkeypatch):
    session, reader, mouse_state = make_session(
        monkeypatch,
        weight=22.5,
        confidence=0.9,
        mouse=True,
        enter_sustain_frames=2,
        mouse_advisory=True,
        stable_min_raw_reads=5,  # keep from announcing during this test
    )
    img = _good_frame()
    for i in range(4):
        session.process_frame(img, frame_seq=i, client_ts_ms=float(i * 200))
    assert session.state == RealtimeState.WEIGHING
    n = len(session._raw_window)
    assert n >= 2
    mouse_state["present"] = False
    r = session.process_frame(img, frame_seq=4, client_ts_ms=800.0)
    assert len(session._raw_window) >= n  # not cleared
    assert "mouse_uncertain" in {h.code for h in r.quality_hints}


def test_attempt_confidence_from_suffix_median(monkeypatch):
    session, reader, _ = make_session(
        monkeypatch,
        weight=22.5,
        confidence=0.9,
        mouse=True,
        enter_sustain_frames=2,
        stable_min_raw_reads=3,
    )
    img = _good_frame()
    confs = [0.6, 0.8, 0.7, 0.7]
    # calibrate
    session.process_frame(img, frame_seq=0, client_ts_ms=0.0)
    session.process_frame(img, frame_seq=1, client_ts_ms=100.0)
    for i, c in enumerate(confs):
        reader.confidence = c
        r = session.process_frame(img, frame_seq=2 + i, client_ts_ms=float(200 + i * 200))
        if r.attempt is not None:
            # suffix median confidence stays ~0.7 across the confirming reads
            assert abs(r.attempt.confidence - 0.7) < 0.01
            return
    raise AssertionError("never announced")


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


def test_announced_retry_goes_to_weighing(monkeypatch):
    """request_retry() -> WEIGHING with new epoch; attempt rejected."""
    session, _, _ = _reach_announced(monkeypatch)
    epoch_before = session.weighing_epoch

    info = session.request_retry()
    assert info["applied"] is True
    assert info["state"] == "weighing"
    assert session.state == RealtimeState.WEIGHING
    assert info["epoch"] == epoch_before + 1
    assert len(session._raw_window) == 0

    attempts = session.get_all_attempts()
    assert any(a.state == "rejected" for a in attempts)
    assert len(session.get_accepted_records()) == 0


def test_retry_not_applied_outside_announced(monkeypatch):
    session, _, _ = make_session(monkeypatch, weight=10.0, confidence=0.9)
    info = session.request_retry()
    assert info["applied"] is False
    assert session.state == RealtimeState.CALIBRATING


# --------------------------------------------------------------------- #
# WAIT_CLEAR
# --------------------------------------------------------------------- #


def test_wait_clear_to_accepted(monkeypatch):
    """Weight drops to ~0 in WAIT_CLEAR -> ACCEPTED -> next frame ARMED."""
    session, reader, _ = _reach_announced(monkeypatch)
    session.accept_weight()
    assert session.state == RealtimeState.WAIT_CLEAR

    reader.weight = 0.0
    img = _good_frame()
    r1 = session.process_frame(img, frame_seq=100, client_ts_ms=10000.0)
    assert r1.state == RealtimeState.ACCEPTED

    r2 = session.process_frame(img, frame_seq=101, client_ts_ms=10100.0)
    assert r2.state == RealtimeState.ARMED


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

    for i in range(4):
        session.process_frame(img, frame_seq=i, client_ts_ms=float(i * 200))
    assert session.state == RealtimeState.WEIGHING

    reader.weight = 0.0
    session.process_frame(img, frame_seq=10, client_ts_ms=1000.0)
    r = session.process_frame(img, frame_seq=11, client_ts_ms=1200.0)
    assert r.state == RealtimeState.ARMED


# --------------------------------------------------------------------- #
# Auto-accept / clear timeout
# --------------------------------------------------------------------- #


def test_auto_accept(monkeypatch):
    """announce_hold_s > 0 and enough wall-clock elapsed -> auto-accept."""
    session, _, _ = _reach_announced(monkeypatch, announce_hold_s=0.05)
    assert session.state == RealtimeState.ANNOUNCED

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
        announced = False
        seq = 0
        for i in range(30):
            r = session.process_frame(img, frame_seq=i, client_ts_ms=float(i * 200))
            if not announced and session.state == RealtimeState.ANNOUNCED:
                assert r.attempt is not None
                session.accept_weight()
                announced = True
                seq = i + 1
                break
        assert announced, f"cycle {cycle}: never reached ANNOUNCED"

        reader.weight = 0.0
        armed_again = False
        for i in range(seq, seq + 10):
            session.process_frame(img, frame_seq=i, client_ts_ms=float(i * 200))
            if session.state == RealtimeState.ARMED:
                armed_again = True
                break
        assert armed_again, f"cycle {cycle}: never returned to ARMED"

        reader.weight = 22.5

    accepted = session.get_accepted_records()
    assert len(accepted) == 2
    assert all(a.state == "accepted" for a in accepted)


# --------------------------------------------------------------------- #
# Concurrency / config
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


def test_validate_realtime_config_rejects_bad_values():
    with pytest.raises(ValueError):
        validate_realtime_config(RealtimeConfig(stable_min_raw_reads=1))
    with pytest.raises(ValueError):
        validate_realtime_config(RealtimeConfig(stable_max_age_s=0))
    with pytest.raises(ValueError):
        validate_realtime_config(RealtimeConfig(min_confidence=0.0))
