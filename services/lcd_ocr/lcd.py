"""LCD localization and digit-area preprocessing for scale OCR."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class LcdBox:
    x: int
    y: int
    w: int
    h: int

    def crop(self, image: np.ndarray) -> np.ndarray:
        return image[self.y : self.y + self.h, self.x : self.x + self.w]

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @classmethod
    def from_dict(cls, d: dict) -> "LcdBox":
        return cls(x=int(d["x"]), y=int(d["y"]), w=int(d["w"]), h=int(d["h"]))


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


def _digit_band(roi_bgr: np.ndarray) -> np.ndarray:
    h, w = roi_bgr.shape[:2]
    # Same digit band ratio as TemplateReader (_digit_area_gray).
    x0, x1 = int(w * 0.18), int(w * 0.88)
    y0, y1 = int(h * 0.15), int(h * 0.90)
    band = roi_bgr[y0:y1, x0:x1]
    return band if band.size else roi_bgr


def _upscale(img: np.ndarray, scale: float) -> np.ndarray:
    if scale == 1.0:
        return img
    return cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)


def prepare_digit_variants(roi_bgr: np.ndarray, *, scale: float = 3.0) -> list[np.ndarray]:
    """Build several LCD crops; caller OCRs each and votes."""
    band = _digit_band(roi_bgr)
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)

    p92 = float(np.percentile(gray, 92))
    thr = max(180.0, p92 * 0.90)
    _, bw = cv2.threshold(gray, thr, 255, cv2.THRESH_BINARY)
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
    inv = cv2.bitwise_not(bw)

    variants = [
        _upscale(cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR), scale),
        _upscale(band, scale),
        _upscale(cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR), scale),
        _upscale(cv2.cvtColor(inv, cv2.COLOR_GRAY2BGR), scale),
    ]
    return variants


def prepare_digit_roi(roi_bgr: np.ndarray, *, scale: float = 3.0) -> np.ndarray:
    """Primary digit-area crop (CLAHE) for single-shot OCR / debug."""
    return prepare_digit_variants(roi_bgr, scale=scale)[0]
