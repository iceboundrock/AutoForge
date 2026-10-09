"""Engine: verification of agent claims, SHA binding, routing, recovery, gate."""

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from autoforge import __prompt_version__
from autoforge.claims import (
    REVIEW,
    render_follow_up_marker,
    render_implementation_marker,
    render_progress_marker,
    scan,
)
from autoforge.config import default_config
from autoforge.effects import EffectKind, Stage, compose_append
from autoforge.effects import follow_up_issue_body as controller_follow_up_body
from autoforge.effects import review_comment_body as render_review_comment
from autoforge.errors import (
    ConfigurationError,
    ControlResultValidationError,
    ExecutionError,
    ExecutionTimeoutError,
    GitHubError,
    GitHubNotFoundError,
    GitHubUnavailableError,
    GitTransportError,
    StateError,
    StateTransitionError,
    VerificationError,
)
from autoforge.executor import ExecutionResult, execute
from autoforge.git_transport import (
    LOCAL_GIT_ENV_ALLOWLIST,
    LOCAL_GIT_SWITCHES,
    NETWORK_GIT_ENV_ALLOWLIST,
    GitRemote,
)
from autoforge.github import (
    ChangedFile,
    CheckInfo,
    GitHubClient,
    MergeQueueStatus,
    WorkflowJob,
    WorkflowRunJobs,
)
from autoforge.loop_guard import RESULT_NEEDS_FIX, review_record
from autoforge.providers import AgentExecutionResult, OpenCodeProvider, ScriptedProvider
from autoforge.result_parser import (
    MAX_FINDING_ID_CHARS,
    MAX_FINDING_LOCATION_CHARS,
    MAX_FINDING_RESOLUTION_CHARS,
    MAX_FINDING_TITLE_CHARS,
    MAX_FINDINGS_PER_REVIEW,
    MAX_FIX_RATIONALE_CHARS,
    MAX_URL_CHARS,
    ReviewResult,
    published_markdown_problem,
)
from autoforge.state import load_state
from autoforge.transitions import Phase
from tests.conftest import (
    BASE_RUN_ID,
    BRANCH,
    CI_RUN_ID,
    CI_WORKFLOW_ID,
    EPIC,
    FOLLOW_UP_TEXT,
    FOLLOW_UP_TITLE,
    GIT_IDENT,
    ISSUE,
    ISSUE3,
    MAIN_SHA,
    MERGE_BASE,
    MERGE_BASE_B,
    NO_CHANGE_RATIONALE,
    PR,
    SHA_A,
    SHA_B,
    SHA_C,
    FakeGitHub,
    analyze_payload,
    block,
    ci_check,
    ci_jobs,
    comment_url,
    commit_in,
    connect_origin,
    controller_review_comment,
    deferred_to,
    fix_commit,
    fix_payload,
    fixed,
    fixer,
    follow_up_issue_body,
    git_out,
    git_repo,
    implement,
    implementation_pr_body,
    make_engine,
    new_follow_up,
    no_change,
    post_progress_comment,
    progress_comment_body,
    publish_pr_head,
    review_comment_body,
    review_result,
    reviewed_head_in,
    scripted,
)

PR41 = "https://github.com/owner/repo/pull/41"


def _finding(rnd: int, n: int = 1, cls: str = "nit") -> dict:
    return {
        "id": f"R{rnd}-F{n}",
        "classification": cls,
        "title": "typo",
        "location": "src/x.py:1",
        "required_resolution": "fix the typo",
    }


def review_payload(rnd: int, sha: str, findings: list[dict]) -> dict:
    """A REMOTE REVIEW result (#162): the controller renders and posts the comment."""
    return review_result(rnd, sha, findings)


def _in_review(
    tmp_state_dir,
    gh: FakeGitHub,
    script,
    round_done: int = 0,
    head: str = SHA_A,
    origin: bool = False,
):
    """A run in REVIEW after ``round_done`` rounds, bound to the PR's HEAD.

    With ``origin`` that HEAD is a real commit published to the PR branch
    (``head`` is not used), so a FIX that follows commits on it and the
    controller pushes the fix (#163).
    """
    eng = make_engine(tmp_state_dir, script, github=gh, origin=origin)
    if origin:
        head = publish_pr_head(eng)
    gh.add_pr(head_sha=head)
    eng.state.phase = Phase.REVIEW
    eng.state.current_pr_url = PR
    eng.state.current_branch = BRANCH
    eng.state.current_head_sha = head
    eng.state.current_base_ref = "main"
    eng.state.current_merge_base_sha = eng.origin.merge_base("main", head) if origin else MERGE_BASE
    eng.state.review_round = round_done
    return eng


def _install_opencode_responses(eng, outcomes):
    """Use the real adapter around a sequence of fake OpenCode exits."""
    seen = []

    def runner(req):
        seen.append(req)
        exit_code, stdout, stderr = outcomes.pop(0)
        return ExecutionResult(req.command, req.cwd, exit_code, stdout, stderr, "t", "t")

    eng.providers._overrides["opencode"] = OpenCodeProvider(runner=runner)
    return seen


# -- INITIALIZING --------------------------------------------------------------
def test_initializing_verifies_issue_then_advances(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, [], github=fake_github)
    out = eng.step()
    assert out.next_phase == "ANALYZE_EXECUTE"
    assert eng.provider.calls == []
    assert ("get_issue", ISSUE) in fake_github.calls
    assert load_state(eng.paths.state_file).phase == Phase.ANALYZE_EXECUTE


def test_initializing_rejects_closed_issue(tmp_state_dir, fake_github):
    fake_github.add_issue(ISSUE, state="CLOSED")
    eng = make_engine(tmp_state_dir, [], github=fake_github)
    with pytest.raises(VerificationError, match="CLOSED"):
        eng.step()
    assert eng.state.phase == Phase.INITIALIZING


def test_initializing_rejects_repository_mismatch(tmp_state_dir):
    gh = FakeGitHub(repo="someone/else")
    eng = make_engine(tmp_state_dir, [], github=gh)
    with pytest.raises(VerificationError, match="repository mismatch"):
        eng.step()


def test_initializing_rejects_missing_issue_as_verification_error(tmp_state_dir, fake_github):
    del fake_github.issues[ISSUE]
    eng = make_engine(tmp_state_dir, [], github=fake_github)
    with pytest.raises(VerificationError, match="does not exist on GitHub"):
        eng.step()
    assert eng.state.phase == Phase.INITIALIZING


def test_initializing_rejects_a_casing_variant_of_the_epic(tmp_state_dir, fake_github):
    """Owner/repo names are case-insensitive on GitHub: .../OWNER/Repo/issues/1 is the EPIC."""
    eng = make_engine(tmp_state_dir, [], github=fake_github)
    eng.state.current_issue_url = "https://github.com/OWNER/Repo/issues/1"
    with pytest.raises(VerificationError, match="is the EPIC itself"):
        eng.step()
    assert eng.state.phase == Phase.INITIALIZING
    assert [c for c in fake_github.calls if c[0] == "get_issue"] == []


@pytest.mark.parametrize(
    "error",
    [
        GitHubError("`gh issue view` failed (exit 1): HTTP 401: Bad credentials"),
        GitHubUnavailableError("`gh issue view` failed (exit 1): HTTP 502"),
    ],
)
def test_initializing_github_failure_is_not_a_verification_failure(
    tmp_state_dir, fake_github, error
):
    """Auth / outage while reading the issue is not "the issue is unusable"."""
    fake_github.get_issue_error = error
    eng = make_engine(tmp_state_dir, [], github=fake_github)
    with pytest.raises(type(error)) as info:
        eng.step()
    assert not isinstance(info.value, VerificationError)
    assert eng.state.phase == Phase.INITIALIZING


def test_initializing_rejects_the_epic_as_the_issue(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, [], github=fake_github)
    eng.state.current_issue_url = EPIC
    with pytest.raises(VerificationError, match="is the EPIC itself"):
        eng.step()
    assert eng.state.phase == Phase.INITIALIZING
    assert ("get_issue", EPIC) not in fake_github.calls  # rejected before any GitHub read


# -- ANALYZE_EXECUTE ---------------------------------------------------------------
MARKER = render_implementation_marker(ISSUE)
CLOSING = f"Closes #2\n\n{MARKER}"


def _analyzed(eng):
    """INITIALIZING, then the ANALYZE_EXECUTE step; its outcome."""
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    return eng.step()


def _worktree_head(req) -> str:
    return git_out("-C", req.cwd, "rev-parse", "HEAD")


def _reports_head(req, **overrides) -> str:
    """An agent whose work is already committed: it reports the worktree's HEAD."""
    return block(analyze_payload(_worktree_head(req), **overrides))


def _from_start(req, **overrides) -> str:
    """The agent the prompt asks for: detached at the start commit, then one commit."""
    start = re.search(r"Start commit: `([0-9a-f]{40})`", req.prompt)
    assert start, "the prompt names no start commit"
    git_out("-C", req.cwd, "checkout", "-q", "--detach", start.group(1))
    return implement(req, **overrides)


def _side_commit(repo, parent: str, message: str = "Earlier work (#2)") -> str:
    """A commit on ``parent`` that moves no ref of ``repo`` (work pushed by someone else)."""
    tree = git_out("-C", str(repo), "rev-parse", f"{parent}^{{tree}}")
    return git_out("-C", str(repo), *GIT_IDENT, "commit-tree", tree, "-p", parent, "-m", message)


def _earlier_work(eng) -> str:
    """Publish a commit for this issue to ``autoforge/2`` before the run."""
    sha = _side_commit(eng.workdir, eng.origin.head("main"))
    return eng.origin.publish(eng.workdir, sha, "autoforge/2")


def _crash(self):
    """The process dies right after the plan's save, before anything is sent."""
    raise KeyboardInterrupt


def _resumed(eng, tmp_state_dir, gh, script=None):
    """A new controller process on the same state, git remote and GitHub."""
    eng.close()
    eng2 = make_engine(tmp_state_dir, script, github=gh)
    connect_origin(eng2, eng.origin)
    eng2.origin = eng.origin
    eng2.load()
    return eng2


def test_analyze_the_controller_pushes_the_agents_commit_and_opens_the_pr(
    tmp_state_dir, fake_github
):
    """#161: the agent only commits. The controller pushes that commit to
    ``autoforge/<n>``, opens the PR with the agent's title and body followed
    by its own ``Closes #n`` line and the issue's marker, reads it back and
    binds it; the prompt names the base the entry fetched."""
    eng = make_engine(tmp_state_dir, implement, github=fake_github, origin=True)
    base = eng.origin.head("main")
    out = _analyzed(eng)
    assert out.next_phase == "REVIEW"
    (req,) = eng.provider.calls
    assert f"- Default branch: `main`, at `{base}` when the controller" in req.prompt
    assert f"Start commit: `{base}` (the default branch head)" in req.prompt
    assert "Branch the controller will push to: `autoforge/2`" in req.prompt
    head = _worktree_head(req)
    assert eng.origin.head("autoforge/2") == head
    assert git_out("-C", req.cwd, "rev-parse", "HEAD^") == base
    s = load_state(eng.paths.state_file)
    assert (s.phase, s.attempt, s.review_round) == (Phase.REVIEW, 0, 0)
    assert (s.current_pr_url, s.current_head_sha, s.current_branch, s.current_base_ref) == (
        PR,
        head,
        "autoforge/2",
        "main",
    )
    pr = fake_github.prs[PR]
    assert (pr.title, pr.head_ref, pr.base_ref, pr.head_sha) == (
        "Add the feature",
        "autoforge/2",
        "main",
        head,
    )
    assert pr.body == f"Adds the feature.\n\nTested with `pytest`.\n\n{CLOSING}"
    assert pr.linked_issue_numbers == [2]
    assert [w[0] for w in fake_github.effect_writes] == ["create_pull_request"]
    assert f"pushed {head[:12]} to autoforge/2 and opened PR {PR}" in out.message


def test_analyze_issue_claim_is_compared_by_identity_not_url_string(tmp_state_dir, fake_github):
    eng = make_engine(
        tmp_state_dir,
        lambda req: implement(req, issue_url="https://github.com/OWNER/Repo/issues/2"),
        github=fake_github,
        origin=True,
    )
    assert _analyzed(eng).next_phase == "REVIEW"
    assert len(eng.provider.calls) == 1


def _attached(req) -> str:
    git_out("-C", req.cwd, "checkout", "-q", "-b", "my-work")
    return implement(req)


def _detach_and_report(req) -> str:
    git_out("-C", req.cwd, "checkout", "-q", "--detach")
    return _reports_head(req)


def _unrelated_history(req) -> str:
    tree = git_out("-C", req.cwd, "rev-parse", "HEAD^{tree}")
    root = git_out("-C", req.cwd, *GIT_IDENT, "commit-tree", tree, "-m", "Implement (#2)")
    git_out("-C", req.cwd, "checkout", "-q", "--detach", root)
    return _reports_head(req)


_FAKE_TOKEN = "gh" + "p_" + "FAKE0000000000000000000000000000000000"


@pytest.mark.parametrize(
    ("first", "again", "error"),
    [
        pytest.param(
            lambda req: implement(req, issue_url=ISSUE3),
            _reports_head,
            "field 'issue_url' names https://github.com/owner/repo/issues/3",
            id="another-issue",
        ),
        pytest.param(
            lambda req: (commit_in(req.cwd), block(analyze_payload(SHA_A)))[1],
            _reports_head,
            f"field 'head_sha' is {SHA_A}, but the worktree's HEAD is",
            id="head-is-not-the-worktree-head",
        ),
        pytest.param(
            _attached,
            _detach_and_report,
            "the worktree's HEAD is attached to a local branch",
            id="attached-head",
        ),
        pytest.param(
            _reports_head,
            implement,
            "is the default branch head itself; commit the implementation",
            id="nothing-committed",
        ),
        pytest.param(
            _unrelated_history,
            _from_start,
            "does not descend from the default branch head",
            id="unrelated-history",
        ),
        pytest.param(
            lambda req: implement(req) if commit_in(req.cwd, "Implement it, closes #3") else "",
            _from_start,
            "commit message names an issue other than this run's own #2 with a closing keyword",
            id="commit-closes-another-issue",
        ),
        pytest.param(
            lambda req: implement(req) if commit_in(req.cwd, f"Use {_FAKE_TOKEN}") else "",
            _from_start,
            "commit message contains a credential-shaped string",
            id="commit-carries-a-credential",
        ),
        pytest.param(
            lambda req: implement(req, pr_body="Adds it. Fixes #2"),
            _reports_head,
            "closing keyword",
            id="body-links-the-issue-itself",
        ),
    ],
)
def test_analyze_candidate_refusal_is_corrected_before_anything_is_published(
    tmp_state_dir, fake_github, first, again, error
):
    """The controller reads the candidate from the worktree itself (detached
    HEAD equal to ``head_sha``, a commit over the base, descending from it,
    every published commit message within policy) and the published text;
    a refusal is the agent's to correct, before any push or PR."""
    eng = make_engine(tmp_state_dir, None, github=fake_github, origin=True)

    def corrected(req):
        assert req.correction and error in req.prompt
        assert eng.origin.head("autoforge/2") is None and fake_github.effect_writes == []
        return again(req)

    eng.provider._handler = scripted(first, corrected)
    out = _analyzed(eng)
    assert out.next_phase == "REVIEW", out.message
    assert len(eng.provider.calls) == 2
    head = _worktree_head(eng.provider.calls[1])
    assert eng.origin.head("autoforge/2") == head == eng.state.current_head_sha
    assert [w[0] for w in fake_github.effect_writes] == ["create_pull_request"]


def test_analyze_continues_earlier_work_on_its_branch_as_a_fast_forward(tmp_state_dir, fake_github):
    """A branch with no PR is earlier work for the issue: the prompt starts the
    agent from its head, a candidate that ignores it is corrected (the push
    would not be a fast-forward), and the PR is opened over it."""
    eng = make_engine(tmp_state_dir, None, github=fake_github, origin=True)
    earlier = _earlier_work(eng)
    eng.provider._handler = scripted(implement, _from_start)
    out = _analyzed(eng)
    assert out.next_phase == "REVIEW", out.message
    first, second = eng.provider.calls
    assert (
        f"Start commit: `{earlier}` (the head of `autoforge/2` on GitHub: earlier work for "
        "this issue, with no PR yet)" in first.prompt
    )
    assert f"does not descend from {earlier}, the head of the branch" in second.prompt
    head = _worktree_head(second)
    assert git_out("-C", second.cwd, "rev-parse", "HEAD^") == earlier
    assert eng.origin.head("autoforge/2") == head == eng.state.current_head_sha
    assert fake_github.prs[PR].head_sha == head


def test_analyze_adopts_the_open_unmarked_pr_on_its_branch(tmp_state_dir, fake_github):
    """K3: an open PR on ``autoforge/<n>`` without a marker is the issue's PR.
    The controller pushes over its head, then appends ``Closes #n`` and the
    marker to its body once GitHub shows it at the candidate; no PR is
    created."""
    eng = make_engine(tmp_state_dir, _from_start, github=fake_github, origin=True)
    earlier = _earlier_work(eng)
    fake_github.add_pr(PR41, head_sha=earlier, branch="autoforge/2", body="Started by hand.")
    out = _analyzed(eng)
    assert out.next_phase == "REVIEW", out.message
    (req,) = eng.provider.calls
    assert (
        f"Start commit: `{earlier}` (the head of `autoforge/2` on GitHub, the branch of open "
        f"PR {PR41}, which the controller will adopt)" in req.prompt
    )
    head = _worktree_head(req)
    s = load_state(eng.paths.state_file)
    assert (s.current_pr_url, s.current_head_sha, s.current_branch) == (PR41, head, "autoforge/2")
    assert fake_github.prs[PR41].body == f"Started by hand.\n\n{CLOSING}"
    assert [w[0] for w in fake_github.effect_writes] == ["write_pr_body"]
    assert PR not in fake_github.prs
    assert f"adopted PR {PR41}" in out.message


def test_analyze_adoption_waits_for_github_to_show_the_pushed_head(tmp_state_dir, fake_github):
    """K3 binds the marker to the PR's code, so it is written only once GitHub
    shows the PR at the candidate: a head still at the branch's old value is
    transient, and the next process completes from the persisted plan
    without launching the agent again."""
    eng = make_engine(tmp_state_dir, _from_start, github=fake_github, origin=True)
    earlier = _earlier_work(eng)
    fake_github.add_pr(PR41, head_sha=earlier, branch="autoforge/2", body="Started by hand.")
    fake_github.pr_heads_lag = True
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    with pytest.raises(GitHubUnavailableError, match="has not caught up"):
        eng.step()
    (req,) = eng.provider.calls
    candidate = _worktree_head(req)
    assert eng.origin.head("autoforge/2") == candidate, "the push landed"
    assert fake_github.effect_writes == [] and MARKER not in fake_github.prs[PR41].body
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.ANALYZE_EXECUTE and s.completion_context

    fake_github.pr_heads_lag = False
    eng2 = _resumed(eng, tmp_state_dir, fake_github, "never")
    out = eng2.step()
    assert out.next_phase == "REVIEW", out.message
    assert "completed from the persisted ANALYZE_EXECUTE plan; no agent launched" in out.message
    assert eng2.provider.calls == []
    assert eng2.state.current_pr_url == PR41 and eng2.state.current_head_sha == candidate
    assert [w[0] for w in fake_github.effect_writes] == ["write_pr_body"]


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        pytest.param(
            lambda eng, gh: gh.add_pr(PR41, head_sha=SHA_A, branch="autoforge/2", state="CLOSED"),
            f"branch 'autoforge/2' already has a closed or merged PR: {PR41} (CLOSED)",
            id="closed-pr-on-the-branch",
        ),
        pytest.param(
            lambda eng, gh: gh.add_pr(PR41, head_sha=SHA_A, branch="autoforge/2", state="MERGED"),
            f"branch 'autoforge/2' already has a closed or merged PR: {PR41} (MERGED)",
            id="merged-pr-on-the-branch",
        ),
        pytest.param(
            lambda eng, gh: (
                gh.add_pr(PR41, head_sha=SHA_A, branch="autoforge/2"),
                gh.add_pr(
                    "https://github.com/owner/repo/pull/40", head_sha=SHA_A, branch="autoforge/2"
                ),
            ),
            "2 open PRs are headed at 'autoforge/2'",
            id="two-open-prs-on-the-branch",
        ),
        pytest.param(
            lambda eng, gh: gh.add_pr(
                PR41, head_sha=SHA_A, branch="autoforge/2", base_ref="release"
            ),
            f"open PR {PR41} on 'autoforge/2' targets 'release', not the default branch 'main'",
            id="pr-onto-another-base",
        ),
        pytest.param(
            lambda eng, gh: gh.add_pr(
                PR41, head_sha=SHA_A, branch="autoforge/2", body=implementation_pr_body(ISSUE3)
            ),
            f"open PR {PR41} on 'autoforge/2' carries an implementation marker that is not "
            "issue #2's",
            id="another-issues-marker-on-the-branch",
        ),
        pytest.param(
            lambda eng, gh: setattr(
                gh.add_pr(PR41, head_sha=SHA_A, branch="feature", body=implementation_pr_body()),
                "head_repository",
                "someone/repo",
            ),
            f"open PR {PR41} carries the implementation marker for issue #2 but is headed in "
            "the fork someone/repo",
            id="marker-pr-from-a-fork",
        ),
    ],
)
def test_analyze_entry_blocks_before_launching_on_a_pr_it_would_not_publish_to(
    tmp_state_dir, fake_github, setup, reason
):
    """A closed or merged PR on the controller's branch, two PRs there, a PR
    onto another base or carrying another issue's marker, or this issue's
    marker on a fork's PR: the controller never opens a second PR beside
    them, never chooses, and cannot publish to a fork, so it blocks before
    the agent runs and sends nothing."""
    eng = make_engine(tmp_state_dir, "never", github=fake_github, origin=True)
    setup(eng, fake_github)
    out = _analyzed(eng)
    assert out.next_phase == "BLOCKED"
    assert reason in eng.state.block_reason
    assert eng.provider.calls == []
    assert fake_github.effect_writes == [] and eng.origin.head("autoforge/2") is None


def test_analyze_marker_pr_the_agent_opened_itself_blocks_with_nothing_published(
    tmp_state_dir, fake_github
):
    """A PR carrying the issue's marker that the entry did not record was not
    opened by the controller: the result is not published beside it, and
    the run blocks rather than adopting it silently."""

    def publishes_itself(req):
        sha = commit_in(req.cwd)
        fake_github.add_pr(PR41, head_sha=sha, branch="mine", body=implementation_pr_body())
        return block(analyze_payload(sha))

    eng = make_engine(tmp_state_dir, publishes_itself, github=fake_github, origin=True)
    out = _analyzed(eng)
    assert out.next_phase == "BLOCKED"
    assert f"open PR {PR41} carries the implementation marker for issue" in eng.state.block_reason
    assert "but the controller did not open it" in eng.state.block_reason
    assert fake_github.effect_writes == [] and eng.origin.head("autoforge/2") is None
    assert not eng.state.effect_records and not eng.state.completion_context

    # The operator decides that PR is the implementation: 'unblock' re-enters
    # the phase, whose fresh entry binds it without launching the agent.
    assert eng.unblock("that PR is the issue's implementation").unblocked
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert len(eng.provider.calls) == 1
    assert eng.state.current_pr_url == PR41 and eng.state.current_branch == "mine"


def test_analyze_correction_after_the_agent_opened_a_marker_pr_blocks_instead_of_relaunching(
    tmp_state_dir, fake_github
):
    """PR #89 review F1, under #161: a correction relaunch is a re-entry, so
    the entry's reads run before it. An agent that opened a marker PR itself
    and lost its result block is not relaunched beside that PR, and the PR
    is not adopted silently either: the run blocks naming it."""

    def publishes_and_loses_the_block(req):
        assert not req.correction
        sha = commit_in(req.cwd)
        fake_github.add_pr(PR41, head_sha=sha, branch="mine", body=implementation_pr_body())
        return "no block here\n"

    eng = make_engine(tmp_state_dir, publishes_and_loses_the_block, github=fake_github, origin=True)
    out = _analyzed(eng)
    assert out.next_phase == "BLOCKED"
    assert len(eng.provider.calls) == 1
    assert f"open PR {PR41} carries the implementation marker" in eng.state.block_reason
    assert fake_github.effect_writes == [] and eng.origin.head("autoforge/2") is None


@pytest.mark.parametrize(
    "existing",
    [
        pytest.param(
            {"branch": "feature-x", "body": implementation_pr_body(ISSUE3)},
            id="another-issues-marker-elsewhere",
        ),
        pytest.param(
            {"branch": "autoforge/2-x", "linked": [2], "body": "Closes #2"},
            id="unmarked-pr-linked-to-the-issue",
        ),
    ],
)
def test_analyze_a_pr_that_is_not_the_issues_implementation_is_neither_adopted_nor_blocking(
    tmp_state_dir, fake_github, existing
):
    """Off the issue's branch, only the issue's own marker is provenance: a PR
    marked for another issue, or linked to this one without the marker (a
    human's, another tool's), is not adopted, and the controller opens its
    own PR on ``autoforge/<n>`` beside it."""
    fake_github.add_pr(**existing)
    before = fake_github.prs[PR].body
    eng = make_engine(tmp_state_dir, implement, github=fake_github, origin=True)
    out = _analyzed(eng)
    assert out.next_phase == "REVIEW", out.message
    assert len(eng.provider.calls) == 1
    assert eng.state.current_pr_url not in ("", PR)
    assert eng.state.current_branch == "autoforge/2"
    assert [w[0] for w in fake_github.effect_writes] == ["create_pull_request"]
    assert fake_github.prs[PR].body == before


@pytest.mark.parametrize(
    ("during", "reason"),
    [
        pytest.param(
            lambda gh, sha: gh.add_pr(
                PR41, head_sha=sha, branch="mine", body='<!-- ai-implementation: {"issue": 2} -->'
            ),
            "repair the unreadable marker",
            id="unreadable-marker",
        ),
        pytest.param(
            lambda gh, sha: setattr(gh, "open_pr_listing_incomplete", True),
            "cannot establish whether an open PR already implements issue #2",
            id="listing-incomplete",
        ),
        pytest.param(
            lambda gh, sha: gh.add_pr(PR41, head_sha=sha, branch="autoforge/2", state="CLOSED"),
            "already has a closed or merged PR",
            id="pr-closed-on-the-branch-during-the-run",
        ),
    ],
)
def test_analyze_publication_blocks_when_github_changed_under_the_run(
    tmp_state_dir, fake_github, during, reason
):
    """The precondition read before the plan is saved: GitHub as the entry
    observed it, or nothing is planned, pushed or created."""

    def agent(req):
        sha = commit_in(req.cwd)
        during(fake_github, sha)
        return block(analyze_payload(sha))

    eng = make_engine(tmp_state_dir, agent, github=fake_github, origin=True)
    out = _analyzed(eng)
    assert out.next_phase == "BLOCKED"
    assert reason in eng.state.block_reason
    assert fake_github.effect_writes == [] and eng.origin.head("autoforge/2") is None
    assert not eng.state.effect_records and not eng.state.completion_context


def test_analyze_branch_pushed_by_someone_else_during_the_run_blocks(tmp_state_dir, fake_github):
    """A head of ``autoforge/<n>`` the entry did not record is never pushed
    over, even when it is the candidate itself; the next entry blocks on it
    before launching anything."""

    def pushes_itself(req):
        sha = commit_in(req.cwd)
        eng.origin.publish(req.cwd, "HEAD", "autoforge/2")
        return block(analyze_payload(sha))

    eng = make_engine(tmp_state_dir, pushes_itself, github=fake_github, origin=True)
    out = _analyzed(eng)
    assert out.next_phase == "BLOCKED"
    candidate = _worktree_head(eng.provider.calls[0])
    assert (
        f"branch 'autoforge/2' is at {candidate}, but this phase's entry recorded no such branch"
        in eng.state.block_reason
    )
    assert fake_github.effect_writes == [] and PR not in fake_github.prs


def test_analyze_unmarked_pr_opened_on_the_branch_during_the_run_is_not_adopted(
    tmp_state_dir, fake_github
):
    """R1-F1 (ADR 0004 K3): the controller adopts only the open PR its entry
    recorded on ``autoforge/<n>``. One opened there while the agent ran,
    over the very head the entry recorded, is unexplained: nothing is
    planned, pushed or written. 'unblock' starts a fresh entry, which reads
    the branch again and adopts it."""
    eng = make_engine(tmp_state_dir, None, github=fake_github, origin=True)
    earlier = _earlier_work(eng)

    def opens_a_pr_meanwhile(req):
        reply = _from_start(req)
        fake_github.add_pr(PR41, head_sha=earlier, branch="autoforge/2", body="Started by hand.")
        return reply

    eng.provider._handler = scripted(opens_a_pr_meanwhile, _reports_head)
    out = _analyzed(eng)
    assert out.next_phase == "BLOCKED"
    assert (
        f"the open PR on 'autoforge/2' is {PR41}, but this phase's entry recorded none"
        in eng.state.block_reason
    )
    s = load_state(eng.paths.state_file)
    assert s.entry_observation["prs"] == {"refs/heads/autoforge/2": None}
    assert not s.effect_records and not s.completion_context
    assert fake_github.effect_writes == [] and eng.origin.head("autoforge/2") == earlier
    assert fake_github.prs[PR41].body == "Started by hand."

    assert eng.unblock("that PR is the issue's").unblocked
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    candidate = _worktree_head(eng.provider.calls[1])
    assert eng.state.current_pr_url == PR41 and eng.state.current_head_sha == candidate
    assert fake_github.prs[PR41].body == f"Started by hand.\n\n{CLOSING}"
    assert [w[0] for w in fake_github.effect_writes] == ["write_pr_body"]


def test_analyze_correction_after_an_unmarked_pr_appeared_on_the_branch_blocks_before_relaunching(
    tmp_state_dir, fake_github
):
    """R1-F1 across re-entry: the observation saved before the first launch
    records no PR on the branch, so the correction relaunch's entry, which
    honors it, blocks on a PR opened there meanwhile instead of launching an
    agent whose work would be published to it."""
    eng = make_engine(tmp_state_dir, None, github=fake_github, origin=True)
    earlier = _earlier_work(eng)

    def opens_a_pr_and_loses_the_block(req):
        _from_start(req)
        fake_github.add_pr(PR41, head_sha=earlier, branch="autoforge/2", body="Started by hand.")
        return "no block here\n"

    eng.provider._handler = scripted(opens_a_pr_and_loses_the_block)
    out = _analyzed(eng)
    assert out.next_phase == "BLOCKED"
    assert len(eng.provider.calls) == 1
    assert (
        f"the open PR on 'autoforge/2' is {PR41}, but this phase's entry recorded none"
        in eng.state.block_reason
    )
    assert fake_github.effect_writes == [] and eng.origin.head("autoforge/2") == earlier


def test_analyze_correction_adopts_the_pr_its_entry_observed(tmp_state_dir, fake_github):
    """K3 across re-entry: the PR the first entry recorded on the branch is
    still the one there at the correction relaunch, so the honored
    observation explains it and the corrected result adopts it."""
    eng = make_engine(tmp_state_dir, None, github=fake_github, origin=True)
    earlier = _earlier_work(eng)
    fake_github.add_pr(PR41, head_sha=earlier, branch="autoforge/2", body="Started by hand.")
    eng.provider._handler = scripted(
        lambda req: (_from_start(req), "no block here\n")[1], _reports_head
    )
    out = _analyzed(eng)
    assert out.next_phase == "REVIEW", out.message
    assert len(eng.provider.calls) == 2 and eng.provider.calls[1].correction
    assert eng.state.current_pr_url == PR41
    assert [w[0] for w in fake_github.effect_writes] == ["write_pr_body"]


def test_analyze_unavailable_github_at_publication_relaunches_from_the_agents_commit(
    tmp_state_dir, fake_github, monkeypatch
):
    """An unavailable GitHub before the plan is saved is transient: nothing
    was sent, the step can be resumed, and the relaunched agent continues
    from the commit it left in the worktree; one push, one PR."""
    listing = fake_github.list_open_prs

    def agent(req):
        sha = commit_in(req.cwd)

        def unavailable(repo):
            raise GitHubUnavailableError("gh: HTTP 502")

        monkeypatch.setattr(fake_github, "list_open_prs", unavailable)
        return block(analyze_payload(sha))

    eng = make_engine(tmp_state_dir, agent, github=fake_github, origin=True)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    with pytest.raises(GitHubUnavailableError):
        eng.step()
    first = _worktree_head(eng.provider.calls[0])
    s = load_state(eng.paths.state_file)
    assert (s.phase, s.attempt, s.completion_context) == (Phase.ANALYZE_EXECUTE, 1, {})
    assert fake_github.effect_writes == [] and eng.origin.head("autoforge/2") is None

    monkeypatch.setattr(fake_github, "list_open_prs", listing)
    eng2 = _resumed(eng, tmp_state_dir, fake_github, _reports_head)
    out = eng2.step()
    assert out.next_phase == "REVIEW", out.message
    assert _worktree_head(eng2.provider.calls[0]) == first
    assert eng.origin.head("autoforge/2") == first == eng2.state.current_head_sha
    assert [w[0] for w in fake_github.effect_writes] == ["create_pull_request"]


def test_analyze_crash_after_the_plan_save_completes_from_the_journal(
    tmp_state_dir, fake_github, monkeypatch
):
    """A crash after the plan is saved and before anything is sent: the next
    process pushes and opens the PR from the persisted plan, and never
    launches the agent again."""
    from autoforge.engine import ControllerEngine

    eng = make_engine(tmp_state_dir, implement, github=fake_github, origin=True)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"

    monkeypatch.setattr(ControllerEngine, "_complete_analyze", _crash)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.ANALYZE_EXECUTE and len(s.effect_records) == 2
    assert eng.origin.head("autoforge/2") is None and fake_github.effect_writes == []
    candidate = _worktree_head(eng.provider.calls[0])
    monkeypatch.undo()

    eng2 = _resumed(eng, tmp_state_dir, fake_github, "never")
    out = eng2.step()
    assert out.next_phase == "REVIEW", out.message
    assert eng2.provider.calls == []
    assert eng.origin.head("autoforge/2") == candidate == eng2.state.current_head_sha
    assert fake_github.prs[PR].body == f"Adds the feature.\n\nTested with `pytest`.\n\n{CLOSING}"


@pytest.mark.parametrize(
    ("key", "edit", "rule"),
    [
        ("title", lambda title: "Notify @octocat", "invalid PR title: .*an @-mention"),
        ("body", lambda body: "Closes #3\n" + body, "invalid PR body: .*a closing keyword"),
        (
            "body",
            lambda body: "<!-- ai-implementation -->\n" + body,
            "invalid PR body: .*a controller marker opener",
        ),
        ("title", lambda title: title + "\x1b", r"invalid PR title: .*control character"),
        ("body", lambda body: "a\x0b" + body, r"invalid PR body: .*control character"),
        ("body", lambda body: "x" * 60000 + body, "invalid PR body: .*accepts at most 60000"),
        ("body", lambda body: body.replace(CLOSING, f"Closes #3\n\n{MARKER}"), "closing block"),
    ],
    ids=[
        "title-mention",
        "extra-closing-reference",
        "marker-opener",
        "title-control-character",
        "body-control-character",
        "body-over-its-bound",
        "closing-line-of-another-issue",
    ],
)
def test_analyze_a_journaled_pr_text_the_parser_refuses_is_never_published_on_recovery(
    tmp_state_dir, fake_github, monkeypatch, key, edit, rule
):
    """R2-F1: the plan is completed from the journal with no agent result in
    between, so the K2 title and the agent's part of its body get the ANALYZE
    parser's rules again on load, and its closing block must be exactly the
    controller's. A stored text the result path would have refused fails the
    load before anything is sent: no push, no PR, no relaunch, and the file
    is left as it is for the operator."""
    from autoforge.engine import ControllerEngine

    eng = make_engine(tmp_state_dir, implement, github=fake_github, origin=True)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    monkeypatch.setattr(ControllerEngine, "_complete_analyze", _crash)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    monkeypatch.undo()
    data = json.loads(eng.paths.state_file.read_text(encoding="utf-8"))
    payload = data["effect_records"][1]["payload"]
    assert payload == {
        "title": "Add the feature",
        "body": f"Adds the feature.\n\nTested with `pytest`.\n\n{CLOSING}",
    }
    payload[key] = edit(payload[key])
    eng.paths.state_file.write_text(json.dumps(data), encoding="utf-8")
    before = eng.paths.state_file.read_bytes()
    eng.close()

    eng2 = make_engine(tmp_state_dir, "never", github=fake_github)
    connect_origin(eng2, eng.origin)
    with pytest.raises(StateError, match=rule) as info:
        eng2.load()
    assert "octocat" not in str(info.value)
    assert eng2.provider.calls == []
    assert fake_github.effect_writes == [] and PR not in fake_github.prs
    assert eng.origin.head("autoforge/2") is None
    assert eng.paths.state_file.read_bytes() == before


def test_analyze_pr_create_whose_reply_was_lost_is_read_back_not_resent(tmp_state_dir, fake_github):
    """The PR create lands but its reply is lost: the read-back finds the PR
    by the issue's marker and binds it; nothing is sent twice."""
    fake_github.write_failures.append(
        ("create_pull_request", GitHubUnavailableError("gh: HTTP 502"), True)
    )
    eng = make_engine(tmp_state_dir, implement, github=fake_github, origin=True)
    out = _analyzed(eng)
    assert out.next_phase == "REVIEW", out.message
    assert eng.state.current_pr_url == PR
    assert eng.state.current_head_sha == eng.origin.head("autoforge/2")
    assert [w[0] for w in fake_github.effect_writes] == ["create_pull_request"]


def test_analyze_pr_create_that_never_landed_is_sent_again_by_the_next_process(
    tmp_state_dir, fake_github
):
    """After the push, the PR create fails before reaching GitHub: the step
    stops for 'resume' with the record attempted, and the next process
    reconciles it by the issue's marker first, then opens the PR once,
    without launching the agent."""
    fake_github.write_failures.append(
        ("create_pull_request", GitHubUnavailableError("gh: HTTP 502"), False)
    )
    eng = make_engine(tmp_state_dir, implement, github=fake_github, origin=True)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    with pytest.raises(GitHubUnavailableError, match="'resume' reconciles it"):
        eng.step()
    candidate = _worktree_head(eng.provider.calls[0])
    assert eng.origin.head("autoforge/2") == candidate, "the push landed first"
    assert PR not in fake_github.prs

    eng2 = _resumed(eng, tmp_state_dir, fake_github, "never")
    out = eng2.step()
    assert out.next_phase == "REVIEW", out.message
    assert eng2.provider.calls == []
    assert eng2.state.current_pr_url == PR and eng2.state.current_head_sha == candidate
    assert [w[0] for w in fake_github.effect_writes] == ["create_pull_request"] * 2
    assert [p.url for p in fake_github.prs.values() if MARKER in p.body] == [PR]


def test_analyze_branch_moved_after_the_plan_conflicts_and_unblock_completes_the_plan(
    tmp_state_dir, fake_github, monkeypatch
):
    """The push is checked against the head the plan recorded: a branch
    another push created in the meantime is a conflict, never force-pushed
    over; once the operator deletes it, 'unblock' re-enters the phase and
    the plan completes from the journal without launching the agent."""
    from autoforge.engine import ControllerEngine

    eng = make_engine(tmp_state_dir, implement, github=fake_github, origin=True)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    monkeypatch.setattr(ControllerEngine, "_complete_analyze", _crash)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    monkeypatch.undo()
    candidate = _worktree_head(eng.provider.calls[0])
    other = eng.origin.publish(
        eng.workdir, _side_commit(eng.workdir, eng.origin.head("main")), "autoforge/2"
    )

    eng2 = _resumed(eng, tmp_state_dir, fake_github, "never")
    out = eng2.step()
    assert out.next_phase == "BLOCKED"
    assert "delete the branch, which did not exist when the controller planned the push" in (
        eng2.state.block_reason
    )
    assert eng.origin.head("autoforge/2") == other and fake_github.effect_writes == []

    preview = eng2.unblock("checking", dry_run=True)
    assert preview.message.startswith("[dry-run] would re-enter ANALYZE_EXECUTE: ")
    assert "its push and PR are planned" in preview.message
    assert "completes from its persisted plan without launching the agent" in preview.message

    eng.origin.delete("autoforge/2")
    assert eng2.unblock("deleted the stray branch").unblocked
    out = eng2.step()
    assert out.next_phase == "REVIEW", out.message
    assert eng2.provider.calls == []
    assert eng.origin.head("autoforge/2") == candidate == eng2.state.current_head_sha


def _rename_main_to_trunk(eng, gh) -> None:
    """GitHub renames the default branch: ``trunk`` at ``main``'s head is now the default."""
    eng.origin.publish(eng.workdir, eng.origin.head("main"), "trunk")
    gh.default_branch = "trunk"


def test_analyze_default_branch_renamed_during_the_run_plans_nothing(tmp_state_dir, fake_github):
    """R1-F2 (ADR 0004 K2): the PR is opened onto the default branch the entry
    read, and only while it is still the default. A rename while the agent
    ran blocks before the plan is saved, with nothing pushed or created;
    'unblock' starts a fresh entry, which opens the PR onto the new one."""
    eng = make_engine(tmp_state_dir, None, github=fake_github, origin=True)

    def renames_meanwhile(req):
        reply = implement(req)
        _rename_main_to_trunk(eng, fake_github)
        return reply

    eng.provider._handler = scripted(renames_meanwhile, _reports_head)
    out = _analyzed(eng)
    assert out.next_phase == "BLOCKED"
    assert (
        "the default branch of owner/repo is 'trunk', but this phase's entry read 'main'"
        in eng.state.block_reason
    )
    assert "Nothing was planned, pushed or created" in eng.state.block_reason
    s = load_state(eng.paths.state_file)
    assert not s.effect_records and not s.completion_context
    assert fake_github.effect_writes == [] and eng.origin.head("autoforge/2") is None

    assert eng.unblock("the default branch was renamed").unblocked
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert fake_github.prs[PR].base_ref == "trunk"
    assert [w[0] for w in fake_github.effect_writes] == ["create_pull_request"]


def test_analyze_correction_after_the_default_branch_was_renamed_blocks_before_relaunching(
    tmp_state_dir, fake_github
):
    """R1-F2 across re-entry: the observation records the default branch the
    candidate is checked against, so the correction relaunch's entry blocks
    after a rename instead of relaunching against a base it did not read."""
    eng = make_engine(tmp_state_dir, None, github=fake_github, origin=True)

    def renames_and_loses_the_block(req):
        commit_in(req.cwd)
        _rename_main_to_trunk(eng, fake_github)
        return "no block here\n"

    eng.provider._handler = scripted(renames_and_loses_the_block)
    out = _analyzed(eng)
    assert out.next_phase == "BLOCKED"
    assert len(eng.provider.calls) == 1
    assert (
        "the default branch of owner/repo is 'trunk', but this phase's entry read 'main'"
        in eng.state.block_reason
    )
    assert "Nothing was launched, pushed or created" in eng.state.block_reason
    assert fake_github.effect_writes == [] and eng.origin.head("autoforge/2") is None


@pytest.mark.parametrize("attempted", [False, True], ids=["planned", "create-attempted"])
def test_analyze_default_branch_renamed_after_the_plan_creates_no_pr(
    tmp_state_dir, fake_github, monkeypatch, attempted
):
    """R1-F2 with journal recovery: a PR create still pending in the persisted
    plan is checked against the default branch before anything of the plan
    is sent, the push included, and a rename since the plan blocks with no
    PR created. Once the planned branch is the default again, 'unblock'
    completes the plan from the journal without launching the agent."""
    from autoforge.engine import ControllerEngine

    eng = make_engine(tmp_state_dir, implement, github=fake_github, origin=True)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    if attempted:
        fake_github.write_failures.append(
            ("create_pull_request", GitHubUnavailableError("gh: HTTP 502"), False)
        )
        with pytest.raises(GitHubUnavailableError, match="'resume' reconciles it"):
            eng.step()
    else:
        monkeypatch.setattr(ControllerEngine, "_complete_analyze", _crash)
        with pytest.raises(KeyboardInterrupt):
            eng.step()
        monkeypatch.undo()
    candidate = _worktree_head(eng.provider.calls[0])
    pushed = eng.origin.head("autoforge/2")
    assert pushed == (candidate if attempted else None)
    writes = list(fake_github.effect_writes)
    _rename_main_to_trunk(eng, fake_github)

    eng2 = _resumed(eng, tmp_state_dir, fake_github, "never")
    out = eng2.step()
    assert out.next_phase == "BLOCKED"
    assert "The journaled plan opens the PR onto 'main'" in eng2.state.block_reason
    assert eng2.provider.calls == []
    assert eng.origin.head("autoforge/2") == pushed
    assert fake_github.effect_writes == writes and PR not in fake_github.prs

    fake_github.default_branch = "main"
    assert eng2.unblock("main is the default branch again").unblocked
    out = eng2.step()
    assert out.next_phase == "REVIEW", out.message
    assert eng2.provider.calls == []
    assert eng.origin.head("autoforge/2") == candidate == eng2.state.current_head_sha
    assert fake_github.prs[PR].base_ref == "main"


def test_analyze_adoption_of_a_pr_retargeted_after_the_plan_blocks(tmp_state_dir, fake_github):
    """R1-F2 for K3: the PR to adopt still targets the default branch when its
    body is written, or it is not adopted; nothing is written to it."""
    eng = make_engine(tmp_state_dir, _from_start, github=fake_github, origin=True)
    earlier = _earlier_work(eng)
    fake_github.add_pr(PR41, head_sha=earlier, branch="autoforge/2", body="Started by hand.")
    fake_github.pr_heads_lag = True
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    with pytest.raises(GitHubUnavailableError, match="has not caught up"):
        eng.step()
    fake_github.pr_heads_lag = False
    fake_github.prs[PR41].base_ref = "release"

    eng2 = _resumed(eng, tmp_state_dir, fake_github, "never")
    out = eng2.step()
    assert out.next_phase == "BLOCKED"
    assert f"PR {PR41} targets 'release', not the default branch 'main'" in (
        eng2.state.block_reason
    )
    assert fake_github.effect_writes == [] and MARKER not in fake_github.prs[PR41].body


def test_analyze_dry_run_plans_the_publication_and_sends_nothing(tmp_state_dir, fake_github):
    """Dry-run reads no candidate and performs no fetch, push or PR write: the
    plan names what the controller would read, check and publish, and the
    prompt shows placeholders rather than a guessed base."""
    eng = make_engine(tmp_state_dir, "never", github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    plan = eng.step(dry_run=True).plan
    notes = "\n".join(plan.notes)
    assert "would read the default branch head, the head of 'autoforge/2'" in notes
    assert "would check the agent's reported head_sha against the worktree's detached" in notes
    assert "would save the push of that HEAD to 'autoforge/2'" in notes
    assert "Start commit: `(read from GitHub at execution)`" in plan.prompt_full
    assert eng.provider.calls == [] and fake_github.effect_writes == []
    assert not eng.paths.state_file.exists()


# -- recovery ------------------------------------------------------------------------
def test_recovery_adopts_the_marked_pr_whatever_its_branch_or_linkage(tmp_state_dir, fake_github):
    """PR #89 review F1: the agent created the PR on a branch of its own
    naming, without linking the issue, and was interrupted before the
    controller persisted it. The marker in its body is what identifies it,
    so the entry adopts it instead of launching a second implementation."""
    fake_github.add_pr(head_sha=SHA_B, branch="feature/anything", body=implementation_pr_body())
    eng = make_engine(tmp_state_dir, ["never"], github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    out = eng.step()
    assert out.next_phase == "REVIEW" and "recovered" in out.message
    assert eng.provider.calls == []
    assert eng.state.current_pr_url == PR and eng.state.current_head_sha == SHA_B
    assert eng.state.current_branch == "feature/anything"
    assert ("list_open_prs", "owner/repo") in fake_github.calls


def test_recovery_finds_the_marked_pr_behind_more_than_a_hundred_open_prs(
    tmp_state_dir, fake_github
):
    """Issue #20: the recovery lookup used to inspect the first 100 open PRs
    and treat the rest as absent, so a busy repository re-implemented an
    issue whose PR already existed. The listing is read to the end now;
    the marked PR is found whatever its position, and the agent is not
    launched. (The fake never paginates; the client's cursor walk is
    tested in test_github.py.)"""
    for n in range(100, 350):  # 250 unrelated open PRs, opened before the issue's
        fake_github.add_pr(
            url=f"https://github.com/owner/repo/pull/{n}",
            head_sha=SHA_A,
            branch=f"feature/{n}",
            body=f"unrelated PR {n}",
        )
    fake_github.add_pr(
        url="https://github.com/owner/repo/pull/350", head_sha=SHA_B, body=implementation_pr_body()
    )
    eng = make_engine(tmp_state_dir, ["never"], github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    out = eng.step()
    assert out.next_phase == "REVIEW" and "recovered" in out.message
    assert eng.provider.calls == []
    assert eng.state.current_pr_url == "https://github.com/owner/repo/pull/350"
    assert eng.state.current_head_sha == SHA_B
    assert ("list_open_prs", "owner/repo") in fake_github.calls


def test_recovery_blocks_when_the_open_pr_listing_cannot_be_read_to_its_end(
    tmp_state_dir, fake_github
):
    """ "No PR implements this issue yet" is a claim about every open PR; a
    listing that stopped before its end cannot make it, and an agent
    launched on it would create a second implementation. BLOCKED, nobody
    launched."""
    fake_github.open_pr_listing_incomplete = True
    eng = make_engine(tmp_state_dir, ["never"], github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    out = eng.step()
    assert out.next_phase == "BLOCKED" and eng.provider.calls == []
    reason = load_state(eng.paths.state_file).block_reason
    assert "cannot establish whether an open PR already implements issue #2" in reason
    assert "cannot be read to its end" in reason and "will not launch an agent" in reason


def test_recovery_after_crash_with_persisted_pr(tmp_state_dir, fake_github):
    """PR created + state persisted, crash before transition -> resume recovers
    the controller's own record, marker or not."""
    fake_github.add_pr(head_sha=SHA_A, branch="feature/x")
    eng = make_engine(tmp_state_dir, ["never"], github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    eng.state.current_pr_url = PR
    eng._save()
    eng2 = make_engine(tmp_state_dir, ["never"], github=fake_github)
    eng2.load()
    out = eng2.step()
    assert out.next_phase == "REVIEW" and eng2.provider.calls == []


def test_recovery_lets_an_unavailable_persisted_pr_read_through_as_transient(
    tmp_state_dir, fake_github
):
    """The persisted PR is the controller's own record; a `gh` that cannot be
    reached says nothing about it. The read is retried, never turned into a
    BLOCKED state that a human has to clear."""
    fake_github.add_pr(head_sha=SHA_A, branch="feature/x")
    eng = make_engine(tmp_state_dir, ["never"], github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    eng.state.current_pr_url = PR
    eng._save()
    fake_github.get_pr_error = GitHubUnavailableError("`gh pr view` failed (exit 1): HTTP 502")
    with pytest.raises(GitHubUnavailableError):
        eng.step()
    assert eng.provider.calls == []
    assert load_state(eng.paths.state_file).phase == Phase.ANALYZE_EXECUTE


def test_recovery_ambiguous_blocks(tmp_state_dir, fake_github):
    fake_github.add_pr(
        url=PR, head_sha=SHA_A, branch="autoforge/2-a", body=implementation_pr_body()
    )
    fake_github.add_pr(
        url="https://github.com/owner/repo/pull/43", head_sha=SHA_B, body=implementation_pr_body()
    )
    eng = make_engine(tmp_state_dir, ["never"], github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "2 open PRs carry the ai-implementation marker" in eng.state.block_reason
    assert eng.provider.calls == []


# -- REVIEW ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "round_done,model,effort",
    [
        (0, "openai/gpt-5.6-luna", "high"),
        (1, "openai/gpt-5.6-terra", "high"),
        (4, "openai/gpt-5.6-terra", "high"),
        (5, "openai/gpt-5.6-sol", "medium"),
    ],
)
def test_review_routing_by_round(tmp_state_dir, round_done, model, effort):
    gh = FakeGitHub()
    rnd = round_done + 1
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(rnd, SHA_A, []))], round_done)
    out = eng.step()
    assert out.next_phase == "READY_FOR_MERGE"
    call = eng.provider.calls[0]
    assert call.profile.model == model and call.profile.effort == effort
    assert f"Round {rnd}" in call.prompt or f"round {rnd}" in call.prompt
    assert SHA_A in call.prompt
    assert eng.state.review_round == rnd and eng.state.reviewed_head_sha == SHA_A


def test_review_finding_goes_to_fix(tmp_state_dir):
    gh = FakeGitHub()
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(1, SHA_A, [_finding(1)]))])
    out = eng.step()
    assert out.next_phase == "FIX"
    s = load_state(eng.paths.state_file)
    assert s.open_findings[0]["id"] == "R1-F1" and s.last_review_needs_fix is True
    assert s.last_review_comment_url == controller_review_comment(eng, 1).url


def test_review_invariant_mismatch_rejected(tmp_state_dir):
    gh = FakeGitHub()
    payload = review_payload(1, SHA_A, [_finding(1)])
    payload["needs_fix_round"] = False
    eng = _in_review(tmp_state_dir, gh, [block(payload)])
    eng.config.execution.max_correction_attempts = 0
    with pytest.raises(ControlResultValidationError, match="needs_fix_round"):
        eng.step()
    assert eng.state.review_round == 0  # failed invocation does not consume a round


def test_review_entry_ignores_a_comment_for_another_round(tmp_state_dir):
    """The marker binds a comment to its round. A round-2 comment at the bound
    HEAD (a human's, or one left by another run) is not round 1's: the entry
    launches the reviewer, and the controller posts round 1's own comment."""
    gh = FakeGitHub()
    gh.add_comment(PR, 100, review_comment_body(2, SHA_A, False))
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(1, SHA_A, []))])
    assert eng.step().next_phase == "READY_FOR_MERGE"
    assert len(eng.provider.calls) == 1
    posted = controller_review_comment(eng, 1)
    assert posted.url != comment_url(PR, 100)
    assert load_state(eng.paths.state_file).last_review_comment_url == posted.url


def test_review_to_fix_handoff_is_githubs_url_of_the_posted_comment(tmp_state_dir):
    """#80: the REVIEW -> FIX handoff is derived from the GitHub object the
    controller verified, never from a string an agent wrote. Since #162 that
    object is the comment the controller posted and read back: the URL
    persisted for the fixer and rendered into the FIX prompt is the one
    GitHub reported for it."""
    gh = FakeGitHub()

    def agent(req):
        if req.phase == "REVIEW":
            return block(review_payload(1, gh.prs[PR].head_sha, [_finding(1)]))
        return fixer(fixed("R1-F1"))(req)

    eng = _in_review(tmp_state_dir, gh, agent, origin=True)
    assert eng.step().next_phase == "FIX"
    posted = controller_review_comment(eng, 1)
    s = load_state(eng.paths.state_file)
    assert s.last_review_comment_url == posted.url
    assert s.review_history[-1]["review_comment_url"] == posted.url
    assert eng.step().next_phase == "REVIEW"
    prompt = eng.provider.calls[1].prompt
    assert f"Verified review comment: {posted.url}" in prompt


def test_fix_handoff_survives_a_restart_between_review_and_fix(tmp_state_dir):
    """#80: the verified comment URL is persisted with the accepted round, so a
    controller restarted between an accepted REVIEW and the FIX invocation
    hands the fixer the same comment it would have without the restart, read
    from ``state.json`` rather than re-chosen from the PR conversation."""
    gh = FakeGitHub()

    def reviews(req):
        return block(review_payload(1, gh.prs[PR].head_sha, [_finding(1)]))

    eng = _in_review(tmp_state_dir, gh, reviews, origin=True)
    head = eng.state.current_head_sha
    assert eng.step().next_phase == "FIX"
    persisted = load_state(eng.paths.state_file)
    assert persisted.phase == Phase.FIX
    posted = controller_review_comment(eng, 1).url
    assert persisted.last_review_comment_url == posted

    # A new process: nothing in memory, state loaded from disk.
    eng2 = _resumed(eng, tmp_state_dir, gh, fixer(fixed("R1-F1")))
    assert eng2.state.phase == Phase.FIX
    assert eng2.step().next_phase == "REVIEW"
    prompt = eng2.provider.calls[0].prompt
    assert f"Verified review comment: {posted}" in prompt
    assert f"Read the verified review comment ({posted})" in prompt
    assert "R1-F1" in prompt and head in prompt


def test_fix_handoff_in_a_mixed_pr_conversation_is_the_marked_round_comment(tmp_state_dir):
    """#80: the PR conversation holds a human comment, the previous round's
    (stale) review, another human comment and an unrelated bot comment. None
    of them is round 2's at the bound HEAD, so the entry launches the
    reviewer, the controller posts round 2's comment, and only that comment
    is handed to the fixer."""
    gh = FakeGitHub()
    gh.add_comment(PR, 90, "Human: please also look at the docs.")
    gh.add_comment(PR, 100, review_comment_body(1, SHA_A, True, ["R1-F1"]))
    gh.add_comment(PR, 91, "Human: thanks, pushed a fix.")
    gh.add_comment(PR, 92, "codecov: coverage 98.2% (+0.1%)")

    def agent(req):
        if req.phase == "REVIEW":
            return block(review_payload(2, gh.prs[PR].head_sha, [_finding(2)]))
        return fixer(fixed("R2-F1"))(req)

    eng = _in_review(tmp_state_dir, gh, agent, round_done=1, origin=True)
    eng.state.reviewed_head_sha = SHA_A
    eng.state.last_review_comment_url = comment_url(PR, 100)
    assert eng.step().next_phase == "FIX"
    posted = controller_review_comment(eng, 2).url
    s = load_state(eng.paths.state_file)
    assert s.last_review_comment_url == posted
    assert eng.step().next_phase == "REVIEW"
    prompt = eng.provider.calls[1].prompt
    assert f"Verified review comment: {posted}" in prompt
    for other in (90, 100, 91, 92):
        assert comment_url(PR, other) not in prompt


@pytest.mark.parametrize(
    "payload, field",
    [
        pytest.param(review_payload(1, SHA_B, []), "reviewed_head_sha", id="other-head"),
        pytest.param(review_payload(3, SHA_A, []), "round", id="other-round"),
    ],
)
def test_review_result_for_another_round_or_head_is_corrected_and_never_posted(
    tmp_state_dir, payload, field
):
    """``round`` and ``reviewed_head_sha`` are cross-checks against the round
    and the HEAD the controller bound. A mismatch is the reviewer's error,
    refused before the result is accepted (#162): the reviewer is asked
    again, and a reviewer that repeats it fails the step with nothing posted
    and no round consumed."""
    gh = FakeGitHub()
    eng = _in_review(tmp_state_dir, gh, [block(payload), block(payload)])
    with pytest.raises(
        ControlResultValidationError, match="did not return a valid CONTROL_RESULT after 2"
    ):
        eng.step()
    assert len(eng.provider.calls) == 2 and eng.provider.calls[1].correction
    assert f"field '{field}'" in eng.provider.calls[1].prompt
    assert gh.effect_writes == [] and gh.comments.get(PR, []) == []
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.REVIEW and s.review_round == 0 and s.reviewed_head_sha == ""
    assert s.last_review_comment_url == "" and s.effect_records == []


def test_review_binds_head_fetched_before_review(tmp_state_dir):
    """State says SHA_A but GitHub says SHA_B: the review must target SHA_B."""
    gh = FakeGitHub()
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(1, SHA_B, []))], head=SHA_A)
    gh.set_head(SHA_B)
    assert eng.step().next_phase == "READY_FOR_MERGE"
    assert SHA_B in eng.provider.calls[0].prompt
    assert eng.state.reviewed_head_sha == SHA_B


def test_review_post_agent_journal_refusal_keeps_the_phase_for_resume(tmp_state_dir):
    """#55 in REMOTE mode: the journal append that records the invocation is
    refused after the reviewer returned, in the same window as a timeout, a
    non-zero exit or a verification failure. The phase is left unchanged with
    no round consumed, the attempt the launch was charged as is persisted,
    the invocation's artifacts are published, the oversized journal is neither
    materialised nor carried forward, and the refusal names the outcome it
    interrupted and what `resume` will do. The refusal comes before the
    round's comment is planned, so nothing was posted (#162): a resume in a
    new process re-enters REVIEW, finds no comment for the round, launches
    the reviewer again, and the round ends with the one comment the
    controller posts."""
    from autoforge.runlog import MAX_EVENT_JOURNAL_BYTES

    gh = FakeGitHub()
    eng = _in_review(tmp_state_dir, gh, None)
    journal = Path(eng.paths.logs_dir) / eng.state.run_id / "events.jsonl"

    def reviews_then_enlarges_the_journal(req):
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.touch()
        os.truncate(journal, 2 * MAX_EVENT_JOURNAL_BYTES)
        return block(review_payload(1, SHA_A, [_finding(1)]))

    eng.provider._handler = reviews_then_enlarges_the_journal
    with pytest.raises(
        StateError,
        match=r"corrupted event journal.*larger than.*interrupted attempt 1 of REVIEW after the "
        r"agent had returned with: a CONTROL_RESULT the controller accepted.*"
        r"a comment, a push, a PR.*may exist.*Repair the log directory, then 'resume'.*"
        r"re-enters REVIEW and completes the round from the persisted review comment plan",
    ):
        eng.step()
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.REVIEW and s.review_round == 0 and s.attempt == 1
    assert s.open_findings == [] and s.last_review_comment_url == ""
    assert s.effect_records == [] and gh.effect_writes == [] and gh.comments.get(PR, []) == []
    assert journal.stat().st_size == 2 * MAX_EVENT_JOURNAL_BYTES, "not carried forward"
    steps = sorted(p.name for p in journal.parent.iterdir() if p.is_dir())
    assert steps == ["001-review-1"]
    assert (journal.parent / steps[0] / "control-result.json").exists()
    eng.close()

    # The operator repairs the journal and resumes in a new process.
    os.truncate(journal, 0)
    eng2 = make_engine(tmp_state_dir, [block(review_payload(1, SHA_A, [_finding(1)]))], github=gh)
    eng2.load()
    assert eng2.step().next_phase == "FIX"
    assert len(eng2.provider.calls) == 1
    s = load_state(eng2.paths.state_file)
    assert s.review_round == 1 and s.attempt == 0
    assert s.last_review_comment_url == controller_review_comment(eng2, 1).url
    assert [f["id"] for f in s.open_findings] == ["R1-F1"]
    assert len(gh.comments[PR]) == 1, "the round has exactly one comment"


# -- REVIEW entry: reconciliation with the PR before the reviewer runs (PR #89 F1) ---------
def test_review_entry_blocks_on_a_round_comment_it_did_not_journal(tmp_state_dir):
    """A comment carrying the marker for the upcoming round at the bound HEAD,
    base and merge base already exists (a human's, or one a reviewer of the
    previous contract posted before its result was recorded). The controller
    posts the round's comment itself (#162) and never adopts or duplicates
    one it did not journal: the entry blocks naming it, with nobody launched
    and nothing posted."""
    gh = FakeGitHub()
    gh.add_comment(PR, 100, review_comment_body(1, SHA_A, False))
    eng = _in_review(tmp_state_dir, gh, ["never"])
    out = eng.step()
    assert out.next_phase == "BLOCKED" and eng.provider.calls == []
    s = load_state(eng.paths.state_file)
    assert "that the controller did not post" in s.block_reason
    assert comment_url(PR, 100) in s.block_reason
    assert "never adopts or duplicates one it did not journal (ADR 0004 D9.6)" in s.block_reason
    assert s.review_round == 0 and s.effect_records == []
    assert gh.effect_writes == [] and len(gh.comments[PR]) == 1


def test_review_entry_ignores_a_comment_for_the_round_at_another_head(tmp_state_dir):
    """The marker binds a comment to (round, HEAD). A round-1 comment at SHA_B
    when the round is bound to SHA_A is not this round's comment: the
    reviewer is launched without it, and the controller posts its own."""
    gh = FakeGitHub()
    gh.add_comment(PR, 90, review_comment_body(1, SHA_B, True, ["R1-F1"]))
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(1, SHA_A, []))])
    assert eng.step().next_phase == "READY_FOR_MERGE"
    assert comment_url(PR, 90) not in eng.provider.calls[0].prompt
    assert [w[0] for w in gh.effect_writes] == ["create_pr_comment"]
    assert len(gh.comments[PR]) == 2
    posted = gh.comments[PR][-1]
    assert load_state(eng.paths.state_file).last_review_comment_url == posted.url
    assert f"Reviewed HEAD: `{SHA_A}`" in posted.body


def test_review_entry_blocks_on_two_comments_for_the_round_without_invoking(tmp_state_dir):
    """Two comments claim the same (round, HEAD): the controller cannot know
    which review is the round's and never chooses. BLOCKED, nobody launched,
    and the reason names both so the operator can remove them."""
    gh = FakeGitHub()
    gh.add_comment(PR, 100, review_comment_body(1, SHA_A, True, ["R1-F1"]))
    gh.add_comment(PR, 101, review_comment_body(1, SHA_A, False))
    eng = _in_review(tmp_state_dir, gh, ["never"])
    out = eng.step()
    assert out.next_phase == "BLOCKED" and eng.provider.calls == []
    reason = load_state(eng.paths.state_file).block_reason
    assert "2 comments carry the ai-review-result marker for round 1" in reason
    assert comment_url(PR, 100) in reason and comment_url(PR, 101) in reason
    assert "never chooses between comments it did not post" in reason
    assert gh.effect_writes == []


def test_a_round_comment_posted_during_the_review_blocks_before_the_controller_posts(
    tmp_state_dir,
):
    """The reviewer publishes nothing (#162), but one that still holds a
    credential, or a human, may post a comment carrying the round's marker
    while it runs. The read of the PR's comments after the result was
    accepted finds it (the uniqueness rule is enforced by the controller,
    not trusted to the prompt): the round blocks naming it, nothing is
    planned or posted, and no round is consumed."""
    gh = FakeGitHub()

    def posts_its_own(req):
        gh.add_comment(PR, 101, review_comment_body(1, SHA_A, True, ["R1-F1"]))
        return block(review_payload(1, SHA_A, [_finding(1)]))

    eng = _in_review(tmp_state_dir, gh, posts_its_own)
    assert eng.step().next_phase == "BLOCKED"
    assert len(eng.provider.calls) == 1
    s = load_state(eng.paths.state_file)
    assert s.review_round == 0 and s.open_findings == [] and s.last_review_comment_url == ""
    assert "that the controller did not post" in s.block_reason
    assert comment_url(PR, 101) in s.block_reason
    assert s.effect_records == [] and gh.effect_writes == [] and len(gh.comments[PR]) == 1


def _review_body_with_marker(marker_json: str) -> str:
    return review_comment_body(1, SHA_A, False).split("<!-- ai-review-result")[0] + (
        f"<!-- ai-review-result: {marker_json} -->\n"
    )


_MALFORMED_MARKERS = [
    pytest.param(
        json.dumps({"round": True, "reviewed_head_sha": SHA_A, "needs_fix_round": False}),
        id="bool-round",
    ),
    pytest.param(
        json.dumps({"round": 1.0, "reviewed_head_sha": SHA_A, "needs_fix_round": False}),
        id="float-round",
    ),
    pytest.param(
        json.dumps({"round": "1", "reviewed_head_sha": SHA_A, "needs_fix_round": False}),
        id="string-round",
    ),
    pytest.param(
        json.dumps({"round": 0, "reviewed_head_sha": SHA_A, "needs_fix_round": False}),
        id="zero-round",
    ),
    pytest.param(json.dumps({"round": 1, "reviewed_head_sha": SHA_A}), id="missing-needs-fix"),
    pytest.param(
        json.dumps({"round": 1, "reviewed_head_sha": SHA_A, "needs_fix_round": 0}),
        id="int-needs-fix",
    ),
    pytest.param(
        json.dumps({"round": 1, "reviewed_head_sha": SHA_A[:7], "needs_fix_round": False}),
        id="short-sha",
    ),
    pytest.param(
        json.dumps({"round": 1, "reviewed_head_sha": 1, "needs_fix_round": False}), id="int-sha"
    ),
    pytest.param(json.dumps([1]), id="not-an-object"),
    pytest.param("{not json", id="not-json"),
    pytest.param(
        json.dumps({"round": 1, "reviewed_head_sha": SHA_A, "needs_fix_round": False, "note": "x"}),
        id="extra-key",
    ),
    pytest.param(
        json.dumps(
            {
                "round": 1,
                "reviewed_head_sha": SHA_A,
                "needs_fix_round": True,
                "finding_ids": "R1-F1",
            }
        ),
        id="finding-ids-not-a-list",
    ),
    pytest.param(
        json.dumps(
            {
                "round": 1,
                "reviewed_head_sha": SHA_A,
                "needs_fix_round": True,
                "finding_ids": ["R2-F1"],
            }
        ),
        id="finding-id-of-another-round",
    ),
    pytest.param(
        json.dumps(
            {
                "round": 1,
                "reviewed_head_sha": SHA_A,
                "needs_fix_round": True,
                "finding_ids": ["R1-F1", "R1-F1"],
            }
        ),
        id="finding-id-repeated",
    ),
    pytest.param(
        # Two complete, well-formed markers in one comment: the comment
        # publishes two identities and proves neither.
        json.dumps({"round": 1, "reviewed_head_sha": SHA_A, "needs_fix_round": False})
        + " -->\n<!-- ai-review-result: "
        + json.dumps({"round": 1, "reviewed_head_sha": SHA_A, "needs_fix_round": True}),
        id="two-markers-in-one-comment",
    ),
]


@pytest.mark.parametrize("marker_json", _MALFORMED_MARKERS)
def test_a_malformed_marker_posted_during_the_review_blocks_before_the_controller_posts(
    tmp_state_dir, marker_json
):
    """A comment whose review marker is not the documented shape (`true == 1`
    and `1.0 == 1` in Python must not make it round 1) appears on the PR
    while the reviewer runs (the reviewer publishes nothing since #162, but
    one holding a credential, or a human, may). The read of the PR's comments
    before the controller posts meets a marker it cannot read: "no comment
    claims this round" is not provable, so the round blocks naming the
    comment, nothing is planned or posted, and nothing is consumed."""
    gh = FakeGitHub()

    def reviews(req):
        gh.add_comment(PR, 100, _review_body_with_marker(marker_json))
        return block(review_payload(1, SHA_A, []))

    eng = _in_review(tmp_state_dir, gh, reviews)
    assert eng.step().next_phase == "BLOCKED"
    s = load_state(eng.paths.state_file)
    assert "cannot establish which comment carries the ai-review-result marker" in s.block_reason
    assert comment_url(PR, 100) in s.block_reason and "nothing was posted" in s.block_reason
    assert s.review_round == 0 and s.last_review_comment_url == ""
    assert s.effect_records == [] and gh.effect_writes == [] and len(gh.comments[PR]) == 1


@pytest.mark.parametrize("marker_json", _MALFORMED_MARKERS)
def test_review_entry_blocks_on_a_malformed_marker_without_invoking(tmp_state_dir, marker_json):
    """The entry scan and the read-back consume the same claim model: a
    comment carrying a review marker the controller cannot read is neither
    adopted nor passed over. "No comment claims this round" is not provable
    while one comment's claim is unreadable (it may be an interrupted
    reviewer's post), so the entry blocks and names the comment instead of
    launching a reviewer that would post a second review."""
    gh = FakeGitHub()
    gh.add_comment(PR, 90, _review_body_with_marker(marker_json))
    eng = _in_review(tmp_state_dir, gh, ["never"])
    out = eng.step()
    assert out.next_phase == "BLOCKED" and eng.provider.calls == []
    reason = load_state(eng.paths.state_file).block_reason
    assert "cannot establish which comment carries the ai-review-result marker" in reason
    assert comment_url(PR, 90) in reason and eng.state.review_round == 0


def test_review_head_changes_during_review_re_reviews(tmp_state_dir):
    gh = FakeGitHub()

    def on_call(req):
        gh.set_head(SHA_B)  # someone pushed while the reviewer was working
        return block(review_payload(1, SHA_A, []))

    eng = _in_review(tmp_state_dir, gh, on_call)
    out = eng.step()
    assert out.next_phase == "REVIEW" and "moved" in out.message
    s = eng.state
    assert s.review_round == 1 and s.last_review_result == "stale"
    assert s.current_head_sha == SHA_B and s.reviewed_head_sha == SHA_A
    # A stale clean round carries nothing forward.
    assert s.prior_findings == [] and s.open_findings == []
    assert "carried" not in out.message


def test_review_stale_round_with_findings_carries_them_to_the_next_review(tmp_state_dir):
    """#14 item 2: a round whose HEAD moved while the reviewer worked is
    consumed, but its findings were never resolved by a fixer and are not
    dropped. They leave `open_findings` (no fixer is launched against a
    commit that is no longer the PR) for `prior_findings`, and the next
    round's reviewer is shown them, with the round, HEAD and comment they
    came from, as findings to re-check at the actual HEAD. That completed
    round then clears the carry: its verdict decided about them."""
    gh = FakeGitHub()
    stale_finding = _finding(1)
    stale_finding["required_resolution"] = "rename the helper\nand its test"

    def round_one_goes_stale(req):
        assert "Prior findings to re-check" in req.prompt
        assert "(none)" in req.prompt.split("Prior findings to re-check", 1)[1][:200]
        gh.set_head(SHA_B)  # someone pushed while the reviewer was working
        return block(review_payload(1, SHA_A, [stale_finding]))

    eng = _in_review(tmp_state_dir, gh, round_one_goes_stale)
    out = eng.step()
    assert out.next_phase == "REVIEW" and "moved" in out.message
    assert "its 1 finding(s) are carried to that review to re-check" in out.message
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.REVIEW and s.review_round == 1 and s.last_review_result == "stale"
    assert s.open_findings == [] and s.prior_findings == [stale_finding]
    assert s.reviewed_head_sha == SHA_A and s.current_head_sha == SHA_B
    assert [r["result"] for r in s.review_history] == ["stale"]
    round_one = controller_review_comment(eng, 1).url
    assert s.last_review_comment_url == round_one
    eng.close()

    # The next entry (a resume, so the carry is read back from disk) hands the
    # findings to the reviewer of the actual HEAD as untrusted evidence.
    def round_two_re_raises(req):
        tail = req.prompt.split("Prior findings to re-check", 1)[1]
        assert f"Review round 1 at HEAD `{SHA_A}` ({round_one})" in tail
        assert "no FIX round resolved them" in tail
        assert "- R1-F1 [nit] src/x.py:1 — typo" in tail
        assert "Required resolution: rename the helper\n    and its test" in tail
        return block(review_payload(2, SHA_B, [_finding(2)]))

    eng2 = make_engine(tmp_state_dir, round_two_re_raises, github=gh)
    eng2.load()
    assert eng2.step().next_phase == "FIX"
    s = load_state(eng2.paths.state_file)
    assert s.review_round == 2 and s.last_review_result == "needs_fix"
    assert [f["id"] for f in s.open_findings] == ["R2-F1"] and s.prior_findings == []
    assert [r["result"] for r in s.review_history] == ["stale", "needs_fix"]


def test_review_clean_round_clears_the_carried_findings(tmp_state_dir):
    """A clean round of the actual HEAD decides about the carried findings
    (the reviewer was shown them and raised none): nothing is carried past
    READY_FOR_MERGE."""
    gh = FakeGitHub()
    eng = _in_review(
        tmp_state_dir, gh, [block(review_payload(2, SHA_B, []))], round_done=1, head=SHA_B
    )
    eng.state.reviewed_head_sha = SHA_A
    eng.state.last_review_comment_url = comment_url(PR, 100)
    eng.state.last_review_result = "stale"
    eng.state.prior_findings = [_finding(1)]
    assert eng.step().next_phase == "READY_FOR_MERGE"
    assert "R1-F1" in eng.provider.calls[0].prompt
    s = load_state(eng.paths.state_file)
    assert s.prior_findings == [] and s.open_findings == [] and s.last_review_result == "clean"


def test_review_stale_round_replaces_the_carried_findings_with_its_own(tmp_state_dir):
    """The carry is replaced, never accumulated: the reviewer of a round that
    went stale in turn was shown the earlier findings and re-raised the ones
    that still applied, so its findings (here: none) supersede them."""
    gh = FakeGitHub()

    def round_two_goes_stale_and_clean(req):
        gh.set_head(SHA_C)
        return block(review_payload(2, SHA_B, []))

    eng = _in_review(tmp_state_dir, gh, round_two_goes_stale_and_clean, round_done=1, head=SHA_B)
    eng.state.reviewed_head_sha = SHA_A
    eng.state.last_review_comment_url = comment_url(PR, 100)
    eng.state.last_review_result = "stale"
    eng.state.prior_findings = [_finding(1)]
    out = eng.step()
    assert out.next_phase == "REVIEW" and "moved" in out.message
    s = load_state(eng.paths.state_file)
    assert s.review_round == 2 and s.prior_findings == [] and s.open_findings == []
    assert s.reviewed_head_sha == SHA_B and s.current_head_sha == SHA_C


def test_review_clean_then_ready_for_merge_holds(tmp_state_dir):
    gh = FakeGitHub()
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(1, SHA_A, []))])
    assert eng.step().next_phase == "READY_FOR_MERGE"
    with pytest.raises(VerificationError, match="merge is disabled"):
        eng.step()
    with pytest.raises(VerificationError, match="merge is disabled"):
        eng.step(allow_merge=True)  # config gate still closed
    assert eng.state.phase == Phase.READY_FOR_MERGE
    # run() never loops past READY_FOR_MERGE
    assert eng.run(max_steps=5) == []


# -- FIX ----------------------------------------------------------------------------------
# Since #163 the fixer only commits: the controller creates the follow-up
# issues it asks for (K5), appends the markers of the findings it defers to
# a handed-over issue (K6), and pushes its commit last (K1, the commit point).
ISSUE43 = "https://github.com/owner/repo/issues/43"  # the first issue an origin-backed run creates


def _in_fix(tmp_state_dir, gh: FakeGitHub, script, findings=None, origin: bool = False):
    """A run in FIX after review round 1, bound to the PR's HEAD.

    With ``origin`` that HEAD is a real commit published to the PR branch of
    the engine's origin, which a fixer commits on and the controller pushes
    to; without, it is ``SHA_A`` and no fixer may reach the push.
    """
    eng = make_engine(tmp_state_dir, script, github=gh, origin=origin)
    head = publish_pr_head(eng) if origin else SHA_A
    gh.add_pr(head_sha=head)
    eng.state.phase = Phase.FIX
    eng.state.current_pr_url = PR
    eng.state.current_head_sha = head
    eng.state.reviewed_head_sha = head
    eng.state.review_round = 1
    eng.state.last_review_comment_url = comment_url(PR, 100)
    eng.state.open_findings = findings if findings is not None else [_finding(1)]
    return eng


def _reports_fix(*resolutions: dict):
    """A fixer whose work is already committed: it reports the worktree's HEAD."""

    def handler(req) -> str:
        reviewed = reviewed_head_in(req.prompt)
        return block(fix_payload(reviewed, _worktree_head(req), list(resolutions)))

    return handler


def _power_loss_at(eng, kind: EffectKind, stage: Stage, *, saved: bool) -> None:
    """The process dies the first time a ``kind`` record moves to ``stage``.

    ``saved``: right after that move was persisted; otherwise with it lost
    (for ``observed``, the write it records has landed on GitHub).
    """
    persist = eng._persist_effect
    fired: list[int] = []

    def dying(record):
        if not fired and record.kind is kind and record.stage is stage:
            fired.append(record.position)
            if saved:
                persist(record)
            raise KeyboardInterrupt
        persist(record)

    eng._persist_effect = dying


def _stages(eng) -> list[tuple[str, str]]:
    """Every persisted move of a FIX record, as (kind, stage), in order."""
    moves: list[tuple[str, str]] = []
    persist = eng._persist_effect

    def spy(record):
        moves.append((record.kind.value, record.stage.value))
        persist(record)

    eng._persist_effect = spy
    return moves


def _marked(finding_id: str, text: str = "A deferred problem.") -> str:
    """An issue body carrying ``finding_id``'s follow-up marker for PR."""
    return f"{text}\n\n{render_follow_up_marker(PR, finding_id)}\n"


def test_fix_the_controller_pushes_the_fixers_commit(tmp_state_dir):
    """#163: the fixer only commits. The controller checks the result against
    the worktree's detached HEAD, pushes that commit to the PR branch as a
    fast-forward over the reviewed HEAD, reads the PR head back and binds
    it. The fixer published nothing, and nothing else was written."""
    gh = FakeGitHub()
    eng = _in_fix(tmp_state_dir, gh, fixer(fixed("R1-F1")), origin=True)
    reviewed = eng.state.reviewed_head_sha
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    call = eng.provider.calls[0]
    assert call.profile.model == "fable" and call.profile.effort == "high"
    assert "R1-F1" in call.prompt and comment_url(PR, 100) in call.prompt
    new = _worktree_head(call)
    assert new != reviewed and eng.origin.is_ancestor(reviewed, new)
    assert eng.origin.head(BRANCH) == new == gh.prs[PR].head_sha
    assert f"pushed {new[:12]} over {reviewed[:12]}" in out.message
    assert "0 follow-up issue(s) created, 0 marker append(s)" in out.message
    s = load_state(eng.paths.state_file)
    assert s.current_head_sha == new and s.reviewed_head_sha == reviewed
    assert s.open_findings == [] and s.last_review_result == "fixed"
    assert s.last_fix_resolutions == [
        {
            "finding_id": "R1-F1",
            "resolution": "fixed",
            "rationale": "",
            "follow_up_issue_url": "",
            "commit_sha": "",
        }
    ]
    assert s.effect_records == [] and s.completion_context == {} and s.entry_observation == {}
    assert s.review_round == 1  # next review is round 2
    assert gh.effect_writes == []


def test_fix_prompt_asks_for_a_commit_and_publishes_nothing(tmp_state_dir):
    """The prompt the fixer gets names the reviewed HEAD as its cross-check
    and asks for no push, checkout of the PR, pull, issue write or PR
    comment (#163)."""
    gh = FakeGitHub()
    eng = _in_fix(tmp_state_dir, gh, fixer(fixed("R1-F1")), origin=True)
    assert eng.step().next_phase == "REVIEW"
    prompt = eng.provider.calls[0].prompt
    assert reviewed_head_in(prompt) == load_state(eng.paths.state_file).reviewed_head_sha
    for forbidden in (
        "git push",
        "gh pr checkout",
        "git pull",
        "gh issue create",
        "gh issue edit",
        "gh pr comment",
    ):
        assert forbidden not in prompt, forbidden


def test_fix_post_agent_journal_refusal_keeps_the_phase_for_resume(tmp_state_dir):
    """#55 in REMOTE mode, FIX: the fixer committed, then the journal append
    was refused before the result was applied. Nothing was planned or
    pushed; the phase, the bound HEAD and the open findings are unchanged
    and the launch is persisted as attempt 1. The refusal names the accepted
    result it interrupted and what `resume` does. The resume relaunches the
    fixer (no plan was saved), which reports the commit it already made, and
    the controller pushes it once."""
    from autoforge.runlog import MAX_EVENT_JOURNAL_BYTES

    gh = FakeGitHub()
    fixes = fixer(fixed("R1-F1"))

    def fixes_then_enlarges_the_journal(req):
        out = fixes(req)
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.touch()
        os.truncate(journal, 2 * MAX_EVENT_JOURNAL_BYTES)
        return out

    eng = _in_fix(tmp_state_dir, gh, fixes_then_enlarges_the_journal, origin=True)
    reviewed = eng.state.reviewed_head_sha
    journal = Path(eng.paths.logs_dir) / eng.state.run_id / "events.jsonl"
    with pytest.raises(
        StateError,
        match=r"corrupted event journal.*interrupted attempt 1 of FIX after the agent had "
        r"returned with: a CONTROL_RESULT the controller accepted.*then 'resume': it "
        r"re-enters FIX and completes the follow-up issues, marker appends and push from the "
        r"persisted plan when it was saved",
    ):
        eng.step()
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.FIX and s.review_round == 1 and s.attempt == 1
    assert s.current_head_sha == reviewed and s.reviewed_head_sha == reviewed
    assert [f["id"] for f in s.open_findings] == ["R1-F1"] and s.last_fix_resolutions == []
    assert s.effect_records == [] and s.completion_context == {}
    assert eng.origin.head(BRANCH) == reviewed and gh.effect_writes == []
    assert journal.stat().st_size == 2 * MAX_EVENT_JOURNAL_BYTES, "not carried forward"
    assert sorted(p.name for p in journal.parent.iterdir() if p.is_dir()) == ["001-fix-1"]
    committed = _worktree_head(eng.provider.calls[0])

    os.truncate(journal, 0)
    eng2 = _resumed(eng, tmp_state_dir, gh, _reports_fix(fixed("R1-F1")))
    out = eng2.step()
    assert out.next_phase == "REVIEW", out.message
    assert len(eng2.provider.calls) == 1
    assert eng.origin.head(BRANCH) == committed == gh.prs[PR].head_sha
    s = load_state(eng2.paths.state_file)
    assert s.phase == Phase.REVIEW and s.current_head_sha == committed
    assert s.last_review_result == "fixed" and s.attempt == 0 and s.prior_findings == []


def test_fix_entry_with_head_past_the_reviewed_one_goes_to_review_without_a_fixer(
    tmp_state_dir,
):
    """PR #89 F1, FIX side: the HEAD the findings are bound to is no longer
    the PR HEAD when FIX is entered (an operator push, or a fixer of the
    previous contract that pushed). The general HEAD-binding rule applies:
    the review is stale, the actual HEAD gets reviewed, and no fixer is
    launched against findings of a commit that is no longer the PR. The
    findings are not dropped either (#14 item 2): they are carried to that
    review to re-check, and the reviewer of the actual HEAD is shown them."""
    gh = FakeGitHub()

    def round_two_sees_the_carry(req):
        assert "- R1-F1 [nit] src/x.py:1 — typo" in req.prompt
        assert f"Review round 1 at HEAD `{SHA_A}` ({comment_url(PR, 100)})" in req.prompt
        return block(review_payload(2, SHA_B, []))

    eng = _in_fix(tmp_state_dir, gh, round_two_sees_the_carry)
    gh.set_head(SHA_B)
    out = eng.step()
    assert out.next_phase == "REVIEW" and eng.provider.calls == []
    assert "past the reviewed HEAD" in out.message and "no fixer launched" in out.message
    assert "the 1 finding(s) of round 1 are carried to that review to re-check" in out.message
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.REVIEW and s.review_round == 1
    assert s.current_head_sha == SHA_B and s.reviewed_head_sha == SHA_A
    assert s.open_findings == [] and s.last_fix_resolutions == []
    assert s.prior_findings == [_finding(1)]
    assert s.last_review_result == "stale" and s.attempt == 0
    eng.close()

    eng2 = make_engine(tmp_state_dir, round_two_sees_the_carry, github=gh)
    eng2.load()
    assert eng2.step().next_phase == "READY_FOR_MERGE"
    assert len(eng2.provider.calls) == 1
    assert load_state(eng2.paths.state_file).prior_findings == []


def test_fix_entry_with_the_pr_retargeted_goes_to_review_without_a_fixer(tmp_state_dir):
    """#95: the findings are bound to the base as well as the HEAD. A PR
    retargeted to another base since the review (same commits) proposes a
    diff the findings were not raised on, so the FIX entry treats it exactly
    like a HEAD past the reviewed one: the review is stale, the actual
    revision is reviewed, no fixer is launched, and the findings are carried
    to that review to re-check rather than dropped."""
    gh = FakeGitHub()

    def round_two_sees_the_carry(req):
        assert "- R1-F1 [nit] src/x.py:1 — typo" in req.prompt
        return block(review_payload(2, SHA_A, []))

    eng = _in_fix(tmp_state_dir, gh, round_two_sees_the_carry)
    eng.state.reviewed_base_ref = "main"
    eng.state.current_base_ref = "main"
    eng.state.reviewed_merge_base_sha = MERGE_BASE
    eng.state.last_fix_resolutions = [{"finding_id": "R0-F1", "resolution": "fixed"}]
    gh.prs[PR].base_ref = "release/1.x"
    out = eng.step()
    assert out.next_phase == "REVIEW" and eng.provider.calls == []
    assert "base changed to 'release/1.x' from the reviewed base 'main'" in out.message
    assert "no fixer launched" in out.message
    assert "the 1 finding(s) of round 1 are carried to that review to re-check" in out.message
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.REVIEW and s.review_round == 1
    assert s.current_base_ref == "release/1.x" and s.reviewed_base_ref == "main"
    assert s.current_head_sha == SHA_A and s.reviewed_head_sha == SHA_A
    assert s.open_findings == [] and s.last_fix_resolutions == []
    assert s.prior_findings == [_finding(1)]
    assert s.last_review_result == "stale" and s.attempt == 0
    eng.close()

    # The next review binds the new base; a clean round on it proceeds.
    eng2 = make_engine(tmp_state_dir, round_two_sees_the_carry, github=gh)
    eng2.load()
    assert eng2.step().next_phase == "READY_FOR_MERGE"
    assert len(eng2.provider.calls) == 1
    s = load_state(eng2.paths.state_file)
    assert s.reviewed_base_ref == "release/1.x" and s.prior_findings == []


def test_fix_entry_without_a_reviewed_base_still_launches_the_fixer(tmp_state_dir):
    """#95: a protocol-2 state file loaded in FIX has an empty
    `reviewed_base_ref` (written before the base was bound). There is no
    base to compare, so the fixer is launched whatever the PR's base is;
    the next completed review writes the binding."""
    gh = FakeGitHub()
    eng = _in_fix(tmp_state_dir, gh, fixer(fixed("R1-F1")), origin=True)
    assert eng.state.reviewed_base_ref == ""
    gh.prs[PR].base_ref = "release/1.x"
    out = eng.step()
    assert out.next_phase == "REVIEW" and len(eng.provider.calls) == 1
    s = load_state(eng.paths.state_file)
    assert s.current_head_sha == _worktree_head(eng.provider.calls[0])
    assert s.open_findings == [] and s.last_fix_resolutions[0]["resolution"] == "fixed"
    assert s.reviewed_base_ref == ""


def test_fix_entry_with_an_unreadable_base_is_a_verification_failure(tmp_state_dir):
    """#95: a PR whose base GitHub does not report is not a retargeted PR;
    it is a read the controller cannot decide on, refused the same way the
    review and merge entries refuse it, with the fixer not launched and the
    findings kept open."""
    gh = FakeGitHub()
    eng = _in_fix(tmp_state_dir, gh, ["never"])
    eng.state.reviewed_base_ref = "main"
    eng.state.current_base_ref = "main"
    eng.state.reviewed_merge_base_sha = MERGE_BASE
    gh.prs[PR].base_ref = ""
    with pytest.raises(VerificationError, match="no readable base branch.*bound to base 'main'"):
        eng.step()
    assert eng.provider.calls == []
    s = eng.state
    assert s.phase == Phase.FIX and [f["id"] for f in s.open_findings] == ["R1-F1"]
    assert s.last_review_result != "stale" and s.current_base_ref == "main"


def test_fix_entry_with_the_base_rewritten_goes_to_review_without_a_fixer(tmp_state_dir):
    """#96 at the FIX entry: same commits, same base name, but the base was
    rewritten under its name since the review, so the findings describe a
    diff the PR no longer shows. Treated like a retarget (#95): the review
    is stale, no fixer is launched, the findings are carried."""
    gh = FakeGitHub()

    def round_two_sees_the_carry(req):
        assert "- R1-F1 [nit] src/x.py:1 — typo" in req.prompt
        return block(review_payload(2, SHA_A, []))

    eng = _in_fix(tmp_state_dir, gh, round_two_sees_the_carry)
    eng.state.reviewed_base_ref = "main"
    eng.state.current_base_ref = "main"
    eng.state.reviewed_merge_base_sha = MERGE_BASE
    eng.state.last_fix_resolutions = [{"finding_id": "R0-F1", "resolution": "fixed"}]
    gh.merge_base = MERGE_BASE_B
    out = eng.step()
    assert out.next_phase == "REVIEW" and eng.provider.calls == []
    assert f"PR merge base moved to {MERGE_BASE_B[:12]} from the reviewed merge base" in (
        out.message
    )
    assert "base 'main' was rewritten under its name" in out.message
    assert "no fixer launched" in out.message
    assert "the 1 finding(s) of round 1 are carried to that review to re-check" in out.message
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.REVIEW and s.review_round == 1
    assert s.current_merge_base_sha == MERGE_BASE_B and s.reviewed_merge_base_sha == MERGE_BASE
    assert s.current_head_sha == SHA_A and s.current_base_ref == "main"
    assert s.open_findings == [] and s.last_fix_resolutions == []
    assert s.prior_findings == [_finding(1)]
    assert s.last_review_result == "stale" and s.attempt == 0
    eng.close()

    # The next review binds the new merge base; a clean round on it proceeds.
    eng2 = make_engine(tmp_state_dir, round_two_sees_the_carry, github=gh)
    eng2.load()
    assert eng2.step().next_phase == "READY_FOR_MERGE"
    assert len(eng2.provider.calls) == 1
    s = load_state(eng2.paths.state_file)
    assert s.reviewed_merge_base_sha == MERGE_BASE_B and s.prior_findings == []


def test_fix_entry_without_a_reviewed_merge_base_still_launches_the_fixer(tmp_state_dir):
    """#96: a protocol-3 state file loaded in FIX has an empty
    `reviewed_merge_base_sha`. There is no merge base to compare, so the
    merge base is not read and the fixer is launched; the next completed
    review writes the binding."""
    gh = FakeGitHub()
    eng = _in_fix(tmp_state_dir, gh, fixer(fixed("R1-F1")), origin=True)
    eng.state.reviewed_base_ref = "main"
    eng.state.current_base_ref = "main"
    assert eng.state.reviewed_merge_base_sha == ""
    gh.merge_base = MERGE_BASE_B
    out = eng.step()
    assert out.next_phase == "REVIEW" and len(eng.provider.calls) == 1
    assert not [c for c in gh.calls if c[0] == "get_merge_base_sha"]
    s = load_state(eng.paths.state_file)
    assert s.current_head_sha == _worktree_head(eng.provider.calls[0])
    assert s.reviewed_merge_base_sha == ""


def test_fix_entry_with_the_unchanged_merge_base_launches_the_fixer(tmp_state_dir):
    gh = FakeGitHub()
    eng = _in_fix(tmp_state_dir, gh, fixer(fixed("R1-F1")), origin=True)
    head = eng.state.reviewed_head_sha
    eng.state.reviewed_base_ref = "main"
    eng.state.current_base_ref = "main"
    eng.state.reviewed_merge_base_sha = eng.origin.merge_base("main", head)
    # main advanced; the merge base did not move
    publish_pr_head(eng, message="Unrelated work on main", branch="main")
    out = eng.step()
    assert out.next_phase == "REVIEW" and len(eng.provider.calls) == 1
    assert ("get_merge_base_sha", "owner/repo", "main", head) in gh.calls
    assert load_state(eng.paths.state_file).open_findings == []


def test_fix_entry_binds_the_unchanged_head_and_fetches_it_before_the_launch(tmp_state_dir):
    """HEAD still equals the reviewed HEAD at FIX entry: the controller
    fetches it into the shared object store (the fixer starts from it
    without contacting the remote), then launches the fixer."""
    gh = FakeGitHub()
    seen = []

    def checks_the_object_store(req):
        reviewed = reviewed_head_in(req.prompt)
        seen.append(git_out("-C", req.cwd, "cat-file", "-t", reviewed))
        return fixer(fixed("R1-F1"))(req)

    eng = _in_fix(tmp_state_dir, gh, checks_the_object_store, origin=True)
    assert eng.step().next_phase == "REVIEW"
    assert len(eng.provider.calls) == 1 and seen == ["commit"]
    assert ("get_pr", PR) in gh.calls


def test_fix_entry_fetches_the_reviewed_head_before_the_launch(tmp_state_dir, offline_fetches):
    """Without an origin the fetch is recorded (and does nothing): the
    reviewed HEAD is what the entry asks for, before the launch."""
    gh = FakeGitHub()
    eng = _in_fix(tmp_state_dir, gh, [block(fix_payload(SHA_A, SHA_A, [fixed("R1-F1")]))])
    eng.config.execution.max_correction_attempts = 0
    offline_fetches.clear()
    with pytest.raises(ControlResultValidationError):
        eng.step()
    assert offline_fetches == [[SHA_A]]


@pytest.mark.parametrize(
    ("head_repository", "branch", "reason"),
    [
        pytest.param(
            "",
            BRANCH,
            f"PR {PR} has no readable head repository, so the controller cannot prove its "
            "branch is a branch of owner/repo",
            id="no-head-repository",
        ),
        pytest.param(
            "someone/repo",
            BRANCH,
            f"PR {PR} is headed in someone/repo, not in owner/repo",
            id="fork-head",
        ),
        pytest.param(
            "owner/repo",
            "",
            f"PR {PR} has no readable head branch ('')",
            id="no-head-branch",
        ),
    ],
)
def test_fix_entry_blocks_without_a_branch_of_the_run_repository(
    tmp_state_dir, head_repository, branch, reason
):
    """PR #205 R1-F1: the controller pushes a fix only to a branch it has
    proven is the PR's head in the run's repository. A PR GitHub reports
    with no head repository (its head was deleted, a fork's typically) is
    not one: a same-named branch of the run's repository is not proven to
    be its head. It blocks before the launch like a fork's head or an
    unreadable branch: no fixer, no entry observation, nothing sent."""
    gh = FakeGitHub()
    eng = _in_fix(tmp_state_dir, gh, fixer(fixed("R1-F1")), origin=True)
    gh.prs[PR].head_repository = head_repository
    gh.prs[PR].head_ref = branch
    reviewed = eng.state.reviewed_head_sha
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    s = load_state(eng.paths.state_file)
    assert reason in s.block_reason and "Nothing was launched" in s.block_reason
    assert eng.provider.calls == [] and s.entry_observation == {}
    assert s.effect_records == [] and s.completion_context == {}
    assert gh.effect_writes == [] and eng.origin.head(BRANCH) == reviewed


@pytest.mark.parametrize(
    "resolution", [fixed("R1-F1"), new_follow_up("R1-F1")], ids=["fixed", "new-follow-up"]
)
def test_fix_plan_blocks_when_the_head_repository_is_gone_after_the_launch(
    tmp_state_dir, resolution
):
    """PR #205 R1-F1, at the plan: the head repository the entry proved is
    no longer readable when the fixer returns. The PR branch is not proven
    to be the run repository's, so neither the push nor a follow-up issue
    is planned or sent."""
    gh = FakeGitHub()
    commits = fixer(resolution)

    def commits_then_the_head_repository_goes(req):
        answer = commits(req)
        gh.prs[PR].head_repository = ""
        return answer

    eng = _in_fix(tmp_state_dir, gh, commits_then_the_head_repository_goes, origin=True)
    reviewed = eng.state.reviewed_head_sha
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert len(eng.provider.calls) == 1
    s = load_state(eng.paths.state_file)
    assert f"PR {PR} has no readable head repository" in s.block_reason
    assert "Nothing was created, appended or pushed" in s.block_reason
    assert s.effect_records == [] and s.completion_context == {}
    assert gh.effect_writes == [] and eng.origin.head(BRANCH) == reviewed


def _detached_at_the_reviewed_parent(req) -> str:
    """A fixer that rebuilt its commit on the reviewed HEAD's parent."""
    reviewed = reviewed_head_in(req.prompt)
    git_out("-C", req.cwd, "checkout", "-q", "--detach", f"{reviewed}^")
    head = commit_in(req.cwd, "Fix R1-F1 (#2)")
    return block(fix_payload(reviewed, head, [fixed("R1-F1"), no_change("R1-F2")]))


def _on_a_local_branch(req) -> str:
    out = fixer(fixed("R1-F1"), no_change("R1-F2"))(req)
    git_out("-C", req.cwd, "checkout", "-q", "-b", "my-fix")
    return out


def _names_the_reviewed_commit(req) -> str:
    reviewed = reviewed_head_in(req.prompt)
    return fixer(fixed("R1-F1", commit_sha=reviewed), no_change("R1-F2"))(req)


@pytest.mark.parametrize(
    ("agent", "rule"),
    [
        (
            fixer(fixed("R1-F1"), no_change("R1-F2"), commit=False),
            r"R1-F1 resolved as fixed, but HEAD is still the reviewed HEAD .*: a fixed "
            r"finding needs a commit",
        ),
        (
            fixer(fixed("R1-F1"), no_change("R1-F2"), head_sha=SHA_B),
            rf"field 'head_sha' is {SHA_B}, but the worktree's HEAD is [0-9a-f]{{40}}",
        ),
        (
            fixer(fixed("R1-F1"), no_change("R1-F2"), previous_head_sha=SHA_C),
            rf"field 'previous_head_sha' is {SHA_C}, but the open findings are bound to",
        ),
        (
            fixer(fixed("R1-F1")),
            r"must resolve exactly the open findings \['R1-F1', 'R1-F2'\], one resolution "
            r"each; missing \['R1-F2'\], unknown \[\]",
        ),
        (
            fixer(fixed("R1-F1"), no_change("R1-F2"), fixed("R1-F9")),
            r"missing \[\], unknown \['R1-F9'\]",
        ),
        (_names_the_reviewed_commit, r"is not one of the commits after the reviewed HEAD"),
        (
            fixer(fixed("R1-F1", commit_sha=SHA_C), no_change("R1-F2")),
            rf"the commit_sha {SHA_C} of R1-F1 is not one of the commits",
        ),
        (_detached_at_the_reviewed_parent, r"does not descend from the reviewed HEAD"),
        (_on_a_local_branch, r"the worktree's HEAD is attached to a local branch"),
        (
            fixer(fixed("R1-F1"), no_change("R1-F2"), message="Fix R1-F1, closes #7"),
            r"commit message names an issue other than this run's own #2 with a closing "
            r"keyword",
        ),
        (
            fixer(
                fixed("R1-F1"),
                no_change("R1-F2"),
                message="Fix R1-F1 with ghp_" + "A1b2C3d4" * 5,
            ),
            r"commit message contains a credential-shaped string",
        ),
        (
            fixer(fixed("R1-F1"), deferred_to("R1-F2", "https://github.com/Owner/REPO/issues/2")),
            r"the follow-up of R1-F2 names the current issue https://github.com/owner/repo/"
            r"issues/2, which is never a follow-up",
        ),
        (
            fixer(fixed("R1-F1"), deferred_to("R1-F2", ISSUE3)),
            rf"{ISSUE3} \(the follow-up of R1-F2\) is not a follow-up issue the controller "
            r"handed over for this PR \(listed: none\)",
        ),
        (
            fixer(fixed("R1-F1"), deferred_to("R1-F2", "https://github.com/other/repo/issues/9")),
            r"is not a follow-up issue the controller handed over",
        ),
    ],
    ids=[
        "fixed-without-a-commit",
        "head-not-the-worktree-head",
        "previous-head-not-the-reviewed-head",
        "missing-finding",
        "extra-finding",
        "commit-sha-is-the-reviewed-head",
        "commit-sha-outside-the-candidate",
        "not-a-fast-forward",
        "attached-head",
        "closes-another-issue",
        "credential-in-a-commit-message",
        "follow-up-is-the-current-issue",
        "follow-up-not-handed-over",
        "follow-up-in-another-repo",
    ],
)
def test_fix_result_is_refused_before_any_effect(tmp_state_dir, agent, rule):
    """#163: the result is checked in full before anything is created,
    appended or pushed, and a refusal is a correction (here with no
    correction left): the PR branch stays at the reviewed HEAD, GitHub gets
    no write, no plan is saved, and the findings stay open."""
    gh = FakeGitHub()
    eng = _in_fix(tmp_state_dir, gh, agent, [_finding(1, 1), _finding(1, 2)], origin=True)
    reviewed = eng.state.reviewed_head_sha
    eng.config.execution.max_correction_attempts = 0
    with pytest.raises(ControlResultValidationError, match=rule) as info:
        eng.step()
    assert "ghp_" not in str(info.value)
    assert eng.origin.head(BRANCH) == reviewed == gh.prs[PR].head_sha
    assert gh.effect_writes == []
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.FIX and s.current_head_sha == reviewed
    assert [f["id"] for f in s.open_findings] == ["R1-F1", "R1-F2"]
    assert s.effect_records == [] and s.completion_context == {}
    assert s.last_fix_resolutions == []


@pytest.mark.parametrize(
    "body",
    [
        "",
        follow_up_issue_body("R1-F1", PR41),
        "closed",
        "missing",
    ],
    ids=["no-marker", "another-pr", "closed", "missing"],
)
def test_fix_deferral_to_an_issue_not_handed_over_is_refused(tmp_state_dir, body):
    """An existing issue is a follow-up the fixer may defer to only when the
    entry handed it over: an open issue carrying a marker of this PR. An
    unmarked issue, another PR's follow-up, a closed one or one that does
    not exist is refused before anything is sent, so the controller never
    writes a marker into an issue it did not hand over."""
    gh = FakeGitHub()
    if body == "closed":
        gh.add_issue(ISSUE3, "closed follow-up", state="CLOSED", body=_marked("R1-F5"))
    elif body != "missing":
        gh.add_issue(ISSUE3, "some issue", body=body)
    eng = _in_fix(tmp_state_dir, gh, fixer(deferred_to("R1-F1", ISSUE3), commit=False), origin=True)
    eng.config.execution.max_correction_attempts = 0
    with pytest.raises(
        ControlResultValidationError,
        match=rf"{ISSUE3} \(the follow-up of R1-F1\) is not a follow-up issue the controller "
        r"handed over for this PR \(listed: none\)",
    ):
        eng.step()
    assert gh.effect_writes == []
    assert load_state(eng.paths.state_file).effect_records == []


@pytest.mark.parametrize(
    "resolution",
    [
        fixed("R1-F1"),
        no_change("R1-F1"),
        new_follow_up("R1-F1"),
        deferred_to("R1-F1", "https://github.com/owner/repo/issues/4"),
    ],
    ids=["fixed", "no-change", "new-follow-up", "deferred-elsewhere"],
)
def test_fix_finding_with_its_own_follow_up_resolved_otherwise_is_refused(
    tmp_state_dir, resolution
):
    """The open issue carrying a finding's marker is the durable record of
    its disposition: the fixer must reuse it, and any other resolution is
    refused before any effect, so no second follow-up is created and state
    never records a resolution GitHub does not carry."""
    gh = FakeGitHub()
    gh.add_issue(ISSUE3, "follow-up", body=follow_up_issue_body("R1-F1"))
    gh.add_issue("https://github.com/owner/repo/issues/4", "earlier", body=_marked("R1-F7"))
    eng = _in_fix(tmp_state_dir, gh, fixer(resolution), origin=True)
    reviewed = eng.state.reviewed_head_sha
    eng.config.execution.max_correction_attempts = 0
    with pytest.raises(
        ControlResultValidationError,
        match=rf"open issue {ISSUE3} already is the follow-up of R1-F1; resolve R1-F1 as "
        "follow_up_created with that issue's URL",
    ):
        eng.step()
    assert gh.effect_writes == [] and eng.origin.head(BRANCH) == reviewed
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.FIX and s.last_fix_resolutions == [] and s.effect_records == []
    assert len(gh.issues) == 4  # EPIC, ISSUE, ISSUE3, issues/4: nothing created


def test_fix_malformed_result_is_corrected_and_the_correction_is_pushed(tmp_state_dir):
    """A fixer that committed and lost its block is asked again; the
    correction reports the commit already in the worktree, and the
    controller pushes it once."""
    gh = FakeGitHub()

    def commits_and_loses_the_block(req):
        fix_commit(req, "Fix R1-F1 (#2)")
        return "junk\n"

    eng = _in_fix(
        tmp_state_dir,
        gh,
        scripted(commits_and_loses_the_block, _reports_fix(fixed("R1-F1"))),
        origin=True,
    )
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    first, second = eng.provider.calls
    assert not first.correction and second.correction
    assert "no CONTROL_RESULT block found" in second.prompt
    assert eng.origin.head(BRANCH) == _worktree_head(second) == eng.state.current_head_sha
    assert gh.effect_writes == []


def test_fix_refused_result_is_corrected_before_any_effect(tmp_state_dir):
    """A result the controller refuses (a fixed finding with no commit) is a
    correction like a malformed one: the fixer is asked again with the
    refusal, nothing was sent in between, and the corrected result is
    planned and pushed."""
    gh = FakeGitHub()
    sent = []

    def corrects(req):
        sent.append(list(gh.effect_writes))
        return fixer(fixed("R1-F1"))(req)

    eng = _in_fix(
        tmp_state_dir,
        gh,
        scripted(fixer(fixed("R1-F1"), commit=False), corrects),
        origin=True,
    )
    reviewed = eng.state.reviewed_head_sha
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    first, second = eng.provider.calls
    assert "a fixed finding needs a commit" in second.prompt
    assert sent == [[]]
    assert eng.origin.head(BRANCH) == _worktree_head(second) != reviewed


def test_fix_creates_the_follow_up_issue_the_fixer_asks_for(tmp_state_dir):
    """A deferral to a new issue: the controller creates it with the fixer's
    title, the fixer's body followed by its own reference to the PR and
    finding and the marker, reads it back, and records its URL. Nothing was
    committed, so nothing is pushed."""
    gh = FakeGitHub()
    eng = _in_fix(tmp_state_dir, gh, fixer(new_follow_up("R1-F1"), commit=False), origin=True)
    reviewed = eng.state.reviewed_head_sha
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert "1 follow-up issue(s) created" in out.message
    assert f"nothing to push (HEAD stays {reviewed[:12]})" in out.message
    body = controller_follow_up_body(FOLLOW_UP_TEXT, PR, "R1-F1")
    assert body.startswith(FOLLOW_UP_TEXT) and body.endswith(render_follow_up_marker(PR, "R1-F1"))
    assert gh.effect_writes == [("create_issue", "owner/repo", body)]
    issue = gh.issues[ISSUE43]
    assert issue.title == FOLLOW_UP_TITLE and issue.body == body and issue.is_open
    assert eng.origin.head(BRANCH) == reviewed
    s = load_state(eng.paths.state_file)
    assert s.current_head_sha == reviewed and s.last_review_result == "fixed"
    assert s.last_fix_resolutions == [
        {
            "finding_id": "R1-F1",
            "resolution": "follow_up_created",
            "rationale": "",
            "follow_up_issue_url": ISSUE43,
            "commit_sha": "",
        }
    ]


def test_fix_round_mixing_every_resolution_creates_appends_then_pushes(tmp_state_dir):
    """#163's happy path: a fixed finding (with the commit that fixed it), a
    new follow-up issue, a re-deferral to an earlier round's follow-up and a
    no-change rationale. The records are persisted in plan order (create,
    append, push last), each read back before the next is sent; every
    resolution's URL is the one its source read back; and the next reviewer
    gets the resolutions as data instead of a PR comment."""
    gh = FakeGitHub()
    earlier = _marked("R1-F9")
    gh.add_issue(ISSUE3, "deferred in round 1", body=earlier)
    findings = [_finding(2, n) for n in (1, 2, 3, 4)]

    def agent(req):
        if req.phase == "REVIEW":
            assert "The last FIX round's resolutions" in req.prompt
            assert "The controller verified these against the commits it pushed" in req.prompt
            assert f'"follow_up_issue_url": "{ISSUE43}"' in req.prompt
            assert f'"commit_sha": "{new[0]}"' in req.prompt
            assert NO_CHANGE_RATIONALE in req.prompt
            for fid, url in (("R1-F9", ISSUE3), ("R2-F2", ISSUE43), ("R2-F3", ISSUE3)):
                assert f"- {fid}: {url}" in req.prompt
            return block(review_payload(3, new[0], []))
        assert "- R2-F1: existing issue: (none)" in req.prompt
        assert f"earlier rounds (finding id:\nissue):\n\n- R1-F9: {ISSUE3}\n" in req.prompt
        new.append(fix_commit(req, "Fix R2-F1 (#2)"))
        return block(
            fix_payload(
                reviewed_head_in(req.prompt),
                new[0],
                [
                    fixed("R2-F1", commit_sha=new[0]),
                    new_follow_up("R2-F2"),
                    deferred_to("R2-F3", ISSUE3),
                    no_change("R2-F4"),
                ],
            )
        )

    new: list[str] = []
    eng = _in_fix(tmp_state_dir, gh, agent, findings, origin=True)
    eng.state.review_round = 2
    reviewed = eng.state.reviewed_head_sha
    moves = _stages(eng)
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert moves == [
        ("follow_up_issue", "attempted"),
        ("follow_up_issue", "observed"),
        ("follow_up_append", "attempted"),
        ("follow_up_append", "observed"),
        ("push", "attempted"),
        ("push", "observed"),
    ]
    appended = compose_append(earlier, render_follow_up_marker(PR, "R2-F3"))
    assert gh.effect_writes == [
        ("create_issue", "owner/repo", controller_follow_up_body(FOLLOW_UP_TEXT, PR, "R2-F2")),
        ("write_issue_body", ISSUE3, appended),
    ]
    assert gh.issues[ISSUE3].body == appended
    assert eng.origin.head(BRANCH) == new[0] == gh.prs[PR].head_sha
    assert (
        f"FIX of review round 2: 4 resolution(s), 1 follow-up issue(s) created, 1 marker "
        f"append(s), pushed {new[0][:12]} over {reviewed[:12]}; FIX -> REVIEW (round 3)"
    ) == out.message
    s = load_state(eng.paths.state_file)
    assert s.current_head_sha == new[0] and s.open_findings == []
    assert s.last_fix_resolutions == [
        {
            "finding_id": "R2-F1",
            "resolution": "fixed",
            "rationale": "",
            "follow_up_issue_url": "",
            "commit_sha": new[0],
        },
        {
            "finding_id": "R2-F2",
            "resolution": "follow_up_created",
            "rationale": "",
            "follow_up_issue_url": ISSUE43,
            "commit_sha": "",
        },
        {
            "finding_id": "R2-F3",
            "resolution": "follow_up_created",
            "rationale": "",
            "follow_up_issue_url": ISSUE3,
            "commit_sha": "",
        },
        {
            "finding_id": "R2-F4",
            "resolution": "no_change_with_rationale",
            "rationale": NO_CHANGE_RATIONALE,
            "follow_up_issue_url": "",
            "commit_sha": "",
        },
    ]

    assert eng.step().next_phase == "READY_FOR_MERGE"
    assert [c.phase for c in eng.provider.calls] == ["FIX", "REVIEW"]
    assert not [c for c in gh.calls if c[0] in ("comment_pr", "create_pr_comment")][1:]


def test_fix_entry_hands_an_existing_follow_up_issue_to_the_fixer(tmp_state_dir):
    """A human (or a fixer of the previous contract) created the finding's
    follow-up before this entry. The open issue carrying the (PR, finding)
    marker is found, named in the prompt, and the fixer reuses it: an empty
    plan, so nothing is created, appended or pushed."""
    gh = FakeGitHub()
    gh.add_issue(ISSUE3, "follow-up", body=follow_up_issue_body("R1-F1"))

    def adopts(req):
        assert f"- R1-F1: existing issue: {ISSUE3}" in req.prompt
        assert render_follow_up_marker(PR, "R1-F1") not in req.prompt
        return fixer(deferred_to("R1-F1", ISSUE3), commit=False)(req)

    eng = _in_fix(tmp_state_dir, gh, adopts, origin=True)
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert "0 follow-up issue(s) created, 0 marker append(s), nothing to push" in out.message
    assert len(eng.provider.calls) == 1 and len(gh.issues) == 1 + 2  # EPIC, ISSUE, ISSUE3
    assert gh.effect_writes == []
    s = load_state(eng.paths.state_file)
    assert s.last_fix_resolutions[0]["follow_up_issue_url"] == ISSUE3


def test_fix_reuse_with_an_empty_plan_completes_after_a_crash_without_a_launch(
    tmp_state_dir, monkeypatch
):
    """The same reuse, with the process dying right after the (empty) plan
    is saved: the next process completes FIX from the journal with no
    launch, no write and no second issue."""
    from autoforge.engine import ControllerEngine

    gh = FakeGitHub()
    gh.add_issue(ISSUE3, "follow-up", body=follow_up_issue_body("R1-F1"))
    eng = _in_fix(tmp_state_dir, gh, fixer(deferred_to("R1-F1", ISSUE3), commit=False), origin=True)
    monkeypatch.setattr(ControllerEngine, "_complete_fix", _crash)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    monkeypatch.undo()
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.FIX and s.effect_records == [] and s.completion_context

    eng2 = _resumed(eng, tmp_state_dir, gh, "never")
    out = eng2.step()
    assert out.next_phase == "REVIEW", out.message
    assert "completed from the persisted FIX plan; no fixer launched" in out.message
    assert eng2.provider.calls == [] and gh.effect_writes == [] and len(gh.issues) == 3
    s = load_state(eng2.paths.state_file)
    assert s.last_fix_resolutions[0]["follow_up_issue_url"] == ISSUE3


def test_fix_legacy_reentry_hands_over_the_follow_up_a_previous_fixer_created(tmp_state_dir):
    """D13.6: a run upgraded while a fixer of the previous contract (which
    created follow-up issues itself) was running. Its re-entry has no entry
    observation to honor; the issue it created is handed over as found, the
    relaunched fixer reuses it, and nothing is created twice."""
    gh = FakeGitHub()
    eng = _in_fix(tmp_state_dir, gh, fixer(deferred_to("R1-F1", ISSUE3), commit=False), origin=True)
    eng._save()
    _as_protocol_5(eng, attempt=1)
    gh.add_issue(ISSUE3, "follow-up", body=follow_up_issue_body("R1-F1"))  # the old fixer's
    eng.load()
    assert eng.state.launch_label == "agent_publishes"
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert f"- R1-F1: existing issue: {ISSUE3}" in eng.provider.calls[0].prompt
    assert gh.effect_writes == [] and len(gh.issues) == 3
    s = load_state(eng.paths.state_file)
    assert s.last_fix_resolutions[0]["follow_up_issue_url"] == ISSUE3


def test_fix_entry_ignores_a_closed_issue_carrying_the_marker(tmp_state_dir):
    """A closed follow-up is not the finding's open follow-up: the fixer is
    told none exists, and the new issue it asks for is created."""
    gh = FakeGitHub()
    gh.add_issue(ISSUE3, "closed follow-up", state="CLOSED", body=follow_up_issue_body("R1-F1"))

    def creates(req):
        assert "- R1-F1: existing issue: (none)" in req.prompt and ISSUE3 not in req.prompt
        return fixer(new_follow_up("R1-F1"), commit=False)(req)

    eng = _in_fix(tmp_state_dir, gh, creates, origin=True)
    assert eng.step().next_phase == "REVIEW"
    assert [w[0] for w in gh.effect_writes] == ["create_issue"]
    assert eng.state.last_fix_resolutions[0]["follow_up_issue_url"] == ISSUE43


def test_fix_entry_blocks_on_two_follow_up_issues_for_one_finding_without_invoking(
    tmp_state_dir,
):
    gh = FakeGitHub()
    other = "https://github.com/owner/repo/issues/4"
    gh.add_issue(ISSUE3, "follow-up", body=follow_up_issue_body("R1-F1"))
    gh.add_issue(other, "follow-up again", body=follow_up_issue_body("R1-F1"))
    eng = _in_fix(tmp_state_dir, gh, ["never"])
    out = eng.step()
    assert out.next_phase == "BLOCKED" and eng.provider.calls == []
    reason = load_state(eng.paths.state_file).block_reason
    assert "2 open issues carry the ai-follow-up marker for finding R1-F1" in reason
    assert ISSUE3 in reason and other in reason and "exactly one remains" in reason


def test_fix_entry_blocks_when_the_open_issue_listing_cannot_be_proven_complete(
    tmp_state_dir,
):
    """ "No follow-up issue exists" is a claim about every open issue; a listing
    that may be truncated cannot make it, and a fixer launched on it could
    ask for a second one. BLOCKED, nobody launched, HEAD not bound."""
    gh = FakeGitHub()
    gh.issue_listing_truncated = True
    eng = _in_fix(tmp_state_dir, gh, ["never"])
    out = eng.step()
    assert out.next_phase == "BLOCKED" and eng.provider.calls == []
    reason = load_state(eng.paths.state_file).block_reason
    assert "cannot establish which follow-up issues already exist" in reason
    assert "may be truncated" in reason


# -- earlier rounds' follow-ups reach the reviewer and the fixer (PR #89 review F2, #90) ---
def test_review_entry_hands_the_prs_existing_follow_up_issues_to_the_reviewer(tmp_state_dir):
    """Finding ids are round-scoped, so the reviewer of round 2 is told which
    problems round 1's fixer already deferred, by finding id and issue."""
    gh = FakeGitHub()
    other = "https://github.com/owner/repo/issues/4"
    gh.add_issue(ISSUE3, "deferred", body=follow_up_issue_body("R1-F2"))
    gh.add_issue(other, "deferred too", body=follow_up_issue_body("R1-F1"))
    gh.add_issue(
        "https://github.com/owner/repo/issues/5",
        "closed",
        state="CLOSED",
        body=follow_up_issue_body("R1-F3"),
    )
    gh.add_issue(
        "https://github.com/owner/repo/issues/6",
        "another PR",
        body=follow_up_issue_body("R1-F1", "https://github.com/owner/repo/pull/41"),
    )

    def reviews(req):
        assert (f"from\n  earlier rounds:\n  - R1-F1: {other}\n- R1-F2: {ISSUE3}\n") in req.prompt
        assert "issues/5" not in req.prompt and "issues/6" not in req.prompt
        return block(review_payload(2, SHA_A, []))

    eng = _in_review(tmp_state_dir, gh, reviews, round_done=1)
    assert eng.step().next_phase == "READY_FOR_MERGE"
    assert ("list_open_issues", "owner/repo", True) in gh.calls


def test_review_entry_blocks_when_the_open_issue_listing_cannot_be_proven_complete(
    tmp_state_dir,
):
    gh = FakeGitHub()
    gh.issue_listing_truncated = True
    eng = _in_review(tmp_state_dir, gh, ["never"])
    out = eng.step()
    assert out.next_phase == "BLOCKED" and eng.provider.calls == []
    reason = load_state(eng.paths.state_file).block_reason
    assert "cannot establish which follow-up issues already exist for PR" in reason
    assert "may be truncated" in reason


def test_fix_entry_hands_earlier_rounds_follow_ups_and_appends_the_marker(tmp_state_dir):
    """The open finding's own follow-up slot is empty; the deferral of an
    earlier round is listed separately, so a re-raised problem is recorded
    on the issue that exists instead of in a second one: the controller
    appends the finding's marker to it, and the issue then carries both."""
    gh = FakeGitHub()
    earlier = follow_up_issue_body("R1-F2")
    gh.add_issue(ISSUE3, "deferred in round 1", body=earlier)

    def defers_again(req):
        assert "- R2-F1: existing issue: (none)" in req.prompt
        assert f"earlier rounds (finding id:\nissue):\n\n- R1-F2: {ISSUE3}\n" in req.prompt
        return fixer(deferred_to("R2-F1", ISSUE3), commit=False)(req)

    eng = _in_fix(tmp_state_dir, gh, defers_again, findings=[_finding(2)], origin=True)
    eng.state.review_round = 2
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert "0 follow-up issue(s) created, 1 marker append(s)" in out.message
    appended = compose_append(earlier, render_follow_up_marker(PR, "R2-F1"))
    assert gh.effect_writes == [("write_issue_body", ISSUE3, appended)]
    assert gh.issues[ISSUE3].body == appended
    assert len(gh.issues) == 3  # EPIC, ISSUE, ISSUE3: no second follow-up issue
    assert eng.state.last_fix_resolutions[0]["follow_up_issue_url"] == ISSUE3


def test_fix_two_findings_deferred_to_one_issue_append_once(tmp_state_dir):
    """Two findings deferred to the same handed-over issue are one append
    (one K6 record, one body write) carrying both markers, in result order."""
    gh = FakeGitHub()
    earlier = _marked("R1-F5")
    gh.add_issue(ISSUE3, "deferred in round 1", body=earlier)
    eng = _in_fix(
        tmp_state_dir,
        gh,
        fixer(deferred_to("R2-F2", ISSUE3), deferred_to("R2-F1", ISSUE3), commit=False),
        findings=[_finding(2, 1), _finding(2, 2)],
        origin=True,
    )
    eng.state.review_round = 2
    moves = _stages(eng)
    assert eng.step().next_phase == "REVIEW"
    assert moves == [("follow_up_append", "attempted"), ("follow_up_append", "observed")]
    block_ = "\n".join(render_follow_up_marker(PR, fid) for fid in ("R2-F2", "R2-F1"))
    assert gh.effect_writes == [("write_issue_body", ISSUE3, compose_append(earlier, block_))]
    urls = [r["follow_up_issue_url"] for r in eng.state.last_fix_resolutions]
    assert urls == [ISSUE3, ISSUE3]


def test_fix_deferral_spelling_a_handed_over_issue_otherwise_is_that_issue(tmp_state_dir):
    """PR #205 R4-F1: GitHub owner and repository names are case-insensitive,
    so ``Owner/REPO/issues/3`` is the earlier round's follow-up the entry
    handed over as ``owner/repo/issues/3``. The deferral is accepted with no
    correction, joins the deferral spelling it as read in one append (one K6
    record, one body write, each marker once), and the record and the
    resolutions carry the URL the controller read, never the reported one."""
    gh = FakeGitHub()
    earlier = _marked("R1-F9")
    gh.add_issue(ISSUE3, "deferred in round 1", body=earlier)
    eng = _in_fix(
        tmp_state_dir,
        gh,
        fixer(
            deferred_to("R2-F1", "https://github.com/Owner/REPO/issues/3"),
            deferred_to("R2-F2", ISSUE3),
            commit=False,
        ),
        findings=[_finding(2, 1), _finding(2, 2)],
        origin=True,
    )
    eng.state.review_round = 2
    eng.config.execution.max_correction_attempts = 0
    targets: list[str] = []
    persist = eng._persist_effect

    def spy(record):
        targets.append(record.target["issue_url"])
        persist(record)

    eng._persist_effect = spy
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert len(eng.provider.calls) == 1
    assert "0 follow-up issue(s) created, 1 marker append(s)" in out.message
    block_ = "\n".join(render_follow_up_marker(PR, fid) for fid in ("R2-F1", "R2-F2"))
    appended = compose_append(earlier, block_)
    assert gh.effect_writes == [("write_issue_body", ISSUE3, appended)]
    assert gh.issues[ISSUE3].body == appended
    assert targets == [ISSUE3, ISSUE3]  # attempted, observed
    urls = [r["follow_up_issue_url"] for r in eng.state.last_fix_resolutions]
    assert urls == [ISSUE3, ISSUE3]


@pytest.mark.parametrize(
    "other",
    [
        "https://github.com/Owner/REPO/issues/4",
        "https://github.com/owner/other/issues/3",
    ],
    ids=["another-number", "another-repository"],
)
def test_fix_deferral_to_another_issue_than_the_handed_over_one_is_refused(tmp_state_dir, other):
    """PR #205 R4-F1: identity, not spelling, decides whether a deferral
    names a handed-over issue. Another number, or the same number in another
    repository, is another issue and stays refused before any effect."""
    gh = FakeGitHub()
    gh.add_issue(ISSUE3, "deferred in round 1", body=_marked("R1-F9"))
    gh.add_issue("https://github.com/owner/repo/issues/4", "unmarked")
    eng = _in_fix(
        tmp_state_dir,
        gh,
        fixer(deferred_to("R2-F1", other), commit=False),
        findings=[_finding(2)],
        origin=True,
    )
    eng.state.review_round = 2
    eng.config.execution.max_correction_attempts = 0
    with pytest.raises(
        ControlResultValidationError,
        match=rf"{other} \(the follow-up of R2-F1\) is not a follow-up issue the controller "
        rf"handed over for this PR \(listed: {ISSUE3}\)",
    ):
        eng.step()
    assert gh.effect_writes == []
    assert load_state(eng.paths.state_file).effect_records == []


def test_fix_reuse_beside_an_append_writes_the_other_findings_marker_only(tmp_state_dir):
    """Finding A's own follow-up is ISSUE3 (found at entry); the fixer
    reuses it for A and defers B to it too. One K6 appends B's marker only,
    so each marker appears exactly once, and each resolution names ISSUE3
    from its own source (the entry for A, the append for B)."""
    gh = FakeGitHub()
    own = follow_up_issue_body("R2-F1")
    gh.add_issue(ISSUE3, "follow-up of A", body=own)
    eng = _in_fix(
        tmp_state_dir,
        gh,
        fixer(deferred_to("R2-F1", ISSUE3), deferred_to("R2-F2", ISSUE3), commit=False),
        findings=[_finding(2, 1), _finding(2, 2)],
        origin=True,
    )
    eng.state.review_round = 2
    assert eng.step().next_phase == "REVIEW"
    marker_a, marker_b = (render_follow_up_marker(PR, fid) for fid in ("R2-F1", "R2-F2"))
    assert gh.effect_writes == [("write_issue_body", ISSUE3, compose_append(own, marker_b))]
    body = gh.issues[ISSUE3].body
    assert body.count(marker_a) == 1 and body.count(marker_b) == 1
    urls = [r["follow_up_issue_url"] for r in eng.state.last_fix_resolutions]
    assert urls == [ISSUE3, ISSUE3]


def test_fix_another_issue_marked_for_a_reused_finding_while_the_fixer_ran_blocks(
    tmp_state_dir,
):
    """Someone marks a second issue for a finding whose follow-up the fixer
    reuses: the precondition read finds two, the controller never chooses
    between them, and nothing is created, appended or pushed."""
    gh = FakeGitHub()
    other = "https://github.com/owner/repo/issues/4"
    gh.add_issue(ISSUE3, "follow-up", body=follow_up_issue_body("R1-F1"))
    reuses = fixer(deferred_to("R1-F1", ISSUE3))

    def reuses_while_another_is_marked(req):
        gh.add_issue(other, "rogue", body=follow_up_issue_body("R1-F1"))
        return reuses(req)

    eng = _in_fix(tmp_state_dir, gh, reuses_while_another_is_marked, origin=True)
    reviewed = eng.state.reviewed_head_sha
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    reason = load_state(eng.paths.state_file).block_reason
    assert "2 open issues carry the ai-follow-up marker for finding R1-F1" in reason
    assert "nothing was created, appended or pushed" in reason
    assert gh.effect_writes == [] and eng.origin.head(BRANCH) == reviewed


def test_fix_a_follow_up_the_controller_did_not_journal_blocks_the_plan(tmp_state_dir):
    """The fixer creates a marked follow-up issue itself (the previous
    contract) and resolves the finding as fixed: the entry recorded no
    follow-up for it, so the issue is not the controller's, is never
    adopted, and nothing is created, appended or pushed."""
    gh = FakeGitHub()
    fixes = fixer(fixed("R1-F1"))

    def creates_one_itself(req):
        gh.add_issue(ISSUE3, "follow-up", body=follow_up_issue_body("R1-F1"))
        return fixes(req)

    eng = _in_fix(tmp_state_dir, gh, creates_one_itself, origin=True)
    reviewed = eng.state.reviewed_head_sha
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    reason = load_state(eng.paths.state_file).block_reason
    assert f"the follow-up issue of R1-F1 is {ISSUE3}, but this phase's entry recorded none" in (
        reason
    )
    assert "Nothing was created, appended or pushed" in reason
    assert gh.effect_writes == [] and eng.origin.head(BRANCH) == reviewed


@pytest.mark.parametrize("when", ["before-the-plan", "at-completion"])
@pytest.mark.parametrize("change", ["closed", "joined"])
def test_fix_reused_follow_up_closed_or_joined_blocks_with_no_write(
    tmp_state_dir, monkeypatch, when, change
):
    """A follow-up issue the fixer reused must still be the one open issue
    carrying its finding's marker when the plan is saved and again before
    anything is sent: closed by a human, or joined by a second marked issue,
    it blocks with no write, and nothing is pushed."""
    from autoforge.engine import ControllerEngine

    gh = FakeGitHub()
    gh.add_issue(ISSUE3, "follow-up", body=follow_up_issue_body("R1-F1"))

    def mutate():
        if change == "closed":
            gh.issues[ISSUE3].state = "CLOSED"
        else:
            gh.add_issue("https://github.com/owner/repo/issues/4", "rogue", body=_marked("R1-F1"))

    reuses = fixer(deferred_to("R1-F1", ISSUE3))

    def agent(req):
        out = reuses(req)
        if when == "before-the-plan":
            mutate()
        return out

    eng = _in_fix(tmp_state_dir, gh, agent, origin=True)
    reviewed = eng.state.reviewed_head_sha
    if when == "at-completion":
        monkeypatch.setattr(ControllerEngine, "_complete_fix", _crash)
        with pytest.raises(KeyboardInterrupt):
            eng.step()
        monkeypatch.undo()
        mutate()
        eng = _resumed(eng, tmp_state_dir, gh, "never")
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    reason = load_state(eng.paths.state_file).block_reason
    if when == "at-completion":
        assert "The open issues contradict the FIX plan" in reason
        assert "nothing more was sent" in reason
    else:
        assert "othing was created, appended or pushed" in reason
    assert gh.effect_writes == [] and eng.origin.head(BRANCH) == reviewed
    assert len(eng.provider.calls) == (1 if when == "before-the-plan" else 0)


def test_fix_unavailable_append_target_propagates_with_no_plan_saved(tmp_state_dir):
    """A follow-up issue `gh` cannot reach when the plan reads it is not an
    issue that does not exist: the transient error propagates with nothing
    planned or sent, and 'resume' reads again."""
    gh = FakeGitHub()
    gh.add_issue(ISSUE3, "deferred in round 1", body=_marked("R1-F5"))
    defers = fixer(deferred_to("R1-F1", ISSUE3))

    def defers_then_github_fails(req):
        gh.get_issue_errors[ISSUE3] = GitHubUnavailableError(
            "`gh issue view` failed (exit 1): HTTP 502"
        )
        return defers(req)

    eng = _in_fix(tmp_state_dir, gh, defers_then_github_fails, origin=True)
    reviewed = eng.state.reviewed_head_sha
    with pytest.raises(GitHubUnavailableError, match="HTTP 502"):
        eng.step()
    assert len(eng.provider.calls) == 1
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.FIX and s.effect_records == [] and s.completion_context == {}
    assert gh.effect_writes == [] and eng.origin.head(BRANCH) == reviewed


@pytest.mark.parametrize("target", ["closed", "other-repo"])
def test_fix_append_target_no_longer_open_blocks_before_the_plan(tmp_state_dir, target):
    gh = FakeGitHub()
    gh.add_issue(ISSUE3, "deferred in round 1", body=_marked("R1-F5"))
    defers = fixer(deferred_to("R1-F1", ISSUE3))

    def defers_then_it_moves(req):
        if target == "closed":
            gh.issues[ISSUE3].state = "CLOSED"
        else:
            # Transferred: GitHub answers the old URL with the issue's new one.
            gh.issues[ISSUE3].url = "https://github.com/other/repo/issues/9"
            gh.issues[ISSUE3].repository = "other/repo"
        return defers(req)

    eng = _in_fix(tmp_state_dir, gh, defers_then_it_moves, origin=True)
    reviewed = eng.state.reviewed_head_sha
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    reason = load_state(eng.paths.state_file).block_reason
    assert f"follow-up issue {ISSUE3}, which R1-F1 is deferred to" in reason
    assert gh.effect_writes == [] and eng.origin.head(BRANCH) == reviewed


CREDENTIAL = "ghp_" + "A1b2C3d4" * 4 + "Zz9Y"


def test_fix_append_to_a_body_holding_a_credential_blocks_with_no_plan(tmp_state_dir):
    """The base an append would extend holds a credential-shaped string: the
    marker block is never appended to it (published text is refused, never
    redacted). BLOCKED with no plan, record or context, the credential is in
    no state file, and no body write is issued."""
    gh = FakeGitHub()
    gh.add_issue(ISSUE3, "deferred", body=_marked("R1-F5", f"Token: {CREDENTIAL}"))
    eng = _in_fix(tmp_state_dir, gh, fixer(deferred_to("R1-F1", ISSUE3)), origin=True)
    reviewed = eng.state.reviewed_head_sha
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    s = load_state(eng.paths.state_file)
    assert f"the markers of R1-F1 cannot be appended to {ISSUE3}" in s.block_reason
    assert "credential-shaped string (pattern class:" in s.block_reason
    assert s.effect_records == [] and s.completion_context == {}
    assert CREDENTIAL not in eng.paths.state_file.read_text(encoding="utf-8")
    assert gh.effect_writes == [] and eng.origin.head(BRANCH) == reviewed


@pytest.mark.parametrize("restart", [False, True], ids=["racing", "after-a-crash"])
def test_fix_body_edited_after_the_plan_was_saved_is_rebased(tmp_state_dir, monkeypatch, restart):
    """D5.5: the append's base was read and the plan saved, then a human
    edited the issue body (while the plan ran, or before the next process
    resumed it). The append is rebased onto the body read now, and the
    human's edit is kept."""
    from autoforge.engine import ControllerEngine

    gh = FakeGitHub()
    earlier = _marked("R1-F5")
    gh.add_issue(ISSUE3, "deferred", body=earlier)
    edited = "Edited by a human.\n\n" + earlier
    eng = _in_fix(tmp_state_dir, gh, fixer(deferred_to("R1-F1", ISSUE3)), origin=True)
    complete = ControllerEngine._complete_fix

    def edits_first(self):
        gh.issues[ISSUE3].body = edited
        if restart:
            raise KeyboardInterrupt
        return complete(self)

    monkeypatch.setattr(ControllerEngine, "_complete_fix", edits_first)
    if restart:
        with pytest.raises(KeyboardInterrupt):
            eng.step()
        monkeypatch.undo()
        eng = _resumed(eng, tmp_state_dir, gh, "never")
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    rebased = compose_append(edited, render_follow_up_marker(PR, "R1-F1"))
    assert gh.effect_writes == [("write_issue_body", ISSUE3, rebased)]
    assert gh.issues[ISSUE3].body == rebased
    assert eng.origin.head(BRANCH) == eng.state.current_head_sha


def test_fix_credential_edited_in_before_a_rebase_is_a_conflict_with_no_write(
    tmp_state_dir, monkeypatch
):
    """The plan is saved, then a credential is edited into the issue body
    before the next process rebases the append: the rebased body would
    publish it, so the record is a conflict, no rebase is saved, no body
    write and no push is issued, and the state file never holds it."""
    from autoforge.engine import ControllerEngine

    gh = FakeGitHub()
    earlier = _marked("R1-F5")
    gh.add_issue(ISSUE3, "deferred", body=earlier)
    eng = _in_fix(tmp_state_dir, gh, fixer(deferred_to("R1-F1", ISSUE3)), origin=True)
    reviewed = eng.state.reviewed_head_sha
    monkeypatch.setattr(ControllerEngine, "_complete_fix", _crash)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    monkeypatch.undo()
    planned = load_state(eng.paths.state_file).effect_records[0]
    gh.issues[ISSUE3].body = f"Token: {CREDENTIAL}\n\n" + earlier

    eng2 = _resumed(eng, tmp_state_dir, gh, "never")
    out = eng2.step()
    assert out.next_phase == "BLOCKED", out.message
    s = load_state(eng2.paths.state_file)
    assert "no rebase" in s.block_reason and "credential-shaped string" in s.block_reason
    assert f"put the body of {ISSUE3} back" in s.block_reason
    record = s.effect_records[0]
    assert record["stage"] == "conflict"
    assert record["payload"] == planned["payload"]
    assert record["precondition"] == planned["precondition"]
    assert CREDENTIAL not in eng2.paths.state_file.read_text(encoding="utf-8")
    assert gh.effect_writes == [] and eng.origin.head(BRANCH) == reviewed
    assert eng2.provider.calls == []


@pytest.mark.parametrize(
    ("kind", "stage", "saved"),
    [
        (EffectKind.FOLLOW_UP_ISSUE, Stage.ATTEMPTED, True),
        (EffectKind.FOLLOW_UP_ISSUE, Stage.OBSERVED, False),
        (EffectKind.FOLLOW_UP_ISSUE, Stage.OBSERVED, True),
        (EffectKind.FOLLOW_UP_APPEND, Stage.ATTEMPTED, True),
        (EffectKind.FOLLOW_UP_APPEND, Stage.OBSERVED, False),
        (EffectKind.FOLLOW_UP_APPEND, Stage.OBSERVED, True),
        (EffectKind.PUSH, Stage.ATTEMPTED, True),
        (EffectKind.PUSH, Stage.OBSERVED, False),
    ],
    ids=[
        "create-intent-saved",
        "create-landed-save-lost",
        "create-observed",
        "append-intent-saved",
        "append-landed-save-lost",
        "append-observed",
        "push-not-issued",
        "push-landed-save-lost",
    ],
)
def test_fix_crash_window_resumes_from_the_journal_and_writes_each_effect_once(
    tmp_state_dir, kind, stage, saved
):
    """Every crash window between the plan's save and the phase's: the next
    process completes FIX from the journal without launching the fixer, a
    landed create is found by its marker, a landed append by its body, a
    landed push by the branch head, and each effect is written exactly once.
    Its own journaled push never routes to REVIEW of a 'drifted' HEAD."""
    gh = FakeGitHub()
    earlier = _marked("R1-F5")
    gh.add_issue(ISSUE3, "deferred in round 1", body=earlier)
    eng = _in_fix(
        tmp_state_dir,
        gh,
        fixer(fixed("R1-F1"), new_follow_up("R1-F2"), deferred_to("R1-F3", ISSUE3)),
        [_finding(1, 1), _finding(1, 2), _finding(1, 3)],
        origin=True,
    )
    reviewed = eng.state.reviewed_head_sha
    _power_loss_at(eng, kind, stage, saved=saved)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    candidate = _worktree_head(eng.provider.calls[0])
    pushed = kind is EffectKind.PUSH and stage is Stage.OBSERVED
    assert eng.origin.head(BRANCH) == (candidate if pushed else reviewed)

    eng2 = _resumed(eng, tmp_state_dir, gh, "never")
    out = eng2.step()
    assert out.next_phase == "REVIEW", out.message
    assert "completed from the persisted FIX plan; no fixer launched" in out.message
    assert f"pushed {candidate[:12]} over {reviewed[:12]}" in out.message
    assert eng2.provider.calls == []
    assert [w[0] for w in gh.effect_writes] == ["create_issue", "write_issue_body"]
    assert [i for i in gh.issues if i == ISSUE43] == [ISSUE43] and len(gh.issues) == 4
    assert gh.issues[ISSUE3].body == compose_append(earlier, render_follow_up_marker(PR, "R1-F3"))
    assert eng.origin.head(BRANCH) == candidate == gh.prs[PR].head_sha
    s = load_state(eng2.paths.state_file)
    assert s.current_head_sha == candidate and s.last_review_result == "fixed"
    assert s.prior_findings == [] and s.effect_records == []
    urls = [r["follow_up_issue_url"] for r in s.last_fix_resolutions]
    assert urls == ["", ISSUE43, ISSUE3]


def test_fix_duplicate_invocation_after_the_effects_writes_nothing_again(
    tmp_state_dir, monkeypatch
):
    """Every effect landed and was journaled, then the process died before
    the phase was saved: the next invocation reads them all back and
    completes, with no second issue, append or push."""
    from autoforge.engine import ControllerEngine

    gh = FakeGitHub()
    gh.add_issue(ISSUE3, "deferred in round 1", body=_marked("R1-F5"))
    eng = _in_fix(
        tmp_state_dir,
        gh,
        fixer(fixed("R1-F1"), new_follow_up("R1-F2"), deferred_to("R1-F3", ISSUE3)),
        [_finding(1, 1), _finding(1, 2), _finding(1, 3)],
        origin=True,
    )
    complete = ControllerEngine._complete_fix

    def dies_after(self):
        complete(self)
        raise KeyboardInterrupt

    monkeypatch.setattr(ControllerEngine, "_complete_fix", dies_after)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    monkeypatch.undo()
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.FIX and {r["stage"] for r in s.effect_records} == {"observed"}
    writes = list(gh.effect_writes)
    candidate = eng.origin.head(BRANCH)

    eng2 = _resumed(eng, tmp_state_dir, gh, "never")
    out = eng2.step()
    assert out.next_phase == "REVIEW", out.message
    assert gh.effect_writes == writes and len(gh.issues) == 4
    assert eng.origin.head(BRANCH) == candidate == eng2.state.current_head_sha
    assert eng2.provider.calls == []


def test_fix_created_follow_up_closed_by_a_human_before_the_read_back_blocks(tmp_state_dir):
    """The create landed and its save was lost; a human closed the issue
    before the next process read it back. A closed controller-created
    follow-up is a conflict, never a reason to create a second one; nothing
    is pushed."""
    gh = FakeGitHub()
    eng = _in_fix(
        tmp_state_dir,
        gh,
        fixer(fixed("R1-F1"), new_follow_up("R1-F2")),
        [_finding(1, 1), _finding(1, 2)],
        origin=True,
    )
    reviewed = eng.state.reviewed_head_sha
    _power_loss_at(eng, EffectKind.FOLLOW_UP_ISSUE, Stage.OBSERVED, saved=False)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    gh.issues[ISSUE43].state = "CLOSED"

    eng2 = _resumed(eng, tmp_state_dir, gh, "never")
    out = eng2.step()
    assert out.next_phase == "BLOCKED", out.message
    reason = load_state(eng2.paths.state_file).block_reason
    assert f"{ISSUE43} carries its identity but is not the object the controller intended" in (
        reason
    )
    assert "reopen the follow-up issue the controller created" in reason
    assert [w[0] for w in gh.effect_writes] == ["create_issue"]
    assert eng.origin.head(BRANCH) == reviewed and eng2.provider.calls == []


# -- PR #205 review round 2: a saved FIX plan is held to the entry's rules again --------
ISSUE4 = "https://github.com/owner/repo/issues/4"


def _plan_saved(eng, monkeypatch) -> None:
    """The fixer runs and the plan is saved; the process dies before anything is sent."""
    from autoforge.engine import ControllerEngine

    monkeypatch.setattr(ControllerEngine, "_complete_fix", _crash)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    monkeypatch.undo()
    assert {r["stage"] for r in load_state(eng.paths.state_file).effect_records} == {"intended"}


def _bound_to_main(eng) -> None:
    """Bind the open findings to base ``main`` and the reviewed HEAD's real merge base."""
    eng.state.reviewed_base_ref = "main"
    eng.state.current_base_ref = "main"
    eng.state.reviewed_merge_base_sha = eng.origin.merge_base("main", eng.state.reviewed_head_sha)


def _after_persisting(eng, kind: EffectKind, stage: Stage, then) -> None:
    """Call ``then(record)`` once, right after a ``kind`` record is persisted at ``stage``."""
    persist = eng._persist_effect
    fired: list[int] = []

    def hooked(record):
        persist(record)
        if not fired and record.kind is kind and record.stage is stage:
            fired.append(record.position)
            then(record)

    eng._persist_effect = hooked


@pytest.mark.parametrize(
    ("head_repository", "branch", "reason"),
    [
        pytest.param(
            "",
            BRANCH,
            f"PR {PR} has no readable head repository, so the controller cannot prove its "
            "branch is a branch of owner/repo",
            id="no-head-repository",
        ),
        pytest.param(
            "someone/repo",
            BRANCH,
            f"PR {PR} is headed in someone/repo, not in owner/repo",
            id="fork-head",
        ),
        pytest.param(
            "owner/repo",
            "another-branch",
            f"PR {PR} is headed at refs/heads/another-branch, but the FIX plan was made for "
            f"refs/heads/{BRANCH}",
            id="another-branch",
        ),
    ],
)
def test_fix_saved_plan_blocks_when_its_push_target_is_no_longer_the_pr_head(
    tmp_state_dir, monkeypatch, head_repository, branch, reason
):
    """PR #205 R2-F1: the plan proved the PR headed at a branch of the run's
    repository when it was saved; a later process replaying it proves that
    again before anything is sent. A head repository emptied or moved to a
    fork, or a head branch renamed, after the save blocks with no follow-up
    created and nothing pushed, and the fixer is not launched again."""
    gh = FakeGitHub()
    eng = _in_fix(
        tmp_state_dir,
        gh,
        fixer(fixed("R1-F1"), new_follow_up("R1-F2")),
        [_finding(1, 1), _finding(1, 2)],
        origin=True,
    )
    reviewed = eng.state.reviewed_head_sha
    _plan_saved(eng, monkeypatch)
    gh.prs[PR].head_repository = head_repository
    gh.prs[PR].head_ref = branch

    eng2 = _resumed(eng, tmp_state_dir, gh, "never")
    out = eng2.step()
    assert out.next_phase == "BLOCKED", out.message
    s = load_state(eng2.paths.state_file)
    assert reason in s.block_reason and "nothing more was sent" in s.block_reason
    assert {r["stage"] for r in s.effect_records} == {"intended"}
    assert s.last_review_result != "fixed" and [f["id"] for f in s.open_findings] == [
        "R1-F1",
        "R1-F2",
    ]
    assert gh.effect_writes == [] and eng.origin.head(BRANCH) == reviewed
    assert eng2.provider.calls == []


@pytest.mark.parametrize(
    ("marked", "planned", "issues"),
    [
        pytest.param("R1-F1", "resolved as fixed", 1, id="fixed"),
        pytest.param("R1-F2", "resolved as no_change_with_rationale", 1, id="no-change"),
        pytest.param("R1-F3", "deferred to follow_up_issue effect 0", 1, id="create-not-issued"),
        pytest.param("R1-F1", "", 2, id="duplicate-claimants"),
    ],
)
def test_fix_saved_plan_blocks_on_a_marked_issue_it_did_not_write(
    tmp_state_dir, monkeypatch, marked, planned, issues
):
    """PR #205 R2-F2: every resolution is checked against the open issues, not
    only the deferred ones. A finding resolved as fixed or with a rationale
    has no follow-up the controller wrote, so an open issue carrying its
    marker (or two) contradicts the plan; so does one carrying the marker of
    a follow-up the plan has not created yet. It blocks before anything is
    sent; the controller never adopts or chooses between such issues."""
    gh = FakeGitHub()
    eng = _in_fix(
        tmp_state_dir,
        gh,
        fixer(fixed("R1-F1"), no_change("R1-F2"), new_follow_up("R1-F3")),
        [_finding(1, 1), _finding(1, 2), _finding(1, 3)],
        origin=True,
    )
    reviewed = eng.state.reviewed_head_sha
    _plan_saved(eng, monkeypatch)
    for n in range(issues):
        gh.add_issue(f"https://github.com/owner/repo/issues/{4 + n}", "rogue", body=_marked(marked))

    eng2 = _resumed(eng, tmp_state_dir, gh, "never")
    out = eng2.step()
    assert out.next_phase == "BLOCKED", out.message
    s = load_state(eng2.paths.state_file)
    if planned:
        assert (
            f"open issue {ISSUE4} carries the follow-up marker of {marked}, which the FIX plan "
            f"has {planned}"
        ) in s.block_reason
    else:
        assert "finding R1-F1 of" in s.block_reason and ISSUE4 in s.block_reason
    assert "The open issues contradict the FIX plan" in s.block_reason
    assert "nothing more was sent" in s.block_reason
    assert {r["stage"] for r in s.effect_records} == {"intended"}
    assert s.last_review_result != "fixed" and s.last_fix_resolutions == []
    assert gh.effect_writes == [] and eng.origin.head(BRANCH) == reviewed
    assert eng2.provider.calls == []


@pytest.mark.parametrize(
    ("kind", "change"),
    [
        (kind, change)
        for kind in (EffectKind.FOLLOW_UP_ISSUE, EffectKind.FOLLOW_UP_APPEND)
        for change in ("closed", "unmarked", "duplicated")
    ],
    ids=lambda v: v.value if isinstance(v, EffectKind) else v,
)
def test_fix_replay_revalidates_observed_follow_ups_before_the_push(tmp_state_dir, kind, change):
    """PR #205 R2-F3: a follow-up an earlier process created or appended to,
    and journaled as observed, is read again when the plan is replayed: an
    observed record is settled in the journal, not on GitHub. Closed,
    unmarked, or joined by a second issue carrying the marker since, it is
    no longer the one open issue the plan names, so it blocks before the
    push: nothing is written again, and the branch stays at the reviewed
    HEAD."""
    gh = FakeGitHub()
    earlier = _marked("R1-F5")
    gh.add_issue(ISSUE3, "deferred in round 1", body=earlier)
    eng = _in_fix(
        tmp_state_dir,
        gh,
        fixer(fixed("R1-F1"), new_follow_up("R1-F2"), deferred_to("R1-F3", ISSUE3)),
        [_finding(1, 1), _finding(1, 2), _finding(1, 3)],
        origin=True,
    )
    reviewed = eng.state.reviewed_head_sha
    _power_loss_at(eng, kind, Stage.OBSERVED, saved=True)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    written = [w[0] for w in gh.effect_writes]
    assert written == (
        ["create_issue"]
        if kind is EffectKind.FOLLOW_UP_ISSUE
        else ["create_issue", "write_issue_body"]
    )
    issue, fid = (ISSUE43, "R1-F2") if kind is EffectKind.FOLLOW_UP_ISSUE else (ISSUE3, "R1-F3")
    if change == "closed":
        gh.issues[issue].state = "CLOSED"
    elif change == "unmarked":
        gh.issues[issue].body = "No marker any more."
    else:
        gh.add_issue(ISSUE4, "rogue", body=_marked(fid))

    eng2 = _resumed(eng, tmp_state_dir, gh, "never")
    out = eng2.step()
    assert out.next_phase == "BLOCKED", out.message
    s = load_state(eng2.paths.state_file)
    assert f"finding {fid} of" in s.block_reason or fid in s.block_reason
    assert "The open issues contradict the FIX plan" in s.block_reason
    assert "nothing more was sent" in s.block_reason
    assert [w[0] for w in gh.effect_writes] == written
    assert eng.origin.head(BRANCH) == reviewed and gh.prs[PR].head_sha == reviewed
    assert s.last_review_result != "fixed" and s.effect_records[-1]["stage"] == "intended"
    assert eng2.provider.calls == []


@pytest.mark.parametrize("marked", ["R1-F1", "R1-F2"], ids=["fixed", "duplicated"])
def test_fix_marker_appearing_after_a_follow_up_write_blocks_the_push(tmp_state_dir, marked):
    """PR #205 R2-F2/R2-F3, within one process: the open issues are read
    again after the follow-up writes and before the push, the commit point.
    An issue marked for a finding resolved as fixed, or a second issue
    marked for the finding the controller just deferred, appearing in
    between blocks with the follow-up on GitHub and nothing pushed."""
    gh = FakeGitHub()
    eng = _in_fix(
        tmp_state_dir,
        gh,
        fixer(fixed("R1-F1"), new_follow_up("R1-F2")),
        [_finding(1, 1), _finding(1, 2)],
        origin=True,
    )
    reviewed = eng.state.reviewed_head_sha
    _after_persisting(
        eng,
        EffectKind.FOLLOW_UP_ISSUE,
        Stage.OBSERVED,
        lambda record: gh.add_issue(ISSUE4, "rogue", body=_marked(marked)),
    )
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    s = load_state(eng.paths.state_file)
    assert "The open issues contradict the FIX plan" in s.block_reason
    assert "the follow-ups are on GitHub but the candidate was not pushed" in s.block_reason
    assert [r["stage"] for r in s.effect_records] == ["observed", "intended"]
    assert [w[0] for w in gh.effect_writes] == ["create_issue"]
    assert eng.origin.head(BRANCH) == reviewed and s.last_review_result != "fixed"


def test_fix_marker_for_a_fixed_finding_at_completion_is_not_accepted(tmp_state_dir):
    """PR #205 R2-F2 at completion: an issue marked for a finding resolved as
    fixed, appearing after the push landed, is not accepted. The fix is not
    recorded (the findings stay open, the plan stays saved); it blocks until
    the issue is closed, and 'unblock' then completes from the journal
    without writing anything again."""
    gh = FakeGitHub()
    eng = _in_fix(
        tmp_state_dir,
        gh,
        fixer(fixed("R1-F1"), new_follow_up("R1-F2")),
        [_finding(1, 1), _finding(1, 2)],
        origin=True,
    )
    _after_persisting(
        eng,
        EffectKind.PUSH,
        Stage.OBSERVED,
        lambda record: gh.add_issue(ISSUE4, "rogue", body=_marked("R1-F1")),
    )
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    candidate = _worktree_head(eng.provider.calls[0])
    s = load_state(eng.paths.state_file)
    assert f"open issue {ISSUE4} carries the follow-up marker of R1-F1" in s.block_reason
    assert "the fix is not recorded" in s.block_reason
    assert s.last_review_result != "fixed" and s.last_fix_resolutions == []
    assert [f["id"] for f in s.open_findings] == ["R1-F1", "R1-F2"]
    assert eng.origin.head(BRANCH) == candidate
    writes = list(gh.effect_writes)

    gh.issues[ISSUE4].state = "CLOSED"
    eng2 = _resumed(eng, tmp_state_dir, gh, "never")
    assert eng2.unblock("closed the rogue follow-up").unblocked
    out = eng2.step()
    assert out.next_phase == "REVIEW", out.message
    assert gh.effect_writes == writes and eng2.provider.calls == []
    s = load_state(eng2.paths.state_file)
    assert s.last_review_result == "fixed" and s.current_head_sha == candidate


@pytest.mark.parametrize("drift", ["retargeted", "rewritten"])
def test_fix_saved_plan_with_the_base_moved_goes_to_review_without_sending(
    tmp_state_dir, monkeypatch, drift
):
    """PR #205 R2-F4: the findings are bound to the base and the merge base
    as well as the HEAD (#95, #96), and a saved plan does not bypass that.
    The PR retargeted from main to release/1.x, or main rewritten under its
    name, after the plan was saved: the review is stale, nothing is sent,
    no fix is recorded and the findings are carried to the next review."""
    gh = FakeGitHub()
    eng = _in_fix(
        tmp_state_dir,
        gh,
        fixer(fixed("R1-F1"), new_follow_up("R1-F2")),
        [_finding(1, 1), _finding(1, 2)],
        origin=True,
    )
    _bound_to_main(eng)
    reviewed = eng.state.reviewed_head_sha
    _plan_saved(eng, monkeypatch)
    if drift == "retargeted":
        gh.prs[PR].base_ref = "release/1.x"
    else:
        gh.merge_base = MERGE_BASE_B

    eng2 = _resumed(eng, tmp_state_dir, gh, "never")
    out = eng2.step()
    assert out.next_phase == "REVIEW", out.message
    if drift == "retargeted":
        assert "base changed to 'release/1.x' from the reviewed base 'main'" in out.message
    else:
        assert f"PR merge base moved to {MERGE_BASE_B[:12]}" in out.message
    assert "nothing more was sent" in out.message
    assert "the 2 finding(s) of round 1 are carried to that review to re-check" in out.message
    s = load_state(eng2.paths.state_file)
    assert s.phase == Phase.REVIEW and s.review_round == 1
    assert s.last_review_result == "stale" and s.last_fix_resolutions == []
    assert s.prior_findings == [_finding(1, 1), _finding(1, 2)] and s.open_findings == []
    assert s.effect_records == [] and s.completion_context == {}
    assert s.current_head_sha == reviewed
    assert gh.effect_writes == [] and eng.origin.head(BRANCH) == reviewed
    assert eng2.provider.calls == []


@pytest.mark.parametrize("drift", ["retargeted", "rewritten"])
def test_fix_base_moved_when_the_push_lands_is_not_recorded_as_fixed(tmp_state_dir, drift):
    """PR #205 R2-F4 at completion: the base or merge base moves while the
    plan is sent. The candidate is on the PR, but the findings it resolves
    describe a diff the PR no longer proposes, so the fix is not recorded:
    the actual revision is reviewed with the findings carried."""
    gh = FakeGitHub()
    eng = _in_fix(tmp_state_dir, gh, fixer(fixed("R1-F1")), origin=True)
    _bound_to_main(eng)

    def moves(record):
        if drift == "retargeted":
            gh.prs[PR].base_ref = "release/1.x"
        else:
            gh.merge_base = MERGE_BASE_B

    _after_persisting(eng, EffectKind.PUSH, Stage.OBSERVED, moves)
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert "read back after the FIX plan was sent" in out.message
    candidate = _worktree_head(eng.provider.calls[0])
    s = load_state(eng.paths.state_file)
    assert s.last_review_result == "stale" and s.last_fix_resolutions == []
    assert s.prior_findings == [_finding(1)] and s.open_findings == []
    assert s.current_head_sha == candidate == eng.origin.head(BRANCH)
    assert s.effect_records == []


def test_fix_saved_plan_with_an_unreadable_base_is_a_verification_failure(
    tmp_state_dir, monkeypatch
):
    """PR #205 R2-F4: a base GitHub does not report when the saved plan is
    replayed is a read the controller cannot decide on, not a retarget: it
    is refused with nothing sent, the phase and the plan kept."""
    gh = FakeGitHub()
    eng = _in_fix(tmp_state_dir, gh, fixer(fixed("R1-F1")), origin=True)
    _bound_to_main(eng)
    reviewed = eng.state.reviewed_head_sha
    _plan_saved(eng, monkeypatch)
    gh.prs[PR].base_ref = ""

    eng2 = _resumed(eng, tmp_state_dir, gh, "never")
    with pytest.raises(VerificationError, match="no readable base branch.*bound to base 'main'"):
        eng2.step()
    s = load_state(eng2.paths.state_file)
    assert s.phase == Phase.FIX and [r["stage"] for r in s.effect_records] == ["intended"]
    assert s.last_review_result != "stale" and [f["id"] for f in s.open_findings] == ["R1-F1"]
    assert gh.effect_writes == [] and eng.origin.head(BRANCH) == reviewed
    assert eng2.provider.calls == []


def test_fix_own_push_moving_the_prs_merge_base_still_completes(tmp_state_dir):
    """PR #205 R2-F4: the merge base the findings are bound to is read
    against the reviewed HEAD, not the PR's. The controller's own push (here
    a candidate GitHub reports another merge base for, as when a fix merges
    the base in) is not drift: the base the findings describe has not
    moved, and the fix is recorded."""
    gh = FakeGitHub()
    eng = _in_fix(tmp_state_dir, gh, fixer(fixed("R1-F1")), origin=True)
    _bound_to_main(eng)
    reviewed = eng.state.reviewed_head_sha

    def pins(record):
        gh.merge_bases[("main", str(record.payload["sha"]))] = MERGE_BASE_B

    _after_persisting(eng, EffectKind.PUSH, Stage.OBSERVED, pins)
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    candidate = _worktree_head(eng.provider.calls[0])
    assert ("get_merge_base_sha", "owner/repo", "main", reviewed) in gh.calls
    assert ("get_merge_base_sha", "owner/repo", "main", candidate) not in gh.calls
    s = load_state(eng.paths.state_file)
    assert s.last_review_result == "fixed" and s.current_head_sha == candidate


# -- PR #205 review round 3: the current issue, and the PR around the commit point -------
_SELF_DEFERRALS = {
    # One finding deferred to the current issue, nothing committed: no push.
    "no-push": ("R1-F1", lambda: fixer(deferred_to("R1-F1", ISSUE), commit=False)),
    # One finding fixed beside it: the plan would push.
    "mixed": ("R1-F2", lambda: fixer(fixed("R1-F1"), deferred_to("R1-F2", ISSUE))),
}


@pytest.mark.parametrize("case", sorted(_SELF_DEFERRALS))
def test_fix_entry_blocks_when_the_current_issue_carries_a_findings_marker(tmp_state_dir, case):
    """PR #205 R3-F1: an open issue carrying the (PR, finding) marker is
    normally the finding's follow-up, handed to the fixer to reuse. The
    current issue carrying it is not: it is never a follow-up of its own
    PR's findings, so the entry blocks before the fixer is launched (which
    would otherwise be told to resolve the finding as deferred to it).
    Nothing is created, appended or pushed, the findings stay open, and
    once the marker is removed 'unblock' launches the fixer afresh."""
    marked, agent = _SELF_DEFERRALS[case]
    gh = FakeGitHub()
    eng = _in_fix(tmp_state_dir, gh, agent(), [_finding(1, 1), _finding(1, 2)], origin=True)
    _bound_to_main(eng)
    eng.state.reviewed_pr_url = PR
    eng.state.last_review_result = "needs_fix"
    reviewed = eng.state.reviewed_head_sha
    gh.issues[ISSUE].body = _marked(marked, "The issue being implemented.")
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    s = load_state(eng.paths.state_file)
    assert (
        f"the current issue {ISSUE} is the follow-up of {marked} of PR {PR}, but the current "
        "issue is never a follow-up of its own PR's findings"
    ) in s.block_reason
    assert "Nothing was launched" in s.block_reason
    assert eng.provider.calls == [] and gh.effect_writes == []
    assert eng.origin.head(BRANCH) == reviewed == gh.prs[PR].head_sha
    assert [f["id"] for f in s.open_findings] == ["R1-F1", "R1-F2"]
    assert s.last_fix_resolutions == [] and s.last_review_result != "fixed"
    assert s.effect_records == [] and s.completion_context == {}

    gh.issues[ISSUE].body = "The issue being implemented."
    eng2 = _resumed(eng, tmp_state_dir, gh, fixer(fixed("R1-F1"), no_change("R1-F2")))
    unblocked = eng2.unblock("removed the follow-up marker from the current issue")
    assert unblocked.unblocked and unblocked.phase == "FIX", unblocked.message
    out = eng2.step()
    assert out.next_phase == "REVIEW", out.message
    assert "- R1-F1: existing issue: (none)" in eng2.provider.calls[0].prompt
    s = load_state(eng2.paths.state_file)
    assert s.last_review_result == "fixed" and gh.effect_writes == []
    assert eng.origin.head(BRANCH) == _worktree_head(eng2.provider.calls[0])


def test_fix_deferral_to_the_current_issue_handed_as_an_earlier_follow_up_is_refused(
    tmp_state_dir,
):
    """PR #205 R3-F1: the current issue carrying an earlier round's marker is
    listed among the earlier follow-ups, but a deferral naming it is still
    refused before anything is sent: no marker is ever appended to it."""
    gh = FakeGitHub()
    eng = _in_fix(tmp_state_dir, gh, fixer(deferred_to("R1-F1", ISSUE), commit=False), origin=True)
    gh.issues[ISSUE].body = _marked("R1-F9", "The issue being implemented.")
    eng.config.execution.max_correction_attempts = 0
    with pytest.raises(
        ControlResultValidationError,
        match=rf"the follow-up of R1-F1 names the current issue {ISSUE}, which is never a "
        "follow-up",
    ):
        eng.step()
    assert f"- R1-F9: {ISSUE}" in eng.provider.calls[0].prompt
    assert gh.effect_writes == []
    s = load_state(eng.paths.state_file)
    assert s.effect_records == [] and s.completion_context == {}
    assert [f["id"] for f in s.open_findings] == ["R1-F1"]


@pytest.mark.parametrize("case", ["no-push", "mixed"])
def test_fix_replayed_plan_deferring_to_the_current_issue_blocks_with_nothing_sent(
    tmp_state_dir, monkeypatch, case
):
    """PR #205 R3-F1 on replay: a saved plan that reuses the current issue as
    a finding's follow-up (one saved without the entry's check; written into
    the journal here) is refused before anything is sent, even though the
    open issues agree with it: the current issue is never a follow-up. The
    finding is not recorded as deferred, nothing is created or pushed, the
    findings stay open and the fixer is not launched again."""
    from autoforge.engine import ControllerEngine

    gh = FakeGitHub()
    gh.add_issue(ISSUE3, "follow-up", body=_marked("R1-F1"))
    if case == "no-push":
        agent = fixer(deferred_to("R1-F1", ISSUE3), commit=False)
        findings = [_finding(1, 1)]
    else:
        agent = fixer(deferred_to("R1-F1", ISSUE3), fixed("R1-F2"))
        findings = [_finding(1, 1), _finding(1, 2)]
    eng = _in_fix(tmp_state_dir, gh, agent, findings, origin=True)
    reviewed = eng.state.reviewed_head_sha
    monkeypatch.setattr(ControllerEngine, "_complete_fix", _crash)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    monkeypatch.undo()
    marker = render_follow_up_marker(PR, "R1-F1")
    assert eng.state.entry_observation["objects"][marker] == ISSUE3
    eng.state.entry_observation["objects"][marker] = ISSUE
    eng._save()
    gh.issues[ISSUE3].body = "No marker any more."
    gh.issues[ISSUE].body = _marked("R1-F1", "The issue being implemented.")

    eng2 = _resumed(eng, tmp_state_dir, gh, "never")
    out = eng2.step()
    assert out.next_phase == "BLOCKED", out.message
    s = load_state(eng2.paths.state_file)
    assert f"the current issue {ISSUE} is the follow-up of R1-F1 of PR {PR}" in s.block_reason
    assert "cannot be completed as saved: nothing more was sent" in s.block_reason
    assert [r["stage"] for r in s.effect_records] == (["intended"] if case == "mixed" else [])
    assert s.last_review_result != "fixed" and s.last_fix_resolutions == []
    assert [f["id"] for f in s.open_findings] == [f["id"] for f in findings]
    assert gh.effect_writes == [] and eng.origin.head(BRANCH) == reviewed
    assert gh.prs[PR].head_sha == reviewed and eng2.provider.calls == []


@pytest.mark.parametrize(
    ("kind", "change"),
    [
        (kind, change)
        for kind in (EffectKind.FOLLOW_UP_ISSUE, EffectKind.FOLLOW_UP_APPEND)
        for change in ("closed", "no-head-repository", "retargeted")
    ],
    ids=lambda v: v.value if isinstance(v, EffectKind) else v,
)
def test_fix_pr_changed_after_a_follow_up_write_is_read_before_the_push(
    tmp_state_dir, kind, change
):
    """PR #205 R3-F2: the PR the plan was checked against before it sent
    anything may change while the follow-ups are written, so it is read
    again, under the same rules, before the push (the commit point). A PR
    closed meanwhile is refused (VerificationError, for 'resume'); one no
    longer headed at a branch of the run's repository blocks; one
    retargeted is drift (the findings carried to REVIEW). In each case the
    follow-ups stay on GitHub and the candidate is never pushed."""
    gh = FakeGitHub()
    gh.add_issue(ISSUE3, "deferred in round 1", body=_marked("R1-F5"))
    eng = _in_fix(
        tmp_state_dir,
        gh,
        fixer(fixed("R1-F1"), new_follow_up("R1-F2"), deferred_to("R1-F3", ISSUE3)),
        [_finding(1, 1), _finding(1, 2), _finding(1, 3)],
        origin=True,
    )
    _bound_to_main(eng)
    reviewed = eng.state.reviewed_head_sha

    def changes(record):
        if change == "closed":
            gh.prs[PR].state = "CLOSED"
        elif change == "no-head-repository":
            gh.prs[PR].head_repository = ""
        else:
            gh.prs[PR].base_ref = "release/1.x"

    _after_persisting(eng, kind, Stage.OBSERVED, changes)
    unpushed = "the follow-ups are on GitHub but the candidate was not pushed"
    if change == "closed":
        with pytest.raises(VerificationError, match="is CLOSED; the workflow only operates"):
            eng.step()
    else:
        out = eng.step()
    assert [w[0] for w in gh.effect_writes] == ["create_issue", "write_issue_body"]
    assert eng.origin.head(BRANCH) == reviewed and gh.prs[PR].head_sha == reviewed
    s = load_state(eng.paths.state_file)
    assert s.last_review_result != "fixed" and s.last_fix_resolutions == []
    if change == "closed":
        assert s.phase == Phase.FIX
        assert [r["stage"] for r in s.effect_records] == ["observed", "observed", "intended"]
        assert [f["id"] for f in s.open_findings] == ["R1-F1", "R1-F2", "R1-F3"]
    elif change == "no-head-repository":
        assert out.next_phase == "BLOCKED", out.message
        assert f"PR {PR} has no readable head repository" in s.block_reason
        assert unpushed in s.block_reason
        assert [r["stage"] for r in s.effect_records] == ["observed", "observed", "intended"]
        assert [f["id"] for f in s.open_findings] == ["R1-F1", "R1-F2", "R1-F3"]
    else:
        assert out.next_phase == "REVIEW", out.message
        assert "base changed to 'release/1.x' from the reviewed base 'main'" in out.message
        assert unpushed in out.message
        assert s.effect_records == [] and s.open_findings == []
        assert [f["id"] for f in s.prior_findings] == ["R1-F1", "R1-F2", "R1-F3"]


def test_fix_pr_closed_after_a_follow_up_write_pushes_once_it_is_reopened(tmp_state_dir):
    """PR #205 R3-F2: the refusal is idempotent. A PR closed after the
    follow-up was created leaves the push unsent; once the PR is reopened,
    'resume' completes the plan from the journal: the follow-up is not
    created again, the candidate is pushed and the fix recorded."""
    gh = FakeGitHub()
    eng = _in_fix(
        tmp_state_dir,
        gh,
        fixer(fixed("R1-F1"), new_follow_up("R1-F2")),
        [_finding(1, 1), _finding(1, 2)],
        origin=True,
    )
    reviewed = eng.state.reviewed_head_sha

    def closes(record):
        gh.prs[PR].state = "CLOSED"

    _after_persisting(eng, EffectKind.FOLLOW_UP_ISSUE, Stage.OBSERVED, closes)
    with pytest.raises(VerificationError, match="is CLOSED"):
        eng.step()
    candidate = _worktree_head(eng.provider.calls[0])
    assert eng.origin.head(BRANCH) == reviewed and [w[0] for w in gh.effect_writes] == [
        "create_issue"
    ]

    gh.prs[PR].state = "OPEN"
    eng2 = _resumed(eng, tmp_state_dir, gh, "never")
    out = eng2.step()
    assert out.next_phase == "REVIEW", out.message
    assert [w[0] for w in gh.effect_writes] == ["create_issue"] and eng2.provider.calls == []
    assert eng.origin.head(BRANCH) == candidate == gh.prs[PR].head_sha
    s = load_state(eng2.paths.state_file)
    assert s.last_review_result == "fixed" and s.current_head_sha == candidate
    assert s.last_fix_resolutions[1]["follow_up_issue_url"] == ISSUE43


@pytest.mark.parametrize(
    ("head_repository", "branch", "reason"),
    [
        pytest.param("", BRANCH, f"PR {PR} has no readable head repository", id="no-head-repo"),
        pytest.param(
            "someone/repo",
            BRANCH,
            f"PR {PR} is headed in someone/repo, not in owner/repo",
            id="fork-head",
        ),
        pytest.param(
            "owner/repo",
            "another-branch",
            f"PR {PR} is headed at refs/heads/another-branch, but the FIX plan was made for "
            f"refs/heads/{BRANCH}",
            id="another-branch",
        ),
    ],
)
def test_fix_push_target_lost_when_the_push_lands_is_not_recorded_as_fixed(
    tmp_state_dir, head_repository, branch, reason
):
    """PR #205 R3-F2 at completion: the PR is read back headed at the
    candidate, the controller's own push, but no longer at the branch of the
    run's repository the plan pushed to. The fix is not recorded (the
    findings stay open, the plan stays saved) and it blocks; once the PR is
    headed there again, 'unblock' completes the plan from the journal with
    nothing sent again."""
    gh = FakeGitHub()
    eng = _in_fix(
        tmp_state_dir,
        gh,
        fixer(fixed("R1-F1"), new_follow_up("R1-F2")),
        [_finding(1, 1), _finding(1, 2)],
        origin=True,
    )

    def loses(record):
        gh.get_pr(PR)  # GitHub has caught up with the push
        gh.prs[PR].head_repository = head_repository
        gh.prs[PR].head_ref = branch

    _after_persisting(eng, EffectKind.PUSH, Stage.OBSERVED, loses)
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    candidate = _worktree_head(eng.provider.calls[0])
    s = load_state(eng.paths.state_file)
    assert reason in s.block_reason
    assert "read back after the FIX plan was sent" in s.block_reason
    assert "the fix of review round 1 is not recorded" in s.block_reason
    assert s.last_review_result != "fixed" and s.last_fix_resolutions == []
    assert [f["id"] for f in s.open_findings] == ["R1-F1", "R1-F2"]
    assert [r["stage"] for r in s.effect_records] == ["observed", "observed"]
    assert eng.origin.head(BRANCH) == candidate == gh.prs[PR].head_sha
    writes = list(gh.effect_writes)

    gh.prs[PR].head_repository = "owner/repo"
    gh.prs[PR].head_ref = BRANCH
    eng2 = _resumed(eng, tmp_state_dir, gh, "never")
    assert eng2.unblock("the PR is headed at its branch again").unblocked
    out = eng2.step()
    assert out.next_phase == "REVIEW", out.message
    assert gh.effect_writes == writes and eng2.provider.calls == []
    s = load_state(eng2.paths.state_file)
    assert s.last_review_result == "fixed" and s.current_head_sha == candidate
    assert s.open_findings == []


def test_fix_human_push_before_the_controllers_is_refused_by_the_lease_and_reviewed(
    tmp_state_dir,
):
    """Issue #90's scenario under #163: the follow-up issue is created, then a
    human pushes to the PR branch between the plan and the controller's
    push. The lease refuses the push (never forced); the PR routes to REVIEW
    of the actual HEAD with the findings carried, and that review is told
    about the follow-up issue, so the run ends clean with exactly one."""
    gh = FakeGitHub()
    human: list[str] = []

    def agent(req):
        if req.phase == "FIX":
            return fixer(new_follow_up("R1-F1"))(req)
        assert f"earlier rounds:\n  - R1-F1: {ISSUE43}\n" in req.prompt
        assert "- R1-F1 [nit] src/x.py:1 — typo" in req.prompt
        return block(review_payload(2, human[0], []))

    eng = _in_fix(tmp_state_dir, gh, agent, origin=True)
    reviewed = eng.state.reviewed_head_sha
    persist = eng._persist_effect

    def human_pushes(record):
        persist(record)
        if record.kind is EffectKind.PUSH and record.stage is Stage.ATTEMPTED and not human:
            human.append(publish_pr_head(eng, parent=reviewed, message="Human fix (#2)"))

    eng._persist_effect = human_pushes
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert "was refused and PR HEAD is" in out.message and human[0][:12] in out.message
    assert "no fix recorded" in out.message
    assert eng.origin.head(BRANCH) == human[0] == gh.prs[PR].head_sha
    s = load_state(eng.paths.state_file)
    assert s.current_head_sha == human[0] and s.last_fix_resolutions == []
    assert s.effect_records == [] and [f["id"] for f in s.prior_findings] == ["R1-F1"]
    assert s.last_review_result == "stale"

    assert eng.step().next_phase == "READY_FOR_MERGE"
    assert [c.phase for c in eng.provider.calls] == ["FIX", "REVIEW"]
    assert [w[0] for w in gh.effect_writes] == ["create_issue", "create_pr_comment"]


def test_fix_human_push_while_the_fixer_ran_sends_nothing_and_goes_to_review(tmp_state_dir):
    gh = FakeGitHub()
    human: list[str] = []
    fixes = fixer(fixed("R1-F1"), new_follow_up("R1-F2"))

    def pushed_meanwhile(req):
        human.append(publish_pr_head(eng, parent=reviewed_head_in(req.prompt)))
        return fixes(req)

    eng = _in_fix(
        tmp_state_dir, gh, pushed_meanwhile, [_finding(1, 1), _finding(1, 2)], origin=True
    )
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert "moved past the reviewed HEAD" in out.message and "while the fixer ran" in out.message
    assert "nothing was created, appended or pushed" in out.message
    assert gh.effect_writes == [] and eng.origin.head(BRANCH) == human[0]
    s = load_state(eng.paths.state_file)
    assert s.current_head_sha == human[0] and s.effect_records == []
    assert s.last_fix_resolutions == [] and len(s.prior_findings) == 2


def test_fix_pr_closed_while_the_fixer_ran_is_refused(tmp_state_dir):
    gh = FakeGitHub()
    fixes = fixer(fixed("R1-F1"))

    def closes(req):
        gh.prs[PR].state = "CLOSED"
        return fixes(req)

    eng = _in_fix(tmp_state_dir, gh, closes, origin=True)
    reviewed = eng.state.reviewed_head_sha
    with pytest.raises(VerificationError, match="is CLOSED; the workflow only operates on OPEN"):
        eng.step()
    assert gh.effect_writes == [] and eng.origin.head(BRANCH) == reviewed
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.FIX and s.effect_records == []


@pytest.mark.parametrize("committed", [False, True], ids=["before-a-commit", "after-a-commit"])
def test_fix_provider_failure_changes_nothing_and_the_relaunch_is_pushed(tmp_state_dir, committed):
    """The fixer exits non-zero, before or after a local commit: nothing is
    planned, created or pushed. The relaunch (attempt 2) reports the commit
    in its worktree (its own, or the one the failed attempt left), and the
    controller pushes it."""
    gh = FakeGitHub()

    def fails(req):
        if committed:
            fix_commit(req, "Fix R1-F1 (#2)")
        return ""

    second = _reports_fix(fixed("R1-F1")) if committed else fixer(fixed("R1-F1"))
    eng = _in_fix(tmp_state_dir, gh, scripted(fails, second), origin=True)
    reviewed = eng.state.reviewed_head_sha
    eng.provider.exit_code = 1
    with pytest.raises(ExecutionError, match="exited 1"):
        eng.step()
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.FIX and s.attempt == 1 and s.effect_records == []
    assert gh.effect_writes == [] and eng.origin.head(BRANCH) == reviewed

    eng.provider.exit_code = 0
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    head = _worktree_head(eng.provider.calls[1])
    assert eng.origin.head(BRANCH) == head != reviewed
    assert eng.state.current_head_sha == head


def test_fix_on_pi_matches_the_scripted_provider(tmp_path_factory):
    """Provider parity (#163): the same fixer behind Pi and behind the
    scripted provider gives the same plan, the same writes and the same
    state; the engine has no provider branch."""
    seen = []
    for on_pi in (False, True):
        gh = FakeGitHub()
        gh.add_issue(ISSUE3, "deferred in round 1", body=_marked("R1-F5"))
        state_dir = tmp_path_factory.mktemp("pi-run" if on_pi else "scripted") / ".autoforge"
        agent = fixer(fixed("R1-F1"), new_follow_up("R1-F2"), deferred_to("R1-F3", ISSUE3))
        eng = _in_fix(
            state_dir,
            gh,
            agent,
            [_finding(1, 1), _finding(1, 2), _finding(1, 3)],
            origin=True,
        )
        if on_pi:
            fake = _to_pi(eng, tmp_path_factory, agent)
        out = eng.step()
        s = load_state(eng.paths.state_file)
        assert eng.origin.head(BRANCH) == s.current_head_sha
        seen.append(
            (
                re.sub(r"\b[0-9a-f]{12}\b", "<SHA>", out.message),
                out.next_phase,
                s.last_fix_resolutions,
                gh.effect_writes,
            )
        )
    assert seen[0] == seen[1]
    assert seen[0][1] == "REVIEW"
    assert eng.provider.calls == [] and len(fake.launches()) == 1


def test_fix_dry_run_without_a_plan_performs_nothing(tmp_state_dir, offline_fetches):
    gh = FakeGitHub()
    eng = _in_fix(tmp_state_dir, gh, ["never"])
    eng._save()
    eng._github = _NoGitHub()
    before = eng.paths.state_file.read_bytes()
    offline_fetches.clear()
    out = eng.step(dry_run=True)
    notes = "\n".join(out.plan.notes)
    assert "would re-read the PR's HEAD, base and merge base first" in notes
    assert "launching the fixer, which publishes nothing" in notes
    assert "then the push of that HEAD as a fast-forward over the reviewed HEAD" in notes
    assert eng.provider.calls == [] and offline_fetches == []
    assert eng.paths.state_file.read_bytes() == before


def test_fix_dry_run_with_a_saved_plan_names_it_and_performs_nothing(tmp_state_dir, monkeypatch):
    from autoforge.engine import ControllerEngine

    gh = FakeGitHub()
    eng = _in_fix(
        tmp_state_dir,
        gh,
        fixer(fixed("R1-F1"), new_follow_up("R1-F2")),
        [_finding(1, 1), _finding(1, 2)],
        origin=True,
    )
    reviewed = eng.state.reviewed_head_sha
    monkeypatch.setattr(ControllerEngine, "_complete_fix", _crash)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    monkeypatch.undo()
    eng2 = _resumed(eng, tmp_state_dir, gh, "never")
    eng2._github = _NoGitHub()
    before = eng2.paths.state_file.read_bytes()
    out = eng2.step(dry_run=True)
    notes = "\n".join(out.plan.notes)
    assert "would reconcile follow_up_issue effect 0 on owner/repo (intended, 0 attempt(s))" in (
        notes
    )
    assert f"would reconcile push effect 1 on refs/heads/{BRANCH} (intended" in notes
    assert "would complete FIX of review round 1 from the persisted plan without launching" in (
        notes
    )
    assert eng2.provider.calls == [] and gh.effect_writes == []
    assert eng.origin.head(BRANCH) == reviewed
    assert eng2.paths.state_file.read_bytes() == before


def test_fix_no_change_requires_rationale(tmp_state_dir):
    gh = FakeGitHub()
    res = [{"finding_id": "R1-F1", "resolution": "no_change_with_rationale", "rationale": "nope"}]
    eng = _in_fix(tmp_state_dir, gh, [block(fix_payload(SHA_A, SHA_A, res))])
    eng.config.execution.max_correction_attempts = 0
    with pytest.raises(ControlResultValidationError, match="rationale"):
        eng.step()


# -- redaction at the persistence boundary (#81) -----------------------------------
# REVIEW findings and FIX resolutions are agent-authored text that lands in
# plain `state.json` and in `status --json`; the engine redacts them before
# they are assigned to state, mirroring the LOCAL-mode test in test_local.py.
def test_remote_review_findings_with_a_credential_never_reach_state_or_the_pr(tmp_state_dir):
    """Since #162 a REMOTE finding is published in the round's comment, so a
    credential-shaped one is refused by the published-content policy before
    the result is accepted (refused, never redacted and published): the
    reviewer is asked again, told the pattern class and not the text, and the
    secret reaches neither `state.json` nor the PR. The redaction at the
    persistence boundary stays behind that refusal as defence in depth."""
    secret = "sk-ant-" + "B" * 30
    gh = FakeGitHub()
    leaky = _finding(1)
    leaky["required_resolution"] = f"Set ANTHROPIC_API_KEY={secret} in the test fixture."
    eng = _in_review(
        tmp_state_dir,
        gh,
        [block(review_payload(1, SHA_A, [leaky])), block(review_payload(1, SHA_A, [_finding(1)]))],
    )
    out = eng.step()
    assert out.next_phase == "FIX"
    assert len(eng.provider.calls) == 2 and eng.provider.calls[1].correction
    assert "pattern class: env-assignment" in eng.provider.calls[1].prompt
    assert secret not in eng.provider.calls[1].prompt
    assert [f["required_resolution"] for f in eng.state.open_findings] == ["fix the typo"]
    assert secret not in eng.paths.state_file.read_text(encoding="utf-8")
    assert len(gh.comments[PR]) == 1 and secret not in gh.comments[PR][0].body


def test_remote_fix_resolutions_are_redacted_before_they_are_persisted(tmp_state_dir):
    secret = "sk-ant-" + "B" * 30
    gh = FakeGitHub()
    rationale = f"The fixture already sets ANTHROPIC_API_KEY={secret}, so nothing to do."
    agent = fixer(fixed("R1-F1"), no_change("R1-F2", rationale))
    eng = _in_fix(tmp_state_dir, gh, agent, [_finding(1, 1), _finding(1, 2)], origin=True)
    out = eng.step()
    assert out.next_phase == "REVIEW"
    assert secret not in json.dumps(eng.state.last_fix_resolutions)
    assert "***REDACTED***" in eng.state.last_fix_resolutions[1]["rationale"]
    assert secret not in eng.paths.state_file.read_text(encoding="utf-8")


def test_fix_rationale_redaction_may_carry_it_past_either_parser_bound(tmp_state_dir):
    """ADR 0004 §6.1 (#163): the parser bounds a rationale as the fixer wrote
    it, and the controller persists it redacted. A rationale at the upper
    bound that redaction lengthens, and one just over the lower bound that
    redaction shortens below it, are both accepted and persisted exactly as
    ``redact`` renders them; the state file reloads and holds neither secret."""
    from autoforge.redaction import redact
    from autoforge.result_parser import MIN_RATIONALE_CHARS

    unit = "HF_TOKEN=x;"
    body = unit * (MAX_FIX_RATIONALE_CHARS // len(unit))
    grows = body + "." * (MAX_FIX_RATIONALE_CHARS - len(body))
    shrinks = "HF_TOKEN=" + "x" * 40
    gh = FakeGitHub()
    agent = fixer(no_change("R1-F1", grows), no_change("R1-F2", shrinks), commit=False)
    eng = _in_fix(tmp_state_dir, gh, agent, [_finding(1, 1), _finding(1, 2)], origin=True)
    assert eng.step().next_phase == "REVIEW"
    assert len(eng.provider.calls) == 1  # accepted as written, no correction
    s = load_state(eng.paths.state_file)
    first, second = (r["rationale"] for r in s.last_fix_resolutions)
    assert first == redact(grows) and len(first) > MAX_FIX_RATIONALE_CHARS
    assert second == redact(shrinks) and len(second) < MIN_RATIONALE_CHARS
    text = eng.paths.state_file.read_text(encoding="utf-8")
    assert "HF_TOKEN=x" not in text and shrinks not in text


# -- correction retry ---------------------------------------------------------------------
def test_malformed_result_triggers_one_correction(tmp_state_dir, fake_github):
    def agent(req):
        if not req.correction:
            return "no block here\n"  # nothing was done, and no result block
        return implement(req)  # correction run does the work and reports it

    eng = make_engine(tmp_state_dir, agent, github=fake_github, origin=True)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    out = eng.step()
    assert out.next_phase == "REVIEW"
    assert len(eng.provider.calls) == 2
    second = eng.provider.calls[1]
    assert second.correction is True
    assert "did not return a valid CONTROL_RESULT" in second.prompt
    assert "Do not blindly repeat" in second.prompt
    assert "inspect the current real Git/GitHub state" in second.prompt
    assert eng.state.attempt == 0  # reset after success
    # both attempts logged
    run_dir = eng.paths.logs_dir / eng.state.run_id
    dirs = sorted(p.name for p in run_dir.iterdir() if p.is_dir())
    assert len(dirs) == 2 and dirs[0].endswith("-1") and dirs[1].endswith("-2")
    assert (run_dir / dirs[0] / "error.txt").exists()
    assert (run_dir / dirs[1] / "control-result.json").exists()


def test_correction_after_the_reviewer_posted_a_round_comment_blocks(tmp_state_dir):
    """The reviewer posted a comment carrying the round's marker itself (the
    controller's write since #162, ADR 0004 D9.6), then returned junk. The
    correction relaunch is preceded by the REVIEW entry probe: it finds a
    comment the controller did not journal and blocks rather than adopt or
    duplicate it. No correction is launched and nothing is posted."""
    gh = FakeGitHub()

    def reviews(req):
        assert not req.correction, "a correction must not be launched"
        gh.add_comment(PR, 100, review_comment_body(1, SHA_A, False))
        return "junk\n"

    eng = _in_review(tmp_state_dir, gh, reviews)
    assert eng.step().next_phase == "BLOCKED"
    assert len(eng.provider.calls) == 1 and len(gh.comments[PR]) == 1
    reason = load_state(eng.paths.state_file).block_reason
    assert "that the controller did not post" in reason and "D9.6" in reason
    assert comment_url(PR, 100) in reason and gh.effect_writes == []


def test_correction_after_the_fix_was_pushed_goes_to_review_without_relaunching(tmp_state_dir):
    """Someone pushed to the PR branch while the fixer ran (a fixer ignoring
    its contract, which since #163 publishes nothing, or a human), then the
    fixer returned junk. Before the correction relaunch the FIX entry probe
    sees the HEAD past the reviewed one and routes to REVIEW of the actual
    HEAD; a fixer is never relaunched against findings that push may have
    resolved."""
    gh = FakeGitHub()

    def pushes_then_junk(req):
        assert not req.correction
        gh.set_head(SHA_B)
        return "junk\n"

    eng = _in_fix(tmp_state_dir, gh, pushes_then_junk)
    out = eng.step()
    assert out.next_phase == "REVIEW" and "no fixer launched" in out.message
    assert len(eng.provider.calls) == 1
    s = load_state(eng.paths.state_file)
    assert s.current_head_sha == SHA_B and s.last_review_result == "stale" and s.attempt == 0


def test_correction_after_the_agent_posted_a_progress_comment_blocks(tmp_state_dir, fake_github):
    """UPDATE_EPIC: the agent posted a progress comment itself (the controller's
    write, ADR 0004 D9.7), then returned junk. The correction relaunch is an
    entry: it finds a comment the controller did not journal and blocks rather
    than adopt or duplicate it."""

    def agent(req):
        assert not req.correction, "a correction must not be launched"
        post_progress_comment(fake_github)
        return "junk\n"

    eng = _in_update_epic(tmp_state_dir, fake_github, agent)
    assert eng.step().next_phase == "BLOCKED"
    assert len(eng.provider.calls) == 1 and len(fake_github.comments[EPIC]) == 1
    reason = load_state(eng.paths.state_file).block_reason
    assert "that the controller did not post" in reason and "D9.7" in reason
    assert _progress_posts(fake_github) == []


def test_every_remote_agent_phase_reconciles_with_github_before_launching():
    """The run-log refusal names what `resume` does on re-entry; every REMOTE
    phase with an agent prompt has such a description because every one of
    them reconciles in `_remote_entry`. The table cannot silently fall back
    to "relaunches the agent" for a phase that was left out."""
    from autoforge.engine import _REMOTE_REENTRY_RECONCILIATION, PHASE_TEMPLATE

    agent_phases = {p for p, template in PHASE_TEMPLATE.items() if template}
    assert set(_REMOTE_REENTRY_RECONCILIATION) == agent_phases
    assert all(
        "relaunch" not in text or "instead of relaunching" in text
        for text in _REMOTE_REENTRY_RECONCILIATION.values()
    )


def test_correction_is_bounded(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, ["junk", "junk again"], github=fake_github, origin=True)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises(ControlResultValidationError, match="after 2 attempt"):
        eng.step()
    assert len(eng.provider.calls) == 2
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.ANALYZE_EXECUTE and s.attempt == 2


def test_correction_disabled(tmp_state_dir, fake_github):
    from autoforge.errors import ControlResultError

    eng = make_engine(tmp_state_dir, scripted("junk", implement), github=fake_github, origin=True)
    eng.config.execution.max_correction_attempts = 0
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises((ControlResultError, ControlResultValidationError)):
        eng.step()
    assert len(eng.provider.calls) == 1


def test_nonzero_exit_is_not_corrected(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, ["junk", "junk"], github=fake_github, exit_code=3, origin=True)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises(ExecutionError, match="exited 3"):
        eng.step()
    assert len(eng.provider.calls) == 1
    assert eng.state.phase == Phase.ANALYZE_EXECUTE


def test_timeout_raises_and_keeps_state(tmp_state_dir, fake_github):
    class TimingOut(ScriptedProvider):
        def execute(self, req):
            self.calls.append(req)
            return AgentExecutionResult(
                command=["x"],
                exit_code=-1,
                stdout="",
                stderr="",
                started_at="t",
                finished_at="t",
                timed_out=True,
            )

    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    eng.providers._overrides = {"claude": TimingOut(), "opencode": TimingOut()}
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises(ExecutionTimeoutError):
        eng.step()
    assert eng.state.phase == Phase.ANALYZE_EXECUTE


# -- post-agent run-log refusal: every outcome persists the launch (PR #89 F2) ---------
class _TimingOut(ScriptedProvider):
    def execute(self, req):
        self.calls.append(req)
        if self._handler is not None:
            self._handler(req)
        return AgentExecutionResult(
            command=["x"],
            exit_code=-1,
            stdout="",
            stderr="",
            started_at="t",
            finished_at="t",
            timed_out=True,
        )


@pytest.mark.parametrize(
    "outcome, expected",
    [
        ("timeout", r"timed out \(idle timeout 900s, max runtime unset\)"),
        ("exit", r"exit 3"),
        ("malformed", r"ControlResultError: "),
    ],
)
def test_post_agent_journal_refusal_names_the_outcome_and_keeps_the_attempt(
    tmp_state_dir, outcome, expected
):
    """PR #89 F2: the launch is persisted before the agent starts, so an
    invocation that times out, exits non-zero or returns no result and then
    has its journal append refused is still on disk as attempt 1, its
    artifacts are published, and the refusal names the outcome it
    interrupted instead of masking it. Nothing is launched again -- not the
    correction a malformed result would otherwise earn."""
    from autoforge.runlog import MAX_EVENT_JOURNAL_BYTES

    gh = FakeGitHub()
    seen_attempts: list[int] = []
    eng = _in_review(tmp_state_dir, gh, None)
    journal = Path(eng.paths.logs_dir) / eng.state.run_id / "events.jsonl"

    def enlarges_the_journal(req):
        seen_attempts.append(load_state(eng.paths.state_file).attempt)
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.touch()
        os.truncate(journal, 2 * MAX_EVENT_JOURNAL_BYTES)
        return "no block here\n"

    if outcome == "timeout":
        provider = _TimingOut(None)
        provider._handler = enlarges_the_journal
        eng.providers._overrides = {"claude": provider, "opencode": provider}
    else:
        provider = eng.provider
        provider._handler = enlarges_the_journal
        if outcome == "exit":
            provider.exit_code = 3
    with pytest.raises(
        StateError,
        match=r"corrupted event journal.*interrupted attempt 1 of REVIEW after the agent had "
        r"returned with: " + expected,
    ):
        eng.step()
    assert seen_attempts == [1], "the launch was not persisted before the agent ran"
    assert len(provider.calls) == 1, "nothing is launched again until the log is repaired"
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.REVIEW and s.review_round == 0 and s.attempt == 1
    step_dirs = sorted(p.name for p in journal.parent.iterdir() if p.is_dir())
    assert step_dirs == ["001-review-1"]
    assert (journal.parent / step_dirs[0] / "error.txt").exists()
    assert journal.stat().st_size == 2 * MAX_EVENT_JOURNAL_BYTES, "not carried forward"


def test_launch_is_persisted_before_the_agent_runs(tmp_state_dir):
    """The attempt counter is on disk when the agent starts: a crash anywhere
    inside the invocation leaves a state that says a launch happened."""
    gh = FakeGitHub()
    seen: list[int] = []

    def reads_state(req):
        seen.append(load_state(eng.paths.state_file).attempt)
        return block(review_payload(1, SHA_A, []))

    eng = _in_review(tmp_state_dir, gh, reads_state)
    assert eng.step().next_phase == "READY_FOR_MERGE"
    assert seen == [1] and load_state(eng.paths.state_file).attempt == 0


# -- agent-reported failure / blocked ----------------------------------------------------------
def test_agent_failure_moves_to_failed(tmp_state_dir, fake_github):
    payload = {"phase": "ANALYZE_EXECUTE", "status": "failure", "message": "tests red"}
    eng = make_engine(tmp_state_dir, [block(payload)], github=fake_github, origin=True)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    out = eng.step()
    assert out.next_phase == "FAILED" and eng.state.block_reason == "tests red"


def test_agent_blocked_moves_to_blocked(tmp_state_dir, fake_github):
    payload = {"phase": "ANALYZE_EXECUTE", "status": "blocked", "message": "need decision"}
    eng = make_engine(tmp_state_dir, [block(payload)], github=fake_github, origin=True)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    assert eng.step().next_phase == "BLOCKED"
    with pytest.raises(StateTransitionError):
        eng.step()


# -- dry run ------------------------------------------------------------------------------------
def test_dry_run_is_side_effect_free(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, ["never"], github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    out = eng.step(dry_run=True)
    assert out.dry_run and out.plan is not None
    assert eng.provider.calls == [] and fake_github.calls == []
    assert not eng.paths.state_file.exists() and not eng.paths.logs_dir.exists()
    plan = out.plan
    assert plan.template == "analyze_execute.md" and plan.expected_next.startswith("REVIEW")
    assert plan.variables["ISSUE_URL"] == ISSUE
    assert plan.command[-1] == plan.prompt_full


def test_dry_run_review_shows_round_and_model(tmp_state_dir):
    gh = FakeGitHub()
    eng = _in_review(tmp_state_dir, gh, [], round_done=1)
    plan = eng.step(dry_run=True).plan
    assert plan.review_round == 2 and plan.model == "openai/gpt-5.6-terra"
    assert "round 2" in plan.routing


# -- MERGE: controller-owned, gated (issue #8) -----------------------------------------------------
REVIEW_CID = 100  # the comment id of the round-2 review `_in_merge` binds


def _in_merge(tmp_state_dir, gh, script=None, reviewed=SHA_A, clean=True, phase=Phase.MERGE):
    """Engine parked in MERGE with the gate open and a clean review bound to ``reviewed``.

    The review is bound the way ``_apply_review`` binds it: round 2 at
    ``reviewed`` on ``main`` from merge base ``MERGE_BASE`` of ``PR``,
    decided by a comment on the fake that carries that round's marker
    (``REVIEW_CID``), which the gate re-reads (#94).
    """
    eng = make_engine(tmp_state_dir, script or [], github=gh)
    eng.config.safety.allow_merge = True
    eng.state.phase = phase
    eng.state.current_pr_url = PR
    eng.state.current_head_sha = reviewed
    eng.state.current_base_ref = "main"
    eng.state.current_merge_base_sha = MERGE_BASE
    eng.state.reviewed_pr_url = PR
    eng.state.reviewed_head_sha = reviewed
    eng.state.reviewed_base_ref = "main"
    eng.state.reviewed_merge_base_sha = MERGE_BASE
    eng.state.last_review_result = "clean" if clean else "needs_fix"
    eng.state.review_round = 2
    if isinstance(gh, FakeGitHub):  # a scripted real client answers the read itself
        gh.add_comment(PR, REVIEW_CID, review_comment_body(2, reviewed, not clean))
    eng.state.last_review_comment_url = comment_url(PR, REVIEW_CID)
    eng._save()
    return eng


def test_merge_phase_requires_both_gates(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, [], github=fake_github)
    eng.state.phase = Phase.MERGE
    eng.state.current_pr_url = PR
    with pytest.raises(VerificationError, match="merge is disabled"):
        eng.step(allow_merge=True)
    eng.config.safety.allow_merge = True
    with pytest.raises(VerificationError, match="merge is disabled"):
        eng.step(allow_merge=False)
    assert fake_github.merges == []  # closed gate: gh pr merge is never run


def test_merge_when_explicitly_enabled_is_done_by_controller(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_A)
    # The scripted "agent" would answer if it were ever called — it must not be.
    script = [block({"phase": "MERGE", "status": "success"})]
    eng = make_engine(tmp_state_dir, script, github=fake_github)
    eng.config.safety.allow_merge = True
    eng.state.phase = Phase.READY_FOR_MERGE
    eng.state.current_pr_url = PR
    eng.state.current_head_sha = SHA_A
    eng.state.current_base_ref = "main"
    eng.state.current_merge_base_sha = MERGE_BASE
    eng.state.reviewed_pr_url = PR
    eng.state.reviewed_head_sha = SHA_A
    eng.state.reviewed_base_ref = "main"
    eng.state.reviewed_merge_base_sha = MERGE_BASE
    eng.state.last_review_result = "clean"
    eng.state.review_round = 2
    fake_github.add_comment(PR, 100, review_comment_body(2, SHA_A, False))
    eng.state.last_review_comment_url = comment_url(PR, 100)
    assert eng.step(allow_merge=True).next_phase == "MERGE"
    out = eng.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC", out.message
    assert "merged by the controller" in out.message
    assert fake_github.merges == [(PR, "squash", SHA_A, False)]
    assert fake_github.prs[PR].state == "MERGED"
    assert eng.provider.calls == []  # no agent invocation in MERGE
    assert eng.state.counted_merged_prs == [PR] and eng.state.merged_since_epic_update == 1
    assert eng.state.current_issue_url == ISSUE  # issue switching belongs to UPDATE_EPIC
    persisted = load_state(eng.paths.state_file)
    assert persisted.phase == Phase.UPDATE_EPIC and persisted.counted_merged_prs == [PR]


def test_merge_uses_configured_method_and_delete_branch(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_A)
    eng = _in_merge(tmp_state_dir, fake_github)
    eng.config.merge.method = "rebase"
    eng.config.merge.delete_branch = True
    plan = eng.step(dry_run=True, allow_merge=True).plan
    assert plan.command == [
        "gh",
        "pr",
        "merge",
        PR,
        "--rebase",
        "--match-head-commit",
        SHA_A,
        "--delete-branch",
    ]
    assert plan.profile_name.startswith("(none") and plan.prompt_full == ""
    assert fake_github.merges == []  # dry-run never merges
    assert eng.step(allow_merge=True).next_phase == "UPDATE_EPIC"
    assert fake_github.merges == [(PR, "rebase", SHA_A, True)]


def test_merge_head_moved_after_clean_review_goes_back_to_review(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_B)
    eng = _in_merge(tmp_state_dir, fake_github, reviewed=SHA_A)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "REVIEW" and "not merged" in out.message
    assert fake_github.merges == []
    assert eng.state.current_head_sha == SHA_B and eng.state.last_review_result == "stale"
    assert eng.state.counted_merged_prs == []


def test_merge_head_moved_between_verification_and_write_goes_back_to_review(
    tmp_state_dir, fake_github
):
    """R2-F1: a push after the pre-merge checks makes `--match-head-commit` refuse the write;
    the post-write read finds the PR OPEN at the new HEAD -> stale review -> REVIEW, not BLOCKED."""
    fake_github.add_pr(head_sha=SHA_A)
    eng = _in_merge(tmp_state_dir, fake_github, reviewed=SHA_A)
    orig_merge = fake_github.merge_pr

    def push_then_merge(*a, **kw):
        fake_github.set_head(SHA_B)  # someone pushes right before gh pr merge runs
        orig_merge(*a, **kw)  # -> "head commit does not match"

    fake_github.merge_pr = push_then_merge
    out = eng.step(allow_merge=True)
    assert out.next_phase == "REVIEW" and "not merged" in out.message
    assert "head commit does not match" in out.message
    assert len(fake_github.merges) == 1 and fake_github.prs[PR].state == "OPEN"
    assert eng.state.phase == Phase.REVIEW and eng.state.block_reason == ""
    assert eng.state.current_head_sha == SHA_B and eng.state.reviewed_head_sha == SHA_A
    assert eng.state.last_review_result == "stale" and eng.state.open_findings == []
    assert eng.state.counted_merged_prs == [] and eng.state.merged_since_epic_update == 0
    persisted = load_state(eng.paths.state_file)
    assert persisted.phase == Phase.REVIEW and persisted.current_head_sha == SHA_B
    # the next step is a review of the new HEAD (round 3 on SHA_B), not another merge attempt
    plan = eng.step(dry_run=True).plan
    assert plan.phase == "REVIEW" and plan.review_round == 3
    assert plan.variables["HEAD_SHA"] == SHA_B


def test_merge_head_moved_after_write_with_auto_merge_armed_disarms_then_reviews(
    tmp_state_dir, fake_github
):
    """HEAD drift after the write is only routed to REVIEW once no async merge is pending."""
    fake_github.add_pr(head_sha=SHA_A)
    fake_github.merge_leaves_open = True
    fake_github.merge_arms_auto = True
    eng = _in_merge(tmp_state_dir, fake_github, reviewed=SHA_A)
    orig_merge = fake_github.merge_pr

    def merge_then_push(*a, **kw):
        orig_merge(*a, **kw)
        fake_github.set_head(SHA_B)

    fake_github.merge_pr = merge_then_push
    out = eng.step(allow_merge=True)
    assert out.next_phase == "REVIEW" and "disabled again" in out.message
    assert fake_github.disabled_auto == [PR] and eng.state.last_review_result == "stale"
    assert eng.state.counted_merged_prs == []


@pytest.mark.parametrize(
    "arrange, needle",
    [
        (lambda gh: setattr(gh, "disable_auto_error", "403"), "could not be disabled"),
        (
            lambda gh: gh.merge_queue.update({PR: MergeQueueStatus(enabled=True, in_queue=True)}),
            "in the merge queue",
        ),
        (lambda gh: setattr(gh, "merge_queue_error", "boom"), "could not be read"),
    ],
)
def test_merge_head_moved_after_write_with_pending_async_merge_blocks(
    tmp_state_dir, fake_github, arrange, needle
):
    """Drift after the write must not re-enter REVIEW while GitHub could still merge the new
    HEAD on its own (auto-merge stuck armed, PR queued, queue status unreadable)."""
    fake_github.add_pr(head_sha=SHA_A)
    fake_github.merge_leaves_open = True
    fake_github.merge_arms_auto = True
    eng = _in_merge(tmp_state_dir, fake_github, reviewed=SHA_A)
    orig_merge = fake_github.merge_pr

    def merge_then_push(*a, **kw):
        orig_merge(*a, **kw)
        fake_github.set_head(SHA_B)
        arrange(fake_github)  # the async-merge condition appears after the write

    fake_github.merge_pr = merge_then_push
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED" and needle in eng.state.block_reason
    assert eng.state.counted_merged_prs == [] and eng.state.last_review_result == "clean"


def test_merge_refuses_without_clean_review(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_A)
    eng = _in_merge(tmp_state_dir, fake_github, clean=False)
    with pytest.raises(VerificationError, match="clean review"):
        eng.step(allow_merge=True)
    assert fake_github.merges == []
    eng2 = _in_merge(tmp_state_dir, fake_github)
    eng2.state.reviewed_head_sha = ""
    with pytest.raises(VerificationError, match="clean review"):
        eng2.step(allow_merge=True)
    assert fake_github.merges == []


# -- the clean review is re-read from GitHub before MERGE (#94) ----------------------
# `_apply_review` verified the round's comment when it wrote the binding, but
# the binding then lives in a plain JSON file. The gate re-reads the comment
# the state names and requires it to still be this round's clean review of
# the reviewed revision on the reviewed PR; anything else is conclusive.


def _gate_blocks_on_review_comment(eng, gh, expect: str) -> None:
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED", out.message
    reason = eng.state.block_reason
    assert expect in reason, reason
    assert "not backed by GitHub" in reason or "not a transient GitHub failure" in reason
    assert "nothing was merged or counted" in reason.lower()
    assert gh.merges == [] and eng.state.counted_merged_prs == []
    assert eng.state.attempt == 0  # conclusive: no verification attempt was consumed
    assert load_state(eng.paths.state_file).phase == Phase.BLOCKED


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
def test_gate_requires_the_review_comment_url_in_state(tmp_state_dir, fake_github, phase):
    """A clean review without the comment that decided it is not a binding
    the gate can re-verify, so it is refused with the other missing fields."""
    fake_github.add_pr(head_sha=SHA_A)
    eng = _in_merge(tmp_state_dir, fake_github, phase=phase)
    eng.state.last_review_comment_url = ""
    with pytest.raises(VerificationError, match="review comment.*refusing to merge"):
        eng.step(allow_merge=True)
    assert fake_github.merges == [] and fake_github.calls == []


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
def test_gate_blocks_when_the_review_comment_is_gone(tmp_state_dir, fake_github, phase):
    """The comment the state names no longer exists on GitHub (deleted, or
    never posted: a hand-written state). Conclusive: BLOCKED, no merge."""
    fake_github.add_pr(head_sha=SHA_A)
    eng = _in_merge(tmp_state_dir, fake_github, phase=phase)
    fake_github.comments.clear()
    _gate_blocks_on_review_comment(eng, fake_github, "comment not found")
    assert ("get_comment", comment_url(PR, REVIEW_CID)) in fake_github.calls


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
def test_gate_blocks_when_the_state_names_a_comment_on_another_pr(
    tmp_state_dir, fake_github, phase
):
    """`last_review_comment_url` on PR 43 while the review is bound to PR 42:
    refused from state alone, before the comment is even read."""
    other = "https://github.com/owner/repo/pull/43"
    fake_github.add_pr(head_sha=SHA_A)
    eng = _in_merge(tmp_state_dir, fake_github, phase=phase)
    fake_github.add_comment(other, 200, review_comment_body(2, SHA_A, False))
    eng.state.last_review_comment_url = comment_url(other, 200)
    _gate_blocks_on_review_comment(eng, fake_github, "is not on the reviewed PR")
    assert not any(c[0] == "get_comment" for c in fake_github.calls)


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
def test_gate_blocks_when_github_answers_with_a_comment_on_another_pr(
    tmp_state_dir, fake_github, phase
):
    """The comments API addresses a comment by id alone: a state URL spelling
    the reviewed PR over a comment that is really on PR 43 is answered with
    that comment, and GitHub's parent is the one that counts."""
    other = "https://github.com/owner/repo/pull/43"
    fake_github.add_pr(head_sha=SHA_A)
    eng = _in_merge(tmp_state_dir, fake_github, phase=phase)
    fake_github.comments.clear()
    fake_github.add_comment(other, REVIEW_CID, review_comment_body(2, SHA_A, False))
    _gate_blocks_on_review_comment(eng, fake_github, "not that comment on the reviewed PR")
    assert "pull/43" in eng.state.block_reason


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
@pytest.mark.parametrize(
    ("body", "expect"),
    [
        pytest.param(review_comment_body(1, SHA_A, False), "no comment carries", id="other-round"),
        pytest.param(review_comment_body(2, SHA_B, False), "no comment carries", id="other-head"),
        pytest.param(
            review_comment_body(2, SHA_A, False, base_ref="release/1.x"),
            "no comment carries",
            id="other-base",
        ),
        pytest.param(
            review_comment_body(2, SHA_A, False, base_ref=None), "no comment carries", id="no-base"
        ),
        pytest.param(
            review_comment_body(2, SHA_A, True, ["R2-F1"]), "needs_fix_round: true", id="needs-fix"
        ),
        pytest.param("# AI Code Review — Round 2\n\nLGTM\n", "no comment carries", id="no-marker"),
        pytest.param(
            '# Round 2\n<!-- ai-review-result: {"round": 2} -->\n',
            "cannot establish",
            id="unreadable-marker",
        ),
        pytest.param(
            review_comment_body(2, SHA_A, False) + review_comment_body(2, SHA_A, False),
            "cannot establish",
            id="two-markers",
        ),
    ],
)
def test_gate_blocks_when_the_review_comment_does_not_say_what_the_state_says(
    tmp_state_dir, fake_github, phase, body, expect
):
    """The comment exists on the reviewed PR but is not this round's clean
    review of the reviewed revision: its marker names another round, HEAD or
    base (or none), says a fix round is needed, is missing, is unreadable,
    or is doubled. Prose such as "LGTM" is never evidence."""
    fake_github.add_pr(head_sha=SHA_A)
    eng = _in_merge(tmp_state_dir, fake_github, phase=phase)
    fake_github.comments.clear()
    fake_github.add_comment(PR, REVIEW_CID, body)
    _gate_blocks_on_review_comment(eng, fake_github, expect)


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
def test_gate_reads_the_review_comment_before_running_the_prs_code(
    tmp_state_dir, fake_github, phase
):
    """A GitHub-side fact: an unbacked review blocks before
    `merge.verification_commands` ever run the PR's code locally."""
    marker = tmp_state_dir.parent / "ran.txt"
    eng, _sha = _in_merge_on_commit(tmp_state_dir, fake_github, [_record_cwd(marker)], phase=phase)
    fake_github.comments.clear()
    _gate_blocks_on_review_comment(eng, fake_github, "comment not found")
    assert not marker.exists()


def test_gate_reads_the_review_comment_exactly_once_per_pass(tmp_state_dir, fake_github):
    """Happy path: READY_FOR_MERGE and MERGE each re-read the comment once;
    the merge itself and the post-merge read-back add no comment read."""
    fake_github.add_pr(head_sha=SHA_A)
    eng = _in_merge(tmp_state_dir, fake_github, phase=Phase.READY_FOR_MERGE)
    assert eng.step(allow_merge=True).next_phase == "MERGE"
    reads = [c for c in fake_github.calls if c[0] == "get_comment"]
    assert reads == [("get_comment", comment_url(PR, REVIEW_CID))]
    out = eng.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC", out.message
    reads = [c for c in fake_github.calls if c[0] == "get_comment"]
    assert reads == [("get_comment", comment_url(PR, REVIEW_CID))] * 2
    assert fake_github.merges == [(PR, "squash", SHA_A, False)]


def test_gate_re_reads_the_review_comment_the_controller_posted(tmp_state_dir, fake_github):
    """#162: the comment the gate re-reads is the one the controller rendered,
    posted and read back for the clean round, by the URL it persisted. The
    gate itself is unchanged."""
    eng = _in_review(tmp_state_dir, fake_github, [block(review_result(1, SHA_A))])
    eng.config.safety.allow_merge = True
    assert eng.step().next_phase == "READY_FOR_MERGE"
    posted = controller_review_comment(eng, 1).url
    assert load_state(eng.paths.state_file).last_review_comment_url == posted
    assert eng.step(allow_merge=True).next_phase == "MERGE"
    out = eng.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC", out.message
    reads = [c for c in fake_github.calls if c[0] == "get_comment"]
    assert reads == [("get_comment", posted)] * 2
    assert fake_github.merges == [(PR, "squash", SHA_A, False)]


def test_gate_blocks_when_the_comment_the_controller_posted_was_edited(tmp_state_dir, fake_github):
    """#162: the controller's comment is evidence only while it still says what
    the controller posted. Edited afterwards to drop its marker, it no longer
    backs the clean review, and the gate blocks as for any other comment."""
    eng = _in_review(tmp_state_dir, fake_github, [block(review_result(1, SHA_A))])
    eng.config.safety.allow_merge = True
    assert eng.step().next_phase == "READY_FOR_MERGE"
    posted = controller_review_comment(eng, 1)
    edited = replace(posted, body=posted.body.split("<!--")[0] + "LGTM\n")
    fake_github.comments[PR] = [edited]
    _gate_blocks_on_review_comment(eng, fake_github, "no comment carries")
    assert ("get_comment", posted.url) in fake_github.calls


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
def test_gate_does_not_read_the_review_comment_of_a_drifted_pr(tmp_state_dir, fake_github, phase):
    """A PR past the reviewed revision goes back to REVIEW whatever its old
    review says; the stale comment is not read."""
    fake_github.add_pr(head_sha=SHA_B)
    eng = _in_merge(tmp_state_dir, fake_github, phase=phase)
    assert eng.step(allow_merge=True).next_phase == "REVIEW"
    assert not any(c[0] == "get_comment" for c in fake_github.calls)
    assert fake_github.merges == []


def test_merge_gh_failure_blocks_without_counting(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_A)
    fake_github.merge_error = "`gh pr merge` failed (exit 1): Pull request is not mergeable"
    eng = _in_merge(tmp_state_dir, fake_github)
    out = eng.step(allow_merge=True)  # no exception: a deterministic BLOCKED, no blind retry
    assert out.next_phase == "BLOCKED"
    assert "not mergeable" in eng.state.block_reason and "Nothing was counted" in out.message
    assert fake_github.prs[PR].state == "OPEN"
    assert eng.state.counted_merged_prs == [] and eng.state.merged_since_epic_update == 0
    assert len(fake_github.merges) == 1


def test_merge_exit_zero_but_pr_still_open_blocks(tmp_state_dir, fake_github):
    """Merge queue / auto-merge: gh returns 0 but GitHub has not merged — never count it."""
    fake_github.add_pr(head_sha=SHA_A)
    fake_github.merge_leaves_open = True
    eng = _in_merge(tmp_state_dir, fake_github)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED"
    assert "still OPEN" in eng.state.block_reason or "as OPEN" in eng.state.block_reason
    assert eng.state.counted_merged_prs == []


def test_merge_closed_pr_blocks(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_A, state="CLOSED")
    eng = _in_merge(tmp_state_dir, fake_github)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED" and "CLOSED" in eng.state.block_reason
    assert fake_github.merges == []


def test_merge_recovery_after_crash_counts_once(tmp_state_dir, fake_github):
    """Crash after `gh pr merge` succeeded but before state was saved -> resume is idempotent."""
    fake_github.add_pr(head_sha=SHA_A, state="MERGED")
    eng = _in_merge(tmp_state_dir, fake_github)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC" and "recovered" in out.message
    assert fake_github.merges == []  # never re-runs gh pr merge on a merged PR
    assert eng.state.counted_merged_prs == [PR] and eng.state.merged_since_epic_update == 1
    # Same recovery when the counter was already persisted: still exactly one.
    eng2 = _in_merge(tmp_state_dir, fake_github)
    eng2.state.counted_merged_prs = [PR]
    eng2.state.merged_since_epic_update = 1
    out2 = eng2.step(allow_merge=True)
    assert out2.next_phase == "UPDATE_EPIC" and "already counted" in out2.message
    assert eng2.state.counted_merged_prs == [PR] and eng2.state.merged_since_epic_update == 1


def test_merge_recovery_at_unreviewed_head_blocks(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_C, state="MERGED")
    eng = _in_merge(tmp_state_dir, fake_github, reviewed=SHA_A)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED" and "never reviewed" in eng.state.block_reason
    assert eng.state.counted_merged_prs == []


def test_merge_dry_run_plan_has_no_agent_and_no_side_effects(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_A)
    eng = _in_merge(tmp_state_dir, fake_github)
    calls_before = list(fake_github.calls)
    out = eng.step(dry_run=True)  # even without --allow-merge, planning is allowed
    assert out.plan.phase == "MERGE" and out.plan.provider == "(none)"
    assert out.plan.command[:3] == ["gh", "pr", "merge"]
    assert "--match-head-commit" in out.plan.command
    assert "UPDATE_EPIC" in out.plan.expected_next
    assert fake_github.calls == calls_before and fake_github.merges == []
    assert eng.state.phase == Phase.MERGE


def test_ready_for_merge_head_moved_goes_back_to_review(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_B)
    eng = _in_ready(tmp_state_dir, fake_github, reviewed=SHA_A)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "REVIEW" and "not merged" in out.message
    assert eng.state.last_review_result == "stale" and eng.state.current_head_sha == SHA_B
    assert fake_github.merges == []


def test_step_terminal_done_reports_cleanly(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, [], github=fake_github)
    eng.state.phase = Phase.DONE
    assert "DONE" in eng.step().message


def test_run_logs_are_redacted_and_structured(tmp_state_dir, fake_github):
    payload = {
        "phase": "ANALYZE_EXECUTE",
        "status": "failure",
        "message": "token ghp_abcdefghijklmnopqrstuvwxyz0123456789 leaked",
    }
    eng = make_engine(tmp_state_dir, [block(payload)], github=fake_github, origin=True)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    eng.step()
    run_dir = eng.paths.logs_dir / eng.state.run_id
    step_dirs = [p for p in run_dir.iterdir() if p.is_dir()]
    assert len(step_dirs) == 1
    d = step_dirs[0]
    for name in (
        "request.json",
        "prompt.md",
        "execution.json",
        "stdout.log",
        "stderr.log",
        "control-result.json",
    ):
        assert (d / name).exists(), name
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in (d / "stdout.log").read_text()
    req = json.loads((d / "request.json").read_text())
    assert req["provider"] == "claude" and req["prompt_version"] == __prompt_version__
    assert "environ" not in req and req["idle_timeout_seconds"] == 900
    assert req["max_runtime_seconds"] is None and req["timeout_seconds"] == 0
    events = (run_dir / "events.jsonl").read_text().strip().splitlines()
    assert len(events) == 1


def test_epic_and_issue_recorded(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, [], github=fake_github)
    assert eng.state.epic_url == EPIC and eng.state.repository == "owner/repo"


# -- MERGE: controller-side pre-merge verification (PR #24 review R1-F1) -----------------------
def _park_and_step(tmp_state_dir, fake_github):
    eng = _in_merge(tmp_state_dir, fake_github)
    return eng, eng.step(allow_merge=True)


def test_merge_blocks_on_conflicting_pr_without_calling_gh(tmp_state_dir, fake_github):
    fake_github.add_pr().mergeable = "CONFLICTING"
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "BLOCKED" and "CONFLICTING" in eng.state.block_reason
    assert fake_github.merges == [] and eng.state.counted_merged_prs == []


def test_merge_unknown_mergeability_stays_in_merge_for_resume(tmp_state_dir, fake_github):
    """Inconclusive GitHub data: fail closed but do NOT terminalise the run."""
    fake_github.add_pr().mergeable = "UNKNOWN"
    eng = _in_merge(tmp_state_dir, fake_github)
    with pytest.raises(VerificationError, match="not determined mergeability") as info:
        eng.step(allow_merge=True)
    # R2-F2: a plain `resume` is a no-op with the gate closed; guidance names the real command.
    assert "'resume --allow-merge' to re-check" in str(info.value)
    assert eng.state.phase == Phase.MERGE and fake_github.merges == []
    # once GitHub has computed it, resume merges normally
    fake_github.prs[PR].mergeable = "MERGEABLE"
    assert eng.step(allow_merge=True).next_phase == "UPDATE_EPIC"
    assert len(fake_github.merges) == 1


@pytest.mark.parametrize(
    "check",
    [
        ci_check(conclusion="FAILURE"),
        CheckInfo(name="ci", state="COMPLETED", conclusion="CANCELLED"),
        CheckInfo(name="ci", state="COMPLETED", conclusion="TIMED_OUT"),
        CheckInfo(name="ci", state="FAILURE"),  # legacy commit status
        CheckInfo(name="ci", state="WEIRD", conclusion="MAYBE"),  # unknown -> fail closed
    ],
)
def test_merge_blocks_on_failing_or_unknown_check(tmp_state_dir, fake_github, check):
    fake_github.add_pr().checks = [
        CheckInfo(name="lint", state="COMPLETED", conclusion="SUCCESS"),
        check,
    ]
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "BLOCKED"
    assert "ci" in eng.state.block_reason and "lint" not in eng.state.block_reason
    assert fake_github.merges == [] and eng.state.counted_merged_prs == []


@pytest.mark.parametrize(
    "check",
    [
        ci_check(state="IN_PROGRESS", conclusion=""),
        ci_check(state="QUEUED", conclusion=""),
        CheckInfo(name="ci", state="PENDING"),  # legacy commit status
    ],
)
def test_merge_waits_for_pending_checks_without_merging(tmp_state_dir, fake_github, check):
    fake_github.add_pr().checks = [check]
    eng = _in_merge(tmp_state_dir, fake_github)
    with pytest.raises(VerificationError, match="still running: ci"):
        eng.step(allow_merge=True)
    assert eng.state.phase == Phase.MERGE and fake_github.merges == []
    fake_github.prs[PR].checks = [ci_check()]
    assert eng.step(allow_merge=True).next_phase == "UPDATE_EPIC"


def test_merge_with_all_checks_green_proceeds(tmp_state_dir, fake_github):
    fake_github.add_pr().checks = [
        ci_check(),
        CheckInfo(name="docs", state="COMPLETED", conclusion="SKIPPED"),
        CheckInfo(name="legacy", state="SUCCESS"),
    ]
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "UPDATE_EPIC" and len(fake_github.merges) == 1


@pytest.mark.parametrize("status", ["BLOCKED", "BEHIND", "DIRTY", "UNSTABLE", "DRAFT", "NEW"])
def test_merge_blocks_on_non_clean_merge_state_status(tmp_state_dir, fake_github, status):
    fake_github.add_pr().merge_state_status = status
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "BLOCKED" and f"mergeStateStatus={status}" in eng.state.block_reason
    assert fake_github.merges == []


@pytest.mark.parametrize("status", ["", "UNKNOWN"])
def test_merge_inconclusive_merge_state_status_stays_in_merge(tmp_state_dir, fake_github, status):
    fake_github.add_pr().merge_state_status = status
    eng = _in_merge(tmp_state_dir, fake_github)
    with pytest.raises(VerificationError, match="mergeStateStatus"):
        eng.step(allow_merge=True)
    assert eng.state.phase == Phase.MERGE and fake_github.merges == []


def test_merge_accepts_has_hooks_status(tmp_state_dir, fake_github):
    fake_github.add_pr().merge_state_status = "HAS_HOOKS"
    _, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "UPDATE_EPIC" and len(fake_github.merges) == 1


def test_merge_blocks_on_draft_pr(tmp_state_dir, fake_github):
    fake_github.add_pr().is_draft = True
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "BLOCKED" and "draft" in eng.state.block_reason
    assert fake_github.merges == []


# -- MERGE: the PR may not redefine the checks the gate trusts (PR #38 review R2-F1) ---------
# GitHub runs the *PR's* copy of `.github/workflows/` and reports it under the
# same check name, so "every check on the PR succeeded" says nothing about a PR
# that rewrites those workflows. Such a PR is never merged unattended.
WORKFLOW_PATH = ".github/workflows/ci.yml"


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
def test_merge_blocks_when_the_pr_changes_the_workflow_defining_its_checks(
    tmp_state_dir, fake_github, phase
):
    fake_github.add_pr().checks = [ci_check()]
    fake_github.changed_files[PR] = ["README.md", WORKFLOW_PATH]
    eng = _in_merge(tmp_state_dir, fake_github, phase=phase)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED"
    assert WORKFLOW_PATH in eng.state.block_reason
    assert "README.md" not in eng.state.block_reason  # only the protected paths are named
    assert fake_github.merges == [] and eng.state.counted_merged_prs == []


def test_merge_proceeds_when_the_pr_touches_no_protected_path(tmp_state_dir, fake_github):
    fake_github.add_pr()
    # `.github/` itself is not protected -- only the workflow definitions under it.
    fake_github.changed_files[PR] = ["src/autoforge/engine.py", ".github/ISSUE_TEMPLATE.md"]
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "UPDATE_EPIC" and len(fake_github.merges) == 1
    assert ("get_pr_changed_files", PR) in fake_github.calls


@pytest.mark.parametrize(
    ("previous", "path"),
    [
        (WORKFLOW_PATH, "docs/old-ci.yml"),  # renamed *out* of the protected range
        ("docs/old-ci.yml", WORKFLOW_PATH),  # ... and into it
    ],
)
def test_merge_blocks_when_the_pr_renames_a_protected_path(
    tmp_state_dir, fake_github, previous, path
):
    """A rename is one changed file carrying both ends, never a delete plus an add.

    GitHub's GraphQL listing shows only the current name, so a PR that moves
    `.github/workflows/ci.yml` elsewhere would read as touching nothing
    protected while removing the very file that defines the check.
    """
    fake_github.add_pr().checks = [ci_check()]
    fake_github.changed_files[PR] = [ChangedFile(path=path, previous_path=previous)]
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "BLOCKED"
    assert f"{previous} -> {path}" in eng.state.block_reason  # both ends are named
    assert fake_github.merges == [] and eng.state.counted_merged_prs == []


def test_merge_proceeds_when_a_rename_touches_no_protected_path(tmp_state_dir, fake_github):
    fake_github.add_pr()
    fake_github.changed_files[PR] = [ChangedFile(path="docs/b.md", previous_path="docs/a.md")]
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "UPDATE_EPIC" and len(fake_github.merges) == 1


def test_a_rename_counts_as_one_file_against_the_truncation_check(tmp_state_dir, fake_github):
    """Two paths, one file: counting paths would hide a truncated listing."""
    fake_github.add_pr()
    fake_github.changed_files[PR] = [ChangedFile(path="docs/b.md", previous_path="docs/a.md")]
    fake_github.changed_files_total[PR] = 2
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "BLOCKED" and "1 of 2 changed files" in eng.state.block_reason
    assert fake_github.merges == []


def test_merge_blocks_on_a_protected_path_past_the_first_page(tmp_state_dir, fake_github):
    """A PR with more than one page of files is judged on all of them (issue #43).

    The protected file sits after the first 100 entries: a complete listing
    blocks for the *path*, not for a listing the controller could not read.
    """
    fake_github.add_pr().checks = [ci_check()]
    files = [f"src/pkg/module_{i}.py" for i in range(120)] + [WORKFLOW_PATH]
    fake_github.changed_files[PR] = files
    fake_github.changed_files_total[PR] = len(files)
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "BLOCKED"
    assert WORKFLOW_PATH in eng.state.block_reason
    assert "changed files for" not in eng.state.block_reason
    assert fake_github.merges == []


def test_merge_proceeds_on_a_complete_multi_page_listing_without_protected_paths(
    tmp_state_dir, fake_github
):
    fake_github.add_pr()
    files = [f"src/pkg/module_{i}.py" for i in range(250)]
    fake_github.changed_files[PR] = files
    fake_github.changed_files_total[PR] = len(files)
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "UPDATE_EPIC" and len(fake_github.merges) == 1


def test_merge_blocks_when_the_changed_file_listing_may_be_truncated(tmp_state_dir, fake_github):
    """A listing shorter than GitHub's own count cannot prove absence."""
    fake_github.add_pr()
    fake_github.changed_files[PR] = ["src/autoforge/engine.py"]
    fake_github.changed_files_total[PR] = 137
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "BLOCKED" and "1 of 137 changed files" in eng.state.block_reason
    assert fake_github.merges == []


def test_empty_protected_merge_paths_disables_the_gate_and_reads_nothing(
    tmp_state_dir, fake_github
):
    fake_github.add_pr()
    fake_github.changed_files[PR] = [WORKFLOW_PATH]
    eng = _in_merge(tmp_state_dir, fake_github)
    eng.config.safety.protected_merge_paths = []
    out = eng.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC" and len(fake_github.merges) == 1
    assert not any(c[0] == "get_pr_changed_files" for c in fake_github.calls)


def test_protected_path_gate_runs_before_check_state_is_consulted(tmp_state_dir, fake_github):
    """A still-running check would only park the run; the redefinition is conclusive."""
    fake_github.add_pr().checks = [ci_check(state="IN_PROGRESS", conclusion="")]
    fake_github.changed_files[PR] = [WORKFLOW_PATH]
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "BLOCKED" and WORKFLOW_PATH in eng.state.block_reason


# -- MERGE: the definition behind a green required check (#42, option 2) -------------------
def _actions_calls(gh):
    return [
        c
        for c in gh.calls
        if c[0]
        in (
            "get_workflow_run",
            "get_workflow_run_jobs",
            "get_branch_head_sha",
            "find_workflow_runs",
        )
    ]


def test_merge_compares_the_pr_run_with_the_base_branch_run(tmp_state_dir, fake_github):
    """The green `ci` is attributed to its run, and that run's shape to the base branch's."""
    fake_github.add_pr()
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "UPDATE_EPIC" and len(fake_github.merges) == 1
    assert _actions_calls(fake_github) == [
        ("get_workflow_run", "owner/repo", CI_RUN_ID),
        ("get_workflow_run_jobs", "owner/repo", CI_RUN_ID),
        ("get_branch_head_sha", "owner/repo", "main"),
        ("find_workflow_runs", "owner/repo", CI_WORKFLOW_ID, "main", "push", MAIN_SHA),
        ("get_workflow_run_jobs", "owner/repo", BASE_RUN_ID),
    ]


def _with_step(jobs, job, step, new):
    """``jobs`` with one step of one job renamed (a `run:` whose command changed)."""
    return WorkflowRunJobs(
        jobs=tuple(
            WorkflowJob(j.name, tuple(new if s == step else s for s in j.steps))
            if j.name == job
            else j
            for j in jobs.jobs
        ),
        total=jobs.total,
    )


def test_merge_blocks_when_the_pr_run_differs_from_the_base_branch_run(tmp_state_dir, fake_github):
    """A `ci` produced by a redefined workflow is a named difference, not a green check."""
    fake_github.add_pr()
    fake_github.workflow_jobs[CI_RUN_ID] = _with_step(
        ci_jobs(), "test (3.11)", "Run pytest", "Run pytest -k smoke"
    )
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "BLOCKED" and fake_github.merges == []
    reason = eng.state.block_reason
    assert f"workflow run {CI_RUN_ID} behind required check 'ci'" in reason
    assert f"base branch's own run {BASE_RUN_ID} of .github/workflows/ci.yml" in reason
    assert "job 'test (3.11)' step 3 is 'Run pytest -k smoke'" in reason
    assert "safety.verify_check_definition" in reason


def test_merge_blocks_when_the_pr_run_lost_a_job(tmp_state_dir, fake_github):
    kept = ci_jobs()
    fake_github.workflow_jobs[CI_RUN_ID] = WorkflowRunJobs(
        jobs=tuple(j for j in kept.jobs if j.name != "lint"), total=kept.total - 1
    )
    fake_github.add_pr()
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "BLOCKED"
    assert "job 'lint' of the base branch's run is missing" in eng.state.block_reason


def test_definition_gate_ignores_job_order(tmp_state_dir, fake_github):
    kept = ci_jobs()
    fake_github.workflow_jobs[CI_RUN_ID] = WorkflowRunJobs(
        jobs=tuple(reversed(kept.jobs)), total=kept.total
    )
    fake_github.add_pr()
    _, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "UPDATE_EPIC"


@pytest.mark.parametrize(
    ("prepare", "expected"),
    [
        (lambda gh: setattr(gh.prs[PR], "checks", []), "appears 0 times"),
        (lambda gh: setattr(gh.prs[PR], "checks", [ci_check(), ci_check()]), "appears 2 times"),
        (
            lambda gh: setattr(gh.prs[PR], "checks", [CheckInfo("ci", "COMPLETED", "SUCCESS")]),
            "not a GitHub Actions check run",
        ),
        (
            lambda gh: setattr(
                gh.prs[PR],
                "checks",
                [replace(ci_check(), details_url="https://ci.example/build/9")],
            ),
            "not a GitHub Actions check run (details URL: https://ci.example/build/9)",
        ),
        (
            lambda gh: setattr(gh.prs[PR], "checks", [ci_check(repo="other/repo")]),
            "workflow run in other/repo, not in owner/repo",
        ),
        (
            lambda gh: gh.workflow_runs.__setitem__(
                CI_RUN_ID, replace(gh.workflow_runs[CI_RUN_ID], head_sha=SHA_B)
            ),
            f"ran at {SHA_B[:12]}, not at the reviewed HEAD {SHA_A[:12]}",
        ),
        (
            lambda gh: gh.workflow_jobs.__setitem__(
                CI_RUN_ID, replace(ci_jobs(), total=ci_jobs().total + 1)
            ),
            "returned 4 of 5 jobs",
        ),
        (lambda gh: gh.branch_heads.__setitem__("main", SHA_C), "has no push run"),
        (
            lambda gh: setattr(gh, "workflow_runs_unlisted", 100),
            "returned 1 of 101 push runs of .github/workflows/ci.yml on base branch 'main'",
        ),
        (
            lambda gh: gh.workflow_runs.__setitem__(
                BASE_RUN_ID, replace(gh.workflow_runs[BASE_RUN_ID], conclusion="failure")
            ),
            "concluded 'failure', not success",
        ),
        (
            lambda gh: gh.workflow_jobs.__setitem__(
                BASE_RUN_ID, replace(ci_jobs(), total=ci_jobs().total + 2)
            ),
            "returned 4 of 6 jobs of the base branch's run",
        ),
        (lambda gh: setattr(gh.prs[PR], "base_ref", ""), "reports no base branch"),
    ],
)
def test_definition_gate_conclusive_shortfalls_block(tmp_state_dir, fake_github, prepare, expected):
    fake_github.add_pr()
    prepare(fake_github)
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "BLOCKED", eng.state.block_reason
    assert expected in eng.state.block_reason and fake_github.merges == []


def test_definition_gate_base_run_still_running_is_inconclusive(tmp_state_dir, fake_github):
    fake_github.add_pr()
    base = fake_github.workflow_runs[BASE_RUN_ID]
    fake_github.workflow_runs[BASE_RUN_ID] = replace(base, status="in_progress", conclusion="")
    eng = _in_merge(tmp_state_dir, fake_github)
    with pytest.raises(VerificationError, match="still in_progress"):
        eng.step(allow_merge=True)
    assert eng.state.phase == Phase.MERGE and eng.state.attempt == 1
    fake_github.workflow_runs[BASE_RUN_ID] = base
    assert eng.step(allow_merge=True).next_phase == "UPDATE_EPIC"


def test_definition_gate_pr_run_not_completed_is_inconclusive(tmp_state_dir, fake_github):
    """The rollup says COMPLETED but the run object does not: re-read, never guessed."""
    fake_github.add_pr()
    run = fake_github.workflow_runs[CI_RUN_ID]
    fake_github.workflow_runs[CI_RUN_ID] = replace(run, status="in_progress", conclusion="")
    eng = _in_merge(tmp_state_dir, fake_github)
    with pytest.raises(VerificationError, match="is in_progress, not completed"):
        eng.step(allow_merge=True)
    assert eng.state.phase == Phase.MERGE and fake_github.merges == []


def test_definition_gate_transient_read_failure_is_inconclusive(tmp_state_dir, fake_github):
    fake_github.add_pr()
    fake_github.actions_error = GitHubUnavailableError("HTTP 502: Bad Gateway")
    eng = _in_merge(tmp_state_dir, fake_github)
    with pytest.raises(VerificationError, match="GitHub unavailable") as info:
        eng.step(allow_merge=True)
    assert "workflow run 1001 behind required check 'ci' could not be read" in str(info.value)
    assert eng.state.phase == Phase.MERGE and eng.state.attempt == 1
    fake_github.actions_error = ""
    assert eng.step(allow_merge=True).next_phase == "UPDATE_EPIC"


def test_definition_gate_conclusive_read_failure_blocks(tmp_state_dir, fake_github):
    fake_github.add_pr()
    fake_github.actions_error = "HTTP 403: Resource not accessible by integration"
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "BLOCKED" and "HTTP 403" in eng.state.block_reason
    assert fake_github.merges == []


def test_definition_gate_disabled_reads_nothing(tmp_state_dir, fake_github):
    fake_github.add_pr()
    fake_github.workflow_jobs[CI_RUN_ID] = WorkflowRunJobs(jobs=(), total=0)  # would block
    eng = _in_merge(tmp_state_dir, fake_github)
    eng.config.safety.verify_check_definition = False
    out = eng.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC" and _actions_calls(fake_github) == []


def test_definition_gate_without_required_checks_reads_nothing(tmp_state_dir, fake_github):
    fake_github.add_pr()
    fake_github.workflow_jobs[CI_RUN_ID] = WorkflowRunJobs(jobs=(), total=0)
    eng = _in_merge(tmp_state_dir, fake_github)
    eng.config.safety.required_checks = []
    out = eng.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC" and _actions_calls(fake_github) == []


def test_definition_gate_only_covers_required_checks(tmp_state_dir, fake_github):
    """An extra green check without an Actions run is still "every check succeeded"."""
    fake_github.add_pr().checks = [ci_check(), CheckInfo("legacy", "SUCCESS")]
    _, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "UPDATE_EPIC"


def test_definition_gate_reads_one_reference_per_workflow(tmp_state_dir, fake_github):
    fake_github.add_pr().checks = [ci_check(), ci_check(name="lint")]
    eng = _in_merge(tmp_state_dir, fake_github)
    eng.config.safety.required_checks = ["ci", "lint"]
    assert eng.step(allow_merge=True).next_phase == "UPDATE_EPIC"
    calls = _actions_calls(fake_github)
    assert calls.count(("get_workflow_run_jobs", "owner/repo", BASE_RUN_ID)) == 1
    assert calls.count(("get_workflow_run", "owner/repo", CI_RUN_ID)) == 2


def test_definition_gate_runs_after_check_state_and_before_mergeability(tmp_state_dir, fake_github):
    # a failing check is conclusive on its own: no Actions read is made
    fake_github.add_pr().checks = [ci_check(conclusion="FAILURE")]
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "BLOCKED" and _actions_calls(fake_github) == []
    # a redefinition is conclusive even while mergeability is still UNKNOWN
    gh = FakeGitHub()
    gh.add_pr().mergeable = "UNKNOWN"
    gh.workflow_jobs[CI_RUN_ID] = WorkflowRunJobs(jobs=(), total=0)
    eng, out = _park_and_step(tmp_state_dir / "b", gh)
    assert out.next_phase == "BLOCKED" and "does not match" in eng.state.block_reason


def test_ready_for_merge_runs_the_definition_gate_too(tmp_state_dir, fake_github):
    fake_github.add_pr()
    fake_github.workflow_jobs[CI_RUN_ID] = WorkflowRunJobs(jobs=(), total=0)
    eng = _in_ready(tmp_state_dir, fake_github)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED" and "does not match" in eng.state.block_reason


# -- MERGE: merge.verification_commands on the exported reviewed HEAD (#42, option 1) --------
def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo, name, content, message="c"):
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


_RECORD_CWD = (
    "import os, pathlib, sys; "
    "pathlib.Path(sys.argv[1]).open('a').write(os.getcwd() + '\\n'); "
    "sys.exit(0 if pathlib.Path('proof.txt').read_text() == sys.argv[2] else 3)"
)


def _record_cwd(marker, expected="v1\n"):
    """A verification command proving *where* it ran and *which* content it saw."""
    return ["python3", "-c", _RECORD_CWD, str(marker), expected]


def _in_merge_on_commit(tmp_state_dir, gh, commands, phase=Phase.MERGE, content="v1\n"):
    """Engine parked with the gate open on a PR whose HEAD is a real local commit."""
    repo = git_repo(tmp_state_dir.parent)
    sha = _commit(repo, "proof.txt", content)
    _commit(repo, "later.txt", "not the reviewed commit\n")  # HEAD moved on; the PR did not
    gh.add_pr(head_sha=sha)
    eng = _in_merge(tmp_state_dir, gh, reviewed=sha, phase=phase)
    eng.config.merge.verification_commands = commands
    return eng, sha


def test_verification_commands_run_in_an_export_of_the_reviewed_head(tmp_state_dir, fake_github):
    marker = tmp_state_dir.parent / "cwd.txt"
    eng, sha = _in_merge_on_commit(tmp_state_dir, fake_github, [_record_cwd(marker)])
    out = eng.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC" and len(fake_github.merges) == 1
    cwds = marker.read_text().splitlines()
    assert len(cwds) == 1
    exported = cwds[0]
    assert not exported.startswith(str(tmp_state_dir.parent))  # never the operator's checkout
    assert "autoforge-premerge-" in exported
    assert not os.path.exists(exported)  # deleted afterwards
    assert eng.state.premerge_verified_head_sha == sha
    assert eng.state.premerge_verified_commands == [_record_cwd(marker)]
    # The operator's checkout was not touched: no worktree, no branch, HEAD where it was.
    assert _git(tmp_state_dir.parent, "worktree", "list").count("\n") == 0
    assert _git(tmp_state_dir.parent, "branch", "--list").count("\n") == 0
    assert _git(tmp_state_dir.parent, "rev-parse", "HEAD") != sha
    # ... and the run is journaled like a validation command.
    run_dir = eng.paths.logs_dir / eng.state.run_id
    steps = [p.name for p in run_dir.iterdir() if p.is_dir()]
    assert any("merge-premerge-verification" in name for name in steps)


def test_failing_verification_command_blocks_the_merge(tmp_state_dir, fake_github):
    marker = tmp_state_dir.parent / "cwd.txt"
    commands = [_record_cwd(marker), ["python3", "-c", "import sys; sys.exit(1)"]]
    eng, sha = _in_merge_on_commit(tmp_state_dir, fake_github, commands, content="v2\n")
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED" and fake_github.merges == []
    reason = eng.state.block_reason
    assert "pre-merge verification command" in reason and "failed with exit 3" in reason
    assert f"reviewed HEAD {sha[:12]}" in reason and "not corroborated locally" in reason
    assert len(marker.read_text().splitlines()) == 1  # the second command never ran
    assert eng.state.premerge_verified_head_sha == ""


def test_verification_command_timeout_blocks_the_merge(tmp_state_dir, fake_github):
    commands = [["python3", "-c", "import time; time.sleep(30)"]]
    eng, _ = _in_merge_on_commit(tmp_state_dir, fake_github, commands)
    eng.config.execution.command_timeout_seconds = 1
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED" and "timed out after 1s" in eng.state.block_reason
    assert fake_github.merges == []


# A verification command that exits at once (0, or the status given as its
# argument) but leaves a descendant holding its stdout/stderr open for far
# longer than the timeout.
_LEAVES_A_SERVER = (
    "import subprocess, sys; "
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
    "print('checks ran'); "
    "sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 else 0)"
)


def test_verification_command_leftover_is_killed_and_its_result_kept(
    tmp_state_dir, fake_github, monkeypatch
):
    """#85: a leftover holding the pipes after a clean exit used to cost the
    whole timeout and turn the exit into a timeout. The executor now kills
    it after the exit grace and returns the command's own exit status, so
    the merge proceeds, and the journal records what was left behind."""
    from autoforge import executor

    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 0.5)
    commands = [["python3", "-c", _LEAVES_A_SERVER]]
    eng, sha = _in_merge_on_commit(tmp_state_dir, fake_github, commands)
    eng.config.execution.command_timeout_seconds = 30
    out = eng.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC" and len(fake_github.merges) == 1
    assert eng.state.premerge_verified_head_sha == sha
    run_dir = eng.paths.logs_dir / eng.state.run_id
    step = next(p for p in run_dir.iterdir() if "premerge-verification" in p.name)
    execution = json.loads((step / "execution.json").read_text(encoding="utf-8"))
    assert execution["exit_code"] == 0 and execution["timed_out"] is False
    assert execution["descendants_killed"] is True
    assert execution["group_survived_kill"] is False
    assert execution["capture_abandoned"] is False
    event = json.loads((run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert event["descendants_killed"] is True and event["timed_out"] is False
    assert not (step / "error.txt").exists()


# A verification command that starts a detached process (its own session,
# none of the command's pipes) and exits at once.
_DETACHES_A_SERVER = (
    "import subprocess, sys; "
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
    "start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
    "stderr=subprocess.DEVNULL); "
    "print('checks ran')"
)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only subreaper")
def test_verification_command_detached_leftover_is_killed_and_journaled(
    tmp_state_dir, fake_github, monkeypatch
):
    """#132: a repository-defined command is contained like an agent (ADR
    0002 §4b). A process it detached from its group is killed after the exit
    grace, the merge proceeds, and the journal names the orphan."""
    from autoforge import executor

    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 0.5)
    commands = [["python3", "-c", _DETACHES_A_SERVER]]
    eng, _ = _in_merge_on_commit(tmp_state_dir, fake_github, commands)
    eng.config.execution.command_timeout_seconds = 30
    out = eng.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC" and len(fake_github.merges) == 1
    run_dir = eng.paths.logs_dir / eng.state.run_id
    step = next(p for p in run_dir.iterdir() if "premerge-verification" in p.name)
    execution = json.loads((step / "execution.json").read_text(encoding="utf-8"))
    assert execution["exit_code"] == 0 and execution["orphans_killed"] is True
    assert execution["descendants_killed"] is False
    assert execution["orphan_survived_kill"] is False and execution["orphans_unchecked"] is False
    event = json.loads((run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert event["orphans_killed"] is True


def test_failing_verification_command_names_its_leftover_in_the_block_reason(
    tmp_state_dir, fake_github, monkeypatch
):
    """PR #114 R1-F1: when the command fails *and* left a process behind,
    the durable block reason names both, not only the exit status; the
    output tail still follows so the reason reads like the clean case."""
    from autoforge import executor

    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 0.5)
    commands = [["python3", "-c", _LEAVES_A_SERVER, "3"]]
    eng, sha = _in_merge_on_commit(tmp_state_dir, fake_github, commands)
    eng.config.execution.command_timeout_seconds = 30
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED" and fake_github.merges == []
    reason = eng.state.block_reason
    assert "pre-merge verification command" in reason and "failed with exit 3" in reason
    assert f"reviewed HEAD {sha[:12]}" in reason
    assert "not corroborated locally (the child exited but left processes behind" in reason, reason
    assert "so the group was killed). Output tail: checks ran" in reason
    assert eng.state.premerge_verified_head_sha == ""
    run_dir = eng.paths.logs_dir / eng.state.run_id
    step = next(p for p in run_dir.iterdir() if "premerge-verification" in p.name)
    execution = json.loads((step / "execution.json").read_text(encoding="utf-8"))
    assert execution["exit_code"] == 3 and execution["descendants_killed"] is True
    assert execution["error"].startswith("exit 3 (the child exited but left processes behind")


def test_verification_commands_never_run_before_github_accepts_the_pr(tmp_state_dir, fake_github):
    """They execute the PR's code, so every GitHub-side fact is checked first."""
    marker = tmp_state_dir.parent / "cwd.txt"
    eng, _ = _in_merge_on_commit(tmp_state_dir, fake_github, [_record_cwd(marker)])
    fake_github.prs[PR].checks = [ci_check(conclusion="FAILURE")]
    assert eng.step(allow_merge=True).next_phase == "BLOCKED"
    assert not marker.exists()


def test_verification_commands_do_not_run_in_dry_run(tmp_state_dir, fake_github):
    marker = tmp_state_dir.parent / "cwd.txt"
    eng, _ = _in_merge_on_commit(tmp_state_dir, fake_github, [_record_cwd(marker)])
    plan = eng.step(dry_run=True).plan
    assert any("merge.verification_commands" in n and "python3 -c" in n for n in plan.notes)
    assert any("safety.verify_check_definition" in n for n in plan.notes)
    assert not marker.exists() and fake_github.merges == []


def test_dry_run_names_the_absence_of_verification_commands(tmp_state_dir, fake_github):
    fake_github.add_pr()
    eng = _in_ready(tmp_state_dir, fake_github)
    plan = eng.step(dry_run=True).plan
    assert any("no merge.verification_commands configured" in n for n in plan.notes)


def test_verification_pass_is_not_repeated_for_the_same_head(tmp_state_dir, fake_github):
    marker = tmp_state_dir.parent / "cwd.txt"
    eng, sha = _in_merge_on_commit(
        tmp_state_dir, fake_github, [_record_cwd(marker)], phase=Phase.READY_FOR_MERGE
    )
    assert eng.step(allow_merge=True).next_phase == "MERGE"
    assert load_state(eng.paths.state_file).premerge_verified_head_sha == sha
    assert eng.step(allow_merge=True).next_phase == "UPDATE_EPIC"
    assert len(marker.read_text().splitlines()) == 1  # READY_FOR_MERGE's pass carried over


def test_verification_reruns_when_the_command_list_changes(tmp_state_dir, fake_github):
    marker = tmp_state_dir.parent / "cwd.txt"
    eng, sha = _in_merge_on_commit(
        tmp_state_dir, fake_github, [_record_cwd(marker)], phase=Phase.READY_FOR_MERGE
    )
    assert eng.step(allow_merge=True).next_phase == "MERGE"
    eng.config.merge.verification_commands = [_record_cwd(marker), ["true"]]
    assert eng.step(allow_merge=True).next_phase == "UPDATE_EPIC"
    assert len(marker.read_text().splitlines()) == 2


def test_verification_pass_does_not_survive_a_new_issue(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, [], github=fake_github)
    eng.state.premerge_verified_head_sha = SHA_A
    eng.state.premerge_verified_commands = [["true"]]
    eng.state.reset_for_new_issue(ISSUE3)
    assert eng.state.premerge_verified_head_sha == ""
    assert eng.state.premerge_verified_commands == []


def test_unreachable_reviewed_head_is_inconclusive_not_blocked(tmp_state_dir, fake_github):
    """No local commit and a remote that does not have it: bounded re-check, the code is
    never guessed at."""
    git_repo(tmp_state_dir.parent)
    empty = tmp_state_dir.parent / "empty.git"
    _git(tmp_state_dir.parent, "init", "-q", "--bare", str(empty))
    fake_github.add_pr(head_sha=SHA_A)
    eng = _in_merge(tmp_state_dir, fake_github)
    eng._git_remote = GitRemote(url=f"file://{empty}")
    eng.config.github.command = _fake_gh(tmp_state_dir.parent)
    eng.config.merge.verification_commands = [["true"]]
    with pytest.raises(VerificationError, match="not in the local repository") as info:
        eng.step(allow_merge=True)
    assert "refs/pull/42/head" in str(info.value)
    assert eng.state.phase == Phase.MERGE and eng.state.attempt == 1
    assert fake_github.merges == []


def test_reviewed_head_is_fetched_from_the_pull_ref_when_not_local(tmp_state_dir, fake_github):
    """The controller's own transport (ADR 0004 D7.1) fetches ``refs/pull/<n>/head`` from
    an explicit URL into the shared object store; the checkout's ``origin`` (here a remote
    that does not have the commit) is never consulted and no ref is created."""
    origin = git_repo(tmp_state_dir.parent / "origin")
    base = _commit(origin, "base.txt", "base\n")
    sha = _commit(origin, "proof.txt", "v1\n")
    _git(origin, "update-ref", "refs/pull/42/head", sha)
    _git(origin, "reset", "-q", "--hard", base)
    local = git_repo(tmp_state_dir.parent)
    decoy = tmp_state_dir.parent / "decoy.git"
    _git(local, "init", "-q", "--bare", str(decoy))
    _git(local, "remote", "add", "origin", str(decoy))
    marker = tmp_state_dir.parent / "cwd.txt"
    fake_github.add_pr(head_sha=sha)
    eng = _in_merge(tmp_state_dir, fake_github, reviewed=sha)
    eng._git_remote = GitRemote(url=f"file://{origin}")
    eng.config.github.command = _fake_gh(tmp_state_dir.parent)
    eng.config.merge.verification_commands = [_record_cwd(marker)]
    assert eng.step(allow_merge=True).next_phase == "UPDATE_EPIC"
    assert len(marker.read_text().splitlines()) == 1
    assert "refs/pull" not in _git(local, "for-each-ref", "--format=%(refname)")


def _fake_gh(where) -> str:
    """An executable ``gh`` stand-in: a ``file://`` remote never asks it for a credential,
    but the transport requires one to exist before any network git runs."""
    gh = where / "fake-gh"
    gh.write_text("#!/bin/sh\nexit 1\n")
    gh.chmod(0o755)
    return str(gh)


def test_no_verification_commands_means_no_git_plumbing(tmp_state_dir, fake_github):
    fake_github.add_pr()
    eng = _in_merge(tmp_state_dir, fake_github)
    seen = []

    def runner(req):
        seen.append(req.command)
        return ExecutionResult(req.command, req.cwd, 0, "", "", "t", "t")

    eng._runner = runner
    assert eng.step(allow_merge=True).next_phase == "UPDATE_EPIC"
    assert seen == []


# -- MERGE: asynchronous merge paths (PR #24 review R1-F2) -----------------------------------
def test_merge_blocks_when_auto_merge_already_armed(tmp_state_dir, fake_github):
    fake_github.add_pr().auto_merge_enabled = True
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "BLOCKED" and "auto-merge armed" in eng.state.block_reason
    assert fake_github.merges == [] and fake_github.disabled_auto == []


def test_merge_blocks_when_base_branch_requires_merge_queue(tmp_state_dir, fake_github):
    """`gh pr merge` would enqueue / arm auto-merge instead of merging: never start it."""
    fake_github.add_pr()
    fake_github.merge_queue[PR] = MergeQueueStatus(enabled=True, in_queue=False)
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "BLOCKED" and "merge queue" in eng.state.block_reason
    assert fake_github.merges == []
    assert ("get_pr_merge_queue_status", PR) in fake_github.calls


def test_merge_blocks_when_pr_already_in_merge_queue(tmp_state_dir, fake_github):
    fake_github.add_pr()
    fake_github.merge_queue[PR] = MergeQueueStatus(enabled=True, in_queue=True)
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "BLOCKED" and "already in a merge queue" in eng.state.block_reason
    assert fake_github.merges == []


def test_merge_queue_status_transient_read_failure_stays_in_merge(tmp_state_dir, fake_github):
    """A 502 on the merge-queue read is inconclusive: bounded re-check, not a raw GitHubError."""
    fake_github.add_pr()
    fake_github.merge_queue_error = GitHubUnavailableError("`gh api graphql` failed (exit 1): 502")
    eng = _in_merge(tmp_state_dir, fake_github)
    with pytest.raises(VerificationError, match="502.*attempt 1/") as info:
        eng.step(allow_merge=True)
    assert isinstance(info.value.__cause__, GitHubUnavailableError)
    assert eng.state.phase == Phase.MERGE and eng.state.attempt == 1
    assert load_state(eng.paths.state_file).attempt == 1
    assert fake_github.merges == []


def test_merge_exit_zero_leaving_pr_open_disarms_auto_merge(tmp_state_dir, fake_github):
    """Defense in depth: if `gh pr merge` armed auto-merge, the controller disarms it."""
    fake_github.add_pr()
    fake_github.merge_leaves_open = True
    fake_github.merge_arms_auto = True
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "BLOCKED"
    assert fake_github.disabled_auto == [PR]
    assert fake_github.prs[PR].auto_merge_enabled is False
    assert "has been disabled again" in eng.state.block_reason
    assert eng.state.counted_merged_prs == []


def test_merge_exit_zero_leaving_pr_open_disarm_failure_is_loud(tmp_state_dir, fake_github):
    fake_github.add_pr()
    fake_github.merge_leaves_open = True
    fake_github.merge_arms_auto = True
    fake_github.disable_auto_error = "`gh pr merge --disable-auto` failed (exit 1): forbidden"
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "BLOCKED"
    assert "could not be disabled" in eng.state.block_reason
    assert "disable it on GitHub immediately" in eng.state.block_reason
    assert eng.state.counted_merged_prs == []


def test_merge_exit_zero_leaving_pr_open_without_auto_merge_does_not_disarm(
    tmp_state_dir, fake_github
):
    fake_github.add_pr()
    fake_github.merge_leaves_open = True
    eng, out = _park_and_step(tmp_state_dir, fake_github)
    assert out.next_phase == "BLOCKED" and fake_github.disabled_auto == []
    assert "resume" not in out.message  # BLOCKED is terminal: never point at `resume`


# -- MERGE: uncertain post-write outcome is recoverable (PR #24 review R1-F3) ----------------
def test_merge_post_write_read_failure_stays_in_merge_and_resume_reconciles(
    tmp_state_dir, fake_github
):
    """`gh pr merge` succeeded but the re-read failed: do not terminalise; resume counts once."""
    fake_github.add_pr()
    fake_github.get_pr_failures = 0
    eng = _in_merge(tmp_state_dir, fake_github)

    # First get_pr (pre-checks) succeeds, the post-merge re-read fails.
    orig_merge = fake_github.merge_pr

    def merge_then_break(*a, **kw):
        orig_merge(*a, **kw)
        fake_github.get_pr_failures = 1

    fake_github.merge_pr = merge_then_break
    with pytest.raises(VerificationError, match="outcome unknown"):
        eng.step(allow_merge=True)
    assert eng.state.phase == Phase.MERGE and eng.state.attempt == 1
    assert eng.state.counted_merged_prs == []
    persisted = load_state(eng.paths.state_file)
    assert persisted.phase == Phase.MERGE

    # resume: GitHub says MERGED at the reviewed HEAD -> recovered, counted exactly once
    eng2 = _in_merge(tmp_state_dir, fake_github)
    out = eng2.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC" and "recovered" in out.message
    assert len(fake_github.merges) == 1  # no second gh pr merge
    assert eng2.state.counted_merged_prs == [PR] and eng2.state.merged_since_epic_update == 1


def test_merge_failed_and_read_failed_stays_in_merge(tmp_state_dir, fake_github):
    fake_github.add_pr()
    fake_github.merge_error = "`gh pr merge` failed (exit 1): 503 upstream"
    orig_merge = fake_github.merge_pr

    def fail_then_break(*a, **kw):
        fake_github.get_pr_failures = 1
        orig_merge(*a, **kw)

    fake_github.merge_pr = fail_then_break
    eng = _in_merge(tmp_state_dir, fake_github)
    with pytest.raises(VerificationError, match="503 upstream"):
        eng.step(allow_merge=True)
    assert eng.state.phase == Phase.MERGE and eng.state.counted_merged_prs == []
    # resume with GitHub reachable again: PR still OPEN at the reviewed HEAD -> re-verified,
    # re-attempted; the fake now merges.
    fake_github.merge_error = ""
    fake_github.merge_pr = orig_merge
    out = eng.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC" and eng.state.counted_merged_prs == [PR]


# -- READY_FOR_MERGE / MERGE: controller-side GitHub verification (issue #3) -----------------
def _in_ready(tmp_state_dir, gh, reviewed=SHA_A, clean=True):
    """Engine parked in READY_FOR_MERGE with the gate open and a clean review on ``reviewed``."""
    return _in_merge(tmp_state_dir, gh, reviewed=reviewed, clean=clean, phase=Phase.READY_FOR_MERGE)


def test_ready_for_merge_verifies_on_github_before_entering_merge(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_A)
    eng = _in_ready(tmp_state_dir, fake_github)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "MERGE" and "GitHub confirms" in out.message
    assert ("get_pr", PR) in fake_github.calls
    assert ("get_pr_merge_queue_status", PR) in fake_github.calls
    assert fake_github.merges == []  # READY_FOR_MERGE never writes
    assert load_state(eng.paths.state_file).phase == Phase.MERGE


def test_ready_for_merge_gate_closed_reads_nothing(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_A)
    eng = _in_ready(tmp_state_dir, fake_github)
    before = list(fake_github.calls)
    with pytest.raises(VerificationError, match="merge is disabled"):
        eng.step(allow_merge=False)
    assert fake_github.calls == before and eng.state.phase == Phase.READY_FOR_MERGE


def test_ready_for_merge_requires_clean_review_in_state(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_A)
    eng = _in_ready(tmp_state_dir, fake_github, clean=False)
    with pytest.raises(VerificationError, match="clean review"):
        eng.step(allow_merge=True)
    assert eng.state.phase == Phase.READY_FOR_MERGE


def test_ready_for_merge_closed_pr_blocks(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_A, state="CLOSED")
    eng = _in_ready(tmp_state_dir, fake_github)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED" and "CLOSED" in eng.state.block_reason
    assert fake_github.merges == []


def test_ready_for_merge_already_merged_pr_routes_to_merge_for_reconciliation(
    tmp_state_dir, fake_github
):
    """Someone merged the PR while the run was holding: MERGE reconciles and counts once."""
    fake_github.add_pr(head_sha=SHA_A, state="MERGED")
    eng = _in_ready(tmp_state_dir, fake_github)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "MERGE" and "already MERGED" in out.message
    assert eng.state.counted_merged_prs == []  # counting belongs to MERGE
    out2 = eng.step(allow_merge=True)
    assert out2.next_phase == "UPDATE_EPIC" and "recovered" in out2.message
    assert fake_github.merges == [] and eng.state.counted_merged_prs == [PR]


def test_ready_for_merge_conflicting_pr_blocks_and_never_enters_merge(tmp_state_dir, fake_github):
    pr = fake_github.add_pr(head_sha=SHA_A)
    pr.mergeable = "CONFLICTING"
    pr.checks = [ci_check(conclusion="FAILURE")]
    eng = _in_ready(tmp_state_dir, fake_github)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED"
    assert "ci" in eng.state.block_reason  # checks are reported first
    assert fake_github.merges == [] and eng.state.counted_merged_prs == []
    assert load_state(eng.paths.state_file).phase == Phase.BLOCKED


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda pr: setattr(pr, "mergeable", "CONFLICTING"), "CONFLICTING"),
        (lambda pr: setattr(pr, "is_draft", True), "draft"),
        (lambda pr: setattr(pr, "merge_state_status", "BLOCKED"), "mergeStateStatus=BLOCKED"),
        (lambda pr: setattr(pr, "auto_merge_enabled", True), "auto-merge armed"),
        (
            lambda pr: setattr(pr, "checks", [ci_check(conclusion="FAILURE")]),
            "failing or inconclusive checks: ci",
        ),
    ],
)
def test_ready_for_merge_conclusive_negatives_block(tmp_state_dir, fake_github, mutate, needle):
    mutate(fake_github.add_pr(head_sha=SHA_A))
    eng = _in_ready(tmp_state_dir, fake_github)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED" and needle in eng.state.block_reason
    assert fake_github.merges == []


def test_ready_for_merge_merge_queue_blocks(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_A)
    fake_github.merge_queue[PR] = MergeQueueStatus(enabled=True, in_queue=False)
    eng = _in_ready(tmp_state_dir, fake_github)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED" and "merge queue" in eng.state.block_reason


def test_ready_for_merge_pending_checks_hold_then_proceed(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_A).checks = [ci_check(state="IN_PROGRESS", conclusion="")]
    eng = _in_ready(tmp_state_dir, fake_github)
    with pytest.raises(VerificationError, match="still running: ci.*READY_FOR_MERGE"):
        eng.step(allow_merge=True)
    assert eng.state.phase == Phase.READY_FOR_MERGE and eng.state.attempt == 1
    assert load_state(eng.paths.state_file).attempt == 1  # persisted across resume
    fake_github.prs[PR].checks = [ci_check()]
    assert eng.step(allow_merge=True).next_phase == "MERGE"
    assert eng.state.attempt == 0  # reset on transition


def test_ready_for_merge_unknown_mergeability_holds(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_A).mergeable = "UNKNOWN"
    eng = _in_ready(tmp_state_dir, fake_github)
    with pytest.raises(VerificationError, match="not determined mergeability"):
        eng.step(allow_merge=True)
    assert eng.state.phase == Phase.READY_FOR_MERGE and fake_github.merges == []


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
def test_inconclusive_verification_is_bounded_then_blocked(tmp_state_dir, fake_github, phase):
    """UNKNOWN mergeability: bounded re-checks via resume, then BLOCKED (never merged)."""
    fake_github.add_pr(head_sha=SHA_A).mergeable = "UNKNOWN"
    eng = _in_merge(tmp_state_dir, fake_github, phase=phase)
    eng.config.merge.max_verification_attempts = 3
    for n in (1, 2):
        with pytest.raises(VerificationError, match=f"attempt {n}/3"):
            eng.step(allow_merge=True)
        assert eng.state.phase == phase and eng.state.attempt == n
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED"
    assert "inconclusive for 3 verification attempt(s)" in eng.state.block_reason
    assert "max_verification_attempts=3" in eng.state.block_reason
    assert "resume" not in out.message
    assert fake_github.merges == [] and eng.state.counted_merged_prs == []


def test_run_with_gate_open_executes_ready_for_merge_verification(tmp_state_dir, fake_github):
    """run()/resume no longer treat READY_FOR_MERGE as a stop phase once the gate is open."""
    fake_github.add_pr(head_sha=SHA_A)
    eng = _in_ready(tmp_state_dir, fake_github)
    assert eng.run(max_steps=1) == []  # flag missing: config alone keeps the hold
    assert eng.state.phase == Phase.READY_FOR_MERGE and ("get_pr", PR) not in fake_github.calls
    outcomes = eng.run(max_steps=1, allow_merge=True)
    assert [o.next_phase for o in outcomes] == ["MERGE"]
    assert fake_github.merges == []  # budget of one step: verification only, no write yet
    assert load_state(eng.paths.state_file).phase == Phase.MERGE


def test_run_with_gate_closed_in_config_holds_despite_flag(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_A)
    eng = _in_ready(tmp_state_dir, fake_github)
    eng.config.safety.allow_merge = False
    assert eng.run(max_steps=5, allow_merge=True) == []
    assert eng.state.phase == Phase.READY_FOR_MERGE and fake_github.calls == []


def test_run_with_gate_open_bounds_inconclusive_ready_for_merge_rechecks(
    tmp_state_dir, fake_github
):
    """Each resume --allow-merge consumes exactly one verification attempt, then BLOCKED."""
    fake_github.add_pr(head_sha=SHA_A).mergeable = "UNKNOWN"
    eng = _in_ready(tmp_state_dir, fake_github)
    eng.config.merge.max_verification_attempts = 3
    for n in (1, 2):
        with pytest.raises(VerificationError, match=f"READY_FOR_MERGE.*attempt {n}/3"):
            eng.run(max_steps=50, allow_merge=True)
        assert eng.state.phase == Phase.READY_FOR_MERGE
        assert load_state(eng.paths.state_file).attempt == n
    outcomes = eng.run(max_steps=50, allow_merge=True)
    assert [o.next_phase for o in outcomes] == ["BLOCKED"]
    assert "inconclusive for 3 verification attempt(s)" in eng.state.block_reason
    assert fake_github.merges == [] and eng.state.counted_merged_prs == []


def test_run_with_gate_open_proceeds_once_checks_finish(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_A).checks = [ci_check(state="IN_PROGRESS", conclusion="")]
    eng = _in_ready(tmp_state_dir, fake_github)
    with pytest.raises(VerificationError, match="still running: ci"):
        eng.run(max_steps=50, allow_merge=True)
    assert eng.state.phase == Phase.READY_FOR_MERGE and eng.state.attempt == 1
    fake_github.prs[PR].checks = [ci_check()]
    outcomes = eng.run(max_steps=1, allow_merge=True)
    assert [o.next_phase for o in outcomes] == ["MERGE"] and eng.state.attempt == 0


def test_inconclusive_bound_of_one_blocks_immediately(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_A).checks = [ci_check(state="QUEUED", conclusion="")]
    eng = _in_merge(tmp_state_dir, fake_github)
    eng.config.merge.max_verification_attempts = 1
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED" and "still running: ci" in eng.state.block_reason
    assert fake_github.merges == []


def test_merge_post_write_read_failure_is_bounded(tmp_state_dir, fake_github):
    """Repeated re-read failures after `gh pr merge` end in BLOCKED, never a guessed count."""
    fake_github.add_pr(head_sha=SHA_A)
    fake_github.merge_leaves_open = True  # gh exits 0, GitHub has not merged (yet)
    eng = _in_merge(tmp_state_dir, fake_github)
    eng.config.merge.max_verification_attempts = 2
    orig_merge = fake_github.merge_pr

    def merge_then_break(*a, **kw):
        orig_merge(*a, **kw)
        fake_github.get_pr_failures = 1  # only the post-write re-read fails

    fake_github.merge_pr = merge_then_break
    with pytest.raises(VerificationError, match="outcome unknown.*attempt 1/2"):
        eng.step(allow_merge=True)
    assert eng.state.phase == Phase.MERGE and eng.state.attempt == 1
    # resume: pre-checks pass (PR still OPEN), re-attempted, re-read fails again -> bound
    # reached -> BLOCKED with nothing counted.
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED" and "could not be re-read" in eng.state.block_reason
    assert "inspect the PR on GitHub manually" in eng.state.block_reason
    assert eng.state.counted_merged_prs == [] and eng.state.merged_since_epic_update == 0


# -- GitHub read failures during pre-merge verification (R3-F1) ----------------------
# Every fact the verification needs comes from two reads: the PR itself and the
# merge-queue status. A failure of either must never escape as a raw GitHubError
# (which `resume --allow-merge` could retry forever): transient failures take the
# bounded inconclusive path, conclusive ones are BLOCKED. Nothing is ever merged.

_UNAVAILABLE = GitHubUnavailableError("`gh pr view` failed (exit 1): 503 Service Unavailable")
_UNAUTHORIZED = GitHubError("`gh pr view` failed (exit 1): HTTP 401: Bad credentials")


def _break_pr_read(gh, exc):
    gh.get_pr_error = exc


def _break_queue_read(gh, exc):
    gh.merge_queue_error = exc


def _break_files_read(gh, exc):
    gh.changed_files_error = exc


def _break_comment_read(gh, exc):
    gh.get_comment_error = exc


_READ_FAILURES = [
    pytest.param(_break_pr_read, "could not be read", id="pr-read"),
    pytest.param(_break_files_read, "changed-file listing", id="files-read"),
    pytest.param(_break_queue_read, "merge-queue status", id="queue-read"),
    pytest.param(_break_comment_read, "review comment", id="comment-read"),
]


# Real `gh` stderr shapes that must be *transient* end to end (R4-F1: server-side
# status class; R5-F1: OS / DNS connectivity failures as Go's net package reports them).
_TRANSIENT_GH_STDERR = [
    pytest.param(
        "HTTP 500: Internal Server Error (https://api.github.com/graphql)",
        "HTTP 500",
        id="http-500",
    ),
    pytest.param(
        'Post "https://api.github.com/graphql": dial tcp 140.82.112.6:443: connect: '
        "network is unreachable",
        "network is unreachable",
        id="network-unreachable",
    ),
    pytest.param(
        'Post "https://api.github.com/graphql": dial tcp: lookup api.github.com: '
        "Temporary failure in name resolution",
        "Temporary failure in name resolution",
        id="dns-temporary-failure",
    ),
]


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
@pytest.mark.parametrize(("stderr", "marker"), _TRANSIENT_GH_STDERR)
def test_transient_gh_stderr_is_retried_then_bounded_verification(
    tmp_state_dir, phase, stderr, marker
):
    """R4-F1 / R5-F1: transient `gh` failures never become a conclusive BLOCKED.

    Drives the real GitHubClient (scripted `gh` runner, no network) through the
    engine: the failed read is retried by the client, then classified as
    unavailable so the engine takes the bounded re-check path and blocks only
    once the bound is reached. Nothing is merged.
    """
    calls: list[list[str]] = []

    def gh_runner(req):
        calls.append(req.command)
        return ExecutionResult(
            command=req.command,
            cwd=None,
            exit_code=1,
            stdout="",
            stderr=stderr,
            started_at="",
            finished_at="",
        )

    gh = GitHubClient(runner=gh_runner, retry_delay_seconds=0, transient_retries=1)
    eng = _in_merge(tmp_state_dir, gh, phase=phase)  # type: ignore[arg-type]
    eng.config.merge.max_verification_attempts = 3
    for n in (1, 2):
        with pytest.raises(
            VerificationError, match=f"could not be read.*{re.escape(marker)}.*attempt {n}/3"
        ):
            eng.step(allow_merge=True)
        assert load_state(eng.paths.state_file).phase == phase
        assert load_state(eng.paths.state_file).attempt == n
        # transient_retries=1 -> the client retried the read once per verification attempt
        assert len(calls) == 2 * n
        assert all(cmd[1:3] == ["pr", "view"] for cmd in calls)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED"
    assert "inconclusive for 3 verification attempt(s)" in eng.state.block_reason
    assert "not a transient GitHub failure" not in eng.state.block_reason
    assert load_state(eng.paths.state_file).phase == Phase.BLOCKED
    assert not any("merge" in cmd for cmd in calls)  # gh pr merge was never run


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
@pytest.mark.parametrize(("breaker", "expected"), _READ_FAILURES)
def test_transient_read_failure_is_bounded_then_blocked(
    tmp_state_dir, fake_github, phase, breaker, expected
):
    """Unavailable GitHub: one attempt per invocation, BLOCKED at the bound, never merged."""
    fake_github.add_pr(head_sha=SHA_A)
    breaker(fake_github, _UNAVAILABLE)
    eng = _in_merge(tmp_state_dir, fake_github, phase=phase)
    eng.config.merge.max_verification_attempts = 3
    for n in (1, 2):
        with pytest.raises(VerificationError, match=f"{expected}.*503.*attempt {n}/3"):
            eng.step(allow_merge=True)
        assert eng.state.phase == phase
        assert load_state(eng.paths.state_file).attempt == n
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED"
    assert expected in eng.state.block_reason
    assert "inconclusive for 3 verification attempt(s)" in eng.state.block_reason
    assert "resume" not in out.message
    assert load_state(eng.paths.state_file).phase == Phase.BLOCKED
    assert fake_github.merges == [] and eng.state.counted_merged_prs == []


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
@pytest.mark.parametrize(("breaker", "expected"), _READ_FAILURES)
def test_conclusive_read_failure_blocks_immediately(
    tmp_state_dir, fake_github, phase, breaker, expected
):
    """Bad credentials / permissions: re-checking would not help -> BLOCKED at once."""
    fake_github.add_pr(head_sha=SHA_A)
    breaker(fake_github, _UNAUTHORIZED)
    eng = _in_merge(tmp_state_dir, fake_github, phase=phase)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED"
    assert expected in eng.state.block_reason and "401" in eng.state.block_reason
    assert "not a transient GitHub failure" in eng.state.block_reason
    assert eng.state.attempt == 0  # no verification attempt was consumed
    assert load_state(eng.paths.state_file).phase == Phase.BLOCKED
    assert fake_github.merges == [] and eng.state.counted_merged_prs == []
    # BLOCKED is terminal for step(): a later resume cannot retry its way into a merge.
    with pytest.raises(StateTransitionError):
        eng.step(allow_merge=True)
    assert fake_github.merges == []


_LEAKY_SECRET = "ghp_FakeSecretForBlockReasonTest0123456789"
_LEAKY_GH_STDERR = (
    f"HTTP 401: Bad credentials (Authorization: Bearer {_LEAKY_SECRET}) "
    f"(https://x-access-token:{_LEAKY_SECRET}@api.github.com/graphql)"
)


@pytest.mark.parametrize(("breaker", "expected"), _READ_FAILURES)
def test_block_reason_quoting_gh_output_is_redacted_before_persistence(
    tmp_state_dir, fake_github, breaker, expected
):
    """#98: `_block` redacts once, at the sink. A `GitHubError` that echoes an
    ``Authorization`` header reaches `state.json`, the step outcome and the
    run log redacted, whichever of the ~40 call sites composed the reason."""
    fake_github.add_pr(head_sha=SHA_A)
    breaker(fake_github, GitHubError(f"`gh pr view` failed (exit 1): {_LEAKY_GH_STDERR}"))
    eng = _in_merge(tmp_state_dir, fake_github, phase=Phase.MERGE)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED"
    assert expected in out.message and _LEAKY_SECRET not in out.message
    assert "Authorization: Bearer ***REDACTED***" in out.message
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.BLOCKED and s.block_reason == out.message
    assert _LEAKY_SECRET not in eng.paths.state_file.read_text()
    assert "Authorization: Bearer ***REDACTED***" in s.block_reason
    assert "https://***REDACTED***@api.github.com" in s.block_reason
    for path in (eng.paths.logs_dir / s.run_id).rglob("*"):
        if path.is_file():
            assert _LEAKY_SECRET not in path.read_text(), path
    assert fake_github.merges == []


def test_block_reason_is_redacted_and_then_bounded_at_the_sink(tmp_state_dir, fake_github):
    """#88: `_block` bounds the reason to ``MAX_BLOCK_REASON_CHARS`` after
    redacting it, whichever call site composed it. The order matters: the
    clip is by character count, so a secret sitting exactly on the clip
    boundary would be cut into fragments no pattern matches if it ran first."""
    from autoforge.state import BLOCK_REASON_TAIL_CHARS, MAX_BLOCK_REASON_CHARS

    fake_github.add_pr(head_sha=SHA_A)
    head_len = MAX_BLOCK_REASON_CHARS - BLOCK_REASON_TAIL_CHARS - 200
    # Two bands of back-to-back secrets, one around where the head ends and
    # one around where the tail begins, each wider than the controller text
    # the call site puts before and after the `gh` output, so a secret
    # straddles each clip boundary wherever exactly it falls.
    band = (_LEAKY_SECRET + " ") * 60
    filler = "gh: request failed; " * 1000
    stderr = filler[: head_len - 1000] + band + filler[:10_000] + band + filler[:500]
    assert len(stderr) > 2 * MAX_BLOCK_REASON_CHARS
    fake_github.get_pr_error = GitHubError(f"`gh pr view` failed (exit 1): {stderr}")
    eng = _in_merge(tmp_state_dir, fake_github, phase=Phase.MERGE)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED"
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.BLOCKED and s.block_reason == out.message
    assert len(s.block_reason) <= MAX_BLOCK_REASON_CHARS
    assert s.block_reason.startswith("PR https://github.com/owner/repo/pull/42 could not be read")
    assert " characters of the block reason omitted; " in s.block_reason
    assert s.block_reason.endswith("Fix the cause and inspect the PR on GitHub manually.")
    assert "***REDACTED*** ***REDACTED***" in s.block_reason
    # Both clips landed inside a band (the text either side of the marker is
    # a redaction marker or a cut one), and no fragment of the secret
    # survives either side of either clip.
    marker_start = s.block_reason.index(" [autoforge: ")
    marker_end = s.block_reason.index(" were kept] ") + len(" were kept] ")
    assert "REDACTED" in s.block_reason[marker_start - 40 : marker_start]
    assert "REDACTED" in s.block_reason[marker_end : marker_end + 40]
    for n in range(4, len(_LEAKY_SECRET)):
        assert _LEAKY_SECRET[:n] not in s.block_reason
        assert _LEAKY_SECRET[-n:] not in s.block_reason
    assert _LEAKY_SECRET not in eng.paths.state_file.read_text()
    assert fake_github.merges == []


@pytest.mark.parametrize("status", ["failure", "blocked"])
def test_agent_message_is_bounded_before_it_reaches_state(tmp_state_dir, fake_github, status):
    """#88: the agent's ``message`` is bounded by the parser only through the
    whole CONTROL_RESULT block (1 MiB); the REMOTE writer bounds it like every
    other block reason, after redacting it."""
    from autoforge.state import MAX_BLOCK_REASON_CHARS

    message = "tests red: " + ("FAILED tests/test_x.py::test_y; " * 5000) + "see the run log"
    assert len(message) > MAX_BLOCK_REASON_CHARS
    payload = {"phase": "ANALYZE_EXECUTE", "status": status, "message": message}
    eng = make_engine(tmp_state_dir, [block(payload)], github=fake_github, origin=True)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    out = eng.step()
    assert out.next_phase == ("FAILED" if status == "failure" else "BLOCKED")
    s = load_state(eng.paths.state_file)
    assert len(s.block_reason) <= MAX_BLOCK_REASON_CHARS
    assert s.block_reason.startswith("tests red: FAILED tests/test_x.py::test_y; ")
    assert s.block_reason.endswith("see the run log")
    assert " characters of the block reason omitted; " in s.block_reason
    assert out.message == f"agent reported {status}: {s.block_reason}"


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
def test_conclusive_read_failure_via_resume_blocks_without_retry(tmp_state_dir, fake_github, phase):
    fake_github.add_pr(head_sha=SHA_A)
    fake_github.get_pr_error = _UNAUTHORIZED
    eng = _in_merge(tmp_state_dir, fake_github, phase=phase)
    outcomes = eng.run(max_steps=50, allow_merge=True)
    assert [o.next_phase for o in outcomes] == ["BLOCKED"]
    assert fake_github.calls.count(("get_pr", PR)) == 1
    assert fake_github.merges == []


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
def test_transient_read_failure_via_resume_consumes_one_attempt_each(
    tmp_state_dir, fake_github, phase
):
    """`resume --allow-merge` against an unavailable GitHub is bounded, then BLOCKED."""
    fake_github.add_pr(head_sha=SHA_A)
    fake_github.merge_queue_error = _UNAVAILABLE
    eng = _in_merge(tmp_state_dir, fake_github, phase=phase)
    eng.config.merge.max_verification_attempts = 2
    with pytest.raises(VerificationError, match=f"{phase.value}.*attempt 1/2"):
        eng.run(max_steps=50, allow_merge=True)
    assert load_state(eng.paths.state_file).attempt == 1
    outcomes = eng.run(max_steps=50, allow_merge=True)
    assert [o.next_phase for o in outcomes] == ["BLOCKED"]
    assert fake_github.merges == [] and eng.state.counted_merged_prs == []


def test_transient_read_failure_then_recovery_proceeds(tmp_state_dir, fake_github):
    """Once GitHub answers again the run continues normally and the attempt counter resets."""
    fake_github.add_pr(head_sha=SHA_A)
    fake_github.get_pr_failures = 1
    eng = _in_ready(tmp_state_dir, fake_github)
    with pytest.raises(VerificationError, match="could not be read.*attempt 1/"):
        eng.step(allow_merge=True)
    assert eng.state.phase == Phase.READY_FOR_MERGE and eng.state.attempt == 1
    out = eng.step(allow_merge=True)
    assert out.next_phase == "MERGE" and eng.state.attempt == 0
    assert fake_github.merges == []  # READY_FOR_MERGE never writes


def test_merge_transient_read_failure_never_reaches_the_write(tmp_state_dir, fake_github):
    """Even at the bound, a failed pre-write read never falls through to `gh pr merge`."""
    fake_github.add_pr(head_sha=SHA_A)
    fake_github.get_pr_error = _UNAVAILABLE
    eng = _in_merge(tmp_state_dir, fake_github)
    eng.config.merge.max_verification_attempts = 1
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED" and "could not be read" in eng.state.block_reason
    assert fake_github.merges == [] and eng.state.counted_merged_prs == []


def test_merge_counts_only_after_github_confirms_merged(tmp_state_dir, fake_github):
    """The controller re-reads the PR after the write; the gh exit status alone never counts."""
    fake_github.add_pr(head_sha=SHA_A)
    eng = _in_merge(tmp_state_dir, fake_github)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC"
    calls = fake_github.calls
    merge_idx = next(i for i, c in enumerate(calls) if c[0] == "merge_pr")
    assert any(c == ("get_pr", PR) for c in calls[:merge_idx])  # verified before writing
    assert any(c == ("get_pr", PR) for c in calls[merge_idx + 1 :])  # re-read after writing


# -- UPDATE_EPIC: next_issue_url is verified before switching issues (issue #12) ----------------
ISSUE999 = "https://github.com/owner/repo/issues/999"
FOREIGN_ISSUE = "https://github.com/other/repo/issues/1"


ROADMAP = "## Roadmap\n- [x] #1 (PR #42)\n- [ ] #3"
PROGRESS = "Implemented issue 2 in PR 42; the test suite passes."
_ABSENT = object()


def _epic_result(
    next_issue_url, roadmap_section: str | None = ROADMAP, progress: str | None = PROGRESS
) -> str:
    """A FULL UPDATE_EPIC result: the agent hands over the progress text, the
    controller posts it (ADR 0004 §2.5)."""
    payload = {
        "phase": "UPDATE_EPIC",
        "status": "success",
        "progress": progress,
        "roadmap_section": roadmap_section,
        "next_issue_url": next_issue_url,
    }
    if progress is None:
        del payload["progress"]
    return block(payload)


def _epic_reselect(next_issue_url=_ABSENT, roadmap_section=_ABSENT) -> str:
    """A re-request result (D4.7): only the keys the re-request asks for."""
    payload: dict = {"phase": "UPDATE_EPIC", "status": "success"}
    if next_issue_url is not _ABSENT:
        payload["next_issue_url"] = next_issue_url
    if roadmap_section is not _ABSENT:
        payload["roadmap_section"] = roadmap_section
    return block(payload)


def _controller_progress_body(progress: str = PROGRESS, issue: str = ISSUE, pr: str = PR) -> str:
    """The progress comment exactly as the controller posts it: text, blank line, marker."""
    return f"{progress}\n\n{render_progress_marker(issue, pr)}"


def _progress_posts(gh) -> list[tuple]:
    """The controller's own progress-comment writes on the EPIC."""
    return [w for w in gh.effect_writes if w[0] == "create_issue_comment" and w[1] == EPIC]


def _in_update_epic(tmp_state_dir, gh, script):
    """Engine parked in UPDATE_EPIC right after the controller merged PR for ISSUE.

    A list of scripted results stands for an agent that returns them in
    turn and publishes nothing: the controller posts the progress comment.
    """
    eng = make_engine(tmp_state_dir, script, github=gh)
    gh.add_pr(head_sha=SHA_A, state="MERGED")
    eng.state.phase = Phase.UPDATE_EPIC
    eng.state.current_pr_url = PR
    eng.state.current_branch = BRANCH
    eng.state.current_head_sha = SHA_A
    eng.state.reviewed_head_sha = SHA_A
    eng.state.last_review_result = "clean"
    eng.state.review_round = 2
    eng.state.record_merge(PR)
    eng._save()
    return eng


def test_update_epic_verified_next_issue_switches(tmp_state_dir, fake_github):
    fake_github.add_issue(ISSUE3, "Next")
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(ISSUE3)])
    out = eng.step()
    assert out.next_phase == "ANALYZE_EXECUTE" and "verified next issue #3" in out.message
    assert ("get_issue", ISSUE3) in fake_github.calls
    # The controller posted the agent's text, followed by the marker, once.
    assert _progress_posts(fake_github) == [
        ("create_issue_comment", EPIC, _controller_progress_body())
    ]
    assert [c.body for c in fake_github.comments[EPIC]] == [_controller_progress_body()]
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.ANALYZE_EXECUTE and s.current_issue_url == ISSUE3
    assert s.current_pr_url == "" and s.review_round == 0 and s.reviewed_head_sha == ""
    assert s.merged_since_epic_update == 0 and s.counted_merged_prs == [PR]
    assert s.next_issue_rejections == [] and s.attempt == 0


def test_update_epic_null_completes_the_run(tmp_state_dir, fake_github):
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(None)])
    out = eng.step()
    assert out.next_phase == "DONE"
    s = load_state(eng.paths.state_file)
    assert s.current_issue_url == ISSUE and s.merged_since_epic_update == 0
    assert [c for c in fake_github.calls if c[0] == "get_issue" and c[1] != EPIC] == []


# -- UPDATE_EPIC entry and read-back: the progress comment (PR #89 review, F2) -------------
def test_update_epic_entry_blocks_on_a_progress_comment_it_did_not_journal(
    tmp_state_dir, fake_github
):
    """ADR 0004 D9.7: a progress comment for this (issue, PR) that no record
    explains is never adopted and never duplicated. The entry blocks before
    launching, names the comment, and posts nothing."""
    fake_github.add_comment(EPIC, 300, progress_comment_body())
    eng = _in_update_epic(tmp_state_dir, fake_github, ["never"])
    out = eng.step()
    assert out.next_phase == "BLOCKED" and eng.provider.calls == []
    s = load_state(eng.paths.state_file)
    assert comment_url(EPIC, 300) in s.block_reason and "D9.7" in s.block_reason
    assert "Nothing was posted" in s.block_reason
    assert _progress_posts(fake_github) == [] and len(fake_github.comments[EPIC]) == 1
    assert s.effect_records == [] and s.current_issue_url == ISSUE


def test_update_epic_entry_ignores_a_progress_comment_for_another_pr(tmp_state_dir, fake_github):
    """The marker binds the comment to (issue, PR): an earlier issue's or PR's
    progress comment on the same EPIC is not this entry's."""
    other_pr = "https://github.com/owner/repo/pull/41"
    fake_github.add_comment(EPIC, 299, progress_comment_body(ISSUE, other_pr))
    fake_github.add_comment(EPIC, 298, progress_comment_body(ISSUE3, PR))

    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(None)])
    assert eng.step().next_phase == "DONE"
    assert len(fake_github.comments[EPIC]) == 3 and len(_progress_posts(fake_github)) == 1


def test_update_epic_entry_blocks_on_two_progress_comments_without_invoking(
    tmp_state_dir, fake_github
):
    fake_github.add_comment(EPIC, 300, progress_comment_body())
    fake_github.add_comment(EPIC, 301, progress_comment_body())
    eng = _in_update_epic(tmp_state_dir, fake_github, ["never"])
    out = eng.step()
    assert out.next_phase == "BLOCKED" and eng.provider.calls == []
    s = load_state(eng.paths.state_file)
    assert "2 comments carry the ai-epic-progress marker for issue" in s.block_reason
    assert comment_url(EPIC, 300) in s.block_reason and comment_url(EPIC, 301) in s.block_reason
    assert s.current_issue_url == ISSUE and s.merged_since_epic_update == 1


def test_update_epic_result_without_progress_text_is_corrected_before_anything_is_posted(
    tmp_state_dir, fake_github
):
    """The progress text is a required field of the FULL result: a result
    without it is malformed, takes the correction path, and nothing is
    posted, switched or written until a well-formed result arrives."""
    fake_github.add_issue(ISSUE3, "Next")
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(ISSUE3, progress=None), "junk"])
    with pytest.raises(ControlResultValidationError, match="after 2 attempt"):
        eng.step()
    assert "missing required field 'progress'" in eng.provider.calls[1].prompt
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.UPDATE_EPIC and s.current_issue_url == ISSUE and s.attempt == 2
    assert s.next_issue_rejections == [] and s.effect_records == []
    assert [c for c in fake_github.calls if c[0] == "get_issue" and c[1] != EPIC] == []
    assert s.merged_since_epic_update == 1 and fake_github.edited_issues == []
    assert _progress_posts(fake_github) == []


def test_update_epic_progress_with_a_url_is_corrected_before_anything_is_posted(
    tmp_state_dir, fake_github
):
    """#160: the progress text carries no URL. A result whose progress links
    the PR takes the correction path: nothing is posted, journaled or
    written while the correction is asked for, and the one comment posted
    afterwards carries the corrected text."""
    linked = "Implemented issue 2 in https://github.com/owner/repo/pull/42; tests pass."
    seen_at_correction: list[tuple] = []

    def agent(req):
        if not req.correction:
            return _epic_result(None, progress=linked)
        s = load_state(eng.paths.state_file)
        seen_at_correction.append(
            (_progress_posts(fake_github), s.effect_records, list(fake_github.edited_issues))
        )
        return _epic_result(None)

    eng = _in_update_epic(tmp_state_dir, fake_github, agent)
    assert eng.step().next_phase == "DONE"
    assert len(eng.provider.calls) == 2
    assert "field 'progress' contains a URL at index 28" in eng.provider.calls[1].prompt
    assert seen_at_correction == [([], [], [])]
    assert _progress_posts(fake_github) == [
        ("create_issue_comment", EPIC, _controller_progress_body())
    ]


def test_update_epic_agent_that_posts_its_own_progress_comment_blocks_before_the_controller_posts(
    tmp_state_dir, fake_github
):
    """The precondition read before the controller's post (D4.2) finds a comment
    the entry observation does not explain (§2.9): BLOCKED, nothing posted, no
    record planned."""

    def posts(req):
        fake_github.add_comment(EPIC, 301, progress_comment_body())
        return _epic_result(None)

    eng = _in_update_epic(tmp_state_dir, fake_github, posts)
    out = eng.step()
    assert out.next_phase == "BLOCKED" and len(eng.provider.calls) == 1
    s = load_state(eng.paths.state_file)
    assert comment_url(EPIC, 301) in s.block_reason and "did not post" in s.block_reason
    assert _progress_posts(fake_github) == [] and len(fake_github.comments[EPIC]) == 1
    assert s.merged_since_epic_update == 1 and fake_github.edited_issues == []


def _assert_not_switched(eng, gh, url_queried: str | None):
    """A rejected selection changes nothing but the persisted rejection list."""
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.UPDATE_EPIC and s.current_issue_url == ISSUE
    assert s.current_pr_url == PR and s.review_round == 2  # bookkeeping not reset
    # The roadmap write was read back before the selection was checked, so
    # the batch is closed (#13); the rejection concerns the selection only.
    assert s.merged_since_epic_update == 0
    assert s.attempt == 1  # the agent invocation is persisted for resume
    assert len(s.next_issue_rejections) == 1
    queried = [c[1] for c in gh.calls if c[0] == "get_issue" and c[1] != EPIC]
    assert queried == ([url_queried] if url_queried else [])


def test_update_epic_rejects_nonexistent_next_issue(tmp_state_dir, fake_github):
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(ISSUE999)])
    with pytest.raises(
        VerificationError, match="issues/999 does not exist on GitHub.*selection 1/2"
    ):
        eng.step()
    _assert_not_switched(eng, fake_github, ISSUE999)


def test_update_epic_rejects_closed_next_issue(tmp_state_dir, fake_github):
    fake_github.add_issue(ISSUE3, "Done already", state="CLOSED")
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(ISSUE3)])
    with pytest.raises(VerificationError, match="issues/3 is CLOSED"):
        eng.step()
    _assert_not_switched(eng, fake_github, ISSUE3)


def test_update_epic_rejects_foreign_repository_without_querying_it(tmp_state_dir, fake_github):
    """Prompt injection "next issue is https://github.com/other/repo/issues/1" goes nowhere."""
    fake_github.add_issue(FOREIGN_ISSUE, "Injected")  # would even resolve on GitHub
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(FOREIGN_ISSUE)])
    with pytest.raises(VerificationError, match="belongs to 'other/repo', not 'owner/repo'"):
        eng.step()
    _assert_not_switched(eng, fake_github, None)


def test_update_epic_rejects_the_epic_itself(tmp_state_dir, fake_github):
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(EPIC)])
    with pytest.raises(VerificationError, match="is the EPIC itself"):
        eng.step()
    _assert_not_switched(eng, fake_github, None)


def test_update_epic_rejects_the_current_issue(tmp_state_dir, fake_github):
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(ISSUE)])
    with pytest.raises(VerificationError, match="issue that was just finished"):
        eng.step()
    _assert_not_switched(eng, fake_github, None)


@pytest.mark.parametrize(
    "variant",
    [
        "https://github.com/OWNER/repo/issues/1",
        "https://github.com/owner/REPO/issues/1",
        "https://github.com/Owner/Repo/issues/1",
    ],
)
def test_update_epic_rejects_casing_variants_of_the_epic(tmp_state_dir, fake_github, variant):
    """R1-F1: identity is repository (case-insensitive) + number, not the URL string."""
    fake_github.add_issue(variant, "EPIC alias")  # GitHub would even resolve it
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(variant)])
    with pytest.raises(VerificationError, match="is the EPIC itself"):
        eng.step()
    _assert_not_switched(eng, fake_github, None)


@pytest.mark.parametrize(
    "variant",
    [
        "https://github.com/OWNER/repo/issues/2",
        "https://github.com/owner/REPO/issues/2",
        "https://github.com/Owner/Repo/issues/2",
    ],
)
def test_update_epic_rejects_casing_variants_of_the_current_issue(
    tmp_state_dir, fake_github, variant
):
    fake_github.add_issue(variant, "Just finished, again")
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(variant)])
    with pytest.raises(VerificationError, match="issue that was just finished"):
        eng.step()
    _assert_not_switched(eng, fake_github, None)


def test_update_epic_accepts_a_casing_variant_of_a_valid_next_issue(tmp_state_dir, fake_github):
    """Case-insensitivity must not over-reject: #3 spelled with another casing is still #3."""
    fake_github.add_issue(ISSUE3, "Next")
    variant = "https://github.com/Owner/Repo/issues/3"
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(variant)])
    out = eng.step()
    assert out.next_phase == "ANALYZE_EXECUTE"
    assert load_state(eng.paths.state_file).current_issue_url == variant


def test_update_epic_malformed_next_issue_url_is_refused_at_parse_time_and_corrected(
    tmp_state_dir, fake_github
):
    """A string that is not an issue URL is a malformed UPDATE_EPIC result (the
    next_issue_url half of #15): it takes the ordinary correction retry, is
    never queried on GitHub and does not spend one of the bounded re-selections."""
    eng = _in_update_epic(
        tmp_state_dir, fake_github, [_epic_result("not a url"), _epic_result(None)]
    )
    out = eng.step()
    assert out.next_phase == "DONE"
    assert len(eng.provider.calls) == 2
    second = eng.provider.calls[1]
    assert second.correction is True
    assert "'next_issue_url' must be a GitHub issue URL" in second.prompt
    assert [c for c in fake_github.calls if c[0] == "get_issue" and c[1] != EPIC] == []
    assert load_state(eng.paths.state_file).next_issue_rejections == []


def test_update_epic_oversized_next_issue_url_is_refused_and_never_quoted(
    tmp_state_dir, fake_github
):
    """An oversized next_issue_url is refused by length before any URL parser
    quotes it: the correction prompt states the size and the limit, and the
    text reaches neither next_issue_rejections, the state file nor the
    controller's error record."""
    huge = "https://github.com/owner/repo/issues/" + "7" * MAX_URL_CHARS
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(huge), _epic_result(None)])
    out = eng.step()
    assert out.next_phase == "DONE"
    second = eng.provider.calls[1]
    assert second.correction is True
    assert f"is {len(huge)} characters" in second.prompt
    assert f"at most {MAX_URL_CHARS}" in second.prompt
    assert "7777" not in second.prompt
    s = load_state(eng.paths.state_file)
    assert s.next_issue_rejections == [] and s.phase == Phase.DONE
    assert "7777" not in eng.paths.state_file.read_text()
    errors = list((eng.paths.logs_dir / s.run_id).rglob("error.txt"))
    assert errors and all("7777" not in f.read_text() for f in errors)


def test_verify_issue_selectable_still_refuses_a_malformed_url(tmp_state_dir, fake_github):
    """Defence in depth: the engine's own parse stays for INITIALIZING (whose
    URL comes from the operator) and for a result that reached it unparsed."""
    eng = _in_update_epic(tmp_state_dir, fake_github, [])
    with pytest.raises(VerificationError, match="'not a url' is not a GitHub issue URL"):
        eng._verify_issue_selectable("not a url", switching=True)
    assert [c for c in fake_github.calls if c[0] == "get_issue"] == []


def test_update_epic_issue_repository_from_github_must_match(tmp_state_dir, fake_github):
    """GitHub resolving the URL to another repository (redirect) is rejected too."""
    fake_github.add_issue(ISSUE3, "Moved").repository = "other/repo"
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(ISSUE3)])
    with pytest.raises(VerificationError, match="belongs to 'other/repo'"):
        eng.step()
    _assert_not_switched(eng, fake_github, ISSUE3)


def test_update_epic_rejection_is_retried_once_with_the_reason_then_blocked(
    tmp_state_dir, fake_github
):
    eng = _in_update_epic(
        tmp_state_dir, fake_github, [_epic_result(ISSUE999), _epic_reselect(ISSUE999)]
    )
    with pytest.raises(VerificationError, match="selection 1/2"):
        eng.step()
    first_prompt = eng.provider.calls[0].prompt
    assert "# Phase: UPDATE_EPIC\n" in first_prompt and "re-request" not in first_prompt
    posted = fake_github.comments[EPIC][0].url

    out = eng.step()  # resume: the agent is asked once more, with the reason
    assert len(eng.provider.calls) == 2
    retry_prompt = eng.provider.calls[1].prompt
    assert "issues/999 does not exist on GitHub" in retry_prompt
    # ADR 0004 D4.7: the re-request names the published comment and asks for
    # the selection only; nothing is posted again.
    assert "# Phase: UPDATE_EPIC (re-request)" in retry_prompt and posted in retry_prompt
    assert '"progress"' not in retry_prompt
    assert len(fake_github.comments[EPIC]) == 1 and len(_progress_posts(fake_github)) == 1
    assert out.next_phase == "BLOCKED"
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.BLOCKED and "2 time(s)" in s.block_reason
    assert "issues/999" in s.block_reason
    assert s.current_issue_url == ISSUE
    # The first invocation's roadmap write verified and closed the batch; the
    # second found the section already in place and wrote nothing (#13).
    assert s.merged_since_epic_update == 0 and len(fake_github.edited_issues) == 1
    assert len(s.next_issue_rejections) == 2


def test_update_epic_retry_with_a_valid_selection_switches(tmp_state_dir, fake_github):
    fake_github.add_issue(ISSUE3, "Next")
    eng = _in_update_epic(
        tmp_state_dir, fake_github, [_epic_result(FOREIGN_ISSUE), _epic_reselect(ISSUE3)]
    )
    with pytest.raises(VerificationError, match="selection 1/2"):
        eng.step()
    out = eng.step()
    assert out.next_phase == "ANALYZE_EXECUTE"
    s = load_state(eng.paths.state_file)
    assert s.current_issue_url == ISSUE3 and s.next_issue_rejections == []
    assert s.merged_since_epic_update == 0 and s.attempt == 0


def test_update_epic_retry_with_null_completes(tmp_state_dir, fake_github):
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(EPIC), _epic_reselect(None)])
    with pytest.raises(VerificationError, match="is the EPIC itself"):
        eng.step()
    assert eng.step().next_phase == "DONE"
    assert load_state(eng.paths.state_file).next_issue_rejections == []


def test_update_epic_transient_github_failure_is_a_rejection_not_a_switch(
    tmp_state_dir, fake_github
):
    """R1-F2: only a *transient* failure takes the bounded re-selection path."""
    fake_github.add_issue(ISSUE3, "Next")
    fake_github.get_issue_errors[ISSUE3] = GitHubUnavailableError("gh: HTTP 502")
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(ISSUE3)])
    with pytest.raises(VerificationError, match="GitHub unavailable: gh: HTTP 502"):
        eng.step()
    _assert_not_switched(eng, fake_github, ISSUE3)


def test_update_epic_rejection_quoting_gh_output_is_redacted_before_persistence(
    tmp_state_dir, fake_github
):
    """#98: a next-issue rejection quotes the failed read, is persisted in
    `next_issue_rejections`, rendered into the correction prompt and, once
    the bound is hit, becomes the BLOCKED reason: redacted at every step."""
    fake_github.add_issue(ISSUE3, "Next")
    fake_github.get_issue_errors[ISSUE3] = GitHubUnavailableError(
        f"`gh issue view` failed (exit 1): HTTP 502 {_LEAKY_GH_STDERR}"
    )
    eng = _in_update_epic(
        tmp_state_dir, fake_github, [_epic_result(ISSUE3), _epic_reselect(ISSUE3)]
    )
    with pytest.raises(VerificationError, match="selection 1/2") as info:
        eng.step()
    assert _LEAKY_SECRET not in str(info.value)
    s = load_state(eng.paths.state_file)
    assert len(s.next_issue_rejections) == 1
    assert _LEAKY_SECRET not in s.next_issue_rejections[0]
    assert "Authorization: Bearer ***REDACTED***" in s.next_issue_rejections[0]
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert _LEAKY_SECRET not in eng.provider.calls[1].prompt
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.BLOCKED and "HTTP 502" in s.block_reason
    assert _LEAKY_SECRET not in eng.paths.state_file.read_text()
    assert _LEAKY_SECRET not in out.message


def test_update_epic_transient_github_failure_twice_is_blocked(tmp_state_dir, fake_github):
    fake_github.add_issue(ISSUE3, "Next")
    fake_github.get_issue_errors[ISSUE3] = GitHubUnavailableError("gh: HTTP 502")
    eng = _in_update_epic(
        tmp_state_dir, fake_github, [_epic_result(ISSUE3), _epic_reselect(ISSUE3)]
    )
    with pytest.raises(VerificationError, match="selection 1/2"):
        eng.step()
    out = eng.step()
    assert out.next_phase == "BLOCKED" and len(eng.provider.calls) == 2
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.BLOCKED and "HTTP 502" in s.block_reason
    assert s.current_issue_url == ISSUE and s.merged_since_epic_update == 0


@pytest.mark.parametrize(
    "error",
    [
        GitHubError("`gh issue view` failed (exit 1): HTTP 401: Bad credentials"),
        GitHubError("`gh issue view` failed (exit 1): HTTP 403: Resource not accessible"),
        GitHubError("`gh issue view` failed (exit 1): gh auth login"),
        GitHubError("`gh` returned invalid JSON: Expecting value"),
    ],
)
def test_update_epic_conclusive_github_failure_blocks_without_reinvoking_agent(
    tmp_state_dir, fake_github, error
):
    """R1-F2: auth / permission / malformed-data failures are not bad selections.

    Re-asking the agent would repeat UPDATE_EPIC's GitHub writes while the
    controller still could not verify anything, so the run blocks at once.
    """
    fake_github.add_issue(ISSUE3, "Next")
    fake_github.get_issue_errors[ISSUE3] = error
    eng = _in_update_epic(
        tmp_state_dir, fake_github, [_epic_result(ISSUE3), _epic_reselect(ISSUE3)]
    )
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert len(eng.provider.calls) == 1  # never asked to select again
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.BLOCKED
    assert str(error) in s.block_reason and "not a transient GitHub failure" in s.block_reason
    assert s.next_issue_rejections == []  # not a rejection of the selection
    assert s.current_issue_url == ISSUE and s.current_pr_url == PR
    assert s.merged_since_epic_update == 0  # the roadmap write verified before the selection
    with pytest.raises(StateTransitionError):
        eng.step()  # BLOCKED is terminal for `step`; nothing else runs


# -- UPDATE_EPIC: controller-owned roadmap splice and merge batching (#13, #4) -----------------
ROADMAP_START = "<!-- ai-controller-roadmap:start -->"
ROADMAP_END = "<!-- ai-controller-roadmap:end -->"
OPERATOR_BODY = "# EPIC\n\nOperator text.\n\n- [ ] #2 feature\n- [ ] #3 next\n"


def _epic_body(section: str | None = None, trailing: str = "") -> str:
    if section is None:
        return OPERATOR_BODY
    return f"{OPERATOR_BODY}\n{ROADMAP_START}\n{section}\n{ROADMAP_END}\n{trailing}"


def _in_update_epic_with_body(tmp_state_dir, gh, script, body: str, every: int = 1):
    gh.issues[EPIC].body = body
    cfg = default_config()
    cfg.workflow.epic_update_every = every
    eng = make_engine(tmp_state_dir, script, github=gh, cfg=cfg)
    gh.add_pr(head_sha=SHA_A, state="MERGED")
    eng.state.phase = Phase.UPDATE_EPIC
    eng.state.current_pr_url = PR
    eng.state.current_branch = BRANCH
    eng.state.current_head_sha = SHA_A
    eng.state.reviewed_head_sha = SHA_A
    eng.state.last_review_result = "clean"
    eng.state.record_merge(PR)
    eng._save()
    return eng


def test_update_epic_appends_the_roadmap_section_when_the_epic_has_none(tmp_state_dir, fake_github):
    """The agent returns the section; the controller writes it. An EPIC
    without markers gets the block appended after the operator's text, which
    stays byte-identical, and the merge counter resets only after the body
    is read back."""
    eng = _in_update_epic_with_body(tmp_state_dir, fake_github, [_epic_result(None)], _epic_body())
    out = eng.step()
    assert out.next_phase == "DONE" and "appended the managed roadmap section" in out.message
    assert "merge counter reset" in out.message
    body = fake_github.issues[EPIC].body
    assert body == f"{OPERATOR_BODY}\n{ROADMAP_START}\n{ROADMAP}\n{ROADMAP_END}\n"
    assert body.startswith(OPERATOR_BODY)
    assert fake_github.edited_issues == [(EPIC, body)]
    calls = fake_github.calls
    write = calls.index(("edit_issue_body", EPIC, body))
    assert ("get_issue", EPIC) in calls[write + 1 :]  # read back after the write
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.DONE and s.merged_since_epic_update == 0


def test_update_epic_replaces_only_the_managed_section(tmp_state_dir, fake_github):
    fake_github.add_issue(ISSUE3, "Next")
    before = _epic_body("old roadmap", trailing="\nOperator note after the section.\n")
    eng = _in_update_epic_with_body(tmp_state_dir, fake_github, [_epic_result(ISSUE3)], before)
    out = eng.step()
    assert out.next_phase == "ANALYZE_EXECUTE" and "replaced the managed" in out.message
    after = fake_github.issues[EPIC].body
    assert after == before.replace("old roadmap", ROADMAP)
    assert after.startswith(OPERATOR_BODY) and after.endswith("Operator note after the section.\n")
    s = load_state(eng.paths.state_file)
    assert s.current_issue_url == ISSUE3 and s.merged_since_epic_update == 0


def test_update_epic_prompt_carries_the_batch_and_the_current_section(tmp_state_dir, fake_github):
    seen = []

    def agent(req):
        seen.append(req.prompt)
        return _epic_result(None)

    eng = _in_update_epic_with_body(
        tmp_state_dir, fake_github, agent, _epic_body("- [x] #1 ```old```")
    )
    eng.state.record_merge(PR41)
    eng._save()
    assert eng.step().next_phase == "DONE"
    (prompt,) = seen
    assert "Roadmap update due now: yes" in prompt
    assert f"  - {PR}\n  - {PR41}" in prompt  # the batch, in merge order
    assert "(merged: 2; the controller updates the roadmap\n  every 1):" in prompt
    assert f"`{ROADMAP_START}`\n   `{ROADMAP_END}`" in prompt
    assert "````markdown\n- [x] #1 ```old```\n````" in prompt  # fenced, longer than its content
    assert "## You make no GitHub write" in prompt and "`gh issue edit`" in prompt


def test_update_epic_prompt_says_when_the_epic_has_no_section_yet(tmp_state_dir, fake_github):
    seen = []

    def agent(req):
        seen.append(req.prompt)
        return _epic_result(None)

    eng = _in_update_epic_with_body(tmp_state_dir, fake_github, agent, _epic_body())
    assert eng.step().next_phase == "DONE"
    assert "(none: the EPIC has no managed section yet" in seen[0]


def test_update_epic_roadmap_not_due_keeps_the_counter_and_writes_nothing(
    tmp_state_dir, fake_github
):
    """workflow.epic_update_every = 2 with one merge: UPDATE_EPIC still runs
    (progress comment, next issue) but the body is not written, the counter
    is kept, and a section the agent returned anyway is ignored."""
    fake_github.add_issue(ISSUE3, "Next")
    seen = []

    def agent(req):
        seen.append(req.prompt)
        return _epic_result(ISSUE3, roadmap_section="unsolicited")

    eng = _in_update_epic_with_body(tmp_state_dir, fake_github, agent, _epic_body("keep"), every=2)
    out = eng.step()
    assert out.next_phase == "ANALYZE_EXECUTE"
    assert "roadmap update not due (1 merge(s)" in out.message
    assert "Roadmap update due now: no" in seen[0]
    assert fake_github.edited_issues == [] and fake_github.issues[EPIC].body == _epic_body("keep")
    s = load_state(eng.paths.state_file)
    assert s.current_issue_url == ISSUE3 and s.merged_since_epic_update == 1
    assert s.counted_merged_prs == [PR]


def test_update_epic_roadmap_due_once_the_batch_is_full(tmp_state_dir, fake_github):
    fake_github.add_issue(ISSUE3, "Next")
    eng = _in_update_epic_with_body(
        tmp_state_dir, fake_github, [_epic_result(ISSUE3)], _epic_body(), every=2
    )
    eng.state.record_merge(PR41)
    eng._save()
    out = eng.step()
    assert out.next_phase == "ANALYZE_EXECUTE" and "roadmap update due (2 merge(s)" in out.message
    assert len(fake_github.edited_issues) == 1
    s = load_state(eng.paths.state_file)
    assert s.merged_since_epic_update == 0 and s.counted_merged_prs == [PR, PR41]


def test_update_epic_epic_complete_requires_the_final_roadmap_even_when_not_due(
    tmp_state_dir, fake_github
):
    """A missing required section is the controller's half of the schema: the
    result is corrected before anything is posted (no progress comment, no
    body write), and the corrected result completes the phase."""
    seen_posts = []

    def agent(req):
        seen_posts.append(len(_progress_posts(fake_github)))
        if not req.correction:
            return _epic_result(None, roadmap_section=None)
        assert "missing required field 'roadmap_section'" in req.prompt
        assert "reported complete" in req.prompt
        return _epic_result(None)

    eng = _in_update_epic_with_body(tmp_state_dir, fake_github, agent, _epic_body(), every=3)
    out = eng.step()
    assert out.next_phase == "DONE" and len(fake_github.edited_issues) == 1
    assert seen_posts == [0, 0] and len(_progress_posts(fake_github)) == 1
    assert load_state(eng.paths.state_file).merged_since_epic_update == 0


def test_update_epic_missing_roadmap_section_when_due_is_rejected_without_reset(
    tmp_state_dir, fake_github
):
    fake_github.add_issue(ISSUE3, "Next")
    eng = _in_update_epic_with_body(
        tmp_state_dir,
        fake_github,
        [_epic_result(ISSUE3, roadmap_section=None)] * 2,
        _epic_body(),
    )
    with pytest.raises(ControlResultValidationError, match="after 2 attempt"):
        eng.step()
    assert "missing required field 'roadmap_section' (1 merge" in eng.provider.calls[1].prompt
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.UPDATE_EPIC and s.merged_since_epic_update == 1 and s.attempt == 2
    assert s.current_issue_url == ISSUE and s.next_issue_rejections == []
    assert fake_github.edited_issues == [] and _progress_posts(fake_github) == []
    assert s.effect_records == [] and s.completion_context == {}
    assert ("get_issue", ISSUE3) not in fake_github.calls  # the selection is not reached


def test_update_epic_counter_resets_only_after_the_write_is_read_back(tmp_state_dir, fake_github):
    """gh exits 0 but the body read back does not carry the section: no reset."""
    fake_github.edit_issue_leaves_body = True
    eng = _in_update_epic_with_body(
        tmp_state_dir, fake_github, [_epic_result(None)], _epic_body("old")
    )
    with pytest.raises(VerificationError, match="does not carry the section that was written"):
        eng.step()
    assert len(fake_github.edited_issues) == 1
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.UPDATE_EPIC and s.merged_since_epic_update == 1


def test_update_epic_read_back_that_differs_outside_the_markers_is_rejected(
    tmp_state_dir, fake_github
):
    """A concurrent edit landing between the write and the read-back shows
    up outside the markers: rejected, counter kept."""

    def concurrent_edit(gh):
        gh.edit_issue_leaves_body = True
        gh.issues[EPIC].body = _epic_body(ROADMAP) + "\nhuman line\n"

    fake_github.edit_issue_race = concurrent_edit
    eng = _in_update_epic_with_body(
        tmp_state_dir, fake_github, [_epic_result(None)], _epic_body("old")
    )
    with pytest.raises(
        VerificationError, match="changed outside the roadmap markers while the section was"
    ):
        eng.step()
    s = load_state(eng.paths.state_file)
    assert s.merged_since_epic_update == 1
    # D4.6: the section is voided and re-requested; the comment stays published.
    assert s.completion_context["section_void"] is True
    assert s.completion_context["roadmap_section"] is None
    assert [r["stage"] for r in s.effect_records] == ["observed"]


def test_update_epic_body_changed_outside_the_markers_while_the_agent_ran_is_not_overwritten(
    tmp_state_dir, fake_github
):
    """The agent (or a human) edited the EPIC body during the invocation: the
    section was composed against a stale view, so nothing is written and
    the next entry re-reads the body."""
    calls = 0

    def agent(req):
        nonlocal calls
        calls += 1
        if calls == 1:
            fake_github.issues[EPIC].body = OPERATOR_BODY.replace("- [ ] #2", "- [x] #2")
            return _epic_result(None)
        assert "# Phase: UPDATE_EPIC (re-request)" in req.prompt
        assert "changed outside the roadmap markers" in req.prompt
        return _epic_reselect(roadmap_section=ROADMAP)

    eng = _in_update_epic_with_body(tmp_state_dir, fake_github, agent, _epic_body())
    with pytest.raises(VerificationError, match="changed outside the roadmap markers since"):
        eng.step()
    assert fake_github.edited_issues == []
    assert load_state(eng.paths.state_file).merged_since_epic_update == 1
    out = eng.step()  # resume: the entry re-reads, asks for the section only, writes it
    assert out.next_phase == "DONE" and len(fake_github.edited_issues) == 1
    assert len(_progress_posts(fake_github)) == 1  # the comment was posted once
    assert fake_github.issues[EPIC].body.startswith(OPERATOR_BODY.replace("- [ ] #2", "- [x] #2"))
    assert load_state(eng.paths.state_file).merged_since_epic_update == 0


def test_update_epic_crash_after_the_write_does_not_double_apply(tmp_state_dir, fake_github):
    """Crash after `gh issue edit` landed but before state was saved: the
    re-entry finds the section already in place, writes nothing, and only
    then resets the counter. One section, never two."""
    fake_github.add_issue(ISSUE3, "Next")
    eng = _in_update_epic_with_body(
        tmp_state_dir, fake_github, [_epic_result(ISSUE3), _epic_result(ISSUE3)], _epic_body()
    )
    landed = fake_github.edit_issue_body

    def crash_after_write(url, body):
        landed(url, body)
        raise RuntimeError("power loss")

    fake_github.edit_issue_body = crash_after_write
    with pytest.raises(RuntimeError, match="power loss"):
        eng.step()
    written = fake_github.issues[EPIC].body
    assert written.count(ROADMAP_START) == 1
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.UPDATE_EPIC and s.merged_since_epic_update == 1 and s.attempt == 1

    fake_github.edit_issue_body = landed
    eng.load()  # a fresh process resumes from disk
    out = eng.step()
    assert out.next_phase == "ANALYZE_EXECUTE"
    assert "already carries this section (no write needed)" in out.message
    # Journal first (§2.10): completed from the persisted context, no relaunch.
    assert "no agent launched" in out.message and len(eng.provider.calls) == 1
    assert len(_progress_posts(fake_github)) == 1
    assert len(fake_github.edited_issues) == 1  # the crashed attempt's write only
    assert fake_github.issues[EPIC].body == written
    assert written.count(ROADMAP_START) == 1 and written.count(ROADMAP_END) == 1
    s = load_state(eng.paths.state_file)
    assert s.current_issue_url == ISSUE3 and s.merged_since_epic_update == 0


def test_update_epic_entry_blocks_on_ambiguous_markers_without_invoking(tmp_state_dir, fake_github):
    body = (
        f"{OPERATOR_BODY}\n{ROADMAP_START}\na\n{ROADMAP_END}\n{ROADMAP_START}\nb\n{ROADMAP_END}\n"
    )
    eng = _in_update_epic_with_body(tmp_state_dir, fake_github, ["never"], body)
    out = eng.step()
    assert out.next_phase == "BLOCKED" and eng.provider.calls == []
    s = load_state(eng.paths.state_file)
    assert "2 start and 2 end roadmap marker(s)" in s.block_reason
    assert "will not guess" in s.block_reason
    assert s.merged_since_epic_update == 1 and fake_github.edited_issues == []
    assert fake_github.issues[EPIC].body == body


def test_update_epic_entry_blocks_on_a_conclusive_failure_reading_the_body(
    tmp_state_dir, fake_github
):
    """R1-F1 of PR #105: the entry's EPIC body read fails conclusively.
    Fail closed like the progress-comment read beside it: durable BLOCKED,
    no agent launched, nothing written, the merge counter kept."""
    fake_github.get_issue_errors[EPIC] = GitHubError("`gh issue view` failed (exit 1): HTTP 403")
    eng = _in_update_epic_with_body(tmp_state_dir, fake_github, ["never"], _epic_body())
    out = eng.step()
    assert out.next_phase == "BLOCKED" and eng.provider.calls == []
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.BLOCKED
    assert f"the body of EPIC {EPIC} could not be read" in s.block_reason
    assert "HTTP 403" in s.block_reason and "not a transient GitHub failure" in s.block_reason
    assert s.current_issue_url == ISSUE and s.merged_since_epic_update == 1
    assert fake_github.edited_issues == [] and fake_github.comments.get(EPIC, []) == []


def test_update_epic_entry_blocks_when_the_epic_is_gone(tmp_state_dir, fake_github):
    """A missing EPIC is a conclusive read failure, not a transient one."""
    fake_github.get_issue_errors[EPIC] = GitHubNotFoundError("issue not found")
    eng = _in_update_epic_with_body(tmp_state_dir, fake_github, ["never"], _epic_body())
    out = eng.step()
    assert out.next_phase == "BLOCKED" and eng.provider.calls == []
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.BLOCKED and "issue not found" in s.block_reason
    assert s.merged_since_epic_update == 1 and fake_github.edited_issues == []


def test_update_epic_entry_transient_failure_reading_the_body_propagates(
    tmp_state_dir, fake_github
):
    """GitHub unavailable while reading the body: the entry is retried by
    'resume', not blocked, and the phase, counter and attempt are untouched."""
    fake_github.get_issue_errors[EPIC] = GitHubUnavailableError("gh: HTTP 502")
    eng = _in_update_epic_with_body(tmp_state_dir, fake_github, [_epic_result(None)], _epic_body())
    with pytest.raises(GitHubUnavailableError):
        eng.step()
    assert eng.provider.calls == []
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.UPDATE_EPIC and s.merged_since_epic_update == 1 and s.attempt == 0
    del fake_github.get_issue_errors[EPIC]
    assert eng.step().next_phase == "DONE"
    assert load_state(eng.paths.state_file).merged_since_epic_update == 0


def test_update_epic_conclusive_failure_writing_the_body_blocks(tmp_state_dir, fake_github):
    fake_github.edit_issue_error = "`gh issue edit` failed (exit 1): HTTP 403: forbidden"
    eng = _in_update_epic_with_body(
        tmp_state_dir, fake_github, [_epic_result(None), _epic_result(None)], _epic_body()
    )
    out = eng.step()
    assert out.next_phase == "BLOCKED" and len(eng.provider.calls) == 1
    s = load_state(eng.paths.state_file)
    assert "HTTP 403" in s.block_reason and "merge counter was not reset" in s.block_reason
    assert s.merged_since_epic_update == 1 and s.phase == Phase.BLOCKED


def test_update_epic_transient_failure_writing_the_body_propagates(tmp_state_dir, fake_github):
    fake_github.edit_issue_error = GitHubUnavailableError("gh: HTTP 502")
    eng = _in_update_epic_with_body(
        tmp_state_dir, fake_github, [_epic_result(None), _epic_result(None)], _epic_body()
    )
    with pytest.raises(GitHubUnavailableError):
        eng.step()
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.UPDATE_EPIC and s.merged_since_epic_update == 1 and s.attempt == 1
    fake_github.edit_issue_error = ""
    assert eng.step().next_phase == "DONE"
    assert load_state(eng.paths.state_file).merged_since_epic_update == 0


def test_update_epic_dry_run_plan_names_the_batching_decision(tmp_state_dir, fake_github):
    eng = _in_update_epic_with_body(tmp_state_dir, fake_github, ["never"], _epic_body(), every=2)
    calls_before = list(fake_github.calls)
    out = eng.step(dry_run=True)
    assert any("roadmap update not due: 1 merge(s)" in n for n in out.plan.notes)
    assert "(read from the EPIC body immediately before the agent runs)" in out.plan.prompt_full
    assert fake_github.calls == calls_before and eng.provider.calls == []
    eng.state.record_merge(PR41)
    out = eng.step(dry_run=True)
    assert any("roadmap update due: 2 merge(s)" in n for n in out.plan.notes)
    assert fake_github.calls == calls_before and fake_github.edited_issues == []


# -- UPDATE_EPIC: the controller-posted progress comment across crash windows (K8) ---------
# ADR 0004 D4 and the crash table of issue #160, at the phase level: whatever
# window a process dies in, the comment is posted at most once per attempt,
# reconciled by its marker before anything is sent again, and a phase whose
# write landed finishes without relaunching its agent.
def _k8_records(eng) -> list[dict]:
    return load_state(eng.paths.state_file).effect_records


def _crash_on_first_call(fn):
    """``fn`` that dies (a power loss) the first time it is called, then works."""
    calls = {"n": 0}

    def wrapper(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("power loss")
        return fn(*args, **kwargs)

    return wrapper


def test_k8_crash_before_the_intent_is_saved_relaunches_and_posts_once(tmp_state_dir, fake_github):
    """Nothing was journaled, nothing was sent: the step re-runs from the launch."""
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(None)] * 2)
    eng._save_update_epic_context = _crash_on_first_call(eng._save_update_epic_context)
    with pytest.raises(RuntimeError, match="power loss"):
        eng.step()
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.UPDATE_EPIC and s.attempt == 1
    assert s.launch_label == "controller_publishes"
    assert s.effect_records == [] and s.completion_context == {}
    assert fake_github.effect_writes == []

    eng.load()
    out = eng.step()
    assert out.next_phase == "DONE" and len(eng.provider.calls) == 2
    assert not eng.provider.calls[1].correction  # a fresh FULL launch, not a correction
    assert _progress_posts(fake_github) == [
        ("create_issue_comment", EPIC, _controller_progress_body())
    ]


def test_k8_intent_saved_and_write_never_issued_posts_once_without_a_relaunch(
    tmp_state_dir, fake_github
):
    """The record is ``intended``: reconcile finds no comment and the precondition
    holds, so the comment is posted once from the journal; the agent is not asked again."""
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(None)])
    eng._persist_effect = _crash_on_first_call(eng._persist_effect)
    with pytest.raises(RuntimeError, match="power loss"):
        eng.step()
    [record] = _k8_records(eng)
    assert record["stage"] == "intended" and record["attempts"] == 0
    assert fake_github.effect_writes == []

    eng.load()
    out = eng.step()
    assert out.next_phase == "DONE" and "no agent launched" in out.message
    assert len(eng.provider.calls) == 1 and len(_progress_posts(fake_github)) == 1
    assert [c.body for c in fake_github.comments[EPIC]] == [_controller_progress_body()]


def _crash_with_the_progress_comment_intended(eng) -> dict:
    """Interrupt the step after the intent save, before the comment is posted;
    return the state file's JSON for the test to corrupt."""
    eng._persist_effect = _crash_on_first_call(eng._persist_effect)
    with pytest.raises(RuntimeError, match="power loss"):
        eng.step()
    [record] = _k8_records(eng)
    assert record["stage"] == "intended" and record["attempts"] == 0
    return json.loads(eng.paths.state_file.read_text(encoding="utf-8"))


def _assert_nothing_published_and_left_unchanged(eng, gh, before: bytes) -> None:
    assert gh.effect_writes == [] and gh.edited_issues == [] and EPIC not in gh.comments
    assert len(eng.provider.calls) == 1  # the interrupted launch only, no relaunch
    assert eng.paths.state_file.read_bytes() == before


@pytest.mark.parametrize(
    "missing", ["effect_records", "entry_observation", "completion_context", "launch_label"]
)
def test_k8_a_pending_progress_comment_is_never_skipped_by_dropping_an_effect_field(
    tmp_state_dir, fake_github, missing
):
    """The record is ``intended`` and the file then loses one effect field (a
    truncation or a hand edit). Read as its empty default, a missing
    ``effect_records`` is an empty plan, and the phase would complete from its
    context -- roadmap written, DONE -- with the progress comment never posted.
    The protocol-7 file is refused instead: nothing is posted or written, the
    agent is not relaunched, and the file is left for the operator."""
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(None)] * 2)
    data = _crash_with_the_progress_comment_intended(eng)
    del data[missing]
    eng.paths.state_file.write_text(json.dumps(data), encoding="utf-8")
    before = eng.paths.state_file.read_bytes()

    with pytest.raises(StateError, match=rf"missing required field\(s\) '{missing}'"):
        eng.load()
    _assert_nothing_published_and_left_unchanged(eng, fake_github, before)


@pytest.mark.parametrize(
    ("section", "rule"),
    [
        ("- [x] #2 thanks @octocat", "an @-mention"),
        ("- [x] #2\n\nCloses #99", "a closing keyword"),
        ("- [x] bad\x01text", "a control character"),
        ("- [x] #2\n<!-- ai-controller-roadmap:end -->", "a controller marker"),
    ],
)
def test_update_epic_a_stored_section_the_parser_refuses_is_never_published_on_recovery(
    tmp_state_dir, fake_github, section, rule
):
    """The persisted completion context is consumed with no agent result in
    between, so its roadmap section gets the parser's rules again on load
    (ADR 0004 D4.6). A section the result path would have refused is refused
    before any GitHub write: no progress comment, no EPIC body edit."""
    eng = _in_update_epic_with_body(
        tmp_state_dir, fake_github, [_epic_result(None)] * 2, _epic_body()
    )
    data = _crash_with_the_progress_comment_intended(eng)
    assert data["completion_context"]["roadmap_section"] == ROADMAP
    data["completion_context"]["roadmap_section"] = section
    eng.paths.state_file.write_text(json.dumps(data), encoding="utf-8")
    before = eng.paths.state_file.read_bytes()

    with pytest.raises(StateError, match="roadmap_section is invalid") as exc:
        eng.load()
    assert rule in str(exc.value)
    _assert_nothing_published_and_left_unchanged(eng, fake_github, before)
    assert fake_github.issues[EPIC].body == _epic_body()


@pytest.mark.parametrize(
    ("progress", "rule"),
    [
        ("Merged, thanks @octocat", "an @-mention"),
        ("Merged.\nCloses #99", "a closing keyword"),
        ("see https://example.com", "a URL"),
    ],
)
def test_k8_a_stored_progress_text_the_parser_refuses_is_never_posted_on_recovery(
    tmp_state_dir, fake_github, progress, rule
):
    """The intended record's payload is the progress text and the marker; a
    resumed write would post it with no agent result in between, so the text
    gets the parser's rules again on load and a refused one is never posted."""
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(None)] * 2)
    data = _crash_with_the_progress_comment_intended(eng)
    payload = data["effect_records"][0]["payload"]
    assert payload["body"] == _controller_progress_body()
    payload["body"] = _controller_progress_body(progress)
    eng.paths.state_file.write_text(json.dumps(data), encoding="utf-8")
    before = eng.paths.state_file.read_bytes()

    with pytest.raises(StateError, match="has an invalid progress text") as exc:
        eng.load()
    assert rule in str(exc.value)
    _assert_nothing_published_and_left_unchanged(eng, fake_github, before)


def _empty_the_plan(data: dict) -> None:
    data["effect_records"] = []


def _empty_the_plan_and_observation(data: dict) -> None:
    data["effect_records"] = []
    data["entry_observation"] = {}


def _empty_the_plan_and_its_marker(data: dict) -> None:
    data["effect_records"] = []
    data["entry_observation"]["objects"] = {}


def _adopt_beside_the_plan(data: dict) -> None:
    data["entry_observation"]["objects"] = {
        render_progress_marker(ISSUE, PR): comment_url(EPIC, 300)
    }


@pytest.mark.parametrize(
    ("corrupt", "needle"),
    [
        (_empty_the_plan, "must be saved with the one progress-comment record of its marker"),
        (_empty_the_plan_and_observation, "is persisted without the entry observation"),
        (_empty_the_plan_and_its_marker, "does not record its progress marker"),
        (_adopt_beside_the_plan, "plans a progress comment beside the adopted"),
    ],
)
def test_k8_a_plan_that_does_not_match_its_entry_is_refused_before_any_write(
    tmp_state_dir, fake_github, corrupt, needle
):
    """The record is ``intended`` and the file is then edited so that the plan
    is explicitly empty (``effect_records: []``), or plans the comment beside an
    adoption. Accepted, an empty plan reads as "every record observed": the
    phase would splice the roadmap, switch to DONE and drop the context with
    no progress comment ever posted. The context is loaded only beside the K8
    record of its marker or the one legacy comment its entry adopted (D13.7);
    anything else is refused before any write and the file is left as is."""
    eng = _in_update_epic_with_body(
        tmp_state_dir, fake_github, [_epic_result(None)] * 2, _epic_body()
    )
    data = _crash_with_the_progress_comment_intended(eng)
    corrupt(data)
    eng.paths.state_file.write_text(json.dumps(data), encoding="utf-8")
    before = eng.paths.state_file.read_bytes()

    with pytest.raises(StateError, match=needle):
        eng.load()
    _assert_nothing_published_and_left_unchanged(eng, fake_github, before)
    assert fake_github.issues[EPIC].body == _epic_body()


def test_k8_completion_with_no_published_comment_writes_no_roadmap_and_switches_nothing(
    tmp_state_dir, fake_github
):
    """The load refuses a context that has neither its K8 record nor an adopted
    comment. Completion checks again before its own writes: with no posted or
    adopted comment it raises before the roadmap splice and the issue switch."""
    eng = _in_update_epic_with_body(tmp_state_dir, fake_github, [_epic_result(None)], _epic_body())
    eng._published_progress_comment_url = lambda: ""
    with pytest.raises(StateError, match="no progress comment posted or adopted"):
        eng.step()
    assert fake_github.edited_issues == [] and fake_github.issues[EPIC].body == _epic_body()
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.UPDATE_EPIC and s.merged_since_epic_update == 1


def test_k8_write_lost_in_flight_is_reconciled_then_issued_once_more(tmp_state_dir, fake_github):
    """Attempt persisted, outcome unknown, and the write did not land: never a blind
    re-send in the same step; the next entry reads, finds nothing, posts once more."""
    fake_github.write_failures = [
        ("create_issue_comment", GitHubUnavailableError("gh: timed out"), False)
    ]
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(None)])
    with pytest.raises(GitHubUnavailableError, match="'resume' reconciles it"):
        eng.step()
    [record] = _k8_records(eng)
    assert record["stage"] == "attempted" and record["attempts"] == 1
    assert len(_progress_posts(fake_github)) == 1 and EPIC not in fake_github.comments

    eng.load()
    out = eng.step()
    assert out.next_phase == "DONE" and len(eng.provider.calls) == 1
    assert len(_progress_posts(fake_github)) == 2  # the lost one, then exactly one more
    assert [c.body for c in fake_github.comments[EPIC]] == [_controller_progress_body()]


def test_k8_write_landed_with_its_reply_lost_is_observed_by_the_read_back(
    tmp_state_dir, fake_github
):
    """Attempt persisted, the comment landed, the reply was lost: the read-back in
    the same step finds exactly one comment with the payload; nothing is sent again."""
    fake_github.write_failures = [
        ("create_issue_comment", GitHubUnavailableError("gh: timed out"), True)
    ]
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(None)])
    out = eng.step()
    assert out.next_phase == "DONE"
    assert len(_progress_posts(fake_github)) == 1 and len(fake_github.comments[EPIC]) == 1


def test_k8_crash_after_the_write_before_the_save_is_observed_without_a_second_post(
    tmp_state_dir, fake_github
):
    landed = fake_github.create_issue_comment

    def crash_after_write(url, body):
        landed(url, body)
        raise RuntimeError("power loss")

    fake_github.create_issue_comment = crash_after_write
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(None)])
    with pytest.raises(RuntimeError, match="power loss"):
        eng.step()
    [record] = _k8_records(eng)
    assert record["stage"] == "attempted" and len(fake_github.comments[EPIC]) == 1

    fake_github.create_issue_comment = landed
    eng.load()
    out = eng.step()
    assert out.next_phase == "DONE" and "no agent launched" in out.message
    assert len(_progress_posts(fake_github)) == 1 and len(fake_github.comments[EPIC]) == 1
    # A duplicate invocation after the restart finds nothing to do.
    assert load_state(eng.paths.state_file).phase == Phase.DONE


def test_k8_a_comment_posted_by_someone_else_between_intent_and_write_blocks(
    tmp_state_dir, fake_github
):
    """The precondition (no comment carries the marker) no longer holds and the
    comment found is not the payload: BLOCKED, naming it; nothing is posted."""
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(None)])
    eng._persist_effect = _crash_on_first_call(eng._persist_effect)
    with pytest.raises(RuntimeError, match="power loss"):
        eng.step()
    fake_github.add_comment(EPIC, 310, progress_comment_body())  # a human, meanwhile

    eng.load()
    out = eng.step()
    assert out.next_phase == "BLOCKED" and len(eng.provider.calls) == 1
    s = load_state(eng.paths.state_file)
    assert comment_url(EPIC, 310) in s.block_reason and "'unblock'" in s.block_reason
    assert _progress_posts(fake_github) == []
    assert [r["stage"] for r in s.effect_records] == ["conflict"]
    assert s.current_issue_url == ISSUE and s.merged_since_epic_update == 1


def test_k8_a_duplicate_marker_comment_after_the_write_blocks(tmp_state_dir, fake_github):
    landed = fake_github.create_issue_comment

    def crash_after_write(url, body):
        landed(url, body)
        raise RuntimeError("power loss")

    fake_github.create_issue_comment = crash_after_write
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(None)])
    with pytest.raises(RuntimeError, match="power loss"):
        eng.step()
    fake_github.create_issue_comment = landed
    fake_github.add_comment(EPIC, 320, _controller_progress_body())  # a second one

    eng.load()
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    s = load_state(eng.paths.state_file)
    assert comment_url(EPIC, 320) in s.block_reason
    assert len(_progress_posts(fake_github)) == 1 and len(fake_github.comments[EPIC]) == 2


def test_k8_the_attempt_bound_exhausted_blocks_naming_the_effect(tmp_state_dir, fake_github):
    """Two attempts, neither landed: the third entry sends nothing and blocks,
    naming the record and the manual step."""
    lost = GitHubUnavailableError("gh: timed out")
    fake_github.write_failures = [
        ("create_issue_comment", lost, False),
        ("create_issue_comment", lost, False),
    ]
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(None)])
    for _ in range(2):
        with pytest.raises(GitHubUnavailableError, match="'resume' reconciles it"):
            eng.step()
        eng.load()
    assert _k8_records(eng)[0]["attempts"] == 2

    out = eng.step()
    assert out.next_phase == "BLOCKED" and len(eng.provider.calls) == 1
    s = load_state(eng.paths.state_file)
    assert f"progress_comment effect 0 on {EPIC} has used its 2 attempts" in s.block_reason
    assert "perform the write by hand" in s.block_reason and "'unblock'" in s.block_reason
    assert [r["stage"] for r in s.effect_records] == ["conflict"]
    assert len(_progress_posts(fake_github)) == 2 and EPIC not in fake_github.comments
    assert s.current_issue_url == ISSUE and s.merged_since_epic_update == 1


def test_k8_a_transient_failure_during_reconciliation_leaves_the_state_unchanged(
    tmp_state_dir, fake_github
):
    landed = fake_github.create_issue_comment

    def crash_after_write(url, body):
        landed(url, body)
        raise RuntimeError("power loss")

    fake_github.create_issue_comment = crash_after_write
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(None)])
    with pytest.raises(RuntimeError, match="power loss"):
        eng.step()
    fake_github.create_issue_comment = landed
    before = load_state(eng.paths.state_file)

    fake_github.comments_error = GitHubUnavailableError("gh: 502")
    eng.load()
    with pytest.raises(GitHubUnavailableError):
        eng.step()
    after = load_state(eng.paths.state_file)
    assert after.effect_records == before.effect_records
    assert after.completion_context == before.completion_context
    assert after.step_count == before.step_count and after.phase == Phase.UPDATE_EPIC

    fake_github.comments_error = None
    eng.load()
    assert eng.step().next_phase == "DONE"
    assert len(_progress_posts(fake_github)) == 1


# -- UPDATE_EPIC: a run upgraded while a previous-protocol agent was publishing (D13.7) ----
def _as_protocol_5(eng, *, attempt: int) -> None:
    """Rewrite the state file as the previous protocol left it: no effect state."""
    data = json.loads(eng.paths.state_file.read_text())
    data["protocol_version"] = "5"
    data["attempt"] = attempt
    for key in ("effect_records", "entry_observation", "completion_context", "launch_label"):
        data.pop(key, None)
    eng.paths.state_file.write_text(json.dumps(data), encoding="utf-8")


def test_update_epic_legacy_reentry_adopts_the_agents_comment_and_asks_for_the_selection(
    tmp_state_dir, fake_github
):
    """Protocol 5, attempt 1: the agent ran under the contract in which it posted
    the comment. Its comment is adopted once (D13.7), nothing is posted, and the
    relaunch is a re-request for the selection that names the adopted comment."""
    fake_github.add_issue(ISSUE3, "Next")
    eng = _in_update_epic(
        tmp_state_dir, fake_github, [_epic_reselect(ISSUE3, roadmap_section=ROADMAP)]
    )
    _as_protocol_5(eng, attempt=1)
    post_progress_comment(fake_github)  # what the previous-protocol agent published
    eng.load()
    assert eng.state.launch_label == "agent_publishes"

    out = eng.step()
    assert out.next_phase == "ANALYZE_EXECUTE"
    [call] = eng.provider.calls
    assert "# Phase: UPDATE_EPIC (re-request)" in call.prompt
    assert comment_url(EPIC, 300) in call.prompt
    assert "posted by an agent under the previous contract" in call.prompt
    assert _progress_posts(fake_github) == [] and len(fake_github.comments[EPIC]) == 1
    s = load_state(eng.paths.state_file)
    assert s.current_issue_url == ISSUE3 and s.protocol_version == "7"
    assert s.effect_records == []  # adopted with no record (D13.7)


def test_update_epic_legacy_adoption_saved_with_no_record_completes_on_resume(
    tmp_state_dir, fake_github
):
    """The adopted comment's context is saved with an empty plan beside the
    entry observation that adopted it, and the process dies before completion.
    That empty plan is the legitimate one (D13.7): it loads, and the next entry
    completes from it with nothing posted and no agent relaunched."""
    fake_github.add_issue(ISSUE3, "Next")
    eng = _in_update_epic(
        tmp_state_dir, fake_github, [_epic_reselect(ISSUE3, roadmap_section=ROADMAP)]
    )
    _as_protocol_5(eng, attempt=1)
    post_progress_comment(fake_github)
    eng.load()
    eng._complete_update_epic = _crash_on_first_call(eng._complete_update_epic)
    with pytest.raises(RuntimeError, match="power loss"):
        eng.step()
    s = load_state(eng.paths.state_file)
    assert s.effect_records == [] and s.completion_context["next_issue_url"] == ISSUE3
    marker = render_progress_marker(ISSUE, PR)
    assert s.entry_observation["objects"] == {marker: comment_url(EPIC, 300)}

    eng.load()
    out = eng.step()
    assert out.next_phase == "ANALYZE_EXECUTE" and "no agent launched" in out.message
    assert comment_url(EPIC, 300) in out.message
    assert len(eng.provider.calls) == 1 and _progress_posts(fake_github) == []
    assert load_state(eng.paths.state_file).current_issue_url == ISSUE3


def test_update_epic_legacy_state_before_any_launch_does_not_adopt_a_comment(
    tmp_state_dir, fake_github
):
    """Protocol 5, attempt 0: no agent of the previous contract ran for this entry,
    so a marker comment is unexplained and blocks (D9.7), as on any entry."""
    eng = _in_update_epic(tmp_state_dir, fake_github, ["never"])
    _as_protocol_5(eng, attempt=0)
    post_progress_comment(fake_github)
    eng.load()
    out = eng.step()
    assert out.next_phase == "BLOCKED" and eng.provider.calls == []
    assert "D9.7" in load_state(eng.paths.state_file).block_reason


def test_update_epic_legacy_reentry_is_one_shot(tmp_state_dir, fake_github):
    """After the first entry of this version persisted its observation, a
    marker comment found later is no longer the legacy one."""
    eng = _in_update_epic(tmp_state_dir, fake_github, ["junk", "junk"])
    _as_protocol_5(eng, attempt=1)
    eng.load()
    with pytest.raises(ControlResultValidationError):
        eng.step()  # the entry saw no comment, launched, and the result was refused
    post_progress_comment(fake_github, cid=330)
    eng.load()
    out = eng.step()
    assert out.next_phase == "BLOCKED" and len(eng.provider.calls) == 2
    reason = load_state(eng.paths.state_file).block_reason
    assert comment_url(EPIC, 330) in reason and "D9.7" in reason


# -- UPDATE_EPIC dry-run: no effect, no read, no git -----------------------------------
class _NoGitHub:
    """Fails the test on any use of the GitHub client (dry-run reads persisted state only)."""

    def __getattr__(self, name: str):
        raise AssertionError(f"dry-run touched GitHub: {name!r}")


def test_update_epic_dry_run_executes_no_effect_and_names_the_planned_one(
    tmp_state_dir, fake_github
):
    eng = _in_update_epic(tmp_state_dir, fake_github, ["never"])
    eng._github = _NoGitHub()
    before = eng.paths.state_file.read_bytes()
    out = eng.step(dry_run=True)
    notes = "\n".join(out.plan.notes)
    assert "would post the agent's progress text on the EPIC itself" in notes
    assert render_progress_marker(ISSUE, PR) in notes
    assert eng.provider.calls == [] and eng.paths.state_file.read_bytes() == before


def test_update_epic_dry_run_names_the_record_it_would_reconcile(tmp_state_dir, fake_github):
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(None)])
    eng._persist_effect = _crash_on_first_call(eng._persist_effect)
    with pytest.raises(RuntimeError, match="power loss"):
        eng.step()
    eng.load()
    writes = list(fake_github.effect_writes)
    eng._github = _NoGitHub()
    before = eng.paths.state_file.read_bytes()
    out = eng.step(dry_run=True)
    notes = "\n".join(out.plan.notes)
    assert "would reconcile" in notes and "(intended, 0 attempt(s))" in notes
    assert "would complete from the persisted UPDATE_EPIC result without launching" in notes
    assert eng.paths.state_file.read_bytes() == before
    assert fake_github.effect_writes == writes and len(eng.provider.calls) == 1


# -- loop bounds: review-round cap, stagnation, step budget (#9) ------------------------------
def _loop_agent(
    gh: FakeGitHub,
    findings_for_round,
    seen: list[str] | None = None,
    pushed: list[str] | None = None,
):
    """Scripted ANALYZE_EXECUTE / REVIEW / FIX loop over FakeGitHub and its origin.

    ``findings_for_round(n)`` returns the findings review round ``n`` reports
    (``[]`` == clean). Every FIX commits a new distinct HEAD (appended to
    ``pushed``; the controller pushes it, #163) and resolves every open
    finding as ``fixed`` — the runaway loop from the issue's evidence. The
    reviewer posts nothing: the controller posts each round's comment (#162).
    """
    rounds = {"n": 0}
    seen = seen if seen is not None else []
    pushed = pushed if pushed is not None else []

    def agent(req):
        seen.append(req.phase)
        if req.phase == "ANALYZE_EXECUTE":
            return implement(req)
        if req.phase == "REVIEW":
            rounds["n"] += 1
            rnd = rounds["n"]
            sha = gh.prs[PR].head_sha
            return block(review_payload(rnd, sha, findings_for_round(rnd)))
        if req.phase == "FIX":
            ids = re.findall(r"^- (R\d+-F\d+) \[", req.prompt, re.M)
            new = fix_commit(req, f"Fix round {rounds['n']} (#2)")
            pushed.append(new)
            return block(fix_payload(reviewed_head_in(req.prompt), new, [fixed(i) for i in ids]))
        raise AssertionError(f"unexpected call {req.phase}")

    return agent


def _one_finding_per_round(rnd: int, text: str | None = None) -> list[dict]:
    f = _finding(rnd)
    f["required_resolution"] = text if text is not None else f"resolution for round {rnd}"
    return [f]


def _no_stagnation(cfg) -> None:
    cfg.workflow.stagnation_identical_rounds = 0
    cfg.workflow.stagnation_unchanged_count_rounds = 0


def test_review_round_cap_blocks_with_findings_and_never_starts_the_last_fix(tmp_state_dir):
    """Round N == cap still has findings -> BLOCKED; no FIX whose result could never be reviewed."""
    gh = FakeGitHub()
    seen: list[str] = []
    pushed: list[str] = []
    eng = make_engine(
        tmp_state_dir,
        _loop_agent(gh, _one_finding_per_round, seen, pushed),
        github=gh,
        origin=True,
    )
    eng.config.workflow.max_review_rounds = 3
    _no_stagnation(eng.config)
    eng._save()
    outcomes = eng.run(max_steps=50)
    phases = [o.next_phase for o in outcomes]
    assert phases == ["ANALYZE_EXECUTE", "REVIEW", "FIX", "REVIEW", "FIX", "REVIEW", "BLOCKED"]
    assert seen == ["ANALYZE_EXECUTE", "REVIEW", "FIX", "REVIEW", "FIX", "REVIEW"]
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.BLOCKED and s.review_round == 3
    assert "workflow.max_review_rounds=3" in s.block_reason and "3 finding" not in s.block_reason
    assert "round 3: 1 finding(s)" in s.block_reason
    assert s.open_findings[0]["id"] == "R3-F1"  # kept for the human
    assert len(pushed) == 2
    assert s.last_review_result == "needs_fix" and s.reviewed_head_sha == pushed[1]
    assert [r["result"] for r in s.review_history] == ["needs_fix"] * 3
    assert gh.prs[PR].state == "OPEN" and gh.merges == []


def test_review_round_cap_does_not_block_a_clean_last_round(tmp_state_dir):
    gh = FakeGitHub()
    eng = make_engine(
        tmp_state_dir,
        _loop_agent(gh, lambda rnd: _one_finding_per_round(rnd) if rnd < 2 else []),
        github=gh,
        origin=True,
    )
    eng.config.workflow.max_review_rounds = 2
    eng._save()
    outcomes = eng.run(max_steps=50)
    assert [o.next_phase for o in outcomes] == [
        "ANALYZE_EXECUTE",
        "REVIEW",
        "FIX",
        "REVIEW",
        "READY_FOR_MERGE",
    ]
    s = load_state(eng.paths.state_file)
    assert s.review_round == 2 and s.last_review_result == "clean"
    assert [r["result"] for r in s.review_history] == ["needs_fix", "clean"]


def test_review_entry_at_cap_blocks_without_binding_head_or_invoking_reviewer(tmp_state_dir):
    """Whatever led to REVIEW with review_round == cap (stale re-review, HEAD drift, resume)."""
    gh = FakeGitHub()
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(3, SHA_A, []))], round_done=2)
    eng.config.workflow.max_review_rounds = 2
    eng.state.last_review_result = "stale"
    eng._save()
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "review round 3 is not started" in out.message
    assert eng.provider.calls == []
    assert ("get_pr", PR) not in gh.calls  # no HEAD binding either
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.BLOCKED and s.review_round == 2 and s.step_count == 0
    assert "workflow.max_review_rounds=2" in s.block_reason


def test_review_stagnation_identical_resolutions_blocks(tmp_state_dir):
    """A FIX that moves the PR HEAD is not progress. Round 2 reviews the new
    HEAD and asks for the same resolution again (new finding id, different case
    and spacing) -> BLOCKED on the open PR, and no second FIX is launched."""
    gh = FakeGitHub()
    seen: list[str] = []
    pushed: list[str] = []
    texts = {1: "Add a regression test", 2: "  add   A REGRESSION test  "}
    agent = _loop_agent(gh, lambda rnd: _one_finding_per_round(rnd, texts[rnd]), seen, pushed)
    eng = make_engine(tmp_state_dir, agent, github=gh, origin=True)
    # Below the replan soft threshold the verdict is the loop guard's to act on.
    assert eng.config.review.replan.soft_threshold > 2
    outcomes = eng.run(max_steps=50)
    assert [o.next_phase for o in outcomes] == [
        "ANALYZE_EXECUTE",
        "REVIEW",
        "FIX",
        "REVIEW",
        "BLOCKED",
    ]
    assert seen == ["ANALYZE_EXECUTE", "REVIEW", "FIX", "REVIEW"]
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.BLOCKED
    assert s.review_round == 2 and "identical resolutions" in s.block_reason
    assert "stagnation_identical_rounds=2" in s.block_reason
    # The FIX really moved the PR, and each round is recorded at the HEAD it reviewed ...
    implemented = git_out(f"--git-dir={eng.origin.bare}", "rev-parse", f"{pushed[0]}^")
    assert gh.prs[PR].head_sha == eng.origin.head("autoforge/2") == pushed[0]
    assert pushed[0] != implemented
    assert [r["reviewed_head_sha"] for r in s.review_history] == [implemented, pushed[0]]
    assert [r["result"] for r in s.review_history] == ["needs_fix", "needs_fix"]
    # ... while the demand, once normalised, is the one round 1 already made.
    assert s.review_history[0]["fingerprint"] == s.review_history[1]["fingerprint"]
    # The PR and the finding are kept for the human: nothing closed, merged or replaced.
    assert [(f["id"], f["required_resolution"]) for f in s.open_findings] == [
        ("R2-F1", "add   A REGRESSION test")
    ]
    assert gh.prs[PR].state == "OPEN" and gh.closed_prs == [] and gh.merges == []
    assert s.replan_transaction == {}


def test_review_stagnation_unchanged_count_blocks_a_ping_pong(tmp_state_dir):
    """The issue's ping-pong (apply X / revert X / apply X): one finding per round, the
    same demand keeps coming back -> BLOCKED after 3 rounds."""
    gh = FakeGitHub()
    texts = {1: "apply refactor X", 2: "revert refactor X", 3: "Apply refactor X"}
    agent = _loop_agent(gh, lambda rnd: _one_finding_per_round(rnd, texts[rnd]))
    eng = make_engine(tmp_state_dir, agent, github=gh, origin=True)
    eng._save()
    outcomes = eng.run(max_steps=50)
    assert [o.next_phase for o in outcomes][-3:] == ["FIX", "REVIEW", "BLOCKED"]
    s = load_state(eng.paths.state_file)
    assert s.review_round == 3 and "finding count has not changed" in s.block_reason
    assert "rounds 1, 2, 3" in s.block_reason and "1 required resolution(s) recur" in s.block_reason
    assert len({r["fingerprint"] for r in s.review_history}) == 2  # A, B, A
    assert all(len(r["resolutions"]) == 1 for r in s.review_history)


def test_review_one_new_finding_per_round_is_progress_not_stagnation(tmp_state_dir):
    """A reviewer that raises a fresh finding every round (each earlier one fixed) is
    bounded by the round cap only: the unchanged count alone is not stagnation."""
    gh = FakeGitHub()
    eng = make_engine(
        tmp_state_dir, _loop_agent(gh, _one_finding_per_round), github=gh, origin=True
    )
    eng.config.workflow.max_review_rounds = 4
    eng._save()  # defaults: identical=2, unchanged_count=3
    outcomes = eng.run(max_steps=50)
    assert [o.next_phase for o in outcomes][-3:] == ["FIX", "REVIEW", "BLOCKED"]
    s = load_state(eng.paths.state_file)
    assert s.review_round == 4 and "workflow.max_review_rounds=4" in s.block_reason
    assert "finding count" not in s.block_reason
    assert len({r["fingerprint"] for r in s.review_history}) == 4  # all texts differed


def test_review_progress_is_not_stagnation(tmp_state_dir):
    """Decreasing finding counts with different texts run to the clean round."""
    gh = FakeGitHub()
    per_round = {1: 3, 2: 2, 3: 1, 4: 0}

    def findings(rnd):
        return [
            dict(_finding(rnd, n), required_resolution=f"r{rnd} fix {n}")
            for n in range(1, per_round[rnd] + 1)
        ]

    eng = make_engine(tmp_state_dir, _loop_agent(gh, findings), github=gh, origin=True)
    eng._save()
    outcomes = eng.run(max_steps=50)
    assert outcomes[-1].next_phase == "READY_FOR_MERGE"
    s = load_state(eng.paths.state_file)
    assert s.review_round == 4
    assert [r["finding_count"] for r in s.review_history] == [3, 2, 1, 0]


def test_review_stale_round_is_recorded_and_breaks_the_stagnation_streak(tmp_state_dir):
    """needs_fix(A) -> stale(A) -> needs_fix(A): no fixer ever saw the stale
    round's demand, so it ends the streak instead of extending it and the
    round after it goes to FIX."""
    gh = FakeGitHub()

    def on_call(req):
        if gh.prs[PR].head_sha == SHA_A:
            gh.set_head(SHA_B)  # someone pushed while the reviewer was working
            return block(review_payload(2, SHA_A, _one_finding_per_round(2, "same text")))
        return block(review_payload(3, SHA_B, _one_finding_per_round(3, "same text")))

    eng = _in_review(tmp_state_dir, gh, on_call, round_done=1)
    eng.state.review_history = [
        review_record(1, SHA_A, RESULT_NEEDS_FIX, _one_finding_per_round(1, "same text"))
    ]
    out = eng.step()
    assert out.next_phase == "REVIEW" and "moved" in out.message  # not BLOCKED
    s = load_state(eng.paths.state_file)
    assert [r["result"] for r in s.review_history] == ["needs_fix", "stale"]
    assert s.review_history[1]["reviewed_head_sha"] == SHA_A and s.open_findings == []

    # The same demand at the HEAD that replaced the reviewed one. Had the stale
    # round counted, this would be the second identical needs-fix round in a row.
    assert eng.step().next_phase == "FIX"
    s = load_state(eng.paths.state_file)
    assert [r["result"] for r in s.review_history] == ["needs_fix", "stale", "needs_fix"]
    assert len({r["fingerprint"] for r in s.review_history}) == 1


def test_failed_review_invocation_consumes_neither_round_nor_history(tmp_state_dir):
    """A review the controller cannot accept (here: no summary, through the
    correction too) is no round: nothing is posted and nothing is recorded."""
    gh = FakeGitHub()
    incomplete = review_payload(1, SHA_A, [_finding(1)])
    del incomplete["summary"]
    eng = _in_review(tmp_state_dir, gh, lambda req: block(incomplete))
    with pytest.raises(ControlResultValidationError, match="did not return a valid"):
        eng.step()
    s = load_state(eng.paths.state_file)
    assert s.review_round == 0 and s.review_history == [] and s.phase == Phase.REVIEW
    assert s.last_review_comment_url == "" and s.effect_records == []
    assert gh.effect_writes == [] and gh.comments.get(PR, []) == []


def test_review_refused_by_the_post_review_pr_read_consumes_neither_round_nor_history(
    tmp_state_dir,
):
    """The controller posts the round's comment, but the PR was closed under the
    reviewer and the controller's re-read refuses it. That failed verification
    is persisted, so nothing of the round may be: once the PR is open again,
    round 1 is still the round to complete (from the saved plan: the reviewer
    is not relaunched and the comment is not posted again), and it enters the
    history exactly once."""
    gh = FakeGitHub()
    payload = block(review_payload(1, SHA_A, [_finding(1)]))

    def closed_under_the_reviewer(req):
        gh.prs[PR].state = "CLOSED"
        return payload

    eng = _in_review(tmp_state_dir, gh, closed_under_the_reviewer)
    with pytest.raises(VerificationError, match="is CLOSED"):
        eng.step()
    s = load_state(eng.paths.state_file)
    assert s.review_round == 0 and s.review_history == [] and s.phase == Phase.REVIEW
    assert s.reviewed_head_sha == "" and s.last_review_comment_url == ""
    assert s.last_review_needs_fix is None and s.open_findings == []
    assert len(s.verification_failures) == 1  # the attempt itself stays on record
    eng.close()

    gh.prs[PR].state = "OPEN"  # a human reopened it
    resumed = make_engine(tmp_state_dir, [payload], github=gh)
    resumed.load()
    assert resumed.step().next_phase == "FIX"
    assert resumed.provider.calls == [] and len(gh.effect_writes) == 1
    s = load_state(resumed.paths.state_file)
    assert s.review_round == 1 and [r["round"] for r in s.review_history] == [1]
    assert s.last_review_comment_url == controller_review_comment(resumed, 1).url


def test_new_pr_resets_review_history(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, implement, github=fake_github, origin=True)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    eng.state.review_history = [review_record(1, SHA_B, RESULT_NEEDS_FIX, [_finding(1)])]
    eng.state.review_round = 1
    eng.state.open_findings = [_finding(1)]
    eng.state.prior_findings = [_finding(1)]
    eng.state.last_review_result = "needs_fix"
    assert eng.step().next_phase == "REVIEW"
    assert eng.state.review_history == [] and eng.state.review_round == 0
    assert eng.state.open_findings == [] and eng.state.prior_findings == []
    assert eng.state.last_review_result == ""


def test_step_budget_is_cumulative_and_survives_resume(tmp_state_dir):
    """`resume` continues the persisted step_count; it never restarts the budget."""
    gh = FakeGitHub()
    seen: list[str] = []
    agent = _loop_agent(gh, _one_finding_per_round, seen)
    eng = make_engine(tmp_state_dir, agent, github=gh, origin=True)
    eng.config.workflow.max_total_steps = 4
    _no_stagnation(eng.config)
    eng._save()
    first = eng.run(max_steps=2)
    assert [o.next_phase for o in first] == ["ANALYZE_EXECUTE", "REVIEW"]
    assert load_state(eng.paths.state_file).step_count == 2

    # a fresh engine, as `resume` builds it: state (and the budget) come from disk
    resumed = make_engine(tmp_state_dir, agent, github=gh, cfg=eng.config)
    connect_origin(resumed, eng.origin)
    resumed.load()
    second = resumed.run(max_steps=50)
    assert [o.next_phase for o in second] == ["FIX", "REVIEW", "BLOCKED"]
    s = load_state(resumed.paths.state_file)
    assert s.phase == Phase.BLOCKED and s.step_count == 4  # the blocked step did not count
    assert "workflow.max_total_steps=4" in s.block_reason
    assert "not reset by 'resume'" in s.block_reason
    assert seen == ["ANALYZE_EXECUTE", "REVIEW", "FIX"]  # blocked before invoking round 2
    # the run is terminal: another resume executes nothing more
    with pytest.raises(StateTransitionError):
        resumed.step()


def test_step_budget_applies_to_deterministic_steps_too(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, [], github=fake_github)
    eng.config.workflow.max_total_steps = 1
    eng.state.step_count = 1
    out = eng.step()  # INITIALIZING would be step 2
    assert out.next_phase == "BLOCKED" and fake_github.calls == []
    assert eng.state.step_count == 1


def test_a_transient_entry_failure_is_not_a_step_and_is_not_charged(tmp_state_dir):
    """A step is charged when it first persists; an entry read that fails transiently never does.

    Before the launch checkpoint (or, for a step that needs no agent, the
    step's resolution) the incremented count exists only in memory, so a
    GitHub outage at the phase entry leaves the state file byte-identical and
    ``resume`` re-reads with the same budget. Once GitHub answers, the launch
    checkpoint charges the step before the agent runs. The REPLAN_REEXECUTE
    budget exemption inherits this rule rather than adding one (#71).
    """
    gh = FakeGitHub()
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(1, SHA_A, []))])
    eng.config.workflow.max_total_steps = 5
    eng.state.step_count = 4  # the last step the budget allows
    eng._save()
    before = eng.paths.state_file.read_bytes()

    gh.get_pr_failures = 1  # the HEAD binding at the REVIEW entry
    with pytest.raises(GitHubUnavailableError):
        eng.step()
    assert eng.paths.state_file.read_bytes() == before
    assert eng.provider.calls == []

    resumed = make_engine(tmp_state_dir, [block(review_payload(1, SHA_A, []))], github=gh)
    resumed.load()
    assert resumed.state.step_count == 4
    assert resumed.step().next_phase == "READY_FOR_MERGE"
    assert len(resumed.provider.calls) == 1
    assert load_state(resumed.paths.state_file).step_count == 5  # counted once


def test_dry_run_plan_reports_loop_bounds(tmp_state_dir, fake_github):
    gh = fake_github
    eng = _in_review(tmp_state_dir, gh, [], round_done=2)
    eng.config.workflow.max_review_rounds = 2
    eng.config.workflow.max_total_steps = 10
    eng.state.step_count = 10
    plan = eng.step(dry_run=True).plan
    assert plan is not None
    notes = "\n".join(plan.notes)
    assert "would enter BLOCKED without invoking the reviewer" in notes
    assert "would enter BLOCKED without executing" in notes and "max_total_steps=10" in notes
    assert "review round 3 of at most 2" in notes
    assert eng.state.phase == Phase.REVIEW and not eng.paths.state_file.exists()


# -- REVIEW payload bounds (#34) -------------------------------------------------------
def _oversized_review(rnd: int, sha: str) -> dict:
    findings = [_finding(rnd, n) for n in range(1, MAX_FINDINGS_PER_REVIEW + 2)]
    return review_payload(rnd, sha, findings)


def test_oversized_review_is_rejected_and_corrected(tmp_state_dir, fake_github):
    """Too many findings: the round is refused whole and the reviewer re-emits;
    the controller posts only the corrected round."""

    def agent(req):
        if not req.correction:
            return block(_oversized_review(1, SHA_A))
        return block(review_payload(1, SHA_A, [_finding(1, 1)]))

    eng = _in_review(tmp_state_dir, fake_github, agent)
    out = eng.step()
    assert out.next_phase == "FIX"
    assert len(eng.provider.calls) == 2
    second = eng.provider.calls[1]
    assert second.correction is True
    assert f"at most {MAX_FINDINGS_PER_REVIEW} per review round" in second.prompt
    assert [f["id"] for f in eng.state.open_findings] == ["R1-F1"]
    assert eng.state.review_round == 1
    assert len(fake_github.effect_writes) == 1


def test_oversized_resolution_never_reaches_state_or_a_prompt(tmp_state_dir, fake_github):
    """One finding past the text bound is refused; state and its file are untouched."""
    huge = ("resolve everything " * 200).strip()  # far past MAX_FINDING_RESOLUTION_CHARS
    assert len(huge) > MAX_FINDING_RESOLUTION_CHARS
    payload = review_payload(1, SHA_A, [dict(_finding(1, 1), required_resolution=huge)])
    eng = _in_review(tmp_state_dir, fake_github, [block(payload)])
    eng.config.execution.max_correction_attempts = 0
    eng._save()
    before = eng.paths.state_file.read_bytes()

    with pytest.raises(ControlResultValidationError) as excinfo:
        eng.step()
    msg = str(excinfo.value)
    assert f"is {len(huge)} characters" in msg
    assert f"at most {MAX_FINDING_RESOLUTION_CHARS}" in msg
    assert "resolve everything" not in msg  # size, never the text

    after = load_state(eng.paths.state_file)
    assert after.phase == Phase.REVIEW and after.review_round == 0
    assert after.open_findings == [] and after.review_history == []
    # The only differences on disk are the counters and the launch label
    # (ADR 0004 D13.3) of the refused launch.
    persisted = json.loads(eng.paths.state_file.read_bytes())
    for volatile in ("updated_at",):
        persisted.pop(volatile)
    expected = json.loads(before) | {
        "attempt": 1,
        "step_count": 1,
        "launch_label": "controller_publishes",
    }
    expected.pop("updated_at")
    assert persisted == expected
    assert "resolve everything" not in eng.paths.state_file.read_text()
    assert fake_github.effect_writes == []
    eng.state.phase = Phase.FIX
    assert "resolve everything" not in eng.render_prompt_for(Phase.FIX)


# -- FIX payload bounds (#77) --------------------------------------------------------
def test_oversized_fix_rationale_is_refused_and_corrected(tmp_state_dir):
    """A FIX past the rationale bound is refused whole; the correction prompt
    names the limit and never the text; the corrected FIX is applied."""
    gh = FakeGitHub()
    huge = ("because " * 400).strip()
    assert len(huge) > MAX_FIX_RATIONALE_CHARS
    fine = "The behaviour is already covered by the existing suite; nothing to change."

    def agent(req):
        rationale = fine if req.correction else huge
        return fixer(no_change("R1-F1", rationale), commit=False)(req)

    eng = _in_fix(tmp_state_dir, gh, agent, origin=True)
    out = eng.step()
    assert out.next_phase == "REVIEW"
    assert len(eng.provider.calls) == 2
    second = eng.provider.calls[1]
    assert second.correction is True
    assert f"is {len(huge)} characters" in second.prompt
    assert f"at most {MAX_FIX_RATIONALE_CHARS}" in second.prompt
    assert "because because" not in second.prompt
    assert eng.state.last_fix_resolutions[0]["rationale"] == fine
    assert "because because" not in eng.paths.state_file.read_text()


# The largest REVIEW payloads the parser accepts, each the most expensive
# along one axis of the FIX renderer (PR #76 review, R2-F2). Every field is
# filled to its bound; the head/tail letters keep the stripped length at the
# bound when the filler is whitespace (a newline, U+2028).
_WORST_CASE_FILLERS = {
    "plain": "r",
    "backticks": "`",  # the fence must outgrow the longest backtick run
    "newlines": "\n",  # a newline inside required_resolution is indented
    "control": "\x00",  # escape_inline renders it as ``\\x00`` (4 characters)
    "line_separator": "\u2028",  # the widest escape, ``\\u2028`` (6 characters)
}
# #78: the parser refuses a control character in a one-line field and any
# but a newline or tab in a resolution, so only these shapes reach state
# through it; the others are seeded into state directly below, standing for
# state written before #78 or edited by hand, which the renderer must still
# contain.
_PARSER_ACCEPTS = {
    "plain",
    "backticks",
}


def _filled(filler: str, length: int) -> str:
    return "a" + filler * (length - 2) + "b"


@pytest.mark.parametrize("filler", sorted(_WORST_CASE_FILLERS), ids=str)
def test_fix_prompt_size_is_bounded_by_the_review_bounds(tmp_state_dir, fake_github, filler):
    """The largest round the parser accepts renders into a FIX prompt of bounded size.

    The renderer's safety measures each cost characters: the fence grows past
    the longest backtick run in the findings, a control character in a
    one-line field becomes its escape, and a newline in a resolution is
    indented under its finding. The bound is therefore a constant factor of
    the parser bounds, not the sum of them, and it holds for every shape of
    content the renderer defends against, not only plain text.

    Since #78 the parser itself refuses a control character in ``title`` or
    ``location`` and any but a newline or tab in ``required_resolution``, so
    those shapes are asserted refused and then seeded into state directly:
    the renderer's escape and indentation remain the defence for state a
    controller without that rule persisted, or a hand-edited state file.
    """
    from autoforge.result_parser import Finding, ReviewResult

    ch = _WORST_CASE_FILLERS[filler]
    findings = [
        dict(
            _finding(1, n),
            # ``R1-F<n>`` padded to the id bound; distinct because the tail differs.
            id="R1-F" + "9" * (MAX_FINDING_ID_CHARS - 4 - len(str(n))) + str(n),
            required_resolution=_filled(ch, MAX_FINDING_RESOLUTION_CHARS),
            title=_filled(ch, MAX_FINDING_TITLE_CHARS),
            location=_filled(ch, MAX_FINDING_LOCATION_CHARS),
        )
        for n in range(1, MAX_FINDINGS_PER_REVIEW + 1)
    ]
    assert all(len(f["id"]) == MAX_FINDING_ID_CHARS for f in findings)
    if filler in _PARSER_ACCEPTS:
        # The parser accepts this payload whole: the bound below is about
        # content the FIX round can actually be handed, not content refused.
        accepted = ReviewResult.from_payload(review_payload(1, SHA_A, findings)).findings
    else:
        with pytest.raises(ControlResultValidationError, match="contains a control character"):
            ReviewResult.from_payload(review_payload(1, SHA_A, findings))
        accepted = [Finding(**f) for f in findings]
    assert len(accepted) == MAX_FINDINGS_PER_REVIEW
    assert len(accepted[0].required_resolution) == MAX_FINDING_RESOLUTION_CHARS

    eng = _in_review(tmp_state_dir, fake_github, [])
    eng.state.phase = Phase.FIX
    eng.state.review_round = 1
    eng.state.reviewed_head_sha = SHA_A
    eng.state.open_findings = []
    empty = len(eng.render_prompt_for(Phase.FIX))
    eng.state.open_findings = [f.to_dict() for f in accepted]
    full = eng.render_prompt_for(Phase.FIX)

    escape_width = 6  # escape_inline: a control character renders as \\xNN or \\uNNNN
    indent_width = 5  # a newline inside required_resolution renders as "\n" + 4 spaces
    framing = 64  # classification, separators, the "Required resolution:" label
    # The follow-up section renders one more line per finding: the id and
    # the existing issue URL or "(none)", each bounded by the id and URL
    # bounds (#163: no marker is rendered; the controller writes it).
    follow_up_line = MAX_FINDING_ID_CHARS + MAX_URL_CHARS + framing
    per_finding = (
        MAX_FINDING_ID_CHARS  # the id's shape admits no control character
        + escape_width * (MAX_FINDING_TITLE_CHARS + MAX_FINDING_LOCATION_CHARS)
        + indent_width * MAX_FINDING_RESOLUTION_CHARS
        + framing
        + follow_up_line
    )
    # Opening and closing fence, one longer than the longest possible run.
    longest_field = max(
        MAX_FINDING_RESOLUTION_CHARS, MAX_FINDING_TITLE_CHARS, MAX_FINDING_LOCATION_CHARS
    )
    fence_overhead = 2 * (longest_field + 1)
    assert len(full) - empty <= MAX_FINDINGS_PER_REVIEW * per_finding + fence_overhead
    # One rendered line per finding in the findings list and one in the
    # follow-up section, nothing else.
    assert full.count("\n- R1-F") == 2 * MAX_FINDINGS_PER_REVIEW
    if ch in ("\x00", "\u2028"):
        # The one-line fields were escaped, not passed through; the resolution
        # is block-quoted, so the fence, not an escape, is what contains it.
        heads = [line for line in full.split("\n") if line.startswith("- R1-F")]
        assert len(heads) == 2 * MAX_FINDINGS_PER_REVIEW  # findings + follow-up section
        assert not any(ch in line for line in heads)


# -- bounded stdout capture (#53) ---------------------------------------------------------
class _TruncatedOutputProvider(ScriptedProvider):
    """Returns what the executor returns for an agent whose stdout passed the
    capture bound: ``head + marker + tail`` with the tail offset recorded.
    ``head`` and ``tail`` are text, or handlers called with the request (such
    as :func:`implement`, the agent's commit in its worktree and its result)."""

    def __init__(self, head, tail) -> None:
        super().__init__()
        self.marker = "\n[autoforge: 999 bytes of stdout omitted; ...]\n"
        self.head, self.tail = head, tail

    def execute(self, req):
        self.calls.append(req)
        head = self.head(req) if callable(self.head) else self.head
        tail = self.tail(req) if callable(self.tail) else self.tail
        return AgentExecutionResult(
            command=["x"],
            exit_code=0,
            stdout=head + self.marker + tail,
            stderr="",
            started_at="t",
            finished_at="t",
            stdout_truncated=True,
            stdout_tail_offset=len(head) + len(self.marker),
        )


def _install(eng, provider):
    eng.providers._overrides = {"claude": provider, "opencode": provider}
    eng.provider = provider


def test_truncated_stdout_accepts_a_block_that_lies_in_the_tail(tmp_state_dir, fake_github):
    """The block is the last thing on stdout, so the kept tail preserves it;
    the whole (marked) capture is what reaches stdout.log."""
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    provider = _TruncatedOutputProvider(
        "runaway logs " * 100, lambda req: "last logs\n" + implement(req)
    )
    _install(eng, provider)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    out = eng.step()
    assert out.next_phase == "REVIEW" and len(provider.calls) == 1
    run_dir = eng.paths.logs_dir / eng.state.run_id
    step = next(p for p in run_dir.iterdir() if p.is_dir())
    assert "bytes of stdout omitted" in (step / "stdout.log").read_text(encoding="utf-8")
    execution = json.loads((step / "execution.json").read_text(encoding="utf-8"))
    assert execution["stdout_truncated"] is True and execution["stderr_truncated"] is False
    event = json.loads((run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert event["stdout_truncated"] is True


# -- what an invocation left behind (#85) ------------------------------------------
class _LeftoverProvider(ScriptedProvider):
    """Returns what the executor returns for an agent that left processes
    behind (killed after its exit) or whose kill was not clean. ``stdout`` is
    text, or a handler called with the request (such as :func:`implement`)."""

    def __init__(self, stdout, exit_code=0, **facts) -> None:
        super().__init__()
        self.stdout, self.facts = stdout, facts
        self.exit_code = -1 if facts.get("timed_out") else exit_code

    def execute(self, req):
        self.calls.append(req)
        return AgentExecutionResult(
            command=["x"],
            exit_code=self.exit_code,
            stdout=self.stdout(req) if callable(self.stdout) else self.stdout,
            stderr="boom",
            started_at="t",
            finished_at="t",
            **self.facts,
        )


def test_descendants_killed_after_a_clean_exit_keeps_the_result_and_is_journaled(
    tmp_state_dir, fake_github
):
    """The agent's own exit and CONTROL_RESULT are what count; that its
    leftovers were killed is recorded, not an error (#85, point 1)."""
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    provider = _LeftoverProvider(implement, descendants_killed=True)
    _install(eng, provider)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    out = eng.step()
    assert out.next_phase == "REVIEW" and len(provider.calls) == 1
    run_dir = eng.paths.logs_dir / eng.state.run_id
    step = next(p for p in run_dir.iterdir() if p.is_dir())
    execution = json.loads((step / "execution.json").read_text(encoding="utf-8"))
    assert execution["descendants_killed"] is True
    assert execution["group_survived_kill"] is False
    assert execution["capture_abandoned"] is False
    assert execution["error"] == "" and not (step / "error.txt").exists()
    event = json.loads((run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert event["descendants_killed"] is True and event["group_survived_kill"] is False


def test_agent_orphan_facts_are_journaled_and_named(tmp_state_dir, fake_github):
    """#132: what the agent left outside its group is recorded with the
    other leftover facts, and its sentence follows a failed exit."""
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    provider = _LeftoverProvider(
        "", exit_code=2, orphans_killed=True, orphan_survived_kill=True, orphans_unchecked=False
    )
    _install(eng, provider)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises(ExecutionError, match="still alive after SIGKILL"):
        eng.step()
    run_dir = eng.paths.logs_dir / eng.state.run_id
    step = next(p for p in run_dir.iterdir() if p.is_dir())
    execution = json.loads((step / "execution.json").read_text(encoding="utf-8"))
    assert execution["orphans_killed"] is True and execution["orphan_survived_kill"] is True
    assert execution["orphans_unchecked"] is False
    assert "outside its process group" in execution["error"]
    event = json.loads((run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert event["orphans_killed"] is True and event["orphan_survived_kill"] is True


def test_timeout_names_a_group_member_that_survived_the_kill(tmp_state_dir, fake_github):
    """#85 addendum: 'was killed' must not read as 'is gone'. A member still
    in the group after SIGKILL is named in the error, the journal and the
    run log so the operator looks for the leftover before resuming."""
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    provider = _LeftoverProvider("", timed_out=True, group_survived_kill=True)
    _install(eng, provider)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises(ExecutionTimeoutError) as excinfo:
        eng.step()
    message = str(excinfo.value)
    assert "was killed (its process group still had a member after SIGKILL" in message
    assert message.endswith("then 'resume'.")
    assert eng.state.phase == Phase.ANALYZE_EXECUTE
    run_dir = eng.paths.logs_dir / eng.state.run_id
    step = next(p for p in run_dir.iterdir() if p.is_dir())
    execution = json.loads((step / "execution.json").read_text(encoding="utf-8"))
    assert execution["timed_out"] is True and execution["group_survived_kill"] is True
    assert "still had a member after SIGKILL" in execution["error"]
    assert "still had a member after SIGKILL" in (step / "error.txt").read_text(encoding="utf-8")
    event = json.loads((run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert event["group_survived_kill"] is True


def test_an_idle_timeout_names_the_limit_and_the_last_activity(tmp_state_dir, fake_github):
    """#193: the error, the run log and the progress line say the agent made
    no progress for its idle limit, and when it was last seen writing."""
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    provider = _LeftoverProvider(
        "", timed_out=True, timeout_limit="idle", last_activity_at="2026-10-07T08:31:02+00:00"
    )
    _install(eng, provider)
    lines: list[str] = []
    eng.progress_output = lines.append
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises(ExecutionTimeoutError) as excinfo:
        eng.step()
    assert str(excinfo.value) == (
        "agent 'analyze_execute' made no progress for 900s (last activity 08:31:02 UTC) "
        "and was killed. State unchanged — inspect the real Git/GitHub state, then 'resume'."
    )
    (req,) = provider.calls
    assert (req.idle_timeout_seconds, req.max_runtime_seconds) == (900, None)
    assert eng.state.phase == Phase.ANALYZE_EXECUTE
    _, step = _step_dir(eng)
    execution = json.loads((step / "execution.json").read_text(encoding="utf-8"))
    assert execution["timed_out"] is True and execution["timeout_limit"] == "idle"
    assert execution["idle_timeout_seconds"] == 900 and execution["max_runtime_seconds"] is None
    assert execution["last_activity_at"] == "2026-10-07T08:31:02+00:00"
    assert execution["error"] == "made no progress for 900s (last activity 08:31:02 UTC)"
    request = json.loads((step / "request.json").read_text(encoding="utf-8"))
    assert request["idle_timeout_seconds"] == 900 and request["max_runtime_seconds"] is None
    shown = [line.split("] ", 1)[1] for line in lines]
    assert "idle timeout 900s, max runtime unset, loop detection warn, attempt 1" in shown[0]
    assert shown[-1].startswith("agent made no progress for 900s (last activity 08:31:02 UTC)")


def test_an_idle_timeout_before_any_output_says_so(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    _install(eng, _LeftoverProvider("", timed_out=True, timeout_limit="idle"))
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises(
        ExecutionTimeoutError,
        match=r"^agent 'analyze_execute' made no progress for 900s \(no output since launch\)",
    ):
        eng.step()


@pytest.mark.parametrize(
    ("ceiling", "expected"),
    [
        (3600, "reached its 3600s maximum runtime"),
        (None, "reached the 604800s (one week) runtime backstop"),
    ],
)
def test_a_maximum_runtime_timeout_names_the_ceiling(tmp_state_dir, fake_github, ceiling, expected):
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    eng.config.execution.default_max_runtime_seconds = ceiling
    eng.config.profile("analyze_execute").idle_timeout_seconds = 120
    provider = _LeftoverProvider(
        "",
        timed_out=True,
        timeout_limit="max_runtime",
        last_activity_at="2026-10-07T08:31:02+00:00",
    )
    _install(eng, provider)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises(ExecutionTimeoutError) as excinfo:
        eng.step()
    assert str(excinfo.value).startswith(f"agent 'analyze_execute' {expected} and was killed.")
    (req,) = provider.calls
    assert (req.idle_timeout_seconds, req.max_runtime_seconds) == (120, ceiling)
    _, step = _step_dir(eng)
    execution = json.loads((step / "execution.json").read_text(encoding="utf-8"))
    assert execution["timeout_limit"] == "max_runtime" and execution["error"] == expected
    assert execution["max_runtime_seconds"] == ceiling


def test_a_text_mode_claude_profile_without_a_ceiling_is_never_launched(tmp_state_dir, fake_github):
    """#193: it would run under no limit at all but the one-week backstop."""
    eng = make_engine(tmp_state_dir, [], github=fake_github)
    eng.config.profile("analyze_execute").options["output_format"] = "text"
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises(ConfigurationError, match="max_runtime_seconds"):
        eng.step()
    assert eng.provider.calls == [] and eng.state.phase == Phase.ANALYZE_EXECUTE


def test_failed_exit_names_what_the_agent_left_behind(tmp_state_dir, fake_github):
    """PR #114 R1-F1: a non-zero exit carries the same leftover facts as a
    timeout. The immediate error, the journal and the run log all name the
    writer that still holds the pipes, so the operator does not read
    "exited 2" as "is gone" before resuming."""
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    provider = _LeftoverProvider("", exit_code=2, capture_abandoned=True)
    _install(eng, provider)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises(ExecutionError) as excinfo:
        eng.step()
    message = str(excinfo.value)
    assert message.startswith(
        "agent 'analyze_execute' exited 2 (its output pipes never reached EOF"
    )
    assert "may still be running). stderr tail: boom State unchanged" in message
    assert eng.state.phase == Phase.ANALYZE_EXECUTE
    run_dir = eng.paths.logs_dir / eng.state.run_id
    step = next(p for p in run_dir.iterdir() if p.is_dir())
    execution = json.loads((step / "execution.json").read_text(encoding="utf-8"))
    assert execution["exit_code"] == 2 and execution["capture_abandoned"] is True
    assert execution["error"] == (
        "exit 2 (its output pipes never reached EOF, so a process outside its group "
        "(one that called setsid) still holds them and may still be running)"
    )
    assert "never reached EOF" in (step / "error.txt").read_text(encoding="utf-8")
    event = json.loads((run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert event["capture_abandoned"] is True


def _step_dir(eng):
    run_dir = eng.paths.logs_dir / eng.state.run_id
    return run_dir, next(p for p in run_dir.iterdir() if p.is_dir())


def test_a_provider_failure_raises_before_the_parser_and_is_journaled(tmp_state_dir, fake_github):
    """#131: a protocol-level failure (here: Pi rejected the prompt) is its own
    error, even with a well-formed block on stdout and exit 0; the run log
    keeps the reason and the provider summary, and state does not move."""
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    reason = "pi: prompt rejected before acceptance: No API key found for openai."
    summary = {"resolved_model_id": "gpt-5.6-terra", "agent_ends": 0, "abort_sent": False}
    provider = _LeftoverProvider(implement, provider_failure=reason, provider_summary=summary)
    _install(eng, provider)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises(ExecutionError) as excinfo:
        eng.step()
    message = str(excinfo.value)
    assert message.startswith(f"agent 'analyze_execute' failed: {reason}. stderr tail: boom")
    assert message.endswith("then 'resume'.")
    assert eng.state.phase == Phase.ANALYZE_EXECUTE and len(provider.calls) == 1
    assert load_state(eng.paths.state_file).phase == Phase.ANALYZE_EXECUTE
    run_dir, step = _step_dir(eng)
    execution = json.loads((step / "execution.json").read_text(encoding="utf-8"))
    assert execution["error"] == reason and execution["exit_code"] == 0
    assert execution["provider_summary"] == summary
    assert reason in (step / "error.txt").read_text(encoding="utf-8")
    assert (step / "stderr.log").read_text(encoding="utf-8") == "boom"
    assert not (step / "control-result.json").exists()
    event = json.loads((run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert event["provider_summary"] == summary


def test_openai_websocket_disconnect_retries_and_reconciles_before_relaunch(
    tmp_state_dir, fake_github
):
    eng = _in_review(tmp_state_dir, fake_github, [])
    calls = _install_opencode_responses(
        eng,
        [
            (1, "complete but rejected", "WebSocket closed with code 1000"),
            (1, "complete but rejected", "WebSocket closed with code 1000"),
            (0, block(review_payload(1, SHA_A, [])), ""),
        ],
    )
    original_entry = eng._remote_entry
    reconciliations = []

    def counted_entry(previous, plan):
        reconciliations.append(previous)
        return original_entry(previous, plan)

    eng._remote_entry = counted_entry
    out = eng.step()

    assert out.next_phase == "READY_FOR_MERGE"
    assert len(calls) == 3
    assert reconciliations == [Phase.REVIEW, Phase.REVIEW, Phase.REVIEW]
    run_dir = eng.paths.logs_dir / eng.state.run_id
    steps = sorted(p for p in run_dir.iterdir() if p.is_dir())
    assert len(steps) == 3
    assert [
        json.loads((p / "execution.json").read_text(encoding="utf-8"))["exit_code"] for p in steps
    ] == [
        1,
        1,
        0,
    ]
    assert all((p / "error.txt").exists() for p in steps[:2])
    assert (steps[2] / "control-result.json").exists()


def test_openai_websocket_disconnect_stops_after_three_total_attempts(tmp_state_dir, fake_github):
    eng = _in_review(tmp_state_dir, fake_github, [])
    calls = _install_opencode_responses(
        eng,
        [(1, "complete but rejected", "WebSocket closed with code 1000")] * 3,
    )

    with pytest.raises(ExecutionError, match="after 3 consecutive retryable failures") as excinfo:
        eng.step()

    state = load_state(eng.paths.state_file)
    assert state.phase == Phase.REVIEW and state.review_round == 0
    assert state.current_pr_url == PR and state.attempt == 3 and state.step_count == 1
    assert len(calls) == 3
    assert "State unchanged" in str(excinfo.value) and "resume" in str(excinfo.value)
    run_dir = eng.paths.logs_dir / state.run_id
    steps = sorted(p for p in run_dir.iterdir() if p.is_dir())
    assert len(steps) == 3
    assert all("code 1000" in (p / "error.txt").read_text() for p in steps)
    assert all(not (p / "control-result.json").exists() for p in steps)


def test_openai_websocket_retry_does_not_consume_control_result_correction(
    tmp_state_dir, fake_github
):
    eng = _in_review(tmp_state_dir, fake_github, [])
    eng.config.execution.max_correction_attempts = 1
    calls = _install_opencode_responses(
        eng,
        [
            (1, "", "WebSocket closed with code 1000"),
            (0, "not a control result", ""),
            (0, block(review_payload(1, SHA_A, [])), ""),
        ],
    )

    assert eng.step().next_phase == "READY_FOR_MERGE"
    assert len(calls) == 3
    run_dir = eng.paths.logs_dir / eng.state.run_id
    steps = sorted(p for p in run_dir.iterdir() if p.is_dir())
    requests = [json.loads((p / "request.json").read_text()) for p in steps]
    assert [request["correction"] for request in requests] == [False, False, True]


def test_unrelated_opencode_failure_is_not_retried(tmp_state_dir, fake_github):
    eng = _in_review(tmp_state_dir, fake_github, [])
    calls = _install_opencode_responses(eng, [(1, "", "WebSocket closed with code 1006")])

    with pytest.raises(ExecutionError, match="exited 1"):
        eng.step()

    state = load_state(eng.paths.state_file)
    assert state.phase == Phase.REVIEW and state.attempt == 1
    assert len(calls) == 1


def test_a_timeout_wins_over_a_provider_failure(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    provider = _LeftoverProvider("", timed_out=True, provider_failure="pi: exited early")
    _install(eng, provider)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises(ExecutionTimeoutError):
        eng.step()
    assert eng.state.phase == Phase.ANALYZE_EXECUTE


def test_a_provider_failure_names_what_the_agent_left_behind(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    provider = _LeftoverProvider(
        "", provider_failure="pi: exited before agent_settled (exit 1)", capture_abandoned=True
    )
    _install(eng, provider)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises(ExecutionError, match="never reached EOF"):
        eng.step()


def test_a_one_shot_provider_writes_no_provider_summary(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    provider = _LeftoverProvider(implement)
    _install(eng, provider)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    assert eng.step().next_phase == "REVIEW"
    _, step = _step_dir(eng)
    execution = json.loads((step / "execution.json").read_text(encoding="utf-8"))
    assert "provider_summary" not in execution


def test_a_pi_phase_runs_end_to_end_over_rpc(tmp_state_dir, fake_github, tmp_path):
    """#131 end to end: the real PiProvider against a scripted fake `pi`. The
    final assistant text is stdout, the parser and GitHub verification
    accept it, and neither argv nor request.json carries the prompt."""
    from autoforge.pi_rpc import js_trim
    from autoforge.providers import PiProvider
    from tests.test_pi_rpc import MODEL, FakePi, _happy

    (tmp_path / "fake").mkdir()
    fake = FakePi(tmp_path / "fake", _happy())
    final: list[str] = []

    class _Pi(PiProvider):
        def execute(self, req):
            # The agent's work, one commit in its worktree, reported as Pi's
            # final text: the same fake `pi`, rescripted before it starts.
            final.append(js_trim(implement(req)))
            FakePi(tmp_path / "fake", _happy(final[-1]))
            return super().execute(req)

    cfg = default_config()
    cfg.profiles["analyze_execute"] = replace(
        cfg.profile("analyze_execute"),
        provider="pi",
        model=MODEL,
        effort="high",
        command=str(fake.command),
        extra_args=[],
        options={},
    )
    eng = make_engine(tmp_state_dir, [], github=fake_github, cfg=cfg, origin=True)
    eng.providers._overrides["pi"] = _Pi(round_trip_seconds=5, abort_seconds=1)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    assert eng.step().next_phase == "REVIEW"
    assert fake.commands() == [
        "get_state",
        "get_available_models",
        "prompt",
        "get_last_assistant_text",
    ]
    prompt = json.loads(fake.stdin()[2])["message"]
    assert "CONTROL_RESULT" in prompt
    _, step = _step_dir(eng)
    assert len(final) == 1
    assert (step / "stdout.log").read_text(encoding="utf-8") == final[0]
    execution = json.loads((step / "execution.json").read_text(encoding="utf-8"))
    assert execution["provider_summary"]["stop_reason"] == "stop"
    assert execution["provider_summary"]["failure"] == ""
    request = (step / "request.json").read_text(encoding="utf-8")
    assert prompt not in request and prompt not in " ".join(fake.argv())
    assert "--mode" in request


def test_truncated_stdout_never_accepts_a_block_from_the_head(tmp_state_dir, fake_github):
    """A block before the cut is stale (the agent wrote more after it) or
    spans the cut; either way it is not the agent's final result. The
    rejection names the truncation so the correction prompt carries it."""
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    eng.config.execution.max_correction_attempts = 0
    provider = _TruncatedOutputProvider(implement, "trailing logs only\n")
    _install(eng, provider)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises(ControlResultValidationError, match="capture bound") as exc:
        eng.step()
    assert "no CONTROL_RESULT block found" in str(exc.value)
    assert eng.state.phase == Phase.ANALYZE_EXECUTE and len(provider.calls) == 1
    run_dir = eng.paths.logs_dir / eng.state.run_id
    step = next(p for p in run_dir.iterdir() if p.is_dir())
    assert "capture bound" in (step / "error.txt").read_text(encoding="utf-8")


# =====================================================================================
# The shared durable-claim protocol (PR #89 re-review): entry reconciliation and
# post-agent read-back consume one validated identity model, for every marker kind.
# =====================================================================================
ISSUE4 = "https://github.com/owner/repo/issues/4"


def _impl_marker(payload: object) -> str:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return f"Closes {ISSUE}\n\n<!-- ai-implementation: {text} -->\n"


_UNREADABLE_IMPLEMENTATION_BODIES = [
    pytest.param(_impl_marker({"issue": 2}), id="issue-not-a-url"),
    pytest.param(_impl_marker({"issue": ISSUE, "pr": PR}), id="extra-key"),
    pytest.param(_impl_marker("{broken"), id="not-json"),
    pytest.param(_impl_marker([ISSUE]), id="not-an-object"),
    pytest.param(implementation_pr_body(ISSUE) + implementation_pr_body(ISSUE3), id="two-issues"),
    pytest.param(implementation_pr_body(ISSUE) * 2, id="same-issue-twice"),
    pytest.param(implementation_pr_body(ISSUE3) * 2, id="another-issue-twice"),
]


def test_analyze_entry_block_reason_never_carries_an_over_long_marker_url(
    tmp_state_dir, fake_github
):
    """A marker URL is untrusted text the entry persists a defect about: the
    reason names its length and the bound, and stays bounded itself, so a
    hostile PR body cannot write itself into the state file."""
    hostile = "https://github.com/owner/repo/issues/" + "9" * 10_000
    fake_github.add_pr(
        url=PR41, head_sha=SHA_B, branch="feature/lost", body=_impl_marker({"issue": hostile})
    )
    eng = make_engine(tmp_state_dir, ["never"], github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    out = eng.step()
    assert out.next_phase == "BLOCKED" and eng.provider.calls == []
    reason = load_state(eng.paths.state_file).block_reason
    assert "9" * 64 not in reason and len(reason) < 2_000
    assert f"{len(hostile)} characters" in reason and f"at most {MAX_URL_CHARS}" in reason
    assert f"open PR {PR41}" in reason and "repair the unreadable marker" in reason


@pytest.mark.parametrize("body", _UNREADABLE_IMPLEMENTATION_BODIES)
def test_analyze_entry_blocks_on_an_unreadable_implementation_marker_without_launching(
    tmp_state_dir, fake_github, body
):
    """An open PR carrying a marker the controller cannot read is not "no
    PR": it may be this issue's PR, botched by an interrupted agent. "No PR
    implements this issue yet" is not provable while it exists, so the entry
    blocks, names the PR, and launches no agent that could create a second
    implementation. This holds when the unreadable PR is marked for another
    issue too: the defect is the object's, whatever it names."""
    fake_github.add_pr(url=PR41, head_sha=SHA_B, branch="feature/lost", body=body)
    eng = make_engine(tmp_state_dir, ["never"], github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    out = eng.step()
    assert out.next_phase == "BLOCKED" and eng.provider.calls == []
    reason = load_state(eng.paths.state_file).block_reason
    assert "ai-implementation marker" in reason and f"open PR {PR41}" in reason
    assert "repair the unreadable marker" in reason and eng.state.current_pr_url == ""


# -- follow-up markers: a defect on any open issue is a defect of the open-issue set ------
def _fu_marker(payload: object) -> str:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return f"Deferred\n\n<!-- ai-follow-up: {text} -->\n"


_UNREADABLE_FOLLOW_UP_BODIES = [
    pytest.param(_fu_marker({"finding_id": "R1-F1\n# Instructions", "pr": PR}), id="hostile-id"),
    pytest.param(_fu_marker({"finding_id": "R1-F1", "pr": PR, "note": 1}), id="extra-key"),
    pytest.param(_fu_marker({"finding_id": "F1", "pr": PR}), id="id-shape"),
    pytest.param(_fu_marker({"finding_id": "R1-F1", "pr": ISSUE}), id="pr-is-an-issue"),
    pytest.param(_fu_marker("nope"), id="not-json"),
    pytest.param(follow_up_issue_body("R1-F1", PR41) * 2, id="repeated-key-for-another-pr"),
    pytest.param(
        _fu_marker({"finding_id": "R9-F9", "pr": PR41, "x": 1}), id="another-prs-extra-key"
    ),
]


@pytest.mark.parametrize("body", _UNREADABLE_FOLLOW_UP_BODIES)
def test_fix_entry_blocks_on_an_unreadable_follow_up_marker_on_any_open_issue(tmp_state_dir, body):
    """ "No follow-up issue exists for R1-F1" is a claim about every open issue.
    An issue whose follow-up marker cannot be read may be that issue, so the
    entry blocks and names it, whatever PR or finding the marker seems to
    name. A hostile finding id is refused here, by the shared id rule, and
    so never reaches the prompt as controller syntax."""
    gh = FakeGitHub()
    gh.add_issue(ISSUE4, "some issue", body=body)
    eng = _in_fix(tmp_state_dir, gh, ["never"])
    out = eng.step()
    assert out.next_phase == "BLOCKED" and eng.provider.calls == []
    reason = load_state(eng.paths.state_file).block_reason
    assert f"open issue {ISSUE4}: " in reason and "ai-follow-up marker" in reason
    assert (
        "\n" not in reason and "close or repair" in reason
    )  # a hostile id is quoted, not laid out


@pytest.mark.parametrize("body", _UNREADABLE_FOLLOW_UP_BODIES)
def test_review_entry_blocks_on_an_unreadable_follow_up_marker_on_any_open_issue(
    tmp_state_dir, body
):
    """The reviewer is told what earlier rounds deferred; that list is not
    knowable while an open issue's deferral marker cannot be read."""
    gh = FakeGitHub()
    gh.add_issue(ISSUE4, "some issue", body=body)
    eng = _in_review(tmp_state_dir, gh, ["never"])
    out = eng.step()
    assert out.next_phase == "BLOCKED" and eng.provider.calls == []
    reason = load_state(eng.paths.state_file).block_reason
    assert "cannot establish which follow-up issues already exist for PR" in reason
    assert f"open issue {ISSUE4}: " in reason and "\n" not in reason


def test_fix_plan_blocks_when_any_open_issue_carries_an_unreadable_follow_up_marker(
    tmp_state_dir,
):
    """A follow-up marker was botched on another open issue while the fixer
    ran (an edit by hand, or a fixer ignoring its contract): the open-issue
    set the plan is read from is inconclusive, so nothing is planned,
    created or pushed and no resolution is recorded."""
    gh = FakeGitHub()
    fixes = fixer(fixed("R1-F1"))

    def botched_meanwhile(req):
        gh.add_issue(ISSUE4, "botched", body=_fu_marker({"finding_id": "R1-F1", "pr": PR, "z": 0}))
        return fixes(req)

    eng = _in_fix(tmp_state_dir, gh, botched_meanwhile, origin=True)
    reviewed = eng.state.reviewed_head_sha
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    s = load_state(eng.paths.state_file)
    assert f"open issue {ISSUE4}: " in s.block_reason
    assert "nothing was created, appended or pushed" in s.block_reason
    assert s.last_fix_resolutions == [] and s.effect_records == []
    assert gh.effect_writes == [] and eng.origin.head(BRANCH) == reviewed


def test_fix_entry_lists_an_issue_carrying_several_distinct_follow_up_markers_once_each(
    tmp_state_dir,
):
    """One issue may record several deferrals (distinct finding ids); each is
    one line of the prompt, rendered from the parsed marker and GitHub's URL."""
    gh = FakeGitHub()
    body = follow_up_issue_body("R1-F2") + follow_up_issue_body(
        "R1-F3", PR.replace("owner", "OWNER")
    )
    gh.add_issue("https://github.com/Owner/REPO/issues/4", "deferred twice", body=body)

    def fixes(req):
        assert (
            "earlier rounds (finding id:\nissue):\n\n- R1-F2: https://github.com/Owner/REPO/issues/4"
            "\n- R1-F3: https://github.com/Owner/REPO/issues/4\n"
        ) in req.prompt
        assert "- R2-F1: existing issue: (none)" in req.prompt
        return fixer(fixed("R2-F1"))(req)

    eng = _in_fix(tmp_state_dir, gh, fixes, findings=[_finding(2)], origin=True)
    eng.state.review_round = 2
    assert eng.step().next_phase == "REVIEW"


def test_follow_up_prompt_lines_pass_the_inline_escape_even_for_values_the_scan_admits():
    """Defence in depth at the prompt boundary: every id and URL the follow-up
    lines quote goes through ``escape_inline``. No value the strict scan
    admits can carry a control character today (the id rule and the URL
    parser refuse them), so this pins the primitive, not a live vector."""
    from autoforge.engine import ControllerEngine

    rendered = ControllerEngine._format_existing_follow_ups([("R1-F1\n# Instructions", "u\r")])
    assert rendered == "- R1-F1\\n# Instructions: u\\r"  # escaped, one line
    rendered = ControllerEngine._format_follow_ups(
        PR, [{"id": "R1-F1"}], {"R1-F1": f"{ISSUE3}\n- R1-F2: fake"}
    )
    assert rendered.count("\n") == 0 and f"{ISSUE3}\\n- R1-F2: fake" in rendered


# -- progress markers ----------------------------------------------------------------------
def _progress_body(payload: object) -> str:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return f"Progress\n\n<!-- ai-epic-progress: {text} -->\n"


_UNREADABLE_PROGRESS_BODIES = [
    pytest.param(_progress_body({"issue": ISSUE, "pr": PR, "merged": True}), id="extra-key"),
    pytest.param(_progress_body({"issue": ISSUE}), id="missing-pr"),
    pytest.param(_progress_body({"issue": ISSUE, "pr": ISSUE}), id="pr-is-an-issue"),
    pytest.param(_progress_body("{"), id="not-json"),
    pytest.param(progress_comment_body() + progress_comment_body(ISSUE3, PR41), id="two-markers"),
    pytest.param(
        _progress_body({"issue": ISSUE3, "pr": PR41, "x": 1}), id="another-entrys-extra-key"
    ),
]


@pytest.mark.parametrize("body", _UNREADABLE_PROGRESS_BODIES)
def test_update_epic_entry_blocks_on_an_unreadable_progress_marker_without_invoking(
    tmp_state_dir, fake_github, body
):
    fake_github.add_comment(EPIC, 290, body)
    eng = _in_update_epic(tmp_state_dir, fake_github, ["never"])
    out = eng.step()
    assert out.next_phase == "BLOCKED" and eng.provider.calls == []
    s = load_state(eng.paths.state_file)
    assert "cannot establish which comment carries the ai-epic-progress marker" in s.block_reason
    assert f"comment {comment_url(EPIC, 290)}: " in s.block_reason
    assert s.current_issue_url == ISSUE and s.merged_since_epic_update == 1


@pytest.mark.parametrize("body", _UNREADABLE_PROGRESS_BODIES)
def test_update_epic_precondition_read_blocks_on_an_unreadable_progress_marker(
    tmp_state_dir, fake_github, body
):
    """An agent that wrote a malformed marker while it ran: the precondition read
    before the controller's post cannot be read unambiguously, so nothing is posted."""

    def agent(req):
        fake_github.add_comment(EPIC, 290, body)
        return _epic_result(None)

    eng = _in_update_epic(tmp_state_dir, fake_github, agent)
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    s = load_state(eng.paths.state_file)
    assert "cannot establish which comment carries" in s.block_reason
    assert "Nothing was posted" in s.block_reason
    assert s.current_issue_url == ISSUE and _progress_posts(fake_github) == []


# -- the correction relaunch is an entry: a botched write blocks it, it does not relaunch --
def test_correction_after_a_botched_review_comment_blocks_instead_of_relaunching(tmp_state_dir):
    """The reviewer posted a comment whose marker cannot be read, then
    returned junk. The correction relaunch is preceded by the REVIEW entry,
    which meets the defect and blocks: a second reviewer would post a
    second comment beside the unreadable one."""
    gh = FakeGitHub()

    def reviews(req):
        assert not req.correction, "a correction must not be launched"
        gh.add_comment(PR, 100, _review_body_with_marker(json.dumps({"round": 1})))
        return "junk\n"

    eng = _in_review(tmp_state_dir, gh, reviews)
    out = eng.step()
    assert out.next_phase == "BLOCKED" and len(eng.provider.calls) == 1
    reason = load_state(eng.paths.state_file).block_reason
    assert "cannot establish which comment carries the ai-review-result marker" in reason
    assert comment_url(PR, 100) in reason and len(gh.comments[PR]) == 1


def test_correction_after_a_botched_progress_comment_blocks_instead_of_relaunching(
    tmp_state_dir, fake_github
):
    def agent(req):
        assert not req.correction, "a correction must not be launched"
        fake_github.add_comment(EPIC, 300, _progress_body({"issue": ISSUE, "pr": PR, "k": 1}))
        return "junk\n"

    eng = _in_update_epic(tmp_state_dir, fake_github, agent)
    out = eng.step()
    assert out.next_phase == "BLOCKED" and len(eng.provider.calls) == 1
    reason = load_state(eng.paths.state_file).block_reason
    assert "cannot establish which comment carries the ai-epic-progress marker" in reason
    assert len(fake_github.comments[EPIC]) == 1


def test_correction_after_a_botched_implementation_pr_blocks_instead_of_relaunching(
    tmp_state_dir, fake_github
):
    def agent(req):
        assert not req.correction, "a correction must not be launched"
        fake_github.add_pr(
            head_sha=SHA_A, branch=BRANCH, body=_impl_marker({"issue": ISSUE, "n": 1})
        )
        return "junk\n"

    eng = make_engine(tmp_state_dir, agent, github=fake_github, origin=True)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    out = eng.step()
    assert out.next_phase == "BLOCKED" and len(eng.provider.calls) == 1
    reason = load_state(eng.paths.state_file).block_reason
    assert f"open PR {PR}: ai-implementation marker payload is invalid" in reason
    assert len(fake_github.prs) == 1


# -- a comment listing the client refuses is inconclusive, like any other strict read --
# One rule for every durable-claim read: GitHubUnavailableError is transient and
# propagates; a conclusive GitHubError (a row `gh` returned that is not a comment)
# blocks the entry without launching and rejects the read-back. A malformed row
# must never read as "no comment carries the marker".
_MALFORMED_COMMENT_ROW = GitHubError(
    "comment on https://github.com/owner/repo/pull/42: url None is not a GitHub comment URL"
)


def test_review_entry_blocks_when_the_comment_listing_cannot_be_decoded(tmp_state_dir):
    gh = FakeGitHub()
    gh.comments_error = _MALFORMED_COMMENT_ROW
    eng = _in_review(tmp_state_dir, gh, ["never"])
    out = eng.step()
    assert out.next_phase == "BLOCKED" and eng.provider.calls == []
    reason = load_state(eng.paths.state_file).block_reason
    assert "cannot establish which comment carries the review for round 1" in reason
    assert "not a GitHub comment URL" in reason and eng.state.review_round == 0


def test_review_entry_lets_an_unavailable_comment_listing_through_as_transient(tmp_state_dir):
    gh = FakeGitHub()
    gh.comments_error = GitHubUnavailableError("`gh pr view` failed (exit 1): HTTP 502")
    eng = _in_review(tmp_state_dir, gh, ["never"])
    with pytest.raises(GitHubUnavailableError):
        eng.step()
    assert eng.provider.calls == [] and load_state(eng.paths.state_file).phase == Phase.REVIEW


def test_review_precondition_read_blocks_when_the_comment_listing_cannot_be_decoded(
    tmp_state_dir,
):
    """The controller reads the PR's comments before it posts the round's; a
    listing it cannot decode blocks the round with nothing posted."""
    gh = FakeGitHub()

    def reviews(req):
        gh.comments_error = _MALFORMED_COMMENT_ROW
        return block(review_payload(1, SHA_A, []))

    eng = _in_review(tmp_state_dir, gh, reviews)
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.BLOCKED and s.review_round == 0 and s.review_history == []
    assert "could not be read before the comment of review round 1 was posted" in s.block_reason
    assert "not a GitHub comment URL" in s.block_reason
    assert "nothing was posted" in s.block_reason
    assert gh.effect_writes == [] and s.effect_records == []


def test_review_precondition_read_lets_an_unavailable_comment_listing_through_as_transient(
    tmp_state_dir,
):
    gh = FakeGitHub()

    def reviews(req):
        gh.comments_error = GitHubUnavailableError("`gh pr view` failed (exit 1): HTTP 502")
        return block(review_payload(1, SHA_A, []))

    eng = _in_review(tmp_state_dir, gh, reviews)
    with pytest.raises(GitHubUnavailableError):
        eng.step()
    assert load_state(eng.paths.state_file).review_round == 0
    assert gh.effect_writes == []


def test_update_epic_entry_blocks_when_the_comment_listing_cannot_be_decoded(
    tmp_state_dir, fake_github
):
    fake_github.comments_error = _MALFORMED_COMMENT_ROW
    eng = _in_update_epic(tmp_state_dir, fake_github, ["never"])
    out = eng.step()
    assert out.next_phase == "BLOCKED" and eng.provider.calls == []
    s = load_state(eng.paths.state_file)
    assert "is the progress comment for issue" in s.block_reason
    assert "not a GitHub comment URL" in s.block_reason
    assert s.current_issue_url == ISSUE and s.merged_since_epic_update == 1


def test_update_epic_entry_lets_an_unavailable_comment_listing_through_as_transient(
    tmp_state_dir, fake_github
):
    fake_github.comments_error = GitHubUnavailableError("`gh issue view` failed: HTTP 502")
    eng = _in_update_epic(tmp_state_dir, fake_github, ["never"])
    with pytest.raises(GitHubUnavailableError):
        eng.step()
    assert eng.provider.calls == []
    assert load_state(eng.paths.state_file).phase == Phase.UPDATE_EPIC


def test_update_epic_precondition_read_blocks_when_the_comment_listing_cannot_be_decoded(
    tmp_state_dir, fake_github
):
    def agent(req):
        fake_github.comments_error = _MALFORMED_COMMENT_ROW
        return _epic_result(None)

    eng = _in_update_epic(tmp_state_dir, fake_github, agent)
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    s = load_state(eng.paths.state_file)
    assert "not a GitHub comment URL" in s.block_reason
    assert "Nothing was posted" in s.block_reason
    assert s.current_issue_url == ISSUE and _progress_posts(fake_github) == []


# -- READY_FOR_MERGE / MERGE: the clean review is bound to the PR and base (issue #68) ---------
PR_B = "https://github.com/owner/repo/pull/43"  # same branch, same HEAD, another base


def _add_same_head_pr_against_another_base(gh: FakeGitHub, head: str = SHA_A) -> None:
    gh.add_pr(PR_B, head_sha=head, branch=BRANCH, base_ref="release/1.x")


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
def test_merge_refuses_a_current_pr_that_is_not_the_reviewed_pr(tmp_state_dir, fake_github, phase):
    """A `current_pr_url` swapped to another PR at the reviewed HEAD (the same
    branch proposed against another base) passes every HEAD-only check; the
    review is bound to the PR it was posted on and is never re-bound: BLOCKED
    before GitHub is asked anything, no merge, nothing counted."""
    fake_github.add_pr(head_sha=SHA_A)
    _add_same_head_pr_against_another_base(fake_github)
    eng = _in_merge(tmp_state_dir, fake_github, phase=phase)
    eng.state.current_pr_url = PR_B
    eng._save()
    before = list(fake_github.calls)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED"
    reason = eng.state.block_reason
    assert "pull/43" in reason and "not the PR the clean review was posted on" in reason
    assert "pull/42" in reason and "Nothing was merged or counted" in reason
    assert fake_github.calls == before  # refused from state alone
    assert fake_github.merges == [] and eng.state.counted_merged_prs == []
    assert fake_github.prs[PR].state == "OPEN" and fake_github.prs[PR_B].state == "OPEN"
    # The binding was not moved to the current URL.
    persisted = load_state(eng.paths.state_file)
    assert persisted.phase == Phase.BLOCKED and persisted.reviewed_pr_url == PR
    assert persisted.current_pr_url == PR_B and persisted.last_review_result == "clean"
    # resume replays the refusal: BLOCKED is terminal, no later step reaches a merge.
    eng.load()
    assert eng.state.phase == Phase.BLOCKED
    with pytest.raises(StateTransitionError):
        eng.step(allow_merge=True)
    assert eng.run(max_steps=5, allow_merge=True) == []
    assert fake_github.merges == []


def test_merge_pr_identity_is_compared_by_target_not_string(tmp_state_dir, fake_github):
    """An equivalent spelling of the reviewed PR's URL is the same PR."""
    fake_github.add_pr(head_sha=SHA_A)
    eng = _in_merge(tmp_state_dir, fake_github)
    eng.state.current_pr_url = "https://github.com/Owner/Repo/pull/42/"
    eng._save()
    out = eng.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC" and eng.state.counted_merged_prs == [PR]


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
def test_merge_refuses_when_github_answers_with_another_pr(tmp_state_dir, fake_github, phase):
    """GitHub is the source of truth for which PR a URL names: a read that
    comes back as a different PR (a redirect, a wrong `gh` answer) blocks."""
    fake_github.add_pr(head_sha=SHA_A)
    _add_same_head_pr_against_another_base(fake_github)
    other = fake_github.prs[PR_B]
    orig_get_pr = fake_github.get_pr
    fake_github.get_pr = lambda url: replace(other) if url == PR else orig_get_pr(url)
    eng = _in_merge(tmp_state_dir, fake_github, phase=phase)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED"
    assert "GitHub answered" in eng.state.block_reason and "pull/43" in eng.state.block_reason
    assert fake_github.merges == [] and eng.state.counted_merged_prs == []
    assert load_state(eng.paths.state_file).reviewed_pr_url == PR


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reviewed_pr_url", ""),
        ("reviewed_base_ref", ""),
        ("reviewed_head_sha", ""),
        ("reviewed_merge_base_sha", ""),
    ],
)
@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
def test_merge_requires_the_whole_review_binding(tmp_state_dir, fake_github, phase, field, value):
    fake_github.add_pr(head_sha=SHA_A)
    eng = _in_merge(tmp_state_dir, fake_github, phase=phase)
    setattr(eng.state, field, value)
    before = list(fake_github.calls)
    with pytest.raises(
        VerificationError,
        match="clean review bound to a PR, a HEAD, a base branch and a merge base",
    ):
        eng.step(allow_merge=True)
    assert fake_github.calls == before and fake_github.merges == []
    assert eng.state.phase == phase


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
def test_merge_base_retargeted_after_clean_review_goes_back_to_review(
    tmp_state_dir, fake_github, phase
):
    """The same commits against another base are a different change: the
    review is stale (same rule as a HEAD move), never merged as reviewed."""
    fake_github.add_pr(head_sha=SHA_A, base_ref="release/1.x")
    eng = _in_merge(tmp_state_dir, fake_github, phase=phase)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "REVIEW" and "not merged" in out.message
    assert "base changed to 'release/1.x'" in out.message
    assert fake_github.merges == [] and eng.state.counted_merged_prs == []
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.REVIEW and s.last_review_result == "stale"
    assert s.current_base_ref == "release/1.x" and s.reviewed_base_ref == "main"
    assert s.current_head_sha == SHA_A and s.open_findings == []


def test_review_after_a_retarget_rebinds_the_base_and_then_merges(tmp_state_dir, fake_github):
    """The re-review binds the new base; a clean round on it is mergeable."""
    fake_github.add_pr(head_sha=SHA_A, base_ref="release/1.x")
    # The new base exists and has its own green push run for the definition gate.
    fake_github.branch_heads["release/1.x"] = MAIN_SHA
    fake_github.workflow_runs[BASE_RUN_ID] = replace(
        fake_github.workflow_runs[BASE_RUN_ID], head_branch="release/1.x"
    )
    eng = _in_merge(tmp_state_dir, fake_github, [block(review_payload(3, SHA_A, []))])
    assert eng.step(allow_merge=True).next_phase == "REVIEW"
    assert eng.step(allow_merge=True).next_phase == "READY_FOR_MERGE"
    s = load_state(eng.paths.state_file)
    assert (s.reviewed_pr_url, s.reviewed_head_sha, s.reviewed_base_ref) == (
        PR,
        SHA_A,
        "release/1.x",
    )
    assert eng.step(allow_merge=True).next_phase == "MERGE"
    out = eng.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC" and eng.state.counted_merged_prs == [PR]


def test_merge_base_retargeted_between_verification_and_write_goes_back_to_review(
    tmp_state_dir, fake_github
):
    """Nothing guards the base on the write; when the merge did not happen and
    the PR is OPEN against another base, the revision-drift rule applies."""
    fake_github.add_pr(head_sha=SHA_A)
    fake_github.merge_error = "`gh pr merge` failed (exit 1): base branch was modified"
    eng = _in_merge(tmp_state_dir, fake_github)
    orig_merge = fake_github.merge_pr

    def retarget_then_merge(*a, **kw):
        fake_github.prs[PR].base_ref = "release/1.x"
        orig_merge(*a, **kw)

    fake_github.merge_pr = retarget_then_merge
    out = eng.step(allow_merge=True)
    assert out.next_phase == "REVIEW" and "not merged" in out.message
    assert "base changed to 'release/1.x'" in out.message
    assert len(fake_github.merges) == 1 and fake_github.prs[PR].state == "OPEN"
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.REVIEW and s.last_review_result == "stale"
    assert s.current_base_ref == "release/1.x" and s.counted_merged_prs == []


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
def test_merge_base_branch_rewritten_after_clean_review_goes_back_to_review(
    tmp_state_dir, fake_github, phase
):
    """#96: same HEAD, same base *name*, but the base was rewritten under
    that name (force-push, reset) so the merge base moved and with it the
    diff the review decided on. The review is stale, exactly as for a HEAD
    move or a retarget; nothing is merged or counted, and the merge base
    the PR now shows is bound for the re-review."""
    fake_github.add_pr(head_sha=SHA_A)
    fake_github.merge_base = MERGE_BASE_B
    eng = _in_merge(tmp_state_dir, fake_github, phase=phase)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "REVIEW" and "not merged" in out.message
    assert f"PR merge base moved to {MERGE_BASE_B[:12]} from the reviewed {MERGE_BASE[:12]}" in (
        out.message
    )
    assert "base 'main' was rewritten under its name" in out.message
    assert fake_github.merges == [] and eng.state.counted_merged_prs == []
    assert ("get_merge_base_sha", "owner/repo", "main", SHA_A) in fake_github.calls
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.REVIEW and s.last_review_result == "stale"
    assert s.current_merge_base_sha == MERGE_BASE_B and s.reviewed_merge_base_sha == MERGE_BASE
    assert s.current_head_sha == SHA_A and s.current_base_ref == "main"
    assert s.reviewed_head_sha == SHA_A and s.reviewed_base_ref == "main"
    assert s.open_findings == []


def test_merge_proceeds_when_the_base_tip_moved_but_the_merge_base_did_not(
    tmp_state_dir, fake_github
):
    """#96 binds the merge base, not the base branch's tip: ordinary commits
    landing on the base leave the reviewed diff intact, so a clean review
    still merges (a PR that fell BEHIND is the readiness check's business,
    unchanged by this)."""
    fake_github.add_pr(head_sha=SHA_A)
    # main advanced since the review, with its own green push run for the
    # definition gate; the merge base of main and the PR HEAD is unchanged.
    fake_github.branch_heads["main"] = SHA_C
    fake_github.workflow_runs[BASE_RUN_ID] = replace(
        fake_github.workflow_runs[BASE_RUN_ID], head_sha=SHA_C
    )
    eng = _in_merge(tmp_state_dir, fake_github)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC" and eng.state.counted_merged_prs == [PR]
    assert ("get_merge_base_sha", "owner/repo", "main", SHA_A) in fake_github.calls
    assert load_state(eng.paths.state_file).reviewed_merge_base_sha == MERGE_BASE


def test_merge_with_an_unreadable_merge_base_blocks_without_merging(tmp_state_dir, fake_github):
    """A merge base GitHub does not answer for is not a moved merge base; the
    gate cannot decide, so it refuses conclusively rather than guessing."""
    fake_github.add_pr(head_sha=SHA_A)
    fake_github.merge_base_error = GitHubError("HTTP 404: Not Found")
    eng = _in_merge(tmp_state_dir, fake_github)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED" and fake_github.merges == []
    assert "pre-merge merge-base read" in eng.state.block_reason
    assert "merge base of 'main' and HEAD" in eng.state.block_reason
    assert "HTTP 404" in eng.state.block_reason
    assert load_state(eng.paths.state_file).reviewed_merge_base_sha == MERGE_BASE


def test_merge_with_a_transiently_unreadable_merge_base_rechecks(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_A)
    fake_github.merge_base_error = GitHubUnavailableError("HTTP 502")
    eng = _in_merge(tmp_state_dir, fake_github)
    with pytest.raises(VerificationError, match="merge-base read .* failed .* HTTP 502"):
        eng.step(allow_merge=True)
    assert fake_github.merges == [] and eng.state.attempt == 1
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.MERGE and s.reviewed_merge_base_sha == MERGE_BASE


def test_merge_base_branch_rewritten_between_verification_and_write_goes_back_to_review(
    tmp_state_dir, fake_github
):
    """#96 on the post-write read: the merge did not happen and the PR is
    OPEN at the reviewed HEAD on the reviewed base name, but the base was
    rewritten in the window. Same rule as a retarget in that window."""
    fake_github.add_pr(head_sha=SHA_A)
    fake_github.merge_error = "`gh pr merge` failed (exit 1): base branch was modified"
    eng = _in_merge(tmp_state_dir, fake_github)
    orig_merge = fake_github.merge_pr

    def rewrite_base_then_merge(*a, **kw):
        fake_github.merge_base = MERGE_BASE_B
        orig_merge(*a, **kw)

    fake_github.merge_pr = rewrite_base_then_merge
    out = eng.step(allow_merge=True)
    assert out.next_phase == "REVIEW" and "not merged" in out.message
    assert f"PR merge base moved to {MERGE_BASE_B[:12]} from the reviewed {MERGE_BASE[:12]}" in (
        out.message
    )
    assert f"from merge base {MERGE_BASE_B[:12]} != reviewed {SHA_A[:12]}" in out.message
    assert f"on 'main' from {MERGE_BASE[:12]}" in out.message
    assert len(fake_github.merges) == 1 and fake_github.prs[PR].state == "OPEN"
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.REVIEW and s.last_review_result == "stale"
    assert s.current_merge_base_sha == MERGE_BASE_B and s.counted_merged_prs == []


def test_merge_base_unreadable_after_a_failed_write_blocks(tmp_state_dir, fake_github):
    """The post-write read cannot tell whether the base was rewritten: the
    failed merge is reported as before, with the read failure noted, and
    the run is BLOCKED rather than sent to REVIEW on a guess."""
    fake_github.add_pr(head_sha=SHA_A)
    fake_github.merge_error = "`gh pr merge` failed (exit 1): base branch was modified"
    eng = _in_merge(tmp_state_dir, fake_github)
    orig_merge = fake_github.merge_pr

    def break_read_then_merge(*a, **kw):
        fake_github.merge_base_error = GitHubError("HTTP 404: Not Found")
        orig_merge(*a, **kw)

    fake_github.merge_pr = break_read_then_merge
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED" and eng.state.counted_merged_prs == []
    assert "base branch was modified" in eng.state.block_reason
    assert "merge base of 'main' and HEAD" in eng.state.block_reason
    assert load_state(eng.paths.state_file).phase == Phase.BLOCKED


def test_merge_base_retargeted_after_write_with_pending_async_merge_blocks(
    tmp_state_dir, fake_github
):
    fake_github.add_pr(head_sha=SHA_A)
    fake_github.merge_leaves_open = True
    fake_github.merge_arms_auto = True
    fake_github.disable_auto_error = "`gh pr merge --disable-auto` failed"
    eng = _in_merge(tmp_state_dir, fake_github)
    orig_merge = fake_github.merge_pr

    def retarget_then_merge(*a, **kw):
        fake_github.prs[PR].base_ref = "release/1.x"
        orig_merge(*a, **kw)

    fake_github.merge_pr = retarget_then_merge
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED" and eng.state.counted_merged_prs == []


@pytest.mark.parametrize("recovered", [True, False])
def test_merge_into_another_base_is_never_counted(tmp_state_dir, fake_github, recovered):
    """A PR MERGED at the reviewed HEAD but into another base is a change no
    review decided on: BLOCKED, not counted -- on crash recovery and on the
    post-write read alike."""
    if recovered:
        fake_github.add_pr(head_sha=SHA_A, state="MERGED", base_ref="release/1.x")
    else:
        fake_github.add_pr(head_sha=SHA_A)
        orig_merge = fake_github.merge_pr

        def retarget_then_merge(*a, **kw):
            fake_github.prs[PR].base_ref = "release/1.x"
            orig_merge(*a, **kw)

        fake_github.merge_pr = retarget_then_merge
    eng = _in_merge(tmp_state_dir, fake_github)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED"
    assert "into 'release/1.x'" in eng.state.block_reason
    assert "against 'main'" in eng.state.block_reason
    assert eng.state.counted_merged_prs == [] and eng.state.merged_since_epic_update == 0
    assert load_state(eng.paths.state_file).phase == Phase.BLOCKED


def test_merge_pr_without_a_readable_base_blocks(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_A, base_ref="")
    eng = _in_merge(tmp_state_dir, fake_github)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED" and "reports no base branch" in eng.state.block_reason
    assert fake_github.merges == []


def test_review_records_the_pr_head_and_base_it_decided_on(tmp_state_dir):
    gh = FakeGitHub()
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(1, SHA_A, []))])
    eng.state.current_base_ref = ""  # bound by the controller right before the review
    assert eng.step().next_phase == "READY_FOR_MERGE"
    s = load_state(eng.paths.state_file)
    assert (s.reviewed_pr_url, s.reviewed_head_sha, s.reviewed_base_ref) == (PR, SHA_A, "main")
    assert s.current_base_ref == "main"


def test_review_entry_binds_the_base_before_launching(tmp_state_dir):
    gh = FakeGitHub()
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(1, SHA_A, []))])
    eng._save()
    gh.prs[PR].base_ref = ""
    with pytest.raises(VerificationError, match="no readable base branch"):
        eng.step()
    assert eng.provider.calls == []
    s = load_state(eng.paths.state_file)
    assert s.review_round == 0 and s.phase == Phase.REVIEW


def test_review_base_changed_during_the_review_is_stale(tmp_state_dir):
    gh = FakeGitHub()

    def on_call(req):
        gh.prs[PR].base_ref = "release/1.x"  # retargeted while the reviewer worked
        return block(review_payload(1, SHA_A, []))

    eng = _in_review(tmp_state_dir, gh, on_call)
    out = eng.step()
    assert out.next_phase == "REVIEW" and "base changed from 'main' to 'release/1.x'" in out.message
    s = load_state(eng.paths.state_file)
    assert s.last_review_result == "stale" and s.review_round == 1
    assert s.reviewed_base_ref == "main" and s.current_base_ref == "release/1.x"
    assert s.reviewed_pr_url == PR and s.open_findings == []
    assert [r["result"] for r in s.review_history] == ["stale"]


def test_review_reentry_after_a_retarget_does_not_adopt_the_old_base_comment(tmp_state_dir):
    """PR #93 review (High): round 1 was posted while the PR targeted main
    and the result was lost; the PR was then retargeted at the same HEAD.
    The re-entry binds the new base and must not take the old comment as
    this round's (neither adopt it nor block on it): it reviewed the diff
    against main, and adopting it would record release/1.x as reviewed by a
    review it never had. The round is reviewed and posted afresh."""
    gh = FakeGitHub()
    gh.add_comment(PR, 100, review_comment_body(1, SHA_A, False, base_ref="main"))
    prompts = []

    def reviews(req):
        prompts.append(req.prompt)
        return block(review_payload(1, SHA_A, []))

    eng = _in_review(tmp_state_dir, gh, reviews)
    gh.prs[PR].base_ref = "release/1.x"
    assert eng.step().next_phase == "READY_FOR_MERGE"
    assert comment_url(PR, 100) not in prompts[0]
    assert "Reviewed base branch (bound by the controller): `release/1.x`" in prompts[0]
    s = load_state(eng.paths.state_file)
    assert (s.reviewed_pr_url, s.reviewed_head_sha, s.reviewed_base_ref) == (
        PR,
        SHA_A,
        "release/1.x",
    )
    assert len(gh.effect_writes) == 1
    assert s.last_review_comment_url == comment_url(PR, 950_001)  # the controller's own


@pytest.mark.parametrize("base", ["x-->y", "<!--x", "a--!>b"])
def test_a_review_against_a_base_named_like_a_comment_delimiter_completes(tmp_state_dir, base):
    """PR #93 review (Medium): ``x-->y`` is a valid refname. The controller
    renders the base into the marker as delimiter-free JSON, so the comment
    it posts scans as one marker; the round binds and verifies like any
    other."""
    gh = FakeGitHub()
    prompts = []

    def reviews(req):
        prompts.append(req.prompt)
        return block(review_payload(1, SHA_A, []))

    eng = _in_review(tmp_state_dir, gh, reviews)
    gh.prs[PR].base_ref = base
    assert eng.step().next_phase == "READY_FOR_MERGE"
    assert f"Reviewed base branch (bound by the controller): `{base}`" in prompts[0]
    s = load_state(eng.paths.state_file)
    assert (s.reviewed_pr_url, s.reviewed_head_sha, s.reviewed_base_ref) == (PR, SHA_A, base)
    posted = controller_review_comment(eng, 1)
    assert s.last_review_comment_url == posted.url
    assert "-->" not in posted.body.split("<!-- ai-review-result:", 1)[1].rsplit("-->", 1)[0]


def test_review_entry_ignores_a_pre_base_comment_for_the_round(tmp_state_dir):
    """A marker written before ``reviewed_base_ref`` existed reviewed a base
    nobody recorded. It is not this round's comment (never adopted) and not
    a defect either (a PR mid-flight carries one per earlier round): the round
    is reviewed and the controller posts its own comment."""
    gh = FakeGitHub()
    gh.add_comment(PR, 90, review_comment_body(1, SHA_B, True, ["R1-F1"], base_ref=None))
    gh.add_comment(PR, 91, review_comment_body(2, SHA_A, True, ["R2-F1"], base_ref=None))
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(2, SHA_A, []))], round_done=1)
    assert eng.step().next_phase == "READY_FOR_MERGE"
    prompt = eng.provider.calls[0].prompt
    assert comment_url(PR, 91) not in prompt
    s = load_state(eng.paths.state_file)
    assert s.reviewed_base_ref == "main"
    assert len(gh.effect_writes) == 1
    assert s.last_review_comment_url == comment_url(PR, 950_001)  # the controller's own


def test_review_records_the_merge_base_it_decided_on(tmp_state_dir):
    """#96: the REVIEW entry reads the merge base from GitHub and binds it;
    the completed round records it next to the HEAD and base."""
    gh = FakeGitHub()
    gh.merge_base = MERGE_BASE_B
    prompts = []

    def reviews(req):
        prompts.append(req.prompt)
        return block(review_payload(1, SHA_A, []))

    eng = _in_review(tmp_state_dir, gh, reviews)
    eng.state.current_merge_base_sha = ""  # bound by the controller right before the review
    assert eng.step().next_phase == "READY_FOR_MERGE"
    assert ("get_merge_base_sha", "owner/repo", "main", SHA_A) in gh.calls
    assert f"computed from): `{MERGE_BASE_B}`" in prompts[0]
    s = load_state(eng.paths.state_file)
    assert s.reviewed_merge_base_sha == MERGE_BASE_B and s.current_merge_base_sha == MERGE_BASE_B
    assert (s.reviewed_pr_url, s.reviewed_head_sha, s.reviewed_base_ref) == (PR, SHA_A, "main")


def test_review_entry_with_an_unreadable_merge_base_launches_nothing(tmp_state_dir):
    gh = FakeGitHub()
    gh.merge_base_error = GitHubError("HTTP 404: Not Found")
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(1, SHA_A, []))])
    eng._save()
    with pytest.raises(GitHubError, match="merge base of 'main' and HEAD .* could not be read"):
        eng.step()
    assert eng.provider.calls == []
    s = load_state(eng.paths.state_file)
    assert s.review_round == 0 and s.phase == Phase.REVIEW and s.reviewed_merge_base_sha == ""


def test_review_merge_base_moved_during_the_review_is_stale(tmp_state_dir):
    """#96: the base was rewritten under its name while the reviewer worked
    (same HEAD, same base name). The round is a review of a diff the PR no
    longer shows: stale, round consumed, the new merge base bound for the
    re-review, and the findings carried."""
    gh = FakeGitHub()

    def on_call(req):
        gh.merge_base = MERGE_BASE_B  # main rewritten while the reviewer worked
        return block(review_payload(1, SHA_A, [_finding(1)]))

    eng = _in_review(tmp_state_dir, gh, on_call)
    out = eng.step()
    assert out.next_phase == "REVIEW"
    assert f"PR merge base moved from {MERGE_BASE[:12]} to {MERGE_BASE_B[:12]}" in out.message
    assert "base 'main' was rewritten under its name" in out.message
    assert "1 finding(s) are carried to that review to re-check" in out.message
    s = load_state(eng.paths.state_file)
    assert s.last_review_result == "stale" and s.review_round == 1
    assert s.reviewed_merge_base_sha == MERGE_BASE and s.current_merge_base_sha == MERGE_BASE_B
    assert s.reviewed_head_sha == SHA_A and s.reviewed_base_ref == "main"
    assert s.open_findings == [] and [f["id"] for f in s.prior_findings] == ["R1-F1"]
    assert [r["result"] for r in s.review_history] == ["stale"]


def test_review_base_tip_moved_during_the_review_is_not_stale(tmp_state_dir):
    """Commits landing on the base while the reviewer worked do not move the
    merge base: the round binds and completes."""
    gh = FakeGitHub()

    def on_call(req):
        gh.branch_heads["main"] = SHA_B
        return block(review_payload(1, SHA_A, []))

    eng = _in_review(tmp_state_dir, gh, on_call)
    assert eng.step().next_phase == "READY_FOR_MERGE"
    s = load_state(eng.paths.state_file)
    assert s.last_review_result == "clean" and s.reviewed_merge_base_sha == MERGE_BASE


def test_review_reentry_after_a_base_rewrite_does_not_adopt_the_old_merge_base_comment(
    tmp_state_dir,
):
    """#96, the #93 rule applied to the merge base: round 1 was posted from
    the old merge base and the result was lost; the base was then rewritten
    under its name at the same HEAD. The re-entry binds the new merge base
    and must not take the old comment as this round's (neither adopt it nor
    block on it); the round is reviewed and posted afresh."""
    gh = FakeGitHub()
    gh.add_comment(PR, 100, review_comment_body(1, SHA_A, False, merge_base_sha=MERGE_BASE))
    prompts = []

    def reviews(req):
        prompts.append(req.prompt)
        return block(review_payload(1, SHA_A, []))

    eng = _in_review(tmp_state_dir, gh, reviews)
    gh.merge_base = MERGE_BASE_B
    assert eng.step().next_phase == "READY_FOR_MERGE"
    assert comment_url(PR, 100) not in prompts[0]
    assert f"computed from): `{MERGE_BASE_B}`" in prompts[0]
    s = load_state(eng.paths.state_file)
    assert s.reviewed_merge_base_sha == MERGE_BASE_B
    assert len(gh.effect_writes) == 1
    assert s.last_review_comment_url == comment_url(PR, 950_001)  # the controller's own


def test_review_entry_ignores_a_pre_merge_base_comment_for_the_round(tmp_state_dir):
    """A marker written before ``reviewed_merge_base_sha`` existed (#96)
    reviewed a diff from a merge base nobody recorded. It is not this
    round's comment (never adopted) and not a defect either: the round is
    reviewed and the controller posts its own comment."""
    gh = FakeGitHub()
    gh.add_comment(PR, 90, review_comment_body(1, SHA_B, True, ["R1-F1"], merge_base_sha=None))
    gh.add_comment(PR, 91, review_comment_body(2, SHA_A, True, ["R2-F1"], merge_base_sha=None))
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(2, SHA_A, []))], round_done=1)
    assert eng.step().next_phase == "READY_FOR_MERGE"
    prompt = eng.provider.calls[0].prompt
    assert comment_url(PR, 91) not in prompt
    s = load_state(eng.paths.state_file)
    assert s.reviewed_merge_base_sha == MERGE_BASE
    assert len(gh.effect_writes) == 1
    assert s.last_review_comment_url == comment_url(PR, 950_001)  # the controller's own


def test_new_pr_clears_the_review_binding(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, implement, github=fake_github, origin=True)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    eng.state.reviewed_pr_url = "https://github.com/owner/repo/pull/7"
    eng.state.reviewed_head_sha = SHA_B
    eng.state.reviewed_base_ref = "release/0.x"
    assert eng.step().next_phase == "REVIEW"
    s = load_state(eng.paths.state_file)
    assert (s.reviewed_pr_url, s.reviewed_head_sha, s.reviewed_base_ref) == ("", "", "")
    assert s.current_base_ref == "main" and s.current_pr_url == PR


# -- REMOTE agent isolation: worktree, environment, checkout anchor (#10) ------------
def _worktrees(repo) -> list[str]:
    out = _git(repo, "worktree", "list", "--porcelain")
    return [line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree ")]


def _analyze_with(tmp_state_dir, fake_github, handler, agent=implement):
    """INITIALIZING done; the ANALYZE_EXECUTE agent runs ``handler``, then ``agent``'s work."""
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    eng.step()  # INITIALIZING

    def on_call(req):
        handler(req)
        return agent(req)

    eng.provider._handler = on_call
    return eng


def _implement_without_hooks(req) -> str:
    """:func:`implement`, by an agent whose own git runs no planted hook or monitor.

    What a test plants for the controller's git processes (repository hooks,
    the file-system monitor, ``GIT_*`` variables) must not be tripped by the
    agent's commit, or the test could not tell whose process ran it.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    argv = ["git", "-C", req.cwd, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]
    ident = ["-c", "user.name=t", "-c", "user.email=t@x"]
    message = "Implement the feature (#2)"
    subprocess.run(
        [*argv, *ident, "commit", "-q", "--allow-empty", "-m", message],
        env=env,
        check=True,
        capture_output=True,
    )
    head = subprocess.run(
        [*argv, "rev-parse", "HEAD"], env=env, check=True, capture_output=True, text=True
    ).stdout.strip()
    return block(analyze_payload(head))


def test_remote_agent_runs_in_a_per_issue_worktree_under_the_git_common_dir(
    tmp_state_dir, fake_github
):
    """The agent's cwd is a detached worktree the controller created, not the checkout."""
    repo = git_repo(tmp_state_dir.parent)
    base = _commit(repo, "a.txt", "1", "base")
    (repo / "uncommitted.txt").write_text("operator's work in progress")
    seen = {}

    def handler(req):
        seen["cwd"] = Path(req.cwd)
        seen["exists"] = (Path(req.cwd) / "a.txt").exists()
        seen["sees_uncommitted"] = (Path(req.cwd) / "uncommitted.txt").exists()
        seen["sees_state"] = (Path(req.cwd) / ".autoforge").exists()
        seen["head"] = _git(req.cwd, "rev-parse", "HEAD")
        seen["branch"] = _git(req.cwd, "rev-parse", "--abbrev-ref", "HEAD")

    eng = _analyze_with(tmp_state_dir, fake_github, handler)
    assert eng.step().next_phase == "REVIEW"
    expected = repo / ".git" / "autoforge" / "worktrees" / "2"
    assert seen["cwd"] == expected and seen["exists"]
    assert not seen["sees_uncommitted"] and not seen["sees_state"]
    assert str(expected.resolve()) in _worktrees(repo)
    # Created detached at the checkout's HEAD; the agent's one commit on it is
    # what the controller published.
    assert seen["head"] == base and seen["branch"] == "HEAD"
    assert _git(expected, "rev-parse", "HEAD") == eng.state.current_head_sha
    assert _git(expected, "rev-parse", "HEAD^") == base
    assert _git(expected, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"  # detached
    # The operator's checkout is untouched.
    assert _git(repo, "rev-parse", "HEAD") == base
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert (repo / "uncommitted.txt").exists()


@pytest.mark.parametrize("hooks_from", ["hooks-dir", "core.hooksPath"])
def test_worktree_creation_runs_no_repository_hook_and_holds_no_credential(
    tmp_state_dir, fake_github, tmp_path_factory, monkeypatch, hooks_from
):
    """ADR 0004 D7.3/D7.5 (PR #195 R3-F2): `git worktree add` is a controller process.

    It ran with the controller's whole environment and with the repository's
    hooks and file-system monitor, all agent-writable: a `post-checkout`
    hook planted in the shared repository ran on worktree creation holding
    the controller's GitHub token. Every git process the engine runs to
    create, identify and reuse the worktree is now hardened like the
    pre-merge export, and the worktree is still created detached, with no
    branch.
    """
    repo = git_repo(tmp_state_dir.parent)
    base = _commit(repo, "a.txt", "1", "base")
    planted = tmp_path_factory.mktemp("planted")
    hooks = repo / ".git" / "hooks"
    if hooks_from == "core.hooksPath":
        hooks = planted / "hooks"
        _git(repo, "config", "core.hooksPath", str(hooks))
    hooks.mkdir(exist_ok=True)
    for hook in ("post-checkout", "reference-transaction"):
        (hooks / hook).write_text(f'#!/bin/sh\nenv > "{planted}/hook-{hook}"\n')
        (hooks / hook).chmod(0o755)
    monitor = planted / "fsmonitor"
    monitor.write_text(f'#!/bin/sh\nenv > "{planted}/fsmonitor-ran"\n')
    monitor.chmod(0o755)
    _git(repo, "config", "core.fsmonitor", str(monitor))
    # A filter driver is configuration git still runs (D7.3 accepts that):
    # what it sees is what the checkout's process holds.
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("* filter=leak\n")
    _git(repo, "config", "filter.leak.smudge", f'env > "{planted}/filter-env"; cat')
    decoy = git_repo(planted / "decoy")
    # The test's origin is seeded before the environment is planted: the push
    # publishing its main is the harness's, not a controller process.
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_controllertokenvalue")
    monkeypatch.setenv("GH_TOKEN", "ghp_controllertokenvalue")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hooks))

    eng.step()  # INITIALIZING
    eng.provider._handler = _implement_without_hooks
    real = eng._runner
    requests = []

    def recording(req):
        requests.append(req)
        return (real or execute)(req)

    eng._runner = recording
    assert eng.step().next_phase == "REVIEW"
    expected = repo / ".git" / "autoforge" / "worktrees" / "2"
    assert eng._ensure_agent_worktree() == expected  # the reuse path: identity, registry
    for name in ("GIT_DIR", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
        monkeypatch.delenv(name)

    assert sorted(p.name for p in planted.iterdir()) == ["decoy", "filter-env", "fsmonitor"] + (
        ["hooks"] if hooks_from == "core.hooksPath" else []
    ), "a repository hook or the file-system monitor ran"
    seen = (planted / "filter-env").read_text()
    assert "ghp_controllertokenvalue" not in seen
    assert str(decoy) not in seen and "GIT_CONFIG_COUNT" not in seen
    # Still detached at HEAD, beside an untouched checkout, with no branch made:
    # created at the checkout's HEAD, holding the agent's one commit on it.
    assert _git(expected, "rev-parse", "HEAD") == eng.state.current_head_sha
    assert _git(expected, "rev-parse", "HEAD^") == base
    assert _git(expected, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _git(repo, "for-each-ref", "--format=%(refname)", "refs/heads") == "refs/heads/main"
    assert _git(decoy, "worktree", "list", "--porcelain").count("worktree ") == 1

    run = [r.command[1 + len(LOCAL_GIT_SWITCHES) :] for r in requests if r.command[0] == "git"]
    assert ["worktree", "add", "--detach", str(expected), "HEAD"] in run
    assert ["worktree", "list", "--porcelain"] in run
    assert ["-C", str(expected), "rev-parse", "--show-toplevel", "--git-common-dir"] in run
    network = []
    for req in requests:
        if req.command[0] != "git":
            continue
        assert req.command[: 1 + len(LOCAL_GIT_SWITCHES)] == ["git", *LOCAL_GIT_SWITCHES]
        assert (req.env or {})["GIT_NO_REPLACE_OBJECTS"] == "1"
        if "GIT_DIR" not in (req.env or {}):
            assert req.env_allowlist == LOCAL_GIT_ENV_ALLOWLIST
            continue
        # The controller's own fetch and push (#161) run in the git transport's
        # private directory, never the planted one, with no global or system
        # configuration; only a network operation holds what gh's credential
        # helper needs (its hardening is test_git_transport.py's).
        assert req.env["GIT_DIR"] == req.cwd and str(decoy) not in req.cwd
        assert req.env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert req.env["GIT_CONFIG_NOSYSTEM"] == "1"
        if req.env_allowlist == NETWORK_GIT_ENV_ALLOWLIST:
            network.append(next(a for a in ("fetch", "push") if a in req.command))
        else:
            assert req.env_allowlist == LOCAL_GIT_ENV_ALLOWLIST
    assert network == ["fetch", "push"]


def test_the_issue_worktree_is_reused_across_phases_as_the_agent_left_it(
    tmp_state_dir, fake_github
):
    repo = git_repo(tmp_state_dir.parent)
    _commit(repo, "a.txt", "1", "base")
    eng = _analyze_with(tmp_state_dir, fake_github, lambda req: None)
    assert eng.step().next_phase == "REVIEW"
    wt = Path(eng.provider.calls[0].cwd)
    _git(wt, "checkout", "-q", "-b", BRANCH)  # the agent's branch, left checked out
    (wt / "agent.txt").write_text("agent's work")

    # The same engine, and a fresh engine over the same state, both land in it.
    fake_github.add_pr()
    eng.state.phase = Phase.REVIEW
    eng.state.current_pr_url = PR
    eng.state.current_head_sha = SHA_A
    eng.state.reviewed_head_sha = ""
    eng.provider._handler = lambda req: ""
    try:
        eng.step()
    except Exception:
        pass
    assert Path(eng.provider.calls[-1].cwd) == wt
    assert (wt / "agent.txt").exists()
    assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD") == BRANCH
    assert _worktrees(repo).count(str(wt.resolve())) == 1


def test_a_path_that_is_not_a_worktree_of_this_repository_is_refused_not_adopted(
    tmp_state_dir, fake_github
):
    repo = git_repo(tmp_state_dir.parent)
    _commit(repo, "a.txt", "1", "base")
    stale = repo / ".git" / "autoforge" / "worktrees" / "2"
    stale.mkdir(parents=True)
    (stale / "leftover.txt").write_text("not ours")
    eng = _analyze_with(tmp_state_dir, fake_github, lambda req: None)
    with pytest.raises(VerificationError, match="exists but is not a worktree"):
        eng.step()
    assert eng.provider.calls == [] and (stale / "leftover.txt").exists()

    # A worktree of *another* repository is refused too.
    other = git_repo(tmp_state_dir.parent.parent / f"{tmp_state_dir.parent.name}-other")
    (stale / "leftover.txt").unlink()
    stale.rmdir()
    _git(other, "worktree", "add", "-q", "--detach", str(stale), "HEAD")
    with pytest.raises(VerificationError, match="part of the repository at"):
        eng.step()
    assert eng.provider.calls == []


def test_a_subdirectory_of_an_existing_tree_is_not_adopted_as_a_worktree(
    tmp_state_dir, fake_github
):
    """`worktree_dir` pointing into the operator's checkout: an existing directory
    there answers git as *that* tree, and a new one would nest inside it."""
    repo = git_repo(tmp_state_dir.parent)
    _commit(repo, "a.txt", "1", "base")
    cfg = default_config()
    cfg.execution.worktree_dir = "agents"
    (repo / "agents" / "2").mkdir(parents=True)
    eng = make_engine(tmp_state_dir, [], github=fake_github, cfg=cfg, origin=True)
    eng.step()
    with pytest.raises(VerificationError, match="inside the worktree rooted at"):
        eng.step()
    (repo / "agents" / "2").rmdir()
    with pytest.raises(VerificationError, match="inside the checkout's working tree"):
        eng.step()
    assert eng.provider.calls == [] and _worktrees(repo) == [str(repo.resolve())]


def test_a_symlink_at_the_worktree_path_is_refused_whatever_it_points_to(
    tmp_state_dir, fake_github
):
    """A link at the expected path answers `git -C` and realpath as its *target*;
    a link to the operator's checkout or to another worktree of this repository
    would otherwise be adopted and the agent launched in operator-owned files."""
    repo = git_repo(tmp_state_dir.parent)
    base = _commit(repo, "a.txt", "1", "base")
    (repo / "uncommitted.txt").write_text("operator's work in progress")
    expected = repo / ".git" / "autoforge" / "worktrees" / "2"
    expected.parent.mkdir(parents=True)
    eng = _analyze_with(tmp_state_dir, fake_github, lambda req: None)

    def refused():
        with pytest.raises(VerificationError, match="is a symbolic link") as info:
            eng.step()
        assert "never through a link" in str(info.value)
        assert eng.provider.calls == []
        assert expected.is_symlink()  # left alone, not removed or replaced
        expected.unlink()

    # To the operator's checkout.
    expected.symlink_to(repo)
    refused()
    # To another worktree of the same repository (the operator's own).
    other = repo.parent / f"{repo.name}-operator-tree"
    _git(repo, "worktree", "add", "-q", "--detach", str(other), "HEAD")
    expected.symlink_to(other)
    refused()
    # A relative link, and a dangling one, are no better.
    expected.symlink_to(Path("..") / ".." / "..")
    refused()
    expected.symlink_to(repo / "gone")
    refused()

    # Nothing was adopted or created: the registry and the checkout are as they were.
    assert sorted(_worktrees(repo)) == sorted([str(repo.resolve()), str(other.resolve())])
    assert _git(repo, "rev-parse", "HEAD") == base
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert (repo / "uncommitted.txt").exists()


def test_a_symlinked_parent_of_the_worktree_path_is_refused_before_creation(
    tmp_state_dir, fake_github
):
    """The path itself does not exist, but a component above it (below the
    resolved common dir) is a link into the operator's working tree: `mkdir`
    and `git worktree add` would follow it and create the worktree there, in
    the operator's `git status`, unnoticed by the literal-path guard. (A link
    at `.git/autoforge` itself never gets this far: the controller lock opens
    that directory with O_NOFOLLOW and refuses it first.)"""
    repo = git_repo(tmp_state_dir.parent)
    base = _commit(repo, "a.txt", "1", "base")
    (repo / "src").mkdir()
    _commit(repo, "src/lib.txt", "code", "src")
    (repo / "uncommitted.txt").write_text("operator's work in progress")
    link = repo / ".git" / "autoforge" / "worktrees"
    eng = _analyze_with(tmp_state_dir, fake_github, lambda req: None)
    status_before = _git(repo, "status", "--porcelain")  # after INITIALIZING made .autoforge/

    def refused():
        with pytest.raises(VerificationError, match="reached through a symbolic link") as info:
            eng.step()
        assert "never through a link" in str(info.value)
        assert eng.provider.calls == []
        assert link.is_symlink()  # left alone, not removed or replaced
        assert not (repo / "src" / "2").exists() and not (repo / "2").exists()
        assert _worktrees(repo) == [str(repo.resolve())]
        assert _git(repo, "status", "--porcelain") == status_before

    # `.git/autoforge/worktrees` -> `<checkout>/src` (the round-2 reproduction).
    link.symlink_to(repo / "src")
    refused()
    refused()  # a second launch refuses the same way; nothing was stranded
    link.unlink()
    # ... -> the working tree root itself, and a relative link there.
    link.symlink_to(repo)
    refused()
    link.unlink()
    link.symlink_to(Path("..") / "..")
    refused()
    link.unlink()

    # With the link gone the worktree is created at the literal path as usual.
    assert eng.step().next_phase == "REVIEW"
    expected = link / "2"
    assert Path(eng.provider.calls[0].cwd) == expected and not link.is_symlink()
    assert str(expected.resolve()) in _worktrees(repo)
    assert _git(repo, "status", "--porcelain") == status_before
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert base in _git(repo, "rev-list", "HEAD")


def test_an_existing_worktree_is_reused_only_when_the_repository_registers_it(
    tmp_state_dir, fake_github
):
    """The directory answering git as a worktree of this repository is not
    enough: the entry itself must be in `git worktree list`."""
    repo = git_repo(tmp_state_dir.parent)
    _commit(repo, "a.txt", "1", "base")
    expected = repo / ".git" / "autoforge" / "worktrees" / "2"
    other = repo.parent / f"{repo.name}-operator-tree"
    _git(repo, "worktree", "add", "-q", "--detach", str(other), "HEAD")
    # A copy of a real worktree of this repository: its `.git` file points at the
    # registry entry of the *other* tree, so git answers the copy as that tree.
    shutil.copytree(other, expected, symlinks=True)
    eng = _analyze_with(tmp_state_dir, fake_github, lambda req: None)
    with pytest.raises(VerificationError, match="not a worktree of its own|not registered"):
        eng.step()
    assert eng.provider.calls == []
    shutil.rmtree(expected)

    # A registered worktree at the path is reused; the same one, once
    # registered, remains the agent's cwd.
    _git(repo, "worktree", "add", "-q", "--detach", str(expected), "HEAD")
    assert eng.step().next_phase == "REVIEW"
    assert Path(eng.provider.calls[0].cwd) == expected


def test_the_worktree_registry_read_failing_refuses_reuse(tmp_state_dir, fake_github):
    repo = git_repo(tmp_state_dir.parent)
    _commit(repo, "a.txt", "1", "base")
    expected = repo / ".git" / "autoforge" / "worktrees" / "2"
    _git(repo, "worktree", "add", "-q", "--detach", str(expected), "HEAD")
    eng = _analyze_with(tmp_state_dir, fake_github, lambda req: None)
    real = eng._runner

    def failing(req):
        if req.command[1 + len(LOCAL_GIT_SWITCHES) :][:2] == ["worktree", "list"]:
            return ExecutionResult(req.command, req.cwd, 128, "", "fatal: cannot read", "t", "t")
        return (real or execute)(req)

    eng._runner = failing
    with pytest.raises(VerificationError, match="cannot list the repository's worktrees"):
        eng.step()
    assert eng.provider.calls == []


def test_execution_worktree_dir_relocates_the_agent_worktrees(tmp_state_dir, fake_github):
    repo = git_repo(tmp_state_dir.parent)
    _commit(repo, "a.txt", "1", "base")
    cfg = default_config()
    cfg.execution.worktree_dir = "../agent-trees"
    eng = make_engine(tmp_state_dir, [], github=fake_github, cfg=cfg, origin=True)
    eng.step()
    eng.provider._handler = implement
    assert eng.step().next_phase == "REVIEW"
    expected = (repo.parent / "agent-trees" / "2").resolve()
    assert Path(eng.provider.calls[0].cwd) == expected
    assert str(expected) in _worktrees(repo)
    assert eng.agent_worktree_description() == str(expected)


def test_dry_run_creates_no_worktree_and_describes_the_isolation(tmp_state_dir, fake_github):
    repo = git_repo(tmp_state_dir.parent)
    _commit(repo, "a.txt", "1", "base")
    eng = make_engine(tmp_state_dir, ["never"], github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    plan = eng.step(dry_run=True).plan
    assert plan is not None
    notes = "\n".join(plan.notes)
    assert "<git common dir>/autoforge/worktrees/2" in notes
    assert "allow-listed name(s) from execution.env_allowlist" in notes
    assert "read before and after the invocation; a change enters BLOCKED" in notes
    assert _worktrees(repo) == [str(repo.resolve())]
    assert not (repo / ".git" / "autoforge" / "worktrees").exists()
    assert eng.provider.calls == []


def test_agent_launch_carries_the_configured_environment_allowlist(tmp_state_dir, fake_github):
    cfg = default_config()
    cfg.execution.env_allowlist_extra = ["MY_TOOL_*"]
    repo = git_repo(tmp_state_dir.parent)
    _commit(repo, "a.txt", "1", "base")
    eng = make_engine(tmp_state_dir, [], github=fake_github, cfg=cfg, origin=True)
    eng.step()
    eng.provider._handler = implement
    assert eng.step().next_phase == "REVIEW"
    req = eng.provider.calls[0]
    assert req.env_allowlist == cfg.execution.environment_names()
    assert "MY_TOOL_*" in req.env_allowlist and "PATH" in req.env_allowlist
    run_dir = eng.paths.logs_dir / eng.state.run_id
    step_dir = max(p for p in run_dir.iterdir() if p.is_dir())
    recorded = json.loads((step_dir / "request.json").read_text())
    assert recorded["metadata"]["env_allowlist"] == list(req.env_allowlist)
    assert "environ" not in recorded


def test_validation_and_premerge_commands_run_under_the_allowlist(tmp_state_dir, fake_github):
    """Repository-defined commands get the agent's environment, not the operator's shell."""
    fake_github.add_pr()
    eng = _in_merge(tmp_state_dir, fake_github)
    eng.config.execution.env_allowlist_extra = ["MY_TOOL_*"]
    eng.config.merge.verification_commands = [["true"]]
    seen = []

    def runner(req):
        seen.append(req)
        return ExecutionResult(req.command, req.cwd, 0, "", "", "t", "t")

    eng._runner = runner
    assert eng.step(allow_merge=True).next_phase == "UPDATE_EPIC"
    commands = [r for r in seen if r.command == ["true"]]
    names = eng.config.execution.environment_names()
    assert commands and all(r.env_allowlist == names for r in commands)
    assert all("MY_TOOL_*" in r.env_allowlist for r in commands)


def _drift_engine(tmp_state_dir, fake_github, mutate, fail=False):
    """ANALYZE_EXECUTE whose agent moves the *operator's* checkout while it runs."""
    repo = git_repo(tmp_state_dir.parent)
    _commit(repo, "a.txt", "1", "base")
    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    eng.step()

    def on_call(req):
        mutate(repo)
        if fail:
            raise RuntimeError("agent exploded")
        return implement(req)

    eng.provider._handler = on_call
    return eng


def test_a_commit_to_the_operators_checkout_during_the_agent_run_blocks(tmp_state_dir, fake_github):
    eng = _drift_engine(
        tmp_state_dir, fake_github, lambda repo: _commit(repo, "b.txt", "2", "sneaky")
    )
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    reason = eng.state.block_reason
    assert "moved while ANALYZE_EXECUTE ran" in reason and "HEAD moved from" in reason
    assert "Nothing was rolled back" in reason and "'unblock'" in reason
    assert "claims were not verified and nothing was applied" in reason
    s = load_state(eng.paths.state_file)
    assert s.phase is Phase.BLOCKED and s.current_pr_url == ""
    assert (tmp_state_dir.parent / "b.txt").exists()  # not undone


def test_a_branch_switch_in_the_operators_checkout_during_the_agent_run_blocks(
    tmp_state_dir, fake_github
):
    eng = _drift_engine(
        tmp_state_dir, fake_github, lambda repo: _git(repo, "checkout", "-q", "-b", "elsewhere")
    )
    assert eng.step().next_phase == "BLOCKED"
    reason = eng.state.block_reason
    assert "the checked-out branch changed from main to elsewhere" in reason
    assert "HEAD moved" not in reason


def test_drift_is_reported_even_when_the_invocation_itself_failed(tmp_state_dir, fake_github):
    eng = _drift_engine(
        tmp_state_dir,
        fake_github,
        lambda repo: _commit(repo, "b.txt", "2", "sneaky"),
        fail=True,
    )
    assert eng.step().next_phase == "BLOCKED"
    reason = eng.state.block_reason
    assert "moved while ANALYZE_EXECUTE ran" in reason
    assert "ended with RuntimeError: agent exploded" in reason


def test_an_unchanged_checkout_is_not_drift(tmp_state_dir, fake_github):
    """Agent work in its worktree (commits, branches) is not a change of the checkout."""

    def work_in_worktree(repo):
        wt = repo / ".git" / "autoforge" / "worktrees" / "2"
        _git(wt, "checkout", "-q", "-b", BRANCH)
        _commit(wt, "agent.txt", "x", "agent commit")
        _git(wt, "checkout", "-q", "--detach")  # the controller publishes a detached HEAD

    eng = _drift_engine(tmp_state_dir, fake_github, work_in_worktree)
    assert eng.step().next_phase == "REVIEW"


def test_the_invocation_failure_propagates_when_nothing_moved(tmp_state_dir, fake_github):
    eng = _drift_engine(tmp_state_dir, fake_github, lambda repo: None, fail=True)
    with pytest.raises(RuntimeError, match="agent exploded"):
        eng.step()


def test_dry_run_renders_a_pi_profile_without_launching_it(tmp_state_dir):
    """#129: a Pi reviewer shows its exact argv; the prompt is not in it (stdin RPC, #131)."""
    cfg = default_config()
    cfg.profiles["review_round_2_5"] = replace(
        cfg.profile("review_round_2_5"), provider="pi", command="pi", options={}
    )
    gh = FakeGitHub()
    eng = _in_review(tmp_state_dir, gh, [], round_done=1)
    eng.config = cfg
    plan = eng.step(dry_run=True).plan
    assert plan.command == [
        "pi",
        "--mode",
        "rpc",
        "--no-session",
        "--no-approve",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--offline",
        "--tools",
        "read,bash",
        "--model",
        "openai/gpt-5.6-terra",
        "--thinking",
        "high",
    ]
    assert plan.prompt_full not in plan.command
    assert eng.provider.calls == []
    assert not eng.paths.state_file.exists()


# =====================================================================================
# Pi parity at the engine boundary (#133): the real PiProvider over RPC against a
# fake `pi` (tests/pi_fake.py) is held to what ScriptedProvider is held to.
# =====================================================================================
PI_STDERR = "fake pi: diagnostics on stderr\n"


def _to_pi(eng, tmp_path_factory, script):
    """Route every profile of ``eng`` to Pi, driven by ``script``; return the fake."""
    from tests.pi_fake import PiFake, ScriptedPi, route_to_pi

    fake = PiFake(tmp_path_factory.mktemp("pi"))
    route_to_pi(eng.config, fake)
    eng.pi = ScriptedPi(fake, script)
    eng.providers._overrides["pi"] = eng.pi
    return fake


@pytest.mark.parametrize("claim", ["pull-request", "reviewed-head"])
def test_a_pi_claim_github_does_not_back_fails_as_a_scripted_one_does(tmp_path_factory, claim):
    """A Pi CONTROL_RESULT the controller cannot back is refused by the same
    check, with the same error, and state stays where it was, exactly as for
    ScriptedProvider: a head_sha that is not the worktree's HEAD (#161: a
    refusal the agent is asked to correct, and nothing is pushed), or a
    review of a HEAD the PR is not bound to (#162: a refusal the reviewer is
    asked to correct, and no review comment is posted)."""
    # The correction gets the same answer, so the bound is reached.
    if claim == "pull-request":
        script = [block(analyze_payload(SHA_A))] * 2
    else:
        script = [block(review_payload(1, SHA_B, []))] * 2
    error = ControlResultValidationError
    seen = []
    for on_pi in (False, True):
        gh = FakeGitHub()
        state_dir = tmp_path_factory.mktemp("pi-run" if on_pi else "scripted") / ".autoforge"
        if claim == "pull-request":
            eng = make_engine(state_dir, list(script), github=gh, origin=True)
            eng.state.phase = Phase.ANALYZE_EXECUTE
        else:
            eng = _in_review(state_dir, gh, list(script))
        if on_pi:
            fake = _to_pi(eng, tmp_path_factory, list(script))
        with pytest.raises(error) as excinfo:
            eng.step()
        # Each run's worktree HEAD is its own repository's commit.
        message = re.sub(r"\b(?!a{40})[0-9a-f]{40}\b", "<HEAD>", str(excinfo.value))
        s = load_state(eng.paths.state_file)
        seen.append((message, s.phase, s.current_pr_url, s.review_round, gh.effect_writes))
    assert seen[0] == seen[1]
    assert seen[0][4] == []
    if claim == "pull-request":
        assert f"field 'head_sha' is {SHA_A}, but the worktree's HEAD is <HEAD>" in seen[0][0]
    else:
        assert "field 'reviewed_head_sha'" in seen[0][0]
        assert seen[0][3] == 0 and seen[0][1] == Phase.REVIEW
    assert eng.provider.calls == [] and len(fake.launches()) == len(script)


def test_a_pi_correction_is_a_second_separate_pi_process(
    tmp_state_dir, fake_github, tmp_path_factory
):
    """A malformed Pi result is corrected by a second, separate Pi process:
    two spawns with identical argv and nothing that resumes a session, the
    correction prompt (sent over RPC) carries the parse error, the entry
    reconciliation runs again before the relaunch, and the valid second
    result advances the phase."""
    from tests.pi_fake import PiTurn

    def agent(req):
        if not req.correction:
            return PiTurn(text="I am done, no block here", stderr=PI_STDERR)
        return implement(req)

    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    fake = _to_pi(eng, tmp_path_factory, agent)
    reconciled = []
    reconcile = eng._reconcile_analyze_entry

    def spy(plan):
        reconciled.append(len(fake.launches()))
        return reconcile(plan)

    eng._reconcile_analyze_entry = spy
    eng.state.phase = Phase.ANALYZE_EXECUTE

    assert eng.step().next_phase == "REVIEW"

    first, second = fake.launches()
    assert first["argv"] == second["argv"]
    assert not {"--session", "--continue", "--fork", "--resume"} & set(first["argv"])
    assert "--no-session" in first["argv"]
    assert reconciled == [0, 1]  # before the first launch and again before the relaunch
    prompts = fake.prompts()
    assert len(prompts) == 2 and "did not return a valid CONTROL_RESULT" not in prompts[0]
    assert "did not return a valid CONTROL_RESULT" in prompts[1]
    assert "no CONTROL_RESULT block found" in prompts[1]
    assert [req.correction for req in eng.pi.calls] == [False, True]
    s = load_state(eng.paths.state_file)
    assert s.current_pr_url == PR and s.attempt == 0
    run_dir = eng.paths.logs_dir / eng.state.run_id
    dirs = sorted(p for p in run_dir.iterdir() if p.is_dir())
    assert [d.name[-2:] for d in dirs] == ["-1", "-2"]
    assert (dirs[0] / "error.txt").exists() and (dirs[1] / "control-result.json").exists()


@pytest.mark.parametrize("corrections", [0, 2])
def test_pi_corrections_are_bounded_as_scripted_ones_are(
    tmp_path_factory, fake_github, corrections
):
    """``max_correction_attempts`` bounds Pi launches exactly as it bounds
    ScriptedProvider ones: the same error after the same number of spawns."""
    seen = []
    for on_pi in (False, True):
        state_dir = tmp_path_factory.mktemp("pi-run" if on_pi else "scripted") / ".autoforge"
        eng = make_engine(state_dir, ["junk"] * 5, github=FakeGitHub(), origin=True)
        eng.config.execution.max_correction_attempts = corrections
        if on_pi:
            fake = _to_pi(eng, tmp_path_factory, ["junk"] * 5)
        eng.state.phase = Phase.ANALYZE_EXECUTE
        with pytest.raises((ControlResultValidationError,)) as excinfo:
            eng.step()
        launched = len(fake.launches()) if on_pi else len(eng.provider.calls)
        seen.append((type(excinfo.value), str(excinfo.value), launched))
        assert load_state(eng.paths.state_file).phase == Phase.ANALYZE_EXECUTE
    assert seen[0] == seen[1] and seen[1][2] == corrections + 1


_PI_FAILURES = [
    pytest.param(
        {
            "auth": {
                "status": "not_ready",
                "provider": "openai",
                "reason": "credentials_not_configured",
            },
            "auth_exit": 1,
        },
        "pi: the auth preflight refused the launch: status not_ready",
        id="credential-preflight-refused",
    ),
    pytest.param(
        {"outcome": "reject", "error": "No API key found for openai."},
        "pi: prompt rejected before acceptance: No API key found for openai.",
        id="prompt-rejected",
    ),
    pytest.param(
        {"outcome": "stop_error", "error": "upstream overloaded"},
        "pi: model error after acceptance: upstream overloaded",
        id="stop-reason-error",
    ),
    pytest.param(
        {"outcome": "exit_early", "exit_code": 3},
        "pi: exited before agent_settled (exit 3)",
        id="exit-before-settled",
    ),
    pytest.param(
        {"outcome": "model_mismatch"},
        "pi: model mismatch: configured openai/pi-analyze",
        id="model-mismatch",
    ),
]


@pytest.mark.parametrize("turn,reason", _PI_FAILURES)
def test_a_pi_failure_leaves_state_unchanged_and_resume_relaunches(
    tmp_state_dir, fake_github, tmp_path_factory, turn, reason
):
    """Each Pi failure is an ExecutionError naming Pi's reason; stdout and
    stderr are recorded, controller state does not move, and `resume`
    relaunches the phase, which then advances on a valid result."""
    from tests.pi_fake import PiTurn

    agent = scripted(PiTurn(stderr=PI_STDERR, **turn), implement)

    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    fake = _to_pi(eng, tmp_path_factory, agent)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises(ExecutionError) as excinfo:
        eng.step()
    message = str(excinfo.value)
    assert message.startswith(f"agent 'analyze_execute' failed: {reason}")
    assert "State unchanged" in message and message.endswith("then 'resume'.")
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.ANALYZE_EXECUTE and s.current_pr_url == "" and s.current_head_sha == ""
    _, step = _step_dir(eng)
    execution = json.loads((step / "execution.json").read_text(encoding="utf-8"))
    assert execution["error"].startswith(reason) and execution["provider_summary"]
    assert (step / "stdout.log").read_text(encoding="utf-8") == ""
    stderr = (step / "stderr.log").read_text(encoding="utf-8")
    if "auth" in turn:
        assert fake.launches() == [] and stderr == ""  # refused before the RPC child
    else:
        assert stderr == PI_STDERR
    assert reason in (step / "error.txt").read_text(encoding="utf-8")
    assert not (step / "control-result.json").exists()

    eng.load()  # what `resume` does
    assert eng.step().next_phase == "REVIEW"
    assert load_state(eng.paths.state_file).current_pr_url == PR


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="orphan sweep is Linux-only")
def test_a_pi_deadline_is_a_timeout_naming_what_it_left_behind(
    tmp_state_dir, fake_github, tmp_path_factory
):
    """Pi accepts the prompt and goes silent: the profile deadline ends the
    launch with ExecutionTimeoutError; the detached process its tool started
    is swept and named; state does not move and `resume` relaunches."""
    from tests.pi_fake import PiTurn

    agent = scripted(PiTurn(outcome="hang", detach=60, stderr=PI_STDERR), implement)

    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    _to_pi(eng, tmp_path_factory, agent)
    eng.config.profiles["analyze_execute"] = replace(
        eng.config.profile("analyze_execute"), idle_timeout_seconds=2
    )
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises(ExecutionTimeoutError) as excinfo:
        eng.step()
    message = str(excinfo.value)
    # Pi answered the controller's first requests, then went silent: the
    # idle limit fired, counted from its last record (#193).
    assert re.match(
        r"agent 'analyze_execute' made no progress for 2s "
        r"\(last activity \d\d:\d\d:\d\d UTC\) and was killed",
        message,
    ), message
    assert "outside its process group" in message
    assert message.endswith("then 'resume'.")
    assert load_state(eng.paths.state_file).phase == Phase.ANALYZE_EXECUTE
    _, step = _step_dir(eng)
    execution = json.loads((step / "execution.json").read_text(encoding="utf-8"))
    assert execution["timed_out"] is True and execution["orphans_killed"] is True
    assert execution["timeout_limit"] == "idle" and execution["idle_timeout_seconds"] == 2
    assert execution["max_runtime_seconds"] is None and execution["last_activity_at"]
    assert (step / "stderr.log").read_text(encoding="utf-8") == PI_STDERR

    eng.load()
    assert eng.step().next_phase == "REVIEW"


def test_a_pi_run_log_records_argv_env_and_summary_and_redacts_secrets(
    tmp_state_dir, fake_github, tmp_path_factory, monkeypatch
):
    """request.json holds the argv (no prompt) and the env allowlist (Pi's
    names, no OPENAI_*), execution.json the Pi summary, stdout.log only the
    final assistant text (never the JSONL stream), stderr.log Pi's stderr;
    a credential-shaped string on stderr and in the final text is redacted
    in every file of the run's logs."""
    from autoforge.pi_rpc import js_trim
    from tests.pi_fake import PiTurn

    secret = "sk-proj-" + "Z9" * 12
    monkeypatch.setenv("OPENAI_API_KEY", secret)  # never reaches Pi: not allow-listed
    texts = []

    def agent(req):
        texts.append(f"Used {secret} while working.\n\n" + implement(req))
        return PiTurn(text=texts[-1], stderr=f"warning: token {secret} rejected\n")

    eng = make_engine(tmp_state_dir, [], github=fake_github, origin=True)
    fake = _to_pi(eng, tmp_path_factory, agent)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    assert eng.step().next_phase == "REVIEW"

    (launch,) = fake.launches()
    assert "OPENAI_API_KEY" not in launch["env"]
    run_dir, step = _step_dir(eng)
    request = json.loads((step / "request.json").read_text(encoding="utf-8"))
    prompt = fake.prompts()[0]
    assert request["command"][1:] == launch["argv"]
    assert prompt not in json.dumps(request["command"])
    allowlist = request["metadata"]["env_allowlist"]
    assert {"PI_CODING_AGENT_DIR", "PI_OFFLINE", "PI_TELEMETRY"} <= set(allowlist)
    assert not [name for name in allowlist if name.startswith("OPENAI")]
    execution = json.loads((step / "execution.json").read_text(encoding="utf-8"))
    assert execution["provider_summary"]["stop_reason"] == "stop"
    assert execution["provider_summary"]["failure"] == ""
    stdout = (step / "stdout.log").read_text(encoding="utf-8")
    (text,) = texts
    assert stdout == js_trim(text).replace(secret, "***REDACTED***")
    assert '"agent_settled"' not in stdout and '"type"' not in stdout
    stderr = (step / "stderr.log").read_text(encoding="utf-8")
    assert stderr == "warning: token ***REDACTED*** rejected\n"
    files = [p for p in run_dir.rglob("*") if p.is_file()]
    assert len(files) > 3
    assert not [p for p in files if secret in p.read_text(encoding="utf-8")]


def test_a_pi_dry_run_prints_the_pi_argv_and_spawns_nothing(
    tmp_state_dir, fake_github, tmp_path_factory
):
    """Dry-run renders the Pi argv for the routed profile (no prompt in it)
    and never starts the fake: not the RPC child, not the auth preflight."""
    from tests.pi_fake import PI_PROFILES, flag

    eng = make_engine(tmp_state_dir, [], github=fake_github)
    fake = _to_pi(eng, tmp_path_factory, [])
    eng.state.phase = Phase.ANALYZE_EXECUTE
    plan = eng.step(dry_run=True).plan
    assert plan.command[:3] == [str(fake.command), "--mode", "rpc"]
    assert (flag(plan.command, "--model"), flag(plan.command, "--thinking")) == PI_PROFILES[
        "analyze_execute"
    ]
    assert plan.prompt_full not in plan.command
    assert not fake.spawned() and eng.pi.calls == []
    assert not eng.paths.state_file.exists() and fake_github.calls == []


# -- REVIEW: the controller-posted review comment (K4, #162) -------------------------------
# ADR 0004 K4 at the phase level: the reviewer publishes nothing; the
# controller renders the round's comment from the validated result, journals
# it with the round's verdict in one save, posts it at most once whatever
# window a process dies in, and completes the round from the journal without
# relaunching the reviewer. No step parses the comment.
def _k4_body(rnd: int = 1, findings: list[dict] | None = None, sha: str = SHA_A) -> str:
    """The comment the controller renders for review round ``rnd`` of ``sha``."""
    res = ReviewResult.from_payload(review_result(rnd, sha, findings))
    return render_review_comment(res, sha, "main", MERGE_BASE)


def _review_posts(gh: FakeGitHub) -> list[tuple]:
    return [w for w in gh.effect_writes if w[0] == "create_pr_comment"]


def _round_outcome(eng) -> dict:
    """What a review round leaves in state, minus the clock."""
    s = load_state(eng.paths.state_file)
    return {
        "phase": s.phase,
        "review_round": s.review_round,
        "reviewed": (s.reviewed_pr_url, s.reviewed_head_sha, s.reviewed_base_ref),
        "reviewed_merge_base_sha": s.reviewed_merge_base_sha,
        "current": (s.current_head_sha, s.current_base_ref, s.current_merge_base_sha),
        "last_review": (s.last_review_result, s.last_review_needs_fix),
        "last_review_comment_url": s.last_review_comment_url,
        "open_findings": s.open_findings,
        "prior_findings": s.prior_findings,
        "review_history": [
            {k: v for k, v in entry.items() if k != "timestamp"} for entry in s.review_history
        ],
        "effects": (s.effect_records, s.completion_context),
    }


def _moves_head(gh: FakeGitHub, payload: dict):
    """A reviewer during whose run the PR HEAD moves to SHA_B."""

    def handler(req):
        gh.set_head(SHA_B)
        return block(payload)

    return handler


@pytest.mark.parametrize("findings", [[], [_finding(1)]], ids=["clean", "findings"])
def test_k4_the_controller_renders_posts_and_reads_back_the_round_comment(
    tmp_state_dir, fake_github, findings
):
    eng = _in_review(tmp_state_dir, fake_github, [block(review_result(1, SHA_A, findings))])
    out = eng.step()
    assert out.next_phase == ("FIX" if findings else "READY_FOR_MERGE")
    body = _k4_body(1, findings)
    assert _review_posts(fake_github) == [("create_pr_comment", PR, body)]
    comment = controller_review_comment(eng, 1)
    assert comment.body == body
    assert body.startswith("# AI Code Review — Round 1\n")
    assert f"Reviewed HEAD: `{SHA_A}` against base `main` (merge base `{MERGE_BASE}`)" in body
    assert "## Summary\n\nReady." in body
    assert f"Needs another fix round: {'YES' if findings else 'NO'}" in body
    # The marker is the durable copy of the round: it is written from the same
    # result as the findings the controller persists, and binds what it bound.
    [claim] = scan(REVIEW, comment.body).claims
    assert claim.round == 1 and claim.needs_fix_round == bool(findings)
    assert claim.finding_ids == tuple(f["id"] for f in findings)
    assert (claim.reviewed_head_sha, claim.reviewed_base_ref) == (SHA_A, "main")
    assert claim.reviewed_merge_base_sha == MERGE_BASE
    s = load_state(eng.paths.state_file)
    assert s.last_review_comment_url == comment.url
    assert s.review_history[-1]["review_comment_url"] == comment.url
    assert [f["id"] for f in s.open_findings] == [f["id"] for f in findings]
    assert s.effect_records == [] and s.completion_context == {}
    assert "review_comment_url" not in eng.provider.calls[0].prompt


def test_k4_crash_before_the_plan_is_saved_relaunches_the_reviewer_and_posts_once(
    tmp_state_dir, fake_github
):
    """Nothing was journaled, nothing was sent: the step re-runs from the launch."""
    payload = review_result(1, SHA_A, [_finding(1)])
    eng = _in_review(tmp_state_dir, fake_github, [block(payload)] * 2)
    eng._apply_review = _crash_on_first_call(eng._apply_review)
    with pytest.raises(RuntimeError, match="power loss"):
        eng.step()
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.REVIEW and s.review_round == 0
    assert s.effect_records == [] and s.completion_context == {}
    assert fake_github.effect_writes == []

    eng.load()
    out = eng.step()
    assert out.next_phase == "FIX" and len(eng.provider.calls) == 2
    assert not eng.provider.calls[1].correction
    assert _review_posts(fake_github) == [("create_pr_comment", PR, _k4_body(1, [_finding(1)]))]


def test_k4_intent_saved_and_write_never_issued_posts_once_without_a_relaunch(
    tmp_state_dir, fake_github
):
    """The record is ``intended`` and the round's verdict is journaled with it:
    the comment is posted once from the journal; the reviewer is not asked again."""
    eng = _in_review(tmp_state_dir, fake_github, [block(review_result(1, SHA_A, [_finding(1)]))])
    eng._persist_effect = _crash_on_first_call(eng._persist_effect)
    with pytest.raises(RuntimeError, match="power loss"):
        eng.step()
    s = load_state(eng.paths.state_file)
    [record] = s.effect_records
    assert record["kind"] == "review_comment" and record["stage"] == "intended"
    assert record["attempts"] == 0 and s.completion_context["round"] == 1
    assert s.phase == Phase.REVIEW and s.review_round == 0
    assert fake_github.effect_writes == []

    eng.load()
    out = eng.step()
    assert out.next_phase == "FIX" and "no reviewer launched" in out.message
    assert len(eng.provider.calls) == 1 and len(_review_posts(fake_github)) == 1
    s = load_state(eng.paths.state_file)
    assert [f["id"] for f in s.open_findings] == ["R1-F1"]
    assert s.last_review_comment_url == controller_review_comment(eng, 1).url


def _tamper_body_only(data: dict) -> None:
    """An @-mention added to the persisted body's summary; the context is untouched."""
    payload = data["effect_records"][0]["payload"]
    assert payload["body"].count("## Summary\n\nReady.") == 1
    payload["body"] = payload["body"].replace(
        "## Summary\n\nReady.", "## Summary\n\nReady, @octocat."
    )


def _tamper_section_and_body(data: dict) -> None:
    """The same mention in the context's summary, and the body re-rendered from it."""
    res = ReviewResult.from_payload(review_result(1, SHA_A, [_finding(1)]))
    res.sections["summary"] = "Ready, @octocat."
    data["completion_context"]["sections"]["summary"] = res.sections["summary"]
    data["effect_records"][0]["payload"]["body"] = render_review_comment(
        res, SHA_A, "main", MERGE_BASE
    )


def _tamper_composition(data: dict) -> None:
    """The finding's resolution and the summary, each valid alone, and the body
    re-rendered from them: the resolution leaves a fence open that the
    summary's first fence closes, so the summary's mention is not code."""
    finding = dict(_finding(1), required_resolution="Rename it:\n~~~")
    res = ReviewResult.from_payload(
        review_result(1, SHA_A, [finding], summary="See\n~~~\n@octocat\n~~~")
    )
    data["completion_context"]["findings"] = [f.to_dict() for f in res.findings]
    data["completion_context"]["sections"] = res.sections
    data["effect_records"][0]["payload"]["body"] = render_review_comment(
        res, SHA_A, "main", MERGE_BASE
    )


@pytest.mark.parametrize(
    ("tamper", "needle"),
    [
        pytest.param(
            _tamper_body_only,
            "whose body is not the comment the controller renders from it",
            id="body-only",
        ),
        pytest.param(
            _tamper_section_and_body,
            "sections.summary is invalid: REVIEW: field 'summary' contains an @-mention",
            id="section-and-body",
        ),
        pytest.param(
            _tamper_composition,
            "a review comment the controller may not publish: the rendered 'review comment' "
            "contains an @-mention",
            id="fields-and-body-composed",
        ),
    ],
)
def test_k4_a_persisted_body_altered_after_the_save_is_refused_and_never_posted(
    tmp_state_dir, fake_github, tamper, needle
):
    """The plan is journaled and the write never issued; the file is then edited
    so that the K4 body carries an @-mention, alone, together with the
    context's section it renders, or composed of context fields each valid
    alone (R2-F1). Recovery posts the persisted body with no
    reviewer result in between, so loading the state refuses it (ADR 0004
    D4.6): nothing is posted, the reviewer is not relaunched, and the file is
    left for the operator."""
    eng = _in_review(tmp_state_dir, fake_github, [block(review_result(1, SHA_A, [_finding(1)]))])
    eng._persist_effect = _crash_on_first_call(eng._persist_effect)
    with pytest.raises(RuntimeError, match="power loss"):
        eng.step()
    data = json.loads(eng.paths.state_file.read_text(encoding="utf-8"))
    assert data["effect_records"][0]["stage"] == "intended"
    tamper(data)
    eng.paths.state_file.write_text(json.dumps(data), encoding="utf-8")
    before = eng.paths.state_file.read_bytes()

    with pytest.raises(StateError) as exc:
        eng.load()
    assert needle in str(exc.value) and "@octocat" not in str(exc.value)
    assert fake_github.effect_writes == [] and PR not in fake_github.comments
    assert len(eng.provider.calls) == 1
    assert eng.paths.state_file.read_bytes() == before


@pytest.mark.parametrize("stale", [False, True], ids=["bound", "stale"])
def test_k4_create_landed_save_lost_completes_as_an_uninterrupted_round(
    tmp_path, tmp_state_dir, fake_github, stale
):
    """The comment landed and the process died before the save: the next entry
    observes it (no second post, no relaunch) and the round completes exactly
    as it would have uninterrupted, the stale path's carried findings included."""
    payload = review_result(1, SHA_A, [_finding(1)])

    def run(state_dir, gh):
        script = _moves_head(gh, payload) if stale else [block(payload)]
        return _in_review(state_dir, gh, script)

    reference = run(tmp_path / "reference" / ".autoforge", FakeGitHub())
    reference.step()

    landed = fake_github.create_pr_comment

    def crash_after_write(url, body):
        landed(url, body)
        raise RuntimeError("power loss")

    fake_github.create_pr_comment = crash_after_write
    eng = run(tmp_state_dir, fake_github)
    with pytest.raises(RuntimeError, match="power loss"):
        eng.step()
    [record] = _k8_records(eng)
    assert record["stage"] == "attempted" and len(fake_github.comments[PR]) == 1

    fake_github.create_pr_comment = landed
    eng.load()
    out = eng.step()
    assert out.next_phase == ("REVIEW" if stale else "FIX")
    assert "no reviewer launched" in out.message and len(eng.provider.calls) == 1
    assert len(_review_posts(fake_github)) == 1 and len(fake_github.comments[PR]) == 1
    assert _round_outcome(eng) == _round_outcome(reference)
    if stale:
        s = load_state(eng.paths.state_file)
        assert s.last_review_result == "stale" and s.current_head_sha == SHA_B
        assert [f["id"] for f in s.prior_findings] == ["R1-F1"] and s.open_findings == []


def test_k4_write_lost_in_flight_is_reconciled_then_issued_once_more(tmp_state_dir, fake_github):
    """Attempt persisted, outcome unknown, and the write did not land: never a blind
    re-send in the same step; the next entry reads, finds nothing, posts once more."""
    fake_github.write_failures = [
        ("create_pr_comment", GitHubUnavailableError("gh: timed out"), False)
    ]
    eng = _in_review(tmp_state_dir, fake_github, [block(review_result(1, SHA_A, [_finding(1)]))])
    with pytest.raises(GitHubUnavailableError, match="'resume' reconciles it"):
        eng.step()
    [record] = _k8_records(eng)
    assert record["stage"] == "attempted" and record["attempts"] == 1
    assert len(_review_posts(fake_github)) == 1 and PR not in fake_github.comments
    assert load_state(eng.paths.state_file).review_round == 0

    eng.load()
    out = eng.step()
    assert out.next_phase == "FIX" and len(eng.provider.calls) == 1
    assert len(_review_posts(fake_github)) == 2  # the lost one, then exactly one more
    assert [c.body for c in fake_github.comments[PR]] == [_k4_body(1, [_finding(1)])]


def test_k4_write_landed_with_its_reply_lost_is_observed_by_the_read_back(
    tmp_state_dir, fake_github
):
    """A timeout whose comment landed: the read-back in the same step finds exactly
    one comment with the payload; nothing is sent again."""
    fake_github.write_failures = [
        ("create_pr_comment", GitHubUnavailableError("gh: timed out"), True)
    ]
    eng = _in_review(tmp_state_dir, fake_github, [block(review_result(1, SHA_A))])
    out = eng.step()
    assert out.next_phase == "READY_FOR_MERGE"
    assert len(_review_posts(fake_github)) == 1 and len(fake_github.comments[PR]) == 1
    assert load_state(eng.paths.state_file).last_review_comment_url == (
        controller_review_comment(eng, 1).url
    )


def test_k4_a_round_comment_posted_by_someone_else_between_intent_and_write_blocks(
    tmp_state_dir, fake_github
):
    """The precondition no longer holds and the comment found is not the payload:
    BLOCKED naming it; nothing is posted and the round is not consumed."""
    eng = _in_review(tmp_state_dir, fake_github, [block(review_result(1, SHA_A, [_finding(1)]))])
    eng._persist_effect = _crash_on_first_call(eng._persist_effect)
    with pytest.raises(RuntimeError, match="power loss"):
        eng.step()
    fake_github.add_comment(PR, 310, review_comment_body(1, SHA_A, True, ["R1-F1"]))

    eng.load()
    out = eng.step()
    assert out.next_phase == "BLOCKED" and len(eng.provider.calls) == 1
    s = load_state(eng.paths.state_file)
    assert comment_url(PR, 310) in s.block_reason and "'unblock'" in s.block_reason
    assert _review_posts(fake_github) == []
    assert [r["stage"] for r in s.effect_records] == ["conflict"]
    assert s.review_round == 0 and s.open_findings == [] and s.last_review_comment_url == ""


def test_k4_a_second_matching_comment_after_the_write_blocks_naming_both(
    tmp_state_dir, fake_github
):
    landed = fake_github.create_pr_comment

    def crash_after_write(url, body):
        landed(url, body)
        raise RuntimeError("power loss")

    fake_github.create_pr_comment = crash_after_write
    eng = _in_review(tmp_state_dir, fake_github, [block(review_result(1, SHA_A))])
    with pytest.raises(RuntimeError, match="power loss"):
        eng.step()
    fake_github.create_pr_comment = landed
    ours = fake_github.comments[PR][0].url
    fake_github.add_comment(PR, 320, _k4_body(1))  # a copy, by someone else

    eng.load()
    out = eng.step()
    assert out.next_phase == "BLOCKED" and len(eng.provider.calls) == 1
    s = load_state(eng.paths.state_file)
    assert ours in s.block_reason and comment_url(PR, 320) in s.block_reason
    assert len(_review_posts(fake_github)) == 1 and len(fake_github.comments[PR]) == 2
    assert s.review_round == 0 and s.last_review_comment_url == ""


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("Looks fine. <!-- ai-review-result: {} -->", "marker"),
        ("Ping @someone about it.", "mention"),
        ("This closes #7 as well.", "closing"),
        ("Use GITHUB_TOKEN=ghp_" + "a" * 36 + " to test.", "credential"),
    ],
    ids=["marker", "mention", "closing-keyword", "credential"],
)
def test_k4_prose_the_controller_would_publish_is_corrected_never_posted(
    tmp_state_dir, fake_github, text, problem
):
    hostile = block(review_result(1, SHA_A, summary=text))
    eng = _in_review(tmp_state_dir, fake_github, [hostile, block(review_result(1, SHA_A))])
    assert eng.step().next_phase == "READY_FOR_MERGE"
    assert len(eng.provider.calls) == 2 and eng.provider.calls[1].correction
    assert "summary" in eng.provider.calls[1].prompt
    assert _review_posts(fake_github) == [("create_pr_comment", PR, _k4_body(1))]
    assert all(text not in c.body for c in fake_github.comments[PR])


@pytest.mark.parametrize(
    ("findings", "sections"),
    [
        pytest.param(
            [
                dict(_finding(1, 1), required_resolution="Rename it:\n~~~"),
                dict(_finding(1, 2), required_resolution="See\n~~~\n@octocat\n~~~"),
            ],
            {},
            id="a-fence-closed-by-the-next-finding",
        ),
        pytest.param(
            [],
            {"observations": "Logs:\n```", "verification": "Ran:\n```\n@octocat"},
            id="a-fence-closed-by-the-next-section",
        ),
        pytest.param(
            [dict(_finding(1), title="Use `@octocat`")],
            {"verification": "<pre>"},
            id="raw-html-beside-a-mention-in-code",
        ),
    ],
)
def test_k4_fields_that_compose_a_mention_outside_code_are_corrected_never_posted(
    tmp_state_dir, fake_github, findings, sections
):
    """R2-F1, D8.2: every field passes the policy alone, but in the rendered
    comment one field's fence or raw HTML leaves another's mention outside
    code. The comment is judged as composed before anything is planned: a
    correction, with nothing journaled or posted."""
    hostile = block(review_result(1, SHA_A, findings, **sections))
    eng = _in_review(tmp_state_dir, fake_github, [hostile, block(review_result(1, SHA_A))])
    assert eng.step().next_phase == "READY_FOR_MERGE"
    assert len(eng.provider.calls) == 2 and eng.provider.calls[1].correction
    prompt = eng.provider.calls[1].prompt
    assert "the rendered 'review comment' contains an @-mention" in prompt
    assert _review_posts(fake_github) == [("create_pr_comment", PR, _k4_body(1))]
    assert all("@octocat" not in c.body for c in fake_github.comments[PR])


def test_k4_the_reviewers_code_stays_code_in_the_posted_comment(tmp_state_dir, fake_github):
    """R2-F1: a title ending in an unpaired backtick and a resolution opening
    with a fence are each a block of the comment, so the mention each holds
    in code is still code once composed (as a list item it was not): the
    result is accepted as returned and posted once."""
    findings = [
        dict(_finding(1, 1, "blocked"), title="A `", required_resolution="`@octocat`"),
        dict(_finding(1, 2, "blocked"), required_resolution="~~~\n@octocat\n~~~"),
    ]
    eng = _in_review(tmp_state_dir, fake_github, [block(review_result(1, SHA_A, findings))])
    assert eng.step().next_phase == "FIX"
    assert len(eng.provider.calls) == 1
    body = _k4_body(1, findings)
    assert _review_posts(fake_github) == [("create_pr_comment", PR, body)]
    assert "\n\nRequired resolution:\n\n`@octocat`\n\n" in body
    assert "\n\nRequired resolution:\n\n~~~\n@octocat\n~~~\n\n" in body
    before_marker = body.rsplit("\n\n", 1)[0]
    assert published_markdown_problem("review comment", before_marker) is None


def test_k4_an_oversized_rendered_comment_is_corrected_before_any_effect(
    tmp_state_dir, fake_github
):
    """D8.6: every field within its own bound, the comment over GitHub's limit."""
    resolution = ("rename the helper and update every caller " * 60)[:MAX_FINDING_RESOLUTION_CHARS]
    big = [dict(_finding(1, n), required_resolution=resolution) for n in range(1, 34)]
    payload = review_result(1, SHA_A, big)
    assert len(_k4_body(1, big)) > 65536
    eng = _in_review(tmp_state_dir, fake_github, [block(payload), block(review_result(1, SHA_A))])
    assert eng.step().next_phase == "READY_FOR_MERGE"
    assert eng.provider.calls[1].correction
    assert "over GitHub's limit of 65536" in eng.provider.calls[1].prompt
    assert _review_posts(fake_github) == [("create_pr_comment", PR, _k4_body(1))]


def test_k4_unblock_with_a_saved_plan_completes_the_round_without_the_reviewer(
    tmp_state_dir, fake_github
):
    """A conflict blocked the planned comment; once the operator removes the stray
    comment, 'unblock' routes back to REVIEW, which posts from the plan."""
    eng = _in_review(tmp_state_dir, fake_github, [block(review_result(1, SHA_A, [_finding(1)]))])
    eng._persist_effect = _crash_on_first_call(eng._persist_effect)
    with pytest.raises(RuntimeError, match="power loss"):
        eng.step()
    fake_github.add_comment(PR, 340, review_comment_body(1, SHA_A, True, ["R1-F1"]))
    eng.load()
    assert eng.step().next_phase == "BLOCKED"

    fake_github.comments[PR] = []  # the operator deletes the stray comment
    eng.load()
    unblocked = eng.unblock("removed the stray review comment")
    assert unblocked.unblocked and unblocked.phase == "REVIEW"
    assert "without launching the reviewer" in unblocked.message
    out = eng.step()
    assert out.next_phase == "FIX" and "no reviewer launched" in out.message
    assert len(eng.provider.calls) == 1
    assert _review_posts(fake_github) == [("create_pr_comment", PR, _k4_body(1, [_finding(1)]))]
    s = load_state(eng.paths.state_file)
    assert [f["id"] for f in s.open_findings] == ["R1-F1"] and s.effect_records == []


def test_k4_the_entry_fetches_the_bound_head_and_merge_base_before_the_launch(
    tmp_state_dir, fake_github, offline_fetches
):
    eng = _in_review(tmp_state_dir, fake_github, [block(review_result(1, SHA_A))])
    offline_fetches.clear()
    eng.step()
    assert offline_fetches == [[SHA_A, MERGE_BASE]]


def test_k4_a_failed_fetch_launches_nothing(tmp_state_dir, fake_github, monkeypatch):
    def refuse(self, revisions):
        raise GitTransportError("fatal: could not read from remote repository")

    eng = _in_review(tmp_state_dir, fake_github, [block(review_result(1, SHA_A))])
    monkeypatch.setattr("autoforge.engine.GitTransport.fetch", refuse)
    with pytest.raises(VerificationError, match="before launching the reviewer"):
        eng.step()
    assert eng.provider.calls == [] and fake_github.effect_writes == []
    assert load_state(eng.paths.state_file).phase == Phase.REVIEW


def test_k4_dry_run_names_the_comment_it_would_post_and_posts_nothing(
    tmp_state_dir, fake_github, offline_fetches
):
    eng = _in_review(tmp_state_dir, fake_github, ["never"])
    offline_fetches.clear()
    calls_before = list(fake_github.calls)
    out = eng.step(dry_run=True)
    assert any("review comment" in n for n in out.plan.notes)
    assert eng.provider.calls == [] and fake_github.effect_writes == []
    assert offline_fetches == [] and fake_github.calls == calls_before


def test_k4_dry_run_touches_no_github_at_all(tmp_state_dir, fake_github, offline_fetches):
    """The dry-run invariant against a client that fails on any use, so no read
    the plan might make is overlooked, and against the state file's bytes."""
    eng = _in_review(tmp_state_dir, fake_github, ["never"])
    eng._save()
    eng._github = _NoGitHub()
    before = eng.paths.state_file.read_bytes()
    offline_fetches.clear()
    out = eng.step(dry_run=True)
    assert any("post it on the PR itself" in n for n in out.plan.notes)
    assert eng.provider.calls == [] and offline_fetches == []
    assert eng.paths.state_file.read_bytes() == before


def test_k4_dry_run_with_a_saved_plan_names_the_comment_and_posts_nothing(
    tmp_state_dir, fake_github, offline_fetches
):
    """A plan saved before a crash: the dry run says the round completes from it
    without the reviewer, and reads, fetches, launches and posts nothing."""
    eng = _in_review(tmp_state_dir, fake_github, [block(review_result(1, SHA_A, [_finding(1)]))])
    eng._persist_effect = _crash_on_first_call(eng._persist_effect)
    with pytest.raises(RuntimeError, match="power loss"):
        eng.step()

    eng.load()
    eng._github = _NoGitHub()
    before = eng.paths.state_file.read_bytes()
    offline_fetches.clear()
    out = eng.step(dry_run=True)
    notes = "\n".join(out.plan.notes)
    assert "would complete review round 1 from the persisted REVIEW plan" in notes
    assert "would reconcile" in notes and "(intended, 0 attempt(s))" in notes
    assert len(eng.provider.calls) == 1 and fake_github.effect_writes == []
    assert offline_fetches == [] and eng.paths.state_file.read_bytes() == before
