#!/usr/bin/env python3
"""Offline A/B compare of LCD digit decoders on fixture frames.

Uses the full LcdOcrEngine path (locate → normalize → vote) so results match
acceptance gates; per-strip CLAHE-only probes are available via --strip-only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "lcd_ocr"))

from decoders import available_decoders, get_decoder  # noqa: E402
from engine import LcdOcrEngine  # noqa: E402
from locator import locate_screen  # noqa: E402
from normalize import strip_slot_candidates  # noqa: E402
from profile import load_scale_profile  # noqa: E402


def _cases_from_manifest(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.get("cases") or data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare LCD OCR decoders")
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=ROOT / "tests" / "fixtures" / "lcd_ocr",
    )
    parser.add_argument("--decoders", default="classic_v2,segodec,ssocr")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--strip-only",
        action="store_true",
        help="Decode a single CLAHE strip only (no majority vote)",
    )
    args = parser.parse_args(argv)

    profile = load_scale_profile(args.profile)
    names = [n.strip() for n in args.decoders.split(",") if n.strip()]

    cases: list[dict] = []
    for subset in ("0001", "refvideo"):
        man = args.fixtures / subset / "manifest.json"
        if man.is_file():
            for c in _cases_from_manifest(man):
                cases.append({**c, "subset": subset, "root": str(args.fixtures / subset)})

    if not cases:
        print("No fixture manifests found; nothing to compare")
        return 1

    rows = []
    for case in cases:
        rel = case["path"]
        img_path = Path(case["root"]) / rel
        if not img_path.is_file():
            rows.append({"path": rel, "error": "missing"})
            continue
        img = cv2.imread(str(img_path))
        entry = {
            "path": rel,
            "subset": case.get("subset"),
            "expect": case.get("expect"),
            "tol": case.get("tol", 0.15),
            "decoders": {},
        }
        for name in names:
            os.environ["LCD_OCR_DECODER"] = name
            try:
                dec = get_decoder(name)
            except Exception as exc:  # noqa: BLE001
                entry["decoders"][name] = {"error": str(exc)}
                continue
            if name == "ssocr" and not getattr(dec, "available", True):
                entry["decoders"][name] = {"error": "ssocr_not_installed", "hit": False}
                continue

            t0 = time.perf_counter()
            if args.strip_only:
                eng = LcdOcrEngine(scale_profile=profile)
                located = locate_screen(
                    img,
                    fixed_roi=eng.fixed_roi,
                    hsv_low=eng.hsv_low,  # type: ignore[arg-type]
                    hsv_high=eng.hsv_high,  # type: ignore[arg-type]
                    min_area=eng.min_area,
                    min_width=eng.min_width,
                    min_height=eng.min_height,
                    min_locator_confidence=eng.min_locator_confidence,
                )
                if located is None:
                    entry["decoders"][name] = {"error": "lcd_not_found", "hit": False}
                    continue
                _w, _m, variants = strip_slot_candidates(
                    img, located.screen_quad, eng.norm_cfg
                )
                usable = [t for t in variants if len(t[2]) == 4]
                if not usable:
                    entry["decoders"][name] = {"error": "no_slots", "hit": False}
                    continue
                _lab, strip, slots = next((t for t in usable if t[0] == "clahe"), usable[0])
                result = dec.read(strip, slots)
                weight, status, digits, quality = (
                    result.weight,
                    result.status,
                    result.digits,
                    result.quality,
                )
            else:
                eng = LcdOcrEngine(scale_profile=profile)
                r = eng.read(img)
                weight, status, digits, quality = r.weight, r.status, r.digits, r.quality
            ms = (time.perf_counter() - t0) * 1000.0

            expect = case.get("expect")
            kind = case.get("kind", "")
            if case.get("status") == "zero_display" or kind == "empty":
                hit = status == "zero_display" or (
                    weight is not None and abs(float(weight)) <= 0.05
                )
            elif case.get("status") == "transition" or kind == "transition":
                bad = (
                    status == "readable"
                    and weight is not None
                    and 10.5 <= float(weight) <= 12.5
                )
                hit = not bad
            elif expect is None:
                hit = True
            else:
                hit = (
                    weight is not None
                    and abs(float(weight) - float(expect)) <= float(entry["tol"])
                )
            entry["decoders"][name] = {
                "weight": weight,
                "status": status,
                "digits": digits,
                "quality": round(float(quality), 3),
                "latency_ms": round(ms, 2),
                "hit": bool(hit),
            }
        rows.append(entry)
        bits = " ".join(
            f"{n}={'Y' if d.get('hit') else 'N'}({d.get('weight')}/{d.get('status') or d.get('error')})"
            for n, d in entry["decoders"].items()
        )
        print(f"{rel}: expect={entry['expect']} {bits}")

    summary = {"available": available_decoders(), "rows": rows}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
