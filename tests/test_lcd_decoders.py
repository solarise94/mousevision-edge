"""Decoder adapter + classic_v2 1/7 multi-evidence tests."""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "lcd_ocr"))

from decoders import get_decoder  # noqa: E402
from engine import LcdOcrEngine  # noqa: E402
from profile import load_scale_profile  # noqa: E402
from decoders.segodec_adapter import decode_slot_segodec  # noqa: E402
from quality import assess_strip_quality, detect_glare  # noqa: E402
from sevenseg_classic import decode_seven_seg  # noqa: E402


def _digit_canvas(segments: set[str], *, size: tuple[int, int] = (40, 28)) -> np.ndarray:
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


def test_factory_classic_v2():
    d = get_decoder("classic_v2")
    assert d.name == "classic_v2"


def test_narrow_seven_not_forced_to_one():
    """Narrow glyph with continuous top bar must prefer 7 (RefVideo regress)."""
    dig = _digit_canvas({"a", "b", "c"}, size=(60, 16))
    r = decode_seven_seg(dig)
    assert r.char == "7"



def test_segodec_decode_seven():
    dig = _digit_canvas({"a", "b", "c"})
    # Paint as BGR-ish patch by stacking.
    patch = np.stack([dig, dig, dig], axis=-1)
    ch, _cf, _ev = decode_slot_segodec(patch)
    assert ch == "7"


def test_quality_gate_rejects_empty_strip():
    strip = np.zeros((64, 240, 3), dtype=np.uint8)
    slots = [np.zeros((64, 40, 3), dtype=np.uint8) for _ in range(4)]
    q = assess_strip_quality(strip, slots)
    assert q.status == "zero_display"


def test_classic_v2_blank_slots_zero():
    d = get_decoder("classic_v2")
    strip = np.zeros((64, 240, 3), dtype=np.uint8)
    slots = [np.zeros((64, 40, 3), dtype=np.uint8) for _ in range(4)]
    r = d.read(strip, slots)
    assert r.status == "zero_display"
    assert r.weight == 0.0


def test_three_glyph_zero_display_is_not_split_into_low_weight():
    root = Path(__file__).resolve().parents[1]
    image = cv2.imread(
        str(
            root
            / "tests/fixtures/lcd_ocr/refvideo/hard/reject_transition_f00016.png"
        )
    )
    assert image is not None
    result = LcdOcrEngine(scale_profile=load_scale_profile()).read(image)
    assert result.status == "zero_display"
    assert result.weight == 0.0


def test_sauvola_binarize_basic():
    """Sauvola should produce a binary image from a synthetic LCD patch."""
    from binarize import to_binary, to_binary_sauvola

    # Synthetic: dark background with bright digit-like strokes
    patch = np.full((64, 128), 40, dtype=np.uint8)
    patch[10:50, 20:30] = 220  # vertical stroke
    patch[10:20, 20:60] = 220  # top bar

    bw_pct = to_binary(patch, method="percentile")
    bw_sau = to_binary(patch, method="sauvola")

    assert bw_pct.shape == patch.shape
    assert bw_sau.shape == patch.shape
    # Both should detect the bright strokes
    assert np.count_nonzero(bw_pct) > 0
    assert np.count_nonzero(bw_sau) > 0


def test_sauvola_robust_to_glare():
    """Sauvola should handle a glare spot better than global percentile."""
    from binarize import to_binary_sauvola

    # Dark LCD with a bright glare spot in corner
    patch = np.full((64, 128), 40, dtype=np.uint8)
    patch[10:50, 20:30] = 200  # digit stroke
    patch[0:10, 0:10] = 255    # glare spot

    bw_sau = to_binary_sauvola(patch)
    # The digit stroke should still be detected
    assert np.count_nonzero(bw_sau[10:50, 20:30]) > 0


def test_glare_detection_on_synthetic_screen():
    """Detect glare on a synthetic LCD screen with a bright spot."""
    # Dark LCD screen with a small bright glare spot
    screen = np.full((128, 480, 3), 60, dtype=np.uint8)
    # Add some digit-like content
    screen[30:90, 100:120] = (200, 200, 200)
    # Add glare spot (small, bright) — 25x25 = 625 px (~1% of 128x480) so it
    # clears the min_glare_frac=0.005 floor while staying well under the
    # max_glare_frac=0.15 specular ceiling.
    screen[10:35, 10:35] = (255, 255, 255)

    report = detect_glare(screen, digit_roi=(0.20, 0.08, 0.66, 0.84))
    assert report.has_glare is True
    assert report.glare_fraction > 0.005
    assert report.glare_fraction < 0.15


def test_glare_detection_no_glare():
    """Uniform dark screen should not trigger glare."""
    screen = np.full((128, 480, 3), 60, dtype=np.uint8)
    report = detect_glare(screen)
    assert report.has_glare is False


def test_glare_detection_overexposure_not_glare():
    """A mostly-bright screen is overexposure, not specular glare."""
    screen = np.full((128, 480, 3), 220, dtype=np.uint8)
    report = detect_glare(screen)
    assert report.has_glare is False  # frac > max_glare_frac or no bright region
