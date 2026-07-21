#!/usr/bin/env python3
"""CLI: run full-video agent weighing on a local mp4 (no job queue).

  MOUSEVISION_AGENT_API_KEY=... python tools/agent_weigh_video.py RefVideo/x.mp4
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mousevision.agent_weigh import AgentWeighClient, resolve_agent_config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("video", type=Path)
    ap.add_argument("--label", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--base-url", default=None)
    args = ap.parse_args()
    if not args.video.is_file():
        print("missing video", args.video, file=sys.stderr)
        return 2
    if args.base_url:
        os.environ["MOUSEVISION_AGENT_BASE_URL"] = args.base_url
    if args.model:
        os.environ["MOUSEVISION_AGENT_MODEL"] = args.model
    cfg = {"agent": resolve_agent_config({})}
    client = AgentWeighClient(cfg)
    label = args.label or args.video.name
    result = client.weigh_video(args.video, label=label)
    out = {
        "model": result.model,
        "input_mode": result.input_mode,
        "latency_s": result.latency_s,
        "prompt_version": result.prompt_version,
        "summary": result.summary,
        "sessions": [
            {
                "ordinal": s.ordinal,
                "weight_g": s.weight_g,
                "confidence": s.confidence,
                "note": s.note,
                "t_start_s": s.t_start_s,
                "t_end_s": s.t_end_s,
                "stable_start_s": s.stable_start_s,
                "stable_end_s": s.stable_end_s,
                "t_stable_s": s.t_stable_s,
                "reported_weight_g": s.reported_weight_g,
                "evidence_consensus_g": s.evidence_consensus_g,
                "review_reasons": s.review_reasons,
                "evidence": [
                    {
                        "timestamp_s": vote.timestamp_s,
                        "weight_g": vote.weight_g,
                        "mouse_present": vote.mouse_present,
                        "display_readable": vote.display_readable,
                    }
                    for vote in s.evidence
                ],
            }
            for s in result.sessions
        ],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
