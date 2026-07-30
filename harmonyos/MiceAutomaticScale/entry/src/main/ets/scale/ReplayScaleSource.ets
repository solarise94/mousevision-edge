/**
 * ReplayScaleSource.ets
 *
 * 回放 captured_raw.jsonl 原始广播包，逐行经过真实的 K797AdvertisementParser
 * （解析失败计入 diagnostics，不污染读数流），按原始时间戳差值配速推进，
 * 支持倍速。
 *
 * captured_raw.jsonl 每行格式：
 * {"local_name":"K797","manufacturer_id":0,
 *  "payload_hex":"CA E8 03 28 08 95 CA 02 10 07 01 ...",
 *  "rssi":-49,"received_at_epoch_ms":1785393390194}
 *
 * 与 ScriptScaleSource 不同：回放源复用真实解析路径，可复现协议问题，
 * 拒绝计数会真实反映解析器行为。
 */

import type { ScaleSourceKind } from './ScaleReading';
import { ScaleSourceBase, type MonotonicClock } from './ScaleSource';
import {
  parse, fromHexSpaces, ParseOk, ParseReject, ParseResult,
} from './K797AdvertisementParser';
import type { ScaleDiagnostics } from '../diagnostics/ScaleDiagnostics';

/** 单条原始捕获记录。 */
export interface CapturedPacket {
  local_name: string | null;
  manufacturer_id: number | null;
  payload_hex: string | null;
  rssi: number | null;
  received_at_epoch_ms: number;
}

/** 速度倍率。 */
export type ReplaySpeed = 0.5 | 1 | 2 | 4;

/** 极简 JSONL 解析（避免引入 JSON 流依赖差异）。 */
export function parseCapturedJsonl(text: string): CapturedPacket[] {
  const out: CapturedPacket[] = [];
  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (line.length === 0 || line.startsWith('#')) {
      continue;
    }
    const parsed = parseLine(line);
    if (parsed !== null) {
      out.push(parsed);
    }
  }
  return out;
}

function parseLine(line: string): CapturedPacket | null {
  try {
    // ArkTS 支持 JSON.parse；这里用宽松解析。
    const obj = JSON.parse(line) as Record<string, Object>;
    return {
      local_name: asStringOrNull(obj['local_name']),
      manufacturer_id: asNumberOrNull(obj['manufacturer_id']),
      payload_hex: asStringOrNull(obj['payload_hex']),
      rssi: asNumberOrNull(obj['rssi']),
      received_at_epoch_ms: asNumberOrZero(obj['received_at_epoch_ms']),
    };
  } catch (e) {
    return null;
  }
}

function asStringOrNull(v: Object | undefined): string | null {
  return typeof v === 'string' ? v : null;
}
function asNumberOrNull(v: Object | undefined): number | null {
  return typeof v === 'number' ? v : null;
}
function asNumberOrZero(v: Object | undefined): number {
  return typeof v === 'number' ? v : 0;
}

/**
 * 回放读数源。加载捕获包后 start()，按时间戳配速逐包回放。
 */
export class ReplayScaleSource extends ScaleSourceBase {
  private packets: CapturedPacket[] = [];
  private speed: ReplaySpeed = 1;
  private timerId: number = -1;
  private running: boolean = false;
  private cursor: number = 0;
  private playbackStartMonoMs: number = 0;
  private firstPacketEpochMs: number = 0;
  private diagnostics: ScaleDiagnostics | null = null;

  constructor(clock: MonotonicClock, diagnostics: ScaleDiagnostics | null = null) {
    super('replay', clock);
    this.diagnostics = diagnostics;
  }

  setDiagnostics(diagnostics: ScaleDiagnostics): void {
    this.diagnostics = diagnostics;
  }

  /** 加载捕获包文本（JSONL）。 */
  loadCapturedRaw(text: string): void {
    this.packets = parseCapturedJsonl(text);
    this.cursor = 0;
  }

  /** 直接注入已解析的包列表（测试或程序化构造时使用）。 */
  loadPackets(packets: CapturedPacket[]): void {
    this.packets = packets;
    this.cursor = 0;
  }

  setSpeed(speed: ReplaySpeed): void {
    this.speed = speed;
  }

  protected onStart(): void {
    if (this.packets.length === 0) {
      this.reportError('未加载捕获包');
      return;
    }
    this.running = true;
    this.cursor = 0;
    this.firstPacketEpochMs = this.packets[0].received_at_epoch_ms;
    this.playbackStartMonoMs = this.clock.nowMs();
    this.scheduleNext();
  }

  protected onStop(): void {
    this.running = false;
    this.cancelTimer();
  }

  private scheduleNext(): void {
    if (!this.running || this.cursor >= this.packets.length) {
      return;
    }
    if (typeof setTimeout === 'undefined') {
      return;
    }
    const pkt = this.packets[this.cursor];
    // 该包相对首包的偏移（毫秒），除以倍速得到真实应等待时间。
    const offsetMs = pkt.received_at_epoch_ms - this.firstPacketEpochMs;
    const waitReal = offsetMs / this.speed;
    const elapsedReal = this.clock.nowMs() - this.playbackStartMonoMs;
    const remain = Math.max(0, waitReal - elapsedReal);
    this.timerId = setTimeout(() => this.flushDue(), remain) as number;
  }

  private flushDue(): void {
    this.timerId = -1;
    if (!this.running) {
      return;
    }
    const nowReal = this.clock.nowMs();
    // 把所有“已到点”的包一次性发出。
    while (this.cursor < this.packets.length) {
      const pkt = this.packets[this.cursor];
      const offsetMs = pkt.received_at_epoch_ms - this.firstPacketEpochMs;
      const dueReal = this.playbackStartMonoMs + offsetMs / this.speed;
      if (dueReal > nowReal) {
        break;
      }
      this.replayOne(pkt);
      this.cursor += 1;
    }
    if (this.cursor >= this.packets.length) {
      // 回放结束；保持最后读数直至 stale，不主动 stop。
      return;
    }
    this.scheduleNext();
  }

  private cancelTimer(): void {
    if (this.timerId !== -1 && typeof clearTimeout !== 'undefined') {
      clearTimeout(this.timerId);
      this.timerId = -1;
    }
  }

  /** 把单条捕获包喂给真实解析器，成功则派发，失败计入 diagnostics。 */
  private replayOne(pkt: CapturedPacket): void {
    const payload = pkt.payload_hex !== null ? fromHexSpaces(pkt.payload_hex) : null;
    const receivedMono = this.clock.nowMs();
    const result: ParseResult = parse(
      pkt.local_name,
      pkt.manufacturer_id,
      payload,
      pkt.rssi,
      pkt.received_at_epoch_ms,
      0,
      'replay',
    );
    if (this.diagnostics !== null) {
      this.diagnostics.recordAdvertisement();
      if (result.ok) {
        this.diagnostics.recordAccepted();
      } else {
        this.diagnostics.recordReject((result as ParseReject).reason);
      }
    }
    if (result.ok) {
      // base 会重新分配 sequence 与派生 stable。
      this.emitReading((result as ParseOk).reading, receivedMono);
    }
  }

  /** 仅用于消除未使用导入告警（sourceName 已由基类提供）。 */
  static readonly KIND: ScaleSourceKind = 'replay';
}
