# 手机实时称重延迟修复计划（修订版）

- 日期：2026-07-21
- 范围：手机实时取帧、WebSocket/frp 传输、实时 OCR、稳定判定、重测状态机
- 目标：解决录像后分析画质不稳定的问题，使手机实时称重在正常网络下快速响应，并在 frp 弱网下不堆积旧帧、不锁定旧体重
- 本文性质：实施方案，不包含本轮代码修改

## 1. 结论与边界

采用手机实时取帧是合适的。实时路径可以持续获得当前画面，避免整段录像上传、转码或抽帧后才发现画质不合格，也能即时提示用户调整手机位置、光照和反光。

但实时 JPEG 仍需经 WebSocket 和 frp 上传到服务器，因此延迟受手机编码、上行带宽、frp RTT、服务端解码和 OCR 共同影响。本轮应把目标区分为：

- 正常网络：从首个有效非零读数到播报，P50 不高于 0.8 秒，P95 不高于 1.5 秒；
- frp 弱网：不堆积历史帧，P95 尽量控制在 2 秒内；
- 服务端开始处理时的帧龄：P95 不高于 500 ms；
- 任意时刻每个会话最多一帧等待服务端处理；
- 如果现场要求在各种公网条件下都稳定低于 1 秒，后续必须把 OCR/稳定判定下沉到实验室局域网边缘端或手机端，frp 只同步结果。

原方案中的四个主要方向保留：

1. 用独立原始 OCR 读数做稳定证据，禁止重复计算融合窗口；
2. WebSocket 改成严格的单帧在途；
3. retry 支持小鼠留在秤上立即重测；
4. 鼠检测改为提示性证据，不再清空可信的重量证据。

## 2. 本次修改文件

| 文件 | 修改内容 |
|---|---|
| `mousevision/realtime.py` | 原始读数稳定窗、唯一帧校验、weighing epoch、retry 留秤语义 |
| `mousevision/reader/template.py` | `read_weight` 接受预计算的 `lcd_box` |
| `ui/realtime_api.py` | 完整 realtime 配置、处理失败 ACK、协议时间点、retry 明确结果 |
| `ui/static/mobile.js` | 按 `frame_seq` 匹配 ACK、ACK 超时、严格 5fps 限速、自适应编码、retry 等待 ACK |
| `configs/scale_refvideo.yaml` | 新增独立的 `realtime` 配置段 |
| `tests/test_realtime.py` | 状态机、旧体重、时间窗、epoch 和 retry 回归 |
| `tests/test_realtime_contract.py` | WebSocket ACK、错误响应和命令契约回归 |

## 3. P0-1：重构稳定判定，禁止锁定旧体重

### 3.1 配置模型

在 `RealtimeConfig` 中新增：

```python
stable_max_age_s: float = 1.6
stable_min_raw_reads: int = 3
stable_weight_tol: float = 0.10
mouse_advisory: bool = True
frame_seq_dedupe: bool = True
```

`stable_min_frames` 暂时保留以兼容旧配置，但实时稳定判定不再使用它。字段使用 `stable_max_age_s`，避免把时间窗误解为“必须等待完整窗口时长”。它表示稳定证据允许保留的最大年龄。

默认不采用“0.8 秒内必须凑齐 3 帧”。在 2fps 或 RTT 较高时，三个读数至少跨越约 1 秒，0.8 秒窗口会导致永远无法播报。默认最大证据年龄先设为 1.6 秒，再根据现场埋点调整。

### 3.2 原始证据结构

新增只保存独立原始 OCR 结果的结构：

```python
@dataclass
class RealtimeRawRead:
    frame_seq: int
    client_ts_ms: float
    weight: float
    confidence: float
    epoch: int
```

会话新增：

```python
self._raw_window: deque[RealtimeRawRead] = deque()
self._weighing_epoch: int = 0
self._last_frame_seq: int = -1
self._last_client_ts_ms: float = -1.0
```

每条稳定证据必须同时满足：

- `frame_seq` 未重复且严格递增；
- `client_ts_ms` 未倒退；
- 属于当前 `_weighing_epoch`；
- OCR 结果有效、重量高于 `enter_min`、置信度达到门槛。

重复、倒序和旧 epoch 的帧只返回状态，不进入稳定窗。

### 3.3 ARMED 阶段保留有效证据

当前实现需要 ARMED 连续两帧后才进入 WEIGHING，但会丢掉这两帧，导致进入 WEIGHING 后重新累计。

修订后，从 ARMED 收到第一条可信非零读数时就放入当前 epoch 的原始窗口；第二条读数触发 WEIGHING 时不清空这两条证据。这样在 5fps 下，第三条一致读数最快约 0.4 秒即可形成候选。

如果 ARMED 期间重量回落到 `enter_min` 以下、读数无效或出现明显平台变化，则清空进入证据并重新开始。

### 3.4 最新稳定后缀判定

不能直接计算整个最大年龄窗口的 `max-min`，否则一个短暂 OCR 异常会让后续所有正确读数等待 1.6 秒才能恢复。

应从最新读数向前寻找连续稳定后缀：

1. 后缀必须包含最新有效读数；
2. 至少包含 `stable_min_raw_reads` 条唯一原始读数；
3. 最老读数距最新读数不超过 `stable_max_age_s`；
4. 后缀内 `max(weight) - min(weight) <= stable_weight_tol`；
5. 最终重量取后缀中位数；
6. 最新读数与中位数差值不得超过 `stable_weight_tol`；
7. attempt 置信度取后缀置信度的中位数，不使用单独一帧的置信度。

当读数发生 `16.14 × 3 -> 15.62 × 3` 的平台切换时，旧平台不能继续输出。只有最新的 `15.62` 自身形成至少三条稳定后缀后，才允许播报。

### 3.5 鼠检测语义

`mouse_advisory=True` 时：

- `mouse_present=False` 不清空原始重量窗口；
- 返回 `mouse_uncertain` 质量提示；
- 重量证据满足条件时仍可播报。

如果未来需要硬门槛，应另设语义明确的 `require_mouse_for_announce`，不能让 `mouse_advisory=False` 处于行为不明确的状态。

### 3.6 离秤与重置

新增统一的 `_reset_weighing()`，清理：

- `_raw_window`；
- `_stable_run` 兼容字段；
- `_enter_sustain`；
- `_leave_count`；
- fusion 状态。

清秤、会话进入下一只小鼠、retry 和异常恢复均通过该方法重置，避免遗漏某个历史状态。

## 4. P0-2：单帧在途协议与严格限速

### 4.1 客户端只允许一个待确认帧

前端不再使用简单的布尔变量表示在途状态，而是记录：

```javascript
let pendingFrameSeq = null;
let pendingFrameSentAt = 0;
let frameAckTimer = null;
let nextAllowedSendAt = 0;
```

发送流程：

1. 完成 JPEG 编码和 `FileReader` 后检查 WebSocket 状态；
2. 在 `ws.send()` 前分配本帧 `frame_seq`；
3. 发送成功后设置 `pendingFrameSeq`；
4. 在收到对应 `frame_seq` 的 `state` 或 `error` 前禁止发送下一帧；
5. `hello`、retry ACK、accept ACK 和其他帧的响应均不得释放当前帧锁。

### 4.2 只按匹配序号释放

客户端收到响应时：

```javascript
if (
  (msg.type === "state" || msg.type === "error") &&
  Number(msg.frame_seq) === pendingFrameSeq
) {
  releasePendingFrame();
  scheduleNextFrame();
}
```

不能在 `handleServerMessage()` 顶部无条件执行 `frameInFlight = false`，否则命令 ACK 或重连 hello 会允许第二帧进入。

### 4.3 ACK 超时与异常恢复

发送帧后启动 3 秒 ACK 超时：

- 超时后丢弃该待确认状态，不重发旧 JPEG；
- 如果 WebSocket 仍打开，只发送当时的最新画面；
- 连续超时达到门槛时触发重连，并显示“网络较慢，正在重连”；
- WebSocket close/error 时必须清理 `pendingFrameSeq` 和计时器。

服务端 `_process_one_frame()` 捕获异常后不能静默返回，必须发送：

```json
{
  "type": "error",
  "code": "frame_processing_failed",
  "message": "本帧识别失败，正在重试",
  "frame_seq": 123
}
```

JPEG 解码失败已经返回 `frame_seq`，继续保留该行为。

### 4.4 ACK 后仍要遵守 5fps 上限

ACK 到达后不能直接 `setTimeout(sendFrame, 0)`。否则在低 RTT 下会绕过 200ms 定时器，实际发送几十 fps。

统一使用：

```javascript
const MIN_FRAME_INTERVAL_MS = 200;

function scheduleNextFrame() {
  const delay = Math.max(0, nextAllowedSendAt - performance.now());
  clearTimeout(nextFrameTimer);
  nextFrameTimer = setTimeout(sendFrame, delay);
}
```

每次成功发送后设置：

```javascript
nextAllowedSendAt = performance.now() + MIN_FRAME_INTERVAL_MS;
```

最终发送频率是 `min(5fps, 1 / 实际 ACK 耗时)`：网络快时最多 5fps，网络慢时自然降频，不维护图片 FIFO。

## 5. P0-3：retry 留秤重测并让 epoch 真正生效

产品语义确定为：同一只鼠可以留在秤上立即重新采样。

后端 `request_retry()`：

1. 仅在 `ANNOUNCED` 状态应用；
2. 当前 attempt 标记为 rejected；
3. `_weighing_epoch += 1`；
4. 调用 `_reset_weighing()`；
5. 清空当前 attempt；
6. 直接进入 `WEIGHING`；
7. 返回是否真正应用以及新的 epoch。

WebSocket retry ACK：

```json
{
  "type": "ack",
  "cmd": "retry",
  "applied": true,
  "state": "weighing",
  "epoch": 2
}
```

若状态不允许 retry，则 `applied=false`，不能仍显示成成功。

前端点击后：

- 暂停新帧调度；
- 如果已有一帧在途，等待该帧对应 ACK 或超时；
- 发送 retry 命令；
- 按钮显示“正在重测…”并禁用；
- 收到 `applied=true` 后清空旧候选、切换到 WEIGHING，再恢复取帧；
- 收到 `applied=false` 或发送失败时恢复按钮并显示提示；
- 不再乐观切换到 ARMED。

同一连接内 WebSocket FIFO 可以确保 retry 排在至多一个在途帧之后。服务端还需记录最后处理的 `frame_seq`；重连或并发旧连接送来的重复/倒序帧不得进入新 epoch 的稳定窗。

## 6. P1-1：复用 LCD 定位结果

修改 `TemplateReader.read_weight`：

```python
def read_weight(
    self,
    image: np.ndarray,
    *,
    lcd_box: LcdBox | None = None,
) -> tuple[float | None, float]:
    box = lcd_box if lcd_box is not None else self._lcd_box(image)
```

实时 WEIGHING 路径每帧只定位一次 LCD，并将结果同时传给鼠检测和重量读取。

所有 FakeReader 和相关测试替身需要同步接受 `lcd_box=None` 关键字参数。

## 7. P1-2：降低带宽但先验证识别质量

不直接把 480×854、JPEG 0.40 设为唯一固定档位。先准备三档：

| 档位 | 分辨率 | JPEG quality | 使用场景 |
|---|---:|---:|---|
| high | 720×1280 | 0.55 | 网络正常、识别困难 |
| medium | 540×960 | 0.50 | 默认候选档 |
| low | 480×854 | 0.40 | frp 明显弱网时降级 |

上线前使用真实手机画面逐档回放，统计：

- LCD 定位成功率；
- OCR 有效读数率；
- 重量错误率；
- 鼠检测召回率；
- 单帧字节数；
- 端到端 ACK 时间。

默认档位以识别质量不显著下降为前提。弱网降档依据连续 ACK 延迟和编码后字节数，网络恢复稳定后再逐步升档，避免频繁来回切换。

实时识别 JPEG 与后台留档录像是两个用途。降低实时帧分辨率不能同步降低留档视频质量；留档仍保持现有录制参数，便于复核。

## 8. 配置加载

在 YAML 中增加独立配置段：

```yaml
realtime:
  calibrate_min_frames: 5
  enter_min: 1.0
  empty_max: 0.15
  leave_max: 0.30
  enter_sustain_frames: 2
  stable_min_raw_reads: 3
  stable_max_age_s: 1.6
  stable_weight_tol: 0.10
  min_confidence: 0.50
  min_brightness: 30.0
  max_glare_ratio: 0.15
  mouse_smooth_window: 5
  mouse_advisory: true
  announce_hold_s: 0.0
  clear_timeout_s: 30.0
  max_fps: 5
  frame_ack_timeout_ms: 3000
  encode_profile: medium
```

`_create_engine()` 必须加载全部实时字段，包括原方案遗漏的：

- `min_brightness`；
- `max_glare_ratio`；
- `mouse_smooth_window`；
- `announce_hold_s`；
- `clear_timeout_s`。

解析后对关键值做范围校验，例如 `stable_min_raw_reads >= 2`、`stable_max_age_s > 0`、`0 < min_confidence <= 1`。配置错误应在创建 session 时明确报错，而不是静默使用危险值。

## 9. 延迟与帧龄埋点

服务端为每帧记录：

- `frame_seq`；
- `epoch`；
- `client_ts_ms`；
- `received_at`；
- `decode_started_at / decode_completed_at`；
- `processing_started_at / processing_completed_at`；
- `response_sent_at`；
- JPEG 字节数和图像尺寸。

客户端记录：

- 采集/编码开始时间；
- 编码完成时间；
- `ws.send` 时间；
- 匹配 ACK 时间；
- ACK 超时、重连和编码档位变化。

计时区间使用 monotonic clock；业务记录的 `created_at` 继续使用 wall clock。没有这些数据时，不能只靠调整帧率推断瓶颈来自 frp、编码还是 OCR。

## 10. 测试计划

### 10.1 状态机单元测试

必须新增：

1. `16.14 × 3 -> 15.62 × 3` 不得播报旧的 `16.14`；
2. 三个最新一致读数最终播报中位数；
3. ARMED 的有效读数可延续到 WEIGHING；
4. 重复 `frame_seq` 不得增加稳定证据；
5. 倒序 `frame_seq/client_ts_ms` 不得增加稳定证据；
6. 旧 epoch 读数不得进入 retry 后的新窗口；
7. 2fps 下三个读数可在默认 1.6 秒最大年龄内稳定；
8. 超出最大证据年龄的读数会被裁剪；
9. 单个异常值后，新的连续稳定后缀能够快速恢复；
10. `mouse_present=False` 不清空 advisory 模式下的窗口；
11. retry 从 ANNOUNCED 进入 WEIGHING，且 epoch 增加；
12. 非 ANNOUNCED retry 返回 `applied=false`；
13. attempt 置信度来自稳定后缀，而不是最后一帧。

### 10.2 WebSocket 契约测试

必须新增：

1. 正常 state 响应携带对应 `frame_seq`；
2. JPEG 解码失败响应携带对应 `frame_seq`；
3. OCR/状态机异常仍返回 `frame_processing_failed + frame_seq`；
4. retry ACK 返回 `applied/state/epoch`；
5. accept/retry 命令不会伪装成帧 ACK；
6. 同一连接按顺序处理至多一个在途帧和一个控制命令；
7. 重连后的重复或倒序帧不会污染稳定窗口。

### 10.3 前端与真机测试

- 验证只有匹配 `frame_seq` 才释放在途帧；
- hello、retry ACK、accept ACK 不会释放错误的帧；
- 处理异常和 3 秒超时后可以继续发送最新画面；
- RTT 很低时发送速率仍不超过 5fps；
- 300–500ms RTT 下不堆帧；
- retry 连续点击不会产生多个命令；
- iPhone Safari 和 Android Chrome 分别测试正常网络、弱网、断线重连；
- 三种编码档位使用相同真实画面做识别质量对比。

## 11. 验收标准

### 正确性

- 播报候选必须由同一 epoch 内至少三条唯一原始读数组成；
- 播报候选必须包含最新有效读数；
- 候选与最新有效原始读数差值不超过 0.10–0.15g；
- `16.14 × 3 -> 15.62 × 3` 不得锁定旧平台；
- retry 后不得复用 retry 前的证据；
- 弱网、异常和重连均不得让前端永久停止取帧。

### 性能

- 正常网络下，从首个有效非零读数到播报：P50 ≤ 0.8s，P95 ≤ 1.5s；
- frp 弱网下：P95 目标 ≤ 2.0s，但首先保证不输出旧帧结果；
- retry 点击到 ACK：P95 ≤ 500ms（网络条件允许时）；
- 服务端处理开始时帧龄：P95 ≤ 500ms；
- 客户端发送频率最高 5fps；
- 任意时刻每个会话最多一个 `pendingFrameSeq`。

### 图像质量

- 默认编码档的 OCR 有效读数率和重量准确率不得显著低于 720×1280 q=0.55；
- 如果 low 档使 OCR 或鼠检测明显下降，则不能仅为降低带宽而设成默认值；
- 留档录像质量不得随实时 JPEG 降档而下降。

## 12. 实施顺序

1. `template.py` 增加可选 `lcd_box`，同步修改测试替身；
2. `realtime.py` 实现原始证据结构、稳定后缀、唯一帧校验和有效 epoch；
3. 修改 retry 返回值和状态语义；
4. `realtime_api.py` 补齐配置、错误 ACK 和协议字段；
5. `mobile.js` 实现匹配 ACK、超时恢复、严格 5fps 限速和 retry ACK 流程；
6. 增加 YAML `realtime` 配置段；
7. 完成状态机与 WebSocket 契约测试；
8. 使用真实手机帧完成三档编码 A/B；
9. iPhone/Android 真机弱网测试；
10. 小流量部署并根据埋点调整 `stable_max_age_s` 和编码档位。

## 13. 本轮暂不包含

- 不修改 frps/frpc 基础设施；
- 不改离线视频分析 pipeline；
- 不训练新的 OCR 或鼠检测模型；
- 不在本轮实现手机端本地 OCR；
- TTS 预激活、voice 选择和错误可见化另列为后续 P1，但不应影响本轮实时识别协议上线。

