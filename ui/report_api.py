"""Device-direct weighing report endpoint (pure-app transformation).

The phone performs weighing judgement locally and POSTs only the final
records to the server for aggregation. The server no longer re-judges
weights; it just durably persists the reported records using the same
on-disk layout as ``realtime_finalize`` (``run_*/mouse_NNN/record.json``
+ sibling ``photo.jpg``), so they appear in every existing page that
reads from the registry / ``collect_records``.

Endpoint: ``POST /api/records/report`` (multipart/form-data).
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from mousevision.run import (
    bump_record_count,
    create_run_dir,
    finish_run,
    write_manifest,
)
from mousevision.upload_queue import UploadStatus
from ui.auth import require_api_token

log = logging.getLogger("report_api")

router = APIRouter()

# Upper bound for a single weight value in grams. Matches the spec's
# [0, 6553.5] envelope (uint16 decigram representation).
_MAX_WEIGHT_G = 6553.5
_PLACEHOLDER_W, _PLACEHOLDER_H = 640, 480


# --------------------------------------------------------------------------- #
# Wiring: the app calls configure() once at import time to hand over the
# same singletons (registry / records_meta / upload_queue / output_root)
# that the rest of the UI reads from, so reported records are visible
# everywhere without duplication.
# --------------------------------------------------------------------------- #

_registry: Any = None
_records_meta: Any = None
_upload_queue: Any = None
_output_root: Path = Path(".")


def configure(
    registry: Any,
    records_meta: Any,
    upload_queue: Any,
    output_root: str | Path,
) -> None:
    global _registry, _records_meta, _upload_queue, _output_root
    _registry = registry
    _records_meta = records_meta
    _upload_queue = upload_queue
    _output_root = Path(output_root)


def _existing_record_dir_for(record_id: str) -> Path | None:
    """Locate the on-disk mouse dir that already owns ``record_id``.

    Used for idempotency: a repeated upload of the same record_id returns
    the existing record instead of creating a duplicate run/mouse dir.
    Scans manifests' record.json files; output/ is small and run-scoped
    so this is cheap in practice.
    """
    if not record_id or _registry is None:
        return None
    try:
        mouse = _registry.get_by_record_id(record_id)
    except Exception:
        return None
    if mouse is None:
        return None
    try:
        p = _output_root / mouse["dir"]
    except Exception:
        return None
    return p if p.is_dir() else None


def _is_finite_weight(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    return not (math.isnan(f) or math.isinf(f))


def _validate_records(raw_records: Any) -> list[dict[str, Any]]:
    """Validate and normalize the parsed ``records`` payload.

    Returns the normalized list (record_id defaulted, types coerced).
    Raises ``HTTPException(400)`` with a concrete reason on any failure.
    """
    if not isinstance(raw_records, list):
        raise HTTPException(status_code=400, detail="records 必须是 JSON 数组")
    if not raw_records:
        raise HTTPException(status_code=400, detail="records 不能为空")

    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for i, item in enumerate(raw_records):
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=400, detail=f"records[{i}] 必须是 JSON 对象"
            )
        weight = item.get("weight_g")
        if not _is_finite_weight(weight):
            raise HTTPException(
                status_code=400,
                detail=f"records[{i}].weight_g 非法（必须为有限数字）",
            )
        w = float(weight)
        if w < 0 or w > _MAX_WEIGHT_G:
            raise HTTPException(
                status_code=400,
                detail=f"records[{i}].weight_g 超出范围 [0, {_MAX_WEIGHT_G}]",
            )

        ordinal = item.get("ordinal")
        try:
            ord_int = int(ordinal) if ordinal is not None else 0
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400, detail=f"records[{i}].ordinal 必须是整数"
            )
        if ord_int <= 0:
            raise HTTPException(
                status_code=400, detail=f"records[{i}].ordinal 必须是正整数"
            )

        record_id = str(item.get("record_id") or "").strip() or str(uuid.uuid4())
        if record_id in seen:
            # Same-batch duplicate: keep first, skip later occurrences.
            continue
        seen.add(record_id)

        normalized.append(
            {
                "record_id": record_id,
                "ordinal": ord_int,
                "weight_g": round(w, 2),
                "weight_raw": item.get("weight_raw"),
                "recorded_at": item.get("recorded_at"),
                "clip_start_ms": item.get("clip_start_ms"),
                "clip_end_ms": item.get("clip_end_ms"),
            }
        )
    if not normalized:
        raise HTTPException(status_code=400, detail="records 去重后为空")
    return normalized


def _video_suffix(filename: str | None, content_type: str | None) -> str:
    name_suffix = Path(filename or "").suffix.lower()
    if name_suffix in {".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv"}:
        return name_suffix
    ct = (content_type or "").lower()
    if ct == "video/mp4":
        return ".mp4"
    if ct == "video/webm":
        return ".webm"
    if ct == "video/quicktime":
        return ".mov"
    return ".mp4"


def _extract_video_frame(
    video_path: Path, target_ms: float | None
) -> np.ndarray | None:
    """Decode a single BGR frame at ``target_ms`` (or video midpoint).

    Returns ``None`` if the video cannot be opened or no frame decodes.
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return None
    try:
        if target_ms is not None and target_ms >= 0:
            cap.set(cv2.CAP_PROP_POS_MSEC, float(target_ms))
            ok, img = cap.read()
            if ok and img is not None:
                return img
        # Fallback: midpoint frame.
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        if n > 0 and fps > 0:
            mid_ms = (n / 2.0) / fps * 1000.0
            cap.set(cv2.CAP_PROP_POS_MSEC, float(mid_ms))
            ok, img = cap.read()
            if ok and img is not None:
                return img
        # Final fallback: very first frame.
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ok, img = cap.read()
        if ok and img is not None:
            return img
        return None
    finally:
        cap.release()


def _write_placeholder_photo(path: Path, *, label: str) -> bool:
    """Generate a 640x480 grey placeholder JPEG with a short caption."""
    img = np.full((_PLACEHOLDER_H, _PLACEHOLDER_W, 3), 200, dtype=np.uint8)
    cv2.putText(
        img,
        "无视频证据 device_report",
        (40, 240),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        (30, 30, 30),
        2,
        cv2.LINE_AA,
    )
    if label:
        cv2.putText(
            img,
            label,
            (40, 290),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (60, 60, 60),
            1,
            cv2.LINE_AA,
        )
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        return False
    path.write_bytes(buf.tobytes())
    return True


def _write_frame_photo(path: Path, frame: np.ndarray) -> bool:
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    if not ok:
        return False
    path.write_bytes(buf.tobytes())
    return True


@router.post("/api/records/report", dependencies=[Depends(require_api_token)])
async def report_records(
    cage_id: str = Form(...),
    project_id: str = Form("default"),
    device_id: str = Form("unknown"),
    weight_source: str = Form("device_report"),
    strain: str | None = Form(None),
    records: str = Form(...),
    video: UploadFile | None = File(None),
) -> JSONResponse:
    """Receive a batch of device-judged weighing records.

    One successful upload = one run directory. Each reported record lands
    in ``mouse_NNN/`` with ``record.json`` + ``photo.jpg`` and (if a video
    was attached) the evidence video is stored once at run scope.
    """
    cage = (cage_id or "").strip()
    if not cage:
        raise HTTPException(status_code=400, detail="cage_id 不能为空")
    project = (project_id or "default").strip() or "default"
    device = (device_id or "unknown").strip() or "unknown"
    wsrc = (weight_source or "device_report").strip() or "device_report"

    try:
        records_payload = json.loads(records)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=400, detail=f"records 不是合法 JSON: {exc}"
        ) from exc

    normalized = _validate_records(records_payload)

    # Idempotency: split out record_ids that already exist on disk so we
    # do NOT create duplicate runs / mouse dirs for them.
    skipped: list[str] = []
    to_write: list[dict[str, Any]] = []
    for rec in normalized:
        rid = rec["record_id"]
        if _existing_record_dir_for(rid) is not None:
            skipped.append(rid)
        else:
            to_write.append(rec)

    # Persist the uploaded evidence video (if any) into a temp path first;
    # only move it into the run dir when we actually create a run.
    uploaded_video_path: Path | None = None
    video_suffix = ".mp4"
    if video is not None and to_write:
        video_suffix = _video_suffix(video.filename, video.content_type)
        tmp_video = _output_root / f".report_upload_{uuid.uuid4().hex}{video_suffix}"
        size = 0
        try:
            with tmp_video.open("wb") as handle:
                while True:
                    chunk = await video.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    handle.write(chunk)
            if size == 0:
                tmp_video.unlink(missing_ok=True)
                tmp_video = None  # type: ignore[assignment]
        except Exception:
            tmp_video.unlink(missing_ok=True)
            raise
        uploaded_video_path = tmp_video if (tmp_video and tmp_video.exists()) else None
    elif video is not None:
        # Nothing to write (everything skipped) but a video was uploaded:
        # drain and discard so the multipart body is fully consumed.
        await video.read()

    if not to_write:
        # All duplicates: do not create an empty run.
        return JSONResponse(
            {
                "ok": True,
                "run_id": None,
                "run_dir": None,
                "count": 0,
                "record_ids": [],
                "photos_extracted": 0,
                "skipped": skipped,
                "message": "全部记录已存在，跳过",
            }
        )

    # Create one run for this report. mode=device_report keeps it distinct
    # from realtime / video analysis runs and avoids ordinal collisions.
    run_dir, manifest = create_run_dir(
        _output_root,
        cage_id=cage,
        mode="device_report",
        source_id=device,
        device_id=device,
        project_id=project,
    )
    run_id = str(manifest["run_id"])

    evidence_rel: str | None = None
    if uploaded_video_path is not None:
        target_video = run_dir / f"source{video_suffix}"
        uploaded_video_path.replace(target_video)
        uploaded_video_path = target_video
        evidence_rel = target_video.name

    written_records: list[dict[str, Any]] = []
    record_ids: list[str] = []
    photos_extracted = 0

    # Pre-extract one representative frame from the video to reuse when a
    # record has no clip_start_ms but a video is present.
    fallback_frame: np.ndarray | None = None
    if uploaded_video_path is not None:
        fallback_frame = _extract_video_frame(uploaded_video_path, None)

    for rec in to_write:
        ordinal = int(rec["ordinal"])
        mouse_dir = run_dir / f"mouse_{ordinal:03d}"
        # Collisions inside a single report (same ordinal twice) should
        # not happen after record_id dedup, but guard anyway.
        mouse_dir.mkdir(parents=True, exist_ok=True)

        recorded_at = rec.get("recorded_at")
        if recorded_at:
            try:
                datetime.fromisoformat(str(recorded_at))
                timestamp = str(recorded_at)
            except ValueError:
                timestamp = datetime.now().isoformat(timespec="seconds")
        else:
            timestamp = datetime.now().isoformat(timespec="seconds")

        record: dict[str, Any] = {
            "record_id": rec["record_id"],
            "run_id": run_id,
            "cage_id": cage,
            "box_id": cage,
            "project_id": project,
            "ordinal": ordinal,
            "actual_ordinal": ordinal,
            "weight": rec["weight_g"],
            "confidence": 1.0,
            "timestamp": timestamp,
            "device": device,
            "photo": "photo.jpg",
            "weight_source": wsrc,
            "needs_review": False,
            "requires_manual_weight": False,
            "verification_method": "设备本地称重上报",
        }
        if rec.get("weight_raw") is not None:
            record["weight_raw"] = rec["weight_raw"]
        if rec.get("clip_start_ms") is not None:
            record["clip_start_ms"] = rec["clip_start_ms"]
        if rec.get("clip_end_ms") is not None:
            record["clip_end_ms"] = rec["clip_end_ms"]
        if evidence_rel:
            record["evidence_video"] = evidence_rel

        photo_path = mouse_dir / "photo.jpg"
        produced_real_frame = False
        if uploaded_video_path is not None:
            clip_start = rec.get("clip_start_ms")
            frame = _extract_video_frame(
                uploaded_video_path,
                float(clip_start) if clip_start is not None else None,
            )
            if frame is None:
                frame = fallback_frame
            if frame is not None:
                produced_real_frame = _write_frame_photo(photo_path, frame)
        if not produced_real_frame:
            # Last-resort: placeholder image so the record stays visible.
            _write_placeholder_photo(
                photo_path, label=f"{cage} #{ordinal:03d} {rec['weight_g']}g"
            )
        else:
            photos_extracted += 1

        (mouse_dir / "record.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        bump_record_count(run_dir)
        written_records.append(record)
        record_ids.append(rec["record_id"])

        # Enqueue directly as PENDING: these are final, locally-judged
        # records. No HELD / postflight phase applies.
        if _upload_queue is not None:
            try:
                _upload_queue.enqueue(
                    record,
                    mouse_dir / "record.json",
                    photo_path if photo_path.is_file() else None,
                    status=UploadStatus.PENDING,
                )
            except Exception:
                log.exception(
                    "report_api: upload_queue.enqueue failed (record_id=%s)",
                    rec["record_id"],
                )

    # Refresh manifest with the aggregate provenance + evidence info.
    manifest = {
        **manifest,
        "record_count": len(written_records),
        "weight_source": wsrc,
        "device_id": device,
        "mode": "device_report",
        "evidence_video": evidence_rel,
    }
    if strain:
        manifest["strain"] = strain
    write_manifest(run_dir, manifest)
    finish_run(run_dir, status="device_report")

    try:
        rel_run = str(run_dir.resolve().relative_to(_output_root.resolve()))
    except ValueError:
        rel_run = run_dir.name

    return JSONResponse(
        {
            "ok": True,
            "run_id": run_id,
            "run_dir": rel_run,
            "count": len(written_records),
            "record_ids": record_ids,
            "photos_extracted": photos_extracted,
            "skipped": skipped,
        },
        status_code=201,
    )
