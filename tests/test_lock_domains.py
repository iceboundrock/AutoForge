"""The lock domains of ADR 0008 (D9.2, D10, D11; #180), with real processes.

L0 (:class:`ControllerLock`) shared by a scheduler with its workers and
adopted after a proof, L2 (:class:`IssueRunLock`) handed to exactly one
worker and probed lock-free, L1 (:class:`SharedGitLock`) around one write to
the common dir, the order L0 -> L2 -> L1, the filesystem refusal and the
descriptor hygiene of every launch.

A multi-process test runs real child processes that talk over pipes. None
sleeps for correctness: a child answers once its step is done, a reaped
child has closed every descriptor, and "released" after an orphaned child
exits is waited for with a blocking ``flock`` bounded by an alarm.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import signal
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO

import pytest

from autoforge import agent_worktree, locking
from autoforge.errors import (
    GitLockTimeout,
    IssueBusy,
    LockError,
    LockOrderError,
    LockProofError,
    NetworkFilesystemError,
)
from autoforge.executor import ExecutionRequest, execute
from autoforge.git_transport import NETWORK_TIMEOUT_SECONDS
from autoforge.github import GitHubClient
from autoforge.local_workspace import GIT_TIMEOUT_SECONDS
from autoforge.locking import (
    SHARED_GIT_LOCK_WAIT_SECONDS,
    ControllerLock,
    FilesystemType,
    IssueRunLock,
    SharedGitLock,
    check_local_lock_filesystem,
    filesystem_type,
    issue_run_lock_live,
    network_filesystem,
    shared_git_lock_path,
)
from autoforge.run_namespace import issue_namespace
from autoforge.transitions import Phase
from tests.conftest import make_engine

SRC = Path(__file__).resolve().parents[1] / "src"
LINUX = sys.platform.startswith("linux")

# A child that takes, adopts, proves and releases locks on request, one
# request per line on stdin, one answer per line on stdout ("ok", or the
# exception's type and message). It exits at the end of its input.
ACTOR = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, sys.argv[1])
    from pathlib import Path
    from autoforge import locking
    from autoforge.run_namespace import issue_namespace

    held = {}
    for line in sys.stdin:
        verb, *args = line.split()
        try:
            if verb == "acquire-l0":
                lock_path = Path(args[0]) / "autoforge" / "controller.lock"
                held["l0"] = locking.ControllerLock(lock_path).acquire()
            elif verb == "adopt-l0":
                held["l0"] = locking.ControllerLock.adopt(int(args[0]), expected_root=args[1])
            elif verb == "acquire-l2":
                namespace = issue_namespace(args[0], int(args[1]))
                held["l2"] = locking.IssueRunLock(namespace).acquire()
            elif verb == "adopt-l2":
                namespace = issue_namespace(args[1], int(args[2]))
                held["l2"] = locking.IssueRunLock.adopt(int(args[0]), namespace)
            elif verb == "acquire-l1":
                held["l1"] = locking.SharedGitLock(args[0]).acquire()
            elif verb == "prove":
                held[args[0]].prove_held()
            elif verb == "release":
                held.pop(args[0]).release()
            else:
                raise ValueError(f"unknown request {verb}")
            answer = "ok"
        except Exception as exc:
            answer = f"{type(exc).__name__}: {exc}"
        print(" ".join(answer.split()), flush=True)
    """
)

# A scheduler stand-in: takes L0 and one issue's L2, switches L0 to
# close-only before its first spawn, hands both to one worker (an ACTOR
# whose stdin and stdout are pipes the test holds), prints the two
# descriptor numbers, then waits for one line and ends as asked: by
# returning, by an exception, or by SIGKILL.
SCHEDULER = textwrap.dedent(
    """
    import os, signal, subprocess, sys
    sys.path.insert(0, sys.argv[1])
    from pathlib import Path
    from autoforge.locking import ControllerLock, IssueRunLock
    from autoforge.run_namespace import issue_namespace

    src, root, issue, to_worker, from_worker, how, actor = sys.argv[1:8]
    with ControllerLock(Path(root) / "autoforge" / "controller.lock") as l0:
        l0_fd = l0.share()
        with IssueRunLock(issue_namespace(root, int(issue))) as l2:
            l2_fd = l2.handoff()
            subprocess.Popen(
                [sys.executable, "-c", actor, src],
                stdin=int(to_worker),
                stdout=int(from_worker),
                stderr=subprocess.DEVNULL,
                pass_fds=(l0_fd, l2_fd),
            )
            os.close(int(to_worker))
            os.close(int(from_worker))
            print(l0_fd, l2_fd, flush=True)
            sys.stdin.readline()
            if how == "exception":
                raise RuntimeError("the scheduler crashed")
            if how == "sigkill":
                os.kill(os.getpid(), signal.SIGKILL)
    """
)

# Lists the (st_dev, st_ino) of every descriptor the process holds.
LIST_FDS = textwrap.dedent(
    """
    import os
    for name in os.listdir("/proc/self/fd"):
        try:
            st = os.stat(f"/proc/self/fd/{name}")
        except OSError:
            continue
        print(st.st_dev, st.st_ino)
    """
)


class Channel:
    """One request line to a child, its one answer line back."""

    def __init__(self, to_child: IO[str], from_child: IO[str]) -> None:
        self._to = to_child
        self._from = from_child

    def __call__(self, *words: object) -> str:
        self._to.write(" ".join(str(word) for word in words) + "\n")
        self._to.flush()
        answer = self._from.readline()
        assert answer, "the child ended without answering"
        return answer.strip()

    def close(self) -> None:
        """End the child's input: it exits, closing every descriptor it holds."""
        if not self._to.closed:
            self._to.close()
        self._from.close()


class Actor(Channel):
    """An ACTOR child of the test, holding ``pass_fds`` from its spawn."""

    def __init__(self, pass_fds: tuple[int, ...] = ()) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-c", ACTOR, str(SRC)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            pass_fds=pass_fds,
        )
        assert self.proc.stdin is not None and self.proc.stdout is not None
        super().__init__(self.proc.stdin, self.proc.stdout)

    def exit(self) -> int:
        self._to.close()
        code = self.proc.wait(timeout=60)
        self._from.close()
        return code

    def kill(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=60)
        self.close()


@pytest.fixture
def spawn() -> Iterator[Callable[..., Actor]]:
    started: list[Actor] = []

    def start(pass_fds: tuple[int, ...] = ()) -> Actor:
        actor = Actor(pass_fds)
        started.append(actor)
        return actor

    yield start
    for actor in started:
        if actor.proc.poll() is None:
            actor.proc.kill()
        actor.proc.wait(timeout=60)
        actor.close()


@pytest.fixture
def root(tmp_path) -> Path:
    """A coordination root (a git common dir stands in as a plain directory)."""
    common = tmp_path / "common"
    common.mkdir()
    return Path(os.path.realpath(common))


def _l0(root: Path) -> Path:
    return root / "autoforge" / "controller.lock"


def _open_fresh(path: Path) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | (os.O_DIRECTORY if path.is_dir() else 0)
    return os.open(path, flags)


def _held(path: Path) -> bool:
    """Whether some open file description holds the lock on ``path``, probed afresh."""
    fd = _open_fresh(path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def _wait_released(path: Path, seconds: int = 60) -> None:
    """Block until nothing holds ``path``'s lock: a blocking flock, bounded by an alarm."""

    def expired(signum, frame):
        raise AssertionError(f"{path} was still locked after {seconds}s")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)
    fd = _open_fresh(path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        os.close(fd)


def _open_fds() -> set[str]:
    return set(os.listdir("/proc/self/fd")) if LINUX else set()


# -- L0: unchanged for a standalone command ------------------------------------------


def test_contention_names_a_scheduler_and_its_surviving_workers(root):
    with ControllerLock(_l0(root)):
        with pytest.raises(LockError) as refused:
            ControllerLock(_l0(root)).acquire()
    message = str(refused.value)
    assert "another AutoForge controller holds" in message
    assert "an AutoForge scheduler or its surviving workers may hold it" in message
    assert not isinstance(refused.value, (IssueBusy, LockProofError))


def test_a_standalone_release_unlocks_even_while_another_copy_is_open(root):
    """Standalone release is LOCK_UN then close, exactly as before #180."""
    lock = ControllerLock(_l0(root)).acquire()
    assert not lock.close_only
    copy = os.dup(lock._fd)  # type: ignore[arg-type]
    try:
        lock.release()
        assert not _held(root), "LOCK_UN releases the description every copy shares"
    finally:
        os.close(copy)


# -- L0: shared by a scheduler, adopted by a worker ----------------------------------


def test_share_switches_to_close_only_before_the_first_spawn(root):
    lock = ControllerLock(_l0(root)).acquire()
    fd = lock.share()
    assert lock.close_only and lock.held
    assert not os.get_inheritable(fd), "only a launch naming it in pass_fds inherits it"
    copy = os.dup(fd)  # what a worker's pass_fds copy is
    lock.release()
    assert _held(root), "the scheduler's release must not unlock its worker's copy"
    os.close(copy)
    assert not _held(root)


def test_share_refuses_a_lock_that_is_not_held(root):
    with pytest.raises(LockError, match="not held"):
        ControllerLock(_l0(root)).share()


def test_adopt_proves_and_wraps_an_inherited_descriptor_close_only(root):
    scheduler = ControllerLock(_l0(root)).acquire()
    record = _l0(root).read_bytes()
    inherited = os.dup(scheduler.share())
    os.set_inheritable(inherited, True)  # as a pass_fds child receives it
    worker = ControllerLock.adopt(inherited, expected_root=root)
    assert worker.held and worker.close_only and worker.coordination_root == root
    assert not os.get_inheritable(inherited), "the worker's own launches do not inherit it"
    worker.prove_held()
    scheduler.release()
    assert _held(root), "the worker holds L0 after its scheduler let go"
    worker.prove_held()
    assert _l0(root).read_bytes() == record, "adoption never writes the PID record"
    worker.release()
    assert not _held(root)


def test_adopt_refuses_a_descriptor_that_holds_nothing(root):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(LockProofError, match="nothing holds it"):
            ControllerLock.adopt(fd, expected_root=root)
        os.fstat(fd)  # left open
        assert not _held(root), "the proof released what its probe took"
    finally:
        os.close(fd)


@pytest.mark.parametrize("kind", ["another directory", "a regular file"])
def test_adopt_refuses_a_descriptor_of_another_object(root, tmp_path, kind):
    other = tmp_path / "other"
    if kind == "another directory":
        other.mkdir()
        fd = os.open(other, os.O_RDONLY | os.O_DIRECTORY)
    else:
        other.write_text("x\n")
        fd = os.open(other, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # it holds *a* lock, on the wrong object
        with ControllerLock(_l0(root)):
            with pytest.raises(LockProofError, match="names another object"):
                ControllerLock.adopt(fd, expected_root=root)
    finally:
        os.close(fd)


def test_adopt_refuses_a_number_that_is_no_descriptor(root):
    with ControllerLock(_l0(root)):
        with pytest.raises(LockProofError, match="not an open descriptor"):
            ControllerLock.adopt(10**6, expected_root=root)


def test_adopt_refuses_a_root_reached_through_a_link(root, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    scheduler = ControllerLock(_l0(root)).acquire()
    inherited = os.dup(scheduler.share())
    try:
        with pytest.raises(LockProofError, match="cannot be opened afresh"):
            ControllerLock.adopt(inherited, expected_root=link)
    finally:
        os.close(inherited)
        scheduler.release()


@pytest.mark.parametrize("how", ["return", "exception", "sigkill"])
def test_a_scheduler_that_ends_leaves_both_locks_with_its_surviving_worker(root, how):
    """D10.2-D10.4: however the scheduler ends, its worker keeps L0 and L2.

    The scheduler switched L0 to close-only before its spawn and releases L2
    by close only, so neither its normal return, an exception nor SIGKILL
    unlocks the description the worker shares. The kernel releases both
    when the worker, the last holder, exits.
    """
    namespace = issue_namespace(root, 7)
    to_r, to_w = os.pipe()
    from_r, from_w = os.pipe()
    scheduler = subprocess.Popen(
        [sys.executable, "-c", SCHEDULER, str(SRC), str(root), "7", str(to_r), str(from_w)]
        + [how, ACTOR],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        pass_fds=(to_r, from_w),
    )
    os.close(to_r)
    os.close(from_w)
    worker = Channel(os.fdopen(to_w, "w"), os.fdopen(from_r))
    try:
        assert scheduler.stdout is not None
        l0_fd, l2_fd = scheduler.stdout.readline().split()
        assert worker("adopt-l0", l0_fd, root) == "ok"
        assert worker("adopt-l2", l2_fd, root, 7) == "ok"
        _, err = scheduler.communicate("end\n", timeout=60)
        expected = {"return": 0, "exception": 1, "sigkill": -signal.SIGKILL}[how]
        assert scheduler.returncode == expected, err
        assert ("the scheduler crashed" in err) == (how == "exception")

        with pytest.raises(LockError, match="surviving workers may hold it"):
            ControllerLock(_l0(root)).acquire()
        assert issue_run_lock_live(namespace)
        with pytest.raises(IssueBusy):
            IssueRunLock(namespace).acquire()
        assert worker("prove", "l0") == "ok"
        assert worker("prove", "l2") == "ok"
    finally:
        worker.close()
        if scheduler.poll() is None:
            scheduler.kill()
            scheduler.wait(timeout=60)
    _wait_released(root)
    _wait_released(namespace.run_lock_file)
    assert not issue_run_lock_live(namespace)
    with ControllerLock(_l0(root)), IssueRunLock(namespace):
        pass


def test_sigkill_of_the_last_holder_releases_every_lock_and_leaves_nothing_to_clean(root, spawn):
    namespace = issue_namespace(root, 7)
    holder = spawn()
    assert holder("acquire-l0", root) == "ok"
    assert holder("acquire-l2", root, 7) == "ok"
    assert holder("acquire-l1", root) == "ok"
    assert _held(root) and issue_run_lock_live(namespace)
    assert _held(shared_git_lock_path(root))
    holder.kill()  # reaped: every descriptor it held is closed
    assert not _held(root) and not issue_run_lock_live(namespace)
    assert not _held(shared_git_lock_path(root))
    # No stale-lock cleanup: the files stay, and the next holder just takes them.
    assert namespace.run_lock_file.is_file() and shared_git_lock_path(root).is_file()
    with ControllerLock(_l0(root)), IssueRunLock(namespace), SharedGitLock(root):
        pass


# -- L2: the per-issue run lock ------------------------------------------------------


def test_l2_is_non_blocking_and_a_held_lock_is_issue_busy(root, spawn):
    namespace = issue_namespace(root, 7)
    other = spawn()
    assert other("acquire-l2", root, 7) == "ok"
    with pytest.raises(IssueBusy, match="held by another process"):
        IssueRunLock(namespace).acquire()
    assert issubclass(IssueBusy, LockError)
    with IssueRunLock(issue_namespace(root, 8)) as eight:  # another issue, another lock
        assert eight.held
    # A second acquisition is a second open file description: busy even in
    # the holder's own process.
    assert other("acquire-l2", root, 7).startswith("IssueBusy: ")
    assert other("release", "l2") == "ok"
    with IssueRunLock(namespace) as lock:
        assert lock.held


def test_l2_is_handed_to_exactly_one_worker_and_released_by_close_only(root, spawn):
    namespace = issue_namespace(root, 7)
    lock = IssueRunLock(namespace).acquire()
    fd = lock.handoff()
    with pytest.raises(LockError, match="handed to exactly one"):
        lock.handoff()
    worker = spawn(pass_fds=(fd,))
    assert worker("adopt-l2", fd, root, 7) == "ok"
    lock.release()
    assert issue_run_lock_live(namespace), "the scheduler's close left the worker holding it"
    with pytest.raises(IssueBusy):
        IssueRunLock(namespace).acquire()
    assert worker("prove", "l2") == "ok"
    assert worker("release", "l2") == "ok"
    assert not issue_run_lock_live(namespace)


def test_l2_release_never_unlocks_a_shared_description(root):
    namespace = issue_namespace(root, 7)
    lock = IssueRunLock(namespace).acquire()
    copy = os.dup(lock.handoff())
    lock.release()
    assert issue_run_lock_live(namespace)
    os.close(copy)
    assert not issue_run_lock_live(namespace)


def test_l2_handoff_of_a_lock_not_held_is_refused(root):
    with pytest.raises(LockError, match="not held"):
        IssueRunLock(issue_namespace(root, 7)).handoff()


def test_l2_adopt_refuses_an_unproven_descriptor(root, tmp_path):
    seven, eight = issue_namespace(root, 7), issue_namespace(root, 8)
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("x\n")
    fd = os.open(unrelated, os.O_RDONLY)
    try:
        with pytest.raises(LockProofError, match="does not exist, so nothing holds it"):
            IssueRunLock.adopt(fd, seven)
    finally:
        os.close(fd)
    assert not seven.directory.exists(), "a proof creates nothing"

    IssueRunLock(seven).acquire().release()
    fd = os.open(seven.run_lock_file, os.O_RDONLY)
    try:
        with pytest.raises(LockProofError, match="nothing holds it"):
            IssueRunLock.adopt(fd, seven)
        with IssueRunLock(eight) as held_eight:
            assert held_eight.held
            with IssueRunLock(seven):
                eight_fd = os.open(eight.run_lock_file, os.O_RDONLY)
                try:
                    with pytest.raises(LockProofError, match="names another object"):
                        IssueRunLock.adopt(eight_fd, seven)
                finally:
                    os.close(eight_fd)
    finally:
        os.close(fd)


def test_an_adopted_l2_is_never_handed_off_again(root):
    namespace = issue_namespace(root, 7)
    lock = IssueRunLock(namespace).acquire()
    inherited = os.dup(lock.handoff())
    worker = IssueRunLock.adopt(inherited, namespace)
    lock.release()
    with pytest.raises(LockError, match="handed to exactly one"):
        worker.handoff()
    worker.prove_held()
    worker.release()
    with pytest.raises(LockProofError, match="not held by this process"):
        worker.prove_held()


def test_the_probe_creates_nothing_and_reads_a_live_owner(root, spawn):
    namespace = issue_namespace(root, 7)
    assert not issue_run_lock_live(namespace)
    assert not (root / "autoforge").exists(), "the probe never creates"
    IssueRunLock(namespace).acquire().release()
    assert not issue_run_lock_live(namespace)
    owner = spawn()
    assert owner("acquire-l2", root, 7) == "ok"
    assert issue_run_lock_live(namespace)
    assert owner("release", "l2") == "ok"
    assert not issue_run_lock_live(namespace)


def test_a_probe_never_excludes_a_probe_and_collides_only_with_an_acquisition(root):
    """D8.3: a probe in flight is no owner to a second probe; an acquisition
    that meets it fails (IssueBusy), which the scheduler reads as live for one tick."""
    namespace = issue_namespace(root, 7)
    IssueRunLock(namespace).acquire().release()
    probing = os.open(namespace.run_lock_file, os.O_RDONLY)
    try:
        fcntl.flock(probing, fcntl.LOCK_SH | fcntl.LOCK_NB)
        assert not issue_run_lock_live(namespace)
        with pytest.raises(IssueBusy):
            IssueRunLock(namespace).acquire()
    finally:
        os.close(probing)
    with IssueRunLock(namespace) as lock:
        assert lock.held


# -- hardening of the L2 and L1 lock files -------------------------------------------

_LOCK_FILES = {
    "L2": lambda root: issue_namespace(root, 7).run_lock_file,
    "L2 probe": lambda root: issue_namespace(root, 7).run_lock_file,
    "L1": shared_git_lock_path,
}


def _use(which: str, root: Path) -> None:
    if which == "L2":
        IssueRunLock(issue_namespace(root, 7)).acquire().release()
    elif which == "L2 probe":
        issue_run_lock_live(issue_namespace(root, 7))
    else:
        SharedGitLock(root).acquire().release()


@pytest.mark.parametrize("which", sorted(_LOCK_FILES))
@pytest.mark.parametrize("kind", ["symlink", "dangling symlink", "fifo", "directory", "hard link"])
def test_a_lock_entry_that_is_not_the_controllers_file_is_refused_untouched(
    root, tmp_path, which, kind
):
    path = _LOCK_FILES[which](root)
    path.parent.mkdir(parents=True)
    target = tmp_path / "target"
    if kind == "symlink":
        target.write_text("keep\n")
        path.symlink_to(target)
    elif kind == "dangling symlink":
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        target.write_text("keep\n")
        os.link(target, path)
    before = _open_fds()
    with pytest.raises(LockError) as refused:
        _use(which, root)
    assert not isinstance(refused.value, IssueBusy)
    assert str(path) in str(refused.value)
    assert _open_fds() == before, "every descriptor of the refusal was closed"
    if kind in ("symlink", "hard link"):
        assert target.read_text() == "keep\n"
    if kind == "dangling symlink":
        assert not target.exists(), "nothing was created through the link"
    assert os.path.lexists(path), "the entry is refused, never removed"


@pytest.mark.parametrize("which", sorted(_LOCK_FILES))
def test_a_linked_directory_above_a_lock_file_is_refused(root, tmp_path, which):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "autoforge").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(LockError):
        _use(which, root)
    assert list(elsewhere.iterdir()) == []


def test_lock_files_are_created_once_and_never_written_truncated_or_unlinked(root):
    namespace = issue_namespace(root, 7)
    with IssueRunLock(namespace), SharedGitLock(root):
        pass
    first = (os.stat(namespace.run_lock_file), os.stat(shared_git_lock_path(root)))
    for _ in range(3):
        with IssueRunLock(namespace), SharedGitLock(root):
            assert issue_run_lock_live(namespace)
        issue_run_lock_live(namespace)
    paths = (namespace.run_lock_file, shared_git_lock_path(root))
    for before, path in zip(first, paths, strict=True):
        now = os.stat(path)
        assert (now.st_dev, now.st_ino) == (before.st_dev, before.st_ino)
        assert now.st_size == 0 and now.st_nlink == 1
        assert now.st_mtime_ns == before.st_mtime_ns


# -- L1: the shared-Git lock ---------------------------------------------------------


def test_l1_waits_for_its_holder_and_proceeds_once_released(root, spawn):
    holder = spawn()
    assert holder("acquire-l1", root) == "ok"
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 3:
            assert holder("release", "l1") == "ok"

    with SharedGitLock(root, operation="fetch abc", poll_seconds=0.01, sleep=sleep) as lock:
        assert lock.held
        assert locking._shared_git_holder == "fetch abc"
    assert waits == [0.01, 0.01, 0.01]
    assert locking._shared_git_holder == ""
    assert not _held(shared_git_lock_path(root))


def test_l1_wait_is_bounded_and_its_expiry_is_a_typed_transient(root, spawn):
    holder = spawn()
    assert holder("acquire-l1", root) == "ok"
    now = [100.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    lock = SharedGitLock(
        root,
        operation="fetch abc",
        wait_seconds=5,
        poll_seconds=2,
        clock=lambda: now[0],
        sleep=sleep,
    )
    before = _open_fds()
    with pytest.raises(GitLockTimeout, match=r"waited 5s for the shared-Git lock .* to fetch abc"):
        lock.acquire()
    assert now[0] == 105.0, "polled until the bound, never past it"
    assert not lock.held and locking._shared_git_holder == ""
    assert _open_fds() == before
    assert not issubclass(GitLockTimeout, LockError), "transient, never a lock-refusal exit"
    assert holder("release", "l1") == "ok"
    with SharedGitLock(root):
        pass


def test_the_l1_wait_bound_covers_every_operation_it_guards():
    """D11.3: the longest guarded timeout plus 60 s; the fetch is the longest."""
    guarded = (
        NETWORK_TIMEOUT_SECONDS,  # the controller's fetch
        agent_worktree.IMPORT_TIMEOUT_SECONDS,  # the strict import's index-pack
        GIT_TIMEOUT_SECONDS,  # each git process of the worktree check-then-create
    )
    assert SHARED_GIT_LOCK_WAIT_SECONDS == max(guarded) + 60
    assert max(guarded) == NETWORK_TIMEOUT_SECONDS


def test_l1_release_closes_and_is_idempotent(root):
    lock = SharedGitLock(root).acquire()
    lock.release()
    lock.release()
    assert not lock.held and not _held(shared_git_lock_path(root))


# -- the order L0 -> L2 -> L1 (D11.2) ------------------------------------------------


def test_the_l1_holder_takes_no_lock_calls_no_github_saves_nothing_launches_nothing(
    tmp_path,
):
    eng = make_engine(tmp_path / ".autoforge", ["never"])
    root = eng.coordination_root()
    namespace = issue_namespace(root, 7)
    held = ControllerLock(_l0(root)).acquire()
    inherited = os.dup(held.share())
    held.release()
    attempts: dict[str, Callable[[], object]] = {
        "L0": lambda: ControllerLock(_l0(root)).acquire(),
        "L0 adoption": lambda: ControllerLock.adopt(inherited, expected_root=root),
        "L2": lambda: IssueRunLock(namespace).acquire(),
        "L2 adoption": lambda: IssueRunLock.adopt(inherited, namespace),
        "L1": lambda: SharedGitLock(root).acquire(),
        "GitHub": lambda: GitHubClient(runner=execute)._invoke(["api", "user"]),
        "save": eng.save,
        "agent": lambda: eng._invoke_phase(Phase.ANALYZE_EXECUTE, lambda: None),
    }
    try:
        with SharedGitLock(root, operation="fetch abc"):
            for what, attempt in attempts.items():
                with pytest.raises(LockOrderError, match=r"holding the shared-Git lock \(L1\)"):
                    attempt()
                    pytest.fail(f"{what} ran under L1")
        assert _held(root), "nothing was released by a refused attempt"
        assert not namespace.directory.exists() and not eng.paths.state_file.exists()
        assert eng.provider.calls == []
        assert not issubclass(LockOrderError, LockError), "a bug, never a lock refusal"
    finally:
        os.close(inherited)
    eng.save()  # free again once L1 is released


def test_l0_then_l2_then_l1_is_the_order(root):
    namespace = issue_namespace(root, 7)
    with ControllerLock(_l0(root)), IssueRunLock(namespace), SharedGitLock(root) as l1:
        assert l1.held


STORM = textwrap.dedent(
    """
    import os, random, sys
    sys.path.insert(0, sys.argv[1])
    from pathlib import Path
    from autoforge import locking
    from autoforge.errors import LockError
    from autoforge.run_namespace import issue_namespace

    root, seed, rounds = Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
    rng = random.Random(seed)

    def inside(name):
        # Exclusive while held: an O_EXCL create fails if another holder is in.
        os.close(os.open(root / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        return name

    sys.stdin.readline()
    taken = 0
    for _ in range(rounds):
        held, marks = [], []
        try:
            if rng.random() < 0.3:
                try:
                    lock = locking.ControllerLock(root / "autoforge" / "controller.lock")
                    held.append(lock.acquire())
                    marks.append(inside("in-l0"))
                    taken += 1
                except LockError:
                    pass
            if rng.random() < 0.6:
                n = rng.randint(1, 3)
                try:
                    held.append(locking.IssueRunLock(issue_namespace(str(root), n)).acquire())
                    marks.append(inside(f"in-l2-{n}"))
                    taken += 1
                except LockError:
                    pass
            lock = locking.SharedGitLock(root, poll_seconds=0.001, wait_seconds=120)
            held.append(lock.acquire())
            marks.append(inside("in-l1"))
            taken += 1
        finally:
            for name in reversed(marks):
                os.unlink(root / name)
            for lock in reversed(held):
                lock.release()
    print(taken)
    """
)


def test_a_thousand_randomized_acquisitions_in_order_never_deadlock(root):
    """Four processes, 250 rounds each: every L1 wait ends within its bound
    (a GitLockTimeout would fail its process), and each lock admits one
    holder at a time (an O_EXCL marker per held lock)."""
    storms = [
        subprocess.Popen(
            [sys.executable, "-c", STORM, str(SRC), str(root), str(seed), "250"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for seed in range(4)
    ]
    try:
        for storm in storms:
            assert storm.stdin is not None
            storm.stdin.write("go\n")
            storm.stdin.flush()
        results = [storm.communicate(timeout=600) for storm in storms]
    finally:
        for storm in storms:
            if storm.poll() is None:
                storm.kill()
                storm.wait()
    for storm, (_, err) in zip(storms, results, strict=True):
        assert storm.returncode == 0, err
    taken = sum(int(out) for out, _ in results)
    assert taken >= 1000, "every round takes L1"
    assert sorted(p.name for p in root.iterdir()) == ["autoforge"], "every marker removed"


# -- the filesystem of a scheduler lock (D11.4) --------------------------------------

EXT4 = FilesystemType(magic=0xEF53)
NFS = FilesystemType(magic=0x6969)
LOCAL_FILESYSTEMS = {
    "ext4": EXT4,
    "xfs": FilesystemType(magic=0x58465342),
    "btrfs": FilesystemType(magic=0x9123683E),
    "tmpfs": FilesystemType(magic=0x01021994),
    "apfs": FilesystemType(name="apfs"),
}
NETWORK_FILESYSTEMS = {
    "nfs": NFS,
    "cifs": FilesystemType(magic=0xFF534D42),
    "smb2": FilesystemType(magic=0xFE534D42),
    "9p": FilesystemType(magic=0x01021997),
    "fuse": FilesystemType(magic=0x65735546),
    "ceph": FilesystemType(magic=0x00C36400),
    "nfs (macOS)": FilesystemType(name="nfs"),
    "smbfs (macOS)": FilesystemType(name="smbfs"),
    "macfuse (macOS)": FilesystemType(name="macfuse"),
}


def _fake_filesystems(monkeypatch, *, path: FilesystemType, fd: FilesystemType) -> None:
    def fake(target):
        return fd if isinstance(target, int) else path

    monkeypatch.setattr(locking, "filesystem_type", fake)


@pytest.mark.parametrize("name", sorted(NETWORK_FILESYSTEMS))
def test_a_network_filesystem_is_refused(root, monkeypatch, name):
    fs = NETWORK_FILESYSTEMS[name]
    assert network_filesystem(fs)
    _fake_filesystems(monkeypatch, path=fs, fd=fs)
    with pytest.raises(NetworkFilesystemError, match="flock is not a local kernel lock"):
        check_local_lock_filesystem(root, what="the coordination root")
    with pytest.raises(NetworkFilesystemError, match="coordination root"):
        IssueRunLock(issue_namespace(root, 7)).acquire()
    assert not issue_run_lock_live(issue_namespace(root, 7)), "refused before the flock"


@pytest.mark.parametrize("name", sorted(LOCAL_FILESYSTEMS))
def test_a_local_filesystem_is_accepted(root, monkeypatch, name):
    fs = LOCAL_FILESYSTEMS[name]
    assert network_filesystem(fs) == ""
    _fake_filesystems(monkeypatch, path=fs, fd=fs)
    check_local_lock_filesystem(root, what="the coordination root")
    with IssueRunLock(issue_namespace(root, 7)), SharedGitLock(root, require_local_filesystem=True):
        pass


def test_a_lock_file_on_nfs_below_a_local_root_is_refused(root, monkeypatch):
    """A mount below the root is caught by the opened descriptor's own check."""
    _fake_filesystems(monkeypatch, path=EXT4, fd=NFS)
    namespace = issue_namespace(root, 7)
    before = _open_fds()
    with pytest.raises(NetworkFilesystemError, match=r"run lock \(L2\) of issue 7 .* nfs"):
        IssueRunLock(namespace).acquire()
    with pytest.raises(NetworkFilesystemError, match=r"shared-Git lock \(L1\) .* nfs"):
        SharedGitLock(root, require_local_filesystem=True).acquire()
    assert _open_fds() == before and locking._shared_git_holder == ""
    assert not issue_run_lock_live(namespace)
    # Standalone commands never check: L0, and L1 without the requirement.
    with ControllerLock(_l0(root)), SharedGitLock(root):
        pass


def test_a_root_on_nfs_is_refused_even_when_the_lock_file_is_local(root, monkeypatch):
    _fake_filesystems(monkeypatch, path=NFS, fd=EXT4)
    with pytest.raises(NetworkFilesystemError, match=re.escape(f"root {root} is on a nfs")):
        SharedGitLock(root, require_local_filesystem=True).acquire()


def test_a_filesystem_that_cannot_be_identified_is_refused(root, monkeypatch):
    def unknown(target):
        raise OSError(errno.ENOSYS, "no filesystem-type check here")

    monkeypatch.setattr(locking, "filesystem_type", unknown)
    with pytest.raises(NetworkFilesystemError, match="cannot tell which filesystem"):
        IssueRunLock(issue_namespace(root, 7)).acquire()


def test_a_platform_without_a_check_cannot_tell(root, monkeypatch):
    monkeypatch.setattr(locking.sys, "platform", "sunos5")
    with pytest.raises(OSError, match="no filesystem-type check on sunos5"):
        filesystem_type(root)


@pytest.mark.skipif(not LINUX, reason="statfs(2) f_type is Linux's")
def test_statfs_reads_the_real_filesystem_of_a_path_and_a_descriptor(tmp_path):
    by_path = filesystem_type(tmp_path)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        by_fd = filesystem_type(fd)
    finally:
        os.close(fd)
    assert by_path == by_fd and by_path.magic is not None
    shown = subprocess.run(
        ["stat", "-f", "-c", "%t", str(tmp_path)], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert by_path.magic == int(shown, 16)
    with pytest.raises(OSError) as missing:
        filesystem_type(tmp_path / "missing")
    assert missing.value.errno == errno.ENOENT


# -- descriptor hygiene (D12.5) -------------------------------------------------------


def _inherited(result_stdout: str) -> set[tuple[int, int]]:
    return {
        (int(dev), int(ino)) for dev, ino in (line.split() for line in result_stdout.splitlines())
    }


def _identity(path: Path) -> tuple[int, int]:
    st = os.stat(path)
    return st.st_dev, st.st_ino


@pytest.mark.skipif(not LINUX, reason="reads /proc/self/fd")
@pytest.mark.parametrize("contain_orphans", [False, True])
def test_no_launch_inherits_a_lock_descriptor(root, contain_orphans):
    """Every launch goes through the executor; none inherits L0 (shared, as a
    scheduler holds it), L2 or L1, even while all three are held."""
    namespace = issue_namespace(root, 7)
    with ControllerLock(_l0(root)) as l0:
        l0.share()
        with IssueRunLock(namespace), SharedGitLock(root):
            result = execute(
                ExecutionRequest(
                    command=[sys.executable, "-c", LIST_FDS],
                    timeout_seconds=60,
                    contain_orphans=contain_orphans,
                )
            )
            locks = {
                _identity(root),
                _identity(namespace.run_lock_file),
                _identity(shared_git_lock_path(root)),
            }
    assert result.exit_code == 0, result.stderr
    seen = _inherited(result.stdout)
    assert seen, "the child listed its descriptors"
    assert not seen & locks


@pytest.mark.skipif(not LINUX, reason="reads /proc/self/fd")
def test_an_adopted_descriptor_is_not_inherited_even_by_a_launch_that_keeps_fds(root):
    """Adoption makes the descriptor non-inheritable, so even a launch that
    does not close descriptors (``close_fds=False``) cannot pass it on."""
    scheduler = ControllerLock(_l0(root)).acquire()
    inherited = os.dup(scheduler.share())
    os.set_inheritable(inherited, True)

    def listed() -> set[tuple[int, int]]:
        out = subprocess.run(
            [sys.executable, "-c", LIST_FDS], capture_output=True, text=True, close_fds=False
        )
        return _inherited(out.stdout)

    try:
        assert _identity(root) in listed(), "an inheritable descriptor is passed on"
        worker = ControllerLock.adopt(inherited, expected_root=root)
        assert _identity(root) not in listed()
        worker.release()
    finally:
        scheduler.release()
