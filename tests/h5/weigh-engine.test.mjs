/* 实时称重判定状态机 (weigh-engine.js) 单元测试 — node:test，零依赖。
 * 运行：node --test tests/h5/weigh-engine.test.mjs
 *
 * 用例对齐 tests/test_realtime.py 的 BLE 相关行为（test_three_consistent_reads_*、
 * test_platform_switch_*、test_stale_epoch_*、test_weighing_mouse_leaves、
 * test_announced_accept / retry、test_clear_timeout 等），改写为读数驱动 +
 * 注入假时钟推进。状态字符串与 Python RealtimeState 严格一致。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const WE = require("../../ui/static/weigh-engine.js");

/* ------------------------- harness ------------------------- */

/* 构造一条合法 BLE 读数。grams 与 raw 自动对齐（raw=grams*10）。 */
function rd(grams, seq, over) {
  const r = {
    grams: grams,
    raw: Math.round(grams * 10),
    sequence: seq,
    receivedAtEpochMs: seq * 200, // 200ms 间隔，落在 1.6s 稳定窗内
  };
  return Object.assign(r, over || {});
}

/* 带可控时钟（ms）的会话工厂。events 收集所有 onEvent 回调。 */
function makeSession(configOver, over) {
  let clockMs = 0;
  const events = [];
  const opts = Object.assign(
    {
      config: Object.assign({}, configOver || {}),
      now: () => clockMs,
      onEvent: (type, payload) => events.push({ type, payload }),
    },
    over || {}
  );
  const session = WE.createSession(opts);
  return {
    session,
    events,
    clockMs: () => clockMs,
    advance: (ms) => { clockMs += ms; },
    setClock: (ms) => { clockMs = ms; },
    // 推进一次 tick（供无新读数也要推进的场景）
    tick: () => session.tick(),
    // 注入读数（先推进时钟到给定 ms，再 ingest）
    feed: (grams, seq, opts2) => {
      const atMs = (opts2 && typeof opts2.atMs === "number") ? opts2.atMs : clockMs;
      clockMs = atMs;
      return session.ingestReading(rd(grams, seq, opts2 && opts2.over));
    },
    // 用自定义 receivedAtEpochMs 注入读数
    feedAt: (grams, seq, receivedAtEpochMs, atMs) => {
      const t = (typeof atMs === "number") ? atMs : clockMs;
      clockMs = t;
      return session.ingestReading(
        rd(grams, seq, { receivedAtEpochMs: receivedAtEpochMs })
      );
    },
  };
}

/* 从 calibrating 推进到 armed：连续 calibrate_min_reads 条空秤读数。
 * 默认 calibrate_min_reads=3。 */
function reachArmed(h, seqStart) {
  let seq = seqStart || 0;
  const n = h.session.getConfig().calibrate_min_reads;
  for (let i = 0; i < n; i++) {
    h.feed(0.0, seq++);
  }
  assert.equal(h.session.getState().state, "armed", "should be armed after empty reads");
  return seq;
}

/* 从 armed 推进到 weighing：连续 enter_sustain_frames 条 >enter_min 读数。
 * armed 证据延续进 weighing（rawWindow 不清空）。返回下一条 seq。 */
function reachWeighing(h, grams, seqStart) {
  let seq = seqStart;
  const n = h.session.getConfig().enter_sustain_frames;
  for (let i = 0; i < n; i++) {
    h.feed(grams, seq++);
  }
  assert.equal(h.session.getState().state, "weighing", "should be weighing after sustains");
  return seq;
}

/* ------------------------- 1. 读数校验 ------------------------- */

test("读数校验：非法 grams/raw/sequence 乱序被拒，缓存不变", () => {
  const h = makeSession();
  // 非法 grams
  assert.equal(h.session.ingestReading(rd(NaN, 1)), false);
  assert.equal(h.session.ingestReading(rd(Infinity, 1)), false);
  assert.equal(h.session.ingestReading(rd(-1, 1)), false);
  assert.equal(h.session.ingestReading(rd(9999, 1)), false);
  // 非法 raw（非整数 / 越界）
  assert.equal(h.session.ingestReading({ grams: 1.0, raw: 1.5, sequence: 1, receivedAtEpochMs: 0 }), false);
  assert.equal(h.session.ingestReading({ grams: 1.0, raw: 70000, sequence: 1, receivedAtEpochMs: 0 }), false);
  assert.equal(h.session.ingestReading({ grams: 1.0, raw: -1, sequence: 1, receivedAtEpochMs: 0 }), false);
  // grams 与 raw 不一致
  assert.equal(h.session.ingestReading({ grams: 5.0, raw: 50, sequence: 1, receivedAtEpochMs: 0 }), true); // 一致
  assert.equal(h.session.ingestReading({ grams: 5.5, raw: 50, sequence: 2, receivedAtEpochMs: 200 }), false); // 差 0.5 > 0.05
  // 非法 sequence（非整数 / 负）
  assert.equal(h.session.ingestReading({ grams: 1.0, raw: 10, sequence: 1.5, receivedAtEpochMs: 0 }), false);
  assert.equal(h.session.ingestReading({ grams: 1.0, raw: 10, sequence: -1, receivedAtEpochMs: 0 }), false);
  // 非 object
  assert.equal(h.session.ingestReading(null), false);

  // 乱序：sequence 必须 > 上次。先喂 seq=10 成功，再 seq<=10 被忽略。
  h.session.reset();
  assert.equal(h.session.ingestReading({ grams: 0.0, raw: 0, sequence: 10, receivedAtEpochMs: 0 }), true);
  assert.equal(h.session.ingestReading({ grams: 0.0, raw: 0, sequence: 10, receivedAtEpochMs: 200 }), false); // 相等
  assert.equal(h.session.ingestReading({ grams: 0.0, raw: 0, sequence: 5, receivedAtEpochMs: 400 }), false); // 回退
  assert.equal(h.session.ingestReading({ grams: 0.0, raw: 0, sequence: 11, receivedAtEpochMs: 600 }), true); // 正常
  // 校验通过但乱序的读数不应推进 calibrating（只 2 条有效空秤读数 < 3，仍 calibrating）
  assert.equal(h.session.getState().state, "calibrating");
  assert.equal(h.session.getState().lastGrams, 0.0);
});

/* ------------------------- 2. 校准 ------------------------- */

test("校准：连续 3 条空秤读数 → armed；中途混入 >empty_max → 重新计数", () => {
  const h = makeSession();
  // 2 条空秤 → 仍 calibrating
  h.feed(0.0, 0);
  h.feed(0.0, 1);
  assert.equal(h.session.getState().state, "calibrating");
  // 第 3 条非空（>empty_max 0.15）→ 清零计数
  h.feed(2.0, 2);
  assert.equal(h.session.getState().state, "calibrating");
  // 再来 1 条空秤 → 计数=1，仍 calibrating
  h.feed(0.0, 3);
  assert.equal(h.session.getState().state, "calibrating");
  // 凑齐 3 条空秤 → armed
  h.feed(0.0, 4);
  h.feed(0.0, 5);
  assert.equal(h.session.getState().state, "armed");
});

test("校准：calibrate_min_reads 可配置", () => {
  const h = makeSession({ calibrate_min_reads: 1 });
  h.feed(0.0, 0);
  assert.equal(h.session.getState().state, "armed");
});

/* ------------------------- 3. armed → weighing ------------------------- */

test("armed→weighing：连续 2 条 >enter_min → weighing", () => {
  const h = makeSession();
  let seq = reachArmed(h, 0);
  // 1 条 >enter_min → 仍 armed，但 rawWindow 已累积 1 条
  h.feed(20.0, seq++);
  assert.equal(h.session.getState().state, "armed");
  // 第 2 条 → weighing（armed 证据延续，不清空 rawWindow）
  h.feed(20.0, seq++);
  assert.equal(h.session.getState().state, "weighing");
});

test("armed：读数 <=enter_min 或无效 → 清空 rawWindow 与 enterSustain", () => {
  const h = makeSession();
  let seq = reachArmed(h, 0);
  h.feed(20.0, seq++); // 1 条 >enter_min
  assert.equal(h.session.getState().state, "armed");
  // 回落到 <=enter_min → 清空
  h.feed(0.5, seq++);
  assert.equal(h.session.getState().state, "armed");
});

/* ------------------------- 4. 三条一致不立即播报 ------------------------- */

test("三条一致读数不立即播报（形成 pending），第 4 条确认 → announced", () => {
  const h = makeSession();
  let seq = reachArmed(h, 0);
  // 2 条 >enter_min → weighing，rawWindow 已有 2 条
  seq = reachWeighing(h, 20.0, seq);
  // 第 3 条一致读数 → 形成 pending，不播报
  h.feed(20.0, seq++);
  assert.equal(h.session.getState().state, "weighing", "3×consistent must not announce");
  // 第 4 条确认读数 → announced
  h.feed(20.0, seq++);
  assert.equal(h.session.getState().state, "announced");
  const announce = h.events.find((e) => e.type === "announce");
  assert.ok(announce, "should emit announce event");
  assert.ok(Math.abs(announce.payload.weight_g - 20.0) < 0.01);
  assert.equal(announce.payload.weight_raw, 200); // 20.0g → raw 200
});

/* ------------------------- 5. 平台切换 ------------------------- */

test("平台切换：16.14×3 后 15.62×3，不锁死旧平台，最终报 15.62", () => {
  const h = makeSession();
  let seq = reachArmed(h, 0);
  // 2 条 16.14 → weighing，rawWindow 有 2 条
  seq = reachWeighing(h, 16.14, seq);
  // 第 3 条 16.14 → pending，不播报
  h.feed(16.14, seq++);
  assert.equal(h.session.getState().state, "weighing", "3×16.14 must not announce");
  // 平台切换到 15.62：候选被撤销，重新累积
  let announced = null;
  for (let i = 0; i < 8; i++) {
    h.feed(15.62, seq++);
    const st = h.session.getState();
    if (st.state === "announced") {
      announced = st.weightCandidate;
      break;
    }
  }
  assert.ok(announced !== null, "should eventually announce new platform 15.62");
  assert.ok(Math.abs(announced - 15.62) < 0.05, "announced=" + announced);
  assert.ok(Math.abs(announced - 16.14) > 0.1, "must not announce old 16.14");
});

/* ------------------------- 6. 早退 ------------------------- */

test("早退：weighing 中连续 2 条 <=leave_max → 回 armed", () => {
  const h = makeSession();
  let seq = reachArmed(h, 0);
  seq = reachWeighing(h, 20.0, seq);
  assert.equal(h.session.getState().state, "weighing");
  // 连续 2 条 <=leave_max（0.30）→ 回 armed
  h.feed(0.2, seq++);
  assert.equal(h.session.getState().state, "weighing"); // 1 条还不够
  h.feed(0.2, seq++);
  assert.equal(h.session.getState().state, "armed");
});

test("早退：>leave_max 或有效读数清零 leaveCount", () => {
  const h = makeSession();
  let seq = reachArmed(h, 0);
  seq = reachWeighing(h, 20.0, seq);
  h.feed(0.2, seq++); // leaveCount=1
  h.feed(20.0, seq++); // >leave_max 清零
  assert.equal(h.session.getState().state, "weighing");
  h.feed(0.2, seq++); // leaveCount=1（重新开始）
  assert.equal(h.session.getState().state, "weighing");
});

/* ------------------------- 7. accept → wait_clear ------------------------- */

test("accept → wait_clear；读数 <=empty_max 一次 → ready_next + 回 armed，mouseCount+1", () => {
  const h = makeSession();
  let seq = reachArmed(h, 0);
  seq = reachWeighing(h, 22.5, seq);
  h.feed(22.5, seq++); // 第 3 条 → pending
  h.feed(22.5, seq++); // 第 4 条 → announced
  assert.equal(h.session.getState().state, "announced");
  assert.equal(h.session.getState().mouseCount, 0);

  // accept → wait_clear
  const attempt = h.session.accept();
  assert.ok(attempt !== null);
  assert.equal(h.session.getState().state, "wait_clear");
  assert.equal(h.session.getState().mouseCount, 1);
  const acceptEv = h.events.filter((e) => e.type === "accept");
  assert.equal(acceptEv.length, 1);
  assert.equal(acceptEv[0].payload.ordinal, 1);
  assert.ok(Math.abs(acceptEv[0].payload.weight_g - 22.5) < 0.01);

  // 读数 >empty_max → 不清秤，仍 wait_clear
  h.feed(5.0, seq++);
  assert.equal(h.session.getState().state, "wait_clear");
  // 读数 <=empty_max 一次 → ready_next + 回 armed
  h.feed(0.0, seq++);
  assert.equal(h.session.getState().state, "armed");
  const readyNext = h.events.filter((e) => e.type === "ready_next");
  assert.equal(readyNext.length, 1);
});

test("accept 仅在 announced 态生效；其他态返回 null", () => {
  const h = makeSession();
  reachArmed(h, 0);
  assert.equal(h.session.getState().state, "armed");
  assert.equal(h.session.accept(), null);
});

/* ------------------------- 8. retry → weighing，epoch 隔离 ------------------------- */

test("retry → 回 weighing，epoch 隔离（旧读数不进新窗口）", () => {
  const h = makeSession();
  let seq = reachArmed(h, 0);
  seq = reachWeighing(h, 22.5, seq);
  h.feed(22.5, seq++); // 第 3 条 → pending
  h.feed(22.5, seq++); // 第 4 条 → announced
  assert.equal(h.session.getState().state, "announced");

  const info = h.session.retry();
  assert.equal(info.applied, true);
  assert.equal(h.session.getState().state, "weighing");
  const epoch = info.epoch;
  // 当前 attempt 被标记 rejected
  // 重新喂新鲜读数，最终报 22.5（而不是任何旧平台残留）
  let announced = null;
  for (let i = 0; i < 6; i++) {
    h.feed(22.5, seq++);
    const st = h.session.getState();
    if (st.state === "announced") { announced = st.weightCandidate; break; }
  }
  assert.ok(announced !== null, "should re-announce after retry");
  assert.ok(Math.abs(announced - 22.5) < 0.05);
  // epoch 应该递增过（≥1）
  assert.ok(epoch >= 1);
});

test("retry 仅在 announced 态生效", () => {
  const h = makeSession();
  reachArmed(h, 0);
  const info = h.session.retry();
  assert.equal(info.applied, false);
  assert.equal(h.session.getState().state, "armed");
});

/* ------------------------- 9. stale ------------------------- */

test("stale：超 ble_stale_s 无读数 → 视为无新鲜读数，状态推进暂停 + 'stale' 事件", () => {
  const h = makeSession({ ble_stale_s: 10.0 });
  let seq = reachArmed(h, 0);
  // 进入 weighing：1 条 >enter_min
  h.feed(20.0, seq++);
  assert.equal(h.session.getState().state, "armed");
  // 时钟推进使读数仍新鲜（< 10s），喂第 2 条 → weighing
  h.advance(1000);
  h.feed(20.0, seq++);
  assert.equal(h.session.getState().state, "weighing");
  // 推进超过 ble_stale_s（10s），tick 时读数已过期 → 视为 None
  h.advance(11000);
  const beforeStaleEvents = h.events.filter((e) => e.type === "stale").length;
  h.tick();
  // stale 事件应被下发（边沿触发：从 false → true）
  const staleEvents = h.events.filter((e) => e.type === "stale");
  assert.equal(staleEvents.length - beforeStaleEvents, 1);
  assert.equal(staleEvents[staleEvents.length - 1].payload.stale, true);
  // 状态仍是 weighing（暂停，未推进）；多 tick 几次也不变
  h.tick();
  h.tick();
  assert.equal(h.session.getState().state, "weighing");
  // 收到新读数 → 恢复新鲜，stale 边沿回落
  const beforeStale2 = h.events.filter((e) => e.type === "stale").length;
  h.feed(20.0, seq++);
  const staleEvents2 = h.events.filter((e) => e.type === "stale");
  assert.equal(staleEvents2.length - beforeStale2, 1);
  assert.equal(staleEvents2[staleEvents2.length - 1].payload.stale, false);
});

/* ------------------------- 10. wait_clear 超时 ------------------------- */

test("wait_clear 超时（30s 无空秤）→ 回 armed", () => {
  const h = makeSession({ clear_timeout_s: 30.0 });
  let seq = reachArmed(h, 0);
  seq = reachWeighing(h, 22.5, seq);
  h.feed(22.5, seq++); // pending
  h.feed(22.5, seq++); // announced
  h.session.accept();
  assert.equal(h.session.getState().state, "wait_clear");

  // 期间持续有非空读数（小鼠未取走），不回 armed
  for (let i = 0; i < 5; i++) {
    h.advance(2000);
    h.feed(5.0, seq++);
    assert.equal(h.session.getState().state, "wait_clear");
  }
  // 推进到超过 clear_timeout_s（30s）后 tick → 回 armed
  h.advance(21000); // 累计 10s + 21s = 31s > 30s
  h.tick();
  assert.equal(h.session.getState().state, "armed");
});

/* ------------------------- stable_min_span_ms（确认期跨度门槛）------------------------- */

test("stable_min_span_ms>0：候选跨度不足不播报，跨度满足后播报", () => {
  // 读数间隔 200ms，要求确认期跨度 >= 500ms：4 条读数（首候选 + 1 确认）
  // 在默认读数间隔下 confirmCount=1 时跨度仅 200ms < 500ms，需再等下一条。
  const h = makeSession({ stable_min_span_ms: 500.0 });
  let seq = reachArmed(h, 0);
  seq = reachWeighing(h, 20.0, seq); // 2 条（ts 0/200）
  h.feed(20.0, seq++); // 第 3 条（ts 400）→ pending
  h.feed(20.0, seq++); // 第 4 条（ts 600）→ confirmCount=1，跨度 600-400=200 < 500 → 不播报
  assert.equal(h.session.getState().state, "weighing");
  h.feed(20.0, seq++); // 第 5 条（ts 800）→ confirmCount=2，跨度 800-400=400 < 500 → 不播报
  assert.equal(h.session.getState().state, "weighing");
  h.feed(20.0, seq++); // 第 6 条（ts 1000）→ confirmCount=3，跨度 1000-400=600 >= 500 → 播报
  assert.equal(h.session.getState().state, "announced");
});

/* ------------------------- announce_hold_s（自动接受）------------------------- */

test("announce_hold_s>0：播报后超时自动 accept → wait_clear", () => {
  const h = makeSession({ announce_hold_s: 2.0 });
  let seq = reachArmed(h, 0);
  seq = reachWeighing(h, 20.0, seq);
  h.feed(20.0, seq++); // pending
  h.feed(20.0, seq++); // announced
  assert.equal(h.session.getState().state, "announced");
  // 推进 < announce_hold_s：仍 announced
  h.advance(1500);
  h.tick();
  assert.equal(h.session.getState().state, "announced");
  // 推进到 >= announce_hold_s（2s）：tick 触发自动 accept → wait_clear
  h.advance(600); // 累计 2.1s
  h.tick();
  assert.equal(h.session.getState().state, "wait_clear");
  assert.equal(h.session.getState().mouseCount, 1);
});

/* ------------------------- 配置校验 ------------------------- */

test("validateConfig：非法取值抛错", () => {
  assert.throws(() => WE.createSession({ config: { stable_min_raw_reads: 1 } }));
  assert.throws(() => WE.createSession({ config: { stable_confirm_raw_reads: -1 } }));
  assert.throws(() => WE.createSession({ config: { stable_max_age_s: 0 } }));
  assert.throws(() => WE.createSession({ config: { stable_weight_tol: 0 } }));
  assert.throws(() => WE.createSession({ config: { calibrate_min_reads: 0 } }));
  assert.throws(() => WE.createSession({ config: { enter_sustain_frames: 0 } }));
});

/* ------------------------- median / round2 单元 ------------------------- */

test("median：奇偶长度与 numpy.median 一致", () => {
  assert.equal(WE.median([3]), 3);
  assert.equal(WE.median([1, 3]), 2);
  assert.equal(WE.median([1, 2, 3]), 2);
  assert.equal(WE.median([1, 2, 3, 4]), 2.5);
  assert.equal(WE.median([10, 20, 30, 40, 50]), 30);
  // 不修改原数组
  const arr = [3, 1, 2];
  WE.median(arr);
  assert.deepEqual(arr, [3, 1, 2]);
});

test("round2：两位小数四舍五入", () => {
  assert.equal(WE.round2(20.005), 20.01); // JS Math.round half-up
  assert.equal(WE.round2(15.625), 15.63);
  assert.equal(WE.round2(0.0), 0);
});

/* ------------------------- reset ------------------------- */

test("reset：回到 calibrating，清空所有证据", () => {
  const h = makeSession();
  let seq = reachArmed(h, 0);
  seq = reachWeighing(h, 20.0, seq);
  assert.equal(h.session.getState().state, "weighing");
  h.session.reset();
  const st = h.session.getState();
  assert.equal(st.state, "calibrating");
  assert.equal(st.mouseCount, 0);
  assert.equal(st.weightCandidate, null);
  assert.equal(st.lastGrams, null);
});

/* ------------------------- tick 不注入证据（真机 bug 回归）-------------------------
 * 修复前：tick() 用缓存读数调 advance()，armed/weighing 会把同一条缓存读数
 * 反复 appendRawRead 进证据窗 → 同一重量 ~0.5s 凑满稳定条件 → 重量未稳即播报。
 * 修复后：tick() 只推进超时/stale，绝不注入证据。以下锁定该行为。 */

test("tick 不注入证据：weighing 中反复 tick 不累计稳定读数、不播报", () => {
  const h = makeSession();
  let seq = reachArmed(h, 0);
  seq = reachWeighing(h, 20.0, seq); // weighing（2 条 20.0 已入窗）
  // 不再喂新读数，只反复 tick（模拟 150ms 定时器在恒定重量下空转）
  for (let i = 0; i < 30; i++) {
    h.advance(150);
    h.tick();
  }
  // 证据窗没有新增读数 → 凑不满 stable_min_raw_reads 的"新"稳定段 → 绝不播报
  assert.equal(h.session.getState().state, "weighing");
  const announced = h.events.filter((e) => e.type === "announce");
  assert.equal(announced.length, 0, "tick 空转绝不应触发 announce");
});

test("tick 不注入证据：armed 中反复 tick 不会凭空进 weighing", () => {
  const h = makeSession();
  reachArmed(h, 0);
  // armed 态不放鼠（无新读数），反复 tick
  for (let i = 0; i < 20; i++) {
    h.advance(150);
    h.tick();
  }
  assert.equal(h.session.getState().state, "armed");
});

test("tick 注入证据修复后：只有真实新读数才能推进稳定判定", () => {
  const h = makeSession();
  let seq = reachArmed(h, 0);
  seq = reachWeighing(h, 20.0, seq);
  // 真实新读数（间隔 200ms）才能累积稳定段并播报
  h.feed(20.0, seq++); // 第3条 → pending
  assert.equal(h.session.getState().state, "weighing");
  h.feed(20.0, seq++); // 第4条 → confirm → announced
  assert.equal(h.session.getState().state, "announced");
  assert.ok(Math.abs(h.session.getState().weightCandidate - 20.0) < 0.05);
});
