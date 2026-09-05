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
  // B5（合同 §7.3-7.6）：租户 outbox v2——每个工作区一个键；批次内固化
  // {tenant_id, credential_id} 快照；flush 前校验当前凭证绑定租户，不匹配
  // 拒绝发送且批次留在原队列（防止换账号把旧草稿传到错误工作区）。
  var OUTBOX_KEY_V2_PREFIX = "mv.reportOutbox.v2.";
  // 与 ui/control_store.py 的 LEGACY_TENANT_ID 一致（§16-G5 固定 UUID）。
  var LEGACY_DEFAULT_TENANT_ID = "00000000-0000-4000-8000-000000000001";
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
   * 从 window.MV_CONFIG 读取打包 app 注入的同步令牌（与 api-client.js
   * authHeaders 一致）。返回字符串或空串。root 可注入便于测试。
   * ------------------------------------------------------------------ */
  function readTokenFromMvConfig(root) {
    var r = root || (typeof window !== "undefined" ? window : null);
    try {
      var cfg = r && r.MV_CONFIG;
      if (cfg && typeof cfg === "object" && typeof cfg.token === "string") {
        return cfg.token.trim();
      }
    } catch (_) {}
    return "";
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
   * dataURL → Blob 转换（确认瞬间照片）。
   * dataURL 形如 "data:image/jpeg;base64,...."，atob 解码需按字节处理，
   * 不能直接遍历字符串（UTF-16 字符 → 1 字节，会丢高位/越界）。
   * 返回 {blob, mime}；解析失败返回 null（调用方跳过该照片，不阻断上报）。
   * ------------------------------------------------------------------ */
  function dataUrlToBlob(dataUrl) {
    if (typeof dataUrl !== "string") return null;
    var m = /^data:([^;,]+);base64,(.*)$/s.exec(dataUrl);
    if (!m) return null;
    var mime = m[1] || "image/jpeg";
    var b64 = m[2] || "";
    if (!b64) return null;
    try {
      var bin = atob(b64);
      var bytes = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) {
        bytes[i] = bin.charCodeAt(i);
      }
      return { blob: new Blob([bytes], { type: mime }), mime: mime };
    } catch (_) {
      return null;
    }
  }

  // 文件名安全化：record_id 只保留 [A-Za-z0-9_-]，避免路径/表单注入。
  function safePhotoStem(recordId) {
    return String(recordId == null ? "" : recordId).replace(/[^A-Za-z0-9_-]/g, "");
  }

  /* ------------------------------------------------------------------
   * outbox 工厂。opts 全部可选，依赖注入便于测试：
   *   storage      localStorage 兼容对象（getItem/setItem/removeItem）
   *   key          存储键。缺省按租户模式推导（见下）；显式传入时优先生效
   *                （share 通道等自定义键继续用这里）。
   *   tenantId     租户模式：工作区 UUID。键 = "mv.reportOutbox.v2.<tenantId>"，
   *                批次入队时快照 {tenant_id, credential_id}。
   *   credentialId 当前设备凭证 ID（快照进批次；与 tenantId 配套）。
   *   boundTenantId 当前凭证绑定的租户（flush 校验基准；缺省= tenantId）。
   *   legacyDefaultTenantId
   *                legacy 模式：批次固化该租户（= legacy-default），存储键
   *                保持 v1 全局键；flush 发送 JSON 载荷并携带 tenant_id
   *                （合同 §7.6：v1 队列只上传 legacy tenant，不静默迁入新账号）。
   *   token        字符串 / ()=>string；缺省则每次 flush 时从 document 读
   *   endpoint     默认 "/api/records/report"
   *   fetchFn      注入 fetch（默认全局 fetch）
   *   now          注入时钟（默认 Date.now）
   *   onChange     队列变化回调 (pendingCount)=>void
   *   addEventListener / removeEventListener  注入 window 事件（测试模拟 online）
   *   setInterval / clearInterval              注入定时器（测试模拟退避）
   *   document     注入 document（测试读 token）
   *   baseIntervalMs / maxIntervalMs          退避参数
   *
   * 模式判定：legacyDefaultTenantId > tenantId > 默认（v1 全局键，历史行为）。
   * 模式只影响「键选择 / 快照 / flush 校验 / 载荷形态」这一层外壳；防丢骨架
   * （原子持久化、死信迁移、按 clientBatchId 出队、401/403 停止与恢复、退避
   * 重排）完全不变（§14.2）。
   * ------------------------------------------------------------------ */
  function createOutbox(opts) {
    opts = opts || {};

    var storage = opts.storage ||
      (typeof localStorage !== "undefined" ? localStorage : null);
    var legacyTenantId = (typeof opts.legacyDefaultTenantId === "string" && opts.legacyDefaultTenantId)
      ? opts.legacyDefaultTenantId : null;
    var tenantId = (typeof opts.tenantId === "string" && opts.tenantId) ? opts.tenantId : null;
    var credentialId = (typeof opts.credentialId === "string" && opts.credentialId) ? opts.credentialId : "";
    // flush 校验基准：当前凭证绑定的租户。缺省 = 队列自身租户（自洽，恒通过）。
    var boundTenantId = (typeof opts.boundTenantId === "string" && opts.boundTenantId)
      ? opts.boundTenantId
      : (tenantId || legacyTenantId);
    // 租户绑定模式（v2 或 legacy 快照）：入队快照 + flush 校验。
    var tenantBound = !!(tenantId || legacyTenantId);
    // legacy 模式走 JSON 载荷（携带 tenant_id 标识，服务端按凭证解析租户）。
    var jsonPayloadMode = !!legacyTenantId;
    var key;
    if (typeof opts.key === "string" && opts.key) {
      key = opts.key; // 显式键（share 通道等自定义 outbox）
    } else if (legacyTenantId) {
      key = DEFAULT_STORAGE_KEY; // legacy 模式：v1 全局键（队列原位，不迁移）
    } else if (tenantId) {
      key = OUTBOX_KEY_V2_PREFIX + tenantId; // v2：按租户分键
    } else {
      key = DEFAULT_STORAGE_KEY; // 历史默认（无租户语义）
    }
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

    // 死信存储键：在 outbox 主键后追加 ".dead"，与主队列同生命周期、同 storage。
    var deadKey = key + ".dead";

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
    // 最近一次 flush 是否因鉴权（401/403）失败停止；用于 UI 暴露"令牌失效"状态。
    // 每次 flush 开始不主动清零——只有成功发出至少一批（kind==="ok"）才视为恢复。
    var lastAuthFailed = false;
    // 最近一次 persist 是否成功落盘（quota/不可写时为 false）。供 UI 监控。
    var lastPersistOk = true;

    /* ---------- 持久化 ----------
     * 返回 boolean：true=主队列与死信都成功落盘；false=storage 不可用或任一抛错
     * （quota/不可写）。调用方按需决定是否重试/抛错（enqueue 失败要抛、
     * flush 内部失败仍吞）。
     *
     * 注意：必须合并主队列与死信两次写入的结果——死信（4xx 批次）落盘失败时
     * 若只报告成功，批次已从主队列移除，重启后死信永久丢失（死信假落盘）。
     */
    function persist() {
      if (!storage || typeof storage.setItem !== "function") {
        lastPersistOk = false;
        return false;
      }
      // 主队列写入。失败直接返回（quota/不可写），不继续写死信（避免部分落盘误报）。
      var okMain = false;
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
        okMain = true;
      } catch (_) {
        // 存储失败（quota / 不可写）不应阻断 flush 主流程；记录已入内存队列，
        // 下次成功 persist 时再落盘。调用方可通过 lastPersistOk()/onChange 监控。
        lastPersistOk = false;
        return false;
      }
      // 死信一并落盘：4xx 拒收的批次也持久化（独立 key），reload 后仍可查/重发。
      // 合并两次结果：任一失败都视为本次 persist 失败，避免死信假落盘。
      var ok = okMain && persistDead();
      lastPersistOk = ok;
      return ok;
    }

    /* 死信持久化（独立 key，便于排查与隔离）。
     * 返回 boolean：true=成功落盘（含死信为空时写入空数组也算成功）；
     * false=storage 不可用或抛错。不抛错、不直接修改 lastPersistOk
     * （由调用方 persist 合并结果后统一回写）。 */
    function persistDead() {
      if (!storage || typeof storage.setItem !== "function") return false;
      try {
        storage.setItem(deadKey, JSON.stringify({ v: 1, dead: deadLetter }));
        return true;
      } catch (_) {
        return false;
      }
    }

    /* 按 clientBatchId 从主队列精确移除一条（找到并移除返回 true，否则 false）。
     * 用于 flush 完成一批后的出队：必须按「已捕获 item 的 clientBatchId」精确定位，
     * 不能无脑 queue.shift()——否则与进行中的 retry 并发时会删错批次：
     *   step() 捕获 queue[0]=A 并 sendOne(A)（异步在途）期间，用户点另一批 B 的
     *   「重传」retry(B) 把 B 重排到队首 → 队列变 [B,A]；A 的成功回调若执行
     *   queue.shift() 删的是 B，随后 step() 又读 queue[0]=A 再发一次 → A 发两次、
     *   B 从未发送（丢批次）。按 clientBatchId 精确移除即可保证删的正是 A。 */
    function removeQueueItemById(id) {
      for (var i = 0; i < queue.length; i++) {
        if (queue[i] && queue[i].clientBatchId === id) {
          queue.splice(i, 1);
          return true;
        }
      }
      return false;
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
      // 死信恢复（独立 key）：损坏时保守清空，不影响主队列。
      try {
        var draw = storage.getItem(deadKey);
        if (draw && typeof draw === "string") {
          var dparsed = JSON.parse(draw);
          if (dparsed && Array.isArray(dparsed.dead)) {
            deadLetter = dparsed.dead.filter(function (d) {
              return d && typeof d === "object" && d.batch && Array.isArray(d.batch.records);
            });
          }
        }
      } catch (_) {
        deadLetter = [];
      }
      // 去重：过滤掉主队列里 clientBatchId 已出现在死信中的条目。
      // 重复窗口来源——flush 的 dead 分支先 persistDead() 成功（死信已落盘），
      // 紧接着 queue.shift() + persist() 写主队列时若失败（quota），持久化状态是
      // 「主队列还有该批 + 死信也有该批」。此时该批已确认为坏批次，应只保留在
      // 死信、不重复出现在主队列，避免下轮 flush 又尝试上报（徒劳）且 UI 重复计数。
      // 该过滤幂等：正常路径下死信与主队列互斥（移出主队列已成功），无条目被滤掉。
      if (deadLetter.length > 0 && queue.length > 0) {
        var deadIds = {};
        for (var di = 0; di < deadLetter.length; di++) {
          var ditem = deadLetter[di];
          if (ditem && typeof ditem.clientBatchId === "string" && ditem.clientBatchId) {
            deadIds[ditem.clientBatchId] = true;
          }
        }
        queue = queue.filter(function (qitem) {
          return !(qitem && typeof qitem.clientBatchId === "string" && deadIds[qitem.clientBatchId]);
        });
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
      // 优先级对齐 api-client.js authHeaders：打包 app 注入的 MV_CONFIG.token
      // 优先，服务器托管 H5 的 <meta> 兜底（此前只读 meta，app 模式 token 断链
      // 导致上报 401「令牌失效或无权限」）。
      return readTokenFromMvConfig(null) || readTokenFromDocument(docRef);
    }

    /* records JSON 用于上报文本 part 时剔除 photo（base64 dataURL）。
     * 照片只走独立 photos 文件字段（见 buildFormData 下方循环），不能进 records
     * 文本 part——否则 base64 照片会让 records 超 Starlette 1MiB 文本 part 上限 → 400。
     * 不 mutate 原对象：浅拷贝每条 record 并跳过 photo 键，保留持久化批次的 photo
     * 供幂等重传 / 独立 part 重发。 */
    function recordsWithoutPhotos(records) {
      return (records || []).map(function (r) {
        if (!r || typeof r !== "object") return r;
        var copy = {};
        for (var k in r) {
          if (!Object.prototype.hasOwnProperty.call(r, k)) continue;
          if (k === "photo") continue;
          copy[k] = r[k];
        }
        return copy;
      });
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
      // records：JSON 字符串数组，剔除 photo（照片只走下方独立 photos 文件字段，
      // 不进 records 文本 part——避免 base64 照片撑爆 1MiB 文本 part 上限）。
      // photos 循环仍从完整 b.records 读 photo，幂等重传/独立 part 重发不丢照片。
      fd.append("records", JSON.stringify(recordsWithoutPhotos(b.records || [])));
      // 确认瞬间照片：records 里带 photo(dataURL) 的，每条追加一个文件字段 photos，
      // filename <record_id>.jpg（record_id 已按 [A-Za-z0-9_-] 过滤防注入）。
      // 服务端按 filename stem → record_id 建映射；dataURL 转 Blob 用二进制安全 atob。
      var photos = b.records || [];
      for (var pi = 0; pi < photos.length; pi++) {
        var prec = photos[pi];
        if (!prec || typeof prec.photo !== "string" || !prec.photo) continue;
        var converted = dataUrlToBlob(prec.photo);
        if (!converted) continue; // 非法 dataURL → 跳过，仍发记录
        var stem = safePhotoStem(prec.record_id);
        if (!stem) continue; // record_id 被过滤空 → 无法对应，跳过
        try {
          fd.append("photos", converted.blob, stem + ".jpg");
        } catch (_) { /* 单个照片异常 → 跳过该照片，仍发记录 */ }
      }
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
     *   "retry"  —— 网络/5xx/可重试 4xx（408/409/425/429 等），保留重试
     *   "dead"   —— 确定的 payload 校验错误（400/413/422），进死信
     *   "auth"   —— 鉴权失败（401/403）：保留在队列、停止本轮 flush（避免连打）、
     *               通过 lastAuthFailed() 暴露给 UI 提示"令牌失效"。
     * 注意：原来所有 4xx 都进死信，导致 token 错误时记录永久丢失且界面显示
     * "已上报"。现在把 401/403 分离为可恢复的 auth（换 token 后即可继续）。
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
      // 鉴权失败：token 失效/无权限 → 保留重试（换 token 后可恢复），不丢数据
      if (status === 401 || status === 403) return "auth";
      // 确定的 payload 校验错误 → 死信（避免坏批次卡死整条队列）
      if (status === 400 || status === 413 || status === 422) return "dead";
      // 其余 4xx（408 超时/409 冲突/425 太早/429 限流）与 5xx → 可重试
      return "retry";
    }

    /* ---------- legacy 快照模式的 JSON 载荷（合同 §7.6） ----------
     * v1 队列 + legacy-default 身份 flush 时发送 JSON：批次快照的 tenant_id
     * 随载荷声明（服务端按凭证解析租户，客户端字段仅自证）。照片 dataURL
     * 保留在 records 内随 JSON 上传（服务端解码）；视频 Blob 无法进 JSON
     * 载荷——与「reload 后丢视频」同一取舍：丢视频可接受，丢记录不可接受。 */
    function buildJsonPayload(item) {
      var b = item.batch || {};
      var payload = {};
      if (b.tenant_id != null) payload.tenant_id = b.tenant_id;
      if (b.cage_id != null) payload.cage_id = b.cage_id;
      if (b.strain != null) payload.strain = b.strain;
      if (b.project_id != null) payload.project_id = b.project_id;
      if (b.device_id != null) payload.device_id = b.device_id;
      if (b.weight_source != null) payload.weight_source = b.weight_source;
      payload.records = b.records || [];
      if (item.readings && typeof item.readings === "object") payload.readings = item.readings;
      return JSON.stringify(payload);
    }

    /* ---------- 发送单批 ---------- */
    function sendOne(item) {
      var headers = {};
      var tok = currentToken();
      if (tok) headers["X-MouseVision-Token"] = tok;

      var bodyPromise;
      if (jsonPayloadMode) {
        headers["Content-Type"] = "application/json";
        bodyPromise = Promise.resolve(buildJsonPayload(item));
      } else {
        bodyPromise = Promise.resolve()
          .then(function () { return buildFormData(item); })
          .catch(function (e) {
            return { formdataError: e };
          });
      }

      return bodyPromise.then(function (bodyOrErr) {
        if (bodyOrErr && bodyOrErr.formdataError) {
          return { kind: "dead", reason: "formdata-error", error: bodyOrErr.formdataError };
        }

      if (!fetchFn) {
        // 无 fetch（node 测试未注入 / 浏览器降级）→ 视为 retry，不丢数据
        return { kind: "retry", reason: "no-fetch" };
      }

      return Promise.resolve()
        .then(function () { return fetchFn(endpoint, { method: "POST", headers: headers, body: bodyOrErr }); })
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
      });
    }

    /* ---------- flush 前的租户一致性校验（合同 §7.4） ----------
     * 当前凭证绑定的租户（boundTenantId）必须与每个批次的租户快照一致；
     * 任一不一致 → 拒绝本轮 flush（一个批次都不发、全部留在原队列），返回
     * 被拒批次清单供 UI 提示「上一工作区还有 N 条未上传」。旧 v1 批次
     * （无快照）在 legacy 模式下视为 legacy-default。
     * 返回 null = 校验通过；非 null = 被拒批次数组。 */
    function collectTenantMismatch() {
      if (!tenantBound) return null;
      var expected = boundTenantId || legacyTenantId || tenantId;
      var rejected = [];
      for (var i = 0; i < queue.length; i++) {
        var qitem = queue[i];
        var t = qitem && qitem.batch ? qitem.batch.tenant_id : null;
        if (!t && legacyTenantId) t = legacyTenantId;
        if (t !== expected) {
          rejected.push({
            clientBatchId: qitem ? qitem.clientBatchId : "",
            batch_tenant_id: t || null,
            bound_tenant_id: expected
          });
        }
      }
      return rejected.length ? rejected : null;
    }

    /* ---------- flush：按入队顺序逐批发送 ---------- */
    function flush() {
      if (inFlight) return Promise.resolve({ sent: 0, remaining: queue.length, rejected: [] });
      // 租户不一致：拒绝发送、批次原队列保留（一个都不发，防错传，§7.4/§7.5）。
      var mismatched = collectTenantMismatch();
      if (mismatched) {
        return Promise.resolve({ sent: 0, remaining: queue.length, rejected: mismatched });
      }
      inFlight = true;
      var sentCount = 0;

      function step() {
        if (queue.length === 0) {
          inFlight = false;
          return { sent: sentCount, remaining: 0, rejected: [] };
        }
        var item = queue[0];
        return sendOne(item).then(function (r) {
          if (r.kind === "ok") {
            // 按已捕获 item 的 clientBatchId 精确移除：并发 retry 重排后 queue[0]
            // 可能已不是本批，shift() 会误删队首（见 removeQueueItemById 注释）。
            removeQueueItemById(item.clientBatchId);
            sentCount += 1;
            // 成功发出至少一批 → 视为鉴权已恢复（之前可能是 token 临时失效）
            lastAuthFailed = false;
            persist();
            notify();
            return step(); // 继续下一批
          }
          if (r.kind === "dead") {
            // 400/413/422 确定校验错误：进死信，移出队列，不阻塞后续。
            //
            // 顺序很关键（先写死信、成功后再移出主队列）——保证死信持久化失败时
            // 批次仍在主队列里（内存 + 已持久化），下轮 flush 会重试迁移，绝不丢。
            //   旧实现先 shift() 再 persist()：persist 内部先写主队列成功、后写死信；
            //   若死信 key 写入失败（quota），persist 返回 false 但 flush 忽略它继续跑，
            //   导致「主队列已不含该批 + 死信没落盘」→ reload 后两边都空，批次永久丢失。
            //
            // 用已捕获的 item（sendOne 入参，即正在发送的那批）构造 deadEntry，
            // 先 push 到内存死信 → persistDead()：
            //   失败 → pop() 回滚死信，该批保留在主队列，按 retry 语义停止本轮 flush
            //          （inFlight=false、consecutiveFailures++、rescheduleRetry()），
            //          下轮 flush 再试迁移；
            //   成功 → 按 clientBatchId 精确移出主队列 → persist() 写主队列 → notify → 继续。
            // 注意：必须用 item 而非 queue[0]——并发 retry 重排后 queue[0] 可能已不是
            //       正在发送的那批（见 removeQueueItemById 注释）。
            var dead = item; // sendOne 的入参，正是刚判为 dead 的那批
            var deadEntry = {
              clientBatchId: dead.clientBatchId,
              enqueuedAt: dead.enqueuedAt,
              batch: dead.batch,
              failedAt: now(),
              // 保留服务端具体错误，避免 UI 只显示笼统"4xx/服务器拒绝"。
              // formdata 构造异常归 formdata-error，其余 4xx 仍标 "4xx"。
              reason: (r && r.reason === "formdata-error") ? "formdata-error" : "4xx"
            };
            if (dead.readings) deadEntry.readings = dead.readings;
            var resObj = (r && r.res) || null;
            var httpStatus = (resObj && typeof resObj.status === "number") ? resObj.status : 0;
            if (httpStatus) deadEntry.httpStatus = httpStatus;
            if (r && r.body && typeof r.body === "object" && r.body.detail != null) {
              deadEntry.serverDetail = String(r.body.detail);
            }
            deadLetter.push(deadEntry);
            if (!persistDead()) {
              // 死信落盘失败：回滚内存死信，该批留在主队列（内存 + 已持久化），
              // 按 retry 语义停止本轮 flush，下轮再试迁移。批次绝不丢。
              // 同步 lastPersistOk=false：persistDead 按设计不自行回写该状态，
              // 这里显式置位，避免状态接口把本次落盘失败误报为成功（后续
              // persist() 成功时会回写 true 恢复）。
              deadLetter.pop();
              lastPersistOk = false;
              inFlight = false;
              consecutiveFailures += 1;
              rescheduleRetry();
              return { sent: sentCount, remaining: queue.length, rejected: [] };
            }
            // 死信已落盘：按 clientBatchId 精确移出主队列并写主队列持久化。
            removeQueueItemById(dead.clientBatchId);
            persist();
            notify();
            return step(); // 继续下一批（不死信卡死）
          }
          if (r.kind === "auth") {
            // 401/403：token 失效/无权限 → 保留该批及后续，停止本轮 flush（避免连打），
            // 暴露 lastAuthFailed=true 让 UI 提示"令牌失效，请检查后重试"。
            lastAuthFailed = true;
            inFlight = false;
            consecutiveFailures += 1;
            rescheduleRetry();
            return { sent: sentCount, remaining: queue.length, rejected: [] };
          }
          // retry：停止，保留该批及后续（离线不丢）
          inFlight = false;
          // 失败一次 → 累加连续失败，触发退避重排
          consecutiveFailures += 1;
          rescheduleRetry();
          return { sent: sentCount, remaining: queue.length, rejected: [] };
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
        return { sent: sentCount, remaining: queue.length, rejected: [], error: e };
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
        var storedBatch = batch;
        if (tenantBound) {
          // 租户绑定模式：批次内固化 {tenant_id, credential_id} 快照（§7.3）。
          // 拷贝而不改写调用方对象；快照以当前绑定为准（覆盖同名客户端字段，
          // 服务端也从不信任客户端租户字段，见 §4.3）。
          storedBatch = {};
          for (var bk in batch) {
            if (Object.prototype.hasOwnProperty.call(batch, bk)) storedBatch[bk] = batch[bk];
          }
          storedBatch.tenant_id = legacyTenantId || tenantId;
          if (!legacyTenantId && credentialId) storedBatch.credential_id = credentialId;
        }
        var item = {
          clientBatchId: uuid(),
          enqueuedAt: now(),
          batch: storedBatch
        };
        if (videoOpt != null) item.videoBlobRef = videoOpt;
        if (readingsOpt && typeof readingsOpt === "object") item.readings = readingsOpt;
        queue.push(item);
        // enqueue 的 persist 失败必须抛错：通知调用方"这批没落盘"。
        // 批次保留在内存队列（不回滚）——下次 persist（enqueue/flush）会再试，
        // 一旦 storage 恢复即可落盘/补传，最大化数据保留。调用方（finishBoxFlow）
        // 捕获后保留草稿/提示用户。与 flush 内部的 persist（吞错不阻断）区分。
        var ok = persist();
        if (!ok) {
          // 通知 onChange 反映内存队列里这一批（虽未落盘），再抛错
          notify();
          throw new Error("enqueue: 持久化失败（storage 不可写/quota）");
        }
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
          var entry = { clientBatchId: d.clientBatchId, enqueuedAt: d.enqueuedAt, batch: d.batch, failedAt: d.failedAt, reason: d.reason };
          if (d.httpStatus) entry.httpStatus = d.httpStatus;
          if (d.serverDetail) entry.serverDetail = d.serverDetail;
          return entry;
        });
      },

      /* ---------- 草稿箱 / 手动重传 ----------
       * 以下方法供 H5「草稿箱 / 失败记录手动重传」页面使用，给用户提供对
       * 待传队列与死信的明细查看 + 手动重传入口。全程遵守防丢契约：
       *   - 先落盘（persist / persistDead）再移出内存；
       *   - 任一步落盘失败回滚，绝不丢数据；
       *   - 不改变现有 flush / enqueue / 死信迁移 的语义。
       */

      /* 立即重传主队列全部待传：重置退避计数 + 立即 flush。
       * 返回 flush 的 Promise（与 flush 同形状 {sent, remaining}）。 */
      retryAll: function () {
        // 用户主动触发 → 视为全新一轮尝试，清零退避用基础间隔立即重试
        consecutiveFailures = 0;
        return flush();
      },

      /* 把指定批次移到队首并立即重传（优先发该批）。幂等：找不到只调 flush。
       * 返回 flush 的 Promise。
       *
       * 并发保护：若 flush 正在进行（inFlight=true），队首 queue[0] 正是正在发送
       * 的那批（step 捕获 queue[0] 并 sendOne，期间 retry 不会改它前面）。此时若
       * 把目标 unshift 到队首，会插到正在发送的批前面 → 并发时序下即便出队已按
       * clientBatchId 精确移除（不会丢），也会打乱「队首=在途批」的不变量。故
       * inFlight 时改把目标插到队首之后（index 1），既优先于其它待发批，又不干扰
       * 正在发送的队首；inFlight=false 时维持原逻辑（移到队首）。 */
      retry: function (clientBatchId) {
        if (typeof clientBatchId === "string" && clientBatchId) {
          for (var i = 0; i < queue.length; i++) {
            if (queue[i] && queue[i].clientBatchId === clientBatchId) {
              if (i > 0) {
                // 从当前位置移出；插入位置取决于是否有 in-flight 队首要保护。
                var item = queue.splice(i, 1)[0];
                // inFlight 时 queue[0] 是正在发送的批，插到 index 1（其后）；
                // 否则无在途批，移到队首（index 0）。
                var insertAt = inFlight ? 1 : 0;
                queue.splice(insertAt, 0, item);
                persist();
                notify();
              }
              break; // clientBatchId 唯一，找到即止
            }
          }
        }
        // 重置退避：用户主动重试该批，用基础间隔
        consecutiveFailures = 0;
        return flush();
      },

      /* 把指定死信移回主队列重新走正常 flush 路径（手动重发）。
       * 防丢顺序：先 queue.push + persist 成功，再从死信移除 + persistDead；
       * 任一步失败回滚，绝不丢。成功后 notify，并触发一次 flush（若已 start）。
       * 返回 boolean：true=成功移回；false=找不到该死信或落盘失败（幂等）。 */
      requeueDead: function (clientBatchId) {
        if (typeof clientBatchId !== "string" || !clientBatchId) return false;
        var idx = -1;
        for (var i = 0; i < deadLetter.length; i++) {
          if (deadLetter[i] && deadLetter[i].clientBatchId === clientBatchId) {
            idx = i;
            break;
          }
        }
        if (idx < 0) return false; // 找不到该死信 → 幂等返回 false
        var dead = deadLetter[idx];
        // 构造回主队列的 item：保留 clientBatchId/enqueuedAt/batch/readings，
        // 去掉 failedAt/reason（这些是死信专有字段，回主队列后不再有意义）。
        var item = {
          clientBatchId: dead.clientBatchId,
          enqueuedAt: dead.enqueuedAt,
          batch: dead.batch
        };
        if (dead.readings) item.readings = dead.readings;
        // 第一步：先 push 到主队列并 persist 落盘。失败则回滚内存（绝不丢）。
        queue.push(item);
        if (!persist()) {
          // 主队列落盘失败：回滚 push，死信保持不变（仍在内存 + 已落盘）
          queue.pop();
          return false;
        }
        // 第二步：主队列已落盘 → 安全从死信移除并 persistDead 落盘。
        // persistDead 失败也不回滚主队列（主队列已成功落盘，该批现在双写：
        // 主队列有 + 死信有；restore 的去重逻辑会按死信优先过滤主队列重复——
        // 但此处死信内存已移除只是持久化未更新，为避免 reload 后死信「复活」
        // 造成重复，persistDead 失败时把死信条目放回内存，让下次 persist 再试）。
        deadLetter.splice(idx, 1);
        if (!persistDead()) {
          // 死信落盘失败：把条目放回内存死信（保持内存与「上次成功落盘的死信」
          // 一致），主队列保留该批（已落盘）。下轮 persistDead 再试。
          // 此时主队列与死信内存中都有该 clientBatchId —— restore 去重会按死信
          // 优先过滤主队列，避免重复上报；但死信持久化未更新，reload 后死信
          // 仍是旧值（含该条），主队列持久化已更新（含该批）→ restore 去重生效，
          // 该批只出现在死信不重复上报。即最坏情况下该批退回死信，绝不丢。
          deadLetter.splice(idx, 0, dead);
          return false;
        }
        notify();
        // 已 start 则立即触发一次 flush（让用户看到重传动作）
        if (started) flush();
        return true;
      },

      /* 从死信删除指定条并落盘。找不到幂等返回 false。
       * 防丢：先快照被删条目（含原位 idx），splice 后若 persistDead() 返回 false，
       * 把条目按原位 splice 回内存（回滚）并返回 false——避免「内存已删、持久化
       * 副本仍在」导致重启后记录复活。成功才 notify + 返回 true。 */
      removeDead: function (clientBatchId) {
        if (typeof clientBatchId !== "string" || !clientBatchId) return false;
        var idx = -1;
        for (var i = 0; i < deadLetter.length; i++) {
          if (deadLetter[i] && deadLetter[i].clientBatchId === clientBatchId) {
            idx = i;
            break;
          }
        }
        if (idx < 0) return false;
        var snapshotEntry = deadLetter[idx];
        var snapshotIdx = idx;
        deadLetter.splice(idx, 1);
        if (!persistDead()) {
          // 落盘失败：把条目放回原位，内存与「上次成功落盘的死信」保持一致。
          deadLetter.splice(snapshotIdx, 0, snapshotEntry);
          return false;
        }
        notify();
        return true;
      },

      /* 清空死信并落盘。返回 boolean：true=成功；false=落盘失败已回滚内存。
       * 已空时维持现状（persistDead 一次并返回其结果，不 notify）。 */
      clearDead: function () {
        if (deadLetter.length === 0) {
          // 已空也走一次 persistDead 保证存储一致（写空数组），返回其结果，不 notify（无变化）
          return persistDead();
        }
        // 先快照整个死信数组；清空后若 persistDead 失败则恢复内存（回滚）。
        var snapshot = deadLetter;
        deadLetter = [];
        if (!persistDead()) {
          deadLetter = snapshot; // 回滚内存
          return false;
        }
        notify();
        return true;
      },

      // 当前计算的下次重试间隔（测试用）
      nextInterval: nextInterval,

      // 连续失败计数（测试用）
      consecutiveFailures: function () { return consecutiveFailures; },

      // 最近一次 persist 是否成功落盘（只读状态，供 UI/测试监控）
      lastPersistOk: function () { return lastPersistOk; },

      // 最近一次 flush 是否因鉴权（401/403）失败停止（只读状态）
      lastAuthFailed: function () { return lastAuthFailed; },

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

  /* ------------------------------------------------------------------ *
   * 读取指定 outbox 键的待传批次数（UI 显示「上一工作区还有 N 条未上传」）。
   * 缺键 / 损坏 → 0（只读，不修复、不迁移——v1 队列绝不静默并入新身份）。
   * ------------------------------------------------------------------ */
  function readQueueCount(storage, key) {
    if (!storage || typeof storage.getItem !== "function") return 0;
    try {
      var raw = storage.getItem(key);
      if (!raw || typeof raw !== "string") return 0;
      var parsed = JSON.parse(raw);
      if (!parsed || !Array.isArray(parsed.queue)) return 0;
      return parsed.queue.filter(function (item) {
        return item && typeof item === "object" && item.batch && Array.isArray(item.batch.records);
      }).length;
    } catch (_) {
      return 0;
    }
  }

  return {
    buildRecord: buildRecord,
    createOutbox: createOutbox,
    createMemoryStorage: createMemoryStorage,
    readTokenFromDocument: readTokenFromDocument,
    uuid: uuid,
    dataUrlToBlob: dataUrlToBlob,
    safePhotoStem: safePhotoStem,
    readQueueCount: readQueueCount,
    DEFAULT_STORAGE_KEY: DEFAULT_STORAGE_KEY,
    OUTBOX_KEY_V2_PREFIX: OUTBOX_KEY_V2_PREFIX,
    LEGACY_DEFAULT_TENANT_ID: LEGACY_DEFAULT_TENANT_ID,
    DEFAULT_ENDPOINT: DEFAULT_ENDPOINT,
    DEFAULT_BASE_INTERVAL_MS: DEFAULT_BASE_INTERVAL_MS,
    DEFAULT_MAX_INTERVAL_MS: DEFAULT_MAX_INTERVAL_MS
  };
});
