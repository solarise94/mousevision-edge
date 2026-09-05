"""API 路由分类注册表（合同 §14.3：路由必须有机器可检查的分类）。

每个 ``/api/*`` 路由 (method, path-template) 必须登记分类；未登记 →
tests/test_tenant_route_policy.py 失败；注册表出现幽灵条目 → 同样失败。

B3/B4 终态分类（not_yet_migrated 已归零）：
- public              真正无需身份的入口（登录/登出/me 探测/health 探活）
- account             账号/工作区控制面（platform / parent / 会话租户切换 /
                      legacy /api/users 通道 / 旧 /api/reset 的 403 墓碑）
- bind_code           以一次性绑定码本身为凭证的设备绑定端点
- tenant_user         会话用户经 TenantContext 访问的租户业务 API（PC 主力）
- tenant_device       设备/移动端业务 API（实现上同一 fail-closed 解析器同时
                      接受 会话/设备凭证/legacy 令牌，租户由凭证绑定决定：
                      legacy 令牌只可能映射 legacy-default，不存在越权面）
- tenant_admin_only   工作区管理动作（设置写入、租户 reset）——tenant_user 的
                      子集，要求 tenant_admin（或平台）
- legacy_default_only 过渡期共享令牌通道标记（当前无路由单列：legacy 令牌在
                      tenant_device/tenant_user 路由上只能到达 legacy-default）
- platform_tool       平台/研发工具（scale-capture 留全局根 scale_captures/，
                      不按子账号开放；/api/reset 墓碑复用 account 语义）
- share_only          本地版公众共享通道（独立 share token，不入租户，§2.4）

实现说明：业务路由的鉴权统一走 ContextResolver（会话 → 设备凭证 → 过渡期
legacy 共享令牌，fail-closed 401）；分类刻画的是该路由的**主要受众与历史
凭证通道**，而非实现层的分支。匿名（无任何凭证）访问任何 business 路由 → 401。
"""

from __future__ import annotations

CATEGORY_PUBLIC = "public"
CATEGORY_ACCOUNT = "account"
CATEGORY_BIND_CODE = "bind_code"
CATEGORY_TENANT_USER = "tenant_user"
CATEGORY_TENANT_DEVICE = "tenant_device"
CATEGORY_TENANT_ADMIN_ONLY = "tenant_admin_only"
CATEGORY_LEGACY_DEFAULT_ONLY = "legacy_default_only"
CATEGORY_PLATFORM_TOOL = "platform_tool"
CATEGORY_SHARE_ONLY = "share_only"
CATEGORY_NOT_YET_MIGRATED = "not_yet_migrated"  # 保留常量供测试引用；终态应为 0 条

NYM = CATEGORY_NOT_YET_MIGRATED

ROUTE_CATEGORIES: dict[tuple[str, str], str] = {
    # ---- ui/app.py 直接路由（B0.3 路由表） --------------------------- #
    ("GET", "/api/health"): CATEGORY_PUBLIC,
    ("POST", "/api/jobs"): CATEGORY_TENANT_DEVICE,
    ("GET", "/api/jobs"): CATEGORY_TENANT_DEVICE,
    ("GET", "/api/jobs/queue"): CATEGORY_TENANT_DEVICE,
    ("GET", "/api/jobs/{job_id}/wait"): CATEGORY_TENANT_DEVICE,
    ("GET", "/api/jobs/{job_id}"): CATEGORY_TENANT_DEVICE,
    ("GET", "/api/jobs/{job_id}/analysis-preview"): CATEGORY_TENANT_DEVICE,
    ("GET", "/api/jobs/{job_id}/report"): CATEGORY_TENANT_DEVICE,
    ("GET", "/api/boxes"): CATEGORY_TENANT_DEVICE,
    ("GET", "/api/boxes/recent"): CATEGORY_TENANT_DEVICE,
    ("POST", "/api/boxes"): CATEGORY_TENANT_DEVICE,
    ("GET", "/api/boxes/{cage_id}"): CATEGORY_TENANT_DEVICE,
    ("PATCH", "/api/boxes/{cage_id}"): CATEGORY_TENANT_DEVICE,
    ("POST", "/api/boxes/{cage_id}/reserve-ordinal"): CATEGORY_TENANT_DEVICE,
    ("GET", "/api/boxes/{cage_id}/qr.svg"): CATEGORY_TENANT_DEVICE,
    ("GET", "/api/boxes/{cage_id}/records"): CATEGORY_TENANT_DEVICE,
    ("GET", "/api/records/{record_id}"): CATEGORY_TENANT_DEVICE,
    ("GET", "/api/records/{record_id}/photo"): CATEGORY_TENANT_DEVICE,
    ("GET", "/api/records/{record_id}/clip"): CATEGORY_TENANT_DEVICE,
    ("POST", "/api/records/{record_id}/confirm-weight"): CATEGORY_TENANT_DEVICE,
    ("DELETE", "/api/records/{record_id}"): CATEGORY_TENANT_USER,
    ("POST", "/api/records/{record_id}/restore"): CATEGORY_TENANT_USER,
    ("PATCH", "/api/records/{record_id}"): CATEGORY_TENANT_USER,
    ("POST", "/api/records/{record_id}/publish"): CATEGORY_TENANT_USER,
    ("POST", "/api/records/{record_id}/unpublish"): CATEGORY_TENANT_USER,
    ("POST", "/api/records/{record_id}/verify"): CATEGORY_TENANT_USER,
    ("POST", "/api/records/{record_id}/reject"): CATEGORY_TENANT_USER,
    ("POST", "/api/runs/{run_id}/release-suspect"): CATEGORY_TENANT_USER,
    ("POST", "/api/runs/{run_id}/reject-suspect"): CATEGORY_TENANT_USER,
    ("POST", "/api/records/{record_id}/detection-label"): CATEGORY_TENANT_USER,
    ("POST", "/api/records/batch"): CATEGORY_TENANT_USER,
    ("GET", "/api/records"): CATEGORY_TENANT_USER,
    ("GET", "/api/overview"): CATEGORY_TENANT_USER,
    ("GET", "/api/mice-admin"): CATEGORY_TENANT_USER,
    ("GET", "/api/verify-cages"): CATEGORY_TENANT_USER,
    ("GET", "/api/export"): CATEGORY_TENANT_USER,
    ("POST", "/api/login"): CATEGORY_PUBLIC,
    ("POST", "/api/logout"): CATEGORY_PUBLIC,
    ("GET", "/api/me"): CATEGORY_PUBLIC,
    ("POST", "/api/me/password"): CATEGORY_ACCOUNT,
    ("GET", "/api/users"): CATEGORY_ACCOUNT,
    ("POST", "/api/users"): CATEGORY_ACCOUNT,
    ("PATCH", "/api/users/{user_id}"): CATEGORY_ACCOUNT,
    ("DELETE", "/api/users/{user_id}"): CATEGORY_ACCOUNT,
    ("GET", "/api/logs"): CATEGORY_TENANT_USER,
    ("GET", "/api/settings"): CATEGORY_TENANT_USER,
    ("PUT", "/api/settings"): CATEGORY_TENANT_ADMIN_ONLY,
    ("GET", "/api/status"): CATEGORY_TENANT_DEVICE,
    ("GET", "/api/runs"): CATEGORY_TENANT_DEVICE,
    ("POST", "/api/runs/active"): CATEGORY_TENANT_DEVICE,
    ("GET", "/api/mice"): CATEGORY_TENANT_DEVICE,
    ("GET", "/api/mice/{index}"): CATEGORY_TENANT_DEVICE,
    ("GET", "/api/mice/{index}/photo"): CATEGORY_TENANT_DEVICE,
    ("POST", "/api/start"): CATEGORY_TENANT_DEVICE,
    ("POST", "/api/reset"): CATEGORY_ACCOUNT,  # 403 墓碑：全局清盘已删除
    ("POST", "/api/tenants/{tenant_id}/reset"): CATEGORY_TENANT_ADMIN_ONLY,
    ("POST", "/api/stop"): CATEGORY_TENANT_DEVICE,
    ("GET", "/api/upload-queue"): CATEGORY_TENANT_DEVICE,
    ("GET", "/api/stream"): CATEGORY_TENANT_DEVICE,
    ("GET", "/api/photo"): CATEGORY_TENANT_DEVICE,
    ("POST", "/api/lab/compare"): CATEGORY_TENANT_USER,
    ("GET", "/api/lab/compare/{compare_id}"): CATEGORY_TENANT_USER,
    ("GET", "/api/lab/compares"): CATEGORY_TENANT_USER,
    ("GET", "/api/lab/videos"): CATEGORY_TENANT_USER,
    ("GET", "/api/lab/videos/{run_id}/poster"): CATEGORY_TENANT_USER,
    ("GET", "/api/lab/compare/{compare_id}/branches/{branch}/mice/{ordinal}/photo"): CATEGORY_TENANT_USER,
    ("GET", "/api/lab/compare/{compare_id}/branches/{branch}/mice/{ordinal}/clip"): CATEGORY_TENANT_USER,
    # ---- realtime 路由器 --------------------------------------------- #
    ("POST", "/api/realtime/session"): CATEGORY_TENANT_DEVICE,
    ("GET", "/api/realtime/session/{session_id}/status"): CATEGORY_TENANT_DEVICE,
    ("POST", "/api/realtime/session/{session_id}/retry"): CATEGORY_TENANT_DEVICE,
    ("POST", "/api/realtime/session/{session_id}/accept"): CATEGORY_TENANT_DEVICE,
    ("POST", "/api/realtime/session/{session_id}/finish"): CATEGORY_TENANT_DEVICE,
    ("WS", "/api/realtime/ws"): CATEGORY_TENANT_DEVICE,
    # ---- scale-sync 路由器 -------------------------------------------- #
    ("POST", "/api/scale-sync/sessions"): CATEGORY_TENANT_DEVICE,
    ("PUT", "/api/scale-sync/sessions/{session_id}/anchors/{kind}"): CATEGORY_TENANT_DEVICE,
    ("DELETE", "/api/scale-sync/sessions/{session_id}/anchors/{kind}"): CATEGORY_TENANT_DEVICE,
    ("POST", "/api/scale-sync/sessions/{session_id}/imports"): CATEGORY_TENANT_DEVICE,
    ("GET", "/api/scale-sync/sessions/{session_id}/imports/{import_id}/readings"): CATEGORY_TENANT_DEVICE,
    ("PUT", "/api/scale-sync/sessions/{session_id}/anchors/{kind}/match"): CATEGORY_TENANT_DEVICE,
    ("POST", "/api/scale-sync/sessions/{session_id}/calculate"): CATEGORY_TENANT_DEVICE,
    ("GET", "/api/scale-sync/sessions/{session_id}"): CATEGORY_TENANT_DEVICE,
    # ---- report / share / capture 路由器 ------------------------------ #
    ("POST", "/api/records/report"): CATEGORY_TENANT_DEVICE,
    ("POST", "/api/records/share"): CATEGORY_SHARE_ONLY,
    ("POST", "/api/scale-capture"): CATEGORY_PLATFORM_TOOL,
    # ---- 控制面（ui/control_api.py） ---------------------------------- #
    ("POST", "/api/control/accounts"): CATEGORY_ACCOUNT,
    ("GET", "/api/control/accounts"): CATEGORY_ACCOUNT,
    ("POST", "/api/control/accounts/{account_id}/tenants"): CATEGORY_ACCOUNT,
    ("GET", "/api/control/accounts/{account_id}/tenants"): CATEGORY_ACCOUNT,
    ("GET", "/api/control/tenants"): CATEGORY_ACCOUNT,
    ("GET", "/api/control/tenants/{tenant_id}/members"): CATEGORY_ACCOUNT,
    ("POST", "/api/control/tenants/{tenant_id}/members"): CATEGORY_ACCOUNT,
    ("DELETE", "/api/control/tenants/{tenant_id}/members/{user_id}"): CATEGORY_ACCOUNT,
    ("GET", "/api/control/tenants/{tenant_id}/devices"): CATEGORY_ACCOUNT,
    ("POST", "/api/control/tenants/{tenant_id}/devices"): CATEGORY_ACCOUNT,
    ("DELETE", "/api/control/devices/{device_id}"): CATEGORY_ACCOUNT,
    ("POST", "/api/control/tenants/{tenant_id}/bind-codes"): CATEGORY_ACCOUNT,
    ("POST", "/api/control/devices/bind"): CATEGORY_BIND_CODE,
    # B5（§6.2/§15-B5）：子账号密码换设备凭证——与绑定码同为「无会话凭证签发」
    # 通道（带 IP 限速），复用 bind_code 分类。
    ("POST", "/api/control/devices/login"): CATEGORY_BIND_CODE,
    # 凭证轮换（签发新+撤旧原子）：tenant_admin/平台的管理动作 → account。
    ("POST", "/api/control/devices/{device_id}/rotate"): CATEGORY_ACCOUNT,
    ("GET", "/api/control/session"): CATEGORY_ACCOUNT,
    ("POST", "/api/control/session/tenant"): CATEGORY_ACCOUNT,
    ("DELETE", "/api/control/session/tenant"): CATEGORY_ACCOUNT,
    # ---- 主账号 account 级汇总/导出（B6，§15-B6） ---------------------- #
    # parent_owner / platform_admin 的只读扇出视图；子账号 403 → account。
    ("GET", "/api/account/summary"): CATEGORY_ACCOUNT,
    ("GET", "/api/account/export"): CATEGORY_ACCOUNT,
}
