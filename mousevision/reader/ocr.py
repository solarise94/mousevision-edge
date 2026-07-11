"""OCR reader stub — reserved for alternate scale models.

Mac PoC fallback can later wire PaddleOCR.
Android edge device should use ML Kit Text Recognition instead (not PaddleOCR).
"""

from __future__ import annotations

import numpy as np


class OCRReader:
    """Placeholder. PoC uses TemplateReader; platform-specific OCR later."""

    def read_weight(self, image: np.ndarray) -> tuple[float | None, float]:
        raise NotImplementedError(
            "OCRReader is reserved. Use TemplateReader for PoC; "
            "on Android prefer ML Kit, on Mac optionally PaddleOCR."
        )
