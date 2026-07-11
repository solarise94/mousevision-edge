# MouseVision Edge

利用视觉代替数据线，实现实验动物称重自动记录的边缘设备。

本仓库当前阶段：**Mac 核心算法 PoC**（视频回放验证状态机 + 模板读数 + 曲线回溯 + JSON 输出）。Android CameraX 接入见 [`android/README.md`](android/README.md)。

## 快速开始

> **参考视频不在仓库内**：`RefVideo/` 下的 mp4 因体积较大被 `.gitignore` 排除。clone 后需手动将参考视频放入 `RefVideo/`（端到端 PoC 与 `tests/test_template_reader.py` 依赖它）。目录结构保留，视频文件需自备。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# ROI 预览（确认 LCD 框）
python -m tools.extract_roi_preview \
  --video RefVideo/9494224d488d6e735c0f108cc5562a2d.mp4 \
  --config configs/scale_refvideo.yaml \
  --out output/roi_preview

# 端到端 PoC
python -m tools.run_poc \
  --video RefVideo/9494224d488d6e735c0f108cc5562a2d.mp4 \
  --config configs/scale_refvideo.yaml \
  --box-id C57-023 \
  --out output/

pytest

# 本地检查 UI（浏览器预览状态机 / 读数 / 曲线）
python -m ui.app
# 打开 http://127.0.0.1:8766
# 手机录像/上传入口：http://127.0.0.1:8766/mobile
```

## Web 录像与后台分析

项目现已包含一个手机优先的 Web 基本框架：浏览器后置相机录像或系统相机选视频，上传后生成独立 `job_id`，后端单 worker 串行调用现有 OCR/曲线分析管线，并返回批次报告。

- 手机入口：`/mobile`
- 任务 API：`/api/jobs`
- 健康检查：`/api/health`
- Podman 与 HTTPS 说明：[docs/WEB_APP_FRAMEWORK.md](docs/WEB_APP_FRAMEWORK.md)

浏览器直接调用手机摄像头需要 HTTPS；通过普通内网 HTTP 地址时仍可使用系统相机/相册上传兜底。

## 架构要点

- 业务围绕**状态机**（EMPTY → ENTER → WEIGHING → LEAVE → ANALYZE），Camera/Video 只是 `FrameSource`
- CLI / UI 共用 `SessionDriver`（逐帧喂帧、保存回调）
- **批次边界**：每次扫码/整段运行创建独立 `run_<stamp>_<id>/`，箱号 `cage_id` 在批次内固定，鼠只用 `ordinal`
- 默认读数：`TemplateReader`（7 段 LCD 模板匹配）；OCR 接口预留（Mac 可选 PaddleOCR，Android 用 ML Kit）
- LCD / 鼠只检测阈值在 `configs/*.yaml`（支持 `lcd_detect.mode: fixed` + `weight_roi`）
- 最终体重：完整曲线回溯找平台中位数，不是“稳定 X 秒”
- 箱号：PoC 由 CLI/UI 注入；可选 `pyzbar` 读帧内二维码；Android 阶段接 ML Kit / ZXing
- 上传队列：本地 SQLite（`UploadQueue`）按 `record_id` 幂等入队，WiFi 同步为后续阶段
- UI：历史卡片默认「只读复核」；「重新分析并保存」才开新批次

## 输出格式

```json
{
  "box_id": "C57-023",
  "cage_id": "C57-023",
  "ordinal": 1,
  "run_id": "…",
  "record_id": "…",
  "weight": 16.15,
  "confidence": 0.97,
  "timestamp": "2026-07-10T10:30:21",
  "device": "scale01",
  "photo": "photo.jpg"
}
```
