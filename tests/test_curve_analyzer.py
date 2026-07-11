"""Unit tests for WeightCurveAnalyzer and photo-frame selection."""

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
    assert result.photo_weight_delta <= 0.02 + 1e-9


def test_short_curve_uses_middle():
    values = [0.0, 10.0, 20.0, 21.0, 20.5, 0.0]
    analyzer = WeightCurveAnalyzer(
        CurveAnalyzerConfig(platform_window_seconds=2.0, near_zero=0.5)
    )
    result = analyzer.analyze(_curve(values, dt_ms=200))
    assert result is not None
    assert result.weight > 10
    assert result.photo_selection in {
        "closest_stable_weight",
        "closest_high_conf",
        "closest_in_platform",
    }


def test_all_zero_returns_none():
    assert WeightCurveAnalyzer().analyze(_curve([0, 0, 0, 0, 0])) is None


def test_photo_prefers_matching_frame_over_midpoint():
    # Platform indices 0..4; midpoint is index 2 (25.5) but final median is 25.0.
    # Matching 25.0 exists at index 0 and 4; midpoint would be wrong.
    times = np.array([0.0, 100.0, 200.0, 300.0, 400.0])
    weights = np.array([25.0, 25.1, 25.5, 24.9, 25.0])
    confs = np.array([0.95, 0.95, 0.95, 0.95, 0.95])
    indices = np.array([10, 11, 12, 13, 14])
    final = 25.0
    photo_i, observed, delta, selection = select_photo_frame(
        times, weights, confs, indices, 0, 5, final, match_tol=0.02
    )
    assert photo_i in {10, 14}
    assert observed == 25.0
    assert delta == 0.0
    assert selection == "closest_stable_weight"
    assert photo_i != 12  # not the midpoint 25.5 frame


def test_photo_tie_break_by_higher_confidence():
    times = np.array([0.0, 100.0, 200.0, 300.0])
    weights = np.array([17.77, 17.77, 17.80, 17.76])
    confs = np.array([0.70, 0.98, 0.99, 0.90])
    indices = np.array([100, 101, 102, 103])
    photo_i, observed, delta, selection = select_photo_frame(
        times, weights, confs, indices, 0, 4, 17.77, match_tol=0.02
    )
    assert photo_i == 101  # same delta 0 as frame 100, higher conf
    assert observed == 17.77
    assert delta == 0.0
    assert selection == "closest_stable_weight"


def test_photo_never_leaves_stable_platform():
    # Ramp has an exact 17.77 match outside platform; must not pick it.
    # Full curve indices: 0 ramp, 1-4 platform (~17.8), 5 ramp with 17.77
    times = np.array([0, 100, 200, 300, 400, 500], dtype=float)
    weights = np.array([10.0, 17.80, 17.79, 17.81, 17.80, 17.77])
    confs = np.array([0.99, 0.90, 0.90, 0.90, 0.90, 0.99])
    indices = np.array([0, 1, 2, 3, 4, 5])
    # Platform is [1:5) — exclude index 5 which has exact match.
    photo_i, observed, delta, selection = select_photo_frame(
        times, weights, confs, indices, 1, 5, 17.80, match_tol=0.02
    )
    assert photo_i != 5
    assert photo_i in {1, 2, 3, 4}
    assert abs(observed - 17.80) <= 0.02 + 1e-9
    assert selection == "closest_stable_weight"


def test_photo_fallback_closest_when_outside_tolerance():
    times = np.array([0.0, 100.0, 200.0, 300.0])
    weights = np.array([16.10, 16.20, 16.30, 16.40])
    confs = np.array([0.9, 0.9, 0.9, 0.9])
    indices = np.array([1, 2, 3, 4])
    # final 16.25 — none within ±0.02; closest are 16.20 and 16.30 (delta 0.05)
    photo_i, observed, delta, selection = select_photo_frame(
        times, weights, confs, indices, 0, 4, 16.25, match_tol=0.02
    )
    assert photo_i in {2, 3}
    assert delta == 0.05
    assert selection == "closest_high_conf"


def test_photo_fallback_prefers_high_conf_over_closer_low_conf():
    times = np.array([0.0, 100.0, 200.0])
    weights = np.array([17.70, 17.77, 17.90])  # 17.77 exact but low conf
    confs = np.array([0.95, 0.20, 0.95])
    indices = np.array([1, 2, 3])
    photo_i, observed, delta, selection = select_photo_frame(
        times, weights, confs, indices, 0, 3, 17.77, match_tol=0.02, min_confidence=0.45
    )
    # Exact match is low-conf → skip; pick closest high-conf (17.70, delta 0.07)
    assert photo_i == 1
    assert observed == 17.70
    assert selection == "closest_high_conf"


def test_analyzer_photo_matches_final_inside_platform():
    # Midpoint frame is 18.0, but median is 17.77; matching frames exist in platform.
    values = [
        0.0,
        5.0,
        17.77,
        17.80,
        18.00,  # would be midpoint of a naive window
        17.77,
        17.76,
        17.78,
        12.0,
        0.0,
    ]
    confs = [0.9] * len(values)
    confs[4] = 0.99  # midpoint has high conf but wrong weight
    result = WeightCurveAnalyzer(
        CurveAnalyzerConfig(platform_window_seconds=0.5, platform_max_std=0.5)
    ).analyze(_curve(values, confs=confs))
    assert result is not None
    assert result.photo_observed_weight is not None
    assert abs(result.photo_observed_weight - result.weight) <= 0.02 + 1e-9
    assert result.photo_frame_index != 4  # not the mismatched midpoint
