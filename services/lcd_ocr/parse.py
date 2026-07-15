"""Parse OCR raw text into a scale weight in grams."""

from __future__ import annotations

import re
from dataclasses import dataclass


_NUM_RE = re.compile(r"(?<!\d)(\d{1,2})[.,]?(\d{0,2})(?!\d)")
_COMPACT_RE = re.compile(r"(?<!\d)(\d{3,4})(?!\d)")


@dataclass
class ParseResult:
    weight: float | None
    confidence: float
    raw_text: str
    digits: list[str]


def _normalize_text(raw: str) -> str:
    text = raw.strip().replace(" ", "").replace("O", "0").replace("o", "0")
    text = text.replace("g", "").replace("G", "").replace("q", "")
    return text


def parse_weight_text(raw_text: str, ocr_score: float = 0.0) -> ParseResult:
    """Extract XX.XX style weight from OCR text.

    Accepts forms like 23.79, 23,79, 2379 (implied two decimals), 23.7.
    """
    text = _normalize_text(raw_text)
    if not text:
        return ParseResult(None, 0.0, raw_text, [])

    # Prefer explicit decimal.
    m = re.search(r"(?<!\d)(\d{1,2})[.,](\d{1,2})(?!\d)", text)
    if m:
        whole, frac = m.group(1), m.group(2)
        if len(frac) == 1:
            frac = frac + "0"
        value = float(f"{whole}.{frac[:2]}")
        if 0.0 <= value <= 80.0:
            return ParseResult(round(value, 2), float(ocr_score), raw_text, list(f"{whole}.{frac[:2]}"))

    # Compact 3–4 digit reading: 2379 -> 23.79, 238 -> 2.38 or 23.8?
    # Scale LCD is typically 4 digits with 2 decimals (XX.XX).
    m2 = _COMPACT_RE.search(text)
    if m2:
        digits = m2.group(1)
        if len(digits) == 4:
            value = float(f"{digits[:2]}.{digits[2:]}")
        else:  # 3 digits -> X.XX
            value = float(f"{digits[0]}.{digits[1:]}")
        if 0.0 <= value <= 80.0:
            return ParseResult(round(value, 2), float(ocr_score) * 0.95, raw_text, list(digits))

    # Integer-only (rare empty scale "0" / "00").
    m3 = re.search(r"(?<!\d)(\d{1,2})(?!\d)", text)
    if m3:
        value = float(m3.group(1))
        if 0.0 <= value <= 80.0:
            return ParseResult(round(value, 2), float(ocr_score) * 0.8, raw_text, list(m3.group(1)))

    return ParseResult(None, 0.0, raw_text, [])
