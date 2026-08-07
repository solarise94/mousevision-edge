/* 纯本地数据存储层 (LocalStore) — 公众版「纯本地数据管理」的数据后端。
 *
 * 把服务器 API（mobile.js 里 api 对象消费的那些端点）的读/写全部搬到本机：
 *   - 箱子（box）元数据 → localStorage `mv.localBoxes.v1`（map: cageId → box）
 *   - 称重记录（record）→ localStorage `mv.localRecords.v1.<cageId>`（数组）
 *   - 视频证据 → IndexedDB `mv-local-media` / store `videos`（key=run_id）
 *
 * 接口形状刻意对齐服务器返回：
 *   createBox  <-> POST /api/boxes
 *   listBoxes  <-> GET  /api/boxes
 *   recentBoxes<-> GET  /api/boxes/recent
 *   getBox     <-> GET  /api/boxes/{cage}
 *   boxRecords <-> GET  /api/boxes/{cage}/records
 *   getRecord  <-> GET  /api/records/{id}
 * 这样 UI 层从线上切到本地时几乎不用改：同一套字段名、同一套 {items:[...]}
 * 容器、同一套错误形状 {status, message}（mobile.js 靠 err.status 分支）。
 *
 * 同步/异步划分：
 *   - localStorage 部分（箱/记录/导出）全部同步，工厂立即返回完整 store；
 *   - IndexedDB 部分（视频）是 Promise 风格，三个方法都返回 Promise。
 *   测试时把 IndexedDB 做成注入式（opts.idbFactory），Node 下传内存假实现。
 *
 * UMD：浏览器挂 window.LocalStore；node 测试 require 该模块。
 * 风格参照同目录 weigh-engine.js / report-client.js / scale-bridge.js。
 */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (root && typeof root === "object") {
    root.LocalStore = api;
  }
})(typeof window !== "undefined" ? window : this, function () {
  "use strict";

  var BOXES_KEY = "mv.localBoxes.v1";          // {v:1, map:{cageId:box}}
  var RECORDS_PREFIX = "mv.localRecords.v1.";  // .<cageId> -> [record]
  var MEDIA_DB = "mv-local-media";             // IndexedDB 库名
  var MEDIA_STORE = "videos";                  // 视频 store，keyPath=run_id

  /* 品系推断规则，与后端 ui/boxes.py _STRAIN_RULES 一致 */
  var STRAIN_RULES = [["C57", "C57BL/6"], ["BALB", "BALB/c"]];

  /* 当前本地时间 ISO（秒级，形如 2026-08-07T12:30:00，对齐后端 timespec="seconds"） */
  function nowISO(nowMs) {
    var d = nowMs != null ? new Date(nowMs) : new Date();
    function p(n) { return (n < 10 ? "0" : "") + n; }
    return (
      d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
      "T" + p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds())
    );
  }

  /* 品系推断，与后端 strain_from_cage 一致 */
  function strainFromCage(cageId) {
    var upper = String(cageId == null ? "" : cageId).toUpperCase();
    for (var i = 0; i < STRAIN_RULES.length; i++) {
      if (upper.indexOf(STRAIN_RULES[i][0]) === 0) return STRAIN_RULES[i][1];
    }
    return "其他";
  }

  /* QR payload：{v:1, project_id, cage_id} 紧凑 JSON，与后端 qr_payload() 一致 */
  function qrPayload(cageId, projectId) {
    return JSON.stringify({ v: 1, project_id: projectId == null ? "default" : projectId, cage_id: cageId });
  }

  /* 生成本地 run_id：local_<ts>_<rand>（时间戳 + 随机段，唯一且可追溯） */
  function makeRunId(ts) {
    var rand = Math.random().toString(36).slice(2, 8);
    return "local_" + (ts != null ? ts : Date.now()) + "_" + rand;
  }

  /* ---------- localStorage 读写辅助（带 Quota 守卫） ---------- */

  /* 读取箱子 map；损坏/空 → {} */
  function readBoxMap(storage) {
    try {
      var raw = storage.getItem(BOXES_KEY);
      var parsed = raw ? JSON.parse(raw) : null;
      if (parsed && typeof parsed === "object" && parsed.map && typeof parsed.map === "object") {
        return parsed.map;
      }
    } catch (_) {}
    return {};
  }

  function writeBoxMap(storage, map) {
    storage.setItem(BOXES_KEY, JSON.stringify({ v: 1, map: map }));
  }

  /* 带 Quota 守卫的写入：存储满抛 {status:507}（对齐保存记录时的 507 语义） */
  function guardedSet(storage, key, value) {
    try {
      storage.setItem(key, value);
    } catch (err) {
      if (err && (err.name === "QuotaExceededError" || err.code === 22 || err.code === 1014)) {
        var qe = new Error("本机存储空间不足");
        qe.status = 507;
        throw qe;
      }
      throw err;
    }
  }

  /* 读取某箱记录数组；无键 → [] */
  function readRecords(storage, cageId) {
    try {
      var raw = storage.getItem(RECORDS_PREFIX + cageId);
      var arr = raw ? JSON.parse(raw) : null;
      return Array.isArray(arr) ? arr : [];
    } catch (_) {
      return [];
    }
  }

  /* ---------- 主工厂 ---------- */

  function create(opts) {
    opts = opts || {};
    // 依赖注入：storage / idbFactory / now（毫秒时间戳函数）。Node 测试传内存 stub。
    var storage = opts.storage || (typeof localStorage !== "undefined" ? localStorage : null);
    var idbFactory = opts.idbFactory; // IndexedDB 实现；未注入时视频方法会 reject
    var nowMs = opts.now || function () { return Date.now(); };

    /* ---------- 箱子 ---------- */

    function createBox(input) {
      input = input || {};
      var cage = String(input.cage_id == null ? "" : input.cage_id).trim();
      if (!cage) {
        var e0 = new Error("箱号不能为空");
        e0.status = 400;
        throw e0;
      }
      var map = readBoxMap(storage);
      if (map.hasOwnProperty(cage)) {
        var dup = new Error("箱号已存在");
        dup.status = 409;
        throw dup;
      }
      var ts = nowISO(nowMs());
      var project = input.project_id != null && String(input.project_id) !== ""
        ? String(input.project_id) : "default";
      var start = Number.isFinite(+input.mouse_no_start) && +input.mouse_no_start >= 1
        ? Math.floor(+input.mouse_no_start) : 1;
      var pad = Number.isFinite(+input.mouse_no_pad) && +input.mouse_no_pad >= 1
        ? Math.floor(+input.mouse_no_pad) : 2;
      var box = {
        cage_id: cage,
        project_id: project,
        strain: input.strain != null && String(input.strain) !== "" ? String(input.strain) : strainFromCage(cage),
        notes: input.notes != null ? String(input.notes) : "",
        mouse_no_start: start,
        mouse_no_pad: pad,
        next_ordinal: start,
        created_at: ts,
        updated_at: ts,
        record_count: 0,
        qr_payload: qrPayload(cage, project),
      };
      map[cage] = box;
      guardedSet(storage, BOXES_KEY, JSON.stringify({ v: 1, map: map }));
      return box;
    }

    /* 所有箱子按 updated_at 降序 */
    function listBoxes() {
      var map = readBoxMap(storage);
      var items = [];
      for (var k in map) if (map.hasOwnProperty(k)) items.push(map[k]);
      items.sort(function (a, b) {
        return String(b.updated_at).localeCompare(String(a.updated_at));
      });
      return { items: items };
    }

    function recentBoxes(limit) {
      var n = Number.isFinite(+limit) && +limit >= 1 ? Math.floor(+limit) : 6;
      var data = listBoxes();
      return { items: data.items.slice(0, n) };
    }

    function getBox(cage) {
      var map = readBoxMap(storage);
      var box = map[String(cage)];
      if (!box) {
        var notFound = new Error("箱子不存在");
        notFound.status = 404;
        throw notFound;
      }
      return box;
    }

    /* ---------- 记录 ---------- */

    /* 保存一批记录（幂等：同 record_id 跳过）。
     * records 元素: {record_id, ordinal, weight_g, recorded_at, weight_source, photo?}
     * runMeta: {run_id?, device_id?, mode?}；无 run_id 则生成 local_<ts>_<rand>。
     * 同时推进箱子 record_count / next_ordinal / updated_at。 */
    function saveRecords(cageId, records, runMeta) {
      records = records || [];
      runMeta = runMeta || {};
      var cage = String(cageId == null ? "" : cageId);
      var runId = runMeta.run_id || makeRunId(nowMs());
      var existing = readRecords(storage, cage);
      var seen = {};
      for (var i = 0; i < existing.length; i++) {
        if (existing[i] && existing[i].record_id != null) seen[String(existing[i].record_id)] = true;
      }
      var added = [];
      var maxOrdinal = -Infinity;
      for (var j = 0; j < records.length; j++) {
        var r = records[j] || {};
        if (r.record_id == null) continue;
        var idKey = String(r.record_id);
        if (seen[idKey]) continue; // 幂等：同 id 跳过
        seen[idKey] = true;
        // 本地模式下写 photo_url: null，photo 以 dataURL 存 record.photo（UI 层读取）
        var stored = {
          record_id: idKey,
          ordinal: r.ordinal,
          weight_g: r.weight_g,
          recorded_at: r.recorded_at,
          weight_source: r.weight_source,
          photo: r.photo || null,
          photo_url: null,
          cage_id: cage,
          run_id: runId,
          created_at: nowISO(nowMs()),
        };
        if (Number.isFinite(+r.ordinal)) maxOrdinal = Math.max(maxOrdinal, +r.ordinal);
        added.push(stored);
      }
      var nextList = existing.concat(added);
      guardedSet(storage, RECORDS_PREFIX + cage, JSON.stringify(nextList));
      // 推进箱子
      var map = readBoxMap(storage);
      if (map.hasOwnProperty(cage)) {
        var box = map[cage];
        box.record_count = nextList.length;
        if (Number.isFinite(maxOrdinal)) {
          var want = maxOrdinal + 1;
          if (want > box.next_ordinal) box.next_ordinal = want;
        }
        box.updated_at = nowISO(nowMs());
        map[cage] = box;
        guardedSet(storage, BOXES_KEY, JSON.stringify({ v: 1, map: map }));
      }
      return { run_id: runId, count: added.length };
    }

    /* 某箱记录按 ordinal 升序 */
    function boxRecords(cageId) {
      var items = readRecords(storage, String(cageId == null ? "" : cageId));
      items.sort(function (a, b) { return (+a.ordinal || 0) - (+b.ordinal || 0); });
      return { items: items };
    }

    /* 跨箱查单条记录 */
    function getRecord(recordId) {
      var want = String(recordId == null ? "" : recordId);
      var keys = listCageIds();
      for (var i = 0; i < keys.length; i++) {
        var arr = readRecords(storage, keys[i]);
        for (var j = 0; j < arr.length; j++) {
          if (arr[j] && String(arr[j].record_id) === want) return arr[j];
        }
      }
      var notFound = new Error("记录不存在");
      notFound.status = 404;
      throw notFound;
    }

    /* 全部有记录的箱号（含空数组箱也列出，供 UI 建箱入口用） */
    function listCageIds() {
      var ids = [];
      if (!storage) return ids;
      // 以 key 前缀遍历：无 length 的 storage stub 无法枚举，退化为遍历盒子 map + 记录 key 两路
      var keys = null;
      if (typeof storage.key === "function" && typeof storage.length === "number") {
        keys = [];
        for (var i = 0; i < storage.length; i++) {
          var k = storage.key(i);
          if (k && k.indexOf(RECORDS_PREFIX) === 0) keys.push(k);
        }
      }
      if (!keys) {
        // 内存 stub：注入的 storage 没有 length/key 时，用 map 里已登记的箱子兜底
        var map = readBoxMap(storage);
        for (var c in map) if (map.hasOwnProperty(c)) ids.push(c);
        return ids;
      }
      for (var j = 0; j < keys.length; j++) {
        ids.push(keys[j].slice(RECORDS_PREFIX.length));
      }
      return ids;
    }

    /* ---------- 视频（IndexedDB，Promise 风格） ---------- */

    function openMediaDB() {
      if (!idbFactory || typeof idbFactory.open !== "function") {
        return Promise.reject(new Error("IndexedDB 不可用"));
      }
      return new Promise(function (resolve, reject) {
        var req = idbFactory.open(MEDIA_DB, 1);
        req.onupgradeneeded = function (ev) {
          var db = ev.target.result;
          if (!db.objectStoreNames.contains(MEDIA_STORE)) {
            db.createObjectStore(MEDIA_STORE, { keyPath: "run_id" });
          }
        };
        req.onsuccess = function (ev) { resolve(ev.target.result); };
        req.onerror = function (ev) { reject(ev.target.error || new Error("IndexedDB 打开失败")); };
      });
    }

    function saveVideo(runId, cageId, blob) {
      return openMediaDB().then(function (db) {
        return new Promise(function (resolve, reject) {
          var tx = db.transaction([MEDIA_STORE], "readwrite");
          var store = tx.objectStore(MEDIA_STORE);
          store.put({ run_id: runId, cage_id: cageId, created_at: nowISO(nowMs()), blob: blob });
          tx.oncomplete = function () { db.close(); resolve(); };
          tx.onerror = function (ev) {
            db.close();
            reject(ev.target.error || new Error("保存视频失败"));
          };
        });
      });
    }

    function getVideo(runId) {
      return openMediaDB().then(function (db) {
        return new Promise(function (resolve, reject) {
          var tx = db.transaction([MEDIA_STORE], "readonly");
          var store = tx.objectStore(MEDIA_STORE);
          var req = store.get(runId);
          req.onsuccess = function () {
            var rec = req.result;
            db.close();
            resolve(rec && rec.blob != null ? rec.blob : null);
          };
          req.onerror = function (ev) {
            db.close();
            reject(ev.target.error || new Error("读取视频失败"));
          };
        });
      });
    }

    function deleteVideo(runId) {
      return openMediaDB().then(function (db) {
        return new Promise(function (resolve, reject) {
          var tx = db.transaction([MEDIA_STORE], "readwrite");
          var store = tx.objectStore(MEDIA_STORE);
          store.delete(runId);
          tx.oncomplete = function () { db.close(); resolve(); };
          tx.onerror = function (ev) {
            db.close();
            reject(ev.target.error || new Error("删除视频失败"));
          };
        });
      });
    }

    /* ---------- 导出 ---------- */

    function exportAll() {
      var boxes = listBoxes().items;
      var recordsByCage = {};
      var keys = listCageIds();
      for (var i = 0; i < keys.length; i++) {
        recordsByCage[keys[i]] = readRecords(storage, keys[i]);
      }
      return {
        boxes: boxes,
        recordsByCage: recordsByCage,
        exportedAt: nowISO(nowMs()),
      };
    }

    return {
      // 箱子（同步）
      createBox: createBox,
      listBoxes: listBoxes,
      recentBoxes: recentBoxes,
      getBox: getBox,
      // 记录（同步）
      saveRecords: saveRecords,
      boxRecords: boxRecords,
      getRecord: getRecord,
      listCageIds: listCageIds,
      // 视频（Promise / IndexedDB）
      saveVideo: saveVideo,
      getVideo: getVideo,
      deleteVideo: deleteVideo,
      // 导出（同步）
      exportAll: exportAll,
      // 常量
      BOXES_KEY: BOXES_KEY,
      RECORDS_PREFIX: RECORDS_PREFIX,
    };
  }

  return {
    create: create,
    makeRunId: makeRunId,
    strainFromCage: strainFromCage,
    qrPayload: qrPayload,
    BOXES_KEY: BOXES_KEY,
    RECORDS_PREFIX: RECORDS_PREFIX,
    MEDIA_DB: MEDIA_DB,
    MEDIA_STORE: MEDIA_STORE,
  };
});
