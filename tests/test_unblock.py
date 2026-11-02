"""The operator's explicit exit from BLOCKED (issue #5).

`unblock` is the controller's decision made from live GitHub state: it
re-enters a phase only through `validate_transition`, refuses (state
untouched) whenever the safe phase is not knowable, and records the operator
action in state and in the run log. No agent runs and no step is charged.
"""

import json
import shlex
import subprocess
from pathlib import Path

import pytest

from autoforge import engine as engine_module
from autoforge.errors import (
    ConfigurationError,
    GitHubError,
    GitHubUnavailableError,
    StateTransitionError,
)
from autoforge.git_transport import NO_AUTO_MAINTENANCE
from autoforge.state import load_state
from autoforge.transitions import UNBLOCK_TARGETS, Phase
from tests.conftest import (
    BRANCH,
    EPIC,
    GIT_IDENT,
    ISSUE,
    MERGE_BASE,
    MERGE_BASE_B,
    PR,
    SHA_A,
    SHA_B,
    FakeGitHub,
    block,
    certify_review,
    comment_url,
    commit_in,
    git_out,
    implementation_pr_body,
    make_engine,
    publish_pr_head,
    read_context_of,
    review_comment_body,
    review_result,
    reviewed_head_in,
    scripted,
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
    certify_review(eng)
    out = eng.unblock(REASON)
    assert out.unblocked and out.phase == "READY_FOR_MERGE"
    assert "clean-reviewed HEAD" in out.message
    assert "review certificate of round 2 applies" in out.message
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
    certify_review(eng)
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
    certify_review(eng)
    seen = []

    def refusing(frm, to):
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


# -- unblock --rereview: a fresh round instead of FIX (#428) ---------------------------
FINDING_R2 = {
    "id": "R2-F1",
    "classification": "spec",
    "title": "stale PR description",
    "required_resolution": "rewrite the PR description to match the code",
}
RESOLVED_OUTSIDE = "replaced the PR description by hand; the fixer cannot edit it"


def _needs_fix(tmp_state_dir, gh, head=SHA_A, findings=(FINDING_R2,), **extra):
    """BLOCKED in FIX with the round-2 findings open, PR open at the reviewed ``head``."""
    gh.add_pr(head_sha=head)
    fields = _reviewed(head=head, result="needs_fix", findings=list(findings)) | extra
    return _blocked(tmp_state_dir, gh, pr_url=PR, reason="agent reported blocked", **fields)


def test_unblock_rereview_reenters_review_and_carries_the_findings_unresolved(
    tmp_state_dir, fake_github
):
    """The plain decision is FIX; --rereview gives that FIX up. The round's
    findings are not dropped and not marked resolved: they move to the
    carry the next round re-checks, the last result is stale, and the round
    counter is untouched (the new round is counted when it completes). The
    history entry, the run log and the message name what was abandoned."""
    eng = _needs_fix(
        tmp_state_dir, fake_github, findings=(FINDING_R2, dict(FINDING_R2, id="R2-F2"))
    )
    out = eng.unblock(RESOLVED_OUTSIDE, rereview=True)
    assert out.unblocked and out.phase == "REVIEW", out.message
    assert out.message.startswith("BLOCKED -> REVIEW: ")
    assert "R2-F1, R2-F2 are abandoned, not resolved" in out.message
    assert "REVIEW round 3 reviews the PR as it now stands" in out.message
    assert "the 2 open finding(s) of round 2 are carried to that review" in out.message
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.REVIEW and s.block_reason == "" and s.step_count == 3
    assert s.open_findings == [] and [f["id"] for f in s.prior_findings] == ["R2-F1", "R2-F2"]
    assert s.last_review_result == "stale" and s.review_round == 2
    # The review binding is the operator path's to read, never to rewrite.
    assert s.reviewed_head_sha == SHA_A and s.reviewed_pr_url == PR
    (entry,) = s.unblock_history
    assert entry["action"] == (
        f"rereview: abandoned R2-F1, R2-F2 (round 2 of {PR} at {SHA_A[:12]})"
    )
    assert entry["phase"] == "REVIEW" and entry["reason"] == RESOLVED_OUTSIDE
    assert engine_module.rereview_carry(s) == entry
    (record,) = _unblock_records(eng)
    assert record["metadata"]["applied"] is True
    assert record["metadata"]["action"] == entry["action"]
    assert record["metadata"]["abandoned_findings"] == ["R2-F1", "R2-F2"]
    assert eng.provider.calls == []


def test_a_plain_unblock_records_no_abandoned_findings(tmp_state_dir, fake_github):
    eng = _needs_fix(tmp_state_dir, fake_github)
    assert eng.unblock(REASON).phase == "FIX"
    (record,) = _unblock_records(eng)
    assert record["metadata"]["abandoned_findings"] == [] and record["metadata"]["action"] == ""


def test_the_review_prompt_says_the_operator_asked_for_the_rereview(tmp_state_dir, fake_github):
    """The next round's PRIOR_FINDINGS line tells a re-review's carry from a
    stale round's: same HEAD, FIX given up by the operator. The operator's
    reason is not in it: it stays uninterpreted."""
    eng = _needs_fix(tmp_state_dir, fake_github)
    assert eng.unblock(RESOLVED_OUTSIDE, rereview=True).unblocked
    eng.state = load_state(eng.paths.state_file)
    text = eng._format_prior_findings()
    assert text.startswith(f"Review round 2 at HEAD `{SHA_A}`")
    assert "the operator gave up that round's FIX at that HEAD" in text
    assert "`unblock --rereview`" in text and "R2-F1" in text
    assert "This round is bound to that same HEAD: no commit moved" in text
    assert RESOLVED_OUTSIDE not in text
    # PR #444 R1-F3: the round's own binding is compared with the HEAD the
    # FIX was given up at; whatever moved since is named, the provenance kept.
    eng.state.current_head_sha = SHA_B
    eng.state.current_merge_base_sha = MERGE_BASE_B
    moved = eng._format_prior_findings()
    assert moved.startswith(f"Review round 2 at HEAD `{SHA_A}`")
    assert "the operator gave up that round's FIX at that HEAD" in moved
    assert "The PR moved after that request" in moved
    assert f"bound to HEAD `{SHA_B}`, not `{SHA_A}`; merge base `{MERGE_BASE_B}`" in moved
    assert "same HEAD" not in moved
    # A stale round's carry keeps its own wording.
    eng.state.unblock_history = []
    assert "unblock --rereview" not in eng._format_prior_findings()
    assert "and no FIX round resolved them" in eng._format_prior_findings()


def test_rereview_carry_matches_only_its_own_findings_round_and_pr(tmp_state_dir, fake_github):
    """The history outlives a carry and an issue switch: an entry is the
    carry's source only for the same findings, round, PR and HEAD."""
    eng = _needs_fix(tmp_state_dir, fake_github)
    assert eng.unblock(RESOLVED_OUTSIDE, rereview=True).unblocked
    s = load_state(eng.paths.state_file)
    assert engine_module.rereview_carry(s) is not None
    # A later stale round replaces the carry under its own round.
    s.review_round, s.prior_findings = 3, [dict(FINDING_R2, id="R3-F1")]
    assert engine_module.rereview_carry(s) is None
    # A completed round clears it.
    s.prior_findings = []
    assert engine_module.rereview_carry(s) is None
    # Another PR with the same round and ids is not this entry's.
    s.review_round, s.prior_findings, s.reviewed_pr_url = 2, [FINDING_R2], PR_B
    assert engine_module.rereview_carry(s) is None


def test_unblock_rereview_after_the_review_round_blocks_again_keeps_the_carry(
    tmp_state_dir, fake_github
):
    """The new round did not complete (it blocked before its comment): a plain
    unblock re-enters REVIEW with the carry, and the prompt still says why."""
    eng = _needs_fix(tmp_state_dir, fake_github)
    assert eng.unblock(RESOLVED_OUTSIDE, rereview=True).unblocked
    eng.state = load_state(eng.paths.state_file)
    eng.state.phase = Phase.BLOCKED
    eng.state.block_reason = "validation failed three times"
    eng._save()
    out = eng.unblock(REASON)
    assert out.unblocked and out.phase == "REVIEW", out.message
    s = load_state(eng.paths.state_file)
    assert [f["id"] for f in s.prior_findings] == ["R2-F1"] and s.open_findings == []
    assert engine_module.rereview_carry(s) == s.unblock_history[0]
    # And a second --rereview has no FIX left to give up.
    eng.state = s
    eng.state.phase = Phase.BLOCKED
    eng.state.block_reason = "blocked again"
    eng._save()
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON, rereview=True)
    assert not out.unblocked and "no such FIX to give up" in out.message
    assert eng.paths.state_file.read_text() == raw


def test_unblock_rereview_is_refused_when_the_merge_base_moved(tmp_state_dir, fake_github):
    """PR #444 R1-F2: the same HEAD on the same base name, but the base was
    rewritten under it, so the diff round 2 reviewed is not the PR's. There
    is no FIX at an unchanged revision to give up: refused, nothing written.
    The plain unblock is the drift's recovery: its FIX entry finds the same
    drift and re-enters REVIEW of the actual revision with the findings
    carried, no fixer launched."""
    eng = _needs_fix(tmp_state_dir, fake_github)
    fake_github.merge_base = MERGE_BASE_B
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(RESOLVED_OUTSIDE, rereview=True)
    assert not out.unblocked
    assert (
        f"PR merge base moved to {MERGE_BASE_B[:12]} from the reviewed merge base "
        f"{MERGE_BASE[:12]} that the open findings of round 2 are bound to" in out.message
    )
    assert "Unblock without --rereview: FIX re-reads the merge base" in out.message
    # Read against the reviewed HEAD, as the FIX entry reads it.
    assert ("get_merge_base_sha", "owner/repo", "main", SHA_A) in fake_github.calls
    _assert_untouched(eng, raw)
    (record,) = _unblock_records(eng)
    assert record["metadata"]["applied"] is False
    assert record["metadata"]["abandoned_findings"] == []

    assert eng.unblock(REASON).phase == "FIX"
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert "PR merge base moved to" in out.message
    s = load_state(eng.paths.state_file)
    assert [f["id"] for f in s.prior_findings] == ["R2-F1"] and s.open_findings == []
    assert s.last_review_result == "stale" and engine_module.rereview_carry(s) is None
    assert eng.provider.calls == []


@pytest.mark.parametrize(
    ("extra", "error", "says"),
    [
        (
            {"reviewed_merge_base_sha": ""},
            None,
            "review round 2 recorded no merge base, so the PR's diff cannot be proven",
        ),
        (
            {},
            GitHubError("HTTP 422: No common ancestor"),
            "This is not a transient GitHub failure, so the PR's diff cannot be proven",
        ),
    ],
    ids=["none-recorded", "unreadable"],
)
def test_unblock_rereview_is_refused_on_a_merge_base_it_cannot_prove(
    tmp_state_dir, fake_github, extra, error, says
):
    """PR #444 R1-F2: a binding that cannot be proven unchanged is not taken
    as unchanged. Refused, nothing written."""
    eng = _needs_fix(tmp_state_dir, fake_github, **extra)
    fake_github.merge_base_error = error
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(RESOLVED_OUTSIDE, rereview=True)
    assert not out.unblocked and says in out.message, out.message
    _assert_untouched(eng, raw)


def test_unblock_rereview_propagates_an_unavailable_merge_base_read(tmp_state_dir, fake_github):
    """Nothing is decided on a read that may succeed next time: no state
    write and no run-log record."""
    eng = _needs_fix(tmp_state_dir, fake_github)
    fake_github.merge_base_error = GitHubUnavailableError("HTTP 502")
    raw = eng.paths.state_file.read_text()
    with pytest.raises(GitHubUnavailableError, match="merge base of 'main'"):
        eng.unblock(RESOLVED_OUTSIDE, rereview=True)
    assert eng.paths.state_file.read_text() == raw
    assert _unblock_records(eng) == []


def test_unblock_rereview_is_refused_past_the_round_cap(tmp_state_dir, fake_github):
    """The new round counts against workflow.max_review_rounds like any other."""
    eng = _needs_fix(tmp_state_dir, fake_github)
    eng.config.workflow.max_review_rounds = 2
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON, rereview=True)
    assert not out.unblocked and "workflow.max_review_rounds=2 is reached" in out.message
    assert "review round 3 is not started" in out.message
    assert "could never be reviewed" not in out.message
    _assert_untouched(eng, raw)
    (record,) = _unblock_records(eng)
    assert record["metadata"]["applied"] is False
    assert record["metadata"]["abandoned_findings"] == []


@pytest.mark.parametrize(
    ("live_head", "fields", "plain"),
    [
        (SHA_B, _reviewed(head=SHA_A, result="needs_fix", findings=[FINDING_R2]), "REVIEW"),
        (SHA_A, _reviewed(head=SHA_A, result="clean"), "REVIEW"),
        (SHA_A, _reviewed(head=SHA_A, result="needs_fix", findings=[]), "REVIEW"),
    ],
    ids=["head-moved", "clean", "no-open-findings"],
)
def test_unblock_rereview_applies_only_where_unblock_would_reenter_fix(
    tmp_state_dir, fake_github, live_head, fields, plain
):
    """Without a FIX to give up the flag is refused, quoting the plain
    decision, and nothing is written: a moved PR already gets a fresh round
    (with the carry), and a clean review has no findings to abandon."""
    fake_github.add_pr(head_sha=live_head)
    eng = _blocked(tmp_state_dir, fake_github, pr_url=PR, **fields)
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON, rereview=True)
    assert not out.unblocked and "unblock --rereview applies only" in out.message
    assert f"Without the flag: PR {PR} is OPEN" in out.message and plain in out.message
    _assert_untouched(eng, raw)


def test_unblock_rereview_without_a_pr_is_refused(tmp_state_dir, fake_github):
    eng = _blocked(tmp_state_dir, fake_github)
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON, rereview=True)
    assert not out.unblocked and "unblock --rereview applies only" in out.message
    assert "ANALYZE_EXECUTE" in out.message
    _assert_untouched(eng, raw)


@pytest.mark.parametrize(
    "other",
    [
        {"approve_action": "A-0123456789abcdef"},
        {"decline_action": "A-0123456789abcdef"},
        {"bind_evidence": [("X-0123456789abcdef", comment_url(PR, 5))]},
    ],
)
def test_unblock_rereview_excludes_the_action_flags(tmp_state_dir, fake_github, other):
    eng = _needs_fix(tmp_state_dir, fake_github)
    raw = eng.paths.state_file.read_text()
    with pytest.raises(ConfigurationError, match="--bind-evidence and --rereview"):
        eng.unblock(REASON, rereview=True, **other)
    _assert_untouched(eng, raw)
    assert fake_github.calls == []


def test_unblock_rereview_dry_run_writes_nothing(tmp_state_dir, fake_github):
    eng = _needs_fix(tmp_state_dir, fake_github)
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON, rereview=True, dry_run=True)
    assert out.dry_run and not out.unblocked
    assert out.message.startswith("[dry-run] would re-enter REVIEW: ")
    assert "R2-F1 are abandoned, not resolved" in out.message
    _assert_untouched(eng, raw)
    assert not eng.paths.logs_dir.exists()


def _worktree_at_reviewed_head(tmp_state_dir, gh, tracked=None):
    """The issue's agent worktree, created as a launch does, and a run reviewed at its HEAD.

    ``tracked`` (path: text) is committed in the worktree first, so the
    reviewed HEAD holds those files.
    """
    eng = make_engine(tmp_state_dir, [], github=gh)
    worktree = eng._ensure_agent_worktree()
    if tracked:
        for name, text in tracked.items():
            (worktree / name).write_text(text, "utf-8")
        git_out("-C", str(worktree), "add", *tracked)
        git_out(
            "-C", str(worktree), *NO_AUTO_MAINTENANCE, *GIT_IDENT, "commit", "-q", "-m", "Feature"
        )
    head = git_out("-C", str(worktree), "rev-parse", "HEAD")
    gh.add_pr(head_sha=head)
    fields = _reviewed(head=head, result="needs_fix", findings=[FINDING_R2])
    eng.state.phase = Phase.BLOCKED
    eng.state.block_reason = "agent reported blocked"
    eng.state.current_pr_url = PR
    for name, value in fields.items():
        setattr(eng.state, name, value)
    eng._save()
    return eng, worktree, head


BUGGY = "TTL_SECONDS = 0  # entries never expire\n"
FIXED = "TTL_SECONDS = 300\n"


def test_unblock_rereview_with_the_worktree_clean_at_the_reviewed_head(tmp_state_dir, fake_github):
    """The fixer blocked without a commit and left nothing uncommitted: the
    worktree holds nothing the reviewed HEAD does not. An ignored file is
    not a change: `git status` does not list it."""
    eng, worktree, _ = _worktree_at_reviewed_head(
        tmp_state_dir, fake_github, tracked={"cache.py": BUGGY}
    )
    common = Path(
        git_out("-C", str(eng.workdir), "rev-parse", "--path-format=absolute", "--git-common-dir")
    )
    (common / "info").mkdir(exist_ok=True)
    (common / "info" / "exclude").write_text("*.log\n", "utf-8")
    (worktree / "pytest.log").write_text("an ignored log\n", "utf-8")
    out = eng.unblock(REASON, rereview=True)
    assert out.unblocked and out.phase == "REVIEW", out.message
    assert (worktree / "pytest.log").exists()


def _change(worktree, change):
    """One uncommitted change of kind ``change`` in ``worktree``; the path it is at."""
    if change == "untracked":
        (worktree / "draft.txt").write_text("an uncommitted draft\n", "utf-8")
        return worktree / "draft.txt"
    (worktree / "cache.py").write_text(FIXED, "utf-8")
    if change == "staged":
        git_out("-C", str(worktree), "add", "cache.py")
    return worktree / "cache.py"


@pytest.mark.parametrize("change", ["untracked", "modified", "staged"])
def test_unblock_rereview_refuses_uncommitted_changes_in_the_worktree(
    tmp_state_dir, fake_github, change
):
    """PR #444 R1-F1: the next round's reviewer works in the agent worktree,
    so an uncommitted change there (a blocked fixer's half-made edit, a
    draft) is what it would read and test in place of the reviewed commit.
    Refused, nothing written and the change left exactly as it was; the
    plain unblock still re-enters FIX, whose prompt lists it."""
    eng, worktree, head = _worktree_at_reviewed_head(
        tmp_state_dir, fake_github, tracked={"cache.py": BUGGY}
    )
    path = _change(worktree, change)
    before = path.read_text("utf-8")
    status = git_out("-C", str(worktree), "status", "--porcelain")
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON, rereview=True)
    assert not out.unblocked
    assert (
        f"the agent worktree {worktree} holds 1 uncommitted change(s) on the reviewed HEAD "
        f"{head[:12]}" in out.message
    )
    assert "what it reads and tests there would be those changes" in out.message
    assert "Unblock without --rereview to let FIX continue from them" in out.message
    _assert_untouched(eng, raw)
    (record,) = _unblock_records(eng)
    assert record["metadata"]["applied"] is False
    assert record["metadata"]["abandoned_findings"] == []
    assert path.read_text("utf-8") == before
    assert git_out("-C", str(worktree), "status", "--porcelain") == status
    assert git_out("-C", str(worktree), "rev-parse", "HEAD") == head
    assert eng.unblock(REASON).phase == "FIX"


@pytest.mark.parametrize("mark", ["--assume-unchanged", "--skip-worktree"])
def test_unblock_rereview_refuses_an_edit_the_worktree_index_hides(
    tmp_state_dir, fake_github, mark
):
    """PR #444 R2-F1: the worktree's index is the fixer's to write, and a
    mark there hides an edit from `git status`. The files are compared with
    the reviewed tree through a fresh index, so the edit refuses the flag
    all the same; nothing is written, and the edit and its mark are left
    exactly as they were."""
    eng, worktree, head = _worktree_at_reviewed_head(
        tmp_state_dir, fake_github, tracked={"cache.py": BUGGY}
    )
    (worktree / "cache.py").write_text(FIXED, "utf-8")
    git_out("-C", str(worktree), "update-index", mark, "cache.py")
    assert git_out("-C", str(worktree), "status", "--porcelain") == ""
    marks = git_out("-C", str(worktree), "ls-files", "-v")
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON, rereview=True)
    assert not out.unblocked
    assert (
        f"the agent worktree {worktree} holds 1 uncommitted change(s) on the reviewed HEAD "
        f"{head[:12]} (`git -C {worktree} status` does not list 1 of them: 1 the worktree's "
        "index records as unchanged although they differ from the reviewed commit" in out.message
    )
    assert f"`git -C {worktree} ls-files -v` shows as a lower-case letter" in out.message
    assert "its prompt lists only what `git status` lists" in out.message
    _assert_untouched(eng, raw)
    assert (worktree / "cache.py").read_text("utf-8") == FIXED
    assert git_out("-C", str(worktree), "ls-files", "-v") == marks
    assert eng.unblock(REASON).phase == "FIX"


def _clean_filter_to(worktree: Path, path: str, committed: str) -> None:
    """A clean filter mapping whatever ``worktree``'s ``path`` holds back to ``committed``.

    Configured in the repository's own configuration and ``info/attributes``,
    which an agent without an enforced sandbox can write (PR #444 R3-F1): git
    then reads the file as ``committed``, whatever its bytes.
    """
    common = Path(
        git_out("-C", str(worktree), "rev-parse", "--path-format=absolute", "--git-common-dir")
    )
    source = common / f"committed-{path}"
    source.write_text(committed, "utf-8")
    clean = f"cat >/dev/null; cat {shlex.quote(str(source))}"
    git_out("-C", str(worktree), "config", "filter.revert.clean", clean)
    (common / "info").mkdir(exist_ok=True)
    (common / "info" / "attributes").write_text(f"{path} filter=revert\n", "utf-8")


def _drop_clean_filter(worktree: Path) -> None:
    common = Path(
        git_out("-C", str(worktree), "rev-parse", "--path-format=absolute", "--git-common-dir")
    )
    git_out("-C", str(worktree), "config", "--unset", "filter.revert.clean")
    (common / "info" / "attributes").unlink()


def test_unblock_rereview_refuses_an_edit_a_clean_filter_hides(tmp_state_dir, fake_github):
    """PR #444 R3-F1: git converts a file as its attributes say before it
    compares it, against any index, so a clean filter mapping the fixer's
    edit back to the reviewed blob hides it from both statuses, with the
    file also marked assume-unchanged as in the reviewer's reproduction.
    The controller compares the worktree's bytes with the reviewed tree
    itself and refuses the flag; nothing is written, and the edit, its mark
    and the filter are left as they were."""
    eng, worktree, head = _worktree_at_reviewed_head(
        tmp_state_dir, fake_github, tracked={"cache.py": BUGGY}
    )
    (worktree / "cache.py").write_text(FIXED, "utf-8")
    git_out("-C", str(worktree), "update-index", "--assume-unchanged", "cache.py")
    _clean_filter_to(worktree, "cache.py", BUGGY)
    assert git_out("-C", str(worktree), "status", "--porcelain") == ""
    assert git_out("-C", str(worktree), "diff", "--no-ext-diff", head) == ""
    marks = git_out("-C", str(worktree), "ls-files", "-v")
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON, rereview=True)
    assert not out.unblocked
    assert (
        f"the agent worktree {worktree} holds 1 uncommitted change(s) on the reviewed HEAD "
        f"{head[:12]} (`git -C {worktree} status` does not list 1 of them: 1 whose bytes or "
        "execute bit differ from the reviewed commit's although git reads them as its content"
        in out.message
    )
    assert f"`git -C {worktree} check-attr -a -- <file>` names" in out.message
    assert "its prompt lists only what `git status` lists" in out.message
    _assert_untouched(eng, raw)
    assert (worktree / "cache.py").read_text("utf-8") == FIXED
    assert git_out("-C", str(worktree), "ls-files", "-v") == marks
    assert git_out("-C", str(worktree), "config", "filter.revert.clean")
    assert eng.unblock(REASON).phase == "FIX"


def test_unblock_rereview_refuses_a_worktree_whose_status_cannot_be_read(
    tmp_state_dir, fake_github
):
    """A `git status` that fails proves nothing about what the reviewer
    would read: refused, never taken as clean."""
    eng, worktree, _ = _worktree_at_reviewed_head(tmp_state_dir, fake_github)
    (eng._agent_admin_dir(worktree) / "index").write_bytes(b"not an index")
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON, rereview=True)
    assert not out.unblocked
    assert f"cannot read the uncommitted changes of the agent worktree {worktree}" in out.message
    assert "is not knowable" in out.message
    _assert_untouched(eng, raw)


def test_unblock_rereview_refuses_a_worktree_holding_a_commit_the_pr_does_not(
    tmp_state_dir, fake_github
):
    """The fixer committed, then blocked: that commit was never pushed or
    reviewed, so a fresh round would leave it behind. Refused, state
    untouched, the worktree as it was; the plain unblock still re-enters FIX."""
    eng, worktree, head = _worktree_at_reviewed_head(tmp_state_dir, fake_github)
    fixed = commit_in(worktree, "Half a fix (#2)")
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON, rereview=True)
    assert not out.unblocked
    assert f"is at {fixed[:12]}, not at the reviewed HEAD {head[:12]}" in out.message
    assert "Unblock without --rereview to let FIX continue" in out.message
    _assert_untouched(eng, raw)
    assert git_out("-C", str(worktree), "rev-parse", "HEAD") == fixed
    assert eng.unblock(REASON).phase == "FIX"


def test_unblock_rereview_refuses_a_worktree_on_a_local_branch(tmp_state_dir, fake_github):
    """An attached HEAD is never resolved by the controller, so whether it
    holds work is not knowable: refused."""
    eng, worktree, _ = _worktree_at_reviewed_head(tmp_state_dir, fake_github)
    git_out("-C", str(worktree), "checkout", "-q", "-b", "side")
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON, rereview=True)
    assert not out.unblocked and "attached to a local branch" in out.message
    _assert_untouched(eng, raw)


def test_unblock_rereview_refuses_a_worktree_path_it_cannot_bind(tmp_state_dir, fake_github):
    """A directory at the worktree path that the registry does not bind is
    not a worktree the controller can read: refused, not taken as empty."""
    eng = _needs_fix(tmp_state_dir, fake_github)
    path = eng.agent_worktree_path()
    path.mkdir(parents=True)
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON, rereview=True)
    assert not out.unblocked and f"the agent worktree {path} cannot be read" in out.message
    _assert_untouched(eng, raw)


def _publish_with(eng, path: str, text: str) -> str:
    """A PR head on the checkout's HEAD whose tree holds ``path`` with ``text``; its SHA.

    Written with plumbing into the checkout's object store, so the
    checkout's HEAD, index and working tree are not moved, and published
    to the PR branch on the engine's origin.
    """
    workdir = str(eng.workdir)

    def plumbing(*args: str, data: str) -> str:
        res = subprocess.run(
            ["git", "-C", workdir, *args], input=data, capture_output=True, text=True, check=True
        )
        return res.stdout.strip()

    blob = plumbing("hash-object", "-w", "--stdin", data=text)
    tree = plumbing("mktree", data=f"100644 blob {blob}\t{path}\n")
    sha = git_out(
        "-C", workdir, *GIT_IDENT, "commit-tree", tree, "-p", "HEAD", "-m", "Add the cache (#2)"
    )
    eng.origin.publish(eng.workdir, sha, BRANCH)
    return sha


def _in_fix_of_round_two(tmp_state_dir, *handlers, files=None):
    """A REMOTE run entering FIX for round 2's R2-F1 on an origin-backed PR; ``(eng, gh, head)``.

    The PR head holds ``files`` (one path: text) when given.
    """
    gh = FakeGitHub()
    gh.add_issue(ISSUE, "Ship the feature")
    eng = make_engine(tmp_state_dir, scripted(*handlers), github=gh, origin=True)
    head = _publish_with(eng, *next(iter(files.items()))) if files else publish_pr_head(eng)
    gh.add_pr(head_sha=head, body="Adds the feature with the old cache design.")
    merge_base = git_out("-C", str(eng.workdir), "rev-parse", "HEAD")
    for name, value in (
        _reviewed(head=head, result="needs_fix", findings=[FINDING_R2])
        | {"reviewed_merge_base_sha": merge_base, "phase": Phase.FIX, "current_pr_url": PR}
    ).items():
        setattr(eng.state, name, value)
    gh.add_comment(PR, REVIEW_CID, review_comment_body(2, head, True))
    eng._save()
    return eng, gh, head


def _blocked_fixer(edit=None):
    """A fixer that checks out the reviewed HEAD, makes ``edit`` uncommitted, and blocks."""

    def fixer(req):
        git_out("-C", str(req.cwd), "checkout", "-q", "--detach", reviewed_head_in(req.prompt))
        if edit is not None:
            edit(Path(req.cwd))
        return block({"phase": "FIX", "status": "blocked", "message": "cannot edit a PR body"})

    return fixer


def test_a_finding_resolved_outside_the_repository_is_rereviewed_not_refixed(tmp_state_dir):
    """The incident of #428 end to end. Round 2 asks for a PR description
    rewrite; the fixer cannot edit a PR body, so it blocks without a commit,
    and the step says which unblock asks for a fresh round instead. The
    operator rewrites the body by hand and runs `unblock --rereview`: no
    fixer launches again, round 3 reviews the same HEAD with R2-F1 to
    re-check and the current body in its read context, and its clean
    verdict clears the carry and goes on to READY_FOR_MERGE."""
    prompts: list[str] = []
    bodies: list[str] = []

    def reviewer(req):
        prompts.append(req.prompt)
        bodies.append((read_context_of(req.prompt) / "pull-request.md").read_text())
        return block(review_result(3, head, []))

    eng, gh, head = _in_fix_of_round_two(tmp_state_dir, _blocked_fixer(), reviewer)

    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.message.startswith("agent reported blocked: cannot edit a PR body (")
    assert "unblock --rereview --reason" in out.message
    assert load_state(eng.paths.state_file).block_reason == "cannot edit a PR body"

    gh.prs[PR].body = "Adds the feature with the new cache design."
    out = eng.unblock(RESOLVED_OUTSIDE, rereview=True)
    assert out.unblocked and out.phase == "REVIEW", out.message

    out = eng.step()
    assert out.next_phase == "READY_FOR_MERGE", out.message
    assert len(eng.provider.calls) == 2  # the blocked fixer and round 3's reviewer
    [prompt] = prompts
    assert "the operator gave up that round's FIX at that HEAD" in prompt
    assert "This round is bound to that same HEAD: no commit moved" in prompt
    assert "R2-F1" in prompt and RESOLVED_OUTSIDE not in prompt
    assert "new cache design" in bodies[0]
    s = load_state(eng.paths.state_file)
    assert s.review_round == 3 and s.reviewed_head_sha == head
    assert s.prior_findings == [] and s.open_findings == [] and s.last_review_result == "clean"


CARRIED = {
    "id": "R3-F1",
    "classification": "blocked",
    "title": "cache entries never expire",
    "location": "cache.py:1",
    "required_resolution": "carried from R2-F1: give TTL_SECONDS a positive value",
}


@pytest.mark.parametrize(
    ("mark", "clean_filter"),
    [
        (None, False),
        ("--assume-unchanged", False),
        ("--skip-worktree", False),
        ("--assume-unchanged", True),
    ],
)
def test_a_rereview_judges_the_bound_commit_not_a_blocked_fixers_uncommitted_edit(
    tmp_state_dir, mark, clean_filter
):
    """PR #444 R1-F1, R2-F1 and R3-F1 end to end. The PR head's cache.py
    holds the bug; the fixer fixes it in the worktree, does not commit, and
    blocks, with or without marking the file in its index so that `git
    status` lists nothing, and, as the R3-F1 reproduction did, with a clean
    filter that maps the fix back to the committed bytes, so that `git
    status` against any index lists nothing either. A re-review then would
    have its reviewer, which reads and tests the worktree, clear code the
    PR does not hold: `unblock --rereview` is refused, nothing written and
    no reviewer launched, and the edit is left as it was. Once the operator
    has moved the edit out, the re-review's reviewer reads the reviewed
    commit's cache.py and raises the bug again."""
    seen: list[str] = []

    def reviewer(req):
        text = (Path(req.cwd) / "cache.py").read_text("utf-8")
        seen.append(text)
        return block(review_result(3, head, [] if text == FIXED else [CARRIED]))

    def half_fix(worktree):
        (worktree / "cache.py").write_text(FIXED, "utf-8")
        if mark:
            git_out("-C", str(worktree), "update-index", mark, "cache.py")
        if clean_filter:
            _clean_filter_to(worktree, "cache.py", BUGGY)

    eng, gh, head = _in_fix_of_round_two(
        tmp_state_dir, _blocked_fixer(half_fix), reviewer, files={"cache.py": BUGGY}
    )
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    worktree = eng.agent_worktree_path()
    status = git_out("-C", str(worktree), "status", "--porcelain")
    assert status == ("" if mark else "M cache.py")

    raw = eng.paths.state_file.read_text()
    out = eng.unblock(RESOLVED_OUTSIDE, rereview=True)
    assert not out.unblocked
    assert "holds 1 uncommitted change(s) on the reviewed HEAD" in out.message
    _assert_untouched(eng, raw)
    assert len(eng.provider.calls) == 1  # the blocked fixer only
    assert (worktree / "cache.py").read_text("utf-8") == FIXED

    # The operator moves the edit out of the worktree (here: drops it).
    if clean_filter:
        _drop_clean_filter(worktree)
    if mark:
        git_out("-C", str(worktree), "update-index", mark.replace("--", "--no-"), "cache.py")
    git_out("-C", str(worktree), "checkout", "-q", "--", "cache.py")
    out = eng.unblock(RESOLVED_OUTSIDE, rereview=True)
    assert out.unblocked and out.phase == "REVIEW", out.message
    out = eng.step()
    assert out.next_phase == "FIX", out.message
    assert seen == [BUGGY]
    s = load_state(eng.paths.state_file)
    assert s.review_round == 3 and s.reviewed_head_sha == head
    assert [f["id"] for f in s.open_findings] == ["R3-F1"] and s.prior_findings == []


def test_a_push_after_unblock_rereview_is_named_in_the_review_prompt(tmp_state_dir):
    """PR #444 R1-F3: someone pushes after `unblock --rereview` succeeded and
    before REVIEW binds its revision. The round reviews the new HEAD, and its
    prompt keeps the provenance (round 2's HEAD, where the operator gave up
    the FIX) while saying the PR moved since and naming the HEAD it is bound
    to: it never claims the HEAD did not move."""
    prompts: list[str] = []

    def reviewer(req):
        prompts.append(req.prompt)
        return block(review_result(3, pushed, []))

    eng, gh, head = _in_fix_of_round_two(tmp_state_dir, _blocked_fixer(), reviewer)
    assert eng.step().next_phase == "BLOCKED"
    out = eng.unblock(RESOLVED_OUTSIDE, rereview=True)
    assert out.unblocked and out.phase == "REVIEW", out.message

    pushed = publish_pr_head(eng, parent=head, message="Tidy the cache (#2)")
    gh.set_head(pushed)
    out = eng.step()
    assert out.next_phase == "READY_FOR_MERGE", out.message
    [prompt] = prompts
    assert f"Reviewed HEAD (bound by the controller): `{pushed}`" in prompt
    assert f"Review round 2 at HEAD `{head}`" in prompt
    assert "the operator gave up that round's FIX at that HEAD" in prompt
    assert "The PR moved after that request" in prompt
    assert f"it is bound to HEAD `{pushed}`, not `{head}`" in prompt
    assert "This round is bound to that same HEAD: no commit moved" not in prompt
    s = load_state(eng.paths.state_file)
    assert s.review_round == 3 and s.reviewed_head_sha == pushed
