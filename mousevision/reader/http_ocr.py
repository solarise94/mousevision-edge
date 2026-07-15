"""HTTP client WeightReader for the lcd-ocr service (single-frame API)."""

from __future__ import annotations

import logging
from typing import Any

import cv2
import httpx
import numpy as np

from mousevision.reader.template import LcdBox, find_lcd_box

logger = logging.getLogger(__name__)


class HttpOcrReader:
    """Call POST /v1/lcd/read per frame. Does NOT use read-batch (state machine needs immediacy)."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_ms: int = 500,
        lcd_detect: dict | None = None,
        weight_roi: dict | None = None,
        match_threshold: float = 0.35,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_ms / 1000.0
        self.lcd_detect = lcd_detect or {}
        self.weight_roi = weight_roi
        self.match_threshold = match_threshold
        self._client = httpx.Client(timeout=self.timeout)

    def close(self) -> None:
        self._client.close()

    def lcd_box(self, image: np.ndarray) -> LcdBox | None:
        mode = str(self.lcd_detect.get("mode", "auto")).lower()
        fixed = self.weight_roi if mode == "fixed" else None
        return find_lcd_box(
            image,
            hsv_low=tuple(self.lcd_detect.get("hsv_low", (90, 40, 80))),
            hsv_high=tuple(self.lcd_detect.get("hsv_high", (130, 255, 255))),
            min_area=int(self.lcd_detect.get("min_area", 8000)),
            min_width=int(self.lcd_detect.get("min_width", 150)),
            min_height=int(self.lcd_detect.get("min_height", 40)),
            fixed_roi=fixed,
        )

    def read_weight(self, image: np.ndarray) -> tuple[float | None, float]:
        ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        if not ok:
            return None, 0.0
        box = self.lcd_box(image)
        data: dict[str, Any] = {"return_debug": "false"}
        files = {"file": ("frame.jpg", buf.tobytes(), "image/jpeg")}
        if box is not None:
            data.update(
                {
                    "lcd_x": str(box.x),
                    "lcd_y": str(box.y),
                    "lcd_w": str(box.w),
                    "lcd_h": str(box.h),
                }
            )
        try:
            resp = self._client.post(f"{self.base_url}/v1/lcd/read", files=files, data=data)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("lcd-ocr request failed: %s", exc)
            return None, 0.0

        weight = payload.get("weight")
        conf = float(payload.get("confidence") or 0.0)
        if weight is None or conf < self.match_threshold:
            return None, conf
        try:
            value = float(weight)
        except (TypeError, ValueError):
            return None, conf
        if value < 0 or value > 80:
            return None, conf
        return value, conf
