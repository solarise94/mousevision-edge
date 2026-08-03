/* 本地称重控制器 (local-weigh.js) 单元测试 — node:test，零依赖。
 * 运行：node --test tests/h5/local-weigh.test.mjs
 *
 * 全部依赖注入：假引擎（捕获 onEvent 便于测试注入 announce/accept）、
 * 假 BLE 通道、假 outbox、假 storage（localStorage 兼容）、假时钟、
 * 假 setInterval/clearInterval（同步触发便于断言）。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const LW = require("../../ui/static/local-weigh.js");
const RC = require("../../ui/static/report-client.js");

/* ------------------------- harness：假组件 ------------------------- */

/* 假 localStorage（与 ReportClient.createMemoryStorage 同构）。 */
function makeStorage() {
  const store = {};
  return {
    getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
    _has: (k) => Object.prototype.hasOwnProperty.call(store, k),
    _dump: () => store,
  };
}

/* 假 BLE 通道：收集 onReading 回调，测试可调 feed(reading) 模拟读数。 */
function makeFakeChannel() {
  const readingCbs = [];
  return {
    onReadingCalls: 0,
    onReading(cb) { if (typeof cb === "function") { readingCbs.push(cb); this.onReadingCalls += 1; } },
    feed(reading) { for (const cb of readingCbs) cb(reading); },
    // 真实 ScaleBridge 没有 start/stop 由控制器管；这里也无副作用。
  };
}

/* 假 outbox：记录 enqueue 调用，pending 返回已入队批次数。 */
function makeFakeOutbox() {
  const enqueued = [];
  return {
    enqueue(batch, videoOpt) {
      const id = "batch-" + (enqueued.length + 1);
      enqueued.push({ clientBatchId: id, batch, videoOpt: videoOpt || null });
      return id;
    },
    pending() { return enqueued.length; },
    flush() { return Promise.resolve({ sent: 0, remaining: enqueued.length }); },
    start() {}, stop() {},
    _enqueued: enqueued,
  };
}

/* 假引擎模块：createSession 返回一个可控 session。
 * session._emit(type, payload) 让测试注入 engine 事件。
 * session.ingestReading / tick / accept / retry / getState 被记录。 */
function makeFakeEngineModule() {
  function createSession(o) {
    const sess = {
      onEvent: o.onEvent || (() => {}),
      ingestCalls: [],
      tickCalls: 0,
      acceptCalls: 0,
      retryCalls: 0,
      lastConfig: o.config || null,
      ingestReading(r) { this.ingestCalls.push(r); return true; },
      tick() { this.tickCalls += 1; },
      accept() { this.acceptCalls += 1; return { weight_g: 25.4, ordinal: this.acceptCalls }; },
      retry() { this.retryCalls += 1; return { applied: true, state: "weighing", epoch: 1 }; },
      getState() { return { state: "calibrating", mouseCount: 0 }; },
      // 测试注入事件
      _emit(type, payload) { this.onEvent(type, payload || {}); },
    };
    return sess;
  }
  return { createSession };
}

/* 假定时器：setInterval 记录回调；提供 _tick() 同步触发全部。 */
function makeFakeTimers() {
  const timers = [];
  let nextId = 1;
  return {
    setInterval(fn, ms) { const id = nextId++; timers.push({ id, fn, ms }); return id; },
    clearInterval(id) { const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1); },
    _tickAll() { for (const t of timers.slice()) t.fn(); },
    _count() { return timers.length; },
  };
}

/* 构造事件收集器：返回 { events, onEvent, types }。 */
function makeEventCollector() {
  const events = [];
  return {
    events,
    onEvent(type, payload) { events.push({ type, payload }); },
    types: () => events.map((e) => e.type),
  };
}

/* （各测试自行构造 opts，便于按需注入真实/假引擎与配置。） */

/* ------------------------------------------------------------------ */
/* 1. announce 模式：announce → UI 收到 + speak；accept → 记录入批次 */
/* 用真实 WeighEngine 验证集成（假引擎 session 由控制器内部创建，无法直接拿引用）。 */
/* ------------------------------------------------------------------ */
test("announce: 集成真实 WeighEngine — 三连稳定读数 → announce + speak", () => {
  const WE = require("../../ui/static/weigh-engine.js");
  let spoken = null;
  const ec = makeEventCollector();
  const timers = makeFakeTimers();
  const channel = makeFakeChannel();
  const outbox = makeFakeOutbox();
  const storage = makeStorage();
  let clock = 0;
  const ctrl = LW.createController({
    mode: "announce",
    weighEngine: WE,
    buildRecord: RC.buildRecord,
    scaleChannel: channel,
    outbox,
    box: { cageId: "C1", strain: "S" },
    storage,
    now: () => clock,
    onEvent: ec.onEvent,
    speak: (w) => { spoken = w; },
    setInterval: timers.setInterval,
    clearInterval: timers.clearInterval,
    // 关闭候选确认期等待，让稳定窗口形成即播报
    engineConfig: { calibrate_min_reads: 1, enter_sustain_frames: 1, stable_min_raw_reads: 2, stable_confirm_raw_reads: 0 },
  });
  ctrl.start();

  // 空秤 calibrating → armed
  clock = 100; channel.feed({ grams: 0.0, raw: 0, sequence: 1, receivedAtEpochMs: 100 });
  // 三连稳定非零读数 → weighing → announce
  clock = 300; channel.feed({ grams: 25.4, raw: 254, sequence: 2, receivedAtEpochMs: 300 });
  clock = 500; channel.feed({ grams: 25.4, raw: 254, sequence: 3, receivedAtEpochMs: 500 });
  clock = 700; channel.feed({ grams: 25.4, raw: 254, sequence: 4, receivedAtEpochMs: 700 });

  const announceEv = ec.events.find((e) => e.type === "announce");
  assert.ok(announceEv, "应收到 'announce' 事件");
  assert.equal(announceEv.payload.weight_g, 25.4);
  assert.equal(spoken, 25.4, "speak 应被调用并收到 25.4");
});

test("announce: accept() → 记录入批次 + 写草稿 + UI 'accepted' (count=1)", () => {
  const WE = require("../../ui/static/weigh-engine.js");
  const ec = makeEventCollector();
  const timers = makeFakeTimers();
  const channel = makeFakeChannel();
  const outbox = makeFakeOutbox();
  const storage = makeStorage();
  let clock = 0;
  const ctrl = LW.createController({
    mode: "announce",
    weighEngine: WE,
    buildRecord: RC.buildRecord,
    scaleChannel: channel,
    outbox,
    box: { cageId: "C1", strain: "S" },
    storage,
    now: () => clock,
    onEvent: ec.onEvent,
    setInterval: timers.setInterval,
    clearInterval: timers.clearInterval,
    engineConfig: { calibrate_min_reads: 1, enter_sustain_frames: 1, stable_min_raw_reads: 2, stable_confirm_raw_reads: 0 },
  });
  ctrl.start();
  // 推到 announced
  clock = 100; channel.feed({ grams: 0.0, raw: 0, sequence: 1, receivedAtEpochMs: 100 });
  clock = 300; channel.feed({ grams: 25.4, raw: 254, sequence: 2, receivedAtEpochMs: 300 });
  clock = 500; channel.feed({ grams: 25.4, raw: 254, sequence: 3, receivedAtEpochMs: 500 });
  clock = 700; channel.feed({ grams: 25.4, raw: 254, sequence: 4, receivedAtEpochMs: 700 });
  // 已 announced
  assert.ok(ec.events.some((e) => e.type === "announce"));

  // 用户确认
  ctrl.accept();

  const acceptedEv = ec.events.find((e) => e.type === "accepted");
  assert.ok(acceptedEv, "应收到 'accepted'");
  assert.equal(acceptedEv.payload.count, 1);
  assert.equal(acceptedEv.payload.weight_g, 25.4);
  assert.equal(acceptedEv.payload.ordinal, 1);

  // 草稿已写
  const draftRaw = storage.getItem(LW._draftKey("C1"));
  assert.ok(draftRaw, "草稿应已持久化");
  const draft = JSON.parse(draftRaw);
  assert.equal(draft.records.length, 1);
  assert.equal(draft.records[0].weight_g, 25.4);
  assert.equal(draft.cageId, "C1");
  assert.equal(draft.mode, "announce");

  // recorded 事件
  const recordedEv = ec.events.find((e) => e.type === "recorded");
  assert.ok(recordedEv, "应收到 'recorded'");
  assert.equal(recordedEv.payload.record.weight_g, 25.4);
  assert.equal(recordedEv.payload.pendingCount, 0); // 还没 finishBox

  // getState
  const st = ctrl.getState();
  assert.equal(st.mouseCount, 1);
  assert.equal(st.mode, "announce");
});

/* ------------------------------------------------------------------ */
/* 2. post_match 模式：'announce' → 自动 accept（不调 speak、不需人工） */
/* ------------------------------------------------------------------ */
test("post_match: engine 'announce' → 自动 accept + 记录生成（不调 speak）", () => {
  const WE = require("../../ui/static/weigh-engine.js");
  let spoken = null;
  const ec = makeEventCollector();
  const timers = makeFakeTimers();
  const channel = makeFakeChannel();
  const outbox = makeFakeOutbox();
  const storage = makeStorage();
  let clock = 0;
  const ctrl = LW.createController({
    mode: "post_match",
    weighEngine: WE,
    buildRecord: RC.buildRecord,
    scaleChannel: channel,
    outbox,
    box: { cageId: "C2" },
    storage,
    now: () => clock,
    onEvent: ec.onEvent,
    speak: (w) => { spoken = w; },
    setInterval: timers.setInterval,
    clearInterval: timers.clearInterval,
    engineConfig: { calibrate_min_reads: 1, enter_sustain_frames: 1, stable_min_raw_reads: 2, stable_confirm_raw_reads: 0 },
  });
  ctrl.start();
  clock = 100; channel.feed({ grams: 0.0, raw: 0, sequence: 1, receivedAtEpochMs: 100 });
  clock = 300; channel.feed({ grams: 18.2, raw: 182, sequence: 2, receivedAtEpochMs: 300 });
  clock = 500; channel.feed({ grams: 18.2, raw: 182, sequence: 3, receivedAtEpochMs: 500 });
  clock = 700; channel.feed({ grams: 18.2, raw: 182, sequence: 4, receivedAtEpochMs: 700 });

  // post_match 不应向 UI 发 'announce'（不打扰），也不调 speak
  assert.ok(!ec.events.some((e) => e.type === "announce"), "post_match 不发 UI 'announce'");
  assert.equal(spoken, null, "post_match 不调 speak");

  // 但应自动 accept → 生成记录
  const acceptedEv = ec.events.find((e) => e.type === "accepted");
  assert.ok(acceptedEv, "post_match 应自动 accept → 'accepted'");
  assert.equal(acceptedEv.payload.count, 1);
  assert.equal(acceptedEv.payload.weight_g, 18.2);

  // 草稿已写
  const draft = JSON.parse(storage.getItem(LW._draftKey("C2")));
  assert.equal(draft.records.length, 1);
  assert.equal(draft.records[0].weight_g, 18.2);
});

/* ------------------------------------------------------------------ */
/* 3. manual 模式：submitManual → 记录生成 + 写草稿；非法值拒绝 */
/* ------------------------------------------------------------------ */
test("manual: submitManual(26.3) → 记录生成 + 写草稿", () => {
  const ec = makeEventCollector();
  const outbox = makeFakeOutbox();
  const storage = makeStorage();
  const ctrl = LW.createController({
    mode: "manual",
    weighEngine: makeFakeEngineModule(),
    buildRecord: RC.buildRecord,
    scaleChannel: null,
    outbox,
    box: { cageId: "CM" },
    storage,
    now: () => 5000,
    onEvent: ec.onEvent,
  });
  ctrl.start();

  const rec = ctrl.submitManual(26.3);
  assert.ok(rec, "应返回 record");
  assert.equal(rec.weight_g, 26.3);
  assert.equal(rec.ordinal, 1);
  assert.ok(rec.record_id, "buildRecord 应自动补 record_id");
  assert.ok(rec.recorded_at, "buildRecord 应自动补 recorded_at");

  // accepted + recorded 事件
  const acceptedEv = ec.events.find((e) => e.type === "accepted");
  assert.ok(acceptedEv);
  assert.equal(acceptedEv.payload.count, 1);
  assert.equal(acceptedEv.payload.weight_g, 26.3);
  const recordedEv = ec.events.find((e) => e.type === "recorded");
  assert.ok(recordedEv);

  // 草稿已写
  const draft = JSON.parse(storage.getItem(LW._draftKey("CM")));
  assert.equal(draft.records.length, 1);
  assert.equal(draft.records[0].weight_g, 26.3);
  assert.equal(draft.mode, "manual");

  assert.equal(ctrl.getState().mouseCount, 1);
  assert.equal(ctrl.getState().state, "manual");
});

test("manual: 非法值拒绝（NaN / -1 / 超界）", () => {
  const ctrl = LW.createController({
    mode: "manual",
    weighEngine: makeFakeEngineModule(),
    buildRecord: RC.buildRecord,
    scaleChannel: null,
    outbox: makeFakeOutbox(),
    box: { cageId: "CM" },
    storage: makeStorage(),
    now: () => 0,
    onEvent: () => {},
  });
  ctrl.start();
  assert.equal(ctrl.submitManual(NaN), null);
  assert.equal(ctrl.submitManual(-1), null);
  assert.equal(ctrl.submitManual(0 - 0.001), null);
  assert.equal(ctrl.submitManual(LW._MAX_GRAMS + 0.1), null);
  assert.equal(ctrl.submitManual("abc"), null); // 非数字
  assert.equal(ctrl.submitManual(undefined), null);
  assert.equal(ctrl.submitManual(Infinity), null);
  // 边界合法
  assert.ok(ctrl.submitManual(0));
  assert.ok(ctrl.submitManual(LW._MAX_GRAMS));
  assert.equal(ctrl.getState().mouseCount, 2);
});

/* ------------------------------------------------------------------ */
/* 4. BLE 读数 → engine.ingestReading 被调 + UI 收到 'weight' */
/* 用假引擎验证读数管道连通（'weight' 事件触发证明 handleReading 执行）， */
/* 用真实引擎验证 ingestReading 真正驱动状态机。 */
/* ------------------------------------------------------------------ */
test("BLE 读数（假引擎）→ UI 'weight' 直读显示", () => {
  const ec = makeEventCollector();
  const channel = makeFakeChannel();
  const timers = makeFakeTimers();
  const ctrl = LW.createController({
    mode: "announce",
    weighEngine: makeFakeEngineModule(),
    buildRecord: RC.buildRecord,
    scaleChannel: channel,
    outbox: makeFakeOutbox(),
    box: { cageId: "C4a" },
    storage: makeStorage(),
    now: () => 0,
    onEvent: ec.onEvent,
    setInterval: timers.setInterval,
    clearInterval: timers.clearInterval,
  });
  ctrl.start();
  channel.feed({ grams: 7.3, raw: 73, sequence: 1, receivedAtEpochMs: 100 });
  const weightEv = ec.events.find((e) => e.type === "weight");
  assert.ok(weightEv, "应收到 'weight'");
  assert.equal(weightEv.payload.grams, 7.3);
});

test("BLE 读数 → 真实 WeighEngine ingestReading + UI 'weight'", () => {
  const WE = require("../../ui/static/weigh-engine.js");
  const ec = makeEventCollector();
  const channel = makeFakeChannel();
  const timers = makeFakeTimers();
  let clock = 0;
  const ctrl = LW.createController({
    mode: "announce",
    weighEngine: WE,
    buildRecord: RC.buildRecord,
    scaleChannel: channel,
    outbox: makeFakeOutbox(),
    box: { cageId: "C4b" },
    storage: makeStorage(),
    now: () => clock,
    onEvent: ec.onEvent,
    setInterval: timers.setInterval,
    clearInterval: timers.clearInterval,
  });
  ctrl.start();

  clock = 200;
  channel.feed({ grams: 12.5, raw: 125, sequence: 5, receivedAtEpochMs: 1000 });

  // UI 应收到 'weight' {grams: 12.5}
  const weightEv = ec.events.find((e) => e.type === "weight");
  assert.ok(weightEv, "应收到 'weight'");
  assert.equal(weightEv.payload.grams, 12.5);

  // 引擎在 calibrating 收到 12.5 > empty_max → 计数清零，仍停留在 calibrating
  // （engine 仅在状态变化时发 'state'，停留不发；这里只验证引擎未崩 + getState 可读）
  assert.equal(ctrl.getState().mode, "announce");

  // getState.lastGrams 反映直读
  assert.equal(ctrl.getState().lastGrams, 12.5);
});

/* ------------------------------------------------------------------ */
/* 5. 草稿恢复：storage 有草稿 → start() 恢复 records/count + 'draft_resumed' */
/* ------------------------------------------------------------------ */
test("草稿恢复: storage 有未完成草稿 → start() 恢复 + 'draft_resumed'", () => {
  const storage = makeStorage();
  // 预置一个有 2 条记录的草稿
  storage.setItem(LW._draftKey("CR"), JSON.stringify({
    cageId: "CR",
    mode: "announce",
    records: [
      { record_id: "r1", ordinal: 1, weight_g: 20.1, recorded_at: "2026-01-01T00:00:00Z" },
      { record_id: "r2", ordinal: 2, weight_g: 21.2, recorded_at: "2026-01-01T00:00:01Z" },
    ],
    startedAt: 1000,
    realtimeT0: 999,
  }));

  const ec = makeEventCollector();
  const ctrl = LW.createController({
    mode: "announce",
    weighEngine: makeFakeEngineModule(),
    buildRecord: RC.buildRecord,
    scaleChannel: makeFakeChannel(),
    outbox: makeFakeOutbox(),
    box: { cageId: "CR" },
    storage,
    now: () => 5000,
    onEvent: ec.onEvent,
    setInterval: makeFakeTimers().setInterval,
    clearInterval: () => {},
  });
  ctrl.start();

  const resumedEv = ec.events.find((e) => e.type === "draft_resumed");
  assert.ok(resumedEv, "应发 'draft_resumed'");
  assert.equal(resumedEv.payload.count, 2);
  assert.equal(ctrl.getState().mouseCount, 2);
  assert.deepEqual(ctrl._records().map((r) => r.weight_g), [20.1, 21.2]);
});

test("草稿恢复: 无草稿 → 不发 'draft_resumed'", () => {
  const ec = makeEventCollector();
  const ctrl = LW.createController({
    mode: "announce",
    weighEngine: makeFakeEngineModule(),
    buildRecord: RC.buildRecord,
    scaleChannel: makeFakeChannel(),
    outbox: makeFakeOutbox(),
    box: { cageId: "C9" },
    storage: makeStorage(),
    now: () => 0,
    onEvent: ec.onEvent,
    setInterval: () => 1,
    clearInterval: () => {},
  });
  ctrl.start();
  assert.ok(!ec.events.some((e) => e.type === "draft_resumed"));
  assert.equal(ctrl.getState().mouseCount, 0);
});

/* ------------------------------------------------------------------ */
/* 6. finishBox: enqueue 被调（batch 含 cageId/records/weight_source）+ 草稿清除 */
/* ------------------------------------------------------------------ */
test("finishBox: enqueue batch + 草稿清除 + 返回 count", () => {
  const outbox = makeFakeOutbox();
  const storage = makeStorage();
  const ctrl = LW.createController({
    mode: "manual",
    weighEngine: makeFakeEngineModule(),
    buildRecord: RC.buildRecord,
    scaleChannel: null,
    outbox,
    box: { cageId: "CF", strain: "Balb" },
    deviceId: "devX",
    projectId: "projY",
    storage,
    now: () => 0,
    onEvent: () => {},
  });
  ctrl.start();
  ctrl.submitManual(10.0);
  ctrl.submitManual(11.0);

  const result = ctrl.finishBox();
  assert.equal(result.count, 2);
  assert.ok(result.batchId, "应返回 batchId");

  // outbox.enqueue 被调一次
  assert.equal(outbox._enqueued.length, 1);
  const batch = outbox._enqueued[0].batch;
  assert.equal(batch.cage_id, "CF");
  assert.equal(batch.strain, "Balb");
  assert.equal(batch.device_id, "devX");
  assert.equal(batch.project_id, "projY");
  assert.equal(batch.weight_source, "manual"); // manual 模式默认
  assert.equal(batch.records.length, 2);
  assert.equal(batch.records[0].weight_g, 10.0);

  // 草稿清除
  assert.equal(storage.getItem(LW._draftKey("CF")), null);

  // 累积清零（同一控制器实例）
  assert.equal(ctrl.getState().mouseCount, 0);
});

test("finishBox: announce 模式 weight_source=ble_k797 + videoBlob 透传", () => {
  const outbox = makeFakeOutbox();
  const ctrl = LW.createController({
    mode: "announce",
    weighEngine: makeFakeEngineModule(),
    buildRecord: RC.buildRecord,
    scaleChannel: makeFakeChannel(),
    outbox,
    box: { cageId: "CB" },
    storage: makeStorage(),
    now: () => 0,
    onEvent: () => {},
    setInterval: () => 1,
    clearInterval: () => {},
  });
  ctrl.start();
  // 通过假引擎直接走 manual? 不行——announce 模式无 submitManual。改为注入记录：
  // 用真实引擎 accept 路径，或直接调 finishBox 空 batch 验证 weight_source。
  const blob = { name: "v.mp4" };
  const result = ctrl.finishBox(blob);
  assert.equal(result.count, 0);
  assert.equal(outbox._enqueued[0].batch.weight_source, "ble_k797");
  assert.equal(outbox._enqueued[0].videoOpt, blob);
});

/* ------------------------------------------------------------------ */
/* 7. 崩溃安全: accept 后草稿已持久化；finishBox 后清除 */
/* ------------------------------------------------------------------ */
test("崩溃安全: accept 后草稿已落 storage；finishBox 后清除", () => {
  const WE = require("../../ui/static/weigh-engine.js");
  const storage = makeStorage();
  const timers = makeFakeTimers();
  const channel = makeFakeChannel();
  let clock = 0;
  const ctrl = LW.createController({
    mode: "announce",
    weighEngine: WE,
    buildRecord: RC.buildRecord,
    scaleChannel: channel,
    outbox: makeFakeOutbox(),
    box: { cageId: "CK" },
    storage,
    now: () => clock,
    onEvent: () => {},
    setInterval: timers.setInterval,
    clearInterval: timers.clearInterval,
    engineConfig: { calibrate_min_reads: 1, enter_sustain_frames: 1, stable_min_raw_reads: 2, stable_confirm_raw_reads: 0 },
  });
  ctrl.start();
  clock = 100; channel.feed({ grams: 0.0, raw: 0, sequence: 1, receivedAtEpochMs: 100 });
  clock = 300; channel.feed({ grams: 30.0, raw: 300, sequence: 2, receivedAtEpochMs: 300 });
  clock = 500; channel.feed({ grams: 30.0, raw: 300, sequence: 3, receivedAtEpochMs: 500 });
  clock = 700; channel.feed({ grams: 30.0, raw: 300, sequence: 4, receivedAtEpochMs: 700 });
  ctrl.accept();

  // 1) accept 后草稿已落 storage（崩溃安全点）
  const draftAfterAccept = storage.getItem(LW._draftKey("CK"));
  assert.ok(draftAfterAccept, "accept 后草稿应已持久化");
  assert.equal(JSON.parse(draftAfterAccept).records.length, 1);

  // 再 accept 一只（需要清秤回到 armed → 再称）
  // wait_clear：清秤 → armed → ready_next → 再称
  clock = 900; channel.feed({ grams: 0.0, raw: 0, sequence: 5, receivedAtEpochMs: 900 });
  clock = 1100; channel.feed({ grams: 28.5, raw: 285, sequence: 6, receivedAtEpochMs: 1100 });
  clock = 1300; channel.feed({ grams: 28.5, raw: 285, sequence: 7, receivedAtEpochMs: 1300 });
  clock = 1500; channel.feed({ grams: 28.5, raw: 285, sequence: 8, receivedAtEpochMs: 1500 });
  ctrl.accept();
  const draft2 = JSON.parse(storage.getItem(LW._draftKey("CK")));
  assert.equal(draft2.records.length, 2, "第二只 accept 后草稿含 2 条");

  // 2) finishBox 后草稿清除
  ctrl.finishBox();
  assert.equal(storage.getItem(LW._draftKey("CK")), null, "finishBox 后草稿应清除");
});

/* ------------------------------------------------------------------ */
/* 补充：tick 定时器在 stop 后清除；videoTimeMs 注入 clip_start_ms */
/* ------------------------------------------------------------------ */
test("tick 定时器: start 启动、stop 清除", () => {
  const timers = makeFakeTimers();
  const ctrl = LW.createController({
    mode: "announce",
    weighEngine: makeFakeEngineModule(),
    buildRecord: RC.buildRecord,
    scaleChannel: makeFakeChannel(),
    outbox: makeFakeOutbox(),
    box: { cageId: "CT" },
    storage: makeStorage(),
    now: () => 0,
    onEvent: () => {},
    setInterval: timers.setInterval,
    clearInterval: timers.clearInterval,
  });
  assert.equal(timers._count(), 0, "start 前无定时器");
  ctrl.start();
  assert.equal(timers._count(), 1, "start 后应有一个 tick 定时器");
  ctrl.stop();
  assert.equal(timers._count(), 0, "stop 后定时器应清除");
});

test("manual 模式不创建 tick 定时器", () => {
  const timers = makeFakeTimers();
  const ctrl = LW.createController({
    mode: "manual",
    weighEngine: makeFakeEngineModule(),
    buildRecord: RC.buildRecord,
    scaleChannel: null,
    outbox: makeFakeOutbox(),
    box: { cageId: "C0" },
    storage: makeStorage(),
    now: () => 0,
    onEvent: () => {},
    setInterval: timers.setInterval,
    clearInterval: timers.clearInterval,
  });
  ctrl.start();
  assert.equal(timers._count(), 0, "manual 模式不应创建 tick 定时器");
});

test("videoTimeMs: accept 时记 clip_start_ms", () => {
  const WE = require("../../ui/static/weigh-engine.js");
  const storage = makeStorage();
  const timers = makeFakeTimers();
  const channel = makeFakeChannel();
  let clock = 0;
  let videoClock = 42000; // 录像相对 42s
  const ctrl = LW.createController({
    mode: "announce",
    weighEngine: WE,
    buildRecord: RC.buildRecord,
    scaleChannel: channel,
    outbox: makeFakeOutbox(),
    box: { cageId: "CV" },
    storage,
    now: () => clock,
    onEvent: () => {},
    setInterval: timers.setInterval,
    clearInterval: timers.clearInterval,
    videoTimeMs: () => videoClock,
    engineConfig: { calibrate_min_reads: 1, enter_sustain_frames: 1, stable_min_raw_reads: 2, stable_confirm_raw_reads: 0 },
  });
  ctrl.start();
  clock = 100; channel.feed({ grams: 0.0, raw: 0, sequence: 1, receivedAtEpochMs: 100 });
  clock = 300; channel.feed({ grams: 22.0, raw: 220, sequence: 2, receivedAtEpochMs: 300 });
  clock = 500; channel.feed({ grams: 22.0, raw: 220, sequence: 3, receivedAtEpochMs: 500 });
  clock = 700; channel.feed({ grams: 22.0, raw: 220, sequence: 4, receivedAtEpochMs: 700 });
  videoClock = 43100;
  ctrl.accept();

  const rec = ctrl._records()[0];
  assert.equal(rec.clip_start_ms, 43100, "clip_start_ms 应为 accept 时刻的录像相对毫秒");
});

test("retry: announce/post_match 调 engine.retry()", () => {
  const WE = require("../../ui/static/weigh-engine.js");
  const storage = makeStorage();
  const timers = makeFakeTimers();
  const channel = makeFakeChannel();
  let clock = 0;
  const ctrl = LW.createController({
    mode: "announce",
    weighEngine: WE,
    buildRecord: RC.buildRecord,
    scaleChannel: channel,
    outbox: makeFakeOutbox(),
    box: { cageId: "CR2" },
    storage,
    now: () => clock,
    onEvent: () => {},
    setInterval: timers.setInterval,
    clearInterval: timers.clearInterval,
    engineConfig: { calibrate_min_reads: 1, enter_sustain_frames: 1, stable_min_raw_reads: 2, stable_confirm_raw_reads: 0 },
  });
  ctrl.start();
  clock = 100; channel.feed({ grams: 0.0, raw: 0, sequence: 1, receivedAtEpochMs: 100 });
  clock = 300; channel.feed({ grams: 19.0, raw: 190, sequence: 2, receivedAtEpochMs: 300 });
  clock = 500; channel.feed({ grams: 19.0, raw: 190, sequence: 3, receivedAtEpochMs: 500 });
  clock = 700; channel.feed({ grams: 19.0, raw: 190, sequence: 4, receivedAtEpochMs: 700 });
  // 已 announced
  const r = ctrl.retry();
  assert.equal(r.applied, true);
  // retry 不应生成记录
  assert.equal(ctrl.getState().mouseCount, 0);
});

test("构造校验: 非法 mode 抛错", () => {
  assert.throws(() => LW.createController({ mode: "bogus", weighEngine: makeFakeEngineModule(), buildRecord: RC.buildRecord, outbox: makeFakeOutbox(), box: { cageId: "X" } }), /mode/);
});

test("构造校验: announce 缺 scaleChannel 抛错", () => {
  assert.throws(() => LW.createController({
    mode: "announce", weighEngine: makeFakeEngineModule(), buildRecord: RC.buildRecord,
    outbox: makeFakeOutbox(), box: { cageId: "X" },
  }), /scaleChannel/);
});

test("构造校验: 缺 buildRecord 抛错", () => {
  assert.throws(() => LW.createController({
    mode: "manual", weighEngine: makeFakeEngineModule(),
    outbox: makeFakeOutbox(), box: { cageId: "X" },
  }), /buildRecord/);
});

test("构造校验: 缺 cageId 抛错", () => {
  assert.throws(() => LW.createController({
    mode: "manual", weighEngine: makeFakeEngineModule(), buildRecord: RC.buildRecord,
    outbox: makeFakeOutbox(), box: {},
  }), /cageId/);
});

test("stop 后通道读数不再触发 UI 'weight'", () => {
  const WE = require("../../ui/static/weigh-engine.js");
  const ec = makeEventCollector();
  const channel = makeFakeChannel();
  const timers = makeFakeTimers();
  let clock = 0;
  const ctrl = LW.createController({
    mode: "announce",
    weighEngine: WE,
    buildRecord: RC.buildRecord,
    scaleChannel: channel,
    outbox: makeFakeOutbox(),
    box: { cageId: "CS" },
    storage: makeStorage(),
    now: () => clock,
    onEvent: ec.onEvent,
    setInterval: timers.setInterval,
    clearInterval: timers.clearInterval,
  });
  ctrl.start();
  ctrl.stop();
  clock = 100;
  channel.feed({ grams: 5.0, raw: 50, sequence: 1, receivedAtEpochMs: 100 });
  assert.ok(!ec.events.some((e) => e.type === "weight"), "stop 后读数不应触发 UI");
});

/* ------------------------------------------------------------------ */
/* ready_next / stale 事件转发
/* ------------------------------------------------------------------ */
test("ready_next 与 stale 事件转发到 UI", () => {
  const WE = require("../../ui/static/weigh-engine.js");
  const ec = makeEventCollector();
  const channel = makeFakeChannel();
  const timers = makeFakeTimers();
  let clock = 0;
  const ctrl = LW.createController({
    mode: "announce",
    weighEngine: WE,
    buildRecord: RC.buildRecord,
    scaleChannel: channel,
    outbox: makeFakeOutbox(),
    box: { cageId: "CN" },
    storage: makeStorage(),
    now: () => clock,
    onEvent: ec.onEvent,
    setInterval: timers.setInterval,
    clearInterval: timers.clearInterval,
    engineConfig: { calibrate_min_reads: 1, enter_sustain_frames: 1, stable_min_raw_reads: 2, stable_confirm_raw_reads: 0, ble_stale_s: 1.0 },
  });
  ctrl.start();
  clock = 100; channel.feed({ grams: 0.0, raw: 0, sequence: 1, receivedAtEpochMs: 100 });
  clock = 300; channel.feed({ grams: 24.0, raw: 240, sequence: 2, receivedAtEpochMs: 300 });
  clock = 500; channel.feed({ grams: 24.0, raw: 240, sequence: 3, receivedAtEpochMs: 500 });
  clock = 700; channel.feed({ grams: 24.0, raw: 240, sequence: 4, receivedAtEpochMs: 700 });
  ctrl.accept();
  // accept → wait_clear；清秤 → armed + ready_next
  clock = 900; channel.feed({ grams: 0.0, raw: 0, sequence: 5, receivedAtEpochMs: 900 });
  assert.ok(ec.events.some((e) => e.type === "ready_next"), "清秤后应转发 ready_next");

  // stale：ble_stale_s=1.0，长时间无读数 → stale
  clock = 5000; // 远超 1s
  timers._tickAll(); // tick 触发 stale 判定
  assert.ok(ec.events.some((e) => e.type === "stale" && e.payload.stale === true), "应转发 stale=true");
});
