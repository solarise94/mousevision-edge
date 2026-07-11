"""Weight reader interfaces."""

from __future__ import annotations

from typing import Protocol

import numpy as np


class WeightReader(Protocol):
    def read_weight(self, image: np.ndarray) -> tuple[float | None, float]:
        """Return (weight_grams, confidence). weight is None if unreadable."""
        ...
