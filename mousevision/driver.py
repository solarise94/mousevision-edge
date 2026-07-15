"""Shared per-frame weighing driver used by CLI pipeline and UI."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from mousevision.analyzer import CurveAnalyzerConfig, WeightCurveAnalyzer
from mousevision.buffer import RingFrameBuffer
from mousevision.clip import clip_bounds_from_history
from mousevision.detect import detect_mouse_box
from mousevision.detector import StateMachineConfig, WeighingState, WeighingStateMachine
from mousevision.fusion import TemporalFusionConfig, TemporalWeightFusion
from mousevision.reader.http_ocr import HttpOcrReader
from mousevision.reader.observations import RawWeightObservation
from mousevision.reader.template import TemplateReader
from mousevision.recorder import Recorder
from mousevision.run import bump_record_count
from mousevision.types import Frame
from mousevision.upload_queue import UploadQueue


@dataclass
class FrameEvent:
    frame: Frame
    state: WeighingState
    weight: float | None
    confidence: float
    lcd: Any | None
    curve_len: int
    needs_review: bool = False
    review_reason: str = ""
    raw_status: str = ""


@dataclass
class SessionSavedEvent:
    record: dict[str, Any]
    output_dir: Path
    session_index: int  # ordinal within run (1-based)
    analysis_weight: float
    analysis_confidence: float
    photo_frame: Frame | None
    state_history: list[dict[str, Any]]
    curve: list[Any]


@dataclass
class SessionDriver:
    """Feed frames one-by-one; emits saved sessions via callback / collected list."""

    config: dict[str, Any]
    templates_dir: str | Path
    output_root: str | Path
    cage_id: str = "C57-023"
    run_id: str = ""
    device_id: str | None = None
    persist: bool = True
    on_frame: Callable[[FrameEvent], None] | None = None
    on_saved: Callable[[SessionSavedEvent], None] | None = None
    upload_queue: UploadQueue | None = None
    start_ordinal: int = 1
    project_id: str = "default"

    session_index: int = 0
    saved_events: list[SessionSavedEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        cfg = self.config
        self.device_id = self.device_id or str(cfg.get("device_id", "scale01"))
        expected = cfg.get("expected_digits")
        self.reader = self._build_reader(cfg, expected)
        self.use_http_ocr = isinstance(self.reader, HttpOcrReader)
        temporal_cfg = cfg.get("temporal") or {}
        self.fusion = TemporalWeightFusion(
            TemporalFusionConfig(
                window_size=int(temporal_cfg.get("window_size", 8)),
                min_agree=int(temporal_cfg.get("min_agree", 3)),
                conflict_min_agree=int(temporal_cfg.get("conflict_min_agree", 2)),
                weight_tol=float(temporal_cfg.get("weight_tol", 0.05)),
                min_confidence=float(
                    temporal_cfg.get("min_confidence", cfg.get("match_threshold", 0.45))
                ),
                one_seven_min_confidence=float(
                    temporal_cfg.get("one_seven_min_confidence", 0.55)
                ),
                cluster_conflict_ratio=float(
                    temporal_cfg.get("cluster_conflict_ratio", 0.35)
                ),
                near_zero=float(cfg.get("near_zero", 0.5)),
                min_weight=float(temporal_cfg.get("min_weight", 5.0)),
                max_weight=float(temporal_cfg.get("max_weight", 50.0)),
                stick_tol=float(temporal_cfg.get("stick_tol", 0.20)),
            )
        )
        self.buffer = RingFrameBuffer(
            window_seconds=float(cfg.get("buffer_seconds", 12)),
            max_items=int(cfg.get("buffer_max_items", 400)),
        )
        # empty_arm + reenter_cooldown are OCR-noise defenses. TemplateReader
        # RefVideo has legitimate tight turnarounds — never apply them there.
        if self.use_http_ocr:
            empty_arm = int(cfg.get("empty_arm_frames", 5))
            cooldown_ms = float(cfg.get("reenter_cooldown_ms", 2500.0))
        else:
            empty_arm = 0
            cooldown_ms = 0.0
        self.sm = WeighingStateMachine(
            StateMachineConfig(
                empty_max=float(cfg.get("empty_max", 0.15)),
                enter_min=float(cfg.get("enter_min", 1.0)),
                leave_max=float(cfg.get("leave_max", 0.30)),
                leave_hold_frames=int(cfg.get("leave_hold_frames", 10)),
                weighing_min_samples=int(cfg.get("weighing_min_samples", 5)),
                empty_arm_frames=empty_arm,
                reenter_cooldown_ms=cooldown_ms,
            )
        )
        self.analyzer = WeightCurveAnalyzer(
            CurveAnalyzerConfig(
                platform_window_seconds=float(cfg.get("platform_window_seconds", 0.8)),
                platform_max_std=float(cfg.get("platform_max_std", 0.35)),
                near_zero=float(cfg.get("near_zero", 0.5)),
                photo_match_tol=float(cfg.get("photo_match_tol", 0.02)),
                photo_min_confidence=float(cfg.get("photo_min_confidence", 0.45)),
                min_reader_confidence=float(cfg.get("min_reader_confidence", 0.35)),
                max_jump_grams=float(cfg.get("max_jump_grams", 5.0)),
                drop_jump_outliers=bool(cfg.get("drop_jump_outliers", False)),
            )
        )
        self.recorder = Recorder(self.output_root, self.device_id)
        self._pinned = False
        self._pending_review_reason = ""

    def _build_reader(self, cfg: dict[str, Any], expected: Any):
        # Env overrides YAML so Quadlet can flip http_ocr without rebuilding config.
        reader_kind = str(
            os.environ.get("MOUSEVISION_WEIGHT_READER")
            or cfg.get("weight_reader")
            or "template"
        ).lower()
        ocr_cfg = cfg.get("ocr_api") or {}
        ocr_url = (
            os.environ.get("MOUSEVISION_OCR_URL")
            or ocr_cfg.get("base_url")
            or ""
        ).rstrip("/")
        # Only switch to http_ocr when explicitly configured AND url is set.
        if reader_kind in {"http_ocr", "ocr"} and ocr_url:
            return HttpOcrReader(
                ocr_url,
                timeout_ms=int(ocr_cfg.get("timeout_ms", 2000)),
                lcd_detect=cfg.get("lcd_detect"),
                weight_roi=cfg.get("weight_roi"),
                match_threshold=float(cfg.get("match_threshold", 0.35)),
            )
        return TemplateReader(
            self.templates_dir,
            match_threshold=float(cfg.get("match_threshold", 0.5)),
            min_digit_confidence=float(cfg.get("min_digit_confidence", 0.45)),
            lcd_detect=cfg.get("lcd_detect"),
            weight_roi=cfg.get("weight_roi"),
            expected_digits=tuple(expected) if expected else (3, 4),
        )

    def _detect_mouse(self, frame: Frame, lcd: Any | None) -> bool | None:
        md_cfg = self.config.get("mouse_detect", {})
        try:
            box = detect_mouse_box(
                frame.image,
                lcd,
                gray_thr=int(md_cfg.get("gray_threshold", 70)),
                min_area=int(md_cfg.get("min_area", 800)),
                x_ratio=tuple(md_cfg.get("x_ratio", (0.12, 0.88))),
            )
        except Exception:  # noqa: BLE001
            return None
        return box is not None

    def process_frame(self, frame: Frame) -> FrameEvent:
        raw_status = ""
        needs_review = False
        review_reason = ""

        if self.use_http_ocr:
            assert isinstance(self.reader, HttpOcrReader)
            raw: RawWeightObservation = self.reader.read_observation(frame.image)
            lcd = self.reader.lcd_box()
            mouse_present = self._detect_mouse(frame, lcd)
            stable = self.fusion.update(
                raw,
                mouse_present=mouse_present,
                timestamp_ms=frame.timestamp_ms,
            )
            raw_status = raw.status
            if self.fusion.last_needs_review:
                needs_review = True
                review_reason = self.fusion.last_review_reason
                self._pending_review_reason = review_reason
            if stable is None:
                weight, conf = None, float(raw.confidence)
            else:
                weight, conf = float(stable.weight), float(stable.confidence)
                if stable.needs_review:
                    needs_review = True
                    review_reason = stable.review_reason or review_reason
                # Strong post-conflict consensus clears a transient review flag.
                elif (
                    stable.reason in {"platform_cluster", "four_nine_break"}
                    and float(stable.confidence) >= 0.70
                    and not self.fusion.last_needs_review
                ):
                    self._pending_review_reason = ""
                    needs_review = False
                    review_reason = ""
        else:
            weight, conf = self.reader.read_weight(frame.image)
            lcd = self.reader.lcd_box(frame.image)
            mouse_present = self._detect_mouse(frame, lcd)

        prev_state = self.sm.state
        # Only the http_ocr path uses mouse_present to hold leave on zero_display.
        # TemplateReader must keep legacy leave behavior (avoid false-positive holds).
        state = self.sm.update(
            frame.timestamp_ms,
            weight,
            conf if weight is not None else 0.0,
            frame.index,
            mouse_present=mouse_present if self.use_http_ocr else None,
        )
        self.buffer.push(frame, weight=weight, weight_confidence=conf)

        if state in {WeighingState.ENTER, WeighingState.WEIGHING} and not self._pinned:
            pin_ms = (
                self.sm.session.enter_ms
                if self.sm.session.enter_ms is not None
                else frame.timestamp_ms
            )
            self.buffer.pin_from(pin_ms)
            self._pinned = True

        event = FrameEvent(
            frame=frame,
            state=state,
            weight=weight,
            confidence=conf,
            lcd=lcd,
            curve_len=len(self.sm.session.curve),
            needs_review=needs_review,
            review_reason=review_reason,
            raw_status=raw_status,
        )
        if self.on_frame is not None:
            self.on_frame(event)

        if state == WeighingState.ANALYZE:
            self._handle_analyze()
            self._pinned = False
            self.fusion.reset()
            if self.use_http_ocr and isinstance(self.reader, HttpOcrReader):
                self.reader.reset_tracking()

        if prev_state == WeighingState.ENTER and state == WeighingState.EMPTY:
            self.buffer.clear()
            self._pinned = False
            self.fusion.reset()

        return event

    def _select_photo_with_mouse(self, analysis) -> tuple[Any, bool, str, float, float, float]:
        """Choose a photo frame preferring mouse-on-scale."""
        analyzer_idx = analysis.photo_frame_index
        analyzer_frame = self.buffer.nearest_frame(analyzer_idx)
        items = list(self.buffer.items())
        if not items or analyzer_frame is None:
            return (
                analyzer_frame,
                False,
                "platform_midpoint",
                analyzer_idx,
                analysis.photo_observed_weight or 0.0,
                analysis.photo_weight_delta or 0.0,
            )

        md_cfg = self.config.get("mouse_detect", {})
        md_gray = int(md_cfg.get("gray_threshold", 70))
        md_area = int(md_cfg.get("min_area", 800))
        md_xr = tuple(md_cfg.get("x_ratio", (0.12, 0.88)))

        def _check_mouse(item):
            if self.use_http_ocr:
                lcd = self.reader.lcd_box()
            else:
                lcd = self.reader.lcd_box(item.frame.image)
            return (
                detect_mouse_box(
                    item.frame.image,
                    lcd,
                    gray_thr=md_gray,
                    min_area=md_area,
                    x_ratio=md_xr,
                )
                is not None
            )

        plat_lo = analysis.platform_start_ms
        plat_hi = analysis.platform_end_ms
        plat_items = [it for it in items if plat_lo <= it.frame.timestamp_ms <= plat_hi]
        enter_ms = self.sm.session.enter_ms
        leave_ms = self.sm.session.leave_ms
        session_items = (
            [
                it
                for it in items
                if (enter_ms is None or it.frame.timestamp_ms >= enter_ms)
                and (leave_ms is None or it.frame.timestamp_ms <= leave_ms)
            ]
            if enter_ms is not None
            else items
        )

        for scope_items, scope_label in [
            (plat_items, "mouse_on_scale"),
            (session_items, "mouse_on_scale"),
        ]:
            if not scope_items:
                continue
            mouse_items = [it for it in scope_items if _check_mouse(it)]
            if mouse_items:
                best = min(mouse_items, key=lambda it: abs(it.frame.index - analyzer_idx))
                observed = float(best.weight) if best.weight is not None else 0.0
                delta = abs(observed - analysis.weight)
                return (
                    best.frame,
                    True,
                    scope_label,
                    best.frame.index,
                    round(observed, 2),
                    round(delta, 3),
                )

        return (
            analyzer_frame,
            False,
            "platform_midpoint",
            analyzer_idx,
            analysis.photo_observed_weight or 0.0,
            analysis.photo_weight_delta or 0.0,
        )

    def _handle_analyze(self) -> None:
        analysis = self.analyzer.analyze(self.sm.session.curve)
        if analysis is None:
            self.sm.finish_analyze(
                self.sm.session.curve[-1].timestamp_ms if self.sm.session.curve else 0.0
            )
            self.buffer.clear()
            return

        (
            photo_frame,
            mouse_detected,
            selection_label,
            photo_idx,
            observed_w,
            weight_delta,
        ) = self._select_photo_with_mouse(analysis)
        analysis.photo_mouse_detected = mouse_detected
        analysis.photo_selection = selection_label
        analysis.photo_verified = mouse_detected
        analysis.photo_frame_index = photo_idx
        analysis.photo_observed_weight = observed_w
        analysis.photo_weight_delta = weight_delta
        near_zero = float(self.config.get("near_zero", 0.5))
        if mouse_detected and analysis.weight <= near_zero:
            analysis.needs_review = True
            reason = "mouse_on_scale_zero_weight"
            analysis.review_reason = (
                f"{analysis.review_reason},{reason}" if analysis.review_reason else reason
            )
        if self._pending_review_reason:
            analysis.needs_review = True
            analysis.review_reason = (
                f"{analysis.review_reason},{self._pending_review_reason}"
                if analysis.review_reason
                else self._pending_review_reason
            )
            self._pending_review_reason = ""
        history = [
            {
                "previous": t.previous.value,
                "current": t.current.value,
                "t_ms": t.timestamp_ms,
                "reason": t.reason,
            }
            for t in self.sm.history
        ]
        curve_snapshot = list(self.sm.session.curve)
        self.session_index += 1
        ordinal = self.start_ordinal + (self.session_index - 1)

        if not self.persist:
            saved = SessionSavedEvent(
                record={
                    "cage_id": self.cage_id,
                    "box_id": self.cage_id,
                    "project_id": self.project_id,
                    "ordinal": ordinal,
                    "requested_ordinal": self.start_ordinal,
                    "actual_ordinal": ordinal,
                    "run_id": self.run_id,
                    "weight": analysis.weight,
                    "confidence": analysis.confidence,
                    "persisted": False,
                },
                output_dir=Path(self.output_root),
                session_index=ordinal,
                analysis_weight=analysis.weight,
                analysis_confidence=analysis.confidence,
                photo_frame=photo_frame,
                state_history=history,
                curve=curve_snapshot,
            )
            self.saved_events.append(saved)
            if self.on_saved is not None:
                self.on_saved(saved)
            ts = curve_snapshot[-1].timestamp_ms if curve_snapshot else 0.0
            self.sm.finish_analyze(ts)
            self.buffer.clear()
            return

        out = self.recorder.save(
            cage_id=self.cage_id,
            ordinal=ordinal,
            run_id=self.run_id,
            analysis=analysis,
            curve=curve_snapshot,
            photo_frame=photo_frame,
            state_history=history,
            project_id=self.project_id,
            requested_ordinal=self.start_ordinal,
        )
        record = json.loads((out / "record.json").read_text(encoding="utf-8"))
        clip_start, clip_end = clip_bounds_from_history(history)
        record["clip_start_ms"] = clip_start
        record["clip_end_ms"] = clip_end
        (out / "record.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        bump_record_count(Path(self.output_root))
        saved = SessionSavedEvent(
            record=record,
            output_dir=out,
            session_index=ordinal,
            analysis_weight=analysis.weight,
            analysis_confidence=analysis.confidence,
            photo_frame=photo_frame,
            state_history=history,
            curve=curve_snapshot,
        )
        self.saved_events.append(saved)
        if self.upload_queue is not None:
            photo_file = out / "photo.jpg"
            self.upload_queue.enqueue(
                record,
                record_path=out / "record.json",
                photo_path=photo_file if photo_file.exists() else None,
            )
        if self.on_saved is not None:
            self.on_saved(saved)

        ts = curve_snapshot[-1].timestamp_ms if curve_snapshot else 0.0
        self.sm.finish_analyze(ts)
        self.buffer.clear()
