"""PlaybackEngine start mutex / run token tests."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from mousevision.upload_queue import UploadQueue
from ui.app import PlaybackEngine
from ui.registry import MouseRegistry


def test_start_rejects_when_previous_thread_still_alive(tmp_path: Path):
    reg = MouseRegistry(tmp_path / "reg.json", tmp_path / "output")
    queue = UploadQueue(tmp_path / "q.db")
    engine = PlaybackEngine(reg, queue)
    engine.output_root = tmp_path / "output"
    engine.output_root.mkdir(parents=True, exist_ok=True)

    blocker = threading.Event()

    def fake_run(self, **kwargs):
        stop_event = kwargs["stop_event"]
        while not blocker.is_set():
            stop_event.wait(0.05)
            time.sleep(0.05)

    engine._run = fake_run.__get__(engine, PlaybackEngine)  # type: ignore[method-assign]

    first = engine.start(cage_id="C57-023", continuous=False, persist=True)
    assert first.get("ok") is True
    assert engine._thread is not None and engine._thread.is_alive()

    second = engine.start(cage_id="C57-023", continuous=False, persist=True, force=False)
    assert second.get("error") == "busy"
    assert second.get("ok") is False

    blocker.set()
    engine.stop()
