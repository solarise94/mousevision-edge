package com.pingoodmice.miceautomatic

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * ScaleProfile 单测。
 *
 * 核心匹配/解析测试**直接构造 [ScaleProfile]**（绕过 org.json），验证 [ScaleProfileRegistry]
 * 的纯函数逻辑；JSON 解析测试用 [parseProfilesJson]（依赖 JVM 版 org.json）单独覆盖。
 *
 * 覆盖要点（详见各 @Test 注释）：
 * - 旧/新序列号 payload 均能命中 k797；
 * - 改序列号不影响匹配，改 mask 参与的头/尾字节立即拒绝；
 * - manufacturerId 不同 / payload 过短 / raw=0 / 小端解码 / identity 提取 / profile 顺序；
 * - 超量程拒绝（复制一个 maxGrams=100.0 的 profile）；
 * - 配置校验（签名/mask 等长、offset 越界、weightDivisor=0、identity 半配置）；
 * - JSON 解析（修订后 assets 配置 → 1 个 k797 profile，signature/mask 等长 ByteArray）。
 */
class ScaleProfileTest {

    // ---- 测试用 profile 与 payload 工厂（不碰 org.json）----

    /**
     * 与 assets/scale_profiles.json 等价的解码 profile：
     * signature = CA E8 03 ?? ?? ?? CA 02 10（中间 3 字节序列号被 mask 忽略）。
     */
    private fun k797Profile(): ScaleProfile = ScaleProfile(
        id = "k797",
        displayName = "K797 蓝牙天平",
        manufacturerId = 0,
        signature = byteArrayOf(
            0xCA.toByte(), 0xE8.toByte(), 0x03, 0x00, 0x00, 0x00,
            0xCA.toByte(), 0x02, 0x10,
        ),
        signatureOffset = 0,
        signatureMask = byteArrayOf(
            0xFF.toByte(), 0xFF.toByte(), 0xFF.toByte(),
            0x00, 0x00, 0x00,
            0xFF.toByte(), 0xFF.toByte(), 0xFF.toByte(),
        ),
        minPayloadBytes = 18,
        identityOffset = 3,
        identityLength = 3,
        weightOffset = 9,
        weightLittleEndian = true,
        weightDivisor = 10.0,
        maxGrams = 6553.5,
        deviceNameFilter = null,
    )

    /**
     * 构造一条 18 字节 payload：前 9 字节签名区（含序列号），9-10 字节重量（小端），
     * 其余补 0。signaturePrefix9 必须是 9 字节。
     */
    private fun payload(signaturePrefix9: ByteArray, rawLo: Int, rawHi: Int): ByteArray {
        assertEquals(9, signaturePrefix9.size)
        val out = ByteArray(18)
        for (i in 0 until 9) out[i] = signaturePrefix9[i]
        out[9] = rawLo.toByte()
        out[10] = rawHi.toByte()
        return out
    }

    /** 旧秤签名前 9 字节：CA E8 03 28 08 95 CA 02 10。 */
    private val oldPrefix = byteArrayOf(
        0xCA.toByte(), 0xE8.toByte(), 0x03, 0x28, 0x08, 0x95.toByte(),
        0xCA.toByte(), 0x02, 0x10,
    )

    /** 新秤签名前 9 字节：CA E8 03 06 44 DF CA 02 10（序列号不同）。 */
    private val newPrefix = byteArrayOf(
        0xCA.toByte(), 0xE8.toByte(), 0x03, 0x06, 0x44, 0xDF.toByte(),
        0xCA.toByte(), 0x02, 0x10,
    )

    // ---- 匹配测试 ----

    @Test
    fun `旧秤序列号 payload 匹配 k797 profile`() {
        val profile = k797Profile()
        val p = payload(oldPrefix, 0x07, 0x01) // raw=263 → 26.3g
        assertTrue(ScaleProfileRegistry.matches(profile, 0, p, null))
    }

    @Test
    fun `新秤序列号 payload 匹配 k797 profile`() {
        val profile = k797Profile()
        val p = payload(newPrefix, 0x07, 0x01) // raw=263 → 26.3g
        assertTrue(ScaleProfileRegistry.matches(profile, 0, p, null))
    }

    @Test
    fun `旧秤与新秤解析出相同预期重量 26_3g`() {
        val profile = k797Profile()
        val oldReading = ScaleProfileRegistry.parseAdvertisement(
            profile, payload(oldPrefix, 0x07, 0x01), -60, 1_000L,
        )
        val newReading = ScaleProfileRegistry.parseAdvertisement(
            profile, payload(newPrefix, 0x07, 0x01), -60, 1_000L,
        )
        assertNotNull(oldReading)
        assertNotNull(newReading)
        assertEquals(26.3, oldReading!!.grams, 0.0001)
        assertEquals(26.3, newReading!!.grams, 0.0001)
        assertEquals(263, oldReading.raw)
        assertEquals(263, newReading.raw)
    }

    @Test
    fun `序列号不同不影响匹配`() {
        val profile = k797Profile()
        // 构造两个序列号完全不同（但都在 mask 忽略的位置）的 payload，都应命中。
        val p1 = payload(
            byteArrayOf(
                0xCA.toByte(), 0xE8.toByte(), 0x03, 0xAA.toByte(), 0xBB.toByte(), 0xCC.toByte(),
                0xCA.toByte(), 0x02, 0x10,
            ),
            0x00, 0x00,
        )
        val p2 = payload(
            byteArrayOf(
                0xCA.toByte(), 0xE8.toByte(), 0x03, 0x11.toByte(), 0x22.toByte(), 0x33.toByte(),
                0xCA.toByte(), 0x02, 0x10,
            ),
            0x00, 0x00,
        )
        assertTrue(ScaleProfileRegistry.matches(profile, 0, p1, null))
        assertTrue(ScaleProfileRegistry.matches(profile, 0, p2, null))
    }

    @Test
    fun `头部签名字节不同 首字节 CB 被拒`() {
        val profile = k797Profile()
        // 首字节 CB（参与 mask 比较）→ 不匹配。
        val bad = byteArrayOf(
            0xCB.toByte(), 0xE8.toByte(), 0x03, 0x28, 0x08, 0x95.toByte(),
            0xCA.toByte(), 0x02, 0x10,
        )
        assertFalse(ScaleProfileRegistry.matches(profile, 0, payload(bad, 0, 0), null))
    }

    @Test
    fun `尾部签名字节不同 CA0210 改为 CA0211 被拒`() {
        val profile = k797Profile()
        // 尾字节 11（应为 10，参与 mask 比较）→ 不匹配。
        val bad = byteArrayOf(
            0xCA.toByte(), 0xE8.toByte(), 0x03, 0x28, 0x08, 0x95.toByte(),
            0xCA.toByte(), 0x02, 0x11.toByte(),
        )
        assertFalse(ScaleProfileRegistry.matches(profile, 0, payload(bad, 0, 0), null))
    }

    @Test
    fun `manufacturerId 不同被拒`() {
        val profile = k797Profile() // manufacturerId = 0
        val p = payload(oldPrefix, 0, 0)
        // 传入与 profile 不同的 manufacturerId → matches 第一关就拒。
        assertFalse(ScaleProfileRegistry.matches(profile, 0xFFFF, p, null))
    }

    @Test
    fun `payload 少于 minPayloadBytes 被拒`() {
        val profile = k797Profile() // minPayloadBytes = 18
        val short = ByteArray(17) // 17 < 18
        assertFalse(ScaleProfileRegistry.matches(profile, 0, short, null))
    }

    @Test
    fun `raw 0 返回 0_0g 不是无效读数`() {
        val profile = k797Profile()
        val reading = ScaleProfileRegistry.parseAdvertisement(profile, payload(oldPrefix, 0, 0), -50, 0L)
        assertNotNull("raw=0 是合法的 0.0g，不应被拒绝", reading)
        assertEquals(0.0, reading!!.grams, 0.0001)
        assertEquals(0, reading.raw)
    }

    @Test
    fun `小端解码正确 01 07 解为 raw 1793 即 179_3g`() {
        val profile = k797Profile()
        // weightOffset=9，小端：lo=0x01, hi=0x07 → raw = 0x01 | (0x07 << 8) = 0x0701 = 1793。
        val reading = ScaleProfileRegistry.parseAdvertisement(profile, payload(oldPrefix, 0x01, 0x07), -50, 0L)
        assertNotNull(reading)
        assertEquals(1793, reading!!.raw)
        assertEquals(179.3, reading.grams, 0.0001)
    }

    // ---- identity / deviceKey ----

    @Test
    fun `identity 提取 旧秤 k797_280895`() {
        val profile = k797Profile()
        val key = ScaleProfileRegistry.buildDeviceKey(profile, payload(oldPrefix, 0, 0), "AA:BB:CC:DD:EE:FF")
        assertEquals("k797:280895", key)
    }

    @Test
    fun `identity 提取 新秤 k797_0644df`() {
        val profile = k797Profile()
        val key = ScaleProfileRegistry.buildDeviceKey(profile, payload(newPrefix, 0, 0), "AA:BB:CC:DD:EE:FF")
        assertEquals("k797:0644df", key)
    }

    @Test
    fun `identity 缺省时回退 profileId_规范化BLE地址`() {
        // 构造一个没有 identityOffset/Length 的 profile，验证回退路径。
        val profile = k797Profile().copy(identityOffset = null, identityLength = null)
        val key = ScaleProfileRegistry.buildDeviceKey(profile, payload(oldPrefix, 0, 0), "AA:BB:CC:DD:EE:FF")
        assertEquals("k797:aabbccddeeff", key)
    }

    // ---- profiles 顺序决定首个命中 ----

    @Test
    fun `profiles 表顺序决定首个命中`() {
        // 两个 profile 都能匹配同一条 payload（相同签名）。
        val first = k797Profile()
        val second = first.copy(id = "k797b", displayName = "第二个")
        val profiles = listOf(first, second)
        val p = payload(oldPrefix, 0x07, 0x01)

        // 模拟 K797BleScanner.processScanResult 的 firstNotNullOfOrNull 逻辑。
        val hit = profiles.firstNotNullOfOrNull { profile ->
            if (ScaleProfileRegistry.matches(profile, profile.manufacturerId, p, null)) {
                profile
            } else {
                null
            }
        }
        assertNotNull(hit)
        assertEquals("k797", hit!!.id) // 取第一个
    }

    // ---- 超量程拒绝 ----

    @Test
    fun `超量程拒绝 maxGrams 100 用 raw 1001 即 100_1g`() {
        // 复制一个 maxGrams=100.0 的 profile；raw=1001 → 100.1g > 100.0 → 拒绝。
        val profile = k797Profile().copy(id = "k797-cap100", maxGrams = 100.0)
        // raw=1001：lo=0xE9, hi=0x03 → 1001。
        val reading = ScaleProfileRegistry.parseAdvertisement(profile, payload(oldPrefix, 0xE9, 0x03), -50, 0L)
        assertNull("100.1g 超过 maxGrams=100.0 应拒绝", reading)
    }

    @Test
    fun `满量程 6553_5g 不被拒`() {
        // k797 默认 maxGrams=6553.5；raw=65535 → 6553.5g 应通过（边界值）。
        val profile = k797Profile()
        val reading = ScaleProfileRegistry.parseAdvertisement(profile, payload(oldPrefix, 0xFF, 0xFF), -50, 0L)
        assertNotNull(reading)
        assertEquals(65535, reading!!.raw)
        assertEquals(6553.5, reading.grams, 0.0001)
    }

    // ---- 配置校验（经 parseProfiles）----

    /** 修订后的完整 JSON（与 assets/scale_profiles.json 等价），作为校验测试的基线。 */
    private val validJson: String = """
        {
          "version": 1,
          "profiles": [
            {
              "id": "k797",
              "displayName": "K797 蓝牙天平",
              "manufacturerId": 0,
              "signature": "CAE803000000CA0210",
              "signatureOffset": 0,
              "signatureMask": "FFFFFF000000FFFFFF",
              "minPayloadBytes": 18,
              "identityOffset": 3,
              "identityLength": 3,
              "weightOffset": 9,
              "weightLittleEndian": true,
              "weightDivisor": 10.0,
              "maxGrams": 6553.5,
              "deviceNameFilter": null
            }
          ]
        }
    """.trimIndent()

    /** 把单字段替换后的 JSON 喂给 parseProfiles，断言整体失败（profiles 空、error 非空）。 */
    private fun parseProfilesJson(json: String): ProfileLoadResult =
        ScaleProfileRegistry.parseProfiles(json)

    @Test
    fun `JSON 解析 修订后配置解码出 1 个 k797 profile 且 signature mask 等长`() {
        val result = parseProfilesJson(validJson)
        assertNull("error 应为 null，实际=${result.error}", result.error)
        assertEquals(1, result.profiles.size)
        val p = result.profiles[0]
        assertEquals("k797", p.id)
        assertEquals("K797 蓝牙天平", p.displayName)
        assertEquals(0, p.manufacturerId)
        assertEquals(9, p.signature.size)
        assertEquals(9, p.signatureMask.size)
        assertEquals(p.signature.size, p.signatureMask.size)
        // 验证解码出的具体字节。
        assertEquals(0xCA.toByte(), p.signature[0])
        assertEquals(0x10.toByte(), p.signature[8])
        assertEquals(0xFF.toByte(), p.signatureMask[0])
        assertEquals(0x00.toByte(), p.signatureMask[3])
        assertEquals(0xFF.toByte(), p.signatureMask[8])
        assertEquals(18, p.minPayloadBytes)
        assertEquals(3, p.identityOffset)
        assertEquals(3, p.identityLength)
        assertEquals(9, p.weightOffset)
        assertTrue(p.weightLittleEndian)
        assertEquals(10.0, p.weightDivisor, 0.0001)
        assertEquals(6553.5, p.maxGrams, 0.0001)
        assertNull(p.deviceNameFilter)
    }

    @Test
    fun `配置校验 signature 与 mask 长度不等被拒`() {
        val bad = validJson.replace("\"signatureMask\": \"FFFFFF000000FFFFFF\"", "\"signatureMask\": \"FFFFFF\"")
        val result = parseProfilesJson(bad)
        assertTrue("profiles 应为空", result.profiles.isEmpty())
        assertNotNull(result.error)
        assertTrue("error 应指明 signature/mask 长度不等: ${result.error}", result.error!!.contains("长度不等"))
    }

    @Test
    fun `配置校验 weightOffset 越界被拒`() {
        // weightOffset=17 + 2 = 19 > minPayloadBytes=18 → 越界。
        val bad = validJson.replace("\"weightOffset\": 9,", "\"weightOffset\": 17,")
        val result = parseProfilesJson(bad)
        assertTrue(result.profiles.isEmpty())
        assertNotNull(result.error)
        assertTrue("error 应指明 weight 范围越界: ${result.error}", result.error!!.contains("weight"))
    }

    @Test
    fun `配置校验 signatureOffset 越界被拒`() {
        // signatureOffset=10 + 9 = 19 > 18 → 越界。
        val bad = validJson.replace("\"signatureOffset\": 0,", "\"signatureOffset\": 10,")
        val result = parseProfilesJson(bad)
        assertTrue(result.profiles.isEmpty())
        assertNotNull(result.error)
        assertTrue("error 应指明 signature 范围越界: ${result.error}", result.error!!.contains("signature"))
    }

    @Test
    fun `配置校验 weightDivisor 为 0 被拒`() {
        val bad = validJson.replace("\"weightDivisor\": 10.0,", "\"weightDivisor\": 0.0,")
        val result = parseProfilesJson(bad)
        assertTrue(result.profiles.isEmpty())
        assertNotNull(result.error)
        assertTrue("error 应指明 weightDivisor 非法: ${result.error}", result.error!!.contains("weightDivisor"))
    }

    @Test
    fun `配置校验 identityOffset 与 Length 只出现其一被拒`() {
        // 删掉 identityLength，只留 identityOffset。
        val bad = validJson.replace("\"identityLength\": 3,\n", "")
        val result = parseProfilesJson(bad)
        assertTrue(result.profiles.isEmpty())
        assertNotNull(result.error)
        assertTrue(
            "error 应指明 identity 必须同时出现或缺省: ${result.error}",
            result.error!!.contains("identity"),
        )
    }

    @Test
    fun `配置校验 空白 JSON 被拒`() {
        val result = parseProfilesJson("")
        assertTrue(result.profiles.isEmpty())
        assertNotNull(result.error)
    }

    @Test
    fun `配置校验 version 非 1 被拒`() {
        val bad = validJson.replace("\"version\": 1,", "\"version\": 2,")
        val result = parseProfilesJson(bad)
        assertTrue(result.profiles.isEmpty())
        assertNotNull(result.error)
        assertTrue(result.error!!.contains("version"))
    }

    @Test
    fun `配置校验 manufacturerId 越界被拒`() {
        val bad = validJson.replace("\"manufacturerId\": 0,", "\"manufacturerId\": 70000,")
        val result = parseProfilesJson(bad)
        assertTrue(result.profiles.isEmpty())
        assertNotNull(result.error)
        assertTrue(result.error!!.contains("manufacturerId"))
    }

    // ---- 整数字段拒绝小数（reqInt 不再静默截断）----

    @Test
    fun `配置校验 weightOffset 为小数 9_9 被整体拒绝`() {
        // 修复前 Number.toInt() 把 9.9 静默截断成 9（看似合法），绕过「非法配置整体拒绝」。
        val bad = validJson.replace("\"weightOffset\": 9,", "\"weightOffset\": 9.9,")
        val result = parseProfilesJson(bad)
        assertTrue("profiles 应为空", result.profiles.isEmpty())
        assertNotNull(result.error)
        assertTrue("error 应指明 weightOffset 非整数: ${result.error}", result.error!!.contains("weightOffset"))
    }

    @Test
    fun `配置校验 manufacturerId 为小数 0_5 被整体拒绝`() {
        val bad = validJson.replace("\"manufacturerId\": 0,", "\"manufacturerId\": 0.5,")
        val result = parseProfilesJson(bad)
        assertTrue(result.profiles.isEmpty())
        assertNotNull(result.error)
        assertTrue("error 应指明 manufacturerId 非整数: ${result.error}", result.error!!.contains("manufacturerId"))
    }

    @Test
    fun `配置校验 minPayloadBytes 为小数 18_7 被整体拒绝`() {
        val bad = validJson.replace("\"minPayloadBytes\": 18,", "\"minPayloadBytes\": 18.7,")
        val result = parseProfilesJson(bad)
        assertTrue(result.profiles.isEmpty())
        assertNotNull(result.error)
        assertTrue("error 应指明 minPayloadBytes 非整数: ${result.error}", result.error!!.contains("minPayloadBytes"))
    }

    @Test
    fun `配置校验 整数字段的字符串小数 9_9 也被拒`() {
        // String 分支 toIntOrNull() 本就拒绝小数；确认 "9.9" → null → 走非法分支。
        val bad = validJson.replace("\"weightOffset\": 9,", "\"weightOffset\": \"9.9\",")
        val result = parseProfilesJson(bad)
        assertTrue(result.profiles.isEmpty())
        assertNotNull(result.error)
        assertTrue(result.error!!.contains("weightOffset"))
    }

    @Test
    fun `配置校验 合法整数字段不被误拒`() {
        // 回归：reqInt 加严后，合法整数（含整数字面量、整数串）仍正常解析。
        val result = parseProfilesJson(validJson)
        assertNull("合法配置不应被拒，error=${result.error}", result.error)
        assertEquals(1, result.profiles.size)
        assertEquals(9, result.profiles[0].weightOffset)
        assertEquals(0, result.profiles[0].manufacturerId)
        assertEquals(18, result.profiles[0].minPayloadBytes)
    }

    // ---- P2-a：version 小数不能被 toInt 截断放行 ----

    @Test
    fun `配置校验 version 小数 1_5 被拒不被 toInt 截断为 1`() {
        // 修复前 version.toInt() 把 1.5 截断成 1 放行（profiles=1, error=null），
        // 违反 version==1 契约。修复后按 Double 比较 1.5 != 1.0 → 整体拒绝。
        val bad = validJson.replace("\"version\": 1,", "\"version\": 1.5,")
        val result = parseProfilesJson(bad)
        assertTrue("profiles 应为空（小数 version 必须拒绝）", result.profiles.isEmpty())
        assertNotNull(result.error)
        assertTrue("error 应含 version: ${result.error}", result.error!!.contains("version"))
    }

    @Test
    fun `配置校验 version 1_0 浮点字面量通过`() {
        // JSON 1.0 解析为 Double；按 Double 比较 1.0 == 1.0 → 放行。
        val ok = validJson.replace("\"version\": 1,", "\"version\": 1.0,")
        val result = parseProfilesJson(ok)
        assertNull("version=1.0 等价整数 1 应通过，error=${result.error}", result.error)
        assertEquals(1, result.profiles.size)
    }

    // ---- P2-b：offset 范围校验 Int 加法溢出绕过 ----

    @Test
    fun `配置校验 weightOffset Int最大值加2溢出被拒`() {
        // 修复前 weightOffset=Int.MAX_VALUE(2147483647) + 2 在 Int 上溢出为负数，
        // 比较失效绕过校验成功加载；后续 parseAdvertisement 按该越界下标访问崩溃。
        // 修复后用 Long 相加：2147483649L > 18L → 整体拒绝。
        val bad = validJson.replace("\"weightOffset\": 9,", "\"weightOffset\": 2147483647,")
        val result = parseProfilesJson(bad)
        assertTrue("profiles 应为空（溢出 offset 必须拒绝）", result.profiles.isEmpty())
        assertNotNull(result.error)
        assertTrue("error 应指明 weight 范围越界: ${result.error}", result.error!!.contains("weight"))
    }

    @Test
    fun `配置校验 signatureOffset Int最大值加签名长度溢出被拒`() {
        // signatureOffset=Int.MAX_VALUE + 9 字节签名 → Int 溢出绕过；
        // Long 相加 2147483656L > 18L → 拒绝。
        val bad = validJson.replace("\"signatureOffset\": 0,", "\"signatureOffset\": 2147483647,")
        val result = parseProfilesJson(bad)
        assertTrue("profiles 应为空（溢出 offset 必须拒绝）", result.profiles.isEmpty())
        assertNotNull(result.error)
        assertTrue("error 应指明 signature 范围越界: ${result.error}", result.error!!.contains("signature"))
    }

    @Test
    fun `配置校验 identityOffset Int最大值加3溢出被拒`() {
        // identityOffset=Int.MAX_VALUE + identityLength=3 → Int 溢出绕过；
        // Long 相加 2147483650L > 18L → 拒绝。
        val bad = validJson.replace("\"identityOffset\": 3,", "\"identityOffset\": 2147483647,")
        val result = parseProfilesJson(bad)
        assertTrue("profiles 应为空（溢出 offset 必须拒绝）", result.profiles.isEmpty())
        assertNotNull(result.error)
        assertTrue("error 应指明 identity 范围越界: ${result.error}", result.error!!.contains("identity"))
    }
}
