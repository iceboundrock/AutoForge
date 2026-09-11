"""Repository-level locking: at most one controller per repository.

Implemented with POSIX flock(2) on the git common directory descriptor
(LOCK_EX | LOCK_NB). A second instance fails fast with LockError instead of
operating concurrently on the same repository. ``autoforge/controller.lock``
remains as a PID record, but is not the lock inode: that keeps a replacement of
the ``autoforge`` directory from creating a second lock domain.

The lock is keyed by the *repository identity*, not by a caller-selectable
path: :func:`repository_lock_path` asks ``git rev-parse --git-common-dir``
for the git directory shared by every working tree of the repository that
contains the controller's working directory. The main checkout, a linked
``git worktree``, any subdirectory, and any ``--state-dir`` therefore all
resolve to the same lock file, so two controllers cannot pick independent
locks for one repository by choosing different state directories or by
being started from different directories. A working directory outside a git
repository cannot be locked and is refused with LockError (nothing is
written). Two *independent clones* of one GitHub repository are two
repositories to this lock; they are not coordinated.

Below the git common dir no path component is ever followed through a
symbolic link. The git common dir (what git reported, already resolved) is
opened as a directory descriptor; the ``autoforge`` directory is created with
``mkdir`` (which never follows a symlink at that name) and then opened
*relative to that descriptor* with ``O_DIRECTORY | O_NOFOLLOW`` and
``fstat``-checked, so an ``autoforge`` entry that is a symbolic link or not a
directory is refused with LockError before ``controller.lock`` is touched;
``controller.lock`` in turn is opened relative to the validated directory
descriptor (``dir_fd``) with ``O_NOFOLLOW``, so a parent replacement cannot
redirect the PID record. The lock itself is taken on the already-open git
common directory descriptor, whose inode is shared by all such attempts. The
lock entry must be a regular
file with exactly one name (``st_nlink == 1``): a ``controller.lock`` that is
a symbolic link, FIFO, socket, device or directory, or a hard link sharing
its inode with another file, is refused with LockError before anything is
written, so a tampered or damaged entry can neither redirect the PID write
to an unrelated file nor turn the refusal into an unhandled traceback. Every
open / flock / write failure is a LockError and every descriptor is closed on
the way out.

Tampering model: these checks validate the entries *as found* (damaged or
tampered at rest). They are taken on open descriptors, but a writer with
write access to the git directory who races between the ``fstat`` and the
PID write (for example by adding a hard link in that window) can still alter
the record. The lock domain itself remains the common-directory inode, so
replacing ``autoforge`` cannot make two controllers acquire independent locks.
The git directory is assumed to be writable only by the operating user.
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from collections.abc import Callable
from pathlib import Path
from types import TracebackType

from .errors import ExecutionError, LockError
from .executor import ExecutionRequest, ExecutionResult, execute
from .safefs import entry_kind

LOCK_DIRNAME = "autoforge"  # inside the git common dir
LOCK_FILENAME = "controller.lock"

_GIT_COMMON_DIR_ARGV = ["git", "rev-parse", "--git-common-dir"]
_GIT_TIMEOUT_SECONDS = 30

Runner = Callable[[ExecutionRequest], ExecutionResult]

_OPEN_FLAGS = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
# The lock's directory: must be a real directory reached without following a
# symlink (O_NOFOLLOW refuses a symlink at that name with ELOOP; O_DIRECTORY
# refuses anything that is not a directory with ENOTDIR).
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# The git common dir itself: already resolved by repository_lock_path.
_BASE_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC


def repository_lock_path(workdir: str | Path, runner: Runner = execute) -> Path:
    """Canonical lock path of the repository that contains ``workdir``.

    ``git rev-parse --git-common-dir`` names the git directory shared by all
    working trees of one repository (for a linked worktree it is the main
    repository's ``.git``, not ``.git/worktrees/<name>``), so the result is
    the same for every subdirectory, every worktree and every state
    directory of that repository. Relative output is resolved against
    ``workdir``; symlinks are resolved so two spellings of one checkout
    agree. Raises LockError when ``workdir`` is not inside a git repository
    or ``git`` cannot be run: without a repository identity there is nothing
    to lock, and the controller must not fall back to a weaker lock.
    """
    base = Path(workdir)
    shown = base.resolve()  # messages only: "." tells the operator nothing
    try:
        res = runner(
            ExecutionRequest(
                command=list(_GIT_COMMON_DIR_ARGV),
                cwd=str(base),
                timeout_seconds=_GIT_TIMEOUT_SECONDS,
            )
        )
    except ExecutionError as exc:
        raise LockError(
            f"cannot derive the controller lock for {shown}: git could not be run ({exc})"
        ) from exc
    if res.timed_out:
        raise LockError(f"cannot derive the controller lock for {shown}: git rev-parse timed out")
    if res.exit_code != 0:
        detail = (res.stderr or res.stdout).strip().splitlines()
        raise LockError(
            f"cannot derive the controller lock: {shown} is not inside a git repository "
            f"({detail[0] if detail else f'git rev-parse exited {res.exit_code}'})"
        )
    common = res.stdout.strip()
    if not common:
        raise LockError(
            f"cannot derive the controller lock for {shown}: git rev-parse returned no git dir"
        )
    git_dir = (base / common).resolve()
    return git_dir / LOCK_DIRNAME / LOCK_FILENAME


class ControllerLock:
    """Non-reentrant exclusive lock.

    Held for a whole CLI command via ``ControllerEngine.locked()`` (from the
    state load / creation through the last persisted step); ``engine.step()``
    / ``engine.run()`` acquire it per call only when used outside that block.
    """

    def __init__(self, lock_path: str | Path) -> None:
        self.lock_path = Path(lock_path)
        self._fd: int | None = None
        self._pid_fd: int | None = None
        self._base_fd: int | None = None

    def _refuse(self, reason: str) -> LockError:
        return LockError(f"cannot use {self.lock_path} as the controller lock: {reason}")

    def _describe_dir_entry(self, base_fd: int) -> str:
        """What sits at the lock directory's name (without following it), for messages."""
        try:
            st = os.lstat(self.lock_path.parent.name, dir_fd=base_fd)
        except OSError:
            return "not a directory; move it out of the way and re-run"
        if stat.S_ISLNK(st.st_mode):
            return "a symbolic link; remove it and re-run (the link target has not been touched)"
        kind = entry_kind(st.st_mode) or "regular file"
        return f"a {kind}, not a directory; move it out of the way and re-run"

    def _open_dir(self) -> int:
        """Descriptor of the lock's directory, reached without following a symlink.

        ``lock_path`` is ``<base>/<dir>/<file>``: ``<base>`` (the git common
        dir, resolved by :func:`repository_lock_path`) is trusted as the
        repository identity and opened as a directory; ``<dir>`` is created
        with ``mkdir`` -- which never follows a symbolic link at that name --
        and then opened *relative to the base descriptor* with
        ``O_DIRECTORY | O_NOFOLLOW``, so a symlink or a non-directory in its
        place is refused (LockError) and its target is never touched.
        """
        lock_dir = self.lock_path.parent
        try:
            base_fd = os.open(lock_dir.parent, _BASE_FLAGS)
        except OSError as exc:
            raise self._refuse(f"cannot open its repository directory: {exc}") from exc
        try:
            try:
                os.mkdir(lock_dir.name, 0o755, dir_fd=base_fd)
            except FileExistsError:
                pass  # validated by the open below; may be a symlink or a file
            except OSError as exc:
                raise self._refuse(f"cannot create its directory {lock_dir}: {exc}") from exc
            try:
                dir_fd = os.open(lock_dir.name, _DIR_FLAGS, dir_fd=base_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
                    # Linux reports a symlink as ENOTDIR under O_DIRECTORY |
                    # O_NOFOLLOW, other systems as ELOOP: name what is there.
                    raise self._refuse(
                        f"its directory {lock_dir} is {self._describe_dir_entry(base_fd)}"
                    ) from exc
                raise self._refuse(f"cannot open its directory {lock_dir}: {exc}") from exc
        except BaseException:
            os.close(base_fd)
            raise
        try:
            st = os.fstat(dir_fd)
        except OSError as exc:
            os.close(dir_fd)
            os.close(base_fd)
            raise self._refuse(f"cannot stat its directory {lock_dir}: {exc}") from exc
        if not stat.S_ISDIR(st.st_mode):
            os.close(dir_fd)
            os.close(base_fd)
            raise self._refuse(f"its directory {lock_dir} is not a directory")
        self._base_fd = base_fd
        return dir_fd

    def _open(self) -> int:
        """Open the lock entry itself, never through a symlink, as a regular file."""
        dir_fd = self._open_dir()
        try:
            try:
                # Relative to the validated directory descriptor: the parent
                # cannot be swapped for a symlink between its check and this
                # open. O_NOFOLLOW: a symlink named controller.lock fails with
                # ELOOP instead of being followed (also with O_CREAT, dangling
                # or not). O_NONBLOCK: a FIFO cannot stall the open; it is
                # refused below.
                fd = os.open(self.lock_path.name, _OPEN_FLAGS, 0o644, dir_fd=dir_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.EMLINK):
                    raise self._refuse(
                        "it is a symbolic link; remove it and re-run "
                        "(the link target has not been touched)"
                    ) from exc
                if exc.errno == errno.EISDIR:
                    raise self._refuse("it is a directory, not a regular file") from exc
                raise self._refuse(f"cannot open it: {exc}") from exc
        except BaseException:
            if self._base_fd is not None:
                os.close(self._base_fd)
                self._base_fd = None
            raise
        finally:
            os.close(dir_fd)
        try:
            st = os.fstat(fd)
        except OSError as exc:
            os.close(fd)
            if self._base_fd is not None:
                os.close(self._base_fd)
                self._base_fd = None
            raise self._refuse(f"cannot stat it: {exc}") from exc
        kind = entry_kind(st.st_mode)
        if kind is not None:
            os.close(fd)
            if self._base_fd is not None:
                os.close(self._base_fd)
                self._base_fd = None
            raise self._refuse(f"it is a {kind}, not a regular file")
        if st.st_nlink != 1:
            # A hard link passes O_NOFOLLOW and S_ISREG but shares its inode
            # with another name: truncating / writing the PID would modify
            # that other file. Refuse; nothing has been written.
            os.close(fd)
            if self._base_fd is not None:
                os.close(self._base_fd)
                self._base_fd = None
            raise self._refuse(
                f"it has {st.st_nlink} hard links, so it shares its inode with "
                "another file; remove it and re-run (the other file has not been touched)"
            )
        return fd

    def acquire(self) -> ControllerLock:
        if self.held:
            raise self._refuse("it is already held by this controller")
        pid_fd = self._open()
        base_fd = self._base_fd
        self._base_fd = None
        if base_fd is None:  # pragma: no cover - _open always retains the base
            os.close(pid_fd)
            raise self._refuse("its repository directory descriptor was lost")
        try:
            try:
                # Lock the stable repository directory inode, not the PID
                # record below `autoforge`, which an agent can replace.
                fcntl.flock(base_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LockError(
                    f"another AutoForge controller holds {self.lock_path} — "
                    "refusing to run concurrently on the same repository"
                ) from exc
            except OSError as exc:
                raise self._refuse(f"flock failed: {exc}") from exc
            try:
                # Replace, do not append: the file holds exactly the current PID.
                os.ftruncate(pid_fd, 0)
                os.write(pid_fd, f"{os.getpid()}\n".encode("ascii"))
            except OSError as exc:
                raise self._refuse(f"cannot record the holder PID: {exc}") from exc
        except BaseException:
            os.close(pid_fd)
            os.close(base_fd)  # also drops the flock if it was taken
            raise
        self._fd = base_fd
        self._pid_fd = pid_fd
        return self

    def release(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        if self._pid_fd is not None:
            pid_fd, self._pid_fd = self._pid_fd, None
            os.close(pid_fd)

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
