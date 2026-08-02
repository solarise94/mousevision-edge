# K797 BLE 虚拟天平固件 — ESP32-C6 构建与烧录

模拟真实 K797 不可连接蓝牙天平的广播，供鸿蒙 / Android 扫描器与解析器联调。
固件零堆分配、不可连接、不建 GATT Server，仅发 `ADV_NONCONN_IND` 广播。

## 为什么用 arduino-cli 而不是 PlatformIO

本工程最初按 PlatformIO 组织，但 **PIO 无法为 ESP32-C6 构建 Arduino 固件**，这是 PIO 的硬限制：

- PIO 官方 `espressif32` 平台里，所有 C6 板定义（`esp32-c6-devkitm-1` 等）的
  `frameworks` 字段**只注册了 `espidf`，从未注册 `arduino`**。
- PIO registry 里的 arduino-esp32 core **完全没有 C6 支持**（无 `esp32c6` variant、
  boards.txt 里 0 条 C6 记录）。

因此 `pio run` 会直接报：

```
Error: This board doesn't support arduino framework!
```

本固件是纯 Arduino（`setup()`/`loop()` + `Serial`）+ NimBLE-Arduino，迁到 ESP-IDF
工作量巨大且无必要。改用官方 **arduino-cli**，其 `esp32:esp32` core 3.x 完整支持 C6。
原 `platformio.ini` 已移除（留着只会误导），构建统一走本目录 `Makefile`。

## 环境准备（一次性）

```sh
# 安装 arduino-cli（如未装）：brew install arduino-cli
make setup    # 装 esp32:esp32 core + NimBLE-Arduino 库
```

已验证版本：arduino-cli 1.4.1、esp32:esp32 3.3.8、NimBLE-Arduino 2.5.0。

## 构建 / 烧录 / 监视

```sh
make build                       # 编译（产物在 .build/build/）
make upload                      # 编译 + 烧录
make upload PORT=/dev/cu.usbmodem1101   # 显式指定端口
make monitor                     # 串口监视，Ctrl+C 退出
make clean                       # 清构建产物
```

默认 FQBN：`esp32:esp32:esp32c6:CDCOnBoot=cdc`。

## ⚠️ CDCOnBoot=cdc 必须开

DevKitM-1 **只有片载 USB-JTAG/CDC，没有外接 USB-UART 桥**。若不带 `CDCOnBoot=cdc`：

- 固件 `Serial` 默认走硬件 UART0（GPIO 引脚），而板上没有把 UART0 接到 USB；
- 结果：USB 口**完全静默**，看不到启动日志，发命令也无响应。

这是"烧录成功但没输出"的最常见原因。Makefile 的 FQBN 已固化该选项。

## ⚠️ 烧录连接失败（C6 原生 USB-JTAG 顽疾）

ESP32-C6 原生 USB-JTAG/CDC 在复位瞬间会 USB 重新枚举，esptool 的端口句柄可能失效，
表现为 `Failed to connect to ESP32-C6: No serial data received`。无 BOOT/RESET 按键的
精简板尤其常见。可靠处理顺序：

1. **重新插拔 USB**，等 1–2 秒端口回来，再 `make upload`——干净状态往往一次成功。
2. 仍失败时直接用核心自带 esptool 的 `usb-reset` 模式（本目录构建产物为
   `.build/build/k797_emulator.ino.merged.bin`）：

   ```sh
   ESPTOOL=~/Library/Arduino15/packages/esp32/tools/esptool_py/*/esptool
   $ESPTOOL --chip esp32c6 --port /dev/cu.usbmodem1101 \
     --before usb-reset --after hard-reset \
     write-flash 0x0 .build/build/k797_emulator.ino.merged.bin
   ```

3. 有 BOOT 键的板：按住 BOOT → 短按 RESET → 松 BOOT，进入下载模式后再烧。

## 验证

烧好后发串口命令（115200，需断言 DTR 让 TinyUSB CDC 认为主机已连接）：

```
STATUS            # 回显 JSON：device/mode/running/intervalMs/lastGrams/freeHeap...
GRAMS 250         # 设固定重量 250.0 g（raw=grams×10=2500），回 OK GRAMS 250.0 (raw 2500)
STATUS            # lastGrams 应为 250.0
HELP              # 列出全部命令与场景
```

正常启动 banner：

```
========================================
K797 BLE emulator (ESP32-C6, NimBLE 2.x)
non-connectable ADV_NONCONN_IND, no GATT
interval 200 ms, payload 18 bytes, name "K797", mfgId 0x0000
send HELP for commands
========================================
```

## 命令速览

`GRAMS <g>` / `RAW <u16>` / `ZERO` / `SILENCE <ms>` / `NOISE <center> <amp>` /
`PLAY <scenario> [LOOP]` / `MALFORMED short|prefix` / `INTERVAL <100..1000>` /
`STOP` / `STATUS` / `HELP`。场景文件见 `scenarios/`。
