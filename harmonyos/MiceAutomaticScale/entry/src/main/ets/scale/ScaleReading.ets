/**
 * ScaleReading.ets
 *
 * 统一读数契约，匹配 docs/HARMONYOS_K797_BLE_INTEGRATION_PLAN.md §5.1。
 * 所有真实与虚拟来源（BleK797Source / ReplayScaleSource / ScriptScaleSource
 * / 硬件模拟器）都产出相同的不可变 ScaleReading，业务侧只依赖此契约。
 *
 * 字段规则：
 * - grams 仅由协议换算（raw / 10.0），原生侧不做业务修正；
 * - stable 目前不是已确认的协议位，只能由重复广播派生，
 *   因此 stableSource 必须是 "derived_repeat"（或 stable=false 时缺省）；
 * - source 取 ble / replay / script / hardware_emulator；
 * - sequence 为应用进程内单调序号，绝不使用随机 BLE 地址；
 * - address 仅用于诊断（K797 地址为随机私有地址，不作设备主键）。
 */

/** 读数来源，与 ScaleSource 实现一一对应。 */
export type ScaleSourceKind = 'ble' | 'replay' | 'script' | 'hardware_emulator';

/**
 * stable 的派生来源。当前仅支持由重复广播派生；
 * 未来若协议确认稳定位，再扩展 'protocol_bit' 等。
 */
export type StableSource = 'derived_repeat';

/**
 * 不可变读数。ArkTS 严格模式下使用 class + 只读字段；
 * 构造完成后不再可变，所有变更由来源重新生成新实例。
 */
export class ScaleReading {
  /** 契约版本，当前固定为 1。 */
  readonly schemaVersion: number = 1;
  /** 设备名，固定 "K797"。 */
  readonly device: string;
  /** 设备主键：k797:<manufacturerId 十六进制>:<前缀十六进制>，不含 BLE 地址。 */
  readonly deviceKey: string;
  /** 重量（克），等于 raw / 10.0。 */
  readonly grams: number;
  /** 协议原始重量值（UInt16LE）。 */
  readonly raw: number;
  /** 广播 RSSI，可为 null（脚本/回放源可能没有）。 */
  readonly rssi: number | null;
  /** ISO8601 UTC 字符串，审计用。 */
  readonly receivedAt: string;
  /** 接收 epoch 毫秒。 */
  readonly receivedAtEpochMs: number;
  /** 进程内单调序号，由来源分配。 */
  readonly sequence: number;
  /** 是否稳定（仅由重复广播派生）。 */
  readonly stable: boolean;
  /** 稳定来源；stable=false 时为 null。 */
  readonly stableSource: StableSource | null;
  /** 读数来源。 */
  readonly source: ScaleSourceKind;
  /** BLE 地址，仅诊断用（固定为 "diagnostic-only" 以避免泄露轮换地址）。 */
  readonly address: string;
  /** 完整 payload 的空格分隔大写十六进制，仅诊断与数据集积累。 */
  readonly payloadHex: string;

  constructor(params: {
    device: string;
    deviceKey: string;
    grams: number;
    raw: number;
    rssi: number | null;
    receivedAt: string;
    receivedAtEpochMs: number;
    sequence: number;
    stable: boolean;
    stableSource: StableSource | null;
    source: ScaleSourceKind;
    address: string;
    payloadHex: string;
  }) {
    this.device = params.device;
    this.deviceKey = params.deviceKey;
    this.grams = params.grams;
    this.raw = params.raw;
    this.rssi = params.rssi;
    this.receivedAt = params.receivedAt;
    this.receivedAtEpochMs = params.receivedAtEpochMs;
    this.sequence = params.sequence;
    this.stable = params.stable;
    this.stableSource = params.stableSource;
    this.source = params.source;
    this.address = params.address;
    this.payloadHex = params.payloadHex;
  }

  /** 序列化为 JSON 字符串，键名严格匹配契约（camelCase）。 */
  toJson(): string {
    // 手工拼接以保证字段顺序与契约一致，并稳定处理 null。
    const rssiStr = this.rssi === null ? 'null' : String(this.rssi);
    const stableSourceStr = this.stableSource === null ? 'null' : `"${this.stableSource}"`;
    return '{'
      + `"schemaVersion":${this.schemaVersion}`
      + `,"device":"${this.device}"`
      + `,"deviceKey":"${this.deviceKey}"`
      + `,"grams":${formatNumber(this.grams)}`
      + `,"raw":${this.raw}`
      + `,"rssi":${rssiStr}`
      + `,"receivedAt":"${this.receivedAt}"`
      + `,"receivedAtEpochMs":${this.receivedAtEpochMs}`
      + `,"sequence":${this.sequence}`
      + `,"stable":${this.stable}`
      + `,"stableSource":${stableSourceStr}`
      + `,"source":"${this.source}"`
      + `,"address":"${this.address}"`
      + `,"payloadHex":"${this.payloadHex}"`
      + '}';
  }
}

/** 把数字按协议需要格式化：整数不带小数，非整数保留必要小数位。 */
function formatNumber(value: number): string {
  if (Number.isInteger(value)) {
    return value.toFixed(1);
  }
  // raw/10.0 最多一位小数，但避免浮点尾差，先四舍五入到 1 位。
  return (Math.round(value * 10) / 10).toString();
}
