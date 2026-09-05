"""迁移工具契约（合同 §5.1 / §9-10 / §16-G5，占位：红到 B7）。

B7 落地 `tools/migrate_tenant_storage.py`（inventory / stage / verify /
activate 前检查）后本文件转绿；在工具存在前，测试因「缺迁移工具」保持红。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "migrate_tenant_storage.py"
LEGACY_TENANT_ID = "00000000-0000-4000-8000-000000000001"


def _synthetic_legacy_root(root: Path) -> None:
    """生成合成旧布局：boxes/jobs/records_meta/upload_queue db + run_* + 照片。"""
    (root / "run_20260101_demo" / "mouse_001").mkdir(parents=True)
    record = {
        "record_id": "rec-mig-1",
        "cage_id": "C57-001",
        "ordinal": 1,
        "actual_ordinal": 1,
        "weight": 20.5,
        "timestamp": "2026-01-01T08:00:00",
        "run_id": "run_20260101_demo",
    }
    (root / "run_20260101_demo" / "mouse_001" / "record.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    (root / "run_20260101_demo" / "mouse_001" / "photo.jpg").write_bytes(b"jpeg-bytes-1")
    conn = sqlite3.connect(root / "boxes.db")
    try:
        conn.execute(
            "CREATE TABLE boxes (cage_id TEXT PRIMARY KEY, next_ordinal INTEGER NOT NULL DEFAULT 1)"
        )
        conn.execute("INSERT INTO boxes (cage_id, next_ordinal) VALUES ('C57-001', 2)")
        conn.commit()
    finally:
        conn.close()


def test_migration_tool_inventory_stage_verify(tmp_path):
    try:
        from tools.migrate_tenant_storage import (
            run_inventory,
            run_stage,
            run_verify,
        )
    except ImportError as exc:  # B7 之前：缺迁移工具
        raise AssertionError(f"B7 迁移工具尚未实现（缺租户能力）: {exc!r}") from exc

    legacy = tmp_path / "legacy"
    _synthetic_legacy_root(legacy)

    # inventory：只读盘点，输出报告
    inventory_path = tmp_path / "inventory.json"
    report = run_inventory(source=legacy, report=inventory_path)
    assert report["boxes"] == 1 or report["counts"]["boxes"] == 1
    assert inventory_path.exists()

    # stage：复制（不得改动 source），legacy tenant 固定 UUID
    legacy_tenant_id = "00000000-0000-4000-8000-000000000001"
    staging = tmp_path / "v2"
    run_stage(source=legacy, staging=staging, legacy_tenant_id=legacy_tenant_id)
    tenant_dir = staging / "tenants" / legacy_tenant_id
    assert (tenant_dir / "boxes.db").exists()
    assert (tenant_dir / "run_20260101_demo" / "mouse_001" / "photo.jpg").exists()
    # source 必须原样保留（先复制后切换）
    assert (legacy / "boxes.db").exists()

    # verify：数量 / 哈希 / 文件一致性；一致 → 通过
    verify_path = tmp_path / "verify.json"
    result = run_verify(source=legacy, staging=staging, report=verify_path)
    assert result["ok"] is True
    assert verify_path.exists()

    # 破坏照片 → verify 必须失败且报告差异
    (tenant_dir / "run_20260101_demo" / "mouse_001" / "photo.jpg").write_bytes(b"corrupted")
    result = run_verify(
        source=legacy, staging=staging, report=tmp_path / "verify2.json"
    )
    assert result["ok"] is False


# --------------------------------------------------------------------------- #
# B7：迁移工具与可回滚演练（合同 §15-B7 / §16-G5；以下测试自行生成合成数据）
# --------------------------------------------------------------------------- #
IGNORED_ITEMS = (
    "users.db", "audit.db", ".thumbs", "shared", "scale_captures",
    "control", "compare_runs", "scale_sync.db", "notes.txt", ".DS_Store",
)


def _db(path: Path, ddl: str, table: str, rows: list[tuple]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(ddl)
        for row in rows:
            conn.execute(
                f"INSERT INTO {table} VALUES ({','.join('?' * len(row))})", row
            )
        conn.commit()
    finally:
        conn.close()


def _synthetic_rich_root(root: Path) -> None:
    """合成旧布局（§15-B7 演练规格）：≥2 run、多 photo/video、四库多行、白名单外杂项。"""
    root.mkdir(parents=True, exist_ok=True)
    # 四个白名单 DB（多行）
    _db(root / "boxes.db",
        "CREATE TABLE boxes (cage_id TEXT PRIMARY KEY, next_ordinal INTEGER)",
        "boxes",
        [("C57-001", 3), ("C57-002", 1)])
    _db(root / "jobs.db",
        "CREATE TABLE analysis_jobs (job_id TEXT PRIMARY KEY, status TEXT)",
        "analysis_jobs",
        [("job-1", "done"), ("job-2", "done"), ("job-3", "queued")])
    _db(root / "records_meta.db",
        "CREATE TABLE records_meta (record_id TEXT PRIMARY KEY, status TEXT)",
        "records_meta",
        [("rec-a-1", "verified"), ("rec-b-1", "pending")])
    _db(root / "upload_queue.db",
        "CREATE TABLE upload_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT, status TEXT)",
        "upload_queue",
        [(1, "rec-a-1", "pending"), (2, "rec-b-1", "pending"), (3, "rec-a-2", "done")])

    (root / "settings.json").write_text(json.dumps({"project_id": "demo"}), encoding="utf-8")
    (root / "mice_registry.json").write_text(
        json.dumps({"active_run_id": "run_20260101_alpha", "active_run_dir": "run_20260101_alpha"}),
        encoding="utf-8",
    )

    # run 1：两个 record 目录（照片 + 视频 + curve.json）
    rec1 = root / "run_20260101_alpha" / "mouse_001"
    rec1.mkdir(parents=True)
    (rec1 / "record.json").write_text(
        json.dumps({"record_id": "rec-a-1", "cage_id": "C57-001", "weight": 21.5}), encoding="utf-8")
    (rec1 / "photo.jpg").write_bytes(b"jpeg-alpha-1-bytes")
    (rec1 / "video.mp4").write_bytes(b"mp4-alpha-1-bytes")
    rec2 = root / "run_20260101_alpha" / "mouse_002"
    rec2.mkdir(parents=True)
    (rec2 / "record.json").write_text(
        json.dumps({"record_id": "rec-a-2", "cage_id": "C57-001", "weight": 22.5}), encoding="utf-8")
    (rec2 / "photo.jpg").write_bytes(b"jpeg-alpha-2-bytes")

    # run 2：时间戳命名 record 目录（真实布局形态）+ 嵌套子目录
    rec3 = root / "run_20260102_beta" / "20260102_090000_000_C57-002_beef" / "extra"
    rec3.mkdir(parents=True)
    (rec3.parent / "record.json").write_text(
        json.dumps({"record_id": "rec-b-1", "cage_id": "C57-002", "weight": 19.0}), encoding="utf-8")
    (rec3.parent / "photo.jpg").write_bytes(b"jpeg-beta-1-bytes")
    (rec3.parent / "video.mp4").write_bytes(b"mp4-beta-1-bytes")
    (rec3 / "curve.json").write_text("{}", encoding="utf-8")

    # job_uploads（白名单目录）
    up = root / "job_uploads" / "job-1"
    up.mkdir(parents=True)
    (up / "upload.bin").write_bytes(b"upload-bytes")

    # 白名单外条目（ignored 逐项带原因；不会被复制）
    (root / "users.db").write_bytes(b"sqlite-users")
    (root / "audit.db").write_bytes(b"sqlite-audit")
    (root / "scale_sync.db").write_bytes(b"sqlite-scale")
    (root / "notes.txt").write_text("junk", encoding="utf-8")
    (root / ".DS_Store").write_bytes(b"junk")
    for name in (".thumbs", "shared", "scale_captures", "control", "compare_runs"):
        (root / name).mkdir()
        (root / name / "keep.txt").write_text("x", encoding="utf-8")


def _stage_rich(tmp_path: Path):
    from tools.migrate_tenant_storage import run_stage

    legacy = tmp_path / "legacy"
    _synthetic_rich_root(legacy)
    staging = tmp_path / "v2"
    stage = run_stage(source=legacy, staging=staging, legacy_tenant_id=LEGACY_TENANT_ID)
    return legacy, staging, staging / "tenants" / LEGACY_TENANT_ID, stage


def test_stage_rejects_nested_and_non_sibling_staging(tmp_path):
    from tools.migrate_tenant_storage import MigrationError, run_stage

    legacy = tmp_path / "legacy"
    _synthetic_rich_root(legacy)
    legacy_boxes = (legacy / "boxes.db").read_bytes()

    # staging 放 source 内部 → 递归复制风险，必须拒绝
    with pytest.raises(MigrationError):
        run_stage(source=legacy, staging=legacy / "staging", legacy_tenant_id=LEGACY_TENANT_ID)
    assert not (legacy / "staging" / "tenants").exists()

    # staging 放 source 的深层子目录同样拒绝
    with pytest.raises(MigrationError):
        run_stage(source=legacy, staging=legacy / "run_x" / "v2", legacy_tenant_id=LEGACY_TENANT_ID)

    # source 在 staging 内部（staging=父目录）也拒绝
    with pytest.raises(MigrationError):
        run_stage(source=legacy, staging=tmp_path, legacy_tenant_id=LEGACY_TENANT_ID)

    # 非兄弟目录（不同父目录）拒绝
    other_parent = tmp_path / "elsewhere"
    other_parent.mkdir()
    with pytest.raises(MigrationError):
        run_stage(source=legacy, staging=other_parent / "v2", legacy_tenant_id=LEGACY_TENANT_ID)

    # 非法 tenant id 拒绝
    with pytest.raises(MigrationError):
        run_stage(source=legacy, staging=tmp_path / "v2", legacy_tenant_id="../evil")

    # source 必须原样不动
    assert (legacy / "boxes.db").read_bytes() == legacy_boxes
    assert not (tmp_path / "v2").exists()


def test_inventory_and_stage_report_ignored_items_and_counts(tmp_path):
    from tools.migrate_tenant_storage import run_inventory

    legacy, staging, tenant_dir, stage = _stage_rich(tmp_path)

    # inventory：只读盘点 + 计数（兼容顶层镜像与 counts 双读法）
    report_path = tmp_path / "inventory.json"
    inv = run_inventory(source=legacy, report=report_path)
    assert inv["counts"]["boxes"] == 2
    assert inv["counts"]["jobs"] == 3
    assert inv["counts"]["records_meta"] == 2
    assert inv["counts"]["upload_queue"] == 3
    assert inv["counts"]["runs"] == 2
    assert inv["counts"]["records"] == 3
    assert inv["counts"]["photos"] == 3
    assert inv["counts"]["videos"] == 2
    assert inv["boxes"] == 2  # 顶层镜像
    assert report_path.exists()

    # ignored 逐项带原因（不能宽泛 glob 静默跳过）
    ignored = {entry["name"]: entry["reason"] for entry in inv["ignored"]}
    for name in IGNORED_ITEMS:
        assert name in ignored, f"白名单外条目未列入报告: {name}"
        assert ignored[name].strip()
    assert inv["duplicates"]["ok"] is True
    assert inv["instance_activity"]["active"] is False

    # stage：白名单复制 + source 原样 + ignored 透传
    assert (tenant_dir / "boxes.db").is_file()
    assert (tenant_dir / "jobs.db").is_file()
    assert (tenant_dir / "records_meta.db").is_file()
    assert (tenant_dir / "upload_queue.db").is_file()
    assert (tenant_dir / "settings.json").is_file()
    assert (tenant_dir / "mice_registry.json").is_file()
    assert (tenant_dir / "run_20260101_alpha" / "mouse_001" / "video.mp4").is_file()
    assert (tenant_dir / "job_uploads" / "job-1" / "upload.bin").is_file()
    assert (legacy / "boxes.db").is_file()  # source 原样
    copied_names = {entry["name"] for entry in stage["copied"]}
    assert {"boxes.db", "jobs.db", "records_meta.db", "upload_queue.db",
            "settings.json", "mice_registry.json",
            "run_20260101_alpha", "run_20260102_beta", "job_uploads"} <= copied_names
    assert not ({"users.db", "audit.db"} & copied_names)
    assert {entry["name"] for entry in stage["ignored"]} >= set(IGNORED_ITEMS)

    # staged 库行数与 source 一致
    conn = sqlite3.connect(tenant_dir / "upload_queue.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM upload_queue").fetchone()[0] == 3
    finally:
        conn.close()


def test_verify_detects_db_row_delete_record_json_tamper_missing_and_duplicates(tmp_path):
    from tools.migrate_tenant_storage import run_verify

    # 基线：stage 后 verify 通过
    legacy, staging, tenant_dir, _stage = _stage_rich(tmp_path)
    ok_report = tmp_path / "verify-ok.json"
    assert run_verify(source=legacy, staging=staging, report=ok_report)["ok"] is True
    assert ok_report.exists()

    # 破坏 1：staged 库删一行 → 非零差异（行数）
    conn = sqlite3.connect(tenant_dir / "records_meta.db")
    try:
        conn.execute("DELETE FROM records_meta WHERE record_id = 'rec-b-1'")
        conn.commit()
    finally:
        conn.close()
    result = run_verify(source=legacy, staging=staging, report=tmp_path / "v-db.json")
    assert result["ok"] is False
    diff_checks = {d["check"] for d in result["differences"]}
    assert "db_row_counts" in diff_checks

    # 破坏 2：record.json 同长度内容篡改 → SHA-256 必须抓住
    legacy2, staging2, tenant_dir2, _ = _stage_rich(tmp_path / "case2")
    rec = tenant_dir2 / "run_20260101_alpha" / "mouse_002" / "record.json"
    original = json.loads(rec.read_text(encoding="utf-8"))
    original["weight"] = 999.99  # 只改内容，刻意制造同长度差异之外的变化
    rec.write_text(json.dumps(original), encoding="utf-8")
    result = run_verify(source=legacy2, staging=staging2, report=tmp_path / "v-json.json")
    assert result["ok"] is False
    assert "record_json_sha256" in {d["check"] for d in result["differences"]}

    # 破坏 3：删除 staged 照片 → 缺失文件清单
    legacy3, staging3, tenant_dir3, _ = _stage_rich(tmp_path / "case3")
    (tenant_dir3 / "run_20260101_alpha" / "mouse_001" / "photo.jpg").unlink()
    result = run_verify(source=legacy3, staging=staging3, report=tmp_path / "v-missing.json")
    assert result["ok"] is False
    diff = next(d for d in result["differences"] if d["check"] == "whitelist_files_present")
    assert any("photo.jpg" in rel for rel in diff["detail"]["missing_in_staging"])

    # 破坏 4：source 自身重复 record_id → 重复 ID 检测
    legacy4 = tmp_path / "case4" / "legacy"
    _synthetic_rich_root(legacy4)
    dup_dir = legacy4 / "run_20260103_gamma" / "mouse_009"
    dup_dir.mkdir(parents=True)
    (dup_dir / "record.json").write_text(
        json.dumps({"record_id": "rec-a-1", "cage_id": "C57-001"}), encoding="utf-8")
    (dup_dir / "photo.jpg").write_bytes(b"dup-photo")
    staging4 = tmp_path / "case4" / "v2"
    from tools.migrate_tenant_storage import run_stage

    run_stage(source=legacy4, staging=staging4, legacy_tenant_id=LEGACY_TENANT_ID)
    result = run_verify(source=legacy4, staging=staging4, report=tmp_path / "v-dup.json")
    assert result["ok"] is False
    dup_detail = next(d for d in result["differences"] if d["check"] == "duplicate_ids")
    assert "rec-a-1" in dup_detail["detail"]["source"]["record_ids"]


def test_activate_guards_atomic_switch_and_rollback(tmp_path):
    from tools.migrate_tenant_storage import run_activate

    legacy, staging, tenant_dir, _ = _stage_rich(tmp_path)
    source = legacy  # activate 后 source 就位为新根

    # 防护 1：无显式确认参数 → 拒绝，两侧原样
    result = run_activate(source, staging, confirm=False)
    assert result["ok"] is False and result["reason"] == "confirmation_required"
    assert (source / "boxes.db").is_file() and staging.is_dir()

    # 防护 2：维护模式检查——source 侧出现最近 mtime 的 db WAL → 拒绝
    wal = source / "boxes.db-wal"
    wal.write_bytes(b"pending-wal")
    os.utime(wal, (time.time(), time.time()))
    result = run_activate(source, staging, confirm=True)
    assert result["ok"] is False and result["reason"] == "instance_activity_detected"
    assert any(e["file"] == "boxes.db-wal" for e in result["instance_activity"]["evidence"])
    wal.unlink()

    # 正常激活：旧根原子改名只读备份，staging 就位为新根
    result = run_activate(source, staging, confirm=True)
    assert result["ok"] is True, result
    backup = Path(result["backup"])
    assert source.is_dir() and (source / "tenants" / LEGACY_TENANT_ID / "boxes.db").is_file()
    assert not (source / "users.db").exists()  # ignored 项不进新根
    assert not staging.exists()  # staging 已就位
    assert (backup / "boxes.db").is_file()  # 旧根完整保留
    assert (backup.stat().st_mode & 0o222) == 0  # 备份根目录只读
    assert ((backup / "boxes.db").stat().st_mode & 0o222) == 0  # 备份文件只读
    state_file = Path(result["state_file"])
    assert state_file.exists()
    state = json.loads(state_file.read_text(encoding="utf-8"))

    # 防护 3：重复激活拒绝
    result = run_activate(source, staging, confirm=True)
    assert result["ok"] is False and result["reason"] == "already_activated"

    # 防护 4：激活后新根有新写入 → --rollback 拒绝并给出反向对账指引
    new_run = source / "tenants" / LEGACY_TENANT_ID / "run_20260104_new" / "mouse_009"
    new_run.mkdir(parents=True)
    (new_run / "record.json").write_text(
        json.dumps({"record_id": "rec-new-1"}), encoding="utf-8")
    result = run_activate(source, rollback=True, confirm=True)
    assert result["ok"] is False and result["reason"] == "new_writes_detected"
    assert "反向对账" in result["message"] and "不做自动合并" in result["message"]
    assert backup.is_dir() and (source / "tenants").is_dir()  # 两侧都没动

    # 清掉新写入后回滚成功：旧根恢复为可写数据根，新根只读留档
    import shutil as _shutil

    _shutil.rmtree(new_run.parent)
    result = run_activate(source, rollback=True, confirm=True)
    assert result["ok"] is True, result
    assert (source / "boxes.db").is_file() and not (source / "tenants").exists()
    assert (source.stat().st_mode & 0o222) != 0  # 旧根恢复可写
    assert ((source / "boxes.db").stat().st_mode & 0o222) != 0
    kept = Path(result["new_root_kept_at"])
    assert (kept / "tenants" / LEGACY_TENANT_ID / "boxes.db").is_file()
    assert (kept.stat().st_mode & 0o222) == 0  # 卸任新根只读留档
    state_after = json.loads(state_file.read_text(encoding="utf-8"))
    assert state_after["rolled_back_at"]
    # 再次回滚拒绝（已无激活态）
    result = run_activate(source, rollback=True, confirm=True)
    assert result["ok"] is False and result["reason"] == "already_rolled_back"
    assert state["backup"] == str(backup)


def _run_cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL_PATH), *argv],
        capture_output=True, text=True, timeout=120,
        cwd=str(TOOL_PATH.parents[1]),
    )


def test_cli_g5_commands_end_to_end(tmp_path):
    """G5 三命令形态 + 破坏性场景 + activate 无确认拒绝（程序化演练）。"""
    for round_no in (1, 2):
        root = tmp_path / f"round{round_no}"
        root.mkdir()
        legacy = root / "legacy"
        _synthetic_rich_root(legacy)
        staging = root / "v2"

        # G5-1 inventory
        proc = _run_cli("inventory", "--source", str(legacy), "--report", str(root / "inventory.json"))
        assert proc.returncode == 0, proc.stderr
        inv = json.loads((root / "inventory.json").read_text(encoding="utf-8"))
        assert inv["counts"]["boxes"] == 2

        # G5-2 stage
        proc = _run_cli("stage", "--source", str(legacy), "--staging", str(staging),
                        "--legacy-tenant-id", LEGACY_TENANT_ID)
        assert proc.returncode == 0, proc.stderr
        assert (staging / "tenants" / LEGACY_TENANT_ID / "boxes.db").is_file()

        # G5-3 verify
        proc = _run_cli("verify", "--source", str(legacy), "--staging", str(staging),
                        "--report", str(root / "verify.json"))
        assert proc.returncode == 0, proc.stderr
        assert json.loads((root / "verify.json").read_text(encoding="utf-8"))["ok"] is True

        # 破坏性场景：改 staged 照片字节 → verify 非零退出
        tenant_dir = staging / "tenants" / LEGACY_TENANT_ID
        (tenant_dir / "run_20260101_alpha" / "mouse_001" / "photo.jpg").write_bytes(b"tampered")
        proc = _run_cli("verify", "--source", str(legacy), "--staging", str(staging),
                        "--report", str(root / "verify-bad.json"))
        assert proc.returncode != 0
        bad = json.loads((root / "verify-bad.json").read_text(encoding="utf-8"))
        assert bad["ok"] is False

        # staging 放 source 内部 → stage 拒绝（非零退出）
        proc = _run_cli("stage", "--source", str(legacy),
                        "--staging", str(legacy / "inner"), "--legacy-tenant-id", LEGACY_TENANT_ID)
        assert proc.returncode != 0
        assert "递归复制" in proc.stdout

        # activate 无确认参数 → 拒绝（非零退出），不发生任何切换
        proc = _run_cli("activate", "--source", str(legacy), "--staging", str(staging))
        assert proc.returncode != 0
        assert json.loads(proc.stdout)["reason"] == "confirmation_required"
        assert (legacy / "boxes.db").is_file()  # 旧根未动
        assert staging.exists()  # staging 未被消费
