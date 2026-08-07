package com.pingoodmice.miceautomatic

import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import android.util.Base64
import android.util.Log
import android.webkit.JavascriptInterface
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
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
    private val context: Context,
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

        private const val TAG = "ScaleJavascriptBridge"

        /** 导出到公共 Download 下的子目录（公众本地版导出报表/数据）。 */
        private const val EXPORT_SUBDIR = "小鼠称重"

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

    /**
     * 网页调用：导出 base64 文件到公共 Download/小鼠称重/。
     *
     * 返回 JSON 字符串 `{"ok":true,"displayName":".."}` 或 `{"ok":false,"error":".."}`。
     * - 文件名做安全过滤（去路径分隔符），重名自动加序号；
     * - API 29+：MediaStore.Downloads 免权限（RELATIVE_PATH）；
     * - API 26-28：Environment.getExternalStoragePublicDirectory 回退分支，需
     *   WRITE_EXTERNAL_STORAGE（manifest 已带 maxSdkVersion=28）。
     */
    @JavascriptInterface
    fun saveToDownloads(filename: String, base64: String, mimeType: String): String {
        val safeName = sanitizeFilename(filename)
        val bytes = try {
            Base64.decode(base64, Base64.DEFAULT)
        } catch (e: IllegalArgumentException) {
            Log.e(TAG, "saveToDownloads: invalid base64", e)
            return err("invalid_base64")
        }
        if (bytes.isEmpty()) return err("empty_data")
        val mime = mimeType.ifBlank { "application/octet-stream" }
        return try {
            val displayName = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                writeViaMediaStore(bytes, safeName, mime)
            } else {
                writeLegacy(bytes, safeName)
            }
            Log.i(TAG, "saveToDownloads ok: $displayName (${bytes.size} bytes)")
            ok(displayName)
        } catch (e: Exception) {
            Log.e(TAG, "saveToDownloads failed: $safeName", e)
            err(e.message ?: "write_failed")
        }
    }

    /** 文件名安全过滤：去路径分隔符 / 控制字符，空则回退默认名。 */
    private fun sanitizeFilename(raw: String): String {
        var name = raw.replace(Regex("[/\\\\]"), "_").trim()
        // 去掉不可见控制字符（防注入/路径穿越）。
        name = name.filter { it.code >= 0x20 }.trim()
        if (name.isEmpty()) name = "export"
        // 保证有扩展名（MediaStore 依扩展名给部分类型建议，保留原始即可）。
        return name
    }

    /** 重名自动加序号，返回无重复的可用文件名。 */
    private fun uniqueName(dir: File, name: String): String {
        val ext = name.substringAfterLast('.', "")
        val base = if (ext.isNotEmpty()) name.removeSuffix(".$ext") else name
        var candidate = name
        var i = 1
        while (File(dir, candidate).exists()) {
            candidate = if (ext.isNotEmpty()) "${base}_${i}.$ext" else "${base}_${i}"
            i++
        }
        return candidate
    }

    /** API 29+：MediaStore.Downloads 写入（免权限）。返回写入后的 display_name。 */
    private fun writeViaMediaStore(bytes: ByteArray, name: String, mime: String): String {
        val resolver = context.contentResolver
        val values = ContentValues().apply {
            put(MediaStore.Downloads.DISPLAY_NAME, name)
            put(MediaStore.Downloads.MIME_TYPE, mime)
            put(
                MediaStore.Downloads.RELATIVE_PATH,
                Environment.DIRECTORY_DOWNLOADS + "/$EXPORT_SUBDIR",
            )
            put(MediaStore.Downloads.IS_PENDING, 1)
        }
        val uri: Uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
            ?: throw IOException("MediaStore insert returned null")
        resolver.openOutputStream(uri)?.use { it.write(bytes) }
            ?: throw IOException("cannot open output stream")
        values.clear()
        values.put(MediaStore.Downloads.IS_PENDING, 0)
        resolver.update(uri, values, null, null)
        return name
    }

    /** API 26-28：公共 Download 目录直写（需 WRITE_EXTERNAL_STORAGE）。返回实际文件名。 */
    private fun writeLegacy(bytes: ByteArray, name: String): String {
        val base = File(
            Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS),
            EXPORT_SUBDIR,
        )
        if (!base.exists() && !base.mkdirs()) throw IOException("cannot create dir ${base.path}")
        val finalName = uniqueName(base, name)
        val target = File(base, finalName)
        FileOutputStream(target).use { it.write(bytes) }
        return finalName
    }

    private fun ok(displayName: String): String =
        JSONObject().put("ok", true).put("displayName", displayName).toString()

    private fun err(message: String): String =
        JSONObject().put("ok", false).put("error", message).toString()
}
