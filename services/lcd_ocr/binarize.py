"""Shared strip/slot binarization with optional highlight masking."""

from __future__ import annotations

import cv2
import numpy as np


def to_binary(
    patch_bgr_or_gray: np.ndarray,
    *,
    thr_scale: float = 0.90,
    thr_floor: float = 160.0,
    mask_highlights: bool = True,
    highlight_pct: float = 99.0,
) -> np.ndarray:
    """Binarize an LCD strip/slot patch to white-ink-on-black.

    When ``mask_highlights`` is True, extreme specular blobs (above the
    highlight percentile) are softened before the adaptive percentile
    threshold so glare cannot inflate p92 and erase real strokes.
    Only applied when the highlight region is a small fraction of the
    patch (true specular), not when most bright pixels are ink.
    """
    if patch_bgr_or_gray.ndim == 3:
        gray = cv2.cvtColor(patch_bgr_or_gray, cv2.COLOR_BGR2GRAY)
    else:
        gray = patch_bgr_or_gray.copy()
    if gray.size == 0:
        return np.zeros((1, 1), dtype=np.uint8)

    work = gray.astype(np.float32)
    if mask_highlights and work.size >= 16:
        hi = float(np.percentile(work, highlight_pct))
        mid = float(np.percentile(work, 50))
        if hi > mid + 25:
            highlight_mask = work >= hi
            frac = float(np.mean(highlight_mask))
            # Specular glare is local (typically << 5%). If many pixels are
            # "highlight", they are likely real ink (synthetic tests / high
            # contrast glyphs) — do not clamp them.
            if 0.0 < frac <= 0.05:
                work = np.where(highlight_mask, mid, work)

    p92 = float(np.percentile(work, 92))
    thr = max(float(thr_floor), p92 * float(thr_scale))
    _, bw = cv2.threshold(work.astype(np.uint8), thr, 255, cv2.THRESH_BINARY)
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
    # Digits are bright on blue LCD → keep white-on-black.
    if float(np.mean(bw)) > 127:
        ink = float(np.count_nonzero(bw)) / float(bw.size)
        if ink > 0.55:
            bw = cv2.bitwise_not(bw)
    return bw
