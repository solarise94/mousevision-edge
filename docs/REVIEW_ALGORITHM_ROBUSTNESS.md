# 后端算法鲁棒性 Review（上称检测 / OCR 光影 / 读数稳定 / 视频帧率）

> 状态：**review 完成，决策已确认，待落代码**。本文档梳理现有判断逻辑的薄弱点并记录最终决策（见第 6/7 节）。
> 范围：`mousevision/`、`services/lcd_ocr/`、`ui/static/mobile.js`、`ui/app.py`。
> 行号对应当前 `main` 分支（`ee9fa63`）工作区状态。
>
> **修订记录**：
> - v1（2026-07-16）：初版决策。
> - v2（2026-07-16）：经第二轮 review 发现 v1 存在事实性错误，修正 3 个阻断项 + 5 个 P1/P2 设计缺口。
> - v3（2026-07-16）：经第三轮 review 收敛最终实施契约：PTS 固定为 `showinfo` 单方案；记录从创建起 Held；超时覆盖整个 active session；ENTER 中止也必须产人工记录；补齐 nullable weight、负数事件和训练资产边界。**第 4～7 节已原位更新，以 v3 为唯一实施依据。**

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
- 在 `mouse_detect:` 下新增独立的 `pan_roi`（或 `roi`）配置，用实际视频标定秤盘像素坐标。**禁止复用 `weight_roi`**：后者是 LCD 显示屏定位区，不是秤盘。
- 不再先取最大轮廓再校验；应先对所有轮廓应用面积、长宽比和 ROI 约束，再从合法候选中选最佳，避免手套大轮廓遮蔽仍然合法的小鼠候选。

**方案 B：背景减除 / 帧差（中成本，抗光照）**
- 系统本身就有"空秤"基准（会话开始前是 EMPTY）。维护一个空秤背景图（例如会话开始时或定期更新），用 `absdiff` + 自适应阈值做运动/前景检测。手套和手套阴影在"背景图"里不存在，而真正放到秤上的物体才被检出。比固定亮度阈值鲁棒得多。
- 注意：纯帧差对"静止不动的小鼠"会失效，需结合方案 C 或保留灰度兜底。

**方案 C：轻量目标检测（高成本，最稳）**
- 训练/微调一个小模型（YOLO-nano 或 MobileNet-SSD）做"小鼠 vs 手套 vs 空"三分类，或至少做"前景块是否为小鼠"的二分类验证。标注成本用现有录制视频即可（已经有 clip.mp4 产出）。
- 模型只对"灰度阈值检出的大块"做二次确认，不需要逐帧跑，延迟可控。

**方案 D：逻辑层兜底（无论检测器用哪个都该加）**
- 给从 ENTER 开始的整个 active session 加**最大时长**（见 §3.3），覆盖 ENTER 和 WEIGHING；超时只产一条人工记录，随后进入 WAIT_CLEAR，避免卡死或重复拆会话。
- LEAVE 保持规则不要 100% 信任 `mouse_present`：当读数**持续稳定归零**（连续多帧 ≤ leave_max）时，即使 `mouse_present=True` 也应允许进入 LEAVE（当前是"零 + 鼠在 = 无限保持"，见 `detector/__init__.py:165`）。

**我的倾向**：先做 **A（几何约束 + 自适应阈值 + 独立 pan ROI）+ D（active-session 超时 + WAIT_CLEAR + 归零优先）**，这两步能消除绝大多数手套误判和卡死问题，成本最低。模型（C）作为后续增强。背景减除（B）需要稳定的空秤基准，对"用户中途放手套"也敏感，可作为 A 的补充而非替代。

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

**问题**：生产主路径走的是 `read_observation()`（`driver.py:206`），但更上游的 OCR 服务本身无法表达负号：经典七段解码只有 `blank | 0-9 | invalid`，`parse.py` 还会删除 `-`。因此实际负数通常在 OCR 层就变成正数、不可读或过渡态，而不是可靠地产出一个可供主程序过滤的负数值。

**后果**：
- 主程序无法区分"负数显示"与普通不可读/OCR 错误，记录里没有可审计的秤异常原因。
- 若称重过程中短暂显示负数（如小鼠刚跳下时秤的惯性），这些帧会变成空隙或错误正值，影响状态机和 `min_platform_points` 判定。

**建议（端到端）**：
- OCR 服务先检测数字区左侧的符号位，新增 `negative_display` 状态，并保留识别出的绝对值/逐位证据；主程序透传但不让负数进入聚类。
- 会话内负数占比统一定义为 `negative_display / (readable + zero_display + negative_display)`；不可读/bad_roi 不进入分母。
- "全程负数、从未 ENTER"没有会话可归属，应保存为 run 级 `scale_anomaly`，或归入独立的 mouse-presence episode；不伪造重量记录。

### 3.3 "小鼠甩上称还没稳定就拿走"（你提到的关键场景）

**现状**：代码**没有显式的 WEIGHING 超时**。状态机（`detector/__init__.py`）一旦进入 WEIGHING（连续 5 个非零样本，`detector/__init__.py:151-156`），只有"连续 10 帧近零/丢失"才能走到 LEAVE（`detector/__init__.py:161-171`）。

"甩上称→不稳定→拿走"实际如何收尾：
1. 读数一直 > `leave_max(0.30)` → 永远停在 WEIGHING；
2. 下称后近零 10 帧 → LEAVE → ANALYZE；
3. 曲线上找不到 0.8s 的 std≤0.35 窗口 → 进入兜底（`analyzer/__init__.py:212-230`），挑 std 最小窗口猜一个值，**强制 `requires_manual_weight=True`**；
4. `driver.py:588` 再用原始 P90-P10 复核，多半再加人工标记；
5. 人工确认的记录不进上传队列，等人工补值（`recorder.py:93`）。

所以**兜底逻辑是"事后人工"，不是"超时拒绝"**——这一点本身设计是对的（不丢会话，让人工兜底）。但有三个真实失效点：

**失效点 1：整个 active session 都没有最大时长**。WEIGHING 会无限拉长；ENTER 在只收到一次非零、随后持续 unreadable 时也会永久停留。建议从 `session.enter_ms` 起计算 `max_session_seconds=30`，覆盖 ENTER + WEIGHING；也可另设更短的 `enter_confirm_timeout_seconds`，但不能只保护 WEIGHING。

**失效点 2：短会话有两类静默丢弃路径**：
- 进入 ANALYZE 后，`analyze()` 会在原始/过滤后点数不足、全零、非零段过短或无候选窗等多处返回 None；
- 更短的会话会直接 `ENTER → EMPTY(abort_to_empty)` 并 `reset_session()`，根本不会调用 analyzer，SessionDriver 也会立即清 buffer。

正确做法是在状态机层把"已经进入 ENTER 后归零/中止"转入 ANALYZE/manual-session，而不是直接 EMPTY；同时在 SessionDriver 边界统一把所有 `analyze() is None` 转成人工记录。两层缺一不可。

**失效点 3：短会话用中段中位数绕过 std 检查**（`analyzer/__init__.py:159-165`，`duration_ms < window_ms*0.5` 即 <0.4s 时直接取 25%~75% 段中位数）。极短抖动会话可能把抖动读数当有效值。建议：短会话也走 std 判定，std 不达标就转人工，不要因为"点少"就放宽。

**人工记录字段契约**：当没有合理猜测时允许 `weight=None`、`guessed_weight=None`、`photo_frame_index=None`（有最后有效帧则使用它）。这不是只改 analyzer：`types.py`、`recorder.py`、driver 的照片/不稳定复核、registry、records API、统计导出、PC/mobile UI 都必须接受 nullable weight；人工确认前禁止上传、核对和发布。

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

**最终方案（v3，唯一实施方案）：ffmpeg `showinfo` 与 rawvideo 同次解码。**

- `video.py` 的 ffmpeg filter graph 固定加入 `-vf showinfo`，使用每条日志的 `n` 和 `pts_time` 与 stdout 的第 `n` 个 rawvideo 帧配对。**禁止**使用 packet `-show_packets`；本项目也不再保留 `ffprobe -show_frames` 作为并列实现，避免两套映射逻辑分叉。
- 当前命令的 `-loglevel error` 会抑制 `showinfo` 的 info 日志，必须调整到能输出 showinfo 的级别，同时过滤/忽略其他非目标日志。
- stderr 必须由独立线程持续排空并解析到有界队列/按 `n` 索引的缓冲；主线程读取 stdout rawvideo。禁止读完 stdout 后才读 stderr，否则逐帧日志填满 pipe 会让 ffmpeg 阻塞。关闭、异常和 `max_frames` 提前停止时必须终止进程、join 解析线程并释放两条 pipe。
- 每个 rawvideo 帧必须拿到同一 `n` 的 PTS 后才能发给下游。若 PTS 缺失或无法配对，使用统一 15fps 的 `index/fps` 兜底，并在 `Frame`/source 统计中标记 `timestamp_source=fallback_fps`；非单调 PTS 记录异常并钳制为严格递增，不能静默倒退。
- 以首个成功解码帧为时间原点，将非零/负起始 PTS 归一化为 0；同时保留原始 PTS 供诊断。`start_ms`/`end_ms`、clip 边界和状态机均使用归一化 PTS。
- 把 `frame_stride` 的含义从“每 N 帧”迁移为**按时间间隔采样**。配置应改为明确的 `analysis_fps`（默认由现有 `frame_stride=2` 与 15fps 目标迁移为约 7.5fps）或 `sample_interval_ms`；按 PTS 选择达到下一个采样时刻的第一帧。兼容旧配置时只做一次换算并记录弃用告警。
- 截断时长使用**解码源流**的首尾 PTS（包含未被分析采样发出的帧），而不是 `decoded_samples × stride / fps`。真实 PTS 不可用时才使用 15fps 兜底，并把结果降级为 `format_suspect`，不能自动判死。
- 验收必须包含真实 VFR fixture：验证 B-frame、非零/负起始 PTS、缺失/非单调 PTS 回退、stderr 大于 pipe 容量不死锁、按时间采样、start/end window、提前 close，以及首尾 PTS 截断判断。

前端仍强制 MP4/H.264，但它只是输入收敛，不替代后端 PTS。上传后、分析前用 ffprobe 校验容器和 `codec_name=h264`；失败则直接要求重录，不进入分析和序号预留流程。

---

## 5. 修改优先级建议（汇总）

| 优先级 | 修改项 | 模块 | 理由 |
|---|---|---|---|
| **P0** | `showinfo` 单方案逐帧 PTS + 按时间采样 | `video.py`、视频测试 | 根治 VFR 时间窗与 stride 偏斜；防止 stderr 死锁 |
| **P0** | run 级两阶段提交：记录创建即 Held，postflight 后统一释放 | `jobs.py`、`upload_queue.py`、`records_meta.py`、UI | 消除“检查前已经上传”的竞态 |
| **P0** | active-session 30s 超时 + WAIT_CLEAR | `detector/`、`driver.py` | 同时覆盖 ENTER/WEIGHING，避免卡死和重复会话 |
| **P1** | ENTER 中止及所有 analyze None 路径统一产人工记录 | 状态机、driver、types、recorder、registry/API/UI | 不再静默丢短会话，支持 nullable weight |
| **P1** | 独立 pan ROI + max_area + 长宽比 + Otsu | `detect.py`、yaml | 低成本减少手套误判 |
| **P1** | OCR 负号端到端 + run/episode 异常归属 | OCR 服务、reader、driver/record | 让秤负数异常真正可观测、可审计 |
| **P1** | 强制 MP4/H.264，分析前校验 codec | `mobile.js`、上传/API | 输入格式不符时尽早失败，不污染分析状态 |
| **P1** | 数据飞轮被动收集 + 独立训练资产/保留策略 | OCR 服务、driver、recorder | 保存真实模型输入和可复现上下文 |
| **P1** | 高光 mask + 统一 `_to_binary` | `sevenseg_classic.py` 等 | 减少反光误读 |
| **P2** | HSV 自适应 + 主动标注 | locator、UI/API/meta | 覆盖极端光照并补人工标签 |
| **P3** | 检测模型评估 | 新模块 | 几何规则不足时增强；许可证先确认 |

---

## 6. 已确认的决策（v3 最终实施契约）

经过逐项讨论，所有待定问题已拍板。下面是最终决策，作为落代码的依据。

### 决策 1：上称检测 —— 几何约束 + 状态机兜底，模型留作 P3 评估
- **现在做（P1）**：给 `detect.py` 加 `max_area`（小鼠斑块面积上限）+ 长宽比约束；二值化改自适应阈值（Otsu）。所有阈值做成 yaml 可调，部署后用真实数据调。
- **后续评估（P3）**：若几何约束在真实数据不够稳，评估开源模型。候选 DAMM（Detect Any Mouse Model）——为"复杂环境下定位小鼠"设计的预训练检测器；但需先用本项目的称重视频（含手套）验证其对手套的区分能力，不达标则用 Roboflow mouse 数据集 + 自有 clip.mp4 标注微调，导出 ONNX 在边缘 CPU 跑，只对灰度检出的大块做二次确认。
- **理由**：几何约束改动局部、不需训练数据、能消除绝大多数手套误判和卡死；模型工程量大（标注+训练+集成+回归），先用低成本方案验证，不够再上。

> **事实修正（已纳入 v3）**：
> 1. **ROI 不能复用 `weight_roi`。** v1 计划"复用 yaml 已有的 weight_roi 收窄鼠检测范围"是**错误的**——已核实 `configs/scale_refvideo.yaml:18-22` 的 `weight_roi: {x:145,y:780,w:430,h:110}` 是 **LCD 显示屏定位区**（`find_lcd_box` 的 `fixed_roi`，见 `template.py:365-366`、`http_ocr.py:74-80`），而鼠检测当前搜索的是 LCD **上方**区域（`detect.py:47` 的 `y2=lcd.y-10`）。直接复用会让检测器盯着显示屏。**正确做法**：在 `mouse_detect:` 下新增独立的 `mouse_detect.roi` / `pan_roi`（秤盘像素坐标），需用实际视频标定，不可与 `weight_roi` 共用。
> 2. **DAMM 许可证措辞修正。** v1 称"许可兼容、基于 detectron2/Apache-2.0 无传染风险"**不能成立**——已查证 [DAMM 仓库](https://github.com/backprop64/DAMM) 根目录未提供 LICENSE 文件；Detectron2 的 Apache-2.0 只覆盖 Detectron2 本身，**不会自动授权 DAMM 自身代码或模型权重**。正确表述：**DAMM 许可证待作者确认，未确认前仅做内部评估，不集成、不分发。**

### 决策 2：读数稳定 —— 负数端到端支持 + active-session 30s 超时（含清秤等待）+ 短会话统一转人工
- **active-session 超时（2B）**：从首次进入 ENTER 的 `session.enter_ms` 起设 **30s** 最大时长，覆盖 ENTER 和 WEIGHING。可另设更短的 ENTER 确认超时，但不能只给 WEIGHING 计时。超时生成一次人工记录后进入 WAIT_CLEAR。
- **短会话（2C）**：不管多短，只要触发过称重就产出一条 `requires_manual_weight=True` 的记录并导出 clip.mp4。同时短会话也走 std 判定，不再用中段中位数绕过（`analyzer/__init__.py:159-165` 的放宽逻辑移除）。

> **事实修正（已纳入 v3）**：
> 1. **负数不能只加字段——OCR 根本不识别负号（2A）。** v1 计划"给 `RawWeightObservation` 加 `negative` 字段统计占比"是**无效的**——已核实经典七段解码的字符类别只有 `blank | 0-9 | invalid`（`sevenseg_classic.py:27`），没有负号类别；且文本解析 `parse.py:20` 的 `re.sub(r"[^0-9.,]", "", text)` 会**主动删除 `-`**。也就是说当前 OCR **根本无法产出负数观测**，加字段永远收不到数据。**正确做法（端到端，前置依赖）**：
>    - 先在 OCR **服务端**（`services/lcd_ocr/`）增加负号区域检测（秤显示屏左侧/数字前的符号位），新增 `negative_display` 状态到 `schemas.py` 的 status 枚举；
>    - `RawWeightObservation` 透传该状态到主程序；
>    - **再**做占比统计：`negative_display / (readable + zero_display + negative_display)`，不可读/bad_roi 不进分母。"全程负数、从未 ENTER"没有会话可归属，必须落为 run 级 `scale_anomaly` 或独立 mouse-presence episode，不产出伪重量记录。
> 2. **超时必须覆盖整个 active session，并配套清秤等待（2B 阻断）。** v1"超时强制 LEAVE→ANALYZE"不完整：WEIGHING 会卡死，ENTER 在一次非零后持续 unreadable 时也会卡死。**正确做法**：从 `enter_ms` 计时；超时只生成一次人工记录，然后进入 `WAIT_CLEAR`/`TIMED_OUT`。WAIT_CLEAR 至少要求连续 `empty_arm_frames` 个近零观测；`mouse_present=False` 只能作为辅助证据，不能凭单帧检测结果重新武装。
> 3. **短会话同时存在 analyzer None 和 ENTER 中止两类路径（2C 阻断）。** `analyzer.analyze` 在过滤后不足 5 点、全零、非零段过短、无候选窗等会返回 None；更短的会话会 `ENTER → EMPTY(abort_to_empty)` 并在 analyzer 之前被清空。**正确做法**：状态机把“已进入 ENTER 后归零/中止”转入 ANALYZE/manual-session；SessionDriver 再统一把所有 `analyze() is None` 转为人工记录。
> 4. **nullable weight 是端到端契约。** 无合理猜测时使用 `weight=None`、`guessed_weight=None`、`photo_frame_index=None`（若有最后有效帧可使用它），`review_reason` 标明"分析无结果/会话过短"。必须同步修改 `types.py`、`recorder.py`、driver 照片/复核逻辑、registry、records API、统计导出和 PC/mobile UI；人工确认前禁止上传、核对和发布。

### 决策 3：视频帧率 —— `showinfo` 单方案 PTS + run 级两阶段提交
- **治本方向**：改 `video.py` 用同一次 ffmpeg 解码的 `showinfo n/pts_time` 作为逐帧时间戳，替代 `index/fps`；分析采样改为按 PTS 时间间隔。
- **截断方向**：所有 run 记录从创建起 Held；完整视频 postflight 通过后才统一释放，截断可疑则保持隔离待人工确认。
- **附带的统一兜底**：`video.py`（解码）和 `jobs.py`（截断校验）的 fps 兜底值统一为 15.0。
- **理由**：后端是对所有视频的最后一道防线，PTS 对任何设备/来源的 VFR 视频都有效。

> **v3 实施细节（必读）**：
> 1. **PTS 唯一方案是 `showinfo`。** ffmpeg 调整日志级别以输出 showinfo；独立线程持续排空 stderr，按 `n` 缓存 `pts_time`，主线程读 stdout rawvideo 并按同一 `n` 配对。必须有有界缓冲、提前 close/异常清理、线程 join 和配对超时，避免 stderr pipe 死锁。禁止 packet PTS，也不维护第二套 `ffprobe -show_frames` 映射。
> 2. **时间边界与回退。** 首帧归零，保留原始 PTS；缺失 PTS 用 15fps 回退并标记 `timestamp_source`；非单调值记录异常并保证下游时间严格递增。start/end、clip、状态机和分析窗使用归一化 PTS。
> 3. **按时间采样。** 以 `analysis_fps`/`sample_interval_ms` 选择到达下一个采样时刻的第一帧；不再使用 `index % frame_stride`。截断判断使用解码源流未采样前的首尾 PTS。
> 4. **记录必须从创建起 Held，不能事后再隔离。** SessionDriver 保存记录时即以 `AnalysisPending/Held` 写入 upload queue；同步消费者永远不读取 Held。全视频解码、PTS 截断、codec 等 postflight 全部通过后，run 级统一释放为 Pending。任一检查可疑则保持 Held，并标记 job/run `format_suspect`。
> 5. **隔离状态以 job/run postflight 为权威，跨库操作必须幂等。** record.json/records_meta 负责 UI 禁止核对和发布，upload queue 负责禁止同步；释放/拒绝 API 按 run_id 操作，可重复调用。启动时 reconciliation 扫描“job 未通过但 queue 已 Pending”并重新 Held，防止进程崩溃造成状态分裂。
> 6. 人工确认"视频完整"→ run 级释放；确认"确实截断"→ 删除/软删除全部记录并处理已占序号（仅在安全时尾部释放，否则明确保留编号空洞），所有动作写审计日志。

### 决策 4：录制格式 —— 前端强制 MP4/H.264（移除所有回退）+ 后端校验 codec
- 目标：强制单一 MP4/H.264 格式，去掉 webm 兜底。

> **事实修正（已纳入 v3）**：v1 只说"改 `pickMime()`"**不够**。已核实 `mobile.js:380-388` 的 `pickMime()` 在无 MP4 支持时返回 `""`，而 `mobile.js:1162` 的 `new MediaRecorder(canvasStream, opts)` 在 `mimeType` 为空时**仍会创建 recorder**（浏览器默认容器，通常 `video/webm`）；`mobile.js:1164` 还有 `catch` 后无 MIME 重试。**正确做法**：
> - 前端：`pickMime()` 无 H.264 MP4 支持时**抛错**而非返回 `""`；移除 `mobile.js:1162-1164` 的无 MIME 构造和 catch 回退，构造失败直接终止录制并提示用户。
> - 后端：上传完成后、分析和序号预留之前，用 ffprobe 校验 `codec_name=h264` 且容器为 mov/mp4——**`video/mp4` 这个 MIME 本身不严格保证 H.264**（可能是 HEVC/H.265 的 mp4），必须显式校验 codec。校验失败直接要求重录，不创建 run/record。

### 决策 5：数据飞轮 —— 两阶段，全量收集，保存真实 OCR 输入，训练资产独立隔离
背景：人工修正流程已存在（`requires_manual_weight` → clip.mp4 → 回放确认 → 补值），但流程产出数据**不足以喂训练**——单帧 OCR 原始观测只在内存（`driver.py:567` clear）、LCD 裁剪图不存、检测框 bbox 不落盘、正常记录无修正入口、无误检标签。好消息是 `tools/explain_session_replay.py` 已证明这些数据技术上都能产出，只需接到生产流程。

**阶段一（P1，纯被动收集，零打扰）**：每次称重自动额外保存训练原材料；另以低频 EMPTY 采样或会话 pre/post-roll 补充真实背景负样本。
**阶段二（P2，主动标注，小幅前端改动）**：放开修正入口 + 误检标签。
**收集范围**：全量收集（所有会话）+ 受控背景采样，带保留策略，加 env/yaml 开关默认开。

> **事实修正（已纳入 v3）**：
> 1. **保存的必须是 OCR 实际输入，不是原图轴对齐裁剪（阻断）。** v1 说"复用 `explain_session_replay._crop_quad()` 保存 LCD crop"是**错误的**——已核实 `_crop_quad()` 只是对原图 LCD bbox 做轴对齐裁剪；而生产 OCR 真正使用的是规范化 screen、digit ROI、raw/CLAHE chosen strip 和 slot patches。**正确做法**：收集开启时，OCR 服务响应返回 JPEG/PNG 编码的 `normalized_screen`、独立 `sign_patch`、实际 `chosen_strip` 和 digit slots；主程序落盘。负号位在数字区左侧，不能假设 chosen digit strip 包含符号信息。
> 2. **跨容器传输协议固定为响应内二进制编码，不返回服务本地路径。** lcd-ocr 与主程序是独立容器，服务文件路径默认不可见。P1 使用可选 base64 字段（仅在 collection flag 开启时返回，设置尺寸/总字节上限）；若后续吞吐不足，再升级为 multipart 或显式共享卷协议，不能返回未声明可访问性的路径。
> 3. **建立逐帧 manifest，绑定全部上下文（P1）。** 每个会话产出一个逐帧 manifest（jsonl 或 parquet），每行包含：`frame_index, pts_ms, raw_pts_ms, timestamp_source, raw_observation(digits/conf/status), screen_quad, mouse_bbox, normalized_screen_path, sign_patch_path, strip_crop_path, digit_slot_paths, model_version, config_hash, schema_version`。这样修正/标注事件才能精确关联到具体帧和具体模型输入。
> 4. **补充 detector 真负样本。** "所有会话"不包含真正的 EMPTY 背景，无法覆盖 mouse/glove/empty 分类的负样本。以低频（可配置）保存 EMPTY 帧，或保存每个会话固定数量的 pre/post-roll 帧；写入同一 manifest，并用 `collection_scope=empty_sample|pre_roll|session|post_roll` 标记来源。
> 5. **P1-c 不是 P1-e 的硬依赖。** 飞轮可以先收集 normalized screen/sign patch，为负号检测积累数据；P1-c 完成后只需让 manifest schema 记录 `negative_display`。两项共享 schema 设计即可并行，不能以"strip 含负号"为由阻塞收集。
> 6. **训练资产必须独立子目录，保留策略只能清训练资产（P1）。** 训练资产统一放 `mouse_NNN/training_assets/` 子目录；保留策略（按天数/限额）**只扫描该子目录**，绝不触碰 record.json/photo.jpg/clip.mp4。已人工标注样本单独长期保留，但仍受独立 hard quota 约束；超限时告警并停止新增 pinned 资产，不能无限排除清理直到磁盘写满。

---

### 顺带保留的 OCR 光影改进（未单独讨论，沿用 §2.3 建议）
- P1：二值化前 mask 反光高光斑（减少 `0.00→11.11`）；统一 4 处 `_to_binary` 实现。
- P2：定位层 HSV 范围自适应。
- P3：CLAHE clipLimit 分场景调，raw/CLAHE 分歧降级为软证据。

---

## 7. 落地优先级（v3 最终版）

> 总体顺序：**PTS → 两阶段 Held 隔离 → active-session 超时/短会话 → 独立 pan ROI → 负号端到端与数据飞轮 → OCR 光影 → P2/P3**。
> "依赖"列标注前置项；有依赖的项必须在其前置完成后才能开始。

| 批次 | 修改项 | 对应决策 | 模块 | 依赖 |
|---|---|---|---|---|
| **P0-a** | `showinfo` 单方案逐帧 PTS：可见 info 日志 + stderr 并发排空/按 n 配对 + 首帧归零 + fallback 标记 + 按时间采样；截断使用未采样源流首尾 PTS | 决策 3 | `video.py`、`types.py`、`test_video_ffmpeg_backend.py` | 无；**验收须含真实 VFR、B-frame 与大 stderr fixture** |
| **P0-b** | run 级两阶段提交：记录创建即 `AnalysisPending/Held`；postflight 通过后幂等释放；可疑保持隔离；启动 reconciliation；人工释放/拒绝及序号处理 | 决策 3 | `driver.py`、`jobs.py`、`upload_queue.py`、`records_meta.py`、UI/API | P0-a（截断使用真实 PTS） |
| **P0-c** | 从 `enter_ms` 起的 active-session 30s 超时（覆盖 ENTER+WEIGHING）+ `WAIT_CLEAR/TIMED_OUT`；连续近零才重新武装 | 决策 2 | `detector/__init__.py`、`driver.py` | 无 |
| **P1-a** | 短会话统一转人工：ENTER 中止改走 ANALYZE/manual-session；SessionDriver 兜住全部 analyze None；端到端支持 nullable weight | 决策 2 | `detector/`、`driver.py`、`analyzer/`、`types.py`、`recorder.py`、registry/API/统计/UI | P0-c（复用 active-session/WAIT_CLEAR 语义） |
| **P1-b** | 新增独立 `mouse_detect.roi`/`pan_roi`（秤盘像素，实际视频标定，不复用 `weight_roi`）+ `max_area` + 长宽比 + 自适应阈值(Otsu) | 决策 1 | `detect.py`、yaml | 无 |
| **P1-c** | 负号端到端：保存/检测 sign patch + `negative_display` → 透传 → 明确占比分母；无会话负数落 run/episode anomaly | 决策 2 | `services/lcd_ocr/`、`observations.py`、`http_ocr.py`、driver/record | 与 P1-e 共享 schema，可并行 |
| **P1-d** | 前端强制 MP4/H.264；上传后、分析/序号预留前 ffprobe 校验 container+codec，失败直接重录 | 决策 4 | `mobile.js`、`app.py`/jobs preflight | 无 |
| **P1-e** | 数据飞轮 P1：base64 返回 normalized screen/sign patch/chosen strip/slots + 逐帧 manifest + EMPTY/pre/post-roll + 独立目录/soft+hard quota | 决策 5 | `services/lcd_ocr/`、`driver.py`、`recorder.py` | 与 P1-c 共享 schema，可先行或并行 |
| **P1-f** | OCR 光影：二值化前 mask 反光高光斑；统一 4 处 `_to_binary` | §2.3 | `sevenseg_classic.py` 等 | 无 |
| **P2-a** | 定位层 HSV 范围自适应 | §2.3 | `locator.py` | 无 |
| **P2-b** | 数据飞轮 P2 主动标注：放开正常记录修正入口（留 `original_weight`）+ `detection_label` + 事件帧绑定 | 决策 5 | `pc.js`、`app.py`、`records_meta.py` | P1-e |
| **P3** | 评估/集成小鼠检测模型（DAMM 候选，**许可证未确认前仅内部评估**） | 决策 1 | 新模块 | P1-b 数据沉淀后 |

---

## 8. 待确认项

无。第 6/7 节已收敛为 v3 唯一实施依据；实现中若需要改变状态模型、持久化权威源或跨容器资产协议，必须先回写本文档再动代码。
