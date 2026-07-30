package com.pingoodmice.miceautomatic

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.webkit.PermissionRequest
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import org.json.JSONObject

class MainActivity : Activity(), K797BleScanner.Listener {
    companion object {
        private const val BLE_PERMISSION_REQUEST = 797
        private const val CAMERA_PERMISSION_REQUEST = 798
        private const val TRUSTED_HOST = "weight.pingoodmice.top"
        private const val TRUSTED_PORT = 16206
    }

    private lateinit var webView: WebView
    private lateinit var scanner: K797BleScanner
    private var pendingCameraRequest: PermissionRequest? = null

    @SuppressLint("SetJavaScriptEnabled", "JavascriptInterface")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        scanner = K797BleScanner(this, this)
        webView = WebView(this)
        setContentView(webView)

        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.settings.mediaPlaybackRequiresUserGesture = false
        webView.addJavascriptInterface(
            ScaleJavascriptBridge(
                startScan = { runOnUiThread { ensureBlePermissionAndStart() } },
                stopScan = { runOnUiThread { scanner.stop() } },
                statusJson = { scanner.statusJson() },
            ),
            "AndroidScale",
        )
        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                return !isTrusted(request.url)
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
        webView.loadUrl(BuildConfig.MICE_WEB_URL)
    }

    override fun onResume() {
        super.onResume()
        if (::webView.isInitialized) webView.onResume()
    }

    override fun onPause() {
        scanner.stop()
        webView.onPause()
        super.onPause()
    }

    override fun onDestroy() {
        scanner.stop()
        webView.removeJavascriptInterface("AndroidScale")
        webView.destroy()
        super.onDestroy()
    }

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
            BLE_PERMISSION_REQUEST -> if (granted) scanner.start() else onScaleStatus(
                JSONObject().put("device", "K797").put("state", "unauthorized")
                    .put("message", "未授予附近设备/蓝牙权限").toString(),
            )
            CAMERA_PERMISSION_REQUEST -> {
                val request = pendingCameraRequest
                pendingCameraRequest = null
                if (granted) request?.grant(arrayOf(PermissionRequest.RESOURCE_VIDEO_CAPTURE)) else request?.deny()
            }
        }
    }

    override fun onScaleReading(json: String) = dispatchScaleEvent(
        "miceautomatic:scale-reading",
        json,
    )

    override fun onScaleStatus(json: String) = dispatchScaleEvent(
        "miceautomatic:scale-status",
        json,
    )

    private fun dispatchScaleEvent(eventName: String, json: String) {
        if (!::webView.isInitialized) return
        val quoted = JSONObject.quote(json)
        val script = "window.dispatchEvent(new CustomEvent('$eventName',{detail:JSON.parse($quoted)}));"
        runOnUiThread { webView.evaluateJavascript(script, null) }
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

    private fun isTrusted(uri: Uri): Boolean =
        uri.scheme == "https" && uri.host == TRUSTED_HOST && uri.port == TRUSTED_PORT
}
