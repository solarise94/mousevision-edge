"""Unit tests for lcd-ocr text → weight parsing."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "lcd_ocr"))

from parse import parse_weight_text  # noqa: E402


def test_parse_decimal():
    r = parse_weight_text("23.79 g", 0.9)
    assert r.weight == 23.79
    assert r.confidence >= 0.9


def test_parse_compact_four_digits():
    r = parse_weight_text("2379", 0.8)
    assert r.weight == 23.79


def test_parse_zero():
    r = parse_weight_text("0.00 g", 0.95)
    assert r.weight == 0.0


def test_parse_rejects_alphanumeric_junk():
    r = parse_weight_text("61F2 g", 0.9)
    assert r.weight is None
