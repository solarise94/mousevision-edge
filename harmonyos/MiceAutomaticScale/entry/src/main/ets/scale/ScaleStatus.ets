/**
 * ScaleStatus.ets
 *
 * 天平状态契约，匹配 docs/HARMONYOS_K797_BLE_INTEGRATION_PLAN.md §5.2。
 * 状态机：off → scanning → active ⇄ stale；以及异常分支
 * unauthorized / bluetooth_off / error。
 *
 * 关键约束：
 * - stale 必须用单调时钟判定（参见 ScaleSource 中的 MonotonicClock），
 *   不能用墙钟 Date.now()，避免系统时间回拨误判；
 * - stale 绝不等于 0 g：没有合法包就是没有读数，不能产生 0.0 g 读数；
 * - 审计字段 lastReadingAtEpochMs 仍保存 epoch 毫秒。
 */

import type { ScaleSourceKind } from './ScaleReading';

/** 状态枚举（字符串字面量联合，便于序列化与跨语言一致）。 */
export type ScaleState =
  | 'off'
  | 'scanning'
  | 'active'
  | 'stale'
  | 'unauthorized'
  | 'bluetooth_off'
  | 'error';

/**
 * ScaleStatus 构造参数。提取为独立 interface，避免 ArkTS 严格模式下
 * 禁止把对象字面量类型直接用作参数类型（arkts-no-obj-literals-as-types），
 * 同时让所有 `new ScaleStatus({...})` 调用处自动获得类型。
 */
export interface ScaleStatusParams {
  device: string;
  state: ScaleState;
  message: string;
  lastReadingAtEpochMs: number | null;
  source: ScaleSourceKind | null;
}

/** 不可变状态快照。 */
export class ScaleStatus {
  readonly device: string;
  readonly state: ScaleState;
  readonly message: string;
  /** 最近一次合法读数的 epoch 毫秒；无则 null。 */
  readonly lastReadingAtEpochMs: number | null;
  /** 当前读数来源。 */
  readonly source: ScaleSourceKind | null;

  constructor(params: ScaleStatusParams) {
    this.device = params.device;
    this.state = params.state;
    this.message = params.message;
    this.lastReadingAtEpochMs = params.lastReadingAtEpochMs;
    this.source = params.source;
  }

  toJson(): string {
    const last = this.lastReadingAtEpochMs === null ? 'null' : String(this.lastReadingAtEpochMs);
    const src = this.source === null ? 'null' : `"${this.source}"`;
    return '{'
      + `"device":"${this.device}"`
      + `,"state":"${this.state}"`
      + `,"message":"${escapeJson(this.message)}"`
      + `,"lastReadingAtEpochMs":${last}`
      + `,"source":${src}`
      + '}';
  }
}

/** 极简 JSON 字符串转义，避免引入 json 模块依赖差异。 */
function escapeJson(value: string): string {
  let out = '';
  for (let i = 0; i < value.length; i++) {
    const ch = value.charAt(i);
    switch (ch) {
      case '"': out += '\\"'; break;
      case '\\': out += '\\\\'; break;
      case '\n': out += '\\n'; break;
      case '\r': out += '\\r'; break;
      case '\t': out += '\\t'; break;
      default: out += ch;
    }
  }
  return out;
}
