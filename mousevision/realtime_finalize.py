"""Finalize a realtime session into durable records.

When the operator finishes a realtime session, the accepted attempts must
become the source of truth — not a re-analysis of the full uploaded video.
This module reads the realtime :class:`~mousevision.realtime_journal.AttemptJournal`
and writes one ``mouse_NNN/record.json`` per accepted attempt under a run dir.

The uploaded video still enters the normal offline pipeline for *clip
extraction and training-data retention*, but the official weight and the
mouse count come from the operator's real-time decisions, not from a
second-pass video analysis.

Rejected attempts are NOT re-analysed: their time ranges are written to the
run manifest so the offline pipeline can skip them and avoid counting a
re-weighed mouse twice.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from mousevision.realtime import Attempt
from mousevision.realtime_journal import AttemptJournal, JournalMeta
from mousevision.run import (
    bump_record_count,
    create_run_dir,
    finish_run,
    write_manifest,
)
from mousevision.upload_queue import UploadQueue, UploadStatus

log = logging.getLogger("realtime_finalize")


def finalize_session(
    *,
    session_id: str,
    output_root: str | Path,
    journal: AttemptJournal,
    accepted: list[Attempt],
    rejected: list[Attempt],
    cage_id: str,
    project_id: str,
    device_id: str = "scale01",
    upload_queue: UploadQueue | None = None,
    video_upload_job_id: str | None = None,
    capture_meta: dict[str, Any] | None = None,
    timing_summary: dict[str, Any] | None = None,
    weight_source: str = "ocr",
) -> dict[str, Any]:
    """Turn accepted attempts into durable records under a new run dir.

    Args:
        session_id: Realtime session id (links to the journal file).
        output_root: The app's output root (e.g. ``output/``).
        journal: The session's journal (so the finish event is persisted).
        accepted: Attempts the operator confirmed.
        rejected: Attempts the operator retried (kept for audit, not counted).
        cage_id / project_id / device_id: Run metadata.
        upload_queue: If provided, enqueue each accepted record for sync.
        video_upload_job_id: The job id of the uploaded full video, if any.
            Recorded in the manifest so the clip extractor can find the source.
        weight_source: Provenance of the weight value — ``"ocr"`` or
            ``"ble_k797"``. Stamped into every ``record.json`` and the run
            manifest so the source of truth survives restart/recovery and is
            auditable downstream (plan §8.2 / §16).

    Returns:
        Summary dict with ``run_dir``, ``records`` (paths), ``count``.
    """
    # Persist the finish event before mutating the filesystem so a crash
    # mid-finalize leaves the journal consistent (the operator decisions are
    # recoverable from the journal alone).
    journal.record_finish(accepted, rejected)

    run_dir, manifest = create_run_dir(
        output_root,
        cage_id=cage_id,
        mode="realtime",
        source_id=session_id,
        device_id=device_id,
        project_id=project_id,
    )

    records: list[Path] = []
    record_ids: list[str] = []
    for ordinal, attempt in enumerate(accepted, start=1):
        mouse_dir = run_dir / f"mouse_{ordinal:03d}"
        mouse_dir.mkdir(parents=True, exist_ok=False)
        record_id = attempt.attempt_id or str(uuid.uuid4())
        record = {
            "box_id": cage_id,
            "cage_id": cage_id,
            "project_id": project_id,
            "record_id": record_id,
            "weight": attempt.weight_g,
            "confidence": attempt.confidence,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "device": device_id,
            "photo": None,  # Photo attached later by offline clip extractor.
            "ordinal": ordinal,
            "actual_ordinal": ordinal,
            # Provenance of the weight value: "ocr" or "ble_k797". Carried from
            # the session so a BLE session's records are unambiguously tagged
            # ble_k797 (plan §8.2: final record must record weight_source).
            "weight_source": weight_source,
            "realtime_session_id": session_id,
            "attempt_frame_seq": attempt.frame_seq,
            "attempt_client_ts_ms": attempt.client_ts_ms,
            "attempt_created_at": attempt.created_at,
            "needs_review": False,
            "requires_manual_weight": False,
            "verification_method": "实时人机闭环确认",
        }
        if capture_meta:
            record["capture_meta"] = capture_meta
        (mouse_dir / "record.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        bump_record_count(run_dir)
        records.append(mouse_dir)
        record_ids.append(record_id)

        if upload_queue is not None and attempt.weight_g is not None:
            upload_queue.enqueue(
                record,
                mouse_dir / "record.json",
                photo_path=None,
                status=UploadStatus.HELD,
            )

    # Write rejected attempt time ranges so the offline pipeline can skip them.
    if rejected:
        manifest["rejected_attempts"] = [
            {
                "attempt_id": a.attempt_id,
                "weight_g": a.weight_g,
                "client_ts_ms": a.client_ts_ms,
                "frame_seq": a.frame_seq,
            }
            for a in rejected
        ]
    if video_upload_job_id:
        manifest["video_upload_job_id"] = video_upload_job_id
    manifest["realtime_session_id"] = session_id
    manifest["record_count"] = len(accepted)
    manifest["weight_source"] = weight_source
    if timing_summary:
        manifest["timing_summary"] = timing_summary
    write_manifest(run_dir, manifest)

    finish_run(run_dir, status="realtime_finalized")
    log.info(
        "realtime_finalize: session=%s run_dir=%s accepted=%d rejected=%d",
        session_id, run_dir.name, len(accepted), len(rejected),
    )

    return {
        "run_dir": str(run_dir),
        "records": [str(p) for p in records],
        "record_ids": record_ids,
        "count": len(accepted),
        "rejected_count": len(rejected),
    }
