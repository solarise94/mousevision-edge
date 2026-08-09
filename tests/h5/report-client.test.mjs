/* 离线称重记录上报客户端 (report-client.js) 单元测试 — node:test 内置运行器，零依赖。
 * 运行：node --test tests/h5/report-client.test.mjs
 *
 * 覆盖任务书要求的 7 个场景：
 *   1. enqueue 持久化 + reload 后队列恢复
 *   2. flush 全部成功 → 队列清空，fetchFn 收到正确 endpoint/字段/records JSON
 *   3. flush 中途网络失败 → 已发批次移除、未发批次保留（离线不丢）
 *   4. 4xx → 该批进 deadLetter 不阻塞后续
 *   5. token 头正确带上
 *   6. 'online' 事件触发自动 flush
 *   7. 退避：连续失败重试间隔指数增长（假时钟验证）
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const RC = require("../../ui/static/report-client.js");

/* ---------- 测试辅助 ---------- */

/* 内存版 storage（与 createMemoryStorage 等价，但显式构造以便控制） */
function makeStorage() {
  const store = Object.create(null);
  return {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
    _dump: () => store,
  };
}

/* 构造一个 fake fetch：
 *   - calls: 记录每次调用 {endpoint, init}
 *   - queueResponses: 按调用顺序返回的 response 对象数组（shift 取用）
 *   - 默认返回 200 + {ok:true}
 * response 形态：{ status, json: async ()=>body, _body } —— _body 供 classify 直接读。
 */
function makeFakeFetch(over) {
  const calls = [];
  const opts = Object.assign({
    responses: [],          // 显式按顺序的响应
    defaultResponse: () => ({ status: 200, _body: { ok: true, run_id: "r1", count: 1, record_ids: [] } }),
    errorOn: null,          // 第 N 次（从 0 起）抛网络错误
  }, over || {});
  let callIdx = 0;
  const fn = (endpoint, init) => {
    calls.push({ endpoint, init, idx: callIdx });
    const i = callIdx++;
    if (opts.errorOn !== null && i === opts.errorOn) {
      return Promise.reject(new Error("network down"));
    }
    if (opts.responses.length > 0) {
      const r = opts.responses.shift();
      return Promise.resolve(r);
    }
    return Promise.resolve(opts.defaultResponse());
  };
  fn.calls = calls;
  return fn;
}

/* 假 window 事件总线 + 假定时器（参照 scale-bridge.test.mjs 的 makeFakeEnv 风格） */
function makeFakeTimers() {
  const listeners = {};
  const timers = [];
  let timerIdSeq = 1;
  return {
    listeners,
    timers,
    addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
    removeEventListener: (type, fn) => {
      const arr = listeners[type];
      if (arr) listeners[type] = arr.filter((f) => f !== fn);
    },
    dispatch: (type, ev) => {
      (listeners[type] || []).forEach((fn) => fn(ev || {}));
    },
    setInterval: (fn, ms) => { const id = timerIdSeq++; timers.push({ id, fn, ms }); return id; },
    clearInterval: (id) => {
      const i = timers.findIndex((t) => t.id === id);
      if (i >= 0) timers.splice(i, 1);
    },
    // 触发最早的定时器（按 ms 升序的第一个）一次并移除
    fireFirst: () => {
      if (timers.length === 0) return null;
      timers.sort((a, b) => a.ms - b.ms);
      const t = timers.shift();
      t.fn();
      return t;
    },
  };
}

function aRecord(ordinal, grams) {
  return RC.buildRecord({ ordinal, weight_g: grams });
}

/* ================================================================== *
 * 0. buildRecord 基础
 * ================================================================== */

test("buildRecord: 自动补 record_id(uuid) 与 recorded_at(ISO8601)", () => {
  const r = RC.buildRecord({ ordinal: 1, weight_g: 25.3 });
  assert.equal(r.ordinal, 1);
  assert.equal(r.weight_g, 25.3);
  assert.ok(typeof r.record_id === "string" && r.record_id.length > 0, "record_id 应为非空字符串");
  assert.ok(/T.*Z/.test(r.recorded_at), "recorded_at 应为 ISO8601");
  assert.equal(r.weight_raw, undefined, "未提供 weight_raw 时不写入");
  assert.equal(r.clip_start_ms, undefined);
});

test("buildRecord: 透传 weight_raw / clip_*", () => {
  const r = RC.buildRecord({ ordinal: 2, weight_g: 10.0, weight_raw: 100, clip_start_ms: 1234.6, clip_end_ms: 5678 });
  assert.equal(r.weight_raw, 100);
  assert.equal(r.clip_start_ms, 1234); // 截断为整数
  assert.equal(r.clip_end_ms, 5678);
});

test("buildRecord: 非法 weight_g / ordinal 抛错", () => {
  assert.throws(() => RC.buildRecord({ ordinal: 1, weight_g: NaN }), /weight_g/);
  assert.throws(() => RC.buildRecord({ ordinal: 1.5, weight_g: 1 }), /ordinal/);
  assert.throws(() => RC.buildRecord({ weight_g: 1 }), /ordinal/);
});

test("buildRecord: record_id 全局唯一（连生成 1000 个无重复）", () => {
  const ids = new Set();
  for (let i = 0; i < 1000; i++) ids.add(RC.buildRecord({ ordinal: i, weight_g: i }).record_id);
  assert.equal(ids.size, 1000);
});

/* ================================================================== *
 * 1. enqueue 持久化 + reload 后队列恢复
 * ================================================================== */

test("enqueue: 持久化到 storage；新建 outbox 读同一 storage 后队列恢复", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch();
  const ob1 = RC.createOutbox({ storage, fetchFn, now: () => 1000 });
  const id1 = ob1.enqueue({ cage_id: "C1", strain: "C57", project_id: "P1", device_id: "D1", weight_source: "ble_k797", records: [aRecord(1, 25.3), aRecord(2, 26.0)] });
  const id2 = ob1.enqueue({ cage_id: "C2", records: [aRecord(1, 30.0)] });
  assert.equal(ob1.pending(), 2);

  // storage 已落盘（JSON 含两条）
  const raw = storage.getItem(RC.DEFAULT_STORAGE_KEY);
  assert.ok(typeof raw === "string");
  const parsed = JSON.parse(raw);
  assert.equal(parsed.queue.length, 2);
  assert.equal(parsed.queue[0].clientBatchId, id1);
  assert.equal(parsed.queue[0].batch.cage_id, "C1");

  // 模拟 reload：新建 outbox 读同一 storage（注意不复用内存队列）
  const ob2 = RC.createOutbox({ storage, fetchFn, now: () => 2000 });
  assert.equal(ob2.pending(), 2, "reload 后队列应恢复");
  const list = ob2.list();
  assert.equal(list[0].clientBatchId, id1);
  assert.equal(list[0].batch.records.length, 2);
  assert.equal(list[1].clientBatchId, id2);
  // reload 后无 videoBlobRef（视频不持久化）——通过 list 不暴露该字段验证
  assert.equal(list[0].videoBlobRef, undefined);
});

test("enqueue: onChange 回调在入队时被触发，传入最新 pendingCount", () => {
  const counts = [];
  const storage = makeStorage();
  const ob = RC.createOutbox({ storage, fetchFn: makeFakeFetch(), onChange: (n) => counts.push(n) });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] });
  ob.enqueue({ cage_id: "C1", records: [aRecord(2, 2)] });
  assert.deepEqual(counts, [1, 2]);
});

/* ================================================================== *
 * 2. flush 全部成功 → 队列清空，fetchFn 收到正确 endpoint/字段/records JSON
 * ================================================================== */

test("flush 全部成功：队列清空，fetchFn 收到正确 endpoint/字段/records JSON", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch();
  const ob = RC.createOutbox({ storage, fetchFn, token: "tok-abc", endpoint: "/api/records/report" });
  ob.enqueue({
    cage_id: "C1", strain: "C57", project_id: "P1", device_id: "D1", weight_source: "ble_k797",
    records: [aRecord(1, 25.3), aRecord(2, 26.0)],
  });
  ob.enqueue({ cage_id: "C2", records: [aRecord(1, 30.0)] });

  const res = await ob.flush();
  assert.equal(res.sent, 2);
  assert.equal(res.remaining, 0);
  assert.equal(ob.pending(), 0);

  assert.equal(fetchFn.calls.length, 2);
  // 第一批：endpoint 与字段
  const c0 = fetchFn.calls[0];
  assert.equal(c0.endpoint, "/api/records/report");
  assert.equal(c0.init.method, "POST");
  assert.equal(c0.init.headers["X-MouseVision-Token"], "tok-abc");
  // FormData 字段（node 18+ 内置 FormData.get）
  const fd0 = c0.init.body;
  assert.equal(fd0.get("cage_id"), "C1");
  assert.equal(fd0.get("strain"), "C57");
  assert.equal(fd0.get("project_id"), "P1");
  assert.equal(fd0.get("device_id"), "D1");
  assert.equal(fd0.get("weight_source"), "ble_k797");
  const recs0 = JSON.parse(fd0.get("records"));
  assert.equal(recs0.length, 2);
  assert.equal(recs0[0].weight_g, 25.3);
  assert.equal(recs0[0].ordinal, 1);
  assert.ok(typeof recs0[0].record_id === "string" && recs0[0].record_id.length > 0);
  assert.ok(/T.*Z/.test(recs0[0].recorded_at));
  // 无视频 → 不应含 video 字段
  assert.equal(fd0.get("video"), null);
});

test("flush 成功后 storage 队列被清空（持久化一致）", async () => {
  const storage = makeStorage();
  const ob = RC.createOutbox({ storage, fetchFn: makeFakeFetch() });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] });
  await ob.flush();
  const parsed = JSON.parse(storage.getItem(RC.DEFAULT_STORAGE_KEY));
  assert.equal(parsed.queue.length, 0);
});

/* ================================================================== *
 * 3. flush 中途网络失败 → 已发批次移除、未发批次保留（离线不丢）
 * ================================================================== */

test("flush 中途网络失败：第一批成功、第二批失败 → 已发移除、未发保留", async () => {
  const storage = makeStorage();
  // 第 0 次成功，第 1 次（第二批）抛网络错误
  const fetchFn = makeFakeFetch({ errorOn: 1 });
  const ob = RC.createOutbox({ storage, fetchFn });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] });
  ob.enqueue({ cage_id: "C2", records: [aRecord(1, 2)] });
  ob.enqueue({ cage_id: "C3", records: [aRecord(1, 3)] });

  const res = await ob.flush();
  assert.equal(res.sent, 1, "第一批成功");
  assert.equal(res.remaining, 2, "第二、三批保留");
  assert.equal(ob.pending(), 2);
  // 保留的是 C2、C3（按入队顺序）
  const list = ob.list();
  assert.equal(list[0].batch.cage_id, "C2");
  assert.equal(list[1].batch.cage_id, "C3");
  // storage 与内存一致（保留 2 条）
  const parsed = JSON.parse(storage.getItem(RC.DEFAULT_STORAGE_KEY));
  assert.equal(parsed.queue.length, 2);
});

test("flush 中途 5xx：视为可重试，保留该批及后续", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch({
    responses: [
      { status: 200, _body: { ok: true } },         // 第一批成功
      { status: 503, _body: { ok: false } },         // 第二批 5xx → retry
    ],
    defaultResponse: () => ({ status: 200, _body: { ok: true } }),
  });
  const ob = RC.createOutbox({ storage, fetchFn });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] });
  ob.enqueue({ cage_id: "C2", records: [aRecord(1, 2)] });
  ob.enqueue({ cage_id: "C3", records: [aRecord(1, 3)] });
  const res = await ob.flush();
  assert.equal(res.sent, 1);
  assert.equal(res.remaining, 2);
  // C2 仍保留在队首
  assert.equal(ob.list()[0].batch.cage_id, "C2");
});

test("flush 2xx 但 body.ok 非 true：保守 retry，不丢", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch({
    responses: [{ status: 200, _body: { ok: false, detail: "still processing" } }],
    defaultResponse: () => ({ status: 200, _body: { ok: false } }),
  });
  const ob = RC.createOutbox({ storage, fetchFn });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] });
  const res = await ob.flush();
  assert.equal(res.sent, 0);
  assert.equal(res.remaining, 1);
});

test("flush 无 fetch 注入且全局无 fetch：视为 retry 不丢（隔离全局 fetch）", async () => {
  // 临时屏蔽全局 fetch（保存恢复）
  const origFetch = globalThis.fetch;
  globalThis.fetch = undefined;
  try {
    const storage = makeStorage();
    const ob = RC.createOutbox({ storage });
    ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] });
    const res = await ob.flush();
    assert.equal(res.sent, 0);
    assert.equal(res.remaining, 1);
  } finally {
    globalThis.fetch = origFetch;
  }
});

/* ================================================================== *
 * 4. 4xx → 该批进 deadLetter 不阻塞后续
 * ================================================================== */

test("flush 4xx：该批进 deadLetter，不阻塞后续批次", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch({
    responses: [
      { status: 400, _body: { ok: false, detail: "bad cage_id" } }, // C1 4xx → 死信
      { status: 200, _body: { ok: true } },                          // C2 成功
      { status: 200, _body: { ok: true } },                          // C3 成功
    ],
    defaultResponse: () => ({ status: 200, _body: { ok: true } }),
  });
  const ob = RC.createOutbox({ storage, fetchFn });
  ob.enqueue({ cage_id: "BAD", records: [aRecord(1, 1)] });
  ob.enqueue({ cage_id: "C2", records: [aRecord(1, 2)] });
  ob.enqueue({ cage_id: "C3", records: [aRecord(1, 3)] });

  const res = await ob.flush();
  assert.equal(res.sent, 2, "C2/C3 成功");
  assert.equal(res.remaining, 0);
  const dl = ob.deadLetters();
  assert.equal(dl.length, 1);
  assert.equal(dl[0].batch.cage_id, "BAD");
  assert.equal(dl[0].reason, "4xx");
  assert.equal(dl[0].failedAt != null, true);
});

test("flush 413 / 422 也是确定的 payload 错误 → 进死信", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch({
    responses: [
      { status: 413, _body: { ok: false } }, // payload too large
      { status: 422, _body: { ok: false } }, // unprocessable
    ],
    defaultResponse: () => ({ status: 200, _body: { ok: true } }),
  });
  const ob = RC.createOutbox({ storage, fetchFn });
  ob.enqueue({ cage_id: "X1", records: [aRecord(1, 1)] });
  ob.enqueue({ cage_id: "X2", records: [aRecord(1, 2)] });
  await ob.flush();
  assert.equal(ob.deadLetters().length, 2, "413 和 422 都应进死信");
  assert.equal(ob.pending(), 0);
});

test("flush 401/403：归为 auth，保留在队列、停止本轮 flush、暴露 lastAuthFailed", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch({
    responses: [
      { status: 401, _body: { ok: false, detail: "invalid token" } }, // C1 → auth
    ],
    defaultResponse: () => ({ status: 200, _body: { ok: true } }),
  });
  const ob = RC.createOutbox({ storage, fetchFn });
  ob.enqueue({ cage_id: "A1", records: [aRecord(1, 1)] });
  ob.enqueue({ cage_id: "A2", records: [aRecord(1, 2)] }); // 不会被发（auth 停止本轮）
  const res = await ob.flush();
  assert.equal(res.sent, 0, "auth 失败 → 未发出任何一批");
  assert.equal(res.remaining, 2, "两批都保留在队列");
  assert.equal(ob.pending(), 2);
  assert.equal(ob.deadLetters().length, 0, "auth 不进死信（可恢复）");
  assert.equal(ob.lastAuthFailed(), true, "暴露 lastAuthFailed=true");

  // 403 同样归类为 auth
  const storage2 = makeStorage();
  const fetchFn2 = makeFakeFetch({
    responses: [{ status: 403, _body: { ok: false } }],
    defaultResponse: () => ({ status: 200, _body: { ok: true } }),
  });
  const ob2 = RC.createOutbox({ storage: storage2, fetchFn: fetchFn2 });
  ob2.enqueue({ cage_id: "B1", records: [aRecord(1, 1)] });
  await ob2.flush();
  assert.equal(ob2.pending(), 1, "403 也保留");
  assert.equal(ob2.lastAuthFailed(), true);
});

test("flush auth 失败后换 token 成功 → lastAuthFailed 复位、队列清空", async () => {
  const storage = makeStorage();
  let token = "stale";
  const fetchFn = makeFakeFetch({
    responses: [
      { status: 401, _body: { ok: false } }, // 旧 token 失败
    ],
    defaultResponse: () => ({ status: 200, _body: { ok: true } }), // 后续成功
  });
  const ob = RC.createOutbox({ storage, fetchFn, token: () => token });
  ob.enqueue({ cage_id: "A1", records: [aRecord(1, 1)] });
  await ob.flush();
  assert.equal(ob.lastAuthFailed(), true);
  // 换新 token 后重试
  token = "fresh";
  const res = await ob.flush();
  assert.equal(res.sent, 1);
  assert.equal(ob.pending(), 0);
  assert.equal(ob.lastAuthFailed(), false, "成功后 lastAuthFailed 复位");
});

test("flush 408/429/409/425：归为 retry（保留重试，不进死信也不算 auth）", async () => {
  for (const status of [408, 409, 425, 429]) {
    const storage = makeStorage();
    const fetchFn = makeFakeFetch({
      responses: [{ status, _body: { ok: false } }],
      defaultResponse: () => ({ status: 200, _body: { ok: true } }),
    });
    const ob = RC.createOutbox({ storage, fetchFn });
    ob.enqueue({ cage_id: "R", records: [aRecord(1, 1)] });
    await ob.flush();
    assert.equal(ob.pending(), 1, `status ${status} 应保留重试`);
    assert.equal(ob.deadLetters().length, 0, `status ${status} 不应进死信`);
    assert.equal(ob.lastAuthFailed(), false, `status ${status} 不应算 auth`);
  }
});

/* 死信持久化：4xx 死信批次落独立 key（<storageKey>.dead），reload 后仍可查。 */
test("死信持久化：flush 4xx 后死信落盘；新建 outbox 读同一 storage 后死信恢复", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch({
    responses: [
      { status: 400, _body: { ok: false } }, // 死信
      { status: 200, _body: { ok: true } },  // 成功
    ],
    defaultResponse: () => ({ status: 200, _body: { ok: true } }),
  });
  const ob1 = RC.createOutbox({ storage, fetchFn, now: () => 1000 });
  ob1.enqueue({ cage_id: "BAD", records: [aRecord(1, 1)] });
  ob1.enqueue({ cage_id: "OK", records: [aRecord(1, 2)] });
  await ob1.flush();
  assert.equal(ob1.deadLetters().length, 1);
  const dlId = ob1.deadLetters()[0].clientBatchId;

  // 死信已落盘到 <storageKey>.dead
  const deadRaw = storage.getItem(RC.DEFAULT_STORAGE_KEY + ".dead");
  assert.ok(typeof deadRaw === "string", "死信应落盘到 .dead key");
  const dparsed = JSON.parse(deadRaw);
  assert.equal(dparsed.dead.length, 1);
  assert.equal(dparsed.dead[0].clientBatchId, dlId);
  assert.equal(dparsed.dead[0].batch.cage_id, "BAD");

  // reload：新建 outbox 读同一 storage，死信恢复
  const ob2 = RC.createOutbox({ storage, fetchFn: makeFakeFetch(), now: () => 2000 });
  const dl = ob2.deadLetters();
  assert.equal(dl.length, 1, "reload 后死信应恢复");
  assert.equal(dl[0].clientBatchId, dlId);
  assert.equal(dl[0].batch.cage_id, "BAD");
  assert.equal(dl[0].reason, "4xx");
  // deadLetters() 返回形状不变（clientBatchId/enqueuedAt/batch/failedAt/reason）
  assert.ok(typeof dl[0].failedAt === "number");
});

test("死信持久化：损坏的 .dead key 不影响主队列恢复（保守清空死信）", async () => {
  const storage = makeStorage();
  storage.setItem(RC.DEFAULT_STORAGE_KEY + ".dead", "not-json{");
  const ob = RC.createOutbox({ storage, fetchFn: makeFakeFetch() });
  assert.equal(ob.deadLetters().length, 0, "损坏的死信存储 → 死信清空");
  assert.equal(ob.pending(), 0, "主队列不受影响");
});

/* 死信迁移顺序修复（死信永久丢失回归）：
 * 修复前 dead 分支先 shift() 把批次移出主队列再 persist()，persist 内部先写主队列
 * 成功、后写死信；若死信 key 写入失败（quota），persist 返回 false 但 flush 忽略它
 * 继续跑 → 内存死信=1、持久化主队列=0、持久化死信=0 → reload 后两边都是 0，批次永久丢失。
 * 修复：先 peek 构造死信条目并 persistDead()，成功后再 shift + persist 写主队列；
 *       persistDead 失败则回滚死信、该批留在主队列，按 retry 语义停止本轮 flush。
 */
test("死信迁移：dead key 写入失败时批次留在主队列不丢，修复后迁移成功", async () => {
  const store = {};
  let deadKeyBroken = false; // 仅在 flush 死信迁移阶段抛错
  const storage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => {
      // 仅 dead key 抛错（模拟 quota 恰好对死信 key 触发）
      if (deadKeyBroken && k === RC.DEFAULT_STORAGE_KEY + ".dead") {
        throw new Error("dead key quota");
      }
      store[k] = String(v);
    },
    removeItem: (k) => { delete store[k]; },
  };
  const fetchFn = makeFakeFetch({
    responses: [
      { status: 400, _body: { ok: false, detail: "bad" } }, // 首次 4xx → 触发死信迁移（失败）
      { status: 400, _body: { ok: false, detail: "bad" } }, // 再次 4xx → 迁移成功
    ],
    defaultResponse: () => ({ status: 200, _body: { ok: true } }),
  });
  const ob = RC.createOutbox({ storage, fetchFn, now: () => 1000 });
  ob.enqueue({ cage_id: "BAD", records: [aRecord(1, 1)] });
  const beforeBatch = JSON.parse(store[RC.DEFAULT_STORAGE_KEY]).queue[0];

  // flush 触发 400 → 死信迁移；dead key 抛错 → 迁移失败，批次留在主队列
  deadKeyBroken = true; // 仅在 flush 死信迁移时让 dead key 失败
  const res = await ob.flush();
  assert.equal(res.sent, 0, "死信迁移失败不算成功发出");
  // 内存：批次仍在主队列，死信为空（已回滚）
  assert.equal(ob.pending(), 1, "内存主队列仍含该批（未 shift）");
  assert.equal(ob.deadLetters().length, 0, "内存死信已回滚为空");
  // 持久化：主队列仍含该批（先写死信失败，未走到 shift）；死信 key 因迁移
  // 写入抛错未更新（仍是 enqueue 时 persistDead 写下的空数组）
  const pMain = JSON.parse(store[RC.DEFAULT_STORAGE_KEY]);
  assert.equal(pMain.queue.length, 1, "持久化主队列仍含该批");
  assert.equal(pMain.queue[0].clientBatchId, beforeBatch.clientBatchId);
  const pDead = JSON.parse(store[RC.DEFAULT_STORAGE_KEY + ".dead"]);
  assert.equal(pDead.dead.length, 0, "持久化死信为空（迁移写入抛错，未新增）");
  // flush 按 retry 语义停止本轮（consecutiveFailures 累加）
  assert.ok(ob.consecutiveFailures() >= 1, "迁移失败按 retry 语义累加失败计数");
  // P2 补充：死信迁移的 persistDead 直接调用失败时，状态接口不得误报成功
  assert.equal(ob.lastPersistOk(), false, "死信迁移落盘失败时 lastPersistOk=false");

  // 修复 storage（dead key 可写）→ 再次 flush（仍是 400）→ 迁移成功：进死信、主队列清空
  deadKeyBroken = false;
  const res2 = await ob.flush();
  assert.equal(res2.sent, 0, "4xx 批次不算 sent（sentCount 只计 ok）");
  assert.equal(ob.pending(), 0, "迁移成功后主队列清空");
  assert.equal(ob.lastPersistOk(), true, "迁移成功（persist 写主队列成功）后 lastPersistOk 恢复 true");
  assert.equal(ob.deadLetters().length, 1, "迁移成功后进死信");
  assert.equal(ob.deadLetters()[0].batch.cage_id, "BAD");
  // 持久化主队列已不含该批，死信已落盘
  const pMain2 = JSON.parse(store[RC.DEFAULT_STORAGE_KEY]);
  assert.equal(pMain2.queue.length, 0, "持久化主队列已移除该批");
  const pDead2 = JSON.parse(store[RC.DEFAULT_STORAGE_KEY + ".dead"]);
  assert.equal(pDead2.dead.length, 1, "持久化死信已落盘");
  assert.equal(pDead2.dead[0].clientBatchId, beforeBatch.clientBatchId);
});

test("死信迁移：死信写成功但主队列写失败 → reload 去重（该批只在死信不在队列）", async () => {
  const store = {};
  let mainBroken = false; // 仅在 shift 后写主队列时触发
  let allowMainAfterDead = true; // 控制第二次 persist 是否失败
  const storage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => {
      // 主队列 key 在死信写成功后的「shift + persist」阶段失败（模拟此时 quota 触发）
      if (k === RC.DEFAULT_STORAGE_KEY && mainBroken && !allowMainAfterDead) {
        throw new Error("main key quota after dead");
      }
      store[k] = String(v);
    },
    removeItem: (k) => { delete store[k]; },
  };
  const fetchFn = makeFakeFetch({
    responses: [
      { status: 400, _body: { ok: false } }, // 4xx → 死信迁移
    ],
    defaultResponse: () => ({ status: 200, _body: { ok: true } }),
  });
  const ob = RC.createOutbox({ storage, fetchFn, now: () => 1000 });
  ob.enqueue({ cage_id: "BAD", records: [aRecord(1, 1)] });
  const batchId = JSON.parse(store[RC.DEFAULT_STORAGE_KEY]).queue[0].clientBatchId;

  // flush：死信先落盘成功 → shift + persist 写主队列时失败
  // 此时持久化状态是「主队列还有该批（写失败未更新，仍是旧值）+ 死信也有该批」
  // —— flush 内 persist() 吞错，但批次已确认进死信，内存主队列已 shift 为空。
  mainBroken = true;
  allowMainAfterDead = false;
  const res = await ob.flush();
  assert.equal(res.sent, 0);
  // 内存：主队列已 shift（迁移逻辑走到 shift），死信有该批
  assert.equal(ob.pending(), 0, "内存主队列已 shift");
  assert.equal(ob.deadLetters().length, 1, "内存死信含该批");
  // 持久化：死信已落盘；主队列因写失败仍是旧值（含该批）—— 重复窗口
  const pDead = JSON.parse(store[RC.DEFAULT_STORAGE_KEY + ".dead"]);
  assert.equal(pDead.dead.length, 1, "持久化死信含该批");
  assert.equal(pDead.dead[0].clientBatchId, batchId);
  const pMain = JSON.parse(store[RC.DEFAULT_STORAGE_KEY]);
  // 主队列 key 仍是 enqueue 时写的旧值（含该批）—— 重复窗口存在
  assert.ok(pMain.queue.some((q) => q.clientBatchId === batchId),
    "持久化主队列仍含该批（写失败未更新，重复窗口存在）");

  // reload：新建 outbox 读同一 storage → restore 去重生效
  // 该批 clientBatchId 既在死信又在主队列，去重后只保留在死信、不重复出现在队列
  const ob2 = RC.createOutbox({ storage, fetchFn: makeFakeFetch(), now: () => 2000 });
  assert.equal(ob2.deadLetters().length, 1, "reload 后死信恢复该批");
  assert.equal(ob2.deadLetters()[0].clientBatchId, batchId);
  assert.equal(ob2.pending(), 0, "reload 后主队列去重：该批不再出现（防重复上报）");
});

test("死信迁移：正常路径（dead key 可写）行为不变——进死信、主队列清空", async () => {
  // 回归：确认修复未破坏正常死信迁移路径（与既有「死信持久化」用例互补）
  const store = {};
  const storage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };
  const fetchFn = makeFakeFetch({
    responses: [
      { status: 400, _body: { ok: false } },
      { status: 200, _body: { ok: true } },
    ],
    defaultResponse: () => ({ status: 200, _body: { ok: true } }),
  });
  const ob = RC.createOutbox({ storage, fetchFn, now: () => 1000 });
  ob.enqueue({ cage_id: "BAD", records: [aRecord(1, 1)] });
  ob.enqueue({ cage_id: "OK", records: [aRecord(1, 2)] });
  const r = await ob.flush();
  assert.equal(r.sent, 1, "OK 批成功发出");
  assert.equal(ob.deadLetters().length, 1, "BAD 批进死信");
  assert.equal(ob.pending(), 0, "主队列清空");
  // 持久化一致：主队列空、死信 1 条
  assert.equal(JSON.parse(store[RC.DEFAULT_STORAGE_KEY]).queue.length, 0);
  assert.equal(JSON.parse(store[RC.DEFAULT_STORAGE_KEY + ".dead"]).dead.length, 1);
});

/* enqueue 持久化失败抛错：storage 不可写/quota → enqueue 抛错，但批次保留在内存队列
 * （下次 persist 再试，最大化数据保留）。flush 内部的 persist 仍吞错不阻断。 */
test("enqueue 持久化失败：storage.setItem 抛错 → enqueue 抛错但批次保留在内存队列", () => {
  const storage = makeStorage();
  // 让 setItem 直接抛错（quota exceeded）。
  storage.setItem = () => { throw new Error("quota exceeded"); };
  const ob = RC.createOutbox({ storage, fetchFn: makeFakeFetch() });
  assert.throws(
    () => ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] }),
    /持久化失败/,
    "enqueue 在 persist 失败时应抛错"
  );
  // 批次保留在内存队列（不回滚）：pending=1，下次 persist 恢复后可落盘/补传
  assert.equal(ob.pending(), 1, "persist 失败 → 批次保留在内存队列（不回滚）");
  assert.equal(ob.lastPersistOk(), false, "lastPersistOk 暴露失败状态");
});

test("enqueue 持久化失败后 storage 恢复 → 下次 enqueue 落盘成功（含前一批）", () => {
  const store = {};
  const storage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };
  const ob = RC.createOutbox({ storage, fetchFn: makeFakeFetch() });
  // 第一次：setItem 抛错 → enqueue 抛错但批次留在内存队列
  storage.setItem = () => { throw new Error("quota"); };
  assert.throws(() => ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] }), /持久化失败/);
  assert.equal(ob.pending(), 1, "失败批次留在内存队列");
  // 恢复 setItem → 再 enqueue 一批，persist 把两批都落盘
  storage.setItem = (k, v) => { store[k] = String(v); };
  ob.enqueue({ cage_id: "C2", records: [aRecord(1, 2)] });
  assert.equal(ob.pending(), 2);
  assert.equal(ob.lastPersistOk(), true);
  const parsed = JSON.parse(storage.getItem(RC.DEFAULT_STORAGE_KEY));
  assert.equal(parsed.queue.length, 2, "两批都落盘（前一批被保留下次 persist 恢复）");
});

test("lastPersistOk：正常情况为 true；storage 缺失时 enqueue 抛错且 lastPersistOk=false", () => {
  // 无 storage 注入且全局无 localStorage → enqueue 必须抛错（不能静默丢）
  const origLocalStorage = globalThis.localStorage;
  Object.defineProperty(globalThis, "localStorage", { value: undefined, configurable: true });
  try {
    const ob = RC.createOutbox({ fetchFn: makeFakeFetch() });
    assert.throws(() => ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] }), /持久化失败/);
    assert.equal(ob.lastPersistOk(), false, "enqueue 持久化失败后 lastPersistOk=false");
  } finally {
    Object.defineProperty(globalThis, "localStorage", { value: origLocalStorage, configurable: true });
  }
});

test("lastPersistOk：正常 storage 时为 true", () => {
  const ob = RC.createOutbox({ storage: makeStorage(), fetchFn: makeFakeFetch() });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] });
  assert.equal(ob.lastPersistOk(), true);
});

/* 死信假落盘回归：主队列 key 可写但 dead key setItem 抛错时，
 * persist 必须返回 false（合并两次写入结果），enqueue 抛错，
 * 批次保留在内存队列——重启后死信不丢。
 * 修复前：persistDead 内部 catch 仅置 lastPersistOk=false，返回后 persist
 * 又无条件 lastPersistOk=true; return true，导致死信没写进 storage（quota 满）
 * 但批次已从主队列移除、persist 报告成功，重启后死信永久丢失。 */
test("persist 合并死信结果：主队列可写但 dead key 抛错 → persist 返回 false、enqueue 抛错、批次保留", () => {
  const store = {};
  const storage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => {
      // 仅 dead key 抛错（模拟 quota 恰好对死信 key 触发）
      if (k === RC.DEFAULT_STORAGE_KEY + ".dead") throw new Error("dead key quota");
      store[k] = String(v);
    },
    removeItem: (k) => { delete store[k]; },
  };
  const ob = RC.createOutbox({ storage, fetchFn: makeFakeFetch() });
  // 主队列 key 写成功，但 dead key 抛错 → persist 合并结果为 false
  assert.throws(
    () => ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] }),
    /持久化失败/,
    "主队列可写但死信 key 抛错 → enqueue 应抛错（persist 合并结果为 false）"
  );
  assert.equal(ob.lastPersistOk(), false, "persist 应报告 false（死信写入失败）");
  // 批次保留在内存队列（不回滚）：下次 persist 恢复后可落盘
  assert.equal(ob.pending(), 1, "批次保留在内存队列");
  // 主队列 key 已写入（persist 先写主队列成功），但整体 persist 报 false
  const raw = storage.getItem(RC.DEFAULT_STORAGE_KEY);
  assert.ok(typeof raw === "string", "主队列 key 已写入（但 persist 整体仍报 false）");
  const parsed = JSON.parse(raw);
  assert.equal(parsed.queue.length, 1, "主队列 key 里有这一批");
});

test("persistDead 返回 boolean：成功 true / 失败 false（不再 mutate lastPersistOk）", () => {
  // 死信为空时正常 storage → persistDead 写空数组也返回 true
  const store1 = {};
  const storage1 = {
    getItem: (k) => (k in store1 ? store1[k] : null),
    setItem: (k, v) => { store1[k] = String(v); },
    removeItem: (k) => { delete store1[k]; },
  };
  const ob1 = RC.createOutbox({ storage: storage1, fetchFn: makeFakeFetch() });
  ob1.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] });
  assert.equal(ob1.lastPersistOk(), true, "正常 storage 时 lastPersistOk=true");
  // 死信 key 也写入了（空数组）
  const deadRaw = storage1.getItem(RC.DEFAULT_STORAGE_KEY + ".dead");
  assert.ok(typeof deadRaw === "string", "死信 key 写入空数组");
  assert.equal(JSON.parse(deadRaw).dead.length, 0);
});

/* ================================================================== *
 * 5. token 头正确带上
 * ================================================================== */

test("token: 字符串选项 → 作为 X-MouseVision-Token 头带上", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch();
  const ob = RC.createOutbox({ storage, fetchFn, token: "secret-token-123" });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] });
  await ob.flush();
  assert.equal(fetchFn.calls[0].init.headers["X-MouseVision-Token"], "secret-token-123");
});

test("token: 函数选项 → 调用取值（支持运行时刷新）", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch();
  let current = "v1";
  const ob = RC.createOutbox({ storage, fetchFn, token: () => current });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] });
  await ob.flush();
  assert.equal(fetchFn.calls[0].init.headers["X-MouseVision-Token"], "v1");
  // 模拟 token 轮换
  current = "v2";
  ob.enqueue({ cage_id: "C2", records: [aRecord(1, 2)] });
  await ob.flush();
  assert.equal(fetchFn.calls[1].init.headers["X-MouseVision-Token"], "v2");
});

test("token: 缺省 → 从注入 document 的 meta 读取（与 mobile.js 一致）", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch();
  const fakeDoc = {
    querySelector: (sel) => (sel === 'meta[name="mousevision-api-token"]'
      ? { content: "  from-meta  " }
      : null),
  };
  const ob = RC.createOutbox({ storage, fetchFn, document: fakeDoc });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] });
  await ob.flush();
  // trim 掉两端空格
  assert.equal(fetchFn.calls[0].init.headers["X-MouseVision-Token"], "from-meta");
});

test("token: 缺省且无 document / 无 meta → 不带 token 头（不抛错）", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch();
  const ob = RC.createOutbox({ storage, fetchFn, document: { querySelector: () => null } });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] });
  await ob.flush();
  assert.equal(fetchFn.calls[0].init.headers["X-MouseVision-Token"], undefined);
});

/* ================================================================== *
 * 6. 'online' 事件触发自动 flush
 * ================================================================== */

test("online 事件：触发自动 flush，队列被清空", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch();
  const timers = makeFakeTimers();
  const ob = RC.createOutbox({
    storage, fetchFn,
    addEventListener: timers.addEventListener,
    removeEventListener: timers.removeEventListener,
    setInterval: timers.setInterval,
    clearInterval: timers.clearInterval,
    // 用微任务延迟让 online 触发的 flush 完成
  });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] });
  ob.enqueue({ cage_id: "C2", records: [aRecord(1, 2)] });
  assert.equal(ob.pending(), 2);

  ob.start();
  // start() 会立即 flush 一次（队列非空），等它完成
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(ob.pending(), 0, "start 立即 flush 清空");

  // 再入队，模拟离线期间积压
  ob.enqueue({ cage_id: "C3", records: [aRecord(1, 3)] });
  assert.equal(ob.pending(), 1);

  // 模拟网络恢复：派发 online 事件
  timers.dispatch("online");
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(ob.pending(), 0, "online 事件触发 flush 清空");
  ob.stop();
});

test("online 事件：连续失败计数被重置（网络恢复用基础间隔）", async () => {
  const storage = makeStorage();
  // 持续失败（每次调用都 5xx），让 consecutiveFailures 能累加
  const fetchFn = makeFakeFetch({
    defaultResponse: () => ({ status: 503, _body: { ok: false } }),
  });
  const timers = makeFakeTimers();
  const ob = RC.createOutbox({
    storage, fetchFn,
    addEventListener: timers.addEventListener,
    removeEventListener: timers.removeEventListener,
    setInterval: timers.setInterval,
    clearInterval: timers.clearInterval,
  });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] });
  ob.start();
  await new Promise((r) => setTimeout(r, 0));
  // 首次 flush 网络失败 → consecutiveFailures = 1（首次失败用 base 间隔）
  assert.equal(ob.consecutiveFailures(), 1);
  assert.equal(ob.nextInterval(), RC.DEFAULT_BASE_INTERVAL_MS, "首次失败 → base 间隔");

  // 再失败一次 → 2 次失败，间隔翻倍（验证退避已生效，而非恒为 base）
  await ob.flush();
  assert.equal(ob.consecutiveFailures(), 2);
  assert.ok(ob.nextInterval() > RC.DEFAULT_BASE_INTERVAL_MS, "2 次失败 → 间隔增大");

  // 派发 online → 同步重置失败计数（handler 内先把计数清 0 再 flush）
  timers.dispatch("online");
  // flush 是异步的，会在下一 tick 再次失败累加计数；这里在同步阶段断言"已重置"
  assert.equal(ob.consecutiveFailures(), 0, "online 同步重置失败计数");
  assert.equal(ob.nextInterval(), RC.DEFAULT_BASE_INTERVAL_MS);
  await new Promise((r) => setTimeout(r, 0));
  ob.stop();
});

/* ================================================================== *
 * 7. 退避：连续失败重试间隔指数增长
 * ================================================================== */

test("退避：连续失败 → nextInterval 指数增长，封顶 maxIntervalMs", async () => {
  const storage = makeStorage();
  // 始终失败
  const fetchFn = makeFakeFetch({
    responses: [],
    defaultResponse: () => ({ status: 503, _body: { ok: false } }),
  });
  const timers = makeFakeTimers();
  const base = 1000;
  const max = 8000;
  const ob = RC.createOutbox({
    storage, fetchFn,
    addEventListener: timers.addEventListener,
    removeEventListener: timers.removeEventListener,
    setInterval: timers.setInterval,
    clearInterval: timers.clearInterval,
    baseIntervalMs: base,
    maxIntervalMs: max,
  });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] });

  // 未 start 时 rescheduleRetry 不排程（started guard）
  ob.start();
  await new Promise((r) => setTimeout(r, 0));
  // start 立即 flush 失败 → consecutiveFailures=1
  assert.equal(ob.consecutiveFailures(), 1);
  assert.equal(ob.nextInterval(), base, "1 次失败 → base");

  // 触发一次重试定时器（用第一个排程的 timer）→ 又失败
  // 注意：start 的 rescheduleRetry 在 consecutiveFailures=1 时排的是 base 间隔
  // flush 失败会 reschedule 一次新间隔。这里我们直接驱动 flush 来累加失败计数，
  // 因为 fake setInterval 不会自动按时间触发。
  await ob.flush(); // 第 2 次失败
  assert.equal(ob.consecutiveFailures(), 2);
  assert.equal(ob.nextInterval(), base * 2, "2 次失败 → 2x");

  await ob.flush(); // 第 3 次
  assert.equal(ob.consecutiveFailures(), 3);
  assert.equal(ob.nextInterval(), base * 4, "3 次失败 → 4x");

  await ob.flush(); // 第 4 次
  assert.equal(ob.consecutiveFailures(), 4);
  assert.equal(ob.nextInterval(), base * 8, "4 次失败 → 8x");

  await ob.flush(); // 第 5 次（应被 max 封顶：base*16=16000 > max=8000）
  assert.equal(ob.consecutiveFailures(), 5);
  assert.equal(ob.nextInterval(), max, "5 次失败 → 封顶 maxIntervalMs");
  ob.stop();
});

test("退避：rescheduleRetry 排程的 setInterval 间隔与 nextInterval 一致", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch({
    defaultResponse: () => ({ status: 503, _body: { ok: false } }),
  });
  const timers = makeFakeTimers();
  const base = 500;
  const ob = RC.createOutbox({
    storage, fetchFn,
    addEventListener: timers.addEventListener,
    removeEventListener: timers.removeEventListener,
    setInterval: timers.setInterval,
    clearInterval: timers.clearInterval,
    baseIntervalMs: base,
    maxIntervalMs: 10000,
  });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] });
  ob.start();
  await new Promise((r) => setTimeout(r, 0));
  // start 即 flush 失败 → consecutiveFailures=1，reschedule 用 base=500
  assert.equal(ob.consecutiveFailures(), 1);
  // 当前应有一个 timer，ms === 500
  assert.equal(timers.timers.length, 1, "应有 1 个重试 timer");
  assert.equal(timers.timers[0].ms, base, "1 次失败 timer 间隔 = base");

  // 手动再触发一次失败（直接 flush）
  await ob.flush();
  assert.equal(ob.consecutiveFailures(), 2);
  // reschedule 后唯一 timer 间隔 = base*2
  assert.equal(timers.timers.length, 1);
  assert.equal(timers.timers[0].ms, base * 2, "2 次失败 timer 间隔 = 2x");
  ob.stop();
});

test("退避：成功后 consecutiveFailures 重置为 0，间隔回到 base", async () => {
  const storage = makeStorage();
  let fail = true;
  const fetchFn = makeFakeFetch({
    defaultResponse: () => fail
      ? { status: 503, _body: { ok: false } }
      : { status: 200, _body: { ok: true } },
  });
  const ob = RC.createOutbox({ storage, fetchFn, baseIntervalMs: 1000 });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] });
  await ob.flush();
  assert.equal(ob.consecutiveFailures(), 1);
  await ob.flush();
  assert.equal(ob.consecutiveFailures(), 2);
  // 现在成功
  fail = false;
  const res = await ob.flush();
  assert.equal(res.sent, 1);
  assert.equal(res.remaining, 0);
  assert.equal(ob.consecutiveFailures(), 0, "成功后重置");
  assert.equal(ob.nextInterval(), 1000);
});

/* ================================================================== *
 * start/stop 生命周期与幂等性补充
 * ================================================================== */

test("start 幂等：重复调用不会重复挂载 online 监听", () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch();
  const timers = makeFakeTimers();
  const ob = RC.createOutbox({
    storage, fetchFn,
    addEventListener: timers.addEventListener,
    removeEventListener: timers.removeEventListener,
    setInterval: timers.setInterval,
    clearInterval: timers.clearInterval,
  });
  ob.start();
  ob.start();
  assert.equal((timers.listeners["online"] || []).length, 1, "仅一个 online 监听");
  ob.stop();
  assert.equal((timers.listeners["online"] || []).length, 0, "stop 后监听已卸载");
});

test("stop 后 online 事件不再触发 flush", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch();
  const timers = makeFakeTimers();
  const ob = RC.createOutbox({
    storage, fetchFn,
    addEventListener: timers.addEventListener,
    removeEventListener: timers.removeEventListener,
    setInterval: timers.setInterval,
    clearInterval: timers.clearInterval,
  });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] });
  ob.start();
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(ob.pending(), 0);
  ob.stop();

  ob.enqueue({ cage_id: "C2", records: [aRecord(1, 2)] });
  timers.dispatch("online");
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(ob.pending(), 1, "stop 后 online 不再触发 flush");
});

test("幂等：同一 record_id 的记录重复 flush 不产生新 record_id（透传）", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch();
  const ob = RC.createOutbox({ storage, fetchFn });
  const rec = RC.buildRecord({ ordinal: 1, weight_g: 25.0 });
  const rid = rec.record_id;
  ob.enqueue({ cage_id: "C1", records: [rec] });
  await ob.flush();
  // 第一次发送的 records[0].record_id 与原 record 一致（未重造）
  const sent = JSON.parse(fetchFn.calls[0].init.body.get("records"));
  assert.equal(sent[0].record_id, rid);
});

test("list 返回浅拷贝：外部修改不影响内部队列", () => {
  const storage = makeStorage();
  const ob = RC.createOutbox({ storage, fetchFn: makeFakeFetch() });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] });
  const list = ob.list();
  list.length = 0;
  list.push("polluted");
  assert.equal(ob.pending(), 1, "内部队列不受 list 返回值修改影响");
});

/* ================================================================== *
 * 视频证据取舍
 * ================================================================== */

test("video: enqueue 附挂 Blob → flush 时随记录一起 append（不持久化）", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch();
  const ob = RC.createOutbox({ storage, fetchFn });
  const blob = new Blob([new Uint8Array([1, 2, 3])], { type: "video/mp4" });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] }, blob);
  await ob.flush();
  assert.equal(fetchFn.calls.length, 1);
  const fd = fetchFn.calls[0].init.body;
  assert.notEqual(fd.get("video"), null, "video 字段应被 append");
  // reload 后视频丢失（不持久化）：验证 storage 里没有 video 序列化痕迹
  const parsed = JSON.parse(storage.getItem(RC.DEFAULT_STORAGE_KEY));
  assert.equal(parsed.queue.length, 0); // 已 flush 成功
});

test("video: 函数形式 ()=>Blob 也支持（延迟求值，flush 时取）", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch();
  const ob = RC.createOutbox({ storage, fetchFn });
  let made = false;
  const blobRef = () => {
    made = true;
    return new Blob([new Uint8Array([9])], { type: "video/mp4" });
  };
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] }, blobRef);
  assert.equal(made, false, "enqueue 时不求值");
  await ob.flush();
  assert.equal(made, true, "flush 时求值");
  assert.notEqual(fetchFn.calls[0].init.body.get("video"), null);
});

/* 真机 bug 回归：真实 fetch Response（status 是只读 getter）下成功上报必须判 ok。
 * 修复前用 Object.create(res)+withBody.status=... 在严格模式抛 TypeError，被外层
 * catch 误判 retry → 设备其实 201 成功却显示"等待联网"并无限重传。 */
test("flush 用真实 Response 对象（只读 status）成功上报判 ok 并出队", async () => {
  const storage = makeStorage();
  // 返回 node 真实 Response（status 只读 getter），body 为合法 ok JSON
  const realFetch = () => Promise.resolve(
    new Response(JSON.stringify({ ok: true, run_id: "r1", count: 1, record_ids: ["a"] }), {
      status: 201,
      headers: { "Content-Type": "application/json" },
    })
  );
  const ob = RC.createOutbox({ storage, fetchFn: realFetch, now: () => 1000 });
  ob.enqueue({ cage_id: "C1", records: [{ record_id: "a", ordinal: 1, weight_g: 26.3 }] });
  const r = await ob.flush();
  assert.equal(r.sent, 1, "201 成功必须计入 sent（不能误判 retry）");
  assert.equal(r.remaining, 0);
  assert.equal(ob.pending(), 0);
});

/* ================================================================== *
 * dev 采集：readings 字段（普通对象，可序列化、可持久化，与 video Blob 不同）
 * ================================================================== */

/* 构造一个合法的 readings payload（与 local-weigh.getReadingsPayload 同构） */
function aReadingsPayload(n) {
  const readings = [];
  for (let i = 0; i < n; i++) {
    readings.push({
      t_ms: 100 * i,
      grams: 20 + i,
      raw: 200 + i,
      sequence: i + 1,
      rssi: -60 - i,
      stable: i % 2 === 0,
      receivedAtEpochMs: 1000 + 100 * i,
    });
  }
  return {
    device_id: "scale01",
    started_at_epoch_ms: 1000,
    app: "h5-dev-collect",
    engine_config: { stable_min_span_ms: 800 },
    readings,
  };
}

test("readings: enqueue 第三参数 → flush 时 append 为 Blob 文件字段 readings (readings.json)", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch();
  const ob = RC.createOutbox({ storage, fetchFn });
  const payload = aReadingsPayload(3);
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] }, null, payload);
  await ob.flush();

  assert.equal(fetchFn.calls.length, 1);
  const fd = fetchFn.calls[0].init.body;
  const readingsField = fd.get("readings");
  assert.notEqual(readingsField, null, "readings 字段应被 append");
  // 应为 Blob/File（filename readings.json）
  assert.equal(typeof readingsField.text, "function", "readings 应为 Blob");
  // FormData.get 返回的 File 带 name
  assert.equal(readingsField.name, "readings.json");
  const parsed = JSON.parse(await readingsField.text());
  assert.equal(parsed.app, "h5-dev-collect");
  assert.equal(parsed.device_id, "scale01");
  assert.equal(parsed.readings.length, 3);
  assert.equal(parsed.readings[0].grams, 20);
});

test("readings: 不传第三参数 → flush 时无 readings 字段（零开销）", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch();
  const ob = RC.createOutbox({ storage, fetchFn });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] });
  await ob.flush();
  const fd = fetchFn.calls[0].init.body;
  assert.equal(fd.get("readings"), null, "未传 readings → 不应含 readings 字段");
});

test("readings: 可持久化到 localStorage outbox，reload 后仍随记录补传", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch();
  const ob1 = RC.createOutbox({ storage, fetchFn: () => Promise.resolve(), now: () => 1000 });
  const payload = aReadingsPayload(2);
  ob1.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] }, null, payload);
  assert.equal(ob1.pending(), 1);

  // storage 落盘应含 readings（与 video Blob 不同，readings 持久化）
  const parsed = JSON.parse(storage.getItem(RC.DEFAULT_STORAGE_KEY));
  assert.equal(parsed.queue.length, 1);
  assert.ok(parsed.queue[0].readings, "持久化的 item 应含 readings");
  assert.equal(parsed.queue[0].readings.app, "h5-dev-collect");

  // reload：新建 outbox 读同一 storage，用可追踪的 fetchFn 补传
  const ob2 = RC.createOutbox({ storage, fetchFn, now: () => 2000 });
  assert.equal(ob2.pending(), 1, "reload 后队列恢复");
  await ob2.flush();
  assert.equal(ob2.pending(), 0, "补传成功");
  // 恢复后的 item flush 时仍带 readings 字段
  assert.equal(fetchFn.calls.length, 1);
  const fd = fetchFn.calls[0].init.body;
  assert.notEqual(fd.get("readings"), null, "reload 后补传仍带 readings");
  const rj = JSON.parse(await fd.get("readings").text());
  assert.equal(rj.readings.length, 2, "readings 数据完整保留");
});

test("readings: 与 video 同时存在（readings 持久化、video 不持久化）", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch();
  const ob1 = RC.createOutbox({ storage, fetchFn: () => Promise.resolve() });
  const blob = new Blob([new Uint8Array([1, 2, 3])], { type: "video/mp4" });
  const payload = aReadingsPayload(1);
  ob1.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] }, blob, payload);

  // reload：用可追踪的 fetchFn 补传
  const ob2 = RC.createOutbox({ storage, fetchFn });
  assert.equal(ob2.pending(), 1);
  await ob2.flush();
  assert.equal(fetchFn.calls.length, 1);
  const fd = fetchFn.calls[0].init.body;
  // readings 恢复（持久化）
  assert.notEqual(fd.get("readings"), null, "reload 后 readings 仍在");
  // video 未恢复（不持久化）
  assert.equal(fd.get("video"), null, "reload 后 video 丢失（不持久化）");
});

test("readings: flush 成功后随批次移除（不残留到下批）", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch();
  const ob = RC.createOutbox({ storage, fetchFn });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 1)] }, null, aReadingsPayload(1));
  ob.enqueue({ cage_id: "C2", records: [aRecord(2, 2)] }); // 第二批无 readings
  await ob.flush();
  assert.equal(fetchFn.calls.length, 2);
  // 第一批带 readings
  assert.notEqual(fetchFn.calls[0].init.body.get("readings"), null);
  // 第二批无 readings
  assert.equal(fetchFn.calls[1].init.body.get("readings"), null);
});

/* ================================================================== *
 * 8. 确认瞬间照片：records 里带 photo(dataURL) → 追加 photos 文件字段，
 *    record_id 特殊字符过滤、持久化往返 photo 不丢
 * ================================================================== */

/* 构造一个合法 JPEG dataURL（含 1x1 像素 base64） */
function aPhotoDataUrl() {
  // 最小 JPEG：ffd8 ffe0 ... ff d9（真实魔数，值任意——客户端不做解码）
  const bytes = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0x4a, 0x46, 0x49, 0x46, 0x00, 0x01, 0xff, 0xd9]);
  let bin = "";
  bytes.forEach((b) => { bin += String.fromCharCode(b); });
  const b64 = btoa(bin);
  return "data:image/jpeg;base64," + b64;
}

test("photos: dataUrlToBlob 正确解码（atob 二进制安全，字节逐一还原）", () => {
  const url = aPhotoDataUrl();
  const out = RC.dataUrlToBlob(url);
  assert.notEqual(out, null);
  assert.equal(out.mime, "image/jpeg");
  assert.ok(out.blob instanceof Blob);
  // 还原后的字节与原 dataURL 中 base64 解码一致（长度 >0）
  return out.blob.arrayBuffer().then((buf) => {
    const bytes = new Uint8Array(buf);
    assert.equal(bytes.length, 14);
    assert.equal(bytes[0], 0xff);
    assert.equal(bytes[1], 0xd8);
    assert.equal(bytes[bytes.length - 1], 0xd9);
  });
});

test("photos: safePhotoStem 只保留 [A-Za-z0-9_-]", () => {
  assert.equal(RC.safePhotoStem("rec-001"), "rec-001");
  assert.equal(RC.safePhotoStem("a_b.c/d:?x"), "a_bcdx"); // 点/斜杠/冒号/问号被过滤
  assert.equal(RC.safePhotoStem("../evil/../x"), "evilx");
  assert.equal(RC.safePhotoStem(""), "");
});

test("photos: 带 photo 的 record → FormData 有 photos 文件字段且 filename 正确", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch();
  const ob = RC.createOutbox({ storage, fetchFn });
  const rec = RC.buildRecord({ ordinal: 1, weight_g: 25.0 });
  rec.photo = aPhotoDataUrl();
  ob.enqueue({ cage_id: "C1", records: [rec] });
  await ob.flush();

  assert.equal(fetchFn.calls.length, 1);
  const fd = fetchFn.calls[0].init.body;
  const photoField = fd.get("photos");
  assert.notEqual(photoField, null, "photos 文件字段应被 append");
  // FormData.get 返回的 File 带 name → filename = <record_id>.jpg
  assert.equal(photoField.name, rec.record_id + ".jpg");
  // records JSON 本体里的 photo 字段保留（幂等重传时仍在）
  const recs = JSON.parse(fd.get("records"));
  assert.equal(recs[0].photo, rec.photo);
});

test("photos: record_id 特殊字符 → filename 已按 [A-Za-z0-9_-] 过滤防注入", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch();
  const ob = RC.createOutbox({ storage, fetchFn });
  const rec = { record_id: "rec/../../evil?x=1", ordinal: 1, weight_g: 22.0, photo: aPhotoDataUrl() };
  ob.enqueue({ cage_id: "C1", records: [rec] });
  await ob.flush();

  const fd = fetchFn.calls[0].init.body;
  const photoField = fd.get("photos");
  assert.notEqual(photoField, null);
  const stem = RC.safePhotoStem(rec.record_id);
  assert.equal(photoField.name, stem + ".jpg");
  assert.ok(!/[/?=]/.test(photoField.name), "filename 不应含路径/查询特殊字符");
});

test("photos: 无 photo 字段的记录 → 不追加 photos 文件字段（零开销）", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch();
  const ob = RC.createOutbox({ storage, fetchFn });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 25.0)] });
  await ob.flush();
  const fd = fetchFn.calls[0].init.body;
  assert.equal(fd.get("photos"), null, "无 photo → 不应含 photos 字段");
});

test("photos: 非法 dataURL → 跳过该照片，仍发记录", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch();
  const ob = RC.createOutbox({ storage, fetchFn });
  const rec = { record_id: "rec-bad", ordinal: 1, weight_g: 20.0, photo: "data:image/jpeg;base64,!!!!notbase64" };
  ob.enqueue({ cage_id: "C1", records: [rec] });
  await ob.flush();
  const fd = fetchFn.calls[0].init.body;
  assert.equal(fd.get("photos"), null, "非法 dataURL → 跳过，不 append photos");
  // 记录本身仍正常发送
  assert.notEqual(fd.get("records"), null);
});

test("photos: 持久化往返 —— photo 随 records 存 localStorage，reload 后补传仍带 photos", async () => {
  const storage = makeStorage();
  const fetchFn0 = () => Promise.resolve(); // 首次 enqueue 用假 fetch，不入队发送
  const ob1 = RC.createOutbox({ storage, fetchFn: fetchFn0, now: () => 1000 });
  const rec = RC.buildRecord({ ordinal: 1, weight_g: 25.3 });
  rec.photo = aPhotoDataUrl();
  ob1.enqueue({ cage_id: "C1", records: [rec] });
  assert.equal(ob1.pending(), 1);

  // storage 落盘：photo 字段随 records JSON 持久化（dataURL 是字符串，不像 Blob 会丢）
  const parsed = JSON.parse(storage.getItem(RC.DEFAULT_STORAGE_KEY));
  assert.equal(parsed.queue.length, 1);
  const storedRec = parsed.queue[0].batch.records[0];
  assert.equal(storedRec.photo, rec.photo, "photo 字段应持久化到 localStorage");

  // reload：新建 outbox 读同一 storage，用可追踪的 fetchFn 补传
  const fetchFn = makeFakeFetch();
  const ob2 = RC.createOutbox({ storage, fetchFn, now: () => 2000 });
  assert.equal(ob2.pending(), 1, "reload 后队列恢复");
  await ob2.flush();
  assert.equal(ob2.pending(), 0, "补传成功");
  const fd = fetchFn.calls[0].init.body;
  assert.notEqual(fd.get("photos"), null, "reload 后补传仍带 photos 文件字段");
  assert.equal(fd.get("photos").name, rec.record_id + ".jpg");
  // records JSON 里 photo 仍在
  const recs = JSON.parse(fd.get("records"));
  assert.equal(recs[0].photo, rec.photo, "reload 后 records JSON 里的 photo 不丢");
});
