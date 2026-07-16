"""New state-machine knobs: sustained ENTER, zero-hold abort, ENTER timeout,
bounded fusion zero-hold, and the photo-selection None-weight crash guard."""

from __future__ import annotations

import numpy as np
import pytest

from mousevision.detector import StateMachineConfig, WeighingState, WeighingStateMachine
from mousevision.driver import SessionDriver
from mousevision.fusion import TemporalFusionConfig, TemporalWeightFusion
from mousevision.reader.observations import RawWeightObservation
from mousevision.types import AnalysisResult


def test_enter_requires_sustained_reads():
    sm = WeighingStateMachine(StateMachineConfig(enter_sustain_frames=3))
    assert sm.update(100, 16.0, 0.9, 1) == WeighingState.EMPTY
    assert sm.update(200, None, 0.0, 2) == WeighingState.EMPTY  # breaks the run
    assert sm.update(300, 16.0, 0.9, 3) == WeighingState.EMPTY
    assert sm.update(400, 16.1, 0.9, 4) == WeighingState.EMPTY
    assert sm.update(500, 16.1, 0.9, 5) == WeighingState.ENTER
    # enter_ms tracks the first sustaining read, not the firing read
    assert sm.session.enter_ms == 300.0


def test_enter_zero_hold_frames_delays_abort():
    sm = WeighingStateMachine(
        StateMachineConfig(enter_zero_hold_frames=3, enter_abort_to_analyze=True)
    )
    sm.update(100, 16.0, 0.9, 1)
    assert sm.state == WeighingState.ENTER
    sm.update(200, 0.0, 0.9, 2)
    sm.update(300, 0.0, 0.9, 3)
    assert sm.state == WeighingState.ENTER  # two zeros: still holding
    # non-zero resets the zero counter
    sm.update(400, 16.1, 0.9, 4)
    sm.update(500, 0.0, 0.9, 5)
    sm.update(600, 0.0, 0.9, 6)
    assert sm.state == WeighingState.ENTER
    sm.update(700, 0.0, 0.9, 7)
    assert sm.state == WeighingState.ANALYZE
    assert sm.session.end_reason == "abort_short_session"


def test_enter_timeout_aborts_to_analyze():
    sm = WeighingStateMachine(
        StateMachineConfig(
            max_enter_ms=10_000.0,
            enter_abort_to_analyze=True,
            weighing_min_samples=10,  # never reached during the test
        )
    )
    sm.update(1_000, 16.0, 0.9, 1)
    assert sm.state == WeighingState.ENTER
    # flicker: nonzero but never enough for WEIGHING, never sees zero
    for i in range(4):
        sm.update(2_000 + i * 100, 16.1, 0.9, 2 + i)
    assert sm.state == WeighingState.ENTER
    sm.update(12_500, None, 0.0, 10)
    assert sm.state == WeighingState.ANALYZE
    assert sm.session.end_reason == "enter_timeout"


def test_legacy_defaults_unchanged():
    sm = WeighingStateMachine(StateMachineConfig())
    assert sm.config.enter_sustain_frames == 1
    assert sm.config.enter_zero_hold_frames == 1
    assert sm.config.max_enter_ms == 0.0
    # single read still opens ENTER; single zero still aborts
    assert sm.update(100, 16.0, 0.9, 1) == WeighingState.ENTER
    assert sm.update(200, 0.0, 0.9, 2) == WeighingState.EMPTY


def test_fusion_zero_hold_bounded():
    fusion = TemporalWeightFusion(
        TemporalFusionConfig(zero_hold_max_frames=2)
    )
    obs = RawWeightObservation(weight=0.0, status="zero_display", confidence=0.9)
    assert fusion.update(obs, mouse_present=True) is None  # held (1)
    assert fusion.update(obs, mouse_present=True) is None  # held (2)
    out = fusion.update(obs, mouse_present=True)  # bound reached: emit 0.0
    assert out is not None and out.weight == 0.0


def test_fusion_zero_hold_unbounded_by_default():
    fusion = TemporalWeightFusion(TemporalFusionConfig())
    obs = RawWeightObservation(weight=0.0, status="zero_display", confidence=0.9)
    for _ in range(10):
        assert fusion.update(obs, mouse_present=True) is None


def _driver_for_crash(tmp_path) -> SessionDriver:
    cfg = {
        "weight_reader": "template",
        "near_zero": 0.5,
        "match_threshold": 0.1,
    }
    return SessionDriver(
        config=cfg,
        templates_dir=tmp_path,
        output_root=tmp_path / "out",
        persist=False,
    )


def test_pick_best_survives_none_analysis_weight(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Regression: float(None) crash when analysis has no weight but a
    mouse-blob frame exists in the buffer."""
    driver = _driver_for_crash(tmp_path)
    from mousevision.buffer import RingFrameBuffer
    from mousevision.types import Frame

    driver.buffer = RingFrameBuffer(window_seconds=12.0, max_items=10)
    img = np.zeros((48, 64, 3), dtype=np.uint8)
    driver.buffer.push(Frame(image=img, timestamp_ms=1000.0, index=1), weight=None, weight_confidence=0.0)
    analysis = AnalysisResult(
        weight=None,
        confidence=0.0,
        platform_start_ms=900.0,
        platform_end_ms=1100.0,
        photo_frame_index=None,
        weight_source="manual_required",
        needs_review=True,
        requires_manual_weight=True,
    )
    # mouse blob "found" low in the frame so _pick_best runs with mouse_items
    monkeypatch.setattr(
        "mousevision.driver.detect_mouse_box", lambda *a, **k: (1, 30, 10, 10)
    )
    frame, mouse, label, idx, observed, delta = driver._select_photo_with_mouse(analysis)
    assert mouse is True
    assert observed == 0.0  # no weight anywhere → 0.0, not a crash
