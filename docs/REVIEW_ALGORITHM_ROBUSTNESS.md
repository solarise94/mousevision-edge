# 后端算法鲁棒性 Review（上称检测 / OCR 光影 / 读数稳定 / 视频帧率）

> 状态：**review 完成，决策已确认，待落代码**。本文档梳理现有判断逻辑的薄弱点并记录最终决策（见第 6/7 节）。
> 范围：`mousevision/`、`services/lcd_ocr/`、`ui/static/mobile.js`、`ui/app.py`。
> 行号对应当前 `main` 分支（`ee9fa63`）工作区状态。
>
> **修订记录**：
> - v1（2026-07-16）：初版决策。
> - v2（2026-07-16）：经第二轮 review 发现 v1 存在事实性错误，已修正 3 个阻断项 + 5 个 P1/P2 设计缺口（见第 6 节各决策的"v2 修正"子项）。**v1 的若干结论不成立，以 v2 为准。**

---

## 0. 一句话总览

| 问题域 | 现状 | 风险等级 |
|---|---|---|
| 上称检测 | 纯灰度阈值 + 最大连通域，无 max_area、无手套/阴影过滤、无分类 | **高** |
| OCR 光影 | 已有自适应阈值 + 多阈值投票 + CLAHE 变体，但定位层 HSV 固定、92 分位数受反光干扰 | **中** |
| 读数稳定 | 有平台 std 判定 + 原始 P90-P10 复核，但负数无显式标记、WEIGHING 无超时、短会话静默丢弃 | **中** |
| 视频帧率 | 后端时间戳 = `index/fps`（恒定帧率假设），前端 `captureStream(15)` 产出 VFR，截断检测 fps 兜底不一致 | **中** |

---

## 1. 小鼠上称检测：逻辑梳理与脆弱点

### 1.1 现有方案

整个项目判断"秤上是否有小鼠"**只有一个函数**：`mousevision/detect.py:18` 的 `detect_mouse_box()`。

算法（`detect.py:41-59`）：
1. 裁 ROI：`y1=40` 到 `lcd.y-10`，横向 `x_ratio=(0.12, 0.88)`（`detect.py:47`）；
2. BGR→灰度→**反向二值化** `threshold(gray, 70, 255, THRESH_BINARY_INV)`（`detect.py:49`），即"比 70 更暗的像素 = 前景"；
3. 形态学 OPEN 5×5、CLOSE 9×9（`detect.py:50-51`，**硬编码**）；
4. `findContours` 取**最大**连通域（`detect.py:55`）；
5. 面积 `< min_area(800)` 才丢弃（`detect.py:56`）。

参数来自 `configs/scale_refvideo.yaml` 的 `mouse_detect:`，可在 `driver.py:191-193` 覆盖。输出 `bool` 喂给：
- 状态机 ENTER 门控（`detector/__init__.py:139-148`，仅 http_ocr 路径启用 `require_mouse_for_enter`）；
- 状态机 LEAVE 保持（`detector/__init__.py:161-173`：`confirmed_zero and mouse_present` → 卡在 WEIGHING）；
- 时序融合零显示保持（`fusion/temporal.py:63-65`）；
- 照片选择（`driver.py:441-446`）。

### 1.2 为什么手套会被算进去

**代码库中没有任何手套/手部/阴影/皮肤/颜色的过滤**（全库 grep `glove|手套|hand|finger|shadow|skin` 无命中）。具体原因：

1. **`gray_thr=70` 是全局亮度假设**。深色丁腈手套（蓝/黑，灰度常 <70）会被阈值化成一大块前景。换光照、换秤台背景色，阈值会静默失效。没有 Otsu/自适应阈值/背景减除。
2. **只有 `min_area`，没有 `max_area`**（`detect.py:56`）。盖住整个 ROI 的手套跟一只小鼠同样"有效"。任何大于 800px（约 28×28）的暗块都通过。
3. **`max(contours, key=area)`**（`detect.py:55`）。手套一旦进入 ROI 就会成为最大轮廓，淹没小鼠信号。
4. **没有形状/长宽比/纹理/分类约束**。`driver.py:420-431` 的 `_pan_overlap_ok` 只校验斑块底部是否落在秤盘中线以下——挡不住"戴着手套的手放在秤盘上"。
5. **ROI 本身就是人手活动区**（`y1=40` 到 `lcd.y-10`，覆盖秤台正上方）。

**后果**：戴手套抓放小鼠时，手套误判会让 `mouse_present=True`：
- 若小鼠已离开但手套还在（读数回零），状态机因 `confirmed_zero and mouse_present`（`detector/__init__.py:165`）**永远卡在 WEIGHING**，只有连续 OCR 不可读（`missing`）才能解锁；
- 反过来，纯手套进场也能满足 ENTER 门控（http_ocr 路径），开出幽灵会话。

### 1.3 建议方案（从轻到重，可组合）

**方案 A：约束检测器几何（低成本，立刻可做）**
- 加 `max_area`：小鼠在 720×1280 ROI 里的斑块面积有合理上限（可从 RefVideo 统计，例如典型鼠标框 30×60 ~ 90×140，按像素面积设上限）。超出上限的块判为"非小鼠"或 `unknown`。
- 加长宽比约束（`bw/bh` 在小鼠的合理区间，例如 0.3~2.0）。手套摊平/手掌握住时长宽比会异常。
- **`detect.py` 内的二值化改为自适应**：用 Otsu 或基于 ROI 局部直方图的自适应阈值，替代固定 `gray_thr=70`，缓解光照漂移。
- ROI 采样自 `weight_roi` 配置（`configs/scale_refvideo.yaml:16-22` 已有秤盘像素坐标），把检测 ROI 严格收窄到**秤盘**，而不是"LCD 下方整片区域"。

**方案 B：背景减除 / 帧差（中成本，抗光照）**
- 系统本身就有"空秤"基准（会话开始前是 EMPTY）。维护一个空秤背景图（例如会话开始时或定期更新），用 `absdiff` + 自适应阈值做运动/前景检测。手套和手套阴影在"背景图"里不存在，而真正放到秤上的物体才被检出。比固定亮度阈值鲁棒得多。
- 注意：纯帧差对"静止不动的小鼠"会失效，需结合方案 C 或保留灰度兜底。

**方案 C：轻量目标检测（高成本，最稳）**
- 训练/微调一个小模型（YOLO-nano 或 MobileNet-SSD）做"小鼠 vs 手套 vs 空"三分类，或至少做"前景块是否为小鼠"的二分类验证。标注成本用现有录制视频即可（已经有 clip.mp4 产出）。
- 模型只对"灰度阈值检出的大块"做二次确认，不需要逐帧跑，延迟可控。

**方案 D：逻辑层兜底（无论检测器用哪个都该加）**
- 给 WEIGHING 状态加**最大时长**（见 §3.3）。即使 `mouse_present` 一直 True，超过 N 秒也强制 LEAVE → ANALYZE，避免手套卡死。
- LEAVE 保持规则不要 100% 信任 `mouse_present`：当读数**持续稳定归零**（连续多帧 ≤ leave_max）时，即使 `mouse_present=True` 也应允许进入 LEAVE（当前是"零 + 鼠在 = 无限保持"，见 `detector/__init__.py:165`）。

**我的倾向**：先做 **A（几何约束 + 自适应阈值 + 收窄 ROI 到秤盘）+ D（WEIGHING 超时 + 归零优先）**，这两步能消除绝大多数手套误判和卡死问题，成本最低。模型（C）作为后续增强。背景减除（B）需要稳定的空秤基准，对"用户中途放手套"也敏感，可作为 A 的补充而非替代。

---

## 2. OCR 光影鲁棒性

### 2.1 现有方案（已经做了不少，值得肯定）

- **自适应二值化**：`sevenseg_classic.py:39-41` 等处用 `percentile(gray,92) × 0.88~0.90` 而非固定阈值，阈值随整体亮度漂移；
- **5 阈值投票**：`sevenseg_classic.py:241-271` 对同一 patch 用 5 个 `thr_scale` 二值化各解一次，取共识；
- **自动反相**：处理黑底白字 / 白底黑字翻转；
- **CLAHE 变体 + raw/CLAHE 投票**（`engine.py:257-346`、`normalize.py:353-364`）；
- **连续横段几何**（`sevenseg_classic.py:87-116`）专治 CLAHE bloom 导致的 1→7；
- **ink_ratio 质量门**（`quality.py:64-130`）拒绝反光残留 / 过渡帧。

这套组合拳是合理的，1/7、4/9 混淆有专门的消歧（`temporal.py:97-106,119-138`）。

### 2.2 对光影仍敏感的点

**点 1：定位层 HSV 固定阈值**（`locator.py:150-151`，`hsv_low=(90,40,80)`）
- V 下限 80、S 下限 40 硬编码。强逆光/弱光下 LCD 蓝底饱和度或亮度跌破阈值 → 定位失败 → 整帧不可读。
- 虽然 yaml 可覆盖，但**不自适应**。建议：HSV 范围用相对统计（如以画面中蓝色像素的中位 S/V 为基准浮动），或加 Otsu 回退。

**点 2：92 分位数假设**（`sevenseg_classic.py:39` 等多处）
- 假设"数字笔画是画面最亮的 ~8%"。当 LCD 有强反光高光斑时，反光成为 92 分位 → 阈值过高 → 真实笔画被切掉。这是 `0.00→11.11`（反光残留被切成 4 个窄槽）的可能根因之一。
- 建议：反光高光斑通常是局部连通的极亮块，可在二值化前先检测并 mask 掉（连通域 + 高亮度阈值），再做百分位统计。

**点 3：二值化逻辑重复且参数不一致**
- 4 处 `_to_binary`（`sevenseg_classic.py`、`quality.py`、`normalize.py`、`segodec_adapter.py`），thr 下限分别是 160/150/165/155，比例 0.90/0.88/0.88/0.90。
- 后果：**切槽（normalize）和解码（sevenseg）可能看到不同的笔画**。建议统一为一个带参数的函数，切槽和解码用同一组阈值（或至少保证 decode 用切槽时的同一二值图）。

**点 4：CLAHE bloom 是结构性矛盾**
- `engine.py:271` 注释自己承认 CLAHE 会把"1"晕开成"7"。当前靠 raw 变体 + 物理范围过滤缓解，但两个变体经常给出不同首位数字（硬冲突 → transition → 丢帧）。
- 建议：对含窄字符的 strip，CLAHE 的 `clipLimit` 降到更低（如 1.5），或干脆不给窄字符槽走 CLAHE 变体；把 raw/CLAHE 的分歧降级为"软证据"而非硬冲突。

**点 5：投影分槽在反光时脆弱**（`normalize.py:152-220`）
- 基于垂直墨水投影，反光的水平亮线会被切成多个窄 run。虽有合并启发式，但 `quality.py:110-119` 的 `pseudo_narrow_ones` 检测说明仍常失败。
- 建议：分槽前先做反光 mask（同点 2）；或分槽结果用 `tall_glyph_ranges`（`quality.py:37-61`）的连通组件数校验一致性。

### 2.3 建议

最高优先级是 **点 2（反光高光斑 mask）+ 点 3（统一二值化）**，这两个能直接减少 `0.00→11.11` 这类反光误读，且改动局部。点 1（HSV 自适应）次之，主要影响极端光照场景。点 4/5 是更深的结构性问题，建议结合 RefVideo/0001 双视频门禁（`accept_refvideo.py`）持续回归。

---

## 3. 读数稳定判定

### 3.1 现有方案（两层 + 原始复核）

**第一层：时序融合**（`fusion/temporal.py`）——对原始单帧 OCR 做 `window_size=8` 滑动窗口聚类投票（`weight_tol=0.08`），产出喂给状态机的"稳定点"。

**第二层：曲线平台**（`analyzer/__init__.py:167-191`）——在状态机曲线上找 0.8s 窗口内 `std ≤ 0.35` 的"稳定平台"，**用标准差判定**，最终重量取窗口内 **IQR 剔除后的中位数**（`analyzer/__init__.py:261`）。

**第三层：原始复核**（`driver.py:331-381::_apply_raw_instability`）——即融合曲线看着稳，也用平台窗内**原始** OCR 的 `P90-P10` 跨度复核，≥0.5g 强制转人工。

这套设计是严谨的（中位数 + IQR + P90-P10 都是为了抗尖峰）。但有几个真实场景的缺口。

### 3.2 负数处理（你提到的"称本身出现负数"）

**现状**：负数拦截**只在旧的 `read_weight()` API**：
- `http_ocr.py:115`：`if obs.weight < 0 or obs.weight > 80: return None`
- `template.py:441`：同样

**问题**：生产主路径走的是 `read_observation()`（`driver.py:206`），它**不检查负数**。负数会原样进入 `RawWeightObservation`，后续靠 `temporal.py:84` 的 `min_weight(0.0) <= weight <= max_weight(50.0)` 区间过滤丢掉。

**后果**：
- 这是"区间过滤"而非"显式负数异常"。负数被当成"不可读/越界"丢掉，**记录里看不出原因**，没有 `negative_weight` 审计字段。真实秤频繁报负数时，你无法从记录里定位"这是秤的问题还是 OCR 的问题"。
- 更隐蔽：若秤在称重过程中短暂报负（如小鼠刚跳下时秤的惯性），这些帧被丢，曲线可能出现空隙，影响 `min_platform_points` 判定。

**建议**：
- 在 `read_observation()` 里**显式标记**负数（如 `RawWeightObservation` 加 `negative: bool` 或在 `status` 里加 `negative_weight`），而不是静默区间过滤；
- 统计每个会话的负数帧占比，若占比过高（如 >30%），在记录里标注"称状态异常"，提示运维检查秤；
- 负数不应进入聚类（现状正确），但要留痕。

### 3.3 "小鼠甩上称还没稳定就拿走"（你提到的关键场景）

**现状**：代码**没有显式的 WEIGHING 超时**。状态机（`detector/__init__.py`）一旦进入 WEIGHING（连续 5 个非零样本，`detector/__init__.py:151-156`），只有"连续 10 帧近零/丢失"才能走到 LEAVE（`detector/__init__.py:161-171`）。

"甩上称→不稳定→拿走"实际如何收尾：
1. 读数一直 > `leave_max(0.30)` → 永远停在 WEIGHING；
2. 下称后近零 10 帧 → LEAVE → ANALYZE；
3. 曲线上找不到 0.8s 的 std≤0.35 窗口 → 进入兜底（`analyzer/__init__.py:212-230`），挑 std 最小窗口猜一个值，**强制 `requires_manual_weight=True`**；
4. `driver.py:588` 再用原始 P90-P10 复核，多半再加人工标记；
5. 人工确认的记录不进上传队列，等人工补值（`recorder.py:93`）。

所以**兜底逻辑是"事后人工"，不是"超时拒绝"**——这一点本身设计是对的（不丢会话，让人工兜底）。但有三个真实失效点：

**失效点 1：没有最大 WEIGHING 时长**。小鼠长时间抖动压秤，会话无限拉长直到视频结束。建议加 `max_weighing_seconds`（如 30s），超时强制 LEAVE → ANALYZE → 走人工兜底。

**失效点 2：曲线点数 <5 时 `analyze` 返回 None**（`analyzer/__init__.py:129`），`driver.py:562-568` 直接 `finish_analyze` 清空——**短时上称会被静默丢弃，不留任何记录、不标人工**。建议：即使是极短会话，也产出一条 `requires_manual_weight=True` 的记录（带"会话过短"原因），而不是 None。否则用户根本不知道这次称重发生过。

**失效点 3：短会话用中段中位数绕过 std 检查**（`analyzer/__init__.py:159-165`，`duration_ms < window_ms*0.5` 即 <0.4s 时直接取 25%~75% 段中位数）。极短抖动会话可能把抖动读数当有效值。建议：短会话也走 std 判定，std 不达标就转人工，不要因为"点少"就放宽。

### 3.4 配置不一致（顺带提）

- `temporal.py:22` 的 `near_zero` 默认 0.15，但 `driver.py:97` 用 `cfg.get("near_zero", 0.5)` 覆盖成 0.5。yaml 漏配时行为分裂。建议统一默认值并在一处定义。
- fps 兜底不一致见 §4。

---

## 4. 前端录制与视频帧率（你提到的"动态帧率/动态码率导致后端帧率裁切出问题"）

### 4.1 现状

**前端**（`ui/static/mobile.js`）：
- `getUserMedia` 请求 `frameRate: {ideal:15, max:30}`（`mobile.js:312`）——**请求值，不保证**；
- canvas 绘帧用 `requestVideoFrameCallback`（`mobile.js:1017`），**跟随源流真实速率**，不是固定 15fps；
- `canvas.captureStream(15)`（`mobile.js:1153`）——**请求 15fps 采样，但源流是 VFR 时输出也是 VFR**；
- `videoBitsPerSecond: 1500000`（`mobile.js:1160`）——固定码率；
- `pickMime()`（`mobile.js:379-388`）优先 MP4/H.264，webm 兜底；
- `recorder.start()` 无 timeslice（`mobile.js:1195`）。

**结论**：前端产出的视频是**可变帧率（VFR）、固定码率**的 MP4（或 webm）。

**后端**（`mousevision/source/video.py`）：
- 帧率从 ffprobe 的 `avg_frame_rate`/`r_frame_rate` 读（`video.py:120-140`）；
- 读不到兜底 **30.0**（`video.py:298-299,337-338`）；
- **时间戳 `timestamp_ms = (index / fps) * 1000`**（`video.py:312,385`）——这是**恒定帧率假设**：用容器声明的平均 fps 给每帧均匀分配时间；
- **没有任何 PTS（真实 presentation timestamp）读取**（全库无 `pts` 处理）；
- 帧率裁切 `frame_stride` 按**帧号取模**（`video.py:315,388`：`index % frame_stride == 0`），不是按时间。

### 4.2 动态帧率会导致什么具体问题

**问题 1：时间戳偏离真实物理时间**
- VFR 视频（如前 2 秒 30fps、之后掉到 8fps）下，后端用平均 fps（比如 15）给每帧均匀分时间，时间戳会**与真实物理时间错位**。
- 状态机的所有时间窗判定都依赖 `frame.timestamp_ms`：ENTER/WEIGHING/LEAVE 时序、平台窗 `platform_window_seconds=0.8`（`analyzer/__init__.py:158`）。VFR 下这些判定会漂移。比如真实的 0.8s 稳定窗口，可能被错算成 1.6s 或 0.4s。

**问题 2：帧率裁切（stride）在 VFR 下采样不均**
- `index % 2 == 0` 假设帧时间均匀。VFR 下，偶数帧可能在时间上集中在某段（密集）或稀疏，导致抽出的帧时间分布严重偏斜，平台 std 判定失真。

**问题 3：截断检测误判**（`mousevision/jobs.py:797-812::_check_truncation`）
```python
fps = ... or 15.0   # jobs.py:804 兜底 15
decoded_duration = decoded_frames * stride / fps   # jobs.py:805
if decoded_duration < recorded * 0.5:   # jobs.py:808
    raise VideoFormatError("视频格式异常...")
```
- VFR 下 ffprobe 报的 fps 不准。若报高（如峰值段主导 `r_frame_rate`→30），`decoded_duration` 被高估，损坏 clip 蒙混过关；若报低，正常 clip 被误判损坏 → 触发 `_rollback_persisted_run`（`jobs.py:836+`）→ **已写盘的分析结果被回滚，用户看到"请重录"误报**。

**问题 4：fps 兜底值不一致**
- `video.py` 解码兜底 **30.0**，`jobs.py:804` 截断校验兜底 **15.0**。同一个 clip 解码用 30、校验用 15，边界 case 下放大误判。

**问题 5：码率虽固定，但码率本身不影响帧率裁切**——你提到的"动态码率"其实当前前端已经固定为 1.5Mbps（`mobile.js:1160`），所以码率不是问题，**真正的风险是动态帧率**。

### 4.3 建议

**核心建议：后端改用真实时间戳（PTS），彻底摆脱恒定帧率假设。** 这是治本方案，且不依赖前端配合。

具体做法（按优先级）：

**方案 1（推荐，治本）：后端读取逐帧 PTS**
- 改 `video.py` 用 ffprobe `-show_packets` 或 ffmpeg 解码时输出 `pts_time`，用真实 PTS 作为 `timestamp_ms`，而不是 `index/fps`。
- 帧率裁切 `stride` 改为**按时间间隔采样**（如每 1/15 秒取一帧），或保留帧号 stride 但用 PTS 修正时间戳。
- 这样无论前端 VFR 多严重，后端的时间窗判定都准确。
- 代价：ffprobe `-show_packets` 对长视频较慢；但称重视频通常很短（几十秒），可接受。需要更新 `test_video_ffmpeg_backend.py` 的断言。

**方案 2（前端配合，治标）：前端强制固定帧率输出**
- 前端 `canvas.captureStream(15)` 改为在 canvas 上**用定时器固定 15fps 绘帧**（`setInterval(draw, 1000/15)` 或在 `requestVideoFrameCallback` 里做时间节流），保证 canvas 流真的是 15fps CFR。
- 这样后端的 `index/fps` 假设就成立。
- 代价：丢弃多余的源帧，可能轻微降低高速运动时的清晰度（对静态称重读数无影响）。

**方案 3（兜底加固）：统一 fps 兜底 + 放宽截断检测**
- `video.py` 和 `jobs.py` 的 fps 兜底统一（都用 15.0，因为前端目标就是 15fps）。
- 截断检测 `decoded_duration < recorded * 0.5` 的 0.5 阈值对 VFR 不够稳，建议改用"解码帧数 vs 录制时长 × 最低预期 fps"（如 `recorded * 10` 帧）这类不依赖容器 fps 的判定，或对 VFR 视频跳过该检查。
- 截断误判**不应触发自动回滚**，至少应转人工确认，避免正常录制结果丢失。

**关于"前端录制后视频编码格式全部一致"**：当前 `pickMime()` 已经优先 MP4/H.264，webm 是兜底。如果你希望**强制统一**，可以：
- 前端检测到不支持 MP4 时直接提示用户换设备/浏览器，而非回退 webm（但会牺牲兼容性）；
- 或后端在上传后做一次**归一化转码**（`source.mp4` 之外统一转一份 CFR H.264 给分析用）。不过后端转码会增加延迟和依赖，建议优先做方案 1（PTS），它对 mp4/webm 都有效。

**我的倾向**：**方案 1（PTS）+ 方案 3（统一兜底）**。PTS 是根治，且让后端对"用户用任何设备录的 VFR 视频"都鲁棒。前端固定帧率（方案 2）可作为额外保险，但不应该是唯一的防线——因为用户也可能手动上传非本前端录制的视频。

---

## 5. 修改优先级建议（汇总）

| 优先级 | 修改项 | 模块 | 理由 |
|---|---|---|---|
| **P0** | 后端读取逐帧 PTS，替代 `index/fps` | `video.py` | 根治 VFR 导致的时间窗/裁切/截断误判 |
| **P0** | 统一 fps 兜底值；截断误判不自动回滚 | `video.py`、`jobs.py` | 避免正常录制结果被 VFR 误判回滚 |
| **P0** | 给 WEIGHING 加最大时长；曲线 <5 点也产人工记录而非 None | `detector/`、`analyzer/` | 避免会话卡死 / 静默丢失 |
| **P1** | 上称检测加 max_area + 长宽比 + ROI 收窄到秤盘 | `detect.py`、yaml | 低成本消除手套误判 |
| **P1** | 二值化前 mask 反光高光斑；统一 4 处 `_to_binary` | `sevenseg_classic.py` 等 | 减少 `0.00→11.11` 反光误读 |
| **P1** | `read_observation` 显式标记负数，统计会话负数占比 | `http_ocr.py`、`observations.py` | 让秤异常可追溯 |
| **P2** | 上称检测自适应阈值（Otsu）/ 背景减除 | `detect.py` | 抗光照漂移 |
| **P2** | 定位层 HSV 范围自适应 | `locator.py` | 极端光照场景 |
| **P2** | 短会话也走 std 判定，不放宽 | `analyzer/__init__.py:159-165` | 防抖动读数当有效值 |
| **P3** | 轻量目标检测验证小鼠 vs 手套 | 新模块 | 根治形状误判 |
| **P3** | CLAHE clipLimit 分场景调；raw/CLAHE 分歧降级为软证据 | `normalize.py`、`engine.py` | 减少 1/7 硬冲突 |

---

## 6. 已确认的决策（2026-07-16 review 讨论）

经过逐项讨论，所有待定问题已拍板。下面是最终决策，作为落代码的依据。

### 决策 1：上称检测 —— 几何约束 + 状态机兜底，模型留作 P3 评估
- **现在做（P1）**：给 `detect.py` 加 `max_area`（小鼠斑块面积上限）+ 长宽比约束；二值化改自适应阈值（Otsu）。所有阈值做成 yaml 可调，部署后用真实数据调。
- **后续评估（P3）**：若几何约束在真实数据不够稳，评估开源模型。候选 DAMM（Detect Any Mouse Model）——为"复杂环境下定位小鼠"设计的预训练检测器；但需先用本项目的称重视频（含手套）验证其对手套的区分能力，不达标则用 Roboflow mouse 数据集 + 自有 clip.mp4 标注微调，导出 ONNX 在边缘 CPU 跑，只对灰度检出的大块做二次确认。
- **理由**：几何约束改动局部、不需训练数据、能消除绝大多数手套误判和卡死；模型工程量大（标注+训练+集成+回归），先用低成本方案验证，不够再上。

> **v2 修正（阻断项，必读）**：
> 1. **ROI 不能复用 `weight_roi`。** v1 计划"复用 yaml 已有的 weight_roi 收窄鼠检测范围"是**错误的**——已核实 `configs/scale_refvideo.yaml:18-22` 的 `weight_roi: {x:145,y:780,w:430,h:110}` 是 **LCD 显示屏定位区**（`find_lcd_box` 的 `fixed_roi`，见 `template.py:365-366`、`http_ocr.py:74-80`），而鼠检测当前搜索的是 LCD **上方**区域（`detect.py:47` 的 `y2=lcd.y-10`）。直接复用会让检测器盯着显示屏。**正确做法**：在 `mouse_detect:` 下新增独立的 `mouse_detect.roi` / `pan_roi`（秤盘像素坐标），需用实际视频标定，不可与 `weight_roi` 共用。
> 2. **DAMM 许可证措辞修正。** v1 称"许可兼容、基于 detectron2/Apache-2.0 无传染风险"**不能成立**——已查证 [DAMM 仓库](https://github.com/backprop64/DAMM) 根目录未提供 LICENSE 文件；Detectron2 的 Apache-2.0 只覆盖 Detectron2 本身，**不会自动授权 DAMM 自身代码或模型权重**。正确表述：**DAMM 许可证待作者确认，未确认前仅做内部评估，不集成、不分发。**

### 决策 2：读数稳定 —— 负数端到端支持 + WEIGHING 30s 超时（含清秤等待）+ 短会话统一转人工
- **WEIGHING 超时（2B）**：设 **30s** 最大时长。小鼠正常称重 5~10s 就稳，30s 还没稳基本是卡住/抖动。超时强制进入人工兜底。
- **短会话（2C）**：不管多短，只要触发过称重就产出一条 `requires_manual_weight=True` 的记录并导出 clip.mp4。同时短会话也走 std 判定，不再用中段中位数绕过（`analyzer/__init__.py:159-165` 的放宽逻辑移除）。

> **v2 修正（阻断项，必读）**：
> 1. **负数不能只加字段——OCR 根本不识别负号（2A）。** v1 计划"给 `RawWeightObservation` 加 `negative` 字段统计占比"是**无效的**——已核实经典七段解码的字符类别只有 `blank | 0-9 | invalid`（`sevenseg_classic.py:27`），没有负号类别；且文本解析 `parse.py:20` 的 `re.sub(r"[^0-9.,]", "", text)` 会**主动删除 `-`**。也就是说当前 OCR **根本无法产出负数观测**，加字段永远收不到数据。**正确做法（端到端，前置依赖）**：
>    - 先在 OCR **服务端**（`services/lcd_ocr/`）增加负号区域检测（秤显示屏左侧/数字前的符号位），新增 `negative_display` 状态到 `schemas.py` 的 status 枚举；
>    - `RawWeightObservation` 透传该状态到主程序；
>    - **再**做占比统计。占比分母需明确定义：用"会话期间 OCR 可读帧总数"而非"所有帧"；并定义"全程负数、从未 ENTER"的归属（建议计为会话级秤异常标记，不产出重量记录）。
> 2. **WEIGHING 超时必须配套清秤等待，否则会拆会话（2B 阻断）。** v1"超时强制 LEAVE→ANALYZE"不完整——已核实 `finish_analyze()`（`detector/__init__.py:183-188`）会 `_set_state(EMPTY)` 并 `_arming=True`；若超时时鼠**仍在秤上**，冷却结束（`reenter_cooldown_ms`）后会再次 ENTER，把同一只鼠拆成多个会话/多条记录。**正确做法**：新增 `WAIT_CLEAR`/`TIMED_OUT` 状态——超时只生成**一次**人工记录，然后**等待连续归零或确认鼠离开**才重新武装（`_arming=True`），而非无脑回 EMPTY。
> 3. **短会话不能只修 `len(curve)<5`（2C 阻断）。** v1 只提到这一处 `None` 路径，但 `analyzer.analyze` 返回 `None` 的路径有多条：过滤后不足 5 点（`analyzer/__init__.py:129`）、全零、非零段过短、找不到候选窗等。**正确做法**：在 **`SessionDriver` 边界**统一拦截——凡"触发过会话（曾进入 ENTER/WEIGHING）但 `analyze` 返回 `None`"，一律转为人工记录，而不依赖 analyzer 内部各 `None` 分支分别处理。同时需定义无合理猜测时的字段表示：`weight=None`、`guessed_weight=None`、`photo_frame_index` 取最后一次有效帧或 `None`，`review_reason` 标明"分析无结果/会话过短"。

### 决策 3：视频帧率 —— 后端读 PTS 治本（实现需收敛）+ 截断可疑记录隔离闭环
- **治本方向**：改 `video.py` 用逐帧真实 PTS 作为 `timestamp_ms`，替代 `index/fps` 恒定帧率假设。
- **截断方向**：`jobs.py:_check_truncation` 判异常时不自动销毁数据，改为隔离待人工确认。
- **附带的统一兜底**：`video.py`（解码）和 `jobs.py`（截断校验）的 fps 兜底值统一为 15.0。
- **理由**：后端是对所有视频的最后一道防线，PTS 对任何设备/来源的 VFR 视频都有效。

> **v2 修正（阻断项，必读）**：
> 1. **PTS 实现必须收敛为一种，且不能用 packet PTS（P1）。** v1 笼统说"用 ffprobe/ffmpeg 读逐帧 PTS"，但已核实当前 `video.py:343-357` 的 ffmpeg 命令只输出裸 `rawvideo` 管道，**完全丢弃了时间戳元数据**，packet PTS 无法和 rawvideo 帧按序对应（B-frame 下 packet 顺序 ≠ 展示顺序）。**正确做法**：明确选用以下之一，且整库统一：
>    - **方案 a（推荐）**：ffmpeg 加 `-vf showinfo`，解析 stderr 每帧的 `pts_time`（与解码输出严格同步）；
>    - **方案 b**：ffprobe `-show_frames -select_streams v` 读 `best_effort_timestamp_time`。
>    **禁止**用 packet `-show_packets` 直接按序映射 rawvideo 帧。必须处理：非零/负起始 PTS 的归一化、缺失 PTS 的回退（退回 `index/fps` 并标记）、非单调时间戳。**截断判断随后应基于首尾解码 PTS 的真实时长**，不再用 `frames × stride / fps`。**验收测试必须包含真实 VFR fixture**（非合成 CFR 视频改个标称 fps）。
> 2. **"截断不回滚"必须配套隔离机制，否则可疑记录会继续同步（阻断）。** v1 说"不回滚、标记待人工"不完整——已核实调用顺序：driver 在 `_run_pipeline`（`jobs.py:629`）里已跑完分析并让记录**入队**（`driver.py:724-731`），`_check_truncation`（`jobs.py:766`）是**之后**才跑的。仅"保留文件+标记人工"会让可疑的部分记录仍在队列里作为正常数据被上传/核对/发布。**必须定义完整隔离闭环**：
>    - job/run 级标记 `format_suspect`；
>    - 该 run 的所有记录进入 **Held/Quarantined 状态**：禁止上传、禁止核对通过、禁止发布；
>    - 人工确认"视频完整"→ 统一释放（Held→正常，放行上传）；
>    - 人工确认"确实截断"→ 删除记录，并处理已占用序号（释放或标记跳过，避免后续编号冲突）。

### 决策 4：录制格式 —— 前端强制 MP4/H.264（移除所有回退）+ 后端校验 codec
- 目标：强制单一 MP4/H.264 格式，去掉 webm 兜底。

> **v2 修正（P1，必读）**：v1 只说"改 `pickMime()`"**不够**。已核实 `mobile.js:380-388` 的 `pickMime()` 在无 MP4 支持时返回 `""`，而 `mobile.js:1162` 的 `new MediaRecorder(canvasStream, opts)` 在 `mimeType` 为空时**仍会创建 recorder**（浏览器默认容器，通常 `video/webm`）；`mobile.js:1164` 还有 `catch` 后无 MIME 重试。**正确做法**：
> - 前端：`pickMime()` 无 H.264 MP4 支持时**抛错**而非返回 `""`；移除 `mobile.js:1162-1164` 的无 MIME 构造和 catch 回退，构造失败直接终止录制并提示用户。
> - 后端：上传后用 ffprobe 校验 `codec_name=h264` 且容器为 mov/mp4——**`video/mp4` 这个 MIME 本身不严格保证 H.264**（可能是 HEVC/H.265 的 mp4），必须显式校验 codec。校验失败转人工/要求重录。

### 决策 5：数据飞轮 —— 两阶段，全量收集，保存真实 OCR 输入，训练资产独立隔离
背景：人工修正流程已存在（`requires_manual_weight` → clip.mp4 → 回放确认 → 补值），但流程产出数据**不足以喂训练**——单帧 OCR 原始观测只在内存（`driver.py:567` clear）、LCD 裁剪图不存、检测框 bbox 不落盘、正常记录无修正入口、无误检标签。好消息是 `tools/explain_session_replay.py` 已证明这些数据技术上都能产出，只需接到生产流程。

**阶段一（P1，纯被动收集，零打扰）**：每次称重自动额外保存训练原材料。
**阶段二（P2，主动标注，小幅前端改动）**：放开修正入口 + 误检标签。
**收集范围**：全量收集（所有会话），带保留策略，加 env/yaml 开关默认开。

> **v2 修正（P1，必读）**：
> 1. **保存的必须是 OCR 实际输入，不是原图轴对齐裁剪（阻断）。** v1 说"复用 `explain_session_replay._crop_quad()` 保存 LCD crop"是**错误的**——已核实 `_crop_quad()` 只是对原图 LCD bbox 做轴对齐裁剪；而生产 OCR 真正使用的是经过**规范化（warp/裁切）、digit ROI 截取、raw/CLAHE 变体选择后的 strip**。保存 `_crop_quad` 的产物训练不出对生产有用的 OCR 模型。**正确做法**：让 OCR 服务在响应中**返回或可选落盘实际选中的 normalized strip**（以及 digit slot patches），主程序保存这个真实输入。需扩展 lcd-ocr 服务的响应 schema 增加可选的 strip 图（base64 或路径）。
> 2. **建立逐帧 manifest，绑定全部上下文（P1）。** 每个会话产出一个逐帧 manifest（jsonl 或 parquet），每行包含：`frame_index, pts_ms, raw_observation(digits/conf/status), screen_quad, mouse_bbox, strip_crop_path, digit_slot_paths, model_version, config_hash, schema_version`。这样修正/标注事件才能精确关联到具体帧和具体模型输入。
> 3. **训练资产必须独立子目录，保留策略只能清训练资产（P1）。** v1 说"保留策略清理旧的"有风险——可能误删 `record.json`、photo.jpg、待人工确认的 clip.mp4 或已标注样本。**正确做法**：训练资产（ocr_observations.jsonl、crops、strips、slots、manifest）统一放 `mouse_NNN/training_assets/` 子目录；保留策略（按天数/限额）**只扫描该子目录**清理，绝不触碰 record.json/photo.jpg/clip.mp4。已人工标注（有 detection_label 或修正过的）样本应排除在自动清理外或单独长期保留。

---

### 顺带保留的 OCR 光影改进（未单独讨论，沿用 §2.3 建议）
- P1：二值化前 mask 反光高光斑（减少 `0.00→11.11`）；统一 4 处 `_to_binary` 实现。
- P2：定位层 HSV 范围自适应。
- P3：CLAHE clipLimit 分场景调，raw/CLAHE 分歧降级为软证据。

---

## 7. 落地优先级（v2，经第二轮 review 调整）

> 顺序遵循第二轮 review 认可的总体顺序：**PTS / 隔离机制 / 超时状态机 → 短会话 → 独立 pan ROI → 负号端到端 → 数据飞轮 P1**。
> "依赖"列标注前置项；有依赖的项必须在其前置完成后才能开始。

| 批次 | 修改项 | 对应决策 | 模块 | 依赖 |
|---|---|---|---|---|
| **P0-a** | 后端 PTS：用 `-vf showinfo` 解析 `pts_time`（或 `best_effort_timestamp_time`），替代 `index/fps`；处理非零/负起始 PTS、缺失/非单调 PTS 回退；统一 fps 兜底 15.0 | 决策 3 | `video.py`、`test_video_ffmpeg_backend.py` | 无；**验收须含真实 VFR fixture** |
| **P0-b** | 截断隔离闭环：`format_suspect` 标记 + Held/Quarantined 状态（禁上传/核对/发布）+ 人工确认释放/拒绝；截断时长改用首尾 PTS；处理已占序号 | 决策 3 | `jobs.py`、`upload_queue.py`、`records_meta.py`、UI | P0-a（依赖真实 PTS 算截断） |
| **P0-c** | WEIGHING 30s 超时 + 新增 `WAIT_CLEAR`/`TIMED_OUT` 状态（只产一次人工记录，等清秤才重新武装） | 决策 2 | `detector/__init__.py` | 无 |
| **P1-a** | 短会话统一转人工：在 `SessionDriver` 边界拦截"曾进入会话但 analyze 返回 None"的所有路径；定义无猜测时字段表示 | 决策 2 | `driver.py`、`analyzer/__init__.py` | 无 |
| **P1-b** | 新增独立 `mouse_detect.roi`/`pan_roi`（秤盘像素，实际视频标定，不复用 `weight_roi`）+ `max_area` + 长宽比 + 自适应阈值(Otsu) | 决策 1 | `detect.py`、yaml | 无 |
| **P1-c** | 负号端到端：OCR 服务端负号区域检测 + `negative_display` 状态 → 透传 → 占比统计（定义分母=可读帧，定义全程负数归属） | 决策 2 | `services/lcd_ocr/`、`observations.py`、`http_ocr.py` | 无（但须先于依赖它的统计逻辑） |
| **P1-d** | 前端强制 MP4/H.264：`pickMime` 无 H.264 抛错、移除无 MIME 回退；后端 ffprobe 校验 `codec_name=h264` | 决策 4 | `mobile.js`、`app.py` | 无 |
| **P1-e** | 数据飞轮 P1 被动收集：OCR 服务返回真实 chosen strip（扩展 schema）+ 逐帧 manifest + 训练资产独立子目录 + 仅清训练资产的保留策略 | 决策 5 | `services/lcd_ocr/`、`driver.py`、`recorder.py` | P1-c（strip 含负号信息后才有意义） |
| **P1-f** | OCR 光影：二值化前 mask 反光高光斑；统一 4 处 `_to_binary` | §2.3 | `sevenseg_classic.py` 等 | 无 |
| **P2-a** | 定位层 HSV 范围自适应 | §2.3 | `locator.py` | 无 |
| **P2-b** | 数据飞轮 P2 主动标注：放开正常记录修正入口（留 `original_weight`）+ `detection_label` + 事件帧绑定 | 决策 5 | `pc.js`、`app.py`、`records_meta.py` | P1-e |
| **P3** | 评估/集成小鼠检测模型（DAMM 候选，**许可证未确认前仅内部评估**） | 决策 1 | 新模块 | P1-b 数据沉淀后 |

---

## 8. 待你确认的几个问题

（已全部确认，见第 6 节。本节保留作为历史记录。）
