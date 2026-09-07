"""Engine lock lifecycle: locked() spans a command; step()/run() never nest a lock.

The lock is keyed by the repository (git common dir) that contains the engine's
workdir, never by the state directory (R6-F1).
"""

import subprocess

import pytest

import autoforge.engine as engine_mod
from autoforge.errors import LockError
from autoforge.locking import ControllerLock, repository_lock_path
from autoforge.state import save_state
from autoforge.transitions import Phase
from tests.conftest import BRANCH, ISSUE, PR, SHA_A, FakeGitHub, block, git_repo, make_engine

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
        assert not _can_lock(eng.lock_path())
        with pytest.raises(LockError, match="already held by this engine"):
            with eng.locked():
                pass
        assert eng.lock_held, "a refused nested acquisition must not release the outer lock"
    assert not eng.lock_held and _can_lock(eng.lock_path())


def test_locked_releases_on_error(tmp_state_dir):
    eng = make_engine(tmp_state_dir, ["never"])
    with pytest.raises(RuntimeError):
        with eng.locked():
            raise RuntimeError("boom")
    assert not eng.lock_held and _can_lock(eng.lock_path())


def test_locked_fails_when_another_controller_holds_the_lock(tmp_state_dir):
    eng = make_engine(tmp_state_dir, ["never"])
    with ControllerLock(eng.lock_path()):
        with pytest.raises(LockError, match="another AutoForge controller"):
            with eng.locked():
                pass
    assert not eng.lock_held


def test_run_and_step_inside_locked_reuse_the_held_lock(tmp_state_dir, monkeypatch):
    """No second acquisition, and the lock stays held while the agent executes."""
    seen: list[bool] = []
    gh = _github()

    def agent(req):
        seen.append(_can_lock(eng.lock_path()))
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
    assert acquired == [eng.lock_path()]


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
    assert acquired == [eng.lock_path()]
    assert _can_lock(eng.lock_path())


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
    with ControllerLock(eng.lock_path()):
        acquired = _count_acquisitions(monkeypatch)
        eng.step(dry_run=True)
        eng.run(max_steps=2, dry_run=True)
    assert acquired == []


def test_dry_run_never_resolves_the_repository(tmp_path, monkeypatch):
    """A dry run spawns no subprocess: not even `git rev-parse` for the lock path."""
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()

    def never(workdir):
        raise AssertionError(f"resolved the lock for {workdir} during a dry run")

    monkeypatch.setattr(engine_mod, "repository_lock_path", never)
    eng = make_engine(not_a_repo / ".autoforge", ["never"], workdir=not_a_repo)
    eng.step(dry_run=True)
    eng.run(max_steps=2, dry_run=True)


# -- R6-F1: the lock is scoped to the repository, not to the state directory ---
def test_lock_path_is_the_repository_git_dir_and_is_cached(tmp_path):
    repo = git_repo(tmp_path)
    eng = make_engine(repo / "state-a", ["never"], workdir=repo)
    expected = (repo / ".git" / "autoforge" / "controller.lock").resolve()
    assert eng.lock_path() == expected
    assert eng.lock_path() is eng.lock_path(), "resolved once, then cached"
    assert not (repo / "state-a").exists(), "nothing in the state dir before the first save"


def test_engines_with_distinct_state_dirs_on_one_repository_share_the_lock(tmp_path):
    repo = git_repo(tmp_path)
    first = make_engine(repo / "state-a", ["never"], workdir=repo)
    second = make_engine(repo / ".autoforge", ["never"], workdir=repo)
    assert first.lock_path() == second.lock_path()
    with first.locked():
        with pytest.raises(LockError, match="another AutoForge controller"):
            with second.locked():
                pass
        assert not second.lock_held
    with second.locked():
        assert second.lock_held


def test_engine_started_in_a_subdirectory_shares_the_lock(tmp_path):
    repo = git_repo(tmp_path)
    sub = repo / "pkg" / "deep"
    sub.mkdir(parents=True)
    root = make_engine(repo / ".autoforge", ["never"], workdir=repo)
    nested = make_engine(sub / ".autoforge", ["never"], workdir=sub)
    assert nested.lock_path() == root.lock_path()
    with root.locked():
        with pytest.raises(LockError, match="another AutoForge controller"):
            with nested.locked():
                pass


def test_engine_in_a_linked_worktree_shares_the_lock(tmp_path):
    repo = git_repo(tmp_path / "main")
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
    )
    worktree = tmp_path / "wt"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(worktree)], check=True)
    main = make_engine(repo / ".autoforge", ["never"], workdir=repo)
    linked = make_engine(worktree / ".autoforge", ["never"], workdir=worktree)
    assert linked.lock_path() == main.lock_path() == repository_lock_path(repo)
    with main.locked():
        with pytest.raises(LockError, match="another AutoForge controller"):
            with linked.locked():
                pass


def test_distinct_repositories_do_not_share_the_lock(tmp_path):
    one = make_engine(
        tmp_path / "one" / ".autoforge", ["never"], workdir=git_repo(tmp_path / "one")
    )
    two = make_engine(
        tmp_path / "two" / ".autoforge", ["never"], workdir=git_repo(tmp_path / "two")
    )
    assert one.lock_path() != two.lock_path()
    with one.locked(), two.locked():
        assert one.lock_held and two.lock_held


def test_locking_outside_a_git_repository_is_a_lock_error(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    eng = make_engine(plain / ".autoforge", ["never"], workdir=plain)
    with pytest.raises(LockError, match="not inside a git repository"):
        with eng.locked():
            pass
    assert not eng.lock_held
    with pytest.raises(LockError, match="not inside a git repository"):
        eng.step()
    assert eng.provider.calls == []
    assert not (plain / ".autoforge").exists()
