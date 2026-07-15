"""OCR reader — thin local wrapper; production uses HttpOcrReader + lcd-ocr service."""

from __future__ import annotations

import numpy as np

from mousevision.reader.http_ocr import HttpOcrReader


class OCRReader:
    """Backward-compatible name; delegates to HttpOcrReader when url is set."""

    def __init__(self, base_url: str | None = None, **kwargs) -> None:
        if not base_url:
            raise NotImplementedError(
                "OCRReader requires base_url of the lcd-ocr service. "
                "Use TemplateReader for offline PoC, or HttpOcrReader(base_url=...)."
            )
        self._inner = HttpOcrReader(base_url, **kwargs)

    def read_weight(self, image: np.ndarray) -> tuple[float | None, float]:
        return self._inner.read_weight(image)

    def lcd_box(self, image: np.ndarray):
        return self._inner.lcd_box(image)
