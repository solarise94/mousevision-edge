# H5 前端测试（蓝牙天平桥接 scale-bridge.js）

零依赖，使用 Node 内置的 `node:test` 运行器。要求 Node ≥ 18。

## 运行

在仓库根目录执行（任选其一）：

```bash
# 显式指定文件（Node 18+ 通用）
node --test tests/h5/scale-bridge.test.mjs

# 或用 glob 匹配该目录下所有用例
node --test 'tests/h5/**/*.test.mjs'
```

## 覆盖范围

- `detectNativeBridge`：无桥 / 方法不全 → false；三方法齐全 → true
- 读数形状校验：非法 grams/raw/sequence 与非对象 detail 被丢弃
- 真实零点：grams=0 / raw=0 是合法读数
- 乱序 / 重复 sequence 丢弃并计入 `droppedOutOfOrder`
- stale 翻转：默认 10s 内无读数 → stale；收到读数恢复
- 原生异常态（bluetooth_off 等）无新鲜读数时立即 stale
- `createLatestOnlySender`：仅保留最新一条，`flush()` 恰好发一条
- `formatScaleDisplay`：无读数/stale → "--"；raw=0 → "0.0"；26.3 → "26.3"
- `buildScaleReadingMessage`：字段与固定 source、client_ts_ms 非负截断
- start/stop：挂载/卸载 window 监听并调用原生 startScaleScan/stopScaleScan

被测文件：`ui/static/scale-bridge.js`（UMD，浏览器挂 `window.ScaleBridge`，
Node 下经 `require` 加载）。
