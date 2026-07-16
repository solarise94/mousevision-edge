"""Shared per-frame weighing driver used by CLI pipeline and UI."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from mousevision.analyzer import CurveAnalyzerConfig, WeightCurveAnalyzer, _iqr_keep_mask
from mousevision.buffer import RingFrameBuffer
from mousevision.clip import clip_bounds_from_history, export_session_clip
from mousevision.detect import detect_mouse_box
from mousevision.detector import StateMachineConfig, WeighingState, WeighingStateMachine
from mousevision.fusion import TemporalFusionConfig, TemporalWeightFusion
from mousevision.reader.http_ocr import HttpOcrReader
from mousevision.reader.observations import RawWeightObservation
from mousevision.reader.template import TemplateReader
from mousevision.recorder import Recorder
from mousevision.run import bump_record_count
from mousevision.types import AnalysisResult, Frame
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
    # Full-length source video for unstable-session clip export (optional).
    source_video: str | Path | None = None

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
                min_weight=float(temporal_cfg.get("min_weight", 0.0)),
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
            enter_min = float(cfg.get("enter_min", 1.0))
        else:
            empty_arm = 0
            cooldown_ms = 0.0
            enter_min = float(cfg.get("enter_min", 1.0))
        self.sm = WeighingStateMachine(
            StateMachineConfig(
                empty_max=float(cfg.get("empty_max", 0.15)),
                enter_min=enter_min,
                leave_max=float(cfg.get("leave_max", 0.30)),
                leave_hold_frames=int(cfg.get("leave_hold_frames", 10)),
                weighing_min_samples=int(cfg.get("weighing_min_samples", 5)),
                empty_arm_frames=empty_arm,
                reenter_cooldown_ms=cooldown_ms,
                require_mouse_for_enter=bool(self.use_http_ocr),
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
        # Raw OCR samples during ENTER/WEIGHING: (timestamp_ms, weight).
        # Instability is scored only inside the chosen platform window.
        self._session_raw_samples: list[tuple[float, float]] = []
        self._unstable_raw_range_g = float(
            (cfg.get("temporal") or {}).get("unstable_raw_range_g")
            or cfg.get("unstable_raw_range_g")
            or 0.5
        )
        self._unstable_confidence_cap = float(cfg.get("unstable_confidence_cap", 0.35))

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
            pending_raw = (
                float(raw.weight)
                if (
                    raw.weight is not None
                    and raw.status in {"readable", "zero_display"}
                    and float(raw.weight) > float(self.config.get("near_zero", 0.5))
                )
                else None
            )
        else:
            weight, conf = self.reader.read_weight(frame.image)
            lcd = self.reader.lcd_box(frame.image)
            mouse_present = self._detect_mouse(frame, lcd)
            pending_raw = (
                float(weight)
                if weight is not None and float(weight) > float(self.config.get("near_zero", 0.5))
                else None
            )

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
        # Raw OCR samples for platform-window instability (timestamped).
        if (
            pending_raw is not None
            and state in {WeighingState.ENTER, WeighingState.WEIGHING}
        ):
            self._session_raw_samples.append((float(frame.timestamp_ms), float(pending_raw)))
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
            self._session_raw_samples.clear()
            self.fusion.reset()
            if self.use_http_ocr and isinstance(self.reader, HttpOcrReader):
                self.reader.reset_tracking()

        if prev_state == WeighingState.ENTER and state == WeighingState.EMPTY:
            self.buffer.clear()
            self._pinned = False
            self._session_raw_samples.clear()
            self.fusion.reset()

        return event

    def _mark_unstable(
        self, analysis: AnalysisResult, *, reason: str, guess: float | None = None
    ) -> None:
        """Flag analysis as no-stable-platform; keep weight as display guess."""
        guessed = round(float(guess if guess is not None else analysis.weight), 2)
        analysis.weight = guessed
        analysis.guessed_weight = guessed
        analysis.requires_manual_weight = True
        analysis.needs_review = True
        analysis.weight_source = "guessed_unstable"
        analysis.confidence = min(
            float(analysis.confidence or 0.0), self._unstable_confidence_cap
        )
        reasons = [r for r in str(analysis.review_reason or "").split(",") if r]
        if reason not in reasons:
            reasons.append(reason)
        if "no_stable_platform" not in reasons:
            reasons.append("no_stable_platform")
        analysis.review_reason = ",".join(reasons)

    def _apply_raw_instability(self, analysis: AnalysisResult) -> None:
        """Reject clean settlement when platform-window raw OCR still swings.

        Only samples inside ``[platform_start_ms, platform_end_ms]`` are used.
        Climbing into ENTER / leaving WEIGHING is excluded. Span is the
        P90-P10 of IQR-trimmed inliers (主体簇), so isolated OCR spikes
        (e.g. 17.9 amid a 17.2 platform) do not force hand-fill.

        HTTP OCR with fewer than 3 platform-window raws is treated as
        ``insufficient_raw_samples`` (require hand-fill). Template path still
        trusts the fused curve in that sparse case.
        """
        if analysis.requires_manual_weight:
            return
        near_zero = float(self.config.get("near_zero", 0.5))
        t0 = float(analysis.platform_start_ms)
        t1 = float(analysis.platform_end_ms)
        if t1 < t0:
            t0, t1 = t1, t0
        raws = [
            w
            for t_ms, w in self._session_raw_samples
            if t0 <= float(t_ms) <= t1 and float(w) > near_zero
        ]
        if len(raws) < 3:
            # Too few platform-window reads to trust auto settlement.
            # Template RefVideo can still settle from the fused curve; HTTP OCR
            # should expose the gap and require experimenter confirmation.
            if self.use_http_ocr:
                guess = (
                    float(np.median(np.asarray(raws, dtype=np.float64)))
                    if raws
                    else float(analysis.weight)
                )
                self._mark_unstable(
                    analysis, reason="insufficient_raw_samples", guess=guess
                )
            return
        arr = np.asarray(raws, dtype=np.float64)
        mask = _iqr_keep_mask(arr)
        inliers = arr[mask]
        # Majority of platform reads disagree → no reliable subject cluster.
        if len(inliers) < max(3, int(np.ceil(0.5 * len(arr)))):
            guess = float(np.median(arr))
            self._mark_unstable(analysis, reason="unstable_raw_range", guess=guess)
            return
        span = float(np.percentile(inliers, 90) - np.percentile(inliers, 10))
        if span < self._unstable_raw_range_g:
            return
        guess = float(np.median(inliers))
        self._mark_unstable(analysis, reason="unstable_raw_range", guess=guess)

    def _select_photo_with_mouse(self, analysis) -> tuple[Any, bool, str, float, float, float]:
        """Choose a photo frame preferring mouse-on-scale + OCR consistency."""
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
        near_zero = float(self.config.get("near_zero", 0.5))
        weight_tol = float(self.config.get("photo_weight_tol", 0.15))
        target_w = float(analysis.weight)

        def _lcd_for(item):
            if self.use_http_ocr:
                return self.reader.lcd_box()
            return self.reader.lcd_box(item.frame.image)

        def _mouse_box(item):
            return detect_mouse_box(
                item.frame.image,
                _lcd_for(item),
                gray_thr=md_gray,
                min_area=md_area,
                x_ratio=md_xr,
            )

        def _pan_overlap_ok(box, lcd, frame_h: int) -> bool:
            """Reject blobs floating high above the pan (not on the platter)."""
            if box is None:
                return False
            _x, y, _bw, bh = box
            bottom = y + bh
            if lcd is None:
                return bottom >= int(frame_h * 0.35)
            pan_lo = 40
            pan_hi = max(pan_lo + 20, int(lcd.y) - 10)
            mid = (pan_lo + pan_hi) / 2.0
            return bottom >= mid

        def _weight_ok(item) -> bool:
            if item.weight is None:
                return False
            w = float(item.weight)
            if w <= near_zero:
                return False
            return abs(w - target_w) <= max(weight_tol, 0.008 * max(target_w, 1.0))

        def _check_mouse(item) -> bool:
            box = _mouse_box(item)
            if box is None:
                return False
            lcd = _lcd_for(item)
            return _pan_overlap_ok(box, lcd, item.frame.image.shape[0])

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

        def _fresh_frame_weight(image) -> float | None:
            """Stateless-ish re-read for photo consistency (ignore sticky hint)."""
            if self.use_http_ocr and isinstance(self.reader, HttpOcrReader):
                saved_quad = self.reader._last_quad
                saved_box = self.reader._last_box
                saved_age = self.reader._hint_age
                self.reader._last_quad = None
                self.reader._last_box = None
                self.reader._hint_age = 0
                try:
                    w, _c = self.reader.read_weight(image)
                finally:
                    self.reader._last_quad = saved_quad
                    self.reader._last_box = saved_box
                    self.reader._hint_age = saved_age
                return float(w) if w is not None else None
            w, _c = self.reader.read_weight(image)
            return float(w) if w is not None else None

        def _pick_best(candidates, scope_label: str):
            # Prefer candidates whose fresh OCR matches final platform weight.
            ranked = sorted(candidates, key=lambda it: abs(it.frame.index - analyzer_idx))
            probed = ranked[:8]
            matched = []
            for it in probed:
                fw = _fresh_frame_weight(it.frame.image)
                if fw is None or fw <= near_zero:
                    continue
                if abs(fw - target_w) <= max(weight_tol, 0.01 * max(target_w, 1.0)):
                    matched.append((it, fw))
            if matched:
                best_it, observed = min(
                    matched, key=lambda t: abs(t[0].frame.index - analyzer_idx)
                )
                delta = abs(observed - target_w)
                return (
                    best_it.frame,
                    True,
                    scope_label,
                    best_it.frame.index,
                    round(observed, 2),
                    round(delta, 3),
                )
            best = min(candidates, key=lambda it: abs(it.frame.index - analyzer_idx))
            observed = (
                float(best.weight) if best.weight is not None else float(analysis.weight)
            )
            delta = abs(observed - analysis.weight)
            return (
                best.frame,
                True,
                scope_label,
                best.frame.index,
                round(observed, 2),
                round(delta, 3),
            )

        for scope_items, scope_label in [
            (plat_items, "mouse_on_scale"),
            (session_items, "mouse_on_scale"),
        ]:
            if not scope_items:
                continue
            consistent = [
                it for it in scope_items if _check_mouse(it) and _weight_ok(it)
            ]
            if consistent:
                return _pick_best(consistent, scope_label)
            mouse_items = [it for it in scope_items if _check_mouse(it)]
            if mouse_items:
                return _pick_best(mouse_items, scope_label)


        # Platform midpoint only if its own weight agrees; else nearest consistent.
        plat_consistent = [it for it in plat_items if _weight_ok(it)]
        if plat_consistent:
            best = min(plat_consistent, key=lambda it: abs(it.frame.index - analyzer_idx))
            observed = float(best.weight) if best.weight is not None else 0.0
            return (
                best.frame,
                False,
                "platform_weight_match",
                best.frame.index,
                round(observed, 2),
                round(abs(observed - analysis.weight), 3),
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
            self._session_raw_samples.clear()
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
        # Analyzer may already have marked no_stable_platform; also catch
        # fusion-stuck curves where raw OCR still oscillated.
        if analysis.requires_manual_weight and analysis.guessed_weight is None:
            analysis.guessed_weight = float(analysis.weight)
        self._apply_raw_instability(analysis)
        near_zero = float(self.config.get("near_zero", 0.5))
        photo_tol = float(self.config.get("photo_weight_tol", 0.15))
        if mouse_detected and analysis.weight <= near_zero:
            analysis.needs_review = True
            reason = "mouse_on_scale_zero_weight"
            analysis.review_reason = (
                f"{analysis.review_reason},{reason}" if analysis.review_reason else reason
            )
        # Only flag mismatch when the photo frame carried a real OCR weight.
        # Missing buffer weights decode as 0.0 and must not fake a 17.x delta.
        if (
            analysis.weight > near_zero
            and observed_w > near_zero
            and weight_delta > max(0.35, photo_tol * 3)
            and not analysis.requires_manual_weight
        ):
            analysis.needs_review = True
            reason = "photo_weight_mismatch"
            analysis.review_reason = (
                f"{analysis.review_reason},{reason}" if analysis.review_reason else reason
            )
        if self._pending_review_reason:
            # Transient cluster conflicts often resolve into a clean platform —
            # don't mark the whole session for review if analysis is solid.
            pending = self._pending_review_reason
            solid = (
                not analysis.needs_review
                and not analysis.requires_manual_weight
                and float(analysis.confidence or 0.0) >= 0.55
                and float(analysis.weight) > near_zero
                and pending.startswith("cluster_conflict")
            )
            if solid:
                self._pending_review_reason = ""
            else:
                analysis.needs_review = True
                analysis.review_reason = (
                    f"{analysis.review_reason},{pending}"
                    if analysis.review_reason
                    else pending
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
                    "needs_review": analysis.needs_review,
                    "review_reason": analysis.review_reason,
                    "guessed_weight": analysis.guessed_weight,
                    "requires_manual_weight": analysis.requires_manual_weight,
                    "weight_source": analysis.weight_source,
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
        if analysis.requires_manual_weight and self.source_video:
            clip_status = export_session_clip(
                self.source_video,
                out / "clip.mp4",
                start_ms=clip_start,
                end_ms=clip_end,
            )
            if clip_status == "ok":
                record["clip_file"] = "clip.mp4"
                record["clip_export"] = "ok"
            else:
                record["clip_export"] = clip_status
        elif analysis.requires_manual_weight:
            record["clip_export"] = "skipped"
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
        # Hold upload until experimenter confirms unstable sessions.
        if self.upload_queue is not None and not analysis.requires_manual_weight:
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
