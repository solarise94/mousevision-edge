package com.pingoodmice.miceautomatic

import android.annotation.SuppressLint
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
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
 * 真实 BLE 扫描读数源（Android），针对不可连接的电子秤广播。
 *
 * **配置驱动**：设备识别/解析规则不再硬编码，由构造期注入的 [profiles]（来自
 * assets/scale_profiles.json，经 [ScaleProfileRegistry] 解码）决定。每条广播按 profiles
 * 顺序找**首个命中**的 profile 进入发现表与重量解析（详见 [processScanResult]）。
 *
 * 与 HarmonyOS 侧 `BleK797Source.ets` / `ScaleSource.ets` / `ScaleStatus.ets` /
 * `ScaleReading.ets` 逐条对齐（见各方法注释）。协议常量（签名/偏移/换算）全部移至
 * ScaleProfile，本类不再持有任何设备特定常量。
 *
 * 关键契约（必须逐字一致）：
 * - 仅扫描广播，绝不调用 GATT connect，绝不要求配对；
 * - 广播秤用 ADV_NONCONN_IND，只能被动扫描，必须 SCAN_MODE_LOW_LATENCY 持续监听；
 * - 身份 = 命中的 profile（含签名/manufacturer id）+ 可选 Local Name 过滤；
 * - 真实 BLE 地址是随机私有地址，**不进读数**（读数 address 固定 "diagnostic-only"），
 *   地址仅作发现表的 deviceId；稳定的跨重启身份用 deviceKey（含序列号）；
 * - sequence 进程内单调递增，每条派发的读数 +1（H5 靠它去重）；start **不清零**，
 *   仅进程重启时归零（有意偏离 ScaleSourceBase.start 的对齐规则，见下方"有意差异点"）；
 * - stable 派生：连续相同 raw ≥3（raw 一变计数重置为 1）；
 * - stale：单调时钟 elapsedRealtime，>15s 无合法包；stale 绝不产生 0g 读数；
 *   scanning 未收过包不进 stale；
 * - 设备发现/选择：未选定=纯发现模式（不派发读数，状态停 scanning）；选定即仅该设备
 *   的包派发读数；首个匹配读数 → active；
 * - 旧版兜底：start 4s 后若从未调用过 selectScaleDevice/clearScaleDevice 且无选择且
 *   发现表非空 → 自动选 rssi 最强；4s 时表为空则每 4s 重试直到页面用 API 或 stop；
 *   getScaleDevices 是纯查询，**不算**触发兜底取消。
 *
 * 与 HarmonyOS 侧的有意差异点：
 * - selectedDeviceId 跨 stop/start 保留（仅 clearScaleDevice 清除）。原因：Android
 *   侧 Activity onPause 会停扫描省电，onResume 再 start；若每次 start 都清选择，回
 *   前台后会丢秤、H5 通道误以为还连着。鸿蒙侧 start 清选择，Android 侧保留，系有意
 *   偏离 ScaleSourceBase.start 的对齐规则。
 * - sequence 跨 stop/start 不清零（进程内单调递增，仅进程重启归零）。原因同上：
 *   weigh-engine 内部有 `r.sequence <= lastSequence` 的去重，若 start 清零，onPause→
 *   stop、onResume→start 后序号从 0 重计，H5 会拒绝所有低序号读数，自动判稳长期失效。
 *   鸿蒙侧 start 清零，Android 侧因前后台切换频繁改为不清零，系有意偏离
 *   ScaleSourceBase.start 的对齐规则。
 *
 * @param profiles 设备签名表（由 MainActivity 从 assets 加载注入）。为空表示配置加载
 *   失败，[start] 将不启动扫描并直接 transition 到 error。
 */
class K797BleScanner(
    context: Context,
    private val listener: Listener,
    private val profiles: List<ScaleProfile>,
) {
    /** 宿主回调：派发读数 / 状态 / 设备发现表 JSON（均为契约 JSON 字符串）。 */
    interface Listener {
        fun onScaleReading(json: String)
        fun onScaleStatus(json: String)
        fun onScaleDevices(json: String)
    }

    companion object {
        private const val TAG = "MiceAutomaticScale"

        /** 把 epoch 毫秒格式化为 ISO8601 UTC（Z 后缀）。Android Instant.toString 已是
         *  ISO-8601 UTC（如 2026-08-03T12:34:56.789Z），满足 receivedAt 契约。
         *  放 companion 以便读数 JSON 构造调用。 */
        private fun toIsoUtc(epochMs: Long): String = Instant.ofEpochMilli(epochMs).toString()

        /** 把克数格式化为协议 JSON：整数带一位小数（如 250.0），非整数最多一位。
         *  放 companion 以便读数 JSON 构造调用。 */
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
    // sequenceCounter 进程内单调递增，start 不清零（仅进程重启归零）。
    private var sequenceCounter = 0
    private var lastReadingAtEpochMs: Long? = null
    private var lastReadingAtMonotonicMs: Long? = null
    private var lastRaw: Int? = null
    private var consecutiveSameRaw = 0

    // ---------- 设备发现 / 选择 ----------
    /** 发现表：deviceId(=BLE 地址) → DiscoveredDevice。ConcurrentHashMap 因扫描回调
     *  可能在 binder 线程上投递，过期清理在 mainHandler 上跑，两者都需要读写。 */
    private val devices: MutableMap<String, DiscoveredDevice> = ConcurrentHashMap()
    /** 选中的设备 deviceId（=BLE 地址）。跨 stop/start 保留（仅 clearScaleDevice 清除），
     *  以支持 onPause/onResume 后台恢复后选秤不丢；详见类头注释的有意差异点。 */
    @Volatile private var selectedDeviceId: String? = null
    /** 本次 start 以来页面是否调用过 selectScaleDevice/clearScaleDevice。
     *  getScaleDevices 是纯查询，不算。一旦为 true 永久取消本次扫描的兜底。
     *  stop/start 会重置（与 selectedDeviceId 的保留策略不同）。 */
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
        /** 该设备命中的 profile 显示名（写入状态/读数 JSON 的 device 字段）。 */
        val displayName: String,
        /** 该设备命中的 profile 算出的稳定身份（写入读数 JSON 的 deviceKey 字段）。 */
        val deviceKey: String,
        val rssi: Int,
        /** 最新解析的克数；无法解析时为 null。 */
        val grams: Double?,
        val lastSeenAtEpochMs: Long,
    )

    // ============================================================
    // 公开 API（由 ScaleJavascriptBridge / MainActivity 调用，必须在主线程）
    // ============================================================

    /** 网页请求开始扫描（幂等）。
     *
     * 注意（两处与 HarmonyOS 侧 ScaleSourceBase.start 的有意差异）：
     * - selectedDeviceId 跨 stop/start 保留（仅 [clearScaleDevice] 清除），以支持
     *   Activity onPause/onResume 后台恢复后选秤不丢。
     * - sequenceCounter **不清零**：进程内单调递增，仅进程重启归零。原因：weigh-engine
     *   内部有 `r.sequence <= lastSequence` 去重，若每次 start 清零，onPause→stop、
     *   onResume→start 后序号从 0 重计，H5 会拒绝所有低序号读数，自动判稳长期失效。
     *   stable 派生状态（lastRaw / consecutiveSameRaw）仍随 start 重置：它们与序号
     *   无关，重置只是让"连续相同"计数从新会话重新累计，不影响已派发读数的去重。
     *
     * 配置缺失保护：profiles 为空（assets 加载失败）时不启动扫描，立即 transition
     * 到 error，message="未加载到设备配置"。 */
    fun start() {
        // 配置缺失：不启动 BLE 扫描，立即报错（H5 可据此提示用户检查打包/更新）。
        if (profiles.isEmpty()) {
            transition("error", "未加载到设备配置", force = true)
            return
        }
        if (started) return
        started = true
        // sequenceCounter 不清零（见方法注释：进程内单调，防 stop/start 后序号倒退）；
        // stable 派生状态重置（与序号无关，仅重置"连续相同"累计）。
        lastReadingAtMonotonicMs = null
        lastReadingAtEpochMs = null
        lastRaw = null
        consecutiveSameRaw = 0
        // selectedDeviceId 保留（见方法注释）；发现表与兜底标记重置。
        devicesApiTouched = false
        legacyFallbackDone = false
        devices.clear()
        transition("scanning", "正在扫描电子秤广播", force = true)
        onStart()
    }

    /** 网页请求停止扫描（幂等）。清表/清兜底定时器。
     *
     * selectedDeviceId 保留（仅 [clearScaleDevice] 清除）：onPause 调本方法后，
     * onResume 恢复扫描时选择仍有效。 */
    fun stop() {
        if (!started) return
        started = false
        cancelStaleCheck()
        cancelLegacyFallback()
        cancelExpireSweep()
        onStop()
        devices.clear()
        // selectedDeviceId 保留：跨 stop/start，支持 onPause/onResume 后台恢复。
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
            // name 为空（设备未广播 Local Name）时回退 displayName，避免提示空白设备名。
            transition("scanning", "已选择 ${dev.name.ifBlank { dev.displayName }}，等待读数", force = true)
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

        // 无过滤扫描：设备识别全部交给 profiles 配置（签名/manufacturer id 可能多型号）。
        // ScanFilter 无法表达"签名带 mask"且多 profile 组合，索性全收、在回调内逐条匹配，
        // 未命中任何 profile 的广播不进发现表（保持发现表只显示可识别设备）。
        // 不可连接广播设备，无 GATT/notify 通道，必须持续被动监听。
        // LOW_LATENCY 用最高占空比，毫秒级广播延迟最低；现场插电连续称量，实时性优先。
        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .setCallbackType(ScanSettings.CALLBACK_TYPE_ALL_MATCHES)
            .build()
        try {
            scanner.startScan(emptyList(), settings, callback)
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
     * 处理单条扫描结果：按 profiles 配置顺序找首个命中的 profile，喂解析器、派发读数。
     *
     * 流程：
     * 1. 按 profiles 顺序，对每个 profile 取其 manufacturerId 对应的 payload，
     *    用 [ScaleProfileRegistry.matches] 判断是否命中；首个命中即采用（firstNotNullOfOrNull）。
     * 2. 未命中任何 profile → 直接 return，**不进发现表**（发现表只收可识别设备）。
     * 3. 命中：用该 profile 的 [ScaleProfileRegistry.parseAdvertisement] 解析重量，
     *    [ScaleProfileRegistry.buildDeviceKey] 算稳定身份，更新发现表；
     *    仅当选定设备匹配时才派发读数（与原逻辑一致）。
     *
     * deviceId 继续用 BLE 地址（r.device?.address）；deviceKey 用 profile 配的序列号字段，
     * 跨重启稳定（BLE 随机地址会轮换，不能直接当身份）。
     */
    private fun processScanResult(r: ScanResult) {
        val record = r.scanRecord ?: return
        // 设备主键（发现表用）：BLE 地址。不可连接广播的地址是随机私有地址，
        // 但同一次扫描会话内足以区分多台秤；用作 deviceId 即可，绝不进读数。
        val deviceId = r.device?.address ?: return
        val rssi = r.rssi
        val receivedEpochMs = System.currentTimeMillis()
        val receivedMonoMs = SystemClock.elapsedRealtime()

        // 按 profiles 配置顺序找首个命中：返回 (profile, payload)。
        val hit = profiles.firstNotNullOfOrNull { profile ->
            val payload = record.getManufacturerSpecificData(profile.manufacturerId)
                ?: return@firstNotNullOfOrNull null
            if (ScaleProfileRegistry.matches(
                    profile,
                    profile.manufacturerId,
                    payload,
                    record.deviceName,
                )
            ) {
                profile to payload
            } else {
                null
            }
        }
        // 未命中任何 profile：保持发现表只显示可识别设备，直接丢弃。
        if (hit == null) return
        val (profile, payload) = hit

        // 命中后解析重量；buildDeviceKey 用 payload 内的序列号字段（稳定身份）。
        val result = ScaleProfileRegistry.parseAdvertisement(profile, payload, rssi, receivedEpochMs)
        val deviceKey = ScaleProfileRegistry.buildDeviceKey(profile, payload, deviceId)
        val displayName = profile.displayName
        val localName = record.deviceName ?: ""

        // 始终更新发现表（无论是否选定设备，无论解析是否成功）。
        val grams = result?.grams
        val tableChanged = updateDevice(
            deviceId = deviceId,
            name = localName,
            displayName = displayName,
            deviceKey = deviceKey,
            rssi = rssi,
            grams = grams,
            receivedEpochMs = receivedEpochMs,
        )

        // 选定设备过滤：仅匹配的包才走 handleParseResult（派发读数 + 转 active）。
        val selected = selectedDeviceId
        if (selected != null && selected == deviceId && result != null) {
            handleParseResult(result, displayName, deviceKey, receivedEpochMs, receivedMonoMs)
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
     *
     * @param displayName 写入读数 JSON 的 device 字段（命中 profile 的 displayName）。
     * @param deviceKey 写入读数 JSON 的 deviceKey 字段（profile 算出的稳定身份）。
     */
    private fun handleParseResult(
        reading: ParsedReading,
        displayName: String,
        deviceKey: String,
        receivedAtEpochMs: Long,
        receivedAtMonotonicMs: Long,
    ) {
        // 派生 stable：连续相同 raw ≥ STABLE_MIN_REPEAT。
        val stable = deriveStable(reading.raw)
        sequenceCounter += 1
        val seq = sequenceCounter
        lastReadingAtMonotonicMs = receivedAtMonotonicMs
        lastReadingAtEpochMs = receivedAtEpochMs
        dispatchReading(reading, displayName, deviceKey, stable, seq)
        transition("active", "已收到电子秤广播")
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
        displayName: String,
        deviceKey: String,
        rssi: Int,
        grams: Double?,
        receivedEpochMs: Long,
    ): Boolean {
        val prev = devices[deviceId]
        if (prev == null) {
            devices[deviceId] = DiscoveredDevice(
                deviceId = deviceId,
                name = name,
                displayName = displayName,
                deviceKey = deviceKey,
                rssi = rssi,
                grams = grams,
                lastSeenAtEpochMs = receivedEpochMs,
            )
            return true
        }
        val rssiChanged = Math.abs(prev.rssi - rssi) >= 5
        val gramsChanged = !sameGrams(prev.grams, grams)
        // name 空串时保留旧值；grams 解析失败则保留旧值（契约要求保留旧值或 null）。
        val newName = if (name.isNotEmpty()) name else prev.name
        val newGrams = grams ?: prev.grams
        devices[deviceId] = DiscoveredDevice(
            deviceId = deviceId,
            name = newName,
            displayName = displayName,
            deviceKey = deviceKey,
            rssi = rssi,
            grams = newGrams,
            lastSeenAtEpochMs = receivedEpochMs,
        )
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
        // 选 rssi 最强（数值最大）的设备。发现表本身只收命中 profile 的设备，
        // 所以兜底天然不会绕过配置表。
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
        // name 为空时回退 displayName，避免提示空白设备名（与 selectScaleDevice 一致）。
        transition("scanning", "已选择 ${target.name.ifBlank { target.displayName }}，等待读数", force = true)
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
            transition("stale", "超过 15 秒未收到合法电子秤广播")
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
    private fun dispatchReading(
        reading: ParsedReading,
        displayName: String,
        deviceKey: String,
        stable: Boolean,
        sequence: Int,
    ) {
        listener.onScaleReading(readingToJson(reading, displayName, deviceKey, stable, sequence))
    }

    // ============================================================
    // JSON 构造（对齐 ScaleReading.toJson / ScaleStatus.toJson / buildDevicesJson）
    // ============================================================

    @Synchronized
    private fun buildStatusJson(): String {
        val last = lastReadingAtEpochMs
        val sel = selectedDeviceId
        val device = if (devices.isEmpty()) {
            // 发现表为空（未识别到任何秤）时 device 用通用占位，避免误显示某型号。
            "天平"
        } else {
            // 取发现表中任意一台的 displayName（同型号场景下一致；多型号混合时取排序首项）。
            devices[devices.keys.min()]?.displayName ?: "天平"
        }
        val sb = StringBuilder("{")
        sb.append("\"device\":\"").append(escapeJson(device)).append('"')
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
            // name 回退到 displayName：deviceNameFilter=null（如内置 k797）时，部分设备
            // 广播不带 Local Name → record.deviceName 为空串，原样发给 H5 会让选择页把
            // 空设备名当标题显示（空白）。这里用 displayName 兜底，保证标题非空。
            val name = d.name.ifBlank { d.displayName }
            sb.append("{\"deviceId\":\"").append(escapeJson(d.deviceId)).append('"')
            sb.append(",\"name\":\"").append(escapeJson(name)).append('"')
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
    // 读数 JSON 构造（对齐 ScaleReading.toJson 字段顺序与格式）
    // ============================================================

    /**
     * 把解析结果组装成读数 JSON（字段顺序与 ScaleReading.toJson 逐字一致）。
     *
     * device/deviceKey 由命中 profile 决定（不再用硬编码常量）；其余字段
     * （grams/raw/rssi/receivedAt/sequence/stable/source/address/payloadHex）保持原格式，
     * H5 靠 sequence 去重。
     */
    private fun readingToJson(
        reading: ParsedReading,
        displayName: String,
        deviceKey: String,
        stable: Boolean,
        sequence: Int,
    ): String {
        val sb = StringBuilder("{")
        sb.append("\"schemaVersion\":1")
        sb.append(",\"device\":\"").append(escapeJson(displayName)).append('"')
        sb.append(",\"deviceKey\":\"").append(escapeJson(deviceKey)).append('"')
        sb.append(",\"grams\":").append(formatGrams(reading.grams))
        sb.append(",\"raw\":").append(reading.raw)
        sb.append(",\"rssi\":").append(reading.rssi)
        sb.append(",\"receivedAt\":\"").append(toIsoUtc(reading.receivedAtEpochMs)).append('"')
        sb.append(",\"receivedAtEpochMs\":").append(reading.receivedAtEpochMs)
        sb.append(",\"sequence\":").append(sequence)
        sb.append(",\"stable\":").append(stable)
        // stableSource：stable=true 时为 "derived_repeat"，否则 null。
        sb.append(",\"stableSource\":").append(if (stable) "\"derived_repeat\"" else "null")
        sb.append(",\"source\":\"ble\"")
        sb.append(",\"address\":\"diagnostic-only\"")
        sb.append(",\"payloadHex\":\"").append(escapeJson(reading.payloadHex)).append('"')
        sb.append('}')
        return sb.toString()
    }

    // ============================================================
    // 工具函数（对齐 ScaleStatus.ets 的 escapeJson 风格）
    // ============================================================

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
