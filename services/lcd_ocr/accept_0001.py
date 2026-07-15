"""Acceptance gate: run LCD OCR on 0001 critical frames.

Uses saved photos where the LCD is readable, plus extracted platform frames
for cases where the production photo was taken on a zero/transition reading.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import LcdOcrEngine  # noqa: E402


# (path relative to sample root, expected_g, tolerance)
CASES = [
    # Critical: TemplateReader had 8.38; LCD shows ~23.79
    ("mouse_004_photo.jpg", 23.79, 1.0, True),
    # Extracted platform frames where LCD actually shows ~24.1 (photo_005 is 0.00)
    ("scan5/t46500_24.18.jpg", 24.18, 0.8, True),
    ("scan5/t48300_24.14.jpg", 24.14, 0.8, True),
    # Already-correct photos (sanity)
    ("mouse_003_photo.jpg", 23.66, 1.0, False),
    ("mouse_006_photo.jpg", 23.47, 1.0, False),
    # Platform frame for mouse 4 near true reading
    ("frames/m4_t32300.jpg", 23.79, 1.0, True),
]


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "tmp_ocr_acceptance/0001")
    eng = LcdOcrEngine()
    print(f"device={eng.device} model={eng.model_name}")
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
        print(
            f"{status} {rel}: weight={r.weight} conf={r.confidence:.3f} "
            f"raw={r.raw_text!r} expect≈{exp}±{tol} latency={r.latency_ms:.0f}ms"
        )
    print(json.dumps({"pass": ok, "fail": fail, "device": eng.device}, ensure_ascii=False))
    if not critical_ok:
        print("GATE FAIL: critical frames not stable — do not wire main pipeline yet")
        return 2
    print("GATE PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
