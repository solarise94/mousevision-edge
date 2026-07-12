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

- **会话登录**：`POST /api/login`，Cookie `mv_session`
- **角色**：`admin` / `operator` / `viewer`
- **兼容**：写 API 仍接受 `X-MouseVision-Token`（与手机端、部署代理一致）

## 算法检查台（旧版）

原桌面回放 UI 保留在 `/legacy`，详情面板中的「回放复核」会打开该页面并触发只读 `/api/start`。
