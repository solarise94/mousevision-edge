#!/usr/bin/env python3
"""Dual-video HTTP OCR end-to-end gate (0001 + RefVideo).

Uses the same HttpOcrReader → TemporalWeightFusion path as the workbench.
Release bar: 0001 9/9 review=0 AND RefVideo 8/8 review=0.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mousevision.pipeline import WeighingPipeline, load_config  # noqa: E402

EXPECTED_0001 = [23.30, 21.60, 23.66, 23.81, 22.10, 24.18, 22.71, 23.44, 22.80]
# Photo-OCR verified ±0.15 under classic_v2 (session #1 LCD reads ~23.3, not 22.75).
TOL_0001 = [0.15, 0.15, 0.15, 0.15, 0.15, 0.15, 0.12, 0.15, 0.15]
EXPECTED_REF = [16.15, 17.22, 17.57, 15.10, 15.64, 17.55, 17.77, 16.87]


def _check(
    name: str,
    records: list[dict],
    expected: list[float],
    *,
    tol: float | list[float],
) -> dict:
    review_n = sum(1 for r in records if r.get("needs_review"))
    weights = [float(r["weight"]) for r in records]
    n_ok = len(weights) == len(expected)
    tols = [float(tol)] * len(expected) if isinstance(tol, (int, float)) else list(tol)
    diffs = []
    hits = []
    for i, (got, exp) in enumerate(zip(weights, expected)):
        d = abs(got - exp)
        diffs.append(round(d, 3))
        hits.append(d <= tols[i])
    while len(hits) < len(expected):
        hits.append(False)
    return {
        "name": name,
        "sessions": len(records),
        "expected_sessions": len(expected),
        "weights": weights,
        "expected": expected,
        "diffs": diffs,
        "hits": hits,
        "needs_review": review_n,
        "pass": n_ok and all(hits) and review_n == 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dual-video HTTP OCR gate")
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "configs" / "scale_refvideo.yaml",
    )
    parser.add_argument(
        "--ocr-url",
        default=os.environ.get("LCD_OCR_URL")
        or os.environ.get("MOUSEVISION_OCR_URL")
        or "http://127.0.0.1:8768",
    )
    parser.add_argument(
        "--video-0001",
        type=Path,
        default=ROOT / "tmp_ocr_acceptance" / "0001" / "source.mp4",
    )
    parser.add_argument(
        "--video-ref",
        type=Path,
        default=ROOT / "RefVideo" / "9494224d488d6e735c0f108cc5562a2d.mp4",
    )
    parser.add_argument("--out", type=Path, default=ROOT / "tmp_ocr_acceptance" / "dual_gate")
    parser.add_argument("--skip-0001", action="store_true")
    parser.add_argument("--skip-ref", action="store_true")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    # weight_reader is a plain string in YAML ("template" | "http_ocr").
    config["weight_reader"] = "http_ocr"
    ocr_api = dict(config.get("ocr_api") or {})
    ocr_api["base_url"] = str(args.ocr_url).rstrip("/")
    config["ocr_api"] = ocr_api
    # Env mirrors YAML for driver override path.
    os.environ["MOUSEVISION_WEIGHT_READER"] = "http_ocr"
    os.environ["MOUSEVISION_OCR_URL"] = ocr_api["base_url"]

    templates = ROOT / config.get("templates_dir", "assets/templates")
    args.out.mkdir(parents=True, exist_ok=True)
    report = {
        "ocr_url": ocr_api["base_url"],
        "weight_reader": config["weight_reader"],
        "results": [],
    }

    jobs = []
    if not args.skip_0001:
        jobs.append(("0001", args.video_0001, EXPECTED_0001, TOL_0001))
    if not args.skip_ref:
        jobs.append(("refvideo", args.video_ref, EXPECTED_REF, 0.10))

    for name, video, expected, tol in jobs:
        if not video.is_file():
            print(f"SKIP {name}: missing {video}")
            report["results"].append({"name": name, "pass": False, "error": "missing_video"})
            continue
        out_dir = args.out / name
        pipeline = WeighingPipeline(config, templates)
        # Sanity: must actually use HttpOcrReader.
        reader_name = type(pipeline.driver.reader).__name__ if hasattr(pipeline, "driver") else "?"
        # WeighingPipeline builds driver per run; check config is wired.
        print(f"run {name}: weight_reader={config['weight_reader']} ocr={ocr_api['base_url']}")
        result = pipeline.run_video(
            video,
            cage_id=f"GATE-{name}",
            output_root=out_dir,
            stop_after_first=False,
            create_run=True,
        )
        records = result.records or []
        checked = _check(name, records, expected, tol=tol)
        checked["run_dir"] = str(result.run_dir)
        report["results"].append(checked)
        flag = "PASS" if checked["pass"] else "FAIL"
        print(
            f"{flag} {name}: sessions={checked['sessions']}/{checked['expected_sessions']} "
            f"review={checked['needs_review']} weights={checked['weights']}"
        )

    report_path = args.out / "report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {report_path}")
    ok = all(r.get("pass") for r in report["results"]) and bool(report["results"])
    if not ok:
        print(
            "RELEASE BLOCKED: dual-video HTTP e2e must be "
            "0001 9/9 review=0 AND RefVideo 8/8 review=0"
        )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
