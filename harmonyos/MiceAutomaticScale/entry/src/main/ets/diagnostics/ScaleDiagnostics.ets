/**
 * ScaleDiagnostics.ets
 *
 * 诊断计数器与原始 payload 环形缓冲。debug 面板读取展示，
 * release 不展示但可写入受限日志。所有 payload 日志设容量上限，
 * 避免长时运行（4h soak）导致无界增长。
 */

import type { ParseRejectReason } from '../scale/K797AdvertisementParser';
import { toHexSpaces } from '../scale/K797AdvertisementParser';

/** payload 环形缓冲容量上限。 */
const PAYLOAD_RING_CAP: number = 50;

/** 单条诊断快照（不可变）。 */
export interface DiagnosticsSnapshot {
  /** 启动以来的总广播数（含被拒）。 */
  totalAdvertisements: number;
  /** 通过解析的广播数。 */
  accepted: number;
  /** 各拒绝原因计数。 */
  rejects: Record<string, number>;
  /** 最近 payloadHex（环形，最新在前）。 */
  recentPayloadHex: string[];
  /** 当前来源名。 */
  source: string;
  /** 进程运行毫秒。 */
  uptimeMs: number;
}

/**
 * 诊断聚合器。非线程安全（ArkTS 单线程 JS 模型，无需锁）。
 */
export class ScaleDiagnostics {
  private total: number = 0;
  private accept: number = 0;
  private readonly rejectCounts: Map<ParseRejectReason, number> = new Map();
  private readonly payloadRing: string[] = [];
  private source: string = 'unknown';
  private readonly startMs: number;

  constructor() {
    this.startMs = Date.now();
  }

  setSource(source: string): void {
    this.source = source;
  }

  /** 记录一条广告（无论是否通过）。 */
  recordAdvertisement(): void {
    this.total += 1;
  }

  /** 记录一次通过。 */
  recordAccepted(): void {
    this.accept += 1;
  }

  /** 记录一次拒绝。 */
  recordReject(reason: ParseRejectReason): void {
    this.rejectCounts.set(reason, (this.rejectCounts.get(reason) ?? 0) + 1);
  }

  /** 记录原始 payload（转为 hex 入环形缓冲，有容量上限）。 */
  recordPayload(payload: Uint8Array): void {
    const hex = toHexSpaces(payload);
    this.payloadRing.unshift(hex);
    if (this.payloadRing.length > PAYLOAD_RING_CAP) {
      this.payloadRing.length = PAYLOAD_RING_CAP;
    }
  }

  /** 取当前快照（深拷贝，避免外部修改）。 */
  snapshot(): DiagnosticsSnapshot {
    const rejects: Record<string, number> = {};
    this.rejectCounts.forEach((v, k) => { rejects[k] = v; });
    return {
      totalAdvertisements: this.total,
      accepted: this.accept,
      rejects,
      recentPayloadHex: this.payloadRing.slice(0),
      source: this.source,
      uptimeMs: Date.now() - this.startMs,
    };
  }

  /** 重置（切换来源或重启时调用）。 */
  reset(): void {
    this.total = 0;
    this.accept = 0;
    this.rejectCounts.clear();
    this.payloadRing.length = 0;
  }
}
