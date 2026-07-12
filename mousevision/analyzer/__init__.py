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
        if duration_ms < window_ms * 0.5:
            # Short session: use median of middle half as the platform.
            mid0 = len(weights) // 4
            mid1 = max(mid0 + 1, (3 * len(weights)) // 4)
            return self._result_from_platform(
                times, weights, confs, indices, mid0, mid1
            )

        best_score = -1.0
        best: tuple[int, int, float, float] | None = None  # i0, i1, median, std

        for i in range(len(weights)):
            j = i
            while j < len(weights) and (times[j] - times[i]) <= window_ms:
                j += 1
            if j - i < 3:
                continue
            segment = weights[i:j]
            std = float(np.std(segment))
            if std > self.config.platform_max_std:
                continue
            median = float(np.median(segment))
            # Prefer long + stable platforms; weight only as tiny tiebreaker.
            length_score = min(1.0, (j - i) / 15.0)
            stability = max(0.0, 1.0 - std / self.config.platform_max_std)
            score = length_score + stability + median * 0.001
            if score > best_score:
                best_score = score
                best = (i, j, median, std)

        if best is None:
            # Fallback: lowest-std window ignoring max_std hard cut.
            best_std = 1e9
            for i in range(len(weights)):
                j = i
                while j < len(weights) and (times[j] - times[i]) <= window_ms:
                    j += 1
                if j - i < 3:
                    continue
                segment = weights[i:j]
                std = float(np.std(segment))
                if std < best_std:
                    best_std = std
                    best = (i, j, float(np.median(segment)), std)

        if best is None:
            return None

        i0, i1, _median, _std = best
        return self._result_from_platform(times, weights, confs, indices, i0, i1)

    def _result_from_platform(
        self,
        times: np.ndarray,
        weights: np.ndarray,
        confs: np.ndarray,
        indices: np.ndarray,
        i0: int,
        i1: int,
    ) -> AnalysisResult:
        segment = weights[i0:i1]
        median = float(np.median(segment))
        std = float(np.std(segment))
        final_weight = round(median, 2)
        conf = self._confidence(i1 - i0, std, float(np.mean(confs[i0:i1])))
        photo_i, observed, delta, selection = select_photo_frame(
            times,
            weights,
            confs,
            indices,
            i0,
            i1,
            final_weight,
        )
        return AnalysisResult(
            weight=final_weight,
            confidence=conf,
            platform_start_ms=float(times[i0]),
            platform_end_ms=float(times[i1 - 1]),
            photo_frame_index=photo_i,
            photo_observed_weight=observed,
            photo_weight_delta=delta,
            photo_selection=selection,
            weight_source="stable_curve_median",
        )

    def _confidence(self, n: int, std: float, reader_conf: float) -> float:
        length = min(1.0, n / 10.0)
        stability = max(0.0, 1.0 - std / max(self.config.platform_max_std, 1e-6))
        conf = 0.40 * length + 0.40 * stability + 0.20 * float(np.clip(reader_conf, 0, 1))
        return float(round(np.clip(conf, 0.0, 1.0), 3))
