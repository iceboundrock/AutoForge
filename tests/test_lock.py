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
