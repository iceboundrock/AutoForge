"""Repository-level locking: at most one controller per working repo.

Implemented with POSIX flock(2) on ``<state_dir>/controller.lock`` (LOCK_EX |
LOCK_NB). A second instance fails fast with LockError instead of operating
concurrently on the same repo.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType
from typing import IO, Any

from .errors import LockError


class ControllerLock:
    """Non-reentrant exclusive lock.

    Held for a whole CLI command via ``ControllerEngine.locked()`` (from the
    state load / creation through the last persisted step); ``engine.step()``
    / ``engine.run()`` acquire it per call only when used outside that block.
    """

    def __init__(self, lock_path: str | Path) -> None:
        self.lock_path = Path(lock_path)
        self._fh: IO[Any] | None = None

    def acquire(self) -> ControllerLock:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise LockError(
                f"another AutoForge controller holds {self.lock_path} — "
                "refusing to run concurrently on the same repository"
            ) from exc
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._fh = fh
        return self

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def __enter__(self) -> ControllerLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
