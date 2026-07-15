# MouseVision Edge 部署备忘

> 防止忘记多层转发结构，每次部署/排障前先读这个文件。

## 外部访问入口

```
https://weight.pingoodmice.top:16206/
```

- 管理端（桌面）：`https://weight.pingoodmice.top:16206/`
- 手机端：`https://weight.pingoodmice.top:16206/mobile`
- 健康检查：`https://weight.pingoodmice.top:16206/api/health`
- API Token：`REDACTED`（写端点需 `X-MouseVision-Token` 头）

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
- sudo 密码：`REDACTED`

## 目录结构

```
~/mousevision/
├── app/              # 代码（git 仓库，remote = github.com/solarise94/mousevision-edge）
├── data/             # 数据卷（挂载到容器 /app/output，持久化）
│   ├── jobs.db
│   ├── boxes.db
│   ├── upload_queue.db
│   └── run_<ts>_<id>/
├── logs/
└── update.sh         # 一键更新部署脚本
```

## 容器管理

容器走 **Quadlet**（podman 5.x systemd 集成），不是 compose 长驻：

- Quadlet 配置：`~/.config/containers/systemd/mousevision.container`
- LCD OCR（可选独立服务）：`mousevision-lcd-ocr.container` + `mousevision.network`（见 `deploy/quadlet/`、`docs/LCD_OCR_SERVICE.md`）
- systemd 服务：`mousevision.service`（`systemctl --user`）
- linger 已启用（VM 重启后自动拉起）
- 容器 kill 后 systemd 自愈（Restart=always）

主分析容器访问 OCR 必须用**同一网络的服务名**（例如 `http://mousevision-lcd-ocr:8768`），不要用主容器内的 `127.0.0.1:8768`。

```bash
# 状态
systemctl --user status mousevision.service
podman ps

# 日志
podman logs -f mousevision-edge

# 重启
systemctl --user restart mousevision.service
```

## 更新部署

```bash
ssh vm-user
bash ~/mousevision/update.sh
```

脚本会：`git pull` → 恢复 RefVideo/Containerfile(aliyun) → 重建镜像 → 重启服务。

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
- token 通过 meta 注入页面源码，仅适用于可信内网（见 docs/WEB_APP_FRAMEWORK.md §8）
