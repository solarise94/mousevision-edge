"""Classic 7-segment decoding on fixed digit slots."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

# Classic 7-seg bitmasks: bits = a b c d e f g (MSB=a)
SEG_MAP = {
    0b1111110: "0",
    0b0110000: "1",
    0b1101101: "2",
    0b1111001: "3",
    0b0110011: "4",
    0b1011011: "5",
    0b1011111: "6",
    0b1110000: "7",
    0b1111111: "8",
    0b1111011: "9",
}


@dataclass
class SlotDecode:
    char: str  # blank | 0-9 | invalid
    confidence: float
    top_bar: float = 0.0  # occupancy of segment a (0..1)


def _to_binary(patch_bgr_or_gray: np.ndarray, *, thr_scale: float = 0.90) -> np.ndarray:
    if patch_bgr_or_gray.ndim == 3:
        gray = cv2.cvtColor(patch_bgr_or_gray, cv2.COLOR_BGR2GRAY)
    else:
        gray = patch_bgr_or_gray
    if gray.size == 0:
        return np.zeros((1, 1), dtype=np.uint8)
    p92 = float(np.percentile(gray, 92))
    thr = max(160.0, p92 * thr_scale)
    _, bw = cv2.threshold(gray, thr, 255, cv2.THRESH_BINARY)
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
    # Digits are bright on blue LCD → keep white-on-black.
    if float(np.mean(bw)) > 127:
        ink = float(np.count_nonzero(bw)) / float(bw.size)
        if ink > 0.55:
            bw = cv2.bitwise_not(bw)
    return bw


def _trim(bw: np.ndarray) -> np.ndarray:
    ys, xs = np.where(bw > 0)
    if len(xs) < 3:
        return bw
    return bw[int(ys.min()) : int(ys.max()) + 1, int(xs.min()) : int(xs.max()) + 1]


def _region_mean(digit: np.ndarray, box: tuple[float, float, float, float]) -> float:
    h, w = digit.shape
    y0, y1, x0, x1 = box
    y0i, y1i = int(y0 * h), int(y1 * h)
    x0i, x1i = int(x0 * w), int(x1 * w)
    y0i, x0i = max(0, y0i), max(0, x0i)
    y1i, x1i = min(h, max(y0i + 1, y1i)), min(w, max(x0i + 1, x1i))
    region = digit[y0i:y1i, x0i:x1i]
    if region.size == 0:
        return 0.0
    return float(np.mean(region))


def _top_bar_score(digit: np.ndarray) -> float:
    """Horizontal span × intensity for segment 'a' (7 vs 1)."""
    h, w = digit.shape
    if h < 6 or w < 3:
        return 0.0
    y0, y1 = int(h * 0.02), max(int(h * 0.18), 2)
    x0, x1 = int(w * 0.10), int(w * 0.90)
    band = digit[y0:y1, x0:x1]
    if band.size == 0:
        return 0.0
    col_on = (band > 0).any(axis=0)
    span = float(np.mean(col_on)) if col_on.size else 0.0
    intensity = float(np.mean(band)) / 255.0
    return span * intensity


def decode_seven_seg(digit_bin: np.ndarray) -> SlotDecode:
    ys, xs = np.where(digit_bin > 0)
    if len(xs) < 5:
        return SlotDecode("blank", 0.85, 0.0)

    digit = digit_bin[int(ys.min()) : int(ys.max()) + 1, int(xs.min()) : int(xs.max()) + 1]
    h, w = digit.shape
    if h < 8 or w < 2:
        return SlotDecode("blank", 0.70, 0.0)

    top_bar = _top_bar_score(digit)

    # Narrow glyph → always "1". Blooming on a ~16px slot otherwise
    # hallucinates a/d/e/g and votes "2" (21.60 → 22.60).
    if w / h < 0.32:
        return SlotDecode("1", 0.92, top_bar)

    segments = {
        "a": (0.00, 0.16, 0.18, 0.82),
        "b": (0.14, 0.46, 0.72, 1.00),
        "c": (0.54, 0.86, 0.72, 1.00),
        "d": (0.84, 1.00, 0.18, 0.82),
        "e": (0.54, 0.86, 0.00, 0.28),
        "f": (0.14, 0.46, 0.00, 0.28),
        "g": (0.45, 0.55, 0.28, 0.72),
    }
    means = {name: _region_mean(digit, box) for name, box in segments.items()}
    peak = max(means.values()) if means else 0.0
    if peak < 40:
        return SlotDecode("blank", 0.60, top_bar)

    thr = max(55.0, peak * 0.38)
    g_thr = max(thr, peak * 0.50)
    on = {name: means[name] >= (g_thr if name == "g" else thr) for name in segments}

    if top_bar >= 0.40:
        on["a"] = True

    bits = 0
    order = "abcdefg"
    for i, name in enumerate(order):
        if on[name]:
            bits |= 1 << (6 - i)

    char = SEG_MAP.get(bits)
    conf = 0.0
    if char is None:
        candidates: list[tuple[int, int, int, str]] = []
        for mask, c in SEG_MAP.items():
            d = bin(bits ^ mask).count("1")
            overlap = bin(bits & mask).count("1")
            extras = bin(bits & ~mask).count("1")
            candidates.append((d, -overlap, extras, c))
        candidates.sort()
        best_d, _neg_ov, _extras, best_c = candidates[0]
        # Ambiguous 2 vs 3 (both often d=1): prefer by c vs e strength.
        if best_d <= 1:
            near = [c for d, _, _, c in candidates if d == best_d]
            if set(near) >= {"2", "3"}:
                best_c = "3" if means["c"] >= means["e"] else "2"
            char = best_c
            conf = 0.65 if best_d == 0 else 0.58
        else:
            return SlotDecode("invalid", 0.0, top_bar)
    else:
        conf = 0.55 + 0.45 * float(
            np.mean([means[n] / 255.0 for n in order if on[n]] or [0.5])
        )

    if char == "1" and top_bar >= 0.40:
        char = "7"
        conf = min(0.95, conf + 0.10)
    elif char == "7" and top_bar < 0.18 and w / h < 0.35:
        char = "1"
        conf = min(0.90, conf)

    # 4 vs 9: glare on a/d often turns a true 4 into mask-9. Prefer 4 when
    # bottom-left (e) is off and a/d are not both solidly on.
    if char == "9":
        a_on = means["a"] >= thr * 0.90
        d_on = means["d"] >= thr * 0.90
        e_on = means["e"] >= thr * 0.90
        if not e_on and not (a_on and d_on):
            char = "4"
            conf = min(0.93, conf + 0.05)

    return SlotDecode(char, float(min(0.99, conf)), top_bar)


def decode_slot_patch(patch: np.ndarray) -> SlotDecode:
    """Decode one slot with multi-threshold vote (glare-robust)."""
    votes: dict[str, list[float]] = {}
    for scale in (0.82, 0.86, 0.90, 0.94, 0.98):
        bw = _to_binary(patch, thr_scale=scale)
        ink_ratio = float(np.count_nonzero(bw)) / float(max(1, bw.size))
        if ink_ratio < 0.008:
            votes.setdefault("blank", []).append(0.90)
            continue
        d = decode_seven_seg(_trim(bw))
        if d.char == "invalid":
            continue
        votes.setdefault(d.char, []).append(float(d.confidence))

    if not votes:
        return SlotDecode("invalid", 0.0, 0.0)

    def rank(ch: str) -> tuple[int, int, float]:
        confs = votes[ch]
        nonblank = 1 if ch.isdigit() else 0
        return (nonblank, len(confs), float(np.mean(confs)))

    best = max(votes.keys(), key=rank)
    conf = float(np.mean(votes[best]))
    digit_candidates = [c for c in votes if c.isdigit()]
    if best == "blank" and digit_candidates:
        dig = max(digit_candidates, key=rank)
        if len(votes[dig]) >= max(1, len(votes["blank"]) - 1):
            best, conf = dig, float(np.mean(votes[dig]))

    top_bar = 0.0
    if best in {"1", "7"}:
        top_bar = _top_bar_score(_trim(_to_binary(patch, thr_scale=0.90)))
    return SlotDecode(best, conf, top_bar)


def compose_weight(
    chars: list[str],
    *,
    decimal_places: int = 2,
) -> float | None:
    """Compose XX.XX or X.XX from 4 slot chars (blank allowed as leading)."""
    if any(c == "invalid" for c in chars):
        return None
    digits = [c for c in chars if c.isdigit()]
    if not digits:
        if all(c in {"blank", "0"} for c in chars):
            return 0.0
        return None
    trimmed = list(chars)
    while trimmed and trimmed[0] == "blank":
        trimmed.pop(0)
    while trimmed and trimmed[-1] == "blank":
        trimmed.pop()
    nums = [c for c in trimmed if c.isdigit()]
    if len(nums) == 0:
        return 0.0
    if len(nums) > 4:
        nums = nums[-4:]
    n = len(nums)
    if n == 4:
        return float(f"{nums[0]}{nums[1]}.{nums[2]}{nums[3]}")
    if n == 3:
        return float(f"{nums[0]}.{nums[1]}{nums[2]}")
    if n == 2 and decimal_places == 2:
        return float(f"{nums[0]}.{nums[1]}0")
    if n == 1:
        return float(nums[0])
    return None


@dataclass
class ClassicRead:
    weight: float | None
    digits: list[str]
    digit_confidences: list[float]
    quality: float
    status: str  # readable | zero_display | unreadable


def read_fixed_slots(slot_patches: list[np.ndarray]) -> ClassicRead:
    if len(slot_patches) != 4:
        return ClassicRead(None, [], [], 0.0, "unreadable")

    decoded = [decode_slot_patch(p) for p in slot_patches]
    chars = [d.char for d in decoded]
    confs = [d.confidence for d in decoded]

    if any(c == "invalid" for c in chars):
        return ClassicRead(None, chars, confs, float(np.mean(confs) if confs else 0.0), "unreadable")

    quality = float(np.mean(confs)) if confs else 0.0
    for d in decoded:
        if d.char == "7" and d.top_bar < 0.25:
            quality *= 0.85
        if d.char == "1" and d.top_bar >= 0.40:
            quality *= 0.85

    weight = compose_weight(chars)
    if weight is None:
        return ClassicRead(None, chars, confs, quality, "unreadable")

    # Empty platter often decodes as 0.01 (trailing noise "1"). Treat near-zero
    # with mostly-zero slots as zero_display so leave / fusion see a clean 0.
    zeroish = sum(1 for c in chars if c in {"blank", "0"}) >= 3
    if weight <= 0.05 or (weight < 0.10 and zeroish):
        return ClassicRead(0.0, chars, confs, quality, "zero_display")

    return ClassicRead(round(weight, 2), chars, confs, quality, "readable")
