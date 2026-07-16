"""Mouse presence detection for photo selection and UI annotation.

Simple, dependency-light detector: grayscale/Otsu thresholding + morphology
+ constrained contour selection. Answers "is there a mouse-sized dark blob
on the scale pan?" without a trained model.
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
    max_area: int | None = None,
    x_ratio: tuple[float, float] = (0.12, 0.88),
    aspect_ratio: tuple[float, float] = (0.3, 2.0),
    pan_roi: dict[str, int] | tuple[int, int, int, int] | None = None,
    use_otsu: bool = True,
    dark_p05: float | None = None,
    dark_ratio: float | None = None,
) -> tuple[int, int, int, int] | None:
    """Detect a mouse on the scale pan.

    Returns bounding box ``(x, y, w, h)`` or ``None``.

    Args:
        image: BGR frame.
        lcd: LCD box (with ``.y``) or None. Used only when pan_roi is absent.
        gray_thr: fixed threshold when Otsu is disabled (darker = mouse).
        min_area / max_area: contour area bounds in pixels.
        x_ratio: horizontal crop when pan_roi is absent.
        aspect_ratio: allowed width/height range for a mouse blob.
        pan_roi: absolute pan pixel region ``{x,y,w,h}`` or ``(x,y,w,h)``.
            Must be the scale pan — **not** ``weight_roi`` (LCD screen).
        use_otsu: prefer Otsu adaptive threshold over fixed gray_thr.
        dark_p05: when set, a candidate blob is accepted only if the 5th
            percentile gray of its pixels is ``<= dark_p05``. Real mice are
            near-black (p05 ~0-27); pan stains / reflections stay lighter.
        dark_ratio: when set, a candidate blob is accepted only if its mean
            gray is ``<= dark_ratio * median(roi gray)``. Rejects large faint
            stain blobs that merely sit below the Otsu split.
    """
    h, w = image.shape[:2]
    if pan_roi is not None:
        if isinstance(pan_roi, dict):
            x1 = int(pan_roi.get("x", 0))
            y1 = int(pan_roi.get("y", 0))
            rw = int(pan_roi.get("w", w))
            rh = int(pan_roi.get("h", h))
        else:
            x1, y1, rw, rh = (int(v) for v in pan_roi)
        x1 = max(0, min(x1, w - 1))
        y1 = max(0, min(y1, h - 1))
        x2 = max(x1 + 1, min(x1 + rw, w))
        y2 = max(y1 + 1, min(y1 + rh, h))
    else:
        y1 = 40
        y2 = lcd.y - 10 if lcd is not None else int(h * 0.55)
        x1, x2 = int(w * x_ratio[0]), int(w * x_ratio[1])
    if y2 <= y1 + 20 or x2 <= x1 + 20:
        return None

    roi = image[y1:y2, x1:x2]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    roi_median = float(np.median(gray)) if dark_ratio is not None else 0.0
    if use_otsu:
        # BINARY_INV: dark mouse on lighter pan becomes white foreground.
        _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    else:
        _, mask = cv2.threshold(gray, gray_thr, 255, cv2.THRESH_BINARY_INV)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    ar_lo, ar_hi = float(aspect_ratio[0]), float(aspect_ratio[1])
    candidates: list[tuple[float, tuple[int, int, int, int]]] = []
    for contour in contours:
        area = float(cv2.contourArea(contour))
        if area < float(min_area):
            continue
        if max_area is not None and area > float(max_area):
            continue
        x, y, bw, bh = cv2.boundingRect(contour)
        if bh <= 0:
            continue
        ar = float(bw) / float(bh)
        if ar < ar_lo or ar > ar_hi:
            continue
        if dark_p05 is not None or dark_ratio is not None:
            blob_mask = np.zeros_like(mask)
            cv2.drawContours(blob_mask, [contour], -1, 255, -1)
            pixels = gray[blob_mask > 0]
            if pixels.size == 0:
                continue
            if dark_p05 is not None and float(np.percentile(pixels, 5)) > float(dark_p05):
                continue
            if dark_ratio is not None and float(pixels.mean()) > float(dark_ratio) * roi_median:
                continue
        candidates.append((area, (x1 + x, y1 + y, bw, bh)))

    if not candidates:
        return None
    # Prefer largest legal candidate (not unfiltered max contour).
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]
