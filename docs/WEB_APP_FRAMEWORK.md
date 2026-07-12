# MouseVision Web 录像、识别与报告框架

## 1. 目标

本框架将现有视频称量算法包装成手机可访问的 Web 工作流：

1. 手机浏览器调用后置摄像头录像，或使用系统相机/相册选择视频。
2. 视频通过 HTTPS 上传到后端。
3. 后端为每次上传生成独立 `job_id`，使用单 worker 串行分析。
4. 分析结果继续使用现有 `run_id / cage_id / ordinal / record_id` 数据模型。
5. 手机显示上传进度、任务状态、稳定照片、体重摘要和鼠只列表。

现有桌面检查 UI 保留在 `/`，新增手机入口为 `/mobile`。

## 2. 当前已实现

### 手机前端

- `getUserMedia()` 后置相机实时预览。
- 目标约束：720×1280、15fps、关闭音频。
- `MediaRecorder` 录制，目标码率 1.5 Mbps。
- 运行时探测 MP4/H.264 或 WebM 支持。
- 每 2 秒生成录制 chunk，避免单一超大内存块。
- 系统相机/相册 `<input capture>` 兜底。
- 上传进度、排队状态、分析状态与错误提示。
- 最近任务和基础称量报告。

浏览器不保证一定采用请求的分辨率和码率。第一阶段应以真实手机录制文件为准做压缩基准测试。

### 后端任务层

- SQLite 持久化任务状态：`output/jobs.db`。
- 上传文件目录：`output/job_uploads/<job_id>/source.*`。
- 状态流转：

  ```text
  uploading → queued → processing → completed
                                   ↘ failed
  ```

- 单 worker 串行执行现有 `WeighingPipeline`，适配 2C4G。
- 服务重启后将中断的上传/处理任务明确标记为失败；已排队任务重新入队。
- 每个任务完成后关联一个独立 `run_id`。
- 报告提供数量、平均体重、范围、平均置信度和逐只照片。
- 默认最大上传 250MB，可通过环境变量调整。

## 3. API 基本契约

### 创建上传任务

```http
POST /api/jobs
Content-Type: multipart/form-data

project_id=default
cage_id=C57-023
video=<video file>
```

成功返回 HTTP 202 和任务对象。

### 查询任务

```http
GET /api/jobs
GET /api/jobs/{job_id}
```

### 查询报告

```http
GET /api/jobs/{job_id}/report
```

### 健康检查

```http
GET /api/health
```

## 4. 数据目录

```text
output/
├── jobs.db
├── job_uploads/
│   └── <job_id>/source.mp4
├── run_<timestamp>_<id>/
│   ├── manifest.json
│   └── mouse_001/
│       ├── record.json
│       ├── curve.json
│       └── photo.jpg
├── mice_registry.json
└── upload_queue.db
```

Podman 部署时必须将 `/app/output` 挂载到持久化卷，否则重建容器会丢失视频、任务和报告。

## 5. Podman 测试

建议虚拟机资源：2 CPU、4GB 内存、至少 20GB 可用磁盘。当前实现一次只分析一个任务。

### 构建

```bash
podman build -t mousevision-edge:dev -f Containerfile .
```

### 运行

```bash
mkdir -p output

podman run --rm \
  --name mousevision-edge \
  -p 8766:8766 \
  -e MOUSEVISION_MAX_UPLOAD_MB=250 \
  -v "$PWD/output:/app/output:Z" \
  mousevision-edge:dev
```

访问：

```text
桌面管理：http://VM_IP:8766/
手机入口：http://VM_IP:8766/mobile
健康检查：http://VM_IP:8766/api/health
```

也可以使用：

```bash
podman compose up --build
```

如果系统没有 Compose provider，使用前面的 `podman build` 和 `podman run` 即可。

## 6. HTTPS 与手机摄像头

手机通过 `http://VM_IP:8766/mobile` 可以使用系统相机/相册上传，但浏览器通常不会在普通 HTTP 内网 IP 上开放 `getUserMedia()` 实时相机。

测试浏览器直接录像时，需要在应用前增加 HTTPS：

```text
手机 HTTPS
    ↓
Caddy / Nginx / 工厂现有网关
    ↓
http://mousevision:8766
```

推荐使用一个正式域名和有效证书，并通过内外网 DNS 分流：

- 工厂内解析到内网网关或 VM。
- 外网解析到受控转发服务器。
- 两边使用同一 HTTPS 域名。

## 7. 5 Mbps 公网带宽建议

网页录像目标码率为 1.5 Mbps。44 秒视频理论大小约 8.3MB，在 5 Mbps 链路上实际上传通常约 15–25 秒。

建议：

- 一次只上传一个视频。
- 管理页面只同步 JSON、截图和短片段。
- 不在公网暴露现有 MJPEG `/api/stream`；其带宽明显高于 5 Mbps。
- 原始视频优先保留内网，需要时再同步远程服务器。

## 8. 上线前必须补充

当前版本是基本框架，**不得直接裸露到公网或不可信局域网**。

### 共享 Token 的能力与边界

部署时设置环境变量 `MOUSEVISION_API_TOKEN`，手机上传等写操作（`POST /api/jobs`、`POST /api/start` 等）须在请求头携带 `X-MouseVision-Token`，或由已登录的 admin/operator 会话调用；页面 `/mobile` 与 `/legacy` 会自动注入 meta 供前端附带。未设置 token 时这些端点对会话/开放模式仍可用（仅适合本地开发）。

**清空数据** `POST /api/reset` 不接受 machine token，必须使用 **admin 会话**（在 `/pc` 登录）。

**重要边界：这个 token 不构成用户访问控制。** 它能提供的是：

- 阻止互联网扫描器随意调用写接口；
- 阻止不知道 token 的脚本误操作；
- 作为可信内网中的轻量保护。

它**不能**区分普通页面访客、操作员和管理员：因为 token 通过 `<meta>` 注入到 `/` 和 `/mobile` 的 HTML 源码里，任何能打开页面的人都能在 view-source 中读到它，进而拥有全部写权限（包括一键清空数据的 `POST /api/reset`）。

因此 token 仅适用于：**只有可信操作员能访问的实验室/工厂内网，且所有人本身即为操作员**。它不适用于公网或存在不可信访客的网络。

### 外网访问的正确做法

需要从外网管理时，**不要直接转发 8766 端口**，在前面增加真正的访问层：

```text
外部浏览器
    ↓ HTTPS + 登录 / VPN
反向代理（Nginx / Caddy / 工厂网关）
    ↓ 删除客户端 X-MouseVision-Token，认证后重新注入内部 token
MouseVision（内网）
```

推荐方案：

1. VPN / WireGuard / Tailscale：适合少量固定管理人员。
2. Nginx / Caddy + 登录认证：适合多人通过域名访问。
3. 应用自身 session/cookie 登录与角色权限：后续迭代再加。

若走反向代理认证，代理应：删除客户端自己提交的 `X-MouseVision-Token`、认证成功后由代理重新注入内部 token、使用 HTTPS。最终建议把权限分两级——操作员（上传、开始、停止、查看报告）与管理员（清空、删除任务、系统设置）。

### 单进程约束

分析队列是进程内的单线程内存队列（`queue.Queue` + 一个 daemon worker）。worker 阻塞等待任务，`stop()` 用哨兵唤醒，服务启动时从 SQLite 恢复所有 `queued` 任务。**当前实现只能在单进程下运行**——不要使用 `uvicorn --workers 4` 或多容器多实例，否则会出现任务被重复处理或队列不一致。多进程/多主机部署时，需替换为 Redis/RQ、Celery 等外部队列。

正式使用前至少需要：

1. 身份认证、项目权限和管理员/操作员角色。
2. HTTPS、CSRF 防护、限流和访问审计。
3. 视频格式探测、恶意文件校验和 FFmpeg 转码隔离。
4. 分片或断点续传，以及弱网恢复。
5. 任务取消、超时和 worker 进程隔离。
6. 数据保留期限与备份策略（mobile job 分析完成后自动删除 `job_uploads/` 源视频；run 产物与 manifest 中的 `source_id` 仍保留路径字符串供审计）。
7. 多实例部署时，将内存队列替换为 Redis/RQ、Celery 等外部队列。
8. 针对目标 Android/iPhone 机型验证 MediaRecorder 格式与码率。

## 9. 推荐迭代顺序

1. 在 Podman VM 上通过上传参考视频验证完整任务链路。
2. 配置 HTTPS 后，用目标手机测试浏览器录像。
3. 对 1.0、1.5、2.0 Mbps 视频进行 8/8 OCR 回归验证。
4. 根据工厂网络决定内网直连、VPN 或公网转发。
5. 再增加扫码、用户权限、报告导出和远程同步。
