"""Explainable per-frame replay for a weighing time window.

Exports each analysed frame's:
  - fused weight / confidence / state
  - raw OCR weight, status, digits, confidence
  - mouse detection box
  - state-machine transitions (with reason)
  - LCD bbox crop (+ optional keyframe overlays)

Intended for Linux-container truth runs (MOUSEVISION_VIDEO_BACKEND=ffmpeg).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mousevision.detect import detect_mouse_box  # noqa: E402
from mousevision.driver import FrameEvent, SessionDriver  # noqa: E402
from mousevision.pipeline import load_config  # noqa: E402
from mousevision.reader.http_ocr import HttpOcrReader  # noqa: E402
from mousevision.source.video import VideoFileSource  # noqa: E402


def _crop_quad(image: np.ndarray, quad: list[list[float]] | None) -> np.ndarray | None:
    if not quad or len(quad) != 4:
        return None
    xs = [float(p[0]) for p in quad]
    ys = [float(p[1]) for p in quad]
    h, w = image.shape[:2]
    x0 = max(0, int(min(xs)))
    y0 = max(0, int(min(ys)))
    x1 = min(w, int(max(xs)) + 1)
    y1 = min(h, int(max(ys)) + 1)
    if x1 <= x0 or y1 <= y0:
        return None
    return image[y0:y1, x0:x1].copy()


def _overlay(
    image: np.ndarray,
    *,
    lcd: Any | None,
    mouse_box: tuple[int, int, int, int] | None,
    text_lines: list[str],
) -> np.ndarray:
    vis = image.copy()
    if lcd is not None:
        cv2.rectangle(
            vis,
            (int(lcd.x), int(lcd.y)),
            (int(lcd.x + lcd.w), int(lcd.y + lcd.h)),
            (0, 255, 255),
            2,
        )
    if mouse_box is not None:
        x, y, bw, bh = mouse_box
        cv2.rectangle(vis, (x, y), (x + bw, y + bh), (0, 255, 0), 2)
    y = 28
    for line in text_lines:
        cv2.putText(
            vis,
            line,
            (12, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 0, 0),
            3,
            cv2.LINE_AA,
        )
        cv2.putText(
            vis,
            line,
            (12, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
        y += 22
    return vis


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Explainable session replay")
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "configs" / "scale_refvideo.yaml")
    parser.add_argument(
        "--ocr-url",
        default=os.environ.get("LCD_OCR_URL")
        or os.environ.get("MOUSEVISION_OCR_URL")
        or "http://127.0.0.1:8768",
    )
    parser.add_argument("--start-ms", type=float, default=0.0)
    parser.add_argument("--end-ms", type=float, default=16000.0)
    parser.add_argument("--frame-stride", type=int, default=None)
    parser.add_argument("--save-every", type=int, default=2, help="Save crop/overlay every N emitted frames")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--backend",
        choices=["ffmpeg", "opencv", "auto"],
        default=os.environ.get("MOUSEVISION_VIDEO_BACKEND") or "ffmpeg",
    )
    args = parser.parse_args(argv)

    if not args.video.is_file():
        print(f"missing video: {args.video}", file=sys.stderr)
        return 2

    config = load_config(args.config)
    config["weight_reader"] = "http_ocr"
    ocr_api = dict(config.get("ocr_api") or {})
    ocr_api["base_url"] = str(args.ocr_url).rstrip("/")
    config["ocr_api"] = ocr_api
    os.environ["MOUSEVISION_WEIGHT_READER"] = "http_ocr"
    os.environ["MOUSEVISION_OCR_URL"] = ocr_api["base_url"]
    os.environ["MOUSEVISION_VIDEO_BACKEND"] = args.backend

    out = args.out
    crops = out / "crops"
    overlays = out / "overlays"
    slots_dir = out / "slots"
    for d in (out, crops, overlays, slots_dir):
        d.mkdir(parents=True, exist_ok=True)

    stride = (
        args.frame_stride
        if args.frame_stride is not None
        else int(config.get("frame_stride", 2))
    )
    templates = ROOT / config.get("templates_dir", "assets/templates")

    rows: list[dict[str, Any]] = []
    transitions_at: list[dict[str, Any]] = []
    saved_sessions: list[dict[str, Any]] = []
    emit_i = 0

    driver = SessionDriver(
        config=config,
        templates_dir=templates,
        output_root=out / "sessions",
        cage_id="EXPLAIN",
        run_id="explain",
        persist=True,
        start_ordinal=1,
    )

    def on_saved(ev: Any) -> None:
        saved_sessions.append(
            {
                "session_index": ev.session_index,
                "weight": ev.record.get("weight"),
                "needs_review": ev.record.get("needs_review"),
                "review_reason": ev.record.get("review_reason"),
                "state_history": ev.record.get("state_history"),
                "platform_start_ms": ev.record.get("platform_start_ms"),
                "platform_end_ms": ev.record.get("platform_end_ms"),
                "clip_start_ms": ev.record.get("clip_start_ms"),
                "clip_end_ms": ev.record.get("clip_end_ms"),
                "photo_observed_weight": ev.record.get("photo_observed_weight"),
                "output_dir": str(ev.output_dir),
            }
        )

    driver.on_saved = on_saved

    source = VideoFileSource(
        args.video,
        frame_stride=stride,
        start_ms=args.start_ms,
        end_ms=args.end_ms,
        backend=args.backend,  # type: ignore[arg-type]
    )
    prev_hist_n = 0
    try:
        for frame in source.frames():
            prev_state = driver.sm.state.value
            event: FrameEvent = driver.process_frame(frame)

            raw = None
            digits: list[str] = []
            digit_confs: list[float] = []
            raw_weight = None
            raw_conf = 0.0
            raw_status = event.raw_status
            quad = None
            if isinstance(driver.reader, HttpOcrReader) and driver.reader._last_obs is not None:
                raw = driver.reader._last_obs
                raw_weight = raw.weight
                raw_conf = float(raw.confidence)
                raw_status = raw.status
                digits = list(raw.digits or [])
                digit_confs = list(raw.digit_confidences or [])
                quad = raw.screen_quad

            md_cfg = config.get("mouse_detect") or {}
            mouse_box = detect_mouse_box(
                frame.image,
                event.lcd,
                gray_thr=int(md_cfg.get("gray_threshold", 70)),
                min_area=int(md_cfg.get("min_area", 800)),
                x_ratio=tuple(md_cfg.get("x_ratio", (0.12, 0.88))),
            )
            mouse_present = mouse_box is not None

            # Capture new SM transitions since last frame.
            hist = driver.sm.history
            if len(hist) > prev_hist_n:
                for t in hist[prev_hist_n:]:
                    transitions_at.append(
                        {
                            "frame_index": frame.index,
                            "t_ms": t.timestamp_ms,
                            "previous": t.previous.value,
                            "current": t.current.value,
                            "reason": t.reason,
                            "fused_weight": event.weight,
                            "raw_weight": raw_weight,
                            "raw_status": raw_status,
                            "mouse": mouse_present,
                        }
                    )
                prev_hist_n = len(hist)

            row = {
                "frame_index": frame.index,
                "t_ms": round(frame.timestamp_ms, 3),
                "state": event.state.value,
                "prev_state": prev_state,
                "fused_weight": event.weight,
                "fused_conf": round(float(event.confidence), 4),
                "raw_weight": raw_weight,
                "raw_status": raw_status,
                "raw_conf": round(raw_conf, 4),
                "digits": digits,
                "digit_confidences": [round(float(c), 4) for c in digit_confs],
                "mouse": mouse_present,
                "mouse_box": list(mouse_box) if mouse_box else None,
                "needs_review": event.needs_review,
                "review_reason": event.review_reason,
                "curve_len": event.curve_len,
                "lcd": (
                    {"x": event.lcd.x, "y": event.lcd.y, "w": event.lcd.w, "h": event.lcd.h}
                    if event.lcd is not None
                    else None
                ),
            }
            rows.append(row)

            if emit_i % max(1, args.save_every) == 0:
                tag = f"f{frame.index:06d}_t{int(frame.timestamp_ms):06d}"
                crop = _crop_quad(frame.image, quad)
                if crop is not None:
                    cv2.imwrite(str(crops / f"{tag}_lcd.jpg"), crop)
                # Four equal-width digit slots from LCD crop when possible.
                if crop is not None and crop.shape[1] >= 40:
                    ch, cw = crop.shape[:2]
                    sw = cw // 4
                    for si in range(4):
                        slot = crop[:, si * sw : (si + 1) * sw]
                        cv2.imwrite(str(slots_dir / f"{tag}_s{si}.jpg"), slot)
                ov = _overlay(
                    frame.image,
                    lcd=event.lcd,
                    mouse_box=mouse_box,
                    text_lines=[
                        f"t={frame.timestamp_ms/1000:.2f}s i={frame.index} {event.state.value}",
                        f"raw={raw_weight} ({raw_status}/{raw_conf:.2f}) dig={''.join(digits)}",
                        f"fused={event.weight} conf={event.confidence:.2f} mouse={int(mouse_present)}",
                    ],
                )
                cv2.imwrite(str(overlays / f"{tag}.jpg"), ov, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            emit_i += 1
    finally:
        source.close()
        if isinstance(driver.reader, HttpOcrReader):
            driver.reader.close()

    summary = {
        "video": str(args.video),
        "ocr_url": ocr_api["base_url"],
        "backend": args.backend,
        "window_ms": [args.start_ms, args.end_ms],
        "frame_stride": stride,
        "n_frames": len(rows),
        "transitions": transitions_at,
        "sessions": saved_sessions,
        "video_backend_env": os.environ.get("MOUSEVISION_VIDEO_BACKEND"),
    }
    (out / "frames.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Compact human-readable timeline of state changes + weight clusters.
    lines = [
        f"# Explain replay {args.video.name}",
        f"window={args.start_ms}-{args.end_ms} ms backend={args.backend} frames={len(rows)}",
        "",
        "## Transitions",
    ]
    for t in transitions_at:
        lines.append(
            f"- t={t['t_ms']/1000:.2f}s f={t['frame_index']}: "
            f"{t['previous']}→{t['current']} ({t['reason']}) "
            f"raw={t['raw_weight']}/{t['raw_status']} fused={t['fused_weight']} mouse={t['mouse']}"
        )
    lines.append("")
    lines.append("## Sessions saved")
    for s in saved_sessions:
        lines.append(
            f"- #{s['session_index']} weight={s['weight']} review={s['needs_review']} "
            f"platform={s['platform_start_ms']}-{s['platform_end_ms']} "
            f"photo_obs={s['photo_observed_weight']}"
        )
    lines.append("")
    lines.append("## Weight timeline (every row)")
    for r in rows:
        if r["raw_weight"] is None and r["fused_weight"] is None and r["state"] == "EMPTY":
            continue
        lines.append(
            f"- t={r['t_ms']/1000:.2f}s f={r['frame_index']} {r['state']}: "
            f"raw={r['raw_weight']}({r['raw_status']}/{r['raw_conf']}) "
            f"fused={r['fused_weight']} dig={''.join(r['digits'])} mouse={int(r['mouse'])}"
        )
    (out / "TIMELINE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"wrote {out} frames={len(rows)} transitions={len(transitions_at)} sessions={len(saved_sessions)}")
    for s in saved_sessions:
        print(f"  session#{s['session_index']} weight={s['weight']} review={s['needs_review']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
