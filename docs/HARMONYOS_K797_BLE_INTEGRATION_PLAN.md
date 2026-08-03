# MiceAutomatic K797 蓝牙天平双端接入计划（鸿蒙优先）

> 状态：规划中
> 实现顺序：MatePad Mini HarmonyOS 6.1 原生版 → 畅享 70 Pro HarmonyOS 4.2/Android 兼容版

## 1. 文档目的

本文定义 MiceAutomatic 在两类华为设备上读取 K797 蓝牙天平广播、向现有手机网页和后端提供实时重量的实施方案。

| 测试设备 | 系统 | 交付形态 | 定位 |
| --- | --- | --- | --- |
| MatePad Mini | HarmonyOS 6.1 | ArkTS 原生 HAP | 首要开发与现场测试设备 |
| 华为畅享 70 Pro（CTR-AL20） | HarmonyOS 4.2 | Kotlin/Android APK | 第二阶段兼容与回归设备 |

两端不是同一个安装包，而是共享同一套网页、后端、JSON 契约、协议 fixture 和虚拟硬件，再分别提供很薄的原生 BLE 壳。MatePad Mini 先完成鸿蒙原生实现；畅享 70 Pro 随后移植 Android 设备适配层。即使 HarmonyOS 5/6 设备可通过卓易通运行部分 APK，也不把该兼容层作为持续 BLE 扫描和 Web Bridge 的正式交付路径。

核心约束是：**真实天平不适合作为日常开发依赖**。K797 只在最终协议核对和现场验收时短暂开机；协议解析、页面交互、断流处理、实时状态机和长时间稳定性测试必须能依靠软件模拟器与 BLE 虚拟硬件独立完成。

第一阶段采用 HarmonyOS 原生 HAP 应用，不先做元服务。原因是现场称量可能持续数小时，需要前台持续扫描 BLE、保持 Web 页面和录像状态，并稳定处理权限与生命周期。鸿蒙版验证完成后再制作 Android APK；最后再评估元服务形态和 AppGallery Connect 分发。

设备与兼容性参考：

- [HarmonyOS 6 支持机型](https://consumer.huawei.com/cn/support/harmonyos/models-6/)
- [HarmonyOS 5/6 安装 Android 应用的兼容条件](https://consumer.huawei.com/cn/support/content/zh-cn16061787/)
- [华为畅享 70 Pro HarmonyOS 4.2 支持信息](https://consumer.huawei.com/cn/support/content/zh-cn15983622/)

## 2. 已确认的 K797 协议

| 项目 | 已确认值 |
| --- | --- |
| BLE Local Name | `K797` |
| Manufacturer ID | `0x0000` |
| Manufacturer Data 前缀 | `CA E8 03 28 08 95 CA 02 10` |
| 连接属性 | 不可连接，`Connectable: No` |
| 重量字段 | payload `[9]`、`[10]` |
| 字节序 | little-endian |
| 重量换算 | `UInt16LE / 10.0` 克 |
| 当前已知最小 payload 长度 | 18 字节 |

示例：

```text
payload[9..10] = 07 01
raw = 0x0107 = 263
grams = 26.3 g
```

稳定设备识别条件必须同时满足：

```text
Local Name == K797
Manufacturer ID == 0x0000
Manufacturer Data 以 CA E8 03 28 08 95 CA 02 10 开头
```

BLE 地址可能是随机私有地址，只能用于诊断，不能作为设备主键。应用不得调用 GATT `connect()`，也不得要求配对。

## 3. 总体架构

```text
┌────────────────────────────┐       ┌────────────────────────────┐
│ HarmonyOS 原生壳            │       │ Android 原生壳              │
│ BleK797Source.ets           │       │ K797BleScanner.kt           │
│ Web 组件 + ArkTS H5 Bridge   │       │ WebView + Kotlin Bridge     │
└─────────────┬──────────────┘       └─────────────┬──────────────┘
              └──────────────────┬─────────────────┘
                                 │ 相同 ScaleReading / ScaleStatus JSON
                    ┌────────────▼─────────────┐
                    │ MiceAutomatic /mobile    │
                    │ 录像、显示、WebSocket     │
                    └────────────┬─────────────┘
                                 │ scale_reading
                    ┌────────────▼─────────────┐
                    │ FastAPI 实时会话          │
                    │ BLE 重量主源 + 视频证据    │
                    └──────────────────────────┘

两端各自实现相同的 `ScaleSource` 抽象，并包含 `ble / replay / script` 三种来源。协议 fixture 和模拟场景共用；平台代码只负责扫描权限、生命周期与网页桥接。
```

设计原则：

1. BLE 与 UI、业务状态机解耦；真实天平、广播回放和脚本模拟产生相同的 `ScaleReading`。
2. BLE 重量是实时称量的主重量源；摄像头继续录像并提供鼠只/现场证据。
3. OCR 保留为普通浏览器或 BLE 不可用时的显式降级模式，不能在 BLE 断流时静默接管。
4. 没收到广播表示 `stale`，绝不能解释为 `0 g`。
5. 原始广播、解析结果、来源和时间戳均保留，便于审计和后续协议修正。

## 4. 双端应用形态与工程结构

建议采用两个独立原生工程：

```text
harmonyos/MiceAutomaticScale/
├── AppScope/
├── entry/
│   └── src/main/ets/
│       ├── entryability/EntryAbility.ets
│       ├── pages/Index.ets
│       ├── scale/ScaleSource.ets
│       ├── scale/ScaleReading.ets
│       ├── scale/K797AdvertisementParser.ets
│       ├── scale/BleK797Source.ets
│       ├── scale/ReplayScaleSource.ets
│       ├── scale/ScriptScaleSource.ets
│       ├── bridge/ScaleWebBridge.ets
│       └── diagnostics/ScaleDiagnostics.ets
├── hvigor/
├── build-profile.json5
├── oh-package.json5
└── README.md

android/MiceAutomaticScale/
├── app/src/main/java/.../
│   ├── MainActivity.kt
│   ├── scale/K797AdvertisementParser.kt
│   ├── scale/K797BleScanner.kt
│   ├── scale/ReplayScaleSource.kt
│   ├── scale/ScriptScaleSource.kt
│   └── bridge/ScaleWebBridge.kt
├── app/src/test/
├── build.gradle.kts
└── README.md
```

工程采用 Stage 模型和 ArkTS。目标 API 以 MatePad Mini 实际系统支持版本为准，在第 0 阶段连接真机后确定；不得在未确认设备 API Level 前固定到最新 HarmonyOS 7 API。

两个原生壳共同负责：

- 请求和管理蓝牙扫描权限；
- 持续扫描不可连接 BLE 广播；
- 解析 K797 Manufacturer Data；
- 维护 `scanning / active / stale / off / unauthorized / error` 状态；
- 加载现有 `https://weight.pingoodmice.top:16206/mobile`；
- 通过 ArkTS H5 Bridge 或 Kotlin WebView Bridge 向网页推送相同的标准 JSON；
- debug 构建中提供模拟场景选择和原始包查看；
- 页面退出、应用切后台或权限撤销时正确停止扫描。

华为官方文档提供 BLE 扫描能力及 ArkTS 与 H5 交互路径。实现时以安装在 DevEco Studio 内的目标 SDK API 参考为准，避免复制已废弃的 `@ohos.bluetooth` 示例。参考：

- [HarmonyOS 开发文档中心](https://developer.huawei.com/consumer/cn/doc/)
- [ArkTS 与 H5 交互资源](https://developer.huawei.com/consumer/cn/arkts/resources/)
- [HarmonyOS BLE Codelab](https://developer.huawei.com/consumer/en/codelab/HarmonyOS-BLE/)

Android 端以 Android SDK 的 `BluetoothLeScanner`、`ScanRecord.getManufacturerSpecificData()` 和 WebView JavaScript Bridge 实现。两个工程可共享规范和测试向量，但不要尝试共享 ArkTS/Kotlin 源码或生成一个“通吃”安装包。

## 5. 统一领域模型

### 5.1 ScaleReading

所有真实和虚拟来源统一输出：

```json
{
  "schemaVersion": 1,
  "device": "K797",
  "deviceKey": "k797:0000:cae803280895ca0210",
  "grams": 26.3,
  "raw": 263,
  "rssi": -49,
  "receivedAt": "2026-07-30T06:36:30.194Z",
  "receivedAtEpochMs": 1785393390194,
  "sequence": 1248,
  "stable": true,
  "stableSource": "derived_repeat",
  "source": "ble",
  "address": "diagnostic-only",
  "payloadHex": "CA E8 03 28 08 95 CA 02 10 07 01 00 00 00 00 00 00 00"
}
```

字段约束：

- `grams` 直接由协议换算，不在原生侧做业务修正；
- `stable` 目前不是已确认的协议位，只能由重复广播派生，因此必须带 `stableSource=derived_repeat`；
- 后端仍用自己的连续读数窗口决定可播报重量，不能盲信原生 `stable`；
- `source` 取 `ble / replay / script / hardware_emulator`；
- `sequence` 为应用进程内单调序号，不使用随机 BLE 地址；
- `payloadHex` 只用于诊断和数据集积累，正式日志应设容量上限。

### 5.2 ScaleStatus

```json
{
  "device": "K797",
  "state": "active",
  "message": "已收到 K797 广播",
  "lastReadingAtEpochMs": 1785393390194,
  "source": "ble"
}
```

状态定义：

| 状态 | 含义 |
| --- | --- |
| `off` | 用户未启动或已停止扫描 |
| `scanning` | 扫描已启动，但尚未收到合法 K797 包 |
| `active` | 最近收到合法包 |
| `stale` | 超过 15 秒未收到合法包 |
| `unauthorized` | 权限未授权或被撤销 |
| `bluetooth_off` | 系统蓝牙关闭 |
| `error` | 扫描器异常 |

`stale` 计时使用单调时钟；对外审计时间同时保存系统 epoch 时间。

## 6. K797 解析器设计

`K797AdvertisementParser` 必须是无系统依赖的纯函数：

```text
parse(localName, manufacturerId, payloadBytes, rssi, receivedAt)
  -> ScaleReading | ParseReject
```

拒绝原因使用枚举而不是异常文本：

```text
wrong_name
wrong_manufacturer_id
payload_too_short
wrong_prefix
weight_out_of_protocol_range
```

解析步骤：

1. 校验 Local Name；
2. 校验 Manufacturer ID；
3. 校验 payload 长度至少为 18；
4. 常量时间比较前 9 字节；
5. `raw = payload[9] | payload[10] << 8`；
6. `grams = raw / 10.0`；
7. 生成不可变 `ScaleReading`。

初版不要自行解释 payload `[11..17]`。在真实天平验收时分别采集空秤、多个重量、低电、关机前和超载状态的完整包，再决定这些字节是否包含电量、符号、单位或稳定标记。

## 7. H5 Bridge 契约

网页只依赖统一事件，不直接依赖 ArkTS 对象名称：

```javascript
window.addEventListener("miceautomatic:scale-reading", (event) => {
  const reading = event.detail;
});

window.addEventListener("miceautomatic:scale-status", (event) => {
  const status = event.detail;
});
```

网页调用原生侧的最小命令：

```text
startScaleScan()
stopScaleScan()
getScaleStatus()
```

仅 debug 构建增加：

```text
selectScaleSource("ble" | "replay" | "script")
loadScaleScenario(name)
```

安全要求：

- Web 组件只允许 `weight.pingoodmice.top:16206` 的 HTTPS 页面使用 Bridge；
- 拦截并拒绝非白名单主机导航；
- release 构建不暴露脚本注入、任意文件读取或任意 ArkTS 方法调用；
- Bridge 使用明确的数据结构，不接受网页传入可执行代码；
- 页面加载完成后再注册回调，重载页面时重新握手；
- 原生侧推送过快时最多以 5–10 Hz 合并刷新 UI，但审计层可保留原始包计数。

## 8. MiceAutomatic 网页与后端接入

### 8.1 手机网页

现有 `/mobile/record` 增加重量源抽象：

```text
native_ble   鸿蒙/Android 原生桥接
ocr          现有视频 OCR
simulation   debug 模式
```

页面行为：

- 检测到鸿蒙 Bridge 时默认选择 `native_ble`；
- 明确显示“蓝牙天平 K797”，不能让用户误以为重量来自 OCR；
- 展示当前重量、最后广播时间、RSSI 和状态；
- 10 秒无合法包显示 `--` 和“天平广播中断”，不显示 `0.0`；
- 真正收到 `raw=0` 时才显示 `0.0 g`；
- WebSocket 断开时缓存最新一条读数，重连后只发送最新值，不补发整段过期队列；
- 页面销毁或结束会话时通知原生停止扫描。

### 8.2 实时 WebSocket

创建会话时声明重量源：

```json
{
  "cage_id": "C57-023",
  "project_id": "default",
  "weight_source": "ble_k797"
}
```

网页向现有实时 WebSocket 发送：

```json
{
  "type": "scale_reading",
  "source": "ble_k797",
  "grams": 26.3,
  "raw": 263,
  "client_ts_ms": 12840,
  "received_at_epoch_ms": 1785393390194,
  "sequence": 1248,
  "stable": true,
  "rssi": -49
}
```

后端要求：

- 校验消息类型、数值范围、有限浮点数、序号和时间单调性；
- 会话声明 `ble_k797` 后，帧处理使用最新且未过期的 BLE 重量替代 OCR；
- 视频帧仍用于录像、画面质量和鼠只证据；
- BLE 超时后暂停状态推进并返回 `scale_stale` 提示；
- 不能在 BLE 会话里静默回退到 OCR；
- 最终 `record.json` 写入 `weight_source=ble_k797`、原始值、来源协议版本和时间证据；
- 尝试记录和 journal 同样保存重量来源，确保重启恢复后不丢来源信息。

### 8.3 稳定判定

真实天平会重复广播。后端继续使用独立读数稳定窗：

- 至少 3 条独立、单调序号的读数；
- 时间跨度和最大年龄沿用实时配置；
- 重量跨度默认不超过 0.10 g；
- 原生 `stable` 只能加权或展示，不得替代后端判定；
- 广播重复、乱序和旧 epoch 读数不得成为新证据。

## 9. 不依赖真实天平的测试体系

### 9.1 第一层：协议单元测试

将真实和人工构造的广播 payload 固化为 fixture：

```text
tests/fixtures/k797_ble/
├── manifest.json
├── zero.hex
├── 26_3g.hex
├── wrong_prefix.hex
├── short_payload.hex
└── captured_raw.jsonl
```

最低测试：

- `07 01` 解析为 `26.3 g`；
- little-endian 不得反转；
- 错误名称、Manufacturer ID、前缀和短包全部拒绝；
- `raw=0` 是真实零值；
- 重复广播产生递增 sequence；
- 未确认的尾部字节不影响重量解析。

ArkTS、Kotlin 与 Python 各实现同一组 fixture 测试，保证原生双端和服务端协议一致。

### 9.2 第二层：应用内虚拟天平

`ScriptScaleSource` 从场景文件产生重量，不经过 BLE 硬件：

```json
{
  "name": "mouse_normal_26_3g",
  "repeat": false,
  "events": [
    { "atMs": 0, "grams": 0.0 },
    { "atMs": 1500, "grams": 8.1 },
    { "atMs": 1900, "grams": 19.7 },
    { "atMs": 2400, "grams": 26.2 },
    { "atMs": 2800, "grams": 26.3 },
    { "atMs": 3200, "grams": 26.3 },
    { "atMs": 3600, "grams": 26.3 },
    { "atMs": 9000, "grams": 0.0 }
  ]
}
```

必须准备以下场景：

1. 正常上秤、稳定、离开；
2. 重量快速跳变后稳定；
3. 鼠未稳定就离开；
4. 同一只重称；
5. 连续多只；
6. 10 秒以上广播中断；
7. 重复与乱序读数；
8. 运行中权限撤销；
9. 4 小时长时运行；
10. 网页/WebSocket 断线重连。

`ReplayScaleSource` 则回放真实天平采集的 `captured_raw.jsonl`，用于复现协议问题。两者均走相同 Bridge 和后端路径，禁止在 UI 层直接伪造显示结果。

### 9.3 第三层：ESP32-C3 BLE 虚拟硬件

建议使用一块 ESP32-C3 开发板模拟真实 K797 广播。未来新增：

```text
hardware/k797-emulator/
├── README.md
├── platformio.ini
├── src/main.cpp
└── scenarios/*.json
```

模拟器广播要求：

| 参数 | 值 |
| --- | --- |
| Local Name | `K797` |
| Manufacturer ID | `0x0000`，在 AD 数据中按 BLE 规范 little-endian 编码 |
| payload | 18 字节；前 9 字节为固定前缀 |
| 重量字段 | payload `[9..10]`，little-endian |
| 广播类型 | non-connectable，不建立 GATT Server |
| 广播间隔 | 默认 200 ms，可配置 |

ESP32 通过 USB 串口接受命令：

```text
GRAMS 26.3
RAW 263
ZERO
SILENCE 12000
NOISE 26.3 0.1
PLAY mouse_normal_26_3g
MALFORMED short
MALFORMED prefix
STOP
```

该设备分别验证“鸿蒙/Android 真实 BLE 扫描 → Manufacturer Data → Parser → Bridge”整条链路，同时避免长时间开启真实天平。

### 9.4 第四层：真实 K797 短时验收

真实天平只用于：

- 确认 Local Name、Manufacturer ID、payload 长度和字节偏移；
- 采集 0 g、多个已知重量和变化过程的完整原始包；
- 对比 ESP32 模拟器与真实包；
- 验证广播频率、重复行为、RSSI 和关机后的 stale 行为；
- 进行一次完整称量录像并确认落库重量来源。

预计每次协议验收只需开机 10–20 分钟，不作为持续集成条件。

## 10. ESP32 虚拟硬件验收标准

模拟器必须做到：

- 鸿蒙应用和 Android 应用均将其识别为 `K797`；
- 不可连接，扫描器从不触发连接流程；
- 指定 `RAW 263` 后页面在 1 秒内显示 `26.3 g`；
- `SILENCE 12000` 后页面与后端进入 stale，且不产生 `0 g`；
- 恢复广播后无需重启应用即可回到 active；
- 错误前缀和短包被计入诊断拒绝数，但不污染重量；
- 连续运行 4 小时不出现内存无界增长、事件队列堆积或重复记录。

## 11. 开发阶段与交付物

### 阶段 0：真机与工具链确认

- 安装 DevEco Studio 和目标 HarmonyOS SDK；
- MatePad Mini 开启开发者模式，通过 `hdc` 识别；
- 读取系统版本、API Level 和 BLE 能力；
- 配置自动签名并安装空壳 HAP；
- 记录真机 Web 组件摄像头、文件上传和 HTTPS 兼容性。
- 记录畅享 70 Pro 的 Android API Level、`adb` 调试能力和 BLE 扫描权限要求，暂不阻塞鸿蒙开发。

验收：空壳应用能加载 `/mobile`，网页录像权限正常。

### 阶段 1：领域模型与纯解析器

- 实现 `ScaleReading`、`ScaleStatus`、`ScaleSource`；
- 实现纯函数 `K797AdvertisementParser`；
- 建立跨语言 fixture；
- 完成 ArkTS/Python 协议测试。

验收：无需蓝牙硬件即可通过全部解析测试。

### 阶段 2：软件模拟源与诊断页

- 实现 `ScriptScaleSource`、`ReplayScaleSource`；
- debug 构建提供场景选择、开始、暂停、倍速和原始事件查看；
- 实现 stale、权限错误和断流状态机。

验收：无需平板 BLE 和真实天平，能演练正常称量及全部异常场景。

### 阶段 3：鸿蒙 BLE 扫描

- 实现 `BleK797Source`；
- 权限、蓝牙开关和生命周期处理；
- 开启重复广播；
- 只按名称 + Manufacturer ID + 前缀接收；
- 不调用连接接口。

验收：先用 ESP32 模拟器跑通，再用真实 K797 短时核对。

### 阶段 4：Web 组件与 H5 Bridge

- 加载现有 `/mobile`；
- 建立 reading/status 双向契约；
- 白名单限制与 release/debug 能力隔离；
- 页面展示 BLE 来源和 stale 状态。

验收：脚本源、回放源、ESP32 和真实天平在网页侧表现一致。

### 阶段 5：后端实时状态机接入

- 会话创建增加 `weight_source`；
- WebSocket 接受 `scale_reading`；
- BLE 主源替换 OCR 重量输入；
- journal、record、manifest 保存来源；
- BLE stale 时暂停而非回退。

验收：模拟场景产生正确 Attempt，最终记录标记 `weight_source=ble_k797`。

### 阶段 6：长时与现场验收

- 应用内模拟器运行 4 小时；
- ESP32 模拟器运行 4 小时；
- 真机 K797 完整称量批次；
- 验证录像、重量、序号、重称、断流和恢复。

验收：真实天平短时结果与模拟器结果一致，日常测试不再依赖天平开机。

### 阶段 7：Android 兼容版移植

- 使用 Android Studio 建立 Kotlin APK 工程；
- 按相同 fixture 实现 `K797AdvertisementParser.kt`；
- 实现 `K797BleScanner.kt`、扫描权限和前台生命周期；
- 实现与鸿蒙完全相同的 reading/status Bridge 契约；
- 使用同一 ESP32-C3 模拟器完成断流、恢复和 4 小时测试；
- 在畅享 70 Pro HarmonyOS 4.2 真机完成 APK 安装与完整称量回归。

验收：同一模拟场景在 HAP 与 APK 上产生等价 JSON 事件和后端记录，网页与后端无需平台分支。

### 阶段 8：分发

- 开发阶段使用 DevEco 真机调试包；
- Android 兼容版先使用签名测试 APK；
- 少量实验室设备可先使用邀请测试；
- 流程稳定后再完成备案、隐私声明和正式上架；
- 不接入非必要的付费 HMS 云服务。

## 12. 自动化测试矩阵

| 层级 | 测试对象 | 不需要真实天平 | CI 可运行 |
| --- | --- | --- | --- |
| Python 单元测试 | payload 解析、WS 校验、状态机 | 是 | 是 |
| ArkTS 单元测试 | payload 解析、source 状态 | 是 | 是 |
| Kotlin 单元测试 | payload 解析、source 状态 | 是 | 是 |
| H5 测试 | Bridge 事件、stale UI、重连 | 是 | 是 |
| 鸿蒙应用内脚本源/广播回放 | HAP 完整业务流程 | 是 | 真机/模拟器 |
| Android 应用内脚本源/广播回放 | APK 完整业务流程 | 是 | 真机/模拟器 |
| ESP32 模拟器 | 两端实际 BLE 无线链路 | 是 | 半自动 |
| 真实 K797 | 两端最终协议与现场验收 | 否 | 否 |

必须新增的服务端测试：

- 非 BLE 会话拒绝 `scale_reading` 或忽略并记录；
- NaN、Infinity、负数、超范围重量、倒序 sequence 被拒绝；
- stale 读数不进入稳定窗；
- BLE 会话不调用 OCR 读取重量；
- 广播恢复后状态机继续工作；
- 重试清空旧 epoch BLE 证据；
- 最终记录保存 `ble_k797` 来源；
- 旧浏览器 OCR 会话契约完全不变。

## 13. 真机数据采集清单

首次连接真实 K797 时，将广播原样保存为 JSONL，不只记录解析后的重量。建议依次采集：

1. 空秤 30 秒；
2. 约 10 g、20 g、30 g 各 30 秒；
3. 缓慢加重和快速放置；
4. 物体保持不动 2 分钟；
5. 拿走物体后 30 秒；
6. 低电量状态（以后有条件时）；
7. 超载或负号状态仅在厂家允许且安全时测试；
8. 天平自动关机前后。

每条原始记录至少包含：完整 Manufacturer Data、Local Name、RSSI、接收时间、系统单调时间和观察到的秤面重量。采集文件脱敏后加入 fixture。

## 14. 风险与保护措施

| 风险 | 保护措施 |
| --- | --- |
| Manufacturer ID `0x0000` 可能被其他设备复用 | 名称 + ID + 9 字节前缀联合识别 |
| BLE 地址轮换 | 不作为设备主键 |
| 广播丢包 | 重复扫描；后端按新鲜度和序号处理 |
| 天平关机被误判为 0 | stale 与真实零值严格分离 |
| 原生 `stable` 推断错误 | 后端独立稳定窗为最终判据 |
| 页面切后台导致扫描暂停 | 称量页明确保持前台；恢复时重新扫描并显示状态 |
| 模拟器与真实协议偏差 | 真实包回放 + 短时 K797 对照验收 |
| debug 注入进入正式包 | 编译变体隔离，release 不注册模拟命令 |
| BLE 读数与视频时间不一致 | 同时记录 epoch 与视频相对时间，后端校验单调性 |
| BLE 断流时 OCR 悄悄接管 | 重量源会话级锁定，切换必须由用户明确确认 |
| 卓易通能装 APK 但后台 BLE 行为不稳定 | MatePad Mini 使用原生 HAP；兼容层仅作临时诊断，不作为交付验收 |
| 双端实现逐渐出现协议差异 | 共用 fixture、JSON Schema、H5 契约和 ESP32 场景做一致性测试 |

## 15. 双端兼容与移植边界

设备交付边界如下：

| 设备 | 正式运行方式 | 不采用的正式方案 |
| --- | --- | --- |
| MatePad Mini / HarmonyOS 6.1 | ArkTS 原生 HAP | 依赖卓易通承载持续 BLE 扫描 |
| 畅享 70 Pro / HarmonyOS 4.2 | Kotlin Android APK | 安装 HarmonyOS NEXT HAP |

Android 版只替换设备适配层和容器层：

```text
HarmonyOS: BleK797Source.ets + H5 Bridge
Android:   K797BleScanner.kt + WebView Bridge
```

以下内容必须跨平台复用：

- K797 payload fixture；
- `ScaleReading / ScaleStatus` JSON 契约；
- H5 自定义事件名称；
- WebSocket `scale_reading` 协议；
- stale、稳定、重试和记录来源语义；
- ESP32-C3 虚拟硬件。

因此“两边兼容”的准确含义是：用户看到相同网页和业务流程，后端收到相同数据协议，两端分别安装适合各自系统的应用；不是一个 HAP/APK 文件同时安装到两台设备。

仓库当前存在一组尚未纳入版本控制的 Android/Kotlin 原型文件，仅作为第二阶段移植参考；尚未完成构建、真机权限、BLE 扫描或 Bridge 验证，不能把它们视为已交付产品代码。

## 16. 最终完成标准

### Release A：HarmonyOS 原生版

1. MatePad Mini 能持续扫描不可连接 K797 广播；
2. 页面实时显示正确重量和来源；
3. BLE 中断不会生成假零值或错误记录；
4. 后端最终记录明确使用 `ble_k797` 真值；
5. 视频仍完整保存用于证据和未来训练；
6. 软件模拟器能覆盖所有业务和异常状态；
7. ESP32-C3 能替代 K797 完成长时间无线链路测试；
8. 真实 K797 只需短时开机即可完成最终协议验收；
9. 现有 OCR 浏览器流程保持兼容；
10. Android 移植无需修改网页和后端协议。

### Release B：Android 兼容版

1. 畅享 70 Pro 能安装签名 APK，并持续扫描不可连接 K797 广播；
2. Kotlin 解析器通过与 ArkTS/Python 相同的 fixture；
3. APK 向网页发送与 HAP 相同的 reading/status 事件；
4. ESP32 断流、恢复、异常包和 4 小时运行结果与鸿蒙版一致；
5. 同一后端无需新增 Android 专用协议或业务分支；
6. 真实 K797 在畅享 70 Pro 上完成一次短时端到端验收。

### 共同完成条件

- 两端都不把“无广播”解释为 `0 g`；
- 两端都不依赖 BLE 配对或 GATT `connect()`；
- 两端都保留视频作为证据和后续 OCR 训练数据；
- 日常开发与自动化主要依靠 fixture、脚本源、广播回放和 ESP32-C3，不要求真实天平一直开机；
- Release A 可以先独立交付，Release B 不阻塞鸿蒙首版上线。
