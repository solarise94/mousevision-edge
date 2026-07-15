"""Export warped LCD / digit-strip debug images for a frame."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "lcd_ocr"))

from engine import LcdOcrEngine  # noqa: E402
from locator import locate_screen  # noqa: E402
from normalize import NormalizeConfig, normalize_digit_strip  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("-o", "--out-dir", default="tmp_lcd_debug")
    args = parser.parse_args()

    cfg_path = ROOT / "configs" / "scale_refvideo.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    img = cv2.imread(args.image)
    if img is None:
        print("cannot read image", file=sys.stderr)
        return 2

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    eng = LcdOcrEngine(
        scale_profile={
            "lcd_normalization": cfg.get("lcd_normalization") or {},
            "lcd_detect": cfg.get("lcd_detect") or {},
            "weight_roi": cfg.get("weight_roi"),
        }
    )
    result = eng.read(img, return_debug=True)
    located = locate_screen(
        img,
        fixed_roi=cfg.get("weight_roi"),
        hsv_low=tuple((cfg.get("lcd_detect") or {}).get("hsv_low", [90, 40, 80])),
        hsv_high=tuple((cfg.get("lcd_detect") or {}).get("hsv_high", [130, 255, 255])),
    )
    if located is not None:
        norm = cfg.get("lcd_normalization") or {}
        ncfg = NormalizeConfig(
            width=int(norm.get("width", 480)),
            height=int(norm.get("height", 128)),
            digit_roi=tuple(norm.get("digit_roi", [0.18, 0.10, 0.70, 0.80])),
        )
        warped, strip, slots, method = normalize_digit_strip(img, located.screen_quad, ncfg)
        cv2.imwrite(str(out / "warped.png"), warped)
        cv2.imwrite(str(out / "digit_strip.png"), strip)
        for i, slot in enumerate(slots):
            cv2.imwrite(str(out / f"slot_{i}.png"), slot)
        vis = img.copy()
        pts = [(int(x), int(y)) for x, y in located.screen_quad]
        for a, b in zip(pts, pts[1:] + pts[:1]):
            cv2.line(vis, a, b, (0, 255, 0), 2)
        cv2.imwrite(str(out / "quad_overlay.png"), vis)
        (out / "screen_method.txt").write_text(method + "\n", encoding="utf-8")

    meta = result.to_api_dict()
    (out / "result.json").write_text(
        __import__("json").dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"wrote {out} weight={result.weight} status={result.status} digits={result.digits}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
