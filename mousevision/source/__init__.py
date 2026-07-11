"""Frame source protocol."""

from __future__ import annotations

from typing import Iterator, Protocol

from mousevision.types import Frame


class FrameSource(Protocol):
    """Abstract camera / video input. Android CameraX will implement the same contract."""

    def frames(self) -> Iterator[Frame]:
        ...

    def close(self) -> None:
        ...
