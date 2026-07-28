# 离线天平—手机时钟校准测试页（MVP）

## 1. 目的与边界

本 MVP 为**不联网的天平**建立一次称量批次内的时间换算关系。它让操作者在手机网页上记录两个校时锚点，称量结束后从 OTG U 盘选择天平导出的 CSV，人工确认两个锚点所对应的 CSV 行，并计算：

```text
手机/视频时间 = 偏移量 + 天平内部时钟 × 时钟速率
```

校准完成后，后端即可把天平 CSV 中每一条稳定读数投影到手机视频的时间线。未来以此将天平读数作为体重真值；视频只作为录入证据与 OCR 训练素材。

本页是一个隔离的试验工具，第一版**不**：

- 修改 `POST /api/jobs`、实时称重、现有记录或体重报告；
- 自动从 LCD 或视频识别重量；
- 自动猜测 CSV 中哪一行是锚点；
- 将校准结果自动用于小鼠记录。

因此，MVP 可以安全地在现场验证华为 Android + OTG + 浏览器上传、CSV 解析、时钟偏差和漂移计算。确认流程可用后再接入正式录制和配对。

## 2. 为什么需要两个锚点

天平本身不联网，其 CSV 内的日期时间来自天平内部时钟；手机录制和网页点击事件来自手机系统时钟。这两套时钟可能有：

1. 固定偏移（例如天平慢 83 秒）；
2. 随时间累积的走时误差（drift）。

单个锚点只能修正偏移；在称量开始和结束各记录一个锚点，才可同时估计偏移和漂移。锚点可以使用**任意稳定的物体**，不要求标准砝码；标准砝码只是人工复核更方便。

一个锚点的推荐现场动作：

1. 放上一个不易与小鼠读数混淆的物体，等待天平稳定；
2. 让物体继续放置至少 10 秒，确保 U 盘导出中产生多条稳定读数；
3. 在网页点击“记录开始锚点”或“记录结束锚点”；
4. 可选填写当时肉眼看到的重量，仅用于上传 CSV 后快速筛选候选行，不作为体重真值。

同一物体或两个不同物体都可以。不要使用接近小鼠体重的物体；如用水瓶、手机加充电宝等较有辨识度的物体，后续确认 CSV 行更容易。

> 注意：网页按钮时间和天平写入 CSV 的时间不必完全相同。天平通常按自己的节奏每数秒写一次稳定读数。MVP 通过“人工选择对应 CSV 行”消除这个不确定性，而不是假装可以凭一个点击自动找到正确行。

## 3. 已有系统的接入位置

现有移动端是原生 JavaScript SPA：

- 页面入口：`/mobile`，后端的 `GET /mobile/{path:path}` 已会返回 SPA；
- 前端：`ui/static/mobile.html`、`ui/static/mobile.js`、`ui/static/mobile.css`；
- 后端：`ui/app.py`（FastAPI）；
- 写接口鉴权：沿用 `require_api_token` 或 `require_token_or_operator`；
- 录制起始时间已在 `mobile.js` 中以 `startedAt = Date.now()` 保存，但当前没有写入 `capture_meta`。

MVP 新增路由 `/mobile/scale-sync`，并从移动端首页增加“天平校时（测试）”入口。不要新增前端框架或新服务。

## 4. 页面流程

### 4.1 页面状态

页面顶部始终显示手机当前时间，精确到毫秒：

```text
手机时间（Asia/Shanghai）  2026-07-28 14:05:03.217
```

时间由 `Date.now()` 驱动，每 100 ms 刷新。页面还应显示浏览器报告的时区（`Intl.DateTimeFormat().resolvedOptions().timeZone`）和 UTC 偏移，便于发现手机时区设置错误。

进入页面后，操作者点击“新建本次校时”，服务器创建一个 `sync_session_id`。页面按以下顺序展示进度：

```text
新建会话 → 记录开始锚点 → 记录结束锚点 → 上传 CSV → 选择两条 CSV 行 → 计算结果
```

刷新页面后，使用 URL 中的 `?session_id=<uuid>` 或 `sessionStorage` 恢复未完成会话；不能丢失已记录锚点。

### 4.2 记录锚点

“记录开始锚点”与“记录结束锚点”均为明确的二次确认按钮，避免误触：

```text
记录开始锚点
当前手机时间：2026-07-28 14:05:03.217
可选：观察到的稳定重量（g） [              ]
[确认记录]
```

确认后，前端立即向服务器提交下列字段：

```json
{
  "kind": "start",
  "client_epoch_ms": 1785218703217,
  "client_perf_ms": 123456.789,
  "client_timezone": "Asia/Shanghai",
  "client_utc_offset_minutes": -480,
  "observed_weight_g": null,
  "note": ""
}
```

`client_epoch_ms` 是匹配视频的主时间；`client_perf_ms` 仅用于审计手机系统时钟在页面打开期间是否跳变。服务器额外写入自己的 `received_at_utc`，用于诊断网络延迟，**不能**替代手机时间。

若还未记录开始锚点，结束按钮禁用。已记录的锚点显示不可伪造的时间与“删除并重记”按钮；删除必须显式确认并写审计事件。

### 4.3 从 U 盘上传 CSV

两个锚点均已记录后，显示：

```html
<input type="file" accept=".csv,text/csv" />
```

在华为 Android 上，用户点击该控件会进入系统文件选择器；已通过 OTG 识别的 U 盘应显示为可选存储位置。网页不主动枚举或读取 U 盘，也不使用 WebUSB。

上传完成后显示：文件名、文件 SHA-256、字节数、解析行数、CSV 首尾时间、识别的单位，以及任何解析警告。原始文件必须只读保存，以备审计。

### 4.4 选择 CSV 对应行

MVP 以人工确认确保可靠。后端返回可搜索/筛选的读数列表，前端至少支持按重量、日期时间和原始行号筛选。对每一个锚点，操作者点击其真实对应的 CSV 行的“设为开始锚点”或“设为结束锚点”。

候选行至少显示：

| 原始行号 | 天平日期时间 | 重量 | 单位 | 原始内容 |
| --- | --- | --- | --- | --- |
| 1 | 2026-05-20 12:49:18 | 500.40 | g | `0,26-05-20,12:49:18,      0, 500.40,g` |

如果填写了 `observed_weight_g`，前端可默认按 ±0.02 g 筛选，但操作者仍必须确认具体行。保持物体 10 秒往往会产生多条相同读数；选择其中的中间一条即可，并将“按钮点击与行时间相差秒数”显示出来供复核。

### 4.5 计算与显示

两个锚点各自绑定一条 CSV 行后，点击“计算校时”。结果必须清楚显示：

- 两个锚点的手机时间与天平 CSV 时间；
- 开始时刻的偏移（天平相对手机快/慢多少秒）；
- 时钟速率与漂移（ppm）；
- 两锚点之间的手机时长、天平时长；
- 映射后的读数预览（至少 20 行）；
- 绿色/黄色/红色状态及解释。

示例：

```text
开始：天平比手机慢 83.4 s
漂移：天平每小时再慢 0.08 s（-22 ppm）
结果：可用于本会话的时间匹配
```

结果页仅提供“下载校时摘要 JSON”和“开始新的校时”。在 MVP 中，不应写入任何小鼠体重。

## 5. API 契约

所有写接口使用现有 `require_api_token`。读取单一会话使用 `require_token_or_operator`；若项目尚未有细粒度角色，MVP 可全部采用 `require_api_token`，避免匿名用户浏览称量证据。

### 5.1 新建会话

```http
POST /api/scale-sync/sessions
Content-Type: application/json

{
  "project_id": "default",
  "cage_id": "C57-023",
  "scale_device_id": "scale-01",
  "scale_timezone": "Asia/Shanghai"
}
```

`cage_id` 和 `scale_device_id` 在 MVP 可选，但字段应保留；没有设备编号时保存为空，页面显示警告。

返回：

```json
{
  "session_id": "4c7...",
  "state": "created",
  "created_at_utc": "2026-07-28T06:05:00.000Z",
  "scale_timezone": "Asia/Shanghai",
  "anchors": [],
  "imports": []
}
```

### 5.2 写入或替换锚点

```http
PUT /api/scale-sync/sessions/{session_id}/anchors/start
Content-Type: application/json
```

请求体使用 §4.2 的 JSON，`kind` 由路径决定，服务端忽略请求体中的冲突值。`/anchors/end` 同理。响应返回不可变的 `anchor_id`、记录的客户端与服务器时间。

### 5.3 上传 CSV

```http
POST /api/scale-sync/sessions/{session_id}/imports
Content-Type: multipart/form-data

file=<CSV>
```

成功返回 `import_id`、SHA-256、行数、时间范围和解析警告。该接口只接受 CSV，最大文件大小建议 5 MB；拒绝空文件、没有有效数据行的文件或日期/重量无法解析的文件。不得删除会话已有导入；重新上传创建新的 `import_id`。

### 5.4 查询读数并绑定锚点

```http
GET /api/scale-sync/sessions/{session_id}/imports/{import_id}/readings?query=500.4

PUT /api/scale-sync/sessions/{session_id}/anchors/start/match
Content-Type: application/json

{
  "import_id": "...",
  "source_line_no": 7
}
```

服务器保存匹配时必须同时保存该行的完整不可变副本（原始行、天平时间、重量、单位），不要只保存行号。两锚点必须来自同一 `import_id`；否则 MVP 拒绝计算，避免跨日/跨文件误配。

### 5.5 计算和读取会话

```http
POST /api/scale-sync/sessions/{session_id}/calculate
GET  /api/scale-sync/sessions/{session_id}
```

计算成功示例：

```json
{
  "session_id": "4c7...",
  "state": "calculated",
  "model": {
    "kind": "two_point_affine_v1",
    "scale_timezone": "Asia/Shanghai",
    "phone_time_equals_scale_time": false,
    "rate": 0.999978,
    "offset_ms": 3912345.6,
    "drift_ppm": -22.0,
    "start_offset_ms": 83400.0,
    "valid_for_scale_from_ms": 1785218700000,
    "valid_for_scale_to_ms": 1785225900000
  },
  "warnings": []
}
```

`offset_ms` 是内部数值，UI 应优先显示人类可读的开始偏移。不要把浮点的绝对 epoch 偏移作为用户界面主信息。

## 6. 数据与 CSV 解析

### 6.1 原始文件保留

在 `output/scale_sync/<session_id>/<import_id>/source.csv` 保存上传的原始字节，上传后设置只读或在应用逻辑上禁止更新。数据库至少保存 SHA-256、原始文件名、MIME、字节数、上传时间和解析器版本。

项目已保留一个不可修改的解析基准：

```text
tests/fixtures/scale_usb/260520.CSV
SHA-256 0103f34c6ddfcd3eb640202d90861f09d81c9ee8c7e99861407da5833ca6f58a
```

这份文件有 10 条 2026-05-20 的 500 g 砝码稳定读数，可用于测试 CSV 解析和校时流程；它不是小鼠体重训练数据。

### 6.2 当前天平导出格式

基准文件无表头，每一行形如：

```csv
0,26-05-20,12:49:18,      0, 500.40,g
```

解析器应：

1. 使用 Python `csv` 模块，正确处理 CR/CRLF/LF；
2. 先尝试 `utf-8-sig`、再尝试 `gb18030`、最后 `latin-1`；所有源字节仍原样保存；
3. 去除每个字段的外部空白；
4. 将第 2、3 列按 `%y-%m-%d %H:%M:%S` 解析，并按会话的 `scale_timezone` 解释；两位年份使用 `2000 + yy`；
5. 将第 5 列解析为十进制克数，第 6 列保留单位；
6. 将第 1、4 列作为未解释的原始字段保留，不能假设其业务含义；
7. 保留所有重复读数和原始物理行号，不能去重或只取“稳定窗口”中的一行。

当前已知导出行应被命名为“天平导出的稳定读数”，而非根据第 4 列推断状态码。未来拿到厂家协议后再定义该列语义。

### 6.3 建议的 SQLite 表

新建独立的 `output/scale_sync.db`，不要侵入现有 `jobs.db` 或 `records_meta.db`。

```text
scale_sync_sessions
  session_id PK, project_id, cage_id, scale_device_id, scale_timezone,
  created_at_utc, state, calculated_model_json, warnings_json

scale_sync_anchors
  anchor_id PK, session_id FK, kind (start/end), client_epoch_ms,
  client_perf_ms, client_timezone, client_utc_offset_minutes,
  server_received_at_utc, observed_weight_g, note,
  import_id nullable, source_line_no nullable, matched_row_json nullable

scale_sync_imports
  import_id PK, session_id FK, original_filename, sha256, byte_count,
  stored_path, parser_version, uploaded_at_utc, summary_json

scale_sync_readings
  import_id FK, source_line_no, raw_line, scale_epoch_ms, weight_g, unit,
  raw_sequence, raw_status, PRIMARY KEY(import_id, source_line_no)

scale_sync_audit
  id PK, session_id FK, event_type, actor, at_utc, payload_json
```

SQLite 的写入使用现有项目的锁 + WAL 模式。任何重新匹配或替换锚点都追加审计记录；计算结果可以重算，但历史匹配不得静默覆盖。

## 7. 时间换算和校验规则

将两个天平时间戳转换为 epoch milliseconds `S1`, `S2`，两个手机锚点转换为 `P1`, `P2`。仅当 `S2 > S1` 且 `P2 > P1` 时计算：

```text
rate       = (P2 - P1) / (S2 - S1)
offset_ms  = P1 - rate × S1
phone_ms(S) = rate × S + offset_ms
drift_ppm  = (rate - 1) × 1,000,000
```

建议校验：

| 条件 | 行为 |
| --- | --- |
| 未记录两个锚点 / 未绑定行 | 拒绝计算 |
| 两锚点不是同一个导入文件 | 拒绝计算 |
| 时间倒序或相同 | 拒绝计算 |
| 锚点间隔少于 60 秒 | 黄色警告；允许测试但不建议正式使用 |
| `abs(drift_ppm) > 5,000` | 红色警告，要求检查是否选错行或设备时钟/时区 |
| 按钮时间与所选 CSV 行相差超过 5 分钟 | 黄色警告，仍允许人工确认 |
| 手机时区不是会话指定的 `Asia/Shanghai` | 红色警告，默认禁止计算 |

模型只在两个天平锚点之间有效。MVP 不允许把映射外推到锚点之前或之后；日后如确需外推，应在记录上显式标为低置信度。

两个点的拟合残差必然为零，不能把它当作准确性证明。后续正式版本可记录第三个“检查锚点”，并显示该点的真实残差。

## 8. 与正式录制/匹配的后续衔接（不属于 MVP）

MVP 验收后按下列顺序演进：

1. 在 `mobile.js` 的 `buildCaptureMeta()` 中写入 `recording_started_at_utc`、`recording_started_epoch_ms`、手机时区和 UTC 偏移；其值来自当前已有的 `startedAt = Date.now()`，不要依赖 MP4 的 `creation_time` 标签。
2. 每只鼠完成称量时写入业务事件：`event_id`、鼠号/扫码号、`client_epoch_ms`、相对视频毫秒、`video_job_id`。先由操作员“确认称量”触发；实时算法可在以后作为候选事件来源。
3. 上传 CSV 后，对每一业务事件使用本会话的 `phone_ms(S)` 映射，选择时间最近且在阈值内的读数；将原始 CSV 行、校时会话 ID、时间差和匹配版本一起保存。
4. 匹配失败、多个候选同样近、或超出阈值时进入人工确认，绝不使用 OCR 补写真值。
5. 将“视频 LCD 裁剪 + 已匹配的 CSV 重量”作为有来源的 OCR 训练样本，并按时间/视频/笼位划分训练集与测试集，避免相邻帧泄漏。

最终权威关系应保持为：

```text
天平 CSV（重量真值）
        +
网页事件 / 视频时间轴（对应关系）
        ↓
可审计的称量记录与 OCR 训练标签
        ↓
OCR 预测（只能核验或辅助，不能覆盖 CSV 真值）
```

## 9. 验收清单

### 自动化测试

- 用 `tests/fixtures/scale_usb/260520.CSV` 解析出恰好 10 条读数；
- 第一条为 `2026-05-20 12:49:18`、`500.40 g`，最后一条为 `2026-05-20 12:55:40`、`500.36 g`；
- 原文件 SHA-256 与 §6.1 一致，导入后原始字节不被转码或重写；
- 合成双锚点数据的 `rate`、`offset_ms`、`drift_ppm` 与预期一致；
- 天平时间倒序、跨导入匹配、坏 CSV、超大 CSV、无权限写入均被拒绝；
- 已计算会话的读数预览时间单调递增，且在有效窗口外明确标记为不可用。

### 现场手工测试

1. 用一台华为 Android 手机和 OTG 转接头连接装有 `WEIGHT/260520.CSV` 的 U 盘；
2. 在 HTTPS 下打开 `/mobile/scale-sync`，确认手机时间持续刷新；
3. 创建会话、记录两锚点、从系统文件选择器选择 U 盘 CSV；
4. 确认读数列表、原始行、解析时间和 SHA-256 正确；
5. 为两个锚点选择正确行后，检查偏移与漂移结果符合人为预期；
6. 断网/刷新后确认未上传的原文件不会丢失已创建的会话，已上传会话可恢复；
7. 使用一份故意错误的 CSV 验证页面不产生校时结果。

## 10. 实现交付物

实现者应提交以下最小集合：

- `ui/static/mobile.js`：新增 `/mobile/scale-sync` 视图、API 调用和会话恢复；
- `ui/static/mobile.css`：移动端校时步骤、读数表、状态色样式；
- `ui/app.py` 或独立且被 `app.include_router()` 注册的路由模块：§5 API；
- 独立存储/解析模块，例如 `mousevision/scale_sync.py`；
- `tests/test_scale_sync.py`：§9 自动化测试；
- 本文档更新为实际路由、存储路径和任何与契约不同之处。

不要在这个 MVP 中修改 OCR、录像编解码、实时 WebSocket 协议或既有小鼠记录表。

## 11. 实现状态（2026-07-28）

下列为实际落地的路由、存储路径与和 §5/§6 契约一致或细化之处，供现场排障与后续演进参考。

### 实际交付文件

| 文件 | 作用 |
| --- | --- |
| `mousevision/scale_sync.py` | 独立 SQLite 存储 + CSV 解析 + 两点时钟模型（核心，无 UI 依赖） |
| `ui/scale_sync_api.py` | §5 REST 路由模块（`APIRouter(prefix="/api/scale-sync")`），被 `app.include_router` 注册 |
| `ui/app.py` | 仅追加两行接入：`scale_sync_api.configure(str(DEFAULT_OUTPUT))` + `app.include_router(scale_sync_api.router)` |
| `ui/static/mobile.js` | 新增 `/scale-sync` 视图 + 首页"天平校时（测试）"入口，复用既有 `h()/appbar/go/route` |
| `ui/static/mobile.css` | 追加校时页样式（进度条、状态色、读数表、弹层） |
| `tests/test_scale_sync.py` | §9 自动化测试，20 条全绿 |

### 存储

- 数据库：`output/scale_sync.db`（独立，WAL + `timeout=30, isolation_level=None`，沿用 `ui/audit.py` 约定），不碰 `jobs.db`/`records_meta.db`。
- 原始 CSV：`output/scale_sync/<session_id>/<import_id>/source.csv`，字节级只读保存。
- 解析器版本：`scale_csv_v1`。

### 路由（全部 `require_api_token`）

与 §5 契约一致，新增 `DELETE /sessions/{sid}/anchors/{kind}`（删除锚点、写审计，对应 §4.2"删除并重记"）。`GET /sessions/{sid}` 返回完整状态，并附加 `readings_preview`（映射后 ≥20 行，每行含 `phone_epoch_ms` 与 `within_window` 标记，对应 §7"模型只在两锚点之间有效"）。

### 与契约一致、需现场知晓的细化

1. 基准 CSV 实际**有一条 GBK 编码表头行**（`序号,日期,时间,产品编号,重量,单位`）。解析器按 §6.2 的"无法解析则跳过并记 warning"处理，产出恰好 10 条读数；表头行计入解析警告。因此读数的 `source_line_no` 从 2 开始（物理行号），绑定锚点时需对应实际行号。
2. CSV 编码探测顺序：`utf-8-sig` → `gb18030` → `latin-1`（基准文件以 `gb18030` 解码成功）。
3. 上传上限 5 MB（`MAX_IMPORT_BYTES`），超限返回 413。
4. 替换锚点会清除其已绑定的 CSV 行匹配并写审计（§6.3"历史匹配不得静默覆盖"），操作者需重新绑定。
5. 两点模型 `rate/offset_ms/drift_ppm` 严格按 §7 公式；校验矩阵中"手机时区 ≠ 会话时区"判为红色，与 §7 一致。

### 部署

经 `docs/DEPLOYMENT.md` 的既有流程：推 `main` → 在 vm-user 跑 `bash ~/mousevision/update.sh`（git pull → 重建镜像 → 重启）。校时页对外入口：`https://weight.pingoodmice.top:16206/mobile/scale-sync`。

