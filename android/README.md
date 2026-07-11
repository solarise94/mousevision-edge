# Android 接入（第二阶段）

本目录预留给 Android 应用。Mac PoC 已验证的核心契约：

| Python 模块 | Android 对应 |
|-------------|--------------|
| `FrameSource` | CameraX `ImageAnalysis` |
| `RingFrameBuffer` | 环形帧缓冲 |
| `WeightReader` | TemplateReader / OCRReader |
| `WeighingStateMachine` | 称量状态机 |
| `WeightCurveAnalyzer` | 曲线回溯 |
| `Recorder` | 本地 JSON + 图片 |

原则：**业务代码围绕状态机写，Camera 只是输入源。** 换 USB / IP / CameraX 时业务层尽量不动。

PoC 成功后再加：扫码、Room 上传队列、Retrofit。
