"""Persist weighing artifacts under a run directory."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from mousevision.types import AnalysisResult, CurvePoint, Frame, WeighingRecord


class Recorder:
    def __init__(self, output_root: str | Path, device_id: str) -> None:
        self.output_root = Path(output_root)
        self.device_id = device_id
        self.output_root.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        *,
        cage_id: str,
        ordinal: int,
        run_id: str,
        analysis: AnalysisResult,
        curve: list[CurvePoint],
        photo_frame: Frame | None,
        photo_image: np.ndarray | None = None,
        state_history: list[dict] | None = None,
        record_id: str | None = None,
        project_id: str = "default",
        requested_ordinal: int | None = None,
    ) -> Path:
        """Save as `mouse_{ordinal:03d}/` under the current run root."""
        rid = record_id or str(uuid.uuid4())
        out_dir = self.output_root / f"mouse_{ordinal:03d}"
        if out_dir.exists():
            raise FileExistsError(f"ordinal {ordinal} already exists: {out_dir}")
        out_dir.mkdir(parents=True, exist_ok=False)

        photo_name = "photo.jpg"
        image = photo_image
        if image is None and photo_frame is not None:
            image = photo_frame.image
        if image is not None:
            cv2.imwrite(str(out_dir / photo_name), image)

        curve_payload = [
            {
                "t_ms": p.timestamp_ms,
                "weight": p.weight,
                "confidence": p.confidence,
                "frame_index": p.frame_index,
            }
            for p in curve
        ]
        (out_dir / "curve.json").write_text(
            json.dumps(curve_payload, indent=2), encoding="utf-8"
        )

        # box_id in record JSON mirrors cage_id for legacy consumers.
        record = WeighingRecord(
            box_id=cage_id,
            weight=analysis.weight,
            confidence=analysis.confidence,
            timestamp=datetime.now().isoformat(timespec="seconds"),
            device=self.device_id,
            photo=photo_name,
            extra={
                "record_id": rid,
                "run_id": run_id,
                "cage_id": cage_id,
                "project_id": project_id,
                "ordinal": ordinal,
                "requested_ordinal": requested_ordinal,
                "actual_ordinal": ordinal,
                "platform_start_ms": analysis.platform_start_ms,
                "platform_end_ms": analysis.platform_end_ms,
                "photo_frame_index": analysis.photo_frame_index,
                "photo_observed_weight": analysis.photo_observed_weight,
                "photo_weight_delta": analysis.photo_weight_delta,
                "photo_selection": analysis.photo_selection,
                "state_history": state_history or [],
                "photo_saved": image is not None,
            },
        )
        (out_dir / "record.json").write_text(
            json.dumps(record.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return out_dir
