"""Unit tests for LCD locator / normalize helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "lcd_ocr"))

from locator import locate_hsv_quad, validate_hint  # noqa: E402
from normalize import NormalizeConfig, normalize_digit_strip  # noqa: E402


def _blue_lcd_frame(h: int = 400, w: int = 600) -> np.ndarray:
    img = np.zeros((h, w, 3), dtype=np.uint8)
    # Blue-ish LCD in BGR (H~100 in HSV)
    img[250:330, 80:520] = (220, 120, 40)
    return img


def test_locate_hsv_quad_finds_blue_panel():
    img = _blue_lcd_frame()
    loc = locate_hsv_quad(img, min_area=2000, min_width=100, min_height=30)
    assert loc is not None
    assert loc.method == "hsv_bbox"
    assert len(loc.screen_quad) == 4


def test_locate_hsv_quad_can_use_poly_corners():
    img = _blue_lcd_frame()
    loc = locate_hsv_quad(
        img, min_area=2000, min_width=100, min_height=30, prefer_axis_bbox=False
    )
    assert loc is not None
    assert loc.method == "hsv_quad"


def test_validate_hint_accepts_good_quad():
    img = _blue_lcd_frame()
    loc = locate_hsv_quad(img, min_area=2000, min_width=100, min_height=30)
    assert loc is not None
    again = validate_hint(img, [list(p) for p in loc.screen_quad])
    assert again is not None
    assert again.method == "quad_hint"


def test_normalize_produces_four_slots():
    img = _blue_lcd_frame()
    loc = locate_hsv_quad(img, min_area=2000, min_width=100, min_height=30)
    assert loc is not None
    warped, strip, slots, method = normalize_digit_strip(
        img, loc.screen_quad, NormalizeConfig(width=240, height=64)
    )
    assert warped.shape[1] == 240
    assert len(slots) == 4
    assert strip.size > 0
    assert method in {"axis", "warp"}
