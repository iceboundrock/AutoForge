"""Repository-level locking: at most one controller per working repo.

Implemented with POSIX flock(2) on ``<state_dir>/controller.lock`` (LOCK_EX |
LOCK_NB). A second instance fails fast with LockError instead of operating
concurrently on the same repo.

The lock entry is opened with ``O_NOFOLLOW`` and must be a regular file
with exactly one name (``st_nlink == 1``): a ``controller.lock`` that is a
symbolic link, FIFO, socket, device or directory, or a hard link sharing its
inode with another file, is refused with LockError before anything is
written, so a tampered or damaged entry can neither redirect the PID write to
an unrelated file nor turn the refusal into an unhandled traceback. Every
open / flock / write failure is a LockError and the descriptor is closed on
the way out.

Tampering model: these checks validate the entry *as found* (damaged or
tampered at rest). They are taken on the open descriptor, but a writer with
write access to the state directory who races between the ``fstat`` and the
PID write (for example by adding a hard link in that window) can still be
affected by the write. Such a writer has the controller's own privileges and
is outside the trust boundary the lock defends; the state directory is
assumed to be writable only by the operating user.
"""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
from types import TracebackType

from .errors import LockError
from .state import entry_kind

_OPEN_FLAGS = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class ControllerLock:
    """Non-reentrant exclusive lock.

    Held for a whole CLI command via ``ControllerEngine.locked()`` (from the
    state load / creation through the last persisted step); ``engine.step()``
    / ``engine.run()`` acquire it per call only when used outside that block.
    """

    def __init__(self, lock_path: str | Path) -> None:
        self.lock_path = Path(lock_path)
        self._fd: int | None = None

    def _refuse(self, reason: str) -> LockError:
        return LockError(f"cannot use {self.lock_path} as the controller lock: {reason}")

    def _open(self) -> int:
        """Open the lock entry itself, never through a symlink, as a regular file."""
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise self._refuse(f"cannot create its directory: {exc}") from exc
        try:
            # O_NOFOLLOW: a symlink named controller.lock fails with ELOOP
            # instead of being followed (also with O_CREAT, dangling or not).
            # O_NONBLOCK: a FIFO cannot stall the open; it is refused below.
            fd = os.open(self.lock_path, _OPEN_FLAGS, 0o644)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                raise self._refuse(
                    "it is a symbolic link; remove it and re-run "
                    "(the link target has not been touched)"
                ) from exc
            if exc.errno == errno.EISDIR:
                raise self._refuse("it is a directory, not a regular file") from exc
            raise self._refuse(f"cannot open it: {exc}") from exc
        try:
            st = os.fstat(fd)
        except OSError as exc:
            os.close(fd)
            raise self._refuse(f"cannot stat it: {exc}") from exc
        kind = entry_kind(st.st_mode)
        if kind is not None:
            os.close(fd)
            raise self._refuse(f"it is a {kind}, not a regular file")
        if st.st_nlink != 1:
            # A hard link passes O_NOFOLLOW and S_ISREG but shares its inode
            # with another name: truncating / writing the PID would modify
            # that other file. Refuse; nothing has been written.
            os.close(fd)
            raise self._refuse(
                f"it has {st.st_nlink} hard links, so it shares its inode with "
                "another file; remove it and re-run (the other file has not been touched)"
            )
        return fd

    def acquire(self) -> ControllerLock:
        fd = self._open()
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LockError(
                    f"another AutoForge controller holds {self.lock_path} — "
                    "refusing to run concurrently on the same repository"
                ) from exc
            except OSError as exc:
                raise self._refuse(f"flock failed: {exc}") from exc
            try:
                # Replace, do not append: the file holds exactly the current PID.
                os.ftruncate(fd, 0)
                os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            except OSError as exc:
                raise self._refuse(f"cannot record the holder PID: {exc}") from exc
        except BaseException:
            os.close(fd)  # also drops the flock if it was taken
            raise
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> ControllerLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
