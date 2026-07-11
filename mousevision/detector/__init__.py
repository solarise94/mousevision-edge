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


@dataclass
class StateMachineConfig:
    empty_max: float = 0.15
    enter_min: float = 1.0
    leave_max: float = 0.30
    leave_hold_frames: int = 8
    weighing_min_samples: int = 5


@dataclass
class SessionData:
    curve: list[CurvePoint] = field(default_factory=list)
    enter_ms: float | None = None
    leave_ms: float | None = None


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
        self.history: list[StateTransition] = []

    def reset_session(self) -> None:
        self.session = SessionData()
        self._nonzero_count = 0
        self._leave_count = 0
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

    def update(
        self,
        timestamp_ms: float,
        weight: float | None,
        confidence: float,
        frame_index: int,
    ) -> WeighingState:
        """Feed one sample. Returns current state after update."""
        cfg = self.config
        # Only ENTER/WEIGHING contribute to the weighing curve (not LEAVE).
        if self.state in {WeighingState.ENTER, WeighingState.WEIGHING} and weight is not None:
            self._append(timestamp_ms, weight, confidence, frame_index)

        if self.state == WeighingState.EMPTY:
            if weight is not None and weight >= cfg.enter_min:
                self.reset_session()
                self._append(timestamp_ms, weight, confidence, frame_index)
                self.session.enter_ms = timestamp_ms
                self._nonzero_count = 1
                self._leave_count = 0
                self._set_state(WeighingState.ENTER, timestamp_ms, "weight_above_enter")

        elif self.state == WeighingState.ENTER:
            if weight is not None and weight >= cfg.enter_min:
                self._nonzero_count += 1
                if self._nonzero_count >= cfg.weighing_min_samples:
                    self._set_state(
                        WeighingState.WEIGHING, timestamp_ms, "sustained_nonzero"
                    )
            elif weight is not None and weight <= cfg.empty_max:
                self._set_state(WeighingState.EMPTY, timestamp_ms, "abort_to_empty")
                self.reset_session()

        elif self.state == WeighingState.WEIGHING:
            # Leave when weight is near-zero OR unreadable (hand/occlusion).
            # Only a clear above-leave reading resets the counter.
            if weight is None or weight <= cfg.leave_max:
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

        return self.state

    def finish_analyze(self, timestamp_ms: float) -> None:
        self._set_state(WeighingState.EMPTY, timestamp_ms, "record_saved")
        self.reset_session()
