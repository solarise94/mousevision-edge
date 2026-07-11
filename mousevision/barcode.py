"""Barcode / QR helpers.

PoC: CLI/UI injects box_id. Android phase wires ML Kit / ZXing.
Mac optional: install `pyzbar` + zbar for real decode from frames.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass
class BarcodeHit:
    text: str
    format: str
    bbox: tuple[int, int, int, int] | None = None


class BarcodeReader:
    """Best-effort QR/barcode reader with graceful fallback."""

    def __init__(self) -> None:
        self._backend = "stub"
        self._decode_fn = None
        try:
            from pyzbar.pyzbar import decode as zbar_decode  # type: ignore

            self._decode_fn = zbar_decode
            self._backend = "pyzbar"
        except Exception:
            self._backend = "stub"

    @property
    def backend(self) -> str:
        return self._backend

    def read(self, image_bgr: np.ndarray) -> BarcodeHit | None:
        if self._decode_fn is None:
            return None
        try:
            import cv2

            gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
            results = self._decode_fn(gray)
            if not results:
                return None
            r0 = results[0]
            rect = getattr(r0, "rect", None)
            bbox = None
            if rect is not None:
                bbox = (int(rect.left), int(rect.top), int(rect.width), int(rect.height))
            text = r0.data.decode("utf-8", errors="ignore")
            fmt = str(getattr(r0, "type", "QR"))
            if not text:
                return None
            return BarcodeHit(text=text, format=fmt, bbox=bbox)
        except Exception:
            return None

    def read_or_default(self, image_bgr: np.ndarray, default_box_id: str) -> tuple[str, dict[str, Any]]:
        hit = self.read(image_bgr)
        if hit is None:
            return default_box_id, {"qr_ok": False, "backend": self.backend, "source": "injected"}
        return hit.text, {
            "qr_ok": True,
            "backend": self.backend,
            "source": "camera",
            "format": hit.format,
            "bbox": hit.bbox,
        }
