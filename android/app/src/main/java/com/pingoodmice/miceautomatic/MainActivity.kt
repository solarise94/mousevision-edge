package com.pingoodmice.miceautomatic

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.view.WindowManager
import android.net.http.SslError
import android.webkit.PermissionRequest
import android.webkit.SslErrorHandler
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import org.json.JSONObject
import java.io.ByteArrayInputStream
import java.io.IOException

/**
 * Android 外壳 Activity：WebView 加载打包进 APK 的本地 H5 + 注入 JS 桥 + BLE 扫描 K797 广播秤。
 *
 * 页面从合成域 https://app.miceautomatic.local/mobile 提供（shouldInterceptRequest
 * 从 assets/www 拦截返回，无真实网络）；API 请求跨源打到配置的服务器并带 token。
 *
 * 对齐 HarmonyOS `Index.ets` + `ScaleWebBridge.ets`：
 * - JS 接口对象名固定 `MiceAutomaticScale`（即 window.MiceAutomaticScale）；
 * - 原生→页面用 CustomEvent 推送三个事件：
 *     miceautomatic:scale-reading  （UI 推送节流 100ms，~10Hz）
 *     miceautomatic:scale-status   （内容变化才推）
 *     miceautomatic:scale-devices  （≥500ms 节流 + 内容变化才推，~2Hz）
 * - 导航白名单：合成域 app.miceautomatic.local 或 https + weight.pingoodmice.top:16206；
 * - 相机 onPermissionRequest：白名单内且已授权才放行（保留）；
 * - 称量页常亮：getWindow().addFlags(FLAG_KEEP_SCREEN_ON)（对齐鸿蒙侧行为）；
 * - onPause 停扫描（保留），避免后台占用 BLE。
 */
class MainActivity : Activity(), K797BleScanner.Listener {
    companion object {
        private const val BLE_PERMISSION_REQUEST = 797
        private const val CAMERA_PERMISSION_REQUEST = 798
        private const val TRUSTED_HOST = "weight.pingoodmice.top"
        private const val TRUSTED_PORT = 16206

        // 合成域：H5 打包进 APK 后以该 https 域从 assets 提供（本地拦截，无真实网络）。
        private const val APP_HOST = "app.miceautomatic.local"
        private const val APP_ORIGIN = "https://app.miceautomatic.local"

        // 原生→页面事件名（与 H5 addEventListener / ScaleWebBridge 一致）。
        private const val EVENT_READING = "miceautomatic:scale-reading"
        private const val EVENT_STATUS = "miceautomatic:scale-status"
        private const val EVENT_DEVICES = "miceautomatic:scale-devices"

        // UI 推送节流参数（对齐 ScaleWebBridge UI_PUSH_MIN_INTERVAL_MS / DEVICES_PUSH_MIN_INTERVAL_MS）。
        private const val READING_PUSH_MIN_INTERVAL_MS = 100L
        private const val DEVICES_PUSH_MIN_INTERVAL_MS = 500L
    }

    private lateinit var webView: WebView
    private lateinit var scanner: K797BleScanner
    private var pendingCameraRequest: PermissionRequest? = null

    // 读数 / 设备表 UI 节流状态（仅主线程访问，无需同步）。
    private var lastReadingPushMs = 0L
    private var lastStatusJson: String? = null
    private var lastDevicesJson: String? = null
    private var lastDevicesPushMs = 0L

    @SuppressLint("SetJavaScriptEnabled", "JavascriptInterface")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        scanner = K797BleScanner(this, this)
        webView = WebView(this)
        setContentView(webView)

        // 称量页必须常亮（对齐鸿蒙侧行为）。
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.settings.mediaPlaybackRequiresUserGesture = false
        webView.addJavascriptInterface(
            ScaleJavascriptBridge(
                startScaleScan = { runOnUiThread { ensureBlePermissionAndStart() } },
                stopScaleScan = { runOnUiThread { scanner.stop() } },
                getScaleStatusJson = { scanner.statusJson() },
                getScaleDevicesJson = { scanner.devicesJson() },
                selectScaleDevice = { id -> runOnUiThread { scanner.selectScaleDevice(id) } },
                clearScaleDevice = { runOnUiThread { scanner.clearScaleDevice() } },
            ),
            "MiceAutomaticScale",
        )
        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                return !isTrusted(request.url)
            }

            /**
             * 本地资产加载：合成域 app.miceautomatic.local 的请求全部由 assets/www
             * 拦截提供（/mobile → mobile.html；静态资源 /static/ 前缀映射到
             * assets/www 下对应文件），其余 host 放行（如 API 服务器）。
             */
            override fun shouldInterceptRequest(view: WebView?, request: WebResourceRequest): WebResourceResponse? {
                val url = request.url
                if (url.host != APP_HOST) return null
                val path = url.path ?: "/"
                return when {
                    path == "/" || path == "/mobile" || path.startsWith("/mobile/") ->
                        assetResponse("www/mobile.html", "text/html", "utf-8")
                    path.startsWith("/static/") -> {
                        val assetPath = "www/" + path.removePrefix("/static/")
                        val mime = mimeFor(assetPath)
                        assetResponse(assetPath, mime, encodingFor(mime))
                    }
                    else -> notFoundResponse()
                }
            }

            /**
             * 放行真实服务器 host 的 SSL 证书错误。
             *
             * 背景：weight.pingoodmice.top 走 FRP，证书由 FRP 服务商签发为
             * Let's Encrypt ECDSA 链（ISRG Root X2）。卓易通等 Android 兼容容器
             * 内的 WebView 信任库较旧，不信任 X2 ECDSA 链，导致 ERR_CERT_AUTHORITY_INVALID
             * 无法加载页面。HarmonyOS 原生 WebView（ArkWeb）信任库较新无此问题。
             *
             * 仅放行真实服务器（TRUSTED_HOST:TRUSTED_PORT）；合成域走拦截器，永远
             * 不会产生 SSL 错误，不在此处放行。信任边界已由主机白名单约束。
             */
            override fun onReceivedSslError(view: WebView?, handler: SslErrorHandler?, error: SslError?) {
                val url = error?.url
                if (handler != null && url != null && isTrustedApiServer(Uri.parse(url))) {
                    handler.proceed()
                } else if (handler != null) {
                    handler.cancel()
                }
            }

            override fun onPageFinished(view: WebView, url: String?) {
                // 重载页面时重新握手：重推当前状态与发现表（对齐 ScaleWebBridge.onPageFinish）。
                resetPushState()
                dispatchScaleEvent(EVENT_STATUS, scanner.statusJson())
                val devicesJson = scanner.devicesJson()
                lastDevicesJson = devicesJson
                lastDevicesPushMs = SystemClock.elapsedRealtime()
                dispatchScaleEvent(EVENT_DEVICES, devicesJson)
            }
        }
        webView.webChromeClient = object : WebChromeClient() {
            override fun onPermissionRequest(request: PermissionRequest) {
                val wantsCamera = request.resources.contains(PermissionRequest.RESOURCE_VIDEO_CAPTURE)
                if (!isTrusted(request.origin) || !wantsCamera) {
                    request.deny()
                    return
                }
                if (checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
                    request.grant(arrayOf(PermissionRequest.RESOURCE_VIDEO_CAPTURE))
                } else {
                    pendingCameraRequest = request
                    requestPermissions(arrayOf(Manifest.permission.CAMERA), CAMERA_PERMISSION_REQUEST)
                }
            }
        }
        val webUrl = if (BuildConfig.MICE_DEV_MODE) {
            // dev 版：H5 开启训练数据采集（每次记录附带读数时间序列）
            APP_ORIGIN + "/mobile?dev=1"
        } else {
            APP_ORIGIN + "/mobile"
        }
        webView.loadUrl(webUrl)
    }

    override fun onResume() {
        super.onResume()
        if (::webView.isInitialized) webView.onResume()
    }

    override fun onPause() {
        // 后台停止扫描（保留）：避免占用 BLE 与耗电，页面会收到 scanning→off。
        scanner.stop()
        webView.onPause()
        super.onPause()
    }

    override fun onDestroy() {
        scanner.stop()
        webView.removeJavascriptInterface("MiceAutomaticScale")
        webView.destroy()
        super.onDestroy()
    }

    @Deprecated("Deprecated in Java", ReplaceWith("super.onBackPressed()"))
    @Suppress("DEPRECATION")
    override fun onBackPressed() {
        if (webView.canGoBack()) webView.goBack() else super.onBackPressed()
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        val granted = grantResults.isNotEmpty() && grantResults.all { it == PackageManager.PERMISSION_GRANTED }
        when (requestCode) {
            BLE_PERMISSION_REQUEST -> if (granted) scanner.start() else {
                // 权限被拒：直接派 unauthorized 状态（scanner 还未 start，需显式推）。
                onScaleStatus(
                    JSONObject()
                        .put("device", "K797")
                        .put("state", "unauthorized")
                        .put("message", "未授予附近设备/蓝牙权限")
                        .put("lastReadingAtEpochMs", JSONObject.NULL)
                        .put("source", "ble")
                        .put("selectedDeviceId", JSONObject.NULL)
                        .toString(),
                )
            }
            CAMERA_PERMISSION_REQUEST -> {
                val request = pendingCameraRequest
                pendingCameraRequest = null
                if (granted) request?.grant(arrayOf(PermissionRequest.RESOURCE_VIDEO_CAPTURE)) else request?.deny()
            }
        }
    }

    // ============================================================
    // K797BleScanner.Listener：原生→页面事件派发（节流 + 内容变化检测）
    // ============================================================

    /** 读数：UI 推送节流 100ms（~10Hz），窗口内丢弃本次 UI 推送（不影响审计）。 */
    override fun onScaleReading(json: String) {
        runOnUiThread {
            val now = SystemClock.elapsedRealtime()
            if (now - lastReadingPushMs < READING_PUSH_MIN_INTERVAL_MS) return@runOnUiThread
            lastReadingPushMs = now
            dispatchScaleEvent(EVENT_READING, json)
        }
    }

    /** 状态：内容变化才推（与上次 JSON 不同才派发）。 */
    override fun onScaleStatus(json: String) {
        runOnUiThread {
            if (json == lastStatusJson) return@runOnUiThread
            lastStatusJson = json
            dispatchScaleEvent(EVENT_STATUS, json)
        }
    }

    /** 设备发现表：内容变化才推 + 相邻推送 ≥500ms（窗口内合并到点推最新一份）。 */
    override fun onScaleDevices(json: String) {
        runOnUiThread {
            if (json == lastDevicesJson) return@runOnUiThread
            val now = SystemClock.elapsedRealtime()
            val elapsed = now - lastDevicesPushMs
            if (elapsed < DEVICES_PUSH_MIN_INTERVAL_MS) {
                // 节流窗口内：到点后用"当前最新 JSON"刷新一次（drop 旧缓存）。
                val remain = DEVICES_PUSH_MIN_INTERVAL_MS - elapsed
                webView.postDelayed({
                    val latest = scanner.devicesJson()
                    if (latest == lastDevicesJson) return@postDelayed
                    lastDevicesJson = latest
                    lastDevicesPushMs = SystemClock.elapsedRealtime()
                    dispatchScaleEvent(EVENT_DEVICES, latest)
                }, remain)
                return@runOnUiThread
            }
            lastDevicesJson = json
            lastDevicesPushMs = now
            dispatchScaleEvent(EVENT_DEVICES, json)
        }
    }

    /** 重置节流去重缓存（页面重载时调用，避免与上一页面实例的缓存比较）。 */
    private fun resetPushState() {
        lastReadingPushMs = 0L
        lastStatusJson = null
        lastDevicesJson = null
        lastDevicesPushMs = 0L
    }

    // ============================================================
    // 工具
    // ============================================================

    private fun dispatchScaleEvent(eventName: String, json: String) {
        if (!::webView.isInitialized) return
        // JSONObject.quote 把 JSON 字符串再安全包装成 JS 字符串字面量（带引号），
        // 等价于 ScaleWebBridge 的 JSON.stringify(json) 二次包装，避免注入。
        val quoted = JSONObject.quote(json)
        val script = "(function(){try{window.dispatchEvent(new CustomEvent('$eventName'," +
            "{detail:JSON.parse($quoted)}));}catch(e){}})();"
        webView.evaluateJavascript(script, null)
    }

    private fun ensureBlePermissionAndStart() {
        val needed = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            listOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
        } else {
            listOf(Manifest.permission.ACCESS_FINE_LOCATION)
        }.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }

        if (needed.isEmpty()) scanner.start()
        else requestPermissions(needed.toTypedArray(), BLE_PERMISSION_REQUEST)
    }

    /** 白名单：合成域（无端口要求）或真实 API 服务器 host:port。 */
    private fun isTrusted(uri: Uri): Boolean =
        (uri.scheme == "https" && uri.host == APP_HOST) ||
            isTrustedApiServer(uri)

    /** 仅真实 API 服务器（SSL 错误放行等场景，合成域不涉及真实 TLS）。 */
    private fun isTrustedApiServer(uri: Uri): Boolean =
        uri.scheme == "https" && uri.host == TRUSTED_HOST && uri.port == TRUSTED_PORT

    /** 从 assets 读取文件并包装为 WebResourceResponse；缺失返回 404。 */
    private fun assetResponse(assetPath: String, mimeType: String, encoding: String?): WebResourceResponse {
        val stream = try {
            assets.open(assetPath)
        } catch (_: IOException) {
            return notFoundResponse()
        }
        return WebResourceResponse(mimeType, encoding, stream)
    }

    private fun notFoundResponse(): WebResourceResponse = WebResourceResponse(
        "text/plain",
        "utf-8",
        404,
        "Not Found",
        emptyMap(),
        ByteArrayInputStream("404 Not Found".toByteArray()),
    )

    private fun mimeFor(assetPath: String): String = when (assetPath.substringAfterLast('.', "").lowercase()) {
        "js" -> "application/javascript"
        "css" -> "text/css"
        "html", "htm" -> "text/html"
        "png" -> "image/png"
        "jpg", "jpeg" -> "image/jpeg"
        "svg" -> "image/svg+xml"
        "json" -> "application/json"
        "woff2" -> "font/woff2"
        "woff" -> "font/woff"
        "ttf" -> "font/ttf"
        "ico" -> "image/x-icon"
        "webp" -> "image/webp"
        "mp4" -> "video/mp4"
        else -> "application/octet-stream"
    }

    private fun encodingFor(mime: String): String? = when (mime) {
        "text/html", "text/css", "application/javascript", "application/json", "image/svg+xml",
        "text/plain",
        -> "utf-8"
        else -> null
    }
}
