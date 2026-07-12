# PC 管理后台与智能入口

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

- **数据管理**：筛选、KPI、Tab（全部/待核对/已发布/已删除）、卡片列表、右侧详情
- **数据总览**：KPI + 每日记录趋势
- **数据核对 / 发布管理**：基于记录生命周期的队列操作
- **导出管理**：按筛选导出 CSV / XLSX
- **箱子管理 / 小鼠管理**：对接 `/api/boxes*` 与 `/api/mice-admin`
- **用户管理 / 操作日志 / 系统设置**：需 `admin` 或对应角色

前端：`ui/static/pc/`（零构建原生 JS SPA）。

## 记录生命周期（叠加层）

体重数据仍以 `output/run_*/mouse_*/record.json` 为权威来源；生命周期状态存于 `records_meta.db`：

- `pending`（默认）
- `published`
- `deleted`（软删除，文件保留）

API：`GET /api/records`、`POST /api/records/{id}/publish` 等，见 [ui/app.py](../ui/app.py)。

## 鉴权

- **会话登录**：`POST /api/login`，Cookie `mv_session`（HTTPS 下自动 / 可通过 `MOUSEVISION_HTTPS=1` 设置 `Secure`）
- **角色**：`admin` / `operator` / `viewer`
- **强制改密**：首次 seed 的 admin 必须调用 `POST /api/me/password` 后才能访问管理 API
- **共享 token**：仅注入到 `/mobile` 与 `/legacy`，供上传/回放写接口使用；**不注入** `/`、`/pc`，且 **永不** 映射为 admin 会话
- **登录限流**：同一 IP 5 分钟内失败 5 次返回 429

## 软删除语义

`DELETE /api/records/{id}` 仅写 `records_meta.status=deleted`，磁盘文件保留。

- 默认读取（手机本箱列表、详情、照片）隐藏已删除记录
- 管理端 `tab=deleted` 或 `include_deleted=true` 才可见
- `POST /api/records/{id}/restore` 恢复为 `pending`
