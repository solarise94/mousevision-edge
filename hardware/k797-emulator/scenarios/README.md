# K797 emulator scenarios

JSON 场景文件，是计划 `docs/HARMONYOS_K797_BLE_INTEGRATION_PLAN.md` §9.2 要求的 10 个场景的人类可读、可交叉核对的权威定义。覆盖正常称量、异常时序与长时浸泡。

## 与固件的关系

`src/scenarios.h` 持有 **固件内置、编译进二进制的场景**（静态结构体表，避免堆分配以支持 4 小时连续运行无内存增长）。固件通过串口 `PLAY <name> [LOOP]` 播放这些内置场景（见 `src/main.cpp`）。

本目录的 JSON 文件是 **权威的人类可读定义**，固件当前不读取它们。两者中重合的 4 个场景已对齐，保持一致：

- `mouse_normal_26_3g.json` ↔ `kMouseNormal26_3g`
- `mouse_batch_5.json` ↔ `kMouseBatch5`
- `broadcast_gap_12s.json` ↔ `kBroadcastGap12s`
- `soak_cycle_60s.json` ↔ `kSoakCycle60s`

修改固件内置场景时，请同步更新对应 JSON；新增场景先在 JSON 中定义，便于 `ScriptScaleSource` 直接加载。

## 场景格式

```json
{
  "name": "mouse_normal_26_3g",
  "repeat": false,
  "description": "...",
  "_note": "...(可选，仅异常场景)...",
  "events": [
    { "atMs": 0, "grams": 0.0 },
    { "atMs": 4000, "silence": true, "durationMs": 12000 }
  ]
}
```

事件类型（与 `scenarios.h` 语义一致）：

- 重量事件 `{ "atMs": N, "grams": X.X }`：到点把固定重量设为该值（克，1 位小数）。
- 静默事件 `{ "atMs": N, "silence": true, "durationMs": M }`：到点停止广播 M 毫秒，结束后自动恢复到上一个固定重量。对应固件 `EventType::Silence`（字段 `gapMs`）。

时间轴基于 `millis()` 单调时钟；`repeat: true` 的场景按末事件 `atMs` 循环回放。

## 场景清单

| 文件 | 名称 | repeat | 用途（计划 §9.2） |
| --- | --- | --- | --- |
| `mouse_normal_26_3g.json` | mouse_normal_26_3g | false | 1. 正常上秤、稳定、离开 |
| `mouse_fast_jump.json` | mouse_fast_jump | false | 2. 重量快速跳变后稳定 |
| `mouse_unstable_leave.json` | mouse_unstable_leave | false | 3. 鼠未稳定就离开 |
| `mouse_reweigh_same.json` | mouse_reweigh_same | false | 4. 同一只重称 |
| `mouse_batch_5.json` | mouse_batch_5 | false | 5. 连续多只 |
| `broadcast_gap_12s.json` | broadcast_gap_12s | false | 6. 10 秒以上广播中断 |
| `repeated_out_of_order.json` | repeated_out_of_order | false | 7. 重复与乱序读数（receiver 侧校验） |
| `permission_revoked.json` | permission_revoked | false | 8. 运行中权限撤销（停止广播不恢复） |
| `soak_cycle_60s.json` | soak_cycle_60s | true | 9. 4 小时长时运行（60s 循环 ×240） |
| `ws_reconnect.json` | ws_reconnect | false | 10. 网页/WebSocket 断线重连（页面侧 latest-only） |

异常场景（6/7/8/10）在各自 JSON 的 `_note` 字段说明了被测试的 receiver 侧行为。
