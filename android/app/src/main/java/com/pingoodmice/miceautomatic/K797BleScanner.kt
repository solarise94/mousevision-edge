package com.pingoodmice.miceautomatic

import android.annotation.SuppressLint
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import java.time.Instant
import java.util.concurrent.ConcurrentHashMap

/**
 * K797BleScanner.kt
 *
 * 真实 BLE 扫描读数源（Android），针对不可连接的 K797 广播秤。
 *
 * 与 HarmonyOS 侧 `BleK797Source.ets` / `ScaleSource.ets` / `ScaleStatus.ets` /
 * `ScaleReading.ets` / `K797AdvertisementParser.ets` 逐条对齐（见各方法注释）。
 *
 * 关键契约（必须逐字一致）：
 * - 仅扫描广播，绝不调用 GATT connect，绝不要求配对；
 * - K797 用 ADV_NONCONN_IND 广播，只能被动扫描，必须 SCAN_MODE_LOW_LATENCY 持续监听；
 * - 身份 = Local Name "K797" + Manufacturer ID 0x0000 + 9 字节前缀；
 * - 真实 BLE 地址是随机私有地址，**不进读数**（读数 address 固定 "diagnostic-only"），
 *   地址仅作发现表的 deviceId；
 * - sequence 进程内单调，start 时清零，每条派发的读数 +1（H5 靠它去重）；
 * - stable 派生：连续相同 raw ≥3（raw 一变计数重置为 1）；
 * - stale：单调时钟 elapsedRealtime，>15s 无合法包；stale 绝不产生 0g 读数；
 *   scanning 未收过包不进 stale；
 * - 设备发现/选择：未选定=纯发现模式（不派发读数，状态停 scanning）；选定即仅该设备
 *   的包派发读数；首个匹配读数 → active；
 * - 旧版兜底：start 4s 后若从未调用过 selectScaleDevice/clearScaleDevice 且无选择且
 *   发现表非空 → 自动选 rssi 最强；4s 时表为空则每 4s 重试直到页面用 API 或 stop；
 *   getScaleDevices 是纯查询，**不算**触发兜底取消。
 */
class K797BleScanner(
    context: Context,
    private val listener: Listener,
) {
    /** 宿主回调：派发读数 / 状态 / 设备发现表 JSON（均为契约 JSON 字符串）。 */
    interface Listener {
        fun onScaleReading(json: String)
        fun onScaleStatus(json: String)
        fun onScaleDevices(json: String)
    }

    companion object {
        private const val TAG = "MiceAutomaticScale"

        // 已确认的 K797 协议常量（与 K797AdvertisementParser.ets 逐字一致）。
        private const val DEVICE_NAME = "K797"
        private const val MANUFACTURER_ID = 0x0000
        private const val MIN_PAYLOAD_BYTES = 18
        private const val MAX_GRAMS = 6553.5
        private val PREFIX = byteArrayOf(
            0xCA.toByte(), 0xE8.toByte(), 0x03, 0x28,
            0x08, 0x95.toByte(), 0xCA.toByte(), 0x02, 0x10,
        )
        // deviceKey 规则：k797:<manufacturerId 4位小写hex>:<9字节前缀小写hex连续>
        private val DEVICE_KEY = buildDeviceKey(MANUFACTURER_ID, PREFIX)

        /** 把 epoch 毫秒格式化为 ISO8601 UTC（Z 后缀）。Android Instant.toString 已是
         *  ISO-8601 UTC（如 2026-08-03T12:34:56.789Z），满足 receivedAt 契约。
         *  放 companion 以便 ParsedReading 内部类调用。 */
        private fun toIsoUtc(epochMs: Long): String = Instant.ofEpochMilli(epochMs).toString()

        /** 把克数格式化为协议 JSON：整数带一位小数（如 250.0），非整数最多一位。
         *  放 companion 以便 ParsedReading 内部类调用。 */
        private fun formatGrams(value: Double): String {
            val rounded = Math.round(value * 10) / 10.0
            return if (rounded == rounded.toInt().toDouble()) {
                rounded.toInt().toString() + ".0"
            } else {
                rounded.toString()
            }
        }

        // stale 阈值（与 ScaleSource.ets STALE_THRESHOLD_MS 一致，15s 单调时钟）。
        private const val STALE_THRESHOLD_MS = 15_000L
        // 派生稳定所需连续相同 raw 的最小条数（与 STABLE_MIN_REPEAT 一致）。
        private const val STABLE_MIN_REPEAT = 3
        // 发现设备过期阈值（>15s 未见则剔除，选中设备豁免）。
        private const val DEVICE_EXPIRE_MS = 15_000L
        // 旧版兜底延迟（start 后 4s）。
        private const val LEGACY_FALLBACK_DELAY_MS = 4_000L
        // 发现表过期清理周期。
        private const val EXPIRE_SWEEP_MS = 3_000L

        /**
         * 构造设备主键：k797:<manufacturerIdHex 4位小写>:<prefixHex 小写连续>。
         * 例：k797:0000:cae803280895ca0210
         */
        private fun buildDeviceKey(manufacturerId: Int, prefix: ByteArray): String {
            val idHex = manufacturerId.toString(16).lowercase().padStart(4, '0')
            val prefixHex = prefix.joinToString("") {
                (it.toInt() and 0xFF).toString(16).lowercase().padStart(2, '0')
            }
            return "k797:$idHex:$prefixHex"
        }
    }

    private val appContext = context.applicationContext
    private val mainHandler = Handler(Looper.getMainLooper())
    private val manager = appContext.getSystemService(BluetoothManager::class.java)

    // ---------- 运行态 ----------
    @Volatile private var started = false
    @Volatile private var scanning = false
    private var state: String = "off"
        @Synchronized set
    private var message: String = "扫描已停止"
        @Synchronized set

    // 序号与 stale 派生状态（handleParseResult 在 mainHandler 线程上调用，无需额外锁）。
    private var sequenceCounter = 0
    private var lastReadingAtEpochMs: Long? = null
    private var lastReadingAtMonotonicMs: Long? = null
    private var lastRaw: Int? = null
    private var consecutiveSameRaw = 0

    // ---------- 设备发现 / 选择 ----------
    /** 发现表：deviceId(=BLE 地址) → DiscoveredDevice。ConcurrentHashMap 因扫描回调
     *  可能在 binder 线程上投递，过期清理在 mainHandler 上跑，两者都需要读写。 */
    private val devices: MutableMap<String, DiscoveredDevice> = ConcurrentHashMap()
    @Volatile private var selectedDeviceId: String? = null
    /** 本次 start 以来页面是否调用过 selectScaleDevice/clearScaleDevice。
     *  getScaleDevices 是纯查询，不算。一旦为 true 永久取消本次扫描的兜底。 */
    @Volatile private var devicesApiTouched = false
    @Volatile private var legacyFallbackDone = false

    // 定时器（用 mainHandler postDelayed，便于统一在主线程取消）。
    private var staleToken: Runnable? = null
    private var legacyFallbackToken: Runnable? = null
    private var expireSweepToken: Runnable? = null

    /** 发现表中单台设备。 */
    private data class DiscoveredDevice(
        val deviceId: String,
        val name: String,
        val rssi: Int,
        /** 最新解析的克数；无法解析时为 null。 */
        val grams: Double?,
        val lastSeenAtEpochMs: Long,
    )

    // ============================================================
    // 公开 API（由 ScaleJavascriptBridge / MainActivity 调用，必须在主线程）
    // ============================================================

    /** 网页请求开始扫描（幂等）。 */
    fun start() {
        if (started) return
        started = true
        // 序号与派生状态在每次 start 时清零（与 ScaleSourceBase.start 一致）。
        sequenceCounter = 0
        lastReadingAtMonotonicMs = null
        lastReadingAtEpochMs = null
        lastRaw = null
        consecutiveSameRaw = 0
        selectedDeviceId = null
        devicesApiTouched = false
        legacyFallbackDone = false
        devices.clear()
        transition("scanning", "正在扫描 K797 广播", force = true)
        onStart()
    }

    /** 网页请求停止扫描（幂等）。清表/清选择/清兜底定时器。 */
    fun stop() {
        if (!started) return
        started = false
        cancelStaleCheck()
        cancelLegacyFallback()
        cancelExpireSweep()
        onStop()
        devices.clear()
        selectedDeviceId = null
        devicesApiTouched = false
        legacyFallbackDone = false
        transition("off", "扫描已停止", force = true)
    }

    /** 查询当前状态 JSON（ScaleStatus 契约形状）。 */
    @Synchronized
    fun statusJson(): String = buildStatusJson()

    /** 查询发现表 JSON（miceautomatic:scale-devices 事件 detail 形状）。 */
    fun devicesJson(): String = buildDevicesJson()

    /**
     * 选定设备（deviceId 已由 Bridge 校验合法性）。选择时若表中没有该设备也接受
     * （等设备出现）。一旦调用，永久取消本次扫描的兜底。
     */
    fun selectScaleDevice(deviceId: String) {
        markDevicesApiTouched()
        selectedDeviceId = deviceId
        notifyDevicesChanged()
        val dev = devices[deviceId]
        if (dev != null) {
            transition("scanning", "已选择 ${dev.name}，等待读数", force = true)
        } else {
            transition("scanning", "已选择设备，等待出现", force = true)
        }
        Log.i(TAG, "selectScaleDevice: $deviceId")
    }

    /** 取消选择，回到纯发现模式。 */
    fun clearScaleDevice() {
        markDevicesApiTouched()
        selectedDeviceId = null
        notifyDevicesChanged()
        transition("scanning", "已取消选择，继续搜索", force = true)
        Log.i(TAG, "clearScaleDevice")
    }

    // ============================================================
    // 启动 / 停止扫描
    // ============================================================

    @SuppressLint("MissingPermission")
    private fun onStart() {
        // 蓝牙开关检查：未开则直接进 bluetooth_off，不创建扫描器。
        val adapter = manager?.adapter
        if (adapter == null) {
            reportError("手机没有可用的蓝牙适配器")
            return
        }
        if (!adapter.isEnabled) {
            reportBluetoothOff("系统蓝牙未开启")
            return
        }
        val scanner = adapter.bluetoothLeScanner
        if (scanner == null) {
            reportError("无法启动 BLE 扫描")
            return
        }

        // 带过滤扫描：name=K797 + manufacturerId=0x0000 + 前缀匹配（前缀作为
        // manufacturerData 的子集匹配，mask 全 1）。onScanResult 内仍二次校验名称/前缀。
        val filter = ScanFilter.Builder()
            .setDeviceName(DEVICE_NAME)
            .setManufacturerData(MANUFACTURER_ID, PREFIX, ByteArray(PREFIX.size) { 0xFF.toByte() })
            .build()
        // K797 不可连接广播设备，无 GATT/notify 通道，必须持续被动监听。
        // LOW_LATENCY 用最高占空比，毫秒级广播延迟最低；现场插电连续称量，实时性优先。
        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .setCallbackType(ScanSettings.CALLBACK_TYPE_ALL_MATCHES)
            .build()
        try {
            scanner.startScan(listOf(filter), settings, callback)
            scanning = true
            // 安排旧版兜底 + 周期清理过期设备。
            scheduleLegacyFallback()
            scheduleExpireSweep()
        } catch (error: SecurityException) {
            reportUnauthorized("蓝牙权限被拒：${error.message}")
        } catch (error: IllegalStateException) {
            reportError("启动 BLE 扫描失败：${error.message}")
        }
    }

    @SuppressLint("MissingPermission")
    private fun onStop() {
        if (scanning) {
            try {
                manager?.adapter?.bluetoothLeScanner?.stopScan(callback)
            } catch (_: SecurityException) {
                // 权限可能在后台被撤销，忽略停止失败。
            }
            scanning = false
        }
    }

    // ============================================================
    // 扫描结果处理（对齐 BleK797Source.processScanResult）
    // ============================================================

    private val callback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            processScanResult(result)
        }

        override fun onBatchScanResults(results: MutableList<ScanResult>?) {
            results?.forEach { processScanResult(it) }
        }

        override fun onScanFailed(errorCode: Int) {
            scanning = false
            reportError("BLE 扫描失败：errorCode=$errorCode")
        }
    }

    /**
     * 处理单条扫描结果：强类型读取字段，喂解析器、派发读数。
     * 无论是否已选定设备，都先解析并更新发现表；仅当选定设备匹配时才派发读数。
     */
    private fun processScanResult(r: ScanResult) {
        val record = r.scanRecord ?: return
        val localName = record.deviceName ?: ""
        // 设备主键（发现表用）：BLE 地址。不可连接广播的地址是随机私有地址，
        // 但同一次扫描会话内足以区分多台 K797；用作 deviceId 即可，绝不进读数。
        val deviceId = r.device?.address ?: return
        val rssi = r.rssi

        // 提取 K797 的 Manufacturer Data（key=MANUFACTURER_ID，value 不含 ID 自身）。
        val payload = record.getManufacturerSpecificData(MANUFACTURER_ID)
        val receivedEpochMs = System.currentTimeMillis()
        val receivedMonoMs = SystemClock.elapsedRealtime()

        val result = parse(localName, MANUFACTURER_ID, payload, rssi, receivedEpochMs)

        // 始终更新发现表（无论是否选定设备，无论解析是否成功）。
        val grams = result?.grams
        val tableChanged = updateDevice(deviceId, localName, rssi, grams, receivedEpochMs)

        // 选定设备过滤：仅匹配的包才走 handleParseResult（派发读数 + 转 active）。
        val selected = selectedDeviceId
        if (selected != null && selected == deviceId && result != null) {
            handleParseResult(result, receivedEpochMs, receivedMonoMs)
        } else if (selected == null) {
            // 纯发现模式：不派发读数，状态停留 scanning，message 随发现数更新。
            maintainDiscoveryMessage(tableChanged)
        }
        // selected != null && selected != deviceId：仅更新发现表，不派发读数。

        // 发现表有实质变化时通知订阅者（推 EVENT_DEVICES）。
        if (tableChanged) {
            notifyDevicesChanged()
        }
    }

    /**
     * 处理解析成功结果：派生 stable、分配 sequence、推送读数与 active 状态，
     * 并重排 stale 检查（对齐 ScaleSourceBase.handleParseResult）。
     */
    private fun handleParseResult(
        reading: ParsedReading,
        receivedAtEpochMs: Long,
        receivedAtMonotonicMs: Long,
    ) {
        // 派生 stable：连续相同 raw ≥ STABLE_MIN_REPEAT。
        val stable = deriveStable(reading.raw)
        sequenceCounter += 1
        val seq = sequenceCounter
        lastReadingAtMonotonicMs = receivedAtMonotonicMs
        lastReadingAtEpochMs = receivedAtEpochMs
        dispatchReading(reading, stable, seq)
        transition("active", "已收到 K797 广播")
        scheduleStaleCheck()
    }

    /** 派生 stable：连续相同 raw ≥ STABLE_MIN_REPEAT（raw 一变计数重置为 1）。 */
    @Synchronized
    private fun deriveStable(raw: Int): Boolean {
        if (lastRaw != null && lastRaw == raw) {
            consecutiveSameRaw += 1
        } else {
            consecutiveSameRaw = 1
        }
        lastRaw = raw
        return consecutiveSameRaw >= STABLE_MIN_REPEAT
    }

    // ============================================================
    // 发现表维护（对齐 BleK797Source.updateDevice / maintainDiscoveryMessage）
    // ============================================================

    /** 更新发现表中某设备；返回 true 表示有实质变化（新增/grams 变/rssi 变化≥5dB）。 */
    private fun updateDevice(
        deviceId: String,
        name: String,
        rssi: Int,
        grams: Double?,
        nowEpochMs: Long,
    ): Boolean {
        val prev = devices[deviceId]
        if (prev == null) {
            devices[deviceId] = DiscoveredDevice(deviceId, name, rssi, grams, nowEpochMs)
            return true
        }
        val rssiChanged = Math.abs(prev.rssi - rssi) >= 5
        val gramsChanged = !sameGrams(prev.grams, grams)
        // name 空串时保留旧值；grams 解析失败则保留旧值（契约要求保留旧值或 null）。
        val newName = if (name.isNotEmpty()) name else prev.name
        val newGrams = grams ?: prev.grams
        devices[deviceId] = DiscoveredDevice(deviceId, newName, rssi, newGrams, nowEpochMs)
        return rssiChanged || gramsChanged
    }

    /** 纯发现模式下根据发现数量刷新 message（仅当表变化时，避免无谓 transition）。 */
    private fun maintainDiscoveryMessage(tableChanged: Boolean) {
        if (!tableChanged) return
        val n = devices.size
        if (n == 0) return // 等下一次更新
        transition("scanning", "发现 $n 台天平，等待选择", force = true)
    }

    /** 判断两个 grams 是否"相等"（0.05g 内视为相同，避免浮点抖动频繁触发推送）。 */
    private fun sameGrams(a: Double?, b: Double?): Boolean {
        if (a == null && b == null) return true
        if (a == null || b == null) return false
        return Math.abs(a - b) < 0.05
    }

    // ============================================================
    // 旧版兜底（对齐 BleK797Source.scheduleLegacyFallback / runLegacyFallback）
    // ============================================================

    /** 标记页面已用 devices API（select/clear），永久取消本次扫描的旧版兜底。 */
    private fun markDevicesApiTouched() {
        devicesApiTouched = true
    }

    private fun scheduleLegacyFallback() {
        cancelLegacyFallback()
        val token = Runnable { runLegacyFallback() }
        legacyFallbackToken = token
        mainHandler.postDelayed(token, LEGACY_FALLBACK_DELAY_MS)
    }

    private fun cancelLegacyFallback() {
        legacyFallbackToken?.let { mainHandler.removeCallbacks(it) }
        legacyFallbackToken = null
    }

    private fun runLegacyFallback() {
        legacyFallbackToken = null
        // 已用新 API / 已做过兜底 / 已有选择 / 已停 → 跳过。
        if (!started) return
        if (devicesApiTouched || legacyFallbackDone) return
        if (selectedDeviceId != null) return
        if (devices.isEmpty()) {
            // 4s 内没发现任何设备，重排一次再等（直到页面用 API 或 stop）。
            scheduleLegacyFallback()
            return
        }
        // 选 rssi 最强（数值最大）的设备。
        var best: DiscoveredDevice? = null
        devices.values.forEach { d ->
            if (best == null || d.rssi > best!!.rssi) best = d
        }
        val target = best ?: return
        legacyFallbackDone = true
        Log.i(
            TAG,
            "auto-select legacy fallback: deviceId=${target.deviceId} rssi=${target.rssi}",
        )
        // 直接选定（不走 markDevicesApiTouched，否则自相矛盾）。
        selectedDeviceId = target.deviceId
        notifyDevicesChanged()
        transition("scanning", "已选择 ${target.name}，等待读数", force = true)
    }

    // ============================================================
    // 过期清理（对齐 BleK797Source.scheduleExpireSweep / expireSweep）
    // ============================================================

    private fun scheduleExpireSweep() {
        cancelExpireSweep()
        val token = Runnable { expireSweep() }
        expireSweepToken = token
        mainHandler.postDelayed(token, EXPIRE_SWEEP_MS.toLong())
    }

    private fun cancelExpireSweep() {
        expireSweepToken?.let { mainHandler.removeCallbacks(it) }
        expireSweepToken = null
    }

    private fun expireSweep() {
        expireSweepToken = null
        if (!started) return
        val now = System.currentTimeMillis()
        val toRemove = devices.values.filter { now - it.lastSeenAtEpochMs > DEVICE_EXPIRE_MS }
        var changed = false
        for (d in toRemove) {
            // 选定的设备即使过期也不从表中移除（避免选择闪烁）。
            if (d.deviceId == selectedDeviceId) continue
            devices.remove(d.deviceId)
            changed = true
        }
        if (changed) notifyDevicesChanged()
        // 继续下一轮清理（只要还在扫描）。
        if (state == "scanning" || state == "active" || state == "stale") {
            scheduleExpireSweep()
        }
    }

    // ============================================================
    // stale 检查（对齐 ScaleSourceBase.scheduleStaleCheck / checkStale）
    // ============================================================

    private fun scheduleStaleCheck() {
        cancelStaleCheck()
        val token = Runnable { checkStale() }
        staleToken = token
        mainHandler.postDelayed(token, STALE_THRESHOLD_MS)
    }

    private fun cancelStaleCheck() {
        staleToken?.let { mainHandler.removeCallbacks(it) }
        staleToken = null
    }

    private fun checkStale() {
        staleToken = null
        if (!started) return
        if (state != "active" && state != "scanning") return
        val lastMono = lastReadingAtMonotonicMs
        if (lastMono == null) {
            // scanning 状态下还没收到包，继续保持 scanning，不进入 stale。
            return
        }
        val elapsed = SystemClock.elapsedRealtime() - lastMono
        if (elapsed >= STALE_THRESHOLD_MS) {
            // stale 绝不产生 0g 读数：这里只切状态，不派发任何读数。
            transition("stale", "超过 15 秒未收到合法 K797 广播")
        } else {
            // 还没到阈值，继续等待剩余时间。
            val remain = STALE_THRESHOLD_MS - elapsed
            val token = Runnable { checkStale() }
            staleToken = token
            mainHandler.postDelayed(token, remain)
        }
    }

    // ============================================================
    // 状态转移 / 派发（对齐 ScaleSourceBase.transition / dispatch*）
    // ============================================================

    /** 由子类在权限被拒时调用。 */
    private fun reportUnauthorized(message: String) = transition("unauthorized", message, force = true)
    /** 由子类在蓝牙关闭时调用。 */
    private fun reportBluetoothOff(message: String) = transition("bluetooth_off", message, force = true)
    /** 由子类在扫描失败时调用。 */
    private fun reportError(message: String) = transition("error", message, force = true)

    /**
     * 状态转移；仅当内容（state/message/lastReadingAtEpochMs/selectedDeviceId）变化
     * 或强制刷新时回调。对齐 ScaleWebBridge.pushStatus 的"内容变化才推"。
     */
    @Synchronized
    private fun transition(newState: String, newMessage: String, force: Boolean = false) {
        if (!force && state == newState) {
            // 同状态不重复触发；active 每包都刷新也无妨（内容变化检测会去重）。
            return
        }
        state = newState
        message = newMessage
        listener.onScaleStatus(buildStatusJson())
    }

    /** 通知发现表变化（宿主订阅后节流推 EVENT_DEVICES）。 */
    private fun notifyDevicesChanged() {
        listener.onScaleDevices(buildDevicesJson())
    }

    /** 派发一条读数 JSON（对齐 ScaleReading.toJson 字段顺序与格式）。 */
    private fun dispatchReading(reading: ParsedReading, stable: Boolean, sequence: Int) {
        listener.onScaleReading(reading.toJson(stable, sequence))
    }

    // ============================================================
    // JSON 构造（对齐 ScaleReading.toJson / ScaleStatus.toJson / buildDevicesJson）
    // ============================================================

    @Synchronized
    private fun buildStatusJson(): String {
        val last = lastReadingAtEpochMs
        val sel = selectedDeviceId
        val sb = StringBuilder("{")
        sb.append("\"device\":\"").append(DEVICE_NAME).append('"')
        sb.append(",\"state\":\"").append(escapeJson(state)).append('"')
        sb.append(",\"message\":\"").append(escapeJson(message)).append('"')
        sb.append(",\"lastReadingAtEpochMs\":").append(if (last == null) "null" else last.toString())
        sb.append(",\"source\":\"ble\"")
        sb.append(",\"selectedDeviceId\":").append(if (sel == null) "null" else "\"" + escapeJson(sel) + "\"")
        sb.append('}')
        return sb.toString()
    }

    private fun buildDevicesJson(): String {
        val scanningState = state == "scanning" || state == "active" || state == "stale"
        val sel = selectedDeviceId
        val sb = StringBuilder("{\"devices\":[")
        var first = true
        // 按 deviceId 排序保证 JSON 稳定（便于节流的内容变化检测）。
        val sortedIds = devices.keys.sorted()
        for (id in sortedIds) {
            val d = devices[id] ?: continue
            if (!first) sb.append(',')
            first = false
            sb.append("{\"deviceId\":\"").append(escapeJson(d.deviceId)).append('"')
            sb.append(",\"name\":\"").append(escapeJson(d.name)).append('"')
            sb.append(",\"rssi\":").append(d.rssi)
            sb.append(",\"grams\":").append(if (d.grams == null) "null" else formatGrams(d.grams))
            sb.append(",\"lastSeenAtEpochMs\":").append(d.lastSeenAtEpochMs)
            sb.append('}')
        }
        sb.append("],\"scanning\":").append(scanningState)
        sb.append(",\"selectedDeviceId\":").append(if (sel == null) "null" else "\"" + escapeJson(sel) + "\"")
        sb.append('}')
        return sb.toString()
    }

    // ============================================================
    // 解析器（对齐 K797AdvertisementParser.parse）
    // ============================================================

    /** 解析成功的中间结果（不含 stable/sequence，由 base 分配）。 */
    private class ParsedReading(
        val grams: Double,
        val raw: Int,
        val rssi: Int,
        val receivedAtEpochMs: Long,
        val payloadHex: String,
    ) {
        /** 序列化为读数 JSON（字段顺序与 ScaleReading.toJson 逐字一致）。 */
        fun toJson(stable: Boolean, sequence: Int): String {
            val sb = StringBuilder("{")
            sb.append("\"schemaVersion\":1")
            sb.append(",\"device\":\"").append(DEVICE_NAME).append('"')
            sb.append(",\"deviceKey\":\"").append(DEVICE_KEY).append('"')
            sb.append(",\"grams\":").append(formatGrams(grams))
            sb.append(",\"raw\":").append(raw)
            sb.append(",\"rssi\":").append(rssi)
            sb.append(",\"receivedAt\":\"").append(toIsoUtc(receivedAtEpochMs)).append('"')
            sb.append(",\"receivedAtEpochMs\":").append(receivedAtEpochMs)
            sb.append(",\"sequence\":").append(sequence)
            sb.append(",\"stable\":").append(stable)
            // stableSource：stable=true 时为 "derived_repeat"，否则 null。
            sb.append(",\"stableSource\":").append(if (stable) "\"derived_repeat\"" else "null")
            sb.append(",\"source\":\"ble\"")
            sb.append(",\"address\":\"diagnostic-only\"")
            sb.append(",\"payloadHex\":\"").append(payloadHex).append('"')
            sb.append('}')
            return sb.toString()
        }
    }

    /**
     * 纯函数解析入口（对齐 K797AdvertisementParser.parse）。命中任一拒绝条件返回 null。
     */
    private fun parse(
        localName: String,
        manufacturerId: Int,
        payload: ByteArray?,
        rssi: Int,
        receivedAtEpochMs: Long,
    ): ParsedReading? {
        // 1. Local Name
        if (localName != DEVICE_NAME) return null
        // 2. Manufacturer ID（Android getManufacturerSpecificData 已按键取值，此处恒等）
        if (manufacturerId != MANUFACTURER_ID) return null
        // 3. 长度
        if (payload == null || payload.size < MIN_PAYLOAD_BYTES) return null
        // 4. 前 9 字节前缀（常量时间比较，不提前短路）
        var prefixMatch = 0
        for (i in PREFIX.indices) {
            prefixMatch = prefixMatch or ((payload[i].toInt() xor PREFIX[i].toInt()))
        }
        if (prefixMatch != 0) return null
        // 5. 重量（小端）
        val raw = (payload[9].toInt() and 0xFF) or ((payload[10].toInt() and 0xFF) shl 8)
        val grams = raw / 10.0
        if (grams < 0 || grams > MAX_GRAMS || !grams.isFinite()) return null
        // 6. 组装
        val payloadHex = toHexSpaces(payload)
        return ParsedReading(grams, raw, rssi, receivedAtEpochMs, payloadHex)
    }

    // ============================================================
    // 工具函数（对齐 K797AdvertisementParser.toHexSpaces / toIsoUtc / formatNumber）
    // ============================================================

    /** 把 payload 转成空格分隔大写十六进制，例：CA E8 03 ... */
    private fun toHexSpaces(bytes: ByteArray): String {
        val sb = StringBuilder(bytes.size * 3)
        for (i in bytes.indices) {
            if (i > 0) sb.append(' ')
            sb.append("%02X".format(bytes[i].toInt() and 0xFF))
        }
        return sb.toString()
    }

    /** 极简 JSON 字符串转义（与 ScaleStatus.ets 的 escapeJson 风格一致）。 */
    private fun escapeJson(value: String): String {
        val sb = StringBuilder(value.length)
        for (ch in value) {
            when (ch) {
                '"' -> sb.append("\\\"")
                '\\' -> sb.append("\\\\")
                '\n' -> sb.append("\\n")
                '\r' -> sb.append("\\r")
                '\t' -> sb.append("\\t")
                else -> sb.append(ch)
            }
        }
        return sb.toString()
    }
}
