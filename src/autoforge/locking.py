"""Repository-level locking: the three lock domains of ADR 0008 (D10-D12).

**L0, orchestration** (:class:`ControllerLock`): at most one orchestration
authority per repository. A standalone mutating command takes it for its
whole lifetime; a parallel EPIC scheduler takes it and shares it with the
workers it launches (below).

It is POSIX flock(2) on the git common directory descriptor
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

**Sharing L0 (D10.2, D10.3).** ``flock`` belongs to an open file
description, so a child that inherits the descriptor (``pass_fds``) holds
the same lock, and ``LOCK_UN`` on any copy would release it for every
holder at once. :meth:`ControllerLock.share` therefore switches the lock to
a *close-only* release before its descriptor is handed out, and
:meth:`ControllerLock.adopt` wraps an inherited descriptor only after
:func:`prove_held` proved it, always close-only and without touching the PID
record. The last ``close`` anywhere releases the lock, so surviving workers
keep the repository closed after their scheduler died. Only an inherited
descriptor proves a worker: no environment variable, PID file or claim.

**L2, per-issue run** (:class:`IssueRunLock`):
``<common>/autoforge/scheduler/issues/<n>/run.lock``, opened with the same
hardening (every directory ``O_NOFOLLOW``, the file ``O_NOFOLLOW |
O_CLOEXEC``, a regular file with one link, never truncated, written or
unlinked), taken ``LOCK_EX | LOCK_NB`` only (:class:`~autoforge.errors.IssueBusy`
when held, never a wait), handed to exactly one child and released by
``close`` only, everywhere. :func:`issue_run_lock_live` is the lock-free
probe that answers "has a live owner?" (D8.3).

**L1, shared Git** (:class:`SharedGitLock`): ``<common>/autoforge/git.lock``,
the same hardening, around every controller write to git's shared data in
the common dir (its object store and its worktree registry, D12.1). It is
the one blocking acquisition, a poll bounded by
:data:`SHARED_GIT_LOCK_WAIT_SECONDS` whose expiry is a transient
:class:`~autoforge.errors.GitLockTimeout` (D11.3), scoped to one git
operation, and released by ``close``.

**Order L0 -> L2 -> L1 (D11.2).** L0 and L2 are only ever taken
non-blocking, and the holder of L1 takes nothing, so no wait-for cycle can
form. Standalone commands take L0 then L1 and never L2. While this process
holds L1 it may acquire no lock, invoke no agent, call no GitHub and save no
state: :func:`assert_shared_git_lock_free` raises
:class:`~autoforge.errors.LockOrderError` otherwise.

**Filesystem (D11.4).** :func:`check_local_lock_filesystem` refuses NFS,
CIFS/SMB, 9p, AFS, cluster filesystems and every FUSE filesystem, whose
``flock`` is not a local kernel lock: by ``statfs``/``fstatfs`` magic number
on Linux, by ``f_fstypename`` on macOS, and on any other platform, where it
cannot tell, it refuses. Scheduler mode applies it to the coordination root
and to every ``run.lock`` and ``git.lock`` descriptor it opens, so a mount
below the root cannot evade it; standalone commands never do.

No lock here has a timeout, lease or heartbeat as liveness: the kernel
releases every one of them when its last descriptor closes, SIGKILL
included, and no stale-lock cleanup exists.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import platform
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

from .errors import (
    ExecutionError,
    GitLockTimeout,
    IssueBusy,
    LockError,
    LockOrderError,
    LockProofError,
    NetworkFilesystemError,
    StateError,
)
from .executor import ExecutionRequest, ExecutionResult, execute
from .git_transport import NETWORK_TIMEOUT_SECONDS, local_git_request
from .safefs import SafeRoot, entry_kind, open_regular_at, split_relpath

if TYPE_CHECKING:
    # Typing only: run_namespace reaches the GitHub client through the state
    # module, and the GitHub client checks the lock order (D11.2).
    from .run_namespace import IssueNamespace

LOCK_DIRNAME = "autoforge"  # inside the git common dir
LOCK_FILENAME = "controller.lock"
GIT_LOCK_FILENAME = "git.lock"  # L1, beside controller.lock (ADR 0008 D5.3)

# D11.3: the longest timeout of any operation L1 guards, plus 60 s, so a
# waiter queued behind one operation that stays within its own bound never
# times out. The network fetch is the longest: the strict import's
# `index-pack` (agent_worktree.IMPORT_TIMEOUT_SECONDS) is as long, the
# worktree check-then-create is a few local git reads and one `worktree
# add` of 120 s each (tests/test_lock_domains.py keeps the three in step),
# and the sandbox directories' creation is a few `mkdir`s.
SHARED_GIT_LOCK_WAIT_SECONDS = NETWORK_TIMEOUT_SECONDS + 60
# How often a waiter retries its non-blocking attempt.
SHARED_GIT_LOCK_POLL_SECONDS = 0.05

_CONTENTION = (
    "another AutoForge controller holds {path} — refusing to run concurrently on the "
    "same repository (an AutoForge scheduler or its surviving workers may hold it)"
)

_GIT_COMMON_DIR_ARGS = ["rev-parse", "--git-common-dir"]
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
        # Hardened like every controller git process (ADR 0004 D7.3), and
        # from the same environment as the workspace reader, so an operator's
        # `GIT_DIR` cannot key the lock to one repository and the run to
        # another.
        res = runner(
            local_git_request(
                _GIT_COMMON_DIR_ARGS, cwd=str(base), timeout_seconds=_GIT_TIMEOUT_SECONDS
            )
        )
    except ExecutionError as exc:
        raise LockError(
            f"cannot derive the controller lock for {shown}: git could not be run ({exc})"
        ) from exc
    if res.timed_out:
        raise LockError(f"cannot derive the controller lock for {shown}: git rev-parse timed out")
    if res.truncated:
        raise LockError(
            f"cannot derive the controller lock for {shown}: git rev-parse output was truncated"
        )
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
    """Non-reentrant exclusive lock: L0.

    Held for a whole CLI command via ``ControllerEngine.locked()`` (from the
    state load / creation through the last persisted step); ``engine.step()``
    / ``engine.run()`` acquire it per call only when used outside that block.

    A scheduler shares it with its workers (:meth:`share`, :meth:`adopt`);
    from then on it is released by ``close`` only, never ``LOCK_UN``
    (ADR 0008 D10.3). A standalone command never shares it, so its
    :meth:`release` unlocks and closes exactly as before.
    """

    def __init__(self, lock_path: str | Path) -> None:
        self.lock_path = Path(lock_path)
        self._fd: int | None = None
        self._pid_fd: int | None = None
        self._base_fd: int | None = None
        # Close-only release (D10.3): set by share() before the descriptor
        # is handed to a child, and always for an adopted descriptor.
        self._close_only = False

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
        assert_shared_git_lock_free("take the controller lock (L0)")
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
                raise LockError(_CONTENTION.format(path=self.lock_path)) from exc
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
        """Release the lock: unlock and close, or only close once it is shared.

        ``LOCK_UN`` is the standalone path, where no other process holds the
        descriptor. After :meth:`share`, and for an adopted lock, ``LOCK_UN``
        would release L0 for the scheduler and every sibling at once, so the
        descriptor is only closed: the lock stays held while any other copy
        of it is open (ADR 0008 D10.3, D10.4).
        """
        if self._fd is not None:
            fd, self._fd = self._fd, None
            if self._close_only:
                os.close(fd)
            else:
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

    @property
    def close_only(self) -> bool:
        """Whether :meth:`release` only closes (shared or adopted; ADR 0008 D10.3)."""
        return self._close_only

    @property
    def coordination_root(self) -> Path:
        """The git common dir whose descriptor this lock is taken on."""
        return self.lock_path.parent.parent

    def share(self) -> int:
        """Switch to close-only release and return the held descriptor, for ``pass_fds``.

        A scheduler calls it before its first spawn (ADR 0008 D10.3), so the
        switch also covers an exit by exception: from then on no release in
        this process calls ``LOCK_UN``, and the lock stays held while a
        worker that inherited the descriptor lives. The descriptor stays
        ``O_CLOEXEC`` here: only a launch that names it in ``pass_fds`` (the
        worker spawn) inherits it.
        """
        if self._fd is None:
            raise self._refuse("it is not held, so there is no descriptor to share")
        self._close_only = True
        return self._fd

    @classmethod
    def adopt(cls, fd: int, *, expected_root: str | Path) -> ControllerLock:
        """Wrap an inherited L0 descriptor once :func:`prove_held` proved it (D9.2, D10.2).

        ``expected_root`` is the coordination root (the git common dir) the
        descriptor must name. The proof opens the root afresh
        (``O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC``), requires the
        same ``(st_dev, st_ino)`` as ``fd`` and requires ``flock(LOCK_EX |
        LOCK_NB)`` on the fresh descriptor to fail with ``EWOULDBLOCK``; it
        never acquires or releases the lock. A refusal is
        :class:`~autoforge.errors.LockProofError` and leaves ``fd`` open and
        untouched. The adopted lock is close-only, never writes the PID
        record (which keeps naming the scheduler, for diagnostics only), and
        its descriptor is made non-inheritable, so no launch of this process
        passes it on.
        """
        assert_shared_git_lock_free("adopt the controller lock (L0)")
        root = Path(expected_root)
        lock = cls(root / LOCK_DIRNAME / LOCK_FILENAME)
        prove_held(fd, lambda: _open_fresh_root(root), what=lock._what())
        os.set_inheritable(fd, False)
        lock._fd = fd
        lock._close_only = True
        return lock

    def prove_held(self) -> None:
        """Prove again that this lock's descriptor holds L0 (:func:`prove_held`)."""
        if self._fd is None:
            raise LockProofError(f"{self._what()}: it is not held by this process")
        root = self.coordination_root
        prove_held(self._fd, lambda: _open_fresh_root(root), what=self._what())

    def _what(self) -> str:
        return f"the controller lock (L0) on {self.coordination_root}"

    def __enter__(self) -> ControllerLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


# -- the proof of holding (ADR 0008 D9.2 step 1) ----------------------------------------


def prove_held(fd: int, open_fresh: Callable[[], int], *, what: str) -> None:
    """Prove that ``fd`` names the object ``open_fresh()`` opens and that its lock is held.

    ``flock(LOCK_EX | LOCK_NB)`` on ``fd`` itself proves nothing: on an open
    file description that holds the lock it is a no-op, and on one that does
    not it silently acquires. So the proof opens the object afresh
    (``open_fresh``: ``O_NOFOLLOW | O_CLOEXEC``, never ``O_CREAT``), requires
    the same file type and ``(st_dev, st_ino)`` as ``fd``, and requires
    ``flock(LOCK_EX | LOCK_NB)`` on the fresh descriptor to fail with
    ``EWOULDBLOCK``, so that some open file description holds the lock. The
    fresh descriptor is closed at once. It is a separate open file
    description, so the proof can neither acquire nor release the lock
    ``fd`` holds; when nothing holds it, the probe held it only until that
    close, and the refusal leaves it unacquired.

    A refusal is :class:`~autoforge.errors.LockProofError` (an object that
    cannot be opened afresh included); ``fd`` is never closed or changed.
    Reusable: a worker proves both inherited locks at startup and its L2
    again at each step boundary (#182).
    """
    try:
        inherited = os.fstat(fd)
    except OSError as exc:
        raise LockProofError(
            f"{what}: descriptor {fd} is not an open descriptor ({exc.strerror})"
        ) from exc
    fresh = open_fresh()
    try:
        st = os.fstat(fresh)
        if stat.S_IFMT(st.st_mode) != stat.S_IFMT(inherited.st_mode) or (
            st.st_dev,
            st.st_ino,
        ) != (inherited.st_dev, inherited.st_ino):
            raise LockProofError(
                f"{what}: descriptor {fd} names another object (device {inherited.st_dev}, "
                f"inode {inherited.st_ino}) than the lock (device {st.st_dev}, inode "
                f"{st.st_ino}); it does not hold that lock"
            )
        try:
            fcntl.flock(fresh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # some open file description holds it: the proof
        except OSError as exc:
            raise LockProofError(f"{what}: the lock could not be probed ({exc})") from exc
        raise LockProofError(
            f"{what}: nothing holds it, so descriptor {fd} does not; the probe released it "
            "again at once"
        )
    finally:
        os.close(fresh)


def _open_fresh_root(root: Path) -> int:
    """The coordination root opened afresh for :func:`prove_held`; never created."""
    try:
        return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise LockProofError(
            f"the coordination root {root} cannot be opened afresh to prove its lock: {exc}"
        ) from exc


def _open_lock_file(root: Path, relpath: str, *, create: bool, what: str) -> int:
    """A hardened descriptor of the lock file ``relpath`` below the coordination root.

    Every directory from ``root`` down is opened ``O_DIRECTORY | O_NOFOLLOW``
    relative to the one before (:class:`~autoforge.safefs.SafeRoot`) and is
    created when ``create``; the file is opened ``O_NOFOLLOW | O_CLOEXEC |
    O_NONBLOCK``, read-write and created when ``create``, read-only and never
    created otherwise, and must be a regular file with exactly one link. It
    is never truncated, written or unlinked. A missing entry without
    ``create`` raises ``FileNotFoundError``; any other refusal (a symbolic
    link, a FIFO, a directory, a second hard link anywhere on the way) is a
    :class:`~autoforge.errors.LockError` naming the entry.
    """
    parts = split_relpath(relpath)
    path = root.joinpath(*parts)
    flags = (os.O_RDWR | os.O_CREAT) if create else os.O_RDONLY
    try:
        with SafeRoot.open(root) as anchor:
            if len(parts) == 1:
                fd = open_regular_at(anchor.fd, parts[0], flags, mode=0o644, where=str(path))
            else:
                with anchor.subroot("/".join(parts[:-1]), create=create) as parent:
                    fd = open_regular_at(parent.fd, parts[-1], flags, mode=0o644, where=str(path))
    except FileNotFoundError:
        if not create:
            raise
        raise LockError(f"cannot use {path} as {what}: {root} does not exist") from None
    except (StateError, OSError) as exc:
        raise LockError(f"cannot use {path} as {what}: {exc}") from exc
    try:
        st = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise LockError(f"cannot use {path} as {what}: cannot stat it: {exc}") from exc
    if st.st_nlink != 1:
        # A write open already refused a second name; a read-only one (the
        # probe and the proof) is held to the same rule, and none at all is
        # a lock file unlinked under the open.
        os.close(fd)
        raise LockError(
            f"cannot use {path} as {what}: it has {st.st_nlink} hard links, so it is not "
            "the lock file the controller created; remove it and re-run (any other file "
            "has not been touched)"
        )
    return fd


# -- L2: the per-issue run lock (ADR 0008 D11.1) ----------------------------------------


class IssueRunLock:
    """One issue's run lock (L2): ``<common>/autoforge/scheduler/issues/<n>/run.lock``.

    Exactly one worker holds it per issue. Only the L0-holding scheduler
    acquires it (:meth:`acquire`, ``LOCK_EX | LOCK_NB``: a held lock is
    :class:`~autoforge.errors.IssueBusy`, never a wait) and hands it to one
    child (:meth:`handoff`), and the worker adopts it (:meth:`adopt`). Every release,
    in the scheduler and in workers, is ``close`` only (ADR 0008 D10.3), so
    the lock stays held while any copy of the descriptor is open, and the
    kernel releases it when the last one closes, SIGKILL included. The file
    is created once and never unlinked, written or truncated. Acquisition is
    scheduler mode only, so the filesystem of the coordination root and of
    the opened descriptor is checked (:func:`check_local_lock_filesystem`).
    """

    def __init__(self, namespace: IssueNamespace) -> None:
        self.namespace = namespace
        self.lock_path = namespace.run_lock_file
        self._fd: int | None = None
        self._handed_off = False

    @property
    def held(self) -> bool:
        return self._fd is not None

    @property
    def _relpath(self) -> str:
        return "/".join((*self.namespace.relative, self.lock_path.name))

    def _what(self) -> str:
        return f"the run lock (L2) of issue {self.namespace.issue_number}"

    def acquire(self) -> IssueRunLock:
        """Take the lock, non-blocking; :class:`~autoforge.errors.IssueBusy` when held."""
        assert_shared_git_lock_free(f"take {self._what()}")
        if self._fd is not None:
            raise LockError(f"{self._what()} is already held by this process")
        root = self.namespace.coordination_root
        fd = _open_lock_file(root, self._relpath, create=True, what=self._what())
        try:
            check_local_lock_filesystem(root, what=f"the coordination root {root}")
            check_local_lock_filesystem(fd, what=f"{self._what()} {self.lock_path}")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise IssueBusy(
                    f"{self._what()} ({self.lock_path}) is held by another process: a live "
                    "worker owns the issue (or a status probe read it at this instant)"
                ) from exc
            except OSError as exc:
                raise LockError(f"cannot take {self._what()}: flock failed: {exc}") from exc
        except BaseException:
            os.close(fd)  # drops the flock too, if it was taken
            raise
        self._fd = fd
        self._handed_off = False
        return self

    def handoff(self) -> int:
        """The held descriptor, for exactly one child's ``pass_fds`` (D9.6).

        Refused (:class:`~autoforge.errors.LockError`) when the lock is not
        held, and on a second call: one issue, one worker. After the spawn
        the caller releases its own copy (:meth:`release`, ``close`` only), and
        the child is the holder.
        """
        if self._fd is None:
            raise LockError(f"{self._what()} is not held, so it cannot be handed to a worker")
        if self._handed_off:
            raise LockError(
                f"{self._what()} was already handed to a worker; it is handed to exactly one"
            )
        self._handed_off = True
        return self._fd

    def release(self) -> None:
        """Close the descriptor; never ``LOCK_UN`` (ADR 0008 D10.3)."""
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)

    @classmethod
    def adopt(cls, fd: int, namespace: IssueNamespace) -> IssueRunLock:
        """Wrap an inherited L2 descriptor once :func:`prove_held` proved it (D9.2).

        The fresh descriptor is this namespace's ``run.lock``, opened
        ``O_RDONLY | O_NOFOLLOW | O_CLOEXEC`` below the coordination root
        without ``O_CREAT``. A refusal is
        :class:`~autoforge.errors.LockProofError` and leaves ``fd`` open and
        untouched. The adopted descriptor is made non-inheritable and is
        never handed off again.
        """
        lock = cls(namespace)
        assert_shared_git_lock_free(f"adopt {lock._what()}")
        lock._prove(fd)
        os.set_inheritable(fd, False)
        lock._fd = fd
        lock._handed_off = True
        return lock

    def prove_held(self) -> None:
        """Prove again that this descriptor holds the lock (the worker's step boundary, #182)."""
        if self._fd is None:
            raise LockProofError(f"{self._what()}: it is not held by this process")
        self._prove(self._fd)

    def _prove(self, fd: int) -> None:
        root = self.namespace.coordination_root

        def fresh() -> int:
            try:
                return _open_lock_file(root, self._relpath, create=False, what=self._what())
            except FileNotFoundError:
                raise LockProofError(
                    f"{self._what()}: {self.lock_path} does not exist, so nothing holds it"
                ) from None
            except LockError as exc:
                raise LockProofError(str(exc)) from exc

        prove_held(fd, fresh, what=f"{self._what()} ({self.lock_path})")

    def __enter__(self) -> IssueRunLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def issue_run_lock_live(namespace: IssueNamespace) -> bool:
    """Whether a live owner holds issue ``n``'s run lock; lock-free (ADR 0008 D8.3).

    Opens ``run.lock`` ``O_RDONLY | O_NOFOLLOW | O_CLOEXEC`` without
    ``O_CREAT`` (a missing file, or a missing directory above it, means no
    owner, and nothing is created), tries ``LOCK_SH | LOCK_NB`` and closes at
    once. ``EWOULDBLOCK`` is a live owner. A shared probe never excludes
    another probe; one that collides with an :meth:`IssueRunLock.acquire`
    makes only that acquisition fail (``IssueBusy``), which the scheduler
    reads as live for one tick. An entry that is not the lock file (a link,
    a FIFO, a second hard link) is a :class:`~autoforge.errors.LockError`:
    the caller cannot tell, and fails closed.
    """
    lock = IssueRunLock(namespace)
    try:
        fd = _open_lock_file(
            namespace.coordination_root, lock._relpath, create=False, what=lock._what()
        )
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError as exc:
            raise LockError(f"cannot probe {lock._what()}: flock failed: {exc}") from exc
        return False
    finally:
        os.close(fd)  # also drops the shared probe lock


# -- L1: the shared-Git lock (ADR 0008 D11.1, D11.3, D12) -------------------------------

# What this process holds L1 for, "" while it holds none (ADR 0008 D11.2).
# Per process: a child that execs starts with none.
_shared_git_holder = ""


def assert_shared_git_lock_free(action: str) -> None:
    """Refuse ``action`` while this process holds the shared-Git lock (D11.2).

    The holder of L1 acquires no lock (L1 included), invokes no agent, calls
    no GitHub and saves no state, so it can never wait on anything while
    others wait on it. :class:`~autoforge.errors.LockOrderError` is a
    controller bug, never a runtime condition.
    """
    if _shared_git_holder:
        raise LockOrderError(
            f"cannot {action} while holding the shared-Git lock (L1) for "
            f"{_shared_git_holder}: its holder takes no lock, invokes no agent, calls no "
            "GitHub and saves no state (ADR 0008 D11.2); this is a controller bug"
        )


def shared_git_lock_path(coordination_root: str | Path) -> Path:
    """``<coordination root>/autoforge/git.lock``."""
    return Path(coordination_root) / LOCK_DIRNAME / GIT_LOCK_FILENAME


class SharedGitLock:
    """The shared-Git lock (L1), scoped to one controller write to the common dir.

    A context manager around exactly one critical section of ADR 0008 D12.1:
    the agent worktree's check-then-create, the controller's fetch, the
    strict import's ``index-pack``, or the REMOTE sandbox directories'
    creation. Taken ``LOCK_EX`` by a poll of non-blocking attempts until
    :data:`SHARED_GIT_LOCK_WAIT_SECONDS` have passed, then
    :class:`~autoforge.errors.GitLockTimeout` (transient; the
    guarded operation reports it as its own failure). Released by ``close``:
    the descriptor is ``O_CLOEXEC`` and never shared. Standalone commands and
    workers both take it; with ``require_local_filesystem`` (scheduler mode)
    the root's and the descriptor's filesystems are checked first.
    """

    def __init__(
        self,
        coordination_root: str | Path,
        *,
        operation: str = "a controller write to the shared git data",
        wait_seconds: float = SHARED_GIT_LOCK_WAIT_SECONDS,
        poll_seconds: float = SHARED_GIT_LOCK_POLL_SECONDS,
        require_local_filesystem: bool = False,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.coordination_root = Path(coordination_root)
        self.lock_path = shared_git_lock_path(self.coordination_root)
        self.operation = operation
        self.wait_seconds = wait_seconds
        self.poll_seconds = poll_seconds
        self.require_local_filesystem = require_local_filesystem
        self._clock = clock
        self._sleep = sleep
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> SharedGitLock:
        global _shared_git_holder
        assert_shared_git_lock_free(f"take the shared-Git lock (L1) for {self.operation}")
        what = "the shared-Git lock (L1)"
        fd = _open_lock_file(
            self.coordination_root, f"{LOCK_DIRNAME}/{GIT_LOCK_FILENAME}", create=True, what=what
        )
        try:
            if self.require_local_filesystem:
                root = self.coordination_root
                check_local_lock_filesystem(root, what=f"the coordination root {root}")
                check_local_lock_filesystem(fd, what=f"{what} {self.lock_path}")
            deadline = self._clock() + self.wait_seconds
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise GitLockTimeout(
                            f"waited {self.wait_seconds:g}s for {what} {self.lock_path} to "
                            f"{self.operation}; another controller process held it all along"
                        ) from None
                    self._sleep(min(self.poll_seconds, remaining))
                except OSError as exc:
                    raise LockError(f"cannot take {what}: flock failed: {exc}") from exc
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        _shared_git_holder = self.operation
        return self

    def release(self) -> None:
        global _shared_git_holder
        if self._fd is not None:
            fd, self._fd = self._fd, None
            _shared_git_holder = ""
            os.close(fd)

    def __enter__(self) -> SharedGitLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


# -- the filesystem of a scheduler lock (ADR 0008 D11.4) --------------------------------


@dataclass(frozen=True)
class FilesystemType:
    """What ``statfs``/``fstatfs`` reported: Linux's ``f_type``, macOS's ``f_fstypename``."""

    magic: int | None = None
    name: str = ""


# Linux ``f_type`` values (statfs(2), <linux/magic.h>) of filesystems whose
# flock is not a local kernel lock: network, distributed and cluster
# filesystems, and FUSE, which every FUSE filesystem reports (sshfs,
# glusterfs, virtiofs among them). Fixed here and tested; anything else is a
# local filesystem to this check (ext4, xfs, btrfs, tmpfs, overlay, zfs, ...).
NETWORK_FILESYSTEM_MAGICS: dict[int, str] = {
    0x6969: "nfs",
    0x517B: "smb",
    0xFF534D42: "cifs",
    0xFE534D42: "smb2",
    0x01021997: "9p",
    0x5346414F: "afs",
    0x6B414653: "kafs",
    0x73757245: "coda",
    0x564C: "ncp",
    0x65735546: "fuse",
    0x00C36400: "ceph",
    0x01161970: "gfs2",
    0x7461636F: "ocfs2",
    0x0BD00BD0: "lustre",
    0x47504653: "gpfs",
    0x20030528: "orangefs",
    0x19830326: "beegfs",
}
# macOS ``f_fstypename`` values of the same kinds; a name that contains
# "fuse" (macfuse, osxfuse, fusefs) is refused as well.
NETWORK_FILESYSTEM_NAMES = frozenset({"nfs", "smbfs", "cifs", "afpfs", "webdav", "ftp", "afs"})


def network_filesystem(fs: FilesystemType) -> str:
    """The refused filesystem ``fs`` is, or "" when its flock is a local kernel lock."""
    if fs.magic is not None and fs.magic in NETWORK_FILESYSTEM_MAGICS:
        return NETWORK_FILESYSTEM_MAGICS[fs.magic]
    name = fs.name.lower()
    if name in NETWORK_FILESYSTEM_NAMES or "fuse" in name:
        return fs.name
    return ""


def filesystem_type(target: int | str | Path) -> FilesystemType:
    """``fstatfs`` of a descriptor, ``statfs`` of a path; ``OSError`` when it cannot tell."""
    if sys.platform.startswith("linux"):
        return _linux_statfs(target)
    if sys.platform == "darwin":
        return _darwin_statfs(target)
    raise OSError(errno.ENOSYS, f"no filesystem-type check on {sys.platform}")


def check_local_lock_filesystem(target: int | str | Path, *, what: str = "") -> None:
    """Refuse a scheduler lock on a filesystem whose flock is not local (ADR 0008 D11.4).

    ``target`` is the coordination root (a path) or an opened ``run.lock``
    or ``git.lock`` descriptor, so a mount below the root is caught by its
    descriptor; ``what`` names it in the refusal. A filesystem the check
    cannot identify is refused too: scheduler mode's live-ownership proof
    depends on local locks. Standalone commands never call this.
    """
    if not what:
        what = f"lock descriptor {target}" if isinstance(target, int) else str(target)
    try:
        fs = filesystem_type(target)
    except OSError as exc:
        raise NetworkFilesystemError(
            f"cannot tell which filesystem {what} is on ({exc}); the parallel EPIC scheduler "
            "needs one whose flock is a local kernel lock"
        ) from exc
    refused = network_filesystem(fs)
    if refused:
        raise NetworkFilesystemError(
            f"{what} is on a {refused} filesystem, whose flock is not a local kernel lock; "
            "the parallel EPIC scheduler refuses it, because its live-ownership proof "
            "depends on local locks (standalone commands are unaffected)"
        )


@cache
def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


# Larger than ``struct statfs`` (and ``statfs64``) on every Linux ABI:
# 120 bytes on x86_64 and aarch64.
_LINUX_STATFS_BYTES = 512


def _linux_statfs(target: int | str | Path) -> FilesystemType:
    buf = ctypes.create_string_buffer(_LINUX_STATFS_BYTES)
    libc = _libc()
    if isinstance(target, int):
        rc = libc.fstatfs(ctypes.c_int(target), buf)
    else:
        rc = libc.statfs(os.fsencode(target), buf)
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    # ``f_type`` is the first member everywhere: a ``long`` (``__fsword_t``),
    # except on s390, where it is an ``unsigned int``. Every magic number
    # fits in 32 bits.
    if platform.machine().startswith("s390"):
        magic = ctypes.c_uint.from_buffer(buf).value
    else:
        magic = ctypes.c_long.from_buffer(buf).value
    return FilesystemType(magic=magic & 0xFFFFFFFF)


class _DarwinStatfs(ctypes.Structure):
    """macOS ``struct statfs`` with 64-bit inodes (the layout of ``statfs$INODE64``)."""

    _fields_ = [
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", ctypes.c_int32 * 2),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
        ("f_flags_ext", ctypes.c_uint32),
        ("f_reserved", ctypes.c_uint32 * 7),
    ]


def _darwin_statfs(target: int | str | Path) -> FilesystemType:  # pragma: no cover - macOS
    libc = _libc()
    name = "fstatfs" if isinstance(target, int) else "statfs"
    try:
        # x86_64 exports the 64-bit-inode layout under this name; arm64 has
        # only that layout, under the plain name.
        func = libc[f"{name}$INODE64"]
    except AttributeError:
        func = libc[name]
    buf = _DarwinStatfs()
    arg = ctypes.c_int(target) if isinstance(target, int) else os.fsencode(target)
    if func(arg, ctypes.byref(buf)) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return FilesystemType(name=buf.f_fstypename.decode("utf-8", "replace"))
