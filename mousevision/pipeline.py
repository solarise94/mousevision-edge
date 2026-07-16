"""End-to-end weighing pipeline (CLI)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import yaml

from mousevision.driver import SessionDriver
from mousevision.run import create_run_dir, finish_run
from mousevision.source.video import VideoFileSource
from mousevision.upload_queue import UploadQueue


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
