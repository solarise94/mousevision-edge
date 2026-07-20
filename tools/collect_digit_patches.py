#!/usr/bin/env python3
"""Extract labeled 28×40 digit patches from weighing videos for CNN training.

Usage:
  python tools/collect_digit_patches.py VIDEO \\
      [--output-dir DIR] [--ground-truth JSON] [--stride N] \\
      [--agent] [--label-source agent|classic|consensus]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services" / "lcd_ocr"))

from locator import locate_screen  # noqa: E402
from normalize import (  # noqa: E402
    NormalizeConfig,
    crop_digit_strip,
    extract_digit_slots,
    ink_trim_strip,
    prepare_screen,
)
from decoders.classic_v2 import ClassicV2Decoder  # noqa: E402

LOG = logging.getLogger("collect_digit_patches")

PATCH_SIZE = (28, 40)  # width × height
MAJORITY_THRESHOLD = 0.60


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def weight_to_digits(w: float) -> list[str]:
    """Convert weight_g to 4 display slot labels (blank | 0-9).

    Scale shows XX.XX with an implicit decimal after slot 1.
    Leading zeros become ``blank`` (e.g. 5.28 → blank,5,2,8).
    """
    raw = f"{float(w):.2f}".replace(".", "")
    # Keep at most 4 chars; if larger (e.g. 123.45), take rightmost 4
    if len(raw) > 4:
        raw = raw[-4:]
    raw = raw.zfill(4)
    result: list[str] = []
    leading = True
    for d in raw:
        if leading and d == "0":
            result.append("blank")
        else:
            leading = False
            result.append(d)
    return result


def normalize_patch(patch: np.ndarray, size: tuple[int, int] = PATCH_SIZE) -> np.ndarray:
    """Resize to fit within size, center on zero-padded canvas, Otsu-binarize.

    Mirrors ``mousevision.reader.template._normalize_digit``.
    """
    tw, th = size
    if patch is None or patch.size == 0:
        return np.zeros((th, tw), dtype=np.uint8)

    if patch.ndim == 3:
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    else:
        gray = patch

    h, w = gray.shape[:2]
    if h < 1 or w < 1:
        return np.zeros((th, tw), dtype=np.uint8)

    scale = min(tw / w, th / h)
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    resized = cv2.resize(gray, (nw, nh), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((th, tw), dtype=np.uint8)
    y0 = (th - nh) // 2
    x0 = (tw - nw) // 2
    canvas[y0 : y0 + nh, x0 : x0 + nw] = resized
    _, binary = cv2.threshold(canvas, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if float(np.mean(binary)) > 127.0:
        binary = 255 - binary
    return binary


def majority_vote(
    votes: list[str],
    *,
    threshold: float = MAJORITY_THRESHOLD,
) -> tuple[str | None, float]:
    """Return (label, share) if share > threshold, else (None, share)."""
    if not votes:
        return None, 0.0
    counts = Counter(votes)
    label, n = counts.most_common(1)[0]
    share = n / len(votes)
    if share > threshold:
        return label, share
    return None, share


@dataclass
class SessionSpec:
    ordinal: int
    weight_g: float | None
    t_start_s: float
    t_end_s: float
    confidence: float | None = None
    note: str = ""


# ---------------------------------------------------------------------------
# Ground truth loading
# ---------------------------------------------------------------------------


def load_ground_truth_json(path: Path) -> list[SessionSpec]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("sessions") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"ground-truth JSON must contain a sessions list: {path}")

    sessions: list[SessionSpec] = []
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        t_start = row.get("t_start_s", row.get("start_s"))
        t_end = row.get("t_end_s", row.get("end_s"))
        if t_start is None or t_end is None:
            LOG.warning("session %s missing time window — skip", row.get("ordinal", i))
            continue
        t0, t1 = float(t_start), float(t_end)
        if t0 > t1:
            t0, t1 = t1, t0
        w = row.get("weight_g", row.get("weight"))
        weight = float(w) if w is not None else None
        sessions.append(
            SessionSpec(
                ordinal=int(row.get("ordinal") or i),
                weight_g=weight,
                t_start_s=t0,
                t_end_s=t1,
                confidence=float(row["confidence"]) if row.get("confidence") is not None else None,
                note=str(row.get("note") or ""),
            )
        )
    sessions.sort(key=lambda s: s.ordinal)
    return sessions


def run_agent_ground_truth(video: Path, *, model: str | None = None) -> list[SessionSpec]:
    from mousevision.agent_weigh import AgentWeighClient, resolve_agent_config

    cfg = {"agent": resolve_agent_config({})}
    if model:
        cfg["agent"]["model"] = model
    client = AgentWeighClient(cfg)
    LOG.info("running Agent VLM (%s) on %s …", model or "default", video)
    result = client.weigh_video(video, label=video.name)
    sessions: list[SessionSpec] = []
    for s in result.sessions:
        if s.t_start_s is None or s.t_end_s is None:
            LOG.warning(
                "agent session ordinal=%s missing t_start/t_end — skip",
                s.ordinal,
            )
            continue
        t0, t1 = float(s.t_start_s), float(s.t_end_s)
        if t0 > t1:
            t0, t1 = t1, t0
        sessions.append(
            SessionSpec(
                ordinal=int(s.ordinal),
                weight_g=float(s.weight_g) if s.weight_g is not None else None,
                t_start_s=t0,
                t_end_s=t1,
                confidence=float(s.confidence),
                note=s.note or "",
            )
        )
    sessions.sort(key=lambda s: s.ordinal)
    LOG.info("agent (%s) returned %d sessions with time windows", model or "default", len(sessions))
    return sessions


# Default cross-validation models (all via CPA service)
# All support native video input:
#   gemini-3-flash: native Gemini API (AgentWeighClient)
#   gpt-5.6, kimi-k3: OpenAI-compatible API via CPA
DEFAULT_CV_MODELS = ["gemini-3-flash", "gpt-5.6", "kimi-k3"]
# Models that use the native Gemini API path
GEMINI_MODELS = {"gemini-3-flash"}
# Models that use OpenAI-compatible chat completions with video
OPENAI_VIDEO_MODELS = {"gpt-5.6", "kimi-k3"}


def _query_openai_video_model(
    model: str,
    video: Path,
) -> list[SessionSpec]:
    """Query an OpenAI-compatible model (via CPA) with a video file.

    Sends the video as base64 in a chat completion request and asks the model
    to identify all weighing sessions with timestamps and weights.
    """
    import base64
    import json as _json
    import urllib.request

    base_url = os.environ.get(
        "MOUSEVISION_AGENT_BASE_URL", "http://agent.invalid:46450"
    ).rstrip("/")
    api_key = os.environ.get("MOUSEVISION_AGENT_API_KEY") or os.environ.get("CPA_API_KEY", "")

    video_b64 = base64.b64encode(video.read_bytes()).decode()
    # Detect mime type
    suffix = video.suffix.lower()
    mime = {"mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime"}.get(
        suffix.lstrip("."), "video/mp4"
    )

    prompt = (
        "This video shows a laboratory mouse being weighed on a digital scale. "
        "The LCD display shows the weight in grams (format XX.XX). "
        "Identify ALL distinct weighing sessions in the video. A session starts when "
        "a mouse is placed on the scale and the reading stabilizes, and ends when "
        "the mouse is removed.\n\n"
        "For each session, report:\n"
        "- ordinal: session number (1, 2, 3, ...)\n"
        "- weight_g: the stable weight reading in grams\n"
        "- t_start_s: approximate start time in seconds\n"
        "- t_end_s: approximate end time in seconds\n"
        "- confidence: your confidence (0-1)\n\n"
        "Reply with ONLY a JSON object: {\"sessions\": [{\"ordinal\": 1, \"weight_g\": 23.59, "
        "\"t_start_s\": 3.2, \"t_end_s\": 7.8, \"confidence\": 0.95}, ...]}"
    )

    content = [
        {"type": "text", "text": prompt},
        {
            "type": "video_url",
            "video_url": {"url": f"data:{mime};base64,{video_b64}"},
        },
    ]

    payload = _json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 2000,
        "temperature": 0.0,
    }).encode()

    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )

    LOG.info("querying %s with video (%.1f MB) …", model, video.stat().st_size / 1e6)
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = _json.loads(resp.read())

    text = data["choices"][0]["message"]["content"].strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    obj = _json.loads(text)
    rows = obj.get("sessions", [])

    sessions: list[SessionSpec] = []
    for i, row in enumerate(rows, start=1):
        t_start = row.get("t_start_s")
        t_end = row.get("t_end_s")
        w = row.get("weight_g")
        if t_start is None or t_end is None:
            continue
        sessions.append(SessionSpec(
            ordinal=int(row.get("ordinal", i)),
            weight_g=float(w) if w is not None else None,
            t_start_s=float(t_start),
            t_end_s=float(t_end),
            confidence=float(row.get("confidence", 0.9)),
            note=f"model={model}",
        ))
    sessions.sort(key=lambda s: s.ordinal)
    LOG.info("  %s returned %d sessions", model, len(sessions))
    return sessions


def cross_validate_sessions(
    video: Path,
    models: list[str] | None = None,
    *,
    weight_tol: float = 0.5,
) -> list[SessionSpec]:
    """Run multiple VLM models on the full video and keep only consensus sessions.

    All models receive the complete video:
    - Gemini models: via native AgentWeighClient (Gemini API)
    - OpenAI-compatible models (gpt-5.6, kimi-k3): via CPA chat completions

    Agreement criteria:
    - Per-session weight within weight_tol grams across reporting models
    - Sessions confirmed by at least 2 models are kept

    Returns consensus sessions with averaged weights and boosted confidence.
    """
    models = models or DEFAULT_CV_MODELS
    all_results: dict[str, list[SessionSpec]] = {}

    for model in models:
        try:
            if model in GEMINI_MODELS:
                sessions = run_agent_ground_truth(video, model=model)
            else:
                sessions = _query_openai_video_model(model, video)
            all_results[model] = sessions
            LOG.info("  %s: %d sessions", model, len(sessions))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("  %s failed: %s — skipping", model, exc)

    if not all_results:
        raise RuntimeError("all cross-validation models failed")

    if len(all_results) == 1:
        LOG.warning("only 1 model succeeded — no cross-validation, using its output directly")
        return list(all_results.values())[0]

    # Use the model with the most sessions as reference (most complete)
    ref_model = max(all_results, key=lambda m: len(all_results[m]))
    ref_sessions = all_results[ref_model]
    LOG.info("reference model: %s (%d sessions)", ref_model, len(ref_sessions))

    consensus: list[SessionSpec] = []
    for ref_s in ref_sessions:
        weights: list[float] = []
        n_confirmed = 0

        # Match this session against all models by time overlap
        for model, sessions in all_results.items():
            if model == ref_model:
                if ref_s.weight_g is not None:
                    weights.append(ref_s.weight_g)
                    n_confirmed += 1
                continue
            best_match = None
            best_overlap = 0.0
            for s in sessions:
                overlap_start = max(ref_s.t_start_s, s.t_start_s)
                overlap_end = min(ref_s.t_end_s, s.t_end_s)
                overlap = max(0.0, overlap_end - overlap_start)
                ref_dur = max(0.1, ref_s.t_end_s - ref_s.t_start_s)
                if overlap / ref_dur > 0.3 and overlap > best_overlap:
                    best_overlap = overlap
                    best_match = s
            if best_match is not None and best_match.weight_g is not None:
                weights.append(best_match.weight_g)
                n_confirmed += 1

        # Decision: need at least 2 models to confirm
        if n_confirmed < 2:
            LOG.warning(
                "session %d: only %d model(s) confirmed — drop",
                ref_s.ordinal,
                n_confirmed,
            )
            continue

        w_spread = float(np.max(weights) - np.min(weights)) if len(weights) >= 2 else 0.0
        if w_spread > weight_tol:
            LOG.warning(
                "session %d: weight disagreement (spread=%.2fg > %.2fg, values=%s) — drop",
                ref_s.ordinal,
                w_spread,
                weight_tol,
                [round(w, 2) for w in weights],
            )
            continue

        w_mean = float(np.mean(weights))
        consensus.append(SessionSpec(
            ordinal=ref_s.ordinal,
            weight_g=round(w_mean, 2),
            t_start_s=ref_s.t_start_s,
            t_end_s=ref_s.t_end_s,
            confidence=min(0.99, 0.90 + 0.03 * n_confirmed),
            note=f"cross_val: {n_confirmed}/{len(all_results)} models, spread={w_spread:.2f}g",
        ))

    LOG.info(
        "cross-validation: %d/%d sessions kept (models: %s)",
        len(consensus),
        len(ref_sessions),
        list(all_results.keys()),
    )
    return consensus


# ---------------------------------------------------------------------------
# Frame → slots
# ---------------------------------------------------------------------------


def extract_frame_slots(
    frame: np.ndarray,
    ncfg: NormalizeConfig,
) -> tuple[list[np.ndarray] | None, np.ndarray | None, str | None]:
    """Locate LCD, normalize, return (4 slot patches, strip, error_reason)."""
    located = locate_screen(frame)
    if located is None or not located.screen_quad:
        return None, None, "lcd_not_found"

    screen, _method = prepare_screen(frame, located.screen_quad, ncfg)
    strip = crop_digit_strip(screen, ncfg)
    if ncfg.ink_trim:
        strip = ink_trim_strip(strip, pad=ncfg.ink_trim_pad)
    if strip is None or strip.size == 0:
        return None, None, "empty_strip"

    slots = extract_digit_slots(strip, ncfg)
    if len(slots) != ncfg.slot_count:
        return None, strip, f"slot_count={len(slots)}"
    for s in slots:
        if s is None or s.size == 0:
            return None, strip, "empty_slot"
    return slots, strip, None


def classic_digits_for_frame(
    strip: np.ndarray,
    slots: list[np.ndarray],
    decoder: ClassicV2Decoder,
) -> list[str] | None:
    """Run ClassicV2Decoder; return 4 digit chars or None if unusable."""
    result = decoder.read(strip, slots)
    digits = list(result.digits or [])
    # Zero-display path may return 3 glyphs — expand to 4 blanks/zeros for slots
    if result.status == "zero_display":
        if len(digits) == 3:
            return ["blank", "0", "0", "0"]
        if len(digits) == 4:
            return [str(d) for d in digits]
        return ["0", "0", "0", "0"]
    if result.status in {"transition", "unreadable"}:
        return None
    if len(digits) != 4:
        return None
    if any(d == "invalid" for d in digits):
        return None
    return [str(d) for d in digits]


# ---------------------------------------------------------------------------
# Session processing
# ---------------------------------------------------------------------------


def frame_indices_for_window(
    t_start_s: float,
    t_end_s: float,
    fps: float,
    total_frames: int,
    stride: int,
) -> list[int]:
    f0 = max(0, int(t_start_s * fps))
    f1 = min(total_frames - 1, int(t_end_s * fps))
    if f1 < f0:
        return []
    stride = max(1, int(stride))
    return list(range(f0, f1 + 1, stride))


def process_session(
    *,
    cap: cv2.VideoCapture,
    session: SessionSpec,
    session_idx: int,
    n_sessions: int,
    fps: float,
    total_frames: int,
    stride: int,
    label_source: str,
    ncfg: NormalizeConfig,
    decoder: ClassicV2Decoder,
    patches_dir: Path,
) -> dict[str, Any]:
    indices = frame_indices_for_window(
        session.t_start_s, session.t_end_s, fps, total_frames, stride
    )
    ordinal = session.ordinal
    skipped = 0
    frame_records: list[dict[str, Any]] = []
    # Per-slot vote lists for classic / consensus
    slot_votes: list[list[str]] = [[], [], [], []]

    for frame_idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ok, frame = cap.read()
        if not ok or frame is None:
            skipped += 1
            continue

        slots, strip, err = extract_frame_slots(frame, ncfg)
        if slots is None or strip is None:
            skipped += 1
            LOG.debug(
                "[session %d/%d] frame %d — skip (%s)",
                session_idx,
                n_sessions,
                frame_idx,
                err,
            )
            continue

        classic_digits = classic_digits_for_frame(strip, slots, decoder)
        if classic_digits is not None:
            for i, d in enumerate(classic_digits):
                slot_votes[i].append(d)

        frame_records.append(
            {
                "frame_idx": frame_idx,
                "slots": slots,
                "classic_digits": classic_digits,
            }
        )
        print(
            f"[session {session_idx}/{n_sessions}] frame {frame_idx}/{total_frames} "
            f"— 4 slots extracted",
            flush=True,
        )

    # Determine labels
    uncertain_slots: list[int] = []
    label_digits: list[str | None] = [None, None, None, None]
    label_shares: list[float] = [0.0, 0.0, 0.0, 0.0]

    if label_source == "agent":
        if session.weight_g is None:
            LOG.warning(
                "session %s: no weight_g for agent labels — all slots uncertain",
                ordinal,
            )
            uncertain_slots = [0, 1, 2, 3]
        else:
            label_digits = weight_to_digits(session.weight_g)  # type: ignore[assignment]
            label_shares = [1.0, 1.0, 1.0, 1.0]
    else:
        # classic & consensus both use majority over sampled frames
        # (classic: same multi-frame vote; named for decoder source)
        for i in range(4):
            lab, share = majority_vote(slot_votes[i])
            label_shares[i] = share
            if lab is None:
                uncertain_slots.append(i)
                label_digits[i] = None
            else:
                label_digits[i] = lab

        # If agent weight is available and label_source is consensus, prefer
        # agent weight only when all slots uncertain? Spec says consensus uses
        # ClassicV2 majority — keep that pure. Agent digits only for "agent".

    if label_source == "agent" and session.weight_g is not None:
        # already set
        pass

    # Save patches for non-uncertain slots
    num_patches = 0
    class_counts: Counter[str] = Counter()
    for rec in frame_records:
        frame_idx = rec["frame_idx"]
        slots = rec["slots"]
        for i, patch in enumerate(slots):
            lab = label_digits[i]
            if lab is None:
                continue
            # classic mode: optionally require this frame's classic read to agree?
            # Spec: use majority vote per slot — still save all sampled patches
            # under the session-level majority label.
            norm = normalize_patch(patch, PATCH_SIZE)
            name = f"{ordinal:02d}_{frame_idx:06d}_slot{i}.png"
            out_path = patches_dir / name
            if not cv2.imwrite(str(out_path), norm):
                LOG.warning("failed to write %s", out_path)
                continue
            num_patches += 1
            class_counts[lab] += 1

    return {
        "ordinal": ordinal,
        "weight_g": session.weight_g,
        "t_start_s": session.t_start_s,
        "t_end_s": session.t_end_s,
        "label_digits": [
            d if d is not None else "uncertain" for d in label_digits
        ],
        "label_source": label_source,
        "label_shares": [round(s, 4) for s in label_shares],
        "num_frames_sampled": len(frame_records),
        "num_frames_requested": len(indices),
        "skipped_frames": skipped,
        "num_patches": num_patches,
        "uncertain_slots": uncertain_slots,
        "class_counts": dict(class_counts),
        "confidence": session.confidence,
        "note": session.note,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Extract labeled 28×40 digit patches from weighing videos.",
    )
    p.add_argument("video", type=Path, help="Path to mp4 video")
    p.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory (default: training_data/<video_stem>)",
    )
    p.add_argument(
        "--ground-truth",
        type=Path,
        default=None,
        help="JSON with sessions[{ordinal, weight_g, t_start_s, t_end_s}]",
    )
    p.add_argument(
        "--agent",
        action="store_true",
        help="Run Agent VLM to obtain ground-truth sessions when JSON missing",
    )
    p.add_argument(
        "--cross-validate",
        action="store_true",
        help="Run multiple VLM models (gemini-3-flash, gpt-5.6, grok-4.5) and keep only consensus sessions",
    )
    p.add_argument(
        "--cv-models",
        type=str,
        default=None,
        help="Comma-separated model names for cross-validation (default: gemini-3-flash,gpt-5.6,grok-4.5)",
    )
    p.add_argument(
        "--cv-weight-tol",
        type=float,
        default=0.5,
        help="Max weight spread (grams) for cross-validation agreement (default: 0.5)",
    )
    p.add_argument(
        "--stride",
        type=int,
        default=3,
        help="Sample every Nth frame within session windows (default: 3)",
    )
    p.add_argument(
        "--label-source",
        choices=("agent", "classic", "consensus"),
        default="consensus",
        help="How to assign digit labels (default: consensus)",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    video: Path = args.video.expanduser().resolve()
    if not video.is_file():
        print(f"error: video not found: {video}", file=sys.stderr)
        return 2

    out_dir: Path = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else (ROOT / "training_data" / video.stem)
    )
    patches_dir = out_dir / "patches"
    patches_dir.mkdir(parents=True, exist_ok=True)

    # Ground truth
    sessions: list[SessionSpec] = []
    if args.ground_truth is not None:
        gt_path = args.ground_truth.expanduser().resolve()
        if not gt_path.is_file():
            print(f"error: ground-truth not found: {gt_path}", file=sys.stderr)
            return 2
        sessions = load_ground_truth_json(gt_path)
    elif args.cross_validate:
        cv_models = (
            [m.strip() for m in args.cv_models.split(",") if m.strip()]
            if args.cv_models
            else None
        )
        try:
            sessions = cross_validate_sessions(
                video, models=cv_models, weight_tol=args.cv_weight_tol
            )
        except Exception as exc:  # noqa: BLE001
            LOG.exception("cross-validation failed: %s", exc)
            print(f"error: cross-validation failed: {exc}", file=sys.stderr)
            return 3
    elif args.agent:
        try:
            sessions = run_agent_ground_truth(video)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("agent ground-truth failed: %s", exc)
            print(f"error: agent failed: {exc}", file=sys.stderr)
            return 3
    else:
        print(
            "error: provide --ground-truth JSON or --agent to obtain sessions",
            file=sys.stderr,
        )
        return 2

    if not sessions:
        print("error: no sessions with usable time windows", file=sys.stderr)
        return 2

    if args.label_source == "agent":
        missing_w = [s.ordinal for s in sessions if s.weight_g is None]
        if missing_w:
            LOG.warning(
                "label-source=agent but sessions missing weight_g: %s",
                missing_w,
            )

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        print(f"error: cannot open video: {video}", file=sys.stderr)
        return 2

    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if fps <= 1e-3:
        LOG.warning("invalid fps from video — assuming 30.0")
        fps = 30.0
    if total_frames <= 0:
        print("error: video has no frames", file=sys.stderr)
        cap.release()
        return 2

    LOG.info(
        "video=%s fps=%.3f frames=%d sessions=%d stride=%d label_source=%s",
        video.name,
        fps,
        total_frames,
        len(sessions),
        args.stride,
        args.label_source,
    )

    ncfg = NormalizeConfig()
    decoder = ClassicV2Decoder()
    session_manifests: list[dict[str, Any]] = []
    global_counts: Counter[str] = Counter()
    total_patches = 0
    total_uncertain_slots = 0
    total_skipped = 0

    try:
        for si, sess in enumerate(sessions, start=1):
            sm = process_session(
                cap=cap,
                session=sess,
                session_idx=si,
                n_sessions=len(sessions),
                fps=fps,
                total_frames=total_frames,
                stride=args.stride,
                label_source=args.label_source,
                ncfg=ncfg,
                decoder=decoder,
                patches_dir=patches_dir,
            )
            session_manifests.append(sm)
            total_patches += int(sm["num_patches"])
            total_uncertain_slots += len(sm["uncertain_slots"])
            total_skipped += int(sm["skipped_frames"])
            for k, v in (sm.get("class_counts") or {}).items():
                global_counts[k] += int(v)
    finally:
        cap.release()

    # Drop per-session class_counts from public manifest (keep compact)
    public_sessions = []
    for sm in session_manifests:
        public_sessions.append(
            {
                "ordinal": sm["ordinal"],
                "weight_g": sm["weight_g"],
                "t_start_s": sm["t_start_s"],
                "t_end_s": sm["t_end_s"],
                "label_digits": sm["label_digits"],
                "label_source": sm["label_source"],
                "label_shares": sm["label_shares"],
                "num_frames_sampled": sm["num_frames_sampled"],
                "num_patches": sm["num_patches"],
                "uncertain_slots": sm["uncertain_slots"],
                "skipped_frames": sm["skipped_frames"],
            }
        )

    manifest = {
        "video": str(video),
        "fps": fps,
        "total_frames": total_frames,
        "stride": int(args.stride),
        "label_source": args.label_source,
        "sessions": public_sessions,
        "total_patches": total_patches,
        "total_skipped_frames": total_skipped,
        "total_uncertain_slot_instances": total_uncertain_slots,
        "class_distribution": dict(sorted(global_counts.items(), key=lambda kv: kv[0])),
        "patch_size": {"width": PATCH_SIZE[0], "height": PATCH_SIZE[1]},
        "majority_threshold": MAJORITY_THRESHOLD,
    }

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print()
    print("=== summary ===")
    print(f"output_dir     : {out_dir}")
    print(f"total_patches  : {total_patches}")
    print(f"skipped_frames : {total_skipped}")
    print(f"uncertain_slots: {total_uncertain_slots} (session×slot with no majority)")
    print(f"class_distribution: {dict(sorted(global_counts.items()))}")
    print(f"manifest       : {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
