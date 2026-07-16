"""Parse OCR raw text into a scale weight in grams."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class ParseResult:
    weight: float | None
    confidence: float
    raw_text: str
    digits: list[str]
    negative: bool = False


def _normalize_text(raw: str) -> str:
    text = raw.strip().replace(" ", "").replace("O", "0").replace("o", "0")
    text = text.replace("g", "").replace("G", "").replace("q", "")
    return re.sub(r"[^0-9.,]", "", text)


def parse_weight_text(raw_text: str, ocr_score: float = 0.0) -> ParseResult:
    """Extract XX.XX style weight from OCR text.

    Accepts forms like 23.79, 23,79, 2379 (implied two decimals), 23.7, 0.00.
    Rejects mixed alphanumerics such as 61F2 / AL22.
    Leading minus is detected as negative_display (weight=None, negative=True).
    """
    stripped = raw_text.strip().replace(" ", "")
    without_unit = re.sub(r"[gGq]", "", stripped)
    # Leading minus / en-dash / fullwidth minus → negative display.
    leading_minus = bool(re.match(r"^[-−–—]", without_unit))
    # Letter junk (beyond unit) → unreadable, do not salvage partial digits.
    # Allow a single leading minus before digits.
    body = without_unit[1:] if leading_minus else without_unit
    if re.search(r"[A-Za-z]", body):
        return ParseResult(None, 0.0, raw_text, [], negative=False)

    text = _normalize_text(raw_text)
    if not text:
        return ParseResult(None, 0.0, raw_text, [], negative=False)

    # Prefer explicit decimal.
    m = re.search(r"(?<!\d)(\d{1,2})[.,](\d{1,2})(?!\d)", text)
    if m:
        whole, frac = m.group(1), m.group(2)
        if len(frac) == 1:
            frac = frac + "0"
        value = float(f"{whole}.{frac[:2]}")
        if 0.0 <= value <= 80.0:
            if leading_minus:
                return ParseResult(
                    None,
                    float(ocr_score),
                    raw_text,
                    list(f"{whole}.{frac[:2]}"),
                    negative=True,
                )
            return ParseResult(
                round(value, 2),
                float(ocr_score),
                raw_text,
                list(f"{whole}.{frac[:2]}"),
            )

    # Compact 3–4 digit reading: 2379 -> 23.79. Whole cleaned string only.
    m2 = re.fullmatch(r"(\d{3,4})", text)
    if m2:
        digits = m2.group(1)
        if len(digits) == 4:
            value = float(f"{digits[:2]}.{digits[2:]}")
        else:
            value = float(f"{digits[0]}.{digits[1:]}")
        if 0.0 <= value <= 80.0:
            if leading_minus:
                return ParseResult(None, float(ocr_score) * 0.95, raw_text, list(digits), negative=True)
            return ParseResult(round(value, 2), float(ocr_score) * 0.95, raw_text, list(digits))

    # Integer-only empty / whole grams.
    m3 = re.fullmatch(r"(\d{1,2})", text)
    if m3:
        value = float(m3.group(1))
        if 0.0 <= value <= 80.0:
            if leading_minus:
                return ParseResult(None, float(ocr_score) * 0.75, raw_text, list(m3.group(1)), negative=True)
            return ParseResult(
                round(value, 2),
                float(ocr_score) * 0.75,
                raw_text,
                list(m3.group(1)),
            )

    return ParseResult(None, 0.0, raw_text, [], negative=False)
