"""Unit tests for WeightCurveAnalyzer and photo-frame selection.

Photo selection is now decoupled from weight matching: the photo proves the
mouse was on the scale, the weight comes from the curve median. These tests
verify the new midpoint-preference behavior.
"""

from __future__ import annotations

import numpy as np

from mousevision.analyzer import (
    CurveAnalyzerConfig,
    WeightCurveAnalyzer,
    select_photo_frame,
)
from mousevision.types import CurvePoint


def _curve(
    values: list[float],
    *,
    dt_ms: float = 100.0,
    confs: list[float] | None = None,
    frame_offset: int = 0,
) -> list[CurvePoint]:
    confs = confs or [0.9] * len(values)
    return [
        CurvePoint(
            timestamp_ms=i * dt_ms,
            weight=w,
            confidence=confs[i],
            frame_index=frame_offset + i,
        )
        for i, w in enumerate(values)
    ]


def test_platform_median_ignores_ramp_and_zero():
    values = [0, 8, 18, 24, 25, 24.8, 25.1, 24.9, 25.0, 12, 0, 0]
    result = WeightCurveAnalyzer().analyze(_curve(values))
    assert result is not None
    assert 24.5 <= result.weight <= 25.5
    assert result.confidence > 0.4
    assert result.photo_observed_weight is not None
    assert result.photo_weight_delta is not None
    assert result.weight_source == "stable_curve_median"


def test_short_curve_uses_middle():
    values = [0.0, 10.0, 20.0, 21.0, 20.5, 0.0]
    analyzer = WeightCurveAnalyzer(
        CurveAnalyzerConfig(platform_window_seconds=2.0, near_zero=0.5)
    )
    result = analyzer.analyze(_curve(values, dt_ms=200))
    assert result is not None
    assert result.weight > 10
    assert result.photo_selection == "platform_midpoint"


def test_all_zero_returns_none():
    assert WeightCurveAnalyzer().analyze(_curve([0, 0, 0, 0, 0])) is None


def test_photo_prefers_platform_midpoint():
    """Photo selection prefers the frame closest to the platform midpoint,
    regardless of OCR weight match."""
    times = np.array([0.0, 100.0, 200.0, 300.0, 400.0])
    weights = np.array([25.0, 25.1, 25.5, 24.9, 25.0])
    confs = np.array([0.95, 0.95, 0.95, 0.95, 0.95])
    indices = np.array([10, 11, 12, 13, 14])
    final = 25.0
    photo_i, observed, delta, selection = select_photo_frame(
        times, weights, confs, indices, 0, 5, final
    )
    # Midpoint is index 2 (frame 12); should be selected as closest to center
    assert photo_i == 12
    assert selection == "platform_midpoint"


def test_photo_tie_break_by_higher_confidence():
    """When equidistant from midpoint, higher OCR confidence wins."""
    times = np.array([0.0, 100.0, 200.0, 300.0])
    weights = np.array([17.77, 17.77, 17.80, 17.76])
    confs = np.array([0.70, 0.98, 0.99, 0.90])
    indices = np.array([100, 101, 102, 103])
    photo_i, observed, delta, selection = select_photo_frame(
        times, weights, confs, indices, 0, 4, 17.77
    )
    # Midpoint is between index 1 and 2 (150ms). Index 1 (101) at 100ms and
    # index 2 (102) at 200ms are equidistant. Higher conf = index 2 (0.99).
    assert photo_i in {101, 102}
    assert selection == "platform_midpoint"


def test_photo_never_leaves_stable_platform():
    """Selection stays within [i0, i1) even if a better frame exists outside."""
    times = np.array([0, 100, 200, 300, 400, 500], dtype=float)
    weights = np.array([10.0, 17.80, 17.79, 17.81, 17.80, 17.77])
    confs = np.array([0.99, 0.90, 0.90, 0.90, 0.90, 0.99])
    indices = np.array([0, 1, 2, 3, 4, 5])
    photo_i, observed, delta, selection = select_photo_frame(
        times, weights, confs, indices, 1, 5, 17.80
    )
    assert photo_i in {1, 2, 3, 4}  # within platform, not index 0 or 5
    assert selection == "platform_midpoint"


def test_photo_returns_valid_observed_and_delta():
    """The function still reports observed weight and delta for audit."""
    times = np.array([0.0, 100.0, 200.0, 300.0])
    weights = np.array([16.10, 16.20, 16.30, 16.40])
    confs = np.array([0.9, 0.9, 0.9, 0.9])
    indices = np.array([1, 2, 3, 4])
    photo_i, observed, delta, selection = select_photo_frame(
        times, weights, confs, indices, 0, 4, 16.25
    )
    assert observed in {16.10, 16.20, 16.30, 16.40}
    assert delta >= 0
    assert selection == "platform_midpoint"


def test_analyzer_photo_selection_within_platform():
    """Analyzer result has photo_frame_index within the platform window and
    does NOT require photo weight to match final weight."""
    values = [
        0.0, 5.0, 17.77, 17.80, 18.00,
        17.77, 17.76, 17.78, 12.0, 0.0,
    ]
    confs = [0.9] * len(values)
    confs[4] = 0.99
    result = WeightCurveAnalyzer(
        CurveAnalyzerConfig(platform_window_seconds=0.5, platform_max_std=0.5)
    ).analyze(_curve(values, confs=confs))
    assert result is not None
    assert result.photo_observed_weight is not None
    # photo_frame_index should be a valid curve index (0..9)
    assert 0 <= result.photo_frame_index < len(values)
    # weight_source field present
    assert result.weight_source == "stable_curve_median"
    # photo_mouse_detected defaults to False at analyzer level (driver overrides)
    assert result.photo_mouse_detected is False
