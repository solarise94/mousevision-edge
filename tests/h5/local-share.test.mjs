/* 共享数据开关（mobile.js 内 getShareEnabled / setShareEnabled /
 * shareTokenAvailable）单元测试。
 * mobile.js 是浏览器 IIFE，不导出模块；这里用 brace 匹配精确提取命名纯函数，
 * 在 node 里独立 eval，验证：
 *   - 开关状态存取 localStorage mv.shareDataEnabled.v1（"1"/"0"，默认关）
 *   - shareTokenAvailable 仅在 MV_CONFIG.shareToken 非空时为真
 *   - setShareEnabled(false) 显式落 "0"
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(join(__dirname, "../../ui/static/mobile.js"), "utf8");

/* 从 mobile.js 提取命名函数（brace 匹配）。 */
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

function makeStorage() {
  const store = {};
  return {
    getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    _dump: () => store,
  };
}

/* 假 localStorage + 假 MV_CONFIG；eval 作用域里被提取函数引用。 */
function buildHelpers({ shareToken } = {}) {
  const storage = makeStorage();
  const localStorage = storage;
  const window = {};
  if (shareToken === null) window.MV_CONFIG = null;
  else if (shareToken !== undefined) window.MV_CONFIG = { shareToken };
  const names = ["shareTokenAvailable", "getShareEnabled", "setShareEnabled"];
  const helpers = {};
  const SHARE_STORAGE_KEY = "mv.shareDataEnabled.v1";
  const code = names.map((n) => extractFunction(SRC, n)).join("\n") +
    "\n" + names.map((n) => `helpers.${n} = ${n};`).join("\n");
  eval(code); // eslint-disable-line no-eval
  return { helpers, storage };
}

test("share toggle defaults off when storage empty", () => {
  const { helpers } = buildHelpers({ shareToken: "tok" });
  assert.equal(helpers.getShareEnabled(), false);
});

test("setShareEnabled(true) persists '1' and get returns true", () => {
  const { helpers, storage } = buildHelpers({ shareToken: "tok" });
  helpers.setShareEnabled(true);
  assert.equal(storage._dump()["mv.shareDataEnabled.v1"], "1");
  assert.equal(helpers.getShareEnabled(), true);
});

test("setShareEnabled(false) persists '0'", () => {
  const { helpers, storage } = buildHelpers({ shareToken: "tok" });
  helpers.setShareEnabled(true);
  helpers.setShareEnabled(false);
  assert.equal(storage._dump()["mv.shareDataEnabled.v1"], "0");
  assert.equal(helpers.getShareEnabled(), false);
});

test("storage '1' string is read as enabled", () => {
  const { helpers, storage } = buildHelpers({ shareToken: "tok" });
  storage.setItem("mv.shareDataEnabled.v1", "1");
  assert.equal(helpers.getShareEnabled(), true);
});

test("shareTokenAvailable is true only with non-empty shareToken", () => {
  const a = buildHelpers({ shareToken: "abc" });
  assert.equal(a.helpers.shareTokenAvailable(), true);

  const b = buildHelpers({ shareToken: "" });
  assert.equal(b.helpers.shareTokenAvailable(), false);

  const c = buildHelpers({ shareToken: null });
  assert.equal(c.helpers.shareTokenAvailable(), false);

  const d = buildHelpers({});
  assert.equal(d.helpers.shareTokenAvailable(), false);
});
