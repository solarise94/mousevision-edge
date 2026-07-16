#!/usr/bin/env python3
"""Extract versioned RefVideo LCD OCR fixtures (platform / empty / hard cases)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "lcd_ocr"))

from mousevision.pipeline import load_config  # noqa: E402
from mousevision.reader.template import TemplateReader  # noqa: E402


REFVIDEO = ROOT / "RefVideo" / "9494224d488d6e735c0f108cc5562a2d.mp4"
EXPECTED = [16.15, 17.22, 17.57, 15.10, 15.64, 17.55, 17.77, 16.87]


def _nearest_expect(w: float) -> float | None:
    best = min(EXPECTED, key=lambda e: abs(e - w))
    return best if abs(best - w) <= 0.25 else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, default=REFVIDEO)
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "tests" / "fixtures" / "lcd_ocr" / "refvideo",
    )
    parser.add_argument("--stride", type=int, default=3)
    args = parser.parse_args(argv)

    if not args.video.is_file():
        print(f"missing video {args.video}")
        return 1

    cfg = load_config(ROOT / "configs" / "scale_refvideo.yaml")
    templates = ROOT / cfg.get("templates_dir", "assets/templates")
    reader = TemplateReader(
        templates,
        match_threshold=float(cfg.get("match_threshold", 0.45)),
        min_digit_confidence=float(cfg.get("min_digit_confidence", 0.40)),
        lcd_detect=cfg.get("lcd_detect"),
        weight_roi=cfg.get("weight_roi"),
    )

    out = args.out
    (out / "frames").mkdir(parents=True, exist_ok=True)
    (out / "hard").mkdir(parents=True, exist_ok=True)

    # Prefer PNG — JPEG can flip 15.10 → 11.10 on the classic path.
    def _save(path: Path, frame):
        path = path.with_suffix(".png")
        cv2.imwrite(str(path), frame)
        return path

    cap = cv2.VideoCapture(str(args.video))
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
    idx = 0
    # Collect stable plateaus: consecutive frames near same expect.
    plateaus: dict[float, list[tuple[int, float, any]]] = {e: [] for e in EXPECTED}
    zeros: list[tuple[int, any]] = []
    hard_11: list[tuple[int, float, any]] = []

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % args.stride != 0:
            idx += 1
            continue
        w, conf = reader.read_weight(frame)
        if w is not None and w <= 0.05 and conf >= 0.4:
            zeros.append((idx, frame.copy()))
        elif w is not None:
            exp = _nearest_expect(float(w))
            if exp is not None:
                plateaus[exp].append((idx, float(w), frame.copy()))
            # Capture suspicious near-11.x if template itself reads that.
            if 10.5 <= float(w) <= 12.0:
                hard_11.append((idx, float(w), frame.copy()))
        idx += 1
    cap.release()

    cases = []
    for exp in EXPECTED:
        samples = plateaus[exp]
        if not samples:
            print(f"WARN: no plateau for {exp}")
            continue
        # Take median-index sample for stability.
        samples.sort(key=lambda t: t[0])
        mid = samples[len(samples) // 2]
        name = f"frames/platform_{exp:.2f}_f{mid[0]:05d}.png"
        path = out / name
        cv2.imwrite(str(path), mid[2])
        cases.append(
            {
                "path": name,
                "expect": exp,
                "tol": 0.15,
                "kind": "platform",
                "critical": True,
                "frame_index": mid[0],
                "template_weight": mid[1],
            }
        )
        print(f"saved {name} template≈{mid[1]}")

    # Empty frames: first and mid zeros.
    if zeros:
        for tag, sample in (("early", zeros[0]), ("mid", zeros[len(zeros) // 2])):
            fi, fr = sample
            name = f"frames/empty_{tag}_f{fi:05d}.png"
            cv2.imwrite(str(out / name), fr)
            cases.append(
                {
                    "path": name,
                    "expect": 0.0,
                    "tol": 0.05,
                    "status": "zero_display",
                    "kind": "empty",
                    "critical": True,
                    "frame_index": fi,
                }
            )
            print(f"saved {name}")

    # Hard / transition candidates around session gaps (heuristic).
    # Save a few frames between plateaus if present in video index gaps.
    for i, (a, b) in enumerate(zip(EXPECTED, EXPECTED[1:])):
        sa, sb = plateaus[a], plateaus[b]
        if not sa or not sb:
            continue
        gap_lo = sa[-1][0]
        gap_hi = sb[0][0]
        mid_i = (gap_lo + gap_hi) // 2
        # Re-grab by seeking.
        cap = cv2.VideoCapture(str(args.video))
        cap.set(cv2.CAP_PROP_POS_FRAMES, mid_i)
        ok, fr = cap.read()
        cap.release()
        if not ok:
            continue
        name = f"frames/transition_{i:02d}_f{mid_i:05d}.png"
        cv2.imwrite(str(out / name), fr)
        cases.append(
            {
                "path": name,
                "expect": None,
                "status": "transition",
                "kind": "transition",
                "critical": False,
                "frame_index": mid_i,
                "note": "must not emit confident non-zero garbage like 11.11",
            }
        )
        print(f"saved {name}")

    for i, (fi, w, fr) in enumerate(hard_11[:4]):
        name = f"hard/near11_f{fi:05d}_{w:.2f}.png"
        cv2.imwrite(str(out / name), fr)
        cases.append(
            {
                "path": name,
                "expect": None,
                "kind": "hard_near11",
                "critical": False,
                "frame_index": fi,
                "template_weight": w,
            }
        )

    # 1/7 special: 17.xx platforms already cover; mark them.
    for c in cases:
        if c.get("expect") in {17.22, 17.57, 17.55, 17.77}:
            c["one_seven"] = True

    manifest = {
        "source_video": str(args.video.relative_to(ROOT)),
        "fps": fps,
        "expected_sessions": EXPECTED,
        "cases": cases,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (out / "README.md").write_text(
        "# LCD OCR fixtures (RefVideo)\n\n"
        "Version-controlled critical frames for HTTP OCR / decoder A/B.\n\n"
        f"Source: `{manifest['source_video']}`\n\n"
        "Expected sessions: "
        + " / ".join(f"{w:.2f}" for w in EXPECTED)
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {out / 'manifest.json'} cases={len(cases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
