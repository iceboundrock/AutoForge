"""Engine lock lifecycle: locked() spans a command; step()/run() never nest a lock."""

import pytest

from autoforge.errors import LockError
from autoforge.locking import ControllerLock
from autoforge.state import save_state
from autoforge.transitions import Phase
from tests.conftest import BRANCH, ISSUE, PR, SHA_A, FakeGitHub, block, make_engine

ANALYZE_OK = {
    "phase": "ANALYZE_EXECUTE",
    "status": "success",
    "issue_url": ISSUE,
    "pr_url": PR,
    "head_sha": SHA_A,
    "branch": BRANCH,
}


def _github() -> FakeGitHub:
    gh = FakeGitHub()
    gh.add_issue(ISSUE, "Feature")
    return gh


def _analyze_ok(gh: FakeGitHub) -> str:
    """Agent stdout for ANALYZE_EXECUTE; creates the PR it claims on the fake GitHub."""
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    return block(ANALYZE_OK)


def _can_lock(path) -> bool:
    try:
        ControllerLock(path).acquire().release()
    except LockError:
        return False
    return True


def _count_acquisitions(monkeypatch) -> list:
    """Record every *successful* lock acquisition from now on."""
    real = ControllerLock.acquire
    acquired = []

    def counting(self):
        lock = real(self)
        acquired.append(self.lock_path)
        return lock

    monkeypatch.setattr(ControllerLock, "acquire", counting)
    return acquired


def test_locked_holds_the_lock_for_the_block_and_is_not_reentrant(tmp_state_dir):
    eng = make_engine(tmp_state_dir, ["never"])
    assert not eng.lock_held
    with eng.locked():
        assert eng.lock_held
        assert not _can_lock(eng.paths.lock_file)
        with pytest.raises(LockError, match="already held by this engine"):
            with eng.locked():
                pass
        assert eng.lock_held, "a refused nested acquisition must not release the outer lock"
    assert not eng.lock_held and _can_lock(eng.paths.lock_file)


def test_locked_releases_on_error(tmp_state_dir):
    eng = make_engine(tmp_state_dir, ["never"])
    with pytest.raises(RuntimeError):
        with eng.locked():
            raise RuntimeError("boom")
    assert not eng.lock_held and _can_lock(eng.paths.lock_file)


def test_locked_fails_when_another_controller_holds_the_lock(tmp_state_dir):
    eng = make_engine(tmp_state_dir, ["never"])
    with ControllerLock(eng.paths.lock_file):
        with pytest.raises(LockError, match="another AutoForge controller"):
            with eng.locked():
                pass
    assert not eng.lock_held


def test_run_and_step_inside_locked_reuse_the_held_lock(tmp_state_dir, monkeypatch):
    """No second acquisition, and the lock stays held while the agent executes."""
    seen: list[bool] = []
    gh = _github()

    def agent(req):
        seen.append(_can_lock(tmp_state_dir / "controller.lock"))
        return _analyze_ok(gh)

    eng = make_engine(tmp_state_dir, agent, github=gh)
    acquired = _count_acquisitions(monkeypatch)
    with eng.locked():
        eng._save()
        outcomes = eng.run(max_steps=1)
        assert [o.next_phase for o in outcomes] == ["ANALYZE_EXECUTE"]
        out = eng.step()
        assert out.next_phase == "REVIEW"
    assert seen == [False]
    assert acquired == [tmp_state_dir / "controller.lock"]


@pytest.mark.parametrize("entry", ["run", "step"])
def test_execution_outside_locked_rereads_disk_state_under_its_own_lock(
    tmp_state_dir, monkeypatch, entry
):
    """A snapshot taken by load() before the lock is never executed.

    Another controller finished the run (DONE) after this engine loaded its
    snapshot. The engine must execute what is on disk under its lock: no
    agent runs and the other controller's state is not overwritten.
    """
    gh = _github()
    seeded = make_engine(tmp_state_dir, ["never"], github=gh)
    seeded.state.phase = Phase.ANALYZE_EXECUTE
    seeded._save()

    eng = make_engine(tmp_state_dir, lambda req: _analyze_ok(gh), github=gh)
    snapshot = eng.load()
    assert snapshot.phase == Phase.ANALYZE_EXECUTE

    other = make_engine(tmp_state_dir, ["never"], github=gh)
    other.state.phase = Phase.DONE
    other._save()

    acquired = _count_acquisitions(monkeypatch)
    if entry == "run":
        # DONE is a stop phase: the loop ends before any step.
        assert eng.run(max_steps=3) == []
    else:
        assert eng.step().message == "workflow already DONE"
    assert eng.provider.calls == []
    assert eng.state is not snapshot
    assert eng.state.run_id == other.state.run_id and eng.state.phase == Phase.DONE
    assert acquired == [tmp_state_dir / "controller.lock"]
    assert _can_lock(tmp_state_dir / "controller.lock")


def test_in_memory_new_run_state_is_executed_as_is(tmp_state_dir):
    """new_run() state is not a disk snapshot: run() executes it without a re-read."""
    gh = _github()
    eng = make_engine(tmp_state_dir, lambda req: _analyze_ok(gh), github=gh)
    save_state(eng.state, eng.paths.state_file)
    run_id = eng.state.run_id
    outcomes = eng.run(max_steps=2)
    assert [o.next_phase for o in outcomes] == ["ANALYZE_EXECUTE", "REVIEW"]
    assert eng.state.run_id == run_id and eng.provider.calls != []


def test_dry_run_never_takes_the_lock(tmp_state_dir, monkeypatch):
    eng = make_engine(tmp_state_dir, ["never"])
    with ControllerLock(eng.paths.lock_file):
        acquired = _count_acquisitions(monkeypatch)
        eng.step(dry_run=True)
        eng.run(max_steps=2, dry_run=True)
    assert acquired == []
