"""Mouse presence detection for photo selection and UI annotation.

This module provides a simple, dependency-light mouse detector used both by
the photo-selection pipeline (``SessionDriver``) and the live UI preview.
It operates on grayscale thresholding + morphology + largest-contour area,
which is sufficient to answer "is there a mouse-sized dark blob on the scale
platform?" without a trained model.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np


def detect_mouse_box(
    image: np.ndarray,
    lcd: Any | None,
    *,
    gray_thr: int = 70,
    min_area: int = 800,
    x_ratio: tuple[float, float] = (0.12, 0.88),
) -> tuple[int, int, int, int] | None:
    """Detect a mouse on the scale platform.

    Looks for a dark blob in the region between the top of the frame and the
    LCD box. Returns a bounding box ``(x, y, w, h)`` or ``None`` if no
    mouse-sized contour is found.

    Args:
        image: BGR frame.
        lcd: LCD box (with ``.y`` attribute) or ``None``. When provided, the
            search region is bounded above the LCD; otherwise a default
            upper-half crop is used.
        gray_thr: grayscale threshold (mouse is darker than background).
        min_area: minimum contour area in pixels to count as a mouse.
        x_ratio: horizontal crop ratio to exclude frame edges.
    """
    h, w = image.shape[:2]
    y1 = 40
    y2 = lcd.y - 10 if lcd is not None else int(h * 0.55)
    x1, x2 = int(w * x_ratio[0]), int(w * x_ratio[1])
    if y2 <= y1 + 20:
        return None
    roi = image[y1:y2, x1:x2]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, gray_thr, 255, cv2.THRESH_BINARY_INV)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    if cv2.contourArea(contour) < min_area:
        return None
    x, y, bw, bh = cv2.boundingRect(contour)
    return x1 + x, y1 + y, bw, bh
