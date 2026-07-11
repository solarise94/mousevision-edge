"""Video file frame source for Mac PoC."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import cv2

from mousevision.types import Frame


class VideoFileSource:
    def __init__(
        self,
        path: str | Path,
        *,
        frame_stride: int = 1,
        max_frames: int | None = None,
        start_ms: float | None = None,
        end_ms: float | None = None,
    ) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        self.frame_stride = max(1, frame_stride)
        self.max_frames = max_frames
        self.start_ms = start_ms
        self.end_ms = end_ms
        self._cap: cv2.VideoCapture | None = None

    def frames(self) -> Iterator[Frame]:
        self._cap = cv2.VideoCapture(str(self.path))
        if not self._cap.isOpened():
            raise RuntimeError(f"Cannot open video: {self.path}")

        fps = float(self._cap.get(cv2.CAP_PROP_FPS) or 30.0)
        if fps <= 1e-3:
            fps = 30.0

        index = 0
        if self.start_ms is not None and self.start_ms > 0:
            index = max(0, int(round(self.start_ms / 1000.0 * fps)))
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, float(index))

        emitted = 0
        while True:
            ok, image = self._cap.read()
            if not ok:
                break
            timestamp_ms = (index / fps) * 1000.0
            if self.end_ms is not None and timestamp_ms > self.end_ms:
                break
            if index % self.frame_stride == 0:
                yield Frame(image=image, timestamp_ms=timestamp_ms, index=index)
                emitted += 1
                if self.max_frames is not None and emitted >= self.max_frames:
                    break
            index += 1

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def __enter__(self) -> "VideoFileSource":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
