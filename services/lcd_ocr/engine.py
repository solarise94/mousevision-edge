"""LCD OCR engine: locate → normalize → pluggable DigitDecoder (RapidOCR audit optional)."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import numpy as np

from decoders import get_decoder
from decoders.base import DecoderResult
from locator import locate_screen, quad_to_bbox
from normalize import NormalizeConfig, strip_slot_candidates
from profile import load_scale_profile
from schemas import (
    STATUS_BAD_ROI,
    STATUS_READABLE,
    STATUS_TRANSITION,
    STATUS_UNREADABLE,
    STATUS_ZERO,
    LatencyBreakdown,
    ReadResult,
)
from sevenseg_classic import ClassicRead, read_fixed_slots

logger = logging.getLogger("lcd_ocr.engine")


def _map_status(status: str) -> str:
    if status == "readable":
        return STATUS_READABLE
    if status == "zero_display":
        return STATUS_ZERO
    if status == "transition":
        return STATUS_TRANSITION
    return STATUS_UNREADABLE


def _result_to_classic(result: DecoderResult) -> ClassicRead:
    return ClassicRead(
        weight=result.weight,
        digits=list(result.digits),
        digit_confidences=list(result.digit_confidences),
        quality=float(result.quality),
        status=result.status,
        evidence=dict(result.evidence or {}),
    )


class LcdOcrEngine:
    def __init__(self, *, scale_profile: dict[str, Any] | None = None) -> None:
        self.device = "CPU"
        profile = scale_profile if scale_profile is not None else load_scale_profile()
        decoder_name = str(
            os.environ.get("LCD_OCR_DECODER")
            or (profile.get("lcd_ocr") or {}).get("decoder")
            or "classic_v2"
        ).strip()
        self.decoder_name = decoder_name
        self.decoder = get_decoder(decoder_name)
        self.model_name = f"{self.decoder.name}"
        self.model_version = f"{self.decoder.name}-v1"
        norm = profile.get("lcd_normalization") or {}
        roi = norm.get("digit_roi", [0.20, 0.08, 0.66, 0.84])
        self.norm_cfg = NormalizeConfig(
            width=int(norm.get("width", 480)),
            height=int(norm.get("height", 128)),
            digit_roi=tuple(float(x) for x in roi),  # type: ignore[arg-type]
            slot_count=int(norm.get("digit_slots", 4)),
            slot_margin=float(norm.get("slot_margin", 0.03)),
            ink_trim=bool(norm.get("ink_trim", True)),
            ink_trim_pad=float(norm.get("ink_trim_pad", 0.05)),
            slot_mode=str(norm.get("slot_mode", "projected")),
            allow_warp=bool(norm.get("allow_warp", False)),
            skew_warp_min=float(norm.get("skew_warp_min", 8.0)),
            skew_warp_threshold=float(norm.get("skew_warp_threshold", 35.0)),
        )
        lcd = profile.get("lcd_detect") or {}
        self.hsv_low = tuple(lcd.get("hsv_low", [90, 40, 80]))  # type: ignore[assignment]
        self.hsv_high = tuple(lcd.get("hsv_high", [130, 255, 255]))  # type: ignore[assignment]
        self.min_area = int(lcd.get("min_area", 8000))
        self.min_width = int(lcd.get("min_width", 150))
        self.min_height = int(lcd.get("min_height", 40))
        self.min_locator_confidence = float(norm.get("min_locator_confidence", 0.55))
        self.fixed_roi = profile.get("weight_roi")
        self._audit_engine = None
        self._latency_window: list[float] = []
        self._warmup_done = False
        self.scale_profile_name = str(profile.get("scale_profile", "current_scale_v1"))
        self._use_legacy_classic = decoder_name.lower() in {"classic", "classic_v1"}

    def warmup(self) -> dict[str, float]:
        """Tiny synthetic probe for /health real latency."""
        img = np.zeros((240, 320, 3), dtype=np.uint8)
        img[80:140, 40:280] = (200, 80, 40)
        t0 = time.perf_counter()
        _ = self.read(img, return_debug=False)
        ms = (time.perf_counter() - t0) * 1000.0
        self._latency_window.append(ms)
        self._latency_window = self._latency_window[-50:]
        self._warmup_done = True
        return {"probe_ms": round(ms, 2)}

    def health_stats(self) -> dict[str, Any]:
        if not self._warmup_done:
            self.warmup()
        window = list(self._latency_window) or [0.0]
        arr = np.asarray(window, dtype=np.float64)
        return {
            "ok": True,
            "device": self.device,
            "model": self.model_version,
            "decoder": self.decoder_name,
            "p50_latency_ms": round(float(np.percentile(arr, 50)), 2),
            "p95_latency_ms": round(float(np.percentile(arr, 95)), 2),
            "rapidocr_audit_ready": self._audit_engine is not None,
        }

    def read(
        self,
        image_bgr: np.ndarray,
        *,
        quad_hint: list[list[float]] | None = None,
        lcd_box: dict | None = None,
        return_debug: bool = False,
        run_audit: bool = False,
    ) -> ReadResult:
        t0 = time.perf_counter()
        fixed = lcd_box if lcd_box is not None else self.fixed_roi

        t_locate = time.perf_counter()
        located = locate_screen(
            image_bgr,
            quad_hint=quad_hint,
            fixed_roi=fixed,
            hsv_low=self.hsv_low,  # type: ignore[arg-type]
            hsv_high=self.hsv_high,  # type: ignore[arg-type]
            min_area=self.min_area,
            min_width=self.min_width,
            min_height=self.min_height,
            min_locator_confidence=self.min_locator_confidence,
        )
        locate_ms = (time.perf_counter() - t_locate) * 1000.0

        if located is None or located.orientation != "upright":
            total = (time.perf_counter() - t0) * 1000.0
            self._note_latency(total)
            return ReadResult(
                weight=None,
                digits=[],
                digit_confidences=[],
                quality=0.0,
                status=STATUS_BAD_ROI,
                screen_quad=None,
                locator=None,
                locator_confidence=0.0,
                device=self.device,
                model_version=self.model_version,
                latency=LatencyBreakdown(locate_ms=locate_ms, total_ms=total),
                confidence=0.0,
                raw_text="",
                lcd_box=None,
                debug={"error": "lcd_not_found"} if return_debug else None,
            )

        t_warp = time.perf_counter()
        warped, screen_method, variants = strip_slot_candidates(
            image_bgr, located.screen_quad, self.norm_cfg
        )
        warp_ms = (time.perf_counter() - t_warp) * 1000.0

        usable = [(label, strip, slots) for label, strip, slots in variants if len(slots) == 4]
        if not usable:
            total = (time.perf_counter() - t0) * 1000.0
            self._note_latency(total)
            return ReadResult(
                weight=None,
                digits=[],
                digit_confidences=[],
                quality=0.0,
                status=STATUS_BAD_ROI,
                screen_quad=[list(p) for p in located.screen_quad],
                locator=located.method,
                locator_confidence=located.confidence,
                device=self.device,
                model_version=self.model_version,
                latency=LatencyBreakdown(
                    locate_ms=locate_ms, warp_ms=warp_ms, total_ms=total
                ),
                confidence=0.0,
                lcd_box=quad_to_bbox(located.screen_quad),
                debug={"error": "digit_strip_empty"} if return_debug else None,
            )

        t_infer = time.perf_counter()
        classic, strip, chosen_label = self._vote_variants(usable)
        infer_ms = (time.perf_counter() - t_infer) * 1000.0
        total = (time.perf_counter() - t0) * 1000.0
        self._note_latency(total)

        status = _map_status(classic.status)
        conf = float(classic.quality)
        if classic.digit_confidences:
            conf = float(
                0.5 * classic.quality + 0.5 * float(np.mean(classic.digit_confidences))
            )

        debug = None
        if return_debug or run_audit:
            debug = {
                "locator": located.method,
                "warped_shape": list(warped.shape),
                "strip_shape": list(strip.shape),
                "digits": classic.digits,
                "model": self.model_version,
                "decoder": self.decoder_name,
                "screen_method": screen_method,
                "slot_mode": self.norm_cfg.slot_mode,
                "strip_variant": chosen_label,
                "evidence": classic.evidence,
            }
            if run_audit:
                debug["audit_text"] = self._audit_text(strip)

        raw = "".join(c if c.isdigit() else ("_" if c == "blank" else "?") for c in classic.digits)
        return ReadResult(
            weight=classic.weight,
            digits=classic.digits,
            digit_confidences=classic.digit_confidences,
            quality=float(classic.quality),
            status=status,
            screen_quad=[list(p) for p in located.screen_quad],
            locator=located.method,
            locator_confidence=float(located.confidence),
            device=self.device,
            model_version=self.model_version,
            latency=LatencyBreakdown(
                locate_ms=locate_ms,
                warp_ms=warp_ms,
                infer_ms=infer_ms,
                total_ms=total,
            ),
            confidence=conf,
            raw_text=raw,
            lcd_box=quad_to_bbox(located.screen_quad),
            debug=debug,
        )

    def _decode_slots(self, strip: Any, slots: list) -> ClassicRead:
        if self._use_legacy_classic:
            return read_fixed_slots(slots)
        result = self.decoder.read(strip, slots)
        return _result_to_classic(result)

    def _vote_variants(
        self,
        usable: list[tuple[str, Any, list]],
    ) -> tuple[ClassicRead, Any, str]:
        """Pick one intact variant — never stitch conflicting slot digits."""
        reads: list[tuple[ClassicRead, Any, str]] = []
        for label, strip, slots in usable:
            reads.append((self._decode_slots(strip, slots), strip, label))

        readable = [
            (r, s, lab)
            for r, s, lab in reads
            if r.status == "readable" and r.weight is not None and r.weight > 0.05
        ]
        # CLAHE often blooms leading "1" into "7" (15.10→75.10). Drop out-of-band.
        phys = [
            t for t in readable if 5.0 <= float(t[0].weight) <= 50.0  # type: ignore[arg-type]
        ]
        pool = phys if phys else readable

        def _digit_diffs(a: ClassicRead, b: ClassicRead) -> int:
            da, db = list(a.digits), list(b.digits)
            if len(da) != 4 or len(db) != 4:
                return 4
            return sum(1 for x, y in zip(da, db) if x != y)

        def _leading_mismatch(a: ClassicRead, b: ClassicRead) -> bool:
            da, db = list(a.digits), list(b.digits)
            return (
                len(da) == 4
                and len(db) == 4
                and da[0].isdigit()
                and db[0].isdigit()
                and da[0] != db[0]
            )

        if len(pool) >= 2:
            weights = [float(t[0].weight) for t in pool]  # type: ignore[arg-type]
            wspan = max(weights) - min(weights)
            max_diffs = 0
            lead_bad = False
            for i in range(len(pool)):
                for j in range(i + 1, len(pool)):
                    max_diffs = max(max_diffs, _digit_diffs(pool[i][0], pool[j][0]))
                    if _leading_mismatch(pool[i][0], pool[j][0]):
                        lead_bad = True
            # Hard conflict: incompatible platforms (e.g. 11.xx vs 17.xx).
            hard = lead_bad or (wspan > 1.0 and max_diffs >= 1) or max_diffs >= 2 and wspan > 0.35
            if hard:
                # Prefer intact animal-range majority-of-one; else transition.
                if len(phys) == 1:
                    return phys[0][0], phys[0][1], f"phys:{phys[0][2]}"
                return (
                    ClassicRead(
                        weight=None,
                        digits=list(pool[0][0].digits),
                        digit_confidences=list(pool[0][0].digit_confidences)
                        or [0.0] * 4,
                        quality=0.0,
                        status="transition",
                        evidence={
                            "quality_gate": "variant_conflict",
                            "variants": [
                                {
                                    "label": lab,
                                    "weight": r.weight,
                                    "digits": list(r.digits),
                                }
                                for r, _s, lab in pool
                            ],
                        },
                    ),
                    pool[0][1],
                    "variant_conflict",
                )
            # Soft disagreement (last digit jitter): pick best intact reading.
            return max(pool, key=lambda t: float(t[0].quality))

        if pool:
            return pool[0][0], pool[0][1], pool[0][2]

        zeros = [t for t in reads if t[0].status == "zero_display"]
        if zeros:
            return zeros[0]

        transitions = [t for t in reads if t[0].status == "transition"]
        if transitions:
            return transitions[0]

        return max(reads, key=lambda t: float(t[0].quality))

    def _note_latency(self, ms: float) -> None:
        self._latency_window.append(float(ms))
        self._latency_window = self._latency_window[-50:]

    def _audit_text(self, strip_bgr: np.ndarray) -> str:
        """Optional RapidOCR audit — never used as final weight."""
        try:
            engine = self._get_audit_engine()
            if engine is None:
                return ""
            result = engine(strip_bgr)
            if result is None:
                return ""
            if hasattr(result, "txts") and result.txts is not None:
                return " ".join(str(t) for t in result.txts)
        except Exception as exc:  # noqa: BLE001
            logger.debug("audit OCR failed: %s", exc)
        return ""

    def _get_audit_engine(self):
        if self._audit_engine is not None:
            return self._audit_engine
        if os.environ.get("LCD_OCR_AUDIT", "").lower() not in {"1", "true", "yes"}:
            return None
        try:
            from rapidocr import RapidOCR

            self._audit_engine = RapidOCR()
            logger.info("RapidOCR audit engine loaded (lazy)")
            return self._audit_engine
        except Exception as exc:  # noqa: BLE001
            logger.warning("RapidOCR audit unavailable: %s", exc)
            return None
