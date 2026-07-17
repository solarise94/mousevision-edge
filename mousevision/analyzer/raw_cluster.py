"""Session weight estimation directly from raw per-frame OCR reads.

The fused curve (TemporalWeightFusion) is deliberately all-or-nothing: it
withholds output whenever the sliding window disagrees. That makes it a good
live readout but a starvation diet for settlement analysis — sessions whose
OCR flickers end with near-empty curves and garbage "guessed" weights.

This module instead clusters the *raw* readable observations collected over
the whole session window (including the pre-ENTER ramp). A real weighing has
a dominant value cluster (the settled display); OCR spikes, seven-seg
confusions and tail-hold partial reads form small minority clusters.

Verdicts:
- ``stable``: dominant cluster has strong support → auto weight, no review.
- ``conflict``: dominant exists but a rival cluster has real support → keep
  the dominant weight, flag needs_review (human confirms, no hand-fill).
- ``insufficient``: not enough evidence → caller falls back to legacy chain.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class RawClusterConfig:
    # Cluster merge tolerance (grams). OCR repeatability on a settled display
    # is ~0.05g; 0.12 absorbs scale wobble without merging 13.x/17.x rivals.
    tol: float = 0.12
    # Minimum raw OCR confidence to count a sample at all.
    min_conf: float = 0.35
    # stable: dominant vote share AND minimum votes required.
    stable_frac: float = 0.60
    stable_min_votes: int = 4
    # Internal spread (P90-P10) allowed for a "stable" cluster.
    stable_max_span: float = 0.25
    # conflict: dominant needs at least this many votes to name a weight.
    conflict_min_votes: int = 3
    # Below this vote share there is no dominant worth reporting.
    conflict_min_frac: float = 0.25
    # Classic seven-seg 4<->9 glare confusion gap (24.18 vs 29.18).
    four_nine_gap: tuple[float, float] = (4.5, 5.5)
    # Score tie-break: prefer the LATER cluster when scores are this close
    # (settlement happens at the end of a session).
    recency_tiebreak: float = 0.15


@dataclass
class RawClusterVerdict:
    status: str  # "stable" | "conflict" | "insufficient"
    weight: float | None = None
    confidence: float = 0.0
    reason: str = ""
    support_frac: float = 0.0
    votes: int = 0
    n_samples: int = 0
    # [(median, votes, mean_conf, t_first, t_last)] best-first, for diagnostics.
    clusters: list[tuple[float, int, float, float, float]] = field(
        default_factory=list
    )


def _modal_digits(samples: list[tuple]) -> list[str]:
    from collections import Counter

    counter: Counter[tuple[str, ...]] = Counter()
    for s in samples:
        digits = s[3] if len(s) > 3 else None
        if digits:
            counter[tuple(str(d) for d in digits)] += 1
    if not counter:
        return []
    return list(counter.most_common(1)[0][0])


def _filter_usable(samples: list[tuple], min_conf: float) -> list[tuple[float, float, float, list[str]]]:
    usable: list[tuple[float, float, float, list[str]]] = []
    for s in samples:
        t_ms = float(s[0])
        w = float(s[1])
        conf = float(s[2]) if len(s) > 2 and s[2] is not None else 1.0
        digits = list(s[3]) if len(s) > 3 and s[3] else []
        if conf < min_conf:
            continue
        usable.append((t_ms, w, conf, digits))
    return usable


def _build_clusters(
    usable: list[tuple[float, float, float, list[str]]], tol: float, min_votes: int = 3
) -> list[dict]:
    """Greedy value clustering on sorted weights (merge within tol)."""
    ordered = sorted(usable, key=lambda s: s[1])
    groups: list[list[tuple[float, float, float, list[str]]]] = []
    for item in ordered:
        if groups:
            center = float(np.median([g[1] for g in groups[-1]]))
            if abs(item[1] - center) <= tol:
                groups[-1].append(item)
                continue
        groups.append([item])

    clusters: list[dict] = []
    for g in groups:
        ws = [x[1] for x in g]
        confs = [x[2] for x in g]
        ts = [x[0] for x in g]
        clusters.append(
            {
                "median": float(np.median(ws)),
                "votes": len(g),
                "mean_conf": float(np.mean(confs)),
                "score": float(len(g) * np.mean(confs)),
                "t_first": float(min(ts)),
                "t_last": float(max(ts)),
                "t_center": 0.5 * (float(min(ts)) + float(max(ts))),
                "span": float(np.percentile(ws, 90) - np.percentile(ws, 10))
                if len(g) >= 2
                else 0.0,
                "digits": _modal_digits(g),
                "members": g,
            }
        )
    return _fold_four_nine(clusters, min_votes)


def _fold_four_nine(clusters: list[dict], min_votes: int = 3) -> list[dict]:
    """Merge classic seven-seg 4<->9 glare-confusion pairs (~5g apart).

    Glare fills segments, so a real '4' can read as '9' (24.18 -> 29.18);
    the reverse is implausible. When two clusters sit 4.5-5.5g apart and
    their modal digits differ only by '4' vs '9' at one position, fold the
    higher cluster's votes into the lower one (the '4' reading is real).
    Without folding, a glare-split plateau (24.1x / 29.1x) can fall below
    the vote threshold on both sides and vanish.

    Guard: folding rescues weak evidence, it must not overturn a strong
    plateau — the '9' side is folded only when it cannot stand on its own
    (``hi votes < min_votes``) or the '4' side is at least as strong
    (``hi votes <= lo votes``). A lone 9->4 misread next to an 8-vote
    plateau would otherwise steal the plateau's votes.
    """
    if len(clusters) < 2:
        return clusters
    used = [False] * len(clusters)
    out: list[dict] = []
    for i, lo in enumerate(clusters):
        if used[i]:
            continue
        for j in range(i + 1, len(clusters)):
            hi = clusters[j]
            if used[j]:
                continue
            gap = float(hi["median"]) - float(lo["median"])
            if not (4.5 <= gap <= 5.5):
                continue
            # Never let a weak '4' cluster steal votes from a strong '9'
            # plateau (a lone 9->4 misread): fold only when the '9' side is
            # sub-threshold or no stronger than the '4' side.
            if not (hi["votes"] < min_votes or hi["votes"] <= lo["votes"]):
                continue
            lo_d, hi_d = lo["digits"], hi["digits"]
            if not lo_d or not hi_d or len(lo_d) != len(hi_d):
                continue
            # Classic confusion sits in one slot (usually the grams place):
            # '4' in lo vs '9' in hi, same prefix; decimals may differ freely.
            folded = False
            for k, (a, b) in enumerate(zip(lo_d, hi_d)):
                if a == b:
                    continue
                if a == "4" and b == "9" and lo_d[:k] == hi_d[:k]:
                    folded = True
                break
            if not folded:
                continue
            # Fold hi into lo: votes join, weight stays with the '4' reads.
            lo["members"] = list(lo["members"]) + [
                (t, w - 5.0, c, d) for (t, w, c, d) in hi["members"]
            ]
            ws = [x[1] for x in lo["members"]]
            confs = [x[2] for x in lo["members"]]
            ts = [x[0] for x in lo["members"]]
            lo["median"] = float(np.median(ws))
            lo["votes"] = len(lo["members"])
            lo["mean_conf"] = float(np.mean(confs))
            lo["score"] = float(lo["votes"] * lo["mean_conf"])
            lo["t_first"] = float(min(ts))
            lo["t_last"] = float(max(ts))
            lo["t_center"] = 0.5 * (lo["t_first"] + lo["t_last"])
            lo["span"] = (
                float(np.percentile(ws, 90) - np.percentile(ws, 10))
                if len(ws) >= 2
                else 0.0
            )
            lo["folded_49"] = True
            used[j] = True
        used[i] = True
        out.append(lo)
    return out


def sustained_clusters(
    samples: list[tuple],
    *,
    tol: float = 0.15,
    min_votes: int = 3,
    min_span_ms: float = 400.0,
    min_conf: float = 0.45,
) -> list[dict]:
    """All value clusters with enough votes and time span, time-ordered.

    Used by orphan-session recovery: sustained raw reads that never became a
    state-machine session (e.g. a restless animal whose OCR never fused).
    """
    usable = _filter_usable(samples, min_conf)
    clusters = _build_clusters(usable, tol, min_votes)
    out = [
        c
        for c in clusters
        if c["votes"] >= min_votes and (c["t_last"] - c["t_first"]) >= min_span_ms
    ]
    out.sort(key=lambda c: c["t_first"])
    return out


def analyze_raw_samples(
    samples: list[tuple],
    config: RawClusterConfig | None = None,
) -> RawClusterVerdict:
    """Cluster raw session reads; return a stability verdict.

    ``samples`` items are ``(t_ms, weight)`` or ``(t_ms, weight, conf)`` or
    ``(t_ms, weight, conf, digits)``; weights <= 0 are ignored by the caller.
    """
    cfg = config or RawClusterConfig()
    usable = _filter_usable(samples, cfg.min_conf)

    n = len(usable)
    if n < cfg.conflict_min_votes:
        return RawClusterVerdict(status="insufficient", n_samples=n)

    clusters = _build_clusters(usable, cfg.tol, cfg.stable_min_votes)

    # 4<->9 (~5g) confusion: when two clusters sit ~5g apart, the lower '4'
    # reading is usually real (glare fills the 4 into a 9). Swap preference
    # unless the higher cluster wins clearly on confidence-weighted score.
    clusters.sort(key=lambda c: c["score"], reverse=True)
    if len(clusters) >= 2:
        c0, c1 = clusters[0], clusters[1]
        gap = abs(c0["median"] - c1["median"])
        if cfg.four_nine_gap[0] <= gap <= cfg.four_nine_gap[1]:
            hi, lo = (c0, c1) if c0["median"] > c1["median"] else (c1, c0)
            lo_has_four = len(lo["digits"]) > 1 and lo["digits"][1] == "4"
            if lo_has_four and lo["score"] >= 0.7 * hi["score"]:
                clusters.remove(lo)
                clusters.insert(0, lo)

    # Recency tie-break: near-equal scores → prefer the later cluster.
    if len(clusters) >= 2:
        c0, c1 = clusters[0], clusters[1]
        if (
            c1["score"] >= (1.0 - cfg.recency_tiebreak) * c0["score"]
            and c1["t_center"] > c0["t_center"]
        ):
            clusters[0], clusters[1] = clusters[1], clusters[0]

    top = clusters[0]
    support = top["votes"] / float(n)
    summary = [
        (round(c["median"], 2), c["votes"], round(c["mean_conf"], 3),
         round(c["t_first"], 1), round(c["t_last"], 1))
        for c in clusters[:4]
    ]

    if (
        support >= cfg.stable_frac
        and top["votes"] >= cfg.stable_min_votes
        and top["span"] <= cfg.stable_max_span
    ):
        conf = float(
            np.clip(
                0.30 + 0.45 * support + 0.20 * min(1.0, top["mean_conf"]),
                0.0,
                0.95,
            )
        )
        return RawClusterVerdict(
            status="stable",
            weight=round(top["median"], 2),
            confidence=round(conf, 3),
            support_frac=round(support, 3),
            votes=top["votes"],
            n_samples=n,
            clusters=summary,
        )

    if support >= cfg.conflict_min_frac and top["votes"] >= cfg.conflict_min_votes:
        second = clusters[1] if len(clusters) > 1 else None
        if second is not None:
            reason = (
                f"cluster_conflict:{top['median']:.2f}x{top['votes']}"
                f"_vs_{second['median']:.2f}x{second['votes']}"
            )
        else:
            reason = "weak_support"
        conf = float(
            np.clip(0.25 + 0.30 * support + 0.15 * min(1.0, top["mean_conf"]), 0.0, 0.55)
        )
        return RawClusterVerdict(
            status="conflict",
            weight=round(top["median"], 2),
            confidence=round(conf, 3),
            reason=reason,
            support_frac=round(support, 3),
            votes=top["votes"],
            n_samples=n,
            clusters=summary,
        )

    return RawClusterVerdict(
        status="insufficient",
        n_samples=n,
        support_frac=round(support, 3),
        votes=top["votes"],
        clusters=summary,
    )
