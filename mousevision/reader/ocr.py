"""OCR reader — thin local wrapper; production uses HttpOcrReader + lcd-ocr service."""

from __future__ import annotations

import numpy as np

from mousevision.reader.http_ocr import HttpOcrReader
from mousevision.reader.observations import RawWeightObservation


class OcrReader:
    """Backward-compatible name; delegates to HttpOcrReader when url is set."""

    def __init__(self, base_url: str | None = None, **kwargs) -> None:
        if not base_url:
            raise ValueError(
                "Use TemplateReader for offline PoC, or HttpOcrReader(base_url=...)."
            )
        self._inner = HttpOcrReader(base_url, **kwargs)

    def read_weight(self, image: np.ndarray) -> tuple[float | None, float]:
        return self._inner.read_weight(image)

    def read_observation(self, image: np.ndarray) -> RawWeightObservation:
        return self._inner.read_observation(image)

    def lcd_box(self, image: np.ndarray | None = None):
        return self._inner.lcd_box(image)
