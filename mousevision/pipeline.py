"""End-to-end weighing pipeline (CLI)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import yaml

from mousevision.agent_evidence import attach_agent_evidence
from mousevision.agent_weigh import (
    AgentWeighClient,
    AgentWeighError,
    persist_agent_sessions,
    resolve_agent_config,
    retain_source_video,
    should_retain_source,
)
from mousevision.driver import SessionDriver
from mousevision.run import create_run_dir, finish_run, load_manifest, write_manifest
from mousevision.source.video import VideoFileSource
from mousevision.upload_queue import UploadQueue

log = logging.getLogger("pipeline")


@dataclass
class PipelineResult:
    output_dir: Path | None
    states: list[str]
    record: dict[str, Any] | None
    samples: int
    readable: int
    records: list[dict[str, Any]] | None = None
    output_dirs: list[Path] | None = None
    run_dir: Path | None = None
    run_id: str | None = None


def load_config(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _resolved_weight_reader(cfg: dict[str, Any]) -> str:
    """Env overrides YAML (same rule as SessionDriver)."""
    return str(
        os.environ.get("MOUSEVISION_WEIGHT_READER")
        or cfg.get("weight_reader")
        or "template"
    ).strip().lower()


class WeighingPipeline:
    def __init__(self, config: dict[str, Any], templates_dir: str | Path) -> None:
        self.config = config
        self.templates_dir = templates_dir
        self.device_id = str(config.get("device_id", "scale01"))

    def run_video(
        self,
        video_path: str | Path,
        *,
        cage_id: str,
        output_root: str | Path,
        frame_stride: int | None = None,
        stop_after_first: bool = True,
        upload_queue: UploadQueue | None = None,
        create_run: bool = True,
        run_dir: Path | None = None,
        run_id: str | None = None,
        persist: bool = True,
        start_ordinal: int = 1,
        project_id: str = "default",
        crop: dict[str, float] | None = None,
        normalize_to_reference: bool = False,
    ) -> PipelineResult:
        stride = (
            frame_stride
            if frame_stride is not None
            else int(self.config.get("frame_stride", 1))
        )
        states_seen: list[str] = []
        samples = 0
        readable = 0

        out_root = Path(output_root)
        rid = run_id
        if run_dir is not None:
            active_run = Path(run_dir)
        elif create_run:
            active_run, manifest = create_run_dir(
                out_root,
                cage_id=cage_id,
                mode="video",
                source_id=str(video_path),
                device_id=self.device_id,
                run_id=rid,
                project_id=project_id,
                requested_ordinal=start_ordinal,
            )
            rid = str(manifest["run_id"])
        else:
            active_run = out_root
            rid = rid or ""

        queue = upload_queue
        if queue is None and persist:
            queue = UploadQueue(Path(output_root) / "upload_queue.db")

        # Long-lived training copy under run/ (independent of job_uploads prune).
        retained = None
        if persist and create_run and should_retain_source(self.config):
            retained = retain_source_video(
                video_path, active_run, enabled=True
            )
            if retained is not None:
                man = load_manifest(active_run) or {}
                man["source_retained"] = True
                man["source_path"] = str(retained)
                write_manifest(active_run, man)

        reader_kind = _resolved_weight_reader(self.config)
        if reader_kind in {"agent", "vlm", "gemini", "agent_full"}:
            return self._run_agent_video(
                video_path,
                cage_id=cage_id,
                active_run=active_run,
                rid=str(rid or ""),
                queue=queue if persist else None,
                persist=persist,
                create_run=create_run,
                start_ordinal=start_ordinal,
                project_id=project_id,
                stop_after_first=stop_after_first,
                retained_source=retained,
            )

        driver = SessionDriver(
            config=self.config,
            templates_dir=self.templates_dir,
            output_root=active_run,
            cage_id=cage_id,
            run_id=rid or "",
            device_id=self.device_id,
            persist=persist,
            upload_queue=queue if persist else None,
            start_ordinal=start_ordinal,
            project_id=project_id,
            source_video=str(video_path),
        )

        # When a preview crop is applied (mobile CSS-crop uploads), or when the
        # client already recorded a canvas stream near the reference size,
        # resize frames to the config reference geometry so fixed-pixel
        # detector thresholds stay meaningful.
        target_size = None
        fw = int(self.config.get("frame_width") or 0)
        fh = int(self.config.get("frame_height") or 0)
        if (crop is not None or normalize_to_reference) and fw > 0 and fh > 0:
            target_size = (fw, fh)
        # New PTS-based sampling config: analysis_fps or sample_interval_ms
        # take precedence over legacy frame_stride. When only frame_stride is
        # configured, pass it through directly to preserve backward-compatible
        # sampling behaviour (stride-based, not time-based).
        source_kwargs: dict[str, object] = {"crop": crop, "target_size": target_size}
        analysis_fps = self.config.get("analysis_fps")
        sample_interval_ms = self.config.get("sample_interval_ms")
        if sample_interval_ms is not None:
            source_kwargs["sample_interval_ms"] = float(sample_interval_ms)
        elif analysis_fps is not None:
            source_kwargs["analysis_fps"] = float(analysis_fps)
        else:
            source_kwargs["frame_stride"] = stride
        source = VideoFileSource(
            video_path, **source_kwargs  # type: ignore[arg-type]
        )
        preview_saved = False
        try:
            for frame in source.frames():
                # Persist the first analysed frame so operators can verify the
                # backend saw the same region the phone framed.
                if (
                    not preview_saved
                    and create_run
                    and persist
                    and active_run is not None
                ):
                    try:
                        cv2.imwrite(
                            str(Path(active_run) / "analysis_preview.jpg"),
                            frame.image,
                            [int(cv2.IMWRITE_JPEG_QUALITY), 85],
                        )
                        preview_saved = True
                    except Exception:
                        preview_saved = True  # do not retry forever
                event = driver.process_frame(frame)
                samples += 1
                if event.weight is not None:
                    readable += 1
                if not states_seen or states_seen[-1] != event.state.value:
                    states_seen.append(event.state.value)
                if driver.saved_events and stop_after_first:
                    break
        finally:
            source.close()
            # P1-a: If video ends while still in ENTER/WEIGHING (mouse never
            # left), flush the state machine to ANALYZE so the session is not
            # silently lost. The driver will produce a manual-weight record.
            from mousevision.detector import WeighingState as _WS
            if driver.sm.state in {_WS.ENTER, _WS.WEIGHING}:
                try:
                    last_ts = (
                        driver.sm.session.curve[-1].timestamp_ms
                        if driver.sm.session.curve
                        else 0.0
                    )
                    driver.sm.session.end_reason = "video_eof"
                    driver.sm._set_state(_WS.ANALYZE, last_ts, "video_eof")
                    driver._handle_analyze()
                except Exception as e:
                    import logging
                    logging.getLogger("pipeline").warning("EOF flush failed: %s", e)
                    raise  # propagate to job worker so it records as failure
            # Trailing unrest after the last session (or a video that ended in
            # EMPTY) still deserves manual records instead of vanishing.
            try:
                driver.flush_orphans()
            except Exception as e:  # noqa: BLE001
                import logging
                logging.getLogger("pipeline").warning("orphan flush failed: %s", e)
            if create_run and persist:
                finish_run(active_run, status="completed" if driver.saved_events else "empty")

        records = [e.record for e in driver.saved_events]
        dirs = [e.output_dir for e in driver.saved_events]
        return PipelineResult(
            output_dir=dirs[-1] if dirs else None,
            states=states_seen,
            record=records[-1] if records else None,
            samples=samples,
            readable=readable,
            records=records,
            output_dirs=dirs,
            run_dir=active_run,
            run_id=rid,
        )

    def _run_agent_video(
        self,
        video_path: str | Path,
        *,
        cage_id: str,
        active_run: Path,
        rid: str,
        queue: UploadQueue | None,
        persist: bool,
        create_run: bool,
        start_ordinal: int,
        project_id: str,
        stop_after_first: bool,
        retained_source: Path | None,
    ) -> PipelineResult:
        """Full-video agent path: no frame SM; sessions → mouse_NNN records."""
        agent_cfg = resolve_agent_config(self.config)
        client = AgentWeighClient(self.config)
        label = f"{cage_id}:{Path(video_path).name}"
        try:
            result = client.weigh_video(video_path, label=label)
        except AgentWeighError as exc:
            fallback = agent_cfg.get("fallback") or "none"
            if fallback in {"http_ocr", "ocr", "template"}:
                log.warning(
                    "agent failed (%s); fallback weight_reader=%s",
                    exc,
                    fallback,
                )
                # Re-enter classic path with temporary reader override.
                saved = os.environ.get("MOUSEVISION_WEIGHT_READER")
                os.environ["MOUSEVISION_WEIGHT_READER"] = fallback
                try:
                    # Avoid re-entering agent branch: force non-agent reader.
                    cfg = dict(self.config)
                    cfg["weight_reader"] = fallback
                    alt = WeighingPipeline(cfg, self.templates_dir)
                    return alt.run_video(
                        video_path,
                        cage_id=cage_id,
                        output_root=active_run.parent,
                        stop_after_first=stop_after_first,
                        upload_queue=queue,
                        create_run=False,
                        run_dir=active_run,
                        run_id=rid,
                        persist=persist,
                        start_ordinal=start_ordinal,
                        project_id=project_id,
                    )
                finally:
                    if saved is None:
                        os.environ.pop("MOUSEVISION_WEIGHT_READER", None)
                    else:
                        os.environ["MOUSEVISION_WEIGHT_READER"] = saved
            if create_run and persist:
                finish_run(active_run, status="failed")
            raise

        sessions = list(result.sessions)
        if stop_after_first and sessions:
            sessions = sessions[:1]
            result.sessions = sessions

        records: list[dict[str, Any]] = []
        dirs: list[Path] = []
        if persist:
            records = persist_agent_sessions(
                result=result,
                run_dir=active_run,
                cage_id=cage_id,
                run_id=rid,
                device_id=self.device_id,
                project_id=project_id,
                start_ordinal=start_ordinal,
                review_confidence=float(agent_cfg["review_confidence"]),
                # Evidence attachment can still fail-close a high-confidence
                # Agent result. Queue only after that local gate has run.
                upload_queue=None,
                source_video=retained_source or video_path,
            )
            for i, _ in enumerate(records):
                dirs.append(active_run / f"mouse_{start_ordinal + i:03d}")
            # Attach local evidence (sample video frames → photo.jpg + platform
            # times) when enabled and the source video is readable.
            attach_source: Path | None = None
            if agent_cfg.get("attach_photos", True):
                if retained_source is not None and Path(retained_source).is_file():
                    attach_source = Path(retained_source)
                elif Path(video_path).is_file():
                    attach_source = Path(video_path)
            if attach_source is not None:
                templates_path = (
                    Path(self.templates_dir)
                    if self.templates_dir
                    else None
                )
                try:
                    records = attach_agent_evidence(
                        records=records,
                        sessions=result.sessions,
                        video_path=attach_source,
                        run_dir=active_run,
                        config=self.config,
                        templates_dir=templates_path,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("attach_agent_evidence failed: %s", exc)
            if bool(agent_cfg.get("photo_gate", True)):
                for i, record in enumerate(records):
                    if record.get("photo_saved"):
                        continue
                    reasons = [
                        r
                        for r in str(record.get("review_reason") or "").split(",")
                        if r
                    ]
                    if "agent_local_evidence_unavailable" not in reasons:
                        reasons.append("agent_local_evidence_unavailable")
                    if record.get("weight") is not None and record.get(
                        "guessed_weight"
                    ) is None:
                        record["guessed_weight"] = record.get("weight")
                    record["weight"] = None
                    record["needs_review"] = True
                    record["requires_manual_weight"] = True
                    record["review_reason"] = ",".join(reasons)
                    record_path = (
                        active_run
                        / f"mouse_{start_ordinal + i:03d}"
                        / "record.json"
                    )
                    try:
                        record_path.write_text(
                            json.dumps(record, indent=2, ensure_ascii=False),
                            encoding="utf-8",
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "agent fail-closed record write failed ordinal=%s: %s",
                            record.get("ordinal", start_ordinal + i),
                            exc,
                        )
            if queue is not None:
                for i, record in enumerate(records):
                    if record.get("weight") is None or record.get(
                        "requires_manual_weight"
                    ):
                        continue
                    mouse_dir = active_run / f"mouse_{start_ordinal + i:03d}"
                    photo_path = mouse_dir / str(record.get("photo") or "photo.jpg")
                    try:
                        queue.enqueue(
                            record,
                            record_path=mouse_dir / "record.json",
                            photo_path=photo_path if photo_path.is_file() else None,
                            status="Held",
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "agent enqueue failed ordinal=%s: %s",
                            record.get("ordinal", start_ordinal + i),
                            exc,
                        )
            man = load_manifest(active_run) or {}
            man["weight_reader"] = "agent"
            man["agent_model"] = result.model
            man["agent_input_mode"] = result.input_mode
            man["agent_latency_s"] = result.latency_s
            man["agent_summary"] = result.summary
            man["agent_prompt_version"] = result.prompt_version
            man["agent_photos_attached"] = sum(
                1 for r in records if r.get("photo_saved")
            )
            write_manifest(active_run, man)
            if create_run:
                finish_run(
                    active_run,
                    status="completed" if records else "empty",
                )
        else:
            # Non-persist: synthesize in-memory records only.
            for i, sess in enumerate(sessions):
                records.append(
                    {
                        "cage_id": cage_id,
                        "ordinal": start_ordinal + i,
                        "weight": sess.weight_g,
                        "confidence": sess.confidence,
                        "weight_source": "agent_full_video",
                        "needs_review": sess.weight_g is None
                        or sess.confidence < float(agent_cfg["review_confidence"]),
                        "agent_note": sess.note,
                        "persisted": False,
                    }
                )

        readable = sum(1 for r in records if r.get("weight") is not None)
        return PipelineResult(
            output_dir=dirs[-1] if dirs else None,
            states=["AGENT"],
            record=records[-1] if records else None,
            samples=len(sessions),
            readable=readable,
            records=records,
            output_dirs=dirs or None,
            run_dir=active_run,
            run_id=rid,
        )
