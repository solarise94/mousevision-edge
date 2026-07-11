"""Record renumbering for multi-detect ordinal reassignment (design §3.5.2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mousevision.run import renumber_records


def _make_record(run_dir: Path, ordinal: int, weight: float) -> None:
    d = run_dir / f"mouse_{ordinal:03d}"
    d.mkdir(parents=True)
    (d / "record.json").write_text(
        json.dumps(
            {
                "cage_id": "C57-1",
                "ordinal": ordinal,
                "actual_ordinal": ordinal,
                "requested_ordinal": 1,
                "weight": weight,
            }
        ),
        encoding="utf-8",
    )
    (d / "photo.jpg").write_bytes(b"jpg")


def test_renumber_overlapping_range(tmp_path: Path):
    run = tmp_path / "run_x"
    run.mkdir()
    for i in range(1, 4):  # ordinals 1,2,3
        _make_record(run, i, 10.0 + i)

    # keep first at 1, move 2,3 to freshly reserved 5,6
    renumber_records(run, [1, 5, 6])

    names = sorted(p.name for p in run.glob("mouse_*"))
    assert names == ["mouse_001", "mouse_005", "mouse_006"]

    r1 = json.loads((run / "mouse_001" / "record.json").read_text())
    r5 = json.loads((run / "mouse_005" / "record.json").read_text())
    r6 = json.loads((run / "mouse_006" / "record.json").read_text())
    assert r1["weight"] == 11.0 and r1["actual_ordinal"] == 1
    assert r5["weight"] == 12.0 and r5["actual_ordinal"] == 5
    assert r6["weight"] == 13.0 and r6["actual_ordinal"] == 6


def test_renumber_adjacent_shift_no_collision(tmp_path: Path):
    run = tmp_path / "run_y"
    run.mkdir()
    for i in range(1, 5):  # 1..4
        _make_record(run, i, 20.0 + i)
    # first stays 1; 2,3,4 -> 3,4,5 (overlaps old 3,4)
    renumber_records(run, [1, 3, 4, 5])
    names = sorted(p.name for p in run.glob("mouse_*"))
    assert names == ["mouse_001", "mouse_003", "mouse_004", "mouse_005"]
    # weights preserved in ascending mapping
    assert json.loads((run / "mouse_003" / "record.json").read_text())["weight"] == 22.0
    assert json.loads((run / "mouse_005" / "record.json").read_text())["weight"] == 24.0


def test_renumber_count_mismatch_raises(tmp_path: Path):
    run = tmp_path / "run_z"
    run.mkdir()
    _make_record(run, 1, 10.0)
    with pytest.raises(ValueError):
        renumber_records(run, [1, 2])
