"""Realtime weighing session engine for the WebSocket / phone-client path.

Unlike :mod:`mousevision.driver` (which ingests an offline video stream),
this module processes individual JPEG frames as they arrive from a phone
client and drives a small state machine dedicated to live announcements:

    CALIBRATING -> ARMED -> WEIGHING -> ANNOUNCED -> WAIT_CLEAR -> ACCEPTED
                                              |
                                              v
                                    (retry) -> WEIGHING  (same mouse, new epoch)

Reuse strategy (no reimplementation):
  * :class:`mousevision.reader.template.TemplateReader` for OCR
  * :func:`mousevision.detect.detect_mouse_box` for mouse presence

Stability is decided from independent raw OCR reads (not a fused sliding
window), so a platform switch like ``16.14 × 3 -> 15.62 × 3`` cannot lock
the old platform.

Thread safety: ``process_frame`` is normally called from one streaming
task, while ``request_retry`` / ``accept_weight`` may arrive from a
separate task. A :class:`threading.Lock` guards every mutation.
"""

from __future__ import annotations

import math
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import numpy as np

from mousevision.detect import detect_mouse_box
from mousevision.fusion.temporal import TemporalWeightFusion
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
    RETRY_REQUESTED = "retry_requested"  # legacy; retry now goes to WEIGHING


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

    # 稳定性（原始 OCR 证据）
    stable_min_frames: int = 4  # 兼容旧配置；实时判定改用 stable_min_raw_reads
    stable_min_raw_reads: int = 3  # 稳定后缀最少独立原始读数
    # 候选确认期：stable_min_raw_reads 条一致只形成 pending candidate，需再
    # 等 stable_confirm_raw_reads 条独立读数仍在容差内才正式播报。挡住
    # ARMED 延续进 WEIGHING 的旧平台残留（16.14×3 不会立即播报）。
    stable_confirm_raw_reads: int = 1
    stable_min_span_ms: float = 0.0  # 确认期最小时间跨度（ms）；0 = 仅按帧数
    stable_max_age_s: float = 1.6  # 稳定证据允许保留的最大年龄（秒）
    stable_weight_tol: float = 0.10  # 稳定后缀内最大跨度（克）
    min_confidence: float = 0.50  # OCR 最低置信度

    # 画面质量
    min_brightness: float = 30.0  # 最小平均亮度
    max_glare_ratio: float = 0.15  # 饱和像素最大占比

    # 小鼠检测
    mouse_smooth_window: int = 5
    mouse_advisory: bool = True  # True: 不因无鼠清空重量窗，仅提示

    # 帧序校验
    frame_seq_dedupe: bool = True

    # 计时
    announce_hold_s: float = 0.0  # 播报后自动接受的等待时间（0 = 关闭）
    clear_timeout_s: float = 30.0  # 等待清秤超时（秒）

    # BLE 天平（K797）。超过 ble_stale_s 未收到广播即视为「天平广播中断」，
    # 不再用过期读数作为重量证据，只下放 scale_stale 质量提示。
    ble_stale_s: float = 10.0


@dataclass
class RealtimeRawRead:
    """一条独立原始 OCR 读数，用作稳定证据。"""

    frame_seq: int
    client_ts_ms: float
    weight: float
    confidence: float
    epoch: int


@dataclass
class _PendingCandidate:
    """候选确认期：stable_min_raw_reads 条一致读数形成的待确认播报。

    只有再收到 stable_confirm_raw_reads 条独立读数仍在容差内的读数才
    正式创建 Attempt；平台变化则撤销。
    """

    median_weight: float
    median_confidence: float
    frame_seq: int
    client_ts_ms: float
    first_ts_ms: float  # 第一条候选读数的 client_ts，用于 span 判定
    confirm_count: int = 0  # 候选之后仍在容差内的独立读数


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
    # BLE 原始 raw（K797 uint16）。OCR 路径不设置（保持 None）；仅 BLE 读数
    # 形成的 attempt 携带，供 finalize 写入 record.json 的 weight_raw。
    weight_raw: int | None = None

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
    epoch: int = 0
    # BLE 会话下，原生 stable 标志（天平自报稳定）。None 表示非 BLE 或本帧无读数；
    # 仅供客户端展示，绝不参与后端稳定窗判定。
    ble_stable: bool | None = None


def validate_realtime_config(cfg: RealtimeConfig) -> None:
    """Raise ``ValueError`` if realtime knobs are out of safe range."""
    if cfg.stable_min_raw_reads < 2:
        raise ValueError(f"stable_min_raw_reads must be >= 2, got {cfg.stable_min_raw_reads}")
    if cfg.stable_confirm_raw_reads < 0:
        raise ValueError(
            f"stable_confirm_raw_reads must be >= 0, got {cfg.stable_confirm_raw_reads}"
        )
    if cfg.stable_min_span_ms < 0:
        raise ValueError(f"stable_min_span_ms must be >= 0, got {cfg.stable_min_span_ms}")
    if cfg.stable_max_age_s <= 0:
        raise ValueError(f"stable_max_age_s must be > 0, got {cfg.stable_max_age_s}")
    if not (0.0 < cfg.min_confidence <= 1.0):
        raise ValueError(f"min_confidence must be in (0, 1], got {cfg.min_confidence}")
    if cfg.stable_weight_tol <= 0:
        raise ValueError(f"stable_weight_tol must be > 0, got {cfg.stable_weight_tol}")
    if cfg.calibrate_min_frames < 1:
        raise ValueError(f"calibrate_min_frames must be >= 1, got {cfg.calibrate_min_frames}")
    if cfg.enter_sustain_frames < 1:
        raise ValueError(f"enter_sustain_frames must be >= 1, got {cfg.enter_sustain_frames}")


# --------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------- #


class RealtimeSession:
    """驱动一次实时称重会话的状态机。

    构造时传入已配置好的 ``reader``（通常是 :class:`TemplateReader`）与
    ``fusion``（保留参数以兼容调用方；稳定判定改用原始 OCR 窗）。
    """

    def __init__(
        self,
        config: RealtimeConfig,
        reader: TemplateReader,
        fusion: TemporalWeightFusion,
        mouse_detect_config: dict | None = None,
        *,
        weight_source: str = "ocr",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        validate_realtime_config(config)
        self.config = config
        self.reader = reader
        self.fusion = fusion
        self.mouse_detect_config: dict = dict(mouse_detect_config or {})
        # 重量来源：OCR（手机拍 LCD）或 ble_k797（天平蓝牙广播）。BLE 模式下
        # _read_weight_once 不再调用 OCR reader，改读 BLE 缓存。
        self.weight_source: str = weight_source
        # 可注入的单调时钟，便于测试控制 BLE 读数新鲜度而无需 sleep。
        self._clock: Callable[[], float] = clock

        # 状态 + 计数器
        self._state: RealtimeState = RealtimeState.CALIBRATING
        self._lock = threading.Lock()

        self._calibrate_good: int = 0  # CALIBRATING 连续好帧
        self._enter_sustain: int = 0  # ARMED 连续高于 enter_min 的帧
        self._stable_run: deque[float] = deque()  # 兼容字段；不再驱动播报
        self._leave_count: int = 0  # WEIGHING 连续低重帧
        self._clear_count: int = 0  # WAIT_CLEAR 连续空秤帧

        # 原始 OCR 稳定证据
        self._raw_window: deque[RealtimeRawRead] = deque()
        self._weighing_epoch: int = 0
        self._last_frame_seq: int = -1
        self._last_client_ts_ms: float = -1.0

        # 小鼠检测时序平滑
        self._mouse_history: deque[bool] = deque(maxlen=max(1, config.mouse_smooth_window))

        # 播报相关
        self._current_attempt: Attempt | None = None
        self._announce_at: float = 0.0  # 进入 ANNOUNCED 的 time.time()
        self._wait_clear_at: float = 0.0  # 进入 WAIT_CLEAR 的时间
        # 候选确认期：stable_min_raw_reads 条一致只形成 pending；需再等
        # stable_confirm_raw_reads 条独立读数仍在容差内才播报。
        self._pending_candidate: _PendingCandidate | None = None

        # 所有 Attempt（含已接受、已拒绝、待处理）
        self._attempts: list[Attempt] = []
        # 已接受记录（get_accepted_records 直接返回）
        self._accepted: list[Attempt] = []

        # 最近一次 weight_candidate（用于 ANNOUNCED 时回填）
        self._last_candidate: float | None = None
        self._last_confidence: float = 0.0

        # BLE（K797）最新读数缓存。ingest_scale_reading 写入，_read_weight_once
        # 在 BLE 模式下读取。None 表示尚无任何 BLE 读数。
        self._ble_reading: dict[str, Any] | None = None  # {grams, raw, stable, ...}
        self._ble_received_monotonic: float = 0.0  # clock() 读数到达时刻
        self._ble_received_epoch_ms: int = 0
        self._last_ble_sequence: int = -1  # 单调校验用；-1 表示尚未收到

    # ----------------------------------------------------------------- #
    # Public API
    # ----------------------------------------------------------------- #

    @property
    def state(self) -> RealtimeState:
        """当前状态（线程安全读取）。"""
        with self._lock:
            return self._state

    @property
    def weighing_epoch(self) -> int:
        with self._lock:
            return self._weighing_epoch

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

    def request_retry(self) -> dict[str, Any]:
        """用户按下「重称」按钮。仅在 ANNOUNCED 状态生效。

        产品语义：同一只鼠可留在秤上立即重新采样。成功时直接进入
        WEIGHING，并递增 weighing epoch，使旧证据无法进入新窗口。

        Returns:
            ``{"applied": bool, "state": str, "epoch": int}``
        """
        with self._lock:
            if self._state != RealtimeState.ANNOUNCED:
                return {
                    "applied": False,
                    "state": self._state.value,
                    "epoch": self._weighing_epoch,
                }
            if self._current_attempt is not None:
                self._current_attempt.mark_rejected()
            self._current_attempt = None
            self._weighing_epoch += 1
            self._reset_weighing()
            self._transition(RealtimeState.WEIGHING)
            return {
                "applied": True,
                "state": self._state.value,
                "epoch": self._weighing_epoch,
            }

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
            self._reset_weighing()
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

    def ingest_manual_weight(self, *, weight_g: float) -> Attempt:
        """手动模式：操作员直接输入一只鼠的克数，跳过 OCR/BLE 自动判定与播报确认。

        合成一个已 accepted 的 Attempt（手动输入即定稿，无需 announced→accept 两步），
        直接追加到 accepted 列表并转到 WAIT_CLEAR。仅在 ``weight_source="manual"`` 时
        由上层调用；本方法不自行校验来源（守卫在 realtime_api 的 WS 命令层）。

        Args:
            weight_g: 操作员输入的克数，已由上层校验为有限数且在 [0, 6553.5]。

        Returns:
            合成并已接受的 :class:`Attempt`（weight_raw=None，confidence=1.0）。
        """
        with self._lock:
            now = time.time()
            attempt = Attempt(
                attempt_id=uuid.uuid4().hex[:12],
                weight_g=round(float(weight_g), 2),
                confidence=1.0,
                frame_seq=self._last_frame_seq,
                client_ts_ms=float(self._last_client_ts_ms),
                state="accepted",
                created_at=now,
                weight_raw=None,
            )
            self._attempts.append(attempt)
            self._accepted.append(attempt)
            # 若当前有未确认的播报 attempt，手动输入视为取代它。
            if self._current_attempt is not None:
                self._current_attempt.mark_rejected()
                self._current_attempt = None
            self._reset_weighing()
            self._clear_count = 0
            self._wait_clear_at = now
            self._state = RealtimeState.WAIT_CLEAR
            return attempt

    # ----------------------------------------------------------------- #
    # BLE (K797) ingest
    # ----------------------------------------------------------------- #

    def ingest_scale_reading(
        self,
        *,
        grams: float,
        raw: int,
        sequence: int,
        received_at_epoch_ms: int,
        stable: bool | None = None,
        rssi: int | None = None,
    ) -> bool:
        """缓存一条 BLE 天平读数，供 BLE 模式下的 _read_weight_once 消费。

        线程安全：与 process_frame 共用 ``self._lock``（process_frame 在
        ``_process_locked`` 内持锁读取缓存，故此处必须用同一把锁写入）。

        校验（任一失败即抛 ``ValueError``，缓存不变）：
          * grams 有限且在 [0, 6553.5]；
          * raw 为 int 且在 [0, 65535]；
          * |grams - raw/10| <= 0.05（前后端读数一致）；
          * sequence 严格大于上一次（单调递增）。

        Returns:
            True 表示读数已更新缓存；False 表示因序列号非单调被忽略。
        """
        # --- 类型 / 范围校验（在锁外做纯函数校验，失败即抛） ------------- #
        if isinstance(grams, bool) or not isinstance(grams, (int, float)):
            raise ValueError(f"grams must be a finite number, got {grams!r}")
        if not math.isfinite(float(grams)):
            raise ValueError(f"grams must be finite, got {grams!r}")
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValueError(f"raw must be int, got {raw!r}")
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise ValueError(f"sequence must be int, got {sequence!r}")
        if isinstance(received_at_epoch_ms, bool) or not isinstance(
            received_at_epoch_ms, int
        ):
            raise ValueError(
                f"received_at_epoch_ms must be int, got {received_at_epoch_ms!r}"
            )
        if sequence < 0:
            raise ValueError(f"sequence must be >= 0, got {sequence}")
        if not (0.0 <= float(grams) <= 6553.5):
            raise ValueError(f"grams out of range [0, 6553.5]: {grams}")
        if not (0 <= raw <= 65535):
            raise ValueError(f"raw out of range [0, 65535]: {raw}")
        if abs(float(grams) - raw / 10.0) > 0.05:
            raise ValueError(
                f"grams/raw mismatch: grams={grams} raw={raw} (raw/10={raw / 10.0})"
            )

        with self._lock:
            # 序列号必须严格单调递增；相等或倒序视为重复/乱序，忽略。
            if sequence <= self._last_ble_sequence:
                return False
            self._last_ble_sequence = sequence
            self._ble_reading = {
                "grams": float(grams),
                "raw": int(raw),
                "stable": bool(stable) if stable is not None else None,
                "rssi": rssi,
                "received_at_epoch_ms": received_at_epoch_ms,
            }
            self._ble_received_monotonic = self._clock()
            self._ble_received_epoch_ms = received_at_epoch_ms
            return True

    # ----------------------------------------------------------------- #
    # Internal helpers (all called under self._lock)
    # ----------------------------------------------------------------- #

    def _reset_weighing(self) -> None:
        """清空称重相关证据与计数（清秤 / 下一只 / retry / 异常恢复）。"""
        self._raw_window.clear()
        self._stable_run.clear()
        self._enter_sustain = 0
        self._leave_count = 0
        self._pending_candidate = None
        try:
            self.fusion.reset()
        except Exception:  # noqa: BLE001
            pass

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

    def _transition(self, new_state: RealtimeState) -> None:
        if new_state == self._state:
            return
        self._state = new_state

    def _accept_frame_order(self, frame_seq: int, client_ts_ms: float) -> bool:
        """校验 frame_seq / client_ts_ms，通过后更新游标。

        Returns:
            True 表示本帧可作为新证据；False 表示重复/倒序，只回状态。
        """
        if not self.config.frame_seq_dedupe:
            return True
        if frame_seq <= self._last_frame_seq:
            return False
        if self._last_client_ts_ms >= 0 and client_ts_ms < self._last_client_ts_ms:
            return False
        self._last_frame_seq = frame_seq
        self._last_client_ts_ms = float(client_ts_ms)
        return True

    def _prune_raw_window(self, latest_ts_ms: float) -> None:
        """裁剪超出最大证据年龄、或不属于当前 epoch 的读数。"""
        max_age_ms = self.config.stable_max_age_s * 1000.0
        epoch = self._weighing_epoch
        kept: deque[RealtimeRawRead] = deque()
        for r in self._raw_window:
            if r.epoch != epoch:
                continue
            if latest_ts_ms - r.client_ts_ms > max_age_ms:
                continue
            kept.append(r)
        self._raw_window = kept

    def _append_raw_read(
        self,
        *,
        frame_seq: int,
        client_ts_ms: float,
        weight: float,
        confidence: float,
    ) -> None:
        self._raw_window.append(
            RealtimeRawRead(
                frame_seq=frame_seq,
                client_ts_ms=float(client_ts_ms),
                weight=float(weight),
                confidence=float(confidence),
                epoch=self._weighing_epoch,
            )
        )
        self._prune_raw_window(float(client_ts_ms))

    def _stable_suffix(self) -> tuple[float, float] | None:
        """从最新读数向前寻找连续稳定后缀。

        Returns:
            ``(median_weight, median_confidence)`` 或 None。
        """
        cfg = self.config
        reads = [r for r in self._raw_window if r.epoch == self._weighing_epoch]
        if len(reads) < cfg.stable_min_raw_reads:
            return None

        latest = reads[-1]
        max_age_ms = cfg.stable_max_age_s * 1000.0
        suffix: list[RealtimeRawRead] = []
        for r in reversed(reads):
            if latest.client_ts_ms - r.client_ts_ms > max_age_ms:
                break
            weights_so_far = [x.weight for x in suffix] + [r.weight]
            if max(weights_so_far) - min(weights_so_far) > cfg.stable_weight_tol:
                break
            suffix.append(r)

        suffix.reverse()
        if len(suffix) < cfg.stable_min_raw_reads:
            return None
        if suffix[-1] is not latest and suffix[-1].frame_seq != latest.frame_seq:
            return None

        weights = [r.weight for r in suffix]
        confs = [r.confidence for r in suffix]
        median_w = float(np.median(weights))
        if abs(latest.weight - median_w) > cfg.stable_weight_tol:
            return None
        return median_w, float(np.median(confs))

    def _read_weight_once(
        self, image: np.ndarray
    ) -> tuple[float | None, float, Any]:
        """定位一次 LCD，同时供鼠检测与重量读取复用。

        OCR 模式：调用 reader.read_weight 做 LCD 识别。
        BLE 模式：重量来自 BLE 缓存（ingest_scale_reading 写入），新鲜
        （age <= ble_stale_s）返回 ``(grams, 1.0, lcd_box)``，过期/缺失返回
        ``(None, 0.0, lcd_box)``。OCR reader 绝不被调用于重量读取。LCD 定位
        仍会执行，因为鼠检测需要 lcd_box 作为 ROI。
        """
        lcd_box = self.reader.lcd_box(image)
        if self.weight_source in ("ble_k797", "manual"):
            # BLE：重量来自缓存；manual：无自动重量来源（由 ingest_manual_weight 驱动）。
            # 两者都不走 OCR 自动读重，避免与手动输入/天平读数冲突。
            grams = self._fresh_ble_grams() if self.weight_source == "ble_k797" else None
            if grams is None:
                return None, 0.0, lcd_box
            return float(grams), 1.0, lcd_box
        weight, conf = self.reader.read_weight(image, lcd_box=lcd_box)
        return weight, float(conf), lcd_box

    def _fresh_ble_grams(self) -> float | None:
        """返回新鲜（age <= ble_stale_s）的 BLE 重量，否则 None。

        必须在 ``self._lock`` 内调用（读缓存）。过期/缺失一律返回 None：
        调用方据此跳过本帧的证据追加并下放 scale_stale 提示，绝不把过期
        读数当证据写入 _raw_window。
        """
        r = self._ble_reading
        if r is None:
            return None
        age_s = self._clock() - self._ble_received_monotonic
        if age_s > self.config.ble_stale_s:
            return None
        return float(r["grams"])

    def _ble_stale_or_missing(self) -> bool:
        """True 表示 BLE 模式下当前无新鲜读数（缓存空或过期）。

        必须在 ``self._lock`` 内调用。供 _process_locked 决定是否下放
        scale_stale 提示。
        """
        if self.weight_source != "ble_k797":
            return False
        return self._fresh_ble_grams() is None

    def _read_clear_weight(self, image: np.ndarray) -> float | None:
        """WAIT_CLEAR / RETRY_REQUESTED 用的重量读取（统一 OCR/BLE 路径）。

        OCR 模式：reader.read_weight。
        BLE 模式：新鲜缓存 grams，过期/缺失返回 None（保持现状，等下一帧）。

        所有重量消费路径（ARMED / WEIGHING 经 _read_weight_once；WAIT_CLEAR /
        RETRY_REQUESTED 经此方法）都经过 BLE 缓存，BLE 会话绝不调用 OCR
        reader 读取重量。
        """
        if self.weight_source in ("ble_k797", "manual"):
            # BLE：新鲜缓存 grams；manual：无自动来源，恒 None（靠 clear_timeout_s 超时回 ARMED）。
            return self._fresh_ble_grams() if self.weight_source == "ble_k797" else None
        weight, _conf = self.reader.read_weight(image)
        return weight

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
            epoch=self._weighing_epoch,
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
            # Legacy path: clear then re-arm. New retry goes straight to WEIGHING.
            self._handle_retry_requested(image, now=now, result=result)
        elif state == RealtimeState.ACCEPTED:
            self._handle_accepted(image, now=now, result=result)

        # 回填最近一次 weight_candidate，便于上层始终显示一个稳定数字。
        if result.weight_candidate is None and self._last_candidate is not None:
            result.weight_candidate = self._last_candidate
        if result.confidence <= 0.0 and self._last_confidence > 0.0:
            result.confidence = self._last_confidence

        # BLE 会话：在需要重量的状态（ARMED/WEIGHING/WAIT_CLEAR/RETRY_REQUESTED）
        # 下若缓存无新鲜读数，下放 scale_stale 提示。过期读数已被各 handler
        # 当作 None 处理（不写入 _raw_window），状态推进自然暂停。
        stale_states = {
            RealtimeState.ARMED,
            RealtimeState.WEIGHING,
            RealtimeState.WAIT_CLEAR,
            RealtimeState.RETRY_REQUESTED,
        }
        if self._state in stale_states and self._ble_stale_or_missing():
            result.quality_hints.append(
                QualityHint(code="scale_stale", message="天平广播中断")
            )

        # 透传原生 stable 标志（仅供客户端展示，不参与后端稳定窗判定）。
        if self.weight_source == "ble_k797" and self._ble_reading is not None:
            result.ble_stable = self._ble_reading.get("stable")

        result.state = self._state
        result.epoch = self._weighing_epoch
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
        cfg = self.config
        order_ok = self._accept_frame_order(frame_seq, client_ts_ms)

        weight, conf, lcd_box = self._read_weight_once(image)
        mouse_present = self._detect_mouse_smoothed(image, lcd_box)
        result.mouse_present = mouse_present
        result.confidence = float(conf)
        if weight is not None:
            result.weight_candidate = float(weight)
            self._last_candidate = float(weight)
            self._last_confidence = float(conf)

        if not order_ok:
            return

        if weight is None or conf < cfg.min_confidence or weight <= cfg.enter_min:
            # 回落 / 无效：清空进入证据，重新开始。
            self._enter_sustain = 0
            self._raw_window.clear()
            return

        # 可信非零读数：保留为当前 epoch 的原始证据（进入 WEIGHING 后不清空）。
        self._append_raw_read(
            frame_seq=frame_seq,
            client_ts_ms=client_ts_ms,
            weight=float(weight),
            confidence=float(conf),
        )
        self._enter_sustain += 1

        if self._enter_sustain >= max(1, cfg.enter_sustain_frames):
            self._enter_sustain = 0
            self._leave_count = 0
            # 不清空 _raw_window：ARMED 证据延续到 WEIGHING。
            self._transition(RealtimeState.WEIGHING)

    def _handle_weighing(
        self,
        image: np.ndarray,
        *,
        frame_seq: int,
        client_ts_ms: float,
        now: float,
        result: RealtimeFrameResult,
    ) -> None:
        cfg = self.config
        order_ok = self._accept_frame_order(frame_seq, client_ts_ms)

        weight, conf, lcd_box = self._read_weight_once(image)
        mouse_present = self._detect_mouse_smoothed(image, lcd_box)
        result.mouse_present = mouse_present
        result.confidence = float(conf)
        if weight is not None:
            result.weight_candidate = float(weight)
            self._last_candidate = float(weight)
            self._last_confidence = float(conf)

        if not mouse_present and cfg.mouse_advisory:
            result.quality_hints.append(
                QualityHint(code="mouse_uncertain", message="未稳定检测到小鼠，请确认秤盘")
            )

        if not order_ok:
            return

        # 1) 小鼠提前离开（重量连续低于 leave_max）→ 回到 ARMED。
        if weight is not None and weight <= cfg.leave_max:
            self._leave_count += 1
            if self._leave_count >= max(1, cfg.enter_sustain_frames):
                self._leave_count = 0
                self._reset_weighing()
                self._transition(RealtimeState.ARMED)
                return
        else:
            self._leave_count = 0

        # 2) 有效原始读数进入稳定窗。
        if weight is None or conf < cfg.min_confidence or weight <= cfg.enter_min:
            return

        self._append_raw_read(
            frame_seq=frame_seq,
            client_ts_ms=client_ts_ms,
            weight=float(weight),
            confidence=float(conf),
        )

        # 3) mouse_advisory=False 时鼠检测为硬门槛（明确语义，避免模糊）。
        if not mouse_present and not cfg.mouse_advisory:
            return

        stable = self._stable_suffix()
        if stable is None:
            return

        suffix_w, suffix_conf = stable

        # 候选确认期：stable_min_raw_reads 条一致只形成 pending；需再等
        # stable_confirm_raw_reads 条独立读数仍在容差内才播报。这挡住了
        # ARMED 延续进 WEIGHING 的旧平台残留（16.14×3 不会立即播报）。
        pc = self._pending_candidate
        announced_w: float | None = None
        announced_conf: float | None = None

        if pc is None:
            # 首次形成候选。不播报，等后续独立确认读数。
            self._pending_candidate = _PendingCandidate(
                median_weight=suffix_w,
                median_confidence=suffix_conf,
                frame_seq=frame_seq,
                client_ts_ms=float(client_ts_ms),
                first_ts_ms=float(client_ts_ms),
                confirm_count=0,
            )
            return

        # 候选已存在：判断新读数是确认还是平台切换。
        tol = cfg.stable_weight_tol
        if abs(suffix_w - pc.median_weight) > tol:
            # 平台变化：撤销候选，用新读数重启候选。
            self._pending_candidate = _PendingCandidate(
                median_weight=suffix_w,
                median_confidence=suffix_conf,
                frame_seq=frame_seq,
                client_ts_ms=float(client_ts_ms),
                first_ts_ms=float(client_ts_ms),
                confirm_count=0,
            )
            return

        # 确认读数：仍在容差内。
        pc.confirm_count += 1
        # 更新中位数（用更新后缀的更稳健估计）。
        pc.median_weight = suffix_w
        pc.median_confidence = suffix_conf

        need = cfg.stable_confirm_raw_reads
        if pc.confirm_count < need:
            return
        # 可选的最小跨度校验：跨度不足则继续等下一条读数。
        if cfg.stable_min_span_ms > 0 and (client_ts_ms - pc.first_ts_ms) < cfg.stable_min_span_ms:
            return

        # 确认通过：播报。
        announced_w = suffix_w
        announced_conf = suffix_conf

        attempt = Attempt(
            attempt_id=uuid.uuid4().hex[:12],
            weight_g=round(announced_w, 2),
            confidence=float(announced_conf),
            frame_seq=frame_seq,
            client_ts_ms=float(client_ts_ms),
            state="announced",
            created_at=now,
            # BLE 会话：把当前缓存的 raw 挂到 attempt 上，供 finalize 写
            # record.json 的 weight_raw。OCR 会话保持 None。
            weight_raw=(
                int(self._ble_reading["raw"])
                if (self.weight_source == "ble_k797" and self._ble_reading is not None)
                else None
            ),
        )
        self._attempts.append(attempt)
        self._current_attempt = attempt
        self._announce_at = now
        self._raw_window.clear()
        self._stable_run.clear()
        self._pending_candidate = None
        self._transition(RealtimeState.ANNOUNCED)
        result.attempt = attempt
        result.weight_candidate = attempt.weight_g
        result.confidence = attempt.confidence

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
                self._reset_weighing()
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
            self._reset_weighing()
            self._transition(RealtimeState.ARMED)
            return

        # 不强求 OCR 成功：读不到就保持现状，等下一帧。
        weight = self._read_clear_weight(image)
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
        self._reset_weighing()
        self._weighing_epoch += 1
        self._transition(RealtimeState.ARMED)

    def _handle_retry_requested(
        self,
        image: np.ndarray,
        *,
        now: float,
        result: RealtimeFrameResult,
    ) -> None:
        """Legacy RETRY_REQUESTED：等空秤后回 ARMED。新路径不再进入此状态。"""
        cfg = self.config
        if (now - self._wait_clear_at) >= cfg.clear_timeout_s:
            self._clear_count = 0
            self._reset_weighing()
            self._transition(RealtimeState.ARMED)
            return

        weight = self._read_clear_weight(image)
        if weight is not None:
            result.weight_candidate = float(weight)
        if weight is not None and weight <= cfg.empty_max:
            self._clear_count += 1
        else:
            self._clear_count = 0
        if self._clear_count >= 1:
            self._clear_count = 0
            self._reset_weighing()
            self._transition(RealtimeState.ARMED)
