"""Tests for weight statistics, per-cage outlier screening, and verify-cages grouping."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from mousevision.run import create_run_dir, finish_run
from ui.records_api import (
    _compute_weight_stats,
    _compute_cage_weight_view,
    _fill_daily_counts,
    _parse_date_to,
    _parse_date_from,
    collect_records,
    verify_cages_view,
)
from ui.records_meta import RecordsMetaStore
from ui.registry import MouseRegistry


# ---------------------------------------------------------------------------
# _compute_weight_stats — pure function
# ---------------------------------------------------------------------------

def test_weight_stats_empty():
    ws = _compute_weight_stats([])
    assert ws["n"] == 0
    assert ws["mean"] is None
    assert ws["show_fit"] is False
    assert ws["fit_x"] == []


def test_weight_stats_basic():
    ws = _compute_weight_stats([16.0, 16.5, 17.0, 15.5, 16.2])
    assert ws["n"] == 5
    assert ws["mean"] == round((16.0 + 16.5 + 17.0 + 15.5 + 16.2) / 5, 2)
    assert ws["min"] == 15.5
    assert ws["max"] == 17.0
    assert ws["range"] == 1.5
    # SD = std(ddof=1)
    expected_sd = round(np.std([16.0, 16.5, 17.0, 15.5, 16.2], ddof=1), 2)
    assert ws["sd"] == expected_sd


def test_weight_stats_small_sample_hides_fit():
    """n < 30 must NOT show a normal fit curve — it has no statistical support."""
    ws = _compute_weight_stats([16.0, 16.5, 17.0, 15.5, 16.2])
    assert ws["show_fit"] is False
    assert ws["fit_x"] == []


def test_weight_stats_large_sample_shows_fit():
    """n >= 30 with single cohort should show the fit curve."""
    rng = np.random.default_rng(42)
    weights = rng.normal(16.5, 0.5, 30).tolist()
    ws = _compute_weight_stats(weights, is_single_cohort=True)
    assert ws["show_fit"] is True
    assert len(ws["fit_x"]) == 40
    assert len(ws["fit_y"]) == 40


def test_weight_stats_fit_hidden_for_mixed_cohort():
    """n >= 30 but mixed cohort (no cage filter) must NOT show fit."""
    rng = np.random.default_rng(42)
    weights = rng.normal(16.5, 0.5, 30).tolist()
    ws = _compute_weight_stats(weights, is_single_cohort=False)
    assert ws["show_fit"] is False
    assert ws["fit_x"] == []


def test_weight_stats_histogram_shape():
    ws = _compute_weight_stats([16.0, 16.2, 16.5, 16.8, 17.0])
    assert len(ws["hist_counts"]) == len(ws["hist_bins"]) - 1
    assert sum(ws["hist_counts"]) == 5


def test_weight_stats_single_sample():
    ws = _compute_weight_stats([16.5])
    assert ws["n"] == 1
    assert ws["mean"] == 16.5
    assert ws["sem"] == 0.0
    assert ws["sd"] == 0.0
    assert ws["show_fit"] is False


# ---------------------------------------------------------------------------
# _compute_cage_weight_view — robust per-cage outlier screening
# ---------------------------------------------------------------------------

def test_cage_weight_outlier_detection():
    recs = [
        {"cage_id": "A", "weight": 16.0, "record_id": "r1", "ordinal": 1},
        {"cage_id": "A", "weight": 16.2, "record_id": "r2", "ordinal": 2},
        {"cage_id": "A", "weight": 20.0, "record_id": "r3", "ordinal": 3},
        {"cage_id": "B", "weight": 18.0, "record_id": "r4", "ordinal": 1},
    ]
    cv = _compute_cage_weight_view(recs)
    # Cage A: median=16.2, threshold 14.2–18.2; 20.0 is an outlier
    cage_a = cv["cages"][0]
    assert cage_a["median"] == 16.2
    assert cage_a["outlier_count"] == 1
    assert cage_a["points"][2]["outlier"] is True
    assert cage_a["points"][0]["outlier"] is False
    # Cage B: single mouse, median=18.0, not an outlier
    cage_b = cv["cages"][1]
    assert cage_b["outlier_count"] == 0
    assert cv["total_outliers"] == 1
    assert cv["total_n"] == 4


def test_cage_weight_skips_none_weight():
    recs = [
        {"cage_id": "A", "weight": 16.0, "record_id": "r1", "ordinal": 1},
        {"cage_id": "A", "weight": None, "record_id": "r2", "ordinal": 2},
    ]
    cv = _compute_cage_weight_view(recs)
    assert cv["cages"][0]["n"] == 1


# ---------------------------------------------------------------------------
# _fill_daily_counts — gap filling for the daily chart axis
# ---------------------------------------------------------------------------

def test_fill_daily_counts_fills_gaps():
    """With explicit bounds, gaps between data days are filled with 0."""
    out = _fill_daily_counts({"2026-07-10": 5, "2026-07-12": 3}, "2026-07-10", "2026-07-12")
    assert len(out) == 3
    assert out[1] == {"date": "2026-07-11", "count": 0}
    assert out[0]["count"] == 5
    assert out[2]["count"] == 3


def test_fill_daily_counts_default_window():
    """Without explicit bounds, defaults to last 30 days from latest data."""
    out = _fill_daily_counts({"2026-07-10": 5, "2026-07-12": 3}, None, None)
    # Latest = 2026-07-12, default 30 days back = 2026-06-12 -> 31 days inclusive
    assert len(out) == 31
    assert out[-1]["date"] == "2026-07-12"
    assert out[-1]["count"] == 3
    assert out[-3]["date"] == "2026-07-10"
    assert out[-3]["count"] == 5


def test_fill_daily_counts_clamps_long_span():
    """Spans longer than max_days are clamped."""
    out = _fill_daily_counts(
        {"2026-01-01": 1, "2026-07-12": 2},
        "2026-01-01", "2026-07-12",
        max_days=10,
    )
    assert len(out) == 11  # 10 days back + 1 inclusive


def test_fill_daily_counts_respects_bounds():
    out = _fill_daily_counts({"2026-07-12": 3}, "2026-07-10", "2026-07-12")
    assert len(out) == 3
    assert out[:2] == [
        {"date": "2026-07-10", "count": 0},
        {"date": "2026-07-11", "count": 0},
    ]


def test_fill_daily_counts_empty():
    assert _fill_daily_counts({}, None, None) == []


# ---------------------------------------------------------------------------
# verify_cages_view — needs a registry with run/mouse data
# ---------------------------------------------------------------------------

def _write_mouse(run_dir: Path, *, ordinal: int, cage_id: str, weight: float, run_id: str) -> Path:
    session_dir = run_dir / f"mouse_{ordinal:03d}"
    session_dir.mkdir(parents=True, exist_ok=True)
    img = np.zeros((20, 20, 3), dtype=np.uint8)
    cv2.imwrite(str(session_dir / "photo.jpg"), img)
    (session_dir / "record.json").write_text(
        json.dumps(
            {
                "box_id": cage_id, "cage_id": cage_id, "weight": weight,
                "confidence": 0.9, "timestamp": "2026-07-10T12:00:00",
                "device": "scale01", "photo": "photo.jpg", "ordinal": ordinal,
                "run_id": run_id, "record_id": f"rec-{cage_id}-{ordinal}",
            }
        ),
        encoding="utf-8",
    )
    return session_dir


def test_verify_cages_groups_pending_only(tmp_path: Path):
    output = tmp_path / "output"
    run_dir, man = create_run_dir(output, cage_id="C57-023", mode="video")
    _write_mouse(run_dir, ordinal=1, cage_id="C57-023", weight=16.0, run_id=man["run_id"])
    _write_mouse(run_dir, ordinal=2, cage_id="C57-023", weight=16.5, run_id=man["run_id"])
    finish_run(run_dir)
    reg = MouseRegistry(tmp_path / "reg.json", output)
    meta = RecordsMetaStore(str(tmp_path / "meta.db"))
    result = verify_cages_view(reg, meta, output)
    assert result["total_cages"] == 1
    assert result["total_records"] == 2
    cage = result["cages"][0]
    assert cage["cage_id"] == "C57-023"
    assert cage["mean_weight"] == round((16.0 + 16.5) / 2, 2)


def test_verify_cages_excludes_published_and_deleted(tmp_path: Path):
    output = tmp_path / "output"
    run_dir, man = create_run_dir(output, cage_id="C57-023", mode="video")
    _write_mouse(run_dir, ordinal=1, cage_id="C57-023", weight=16.0, run_id=man["run_id"])
    _write_mouse(run_dir, ordinal=2, cage_id="C57-023", weight=16.5, run_id=man["run_id"])
    _write_mouse(run_dir, ordinal=3, cage_id="C57-023", weight=17.0, run_id=man["run_id"])
    finish_run(run_dir)
    reg = MouseRegistry(tmp_path / "reg.json", output)
    meta = RecordsMetaStore(str(tmp_path / "meta.db"))
    meta.publish("rec-C57-023-1")
    meta.soft_delete("rec-C57-023-2")
    result = verify_cages_view(reg, meta, output)
    assert result["total_records"] == 1
    assert result["cages"][0]["records"][0]["record_id"] == "rec-C57-023-3"


# ---------------------------------------------------------------------------
# Date boundary parsing (P1: date_to must include the full day)
# ---------------------------------------------------------------------------

def test_parse_date_to_extends_to_next_midnight():
    """date_to='2026-07-12' should become 2026-07-13 00:00 (exclusive upper)."""
    from datetime import datetime
    dt = _parse_date_to("2026-07-12")
    assert dt == datetime(2026, 7, 13, 0, 0, 0)


def test_parse_date_to_keeps_full_timestamp():
    """A full timestamp should be used as-is (not extended)."""
    from datetime import datetime
    dt = _parse_date_to("2026-07-12T15:30:00")
    assert dt == datetime(2026, 7, 12, 15, 30, 0)


def test_parse_date_from_is_inclusive_midnight():
    from datetime import datetime
    dt = _parse_date_from("2026-07-12")
    assert dt == datetime(2026, 7, 12, 0, 0, 0)


def test_collect_records_date_to_includes_full_day(tmp_path: Path):
    """Records at 12:00 on the date_to day must NOT be excluded."""
    output = tmp_path / "output"
    run_dir, man = create_run_dir(output, cage_id="C57-023", mode="video")
    session_dir = run_dir / "mouse_001"
    session_dir.mkdir(parents=True, exist_ok=True)
    img = np.zeros((20, 20, 3), dtype=np.uint8)
    cv2.imwrite(str(session_dir / "photo.jpg"), img)
    (session_dir / "record.json").write_text(json.dumps({
        "box_id": "C57-023", "cage_id": "C57-023", "weight": 16.0,
        "confidence": 0.9, "timestamp": "2026-07-12T15:30:00",
        "device": "scale01", "photo": "photo.jpg", "ordinal": 1,
        "run_id": man["run_id"], "record_id": "rec-late",
    }), encoding="utf-8")
    finish_run(run_dir)
    reg = MouseRegistry(tmp_path / "reg.json", output)
    meta = RecordsMetaStore(str(tmp_path / "meta.db"))
    # Same day for from and to - must include the 15:30 record
    items = collect_records(reg, meta, output, date_from="2026-07-12", date_to="2026-07-12")
    assert len(items) == 1
    assert items[0]["record_id"] == "rec-late"
