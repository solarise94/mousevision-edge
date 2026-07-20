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
    method: str = "percentile",
) -> np.ndarray:
    """Binarize an LCD strip/slot patch to white-ink-on-black.

    When ``mask_highlights`` is True, extreme specular blobs (above the
    highlight percentile) are softened before the adaptive percentile
    threshold so glare cannot inflate p92 and erase real strokes.
    Only applied when the highlight region is a small fraction of the
    patch (true specular), not when most bright pixels are ink.

    Args:
        method: "percentile" (default, global P92) or "sauvola" (local
            adaptive threshold, more robust to uneven illumination).
    """
    if method == "sauvola":
        return to_binary_sauvola(
            patch_bgr_or_gray,
            thr_floor=thr_floor,
            mask_highlights=mask_highlights,
            highlight_pct=highlight_pct,
        )
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


def to_binary_sauvola(
    patch_bgr_or_gray: np.ndarray,
    *,
    window_size: int = 25,
    k: float = 0.20,
    thr_floor: float = 100.0,
    mask_highlights: bool = True,
    highlight_pct: float = 99.0,
) -> np.ndarray:
    """Binarize using Sauvola adaptive threshold (local mean/std based).

    Sauvola: T(x,y) = mean(x,y) * (1 + k * (std(x,y) / R - 1))
    where R = 128 (dynamic range of grayscale).

    More robust than global percentile thresholds when illumination is
    uneven (glare spots, vignetting). Falls back to global P92 when the
    patch is too small for a meaningful local window.

    Returns white-ink-on-black binary image (same convention as to_binary).
    """
    if patch_bgr_or_gray.ndim == 3:
        gray = cv2.cvtColor(patch_bgr_or_gray, cv2.COLOR_BGR2GRAY)
    else:
        gray = patch_bgr_or_gray.copy()
    if gray.size == 0:
        return np.zeros((1, 1), dtype=np.uint8)

    work = gray.astype(np.float32)

    # Highlight masking (same logic as to_binary).
    if mask_highlights and work.size >= 16:
        hi = float(np.percentile(work, highlight_pct))
        mid = float(np.percentile(work, 50))
        if hi > mid + 25:
            highlight_mask = work >= hi
            frac = float(np.mean(highlight_mask))
            if 0.0 < frac <= 0.05:
                work = np.where(highlight_mask, mid, work)

    h, w = work.shape
    # Sauvola needs a reasonable window; fall back to global for tiny patches.
    if h < 8 or w < 8:
        p92 = float(np.percentile(work, 92))
        thr = max(thr_floor, p92 * 0.90)
        _, bw = cv2.threshold(work.astype(np.uint8), thr, 255, cv2.THRESH_BINARY)
    else:
        # Ensure odd window size.
        ws = window_size if window_size % 2 == 1 else window_size + 1
        ws = min(ws, min(h, w) if min(h, w) % 2 == 1 else min(h, w) - 1)
        ws = max(3, ws)
        # OpenCV doesn't have native Sauvola, so compute manually:
        # T = mean * (1 + k * (std / 128 - 1))
        work_u8 = work.astype(np.uint8)
        work_f = work_u8.astype(np.float32)
        mean = cv2.blur(work_f, (ws, ws))
        sq_mean = cv2.blur(work_f ** 2, (ws, ws))
        std = np.sqrt(np.maximum(sq_mean - mean ** 2, 0))
        R = 128.0
        threshold = mean * (1.0 + k * (std / R - 1.0))
        threshold = np.maximum(threshold, thr_floor)
        bw = ((work > threshold) * 255).astype(np.uint8)

    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
    # Digits are bright on blue LCD → keep white-on-black.
    if float(np.mean(bw)) > 127:
        ink = float(np.count_nonzero(bw)) / float(bw.size)
        if ink > 0.55:
            bw = cv2.bitwise_not(bw)
    return bw
