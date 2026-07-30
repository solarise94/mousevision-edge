package com.pingoodmice.miceautomatic

import android.annotation.SuppressLint
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.os.ParcelUuid
import android.os.SystemClock
import org.json.JSONObject
import java.time.Instant
import java.util.ArrayDeque

/**
 * Advertisement-only reader for the non-connectable K797 scale.
 *
 * No GATT connection or pairing is attempted. Identity is the conjunction of
 * local name, manufacturer id and protocol prefix; the BLE address is emitted
 * for diagnostics only because it may be a rotating private address.
 */
class K797BleScanner(
    context: Context,
    private val listener: Listener,
) {
    interface Listener {
        fun onScaleReading(json: String)
        fun onScaleStatus(json: String)
    }

    companion object {
        private const val DEVICE_NAME = "K797"
        private const val MANUFACTURER_ID = 0x0000
        private const val MIN_PAYLOAD_BYTES = 18
        private const val STABLE_WINDOW_MS = 1_200L
        private const val STABLE_MIN_SAMPLES = 3
        private const val STABLE_RAW_SPAN = 1 // 0.1 g
        private val PREFIX = byteArrayOf(
            0xCA.toByte(), 0xE8.toByte(), 0x03, 0x28,
            0x08, 0x95.toByte(), 0xCA.toByte(), 0x02, 0x10,
        )
        private val PREFIX_MASK = ByteArray(PREFIX.size) { 0xFF.toByte() }
    }

    private val manager = context.getSystemService(BluetoothManager::class.java)
    private var scanning = false
    private var lastReadingAtEpochMs: Long? = null
    private val recent = ArrayDeque<Pair<Long, Int>>()

    fun statusJson(): String = JSONObject()
        .put("device", DEVICE_NAME)
        .put("state", if (scanning) "scanning" else "off")
        .put("lastReadingAtEpochMs", lastReadingAtEpochMs ?: JSONObject.NULL)
        .toString()

    @SuppressLint("MissingPermission")
    fun start() {
        if (scanning) return
        val adapter = manager?.adapter
        if (adapter == null) {
            emitStatus("unsupported", "手机没有可用的蓝牙适配器")
            return
        }
        if (!adapter.isEnabled) {
            emitStatus("off", "请开启手机蓝牙")
            return
        }
        val scanner = adapter.bluetoothLeScanner
        if (scanner == null) {
            emitStatus("unsupported", "无法启动 BLE 扫描")
            return
        }

        val filter = ScanFilter.Builder()
            .setDeviceName(DEVICE_NAME)
            .setManufacturerData(MANUFACTURER_ID, PREFIX, PREFIX_MASK)
            .build()
        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .setCallbackType(ScanSettings.CALLBACK_TYPE_ALL_MATCHES)
            .build()
        try {
            scanner.startScan(listOf(filter), settings, callback)
            scanning = true
            emitStatus("scanning", "正在扫描 K797 广播")
        } catch (error: SecurityException) {
            emitStatus("unauthorized", "缺少附近设备/蓝牙权限")
        } catch (error: IllegalStateException) {
            emitStatus("error", error.message ?: "BLE 扫描启动失败")
        }
    }

    @SuppressLint("MissingPermission")
    fun stop() {
        if (!scanning) return
        try {
            manager?.adapter?.bluetoothLeScanner?.stopScan(callback)
        } catch (_: SecurityException) {
            // Permission may have been revoked while the Activity was paused.
        }
        scanning = false
        recent.clear()
        emitStatus("off", "扫描已停止")
    }

    private val callback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            val record = result.scanRecord ?: return
            if (record.deviceName != DEVICE_NAME) return
            val payload = record.getManufacturerSpecificData(MANUFACTURER_ID) ?: return
            if (payload.size < MIN_PAYLOAD_BYTES) return
            if (!payload.copyOfRange(0, PREFIX.size).contentEquals(PREFIX)) return

            val raw = (payload[9].toInt() and 0xFF) or
                ((payload[10].toInt() and 0xFF) shl 8)
            val nowElapsed = SystemClock.elapsedRealtime()
            val nowEpoch = System.currentTimeMillis()
            recent.addLast(nowElapsed to raw)
            while (recent.isNotEmpty() && nowElapsed - recent.first().first > STABLE_WINDOW_MS) {
                recent.removeFirst()
            }
            val values = recent.map { it.second }
            val stable = values.size >= STABLE_MIN_SAMPLES &&
                (values.maxOrNull()!! - values.minOrNull()!!) <= STABLE_RAW_SPAN
            lastReadingAtEpochMs = nowEpoch

            val json = JSONObject()
                .put("device", DEVICE_NAME)
                .put("manufacturerId", MANUFACTURER_ID)
                .put("grams", raw / 10.0)
                .put("raw", raw)
                .put("rssi", result.rssi)
                .put("receivedAt", Instant.ofEpochMilli(nowEpoch).toString())
                .put("receivedAtEpochMs", nowEpoch)
                .put("stable", stable)
                .put("stableSource", "derived_repeat")
                .put("address", result.device.address)
                .put("payloadHex", payload.joinToString(" ") { "%02X".format(it) })
            listener.onScaleReading(json.toString())
            emitStatus("active", "已收到 K797 广播")
        }

        override fun onScanFailed(errorCode: Int) {
            scanning = false
            emitStatus("error", "BLE 扫描失败：$errorCode")
        }
    }

    private fun emitStatus(state: String, message: String) {
        listener.onScaleStatus(
            JSONObject()
                .put("device", DEVICE_NAME)
                .put("state", state)
                .put("message", message)
                .put("lastReadingAtEpochMs", lastReadingAtEpochMs ?: JSONObject.NULL)
                .toString(),
        )
    }
}
