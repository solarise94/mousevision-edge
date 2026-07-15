"""Phase B offline compare: template vs warped fixed-slot classic seven-seg.

Usage:
  python tools/compare_stage_b.py path/to/frame.jpg [more.jpg ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "lcd_ocr"))
sys.path.insert(0, str(ROOT))

from engine import LcdOcrEngine  # noqa: E402
from mousevision.reader.template import TemplateReader  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: compare_stage_b.py <image> [image...]", file=sys.stderr)
        return 2

    cfg_path = ROOT / "configs" / "scale_refvideo.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    templates = ROOT / str(cfg.get("templates_dir", "assets/templates"))
    template = TemplateReader(
        templates if templates.exists() else None,
        match_threshold=float(cfg.get("match_threshold", 0.5)),
        min_digit_confidence=float(cfg.get("min_digit_confidence", 0.45)),
        lcd_detect=cfg.get("lcd_detect"),
        weight_roi=cfg.get("weight_roi"),
    )
    classic = LcdOcrEngine(
        scale_profile={
            "lcd_normalization": cfg.get("lcd_normalization") or {},
            "lcd_detect": cfg.get("lcd_detect") or {},
            "weight_roi": cfg.get("weight_roi"),
        }
    )

    rows = []
    for path_s in sys.argv[1:]:
        path = Path(path_s)
        img = cv2.imread(str(path))
        if img is None:
            rows.append({"file": path.name, "error": "unreadable"})
            continue
        tw, tc = template.read_weight(img)
        cr = classic.read(img, return_debug=True)
        rows.append(
            {
                "file": path.name,
                "template_weight": tw,
                "template_conf": round(float(tc), 3),
                "classic_weight": cr.weight,
                "classic_status": cr.status,
                "classic_digits": cr.digits,
                "classic_conf": round(float(cr.confidence), 3),
                "locator": cr.locator,
                "latency_ms": cr.latency.to_dict(),
            }
        )
        print(
            f"{path.name}: template={tw} classic={cr.weight} "
            f"status={cr.status} digits={cr.digits}"
        )

    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
