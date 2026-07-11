"""Clip boundary helpers for per-mouse video replay."""

from __future__ import annotations

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
