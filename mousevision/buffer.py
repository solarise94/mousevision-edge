"""Ring frame buffer keyed by timestamp, with session pinning."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from mousevision.types import Frame, WeightSample


@dataclass
class BufferedItem:
    frame: Frame
    weight: float | None = None
    weight_confidence: float = 0.0


@dataclass
class RingFrameBuffer:
    """Keep recent frames; optionally pin all frames after session start."""

    window_seconds: float = 12.0
    max_items: int = 400
    _items: deque[BufferedItem] = field(default_factory=deque)
    _pin_from_ms: float | None = None

    def pin_from(self, timestamp_ms: float | None) -> None:
        """While set, do not trim frames at/after this timestamp (session retention)."""
        self._pin_from_ms = timestamp_ms

    def clear_pin(self) -> None:
        self._pin_from_ms = None

    def push(
        self,
        frame: Frame,
        weight: float | None = None,
        weight_confidence: float = 0.0,
    ) -> None:
        self._items.append(
            BufferedItem(frame=frame, weight=weight, weight_confidence=weight_confidence)
        )
        self._trim(frame.timestamp_ms)

    def _trim(self, now_ms: float) -> None:
        cutoff = now_ms - self.window_seconds * 1000.0
        pin = self._pin_from_ms
        while self._items:
            oldest_ts = self._items[0].frame.timestamp_ms
            over_cap = len(self._items) > self.max_items
            before_cutoff = oldest_ts < cutoff
            pinned = pin is not None and oldest_ts >= pin

            if pinned and not over_cap:
                break
            if not before_cutoff and not over_cap:
                break
            # Drop oldest: either expired (and not pinned), or over capacity.
            self._items.popleft()

    def clear(self) -> None:
        self._items.clear()
        self._pin_from_ms = None

    def __len__(self) -> int:
        return len(self._items)

    def items(self) -> list[BufferedItem]:
        return list(self._items)

    def frames(self) -> list[Frame]:
        return [item.frame for item in self._items]

    def weight_samples(self) -> list[WeightSample]:
        samples: list[WeightSample] = []
        for item in self._items:
            if item.weight is None:
                continue
            samples.append(
                WeightSample(
                    timestamp_ms=item.frame.timestamp_ms,
                    weight=item.weight,
                    confidence=item.weight_confidence,
                    frame_index=item.frame.index,
                )
            )
        return samples

    def frame_by_index(self, frame_index: int) -> Frame | None:
        for item in self._items:
            if item.frame.index == frame_index:
                return item.frame
        return None

    def nearest_frame(self, frame_index: int) -> Frame | None:
        """Exact match, else closest available frame (fallback for photo)."""
        exact = self.frame_by_index(frame_index)
        if exact is not None:
            return exact
        if not self._items:
            return None
        return min(self._items, key=lambda it: abs(it.frame.index - frame_index)).frame
