"""RapidOCR engine wrapper with OpenVINO GPU prefer / CPU fallback."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from parse import ParseResult, parse_weight_text
from lcd import LcdBox, find_lcd_box, prepare_digit_variants

logger = logging.getLogger("lcd_ocr.engine")


@dataclass
class ReadResult:
    weight: float | None
    confidence: float
    raw_text: str
    digits: list[str]
    lcd_box: dict[str, int] | None
    device: str
    latency_ms: float
    debug: dict[str, Any] | None = None


class LcdOcrEngine:
    def __init__(self) -> None:
        self.device = "CPU"
        self.model_name = "rapidocr"
        self._engine = None
        self._init_engine()

    def _init_engine(self) -> None:
        prefer_gpu = os.environ.get("LCD_OCR_DEVICE", "GPU").upper() != "CPU"
        last_err: Exception | None = None

        if prefer_gpu:
            try:
                self._engine = self._build_openvino(device="GPU")
                self.device = "GPU"
                self.model_name = "rapidocr+openvino"
                logger.info("LCD OCR engine ready on OpenVINO GPU")
                return
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                logger.warning("OpenVINO GPU init failed, falling back: %s", exc)

        try:
            self._engine = self._build_openvino(device="CPU")
            self.device = "CPU"
            self.model_name = "rapidocr+openvino"
            logger.info("LCD OCR engine ready on OpenVINO CPU")
            return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            logger.warning("OpenVINO CPU init failed, trying onnxruntime: %s", exc)

        try:
            from rapidocr import RapidOCR

            self._engine = RapidOCR()
            self.device = "CPU"
            self.model_name = "rapidocr+onnxruntime"
            logger.info("LCD OCR engine ready on onnxruntime CPU")
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"failed to init OCR engine: {exc}; prior={last_err}") from exc

    @staticmethod
    def _build_openvino(*, device: str):
        from rapidocr import EngineType, RapidOCR

        # RapidOCR 3.x: OpenVINO via EngineType; device via EngineConfig when supported.
        params: dict[str, Any] = {
            "Det.engine_type": EngineType.OPENVINO,
            "Cls.engine_type": EngineType.OPENVINO,
            "Rec.engine_type": EngineType.OPENVINO,
        }
        # Best-effort GPU device hints (ignored if unsupported by this RapidOCR build).
        if device.upper() == "GPU":
            params["EngineConfig.openvino.inference_num_threads"] = 4
            # Probe OpenVINO devices first so we fail fast if GPU is absent.
            import openvino as ov

            core = ov.Core()
            available = core.available_devices
            if not any(d.startswith("GPU") for d in available):
                raise RuntimeError(f"no OpenVINO GPU in available_devices={available}")
            params["EngineConfig.openvino.device"] = "GPU"
            params["Det.engine_config"] = {"device_name": "GPU"}
            params["Cls.engine_config"] = {"device_name": "GPU"}
            params["Rec.engine_config"] = {"device_name": "GPU"}
        return RapidOCR(params=params)

    def read(
        self,
        image_bgr: np.ndarray,
        *,
        lcd_box: dict | None = None,
        return_debug: bool = False,
    ) -> ReadResult:
        t0 = time.perf_counter()
        box = LcdBox.from_dict(lcd_box) if lcd_box else find_lcd_box(image_bgr)
        if box is None:
            return ReadResult(
                weight=None,
                confidence=0.0,
                raw_text="",
                digits=[],
                lcd_box=None,
                device=self.device,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                debug={"error": "lcd_not_found"} if return_debug else None,
            )

        variants = prepare_digit_variants(box.crop(image_bgr))
        votes: list[tuple[float, float, str, list[str]]] = []
        raw_all: list[str] = []
        for roi in variants:
            raw_text, score = self._run_ocr(roi)
            if raw_text:
                raw_all.append(raw_text)
            parsed: ParseResult = parse_weight_text(raw_text, score)
            if parsed.weight is not None:
                votes.append(
                    (float(parsed.weight), float(parsed.confidence), raw_text, parsed.digits)
                )

        best_weight: float | None = None
        best_conf = 0.0
        best_raw = " | ".join(raw_all)
        best_digits: list[str] = []
        if votes:
            # Consensus by (count, mean conf); break ties toward higher conf,
            # then prefer non-integer (XX.XX) readings typical of the LCD.
            by_value: dict[float, list[tuple[float, float, str, list[str]]]] = {}
            for v in votes:
                by_value.setdefault(round(v[0], 2), []).append(v)

            def rank(val: float) -> tuple[int, float, int]:
                pool = by_value[val]
                mean_conf = sum(p[1] for p in pool) / len(pool)
                # Prefer fractional LCD style (23.19) over whole grams (61).
                frac_bonus = 1 if abs(val - round(val)) > 1e-6 else 0
                return (len(pool), mean_conf, frac_bonus)

            consensus = max(by_value.keys(), key=rank)
            pool = by_value[consensus]
            best_weight, best_conf, best_raw_one, best_digits = max(pool, key=lambda v: v[1])
            best_raw = best_raw_one if not raw_all else best_raw

        debug = None
        if return_debug:
            debug = {
                "variants": len(variants),
                "vote_count": len(votes),
                "raw_all": raw_all,
                "model": self.model_name,
            }
        return ReadResult(
            weight=best_weight,
            confidence=best_conf,
            raw_text=best_raw,
            digits=best_digits,
            lcd_box=box.to_dict(),
            device=self.device,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            debug=debug,
        )

    def _run_ocr(self, image_bgr: np.ndarray) -> tuple[str, float]:
        assert self._engine is not None
        result = self._engine(image_bgr)
        # RapidOCR 3.x returns an object with .txts / .scores or nested lists.
        texts: list[str] = []
        scores: list[float] = []

        if result is None:
            return "", 0.0

        if hasattr(result, "txts") and result.txts is not None:
            texts = [str(t) for t in result.txts]
            if hasattr(result, "scores") and result.scores is not None:
                scores = [float(s) for s in result.scores]
        elif isinstance(result, (list, tuple)):
            # Legacy: [[box, text, score], ...] or (boxes, texts, scores)
            if result and isinstance(result[0], (list, tuple)) and len(result[0]) >= 2:
                for item in result:
                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                        texts.append(str(item[1]))
                        if len(item) >= 3:
                            try:
                                scores.append(float(item[2]))
                            except (TypeError, ValueError):
                                pass
            elif len(result) >= 2 and result[1] is not None:
                texts = [str(t) for t in result[1]]
                if len(result) >= 3 and result[2] is not None:
                    scores = [float(s) for s in result[2]]

        raw = " ".join(texts).strip()
        conf = float(sum(scores) / len(scores)) if scores else (0.5 if raw else 0.0)
        return raw, conf
