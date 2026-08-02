// =============================================================================
// K797 BLE 虚拟硬件固件 (ESP32-C6 + NimBLE-Arduino 2.x)
// -----------------------------------------------------------------------------
// 模拟真实 K797 不可连接蓝牙天平的广播，供鸿蒙/Android 扫描器与解析器联调。
//
// ============================================================
//  广播字节布局 (legacy ADV, 31 字节预算, 非连接, 无 scan response)
// ============================================================
//  Flags AD          : 02 01 06                         (3 字节)
//  Manufacturer Data : 16 FF 00 00 <18 payload>         (22 字节)
//    └─ 16=length(22)  FF=类型  00 00=公司ID(LE)  +18B 载荷
//  Complete Local Name: 05 09 4B 37 39 37 ("K797")     (6 字节)
//                                                       ──────
//                                          合计      = 31 字节
//  说明：NimBLE 2.x 的 setManufacturerData(uint8_t*,size_t) 不会自动补公司ID，
//  调用方必须把 2 字节 little-endian 公司 ID 放在数据最前面。
//  本固件把 [00 00] + 18B 载荷共 20 字节传给 setManufacturerData()，
//  上线后 AD 结构即 16 FF 00 00 <18B>，与解析器 getManufacturerSpecificData(0x0000)
//  返回的 18 字节载荷一致。
//
//  18 字节载荷：
//    [0..8]  固定前缀 CA E8 03 28 08 95 CA 02 10
//    [9..10] 重量 raw, little-endian uint16, grams = raw / 10.0
//    [11..17]尾部 7 字节（真实设备未确认，默认全零；K797_TRAILING 配置）
//
// ============================================================
//  NimBLE API 版本说明 (重要)
// ============================================================
//  本代码针对 **NimBLE-Arduino 2.x (>=2.5.0)** 编写：
//    - 可连接模式用 setConnectableMode(BLE_GAP_CONN_MODE_NON)（1.x 的
//      setConnectable(false) 在 2.x 已废弃，2.x 改为 connectable+discoverable
//      两个独立模式）。
//    - setManufacturerData(uint8_t*, size_t) 不自动补公司 ID（见上）。
//    - 运行时刷新载荷用 refreshAdvertisingData()，无需 stop/start。
//    - 间隔单位为 0.625ms（BLE_GAP_ADV_ITVL），200ms => 320。
//  platformio.ini 锁定 ^2.5.0；切勿降级到 1.x。
//
// ============================================================
//  内存与稳定性设计 (4 小时连续运行无内存无界增长)
// ============================================================
//  - 全程无 malloc/new/malloc-family 调用；广播缓冲区与场景表全部静态。
//  - 串口命令处理使用静态行缓冲，单事件循环驱动，无动态队列。
//  - 重量变更通过原地改缓冲区 + refreshAdvertisingData()，不重建对象。
//  - 不使用 String 堆分配构造串口输出（除 STATUS 调试，使用静态 printf）。
//  设计目标：4 小时连续运行 free heap 不下降、事件计数线性增长无回压。
//
// 仅用于开发/测试。作者不对此固件的真实硬件适配性作任何保证。
// =============================================================================

#include <Arduino.h>
#include <NimBLEDevice.h>
#include "scenarios.h"

// ---------------------------------------------------------------------------
// 协议常量（与解析器 K797BleScanner.kt / 文档 §2 完全一致）
// ---------------------------------------------------------------------------
static constexpr const char* K797_NAME          = "K797";
static constexpr uint16_t    K797_MANUFACTURER_ID = 0x0000;   // BLE 公司 ID（little-endian 编码）
static constexpr uint8_t     K797_PAYLOAD_LEN  = 18;          // 最小/固定载荷长度
static constexpr uint8_t     K797_PREFIX_LEN   = 9;           // 固定前缀长度
static constexpr uint8_t     K797_PREFIX[K797_PREFIX_LEN] = {
    0xCA, 0xE8, 0x03, 0x28, 0x08, 0x95, 0xCA, 0x02, 0x10
};
// 尾部 [11..17] 默认全零；真实设备未确认。
// 若需变更，可在此数组里填入采集到的字节（长度必须为 7）。
static constexpr uint8_t     K797_TRAILING[7] = {0, 0, 0, 0, 0, 0, 0};

// 重量范围：raw 为 uint16，grams = raw/10.0，故 grams 上限 6553.5。
static constexpr float        GRAMS_MAX = 6553.5f;
static constexpr float        GRAMS_ROUND = 0.1f;             // 0.1 g 分辨率 = raw ±1
static constexpr uint16_t     GRAMS_RAW_MAX = 65535;

// 广播间隔（默认 200ms，可运行时配置 100..1000ms）
static constexpr uint32_t     ADV_INTERVAL_DEFAULT_MS = 200;
static constexpr uint32_t     ADV_INTERVAL_MIN_MS     = 100;
static constexpr uint32_t     ADV_INTERVAL_MAX_MS     = 1000;
// NimBLE 间隔单位 0.625ms；ms -> 单位 = ms * 1000 / 625 = ms * 8 / 5
static inline constexpr uint16_t msToAdvUnits(uint32_t ms) {
    return (uint16_t)((ms * 8U) / 5U);
}

// 状态 LED（可选；板载 RGB 在 GPIO8，默认关闭避免 STRAP 冲突）
#ifdef K797_LED_PIN
static constexpr uint8_t LED_PIN = K797_LED_PIN;
#endif

// ---------------------------------------------------------------------------
// 静态广播缓冲区（构建/读取全程无堆分配）
//   g_manufData : 公司ID(2) + 载荷(18) = 20 字节，传给 setManufacturerData
// ---------------------------------------------------------------------------
static uint8_t g_manufData[2 + K797_PAYLOAD_LEN] = {0};

// ---------------------------------------------------------------------------
// 运行模式
// ---------------------------------------------------------------------------
enum class Mode : uint8_t {
    Fixed     = 0,  // 固定重量（含 ZERO）
    Noise     = 1,  // 固定重量 ± 抖动
    Scenario  = 2,  // 内置场景播放（可能 LOOP）
    Malformed = 3,  // 故意畸形载荷（short / prefix）
};

enum class MalformedKind : uint8_t {
    None    = 0,
    Short   = 1,   // 10 字节截断载荷（仍名 K797 + ID 0x0000）
    Prefix  = 2,   // 第一字节前缀破坏（CA -> CB）
};

// ---------------------------------------------------------------------------
// 全局状态（串口命令写、loop 读；Arduino 单核 loop 串行化，无需互斥）
// ---------------------------------------------------------------------------
static NimBLEAdvertising* g_adv = nullptr;

static Mode          g_mode        = Mode::Fixed;
static uint16_t      g_lastRaw     = 0;        // 最近一次固定重量 raw（恢复用）
static uint16_t      g_noiseCenter = 0;        // NOISE 中心 raw
static uint16_t      g_noiseAmp    = 0;        // NOISE 抖动幅度 raw（±）
static MalformedKind g_malKind     = MalformedKind::None;

// SILENCE：停止广播若干毫秒，结束后恢复上一个固定重量
static bool          g_silenceActive   = false;
static uint32_t      g_silenceUntilMs  = 0;
static uint32_t      g_advIntervalMs   = ADV_INTERVAL_DEFAULT_MS;
static bool          g_advRunning      = false;

// 场景播放状态
static const k797::Scenario* g_scenario    = nullptr;
static bool                  g_scenarioLoop = false;
static uint32_t              g_scenarioStartMs = 0;
static uint32_t              g_scenarioLenMs   = 0;   // 末事件 atMs（循环边界）
static size_t                g_scenarioNextIdx = 0;
static uint32_t              g_scenarioSilenceUntilMs = 0; // 场景内静默结束时间

// 调试统计（单调递增，便于 host 校验无丢步）
static uint32_t      g_updateCount = 0;

// ---------------------------------------------------------------------------
// 工具：grams <-> raw
// ---------------------------------------------------------------------------
static inline uint16_t gramsToRaw(float grams) {
    if (grams < 0.0f) grams = 0.0f;
    if (grams > GRAMS_MAX) grams = GRAMS_MAX;
    // 四舍五入到 0.1g（raw ±1）
    long r = (long)llroundf(grams * 10.0f);
    if (r < 0) r = 0;
    if (r > GRAMS_RAW_MAX) r = GRAMS_RAW_MAX;
    return (uint16_t)r;
}
static inline float rawToGrams(uint16_t raw) { return raw / 10.0f; }

// ---------------------------------------------------------------------------
// 构建 Manufacturer Data（公司ID + 完整 18B 载荷），写入 g_manufData
// ---------------------------------------------------------------------------
static void buildManufDataRaw(uint16_t raw) {
    // 公司 ID little-endian
    g_manufData[0] = (uint8_t)(K797_MANUFACTURER_ID & 0xFF);       // 0x00
    g_manufData[1] = (uint8_t)((K797_MANUFACTURER_ID >> 8) & 0xFF);// 0x00
    // 固定前缀
    memcpy(&g_manufData[2], K797_PREFIX, K797_PREFIX_LEN);
    // 重量 little-endian
    g_manufData[2 + 9]  = (uint8_t)(raw & 0xFF);
    g_manufData[2 + 10] = (uint8_t)((raw >> 8) & 0xFF);
    // 尾部 7 字节
    memcpy(&g_manufData[2 + 11], K797_TRAILING, sizeof(K797_TRAILING));
}

// 构建畸形载荷：
//   Short  : 公司ID(2) + 载荷(10)：9B 前缀 + 重量低字节(1)
//            getManufacturerSpecificData(0x0000) 剥掉公司 ID 后返回 10 字节，
//            10 < 18，被解析器 payload_too_short 拒绝。
//   Prefix : 公司ID(2) + 完整 18B，但首字节 CA->CB（其余不变）
//            getManufacturerSpecificData(0x0000) 返回 18 字节，长度通过，
//            但前 9 字节与固定前缀不符，被 wrong_prefix 拒绝。
//   注：两种情况名称仍为 K797、公司 ID 仍 0x0000，扫描器仍会收到包但解析拒绝。
static void buildMalformedManufData(MalformedKind kind, uint16_t raw) {
    if (kind == MalformedKind::Short) {
        // 公司 ID + 9B 前缀 + 重量低字节 = 公司ID(2) + 载荷(10) = 共 12 字节
        // 剥公司 ID 后载荷恰好 10 字节。
        g_manufData[0] = 0x00;
        g_manufData[1] = 0x00;
        memcpy(&g_manufData[2], K797_PREFIX, K797_PREFIX_LEN); // 完整 9 字节前缀
        g_manufData[2 + K797_PREFIX_LEN] = (uint8_t)(raw & 0xFF); // 重量低字节
        return;
    }
    if (kind == MalformedKind::Prefix) {
        // 公司 ID + 破坏的首字节 + 其余前缀 + 重量 + 尾部（载荷仍 18B）
        g_manufData[0] = 0x00;
        g_manufData[1] = 0x00;
        memcpy(&g_manufData[2], K797_PREFIX, K797_PREFIX_LEN);
        g_manufData[2 + 0] = 0xCB;   // 把 CA 改成 CB
        g_manufData[2 + 9]  = (uint8_t)(raw & 0xFF);
        g_manufData[2 + 10] = (uint8_t)((raw >> 8) & 0xFF);
        memcpy(&g_manufData[2 + 11], K797_TRAILING, sizeof(K797_TRAILING));
        return;
    }
    buildManufDataRaw(raw);
}

// 返回当前模式应使用的 manufacturer data 长度（字节）
static size_t currentManufDataLen() {
    if (g_mode == Mode::Malformed && g_malKind == MalformedKind::Short) {
        return 2 + 10;  // 公司 ID(2) + 10 字节截断载荷
    }
    return sizeof(g_manufData); // 正常：公司 ID(2) + 18 载荷
}

// ---------------------------------------------------------------------------
// 应用载荷到 NimBLE 并刷新广播（运行时刷新，无 stop/start 重建）
// ---------------------------------------------------------------------------
static void applyPayloadAndRefresh() {
    if (g_adv == nullptr) return;
    // setManufacturerData 内部把数据复制进 NimBLEAdvertisementData，
    // 这里传入“公司ID + 载荷”，上线即 16 FF 00 00 <载荷>。
    bool ok = g_adv->setManufacturerData(g_manufData, currentManufDataLen());
    if (!ok) {
        // 极少发生；通常缓冲区超限。打印但不阻断。
        Serial.println(F("ERR setManufacturerData failed"));
    }
    if (g_advRunning) {
        g_adv->refreshAdvertisingData();  // 原地刷新，无需 stop/start
    }
    ++g_updateCount;
#ifdef K797_LED_PIN
    // 每次广播更新翻转状态灯（占空比=广播频率）
    digitalWrite(LED_PIN, (g_updateCount & 1U) ? HIGH : LOW);
#endif
}

// ---------------------------------------------------------------------------
// 广播启停（统一入口，处理 interval 与 connectable 模式）
// ---------------------------------------------------------------------------
static void startAdv() {
    if (g_adv == nullptr) return;
    if (g_advRunning) return;
    // 不可连接 + 不可扫描（ADV_NONCONN_IND）；不可发现避免被意外枚举
    g_adv->setConnectableMode(BLE_GAP_CONN_MODE_NON);   // 0 = non-connectable
    g_adv->setDiscoverableMode(BLE_GAP_DISC_MODE_NON);  // 0 = non-discoverable
    g_adv->enableScanResponse(false);                   // 非连接广播无 scan response
    g_adv->setAdvertisingInterval(msToAdvUnits(g_advIntervalMs));
    g_adv->start();
    g_advRunning = true;
}

static void stopAdv() {
    if (g_adv == nullptr) return;
    if (!g_advRunning) return;
    g_adv->stop();
    g_advRunning = false;
}

// ---------------------------------------------------------------------------
// 初始化广播配置（一次性，在 setup() 中调用）
// ---------------------------------------------------------------------------
static void initAdvertising() {
    NimBLEDevice::init(K797_NAME);
    NimBLEDevice::setDeviceName(K797_NAME);
    // 不创建任何 GATT Server：真实 K797 不可连接，本固件也绝不 connectable。
    g_adv = NimBLEDevice::getAdvertising();
    g_adv->setConnectableMode(BLE_GAP_CONN_MODE_NON);
    g_adv->setDiscoverableMode(BLE_GAP_DISC_MODE_NON);
    g_adv->enableScanResponse(false);
    // 名称必须在 ADV PDU 内（31B 预算正好放得下），不依赖 scan response
    g_adv->setName(K797_NAME);
    g_adv->setAdvertisingInterval(msToAdvUnits(g_advIntervalMs));
    // 初始载荷：0.0g
    buildManufDataRaw(0);
    applyPayloadAndRefresh();
}

// ===========================================================================
// 串口命令实现（每条命令一个函数，返回前已通过 Serial 打印 OK/ERR）
// ===========================================================================

static bool parseFloat(const char* s, float& out) {
    if (!s || !*s) return false;
    char* end = nullptr;
    float v = strtof(s, &end);
    if (end == s) return false;
    out = v;
    return true;
}
static bool parseULong(const char* s, uint32_t& out) {
    if (!s || !*s) return false;
    char* end = nullptr;
    unsigned long v = strtoul(s, &end, 10);
    if (end == s) return false;
    out = (uint32_t)v;
    return true;
}

// 读取一行串口到静态缓冲区，返回是否读到完整行
static char g_serialLine[96];
static size_t g_serialLen = 0;
static bool readSerialLine(String& outLine) {
    while (Serial.available()) {
        int c = Serial.read();
        if (c < 0) break;
        if (c == '\r') continue;
        if (c == '\n') {
            g_serialLine[g_serialLen] = 0;
            outLine = g_serialLine;
            g_serialLen = 0;
            return true;
        }
        if (g_serialLen < sizeof(g_serialLine) - 1) {
            g_serialLine[g_serialLen++] = (char)c;
        }
        // 超长则丢弃后续直到换行
    }
    return false;
}

// 把命令行按空格切分：返回首词（小写化），其余为参数子串
static void splitCmd(const String& line, String& cmd, const char*& arg1, const char*& arg2) {
    // 使用 String::indexOf 手工切分，避免额外堆分配链
    int sp = line.indexOf(' ');
    if (sp < 0) {
        cmd = line;
        cmd.toLowerCase();
        arg1 = nullptr;
        arg2 = nullptr;
        return;
    }
    cmd = line.substring(0, sp);
    cmd.toLowerCase();
    int restStart = sp + 1;
    int sp2 = line.indexOf(' ', restStart);
    if (sp2 < 0) {
        // 仅一个参数
        static String a1;
        a1 = line.substring(restStart);
        a1.trim();
        arg1 = a1.length() ? a1.c_str() : nullptr;
        arg2 = nullptr;
    } else {
        static String a1, a2;
        a1 = line.substring(restStart, sp2);
        a1.trim();
        a2 = line.substring(sp2 + 1);
        a2.trim();
        arg1 = a1.length() ? a1.c_str() : nullptr;
        arg2 = a2.length() ? a2.c_str() : nullptr;
    }
}

// 进入“固定重量”广播，写入并刷新
static void enterFixed(uint16_t raw) {
    g_mode = Mode::Fixed;
    g_lastRaw = raw;
    g_malKind = MalformedKind::None;
    g_noiseAmp = 0;
    buildManufDataRaw(raw);
    applyPayloadAndRefresh();
    // 若处于静默，恢复广播
    g_silenceActive = false;
    startAdv();
}

// ---- GRAMS <float> ---------------------------------------------------------
static void cmdGrams(const char* arg) {
    float g;
    if (!parseFloat(arg, g)) {
        Serial.println(F("ERR GRAMS <grams> 0..6553.5"));
        return;
    }
    if (g < 0.0f || g > GRAMS_MAX) {
        Serial.printf("ERR grams out of range (0..%.1f)\n", (double)GRAMS_MAX);
        return;
    }
    uint16_t raw = gramsToRaw(g);
    // 清除噪声/场景/畸形状态，回到固定
    g_scenario = nullptr;
    enterFixed(raw);
    Serial.printf("OK GRAMS %.1f (raw %u)\n", (double)g, raw);
}

// ---- RAW <uint16> ----------------------------------------------------------
static void cmdRaw(const char* arg) {
    uint32_t r;
    if (!parseULong(arg, r)) {
        Serial.println(F("ERR RAW <raw 0..65535>"));
        return;
    }
    if (r > GRAMS_RAW_MAX) {
        Serial.println(F("ERR raw out of range (0..65535)"));
        return;
    }
    g_scenario = nullptr;
    enterFixed((uint16_t)r);
    Serial.printf("OK RAW %u (%.1f g)\n", (unsigned)r, (double)rawToGrams((uint16_t)r));
}

// ---- ZERO ------------------------------------------------------------------
static void cmdZero() {
    g_scenario = nullptr;
    enterFixed(0);
    Serial.println(F("OK ZERO (0.0 g)"));
}

// ---- SILENCE <ms> ----------------------------------------------------------
static void cmdSilence(const char* arg) {
    uint32_t ms;
    if (!parseULong(arg, ms) || ms == 0) {
        Serial.println(F("ERR SILENCE <ms>"));
        return;
    }
    // 记住当前固定重量（恢复用），停止广播
    g_silenceActive = true;
    g_silenceUntilMs = millis() + ms;
    stopAdv();
    Serial.printf("OK SILENCE %lu ms (resume raw %u)\n",
                  (unsigned long)ms, (unsigned)g_lastRaw);
}

// ---- NOISE <grams> <amp> ---------------------------------------------------
static void cmdNoise(const char* a1, const char* a2) {
    float g, amp;
    if (!parseFloat(a1, g) || !parseFloat(a2, amp)) {
        Serial.println(F("ERR NOISE <grams> <amplitude>"));
        return;
    }
    if (g < 0.0f || g > GRAMS_MAX || amp < 0.0f) {
        Serial.println(F("ERR invalid NOISE args"));
        return;
    }
    g_scenario = nullptr;
    g_mode = Mode::Noise;
    g_malKind = MalformedKind::None;
    g_noiseCenter = gramsToRaw(g);
    g_noiseAmp    = gramsToRaw(amp);  // amp 转 raw（0.1g 步进 => raw ±1）
    g_lastRaw     = g_noiseCenter;    // STOP 时回到中心
    // 立即应用一次（中心值）
    buildManufDataRaw(g_noiseCenter);
    applyPayloadAndRefresh();
    g_silenceActive = false;
    startAdv();
    Serial.printf("OK NOISE center %.1f amp +-%.1f g (raw %u +-%u)\n",
                  (double)g, (double)amp, (unsigned)g_noiseCenter, (unsigned)g_noiseAmp);
}

// ---- INTERVAL <ms> ---------------------------------------------------------
static void cmdInterval(const char* arg) {
    uint32_t ms;
    if (!parseULong(arg, ms)) {
        Serial.println(F("ERR INTERVAL <ms 100..1000>"));
        return;
    }
    if (ms < ADV_INTERVAL_MIN_MS || ms > ADV_INTERVAL_MAX_MS) {
        Serial.println(F("ERR interval out of range (100..1000 ms)"));
        return;
    }
    g_advIntervalMs = ms;
    // 运行中变更需重启广播以应用新间隔
    if (g_advRunning) {
        stopAdv();
        startAdv();
    }
    Serial.printf("OK INTERVAL %lu ms\n", (unsigned long)ms);
}

// ---- MALFORMED short | prefix ---------------------------------------------
static void cmdMalformed(const char* arg) {
    if (!arg) { Serial.println(F("ERR MALFORMED short|prefix")); return; }
    MalformedKind k;
    // 大小写无关比较
    if (strcasecmp(arg, "short") == 0)      k = MalformedKind::Short;
    else if (strcasecmp(arg, "prefix") == 0) k = MalformedKind::Prefix;
    else { Serial.println(F("ERR MALFORMED short|prefix")); return; }

    g_mode = Mode::Malformed;
    g_malKind = k;
    g_scenario = nullptr;
    g_noiseAmp = 0;
    buildMalformedManufData(k, g_lastRaw);
    applyPayloadAndRefresh();
    g_silenceActive = false;
    startAdv();
    Serial.printf("OK MALFORMED %s active (raw %u) — send STOP to resume\n",
                  k == MalformedKind::Short ? "short" : "prefix",
                  (unsigned)g_lastRaw);
}

// ---- STOP ------------------------------------------------------------------
static void cmdStop() {
    // 清除噪声/场景/畸形/静默，回到上一个固定重量广播
    g_scenario = nullptr;
    g_scenarioLoop = false;
    g_noiseAmp = 0;
    g_malKind = MalformedKind::None;
    enterFixed(g_lastRaw);
    Serial.printf("OK STOP — resumed fixed %.1f g (raw %u)\n",
                  (double)rawToGrams(g_lastRaw), (unsigned)g_lastRaw);
}

// ---- PLAY <name> [LOOP] ----------------------------------------------------
static void cmdPlay(const char* a1, const char* a2) {
    if (!a1) { Serial.println(F("ERR PLAY <scenario> [LOOP]")); return; }
    const k797::Scenario* sc = k797::findScenario(a1);
    if (!sc) {
        Serial.printf("ERR unknown scenario: %s\n", a1);
        return;
    }
    bool loop = (sc->repeat);  // soak_cycle_60s 默认 LOOP
    if (a2 && strcasecmp(a2, "LOOP") == 0) loop = true;

    g_scenario = sc;
    g_scenarioLoop = loop;
    g_scenarioStartMs = millis();
    g_scenarioNextIdx = 0;
    g_scenarioSilenceUntilMs = 0;
    // 计算循环边界（末事件 atMs）
    uint32_t len = 0;
    for (size_t i = 0; i < sc->eventCount; ++i) {
        if (sc->events[i].atMs > len) len = sc->events[i].atMs;
    }
    g_scenarioLenMs = len;
    g_mode = Mode::Scenario;
    g_malKind = MalformedKind::None;
    g_noiseAmp = 0;
    g_silenceActive = false;
    // 立即触发首个事件（loop tick 会处理 0ms 事件）
    Serial.printf("OK PLAY %s%s (len %lu ms)\n",
                  sc->name, loop ? " LOOP" : "", (unsigned long)len);
}

// ---- STATUS ----------------------------------------------------------------
static void cmdStatus() {
    // 输出 JSON；使用静态 printf 不分配 String
    const char* modeStr =
        g_mode == Mode::Fixed     ? "fixed"  :
        g_mode == Mode::Noise     ? "noise"  :
        g_mode == Mode::Scenario  ? "scenario" :
        g_mode == Mode::Malformed ? "malformed" : "unknown";
    const char* malStr =
        g_malKind == MalformedKind::Short  ? "short"  :
        g_malKind == MalformedKind::Prefix ? "prefix" : "none";
    Serial.printf(
        "{\"device\":\"K797\",\"mode\":\"%s\",\"running\":%s,"
        "\"silenced\":%s,\"intervalMs\":%lu,\"lastRaw\":%u,\"lastGrams\":%.1f,"
        "\"noiseCenter\":%u,\"noiseAmp\":%u,\"malformed\":\"%s\","
        "\"scenario\":\"%s\",\"scenarioLoop\":%s,"
        "\"updateCount\":%lu,\"freeHeap\":%lu}\n",
        modeStr,
        g_advRunning ? "true" : "false",
        g_silenceActive ? "true" : "false",
        (unsigned long)g_advIntervalMs,
        (unsigned)g_lastRaw, (double)rawToGrams(g_lastRaw),
        (unsigned)g_noiseCenter, (unsigned)g_noiseAmp,
        malStr,
        g_scenario ? g_scenario->name : "",
        g_scenarioLoop ? "true" : "false",
        (unsigned long)g_updateCount,
        (unsigned long)ESP.getFreeHeap());
}

// ---- HELP ------------------------------------------------------------------
static void cmdHelp() {
    Serial.println(F("K797 emulator commands:"));
    Serial.println(F("  GRAMS <g>          set weight grams (0..6553.5, step 0.1)"));
    Serial.println(F("  RAW <u16>          set raw uint16 (0..65535)"));
    Serial.println(F("  ZERO               weight 0.0 g"));
    Serial.println(F("  SILENCE <ms>       stop advertising for ms, then resume"));
    Serial.println(F("  NOISE <g> <amp>    jitter +/-amp grams per update"));
    Serial.println(F("  PLAY <name> [LOOP] play built-in scenario"));
    Serial.println(F("  MALFORMED short    10-byte truncated payload until STOP"));
    Serial.println(F("  MALFORMED prefix   corrupted prefix byte (CB) until STOP"));
    Serial.println(F("  INTERVAL <ms>      advertising interval (100..1000)"));
    Serial.println(F("  STOP               stop noise/scenario/malformed, resume"));
    Serial.println(F("  STATUS             print state JSON"));
    Serial.println(F("  HELP               this list"));
    Serial.println(F("scenarios:"));
    for (size_t i = 0; i < k797::kScenarioCount; ++i) {
        Serial.printf("  %s (events=%u)%s\n",
                      k797::kScenarios[i].name,
                      (unsigned)k797::kScenarios[i].eventCount,
                      k797::kScenarios[i].repeat ? " [default LOOP]" : "");
    }
}

// ---------------------------------------------------------------------------
// 命令分发
// ---------------------------------------------------------------------------
static void dispatchCommand(const String& line) {
    String cmd; const char* a1 = nullptr; const char* a2 = nullptr;
    splitCmd(line, cmd, a1, a2);
    if (cmd.length() == 0) return;

    if      (cmd == "grams")     cmdGrams(a1);
    else if (cmd == "raw")       cmdRaw(a1);
    else if (cmd == "zero")      cmdZero();
    else if (cmd == "silence")   cmdSilence(a1);
    else if (cmd == "noise")     cmdNoise(a1, a2);
    else if (cmd == "play")      cmdPlay(a1, a2);
    else if (cmd == "malformed") cmdMalformed(a1);
    else if (cmd == "interval")  cmdInterval(a1);
    else if (cmd == "stop")      cmdStop();
    else if (cmd == "status")    cmdStatus();
    else if (cmd == "help" || cmd == "?") cmdHelp();
    else {
        Serial.printf("ERR unknown command: %s (send HELP)\n", cmd.c_str());
    }
}

// ===========================================================================
// 场景调度器：基于 millis()，处理 Weight 与 Silence 事件
// ===========================================================================
static void tickScenario() {
    if (g_mode != Mode::Scenario || g_scenario == nullptr) return;

    uint32_t now = millis();
    // 处理静默窗口
    if (g_scenarioSilenceUntilMs != 0) {
        if (now < g_scenarioSilenceUntilMs) {
            return; // 仍在静默
        }
        // 静默结束，恢复广播（保持上一个固定重量）
        g_scenarioSilenceUntilMs = 0;
        startAdv();
    }

    uint32_t elapsed = now - g_scenarioStartMs;

    // 处理所有“到点”事件（可能一帧内多个事件同时到期）
    while (g_scenarioNextIdx < g_scenario->eventCount) {
        const k797::ScenarioEvent& ev = g_scenario->events[g_scenarioNextIdx];
        if (elapsed < ev.atMs) break;

        if (ev.type == k797::EventType::Weight) {
            uint16_t raw = gramsToRaw(ev.grams);
            g_lastRaw = raw;
            buildManufDataRaw(raw);
            applyPayloadAndRefresh();
            if (g_advRunning == false && g_scenarioSilenceUntilMs == 0) {
                startAdv();
            }
        } else if (ev.type == k797::EventType::Silence) {
            // 进入静默 gap 毫秒
            g_scenarioSilenceUntilMs = now + ev.gapMs;
            stopAdv();
        }
        ++g_scenarioNextIdx;
    }

    // 播放结束处理
    if (g_scenarioNextIdx >= g_scenario->eventCount) {
        if (g_scenarioLoop && g_scenarioLenMs > 0) {
            // 循环回放：把 startMs 推进到最近的循环对齐点
            uint32_t cyclesDone = (elapsed / g_scenarioLenMs);
            if (cyclesDone < 1) cyclesDone = 1;
            g_scenarioStartMs += cyclesDone * g_scenarioLenMs;
            g_scenarioNextIdx = 0;
        } else {
            // 单次播放结束：停留在末态，切回固定模式（保留 g_lastRaw）
            g_mode = Mode::Fixed;
            g_scenario = nullptr;
            Serial.println(F("OK scenario finished"));
        }
    }
}

// ---------------------------------------------------------------------------
// NOISE 调度器：每个广播周期抖动一次
// ---------------------------------------------------------------------------
static uint32_t g_lastNoiseUpdateMs = 0;
static void tickNoise() {
    if (g_mode != Mode::Noise) return;
    uint32_t now = millis();
    // 按广播间隔节流抖动（避免 100% CPU 抖动；与广播频率对齐）
    if (now - g_lastNoiseUpdateMs < g_advIntervalMs) return;
    g_lastNoiseUpdateMs = now;

    // 在 [center - amp, center + amp] 内均匀取整 raw（0.1g 步进）
    long lo = (long)g_noiseCenter - (long)g_noiseAmp;
    long hi = (long)g_noiseCenter + (long)g_noiseAmp;
    if (lo < 0) lo = 0;
    if (hi > GRAMS_RAW_MAX) hi = GRAMS_RAW_MAX;
    // 伪随机：用 esp_random()（硬件 RNG，无状态、无堆）
    uint32_t span = (uint32_t)(hi - lo + 1);
    uint16_t raw = (uint16_t)(lo + (esp_random() % span));
    buildManufDataRaw(raw);
    applyPayloadAndRefresh();
}

// ---------------------------------------------------------------------------
// SILENCE 恢复检查
// ---------------------------------------------------------------------------
static void tickSilence() {
    if (!g_silenceActive) return;
    if (millis() >= g_silenceUntilMs) {
        // 静默结束，恢复上一个固定重量广播
        g_silenceActive = false;
        buildManufDataRaw(g_lastRaw);
        applyPayloadAndRefresh();
        startAdv();
        Serial.printf("OK silence ended, resumed %.1f g (raw %u)\n",
                      (double)rawToGrams(g_lastRaw), (unsigned)g_lastRaw);
    }
}

// ===========================================================================
// Arduino 入口
// ===========================================================================
void setup() {
    Serial.begin(115200);
    // 不阻塞等待串口（脱离 USB 也能跑）

#ifdef K797_LED_PIN
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, LOW);
#endif

    initAdvertising();
    startAdv();

    delay(100);  // 让 NimBLE 稳定
    Serial.println();
    Serial.println(F("========================================"));
    Serial.println(F("K797 BLE emulator (ESP32-C6, NimBLE 2.x)"));
    Serial.println(F("non-connectable ADV_NONCONN_IND, no GATT"));
    Serial.printf("interval %lu ms, payload %u bytes, name \"%s\", mfgId 0x%04X\n",
                  (unsigned long)g_advIntervalMs, K797_PAYLOAD_LEN,
                  K797_NAME, (unsigned)K797_MANUFACTURER_ID);
    Serial.println(F("send HELP for commands"));
    Serial.println(F("========================================"));
}

void loop() {
    // 单事件循环：串口命令 → 静默恢复 → 场景 → 噪声
    // 串口命令和广播更新在此串行执行，载荷构建期间无并发改写（无需互斥）。
    String line;
    if (readSerialLine(line)) {
        dispatchCommand(line);
    }
    tickSilence();
    tickScenario();
    tickNoise();

    // 轻量让出：避免 100% CPU；ESP32-C6 单核 Arduino 无 RTOS yield 需求，
    // 但 delay(1) 给 NimBLE 协议栈留出处理窗口。
    delay(1);
}
