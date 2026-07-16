"""Weight curve platform analyzer."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from mousevision.types import AnalysisResult, CurvePoint


@dataclass
class CurveAnalyzerConfig:
    platform_window_seconds: float = 0.8
    platform_max_std: float = 0.35
    near_zero: float = 0.5
    # Photo selection no longer couples to weight matching; these are kept
    # for backward config compatibility but no longer gate photo selection.
    photo_match_tol: float = 0.02
    photo_min_confidence: float = 0.45
    # Defenses against OCR misreads feeding the platform picker.
    min_reader_confidence: float = 0.35
    max_jump_grams: float = 5.0
    # Soft jump handling: down-weight spikes instead of deleting from the curve.
    drop_jump_outliers: bool = False
    jump_confidence_penalty: float = 0.25
    min_platform_points: int = 3
    prefer_nonzero_platform: bool = True
    # A scale can show a short, stable intermediate value before settling.
    # Prefer a later equally stable platform without looking at its magnitude.
    settlement_recency_weight: float = 0.40
    # Cap confidence when we only have a guessed (unstable) weight.
    unstable_confidence_cap: float = 0.35


def select_photo_frame(
    times: np.ndarray,
    weights: np.ndarray,
    confs: np.ndarray,
    indices: np.ndarray,
    i0: int,
    i1: int,
    final_weight: float,
) -> tuple[int, float, float, str]:
    """Pick a representative frame inside the stable platform [i0:i1).

    The photo proves the mouse was on the scale; the weight comes from the
    curve median. So selection prefers the platform midpoint (most likely to
    show a settled mouse), then higher OCR confidence, with weight-delta as
    only a minor tiebreaker. This intentionally does NOT require the photo's
    OCR reading to match the final weight.
    """
    if i1 <= i0:
        raise ValueError("empty platform window")

    mid_t = 0.5 * (float(times[i0]) + float(times[i1 - 1]))
    platform = list(range(i0, i1))

    def sort_key(k: int) -> tuple[float, float, float]:
        # 1. Closest to platform midpoint (settled mouse, best framing)
        # 2. Higher OCR confidence (clearer read, sharper image)
        # 3. Smaller weight delta (minor tiebreaker only)
        return (
            abs(float(times[k]) - mid_t),
            -float(confs[k]),
            abs(float(weights[k]) - final_weight),
        )

    best = min(platform, key=sort_key)
    observed = float(weights[best])
    delta = abs(observed - final_weight)
    return int(indices[best]), round(observed, 2), round(delta, 3), "platform_midpoint"


def _iqr_keep_mask(values: np.ndarray) -> np.ndarray:
    """Boolean mask keeping points within [Q1 - 1.5 IQR, Q3 + 1.5 IQR]."""
    if len(values) < 4:
        return np.ones(len(values), dtype=bool)
    q1 = float(np.percentile(values, 25))
    q3 = float(np.percentile(values, 75))
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    return (values >= lo) & (values <= hi)


def _filter_curve(
    times: np.ndarray,
    weights: np.ndarray,
    confs: np.ndarray,
    indices: np.ndarray,
    cfg: CurveAnalyzerConfig,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Drop low-confidence points; soft-penalize (or optionally drop) jump spikes.

    Raw session curves are preserved upstream; this only affects platform picking.
    """
    keep = confs >= cfg.min_reader_confidence
    if not np.any(keep):
        return times, weights, confs, indices
    times, weights, confs, indices = times[keep], weights[keep], confs[keep], indices[keep]

    if len(weights) < 2:
        return times, weights, confs, indices

    jump_keep = np.ones(len(weights), dtype=bool)
    adjusted = confs.astype(np.float64, copy=True)
    for i in range(1, len(weights) - 1):
        prev_w, cur_w, next_w = float(weights[i - 1]), float(weights[i]), float(weights[i + 1])
        # Isolated spike: far from both neighbors while neighbors agree.
        if (
            abs(cur_w - prev_w) > cfg.max_jump_grams
            and abs(cur_w - next_w) > cfg.max_jump_grams
            and abs(prev_w - next_w) <= cfg.max_jump_grams
        ):
            if cfg.drop_jump_outliers:
                jump_keep[i] = False
            else:
                adjusted[i] = max(0.0, float(adjusted[i]) - cfg.jump_confidence_penalty)
    if cfg.drop_jump_outliers:
        return times[jump_keep], weights[jump_keep], adjusted[jump_keep], indices[jump_keep]
    return times, weights, adjusted, indices


class WeightCurveAnalyzer:
    def __init__(self, config: CurveAnalyzerConfig | None = None) -> None:
        self.config = config or CurveAnalyzerConfig()

    def analyze(self, curve: list[CurvePoint]) -> AnalysisResult | None:
        if len(curve) < 5:
            return None

        times = np.array([p.timestamp_ms for p in curve], dtype=np.float64)
        weights = np.array([p.weight for p in curve], dtype=np.float64)
        confs = np.array([p.confidence for p in curve], dtype=np.float64)
        indices = np.array([p.frame_index for p in curve], dtype=np.int64)

        times, weights, confs, indices = _filter_curve(
            times, weights, confs, indices, self.config
        )
        if len(weights) < 5:
            return None

        # Trim leading / trailing near-zero segments.
        nonzero = weights > self.config.near_zero
        if not np.any(nonzero):
            return None
        first = int(np.argmax(nonzero))
        last = int(len(weights) - 1 - np.argmax(nonzero[::-1]))
        if last - first < 3:
            return None

        times = times[first : last + 1]
        weights = weights[first : last + 1]
        confs = confs[first : last + 1]
        indices = indices[first : last + 1]

        duration_ms = float(times[-1] - times[0])
        window_ms = self.config.platform_window_seconds * 1000.0
        # Short sessions no longer bypass std stability: they fall through to
        # the normal sliding-window search (and to unstable/None if none fit).

        candidates: list[tuple[float, int, int, float, float]] = []
        for i in range(len(weights)):
            j = i
            while j < len(weights) and (times[j] - times[i]) <= window_ms:
                j += 1
            if j - i < self.config.min_platform_points:
                continue
            segment = weights[i:j]
            std = float(np.std(segment))
            if std > self.config.platform_max_std:
                continue
            median = float(np.median(segment))
            length_score = min(1.0, (j - i) / 15.0)
            stability = max(0.0, 1.0 - std / self.config.platform_max_std)
            recency = float(i) / float(max(1, len(weights) - 1))
            score = (
                length_score
                + stability
                + median * 0.001
                + self.config.settlement_recency_weight * recency
            )
            # Prefer real weighing platforms over stable OCR-zero plateaus.
            if self.config.prefer_nonzero_platform and median <= self.config.near_zero:
                score *= 0.05
            candidates.append((score, i, j, median, std))

        best: tuple[int, int, float, float] | None = None
        if candidates:
            # Classic 4↔9 (~5g): down-weight the higher twin when both exist.
            adjusted: list[tuple[float, int, int, float, float]] = []
            for score, i0, i1, median, std in candidates:
                pen = 1.0
                for _s2, _a, _b, median2, _std2 in candidates:
                    if (
                        4.5 <= abs(median - median2) <= 5.5
                        and median > median2
                        and median2 > self.config.near_zero
                    ):
                        pen = min(pen, 0.55)
                adjusted.append((score * pen, i0, i1, median, std))
            adjusted.sort(key=lambda c: c[0], reverse=True)
            _score, i0, i1, median, std = adjusted[0]
            best = (i0, i1, median, std)

        unstable_fallback = False
        if best is None:
            # No window passed platform_max_std — still pick a lowest-std guess,
            # but mark as no_stable_platform (do not silently write clean).
            unstable_fallback = True
            best_std = 1e9
            for i in range(len(weights)):
                j = i
                while j < len(weights) and (times[j] - times[i]) <= window_ms:
                    j += 1
                if j - i < self.config.min_platform_points:
                    continue
                segment = weights[i:j]
                std = float(np.std(segment))
                median = float(np.median(segment))
                # Still de-prioritize near-zero fallbacks when any heavier window exists.
                rank_std = std + (10.0 if median <= self.config.near_zero else 0.0)
                if rank_std < best_std:
                    best_std = rank_std
                    best = (i, j, median, std)

        if best is None:
            return None

        i0, i1, _median, _std = best
        return self._result_from_platform(
            times, weights, confs, indices, i0, i1, unstable=unstable_fallback
        )

    def _result_from_platform(
        self,
        times: np.ndarray,
        weights: np.ndarray,
        confs: np.ndarray,
        indices: np.ndarray,
        i0: int,
        i1: int,
        *,
        unstable: bool = False,
    ) -> AnalysisResult:
        segment = weights[i0:i1]
        seg_confs = confs[i0:i1]
        mask = _iqr_keep_mask(segment)
        filtered = segment[mask]
        filtered_confs = seg_confs[mask]
        if len(filtered) < max(2, self.config.min_platform_points - 1):
            filtered = segment
            filtered_confs = seg_confs
            mask = np.ones(len(segment), dtype=bool)

        median = float(np.median(filtered))
        std = float(np.std(filtered))
        final_weight = round(median, 2)
        conf = self._confidence(len(filtered), std, float(np.mean(filtered_confs)))

        # Map filtered indices back for photo selection.
        kept_local = np.where(mask)[0]
        if len(kept_local) == 0:
            kept_local = np.arange(i0, i1) - i0
        photo_times = times[i0:i1][kept_local]
        photo_weights = weights[i0:i1][kept_local]
        photo_confs = confs[i0:i1][kept_local]
        photo_indices = indices[i0:i1][kept_local]
        photo_i, observed, delta, selection = select_photo_frame(
            photo_times,
            photo_weights,
            photo_confs,
            photo_indices,
            0,
            len(photo_times),
            final_weight,
        )

        needs_review = False
        reasons: list[str] = []
        requires_manual = False
        guessed: float | None = None
        weight_source = "stable_curve_median"
        if len(filtered) < self.config.min_platform_points:
            needs_review = True
            reasons.append("few_platform_points")
        if std > self.config.platform_max_std:
            needs_review = True
            reasons.append("high_platform_std")
        if final_weight <= self.config.near_zero:
            needs_review = True
            reasons.append("near_zero_weight")
        if unstable:
            needs_review = True
            requires_manual = True
            guessed = final_weight
            weight_source = "guessed_unstable"
            if "no_stable_platform" not in reasons:
                reasons.append("no_stable_platform")
            conf = min(float(conf), float(self.config.unstable_confidence_cap))

        return AnalysisResult(
            weight=final_weight,
            confidence=conf,
            platform_start_ms=float(times[i0]),
            platform_end_ms=float(times[i1 - 1]),
            photo_frame_index=photo_i,
            photo_observed_weight=observed,
            photo_weight_delta=delta,
            photo_selection=selection,
            weight_source=weight_source,
            needs_review=needs_review,
            review_reason=",".join(reasons),
            guessed_weight=guessed,
            requires_manual_weight=requires_manual,
        )

    def _confidence(self, n: int, std: float, reader_conf: float) -> float:
        length = min(1.0, n / 10.0)
        stability = max(0.0, 1.0 - std / max(self.config.platform_max_std, 1e-6))
        conf = 0.40 * length + 0.40 * stability + 0.20 * float(np.clip(reader_conf, 0, 1))
        return float(round(np.clip(conf, 0.0, 1.0), 3))
