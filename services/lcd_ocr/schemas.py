"""Shared result schemas for the LCD OCR service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Single-frame statuses only — no temporal conclusions.
STATUS_READABLE = "readable"
STATUS_ZERO = "zero_display"
STATUS_TRANSITION = "transition"
STATUS_UNREADABLE = "unreadable"
STATUS_BAD_ROI = "bad_roi"
STATUS_NEGATIVE = "negative_display"


@dataclass
class LatencyBreakdown:
    locate_ms: float = 0.0
    warp_ms: float = 0.0
    infer_ms: float = 0.0
    total_ms: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return {
            "locate": round(self.locate_ms, 2),
            "warp": round(self.warp_ms, 2),
            "infer": round(self.infer_ms, 2),
            "total": round(self.total_ms, 2),
        }


@dataclass
class ReadResult:
    weight: float | None
    digits: list[str]
    digit_confidences: list[float]
    quality: float
    status: str
    screen_quad: list[list[float]] | None
    locator: str | None
    locator_confidence: float
    device: str
    model_version: str
    latency: LatencyBreakdown = field(default_factory=LatencyBreakdown)
    # Backward-compatible aliases for older clients.
    confidence: float = 0.0
    raw_text: str = ""
    lcd_box: dict[str, int] | None = None
    debug: dict[str, Any] | None = None

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "weight": self.weight,
            "confidence": round(float(self.confidence), 4),
            "digits": list(self.digits),
            "digit_confidences": [round(float(c), 4) for c in self.digit_confidences],
            "quality": round(float(self.quality), 4),
            "status": self.status,
            "raw_text": self.raw_text,
            "locator": self.locator,
            "locator_confidence": round(float(self.locator_confidence), 4),
            "screen_quad": self.screen_quad,
            "lcd_box": self.lcd_box,
            "model_version": self.model_version,
            "device": self.device,
            "latency_ms": self.latency.to_dict(),
            "debug": self.debug,
        }
