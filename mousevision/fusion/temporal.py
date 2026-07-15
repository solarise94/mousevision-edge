"""Temporal fusion of single-frame OCR observations into stable weights."""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field

from mousevision.reader.observations import RawWeightObservation, StableWeightObservation


@dataclass
class TemporalFusionConfig:
    window_size: int = 8
    min_agree: int = 3
    # Second cluster needs this many votes to count as a conflict (can be < min_agree).
    conflict_min_agree: int = 2
    weight_tol: float = 0.08
    min_confidence: float = 0.45
    # Classic seven-seg slot conf for 1/7 is often ~0.55–0.70; 0.70 was too harsh.
    one_seven_min_confidence: float = 0.55
    cluster_conflict_ratio: float = 0.35  # second cluster / top cluster
    near_zero: float = 0.15
    min_weight: float = 5.0
    max_weight: float = 50.0
    # Mild stick: suppress tiny plateau jitter (22.72↔22.80) without locking
    # wrong first clusters like 29.x forever.
    stick_tol: float = 0.20


@dataclass
class TemporalWeightFusion:
    """Per-SessionDriver fusion state — no shared server session_id."""

    config: TemporalFusionConfig = field(default_factory=TemporalFusionConfig)
    _recent: deque[RawWeightObservation] = field(default_factory=deque)
    _last_stable: StableWeightObservation | None = None
    last_needs_review: bool = False
    last_review_reason: str = ""

    def __post_init__(self) -> None:
        self._recent = deque(maxlen=self.config.window_size)

    def reset(self) -> None:
        self._recent.clear()
        self._last_stable = None
        self.last_needs_review = False
        self.last_review_reason = ""

    def update(
        self,
        obs: RawWeightObservation,
        *,
        mouse_present: bool | None = None,
        timestamp_ms: float = 0.0,
    ) -> StableWeightObservation | None:
        """Return a stable observation for the state machine, or None to hold."""
        del timestamp_ms  # reserved for future time-weighted rules
        self._recent.append(obs)
        self.last_needs_review = False
        self.last_review_reason = ""

        # Mouse on scale + zero display → transition, do not emit leave signal.
        if mouse_present and obs.is_zero_display:
            return None

        if obs.status in {"unreadable", "bad_roi"}:
            return None

        if obs.is_zero_display:
            self._last_stable = None
            return StableWeightObservation(
                weight=0.0,
                confidence=float(obs.confidence or obs.quality),
                digits=list(obs.digits),
                reason="zero_display",
                screen_quad=obs.screen_quad,
            )

        if not obs.is_readable:
            return None

        if obs.weight is None or not (
            self.config.min_weight <= float(obs.weight) <= self.config.max_weight
        ):
            return None

        if not self._passes_slot_gates(obs):
            return None

        clusters = self._cluster_readable()
        if not clusters:
            return None

        # 4↔9 confusion (~5g): prefer the higher-confidence cluster instead of
        # blindly taking the vote leader (24.18 vs 29.18).
        if len(clusters) >= 2:
            w0, n0, c0, d0, q0 = clusters[0]
            w1, n1, c1, d1, q1 = clusters[1]
            if (
                n1 >= self.config.conflict_min_agree
                and abs(float(w0) - float(w1)) >= 4.5
                and abs(float(w0) - float(w1)) <= 5.5
                and float(c1) > float(c0) + 0.02
            ):
                clusters[0], clusters[1] = clusters[1], clusters[0]

        top_weight, top_count, top_conf, top_digits, top_quad = clusters[0]
        if top_count < self.config.min_agree:
            return None

        if len(clusters) >= 2:
            second_count = clusters[1][1]
            if second_count >= self.config.conflict_min_agree and (
                second_count / max(1, top_count) >= self.config.cluster_conflict_ratio
            ):
                w0 = float(clusters[0][0])
                w1 = float(clusters[1][0])
                # Classic 4↔9 (~5g): break the tie with confidence / prefer '4'.
                if 4.5 <= abs(w0 - w1) <= 5.5:
                    pick = max(
                        clusters[:2],
                        key=lambda t: (
                            float(t[2]),
                            1 if len(t[3]) > 1 and t[3][1] == "4" else 0,
                            int(t[1]),
                        ),
                    )
                    if int(pick[1]) >= self.config.min_agree:
                        stable = StableWeightObservation(
                            weight=float(pick[0]),
                            confidence=float(pick[2]),
                            digits=list(pick[3]),
                            reason="four_nine_break",
                            screen_quad=pick[4],
                        )
                        self._last_stable = stable
                        return stable
                if self._last_stable is not None:
                    last_w = float(self._last_stable.weight)
                    for w, n, c, d, q in clusters[:2]:
                        if (
                            n >= self.config.conflict_min_agree
                            and abs(float(w) - last_w) <= self.config.stick_tol
                        ):
                            return StableWeightObservation(
                                weight=last_w,
                                confidence=float(self._last_stable.confidence),
                                digits=list(self._last_stable.digits),
                                reason="sticky_near",
                                screen_quad=self._last_stable.screen_quad,
                            )
                self.last_needs_review = True
                self.last_review_reason = (
                    f"cluster_conflict:{w0}x{top_count}_vs_{w1}x{second_count}"
                )
                return None

        # Mild stick for tiny jitter around an established plateau.
        if self._last_stable is not None and self._last_stable.weight > self.config.near_zero:
            last_w = float(self._last_stable.weight)
            if abs(float(top_weight) - last_w) <= self.config.stick_tol:
                top_weight = last_w
                top_digits = list(self._last_stable.digits) or top_digits
                top_quad = self._last_stable.screen_quad or top_quad

        stable = StableWeightObservation(
            weight=float(top_weight),
            confidence=float(top_conf),
            digits=list(top_digits),
            reason="platform_cluster",
            needs_review=False,
            screen_quad=top_quad,
        )
        self._last_stable = stable
        return stable

    def _passes_slot_gates(self, obs: RawWeightObservation) -> bool:
        if float(obs.confidence or obs.quality) < self.config.min_confidence:
            return False
        digits = obs.digits
        confs = obs.digit_confidences
        for i, d in enumerate(digits):
            if d not in {"1", "7"}:
                continue
            c = float(confs[i]) if i < len(confs) else float(obs.quality)
            if c < self.config.one_seven_min_confidence:
                return False
        return True

    def _cluster_readable(
        self,
    ) -> list[tuple[float, int, float, list[str], list[list[float]] | None]]:
        buckets: dict[float, list[RawWeightObservation]] = {}
        for obs in self._recent:
            if not obs.is_readable or obs.weight is None:
                continue
            if not (
                self.config.min_weight <= float(obs.weight) <= self.config.max_weight
            ):
                continue
            if not self._passes_slot_gates(obs):
                continue
            key = round(float(obs.weight), 2)
            matched = None
            for existing in buckets:
                if abs(existing - key) <= self.config.weight_tol:
                    matched = existing
                    break
            if matched is None:
                buckets[key] = [obs]
            else:
                buckets[matched].append(obs)

        ranked: list[tuple[float, int, float, list[str], list[list[float]] | None]] = []
        for key, items in buckets.items():
            conf = float(sum(float(o.confidence or o.quality) for o in items) / len(items))
            dig_counter: Counter[tuple[str, ...]] = Counter(
                tuple(o.digits) for o in items if o.digits
            )
            digits = list(dig_counter.most_common(1)[0][0]) if dig_counter else []
            quad = next((o.screen_quad for o in reversed(items) if o.screen_quad), None)
            ranked.append((key, len(items), conf, digits, quad))
        # Prefer '4' over '9' in the tens slot when vote count/conf tie — classic
        # seven-seg glare turns 4→9 (24.18→29.18).
        ranked.sort(
            key=lambda t: (
                t[1],
                t[2],
                1 if len(t[3]) > 1 and t[3][1] == "4" else 0,
            ),
            reverse=True,
        )
        return ranked
