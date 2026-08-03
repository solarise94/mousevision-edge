package com.pingoodmice.miceautomatic

import android.webkit.JavascriptInterface
import java.util.regex.Pattern

/**
 * 注入到 `window.MiceAutomaticScale` 的 JS 接口对象（对齐 HarmonyOS
 * `ScaleWebBridge.ScaleProxyObject`）。
 *
 * 契约约束（见 ScaleWebBridge.ets 头注释）：
 * - 对象名固定 `MiceAutomaticScale`（由 MainActivity.addJavascriptInterface 注册）；
 * - 所有原生方法只接受 string/void，不接受可执行代码；
 * - selectScaleDevice 对 deviceId 做 `^[0-9A-Za-z:\-_.]{1,64}$` 校验，非法直接忽略
 *   （不抛异常、不改状态）；
 * - 方法名与文档契约严格一致，H5 侧 detectNativeBridge/detectDeviceSupport 靠它探测。
 *
 * 宿主回调（host）全部在主线程上执行（由 MainActivity 提供的 lambda 保证）。
 */
class ScaleJavascriptBridge(
    private val startScaleScan: () -> Unit,
    private val stopScaleScan: () -> Unit,
    private val getScaleStatusJson: () -> String,
    private val getScaleDevicesJson: () -> String,
    private val selectScaleDevice: (deviceId: String) -> Unit,
    private val clearScaleDevice: () -> Unit,
) {
    companion object {
        /** 合法 deviceId 校验：字符串、长度 ≤64、仅 [0-9A-Za-z:\-_.]。 */
        private val DEVICE_ID_RE: Pattern =
            Pattern.compile("^[0-9A-Za-z:\\-_.]{1,64}$")

        internal fun isValidDeviceId(deviceId: String?): Boolean {
            if (deviceId == null) return false
            return DEVICE_ID_RE.matcher(deviceId).matches()
        }
    }

    /** 网页调用：开始扫描。 */
    @JavascriptInterface
    fun startScaleScan() = startScaleScan.invoke()

    /** 网页调用：停止扫描。 */
    @JavascriptInterface
    fun stopScaleScan() = stopScaleScan.invoke()

    /** 网页调用：查询状态，返回 ScaleStatus JSON 字符串。 */
    @JavascriptInterface
    fun getScaleStatus(): String = getScaleStatusJson.invoke()

    /** 网页调用：查询发现表，返回 miceautomatic:scale-devices 事件 detail 形状 JSON。 */
    @JavascriptInterface
    fun getScaleDevices(): String = getScaleDevicesJson.invoke()

    /** 网页调用：选定设备（校验 deviceId 合法性后转发给源）。非法直接忽略。 */
    @JavascriptInterface
    fun selectScaleDevice(deviceId: String) {
        if (!isValidDeviceId(deviceId)) return
        selectScaleDevice.invoke(deviceId)
    }

    /** 网页调用：取消选择。 */
    @JavascriptInterface
    fun clearScaleDevice() = clearScaleDevice.invoke()
}
