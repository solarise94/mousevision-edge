"""Unit tests for classic seven-seg slot decode (service-local)."""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "lcd_ocr"))

from sevenseg_classic import compose_weight, decode_seven_seg, read_fixed_slots  # noqa: E402
from engine import LcdOcrEngine  # noqa: E402
from profile import load_scale_profile  # noqa: E402


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
    """Real 21.60 fixture: blooming narrow 1 must not become 2/7."""
    root = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "lcd_ocr" / "0001"
    img = cv2.imread(str(root / "frames" / "m2_photo_21.60.jpg"))
    assert img is not None
    eng = LcdOcrEngine(scale_profile=load_scale_profile())
    r = eng.read(img)
    assert r.weight is not None
    assert abs(float(r.weight) - 21.60) <= 0.35
    assert r.digits[1] == "1"


def test_compose_four_digits():
    assert compose_weight(["2", "2", "7", "5"]) == 22.75


def test_compose_leading_blank():
    assert compose_weight(["blank", "8", "2", "2"]) == 8.22


def test_read_fixed_slots_zero():
    blanks = [np.zeros((40, 28), dtype=np.uint8) for _ in range(4)]
    r = read_fixed_slots(blanks)
    assert r.status == "zero_display"
    assert r.weight == 0.0


def test_validate_slot_consistency_removes_fragment():
    """A very narrow fragment next to normal slots should be removed."""
    from mousevision.reader.template import _validate_slot_consistency
    import numpy as np

    # Synthetic binary strip: 3 normal-width slots + 1 tiny fragment
    bw = np.zeros((60, 200), dtype=np.uint8)
    # Normal slots (width ~30)
    bw[10:50, 10:40] = 255
    bw[10:50, 50:80] = 255
    bw[10:50, 90:120] = 255
    # Tiny fragment (width 5)
    bw[10:50, 130:135] = 255

    slots = [(10, 40), (50, 80), (90, 120), (130, 135)]
    result = _validate_slot_consistency(slots, bw)
    # Fragment should be removed
    assert len(result) == 3
    assert (130, 135) not in result


def test_validate_slot_consistency_keeps_uniform():
    """Uniform slots should all be kept."""
    from mousevision.reader.template import _validate_slot_consistency
    import numpy as np

    bw = np.zeros((60, 200), dtype=np.uint8)
    bw[10:50, 10:40] = 255
    bw[10:50, 50:80] = 255
    bw[10:50, 90:120] = 255
    bw[10:50, 130:160] = 255

    slots = [(10, 40), (50, 80), (90, 120), (130, 160)]
    result = _validate_slot_consistency(slots, bw)
    assert len(result) == 4
