"""Raw-cluster session analysis: verdicts, 4/9 confusion, orphan clusters."""

from __future__ import annotations

from mousevision.analyzer.raw_cluster import (
    RawClusterConfig,
    analyze_raw_samples,
    sustained_clusters,
)


def _samples(weights, conf=0.8, t0=1000.0, dt=130.0):
    return [(t0 + i * dt, w, conf, []) for i, w in enumerate(weights)]


def test_stable_dominant_cluster():
    v = analyze_raw_samples(_samples([17.51, 17.52, 17.50, 17.51, 17.52, 17.51]))
    assert v.status == "stable"
    assert v.weight == 17.51
    assert v.support_frac >= 0.99
    assert v.confidence >= 0.55


def test_spikes_ignored_when_dominant_strong():
    # settled 15.09 platform + two 37.11 OCR spikes (RefVideo S4 shape)
    v = analyze_raw_samples(
        _samples([15.10, 15.09, 37.11, 15.08, 15.10, 37.11, 15.09, 15.10, 15.09])
    )
    assert v.status == "stable"
    assert v.weight == 15.09


def test_conflict_keeps_dominant_weight():
    weights = [17.18, 17.16, 12.71, 13.21, 13.61, 18.11, 17.11, 13.81, 16.81, 16.64, 15.93]
    v = analyze_raw_samples(_samples(weights))
    assert v.status == "conflict"
    assert v.weight is not None
    assert 16.5 <= v.weight <= 17.5  # dominant 17.1x, not a garbage median
    assert "cluster_conflict" in v.reason


def test_insufficient_when_few_samples():
    assert analyze_raw_samples(_samples([17.5, 17.5])).status == "insufficient"
    assert analyze_raw_samples([]).status == "insufficient"


def test_no_dominant_still_reports_best_guess_with_review():
    # three equal-strength clusters: no stable winner, but the conflict
    # verdict still names a (reviewable) best guess instead of nothing
    weights = [10.1, 20.2, 30.3, 10.1, 20.2, 30.3, 10.1, 20.2, 30.3]
    v = analyze_raw_samples(_samples(weights))
    assert v.status == "conflict"
    assert v.weight in {10.1, 20.2, 30.3}
    assert v.support_frac < 0.60


def test_low_conf_samples_excluded():
    v = analyze_raw_samples(_samples([17.5] * 5, conf=0.10))
    assert v.status == "insufficient"


def test_four_nine_prefers_four_cluster():
    # 24.18 (real, with '4') vs 29.18 (glare) — near-equal votes: prefer 24.18
    samples = [(1000 + i * 130, 24.18, 0.8, ["2", "4", "1", "8"]) for i in range(4)]
    samples += [(1600 + i * 130, 29.18, 0.75, ["2", "9", "1", "8"]) for i in range(3)]
    v = analyze_raw_samples(samples)
    assert v.weight == 24.18


def test_recency_tiebreak_prefers_later_cluster():
    # equal votes/conf: settlement is the later plateau
    samples = [(1000 + i * 130, 19.0, 0.8, []) for i in range(4)]
    samples += [(3000 + i * 130, 10.0, 0.8, []) for i in range(4)]
    v = analyze_raw_samples(samples)
    assert v.weight == 10.0


def test_two_tuple_samples_accepted():
    v = analyze_raw_samples([(1000.0, 22.8), (1130.0, 22.8), (1260.0, 22.8)])
    assert v.status in {"stable", "conflict"}
    assert v.weight == 22.8


def test_stable_span_limit_rejects_wide_cluster():
    # one wide cluster (drift chain) with span > 0.25 → not "stable"
    weights = [22.10 + i * 0.05 for i in range(9)]  # span ~0.4
    v = analyze_raw_samples(_samples(weights))
    assert v.status != "stable"
    assert v.weight is not None


def test_four_nine_fold_recovers_split_plateau():
    # 0001 S6 server-decode shape: 24.1x real reads + 29.1x glare reads,
    # neither side reaching 3 votes alone -> folded into one 24.1x cluster
    samples = [
        (45265.5, 29.10, 0.80, ["2", "9", "1", "0"]),
        (45405.5, 24.14, 0.79, ["2", "4", "1", "4"]),
        (45564.4, 29.81, 0.72, ["2", "9", "8", "1"]),
        (45725.2, 29.11, 0.76, ["2", "9", "1", "1"]),
        (45921.0, 24.01, 0.65, ["2", "4", "0", "1"]),
    ]
    v = analyze_raw_samples(samples)
    assert v.weight is not None
    assert 23.9 <= v.weight <= 24.3  # folded toward the '4' reads
    # orphan scanner sees one 4-vote cluster instead of two 2-vote ones;
    # the lone 29.81 read folds to 24.81, 0.7g off the plateau, and stays out
    clusters = sustained_clusters(samples, min_votes=3, min_span_ms=250.0)
    assert len(clusters) == 1
    assert clusters[0]["votes"] == 4
    assert 23.9 <= clusters[0]["median"] <= 24.5


def test_four_nine_no_fold_when_digits_missing():
    samples = [(1000.0 + i * 200, 24.1, 0.8, []) for i in range(2)]
    samples += [(3000.0 + i * 200, 29.1, 0.8, []) for i in range(3)]
    clusters = sustained_clusters(samples, min_votes=2, min_span_ms=100.0)
    # without digit evidence the conservative path keeps both clusters
    assert len(clusters) == 2


def test_four_nine_fold_never_steals_strong_plateau():
    # mobile_0716_wang S2 shape: solid 19.0x plateau + one 9->4 glare misread;
    # the lone '4' cluster must not absorb the plateau's 8 votes
    samples = [(1000.0 + i * 130, 19.02, 0.8, ["1", "9", "0", "2"]) for i in range(8)]
    samples += [(2300.0 + i * 130, 10.93, 0.7, ["1", "0", "9", "3"]) for i in range(2)]
    samples += [(2600.0, 14.02, 0.75, ["1", "4", "0", "2"])]
    v = analyze_raw_samples(samples)
    assert v.status == "stable"
    assert 18.9 <= v.weight <= 19.1
    clusters = sustained_clusters(samples, min_votes=3, min_span_ms=250.0)
    assert len(clusters) == 1
    assert clusters[0]["votes"] == 8
    assert 18.9 <= clusters[0]["median"] <= 19.1


def test_four_nine_fold_when_four_side_at_least_as_strong():
    # 3x '4' votes vs 2x '9' votes: the majority '4' side absorbs glare reads
    samples = [(1000.0 + i * 130, 24.10, 0.8, ["2", "4", "1", "0"]) for i in range(3)]
    samples += [(2000.0 + i * 130, 29.10, 0.75, ["2", "9", "1", "0"]) for i in range(2)]
    clusters = sustained_clusters(samples, min_votes=2, min_span_ms=250.0)
    assert len(clusters) == 1
    assert clusters[0]["votes"] == 5
    assert 23.9 <= clusters[0]["median"] <= 24.3


def test_sustained_clusters_filters_short_and_thin():
    samples = _samples([17.5] * 4, dt=200.0)  # span 600ms, 4 votes
    samples += [(9000.0, 30.5, 0.8, []), (9130.0, 30.5, 0.8, [])]  # thin
    samples += [(20000.0, 12.3, 0.8, []), (20050.0, 12.3, 0.8, []), (20100.0, 12.3, 0.8, [])]  # short span
    clusters = sustained_clusters(samples, min_votes=3, min_span_ms=400.0)
    assert [(round(c["median"], 1), c["votes"]) for c in clusters] == [(17.5, 4)]


def test_sustained_clusters_respects_min_conf():
    samples = _samples([17.5] * 4, conf=0.2, dt=200.0)
    assert sustained_clusters(samples, min_conf=0.45) == []


def test_tiny_gap_merged_as_stable():
    """Two tight clusters 0.10g apart, temporally interleaved → stable."""
    from mousevision.analyzer.raw_cluster import RawClusterConfig, analyze_raw_samples

    cfg = RawClusterConfig(
        tol=0.12,
        stable_frac=0.60,
        stable_min_votes=4,
        stable_max_span=0.25,
        conflict_weight_tol=0.50,
    )
    # 5 reads around 18.20, 4 reads around 18.10 (gap ≈ 0.10g < 0.50)
    # Temporally interleaved (OCR jitter during the same platform).
    samples = [
        (0.0, 18.19, 0.8),
        (100.0, 18.09, 0.8),
        (200.0, 18.20, 0.8),
        (300.0, 18.10, 0.8),
        (400.0, 18.21, 0.8),
        (500.0, 18.11, 0.8),
        (600.0, 18.20, 0.8),
        (700.0, 18.10, 0.8),
        (800.0, 18.22, 0.8),
    ]
    verdict = analyze_raw_samples(samples, cfg)
    assert verdict.status == "stable"
    assert abs(verdict.weight - 18.20) < 0.15
    assert verdict.votes == 9  # merged


def test_dominant_cluster_suppresses_noise():
    """Strong top cluster + large-gap weak second → stable, not conflict."""
    from mousevision.analyzer.raw_cluster import RawClusterConfig, analyze_raw_samples

    cfg = RawClusterConfig(
        tol=0.12,
        stable_frac=0.60,
        stable_min_votes=4,
        stable_max_span=0.25,
        dominant_suppress_frac=0.65,
        dominant_gap_grams=5.0,
    )
    # 8 reads at 18.11, 3 reads at 11.88 (gap = 6.23g, support = 8/11 = 0.73)
    samples = (
        [(float(i * 100), 18.11, 0.8) for i in range(8)]
        + [(float(800 + i * 100), 11.88, 0.6) for i in range(3)]
    )
    verdict = analyze_raw_samples(samples, cfg)
    assert verdict.status == "stable"
    assert abs(verdict.weight - 18.11) < 0.15


def test_genuine_conflict_still_flagged():
    """Two strong clusters 2g apart → still conflict."""
    from mousevision.analyzer.raw_cluster import RawClusterConfig, analyze_raw_samples

    cfg = RawClusterConfig(
        tol=0.12,
        stable_frac=0.60,
        stable_min_votes=4,
        conflict_weight_tol=0.50,
        dominant_suppress_frac=0.65,
        dominant_gap_grams=5.0,
    )
    # 5 reads at 17.50, 5 reads at 15.50 (gap = 2.0g, neither dominant)
    samples = (
        [(float(i * 100), 17.50, 0.8) for i in range(5)]
        + [(float(500 + i * 100), 15.50, 0.8) for i in range(5)]
    )
    verdict = analyze_raw_samples(samples, cfg)
    assert verdict.status == "conflict"
