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
      // device-selection API (optional; tests toggle via withDeviceApi)
      selectCalls: [], clearCalls: 0, deviceApi: false,
      getScaleDevices() { return JSON.stringify({ devices: this._devices || [], scanning: true }); },
      selectScaleDevice(id) { if (this.deviceApi) this.selectCalls.push(id); },
      clearScaleDevice() { if (this.deviceApi) this.clearCalls += 1; },
    },
    dispatchReading: (detail) => {
      (listeners[SB.READING_EVENT] || []).forEach((fn) => fn({ detail }));
    },
    dispatchStatus: (detail) => {
      (listeners[SB.STATUS_EVENT] || []).forEach((fn) => fn({ detail }));
    },
    dispatchDevices: (detail) => {
      (listeners[SB.DEVICES_EVENT] || []).forEach((fn) => fn({ detail }));
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

/* ---------- createDedupSender：值变化去重 + 心跳 + flush ---------- */
function makeDedupHarness(over) {
  const sent = [];
  let clockMs = 1000;
  const timers = [];
  const opts = Object.assign({
    now: () => clockMs,
    heartbeatMs: 2000,
    setInterval: (fn, ms) => { const id = { fn, ms }; timers.push(id); return id; },
    clearInterval: (id) => { const i = timers.indexOf(id); if (i >= 0) timers.splice(i, 1); },
  }, over || {});
  const build = (r) => ({ grams: r.grams, seq: r.sequence });
  const dedup = SB.createDedupSender((m) => sent.push(m), build, opts);
  const fireHeartbeat = () => { timers.forEach((t) => t.fn()); };
  const advance = (ms) => { clockMs += ms; };
  return { sent, dedup, fireHeartbeat, advance, timers };
}

test("dedup sender：首条必发；同值连续不重复发", () => {
  const h = makeDedupHarness();
  h.dedup.start();
  h.dedup.send(goodReading({ grams: 25.0, sequence: 1 }));
  h.dedup.send(goodReading({ grams: 25.0, sequence: 2 }));
  h.dedup.send(goodReading({ grams: 25.0, sequence: 3 }));
  assert.equal(h.sent.length, 1);
  assert.equal(h.sent[0].grams, 25.0);
});

test("dedup sender：值变化立即发（曲线保真）", () => {
  const h = makeDedupHarness();
  h.dedup.start();
  h.dedup.send(goodReading({ grams: 10.0, sequence: 1 }));
  h.dedup.send(goodReading({ grams: 10.5, sequence: 2 }));
  h.dedup.send(goodReading({ grams: 11.0, sequence: 3 }));
  assert.equal(h.sent.length, 3);
  assert.deepEqual(h.sent.map((m) => m.grams), [10.0, 10.5, 11.0]);
});

test("dedup sender：值不变超心跳周期补发一条（保活，防后端误判 stale）", () => {
  const h = makeDedupHarness({ heartbeatMs: 2000 });
  h.dedup.start();
  h.dedup.send(goodReading({ grams: 30.0, sequence: 1 })); // 首条发
  assert.equal(h.sent.length, 1);
  // 不足心跳 → 不补发
  h.advance(1500);
  h.fireHeartbeat();
  assert.equal(h.sent.length, 1);
  // 超过心跳 → 补发一条（仍是当前值）
  h.advance(600);
  h.fireHeartbeat();
  assert.equal(h.sent.length, 2);
  assert.equal(h.sent[1].grams, 30.0);
});

test("dedup sender：flush 重连后补发最新一条", () => {
  const h = makeDedupHarness();
  h.dedup.start();
  h.dedup.send(goodReading({ grams: 5.0, sequence: 1 }));
  h.dedup.send(goodReading({ grams: 5.0, sequence: 2 })); // 同值不重复发，但 pendingMsg 更新
  assert.equal(h.sent.length, 1);
  // 模拟重连
  const ok = h.dedup.flush();
  assert.equal(ok, true);
  assert.equal(h.sent.length, 2);
  assert.equal(h.sent[1].seq, 2);
});

test("dedup sender：stop 清除心跳定时器", () => {
  const h = makeDedupHarness();
  h.dedup.start();
  assert.equal(h.timers.length, 1);
  h.dedup.stop();
  assert.equal(h.timers.length, 0);
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

/* ---------- 设备选择 API（C1 契约扩展）---------- */

/* 开启设备 API 的 native（在 makeFakeEnv 基础上） */
function nativeWithDevices(env) {
  env.native.deviceApi = true;
  return env.native;
}

test("detectDeviceSupport: 三方法齐全返回 true；缺一返回 false", () => {
  const env = makeFakeEnv();
  // 默认 fake native 没开启 deviceApi（selectScaleDevice 不计入）——这里直接构造
  const full = {
    startScaleScan() {}, stopScaleScan() {}, getScaleStatus() {},
    getScaleDevices() {}, selectScaleDevice() {}, clearScaleDevice() {},
  };
  assert.equal(SB.detectDeviceSupport({ MiceAutomaticScale: full }), true);
  // 缺 selectScaleDevice
  assert.equal(SB.detectDeviceSupport({ MiceAutomaticScale: {
    getScaleDevices() {}, clearScaleDevice() {},
  } }), false);
  // 无桥
  assert.equal(SB.detectDeviceSupport({}), false);
  assert.equal(SB.detectDeviceSupport(null), false);
});

test("channel start：带 deviceId 且支持选择 API → startScaleScan 后调 selectScaleDevice", () => {
  const env = makeFakeEnv();
  nativeWithDevices(env);
  const ch = SB.createScaleChannel({
    windowScope: fakeWindowWithBridge(env.native),
    nativeBridge: env.native,
    deviceId: "AA:BB:CC:DD:EE:FF",
    now: env.now, perfNow: env.perfNow,
    addEventListener: env.addEventListener, removeEventListener: env.removeEventListener,
    setInterval: env.setInterval, clearInterval: env.clearInterval,
  });
  ch.start();
  assert.equal(env.native.startScaleScanCalls, 1);
  assert.deepEqual(env.native.selectCalls, ["AA:BB:CC:DD:EE:FF"]);
  ch.stop();
});

test("channel start：不支持选择 API（legacy app）→ 不调 selectScaleDevice", () => {
  const env = makeFakeEnv();
  // deviceApi 默认 false：selectScaleDevice 不会推入 selectCalls
  const ch = SB.createScaleChannel({
    windowScope: fakeWindowWithBridge(env.native),
    nativeBridge: env.native,
    deviceId: "AA:BB:CC:DD:EE:FF",
    now: env.now, perfNow: env.perfNow,
    addEventListener: env.addEventListener, removeEventListener: env.removeEventListener,
    setInterval: env.setInterval, clearInterval: env.clearInterval,
  });
  ch.start();
  assert.equal(env.native.startScaleScanCalls, 1);
  assert.deepEqual(env.native.selectCalls, []);
  ch.stop();
});

test("channel start：无 deviceId（设备发现模式）→ 不调 selectScaleDevice", () => {
  const env = makeFakeEnv();
  nativeWithDevices(env);
  const ch = SB.createScaleChannel({
    windowScope: fakeWindowWithBridge(env.native),
    nativeBridge: env.native,
    now: env.now, perfNow: env.perfNow,
    addEventListener: env.addEventListener, removeEventListener: env.removeEventListener,
    setInterval: env.setInterval, clearInterval: env.clearInterval,
  });
  ch.start();
  assert.deepEqual(env.native.selectCalls, []);
  ch.stop();
});

test("DEVICES_EVENT 合法 payload → onDevices 触发，getState 含 devices/selectedDeviceId", () => {
  const env = makeFakeEnv();
  nativeWithDevices(env);
  const ch = SB.createScaleChannel({
    windowScope: fakeWindowWithBridge(env.native),
    nativeBridge: env.native,
    now: env.now, perfNow: env.perfNow,
    addEventListener: env.addEventListener, removeEventListener: env.removeEventListener,
    setInterval: env.setInterval, clearInterval: env.clearInterval,
  });
  ch.start();
  const seen = [];
  ch.onDevices((d) => seen.push(d));
  env.dispatchDevices({
    devices: [
      { deviceId: "AA:BB:CC:DD:EE:FF", name: "K797", rssi: -52, grams: 250.0, lastSeenAtEpochMs: 1785393390194 },
      { deviceId: "11:22:33:44:55:66", name: "K797-2", rssi: -70, grams: null },
    ],
    scanning: true,
    selectedDeviceId: "AA:BB:CC:DD:EE:FF",
  });
  assert.equal(seen.length, 1);
  assert.equal(seen[0].devices.length, 2);
  assert.equal(seen[0].devices[0].name, "K797");
  assert.equal(seen[0].devices[1].grams, null);
  assert.equal(seen[0].selectedDeviceId, "AA:BB:CC:DD:EE:FF");
  const st = ch.getState();
  assert.equal(st.devices.length, 2);
  assert.equal(st.selectedDeviceId, "AA:BB:CC:DD:EE:FF");
  assert.equal(st.deviceSupport, true);
  ch.stop();
  // stop 后不再触发
  env.dispatchDevices({ devices: [], scanning: true });
  assert.equal(seen.length, 1);
});

test("DEVICES_EVENT 非法 payload 被丢弃（不触发 onDevices，不污染 state）", () => {
  const env = makeFakeEnv();
  nativeWithDevices(env);
  const ch = SB.createScaleChannel({
    windowScope: fakeWindowWithBridge(env.native),
    nativeBridge: env.native,
    now: env.now, perfNow: env.perfNow,
    addEventListener: env.addEventListener, removeEventListener: env.removeEventListener,
    setInterval: env.setInterval, clearInterval: env.clearInterval,
  });
  ch.start();
  const seen = [];
  ch.onDevices((d) => seen.push(d));
  // 非 object
  env.dispatchDevices(null);
  env.dispatchDevices("oops");
  // devices 非数组
  env.dispatchDevices({ devices: "x" });
  // 单项缺 deviceId
  env.dispatchDevices({ devices: [{ name: "x", rssi: -50 }] });
  // rssi 非有限数
  env.dispatchDevices({ devices: [{ deviceId: "a", name: "x", rssi: "bad" }] });
  // grams 非法（负数）
  env.dispatchDevices({ devices: [{ deviceId: "a", name: "x", rssi: -50, grams: -1 }] });
  // grams 非数非 null
  env.dispatchDevices({ devices: [{ deviceId: "a", name: "x", rssi: -50, grams: "x" }] });
  assert.equal(seen.length, 0, "所有非法 payload 应被丢弃");
  assert.equal(ch.getState().devices.length, 0);
  ch.stop();
});

test("selectDevice / clearDevice 转发到 nativeBridge；legacy app no-op", () => {
  const env = makeFakeEnv();
  nativeWithDevices(env);
  const ch = SB.createScaleChannel({
    windowScope: fakeWindowWithBridge(env.native),
    nativeBridge: env.native,
    now: env.now, perfNow: env.perfNow,
    addEventListener: env.addEventListener, removeEventListener: env.removeEventListener,
    setInterval: env.setInterval, clearInterval: env.clearInterval,
  });
  ch.start();
  ch.selectDevice("AA:BB:CC:DD:EE:FF");
  ch.clearDevice();
  assert.deepEqual(env.native.selectCalls, ["AA:BB:CC:DD:EE:FF"]);
  assert.equal(env.native.clearCalls, 1);
  ch.stop();
});

test("selectDevice / clearDevice：无设备 API 时不抛错且不调用", () => {
  const env = makeFakeEnv();
  // deviceApi false
  const ch = SB.createScaleChannel({
    windowScope: fakeWindowWithBridge(env.native),
    nativeBridge: env.native,
    now: env.now, perfNow: env.perfNow,
    addEventListener: env.addEventListener, removeEventListener: env.removeEventListener,
    setInterval: env.setInterval, clearInterval: env.clearInterval,
  });
  ch.start();
  // 不应抛错
  ch.selectDevice("AA:BB:CC:DD:EE:FF");
  ch.clearDevice();
  assert.deepEqual(env.native.selectCalls, []);
  ch.stop();
});

test("stop 卸载 DEVICES_EVENT 监听（同 READING/STATUS）", () => {
  const env = makeFakeEnv();
  nativeWithDevices(env);
  const ch = SB.createScaleChannel({
    windowScope: fakeWindowWithBridge(env.native),
    nativeBridge: env.native,
    now: env.now, perfNow: env.perfNow,
    addEventListener: env.addEventListener, removeEventListener: env.removeEventListener,
    setInterval: env.setInterval, clearInterval: env.clearInterval,
  });
  ch.start();
  assert.ok(env.listeners[SB.DEVICES_EVENT] && env.listeners[SB.DEVICES_EVENT].length > 0);
  ch.stop();
  assert.equal((env.listeners[SB.DEVICES_EVENT] || []).length, 0);
});

test("refreshDevices：合法 JSON → 分发 onDevices + 更新 getState，返回 true", () => {
  const env = makeFakeEnv();
  nativeWithDevices(env);
  env.native._devices = [
    { deviceId: "AA:BB:CC:DD:EE:FF", name: "K797", rssi: -52, grams: 250.0, lastSeenAtEpochMs: 1785393390194 },
  ];
  const ch = SB.createScaleChannel({
    windowScope: fakeWindowWithBridge(env.native),
    nativeBridge: env.native,
    now: env.now, perfNow: env.perfNow,
    addEventListener: env.addEventListener, removeEventListener: env.removeEventListener,
    setInterval: env.setInterval, clearInterval: env.clearInterval,
  });
  ch.start();
  const seen = [];
  ch.onDevices((d) => seen.push(d));
  const ok = ch.refreshDevices();
  assert.equal(ok, true);
  assert.equal(seen.length, 1);
  assert.equal(seen[0].devices.length, 1);
  assert.equal(seen[0].devices[0].deviceId, "AA:BB:CC:DD:EE:FF");
  assert.equal(ch.getState().devices.length, 1);
  ch.stop();
});

test("refreshDevices：非法 payload（grams 超界）→ 返回 false 且不分发", () => {
  const env = makeFakeEnv();
  nativeWithDevices(env);
  env.native.getScaleDevices = function () {
    return JSON.stringify({ devices: [{ deviceId: "X", name: "K797", rssi: -52, grams: 99999 }], scanning: true });
  };
  const ch = SB.createScaleChannel({
    windowScope: fakeWindowWithBridge(env.native),
    nativeBridge: env.native,
    now: env.now, perfNow: env.perfNow,
    addEventListener: env.addEventListener, removeEventListener: env.removeEventListener,
    setInterval: env.setInterval, clearInterval: env.clearInterval,
  });
  ch.start();
  const seen = [];
  ch.onDevices((d) => seen.push(d));
  assert.equal(ch.refreshDevices(), false);
  assert.equal(seen.length, 0);
  assert.equal(ch.getState().devices.length, 0);
  ch.stop();
});

test("refreshDevices：无设备 API（legacy app）→ 返回 false 不抛错", () => {
  const env = makeFakeEnv();
  const legacy = {
    startScaleScan() {}, stopScaleScan() {}, getScaleStatus() { return ""; },
  };
  const ch = SB.createScaleChannel({
    windowScope: fakeWindowWithBridge(legacy),
    nativeBridge: legacy,
    now: env.now, perfNow: env.perfNow,
    addEventListener: env.addEventListener, removeEventListener: env.removeEventListener,
    setInterval: env.setInterval, clearInterval: env.clearInterval,
  });
  ch.start();
  assert.equal(ch.refreshDevices(), false);
  ch.stop();
});
