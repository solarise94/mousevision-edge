/* 蓝牙天平桥接 (K797) — HarmonyOS / Android 原生外壳注入的 javaScriptProxy
 * 与本页之间的适配层。纯函数 + 单一通道工厂，无框架依赖。
 *
 * 原生→页面事件（CustomEvent on window，detail 为已解析 JSON）：
 *   - miceautomatic:scale-reading  天平读数
 *   - miceautomatic:scale-status   天平状态
 *
 * 设计目标：
 *   1) 无原生桥时 detectNativeBridge() 返回 false，本模块对页面无副作用；
 *   2) 读数做形状校验与乱序去重，stale=10s 内无有效广播（raw=0 是真实 0g）；
 *   3) 断线期间只缓存最新一条读数，重连后 flush() 仅发一条（不补发过期队列）。
 *
 * UMD：浏览器挂 window.ScaleBridge；node 测试 require 该模块。
 */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (root && typeof root === "object") {
    root.ScaleBridge = api;
  }
})(typeof window !== "undefined" ? window : this, function () {
  "use strict";

  var READING_EVENT = "miceautomatic:scale-reading";
  var STATUS_EVENT = "miceautomatic:scale-status";
  // 读数校验上下界（K797 量程相关，与后端一致）
  var MAX_GRAMS = 6553.5;
  var MAX_RAW = 65535;
  // stale 判定窗口：超过该毫秒数无有效读数即视为广播中断
  var DEFAULT_STALE_MS = 10000;
  // stale 看门狗检查间隔
  var STALE_CHECK_MS = 1000;
  // 一旦原生状态处于下列取值且无新鲜读数，通道直接视为 stale
  var STALE_NATIVE_STATES = { stale: true, off: true, unauthorized: true, bluetooth_off: true, error: true };

  /* ------------------------------------------------------------------ *
   * 原生桥探测：需要同时存在三个核心方法。
   * ------------------------------------------------------------------ */
  function detectNativeBridge(scope) {
    var w = scope || (typeof window !== "undefined" ? window : null);
    if (!w || !w.MiceAutomaticScale) return false;
    var b = w.MiceAutomaticScale;
    return typeof b.startScaleScan === "function" &&
      typeof b.stopScaleScan === "function" &&
      typeof b.getScaleStatus === "function";
  }

  /* ------------------------------------------------------------------ *
   * 读数形状校验。返回归一化后的读数对象，或 null 表示丢弃。
   * grams: 有限数且 0..MAX_GRAMS；raw: 整数 0..MAX_RAW；sequence: 整数 >=0
   * ------------------------------------------------------------------ */
  function isValidReading(detail) {
    if (!detail || typeof detail !== "object") return false;
    var grams = detail.grams;
    var raw = detail.raw;
    var sequence = detail.sequence;
    if (typeof grams !== "number" || !isFinite(grams) || grams < 0 || grams > MAX_GRAMS) return false;
    if (typeof raw !== "number" || !isFinite(raw) || Math.floor(raw) !== raw || raw < 0 || raw > MAX_RAW) return false;
    if (typeof sequence !== "number" || !isFinite(sequence) || Math.floor(sequence) !== sequence || sequence < 0) return false;
    return true;
  }

  /* ------------------------------------------------------------------ *
   * 通道工厂。opts 支持注入 clock（返回 ms）与 timer 工厂，便于测试。
   * ------------------------------------------------------------------ */
  function createScaleChannel(opts) {
    opts = opts || {};
    var now = typeof opts.now === "function" ? opts.now : function () { return Date.now(); };
    var perfNow = typeof opts.perfNow === "function"
      ? opts.perfNow
      : (typeof performance !== "undefined" && performance.now ? function () { return performance.now(); } : now);
    var setIntervalFn = opts.setInterval || (typeof setInterval !== "undefined" ? setInterval : null);
    var clearIntervalFn = opts.clearInterval || (typeof clearInterval !== "undefined" ? clearInterval : null);
    var addEventListener = opts.addEventListener ||
      (typeof window !== "undefined" && window.addEventListener ? function (t, fn) { window.addEventListener(t, fn); } : null);
    var removeEventListener = opts.removeEventListener ||
      (typeof window !== "undefined" && window.removeEventListener ? function (t, fn) { window.removeEventListener(t, fn); } : null);
    var nativeBridge = opts.nativeBridge || (typeof window !== "undefined" ? window.MiceAutomaticScale : null);
    var staleMs = typeof opts.staleMs === "number" ? opts.staleMs : DEFAULT_STALE_MS;

    var available = detectNativeBridge(opts.windowScope);
    var started = false;

    var status = null;             // 最近一条状态（detail）
    var lastReading = null;        // 最近一条有效读数（detail）
    var lastReadingAtMs = 0;       // 本地单调时钟（performance.now 基准）
    var lastSequence = -1;         // 已接受的最大 sequence
    var droppedOutOfOrder = 0;     // 乱序/重复丢弃计数
    // 从未收到读数即视为 stale（computeStale 在 lastReadingAtMs===0 时恒为 true）。
    var stale = true;
    var staleTimer = null;

    var readingCbs = [];
    var statusCbs = [];
    var staleCbs = [];

    function pushStaleChange() {
      var s = computeStale();
      if (s === stale) return;
      stale = s;
      for (var i = 0; i < staleCbs.length; i++) {
        try { staleCbs[i](stale); } catch (_) {}
      }
    }

    function computeStale() {
      // 原生明确上报异常态且无新鲜读数 → stale
      if (status && STALE_NATIVE_STATES[status.state] && lastReadingAtMs === 0) return true;
      if (lastReadingAtMs === 0) return true;
      var age = now() - lastReadingAtMs;
      if (age > staleMs) return true;
      return false;
    }

    function onReadingEvent(ev) {
      var detail = ev && ev.detail;
      if (!isValidReading(detail)) return;
      // 乱序/重复：sequence 必须 > 已接受的最大值
      if (detail.sequence <= lastSequence) {
        droppedOutOfOrder += 1;
        return;
      }
      lastSequence = detail.sequence;
      lastReading = detail;
      lastReadingAtMs = now();
      pushStaleChange();
      for (var i = 0; i < readingCbs.length; i++) {
        try { readingCbs[i](detail); } catch (_) {}
      }
    }

    function onStatusEvent(ev) {
      var detail = ev && ev.detail;
      if (!detail || typeof detail !== "object") return;
      status = detail;
      pushStaleChange();
      for (var i = 0; i < statusCbs.length; i++) {
        try { statusCbs[i](detail); } catch (_) {}
      }
    }

    function tickStaleWatchdog() {
      pushStaleChange();
    }

    return {
      start: function () {
        if (started) return;
        started = true;
        if (addEventListener) {
          addEventListener(READING_EVENT, onReadingEvent);
          addEventListener(STATUS_EVENT, onStatusEvent);
        }
        if (setIntervalFn) {
          staleTimer = setIntervalFn(tickStaleWatchdog, STALE_CHECK_MS);
        }
        if (available && nativeBridge && typeof nativeBridge.startScaleScan === "function") {
          try { nativeBridge.startScaleScan(); } catch (_) {}
        }
      },
      stop: function () {
        if (!started) return;
        started = false;
        if (removeEventListener) {
          try { removeEventListener(READING_EVENT, onReadingEvent); } catch (_) {}
          try { removeEventListener(STATUS_EVENT, onStatusEvent); } catch (_) {}
        }
        if (staleTimer && clearIntervalFn) {
          try { clearIntervalFn(staleTimer); } catch (_) {}
          staleTimer = null;
        }
        if (available && nativeBridge && typeof nativeBridge.stopScaleScan === "function") {
          try { nativeBridge.stopScaleScan(); } catch (_) {}
        }
      },
      onReading: function (cb) { if (typeof cb === "function") readingCbs.push(cb); },
      onStatus: function (cb) { if (typeof cb === "function") statusCbs.push(cb); },
      onStaleChange: function (cb) { if (typeof cb === "function") staleCbs.push(cb); },
      getState: function () {
        return {
          available: available,
          status: status,
          lastReading: lastReading,
          lastReadingAtMs: lastReadingAtMs,
          stale: computeStale(),
          droppedOutOfOrder: droppedOutOfOrder
        };
      },
      // 重置内部状态，主要供测试用
      _reset: function () {
        status = null;
        lastReading = null;
        lastReadingAtMs = 0;
        lastSequence = -1;
        droppedOutOfOrder = 0;
        stale = true;
      }
    };
  }

  /* ------------------------------------------------------------------ *
   * 构造发送给 WS 的 scale_reading 文本消息（source 固定 ble_k797）。
   * ------------------------------------------------------------------ */
  function buildScaleReadingMessage(reading, clientTsMs) {
    return {
      type: "scale_reading",
      source: "ble_k797",
      grams: reading.grams,
      raw: reading.raw,
      client_ts_ms: Math.max(0, Math.floor(clientTsMs)),
      received_at_epoch_ms: reading.receivedAtEpochMs || 0,
      sequence: reading.sequence,
      stable: reading.stable === true,
      rssi: typeof reading.rssi === "number" ? reading.rssi : 0
    };
  }

  /* ------------------------------------------------------------------ *
   * “仅保留最新一条”发送器：断开时缓存最新值，flush() 仅发一条。
   * ------------------------------------------------------------------ */
  function createLatestOnlySender(sendFn) {
    var pending = null;       // 待发送的最新消息（仅一条）
    return {
      offer: function (msg) {
        // 始终只保留最新一条：覆盖旧值
        pending = msg;
      },
      flush: function () {
        if (pending == null) return false;
        var m = pending;
        pending = null;
        try { sendFn(m); return true; } catch (_) { return false; }
      },
      hasPending: function () { return pending != null; }
    };
  }

  /* ------------------------------------------------------------------ *
   * 称重流去重发送器：值变化即发（曲线保真），值不变时按心跳周期补发一条
   * （让后端知道天平仍在线，不误判 stale）。实测 HarmonyOS 对同值广播本身
   * 已合并上报，但转发给后端 WS 时稳态仍会每 ~200ms 轰炸；本层在前端拦截。
   *
   * buildMsg(reading) 由调用方提供，返回待发的消息对象；sendFn(msg) 实际发送。
   * now() 与 timer 工厂可注入便于测试。
   * ------------------------------------------------------------------ */
  var DEFAULT_HEARTBEAT_MS = 2000;

  function createDedupSender(sendFn, buildMsg, opts) {
    opts = opts || {};
    var now = typeof opts.now === "function" ? opts.now : function () { return Date.now(); };
    var heartbeatMs = typeof opts.heartbeatMs === "number" ? opts.heartbeatMs : DEFAULT_HEARTBEAT_MS;
    var setIntervalFn = opts.setInterval || (typeof setInterval !== "undefined" ? setInterval : null);
    var clearIntervalFn = opts.clearInterval || (typeof clearInterval !== "undefined" ? clearInterval : null);

    var lastSentGrams = null;   // 上次发送的 grams（null=从未发过，首条必发）
    var lastSentAtMs = 0;
    var heartbeatTimer = null;
    var pendingMsg = null;      // 心跳周期到来时重发的“当前最新消息”

    function doSend(msg) {
      try { sendFn(msg); } catch (_) {}
    }

    // 心跳：若距上次发送已超 heartbeatMs 且有可用最新消息，补发一条。
    function heartbeatTick() {
      if (pendingMsg == null) return;
      if (now() - lastSentAtMs < heartbeatMs) return;
      doSend(pendingMsg);
      lastSentAtMs = now();
    }

    return {
      // 收到一条读数：值变化或首条 → 立即发；否则只更新 pendingMsg 等心跳。
      send: function (reading) {
        var msg = buildMsg(reading);
        pendingMsg = msg;
        var grams = reading.grams;
        var first = lastSentGrams === null;
        if (first || grams !== lastSentGrams) {
          doSend(msg);
          lastSentGrams = grams;
          lastSentAtMs = now();
        }
      },
      start: function () {
        if (heartbeatTimer == null && setIntervalFn) {
          heartbeatTimer = setIntervalFn(heartbeatTick, heartbeatMs);
        }
      },
      stop: function () {
        if (heartbeatTimer != null && clearIntervalFn) {
          try { clearIntervalFn(heartbeatTimer); } catch (_) {}
        }
        heartbeatTimer = null;
      },
      // 重连后补发最新一条（由调用方在 ws.onopen 调用）。
      flush: function () {
        if (pendingMsg == null) return false;
        doSend(pendingMsg);
        lastSentAtMs = now();
        return true;
      },
      _state: function () {
        return { lastSentGrams: lastSentGrams, lastSentAtMs: lastSentAtMs, hasPending: pendingMsg != null };
      }
    };
  }
  function createLatestOnlySender(sendFn) {
    var pending = null;       // 待发送的最新消息（仅一条）
    return {
      offer: function (msg) {
        // 始终只保留最新一条：覆盖旧值
        pending = msg;
      },
      flush: function () {
        if (pending == null) return false;
        var m = pending;
        pending = null;
        try { sendFn(m); return true; } catch (_) { return false; }
      },
      hasPending: function () { return pending != null; }
    };
  }

  /* ------------------------------------------------------------------ *
   * 显示格式化：无读数/stale → "--"；raw=0 → "0.0"；否则一位小数。
   * ------------------------------------------------------------------ */
  function formatScaleDisplay(state) {
    var s = state || {};
    var reading = s.lastReading;
    if (!reading || s.stale) return { text: "--", stale: true };
    var g = Number(reading.grams);
    if (!isFinite(g)) return { text: "--", stale: true };
    // raw=0 是真实 0g，必须显示 "0.0"
    return { text: g.toFixed(1), stale: false };
  }

  return {
    detectNativeBridge: detectNativeBridge,
    createScaleChannel: createScaleChannel,
    buildScaleReadingMessage: buildScaleReadingMessage,
    createLatestOnlySender: createLatestOnlySender,
    createDedupSender: createDedupSender,
    formatScaleDisplay: formatScaleDisplay,
    isValidReading: isValidReading,
    READING_EVENT: READING_EVENT,
    STATUS_EVENT: STATUS_EVENT,
    DEFAULT_STALE_MS: DEFAULT_STALE_MS,
    DEFAULT_HEARTBEAT_MS: DEFAULT_HEARTBEAT_MS
  };
});
