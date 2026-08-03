/**
 * BleK797Source.ets
 *
 * 真实 BLE 扫描读数源，针对不可连接的 K797 广播秤。
 * - 仅扫描广播，绝不调用 GATT connect，绝不要求配对；
 * - 允许重复广播，以便 base 派生 stable；
 * - 提取 manufacturerDataMap[0x0000] 的 Manufacturer Data 喂给解析器；
 * - BLE 地址仅诊断用（随机私有地址，不作设备主键，parse 的 address 固定 diagnostic-only）。
 *
 * 设备发现与选择（阶段 4）：
 * - 维护发现表 Map<deviceId, DiscoveredDevice>；processScanResult 无论是否已选定
 *   设备都先解析并更新表（grams 解析失败则保留旧值或 null），超过 15s 未见的设备剔除。
 * - 未选定设备 = 纯发现模式：不派发读数（不调 handleParseResult），状态停留 scanning，
 *   message 随发现数更新（如"发现 2 台天平，等待选择"）。
 * - selectScaleDevice(id) 后：只有 r.deviceId === id 的包才走 handleParseResult，
 *   其余仅更新发现表；首个匹配读数到达 → 状态机自然转 active。
 * - clearScaleDevice()：清除选择，transition 回 scanning（force=true）。
 * - onStop()：清空发现表与选择。
 * - 旧版 H5 兼容兜底：扫描开始 4 秒后若页面从未调用 devices API 且发现表非空，
 *   自动选定 rssi 最强的设备（hilog info 备注 auto-select legacy fallback）；
 *   一旦调用过任何 devices API，永久取消本次扫描的兜底。
 *
 * 阶段 3 真机核实（API 24，@ohos.bluetooth.ble.d.ts / @ohos.bluetooth.access.d.ts）：
 *   - ble.createBleScanner(): BleScanner；
 *   - scanner.startScan(filters: Array<ScanFilter>, options?): Promise<void>，
 *     filters 必传，按 name + manufactureId 过滤 K797（拼写 manufacture 为官方字段名）；
 *   - 结果投递：用 scanner 实例的 on('BLEDeviceFind', Callback<ScanReport>)（@since 15），
 *     而非全局 ble.on(...)——真机实测全局 ble.on 在 BleScanner.startScan 路径下不触发，
 *     必须注册到 scanner 实例；ScanReport.scanResult 为 Array<ScanResult>；
 *   - ScanResult 字段：deviceName / rssi / deviceId / manufacturerDataMap(Map<number,Uint8Array>)；
 *   - 蓝牙开关用 access.getState() + access.on('stateChange')，枚举 access.BluetoothState。
 *
 * 已端到端验证（ESP32-C6 模拟器 ↔ MatePad）：C6 广播 K797 + 250.0g(raw2500)，
 * MatePad scanner.on 收到、manufacturerDataMap[0x0000] 为 18B（不含公司 ID）、
 * 前缀 CA E8 03 28 08 95 CA 02 10 匹配、payload[9..10]=C4 09 解析为 250.0g，ok=true。
 */

import { ble, access } from '@kit.ConnectivityKit';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { parse, ParseResult, ParseReject, ParseOk, K797_DEVICE_NAME, K797_MANUFACTURER_ID } from './K797AdvertisementParser';
import { ScaleSourceBase, type MonotonicClock } from './ScaleSource';
import type { ScaleDiagnostics } from '../diagnostics/ScaleDiagnostics';

/** 扫描结果回调类型。 */
type ScanHandler = (results: Array<ble.ScanResult>) => void;
/** scanner 实例级 ScanReport 回调类型（API>=15）。 */
type ReportHandler = (report: ble.ScanReport) => void;
/** 蓝牙状态变化回调类型。 */
type StateHandler = (state: access.BluetoothState) => void;
/** 发现表变化回调类型。 */
type DevicesChangedCallback = () => void;

/** 发现表中单台设备的不可变记录。 */
export interface DiscoveredDevice {
  deviceId: string;
  name: string;
  rssi: number;
  /** 最新解析的克数；无法解析时为 null。 */
  grams: number | null;
  lastSeenAtEpochMs: number;
}

/** 设备发现表快照的 JSON 形状参数（避免对象字面量类型作参数）。 */
export interface DevicesPayloadParams {
  devices: DiscoveredDevice[];
  scanning: boolean;
  selectedDeviceId: string | null;
}

/** 发现设备过期阈值：超过此时间未再见的设备从表中剔除（毫秒）。 */
const DEVICE_EXPIRE_MS: number = 15_000;
/** 旧版兜底延迟：扫描开始后多久若页面未用 devices API 则自动选 rssi 最强设备。 */
const LEGACY_FALLBACK_DELAY_MS: number = 4_000;
/** selectedDeviceId 合法字符集校验：[0-9A-Za-z:\-_.]，长度 ≤64。 */
const DEVICE_ID_PATTERN: RegExp = /^[0-9A-Za-z:\-_.]{1,64}$/;
/** hilog domain（0x0000~0xFFFF，自定义）。 */
const HILOG_DOMAIN: number = 0x0510;
const HILOG_TAG: string = 'MiceAutomaticScale';

/**
 * 真实 BLE 扫描读数源。
 */
export class BleK797Source extends ScaleSourceBase {
  private scanner: ble.BleScanner | null = null;
  private diagnostics: ScaleDiagnostics | null = null;
  private reportHandler: ReportHandler | null = null;
  private stateHandler: StateHandler | null = null;

  /** 发现表：deviceId → DiscoveredDevice。 */
  private readonly devices: Map<string, DiscoveredDevice> = new Map();
  /** 发现表变化回调（Index.ets 订阅以推送 EVENT_DEVICES）。 */
  private devicesChangedCb: DevicesChangedCallback | null = null;
  /** 旧版兜底定时器句柄。 */
  private legacyFallbackTimerId: number = -1;
  /** 本次 start 以来页面是否调用过任何 devices API。 */
  private devicesApiTouched: boolean = false;
  /** 本次 start 以来是否已触发过旧版兜底（避免重复）。 */
  private legacyFallbackDone: boolean = false;
  /** 发现表过期清理定时器句柄。 */
  private expireTimerId: number = -1;

  constructor(clock: MonotonicClock, diagnostics: ScaleDiagnostics | null = null) {
    super('ble', clock);
    this.diagnostics = diagnostics;
  }

  setDiagnostics(diagnostics: ScaleDiagnostics): void {
    this.diagnostics = diagnostics;
  }

  /** 订阅发现表变化（由 Index.ets 注册以推送 EVENT_DEVICES）。 */
  onDevicesChanged(cb: DevicesChangedCallback): void {
    this.devicesChangedCb = cb;
  }

  /**
   * 返回当前发现表 + scanning + selectedDeviceId 的 JSON（与
   * miceautomatic:scale-devices 事件 detail 形状一致）。
   */
  getDevicesJson(): string {
    return this.buildDevicesJson();
  }

  /**
   * 选定设备。校验：字符串、长度 ≤64、仅 [0-9A-Za-z:\-_.]，非法直接忽略。
   * 选择时若表中没有该设备也接受（等设备出现）。一旦调用，永久取消本次扫描的旧版兜底。
   */
  selectScaleDevice(deviceId: string): void {
    // 任何 devices API 调用都标记本次扫描"页面已用新 API"，永久取消兜底。
    this.markDevicesApiTouched();
    if (typeof deviceId !== 'string' || !DEVICE_ID_PATTERN.test(deviceId)) {
      // 非法 deviceId 直接忽略（不抛异常，不改变状态）。
      hilog.info(HILOG_DOMAIN, HILOG_TAG, 'selectScaleDevice ignored invalid deviceId');
      return;
    }
    this.setSelectedDeviceId(deviceId);
    // 选择变化立即推一次发现表，让 selectedDeviceId 尽快同步到页面。
    this.notifyDevicesChanged();
    // 选定后若该设备已在表中，message 用其 name；否则保持 scanning 等待出现。
    const dev = this.devices.get(deviceId);
    if (dev !== undefined) {
      this.transition('scanning', `已选择 ${dev.name}，等待读数`, true);
    } else {
      this.transition('scanning', '已选择设备，等待出现', true);
    }
    hilog.info(HILOG_DOMAIN, HILOG_TAG, 'selectScaleDevice: %s', deviceId);
  }

  /** 取消选择，回到纯发现模式。 */
  clearScaleDevice(): void {
    this.markDevicesApiTouched();
    this.setSelectedDeviceId(null);
    this.notifyDevicesChanged();
    this.transition('scanning', '已取消选择，继续搜索', true);
    hilog.info(HILOG_DOMAIN, HILOG_TAG, 'clearScaleDevice');
  }

  protected onStart(): void {
    // 蓝牙开关检查：未开则直接进 bluetooth_off，不创建扫描器。
    try {
      const st = access.getState();
      if (st !== access.BluetoothState.STATE_ON && st !== access.BluetoothState.STATE_BLE_ON) {
        this.reportBluetoothOff('系统蓝牙未开启');
        return;
      }
    } catch (e) {
      // 查询状态失败不阻断，继续尝试扫描（扫描失败会再报错）。
    }

    // 监听运行中蓝牙被关闭，转 bluetooth_off。
    this.stateHandler = (state: access.BluetoothState): void => {
      if (state === access.BluetoothState.STATE_OFF || state === access.BluetoothState.STATE_BLE_TURNING_OFF) {
        this.reportBluetoothOff('系统蓝牙已关闭');
      }
    };
    try {
      access.on('stateChange', this.stateHandler);
    } catch (e) {
      // 状态监听失败不阻断扫描。
    }

    try {
      this.scanner = ble.createBleScanner();
    } catch (e) {
      this.reportError(`无法创建 BLE 扫描器：${e}`);
      return;
    }

    // API>=15：BleScanner.startScan 的结果只投递到 scanner 实例的
    // on('BLEDeviceFind', Callback<ScanReport>)；全局 ble.on 在此路径不触发。
    this.reportHandler = (report: ble.ScanReport): void => {
      const arr: Array<ble.ScanResult> = report.scanResult;
      for (let i = 0; i < arr.length; i++) {
        this.processScanResult(arr[i]);
      }
    };
    try {
      this.scanner.on('BLEDeviceFind', this.reportHandler);
    } catch (e) {
      this.reportError(`注册扫描监听失败：${e}`);
      return;
    }

    // 带过滤扫描：name=K797 + manufactureId=0x0000。onScanResults 内仍二次校验名称。
    const filter: ble.ScanFilter = { name: K797_DEVICE_NAME, manufactureId: K797_MANUFACTURER_ID };
    // 扫描占空比：K797 是不可连接广播设备，无 GATT/notify 通道，必须持续被动监听。
    // LOW_LATENCY 用最高占空比，对应毫秒级广播时延迟最低；现场插电连续称量，
    // 实时性优先于功耗。stale 阈值见 STALE_THRESHOLD_MS（15s，宽容系统批处理抖动）。
    const options: ble.ScanOptions = { dutyMode: ble.ScanDuty.SCAN_MODE_LOW_LATENCY };
    try {
      this.scanner.startScan([filter], options).then(() => {
        // 扫描真正启动后安排旧版兜底（若页面 4s 内未用 devices API）。
        this.scheduleLegacyFallback();
        // 周期清理过期设备（每 3s 一次，配合 15s 过期阈值）。
        this.scheduleExpireSweep();
      }).catch((e: Object) => {
        const msg = `${e}`;
        if (msg.indexOf('201') >= 0 || msg.indexOf('ermission') >= 0) {
          this.reportUnauthorized(`蓝牙权限被拒：${msg}`);
        } else {
          this.reportError(`启动 BLE 扫描失败：${msg}`);
        }
      });
    } catch (e) {
      const msg = `${e}`;
      if (msg.indexOf('201') >= 0 || msg.indexOf('ermission') >= 0) {
        this.reportUnauthorized(`蓝牙权限被拒：${msg}`);
      } else if (msg.indexOf('luetooth') >= 0 && msg.indexOf('ff') >= 0) {
        this.reportBluetoothOff('系统蓝牙未开启');
      } else {
        this.reportError(`启动 BLE 扫描失败：${msg}`);
      }
    }
  }

  protected onStop(): void {
    if (this.legacyFallbackTimerId !== -1 && typeof clearTimeout !== 'undefined') {
      clearTimeout(this.legacyFallbackTimerId);
      this.legacyFallbackTimerId = -1;
    }
    if (this.expireTimerId !== -1 && typeof clearTimeout !== 'undefined') {
      clearTimeout(this.expireTimerId);
      this.expireTimerId = -1;
    }
    if (this.reportHandler !== null && this.scanner !== null) {
      try {
        this.scanner.off('BLEDeviceFind', this.reportHandler);
      } catch (e) {
        // 忽略解除监听失败。
      }
      this.reportHandler = null;
    }
    if (this.stateHandler !== null) {
      try {
        access.off('stateChange', this.stateHandler);
      } catch (e) {
        // 忽略。
      }
      this.stateHandler = null;
    }
    if (this.scanner !== null) {
      try {
        this.scanner.stopScan();
      } catch (e) {
        // 权限可能在后台被撤销，忽略停止失败。
      }
      this.scanner = null;
    }
    // 清空发现表与选择状态（selectedDeviceId 由 stop() 统一清）。
    this.devices.clear();
    this.devicesApiTouched = false;
    this.legacyFallbackDone = false;
  }

  /** 处理单条扫描结果：强类型读取字段，喂解析器、计 diagnostics、派发读数。
   *  无论是否已选定设备，都先解析并更新发现表；仅当选定设备匹配时才派发读数。 */
  private processScanResult(r: ble.ScanResult): void {
    const localName: string = r.deviceName;
    const rssi: number = r.rssi;
    const deviceId: string = r.deviceId;

    // 提取 K797 的 Manufacturer Data。
    let payload: Uint8Array | null = null;
    let manufacturerId: number | null = null;
    const mdmap = r.manufacturerDataMap;
    if (mdmap !== undefined && mdmap !== null) {
      const data = mdmap.get(K797_MANUFACTURER_ID);
      if (data !== undefined) {
        payload = data;
        manufacturerId = K797_MANUFACTURER_ID;
      } else if (mdmap.size === 1) {
        // 兜底：单条目且 ID 编码差异时接受唯一载荷。
        for (const entry of mdmap) {
          payload = entry[1];
          manufacturerId = entry[0];
          break;
        }
      }
    }

    const receivedEpochMs = Date.now();
    const receivedMonoMs = this.clock.nowMs();

    const result = parse(
      localName,
      manufacturerId,
      payload,
      rssi,
      receivedEpochMs,
      0, // sequence 由 base 统一分配
      'ble',
      'diagnostic-only', // 不暴露真实 BLE 地址
    );

    if (this.diagnostics !== null) {
      this.diagnostics.recordAdvertisement();
      if (payload !== null) {
        this.diagnostics.recordPayload(payload);
      }
      if (result.ok) {
        this.diagnostics.recordAccepted();
      } else {
        this.diagnostics.recordReject((result as ParseReject).reason);
      }
    }

    // 始终更新发现表（无论是否选定设备，无论解析是否成功）。
    const grams = this.extractGrams(result);
    const tableChanged = this.updateDevice(deviceId, localName, rssi, grams, receivedEpochMs);

    // 选定设备过滤：仅匹配的包才走 handleParseResult（派发读数 + 转 active）。
    const selected = this.currentSelectedDeviceId;
    if (selected !== null && selected === deviceId && result.ok) {
      const okResult = result as ParseOk;
      this.handleParseResult(okResult, receivedEpochMs, receivedMonoMs);
    } else if (selected === null) {
      // 纯发现模式：不派发读数，状态停留 scanning，message 随发现数更新。
      this.maintainDiscoveryMessage(tableChanged);
    }
    // selected !== null && selected !== deviceId：仅更新发现表，不派发读数。

    // 发现表有实质变化时通知订阅者（Index 推 EVENT_DEVICES）。
    if (tableChanged) {
      this.notifyDevicesChanged();
    }
  }

  /** 从解析结果提取 grams；解析失败返回 null。 */
  private extractGrams(result: ParseResult): number | null {
    if (result.ok) {
      return (result as ParseOk).reading.grams;
    }
    return null;
  }

  /** 更新发现表中某设备；返回 true 表示有实质变化（新增/grams/rssi≥5dB/首次出现）。 */
  private updateDevice(
    deviceId: string,
    name: string,
    rssi: number,
    grams: number | null,
    nowEpochMs: number,
  ): boolean {
    const prev = this.devices.get(deviceId);
    if (prev === undefined) {
      const dev: DiscoveredDevice = {
        deviceId,
        name,
        rssi,
        grams,
        lastSeenAtEpochMs: nowEpochMs,
      };
      this.devices.set(deviceId, dev);
      return true;
    }
    // 计算是否实质变化：grams 变化 / rssi 变化 ≥5dB / grams 由 null 变非 null。
    const rssiChanged = Math.abs(prev.rssi - rssi) >= 5;
    const gramsChanged = !sameGrams(prev.grams, grams);
    const updated: DiscoveredDevice = {
      deviceId,
      name: name.length > 0 ? name : prev.name,
      rssi,
      // grams 解析失败则保留旧值（契约要求"保留旧值或 null"）。
      grams: grams !== null ? grams : prev.grams,
      lastSeenAtEpochMs: nowEpochMs,
    };
    this.devices.set(deviceId, updated);
    return rssiChanged || gramsChanged;
  }

  /** 纯发现模式下根据发现数量刷新 message（仅当表变化时，避免无谓 transition）。 */
  private maintainDiscoveryMessage(tableChanged: boolean): void {
    if (!tableChanged) {
      return;
    }
    const n = this.devices.size;
    if (n === 0) {
      return; // 等下一次更新
    }
    this.transition('scanning', `发现 ${n} 台天平，等待选择`, true);
  }

  /** 通知发现表变化（Index.ets 订阅后节流推 EVENT_DEVICES）。 */
  private notifyDevicesChanged(): void {
    if (this.devicesChangedCb !== null) {
      this.devicesChangedCb();
    }
  }

  /** 标记页面已用 devices API，永久取消本次扫描的旧版兜底。 */
  private markDevicesApiTouched(): void {
    this.devicesApiTouched = true;
  }

  /** 安排旧版兜底：扫描开始 4s 后若未用 devices API 且发现表非空，自动选 rssi 最强。 */
  private scheduleLegacyFallback(): void {
    if (this.legacyFallbackTimerId !== -1) {
      return;
    }
    if (typeof setTimeout === 'undefined') {
      return;
    }
    this.legacyFallbackTimerId = setTimeout(() => {
      this.legacyFallbackTimerId = -1;
      this.runLegacyFallback();
    }, LEGACY_FALLBACK_DELAY_MS) as number;
  }

  private runLegacyFallback(): void {
    // 已选定 / 已用新 API / 已做过兜底 / 发现表空 → 跳过。
    if (this.devicesApiTouched || this.legacyFallbackDone) {
      return;
    }
    if (this.currentSelectedDeviceId !== null) {
      return;
    }
    if (this.devices.size === 0) {
      // 4s 内没发现任何设备，重排一次再等（直到页面用 API 或 stop）。
      this.scheduleLegacyFallback();
      return;
    }
    // 选 rssi 最强（数值最大）的设备。
    let best: DiscoveredDevice | null = null;
    this.devices.forEach((d: DiscoveredDevice) => {
      if (best === null || d.rssi > (best as DiscoveredDevice).rssi) {
        best = d;
      }
    });
    if (best === null) {
      return;
    }
    const target = best as DiscoveredDevice;
    this.legacyFallbackDone = true;
    hilog.info(
      HILOG_DOMAIN,
      HILOG_TAG,
      'auto-select legacy fallback: deviceId=%s rssi=%d',
      target.deviceId,
      target.rssi,
    );
    // 直接选定（不走 selectScaleDevice 的 markDevicesApiTouched，否则自相矛盾）。
    this.setSelectedDeviceId(target.deviceId);
    this.notifyDevicesChanged();
    this.transition('scanning', `已选择 ${target.name}，等待读数`, true);
  }

  /** 周期清理过期设备（>15s 未见），并通知变化。 */
  private scheduleExpireSweep(): void {
    if (this.expireTimerId !== -1) {
      return;
    }
    if (typeof setTimeout === 'undefined') {
      return;
    }
    this.expireTimerId = setTimeout(() => {
      this.expireTimerId = -1;
      this.expireSweep();
    }, 3000) as number;
  }

  private expireSweep(): void {
    const now = Date.now();
    let changed = false;
    const toRemove: string[] = [];
    this.devices.forEach((d: DiscoveredDevice) => {
      if (now - d.lastSeenAtEpochMs > DEVICE_EXPIRE_MS) {
        toRemove.push(d.deviceId);
      }
    });
    for (let i = 0; i < toRemove.length; i++) {
      const id = toRemove[i];
      // 选定的设备即使过期也不从表中移除（避免选择闪烁）；但可标记。
      if (id === this.currentSelectedDeviceId) {
        continue;
      }
      this.devices.delete(id);
      changed = true;
    }
    if (changed) {
      this.notifyDevicesChanged();
    }
    // 继续下一轮清理（只要还在扫描）。
    if (this.state === 'scanning' || this.state === 'active' || this.state === 'stale') {
      this.scheduleExpireSweep();
    }
  }

  /** 构造发现表 JSON（手写拼串，与 ScaleStatus.toJson 风格一致）。 */
  private buildDevicesJson(): string {
    const scanning = this.state === 'scanning' || this.state === 'active' || this.state === 'stale';
    const selected = this.currentSelectedDeviceId;
    const parts: string[] = [];
    this.devices.forEach((d: DiscoveredDevice) => {
      const g = d.grams === null ? 'null' : formatGrams(d.grams);
      parts.push('{'
        + `"deviceId":"${escapeJsonStr(d.deviceId)}"`
        + `,"name":"${escapeJsonStr(d.name)}"`
        + `,"rssi":${d.rssi}`
        + `,"grams":${g}`
        + `,"lastSeenAtEpochMs":${d.lastSeenAtEpochMs}`
        + '}');
    });
    const sel = selected === null ? 'null' : `"${escapeJsonStr(selected)}"`;
    return '{'
      + `"devices":[${parts.join(',')}]`
      + `,"scanning":${scanning}`
      + `,"selectedDeviceId":${sel}`
      + '}';
  }
}

/** 判断两个 grams 是否"相等"（小数点后一位内视为相同，避免浮点抖动频繁触发推送）。 */
function sameGrams(a: number | null, b: number | null): boolean {
  if (a === null && b === null) {
    return true;
  }
  if (a === null || b === null) {
    return false;
  }
  return Math.abs(a - b) < 0.05;
}

/** 把克数格式化为协议 JSON：整数带一位小数（如 250.0），非整数最多一位。 */
function formatGrams(value: number): string {
  if (Number.isInteger(value)) {
    return value.toFixed(1);
  }
  return (Math.round(value * 10) / 10).toString();
}

/** 极简 JSON 字符串转义（与 ScaleStatus.ets 的 escapeJson 保持一致风格）。 */
function escapeJsonStr(value: string): string {
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
