/**
 * BleK797Source.ets
 *
 * 真实 BLE 扫描读数源，针对不可连接的 K797 广播秤。
 * - 仅扫描广播，绝不调用 GATT connect，绝不要求配对；
 * - 允许重复广播（report all broadcasts），以便 base 派生 stable；
 * - 提取 Manufacturer ID 0x0000 的 Manufacturer Data 喂给解析器；
 * - BLE 地址仅诊断用（随机私有地址，不作设备主键）。
 *
 * =====================================================================
 * TODO(Phase-0) 设备 API 核实清单（未装 DevEco Studio，无法编译验证）：
 *   1. 扫描 API：本文件使用 @kit.ConnectivityKit 的 `ble`：
 *        import { ble } from '@kit.ConnectivityKit';
 *        const scanner = ble.createBleScanner();
 *        scanner.startScan(filterOrOptions);
 *        scanner.stopScan();
 *      真机必须以 DevEco Studio 内安装的 SDK API 参考为准：
 *        - createBleScanner / startScan / stopScan / on('BLEDeviceFind')
 *          的精确方法名、参数结构与回调签名均需核对；
 *        - 避免复制已废弃的 @ohos.bluetooth.ble 旧示例；
 *        - 扫描过滤：优先用 name="K797" 的 BleScanFilter；若该版本
 *          filter 不稳定，改为无过滤扫描后在代码内匹配（两者皆注释）。
 *   2. 蓝牙开关与权限：accessibilityManager / bluetooth state：
 *        - connection 蓝牙状态监听 API 名（如 ble.on('stateChange')）需核实；
 *        - 权限常量 ohos.permission.ACCESS_BLUETOOTH（module.json5）需核实
 *          是否还需 ohos.permission.LOCATION 才能扫描。
 *   3. 后台扫描限制：HarmonyOS 后台 BLE 扫描可能被冻结，需 ability 前台
 *      保持，并在 onForeground/onBackground 显式 stop/start。
 * =====================================================================
 */

import { ble } from '@kit.ConnectivityKit';
import { parse, K797_DEVICE_NAME, K797_MANUFACTURER_ID } from './K797AdvertisementParser';
import { ScaleSourceBase, type MonotonicClock } from './ScaleSource';
import type { ScaleDiagnostics } from '../diagnostics/ScaleDiagnostics';

/** K797 扫描结果回调中提取出的结构化字段（屏蔽 BLE SDK 类型差异）。 */
interface ExtractedAdvertisement {
  localName: string | null;
  manufacturerId: number | null;
  payload: Uint8Array | null;
  rssi: number | null;
  address: string;
}

/**
 * 真实 BLE 扫描读数源。
 *
 * 由于无法在本机编译，SDK 类型差异用一个 adapter 函数 `extract` 把
 * BLE 回调结果转成 ExtractedAdvertisement；真机核实后调整 extract 即可，
 * 其余解析/状态机逻辑保持不变。
 */
export class BleK797Source extends ScaleSourceBase {
  private scanner: ble.BLEScanner | null = null;
  private diagnostics: ScaleDiagnostics | null = null;
  private scanListenerAttached: boolean = false;

  constructor(clock: MonotonicClock, diagnostics: ScaleDiagnostics | null = null) {
    super('ble', clock);
    this.diagnostics = diagnostics;
  }

  setDiagnostics(diagnostics: ScaleDiagnostics): void {
    this.diagnostics = diagnostics;
  }

  protected onStart(): void {
    try {
      // TODO(Phase-0): 核实 createBleScanner 的精确 API 名与可用性。
      //   若该 API 在目标版本不存在，参考 SDK 提供的等价扫描入口。
      this.scanner = ble.createBleScanner();
    } catch (e) {
      this.reportError(`无法创建 BLE 扫描器：${e}`);
      return;
    }

    // 注册扫描结果监听。
    // TODO(Phase-0): 核实 on('BLEDeviceFind') 事件名与回调签名
    //   （结果可能是数组 BLEDevice[] 或单个对象）。
    try {
      const handler = (results: Object) => this.onDeviceFind(results);
      // @ts-ignore：SDK 事件签名以真机为准。
      ble.on('BLEDeviceFind', handler);
      this.scanListenerAttached = true;
    } catch (e) {
      this.reportError(`注册扫描监听失败：${e}`);
      return;
    }

    try {
      // 方式 A（首选）：带过滤扫描，名称="K797"。
      // TODO(Phase-0): 核实 BleScanFilter 字段名（name / namePrefix /
      //   serviceUuid / manufacturerData 等），以及 startScan 是否接受
      //   单个 filter 对象还是数组。当前按“对象”假设。
      // const filter: ble.ScanFilter = { name: K797_DEVICE_NAME };
      // this.scanner.startScan([filter]);

      // 方式 B（兜底）：无过滤扫描，代码内匹配 K797。
      //   若 filter API 在目标版本不稳定，启用 B 并注释 A。
      //   保留两种写法以便真机二选一。
      // @ts-ignore：startScan 签名以真机为准。
      this.scanner.startScan();
    } catch (e) {
      const msg = `${e}`;
      if (msg.indexOf('permission') >= 0 || msg.indexOf('Permission') >= 0) {
        this.reportUnauthorized(`蓝牙权限被拒：${e}`);
      } else if (msg.indexOf('bluetooth') >= 0 && msg.indexOf('off') >= 0) {
        this.reportBluetoothOff('系统蓝牙未开启');
      } else {
        this.reportError(`启动 BLE 扫描失败：${e}`);
      }
    }
  }

  protected onStop(): void {
    if (this.scanListenerAttached) {
      try {
        // TODO(Phase-0): 核实 off('BLEDeviceFind') 用法。
        // @ts-ignore
        ble.off('BLEDeviceFind');
      } catch (e) {
        // 忽略解除监听失败。
      }
      this.scanListenerAttached = false;
    }
    if (this.scanner !== null) {
      try {
        // @ts-ignore
        this.scanner.stopScan();
      } catch (e) {
        // 权限可能在后台被撤销，忽略停止失败。
      }
      this.scanner = null;
    }
  }

  /** 扫描结果统一入口（屏蔽 SDK 单对象/数组差异）。 */
  private onDeviceFind(results: Object): void {
    const list = toArray(results);
    for (let i = 0; i < list.length; i++) {
      const extracted = this.extract(list[i]);
      if (extracted === null) {
        continue;
      }
      this.processAdvertisement(extracted);
    }
  }

  /** 处理单条广告：喂解析器、计 diagnostics、派发读数。 */
  private processAdvertisement(ad: ExtractedAdvertisement): void {
    const receivedEpochMs = Date.now();
    const receivedMonoMs = this.clock.nowMs();

    const result = parse(
      ad.localName,
      ad.manufacturerId,
      ad.payload,
      ad.rssi,
      receivedEpochMs,
      0, // sequence 由 base 统一分配
      'ble',
      'diagnostic-only', // 不暴露真实 BLE 地址
    );

    if (this.diagnostics !== null) {
      this.diagnostics.recordAdvertisement();
      if (ad.payload !== null) {
        this.diagnostics.recordPayload(ad.payload);
      }
      if (result.ok) {
        this.diagnostics.recordAccepted();
      } else {
        this.diagnostics.recordReject(result.reason);
      }
    }

    if (result.ok) {
      this.handleParseResult(result, receivedEpochMs, receivedMonoMs);
    }
  }

  /**
   * 把 BLE SDK 的扫描结果对象转成结构化字段。
   * TODO(Phase-0): 真机核实字段名：
   *   - deviceName / localName
   *   - manufacturerData 字段是否为 Map<number, Uint8Array> 或对象
   *   - rssi 字段
   *   - device.address 或 deviceId
   * 当前按常见结构假设，真机确认后调整。
   */
  private extract(result: Object): ExtractedAdvertisement | null {
    if (result === null || typeof result !== 'object') {
      return null;
    }
    const r = result as Record<string, Object>;
    const localName = asString(r['deviceName']) ?? asString(r['localName']);
    const rssi = asNumber(r['rssi']);
    const address = asString(r['deviceId']) ?? asString(r['address']) ?? 'diagnostic-only';

    // Manufacturer Data：尝试多种可能字段名。
    let payload: Uint8Array | null = null;
    let manufacturerId: number | null = null;
    const md = r['manufacturerData'] ?? r['manufacturerSpecificData'];
    if (md !== null && md !== undefined) {
      const extracted = extractManufacturer(md, K797_MANUFACTURER_ID);
      if (extracted !== null) {
        payload = extracted.payload;
        manufacturerId = extracted.id;
      }
    }

    return {
      localName,
      manufacturerId,
      payload,
      rssi,
      address,
    };
  }
}

/** 把可能是单对象或数组的输入统一成数组。 */
function toArray(results: Object): Object[] {
  if (Array.isArray(results)) {
    return results as Object[];
  }
  return results === null || results === undefined ? [] : [results];
}

function asString(v: Object | undefined): string | null {
  return typeof v === 'string' ? v : null;
}
function asNumber(v: Object | undefined): number | null {
  return typeof v === 'number' ? v : null;
}

/**
 * 从 Manufacturer Data 容器中提取指定 ID 的 payload。
 * TODO(Phase-0): 真机核实容器结构（Map / Record / ArrayBuffer 包装）。
 */
function extractManufacturer(
  md: Object,
  targetId: number,
): { id: number; payload: Uint8Array } | null {
  // 情况 1：Map<number, Uint8Array>
  const maybeMap = md as Map<number, Uint8Array>;
  if (maybeMap instanceof Map) {
    const p = maybeMap.get(targetId);
    if (p !== undefined && p !== null) {
      return { id: targetId, payload: p };
    }
    // 取不到精确 ID 时，若只有一个条目也接受（不同 SDK 编码差异）。
    if (maybeMap.size === 1) {
      const first = maybeMap.entries().next();
      if (!first.done) {
        const entry = first.value as [number, Uint8Array];
        return { id: entry[0], payload: entry[1] };
      }
    }
    return null;
  }
  // 情况 2：Record<string/number, ...>
  const rec = md as Record<string, Object>;
  const key = String(targetId);
  const val = rec[key] ?? rec['0'] ?? rec['0x0000'];
  if (val instanceof Uint8Array) {
    return { id: targetId, payload: val };
  }
  return null;
}
