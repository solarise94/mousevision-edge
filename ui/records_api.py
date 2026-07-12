"""Record listing, overview, and export helpers."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ui.boxes import strain_from_cage
from ui.records_meta import RecordsMetaStore
from ui.registry import MouseRegistry


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _load_record_json(output_root: Path, mouse: dict[str, Any]) -> dict[str, Any]:
    path = output_root / mouse["dir"] / "record.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _duration_sec(record: dict[str, Any]) -> float | None:
    start = record.get("clip_start_ms")
    end = record.get("clip_end_ms")
    if start is None or end is None:
        return None
    return round(max(0.0, float(end) - float(start)) / 1000.0, 1)


def collect_records(
    registry: MouseRegistry,
    meta_store: RecordsMetaStore,
    output_root: Path,
    *,
    tab: str | None = None,
    strain: str | None = None,
    cage_id: str | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    include_deleted: bool = False,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    df = _parse_ts(date_from)
    dt = _parse_ts(date_to)

    for run in registry.list_runs():
        for mouse in registry._mice_in_dir(Path(run["path"]), run_id=run["run_id"]):
            record_id = mouse.get("record_id")
            if not record_id:
                continue
            raw = _load_record_json(output_root, mouse)
            cage = str(mouse.get("cage_id") or raw.get("cage_id") or "-")
            if cage_id and cage != cage_id:
                continue
            resolved_strain = strain_from_cage(cage)
            if strain and resolved_strain != strain:
                continue
            meta = meta_store.ensure(record_id)
            status = meta["status"]
            if status == "deleted" and not include_deleted and tab != "deleted":
                continue
            if tab and tab != "all" and status != tab:
                if tab == "pending" and status != "pending":
                    continue
                elif tab in {"published", "deleted"} and status != tab:
                    continue
            ts = mouse.get("timestamp") or raw.get("timestamp")
            ts_dt = _parse_ts(ts)
            if df and ts_dt and ts_dt < df:
                continue
            if dt and ts_dt and ts_dt > dt:
                continue
            notes = meta.get("notes") or ""
            if q:
                needle = q.lower()
                hay = " ".join(
                    [
                        str(record_id),
                        cage,
                        resolved_strain,
                        str(mouse.get("ordinal")),
                        notes,
                    ]
                ).lower()
                if needle not in hay:
                    continue
            items.append(
                {
                    **mouse,
                    "record_id": record_id,
                    "cage_id": cage,
                    "strain": resolved_strain,
                    "status": status,
                    "verified": bool(meta.get("verified")),
                    "published_at": meta.get("published_at"),
                    "deleted_at": meta.get("deleted_at"),
                    "operator": meta.get("operator"),
                    "notes": notes,
                    "tags": meta.get("tags") or "",
                    "photo_url": f"/api/records/{record_id}/photo",
                    "duration_sec": _duration_sec(raw),
                    "clip_start_ms": raw.get("clip_start_ms"),
                    "clip_end_ms": raw.get("clip_end_ms"),
                    "run_started_at": run.get("started_at"),
                }
            )
    items.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    return items


def overview_stats(
    registry: MouseRegistry,
    meta_store: RecordsMetaStore,
    output_root: Path,
) -> dict[str, Any]:
    records = collect_records(
        registry, meta_store, output_root, include_deleted=True
    )
    weights = [
        float(r["weight"])
        for r in records
        if r.get("weight") is not None and r.get("status") != "deleted"
    ]
    status_counts = {"pending": 0, "published": 0, "deleted": 0}
    daily: dict[str, int] = {}
    for rec in records:
        status_counts[rec.get("status", "pending")] = (
            status_counts.get(rec.get("status", "pending"), 0) + 1
        )
        if rec.get("status") == "deleted":
            continue
        ts = rec.get("timestamp")
        if ts:
            day = ts[:10]
            daily[day] = daily.get(day, 0) + 1
    avg_weight = round(sum(weights) / len(weights), 2) if weights else None
    meta_counts = meta_store.counts()
    return {
        "total_records": status_counts.get("pending", 0) + status_counts.get("published", 0),
        "pending_count": status_counts.get("pending", 0),
        "published_count": status_counts.get("published", 0),
        "deleted_count": status_counts.get("deleted", 0),
        "average_weight": avg_weight,
        "meta_overlay": meta_counts,
        "daily_counts": [
            {"date": k, "count": daily[k]} for k in sorted(daily.keys())
        ],
        "weight_samples": weights[:200],
    }


def mice_admin_view(
    registry: MouseRegistry,
    meta_store: RecordsMetaStore,
    output_root: Path,
) -> list[dict[str, Any]]:
    records = collect_records(
        registry, meta_store, output_root, tab="all", include_deleted=False
    )
    by_cage: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        cage = rec["cage_id"]
        by_cage.setdefault(cage, []).append(rec)
    rows = []
    for cage, recs in sorted(by_cage.items()):
        recs.sort(key=lambda r: int(r.get("ordinal") or 0))
        rows.append(
            {
                "cage_id": cage,
                "strain": strain_from_cage(cage),
                "mouse_count": len(recs),
                "latest_weight": recs[-1].get("weight") if recs else None,
                "latest_at": recs[-1].get("timestamp") if recs else None,
                "records": recs,
            }
        )
    return rows


def export_csv(records: list[dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    fields = [
        "record_id",
        "cage_id",
        "strain",
        "ordinal",
        "weight",
        "confidence",
        "status",
        "verified",
        "timestamp",
        "notes",
        "run_id",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for rec in records:
        writer.writerow(rec)
    return buf.getvalue().encode("utf-8-sig")


def export_xlsx(records: list[dict[str, Any]]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "records"
    headers = [
        "record_id",
        "cage_id",
        "strain",
        "ordinal",
        "weight",
        "confidence",
        "status",
        "verified",
        "timestamp",
        "notes",
        "run_id",
    ]
    ws.append(headers)
    for rec in records:
        ws.append([rec.get(h) for h in headers])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
