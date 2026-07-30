/**
 * EntryAbility.ets
 *
 * Stage 模型入口 Ability。负责：
 * - 进入前台时请求蓝牙权限（ohos.permission.ACCESS_BLUETOOTH）；
 * - 把前后台事件下发给页面，使页面能 stop/resume 扫描；
 * - 保持屏幕常亮（称量页必须前台）。
 *
 * TODO(Phase-0) 设备 API 核实清单：
 *   1. 权限请求 API：abilityAccessController.requestPermissionsFromUser
 *      的精确导入路径与参数（@kit.AbilityKit）；
 *   2. 屏幕常亮：@kit.BasicServicesKit power.setKeepScreenOn 与
 *      window.setWindowKeepScreenOn 二选一，需核实可用版本；
 *   3. 生命周期 onForeground/onBackground 的 ability 回调签名。
 */

import { AbilityConstant, UIAbility, Want } from '@kit.AbilityKit';
import { abilityAccessController, Permissions } from '@kit.AbilityKit';
import { window } from '@kit.ArkUI';
import type { ScaleAbilityContext } from '../pages/Index';

export default class EntryAbility extends UIAbility {
  private scaleContext: ScaleAbilityContext | null = null;

  onCreate(want: Want, launchParam: AbilityConstant.LaunchParam): void {
    // 应用创建；可在此初始化全局对象。
  }

  onWindowStageCreate(windowStage: window.WindowStage): void {
    // 把 ability 引用通过 'entryAbility' 全局通道暴露给页面，
    // 页面用它接收前后台事件与权限结果。
    const ctx: ScaleAbilityContext = {
      onForeground: () => this.handleForeground(),
      onBackground: () => this.handleBackground(),
      requestBluetoothPermission: () => this.requestBluetoothPermission(),
    };
    this.scaleContext = ctx;
    AppStorage.setOrCreate<ScaleAbilityContext>('scaleAbilityContext', ctx);

    windowStage.loadContent('pages/Index', (err) => {
      if (err.code) {
        // 加载失败仅记录，不崩溃。
      }
    });

    // 进入时即请求蓝牙权限（用户首次进入称量页）。
    this.requestBluetoothPermission();
  }

  onWindowStageDestroy(): void {
    this.scaleContext = null;
  }

  onForeground(): void {
    this.handleForeground();
  }

  onBackground(): void {
    this.handleBackground();
  }

  private handleForeground(): void {
    // TODO(Phase-0): 用 power.setKeepScreenOn(true) 或 window keepScreenOn。
    //   此处先用 window 通道占位，真机核实后保留其一。
    try {
      this.windowStage?.getMainWindow().then((win) => {
        win.setWindowKeepScreenOn(true).catch(() => {});
      });
    } catch (e) {
      // 忽略。
    }
  }

  private handleBackground(): void {
    // 后台时不再保持屏幕常亮。
    try {
      this.windowStage?.getMainWindow().then((win) => {
        win.setWindowKeepScreenOn(false).catch(() => {});
      });
    } catch (e) {
      // 忽略。
    }
  }

  /** 请求蓝牙权限；结果通过 AppStorage 通道通知页面。 */
  private requestBluetoothPermission(): void {
    const permissions: Permissions[] = ['ohos.permission.ACCESS_BLUETOOTH'];
    try {
      const controller = abilityAccessController.create(this.context);
      controller.requestPermissionsFromUser(permissions).then((data) => {
        const granted = data.authResults.every((r) => r === 0);
        AppStorage.setOrCreate<boolean>('bluetoothGranted', granted);
      }).catch(() => {
        AppStorage.setOrCreate<boolean>('bluetoothGranted', false);
      });
    } catch (e) {
      AppStorage.setOrCreate<boolean>('bluetoothGranted', false);
    }
  }
}
