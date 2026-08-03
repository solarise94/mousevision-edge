/**
 * ScaleWebBridge.ets
 *
 * H5 与原生侧的桥接控制器：
 * - 管理 Web 组件加载固定页面 https://weight.pingoodmice.top:16206/mobile；
 * - 向页面注入 javaScriptProxy，对象名固定为 `MiceAutomaticScale`
 *   （即 window.MiceAutomaticScale）；
 * - 原生→页面用 CustomEvent 推送（miceautomatic:scale-reading /
 *   miceautomatic:scale-status / miceautomatic:scale-devices），
 *   读数/状态 UI 推送限 ~10 Hz，设备发现表推送限 ~2 Hz（≥500ms）；
 * - 页面→原生方法（javaScriptProxy，release 白名单）：
 *   startScaleScan / stopScaleScan / getScaleStatus /
 *   getScaleDevices / selectScaleDevice / clearScaleDevice；
 * - 导航白名单：仅允许 weight.pingoodmice.top:16206；
 * - debug 构建额外暴露 selectScaleSource / loadScaleScenario，
 *   release 必须不注册。
 *
 * 安全约束：
 * - 所有原生方法只接受 string/void，不接受可执行代码；
 * - selectScaleDevice 对 deviceId 做 [0-9A-Za-z:\-_.]{1,64} 校验，非法直接忽略；
 * - 非白名单主机一律拦截；
 * - 重载页面时重新握手（重推状态与发现表）。
 */

import { webview } from '@kit.ArkWeb';
import type { ScaleReading } from '../scale/ScaleReading';
import type { ScaleStatus } from '../scale/ScaleStatus';
import type { ScaleDiagnostics } from '../diagnostics/ScaleDiagnostics';

/** 固定页面地址（单一事实来源，与 Android BuildConfig.MICE_WEB_URL 一致）。 */
export const TRUSTED_URL: string = 'https://weight.pingoodmice.top:16206/mobile';
export const TRUSTED_HOST: string = 'weight.pingoodmice.top';
export const TRUSTED_PORT: number = 16206;

/** 原生→页面事件名（与 docs §7 / 网页 addEventListener 一致）。 */
export const EVENT_READING: string = 'miceautomatic:scale-reading';
export const EVENT_STATUS: string = 'miceautomatic:scale-status';
export const EVENT_DEVICES: string = 'miceautomatic:scale-devices';

/** UI 推送最小间隔（毫秒），约 10 Hz。 */
const UI_PUSH_MIN_INTERVAL_MS: number = 100;
/** 设备发现表推送最小间隔（毫秒），约 2 Hz。 */
const DEVICES_PUSH_MIN_INTERVAL_MS: number = 500;

/**
 * DEBUG 开关。
 * TODO(Phase-0): 接入 BuildProfile.DEBUG 或编译变体常量；
 *   release 构建必须将其置为 false（不注册 debug 方法）。
 *   当前默认 true 仅为开发期；正式签名包前必须改为读取构建配置。
 */
export const DEBUG_ENABLED: boolean = true;

/** 来源切换命令的合法值。 */
export type SourceSelector = 'ble' | 'replay' | 'script';

/**
 * 桥接依赖的宿主能力（由 Index.ets 实现）。Bridge 本身不持有 ScaleSource，
 * 只通过回调通知宿主执行 start/stop/切换/加载场景，并接收宿主推送的读数与状态。
 */
export interface BridgeHost {
  /** 网页请求开始扫描。 */
  startScaleScan(): void;
  /** 网页请求停止扫描。 */
  stopScaleScan(): void;
  /** 网页查询当前状态（返回 ScaleStatus JSON）。 */
  getScaleStatusJson(): string;
  /** 网页查询发现表（返回 miceautomatic:scale-devices 事件 detail 形状 JSON）。 */
  getScaleDevicesJson(): string;
  /** 网页选定设备（deviceId 已校验合法性后转发给源）。 */
  selectScaleDevice(deviceId: string): void;
  /** 网页取消选择。 */
  clearScaleDevice(): void;
  /** debug：切换来源。 */
  selectScaleSource(name: SourceSelector): void;
  /** debug：加载场景。 */
  loadScaleScenario(name: string): void;
}

/**
 * 注入到 window.MiceAutomaticScale 的代理对象。
 * 注意：方法名与文档契约严格一致；所有方法返回 string/void。
 */
export class ScaleProxyObject {
  private host: BridgeHost;

  constructor(host: BridgeHost) {
    this.host = host;
  }

  /** 网页调用：开始扫描。 */
  startScaleScan(): void {
    this.host.startScaleScan();
  }

  /** 网页调用：停止扫描。 */
  stopScaleScan(): void {
    this.host.stopScaleScan();
  }

  /** 网页调用：查询状态，返回 ScaleStatus JSON 字符串。 */
  getScaleStatus(): string {
    return this.host.getScaleStatusJson();
  }

  /** 网页调用：查询发现表，返回 miceautomatic:scale-devices 事件 detail 形状 JSON。 */
  getScaleDevices(): string {
    return this.host.getScaleDevicesJson();
  }

  /** 网页调用：选定设备（校验 deviceId 合法性后转发给源）。 */
  selectScaleDevice(deviceId: string): void {
    if (!isValidDeviceId(deviceId)) {
      // 非法直接忽略（不抛异常、不改状态）。
      return;
    }
    this.host.selectScaleDevice(deviceId);
  }

  /** 网页调用：取消选择。 */
  clearScaleDevice(): void {
    this.host.clearScaleDevice();
  }

  /** debug：切换来源（仅 DEBUG_ENABLED 时由 Bridge 注册）。 */
  selectScaleSource(name: string): void {
    if (!DEBUG_ENABLED) {
      return;
    }
    if (name === 'ble' || name === 'replay' || name === 'script') {
      this.host.selectScaleSource(name as SourceSelector);
    }
  }

  /** debug：加载场景（仅 DEBUG_ENABLED 时由 Bridge 注册）。 */
  loadScaleScenario(name: string): void {
    if (!DEBUG_ENABLED) {
      return;
    }
    this.host.loadScaleScenario(name);
  }
}

/**
 * 桥接控制器。Index.ets 持有一个实例，绑定到 Web 组件。
 */
export class ScaleWebBridge {
  private controller: webview.WebviewController;
  private host: BridgeHost;
  private diagnostics: ScaleDiagnostics | null;
  private proxyObject: ScaleProxyObject;
  private lastReadingPushMs: number = 0;
  private lastStatusPushMs: number = 0;
  private lastStatusJson: string | null = null;
  private lastDevicesJson: string | null = null;
  private lastDevicesPushMs: number = 0;
  /** 标记有待推送的设备表变化（节流窗口内合并）。 */
  private devicesDirty: boolean = false;
  /** 节流窗口内缓存的最新设备表 JSON（到点推送这一份）。 */
  private pendingDevicesJson: string | null = null;
  /** 是否已安排一次节流刷新定时器。 */
  private devicesFlushScheduled: boolean = false;

  constructor(controller: webview.WebviewController, host: BridgeHost, diagnostics: ScaleDiagnostics | null = null) {
    this.controller = controller;
    this.host = host;
    this.diagnostics = diagnostics;
    this.proxyObject = new ScaleProxyObject(host);
  }

  /**
   * 在 Web 组件创建后、loadUrl 之前调用：注册 javaScriptProxy。
   * 对象名固定为 MiceAutomaticScale。
   *
   * 注：实际 javaScriptProxy 注册由 Web 组件的 .javaScriptProxy() 属性完成，
   * 这里只提供对象与所需方法名列表，供 Index.ets 绑定时使用。
   */
  getProxyObject(): ScaleProxyObject {
    return this.proxyObject;
  }

  /** 需要注入的方法名列表（release 子集）。 */
  getReleaseMethodNames(): string[] {
    return ['startScaleScan', 'stopScaleScan', 'getScaleStatus', 'getScaleDevices', 'selectScaleDevice', 'clearScaleDevice'];
  }

  /** debug 额外方法名列表。 */
  getDebugMethodNames(): string[] {
    return DEBUG_ENABLED ? ['selectScaleSource', 'loadScaleScenario'] : [];
  }

  /** 原生→页面：推送读数（限 ~10 Hz，审计计数仍记录原始包）。 */
  pushReading(reading: ScaleReading): void {
    if (this.diagnostics !== null) {
      // 审计层始终计数原始包，与 UI 节流无关。
    }
    const now = Date.now();
    if (now - this.lastReadingPushMs < UI_PUSH_MIN_INTERVAL_MS) {
      return; // 节流：丢弃本次 UI 推送（不影响审计计数）
    }
    this.lastReadingPushMs = now;
    this.dispatchEvent(EVENT_READING, reading.toJson());
  }

  /** 原生→页面：推送状态（状态变化时立即推送）。 */
  pushStatus(status: ScaleStatus): void {
    const json = status.toJson();
    if (json === this.lastStatusJson) {
      return; // 同状态不重复推送
    }
    this.lastStatusJson = json;
    this.lastStatusPushMs = Date.now();
    this.dispatchEvent(EVENT_STATUS, json);
  }

  /**
   * 原生→页面：推送设备发现表。
   * - 内容变化才推（与上次 JSON 不同）；
   * - 相邻推送 ≥500ms：窗口内的后续变化合并，到点只推最新一份；
   * - 内容未变化直接忽略。
   */
  pushDevices(json: string): void {
    if (json === this.lastDevicesJson) {
      return; // 内容未变化
    }
    const now = Date.now();
    const elapsed = now - this.lastDevicesPushMs;
    if (elapsed < DEVICES_PUSH_MIN_INTERVAL_MS) {
      // 节流窗口内：标记 dirty，到点由定时器刷新最新 JSON。
      this.devicesDirty = true;
      // 缓存最新 JSON 供定时器到点推送（这里只存引用，下次 push 会覆盖）。
      this.pendingDevicesJson = json;
      if (!this.devicesFlushScheduled) {
        this.devicesFlushScheduled = true;
        if (typeof setTimeout !== 'undefined') {
          const remain = DEVICES_PUSH_MIN_INTERVAL_MS - elapsed;
          setTimeout(() => this.flushDevices(), remain > 0 ? remain : DEVICES_PUSH_MIN_INTERVAL_MS);
        }
      }
      return;
    }
    this.lastDevicesJson = json;
    this.lastDevicesPushMs = now;
    this.devicesDirty = false;
    this.pendingDevicesJson = null;
    this.dispatchEvent(EVENT_DEVICES, json);
  }

  /** 节流到点后刷新待推送的设备表（取最新 JSON）。 */
  private flushDevices(): void {
    this.devicesFlushScheduled = false;
    if (!this.devicesDirty) {
      return;
    }
    const json = this.pendingDevicesJson;
    this.devicesDirty = false;
    this.pendingDevicesJson = null;
    if (json === null) {
      return;
    }
    if (json === this.lastDevicesJson) {
      return;
    }
    this.lastDevicesJson = json;
    this.lastDevicesPushMs = Date.now();
    this.dispatchEvent(EVENT_DEVICES, json);
  }

  /** 页面加载完成后调用：重新握手，重推当前状态与发现表。 */
  onPageFinish(): void {
    this.lastStatusJson = null;
    this.lastDevicesJson = null;
    // 重推当前状态，让页面刷新后立刻拿到。
    const statusJson = this.host.getScaleStatusJson();
    this.dispatchEvent(EVENT_STATUS, statusJson);
    // 重推当前发现表。
    const devicesJson = this.host.getScaleDevicesJson();
    this.lastDevicesJson = devicesJson;
    this.lastDevicesPushMs = Date.now();
    this.dispatchEvent(EVENT_DEVICES, devicesJson);
  }

  /**
   * 导航白名单校验。返回 true 表示放行，false 表示拦截。
   * 仅允许 https + weight.pingoodmice.top:16206。
   *
   * TODO(Phase-0): 真机核实 Web 组件回调名（onLoadIntercept /
   *   onOverrideUrlLoading / onPageBegin）及 URL 参数类型，
   *   Index.ets 绑定时按 SDK 实际签名调用 isTrustedUrl。
   */
  isTrustedUrl(url: string): boolean {
    if (!url.startsWith('https://')) {
      return false;
    }
    try {
      // 简单解析 host:port，避免依赖 URL 类差异。
      const noScheme = url.substring('https://'.length);
      const slash = noScheme.indexOf('/');
      const hostPort = slash >= 0 ? noScheme.substring(0, slash) : noScheme;
      const colon = hostPort.indexOf(':');
      const host = colon >= 0 ? hostPort.substring(0, colon) : hostPort;
      const port = colon >= 0 ? parseInt(hostPort.substring(colon + 1), 10) : 443;
      return host === TRUSTED_HOST && port === TRUSTED_PORT;
    } catch (e) {
      return false;
    }
  }

  /** 通过 webview 控制器执行 JS 派发 CustomEvent。 */
  private dispatchEvent(eventName: string, json: string): void {
    // 等价于 Android 端：
    //   window.dispatchEvent(new CustomEvent(name,{detail:JSON.parse(jsonStr)}));
    // 注意：JSON 字符串需安全嵌入；用 JSON.stringify 二次包装避免注入。
    const wrapped = jsonStringify(json);
    const script = `(function(){try{window.dispatchEvent(new CustomEvent('${eventName}',{detail:JSON.parse(${wrapped})}));}catch(e){}})();`;
    try {
      this.controller.runJavaScript(script);
    } catch (e) {
      // 控制器未就绪或页面不可用，忽略。
    }
  }

  /** 是否启用 debug 方法。 */
  isDebugEnabled(): boolean {
    return DEBUG_ENABLED;
  }
}

/** 把字符串安全包装成 JS 字符串字面量（带引号）。 */
function jsonStringify(s: string): string {
  // 用 JSON.stringify 处理转义，再保证外层是双引号字符串。
  return JSON.stringify(s);
}

/** 合法 deviceId 校验：字符串、长度 ≤64、仅 [0-9A-Za-z:\-_.]。 */
const DEVICE_ID_RE: RegExp = /^[0-9A-Za-z:\-_.]{1,64}$/;
function isValidDeviceId(deviceId: string): boolean {
  return typeof deviceId === 'string' && DEVICE_ID_RE.test(deviceId);
}
