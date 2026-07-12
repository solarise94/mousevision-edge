"""Shared per-frame weighing driver used by CLI pipeline and UI."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from mousevision.analyzer import CurveAnalyzerConfig, WeightCurveAnalyzer
from mousevision.buffer import RingFrameBuffer
from mousevision.clip import clip_bounds_from_history
from mousevision.detect import detect_mouse_box
from mousevision.detector import StateMachineConfig, WeighingState, WeighingStateMachine
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
        self.reader = TemplateReader(
            self.templates_dir,
            match_threshold=float(cfg.get("match_threshold", 0.5)),
            min_digit_confidence=float(cfg.get("min_digit_confidence", 0.45)),
            lcd_detect=cfg.get("lcd_detect"),
            weight_roi=cfg.get("weight_roi"),
            expected_digits=tuple(expected) if expected else (3, 4),
        )
        self.buffer = RingFrameBuffer(
            window_seconds=float(cfg.get("buffer_seconds", 12)),
            max_items=int(cfg.get("buffer_max_items", 400)),
        )
        self.sm = WeighingStateMachine(
            StateMachineConfig(
                empty_max=float(cfg.get("empty_max", 0.15)),
                enter_min=float(cfg.get("enter_min", 1.0)),
                leave_max=float(cfg.get("leave_max", 0.30)),
                leave_hold_frames=int(cfg.get("leave_hold_frames", 10)),
                weighing_min_samples=int(cfg.get("weighing_min_samples", 5)),
            )
        )
        self.analyzer = WeightCurveAnalyzer(
            CurveAnalyzerConfig(
                platform_window_seconds=float(cfg.get("platform_window_seconds", 0.8)),
                platform_max_std=float(cfg.get("platform_max_std", 0.35)),
                near_zero=float(cfg.get("near_zero", 0.5)),
                photo_match_tol=float(cfg.get("photo_match_tol", 0.02)),
                photo_min_confidence=float(cfg.get("photo_min_confidence", 0.45)),
            )
        )
        self.recorder = Recorder(self.output_root, self.device_id)
        self._pinned = False

    def process_frame(self, frame: Frame) -> FrameEvent:
        weight, conf = self.reader.read_weight(frame.image)
        lcd = self.reader.lcd_box(frame.image)
        prev_state = self.sm.state
        state = self.sm.update(
            frame.timestamp_ms,
            weight,
            conf if weight is not None else 0.0,
            frame.index,
        )
        self.buffer.push(frame, weight=weight, weight_confidence=conf)

        if state in {WeighingState.ENTER, WeighingState.WEIGHING} and not self._pinned:
            pin_ms = self.sm.session.enter_ms if self.sm.session.enter_ms is not None else frame.timestamp_ms
            self.buffer.pin_from(pin_ms)
            self._pinned = True

        event = FrameEvent(
            frame=frame,
            state=state,
            weight=weight,
            confidence=conf,
            lcd=lcd,
            curve_len=len(self.sm.session.curve),
        )
        if self.on_frame is not None:
            self.on_frame(event)

        if state == WeighingState.ANALYZE:
            self._handle_analyze()
            self._pinned = False

        if prev_state == WeighingState.ENTER and state == WeighingState.EMPTY:
            self.buffer.clear()
            self._pinned = False

        return event

    def _select_photo_with_mouse(self, analysis: "AnalysisResult") -> tuple[Any, bool, str]:
        """Choose a photo frame preferring mouse-on-scale, overriding the
        analyzer's curve-only pick when a better frame is found.

        Returns (frame, mouse_detected, selection_label).
        """
        from mousevision.types import AnalysisResult as _AR  # noqa: avoid cycle

        analyzer_idx = analysis.photo_frame_index
        items = list(self.buffer.items())
        if not items:
            frame = self.buffer.nearest_frame(analyzer_idx)
            return frame, False, "platform_midpoint"

        # Detect mouse on each buffered frame. Cache LCD boxes to avoid re-detect.
        mouse_flags: dict[int, bool] = {}
        for item in items:
            idx = item.frame.index
            lcd = self.reader.lcd_box(item.frame.image)
            mouse_flags[idx] = detect_mouse_box(item.frame.image, lcd) is not None

        mouse_indices = [idx for idx, has in mouse_flags.items() if has]
        if not mouse_indices:
            # No mouse detected anywhere — keep analyzer's midpoint pick.
            frame = self.buffer.nearest_frame(analyzer_idx)
            return frame, False, "platform_midpoint"

        # Prefer a mouse frame near the analyzer's platform midpoint index.
        best_idx = min(mouse_indices, key=lambda idx: abs(idx - analyzer_idx))
        frame = self.buffer.frame_by_index(best_idx) or self.buffer.nearest_frame(best_idx)
        return frame, True, "mouse_on_scale"

    def _handle_analyze(self) -> None:
        analysis = self.analyzer.analyze(self.sm.session.curve)
        if analysis is None:
            self.sm.finish_analyze(
                self.sm.session.curve[-1].timestamp_ms if self.sm.session.curve else 0.0
            )
            self.buffer.clear()
            return

        photo_frame, mouse_detected, selection_label = self._select_photo_with_mouse(analysis)
        analysis.photo_mouse_detected = mouse_detected
        analysis.photo_selection = selection_label
        analysis.photo_verified = mouse_detected  # verified = mouse was seen
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
            # Review-only: advance state machine without writing files.
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
