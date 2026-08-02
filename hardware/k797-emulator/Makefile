# =============================================================================
# K797 BLE 虚拟天平固件 — ESP32-C6 构建脚本（arduino-cli）
# -----------------------------------------------------------------------------
# 为什么不是 PlatformIO？见同目录 BUILD.md：PIO 官方 espressif32 平台把 C6
# 板只注册为 espidf 框架，其 arduino-esp32 core 也无 C6 支持，故 C6+Arduino
# 在 PIO 下会报 "This board doesn't support arduino framework!"。本固件是纯
# Arduino + NimBLE-Arduino，因此走官方 arduino-cli（esp32 core 3.x 完整支持 C6）。
#
# 关键：FQBN 必须带 CDCOnBoot=cdc。DevKitM-1 只有片载 USB-JTAG/CDC、无外接
# USB-UART 桥；不开 CDCOnBoot 时 Serial 走硬件 UART0，USB 口将完全静默、
# 命令也无响应（这是最常见的"烧好了但没输出"陷阱）。
#
# 常用：
#   make setup      # 一次性：装 esp32 core + NimBLE 库
#   make build      # 编译
#   make upload     # 编译 + 烧录（PORT 可用环境变量覆盖）
#   make monitor    # 串口监视（Ctrl+] 或 Ctrl+C 退出）
#   make clean      # 清构建产物
# =============================================================================

ARDUINO_CLI ?= arduino-cli
FQBN        ?= esp32:esp32:esp32c6:CDCOnBoot=cdc
PORT        ?= /dev/cu.usbmodem1101
SKETCH_DIR  := .build/k797_emulator
BUILD_DIR   := .build/build

.PHONY: all setup stage build upload monitor clean

all: build

# 一次性环境准备：安装 esp32 core 与 NimBLE-Arduino 库
setup:
	$(ARDUINO_CLI) core update-index
	$(ARDUINO_CLI) core install esp32:esp32
	$(ARDUINO_CLI) lib install "NimBLE-Arduino"

# 把 src/ 暂存为 arduino-cli 可编译的 sketch（入口 setup()/loop() 在 main.cpp）
stage:
	mkdir -p $(SKETCH_DIR)
	cp -f src/main.cpp src/scenarios.h $(SKETCH_DIR)/
	printf '// K797 emulator build wrapper — setup()/loop() live in main.cpp\n' \
	  > $(SKETCH_DIR)/k797_emulator.ino

build: stage
	$(ARDUINO_CLI) compile --fqbn $(FQBN) --build-path $(BUILD_DIR) $(SKETCH_DIR)

# 用已编译产物烧录，避免重复编译；连接失败见 BUILD.md 的重新插电/usb-reset 说明
upload: build
	$(ARDUINO_CLI) upload -p $(PORT) --fqbn $(FQBN) --input-dir $(BUILD_DIR)

monitor:
	$(ARDUINO_CLI) monitor -p $(PORT) -c baudrate=115200

clean:
	rm -rf .build
