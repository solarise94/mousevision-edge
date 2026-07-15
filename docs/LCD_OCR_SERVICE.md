# LCD OCR 服务（独立容器）

通用 OCR（RapidOCR / PP-OCRv6 + OpenVINO）读电子秤 LCD，供分析管线与复核工具共用。

> 下一阶段将切换为：**无状态单帧 OCR 服务（定位吸附 + 七段码）+ mousevision 侧时序融合**；
> RapidOCR 仅作审计/实验回退。阶段 B 强制先做无 CNN 验证。实施设计见
> [`LCD_SEVEN_SEGMENT_UPGRADE.md`](LCD_SEVEN_SEGMENT_UPGRADE.md)。

## 门禁

**先验收，再把主流程切到 `http_ocr`。** 两套都要过：

1. **关键帧**：定位与单帧数字识别（`accept_0001.py`）
2. **端到端 replay**：0001 必须 9 个 session，且第 1、7、9 次正确

```bash
# 本机 CPU 验收（默认读 tests/fixtures/lcd_ocr/0001，可换临时目录）
LCD_OCR_DEVICE=CPU python services/lcd_ocr/accept_0001.py
# 或：python services/lcd_ocr/accept_0001.py tmp_ocr_acceptance/0001
```

当前关键帧用例（版本库 `tests/fixtures/lcd_ocr/0001/`）：
`mouse_004`（约 23.8）、`scan5` 平台帧（约 24.1）、`m2_photo_21.60`（`1`→`2` 回归）、
以及端到端 9 session 门禁。不通过则不要改 `weight_reader`。

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | `{ok, device, model}` — 部署后确认 `device=GPU` |
| POST | `/v1/lcd/read` | 单帧（multipart `file` 或 form `image_base64`）— **状态机主路径** |
| POST | `/v1/lcd/read-json` | JSON base64 单帧 |
| POST | `/v1/lcd/read-batch` | 仅离线重分析；**不要**接入 `read_weight` |

## 容器网络（重要）

主容器内 `127.0.0.1:8768` 指向主容器自己，**不是** OCR 容器。

推荐：同一 Podman 网络，用服务名：

```
MOUSEVISION_OCR_URL=http://mousevision-lcd-ocr:8768
MOUSEVISION_WEIGHT_READER=http_ocr   # 仅门禁通过后
```

## Iris Xe / OpenVINO

宿主机先装 GPU 计算栈（一次性）：

```bash
sudo apt-get install -y intel-opencl-icd libze1 libze-intel-gpu1
sudo usermod -aG render vm-user   # 重登后生效；Quadlet 也可用 GroupAdd=<render gid>
clinfo -l   # 应看到 Intel Iris Xe
```

容器侧：

1. Quadlet 透传：`Device=/dev/dri/renderD128` + `GroupAdd=<render gid>`
2. 挂载宿主机 OpenCL ICD 与 Level Zero：`/etc/OpenCL/vendors`、`intel-opencl/`、`libze_*.so`（见 `deploy/quadlet/mousevision-lcd-ocr.container`）
3. 启动后 `curl http://127.0.0.1:8768/health` 应见 `"device":"GPU"`
4. GPU 不可用时服务自动回退 CPU（`LCD_OCR_DEVICE=CPU` 可强制）

## 示例 Quadlet

见 [`deploy/quadlet/mousevision-lcd-ocr.container`](../deploy/quadlet/mousevision-lcd-ocr.container) 与网络单元。复制到 VM：

```bash
mkdir -p ~/.config/containers/systemd
cp deploy/quadlet/* ~/.config/containers/systemd/
systemctl --user daemon-reload
systemctl --user start mousevision-lcd-ocr.service
```

主服务 `mousevision.container` 需加入同一 `Network=`，并设置上述环境变量。

## 本地跑服务

主路径已改为：**HSV/hint 四角定位 → 透视校正 → 固定四槽位经典七段码**。
RapidOCR 不再默认加载（仅 `LCD_OCR_AUDIT=1` 时懒加载审计）。

```bash
cd services/lcd_ocr
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8768
```

调试：

```bash
python tools/export_lcd_debug.py path/to/frame.jpg -o tmp_lcd_debug
python tools/compare_stage_b.py path/to/frame.jpg
```
