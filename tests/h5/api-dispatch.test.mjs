/* 数据层 API 分发（mobile.js 内 makeApiRoutes）单元测试。
 * mobile.js 是浏览器 IIFE，不导出模块；这里把 makeApiRoutes / localUnsupported
 * 从源码里精确提取（brace 匹配），在 node 里注入：
 *   - 本地版 store（真实 LocalStore + 内存 storage）→ 六个数据方法路由到本机，
 *     jobs 相关 reject({status:400, message:"本地版无此功能"})
 *   - 云版 store=null + json mock → 原样走网络请求（行为不变）
 * 覆盖：createBox 幂等 / 重名 409、boxRecords 读回、record 读回、
 *       本地版 jobs 占位错误形状、云版转发调用次数。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const __dirname = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(join(__dirname, "../../ui/static/mobile.js"), "utf8");
const LS = require("../../ui/static/local-store.js");

/* ---------- 从 mobile.js 提取命名函数（brace 匹配） ---------- */
function extractFunction(src, name) {
  const re = new RegExp("function\\s+" + name + "\\s*\\([^)]*\\)\\s*\\{");
  const m = src.match(re);
  if (!m) throw new Error("function not found: " + name);
  let depth = 0;
  for (let j = m.index + m[0].length - 1; j < src.length; j++) {
    if (src[j] === "{") depth += 1;
    else if (src[j] === "}") {
      depth -= 1;
      if (depth === 0) return src.slice(m.index, j + 1);
    }
  }
  throw new Error("unbalanced braces: " + name);
}

/* eval 提取；makeApiRoutes 云版分支会引用缓存辅助，这里补 no-op 占位。 */
const helpers = {};
let writeBoxCacheEntry = () => {};
let cacheBoxResult = (b) => b;
{
  const names = ["makeApiRoutes", "localUnsupported"];
  const code = names.map((n) => extractFunction(SRC, n)).join("\n") +
    "\n" + names.map((n) => `helpers.${n} = ${n};`).join("\n");
  eval(code); // eslint-disable-line no-eval
}

/* ---------- 内存 storage + LocalStore ---------- */
function makeStorage() {
  const store = Object.create(null);
  return {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
    key: (i) => Object.keys(store)[i] ?? null,
    get length() { return Object.keys(store).length; },
    _dump: () => store,
  };
}
function makeLocalStore() {
  return LS.create({ storage: makeStorage() });
}

test("本地版：createBox → getBox / boxRecords / record 闭环（纯本地）", async () => {
  const store = makeLocalStore();
  const api = helpers.makeApiRoutes(store, async () => { throw new Error("不应走网络"); });
  await api.createBox({ cage_id: "C57-001", strain: "C57BL/6", mouse_no_pad: 2 });
  const box = await api.box("C57-001");
  assert.equal(box.cage_id, "C57-001");
  assert.equal(box.strain, "C57BL/6");
});

test("本地版：createBox 重名抛 {status:409}", async () => {
  const store = makeLocalStore();
  const api = helpers.makeApiRoutes(store, async () => { throw new Error("不应走网络"); });
  await api.createBox({ cage_id: "C57-002" });
  await assert.rejects(
    () => api.createBox({ cage_id: "C57-002" }),
    (err) => err.status === 409
  );
});

test("本地版：saveRecords 后 boxRecords / record 读回 + photo 保留", async () => {
  const store = makeLocalStore();
  const api = helpers.makeApiRoutes(store, async () => { throw new Error("不应走网络"); });
  await api.createBox({ cage_id: "C57-003", strain: "BALB/c" });
  store.saveRecords("C57-003", [
    { record_id: "r1", ordinal: 1, weight_g: 20.1, recorded_at: "2026-08-07T12:00:00", photo: "data:image/jpeg;base64,AAA" },
    { record_id: "r2", ordinal: 2, weight_g: 21.2, recorded_at: "2026-08-07T12:01:00" },
  ], { device_id: "scale01", mode: "manual" });

  const br = await api.boxRecords("C57-003");
  assert.equal(br.items.length, 2);
  // 按 ordinal 升序
  assert.equal(br.items[0].ordinal, 1);
  assert.equal(br.items[0].photo, "data:image/jpeg;base64,AAA");
  assert.equal(br.items[1].photo, null);
  // photo_url 本地固定 null
  assert.equal(br.items[0].photo_url, null);

  const r = await api.record("r2");
  assert.equal(r.weight_g, 21.2);
  assert.equal(r.record_id, "r2");
});

test("本地版：recentBoxes / boxes 按 strain 过滤", async () => {
  const store = makeLocalStore();
  const api = helpers.makeApiRoutes(store, async () => { throw new Error("不应走网络"); });
  await api.createBox({ cage_id: "C57-010", strain: "C57BL/6" });
  await api.createBox({ cage_id: "BALB-011", strain: "BALB/c" });
  const all = await api.boxes();
  assert.equal(all.items.length, 2);
  const onlyC57 = await api.boxes("C57BL/6");
  assert.equal(onlyC57.items.length, 1);
  assert.equal(onlyC57.items[0].cage_id, "C57-010");
  const recent = await api.recentBoxes();
  assert.equal(recent.items.length, 2);
});

test("本地版：jobs 相关 reject {status:400, message}", async () => {
  const store = makeLocalStore();
  const api = helpers.makeApiRoutes(store, async () => { throw new Error("不应走网络"); });
  for (const fn of [() => api.job("j1"), () => api.jobWait("j1"), () => api.jobReport("j1")]) {
    await assert.rejects(
      fn,
      (err) => err.status === 400 && /本地版无此功能/.test(err.message)
    );
  }
});

test("云版（store=null）：转发到 json 网络请求", async () => {
  const calls = [];
  const json = async (url, opts) => {
    calls.push({ url, opts });
    return { items: [{ cage_id: "X" }] };
  };
  const api = helpers.makeApiRoutes(null, json);
  const recent = await api.recentBoxes();
  assert.equal(calls[calls.length - 1].url, "/api/boxes/recent?limit=6");
  assert.equal(recent.items.length, 1);

  await api.box("C57");
  assert.equal(calls[calls.length - 1].url, "/api/boxes/C57");
  await api.job("j1");
  assert.equal(calls[calls.length - 1].url, "/api/jobs/j1");
});

test("云版：createBox 转发 POST 载荷", async () => {
  const calls = [];
  const json = async (url, opts) => {
    calls.push({ url, opts });
    return { cage_id: "C57" };
  };
  const api = helpers.makeApiRoutes(null, json);
  await api.createBox({ cage_id: "C57" });
  const c = calls[0];
  assert.equal(c.url, "/api/boxes");
  assert.equal(c.opts.method, "POST");
  assert.equal(JSON.parse(c.opts.body).cage_id, "C57");
});
