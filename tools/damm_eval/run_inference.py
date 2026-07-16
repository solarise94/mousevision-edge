"""Run DAMM inference on collected frames.

Requires detectron2 + DAMM weights - run in isolated venv, NOT in main project.
This is a scaffold: adjust model config path and confidence threshold as needed.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description="DAMM inference scaffold")
    ap.add_argument("--weights", required=True, type=Path, help="DAMM model_final.pth")
    ap.add_argument("--input", required=True, type=Path, help="dir of sample frames")
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    ap.add_argument("--conf-threshold", type=float, default=0.5)
    args = ap.parse_args()

    try:
        import cv2  # noqa: F401
        from detectron2.config import get_cfg
        from detectron2.engine import DefaultPredictor
        from detectron2 import model_zoo
    except ImportError:
        print("ERROR: detectron2 not installed. Run in isolated venv:")
        print("  pip install detectron2 torch torchvision opencv-python-headless")
        raise SystemExit(1)

    cfg = get_cfg()
    # DAMM uses Mask R-CNN R50-FPN; adjust if DAMM specifies a different config.
    cfg.merge_from_file(model_zoo.get_config_file("COCO-InstanceSegmentation/mask_rcnn_R_50_FPN_3x.yaml"))
    cfg.MODEL.WEIGHTS = str(args.weights)
    cfg.MODEL.DEVICE = args.device
    cfg.MODEL.ROI_HEADS.SCORE_THRESH_TEST = args.conf_threshold
    # DAMM's custom class count (mouse=1 class) - adjust per DAMM README.
    cfg.MODEL.ROI_HEADS.NUM_CLASSES = 1
    predictor = DefaultPredictor(cfg)

    args.output.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    frames = sorted(args.input.glob("*.jpg")) + sorted(args.input.glob("*.png"))
    for fp in frames:
        import cv2
        img = cv2.imread(str(fp))
        if img is None:
            continue
        t0 = time.perf_counter()
        outputs = predictor(img)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        instances = outputs["instances"].to("cpu")
        boxes = instances.pred_boxes.tensor.tolist() if len(instances) else []
        scores = instances.scores.tolist() if len(instances) else []
        results.append({
            "file": fp.name,
            "latency_ms": round(latency_ms, 1),
            "n_detections": len(boxes),
            "boxes": [{"bbox": [round(c, 1) for c in box], "score": round(s, 3)} for box, s in zip(boxes, scores)],
        })

    out = args.output / "predictions.json"
    out.write_text(json.dumps(results, indent=2))
    latencies = [r["latency_ms"] for r in results]
    print(f"Inference done: {len(results)} frames -> {out}")
    if latencies:
        latencies.sort()
        p50 = latencies[len(latencies) // 2]
        p90 = latencies[int(len(latencies) * 0.9)]
        print(f"Latency p50={p50:.1f}ms p90={p90:.1f}ms")


if __name__ == "__main__":
    main()
