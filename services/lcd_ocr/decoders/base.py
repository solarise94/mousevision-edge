"""Pluggable digit decoder interface for LCD OCR."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class DecoderResult:
    digits: list[str]  # fixed four slots: blank | 0..9 | invalid
    digit_confidences: list[float]
    weight: float | None
    status: str  # readable | zero_display | transition | unreadable
    quality: float
    evidence: dict[str, Any] = field(default_factory=dict)


class DigitDecoder(Protocol):
    name: str

    def read(self, normalized_strip: Any, slot_patches: list[Any]) -> DecoderResult:
        """Decode a normalized digit strip (and optional pre-cut slots)."""
        ...
