"""Unit tests for full-video agent weighing path (mocked CPA)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mousevision.agent_weigh import (
    AGENT_PROMPT_VERSION,
    FULL_VIDEO_PROMPT,
    AgentEvidenceVote,
    AgentSession,
    AgentWeighClient,
    AgentWeighError,
    AgentWeighResult,
    _sessions_from_payload,
    persist_agent_sessions,
    resolve_agent_config,
    retain_source_video,
    should_retain_source,
)
from mousevision.pipeline import WeighingPipeline, _resolved_weight_reader
from mousevision.upload_queue import UploadQueue


def test_resolve_agent_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOUSEVISION_AGENT_BASE_URL", "http://example:9")
    monkeypatch.setenv("MOUSEVISION_AGENT_MODEL", "gemini-3-flash")
    monkeypatch.setenv("MOUSEVISION_AGENT_API_KEY", "k" * 8)
    cfg = resolve_agent_config({"agent": {"max_upload_bytes": 1000}})
    assert cfg["base_url"] == "http://example:9"
    assert cfg["model"] == "gemini-3-flash"
    assert cfg["api_key"] == "k" * 8
    assert cfg["max_upload_bytes"] == 1000
    assert cfg["light_transcode"]["min_fps"] >= 8


def test_retain_source_hardlink(tmp_path: Path) -> None:
    src = tmp_path / "in.mp4"
    src.write_bytes(b"fake-video-bytes")
    run = tmp_path / "run_x"
    run.mkdir()
    dest = retain_source_video(src, run, enabled=True)
    assert dest is not None
    assert dest.exists()
    assert dest.name.startswith("source")
    assert dest.read_bytes() == b"fake-video-bytes"


def test_should_retain_source_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOUSEVISION_RETAIN_SOURCE_VIDEO", "1")
    assert should_retain_source({}) is True
    monkeypatch.setenv("MOUSEVISION_RETAIN_SOURCE_VIDEO", "0")
    assert should_retain_source({"retain_source_video": True}) is False


def test_persist_agent_sessions(tmp_path: Path) -> None:
    result = AgentWeighResult(
        sessions=[
            AgentSession(1, 16.15, 0.95, "ok"),
            AgentSession(2, None, 0.0, "blur"),
            AgentSession(3, 17.57, 0.5, "low"),
        ],
        summary="3 sessions",
        model="gemini-3-flash",
        input_mode="original",
        latency_s=1.2,
    )
    q = MagicMock()
    records = persist_agent_sessions(
        result=result,
        run_dir=tmp_path,
        cage_id="0001",
        run_id="rid",
        device_id="scale01",
        project_id="p",
        start_ordinal=1,
        review_confidence=0.7,
        upload_queue=q,
    )
    assert len(records) == 3
    assert records[0]["weight"] == 16.15
    assert records[0]["weight_source"] == "agent_full_video"
    assert records[0]["needs_review"] is False
    assert records[1]["weight"] is None
    assert records[1]["needs_review"] is True
    assert records[1]["requires_manual_weight"] is True
    assert records[2]["needs_review"] is True  # low conf
    assert records[2]["weight"] is None
    assert records[2]["guessed_weight"] == 17.57
    assert (tmp_path / "mouse_001" / "record.json").is_file()
    assert (tmp_path / "mouse_002" / "record.json").is_file()
    # Null and low-confidence weights are manual-only; only accepted rows queue.
    assert q.enqueue.call_count == 1


def test_pipeline_agent_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 64)
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setenv("MOUSEVISION_WEIGHT_READER", "agent")
    monkeypatch.setenv("MOUSEVISION_RETAIN_SOURCE_VIDEO", "1")

    fake = AgentWeighResult(
        sessions=[
            AgentSession(1, 16.15, 0.95, "a"),
            AgentSession(2, 17.22, 0.9, "b"),
        ],
        summary="ok",
        model="gemini-3-flash",
        input_mode="original",
        latency_s=0.5,
    )

    with patch.object(AgentWeighClient, "weigh_video", return_value=fake):
        pipe = WeighingPipeline(
            {
                "device_id": "scale01",
                "weight_reader": "agent",
                "retain_source_video": True,
                "agent": {"review_confidence": 0.7, "photo_gate": False},
            },
            tmp_path,
        )
        result = pipe.run_video(
            video,
            cage_id="0001",
            output_root=out,
            create_run=True,
            persist=True,
            stop_after_first=False,
            start_ordinal=1,
        )

    assert result.records is not None
    assert len(result.records) == 2
    assert [r["weight"] for r in result.records] == [16.15, 17.22]
    assert result.states == ["AGENT"]
    assert result.run_dir is not None
    # retained source
    sources = list(result.run_dir.glob("source*"))
    assert sources, "expected run_*/source.*"
    man = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert man.get("source_retained") is True
    assert man.get("weight_reader") == "agent"
    assert man.get("agent_prompt_version") == AGENT_PROMPT_VERSION
    assert man.get("status") == "completed"


def test_pipeline_queues_only_after_local_evidence_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"not-a-real-video")
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setenv("MOUSEVISION_WEIGHT_READER", "agent")
    fake = AgentWeighResult(
        sessions=[AgentSession(1, 23.98, 0.95, "agent accepted")],
        model="gemini-3-flash",
    )

    def reject_evidence(**kwargs):
        records = kwargs["records"]
        records[0]["guessed_weight"] = records[0]["weight"]
        records[0]["weight"] = None
        records[0]["needs_review"] = True
        records[0]["requires_manual_weight"] = True
        records[0]["review_reason"] = "agent_photo_weight_mismatch"
        return records

    with (
        patch.object(AgentWeighClient, "weigh_video", return_value=fake),
        patch("mousevision.pipeline.attach_agent_evidence", side_effect=reject_evidence),
    ):
        result = WeighingPipeline(
            {
                "device_id": "scale01",
                "weight_reader": "agent",
                "retain_source_video": True,
                "agent": {"attach_photos": True},
            },
            tmp_path,
        ).run_video(
            video,
            cage_id="0001",
            output_root=out,
            create_run=True,
            persist=True,
            stop_after_first=False,
        )

    assert result.records is not None
    assert result.records[0]["requires_manual_weight"] is True
    assert UploadQueue(out / "upload_queue.db").list_held() == []


def test_resolved_weight_reader_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOUSEVISION_WEIGHT_READER", "agent")
    assert _resolved_weight_reader({"weight_reader": "template"}) == "agent"


def test_sessions_from_payload_parses_time_anchors() -> None:
    payload = {
        "sessions": [
            {
                "ordinal": 1,
                "weight_g": 16.15,
                "confidence": 0.9,
                "note": "ok",
                "t_start_s": 3.2,
                "t_end_s": 7.8,
                "t_stable_s": 5.0,
            },
            {
                "ordinal": 2,
                "weight_g": None,
                "confidence": 0.0,
                "note": "blur",
                "t_start_s": "not-a-number",
                "start_s": 12.5,  # alias
                "end_s": 15.0,  # alias
            },
            {"ordinal": 3, "weight_g": 17.5, "confidence": 0.5, "note": "n"},
        ]
    }
    sessions = _sessions_from_payload(payload)
    assert len(sessions) == 3
    assert sessions[0].t_start_s == 3.2
    assert sessions[0].t_end_s == 7.8
    assert sessions[0].t_stable_s == 5.0
    # Bad t_start_s -> None; aliases parsed.
    assert sessions[1].t_start_s is None
    assert sessions[1].t_end_s == 15.0
    assert sessions[1].t_stable_s is None
    # No time fields -> all None
    assert sessions[2].t_start_s is None
    assert sessions[2].t_end_s is None


def test_sessions_from_payload_rejects_out_of_range_weight() -> None:
    payload = {
        "sessions": [
            {"ordinal": 1, "weight_g": 200.0, "confidence": 0.9, "note": "ok"},
            {"ordinal": 2, "weight_g": 16.5, "confidence": 0.9, "note": "ok"},
        ]
    }
    sessions = _sessions_from_payload(payload)
    assert sessions[0].weight_g is None  # 200g rejected
    assert sessions[0].confidence == 0.0
    assert "weight_out_of_range" in sessions[0].note
    assert sessions[1].weight_g == 16.5  # normal weight kept


def test_sessions_from_payload_fixes_inverted_time_anchors() -> None:
    payload = {
        "sessions": [
            {
                "ordinal": 1,
                "weight_g": 17.0,
                "confidence": 0.9,
                "t_start_s": 10.0,
                "t_end_s": 5.0,
                "t_stable_s": 7.0,
            },
        ]
    }
    sessions = _sessions_from_payload(payload)
    assert sessions[0].t_start_s == 5.0  # swapped
    assert sessions[0].t_end_s == 10.0
    assert sessions[0].t_stable_s == 7.0  # within range, unchanged
    assert "time_anchors_inverted" in sessions[0].note


def test_sessions_from_payload_clamps_t_stable() -> None:
    payload = {
        "sessions": [
            {
                "ordinal": 1,
                "weight_g": 17.0,
                "confidence": 0.9,
                "t_start_s": 5.0,
                "t_end_s": 10.0,
                "t_stable_s": 15.0,  # after t_end
            },
        ]
    }
    sessions = _sessions_from_payload(payload)
    assert sessions[0].t_stable_s == 10.0  # clamped to t_end
    assert "t_stable_after_t_end" in sessions[0].note


def test_sessions_from_payload_clamps_confidence() -> None:
    payload = {
        "sessions": [
            {"ordinal": 1, "weight_g": 17.0, "confidence": 1.5},
        ]
    }
    sessions = _sessions_from_payload(payload)
    assert sessions[0].confidence == 1.0


def test_agent_evidence_consensus_is_canonical() -> None:
    payload = {
        "sessions": [
            {
                "ordinal": 1,
                "weight_g": 23.49,
                "confidence": 0.95,
                "t_start_s": 31.5,
                "t_end_s": 36.0,
                "stable_start_s": 33.0,
                "stable_end_s": 35.5,
                "t_stable_s": 34.0,
                "evidence": [
                    {"timestamp_s": 33.2, "weight_g": 23.48, "mouse_present": True, "display_readable": True},
                    {"timestamp_s": 34.0, "weight_g": 23.49, "mouse_present": True, "display_readable": True},
                    {"timestamp_s": 35.0, "weight_g": 23.50, "mouse_present": True, "display_readable": True},
                ],
            }
        ]
    }
    sess = _sessions_from_payload(payload)[0]
    assert sess.weight_g == 23.49
    assert sess.evidence_consensus_g == 23.49
    assert sess.review_reasons == []
    assert len(sess.evidence) == 3


def test_prompt_v2_requires_three_interior_median_votes() -> None:
    assert AGENT_PROMPT_VERSION == "weighing-evidence-v2"
    assert "evidence 必须恰好给 3 项" in FULL_VIDEO_PROMPT
    assert "中位数" in FULL_VIDEO_PROMPT
    assert "0.4–1.0 秒" in FULL_VIDEO_PROMPT


def test_agent_single_frame_peak_fails_closed() -> None:
    """Regression: the 23.98 single-frame peak must not beat 23.48/23.49."""
    payload = {
        "sessions": [
            {
                "ordinal": 5,
                "weight_g": 23.98,
                "confidence": 0.95,
                "t_start_s": 31.5,
                "t_end_s": 36.0,
                "evidence": [
                    {"timestamp_s": 33.5, "weight_g": 23.48, "mouse_present": True, "display_readable": True},
                    {"timestamp_s": 34.0, "weight_g": 23.49, "mouse_present": True, "display_readable": True},
                    {"timestamp_s": 34.5, "weight_g": 23.98, "mouse_present": True, "display_readable": True},
                ],
            }
        ]
    }
    sess = _sessions_from_payload(payload)[0]
    assert sess.weight_g == 23.49
    assert "agent_evidence_multimodal" in sess.review_reasons
    assert "agent_report_vote_mismatch" in sess.review_reasons


def test_agent_missing_evidence_requires_review() -> None:
    sess = _sessions_from_payload(
        {"sessions": [{"ordinal": 1, "weight_g": 18.91, "confidence": 0.99}]}
    )[0]
    assert "agent_insufficient_evidence" in sess.review_reasons


def test_persist_gated_agent_result_is_manual_and_not_queued(tmp_path: Path) -> None:
    result = AgentWeighResult(
        sessions=[
            AgentSession(
                1,
                23.49,
                0.95,
                "single peak",
                evidence=[
                    AgentEvidenceVote(33.5, 23.48, True, True),
                    AgentEvidenceVote(34.0, 23.49, True, True),
                    AgentEvidenceVote(34.5, 23.98, True, True),
                ],
                reported_weight_g=23.98,
                evidence_consensus_g=23.49,
                review_reasons=["agent_evidence_multimodal"],
            )
        ]
    )
    q = MagicMock()
    records = persist_agent_sessions(
        result=result,
        run_dir=tmp_path,
        cage_id="0001",
        run_id="rid",
        device_id="scale01",
        project_id="p",
        start_ordinal=1,
        upload_queue=q,
    )
    assert records[0]["weight"] is None
    assert records[0]["guessed_weight"] == 23.49
    assert records[0]["requires_manual_weight"] is True
    assert "agent_evidence_multimodal" in records[0]["review_reason"]
    q.enqueue.assert_not_called()


def test_resolve_agent_config_attach_defaults() -> None:
    cfg = resolve_agent_config({})
    assert cfg["attach_photos"] is True
    assert cfg["photo_sample_interval_ms"] == 200.0
    assert cfg["photo_pad_s"] == 1.5
    assert cfg["photo_weight_tol"] == 0.25
    assert cfg["photo_window_s"] == 5.0
    cfg2 = resolve_agent_config({"agent": {"attach_photos": False, "photo_window_s": 9.0}})
    assert cfg2["attach_photos"] is False
    assert cfg2["photo_window_s"] == 9.0


def test_agent_client_requires_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOUSEVISION_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("CPA_API_KEY", raising=False)
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"x")
    client = AgentWeighClient({"agent": {"api_key": ""}})
    with pytest.raises(AgentWeighError, match="API_KEY"):
        client.weigh_video(vid)
