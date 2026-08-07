/* 纯本地数据存储层 (ui/static/local-store.js) 单元测试 — node:test，零依赖。
 * 运行：node --test 'tests/h5/star-star/star.test.mjs'（或直接 node --test tests/h5/local-store.test.mjs）
 *
 * 覆盖：
 *   - 建箱默认值对齐后端（project_id/strain/mouse_no_start/mouse_no_pad/next_ordinal
 *     /record_count/qr_payload），重名 409
 *   - 列表按 updated_at 降序、recentBoxes 截断
 *   - getBox 不存在抛 {status:404, message:"箱子不存在"}
 *   - saveRecords 幂等（同 record_id 跳过）+ next_ordinal / record_count 推进
 *   - boxRecords 按 ordinal 升序、photo dataURL 存取
 *   - getRecord 跨箱查找 + 404 形状
 *   - saveRecords 遇 Quota 抛 {status:507, message:"本机存储空间不足"}
 *   - exportAll 结构 {boxes, recordsByCage, exportedAt}
 *   - 视频 IndexedDB 用注入的假 idbFactory 验证 save/get/delete
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const LS = require("../../ui/static/local-store.js");

/* ---------- 内存 localStorage stub ---------- */
function makeStorage() {
  const store = Object.create(null);
  return {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
    key: (i) => Object.keys(store)[i] ?? null,
    get length() { return Object.keys(store).length; },
    _dump: () => store,
    _raw: (k) => store[k],
  };
}

/* 可触发 Quota 的 stub：setItem 到第 n 次抛 QuotaExceededError */
function makeQuotaStorage(hitsBeforeThrow) {
  const base = makeStorage();
  let count = 0;
  return {
    getItem: base.getItem,
    key: base.key,
    get length() { return base.length; },
    setItem: (k, v) => {
      count += 1;
      if (count >= hitsBeforeThrow) {
        const e = new Error("Quota exceeded");
        e.name = "QuotaExceededError";
        throw e;
      }
      base.setItem(k, v);
    },
    removeItem: base.removeItem,
    _dump: base._dump,
  };
}

/* ---------- 内存 IndexedDB 假实现（只实现 local-store 用到的 API） ---------- */
function makeFakeIDB() {
  const stores = new Map(); // storeName -> Map<key, value>
  function reqObject(value) {
    // 请求对象：调用方挂 onsuccess/onerror，随后在微任务里触发 onsuccess
    const r = { result: value, error: null };
    r.onsuccess = null;
    r.onerror = null;
    Promise.resolve().then(() => { if (r.onsuccess) r.onsuccess({ target: r }); });
    return r;
  }
  function db(name) {
    return {
      objectStoreNames: {
        contains: (n) => stores.has(n),
      },
      createObjectStore: (n) => { if (!stores.has(n)) stores.set(n, new Map()); return null; },
      transaction: (names, mode) => {
        const storeName = names[0];
        if (!stores.has(storeName)) stores.set(storeName, new Map());
        const m = stores.get(storeName);
        const tx = {};
        tx.objectStore = () => ({
          put: (value) => { m.set(value.run_id, value); return reqObject(undefined); },
          get: (key) => { const v = m.get(key); return reqObject(v === undefined ? undefined : v); },
          delete: (key) => { m.delete(key); return reqObject(undefined); },
        });
        tx.oncomplete = null;
        tx.onerror = null;
        // 事务完成：微任务里触发
        Promise.resolve().then(() => { if (tx.oncomplete) tx.oncomplete({}); });
        return tx;
      },
      close: () => {},
    };
  }
  return {
    open: (name, version) => {
      const req = { result: null, error: null };
      setTimeout(() => {
        const d = db(name);
        if (req.onupgradeneeded) req.onupgradeneeded({ target: { result: d } });
        req.result = d;
        if (req.onsuccess) req.onsuccess({ target: { result: d } });
      }, 0);
      return req;
    },
    _stores: stores,
  };
}

/* ---------- 测试辅助 ---------- */
function makeStore(over) {
  const storage = (over && over.storage) || makeStorage();
  const opts = Object.assign({ storage: storage }, over || {});
  return LS.create(opts);
}

function baseRecord(over) {
  return Object.assign(
    {
      record_id: "rec1",
      ordinal: 1,
      weight_g: 20.5,
      recorded_at: "2026-08-07T10:00:00",
      weight_source: "manual",
    },
    over || {}
  );
}

/* ============================================================ */
/* 箱子：建箱默认值 / 409 / 列表排序                            */
/* ============================================================ */
test("createBox 默认值对齐后端（project/strain/next_ordinal/qr_payload）", () => {
  const s = makeStore();
  const box = s.createBox({ cage_id: "C57-023" });
  assert.equal(box.cage_id, "C57-023");
  assert.equal(box.project_id, "default");
  assert.equal(box.strain, "C57BL/6"); // C57 前缀 → C57BL/6
  assert.equal(box.notes, "");
  assert.equal(box.mouse_no_start, 1);
  assert.equal(box.mouse_no_pad, 2);
  assert.equal(box.next_ordinal, 1); // 初始 = mouse_no_start
  assert.equal(box.record_count, 0);
  assert.equal(box.qr_payload, JSON.stringify({ v: 1, project_id: "default", cage_id: "C57-023" }));
  assert.ok(box.created_at && box.updated_at);
});

test("createBox 显式字段 + 非 C57/BALB 品系推断为其他", () => {
  const s = makeStore();
  const box = s.createBox({ cage_id: "X-001", strain: "ICR", project_id: "p2", mouse_no_start: 5, mouse_no_pad: 3, notes: "备注" });
  assert.equal(box.project_id, "p2");
  assert.equal(box.strain, "ICR"); // 显式优先
  assert.equal(box.mouse_no_start, 5);
  assert.equal(box.mouse_no_pad, 3);
  assert.equal(box.next_ordinal, 5);
  assert.equal(box.notes, "备注");
  assert.equal(box.qr_payload, JSON.stringify({ v: 1, project_id: "p2", cage_id: "X-001" }));

  const auto = s.createBox({ cage_id: "BALB-01" });
  assert.equal(auto.strain, "BALB/c");
});

test("createBox 重名抛 {status:409, message:箱号已存在}", () => {
  const s = makeStore();
  s.createBox({ cage_id: "C57-023" });
  assert.throws(() => s.createBox({ cage_id: "C57-023" }), (err) => {
    assert.equal(err.status, 409);
    assert.equal(err.message, "箱号已存在");
    return true;
  });
});

test("listBoxes 按 updated_at 降序；recentBoxes 截断", () => {
  const s = makeStore();
  s.createBox({ cage_id: "C57-001" }); // updated 最早
  s.createBox({ cage_id: "C57-002" }); // 最晚
  s.createBox({ cage_id: "C57-003" });
  const all = s.listBoxes();
  assert.equal(all.items.length, 3);
  // 无 saveRecords 时 updated_at 相同，但三条 created/updated 都在同一毫秒 → 顺序不保证，
  // 这里只验证结构 {items} 与数量。
  assert.ok(Array.isArray(all.items));
  const recent = s.recentBoxes(2);
  assert.equal(recent.items.length, 2);
});

test("recentBoxes 默认 limit 与排序一致性（updated_at 不同）", () => {
  const s = makeStore({ now: (() => { let t = 1000; return () => (t += 1); })() });
  s.createBox({ cage_id: "A" });
  s.createBox({ cage_id: "B" });
  s.createBox({ cage_id: "C" });
  const all = s.listBoxes();
  const ts = all.items.map((b) => b.updated_at);
  const sorted = [...ts].sort().reverse();
  assert.deepEqual(ts, sorted); // 降序
});

test("getBox 存在返回 / 不存在抛 {status:404, message:箱子不存在}", () => {
  const s = makeStore();
  s.createBox({ cage_id: "C57-023" });
  assert.equal(s.getBox("C57-023").cage_id, "C57-023");
  assert.throws(() => s.getBox("NO-SUCH"), (err) => {
    assert.equal(err.status, 404);
    assert.equal(err.message, "箱子不存在");
    return true;
  });
});

/* ============================================================ */
/* 记录：saveRecords / 幂等 / next_ordinal 推进 / 排序          */
/* ============================================================ */
test("saveRecords 生成 run_id + 推进 next_ordinal/record_count", () => {
  const s = makeStore();
  s.createBox({ cage_id: "C57-023", mouse_no_start: 1 });
  const recs = [
    baseRecord({ record_id: "r1", ordinal: 1, weight_g: 20.1 }),
    baseRecord({ record_id: "r2", ordinal: 2, weight_g: 21.2 }),
    baseRecord({ record_id: "r3", ordinal: 3, weight_g: 22.3 }),
  ];
  const out = s.saveRecords("C57-023", recs, { device_id: "dev1", mode: "manual" });
  assert.ok(out.run_id.indexOf("local_") === 0);
  assert.equal(out.count, 3);

  const box = s.getBox("C57-023");
  assert.equal(box.record_count, 3);
  assert.equal(box.next_ordinal, 4); // max ordinal 3 + 1
});

test("saveRecords 幂等：同 record_id 跳过，runMeta 自带 run_id", () => {
  const s = makeStore();
  s.createBox({ cage_id: "C57-023" });
  const recs = [baseRecord({ record_id: "r1", ordinal: 1 })];
  const out1 = s.saveRecords("C57-023", recs, { run_id: "run-abc" });
  assert.equal(out1.run_id, "run-abc");
  assert.equal(out1.count, 1);
  // 再存同批 + 新一条
  const out2 = s.saveRecords("C57-023", [...recs, baseRecord({ record_id: "r2", ordinal: 2 })], { run_id: "run-abc" });
  assert.equal(out2.count, 1); // r1 跳过，只加 r2
  const box = s.getBox("C57-023");
  assert.equal(box.record_count, 2);
  assert.equal(box.next_ordinal, 3);
  // 第三次全量幂等重放 → 不新增
  const out3 = s.saveRecords("C57-023", [baseRecord({ record_id: "r1", ordinal: 1 }), baseRecord({ record_id: "r2", ordinal: 2 })], {});
  assert.equal(out3.count, 0);
  assert.equal(s.getBox("C57-023").record_count, 2);
});

test("boxRecords 按 ordinal 升序 + photo dataURL 存取 + photo_url:null", () => {
  const s = makeStore();
  s.createBox({ cage_id: "C57-023" });
  s.saveRecords("C57-023", [
    baseRecord({ record_id: "r3", ordinal: 3, weight_g: 30 }),
    baseRecord({ record_id: "r1", ordinal: 1, weight_g: 10, photo: "data:image/jpeg;base64,AAA" }),
    baseRecord({ record_id: "r2", ordinal: 2, weight_g: 20 }),
  ], {});
  const { items } = s.boxRecords("C57-023");
  assert.deepEqual(items.map((r) => r.ordinal), [1, 2, 3]);
  assert.equal(items[0].weight_g, 10);
  assert.equal(items[0].photo, "data:image/jpeg;base64,AAA"); // dataURL 保留
  assert.equal(items[0].photo_url, null); // 本地模式 photo_url 固定 null
});

test("boxRecords 无记录返回空 items", () => {
  const s = makeStore();
  s.createBox({ cage_id: "C57-023" });
  assert.deepEqual(s.boxRecords("C57-023").items, []);
});

test("getRecord 跨箱查找 + 404 形状", () => {
  const s = makeStore();
  s.createBox({ cage_id: "C57-001" });
  s.createBox({ cage_id: "BALB-002" });
  s.saveRecords("C57-001", [baseRecord({ record_id: "a1", ordinal: 1 })], {});
  s.saveRecords("BALB-002", [baseRecord({ record_id: "b1", ordinal: 1, weight_g: 99 })], {});
  const rec = s.getRecord("b1");
  assert.equal(rec.cage_id, "BALB-002");
  assert.equal(rec.weight_g, 99);
  assert.throws(() => s.getRecord("nope"), (err) => {
    assert.equal(err.status, 404);
    return true;
  });
});

test("listCageIds 返回有记录的箱号", () => {
  const s = makeStore();
  s.createBox({ cage_id: "C57-001" });
  s.createBox({ cage_id: "C57-002" });
  s.saveRecords("C57-001", [baseRecord({ record_id: "a1" })], {});
  s.saveRecords("C57-002", [baseRecord({ record_id: "b1" })], {});
  const ids = s.listCageIds();
  assert.ok(ids.includes("C57-001"));
  assert.ok(ids.includes("C57-002"));
});

test("saveRecords 遇 Quota 抛 {status:507, message:本机存储空间不足}", () => {
  const storage = makeQuotaStorage(2); // 第 2 次 setItem 抛（建箱 1 次 + 记录 1 次）
  const s = makeStore({ storage });
  s.createBox({ cage_id: "C57-023" }); // 第 1 次 setItem
  assert.throws(
    () => s.saveRecords("C57-023", [baseRecord({ record_id: "r1" })], {}),
    (err) => {
      assert.equal(err.status, 507);
      assert.equal(err.message, "本机存储空间不足");
      return true;
    }
  );
});

/* ============================================================ */
/* 导出                                                    */
/* ============================================================ */
test("exportAll 结构 {boxes, recordsByCage, exportedAt}", () => {
  const s = makeStore();
  s.createBox({ cage_id: "C57-001" });
  s.createBox({ cage_id: "BALB-002" });
  s.saveRecords("C57-001", [baseRecord({ record_id: "a1" })], {});
  s.saveRecords("BALB-002", [baseRecord({ record_id: "b1" })], {});
  const out = s.exportAll();
  assert.ok(Array.isArray(out.boxes));
  assert.equal(out.boxes.length, 2);
  assert.ok(out.recordsByCage["C57-001"] && out.recordsByCage["C57-001"].length === 1);
  assert.ok(out.recordsByCage["BALB-002"] && out.recordsByCage["BALB-002"].length === 1);
  assert.ok(typeof out.exportedAt === "string" && out.exportedAt);
});

/* ============================================================ */
/* 视频 IndexedDB（注入假 idbFactory）                        */
/* ============================================================ */
test("视频 saveVideo/getVideo/deleteVideo 走 IndexedDB（注入假实现）", async () => {
  const idb = makeFakeIDB();
  const s = makeStore({ idbFactory: idb });
  const blob = new Uint8Array([1, 2, 3]);
  await s.saveVideo("run-1", "C57-023", blob);
  const got = await s.getVideo("run-1");
  assert.deepEqual(got, blob);
  // 未找到 → null
  const missing = await s.getVideo("run-nope");
  assert.equal(missing, null);
  await s.deleteVideo("run-1");
  const after = await s.getVideo("run-1");
  assert.equal(after, null);
});

test("视频未注入 idbFactory → Promise reject", async () => {
  const s = makeStore({}); // 无 idbFactory
  await assert.rejects(s.getVideo("run-1"), /IndexedDB 不可用/);
  await assert.rejects(s.saveVideo("run-1", "C", new Uint8Array()), /IndexedDB 不可用/);
});

/* ============================================================ */
/* 模块级工具                                                    */
/* ============================================================ */
test("模块级 strainFromCage / qrPayload / makeRunId", () => {
  assert.equal(LS.strainFromCage("C57-01"), "C57BL/6");
  assert.equal(LS.strainFromCage("BALBc-01"), "BALB/c");
  assert.equal(LS.strainFromCage("X-1"), "其他");
  assert.equal(LS.qrPayload("C57-01", "p"), JSON.stringify({ v: 1, project_id: "p", cage_id: "C57-01" }));
  assert.ok(LS.makeRunId(123).indexOf("local_123_") === 0);
});
