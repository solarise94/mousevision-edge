"""Unit tests for weighing state machine."""

from mousevision.detector import StateMachineConfig, WeighingState, WeighingStateMachine


def _feed(sm: WeighingStateMachine, weights: list[float | None], start_ms: float = 0.0):
    states = []
    for i, w in enumerate(weights):
        st = sm.update(start_ms + i * 100, w, 0.9 if w is not None else 0.0, i)
        states.append(st)
    return states


def test_full_cycle_empty_to_analyze():
    sm = WeighingStateMachine(
        StateMachineConfig(
            enter_min=1.0,
            leave_max=0.3,
            leave_hold_frames=3,
            weighing_min_samples=3,
        )
    )
    weights: list[float | None] = (
        [0.0, 0.0]
        + [5.0, 10.0, 20.0, 24.0, 25.0, 24.8, 25.0]
        + [0.1, 0.0, 0.0, 0.0]
    )
    states = _feed(sm, weights)
    values = [s.value for s in states]
    assert "ENTER" in values
    assert "WEIGHING" in values
    assert "LEAVE" in values
    assert "ANALYZE" in values
    assert sm.state == WeighingState.ANALYZE
    assert len(sm.session.curve) >= 5


def test_abort_false_enter():
    sm = WeighingStateMachine(
        StateMachineConfig(enter_min=1.0, weighing_min_samples=5, empty_max=0.15)
    )
    _feed(sm, [0.0, 2.0, 0.0, 0.0])
    assert sm.state == WeighingState.EMPTY


def test_history_cleared_between_sessions():
    sm = WeighingStateMachine(
        StateMachineConfig(
            enter_min=1.0,
            leave_max=0.3,
            leave_hold_frames=2,
            weighing_min_samples=2,
        )
    )
    _feed(sm, [5.0, 10.0, 20.0, 0.0, 0.0, 0.0])
    assert sm.state == WeighingState.ANALYZE
    hist1 = len(sm.history)
    assert hist1 >= 3
    sm.finish_analyze(1000)
    assert sm.history == []

    _feed(sm, [6.0, 12.0, 22.0, 0.0, 0.0, 0.0], start_ms=2000)
    assert sm.state == WeighingState.ANALYZE
    assert len(sm.history) <= hist1
    assert all(t.timestamp_ms >= 2000 for t in sm.history)


def test_leave_counts_unreadable_frames():
    sm = WeighingStateMachine(
        StateMachineConfig(
            enter_min=1.0,
            leave_max=0.3,
            leave_hold_frames=3,
            weighing_min_samples=2,
        )
    )
    sm.update(0, 10.0, 0.9, 0)
    sm.update(100, 20.0, 0.9, 1)
    assert sm.state == WeighingState.WEIGHING
    sm.update(200, None, 0.0, 2)
    sm.update(300, None, 0.0, 3)
    sm.update(400, None, 0.0, 4)
    assert sm.state in {WeighingState.LEAVE, WeighingState.ANALYZE}


def test_leave_state_not_appended_to_curve():
    sm = WeighingStateMachine(
        StateMachineConfig(
            enter_min=1.0,
            leave_max=0.3,
            leave_hold_frames=2,
            weighing_min_samples=2,
        )
    )
    # ENTER+WEIGHING then near-zero hold → LEAVE
    _feed(sm, [5.0, 20.0, 0.05, 0.0])
    assert sm.state == WeighingState.LEAVE
    curve_len = len(sm.session.curve)
    # While in LEAVE / ANALYZE, further samples must not grow the curve.
    sm.update(500, 0.0, 0.9, 99)
    assert sm.state == WeighingState.ANALYZE
    assert len(sm.session.curve) == curve_len
    sm.update(600, 0.0, 0.9, 100)
    assert len(sm.session.curve) == curve_len
