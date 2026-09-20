"""The operator's explicit exit from BLOCKED (issue #5).

`unblock` is the controller's decision made from live GitHub state: it
re-enters a phase only through `validate_transition`, refuses (state
untouched) whenever the safe phase is not knowable, and records the operator
action in state and in the run log. No agent runs and no step is charged.
"""

import json

import pytest

from autoforge import engine as engine_module
from autoforge.errors import (
    ConfigurationError,
    GitHubError,
    GitHubUnavailableError,
    StateTransitionError,
)
from autoforge.state import load_state
from autoforge.transitions import UNBLOCK_TARGETS, Phase, WorkflowMode
from tests.conftest import (
    BRANCH,
    EPIC,
    ISSUE,
    MERGE_BASE,
    PR,
    SHA_A,
    SHA_B,
    comment_url,
    implementation_pr_body,
    make_engine,
    review_comment_body,
)

PR_B = "https://github.com/owner/repo/pull/43"
REASON = "operator inspected the PR; the flaky check was rerun and is green"
FINDING = {"id": "R1-F1", "classification": "bug", "required_resolution": "fix it"}


def _blocked(tmp_state_dir, gh, *, reason="stuck", pr_url: str = "", **fields):
    """An engine whose run is BLOCKED with ``fields`` describing where it was."""
    eng = make_engine(tmp_state_dir, [], github=gh)
    eng.state.phase = Phase.BLOCKED
    eng.state.block_reason = reason
    eng.state.step_count = 3
    eng.state.current_pr_url = pr_url
    for name, value in fields.items():
        setattr(eng.state, name, value)
    eng._save()
    return eng


REVIEW_CID = 100  # the comment id of the review `_reviewed` names


def _reviewed(head=SHA_A, result="clean", findings=None, round_=2):
    """State fields of a completed review of PR at ``head`` on main.

    The review comment named is ``REVIEW_CID``; a test that steps through
    the merge gate afterwards posts it on the fake (``_review_comment``),
    because the gate re-reads it (#94).
    """
    return dict(
        current_head_sha=head,
        current_base_ref="main",
        current_branch=BRANCH,
        reviewed_pr_url=PR,
        reviewed_head_sha=head,
        reviewed_base_ref="main",
        reviewed_merge_base_sha=MERGE_BASE,
        last_review_result=result,
        last_review_comment_url=comment_url(PR, REVIEW_CID),
        open_findings=list(findings or []),
        review_round=round_,
    )


def _review_comment(gh, head=SHA_A, needs_fix=False, round_=2):
    """The comment ``_reviewed`` names, on the fake PR."""
    gh.add_comment(PR, REVIEW_CID, review_comment_body(round_, head, needs_fix))


def _unblock_records(eng):
    """The run log's controller-only unblock records (request.json), oldest first."""
    run_dir = eng.paths.logs_dir / eng.state.run_id
    if not run_dir.exists():
        return []
    return [
        json.loads((d / "request.json").read_text())
        for d in sorted(run_dir.iterdir())
        if "blocked-unblock" in d.name
    ]


def _assert_untouched(eng, raw_before: str, gh_calls_before=None):
    """A refusal writes no state; the run stays BLOCKED exactly as it was."""
    assert eng.paths.state_file.read_text() == raw_before
    assert load_state(eng.paths.state_file).phase == Phase.BLOCKED
    assert load_state(eng.paths.state_file).unblock_history == []


# -- preconditions ---------------------------------------------------------------
def test_unblock_requires_a_blocked_run(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, [], github=fake_github)
    eng.state.phase = Phase.REVIEW
    eng._save()
    raw = eng.paths.state_file.read_text()
    with pytest.raises(StateTransitionError, match="in phase REVIEW, not BLOCKED"):
        eng.unblock(REASON)
    assert eng.paths.state_file.read_text() == raw
    assert fake_github.calls == [] and _unblock_records(eng) == []


@pytest.mark.parametrize("phase", [Phase.DONE, Phase.FAILED])
def test_unblock_refuses_the_other_terminal_phases(tmp_state_dir, fake_github, phase):
    eng = make_engine(tmp_state_dir, [], github=fake_github)
    eng.state.phase = phase
    eng._save()
    with pytest.raises(StateTransitionError, match=f"in phase {phase.value}, not BLOCKED"):
        eng.unblock(REASON)
    assert load_state(eng.paths.state_file).phase == phase


@pytest.mark.parametrize("reason", ["", "   \n"])
def test_unblock_requires_a_reason(tmp_state_dir, fake_github, reason):
    eng = _blocked(tmp_state_dir, fake_github)
    raw = eng.paths.state_file.read_text()
    with pytest.raises(ConfigurationError, match="non-empty --reason"):
        eng.unblock(reason)
    _assert_untouched(eng, raw)
    assert fake_github.calls == []


def test_unblock_bounds_the_reason_length(tmp_state_dir, fake_github):
    eng = _blocked(tmp_state_dir, fake_github)
    with pytest.raises(ConfigurationError, match="at most"):
        eng.unblock("x" * (engine_module.MAX_UNBLOCK_REASON_CHARS + 1))
    assert fake_github.calls == []


def test_unblock_is_not_available_to_local_runs(tmp_state_dir, fake_github):
    eng = _blocked(tmp_state_dir, fake_github)
    eng.state.mode = WorkflowMode.LOCAL
    with pytest.raises(StateTransitionError, match="GitHub runs only"):
        eng.unblock(REASON)
    assert fake_github.calls == []


# -- no PR bound: ANALYZE_EXECUTE once its entry probe is known to succeed ---------
def test_unblock_without_a_pr_reenters_analyze_execute(tmp_state_dir, fake_github):
    """Two candidate PRs blocked ANALYZE_EXECUTE; the operator closed the stale
    one. The controller verifies the issue and the listing itself, records the
    action, clears the reason and re-enters the phase; `resume` then adopts."""
    fake_github.add_pr(head_sha=SHA_A, body=implementation_pr_body())
    fake_github.add_pr(PR_B, head_sha=SHA_B, branch="other", state="CLOSED")
    eng = _blocked(tmp_state_dir, fake_github, reason="2 open PRs claim to implement issue #2")
    out = eng.unblock(REASON)
    assert out.unblocked and out.phase == "ANALYZE_EXECUTE" and not out.dry_run
    assert "BLOCKED -> ANALYZE_EXECUTE" in out.message and "pull/42" in out.message
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.ANALYZE_EXECUTE and s.block_reason == ""
    assert s.step_count == 3  # not a step: no agent ran
    assert s.attempt == 0
    (entry,) = s.unblock_history
    assert entry["reason"] == REASON and entry["phase"] == "ANALYZE_EXECUTE"
    assert entry["block_reason"] == "2 open PRs claim to implement issue #2"
    assert entry["at"].endswith("+00:00") and "pull/42" in entry["detail"]
    (record,) = _unblock_records(eng)
    assert record["phase"] == "BLOCKED-unblock" and record["profile"].startswith("(operator")
    assert record["metadata"]["applied"] is True
    assert record["metadata"]["target_phase"] == "ANALYZE_EXECUTE"
    assert record["metadata"]["operator_reason"] == REASON
    # The live inspection is the ANALYZE_EXECUTE probe: issue + strict listing.
    assert ("get_issue", ISSUE) in fake_github.calls
    assert ("list_open_prs", "owner/repo") in fake_github.calls
    # And the entry adopts the PR the unblock saw, without an agent.
    nxt = eng.step()
    assert nxt.next_phase == "REVIEW" and "recovered existing open PR" in nxt.message
    assert eng.provider.calls == []


def test_unblock_without_any_pr_lets_analyze_execute_launch(tmp_state_dir, fake_github):
    eng = _blocked(tmp_state_dir, fake_github, reason="agent reported blocked")
    out = eng.unblock(REASON)
    assert out.unblocked and out.phase == "ANALYZE_EXECUTE"
    assert "no open PR implements it" in out.message


def test_unblock_without_a_pr_refuses_while_the_probe_still_cannot_decide(
    tmp_state_dir, fake_github
):
    """Still two open candidates: the controller never guesses. Nothing is
    written to state; the refusal is in the run log."""
    fake_github.add_pr(head_sha=SHA_A, body=implementation_pr_body())
    fake_github.add_pr(PR_B, head_sha=SHA_B, branch="other", body=implementation_pr_body())
    eng = _blocked(tmp_state_dir, fake_github, reason="2 open PRs")
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON)
    assert not out.unblocked and out.phase == "BLOCKED"
    assert out.message.startswith("stays BLOCKED: ") and "never guesses" in out.message
    _assert_untouched(eng, raw)
    (record,) = _unblock_records(eng)
    assert record["metadata"]["applied"] is False and record["metadata"]["target_phase"] == ""
    assert (eng.paths.logs_dir / eng.state.run_id / "001-blocked-unblock-1" / "error.txt").exists()


def test_unblock_without_a_pr_refuses_a_listing_that_cannot_be_completed(
    tmp_state_dir, fake_github
):
    fake_github.open_pr_listing_incomplete = True
    eng = _blocked(tmp_state_dir, fake_github)
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON)
    assert not out.unblocked and "cannot establish whether an open PR" in out.message
    _assert_untouched(eng, raw)


def test_unblock_without_a_pr_refuses_when_the_issue_cannot_be_read_conclusively(
    tmp_state_dir, fake_github
):
    """A conclusive GitHub failure of the issue read (R1-F3 of PR #101) is a
    decision -- the ANALYZE_EXECUTE entry probe is known to fail -- so it is
    a logged refusal with the audit record, as in the bound-PR path, not an
    error that escapes before the record is written."""
    fake_github.get_issue_error = GitHubError("HTTP 403: forbidden")
    eng = _blocked(tmp_state_dir, fake_github, reason="agent reported blocked")
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON)
    assert not out.unblocked and out.phase == "BLOCKED"
    assert out.message.startswith("stays BLOCKED: ")
    assert "cannot be verified" in out.message and "403" in out.message
    assert "not a transient GitHub failure" in out.message
    _assert_untouched(eng, raw)
    (record,) = _unblock_records(eng)
    assert record["metadata"]["applied"] is False and record["metadata"]["target_phase"] == ""
    assert record["metadata"]["operator_reason"] == REASON
    assert (eng.paths.logs_dir / eng.state.run_id / "001-blocked-unblock-1" / "error.txt").exists()
    assert ("list_open_prs", "owner/repo") not in fake_github.calls


def test_unblock_without_a_pr_propagates_a_transient_issue_read_failure(tmp_state_dir, fake_github):
    """A read that may succeed next time decides nothing: no refusal, no
    record, no state write -- the same rule as the bound-PR path."""
    fake_github.get_issue_error = GitHubUnavailableError("HTTP 502: bad gateway")
    eng = _blocked(tmp_state_dir, fake_github)
    raw = eng.paths.state_file.read_text()
    with pytest.raises(GitHubUnavailableError):
        eng.unblock(REASON)
    _assert_untouched(eng, raw)
    assert _unblock_records(eng) == []


def test_unblock_without_a_pr_refuses_a_closed_issue(tmp_state_dir, fake_github):
    fake_github.add_issue(ISSUE, state="CLOSED")
    eng = _blocked(tmp_state_dir, fake_github)
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON)
    assert not out.unblocked and "issue cannot be worked on" in out.message
    assert "CLOSED" in out.message
    _assert_untouched(eng, raw)
    assert ("list_open_prs", "owner/repo") not in fake_github.calls


# -- an OPEN PR is bound -----------------------------------------------------------
def test_unblock_open_pr_at_clean_reviewed_revision_reenters_ready_for_merge(
    tmp_state_dir, fake_github
):
    """BLOCKED by a failing check after a clean review; the check was rerun.
    The clean review is still bound to this PR at this HEAD and base, so the
    holding state is re-entered and the merge gate re-verifies from there."""
    fake_github.add_pr(head_sha=SHA_A)
    _review_comment(fake_github)
    eng = _blocked(tmp_state_dir, fake_github, pr_url=PR, reason="check ci failed", **_reviewed())
    out = eng.unblock(REASON)
    assert out.unblocked and out.phase == "READY_FOR_MERGE"
    assert "clean-reviewed HEAD" in out.message
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.READY_FOR_MERGE and s.last_review_result == "clean"
    assert s.reviewed_head_sha == SHA_A and s.open_findings == [] and s.prior_findings == []
    # The gate still verifies on GitHub before MERGE: nothing was merged here.
    assert fake_github.merges == []
    eng.config.safety.allow_merge = True
    assert eng.step(allow_merge=True).next_phase == "MERGE"


def test_unblock_open_pr_at_reviewed_head_with_open_findings_reenters_fix(
    tmp_state_dir, fake_github
):
    """BLOCKED at the review-round cap with findings; the operator raised the
    cap. The findings are still those of the PR's actual revision, so FIX is
    re-entered with them open (its entry re-reads the HEAD anyway)."""
    fake_github.add_pr(head_sha=SHA_A)
    eng = _blocked(
        tmp_state_dir,
        fake_github,
        pr_url=PR,
        reason="cap reached",
        **_reviewed(result="needs_fix", findings=[FINDING]),
    )
    eng.config.workflow.max_review_rounds = 5
    out = eng.unblock(REASON)
    assert out.unblocked and out.phase == "FIX" and "1 open finding(s)" in out.message
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.FIX and s.open_findings == [FINDING] and s.prior_findings == []
    assert s.last_review_result == "needs_fix" and s.review_round == 2


def test_unblock_refuses_fix_while_the_next_review_round_is_still_capped(
    tmp_state_dir, fake_github
):
    """The operator did not raise the cap: a FIX round now could never be
    reviewed, and re-entering only to block again is not an unblock."""
    fake_github.add_pr(head_sha=SHA_A)
    eng = _blocked(
        tmp_state_dir,
        fake_github,
        pr_url=PR,
        **_reviewed(result="needs_fix", findings=[FINDING], round_=2),
    )
    eng.config.workflow.max_review_rounds = 2
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON)
    assert not out.unblocked and "max_review_rounds" in out.message
    assert "could never be reviewed" in out.message
    _assert_untouched(eng, raw)


def test_unblock_open_pr_whose_head_moved_reenters_review_and_carries_findings(
    tmp_state_dir, fake_github
):
    """A human pushed to the PR while it was BLOCKED with findings: the
    review of the new HEAD is the only safe continuation, and the findings
    of the old one are handed to it to re-check rather than dropped."""
    fake_github.add_pr(head_sha=SHA_B)
    eng = _blocked(
        tmp_state_dir,
        fake_github,
        pr_url=PR,
        **_reviewed(head=SHA_A, result="needs_fix", findings=[FINDING]),
    )
    out = eng.unblock(REASON)
    assert out.unblocked and out.phase == "REVIEW"
    assert "revision moved" in out.message and "carried to that review" in out.message
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.REVIEW and s.current_head_sha == SHA_B and s.current_base_ref == "main"
    assert s.open_findings == [] and s.prior_findings == [FINDING]
    assert s.last_review_result == "stale"
    # The review binding itself is never rewritten by the operator path.
    assert s.reviewed_head_sha == SHA_A and s.reviewed_pr_url == PR


def test_unblock_open_pr_whose_base_changed_reenters_review(tmp_state_dir, fake_github):
    """The clean review was of the PR against 'main'; retargeted, it is
    stale like any other drift (R1-F1 of PR #101): a clean verdict must not
    stay recorded as current while the actual revision is reviewed."""
    fake_github.add_pr(head_sha=SHA_A, base_ref="release/1.x")
    eng = _blocked(tmp_state_dir, fake_github, pr_url=PR, **_reviewed())
    out = eng.unblock(REASON)
    assert out.unblocked and out.phase == "REVIEW" and "'release/1.x'" in out.message
    assert "carried to that review" not in out.message
    s = load_state(eng.paths.state_file)
    assert s.current_base_ref == "release/1.x" and s.last_review_result == "stale"
    assert s.prior_findings == [] and s.open_findings == []  # a clean review carries nothing


def test_unblock_after_a_stale_round_preserves_the_carried_findings(tmp_state_dir, fake_github):
    """Round 2 at A reported a finding, the post-review read found HEAD B
    (REVIEW -> REVIEW, the finding carried to `prior_findings`, round
    consumed), and the next REVIEW entry blocked on the round cap. The
    operator raised the cap and unblocks. No reviewer has examined the carry
    in between (an unblock is not a review round), so it is not replaced by
    the empty `open_findings` (R3-F1 of PR #101): the next REVIEW entry must
    still render it, or the #14 failure mode the carry exists to prevent is
    back."""
    carried = {"id": "R2-F1", "classification": "bug", "required_resolution": "older"}
    fake_github.add_pr(head_sha=SHA_B)
    eng = _blocked(
        tmp_state_dir,
        fake_github,
        pr_url=PR,
        reason="review round 3 would exceed max_review_rounds=2",
        **_reviewed(head=SHA_A, result="stale", findings=[]),
        prior_findings=[carried],
    )
    eng.config.workflow.max_review_rounds = 3
    out = eng.unblock(REASON)
    assert out.unblocked and out.phase == "REVIEW" and "revision moved" in out.message
    assert "1 finding(s) already carried from the stale round 2 stay carried" in out.message
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.REVIEW and s.current_head_sha == SHA_B
    assert s.last_review_result == "stale"
    assert s.prior_findings == [carried] and s.open_findings == []
    assert s.reviewed_head_sha == SHA_A and s.reviewed_pr_url == PR


def test_unblock_stale_clean_review_carries_nothing(tmp_state_dir, fake_github):
    """A clean review of A with no earlier carry (a completed round clears
    one), PR now at B: the verdict is stale and there is nothing to carry."""
    fake_github.add_pr(head_sha=SHA_B)
    eng = _blocked(tmp_state_dir, fake_github, pr_url=PR, **_reviewed(head=SHA_A))
    out = eng.unblock(REASON)
    assert out.unblocked and out.phase == "REVIEW" and "revision moved" in out.message
    assert "carried" not in out.message
    s = load_state(eng.paths.state_file)
    assert s.last_review_result == "stale"
    assert s.prior_findings == [] and s.open_findings == []
    assert s.reviewed_head_sha == SHA_A and s.reviewed_pr_url == PR


def test_unblock_head_drift_with_findings_replaces_an_earlier_carry(tmp_state_dir, fake_github):
    """Open findings of the reviewed revision replace an earlier carry, as
    HEAD drift does (a completed round clears the carry, so this shape is
    defensive); the preserve rule applies only when there is nothing open."""
    earlier = {"id": "R1-F1", "classification": "bug", "required_resolution": "older"}
    fake_github.add_pr(head_sha=SHA_B)
    eng = _blocked(
        tmp_state_dir,
        fake_github,
        pr_url=PR,
        **_reviewed(head=SHA_A, result="needs_fix", findings=[FINDING]),
        prior_findings=[earlier],
    )
    out = eng.unblock(REASON)
    assert out.unblocked and "1 open finding(s) of round 2 are carried" in out.message
    s = load_state(eng.paths.state_file)
    assert s.last_review_result == "stale"
    assert s.prior_findings == [FINDING] and s.open_findings == []


@pytest.mark.parametrize(
    "live_state, live_head",
    [("OPEN", SHA_A), ("OPEN", SHA_B), ("MERGED", SHA_A)],
)
def test_unblock_refuses_a_review_bound_to_another_pr(
    tmp_state_dir, fake_github, live_state, live_head
):
    """The review binding names PR 43 but the run is on PR 42 (R1-F2 of PR
    #101). Whatever PR 42's live state, the persisted verdict and findings
    are about another PR: they are neither a clean review to merge on nor
    findings to carry into PR 42's review. Refused, state untouched."""
    fake_github.add_pr(head_sha=live_head, state=live_state)
    fields = _reviewed(head=SHA_A, result="needs_fix", findings=[FINDING]) | {
        "reviewed_pr_url": PR_B
    }
    eng = _blocked(tmp_state_dir, fake_github, pr_url=PR, **fields)
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON)
    assert not out.unblocked and out.phase == "BLOCKED"
    assert "review bound in state is of PR " + PR_B in out.message
    assert "not of the current PR " + PR in out.message
    assert "another PR" in out.message
    _assert_untouched(eng, raw)
    s = load_state(eng.paths.state_file)
    assert s.open_findings == [FINDING] and s.prior_findings == []
    assert s.last_review_result == "needs_fix" and s.reviewed_pr_url == PR_B
    (record,) = _unblock_records(eng)
    assert record["metadata"]["applied"] is False and record["metadata"]["target_phase"] == ""


def test_unblock_refuses_a_clean_review_of_another_pr_at_the_same_head(tmp_state_dir, fake_github):
    """Same branch, same HEAD, proposed as another PR: the clean review is
    bound to PR 43 and is not moved to PR 42 (the merge gate refuses the
    same state). Not READY_FOR_MERGE and not REVIEW either."""
    fake_github.add_pr(head_sha=SHA_A)
    fields = _reviewed() | {"reviewed_pr_url": PR_B}
    eng = _blocked(tmp_state_dir, fake_github, pr_url=PR, **fields)
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON)
    assert not out.unblocked and "review bound in state is of PR " + PR_B in out.message
    _assert_untouched(eng, raw)


def test_unblock_open_pr_without_a_review_reenters_review(tmp_state_dir, fake_github):
    """BLOCKED at the REVIEW entry (two review comments for the round); the
    operator deleted one. REVIEW re-enters and its probe decides again."""
    fake_github.add_pr(head_sha=SHA_A)
    eng = _blocked(tmp_state_dir, fake_github, pr_url=PR, reason="2 review comments")
    out = eng.unblock(REASON)
    assert out.unblocked and out.phase == "REVIEW" and "no completed review" in out.message
    s = load_state(eng.paths.state_file)
    assert s.current_head_sha == SHA_A and s.current_branch == BRANCH and s.review_round == 0
    # A PR never reviewed has no result to mark stale.
    assert s.last_review_result == "" and s.prior_findings == [] and s.open_findings == []


def test_unblock_refuses_review_past_the_round_cap(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_B)
    eng = _blocked(tmp_state_dir, fake_github, pr_url=PR, **_reviewed(round_=3))
    eng.config.workflow.max_review_rounds = 3
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON)
    assert not out.unblocked and "max_review_rounds" in out.message
    _assert_untouched(eng, raw)


def test_unblock_refuses_a_closed_pr(tmp_state_dir, fake_github):
    """Reopen or reimplement is a decision the controller will not make."""
    fake_github.add_pr(head_sha=SHA_A, state="CLOSED")
    eng = _blocked(tmp_state_dir, fake_github, pr_url=PR, **_reviewed())
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON)
    assert not out.unblocked and "is CLOSED" in out.message and "Reopen the PR" in out.message
    _assert_untouched(eng, raw)


def test_unblock_refuses_a_pr_that_cannot_be_read_conclusively(tmp_state_dir, fake_github):
    fake_github.get_pr_error = GitHubError("HTTP 403: forbidden")
    eng = _blocked(tmp_state_dir, fake_github, pr_url=PR)
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON)
    assert not out.unblocked and "cannot be read" in out.message and "403" in out.message
    _assert_untouched(eng, raw)


def test_unblock_propagates_a_transient_github_failure_and_writes_nothing(
    tmp_state_dir, fake_github
):
    """A read that may succeed next time decides nothing: not even a refusal
    is recorded, because there was no decision."""
    fake_github.get_pr_failures = 1
    eng = _blocked(tmp_state_dir, fake_github, pr_url=PR)
    raw = eng.paths.state_file.read_text()
    with pytest.raises(GitHubUnavailableError):
        eng.unblock(REASON)
    _assert_untouched(eng, raw)
    assert _unblock_records(eng) == []


def test_unblock_refuses_a_pr_answered_with_another_pr(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_A)
    fake_github.prs[PR].url = PR_B
    eng = _blocked(tmp_state_dir, fake_github, pr_url=PR)
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON)
    assert not out.unblocked and "another PR" in out.message
    _assert_untouched(eng, raw)


# -- a MERGED PR is bound ------------------------------------------------------------
def test_unblock_merged_and_counted_pr_reenters_update_epic(tmp_state_dir, fake_github):
    """BLOCKED in UPDATE_EPIC (two progress comments); the operator removed
    one. The merge is done and counted, so UPDATE_EPIC is what is left."""
    fake_github.add_pr(head_sha=SHA_A, state="MERGED")
    eng = _blocked(
        tmp_state_dir,
        fake_github,
        pr_url=PR,
        counted_merged_prs=[PR],
        merged_since_epic_update=1,
        **_reviewed(),
    )
    out = eng.unblock(REASON)
    assert out.unblocked and out.phase == "UPDATE_EPIC" and "already counted" in out.message
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.UPDATE_EPIC and s.counted_merged_prs == [PR]
    assert s.merged_since_epic_update == 1  # nothing counted twice


def test_unblock_merged_uncounted_pr_at_reviewed_revision_reenters_ready_for_merge(
    tmp_state_dir, fake_github
):
    """A human merged the PR by hand after the controller blocked: at the
    clean-reviewed HEAD into the reviewed base, so the merge machinery may
    reconcile and count it once (with the gate open), exactly as it does for
    a PR merged while holding."""
    fake_github.add_pr(head_sha=SHA_A, state="MERGED")
    eng = _blocked(tmp_state_dir, fake_github, pr_url=PR, **_reviewed())
    out = eng.unblock(REASON)
    assert out.unblocked and out.phase == "READY_FOR_MERGE" and "not yet counted" in out.message
    assert load_state(eng.paths.state_file).counted_merged_prs == []
    eng.config.safety.allow_merge = True
    assert eng.step(allow_merge=True).next_phase == "MERGE"
    nxt = eng.step(allow_merge=True)
    assert nxt.next_phase == "UPDATE_EPIC" and "recovered" in nxt.message
    assert fake_github.merges == [] and eng.state.counted_merged_prs == [PR]


@pytest.mark.parametrize(
    ("fields", "expect"),
    [
        (_reviewed(head=SHA_B), "at HEAD"),
        (dict(_reviewed(), reviewed_base_ref="release/1.x"), "into 'main'"),
        (_reviewed(result="needs_fix", findings=[FINDING]), "no clean review"),
        ({}, "no clean review"),
    ],
    ids=["other-head", "other-base", "review-had-findings", "no-review"],
)
def test_unblock_refuses_a_merge_no_review_decided_on(tmp_state_dir, fake_github, fields, expect):
    fake_github.add_pr(head_sha=SHA_A, state="MERGED")
    eng = _blocked(tmp_state_dir, fake_github, pr_url=PR, **fields)
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON)
    assert not out.unblocked and expect in out.message
    assert "will not count a merge no review decided on" in out.message
    _assert_untouched(eng, raw)


# -- bounds and journals refuse before GitHub is asked -------------------------------
def test_unblock_refuses_while_the_step_budget_is_exhausted(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_A)
    eng = _blocked(tmp_state_dir, fake_github, pr_url=PR, step_count=7, **_reviewed())
    eng.config.workflow.max_total_steps = 7
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON)
    assert not out.unblocked and "max_total_steps" in out.message
    _assert_untouched(eng, raw)
    assert fake_github.calls == []
    # Raising the budget is exactly what the refusal asked for.
    eng.config.workflow.max_total_steps = 50
    assert eng.unblock(REASON).phase == "READY_FOR_MERGE"


def test_unblock_refuses_a_run_with_a_replan_journal(tmp_state_dir, fake_github):
    """REVIEW is REPLAN_REEXECUTE's only entry; the operator path never
    replays a replan transaction, in flight or rejected."""
    fake_github.add_pr(head_sha=SHA_A)
    eng = _blocked(
        tmp_state_dir,
        fake_github,
        pr_url=PR,
        replan_transaction={"stage": "rejected", "issue_url": ISSUE, "decision_pr_url": PR},
        **_reviewed(),
    )
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON)
    assert not out.unblocked and "REPLAN_REEXECUTE journal" in out.message
    assert "'rejected'" in out.message
    _assert_untouched(eng, raw)
    assert fake_github.calls == []


# -- the transition is the topology's, not the command's ------------------------------
def test_unblock_goes_through_validate_transition(tmp_state_dir, fake_github, monkeypatch):
    """The chosen phase is applied only if `validate_transition(BLOCKED, it)`
    passes; an edge the topology refuses is never coerced into state, and
    the audit trail never claims an applied unblock into it either."""
    fake_github.add_pr(head_sha=SHA_A)
    eng = _blocked(tmp_state_dir, fake_github, pr_url=PR, **_reviewed())
    seen = []

    def refusing(frm, to, mode=WorkflowMode.REMOTE):
        seen.append((frm, to))
        raise StateTransitionError(f"illegal transition {frm.value} -> {to.value}")

    monkeypatch.setattr(engine_module, "validate_transition", refusing)
    raw = eng.paths.state_file.read_text()
    with pytest.raises(StateTransitionError, match="illegal transition BLOCKED -> READY"):
        eng.unblock(REASON)
    assert seen == [(Phase.BLOCKED, Phase.READY_FOR_MERGE)]
    assert eng.paths.state_file.read_text() == raw
    # The check runs before the run-log write: no record claims `applied`.
    assert [r for r in _unblock_records(eng) if r["metadata"]["applied"]] == []
    assert not eng.paths.logs_dir.exists()


def test_every_unblock_target_is_a_legal_operator_edge():
    """The decision table only ever names phases the topology lets BLOCKED
    reach; this pins the two together."""
    decided = {
        Phase.ANALYZE_EXECUTE,
        Phase.REVIEW,
        Phase.FIX,
        Phase.READY_FOR_MERGE,
        Phase.UPDATE_EPIC,
    }
    assert decided == UNBLOCK_TARGETS


# -- dry-run and redaction ---------------------------------------------------------------
def test_unblock_dry_run_reads_github_and_writes_nothing(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_B)
    eng = _blocked(
        tmp_state_dir, fake_github, pr_url=PR, **_reviewed(result="needs_fix", findings=[FINDING])
    )
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON, dry_run=True)
    assert out.dry_run and not out.unblocked and out.phase == "BLOCKED"
    assert out.message.startswith("[dry-run] would re-enter REVIEW")
    assert ("get_pr", PR) in fake_github.calls
    _assert_untouched(eng, raw)
    assert _unblock_records(eng) == []
    assert not eng.paths.logs_dir.exists()
    # A refusal previews the same way.
    fake_github.prs[PR].state = "CLOSED"
    out = eng.unblock(REASON, dry_run=True)
    assert out.message.startswith("[dry-run] would stay BLOCKED") and "CLOSED" in out.message
    _assert_untouched(eng, raw)


def test_unblock_redacts_the_operator_text_before_persisting(tmp_state_dir, fake_github):
    secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    fake_github.add_pr(head_sha=SHA_A)
    eng = _blocked(tmp_state_dir, fake_github, pr_url=PR, reason=f"token {secret}", **_reviewed())
    out = eng.unblock(f"reran with GITHUB_TOKEN={secret}")
    assert out.unblocked
    assert secret not in eng.paths.state_file.read_text()
    (entry,) = load_state(eng.paths.state_file).unblock_history
    assert "***REDACTED***" in entry["reason"] and "***REDACTED***" in entry["block_reason"]
    run_dir = eng.paths.logs_dir / eng.state.run_id
    for path in run_dir.rglob("*"):
        if path.is_file():
            assert secret not in path.read_text()


def test_unblock_twice_records_both_actions(tmp_state_dir, fake_github):
    fake_github.add_pr(head_sha=SHA_A)
    eng = _blocked(tmp_state_dir, fake_github, pr_url=PR, **_reviewed())
    assert eng.unblock("first").unblocked
    eng.state.phase = Phase.BLOCKED
    eng.state.block_reason = "blocked again"
    eng._save()
    assert eng.unblock("second").unblocked
    history = load_state(eng.paths.state_file).unblock_history
    assert [h["reason"] for h in history] == ["first", "second"]
    assert history[1]["block_reason"] == "blocked again"
    assert len(_unblock_records(eng)) == 2


def test_unblock_survives_an_issue_switch(tmp_state_dir, fake_github):
    """The trail is the run's, not the PR's: reset_for_new_issue keeps it."""
    eng = _blocked(tmp_state_dir, fake_github)
    assert eng.unblock(REASON).unblocked
    eng.state.reset_for_new_issue("https://github.com/owner/repo/issues/3")
    assert len(eng.state.unblock_history) == 1
    assert eng.state.epic_url == EPIC
