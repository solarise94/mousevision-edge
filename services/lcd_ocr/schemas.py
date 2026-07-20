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
    # P1-e: training flywheel assets (base64 JPEG, only when collection enabled).
    collection_assets: dict[str, str] | None = None  # {normalized_screen, chosen_strip, sign_patch}
    # Glare detection on the warped screen (top-level so temporal fusion /
    # clustering can downweight frames without probing ``debug``).
    glare_fraction: float = 0.0
    glare_overlaps_digits: bool = False

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
            "collection_assets": self.collection_assets,
            "glare_fraction": round(float(self.glare_fraction), 5),
            "glare_overlaps_digits": bool(self.glare_overlaps_digits),
        }
