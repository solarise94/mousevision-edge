"""Temporal fusion of single-frame OCR observations into stable weights."""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from math import exp

from mousevision.four_nine import is_four_nine_pair, prefer_four, resolve_four_nine_clusters
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
    # No animal-weight floor: valid low weights must not be silently dropped.
    min_weight: float = 0.0
    max_weight: float = 50.0
    # Mild stick: suppress tiny plateau jitter (22.72↔22.80) without locking
    # wrong first clusters like 29.x forever.
    stick_tol: float = 0.20
    # Minimum weight gap (grams) between two clusters to count as a real
    # conflict. Gaps below this are OCR jitter, not genuine disagreement.
    # Typical LCD repeatability is ~0.05g; 0.50 absorbs scale wobble +
    # last-digit flicker without masking real 4↔9 (~5g) or 1↔blank (~8g).
    conflict_weight_tol: float = 0.50
    # Bound the mouse-on-scale zero hold (re-tare transient). After this many
    # consecutive held zero frames the zero is emitted anyway — a long zero run
    # means the animal really left and the mouse detector is stale/false.
    # 0 disables the bound (legacy behaviour: hold forever).
    zero_hold_max_frames: int = 0
    # Time-weighted voting: recent reads get exponentially more weight.
    # half_life_ms is the time (ms) for a read's weight to decay to 50%.
    # 0 disables time weighting (all reads in the window are equal).
    time_weight_half_life_ms: float = 0.0


@dataclass
class TemporalWeightFusion:
    """Per-SessionDriver fusion state — no shared server session_id."""

    config: TemporalFusionConfig = field(default_factory=TemporalFusionConfig)
    _recent: deque[RawWeightObservation] = field(default_factory=deque)
    _last_stable: StableWeightObservation | None = None
    last_needs_review: bool = False
    last_review_reason: str = ""
    _zero_hold_count: int = 0
    _latest_ts_ms: float = 0.0

    def __post_init__(self) -> None:
        self._recent = deque(maxlen=self.config.window_size)

    def reset(self) -> None:
        self._recent.clear()
        self._last_stable = None
        self.last_needs_review = False
        self.last_review_reason = ""
        self._zero_hold_count = 0
        self._latest_ts_ms = 0.0

    def update(
        self,
        obs: RawWeightObservation,
        *,
        mouse_present: bool | None = None,
        timestamp_ms: float = 0.0,
    ) -> StableWeightObservation | None:
        """Return a stable observation for the state machine, or None to hold."""
        if timestamp_ms > 0:
            self._latest_ts_ms = float(timestamp_ms)
        # Tag observation with timestamp for time-weighted clustering.
        if timestamp_ms > 0:
            obs._ts_ms = float(timestamp_ms)
        self._recent.append(obs)
        self.last_needs_review = False
        self.last_review_reason = ""

        # Mouse on scale + zero display → transition, do not emit leave signal.
        # Bounded: a long zero run is a real empty scale (detector was stale).
        if mouse_present and obs.is_zero_display:
            self._zero_hold_count += 1
            if (
                self.config.zero_hold_max_frames <= 0
                or self._zero_hold_count <= self.config.zero_hold_max_frames
            ):
                return None
        else:
            self._zero_hold_count = 0

        # Negative display: pan rebounded below tare — unambiguous empty-scale
        # evidence (a held or partly supported animal still reads positive).
        # Emit 0.0 directly; never subject to the mouse-on-scale hold.
        if obs.is_negative_display:
            self._last_stable = None
            return StableWeightObservation(
                weight=0.0,
                confidence=float(obs.confidence or obs.quality),
                digits=list(obs.digits),
                reason="negative_display",
                screen_quad=obs.screen_quad,
            )

        if obs.status in {"unreadable", "bad_roi", "transition"}:
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
                and is_four_nine_pair(float(w0), float(w1))
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
                # Tiny gap → OCR jitter, not a real conflict. Merge into
                # the top cluster silently (no review flag).
                if abs(w0 - w1) < self.config.conflict_weight_tol:
                    pass  # fall through to normal emit (top cluster wins)
                # Classic 4↔9 (~5g): break the tie with confidence / prefer '4'.
                elif is_four_nine_pair(w0, w1):
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
                else:
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
                    if second_count >= self.config.min_agree:
                        # Genuine dual plateau (both sides well supported): hold
                        # the output — the raw-cluster analyzer resolves it later.
                        return None
                    # Weak minority (1-2 flicker reads): emit the majority anyway
                    # so the state machine is not starved; the review flag above
                    # still reaches the session record.
                    # (fall through to the normal emit path)

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
        half_life = float(self.config.time_weight_half_life_ms)
        use_time_weight = half_life > 0 and self._latest_ts_ms > 0

        buckets: dict[float, list[tuple[RawWeightObservation, float]]] = {}
        for obs in self._recent:
            if not obs.is_readable or obs.weight is None:
                continue
            if not (
                self.config.min_weight <= float(obs.weight) <= self.config.max_weight
            ):
                continue
            if not self._passes_slot_gates(obs):
                continue
            # Time-decay weight: recent reads count more.
            if use_time_weight:
                obs_ts = float(getattr(obs, "_ts_ms", 0.0) or 0.0)
                if obs_ts > 0:
                    age_ms = max(0.0, self._latest_ts_ms - obs_ts)
                    tw = exp(-0.693147 * age_ms / half_life)  # ln2 ≈ 0.693147
                else:
                    tw = 1.0
            else:
                tw = 1.0

            key = round(float(obs.weight), 2)
            matched = None
            for existing in buckets:
                if abs(existing - key) <= self.config.weight_tol:
                    matched = existing
                    break
            if matched is None:
                buckets[key] = [(obs, tw)]
            else:
                buckets[matched].append((obs, tw))

        ranked: list[tuple[float, int, float, list[str], list[list[float]] | None]] = []
        for key, items in buckets.items():
            total_tw = sum(tw for _, tw in items)
            conf = float(
                sum(float(o.confidence or o.quality) * tw for o, tw in items)
                / max(total_tw, 1e-9)
            )
            dig_counter: Counter[tuple[str, ...]] = Counter(
                tuple(o.digits) for o, _ in items if o.digits
            )
            digits = list(dig_counter.most_common(1)[0][0]) if dig_counter else []
            quad = next(
                (o.screen_quad for o, _ in reversed(items) if o.screen_quad), None
            )
            # Effective vote count: sum of time weights (>= raw count when
            # time weighting is off, since all tw=1.0).
            effective_votes = int(round(total_tw)) if use_time_weight else len(items)
            ranked.append((key, max(1, effective_votes), conf, digits, quad))
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
        # Apply 4↔9 resolution on the top two clusters.
        if len(ranked) >= 2:
            ranked = resolve_four_nine_clusters(
                ranked,
                weight_idx=0,
                votes_idx=1,
                conf_idx=2,
                digits_idx=3,
                min_votes=self.config.min_agree,
            )
        return ranked
