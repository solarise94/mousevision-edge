"""Local evidence attachment for the agent weighing path.

After ``persist_agent_sessions`` writes ``mouse_NNN/record.json`` with weight +
count, this module samples frames from the retained source video, picks the
best report photo per session (mouse-on-scale + optional OCR cross-check) and
writes ``mouse_NNN/photo.jpg`` plus platform time anchors into ``record.json``.

Soft-fail: any per-session error is logged and the record is left without a
photo rather than raising — the agent weight already stands.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from mousevision.agent_weigh import AgentSession, resolve_agent_config
from mousevision.detect import detect_mouse_box

log = logging.getLogger("mousevision.agent_evidence")


def sample_video_frames(
    video_path: Path,
    *,
    interval_ms: float = 200.0,
    max_frames: int = 400,
) -> list[tuple[float, int, np.ndarray]]:
    """Return ``[(timestamp_ms, frame_index, bgr_image), ...]`` sampled every
    ``interval_ms`` wall time. Caps to ``max_frames`` (evenly when huge).
    Returns ``[]`` if the video cannot be opened.
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        log.warning("sample_video_frames: cannot open %s", video_path)
        return []
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        frames: list[tuple[float, int, np.ndarray]] = []
        idx = 0
        next_ms = 0.0
        while True:
            ok, img = cap.read()
            if not ok or img is None:
                break
            # Prefer OpenCV's PTS ms; fall back to index/fps.
            pos_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
            if pos_ms is None or pos_ms < 0 or (fps <= 0 and idx > 0):
                ts_ms = (idx / fps * 1000.0) if fps > 0 else float(idx * interval_ms)
            else:
                ts_ms = float(pos_ms)
            if ts_ms + 1e-3 >= next_ms:
                frames.append((ts_ms, idx, img))
                next_ms += interval_ms
            idx += 1
        # Even cap when video is huge.
        if len(frames) > max_frames:
            step = len(frames) / float(max_frames)
            picked = [frames[int(i * step)] for i in range(max_frames)]
            frames = picked
        # If PTS was unavailable, n_frames helps callers estimate duration.
        if not frames and n_frames > 0 and fps > 0:
            log.debug(
                "sample_video_frames: no frames decoded (n=%s fps=%.2f)",
                n_frames,
                fps,
            )
        return frames
    finally:
        cap.release()


def _video_duration_ms(
    frames: list[tuple[float, int, np.ndarray]],
    video_path: Path,
) -> float:
    """Best-effort total duration in ms."""
    if frames:
        last_ts = frames[-1][0]
        # Heuristic: last sampled ts is just before EOF; add one interval.
        return max(last_ts, 0.0)
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return 0.0
    try:
        n = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        fps = cap.get(cv2.CAP_PROP_FPS)
        if n and fps and fps > 0:
            return float(n) / float(fps) * 1000.0
    finally:
        cap.release()
    return 0.0


def resolve_session_window_ms(
    sess: AgentSession,
    *,
    video_duration_ms: float,
    session_index: int,
    n_sessions: int,
    pad_s: float = 1.5,
    default_window_s: float = 5.0,
) -> tuple[float, float, float | None]:
    """Resolve ``(start_ms, end_ms, stable_ms_or_None)`` for a session.

    Priority:
    1. t_start_s + t_end_s → pad both sides by ``pad_s``.
    2. only t_stable_s → ``[stable - window/2, stable + window/2]``.
    3. only one of t_start / t_end → expand by ``default_window_s``.
    4. no times → even temporal partitions of the full duration by ordinal.
    """
    pad_ms = float(pad_s) * 1000.0
    win_ms = float(default_window_s) * 1000.0
    dur = max(0.0, float(video_duration_ms))
    t_start = float(sess.t_start_s) if sess.t_start_s is not None else None
    t_end = float(sess.t_end_s) if sess.t_end_s is not None else None
    t_stable = float(sess.t_stable_s) if sess.t_stable_s is not None else None

    def _s_to_ms(v: float | None) -> float | None:
        return None if v is None else v * 1000.0

    start_ms: float | None = None
    end_ms: float | None = None
    stable_ms = _s_to_ms(t_stable)

    if t_start is not None and t_end is not None:
        start_ms = t_start * 1000.0 - pad_ms
        end_ms = t_end * 1000.0 + pad_ms
        if stable_ms is None:
            stable_ms = (t_start + t_end) / 2.0 * 1000.0
    elif t_stable is not None and t_start is None and t_end is None:
        start_ms = stable_ms - win_ms / 2.0
        end_ms = stable_ms + win_ms / 2.0
    elif t_start is not None or t_end is not None:
        anchor = t_start if t_start is not None else t_end
        assert anchor is not None
        start_ms = anchor * 1000.0
        end_ms = start_ms + win_ms
        if stable_ms is None:
            stable_ms = (start_ms + end_ms) / 2.0
    else:
        # Even temporal partition by ordinal index.
        if n_sessions <= 0:
            n_sessions = 1
        i = max(0, min(int(session_index), int(n_sessions) - 1))
        if dur <= 0:
            return (0.0, max(win_ms, 300.0), None)
        seg = dur / float(n_sessions)
        start_ms = i * seg
        end_ms = (i + 1) * seg
        stable_ms = (start_ms + end_ms) / 2.0

    assert start_ms is not None and end_ms is not None
    # Clamp to [0, dur].
    start_ms = max(0.0, start_ms)
    end_ms = max(0.0, end_ms) if dur <= 0 else min(dur, end_ms)
    # Ensure end > start by at least 300ms.
    if end_ms - start_ms < 300.0:
        end_ms = start_ms + 300.0
        if dur > 0 and end_ms > dur:
            end_ms = dur
            start_ms = max(0.0, end_ms - 300.0)
    if stable_ms is not None:
        stable_ms = max(start_ms, min(end_ms, stable_ms))
    return (start_ms, end_ms, stable_ms)


def _detect_mouse(image: np.ndarray, mouse_detect_cfg: dict) -> tuple[int, int, int, int] | None:
    md = mouse_detect_cfg or {}
    return detect_mouse_box(
        image,
        None,
        gray_thr=int(md.get("gray_threshold", 70)),
        min_area=int(md.get("min_area", 800)),
        x_ratio=tuple(md.get("x_ratio", (0.12, 0.88))),
        max_area=(int(md["max_area"]) if md.get("max_area") is not None else None),
        aspect_ratio=tuple(md.get("aspect_ratio", (0.3, 2.0))),
        use_otsu=bool(md.get("use_otsu", True)),
        dark_p05=(float(md["dark_p05"]) if md.get("dark_p05") is not None else None),
        dark_ratio=(float(md["dark_ratio"]) if md.get("dark_ratio") is not None else None),
    )


def score_frame_for_session(
    image: np.ndarray,
    *,
    target_weight: float | None,
    weight_tol: float,
    reader: Any | None,
    mouse_detect_cfg: dict,
) -> tuple[float, dict]:
    """Score a candidate report frame.

    - mouse blob present: +2.0
    - if reader and target_weight: read_weight; if ``|w - target| <= tol`` → +3.0,
      elif readable non-zero → +0.5
    Returns ``(score, meta)``.
    """
    meta: dict[str, Any] = {
        "mouse_detected": False,
        "ocr_weight": None,
        "ocr_confidence": 0.0,
    }
    score = 0.0
    try:
        mouse_box = _detect_mouse(image, mouse_detect_cfg)
    except Exception as exc:  # noqa: BLE001
        log.debug("mouse detect failed: %s", exc)
        mouse_box = None
    if mouse_box is not None:
        meta["mouse_detected"] = True
        score += 2.0

    if reader is not None and target_weight is not None:
        try:
            w, c = reader.read_weight(image)
        except Exception as exc:  # noqa: BLE001
            log.debug("reader.read_weight failed: %s", exc)
            w, c = None, 0.0
        if w is not None:
            meta["ocr_weight"] = float(w)
            meta["ocr_confidence"] = float(c)
            if abs(float(w) - float(target_weight)) <= float(weight_tol):
                score += 3.0
            elif w > 0:
                score += 0.5
    return score, meta


def pick_photo_for_session(
    frames: list[tuple[float, int, np.ndarray]],
    window: tuple[float, float, float | None],
    *,
    target_weight: float | None,
    weight_tol: float,
    reader: Any | None,
    mouse_detect_cfg: dict,
) -> dict[str, Any] | None:
    """Pick the best photo frame inside ``window``.

    Returns dict with ``photo_image`` / ``timestamp_ms`` / ``frame_index`` /
    ``score`` / ``selection`` / ``mouse_detected`` / ``photo_observed_weight`` /
    ``photo_weight_delta`` / ``platform_start_ms`` / ``platform_end_ms`` or None
    when there are no frames at all.
    """
    if not frames:
        return None
    start_ms, end_ms, stable_ms = window

    in_window = [f for f in frames if start_ms - 1e-3 <= f[0] <= end_ms + 1e-3]
    candidates = in_window
    selection = "window"
    if not in_window:
        # Fall back to nearest frames by absolute distance to the window.
        mid = stable_ms if stable_ms is not None else (start_ms + end_ms) / 2.0
        nearest = sorted(frames, key=lambda f: abs(f[0] - mid))[:6]
        candidates = nearest
        selection = "nearest"

    anchor = stable_ms if stable_ms is not None else (start_ms + end_ms) / 2.0
    best: tuple[float, float, float, int, np.ndarray, dict] | None = None
    # tie-break keys: (-score, distance_to_anchor)
    for ts_ms, fidx, img in candidates:
        score, meta = score_frame_for_session(
            img,
            target_weight=target_weight,
            weight_tol=weight_tol,
            reader=reader,
            mouse_detect_cfg=mouse_detect_cfg,
        )
        dist = abs(ts_ms - anchor)
        key = (score, -dist)
        if best is None or key > (best[0], -best[1]):
            best = (score, dist, ts_ms, fidx, img, meta)

    if best is None:
        return None
    score, _dist, ts_ms, fidx, img, meta = best
    obs_w = meta.get("ocr_weight")
    delta = None
    if obs_w is not None and target_weight is not None:
        delta = float(obs_w) - float(target_weight)
    return {
        "photo_image": img,
        "timestamp_ms": float(ts_ms),
        "frame_index": int(fidx),
        "score": float(score),
        "selection": selection,
        "mouse_detected": bool(meta.get("mouse_detected", False)),
        "photo_observed_weight": obs_w,
        "photo_weight_delta": delta,
        "platform_start_ms": float(start_ms),
        "platform_end_ms": float(end_ms),
    }


def _maybe_build_reader(
    config: dict | None, templates_dir: Path | None
) -> Any | None:
    """Construct a TemplateReader when templates exist; else None (no OCR)."""
    if not templates_dir:
        return None
    td = Path(templates_dir)
    if not td.exists():
        return None
    try:
        from mousevision.reader.template import TemplateReader
    except Exception as exc:  # noqa: BLE001
        log.warning("TemplateReader import failed: %s", exc)
        return None
    cfg = config or {}
    try:
        return TemplateReader(
            templates_dir=td,
            match_threshold=float(cfg.get("match_threshold", 0.50)),
            min_digit_confidence=float(cfg.get("min_digit_confidence", 0.45)),
            lcd_detect=cfg.get("lcd_detect") or {},
            weight_roi=cfg.get("weight_roi"),
            expected_digits=tuple(cfg.get("expected_digits", (3, 4))),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("TemplateReader init failed: %s", exc)
        return None


def attach_agent_evidence(
    *,
    records: list[dict],
    sessions: list[AgentSession],
    video_path: Path,
    run_dir: Path,
    config: dict | None = None,
    templates_dir: Path | None = None,
) -> list[dict]:
    """For each session/record pair, pick a photo and write it + platform times.

    Updates ``record.json`` in place and returns the (mutated) records list.
    Soft-fails per session — never raises.
    """
    cfg = resolve_agent_config(config) if config is not None else resolve_agent_config(None)
    interval_ms = float(cfg.get("photo_sample_interval_ms", 200.0))
    pad_s = float(cfg.get("photo_pad_s", 1.5))
    weight_tol = float(cfg.get("photo_weight_tol", 0.25))
    window_s = float(cfg.get("photo_window_s", 5.0))
    mouse_detect_cfg = (config or {}).get("mouse_detect") or {}

    video_path = Path(video_path)
    run_dir = Path(run_dir)

    if not records or not sessions:
        return records
    if not video_path.is_file():
        log.warning("attach_agent_evidence: missing video %s", video_path)
        return records

    try:
        frames = sample_video_frames(video_path, interval_ms=interval_ms)
    except Exception as exc:  # noqa: BLE001
        log.warning("attach_agent_evidence: sample failed: %s", exc)
        return records
    if not frames:
        log.warning("attach_agent_evidence: no frames sampled from %s", video_path)
        return records

    duration_ms = _video_duration_ms(frames, video_path)
    reader = _maybe_build_reader(config, templates_dir)
    n_sessions = len(sessions)

    for i, record in enumerate(records):
        if i >= n_sessions:
            break
        sess = sessions[i]
        mouse_dir = run_dir / f"mouse_{int(record.get('ordinal') or (i + 1)):03d}"
        try:
            start_ms, end_ms, stable_ms = resolve_session_window_ms(
                sess,
                video_duration_ms=duration_ms,
                session_index=i,
                n_sessions=n_sessions,
                pad_s=pad_s,
                default_window_s=window_s,
            )
            target = record.get("weight")
            target_w = float(target) if isinstance(target, (int, float)) else None
            pick = pick_photo_for_session(
                frames,
                (start_ms, end_ms, stable_ms),
                target_weight=target_w,
                weight_tol=weight_tol,
                reader=reader,
                mouse_detect_cfg=mouse_detect_cfg,
            )
            if pick is None:
                continue
            # Write photo.jpg
            photo_path = mouse_dir / "photo.jpg"
            try:
                mouse_dir.mkdir(parents=True, exist_ok=True)
                ok = cv2.imwrite(
                    str(photo_path),
                    pick["photo_image"],
                    [int(cv2.IMWRITE_JPEG_QUALITY), 90],
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("attach photo write failed %s: %s", photo_path, exc)
                ok = False
            if not ok:
                log.warning("attach photo write returned False: %s", photo_path)

            # Update record.json
            record_path = mouse_dir / "record.json"
            updated = dict(record)
            updated["platform_start_ms"] = float(start_ms)
            updated["platform_end_ms"] = float(end_ms)
            updated["photo_frame_index"] = int(pick["frame_index"])
            updated["photo_selection"] = pick["selection"]
            updated["photo_mouse_detected"] = bool(pick["mouse_detected"])
            updated["photo_verified"] = bool(pick["mouse_detected"])
            updated["photo_observed_weight"] = pick["photo_observed_weight"]
            updated["photo_weight_delta"] = pick["photo_weight_delta"]
            updated["photo_saved"] = bool(ok)
            updated["agent_photo_score"] = float(pick["score"])
            updated["clip_start_ms"] = float(start_ms)
            updated["clip_end_ms"] = float(end_ms)
            if record_path.is_file():
                try:
                    record_path.write_text(
                        json.dumps(updated, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("record.json write failed %s: %s", record_path, exc)
            # Reflect into in-memory record dict so callers see new fields.
            record.update(updated)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "attach_agent_evidence: session %s failed: %s",
                record.get("ordinal", i + 1),
                exc,
            )
            continue

    return records
