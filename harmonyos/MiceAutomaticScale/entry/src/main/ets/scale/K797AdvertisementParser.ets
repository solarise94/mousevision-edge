/**
 * K797AdvertisementParser.ets
 *
 * 纯函数 BLE 广播解析器，零系统依赖（无 @kit.*、无 @ohos.* 导入）。
 * 与 Python 侧 / Android 侧解析逻辑保持一致，跨语言 fixture 共用。
 *
 * 解析步骤（顺序严格，命中即返回 ParseReject，不抛异常）：
 *   1. 校验 Local Name == "K797"            → wrong_name
 *   2. 校验 Manufacturer ID == 0x0000        → wrong_manufacturer_id
 *   3. 校验 payload 长度 >= 18               → payload_too_short
 *   4. 常量时间比较前 9 字节前缀              → wrong_prefix
 *   5. raw = payload[9] | payload[10]<<8（小端）
 *      范围 [0, 6553.5]（UInt16 上限 / 10）  → weight_out_of_protocol_range
 *   6. 生成不可变 ScaleReading（source/address 由调用方覆盖）。
 *
 * payload[11..17] 当前未确认含义，不解释。
 */

import { ScaleReading, type ScaleSourceKind, type StableSource } from './ScaleReading';

/** 已确认的 K797 协议常量。 */
export const K797_DEVICE_NAME: string = 'K797';
export const K797_MANUFACTURER_ID: number = 0x0000;
export const K797_MIN_PAYLOAD_BYTES: number = 18;
/** UInt16 最大值 / 10.0 = 6553.5 g。 */
export const K797_MAX_GRAMS: number = 6553.5;
/** 9 字节前缀 CA E8 03 28 08 95 CA 02 10。 */
export const K797_PREFIX: number[] = [0xCA, 0xE8, 0x03, 0x28, 0x08, 0x95, 0xCA, 0x02, 0x10];

/** 解析拒绝原因枚举（字符串字面量联合）。 */
export type ParseRejectReason =
  | 'wrong_name'
  | 'wrong_manufacturer_id'
  | 'payload_too_short'
  | 'wrong_prefix'
  | 'weight_out_of_protocol_range';

/** 解析拒绝结果（与 ScaleReading 互斥的联合分支）。 */
export class ParseReject {
  readonly ok: boolean = false;
  readonly reason: ParseRejectReason;
  readonly message: string;
  constructor(reason: ParseRejectReason, message: string) {
    this.reason = reason;
    this.message = message;
  }
}

/** 解析成功结果包装，便于调用方用 result.ok 判定。 */
export class ParseOk {
  readonly ok: boolean = true;
  readonly reading: ScaleReading;
  constructor(reading: ScaleReading) {
    this.reading = reading;
  }
}

export type ParseResult = ParseOk | ParseReject;

/**
 * 纯函数解析入口。
 *
 * @param localName        广播 Local Name，可为 null
 * @param manufacturerId   Manufacturer ID，可为 null
 * @param payload          Manufacturer Data（不含 ID 自身），可为 null
 * @param rssi             RSSI，可为 null
 * @param receivedAtEpochMs 接收 epoch 毫秒（由调用方提供，确保单调与审计一致）
 * @param sequence         进程内单调序号（由来源分配）
 * @param source           读数来源，默认 'ble'
 * @param address          诊断地址，默认 'diagnostic-only'
 * @param stable           是否稳定（由来源的重复广播窗口派生）
 * @param stableSource     稳定来源
 */
export function parse(
  localName: string | null,
  manufacturerId: number | null,
  payload: Uint8Array | null,
  rssi: number | null,
  receivedAtEpochMs: number,
  sequence: number,
  source: ScaleSourceKind = 'ble',
  address: string = 'diagnostic-only',
  stable: boolean = false,
  stableSource: StableSource | null = null,
): ParseResult {
  // 1. Local Name
  if (localName !== K797_DEVICE_NAME) {
    return new ParseReject('wrong_name', `Local Name 不匹配：${localName}`);
  }

  // 2. Manufacturer ID
  if (manufacturerId !== K797_MANUFACTURER_ID) {
    return new ParseReject(
      'wrong_manufacturer_id',
      `Manufacturer ID 不匹配：0x${(manufacturerId ?? -1).toString(16).toUpperCase()}`,
    );
  }

  // 3. 长度
  if (payload === null || payload.length < K797_MIN_PAYLOAD_BYTES) {
    const len = payload === null ? 0 : payload.length;
    return new ParseReject('payload_too_short', `payload 长度不足：${len} < ${K797_MIN_PAYLOAD_BYTES}`);
  }

  // 4. 常量时间比较前 9 字节前缀（按字节 &, 不提前短路，避免时序泄露）
  let prefixMatch = 0;
  for (let i = 0; i < K797_PREFIX.length; i++) {
    // 不匹配则累计差值；循环照常跑完。
    prefixMatch |= (payload[i] ^ K797_PREFIX[i]);
  }
  if (prefixMatch !== 0) {
    return new ParseReject('wrong_prefix', '前 9 字节前缀不匹配');
  }

  // 5. 重量（小端）
  const raw: number = (payload[9] & 0xFF) | ((payload[10] & 0xFF) << 8);
  const grams: number = raw / 10.0;
  if (grams < 0 || grams > K797_MAX_GRAMS || !Number.isFinite(grams)) {
    return new ParseReject('weight_out_of_protocol_range', `重量超协议范围：${grams}`);
  }

  // 6. 组装不可变读数
  const payloadHex = toHexSpaces(payload);
  const deviceKey = buildDeviceKey(K797_MANUFACTURER_ID, K797_PREFIX);
  const reading = new ScaleReading({
    device: K797_DEVICE_NAME,
    deviceKey,
    grams,
    raw,
    rssi,
    receivedAt: toIsoUtc(receivedAtEpochMs),
    receivedAtEpochMs,
    sequence,
    stable,
    stableSource: stable ? stableSource : null,
    source,
    address,
    payloadHex,
  });
  return new ParseOk(reading);
}

/** 把 payload 转成空格分隔大写十六进制，例：CA E8 03 ...。 */
export function toHexSpaces(bytes: Uint8Array): string {
  const parts: string[] = [];
  for (let i = 0; i < bytes.length; i++) {
    parts.push((bytes[i] & 0xFF).toString(16).toUpperCase().padStart(2, '0'));
  }
  return parts.join(' ');
}

/** 把空格分隔大写十六进制文本解析为 Uint8Array（fixture 加载用）。 */
export function fromHexSpaces(hex: string): Uint8Array {
  const tokens = hex.trim().split(/\s+/);
  const out = new Uint8Array(tokens.length);
  for (let i = 0; i < tokens.length; i++) {
    out[i] = parseInt(tokens[i], 16) & 0xFF;
  }
  return out;
}

/** 构造设备主键：k797:<manufacturerIdHex>:<prefixHex>。
 *  hex 必须小写，与 Python 侧 _DEVICE_KEY 及计划 §5.1 契约
 *  （k797:0000:cae803280895ca0210）保持一致；payloadHex 显示另用大写。 */
export function buildDeviceKey(manufacturerId: number, prefix: number[]): string {
  const idHex = manufacturerId.toString(16).toLowerCase().padStart(4, '0');
  const prefixHex = prefix.map(b => b.toString(16).toLowerCase().padStart(2, '0')).join('');
  return `k797:${idHex}:${prefixHex}`;
}

/** 把 epoch 毫秒格式化为 ISO8601 UTC（Z 后缀），不依赖系统 Date 格式化差异。 */
export function toIsoUtc(epochMs: number): string {
  const ms = Math.floor(epochMs);
  const seconds = Math.floor(ms / 1000);
  const millis = ms - seconds * 1000;
  const daysFromEpoch = Math.floor(seconds / 86400);
  const secsOfDay = seconds - daysFromEpoch * 86400;
  const date = civilFromDays(daysFromEpoch);
  const hh = String(Math.floor(secsOfDay / 3600)).padStart(2, '0');
  const mm = String(Math.floor((secsOfDay % 3600) / 60)).padStart(2, '0');
  const ss = String(secsOfDay % 60).padStart(2, '0');
  const sss = String(millis).padStart(3, '0');
  const yyyy = String(date.year).padStart(4, '0');
  const MM = String(date.month).padStart(2, '0');
  const dd = String(date.day).padStart(2, '0');
  return `${yyyy}-${MM}-${dd}T${hh}:${mm}:${ss}.${sss}Z`;
}

/** civilFromDays 的返回结构。提取为 interface，避免对象字面量类型用作返回类型。 */
interface CivilDate {
  year: number;
  month: number;
  day: number;
}

/** Howard Hinnant 的 civil_from_days 算法，无时区依赖。 */
function civilFromDays(z: number): CivilDate {
  const Z = z + 719468;
  const era = Math.floor(Z >= 0 ? Z : Z - 146096) / 146097;
  const doe = Math.floor(Z - era * 146097);
  const yoe = Math.floor((doe - Math.floor(doe / 1460) + Math.floor(doe / 36524) - Math.floor(doe / 146096)) / 365);
  const y = Math.floor(yoe + era * 400);
  const doy = doe - Math.floor((365 * yoe + Math.floor(yoe / 4) - Math.floor(yoe / 100)));
  const mp = Math.floor((5 * doy + 2) / 153);
  const d = doy - Math.floor((153 * mp + 2) / 5) + 1;
  const m = mp < 10 ? mp + 3 : mp - 9;
  const year = m <= 2 ? y + 1 : y;
  const result: CivilDate = { year, month: m, day: d };
  return result;
}
