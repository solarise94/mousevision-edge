"""Weighing run (batch) helpers: one cage scan → one run directory."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


def new_run_id() -> str:
    return str(uuid.uuid4())


def create_run_dir(
    output_root: str | Path,
    *,
    cage_id: str,
    mode: str = "video",
    source_id: str | None = None,
    device_id: str = "scale01",
    run_id: str | None = None,
    project_id: str = "default",
    requested_ordinal: int | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Create `output/run_<stamp>_<shortid>/` with manifest.json."""
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    rid = run_id or new_run_id()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = output_root / f"run_{stamp}_{rid[:8]}"
    run_dir.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "run_id": rid,
        "cage_id": cage_id,
        "project_id": project_id,
        "requested_ordinal": requested_ordinal,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "source_id": source_id,
        "device": device_id,
        "status": "running",
        "record_count": 0,
    }
    write_manifest(run_dir, manifest)
    return run_dir, manifest


def write_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    (Path(run_dir) / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def load_manifest(run_dir: Path) -> dict[str, Any] | None:
    path = Path(run_dir) / "manifest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def bump_record_count(run_dir: Path, count: int | None = None) -> None:
    manifest = load_manifest(run_dir) or {}
    if count is None:
        manifest["record_count"] = int(manifest.get("record_count", 0)) + 1
    else:
        manifest["record_count"] = int(count)
    write_manifest(run_dir, manifest)


def finish_run(run_dir: Path, status: str = "completed") -> None:
    manifest = load_manifest(run_dir) or {}
    manifest["status"] = status
    manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
    write_manifest(run_dir, manifest)


def renumber_records(run_dir: str | Path, new_ordinals: list[int]) -> list[Path]:
    """Reassign `mouse_NNN` ordinals to `new_ordinals` (aligned to ascending order).

    Journal-based two-phase rename so overlapping old/new ranges never collide,
    and any failure (mid-final-rename, JSON write error, crash) rolls back to
    the EXACT original state — including already-completed finals and modified
    record.json content. The journal persists to disk so a crash mid-renumber
    can be recovered on startup via ``restore_renumber_journal``.

    Returns final dirs on success. Raises on failure (after rollback).
    """
    run_dir = Path(run_dir)
    dirs = sorted(
        run_dir.glob("mouse_*"),
        key=lambda p: int(p.name.split("_", 1)[1]) if p.name.split("_", 1)[1].isdigit() else 0,
    )
    dirs = [d for d in dirs if not d.name.endswith(".renum")]
    if len(dirs) != len(new_ordinals):
        raise ValueError(
            f"renumber count mismatch: {len(dirs)} dirs vs {len(new_ordinals)} ordinals"
        )

    # Build the journal: for each dir, capture original name, temp name, final
    # name, and the original record.json content so we can restore byte-for-byte.
    journal_path = run_dir / ".renum_journal.json"
    plan: list[dict[str, Any]] = []
    for d, new_ord in zip(dirs, new_ordinals):
        orig_name = d.name
        temp_name = d.name + ".renum"
        final_name = f"mouse_{int(new_ord):03d}"
        rec_path = d / "record.json"
        orig_record = None
        if rec_path.exists():
            try:
                orig_record = rec_path.read_text(encoding="utf-8")
            except Exception:
                orig_record = None
        plan.append(
            {
                "orig": orig_name,
                "temp": temp_name,
                "final": final_name,
                "record": orig_record,
                "new_ordinal": int(new_ord),
            }
        )

    # Persist the journal BEFORE mutating anything, so a crash is recoverable.
    journal_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    try:
        # Phase 1: all originals → temps (frees all final slots).
        for entry in plan:
            (run_dir / entry["orig"]).rename(run_dir / entry["temp"])

        # Phase 2: temps → finals + rewrite record.json.
        for entry in plan:
            temp = run_dir / entry["temp"]
            final = run_dir / entry["final"]
            temp.rename(final)
            entry["finalized"] = True  # mark for rollback ordering
            rec_path = final / "record.json"
            if rec_path.exists():
                record = json.loads(rec_path.read_text(encoding="utf-8"))
                record["ordinal"] = entry["new_ordinal"]
                record["actual_ordinal"] = entry["new_ordinal"]
                rec_path.write_text(
                    json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
                )

        # Success: clean up the journal.
        journal_path.unlink(missing_ok=True)
        return [run_dir / e["final"] for e in plan]
    except Exception:
        _rollback_renumber(run_dir, plan)
        journal_path.unlink(missing_ok=True)
        raise


def _rollback_renumber(run_dir: Path, plan: list[dict[str, Any]]) -> None:
    """Fully reverse a (possibly partially-applied) renumber plan.

    Reverse order: any finalized dir → back to a temp; then every temp → back
    to its original name; finally restore original record.json content. This
    handles the case where phase 2 completed some finals before failing.
    """
    # Step A: move any completed finals back to temp names (reverse order so
    # we never clobber a slot another rollback step still needs).
    for entry in reversed(plan):
        if not entry.get("finalized"):
            continue
        final = run_dir / entry["final"]
        temp = run_dir / entry["temp"]
        if final.exists() and not temp.exists():
            final.rename(temp)
        elif final.exists() and temp.exists():
            # Both exist (shouldn't normally); prefer keeping the temp.
            import shutil

            shutil.rmtree(final, ignore_errors=True)

    # Step B: restore all temps to their original names (all final slots are
    # now free because step A moved finals back to temps).
    for entry in plan:
        temp = run_dir / entry["temp"]
        orig = run_dir / entry["orig"]
        if temp.exists():
            temp.rename(orig)

    # Step C: restore original record.json content for each original dir.
    for entry in plan:
        orig = run_dir / entry["orig"]
        if entry.get("record") is not None:
            (orig / "record.json").write_text(entry["record"], encoding="utf-8")


def restore_renumber_temps(run_dir: str | Path) -> int:
    """Startup repair: restore any leftover ``*.renum`` dirs or journal from a crash.

    Prefers the journal (byte-exact rollback including record.json) when present;
    falls back to restoring bare ``.renum`` temps. Returns dirs restored.
    """
    run_dir = Path(run_dir)
    journal_path = run_dir / ".renum_journal.json"
    if journal_path.exists():
        try:
            plan = json.loads(journal_path.read_text(encoding="utf-8"))
            _rollback_renumber(run_dir, plan)
            journal_path.unlink(missing_ok=True)
            return len(plan)
        except Exception:
            pass  # fall through to bare-temp recovery

    restored = 0
    import shutil

    for temp in run_dir.glob("mouse_*.renum"):
        orig_name = temp.name[: -len(".renum")]
        target = run_dir / orig_name
        if target.exists():
            shutil.rmtree(temp, ignore_errors=True)
        else:
            temp.rename(target)
        restored += 1
    return restored
