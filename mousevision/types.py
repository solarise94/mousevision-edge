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
    # "pts" when derived from ffmpeg showinfo pts_time (post first-frame
    # normalization), "fallback_fps" when PTS was unavailable and we fell
    # back to index/fps. Downstream code can check this to know whether
    # timestamps are wall-clock-accurate or uniform-fps estimates.
    timestamp_source: str = "pts"
    # Original PTS before first-frame normalization (ms), for diagnostics.
    raw_pts_ms: float | None = None


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
    # weight may be None when analysis produced no usable estimate
    # (short/aborted/timeout session) — requires_manual_weight should be True.
    weight: float | None
    confidence: float
    platform_start_ms: float
    platform_end_ms: float
    photo_frame_index: int | None
    photo_observed_weight: float | None = None
    photo_weight_delta: float | None = None
    photo_selection: str = "platform_midpoint"
    weight_source: str = "stable_curve_median"
    photo_mouse_detected: bool = False
    photo_verified: bool = False
    needs_review: bool = False
    review_reason: str = ""
    # Unstable settlement: keep a guess in ``weight`` for display, but require
    # experimenter confirmation before the value counts as clean.
    guessed_weight: float | None = None
    requires_manual_weight: bool = False


@dataclass
class WeighingRecord:
    box_id: str
    weight: float | None
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
