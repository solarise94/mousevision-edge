# MiceAutomatic · 小鼠称重

手机 App + **K797 系列蓝牙天平**，自动记录实验小鼠体重：每只小鼠放上秤盘，App 通过蓝牙实时读取重量、录像留证并抓拍确认瞬间照片，完成一箱后汇总保存 / 上报。

> 本仓库早期为视觉称重 PoC（MouseVision Edge：LCD OCR + 曲线回溯）。**当前产品判定已全部切换为 K797 系列蓝牙天平读数**，视觉管线代码保留在 `mousevision/`（服务端视频抽帧、历史分析工具仍使用）。

## 方案与架构

```
K797 系列天平 ──蓝牙广播(无配对)──▶ Android App（WebView 壳 + 原生 BLE 扫描）
                                    │  H5 录制界面：读数判定 · 录像 · 抓拍 · 断网不丢
                                    ├─ 内测云版：outbox 离线队列 ──▶ POST /api/records/report
                                    └─ 公众离线版：数据存本机，导出 CSV / JSON
                                                        │
                          服务端 FastAPI（ui/）── 箱子管理 · 记录汇总 · PC 管理后台 /pc
```

- **称重判定**：三种记录方式——即时报数（稳定锁定 + 语音报数 + 人工确认）、后匹配（连续自动记录、事后审核）、手动（人眼判定点按录入）。每确认一只即落盘，中断可恢复。
- **天平接入**：只监听广播、不做 GATT 连接与配对；按协议签名识别 **K797 整个系列**（同系列不同个体/量程均可）。支持的型号由 `android/app/src/main/assets/scale_profiles.json` 配置驱动（签名/字节序/量程），需要兼容其他电子秤时反馈型号/广播数据即可新增 profile，无需改代码。
- **离线优先**：称重过程无需联网；记录先入本机队列，联网后自动补传，服务端按 `record_id` 幂等去重。
- **证据链**：每次确认抓拍照片（最长边 ≤1280px）+ 整箱录像；上传失败进死信，保留服务端具体错误，可手动重传。

## 下载与使用（公众离线版）

- 下载：[GitHub Releases](https://github.com/solarise94/mousevision-edge/releases)（`miceautomatic-local-v0.3.4.apk`，Android 8.0+，数据仅存本机）
- 使用说明：[docs/USER_GUIDE.md](docs/USER_GUIDE.md)——用什么秤、怎么连接、怎么称重、怎么导出
- 内测云版（数据直传服务器）不在 Release 分发，仅实验室内部渠道。

## Web 端入口（服务端）

| 入口 | 路径 | 用途 |
|------|------|------|
| 手机 H5 | `/mobile` | 称重录制（与打包 App 同一份 H5） |
| 电脑管理后台 | `/pc` | 数据总览、核对、导出、箱子/用户/日志管理 |
| 手机上报 API | `/api/records/report` | 称重记录汇聚（multipart：records + photos + video） |
| 公众共享 API | `/api/records/share` | 离线版「共享数据以改善应用」匿名上传通道 |

管理后台首次启动自动创建 `admin` 账号：设 `MOUSEVISION_ADMIN_PASSWORD` 则用该密码，否则生成一次性随机密码打印到日志；首次登录强制改密。浏览器直接访问 `/mobile` 使用相机需要 HTTPS（打包 App 不受影响）。

## 开发

```bash
# 服务端（Python 3.12）
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                                # 服务端测试（部分历史视觉测试依赖自备的 RefVideo/ 视频）
python -m ui.app                      # 起服务：http://127.0.0.1:8766/

# H5（零依赖，node 测试）
node --test tests/h5/

# Android（JDK17 + 本地 SDK，见 android/README.md）
JAVA_HOME=/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home \
ANDROID_HOME=$PWD/android-sdk \
.toolchain/gradle-8.11.1/bin/gradle -p android :app:assembleLocalRelease --no-daemon
# flavor：cloud（内测）/ local（公众离线版）；H5 资产由 syncH5Assets 构建期自动打包

# 部署（VM · podman/quadlet）
# 见 docs/DEPLOYMENT.md（镜像 :deploy 标签、--no-cache、rsync 备选等坑位记录）
```

## 文档索引

- [使用说明（离线版）](docs/USER_GUIDE.md) — 面向用户：设备要求、连接天平、称重流程、数据导出
- [部署](docs/DEPLOYMENT.md) — VM 部署链路与已踩坑记录
- [移动端设计](docs/MOBILE_WEB_APP_DESIGN.md) / [PC 管理端](docs/PC_ADMIN_PLATFORM.md)
- [K797 BLE 接入方案](docs/HARMONYOS_K797_BLE_INTEGRATION_PLAN.md) — 协议细节（广播格式、重量字节）
- 历史视觉方案：[LCD OCR 服务](docs/LCD_OCR_SERVICE.md)、[算法评审](docs/REVIEW_ALGORITHM_ROBUSTNESS.md) 等

## 输出格式（上报记录）

```json
{
  "cage_id": "C57-023",
  "ordinal": 1,
  "weight_g": 21.4,
  "run_id": "…",
  "record_id": "…",
  "recorded_at": "2026-08-14T10:30:21",
  "weight_source": "ble_k797",
  "photo": "photo.jpg"
}
```
