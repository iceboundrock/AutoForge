"""Lock: first holder wins, second fails, release re-arms."""

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
