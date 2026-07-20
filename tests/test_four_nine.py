"""Tests for the shared 4↔9 confusion resolution module."""

from mousevision.four_nine import (
    FOUR_NINE_GAP_HI,
    FOUR_NINE_GAP_LO,
    is_four_nine_pair,
    prefer_four,
    resolve_four_nine_clusters,
)


def test_is_four_nine_pair():
    assert is_four_nine_pair(24.18, 29.18) is True
    assert is_four_nine_pair(29.18, 24.18) is True
    assert is_four_nine_pair(24.18, 29.00) is True  # gap = 4.82
    assert is_four_nine_pair(24.18, 30.00) is False  # gap = 5.82
    assert is_four_nine_pair(24.18, 24.18) is False  # gap = 0
    assert is_four_nine_pair(17.22, 22.22) is True  # gap = 5.0


def test_prefer_four():
    assert prefer_four(["2", "4", "1", "8"], ["2", "9", "1", "8"]) == -1
    assert prefer_four(["2", "9", "1", "8"], ["2", "4", "1", "8"]) == 1
    assert prefer_four(["2", "4", "1", "8"], ["2", "4", "1", "8"]) == 0
    assert prefer_four(None, ["2", "9", "1", "8"]) == 0
    assert prefer_four([], []) == 0


def test_resolve_prefers_four_cluster():
    clusters = [
        (29.18, 4, 0.7, ["2", "9", "1", "8"]),  # '9' cluster, more votes
        (24.18, 3, 0.8, ["2", "4", "1", "8"]),  # '4' cluster, fewer votes
    ]
    result = resolve_four_nine_clusters(clusters, min_votes=3)
    # '4' cluster should be promoted to first
    assert result[0][0] == 24.18


def test_resolve_strong_nine_plateau_stands():
    clusters = [
        (29.18, 10, 0.9, ["2", "9", "1", "8"]),  # strong '9' plateau
        (24.18, 2, 0.5, ["2", "4", "1", "8"]),   # weak '4' rival
    ]
    result = resolve_four_nine_clusters(clusters, min_votes=3)
    # Strong '9' plateau should NOT be overturned by weak '4'
    assert result[0][0] == 29.18


def test_resolve_no_digit_evidence_prefers_lower():
    clusters = [
        (29.18, 4, 0.7, []),
        (24.18, 4, 0.7, []),
    ]
    result = resolve_four_nine_clusters(clusters, min_votes=3)
    # No digit evidence → prefer lower weight (glare is 4→9)
    assert result[0][0] == 24.18


def test_resolve_non_four_nine_pair_unchanged():
    clusters = [
        (22.75, 5, 0.9, ["2", "2", "7", "5"]),
        (17.22, 3, 0.7, ["1", "7", "2", "2"]),
    ]
    result = resolve_four_nine_clusters(clusters, min_votes=3)
    assert result[0][0] == 22.75  # unchanged


def test_resolve_four_already_first_stays():
    """When the '4' cluster is already at position 0, it must NOT be swapped away."""
    clusters = [
        (24.18, 4, 0.8, ["2", "4", "1", "8"]),  # '4' cluster already first
        (29.18, 3, 0.7, ["2", "9", "1", "8"]),  # '9' cluster second
    ]
    result = resolve_four_nine_clusters(clusters, min_votes=3)
    assert result[0][0] == 24.18  # must stay first


def test_resolve_no_evidence_lower_already_first_stays():
    """No digit evidence + lower weight already first → no swap."""
    clusters = [
        (24.18, 4, 0.7, []),  # lower weight already first
        (29.18, 4, 0.7, []),
    ]
    result = resolve_four_nine_clusters(clusters, min_votes=3)
    assert result[0][0] == 24.18
