"""Structured weight observations between OCR service and temporal fusion."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawWeightObservation:
    weight: float | None
    digits: list[str] = field(default_factory=list)
    digit_confidences: list[float] = field(default_factory=list)
    quality: float = 0.0
    # readable | zero_display | negative_display | transition | unreadable | bad_roi
    status: str = "unreadable"
    screen_quad: list[list[float]] | None = None
    locator_confidence: float = 0.0
    locator: str | None = None
    model_version: str | None = None
    confidence: float = 0.0
    raw_text: str = ""
    latency_ms: float = 0.0
    collection_assets: dict[str, str] | None = None  # base64 JPEGs from OCR service
    # Timestamp attached by temporal fusion for time-weighted clustering.
    _ts_ms: float = 0.0

    @property
    def is_readable(self) -> bool:
        return self.status == "readable" and self.weight is not None

    @property
    def is_zero_display(self) -> bool:
        return self.status == "zero_display"

    @property
    def is_negative_display(self) -> bool:
        return self.status == "negative_display"


@dataclass
class StableWeightObservation:
    weight: float
    confidence: float
    digits: list[str] = field(default_factory=list)
    reason: str = "consensus"
    needs_review: bool = False
    review_reason: str = ""
    screen_quad: list[list[float]] | None = None
