"""Realtime weighing session engine for the WebSocket / phone-client path.

Unlike :mod:`mousevision.driver` (which ingests an offline video stream),
this module processes individual JPEG frames as they arrive from a phone
client and drives a small state machine dedicated to live announcements:

    CALIBRATING -> ARMED -> WEIGHING -> ANNOUNCED -> WAIT_CLEAR -> ACCEPTED
                                              |
                                              v
                                    RETRY_REQUESTED -> ARMED (after clear)

Reuse strategy (no reimplementation):
  * :class:`mousevision.reader.template.TemplateReader` for OCR
  * :class:`mousevision.fusion.temporal.TemporalWeightFusion` for stability
  * :func:`mousevision.detect.detect_mouse_box` for mouse presence

The session is in-memory only — no recorder, no upload queue, no clip
export. The caller (WebSocket handler) is responsible for persisting the
accepted :class:`Attempt` records returned by :meth:`get_accepted_records`.

Thread safety: ``process_frame`` is normally called from one streaming
task, while ``request_retry`` / ``accept_weight`` may arrive from a
separate task. A :class:`threading.Lock` guards every mutation.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from mousevision.detect import detect_mouse_box
from mousevision.fusion.temporal import TemporalWeightFusion
from mousevision.reader.observations import RawWeightObservation
from mousevision.reader.template import TemplateReader


# --------------------------------------------------------------------- #
# State enum
# --------------------------------------------------------------------- #


class RealtimeState(str, Enum):
    """Realtime session state machine.

    The value is the lowercase wire string sent to the phone client.
    """

    CALIBRATING = "calibrating"
    ARMED = "armed"
    WEIGHING = "weighing"
    ANNOUNCED = "announced"
    WAIT_CLEAR = "wait_clear"
    ACCEPTED = "accepted"
    RETRY_REQUESTED = "retry_requested"


# --------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------- #


@dataclass
class RealtimeConfig:
    """Tuning knobs for the realtime session.

    Weight thresholds mirror the offline config; quality gates are tuned
    for the phone-client path where lighting/glare change per user.
    """

    # 校准阶段
    calibrate_min_frames: int = 5  # 连续好帧数才通过校准

    # 重量阈值（沿用主配置语义）
    enter_min: float = 1.0  # 进入称重阶段的阈值（克）
    empty_max: float = 0.15  # 判定为空秤的阈值（克）
    leave_max: float = 0.30  # 判定小鼠离开的阈值（克）

    # 进入称重阶段需要的连续非零帧（沿用 enter_sustain_frames 概念）
    enter_sustain_frames: int = 2

    # 稳定性
    stable_min_frames: int = 4  # 连续稳定读数才播报
    stable_weight_tol: float = 0.10  # 稳定窗内最大跨度（克）
    min_confidence: float = 0.50  # OCR 最低置信度

    # 画面质量
    min_brightness: float = 30.0  # 最小平均亮度
    max_glare_ratio: float = 0.15  # 饱和像素最大占比

    # 小鼠检测时序平滑窗口（多数投票）
    mouse_smooth_window: int = 5

    # 计时
    announce_hold_s: float = 3.0  # 播报后自动接受的等待时间（0 = 关闭）
    clear_timeout_s: float = 30.0  # 等待清秤超时（秒）


@dataclass
class QualityHint:
    """质量提示：code 给客户端判断，message 给用户看的中文文案。"""

    code: str  # 例如 "lcd_not_found" / "too_dark" / "glare" / "unstable"
    message: str  # 中文用户友好提示


@dataclass
class Attempt:
    """单次称重尝试的记录（不论最终是否被接受）。"""

    attempt_id: str
    weight_g: float | None
    confidence: float
    frame_seq: int
    client_ts_ms: float
    state: str  # "announced" | "accepted" | "rejected"
    created_at: float  # time.time()

    def mark_accepted(self) -> None:
        self.state = "accepted"

    def mark_rejected(self) -> None:
        self.state = "rejected"


@dataclass
class RealtimeFrameResult:
    """``process_frame`` 的返回值。

    `attempt` 在新建 Attempt（播报瞬间）时填入；`accepted_weight` 在
    某次 Attempt 被接受时填入，便于上层立即推送给客户端。
    """

    state: RealtimeState
    weight_candidate: float | None = None  # 当前最佳猜测（可能为 None）
    confidence: float = 0.0
    mouse_present: bool = False
    quality_hints: list[QualityHint] = field(default_factory=list)
    attempt: Attempt | None = None  # 仅在本帧新建 Attempt 时设置
    accepted_weight: float | None = None  # 仅在本帧某 Attempt 被接受时设置
    frame_seq: int = 0


# --------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------- #


class RealtimeSession:
    """驱动一次实时称重会话的状态机。

    构造时传入已配置好的 ``reader``（通常是 :class:`TemplateReader`）与
    ``fusion``（通常是 :class:`TemporalWeightFusion`）。这两个对象由调用
    方持有，本会话不负责它们的生命周期。
    """

    def __init__(
        self,
        config: RealtimeConfig,
        reader: TemplateReader,
        fusion: TemporalWeightFusion,
        mouse_detect_config: dict | None = None,
    ) -> None:
        self.config = config
        self.reader = reader
        self.fusion = fusion
        self.mouse_detect_config: dict = dict(mouse_detect_config or {})

        # 状态 + 计数器
        self._state: RealtimeState = RealtimeState.CALIBRATING
        self._lock = threading.Lock()

        self._calibrate_good: int = 0  # CALIBRATING 连续好帧
        self._enter_sustain: int = 0  # ARMED 连续高于 enter_min 的帧
        self._stable_run: deque[float] = deque()  # WEIGHING 稳定窗
        self._leave_count: int = 0  # WEIGHING 连续低重帧
        self._clear_count: int = 0  # WAIT_CLEAR / RETRY_REQUESTED 连续空秤帧

        # 小鼠检测时序平滑
        self._mouse_history: deque[bool] = deque(maxlen=max(1, config.mouse_smooth_window))

        # 播报相关
        self._current_attempt: Attempt | None = None
        self._announce_at: float = 0.0  # 进入 ANNOUNCED 的 time.time()
        self._wait_clear_at: float = 0.0  # 进入 WAIT_CLEAR / RETRY_REQUESTED 的时间

        # 所有 Attempt（含已接受、已拒绝、待处理）
        self._attempts: list[Attempt] = []
        # 已接受记录（get_accepted_records 直接返回）
        self._accepted: list[Attempt] = []

        # 最近一次 weight_candidate（用于 ANNOUNCED/RETRY_REQUESTED 时回填）
        self._last_candidate: float | None = None
        self._last_confidence: float = 0.0

    # ----------------------------------------------------------------- #
    # Public API
    # ----------------------------------------------------------------- #

    @property
    def state(self) -> RealtimeState:
        """当前状态（线程安全读取）。"""
        with self._lock:
            return self._state

    def process_frame(
        self,
        image: np.ndarray,
        *,
        frame_seq: int = 0,
        client_ts_ms: float = 0.0,
    ) -> RealtimeFrameResult:
        """处理一帧，返回当前状态与（可能的）事件。

        本方法是状态机的唯一驱动入口。所有状态切换都在锁内完成。
        """
        now = time.time()
        with self._lock:
            return self._process_locked(image, frame_seq=frame_seq, client_ts_ms=client_ts_ms, now=now)

    def request_retry(self) -> None:
        """用户按下「重称」按钮。仅在 ANNOUNCED 状态生效。"""
        with self._lock:
            if self._state != RealtimeState.ANNOUNCED:
                return
            if self._current_attempt is not None:
                self._current_attempt.mark_rejected()
            self._current_attempt = None
            self._stable_run.clear()
            self._enter_sustain = 0
            self._clear_count = 0
            self._wait_clear_at = time.time()
            self._state = RealtimeState.RETRY_REQUESTED
            self.fusion.reset()

    def accept_weight(self) -> Attempt | None:
        """用户确认播报的重量。仅在 ANNOUNCED 状态生效。

        Returns:
            被接受的 :class:`Attempt`（已追加到 accepted 列表），
            若当前不在 ANNOUNCED 状态则返回 None。
        """
        with self._lock:
            if self._state != RealtimeState.ANNOUNCED or self._current_attempt is None:
                return None
            attempt = self._current_attempt
            attempt.mark_accepted()
            self._accepted.append(attempt)
            self._current_attempt = None
            self._stable_run.clear()
            self._clear_count = 0
            self._wait_clear_at = time.time()
            self._state = RealtimeState.WAIT_CLEAR
            return attempt

    def get_accepted_records(self) -> list[Attempt]:
        """返回所有已接受的 Attempt 副本（用于最终落库）。"""
        with self._lock:
            return list(self._accepted)

    def get_all_attempts(self) -> list[Attempt]:
        """返回本会话产生的全部 Attempt（含被拒绝/已被取代的）。"""
        with self._lock:
            return list(self._attempts)

    # ----------------------------------------------------------------- #
    # Internal helpers (all called under self._lock)
    # ----------------------------------------------------------------- #

    def _quality_checks(self, image: np.ndarray) -> tuple[bool, list[QualityHint]]:
        """画面质量检查（所有状态共用）。

        Returns:
            (ok, hints)：ok=False 表示画面不可用，应跳过本帧的后续判定。
        """
        hints: list[QualityHint] = []
        cfg = self.config

        # 直接基于 BGR 计算亮度均值与饱和像素比，省去一次 cvtColor。
        mean_brightness = float(np.mean(image))
        saturated_ratio = float(np.mean(np.all(image >= 250, axis=-1)))

        if mean_brightness < cfg.min_brightness:
            hints.append(QualityHint(code="too_dark", message="画面太暗，请调整光线"))
        if saturated_ratio > cfg.max_glare_ratio:
            hints.append(QualityHint(code="glare", message="显示屏反光，请调整角度"))

        return len(hints) == 0, hints

    def _detect_mouse_smoothed(self, image: np.ndarray, lcd_box) -> bool:
        """调用 detect_mouse_box 并做时序多数投票。"""
        md = self.mouse_detect_config
        try:
            box = detect_mouse_box(
                image,
                lcd_box,
                gray_thr=int(md.get("gray_threshold", 70)),
                min_area=int(md.get("min_area", 800)),
                x_ratio=tuple(md.get("x_ratio", (0.12, 0.88))),
                max_area=(int(md["max_area"]) if md.get("max_area") is not None else None),
                aspect_ratio=tuple(md.get("aspect_ratio", (0.3, 2.0))),
                pan_roi=md.get("pan_roi") or md.get("roi"),
                use_otsu=bool(md.get("use_otsu", True)),
                dark_p05=(float(md["dark_p05"]) if md.get("dark_p05") is not None else None),
                dark_ratio=(float(md["dark_ratio"]) if md.get("dark_ratio") is not None else None),
                min_solidity=(float(md["min_solidity"]) if md.get("min_solidity") is not None else None),
                min_extent=(float(md["min_extent"]) if md.get("min_extent") is not None else None),
            )
        except Exception:  # noqa: BLE001
            box = None
        detected = box is not None

        self._mouse_history.append(detected)
        # 历史不足时直接返回单帧结果（与 SessionDriver 一致）。
        if len(self._mouse_history) < 3:
            return detected
        return sum(self._mouse_history) > len(self._mouse_history) / 2

    def _obs_from_weight(
        self, weight: float | None, confidence: float, *, digits: list[str] | None = None
    ) -> RawWeightObservation:
        """把 TemplateReader.read_weight 的二元组包装成 RawWeightObservation，
        供 TemporalWeightFusion.update 使用。"""
        if weight is None:
            status = "unreadable"
        elif weight <= self.config.empty_max:
            status = "zero_display"
        else:
            status = "readable"
        return RawWeightObservation(
            weight=weight,
            digits=list(digits or []),
            quality=float(confidence),
            status=status,
            confidence=float(confidence),
        )

    def _transition(self, new_state: RealtimeState) -> None:
        if new_state == self._state:
            return
        self._state = new_state

    # ----------------------------------------------------------------- #
    # State handlers
    # ----------------------------------------------------------------- #

    def _process_locked(
        self,
        image: np.ndarray,
        *,
        frame_seq: int,
        client_ts_ms: float,
        now: float,
    ) -> RealtimeFrameResult:
        cfg = self.config
        quality_ok, hints = self._quality_checks(image)

        result = RealtimeFrameResult(
            state=self._state,
            quality_hints=hints,
            frame_seq=frame_seq,
        )

        # 画面太差时：CALIBRATING 重置计数；其他状态仅返回提示，不推进判定。
        if not quality_ok:
            if self._state == RealtimeState.CALIBRATING:
                self._calibrate_good = 0
            result.state = self._state
            return result

        state = self._state

        if state == RealtimeState.CALIBRATING:
            self._handle_calibrating(image, frame_seq=frame_seq, result=result)
        elif state == RealtimeState.ARMED:
            self._handle_armed(image, frame_seq=frame_seq, client_ts_ms=client_ts_ms, result=result)
        elif state == RealtimeState.WEIGHING:
            self._handle_weighing(image, frame_seq=frame_seq, client_ts_ms=client_ts_ms, now=now, result=result)
        elif state == RealtimeState.ANNOUNCED:
            self._handle_announced(now=now, result=result)
        elif state == RealtimeState.WAIT_CLEAR:
            self._handle_wait_clear(image, now=now, result=result)
        elif state == RealtimeState.RETRY_REQUESTED:
            self._handle_retry_requested(image, now=now, result=result)
        elif state == RealtimeState.ACCEPTED:
            self._handle_accepted(image, now=now, result=result)

        # 回填最近一次 weight_candidate，便于上层始终显示一个稳定数字。
        if result.weight_candidate is None and self._last_candidate is not None:
            result.weight_candidate = self._last_candidate
        if result.confidence <= 0.0 and self._last_confidence > 0.0:
            result.confidence = self._last_confidence
        result.state = self._state
        return result

    def _handle_calibrating(
        self,
        image: np.ndarray,
        *,
        frame_seq: int,
        result: RealtimeFrameResult,
    ) -> None:
        lcd_box = self.reader.lcd_box(image)
        if lcd_box is None:
            result.quality_hints.append(
                QualityHint(code="lcd_not_found", message="请调整手机，使显示屏位于画面内")
            )
            self._calibrate_good = 0
            return

        self._calibrate_good += 1
        if self._calibrate_good >= self.config.calibrate_min_frames:
            self._calibrate_good = 0
            self._transition(RealtimeState.ARMED)

    def _handle_armed(
        self,
        image: np.ndarray,
        *,
        frame_seq: int,
        client_ts_ms: float,
        result: RealtimeFrameResult,
    ) -> None:
        weight, conf = self.reader.read_weight(image)
        result.confidence = float(conf)
        if weight is not None:
            result.weight_candidate = float(weight)
            self._last_candidate = float(weight)
            self._last_confidence = float(conf)

        if weight is None or conf < self.config.min_confidence:
            self._enter_sustain = 0
            return

        if weight > self.config.enter_min:
            self._enter_sustain += 1
        else:
            self._enter_sustain = 0

        if self._enter_sustain >= max(1, self.config.enter_sustain_frames):
            self._enter_sustain = 0
            self._stable_run.clear()
            self._leave_count = 0
            self.fusion.reset()
            self._transition(RealtimeState.WEIGHING)
            # 注意：本帧只完成状态切换，不重复读取；下一帧由 _handle_weighing
            # 处理，避免对 HttpOcrReader 造成额外的网络往返。

    def _feed_weighing(
        self,
        image: np.ndarray,
        *,
        frame_seq: int,
        client_ts_ms: float,
        result: RealtimeFrameResult,
    ) -> tuple[float | None, float, bool]:
        """读一帧并喂给 fusion + 小鼠检测，返回 (stable_weight, stable_conf, mouse_present)。"""
        lcd_box = self.reader.lcd_box(image)
        mouse_present = self._detect_mouse_smoothed(image, lcd_box)
        result.mouse_present = mouse_present

        weight, conf = self.reader.read_weight(image)
        result.confidence = float(conf)
        if weight is not None:
            result.weight_candidate = float(weight)
            self._last_candidate = float(weight)
            self._last_confidence = float(conf)

        obs = self._obs_from_weight(weight, conf)
        stable = self.fusion.update(
            obs,
            mouse_present=mouse_present,
            timestamp_ms=float(client_ts_ms),
        )
        if stable is not None and stable.weight is not None:
            return float(stable.weight), float(stable.confidence), mouse_present
        return None, 0.0, mouse_present

    def _handle_weighing(
        self,
        image: np.ndarray,
        *,
        frame_seq: int,
        client_ts_ms: float,
        now: float,
        result: RealtimeFrameResult,
    ) -> None:
        stable_w, stable_conf, mouse_present = self._feed_weighing(
            image, frame_seq=frame_seq, client_ts_ms=client_ts_ms, result=result
        )
        cfg = self.config

        # 1) 小鼠提前离开（重量连续低于 leave_max）→ 回到 ARMED。
        cur_w = result.weight_candidate
        if cur_w is not None and cur_w <= cfg.leave_max:
            self._leave_count += 1
            if self._leave_count >= max(1, cfg.enter_sustain_frames):
                self._leave_count = 0
                self._stable_run.clear()
                self._enter_sustain = 0
                self.fusion.reset()
                self._transition(RealtimeState.ARMED)
                return
        else:
            self._leave_count = 0

        # 2) 没有 stable 观测则继续等待。
        if stable_w is None:
            return

        # 3) fusion 已稳定 + 小鼠在秤 → 追踪连续稳定读数。
        if not mouse_present:
            self._stable_run.clear()
            return

        # 与当前稳定窗的跨度在 tol 内则计入，否则重置（新的稳定点）。
        if self._stable_run and abs(stable_w - float(np.mean(self._stable_run))) > cfg.stable_weight_tol:
            self._stable_run.clear()
        self._stable_run.append(stable_w)

        if len(self._stable_run) >= cfg.stable_min_frames:
            announced_w = float(np.mean(self._stable_run))
            attempt = Attempt(
                attempt_id=uuid.uuid4().hex[:12],
                weight_g=round(announced_w, 2),
                confidence=float(np.median([stable_conf] * len(self._stable_run))),
                frame_seq=frame_seq,
                client_ts_ms=float(client_ts_ms),
                state="announced",
                created_at=now,
            )
            self._attempts.append(attempt)
            self._current_attempt = attempt
            self._announce_at = now
            self._stable_run.clear()
            self._transition(RealtimeState.ANNOUNCED)
            result.attempt = attempt
            result.weight_candidate = attempt.weight_g

    def _handle_announced(self, *, now: float, result: RealtimeFrameResult) -> None:
        """播报态：等待用户 accept/retry，或超时自动接受（可选）。"""
        if self._current_attempt is not None:
            result.weight_candidate = self._current_attempt.weight_g
            result.confidence = self._current_attempt.confidence

        cfg = self.config
        if cfg.announce_hold_s > 0 and (now - self._announce_at) >= cfg.announce_hold_s:
            # 自动接受（视为用户已确认）。
            attempt = self._current_attempt
            if attempt is not None:
                attempt.mark_accepted()
                self._accepted.append(attempt)
                result.accepted_weight = attempt.weight_g
                self._current_attempt = None
                self._clear_count = 0
                self._wait_clear_at = now
                self._transition(RealtimeState.WAIT_CLEAR)

    def _handle_wait_clear(
        self,
        image: np.ndarray,
        *,
        now: float,
        result: RealtimeFrameResult,
    ) -> None:
        cfg = self.config
        if (now - self._wait_clear_at) >= cfg.clear_timeout_s:
            # 超时：直接进入 ARMED，避免永久卡在 WAIT_CLEAR。
            self._clear_count = 0
            self._transition(RealtimeState.ARMED)
            return

        # 不强求 OCR 成功：读不到就保持现状，等下一帧。
        weight, _conf = self.reader.read_weight(image)
        if weight is not None:
            result.weight_candidate = float(weight)
        if weight is not None and weight <= cfg.empty_max:
            self._clear_count += 1
        else:
            self._clear_count = 0
        # 连续 1 帧空秤即进入 ACCEPTED（再下一帧 ACCEPTED 会回到 ARMED）。
        if self._clear_count >= 1:
            self._clear_count = 0
            self._transition(RealtimeState.ACCEPTED)

    def _handle_accepted(
        self,
        image: np.ndarray,
        *,
        now: float,
        result: RealtimeFrameResult,
    ) -> None:
        """ACCEPTED 是一个瞬态：紧接着回到 ARMED，等待下一只小鼠。"""
        self._enter_sustain = 0
        self._stable_run.clear()
        self._leave_count = 0
        self.fusion.reset()
        self._transition(RealtimeState.ARMED)

    def _handle_retry_requested(
        self,
        image: np.ndarray,
        *,
        now: float,
        result: RealtimeFrameResult,
    ) -> None:
        cfg = self.config
        if (now - self._wait_clear_at) >= cfg.clear_timeout_s:
            # 超时仍未清秤：强制重新 ARMED（用户可能已换姿势）。
            self._clear_count = 0
            self._transition(RealtimeState.ARMED)
            return

        weight, _conf = self.reader.read_weight(image)
        if weight is not None:
            result.weight_candidate = float(weight)
        if weight is not None and weight <= cfg.empty_max:
            self._clear_count += 1
        else:
            self._clear_count = 0
        if self._clear_count >= 1:
            self._clear_count = 0
            self._enter_sustain = 0
            self._stable_run.clear()
            self.fusion.reset()
            self._transition(RealtimeState.ARMED)
