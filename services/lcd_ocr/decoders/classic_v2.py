"""Classic seven-seg decoder v2: multi-evidence 1/7 + strip quality gates."""

from __future__ import annotations

from typing import Any

import numpy as np

from quality import assess_strip_quality, tall_glyph_ranges
from sevenseg_classic import ClassicRead, compose_weight, decode_slot_patch

from .base import DecoderResult


class ClassicV2Decoder:
    name = "classic_v2"

    def read(self, normalized_strip: Any, slot_patches: list[Any]) -> DecoderResult:
        if len(slot_patches) != 4:
            return DecoderResult(
                digits=[],
                digit_confidences=[],
                weight=None,
                status="unreadable",
                quality=0.0,
                evidence={"error": "need_4_slots"},
            )

        # This scale renders zero as three glyphs (0.00). Detect those intact
        # glyphs before the four-slot splitter can divide them into 10.81.
        zero_ranges = tall_glyph_ranges(normalized_strip)
        if len(zero_ranges) == 3:
            zero_decoded = []
            for x0, x1 in zero_ranges:
                pad = max(1, int(0.05 * (x1 - x0)))
                patch = normalized_strip[:, max(0, x0 - pad) : min(normalized_strip.shape[1], x1 + pad)]
                zero_decoded.append(decode_slot_patch(patch))
            if all(d.char == "0" for d in zero_decoded):
                return DecoderResult(
                    digits=["0", "0", "0"],
                    digit_confidences=[float(d.confidence) for d in zero_decoded],
                    weight=0.0,
                    status="zero_display",
                    quality=float(np.mean([d.confidence for d in zero_decoded])),
                    evidence={
                        "quality_gate": "three_zero_glyphs",
                        "glyph_ranges": zero_ranges,
                    },
                )

        q = assess_strip_quality(normalized_strip, slot_patches)
        if q.status == "zero_display":
            return DecoderResult(
                digits=["0", "0", "0", "0"],
                digit_confidences=[0.9, 0.9, 0.9, 0.9],
                weight=0.0,
                status="zero_display",
                quality=0.85,
                evidence={"quality_gate": q.reason, **q.evidence},
            )
        if q.status in {"transition", "unreadable"}:
            return DecoderResult(
                digits=["invalid"] * 4,
                digit_confidences=[0.0] * 4,
                weight=None,
                status=q.status,
                quality=0.0,
                evidence={"quality_gate": q.reason, **q.evidence},
            )

        decoded = [decode_slot_patch(p) for p in slot_patches]
        chars = [d.char for d in decoded]
        confs = [d.confidence for d in decoded]
        evidence = {
            "quality_gate": "ok",
            "top_bars": [round(float(d.top_bar), 3) for d in decoded],
            "ink_ratio": q.ink_ratio,
            **q.evidence,
        }

        if any(c == "invalid" for c in chars):
            return DecoderResult(
                digits=chars,
                digit_confidences=confs,
                weight=None,
                status="unreadable",
                quality=float(np.mean(confs) if confs else 0.0),
                evidence=evidence,
            )

        quality = float(np.mean(confs)) if confs else 0.0
        for d in decoded:
            if d.char == "7" and d.top_bar < 0.25:
                quality *= 0.85
            if d.char == "1" and d.top_bar >= 0.40:
                quality *= 0.85

        # Soft reject: four "1"s with weak top bars after decode still look
        # like glare residue — treat as transition, do not emit 11.11.
        ones = sum(1 for c in chars if c == "1")
        if chars == ["1", "1", "1", "1"] or (ones >= 3 and all(c in {"1", "blank", "0"} for c in chars)):
            weak = sum(1 for d in decoded if d.top_bar < 0.22)
            if weak >= 2 or q.ink_ratio < 0.12 or chars.count("1") >= 3:
                return DecoderResult(
                    digits=chars,
                    digit_confidences=confs,
                    weight=None,
                    status="transition",
                    quality=quality * 0.4,
                    evidence={**evidence, "quality_gate": "pseudo_ones"},
                )

        weight = compose_weight(chars)
        if weight is None:
            return DecoderResult(
                digits=chars,
                digit_confidences=confs,
                weight=None,
                status="unreadable",
                quality=quality,
                evidence=evidence,
            )

        zeroish = sum(1 for c in chars if c in {"blank", "0"}) >= 3
        if weight <= 0.05 or (weight < 0.10 and zeroish):
            return DecoderResult(
                digits=chars,
                digit_confidences=confs,
                weight=0.0,
                status="zero_display",
                quality=quality,
                evidence=evidence,
            )

        # Keep only the configured scale-range ceiling here.  A lower animal
        # weight bound would hide decoder mistakes and reject valid small
        # animals; low non-zero values must remain observable downstream.
        if weight > 50.0:
            return DecoderResult(
                digits=chars,
                digit_confidences=confs,
                weight=None,
                status="transition",
                quality=quality * 0.3,
                evidence={**evidence, "quality_gate": "out_of_band", "weight": weight},
            )

        return DecoderResult(
            digits=chars,
            digit_confidences=confs,
            weight=round(float(weight), 2),
            status="readable",
            quality=quality,
            evidence=evidence,
        )


def classic_read_from_decoder(result: DecoderResult) -> ClassicRead:
    return ClassicRead(
        weight=result.weight,
        digits=list(result.digits),
        digit_confidences=list(result.digit_confidences),
        quality=float(result.quality),
        status=result.status,
        evidence=dict(result.evidence or {}),
    )
