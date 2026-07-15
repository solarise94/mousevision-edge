"""Acceptance gate: classic seven-seg on 0001 critical frames.

Loads the same scale profile as production (`configs/scale_refvideo.yaml`).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import LcdOcrEngine  # noqa: E402
from profile import load_scale_profile  # noqa: E402


# (path relative to sample root, expected_g, tolerance, critical)
CASES = [
    ("mouse_004_photo.jpg", 23.79, 1.0, True),
    ("scan5/t46500_24.18.jpg", 24.18, 0.8, True),
    ("scan5/t48300_24.14.jpg", 24.14, 0.8, True),
    ("mouse_003_photo.jpg", 23.66, 1.0, False),
    ("mouse_006_photo.jpg", 23.47, 1.0, False),
    ("frames/m4_t32300.jpg", 23.79, 1.0, True),
    # Session #2 regression: photo clearly shows 21.60; was misread 22.58 / 22.60
    # when narrow "1" bloomed into a seven-seg "2".
    ("frames/m2_photo_21.60.jpg", 21.60, 0.35, True),
]


def _default_fixture_root() -> Path:
    return Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "lcd_ocr" / "0001"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="0001 LCD OCR acceptance gate")
    parser.add_argument(
        "root",
        nargs="?",
        default=None,
        help="directory with acceptance frames "
        f"(default: {_default_fixture_root()})",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="path to scale YAML/JSON (default: configs/scale_refvideo.yaml)",
    )
    args = parser.parse_args(argv)

    profile = load_scale_profile(args.profile)
    eng = LcdOcrEngine(scale_profile=profile)
    print(
        f"device={eng.device} model={eng.model_version} "
        f"profile={eng.scale_profile_name} "
        f"digit_roi={list(eng.norm_cfg.digit_roi)} ink_trim={eng.norm_cfg.ink_trim}"
    )

    root = Path(args.root) if args.root else _default_fixture_root()
    if not root.is_absolute():
        cand = Path.cwd() / root
        root = cand if cand.is_dir() else (Path(__file__).resolve().parents[2] / root)
    ok = fail = 0
    critical_ok = True
    for rel, exp, tol, critical in CASES:
        path = root / rel
        if not path.is_file():
            print(f"SKIP {rel}: missing")
            if critical:
                critical_ok = False
            continue
        img = cv2.imread(str(path))
        r = eng.read(img, return_debug=True)
        hit = r.weight is not None and abs(float(r.weight) - exp) <= tol
        status = "PASS" if hit else "FAIL"
        if hit:
            ok += 1
        else:
            fail += 1
            if critical:
                critical_ok = False
        lat = r.latency.total_ms
        print(
            f"{status} {rel}: weight={r.weight} status={r.status} conf={r.confidence:.3f} "
            f"digits={r.digits} expect≈{exp}±{tol} latency={lat:.0f}ms"
        )
    print(
        json.dumps(
            {
                "pass": ok,
                "fail": fail,
                "device": eng.device,
                "model": eng.model_version,
                "digit_roi": list(eng.norm_cfg.digit_roi),
            },
            ensure_ascii=False,
        )
    )
    if not critical_ok:
        print("GATE FAIL: critical frames not stable — do not wire main pipeline yet")
        return 2
    print("GATE PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
