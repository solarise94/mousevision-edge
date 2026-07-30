/* 蓝牙天平桥接 (scale-bridge.js) 单元测试 — node:test 内置运行器，零依赖。
 * 运行：node --test tests/h5/
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const SB = require("../../ui/static/scale-bridge.js");

/* 构造一条合法读数 detail */
function goodReading(over) {
  return Object.assign(
    {
      schemaVersion: 1,
      device: "K797",
      deviceKey: "k797:0000:abc",
      grams: 26.3,
      raw: 263,
      rssi: -49,
      receivedAt: "2026-07-30T00:00:00Z",
      receivedAtEpochMs: 1785393390194,
      sequence: 1,
      stable: true,
      stableSource: "derived_repeat",
      source: "ble",
      payloadHex: "CA",
    },
    over || {}
  );
}

/* 构造一个无真实 window/event 的通道：用注入的 listeners 字典与可控时钟 */
function makeFakeEnv() {
  const listeners = {};
  const timers = [];
  let nowMs = 1000;
  return {
    listeners,
    now: () => nowMs,
    advance: (ms) => { nowMs += ms; },
    perfNow: () => nowMs,
    addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
    removeEventListener: (type, fn) => {
      const arr = listeners[type];
      if (arr) listeners[type] = arr.filter((f) => f !== fn);
    },
    setInterval: (fn, ms) => { const id = { fn, ms }; timers.push(id); return id; },
    clearInterval: (id) => { const i = timers.indexOf(id); if (i >= 0) timers.splice(i, 1); },
    fireTimer: () => { timers.forEach((t) => t.fn()); },
    native: { startScaleScanCalls: 0, stopScaleScanCalls: 0, status: null,
      startScaleScan() { this.startScaleScanCalls += 1; },
      stopScaleScan() { this.stopScaleScanCalls += 1; },
      getScaleStatus() { return this.status ? JSON.stringify(this.status) : ""; },
    },
    dispatchReading: (detail) => {
      (listeners[SB.READING_EVENT] || []).forEach((fn) => fn({ detail }));
    },
    dispatchStatus: (detail) => {
      (listeners[SB.STATUS_EVENT] || []).forEach((fn) => fn({ detail }));
    },
  };
}

/* 模拟存在原生桥的 window 作用域 */
function fakeWindowWithBridge(native) {
  return { MiceAutomaticScale: native };
}

test("detectNativeBridge: 无桥返回 false", () => {
  assert.equal(SB.detectNativeBridge({}), false);
  assert.equal(SB.detectNativeBridge(null), false);
  assert.equal(SB.detectNativeBridge({ MiceAutomaticScale: {} }), false);
});

test("detectNativeBridge: 三方法齐全返回 true", () => {
  const w = fakeWindowWithBridge({
    startScaleScan() {}, stopScaleScan() {}, getScaleStatus() {},
  });
  assert.equal(SB.detectNativeBridge(w), true);
});

test("读数校验：非法形状被丢弃", () => {
  const env = makeFakeEnv();
  const ch = SB.createScaleChannel({
    windowScope: fakeWindowWithBridge(env.native),
    nativeBridge: env.native,
    now: env.now, perfNow: env.perfNow,
    addEventListener: env.addEventListener, removeEventListener: env.removeEventListener,
    setInterval: env.setInterval, clearInterval: env.clearInterval,
  });
  ch.start();
  const seen = [];
  ch.onReading((r) => seen.push(r));

  // grams 非有限
  ch.getState; // no-op
  env.dispatchReading(goodReading({ grams: NaN }));
  env.dispatchReading(goodReading({ grams: Infinity }));
  env.dispatchReading(goodReading({ grams: -1 }));
  env.dispatchReading(goodReading({ grams: 9999 }));
  // raw 非整数 / 越界
  env.dispatchReading(goodReading({ raw: 1.5 }));
  env.dispatchReading(goodReading({ raw: 70000 }));
  env.dispatchReading(goodReading({ raw: -1 }));
  // sequence 非整数 / 负
  env.dispatchReading(goodReading({ sequence: 1.5 }));
  env.dispatchReading(goodReading({ sequence: -1 }));
  // detail 非 object
  env.dispatchReading(null);

  assert.equal(seen.length, 0, "所有非法读数应被丢弃");
  assert.equal(ch.getState().lastReading, null);
  ch.stop();
});

test("读数校验：grams=0 / raw=0 是合法真实零点", () => {
  const env = makeFakeEnv();
  const ch = SB.createScaleChannel({
    windowScope: fakeWindowWithBridge(env.native),
    nativeBridge: env.native,
    now: env.now, perfNow: env.perfNow,
    addEventListener: env.addEventListener, removeEventListener: env.removeEventListener,
    setInterval: env.setInterval, clearInterval: env.clearInterval,
  });
  ch.start();
  const seen = [];
  ch.onReading((r) => seen.push(r));
  env.dispatchReading(goodReading({ grams: 0, raw: 0, sequence: 1 }));
  assert.equal(seen.length, 1);
  assert.equal(seen[0].grams, 0);
  ch.stop();
});

test("乱序 / 重复 sequence 被丢弃并计数", () => {
  const env = makeFakeEnv();
  const ch = SB.createScaleChannel({
    windowScope: fakeWindowWithBridge(env.native),
    nativeBridge: env.native,
    now: env.now, perfNow: env.perfNow,
    addEventListener: env.addEventListener, removeEventListener: env.removeEventListener,
    setInterval: env.setInterval, clearInterval: env.clearInterval,
  });
  ch.start();
  const seen = [];
  ch.onReading((r) => seen.push(r.sequence));

  env.dispatchReading(goodReading({ sequence: 10 }));
  env.dispatchReading(goodReading({ sequence: 10 })); // 重复
  env.dispatchReading(goodReading({ sequence: 5 }));  // 回退
  env.dispatchReading(goodReading({ sequence: 11 })); // 正常
  env.dispatchReading(goodReading({ sequence: 9 }));  // 回退

  assert.deepEqual(seen, [10, 11]);
  assert.equal(ch.getState().droppedOutOfOrder, 3);
  ch.stop();
});

test("stale：10s 内无读数翻转；收到读数后恢复", () => {
  const env = makeFakeEnv();
  const ch = SB.createScaleChannel({
    windowScope: fakeWindowWithBridge(env.native),
    nativeBridge: env.native,
    now: env.now, perfNow: env.perfNow,
    addEventListener: env.addEventListener, removeEventListener: env.removeEventListener,
    setInterval: env.setInterval, clearInterval: env.clearInterval,
    staleMs: 10000,
  });
  ch.start();
  const staleChanges = [];
  ch.onStaleChange((s) => staleChanges.push(s));

  // 初始 stale（从未收到读数）
  assert.equal(ch.getState().stale, true);

  // 收到读数 → 不 stale
  env.dispatchReading(goodReading({ sequence: 1 }));
  assert.equal(ch.getState().stale, false);

  // 推进 9.5s，仍新鲜
  env.advance(9500);
  env.fireTimer(); // 看门狗触发
  assert.equal(ch.getState().stale, false);

  // 再推进 1s（累计 10.5s）→ stale 翻转
  env.advance(1000);
  env.fireTimer();
  assert.equal(ch.getState().stale, true);
  assert.deepEqual(staleChanges, [false, true]);

  // 新读数到达 → 恢复
  env.dispatchReading(goodReading({ sequence: 2 }));
  assert.equal(ch.getState().stale, false);
  ch.stop();
});

test("stale：原生异常态且无新鲜读数立即 stale", () => {
  const env = makeFakeEnv();
  const ch = SB.createScaleChannel({
    windowScope: fakeWindowWithBridge(env.native),
    nativeBridge: env.native,
    now: env.now, perfNow: env.perfNow,
    addEventListener: env.addEventListener, removeEventListener: env.removeEventListener,
    setInterval: env.setInterval, clearInterval: env.clearInterval,
  });
  ch.start();
  env.dispatchStatus({ device: "K797", state: "bluetooth_off", message: "蓝牙已关闭" });
  assert.equal(ch.getState().stale, true);
  ch.stop();
});

test("latest-only sender：仅保留最新一条，flush 只发一条", () => {
  const sent = [];
  const sender = SB.createLatestOnlySender((m) => sent.push(m));
  sender.offer({ sequence: 1 });
  sender.offer({ sequence: 2 });
  sender.offer({ sequence: 3 });
  assert.equal(sender.hasPending(), true);
  // flush 发送最新（3），不补发 1/2
  const ok = sender.flush();
  assert.equal(ok, true);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].sequence, 3);
  // 再次 flush：已无缓存
  assert.equal(sender.flush(), false);
  assert.equal(sender.hasPending(), false);
});

test("latest-only sender：发送函数抛错时 flush 返回 false 不崩溃", () => {
  const sender = SB.createLatestOnlySender(() => { throw new Error("x"); });
  sender.offer({ a: 1 });
  assert.equal(sender.flush(), false);
});

test("formatScaleDisplay：无读数 → '--'", () => {
  const r = SB.formatScaleDisplay({ lastReading: null, stale: true });
  assert.deepEqual(r, { text: "--", stale: true });
});

test("formatScaleDisplay：stale → '--'", () => {
  const r = SB.formatScaleDisplay({ lastReading: goodReading(), stale: true });
  assert.deepEqual(r, { text: "--", stale: true });
});

test("formatScaleDisplay：raw=0 → '0.0'", () => {
  const r = SB.formatScaleDisplay({ lastReading: goodReading({ grams: 0, raw: 0 }), stale: false });
  assert.deepEqual(r, { text: "0.0", stale: false });
});

test("formatScaleDisplay：26.3 → '26.3'", () => {
  const r = SB.formatScaleDisplay({ lastReading: goodReading({ grams: 26.3 }), stale: false });
  assert.deepEqual(r, { text: "26.3", stale: false });
});

test("buildScaleReadingMessage：字段与 source 固定值", () => {
  const reading = goodReading({ grams: 26.3, raw: 263, sequence: 1248, stable: true, rssi: -49, receivedAtEpochMs: 1785393390194 });
  const msg = SB.buildScaleReadingMessage(reading, 12840);
  assert.equal(msg.type, "scale_reading");
  assert.equal(msg.source, "ble_k797");
  assert.equal(msg.grams, 26.3);
  assert.equal(msg.raw, 263);
  assert.equal(msg.client_ts_ms, 12840);
  assert.equal(msg.received_at_epoch_ms, 1785393390194);
  assert.equal(msg.sequence, 1248);
  assert.equal(msg.stable, true);
  assert.equal(msg.rssi, -49);
});

test("buildScaleReadingMessage：client_ts_ms 截断为非负整数", () => {
  const msg = SB.buildScaleReadingMessage(goodReading(), -5.7);
  assert.equal(msg.client_ts_ms, 0);
  const msg2 = SB.buildScaleReadingMessage(goodReading(), 12.9);
  assert.equal(msg2.client_ts_ms, 12);
});

test("start/stop：挂载/卸载监听并调用原生 startScaleScan/stopScaleScan", () => {
  const env = makeFakeEnv();
  const ch = SB.createScaleChannel({
    windowScope: fakeWindowWithBridge(env.native),
    nativeBridge: env.native,
    now: env.now, perfNow: env.perfNow,
    addEventListener: env.addEventListener, removeEventListener: env.removeEventListener,
    setInterval: env.setInterval, clearInterval: env.clearInterval,
  });
  ch.start();
  assert.equal(env.native.startScaleScanCalls, 1);
  assert.ok(env.listeners[SB.READING_EVENT] && env.listeners[SB.READING_EVENT].length > 0);
  ch.stop();
  assert.equal(env.native.stopScaleScanCalls, 1);
  assert.equal((env.listeners[SB.READING_EVENT] || []).length, 0);
});

test("onStatus 回调被触发，onReading 回调被触发", () => {
  const env = makeFakeEnv();
  const ch = SB.createScaleChannel({
    windowScope: fakeWindowWithBridge(env.native),
    nativeBridge: env.native,
    now: env.now, perfNow: env.perfNow,
    addEventListener: env.addEventListener, removeEventListener: env.removeEventListener,
    setInterval: env.setInterval, clearInterval: env.clearInterval,
  });
  ch.start();
  const statuses = [];
  const readings = [];
  ch.onStatus((s) => statuses.push(s.state));
  ch.onReading((r) => readings.push(r.sequence));
  env.dispatchStatus({ device: "K797", state: "scanning", message: "" });
  env.dispatchReading(goodReading({ sequence: 7 }));
  assert.deepEqual(statuses, ["scanning"]);
  assert.deepEqual(readings, [7]);
  ch.stop();
});
