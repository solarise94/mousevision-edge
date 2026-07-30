# MiceAutomaticScale 命令行构建 / 签名 / 安装指南

本工程用 **Command Line Tools 6.1.1（API 24）** 全流程命令行构建，**不依赖
DevEco Studio GUI**。设备：HUAWEI MatePad Mini（TEST_TABLET，HarmonyOS 6.1.0.135，
API 24，arm64-v8a）。

> 工具链（~6GB，含 SDK/hdc/hvigor）放在仓库根的 `command-line-tools/`，已 gitignore，
> 不入版本库。JDK 用 Homebrew `openjdk@17`（keg-only）。

## 0. 环境变量

```bash
export CLT=/Users/solarise/Ranalysis/MiceAutomatic/command-line-tools
export JAVA_HOME=/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home
export PATH="$JAVA_HOME/bin:$CLT/bin:$PATH"
export DEVECO_SDK_HOME="$CLT/sdk"
cd harmonyos/MiceAutomaticScale
```

## 1. 构建未签名 HAP

```bash
hvigorw assembleHap --mode module -p product=default -p buildMode=debug --no-daemon
# 产物：entry/build/default/outputs/default/entry-default-unsigned.hap
```

ArkTS 为 API 24 严格模式。已知无害 WARN：`Index.ets` 的 `getContext(this)` 标
deprecated（组件内合法调用，不影响功能/安装/运行）。

## 2. 签名（用 hap-sign-tool，**不**用 hvigor signingConfigs）

`build-profile.json5` 的 `signingConfigs` 故意留空。原因：hvigor 期望
`storePassword`/`keyPassword` 是**密文**，并依赖 `signature/material/{fd,ac,ce}`
加密目录，而该目录只能由 DevEco GUI 生成，纯 CLT 无对应加密工具。
`hap-sign-tool sign-app -mode localSign` 接受**明文**密码直接签，更可控：

```bash
bash signature/sign.sh
# 产物：entry/build/default/outputs/default/entry-default-signed.hap
```

签名材料（`signature/`，**私钥/Profile 已 gitignore，仅 .cer 入库**）：
- `miceautomatic_debug.p12` — 本地 EC 密钥库（私钥）
- `miceautomatic_debug.cer` — AGC 调试证书链（含开发者证书）
- `miceautomatic_debugDebug.p7b` — AGC 调试 Profile（绑定设备 UDID）

密钥库口令见本地 `.deveco-env.sh`（gitignore）。若口令长度 < 32 会被 hvigor 拒
（本流程不走 hvigor 签名，但 hap-sign-tool 无此限制；改口令用
`keytool -storepasswd` 保留密钥对，PKCS12 的 store/key 口令是同一个）。

AGC 申请要点：包名必须等于 `AppScope/app.json5` 的 `bundleName`
（`com.pingoodmice.miceautomatic.scale`）；Profile 类型选**调试**；设备 UDID 用
`hdc shell bm get --udid` 读取。

## 3. 安装 / 拉起

```bash
HDC=$CLT/sdk/default/openharmony/toolchains/hdc
$HDC list targets                       # 应显示设备序列号 + Connected
$HDC uninstall com.pingoodmice.miceautomatic.scale
$HDC install entry/build/default/outputs/default/entry-default-signed.hap
$HDC shell aa start -a EntryAbility -b com.pingoodmice.miceautomatic.scale
$HDC shell snapshot_display -f /data/local/tmp/s.jpeg   # 截屏验证
$HDC file recv /data/local/tmp/s.jpeg /tmp/s.jpeg
```

**锁屏限制**：开发者模式下 `aa start` 在屏幕锁定时报 `10106102`（hdc 不能自动
解锁）。安装（`bm` 操作）与 `bm dump` 验证不需解锁；**拉起/截屏需先解锁屏幕**。
不依赖解锁的运行时验证：`$HDC shell bm dump -n <bundle>` 可确认 abilities 与
`ACCESS_BLUETOOTH`/`INTERNET`/`LOCATION` 权限已落地到安装包。

## 4. hdc 连设备踩坑备忘

- 仅充电模式 hdc 看不到设备；需切"传输文件"并开启**开发者选项 → USB 调试**；
- 首次连接设备弹"允许 HDC 调试"需点信任；
- `ioreg` 能看到 `HDC Device` 但 `hdc list targets` 空 → 通常是设备端未授权或
  仍在 MTP 重枚举，重插线 + 授权即可。

## 5. 跨语言契约守护

`tests/test_k797_cross_lang.py` 解析 ArkTS `K797AdvertisementParser.ets` 源码，
断言其协议常量、`buildDeviceKey` 小写、`toHexSpaces` 大写、校验顺序与 Python
`mousevision/scale_k797.py` 及计划 §5.1 契约逐字一致，零设备依赖、进 CI。
（ArkTS on-device hypium 测试需 ohosTest 脚手架 + 设备 instrumentation，留待设备
可用时搭建。）`deviceKey` 必须**小写** hex（`k797:0000:cae803280895ca0210`）；
`payloadHex` 显示用**大写**——两者勿混。

## 6. 源抽象与 debug 面板

页面 `Index.ets` 的 `ScaleSource` 有三种实现：`script`（rawfile 场景演练）、
`replay`（回放 captured_raw.jsonl）、`ble`（`BleK797Source` 真实 BLE 扫描）。
debug 面板（`DEBUG_ENABLED`）提供源切换 / 9+1 场景加载 / 启停按钮，无网页即可肉眼
演练全部异常场景。默认源按 `AppStorage('bluetoothGranted')` 选 ble/script。
BLE 无线链路验证需 ESP32-C3 模拟器或真实 K797（见仓库 `hardware/` 与计划 §9.3/§9.4）。
