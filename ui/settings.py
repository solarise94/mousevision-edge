"""System settings persisted as JSON."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

DEFAULT_SETTINGS: dict[str, Any] = {
    "project_id": "default",
    "mouse_no_pad": 2,
    "mouse_no_start": 1,
    "retention_days": 365,
    "publish_target": "",
    "default_strain": "C57BL/6",
    "admin_password_hint": "首次登录请修改默认管理员密码",
}


class SettingsStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def get(self) -> dict[str, Any]:
        with self.lock:
            if not self.path.exists():
                return dict(DEFAULT_SETTINGS)
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                return dict(DEFAULT_SETTINGS)
            merged = dict(DEFAULT_SETTINGS)
            merged.update(data)
            return merged

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        current = self.get()
        allowed = set(DEFAULT_SETTINGS)
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported settings: {sorted(unknown)}")
        current.update(changes)
        with self.lock:
            self.path.write_text(
                json.dumps(current, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        return current
