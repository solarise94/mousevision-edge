# 主账号 / 子工作区隔离升级

| 项目 | 内容 |
|------|------|
| 状态 | **单 agent 连续执行任务书 v1.0**（尚未改业务代码） |
| 日期 | 2026-09-03 |
| 范围 | **云版**（内测 APK + 边缘服务器）。本地版（`local` flavor）仍是单机数据，不引入租户 |
| 关联 | [PC_ADMIN_PLATFORM.md](./PC_ADMIN_PLATFORM.md)、[MOBILE_WEB_APP_DESIGN.md](./MOBILE_WEB_APP_DESIGN.md)、[DEPLOYMENT.md](./DEPLOYMENT.md) |
| 当前基线 | `main@5129d59`；执行前必须重新核对 HEAD、工作区、测试与线上版本，不能把本行当成当前事实 |

---

## 1. 审查结论

**不能只给 `users` 表加「主账号 / 子账号」字段。** 当前系统的数据归属边界是整台部署：一个 `output/`、一套 SQLite、一个共享 API token、一组进程内单例。账号只决定「能不能登录 / 能不能写」，不决定「看到哪一份数据」。

正确边界是 **工作区（tenant）**：

```text
主账号（account / parent_owner）
  ├─ 子工作区 A ─ 用户成员 / 设备凭证 / 箱子 / 记录 / 文件 / 设置
  ├─ 子工作区 B ─ …
  └─ 子工作区 C ─ …
```

- 子账号只访问自己的工作区。
- 主账号默认可汇总、切换查看已绑定工作区；**默认只读**。代管子工作区写操作必须显式确认并审计。
- `project_id` 继续只是任务标签，**不承担隔离**（见设计文档 §3.5.3）。隔离键是 `tenant_id`。

本轮审查基于仓库 HEAD（`5129d59` 附近），未改代码，也未核对线上容器内数据。现有鉴权 / 上报 / 箱子测试仍有效，但仓库里 **没有租户隔离测试**。

---

## 2. 现状（代码事实，不是推断）

### 2.1 存储是全局单根

[`ui/app.py`](../ui/app.py) 在导入时绑定唯一输出根和全局 store：

- `DEFAULT_OUTPUT` ← `MOUSEVISION_OUTPUT_DIR` 或仓库 `output/`
- `users.db` / `boxes.db` / `jobs.db` / `records_meta.db` / `upload_queue.db` / `audit.db` / `settings.json` / `mice_registry.json` / `run_*` / `job_uploads/` 全部挂在同一根下
- `BoxRegistry`、`MouseRegistry`、`JobStore`、`RecordsMetaStore`、`UploadQueue`、`PlaybackEngine`、`AnalysisJobManager` 都是 **进程级单例**

`project_id` 明确不隔离；`cage_id` 要求部署内全局唯一（[`ui/boxes.py`](../ui/boxes.py) 模块注释、[MOBILE_WEB_APP_DESIGN.md §3.5.3](./MOBILE_WEB_APP_DESIGN.md)）。

### 2.2 身份里没有工作区

[`ui/users.py`](../ui/users.py) 角色只有 `admin / operator / viewer`，写在 `users.role` 上。会话只有 `token + user_id + expires_at`。

[`ui/auth.py`](../ui/auth.py)：

- PC：Cookie `mv_session`
- 手机 / 机器：环境变量 `MOUSEVISION_API_TOKEN` 的全站共享明文比对
- token **永不**映射为 admin 会话（这点保留）
- 未配置 token 时，`require_api_token` 和 `require_token_or_operator` 进入 **open mode**（匿名可写）

### 2.3 读接口默认匿名

写接口大多有 token 或会话；下列读接口在 HEAD 上 **无登录依赖**（匿名 200）：

| 路径 | 泄露 |
|------|------|
| `GET /api/boxes`、`/recent`、`/{cage_id}`、`/{cage_id}/records` | 全部箱子与本箱记录 |
| `GET /api/boxes/{cage_id}/qr.svg` | 箱号二维码 |
| `GET /api/records/{id}`、`/photo`、`/clip` | 猜 UUID 即可取记录 / 照片 / 视频 |
| `GET /api/jobs`、`/{id}`、`/{id}/report` | 任务与报告 |
| `GET /api/runs`、`/api/mice`、`/mice/{i}/photo` | 批次与鼠只 |
| `GET /api/upload-queue`、`/api/status`、`/api/stream`、`/api/photo` | 队列与实时画面 |
| `GET /api/health` | 含 `active_jobs`（可保留存活探测，但不要带业务明细） |

PC 管理列表（`/api/records`、`/api/overview` 等）已经要会话；手机工作流没有。

### 2.4 云版设备没有独立身份

[`android/app/build.gradle.kts`](../android/app/build.gradle.kts)：`cloud` flavor 把 `MICE_SYNC_TOKEN` 写进 `assets/www/config.js` 的 `MV_CONFIG.token`。所有云版 APK 共用同一写令牌。

服务端还把同一令牌注入 `/mobile`、`/legacy` 的 HTML meta（[`ui/app.py`](../ui/app.py) `_inject_api_token`）。PC `/` 和 `/pc` 不注入——这点正确，升级后手机页也不应再内嵌写令牌。

`local` flavor 的同步令牌为空；`MOUSEVISION_SHARE_TOKEN` 只给本地版「共享数据」通道，写入 `output/shared/`，与实验室 registry 隔离（[`ui/share_api.py`](../ui/share_api.py)）。**租户升级不要把 share 通道并进工作区数据。**

### 2.5 手机队列与重置是全局语义

- 上报队列键固定 `mv.reportOutbox.v1`（[`ui/static/report-client.js`](../ui/static/report-client.js)）。同一浏览器 / WebView 换账号会继续刷旧队列。
- `POST /api/reset` 只接受 admin 会话，但删除的是整个 `DEFAULT_OUTPUT` 下的 `run_*`（[`ui/app.py`](../ui/app.py)）。若把「子工作区管理员」映射成现有 `admin`，会清空所有账号数据。

### 2.6 即便物理分目录，当前进程内对象仍会串租户

分目录只能隔离磁盘。下列对象今天是 **一个进程一份**，升级时必须改成「每个请求的 TenantContext 取自己的 store」，否则目录拆开了内存里还是串的：

| 对象 | 位置 | 风险 |
|------|------|------|
| `PlaybackEngine` | `ui/app.py` `engine` | `/api/status` `/api/stream` 看到别人的实时画面 |
| realtime session dict | `ui/realtime_api.py` | 猜 `session_id` + 有效令牌即可操作他人会话 |
| `AnalysisJobManager` | 绑定单一 `output_root` | job 文件写到错误租户 |
| `report_api` / `scale_sync_api` / `capture_api` 模块全局 | `configure()` 一次 | 上报、秤同步、BLE 捕获落到同一根 |
| `MouseRegistry.active_run` | 全局 JSON | 切换活跃批次跨租户 |

单 worker 队列可以共享线程，但 **每个 job 必须携带不可伪造的 `tenant_id`，写出路径只能来自服务端 TenantContext**。

---

## 3. 目标与非目标

### 目标

1. 两个子工作区使用相同 `cage_id`、相同客户端 `record_id` 互不影响。
2. 未授权主体不能靠猜 URL 读到记录 / 照片 / 视频 / 箱子 / 任务。
3. 主账号只能访问已绑定子工作区；默认只读。
4. 子工作区重置、设置、编号器不影响其他工作区。
5. 手机换账号不会用新凭证把旧草稿传到错误工作区。
6. 箱内序号在 `(tenant_id, cage_id)` 上原子分配、不重号。
7. 旧云版 APK 的共享令牌在过渡期只映射到 `legacy-default` 工作区；队列排空后再撤销。

### 非目标（v1 不做）

- 本地版（`local` flavor）多用户 / 多工作区
- 把 `project_id` 升级成隔离键
- 嵌套工作区树（`parent_tenant_id` 递归）
- 主账号默认获得子工作区写权限
- SSO / 账单 / 按记录 ACL
- 把公众共享通道（`/api/records/share`）并进实验室工作区
- 在未完成设备绑定前，直接作废共享令牌（会把旧 APK 草稿永久滞留在手机上）

---

## 4. 推荐模型

原方案的 `tenants + users + memberships + device_credentials + sessions` 成立。审查后做三处收紧，避免两套「谁能进这个工作区」的真相源。

### 4.1 控制面（`output/control/`，全站一份）

不要把用户库再放到某个业务工作区目录里。

```text
accounts          主账号组织（一个登录主体名下的「实验室集团」）
                  id, name, status, created_at
tenants           工作区
                  id, account_id, name, slug, status, created_at
users             全局登录身份（无业务角色、无 tenant_id）
                  id, username UNIQUE, password_hash, salt,
                  display_name, disabled, must_change_password, …
memberships       user_id + tenant_id + role + created_at
                  UNIQUE(user_id, tenant_id)
account_owners    user_id + account_id + role
                  role ∈ {parent_owner}   -- 主账号
device_credentials  每台手机一把
                  id, tenant_id, token_hash, device_label,
                  revoked_at, created_at, last_used_at
sessions          token_hash, user_id, account_id,
                  active_tenant_id NULLABLE,
                  user_secret_version, expires_at
device_bind_codes 一次性绑定码
                  code_hash, tenant_id, expires_at, used_at, created_by
```

**不采用独立的 `tenant_relations` 表。** 主账号能看哪些子工作区 = 该用户是哪些 `account_id` 的 `parent_owner`，再列出该 account 下 `status=active` 的 tenants。绑定 / 解绑子工作区 = 改 `tenants.account_id` 或 `tenants.status`，只走主账号管理 API，并写审计。

**不采用递归 `parent_tenant_id`。** v1 固定两层：Account → Tenant。需要第三层时再迁移，避免现在就把所有查询写成树遍历。

### 4.2 角色

现有 `users.role` 退役，改成 **作用域角色**：

| 作用域 | 角色 | 能力 |
|--------|------|------|
| 平台（仅 seed，不租给客户） | `platform_admin` | 建主账号、运维、看控制面。**不是**子工作区管理员 |
| 主账号 | `parent_owner` | 列出 / 切换 / 汇总 / 导出已绑定工作区；默认只读业务数据 |
| 工作区 | `tenant_admin` | 本工作区用户、设备绑定、设置、**本工作区 reset** |
| 工作区 | `operator` | 本工作区称重、改记录、发布 |
| 工作区 | `viewer` | 本工作区只读 |

禁止把子工作区管理员映射到今天的 `admin`。今天的 `require_admin_session` 能 `POST /api/reset` 清空整盘，语义是平台级的。

主账号若要改子工作区数据：单独 API（如 `POST /api/tenants/{id}/acting-as`），短 TTL、再次确认、审计字段 `actor_user_id + acting_tenant_id + reason`。默认会话里 `active_tenant_id` 对 parent 只开读。

### 4.3 请求上下文（不可伪造）

每个请求进入业务层之前生成冻结的 `TenantContext`：

```text
TenantContext(
  tenant_id,          # 服务端解析，永不取自 JSON / query / form
  account_id,
  actor,              # user | device | legacy_token | platform
  actor_id,
  roles,              # 本请求在该 tenant 上的角色集
  stores,             # 该 tenant 的 BoxRegistry / JobStore / …
  output_root,        # output/tenants/<tenant_id>/
)
```

解析顺序：

1. 有效用户会话 → 用 `sessions.active_tenant_id`；若为空且是 `parent_owner`，只允许 account 级 API（列表 / 汇总），不允许碰业务 store。
2. 设备凭证（`Authorization: Bearer mvdev_…` 或后继头）→ 凭证行上的 `tenant_id`，忽略客户端传的任何 tenant 字段。
3. 过渡期共享令牌 → **写死** `legacy-default`。
4. 否则 401。open mode 在云版部署关闭（`MOUSEVISION_API_TOKEN` 未配置时拒绝写，不再匿名放行）。

客户端上传的 `tenant_id` / `project_id` / `cage_id` **不能**决定写到哪棵目录。`project_id` 仍只当标签写入 record / job。

### 4.4 业务对象唯一约束

全部改为租户内唯一：

| 对象 | 新唯一键 |
|------|----------|
| 箱子 | `(tenant_id, cage_id)` |
| 记录 | `(tenant_id, record_id)` |
| 上报批次 | `(tenant_id, client_batch_id)` |
| 分析任务 | `(tenant_id, job_id)` |
| 批次目录 | `(tenant_id, run_id)` |
| 箱内序号 | `(tenant_id, cage_id)` 上的 `next_ordinal` |

物理分目录时，SQLite 主键可以仍是 `cage_id` / `record_id`（库已经在租户目录内）。应用层和跨库汇总不要假设它们全局唯一。

二维码 v2：

```json
{"v": 2, "tenant_id": "<uuid>", "project_id": "default", "cage_id": "C57-023"}
```

设备已绑定工作区时：若 payload 带 `tenant_id` 且与凭证不一致 → 拒绝，避免扫到别的实验室的箱子却写到本工作区。旧 v1 / 裸箱号：只在当前绑定工作区内解释。

---

## 5. 存储布局

文件系统仍是记录权威来源（`run_*/mouse_*/record.json`）。近期不要把所有租户的 `run_*` 继续混在一个目录里靠 WHERE 过滤——漏一个 glob 就会串数据。

```text
output/
  control/
    control.db          # accounts/tenants/users/memberships/sessions/devices
    audit.db            # 控制面 + 跨租户审计（每条带 tenant_id）
  tenants/
    <tenant_uuid>/
      boxes.db
      jobs.db
      records_meta.db
      upload_queue.db
      settings.json
      mice_registry.json
      job_uploads/
      run_*/
      scale_sync/         # 若保留秤时间同步，必须进租户目录
  shared/                 # 本地版公众共享，不属于任何实验室工作区
  scale_captures/         # 仅平台/研发；不要按子账号开放
```

`TenantStoreFactory.get(tenant_id)`：

- 校验 `tenant_id` 为已存在 UUID，路径只拼服务端 ID，禁止 `../`
- 缓存已打开的 SQLite 连接（按 tenant），但 **禁止** 再提供无 tenant 的模块级 `box_registry` 默认值
- 请求结束不关连接（进程内复用），重置工作区时先停该 tenant 的 job/realtime 再删目录

主账号汇总：服务端遍历其 account 下的 tenant 目录，**扇出读取**，每条结果附 `tenant_id` + `tenant_name`。不要把多个 `boxes.db` attach 成一个可写连接。

### 5.1 迁移现网数据

一次离线窗口（或只读窗口）执行：

1. 创建 `legacy-default` 工作区，account 挂到现有 seed `admin`（临时 `platform_admin` + `parent_owner`，后续再拆）。
2. 把现有 `boxes.db`、`jobs.db`、`records_meta.db`、`upload_queue.db`、`settings.json`、`mice_registry.json`、`run_*`、`job_uploads/` **复制**（不是先删）到 `tenants/<legacy-id>/`。
3. `users.db` 迁入 `control.db`；原 `users.role` 映射为 `legacy-default` 上的 membership（`admin`→`tenant_admin`，其余不变）。**平台角色另表保存**，不要让历史 admin 继续拥有清全盘能力。
4. 核对：箱子数、记录数、`record.json` 哈希、照片 / 视频字节数、job 行数。
5. 切换 `MOUSEVISION_OUTPUT_DIR` 布局；共享 token 映射到 `legacy-default`。
6. 保留原文件只读副本至少一个发布周期，确认云端读写正常再删。

失败则切回旧根目录，不做「半迁半留」。

---

## 6. API 与鉴权改造

### 6.1 默认需身份

除下列入口外，全部 API 要求会话、设备凭证或（过渡期）legacy token：

- `GET /api/health`（只返回 `ok/service`，去掉业务计数或改为无租户含义的进程探活）
- `POST /api/login`、`POST /api/logout`
- 静态资源、`/mobile` HTML（**不再注入 token meta**）
- 本地版共享：`POST /api/records/share`（独立 share token，写入 `output/shared/`）

`GET /api/records/{id}`、照片、视频、箱子、任务必须在解析出 TenantContext 后，到 **该 tenant 的 store** 查找；找不到 → 统一 404，不 403（避免用 403 探测其他租户是否存在该 id）。

### 6.2 设备凭证

- 云版首次：登录子账号，或扫描 `tenant_admin` 生成的一次性绑定码（TTL 短、单次、绑定后作废）。
- 服务端只存 `token_hash`（salted hash），明文只在签发时返回一次。
- 凭证 **固定** `tenant_id`，不能改绑；要换工作区就撤旧签新。
- 访问令牌可短期（如 12–24h）+ 可撤销 refresh；MVP 也允许不透明长期设备 token，但必须能单台撤销。
- 请求头建议新名：`Authorization: Bearer <device>`，过渡期仍接受 `X-MouseVision-Token`：先查 device 表，再查 legacy 共享令牌。

### 6.3 实时称重 / 回放 / 分析队列

- realtime session 创建时写入 `tenant_id`；WS `/ws` 校验 token 所属 tenant 与 session 一致，否则 4403。
- `PlaybackEngine` 按 tenant 分实例，或单实例但 `status/stream` 必须带 TenantContext，禁止返回「当前全局正在播什么」。
- `AnalysisJobManager` 出队时用 job 行上的 `tenant_id` 取 store；worker 线程局部不保存上一个 job 的 output_root。
- `POST /api/reset` 改为 `POST /api/tenants/{id}/reset`，只删该目录，且仅 `tenant_admin` 或平台运维；`parent_owner` 默认不能重置。

### 6.4 审计

现有 [`ui/audit.py`](../ui/audit.py) 增加 `tenant_id`、`account_id`、`actor_type`。敏感字段继续脱敏。主账号代管写操作强制记 `reason`。

---

## 7. 移动端

仅 **cloud** flavor。local 版继续：无同步令牌、数据在本机、可选 share token。

1. 删除 APK `MV_CONFIG.token` 与网页 meta 中的全局写令牌（新包）。
2. 启动：无设备凭证 → 登录页或扫绑定码；有凭证 → 拉工作区摘要。
3. Outbox 键：`mv.reportOutbox.v2.<tenant_id>`；每条 batch 内快照 `{tenant_id, credential_id}`。
4. flush 前：当前凭证的 `tenant_id` 必须等于 batch 快照，否则拒绝发送并留在原队列。
5. 切换账号：不自动用新凭证 flush 旧键；UI 提示「上一工作区还有 N 条未上传」。
6. 旧 APK（v1 键 + 共享令牌）：服务端把该令牌映射到 `legacy-default`；新服务继续接受直到该工作区出站队列为空、且现场 APK 已升级。
7. 共享 outbox（`mv.shareOutbox.v1`）保持独立，与租户队列隔离（现有 local 逻辑已隔离，不要合并）。

H5 测试与 Android 单测都要覆盖：换 tenant 后旧队列不发送。

---

## 8. 实施顺序

原方案 7 步可用，但 **不能先「全部 API 要登录」再引入 tenant**：云版 APK 会立刻全军 401，手机草稿无法上报。改为「先等价迁移，再收口身份」。

| 阶段 | 内容 | 对外行为 |
|------|------|----------|
| 0 | 冻结模型；`TenantContext` + `TenantStoreFactory` 接口；测试夹具 | 无 |
| 1 | 物理目录 + 迁入 `legacy-default`；全局单例改为 factory（暂时只有一个 tenant） | 行为等价 |
| 2 | 读 API 一律走 TenantContext；匿名读关闭，但 **legacy token 仍可读写 legacy-default** | 旧 APK 仍可用 |
| 3 | memberships、设备凭证、绑定码；云版 APK 登录 / 绑定；outbox v2 | 新包多工作区 |
| 4 | 主账号列表、切换、汇总看板、跨工作区导出（只读） | 主账号可用 |
| 5 | 代管写（可选，可放到 4 之后） | 显式确认 |
| 6 | 小流量；确认旧 outbox 排空；撤销共享令牌与 HTML 注入；关 open mode | 切断全局 token |

单人完整（含代管写、主账号汇总、APK、迁移脚本、隔离测试）：**4–6 工程周**。若 v1 只做「子账号上传+查看，主账号只读汇总」：约 **3–4 周**。不要在第 1 阶段并行做 PC 新 UI。

本任务由 **一个 agent 连续完成**：先写隔离回归，再实现、迁移演练、全量复核并输出证据。不得把编码、复核或测试外包给另一个 agent；测试结果以 pytest / Node TAP / Gradle XML 的真实计数为准。

---

## 9. 测试与验收

仓库现状：鉴权 / 上报 / 箱子相关约 67 passed，**零条租户测试**。本升级的回归主体是新测试，而不是「旧测试仍然全绿」（旧测试会在阶段 1 改夹具后继续绿，但证明不了隔离）。

必须覆盖：

1. 两租户同 `cage_id`：各写各的 `next_ordinal`，互不重号、互不可见。
2. 两租户同 `record_id`：详情 / 照片 / 视频只返回当前租户；跨租户猜 URL → 404。
3. 设备凭证绑 A，请求里带 B 的 `tenant_id` 或 `project_id` → 仍写入 A。
4. `parent_owner` 未绑定的租户 → 404/403（列表不含）。
5. 租户 reset 只删自己的 `run_*` 与 db；`control/` 与其他 tenant 目录仍在。
6. outbox：存储键分离；换凭证 flush 被拒；legacy 键 + 共享令牌只进 `legacy-default`。
7. realtime session 跨租户 `session_id` → WS 4403 / REST 404。
8. `platform_admin` 与 `tenant_admin` 权限不重叠：后者不能列其他租户用户、不能改 control.db。
9. 并发：两设备同租户同箱号同时 reserve ordinal，不重复。
10. 迁移脚本：复制后记录数、文件哈希、照片存在性一致。

阶段 2 起，CI 必须跑这组隔离测试；阶段 3 起 Android / H5 增加 outbox 键测试。

---

## 10. 对原方案的修订清单

| 原方案 | 审查决定 |
|--------|----------|
| `users` 不再写死角色 | 采纳 |
| `tenants.parent_tenant_id` 递归 | **v1 不做**；改为 `accounts` → `tenants` 两层 |
| 独立 `tenant_relations` | **合并进 account_owners + tenants.account_id**，避免双真相源 |
| 物理分目录 | 采纳，且必须改掉进程内全局 store |
| 先把所有 API 改为需登录 | **延后到 legacy token 仍能映射 default 之后**，避免旧 APK 卡死 |
| 主账号默认只读 + 显式代管 | 采纳 |
| 子账号管理员 = 现有 `admin` | **禁止**；现有 admin 是平台级清盘权限 |
| 共享令牌过渡映射 default | 采纳；撤销前必须确认旧队列已空 |
| outbox `v2.<tenant_id>` | 采纳；批次内再快照 tenant，flush 时校验 |
| 单人 4–6 周 / 收窄 3–4 周 | 在「不做嵌套租户、不做代管写」时成立 |

---

## 11. 明确不改的文件与渠道

- 不把内测云版 APK 发到 GitHub Release（现有发布惯例：只发 local APK + USER_GUIDE）。
- 不把 share / scale_captures 当作子工作区数据。
- 实施前不修改 `android/app/build.gradle.kts` 的无关本地改动；租户阶段再单独去掉 cloud 的 `syncToken` 注入。
- `USER_GUIDE.md` 面向本地版，租户登录流程不要写进公众说明书；云版另写内测说明。

---

## 12. 阶段 0 接口草稿（供编码对照）

```python
@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    account_id: str
    actor_type: str          # user | device | legacy_token | platform
    actor_id: str
    roles: frozenset[str]
    output_root: Path

class TenantStoreFactory:
    def context_from_request(self, request) -> TenantContext: ...
    def stores(self, tenant_id: str) -> TenantStores: ...
    def require_role(self, ctx: TenantContext, *roles: str) -> None: ...
```

业务 handler 签名从「模块全局 `box_registry`」改为 `stores = factory.stores(ctx.tenant_id)`。找不到这条重构，分目录只是把串数据从「一个文件夹」变成「一个单例」。

---

## 13. 给执行 agent 的完整指令

当用户说“按本文执行”时，把本节到第 18 节视为执行合同；前文是架构约束和验收依据。

### 13.1 唯一目标

在不破坏现有称重、草稿防丢、本地版和公众共享通道的前提下，完成云版两层账号模型：

```text
Account（主账号） → Tenant（子工作区） → 成员 / 设备 / 业务数据
```

交付必须同时包含后端、PC 管理端、云版 H5/Android、迁移工具、自动化测试和部署文档。只做 UI 筛选、只给 `users` 加 `parent_id`、只给部分表加 `tenant_id`，均不算完成。

### 13.2 执行行为

1. **一个 agent 从头做到尾**，不创建子任务、不委派编码、不把复核交给其他 agent。
2. 先读取 `git status`、相关 diff、当前 HEAD 和真实入口；保留所有用户已有改动。当前已知 `android/app/build.gradle.kts` 有未提交改动、`android/.kotlin/` 是未跟踪生成目录，但执行时仍以现场检查为准。
3. 除非遇到第 13.4 节的硬阻塞，连续完成所有本地批次；不要每完成一个文件就停下来请求确认。
4. 用现有 store、`persist_report_records()`、记录目录格式、outbox 防丢逻辑和 local/share 分流；不借机重写前端框架、称重算法、OCR、BLE 或任务队列。
5. 不卸载 App、不清手机数据、不覆盖旧数据根、不删除旧 token 对应的待传队列。迁移一律先复制、校验，再切换。
6. 不输出、记录或提交密码、设备凭证明文、生产 API token、Cookie、签名密码；日志和审计继续脱敏。
7. 本地实现和测试在授权范围内直接完成。**commit、push、生产迁移、部署、发 APK 必须分别有用户明确授权**；缺少该授权不影响判定 `implementation_complete`。
8. 不以“旧测试通过”替代租户隔离验收，不允许关键租户测试 `skip`、`xfail` 或只测前端隐藏。

### 13.3 禁止扩张

- 不做递归组织树、SSO、计费、邀请邮件、按单条记录 ACL。
- 不把 SQLite 全量改成 PostgreSQL，不引入 Redis/Celery，不改现有单 worker 约束。
- 不把原生 H5 迁移到 React/Vue；Playwright 只作为测试依赖，不作为前端构建链。
- 不把 `shared/`、研发 `scale_captures/`、本地版数据混进 Tenant。
- 不把 `project_id` 解释成 tenant，也不允许客户端字段决定服务端输出根。

### 13.4 只有这些情况可以暂停并报告 blocked

- 用户已有改动与必须修改的同一语义冲突，且无法无损合并；报告具体文件和冲突片段。
- 无法唯一识别生产旧数据根、备份失败、磁盘不足，或迁移 `verify` 的数量/哈希不一致。
- 控制面 schema、旧账号角色映射或现有数据出现无法自动决定的一对多歧义。
- 必需的签名材料、生产权限或维护窗口缺失：只阻塞 `release_verified`，不能阻塞本地实现和迁移演练。
- 某测试暴露与本任务无关的既有故障：保存命令、完整失败摘要和最小复现；继续执行不依赖该故障的批次。

普通实现困难、测试耗时、文档过时、需要新增迁移代码，都不是暂停理由。

---

## 14. 复用边界与文件责任

### 14.1 只新增一套控制面

建议新增：

- `ui/control_store.py`：accounts / tenants / users / memberships / account_owners / sessions / device credentials / bind codes 的 schema 与单调迁移。
- `ui/tenant_context.py`：冻结的 `TenantContext`、身份解析、作用域角色检查。
- `ui/tenant_stores.py`：唯一的租户目录解析和 `TenantStoreFactory`。
- `tools/migrate_tenant_storage.py`：只负责 inventory / stage / verify / activate 前检查，不在 Web 请求里偷偷迁移大文件。
- `tests/test_tenant_*.py`、`tests/h5/tenant-outbox.test.mjs`、`tests/e2e/`：隔离、移动端和主账号 E2E。

### 14.2 必须改造但不得复制的现有实现

| 范围 | 改造要求 | 禁止 |
|------|----------|------|
| `ui/users.py` / `ui/auth.py` | 兼容迁入 control store；产出 TenantContext | 保留第二套可绕过 TenantContext 的登录真相源 |
| `ui/app.py` | 删除业务 store 全局默认；所有 handler 显式取 `ctx.stores` | 用可选 `tenant_id=None` 回退全局根 |
| `ui/report_api.py` | `persist_report_records()` 继续唯一落盘核心，调用方注入 tenant root | 为租户再复制一个 report endpoint |
| `ui/realtime_api.py` / `ui/scale_sync_api.py` / `ui/capture_api.py` | 去除一次性全局 `configure(output_root)` 的业务写入语义 | 模块变量记住上一个请求的 tenant |
| `ui/registry.py` / `ui/boxes.py` / `ui/records_meta.py` / `mousevision/jobs.py` / `mousevision/upload_queue.py` | 实例继续复用，但实例只能由 factory 按 tenant 构造 | 在每个查询里散落手写路径拼接 |
| `ui/static/report-client.js` | 保留现有原子持久化、死信迁移和重试顺序，外层增加 tenant/credential 绑定 | 重写队列导致 401、死信或并发重传回归 |
| `android/app/build.gradle.kts` | 合并用户已有版本改动后，移除 cloud 共享 token 注入 | 回退版本号或覆盖签名配置 |
| `ui/share_api.py` | 维持现有独立 shared root 和 share token | 复用 TenantContext 后把匿名共享数据写进实验室 |

### 14.3 路由必须有机器可检查的分类

为 FastAPI 路由建立显式策略测试，至少分为：`public`、`account`、`tenant_user`、`tenant_device`、`legacy_default_only`、`share_only`。任何新增 `/api/*` 路由没有分类时测试失败。不要仅靠人工 grep 查漏网匿名接口。

---

## 15. 单 agent 连续执行批次

严格按 B0 → B8 执行。一个批次失败时先修复；只有满足第 13.4 节才暂停。

### B0：现场基线与保护

- 记录 HEAD、branch、remote、工作区状态和相关 diff；不得清理 `android/.kotlin/` 或回退 `build.gradle.kts`。
- 读取 `ui/app.py`、所有 `*_api.py`、auth/users、各 store、Android config、H5 outbox、部署文档和相关测试。
- 枚举全部 FastAPI HTTP/WS 路由、依赖、当前匿名状态和数据根。
- 跑第 16 节 G1 基线命令并保存计数。2026-09-03 参考基线为 Python `569 passed`、H5 `239 passed`、Android cloud/local 各 `35 tests`；执行时以现场为准，变化必须解释。

**B0 Done：** 已知用户改动被标记为 preserve；路由和全局状态清单完整；基线失败均有证据。

### B1：先写会失败的隔离契约

- 建两个 account、三个 tenant、parent、tenant admin/operator/viewer、两台 device fixture。
- 先增加第 9 节 10 类测试，并增加未分类 API 路由失败测试。
- 加入 control schema 升级/重复执行/损坏输入/会话撤销/绑定码单次消费测试。
- 新测试在旧实现上必须因缺租户能力而失败，避免写成永远为绿的空断言。

**B1 Done：** 隔离测试能准确暴露全局 store、匿名读、共享 token、跨账号 outbox 和全局 reset。

### B2：控制面和 TenantContext

- 实现第 4 节控制面 schema，所有迁移带 `schema_version`，只能向前、事务化、可重复启动。
- 密码沿用当前 PBKDF2 兼容验证；新会话 token 和 device token 仅存哈希。补 `user_secret_version`，改密/禁用/撤销后旧会话失效。
- 实现 account 级和 tenant 级角色，不再从 `users.role` 直接授权业务动作。
- 实现 `TenantContext` 解析；legacy token 只能产出固定 legacy tenant context，云版 open mode fail closed。

**B2 Done：** 身份不能从 body/query/form 伪造 tenant；平台、主账号、子工作区和设备权限测试通过。

### B3：租户 store、文件根和业务链路

- 实现 `TenantStoreFactory`，把 `DEFAULT_OUTPUT` 仅保留为总根，不再直接作为业务记录根。
- 逐条改造 boxes、records/meta、jobs、upload queue、settings、audit、realtime journal、scale sync、report、回放、缩略图、导出和恢复流程。
- job 入队时固化 `tenant_id`；worker 每次出队重新由 factory 解析 store/root。
- 所有 record/photo/clip/job/run 查找只在当前 tenant 内进行；跨 tenant 与不存在统一 404。
- reset 变成租户操作：先阻止该 tenant 新写入、停止其活动 job/realtime、再只处理其目录。

**B3 Done：** 搜索不到业务 handler 对模块级 `registry/job_store/box_registry/records_meta/upload_queue/settings_store/DEFAULT_OUTPUT` 的无上下文访问；B1 后端隔离测试通过。

### B4：路由默认关闭与兼容窗口

- 落实第 6 节路由分类。匿名只能访问真正 public/share 入口。
- legacy token 仅在兼容窗口访问 `legacy-default`，响应增加可观测的 deprecation 标记，但不得把 token 或 tenant secret 写日志。
- Cookie 写接口增加同源/CSRF 防护；设备接口不用 Cookie，CORS 只放行需要的 origin/header/method。
- account 汇总 API 与 tenant 业务 API 分开，parent 不能把任意 tenant UUID 塞入普通子账号 API。

**B4 Done：** 路由策略测试无未分类项；匿名探测业务 API 全部 401/404；旧 APK 兼容测试仍可写 legacy tenant。

### B5：云版设备绑定和 outbox v2

- 实现一次性短 TTL 绑定码、单台撤销、last-used、凭证轮换；明文只签发一次。
- cloud H5 增加登录/绑定/当前工作区/撤销后的重新绑定状态；local H5 行为不变。
- outbox 改为 `v2.<tenant_id>`，batch 固化 tenant + credential；flush 不匹配时保留原队列并给出明确 UI。
- 提供 v1 队列只上传 legacy tenant 的兼容路径；不静默把 v1 数据迁入当前新账号。
- cloud APK 不再打包 `MICE_SYNC_TOKEN`，服务器托管 `/mobile` 不再注入共享 token meta。

**B5 Done：** H5 断网、401/403、死信、并发 retry 的现有防丢测试继续通过；新增换账号错传测试通过；APK 资产无共享写 token。

### B6：主账号和子工作区后台

- 主账号页：工作区列表、状态、记录/箱子/待核对计数、最近同步时间；支持显式切换只读查看和跨工作区导出。
- 所有汇总行、明细、导出 CSV/XLSX 都带 `tenant_id` 和 `tenant_name`。
- 子账号后台不显示其他 tenant 名称、计数或可猜链接；UI 隐藏之外，API 必须先已拒绝。
- v1 不实现代管写；若已有入口，保持禁用并说明只读。

**B6 Done：** Chromium E2E 覆盖 parent 看已绑定 A/B、不看未绑定 C；子 A 看不到 B；直接深链也不能越权。

### B7：迁移工具和可回滚演练

- CLI 固定为 `inventory`、`stage`、`verify` 三个无生产副作用子命令；`activate` 必须另有显式参数与维护模式检查。
- staging 必须是源目录的兄弟目录，禁止放在 source 内部，防止递归复制。
- 复制白名单见第 5.1 节；忽略缓存/临时文件要显式列入报告，不能用宽泛 glob。
- `verify` 至少比较 DB 行数、run/record/photo/video 数量、record JSON SHA-256、总字节数、缺失文件和重复 ID。
- 激活前停止写入并做最终 inventory/stage/verify；旧根原子改名为带时间戳只读备份，新根再原子就位。
- 激活后已有新写入时禁止直接切回旧根；必须先反向对账/合并，避免丢掉新记录。

**B7 Done：** 在合成旧目录和一份脱敏生产快照上重复演练两次均通过；故意损坏照片、DB 行或 JSON 时 verify 必须非零退出且不 activate。

### B8：全量复核、文档和交付

- 跑 G1–G7；逐项检查本地版、share 通道、云版、PC、realtime、reset 和迁移。
- 更新 `PC_ADMIN_PLATFORM.md`、`MOBILE_WEB_APP_DESIGN.md`、`DEPLOYMENT.md`；新增云版账号/设备绑定说明，不改面向公众本地版的 `USER_GUIDE.md` 语义。
- 生成最终 diff、风险清单和分层完成状态。只有用户明确授权时才 commit/push/部署/签名发包并继续 G8。

**B8 Done：** 满足第 17 节对应终态，并按第 18 节交付报告。

---

## 16. 不可跳过的门禁命令

命令以仓库根为工作目录。依赖路径变化时允许使用等价的已验证路径，但必须在报告中写明。

### G1：静态与工作区

```bash
git status --short --branch
git diff --check
rg -n "DEFAULT_OUTPUT|configure\(|MOUSEVISION_API_TOKEN|mv\.reportOutbox\.v1|X-MouseVision-Token" ui mousevision android/app/src tests
```

`rg` 有结果不自动失败；agent 必须逐条分类为 control root、legacy compatibility、share-only、test fixture 或尚未改完。无法解释的业务使用即失败。

### G2：Python 全量与隔离专项

```bash
.venv/bin/python -m pytest -q tests/test_tenant_*.py tests/test_admin_platform.py tests/test_api_auth.py tests/test_records_report.py tests/test_boxes.py tests/test_realtime.py tests/test_scale_sync.py
.venv/bin/python -m pytest -q
```

隔离专项不得 skip。全量测试现有视频 fixture 若缺失，agent 应先确认是否为环境缺件；不能把关键租户测试归入可跳过集合。

### G3：H5 全量

```bash
node --test 'tests/h5/**/*.test.mjs'
```

必须保留现有 outbox 防丢、死信持久化、401/403 保留和并发 retry 测试；新增 tenant outbox 测试必须实际运行。

### G4：Android 双 flavor 单测

```bash
JAVA_HOME=/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home \
  .toolchain/gradle-8.11.1/bin/gradle -p android \
  :app:testCloudDebugUnitTest :app:testLocalDebugUnitTest --no-daemon

find android/app/build/test-results -name 'TEST-*.xml' -print
```

报告必须汇总 XML 中 `tests/failures/errors/skipped`，不能只写 `BUILD SUCCESSFUL`。

### G5：迁移破坏性测试与演练

实现后的 CLI 必须支持下列形态；参数名若合理调整，文档和测试同步更新：

```bash
mv_tenant_tmp=$(mktemp -d /tmp/mv-tenant-rehearsal.XXXXXX)
.venv/bin/python tools/migrate_tenant_storage.py inventory \
  --source "$mv_tenant_tmp/legacy" --report "$mv_tenant_tmp/inventory.json"
.venv/bin/python tools/migrate_tenant_storage.py stage \
  --source "$mv_tenant_tmp/legacy" --staging "$mv_tenant_tmp/v2" \
  --legacy-tenant-id 00000000-0000-4000-8000-000000000001
.venv/bin/python tools/migrate_tenant_storage.py verify \
  --source "$mv_tenant_tmp/legacy" --staging "$mv_tenant_tmp/v2" \
  --report "$mv_tenant_tmp/verify.json"
```

测试必须自行生成合成旧数据；不得把仓库真实 `output/` 当测试删除目标。

### G6：Chromium E2E

根目录新增最小、锁定版本的 Playwright 测试依赖，只用于测试，不接管前端构建：

```bash
npm ci
npm run test:e2e
```

E2E 必须断言页面进入终态、没有永久 loading，并验证 parent/child/未授权 tenant 三条路径。若运行环境缺 Chromium，可先完成代码，但 `implementation_complete` 仍不得标记，除非在另一个可复现环境补跑通过。

### G7：双 flavor 构建与 APK 内容

```bash
JAVA_HOME=/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home \
  .toolchain/gradle-8.11.1/bin/gradle -p android \
  :app:assembleCloudRelease :app:assembleLocalRelease --no-daemon
```

- 解包 cloud APK，确认不存在可用的全局写 token；local 版仍无实验室同步凭证。
- 用 `aapt dump badging` 核对 package/version；有签名材料时用 JDK 17 下的 `apksigner verify --verbose --print-certs`。
- 构建/检查过程中不得打印 token、keystore 密码或完整设备凭证。

### G8：发布与线上验证（仅明确授权后）

1. 核对 production checkout/镜像/容器当前 commit，先备份和 inventory，不接受“本地 HEAD 相同所以线上也相同”。
2. 维护窗口停止写入，执行 stage + verify；任何差异立即终止，不 activate。
3. 切换后确认容器使用新镜像和新目录；检查 health、登录、legacy-default 兼容、新 device 绑定。
4. 用两个测试 tenant 验证同箱号/同 record ID、匿名读取、照片/视频深链、parent 汇总、child 隔离、租户 reset。
5. 查看日志和 audit：不得出现 token/password，不得出现跨 tenant 路径；确认旧 outbox 的可观测计数。
6. 旧 APK 队列未排空前不撤 legacy token；撤销动作单独记录时间、影响面和回滚条件。

### G9：最终证据

```bash
git diff --check
git status --short --branch
git diff --stat
```

如用户授权 commit，报告 commit hash；如授权 push，核对远端确有该 hash；如授权部署，记录镜像 ID、线上 commit、迁移 verify 报告和浏览器/API 结果。三者不得互相替代。

---

## 17. 完成状态

只能使用以下终态，禁止笼统写“已完成”：

### `implementation_complete`

满足 B0–B8，G1–G7 全部通过，迁移只在本地/脱敏快照演练；代码、测试、文档、双 flavor 构建齐全。可以没有 commit、push、部署，但必须明确写出状态。

### `release_verified`

在 `implementation_complete` 基础上，用户已授权且 G8/G9 完成：生产备份与迁移校验通过、远端代码/镜像匹配、线上隔离和兼容路径验证通过。只有此状态才能说“线上升级完成”。

### `blocked`

符合第 13.4 节，且安全范围内没有可继续的工作。报告阻塞证据、已完成批次、未执行门禁和解除阻塞所需的最小输入。缺少部署授权通常应写 `implementation_complete, release: 未执行`，而不是 blocked。

任何隔离测试失败、关键测试 skip、迁移 verify 不一致、旧队列无迁移/兼容方案，都不允许标记完成。

---

## 18. 最终交付报告模板

```text
状态：implementation_complete | release_verified | blocked

基线：
- branch / start HEAD / end HEAD
- 执行前已有改动及保留方式

实现批次：
- B0 ... B8：逐项 done / blocked
- 关键设计偏差及理由

门禁：
- G1 静态：结果
- G2 Python：通过/失败/skip 计数
- G3 H5：通过/失败/skip 计数
- G4 Android：cloud/local XML 计数
- G5 迁移：inventory/verify 摘要与报告路径
- G6 Chromium E2E：场景与结果
- G7 APK：版本、flavor、签名、token 检查
- G8 线上：已验证 | 未授权未执行 | blocked
- G9 diff/commit/push/deploy：分别列出

数据安全：
- 旧数据根/备份位置
- 旧 outbox/legacy token 状态
- 是否存在新写入后的回滚限制

残余风险：
- 仅列有证据的未完成项
```

最终报告必须分别写：`commit`、`push`、`deploy`、`online verification`。本地测试全绿不等于已发布，发布了也不等于跨租户隔离已经在线验证。
