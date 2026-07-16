#!/usr/bin/env python3
"""RefVideo single-frame LCD OCR acceptance (strict).

Rules:
- platform: must hit expected weight
- empty: must be zero_display (weight ~0)
- transition / hard: must be transition | unreadable | zero_display | bad_roi
  — any non-zero readable is FAIL
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


_REJECT_STATUSES = {"transition", "unreadable", "zero_display", "bad_roi"}


def _default_root() -> Path:
    return Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "lcd_ocr" / "refvideo"


def _is_reject_ok(status: str, weight: float | None) -> bool:
    if status not in _REJECT_STATUSES:
        return False
    if status == "zero_display":
        return weight is None or abs(float(weight)) <= 0.05
    return weight is None or abs(float(weight or 0.0)) <= 0.05 or status != "readable"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RefVideo LCD OCR acceptance (strict)")
    parser.add_argument("root", nargs="?", default=None)
    parser.add_argument("--profile", default=None)
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else _default_root()
    man = root / "manifest.json"
    if not man.is_file():
        print(f"missing manifest: {man}")
        return 2

    data = json.loads(man.read_text(encoding="utf-8"))
    cases = list(data.get("cases") or [])
    profile = load_scale_profile(args.profile)
    eng = LcdOcrEngine(scale_profile=profile)
    print(
        f"device={eng.device} model={eng.model_version} decoder={eng.decoder_name} "
        f"profile={eng.scale_profile_name}"
    )

    ok = fail = skip = 0
    critical_ok = True
    for case in cases:
        rel = case["path"]
        path = root / rel
        if not path.is_file():
            print(f"SKIP {rel}: missing")
            skip += 1
            if case.get("critical", True):
                critical_ok = False
            continue
        img = cv2.imread(str(path))
        r = eng.read(img, return_debug=True)
        expect = case.get("expect")
        tol = float(case.get("tol", 0.15))
        kind = str(case.get("kind") or "")
        want_status = case.get("status")

        if kind == "platform" or (expect is not None and kind not in {"empty", "transition", "hard_near11"}):
            hit = (
                r.status == "readable"
                and r.weight is not None
                and abs(float(r.weight) - float(expect)) <= tol
            )
            critical = bool(case.get("critical", True))
        elif kind == "empty" or want_status == "zero_display":
            hit = r.status == "zero_display" and (
                r.weight is None or abs(float(r.weight)) <= 0.05
            )
            critical = bool(case.get("critical", True))
        elif kind in {"transition", "hard_near11"} or want_status == "transition":
            # Strict: no non-zero readable garbage.
            hit = r.status in _REJECT_STATUSES and not (
                r.status == "readable" and r.weight is not None and float(r.weight) > 0.05
            )
            # zero_display / transition / unreadable / bad_roi only
            if r.status == "readable":
                hit = False
            critical = bool(case.get("critical", True))
        else:
            hit = False
            critical = True

        status = "PASS" if hit else "FAIL"
        if hit:
            ok += 1
        else:
            fail += 1
            if critical:
                critical_ok = False
        print(
            f"{status} {rel}: weight={r.weight} status={r.status} digits={r.digits} "
            f"expect={expect} kind={kind} latency={r.latency.total_ms:.0f}ms"
        )

    print(
        json.dumps(
            {
                "pass": ok,
                "fail": fail,
                "skip": skip,
                "decoder": eng.decoder_name,
                "model": eng.model_version,
                "strict": True,
            },
            ensure_ascii=False,
        )
    )
    return 0 if critical_ok and fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
