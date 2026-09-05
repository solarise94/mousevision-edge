"""租户隔离业务契约（合同 §9-1、2、4业务侧、5、6、7、9 / §15-B1）。

这些测试刻画 B3 完成后的终态。按 B1 批次约定，它们在业务 handler
仍接在全局单例上时保持红（失败原因 = 缺租户能力），逐条红名单见
.tenant-upgrade-progress.md。

覆盖：
1. 两租户同 cage_id 各写各 next_ordinal、互不可见（B3）
2. 两租户同 record_id：详情/照片只返回本租户，跨租户猜 URL → 404（B3）
4b. parent_owner 直接访问业务数据（B3）
5. 租户 reset 只删自己目录（B3）
7. realtime session 跨租户 session_id → REST 404 / WS 4403（B3）
9. 并发同租户同箱 reserve ordinal 不重复、租户间独立（B3）
"""

from __future__ import annotations

import concurrent.futures

import pytest
from fastapi.testclient import TestClient

import tenant_fixture as tf
from tenant_fixture import LEGACY_TOKEN
from tenant_fixture import world  # noqa: F401 - pytest fixture 注册

TOKEN_HEADER = {"X-MouseVision-Token": LEGACY_TOKEN}
CAGE = "C57-900"


# ------------------------------------------------------------------ #
# 1. 同 cage_id 双租户独立序号
# ------------------------------------------------------------------ #
def test_same_cage_two_tenants_independent_ordinals(world):
    ca = world.member_client("op-a1", "op-a1", tf.OPERATOR_PW, "a1")
    cb = world.member_client("op-b1", "op-b1", tf.OPERATOR_PW, "b1")

    r = ca.post(f"/api/boxes/{CAGE}/reserve-ordinal", headers=TOKEN_HEADER)
    assert r.status_code == 200, r.text
    assert r.json()["requested_ordinal"] == 1, "租户 A 同箱首号必须是 1"

    r = cb.post(f"/api/boxes/{CAGE}/reserve-ordinal", headers=TOKEN_HEADER)
    assert r.status_code == 200, r.text
    assert r.json()["requested_ordinal"] == 1, (
        "租户 B 同箱序号必须从 1 重新开始（各写各的 next_ordinal），"
        "而不是接入全局单例计数器"
    )

    ca.post(f"/api/boxes/{CAGE}/reserve-ordinal", headers=TOKEN_HEADER)
    r = ca.get(f"/api/boxes/{CAGE}", headers=TOKEN_HEADER)
    assert r.status_code == 200
    assert r.json()["next_ordinal"] == 3, "A 租户自己数到 3"

    r = cb.get(f"/api/boxes/{CAGE}", headers=TOKEN_HEADER)
    assert r.status_code == 200
    assert r.json()["next_ordinal"] == 2, "B 租户的计数器不得被 A 推着走（互不可见）"


# ------------------------------------------------------------------ #
# 2. 同 record_id 双租户；跨租户猜 URL → 404
# ------------------------------------------------------------------ #
def test_same_record_id_two_tenants_detail_and_photo(world):
    rid = "rec-shared-0001"
    world.seed_tenant_record("a1", rid, weight=11.11, photo=b"photo-of-tenant-A")
    world.seed_tenant_record("b1", rid, weight=22.22, photo=b"photo-of-tenant-B")
    # 预迁移数据同时存在于全局根（现网形态）：匿名/无上下文读会拿到它
    world.seed_global_record(rid, weight=33.33, photo=b"photo-of-GLOBAL-root")

    ca = world.member_client("op-a1", "op-a1", tf.OPERATOR_PW, "a1")
    cb = world.member_client("op-b1", "op-b1", tf.OPERATOR_PW, "b1")

    r = ca.get(f"/api/records/{rid}", headers=TOKEN_HEADER)
    assert r.status_code == 200, (
        f"A 租户必须能在自己目录里读到本租户记录（红到 B3）: {r.status_code}"
    )
    assert r.json()["weight"] == 11.11, "详情必须来自 A 租户目录"

    r = cb.get(f"/api/records/{rid}", headers=TOKEN_HEADER)
    assert r.status_code == 200
    assert r.json()["weight"] == 22.22, "B 租户读到的必须是 B 目录里的记录"

    r = ca.get(f"/api/records/{rid}/photo", params={"size": "full"}, headers=TOKEN_HEADER)
    assert r.status_code == 200
    assert r.content == b"photo-of-tenant-A", "照片必须返回 A 租户自己的字节"

    # 匿名读业务记录必须被拒（终态 401/404；B4 收口匿名）
    anon = TestClient(world.app)
    r = anon.get(f"/api/records/{rid}")
    assert r.status_code in (401, 404), (
        f"匿名不得读到业务记录（现返回 {r.status_code}，红到 B3/B4）"
    )


def test_cross_tenant_record_guess_returns_404(world):
    """A 租户上下文里访问只存在于 B 租户目录的记录 → 404（统一 404，不 403）。"""
    rid_b = "rec-only-in-b"
    world.seed_tenant_record("b1", rid_b, weight=9.99, photo=b"photo-B-only")
    world.seed_global_record(rid_b, weight=8.88, photo=b"photo-LEAK")  # 预迁移泄露形态

    ca = world.member_client("op-a1", "op-a1", tf.OPERATOR_PW, "a1")
    r = ca.get(f"/api/records/{rid_b}", headers=TOKEN_HEADER)
    assert r.status_code == 404, (
        f"A 上下文不得读到 B 租户/全局根的记录（现返回 {r.status_code}，红到 B3）"
    )
    r = ca.get(f"/api/records/{rid_b}/photo", headers=TOKEN_HEADER)
    assert r.status_code == 404, "跨租户照片猜 URL 必须 404（红到 B3）"


# ------------------------------------------------------------------ #
# 4b. parent_owner 直接访问业务数据（业务侧；红到 B3）
# ------------------------------------------------------------------ #
def test_parent_owner_cannot_read_business_records_directly(world):
    """parent 未切换（或只读查看）时，普通业务 API 不得返回业务数据。"""
    world.seed_global_record("rec-parent-probe", weight=77.7, photo=b"leak")
    parent = world.parent_client()
    r = parent.get("/api/records/rec-parent-probe")
    assert r.status_code in (401, 403, 404), (
        f"parent_owner 无业务上下文时不得经普通业务 API 读记录（现 {r.status_code}，红到 B3）"
    )


# ------------------------------------------------------------------ #
# 5. 租户 reset 只删自己目录
# ------------------------------------------------------------------ #
def test_tenant_reset_only_deletes_own_directory(world):
    world.seed_tenant_record("a1", "rec-reset-a", weight=1.0, photo=b"a")
    world.seed_tenant_record("b1", "rec-reset-b", weight=2.0, photo=b"b")
    control_db = world.control_db()
    assert control_db.exists()

    admin = world.member_client("admin-a1", "admin-a1", tf.TENANT_ADMIN_PW, "a1")
    r = admin.post(f"/api/tenants/{world.tid('a1')}/reset")
    assert r.status_code == 200, (
        f"租户级 reset 端点缺失（现 {r.status_code}，红到 B3）: {r.text}"
    )

    a_runs = list(world.tenant_dir("a1").glob("run_*"))
    assert not a_runs, "reset 后本租户 run_* 必须清空"
    b_runs = list(world.tenant_dir("b1").glob("run_*"))
    assert b_runs, "其他租户目录必须原样保留"
    assert control_db.exists(), "control/ 与控制面数据必须原样保留"
    assert world.tenant_dir("b1").exists()

    # 租户管理员绝不能触发旧的全局清盘
    r = admin.post("/api/reset")
    assert r.status_code == 403, "tenant_admin 不得拥有平台级 /api/reset 权限"


# ------------------------------------------------------------------ #
# 7. realtime session 跨租户
# ------------------------------------------------------------------ #
def _create_realtime_session(client) -> str:
    r = client.post(
        "/api/realtime/session", json={"cage_id": CAGE}, headers=TOKEN_HEADER
    )
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def test_realtime_cross_tenant_session_status_404(world):
    a_op = world.member_client("op-a1", "op-a1", tf.OPERATOR_PW, "a1")
    b_op = world.member_client("op-b1", "op-b1", tf.OPERATOR_PW, "b1")
    sid = _create_realtime_session(a_op)

    r = b_op.get(f"/api/realtime/session/{sid}/status", headers=TOKEN_HEADER)
    assert r.status_code == 404, (
        f"跨租户 session_id 必须按不存在处理（现 {r.status_code}，红到 B3）"
    )
    # 本租户仍可见
    r = a_op.get(f"/api/realtime/session/{sid}/status", headers=TOKEN_HEADER)
    assert r.status_code == 200


def test_realtime_cross_tenant_ws_4403(world):
    from starlette.websockets import WebSocketDisconnect

    a_op = world.member_client("op-a1", "op-a1", tf.OPERATOR_PW, "a1")
    b_op = world.member_client("op-b1", "op-b1", tf.OPERATOR_PW, "b1")
    sid = _create_realtime_session(a_op)

    b_cookie = b_op.cookies.get("mv_session")
    with pytest.raises(WebSocketDisconnect) as exc:
        with b_op.websocket_connect(
            f"/api/realtime/ws?session_id={sid}&token={LEGACY_TOKEN}",
            headers={"cookie": f"mv_session={b_cookie}"},
        ):
            pass  # 握手若成功即违反契约
    assert exc.value.code == 4403, (
        f"跨租户 WS 必须以 4403 关闭（实际 {exc.value.code!r}，红到 B3）"
    )


def test_realtime_cross_tenant_finish_404_and_owner_can_still_finish(world):
    """Review B1 修复：跨租户 finish 不得 finalize 他租户会话。

    旧缺陷：finish 的内联租户检查在 mismatch 分支不 pop 但 session 引用残留
    → 租户 A 可对租户 B 的会话 finish（200 + 对方称重数据，且对方会话被终止）。
    修复后：mismatch → 404、B 的会话保留在原租户（B 可再次 finish 成功）、
    同租户 finish 仍 200 且数据只落 B 租户目录。
    """
    a_op = world.member_client("op-a1", "op-a1", tf.OPERATOR_PW, "a1")
    b_op = world.member_client("op-b1", "op-b1", tf.OPERATOR_PW, "b1")
    sid = _create_realtime_session(b_op)  # b_op 激活 b1 → 会话固化 b1

    r = a_op.post(f"/api/realtime/session/{sid}/finish", headers=TOKEN_HEADER)
    assert r.status_code == 404, (
        f"跨租户 finish 必须按不存在处理（实际 {r.status_code}，review B1）"
    )

    # B 的会话必须仍在原租户（未被 A pop/finalize）——可读状态、可正常 finish
    r = b_op.get(f"/api/realtime/session/{sid}/status", headers=TOKEN_HEADER)
    assert r.status_code == 200, "跨租户 finish 不得终止 B 的会话"

    r = b_op.post(f"/api/realtime/session/{sid}/finish", headers=TOKEN_HEADER)
    assert r.status_code == 200, f"同租户 finish 必须仍成功：{r.text}"

    # 落盘只在 B 租户目录；A 租户目录不得出现该会话产物
    assert not list(world.tenant_dir("a1").glob("run_*")), (
        "A 租户目录不得出现 B 会话的 run"
    )
    assert list(world.tenant_dir("b1").glob("run_*")), "finish 必须落 B 租户目录"


# ------------------------------------------------------------------ #
# 9. 并发同租户同箱 reserve ordinal 不重复、租户间独立
# ------------------------------------------------------------------ #
def test_concurrent_reserve_same_tenant_no_duplicates(world):
    a_op = world.member_client("op-a1", "op-a1", tf.OPERATOR_PW, "a1")
    b_op = world.member_client("op-b1", "op-b1", tf.OPERATOR_PW, "b1")
    a_cookie = a_op.cookies.get("mv_session")
    b_cookie = b_op.cookies.get("mv_session")

    def reserve(client_cookie: str, cage: str) -> int:
        c = TestClient(world.app)
        r = c.post(
            f"/api/boxes/{cage}/reserve-ordinal",
            headers=TOKEN_HEADER,
            cookies={"mv_session": client_cookie},
        )
        assert r.status_code == 200, r.text
        return int(r.json()["requested_ordinal"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        a_ordinals = list(pool.map(lambda _: reserve(a_cookie, "C57-CONC"), range(24)))
        b_ordinals = list(pool.map(lambda _: reserve(b_cookie, "C57-CONC"), range(24)))

    assert sorted(a_ordinals) == list(range(1, 25)), (
        f"A 租户 24 次并发预约必须恰好拿到 1..24 且不重复，实际 {sorted(a_ordinals)}"
    )
    assert sorted(b_ordinals) == list(range(1, 25)), (
        f"B 租户必须独立从 1..24 分配（租户间计数器分离），实际 {sorted(b_ordinals)}"
    )
    assert len(set(a_ordinals)) == 24 and len(set(b_ordinals)) == 24
