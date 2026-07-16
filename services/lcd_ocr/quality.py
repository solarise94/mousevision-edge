"""Strip-level empty / transition quality gates (before weight composition)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
from binarize import to_binary as _shared_to_binary
import numpy as np


@dataclass
class StripQuality:
    status: str  # ok | zero_display | transition | unreadable
    reason: str
    ink_ratio: float
    evidence: dict[str, Any]


def _strip_binary(gray_or_bgr):
    return _shared_to_binary(gray_or_bgr, thr_scale=0.88, thr_floor=150.0)

def tall_glyph_ranges(strip_bgr: np.ndarray) -> list[tuple[int, int]]:
    """Return full-height digit components without assuming a digit count.

    This is used to recognize the scale's three-glyph ``0.00`` display before
    the normal four-slot splitter can accidentally split those glyphs into a
    plausible non-zero value.
    """
    bw = _strip_binary(strip_bgr)
    h, _w = bw.shape
    if h < 8:
        return []
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        (bw > 0).astype(np.uint8), 8
    )
    ranges: list[tuple[int, int]] = []
    for i in range(1, count):
        x, _y, w, ch, area = (int(v) for v in stats[i])
        aspect = float(w) / float(max(1, ch))
        if (
            ch >= int(0.65 * h)
            and 0.18 <= aspect <= 0.70
            and area >= int(0.06 * h * h)
        ):
            ranges.append((x, x + w))
    return sorted(ranges)


def assess_strip_quality(
    strip_bgr: np.ndarray,
    slot_patches: list[np.ndarray] | None = None,
) -> StripQuality:
    """Reject empty / transitional digit strips before composing a weight."""
    bw = _strip_binary(strip_bgr)
    ink_ratio = float(np.count_nonzero(bw)) / float(max(1, bw.size))
    evidence: dict[str, Any] = {"ink_ratio": round(ink_ratio, 4)}

    if ink_ratio < 0.012:
        return StripQuality("zero_display", "low_ink", ink_ratio, evidence)

    # Check slot geometry / ink consistency when slots are provided.
    if slot_patches and len(slot_patches) == 4:
        slot_inks: list[float] = []
        aspect_ratios: list[float] = []
        top_spans: list[float] = []
        for patch in slot_patches:
            sb = _strip_binary(patch)
            ys, xs = np.where(sb > 0)
            ink = float(len(xs)) / float(max(1, sb.size))
            slot_inks.append(ink)
            if len(xs) < 4:
                aspect_ratios.append(0.0)
                top_spans.append(0.0)
                continue
            h = int(ys.max()) - int(ys.min()) + 1
            w = int(xs.max()) - int(xs.min()) + 1
            aspect_ratios.append(float(w) / float(max(1, h)))
            band = sb[int(ys.min()) : int(ys.min()) + max(2, h // 6), int(xs.min()) : int(xs.max()) + 1]
            if band.size:
                col_on = (band > 0).any(axis=0)
                top_spans.append(float(np.mean(col_on)))
            else:
                top_spans.append(0.0)

        evidence["slot_inks"] = [round(x, 4) for x in slot_inks]
        evidence["aspects"] = [round(x, 3) for x in aspect_ratios]
        evidence["top_spans"] = [round(x, 3) for x in top_spans]

        # True empty: mostly blank slots.
        if sum(1 for x in slot_inks if x < 0.02) >= 3 and ink_ratio < 0.06:
            return StripQuality("zero_display", "mostly_blank_slots", ink_ratio, evidence)

        # Pseudo "1111" from residual glare lines: four very narrow, weak-stem slots
        # with almost no top bar (true zeros/empty often leave thin vertical noise).
        narrow = sum(1 for a in aspect_ratios if 0.0 < a < 0.28)
        weak_top = sum(1 for t in top_spans if t < 0.20)
        weak_ink = sum(1 for x in slot_inks if 0.01 < x < 0.08)
        if narrow >= 3 and weak_top >= 3 and weak_ink >= 2 and ink_ratio < 0.10:
            return StripQuality(
                "transition",
                "pseudo_narrow_ones",
                ink_ratio,
                evidence,
            )

        # Flicker / uneven occupancy typical of transition frames.
        nonzero = [x for x in slot_inks if x >= 0.015]
        if len(nonzero) in {1, 2} and ink_ratio < 0.08:
            return StripQuality("transition", "partial_slots", ink_ratio, evidence)

    # Mid ink with scattered speckles — often motion blur / refresh.
    if 0.02 <= ink_ratio < 0.035:
        return StripQuality("transition", "speckle_ink", ink_ratio, evidence)

    return StripQuality("ok", "ok", ink_ratio, evidence)
