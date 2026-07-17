"""Unit tests for full-video agent weighing path (mocked CPA)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mousevision.agent_weigh import (
    AgentSession,
    AgentWeighClient,
    AgentWeighError,
    AgentWeighResult,
    persist_agent_sessions,
    resolve_agent_config,
    retain_source_video,
    should_retain_source,
)
from mousevision.pipeline import WeighingPipeline, _resolved_weight_reader


def test_resolve_agent_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOUSEVISION_AGENT_BASE_URL", "http://example:9")
    monkeypatch.setenv("MOUSEVISION_AGENT_MODEL", "gemini-3-flash-agent")
    monkeypatch.setenv("MOUSEVISION_AGENT_API_KEY", "k" * 8)
    cfg = resolve_agent_config({"agent": {"max_upload_bytes": 1000}})
    assert cfg["base_url"] == "http://example:9"
    assert cfg["model"] == "gemini-3-flash-agent"
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
        model="gemini-3-flash-agent",
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
    assert (tmp_path / "mouse_001" / "record.json").is_file()
    assert (tmp_path / "mouse_002" / "record.json").is_file()
    # null weight skips enqueue; two non-null enqueued
    assert q.enqueue.call_count == 2


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
        model="gemini-3-flash-agent",
        input_mode="original",
        latency_s=0.5,
    )

    with patch.object(AgentWeighClient, "weigh_video", return_value=fake):
        pipe = WeighingPipeline(
            {
                "device_id": "scale01",
                "weight_reader": "agent",
                "retain_source_video": True,
                "agent": {"review_confidence": 0.7},
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
    assert man.get("status") == "completed"


def test_resolved_weight_reader_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOUSEVISION_WEIGHT_READER", "agent")
    assert _resolved_weight_reader({"weight_reader": "template"}) == "agent"


def test_agent_client_requires_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOUSEVISION_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("CPA_API_KEY", raising=False)
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"x")
    client = AgentWeighClient({"agent": {"api_key": ""}})
    with pytest.raises(AgentWeighError, match="API_KEY"):
        client.weigh_video(vid)
