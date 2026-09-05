"""E2E（G6）种子 + 启动脚本——只由 playwright.config.js 的 webServer 调用。

流程：
1. 一次性临时输出目录由 playwright 配置注入（MOUSEVISION_OUTPUT_DIR）。
2. 直接经 ``ui.app`` 的 ControlStore / TenantStoreFactory 种子拓扑（§15-B6）：
   - account A（owner=parent-a）：租户 Workspace A1 / Workspace A2
   - account B（无 owner）：租户 Workspace B1
   - op-a1（operator @ A1）
   - 各租户 1-2 条记录（复用 persist_report_records 唯一落盘核心，照片走
     占位兜底——"照片可缺"）
3. 单 worker uvicorn 起服务（§13.3 约束不变）。

本脚本**只服务测试**：所有数据落在临时目录，绝不触碰生产 output/。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PORT = int(os.environ.get("MV_E2E_PORT", "8931"))

# 种子身份（测试夹具值，非生产凭证）
PARENT_PW = "e2e-parent-password"
OPERATOR_PW = "e2e-operator-password"

TENANTS: dict[str, str] = {}  # slug -> tenant_id（写回环境变量供 spec 断言用）


def seed() -> None:
    import ui.app as app_mod  # noqa: PLC0415 - 延迟导入，先让 env 就位

    control = app_mod.control_store
    factory = app_mod.tenant_factory

    # account + parent_owner
    owner = control.create_user("parent-a", PARENT_PW, display_name="主账号 A")
    account_a = control.create_account("Lab A", owner_user_id=owner["id"])
    account_b = control.create_account("Lab B")

    # tenants
    for slug, account_id, name in (
        ("a1", account_a["id"], "Workspace A1"),
        ("a2", account_a["id"], "Workspace A2"),
        ("b1", account_b["id"], "Workspace B1"),
    ):
        tenant = control.create_tenant(account_id, name, slug)
        TENANTS[slug] = str(tenant["id"])

    # 子账号（operator @ A1；单租户 → 登录自动激活）
    op_a1 = control.create_user("op-a1", OPERATOR_PW, display_name="实验员 A1")
    control.add_membership(op_a1["id"], TENANTS["a1"], "operator")

    # 各租户少量记录（经 persist 唯一落盘核心；照片缺 → 占位兜底）
    from ui.report_api import persist_report_records  # noqa: PLC0415

    async def _report(slug: str, cage: str, records: list[dict]) -> None:
        stores = factory.stores(TENANTS[slug])
        resp = await persist_report_records(
            output_root=stores.output_root,
            registry=stores.registry,
            upload_queue=stores.upload_queue,
            boxes_store=stores.box_registry,
            cage=cage,
            project="default",
            device="e2e-seed",
            wsrc="device_report",
            strain=None,
            records=json.dumps(records),
            video=None,
            readings=None,
            photos=[],
        )
        assert resp.status_code == 201, f"seed report failed: {resp.body!r}"

    asyncio.run(_report("a1", "C57-101", [{"record_id": "e2e-a1-1", "ordinal": 1, "weight_g": 11.11}]))
    asyncio.run(_report("a1", "C57-101", [{"record_id": "e2e-a1-2", "ordinal": 2, "weight_g": 12.5}]))
    asyncio.run(_report("a2", "C57-202", [{"record_id": "e2e-a2-1", "ordinal": 1, "weight_g": 22.22}]))
    asyncio.run(_report("b1", "C57-301", [{"record_id": "e2e-b1-1", "ordinal": 1, "weight_g": 33.33}]))

    print(
        "[e2e-seed] "
        f"a1={TENANTS['a1']} a2={TENANTS['a2']} b1={TENANTS['b1']} "
        f"output={os.environ.get('MOUSEVISION_OUTPUT_DIR')}",
        flush=True,
    )


def main() -> None:
    seed()
    import uvicorn  # noqa: PLC0415

    import ui.app as app_mod  # noqa: PLC0415

    uvicorn.run(
        app_mod.app,
        host="127.0.0.1",
        port=PORT,
        log_level="warning",
        workers=1,  # 单 worker 约束（§13.3）
    )


if __name__ == "__main__":
    main()
