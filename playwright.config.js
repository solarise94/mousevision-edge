// Playwright 配置（合同 §16-G6：最小、锁定版本、仅测试用，不接管前端构建）。
// - 随机空闲端口 + 一次性临时输出目录（MOUSEVISION_OUTPUT_DIR）。
// - webServer 由 tests/e2e/seed_and_run.py 负责：先 ControlStore 种子
//   两个 account（A: A1/A2；B: B1）+ parent/子账号 + 各租户少量记录，
//   再以单 worker uvicorn 起服务（单 worker 约束不变）。
// - 单 worker：E2E 不并行。
// @ts-check
const { defineConfig } = require("@playwright/test");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");

const ROOT = __dirname;

/** 随机挑一个真实空闲的 TCP 端口（20000-29999）。 */
function findFreePort() {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("findFreePort timeout")), 10000);
    const tryOnce = () => {
      const port = 20000 + Math.floor(Math.random() * 10000);
      const srv = net.createServer();
      srv.once("error", () => {
        /* 端口被占，换一个再试 */
        tryOnce();
      });
      srv.once("listening", () =>
        srv.close(() => {
          clearTimeout(timer);
          resolve(port);
        })
      );
      srv.listen(port, "127.0.0.1");
    };
    tryOnce();
  });
}

module.exports = (async () => {
  // Playwright 会对本文件做多次求值（主进程 + 每次 worker/测试装载）。
  // 端口与输出目录一旦选定就锁进 process.env，保证整个 run 唯一；
  // worker 进程 fork 自主进程，继承同一环境变量。
  let port = Number(process.env.MV_E2E_PORT || 0);
  if (!port) {
    port = await findFreePort();
    process.env.MV_E2E_PORT = String(port);
  }
  const outputDir =
    process.env.MV_E2E_OUTPUT_DIR ||
    path.join(os.tmpdir(), `mv-e2e-output-${Date.now()}-${process.pid}`);
  process.env.MV_E2E_OUTPUT_DIR = outputDir;
  const venvPython = path.join(ROOT, ".venv", "bin", "python");
  return defineConfig({
    testDir: path.join(ROOT, "tests", "e2e"),
    timeout: 60_000,
    expect: { timeout: 10_000 },
    workers: 1,
    fullyParallel: false,
    retries: 0,
    reporter: [["list"]],
    use: {
      baseURL: `http://127.0.0.1:${port}`,
      headless: true,
      trace: "off",
      screenshot: "only-on-failure",
    },
    webServer: {
      command: `${venvPython} ${path.join(ROOT, "tests", "e2e", "seed_and_run.py")}`,
      url: `http://127.0.0.1:${port}/api/health`,
      reuseExistingServer: false,
      timeout: 180_000,
      env: {
        ...process.env,
        MV_E2E_PORT: String(port),
        MV_E2E_OUTPUT_DIR: outputDir,
        MOUSEVISION_OUTPUT_DIR: outputDir,
        MOUSEVISION_ADMIN_PASSWORD: "e2e-admin-password",
        MOUSEVISION_API_TOKEN: "",
      },
    },
  });
})();
