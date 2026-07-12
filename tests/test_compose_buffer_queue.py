"""Tests for digit compose / buffer pin / upload queue / run isolation."""

from pathlib import Path

import numpy as np

from mousevision.buffer import RingFrameBuffer
from mousevision.reader.template import _compose_value
from mousevision.run import create_run_dir
from mousevision.types import Frame
from mousevision.upload_queue import UploadQueue, UploadStatus


def test_compose_rejects_two_digits_by_default():
    assert _compose_value(["2", "5"]) is None


def test_compose_two_digits_when_expected():
    assert _compose_value(["2", "5"], expected_digits=(2,)) == "25"


def test_compose_three_and_four():
    assert _compose_value(["1", "5", "0"]) == "1.50"
    assert _compose_value(["1", "6", "1", "5"]) == "16.15"


def test_buffer_pin_keeps_session_frames():
    buf = RingFrameBuffer(window_seconds=0.2, max_items=50)
    for i in range(20):
        img = np.zeros((10, 10, 3), dtype=np.uint8)
        fr = Frame(image=img, timestamp_ms=i * 100.0, index=i)
        if i == 5:
            buf.pin_from(fr.timestamp_ms)
        buf.push(fr, weight=float(i))
    indices = [f.index for f in buf.frames()]
    assert min(indices) <= 5
    assert 5 in indices
    assert buf.nearest_frame(5) is not None
    assert buf.nearest_frame(999).index == indices[-1]


def test_upload_queue_enqueue(tmp_path: Path):
    q = UploadQueue(tmp_path / "queue.db")
    rid = q.enqueue(
        {"box_id": "C57-001", "cage_id": "C57-023", "record_id": "r1", "weight": 16.15},
        record_path=tmp_path / "record.json",
        photo_path=tmp_path / "photo.jpg",
    )
    assert rid >= 1
    pending = q.list_pending()
    assert len(pending) == 1
    assert pending[0]["status"] == UploadStatus.PENDING.value
    q.mark_uploaded(rid)
    assert q.counts().get(UploadStatus.UPLOADED.value) == 1


def test_upload_queue_idempotent_on_record_id(tmp_path: Path):
    q = UploadQueue(tmp_path / "queue.db")
    payload = {"record_id": "same-uuid", "cage_id": "C57-023", "weight": 16.15}
    a = q.enqueue(payload, record_path=tmp_path / "a.json")
    b = q.enqueue(payload, record_path=tmp_path / "b.json")
    assert a == b
    assert len(q.list_pending()) == 1


def test_upload_queue_update_after_renumber(tmp_path: Path):
    """Renumber moved a record's dir; queue must reflect new path + ordinal."""
    import json

    q = UploadQueue(tmp_path / "queue.db")
    q.enqueue(
        {"record_id": "rec-A", "cage_id": "C57-023", "ordinal": 2, "actual_ordinal": 2},
        record_path=tmp_path / "run" / "mouse_002" / "record.json",
        photo_path=tmp_path / "run" / "mouse_002" / "photo.jpg",
    )
    updated = q.update_by_record_id(
        "rec-A",
        {"record_id": "rec-A", "cage_id": "C57-023", "ordinal": 5, "actual_ordinal": 5},
        record_path=tmp_path / "run" / "mouse_005" / "record.json",
        photo_path=tmp_path / "run" / "mouse_005" / "photo.jpg",
    )
    assert updated is True
    pending = q.list_pending()
    assert "mouse_005" in pending[0]["record_path"]
    payload = json.loads(q._connect().execute(
        "SELECT payload FROM upload_queue WHERE record_id=?", ("rec-A",)
    ).fetchone()["payload"])
    assert payload["actual_ordinal"] == 5


def test_upload_queue_delete_by_record_id(tmp_path: Path):
    """Deleting a record removes it from the sync queue (bug #4)."""
    q = UploadQueue(tmp_path / "queue.db")
    q.enqueue(
        {"record_id": "rec-D", "cage_id": "C57-023", "weight": 16.0},
        record_path=tmp_path / "r.json",
    )
    assert len(q.list_pending()) == 1
    n = q.delete_by_record_id("rec-D")
    assert n == 1
    assert len(q.list_pending()) == 0


def test_create_run_dir_isolated(tmp_path: Path):
    a, ma = create_run_dir(tmp_path, cage_id="C57-023")
    b, mb = create_run_dir(tmp_path, cage_id="C57-023")
    assert a != b
    assert ma["run_id"] != mb["run_id"]
    assert (a / "manifest.json").exists()
    assert ma["cage_id"] == "C57-023"
