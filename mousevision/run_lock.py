"""Cross-process exclusive lock for per-run state transitions.

Covers release-suspect / reject-suspect / reject recovery for a single run
directory. Uses ``fcntl.flock`` on a lock file under the run dir so concurrent
threads *and* multi-process uvicorn workers serialize correctly.
"""

from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

LOCK_NAME = ".run_state.lock"


class RunLockTimeout(TimeoutError):
    """Raised when a run-dir lock cannot be acquired within the timeout."""


@contextmanager
def run_dir_lock(
    run_dir: str | Path,
    *,
    timeout_sec: float = 60.0,
    poll_sec: float = 0.05,
) -> Iterator[None]:
    """Exclusive lock on ``run_dir / .run_state.lock``.

    Blocks until acquired or ``timeout_sec`` elapses. Always releases the
    flock and closes the fd on exit (even on exception).
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / LOCK_NAME
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RunLockTimeout(
                        f"timeout acquiring run lock: {lock_path}"
                    ) from None
                time.sleep(poll_sec)
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass
