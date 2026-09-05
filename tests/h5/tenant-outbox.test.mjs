/* 租户 outbox 契约（合同 §7 / §14.1 / §15-B1 第 6 项，占位：红到 B5）。
 *
 * 运行：node --test tests/h5/tenant-outbox.test.mjs
 *
 * 终态契约（B5 实现 report-client.js v2 后转绿）：
 *   1. 存储键按租户分离：mv.reportOutbox.v2.<tenant_id>；
 *   2. 每个批次快照携带 {tenant_id, credential_id}；
 *   3. flush 前校验当前凭证的 tenant_id 与批次快照一致，不一致 → 拒绝发送、
 *      批次保留原队列（防止换账号后把旧草稿传到错误工作区）；
 *   4. 旧 v1 键 + 共享令牌只允许声明 legacy-default 租户。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const RC = require("../../ui/static/report-client.js");

/* 与 report-client.test.mjs 相同的内存 storage / fake fetch 辅助 */
function makeStorage() {
  const store = Object.create(null);
  return {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
    _dump: () => store,
  };
}

function makeFakeFetch() {
  const calls = [];
  const fn = (endpoint, init) => {
    calls.push({ endpoint, init });
    return Promise.resolve({ status: 200, _body: { ok: true, run_id: "r1", count: 1, record_ids: [] } });
  };
  fn.calls = calls;
  return fn;
}

const TENANT_A = "11111111-1111-4111-8111-111111111111";
const TENANT_B = "22222222-2222-4222-8222-222222222222";

function aRecord(ordinal, grams) {
  return RC.buildRecord({ ordinal, weight_g: grams });
}

/* ------------------------------------------------------------------ *
 * 1. 存储键按租户分离（红到 B5：v2 键尚未实现）
 * ------------------------------------------------------------------ */
test("outbox v2：存储键为 mv.reportOutbox.v2.<tenant_id>", async () => {
  const storage = makeStorage();
  const ob = RC.createOutbox({
    storage,
    fetchFn: makeFakeFetch(),
    tenantId: TENANT_A,
    credentialId: "cred-a1",
  });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 25.3)] });
  const raw = storage.getItem(`mv.reportOutbox.v2.${TENANT_A}`);
  assert.ok(typeof raw === "string", `必须写入 v2 租户键，实际 storage=${JSON.stringify(storage._dump())}`);
  assert.equal(storage.getItem(RC.DEFAULT_STORAGE_KEY), null, "租户模式不得回落到 v1 全局键");
});

/* ------------------------------------------------------------------ *
 * 2. 批次快照携带 tenant/credential（红到 B5）
 * ------------------------------------------------------------------ */
test("批次快照携带 tenant_id 与 credential_id", () => {
  const storage = makeStorage();
  const ob = RC.createOutbox({
    storage,
    fetchFn: makeFakeFetch(),
    tenantId: TENANT_A,
    credentialId: "cred-a1",
  });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 25.3)] });
  const batch = ob.list()[0];
  assert.equal(batch.batch.tenant_id, TENANT_A, "批次内必须固化 tenant_id");
  assert.equal(batch.batch.credential_id, "cred-a1", "批次内必须固化 credential_id");
});

/* ------------------------------------------------------------------ *
 * 3. 换凭证 flush 拒绝：跨租户凭证不得发送旧租户队列（红到 B5）
 * ------------------------------------------------------------------ */
test("flush 前凭证租户与批次快照不一致 → 拒绝发送并保留队列", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch();
  const ob = RC.createOutbox({
    storage,
    fetchFn,
    tenantId: TENANT_A,
    credentialId: "cred-a1",
  });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 25.3)] });

  // 同一存储键上，客户端被重新绑定到 B 租户的新凭证
  const obWithForeignCredential = RC.createOutbox({
    storage,
    fetchFn,
    tenantId: TENANT_A,
    credentialId: "cred-b1",
    boundTenantId: TENANT_B,
  });
  const res = await obWithForeignCredential.flush();
  assert.equal(fetchFn.calls.length, 0, "租户不匹配时绝不允许发起网络发送");
  assert.equal(obWithForeignCredential.pending(), 1, "被拒批次必须保留在原队列");
  assert.equal(res.sent, 0);
  assert.ok(res.rejected && res.rejected.length === 1, "结果必须标明被拒绝的批次");
});

/* ------------------------------------------------------------------ *
 * 4. v1 键 + 共享令牌只进 legacy-default（红到 B5）
 * ------------------------------------------------------------------ */
test("legacy 模式：v1 键队列只能声明 legacy-default 租户", async () => {
  const storage = makeStorage();
  const fetchFn = makeFakeFetch();
  const ob = RC.createOutbox({ storage, fetchFn, legacyDefaultTenantId: RC.LEGACY_DEFAULT_TENANT_ID });
  ob.enqueue({ cage_id: "C1", records: [aRecord(1, 25.3)] });
  const batch = ob.list()[0];
  assert.equal(batch.batch.tenant_id, RC.LEGACY_DEFAULT_TENANT_ID, "v1 批次必须固定 legacy-default 租户");
  const res = await ob.flush();
  assert.equal(res.sent, 1);
  const body = JSON.parse(fetchFn.calls[0].init.body);
  assert.equal(body.tenant_id, RC.LEGACY_DEFAULT_TENANT_ID, "发送载荷必须携带 legacy-default 租户标识");
});
