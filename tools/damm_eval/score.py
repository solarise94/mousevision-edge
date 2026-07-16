"""Score DAMM predictions against manual labels.

Labels CSV format: file,mouse_gt,glove_gt  (1/0 for presence)
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description="Score DAMM eval")
    ap.add_argument("--predictions", required=True, type=Path)
    ap.add_argument("--labels", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()

    preds = json.loads(args.predictions.read_text())
    labels: dict[str, dict] = {}
    with args.labels.open() as f:
        for row in csv.DictReader(f):
            labels[row["file"]] = {
                "mouse_gt": int(row.get("mouse_gt", 0)),
                "glove_gt": int(row.get("glove_gt", 0)),
            }

    tp = fp_mouse = false_neg = 0
    for p in preds:
        fname = p["file"]
        gt = labels.get(fname, {})
        has_mouse_gt = bool(gt.get("mouse_gt", 0))
        has_glove_gt = bool(gt.get("glove_gt", 0))
        has_pred = p["n_detections"] > 0
        if has_pred and has_mouse_gt:
            tp += 1
        elif has_pred and has_glove_gt and not has_mouse_gt:
            fp_mouse += 1
        elif not has_pred and has_mouse_gt:
            false_neg += 1

    n = max(1, len(preds))
    report = {
        "total_frames": len(preds),
        "labeled_frames": len(labels),
        "true_positive": tp,
        "false_positive_glove": fp_mouse,
        "false_negative": false_neg,
        "mouse_detection_rate": round(tp / max(1, tp + false_neg), 3),
        "glove_false_positive_rate": round(fp_mouse / n, 3),
    }
    args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
