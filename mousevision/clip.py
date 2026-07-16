"""Clip boundary helpers and ffmpeg export for per-mouse video replay."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


def clip_bounds_from_record(
    record: dict[str, Any],
    *,
    pad_before_ms: float = 800.0,
    pad_after_ms: float = 800.0,
) -> tuple[float, float]:
    """Derive [start_ms, end_ms] for replaying one weighing session."""
    if record.get("clip_start_ms") is not None and record.get("clip_end_ms") is not None:
        return float(record["clip_start_ms"]), float(record["clip_end_ms"])

    history = record.get("state_history") or []
    enter_ms = None
    end_ms = None
    for t in history:
        if t.get("current") == "ENTER" and enter_ms is None:
            enter_ms = float(t["t_ms"])
        if t.get("current") in {"LEAVE", "ANALYZE"}:
            end_ms = float(t["t_ms"])

    if enter_ms is None and record.get("platform_start_ms") is not None:
        enter_ms = float(record["platform_start_ms"])
    if end_ms is None and record.get("platform_end_ms") is not None:
        end_ms = float(record["platform_end_ms"])

    if enter_ms is None:
        enter_ms = 0.0
    if end_ms is None:
        end_ms = enter_ms + 5000.0

    start = max(0.0, enter_ms - pad_before_ms)
    finish = max(start + 500.0, end_ms + pad_after_ms)
    return start, finish


def clip_bounds_from_history(
    history: list[dict[str, Any]],
    *,
    pad_before_ms: float = 800.0,
    pad_after_ms: float = 800.0,
) -> tuple[float, float]:
    return clip_bounds_from_record(
        {"state_history": history},
        pad_before_ms=pad_before_ms,
        pad_after_ms=pad_after_ms,
    )


def _ffmpeg_bin() -> str:
    return os.environ.get("MOUSEVISION_FFMPEG") or "ffmpeg"


def export_session_clip(
    source_video: str | Path,
    out_path: str | Path,
    *,
    start_ms: float,
    end_ms: float,
) -> str:
    """Cut ``[start_ms, end_ms]`` from ``source_video`` into ``out_path``.

    Returns ``\"ok\"`` on success, or a short reason string on skip/failure
    (``missing_source``, ``bad_window``, ``ffmpeg_missing``, ``ffmpeg_failed``).
    """
    src = Path(source_video)
    dest = Path(out_path)
    if not src.is_file():
        return "missing_source"
    if end_ms <= start_ms:
        return "bad_window"
    ff = _ffmpeg_bin()
    if shutil.which(ff) is None:
        return "ffmpeg_missing"

    dest.parent.mkdir(parents=True, exist_ok=True)
    start_s = max(0.0, float(start_ms) / 1000.0)
    dur_s = max(0.2, (float(end_ms) - float(start_ms)) / 1000.0)

    def _run(cmd: list[str]) -> bool:
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0 and dest.is_file() and dest.stat().st_size > 0

    # Prefer stream copy (fast, bit-stable). Fall back to re-encode if needed.
    copy_cmd = [
        ff,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start_s:.3f}",
        "-i",
        str(src),
        "-t",
        f"{dur_s:.3f}",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        str(dest),
    ]
    if _run(copy_cmd):
        return "ok"

    if dest.exists():
        try:
            dest.unlink()
        except OSError:
            pass

    encode_cmd = [
        ff,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start_s:.3f}",
        "-i",
        str(src),
        "-t",
        f"{dur_s:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-an",
        str(dest),
    ]
    if _run(encode_cmd):
        return "ok"
    return "ffmpeg_failed"
