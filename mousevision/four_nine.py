"""Shared 4↔9 seven-segment glare confusion resolution.

Classic seven-segment LCDs under glare: segment 'g' (middle bar) fills in,
turning a '4' (segments b,c,f,g) into a '9' (segments a,b,c,d,f,g). The
reverse (9→4) is implausible because it would require removing segments.
Therefore the LOWER value in a ~5g pair is almost always the real reading.

This module centralizes the detection and resolution logic used by:
- TemporalWeightFusion (frame-level consensus)
- analyze_raw_samples (session-level raw clustering)
- WeightCurveAnalyzer (curve platform selection)
"""

from __future__ import annotations


# The weight gap between a 4→9 confusion pair depends on digit position:
#   ones place: 4 vs 9 → gap = 5.0g
# We only handle the ones-place case (by far the most common).
FOUR_NINE_GAP_LO = 4.5
FOUR_NINE_GAP_HI = 5.5


def is_four_nine_pair(weight_a: float, weight_b: float) -> bool:
    """Return True if two weights are ~5g apart (potential 4↔9 confusion)."""
    gap = abs(float(weight_a) - float(weight_b))
    return FOUR_NINE_GAP_LO <= gap <= FOUR_NINE_GAP_HI


def prefer_four(
    digits_a: list[str] | None,
    digits_b: list[str] | None,
) -> int:
    """Return -1 if A should be preferred (has '4'), +1 if B, 0 if no evidence.

    Checks the ones-place digit (index 1 in a 4-digit display like "24.18").
    """
    def _ones_digit(digits: list[str] | None) -> str | None:
        if digits and len(digits) >= 2:
            return digits[1]
        return None

    da = _ones_digit(digits_a)
    db = _ones_digit(digits_b)
    if da == "4" and db == "9":
        return -1  # A is the '4' reading → prefer A
    if da == "9" and db == "4":
        return 1   # B is the '4' reading → prefer B
    return 0


def resolve_four_nine_clusters(
    clusters: list[tuple],
    *,
    weight_idx: int = 0,
    votes_idx: int = 1,
    conf_idx: int = 2,
    digits_idx: int = 3,
    min_votes: int = 3,
) -> list[tuple]:
    """Sort clusters with 4↔9 awareness.

    When the top two clusters form a 4↔9 pair (~5g apart):
    - If digit evidence shows '4' vs '9', prefer the '4' cluster.
    - If no digit evidence, prefer the lower weight (glare direction is 4→9).
    - Guard: never let a weak '4' cluster steal from a strong '9' plateau.

    Args:
        clusters: List of tuples with (weight, votes, conf, digits, ...).
        weight_idx: Index of weight in each tuple.
        votes_idx: Index of vote count.
        conf_idx: Index of confidence.
        digits_idx: Index of digit list.
        min_votes: Minimum votes for a cluster to stand on its own.

    Returns:
        Re-sorted list (best first). Does not modify the input list.
    """
    if len(clusters) < 2:
        return list(clusters)

    result = list(clusters)
    c0, c1 = result[0], result[1]
    w0 = float(c0[weight_idx])
    w1 = float(c1[weight_idx])

    if not is_four_nine_pair(w0, w1):
        return result

    # Identify which is higher and which is lower.
    if w0 > w1:
        hi, lo = c0, c1
        hi_idx, lo_idx = 0, 1
    else:
        hi, lo = c1, c0
        hi_idx, lo_idx = 1, 0

    hi_votes = int(hi[votes_idx])
    lo_votes = int(lo[votes_idx])
    hi_digits = hi[digits_idx] if digits_idx < len(hi) else None
    lo_digits = lo[digits_idx] if digits_idx < len(lo) else None

    # Guard: don't overturn a strong plateau with a weak rival.
    if hi_votes >= min_votes and hi_votes > lo_votes * 2:
        return result  # strong '9' plateau stands

    # Check digit evidence.
    pref = prefer_four(lo_digits, hi_digits)
    if pref == -1 and lo_idx != 0:
        # lo has '4', hi has '9' → promote lo to position 0.
        result[hi_idx], result[lo_idx] = result[lo_idx], result[hi_idx]
    elif pref == 0 and w0 > w1:
        # No digit evidence: prefer the lower weight (glare is 4→9).
        # Only swap when the higher weight is currently at position 0.
        result[0], result[1] = result[1], result[0]

    return result
