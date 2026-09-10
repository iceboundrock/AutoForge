"""Engine: verification of agent claims, SHA binding, routing, recovery, gate."""

import json
import re

import pytest

from autoforge.errors import (
    ControlResultValidationError,
    ExecutionError,
    ExecutionTimeoutError,
    GitHubError,
    GitHubUnavailableError,
    StateTransitionError,
    VerificationError,
)
from autoforge.executor import ExecutionResult
from autoforge.github import CheckInfo, GitHubClient, MergeQueueStatus
from autoforge.loop_guard import RESULT_NEEDS_FIX, review_record
from autoforge.providers import AgentExecutionResult, ScriptedProvider
from autoforge.state import load_state
from autoforge.transitions import Phase
from tests.conftest import (
    BRANCH,
    EPIC,
    ISSUE,
    ISSUE3,
    PR,
    SHA_A,
    SHA_B,
    SHA_C,
    FakeGitHub,
    block,
    comment_url,
    make_engine,
    review_comment_body,
)

ANALYZE_OK = {
    "phase": "ANALYZE_EXECUTE",
    "status": "success",
    "issue_url": ISSUE,
    "pr_url": PR,
    "head_sha": SHA_A,
    "branch": BRANCH,
}


def _finding(rnd: int, n: int = 1, cls: str = "nit") -> dict:
    return {
        "id": f"R{rnd}-F{n}",
        "classification": cls,
        "title": "typo",
        "location": "src/x.py:1",
        "required_resolution": "fix the typo",
    }


def review_payload(rnd: int, sha: str, findings: list[dict], cid: int = 100) -> dict:
    return {
        "phase": "REVIEW",
        "status": "success",
        "round": rnd,
        "reviewed_head_sha": sha,
        "review_comment_url": comment_url(PR, cid),
        "needs_fix_round": bool(findings),
        "findings": findings,
    }


def _in_review(tmp_state_dir, gh: FakeGitHub, script, round_done: int = 0, head: str = SHA_A):
    eng = make_engine(tmp_state_dir, script, github=gh)
    gh.add_pr(head_sha=head)
    eng.state.phase = Phase.REVIEW
    eng.state.current_pr_url = PR
    eng.state.current_branch = BRANCH
    eng.state.current_head_sha = head
    eng.state.review_round = round_done
    return eng


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
def test_analyze_valid_pr_verified_enters_review(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, [block(ANALYZE_OK)], github=fake_github)
    eng.step()  # INITIALIZING

    def on_call(req):  # the agent "creates" the PR as a side effect
        fake_github.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
        return block(ANALYZE_OK)

    eng.provider._handler = on_call
    out = eng.step()
    assert out.next_phase == "REVIEW"
    s = load_state(eng.paths.state_file)
    assert s.current_pr_url == PR and s.current_head_sha == SHA_A and s.current_branch == BRANCH
    assert s.review_round == 0
    call = eng.provider.calls[0]
    assert call.phase == "ANALYZE_EXECUTE" and call.profile.model == "fable"
    assert call.profile.effort == "high"


def test_analyze_fake_pr_url_rejected(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, [block(ANALYZE_OK)], github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises(VerificationError, match="cannot resolve"):
        eng.step()
    assert load_state(eng.paths.state_file).phase == Phase.ANALYZE_EXECUTE


def test_analyze_head_sha_mismatch_rejected(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, [block(ANALYZE_OK)], github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    eng.provider._handler = lambda req: (fake_github.add_pr(head_sha=SHA_B), block(ANALYZE_OK))[1]
    with pytest.raises(VerificationError, match="head mismatch"):
        eng.step()
    assert eng.state.phase == Phase.ANALYZE_EXECUTE
    assert eng.state.current_pr_url == ""  # never entered REVIEW


def test_analyze_issue_repo_mismatch_rejected(tmp_state_dir, fake_github):
    payload = dict(ANALYZE_OK, issue_url="https://github.com/other/repo/issues/2")
    eng = make_engine(tmp_state_dir, [block(payload)], github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises(VerificationError, match="issue"):
        eng.step()


def test_analyze_pr_in_other_repo_rejected(tmp_state_dir, fake_github):
    payload = dict(ANALYZE_OK, pr_url="https://github.com/other/repo/pull/1")
    eng = make_engine(tmp_state_dir, [block(payload)], github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises(VerificationError, match="outside repository"):
        eng.step()


def test_analyze_branch_mismatch_rejected(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_A, branch="feature/other")
    eng = make_engine(tmp_state_dir, [block(ANALYZE_OK)], github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    # recovery does not match (branch not autoforge/2-*, not linked) -> agent runs
    with pytest.raises(VerificationError, match="branch mismatch"):
        eng.step()


# -- recovery ------------------------------------------------------------------------
def test_recovery_existing_open_pr_skips_agent(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_B, branch="autoforge/2-x")
    eng = make_engine(tmp_state_dir, ["never"], github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    out = eng.step()
    assert out.next_phase == "REVIEW" and "recovered" in out.message
    assert eng.provider.calls == []
    assert eng.state.current_pr_url == PR and eng.state.current_head_sha == SHA_B


def test_recovery_after_crash_with_persisted_pr(tmp_state_dir, fake_github):
    """PR created + state persisted, crash before transition -> resume recovers."""
    fake_github.add_pr(head_sha=SHA_A, branch="feature/x")  # not detectable by naming
    eng = make_engine(tmp_state_dir, ["never"], github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    eng.state.current_pr_url = PR
    eng._save()
    eng2 = make_engine(tmp_state_dir, ["never"], github=fake_github)
    eng2.load()
    out = eng2.step()
    assert out.next_phase == "REVIEW" and eng2.provider.calls == []


def test_recovery_ambiguous_blocks(tmp_state_dir, fake_github):
    fake_github.add_pr(url=PR, head_sha=SHA_A, branch="autoforge/2-a")
    fake_github.add_pr(url="https://github.com/owner/repo/pull/43", head_sha=SHA_B, linked=[2])
    eng = make_engine(tmp_state_dir, ["never"], github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    out = eng.step()
    assert out.next_phase == "BLOCKED" and "multiple open PRs" in eng.state.block_reason
    assert eng.provider.calls == []


def test_recovery_pr_not_persisted_agent_reuses(tmp_state_dir, fake_github):
    """PR exists on GitHub, nothing persisted: naming rule recovers it."""
    fake_github.add_pr(head_sha=SHA_A, branch="autoforge/2")
    eng = make_engine(tmp_state_dir, ["never"], github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    assert eng.step().next_phase == "REVIEW"


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
    gh.add_comment(PR, 100, review_comment_body(rnd, SHA_A, False))
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
    gh.add_comment(PR, 100, review_comment_body(1, SHA_A, True, ["R1-F1"]))
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(1, SHA_A, [_finding(1)]))])
    out = eng.step()
    assert out.next_phase == "FIX"
    s = load_state(eng.paths.state_file)
    assert s.open_findings[0]["id"] == "R1-F1" and s.last_review_needs_fix is True
    assert s.last_review_comment_url == comment_url(PR, 100)


def test_review_invariant_mismatch_rejected(tmp_state_dir):
    gh = FakeGitHub()
    payload = review_payload(1, SHA_A, [_finding(1)])
    payload["needs_fix_round"] = False
    eng = _in_review(tmp_state_dir, gh, [block(payload)])
    eng.config.execution.max_correction_attempts = 0
    with pytest.raises(ControlResultValidationError, match="needs_fix_round"):
        eng.step()
    assert eng.state.review_round == 0  # failed invocation does not consume a round


def test_review_comment_missing_rejected(tmp_state_dir):
    gh = FakeGitHub()
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(1, SHA_A, []))])
    with pytest.raises(VerificationError, match="not found on PR"):
        eng.step()
    assert eng.state.review_round == 0 and eng.state.phase == Phase.REVIEW


def test_review_comment_wrong_round_marker_rejected(tmp_state_dir):
    gh = FakeGitHub()
    gh.add_comment(PR, 100, review_comment_body(2, SHA_A, False))
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(1, SHA_A, []))])
    with pytest.raises(VerificationError, match="Round 1"):
        eng.step()


def test_review_comment_wrong_sha_marker_rejected(tmp_state_dir):
    gh = FakeGitHub()
    gh.add_comment(PR, 100, review_comment_body(1, SHA_B, False))
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(1, SHA_A, []))])
    with pytest.raises(VerificationError, match="reviewed_head_sha"):
        eng.step()


def test_review_comment_on_other_pr_rejected(tmp_state_dir):
    gh = FakeGitHub()
    payload = review_payload(1, SHA_A, [])
    payload["review_comment_url"] = comment_url("https://github.com/owner/repo/pull/7", 100)
    eng = _in_review(tmp_state_dir, gh, [block(payload)])
    with pytest.raises(VerificationError, match="does not belong"):
        eng.step()


def test_review_wrong_reviewed_sha_rejected(tmp_state_dir):
    gh = FakeGitHub()
    gh.add_comment(PR, 100, review_comment_body(1, SHA_B, False))
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(1, SHA_B, []))])
    with pytest.raises(VerificationError, match="SHA mismatch"):
        eng.step()


def test_review_round_mismatch_rejected(tmp_state_dir):
    gh = FakeGitHub()
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(3, SHA_A, []))])
    with pytest.raises(VerificationError, match="round mismatch"):
        eng.step()


def test_review_binds_head_fetched_before_review(tmp_state_dir):
    """State says SHA_A but GitHub says SHA_B: the review must target SHA_B."""
    gh = FakeGitHub()
    gh.add_comment(PR, 100, review_comment_body(1, SHA_B, False))
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(1, SHA_B, []))], head=SHA_A)
    gh.set_head(SHA_B)
    assert eng.step().next_phase == "READY_FOR_MERGE"
    assert SHA_B in eng.provider.calls[0].prompt
    assert eng.state.reviewed_head_sha == SHA_B


def test_review_head_changes_during_review_re_reviews(tmp_state_dir):
    gh = FakeGitHub()
    gh.add_comment(PR, 100, review_comment_body(1, SHA_A, False))

    def on_call(req):
        gh.set_head(SHA_B)  # someone pushed while the reviewer was working
        return block(review_payload(1, SHA_A, []))

    eng = _in_review(tmp_state_dir, gh, on_call)
    out = eng.step()
    assert out.next_phase == "REVIEW" and "moved" in out.message
    s = eng.state
    assert s.review_round == 1 and s.last_review_result == "stale"
    assert s.current_head_sha == SHA_B and s.reviewed_head_sha == SHA_A


def test_review_clean_then_ready_for_merge_holds(tmp_state_dir):
    gh = FakeGitHub()
    gh.add_comment(PR, 100, review_comment_body(1, SHA_A, False))
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
def _in_fix(tmp_state_dir, gh: FakeGitHub, script, findings=None):
    eng = make_engine(tmp_state_dir, script, github=gh)
    gh.add_pr(head_sha=SHA_A)
    eng.state.phase = Phase.FIX
    eng.state.current_pr_url = PR
    eng.state.current_head_sha = SHA_A
    eng.state.reviewed_head_sha = SHA_A
    eng.state.review_round = 1
    eng.state.last_review_comment_url = comment_url(PR, 100)
    eng.state.open_findings = findings if findings is not None else [_finding(1)]
    return eng


def fix_payload(prev: str, new: str, resolutions: list[dict]) -> dict:
    return {
        "phase": "FIX",
        "status": "success",
        "previous_head_sha": prev,
        "new_head_sha": new,
        "resolutions": resolutions,
    }


def test_fix_valid_head_changed(tmp_state_dir):
    gh = FakeGitHub()

    def on_call(req):
        gh.set_head(SHA_B)
        return block(fix_payload(SHA_A, SHA_B, [{"finding_id": "R1-F1", "resolution": "fixed"}]))

    eng = _in_fix(tmp_state_dir, gh, on_call)
    out = eng.step()
    assert out.next_phase == "REVIEW"
    call = eng.provider.calls[0]
    assert call.profile.model == "fable" and call.profile.effort == "high"
    assert "R1-F1" in call.prompt and comment_url(PR, 100) in call.prompt
    s = eng.state
    assert s.current_head_sha == SHA_B and s.open_findings == []
    assert s.last_fix_resolutions[0]["resolution"] == "fixed"
    assert s.review_round == 1  # next review is round 2


def test_fix_returned_sha_mismatch_rejected(tmp_state_dir):
    gh = FakeGitHub()

    def on_call(req):
        gh.set_head(SHA_C)
        return block(fix_payload(SHA_A, SHA_B, [{"finding_id": "R1-F1", "resolution": "fixed"}]))

    eng = _in_fix(tmp_state_dir, gh, on_call)
    with pytest.raises(VerificationError, match="actual PR HEAD"):
        eng.step()
    assert eng.state.phase == Phase.FIX


def test_fix_claims_fixed_without_new_commit_rejected(tmp_state_dir):
    gh = FakeGitHub()
    eng = _in_fix(
        tmp_state_dir,
        gh,
        [block(fix_payload(SHA_A, SHA_A, [{"finding_id": "R1-F1", "resolution": "fixed"}]))],
    )
    with pytest.raises(VerificationError, match="did not change"):
        eng.step()


def test_fix_previous_sha_mismatch_rejected(tmp_state_dir):
    gh = FakeGitHub()
    eng = _in_fix(
        tmp_state_dir,
        gh,
        [block(fix_payload(SHA_C, SHA_B, [{"finding_id": "R1-F1", "resolution": "fixed"}]))],
    )
    with pytest.raises(VerificationError, match="previous_head_sha"):
        eng.step()


def test_fix_must_cover_all_findings(tmp_state_dir):
    gh = FakeGitHub()
    findings = [_finding(1, 1), _finding(1, 2, "blocked")]

    def on_call(req):
        gh.set_head(SHA_B)
        return block(fix_payload(SHA_A, SHA_B, [{"finding_id": "R1-F1", "resolution": "fixed"}]))

    eng = _in_fix(tmp_state_dir, gh, on_call, findings)
    with pytest.raises(VerificationError, match="missing=\\['R1-F2'\\]"):
        eng.step()


def test_fix_follow_up_issue_verified(tmp_state_dir):
    gh = FakeGitHub()
    gh.add_issue(ISSUE3, "follow-up")
    res = [
        {"finding_id": "R1-F1", "resolution": "follow_up_created", "follow_up_issue_url": ISSUE3}
    ]
    eng = _in_fix(tmp_state_dir, gh, [block(fix_payload(SHA_A, SHA_A, res))])
    assert eng.step().next_phase == "REVIEW"  # no commit needed for a pure follow-up
    assert ("get_issue", ISSUE3) in gh.calls


def test_fix_follow_up_issue_missing_rejected(tmp_state_dir):
    gh = FakeGitHub()
    res = [
        {"finding_id": "R1-F1", "resolution": "follow_up_created", "follow_up_issue_url": ISSUE3}
    ]
    eng = _in_fix(tmp_state_dir, gh, [block(fix_payload(SHA_A, SHA_A, res))])
    with pytest.raises(VerificationError, match="does not exist"):
        eng.step()


def test_fix_follow_up_issue_other_repo_rejected(tmp_state_dir):
    gh = FakeGitHub()
    res = [
        {
            "finding_id": "R1-F1",
            "resolution": "follow_up_created",
            "follow_up_issue_url": "https://github.com/other/repo/issues/9",
        }
    ]
    eng = _in_fix(tmp_state_dir, gh, [block(fix_payload(SHA_A, SHA_A, res))])
    with pytest.raises(VerificationError, match="outside"):
        eng.step()


def test_fix_no_change_requires_rationale(tmp_state_dir):
    gh = FakeGitHub()
    res = [{"finding_id": "R1-F1", "resolution": "no_change_with_rationale", "rationale": "nope"}]
    eng = _in_fix(tmp_state_dir, gh, [block(fix_payload(SHA_A, SHA_A, res))])
    eng.config.execution.max_correction_attempts = 0
    with pytest.raises(ControlResultValidationError, match="rationale"):
        eng.step()


# -- correction retry ---------------------------------------------------------------------
def test_malformed_result_triggers_one_correction(tmp_state_dir, fake_github):
    def agent(req):
        if not req.correction:
            fake_github.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])  # PR was created…
            return "no block here\n"  # …but the result block was lost
        return block(ANALYZE_OK)  # correction run recovers and reports it

    eng = make_engine(tmp_state_dir, agent, github=fake_github)
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


def test_correction_is_bounded(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, ["junk", "junk again"], github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises(ControlResultValidationError, match="after 2 attempt"):
        eng.step()
    assert len(eng.provider.calls) == 2
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.ANALYZE_EXECUTE and s.attempt == 2


def test_correction_disabled(tmp_state_dir, fake_github):
    from autoforge.errors import ControlResultError

    eng = make_engine(tmp_state_dir, ["junk", block(ANALYZE_OK)], github=fake_github)
    eng.config.execution.max_correction_attempts = 0
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises((ControlResultError, ControlResultValidationError)):
        eng.step()
    assert len(eng.provider.calls) == 1


def test_nonzero_exit_is_not_corrected(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, ["junk", "junk"], github=fake_github, exit_code=3)
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

    eng = make_engine(tmp_state_dir, [], github=fake_github)
    eng.providers._overrides = {"claude": TimingOut(), "opencode": TimingOut()}
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises(ExecutionTimeoutError):
        eng.step()
    assert eng.state.phase == Phase.ANALYZE_EXECUTE


# -- agent-reported failure / blocked ----------------------------------------------------------
def test_agent_failure_moves_to_failed(tmp_state_dir, fake_github):
    payload = {"phase": "ANALYZE_EXECUTE", "status": "failure", "message": "tests red"}
    eng = make_engine(tmp_state_dir, [block(payload)], github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    out = eng.step()
    assert out.next_phase == "FAILED" and eng.state.block_reason == "tests red"


def test_agent_blocked_moves_to_blocked(tmp_state_dir, fake_github):
    payload = {"phase": "ANALYZE_EXECUTE", "status": "blocked", "message": "need decision"}
    eng = make_engine(tmp_state_dir, [block(payload)], github=fake_github)
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
def _in_merge(tmp_state_dir, gh, script=None, reviewed=SHA_A, clean=True, phase=Phase.MERGE):
    """Engine parked in MERGE with the gate open and a clean review bound to ``reviewed``."""
    eng = make_engine(tmp_state_dir, script or [], github=gh)
    eng.config.safety.allow_merge = True
    eng.state.phase = phase
    eng.state.current_pr_url = PR
    eng.state.current_head_sha = reviewed
    eng.state.reviewed_head_sha = reviewed
    eng.state.last_review_result = "clean" if clean else "needs_fix"
    eng.state.review_round = 2
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
    eng.state.reviewed_head_sha = SHA_A
    eng.state.last_review_result = "clean"
    eng.state.review_round = 2
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
    eng = make_engine(tmp_state_dir, [block(payload)], github=fake_github)
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
    assert req["provider"] == "claude" and req["prompt_version"] == "v1"
    assert "environ" not in req and req["timeout_seconds"] > 0
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
        CheckInfo(name="ci", state="COMPLETED", conclusion="FAILURE"),
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
        CheckInfo(name="ci", state="IN_PROGRESS"),
        CheckInfo(name="ci", state="QUEUED"),
        CheckInfo(name="ci", state="PENDING"),  # legacy commit status
    ],
)
def test_merge_waits_for_pending_checks_without_merging(tmp_state_dir, fake_github, check):
    fake_github.add_pr().checks = [check]
    eng = _in_merge(tmp_state_dir, fake_github)
    with pytest.raises(VerificationError, match="still running: ci"):
        eng.step(allow_merge=True)
    assert eng.state.phase == Phase.MERGE and fake_github.merges == []
    fake_github.prs[PR].checks = [CheckInfo(name="ci", state="COMPLETED", conclusion="SUCCESS")]
    assert eng.step(allow_merge=True).next_phase == "UPDATE_EPIC"


def test_merge_with_all_checks_green_proceeds(tmp_state_dir, fake_github):
    fake_github.add_pr().checks = [
        CheckInfo(name="ci", state="COMPLETED", conclusion="SUCCESS"),
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
    pr.checks = [CheckInfo(name="ci", state="COMPLETED", conclusion="FAILURE")]
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
            lambda pr: setattr(
                pr, "checks", [CheckInfo(name="ci", state="COMPLETED", conclusion="FAILURE")]
            ),
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
    fake_github.add_pr(head_sha=SHA_A).checks = [CheckInfo(name="ci", state="IN_PROGRESS")]
    eng = _in_ready(tmp_state_dir, fake_github)
    with pytest.raises(VerificationError, match="still running: ci.*READY_FOR_MERGE"):
        eng.step(allow_merge=True)
    assert eng.state.phase == Phase.READY_FOR_MERGE and eng.state.attempt == 1
    assert load_state(eng.paths.state_file).attempt == 1  # persisted across resume
    fake_github.prs[PR].checks = [CheckInfo(name="ci", state="COMPLETED", conclusion="SUCCESS")]
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
    fake_github.add_pr(head_sha=SHA_A).checks = [CheckInfo(name="ci", state="IN_PROGRESS")]
    eng = _in_ready(tmp_state_dir, fake_github)
    with pytest.raises(VerificationError, match="still running: ci"):
        eng.run(max_steps=50, allow_merge=True)
    assert eng.state.phase == Phase.READY_FOR_MERGE and eng.state.attempt == 1
    fake_github.prs[PR].checks = [CheckInfo(name="ci", state="COMPLETED", conclusion="SUCCESS")]
    outcomes = eng.run(max_steps=1, allow_merge=True)
    assert [o.next_phase for o in outcomes] == ["MERGE"] and eng.state.attempt == 0


def test_inconclusive_bound_of_one_blocks_immediately(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_A).checks = [CheckInfo(name="ci", state="QUEUED")]
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


_READ_FAILURES = [
    pytest.param(_break_pr_read, "could not be read", id="pr-read"),
    pytest.param(_break_queue_read, "merge-queue status", id="queue-read"),
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


def _epic_result(next_issue_url) -> str:
    return block({"phase": "UPDATE_EPIC", "status": "success", "next_issue_url": next_issue_url})


def _in_update_epic(tmp_state_dir, gh, script):
    """Engine parked in UPDATE_EPIC right after the controller merged PR for ISSUE."""
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
    assert [c for c in fake_github.calls if c[0] == "get_issue"] == []


def _assert_not_switched(eng, gh, url_queried: str | None):
    """A rejected selection changes nothing but the persisted rejection list."""
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.UPDATE_EPIC and s.current_issue_url == ISSUE
    assert s.current_pr_url == PR and s.review_round == 2  # bookkeeping not reset
    assert s.merged_since_epic_update == 1  # the EPIC batch is not closed yet
    assert s.attempt == 1  # the agent invocation is persisted for resume
    assert len(s.next_issue_rejections) == 1
    queried = [c[1] for c in gh.calls if c[0] == "get_issue"]
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


def test_update_epic_rejects_malformed_url_as_verification_error(tmp_state_dir, fake_github):
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result("not a url")])
    with pytest.raises(VerificationError, match="'not a url' is not a GitHub issue URL"):
        eng.step()
    _assert_not_switched(eng, fake_github, None)


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
        tmp_state_dir, fake_github, [_epic_result(ISSUE999), _epic_result(ISSUE999)]
    )
    with pytest.raises(VerificationError, match="selection 1/2"):
        eng.step()
    first_prompt = eng.provider.calls[0].prompt
    assert "Previous selection rejected by the controller: (none)" in first_prompt

    out = eng.step()  # resume: the agent is asked once more, with the reason
    assert len(eng.provider.calls) == 2
    retry_prompt = eng.provider.calls[1].prompt
    assert "issues/999 does not exist on GitHub" in retry_prompt
    assert out.next_phase == "BLOCKED"
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.BLOCKED and "2 time(s)" in s.block_reason
    assert "issues/999" in s.block_reason
    assert s.current_issue_url == ISSUE and s.merged_since_epic_update == 1
    assert len(s.next_issue_rejections) == 2


def test_update_epic_retry_with_a_valid_selection_switches(tmp_state_dir, fake_github):
    fake_github.add_issue(ISSUE3, "Next")
    eng = _in_update_epic(
        tmp_state_dir, fake_github, [_epic_result(FOREIGN_ISSUE), _epic_result(ISSUE3)]
    )
    with pytest.raises(VerificationError, match="selection 1/2"):
        eng.step()
    out = eng.step()
    assert out.next_phase == "ANALYZE_EXECUTE"
    s = load_state(eng.paths.state_file)
    assert s.current_issue_url == ISSUE3 and s.next_issue_rejections == []
    assert s.merged_since_epic_update == 0 and s.attempt == 0


def test_update_epic_retry_with_null_completes(tmp_state_dir, fake_github):
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(EPIC), _epic_result(None)])
    with pytest.raises(VerificationError, match="is the EPIC itself"):
        eng.step()
    assert eng.step().next_phase == "DONE"
    assert load_state(eng.paths.state_file).next_issue_rejections == []


def test_update_epic_transient_github_failure_is_a_rejection_not_a_switch(
    tmp_state_dir, fake_github
):
    """R1-F2: only a *transient* failure takes the bounded re-selection path."""
    fake_github.add_issue(ISSUE3, "Next")
    fake_github.get_issue_error = GitHubUnavailableError("gh: HTTP 502")
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(ISSUE3)])
    with pytest.raises(VerificationError, match="GitHub unavailable: gh: HTTP 502"):
        eng.step()
    _assert_not_switched(eng, fake_github, ISSUE3)


def test_update_epic_transient_github_failure_twice_is_blocked(tmp_state_dir, fake_github):
    fake_github.add_issue(ISSUE3, "Next")
    fake_github.get_issue_error = GitHubUnavailableError("gh: HTTP 502")
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(ISSUE3), _epic_result(ISSUE3)])
    with pytest.raises(VerificationError, match="selection 1/2"):
        eng.step()
    out = eng.step()
    assert out.next_phase == "BLOCKED" and len(eng.provider.calls) == 2
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.BLOCKED and "HTTP 502" in s.block_reason
    assert s.current_issue_url == ISSUE and s.merged_since_epic_update == 1


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
    fake_github.get_issue_error = error
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(ISSUE3), _epic_result(ISSUE3)])
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert len(eng.provider.calls) == 1  # never asked to select again
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.BLOCKED
    assert str(error) in s.block_reason and "not a transient GitHub failure" in s.block_reason
    assert s.next_issue_rejections == []  # not a rejection of the selection
    assert s.current_issue_url == ISSUE and s.current_pr_url == PR
    assert s.merged_since_epic_update == 1  # the EPIC batch is not closed
    with pytest.raises(StateTransitionError):
        eng.step()  # BLOCKED is terminal for `step`; nothing else runs


# -- loop bounds: review-round cap, stagnation, step budget (#9) ------------------------------
def _sha(n: int) -> str:
    return f"{n:040x}"


def _loop_agent(gh: FakeGitHub, findings_for_round, seen: list[str] | None = None):
    """Scripted ANALYZE_EXECUTE / REVIEW / FIX loop over FakeGitHub.

    ``findings_for_round(n)`` returns the findings review round ``n`` reports
    (``[]`` == clean). Every FIX pushes a new distinct HEAD and resolves every
    open finding as ``fixed`` — the runaway loop from the issue's evidence.
    """
    rounds = {"n": 0}
    seen = seen if seen is not None else []

    def agent(req):
        seen.append(req.phase)
        if req.phase == "ANALYZE_EXECUTE":
            gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
            return block(ANALYZE_OK)
        if req.phase == "REVIEW":
            rounds["n"] += 1
            rnd = rounds["n"]
            sha = gh.prs[PR].head_sha
            findings = findings_for_round(rnd)
            ids = [f["id"] for f in findings]
            gh.add_comment(PR, 100 + rnd, review_comment_body(rnd, sha, bool(findings), ids))
            return block(review_payload(rnd, sha, findings, cid=100 + rnd))
        if req.phase == "FIX":
            prev = gh.prs[PR].head_sha
            ids = re.findall(r"\*\*(R\d+-F\d+)\*\*", req.prompt)
            new = _sha(rounds["n"])
            gh.set_head(new)
            return block(
                fix_payload(prev, new, [{"finding_id": i, "resolution": "fixed"} for i in ids])
            )
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
    eng = make_engine(tmp_state_dir, _loop_agent(gh, _one_finding_per_round, seen), github=gh)
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
    assert s.last_review_result == "needs_fix" and s.reviewed_head_sha == _sha(2)
    assert [r["result"] for r in s.review_history] == ["needs_fix"] * 3
    assert gh.prs[PR].state == "OPEN" and gh.merges == []


def test_review_round_cap_does_not_block_a_clean_last_round(tmp_state_dir):
    gh = FakeGitHub()
    eng = make_engine(
        tmp_state_dir,
        _loop_agent(gh, lambda rnd: _one_finding_per_round(rnd) if rnd < 2 else []),
        github=gh,
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
    """Two consecutive rounds requesting the same resolution (ids differ) -> BLOCKED."""
    gh = FakeGitHub()
    seen: list[str] = []
    agent = _loop_agent(gh, lambda rnd: _one_finding_per_round(rnd, "Add a regression test"), seen)
    eng = make_engine(tmp_state_dir, agent, github=gh)
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
    assert s.review_round == 2 and "identical resolutions" in s.block_reason
    assert "stagnation_identical_rounds=2" in s.block_reason
    assert s.review_history[0]["fingerprint"] == s.review_history[1]["fingerprint"]
    assert s.open_findings[0]["id"] == "R2-F1"


def test_review_stagnation_unchanged_count_blocks_a_ping_pong(tmp_state_dir):
    """The issue's ping-pong (apply X / revert X / apply X): one finding per round, the
    same demand keeps coming back -> BLOCKED after 3 rounds."""
    gh = FakeGitHub()
    texts = {1: "apply refactor X", 2: "revert refactor X", 3: "Apply refactor X"}
    agent = _loop_agent(gh, lambda rnd: _one_finding_per_round(rnd, texts[rnd]))
    eng = make_engine(tmp_state_dir, agent, github=gh)
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
    eng = make_engine(tmp_state_dir, _loop_agent(gh, _one_finding_per_round), github=gh)
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

    eng = make_engine(tmp_state_dir, _loop_agent(gh, findings), github=gh)
    eng._save()
    outcomes = eng.run(max_steps=50)
    assert outcomes[-1].next_phase == "READY_FOR_MERGE"
    s = load_state(eng.paths.state_file)
    assert s.review_round == 4
    assert [r["finding_count"] for r in s.review_history] == [3, 2, 1, 0]


def test_review_stale_round_is_recorded_and_breaks_the_stagnation_streak(tmp_state_dir):
    gh = FakeGitHub()
    gh.add_comment(PR, 100, review_comment_body(2, SHA_A, True, ["R2-F1"]))
    same = _one_finding_per_round(2, "same text")

    def on_call(req):
        gh.set_head(SHA_B)  # someone pushed while the reviewer was working
        return block(review_payload(2, SHA_A, same))

    eng = _in_review(tmp_state_dir, gh, on_call, round_done=1)
    eng.state.review_history = [
        review_record(1, SHA_A, RESULT_NEEDS_FIX, _one_finding_per_round(1, "same text"))
    ]
    out = eng.step()
    assert out.next_phase == "REVIEW" and "moved" in out.message  # not BLOCKED
    s = load_state(eng.paths.state_file)
    assert [r["result"] for r in s.review_history] == ["needs_fix", "stale"]
    assert s.review_history[1]["reviewed_head_sha"] == SHA_A and s.open_findings == []


def test_failed_review_invocation_consumes_neither_round_nor_history(tmp_state_dir):
    gh = FakeGitHub()
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(1, SHA_A, [_finding(1)]))])
    with pytest.raises(VerificationError, match="not found on PR"):  # no review comment
        eng.step()
    s = load_state(eng.paths.state_file)
    assert s.review_round == 0 and s.review_history == [] and s.phase == Phase.REVIEW


def test_new_pr_resets_review_history(tmp_state_dir, fake_github):
    def on_call(req):
        fake_github.add_pr(head_sha=SHA_A, branch=BRANCH)
        return block(ANALYZE_OK)

    eng = make_engine(tmp_state_dir, on_call, github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    eng.state.review_history = [review_record(1, SHA_B, RESULT_NEEDS_FIX, [_finding(1)])]
    eng.state.review_round = 1
    assert eng.step().next_phase == "REVIEW"
    assert eng.state.review_history == [] and eng.state.review_round == 0


def test_step_budget_is_cumulative_and_survives_resume(tmp_state_dir):
    """`resume` continues the persisted step_count; it never restarts the budget."""
    gh = FakeGitHub()
    seen: list[str] = []
    agent = _loop_agent(gh, _one_finding_per_round, seen)
    eng = make_engine(tmp_state_dir, agent, github=gh)
    eng.config.workflow.max_total_steps = 4
    _no_stagnation(eng.config)
    eng._save()
    first = eng.run(max_steps=2)
    assert [o.next_phase for o in first] == ["ANALYZE_EXECUTE", "REVIEW"]
    assert load_state(eng.paths.state_file).step_count == 2

    # a fresh engine, as `resume` builds it: state (and the budget) come from disk
    resumed = make_engine(tmp_state_dir, agent, github=gh, cfg=eng.config)
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
