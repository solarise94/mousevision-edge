/* 离线称重记录上报客户端 (report-client.js)
 *
 * 纯 app 化改造：称重记录先在手机本地落队列（outbox），联网后自动补传到
 * 后端 POST /api/records/report 汇聚。零依赖、可注入存储/时钟/发送函数便于测试。
 *
 * 设计目标（与 mobile.js 现有上传策略保持一致的取舍）：
 *   1) 离线称重不丢数据：每次 enqueue 都立即持久化到 storage；reload 后从
 *      storage 恢复整个队列。称重过程中无需联网，断网可继续记录。
 *   2) 幂等：每条 record 由客户端生成 record_id（uuid）；flush 成功才把整批
 *      从队列移除；网络失败保留重发，服务端按 record_id 去重，补传不会产生
 *      重复数据。
 *   3) 网络失败保留（离线不丢）、4xx 参数错误进死信（避免坏批次卡死整条
 *      队列导致后续正常批次永远发不出去）。
 *
 * 视频证据不进 outbox（取舍）：
 *   outbox 只存小 JSON 元数据，避免 IndexedDB/大体积 Blob 序列化复杂度。
 *   视频证据由 mobile.js 在完成本箱（"结束称重"）时单独一次性上传：
 *   - 在线 → 立即上传视频；
 *   - 离线 → 跳过视频，只保证称重记录入队（记录是分析的核心数据，视频是
 *     辅助证据；离线丢视频可接受，丢记录不可接受）。
 *   mobile.js 可在 enqueue 时通过 batch.video 字段附挂当前批次对应的视频
 *   Blob，在线补传时随记录一起发；但该 Blob 不会被持久化——reload 后视频
 *   丢失，记录仍会照常补传（无 video 字段）。这是有意的取舍。
 *
 * UMD：浏览器挂 window.ReportClient；node 测试 require 该模块。
 * 风格参照同目录 scale-bridge.js。
 */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (root && typeof root === "object") {
    root.ReportClient = api;
  }
})(typeof window !== "undefined" ? window : this, function () {
  "use strict";

  var DEFAULT_STORAGE_KEY = "mv.reportOutbox.v1";
  var DEFAULT_ENDPOINT = "/api/records/report";
  var TOKEN_META_SELECTOR = 'meta[name="mousevision-api-token"]';

  // 周期重试参数（与 scale-bridge 的 stale/心跳风格一致，全部可注入便于测试）
  var DEFAULT_BASE_INTERVAL_MS = 15000;   // 起始 15s
  var DEFAULT_MAX_INTERVAL_MS = 5 * 60 * 1000; // 上限 5min
  var BACKOFF_FACTOR = 2;                  // 指数退避倍数

  /* ------------------------------------------------------------------ *
   * uuid 生成：优先 crypto.randomUUID（现代浏览器/Node 19+），不可用时
   * 用 RFC4122 v4 回退（Math.random，非密码学强度，此处仅作客户端去重键）。
   * ------------------------------------------------------------------ */
  function uuid() {
    try {
      var c = (typeof crypto !== "undefined") ? crypto : null;
      if (c && typeof c.randomUUID === "function") {
        return c.randomUUID();
      }
    } catch (_) {}
    // 回退 v4
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (ch) {
      var r = (Math.random() * 16) | 0;
      var v = ch === "x" ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  /* ------------------------------------------------------------------ *
   * 从页面 meta 读取鉴权 token（与 mobile.js uploadVideo 一致）。
   * 返回字符串或空串。可注入 document 便于测试。
   * ------------------------------------------------------------------ */
  function readTokenFromDocument(doc) {
    var d = doc || (typeof document !== "undefined" ? document : null);
    if (!d || typeof d.querySelector !== "function") return "";
    try {
      var el = d.querySelector(TOKEN_META_SELECTOR);
      var c = el && el.content;
      return typeof c === "string" ? c.trim() : "";
    } catch (_) {
      return "";
    }
  }

  /* ------------------------------------------------------------------ *
   * 解析 token 选项：字符串 → 直接用；函数 → 调用取值；否则从 document 读。
   * ------------------------------------------------------------------ */
  function resolveToken(opt) {
    if (typeof opt === "string") return opt;
    if (typeof opt === "function") {
      try { var v = opt(); return typeof v === "string" ? v : ""; } catch (_) { return ""; }
    }
    return "";
  }

  /* ------------------------------------------------------------------ *
   * 构造一条 record（称重记录的客户端形态）。
   * 自动补 record_id（uuid）+ recorded_at（ISO8601）。
   * 形状与后端契约一致：
   *   {record_id, ordinal, weight_g, weight_raw?, recorded_at?,
   *    clip_start_ms?, clip_end_ms?}
   * nowISO 可注入便于测试（默认 new Date().toISOString()）。
   * ------------------------------------------------------------------ */
  function buildRecord(input, nowISO) {
    input = input || {};
    if (typeof input.weight_g !== "number" || !isFinite(input.weight_g)) {
      throw new Error("buildRecord: weight_g 必须是有限数");
    }
    if (typeof input.ordinal !== "number" || !isFinite(input.ordinal) || Math.floor(input.ordinal) !== input.ordinal) {
      throw new Error("buildRecord: ordinal 必须是整数");
    }
    var rec = {
      record_id: (typeof input.record_id === "string" && input.record_id) ? input.record_id : uuid(),
      ordinal: input.ordinal,
      weight_g: input.weight_g
    };
    if (input.weight_raw != null && typeof input.weight_raw === "number" && isFinite(input.weight_raw)) {
      rec.weight_raw = input.weight_raw;
    }
    // recorded_at：优先用调用方提供的（ISO8601），否则用注入时钟或当前时间
    if (typeof input.recorded_at === "string" && input.recorded_at) {
      rec.recorded_at = input.recorded_at;
    } else {
      rec.recorded_at = typeof nowISO === "function"
        ? nowISO()
        : (typeof nowISO === "string" ? nowISO : new Date().toISOString());
    }
    if (typeof input.clip_start_ms === "number" && isFinite(input.clip_start_ms)) {
      rec.clip_start_ms = Math.floor(input.clip_start_ms);
    }
    if (typeof input.clip_end_ms === "number" && isFinite(input.clip_end_ms)) {
      rec.clip_end_ms = Math.floor(input.clip_end_ms);
    }
    return rec;
  }

  /* ------------------------------------------------------------------ *
   * 内存版 localStorage（node 测试用）。导出便于测试构造。
   * ------------------------------------------------------------------ */
  function createMemoryStorage() {
    var store = {};
    return {
      getItem: function (k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
      setItem: function (k, v) { store[k] = String(v); },
      removeItem: function (k) { delete store[k]; },
      // 测试辅助（非标准）
      _dump: function () { return store; }
    };
  }

  /* ------------------------------------------------------------------ *
   * outbox 工厂。opts 全部可选，依赖注入便于测试：
   *   storage      localStorage 兼容对象（getItem/setItem/removeItem）
   *   key          存储键，默认 "mv.reportOutbox.v1"
   *   token        字符串 / ()=>string；缺省则每次 flush 时从 document 读
   *   endpoint     默认 "/api/records/report"
   *   fetchFn      注入 fetch（默认全局 fetch）
   *   now          注入时钟（默认 Date.now）
   *   onChange     队列变化回调 (pendingCount)=>void
   *   addEventListener / removeEventListener  注入 window 事件（测试模拟 online）
   *   setInterval / clearInterval              注入定时器（测试模拟退避）
   *   document     注入 document（测试读 token）
   *   baseIntervalMs / maxIntervalMs          退避参数
   * ------------------------------------------------------------------ */
  function createOutbox(opts) {
    opts = opts || {};

    var storage = opts.storage ||
      (typeof localStorage !== "undefined" ? localStorage : null);
    var key = (typeof opts.key === "string" && opts.key) ? opts.key : DEFAULT_STORAGE_KEY;
    var tokenOpt = opts.token; // string | function | undefined
    var endpoint = (typeof opts.endpoint === "string" && opts.endpoint) ? opts.endpoint : DEFAULT_ENDPOINT;
    var fetchFn = opts.fetchFn ||
      (typeof fetch !== "undefined" ? fetch : null);
    var now = typeof opts.now === "function" ? opts.now : function () { return Date.now(); };
    var onChangeCb = typeof opts.onChange === "function" ? opts.onChange : null;

    var addEventListener = opts.addEventListener ||
      (typeof window !== "undefined" && window.addEventListener ? function (t, fn) { window.addEventListener(t, fn); } : null);
    var removeEventListener = opts.removeEventListener ||
      (typeof window !== "undefined" && window.removeEventListener ? function (t, fn) { window.removeEventListener(t, fn); } : null);
    var setIntervalFn = opts.setInterval || (typeof setInterval !== "undefined" ? setInterval : null);
    var clearIntervalFn = opts.clearInterval || (typeof clearInterval !== "undefined" ? clearInterval : null);
    var docRef = opts.document || (typeof document !== "undefined" ? document : null);

    var baseIntervalMs = typeof opts.baseIntervalMs === "number" ? opts.baseIntervalMs : DEFAULT_BASE_INTERVAL_MS;
    var maxIntervalMs = typeof opts.maxIntervalMs === "number" ? opts.maxIntervalMs : DEFAULT_MAX_INTERVAL_MS;

    // 内部队列：[{clientBatchId, enqueuedAt, batch:{cage_id,...,records:[...]}, videoBlobRef?}]
    // videoBlobRef 不参与 JSON 序列化（见 persist）。
    var queue = [];
    // 死信：4xx 参数错误批次，避免卡死整条队列。数组便于排查。
    var deadLetter = [];

    var started = false;
    var onlineHandler = null;
    var retryTimer = null;
    var consecutiveFailures = 0;
    var inFlight = false; // 防止 flush 重入

    /* ---------- 持久化 ---------- */
    function persist() {
      if (!storage || typeof storage.setItem !== "function") return;
      try {
        // 只序列化可 JSON 化的字段：剥掉 videoBlobRef（Blob/File 不可序列化，
        // 且我们有意不持久化视频，见文件头注释）。
        // readings 是普通对象（dev 采集的天平读数时间序列），可持久化，与 videoBlobRef 不同。
        var serializable = queue.map(function (item) {
          var o = { clientBatchId: item.clientBatchId, enqueuedAt: item.enqueuedAt, batch: item.batch };
          if (item.readings) o.readings = item.readings;
          return o;
        });
        storage.setItem(key, JSON.stringify({ v: 1, queue: serializable }));
      } catch (_) {
        // 存储失败（quota / 不可写）不应阻断 enqueue 主流程；记录已入内存队列，
        // 下次成功 persist 时再落盘。调用方可通过 onChange 监控 pendingCount。
      }
    }

    function restore() {
      if (!storage || typeof storage.getItem !== "function") return;
      try {
        var raw = storage.getItem(key);
        if (!raw || typeof raw !== "string") return;
        var parsed = JSON.parse(raw);
        if (!parsed || !Array.isArray(parsed.queue)) return;
        queue = parsed.queue.filter(function (item) {
          return item && typeof item === "object" && item.batch && Array.isArray(item.batch.records);
        }).map(function (item) {
          var o = {
            clientBatchId: (typeof item.clientBatchId === "string" && item.clientBatchId) ? item.clientBatchId : uuid(),
            enqueuedAt: typeof item.enqueuedAt === "number" ? item.enqueuedAt : 0,
            batch: item.batch
            // videoBlobRef 不恢复（reload 后视频丢失，记录仍补传）
          };
          // readings 是普通对象，可恢复（与 videoBlobRef 不同）
          if (item.readings && typeof item.readings === "object") o.readings = item.readings;
          return o;
        });
      } catch (_) {
        // 损坏的存储：清空内存队列避免使用半截数据。原 storage 不动（保守）。
        queue = [];
      }
    }

    function notify() {
      if (onChangeCb) {
        try { onChangeCb(queue.length); } catch (_) {}
      }
    }

    /* ---------- 取 token ---------- */
    function currentToken() {
      if (tokenOpt !== undefined) return resolveToken(tokenOpt);
      return readTokenFromDocument(docRef);
    }

    /* ---------- 构造 FormData ---------- */
    function buildFormData(item) {
      // FormData 在浏览器/node18+ 全局可用；测试通过假 fetchFn 验证字段，
      // 因此这里需要真实 FormData（node 18 起实验性内置；若环境无 FormData，
      // flush 时会抛错——这是运行期依赖，与 mobile.js 用 FormData 一致）。
      var FD = (typeof FormData !== "undefined") ? FormData : null;
      if (!FD) throw new Error("FormData 不可用");
      var fd = new FD();
      var b = item.batch || {};
      if (b.cage_id != null) fd.append("cage_id", String(b.cage_id));
      if (b.strain != null) fd.append("strain", String(b.strain));
      if (b.project_id != null) fd.append("project_id", String(b.project_id));
      if (b.device_id != null) fd.append("device_id", String(b.device_id));
      if (b.weight_source != null) fd.append("weight_source", String(b.weight_source));
      // records：JSON 字符串数组
      fd.append("records", JSON.stringify(b.records || []));
      // dev 采集的天平读数时间序列：普通对象，附为 JSON Blob 文件字段。
      // 可持久化到 localStorage outbox（与 video Blob 不同），reload 后仍随记录补传。
      if (item.readings && typeof item.readings === "object") {
        try {
          var rj = JSON.stringify(item.readings);
          var rblob = new Blob([rj], { type: "application/json" });
          fd.append("readings", rblob, "readings.json");
        } catch (_) { /* readings 形状异常 → 跳过，仍发记录 */ }
      }
      // 视频证据：仅当本批次附带未过期的 Blob 时附挂（reload 后丢失，跳过）
      if (item.videoBlobRef) {
        var v = item.videoBlobRef;
        var blob = typeof v === "function" ? v() : v;
        if (blob) {
          try {
            var name = (blob && typeof blob.name === "string" && blob.name) ? blob.name : ("evidence-" + item.clientBatchId + ".mp4");
            fd.append("video", blob, name);
          } catch (_) { /* Blob 形状异常 → 跳过视频，仍发记录 */ }
        }
      }
      return fd;
    }

    /* ---------- 判定单批次发送结果 ----------
     * 返回：
     *   "ok"     —— 成功，移出队列
     *   "retry"  —— 网络/5xx，保留重试
     *   "dead"   —— 4xx 参数错误，进死信
     */
    function classifyResult(res) {
      // fetch reject（网络错误）在外层捕获，归类为 retry
      if (!res) return "retry";
      var status = typeof res.status === "number" ? res.status : 0;
      if (status >= 200 && status < 300) {
        // 进一步要求 body.ok === true
        if (res._body && res._body.ok === true) return "ok";
        // body 缺失或 ok 非 true：保守视为 retry（可能后端还在处理）
        return "retry";
      }
      if (status >= 400 && status < 500) return "dead"; // 参数错误
      // 5xx / 其它 → retry
      return "retry";
    }

    /* ---------- 发送单批 ---------- */
    function sendOne(item) {
      var fd;
      try { fd = buildFormData(item); }
      catch (e) { return Promise.resolve({ kind: "dead", reason: "formdata-error", error: e }); }
      var headers = {};
      var tok = currentToken();
      if (tok) headers["X-MouseVision-Token"] = tok;

      if (!fetchFn) {
        // 无 fetch（node 测试未注入 / 浏览器降级）→ 视为 retry，不丢数据
        return Promise.resolve({ kind: "retry", reason: "no-fetch" });
      }

      return Promise.resolve()
        .then(function () { return fetchFn(endpoint, { method: "POST", headers: headers, body: fd }); })
        .then(function (res) {
          // 尝试解析 body（res.json() 或注入的假 fetch 直接返回 _body）
          var bodyP;
          try {
            if (res && typeof res.json === "function") {
              bodyP = Promise.resolve(res.json());
            } else {
              bodyP = Promise.resolve(res && res._body);
            }
          } catch (_) { bodyP = Promise.resolve(null); }
          return bodyP.then(function (body) {
            // 判定用普通对象携带 status + 解析后的 body。
            // 不能用 Object.create(res) 再赋值 status：Response.prototype.status 是
            // 只读 getter，严格模式下 withBody.status=... 会抛 TypeError（被外层
            // catch 误判为 retry）——导致设备其实上报成功(201)却显示"等待联网"并
            // 无限重传。该 bug 只在真实 fetch 下出现（测试假 fetch 自带 _body，
            // 不走此分支），真机实测暴露。
            var statusCode = (res && typeof res.status === "number") ? res.status : 0;
            var bodyFinal = body || (res && res._body) || null;
            return {
              kind: classifyResult({ status: statusCode, _body: bodyFinal }),
              res: res,
              body: bodyFinal,
            };
          }, function () {
            // body 解析失败：仅凭 status 判定（classify 在 status 2xx 但无 body.ok 时 retry）
            var statusCode2 = (res && typeof res.status === "number") ? res.status : 0;
            return { kind: classifyResult({ status: statusCode2, _body: res && res._body }), res: res, body: null };
          });
        })
        .catch(function () {
          // 网络/拒绝 → retry（离线不丢）
          return { kind: "retry" };
        });
    }

    /* ---------- flush：按入队顺序逐批发送 ---------- */
    function flush() {
      if (inFlight) return Promise.resolve({ sent: 0, remaining: queue.length });
      inFlight = true;
      var sentCount = 0;

      function step() {
        if (queue.length === 0) {
          inFlight = false;
          return { sent: sentCount, remaining: 0 };
        }
        var item = queue[0];
        return sendOne(item).then(function (r) {
          if (r.kind === "ok") {
            queue.shift();
            sentCount += 1;
            persist();
            notify();
            return step(); // 继续下一批
          }
          if (r.kind === "dead") {
            // 4xx：进死信，移出队列，不阻塞后续
            var dead = queue.shift();
            var deadEntry = {
              clientBatchId: dead.clientBatchId,
              enqueuedAt: dead.enqueuedAt,
              batch: dead.batch,
              failedAt: now(),
              reason: "4xx"
            };
            if (dead.readings) deadEntry.readings = dead.readings;
            deadLetter.push(deadEntry);
            persist();
            notify();
            return step(); // 继续下一批（不死信卡死）
          }
          // retry：停止，保留该批及后续（离线不丢）
          inFlight = false;
          // 失败一次 → 累加连续失败，触发退避重排
          consecutiveFailures += 1;
          rescheduleRetry();
          return { sent: sentCount, remaining: queue.length };
        });
      }

      return Promise.resolve().then(step).then(function (result) {
        // 成功清空（无 retry 终止）→ 重置失败计数，停止快速重试
        if (result.remaining === 0) {
          consecutiveFailures = 0;
          rescheduleRetry();
        }
        return result;
      }, function (e) {
        // 意外异常（不应发生）：解锁并保留队列
        inFlight = false;
        consecutiveFailures += 1;
        rescheduleRetry();
        return { sent: sentCount, remaining: queue.length, error: e };
      });
    }

    /* ---------- 退避重试调度 ----------
     * 连续失败次数越多，下次重试越晚（指数退避到上限）。
     * 成功后重置。start() 注册周期定时器；这里只负责失败后重新排程更长的间隔。
     */
    function nextInterval() {
      if (consecutiveFailures <= 0) return baseIntervalMs;
      // 每多一次失败，间隔翻倍，封顶 maxIntervalMs
      var n = baseIntervalMs * Math.pow(BACKOFF_FACTOR, consecutiveFailures - 1);
      return Math.min(n, maxIntervalMs);
    }

    function rescheduleRetry() {
      if (!started) return;
      if (!setIntervalFn || !clearIntervalFn) return;
      if (retryTimer) {
        try { clearIntervalFn(retryTimer); } catch (_) {}
        retryTimer = null;
      }
      if (queue.length === 0) return; // 队列空 → 不排程
      var ms = nextInterval();
      // 用 setInterval 语义但当成"单次延时"使用：回调里立即清掉自己。
      // 注入的测试 fake setInterval 通常记录 {fn, ms}，验证 ms 即可。
      retryTimer = setIntervalFn(function () {
        if (retryTimer && clearIntervalFn) {
          try { clearIntervalFn(retryTimer); } catch (_) {}
          retryTimer = null;
        }
        flush();
      }, ms);
    }

    /* ---------- 首次加载恢复 ---------- */
    restore();

    /* ---------- 对外 API ---------- */
    return {
      // 入队一批。batch.records 应为 buildRecord 构造好的数组（也接受裸对象，
      // 内部不强制重造 record_id，保留调用方原样以便幂等）。
      // videoOpt: 可选 Blob/File 或 ()=>Blob/File，附挂视频证据（不持久化）。
      // readingsOpt: 可选普通对象（dev 采集的天平读数时间序列），可 JSON 序列化、
      //   可持久化到 localStorage outbox（与 videoOpt 不同），随记录补传。
      enqueue: function (batch, videoOpt, readingsOpt) {
        batch = batch || {};
        if (!Array.isArray(batch.records)) {
          throw new Error("enqueue: batch.records 必须是数组");
        }
        var item = {
          clientBatchId: uuid(),
          enqueuedAt: now(),
          batch: batch
        };
        if (videoOpt != null) item.videoBlobRef = videoOpt;
        if (readingsOpt && typeof readingsOpt === "object") item.readings = readingsOpt;
        queue.push(item);
        persist();
        notify();
        // 已 start 且当前没有排程 → 安排一次尽快重试（用 base 间隔）
        if (started) rescheduleRetry();
        return item.clientBatchId;
      },

      flush: flush,

      pending: function () { return queue.length; },

      list: function () {
        // 返回浅拷贝，避免外部直接修改内部队列
        return queue.map(function (item) {
          return {
            clientBatchId: item.clientBatchId,
            enqueuedAt: item.enqueuedAt,
            batch: item.batch
          };
        });
      },

      deadLetters: function () {
        return deadLetter.map(function (d) {
          return { clientBatchId: d.clientBatchId, enqueuedAt: d.enqueuedAt, batch: d.batch, failedAt: d.failedAt, reason: d.reason };
        });
      },

      // 当前计算的下次重试间隔（测试用）
      nextInterval: nextInterval,

      // 连续失败计数（测试用）
      consecutiveFailures: function () { return consecutiveFailures; },

      start: function () {
        if (started) return;
        started = true;
        // online 事件：网络恢复立即 flush
        if (addEventListener) {
          onlineHandler = function () {
            // 恢复在线 → 重置失败计数（网络回来了，用基础间隔）
            consecutiveFailures = 0;
            rescheduleRetry();
            flush();
          };
          try { addEventListener("online", onlineHandler); } catch (_) {}
        }
        // 周期重试（队列非空时）
        rescheduleRetry();
        // 启动时若队列已有积压，立即试一次（覆盖"已联网但还未触发 online"场景）
        if (queue.length > 0) flush();
      },

      stop: function () {
        if (!started) return;
        started = false;
        if (removeEventListener && onlineHandler) {
          try { removeEventListener("online", onlineHandler); } catch (_) {}
          onlineHandler = null;
        }
        if (retryTimer && clearIntervalFn) {
          try { clearIntervalFn(retryTimer); } catch (_) {}
          retryTimer = null;
        }
      }
    };
  }

  return {
    buildRecord: buildRecord,
    createOutbox: createOutbox,
    createMemoryStorage: createMemoryStorage,
    readTokenFromDocument: readTokenFromDocument,
    uuid: uuid,
    DEFAULT_STORAGE_KEY: DEFAULT_STORAGE_KEY,
    DEFAULT_ENDPOINT: DEFAULT_ENDPOINT,
    DEFAULT_BASE_INTERVAL_MS: DEFAULT_BASE_INTERVAL_MS,
    DEFAULT_MAX_INTERVAL_MS: DEFAULT_MAX_INTERVAL_MS
  };
});
