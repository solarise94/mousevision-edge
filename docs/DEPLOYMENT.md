# MouseVision Edge 部署备忘

> 防止忘记多层转发结构，每次部署/排障前先读这个文件。

## 外部访问入口

```
https://weight.pingoodmice.top:16206/
```

- 管理端（桌面）：`https://weight.pingoodmice.top:16206/`（云版 PC 管理台在 `/pc`）
- 手机端：`https://weight.pingoodmice.top:16206/mobile`
- 健康检查：`https://weight.pingoodmice.top:16206/api/health`（只返回 `{ok, service}`，无业务计数）
- API Token：**不写入本文件**（2026-08-14 已轮换；真值在 VM `~/.config/containers/systemd/mousevision.container` 的 `MOUSEVISION_API_TOKEN=`）。
  **租户隔离升级后的语义**：该令牌只是**过渡期 legacy 令牌**，请求被固定映射到 `legacy-default`
  工作区（响应带 `X-MV-Deprecated-Token: 1`）；`MOUSEVISION_API_TOKEN` 未配置时写接口一律 401
  （open mode 已 fail-closed 关闭）。云版设备的常规访问走**设备凭证**（`mvdev_`，见下文
  「云版设备绑定运营流程」），不再使用共享令牌。

## 完整转发链路（三层）

不要只看一层就动手改端口，三层是独立的：

```
外部浏览器
  ↓ HTTPS :16206
frps（公网 frp 服务端，不用在意在哪）
  ↓ TCP 转发 remote_port 16206 → local_port 18766
frpc（VM 上 ~/frp/frpc.../frpc.ini，[weight_web] 段）
  ↓ 转发到 127.0.0.1:18766
nginx（VM，监听 18766 SSL）
  ↓ proxy_pass http://127.0.0.1:8767
  ↓ 配置文件: /etc/nginx/conf.d/weight.pingoodmice.top.conf
podman 容器（PublishPort 8767:8766）
  ↓ 容器内 :8766
uvicorn app（python -m ui.app）
```

### 各层端口对应

| 层 | 端口 | 谁监听 | 说明 |
|---|---|---|---|
| 外部入口 | 16206 | frps（公网） | 浏览器访问这个 |
| frpc 转发 | 18766 ← 16206 | frpc（VM） | `frpc.ini [weight_web]` |
| nginx SSL | 18766 | nginx（VM） | SSL 终止 + 反代 |
| podman 宿主 | 8767 | pasta（rootless） | `PublishPort=8767:8766` |
| 容器内 | 8766 | uvicorn | app 实际监听 |

**关键：podman 必须绑 8767（不是 18766，那会被 nginx 占）。** 上一轮部署曾误改成 18766 导致端口冲突。

## VM 信息

- 主机：`vm-host`（Ubuntu 26.04, 16C/14G）
- LAN SSH：`ssh vm-lan`（LAN_VM_IP，有时不可达）
- FRP SSH：`ssh vm-user`（经 frp，稳定可达）
- sudo 密码：**不写入本文件**（2026-08-14 已轮换，存本地密码管理器）

## 目录结构（租户隔离后）

数据卷（容器内 `/app/output`）为**两层布局**：控制面全站一份，业务数据按工作区分目录。

```
~/mousevision/
├── app/                    # 代码（git 仓库，remote = github.com/solarise94/mousevision-edge）
├── data/                   # 数据卷（挂载到容器 /app/output，持久化）
│   ├── control/            # 控制面（全站一份）
│   │   ├── control.db      # accounts/tenants/users/memberships/sessions/设备凭证/绑定码
│   │   └── audit.db        # 控制面 + 跨租户审计（每条带 tenant_id/account_id/actor_type）
│   ├── tenants/
│   │   └── <tenant_uuid>/  # 每个工作区一个目录
│   │       ├── boxes.db · jobs.db · records_meta.db · upload_queue.db
│   │       ├── settings.json · mice_registry.json
│   │       ├── job_uploads/
│   │       ├── run_<ts>_<id>/          # 权威记录数据 run_*/mouse_*/record.json
│   │       └── scale_sync.db · scale_sync/
│   ├── shared/             # 本地版公众共享通道数据（share token 专用，不属于任何工作区）
│   ├── scale_captures/     # 平台/研发 BLE 捕获（platform_tool，全局根，不按子账号开放）
│   └── users.db            # 仅迁移前存在：旧全局账号库；迁移后不再读取，留在只读备份
├── logs/
└── update.sh
```

- 所有业务数据（箱子/记录/任务/队列/设置/序号计数器）都以 `tenant_id` 为隔离键；
  `cage_id`、`record_id` 等**只在租户内唯一**，跨租户汇总不要假设全局唯一。
- `project_id` 仍只是任务标签，不承担隔离。
- `POST /api/reset`（全局清盘）已删除，保留为永久 403；租户级重置用
  `POST /api/tenants/{tenant_id}/reset`，只删该租户目录。

## 容器管理

容器走 **Quadlet**（podman 5.x systemd 集成），不是 compose 长驻：

- Quadlet 配置：`~/.config/containers/systemd/mousevision.container`
- LCD OCR（可选独立服务）：`mousevision-lcd-ocr.container` + `mousevision.network`（见 `deploy/quadlet/`、`docs/LCD_OCR_SERVICE.md`）
- systemd 服务：`mousevision.service`（`systemctl --user`）
- linger 已启用（VM 重启后自动拉起）
- 容器 kill 后 systemd 自愈（Restart=always）

主分析容器访问 OCR 必须用**同一网络的服务名**（例如 `http://mousevision-lcd-ocr:8768`），不要用主容器内的 `127.0.0.1:8768`。

### 称重主路径：Agent（推荐）

生产优先用 **整段视频** `gemini-3-flash`（CPA / homePC `http://agent.invalid:46450`），不再依赖七段硬匹配作为唯一读数源：

```bash
# 写入 ~/.config/containers/systemd/mousevision.container 的 Environment=
Environment=MOUSEVISION_WEIGHT_READER=agent
Environment=MOUSEVISION_AGENT_BASE_URL=http://agent.invalid:46450
Environment=MOUSEVISION_AGENT_API_KEY=<cpa-key>
Environment=MOUSEVISION_AGENT_MODEL=gemini-3-flash
Environment=MOUSEVISION_RETAIN_SOURCE_VIDEO=1
Environment=MOUSEVISION_VIDEO_BACKEND=ffmpeg
```

- 默认 **原片** 送 agent；仅超 `max_upload_bytes` 时轻压（≥8fps，禁止默认 1fps）。
- 分析后在 `run_*/source.*` **硬链/复制**完整源视频，供训练长留存；`job_uploads` 仍 14 天 prune，**不会**删 `run_*/source.*`。
- 七段 `http_ocr` 仅作可选 fallback（yaml `agent.fallback`）或离线对照。
- 参考：`deploy/quadlet/mousevision-edge.env.snippet`。

```bash
# 状态
systemctl --user status mousevision.service
podman ps

# 日志
podman logs -f mousevision-edge

# 重启
systemctl --user restart mousevision.service
```

**镜像标签坑（2026-08 踩过）**：Quadlet `~/.config/containers/systemd/mousevision.container`
的 `Image=` 指向 **`mousevision-edge:deploy`**，不是 `dev`！`podman build -t
mousevision-edge:dev` 只会更新 dev 标签，重启后服务仍用旧的 deploy 镜像（表现为
"代码 rsync/拉了新，但容器里还是旧代码"）。正确流程：

```bash
podman build --no-cache -t mousevision-edge:dev -f Containerfile .
podman tag localhost/mousevision-edge:dev localhost/mousevision-edge:deploy
systemctl --user stop mousevision.service && podman rm -f mousevision-edge
systemctl --user start mousevision.service
# 验证容器真的用了新代码：
podman exec mousevision-edge grep -c "<新代码标志>" /app/ui/app.py
```

另外：VM 直连 GitHub 443 常被墙（git fetch 超时），ghproxy 网页通但 git smart-http
也被断。可靠做法是从能推 GitHub 的机器（如 Mac）`rsync` 源码过去（排除 `.git/`、
`Containerfile`、`refapp/`、`output/`、`*/build/`），Containerfile 保留 VM 本地
aliyun pip 源改动。

## 更新部署

```bash
ssh vm-user
bash ~/mousevision/update.sh
```

脚本会：`git pull` → 恢复 RefVideo/Containerfile(aliyun) → 重建镜像 → 重启服务。

## 迁移上线流程（旧数据根 → 租户布局）

工具：`tools/migrate_tenant_storage.py`（纯标准库 CLI；退出码 0 成功 / 1 差异或拒绝 / 2 参数错误）。
**只在维护窗口内执行 `activate`**；`inventory/stage/verify` 均无生产副作用（只读 source / 只写 staging）。

```bash
# 1) 盘点（只读；SQLite 以 mode=ro 打开，绝不写 source）
.venv/bin/python tools/migrate_tenant_storage.py inventory --source ~/mousevision/data --report /tmp/inventory.json

# 2) 复制到 staging（staging 必须是 source 的兄弟目录；source 不动）
.venv/bin/python tools/migrate_tenant_storage.py stage --source ~/mousevision/data \
  --staging ~/mousevision/data-v2 --legacy-tenant-id 00000000-0000-4000-8000-000000000001

# 3) 对账（两侧只读：DB 行数、run/record/photo/video 数量、record.json SHA-256、总字节、缺失与重复 ID）
.venv/bin/python tools/migrate_tenant_storage.py verify --source ~/mousevision/data \
  --staging ~/mousevision/data-v2 --report /tmp/verify.json

# 4) 维护窗口：停止写入（systemctl --user stop mousevision.service）后激活
.venv/bin/python tools/migrate_tenant_storage.py activate --source ~/mousevision/data \
  --staging ~/mousevision/data-v2 --i-understand-data-loss
```

`activate` 的防护：必须显式 `--i-understand-data-loss`；检测到激活状态文件 / source 已是租户布局 /
实例仍在写（近期 db `-wal/-shm/-journal`/锁文件，容差默认 300s，可用 `--max-instance-idle-seconds` 调整）即拒绝；内部先重跑 inventory+verify，未通过绝不切换。切换本身是原子的：旧根改名
`<name>.pre-tenant-migration-<UTC时间戳>` 并递归置只读，staging 原子就位为新根；激活状态写父目录
`<name>.migrate-tenant-state-<basename>.json`。

**回滚限制（重要）**：

- `activate --rollback --i-understand-data-loss` 只在**新根无任何新写入**时执行（扫描全部文件 mtime，
  任何晚于激活时刻的文件都会拒绝，`new_writes_detected`）；
- 激活后已有新写入时，**禁止直接切回旧根**：先 `inventory` 新根得到新写入清单，人工对账归并后再 rollback；工具不做自动合并；
- 只读备份至少保留一个发布周期，确认云端读写正常后再清理。

**账号并入（工具不搬运 users.db，属部署手册事项）**：

1. 升级后首次启动会自动 seed 平台管理员 `admin`（`MOUSEVISION_ADMIN_PASSWORD` 或启动日志随机密码，
   `must_change_password=1`），并自动创建 `legacy-default` 工作区；seed admin 同时是其 parent_owner 与
   legacy-default 的 tenant_admin。**首次登录后立即改密。**
2. 用控制面 API（`/api/control/accounts` → `/accounts/{id}/tenants` → `/tenants/{id}/members`）
   重建真实组织：建主账号/工作区/成员，旧 `users.role` 中 `admin` → `tenant_admin`，其余按原角色；
   平台角色另表保存，不让历史 admin 继续拥有清全盘能力（该能力已随全局 reset 一并删除）。
3. 旧 `users.db` / 全局 `audit.db` 被迁移工具列入 ignored 报告，最终留在只读备份中，不再被读取。
4. 激活只改数据根目录名（staging 原子就位为原名），容器数据卷挂载路径与 Quadlet 配置**无需改动**；
   激活后按上节验证 health、登录、legacy-default 兼容与新设备绑定，再做现场工作区验证
   （同箱号/同 record_id、匿名读 401、照片/视频深链、主账号汇总、子账号隔离、租户 reset）。

**生产脱敏要求保持**：本文件与所有迁移报告、日志、审计中**不得出现任何真实 token、密码、设备凭证明文**；verify/inventory 报告只含路径、计数与哈希，可安全归档。

## 云版设备绑定运营流程

云版手机以**设备凭证**访问工作区（详见 [CLOUD_ACCOUNT_GUIDE.md](./CLOUD_ACCOUNT_GUIDE.md)，此处为运营视角）：

- **发绑定码（推荐现场路径）**：工作区 `tenant_admin` 登录 `/pc` → 设备管理 → 生成一次性绑定码
  （`POST /api/control/tenants/{tenant_id}/bind-codes`，TTL ≤ 600s、默认 300s、单次消费）→
  现场手机 H5 绑定页输入（`POST /api/control/devices/bind`）→ 凭证明文只在响应出现一次，落在手机本地。
- **子账号登录换凭证**：手机绑定页直接输用户名/密码（`POST /api/control/devices/login`）；
  仅 operator/tenant_admin 可绑；一账号多工作区时需显式选择。
- **轮换/撤销**：`POST /api/control/devices/{device_id}/rotate`（签发新 + 撤旧原子）或
  `DELETE /api/control/devices/{device_id}`（单台撤销）。撤销/轮换后旧凭证立即 401，
  手机下次联网校验即清除本地凭证并回到绑定页。
- legacy 共享令牌（`MOUSEVISION_API_TOKEN`）只供旧 APK 排空存量队列；确认旧队列排空且现场 APK
  升级后即可撤销该环境变量（撤销动作单独记录时间与影响面）。

## 仓库

- GitHub：`https://github.com/solarise94/mousevision-edge`（Public）
- VM 上 `~/mousevision/app` 是 git 工作区，remote 已关联
- `RefVideo/*.mp4` 和 `compose.deploy.yaml` 被 gitignore，VM 上从备份恢复

## 排障速查

| 症状 | 排查 |
|---|---|
| 外部 16206 访问失败 | 先查 frpc 是否在跑：`ps aux \| grep frpc` |
| 18766 SSL 报错 | 查 nginx：`sudo nginx -t`、证书 `/etc/nginx/weight.pingoodmice.top.crt` |
| 8767 后端不通 | 查容器：`podman ps`、`systemctl --user status mousevision.service` |
| 容器起不来报端口占用 | 8767 被 pasta 残留占用：`podman rm -fa` 后重启 service |
| 端口冲突 18766 | nginx 占用，podman 不能绑 18766，必须是 8767 |

## 注意事项

- VM 无 curl，HTTP 测试用 `python3 -c "import urllib.request..."`
- GitHub 在 VM 上较慢，镜像构建走 daocloud + aliyun pip 源加速
- 单进程单 worker，不要 `uvicorn --workers N`
- HTML 页面**不再注入**共享 token meta（`_inject_api_token` 已删除）；手机/设备经设备凭证或
  （过渡期）legacy 令牌访问。携带会话 Cookie 的写请求有 CSRF 同源校验（Origin/Referer 与 Host 比对），
  反代改写 Host 时注意保持一致，否则 PC 写操作会 403
