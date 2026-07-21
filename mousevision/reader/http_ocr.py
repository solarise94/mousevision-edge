"""HTTP client for the lcd-ocr service (stateless single-frame API)."""

from __future__ import annotations

import json
import logging
from typing import Any

import cv2
import httpx
import numpy as np

from mousevision.reader.observations import RawWeightObservation
from mousevision.reader.template import LcdBox

logger = logging.getLogger(__name__)


class HttpOcrReader:
    """Call POST /v1/lcd/read per frame. Caller holds quad_hint / fusion state."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_ms: int = 2000,
        match_threshold: float = 0.35,
        weight_roi: dict | None = None,
        lcd_detect: dict | None = None,
        # Force a full HSV relocate every N frames (do not send sticky hint).
        force_relocate_every: int = 12,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_ms / 1000.0
        self.match_threshold = match_threshold
        self.weight_roi = weight_roi
        self.lcd_detect = lcd_detect or {}
        self.force_relocate_every = max(1, int(force_relocate_every))
        self._client = httpx.Client(timeout=self.timeout)
        self._last_quad: list[list[float]] | None = None
        self._last_box: LcdBox | None = None
        self._last_obs: RawWeightObservation | None = None
        self._hint_age = 0
        self._collect_assets = False

    def close(self) -> None:
        self._client.close()

    def reset_tracking(self) -> None:
        self._last_quad = None
        self._last_box = None
        self._hint_age = 0

    def lcd_box(self, image: np.ndarray | None = None) -> LcdBox | None:
        """Return last known LCD bbox (from service screen_quad)."""
        del image
        return self._last_box

    def read_observation(self, image: np.ndarray) -> RawWeightObservation:
        ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        if not ok:
            return RawWeightObservation(weight=None, status="unreadable")

        return_debug = bool(getattr(self, "_collect_assets", False))
        data: dict[str, Any] = {"return_debug": "true" if return_debug else "false"}
        files = {"file": ("frame.jpg", buf.tobytes(), "image/jpeg")}
        use_hint = self._last_quad is not None
        if use_hint:
            self._hint_age += 1
            # Periodic forced HSV relocate: omit sticky hint this frame.
            if self._hint_age >= self.force_relocate_every:
                use_hint = False
                self._hint_age = 0
        if use_hint and self._last_quad is not None:
            data["quad_hint"] = json.dumps(self._last_quad)
        if self.weight_roi is not None and self._last_quad is None:
            data.update(
                {
                    "lcd_x": str(int(self.weight_roi["x"])),
                    "lcd_y": str(int(self.weight_roi["y"])),
                    "lcd_w": str(int(self.weight_roi["w"])),
                    "lcd_h": str(int(self.weight_roi["h"])),
                }
            )

        try:
            resp = self._client.post(f"{self.base_url}/v1/lcd/read", files=files, data=data)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("lcd-ocr request failed: %s", exc)
            return RawWeightObservation(weight=None, status="unreadable", quality=0.0)

        obs = self._parse_payload(payload)
        self._last_obs = obs
        if obs.status in {"unreadable", "bad_roi", "transition"}:
            # Drop sticky hint so a bad locate / transition cannot poison later frames.
            self._last_quad = None
            self._last_box = None
            self._hint_age = 0
        elif obs.screen_quad is not None:
            self._last_quad = obs.screen_quad
            self._last_box = self._box_from_quad(obs.screen_quad)
            if not use_hint:
                self._hint_age = 0
        return obs

    def read_weight(
        self,
        image: np.ndarray,
        *,
        lcd_box: Any | None = None,  # noqa: ARG002 — protocol compat with TemplateReader
    ) -> tuple[float | None, float]:
        """Backward-compatible tuple API (no temporal fusion)."""
        obs = self.read_observation(image)
        if obs.status not in {"readable", "zero_display"}:
            return None, float(obs.confidence)
        if obs.weight is None:
            return None, float(obs.confidence)
        if obs.status == "readable" and float(obs.confidence) < self.match_threshold:
            return None, float(obs.confidence)
        if obs.weight < 0 or obs.weight > 80:
            return None, float(obs.confidence)
        return float(obs.weight), float(obs.confidence)

    @staticmethod
    def _box_from_quad(quad: list[list[float]]) -> LcdBox:
        xs = [float(p[0]) for p in quad]
        ys = [float(p[1]) for p in quad]
        x0, y0 = int(min(xs)), int(min(ys))
        x1, y1 = int(max(xs)), int(max(ys))
        return LcdBox(x=x0, y=y0, w=max(1, x1 - x0), h=max(1, y1 - y0))

    @staticmethod
    def _parse_payload(payload: dict[str, Any]) -> RawWeightObservation:
        weight = payload.get("weight")
        try:
            weight_f = float(weight) if weight is not None else None
        except (TypeError, ValueError):
            weight_f = None

        lat = payload.get("latency_ms")
        if isinstance(lat, dict):
            latency = float(lat.get("total") or 0.0)
        else:
            try:
                latency = float(lat or 0.0)
            except (TypeError, ValueError):
                latency = 0.0

        conf = float(payload.get("confidence") or payload.get("quality") or 0.0)
        status = str(payload.get("status") or "unreadable")
        if weight_f is not None and weight_f < 0:
            status = "negative_display"
            weight_f = None
        elif status == "negative_display":
            weight_f = None
        digits = [str(d) for d in (payload.get("digits") or [])]
        digit_confs = [float(c) for c in (payload.get("digit_confidences") or [])]
        quad = payload.get("screen_quad")
        if isinstance(quad, list) and len(quad) == 4:
            screen_quad = [[float(p[0]), float(p[1])] for p in quad]
        else:
            screen_quad = None

        return RawWeightObservation(
            weight=weight_f,
            digits=digits,
            digit_confidences=digit_confs,
            quality=float(payload.get("quality") or conf),
            status=status,
            screen_quad=screen_quad,
            locator_confidence=float(payload.get("locator_confidence") or 0.0),
            locator=payload.get("locator"),
            model_version=payload.get("model_version"),
            confidence=conf,
            raw_text=str(payload.get("raw_text") or ""),
        collection_assets=payload.get("collection_assets"),
            latency_ms=latency,
        )
