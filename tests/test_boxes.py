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
