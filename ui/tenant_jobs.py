"""租户感知的分析任务调度器（合同 §2.6 / §6.3 / §15-B3）。

约束：
- **单 worker**：与旧 AnalysisJobManager 相同的单进程单线程模型，跨全部租户
  一次只分析一个 job；不引入多 worker / 队列服务。
- 每个 job 行固化 ``tenant_id``（JobStore tenant_id 列）；worker 每次出队用
  该 tenant_id 经 :class:`TenantStoreFactory` 重新解析 store/output_root，
  **线程局部不缓存上一个 job 的根**（出队即解析，用完即弃）。
- 各租户的重分析对象（pipeline / reserve 回调 / upload_queue）按租户挂在
  ``TenantStores.extra["job_manager"]``（AnalysisJobManager 实例，不自带线程），
  ``process_one`` 只做一次出队任务的处理。
"""

from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from typing import Any

from ui.tenant_stores import TenantStoreFactory

log = logging.getLogger("tenant_jobs")


class TenantJobDispatcher:
    """跨租户的单 worker 分析队列。

    队列元素为 ``(tenant_id, job_id)``；``start()`` 遍历 control 中 active
    租户逐个 recover（fail_interrupted / reconcile_held / prune）并把遗留
    queued 任务重新入队。
    """

    def __init__(self, factory: TenantStoreFactory) -> None:
        self.factory = factory
        self._queue: queue.Queue[tuple[str, str] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- #
    def start(self) -> None:
        """启动单 worker；恢复各 active 租户的中断任务（幂等）。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            queued: list[tuple[str, str]] = []
            for tenant in self.factory.active_tenants():
                tid = str(tenant["id"])
                try:
                    stores = self.factory.stores(tid)
                except KeyError:
                    continue  # 租户行刚被删：跳过
                mgr = stores.extra["job_manager"]
                for job_id in mgr.recover():
                    queued.append((tid, job_id))
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()
        for item in queued:
            self._queue.put(item)

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)
        thread = self._thread
        if thread is not None and self._thread.is_alive():
            thread.join(timeout=3.0)

    # ---------------------------------------------------------------- #
    def submit(self, tenant_id: str, job_id: str) -> dict[str, Any]:
        """把一个已落盘的上传任务标记 queued 并进入全局单队列。"""
        stores = self.factory.stores(tenant_id)
        mgr = stores.extra["job_manager"]
        queued = mgr.enqueue(job_id)
        # 防串号：job 行的 tenant_id 必须与入队租户一致（job 行固化字段）。
        if queued.get("tenant_id") and str(queued["tenant_id"]) != str(tenant_id):
            raise ValueError("job tenant_id mismatch")
        self._queue.put((str(tenant_id), str(job_id)))
        return queued

    def active_count(self) -> int:
        """全局在途任务数（上传/排队/分析中，跨租户求和）——仅供运维观测。"""
        total = 0
        for tenant in self.factory.active_tenants():
            try:
                stores = self.factory.stores(str(tenant["id"]))
            except KeyError:
                continue
            total += stores.job_store.active_count()
        return total

    # ---------------------------------------------------------------- #
    def _worker(self) -> None:
        while not self._stop.is_set():
            item = self._queue.get()  # blocks until submit() or sentinel
            if item is None:
                break
            tenant_id, job_id = item
            self._process(tenant_id, job_id)

    def _process(self, tenant_id: str, job_id: str) -> None:
        """出队即按 job 的 tenant_id 重新解析 store/root；不缓存上一个根。"""
        try:
            stores = self.factory.stores(tenant_id)
        except KeyError:
            log.error("tenant job dropped (tenant missing): tenant=%s job=%s", tenant_id, job_id)
            return
        mgr = stores.extra["job_manager"]
        job = stores.job_store.get(job_id)
        if job is None:
            return
        row_tenant = str(job.get("tenant_id") or "")
        if row_tenant and row_tenant != str(tenant_id):
            log.error(
                "tenant job tenant_id mismatch (row=%s queue=%s job=%s) — skipped",
                row_tenant, tenant_id, job_id,
            )
            return
        try:
            mgr.process_one(job_id)
        except Exception:  # noqa: BLE001 - 单个 job 的意外异常不得杀死 worker
            log.exception("tenant job crashed: tenant=%s job=%s", tenant_id, job_id)
