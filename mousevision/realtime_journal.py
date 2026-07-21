"""Durable realtime attempt journal.

The realtime session engine keeps attempts in process memory only. A container
restart, session timeout, or a crash between ``finish`` and video upload would
otherwise lose the operator's accept/reject decisions — and the offline video
re-analysis would then re-detect rejected retries as extra mice.

This module persists each attempt decision to a JSON-lines journal file under
``output/realtime_journal/<session_id>.jsonl`` as it happens, so the decisions
survive process death and can drive :mod:`mousevision.realtime_finalize`.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class JournalMeta:
    """Session-level metadata written once at journal creation."""

    session_id: str
    cage_id: str
    project_id: str
    created_at: float
    device_id: str = ""


class AttemptJournal:
    """Append-only JSONL journal of realtime attempt decisions.

    Thread-safe: a single lock guards appends. Each line is one event
    (``meta`` / ``attempt`` / ``decision`` / ``finish``) so the file can be
    replayed line-by-line to reconstruct the session outcome.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _append(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def write_meta(self, meta: JournalMeta) -> None:
        self._append({"event": "meta", **asdict(meta)})

    def record_attempt(self, attempt: Any) -> None:
        """Log a newly announced attempt (state='announced')."""
        self._append(
            {
                "event": "attempt",
                "attempt_id": attempt.attempt_id,
                "weight_g": attempt.weight_g,
                "confidence": attempt.confidence,
                "frame_seq": attempt.frame_seq,
                "client_ts_ms": attempt.client_ts_ms,
                "state": attempt.state,
                "created_at": attempt.created_at,
            }
        )

    def record_decision(self, attempt_id: str, decision: str, weight_g: float | None) -> None:
        """Log an accept/reject decision for an attempt."""
        self._append(
            {
                "event": "decision",
                "attempt_id": attempt_id,
                "decision": decision,  # "accepted" | "rejected"
                "weight_g": weight_g,
            }
        )

    def record_finish(self, accepted: list[Any], rejected: list[Any]) -> None:
        self._append(
            {
                "event": "finish",
                "accepted_ids": [a.attempt_id for a in accepted],
                "rejected_ids": [a.attempt_id for a in rejected],
            }
        )

    # ----------------------------------------------------------------- #
    # Replay
    # ----------------------------------------------------------------- #

    @staticmethod
    def read(path: Path) -> dict[str, Any]:
        """Replay a journal file into a summary dict.

        Returns ``{"meta": {...}, "attempts": {id: {...}}, "decisions": {id: str},
        "finished": bool}``. Missing file returns an empty summary.
        """
        path = Path(path)
        summary: dict[str, Any] = {
            "meta": None,
            "attempts": {},
            "decisions": {},
            "finished": False,
        }
        if not path.exists():
            return summary
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = ev.get("event")
            if kind == "meta":
                summary["meta"] = {k: v for k, v in ev.items() if k != "event"}
            elif kind == "attempt":
                summary["attempts"][ev["attempt_id"]] = {
                    k: v for k, v in ev.items() if k != "event"
                }
            elif kind == "decision":
                summary["decisions"][ev["attempt_id"]] = ev.get("decision")
            elif kind == "finish":
                summary["finished"] = True
        return summary


def journal_path(output_root: str | Path, session_id: str) -> Path:
    return Path(output_root) / "realtime_journal" / f"{session_id}.jsonl"
