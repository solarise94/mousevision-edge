/**
 * ScriptScaleSource.ets
 *
 * 不依赖 BLE 硬件的脚本场景读数源。加载 rawfile/scenarios/*.json，
 * 按事件时间轴以 ~5 Hz（200ms）产生重复广播读数，模拟真实天平重复广播。
 *
 * 场景格式（与 docs §9.2 一致）：
 * {
 *   "name": "mouse_normal_26_3g",
 *   "repeat": false,
 *   "events": [
 *     { "atMs": 0, "grams": 0.0 },
 *     { "atMs": 1500, "grams": 8.1 },
 *     { "atMs": 2800, "gap": 12000 },          // 广播中断
 *     { "atMs": 16000, "revoke": "permission" } // 权限撤销演练
 *   ]
 * }
 *
 * 广播节奏：从事件 atMs 起，以 200ms 间隔重复发出该事件的 grams，
 * 直到下一个事件接管（真实秤会重复广播）。gapMs 字段模拟静默；
 * revoke 事件驱动状态机进入 unauthorized。
 *
 * 重复/乱序场景支持可选 "seqOverride" 字段（见 duplicate_out_of_order），
 * 用于显式制造序号异常；该字段仅由脚本源注入，生产 BLE 源不会出现。
 */

import { ScaleReading, type StableSource } from './ScaleReading';
import { ScaleSourceBase, type MonotonicClock } from './ScaleSource';
import {
  K797_MANUFACTURER_ID, K797_PREFIX, K797_DEVICE_NAME, K797_MIN_PAYLOAD_BYTES,
  buildDeviceKey, toHexSpaces, toIsoUtc,
} from './K797AdvertisementParser';

/** 速度倍率。 */
export type SpeedMultiplier = 0.5 | 1 | 2 | 4;

/**
 * 场景中的事件（联合类型）。ArkTS 禁止类型谓词（`x is Y`），
 * 故用 `type` 字面量判别字段区分联合分支；场景 JSON 加载时由调用方注入 type。
 */
export interface GramsEvent { type: 'grams'; atMs: number; grams: number; seqOverride?: number; }
export interface GapEvent { type: 'gap'; atMs: number; gap: number; }
export interface RevokeEvent { type: 'revoke'; atMs: number; revoke: 'permission'; }
export type ScenarioEvent = GramsEvent | GapEvent | RevokeEvent;

/** 场景定义。 */
export interface Scenario {
  name: string;
  repeat: boolean;
  events: ScenarioEvent[];
}

/** 广播间隔（毫秒），对应 ~5 Hz。 */
const BROADCAST_INTERVAL_MS: number = 200;

/** 判别函数：基于 `type` 字段返回 boolean（ArkTS 禁止类型谓词）。 */
function isGramsEvent(e: ScenarioEvent): boolean {
  return e.type === 'grams';
}
function isGapEvent(e: ScenarioEvent): boolean {
  return e.type === 'gap';
}
function isRevokeEvent(e: ScenarioEvent): boolean {
  return e.type === 'revoke';
}

/**
 * 脚本读数源。构造后通过 loadScenario 注入场景，再 start()。
 */
export class ScriptScaleSource extends ScaleSourceBase {
  private scenario: Scenario | null = null;
  private speed: SpeedMultiplier = 1;
  private timerId: number = -1;
  private running: boolean = false;
  private paused: boolean = false;

  // 虚拟场景内部时钟（场景时间，毫秒），从 0 起递增；倍速时按 speed 推进。
  private virtualNowMs: number = 0;
  // 上一次 tick 的真实单调时间，用于按真实间隔推进 virtualNowMs。
  private lastTickMonotonicMs: number = 0;
  // 当前所处事件的索引与该事件已发出的广播次数。
  private currentEventIndex: number = 0;

  constructor(clock: MonotonicClock) {
    super('script', clock);
  }

  /** 加载场景（start 前或运行中均可切换，切换会重置时间轴）。 */
  loadScenario(scenario: Scenario): void {
    this.scenario = scenario;
    this.resetTimeline();
  }

  /** 设置倍速（运行中调整即时生效）。 */
  setSpeed(speed: SpeedMultiplier): void {
    this.speed = speed;
  }

  /** 暂停（保留进度）。 */
  pause(): void {
    if (!this.running) {
      return;
    }
    this.paused = true;
    this.cancelTimer();
  }

  /** 恢复。 */
  resume(): void {
    if (!this.running || !this.paused) {
      return;
    }
    this.paused = false;
    this.lastTickMonotonicMs = this.clock.nowMs();
    this.scheduleTick();
  }

  isPaused(): boolean {
    return this.paused;
  }

  protected onStart(): void {
    if (this.scenario === null) {
      this.reportError('未加载场景');
      return;
    }
    this.running = true;
    this.paused = false;
    this.resetTimeline();
    this.lastTickMonotonicMs = this.clock.nowMs();
    this.scheduleTick();
  }

  protected onStop(): void {
    this.running = false;
    this.paused = false;
    this.cancelTimer();
  }

  private resetTimeline(): void {
    this.virtualNowMs = 0;
    this.currentEventIndex = 0;
  }

  private scheduleTick(): void {
    this.cancelTimer();
    if (typeof setTimeout === 'undefined') {
      return;
    }
    this.timerId = setTimeout(() => this.tick(), BROADCAST_INTERVAL_MS) as number;
  }

  private cancelTimer(): void {
    if (this.timerId !== -1 && typeof clearTimeout !== 'undefined') {
      clearTimeout(this.timerId);
      this.timerId = -1;
    }
  }

  private tick(): void {
    this.timerId = -1;
    if (!this.running || this.paused || this.scenario === null) {
      return;
    }
    const nowMono = this.clock.nowMs();
    const realDelta = nowMono - this.lastTickMonotonicMs;
    this.lastTickMonotonicMs = nowMono;
    // 倍速：真实经过 * speed 推进虚拟时间。
    this.virtualNowMs += realDelta * this.speed;

    // 处理已到点的 revoke/gap 边界，并在当前 grams 事件窗口内发广播。
    this.advanceScenario();

    // 继续下一个 tick。
    this.scheduleTick();
  }

  /** 推进场景：处理事件边界并发出当前窗口的广播。 */
  private advanceScenario(): void {
    const scenario = this.scenario as Scenario;
    const events = scenario.events;
    if (events.length === 0) {
      return;
    }

    // 推进 currentEventIndex 到 virtualNowMs 所属事件（atMs <= virtualNowMs 的最后一个）。
    while (
      this.currentEventIndex + 1 < events.length &&
      events[this.currentEventIndex + 1].atMs <= this.virtualNowMs
    ) {
      this.currentEventIndex += 1;
    }

    const ev = events[this.currentEventIndex];

    // revoke 事件：进入 unauthorized 并停止发广播（但保持 running 以便恢复演练）。
    if (isRevokeEvent(ev)) {
      this.reportUnauthorized('场景演练：权限被撤销');
      return;
    }

    // gap 事件：在 gap 窗口内静默（不发广播），让 stale 检查自然触发。
    if (isGapEvent(ev)) {
      const gapEv = ev as GapEvent;
      const gapEnd = gapEv.atMs + gapEv.gap;
      if (this.virtualNowMs < gapEnd) {
        // 静默中；不发包，依赖 ScaleSourceBase 的 stale 检查。
        return;
      }
      // gap 已过，但没有 grams 字段；尝试用下一个事件的 grams 继续。
      const next = events[this.currentEventIndex + 1];
      if (next !== undefined && isGramsEvent(next)) {
        const nextG = next as GramsEvent;
        this.emitGrams(nextG.grams, nextG.seqOverride);
      }
      return;
    }

    // grams 事件：发出一次广播（每个 tick 一次，~5 Hz）。
    if (isGramsEvent(ev)) {
      const gramsEv = ev as GramsEvent;
      this.emitGrams(gramsEv.grams, gramsEv.seqOverride);
    }

    // 到达场景末尾：若 repeat 则循环，否则停止时间轴（保持最后读数直至 stale）。
    const isLast = this.currentEventIndex === events.length - 1;
    if (isLast) {
      const lastAt = ev.atMs;
      // 给末事件留 ~1s 余量后判定结束。
      if (this.virtualNowMs - lastAt > 1000) {
        if (scenario.repeat) {
          this.resetTimeline();
        }
        // 非 repeat：不再前进，继续重复发末事件广播（与真实秤一致），直到 stop。
      }
    }
  }

  /** 构造一条 grams 读数并派发。 */
  private emitGrams(grams: number, seqOverride: number | undefined): void {
    const raw = Math.round(grams * 10);
    const payload = buildPayload(raw);
    const receivedAtEpochMs = Date.now();
    const receivedAtMono = this.clock.nowMs();
    const deviceKey = buildDeviceKey(K797_MANUFACTURER_ID, K797_PREFIX);

    // 脚本源由 ScaleSourceBase 统一派生 stable 与 sequence；
    // seqOverride 仅用于显式制造序号异常场景（duplicate_out_of_order），
    // 通过覆盖 super 的 sequenceCounter 实现，文档化此异常注入点。
    const stable: boolean = false; // base 会按连续相同 raw 重新派生
    const stableSource: StableSource | null = null;
    const reading = new ScaleReading({
      device: K797_DEVICE_NAME,
      deviceKey,
      grams: raw / 10.0,
      raw,
      rssi: -60, // 脚本源给一个固定模拟 RSSI
      receivedAt: toIsoUtc(receivedAtEpochMs),
      receivedAtEpochMs,
      sequence: seqOverride !== undefined ? seqOverride : 0,
      stable,
      stableSource,
      source: 'script',
      address: 'diagnostic-only',
      payloadHex: toHexSpaces(payload),
    });
    // base 会按连续相同 raw 重新派生 stable，并统一分配单调 sequence。
    // seqOverride 非空时 base 保留该序号（用于 duplicate_out_of_order）。
    const override = seqOverride !== undefined ? seqOverride : null;
    this.emitReading(reading, receivedAtMono, override);
  }
}

/** 构造 18 字节 payload：9 字节前缀 + 2 字节小端重量 + 7 字节零。 */
function buildPayload(raw: number): Uint8Array {
  const payload = new Uint8Array(K797_MIN_PAYLOAD_BYTES);
  for (let i = 0; i < K797_PREFIX.length; i++) {
    payload[i] = K797_PREFIX[i];
  }
  payload[9] = raw & 0xFF;
  payload[10] = (raw >> 8) & 0xFF;
  // [11..17] 未确认，保持 0。
  return payload;
}
