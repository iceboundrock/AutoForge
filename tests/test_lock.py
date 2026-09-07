"""Lock: first holder wins, second fails, release re-arms; the entry must be a regular file."""

import errno
import os
import stat

import pytest

from autoforge.errors import LockError
from autoforge.locking import ControllerLock


def test_acquire_and_release(tmp_path):
    lock = ControllerLock(tmp_path / "controller.lock")
    assert not lock.held
    lock.acquire()
    assert lock.held
    lock.release()
    assert not lock.held
    # re-acquire after release works
    with ControllerLock(tmp_path / "controller.lock"):
        pass


def test_second_acquire_fails_while_held(tmp_path):
    path = tmp_path / "controller.lock"
    with ControllerLock(path):
        with pytest.raises(LockError, match="another AutoForge controller"):
            ControllerLock(path).acquire()


def test_context_manager_releases_on_exception(tmp_path):
    path = tmp_path / "controller.lock"
    with pytest.raises(RuntimeError):
        with ControllerLock(path):
            raise RuntimeError("boom")
    # lock must be free again
    with ControllerLock(path):
        pass


# -- lock entry validation (PFR-F1 on PR #28) ----------------------------------
def test_lock_file_holds_exactly_the_current_pid(tmp_path):
    path = tmp_path / "controller.lock"
    path.write_text("4242\n", encoding="utf-8")  # a stale PID from an earlier run
    with ControllerLock(path):
        assert path.read_text(encoding="utf-8") == f"{os.getpid()}\n"
    with ControllerLock(path):
        # replaced, not appended: no PID growth across acquisitions
        assert path.read_text(encoding="utf-8") == f"{os.getpid()}\n"


def test_symlink_lock_entry_is_refused_and_target_untouched(tmp_path):
    target = tmp_path / "unrelated.txt"
    target.write_text("keep\n", encoding="utf-8")
    path = tmp_path / "controller.lock"
    path.symlink_to(target)
    lock = ControllerLock(path)
    with pytest.raises(LockError, match="symbolic link"):
        lock.acquire()
    assert not lock.held
    assert target.read_text(encoding="utf-8") == "keep\n"
    assert path.is_symlink() and os.readlink(path) == str(target)


def test_hard_linked_lock_entry_is_refused_and_target_untouched(tmp_path):
    """PFR-F2: a hard link passes O_NOFOLLOW + S_ISREG but shares its inode."""
    target = tmp_path / "unrelated.txt"
    target.write_text("preserve\n", encoding="utf-8")
    path = tmp_path / "controller.lock"
    os.link(target, path)
    assert os.lstat(path).st_nlink == 2
    lock = ControllerLock(path)
    with pytest.raises(LockError, match="2 hard links"):
        lock.acquire()
    assert not lock.held
    assert target.read_text(encoding="utf-8") == "preserve\n"
    assert path.read_text(encoding="utf-8") == "preserve\n"
    assert os.lstat(path).st_ino == os.lstat(target).st_ino  # entry left in place
    # Nothing else held the flock: a fresh, single-name lock file works.
    os.unlink(path)
    with ControllerLock(path):
        assert target.read_text(encoding="utf-8") == "preserve\n"


def test_dangling_symlink_lock_entry_is_refused_and_creates_nothing(tmp_path):
    path = tmp_path / "controller.lock"
    path.symlink_to(tmp_path / "missing")
    with pytest.raises(LockError, match="symbolic link"):
        ControllerLock(path).acquire()
    assert path.is_symlink() and not (tmp_path / "missing").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["controller.lock"]


def test_fifo_lock_entry_is_refused_without_blocking(tmp_path):
    path = tmp_path / "controller.lock"
    os.mkfifo(path)
    with pytest.raises(LockError, match="a FIFO, not a regular file"):
        ControllerLock(path).acquire()
    assert stat.S_ISFIFO(os.lstat(path).st_mode)


def test_directory_lock_entry_is_refused(tmp_path):
    path = tmp_path / "controller.lock"
    path.mkdir()
    with pytest.raises(LockError, match="directory"):
        ControllerLock(path).acquire()
    assert path.is_dir() and list(path.iterdir()) == []


def test_unwritable_lock_directory_is_a_lock_error(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    parent = tmp_path / "ro"
    parent.mkdir()
    parent.chmod(0o500)
    try:
        with pytest.raises(LockError, match="cannot open it"):
            ControllerLock(parent / "controller.lock").acquire()
    finally:
        parent.chmod(0o700)


def test_flock_failure_other_than_contention_is_a_lock_error(tmp_path, monkeypatch):
    import fcntl

    def broken(fd, op):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fcntl, "flock", broken)
    lock = ControllerLock(tmp_path / "controller.lock")
    with pytest.raises(LockError, match="flock failed"):
        lock.acquire()
    assert not lock.held


def test_pid_write_failure_closes_descriptor_and_is_a_lock_error(tmp_path, monkeypatch):
    def broken(fd, data):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "write", broken)
    path = tmp_path / "controller.lock"
    lock = ControllerLock(path)
    with pytest.raises(LockError, match="holder PID"):
        lock.acquire()
    assert not lock.held
    monkeypatch.undo()
    # the descriptor (and with it the flock) was released on the failure path
    with ControllerLock(path):
        pass


# -- repository_lock_path (R6-F1) ----------------------------------------------
def _git(*argv, cwd):
    import subprocess

    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *argv], cwd=cwd, check=True)


def test_repository_lock_path_is_inside_the_git_common_dir(tmp_path):
    from autoforge.locking import repository_lock_path

    _git("init", "-q", cwd=tmp_path)
    assert (
        repository_lock_path(tmp_path)
        == (tmp_path / ".git" / "autoforge" / "controller.lock").resolve()
    )


def test_repository_lock_path_is_the_same_from_every_working_directory(tmp_path):
    """Root, subdirectory, relative cwd, symlinked spelling and linked worktree agree."""
    from autoforge.locking import repository_lock_path

    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    sub = repo / "pkg" / "deep"
    sub.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    worktree = tmp_path / "wt"
    _git("worktree", "add", "-q", str(worktree), cwd=repo)

    expected = repository_lock_path(repo)
    assert repository_lock_path(sub) == expected
    assert repository_lock_path(alias) == expected
    assert repository_lock_path(alias / "pkg") == expected
    assert repository_lock_path(worktree) == expected
    assert "worktrees" not in expected.parts


def test_repository_lock_path_differs_between_repositories(tmp_path):
    from autoforge.locking import repository_lock_path

    for name in ("one", "two"):
        (tmp_path / name).mkdir()
        _git("init", "-q", cwd=tmp_path / name)
    assert repository_lock_path(tmp_path / "one") != repository_lock_path(tmp_path / "two")


def test_repository_lock_path_outside_a_repository_is_a_lock_error(tmp_path, monkeypatch):
    from autoforge.locking import repository_lock_path

    # Stop discovery at the filesystem boundary / above tmp_path so the test
    # never picks up an enclosing repository.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(LockError, match="not inside a git repository"):
        repository_lock_path(plain)
    assert list(plain.iterdir()) == []


def test_repository_lock_path_when_git_cannot_run_is_a_lock_error(tmp_path):
    from autoforge.errors import ExecutionError
    from autoforge.executor import ExecutionResult
    from autoforge.locking import repository_lock_path

    def missing(req):
        raise ExecutionError("failed to spawn git: [Errno 2] No such file or directory")

    with pytest.raises(LockError, match="git could not be run"):
        repository_lock_path(tmp_path, runner=missing)

    def timed_out(req):
        return ExecutionResult(
            command=req.command,
            cwd=req.cwd,
            exit_code=-9,
            stdout="",
            stderr="",
            started_at="",
            finished_at="",
            timed_out=True,
        )

    with pytest.raises(LockError, match="timed out"):
        repository_lock_path(tmp_path, runner=timed_out)


def test_repository_lock_path_runs_git_in_the_workdir_and_joins_relative_output(tmp_path):
    from autoforge.executor import ExecutionResult
    from autoforge.locking import repository_lock_path

    seen = []

    def fake_git(req):
        seen.append(req)
        return ExecutionResult(
            command=req.command,
            cwd=req.cwd,
            exit_code=0,
            stdout="../.git\n",  # relative to cwd, as git prints it from a subdirectory
            stderr="",
            started_at="",
            finished_at="",
        )

    sub = tmp_path / "sub"
    sub.mkdir()
    path = repository_lock_path(sub, runner=fake_git)
    assert path == (tmp_path / ".git" / "autoforge" / "controller.lock").resolve()
    assert seen[0].command == ["git", "rev-parse", "--git-common-dir"]
    assert seen[0].cwd == str(sub)
