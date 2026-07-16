"""Decoder factory: LCD_OCR_DECODER = classic_v2 | ssocr | segodec | classic."""

from __future__ import annotations

import os
from typing import Any

from .base import DecoderResult, DigitDecoder
from .classic_v2 import ClassicV2Decoder
from .segodec_adapter import SegoDecAdapter
from .ssocr_adapter import SsocrAdapter

# Legacy alias kept for offline A/B against pre-gate classic behavior.
_LEGACY_NAMES = {"classic", "classic_v1"}


def available_decoders() -> list[str]:
    names = ["classic_v2", "segodec", "ssocr"]
    return names


def get_decoder(name: str | None = None) -> Any:
    """Return a DigitDecoder instance for the given name."""
    chosen = (name or os.environ.get("LCD_OCR_DECODER") or "classic_v2").strip().lower()
    if chosen in {"classic_v2", "classic-v2"}:
        return ClassicV2Decoder()
    if chosen in {"segodec", "sego"}:
        return SegoDecAdapter()
    if chosen == "ssocr":
        return SsocrAdapter()
    if chosen in _LEGACY_NAMES:
        # Use classic_v2 path; genuine v1 is sevenseg_classic.read_fixed_slots via engine fallback.
        return ClassicV2Decoder()
    raise ValueError(f"unknown LCD_OCR_DECODER={chosen!r}; try {available_decoders()}")


__all__ = [
    "DecoderResult",
    "DigitDecoder",
    "available_decoders",
    "get_decoder",
    "ClassicV2Decoder",
    "SegoDecAdapter",
    "SsocrAdapter",
]
