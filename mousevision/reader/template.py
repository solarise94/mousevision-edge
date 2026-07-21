"""7-segment LCD weight reader (segment occupancy + optional image templates)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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
class LcdBox:
    x: int
    y: int
    w: int
    h: int

    def crop(self, image: np.ndarray) -> np.ndarray:
        return image[self.y : self.y + self.h, self.x : self.x + self.w]


def find_lcd_box(
    image: np.ndarray,
    *,
    hsv_low: tuple[int, int, int] = (90, 40, 80),
    hsv_high: tuple[int, int, int] = (130, 255, 255),
    min_area: int = 8_000,
    min_width: int = 150,
    min_height: int = 40,
    fixed_roi: dict | None = None,
) -> LcdBox | None:
    """Detect blue backlit LCD, or use fixed ROI when provided."""
    if fixed_roi is not None:
        return LcdBox(
            x=int(fixed_roi["x"]),
            y=int(fixed_roi["y"]),
            w=int(fixed_roi["w"]),
            h=int(fixed_roi["h"]),
        )
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array(hsv_low), np.array(hsv_high))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    x, y, w, h = cv2.boundingRect(contour)
    if w * h < min_area or w < min_width or h < min_height:
        return None
    return LcdBox(x=x, y=y, w=w, h=h)


def _digit_area_gray(roi_bgr: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    return gray[:, int(w * 0.20) : int(w * 0.86)]


def _binarize_digits(gray: np.ndarray) -> np.ndarray:
    p92 = float(np.percentile(gray, 92))
    thr = max(200.0, p92 * 0.92)
    _, bw = cv2.threshold(gray, thr, 255, cv2.THRESH_BINARY)
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
    return bw


def _normalize_digit(patch: np.ndarray, size: tuple[int, int] = (28, 40)) -> np.ndarray:
    tw, th = size
    h, w = patch.shape[:2]
    if h < 1 or w < 1:
        return np.zeros((th, tw), dtype=np.uint8)
    scale = min(tw / w, th / h)
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    resized = cv2.resize(patch, (nw, nh), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((th, tw), dtype=np.uint8)
    x0 = (tw - nw) // 2
    y0 = (th - nh) // 2
    canvas[y0 : y0 + nh, x0 : x0 + nw] = resized
    _, canvas = cv2.threshold(canvas, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if np.mean(canvas) > 127:
        canvas = cv2.bitwise_not(canvas)
    return canvas


def _run_height(bw: np.ndarray, a: int, b: int) -> tuple[int, float]:
    ys = np.where(bw[:, a:b].any(axis=1))[0]
    if len(ys) == 0:
        return 0, 0.0
    return int(ys.max() - ys.min() + 1), float(ys.mean()) / bw.shape[0]


def _projection_slots(bw: np.ndarray) -> list[tuple[int, int]]:
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

    # Merge broken "4" fragments:
    # 1) short-height left piece + next digit
    # 2) two adjacent narrow stems
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
            # Broken "4": short upper-left fragment then tall right stem.
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

    typical = max(22, int(h * 0.40))
    slots: list[tuple[int, int]] = []
    for a, b in merged:
        width = b - a
        ph, y_center = _run_height(bw, a, b)
        if ph == 0:
            continue
        if ph < h * 0.28 and y_center > 0.50:
            continue  # decimal point
        if a > w * 0.82 and (ph < h * 0.50 or width < 14):
            continue  # unit / battery junk

        while width > typical * 1.55:
            mid0 = a + int(width * 0.25)
            mid1 = b - int(width * 0.25)
            if mid1 <= mid0 + 2:
                break
            cut = mid0 + int(np.argmin(col[mid0:mid1]))
            if cut - a >= 10 and b - cut >= 10:
                slots.append((a, cut))
                a = cut
                width = b - a
            else:
                break
        slots.append((a, b))
    return slots


def _validate_slot_consistency(
    slots: list[tuple[int, int]],
    bw: np.ndarray,
) -> list[tuple[int, int]]:
    """Filter out slots with inconsistent width or height.

    Real digit slots should have roughly consistent width and height.
    Removes slots that are likely fragments from bad projection splits
    (e.g. an '8' split into two halves, or two digits merged into one).

    Returns the filtered slot list (may be shorter than input).
    """
    if len(slots) <= 1:
        return slots

    h = bw.shape[0]
    widths = [b - a for a, b in slots]
    heights = []
    for a, b in slots:
        ph, _ = _run_height(bw, a, b)
        heights.append(ph)

    med_w = float(np.median(widths))
    med_h = float(np.median(heights))

    if med_w <= 0 or med_h <= 0:
        return slots

    valid: list[tuple[int, int]] = []
    for i, (a, b) in enumerate(slots):
        w = widths[i]
        ph = heights[i]
        # Width consistency: within [0.35, 2.8] of median.
        # Too narrow → fragment; too wide → merged digits.
        if w < med_w * 0.35 or w > med_w * 2.8:
            continue
        # Height consistency: within [0.5, 1.5] of median.
        if ph > 0 and (ph < med_h * 0.5 or ph > med_h * 1.5):
            continue
        valid.append((a, b))

    return valid if valid else slots  # never return empty


def _expand_seven_slots(bw: np.ndarray, slots: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Expand narrow right-stem slots leftward to capture a disconnected '7' top bar."""
    h, _ = bw.shape
    if not slots:
        return slots
    out: list[tuple[int, int]] = []
    for i, (a, b) in enumerate(slots):
        width = b - a
        ph, _ = _run_height(bw, a, b)
        if ph >= h * 0.45 and width < h * 0.22:
            search_left = max(0, a - int(h * 0.40))
            # Don't invade previous slot.
            if i > 0:
                search_left = max(search_left, out[-1][1] + 2 if out else slots[i - 1][1] + 2)
            top = bw[int(h * 0.02) : int(h * 0.20), search_left:a]
            full = bw[int(h * 0.20) : int(h * 0.85), search_left:a]
            top_cols = np.where(top.any(axis=0))[0]
            # Expand only if top bar exists and body region is mostly empty.
            if len(top_cols) and float(np.mean(full)) < 25:
                left = search_left + int(top_cols.min())
                if a - left >= 8:
                    a = left
        out.append((a, b))
    return out


def _trim_patch(bw: np.ndarray, a: int, b: int) -> np.ndarray:
    patch = bw[:, a:b]
    rows = np.where(patch.any(axis=1))[0]
    cols = np.where(patch.any(axis=0))[0]
    if len(rows) == 0 or len(cols) == 0:
        return patch
    return patch[rows[0] : rows[-1] + 1, cols[0] : cols[-1] + 1]


def _is_noise_slot(bw: np.ndarray, a: int, b: int) -> bool:
    """Trailing unit/battery fragments."""
    h, w = bw.shape
    patch = _trim_patch(bw, a, b)
    if patch.size == 0:
        return True
    ph, pw = patch.shape
    # Far-right thin junk.
    if a > w * 0.78 and (pw < 18 or ph < h * 0.45 or float(np.mean(patch)) < 70):
        return True
    # Tiny blobs.
    if ph < h * 0.25 and pw < 16:
        return True
    return False


def _is_dot_slot(bw: np.ndarray, a: int, b: int) -> bool:
    h = bw.shape[0]
    patch = _trim_patch(bw, a, b)
    if patch.size == 0:
        return False
    ph, pw = patch.shape
    ys = np.where(bw[:, a:b] > 0)[0]
    if len(ys) == 0:
        return False
    y_center = float(ys.mean()) / h
    return ph < h * 0.28 and pw < h * 0.30 and y_center > 0.55


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


def decode_seven_seg(digit_bin: np.ndarray) -> tuple[str, float]:
    ys, xs = np.where(digit_bin > 0)
    if len(xs) < 5:
        return "?", 0.0
    digit = digit_bin[int(ys.min()) : int(ys.max()) + 1, int(xs.min()) : int(xs.max()) + 1]
    h, w = digit.shape
    if h < 8 or w < 3:
        return "?", 0.0

    # Narrow glyph → "1" (true ones are ~0.12–0.20 width/height).
    if w / h < 0.22:
        return "1", 0.92

    segments = {
        "a": (0.00, 0.16, 0.18, 0.82),
        "b": (0.14, 0.46, 0.72, 1.00),
        "c": (0.54, 0.86, 0.72, 1.00),
        "d": (0.84, 1.00, 0.18, 0.82),
        "e": (0.54, 0.86, 0.00, 0.28),
        "f": (0.14, 0.46, 0.00, 0.28),
        # Tight middle bar — critical to separate 0 vs 8.
        "g": (0.45, 0.55, 0.28, 0.72),
    }
    means = {name: _region_mean(digit, box) for name, box in segments.items()}
    peak = max(means.values()) if means else 0.0
    if peak < 40:
        return "?", 0.0
    # Relative to brightest segment — robust across exposure.
    thr = max(80.0, peak * 0.45)
    g_thr = max(thr, peak * 0.55)
    on = {name: means[name] >= (g_thr if name == "g" else thr) for name in segments}

    bits = 0
    order = "abcdefg"
    for i, name in enumerate(order):
        if on[name]:
            bits |= 1 << (6 - i)

    char = SEG_MAP.get(bits)
    if char is None:
        best_d, best_c = 8, "?"
        for mask, c in SEG_MAP.items():
            d = bin(bits ^ mask).count("1")
            if d < best_d:
                best_d, best_c = d, c
        if best_d <= 1:
            char = best_c
            conf = 0.65
        else:
            return "?", 0.0
    else:
        conf = 0.55 + 0.45 * float(np.mean([means[n] / 255.0 for n in order if on[n]] or [0.5]))

    return char, float(min(0.99, conf))


def _compose_value(
    chars: list[str],
    *,
    expected_digits: tuple[int, ...] = (3, 4),
) -> str | None:
    """Compose LCD digits. Reject lengths outside expected_digits (avoids 25→2.50)."""
    digits = [c for c in chars if c.isdigit()]
    if len(digits) > 4:
        digits = digits[-4:]
    n = len(digits)
    if n not in expected_digits:
        return None
    if n == 4:
        return f"{digits[0]}{digits[1]}.{digits[2]}{digits[3]}"
    if n == 3:
        return f"{digits[0]}.{digits[1]}{digits[2]}"
    if n == 2:
        # Explicit 2-digit mode only (integer grams); never invent a decimal.
        return f"{digits[0]}{digits[1]}"
    if n == 1:
        return digits[0]
    return None


class TemplateReader:
    def __init__(
        self,
        templates_dir: str | Path | None = None,
        *,
        match_threshold: float = 0.50,
        min_digit_confidence: float = 0.45,
        lcd_detect: dict | None = None,
        weight_roi: dict | None = None,
        expected_digits: tuple[int, ...] | list[int] | None = None,
    ) -> None:
        self.templates_dir = Path(templates_dir) if templates_dir else None
        self.match_threshold = match_threshold
        self.min_digit_confidence = min_digit_confidence
        self.lcd_detect = lcd_detect or {}
        self.weight_roi = weight_roi
        self.expected_digits = tuple(expected_digits) if expected_digits else (3, 4)
        self.templates: dict[str, np.ndarray] = {}
        if self.templates_dir and self.templates_dir.exists():
            self._load_templates()

    def lcd_box(self, image: np.ndarray) -> LcdBox | None:
        mode = str(self.lcd_detect.get("mode", "auto"))
        if mode == "fixed" and self.weight_roi is not None:
            return find_lcd_box(image, fixed_roi=self.weight_roi)
        hsv_low = tuple(self.lcd_detect.get("hsv_low", [90, 40, 80]))
        hsv_high = tuple(self.lcd_detect.get("hsv_high", [130, 255, 255]))
        return find_lcd_box(
            image,
            hsv_low=hsv_low,  # type: ignore[arg-type]
            hsv_high=hsv_high,  # type: ignore[arg-type]
            min_area=int(self.lcd_detect.get("min_area", 8000)),
            min_width=int(self.lcd_detect.get("min_width", 150)),
            min_height=int(self.lcd_detect.get("min_height", 40)),
        )

    def _lcd_box(self, image: np.ndarray) -> LcdBox | None:
        return self.lcd_box(image)

    def _load_templates(self) -> None:
        assert self.templates_dir is not None
        for path in sorted(self.templates_dir.glob("*.png")):
            img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            _, img = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            if np.mean(img) > 127:
                img = cv2.bitwise_not(img)
            self.templates[path.stem] = _normalize_digit(img)

    def _match_image_template(self, patch: np.ndarray) -> tuple[str, float]:
        if not self.templates:
            return "?", 0.0
        norm = _normalize_digit(patch)
        best_key, best_score = "?", -1.0
        for key, tmpl in self.templates.items():
            if key == "dot":
                continue
            score = float(cv2.matchTemplate(norm, tmpl, cv2.TM_CCOEFF_NORMED).max())
            if score > best_score:
                best_score = score
                best_key = key
        return best_key, best_score

    def read_weight(
        self,
        image: np.ndarray,
        *,
        lcd_box: LcdBox | None = None,
    ) -> tuple[float | None, float]:
        box = lcd_box if lcd_box is not None else self._lcd_box(image)
        if box is None:
            return None, 0.0
        gray = _digit_area_gray(box.crop(image))

        # Multi-threshold vote: LCD exposure varies; pick consensus reading.
        votes: dict[float, list[float]] = {}
        p92 = float(np.percentile(gray, 92))
        for scale in (0.88, 0.90, 0.92, 0.94, 0.96, 0.98):
            thr = max(190.0, p92 * scale)
            _, bw = cv2.threshold(gray, thr, 255, cv2.THRESH_BINARY)
            bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
            value, conf, n_digits = self._decode_binary(bw)
            if value is None or n_digits not in (3, 4):
                continue
            key = round(value, 2)
            votes.setdefault(key, []).append(conf)

        if not votes:
            return None, 0.0

        # Prefer values with most votes, then highest mean confidence.
        best_value = None
        best_score = -1.0
        best_conf = 0.0
        for value, confs in votes.items():
            score = len(confs) + float(np.mean(confs))
            if score > best_score:
                best_score = score
                best_value = value
                best_conf = float(np.mean(confs))

        if best_value is None or best_conf < self.match_threshold:
            return None, best_conf
        if best_value < 0 or best_value > 80:
            return None, best_conf
        return best_value, best_conf

    def _decode_binary(self, bw: np.ndarray) -> tuple[float | None, float, int]:
        slots = [s for s in _projection_slots(bw) if not _is_noise_slot(bw, s[0], s[1])]
        slots = _validate_slot_consistency(slots, bw)
        slots = _expand_seven_slots(bw, slots)
        if len(slots) < 2:
            return None, 0.0, 0

        chars: list[str] = []
        scores: list[float] = []
        for a, b in slots:
            if _is_dot_slot(bw, a, b):
                continue
            patch = _trim_patch(bw, a, b)
            seg_char, seg_conf = decode_seven_seg(patch)
            img_char, img_conf = self._match_image_template(patch)
            if seg_char != "?" and seg_conf >= self.min_digit_confidence:
                chars.append(seg_char)
                scores.append(seg_conf)
            elif img_char.isdigit() and img_conf >= self.min_digit_confidence:
                chars.append(img_char)
                scores.append(img_conf)

        text = _compose_value(chars, expected_digits=self.expected_digits)
        if text is None:
            return None, float(np.mean(scores) if scores else 0.0), len(chars)
        try:
            value = float(text)
        except ValueError:
            return None, float(np.mean(scores) if scores else 0.0), len(chars)
        conf = float(np.mean(scores)) if scores else 0.0
        return value, conf, len([c for c in chars if c.isdigit()])

    def debug_overlay(self, image: np.ndarray) -> np.ndarray:
        vis = image.copy()
        box = self._lcd_box(image)
        if box is None:
            return vis
        cv2.rectangle(vis, (box.x, box.y), (box.x + box.w, box.y + box.h), (0, 255, 0), 2)
        weight, conf = self.read_weight(image)
        label = "N/A" if weight is None else f"{weight:.2f}g ({conf:.2f})"
        cv2.putText(
            vis,
            label,
            (box.x, max(30, box.y - 10)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 255, 0),
            2,
            cv2.LINE_AA,
        )
        return vis
