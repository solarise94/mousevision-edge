"""Shared scale-profile loading for lcd-ocr engine and acceptance gates."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def default_scale_profile() -> dict[str, Any]:
    return {
        "scale_profile": "current_scale_v1",
        "lcd_normalization": {
            "width": 480,
            "height": 128,
            # TemplateReader-compatible digit band on LCD crop (excludes '+' / 'g').
            "digit_roi": [0.20, 0.08, 0.66, 0.84],
            "digit_slots": 4,
            "slot_margin": 0.03,
            "ink_trim": True,
            "ink_trim_pad": 0.05,
            "slot_mode": "projected",
            "allow_warp": False,
            "skew_warp_min": 8.0,
            "skew_warp_threshold": 35.0,
            "min_locator_confidence": 0.55,
        },
        "lcd_detect": {
            "hsv_low": [90, 40, 80],
            "hsv_high": [130, 255, 255],
            "min_area": 8000,
            "min_width": 150,
            "min_height": 40,
        },
        "weight_roi": {
            "x": 145,
            "y": 780,
            "w": 430,
            "h": 110,
        },
    }


def load_scale_profile(path: str | Path | None = None) -> dict[str, Any]:
    """Load profile from YAML / JSON / env; always merges over defaults."""
    profile = default_scale_profile()

    env_json = os.environ.get("LCD_OCR_SCALE_PROFILE_JSON", "").strip()
    if env_json:
        try:
            profile = _merge(profile, json.loads(env_json))
        except json.JSONDecodeError:
            pass

    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    env_path = os.environ.get("LCD_OCR_SCALE_PROFILE", "").strip()
    if env_path:
        candidates.append(Path(env_path))

    here = Path(__file__).resolve()
    # Container image: /app/configs/...
    candidates.append(here.parent / "configs" / "scale_refvideo.yaml")
    # Repo checkout: services/lcd_ocr → configs/scale_refvideo.yaml
    if len(here.parents) > 2:
        candidates.append(here.parents[2] / "configs" / "scale_refvideo.yaml")
    candidates.append(Path.cwd() / "configs" / "scale_refvideo.yaml")

    for cand in candidates:
        if not cand.is_file():
            continue
        data = _read_config_file(cand)
        if data:
            return _merge(profile, data)
    return profile


def _read_config_file(path: Path) -> dict[str, Any] | None:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError:
            return None
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else None
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    return None


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out
