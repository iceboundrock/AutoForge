"""Engine: verification of agent claims, SHA binding, routing, recovery, gate."""

import json

import pytest

from autoforge.errors import (
    ControlResultValidationError,
    ExecutionError,
    ExecutionTimeoutError,
    StateTransitionError,
    VerificationError,
)
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


# -- MERGE gate (future milestone, must stay closed) ----------------------------------------------
def test_merge_phase_requires_both_gates(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, [], github=fake_github)
    eng.state.phase = Phase.MERGE
    eng.state.current_pr_url = PR
    with pytest.raises(VerificationError, match="merge is disabled"):
        eng.step(allow_merge=True)
    eng.config.safety.allow_merge = True
    with pytest.raises(VerificationError, match="merge is disabled"):
        eng.step(allow_merge=False)


def test_merge_when_explicitly_enabled(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_A)
    payload = {
        "phase": "MERGE",
        "status": "success",
        "merged": True,
        "next_action": "NEXT_ISSUE",
        "next_issue_url": ISSUE3,
    }
    eng = make_engine(tmp_state_dir, [block(payload)], github=fake_github)
    eng.config.safety.allow_merge = True
    eng.state.phase = Phase.READY_FOR_MERGE
    eng.state.current_pr_url = PR
    eng.state.reviewed_head_sha = SHA_A
    eng.state.review_round = 2
    assert eng.step(allow_merge=True).next_phase == "MERGE"
    assert eng.step(allow_merge=True).next_phase == "ANALYZE_EXECUTE"
    assert eng.state.current_issue_url == ISSUE3 and eng.state.review_round == 0
    assert eng.state.counted_merged_prs == [PR]


def test_ready_for_merge_head_moved_goes_back_to_review(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_B)
    eng = make_engine(tmp_state_dir, [], github=fake_github)
    eng.config.safety.allow_merge = True
    eng.state.phase = Phase.READY_FOR_MERGE
    eng.state.current_pr_url = PR
    eng.state.reviewed_head_sha = SHA_A
    assert eng.step(allow_merge=True).next_phase == "REVIEW"


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
