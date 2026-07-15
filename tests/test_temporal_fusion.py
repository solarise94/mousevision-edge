"""Unit tests for TemporalWeightFusion."""

from __future__ import annotations

from mousevision.fusion import TemporalFusionConfig, TemporalWeightFusion
from mousevision.reader.observations import RawWeightObservation


def _obs(
    weight: float | None,
    *,
    status: str = "readable",
    conf: float = 0.9,
    digits: list[str] | None = None,
    digit_confs: list[float] | None = None,
) -> RawWeightObservation:
    if digits is None and weight is not None and status == "readable":
        text = f"{weight:05.2f}".replace(".", "")
        digits = list(text[:4]) if len(text) >= 4 else list(text.ljust(4, "0"))
    return RawWeightObservation(
        weight=weight,
        status=status,
        confidence=conf,
        quality=conf,
        digits=digits or [],
        digit_confidences=digit_confs or ([0.95] * len(digits or [])),
    )


def test_needs_three_of_five_consensus():
    fusion = TemporalWeightFusion(TemporalFusionConfig(window_size=5, min_agree=3))
    assert fusion.update(_obs(22.75)) is None
    assert fusion.update(_obs(22.75)) is None
    stable = fusion.update(_obs(22.75))
    assert stable is not None
    assert abs(stable.weight - 22.75) < 1e-6


def test_mouse_on_zero_holds():
    fusion = TemporalWeightFusion()
    out = fusion.update(_obs(0.0, status="zero_display"), mouse_present=True)
    assert out is None


def test_zero_without_mouse_emits_zero():
    fusion = TemporalWeightFusion()
    out = fusion.update(_obs(0.0, status="zero_display"), mouse_present=False)
    assert out is not None
    assert out.weight == 0.0


def test_cluster_conflict_sets_review():
    fusion = TemporalWeightFusion(
        TemporalFusionConfig(
            window_size=8,
            min_agree=3,
            conflict_min_agree=2,
            cluster_conflict_ratio=0.3,
            stick_tol=0.05,
        )
    )
    for _ in range(3):
        fusion.update(_obs(22.16, digits=["2", "2", "1", "6"], digit_confs=[0.95] * 4))
    out = None
    for _ in range(2):
        out = fusion.update(
            _obs(22.76, digits=["2", "2", "7", "6"], digit_confs=[0.95] * 4)
        )
    assert out is None or abs(out.weight - 22.16) < 0.05
    assert fusion.last_needs_review or (out is not None and out.reason == "sticky_near")


def test_one_seven_requires_higher_confidence():
    fusion = TemporalWeightFusion(
        TemporalFusionConfig(window_size=5, min_agree=3, one_seven_min_confidence=0.70)
    )
    weak = _obs(
        22.16,
        digits=["2", "2", "1", "6"],
        digit_confs=[0.95, 0.95, 0.50, 0.95],
        conf=0.9,
    )
    for _ in range(5):
        assert fusion.update(weak) is None


def test_four_nine_prefers_higher_confidence():
    fusion = TemporalWeightFusion(
        TemporalFusionConfig(window_size=8, min_agree=3, conflict_min_agree=2)
    )
    for _ in range(3):
        fusion.update(
            _obs(29.18, digits=["2", "9", "1", "8"], digit_confs=[0.7] * 4, conf=0.70)
        )
    outs = []
    for _ in range(3):
        outs.append(
            fusion.update(
                _obs(24.18, digits=["2", "4", "1", "8"], digit_confs=[0.9] * 4, conf=0.90)
            )
        )
    later = [o for o in outs if o is not None]
    assert later
    assert abs(later[-1].weight - 24.18) < 0.2


def test_out_of_range_weight_ignored():
    fusion = TemporalWeightFusion(TemporalFusionConfig(window_size=5, min_agree=3))
    for _ in range(5):
        assert fusion.update(_obs(81.2)) is None
