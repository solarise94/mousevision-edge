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

    Two-phase (temp suffix) rename so overlapping old/new ranges never collide.
    Updates each record.json `ordinal` / `actual_ordinal`. Returns final dirs.
    Used when a single-mouse job unexpectedly detects multiple mice and the
    extra records must consume freshly reserved ordinals (design §3.5.2 rule 4).
    """
    run_dir = Path(run_dir)
    dirs = sorted(
        run_dir.glob("mouse_*"),
        key=lambda p: int(p.name.split("_", 1)[1]) if p.name.split("_", 1)[1].isdigit() else 0,
    )
    if len(dirs) != len(new_ordinals):
        raise ValueError(
            f"renumber count mismatch: {len(dirs)} dirs vs {len(new_ordinals)} ordinals"
        )
    temps: list[Path] = []
    for d in dirs:
        t = d.with_name(d.name + ".renum")
        d.rename(t)
        temps.append(t)
    finals: list[Path] = []
    for temp, new_ord in zip(temps, new_ordinals):
        final = run_dir / f"mouse_{int(new_ord):03d}"
        temp.rename(final)
        finals.append(final)
        rec_path = final / "record.json"
        if rec_path.exists():
            record = json.loads(rec_path.read_text(encoding="utf-8"))
            record["ordinal"] = int(new_ord)
            record["actual_ordinal"] = int(new_ord)
            rec_path.write_text(
                json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
            )
    return finals
