"""Tests for the realtime attempt journal and finalize pipeline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mousevision.realtime import Attempt
from mousevision.realtime_journal import AttemptJournal, JournalMeta, journal_path
from mousevision.realtime_finalize import finalize_session


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_attempt(
    *,
    aid: str = "att1",
    weight: float = 23.48,
    state: str = "announced",
    seq: int = 10,
    ts: float = 1234.0,
) -> Attempt:
    return Attempt(
        attempt_id=aid,
        weight_g=weight,
        confidence=0.85,
        frame_seq=seq,
        client_ts_ms=ts,
        state=state,
        created_at=1700000000.0,
    )


# --------------------------------------------------------------------------- #
# AttemptJournal
# --------------------------------------------------------------------------- #


def test_journal_write_and_replay(tmp_path: Path) -> None:
    jpath = tmp_path / "journal.jsonl"
    j = AttemptJournal(jpath)

    j.write_meta(
        JournalMeta(
            session_id="s1",
            cage_id="CAGE1",
            project_id="default",
            created_at=1000.0,
            device_id="scale01",
        )
    )

    a1 = _make_attempt(aid="att1", weight=23.48)
    a2 = _make_attempt(aid="att2", weight=19.02)
    j.record_attempt(a1)
    j.record_attempt(a2)

    j.record_decision("att1", "accepted", 23.48)
    j.record_decision("att2", "rejected", 19.02)

    j.record_finish([a1], [a2])

    # The file exists and has 5 lines (meta + 2 attempts + 2 decisions + finish).
    lines = jpath.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 6
    for line in lines:
        assert json.loads(line)  # each line is valid JSON

    summary = AttemptJournal.read(jpath)
    assert summary["meta"]["cage_id"] == "CAGE1"
    assert set(summary["attempts"].keys()) == {"att1", "att2"}
    assert summary["decisions"] == {"att1": "accepted", "att2": "rejected"}
    assert summary["finished"] is True


def test_journal_read_missing_file(tmp_path: Path) -> None:
    summary = AttemptJournal.read(tmp_path / "nope.jsonl")
    assert summary["meta"] is None
    assert summary["attempts"] == {}
    assert summary["finished"] is False


def test_journal_path_layout(tmp_path: Path) -> None:
    p = journal_path(tmp_path, "abc123")
    assert p == tmp_path / "realtime_journal" / "abc123.jsonl"


# --------------------------------------------------------------------------- #
# finalize_session
# --------------------------------------------------------------------------- #


def test_finalize_creates_records_for_accepted_only(tmp_path: Path) -> None:
    jpath = journal_path(tmp_path, "s1")
    j = AttemptJournal(jpath)
    j.write_meta(
        JournalMeta(
            session_id="s1",
            cage_id="CAGE1",
            project_id="default",
            created_at=1000.0,
            device_id="scale01",
        )
    )

    accepted = [
        _make_attempt(aid="att1", weight=23.48),
        _make_attempt(aid="att3", weight=19.02, seq=20, ts=2000.0),
    ]
    rejected = [_make_attempt(aid="att2", weight=25.00, seq=15, ts=1500.0)]

    result = finalize_session(
        session_id="s1",
        output_root=tmp_path,
        journal=j,
        accepted=accepted,
        rejected=rejected,
        cage_id="CAGE1",
        project_id="default",
        device_id="scale01",
    )

    assert result["count"] == 2
    assert result["rejected_count"] == 1
    assert len(result["records"]) == 2

    # Two mouse dirs created with correct ordinals.
    run_dir = Path(result["run_dir"])
    assert (run_dir / "mouse_001").exists()
    assert (run_dir / "mouse_002").exists()
    assert not (run_dir / "mouse_003").exists()

    # record.json has realtime weight source (default ocr when not specified).
    rec1 = json.loads((run_dir / "mouse_001" / "record.json").read_text())
    assert rec1["weight"] == 23.48
    assert rec1["weight_source"] == "ocr"
    assert rec1["cage_id"] == "CAGE1"
    assert rec1["realtime_session_id"] == "s1"
    assert rec1["ordinal"] == 1

    # Manifest links the run to the session and lists rejected attempts.
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["mode"] == "realtime"
    assert manifest["realtime_session_id"] == "s1"
    assert manifest["record_count"] == 2
    assert manifest["weight_source"] == "ocr"
    assert manifest["status"] == "realtime_finalized"
    assert len(manifest["rejected_attempts"]) == 1
    assert manifest["rejected_attempts"][0]["attempt_id"] == "att2"

    # Journal was finalized.
    summary = AttemptJournal.read(jpath)
    assert summary["finished"] is True


def test_finalize_with_no_accepted(tmp_path: Path) -> None:
    """A session where the operator rejected everything should still produce
    a run dir (with zero records) and preserve the rejected attempts."""
    jpath = journal_path(tmp_path, "s2")
    j = AttemptJournal(jpath)
    j.write_meta(
        JournalMeta(
            session_id="s2",
            cage_id="CAGE2",
            project_id="default",
            created_at=2000.0,
            device_id="scale01",
        )
    )

    rejected = [_make_attempt(aid="att1", weight=23.48)]

    result = finalize_session(
        session_id="s2",
        output_root=tmp_path,
        journal=j,
        accepted=[],
        rejected=rejected,
        cage_id="CAGE2",
        project_id="default",
        device_id="scale01",
    )

    assert result["count"] == 0
    assert result["rejected_count"] == 1
    run_dir = Path(result["run_dir"])
    assert not (run_dir / "mouse_001").exists()

    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["record_count"] == 0
    assert len(manifest["rejected_attempts"]) == 1
