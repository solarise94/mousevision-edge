/* 离线选箱缓存（mobile.js 内 mv.boxCache.v1 + isNetworkError）单元测试。
 * mobile.js 是浏览器 IIFE，不导出模块；这里把其中的纯函数从源码里精确提取
 * （函数体 brace 匹配），在 node 环境里用注入的 localStorage 验证：
 *   - writeBoxCacheEntry / readBoxCacheEntry / cacheBoxResult：缓存写读闭环
 *     （离线选箱回退缓存后，录制/上报所需的箱号字段与线上一致）
 *   - 持久化结构 {v:1, map:{cageId:{box, cachedAt}}}、覆盖写、损坏容错
 *   - isNetworkError：网络错误（无 status）→ 走缓存；业务错误（404/5xx 有
 *     status）→ 不回退缓存（区分"断网"与"箱子不存在/服务器错误"）
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(join(__dirname, "../../ui/static/mobile.js"), "utf8");

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

/* eval 提取出的函数体；闭包引用模块作用域里的 localStorage / BOX_CACHE_KEY */
const BOX_CACHE_KEY = "mv.boxCache.v1"; // 与 mobile.js 常量保持一致
const helpers = {};
let localStorage; // 每个用例重新注入 fake（函数体闭包引用此变量）
{
  const fnNames = [
    "readBoxCache",
    "writeBoxCacheEntry",
    "readBoxCacheEntry",
    "cacheBoxResult",
    "isNetworkError",
  ];
  const code = fnNames.map((n) => extractFunction(SRC, n)).join("\n") +
    "\n" + fnNames.map((n) => `helpers.${n} = ${n};`).join("\n");
  // 直接 eval：在模块作用域求值——提取出的函数体（严格模式下函数声明不泄露到
  // 模块）闭包引用模块作用域的 localStorage / BOX_CACHE_KEY / 互相调用。
  eval(code); // eslint-disable-line no-eval
}

/* ---------- 测试辅助 ---------- */
function makeStorage() {
  const store = Object.create(null);
  return {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
    _dump: () => store,
  };
}

/* 一个录制所需字段齐全的箱子（与后端 /api/boxes/{id} 返回形状一致） */
function aBox() {
  return {
    cage_id: "C57-023",
    project_id: "default",
    strain: "C57BL/6",
    notes: "",
    mouse_no_start: 1,
    mouse_no_pad: 2,
    next_ordinal: 5,
    created_at: "2026-08-01T10:00:00",
    updated_at: "2026-08-01T10:00:00",
    qr_payload: "{\"cage_id\":\"C57-023\",\"project_id\":\"default\"}",
  };
}

/* ================================================================== *
 * 缓存写读闭环：离线选箱回退缓存后，录制字段与线上一致
 * ================================================================== */

test("cacheBoxResult: 成功后写入缓存并原样返回；离线回退能读回完整箱字段", () => {
  localStorage = makeStorage();
  const box = aBox();
  const returned = helpers.cacheBoxResult(box);
  assert.equal(returned, box, "cacheBoxResult 应原样返回 box（不改写）");

  const cached = helpers.readBoxCacheEntry("C57-023");
  assert.notEqual(cached, null);
  // 录制/上报所需字段全部保留
  assert.equal(cached.cage_id, "C57-023");
  assert.equal(cached.project_id, "default");
  assert.equal(cached.strain, "C57BL/6");
  assert.equal(cached.next_ordinal, 5);
  assert.equal(cached.mouse_no_start, 1);
  assert.equal(cached.mouse_no_pad, 2);
});

test("持久化结构：localStorage 落盘为 {v:1, map:{cageId:{box, cachedAt}}}", () => {
  localStorage = makeStorage();
  helpers.cacheBoxResult(aBox());
  const raw = localStorage.getItem("mv.boxCache.v1");
  assert.ok(typeof raw === "string", "应写入 mv.boxCache.v1");
  const parsed = JSON.parse(raw);
  assert.equal(parsed.v, 1);
  const entry = parsed.map["C57-023"];
  assert.ok(entry && typeof entry === "object", "map 应含 cageId → {box, cachedAt}");
  assert.equal(entry.box.cage_id, "C57-023");
  assert.ok(typeof entry.cachedAt === "number", "cachedAt 应为时间戳");
});

test("多箱共存 + 覆盖写：同一箱号更新、其它箱不受影响", () => {
  localStorage = makeStorage();
  helpers.cacheBoxResult(aBox());
  helpers.cacheBoxResult({ ...aBox(), cage_id: "BALB-001" });
  const c1 = helpers.readBoxCacheEntry("C57-023");
  const c2 = helpers.readBoxCacheEntry("BALB-001");
  assert.equal(c1.next_ordinal, 5);
  assert.equal(c2.strain, "C57BL/6");

  // 覆盖写同箱号：next_ordinal 更新，其它箱仍保留
  helpers.cacheBoxResult({ ...aBox(), cage_id: "C57-023", next_ordinal: 9 });
  assert.equal(helpers.readBoxCacheEntry("C57-023").next_ordinal, 9);
  assert.equal(helpers.readBoxCacheEntry("BALB-001").cage_id, "BALB-001");
});

test("readBoxCacheEntry: 未缓存的箱号返回 null", () => {
  localStorage = makeStorage();
  helpers.cacheBoxResult(aBox());
  assert.equal(helpers.readBoxCacheEntry("NO-SUCH-BOX"), null);
});

test("writeBoxCacheEntry: 非对象 / 缺 cage_id 一律忽略（不落盘）", () => {
  localStorage = makeStorage();
  const before = localStorage.getItem("mv.boxCache.v1");
  helpers.writeBoxCacheEntry(null);
  helpers.writeBoxCacheEntry(undefined);
  helpers.writeBoxCacheEntry({});
  helpers.writeBoxCacheEntry({ strain: "C57BL/6" }); // 缺 cage_id
  assert.equal(localStorage.getItem("mv.boxCache.v1"), before, "不应写入任何缓存");
  assert.equal(helpers.readBoxCacheEntry("x"), null);
});

test("损坏 / 非预期形状的缓存：容错返回空，不抛错", () => {
  localStorage = makeStorage();
  localStorage.setItem("mv.boxCache.v1", "not-json{");
  assert.equal(helpers.readBoxCacheEntry("C57-023"), null);

  localStorage.setItem("mv.boxCache.v1", JSON.stringify({ v: 1, map: "oops" }));
  assert.equal(helpers.readBoxCacheEntry("C57-023"), null);
});

/* ================================================================== *
 * isNetworkError：区分"网络错误"与"业务错误"
 * ================================================================== */

test("isNetworkError: 网络错误（无 status）→ true，走缓存", () => {
  // fetch throw 的 Error 没有 status
  assert.equal(helpers.isNetworkError(new Error("NetworkError")), true);
  // 空对象 / undefined / null（无 status）
  assert.equal(helpers.isNetworkError({}), true);
  assert.equal(helpers.isNetworkError(undefined), true);
  assert.equal(helpers.isNetworkError(null), true);
});

test("isNetworkError: 业务错误（有 HTTP status）→ false，不回退缓存", () => {
  // 404 箱子不存在 / 5xx 服务器错误：err.status 为数字 → false
  const notFound = new Error("HTTP 404");
  notFound.status = 404;
  assert.equal(helpers.isNetworkError(notFound), false);
  const serverErr = new Error("HTTP 503");
  serverErr.status = 503;
  assert.equal(helpers.isNetworkError(serverErr), false);
});

/* ================================================================== *
 * selectCage 分支语义（离线回退 vs 404 不回退）——直接验证缓存函数组合
 * ================================================================== */

test("组合语义：断网回退缓存可拿到箱，404 时不会命中缓存", () => {
  localStorage = makeStorage();
  const box = aBox();
  // 在线时缓存过该箱
  helpers.cacheBoxResult(box);

  // 断网（网络错误）→ 回退缓存：readBoxCacheEntry 能取到完整箱信息
  const offlineBox = helpers.readBoxCacheEntry("C57-023");
  assert.equal(offlineBox.cage_id, "C57-023");
  assert.equal(offlineBox.next_ordinal, 5);

  // 404（服务器明确不存在）→ 判定为业务错误（isNetworkError=false），
  // selectCage 的 404 分支不会去读缓存；即使本地还有旧缓存也不该回退。
  // 这里验证"404 不是网络错误"，selectCage 中因此不会进入回退缓存分支。
  const notFound = new Error("HTTP 404");
  notFound.status = 404;
  assert.equal(helpers.isNetworkError(notFound), false,
    "404 是业务错误：selectCage 应提示不存在/新建，而非回退缓存");
});
