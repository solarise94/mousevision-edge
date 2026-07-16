"""SegoDec-style fixed segment probe + fuzzy Hamming decode.

Inspired by scottmudge/SegoDec's segment test-point idea; not a source copy.
"""

from __future__ import annotations

from typing import Any

import cv2
from binarize import to_binary as _shared_to_binary
import numpy as np

from quality import assess_strip_quality
from sevenseg_classic import compose_weight

from .base import DecoderResult

# Classic masks: bits a b c d e f g
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

# Relative probe boxes (y0,y1,x0,x1) for segments a..g
SEGMENT_BOXES = {
    "a": (0.04, 0.16, 0.22, 0.78),
    "b": (0.16, 0.46, 0.72, 0.96),
    "c": (0.54, 0.84, 0.72, 0.96),
    "d": (0.84, 0.96, 0.22, 0.78),
    "e": (0.54, 0.84, 0.04, 0.28),
    "f": (0.16, 0.46, 0.04, 0.28),
    "g": (0.46, 0.56, 0.28, 0.72),
}


def _to_binary(patch_bgr_or_gray, *, thr_scale: float = 0.9):
    return _shared_to_binary(patch_bgr_or_gray, thr_scale=thr_scale, thr_floor=155.0)

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


def _top_bar_span(digit: np.ndarray) -> float:
    h, w = digit.shape
    if h < 6 or w < 3:
        return 0.0
    y0, y1 = int(h * 0.02), max(int(h * 0.18), 2)
    x0, x1 = int(w * 0.10), int(w * 0.90)
    band = digit[y0:y1, x0:x1]
    if band.size == 0:
        return 0.0
    col_on = (band > 0).any(axis=0)
    return float(np.mean(col_on)) if col_on.size else 0.0


def decode_slot_segodec(patch: np.ndarray) -> tuple[str, float, dict[str, Any]]:
    bw = _to_binary(patch)
    ys, xs = np.where(bw > 0)
    if len(xs) < 5:
        return "blank", 0.85, {"bits": 0, "top_bar": 0.0}

    digit = bw[int(ys.min()) : int(ys.max()) + 1, int(xs.min()) : int(xs.max()) + 1]
    h, w = digit.shape
    if h < 8 or w < 2:
        return "blank", 0.70, {"bits": 0, "top_bar": 0.0}

    top_bar = _top_bar_span(digit)
    means = {name: _region_mean(digit, box) for name, box in SEGMENT_BOXES.items()}
    peak = max(means.values()) if means else 0.0
    if peak < 40:
        return "blank", 0.60, {"bits": 0, "top_bar": top_bar, "means": means}

    thr = max(55.0, peak * 0.38)
    g_thr = max(thr, peak * 0.50)
    on = {name: means[name] >= (g_thr if name == "g" else thr) for name in SEGMENT_BOXES}
    if top_bar >= 0.40:
        on["a"] = True

    bits = 0
    order = "abcdefg"
    for i, name in enumerate(order):
        if on[name]:
            bits |= 1 << (6 - i)

    candidates: list[tuple[int, str]] = []
    for mask, ch in SEG_MAP.items():
        candidates.append((bin(bits ^ mask).count("1"), ch))
    candidates.sort()
    best_d, best_c = candidates[0]
    near = [c for d, c in candidates if d == best_d]

    aspect = float(w) / float(h)
    evidence = {
        "bits": bits,
        "hamming": best_d,
        "top_bar": round(top_bar, 3),
        "aspect": round(aspect, 3),
        "means": {k: round(v, 1) for k, v in means.items()},
    }

    # Multi-evidence 1/7 — width alone never decides.
    if set(near) <= {"1", "7"} or best_c in {"1", "7"}:
        right_on = means["b"] >= thr * 0.85 and means["c"] >= thr * 0.85
        if top_bar >= 0.35:
            best_c = "7"
            conf = 0.88 if top_bar >= 0.45 else 0.78
        elif aspect < 0.32 and right_on and top_bar < 0.22:
            best_c = "1"
            conf = 0.90
        elif best_d <= 1:
            best_c = "7" if top_bar >= 0.28 else "1"
            conf = 0.72 if best_d == 0 else 0.62
        else:
            return "invalid", 0.0, evidence
        return best_c, conf, evidence

    if best_d > 2:
        return "invalid", 0.0, evidence

    if set(near) >= {"2", "3"}:
        best_c = "3" if means["c"] >= means["e"] else "2"

    if best_c == "9":
        a_on = means["a"] >= thr * 0.90
        d_on = means["d"] >= thr * 0.90
        e_on = means["e"] >= thr * 0.90
        if not e_on and not (a_on and d_on):
            best_c = "4"

    conf = 0.55 + 0.40 * (1.0 - best_d / 3.0)
    return best_c, float(min(0.98, conf)), evidence


class SegoDecAdapter:
    name = "segodec"

    def read(self, normalized_strip: Any, slot_patches: list[Any]) -> DecoderResult:
        if len(slot_patches) != 4:
            return DecoderResult([], [], None, "unreadable", 0.0, {"error": "need_4_slots"})

        q = assess_strip_quality(normalized_strip, slot_patches)
        if q.status == "zero_display":
            return DecoderResult(
                ["0", "0", "0", "0"],
                [0.9] * 4,
                0.0,
                "zero_display",
                0.85,
                {"quality_gate": q.reason, **q.evidence},
            )
        if q.status in {"transition", "unreadable"}:
            return DecoderResult(
                ["invalid"] * 4,
                [0.0] * 4,
                None,
                q.status,
                0.0,
                {"quality_gate": q.reason, **q.evidence},
            )

        chars: list[str] = []
        confs: list[float] = []
        slot_ev: list[dict[str, Any]] = []
        for patch in slot_patches:
            ch, cf, ev = decode_slot_segodec(patch)
            chars.append(ch)
            confs.append(cf)
            slot_ev.append(ev)

        evidence = {"quality_gate": "ok", "slots": slot_ev, **q.evidence}
        if any(c == "invalid" for c in chars):
            return DecoderResult(chars, confs, None, "unreadable", float(np.mean(confs)), evidence)

        if chars == ["1", "1", "1", "1"]:
            weak = sum(1 for ev in slot_ev if float(ev.get("top_bar", 0)) < 0.22)
            if weak >= 3 or q.ink_ratio < 0.09:
                return DecoderResult(
                    chars,
                    confs,
                    None,
                    "transition",
                    0.2,
                    {**evidence, "quality_gate": "all_ones_weak"},
                )

        quality = float(np.mean(confs))
        weight = compose_weight(chars)
        if weight is None:
            return DecoderResult(chars, confs, None, "unreadable", quality, evidence)

        zeroish = sum(1 for c in chars if c in {"blank", "0"}) >= 3
        if weight <= 0.05 or (weight < 0.10 and zeroish):
            return DecoderResult(chars, confs, 0.0, "zero_display", quality, evidence)

        return DecoderResult(chars, confs, round(float(weight), 2), "readable", quality, evidence)
