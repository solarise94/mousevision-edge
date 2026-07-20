"""Weighing session state machine driven by weight samples."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from mousevision.types import CurvePoint


class WeighingState(str, Enum):
    EMPTY = "EMPTY"
    ENTER = "ENTER"
    WEIGHING = "WEIGHING"
    LEAVE = "LEAVE"
    ANALYZE = "ANALYZE"
    # After session_timeout: wait for continuous near-zero before re-arming.
    WAIT_CLEAR = "WAIT_CLEAR"


@dataclass
class StateMachineConfig:
    empty_max: float = 0.15
    enter_min: float = 1.0
    leave_max: float = 0.30
    leave_hold_frames: int = 8
    weighing_min_samples: int = 5
    # After saving a session, require this many near-empty frames before the
    # next ENTER — blocks post-platform OCR garbage (23.x after 22.75).
    empty_arm_frames: int = 5
    # Absolute cooldown after a saved session (ms) before next ENTER is allowed.
    reenter_cooldown_ms: float = 2500.0
    # HTTP OCR: require mouse on scale before ENTER (blocks phantom 10.11 opens).
    require_mouse_for_enter: bool = False
    # When True, ENTER abort goes to ANALYZE (manual path) instead of EMPTY.
    # Used for http_ocr where mouse detection confirmed entry.
    enter_abort_to_analyze: bool = False
    # Active session (ENTER+WEIGHING) hard timeout from enter_ms (ms).
    max_session_ms: float = 30_000.0
    # ENTER only fires after this many consecutive non-zero fused reads
    # (a single fused spike no longer opens a session). 1 = legacy behaviour.
    enter_sustain_frames: int = 1
    # Consecutive confirmed-zero reads needed to abort ENTER. 1 = legacy
    # (single zero aborts). >1 tolerates OCR zero flicker during placement.
    enter_zero_hold_frames: int = 1
    # Stuck ENTER (never reaches WEIGHING, never sees zero) aborts to ANALYZE
    # after this many ms from enter_ms. 0 disables (legacy).
    max_enter_ms: float = 0.0
    # --- Time-based parameter overrides ---
    # When set (> 0), these take precedence over the frame-based parameters
    # above. The driver converts them to frame counts using analysis_fps.
    # This simplifies configuration: operators think in seconds, not frames.
    enter_confirm_seconds: float = 0.0  # overrides enter_sustain_frames
    leave_confirm_seconds: float = 0.0  # overrides leave_hold_frames
    empty_arm_seconds: float = 0.0  # overrides empty_arm_frames

    def resolved(self, analysis_fps: float) -> "StateMachineConfig":
        """Return a copy with time-based params converted to frame counts.

        Time-based overrides (``*_seconds``) take precedence over the
        frame-based fields when > 0. Frame counts are rounded up to at
        least 1 frame.
        """
        import math

        if analysis_fps <= 0:
            return self
        cfg = StateMachineConfig(
            empty_max=self.empty_max,
            enter_min=self.enter_min,
            leave_max=self.leave_max,
            leave_hold_frames=self.leave_hold_frames,
            weighing_min_samples=self.weighing_min_samples,
            empty_arm_frames=self.empty_arm_frames,
            reenter_cooldown_ms=self.reenter_cooldown_ms,
            require_mouse_for_enter=self.require_mouse_for_enter,
            enter_abort_to_analyze=self.enter_abort_to_analyze,
            max_session_ms=self.max_session_ms,
            enter_sustain_frames=self.enter_sustain_frames,
            enter_zero_hold_frames=self.enter_zero_hold_frames,
            max_enter_ms=self.max_enter_ms,
        )
        if self.enter_confirm_seconds > 0:
            cfg.enter_sustain_frames = max(1, math.ceil(self.enter_confirm_seconds * analysis_fps))
        if self.leave_confirm_seconds > 0:
            cfg.leave_hold_frames = max(1, math.ceil(self.leave_confirm_seconds * analysis_fps))
        if self.empty_arm_seconds > 0:
            cfg.empty_arm_frames = max(1, math.ceil(self.empty_arm_seconds * analysis_fps))
        return cfg


@dataclass
class SessionData:
    curve: list[CurvePoint] = field(default_factory=list)
    enter_ms: float | None = None
    leave_ms: float | None = None
    # Optional end reason for driver: session_timeout | abort_short_session | ...
    end_reason: str = ""


@dataclass
class StateTransition:
    previous: WeighingState
    current: WeighingState
    timestamp_ms: float
    reason: str


class WeighingStateMachine:
    def __init__(self, config: StateMachineConfig | None = None) -> None:
        self.config = config or StateMachineConfig()
        self.state = WeighingState.EMPTY
        self.session = SessionData()
        self._nonzero_count = 0
        self._leave_count = 0
        self._platform_ref: float | None = None
        self._arming = False
        self._empty_arm_count = 0
        self._reenter_after_ms: float = 0.0
        self._wait_clear_count = 0
        self._enter_sustain_count = 0
        self._enter_sustain_start_ms: float = 0.0
        self._enter_zero_count = 0
        self.history: list[StateTransition] = []

    def reset_session(self) -> None:
        self.session = SessionData()
        self._nonzero_count = 0
        self._leave_count = 0
        self._platform_ref = None
        self._enter_sustain_count = 0
        self._enter_zero_count = 0
        self.history.clear()

    def _set_state(self, new_state: WeighingState, timestamp_ms: float, reason: str) -> None:
        if new_state == self.state:
            return
        self.history.append(
            StateTransition(
                previous=self.state,
                current=new_state,
                timestamp_ms=timestamp_ms,
                reason=reason,
            )
        )
        self.state = new_state

    def _append(self, timestamp_ms: float, weight: float, confidence: float, frame_index: int) -> None:
        self.session.curve.append(
            CurvePoint(
                timestamp_ms=timestamp_ms,
                weight=weight,
                confidence=confidence,
                frame_index=frame_index,
            )
        )
        if weight > self.config.enter_min:
            if self._platform_ref is None:
                self._platform_ref = float(weight)
            elif abs(float(weight) - self._platform_ref) <= 1.0:
                self._platform_ref = 0.85 * self._platform_ref + 0.15 * float(weight)

    def _check_session_timeout(self, timestamp_ms: float) -> bool:
        """If ENTER/WEIGHING exceeded max_session_ms, force ANALYZE once.

        Returns True if timeout was triggered (caller should return immediately).
        """
        cfg = self.config
        if self.state not in {WeighingState.ENTER, WeighingState.WEIGHING}:
            return False
        if self.session.enter_ms is None:
            return False
        if float(cfg.max_session_ms) <= 0:
            return False
        if timestamp_ms - float(self.session.enter_ms) < float(cfg.max_session_ms):
            return False
        self.session.leave_ms = timestamp_ms
        self.session.end_reason = "session_timeout"
        self._set_state(WeighingState.ANALYZE, timestamp_ms, "session_timeout")
        return True

    def update(
        self,
        timestamp_ms: float,
        weight: float | None,
        confidence: float,
        frame_index: int,
        *,
        mouse_present: bool | None = None,
    ) -> WeighingState:
        """Feed one sample. Returns current state after update.

        Confirmed 0g with mouse still detected holds leave. Unreadable frames
        still count toward leave. After ANALYZE→EMPTY, require empty_arm_frames
        of near-empty before the next ENTER. Active sessions (ENTER+WEIGHING)
        hard-timeout after max_session_ms from enter_ms.
        """
        cfg = self.config
        # Only ENTER/WEIGHING contribute to the weighing curve (not LEAVE).
        if self.state in {WeighingState.ENTER, WeighingState.WEIGHING} and weight is not None:
            self._append(timestamp_ms, weight, confidence, frame_index)

        # Active-session hard timeout covers ENTER + WEIGHING.
        if self._check_session_timeout(timestamp_ms):
            return self.state

        if self.state == WeighingState.EMPTY:
            near_empty = weight is None or weight <= cfg.leave_max
            if timestamp_ms < self._reenter_after_ms:
                # Absolute post-session cooldown — ignore phantom platforms.
                if near_empty:
                    self._empty_arm_count += 1
                    # Empty evidence observed during cooldown is still valid;
                    # otherwise a quickly placed next animal can never re-arm.
                    if (
                        self._arming
                        and self._empty_arm_count >= cfg.empty_arm_frames
                    ):
                        self._arming = False
                return self.state
            if self._arming:
                if near_empty:
                    self._empty_arm_count += 1
                    if self._empty_arm_count >= cfg.empty_arm_frames:
                        self._arming = False
                else:
                    self._empty_arm_count = 0
                return self.state
            if weight is not None and weight >= cfg.enter_min:
                # Sustained-read ENTER: require N consecutive non-zero reads so
                # a lone fused spike cannot open a phantom session.
                if self._enter_sustain_count == 0:
                    self._enter_sustain_start_ms = timestamp_ms
                self._enter_sustain_count += 1
                if self._enter_sustain_count < cfg.enter_sustain_frames:
                    return self.state
                if cfg.require_mouse_for_enter and mouse_present is not True:
                    # Phantom non-zero OCR without a mouse — stay EMPTY.
                    self._enter_sustain_count = 0
                    return self.state
                self.reset_session()
                self._append(timestamp_ms, weight, confidence, frame_index)
                self.session.enter_ms = self._enter_sustain_start_ms
                self._nonzero_count = 1
                self._leave_count = 0
                self._set_state(WeighingState.ENTER, timestamp_ms, "weight_above_enter")
            else:
                self._enter_sustain_count = 0

        elif self.state == WeighingState.ENTER:
            if (
                cfg.max_enter_ms > 0
                and self.session.enter_ms is not None
                and timestamp_ms - float(self.session.enter_ms) >= cfg.max_enter_ms
            ):
                # Stuck ENTER (OCR flicker / handling without settlement):
                # close the session instead of merging the next animal in.
                self.session.leave_ms = timestamp_ms
                self.session.end_reason = "enter_timeout"
                self._set_state(WeighingState.ANALYZE, timestamp_ms, "enter_timeout")
            elif weight is not None and weight >= cfg.enter_min:
                self._enter_zero_count = 0
                self._nonzero_count += 1
                if self._nonzero_count >= cfg.weighing_min_samples:
                    self._set_state(
                        WeighingState.WEIGHING, timestamp_ms, "sustained_nonzero"
                    )
            elif weight is not None and weight <= cfg.empty_max:
                self._enter_zero_count += 1
                if self._enter_zero_count < cfg.enter_zero_hold_frames:
                    return self.state
                if cfg.enter_abort_to_analyze:
                    # http_ocr path: mouse detection confirmed entry, so this
                    # is a real short session (not OCR noise). Go ANALYZE.
                    self.session.leave_ms = timestamp_ms
                    self.session.end_reason = "abort_short_session"
                    self._set_state(
                        WeighingState.ANALYZE, timestamp_ms, "abort_short_session"
                    )
                else:
                    # Template/RefVideo path: likely OCR noise, reset to EMPTY.
                    self._set_state(WeighingState.EMPTY, timestamp_ms, "abort_to_empty")
                    self.reset_session()

        elif self.state == WeighingState.WEIGHING:
            confirmed_zero = weight is not None and weight <= cfg.leave_max
            missing = weight is None
            # Hold leave only on confirmed 0g while mouse still detected.
            if confirmed_zero and mouse_present:
                self._leave_count = 0
            elif confirmed_zero or missing:
                self._leave_count += 1
                if self._leave_count >= cfg.leave_hold_frames:
                    self.session.leave_ms = timestamp_ms
                    self._set_state(WeighingState.LEAVE, timestamp_ms, "weight_near_zero")
            else:
                self._leave_count = 0

        elif self.state == WeighingState.LEAVE:
            self._set_state(WeighingState.ANALYZE, timestamp_ms, "buffer_ready")

        elif self.state == WeighingState.ANALYZE:
            pass

        elif self.state == WeighingState.WAIT_CLEAR:
            # After timeout: require continuous near-zero before re-arming.
            # mouse_present=False is auxiliary only — do not re-arm on a single
            # false mouse detection while weight is still non-zero.
            near_empty = weight is not None and weight <= cfg.leave_max
            if near_empty:
                self._wait_clear_count += 1
                if self._wait_clear_count >= max(1, cfg.empty_arm_frames):
                    self._wait_clear_count = 0
                    self._arming = False
                    self._empty_arm_count = 0
                    self._set_state(WeighingState.EMPTY, timestamp_ms, "scale_cleared")
            else:
                self._wait_clear_count = 0

        return self.state

    def finish_analyze(self, timestamp_ms: float, *, wait_clear: bool = False) -> None:
        """End ANALYZE. If wait_clear, enter WAIT_CLEAR instead of EMPTY.

        wait_clear is used after session_timeout so a mouse still on the scale
        cannot immediately open a second session.
        """
        if wait_clear:
            self._set_state(WeighingState.WAIT_CLEAR, timestamp_ms, "record_saved_wait_clear")
            self.reset_session()
            self._arming = True
            self._empty_arm_count = 0
            self._wait_clear_count = 0
            self._reenter_after_ms = float(timestamp_ms) + float(self.config.reenter_cooldown_ms)
            return
        self._set_state(WeighingState.EMPTY, timestamp_ms, "record_saved")
        self.reset_session()
        self._arming = True
        self._empty_arm_count = 0
        self._wait_clear_count = 0
        self._reenter_after_ms = float(timestamp_ms) + float(self.config.reenter_cooldown_ms)
