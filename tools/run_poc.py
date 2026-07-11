"""Run MouseVision Edge PoC on a reference video."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mousevision.pipeline import WeighingPipeline, load_config


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="MouseVision Edge video PoC")
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--cage-id", "--box-id", dest="cage_id", type=str, default="C57-023")
    parser.add_argument("--out", type=Path, default=Path("output"))
    parser.add_argument("--templates", type=Path, default=None)
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process all weighing sessions in the video (not just the first)",
    )
    parser.add_argument(
        "--no-run-dir",
        action="store_true",
        help="Write directly into --out without creating a run_* subdirectory",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    templates = args.templates
    if templates is None:
        templates = Path(config.get("templates_dir", "assets/templates"))
    if not templates.is_absolute():
        templates = Path.cwd() / templates

    pipeline = WeighingPipeline(config, templates)
    result = pipeline.run_video(
        args.video,
        cage_id=args.cage_id,
        output_root=args.out,
        stop_after_first=not args.all,
        create_run=not args.no_run_dir,
    )
    print("states:", " -> ".join(result.states))
    print(f"samples={result.samples} readable={result.readable}")
    print(f"run_dir={result.run_dir} run_id={result.run_id}")
    records = result.records or []
    if not records:
        print("FAIL: no weighing sessions detected")
        raise SystemExit(1)
    if args.all:
        print(f"sessions={len(records)} cage_id={args.cage_id}")
        for i, rec in enumerate(records, 1):
            hist = rec.get("state_history") or []
            print(
                f"  #{i:02d} ordinal={rec.get('ordinal')} cage={rec.get('cage_id')} "
                f"weight={rec.get('weight')} conf={rec.get('confidence')} "
                f"history_len={len(hist)} record_id={str(rec.get('record_id', ''))[:8]}"
            )
            if result.output_dirs:
                print(f"       dir={result.output_dirs[i - 1]}")
    else:
        print(f"output: {result.output_dir}")
        print(json.dumps(result.record, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
