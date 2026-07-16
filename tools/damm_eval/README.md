# DAMM (Detect Any Mouse Model) Evaluation Scaffold

> **许可证状态**：DAMM 仓库 (https://github.com/backprop64/DAMM) 根目录未提供
> LICENSE 文件。Detectron2 的 Apache-2.0 只覆盖 Detectron2 本身，**不自动授权
> DAMM 自身代码或模型权重**。在作者确认许可证之前，**仅允许内部评估，不集成、
> 不分发**。

## 目的

评估 DAMM 预训练检测器在"小鼠自动称重"场景中的表现，重点验证：

1. 能否区分**小鼠 vs 手套/人手**（核心需求，DAMM 训练场景是旷场/cage，手套是分布外样本）
2. 能否在称重视频帧（含秤盘+LCD+操作员手）中正确定位小鼠 bbox
3. CPU 推理速度是否可接受（边缘部署，无 GPU）

## 评估流程

### 1. 环境准备（独立 venv，不污染主项目）

```bash
python -m venv /tmp/damm_venv
source /tmp/damm_venv/bin/activate
pip install detectron2 torch torchvision opencv-python-headless
git clone https://github.com/backprop64/DAMM /tmp/DAMM
# 按 DAMM README 下载预训练权重
```

### 2. 采集评估样本

从本项目的 `training_assets/`（P1-e 被动收集产出）中抽取含手套的帧：

```bash
python tools/damm_eval/collect_samples.py \
  --output /tmp/damm_samples/ \
  --source output/run_*/mouse_*/training_assets/ \
  --max-per-session 5
```

### 3. 运行推理

```bash
python tools/damm_eval/run_inference.py \
  --weights /tmp/DAMM/model_final.pth \
  --input /tmp/damm_samples/ \
  --output /tmp/damm_results/ \
  --device cpu
```

### 4. 人工标注与统计

```bash
# 标注工具：对每张结果图标记 ground truth (mouse / glove / empty)
python tools/damm_eval/score.py \
  --predictions /tmp/damm_results/ \
  --labels /tmp/damm_labels.csv \
  --output /tmp/damm_report.json
```

## 评估指标

- **小鼠检测准确率**：DAMM bbox 与人工标注的 IoU ≥ 0.5 占比
- **手套误检率**：手套被检测为 mouse 的比例
- **CPU 推理延迟**：单帧 ms（p50/p90）
- **漏检率**：小鼠在秤上但 DAMM 未检出的比例

## 集成决策标准

满足以下**全部**条件才考虑集成到生产：

1. 小鼠检测准确率 ≥ 90%
2. 手套误检率 ≤ 10%
3. CPU 单帧延迟 ≤ 200ms（只对灰度检出的大块做二次确认）
4. DAMM 许可证已获作者确认（或改用自有数据训练的 ONNX 模型）

**不满足时的替代路径**：用本项目 `training_assets/` 积累的数据 + Roboflow
mouse 数据集，训练轻量 YOLO/SSD，导出 ONNX 在边缘 CPU 跑。注意 YOLOv8 是
AGPL-3.0（传染），需用 ONNX 推理或选非 AGPL 框架。

## 文件清单

- `collect_samples.py`：从 training_assets 抽取评估帧
- `run_inference.py`：调用 DAMM 模型推理（需 detectron2 环境）
- `score.py`：对比预测与人工标注，输出指标报告
