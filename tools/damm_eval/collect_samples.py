"""Collect evaluation frames from training_assets for DAMM assessment.

Does NOT require detectron2 - just copies frames for manual labeling.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description="Collect frames for DAMM eval")
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--source", nargs="+", required=True, type=Path)
    ap.add_argument("--max-per-session", type=int, default=5)
    args = ap.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    idx = 0
    for src in args.source:
        jsonl = src / "ocr_observations.jsonl"
        if not jsonl.exists():
            for sub in sorted(src.glob("**/ocr_observations.jsonl")):
                jsonl = sub
                break
            if not jsonl.exists():
                continue
        # We don't have actual frame images in P1-e yet (only OCR observations).
        # This script is a placeholder: when P1-e is enhanced to save frame
        # crops, it will collect them here. For now, record metadata.
        for line in jsonl.read_text(encoding="utf-8").strip().splitlines():
            row = json.loads(line)
            if idx >= args.max_per_session * len(args.source):
                break
            manifest.append({
                "sample_id": idx,
                "source": str(jsonl.parent),
                "frame_index": row.get("frame_index"),
                "timestamp_ms": row.get("timestamp_ms"),
                "ocr_weight": row.get("weight"),
                "ocr_status": row.get("status"),
            })
            idx += 1

    out_manifest = args.output / "samples_manifest.json"
    out_manifest.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(f"Collected {len(manifest)} sample metadata entries -> {out_manifest}")
    print("Note: actual frame image collection requires enhanced P1-e with crop saving.")


if __name__ == "__main__":
    main()
