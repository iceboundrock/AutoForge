"""``merge.delete_branch`` and the operator's checkout during MERGE (#283).

`gh pr merge --delete-branch` deletes the local branch as well as the remote
one, and gh 2.103.2 asks git about the local branch in its working directory
on every `gh pr merge`, flag or not (pkg/cmd/pr/merge/merge.go). So the
controller never passes the flag, every `gh` it starts runs in an empty
directory of its own (``tests/test_github.py``), and ``merge.delete_branch``
deletes exactly one ref, ``refs/heads/<head>`` of the run's repository on
GitHub, after the merge is verified. What is covered:

- the deletion: what it targets, in which order, and its dry-run note;
- each refusal (a fork, the base or default branch, a branch that moved or
  is gone, a read that failed), each leaving the branch and the run alone;
- no deletion when the merge was not verified;
- a deletion that fails, lands with an error, or cannot be read back;
- the crash windows around it: neither the merge nor the deletion repeats;
- the regression: a `gh` that runs git in its working directory, as gh does,
  leaves the operator's checkout untouched through MERGE, with the setting
  on and off.

Offline throughout: the in-memory GitHub, and a shell-script `gh` where the
real client is exercised.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autoforge.errors import GitHubError, GitHubUnavailableError
from autoforge.github import GH_WORKDIR_PREFIX, GitHubClient
from autoforge.state import load_state
from autoforge.transitions import Phase

from .conftest import (
    BRANCH,
    PR,
    SHA_A,
    SHA_B,
    FakeGitHub,
    git_out,
    git_repo,
    local_git_gh,
    local_git_gh_runs,
    make_engine,
)
from .test_engine import _in_merge

REPO = "owner/repo"
_BRANCH_CALLS = ("get_repo", "get_branch_ref", "delete_branch_ref")


def _merging(tmp_state_dir, gh, *, delete_branch=True, branch_exists=True):
    """An engine in MERGE on ``PR`` at ``SHA_A``, its head branch on GitHub at that HEAD."""
    gh.add_pr(head_sha=SHA_A)
    if branch_exists:
        gh.branch_heads[BRANCH] = SHA_A
    eng = _in_merge(tmp_state_dir, gh)
    eng.config.merge.delete_branch = delete_branch
    return eng


def _after_merge(gh, change):
    """Make ``gh``'s merge land, then ``change(gh)``: GitHub as the deletion finds it."""
    real = gh.merge_pr

    def merge_then_change(*args, **kwargs):
        real(*args, **kwargs)
        change(gh)

    gh.merge_pr = merge_then_change


def _branch_calls(gh) -> list[tuple]:
    """The branch reads and writes made after the merge call, in order."""
    names = [call[0] for call in gh.calls]
    after = gh.calls[names.index("merge_pr") + 1 :] if "merge_pr" in names else []
    return [call for call in after if call[0] in _BRANCH_CALLS]


def _reopened(eng, tmp_state_dir, gh):
    """A new controller process on the same run, configured as ``eng`` was."""
    eng.close()
    eng2 = make_engine(tmp_state_dir, [], github=gh, cfg=eng.config)
    eng2.load()
    return eng2


def test_off_reads_and_deletes_no_branch(tmp_state_dir, fake_github):
    eng = _merging(tmp_state_dir, fake_github, delete_branch=False)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC" and "head branch" not in out.message
    assert [c for c in fake_github.calls if c[0] in _BRANCH_CALLS[1:]] == []
    assert fake_github.deleted_branches == [] and fake_github.branch_heads[BRANCH] == SHA_A
    assert eng._delete_branch_plan_notes(eng.state) == []


def test_on_deletes_exactly_the_merged_head_ref_after_the_merge_is_verified(
    tmp_state_dir, fake_github
):
    eng = _merging(tmp_state_dir, fake_github)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC"
    assert f"head branch {BRANCH!r} deleted on GitHub (read back as absent)" in out.message
    names = [call[0] for call in fake_github.calls]
    merged = names.index("merge_pr")
    # The post-merge read that verified the merge comes before any branch call.
    assert "get_pr" in names[merged:] and names.index("get_pr", merged) < names.index(
        "get_branch_ref"
    )
    assert _branch_calls(fake_github) == [
        ("get_repo", REPO),
        ("get_branch_ref", REPO, BRANCH),
        ("delete_branch_ref", REPO, BRANCH, "main"),
        ("get_branch_ref", REPO, BRANCH),
    ]
    assert fake_github.deleted_branches == [(REPO, BRANCH)]
    assert BRANCH not in fake_github.branch_heads and "main" in fake_github.branch_heads
    saved = load_state(eng.paths.state_file)
    assert saved.phase == Phase.UPDATE_EPIC and saved.counted_merged_prs == [PR]
    assert len(fake_github.merges) == 1


def test_the_dry_run_names_the_remote_only_deletion(tmp_state_dir, fake_github):
    eng = _merging(tmp_state_dir, fake_github)
    eng.state.current_branch = BRANCH
    plan = eng.step(dry_run=True, allow_merge=True).plan
    (note,) = [n for n in plan.notes if n.startswith("merge.delete_branch:")]
    assert f"would delete {BRANCH!r} on GitHub only" in note
    assert f"repos/{REPO}/git/refs/heads/<branch>" in note
    assert "no local branch is deleted or checked out" in note
    assert "--delete-branch" not in plan.command
    assert fake_github.merges == [] and fake_github.deleted_branches == []


def _fork(gh):
    gh.prs[PR].head_repository = "someone/repo"


def _head_repository_deleted(gh):
    gh.prs[PR].head_repository = ""


def _default_branch(gh):
    gh.default_branch = BRANCH


def _no_default_branch(gh):
    gh.default_branch = ""


def _moved(gh):
    gh.branch_heads[BRANCH] = SHA_B


def _gone(gh):
    del gh.branch_heads[BRANCH]


def _repo_unreadable(gh):
    gh.get_repo_error = GitHubError("gh api repos/owner/repo: HTTP 502")


def _ref_unreadable(gh):
    gh.branch_ref_error = GitHubError("gh api .../git/ref/heads/...: HTTP 403")


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (_fork, f"{BRANCH!r} is in someone/repo, not in {REPO}; a fork's branch"),
        (_head_repository_deleted, f"{BRANCH!r} is in a repository GitHub no longer names"),
        (_default_branch, f"{BRANCH!r} is the default branch of {REPO}"),
        (_no_default_branch, f"GitHub names no default branch for {REPO}"),
        (_moved, f"{BRANCH!r} now points at {SHA_B[:12]}, not at the merged HEAD"),
        (_gone, f"{BRANCH!r} no longer exists on GitHub"),
        (_repo_unreadable, f"the default branch of {REPO} could not be read (gh api"),
        (_ref_unreadable, f"{BRANCH!r} could not be read (gh api"),
    ],
    ids=lambda v: v.__name__.strip("_") if callable(v) else "",
)
def test_a_refusal_deletes_nothing_and_never_blocks_the_run(
    tmp_state_dir, fake_github, change, reason
):
    eng = _merging(tmp_state_dir, fake_github)
    _after_merge(fake_github, change)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC", out.message
    assert f"; head branch not deleted: {reason}" in out.message
    assert [c for c in fake_github.calls if c[0] == "delete_branch_ref"] == []
    assert fake_github.deleted_branches == []
    saved = load_state(eng.paths.state_file)
    assert saved.counted_merged_prs == [PR] and len(fake_github.merges) == 1


def test_a_branch_the_run_is_not_bound_to_is_not_deleted(tmp_state_dir, fake_github):
    eng = _merging(tmp_state_dir, fake_github)
    eng.state.current_branch = BRANCH
    eng._save()
    _after_merge(fake_github, lambda gh: setattr(gh.prs[PR], "head_ref", "autoforge/9-other"))
    fake_github.branch_heads["autoforge/9-other"] = SHA_A
    out = eng.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC", out.message
    assert f"but this run is bound to {BRANCH!r}" in out.message
    assert fake_github.deleted_branches == []


def test_the_base_branch_is_never_deleted(tmp_state_dir, fake_github):
    eng = _merging(tmp_state_dir, fake_github)
    fake_github.default_branch = "trunk"  # not the default: the base check alone refuses
    _after_merge(fake_github, lambda gh: setattr(gh.prs[PR], "head_ref", "main"))
    out = eng.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC", out.message
    assert "head branch not deleted: 'main' is the PR's base branch" in out.message
    assert fake_github.deleted_branches == [] and "main" in fake_github.branch_heads


def _leaves_open(gh):
    gh.merge_leaves_open = True


def _pushed_before_the_write(gh):
    real = gh.merge_pr

    def push_then_merge(*args, **kwargs):
        gh.set_head(SHA_B)
        real(*args, **kwargs)

    gh.merge_pr = push_then_merge


def _merged_at_another_head(gh):
    _after_merge(gh, lambda g: setattr(g.prs[PR], "head_sha", SHA_B))


@pytest.mark.parametrize(
    ("unverified", "phase"),
    [
        (_leaves_open, "BLOCKED"),
        (_pushed_before_the_write, "REVIEW"),
        (_merged_at_another_head, "BLOCKED"),
    ],
    ids=["left-open", "head-moved", "merged-unreviewed"],
)
def test_no_branch_is_touched_unless_the_merge_is_verified(
    tmp_state_dir, fake_github, unverified, phase
):
    eng = _merging(tmp_state_dir, fake_github)
    unverified(fake_github)
    out = eng.step(allow_merge=True)
    assert out.next_phase == phase, out.message
    assert [c for c in fake_github.calls if c[0] in _BRANCH_CALLS[1:]] == []
    assert fake_github.deleted_branches == [] and fake_github.branch_heads[BRANCH] == SHA_A


@pytest.mark.parametrize(
    ("lands", "outcome"),
    [
        (
            False,
            f"not deleted (the deletion failed: HTTP 403 forbidden): GitHub still has it at "
            f"{SHA_A[:12]}; delete it there if it should go",
        ),
        (True, "deleted on GitHub (read back as absent)"),
    ],
    ids=["refused", "landed-reply-lost"],
)
def test_a_failed_deletion_is_judged_by_reading_the_branch_back(
    tmp_state_dir, fake_github, lands, outcome
):
    eng = _merging(tmp_state_dir, fake_github)
    fake_github.delete_branch_error = GitHubError("HTTP 403 forbidden")
    fake_github.delete_branch_lands = lands
    out = eng.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC", out.message
    assert f"; head branch {BRANCH!r} {outcome}" in out.message
    assert fake_github.deleted_branches == [(REPO, BRANCH)]  # sent once, never retried
    assert load_state(eng.paths.state_file).counted_merged_prs == [PR]


def test_a_deletion_that_cannot_be_read_back_is_reported_unknown(tmp_state_dir, fake_github):
    eng = _merging(tmp_state_dir, fake_github)
    real = fake_github.delete_branch_ref

    def delete_then_lose_reads(*args, **kwargs):
        real(*args, **kwargs)
        fake_github.branch_ref_error = GitHubError("HTTP 502")

    fake_github.delete_branch_ref = delete_then_lose_reads
    out = eng.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC", out.message
    assert f"whether head branch {BRANCH!r} was deleted is unknown" in out.message
    assert "check it on GitHub" in out.message and fake_github.deleted_branches == [(REPO, BRANCH)]


# -- the crash windows: neither the merge nor the deletion is sent twice ---------------------


def _interrupting(gh, name):
    """Make ``gh.<name>`` land, then the controller die."""
    real = getattr(gh, name)

    def land_then_die(*args, **kwargs):
        real(*args, **kwargs)
        raise KeyboardInterrupt

    setattr(gh, name, land_then_die)
    return real


def test_a_crash_after_the_merge_deletes_the_branch_once_on_recovery(tmp_state_dir, fake_github):
    """Before the checkpoint nothing is counted: the PR reads MERGED, so the merge
    is recovered, not repeated, and the branch, still at the merged HEAD, deleted."""
    eng = _merging(tmp_state_dir, fake_github)
    real = _interrupting(fake_github, "merge_pr")
    with pytest.raises(KeyboardInterrupt):
        eng.step(allow_merge=True)
    fake_github.merge_pr = real
    saved = load_state(eng.paths.state_file)
    assert saved.phase == Phase.MERGE and saved.counted_merged_prs == []
    assert fake_github.deleted_branches == []
    out = _reopened(eng, tmp_state_dir, fake_github).step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC" and "already MERGED on GitHub (recovered)" in out.message
    assert f"head branch {BRANCH!r} deleted on GitHub" in out.message
    assert len(fake_github.merges) == 1 and fake_github.deleted_branches == [(REPO, BRANCH)]


def test_a_crash_after_the_deletion_neither_merges_nor_deletes_again(tmp_state_dir, fake_github):
    eng = _merging(tmp_state_dir, fake_github)
    real = _interrupting(fake_github, "delete_branch_ref")
    with pytest.raises(KeyboardInterrupt):
        eng.step(allow_merge=True)
    fake_github.delete_branch_ref = real
    saved = load_state(eng.paths.state_file)
    assert saved.phase == Phase.MERGE and saved.counted_merged_prs == [PR]  # the checkpoint
    assert BRANCH not in fake_github.branch_heads
    out = _reopened(eng, tmp_state_dir, fake_github).step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC", out.message
    assert "already counted" in out.message
    assert f"head branch {BRANCH!r} is gone from GitHub (read back as absent)" in out.message
    assert len(fake_github.merges) == 1 and fake_github.deleted_branches == [(REPO, BRANCH)]
    saved = load_state(eng.paths.state_file)
    assert saved.counted_merged_prs == [PR] and saved.merged_since_epic_update == 1


def test_a_crash_between_the_checkpoint_and_the_deletion_never_sends_it(tmp_state_dir, fake_github):
    """Whether a deletion went out cannot be told from the state, so a re-entry
    that finds the merge counted reads the branch back and sends nothing."""
    eng = _merging(tmp_state_dir, fake_github)

    def die_before_sending(*args, **kwargs):
        raise KeyboardInterrupt

    real = fake_github.delete_branch_ref
    fake_github.delete_branch_ref = die_before_sending
    with pytest.raises(KeyboardInterrupt):
        eng.step(allow_merge=True)
    fake_github.delete_branch_ref = real
    out = _reopened(eng, tmp_state_dir, fake_github).step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC", out.message
    assert f"head branch {BRANCH!r} not deleted (an earlier step of this merge" in out.message
    assert f"GitHub still has it at {SHA_A[:12]}" in out.message
    assert fake_github.deleted_branches == [] and fake_github.branch_heads[BRANCH] == SHA_A
    assert len(fake_github.merges) == 1


@pytest.mark.parametrize(
    ("error", "phase"),
    [
        (GitHubError("HTTP 401: Bad credentials"), "BLOCKED"),
        (GitHubUnavailableError("HTTP 502"), "MERGE"),
    ],
    ids=["conclusive", "transient"],
)
def test_a_failed_re_entry_after_the_checkpoint_says_the_merge_is_counted(
    tmp_state_dir, fake_github, error, phase
):
    """The re-entry's PR read failing is reported as it is: the merge was made and
    counted by the earlier step, never "nothing was merged or counted"."""
    eng = _merging(tmp_state_dir, fake_github)
    eng.config.merge.max_verification_attempts = 1  # a transient failure gives up at once
    real = _interrupting(fake_github, "delete_branch_ref")
    with pytest.raises(KeyboardInterrupt):
        eng.step(allow_merge=True)
    fake_github.delete_branch_ref = real
    fake_github.checks_error = error
    eng2 = _reopened(eng, tmp_state_dir, fake_github)
    out = eng2.step(allow_merge=True)
    assert out.next_phase == "BLOCKED", out.message
    reason = eng2.state.block_reason
    assert "An earlier step of this run merged and counted the PR" in reason, reason
    assert "Nothing was merged or counted" not in reason
    assert len(fake_github.merges) == 1 and fake_github.deleted_branches == [(REPO, BRANCH)]
    assert load_state(eng2.paths.state_file).counted_merged_prs == [PR]


# -- the regression: the operator's checkout through MERGE ------------------------------------


class _RealGhWrites(FakeGitHub):
    """The in-memory GitHub, its merge and branch deletion also sent through the real
    client to a real `gh` process: the two calls gh would run git for."""

    def __init__(self, gh_command: Path) -> None:
        super().__init__()
        self.client = GitHubClient(gh_command=str(gh_command), retry_delay_seconds=0)

    def merge_pr(self, url: str, method: str = "squash", match_head_sha: str = "") -> None:
        self.client.merge_pr(url, method=method, match_head_sha=match_head_sha)
        super().merge_pr(url, method, match_head_sha)

    def delete_branch_ref(self, repository: str, branch: str, *, default_branch: str) -> None:
        self.client.delete_branch_ref(repository, branch, default_branch=default_branch)
        super().delete_branch_ref(repository, branch, default_branch=default_branch)


@pytest.mark.parametrize("delete_branch", [False, True], ids=["off", "on"])
def test_merge_runs_no_git_in_the_operators_checkout(tmp_state_dir, monkeypatch, delete_branch):
    """The operator's checkout has the PR's head branch checked out, which is what
    makes gh's `--delete-branch` check out another branch, pull and delete it. The
    `gh` here runs those git commands in its working directory; none of them
    finds a repository, and the controller starts nothing else in the checkout."""
    checkout = git_repo(tmp_state_dir.parent)
    git_out("-C", str(checkout), "checkout", "-q", "-b", BRANCH)
    head = git_out("-C", str(checkout), "rev-parse", "HEAD")
    fake, log = local_git_gh(checkout.parent / f"{checkout.name}-bin", BRANCH)
    gh = _RealGhWrites(fake)
    eng = _merging(tmp_state_dir, gh, delete_branch=delete_branch)
    started: list[tuple[list[str], str | None]] = []
    real_popen = subprocess.Popen

    def recording_popen(args, *rest, **kwargs):
        started.append(([str(a) for a in args], kwargs.get("cwd")))
        return real_popen(args, *rest, **kwargs)

    # As the CLI does, the command holds the lock (whose own hardened git read of
    # the checkout's identity is the command's, not MERGE's) around the step.
    with eng.locked():
        monkeypatch.setattr(subprocess, "Popen", recording_popen)
        monkeypatch.chdir(checkout)  # what a process given no cwd would inherit
        out = eng.step(allow_merge=True)
        monkeypatch.undo()
    assert out.next_phase == "UPDATE_EPIC", out.message
    assert len(gh.merges) == 1
    assert gh.deleted_branches == ([(REPO, BRANCH)] if delete_branch else [])
    # Every process the controller started was the `gh`, each in its own empty directory.
    assert [argv[0] for argv, _ in started] == [str(fake)] * (2 if delete_branch else 1)
    assert all(argv[1:4] != ["pr", "merge", "--delete-branch"] for argv, _ in started)
    assert not any("--delete-branch" in argv for argv, _ in started)
    for _, cwd in started:
        assert cwd is not None and Path(cwd).name.startswith(GH_WORKDIR_PREFIX)
        assert not Path(cwd).resolve().is_relative_to(checkout.resolve())
    cwds, outcomes = local_git_gh_runs(log)
    assert all(not Path(c).is_relative_to(checkout.resolve()) for c in cwds)
    assert outcomes == [f"{op} 128" for op in ("rev-parse", "checkout", "branch-D")] * len(started)
    # The checkout is as the operator left it: on the head branch, at its commit.
    assert git_out("-C", str(checkout), "symbolic-ref", "--short", "HEAD") == BRANCH
    assert git_out("-C", str(checkout), "rev-parse", "HEAD") == head
    assert git_out("-C", str(checkout), "branch", "--list", BRANCH).strip("* ") == BRANCH
