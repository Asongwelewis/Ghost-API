"""Filesystem helpers that stay correct when several agents write at the same time."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_TIMEOUT = 30.0


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file and rename, so readers never see half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    for attempt in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: another process has the target open for a moment
            if attempt == 49:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.02)


@contextmanager
def exclusive_lock(path: Path, timeout: float = LOCK_TIMEOUT) -> Iterator[None]:
    """Process-wide exclusive lock on `path` (created if missing). Raises TimeoutError."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        deadline = time.monotonic() + timeout
        while not _try_lock(handle):
            if time.monotonic() > deadline:
                raise TimeoutError(f"{path} is held by another process")
            time.sleep(0.02)
        try:
            yield
        finally:
            _unlock(handle)


if sys.platform == "win32":
    import msvcrt

    def _try_lock(handle) -> bool:  # type: ignore[no-untyped-def]
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(handle) -> None:  # type: ignore[no-untyped-def]
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(handle) -> bool:  # type: ignore[no-untyped-def]
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(handle) -> None:  # type: ignore[no-untyped-def]
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
