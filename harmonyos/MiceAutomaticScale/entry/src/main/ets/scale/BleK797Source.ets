/**
 * BleK797Source.ets
 *
 * 真实 BLE 扫描读数源，针对不可连接的 K797 广播秤。
 * - 仅扫描广播，绝不调用 GATT connect，绝不要求配对；
 * - 允许重复广播，以便 base 派生 stable；
 * - 提取 manufacturerDataMap[0x0000] 的 Manufacturer Data 喂给解析器；
 * - BLE 地址仅诊断用（随机私有地址，不作设备主键，parse 的 address 固定 diagnostic-only）。
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
import { parse, ParseReject, K797_DEVICE_NAME, K797_MANUFACTURER_ID } from './K797AdvertisementParser';
import { ScaleSourceBase, type MonotonicClock } from './ScaleSource';
import type { ScaleDiagnostics } from '../diagnostics/ScaleDiagnostics';

/** 扫描结果回调类型。 */
type ScanHandler = (results: Array<ble.ScanResult>) => void;
/** scanner 实例级 ScanReport 回调类型（API>=15）。 */
type ReportHandler = (report: ble.ScanReport) => void;
/** 蓝牙状态变化回调类型。 */
type StateHandler = (state: access.BluetoothState) => void;

/**
 * 真实 BLE 扫描读数源。
 */
export class BleK797Source extends ScaleSourceBase {
  private scanner: ble.BleScanner | null = null;
  private diagnostics: ScaleDiagnostics | null = null;
  private reportHandler: ReportHandler | null = null;
  private stateHandler: StateHandler | null = null;

  constructor(clock: MonotonicClock, diagnostics: ScaleDiagnostics | null = null) {
    super('ble', clock);
    this.diagnostics = diagnostics;
  }

  setDiagnostics(diagnostics: ScaleDiagnostics): void {
    this.diagnostics = diagnostics;
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
    try {
      this.scanner.startScan([filter]).catch((e: Object) => {
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
  }

  /** 处理单条扫描结果：强类型读取字段，喂解析器、计 diagnostics、派发读数。 */
  private processScanResult(r: ble.ScanResult): void {
    const localName: string = r.deviceName;
    const rssi: number = r.rssi;

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

    if (result.ok) {
      this.handleParseResult(result, receivedEpochMs, receivedMonoMs);
    }
  }
}
