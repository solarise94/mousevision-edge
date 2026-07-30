// =============================================================================
// scenarios.h — K797 虚拟硬件内置场景定义
// -----------------------------------------------------------------------------
// 场景以静态结构体表形式固件内置，避免堆分配（4 小时连续运行无内存增长）。
// 与 HarmonyOS 侧 ScriptScaleSource 的 {name, repeat, events[]} 语义一致；
// scenarios/*.json 是人类可读的交叉核对副本，固件不读取它们。
//
// 事件类型：
//   - 重量事件 atMs -> grams（把当前固定重量设为该值）
//   - 静默事件 atMs -> gap(ms)（停止广播 gap 毫秒，恢复后回到上一个固定重量）
//
// 时间轴基于 millis() 单调时钟；LOOP 时按总时长（最后一个事件 atMs）循环回放。
// =============================================================================

#pragma once

#include <cstdint>
#include <stddef.h>

namespace k797 {

// 事件类型标记
enum class EventType : uint8_t {
    Weight = 0,   // 设置固定重量（克）
    Silence = 1,  // 停止广播 N 毫秒后自动恢复
};

// 单条时间轴事件（8 字节，静态布局）
struct ScenarioEvent {
    uint32_t  atMs;     // 触发时间（相对场景起点）
    EventType type;
    // union 在 constexpr 初始化表中不如直接命名成员直观，故并列使用：
    float     grams;    // type==Weight 时有效
    uint32_t  gapMs;    // type==Silence 时有效（静默时长）
};

// 场景定义（静态，不可变）
struct Scenario {
    const char*         name;
    bool                repeat;   // 是否默认循环
    const ScenarioEvent* events;
    size_t              eventCount;
};

// ---------------------------------------------------------------------------
// mouse_normal_26_3g：单只鼠正常上秤 → 稳定 → 离开
//   0→0.0, 1500→8.1, 1900→19.7, 2400→26.2, 2800→26.3,
//   3200→26.3, 3600→26.3, 9000→0.0
// ---------------------------------------------------------------------------
inline constexpr ScenarioEvent kMouseNormal26_3g[] = {
    {   0, EventType::Weight,  0.0f, 0    },
    {1500, EventType::Weight,  8.1f, 0    },
    {1900, EventType::Weight, 19.7f, 0    },
    {2400, EventType::Weight, 26.2f, 0    },
    {2800, EventType::Weight, 26.3f, 0    },
    {3200, EventType::Weight, 26.3f, 0    },
    {3600, EventType::Weight, 26.3f, 0    },
    {9000, EventType::Weight,  0.0f, 0    },
};

// ---------------------------------------------------------------------------
// broadcast_gap_12s：稳定重量后产生 12 秒广播中断（验证 stale/恢复）
//   2000→26.3，4000 处静默 12000ms，16000 恢复 26.3，20000→0.0
//   注：恢复点 16000 = 4000 + 12000，与静默事件语义一致。
// ---------------------------------------------------------------------------
inline constexpr ScenarioEvent kBroadcastGap12s[] = {
    { 2000, EventType::Weight, 26.3f, 0      },
    { 4000, EventType::Silence, 0.0f, 12000  },
    {16000, EventType::Weight, 26.3f, 0      },
    {20000, EventType::Weight,  0.0f, 0      },
};

// ---------------------------------------------------------------------------
// mouse_batch_5：连续五只鼠，中间有清晰间隔（空秤归零）
//   18.2 / 21.0 / 24.6 / 26.3 / 29.9 克，每只间隔 1500ms 空秤
// ---------------------------------------------------------------------------
inline constexpr ScenarioEvent kMouseBatch5[] = {
    {    0, EventType::Weight,  0.0f, 0 },
    { 1000, EventType::Weight, 18.2f, 0 },
    { 3000, EventType::Weight,  0.0f, 0 },  // 第一只离开
    { 4500, EventType::Weight, 21.0f, 0 },
    { 6500, EventType::Weight,  0.0f, 0 },
    { 8000, EventType::Weight, 24.6f, 0 },
    {10000, EventType::Weight,  0.0f, 0 },
    {11500, EventType::Weight, 26.3f, 0 },
    {13500, EventType::Weight,  0.0f, 0 },
    {15000, EventType::Weight, 29.9f, 0 },
    {17000, EventType::Weight,  0.0f, 0 },
};

// ---------------------------------------------------------------------------
// soak_cycle_60s：60 秒称量/清零循环，专为 LOOP 设计（4 小时浸泡）。
//   0→0.0，5000→25.0，15000→25.0，17000→0.0，
//   30000→30.0，40000→30.0，42000→0.0，
//   60000→0.0（循环边界，与起点 0.0 对齐）
//   60000ms 周期 × 240 次 ≈ 4 小时。
// ---------------------------------------------------------------------------
inline constexpr ScenarioEvent kSoakCycle60s[] = {
    {    0, EventType::Weight,  0.0f, 0 },
    { 5000, EventType::Weight, 25.0f, 0 },
    {15000, EventType::Weight, 25.0f, 0 },
    {17000, EventType::Weight,  0.0f, 0 },
    {30000, EventType::Weight, 30.0f, 0 },
    {40000, EventType::Weight, 30.0f, 0 },
    {42000, EventType::Weight,  0.0f, 0 },
    {60000, EventType::Weight,  0.0f, 0 },
};

// 场景注册表（线性查找；场景数量少，无需哈希）
inline constexpr Scenario kScenarios[] = {
    {"mouse_normal_26_3g", false, kMouseNormal26_3g, sizeof(kMouseNormal26_3g) / sizeof(ScenarioEvent)},
    {"broadcast_gap_12s",  false, kBroadcastGap12s,  sizeof(kBroadcastGap12s)  / sizeof(ScenarioEvent)},
    {"mouse_batch_5",      false, kMouseBatch5,      sizeof(kMouseBatch5)      / sizeof(ScenarioEvent)},
    {"soak_cycle_60s",     true,  kSoakCycle60s,     sizeof(kSoakCycle60s)     / sizeof(ScenarioEvent)},
};

inline constexpr size_t kScenarioCount = sizeof(kScenarios) / sizeof(Scenario);

// 按名称查找场景（nullptr 表示未找到）
inline const Scenario* findScenario(const char* name) {
    if (!name) return nullptr;
    for (size_t i = 0; i < kScenarioCount; ++i) {
        // 简单 strcmp（固件内自实现，避免拉 <cstring> 依赖差异）
        const char* a = kScenarios[i].name;
        const char* b = name;
        while (*a && *b && *a == *b) { ++a; ++b; }
        if (*a == 0 && *b == 0) return &kScenarios[i];
    }
    return nullptr;
}

} // namespace k797
