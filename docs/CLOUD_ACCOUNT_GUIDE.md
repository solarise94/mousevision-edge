# 云版账号与设备绑定 · 内测说明（内部文档）

> 适用范围：**云版内测**（cloud APK + 边缘服务器）。面向运营 / 现场支持人员。
> 本文档描述租户/账号体系的内部语义；面向公众的本地版说明书是
> [USER_GUIDE.md](./USER_GUIDE.md)（本地版无账号、无联网上传，两者不要混写）。
> 本文与代码行为同步（`ui/control_api.py`、`ui/account_api.py`、`ui/tenant_context.py`、
> `ui/static/device-credential.js`、`ui/static/report-client.js`）。
> **脱敏要求：本文及工单/日志中不得出现任何真实 token、密码、设备凭证明文。**

## 1. 概念与账号模型

```text
主账号（Account，一个实验室组织）
  ├─ 主账号用户（parent_owner）：汇总/切换/导出自己主账号下的工作区，默认只读
  └─ 工作区（Tenant）
       ├─ 子账号成员：tenant_admin（管理）/ operator（称重写）/ viewer（只读）
       ├─ 设备凭证：每台手机一把，固定绑定本工作区
       └─ 业务数据：箱子、记录、任务、队列、设置（只在本工作区内可见/唯一）
平台管理员（platform_admin）：平台侧运维，建主账号/工作区；不是工作区管理员，业务写一律 403
```

- 登录 = 全局用户名（`POST /api/login`，PC 网页）；设备 = 设备凭证（手机 App/H5）。
  同一个子账号既可网页登录，也可在手机上换成设备凭证。
- `cage_id` / `record_id` 只在**工作区内唯一**；两个实验室用相同箱号互不影响。
- `project_id` 仍只是任务标签，与数据归属无关；隔离键是 `tenant_id`（服务端解析，客户端传什么都不算数）。

## 2. 开通一个新实验室（平台管理员操作）

1. 建主账号（可选自带 owner 用户）：
   `POST /api/control/accounts`（body：`name`，可选 `owner_username` / `owner_password`）。
2. 建工作区：`POST /api/control/accounts/{account_id}/tenants`（body：`name`，可选 `slug`）。
3. 加子账号成员：`POST /api/control/tenants/{tenant_id}/members`
   （body：`username` + `role`（`tenant_admin` / `operator` / `viewer`）+ `display_name`；
   用户不存在时同一请求内创建，需附 `password`（≥8 位）作为初始密码）。
4. 设备绑定：见下一节（工作区管理员自助完成）。

要点：

- 新登录会话若**恰好只有一个** active 工作区会自动激活（旧「登录即可用」体验）；多个工作区时在
  `/pc` 顶栏切换器显式选择（`POST /api/control/session/tenant`）。
- 平台管理员一律**不自动激活**；全域视图用「工作区总览」（`GET /api/account/summary`），进入具体
  工作区需要是该工作区成员。
- 主账号默认**只读**：可看汇总（`GET /api/account/summary`）、跨工作区导出 CSV/XLSX
  （`GET /api/account/export`，行带 tenant_id/tenant_name 两列）、以只读身份进入工作区；
  v1 无代管写。
- 工作区重置：`POST /api/tenants/{tenant_id}/reset`（tenant_admin/平台），只删本工作区数据；
  旧的全局 `POST /api/reset` 已永久 403。

## 3. 设备绑定（云版手机）

手机首次打开（或凭证失效后）出现全屏「绑定工作区」引导页，两种方式任选：

### 方式 A：绑定码（推荐现场使用）

1. 工作区 `tenant_admin` 在 `/pc` 生成一次性绑定码：
   `POST /api/control/tenants/{tenant_id}/bind-codes`
   （TTL 最长 600 秒，默认 300 秒；**单次消费**，绑定成功即作废）。
2. 把绑定码以任意安全渠道发给现场。
3. 手机绑定页选「绑定码」输入 → `POST /api/control/devices/bind` → 返回设备凭证
   （`device_id` / `tenant_id` / `tenant_name` / `token`，**明文只出现这一次**，存手机本地
   `localStorage` 键 `mv.deviceCredential.v1`）。

### 方式 B：子账号登录换凭证

1. 手机绑定页选「子账号登录」，输入用户名/密码 → `POST /api/control/devices/login`。
2. 限制：
   - 只有 **operator / tenant_admin** 成员能绑定（设备是写身份）；viewer 与主账号 403；
   - 首次登录未改密（must_change_password）→ 403，先在网页改密；
   - 账号属于**多个**工作区 → 返回 400 + `detail.tenants[]` 列表，需选择要绑定的工作区；
   - 与网页登录同款 IP 限速：5 分钟内失败 5 次 → 429。

### 日常语义

- 一台设备 = 一个工作区。凭证**不能改绑**到别的工作区；要换 = 轮换或撤销后重绑。
- 轮换：`POST /api/control/devices/{device_id}/rotate`（tenant_admin/平台操作；签发新凭证 +
  撤旧凭证原子完成，旧凭证立即 401）。
- 撤销：`DELETE /api/control/devices/{device_id}`（单台撤销，如设备丢失）。
- 换账号/换设备：在旧设备设置页「退出绑定」清本地凭证（不影响服务端），再按方式 A/B 重新绑定。

## 4. 手机端凭证与数据队列（outbox）状态含义

设置页 / 草稿箱（`/mobile` 设置 → 草稿箱）呈现的状态：

| 状态 | 含义 | 处理 |
|------|------|------|
| 待上传（N 批） | 已确认的称重批次在本机队列，等网络 | 自动补传；可点「立即重传全部」 |
| 上传失败（死信） | 确定性失败（如载荷校验 400/413/422）的批次，留在死信区，数据未丢 | 修复网络/凭证后可重传；死信与主队列同键分工作区存储（`<队列键>.dead`），互不混合 |
| 「上一工作区还有 N 条未上传」 | 换绑/换账号前留在**旧工作区队列键**里的批次 | **不会被自动迁移/上传**；要传完需回到原工作区凭证 |
| 「凭证校验失败 / 请重新绑定」 | 启动校验返回 401/403（见下节） | 重新走绑定流程 |

队列按工作区分键（`mv.reportOutbox.v2.<tenant_id>`），每个批次入队时固化
`{tenant_id, credential_id}` 快照；上传前校验当前凭证与快照一致，不一致**整轮拒绝、原队列保留**
（0 网络请求）——所以换错凭证不会把 A 实验室的草稿传进 B 实验室。

## 5. revoked 凭证重绑（设备丢失 / 人员变动 / 轮换）

1. 触发：平台或 tenant_admin 撤销/轮换了该设备凭证（`DELETE /api/control/devices/{id}` 或
   `…/rotate`），或绑定码被重复消费。
2. 手机端表现：下次联网启动时 `verifyCredential()` 校验返回 **401/403** → 自动清除本地凭证、
   弹出绑定引导页并提示「设备凭证已被撤销，请重新绑定工作区」。**本地已称重的草稿不会丢**，
   留在原工作区队列键里。
3. 重绑：按 §3 方式 A（让 tenant_admin 现场发新绑定码）或方式 B。绑定成功后：
   - 新凭证指向（轮换场景）同一工作区 → 旧队列键与凭证匹配，队列继续排空；
   - 绑定到**另一**工作区 → 旧工作区队列保持原样，设置页显示「上一工作区还有 N 条未上传」。
4. 离线期间的撤销不会立刻反映：手机恢复联网后第一次校验即感知并回到绑定页。

## 6. 常见 401 / 403（及其他）语义速查

| 现象（接口/场景） | 状态码 | 含义与处理 |
|------|------|------|
| 手机请求任意业务 API，无/错设备凭证 | 401 | 凭证缺失、写错或已被撤销 → 重新绑定（§5） |
| 设备凭证有效，但访问别的接口语义越权 | 403 | 设备只有 operator 级写权，无管理角色；属预期 |
| `POST /api/control/devices/login` 密码错 | 401 | 用户名或密码错误；连续失败触发 429 限速 |
| `POST /api/control/devices/login` viewer/主账号 | 403 | 设备凭证只发给 operator/tenant_admin |
| `POST /api/control/devices/login` 未改密账号 | 403 | 先在网页登录改密（`POST /api/me/password`） |
| `POST /api/control/devices/login` 多工作区 | 400 | body 带 `detail.tenants[]`，选择 tenant_id 重试 |
| 网页登录后业务 API 403 | 403 | 会话未激活工作区（0/多个工作区、平台账号）→ 顶栏切换器选择；或角色只读（viewer/主账号） |
| 跨工作区猜 URL（记录/箱子/任务） | 404 | 有意折叠为 404（不泄露存在性）；换回正确工作区即可 |
| `POST /api/reset`（旧全局端点） | 403 | 已永久退役；用 `POST /api/tenants/{tenant_id}/reset` |
| 带 `X-MouseVision-Token` 的旧共享令牌请求 | 200 + 响应头 `X-MV-Deprecated-Token: 1` | 过渡期 legacy 令牌，固定落到 `legacy-default` 工作区；尽快升级 APK / 改用设备凭证 |
| 环境未配置 `MOUSEVISION_API_TOKEN` 时的旧脚本 | 401 | open mode 已关闭（fail-closed）；配置令牌或改用设备凭证 |
| 写请求返回「跨站写请求被拒绝」 | 403 | CSRF 同源校验：网页写请求的 Origin/Referer 必须与站点 Host 一致（反代改写 Host 时检查） |

## 7. 过渡期与旧 APK

- 旧版云 APK（内嵌共享令牌）仍可上传，但数据只会进入 `legacy-default` 工作区，且每个响应带
  deprecation 标记。
- 升级路径：现场换装新版 cloud APK（不内嵌任何令牌）→ 首次打开走 §3 绑定 → 绑定到真实工作区。
  旧 APK 手机上未传完的 v1 队列会在 legacy 身份下继续排空，不会静默迁入新工作区。
- 确认所有旧设备升级、legacy 队列排空后，运营侧再撤销服务器的 `MOUSEVISION_API_TOKEN` 环境变量
  （撤销动作单独记录时间与影响面，见 [DEPLOYMENT.md](./DEPLOYMENT.md)）。

## 8. 相关文档

- [PC_ADMIN_PLATFORM.md](./PC_ADMIN_PLATFORM.md)：两层账号模型、角色矩阵、主账号汇总页
- [MOBILE_WEB_APP_DESIGN.md](./MOBILE_WEB_APP_DESIGN.md) §13：云版租户隔离与 outbox v2 技术细节
- [DEPLOYMENT.md](./DEPLOYMENT.md)：迁移上线流程与设备绑定运营流程
