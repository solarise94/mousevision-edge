# PC 管理后台与智能入口

> 租户隔离升级（云版）后，PC 管理台运行在**两层账号模型**上：所有业务数据归属工作区（tenant），
> 登录会话必须先激活一个工作区才能读写业务 API。本文档随 `ui/app.py`、`ui/control_api.py`、
> `ui/account_api.py`、`ui/tenant_context.py` 演进同步更新。

## 智能入口（跳转代理）

根路径 `/` 提供**智能入口页**，按用户意图与设备类型分流：

| 参数 | 行为 |
|------|------|
| `?intent=record` | 跳转 `/mobile`（录制） |
| `?intent=manage` | 桌面 → `/pc`；手机 → `/mobile/manage` |
| `?to=mobile` | 302 → `/mobile` |
| `?to=pc` | 302 → `/pc` |
| `?to=manage` | 302 → `/mobile/manage` |
| 无参数 | 展示「录制 / 管理」二选一，并按 UA 高亮推荐项 |

实现文件：

- [ui/static/entry.html](../ui/static/entry.html)
- [ui/static/entry.js](../ui/static/entry.js)
- [ui/app.py](../ui/app.py) 中 `GET /` 路由

## 电脑端管理后台

路径 `/pc`，深色管理台 UI，侧栏模块：

- **工作区总览**（主账号 / 平台管理员可见）：`/pc/workspaces`，对接 `GET /api/account/summary`
- **数据管理**：筛选、KPI、Tab（全部/待核对/已发布/已删除）、卡片列表、右侧详情
- **数据总览**：KPI + 每日记录趋势
- **数据核对 / 发布管理**：基于记录生命周期的队列操作
- **导出管理**：按筛选导出 CSV / XLSX（当前激活工作区内）
- **箱子管理 / 小鼠管理**：对接 `/api/boxes*` 与 `/api/mice-admin`
- **用户管理 / 操作日志 / 系统设置**：见下文「鉴权」与「旧 `/api/users` 端点的兼容语义」

前端：`ui/static/pc/`（零构建原生 JS SPA）。

## 两层账号模型（Account → Tenant）

```text
Account（主账号组织）
  ├─ parent_owner（主账号用户，可多个）
  └─ Tenant 工作区 A ── 成员（tenant_admin / operator / viewer）+ 设备凭证 + 箱子/记录/文件/设置
  └─ Tenant 工作区 B ── …
platform_admin（平台管理员，另表保存，不属于任何工作区）
```

- 控制面数据（accounts / tenants / users / memberships / account_owners / device_credentials /
  sessions / device_bind_codes）全站一份，存于 `output/control/control.db`；审计在
  `output/control/audit.db`（每条带 `tenant_id` / `account_id` / `actor_type`）。
- 业务数据（boxes.db、records_meta.db、run_*、settings 等）全部落在
  `output/tenants/<tenant_uuid>/`，互不共享。
- v1 固定两层，无嵌套工作区；`project_id` 仍只是任务标签，**不承担隔离**。
- 控制面 API（`/api/control/*`）：

| 方法与路径 | 说明 |
|------------|------|
| `POST /api/control/accounts`、`GET /api/control/accounts` | 平台建主账号（带 owner）；列表仅平台 / parent_owner |
| `POST /api/control/accounts/{account_id}/tenants`、`GET …` | 建 / 列工作区 |
| `GET /api/control/tenants` | 当前登录用户可见的工作区列表 |
| `GET/POST /api/control/tenants/{tenant_id}/members`、`DELETE …/members/{user_id}` | 成员管理（tenant_admin / 平台） |
| `GET/POST /api/control/tenants/{tenant_id}/devices`、`DELETE /api/control/devices/{device_id}` | 设备凭证签发 / 列表 / 撤销 |
| `POST /api/control/tenants/{tenant_id}/bind-codes` | 生成一次性绑定码（TTL ≤ 600s，默认 300s，单次消费） |
| `POST /api/control/devices/bind` | 绑定码换设备凭证（明文只返回一次） |
| `POST /api/control/devices/login` | 子账号密码换设备凭证（云版首次绑定路径之二） |
| `POST /api/control/devices/{device_id}/rotate` | 凭证轮换（签发新 + 撤旧，单事务原子） |
| `GET /api/control/session`、`POST/DELETE /api/control/session/tenant` | 会话信息 / 激活与清除 active_tenant_id |

### 作用域角色

旧 `users.role`（admin/operator/viewer 全局角色）已退役，改为**作用域角色**：

| 作用域 | 角色 | 能力 |
|--------|------|------|
| 平台 | `platform_admin` | 建主账号 / 工作区、控制面运维、`GET /api/account/summary` 全量视图。**不是**子工作区管理员，业务写一律 403 |
| 主账号 | `parent_owner` | 列出 / 切换 / 汇总 / 导出自己 account 下的工作区；业务数据**默认只读** |
| 工作区 | `tenant_admin` | 本工作区成员、设备绑定、设置写（`PUT /api/settings`）、本工作区 reset（`POST /api/tenants/{tenant_id}/reset`） |
| 工作区 | `operator` | 本工作区称重、改记录、发布 |
| 工作区 | `viewer` | 本工作区只读 |
| 设备 | `device`（+operator） | 设备凭证上下文，等价 operator 级写入，无管理角色 |

读 = viewer/operator/tenant_admin/parent_owner；写 = operator/tenant_admin（设备上下文按 operator 过）；
设置写与租户 reset = tenant_admin（或平台）。v1 不做主账号「代管写」：平台管理员在汇总页对
无成员身份的工作区「进入」置灰。

### 请求上下文解析（不可伪造）

每个业务请求先解析冻结的 `TenantContext`（`ui/tenant_context.py`），顺序：

1. **用户会话**（Cookie `mv_session`）→ 用会话的 `active_tenant_id` 取成员角色；无激活租户时
   parent_owner 只能访问 account 级 API，platform_admin 得到平台上下文，其余业务 API 403。
2. **设备凭证**（`Authorization: Bearer mvdev_…`；过渡头 `X-MouseVision-Token` 先查设备表）→
   凭证行绑定的 `tenant_id`，忽略客户端传的任何 tenant 字段；未知 `mvdev_` 401 不回退。
3. **过渡期共享令牌**（`MOUSEVISION_API_TOKEN`）→ 写死 `legacy-default` 工作区 + `operator` 角色；
   响应带 `X-MV-Deprecated-Token: 1` 观测标记。
4. 否则 401。云版 open mode 已关闭：`MOUSEVISION_API_TOKEN` 未配置时一律 401（fail-closed）。

租户只来自服务端解析（会话 / 凭证），**永不**取自 JSON body / query / form。

### 登录自动激活规则

`POST /api/login` 成功后服务端自动决定会话的 `active_tenant_id`（`ui/app.py` `_auto_activate_tenant`）：

- **恰好 1 个 active 工作区**（成员或主账号身份）→ 自动激活，保留旧「登录即可用」体验；
- **多个** → 不自动选，由 PC 顶栏工作区切换器显式激活（`POST /api/control/session/tenant`）；
- **0 个**（空 account 的主账号 / 纯平台管理员）→ 保持 account / 平台级；
- `paused` 工作区不参与自动激活；
- **`platform_admin` 一律豁免**：即使同时是某工作区成员也不自动激活（平台会话 + 显式设备/legacy
  令牌同时出现时按令牌解析，自动激活会压过该契约）；平台进入具体工作区需显式切换（须有成员身份）。
- `POST /api/me/password` 改密后换发的新会话同样自动激活。

登录响应在既有字段上只增不改：附加 `tenants` 与 `active_tenant_id`。

### 主账号汇总页与跨工作区导出

`/pc/workspaces`（路由 `workspaces`）：

- 数据源 `GET /api/account/summary`：parent_owner → 自己 account 下全部 active 工作区；
  platform_admin → 全部 active 工作区；子账号 403；匿名 401。
- 每行字段：`{tenant_id, tenant_name, account_id, account_name, status, boxes, records,
  pending_uploads, last_sync_at}`（`records` 计数为租户目录 `run_*/mouse_*/record.json` 文件数；
  `last_sync_at = max(records_meta 最新 updated_at, 设备凭证最近 last_used_at)`）。
- 「跨工作区导出」按钮 → `GET /api/account/export`：CSV（utf-8-sig）/ XLSX 只读导出，
  行前置 `tenant_id` / `tenant_name` 两列，其余列与单工作区导出一致。
- 卡片「进入」以只读身份激活该工作区并落数据页；只读工作区（parent_owner / viewer）顶栏显示
  「只读」徽标，不渲染写入口。
- 主账号无任何激活租户时，登录/引导默认落本页（避免业务 API 403 白屏）。

### 旧 `/api/users` 端点的兼容语义

`GET/POST /api/users`、`PATCH/DELETE /api/users/{user_id}` 保留，鉴权仍为会话 admin 角色
（`require_role("admin")`），但语义收窄为**legacy-default 工作区成员管理通道**：

- 「admin」角色现由控制面派生（`ui/users.py` 兼容门面）：`platform_admin` → admin；
  legacy-default membership 为 `tenant_admin` → admin；`operator` → operator；其余 → viewer。
- 该通道 CRUD 的用户全部落在 legacy-default 工作区的 membership 上（`admin` 映射为
  `tenant_admin`），**管理不到其他工作区的成员**，也没有旧「admin 可 `POST /api/reset` 清全盘」
  的能力（全局 reset 已删除，见下）。
- 新工作区的成员一律走控制面 API（`/api/control/tenants/{id}/members`）。
- 旧 `users.db` 不再被读取；迁移工具将其列入 ignored 报告（不迁入租户），最终保留在只读备份中。
  登录 / 会话的唯一真相源是 `control/control.db`。

## 记录生命周期（叠加层）

体重数据以 `output/tenants/<tenant_uuid>/run_*/mouse_*/record.json` 为权威来源；生命周期状态存于
同租户目录的 `records_meta.db`：

- `pending`（默认）
- `published`
- `deleted`（软删除，文件保留）

API：`GET /api/records`、`POST /api/records/{id}/publish` 等，见 [ui/app.py](../ui/app.py)；
跨工作区只读视图见上文「主账号汇总页」。

## 鉴权

- **会话登录**：`POST /api/login`，Cookie `mv_session`（HTTPS 下自动 / 可通过 `MOUSEVISION_HTTPS=1` 设置 `Secure`）；登录即按上文规则自动激活工作区
- **作用域角色**：`platform_admin` / `parent_owner` / `tenant_admin` / `operator` / `viewer`（旧全局 admin/operator/viewer 已退役，兼容派生见上文）
- **强制改密**：首次 seed 的 admin 必须调用 `POST /api/me/password` 后才能访问管理 API；改密撤销全部旧会话并重签 Cookie
- **共享 token（过渡）**：`MOUSEVISION_API_TOKEN` 仅映射 `legacy-default` 工作区，响应带 `X-MV-Deprecated-Token: 1`；未配置时不再匿名放行（401 fail-closed）；token **永不**映射为 admin 会话，HTML 页面（`/`、`/pc`、`/mobile`、`/legacy`）**不再注入** token meta
- **CSRF 同源校验**：携带会话 Cookie 的写请求（非 GET/HEAD/OPTIONS）要求 Origin（兜底 Referer）host 与请求 Host 一致，失败 403；两者都缺省放行（非浏览器客户端不自动附带 Cookie）。设备 / token / share / bind 通道无 Cookie，不受约束
- **登录限流**：同一 IP 5 分钟内失败 5 次返回 429；仅当设置 `MOUSEVISION_TRUST_PROXY=1` 时才信任 `X-Forwarded-For`
- **租户 reset 取代全局 reset**：`POST /api/reset` 为**永久 403 墓碑**（任何主体，含平台管理员）；重置工作区用 `POST /api/tenants/{tenant_id}/reset`（tenant_admin / 平台），只删该租户目录，先置 `paused` 阻止新写入、停该租户 realtime 会话与回放引擎、活动分析任务返回 409
- **健康检查**：`GET /api/health` 只返回 `{ok, service}`，不含业务计数

## 软删除语义

`DELETE /api/records/{id}` 仅写 `records_meta.status=deleted`，磁盘文件保留。

- 默认读取（手机本箱列表、详情、照片）隐藏已删除记录
- 管理端 `tab=deleted` 或 `include_deleted=true` 才可见
- `POST /api/records/{id}/restore` 恢复为 `pending`
