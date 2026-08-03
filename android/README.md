# Android 外壳应用（K797 蓝牙天平）

Android WebView 外壳：加载固定页面 `https://weight.pingoodmice.top:16206/mobile` + 注入 JS 桥 + BLE 被动扫描 K797 广播秤。

**语义与鸿蒙侧 `harmonyos/MiceAutomaticScale` 完全一致**——同一份桥契约，H5 侧（`ui/static/scale-bridge.js`）无需区分平台。称量/选择/去重逻辑全在 H5 页面，Android 侧只负责：扫描广播、解析协议、派发读数/状态/发现表事件、接收页面命令。

K797 是**不可连接广播秤**（ADV_NONCONN_IND），绝不调用 GATT connect、绝不要求配对，只能被动扫描；身份 = Local Name `K797` + Manufacturer ID `0x0000` + 9 字节前缀 `CA E8 03 28 08 95 CA 02 10`。真实 BLE 地址是随机私有地址，**不进读数**（`address` 固定 `diagnostic-only`），仅作发现表的 `deviceId`。

---

## 构建

### 前置依赖

- **JDK 17**（已用 `/opt/homebrew/opt/openjdk@17`，AGP 8.7.x 强制要求 17）。
- **Android SDK**：`compileSdk=35` / `build-tools;35.0.0`（AGP 首次构建会自动补装 `build-tools;34.0.0` 作为默认，无需手动处理）。
- **Gradle ≥ 8.9**（AGP 8.7.3 要求；本仓库用本地 8.11.1）。

### 工具链引导（机器无 SDK/Gradle 时）

仓库根 `.toolchain/` 与 `android-sdk/` 已 `.gitignore`，下面是一次性引导步骤（已在主代理会话中执行过）：

```sh
# 1) Android cmdline-tools（注意 dl.google.com 的 -latest 链接会 404，用带版本号的 URL）
curl -L -o /tmp/cmdline-tools.zip \
  https://dl.google.com/android/repository/commandlinetools-mac-12266719_latest.zip
mkdir -p android-sdk/cmdline-tools
unzip /tmp/cmdline-tools.zip -d /tmp/cml-extract
mv /tmp/cml-extract/cmdline-tools android-sdk/cmdline-tools/latest

export JAVA_HOME=/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home
export ANDROID_HOME=$PWD/android-sdk
SDKMGR=$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager

yes | "$SDKMGR" --sdk_root="$ANDROID_HOME" --licenses
"$SDKMGR" --sdk_root="$ANDROID_HOME" "platforms;android-35" "build-tools;35.0.0" "platform-tools"

# 2) local.properties（已存在，指向 android-sdk/）
echo "sdk.dir=$PWD/android-sdk" > android/local.properties

# 3) Gradle 8.11.1（services.gradle.org → github releases 重定向）
curl -L -o /tmp/gradle-8.11.1-bin.zip \
  https://services.gradle.org/distributions/gradle-8.11.1-bin.zip
mkdir -p .toolchain && unzip -q /tmp/gradle-8.11.1-bin.zip -d .toolchain
```

### 构建 debug APK

```sh
export JAVA_HOME=/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home
export ANDROID_HOME=$PWD/android-sdk
cd android
../../.toolchain/gradle-8.11.1/bin/gradle assembleDebug --no-daemon
```

产物：`android/app/build/outputs/apk/debug/app-debug.apk`（debug 自签名，约 820 KB，含 Kotlin stdlib）。

静态自查：

```sh
AAPT=$ANDROID_HOME/build-tools/34.0.0/aapt
"$AAPT" dump badging app/build/outputs/apk/debug/app-debug.apk
```

### 安装到真机

```sh
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

运行时：授予 BLUETOOTH_SCAN/BLUETOOTH_CONNECT（Android 12+）或 ACCESS_FINE_LOCATION（Android 11-）+ CAMERA（称量页扫码）；开启系统蓝牙；放一台 K797 上电广播即可。

---

## 桥契约

### JS 接口（`window.MiceAutomaticScale`，全部 `@JavascriptInterface`，仅 string/void）

| 方法 | 返回 | 说明 |
|------|------|------|
| `startScaleScan()` | void | 开始扫描（幂等）。未授权转 `unauthorized`，蓝牙关转 `bluetooth_off` |
| `stopScaleScan()` | void | 停止扫描（幂等），清表/清选择/清兜底定时器 |
| `getScaleStatus()` | String | 查询状态 JSON（ScaleStatus 形状） |
| `getScaleDevices()` | String | 查询发现表 JSON（devices 事件 detail 形状）；**纯查询，不触发兜底取消** |
| `selectScaleDevice(deviceId)` | void | 选定设备。`deviceId` 须匹配 `^[0-9A-Za-z:\-_.]{1,64}$`，非法忽略不抛错 |
| `clearScaleDevice()` | void | 取消选择，回发现模式 |

`deviceId` 合法性由 `ScaleJavascriptBridge.DEVICE_ID_RE` 校验；非法直接 `return`（不抛异常、不改状态）。

### 原生→页面事件（`webView.evaluateJavascript` 主线程派发，`JSONObject.quote` 包装）

| 事件名 | 节流 | 说明 |
|--------|------|------|
| `miceautomatic:scale-reading` | 100 ms（~10 Hz） | 读数。窗口内丢弃 UI 推送 |
| `miceautomatic:scale-status` | 内容变化才推 | 状态。JSON 与上次不同才派发 |
| `miceautomatic:scale-devices` | ≥500 ms（~2 Hz）+ 内容变化 | 设备发现表。窗口内合并到点推最新一份 |

派发脚本：`(function(){try{window.dispatchEvent(new CustomEvent('name',{detail:JSON.parse(quoted)}));}catch(e){}})();`

页面加载完成（`onPageFinished`）时重推当前状态 + 发现表，做握手。

### 读数 JSON（字段名/格式与 `ScaleReading.toJson` 逐字一致）

```json
{"schemaVersion":1,"device":"K797","deviceKey":"k797:0000:cae803280895ca0210",
 "grams":250.0,"raw":2500,"rssi":-67,
 "receivedAt":"2026-08-03T12:34:56.789Z","receivedAtEpochMs":1754226896789,
 "sequence":7,"stable":true,"stableSource":"derived_repeat",
 "source":"ble","address":"diagnostic-only","payloadHex":"CA E8 03 28 08 95 CA 02 10 C4 09 ..."}
```

- `grams` = `raw / 10.0`，整数带一位小数（`250.0`）；
- `deviceKey` = `k797:<manufacturerId 4位小写hex>:<9字节前缀小写hex连续>` → `k797:0000:cae803280895ca0210`（运行时由 `PREFIX` 常量构造）；
- `sequence` 进程内单调，`start` 时清零，每条派发读数 +1（H5 `isValidReading` 靠它去重，缺失=全丢）；
- `stable` 派生：**连续相同 `raw` ≥3**（raw 一变计数重置为 1）；`stableSource` 为 `"derived_repeat"` 或 `null`；
- `address` 固定 `"diagnostic-only"`（真实 BLE 地址不进读数）；
- `payloadHex` 大写空格分隔 18 字节。

### 状态 JSON（`ScaleStatus.toJson` 形状）

```json
{"device":"K797","state":"scanning","message":"发现 2 台天平，等待选择",
 "lastReadingAtEpochMs":null,"source":"ble","selectedDeviceId":null}
```

`state` ∈ `off | scanning | active | stale | unauthorized | bluetooth_off | error`。

### 关键状态机/语义

- **stale**：单调时钟 `SystemClock.elapsedRealtime`，>15 s 无合法包才进；stale **绝不产生 0 g 读数**；`scanning` 未收过包不进 stale。
- **设备发现/选择**：
  - 发现表 `Map<deviceId=BLE地址, {deviceId,name,rssi,grams,lastSeenAtEpochMs}>`；**每条扫描结果先解析更新表**（解析失败 grams 保留旧值），再按选择过滤；
  - 未选定 = 纯发现模式：不派发读数，状态停 `scanning`，`message` = `发现 N 台天平，等待选择`；
  - `selectScaleDevice(id)`：仅该设备的包派发读数；首个匹配读数 → `active` `已收到 K797 广播`；
  - `clearScaleDevice()`：回 `scanning` `已取消选择，继续搜索`；
  - 发现表 >15 s 未见设备剔除（选中设备豁免）；推送仅在新设备出现/消失/grams 变/rssi 变 ≥5 dB/选择变化时。
- **旧版兜底**：`start` 4 s 后，若**从未调用过 `selectScaleDevice`/`clearScaleDevice`**（`getScaleDevices` 是纯查询，**不算**）且无选择且发现表非空 → 自动选 rssi 最强（log `auto-select legacy fallback`）；4 s 时表空则每 4 s 重试直到页面用 API 或 `stop`。

### Activity 行为

- `window.addFlags(FLAG_KEEP_SCREEN_ON)`——称量页常亮（对齐鸿蒙侧）；
- 导航白名单：仅 `https://weight.pingoodmice.top:16206`（`shouldOverrideUrlLoading` + `onPermissionRequest` 双重校验）；
- 相机 `onPermissionRequest`：白名单内且已授权才 `grant`，否则 `deny`；
- `onPause` 停扫描（避免后台占用 BLE）。

---

## 与鸿蒙侧的语义对应

| Android | HarmonyOS | 角色 |
|---------|-----------|------|
| `K797BleScanner.kt` | `scale/BleK797Source.ets` + `scale/ScaleSource.ets` + `scale/K797AdvertisementParser.ets` | BLE 扫描 + 解析 + 状态机 + 发现表 |
| `ScaleJavascriptBridge.kt` | `bridge/ScaleWebBridge.ets` 的 `ScaleProxyObject` | `window.MiceAutomaticScale` JS 接口（6 方法 + deviceId 校验） |
| `MainActivity.kt` | `bridge/ScaleWebBridge.ets` + `Index.ets` | WebView 宿主 + 事件派发（节流/去重）+ 导航白名单 |
| `BuildConfig.MICE_WEB_URL` | `ScaleWebBridge.TRUSTED_URL` | 固定页面地址（单一事实来源） |

Android 侧把鸿蒙的 `ScaleSourceBase`（序列分配/稳定派生/stale 检查）+ `BleK797Source`（扫描/发现/选择/兜底）合并进 `K797BleScanner` 一个类；JSON 构造与解析逻辑与 `ScaleReading.toJson` / `ScaleStatus.toJson` / `K797AdvertisementParser.parse` 逐字对齐。`stale` 阈值 15 s、稳定最小重复 3、发现过期 15 s、兜底延迟 4 s 与鸿蒙常量一致。

---

## 文件清单

```
android/
├─ build.gradle.kts                  # AGP 8.7.3 + Kotlin 2.0.21 插件声明
├─ settings.gradle.kts               # google()/mavenCentral() 仓库
├─ gradle.properties
├─ local.properties                  # sdk.dir（gitignored）
├─ README.md                         # 本文件
└─ app/
   ├─ build.gradle.kts               # compileSdk=35 / minSdk=26 / BuildConfig.MICE_WEB_URL
   ├─ proguard-rules.pro             # 保留 @JavascriptInterface 方法
   └─ src/main/
      ├─ AndroidManifest.xml         # BLUETOOTH_SCAN(neverForLocation)/BLUETOOTH_CONNECT/CAMERA/INTERNET
      ├─ res/values/styles.xml
      └─ java/com/pingoodmice/miceautomatic/
         ├─ MainActivity.kt          # WebView 宿主 + 事件派发 + 权限流
         ├─ K797BleScanner.kt        # BLE 扫描 + 解析 + 状态机 + 发现表（核心）
         └─ ScaleJavascriptBridge.kt # window.MiceAutomaticScale JS 接口
```

---

## 远期：第二阶段原生管线（保留）

当前外壳只做"WebView + 蓝牙桥"，称量/OCR/状态机全在 H5 与后端。若未来要把视觉管线下沉到原生（离线/低延迟场景），Mac PoC 已验证的对应关系：

| Python 模块 | Android 对应 |
|-------------|--------------|
| `FrameSource` | CameraX `ImageAnalysis` |
| `RingFrameBuffer` | 环形帧缓冲 |
| `WeightReader` | TemplateReader / OCRReader |
| `WeighingStateMachine` | 称量状态机 |
| `WeightCurveAnalyzer` | 曲线回溯 |
| `Recorder` | 本地 JSON + 图片 |

原则：**业务代码围绕状态机写，Camera 只是输入源。** 换 USB / IP / CameraX 时业务层尽量不动。PoC 成功后再加：扫码、Room 上传队列、Retrofit。
