package com.pingoodmice.miceautomatic

import android.content.Context
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.IOException
import java.io.InputStreamReader

/**
 * ScaleProfile.kt
 *
 * 设备签名表驱动：把"哪台电子秤怎么识别/解析"从硬编码抽到 assets/scale_profiles.json。
 *
 * 设计目标：
 * - 匹配与解析逻辑是**纯 Kotlin**（[matches] / [parseAdvertisement] / [buildDeviceKey]
 *   不碰 org.json、不碰 Android Context），可在普通 JVM 单测中直接构造 [ScaleProfile]
 *   调用验证；
 * - JSON 解析与 Asset I/O 单独放在 [ScaleProfileRegistry.load] / [parseProfiles]，
 *   仅这一处依赖 org.json 与 [Context.assets]（单测对 [parseProfiles] 用 JVM 版 org.json
 *   覆盖，对匹配/解析用直接构造的 profile 覆盖，互不耦合）。
 *
 * 全量失败策略：配置中任一 profile 非法 → 整体 [ProfileLoadResult.profiles] 为空、
 * [ProfileLoadResult.error] 指明具体 profile id + 字段名。K797BleScanner 在 profiles 为空时
 * 不启动 BLE 扫描、立即 transition 到 error 状态（"未加载到设备配置"）。
 */

/**
 * 一台电子秤的解码后配置（signature / signatureMask 为已解码 [ByteArray]）。
 *
 * @property id 配置项唯一标识（如 "k797"），用于 deviceKey 前缀与日志。
 * @property displayName 显示名（写入读数 JSON 的 `device` 字段与发现表）。
 * @property manufacturerId BLE Manufacturer Specific Data 的 key（0..65535）。
 *   注意：[android.bluetooth.le.ScanRecord.getManufacturerSpecificData] 返回的 payload
 *   **不含**这 2 字节 id 本身，因此 [signatureOffset] 从 payload 第 0 字节算起。
 * @property signature 签名字节序列（与 mask 等长）。
 * @property signatureOffset 签名在 payload 中的起始偏移。
 * @property signatureMask 逐字节掩码：0xFF 参与比较、0x00 忽略，支持位级 mask。
 * @property minPayloadBytes payload 最短长度（含签名、重量、identity 全部范围）。
 * @property identityOffset 身份字节（序列号）起始偏移；null 表示该 profile 不提取身份。
 * @property identityLength 身份字节长度；null 表示该 profile 不提取身份。
 *   两者必须同时非空或同时为 null。
 * @property weightOffset 重量 2 字节在 payload 中的起始偏移。
 * @property weightLittleEndian true=小端（低字节在前），false=大端。
 * @property weightDivisor 重量换算除数（grams = raw / weightDivisor）。
 * @property maxGrams 合法克数上限（含），超出视为超量程拒绝。
 * @property deviceNameFilter null=不检查 Local Name；非空=对广播内 Local Name
 *   （ScanRecord.deviceName）做忽略大小写的完整匹配。
 */
data class ScaleProfile(
    val id: String,
    val displayName: String,
    val manufacturerId: Int,
    val signature: ByteArray,
    val signatureOffset: Int,
    val signatureMask: ByteArray,
    val minPayloadBytes: Int,
    val identityOffset: Int?,
    val identityLength: Int?,
    val weightOffset: Int,
    val weightLittleEndian: Boolean,
    val weightDivisor: Double,
    val maxGrams: Double,
    val deviceNameFilter: String?,
) {
    /** data class 自动生成的 equals/hashCode 对 ByteArray 比较的是引用而非内容，
     *  这里按内容重写，便于单测构造与去重判断。 */
    override fun equals(other: Any?): Boolean {
        if (this === other) return true
        if (other !is ScaleProfile) return false
        return id == other.id &&
            displayName == other.displayName &&
            manufacturerId == other.manufacturerId &&
            signature.contentEquals(other.signature) &&
            signatureOffset == other.signatureOffset &&
            signatureMask.contentEquals(other.signatureMask) &&
            minPayloadBytes == other.minPayloadBytes &&
            identityOffset == other.identityOffset &&
            identityLength == other.identityLength &&
            weightOffset == other.weightOffset &&
            weightLittleEndian == other.weightLittleEndian &&
            weightDivisor == other.weightDivisor &&
            maxGrams == other.maxGrams &&
            deviceNameFilter == other.deviceNameFilter
    }

    override fun hashCode(): Int {
        var result = id.hashCode()
        result = 31 * result + displayName.hashCode()
        result = 31 * result + manufacturerId
        result = 31 * result + signature.contentHashCode()
        result = 31 * result + signatureOffset
        result = 31 * result + signatureMask.contentHashCode()
        result = 31 * result + minPayloadBytes
        result = 31 * result + (identityOffset ?: 0)
        result = 31 * result + (identityLength ?: 0)
        result = 31 * result + weightOffset
        result = 31 * result + weightLittleEndian.hashCode()
        result = 31 * result + weightDivisor.hashCode()
        result = 31 * result + maxGrams.hashCode()
        result = 31 * result + (deviceNameFilter?.hashCode() ?: 0)
        return result
    }
}

/**
 * 配置加载结果。
 *
 * @property profiles 解码后的 profile 列表；任一非法或 IO 失败时为空。
 * @property error 失败原因（null = 全部成功）。H5 端不消费此字段，
 *  仅供日志/状态判断（K797BleScanner.start 在 profiles 为空时进入 error 状态）。
 */
data class ProfileLoadResult(
    val profiles: List<ScaleProfile>,
    val error: String?,
)

/**
 * 解析成功的中间结果（不含 stable / sequence，由 K797BleScanner 分配）。
 * 纯数据类，不碰 org.json，便于 JVM 单测构造。
 */
data class ParsedReading(
    val grams: Double,
    val raw: Int,
    val rssi: Int,
    val receivedAtEpochMs: Long,
    val payloadHex: String,
)

/**
 * profile 加载与匹配/解析的统一入口。
 *
 * 线程模型：[load] 做一次 Asset 读 + 解析（onCreate 主线程同步调用，阻塞可忽略）。
 * [matches] / [parseAdvertisement] / [buildDeviceKey] 均为无状态纯函数，
 * 可在任意线程调用（BLE 扫描回调可能在 binder 线程）。
 */
object ScaleProfileRegistry {
    private const val TAG = "ScaleProfileRegistry"
    private const val ASSET_NAME = "scale_profiles.json"

    /**
     * 从 [context].assets 读取 scale_profiles.json 并解析。
     *
     * 读不到 / IO 失败 / 解析失败均返回空 profiles + 非空 error（**不抛异常**），
     * 让调用方（MainActivity）安全地走 error 状态。
     *
     * Asset 读取模式参考 [TlsPinValidator.readAsset]：UTF-8 文本一次性读出。
     */
    fun load(context: Context): ProfileLoadResult {
        val text = try {
            context.assets.open(ASSET_NAME).use { stream ->
                BufferedReader(InputStreamReader(stream, Charsets.UTF_8)).readText()
            }
        } catch (e: IOException) {
            Log.e(TAG, "读取 $ASSET_NAME 失败: ${e.message}")
            return ProfileLoadResult(emptyList(), "无法读取 $ASSET_NAME: ${e.message}")
        } catch (e: OutOfMemoryError) {
            // 极端情况（异常巨大文件）：兜底返回 error，不让 OOM 把进程拖崩。
            Log.e(TAG, "读取 $ASSET_NAME 内存溢出: ${e.message}")
            return ProfileLoadResult(emptyList(), "$ASSET_NAME 过大无法解析")
        }
        return parseProfiles(text)
    }

    /**
     * 纯解析：把 JSON 文本解码为 [ProfileLoadResult]。这里用 org.json。
     *
     * 全量校验（任一项非法 → 整体 profiles 为空、error 指明 profile id + 字段名）：
     * - 顶层必须有 "version"==1 与 "profiles" 数组；
     * - 每个 profile：id 非空且在表内不重复；
     * - manufacturerId 在 0..65535；
     * - signature / signatureMask 是合法 hex 且偶数长度；
     * - signature 与 signatureMask **等长**；
     * - signatureOffset、weightOffset、identityOffset/Length 均非负；
     * - signature(signatureOffset+len)、weight(weightOffset+2)、
     *   identity(identityOffset+identityLength) 范围均 ≤ minPayloadBytes；
     * - weightDivisor 有限且 > 0；maxGrams 有限且 ≥ 0；
     * - identityOffset 与 identityLength 必须同时出现或同时缺省。
     */
    fun parseProfiles(json: String): ProfileLoadResult {
        val root = try {
            JSONObject(json)
        } catch (e: org.json.JSONException) {
            return ProfileLoadResult(emptyList(), "JSON 解析失败: ${e.message}")
        }
        // version == 1
        if (!root.has("version")) {
            return ProfileLoadResult(emptyList(), "缺少 version 字段")
        }
        val version = root.opt("version")
        // 必须严格等于整数 1：toInt() 会把 1.5 截断成 1 放行（违反 fail-closed），
        // 故先按 Double 比较完整数值，再排除非 Number。
        // toDouble()：整数 1→1.0、1.5→1.5、2→2.0，比较 ==1.0 正确区分；
        // 字符串 "1"（非 Number）走前半分支直接拒绝。
        if (version !is Number || version.toDouble() != 1.0) {
            return ProfileLoadResult(emptyList(), "不支持的 version: $version（仅支持 1）")
        }
        // profiles 数组
        val arr: JSONArray = try {
            root.getJSONArray("profiles")
        } catch (e: org.json.JSONException) {
            return ProfileLoadResult(emptyList(), "profiles 必须是数组: ${e.message}")
        }
        if (arr.length() == 0) {
            return ProfileLoadResult(emptyList(), "profiles 为空")
        }

        val seenIds = HashSet<String>()
        val out = ArrayList<ScaleProfile>(arr.length())
        for (i in 0 until arr.length()) {
            val item = arr.optJSONObject(i)
                ?: return ProfileLoadResult(emptyList(), "profiles[$i] 不是对象")
            // 解析与校验每项；任一非法 → 整体失败（全量策略）。
            // 错误信息以 IllegalArgumentException 承载，集中在下面 catch 转成 error。
            val parsed = try {
                parseOne(item, i, seenIds)
            } catch (e: IllegalArgumentException) {
                return ProfileLoadResult(emptyList(), e.message ?: "profiles[$i] 非法")
            }
            out.add(parsed)
        }
        return ProfileLoadResult(out, null)
    }

    /**
     * 解析单个 profile 对象；非法时抛 [IllegalArgumentException]（由 [parseProfiles]
     * 捕获转成整体失败）。成功返回 [ScaleProfile]。
     */
    private fun parseOne(
        obj: JSONObject,
        index: Int,
        seenIds: HashSet<String>,
    ): ScaleProfile {
        val id = reqString(obj, "id", index).also {
            if (it.isEmpty()) throw IllegalArgumentException("profiles[$index].id 为空")
            if (!seenIds.add(it)) throw IllegalArgumentException("profiles[$index].id 重复: $it")
        }
        val displayName = reqString(obj, "displayName", index)
        val manufacturerId = reqInt(obj, "manufacturerId", index).also {
            if (it !in 0..65535)
                throw IllegalArgumentException("profiles[$index]($id).manufacturerId 越界: $it")
        }
        val signatureHex = reqString(obj, "signature", index)
        val maskHex = reqString(obj, "signatureMask", index)
        val signature = decodeHex(signatureHex)
            ?: throw IllegalArgumentException("profiles[$index]($id).signature 非法 hex: $signatureHex")
        val mask = decodeHex(maskHex)
            ?: throw IllegalArgumentException("profiles[$index]($id).signatureMask 非法 hex: $maskHex")
        if (signature.size != mask.size) {
            throw IllegalArgumentException(
                "profiles[$index]($id).signature/mask 长度不等: ${signature.size} vs ${mask.size}",
            )
        }
        if (signature.isEmpty()) {
            throw IllegalArgumentException("profiles[$index]($id).signature 为空")
        }
        val signatureOffset = reqInt(obj, "signatureOffset", index).also {
            if (it < 0) throw IllegalArgumentException("profiles[$index]($id).signatureOffset 为负: $it")
        }
        val minPayloadBytes = reqInt(obj, "minPayloadBytes", index).also {
            if (it <= 0) throw IllegalArgumentException("profiles[$index]($id).minPayloadBytes 非正: $it")
        }
        // identity：offset/length 必须同时出现或同时缺省。
        val hasIdOffset = obj.has("identityOffset")
        val hasIdLen = obj.has("identityLength")
        if (hasIdOffset != hasIdLen) {
            throw IllegalArgumentException(
                "profiles[$index]($id).identityOffset/Length 必须同时出现或同时缺省",
            )
        }
        val identityOffset: Int?
        val identityLength: Int?
        if (hasIdOffset && hasIdLen) {
            identityOffset = reqInt(obj, "identityOffset", index).also {
                if (it < 0) throw IllegalArgumentException("profiles[$index]($id).identityOffset 为负: $it")
            }
            identityLength = reqInt(obj, "identityLength", index).also {
                if (it < 0) throw IllegalArgumentException("profiles[$index]($id).identityLength 为负: $it")
            }
        } else {
            identityOffset = null
            identityLength = null
        }
        val weightOffset = reqInt(obj, "weightOffset", index).also {
            if (it < 0) throw IllegalArgumentException("profiles[$index]($id).weightOffset 为负: $it")
        }
        val weightLittleEndian = reqBool(obj, "weightLittleEndian", index)
        val weightDivisor = reqDouble(obj, "weightDivisor", index).also {
            if (!it.isFinite() || it <= 0.0)
                throw IllegalArgumentException("profiles[$index]($id).weightDivisor 非法: $it")
        }
        val maxGrams = reqDouble(obj, "maxGrams", index).also {
            if (!it.isFinite() || it < 0.0)
                throw IllegalArgumentException("profiles[$index]($id).maxGrams 非法: $it")
        }
        // deviceNameFilter：null = 不检查；其余按字符串原样存（matches 时忽略大小写）。
        val deviceNameFilter: String? = if (obj.has("deviceNameFilter") && !obj.isNull("deviceNameFilter")) {
            obj.getString("deviceNameFilter")
        } else {
            null
        }

        // 范围校验：所有字节访问下标必须 ≤ minPayloadBytes。
        // 用 Long 相加：Int.MAX_VALUE + 2 会在 Int 上溢出为负数使比较失效，
        // 绕过校验后 matches/parseAdvertisement/buildDeviceKey 按该越界下标访问数组崩溃。
        // offset 已是 Int 范围内非负值，signature.size 同为 Int，转 Long 相加不会溢出。
        if (signatureOffset.toLong() + signature.size.toLong() > minPayloadBytes.toLong()) {
            throw IllegalArgumentException(
                "profiles[$index]($id).signature 范围越界: " +
                    "offset=$signatureOffset len=${signature.size} > minPayload=$minPayloadBytes",
            )
        }
        if (weightOffset.toLong() + 2L > minPayloadBytes.toLong()) {
            throw IllegalArgumentException(
                "profiles[$index]($id).weight 范围越界: " +
                    "offset=$weightOffset 需 2 字节 > minPayload=$minPayloadBytes",
            )
        }
        if (identityOffset != null && identityLength != null) {
            if (identityOffset.toLong() + identityLength.toLong() > minPayloadBytes.toLong()) {
                throw IllegalArgumentException(
                    "profiles[$index]($id).identity 范围越界: " +
                        "offset=$identityOffset len=$identityLength > minPayload=$minPayloadBytes",
                )
            }
        }

        return ScaleProfile(
            id = id,
            displayName = displayName,
            manufacturerId = manufacturerId,
            signature = signature,
            signatureOffset = signatureOffset,
            signatureMask = mask,
            minPayloadBytes = minPayloadBytes,
            identityOffset = identityOffset,
            identityLength = identityLength,
            weightOffset = weightOffset,
            weightLittleEndian = weightLittleEndian,
            weightDivisor = weightDivisor,
            maxGrams = maxGrams,
            deviceNameFilter = deviceNameFilter,
        )
    }

    // ---- org.json 字段读取小工具（缺失/类型错都抛带定位信息的异常）----

    private fun reqString(obj: JSONObject, key: String, index: Int): String =
        try {
            obj.getString(key)
        } catch (e: org.json.JSONException) {
            throw IllegalArgumentException("profiles[$index] 缺少或非字符串: $key")
        }

    private fun reqInt(obj: JSONObject, key: String, index: Int): Int {
        if (!obj.has(key)) throw IllegalArgumentException("profiles[$index] 缺少: $key")
        val v = obj.get(key)
        return when (v) {
            is Number -> {
                // 转换前先校验：必须有限、无小数部分、且在 Int 范围内。
                // 否则 Number.toInt() 会静默截断（9.9→9、0.5→0），绕过「非法配置整体拒绝」
                // 契约——调用方按截断后的值继续，行为偏离配置意图。
                val d = v.toDouble()
                if (!d.isFinite() ||
                    d % 1.0 != 0.0 ||
                    d < Int.MIN_VALUE.toDouble() ||
                    d > Int.MAX_VALUE.toDouble()
                ) {
                    throw IllegalArgumentException("profiles[$index] $key 非整数: $v")
                }
                d.toInt()
            }
            is String -> v.toIntOrNull()
                ?: throw IllegalArgumentException("profiles[$index] $key 非整数: $v")
            else -> throw IllegalArgumentException("profiles[$index] $key 类型非法: ${v.javaClass}")
        }
    }

    private fun reqDouble(obj: JSONObject, key: String, index: Int): Double {
        if (!obj.has(key)) throw IllegalArgumentException("profiles[$index] 缺少: $key")
        val v = obj.get(key)
        return when (v) {
            is Number -> v.toDouble()
            is String -> v.toDoubleOrNull()
                ?: throw IllegalArgumentException("profiles[$index] $key 非数值: $v")
            else -> throw IllegalArgumentException("profiles[$index] $key 类型非法: ${v.javaClass}")
        }
    }

    private fun reqBool(obj: JSONObject, key: String, index: Int): Boolean {
        if (!obj.has(key)) throw IllegalArgumentException("profiles[$index] 缺少: $key")
        val v = obj.get(key)
        return when (v) {
            is Boolean -> v
            is String -> v.toBooleanStrictOrNull()
                ?: throw IllegalArgumentException("profiles[$index] $key 非布尔: $v")
            else -> throw IllegalArgumentException("profiles[$index] $key 类型非法: ${v.javaClass}")
        }
    }

    /**
     * hex 字符串解码为 [ByteArray]；非法（奇数长度、非 hex 字符）返回 null。
     * 大小写不敏感。
     */
    private fun decodeHex(hex: String): ByteArray? {
        // 空串视为非法（签名/掩码不能为空）；奇数长度一定非法。
        if (hex.isEmpty() || hex.length % 2 != 0) return null
        val out = ByteArray(hex.length / 2)
        var i = 0
        while (i < hex.length) {
            val hi = hexNibble(hex[i])
            val lo = hexNibble(hex[i + 1])
            if (hi < 0 || lo < 0) return null
            out[i / 2] = ((hi shl 4) or lo).toByte()
            i += 2
        }
        return out
    }

    private fun hexNibble(c: Char): Int = when (c) {
        in '0'..'9' -> c - '0'
        in 'a'..'f' -> c - 'a' + 10
        in 'A'..'F' -> c - 'A' + 10
        else -> -1
    }

    // ============================================================
    // 纯函数：匹配 / 解析 / deviceKey（均不碰 org.json）
    // ============================================================

    /**
     * 判断一条广播 payload 是否命中该 profile。命中规则：
     * 1. manufacturerId 相等；
     * 2. payload 长度 ≥ [ScaleProfile.minPayloadBytes]；
     * 3. [ScaleProfile.deviceNameFilter] 非空时，[advertisedName] 与之忽略大小写完整相等；
     * 4. 签名逐字节（带 mask）匹配：
     *    `for i: (payload[offset+i] & mask[i]) == (signature[i] & mask[i])`，
     *    mask 字节 0xFF 参与比较、0x00 忽略，天然支持位级 mask。
     *
     * @param manufacturerId 调用方取到的 manufacturer id（应与 profile.manufacturerId 一致，
     *   通常来自 [android.bluetooth.le.ScanRecord.getManufacturerSpecificData] 的 key）。
     * @param payload 该 manufacturer id 对应的 payload（**不含** id 本身 2 字节）。
     * @param advertisedName 广播内的 Local Name（ScanRecord.deviceName），
     *   **不要**用 BluetoothDevice.name（后者可能为系统缓存或 null）。
     */
    fun matches(
        profile: ScaleProfile,
        manufacturerId: Int,
        payload: ByteArray?,
        advertisedName: String?,
    ): Boolean {
        if (manufacturerId != profile.manufacturerId) return false
        if (payload == null || payload.size < profile.minPayloadBytes) return false
        // Local Name 过滤：非空才检查，忽略大小写完整匹配。
        val filter = profile.deviceNameFilter
        if (filter != null) {
            if (advertisedName == null) return false
            if (!advertisedName.equals(filter, ignoreCase = true)) return false
        }
        // 签名逐字节 mask 比较（不提前短路，避免时序差异；签名仅 9 字节，成本可忽略）。
        val sig = profile.signature
        val mask = profile.signatureMask
        val off = profile.signatureOffset
        for (i in sig.indices) {
            val p = payload[off + i].toInt() and 0xFF
            val s = sig[i].toInt() and 0xFF
            val m = mask[i].toInt() and 0xFF
            if ((p and m) != (s and m)) return false
        }
        return true
    }

    /**
     * 解析命中后的 payload 为读数。命中前的基本校验（长度/id/签名）已由 [matches] 完成，
     * 这里只做重量解码与量程判断。
     *
     * - raw = payload[weightOffset] | (payload[weightOffset+1] << 8)，
     *   小端/大端由 [ScaleProfile.weightLittleEndian] 决定；
     * - grams = raw / weightDivisor；
     * - raw==0 是合法的 0.0g，**不**拒绝；
     * - grams 超出 `[0, maxGrams]` 或非有限 → 返回 null（协议拒绝）。
     *
     * @return 解析成功返回 [ParsedReading]；超量程返回 null。
     */
    fun parseAdvertisement(
        profile: ScaleProfile,
        payload: ByteArray,
        rssi: Int,
        receivedAtEpochMs: Long,
    ): ParsedReading? {
        // 长度二次保护（matches 已校验，防御性）。
        if (payload.size < profile.weightOffset + 2) return null
        val lo = payload[profile.weightOffset].toInt() and 0xFF
        val hi = payload[profile.weightOffset + 1].toInt() and 0xFF
        val raw = if (profile.weightLittleEndian) {
            lo or (hi shl 8)
        } else {
            (lo shl 8) or hi
        }
        val grams = raw / profile.weightDivisor
        if (!grams.isFinite()) return null
        if (grams < 0.0 || grams > profile.maxGrams) return null
        return ParsedReading(
            grams = grams,
            raw = raw,
            rssi = rssi,
            receivedAtEpochMs = receivedAtEpochMs,
            payloadHex = toHexSpaces(payload),
        )
    }

    /**
     * 构建设备主键：`<profileId>:<identityHex 小写>`，如 `k797:0644df`。
     *
     * - 若 profile 配置了 identityOffset/Length（同时非空）：从 payload 取出那几个字节，
     *   转小写连续 hex，拼成 `k797:0644df`。这是稳定的设备身份（同一台秤的序列号不变）。
     * - 若 profile 未配置 identity（offset/length 均缺省）：回退
     *   `<profileId>:<规范化 BLE 地址>`（去冒号小写）。
     *   **注意：BLE 随机私有地址会周期性轮换，回退键不稳定**——仅作 deviceId 的弱标识，
     *   配置 K797 时务必提供 identityOffset/Length 以获得稳定身份。
     *
     * @param bleAddress 用于回退路径的 BLE 地址（如 "AA:BB:CC:DD:EE:FF"）。
     */
    fun buildDeviceKey(
        profile: ScaleProfile,
        payload: ByteArray,
        bleAddress: String,
    ): String {
        val off = profile.identityOffset
        val len = profile.identityLength
        if (off != null && len != null && len > 0 && off + len <= payload.size) {
            val sb = StringBuilder(profile.id.length + 1 + len * 2)
            sb.append(profile.id).append(':')
            for (i in 0 until len) {
                val b = payload[off + i].toInt() and 0xFF
                sb.append(HEX_LOWER[b ushr 4]).append(HEX_LOWER[b and 0x0F])
            }
            return sb.toString()
        }
        // 回退：规范化 BLE 地址（去冒号、小写）。注意随机地址不稳定（见方法注释）。
        val norm = bleAddress.replace(":", "").lowercase()
        return "${profile.id}:$norm"
    }

    /** 把 payload 转成空格分隔大写十六进制（便于日志与 payloadHex 字段）。 */
    private fun toHexSpaces(bytes: ByteArray): String {
        val sb = StringBuilder(bytes.size * 3)
        for (i in bytes.indices) {
            if (i > 0) sb.append(' ')
            val v = bytes[i].toInt() and 0xFF
            sb.append(HEX_UPPER[v ushr 4]).append(HEX_UPPER[v and 0x0F])
        }
        return sb.toString()
    }

    private val HEX_LOWER = "0123456789abcdef".toCharArray()
    private val HEX_UPPER = "0123456789ABCDEF".toCharArray()
}
