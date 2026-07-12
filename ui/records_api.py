"""Record listing, overview, and export helpers."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

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


def _parse_date_from(value: str | None) -> datetime | None:
    """Parse a date-from bound. Date-only 'YYYY-MM-DD' -> midnight inclusive."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    # If only a date was given (no time component), it's already midnight.
    return dt


def _parse_date_to(value: str | None) -> datetime | None:
    """Parse a date-to bound as an exclusive upper bound (next midnight).

    '2026-07-12' -> 2026-07-13 00:00:00, so all records on July 12 are included.
    A full timestamp '2026-07-12T15:30:00' is used as-is (exclusive).
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    # If only a date (length 10, no 'T'), extend to next midnight.
    if "T" not in value and len(value) == 10:
        return dt + timedelta(days=1)
    return dt


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
    df = _parse_date_from(date_from)
    dt = _parse_date_to(date_to)

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
            # If a date filter is set but the record has no parseable timestamp,
            # exclude it rather than silently letting it bypass the filter.
            if (df or dt) and ts_dt is None:
                continue
            if df and ts_dt < df:
                continue
            if dt and ts_dt >= dt:
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


def _compute_weight_stats(
    weights: list[float], *, is_single_cohort: bool = False
) -> dict[str, Any]:
    """Descriptive stats for continuous weight data (QC overview).

    Returns Mean±SD (not SEM - SD describes individual spread, which is what
    QC needs), min/max/range/median, a histogram with adaptive binning, and a
    normal PDF fit curve. The fit is only shown when the data is a single
    homogeneous cohort (one cage) AND n >= 30: n>=30 alone does NOT make raw
    weights normal (CLT applies to sample means, not the data itself), and
    mixing cages/strains/batches can be multimodal. Uses numpy only (no scipy).
    """
    n = len(weights)
    if n == 0:
        return {
            "mean": None, "sd": None, "sem": None, "min": None, "max": None,
            "range": None, "median": None, "n": 0,
            "hist_bins": [], "hist_counts": [],
            "fit_x": [], "fit_y": [], "show_fit": False,
        }
    w = np.array(weights, dtype=float)
    mean = float(w.mean())
    sd = float(w.std(ddof=1)) if n > 1 else 0.0
    sem = sd / float(np.sqrt(n)) if n > 1 else 0.0
    wmin, wmax = float(w.min()), float(w.max())

    # Adaptive histogram: Sturges rule with 0.2g floor on bin width,
    # clamped to [4, 20] bins. Avoids the fixed-0.5g sparseness on small n.
    n_bins = max(4, min(20, int(np.ceil(np.log2(n) + 1))))
    raw_width = (wmax - wmin) / n_bins if wmax > wmin else 0.5
    bin_width = max(0.2, raw_width)
    lo = np.floor(wmin / bin_width) * bin_width
    hi = np.ceil(wmax / bin_width) * bin_width
    if hi <= lo:
        hi = lo + bin_width
    bin_edges = np.arange(lo, hi + bin_width / 2, bin_width)
    counts, _ = np.histogram(w, bins=bin_edges)

    # Normal PDF fit - only for single cohort + n >= 30 (see docstring).
    show_fit = n >= 30 and sd > 1e-9 and is_single_cohort
    if show_fit:
        sigma = sd
        fit_x = np.linspace(mean - 3 * sigma, mean + 3 * sigma, 40)
        fit_y_pdf = (1.0 / (sigma * np.sqrt(2 * np.pi))) * np.exp(
            -0.5 * ((fit_x - mean) / sigma) ** 2
        )
        fit_y_scaled = fit_y_pdf * n * bin_width  # scale to histogram counts
    else:
        fit_x, fit_y_scaled = np.array([]), np.array([])

    return {
        "mean": round(mean, 2),
        "sd": round(sd, 2),
        "sem": round(sem, 2),
        "min": round(wmin, 2),
        "max": round(wmax, 2),
        "range": round(wmax - wmin, 2),
        "median": round(float(np.median(w)), 2),
        "n": n,
        "hist_bins": [round(float(b), 2) for b in bin_edges],
        "hist_counts": [int(c) for c in counts],
        "fit_x": [round(float(x), 2) for x in fit_x],
        "fit_y": [round(float(y), 2) for y in fit_y_scaled],
        "show_fit": bool(show_fit),
    }


def _compute_cage_weight_view(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-cage weight view for strip plot + robust outlier screening.

    Groups records by cage_id. Within each cage, outliers are flagged by the
    cage's own median ±2g (robust to extreme values pulling the mean). This
    replaces the old global-mean ±2g "compliance" which was a logical loop.
    """
    by_cage: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        if rec.get("weight") is None:
            continue
        by_cage.setdefault(rec["cage_id"], []).append(rec)

    cages_out = []
    total_outliers = 0
    total_n = 0
    for cage_id in sorted(by_cage.keys()):
        recs = by_cage[cage_id]
        weights = [float(r["weight"]) for r in recs]
        median = float(np.median(weights))
        thr_lo, thr_hi = median - 2.0, median + 2.0
        points = []
        for r in recs:
            w = float(r["weight"])
            is_outlier = w < thr_lo or w > thr_hi
            points.append({
                "record_id": r.get("record_id"),
                "ordinal": r.get("ordinal"),
                "weight": round(w, 2),
                "outlier": is_outlier,
            })
        n_out = sum(1 for p in points if p["outlier"])
        total_outliers += n_out
        total_n += len(points)
        cages_out.append({
            "cage_id": cage_id,
            "strain": strain_from_cage(cage_id),
            "n": len(points),
            "median": round(median, 2),
            "threshold_low": round(thr_lo, 2),
            "threshold_high": round(thr_hi, 2),
            "outlier_count": n_out,
            "points": points,
        })
    return {
        "cages": cages_out,
        "total_n": total_n,
        "total_outliers": total_outliers,
    }


def overview_stats(
    registry: MouseRegistry,
    meta_store: RecordsMetaStore,
    output_root: Path,
    *,
    strain: str | None = None,
    cage_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    # status filter: tab semantics — None means all non-deleted.
    tab = status if status and status != "all" else None
    records = collect_records(
        registry, meta_store, output_root,
        tab=tab,
        strain=strain,
        cage_id=cage_id,
        date_from=date_from,
        date_to=date_to,
        include_deleted=True,
    )
    active = [r for r in records if r.get("status") != "deleted"]
    weights = [
        float(r["weight"])
        for r in active
        if r.get("weight") is not None
    ]
    status_counts = {"pending": 0, "published": 0, "deleted": 0}
    for rec in records:
        status_counts[rec.get("status", "pending")] = (
            status_counts.get(rec.get("status", "pending"), 0) + 1
        )
    # Daily counts — fill gaps so consecutive days are equidistant on the axis.
    daily: dict[str, int] = {}
    for rec in active:
        ts = rec.get("timestamp")
        if ts:
            day = ts[:10]
            daily[day] = daily.get(day, 0) + 1
    daily_filled = _fill_daily_counts(daily, date_from, date_to)

    avg_weight = round(sum(weights) / len(weights), 2) if weights else None
    meta_counts = meta_store.counts()
    return {
        "total_records": status_counts.get("pending", 0) + status_counts.get("published", 0),
        "pending_count": status_counts.get("pending", 0),
        "published_count": status_counts.get("published", 0),
        "deleted_count": status_counts.get("deleted", 0),
        "average_weight": avg_weight,
        "meta_overlay": meta_counts,
        "daily_counts": daily_filled,
        "weight_stats": _compute_weight_stats(weights, is_single_cohort=bool(cage_id)),
        "cage_weights": _compute_cage_weight_view(active),
        "filters": {
            "strain": strain, "cage_id": cage_id,
            "date_from": date_from, "date_to": date_to, "status": status,
            "n": len(active),
        },
    }


def _fill_daily_counts(
    daily: dict[str, int],
    date_from: str | None,
    date_to: str | None,
    *,
    max_days: int = 90,
    default_days: int = 30,
) -> list[dict[str, Any]]:
    """Return daily counts with gaps filled as 0 over a contiguous date range.

    When no explicit bounds are given, defaults to the last ``default_days``
    from the latest data point. Spans longer than ``max_days`` are clamped to
    ``max_days`` ending at the latest point to prevent millions of objects and
    chart overcrowding.
    """
    if not daily and not date_from and not date_to:
        return []
    days_present = sorted(daily.keys())
    from datetime import datetime as _dt, timedelta as _td
    # Resolve end bound
    if date_to:
        try:
            last = _dt.fromisoformat(date_to[:10])
        except ValueError:
            last = _dt.fromisoformat(days_present[-1]) if days_present else None
    elif days_present:
        last = _dt.fromisoformat(days_present[-1])
    else:
        return []
    # Resolve start bound
    if date_from:
        try:
            cur = _dt.fromisoformat(date_from[:10])
        except ValueError:
            cur = last - _td(days=default_days) if last else None
    elif days_present:
        cur = last - _td(days=default_days)
    else:
        return []
    if not cur or not last:
        return [{"date": k, "count": daily[k]} for k in days_present]
    # Clamp span to max_days
    if (last - cur).days > max_days:
        cur = last - _td(days=max_days)
    out = []
    while cur <= last:
        key = cur.strftime("%Y-%m-%d")
        out.append({"date": key, "count": daily.get(key, 0)})
        cur += _td(days=1)
    return out


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


def verify_cages_view(
    registry: MouseRegistry,
    meta_store: RecordsMetaStore,
    output_root: Path,
    *,
    strain: str | None = None,
    cage_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Cage-grouped view of pending records for quick verification.

    Only cages with at least one pending record are returned. Each cage carries
    its pending records (sorted by ordinal), a mean weight, and the strain.
    """
    records = collect_records(
        registry, meta_store, output_root,
        tab="pending",
        strain=strain,
        cage_id=cage_id,
        date_from=date_from,
        date_to=date_to,
        include_deleted=False,
    )
    by_cage: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        by_cage.setdefault(rec["cage_id"], []).append(rec)
    cages = []
    all_weights: list[float] = []
    for cage, recs in sorted(by_cage.items()):
        recs.sort(key=lambda r: int(r.get("ordinal") or 0))
        weights = [float(r["weight"]) for r in recs if r.get("weight") is not None]
        all_weights.extend(weights)
        cages.append({
            "cage_id": cage,
            "strain": strain_from_cage(cage),
            "count": len(recs),
            "mean_weight": round(sum(weights) / len(weights), 2) if weights else None,
            "records": recs,
        })
    return {
        "cages": cages,
        "total_cages": len(cages),
        "total_records": len(records),
        "average_weight": round(sum(all_weights) / len(all_weights), 2) if all_weights else None,
    }


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
