"""Box registry + atomic ordinal reservation tests."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from ui.boxes import BoxRegistry, qr_payload, strain_from_cage


def test_strain_inference():
    assert strain_from_cage("C57-023") == "C57BL/6"
    assert strain_from_cage("BALB-001") == "BALB/c"
    assert strain_from_cage("X-1") == "其他"


def test_qr_payload_structure():
    payload = json.loads(qr_payload("Box-1", "study-a"))
    assert payload == {"v": 1, "project_id": "study-a", "cage_id": "Box-1"}


def test_reserve_ordinal_is_sequential(tmp_path: Path):
    reg = BoxRegistry(tmp_path / "boxes.db")
    reg.create(cage_id="C57-023", mouse_no_start=1)
    assert reg.reserve_ordinal("C57-023") == 1
    assert reg.reserve_ordinal("C57-023") == 2
    assert reg.reserve_ordinal("C57-023") == 3


def test_reserve_autocreates_box(tmp_path: Path):
    reg = BoxRegistry(tmp_path / "boxes.db")
    assert reg.reserve_ordinal("ADHOC-9") == 1
    box = reg.get("ADHOC-9")
    assert box is not None
    assert box["next_ordinal"] == 2


def test_release_ordinal_rolls_back_tail(tmp_path: Path):
    reg = BoxRegistry(tmp_path / "boxes.db")
    reg.create(cage_id="C57-023")
    first = reg.reserve_ordinal("C57-023")
    reg.release_ordinal("C57-023", first)
    # released tail is reusable
    assert reg.reserve_ordinal("C57-023") == first


def test_release_keeps_gap_when_not_tail(tmp_path: Path):
    reg = BoxRegistry(tmp_path / "boxes.db")
    reg.create(cage_id="C57-023")
    a = reg.reserve_ordinal("C57-023")  # 1
    reg.reserve_ordinal("C57-023")  # 2 (someone else)
    reg.release_ordinal("C57-023", a)  # no-op, not tail
    assert reg.reserve_ordinal("C57-023") == 3


def test_concurrent_reservations_no_duplicates(tmp_path: Path):
    reg = BoxRegistry(tmp_path / "boxes.db")
    reg.create(cage_id="C57-023")
    results: list[int] = []
    lock = threading.Lock()

    def worker():
        val = reg.reserve_ordinal("C57-023")
        with lock:
            results.append(val)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == list(range(1, 21))
    assert len(set(results)) == 20


# --------------------------------------------------------------------------- #
# Upgrade: seed next_ordinal from existing records (bug #1)
# --------------------------------------------------------------------------- #


def _write_legacy_record(output_root: Path, cage_id: str, ordinal: int) -> None:
    """Simulate a pre-boxes-db run directory with records on disk."""
    run_dir = output_root / f"run_old_{cage_id}_{ordinal}"
    run_dir.mkdir(parents=True)
    mouse_dir = run_dir / f"mouse_{ordinal:03d}"
    mouse_dir.mkdir()
    (mouse_dir / "photo.jpg").write_bytes(b"jpg")
    (mouse_dir / "record.json").write_text(
        json.dumps(
            {
                "cage_id": cage_id,
                "box_id": cage_id,
                "ordinal": ordinal,
                "actual_ordinal": ordinal,
                "weight": 16.0,
            }
        ),
        encoding="utf-8",
    )


def test_sync_from_records_seeds_next_ordinal(tmp_path: Path):
    """Empty boxes.db + existing records: sync must bump next_ordinal past history."""
    output = tmp_path / "output"
    _write_legacy_record(output, "C57-023", 1)
    _write_legacy_record(output, "C57-023", 2)
    _write_legacy_record(output, "C57-023", 8)

    reg = BoxRegistry(tmp_path / "boxes.db")
    bumped = reg.sync_from_records(output)
    assert "C57-023" in bumped
    assert bumped["C57-023"] == 9  # max(8) + 1

    # After sync, reservation must not collide with history.
    assert reg.reserve_ordinal("C57-023") == 9


def test_sync_is_monotonic_and_idempotent(tmp_path: Path):
    """A box already ahead of records is not lowered by sync."""
    output = tmp_path / "output"
    _write_legacy_record(output, "C57-023", 1)
    reg = BoxRegistry(tmp_path / "boxes.db")
    reg.create(cage_id="C57-023", mouse_no_start=50)  # next_ordinal=50
    bumped = reg.sync_from_records(output)
    assert "C57-023" not in bumped  # 2 < 50, no change
    assert reg.get("C57-023")["next_ordinal"] == 50
    # Running again is a no-op.
    assert reg.sync_from_records(output) == {}


def test_reserve_with_baseline_seeds_from_records(tmp_path: Path):
    """Reserve on a missing cage must seed from records when baseline given."""
    output = tmp_path / "output"
    _write_legacy_record(output, "C57-023", 5)
    reg = BoxRegistry(tmp_path / "boxes.db")
    first = reg.reserve_ordinal("C57-023", baseline_records=output)
    assert first == 6  # past the existing ordinal 5


def test_sync_handles_legacy_nested_layout(tmp_path: Path):
    """Old run layout: run_*/<stamp>_<box>/record.json with session_index.

    The current mouse_NNN layout did not exist in early versions; records used
    a flat <timestamp>_<box> folder with box_id + session_index fields.
    """
    output = tmp_path / "output"
    # Legacy nested layout (not mouse_NNN)
    legacy = output / "run_20260710_120125" / "20260710_114607_C57-002_23d6"
    legacy.mkdir(parents=True)
    (legacy / "photo.jpg").write_bytes(b"jpg")
    (legacy / "record.json").write_text(
        json.dumps(
            {
                "box_id": "C57-002",
                "weight": 17.22,
                "session_index": 2,
                # no ordinal / actual_ordinal / cage_id
            }
        ),
        encoding="utf-8",
    )
    reg = BoxRegistry(tmp_path / "boxes.db")
    bumped = reg.sync_from_records(output)
    assert "C57-002" in bumped
    assert bumped["C57-002"] == 3  # session_index 2 + 1
    assert reg.get("C57-002")["next_ordinal"] == 3


def test_sync_ordinal_fallback_matches_registry(tmp_path: Path):
    """Ordinal resolution: actual_ordinal > ordinal > session_index."""
    output = tmp_path / "output"
    # A record with both ordinal and session_index uses ordinal.
    rec_dir = output / "run_x" / "mouse_001"
    rec_dir.mkdir(parents=True)
    (rec_dir / "photo.jpg").write_bytes(b"jpg")
    (rec_dir / "record.json").write_text(
        json.dumps({"cage_id": "M1", "ordinal": 7, "session_index": 1}),
        encoding="utf-8",
    )
    reg = BoxRegistry(tmp_path / "boxes.db")
    bumped = reg.sync_from_records(output)
    assert bumped.get("M1") == 8  # ordinal 7 + 1, not session_index
