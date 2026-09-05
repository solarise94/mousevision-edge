"""TenantStoreFactory：唯一的租户目录解析与租户 store 集（合同 §4.3 / §5 / §12）。

B2 批次只交付骨架：目录解析、路径安全（UUID 校验 + 租户行存在性检查，防
``../``）、按租户缓存 store 实例、``require_role`` 作用域角色检查、
``context_from_request``。业务 handler 的接线改造属于 B3，本批不动。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from ui.control_store import LEGACY_TENANT_ID, ControlStore
from ui.tenant_context import ContextResolver, TenantContext


@dataclass
class TenantStores:
    """一个租户的整套业务 store（实例由 factory 按 tenant 构造，§14.2）。

    SQLite 主键仍可只用 cage_id / record_id —— 库本身已在租户目录内；
    应用层和跨租户汇总不得假设它们全局唯一（合同 §4.4）。
    """

    tenant_id: str
    output_root: Path
    box_registry: Any = None      # ui.boxes.BoxRegistry
    job_store: Any = None         # mousevision.jobs.JobStore
    records_meta: Any = None      # ui.records_meta.RecordsMetaStore
    upload_queue: Any = None      # mousevision.upload_queue.UploadQueue
    settings_store: Any = None    # ui.settings.SettingsStore
    registry: Any = None          # ui.registry.MouseRegistry
    extra: dict[str, Any] = field(default_factory=dict)


class _LazyExtra(dict):
    """按需构建的重对象（PlaybackEngine / ScaleSyncStore / 租户 JobManager）。

    builder 由宿主（ui.app）注册，避免 tenant_stores 反向依赖业务模块；
    每租户一份，禁止任何跨租户共享"正在播什么/正在算什么"。
    """

    def __init__(self, stores: "TenantStores", builders: dict[str, Any]) -> None:
        super().__init__()
        self._stores = stores
        self._builders = builders

    def __getitem__(self, key: str) -> Any:
        if key not in self:
            builder = self._builders.get(key)
            if builder is None:
                raise KeyError(key)
            self[key] = builder(self._stores)
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        try:
            return self[key]
        except KeyError:
            return default


class TenantStoreFactory:
    """租户目录 + store 集的唯一来源；禁止再提供无 tenant 的模块级默认值。"""

    def __init__(self, output_root: str | Path, control_store: ControlStore) -> None:
        self.output_root = Path(output_root)
        self.control = control_store
        self._resolver = ContextResolver(control_store, self.output_root)
        self._cache: dict[str, TenantStores] = {}
        self._lock = threading.Lock()
        self._extra_builders: dict[str, Any] = {}

    # ---------------------------------------------------------------- #
    # 路径安全
    # ---------------------------------------------------------------- #
    @property
    def tenants_root(self) -> Path:
        return self.output_root / "tenants"

    @staticmethod
    def validate_tenant_id(tenant_id: str) -> str:
        """只接受合法 UUID（防 ``../`` 与任意路径拼接；路径只拼服务端 ID）。"""
        try:
            return str(uuid.UUID(str(tenant_id)))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError(f"invalid tenant id: {tenant_id!r}") from exc

    def tenant_dir(self, tenant_id: str) -> Path:
        tid = self.validate_tenant_id(tenant_id)
        tenant = self.control.get_tenant(tid)
        if tenant is None:
            raise KeyError(f"tenant not found: {tid}")
        return self.tenants_root / tid

    def ensure_legacy_default(self) -> Path:
        """幂等确保 legacy-default 租户行存在（B7 迁移前的兼容目标目录）。"""
        self.control.ensure_legacy_default()
        return self.tenants_root / LEGACY_TENANT_ID

    # ---------------------------------------------------------------- #
    # 重对象 extra 构建器（宿主注册，避免反向依赖）
    # ---------------------------------------------------------------- #
    def register_extra_builder(self, name: str, builder: Any) -> None:
        """注册按租户构建重对象的工厂（如 PlaybackEngine）。幂等覆盖。"""
        self._extra_builders[name] = builder

    def active_tenants(self) -> list[dict[str, Any]]:
        """control 中 status=active 的租户行（lifespan 按租户恢复用）。"""
        return [
            t for t in self.control.list_tenants()
            if str(t.get("status") or "active") == "active"
        ]

    def orphan_tenant_dirs(self) -> list[Path]:
        """tenants/ 下存在目录但无租户行的孤儿目录（启动时报告，不崩溃）。"""
        orphans: list[Path] = []
        root = self.tenants_root
        if not root.is_dir():
            return orphans
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            try:
                tid = self.validate_tenant_id(child.name)
            except ValueError:
                orphans.append(child)
                continue
            if self.control.get_tenant(tid) is None:
                orphans.append(child)
        return orphans

    # ---------------------------------------------------------------- #
    # store 集（按租户缓存；请求结束不关连接，进程内复用，§5）
    # ---------------------------------------------------------------- #
    def stores(self, tenant_id: str) -> TenantStores:
        tid = self.validate_tenant_id(tenant_id)
        if self.control.get_tenant(tid) is None:
            raise KeyError(f"tenant not found: {tid}")
        # 构建全程持锁：避免并发首访对同一租户各建一套 store（SQLite 建库/
        # 迁移非并发安全，并发 8 线程 reserve-ordinal 首访时曾触发 ALTER 竞态）。
        with self._lock:
            cached = self._cache.get(tid)
            if cached is not None:
                return cached
            root = self.tenants_root / tid
            root.mkdir(parents=True, exist_ok=True)
            stores = self._build_stores(tid, root)
            self._cache[tid] = stores
            return stores

    def drop_tenant(self, tenant_id: str) -> None:
        """租户 reset 后丢弃该租户的 store/重对象缓存（下次访问重建）。

        仅在调用方已停止该租户的 engine / realtime / job 活动之后调用；
        各 store 均为按操作开关连接，无长驻连接需要关闭。
        """
        tid = self.validate_tenant_id(tenant_id)
        with self._lock:
            self._cache.pop(tid, None)

    def _build_stores(self, tenant_id: str, root: Path) -> TenantStores:
        from mousevision.jobs import JobStore
        from mousevision.upload_queue import UploadQueue

        from ui.boxes import BoxRegistry
        from ui.records_meta import RecordsMetaStore
        from ui.registry import MouseRegistry
        from ui.settings import SettingsStore

        stores = TenantStores(
            tenant_id=tenant_id,
            output_root=root,
            box_registry=BoxRegistry(root / "boxes.db"),
            job_store=JobStore(root / "jobs.db"),
            records_meta=RecordsMetaStore(str(root / "records_meta.db")),
            upload_queue=UploadQueue(root / "upload_queue.db"),
            settings_store=SettingsStore(root / "settings.json"),
            registry=MouseRegistry(root / "mice_registry.json", root),
        )
        stores.extra = _LazyExtra(stores, self._extra_builders)
        return stores

    # ---------------------------------------------------------------- #
    # 上下文与角色（合同 §12 签名）
    # ---------------------------------------------------------------- #
    def context_from_request(self, request: Request) -> TenantContext:
        """从请求解析 TenantContext；无凭证/凭证无效 → 401（fail-closed）。"""
        return self._resolver.resolve(request)

    @staticmethod
    def require_role(ctx: TenantContext, *roles: str) -> None:
        """作用域角色检查：account 级上下文与角色不合者一律 403。

        platform_admin 不是租户角色（§4.2），parent_owner 是只读作用域，
        都不会通过需要写角色的检查。
        """
        if ctx.is_account_level or not (ctx.roles & set(roles)):
            raise HTTPException(status_code=403, detail="权限不足")
