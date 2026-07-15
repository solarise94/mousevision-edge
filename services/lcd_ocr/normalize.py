"""LCD normalization: prefer axis-aligned crop; warp only when skewed."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from locator import quad_to_bbox


@dataclass
class NormalizeConfig:
    width: int = 480
    height: int = 128
    # Relative ROI inside the LCD crop (axis-aligned or warped).
    # Defaults match TemplateReader digit band after excluding '+' / 'g'.
    digit_roi: tuple[float, float, float, float] = (0.20, 0.08, 0.66, 0.84)
    slot_count: int = 4
    slot_margin: float = 0.03
    ink_trim: bool = True
    ink_trim_pad: float = 0.05
    slot_mode: str = "projected"  # projected | fixed
    # Stage B: keep axis-aligned by default. Enable warp only when corners are trusted.
    allow_warp: bool = False
    skew_warp_min: float = 8.0
    skew_warp_threshold: float = 35.0


def _quad_skew(quad: list[tuple[float, float]] | list[list[float]]) -> float:
    """Rough non-rectangularity: max abs dy of top/bottom edges and dx of sides."""
    (tl, tr, br, bl) = [(float(p[0]), float(p[1])) for p in quad]
    top_dy = abs(tr[1] - tl[1])
    bot_dy = abs(br[1] - bl[1])
    left_dx = abs(bl[0] - tl[0])
    right_dx = abs(br[0] - tr[0])
    return float(max(top_dy, bot_dy, left_dx, right_dx))


def warp_screen(
    image: np.ndarray,
    quad: list[tuple[float, float]] | list[list[float]],
    *,
    width: int = 480,
    height: int = 128,
) -> np.ndarray:
    src = np.asarray(quad, dtype=np.float32).reshape(4, 2)
    dst = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(image, matrix, (width, height), flags=cv2.INTER_LINEAR)


def axis_aligned_screen(
    image: np.ndarray,
    quad: list[tuple[float, float]] | list[list[float]],
    *,
    width: int = 480,
    height: int = 128,
) -> np.ndarray:
    """Crop LCD bbox and resize to the standard canvas (no perspective)."""
    box = quad_to_bbox(quad)
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    ih, iw = image.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(iw, x + w), min(ih, y + h)
    crop = image[y0:y1, x0:x1]
    if crop.size == 0:
        return np.zeros((height, width, 3), dtype=np.uint8)
    return cv2.resize(crop, (width, height), interpolation=cv2.INTER_LINEAR)


def prepare_screen(
    image: np.ndarray,
    quad: list[tuple[float, float]] | list[list[float]],
    cfg: NormalizeConfig,
) -> tuple[np.ndarray, str]:
    """Return (standard_screen, method) where method is axis|warp.

    Stage B default: axis-aligned crop. Perspective warp is only used when
    explicitly enabled and skew is in a moderate band — bad quads with huge
    skew (mis-located corners) destroy digits if warped blindly.
    """
    if not cfg.allow_warp:
        return axis_aligned_screen(image, quad, width=cfg.width, height=cfg.height), "axis"

    skew = _quad_skew(quad)
    # Warp only for mild perspective; extreme skew usually means bad corners.
    if cfg.skew_warp_min <= skew <= cfg.skew_warp_threshold:
        return warp_screen(image, quad, width=cfg.width, height=cfg.height), "warp"
    return axis_aligned_screen(image, quad, width=cfg.width, height=cfg.height), "axis"


def crop_digit_strip(screen: np.ndarray, cfg: NormalizeConfig | None = None) -> np.ndarray:
    cfg = cfg or NormalizeConfig()
    h, w = screen.shape[:2]
    x, y, rw, rh = cfg.digit_roi
    x0 = int(np.clip(x * w, 0, w - 1))
    y0 = int(np.clip(y * h, 0, h - 1))
    x1 = int(np.clip((x + rw) * w, x0 + 1, w))
    y1 = int(np.clip((y + rh) * h, y0 + 1, h))
    return screen[y0:y1, x0:x1]


def _strip_binary(strip: np.ndarray) -> np.ndarray:
    if strip.ndim == 3:
        gray = cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY)
    else:
        gray = strip
    p92 = float(np.percentile(gray, 92))
    thr = max(165.0, p92 * 0.88)
    _, bw = cv2.threshold(gray, thr, 255, cv2.THRESH_BINARY)
    if float(np.count_nonzero(bw)) < gray.shape[0] * 4:
        _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        if float(np.mean(bw)) < 127:
            bw = cv2.bitwise_not(bw)
    return cv2.morphologyEx(bw, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))


def ink_trim_strip(strip: np.ndarray, *, pad: float = 0.05) -> np.ndarray:
    """Tighten to first→last ink column. Never split on inter-digit gaps."""
    if strip.size == 0:
        return strip
    h, w = strip.shape[:2]
    if w < 16:
        return strip
    bw = _strip_binary(strip)
    col = (bw > 0).sum(axis=0).astype(np.float32)
    thr_c = max(3.0, float(np.percentile(col[col > 0], 30)) if np.any(col > 0) else 3.0)
    active = np.where(col >= thr_c)[0]
    if len(active) < 10:
        return strip
    a, b = int(active[0]), int(active[-1]) + 1
    pad_px = max(3, int((b - a) * pad))
    a = max(0, a - pad_px)
    b = min(w, b + pad_px)
    if b - a < max(64, w // 3):
        return strip
    return strip[:, a:b]


def _run_height(bw: np.ndarray, a: int, b: int) -> tuple[int, float]:
    ys = np.where(bw[:, a:b].any(axis=1))[0]
    if len(ys) == 0:
        return 0, 0.0
    return int(ys.max() - ys.min() + 1), float(ys.mean()) / bw.shape[0]


def projected_slot_ranges(bw: np.ndarray) -> list[tuple[int, int]]:
    h, w = bw.shape
    col = (bw > 0).sum(axis=0).astype(np.float32)
    thresh = max(2.0, h * 0.10)
    active = col >= thresh
    runs: list[list[int]] = []
    i = 0
    while i < w:
        if not active[i]:
            i += 1
            continue
        j = i
        while j < w and active[j]:
            j += 1
        if j - i >= 3:
            runs.append([i, j])
        i = j

    merged: list[list[int]] = []
    idx = 0
    while idx < len(runs):
        a, b = runs[idx]
        ph, _ = _run_height(bw, a, b)
        width = b - a
        if idx + 1 < len(runs):
            na, nb = runs[idx + 1]
            gap = na - b
            nph, _ = _run_height(bw, na, nb)
            nwidth = nb - na
            short_left = (
                ph < h * 0.55
                and width < h * 0.30
                and gap <= 20
                and nph >= h * 0.55
                and nwidth < h * 0.30
            )
            tiny_gap = gap <= 2 and (width < h * 0.20 or nwidth < h * 0.20)
            if short_left or tiny_gap:
                merged.append([a, nb])
                idx += 2
                continue
        merged.append([a, b])
        idx += 1

    typical = max(18, int(h * 0.36))
    slots: list[tuple[int, int]] = []
    for a, b in merged:
        width = b - a
        ph, y_center = _run_height(bw, a, b)
        if ph == 0:
            continue
        if ph < h * 0.28 and y_center > 0.50:
            continue
        if a > w * 0.88 and (ph < h * 0.50 or width < 12):
            continue
        while width > typical * 1.60:
            mid0 = a + int(width * 0.25)
            mid1 = b - int(width * 0.25)
            if mid1 <= mid0 + 2:
                break
            cut = mid0 + int(np.argmin(col[mid0:mid1]))
            if cut - a >= 8 and b - cut >= 8:
                slots.append((a, cut))
                a = cut
                width = b - a
            else:
                break
        slots.append((a, b))
    return slots


def expand_seven_slots(bw: np.ndarray, slots: list[tuple[int, int]]) -> list[tuple[int, int]]:
    h, _ = bw.shape
    if not slots:
        return slots
    out: list[tuple[int, int]] = []
    for i, (a, b) in enumerate(slots):
        width = b - a
        ph, _ = _run_height(bw, a, b)
        if ph >= h * 0.45 and width < h * 0.22:
            search_left = max(0, a - int(h * 0.40))
            if i > 0:
                search_left = max(search_left, out[-1][1] + 2)
            top = bw[int(h * 0.02) : int(h * 0.20), search_left:a]
            full = bw[int(h * 0.20) : int(h * 0.85), search_left:a]
            top_cols = np.where(top.any(axis=0))[0]
            if len(top_cols) and float(np.mean(full)) < 25:
                left = search_left + int(top_cols.min())
                if a - left >= 6:
                    a = left
        out.append((a, b))
    return out


def fixed_digit_slots(
    strip: np.ndarray,
    *,
    slot_count: int = 4,
    margin: float = 0.03,
) -> list[np.ndarray]:
    h, w = strip.shape[:2]
    if h < 4 or w < slot_count * 4:
        return []
    slot_w = w / float(slot_count)
    gap = slot_w * margin
    patches: list[np.ndarray] = []
    for i in range(slot_count):
        x0 = int(i * slot_w + gap)
        x1 = int((i + 1) * slot_w - gap)
        x0 = max(0, min(x0, w - 1))
        x1 = max(x0 + 1, min(x1, w))
        patches.append(strip[:, x0:x1])
    return patches


def projected_digit_slots(strip: np.ndarray, *, slot_count: int = 4) -> list[np.ndarray]:
    bw = _strip_binary(strip)
    ranges = expand_seven_slots(bw, projected_slot_ranges(bw))
    ranges = _merge_to_slot_count(ranges, slot_count)
    if len(ranges) != slot_count:
        return []
    patches = []
    for a, b in ranges:
        pad = max(1, int(0.08 * (b - a)))
        x0 = max(0, a - pad)
        x1 = min(strip.shape[1], b + pad)
        patches.append(strip[:, x0:x1])
    return patches


def _merge_to_slot_count(
    ranges: list[tuple[int, int]], slot_count: int
) -> list[tuple[int, int]]:
    """Merge nearest adjacent runs until we have exactly slot_count digits.

    Fragmented digits (glare splits a '4' or '8') produce too many runs; merging
    the smallest gaps recovers four glyph boxes more reliably than picking the
    tallest four runs (which drops whole digits).
    """
    if len(ranges) <= slot_count:
        return ranges
    ranges = list(ranges)
    while len(ranges) > slot_count:
        gaps = [
            (ranges[i + 1][0] - ranges[i][1], i) for i in range(len(ranges) - 1)
        ]
        _gap, i = min(gaps)
        merged = (ranges[i][0], ranges[i + 1][1])
        ranges = ranges[:i] + [merged] + ranges[i + 2 :]
    return ranges


def extract_digit_slots(strip: np.ndarray, cfg: NormalizeConfig) -> list[np.ndarray]:
    mode = str(cfg.slot_mode or "projected").lower()
    if mode == "projected":
        patches = projected_digit_slots(strip, slot_count=cfg.slot_count)
        if len(patches) == cfg.slot_count:
            return patches
    return fixed_digit_slots(strip, slot_count=cfg.slot_count, margin=cfg.slot_margin)


def normalize_digit_strip(
    image: np.ndarray,
    quad: list[tuple[float, float]] | list[list[float]],
    cfg: NormalizeConfig | None = None,
) -> tuple[np.ndarray, np.ndarray, list[np.ndarray], str]:
    """Return (screen, digit_strip, slot_patches, screen_method)."""
    cfg = cfg or NormalizeConfig()
    screen, method = prepare_screen(image, quad, cfg)
    strip = crop_digit_strip(screen, cfg)
    if cfg.ink_trim:
        strip = ink_trim_strip(strip, pad=cfg.ink_trim_pad)
    slots = extract_digit_slots(strip, cfg)
    return screen, strip, slots, method


def strip_slot_candidates(
    image: np.ndarray,
    quad: list[tuple[float, float]] | list[list[float]],
    cfg: NormalizeConfig | None = None,
) -> tuple[np.ndarray, str, list[tuple[str, np.ndarray, list[np.ndarray]]]]:
    """Build raw + CLAHE strip variants for decode voting.

    Returns (screen, method, [(label, strip, slots), ...]).
    """
    cfg = cfg or NormalizeConfig()
    screen, method = prepare_screen(image, quad, cfg)
    base = crop_digit_strip(screen, cfg)
    if cfg.ink_trim:
        base = ink_trim_strip(base, pad=cfg.ink_trim_pad)

    variants: list[tuple[str, np.ndarray, list[np.ndarray]]] = []
    raw_slots = extract_digit_slots(base, cfg)
    variants.append(("raw", base, raw_slots))

    enhanced = _enhance_strip(base)
    enh_slots = extract_digit_slots(enhanced, cfg)
    variants.append(("clahe", enhanced, enh_slots))
    return screen, method, variants


def _enhance_strip(strip: np.ndarray) -> np.ndarray:
    if strip.size == 0:
        return strip
    if strip.ndim == 3:
        gray = cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY)
    else:
        gray = strip
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    if strip.ndim == 3:
        return cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)
    return enhanced
