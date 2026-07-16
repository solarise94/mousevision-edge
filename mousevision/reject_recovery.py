"""Crash-safe reject-suspect: durable journal + startup recovery.

Reject spans multiple non-atomic steps (rename → rmtree → queue → metadata).
A process crash between any of them must not leave an unrecoverable state
where the operator cannot retry. The journal records per-item phase so
startup (or a re-entry) can finish the work.
"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from mousevision.run import atomic_write_text

REJECT_JOURNAL_NAME = ".reject_journal.json"
REJECT_DIR_PREFIX = ".rejecting_"

# Phase order (monotone): later phases never roll back to earlier ones.
# planned → quarantined → disk_gone → queue_gone → done
PHASE_PLANNED = "planned"
PHASE_QUARANTINED = "quarantined"
PHASE_DISK_GONE = "disk_gone"
PHASE_QUEUE_GONE = "queue_gone"
PHASE_DONE = "done"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def reject_journal_path(run_dir: Path) -> Path:
    return Path(run_dir) / REJECT_JOURNAL_NAME


def load_reject_journal(run_dir: Path) -> dict[str, Any] | None:
    path = reject_journal_path(run_dir)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def save_reject_journal(run_dir: Path, journal: dict[str, Any]) -> None:
    atomic_write_text(
        reject_journal_path(run_dir),
        json.dumps(journal, indent=2, ensure_ascii=False),
    )


def clear_reject_journal(run_dir: Path) -> None:
    reject_journal_path(run_dir).unlink(missing_ok=True)


def new_reject_journal(*, run_id: str, actor: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "actor": actor,
        "started_at": _now(),
        "items": [],
    }


def append_reject_item(
    journal: dict[str, Any],
    *,
    original_name: str,
    quarantine_name: str,
    record_id: str,
    actor: str = "",
) -> dict[str, Any]:
    item = {
        "original_name": original_name,
        "quarantine_name": quarantine_name,
        "record_id": record_id,
        "actor": actor or str(journal.get("actor") or "system"),
        "phase": PHASE_PLANNED,
        "updated_at": _now(),
    }
    journal.setdefault("items", []).append(item)
    return item


def set_item_phase(item: dict[str, Any], phase: str) -> None:
    item["phase"] = phase
    item["updated_at"] = _now()


def make_quarantine_name(original_name: str) -> str:
    return f"{REJECT_DIR_PREFIX}{original_name}_{uuid.uuid4().hex[:8]}"


def parse_original_from_quarantine_name(quarantine_name: str) -> str | None:
    """Recover ``mouse_NNN`` from ``.rejecting_mouse_NNN_<8hex>``."""
    name = str(quarantine_name or "")
    if not name.startswith(REJECT_DIR_PREFIX):
        return None
    rest = name[len(REJECT_DIR_PREFIX) :]
    if "_" not in rest:
        return None
    original, suffix = rest.rsplit("_", 1)
    if len(suffix) != 8 or any(c not in "0123456789abcdef" for c in suffix.lower()):
        return None
    if not original.startswith("mouse_"):
        return None
    return original


def _call_mark_meta(
    mark_meta_deleted: Callable[..., None] | None,
    record_id: str,
    *,
    operator: str,
) -> None:
    """Invoke metadata callback with operator when supported."""
    if not record_id or mark_meta_deleted is None:
        return
    try:
        mark_meta_deleted(record_id, operator=operator)
    except TypeError:
        mark_meta_deleted(record_id)


def resolve_record_id(
    mouse_dir: Path,
    *,
    upload_queue: Any = None,
) -> str:
    """Return a reliable record_id from record.json or queue path lookup.

    Fail-closed: empty string means identity cannot be established.
    """
    mouse_dir = Path(mouse_dir)
    rec_path = mouse_dir / "record.json"
    if rec_path.exists():
        try:
            raw = json.loads(rec_path.read_text(encoding="utf-8"))
            rid = str(raw.get("record_id") or "")
            if rid:
                return rid
        except Exception:
            pass
    if upload_queue is not None and hasattr(upload_queue, "find_record_id_by_path"):
        try:
            found = upload_queue.find_record_id_by_path(rec_path)
            if found:
                return str(found)
        except Exception:
            pass
        try:
            found = upload_queue.find_record_id_by_path(mouse_dir)
            if found:
                return str(found)
        except Exception:
            pass
    return ""


def finish_reject_item(
    run_dir: Path,
    item: dict[str, Any],
    *,
    upload_queue: Any = None,
    mark_meta_deleted: Callable[..., None] | None = None,
    operator: str = "system",
) -> str:
    """Advance one journal item as far as possible. Returns final phase.

    Fail-closed rules:
      - ``queue_gone`` without ``mark_meta_deleted`` stays at queue_gone (do not
        fake-complete metadata when AnalysisJobManager.start has no callback).
      - ``disk_gone`` without ``upload_queue`` stays at disk_gone when a record_id
        is present.
      - ``quarantined`` + original exists + quarantine missing = rolled-back
        rmtree failure: mark done WITHOUT deleting queue/meta.
      - Empty ``record_id`` never advances past planned/quarantined into delete
        of queue (identity missing); if disk already gone, stay at disk_gone.
    """
    run_dir = Path(run_dir)
    phase = str(item.get("phase") or PHASE_PLANNED)
    rid = str(item.get("record_id") or "")
    op = str(item.get("actor") or operator or "system")
    q_name = str(item.get("quarantine_name") or "")
    o_name = str(item.get("original_name") or "")
    quarantine = run_dir / q_name if q_name else None
    original = run_dir / o_name if o_name else None
    q_exists = quarantine is not None and quarantine.exists()
    o_exists = original is not None and original.exists()

    if phase == PHASE_DONE:
        return PHASE_DONE

    # --- planned / quarantined: resolve disk state first ---
    if phase in (PHASE_PLANNED, PHASE_QUARANTINED):
        if q_exists:
            # Without a record_id, refuse to destroy the quarantine — restore
            # to original if possible so evidence is not lost.
            if not rid:
                if original is not None and not original.exists():
                    try:
                        quarantine.rename(original)  # type: ignore[union-attr]
                    except Exception:
                        return phase
                set_item_phase(item, PHASE_DONE)
                return PHASE_DONE
            try:
                shutil.rmtree(quarantine)  # type: ignore[arg-type]
            except Exception:
                return phase
            if quarantine is not None and quarantine.exists():
                return phase
            # Quarantine successfully removed → disk committed.
            set_item_phase(item, PHASE_DISK_GONE)
            phase = PHASE_DISK_GONE
        else:
            # Quarantine absent.
            if o_exists:
                # Original still present:
                # - planned: never renamed (abandoned intent)
                # - quarantined: rmtree failed and was renamed back; journal
                #   item removal did not land. MUST NOT delete queue.
                set_item_phase(item, PHASE_DONE)
                return PHASE_DONE
            # Neither original nor quarantine: delete already committed
            # (rmtree succeeded; crash before phase advance).
            if not rid:
                # Cannot finish queue/meta without identity — leave journal
                # at a sticky phase for operator inspection.
                set_item_phase(item, PHASE_DISK_GONE)
                return PHASE_DISK_GONE
            set_item_phase(item, PHASE_DISK_GONE)
            phase = PHASE_DISK_GONE

    if phase == PHASE_DISK_GONE:
        if not rid:
            return phase
        if upload_queue is None:
            # Cannot prove queue deletion without a queue handle.
            return phase
        try:
            upload_queue.delete_by_record_id(rid)
        except Exception:
            return phase
        set_item_phase(item, PHASE_QUEUE_GONE)
        phase = PHASE_QUEUE_GONE

    if phase == PHASE_QUEUE_GONE:
        if not rid:
            return phase
        if mark_meta_deleted is None:
            # Do not mark done when metadata callback is missing — lifespan
            # may still need to finish this item on a later pass.
            return phase
        try:
            _call_mark_meta(mark_meta_deleted, rid, operator=op)
        except Exception:
            return phase
        set_item_phase(item, PHASE_DONE)
        return PHASE_DONE

    return phase


def _ensure_journal(run_dir: Path, *, run_id: str = "", actor: str = "system") -> dict[str, Any]:
    existing = load_reject_journal(run_dir)
    if existing is not None:
        return existing
    journal = new_reject_journal(run_id=run_id or run_dir.name, actor=actor)
    save_reject_journal(run_dir, journal)
    return journal


def _adopt_orphan_quarantine(
    run_dir: Path,
    quarantine: Path,
    *,
    upload_queue: Any = None,
    mark_meta_deleted: Callable[..., None] | None = None,
    default_actor: str = "system",
) -> str:
    """Journalize an untracked ``.rejecting_*`` dir, then finish or restore.

    Returns one of: ``finished``, ``restored``, ``error``.
    """
    run_dir = Path(run_dir)
    quarantine = Path(quarantine)
    original_name = parse_original_from_quarantine_name(quarantine.name)
    rid = resolve_record_id(quarantine, upload_queue=upload_queue)

    # No reliable identity → restore to mouse_* for operator retry (safer
    # than deleting evidence).
    if not rid or not original_name:
        if original_name:
            target = run_dir / original_name
            if not target.exists():
                try:
                    quarantine.rename(target)
                    return "restored"
                except Exception:
                    return "error"
        return "error"

    journal = _ensure_journal(run_dir, actor=default_actor)
    # Avoid double-tracking the same quarantine name.
    for existing in journal.get("items") or []:
        if (
            isinstance(existing, dict)
            and str(existing.get("quarantine_name") or "") == quarantine.name
        ):
            item = existing
            break
    else:
        item = append_reject_item(
            journal,
            original_name=original_name,
            quarantine_name=quarantine.name,
            record_id=rid,
            actor=str(journal.get("actor") or default_actor),
        )
        set_item_phase(item, PHASE_QUARANTINED)
        save_reject_journal(run_dir, journal)

    op = str(item.get("actor") or journal.get("actor") or default_actor)
    after = finish_reject_item(
        run_dir,
        item,
        upload_queue=upload_queue,
        mark_meta_deleted=mark_meta_deleted,
        operator=op,
    )
    # Persist phase updates from finish_reject_item.
    remaining = [
        i for i in (journal.get("items") or [])
        if isinstance(i, dict) and i.get("phase") != PHASE_DONE
    ]
    if remaining:
        journal["items"] = remaining
        save_reject_journal(run_dir, journal)
    else:
        clear_reject_journal(run_dir)
    return "finished" if after == PHASE_DONE else "error"


def recover_reject_state(
    output_root: str | Path,
    *,
    upload_queue: Any = None,
    mark_meta_deleted: Callable[..., None] | None = None,
    default_actor: str = "system",
    run_dirs: list[Path] | None = None,
) -> dict[str, int]:
    """Scan runs for reject journals and orphan ``.rejecting_*`` dirs; finish them.

    Each run directory is processed under ``run_dir_lock`` so recovery cannot
    race with release/reject endpoints. Metadata updates use the journal item's
    original ``actor`` when present.

    Returns counters: journals, items_finished, orphans_removed, orphans_restored, errors.
    """
    from mousevision.run_lock import run_dir_lock

    root = Path(output_root)
    stats = {
        "journals": 0,
        "items_finished": 0,
        "orphans_removed": 0,
        "orphans_restored": 0,
        "errors": 0,
    }
    if run_dirs is None:
        if not root.is_dir():
            return stats
        candidates = sorted(p for p in root.glob("run_*") if p.is_dir())
    else:
        candidates = [Path(p) for p in run_dirs if Path(p).is_dir()]

    for run_dir in candidates:
        try:
            with run_dir_lock(run_dir):
                _recover_one_run(
                    run_dir,
                    stats,
                    upload_queue=upload_queue,
                    mark_meta_deleted=mark_meta_deleted,
                    default_actor=default_actor,
                )
        except Exception:
            stats["errors"] += 1

    return stats


def _recover_one_run(
    run_dir: Path,
    stats: dict[str, int],
    *,
    upload_queue: Any = None,
    mark_meta_deleted: Callable[..., None] | None = None,
    default_actor: str = "system",
) -> None:
    journal = load_reject_journal(run_dir)
    known_quarantines: set[str] = set()
    if journal is not None:
        stats["journals"] += 1
        journal_actor = str(journal.get("actor") or default_actor)
        items = list(journal.get("items") or [])
        changed = False
        for item in items:
            if not isinstance(item, dict):
                continue
            qn = str(item.get("quarantine_name") or "")
            if qn:
                known_quarantines.add(qn)
            before = item.get("phase")
            op = str(item.get("actor") or journal_actor or default_actor)
            try:
                after = finish_reject_item(
                    run_dir,
                    item,
                    upload_queue=upload_queue,
                    mark_meta_deleted=mark_meta_deleted,
                    operator=op,
                )
            except Exception:
                stats["errors"] += 1
                continue
            if after != before:
                changed = True
            if after == PHASE_DONE and before != PHASE_DONE:
                stats["items_finished"] += 1
        remaining = [
            i for i in items
            if isinstance(i, dict) and i.get("phase") != PHASE_DONE
        ]
        if remaining:
            journal["items"] = remaining
            journal["updated_at"] = _now()
            try:
                save_reject_journal(run_dir, journal)
            except Exception:
                stats["errors"] += 1
        else:
            if changed or journal.get("items"):
                try:
                    clear_reject_journal(run_dir)
                except Exception:
                    stats["errors"] += 1

    # Orphan quarantine dirs: journalize first (never rmtree without a plan).
    for quarantine in sorted(run_dir.glob(f"{REJECT_DIR_PREFIX}*")):
        if not quarantine.is_dir():
            continue
        if quarantine.name in known_quarantines:
            continue
        try:
            result = _adopt_orphan_quarantine(
                run_dir,
                quarantine,
                upload_queue=upload_queue,
                mark_meta_deleted=mark_meta_deleted,
                default_actor=default_actor,
            )
        except Exception:
            stats["errors"] += 1
            continue
        if result == "finished":
            stats["orphans_removed"] += 1
        elif result == "restored":
            stats["orphans_restored"] += 1
        else:
            if not quarantine.exists() and load_reject_journal(run_dir) is not None:
                stats["orphans_removed"] += 1
            else:
                stats["errors"] += 1


def reject_mouse_dir(
    run_dir: Path,
    mouse_dir: Path,
    *,
    journal: dict[str, Any],
    upload_queue: Any = None,
    mark_meta_deleted: Callable[..., None] | None = None,
) -> None:
    """Reject one mouse_* directory using the durable journal protocol.

    Requires a reliable record_id (from record.json or queue path lookup)
    *before* any rename/rmtree. Raises on failure after best-effort journal
    update so callers can surface 500 while leaving enough state for recovery.
    """
    run_dir = Path(run_dir)
    mouse_dir = Path(mouse_dir)
    original_name = mouse_dir.name
    rid = resolve_record_id(mouse_dir, upload_queue=upload_queue)
    if not rid:
        raise RuntimeError(
            f"{original_name}: 无法取得 record_id（record.json 缺失/损坏且 "
            f"queue 无法反查），拒绝删除以保留目录与队列"
        )

    actor = str(journal.get("actor") or "system")
    quarantine_name = make_quarantine_name(original_name)
    item = append_reject_item(
        journal,
        original_name=original_name,
        quarantine_name=quarantine_name,
        record_id=rid,
        actor=actor,
    )
    save_reject_journal(run_dir, journal)

    quarantine = run_dir / quarantine_name
    try:
        mouse_dir.rename(quarantine)
        set_item_phase(item, PHASE_QUARANTINED)
        save_reject_journal(run_dir, journal)
    except Exception:
        # Rename failed — remove planned item so journal does not block.
        journal["items"] = [
            i for i in journal.get("items", []) if i is not item
        ]
        if journal["items"]:
            save_reject_journal(run_dir, journal)
        else:
            clear_reject_journal(run_dir)
        raise

    try:
        shutil.rmtree(quarantine)
    except Exception as exc:
        # Restore original name for operator retry. Journal item is dropped only
        # AFTER a successful rename-back; if we crash between rename-back and
        # journal clear, recovery treats
        # quarantined + original exists + quarantine missing as rolled-back.
        if quarantine.exists() and not mouse_dir.exists():
            try:
                quarantine.rename(mouse_dir)
            except Exception:
                pass
        if mouse_dir.exists() and not quarantine.exists():
            journal["items"] = [
                i for i in journal.get("items", []) if i is not item
            ]
            if journal["items"]:
                save_reject_journal(run_dir, journal)
            else:
                clear_reject_journal(run_dir)
        raise RuntimeError(f"rmtree failed: {exc}") from exc

    if quarantine.exists():
        if not mouse_dir.exists():
            try:
                quarantine.rename(mouse_dir)
            except Exception:
                pass
        if mouse_dir.exists() and not quarantine.exists():
            journal["items"] = [
                i for i in journal.get("items", []) if i is not item
            ]
            if journal["items"]:
                save_reject_journal(run_dir, journal)
            else:
                clear_reject_journal(run_dir)
        raise RuntimeError(f"{original_name}: 目录删除后仍存在")

    set_item_phase(item, PHASE_DISK_GONE)
    save_reject_journal(run_dir, journal)

    # From here, disk is gone — never restore. Finish queue/meta; recovery
    # will complete if we crash mid-way.
    if upload_queue is None:
        raise RuntimeError(f"{original_name}: queue handle missing after disk delete")
    try:
        upload_queue.delete_by_record_id(rid)
        set_item_phase(item, PHASE_QUEUE_GONE)
        save_reject_journal(run_dir, journal)
    except Exception as exc:
        raise RuntimeError(f"{original_name}: queue delete failed: {exc}") from exc

    if mark_meta_deleted is None:
        raise RuntimeError(
            f"{original_name}: metadata callback missing after queue delete"
        )
    try:
        _call_mark_meta(mark_meta_deleted, rid, operator=actor)
        set_item_phase(item, PHASE_DONE)
        save_reject_journal(run_dir, journal)
    except Exception as exc:
        raise RuntimeError(f"{original_name}: metadata update failed: {exc}") from exc
