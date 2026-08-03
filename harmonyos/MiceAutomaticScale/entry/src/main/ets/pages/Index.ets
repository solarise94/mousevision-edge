/**
 * Index.ets
 *
 * 称量主页：
 * - Web 组件加载固定页面 TRUSTED_URL（https://weight.pingoodmice.top:16206/mobile）；
 * - 注入 javaScriptProxy（window.MiceAutomaticScale），方法名与契约一致；
 * - 实现 BridgeHost：把网页的 start/stop/查询/切换/加载场景命令转发给当前 ScaleSource；
 * - 把 ScaleSource 的读数与状态经 ScaleWebBridge 推回网页（节流 ~10 Hz）；
 * - 导航白名单：仅放行 weight.pingoodmice.top:16206。
 *
 * 阶段 2：软件模拟源可演练。
 * - selectSource 在 script / replay 间切换（ble 源在阶段 3 接入）；
 * - loadScenario 从 rawfile/scenarios/*.json 加载场景并归一化事件
 *   （JSON 的 silence/durationMs → 内部 type:'gap'；无 revoke 字段时以超长
 *   silence 近似，receiver 端走 stale 路径）；
 * - debug 面板提供源切换 / 场景加载 / 启停按钮，便于无网页时肉眼演练全部场景。
 *
 * 阶段 3：BleK797Source 接入后，selectSource('ble') 实例化真实 BLE 源。
 */

import { webview } from '@kit.ArkWeb';
import { util } from '@kit.ArkTS';
import { ScaleWebBridge, type BridgeHost, type SourceSelector, TRUSTED_URL, DEBUG_ENABLED } from '../bridge/ScaleWebBridge';
import { ScaleDiagnostics } from '../diagnostics/ScaleDiagnostics';
import { ScaleSource, WallClock, type MonotonicClock } from '../scale/ScaleSource';
import { ScriptScaleSource, type Scenario, type ScenarioEvent, type GramsEvent, type GapEvent, type RevokeEvent } from '../scale/ScriptScaleSource';
import { ReplayScaleSource } from '../scale/ReplayScaleSource';
import { BleK797Source } from '../scale/BleK797Source';
import type { ScaleStatus } from '../scale/ScaleStatus';
import type { ScaleReading } from '../scale/ScaleReading';

/** 内置可演练场景名（与 rawfile/scenarios/*.json 一一对应）。 */
const SCENARIO_NAMES: string[] = [
  'mouse_normal_26_3g',
  'mouse_fast_jump',
  'mouse_unstable_leave',
  'mouse_reweigh_same',
  'mouse_batch_5',
  'broadcast_gap_12s',
  'repeated_out_of_order',
  'permission_revoked',
  'ws_reconnect',
  'soak_cycle_60s',
];

/**
 * BridgeHost 的 ArkTS 合规实现（含方法的对象字面量必须对应 class）。
 */
class IndexBridgeHost implements BridgeHost {
  private readonly startScaleScanCb: () => void;
  private readonly stopScaleScanCb: () => void;
  private readonly getScaleStatusJsonCb: () => string;
  private readonly getScaleDevicesJsonCb: () => string;
  private readonly selectScaleDeviceCb: (deviceId: string) => void;
  private readonly clearScaleDeviceCb: () => void;
  private readonly selectScaleSourceCb: (name: SourceSelector) => void;
  private readonly loadScaleScenarioCb: (name: string) => void;

  constructor(
    startScaleScanCb: () => void,
    stopScaleScanCb: () => void,
    getScaleStatusJsonCb: () => string,
    getScaleDevicesJsonCb: () => string,
    selectScaleDeviceCb: (deviceId: string) => void,
    clearScaleDeviceCb: () => void,
    selectScaleSourceCb: (name: SourceSelector) => void,
    loadScaleScenarioCb: (name: string) => void,
  ) {
    this.startScaleScanCb = startScaleScanCb;
    this.stopScaleScanCb = stopScaleScanCb;
    this.getScaleStatusJsonCb = getScaleStatusJsonCb;
    this.getScaleDevicesJsonCb = getScaleDevicesJsonCb;
    this.selectScaleDeviceCb = selectScaleDeviceCb;
    this.clearScaleDeviceCb = clearScaleDeviceCb;
    this.selectScaleSourceCb = selectScaleSourceCb;
    this.loadScaleScenarioCb = loadScaleScenarioCb;
  }

  startScaleScan(): void { this.startScaleScanCb(); }
  stopScaleScan(): void { this.stopScaleScanCb(); }
  getScaleStatusJson(): string { return this.getScaleStatusJsonCb(); }
  getScaleDevicesJson(): string { return this.getScaleDevicesJsonCb(); }
  selectScaleDevice(deviceId: string): void { this.selectScaleDeviceCb(deviceId); }
  clearScaleDevice(): void { this.clearScaleDeviceCb(); }
  selectScaleSource(name: SourceSelector): void { this.selectScaleSourceCb(name); }
  loadScaleScenario(name: string): void { this.loadScaleScenarioCb(name); }
}

/**
 * javaScriptProxy 参数的 ArkTS 合规实现。
 */
class ScaleJavaScriptProxy implements JavaScriptProxy {
  object: object;
  name: string;
  methodList: Array<string>;
  controller: webview.WebviewController;

  constructor(object: object, name: string, methodList: Array<string>, controller: webview.WebviewController) {
    this.object = object;
    this.name = name;
    this.methodList = methodList;
    this.controller = controller;
  }
}

/** EntryAbility 与页面共享的能力通道。 */
export interface ScaleAbilityContext {
  onForeground(): void;
  onBackground(): void;
  requestBluetoothPermission(): void;
}

/**
 * 把 rawfile 场景 JSON 的单个原始事件归一化为内部 ScenarioEvent。
 * JSON 形态：{atMs, grams} / {atMs, silence:true, durationMs} / {atMs, gap} /
 * {atMs, revoke:'permission'}。内部统一带 type 判别字段（ArkTS 友好）。
 */
function normalizeEvent(raw: Record<string, Object>): ScenarioEvent {
  const atRaw = raw['atMs'];
  const atMs: number = typeof atRaw === 'number' ? (atRaw as number) : 0;

  const gramsRaw = raw['grams'];
  if (typeof gramsRaw === 'number') {
    const seqRaw = raw['seqOverride'];
    const seq: number | undefined = typeof seqRaw === 'number' ? (seqRaw as number) : undefined;
    const g: GramsEvent = { type: 'grams', atMs: atMs, grams: gramsRaw as number, seqOverride: seq };
    return g;
  }

  // 静默：JSON 用 silence+durationMs，亦兼容 gap 字段。
  if (raw['silence'] === true || raw['gap'] !== undefined || raw['durationMs'] !== undefined) {
    const gapRaw = raw['gap'];
    const durRaw = raw['durationMs'];
    let gap: number = 0;
    if (typeof gapRaw === 'number') {
      gap = gapRaw as number;
    } else if (typeof durRaw === 'number') {
      gap = durRaw as number;
    }
    const s: GapEvent = { type: 'gap', atMs: atMs, gap: gap };
    return s;
  }

  if (raw['revoke'] !== undefined) {
    const r: RevokeEvent = { type: 'revoke', atMs: atMs, revoke: 'permission' };
    return r;
  }

  // 兜底：无法识别的事件当作 0g 读数。
  const d: GramsEvent = { type: 'grams', atMs: atMs, grams: 0 };
  return d;
}

/** 解析场景 JSON 文本为强类型 Scenario（归一化事件）。 */
function parseScenario(text: string): Scenario | null {
  try {
    const obj = JSON.parse(text) as Record<string, Object>;
    const eventsRaw = obj['events'];
    const events: ScenarioEvent[] = [];
    if (Array.isArray(eventsRaw)) {
      const arr = eventsRaw as Object[];
      for (let i = 0; i < arr.length; i++) {
        const item = arr[i];
        if (item !== null && typeof item === 'object') {
          events.push(normalizeEvent(item as Record<string, Object>));
        }
      }
    }
    const nameRaw = obj['name'];
    const repeatRaw = obj['repeat'];
    const scenario: Scenario = {
      name: typeof nameRaw === 'string' ? (nameRaw as string) : 'unnamed',
      repeat: repeatRaw === true,
      events: events,
    };
    return scenario;
  } catch (e) {
    return null;
  }
}

@Entry
@Component
struct Index {
  private webController: webview.WebviewController = new webview.WebviewController();
  private bridge: ScaleWebBridge | null = null;
  private diagnostics: ScaleDiagnostics = new ScaleDiagnostics();
  private clock: MonotonicClock = new WallClock();
  private scaleSource: ScaleSource = new ScriptScaleSource(new WallClock());

  @State statusText: string = '未启动';
  @State statusState: string = 'off';
  @State currentSourceName: string = 'script';
  @State currentScenario: string = '(无)';
  private lastReadingAtEpochMs: number | null = null;

  aboutToAppear(): void {
    const host: BridgeHost = new IndexBridgeHost(
      () => this.startScan(),
      () => this.stopScan(),
      () => this.currentStatusJson(),
      () => this.currentDevicesJson(),
      (deviceId: string) => this.selectScaleDevice(deviceId),
      () => this.clearScaleDevice(),
      (name: SourceSelector) => this.selectSource(name),
      (name: string) => this.loadScenario(name),
    );
    this.bridge = new ScaleWebBridge(this.webController, host, this.diagnostics);
    this.diagnostics.setSource(this.scaleSource.sourceName);
    this.attachCallbacks(this.scaleSource);
    this.attachDevicesCallback(this.scaleSource);
    // 默认源：EntryAbility 已请求蓝牙权限并把结果存 AppStorage('bluetoothGranted')。
    // 已授权则默认 BLE 实时源（不自动 start，等用户/网页触发，避免无谓扫描耗电）；
    // 未授权则用 script 源并预加载场景，便于无 BLE 环境演练全部异常场景。
    const granted = AppStorage.get<boolean>('bluetoothGranted');
    if (granted === true) {
      const b = new BleK797Source(this.clock, this.diagnostics);
      this.switchSource(b, 'ble');
      this.currentScenario = '(BLE 实时扫描)';
    } else {
      this.loadScenarioInto('mouse_normal_26_3g');
    }
  }

  /** 给某个源绑定读数/状态回调（切源时对新源重新绑定）。 */
  private attachCallbacks(src: ScaleSource): void {
    src.onReading((reading: ScaleReading) => {
      this.lastReadingAtEpochMs = reading.receivedAtEpochMs;
      this.diagnostics.recordAccepted();
      if (this.bridge !== null) {
        this.bridge.pushReading(reading);
      }
    });
    src.onStatus((status: ScaleStatus) => {
      this.statusText = status.message;
      this.statusState = status.state;
      if (this.bridge !== null) {
        this.bridge.pushStatus(status);
      }
    });
  }

  /** 仅 BleK797Source 支持设备发现表订阅；其他源忽略（空 payload 兜底）。 */
  private attachDevicesCallback(src: ScaleSource): void {
    if (src instanceof BleK797Source) {
      const b = src as BleK797Source;
      b.onDevicesChanged(() => {
        if (this.bridge !== null) {
          this.bridge.pushDevices(b.getDevicesJson());
        }
      });
    }
  }

  /** 切换读数源：停旧源、换新源、重绑回调、刷新诊断来源。 */
  private switchSource(src: ScaleSource, displayName: string): void {
    this.scaleSource.stop();
    this.scaleSource = src;
    this.attachCallbacks(src);
    this.attachDevicesCallback(src);
    this.currentSourceName = displayName;
    this.diagnostics.setSource(src.sourceName);
    this.statusText = `已切换来源：${displayName}`;
  }

  // ---------------------------------------------------------------
  // BridgeHost 实现
  // ---------------------------------------------------------------

  private startScan(): void {
    this.scaleSource.start();
  }

  private stopScan(): void {
    this.scaleSource.stop();
  }

  private currentStatusJson(): string {
    const s = this.scaleSource.state;
    const last = this.lastReadingAtEpochMs === null ? 'null' : String(this.lastReadingAtEpochMs);
    const sel = this.scaleSource.currentSelectedDeviceId;
    const selStr = sel === null ? 'null' : `"${escapeJsonInline(sel)}"`;
    return '{'
      + `"device":"K797"`
      + `,"state":"${s}"`
      + `,"message":"${escapeJsonInline(this.statusText)}"`
      + `,"lastReadingAtEpochMs":${last}`
      + `,"source":"${this.currentSourceName}"`
      + `,"selectedDeviceId":${selStr}`
      + '}';
  }

  /** 返回发现表 JSON；非 BLE 源返回空发现表的契约形状态。 */
  private currentDevicesJson(): string {
    if (this.scaleSource instanceof BleK797Source) {
      return (this.scaleSource as BleK797Source).getDevicesJson();
    }
    // script/replay 源不支持设备发现：返回空表、scanning 取当前状态、selectedDeviceId=null。
    const st = this.scaleSource.state;
    const scanning = st === 'scanning' || st === 'active' || st === 'stale';
    return `{"devices":[],"scanning":${scanning},"selectedDeviceId":null}`;
  }

  /** 选定设备：仅对 BLE 源有效。 */
  private selectScaleDevice(deviceId: string): void {
    if (this.scaleSource instanceof BleK797Source) {
      (this.scaleSource as BleK797Source).selectScaleDevice(deviceId);
    }
    // 非 BLE 源忽略。
  }

  /** 取消选择：仅对 BLE 源有效。 */
  private clearScaleDevice(): void {
    if (this.scaleSource instanceof BleK797Source) {
      (this.scaleSource as BleK797Source).clearScaleDevice();
    }
  }

  private selectSource(name: SourceSelector): void {
    if (name === 'script') {
      const s = new ScriptScaleSource(this.clock);
      this.switchSource(s, 'script');
      this.loadScenarioInto('mouse_normal_26_3g');
    } else if (name === 'replay') {
      const r = new ReplayScaleSource(this.clock, this.diagnostics);
      this.loadReplayInto(r);
      this.switchSource(r, 'replay');
    } else {
      // 'ble'：真实 BLE 扫描源（K797 不可连接广播）。start 时才真正扫描，
      // 此处仅实例化与切换；蓝牙未开/权限不足会在 start 时转 bluetooth_off/unauthorized。
      const b = new BleK797Source(this.clock, this.diagnostics);
      this.switchSource(b, 'ble');
      this.currentScenario = '(BLE 实时扫描)';
    }
  }

  private loadScenario(name: string): void {
    // 网页 debug 面板调用：确保当前是 script 源再加载。
    if (this.currentSourceName !== 'script') {
      const s = new ScriptScaleSource(this.clock);
      this.switchSource(s, 'script');
    }
    this.loadScenarioInto(name);
  }

  /** 从 rawfile 读取场景并注入当前 script 源。 */
  private loadScenarioInto(name: string): void {
    const text = this.readRawFile(`scenarios/${name}.json`);
    if (text === null) {
      this.statusText = `场景读取失败：${name}`;
      return;
    }
    const scenario = parseScenario(text);
    if (scenario === null) {
      this.statusText = `场景解析失败：${name}`;
      return;
    }
    const cur = this.scaleSource;
    if (cur instanceof ScriptScaleSource) {
      cur.loadScenario(scenario);
      this.currentScenario = name;
      this.statusText = `已加载场景：${name}`;
    }
  }

  /** 从 rawfile 读取 captured_raw.jsonl 并注入 replay 源。 */
  private loadReplayInto(r: ReplayScaleSource): void {
    const text = this.readRawFile('captured_raw.jsonl');
    if (text !== null) {
      r.loadCapturedRaw(text);
      this.currentScenario = 'captured_raw.jsonl';
    }
  }

  /** 读 rawfile 文本；失败返回 null。 */
  private readRawFile(path: string): string | null {
    try {
      const ctx = getContext(this);
      const bytes = ctx.resourceManager.getRawFileContentSync(path);
      const decoder = new util.TextDecoder('utf-8');
      return decoder.decodeToString(bytes);
    } catch (e) {
      return null;
    }
  }

  // ---------------------------------------------------------------
  // UI
  // ---------------------------------------------------------------

  build() {
    Column() {
      // 状态条。
      Row() {
        Text(`[${this.currentSourceName}] ${this.statusText}`)
          .fontSize(13)
          .fontColor('#333333')
          .layoutWeight(1)
      }
      .width('100%')
      .padding({ left: 12, right: 12, top: 8, bottom: 4 })

      // debug 面板：源切换 / 启停。
      if (DEBUG_ENABLED) {
        Row({ space: 8 }) {
          Button('脚本源').onClick(() => this.selectSource('script')).fontSize(12).layoutWeight(1)
          Button('回放源').onClick(() => this.selectSource('replay')).fontSize(12).layoutWeight(1)
          Button('BLE源').onClick(() => this.selectSource('ble')).fontSize(12).layoutWeight(1)
        }
        .width('100%')
        .padding({ left: 12, right: 12, bottom: 4 })

        Row({ space: 8 }) {
          Button('开始').onClick(() => this.startScan()).fontSize(12).layoutWeight(1)
          Button('停止').onClick(() => this.stopScan()).fontSize(12).layoutWeight(1)
        }
        .width('100%')
        .padding({ left: 12, right: 12, bottom: 4 })

        // debug 面板：场景加载。
        Flex({ wrap: FlexWrap.Wrap }) {
          ForEach(SCENARIO_NAMES, (name: string) => {
            Button(name)
              .fontSize(10)
              .margin({ right: 6, bottom: 6 })
              .onClick(() => this.loadScenario(name))
          }, (name: string) => name)
        }
        .width('100%')
        .padding({ left: 12, right: 12, bottom: 4 })
      }

      Web({ src: TRUSTED_URL, controller: this.webController })
        .javaScriptAccess(true)
        .javaScriptProxy(
          new ScaleJavaScriptProxy(
            this.bridge !== null ? this.bridge.getProxyObject() : new Object(),
            'MiceAutomaticScale',
            this.bridge !== null
              ? this.bridge.getReleaseMethodNames().concat(this.bridge.getDebugMethodNames())
              : ['startScaleScan', 'stopScaleScan', 'getScaleStatus'],
            this.webController,
          ),
        )
        .domStorageAccess(true)
        .onPageEnd(() => {
          if (this.bridge !== null) {
            this.bridge.onPageFinish();
          }
        })
        .onOverrideUrlLoading((webResourceRequest: WebResourceRequest): boolean => {
          const url = webResourceRequest.getRequestUrl();
          const trusted = this.bridge ? this.bridge.isTrustedUrl(url) : false;
          return !trusted;
        })
        .width('100%')
        .layoutWeight(1)
    }
    .width('100%')
    .height('100%')
  }
}

/** 极简 JSON 字符串转义（与 ScaleStatus.toJson 风格一致，手写拼串场景使用）。 */
function escapeJsonInline(value: string): string {
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
