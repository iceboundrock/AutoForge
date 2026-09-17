"""Engine: verification of agent claims, SHA binding, routing, recovery, gate."""

import json
import os
import re
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from autoforge.errors import (
    ControlResultValidationError,
    ExecutionError,
    ExecutionTimeoutError,
    GitHubError,
    GitHubUnavailableError,
    StateError,
    StateTransitionError,
    VerificationError,
)
from autoforge.executor import ExecutionResult
from autoforge.github import (
    ChangedFile,
    CheckInfo,
    GitHubClient,
    MergeQueueStatus,
    WorkflowJob,
    WorkflowRunJobs,
)
from autoforge.loop_guard import RESULT_NEEDS_FIX, review_record
from autoforge.providers import AgentExecutionResult, ScriptedProvider
from autoforge.result_parser import (
    MAX_FINDING_ID_CHARS,
    MAX_FINDING_LOCATION_CHARS,
    MAX_FINDING_RESOLUTION_CHARS,
    MAX_FINDING_TITLE_CHARS,
    MAX_FINDINGS_PER_REVIEW,
    MAX_FIX_RATIONALE_CHARS,
    MAX_URL_CHARS,
)
from autoforge.state import load_state
from autoforge.transitions import Phase
from tests.conftest import (
    BASE_RUN_ID,
    BRANCH,
    CI_RUN_ID,
    CI_WORKFLOW_ID,
    EPIC,
    ISSUE,
    ISSUE3,
    MAIN_SHA,
    PR,
    SHA_A,
    SHA_B,
    SHA_C,
    FakeGitHub,
    block,
    ci_check,
    ci_jobs,
    comment_url,
    follow_up_issue_body,
    git_repo,
    make_engine,
    post_progress_comment,
    progress_comment_body,
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


def test_analyze_issue_claim_is_compared_by_identity_not_url_string(tmp_state_dir, fake_github):
    """Issue #37 N1: `Owner/REPO` names the run's repository as GitHub sees it."""
    payload = dict(ANALYZE_OK, issue_url="https://github.com/Owner/REPO/issues/2")
    eng = make_engine(tmp_state_dir, [block(payload)], github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    eng.provider._handler = lambda req: (
        fake_github.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2]),
        block(payload),
    )[1]
    assert eng.step().next_phase == "REVIEW"
    assert eng.state.current_issue_url == ISSUE  # the run's own spelling is kept


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


def test_review_comment_on_a_case_variant_of_the_pr_belongs_to_it(tmp_state_dir):
    """Issue #37 N1: the comment's parent PR is compared by identity, not URL text."""
    gh = FakeGitHub()
    gh.add_comment(PR, 100, review_comment_body(1, SHA_A, False))
    payload = review_payload(1, SHA_A, [])
    payload["review_comment_url"] = comment_url("https://github.com/Owner/REPO/pull/42", 100)
    eng = _in_review(tmp_state_dir, gh, [block(payload)])
    assert eng.step().next_phase == "READY_FOR_MERGE"


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


def test_review_post_agent_journal_refusal_keeps_the_phase_for_resume(tmp_state_dir):
    """#55 in REMOTE mode: the journal append that records the invocation is
    refused after the reviewer returned, in the same window as a timeout, a
    non-zero exit or a verification failure. The phase is left unchanged with
    no round consumed, the attempt the launch was charged as is persisted,
    the invocation's artifacts are published, the oversized journal is neither
    materialised nor carried forward, and the refusal names the outcome it
    interrupted and what `resume` will do. A resume in a new process re-enters
    REVIEW, finds the round-1 comment the dead reviewer posted at this HEAD,
    and hands it to the reviewer to adopt: the round ends with one comment."""
    from autoforge.runlog import MAX_EVENT_JOURNAL_BYTES

    gh = FakeGitHub()
    eng = _in_review(tmp_state_dir, gh, None)
    journal = Path(eng.paths.logs_dir) / eng.state.run_id / "events.jsonl"

    def reviews_then_enlarges_the_journal(req):
        gh.add_comment(PR, 100, review_comment_body(1, SHA_A, True, ["R1-F1"]))
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
        r"re-enters REVIEW and hands a review comment already posted",
    ):
        eng.step()
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.REVIEW and s.review_round == 0 and s.attempt == 1
    assert s.open_findings == [] and s.last_review_comment_url == ""
    assert journal.stat().st_size == 2 * MAX_EVENT_JOURNAL_BYTES, "not carried forward"
    steps = sorted(p.name for p in journal.parent.iterdir() if p.is_dir())
    assert steps == ["001-review-1"]
    assert (journal.parent / steps[0] / "control-result.json").exists()
    assert len(gh.comments[PR]) == 1
    eng.close()

    # The operator repairs the journal and resumes in a new process.
    os.truncate(journal, 0)
    eng2 = make_engine(tmp_state_dir, None, github=gh)
    eng2.load()

    def adopts_the_existing_comment(req):
        assert comment_url(PR, 100) in req.prompt, "the existing comment was not handed over"
        return block(review_payload(1, SHA_A, [_finding(1)]))

    eng2.provider._handler = adopts_the_existing_comment
    assert eng2.step().next_phase == "FIX"
    assert len(eng2.provider.calls) == 1
    s = load_state(eng2.paths.state_file)
    assert s.review_round == 1 and s.attempt == 0
    assert s.last_review_comment_url == comment_url(PR, 100)
    assert [f["id"] for f in s.open_findings] == ["R1-F1"]
    assert len(gh.comments[PR]) == 1, "the round has exactly one comment"


# -- REVIEW entry: reconciliation with the PR before the reviewer runs (PR #89 F1) ---------
def test_review_entry_hands_an_existing_round_comment_to_the_reviewer(tmp_state_dir):
    """A comment carrying the marker for the upcoming round at the bound HEAD
    already exists (a reviewer whose result was never recorded). The
    controller reads the PR first and names it in the prompt; the reviewer
    adopts it instead of posting a second one."""
    gh = FakeGitHub()
    gh.add_comment(PR, 100, review_comment_body(1, SHA_A, False))
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(1, SHA_A, []))])
    assert eng.step().next_phase == "READY_FOR_MERGE"
    prompt = eng.provider.calls[0].prompt
    assert (
        f"Comment already posted for THIS round at THIS HEAD (if any):\n  {comment_url(PR, 100)}"
        in prompt
    )
    assert "THIS HEAD (if any):\n  (none)" not in prompt
    assert len(gh.comments[PR]) == 1


def test_review_entry_ignores_a_comment_for_the_round_at_another_head(tmp_state_dir):
    """The marker binds a comment to (round, HEAD). A round-1 comment at SHA_B
    when the round is bound to SHA_A is not this round's comment."""
    gh = FakeGitHub()
    gh.add_comment(PR, 90, review_comment_body(1, SHA_B, True, ["R1-F1"]))

    def reviews(req):
        gh.add_comment(PR, 100, review_comment_body(1, SHA_A, False))
        return block(review_payload(1, SHA_A, []))

    eng = _in_review(tmp_state_dir, gh, reviews)
    assert eng.step().next_phase == "READY_FOR_MERGE"
    prompt = eng.provider.calls[0].prompt
    assert "Comment already posted for THIS round at THIS HEAD (if any):\n  (none)" in prompt
    assert comment_url(PR, 90) not in prompt


def test_review_entry_blocks_on_two_comments_for_the_round_without_invoking(tmp_state_dir):
    """Two comments claim the same (round, HEAD): the controller cannot know
    which review is the round's and never chooses. BLOCKED, nobody launched,
    and the reason names both so the operator can remove one."""
    gh = FakeGitHub()
    gh.add_comment(PR, 100, review_comment_body(1, SHA_A, True, ["R1-F1"]))
    gh.add_comment(PR, 101, review_comment_body(1, SHA_A, False))
    eng = _in_review(tmp_state_dir, gh, ["never"])
    out = eng.step()
    assert out.next_phase == "BLOCKED" and eng.provider.calls == []
    reason = load_state(eng.paths.state_file).block_reason
    assert "2 review comments for round 1" in reason
    assert comment_url(PR, 100) in reason and comment_url(PR, 101) in reason
    assert "exactly one remains" in reason


def test_reviewer_posting_a_second_comment_for_the_round_is_rejected_then_blocked(
    tmp_state_dir,
):
    """The reviewer ignores the existing comment and posts another: the round
    is rejected after the fact (the uniqueness rule is enforced on read-back,
    not trusted to the prompt), no round is consumed, and the next entry
    blocks on the two comments instead of launching a third reviewer."""
    gh = FakeGitHub()
    gh.add_comment(PR, 100, review_comment_body(1, SHA_A, True, ["R1-F1"]))

    def posts_again(req):
        gh.add_comment(PR, 101, review_comment_body(1, SHA_A, True, ["R1-F1"]))
        return block(review_payload(1, SHA_A, [_finding(1)], cid=101))

    eng = _in_review(tmp_state_dir, gh, posts_again)
    with pytest.raises(
        VerificationError, match=r"2 review comments for round 1.*exactly one review comment"
    ):
        eng.step()
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.REVIEW and s.review_round == 0 and s.open_findings == []
    assert eng.step().next_phase == "BLOCKED"
    assert len(eng.provider.calls) == 1


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
]


@pytest.mark.parametrize("marker_json", _MALFORMED_MARKERS)
def test_parse_review_marker_rejects_malformed_markers(marker_json):
    """PR #89 review F4: one strict validator; `true == 1` and `1.0 == 1` in
    Python must not make a malformed marker match a round."""
    from autoforge.engine import parse_review_marker

    with pytest.raises(ValueError):
        parse_review_marker(marker_json)


def test_parse_review_marker_accepts_a_well_formed_marker():
    from autoforge.engine import parse_review_marker

    m = parse_review_marker(
        json.dumps({"round": 2, "reviewed_head_sha": SHA_A.upper(), "needs_fix_round": True})
    )
    assert (m.round, m.reviewed_head_sha, m.needs_fix_round) == (2, SHA_A, True)


@pytest.mark.parametrize("marker_json", _MALFORMED_MARKERS)
def test_review_verification_rejects_a_malformed_marker(tmp_state_dir, marker_json):
    gh = FakeGitHub()
    gh.add_comment(PR, 100, _review_body_with_marker(marker_json))
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(1, SHA_A, []))])
    # A payload that is not even a JSON object never matches the marker shape.
    with pytest.raises(VerificationError, match="marker is unusable|lacks the .* marker"):
        eng.step()
    assert eng.state.phase == Phase.REVIEW and eng.state.review_round == 0


@pytest.mark.parametrize("marker_json", _MALFORMED_MARKERS)
def test_review_entry_does_not_adopt_a_malformed_marker(tmp_state_dir, marker_json):
    """The entry scan and the read-back use the same validator: a comment the
    read-back would reject is not handed to the reviewer as the round's."""
    gh = FakeGitHub()
    gh.add_comment(PR, 90, _review_body_with_marker(marker_json))

    def reviews(req):
        assert "THIS HEAD (if any):\n  (none)" in req.prompt
        gh.add_comment(PR, 100, review_comment_body(1, SHA_A, False))
        return block(review_payload(1, SHA_A, []))

    eng = _in_review(tmp_state_dir, gh, reviews)
    assert eng.step().next_phase == "READY_FOR_MERGE"


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


def test_fix_post_agent_journal_refusal_keeps_the_phase_for_resume(tmp_state_dir):
    """#55 in REMOTE mode, FIX: the fixer pushed, then the journal append was
    refused. The phase, the bound HEAD and the open findings are unchanged
    and the launch is persisted as attempt 1; the refusal names the accepted
    result it interrupted and that `resume` re-enters FIX and routes a pushed
    HEAD back to REVIEW. The resume then finds HEAD past the reviewed one and
    schedules the review of the actual HEAD without launching a fixer."""
    from autoforge.runlog import MAX_EVENT_JOURNAL_BYTES

    gh = FakeGitHub()

    def fixes_then_enlarges_the_journal(req):
        gh.set_head(SHA_B)
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.touch()
        os.truncate(journal, 2 * MAX_EVENT_JOURNAL_BYTES)
        return block(fix_payload(SHA_A, SHA_B, [{"finding_id": "R1-F1", "resolution": "fixed"}]))

    eng = _in_fix(tmp_state_dir, gh, fixes_then_enlarges_the_journal)
    journal = Path(eng.paths.logs_dir) / eng.state.run_id / "events.jsonl"
    with pytest.raises(
        StateError,
        match=r"corrupted event journal.*interrupted attempt 1 of FIX after the agent had "
        r"returned with: a CONTROL_RESULT the controller accepted.*then 'resume': it "
        r"re-enters FIX and routes a HEAD already pushed past the reviewed one back to REVIEW",
    ):
        eng.step()
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.FIX and s.review_round == 1 and s.attempt == 1
    assert s.current_head_sha == SHA_A and s.reviewed_head_sha == SHA_A
    assert [f["id"] for f in s.open_findings] == ["R1-F1"] and s.last_fix_resolutions == []
    assert journal.stat().st_size == 2 * MAX_EVENT_JOURNAL_BYTES, "not carried forward"
    assert sorted(p.name for p in journal.parent.iterdir() if p.is_dir()) == ["001-fix-1"]
    eng.close()

    os.truncate(journal, 0)
    eng2 = make_engine(tmp_state_dir, ["never"], github=gh)
    eng2.load()
    out = eng2.step()
    assert out.next_phase == "REVIEW" and "no fixer launched" in out.message
    assert eng2.provider.calls == []
    s = load_state(eng2.paths.state_file)
    assert s.phase == Phase.REVIEW and s.current_head_sha == SHA_B
    assert s.open_findings == [] and s.last_review_result == "stale" and s.attempt == 0


def test_fix_entry_with_head_past_the_reviewed_one_goes_to_review_without_a_fixer(
    tmp_state_dir,
):
    """PR #89 F1, FIX side: the HEAD the findings are bound to is no longer
    the PR HEAD when FIX is entered (an unrecorded fix, an operator push).
    The general HEAD-binding rule applies: the review is stale, the actual
    HEAD gets reviewed, and no fixer is launched against findings of a
    commit that is no longer the PR."""
    gh = FakeGitHub()
    eng = _in_fix(tmp_state_dir, gh, ["never"])
    gh.set_head(SHA_B)
    out = eng.step()
    assert out.next_phase == "REVIEW" and eng.provider.calls == []
    assert "past the reviewed HEAD" in out.message and "no fixer launched" in out.message
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.REVIEW and s.review_round == 1
    assert s.current_head_sha == SHA_B and s.reviewed_head_sha == SHA_A
    assert s.open_findings == [] and s.last_fix_resolutions == []
    assert s.last_review_result == "stale" and s.attempt == 0


def test_fix_entry_binds_the_unchanged_head_and_launches_the_fixer(tmp_state_dir):
    """HEAD still equals the reviewed HEAD at FIX entry: the fixer runs."""
    gh = FakeGitHub()

    def on_call(req):
        gh.set_head(SHA_B)
        return block(fix_payload(SHA_A, SHA_B, [{"finding_id": "R1-F1", "resolution": "fixed"}]))

    eng = _in_fix(tmp_state_dir, gh, on_call)
    assert eng.step().next_phase == "REVIEW"
    assert len(eng.provider.calls) == 1
    assert ("get_pr", PR) in gh.calls


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

    def creates_follow_up(req):
        gh.add_issue(ISSUE3, "follow-up", body=follow_up_issue_body("R1-F1"))
        return block(fix_payload(SHA_A, SHA_A, _follow_up("R1-F1", ISSUE3)))

    eng = _in_fix(tmp_state_dir, gh, creates_follow_up)
    assert eng.step().next_phase == "REVIEW"  # no commit needed for a pure follow-up
    assert ("get_issue", ISSUE3) in gh.calls
    assert ("list_open_issues", "owner/repo", True) in gh.calls


def _follow_up(finding_id: str, url: str) -> list[dict]:
    return [
        {"finding_id": finding_id, "resolution": "follow_up_created", "follow_up_issue_url": url}
    ]


# -- FIX entry and read-back: follow-up issues (PR #89 review, F3) -------------------------
def test_fix_entry_hands_an_existing_follow_up_issue_to_the_fixer(tmp_state_dir):
    """A fixer whose result was never recorded created the follow-up issue and
    pushed nothing, so the HEAD is unchanged and a HEAD probe sees nothing.
    The open issue carrying the (PR, finding) marker is what records that
    write; the entry finds it, names it in the prompt, and the fixer reports
    it instead of creating a second one."""
    gh = FakeGitHub()
    gh.add_issue(ISSUE3, "follow-up", body=follow_up_issue_body("R1-F1"))
    from autoforge.engine import render_follow_up_marker

    def adopts(req):
        marker = render_follow_up_marker(PR, "R1-F1")
        assert f"- R1-F1: marker `{marker}`; existing issue: {ISSUE3}" in req.prompt
        return block(fix_payload(SHA_A, SHA_A, _follow_up("R1-F1", ISSUE3)))

    eng = _in_fix(tmp_state_dir, gh, adopts)
    assert eng.step().next_phase == "REVIEW"
    assert len(eng.provider.calls) == 1 and len(gh.issues) == 1 + 2  # EPIC, ISSUE, ISSUE3


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
    assert "2 open issues carry the follow-up marker for finding R1-F1" in reason
    assert ISSUE3 in reason and other in reason and "exactly one remains" in reason


def test_fix_entry_blocks_when_the_open_issue_listing_cannot_be_proven_complete(
    tmp_state_dir,
):
    """ "No follow-up issue exists" is a claim about every open issue; a listing
    that may be truncated cannot make it, and a fixer launched on it could
    create a second issue. BLOCKED, nobody launched, HEAD not bound."""
    gh = FakeGitHub()
    gh.issue_listing_truncated = True
    eng = _in_fix(tmp_state_dir, gh, ["never"])
    out = eng.step()
    assert out.next_phase == "BLOCKED" and eng.provider.calls == []
    reason = load_state(eng.paths.state_file).block_reason
    assert "cannot establish which follow-up issues already exist" in reason
    assert "may be truncated" in reason


def test_fix_entry_ignores_a_closed_issue_carrying_the_marker(tmp_state_dir):
    """A closed follow-up is not the finding's open follow-up: the fixer is
    told none exists and may create one during its run."""
    gh = FakeGitHub()
    gh.add_issue(ISSUE3, "closed follow-up", state="CLOSED", body=follow_up_issue_body("R1-F1"))
    new = "https://github.com/owner/repo/issues/5"

    def creates(req):
        assert "; existing issue: (none)" in req.prompt and ISSUE3 not in req.prompt
        gh.add_issue(new, "follow-up", body=follow_up_issue_body("R1-F1"))
        return block(fix_payload(SHA_A, SHA_A, _follow_up("R1-F1", new)))

    eng = _in_fix(tmp_state_dir, gh, creates)
    assert eng.step().next_phase == "REVIEW"


@pytest.mark.parametrize(
    "body",
    [
        "",
        follow_up_issue_body("R1-F2"),
        follow_up_issue_body("R1-F1", "https://github.com/owner/repo/pull/41"),
    ],
    ids=["no-marker", "another-finding", "another-pr"],
)
def test_fix_follow_up_issue_not_carrying_the_findings_marker_is_rejected(tmp_state_dir, body):
    """An existing open issue is not a follow-up of this finding unless it
    carries the (PR, finding) marker; the marker is what a later entry finds,
    so an unmarked follow-up would be recreated on the next relaunch."""
    gh = FakeGitHub()
    gh.add_issue(ISSUE3, "some issue", body=body)
    eng = _in_fix(
        tmp_state_dir, gh, [block(fix_payload(SHA_A, SHA_A, _follow_up("R1-F1", ISSUE3)))]
    )
    with pytest.raises(VerificationError, match="is not the open issue carrying the marker"):
        eng.step()
    assert eng.state.phase == Phase.FIX


def test_fix_follow_up_claiming_another_issue_than_the_marked_one_is_rejected(tmp_state_dir):
    gh = FakeGitHub()
    other = "https://github.com/owner/repo/issues/4"
    gh.add_issue(ISSUE3, "the marked one", body=follow_up_issue_body("R1-F1"))
    gh.add_issue(other, "unmarked")
    eng = _in_fix(tmp_state_dir, gh, [block(fix_payload(SHA_A, SHA_A, _follow_up("R1-F1", other)))])
    with pytest.raises(VerificationError, match=rf"carrying the marker .*\(that is {ISSUE3}\)"):
        eng.step()


def test_fix_resolving_a_finding_otherwise_while_its_follow_up_issue_is_open_is_rejected(
    tmp_state_dir,
):
    """The marked open issue is the durable record of the finding's
    disposition; a result that contradicts it is not adopted, and state never
    records a resolution GitHub does not carry."""
    gh = FakeGitHub()
    gh.add_issue(ISSUE3, "follow-up", body=follow_up_issue_body("R1-F1"))

    def fixes_instead(req):
        gh.set_head(SHA_B)
        return block(fix_payload(SHA_A, SHA_B, [{"finding_id": "R1-F1", "resolution": "fixed"}]))

    eng = _in_fix(tmp_state_dir, gh, fixes_instead)
    with pytest.raises(VerificationError, match="resolved as fixed but open issue .* carries"):
        eng.step()
    assert eng.state.phase == Phase.FIX and eng.state.last_fix_resolutions == []


def test_fix_creating_a_second_follow_up_issue_is_rejected_then_blocked(tmp_state_dir):
    """The fixer ignores the existing issue and creates another: the result is
    rejected on read-back (the rule is not trusted to the prompt), and the
    next entry blocks on the pair instead of launching a third fixer."""
    gh = FakeGitHub()
    other = "https://github.com/owner/repo/issues/4"
    gh.add_issue(ISSUE3, "follow-up", body=follow_up_issue_body("R1-F1"))

    def creates_again(req):
        gh.add_issue(other, "follow-up again", body=follow_up_issue_body("R1-F1"))
        return block(fix_payload(SHA_A, SHA_A, _follow_up("R1-F1", other)))

    eng = _in_fix(tmp_state_dir, gh, creates_again)
    with pytest.raises(VerificationError, match="2 open issues carry the follow-up marker"):
        eng.step()
    assert eng.step().next_phase == "BLOCKED"
    assert len(eng.provider.calls) == 1


def test_fix_follow_up_issue_missing_rejected(tmp_state_dir):
    gh = FakeGitHub()
    res = [
        {"finding_id": "R1-F1", "resolution": "follow_up_created", "follow_up_issue_url": ISSUE3}
    ]
    eng = _in_fix(tmp_state_dir, gh, [block(fix_payload(SHA_A, SHA_A, res))])
    with pytest.raises(VerificationError, match="does not exist"):
        eng.step()


def test_fix_follow_up_on_a_case_variant_of_the_current_issue_is_self_reference(tmp_state_dir):
    """Issue #37 N1: `Owner/REPO/issues/2` *is* the current issue; it must not slip
    past the self-reference check by spelling."""
    gh = FakeGitHub()
    variant = "https://github.com/Owner/REPO/issues/2"
    gh.add_issue(variant, "the same issue")
    res = [
        {"finding_id": "R1-F1", "resolution": "follow_up_created", "follow_up_issue_url": variant}
    ]
    eng = _in_fix(tmp_state_dir, gh, [block(fix_payload(SHA_A, SHA_A, res))])
    with pytest.raises(VerificationError, match="points at the current issue itself"):
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


# -- redaction at the persistence boundary (#81) -----------------------------------
# REVIEW findings and FIX resolutions are agent-authored text that lands in
# plain `state.json` and in `status --json`; the engine redacts them before
# they are assigned to state, mirroring the LOCAL-mode test in test_local.py.
def test_remote_review_findings_are_redacted_before_they_are_persisted(tmp_state_dir):
    secret = "sk-ant-" + "B" * 30
    gh = FakeGitHub()
    gh.add_comment(PR, 100, review_comment_body(1, SHA_A, True, ["R1-F1"]))
    leaky = _finding(1)
    leaky["required_resolution"] = f"Set ANTHROPIC_API_KEY={secret} in the test fixture."
    eng = _in_review(tmp_state_dir, gh, [block(review_payload(1, SHA_A, [leaky]))])
    out = eng.step()
    assert out.next_phase == "FIX"
    assert secret not in json.dumps(eng.state.open_findings)
    assert "***REDACTED***" in eng.state.open_findings[0]["required_resolution"]
    assert secret not in eng.paths.state_file.read_text(encoding="utf-8")


def test_remote_fix_resolutions_are_redacted_before_they_are_persisted(tmp_state_dir):
    secret = "sk-ant-" + "B" * 30
    gh = FakeGitHub()
    res = [
        {"finding_id": "R1-F1", "resolution": "fixed"},
        {
            "finding_id": "R1-F2",
            "resolution": "no_change_with_rationale",
            "rationale": f"The fixture already sets ANTHROPIC_API_KEY={secret}, so nothing to do.",
        },
    ]

    def on_call(req):
        gh.set_head(SHA_B)
        return block(fix_payload(SHA_A, SHA_B, res))

    eng = _in_fix(tmp_state_dir, gh, on_call, findings=[_finding(1, 1), _finding(1, 2)])
    out = eng.step()
    assert out.next_phase == "REVIEW"
    assert secret not in json.dumps(eng.state.last_fix_resolutions)
    assert "***REDACTED***" in eng.state.last_fix_resolutions[1]["rationale"]
    assert secret not in eng.paths.state_file.read_text(encoding="utf-8")


# -- correction retry ---------------------------------------------------------------------
def test_malformed_result_after_the_pr_was_created_is_recovered_not_corrected(
    tmp_state_dir, fake_github
):
    """PR #89 review F1: a correction relaunch is a re-entry like any other,
    so the phase's GitHub reconciliation runs before it. The agent created
    the PR and then lost its result block: the PR is adopted and no
    correction is launched."""

    def agent(req):
        assert not req.correction
        fake_github.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])  # PR was created…
        return "no block here\n"  # …but the result block was lost

    eng = make_engine(tmp_state_dir, agent, github=fake_github)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    out = eng.step()
    assert out.next_phase == "REVIEW" and "recovered" in out.message
    assert len(eng.provider.calls) == 1
    s = load_state(eng.paths.state_file)
    assert s.current_pr_url == PR and s.current_head_sha == SHA_A and s.attempt == 0


def test_malformed_result_triggers_one_correction(tmp_state_dir, fake_github):
    def agent(req):
        if not req.correction:
            return "no block here\n"  # nothing was done, and no result block
        fake_github.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
        return block(ANALYZE_OK)  # correction run does the work and reports it

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


def test_correction_after_the_review_comment_was_posted_adopts_it(tmp_state_dir):
    """The reviewer posted the round's comment, then returned junk. The
    correction relaunch is preceded by the REVIEW entry probe: the comment is
    handed to the corrected reviewer, which adopts it. One comment remains."""
    gh = FakeGitHub()

    def reviews(req):
        if not req.correction:
            gh.add_comment(PR, 100, review_comment_body(1, SHA_A, False))
            return "junk\n"
        assert f"THIS HEAD (if any):\n  {comment_url(PR, 100)}" in req.prompt
        return block(review_payload(1, SHA_A, []))

    eng = _in_review(tmp_state_dir, gh, reviews)
    assert eng.step().next_phase == "READY_FOR_MERGE"
    assert len(eng.provider.calls) == 2 and len(gh.comments[PR]) == 1


def test_correction_after_the_fix_was_pushed_goes_to_review_without_relaunching(tmp_state_dir):
    """The fixer pushed, then returned junk. Before the correction relaunch
    the FIX entry probe sees the HEAD past the reviewed one and routes to
    REVIEW of the actual HEAD; a fixer is never relaunched against findings
    its push may have resolved."""
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


def test_correction_after_the_progress_comment_was_posted_adopts_it(tmp_state_dir, fake_github):
    """UPDATE_EPIC: the agent posted the progress comment, then returned junk.
    The correction is told about the comment and does not post again."""

    def agent(req):
        if not req.correction:
            post_progress_comment(fake_github)
            return "junk\n"
        assert f"for this issue (if any):\n  {comment_url(EPIC, 300)}" in req.prompt
        return _epic_result(None)

    eng = _in_update_epic(tmp_state_dir, fake_github, agent)
    assert eng.step().next_phase == "DONE"
    assert len(eng.provider.calls) == 2 and len(fake_github.comments[EPIC]) == 1


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
        ("timeout", r"timed out after \d+s"),
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
        gh.add_comment(PR, 100, review_comment_body(1, SHA_A, False))
        return block(review_payload(1, SHA_A, []))

    eng = _in_review(tmp_state_dir, gh, reads_state)
    assert eng.step().next_phase == "READY_FOR_MERGE"
    assert seen == [1] and load_state(eng.paths.state_file).attempt == 0


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
    eng.config.execution.default_timeout_seconds = 1
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED" and "timed out after 1s" in eng.state.block_reason
    assert fake_github.merges == []


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
    """No local commit and no `origin`: bounded re-check, the code is never guessed at."""
    git_repo(tmp_state_dir.parent)
    fake_github.add_pr(head_sha=SHA_A)
    eng = _in_merge(tmp_state_dir, fake_github)
    eng.config.merge.verification_commands = [["true"]]
    with pytest.raises(VerificationError, match="not in the local repository") as info:
        eng.step(allow_merge=True)
    assert "refs/pull/42/head" in str(info.value)
    assert eng.state.phase == Phase.MERGE and eng.state.attempt == 1
    assert fake_github.merges == []


def test_reviewed_head_is_fetched_from_the_pull_ref_when_not_local(tmp_state_dir, fake_github):
    origin = git_repo(tmp_state_dir.parent / "origin")
    base = _commit(origin, "base.txt", "base\n")
    sha = _commit(origin, "proof.txt", "v1\n")
    _git(origin, "update-ref", "refs/pull/42/head", sha)
    _git(origin, "reset", "-q", "--hard", base)
    local = git_repo(tmp_state_dir.parent)
    _git(local, "remote", "add", "origin", str(origin))
    _git(local, "fetch", "-q", "origin", "HEAD")
    marker = tmp_state_dir.parent / "cwd.txt"
    fake_github.add_pr(head_sha=sha)
    eng = _in_merge(tmp_state_dir, fake_github, reviewed=sha)
    eng.config.merge.verification_commands = [_record_cwd(marker)]
    assert eng.step(allow_merge=True).next_phase == "UPDATE_EPIC"
    assert len(marker.read_text().splitlines()) == 1
    assert "refs/pull" not in _git(local, "for-each-ref", "--format=%(refname)")


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


_READ_FAILURES = [
    pytest.param(_break_pr_read, "could not be read", id="pr-read"),
    pytest.param(_break_files_read, "changed-file listing", id="files-read"),
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


def _in_update_epic(tmp_state_dir, gh, script, posts_progress: bool = True):
    """Engine parked in UPDATE_EPIC right after the controller merged PR for ISSUE.

    A list of scripted results stands for an agent that posts the progress
    comment once (an invocation asked again adopts the one it posted) and
    returns the results in turn; ``posts_progress=False`` is an agent that
    skipped the phase's write.
    """
    if isinstance(script, list):
        queue = list(script)

        def scripted(req):
            if posts_progress:
                post_progress_comment(gh)
            return queue.pop(0)

        script = scripted
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


# -- UPDATE_EPIC entry and read-back: the progress comment (PR #89 review, F2) -------------
def test_update_epic_entry_hands_an_existing_progress_comment_to_the_agent(
    tmp_state_dir, fake_github
):
    """An interrupted UPDATE_EPIC already posted the progress comment. The
    entry reads the EPIC, names the comment, and the agent adopts it."""
    fake_github.add_comment(EPIC, 300, progress_comment_body())

    def adopts(req):
        assert f"for this issue (if any):\n  {comment_url(EPIC, 300)}" in req.prompt
        return _epic_result(None)

    eng = _in_update_epic(tmp_state_dir, fake_github, adopts)
    assert eng.step().next_phase == "DONE"
    assert len(fake_github.comments[EPIC]) == 1
    assert ("get_issue_comments", EPIC) in fake_github.calls


def test_update_epic_entry_ignores_a_progress_comment_for_another_pr(tmp_state_dir, fake_github):
    """The marker binds the comment to (issue, PR): an earlier issue's or PR's
    progress comment on the same EPIC is not this entry's."""
    other_pr = "https://github.com/owner/repo/pull/41"
    fake_github.add_comment(EPIC, 299, progress_comment_body(ISSUE, other_pr))
    fake_github.add_comment(EPIC, 298, progress_comment_body(ISSUE3, PR))

    def posts(req):
        assert "for this issue (if any):\n  (none)" in req.prompt
        post_progress_comment(fake_github)
        return _epic_result(None)

    eng = _in_update_epic(tmp_state_dir, fake_github, posts)
    assert eng.step().next_phase == "DONE"
    assert len(fake_github.comments[EPIC]) == 3


def test_update_epic_entry_blocks_on_two_progress_comments_without_invoking(
    tmp_state_dir, fake_github
):
    fake_github.add_comment(EPIC, 300, progress_comment_body())
    fake_github.add_comment(EPIC, 301, progress_comment_body())
    eng = _in_update_epic(tmp_state_dir, fake_github, ["never"])
    out = eng.step()
    assert out.next_phase == "BLOCKED" and eng.provider.calls == []
    s = load_state(eng.paths.state_file)
    assert "carries 2 progress comments for issue" in s.block_reason
    assert comment_url(EPIC, 300) in s.block_reason and comment_url(EPIC, 301) in s.block_reason
    assert s.current_issue_url == ISSUE and s.merged_since_epic_update == 1


def test_update_epic_without_the_progress_comment_is_rejected(tmp_state_dir, fake_github):
    """The phase's write is read back, never inferred from the result: an
    agent that selected the next issue but posted nothing has not done the
    phase. No switch, no selection queried, the phase stays for `resume`."""
    fake_github.add_issue(ISSUE3, "Next")
    eng = _in_update_epic(tmp_state_dir, fake_github, [_epic_result(ISSUE3)], posts_progress=False)
    with pytest.raises(VerificationError, match="carries no progress comment with the marker"):
        eng.step()
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.UPDATE_EPIC and s.current_issue_url == ISSUE and s.attempt == 1
    assert s.next_issue_rejections == []  # not a selection rejection
    assert [c for c in fake_github.calls if c[0] == "get_issue"] == []


def test_update_epic_posting_a_second_progress_comment_is_rejected_then_blocked(
    tmp_state_dir, fake_github
):
    fake_github.add_comment(EPIC, 300, progress_comment_body())

    def posts_again(req):
        fake_github.add_comment(EPIC, 301, progress_comment_body())
        return _epic_result(None)

    eng = _in_update_epic(tmp_state_dir, fake_github, posts_again)
    with pytest.raises(VerificationError, match="2 progress comments .*exactly one progress"):
        eng.step()
    assert load_state(eng.paths.state_file).phase == Phase.UPDATE_EPIC
    assert eng.step().next_phase == "BLOCKED"
    assert len(eng.provider.calls) == 1


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
    assert [c for c in fake_github.calls if c[0] == "get_issue"] == []
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
    # PR #89 F2: the re-selection is a re-entry; the progress comment the
    # first invocation posted is handed over, not posted again.
    assert f"for this issue (if any):\n  {comment_url(EPIC, 300)}" in retry_prompt
    assert len(fake_github.comments[EPIC]) == 1
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
            ids = re.findall(r"^- (R\d+-F\d+) \[", req.prompt, re.M)
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


# -- REVIEW payload bounds (#34) -------------------------------------------------------
def _oversized_review(rnd: int, sha: str, cid: int = 100) -> dict:
    findings = [_finding(rnd, n) for n in range(1, MAX_FINDINGS_PER_REVIEW + 2)]
    return review_payload(rnd, sha, findings, cid=cid)


def test_oversized_review_is_rejected_and_corrected(tmp_state_dir, fake_github):
    """Too many findings: the round is refused whole and the reviewer re-emits."""
    ids = [f"R1-F{n}" for n in range(1, MAX_FINDINGS_PER_REVIEW + 2)]

    def agent(req):
        if not req.correction:
            # Posted once; the correction re-emits the result for that
            # comment instead of posting a second one for the round.
            fake_github.add_comment(PR, 100, review_comment_body(1, SHA_A, True, ids[:1]))
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


def test_oversized_resolution_never_reaches_state_or_a_prompt(tmp_state_dir, fake_github):
    """One finding past the text bound is refused; state and its file are untouched."""
    huge = ("resolve everything " * 200).strip()  # far past MAX_FINDING_RESOLUTION_CHARS
    assert len(huge) > MAX_FINDING_RESOLUTION_CHARS
    fake_github.add_comment(PR, 100, review_comment_body(1, SHA_A, True, ["R1-F1"]))
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
    # The only differences on disk are the counters of the refused launch.
    persisted = json.loads(eng.paths.state_file.read_bytes())
    for volatile in ("updated_at",):
        persisted.pop(volatile)
    expected = json.loads(before) | {"attempt": 1, "step_count": 1}
    expected.pop("updated_at")
    assert persisted == expected
    assert "resolve everything" not in eng.paths.state_file.read_text()
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
        return block(
            fix_payload(
                SHA_A,
                SHA_A,
                [
                    {
                        "finding_id": "R1-F1",
                        "resolution": "no_change_with_rationale",
                        "rationale": rationale,
                    }
                ],
            )
        )

    eng = _in_fix(tmp_state_dir, gh, agent)
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
    # The follow-up section renders one more line per finding: the id, its
    # marker (the PR URL and the id again, JSON-quoted) and the existing
    # issue URL or "(none)"; every part is bounded by the id and URL bounds.
    follow_up_line = 2 * MAX_FINDING_ID_CHARS + 2 * MAX_URL_CHARS + framing
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
    capture bound: ``head + marker + tail`` with the tail offset recorded."""

    def __init__(self, head: str, tail: str, on_call=None) -> None:
        super().__init__()
        self.marker = "\n[autoforge: 999 bytes of stdout omitted; ...]\n"
        self.head, self.tail = head, tail
        self.on_call = on_call

    def execute(self, req):
        self.calls.append(req)
        if self.on_call is not None:
            self.on_call()
        return AgentExecutionResult(
            command=["x"],
            exit_code=0,
            stdout=self.head + self.marker + self.tail,
            stderr="",
            started_at="t",
            finished_at="t",
            stdout_truncated=True,
            stdout_tail_offset=len(self.head) + len(self.marker),
        )


def _install(eng, provider):
    eng.providers._overrides = {"claude": provider, "opencode": provider}
    eng.provider = provider


def test_truncated_stdout_accepts_a_block_that_lies_in_the_tail(tmp_state_dir, fake_github):
    """The block is the last thing on stdout, so the kept tail preserves it;
    the whole (marked) capture is what reaches stdout.log."""
    eng = make_engine(tmp_state_dir, [], github=fake_github)
    provider = _TruncatedOutputProvider(
        "runaway logs " * 100,
        "last logs\n" + block(ANALYZE_OK),
        on_call=lambda: fake_github.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2]),
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


def test_truncated_stdout_never_accepts_a_block_from_the_head(tmp_state_dir, fake_github):
    """A block before the cut is stale (the agent wrote more after it) or
    spans the cut; either way it is not the agent's final result. The
    rejection names the truncation so the correction prompt carries it."""
    eng = make_engine(tmp_state_dir, [], github=fake_github)
    eng.config.execution.max_correction_attempts = 0
    provider = _TruncatedOutputProvider(block(ANALYZE_OK), "trailing logs only\n")
    _install(eng, provider)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises(ControlResultValidationError, match="capture bound") as exc:
        eng.step()
    assert "no CONTROL_RESULT block found" in str(exc.value)
    assert eng.state.phase == Phase.ANALYZE_EXECUTE and len(provider.calls) == 1
    run_dir = eng.paths.logs_dir / eng.state.run_id
    step = next(p for p in run_dir.iterdir() if p.is_dir())
    assert "capture bound" in (step / "error.txt").read_text(encoding="utf-8")
