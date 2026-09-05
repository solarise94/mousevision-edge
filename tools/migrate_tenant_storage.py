#!/usr/bin/env python3
"""租户存储迁移工具（合同 docs/UPGRADE_TENANT_ISOLATION.md §5.1 / §15-B7 / §16-G5）。

子命令
------
inventory  只读盘点旧数据根，输出 JSON 报告：§5.1 白名单在位情况、白名单外条目
           （``ignored``，逐项带原因，绝不宽泛 glob 静默跳过）、DB 行数、run/record/
           photo/video 计数、重复 record_id、运行实例证据（最近 mtime 的 WAL/锁文件）。
stage      把白名单复制到 staging 的 ``tenants/<uuid>/``（source 原样不动；staging 必须
           是 source 的兄弟目录，放进 source 内部 / 与 source 互相嵌套一律拒绝，防递归
           复制）。复制后做 legacy-default 收尾：缺失的 settings.json / mice_registry.json
           / job_uploads/ 写入最小可用默认值，落位即用（与 TenantStoreFactory 期望布局
           一致）。SQLite 一律走 backup API 快照（WAL 折叠），坏库退回字节复制。
verify     逐项比对 source 与 staging：DB 表集与行数、run 目录名集合、record/photo/video
           数、record.json SHA-256、全部非 DB 文件 SHA-256、总字节数、缺失/多余文件、
           重复 record_id（两侧）。任何差异 → ok=False，CLI 退出码 1。
activate   唯一有生产副作用的子命令：必须显式 ``--i-understand-data-loss`` + 维护模式
           检查（source 侧发现最近 mtime 的 db WAL/SHM/锁文件即拒绝）+ 最终 verify
           通过，三者缺一不可；旧根原子改名为带时间戳只读备份（chmod a-w），staging
           原子就位为新根。``--rollback`` 仅在激活后新根无新写入（文件 mtime 均早于
           激活时间戳）时允许切回；检测到新写入一律拒绝并打印反向对账指引，绝不自动
           合并。

G5 形态（§16）::

    .venv/bin/python tools/migrate_tenant_storage.py inventory --source <dir> --report <json>
    .venv/bin/python tools/migrate_tenant_storage.py stage --source <dir> --staging <dir> \
        --legacy-tenant-id 00000000-0000-4000-8000-000000000001
    .venv/bin/python tools/migrate_tenant_storage.py verify --source <dir> --staging <dir> \
        --report <json>

退出码：0 成功；1 差异/拒绝；2 参数错误。仅标准库（argparse/shutil/sqlite3/hashlib/json）。
users.db 不迁移：账号并入 control.db 属部署手册事项（§5.1.3），本工具只搬数据根。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOOL_NAME = "migrate_tenant_storage"

#: 与 ui/control_store.LEGACY_TENANT_ID 一致（§5.1.1 固定 UUID）。
LEGACY_TENANT_ID = "00000000-0000-4000-8000-000000000001"

#: §5.1.2 复制白名单——顶层文件。
WHITELIST_FILES: tuple[str, ...] = (
    "boxes.db",
    "jobs.db",
    "records_meta.db",
    "upload_queue.db",
    "settings.json",
    "mice_registry.json",
)
#: 白名单中的 SQLite 库（走 backup API；verify 比行数不比字节）。
TOP_LEVEL_DBS: tuple[str, ...] = (
    "boxes.db",
    "jobs.db",
    "records_meta.db",
    "upload_queue.db",
)
#: 白名单目录：run_* 前缀目录 + job_uploads/。
WHITELIST_DIR_NAMES: tuple[str, ...] = ("job_uploads",)
RUN_DIR_PREFIX = "run_"

#: stage 收尾文件：legacy 根缺失时写入最小可用默认值（落位即用）。
FINISHING_FILES: tuple[str, ...] = ("settings.json", "mice_registry.json")

#: 计数镜像键（inventory 报告 counts / 顶层镜像）。
COUNT_MIRRORS = {
    "boxes.db": "boxes",
    "jobs.db": "jobs",
    "records_meta.db": "records_meta",
    "upload_queue.db": "upload_queue",
}
#: 各库的主业务表（报告可读性用；verify 比较全部用户表）。
MAIN_TABLES = {
    "boxes.db": "boxes",
    "jobs.db": "analysis_jobs",
    "records_meta.db": "records_meta",
    "upload_queue.db": "upload_queue",
}

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".3gp"}

#: 维护模式检查：视为「已运行实例证据」的 sidecar / 锁文件后缀。
INSTANCE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
DEFAULT_INSTANCE_IDLE_SECONDS = 300.0

#: 与 ui.settings.DEFAULT_SETTINGS 一致（ui 不可导入时的兜底副本）。
_DEFAULT_SETTINGS_FALLBACK = {
    "project_id": "default",
    "mouse_no_pad": 2,
    "mouse_no_start": 1,
    "retention_days": 365,
    "publish_target": "",
    "default_strain": "C57BL/6",
    "admin_password_hint": "首次登录请修改默认管理员密码",
}
#: 与 ui.registry.MouseRegistry._default 一致。
_REGISTRY_DEFAULT = {"active_run_id": None, "active_run_dir": None}

#: 白名单外已知条目 → 逐项原因（合同 §15-B7：忽略必须显式列入报告）。
IGNORED_REASONS = {
    "users.db": "账号库不迁入租户目录（§5.1.3）：账号并入 control.db 属部署手册事项，"
                "B7 只搬数据根；旧 users.db 留在只读备份中",
    "audit.db": "审计已由控制面 control/audit.db 承接（B3，每条带 tenant_id）；"
                "旧 audit.db 留在只读备份，不进租户",
    "control": "控制面目录（control.db / audit.db）全站一份，不属于任何租户（§5.1）",
    "shared": "share-only 本地共享通道独立根，不并入任何工作区（§5 / §11）",
    "scale_captures": "platform_tool 全局根，仅平台/研发使用，不按租户迁移（§5）",
    "compare_runs": "历史对比结果未列入 §5.1 白名单；B3 起按租户目录重建，"
                    "如需保留历史需人工决定后手工复制",
    ".thumbs": "缩略图缓存，服务端按需重建，不迁移",
    "scale_sync.db": "秤时间同步库未列入 §5.1 白名单；如需保留应手工迁入租户目录"
                     "（§5 布局允许 scale_sync/ 进租户）",
    "scale_sync": "秤时间同步数据未列入 §5.1 白名单；同 scale_sync.db，需人工决定",
    ".DS_Store": "macOS 桌面元数据，与业务数据无关",
}
DEFAULT_IGNORE_REASON = "未列入 §5.1 复制白名单的未知条目（显式列出，不复制、不静默跳过）"

STATE_FILE_PREFIX = ".migrate-tenant-state-"

_ROLLOUT_GUIDANCE = (
    "检测到激活后新根（租户布局）有新写入（存在 mtime 晚于激活时间戳的文件）。"
    "禁止直接切回旧根——会把新记录丢在旧根之外。请先反向对账：\n"
    "  1) .venv/bin/python tools/migrate_tenant_storage.py inventory --source <当前新根> "
    "--report <new-inventory.json>  # 得到新写入的 run/record 清单\n"
    "  2) 对照激活时间戳找出新增/修改的 run_*、record.json、DB 行，人工决定合并目标；\n"
    "  3) 处理完毕后再执行 --rollback。本工具不做自动合并。"
)


class MigrationError(RuntimeError):
    """结构性校验失败（staging 位置非法、参数无效等）——CLI 退出码 1。"""


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json_report(path: str | os.PathLike[str] | None, data: dict) -> Path | None:
    if path is None:
        return None
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _normalize_tenant_id(legacy_tenant_id: str) -> str:
    try:
        return str(uuid.UUID(str(legacy_tenant_id)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise MigrationError(f"legacy-tenant-id 不是合法 UUID: {legacy_tenant_id!r}") from exc


def _validate_stage_placement(source: Path, staging: Path) -> None:
    """staging 必须是 source 的兄弟目录；任何嵌套关系都拒绝（防递归复制）。"""
    if staging == source:
        raise MigrationError(f"staging 不能与 source 是同一目录: {staging}")
    if source in staging.parents:
        raise MigrationError(
            f"staging 在 source 内部（递归复制风险），已拒绝: staging={staging} source={source}"
        )
    if staging in source.parents:
        raise MigrationError(
            f"source 在 staging 内部（递归复制风险），已拒绝: staging={staging} source={source}"
        )
    if staging.parent != source.parent:
        raise MigrationError(
            "staging 必须是 source 的兄弟目录（同一父目录）: "
            f"staging.parent={staging.parent} source.parent={source.parent}"
        )


# --------------------------------------------------------------------------- #
# SQLite：只读计数（绝不写 source；WAL 打不开就快照到临时目录）
# --------------------------------------------------------------------------- #
def _connect_ro(db_path: Path) -> sqlite3.Connection:
    uri = "file:" + urllib.parse.quote(str(db_path)) + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _count_tables(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    counts: dict[str, int] = {}
    errors: dict[str, str] = {}
    for table in sorted(tables):
        quoted = '"' + table.replace('"', '""') + '"'
        try:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
        except sqlite3.Error as exc:
            errors[table] = f"{type(exc).__name__}: {exc}"
    return {"tables": counts, "errors": errors}


def _read_db_structure(db_path: Path) -> dict[str, Any]:
    """→ {"tables": {表: 行数}, "errors": {...}}；库打不开时 tables 为空且带 errors。"""
    try:
        conn = _connect_ro(db_path)
        try:
            return _count_tables(conn)
        finally:
            conn.close()
    except sqlite3.Error as first_err:
        # WAL 模式只读打开失败等场景：把 db(+wal/shm) 快照到临时目录再读，绝不写 source。
        try:
            with tempfile.TemporaryDirectory(prefix="mv-mig-dbread-") as td:
                tmp = Path(td) / db_path.name
                shutil.copy2(db_path, tmp)
                for suffix in ("-wal", "-shm"):
                    side = db_path.parent / (db_path.name + suffix)
                    if side.exists():
                        shutil.copy2(side, Path(str(tmp) + suffix))
                conn = sqlite3.connect(str(tmp))
                try:
                    return _count_tables(conn)
                finally:
                    conn.close()
        except (sqlite3.Error, OSError) as exc:
            return {
                "tables": {},
                "errors": {
                    db_path.name: f"{type(first_err).__name__}: {first_err}; "
                                  f"snapshot fallback: {type(exc).__name__}: {exc}"
                },
            }


def _upload_queue_duplicate_ids(db_path: Path) -> dict[str, int]:
    """upload_queue.record_id 重复检测（旧库可能没有唯一索引）。"""
    structure = _read_db_structure(db_path)
    if "upload_queue" not in structure["tables"]:
        return {}
    conn = _connect_ro(db_path)
    try:
        rows = conn.execute(
            "SELECT record_id, COUNT(*) AS c FROM upload_queue "
            "WHERE record_id IS NOT NULL AND record_id != '' "
            "GROUP BY record_id HAVING c > 1"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {str(row[0]): int(row[1]) for row in rows}


# --------------------------------------------------------------------------- #
# 目录扫描
# --------------------------------------------------------------------------- #
def _run_dirs(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    return sorted(
        p for p in base.iterdir()
        if p.is_dir() and p.name.startswith(RUN_DIR_PREFIX)
    )


def _plan_copy(source: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """→ (plan, ignored, missing_whitelist)。ignored 逐项列出白名单外条目及原因。"""
    plan: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    missing: list[str] = []
    if not source.is_dir():
        raise MigrationError(f"source 不存在或不是目录: {source}")
    for name in WHITELIST_FILES:
        p = source / name
        if p.is_file():
            plan.append({"name": name, "kind": "sqlite" if name in TOP_LEVEL_DBS else "file", "path": p})
        elif p.is_dir():
            plan.append({"name": name, "kind": "dir", "path": p})
        else:
            missing.append(name)
    for child in sorted(source.iterdir()):
        if child.name in WHITELIST_FILES:
            continue
        is_run = child.is_dir() and child.name.startswith(RUN_DIR_PREFIX)
        is_whitelisted_dir = child.is_dir() and child.name in WHITELIST_DIR_NAMES
        if is_run or is_whitelisted_dir:
            plan.append({"name": child.name, "kind": "dir", "path": child})
            continue
        reason = IGNORED_REASONS.get(child.name, DEFAULT_IGNORE_REASON)
        entry: dict[str, Any] = {
            "name": child.name,
            "kind": "dir" if child.is_dir() else "file",
            "reason": reason,
        }
        if child.is_file():
            entry["bytes"] = child.stat().st_size
        ignored.append(entry)
    return plan, ignored, missing


def _record_ids_under_runs(base: Path) -> tuple[dict[str, list[str]], list[str]]:
    """扫描 run_* 下的 record.json → ({record_id: [相对路径]}, [无法解析的路径])。"""
    ids: dict[str, list[str]] = {}
    unparsed: list[str] = []
    for run in _run_dirs(base):
        for rec in sorted(run.rglob("record.json")):
            rel = rec.relative_to(base).as_posix()
            try:
                data = json.loads(rec.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                unparsed.append(rel)
                continue
            rid = data.get("record_id") if isinstance(data, dict) else None
            if not rid:
                unparsed.append(rel)
                continue
            ids.setdefault(str(rid), []).append(rel)
    return ids, unparsed


def _duplicates(base: Path) -> dict[str, Any]:
    ids, unparsed = _record_ids_under_runs(base)
    dups = {rid: rels for rid, rels in ids.items() if len(rels) > 1}
    queue_dups: dict[str, int] = {}
    queue_db = base / "upload_queue.db"
    if queue_db.is_file():
        queue_dups = _upload_queue_duplicate_ids(queue_db)
    return {
        "record_ids": dups,
        "upload_queue_record_ids": queue_dups,
        "unparsed_record_json": unparsed,
        "ok": not dups and not queue_dups,
    }


def _instance_activity(source: Path, idle_seconds: float) -> dict[str, Any]:
    """维护模式检查：最近 mtime 的 db WAL/SHM/journal/锁文件 = 已运行实例证据。

    只看数据库 sidecar 与锁文件（合同 §15-B7 原文「db WAL/锁文件」）；
    job_uploads 等业务数据的新近程度不作为实例证据（会产生长期误报）。
    """
    now = time.time()
    evidence: list[dict[str, Any]] = []
    for child in sorted(source.glob("*")) if source.is_dir() else []:
        if not child.is_file():
            continue
        if child.name.endswith(INSTANCE_SIDECAR_SUFFIXES) or child.name.endswith(".lock"):
            age = max(0.0, now - child.stat().st_mtime)
            evidence.append(
                {"file": child.name, "age_seconds": round(age, 1), "recent": age < idle_seconds}
            )
    return {
        "idle_seconds": idle_seconds,
        "evidence": evidence,
        "active": any(item["recent"] for item in evidence),
    }


# --------------------------------------------------------------------------- #
# inventory
# --------------------------------------------------------------------------- #
def run_inventory(source: str | os.PathLike[str], report: str | os.PathLike[str] | None = None,
                  max_instance_idle_seconds: float = DEFAULT_INSTANCE_IDLE_SECONDS) -> dict[str, Any]:
    """只读盘点旧数据根；写 JSON 报告文件并返回报告 dict。"""
    src = Path(source).resolve()
    inventory: dict[str, Any] = {
        "tool": TOOL_NAME,
        "action": "inventory",
        "source": str(src),
        "generated_at": _now_iso(),
        "ok": src.is_dir(),
    }
    if not src.is_dir():
        inventory["error"] = f"source 不存在或不是目录: {src}"
        _write_json_report(report, inventory)
        return inventory

    plan, ignored, missing = _plan_copy(src)
    counts: dict[str, Any] = {}
    db_detail: dict[str, Any] = {}
    db_errors: dict[str, str] = {}
    for item in plan:
        if item["kind"] != "sqlite":
            continue
        structure = _read_db_structure(item["path"])
        db_detail[item["name"]] = structure
        db_errors.update(structure["errors"])
        key = COUNT_MIRRORS.get(item["name"])
        mirror_key = MAIN_TABLES.get(item["name"])
        value = structure["tables"].get(mirror_key) if key and mirror_key else None
        if key:
            counts[key] = value

    runs = _run_dirs(src)
    photos = 0
    videos = 0
    records = 0
    total_bytes = 0
    for run in runs:
        for p in run.rglob("*"):
            if not p.is_file():
                continue
            ext = p.suffix.lower()
            if ext in PHOTO_EXTS:
                photos += 1
            elif ext in VIDEO_EXTS:
                videos += 1
            if p.name == "record.json":
                records += 1
    for item in plan:
        if item["kind"] == "sqlite":
            continue
        if item["kind"] == "dir":
            for p in item["path"].rglob("*"):
                if p.is_file():
                    total_bytes += p.stat().st_size
        else:
            total_bytes += item["path"].stat().st_size

    counts.setdefault("boxes", None)
    counts.setdefault("jobs", None)
    counts.setdefault("records_meta", None)
    counts.setdefault("upload_queue", None)
    counts["runs"] = len(runs)
    counts["records"] = records
    counts["photos"] = photos
    counts["videos"] = videos
    counts["whitelist_bytes"] = total_bytes

    job_uploads = src / "job_uploads"
    job_upload_files = (
        sum(1 for p in job_uploads.rglob("*") if p.is_file()) if job_uploads.is_dir() else 0
    )
    counts["job_upload_files"] = job_upload_files

    inventory.update(
        counts=counts,
        # 顶层镜像：report["boxes"] 与 report["counts"]["boxes"] 等价
        boxes=counts["boxes"],
        jobs=counts["jobs"],
        records_meta=counts["records_meta"],
        upload_queue=counts["upload_queue"],
        runs=[r.name for r in runs],
        records=counts["records"],
        photos=counts["photos"],
        videos=counts["videos"],
        whitelist_present=[item["name"] for item in plan],
        missing_whitelist=missing,
        ignored=ignored,
        db_detail=db_detail,
        db_errors=db_errors,
        duplicates=_duplicates(src),
        instance_activity=_instance_activity(src, max_instance_idle_seconds),
        notes=[
            "ignored 为白名单外条目，逐项给出原因，不复制（§15-B7）",
            "users.db 不迁移：账号并入 control.db 属部署手册事项（§5.1.3）",
        ],
    )
    _write_json_report(report, inventory)
    return inventory


# --------------------------------------------------------------------------- #
# stage
# --------------------------------------------------------------------------- #
def _backup_sqlite(src: Path, dest: Path) -> dict[str, Any]:
    """SQLite 走 backup API（WAL 折叠为单文件）；坏库退回字节复制。绝不写 source。"""
    sidecars = [
        src.name + suffix for suffix in ("-wal", "-shm", "-journal")
        if (src.parent / (src.name + suffix)).exists()
    ]
    try:
        src_conn = _connect_ro(src)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dst_conn = sqlite3.connect(str(dest))
            try:
                src_conn.backup(dst_conn)
                # 统一为 rollback journal 模式：checkpoint 进主文件、移除 -wal。
                dst_conn.execute("PRAGMA journal_mode=DELETE")
                return {"method": "sqlite-backup", "sidecars_folded": sidecars}
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
    except (sqlite3.Error, OSError):
        if dest.exists():
            dest.unlink()
        shutil.copy2(src, dest)
        return {"method": "file-copy(not-a-valid-db)", "sidecars_folded": sidecars}


def _copy_plan(plan: list[dict[str, Any]], tenant_dir: Path) -> list[dict[str, Any]]:
    copied: list[dict[str, Any]] = []
    for item in plan:
        dest = tenant_dir / item["name"]
        if item["kind"] == "sqlite":
            note = _backup_sqlite(item["path"], dest)
            copied.append({"name": item["name"], "kind": "sqlite",
                           "bytes": dest.stat().st_size, **note})
        elif item["kind"] == "dir":
            shutil.copytree(item["path"], dest)
            size = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())
            copied.append({"name": item["name"], "kind": "dir", "bytes": size,
                           "method": "copytree"})
        else:
            shutil.copy2(item["path"], dest)
            copied.append({"name": item["name"], "kind": "file",
                           "bytes": dest.stat().st_size, "method": "copy2"})
    return copied


def _finish_tenant_dir(tenant_dir: Path) -> list[str]:
    """legacy-default 收尾：缺失的 settings/registry/job_uploads 写最小可用默认值。"""
    actions: list[str] = []
    try:
        from ui.settings import DEFAULT_SETTINGS  # 第一方、纯标准库模块
        settings_default = dict(DEFAULT_SETTINGS)
    except Exception:
        settings_default = dict(_DEFAULT_SETTINGS_FALLBACK)
    for name in FINISHING_FILES:
        p = tenant_dir / name
        if not p.exists():
            data = _REGISTRY_DEFAULT if name == "mice_registry.json" else settings_default
            p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            actions.append(f"created {name}（legacy 根缺失，写入最小可用默认值）")
    job_uploads = tenant_dir / "job_uploads"
    if not job_uploads.exists():
        job_uploads.mkdir(parents=True)
        actions.append("created job_uploads/（空目录占位，布局与 TenantStoreFactory 期望一致）")
    return actions


def run_stage(source: str | os.PathLike[str], staging: str | os.PathLike[str],
              legacy_tenant_id: str = LEGACY_TENANT_ID,
              report: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """复制白名单到 staging/tenants/<uuid>/；source 原样不动。"""
    src = Path(source).resolve()
    stg = Path(staging).resolve()
    tid = _normalize_tenant_id(legacy_tenant_id)
    _validate_stage_placement(src, stg)
    if not src.is_dir():
        raise MigrationError(f"source 不存在或不是目录: {src}")
    if stg.exists():
        if not stg.is_dir():
            raise MigrationError(f"staging 已存在且不是目录: {stg}")
        if any(stg.iterdir()):
            raise MigrationError(f"staging 已存在且非空（拒绝混合旧内容）: {stg}")

    plan, ignored, missing = _plan_copy(src)
    tenant_dir = stg / "tenants" / tid
    tenant_dir.mkdir(parents=True)
    copied = _copy_plan(plan, tenant_dir)
    finishing = _finish_tenant_dir(tenant_dir)

    result: dict[str, Any] = {
        "tool": TOOL_NAME,
        "action": "stage",
        "ok": True,
        "source": str(src),
        "staging": str(stg),
        "tenant_id": tid,
        "tenant_dir": str(tenant_dir),
        "generated_at": _now_iso(),
        "copied": copied,
        "ignored": ignored,
        "missing_whitelist": missing,
        "finishing": finishing,
        "notes": [
            "source 原样不动（先复制后切换，§5.1.6）",
            "SQLite 经 backup API 快照（WAL 折叠），staged 库以行数对账而非字节",
            "users.db 不迁移：账号并入 control.db 属部署手册事项（§5.1.3）",
        ],
    }
    _write_json_report(report, result)
    return result


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
def _discover_tenant_dir(staging: Path, legacy_tenant_id: str | None) -> tuple[str | None, Path | None, str | None]:
    tenants = staging / "tenants"
    if not tenants.is_dir():
        return legacy_tenant_id, None, f"staging 缺少 tenants/ 目录: {staging}"
    dirs = sorted(p for p in tenants.iterdir() if p.is_dir())
    if legacy_tenant_id:
        try:
            tid = _normalize_tenant_id(legacy_tenant_id)
        except MigrationError as exc:
            return None, None, str(exc)
        target = tenants / tid
        if target not in dirs:
            return tid, None, f"staging/tenants 下没有 {tid}（现有: {[d.name for d in dirs]}）"
        return tid, target, None
    if len(dirs) == 1:
        return dirs[0].name, dirs[0], None
    return None, None, (
        f"staging/tenants 下有 {len(dirs)} 个租户目录且未显式给 legacy-tenant-id，无法确定对账目标"
    )


def _comparable_map(base: Path) -> tuple[dict[str, dict[str, Any]], dict[str, int], list[str]]:
    """→ (files rel→{size,sha256}, dbs name→size, run_names)。

    files 覆盖 run_* / job_uploads 下全部文件 + 顶层 FINISHING_FILES（settings.json、
    mice_registry.json）；顶层白名单 DB 单独走行数对账（backup API 归一化后字节可不同）。
    """
    files: dict[str, dict[str, Any]] = {}
    dbs: dict[str, int] = {}
    runs: list[str] = []
    if not base.is_dir():
        return files, dbs, runs
    for child in sorted(base.iterdir()):
        if child.is_file() and child.name in TOP_LEVEL_DBS:
            dbs[child.name] = child.stat().st_size
            continue
        if child.is_dir() and (child.name.startswith(RUN_DIR_PREFIX) or child.name in WHITELIST_DIR_NAMES):
            if child.name.startswith(RUN_DIR_PREFIX):
                runs.append(child.name)
            for p in sorted(child.rglob("*")):
                if p.is_file():
                    files[p.relative_to(base).as_posix()] = {
                        "size": p.stat().st_size, "sha256": _sha256(p),
                    }
            continue
        if child.is_file() and child.name in FINISHING_FILES:
            files[child.name] = {"size": child.stat().st_size, "sha256": _sha256(child)}
    return files, dbs, runs


def _media_counts(files: dict[str, dict[str, Any]], runs: list[str]) -> dict[str, int]:
    prefixes = tuple(f"{name}/" for name in runs)
    photos = records = videos = 0
    for rel in files:
        if not rel.startswith(prefixes):
            continue
        if rel.endswith("/record.json"):
            records += 1
        ext = rel.rsplit(".", 1)[-1].lower() if "." in rel.rsplit("/", 1)[-1] else ""
        if f".{ext}" in PHOTO_EXTS:
            photos += 1
        elif f".{ext}" in VIDEO_EXTS:
            videos += 1
    return {"records": records, "photos": photos, "videos": videos}


def run_verify(source: str | os.PathLike[str], staging: str | os.PathLike[str],
               report: str | os.PathLike[str] | None = None,
               legacy_tenant_id: str | None = None) -> dict[str, Any]:
    """全量对账 source 与 staging；任何差异 ok=False（CLI 退出码 1），绝不改动两侧。"""
    src = Path(source).resolve()
    stg = Path(staging).resolve()
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any) -> bool:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    ok = True
    ok &= check("source_exists", src.is_dir(), str(src))
    ok &= check("staging_exists", stg.is_dir(), str(stg))

    tenant_dir: Path | None = None
    tid: str | None = legacy_tenant_id
    if stg.is_dir():
        tid, tenant_dir, err = _discover_tenant_dir(stg, legacy_tenant_id)
        ok &= check("tenant_dir_found", tenant_dir is not None, err or str(tenant_dir))
    else:
        ok &= check("tenant_dir_found", False, "staging 不存在")

    src_files: dict[str, dict[str, Any]] = {}
    src_dbs: dict[str, int] = {}
    src_runs: list[str] = []
    stg_files: dict[str, dict[str, Any]] = {}
    stg_dbs: dict[str, int] = {}
    stg_runs: list[str] = []
    if src.is_dir():
        src_files, src_dbs, src_runs = _comparable_map(src)
    if tenant_dir is not None and tenant_dir.is_dir():
        stg_files, stg_dbs, stg_runs = _comparable_map(tenant_dir)

    # 1) run 目录集合
    ok &= check("run_dirs_match", sorted(src_runs) == sorted(stg_runs),
                {"source": sorted(src_runs), "staging": sorted(stg_runs),
                 "missing_in_staging": sorted(set(src_runs) - set(stg_runs)),
                 "unexpected_in_staging": sorted(set(stg_runs) - set(src_runs))})

    # 2) 白名单文件：缺失 / 多余
    #    多余的容忍集 = stage 收尾产物（仅当 source 本来缺失对应项时）。
    src_top = {p.name for p in src.iterdir()} if src.is_dir() else set()
    stg_top = {p.name for p in tenant_dir.iterdir()} if tenant_dir is not None and tenant_dir.is_dir() else set()
    allowed_top = src_top | {"job_uploads"} | {f for f in FINISHING_FILES if f not in src_top}
    unexpected_top = sorted(stg_top - allowed_top)
    allowed_extra_rels: set[str] = set()
    if "job_uploads" not in src_top:
        allowed_extra_rels = {rel for rel in stg_files
                              if rel == "job_uploads" or rel.startswith("job_uploads/")}
    missing_files = sorted(set(src_files) - set(stg_files))
    # stage 收尾产物的容忍集：仅当 source 本来缺失对应项时（settings/registry/job_uploads）。
    allowed_extra_rels: set[str] = {f for f in FINISHING_FILES if f not in src_top}
    if "job_uploads" not in src_top:
        allowed_extra_rels |= {rel for rel in stg_files
                               if rel == "job_uploads" or rel.startswith("job_uploads/")}
    unexpected_files = sorted((set(stg_files) - set(src_files)) - allowed_extra_rels)
    ok &= check("whitelist_files_present", not missing_files,
                {"missing_in_staging": missing_files, "count_source_files": len(src_files)})
    ok &= check("unexpected_staged_files", not unexpected_files and not unexpected_top,
                {"unexpected_top_level": unexpected_top, "unexpected_files": unexpected_files})

    # 3) DB 表集 + 行数
    db_detail: dict[str, Any] = {}
    db_ok = True
    for name in TOP_LEVEL_DBS:
        if name not in src_dbs and name not in stg_dbs:
            continue
        if name not in stg_dbs:
            db_detail[name] = {"error": "staging 缺少该库", "source_size": src_dbs.get(name)}
            db_ok = False
            continue
        if name not in src_dbs:
            db_detail[name] = {"error": "staging 多出该库（source 无）"}
            db_ok = False
            continue
        src_struct = _read_db_structure(src / name)
        stg_struct = _read_db_structure(tenant_dir / name)
        entry: dict[str, Any] = {
            "source": src_struct, "staging": stg_struct,
            "source_size": src_dbs[name], "staging_size": stg_dbs[name],
        }
        if src_struct["errors"] or stg_struct["errors"]:
            entry["error"] = {"source": src_struct["errors"], "staging": stg_struct["errors"]}
            db_ok = False
        if src_struct["tables"] != stg_struct["tables"]:
            entry["row_diff"] = {
                table: {"source": src_struct["tables"].get(table),
                        "staging": stg_struct["tables"].get(table)}
                for table in sorted(set(src_struct["tables"]) | set(stg_struct["tables"]))
                if src_struct["tables"].get(table) != stg_struct["tables"].get(table)
            }
            db_ok = False
        db_detail[name] = entry
    ok &= check("db_row_counts", db_ok, db_detail)

    # 4) record.json SHA-256（显式单列，§15-B7）
    rec_missing = [rel for rel in missing_files if rel.endswith("/record.json")]
    rec_mismatch = []
    rec_compared = 0
    for rel in sorted(set(src_files) & set(stg_files)):
        if not rel.endswith("/record.json"):
            continue
        rec_compared += 1
        if src_files[rel]["sha256"] != stg_files[rel]["sha256"]:
            rec_mismatch.append({
                "file": rel,
                "source_sha256": src_files[rel]["sha256"],
                "staging_sha256": stg_files[rel]["sha256"],
            })
    ok &= check("record_json_sha256", not rec_mismatch and not rec_missing,
                {"compared": rec_compared, "mismatches": rec_mismatch, "missing": rec_missing})

    # 5) 全部非 DB 文件 SHA-256 + 总字节数
    byte_mismatch = []
    total_src = 0
    total_stg = 0
    for rel in sorted(set(src_files) & set(stg_files)):
        total_src += src_files[rel]["size"]
        total_stg += stg_files[rel]["size"]
        if src_files[rel]["sha256"] != stg_files[rel]["sha256"] or \
                src_files[rel]["size"] != stg_files[rel]["size"]:
            byte_mismatch.append({
                "file": rel,
                "source": src_files[rel], "staging": stg_files[rel],
            })
    ok &= check("file_bytes_sha256", not byte_mismatch,
                {"compared": len(set(src_files) & set(stg_files)), "mismatches": byte_mismatch})
    ok &= check("total_bytes", total_src == total_stg,
                {"source": total_src, "staging": total_stg})

    # 6) run / record / photo / video 计数
    src_counts = _media_counts(src_files, src_runs)
    stg_counts = _media_counts(stg_files, stg_runs)
    ok &= check("record_photo_video_counts", src_counts == stg_counts,
                {"source": src_counts, "staging": stg_counts})

    # 7) 重复 ID（两侧各自检测）
    src_dups = _duplicates(src) if src.is_dir() else {"ok": True}
    stg_dups = _duplicates(tenant_dir) if tenant_dir is not None and tenant_dir.is_dir() else {"ok": True}
    ok &= check("duplicate_ids", bool(src_dups.get("ok")) and bool(stg_dups.get("ok")),
                {"source": src_dups, "staging": stg_dups})

    differences = [
        {"check": c["check"], "detail": c["detail"]} for c in checks if not c["ok"]
    ]
    db_counts: dict[str, Any] = {}
    for name in TOP_LEVEL_DBS:
        src_has = (src / name).is_file()
        stg_has = tenant_dir is not None and (tenant_dir / name).is_file()
        src_rows = _read_db_structure(src / name)["tables"].get(MAIN_TABLES[name]) if src_has else None
        stg_rows = (
            _read_db_structure(tenant_dir / name)["tables"].get(MAIN_TABLES[name])
            if stg_has else None
        )
        db_counts[COUNT_MIRRORS[name]] = {"source": src_rows, "staging": stg_rows}
    result: dict[str, Any] = {
        "tool": TOOL_NAME,
        "action": "verify",
        "ok": bool(ok),
        "source": str(src),
        "staging": str(stg),
        "tenant_id": tid,
        "tenant_dir": str(tenant_dir) if tenant_dir else None,
        "verified_at": _now_iso(),
        "verified_at_epoch": time.time(),
        "counts": {
            "runs": {"source": len(src_runs), "staging": len(stg_runs)},
            "records": {"source": src_counts["records"], "staging": stg_counts["records"]},
            "photos": {"source": src_counts["photos"], "staging": stg_counts["photos"]},
            "videos": {"source": src_counts["videos"], "staging": stg_counts["videos"]},
            **db_counts,
        },
        "checks": checks,
        "differences": differences,
    }
    _write_json_report(report, result)
    return result


# --------------------------------------------------------------------------- #
# activate / rollback
# --------------------------------------------------------------------------- #
def _make_readonly(root: Path) -> None:
    """chmod a-w：文件 0444，目录 0555（时间戳备份只读，§15-B7）。"""
    for p in sorted(root.rglob("*"), reverse=True):
        if p.is_dir():
            p.chmod(0o555)
    for p in root.rglob("*"):
        if p.is_file():
            p.chmod(0o444)
    root.chmod(0o555)


def _make_writable(root: Path) -> None:
    """恢复常规可写权限（文件 0644，目录 0755）。"""
    root.chmod(0o755)
    for p in root.rglob("*"):
        if p.is_dir():
            p.chmod(0o755)
        elif p.is_file():
            p.chmod(0o644)


def _newest_mtime(root: Path) -> float | None:
    newest: float | None = None
    for p in root.rglob("*"):
        if p.is_file():
            mtime = p.stat().st_mtime
            if newest is None or mtime > newest:
                newest = mtime
    return newest


def _unique_path(base: Path) -> Path:
    candidate = base
    n = 1
    while candidate.exists():
        n += 1
        candidate = Path(f"{base}-{n}")
    return candidate


def _refuse(result: dict[str, Any], report, reason: str, **extra: Any) -> dict[str, Any]:
    result["ok"] = False
    result["reason"] = reason
    result.update(extra)
    _write_json_report(report, result)
    return result


def run_activate(source: str | os.PathLike[str],
                 staging: str | os.PathLike[str] | None = None,
                 *, confirm: bool = False, rollback: bool = False,
                 report: str | os.PathLike[str] | None = None,
                 legacy_tenant_id: str | None = None,
                 max_instance_idle_seconds: float = DEFAULT_INSTANCE_IDLE_SECONDS) -> dict[str, Any]:
    """激活（或 --rollback 回滚）。除本函数外，其余入口均无生产副作用。"""
    src = Path(source).resolve()
    state_file = src.parent / f"{STATE_FILE_PREFIX}{src.name}.json"
    result: dict[str, Any] = {
        "tool": TOOL_NAME,
        "action": "rollback" if rollback else "activate",
        "source": str(src),
        "generated_at": _now_iso(),
    }
    if not confirm:
        return _refuse(
            result, report, "confirmation_required",
            message="activate 会改写数据根目录；必须显式传 --i-understand-data-loss 确认数据丢失风险",
        )
    if rollback:
        return _run_rollback(src, state_file, result, report)

    if state_file.exists():
        return _refuse(result, report, "already_activated",
                       state_file=str(state_file),
                       message="已存在本工具的激活状态文件（疑似已激活）；如需切回请用 --rollback")
    if (src / "tenants").is_dir():
        return _refuse(result, report, "source_already_tenant_layout",
                       message="source 顶层已有 tenants/ 目录，看起来已是租户布局，拒绝重复激活")
    if staging is None:
        return _refuse(result, report, "staging_required",
                       message="activate 需要 --staging 指向已 stage/verify 通过的兄弟目录")
    stg = Path(staging).resolve()
    try:
        _validate_stage_placement(src, stg)
    except MigrationError as exc:
        return _refuse(result, report, "invalid_staging_placement", message=str(exc))
    if not src.is_dir():
        return _refuse(result, report, "source_missing", message=f"source 不存在: {src}")
    if not stg.is_dir():
        return _refuse(result, report, "staging_missing", message=f"staging 不存在: {stg}")

    # 维护模式检查：source 侧有已运行实例证据（最近 mtime 的 WAL/SHM/锁文件）→ 拒绝
    activity = _instance_activity(src, max_instance_idle_seconds)
    result["instance_activity"] = activity
    if activity["active"]:
        return _refuse(
            result, report, "instance_activity_detected",
            message="检测到运行实例证据（最近写入的 db WAL/SHM/锁文件）；请先停止服务/等待静默后重试",
        )

    # 最终 inventory + verify（三连的最终关）；任一失败绝不 activate
    inventory = run_inventory(src, report=None)
    result["inventory_counts"] = inventory.get("counts")
    verification = run_verify(src, stg, report=None, legacy_tenant_id=legacy_tenant_id)
    result["verify"] = {"ok": verification["ok"], "differences": verification["differences"]}
    if not verification["ok"]:
        return _refuse(result, report, "verify_failed",
                       message="最终 verify 未通过，绝不 activate；请检查 differences 逐项处理后重新 stage/verify")

    tid = verification.get("tenant_id") or LEGACY_TENANT_ID
    ts = time.strftime("%Y%m%dT%H%M%S")
    backup = _unique_path(src.parent / f"{src.name}.pre-tenant-migration-{ts}")
    try:
        os.rename(src, backup)  # 同父目录 rename = 原子
    except OSError as exc:
        return _refuse(result, report, "rename_source_failed", message=f"{type(exc).__name__}: {exc}")
    _make_readonly(backup)
    try:
        os.rename(stg, src)  # staging 原子就位为新根
    except OSError as exc:
        _make_writable(backup)
        os.rename(backup, src)  # 尽力还原
        return _refuse(result, report, "rename_staging_failed", message=f"{type(exc).__name__}: {exc}")

    activated_epoch = time.time()
    state = {
        "tool": TOOL_NAME,
        "activated_at": _now_iso(),
        "activated_at_epoch": activated_epoch,
        "backup": str(backup),
        "staging_used": str(stg),
        "legacy_tenant_id": tid,
        "verify_ok": True,
    }
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    result.update(
        ok=True,
        backup=str(backup),
        activated_at=state["activated_at"],
        state_file=str(state_file),
        rollback_hint=f"如需切回：本工具 activate --source {src} --rollback --i-understand-data-loss"
                      f"（激活后新根若有新写入将被拒绝，需先反向对账）",
    )
    _write_json_report(report, result)
    return result


def _run_rollback(src: Path, state_file: Path, result: dict[str, Any], report) -> dict[str, Any]:
    if not state_file.exists():
        return _refuse(result, report, "state_file_missing",
                       message=f"找不到激活状态文件 {state_file}；本工具没有激活记录，拒绝盲目切回")
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _refuse(result, report, "state_file_unreadable", message=str(exc))
    if state.get("rolled_back_at"):
        return _refuse(result, report, "already_rolled_back",
                       message=f"已于 {state['rolled_back_at']} 回滚过，无再次回滚的激活记录")
    backup = Path(str(state.get("backup", "")))
    if not backup.is_dir():
        return _refuse(result, report, "backup_missing",
                       message=f"只读备份不存在: {backup}")
    if not src.is_dir():
        return _refuse(result, report, "source_missing", message=f"当前新根不存在: {src}")

    # 新写入检测：激活后新根任何文件 mtime 晚于激活时间戳 → 拒绝直接切回。
    # 新根内全部文件的 mtime 都来自 stage（早于激活时刻，rename 不改 mtime），
    # 因此不需要容差：任何 > activated_at_epoch 的 mtime 都是激活后的新写入。
    newest = _newest_mtime(src)
    result["activated_at"] = state.get("activated_at")
    if newest is not None and newest > float(state.get("activated_at_epoch", 0)):
        newest_iso = datetime.fromtimestamp(newest, tz=timezone.utc).isoformat()
        return _refuse(
            result, report, "new_writes_detected",
            newest_write_mtime=newest_iso,
            message=_ROLLOUT_GUIDANCE,
        )

    ts = time.strftime("%Y%m%dT%H%M%S")
    aside = _unique_path(src.parent / f"{src.name}.post-migration-rolledback-{ts}")
    os.rename(src, aside)
    _make_readonly(aside)
    _make_writable(backup)
    os.rename(backup, src)
    state["rolled_back_at"] = _now_iso()
    state["new_root_kept_at"] = str(aside)
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    result.update(
        ok=True,
        restored_from=str(backup),
        new_root_kept_at=str(aside),
        message="已切回旧根（恢复可写）；激活后的新根以只读保留在 new_root_kept_at，供反向对账",
    )
    _write_json_report(report, result)
    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_tenant_storage",
        description="租户存储迁移工具：inventory / stage / verify 无生产副作用；activate 需显式确认 + 维护模式检查（§5.1 / §15-B7 / §16-G5）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_inv = sub.add_parser("inventory", help="只读盘点旧数据根并输出 JSON 报告")
    p_inv.add_argument("--source", required=True, help="旧数据根目录")
    p_inv.add_argument("--report", required=True, help="报告 JSON 输出路径")
    p_inv.add_argument("--max-instance-idle-seconds", type=float,
                       default=DEFAULT_INSTANCE_IDLE_SECONDS,
                       help="运行实例证据判定阈值（秒），默认 300")

    p_stage = sub.add_parser("stage", help="复制 §5.1 白名单到 staging/tenants/<uuid>/（source 不动）")
    p_stage.add_argument("--source", required=True, help="旧数据根目录")
    p_stage.add_argument("--staging", required=True, help="staging 目录（必须是 source 的兄弟目录）")
    p_stage.add_argument("--legacy-tenant-id", default=LEGACY_TENANT_ID,
                         help=f"目标租户 UUID，默认 {LEGACY_TENANT_ID}")
    p_stage.add_argument("--report", help="可选：stage 结果 JSON 输出路径")

    p_verify = sub.add_parser("verify", help="全量对账 source 与 staging；有差异退出码 1")
    p_verify.add_argument("--source", required=True, help="旧数据根目录")
    p_verify.add_argument("--staging", required=True, help="staging 目录")
    p_verify.add_argument("--report", required=True, help="报告 JSON 输出路径")
    p_verify.add_argument("--legacy-tenant-id", default=None,
                          help="目标租户 UUID；staging/tenants 下只有一个目录时可省略")

    p_act = sub.add_parser("activate", help="原子切换 staging 为新数据根（唯一有副作用子命令）")
    p_act.add_argument("--source", required=True, help="数据根目录（激活后成为租户布局新根）")
    p_act.add_argument("--staging", help="staging 目录（必须是 source 的兄弟目录；--rollback 时不需要）")
    p_act.add_argument("--legacy-tenant-id", default=None, help="目标租户 UUID（可选）")
    p_act.add_argument("--i-understand-data-loss", action="store_true",
                       help="显式确认数据丢失风险；缺少该参数一律拒绝")
    p_act.add_argument("--rollback", action="store_true",
                       help="回滚模式：切回激活前的只读备份（检测到新写入会拒绝）")
    p_act.add_argument("--max-instance-idle-seconds", type=float,
                       default=DEFAULT_INSTANCE_IDLE_SECONDS,
                       help="维护模式检查阈值（秒），默认 300")
    p_act.add_argument("--report", help="可选：激活/回滚结果 JSON 输出路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "inventory":
        result = run_inventory(args.source, args.report,
                               max_instance_idle_seconds=args.max_instance_idle_seconds)
        code = 0 if result.get("ok") else 1
    elif args.command == "stage":
        try:
            result = run_stage(args.source, args.staging, args.legacy_tenant_id, args.report)
            code = 0
        except MigrationError as exc:
            result = {"tool": TOOL_NAME, "action": "stage", "ok": False,
                      "reason": "invalid_stage_request", "message": str(exc)}
            code = 1
    elif args.command == "verify":
        result = run_verify(args.source, args.staging, args.report,
                            legacy_tenant_id=args.legacy_tenant_id)
        code = 0 if result.get("ok") else 1
    elif args.command == "activate":
        if not args.rollback and not args.staging:
            parser.error("activate 需要 --staging（--rollback 模式除外）")
        result = run_activate(
            args.source, args.staging,
            confirm=args.i_understand_data_loss, rollback=args.rollback,
            report=args.report, legacy_tenant_id=args.legacy_tenant_id,
            max_instance_idle_seconds=args.max_instance_idle_seconds,
        )
        code = 0 if result.get("ok") else 1
    else:  # pragma: no cover - argparse required=True 已拦截
        parser.error(f"未知子命令: {args.command}")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
