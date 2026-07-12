"""Tests for mouse presence detection (mousevision.detect)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from mousevision.detect import detect_mouse_box


@dataclass
class _FakeLCD:
    x: int = 0
    y: int = 400
    w: int = 300
    h: int = 80


def _blank_frame(w: int = 720, h: int = 1280) -> np.ndarray:
    """A bright empty frame (no mouse)."""
    return np.full((h, w, 3), 200, dtype=np.uint8)


def _frame_with_mouse(w: int = 720, h: int = 1280) -> np.ndarray:
    """A frame with a dark blob (simulated mouse) in the platform area."""
    img = np.full((h, w, 3), 200, dtype=np.uint8)
    # Draw a dark blob where a mouse would be (above LCD, center-ish)
    img[100:350, 200:500, :] = 30  # dark region = mouse
    return img


def test_detect_mouse_empty_frame_returns_none():
    """A blank bright frame should not detect a mouse."""
    frame = _blank_frame()
    assert detect_mouse_box(frame, _FakeLCD()) is None


def test_detect_mouse_with_dark_blob_returns_bbox():
    """A frame with a dark blob should detect a mouse bounding box."""
    frame = _frame_with_mouse()
    result = detect_mouse_box(frame, _FakeLCD())
    assert result is not None
    x, y, bw, bh = result
    assert bw > 0 and bh > 0
    # The blob was placed at x=200..500, y=100..350
    assert 150 <= x <= 250


def test_detect_mouse_no_lcd_uses_default_region():
    """Without LCD info, detection should still work on upper region."""
    frame = _frame_with_mouse()
    result = detect_mouse_box(frame, None)
    assert result is not None


def test_detect_mouse_small_blob_below_threshold():
    """A tiny dark spot below min_area should not count as a mouse."""
    frame = _blank_frame()
    frame[100:110, 100:110, :] = 30  # 10x10 = 100px, below min_area 800
    assert detect_mouse_box(frame, _FakeLCD()) is None
