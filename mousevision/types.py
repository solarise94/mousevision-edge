"""Shared data types for MouseVision Edge."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class Frame:
    image: np.ndarray  # BGR
    timestamp_ms: float
    index: int


@dataclass
class WeightSample:
    timestamp_ms: float
    weight: float
    confidence: float
    frame_index: int


@dataclass
class CurvePoint:
    timestamp_ms: float
    weight: float
    confidence: float
    frame_index: int


@dataclass
class AnalysisResult:
    weight: float
    confidence: float
    platform_start_ms: float
    platform_end_ms: float
    photo_frame_index: int
    photo_observed_weight: float | None = None
    photo_weight_delta: float | None = None
    photo_selection: str = "platform_midpoint"
    weight_source: str = "stable_curve_median"
    photo_mouse_detected: bool = False
    photo_verified: bool = True


@dataclass
class WeighingRecord:
    box_id: str
    weight: float
    confidence: float
    timestamp: str
    device: str
    photo: str = "photo.jpg"
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "box_id": self.box_id,
            "weight": self.weight,
            "confidence": self.confidence,
            "timestamp": self.timestamp,
            "device": self.device,
            "photo": self.photo,
        }
        data.update(self.extra)
        return data
