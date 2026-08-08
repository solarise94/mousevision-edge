/* 本地称重控制器 (local-weigh.js)
 *
 * 纯 app 化的中间层：把本地称重引擎（WeighEngine）、BLE 天平通道
 * （ScaleBridge channel）、记录累积、离线队列（ReportClient outbox）、
 * 崩溃草稿持久化（localStorage）串成一个可被 UI 薄层（mobile.js）调用的
 * 控制器。称重判定 / 记录 / 离线全在本地完成，UI 只订阅 onEvent 回调并
 * 调用语义化方法（accept/retry/submitManual/finishBox）。
 *
 * 职责边界：
 *   - 控制器**不**负责 create/start/stop scaleChannel（由 mobile.js 管），
 *     只通过 scaleChannel.onReading 订阅读数。
 *   - 控制器**负责** create engine session（announce/post_match 模式），
 *     并驱动 ~150ms tick 定时器。
 *   - 控制器**负责** buildRecord + 草稿持久化 + finishBox 时 outbox.enqueue。
 *
 * 设计目标：
 *   1) 崩溃安全：每"确认一只"立即把整个在录批次草稿写 storage；start() 时
 *      恢复未完成草稿。app 被杀进程/崩溃后记录不丢。
 *   2) 零依赖、依赖注入便于测试：weighEngine / scaleChannel / outbox /
 *      storage / now / 定时器工厂全部可注入。
 *   3) 模式差异集中在 accept 路径：announce 模式由 UI 调 accept() 确认；
 *      post_match 模式在 engine 'announce' 时自动 accept；manual 不建引擎。
 *
 * UMD：浏览器挂 window.LocalWeigh；node 测试 require 该模块。
 * 风格参照同目录 weigh-engine.js / report-client.js。
 */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (root && typeof root === "object") {
    root.LocalWeigh = api;
  }
})(typeof window !== "undefined" ? window : this, function () {
  "use strict";

  // 读数范围（K797 量程，与 weigh-engine.js / scale-bridge.js 一致）
  var MAX_GRAMS = 6553.5;

  // manual 模式：读数超过该时长视为 stale（人眼判定后点按钮录入，期间读数应保持新鲜）
  var MANUAL_STALE_MS = 2500;

  // 改进 A：manual 模式"清秤门槛"——成功录入一只后，要求天平读数回落到 ≤该克数
  // 才允许录入下一只（防止上一只还没拿走/残留重量导致连点生成重复记录）。
  var MANUAL_CLEAR_THRESHOLD_G = 3;
  // 改进 A：manual 模式防抖——两次成功录入之间至少间隔该毫秒数（防止抖动/误连点）。
  var MANUAL_MIN_INTERVAL_MS = 800;

  // engine tick 周期（处理 wait_clear 超时等无新读数也要推进的情况）
  var DEFAULT_TICK_MS = 150;

  // 草稿存储键前缀（按笼号隔离）
  var DRAFT_KEY_PREFIX = "mv.weighDraft.v1.";

  // 三种合法模式
  var MODES = { announce: true, post_match: true, manual: true };

  // 默认 weight_source 映射
  var DEFAULT_WEIGHT_SOURCE = {
    announce: "ble_k797",
    post_match: "ble_k797",
    manual: "manual",
  };

  /* ------------------------------------------------------------------ *
   * 草稿存储键。
   * ------------------------------------------------------------------ */
  function draftKey(cageId) {
    return DRAFT_KEY_PREFIX + String(cageId);
  }

  /* ------------------------------------------------------------------ *
   * 写草稿（崩溃安全）：把整个在录批次序列化进 storage。
   * 失败（quota / 不可写）吞错——记录已入内存批次，finishBox 时仍会入队。
   * ------------------------------------------------------------------ */
  function writeDraft(storage, cageId, draft) {
    if (!storage || typeof storage.setItem !== "function") return;
    try {
      storage.setItem(draftKey(cageId), JSON.stringify(draft));
    } catch (_) {
      // 存储失败不阻断主流程；调用方可通过 getState().pendingCount 与
      // mouseCount 监控。finishBox 成功后会清草稿。
    }
  }

  /* ------------------------------------------------------------------ *
   * 读草稿。返回解析后的草稿对象或 null（无 / 损坏）。
   * ------------------------------------------------------------------ */
  function readDraft(storage, cageId) {
    if (!storage || typeof storage.getItem !== "function") return null;
    try {
      var raw = storage.getItem(draftKey(cageId));
      if (!raw || typeof raw !== "string") return null;
      var parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object") return null;
      if (!Array.isArray(parsed.records)) return null;
      return parsed;
    } catch (_) {
      return null;
    }
  }

  /* ------------------------------------------------------------------ *
   * 删草稿（finishBox 成功后）。
   * ------------------------------------------------------------------ */
  function clearDraft(storage, cageId) {
    if (!storage || typeof storage.removeItem !== "function") return;
    try {
      storage.removeItem(draftKey(cageId));
    } catch (_) {}
  }

  /* ================================================================== *
   * createController(opts)
   * ================================================================== */
  function createController(opts) {
    opts = opts || {};

    var mode = opts.mode;
    if (!MODES[mode]) {
      throw new Error("createController: mode 必须是 announce/post_match/manual");
    }

    var weighEngine = opts.weighEngine;
    if (!weighEngine || typeof weighEngine.createSession !== "function") {
      throw new Error("createController: weighEngine.createSession 缺失（需注入 WeighEngine 模块）");
    }
    var outbox = opts.outbox;
    if (!outbox || typeof outbox.enqueue !== "function") {
      throw new Error("createController: outbox.enqueue 缺失（需注入 ReportClient outbox 实例）");
    }

    // buildRecord 由 ReportClient 提供（与 WeighEngine 是不同模块）。优先用
    // opts.buildRecord（注入 ReportClient.buildRecord），回退 weighEngine.buildRecord
    // （允许调用方传入合并了两个 API 的模块）。
    var buildRecordFn = null;
    if (typeof opts.buildRecord === "function") buildRecordFn = opts.buildRecord;
    else if (typeof weighEngine.buildRecord === "function") buildRecordFn = weighEngine.buildRecord;
    if (typeof buildRecordFn !== "function") {
      throw new Error("createController: buildRecord 缺失（opts.buildRecord 需提供 ReportClient.buildRecord）");
    }

    var box = opts.box || {};
    var cageId = box.cageId;
    var strain = box.strain;
    if (cageId == null) {
      throw new Error("createController: box.cageId 必填");
    }

    // 所有模式现在都连天平：
    //   - announce/post_match：建引擎、自动判定
    //   - manual：只订阅读数，人眼判定稳定后点按钮录入（不建引擎、不做自动判定）
    var scaleChannel = opts.scaleChannel;
    if (!scaleChannel || typeof scaleChannel.onReading !== "function") {
      throw new Error("createController: scaleChannel.onReading 缺失（所有模式均需注入 BLE 通道）");
    }

    var deviceId = opts.deviceId || "scale01";
    var projectId = opts.projectId || "default";
    var weightSource = opts.weightSource || DEFAULT_WEIGHT_SOURCE[mode];

    // 起始序号：同一箱"继续录制"时由调用方传入 box.next_ordinal，避免从 1 重号。
    // 默认 1；强制为整数 ≥1（NaN/<1 回退 1）。草稿恢复时以草稿里的 startOrdinal 为准
    // （崩溃恢复后续号必须与崩溃前一致，否则恢复的记录与新增记录会错位/重号）。
    var startOrdinal = 1;
    if (typeof opts.startOrdinal === "number" && isFinite(opts.startOrdinal) && Math.floor(opts.startOrdinal) === opts.startOrdinal && opts.startOrdinal >= 1) {
      startOrdinal = opts.startOrdinal;
    }

    // dev 采集：开启后订阅每条天平读数进缓冲，finishBox 时随记录上报。
    // 默认 false（非 dev 模式零开销：不采集、不附字段）。
    var collectReadings = !!opts.collectReadings;

    var storage = opts.storage || null;
    var now = typeof opts.now === "function" ? opts.now : function () { return Date.now(); };
    var onEvent = typeof opts.onEvent === "function" ? opts.onEvent : function () {};
    var speakFn = typeof opts.speak === "function" ? opts.speak : null;

    // 视频时间轴注入：调用方可提供 videoTimeMs() 返回当前录像相对毫秒，
    // 用于 accept 时记 clip_start_ms（供服务端抽帧）。可选。
    var videoTimeMs = typeof opts.videoTimeMs === "function" ? opts.videoTimeMs : null;

    // 定时器工厂注入
    var setIntervalFn = opts.setInterval || (typeof setInterval !== "undefined" ? setInterval : null);
    var clearIntervalFn = opts.clearInterval || (typeof clearInterval !== "undefined" ? clearInterval : null);
    var tickMs = typeof opts.tickMs === "number" ? opts.tickMs : DEFAULT_TICK_MS;

    // 当前录制的实时时钟起点（用于草稿 realtimeT0，便于服务端对齐时间轴）
    var startedAt = 0;
    var realtimeT0 = 0;
    if (typeof opts.realtimeT0 === "number") realtimeT0 = opts.realtimeT0;

    // ----- 运行态 -----
    var started = false;
    var stopped = false;
    var engineSession = null;   // announce/post_match 才有
    var tickTimer = null;
    var readingListenerActive = false; // stop() 后忽略通道回调

    // 当前箱批次累积记录（数组，每条由 buildRecord 生成）
    var records = [];
    // 当前直读克数（最近一次 BLE 读数；manual 模式为 null）
    var lastGrams = null;
    // 最近一次完整读数对象（manual 模式 submitManual 用，{grams, receivedAtEpochMs}）
    var lastReading = null;
    // engine 当前状态字符串（manual 模式恒 "manual"）
    var engineState = mode === "manual" ? "manual" : "calibrating";
    // 当前候选重量（announced 态的播报克数；其它态为 null）
    var weightCandidate = null;
    // stale 标志
    var stale = false;

    // 改进 A：manual 模式辅助状态——
    //   manualNeedsClear：成功录入一只后置 true，要求收到 ≤CLEAR_THRESHOLD_G 的有效
    //     读数才清除（秤已清空、可放下一只）；为 true 时 submitManual 返回 not_cleared。
    //   manualLastAcceptedAtMs：最近一次成功录入的时钟值，用于 800ms 最小间隔防抖。
    var manualNeedsClear = false;
    var manualLastAcceptedAtMs = 0;

    // dev 读数采集缓冲（仅 collectReadings=true 时填充）
    // 每条 {t_ms(相对会话开始), grams, raw, sequence, rssi, stable, receivedAtEpochMs}
    var readingsBuffer = [];
    var readingsStartedAtEpochMs = 0;
    var readingsCollecting = false; // start() 置 true、stop()/finishBox() 置 false

    /* ---------- UI 回调封装（吞错，避免回调异常中断状态机）---------- */
    function emit(type, payload) {
      try { onEvent(type, payload || {}); } catch (_) {}
    }

    /* ---------- 当前批次草稿对象 ---------- */
    function currentDraft() {
      return {
        cageId: cageId,
        mode: mode,
        records: records.slice(),
        startedAt: startedAt,
        realtimeT0: realtimeT0,
        // 起始序号也随草稿持久化：崩溃恢复后续号必须与崩溃前一致
        startOrdinal: startOrdinal,
      };
    }

    /* ---------- 写草稿（便利封装）---------- */
    function persistDraft() {
      writeDraft(storage, cageId, currentDraft());
    }

    /* ================================================================== *
     * 记录生成：每次"确认一只"调用。
     * weightG: 克数（已 round2）；weightRaw: 原始读数（可空）
     * 返回生成的 record。
     * ================================================================== */
    function appendRecord(weightG, weightRaw) {
      // ordinal = startOrdinal + records.length：同一箱"继续录制"时 startOrdinal 由
      // 调用方传入（box.next_ordinal），保证后续号续接而非从 1 重号。
      var ordinal = startOrdinal + records.length;
      var recInput = {
        ordinal: ordinal,
        weight_g: weightG,
      };
      if (weightRaw != null && typeof weightRaw === "number" && isFinite(weightRaw)) {
        recInput.weight_raw = weightRaw;
      }
      // clip_start_ms：accept 时刻的录像相对毫秒（近似，供服务端抽帧）。
      // clip_end_ms 不在此设置（由服务端按默认窗口推断）。
      if (videoTimeMs) {
        try {
          var vt = videoTimeMs();
          if (typeof vt === "number" && isFinite(vt)) {
            recInput.clip_start_ms = Math.floor(vt);
          }
        } catch (_) {}
      }
      var rec = buildRecordFn(recInput);
      records.push(rec);
      persistDraft();
      return rec;
    }

    /* ================================================================== *
     * engine 事件转发 + 记录生成
     * ================================================================== */
    function handleEngineEvent(type, payload) {
      payload = payload || {};
      if (type === "state") {
        engineState = payload.state || engineState;
        emit("state", { state: engineState });
        return;
      }
      if (type === "announce") {
        var wg = payload.weight_g;
        weightCandidate = wg;
        if (mode === "post_match") {
          // 自动接受：不打扰 UI，不调 speak；engine.accept() 会触发 'accept'
          // 事件 → 在 'accept' 分支生成记录。
          try { engineSession.accept(); } catch (_) {}
        } else {
          // announce 模式：发 UI 'announce'（UI 弹确认/重测 + speak）
          emit("announce", { weight_g: wg });
          if (speakFn) {
            try { speakFn(wg); } catch (_) {}
          }
        }
        return;
      }
      if (type === "accept") {
        // 两种模式都在此生成记录（announce 的人工 accept、post_match 的自动 accept）
        var accG = payload.weight_g;
        var ordinal = payload.ordinal;
        // weight_raw：优先从 payload.weight_raw（engine 当前未带），回退 weightCandidate 上下文无
        var rec = appendRecord(accG, null);
        emit("accepted", {
          ordinal: ordinal != null ? ordinal : records.length,
          weight_g: accG,
          count: records.length,
        });
        emit("recorded", {
          record: rec,
          pendingCount: typeof outbox.pending === "function" ? outbox.pending() : 0,
        });
        return;
      }
      if (type === "ready_next") {
        weightCandidate = null;
        emit("ready_next", {});
        return;
      }
      if (type === "stale") {
        stale = !!payload.stale;
        emit("stale", { stale: stale });
        return;
      }
      // 其它事件（如未来扩展）忽略
    }

    /* ================================================================== *
     * BLE 读数处理：同时 (a) ingest 到引擎、(b) 发 UI 'weight' 直读显示。
     * ================================================================== */
    function handleReading(reading) {
      if (!readingListenerActive) return;
      lastGrams = (reading && typeof reading.grams === "number") ? reading.grams : lastGrams;
      // 维护最近一次完整读数（manual 模式 submitManual 读 lastReading.grams/receivedAtEpochMs）
      if (reading && typeof reading.grams === "number" && isFinite(reading.grams)) {
        lastReading = {
          grams: reading.grams,
          receivedAtEpochMs: (typeof reading.receivedAtEpochMs === "number") ? reading.receivedAtEpochMs : now(),
        };
      }
      // 直读显示：每次有效读数都向 UI 发 'weight' {grams}
      if (reading && typeof reading.grams === "number" && isFinite(reading.grams)) {
        emit("weight", { grams: reading.grams });
      }
      // 改进 A：manual 模式清秤门槛——成功录入一只后 manualNeedsClear=true，
      // 收到 ≤MANUAL_CLEAR_THRESHOLD_G 的有效读数视为"秤已清空、可放下一只"，
      // 清除该标志（下一次 submitManual 即可录入）。仅 manual 模式生效。
      if (mode === "manual" && manualNeedsClear &&
        reading && typeof reading.grams === "number" && isFinite(reading.grams) &&
        reading.grams <= MANUAL_CLEAR_THRESHOLD_G) {
        manualNeedsClear = false;
      }
      // dev 采集：把完整读数时间序列进缓冲（相对会话开始的毫秒时间戳）
      if (readingsCollecting && reading) {
        var recvMs = (typeof reading.receivedAtEpochMs === "number") ? reading.receivedAtEpochMs : now();
        readingsBuffer.push({
          t_ms: recvMs - readingsStartedAtEpochMs,
          grams: (typeof reading.grams === "number") ? reading.grams : null,
          raw: reading.raw != null ? reading.raw : null,
          sequence: (typeof reading.sequence === "number") ? reading.sequence : null,
          rssi: reading.rssi != null ? reading.rssi : null,
          stable: (typeof reading.stable === "boolean") ? reading.stable : null,
          receivedAtEpochMs: recvMs,
        });
      }
      if (engineSession && reading) {
        try {
          engineSession.ingestReading({
            grams: reading.grams,
            raw: reading.raw,
            sequence: reading.sequence,
            receivedAtEpochMs: reading.receivedAtEpochMs,
          });
        } catch (_) {}
      }
    }

    /* ================================================================== *
     * start()
     * ================================================================== */
    function start() {
      if (started) return;
      started = true;
      stopped = false;
      startedAt = now();

      // dev 采集：新会话/新箱开始时重置缓冲（避免跨箱混入上一箱读数）
      if (collectReadings) {
        readingsBuffer = [];
        readingsStartedAtEpochMs = startedAt;
        readingsCollecting = true;
      }

      // 恢复未完成草稿（崩溃安全）
      var draft = readDraft(storage, cageId);
      if (draft && Array.isArray(draft.records) && draft.records.length > 0) {
        records = draft.records.slice();
        // realtimeT0 恢复（保留原录制起点）
        if (typeof draft.realtimeT0 === "number") realtimeT0 = draft.realtimeT0;
        // startOrdinal 以草稿为准：崩溃恢复后续号必须与崩溃前一致，
        // 否则恢复出的 N 条记录之后会从"当前 startOrdinal + N"继续，
        // 而恢复的记录的 ordinal 已是历史值，会出现错位/重号。
        if (typeof draft.startOrdinal === "number" && isFinite(draft.startOrdinal) && Math.floor(draft.startOrdinal) === draft.startOrdinal && draft.startOrdinal >= 1) {
          startOrdinal = draft.startOrdinal;
        }
        emit("draft_resumed", { count: records.length });
      }

      // 订阅通道读数（控制器不负责 channel.start/stop，由 mobile.js 管）。
      // 所有模式都订阅：announce/post_match 喂引擎，manual 维护 lastReading。
      readingListenerActive = true;
      try { scaleChannel.onReading(handleReading); } catch (_) {}

      if (mode === "manual") {
        engineState = "manual";
        emit("state", { state: engineState });
        return;
      }

      // announce / post_match：创建 engine session
      engineSession = weighEngine.createSession({
        config: opts.engineConfig || undefined,
        now: now,
        onEvent: handleEngineEvent,
      });
      // 启动 tick 定时器（无新读数也要推进：wait_clear 超时等）
      if (setIntervalFn) {
        tickTimer = setIntervalFn(function () {
          if (engineSession) {
            try { engineSession.tick(); } catch (_) {}
          }
        }, tickMs);
      }
    }

    /* ================================================================== *
     * stop()
     * ================================================================== */
    function stop() {
      if (stopped) return;
      stopped = true;
      started = false;
      // 退订：scaleChannel.onReading 无取消订阅 API，用标志位忽略后续回调。
      readingListenerActive = false;
      // dev 采集：stop 后停止写入缓冲（finishBox 仍可读已采集数据）
      readingsCollecting = false;
      if (tickTimer && clearIntervalFn) {
        try { clearIntervalFn(tickTimer); } catch (_) {}
        tickTimer = null;
      }
      engineSession = null;
    }

    /* ================================================================== *
     * accept()：announce 模式人工确认 → engine.accept()
     * ================================================================== */
    function accept() {
      if (mode !== "announce") return null;
      if (!engineSession) return null;
      try { return engineSession.accept(); } catch (_) { return null; }
    }

    /* ================================================================== *
     * retry()：announce/post_match 重测 → engine.retry()
     * ================================================================== */
    function retry() {
      if (mode === "manual") return null;
      if (!engineSession) return null;
      weightCandidate = null;
      try { return engineSession.retry(); } catch (_) { return null; }
    }

    /* ================================================================== *
     * submitManual(weightG?)：manual 模式录入——读取当前天平读数（人眼判定
     * 稳定后点按钮触发），直接生成一条记录（不走引擎）。weight_source 仍为
     * "manual"（人眼判定、手动触发）。
     *
     * 可选 weightG（仅测试注入用，显式指定克数跳过读数判定）；生产调用无参。
     *
     * 返回结果对象：
     *   成功 → { ok:true, record:<record>, weight_g:<g> }
     *   无读数 / 读数超过 MANUAL_STALE_MS → { ok:false, reason:"stale", record:null }
     *   grams <= 0 → { ok:false, reason:"zero", record:null }
     *   上一只还没清秤（读数仍 > 清秤门槛）→ { ok:false, reason:"not_cleared", record:null }
     *   距上次成功录入 < 800ms（防抖）→ { ok:false, reason:"too_fast", record:null }
     *   非 manual 模式 / 显式注入越界 → { ok:false, reason:"invalid", record:null }
     *
     * 改进 A（清秤门槛 + 防抖）仅在生产路径（无 weightG 注入）生效；显式注入
     * 跳过这两个门槛，便于测试直接构造记录。manualNeedsClear 由 handleReading
     * 在收到 ≤MANUAL_CLEAR_THRESHOLD_G 的有效读数时自动清除。
     * ================================================================== */
    function submitManual(weightG) {
      if (mode !== "manual") return { ok: false, reason: "invalid", record: null };

      var injected = arguments.length > 0 && typeof weightG !== "undefined";
      var grams;
      if (injected) {
        // 显式注入（测试用）：只做范围校验，不查 lastReading/新鲜度/清秤/防抖
        if (typeof weightG !== "number" || !isFinite(weightG) || weightG < 0 || weightG > MAX_GRAMS) {
          return { ok: false, reason: "invalid", record: null };
        }
        grams = weightG;
      } else {
        // 生产路径：读当前天平读数
        if (!lastReading) return { ok: false, reason: "stale", record: null };
        var ageMs = now() - (lastReading.receivedAtEpochMs || 0);
        if (ageMs > MANUAL_STALE_MS) return { ok: false, reason: "stale", record: null };
        // 改进 A：清秤门槛——上一只录入后要求秤回落到 ≤门槛 才允许录下一只
        if (manualNeedsClear) {
          return { ok: false, reason: "not_cleared", record: null };
        }
        // 改进 A：防抖——距上次成功录入 < 800ms 拒绝（防误连点）
        if (manualLastAcceptedAtMs > 0 && (now() - manualLastAcceptedAtMs) < MANUAL_MIN_INTERVAL_MS) {
          return { ok: false, reason: "too_fast", record: null };
        }
        grams = lastReading.grams;
        if (!(typeof grams === "number" && isFinite(grams) && grams > 0 && grams <= MAX_GRAMS)) {
          return { ok: false, reason: "zero", record: null };
        }
      }

      var rec = appendRecord(grams, null);
      // 改进 A：成功录入后置位清秤门槛 + 记录入录时间（防抖基线）。仅在真实读数
      // 路径（非测试注入）置位——显式注入跳过这两个门槛，便于测试连续构造记录。
      if (!injected) {
        manualNeedsClear = true;
        manualLastAcceptedAtMs = now();
      }
      emit("accepted", {
        ordinal: records.length,
        weight_g: grams,
        count: records.length,
      });
      emit("recorded", {
        record: rec,
        pendingCount: typeof outbox.pending === "function" ? outbox.pending() : 0,
      });
      return { ok: true, record: rec, weight_g: grams };
    }

    /* ================================================================== *
     * finishBox(videoBlob?, readings?)：累积批次 outbox.enqueue → 清草稿
     * → 返回 {count, batchId}
     *   readings：可选 dev 采集 payload（getReadingsPayload() 返回值），
     *             随记录一起入队上报（可 JSON 序列化、可持久化，与 videoBlob 不同）。
     * ================================================================== */
    function finishBox(videoBlob, readings) {
      var batch = {
        cage_id: cageId,
        project_id: projectId,
        device_id: deviceId,
        weight_source: weightSource,
        records: records.slice(),
      };
      if (strain != null) batch.strain = strain;

      var batchId;
      try {
        // readings 为可选第三参数（null/undefined 时 outbox 不附字段）
        batchId = outbox.enqueue(batch, videoBlob || undefined, readings || undefined);
      } catch (e) {
        // enqueue 失败（如 records 非数组）不应清草稿（数据未入队）
        throw e;
      }
      // 成功入队 → 清草稿（崩溃安全：记录已进 outbox 持久化）
      clearDraft(storage, cageId);
      var count = records.length;
      // 重置当前批次累积（同一控制器实例可继续用于下一箱——但建议每箱 new 一个）
      records = [];
      weightCandidate = null;
      lastReading = null;
      // 改进 A：manual 辅助状态也随批次重置（下一箱从头开始）
      manualNeedsClear = false;
      manualLastAcceptedAtMs = 0;
      // dev 采集缓冲也随批次清空（下一箱重新累计）
      readingsBuffer = [];
      return { count: count, batchId: batchId };
    }

    /* ================================================================== *
     * getReadingsPayload()：dev 采集——返回当前会话的完整读数时间序列。
     * 无数据（未开启采集 / 会话未开始 / 无读数）返回 null。
     * 有数据返回：
     *   {device_id, started_at_epoch_ms, app: "h5-dev-collect",
     *    engine_config: <当前引擎配置快照>, readings: [...]}
     * engine_config 快照取 opts.engineConfig（mobile.js 传入的 {stable_min_span_ms:800} 等）。
     * ================================================================== */
    function getReadingsPayload() {
      if (!collectReadings) return null;
      if (!readingsBuffer || readingsBuffer.length === 0) return null;
      var cfg = null;
      if (opts.engineConfig && typeof opts.engineConfig === "object") {
        // 浅拷贝快照（避免后续 mutate 影响上报）
        try { cfg = JSON.parse(JSON.stringify(opts.engineConfig)); } catch (_) { cfg = opts.engineConfig; }
      }
      return {
        device_id: deviceId,
        started_at_epoch_ms: readingsStartedAtEpochMs || startedAt || 0,
        app: "h5-dev-collect",
        engine_config: cfg,
        readings: readingsBuffer.slice(),
      };
    }

    /* ================================================================== *
     * getState()
     * ================================================================== */
    function getState() {
      return {
        mode: mode,
        state: engineState,
        mouseCount: records.length,
        // 下一只将分配的序号（只读，便于调试/断言"续号正确"）
        nextOrdinal: startOrdinal + records.length,
        weightCandidate: weightCandidate,
        lastGrams: lastGrams,
        stale: stale,
        pendingCount: typeof outbox.pending === "function" ? outbox.pending() : 0,
      };
    }

    return {
      start: start,
      stop: stop,
      accept: accept,
      retry: retry,
      submitManual: submitManual,
      finishBox: finishBox,
      getState: getState,
      getReadingsPayload: getReadingsPayload,
      // 测试/调试用只读视图
      _records: function () { return records.slice(); },
      _readings: function () { return readingsBuffer.slice(); },
    };
  }

  return {
    createController: createController,
    // 暴露内部辅助供测试
    _draftKey: draftKey,
    _MAX_GRAMS: MAX_GRAMS,
    _DEFAULT_TICK_MS: DEFAULT_TICK_MS,
    _MANUAL_STALE_MS: MANUAL_STALE_MS,
    _MANUAL_CLEAR_THRESHOLD_G: MANUAL_CLEAR_THRESHOLD_G,
    _MANUAL_MIN_INTERVAL_MS: MANUAL_MIN_INTERVAL_MS,
  };
});
