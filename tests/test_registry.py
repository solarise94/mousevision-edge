"""Registry sync across run-scoped layouts."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from mousevision.run import create_run_dir, finish_run
from ui.registry import MouseRegistry


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
                "record_id": f"rec-{ordinal}",
            }
        ),
        encoding="utf-8",
    )
    return session_dir


def test_registry_lists_mice_within_active_run_only(tmp_path: Path):
    output = tmp_path / "output"
    run_a, man_a = create_run_dir(output, cage_id="C57-023", mode="video")
    run_b, man_b = create_run_dir(output, cage_id="C57-023", mode="video")
    _write_mouse(run_a, ordinal=1, cage_id="C57-023", weight=16.15, run_id=man_a["run_id"])
    _write_mouse(run_a, ordinal=2, cage_id="C57-023", weight=17.22, run_id=man_a["run_id"])
    _write_mouse(run_b, ordinal=1, cage_id="C57-023", weight=16.15, run_id=man_b["run_id"])
    finish_run(run_a)
    finish_run(run_b)

    reg = MouseRegistry(tmp_path / "mice_registry.json", output)
    reg.set_active_run(man_a["run_id"], run_a)
    mice_a = reg.list_mice(run_id=man_a["run_id"])
    assert len(mice_a) == 2
    assert {m["ordinal"] for m in mice_a} == {1, 2}
    assert all(m["cage_id"] == "C57-023" for m in mice_a)

    mice_b = reg.list_mice(run_id=man_b["run_id"])
    assert len(mice_b) == 1
    assert mice_b[0]["weight"] == 16.15

    # Same weight in another run must not inflate active run.
    active = reg.list_mice()
    assert len(active) == 2


def test_register_idempotent_on_run_ordinal(tmp_path: Path):
    output = tmp_path / "output"
    run_dir, man = create_run_dir(output, cage_id="C57-023")
    out = _write_mouse(run_dir, ordinal=1, cage_id="C57-023", weight=16.15, run_id=man["run_id"])
    reg = MouseRegistry(tmp_path / "reg.json", output)
    a = reg.register(
        run_id=man["run_id"],
        run_dir=run_dir,
        cage_id="C57-023",
        ordinal=1,
        record_id="rec-1",
        weight=16.15,
        confidence=0.9,
        output_dir=out,
    )
    b = reg.register(
        run_id=man["run_id"],
        run_dir=run_dir,
        cage_id="C57-023",
        ordinal=1,
        record_id="rec-1",
        weight=99.0,
        confidence=0.1,
        output_dir=out,
    )
    assert a["ordinal"] == b["ordinal"] == 1
    assert a["weight"] == b["weight"] == 16.15


def test_registry_exposes_needs_review(tmp_path: Path):
    output = tmp_path / "output"
    run_dir, man = create_run_dir(output, cage_id="C57-023")
    session_dir = run_dir / "mouse_001"
    session_dir.mkdir(parents=True, exist_ok=True)
    img = np.zeros((20, 20, 3), dtype=np.uint8)
    cv2.imwrite(str(session_dir / "photo.jpg"), img)
    (session_dir / "record.json").write_text(
        json.dumps(
            {
                "cage_id": "C57-023",
                "weight": 16.15,
                "confidence": 0.9,
                "ordinal": 1,
                "run_id": man["run_id"],
                "record_id": "rec-1",
                "needs_review": True,
                "review_reason": "cluster_conflict:demo",
                "photo": "photo.jpg",
                "timestamp": "2026-07-10T12:00:00",
            }
        ),
        encoding="utf-8",
    )
    reg = MouseRegistry(tmp_path / "reg.json", output)
    mice = reg._mice_in_dir(run_dir, run_id=man["run_id"])
    assert mice[0]["needs_review"] is True
    assert "cluster_conflict" in mice[0]["review_reason"]
