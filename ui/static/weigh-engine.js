/* 实时称重判定状态机 (WeighEngine) — mousevision/realtime.py 的纯 JS 忠实移植。
 *
 * 纯 app 化改造：称重判定从服务端下放到手机本地，BLE 重量直读驱动，不依赖
 * 视频帧、不依赖网络。算法语义与 Python 版逐条对齐，唯一刻意的差异是
 * calibrating 阶段：Python 版靠视频校准（LCD 定位），JS 版改为纯重量确认
 * 空秤（连续 calibrate_min_reads 条新鲜读数 <= empty_max），无需相机。
 *
 * 驱动方式（与 Python 帧驱动的差异）：
 *   - Python 版每帧 process_frame 读一次 BLE 缓存推进状态机；
 *   - JS 版改为「读数驱动 + 定时器驱动」：
 *     * ingestReading(r)：校验并缓存 BLE 读数，然后内部推进一次状态机；
 *     * tick()：用当前缓存的新鲜读数推进一次（供定时器周期调用，
 *       处理 wait_clear 超时等无新读数也要推进的场景）。
 *
 * 风格：纯函数 + 工厂、零依赖、依赖注入时钟便于测试（参照 scale-bridge.js）。
 *
 * UMD：浏览器挂 window.WeighEngine；node 测试 require 该模块。
 */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (root && typeof root === "object") {
    root.WeighEngine = api;
  }
})(typeof window !== "undefined" ? window : this, function () {
  "use strict";

  // 读数范围（K797 量程，与后端一致）
  var MAX_GRAMS = 6553.5;
  var MAX_RAW = 65535;
  // grams 与 raw/10 允许的舍入偏差
  var GRAMS_RAW_TOL = 0.05;

  // ------------------------------------------------------------------ //
  // 完整 config 默认值（对齐 RealtimeConfig + 新增 calibrate_min_reads）
  // ------------------------------------------------------------------ //
  var DEFAULT_CONFIG = {
    calibrate_min_reads: 3,      // calibrating 连续空秤读数
    enter_min: 1.0,              // 进入称重的阈值（克）
    empty_max: 0.15,             // 判定空秤的阈值（克）
    leave_max: 0.30,             // 判定小鼠离开的阈值（克）
    enter_sustain_frames: 2,     // 进入称重需要的连续非零读数
    stable_min_raw_reads: 3,     // 稳定后缀最少独立原始读数
    stable_confirm_raw_reads: 1, // 候选确认期需要再确认的独立读数
    stable_min_span_ms: 0.0,     // 确认期最小时间跨度（ms）；0 = 仅按读数
    stable_max_age_s: 1.6,       // 稳定证据允许保留的最大年龄（秒）
    stable_weight_tol: 0.10,     // 稳定后缀内最大跨度（克）
    min_confidence: 0.50,        // 最低置信度（BLE 恒 1.0）
    announce_hold_s: 0.0,        // 播报后自动接受的等待时间（0 = 关闭）
    clear_timeout_s: 30.0,       // 等待清秤超时（秒）
    ble_stale_s: 10.0,           // 超过该秒数无广播即视为读数过期
  };

  /* 合并默认配置（浅合并；opts.config 覆盖默认）。 */
  function mergeConfig(userCfg) {
    var out = {};
    var k;
    for (k in DEFAULT_CONFIG) {
      if (Object.prototype.hasOwnProperty.call(DEFAULT_CONFIG, k)) {
        out[k] = DEFAULT_CONFIG[k];
      }
    }
    if (userCfg && typeof userCfg === "object") {
      for (k in userCfg) {
        if (Object.prototype.hasOwnProperty.call(userCfg, k)) {
          out[k] = userCfg[k];
        }
      }
    }
    return out;
  }

  /* 校验 config 取值范围（对齐 validate_realtime_config）。抛错即非法。 */
  function validateConfig(cfg) {
    function fail(msg) { throw new Error(msg); }
    if (cfg.stable_min_raw_reads < 2) fail("stable_min_raw_reads must be >= 2, got " + cfg.stable_min_raw_reads);
    if (cfg.stable_confirm_raw_reads < 0) fail("stable_confirm_raw_reads must be >= 0, got " + cfg.stable_confirm_raw_reads);
    if (cfg.stable_min_span_ms < 0) fail("stable_min_span_ms must be >= 0, got " + cfg.stable_min_span_ms);
    if (!(cfg.stable_max_age_s > 0)) fail("stable_max_age_s must be > 0, got " + cfg.stable_max_age_s);
    if (!(cfg.min_confidence > 0 && cfg.min_confidence <= 1)) fail("min_confidence must be in (0, 1], got " + cfg.min_confidence);
    if (!(cfg.stable_weight_tol > 0)) fail("stable_weight_tol must be > 0, got " + cfg.stable_weight_tol);
    if (cfg.calibrate_min_reads < 1) fail("calibrate_min_reads must be >= 1, got " + cfg.calibrate_min_reads);
    if (cfg.enter_sustain_frames < 1) fail("enter_sustain_frames must be >= 1, got " + cfg.enter_sustain_frames);
  }

  /* ------------------------------------------------------------------ //
   * 读数校验（对齐 ingest_scale_reading L425-489）。任一失败返回 false。
   * ------------------------------------------------------------------ */
  function isValidReading(r) {
    if (!r || typeof r !== "object") return false;
    var grams = r.grams;
    var raw = r.raw;
    var sequence = r.sequence;
    var receivedAtEpochMs = r.receivedAtEpochMs;
    // grams：有限数且 [0, MAX_GRAMS]
    if (typeof grams !== "number" || !isFinite(grams) || grams < 0 || grams > MAX_GRAMS) return false;
    // raw：整数且 [0, MAX_RAW]
    if (typeof raw !== "number" || !isFinite(raw) || Math.floor(raw) !== raw || raw < 0 || raw > MAX_RAW) return false;
    // sequence：整数 >= 0
    if (typeof sequence !== "number" || !isFinite(sequence) || Math.floor(sequence) !== sequence || sequence < 0) return false;
    // receivedAtEpochMs：整数
    if (typeof receivedAtEpochMs !== "number" || !isFinite(receivedAtEpochMs) || Math.floor(receivedAtEpochMs) !== receivedAtEpochMs) return false;
    // grams 与 raw/10 一致
    if (Math.abs(grams - raw / 10.0) > GRAMS_RAW_TOL) return false;
    return true;
  }

  /* ------------------------------------------------------------------ //
   * 中位数计算（对齐 numpy.median 对奇/偶长度的行为）。
   * numpy.median 偶数长度取中间两个的均值；奇数长度取正中。
   * 输入会被复制后排序，不修改原数组。
   * ------------------------------------------------------------------ */
  function median(values) {
    if (!values.length) return 0;
    var sorted = values.slice().sort(function (a, b) { return a - b; });
    var n = sorted.length;
    var mid = n >> 1; // floor(n/2)
    if ((n & 1) === 1) return sorted[mid];
    return (sorted[mid - 1] + sorted[mid]) / 2.0;
  }

  /* round 到两位小数（对齐 Python round(x,2)，使用 banker-free round-half-up
   * 在 .005 这种边缘 case 上可能与 Python 的 round-half-even 略有差异，但对
   * 称重克数显示无实际影响）。 */
  function round2(x) {
    return Math.round(x * 100) / 100;
  }

  /* ------------------------------------------------------------------ //
   * 会话工厂。opts.config 合并默认值；opts.now 注入单调时钟（ms）；
   * opts.onEvent(type, payload) 回调。
   * ------------------------------------------------------------------ */
  function createSession(opts) {
    opts = opts || {};
    var cfg = mergeConfig(opts.config);
    validateConfig(cfg);
    var now = typeof opts.now === "function"
      ? opts.now
      : (typeof performance !== "undefined" && typeof performance.now === "function"
          ? function () { return performance.now(); }
          : function () { return Date.now(); });
    var onEvent = typeof opts.onEvent === "function" ? opts.onEvent : function () {};

    // --- 状态机 ------------------------------------------------------- //
    var state = "calibrating";

    // 计数器
    var calibrateGood = 0;     // calibrating 连续空秤读数
    var enterSustain = 0;      // armed 连续高于 enter_min 的读数
    var leaveCount = 0;        // weighing 连续低重读数
    var clearCount = 0;        // wait_clear 连续空秤读数

    // 原始稳定证据：{frameSeq, clientTsMs, weight, confidence, epoch}
    var rawWindow = [];
    var weighingEpoch = 0;

    // 候选确认期：{median, medianConf, firstTsMs, confirmCount}
    var pendingCandidate = null;

    // 当前 attempt（announced 态待接受/重称）
    var currentAttempt = null;
    // 已接受记录
    var accepted = [];
    // mouseCount：已接受只数（= accepted.length）
    // 进入 wait_clear 的时刻（now() 基准）
    var waitClearAt = 0;

    // BLE 读数缓存：{grams, raw, sequence, receivedAtEpochMs, receivedMonotonicMs}
    var bleReading = null;
    var lastSequence = -1;     // 单调校验用；-1 表示尚未收到

    // 最近一次重量候选（供 getState 显示）
    var lastCandidate = null;
    var lastConfidence = 0.0;

    // 上一次 stale 事件下发的状态（边沿触发）
    var lastStaleReported = false;

    /* ---- BLE 新鲜读数（对齐 _fresh_ble_grams L666-679）---- */
    function freshGrams() {
      if (!bleReading) return null;
      var ageS = (now() - bleReading.receivedMonotonicMs) / 1000.0;
      if (ageS > cfg.ble_stale_s) return null;
      return bleReading.grams;
    }

    function freshRaw() {
      if (!bleReading) return null;
      var ageS = (now() - bleReading.receivedMonotonicMs) / 1000.0;
      if (ageS > cfg.ble_stale_s) return null;
      return bleReading.raw;
    }

    /* ---- 裁剪 rawWindow（对齐 _prune_raw_window L577-588）---- */
    function pruneRawWindow(latestTsMs) {
      var maxAgeMs = cfg.stable_max_age_s * 1000.0;
      var epoch = weighingEpoch;
      var kept = [];
      for (var i = 0; i < rawWindow.length; i++) {
        var r = rawWindow[i];
        if (r.epoch !== epoch) continue;
        if (latestTsMs - r.clientTsMs > maxAgeMs) continue;
        kept.push(r);
      }
      rawWindow = kept;
    }

    function appendRawRead(frameSeq, clientTsMs, weight, confidence) {
      rawWindow.push({
        frameSeq: frameSeq,
        clientTsMs: clientTsMs,
        weight: weight,
        confidence: confidence,
        epoch: weighingEpoch,
      });
      pruneRawWindow(clientTsMs);
    }

    /* ---- 清空称重相关证据与计数（对齐 _reset_weighing L495-505）---- */
    function resetWeighing() {
      rawWindow = [];
      enterSustain = 0;
      leaveCount = 0;
      pendingCandidate = null;
    }

    /* ---- _stableSuffix（核心，对齐 L609-642）----
     * 取当前 epoch 的读数，从最新往回走连续一段，同时满足：
     *   (a) 每条与最新读数 clientTsMs 差 <= stable_max_age_s*1000；
     *   (b) 段内 max-min <= stable_weight_tol；
     *   (c) 段长 >= stable_min_raw_reads；
     *   (d) 最新一条与段中位数差 <= tol。
     * 满足返回 {medianW, medianConf}，否则 null。
     */
    function stableSuffix() {
      var reads = [];
      for (var i = 0; i < rawWindow.length; i++) {
        if (rawWindow[i].epoch === weighingEpoch) reads.push(rawWindow[i]);
      }
      if (reads.length < cfg.stable_min_raw_reads) return null;

      var latest = reads[reads.length - 1];
      var maxAgeMs = cfg.stable_max_age_s * 1000.0;
      var suffix = [];
      for (var j = reads.length - 1; j >= 0; j--) {
        var r = reads[j];
        if (latest.clientTsMs - r.clientTsMs > maxAgeMs) break;
        var weightsSoFar = [];
        for (var k = 0; k < suffix.length; k++) weightsSoFar.push(suffix[k].weight);
        weightsSoFar.push(r.weight);
        var wmax = weightsSoFar[0], wmin = weightsSoFar[0];
        for (var m = 1; m < weightsSoFar.length; m++) {
          if (weightsSoFar[m] > wmax) wmax = weightsSoFar[m];
          if (weightsSoFar[m] < wmin) wmin = weightsSoFar[m];
        }
        if (wmax - wmin > cfg.stable_weight_tol) break;
        suffix.push(r);
      }
      // reverse 到时间正序（与 Python 的 suffix.reverse() 等价）
      suffix.reverse();

      if (suffix.length < cfg.stable_min_raw_reads) return null;
      // suffix 末尾必须是 latest（与 Python L634 一致）
      var lastSuffix = suffix[suffix.length - 1];
      if (lastSuffix !== latest && lastSuffix.frameSeq !== latest.frameSeq) return null;

      var weights = [];
      var confs = [];
      for (var p = 0; p < suffix.length; p++) {
        weights.push(suffix[p].weight);
        confs.push(suffix[p].confidence);
      }
      var medianW = median(weights);
      if (Math.abs(latest.weight - medianW) > cfg.stable_weight_tol) return null;
      return { medianW: medianW, medianConf: median(confs) };
    }

    /* ---- emit helper ---- */
    function emit(type, payload) {
      try { onEvent(type, payload || {}); } catch (_) {}
    }

    function setState(next) {
      if (next === state) return;
      state = next;
      emit("state", { state: state });
    }

    /* ---- stale 边沿：在需要重量的状态下，缓存无新鲜读数 → 下发 'stale' 事件。
     * 过期读数已被各 handler 当作 None 处理（不写入 rawWindow），状态推进暂停。 */
    function reportStaleEdge() {
      var needWeight = state === "armed" || state === "weighing" || state === "wait_clear";
      var stale = needWeight && freshGrams() === null;
      if (stale !== lastStaleReported) {
        lastStaleReported = stale;
        emit("stale", { stale: stale });
      }
    }

    /* ---- 当前 attempt 的广播辅助（对齐 _handle_weighing 末尾）---- */
    function announce(weightW, weightConf, frameSeq, clientTsMs) {
      var g = round2(weightW);
      var raw = freshRaw();
      var attempt = {
        attemptId: makeAttemptId(),
        weightG: g,
        confidence: weightConf,
        frameSeq: frameSeq,
        clientTsMs: clientTsMs,
        state: "announced",
        createdAtMs: now(),
        weightRaw: raw !== null ? raw : null,
      };
      currentAttempt = attempt;
      // rawWindow 清空（与 Python L976 一致）
      rawWindow = [];
      leaveCount = 0;
      pendingCandidate = null;
      lastCandidate = g;
      lastConfidence = weightConf;
      setState("announced");
      emit("announce", { weight_g: g, weight_raw: attempt.weightRaw });
      return attempt;
    }

    /* =================================================================
     * 状态 handlers（对齐 _handle_*）
     * ================================================================= */

    // calibrating：纯重量化改造。连续 calibrate_min_reads 条新鲜读数且都
    // <= empty_max → armed。任何一条 > empty_max 或 stale/缺失 → 清零计数。
    function handleCalibrating(g) {
      if (g === null || g > cfg.empty_max) {
        calibrateGood = 0;
        return;
      }
      calibrateGood += 1;
      if (calibrateGood >= cfg.calibrate_min_reads) {
        calibrateGood = 0;
        setState("armed");
      }
    }

    // armed（对齐 _handle_armed L802-844）
    function handleArmed(g, conf, frameSeq, clientTsMs) {
      if (g === null || conf < cfg.min_confidence || g <= cfg.enter_min) {
        // 回落 / 无效：清空进入证据，重新开始；清空 rawWindow
        enterSustain = 0;
        rawWindow = [];
        return;
      }
      // 可信非零读数：保留为当前 epoch 的原始证据（进入 weighing 后不清空）
      appendRawRead(frameSeq, clientTsMs, g, conf);
      enterSustain += 1;
      if (enterSustain >= Math.max(1, cfg.enter_sustain_frames)) {
        enterSustain = 0;
        leaveCount = 0;
        // 不清空 rawWindow：armed 证据延续到 weighing
        setState("weighing");
      }
    }

    // weighing（对齐 _handle_weighing L846-982）
    function handleWeighing(g, conf, frameSeq, clientTsMs) {
      // 1) 早退：weight <= leave_max 连续 enter_sustain_frames 次 → 回 armed
      if (g !== null && g <= cfg.leave_max) {
        leaveCount += 1;
        if (leaveCount >= Math.max(1, cfg.enter_sustain_frames)) {
          leaveCount = 0;
          resetWeighing();
          setState("armed");
          return;
        }
      } else {
        // > leave_max 或 stale：清零 leaveCount（stale 时 g===null 进 else 分支）
        leaveCount = 0;
      }

      // 2) 无效读数（None 或 <= enter_min 或 conf 不足）→ 直接返回
      if (g === null || conf < cfg.min_confidence || g <= cfg.enter_min) return;

      // 3) 有效读数追加 rawWindow
      appendRawRead(frameSeq, clientTsMs, g, conf);

      // 4) stableSuffix；None 则返回
      var stable = stableSuffix();
      if (stable === null) return;

      var suffixW = stable.medianW;
      var suffixConf = stable.medianConf;
      var tol = cfg.stable_weight_tol;

      // 5) 候选确认期（对齐 L907-951）
      var pc = pendingCandidate;
      if (pc === null) {
        // 首次形成候选；不播报，等后续独立确认读数
        pendingCandidate = {
          median: suffixW,
          medianConf: suffixConf,
          firstTsMs: clientTsMs,
          confirmCount: 0,
        };
        return;
      }

      if (Math.abs(suffixW - pc.median) > tol) {
        // 平台切换：撤销候选，用新读数重启候选
        pendingCandidate = {
          median: suffixW,
          medianConf: suffixConf,
          firstTsMs: clientTsMs,
          confirmCount: 0,
        };
        return;
      }

      // 确认读数：仍在容差内
      pc.confirmCount += 1;
      pc.median = suffixW;
      pc.medianConf = suffixConf;

      if (pc.confirmCount < cfg.stable_confirm_raw_reads) return;
      // 可选的最小跨度校验：跨度不足则继续等下一条读数
      if (cfg.stable_min_span_ms > 0 && (clientTsMs - pc.firstTsMs) < cfg.stable_min_span_ms) return;

      // 确认通过：播报
      announce(suffixW, suffixConf, frameSeq, clientTsMs);
    }

    // announced：默认不自动接受（announce_hold_s=0）。自动接受路径保留以对齐
    // Python _handle_announced L984-1002。
    function handleAnnounced() {
      if (currentAttempt !== null) {
        lastCandidate = currentAttempt.weightG;
        lastConfidence = currentAttempt.confidence;
      }
      if (cfg.announce_hold_s > 0) {
        var since = (now() - (currentAttempt ? currentAttempt.createdAtMs : 0)) / 1000.0;
        if (since >= cfg.announce_hold_s && currentAttempt !== null) {
          // 自动接受（视为用户已确认）
          doAcceptLocked();
        }
      }
    }

    // wait_clear（对齐 _handle_wait_clear L1004-1030）
    function handleWaitClear(g) {
      if ((now() - waitClearAt) / 1000.0 >= cfg.clear_timeout_s) {
        // 超时：直接进入 armed，避免永久卡在 wait_clear
        clearCount = 0;
        resetWeighing();
        setState("armed");
        return;
      }
      if (g === null) {
        // stale 读数：不累加也不清零（暂停等待）
        return;
      }
      if (g <= cfg.empty_max) {
        clearCount += 1;
      } else {
        clearCount = 0;
      }
      // 连续 1 次空秤 → accepted 瞬态 → armed（同只鼠重称也走此路径）
      if (clearCount >= 1) {
        clearCount = 0;
        resetWeighing();
        weighingEpoch += 1; // epoch 隔离
        setState("armed");
        emit("ready_next", {});
      }
    }

    /* ---- accept 内部实现（持锁语义；JS 单线程无锁）---- */
    function doAcceptLocked() {
      if (currentAttempt === null) return null;
      var attempt = currentAttempt;
      attempt.state = "accepted";
      accepted.push(attempt);
      currentAttempt = null;
      resetWeighing();
      clearCount = 0;
      waitClearAt = now();
      setState("wait_clear");
      emit("accept", { weight_g: attempt.weightG, ordinal: accepted.length });
      return attempt;
    }

    /* =================================================================
     * 推进状态机一次（读数驱动 / tick 共用）
     * g/conf/frameSeq/clientTsMs 来自当前 BLE 缓存的新鲜读数；缓存过期或
     * 无读数时 g===null（对应 Python 的 _read_weight_once 返回 None）。
     * ================================================================= */
    function advance(g, conf, frameSeq, clientTsMs) {
      if (g !== null) {
        lastCandidate = g;
        lastConfidence = conf;
      }

      var s = state;
      if (s === "calibrating") {
        handleCalibrating(g);
      } else if (s === "armed") {
        handleArmed(g, conf, frameSeq, clientTsMs);
      } else if (s === "weighing") {
        handleWeighing(g, conf, frameSeq, clientTsMs);
      } else if (s === "announced") {
        handleAnnounced();
      } else if (s === "wait_clear") {
        handleWaitClear(g);
      }
      // accepted 是瞬态：上一轮 setState('accepted') 不会发生（我们直接进 armed），
      // 这里不单独处理。

      reportStaleEdge();
    }

    /* =================================================================
     * Public API
     * ================================================================= */

    /* 读数驱动：校验并缓存 BLE 读数，然后推进一次状态机。
     * 返回 true 表示读数已更新缓存；false 表示因 sequence 非单调被忽略。
     * 读数形状非法时返回 false（缓存不变），不抛错（前端容错）。 */
    function ingestReading(r) {
      if (!isValidReading(r)) return false;
      if (r.sequence <= lastSequence) return false;
      lastSequence = r.sequence;
      bleReading = {
        grams: r.grams,
        raw: r.raw,
        sequence: r.sequence,
        receivedAtEpochMs: r.receivedAtEpochMs,
        receivedMonotonicMs: now(),
      };
      // 用本条读数推进一次（conf 恒 1.0，BLE 来源可信）
      advance(r.grams, 1.0, r.sequence, r.receivedAtEpochMs);
      return true;
    }

    /* 定时器驱动：只推进"无新读数也需处理"的时间逻辑（wait_clear 超时、
     * announced 自动接受超时、stale 边沿），**绝不往证据窗注入读数**。
     *
     * 真机 bug 修复（重量未稳即播报）：此前 tick() 用当前缓存读数调 advance()，
     * 而 armed/weighing handler 会把该读数 appendRawRead 进证据窗——150ms 定时器
     * 反复把同一条缓存读数计入稳定窗，同一重量 ~0.5s 就凑满 stable_min_raw_reads+confirm
     * → 重量还在爬升/抖动就播报。证据只能来自 ingestReading 的真实新读数（与 Python
     * 帧驱动语义一致：每帧=一条真实新样本，绝不重复用旧样本充数）。
     * wait_clear/announced 的超时推进不需要新证据，故 tick 只调这两个 handler。 */
    function tick() {
      if (state === "wait_clear") {
        // handleWaitClear 内部先查超时（超时→armed），g=null 直接返回、不动 clearCount、
        // 不注入证据。
        handleWaitClear(null);
      } else if (state === "announced") {
        // announce_hold_s 自动接受超时（默认 0=关闭则不动作）。
        handleAnnounced();
      }
      // armed/weighing/calibrating 需要真实新读数才能推进，tick 不驱动它们。
      reportStaleEdge();
    }

    /* 用户接受当前播报。仅在 announced 态生效。
     * 返回被接受的 attempt（含 weight_g/ordinal），否则 null。 */
    function accept() {
      if (state !== "announced" || currentAttempt === null) return null;
      return doAcceptLocked();
    }

    /* 用户重称。仅在 announced 态生效。产品语义：同一只鼠留在秤上重新采样，
     * 直接进入 weighing 并递增 weighing epoch，使旧证据无法进入新窗口。
     * 返回 {applied, state, epoch}。 */
    function retry() {
      if (state !== "announced") {
        return { applied: false, state: state, epoch: weighingEpoch };
      }
      if (currentAttempt !== null) currentAttempt.state = "rejected";
      currentAttempt = null;
      weighingEpoch += 1;
      resetWeighing();
      setState("weighing");
      return { applied: true, state: state, epoch: weighingEpoch };
    }

    /* 当前状态快照。 */
    function getState() {
      return {
        state: state,
        weightCandidate: lastCandidate,
        mouseCount: accepted.length,
        lastGrams: bleReading ? bleReading.grams : null,
        epoch: weighingEpoch,
      };
    }

    /* 重置整个会话（回 calibrating，清空所有证据/计数/缓存）。 */
    function reset() {
      state = "calibrating";
      calibrateGood = 0;
      enterSustain = 0;
      leaveCount = 0;
      clearCount = 0;
      rawWindow = [];
      weighingEpoch = 0;
      pendingCandidate = null;
      currentAttempt = null;
      accepted = [];
      waitClearAt = 0;
      bleReading = null;
      lastSequence = -1;
      lastCandidate = null;
      lastConfidence = 0.0;
      lastStaleReported = false;
      emit("state", { state: state });
    }

    return {
      ingestReading: ingestReading,
      tick: tick,
      accept: accept,
      retry: retry,
      getState: getState,
      reset: reset,
      // 调试/测试用只读视图
      getConfig: function () { return cfg; },
    };
  }

  /* 唯一 attempt id（12 位十六进制；非密码学用途）。 */
  var _idCounter = 0;
  function makeAttemptId() {
    _idCounter = (_idCounter + 1) >>> 0;
    var t = Date.now().toString(16);
    var c = _idCounter.toString(16);
    return (t + c).slice(0, 12);
  }

  return {
    createSession: createSession,
    mergeConfig: mergeConfig,
    validateConfig: validateConfig,
    isValidReading: isValidReading,
    median: median,
    round2: round2,
    DEFAULT_CONFIG: DEFAULT_CONFIG,
  };
});
