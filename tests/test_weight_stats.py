"""Tests for weight statistics and verify-cages grouping."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from mousevision.run import create_run_dir, finish_run
from ui.records_api import _compute_weight_stats, verify_cages_view
from ui.records_meta import RecordsMetaStore
from ui.registry import MouseRegistry


# ---------------------------------------------------------------------------
# _compute_weight_stats — pure function, no fixtures needed
# ---------------------------------------------------------------------------

def test_weight_stats_empty():
    ws = _compute_weight_stats([])
    assert ws["n"] == 0
    assert ws["mean"] is None
    assert ws["out_of_range"] == 0
    assert ws["fit_x"] == []


def test_weight_stats_basic():
    ws = _compute_weight_stats([16.0, 16.5, 17.0, 15.5, 16.2])
    assert ws["n"] == 5
    assert ws["mean"] == round((16.0 + 16.5 + 17.0 + 15.5 + 16.2) / 5, 2)
    assert ws["min"] == 15.5
    assert ws["max"] == 17.0
    assert ws["range"] == 1.5
    # SEM = std(ddof=1)/sqrt(n)
    import numpy as np
    expected_sem = round(np.std([16.0, 16.5, 17.0, 15.5, 16.2], ddof=1) / np.sqrt(5), 2)
    assert ws["sem"] == expected_sem


def test_weight_stats_threshold_and_outliers():
    # mean = 16.3, threshold = 14.3–18.3; 20.0 is out of range
    ws = _compute_weight_stats([16.0, 16.5, 16.4, 20.0])
    assert ws["threshold_low"] == round(ws["mean"] - 2.0, 2)
    assert ws["threshold_high"] == round(ws["mean"] + 2.0, 2)
    assert ws["out_of_range"] == 1


def test_weight_stats_histogram_and_fit_shape():
    ws = _compute_weight_stats([16.0, 16.2, 16.5, 16.8, 17.0])
    assert len(ws["hist_counts"]) == len(ws["hist_bins"]) - 1
    assert sum(ws["hist_counts"]) == 5
    assert len(ws["fit_x"]) == 40
    assert len(ws["fit_y"]) == 40
    # Fit curve should peak near the mean
    peak_idx = ws["fit_y"].index(max(ws["fit_y"]))
    assert abs(ws["fit_x"][peak_idx] - ws["mean"]) < 0.6


def test_weight_stats_single_sample():
    ws = _compute_weight_stats([16.5])
    assert ws["n"] == 1
    assert ws["mean"] == 16.5
    assert ws["sem"] == 0.0  # no variance with single sample
    assert ws["std"] == 0.0
    assert ws["out_of_range"] == 0


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
                "box_id": cage_id,
                "cage_id": cage_id,
                "weight": weight,
                "confidence": 0.9,
                "timestamp": "2026-07-10T12:00:00",
                "device": "scale01",
                "photo": "photo.jpg",
                "ordinal": ordinal,
                "run_id": run_id,
                "record_id": f"rec-{cage_id}-{ordinal}",
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
    assert cage["count"] == 2
    assert cage["mean_weight"] == round((16.0 + 16.5) / 2, 2)
    assert len(cage["records"]) == 2


def test_verify_cages_excludes_published_and_deleted(tmp_path: Path):
    output = tmp_path / "output"
    run_dir, man = create_run_dir(output, cage_id="C57-023", mode="video")
    _write_mouse(run_dir, ordinal=1, cage_id="C57-023", weight=16.0, run_id=man["run_id"])
    _write_mouse(run_dir, ordinal=2, cage_id="C57-023", weight=16.5, run_id=man["run_id"])
    _write_mouse(run_dir, ordinal=3, cage_id="C57-023", weight=17.0, run_id=man["run_id"])
    finish_run(run_dir)

    reg = MouseRegistry(tmp_path / "reg.json", output)
    meta = RecordsMetaStore(str(tmp_path / "meta.db"))
    # Publish #1, delete #2 — only #3 should remain pending
    meta.publish("rec-C57-023-1")
    meta.soft_delete("rec-C57-023-2")

    result = verify_cages_view(reg, meta, output)
    assert result["total_records"] == 1
    cage = result["cages"][0]
    assert cage["records"][0]["record_id"] == "rec-C57-023-3"


def test_verify_cages_multiple_cages(tmp_path: Path):
    output = tmp_path / "output"
    run_a, man_a = create_run_dir(output, cage_id="C57-023", mode="video")
    _write_mouse(run_a, ordinal=1, cage_id="C57-023", weight=16.0, run_id=man_a["run_id"])
    finish_run(run_a)
    run_b, man_b = create_run_dir(output, cage_id="C57-045", mode="video")
    _write_mouse(run_b, ordinal=1, cage_id="C57-045", weight=18.0, run_id=man_b["run_id"])
    finish_run(run_b)

    reg = MouseRegistry(tmp_path / "reg.json", output)
    meta = RecordsMetaStore(str(tmp_path / "meta.db"))

    result = verify_cages_view(reg, meta, output)
    assert result["total_cages"] == 2
    cage_ids = [c["cage_id"] for c in result["cages"]]
    assert "C57-023" in cage_ids
    assert "C57-045" in cage_ids


def test_verify_cages_empty_when_all_published(tmp_path: Path):
    output = tmp_path / "output"
    run_dir, man = create_run_dir(output, cage_id="C57-023", mode="video")
    _write_mouse(run_dir, ordinal=1, cage_id="C57-023", weight=16.0, run_id=man["run_id"])
    finish_run(run_dir)

    reg = MouseRegistry(tmp_path / "reg.json", output)
    meta = RecordsMetaStore(str(tmp_path / "meta.db"))
    meta.publish("rec-C57-023-1")

    result = verify_cages_view(reg, meta, output)
    assert result["total_cages"] == 0
    assert result["cages"] == []
