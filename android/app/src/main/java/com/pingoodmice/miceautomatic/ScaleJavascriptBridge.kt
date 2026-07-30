package com.pingoodmice.miceautomatic

import android.webkit.JavascriptInterface

/** Narrow bridge exposed only to the trusted MiceAutomatic HTTPS origin. */
class ScaleJavascriptBridge(
    private val startScan: () -> Unit,
    private val stopScan: () -> Unit,
    private val statusJson: () -> String,
) {
    @JavascriptInterface
    fun startScan() = startScan.invoke()

    @JavascriptInterface
    fun stopScan() = stopScan.invoke()

    @JavascriptInterface
    fun getStatus(): String = statusJson.invoke()
}
