"""Optional ssocr (auerswal/ssocr) subprocess adapter.

Does not vendor GPL sources. Requires `ssocr` on PATH or LCD_OCR_SSOCR_BIN.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from quality import assess_strip_quality
from sevenseg_classic import compose_weight

from .base import DecoderResult


def _find_ssocr() -> str | None:
    env = os.environ.get("LCD_OCR_SSOCR_BIN", "").strip()
    if env and Path(env).is_file():
        return env
    return shutil.which("ssocr")


class SsocrAdapter:
    name = "ssocr"

    def __init__(self, *, binary: str | None = None, digits: int = 4) -> None:
        self.binary = binary or _find_ssocr()
        self.digits = digits

    @property
    def available(self) -> bool:
        return bool(self.binary)

    def read(self, normalized_strip: Any, slot_patches: list[Any]) -> DecoderResult:
        if not self.binary:
            return DecoderResult(
                [],
                [],
                None,
                "unreadable",
                0.0,
                {"error": "ssocr_not_installed"},
            )

        q = assess_strip_quality(normalized_strip, slot_patches if slot_patches else None)
        if q.status == "zero_display":
            return DecoderResult(
                ["0"] * 4,
                [0.9] * 4,
                0.0,
                "zero_display",
                0.85,
                {"quality_gate": q.reason, **q.evidence},
            )
        if q.status in {"transition", "unreadable"}:
            return DecoderResult(
                ["invalid"] * 4,
                [0.0] * 4,
                None,
                q.status,
                0.0,
                {"quality_gate": q.reason, **q.evidence},
            )

        # ssocr works best on a high-contrast grayscale strip.
        if normalized_strip.ndim == 3:
            gray = cv2.cvtColor(normalized_strip, cv2.COLOR_BGR2GRAY)
        else:
            gray = normalized_strip

        with tempfile.TemporaryDirectory(prefix="ssocr_") as tmp:
            path = Path(tmp) / "strip.png"
            cv2.imwrite(str(path), gray)
            try:
                proc = subprocess.run(
                    [
                        self.binary,
                        "-d",
                        str(self.digits),
                        "-t",
                        "50",
                        "digit",
                        str(path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=2.0,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return DecoderResult(
                    [],
                    [],
                    None,
                    "unreadable",
                    0.0,
                    {"error": f"ssocr_failed:{exc}"},
                )

        raw = (proc.stdout or "").strip()
        evidence = {
            "quality_gate": q.reason if q.status != "ok" else "ok",
            "ssocr_raw": raw,
            "ssocr_rc": proc.returncode,
            "ssocr_stderr": (proc.stderr or "").strip()[:200],
            **q.evidence,
        }
        if proc.returncode != 0 or not raw:
            return DecoderResult([], [], None, "unreadable", 0.0, evidence)

        # Parse digits / spaces; keep first 4 numeric chars.
        cleaned = "".join(ch for ch in raw if ch.isdigit() or ch in {".", " "})
        nums = [ch for ch in cleaned if ch.isdigit()]
        if not nums:
            if "0" in raw or raw in {".", "-"}:
                return DecoderResult(
                    ["0"] * 4, [0.8] * 4, 0.0, "zero_display", 0.7, evidence
                )
            return DecoderResult([], [], None, "unreadable", 0.0, evidence)

        while len(nums) < 4:
            nums.insert(0, "blank")  # type: ignore[arg-type]
        nums = nums[-4:]
        # blank placeholders for leading missing digits:
        chars: list[str] = []
        for ch in nums:
            chars.append(ch if ch.isdigit() else "blank")
        while len(chars) < 4:
            chars.insert(0, "blank")
        chars = chars[-4:]
        confs = [0.80 if c.isdigit() else 0.70 for c in chars]

        weight = compose_weight(chars)
        if weight is None:
            return DecoderResult(chars, confs, None, "unreadable", 0.5, evidence)
        if weight <= 0.05:
            return DecoderResult(chars, confs, 0.0, "zero_display", 0.75, evidence)
        return DecoderResult(
            chars, confs, round(float(weight), 2), "readable", 0.80, evidence
        )
