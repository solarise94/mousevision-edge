"""Run-scoped mouse registry for local inspection UI."""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from mousevision.run import load_manifest


class MouseRegistry:
    """Filesystem-backed registry keyed by run_id + ordinal."""

    def __init__(self, path: Path, output_root: Path) -> None:
        self.path = path
        self.output_root = output_root
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _default(self) -> dict[str, Any]:
        return {"active_run_id": None, "active_run_dir": None}

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._default()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            data.setdefault("active_run_id", None)
            data.setdefault("active_run_dir", None)
            return data
        except Exception:
            return self._default()

    def _save(self) -> None:
        self.path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def set_active_run(self, run_id: str, run_dir: Path) -> None:
        with self.lock:
            try:
                rel = str(run_dir.resolve().relative_to(self.output_root.resolve()))
            except ValueError:
                rel = str(run_dir)
            self._data["active_run_id"] = run_id
            self._data["active_run_dir"] = rel
            self._save()

    def active_run(self) -> dict[str, Any] | None:
        with self.lock:
            run_id = self._data.get("active_run_id")
            rel = self._data.get("active_run_dir")
        if not run_id or not rel:
            runs = self.list_runs()
            return runs[0] if runs else None
        run_dir = self.output_root / rel
        if not run_dir.exists():
            runs = self.list_runs()
            return runs[0] if runs else None
        manifest = load_manifest(run_dir) or {}
        mice = self._mice_in_dir(run_dir, run_id=str(manifest.get("run_id") or run_id))
        return {
            "run_id": str(manifest.get("run_id") or run_id),
            "cage_id": manifest.get("cage_id", "-"),
            "dir": rel,
            "started_at": manifest.get("started_at"),
            "status": manifest.get("status", "unknown"),
            "mode": manifest.get("mode", "video"),
            "record_count": len(mice),
            "path": str(run_dir),
        }

    def list_runs(self) -> list[dict[str, Any]]:
        if not self.output_root.exists():
            return []
        runs: list[dict[str, Any]] = []
        for run_dir in sorted(self.output_root.glob("run_*"), reverse=True):
            if not run_dir.is_dir():
                continue
            manifest = load_manifest(run_dir)
            if manifest is None:
                # Synthesize for older run dirs without manifest.
                mice = self._mice_in_dir(run_dir, run_id=run_dir.name)
                if not mice:
                    continue
                manifest = {
                    "run_id": run_dir.name,
                    "cage_id": mice[0].get("cage_id") or mice[0].get("box_id") or "-",
                    "started_at": mice[0].get("timestamp"),
                    "status": "legacy",
                    "mode": "video",
                    "record_count": len(mice),
                }
            else:
                mice = self._mice_in_dir(run_dir, run_id=str(manifest.get("run_id") or run_dir.name))
                manifest = dict(manifest)
                manifest["record_count"] = len(mice)
            try:
                rel = str(run_dir.resolve().relative_to(self.output_root.resolve()))
            except ValueError:
                rel = run_dir.name
            runs.append(
                {
                    "run_id": str(manifest.get("run_id") or run_dir.name),
                    "cage_id": manifest.get("cage_id", "-"),
                    "dir": rel,
                    "started_at": manifest.get("started_at"),
                    "status": manifest.get("status", "unknown"),
                    "mode": manifest.get("mode", "video"),
                    "record_count": int(manifest.get("record_count") or 0),
                    "path": str(run_dir),
                }
            )
        return runs

    def _mice_in_dir(self, run_dir: Path, *, run_id: str) -> list[dict[str, Any]]:
        mice: list[dict[str, Any]] = []
        # Prefer mouse_NNN layout; fall back to any nested record.json.
        record_paths = sorted(run_dir.glob("mouse_*/record.json"))
        if not record_paths:
            record_paths = sorted(run_dir.glob("**/record.json"))
        for record_path in record_paths:
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            photo = record_path.parent / "photo.jpg"
            if not photo.exists():
                continue
            ordinal = int(record.get("ordinal") or record.get("session_index") or 0)
            if ordinal <= 0:
                # Derive from mouse_003 folder name when possible.
                name = record_path.parent.name
                if name.startswith("mouse_"):
                    try:
                        ordinal = int(name.split("_", 1)[1])
                    except ValueError:
                        ordinal = len(mice) + 1
                else:
                    ordinal = len(mice) + 1
            try:
                rel = str(record_path.parent.resolve().relative_to(self.output_root.resolve()))
            except ValueError:
                rel = str(record_path.parent)
            mice.append(
                {
                    "index": ordinal,  # UI label: ordinal within run
                    "ordinal": ordinal,
                    "actual_ordinal": record.get("actual_ordinal", ordinal),
                    "requested_ordinal": record.get("requested_ordinal"),
                    "record_id": record.get("record_id"),
                    "run_id": record.get("run_id") or run_id,
                    "project_id": record.get("project_id", "default"),
                    "cage_id": record.get("cage_id") or record.get("box_id") or "-",
                    "box_id": record.get("cage_id") or record.get("box_id") or "-",
                    "weight": record.get("weight"),
                    "confidence": record.get("confidence"),
                    "timestamp": record.get("timestamp")
                    or datetime.now().isoformat(timespec="seconds"),
                    "dir": rel,
                    "photo": "photo.jpg",
                    "device": record.get("device", "scale01"),
                }
            )
        mice.sort(key=lambda m: int(m["ordinal"]))
        return mice

    def list_mice(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """Mice in the given run (or active/latest run). Newest ordinal first for grid."""
        with self.lock:
            active_id = self._data.get("active_run_id")
        target = run_id or active_id
        runs = self.list_runs()
        if not runs:
            return []
        if target:
            run = next((r for r in runs if r["run_id"] == target), None)
            if run is None:
                run = runs[0]
        else:
            run = runs[0]
        mice = self._mice_in_dir(Path(run["path"]), run_id=run["run_id"])
        mice.sort(key=lambda m: m.get("ordinal", 0), reverse=True)
        return mice

    def get(self, index: int, run_id: str | None = None) -> dict[str, Any] | None:
        for mouse in self.list_mice(run_id=run_id):
            if int(mouse.get("ordinal", mouse.get("index", -1))) == int(index):
                return dict(mouse)
        return None

    def get_by_record_id(self, record_id: str) -> dict[str, Any] | None:
        for run in self.list_runs():
            for mouse in self._mice_in_dir(Path(run["path"]), run_id=run["run_id"]):
                if mouse.get("record_id") == record_id:
                    return dict(mouse)
        return None

    def peek_next_ordinal(self, run_id: str | None = None) -> int:
        mice = self.list_mice(run_id=run_id)
        if not mice:
            return 1
        return max(int(m["ordinal"]) for m in mice) + 1

    def peek_next_index(self) -> int:
        """Compat alias: next ordinal in active run."""
        return self.peek_next_ordinal()

    def clear(self) -> None:
        with self.lock:
            self._data = self._default()
            self._save()

    def register(
        self,
        *,
        run_id: str,
        run_dir: Path,
        cage_id: str,
        ordinal: int,
        record_id: str | None,
        weight: float,
        confidence: float,
        output_dir: Path,
        timestamp: str | None = None,
        device: str = "scale01",
    ) -> dict[str, Any]:
        """Idempotent register: UNIQUE(run_id, ordinal). Conflict returns existing."""
        with self.lock:
            self._data["active_run_id"] = run_id
            try:
                rel_run = str(run_dir.resolve().relative_to(self.output_root.resolve()))
            except ValueError:
                rel_run = str(run_dir)
            self._data["active_run_dir"] = rel_run
            self._save()

        existing = [
            m
            for m in self._mice_in_dir(run_dir, run_id=run_id)
            if int(m["ordinal"]) == int(ordinal)
        ]
        if existing:
            return dict(existing[0])

        try:
            rel = str(output_dir.resolve().relative_to(self.output_root.resolve()))
        except ValueError:
            rel = str(output_dir)
        return {
            "index": ordinal,
            "ordinal": ordinal,
            "record_id": record_id,
            "run_id": run_id,
            "cage_id": cage_id,
            "box_id": cage_id,
            "weight": weight,
            "confidence": confidence,
            "timestamp": timestamp or datetime.now().isoformat(timespec="seconds"),
            "dir": rel,
            "photo": "photo.jpg",
            "device": device,
        }
