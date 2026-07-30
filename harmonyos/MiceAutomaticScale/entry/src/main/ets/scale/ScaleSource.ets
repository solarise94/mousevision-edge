/**
 * ScaleSource.ets
 *
 * 统一读数源接口与共享工具：
 * - ScaleSource 接口：start/stop/onReading/onStatus/readonly state；
 * - ScaleSourceBase：分配单调 sequence、按 ≥3 条连续相同 raw 派生 stable、
 *   维护 scanning→active→stale（>10s 无合法包）状态转移；
 * - MonotonicClock：单调时钟抽象，stale 判定不得用墙钟。
 *
 * 真实扫描（BleK797Source）、广播回放（ReplayScaleSource）、脚本场景
 * （ScriptScaleSource）均实现此接口，使上层 Bridge 与状态机完全一致。
 */

import { ScaleReading, type ScaleSourceKind, type StableSource } from './ScaleReading';
import { ScaleStatus, type ScaleState } from './ScaleStatus';
import { ParseResult, ParseOk } from './K797AdvertisementParser';

/**
 * setTimeout 句柄类型。ArkUI 宿主下 setTimeout 返回 number，
 * 故统一用 number 表示定时器句柄，无需 any/unknown 转换或 ReturnType 查询。
 */
type TimerHandle = number;

/** 读数回调。 */
export type ReadingCallback = (reading: ScaleReading) => void;
/** 状态回调。 */
export type StatusCallback = (status: ScaleStatus) => void;

/** 统一读数源接口。 */
export interface ScaleSource {
  /** 来源名（ble / replay / script / hardware_emulator）。 */
  readonly sourceName: ScaleSourceKind;
  /** 当前状态（只读视图）。 */
  readonly state: ScaleState;
  /** 注册读数回调。 */
  onReading(cb: ReadingCallback): void;
  /** 注册状态回调。 */
  onStatus(cb: StatusCallback): void;
  /** 启动（幂等：重复调用安全）。 */
  start(): void;
  /** 停止（幂等）。 */
  stop(): void;
}

/** stale 阈值：超过此时间无合法包则进入 stale（毫秒，单调时钟）。 */
export const STALE_THRESHOLD_MS: number = 10_000;
/** 派生稳定所需连续相同 raw 的最小条数。 */
export const STABLE_MIN_REPEAT: number = 3;
/** device 固定为 K797（来源无关）。 */
export const SCALE_DEVICE: string = 'K797';

/**
 * 单调时钟抽象。默认实现用 Date.now() 作为回退；真机优先注入
 * @kit.BasicServicesKit 的 systemTime.getCurrentTime()（或自维护计数器）。
 * 注意：stale 判定必须单调，墙钟回拨会导致误判。
 *
 * TODO(Phase-0): 真机核实 systemTime API 名称（@kit.BasicServicesKit
 *   systemTime.getCurrentTime(false) 返回毫秒），并替换默认实现。
 */
export interface MonotonicClock {
  nowMs(): number;
}

/** 默认墙钟实现，仅用于无系统依赖的单元测试与回退。 */
export class WallClock implements MonotonicClock {
  nowMs(): number {
    return Date.now();
  }
}

/**
 * 共享基类：实现来源无关的序列分配、稳定派生、状态转移与回调分发。
 * 子类只需在收到原始广播/事件时调用 emitParseResult 或 emitReading。
 */
export abstract class ScaleSourceBase implements ScaleSource {
  readonly sourceName: ScaleSourceKind;
  private _state: ScaleState = 'off';
  private readingCb: ReadingCallback | null = null;
  private statusCb: StatusCallback | null = null;

  private sequenceCounter: number = 0;
  private lastReadingAtMonotonicMs: number | null = null;
  private lastReadingAtEpochMs: number | null = null;
  private lastRaw: number | null = null;
  private consecutiveSameRaw: number = 0;

  protected readonly clock: MonotonicClock;
  private staleTimerId: number = -1;
  private started: boolean = false;

  constructor(sourceName: ScaleSourceKind, clock: MonotonicClock = new WallClock()) {
    this.sourceName = sourceName;
    this.clock = clock;
  }

  get state(): ScaleState {
    return this._state;
  }

  onReading(cb: ReadingCallback): void {
    this.readingCb = cb;
  }

  onStatus(cb: StatusCallback): void {
    this.statusCb = cb;
  }

  start(): void {
    if (this.started) {
      return;
    }
    this.started = true;
    this.sequenceCounter = 0;
    this.lastReadingAtMonotonicMs = null;
    this.lastReadingAtEpochMs = null;
    this.lastRaw = null;
    this.consecutiveSameRaw = 0;
    this.transition('scanning', '正在扫描 K797 广播');
    this.onStart();
  }

  stop(): void {
    if (!this.started) {
      return;
    }
    this.started = false;
    this.cancelStaleCheck();
    this.onStop();
    this.transition('off', '扫描已停止');
  }

  /** 子类实现：真正启动扫描/回放/脚本。 */
  protected abstract onStart(): void;
  /** 子类实现：真正停止。 */
  protected abstract onStop(): void;

  /**
   * 处理解析结果。成功则分配 sequence、派生 stable、推送读数与 active 状态，
   * 并重排 stale 检查；失败则忽略（拒绝计数由 diagnostics 单独维护）。
   *
   * @param result       解析器结果
   * @param receivedAtEpochMs 接收 epoch（审计）
   * @param receivedAtMonotonicMs 接收单调时间（stale 判定）
   */
  protected handleParseResult(
    result: ParseResult,
    receivedAtEpochMs: number,
    receivedAtMonotonicMs: number,
  ): void {
    if (!result.ok) {
      return;
    }
    const reading = (result as ParseOk).reading;
    // 派生 stable：连续相同 raw ≥ STABLE_MIN_REPEAT
    const stable = this.deriveStable(reading.raw);
    const finalReading = this.withStable(reading, stable);
    this.sequenceCounter += 1;
    // 注意：reading.sequence 来自解析调用方传入；这里由 base 统一覆盖，
    // 保证进程单调（解析器只做协议工作，不关心序号分配）。
    const seq = this.sequenceCounter;
    const stamped = this.restampSequence(finalReading, seq);
    this.lastReadingAtMonotonicMs = receivedAtMonotonicMs;
    this.lastReadingAtEpochMs = receivedAtEpochMs;
    this.dispatchReading(stamped);
    this.transition('active', '已收到 K797 广播');
    this.scheduleStaleCheck();
  }

  /**
   * 直接派发一个已构造好的读数（脚本/回放源使用）。
   * 会按连续相同 raw 派生 stable，并由 base 统一分配单调 sequence。
   *
   * @param seqOverride 可选序号覆盖，仅用于 duplicate_out_of_order 等
   *   显式制造序号异常的脚本场景；正常应为 undefined。
   */
  protected emitReading(
    reading: ScaleReading,
    receivedAtMonotonicMs: number,
    seqOverride: number | null = null,
  ): void {
    const stable = this.deriveStable(reading.raw);
    let stamped = this.withStable(reading, stable);
    const seq = seqOverride !== null ? seqOverride : (() => {
      this.sequenceCounter += 1;
      return this.sequenceCounter;
    })();
    stamped = this.restampSequence(stamped, seq);
    this.lastReadingAtMonotonicMs = receivedAtMonotonicMs;
    this.lastReadingAtEpochMs = reading.receivedAtEpochMs;
    this.dispatchReading(stamped);
    this.transition('active', '已收到读数');
    this.scheduleStaleCheck();
  }

  /** 由子类在权限被拒时调用。 */
  protected reportUnauthorized(message: string): void {
    this.transition('unauthorized', message);
  }

  /** 由子类在蓝牙关闭时调用。 */
  protected reportBluetoothOff(message: string): void {
    this.transition('bluetooth_off', message);
  }

  /** 由子类在扫描失败时调用。 */
  protected reportError(message: string): void {
    this.transition('error', message);
  }

  /** 派生 stable：连续相同 raw ≥ STABLE_MIN_REPEAT。 */
  private deriveStable(raw: number): boolean {
    if (this.lastRaw !== null && this.lastRaw === raw) {
      this.consecutiveSameRaw += 1;
    } else {
      this.consecutiveSameRaw = 1;
    }
    this.lastRaw = raw;
    return this.consecutiveSameRaw >= STABLE_MIN_REPEAT;
  }

  private withStable(reading: ScaleReading, stable: boolean): ScaleReading {
    if (reading.stable === stable) {
      return reading;
    }
    const stableSource: StableSource | null = stable ? 'derived_repeat' : null;
    return new ScaleReading({
      device: reading.device,
      deviceKey: reading.deviceKey,
      grams: reading.grams,
      raw: reading.raw,
      rssi: reading.rssi,
      receivedAt: reading.receivedAt,
      receivedAtEpochMs: reading.receivedAtEpochMs,
      sequence: reading.sequence,
      stable,
      stableSource,
      source: reading.source,
      address: reading.address,
      payloadHex: reading.payloadHex,
    });
  }

  private restampSequence(reading: ScaleReading, sequence: number): ScaleReading {
    return new ScaleReading({
      device: reading.device,
      deviceKey: reading.deviceKey,
      grams: reading.grams,
      raw: reading.raw,
      rssi: reading.rssi,
      receivedAt: reading.receivedAt,
      receivedAtEpochMs: reading.receivedAtEpochMs,
      sequence,
      stable: reading.stable,
      stableSource: reading.stableSource,
      source: reading.source,
      address: reading.address,
      payloadHex: reading.payloadHex,
    });
  }

  /** 状态转移；仅当变化或强制刷新时回调。 */
  protected transition(state: ScaleState, message: string, force: boolean = false): void {
    if (!force && this._state === state && state !== 'active') {
      // 同状态不重复触发，但 active 每包都刷新 message 也无妨；这里仍只回调一次。
      return;
    }
    this._state = state;
    const status = new ScaleStatus({
      device: SCALE_DEVICE,
      state,
      message,
      lastReadingAtEpochMs: this.lastReadingAtEpochMs,
      source: this.sourceName,
    });
    if (this.statusCb !== null) {
      this.statusCb(status);
    }
  }

  private dispatchReading(reading: ScaleReading): void {
    if (this.readingCb !== null) {
      this.readingCb(reading);
    }
  }

  /** 安排一次 stale 检查（10s 后），覆盖之前的。 */
  private scheduleStaleCheck(): void {
    this.cancelStaleCheck();
    // setTimeout 在 ArkTS/浏览器宿主可用；如目标环境无全局 setTimeout，
    // 子类可覆盖此方法用系统定时器。
    // TODO(Phase-0): 真机优先用 @ohos.workScheduler 或 ability 内定时器，
    //   并核实后台时定时器是否被冻结。
    if (typeof setTimeout !== 'undefined') {
      this.staleTimerId = setTimeout(() => this.checkStale(), STALE_THRESHOLD_MS) as number;
    }
  }

  private cancelStaleCheck(): void {
    if (this.staleTimerId !== -1 && typeof clearTimeout !== 'undefined') {
      clearTimeout(this.staleTimerId);
      this.staleTimerId = -1;
    }
  }

  /** 检查是否进入 stale：单调时钟下超过阈值且未收到新包。 */
  private checkStale(): void {
    this.staleTimerId = -1;
    if (this._state !== 'active' && this._state !== 'scanning') {
      return;
    }
    if (this.lastReadingAtMonotonicMs === null) {
      // scanning 状态下还没收到包，继续保持 scanning，不进入 stale。
      return;
    }
    const elapsed = this.clock.nowMs() - this.lastReadingAtMonotonicMs;
    if (elapsed >= STALE_THRESHOLD_MS) {
      // stale 绝不产生 0 g 读数：这里只切状态，不派发任何读数。
      this.transition('stale', '超过 10 秒未收到合法 K797 广播');
    } else {
      // 还没到阈值，继续等待剩余时间。
      const remain = STALE_THRESHOLD_MS - elapsed;
      if (typeof setTimeout !== 'undefined') {
        this.staleTimerId = setTimeout(() => this.checkStale(), remain) as number;
      }
    }
  }

  /** 供测试注入虚拟时钟触发 stale 检查。 */
  forceStaleCheckForTest(): void {
    this.checkStale();
  }
}
