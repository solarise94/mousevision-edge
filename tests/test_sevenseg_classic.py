"""Unit tests for classic seven-seg slot decode (service-local)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "lcd_ocr"))

from sevenseg_classic import compose_weight, decode_seven_seg, read_fixed_slots  # noqa: E402


def _digit_canvas(segments: set[str], *, size: tuple[int, int] = (40, 28)) -> np.ndarray:
    """Paint classic segments on a blank digit (white ink)."""
    h, w = size
    img = np.zeros((h, w), dtype=np.uint8)
    boxes = {
        "a": (0.00, 0.16, 0.18, 0.82),
        "b": (0.14, 0.46, 0.72, 1.00),
        "c": (0.54, 0.86, 0.72, 1.00),
        "d": (0.84, 1.00, 0.18, 0.82),
        "e": (0.54, 0.86, 0.00, 0.28),
        "f": (0.14, 0.46, 0.00, 0.28),
        "g": (0.45, 0.55, 0.28, 0.72),
    }
    for name in segments:
        y0, y1, x0, x1 = boxes[name]
        img[int(y0 * h) : int(y1 * h), int(x0 * w) : int(x1 * w)] = 255
    return img


def test_decode_seven():
    # 7 = a b c
    dig = _digit_canvas({"a", "b", "c"})
    r = decode_seven_seg(dig)
    assert r.char == "7"


def test_decode_bloomed_narrow_one_not_two():
    """w/h≈0.25 with blooming a/e/g must still be 1 (21.60 regression)."""
    h, w = 71, 16
    dig = np.zeros((h, w), dtype=np.uint8)
    dig[:, w - 5 :] = 255  # thick right stem only
    # Bloom spill on left half — would otherwise look like a "2".
    dig[2:10, 2:12] = 180
    dig[32:40, 1:10] = 200
    dig[55:65, 2:12] = 160
    r = decode_seven_seg(dig)
    assert r.char == "1"


def test_compose_four_digits():
    assert compose_weight(["2", "2", "7", "5"]) == 22.75


def test_compose_leading_blank():
    assert compose_weight(["blank", "8", "2", "2"]) == 8.22


def test_read_fixed_slots_zero():
    blanks = [np.zeros((40, 28), dtype=np.uint8) for _ in range(4)]
    r = read_fixed_slots(blanks)
    assert r.status == "zero_display"
    assert r.weight == 0.0
