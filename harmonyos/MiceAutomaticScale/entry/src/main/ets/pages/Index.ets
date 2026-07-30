/**
 * Index.ets
 *
 * 称量主页（阶段 0 空壳）：
 * - 用 Web 组件加载固定页面 TRUSTED_URL（https://weight.pingoodmice.top:16206/mobile）；
 * - 注入 javaScriptProxy（window.MiceAutomaticScale），方法名与契约一致；
 * - 实现 BridgeHost：把网页的 start/stop/查询/切换命令转发给当前 ScaleSource；
 * - 把 ScaleSource 的读数与状态经 ScaleWebBridge 推回网页（节流 ~10 Hz）；
 * - 导航白名单：仅放行 weight.pingoodmice.top:16206。
 *
 * 阶段 0 范围：先把"加载页面 + Bridge 握手"跑通；BLE 扫描（BleK797Source）
 * 在阶段 3 接入，此处默认用 ScriptScaleSource 作为占位源以验证 UI 链路。
 *
 * TODO(Phase-0): 真机核实项（编译期以 API 24 SDK 校验）：
 *   1. Web 组件 javaScriptProxy 的对象/方法签名（@kit.ArkWeb webview）；
 *   2. 导航拦截回调（onOverrideUrlLoading / onLoadIntercept）精确签名；
 *   3. EntryAbility 与页面间的 AppStorage 通道（前后台/权限结果）。
 */

import { webview } from '@kit.ArkWeb';
import { ScaleWebBridge, type BridgeHost, type SourceSelector, TRUSTED_URL } from '../bridge/ScaleWebBridge';
import { ScaleDiagnostics } from '../diagnostics/ScaleDiagnostics';
import type { ScaleSource } from '../scale/ScaleSource';
import { ScriptScaleSource } from '../scale/ScriptScaleSource';
import type { ScaleStatus } from '../scale/ScaleStatus';
import type { ScaleReading } from '../scale/ScaleReading';

/** EntryAbility 与页面共享的能力通道（AppStorage key = 'scaleAbilityContext'）。 */
export interface ScaleAbilityContext {
  /** 前台恢复：恢复扫描与屏幕常亮。 */
  onForeground(): void;
  /** 退到后台：可暂停扫描。 */
  onBackground(): void;
  /** 触发蓝牙权限请求（EntryAbility 持有 abilityAccessController）。 */
  requestBluetoothPermission(): void;
}

@Entry
@Component
struct Index {
  private webController: webview.WebviewController = new webview.WebviewController();
  private bridge: ScaleWebBridge | null = null;
  private diagnostics: ScaleDiagnostics = new ScaleDiagnostics();
  private scaleSource: ScaleSource = new ScriptScaleSource();

  /** 页面顶部展示的当前天平状态（简化；详细重量在网页内显示）。 */
  @State statusText: string = '未启动';
  @State statusState: string = 'off';
  /** 最近一次合法读数的 epoch 毫秒（供 getScaleStatus 查询）。 */
  private lastReadingAtEpochMs: number | null = null;

  aboutToAppear(): void {
    const host: BridgeHost = {
      startScaleScan: () => this.startScan(),
      stopScaleScan: () => this.stopScan(),
      getScaleStatusJson: () => this.currentStatusJson(),
      selectScaleSource: (name: SourceSelector) => this.selectSource(name),
      loadScaleScenario: (name: string) => this.loadScenario(name),
    };
    this.bridge = new ScaleWebBridge(this.webController, host, this.diagnostics);
    this.diagnostics.setSource(this.scaleSource.sourceName);

    // 读数与状态桥回网页。
    this.scaleSource.onReading((reading: ScaleReading) => {
      this.lastReadingAtEpochMs = reading.receivedAtEpochMs;
      this.diagnostics.recordAccepted();
      this.bridge?.pushReading(reading);
    });
    this.scaleSource.onStatus((status: ScaleStatus) => {
      this.statusText = status.message;
      this.statusState = status.state;
      this.bridge?.pushStatus(status);
    });
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
    return '{'
      + `"device":"K797"`
      + `,"state":"${s}"`
      + `,"message":"${this.statusText}"`
      + `,"lastReadingAtEpochMs":${last}`
      + `,"source":"${this.scaleSource.sourceName}"`
      + '}';
  }

  private selectSource(name: SourceSelector): void {
    // 阶段 0：仅 script 占位；ble/replay 在阶段 2/3 接入。
    // 保持接口存在以便网页 debug 面板调用。
  }

  private loadScenario(name: string): void {
    // 阶段 0：场景加载留待阶段 2（ScriptScaleSource.loadScenario + rawfile）。
  }

  // ---------------------------------------------------------------
  // UI
  // ---------------------------------------------------------------

  build() {
    Column() {
      // 原生状态条（便于无网页调试时肉眼确认 Bridge 链路）。
      Row() {
        Text(this.statusText)
          .fontSize(13)
          .fontColor('#333333')
      }
      .width('100%')
      .padding({ left: 12, right: 12, top: 8, bottom: 4 })

      Web({ src: TRUSTED_URL, controller: this.webController })
        .javaScriptAccess(true)
        .javaScriptProxy({
          object: this.bridge?.getProxyObject(),
          name: 'MiceAutomaticScale',
          methodList: this.bridge
            ? this.bridge.getReleaseMethodNames().concat(this.bridge.getDebugMethodNames())
            : ['startScaleScan', 'stopScaleScan', 'getScaleStatus'],
          controller: this.webController,
        })
        .domStorageAccess(true)
        .onPageEnd(() => {
          // 页面加载完成后重新握手，重推当前状态。
          this.bridge?.onPageFinish();
        })
        .onOverrideUrlLoading((webResourceRequest: WebResourceRequest): boolean => {
          // 白名单：非受信主机拦截（返回 true 表示已处理、不再加载）。
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
