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


def test_renumber_rolls_back_on_failure(tmp_path: Path, monkeypatch):
    """A mid-renumber failure restores original dirs; no .renum left behind."""
    run = tmp_path / "run_rb"
    run.mkdir()
    for i in range(1, 4):
        _make_record(run, i, 10.0 + i)

    original_state = sorted(p.name for p in run.glob("mouse_*"))

    # Sabotage the second-phase rename so renumber fails after temp rename.
    real_rename = Path.rename

    def failing_rename(self, target):
        if "mouse_005" in str(target):
            raise OSError("simulated disk failure")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", failing_rename)
    with pytest.raises(OSError):
        renumber_records(run, [1, 5, 6])

    # Rollback: original dirs restored, no .renum temps lingering.
    after = sorted(p.name for p in run.iterdir())
    assert after == original_state
    assert not any(p.name.endswith(".renum") for p in run.iterdir())
    # record.json content unchanged.
    r2 = json.loads((run / "mouse_002" / "record.json").read_text())
    assert r2["actual_ordinal"] == 2


def test_renumber_rollback_restores_completed_final(tmp_path: Path, monkeypatch):
    """Failure AFTER some finals completed: those finals move back too.

    [1,2,3] -> [1,3,5], failing at mouse_005. Before the fix, mouse_003.renum
    could not restore (mouse_003 already occupied by r2), losing mouse_002's
    content. The journal must roll back the completed final first.
    """
    run = tmp_path / "run_journal"
    run.mkdir()
    for i in range(1, 4):
        _make_record(run, i, 10.0 + i)

    original_names = sorted(p.name for p in run.glob("mouse_*"))
    real_rename = Path.rename

    def fail_at_5(self, target):
        if "mouse_005" in str(target):
            raise OSError("fail at final #3")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", fail_at_5)
    with pytest.raises(OSError):
        renumber_records(run, [1, 3, 5])

    after = sorted(p.name for p in run.iterdir() if not p.name.startswith("."))
    assert after == original_names  # all three originals restored
    # Content must be byte-identical to original (journal restores record.json).
    for i in range(1, 4):
        rec = json.loads((run / f"mouse_{i:03d}" / "record.json").read_text())
        assert rec["actual_ordinal"] == i
        assert rec["weight"] == 10.0 + i


def test_renumber_rollback_on_json_write_failure(tmp_path: Path, monkeypatch):
    """A record.json write failure mid-final-phase rolls back fully."""
    run = tmp_path / "run_jsonfail"
    run.mkdir()
    for i in range(1, 4):
        _make_record(run, i, 20.0 + i)

    real_write = Path.write_text
    calls = {"n": 0}

    def fail_second_write(self, data, encoding=None):
        calls["n"] += 1
        if calls["n"] == 2:  # second record.json write fails
            raise OSError("disk full")
        return real_write(self, data, encoding=encoding)

    monkeypatch.setattr(Path, "write_text", fail_second_write)
    with pytest.raises(OSError):
        renumber_records(run, [1, 5, 6])

    after = sorted(p.name for p in run.iterdir() if not p.name.startswith("."))
    assert after == ["mouse_001", "mouse_002", "mouse_003"]
    for i in range(1, 4):
        rec = json.loads((run / f"mouse_{i:03d}" / "record.json").read_text())
        assert rec["actual_ordinal"] == i  # original content restored


def test_restore_renumber_journal_recovers_crash(tmp_path: Path):
    """A persisted journal + partial state is recovered on startup."""
    from mousevision.run import restore_renumber_temps

    run = tmp_path / "run_crash_j"
    run.mkdir()
    for i in range(1, 4):
        _make_record(run, i, 10.0 + i)
    # Simulate: phase1 done (all in .renum), phase2 done for mouse_001 only,
    # journal persisted, then crash.
    import json as _json

    (run / "mouse_001").rename(run / "mouse_001.renum")
    (run / "mouse_002").rename(run / "mouse_002.renum")
    (run / "mouse_003").rename(run / "mouse_003.renum")
    (run / "mouse_001.renum").rename(run / "mouse_001")  # one final completed
    plan = [
        {"orig": "mouse_001", "temp": "mouse_001.renum", "final": "mouse_001",
         "new_ordinal": 1, "finalized": True, "record": None},
        {"orig": "mouse_002", "temp": "mouse_002.renum", "final": "mouse_005",
         "new_ordinal": 5, "finalized": False, "record": None},
        {"orig": "mouse_003", "temp": "mouse_003.renum", "final": "mouse_006",
         "new_ordinal": 6, "finalized": False, "record": None},
    ]
    (run / ".renum_journal.json").write_text(_json.dumps(plan), encoding="utf-8")

    restored = restore_renumber_temps(run)
    assert restored == 3
    after = sorted(p.name for p in run.iterdir() if not p.name.startswith("."))
    assert after == ["mouse_001", "mouse_002", "mouse_003"]


def test_restore_renumber_temps_recovers_crash(tmp_path: Path):
    """A leftover .renum dir from a crash is restored on startup."""
    from mousevision.run import restore_renumber_temps

    run = tmp_path / "run_crash"
    run.mkdir()
    _make_record(run, 1, 10.0)
    # Simulate: original renamed to .renum, no final created.
    (run / "mouse_001").rename(run / "mouse_001.renum")
    assert any(p.name.endswith(".renum") for p in run.iterdir())

    restored = restore_renumber_temps(run)
    assert restored == 1
    assert (run / "mouse_001").exists()
    assert not (run / "mouse_001.renum").exists()


def test_restore_renumber_temps_drops_orphan_when_final_exists(tmp_path: Path):
    """If a final dir already exists, the orphan temp is removed not clobbered."""
    from mousevision.run import restore_renumber_temps

    run = tmp_path / "run_orphan"
    run.mkdir()
    _make_record(run, 1, 10.0)  # good final dir
    (run / "mouse_001").with_name("mouse_001.renum").mkdir()  # stale temp
    (run / "mouse_001.renum" / "record.json").write_text("{}", encoding="utf-8")

    restored = restore_renumber_temps(run)
    assert restored == 1
    assert (run / "mouse_001").exists()
    # Good final preserved; temp removed.
    assert json.loads((run / "mouse_001" / "record.json").read_text())["weight"] == 10.0
    assert not (run / "mouse_001.renum").exists()
