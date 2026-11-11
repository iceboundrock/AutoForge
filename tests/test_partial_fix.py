"""A blocked FIX that publishes the commits it made for some findings (#433).

The fixer resolves what it can and reports ``blocked`` for a finding only a
human can act on, listing its resolutions (at least one ``fixed``) with its
candidate and the others as ``unresolved``. The controller checks that claim
as it checks a successful FIX, plans it as it plans a successful FIX (every
write that waits for the operator first, holding the push of the exact
candidate, which is last), completes the plan and reads it back, and stops
``BLOCKED`` with every finding of the round still open. Nothing re-reviews or
relaunches on its own, the REVIEW an unblock enters is held to the worktree
checks of a re-review, and the commits are never discarded, reset or forced.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from autoforge import effects as effects_module
from autoforge import state as state_module
from autoforge.effects import EffectKind, Stage
from autoforge.errors import ControlResultValidationError, GitHubError, StateError
from autoforge.git_transport import GitTransport
from autoforge.result_parser import (
    MAX_FIX_RATIONALE_CHARS,
    MAX_RESOLUTIONS_PER_FIX,
    PartialFixResult,
    is_partial_fix,
    parse_control_result,
)
from autoforge.state import AutoForgeState, load_state, save_state
from autoforge.stop_classification import StopReasonCode
from autoforge.transitions import Phase
from tests.conftest import (
    BRANCH,
    GIT_IDENT,
    ISSUE,
    PR,
    FakeGitHub,
    acceptance,
    block,
    comment_url,
    commit_in,
    fix_commit,
    fixed,
    git_out,
    no_change,
    pr_body_update,
    publish_pr_head,
    review_result,
    reviewed_head_in,
    scripted,
)
from tests.test_engine import _finding, _in_fix, _power_loss_at, _resumed, _worktree_head
from tests.test_state import make_state

MESSAGE = "R1-F3 needs the DEPLOY_TOKEN repository secret, which only an operator can set."
REASON = "The deploy job reads DEPLOY_TOKEN; setting a repository secret needs an operator."
FINDINGS = [_finding(1, 1), _finding(1, 2), _finding(1, 3)]


def unresolved(finding_id: str, reason: str = REASON, *, blocks: bool = False) -> dict:
    """An ``unresolved`` entry of a partial FIX result, as the fixer reports it."""
    return {"finding_id": finding_id, "reason": reason, "blocks_publication": blocks}


def stored(finding_id: str, reason: str = REASON) -> dict:
    """An unresolved finding as the controller saves it with a plan or a partial fix.

    Only a result that blocks no publication is planned (PR #475 R2-F2), so
    the answer is not saved.
    """
    return {"finding_id": finding_id, "reason": reason}


def partial_payload(prev: str, head: str, resolutions, left, **overrides) -> dict:
    """A REMOTE FIX result reporting ``blocked`` with a partial fix (#433 schema)."""
    return {
        "phase": "FIX",
        "status": "blocked",
        "message": MESSAGE,
        "previous_head_sha": prev,
        "head_sha": head,
        "resolutions": list(resolutions),
        "unresolved": list(left),
        **overrides,
    }


def partial_fixer(resolutions=None, left=None, *, commit: bool = True, during=None, **overrides):
    """A fixer that commits (unless ``commit`` is False), then reports a partial fix.

    ``during`` runs inside the launch, after the commit: a write someone
    else makes while the fixer works.
    """
    resolutions = [fixed("R1-F1"), fixed("R1-F2")] if resolutions is None else resolutions
    left = [unresolved("R1-F3")] if left is None else left

    def handler(req) -> str:
        reviewed = reviewed_head_in(req.prompt)
        if commit:
            head = fix_commit(req, "Fix R1-F1 and R1-F2 (#2)")
        else:
            git_out("-C", str(req.cwd), "checkout", "-q", "--detach", reviewed)
            head = reviewed
        if during is not None:
            during(req)
        return block(partial_payload(reviewed, head, resolutions, left, **overrides))

    return handler


def plain_blocked(req) -> str:
    return block({"phase": "FIX", "status": "blocked", "message": MESSAGE})


def in_fix(tmp_state_dir, gh, script, *, cfg=None):
    """A run in FIX after review round 1 with three open findings, on a real origin."""
    eng = _in_fix(tmp_state_dir, gh, script, findings=[dict(f) for f in FINDINGS], origin=True)
    if cfg is not None:
        eng.config = cfg
    eng.state.reviewed_pr_url = PR
    eng.state.reviewed_base_ref = "main"
    eng.state.current_base_ref = "main"
    eng.state.current_branch = BRANCH
    eng.state.last_review_result = "needs_fix"
    eng._save()
    return eng


def count_pushes(monkeypatch) -> list[str]:
    """Every push the controller's transport sends, by candidate."""
    sent: list[str] = []
    push = GitTransport.push

    def counting(self, *, sha, **kwargs):
        sent.append(sha)
        return push(self, sha=sha, **kwargs)

    monkeypatch.setattr(GitTransport, "push", counting)
    return sent


# -- the schema -------------------------------------------------------------------------


A, B = "a" * 40, "b" * 40


def _parsed(payload: dict) -> dict:
    return parse_control_result(block(payload), Phase.FIX)


def test_the_unresolved_list_and_nothing_else_makes_a_blocked_fix_partial():
    """Only the agent's ``unresolved`` opts in: a blocked result that merely
    carries other FIX fields stays a plain block, never read for a fix."""
    plain = {"phase": "FIX", "status": "blocked", "message": MESSAGE}
    assert not is_partial_fix(Phase.FIX, _parsed({**plain, "resolutions": [{"x": 1}]}))
    assert not is_partial_fix(Phase.FIX, _parsed({**plain, "head_sha": "not a sha"}))
    payload = partial_payload(A, B, [fixed("R1-F1")], [unresolved("R1-F2")])
    assert is_partial_fix(Phase.FIX, _parsed(payload))
    assert not is_partial_fix(Phase.REVIEW, {**payload, "phase": "REVIEW"})
    res = PartialFixResult.from_payload(payload)
    assert [r.finding_id for r in res.resolutions] == ["R1-F1"]
    assert [u.to_dict() for u in res.unresolved] == [unresolved("R1-F2")]
    assert res.held == ()


def test_whether_a_partial_fix_blocks_publication_is_its_typed_field_alone():
    """PR #475 R2-F2: the fixer answers per unresolved finding, as a JSON
    boolean; the same reason text with either answer is the same reason, and
    one blocking entry holds the whole result."""
    for blocks in (False, True):
        left = [unresolved("R1-F2", blocks=blocks), unresolved("R1-F3")]
        res = PartialFixResult.from_payload(_parsed(partial_payload(A, B, [fixed("R1-F1")], left)))
        assert res.held == (("R1-F2",) if blocks else ())
        assert [u.reason for u in res.unresolved] == [REASON, REASON]


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        ({"unresolved": []}, "'unresolved' must be a non-empty list"),
        ({"unresolved": "R1-F2"}, "'unresolved' must be a non-empty list"),
        ({"resolutions": []}, "resolves no finding"),
        ({"head_sha": None}, "missing required field 'head_sha'"),
        ({"previous_head_sha": "abc"}, "must be a full git SHA"),
        ({"resolutions": [no_change("R1-F1")]}, "none of its resolutions is 'fixed'"),
        (
            {
                "resolutions": [
                    {
                        "finding_id": "R1-F1",
                        "resolution": "operator_acceptance",
                        "rationale": "x" * 60,
                        "operator_acceptance": {
                            "actions": [
                                {"kind": "related_issue_comment", "issue": 16, "body": "Note."}
                            ]
                        },
                    }
                ]
            },
            "none of its resolutions is 'fixed'",
        ),
        ({"unresolved": [unresolved("R1-F1")]}, "names a finding twice"),
        ({"unresolved": [{"finding_id": "R1-F2"}]}, "missing required field 'reason'"),
        ({"unresolved": [{"finding_id": "R1-F2", "reason": "   "}]}, "'reason'"),
        ({"unresolved": [{**unresolved("R1-F2"), "url": "x"}]}, "carries 'url'"),
        ({"unresolved": [unresolved("bogus")]}, "invalid finding_id"),
        (
            {"unresolved": [{"finding_id": "R1-F2", "reason": REASON}]},
            "field 'blocks_publication' must be true or false",
        ),
        (
            {"unresolved": [{**unresolved("R1-F2"), "blocks_publication": "true"}]},
            "field 'blocks_publication' must be true or false",
        ),
        (
            {"unresolved": [{**unresolved("R1-F2"), "blocks_publication": None}]},
            "field 'blocks_publication' must be true or false",
        ),
        (
            {"unresolved": [{**unresolved("R1-F2"), "blocks_publication": 1}]},
            "field 'blocks_publication' must be true or false",
        ),
        (
            {"unresolved": [unresolved("R1-F2", "r" * (MAX_FIX_RATIONALE_CHARS + 1))]},
            f"accepts at most {MAX_FIX_RATIONALE_CHARS}",
        ),
        ({"unresolved": [unresolved("R1-F2", "a\x07b")]}, "control character"),
        (
            {"unresolved": [unresolved(f"R1-F{n}") for n in range(2, MAX_RESOLUTIONS_PER_FIX + 2)]},
            f"at most {MAX_RESOLUTIONS_PER_FIX} together",
        ),
    ],
)
def test_a_malformed_partial_fix_is_refused_whole(change, needle):
    """The offer is held to the FIX schema, so a malformed one is corrected,
    never read as a plain block and never clipped."""
    payload = {**partial_payload(A, B, [fixed("R1-F1")], [unresolved("R1-F2")]), **change}
    with pytest.raises(ControlResultValidationError, match="FIX|resolution|status") as exc:
        _parsed(payload)
    assert needle in str(exc.value)


def test_a_partial_fix_still_needs_the_message_of_a_block():
    payload = partial_payload(A, B, [fixed("R1-F1")], [unresolved("R1-F2")], message=" ")
    with pytest.raises(ControlResultValidationError, match="non-empty 'message'"):
        _parsed(payload)


# -- the publication ---------------------------------------------------------------------


def test_two_fixed_one_human_blocked_pushes_the_candidate_and_stays_blocked(
    tmp_state_dir, monkeypatch
):
    """The acceptance case: R1-F1 and R1-F2 fixed in one commit, R1-F3 needs a
    human. The candidate is checked, journaled as the one push, pushed over the
    reviewed HEAD and read back as the PR head; the run is BLOCKED with all
    three findings open, the two claims recorded as the last FIX's resolutions
    and the unresolved one with the fixer's reason. Nothing else was written."""
    gh = FakeGitHub()
    pushes = count_pushes(monkeypatch)
    moves: list[tuple[str, str]] = []
    eng = in_fix(tmp_state_dir, gh, partial_fixer())
    persist = eng._persist_effect

    def spy(record):
        moves.append((record.kind.value, record.stage.value))
        persist(record)

    eng._persist_effect = spy
    reviewed = eng.state.reviewed_head_sha
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop is not None and out.stop.code is StopReasonCode.PARTIAL_FIX
    candidate = _worktree_head(eng.provider.calls[0])
    assert candidate != reviewed and eng.origin.is_ancestor(reviewed, candidate)
    assert pushes == [candidate]
    assert eng.origin.head(BRANCH) == candidate == gh.prs[PR].head_sha
    assert moves == [("push", "attempted"), ("push", "observed")]
    assert gh.effect_writes == []
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.BLOCKED and s.stop_code == "partial-fix"
    assert s.open_findings == FINDINGS and s.prior_findings == []
    assert s.last_review_result == "needs_fix" and s.review_round == 1
    assert s.reviewed_head_sha == reviewed and s.current_head_sha == candidate
    assert [(r["finding_id"], r["resolution"]) for r in s.last_fix_resolutions] == [
        ("R1-F1", "fixed"),
        ("R1-F2", "fixed"),
    ]
    assert s.partial_fix == {
        "round": 1,
        "pr_url": PR,
        "reviewed_head_sha": reviewed,
        "candidate_sha": candidate,
        "published_by": "controller",
        "resolved": ["R1-F1", "R1-F2"],
        "unresolved": [stored("R1-F3")],
    }
    assert s.effect_records == [] and s.completion_context == {} and s.entry_observation == {}
    reason = s.block_reason
    assert MESSAGE in reason and REASON in reason
    assert f"pushed {candidate[:12]} over {reviewed[:12]}" in reason
    assert "None of the round's 3 finding(s) is marked resolved" in reason
    assert "R1-F1, R1-F2 are claimed fixed, and R1-F3 is not" in reason
    assert "REVIEW round 2 reviews" in reason and "unblock" in reason
    assert "Partial fix:" not in reason


def test_a_partial_fix_is_journaled_before_the_push(tmp_state_dir):
    """The plan (the push alone, the context with the unresolved findings and
    the fixer's message) is saved before anything is sent."""
    gh = FakeGitHub()
    eng = in_fix(tmp_state_dir, gh, partial_fixer())
    _power_loss_at(eng, EffectKind.PUSH, Stage.ATTEMPTED, saved=False)
    reviewed = eng.state.reviewed_head_sha
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    raw = json.loads(eng.paths.state_file.read_text())
    assert raw["phase"] == "FIX"
    (push,) = raw["effect_records"]
    assert push["kind"] == "push" and push["stage"] == "intended"
    assert push["precondition"]["expected_old"] == reviewed
    context = raw["completion_context"]
    assert [r["finding_id"] for r in context["resolutions"]] == ["R1-F1", "R1-F2"]
    assert context["unresolved"] == [stored("R1-F3")]
    assert context["message"] == MESSAGE
    assert eng.origin.head(BRANCH) == reviewed, "nothing pushed yet"


def test_a_dry_run_of_a_saved_partial_plan_says_it_stays_blocked(tmp_state_dir):
    """The saved plan is described from state alone: one push, then BLOCKED."""
    gh = FakeGitHub()
    eng = in_fix(tmp_state_dir, gh, partial_fixer())
    _power_loss_at(eng, EffectKind.PUSH, Stage.ATTEMPTED, saved=False)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    again = _resumed(eng, tmp_state_dir, gh, "never")
    before = again.paths.state_file.read_bytes()
    out = again.step(dry_run=True)
    notes = "\n".join(out.plan.notes)
    assert "would complete the partial fix of review round 1" in notes
    assert "stay BLOCKED with every finding open (R1-F3 left by the fixer)" in notes
    assert "would complete FIX of review round 1" not in notes
    assert again.paths.state_file.read_bytes() == before
    assert again.provider.calls == []


def test_a_plain_blocked_fix_pushes_nothing(tmp_state_dir, monkeypatch):
    """No valid candidate offered: BLOCKED with the agent's message, as before
    #433, and the fixer's commits stay unpushed in the worktree."""
    gh = FakeGitHub()
    pushes = count_pushes(monkeypatch)

    def commits_then_blocks(req) -> str:
        fix_commit(req)
        return plain_blocked(req)

    eng = in_fix(tmp_state_dir, gh, commits_then_blocks)
    reviewed = eng.state.reviewed_head_sha
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert out.stop is not None and out.stop.code is StopReasonCode.AGENT_BLOCKED
    assert pushes == [] and eng.origin.head(BRANCH) == reviewed
    s = load_state(eng.paths.state_file)
    assert s.block_reason == MESSAGE and s.partial_fix == {}
    assert s.open_findings == FINDINGS
    assert _worktree_head(eng.provider.calls[0]) != reviewed, "the commit is kept"


def test_a_blocked_fix_shaped_like_a_fix_without_unresolved_says_it_pushed_nothing(
    tmp_state_dir, monkeypatch
):
    gh = FakeGitHub()
    pushes = count_pushes(monkeypatch)

    def forgets_unresolved(req) -> str:
        reviewed = reviewed_head_in(req.prompt)
        head = fix_commit(req)
        payload = partial_payload(reviewed, head, [fixed("R1-F1")], [])
        del payload["unresolved"]
        return block(payload)

    eng = in_fix(tmp_state_dir, gh, forgets_unresolved)
    out = eng.step()
    assert out.next_phase == "BLOCKED" and pushes == []
    assert "carries no 'unresolved' list, so it offers no partial fix" in out.message
    assert load_state(eng.paths.state_file).block_reason == MESSAGE


def _wrong_head(req) -> str:
    reviewed = reviewed_head_in(req.prompt)
    fix_commit(req)
    return block(partial_payload(reviewed, "c" * 40, [fixed("R1-F1")], [unresolved("R1-F2")]))


def _no_commit(req) -> str:
    reviewed = reviewed_head_in(req.prompt)
    git_out("-C", str(req.cwd), "checkout", "-q", "--detach", reviewed)
    return block(partial_payload(reviewed, reviewed, [fixed("R1-F1")], [unresolved("R1-F2")]))


def _foreign_commit_sha(req) -> str:
    reviewed = reviewed_head_in(req.prompt)
    head = fix_commit(req)
    return block(
        partial_payload(
            reviewed, head, [fixed("R1-F1", commit_sha=reviewed)], [unresolved("R1-F2")]
        )
    )


def _missing_finding(req) -> str:
    reviewed = reviewed_head_in(req.prompt)
    head = fix_commit(req)
    return block(partial_payload(reviewed, head, [fixed("R1-F1")], [unresolved("R1-F9")]))


def _wrong_previous(req) -> str:
    head = fix_commit(req)
    return block(partial_payload("d" * 40, head, [fixed("R1-F1")], [unresolved("R1-F2")]))


def _off_the_reviewed_head(req) -> str:
    """A commit on the reviewed HEAD's parent: not a fast-forward of the PR branch."""
    reviewed = reviewed_head_in(req.prompt)
    git_out("-C", str(req.cwd), "checkout", "-q", "--detach", f"{reviewed}~1")
    head = commit_in(req.cwd, "Elsewhere (#2)")
    return block(partial_payload(reviewed, head, [fixed("R1-F1")], [unresolved("R1-F2")]))


def _closing_keyword(req) -> str:
    reviewed = reviewed_head_in(req.prompt)
    head = fix_commit(req, "Fix the typo, closes #7")
    return block(partial_payload(reviewed, head, [fixed("R1-F1")], [unresolved("R1-F2")]))


@pytest.mark.parametrize(
    ("first", "needle"),
    [
        (_wrong_head, "field 'head_sha' is"),
        (_no_commit, "a fixed finding needs a commit"),
        (_foreign_commit_sha, "is not one of the commits after the reviewed HEAD"),
        (_missing_finding, "must name exactly the open findings"),
        (_wrong_previous, "field 'previous_head_sha' is"),
        (_off_the_reviewed_head, "does not descend from the reviewed HEAD"),
        (_closing_keyword, "closing keyword"),
    ],
    ids=[
        "head",
        "no-commit",
        "commit-sha",
        "coverage",
        "previous",
        "ancestry",
        "message",
    ],
)
def test_an_unverifiable_partial_fix_is_corrected_and_publishes_nothing(
    tmp_state_dir, monkeypatch, first, needle
):
    """Mismatched claims, candidate identity, ancestry or commit policy: the
    offer is refused before anything is planned, the fixer is asked again with
    the reason, and its answer (here a plain block) decides. Nothing is pushed
    or published and the worktree is never reset."""
    gh = FakeGitHub()
    pushes = count_pushes(monkeypatch)
    findings = [_finding(1, 1), _finding(1, 2)]
    eng = _in_fix(tmp_state_dir, gh, scripted(first, plain_blocked), findings=findings, origin=True)
    reviewed = eng.state.reviewed_head_sha
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert len(eng.provider.calls) == 2
    assert needle in eng.provider.calls[1].prompt
    assert pushes == [] and eng.origin.head(BRANCH) == reviewed
    assert gh.effect_writes == []
    s = load_state(eng.paths.state_file)
    assert s.partial_fix == {} and s.effect_records == [] and s.open_findings == findings


def test_a_partial_fix_never_marks_a_finding_that_has_a_follow_up_fixed(tmp_state_dir):
    """An open follow-up issue is a finding's recorded deferral; a partial fix,
    which records no deferral, cannot claim that finding fixed."""
    from tests.test_engine import _marked

    gh = FakeGitHub()
    follow_up = gh.add_issue("https://github.com/owner/repo/issues/7", body=_marked("R1-F1")).url
    eng = in_fix(tmp_state_dir, gh, scripted(partial_fixer(), plain_blocked))
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert f"open issue {follow_up} already is the follow-up of R1-F1" in (
        eng.provider.calls[1].prompt
    )
    assert load_state(eng.paths.state_file).partial_fix == {}


def test_a_candidate_that_fails_validation_is_not_pushed_and_the_run_waits(
    tmp_state_dir, monkeypatch
):
    """ADR 0006 D5.1 holds for a partial fix: the exact candidate is validated
    first. A failure is recorded and counted, nothing is pushed, and the run
    stays BLOCKED on the fixer's message rather than relaunching it."""
    from tests.test_validation_gates import FAILS, config

    gh = FakeGitHub()
    pushes = count_pushes(monkeypatch)
    eng = in_fix(tmp_state_dir, gh, partial_fixer(), cfg=config(FAILS))
    reviewed = eng.state.reviewed_head_sha
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop is not None and out.stop.code is StopReasonCode.AGENT_BLOCKED
    assert len(eng.provider.calls) == 1
    assert pushes == [] and eng.origin.head(BRANCH) == reviewed
    s = load_state(eng.paths.state_file)
    (record,) = s.validation_records
    assert record["outcome"] == "FAIL"
    # Counted per phase entry: the stop that leaves FIX clears it (D5.2).
    assert s.producer_validation_failure == {}
    assert "validation of its candidate" in s.block_reason and MESSAGE in s.block_reason
    assert "Nothing was pushed" in s.block_reason
    assert s.partial_fix == {} and s.effect_records == [] and s.open_findings == FINDINGS


def test_a_pr_moved_while_the_fixer_ran_blocks_with_nothing_pushed(tmp_state_dir, monkeypatch):
    """A push the controller did not make: no lease is bypassed, nothing of the
    fixer's is pushed or adopted, and the run waits on the fixer's message
    instead of reviewing on its own. 'unblock' then reviews the actual HEAD
    with the round's findings carried."""
    gh = FakeGitHub()
    pushes = count_pushes(monkeypatch)
    human: list[str] = []

    def pushes_by_hand(req):
        reviewed = reviewed_head_in(req.prompt)
        human.append(publish_pr_head(eng, reviewed, "A human's push (#2)"))
        gh.prs[PR].head_sha = human[0]

    eng = in_fix(tmp_state_dir, gh, partial_fixer(during=pushes_by_hand))
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert out.stop is not None and out.stop.code is StopReasonCode.AGENT_BLOCKED
    assert pushes == [] and eng.origin.head(BRANCH) == human[0]
    s = load_state(eng.paths.state_file)
    assert "while the fixer ran (a push the controller did not make" in s.block_reason
    assert MESSAGE in s.block_reason and "Nothing was pushed" in s.block_reason
    assert s.open_findings == FINDINGS and s.partial_fix == {} and s.effect_records == []
    # PR #475 R2-F1: no partial fix is recorded, but the worktree the next
    # reviewer works in still holds the fixer's commit, which the PR does not.
    worktree = eng.agent_worktree_path()
    candidate = _worktree_head(eng.provider.calls[0])
    raw = eng.paths.state_file.read_text()
    refused = eng.unblock("the operator set the secret")
    assert not refused.unblocked, refused.message
    assert (
        f"the agent worktree {worktree} is at {candidate[:12]}, not at the PR's HEAD "
        f"{human[0][:12]} or the reviewed HEAD"
    ) in refused.message
    assert "so it may hold commits the PR does not" in refused.message
    assert eng.paths.state_file.read_text() == raw
    assert git_out("-C", str(worktree), "rev-parse", "HEAD") == candidate
    # The operator keeps the fixer's commit elsewhere and detaches the
    # worktree at the PR's head: the same unblock enters REVIEW.
    git_out("-C", str(worktree), "checkout", "-q", "--detach", human[0])
    applied = eng.unblock("the operator set the secret")
    assert applied.unblocked and applied.phase == "REVIEW", applied.message
    s = load_state(eng.paths.state_file)
    assert s.prior_findings == FINDINGS and s.open_findings == []


def test_a_crash_after_the_push_reconciles_it_once_as_the_controllers(tmp_state_dir, monkeypatch):
    """The push landed and the process died before its observation was saved.
    The next process completes the saved plan, reads the branch at the
    candidate, sends nothing again, records the push as its own (the attempt
    was journaled before it), and stops exactly as the first would have."""
    gh = FakeGitHub()
    pushes = count_pushes(monkeypatch)
    eng = in_fix(tmp_state_dir, gh, partial_fixer())
    reviewed = eng.state.reviewed_head_sha
    _power_loss_at(eng, EffectKind.PUSH, Stage.OBSERVED, saved=False)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    candidate = _worktree_head(eng.provider.calls[0])
    assert pushes == [candidate] and eng.origin.head(BRANCH) == candidate
    assert load_state(eng.paths.state_file).phase == Phase.FIX
    again = _resumed(eng, tmp_state_dir, gh, "never")
    out = again.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop is not None and out.stop.code is StopReasonCode.PARTIAL_FIX
    assert pushes == [candidate], "never pushed twice"
    assert "no fixer launched" not in out.message
    s = load_state(again.paths.state_file)
    assert s.partial_fix["published_by"] == "controller"
    assert s.partial_fix["candidate_sha"] == candidate
    assert f"pushed {candidate[:12]} over {reviewed[:12]}" in s.block_reason
    assert MESSAGE in s.block_reason, "the fixer's message is kept with the plan"


def test_an_exact_candidate_found_on_the_branch_is_recorded_as_external(tmp_state_dir, monkeypatch):
    """Someone pushed exactly the verified candidate by hand between the saved
    plan and the controller's push. Nothing is pushed again, and the record
    says the change on GitHub was not the controller's."""
    gh = FakeGitHub()
    pushes = count_pushes(monkeypatch)
    eng = in_fix(tmp_state_dir, gh, partial_fixer())
    _power_loss_at(eng, EffectKind.PUSH, Stage.ATTEMPTED, saved=False)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    candidate = _worktree_head(eng.provider.calls[0])
    eng.origin.publish(eng.workdir, candidate, BRANCH)
    gh.prs[PR].head_sha = candidate
    again = _resumed(eng, tmp_state_dir, gh, "never")
    out = again.step()
    assert out.next_phase == "BLOCKED"
    assert out.stop is not None and out.stop.code is StopReasonCode.PARTIAL_FIX
    assert pushes == []
    s = load_state(again.paths.state_file)
    assert s.partial_fix["published_by"] == "external"
    assert "pushed by someone else, and sent nothing" in s.block_reason


def test_another_push_before_the_controllers_is_never_forced_over(tmp_state_dir, monkeypatch):
    """The plan is saved, then someone pushes a different commit. The saved
    push is not sent over it (the lease and the drift rule agree), the plan is
    dropped as any FIX plan's would be, the findings are carried, and the run
    stays BLOCKED with the fixer's message instead of reviewing on its own."""
    gh = FakeGitHub()
    pushes = count_pushes(monkeypatch)
    eng = in_fix(tmp_state_dir, gh, partial_fixer())
    reviewed = eng.state.reviewed_head_sha
    _power_loss_at(eng, EffectKind.PUSH, Stage.ATTEMPTED, saved=False)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    human = publish_pr_head(eng, reviewed, "A human's push (#2)")
    gh.prs[PR].head_sha = human
    again = _resumed(eng, tmp_state_dir, gh, "never")
    out = again.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop is not None and out.stop.code is StopReasonCode.AGENT_BLOCKED
    assert pushes == [] and again.origin.head(BRANCH) == human
    s = load_state(again.paths.state_file)
    assert MESSAGE in s.block_reason and "partial fix is not recorded" in s.block_reason
    assert s.effect_records == [] and s.partial_fix == {}
    assert s.prior_findings == FINDINGS and s.last_review_result == "stale"


# -- writes that wait for the operator hold the push (PR #475 R1-F2) ------------------------


def _stages(eng) -> list[tuple[str, str]]:
    """Every persisted move of a plan record, as (kind, stage), in order."""
    moves: list[tuple[str, str]] = []
    persist = eng._persist_effect

    def spy(record):
        moves.append((record.kind.value, record.stage.value))
        persist(record)

    eng._persist_effect = spy
    return moves


def _records(eng) -> list:
    from autoforge.effects import EffectRecord

    return [EffectRecord.from_dict(r) for r in load_state(eng.paths.state_file).effect_records]


def _held_before_the_push(eng, kinds: list[str], pushes: list[str], reviewed: str) -> str:
    """The plan is saved with ``kinds`` before the push and nothing of it is sent."""
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.BLOCKED and s.partial_fix == {}
    assert [r["kind"] for r in s.effect_records] == [*kinds, "push"]
    assert s.completion_context["unresolved"] == [stored("R1-F3")]
    assert pushes == [] and eng.origin.head(BRANCH) == reviewed
    assert s.open_findings == FINDINGS
    assert "This plan is the partial fix of review round 1" in s.block_reason
    assert MESSAGE in s.block_reason
    return s.block_reason


def _recorded(eng, candidate: str, *, by: str = "controller") -> AutoForgeState:
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.BLOCKED and s.stop_code == "partial-fix", s.block_reason
    assert s.open_findings == FINDINGS and s.effect_records == []
    assert s.partial_fix["candidate_sha"] == candidate
    assert s.partial_fix["published_by"] == by
    assert s.partial_fix["resolved"] == ["R1-F1", "R1-F2"]
    assert eng.origin.head(BRANCH) == candidate
    return s


def test_an_acceptance_action_of_a_partial_fix_holds_the_push_until_approved(
    tmp_state_dir, monkeypatch
):
    """K9 (#211) under ``approve``: the partial plan is a successful FIX's plan,
    the acceptance comment first, so the push waits for the operator's approval
    instead of going out with the acceptance finding merely left unresolved.
    Once approved, the comment is published and read back, then the commit is
    pushed, and the run stays BLOCKED with every finding open."""
    gh = FakeGitHub()
    gh.add_issue(ISSUE, "Ship the feature")
    pushes = count_pushes(monkeypatch)
    resolutions = [fixed("R1-F1"), acceptance("R1-F2")]
    eng = in_fix(tmp_state_dir, gh, partial_fixer(resolutions))
    reviewed = eng.state.reviewed_head_sha
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop is not None and out.stop.code is StopReasonCode.ACCEPTANCE_ACTION
    reason = _held_before_the_push(eng, ["acceptance_comment"], pushes, reviewed)
    assert gh.effect_writes == []
    (k9, _) = _records(eng)
    assert k9.stage is Stage.INTENDED and not k9.authorized
    assert f"--approve-action {k9.action_id}" in reason
    done = eng.unblock("the operator read the comment", approve_action=k9.action_id)
    assert done.unblocked and done.phase == "FIX", done.message
    moves = _stages(eng)
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop is not None and out.stop.code is StopReasonCode.PARTIAL_FIX
    assert len(eng.provider.calls) == 1, "no fixer relaunched"
    assert moves == [
        ("acceptance_comment", "attempted"),
        ("acceptance_comment", "observed"),
        ("push", "attempted"),
        ("push", "observed"),
    ]
    candidate = _worktree_head(eng.provider.calls[0])
    assert pushes == [candidate]
    s = _recorded(eng, candidate)
    [comment] = gh.comments[ISSUE]
    assert [r["acceptance_comment_url"] for r in s.last_fix_resolutions] == ["", comment.url]
    assert "R1-F1 is claimed fixed; R1-F2 is claimed resolved as acceptance_action" in (
        s.block_reason
    )
    assert f"acceptance comment {comment.url} verified" in s.block_reason


def test_operator_actions_of_a_partial_fix_hold_the_push_until_each_is_verified(
    tmp_state_dir, monkeypatch
):
    """K10 (#243): the operator's comments are the plan's prefix, so the push of
    a partial fix waits until every one is verified, exactly as a successful
    FIX's does; then it completes from the journal with no fixer relaunched."""
    from tests.test_operator_actions import (
        NOTE16,
        NOTE158,
        T16,
        T158,
        _operating,
        _post,
        operator,
        payload,
    )

    gh = FakeGitHub()
    pushes = count_pushes(monkeypatch)
    resolutions = [fixed("R1-F1"), operator("R1-F2", (16, NOTE16), (158, NOTE158))]
    eng = _operating(tmp_state_dir, gh, partial_fixer(resolutions), findings=3)
    eng.state.open_findings = [dict(f) for f in FINDINGS]
    eng._save()
    reviewed = eng.state.reviewed_head_sha
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    _held_before_the_push(eng, ["operator_comment", "operator_comment"], pushes, reviewed)
    _post(gh, T16, 9016, payload(T16, NOTE16, "R1-F2"))
    assert eng.unblock("posted #16").unblocked
    assert eng.step().next_phase == "BLOCKED"
    _held_before_the_push(eng, ["operator_comment", "operator_comment"], pushes, reviewed)
    _post(gh, T158, 9158, payload(T158, NOTE158, "R1-F2"))
    assert eng.unblock("posted #158").unblocked
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop is not None and out.stop.code is StopReasonCode.PARTIAL_FIX
    assert len(eng.provider.calls) == 1
    candidate = _worktree_head(eng.provider.calls[0])
    assert pushes == [candidate]
    s = _recorded(eng, candidate)
    evidence = s.last_fix_resolutions[1]["operator_evidence"]
    assert [e["evidence"] for e in evidence] == ["exact", "exact"]


def test_a_pr_body_update_of_a_partial_fix_is_verified_before_the_push_and_recovers(
    tmp_state_dir, monkeypatch
):
    """K12 (#423) under ``approve``: the replacement waits for the operator and
    holds the push. Approved, it is written and read back; a crash right after
    is completed by the next process from the journal: one body write, one
    push, then BLOCKED with every finding open."""
    from tests.test_pr_body_update import _payload, _resumed, _updating

    gh = FakeGitHub()
    pushes = count_pushes(monkeypatch)
    resolutions = [fixed("R1-F1"), pr_body_update("R1-F2")]
    eng = _updating(tmp_state_dir, gh, partial_fixer(resolutions), policy="approve", findings=3)
    eng.state.open_findings = [dict(f) for f in FINDINGS]
    eng._save()
    reviewed = eng.state.reviewed_head_sha
    run = eng.state.run_id
    before = gh.prs[PR].body
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop is not None and out.stop.code is StopReasonCode.ACCEPTANCE_ACTION
    _held_before_the_push(eng, ["pr_body_update"], pushes, reviewed)
    assert gh.prs[PR].body == before and gh.effect_writes == []
    (k12, _) = _records(eng)
    assert eng.unblock("read the description", approve_action=k12.action_id).unblocked
    _power_loss_at(eng, EffectKind.PR_BODY_UPDATE, Stage.OBSERVED, saved=True)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    assert gh.prs[PR].body == _payload(run) and pushes == []
    again = _resumed(eng, tmp_state_dir, gh, policy="approve")
    out = again.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop is not None and out.stop.code is StopReasonCode.PARTIAL_FIX
    assert again.provider.calls == []
    candidate = _worktree_head(eng.provider.calls[0])
    assert pushes == [candidate]
    assert [w[0] for w in gh.effect_writes] == ["write_pr_body"]
    s = _recorded(again, candidate)
    assert s.last_fix_resolutions[1]["pr_body_update"]["reviewed_head_sha"] == reviewed


def test_a_partial_fix_with_an_acceptance_action_is_corrected_while_the_setting_is_off(
    tmp_state_dir, monkeypatch
):
    """Each resolution kind is held to a successful FIX's rules: an acceptance
    action the configuration does not offer is corrected, nothing planned."""
    from autoforge.config import default_config

    gh = FakeGitHub()
    gh.add_issue(ISSUE, "Ship the feature")
    pushes = count_pushes(monkeypatch)
    cfg = default_config()
    cfg.safety = replace(cfg.safety, acceptance_actions="off")
    first = partial_fixer([fixed("R1-F1"), acceptance("R1-F2")])
    # PR #475 R2-F2: corrected, the fixer holds the result instead.
    held = partial_fixer([fixed("R1-F1")], [unresolved("R1-F2", blocks=True), unresolved("R1-F3")])
    eng = in_fix(tmp_state_dir, gh, scripted(first, held), cfg=cfg)
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert out.stop is not None and out.stop.code is StopReasonCode.AGENT_BLOCKED
    correction = eng.provider.calls[1].prompt
    assert "which this run does not offer (safety.acceptance_actions: off)" in correction
    assert (
        "In this partial fix, if what that finding needs cannot be a resolution here and must "
        "be on GitHub before your commits are, list it in 'unresolved' with "
        "'blocks_publication': true instead"
    ) in " ".join(correction.split())
    assert pushes == [] and gh.effect_writes == []
    s = load_state(eng.paths.state_file)
    assert s.effect_records == [] and s.partial_fix == {} and s.open_findings == FINDINGS
    assert "it marks R1-F2 'blocks_publication'" in s.block_reason


# -- a prerequisite no resolution can carry holds the whole result (PR #475 R2-F2) ---------

PREREQUISITE = "R1-F3 needs {what}, which must be on GitHub before these commits are reviewed."
HELD_HINT = 'list it under `unresolved` with `"blocks_publication": true`'


def _safety(**changes):
    from autoforge.config import default_config

    cfg = default_config()
    cfg.safety = replace(cfg.safety, **changes)
    return cfg


def _unreadable_issue(gh) -> None:
    gh.get_issue_errors[ISSUE] = GitHubError("HTTP 403: Resource not accessible by integration")


@pytest.mark.parametrize(
    ("safety", "setup", "what", "unavailable"),
    [
        (
            {"pr_body_updates": "off"},
            None,
            "a PR description update",
            "The `pr_body_update` resolution is not available in this FIX entry "
            "(`safety.pr_body_updates: off`)",
        ),
        (
            {},
            None,
            "a PR description update",
            "The `pr_body_update` resolution is not available in this FIX entry (when FIX was "
            "entered the PR's body did not hold exactly the controller's closing block",
        ),
        (
            {"acceptance_actions": "off"},
            None,
            "a comment on the issue",
            "The `acceptance_action` resolution is not available in this run "
            "(`safety.acceptance_actions: off`)",
        ),
        (
            {"acceptance_actions": "off"},
            None,
            "comments on other issues",
            "The `operator_acceptance` resolution is not available in this FIX entry "
            "(`safety.acceptance_actions: off`)",
        ),
        (
            {},
            _unreadable_issue,
            "comments on other issues",
            "The `operator_acceptance` resolution is not available in this FIX entry (the "
            "controller could not read the run's issue when FIX was entered)",
        ),
    ],
    ids=["K12-disabled", "K12-unoffered", "K9-disabled", "K10-disabled", "K10-unoffered"],
)
def test_an_unavailable_publication_prerequisite_publishes_nothing(
    tmp_state_dir, monkeypatch, safety, setup, what, unavailable
):
    """The fixer fixed R1-F1 and R1-F2, and R1-F3 needs text that must be on
    GitHub before its commits, which this run cannot carry as a resolution, so
    no plan could order it before the push. The prompt tells the fixer to say
    so in the typed field, and it does: nothing is pushed, created or
    appended, no claim is recorded, the commits stay in the worktree and the
    run waits BLOCKED with every finding open. The unblock relaunches the
    fixer, which finds its commits where it left them."""
    gh = FakeGitHub()
    pushes = count_pushes(monkeypatch)
    relaunched: list[str] = []

    def continues(req) -> str:
        relaunched.append(_worktree_head(req))
        return plain_blocked(req)

    left = [unresolved("R1-F3", PREREQUISITE.format(what=what), blocks=True)]
    cfg = _safety(**safety) if safety else None
    if setup is not None:
        setup(gh)
    eng = in_fix(tmp_state_dir, gh, scripted(partial_fixer(left=left), continues), cfg=cfg)
    reviewed = eng.state.reviewed_head_sha
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop is not None and out.stop.code is StopReasonCode.AGENT_BLOCKED
    (first,) = eng.provider.calls
    assert unavailable in first.prompt and HELD_HINT in first.prompt
    flat = " ".join(first.prompt.split())
    assert "It is always `true` for text the finding needs on the issue" in flat
    candidate = _worktree_head(first)
    assert candidate != reviewed
    assert pushes == [] and gh.effect_writes == [] and eng.origin.head(BRANCH) == reviewed
    s = load_state(eng.paths.state_file)
    assert s.effect_records == [] and s.completion_context == {} and s.partial_fix == {}
    assert s.last_fix_resolutions == [] and s.open_findings == FINDINGS
    assert "it marks R1-F3 'blocks_publication'" in s.block_reason
    assert "the controller publishes nothing of it" in s.block_reason
    assert "none of its claims was checked or recorded" in s.block_reason
    assert MESSAGE in s.block_reason and PREREQUISITE.format(what=what) in s.block_reason
    worktree = eng.agent_worktree_path()
    assert git_out("-C", str(worktree), "rev-parse", "HEAD") == candidate
    applied = eng.unblock("the operator met the need")
    assert applied.unblocked and applied.phase == "FIX", applied.message
    assert eng.step().next_phase == "BLOCKED"
    assert relaunched == [candidate], "the next fixer starts from the kept commits"
    assert pushes == [] and eng.origin.head(BRANCH) == reviewed


def test_one_blocking_entry_holds_a_result_whose_other_needs_are_independent(
    tmp_state_dir, monkeypatch
):
    """A partial fix with an operator write (K9) that would hold the push, and an
    unresolved need marked blocking: nothing is planned, not even the
    acceptance comment, and nothing is published."""
    gh = FakeGitHub()
    gh.add_issue(ISSUE, "Ship the feature")
    pushes = count_pushes(monkeypatch)
    resolutions = [fixed("R1-F1"), acceptance("R1-F2")]
    left = [unresolved("R1-F3", blocks=True)]
    eng = in_fix(tmp_state_dir, gh, partial_fixer(resolutions, left))
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop is not None and out.stop.code is StopReasonCode.AGENT_BLOCKED
    assert pushes == [] and gh.effect_writes == [] and gh.comments.get(ISSUE, []) == []
    s = load_state(eng.paths.state_file)
    assert s.effect_records == [] and s.acceptance_outcomes == []


def test_a_held_partial_fix_is_not_corrected_for_claims_it_never_publishes(
    tmp_state_dir, monkeypatch
):
    """Nothing of a held result is acted on, so its claims are not checked: a
    head_sha the worktree does not hold asks for no correction."""

    def wrong_head(req) -> str:
        reviewed = reviewed_head_in(req.prompt)
        fix_commit(req, "Fix R1-F1 and R1-F2 (#2)")
        left = [unresolved("R1-F3", blocks=True)]
        return block(partial_payload(reviewed, "c" * 40, [fixed("R1-F1"), fixed("R1-F2")], left))

    gh = FakeGitHub()
    pushes = count_pushes(monkeypatch)
    eng = in_fix(tmp_state_dir, gh, wrong_head)
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert len(eng.provider.calls) == 1
    assert pushes == []


# -- the exact candidate pushed while the fixer ran (PR #475 R1-F3) -------------------------


def _pushes_the_candidate(holder: list):
    """A ``during`` hook: someone pushes exactly the fixer's commit by hand."""

    def during(req):
        eng = holder[0]
        candidate = _worktree_head(req)
        eng.origin.publish(eng.workdir, candidate, BRANCH)
        holder[1].prs[PR].head_sha = candidate

    return during


def test_the_exact_candidate_pushed_while_the_fixer_ran_is_recorded_as_external(
    tmp_state_dir, monkeypatch
):
    """The operator pushed the fixer's commit by hand while it was still
    reporting. The candidate is held to every check before planning (binding,
    branch, validation, follow-ups), planned, and its push finds the branch at
    it: nothing is sent, the claims are recorded, and the record says the
    commit was published externally. The reviewer is told so, not that the
    controller pushed it."""
    gh = FakeGitHub()
    pushes = count_pushes(monkeypatch)
    holder: list = []
    reviewer_prompts: list[str] = []

    def reviewer(req) -> str:
        reviewer_prompts.append(req.prompt)
        return block(review_result(2, eng.state.current_head_sha, []))

    eng = in_fix(
        tmp_state_dir, gh, scripted(partial_fixer(during=_pushes_the_candidate(holder)), reviewer)
    )
    holder.extend([eng, gh])
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop is not None and out.stop.code is StopReasonCode.PARTIAL_FIX
    candidate = _worktree_head(eng.provider.calls[0])
    assert pushes == []
    s = _recorded(eng, candidate, by="external")
    assert [(r["finding_id"], r["resolution"]) for r in s.last_fix_resolutions] == [
        ("R1-F1", "fixed"),
        ("R1-F2", "fixed"),
    ]
    assert "pushed by someone else, and sent nothing" in s.block_reason
    preview = eng.unblock("the operator set the secret", dry_run=True)
    assert "someone else pushed, and the controller verified," in preview.message
    assert "the controller published" not in preview.message
    assert eng.unblock("the operator set the secret").phase == "REVIEW"
    assert eng.step().next_phase == "READY_FOR_MERGE"
    (prompt,) = reviewer_prompts
    assert (
        f"verified the fixer's commits at `{candidate}` and found the PR branch already at "
        "exactly that commit, pushed by someone else, so it pushed nothing"
    ) in prompt
    assert "and pushed them" not in prompt
    # The static text says who may have pushed, never that it was the controller.
    flat = " ".join(prompt.split())
    assert "the line says who put it on the PR branch" in flat
    assert "the HEAD it names is the one the controller verified and pushed" not in flat


def test_the_exact_candidate_pushed_ahead_of_an_operator_write_is_not_planned(
    tmp_state_dir, monkeypatch
):
    """The commits are already out ahead of the acceptance action a FIX plan
    orders before them: no plan can represent that, so nothing is planned or
    sent, and the run waits on the fixer's message with the findings open."""
    gh = FakeGitHub()
    gh.add_issue(ISSUE, "Ship the feature")
    pushes = count_pushes(monkeypatch)
    holder: list = []
    fixer_ = partial_fixer(
        [fixed("R1-F1"), acceptance("R1-F2")], during=_pushes_the_candidate(holder)
    )
    eng = in_fix(tmp_state_dir, gh, fixer_)
    holder.extend([eng, gh])
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop is not None and out.stop.code is StopReasonCode.AGENT_BLOCKED
    assert pushes == [] and gh.effect_writes == []
    s = load_state(eng.paths.state_file)
    assert "ahead of the operator's part the resolutions of R1-F2 ask for" in s.block_reason
    assert MESSAGE in s.block_reason
    assert s.effect_records == [] and s.partial_fix == {} and s.open_findings == FINDINGS


# -- after the stop -----------------------------------------------------------------------


def _published(tmp_state_dir, gh, *, review=None):
    eng = in_fix(tmp_state_dir, gh, scripted(partial_fixer(), *(review or [])))
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    return eng


def test_unblock_rereviews_the_published_head_with_every_finding_carried(tmp_state_dir):
    """Once the operator has acted, 'unblock' re-enters REVIEW of the published
    HEAD (never FIX: the findings are bound to the reviewed HEAD), carrying all
    three findings. The reviewer is told which were claimed fixed, which were
    left open and why, as data; the completed round clears the record."""
    gh = FakeGitHub()
    holder: list = []

    def reviewer(req) -> str:
        holder.append(req)
        head = eng.state.current_head_sha
        return block(review_result(2, head, []))

    eng = _published(tmp_state_dir, gh, review=[reviewer])
    candidate = eng.state.partial_fix["candidate_sha"]
    preview = eng.unblock("the operator set the secret", dry_run=True)
    assert "would re-enter REVIEW" in preview.message
    assert f"its HEAD {candidate[:12]} is the partial fix of review round 1" in preview.message
    applied = eng.unblock("the operator set the secret")
    assert applied.unblocked and applied.phase == "REVIEW"
    s = load_state(eng.paths.state_file)
    assert s.prior_findings == FINDINGS and s.open_findings == []
    assert s.partial_fix["candidate_sha"] == candidate, "kept for the review"
    out = eng.step()
    assert out.next_phase == "READY_FOR_MERGE", out.message
    (req,) = holder
    assert f"verified the fixer's commits at `{candidate}` and pushed them" in req.prompt
    assert "claims resolutions of R1-F1, R1-F2" in req.prompt
    assert "left R1-F3 unresolved" in req.prompt
    assert "This round is bound to that published HEAD" in req.prompt
    assert REASON in req.prompt
    s = load_state(eng.paths.state_file)
    assert s.review_round == 2 and s.partial_fix == {} and s.prior_findings == []


def test_unblock_after_a_partial_fix_is_held_to_the_review_round_cap(tmp_state_dir):
    """The partial fix buys no review round: at the cap, unblock refuses."""
    gh = FakeGitHub()
    eng = _published(tmp_state_dir, gh)
    eng.config.workflow.max_review_rounds = 1
    before = eng.paths.state_file.read_text()
    out = eng.unblock("the operator set the secret")
    assert not out.unblocked and "max_review_rounds" in out.message
    assert eng.paths.state_file.read_text() == before


def test_rereview_is_refused_after_a_partial_fix_and_names_the_plain_unblock(tmp_state_dir):
    gh = FakeGitHub()
    eng = _published(tmp_state_dir, gh)
    out = eng.unblock("done", rereview=True)
    assert not out.unblocked
    assert "Without the flag" in out.message and "partial fix of review round 1" in out.message


def test_status_names_the_partial_fix(tmp_state_dir, capsys):
    from autoforge import cli

    gh = FakeGitHub()
    eng = _published(tmp_state_dir, gh)
    candidate = eng.state.partial_fix["candidate_sha"]
    eng.close()
    assert cli.main(["--state-dir", str(tmp_state_dir), "status"]) == 0
    out = capsys.readouterr().out
    assert (
        f"Partial fix:   {candidate} over {eng.state.reviewed_head_sha} (round 1, the "
        "controller's push): R1-F1, R1-F2 claimed resolved, R1-F3 left open"
    ) in out
    assert "partial-fix" in out


# -- the worktree the next reviewer works in (PR #475 R1-F1) -------------------------------

BUGGY = "def cache():\n    return None\n"
FIXED = "def cache():\n    return {}\n"


def _fixer_leaving(leave):
    """A fixer that commits a real file for R1-F1 and R1-F2, leaves ``leave``
    behind uncommitted in its worktree, and reports a partial fix."""

    def handler(req) -> str:
        reviewed = reviewed_head_in(req.prompt)
        cwd = Path(req.cwd)
        git_out("-C", str(cwd), "checkout", "-q", "--detach", reviewed)
        (cwd / "cache.py").write_text(BUGGY, "utf-8")
        git_out("-C", str(cwd), "add", "cache.py")
        git_out("-C", str(cwd), *GIT_IDENT, "commit", "-q", "-m", "Fix R1-F1 and R1-F2 (#2)")
        head = git_out("-C", str(cwd), "rev-parse", "HEAD")
        leave(cwd)
        return block(
            partial_payload(reviewed, head, [fixed("R1-F1"), fixed("R1-F2")], [unresolved("R1-F3")])
        )

    return handler


def _untracked(cwd: Path) -> None:
    (cwd / "draft.txt").write_text("a half-made fix for R1-F3\n", "utf-8")


def _modified(cwd: Path) -> None:
    (cwd / "cache.py").write_text(FIXED, "utf-8")


def _staged(cwd: Path) -> None:
    _modified(cwd)
    git_out("-C", str(cwd), "add", "cache.py")


def _concealed(cwd: Path) -> None:
    """An edit the worktree's own index hides from `git status`."""
    _modified(cwd)
    git_out("-C", str(cwd), "update-index", "--assume-unchanged", "cache.py")
    assert git_out("-C", str(cwd), "status", "--porcelain") == ""


@pytest.mark.parametrize(
    ("leave", "needle"),
    [
        (_untracked, "`git -C {wt} status` lists them"),
        (_modified, "`git -C {wt} status` lists them"),
        (_staged, "`git -C {wt} status` lists them"),
        (_concealed, "index records as unchanged although they differ from the commit under"),
    ],
    ids=["untracked", "modified", "staged", "concealed"],
)
def test_unblock_after_a_partial_fix_refuses_a_worktree_holding_uncommitted_work(
    tmp_state_dir, monkeypatch, leave, needle
):
    """The partial fix was published from the worktree the next reviewer works
    in, and the fixer left work there for R1-F3: what that reviewer would read
    and test in place of the published commit. Unblock refuses REVIEW, writes
    nothing and leaves the work exactly as it was; once the operator has moved
    it out, the same unblock enters REVIEW."""
    gh = FakeGitHub()
    eng = in_fix(tmp_state_dir, gh, _fixer_leaving(leave))
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop is not None and out.stop.code is StopReasonCode.PARTIAL_FIX
    worktree = eng.agent_worktree_path()
    candidate = eng.state.partial_fix["candidate_sha"]
    status = git_out("-C", str(worktree), "status", "--porcelain")
    raw = eng.paths.state_file.read_text()
    refused = eng.unblock("the operator set the secret")
    assert not refused.unblocked, refused.message
    assert (
        f"the agent worktree {worktree} holds 1 uncommitted change(s) on the partial fix's "
        f"candidate {candidate[:12]}"
    ) in refused.message
    assert needle.format(wt=worktree) in refused.message
    assert "what it reads and tests there would be those changes" in refused.message
    assert "move them out of the worktree yourself" in refused.message
    assert eng.paths.state_file.read_text() == raw
    assert git_out("-C", str(worktree), "status", "--porcelain") == status
    assert git_out("-C", str(worktree), "rev-parse", "HEAD") == candidate
    if leave is _untracked:
        assert (worktree / "draft.txt").exists()
        (worktree / "draft.txt").unlink()
    else:
        assert (worktree / "cache.py").read_text("utf-8") == FIXED
        git_out("-C", str(worktree), "update-index", "--no-assume-unchanged", "cache.py")
        git_out("-C", str(worktree), "checkout", "-q", "HEAD", "--", "cache.py")
    applied = eng.unblock("the operator moved the draft out")
    assert applied.unblocked and applied.phase == "REVIEW", applied.message


def test_unblock_after_a_partial_fix_refuses_a_worktree_moved_off_the_candidate(tmp_state_dir):
    """A commit made in the worktree after the publication is not on the PR:
    a review there would read it. Refused, the commit kept."""
    gh = FakeGitHub()
    eng = _published(tmp_state_dir, gh)
    worktree = eng.agent_worktree_path()
    candidate = eng.state.partial_fix["candidate_sha"]
    later = commit_in(worktree, "More work (#2)")
    out = eng.unblock("the operator set the secret")
    assert not out.unblocked
    assert (
        f"is at {later[:12]}, not at the partial fix's candidate {candidate[:12]} or the "
        f"reviewed HEAD {eng.state.reviewed_head_sha[:12]}, so it may hold commits the PR does not"
    ) in out.message
    assert git_out("-C", str(worktree), "rev-parse", "HEAD") == later


def test_unblock_after_a_partial_fix_enters_review_from_a_clean_published_worktree(
    tmp_state_dir,
):
    """The worktree holds exactly the published candidate: REVIEW is entered."""
    gh = FakeGitHub()
    eng = in_fix(tmp_state_dir, gh, _fixer_leaving(lambda cwd: None))
    assert eng.step().next_phase == "BLOCKED"
    out = eng.unblock("the operator set the secret")
    assert out.unblocked and out.phase == "REVIEW", out.message


# -- the check survives drift, recovery and a plain block (PR #475 R2-F1) ------------------

LEFT_BEHIND = [
    (_untracked, "`git -C {wt} status` lists them"),
    (_modified, "`git -C {wt} status` lists them"),
    (_staged, "`git -C {wt} status` lists them"),
    (_concealed, "index records as unchanged although they differ from the commit under"),
]
LEFT_BEHIND_IDS = ["untracked", "modified", "staged", "concealed"]


def _moved_out(cwd: Path, leave) -> None:
    """What an operator who moved the fixer's leftover work out of ``cwd`` leaves."""
    if leave is _untracked:
        (cwd / "draft.txt").unlink()
        return
    git_out("-C", str(cwd), "update-index", "--no-assume-unchanged", "cache.py")
    git_out("-C", str(cwd), "checkout", "-q", "HEAD", "--", "cache.py")


def _refuses_until_moved_out(eng, leave, needle: str, label: str, head: str) -> None:
    """Unblock refuses REVIEW with nothing written or discarded, then enters it."""
    worktree = eng.agent_worktree_path()
    status = git_out("-C", str(worktree), "status", "--porcelain")
    raw = eng.paths.state_file.read_text()
    refused = eng.unblock("the operator set the secret")
    assert not refused.unblocked, refused.message
    assert f"REVIEW round {eng.state.review_round + 1} is not entered" in refused.message
    assert (
        f"the agent worktree {worktree} holds 1 uncommitted change(s) on {label} {head[:12]}"
    ) in refused.message
    assert needle.format(wt=worktree) in refused.message
    assert "what it reads and tests there would be those changes" in refused.message
    assert eng.paths.state_file.read_text() == raw
    assert git_out("-C", str(worktree), "status", "--porcelain") == status
    assert git_out("-C", str(worktree), "rev-parse", "HEAD") == head
    if leave is not _untracked:
        assert (worktree / "cache.py").read_text("utf-8") == FIXED, "never discarded"
    _moved_out(worktree, leave)
    applied = eng.unblock("the operator moved the work out")
    assert applied.unblocked and applied.phase == "REVIEW", applied.message


@pytest.mark.parametrize(("leave", "needle"), LEFT_BEHIND, ids=LEFT_BEHIND_IDS)
def test_a_partial_push_dropped_by_drift_after_a_crash_keeps_the_worktree_check(
    tmp_state_dir, monkeypatch, leave, needle
):
    """The partial push landed and the process died before it was read back;
    then the PR was retargeted. The next process finds the drift, drops the
    plan (``partial_fix`` is never set) and stays BLOCKED. The plain unblock
    into REVIEW is still held to the worktree checks: the fixer's uncommitted
    fix for R1-F3 would otherwise be what the reviewer reads and tests, while
    the published candidate holds the buggy version."""
    gh = FakeGitHub()
    pushes = count_pushes(monkeypatch)
    eng = in_fix(tmp_state_dir, gh, _fixer_leaving(leave))
    _power_loss_at(eng, EffectKind.PUSH, Stage.OBSERVED, saved=False)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    candidate = _worktree_head(eng.provider.calls[0])
    assert pushes == [candidate] and eng.origin.head(BRANCH) == candidate
    gh.prs[PR].base_ref = "release/1.x"
    again = _resumed(eng, tmp_state_dir, gh, "never")
    out = again.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop is not None and out.stop.code is StopReasonCode.AGENT_BLOCKED
    assert pushes == [candidate], "never pushed twice"
    s = load_state(again.paths.state_file)
    assert s.partial_fix == {} and s.effect_records == [], "the drift dropped the plan"
    assert s.prior_findings == FINDINGS and s.last_review_result == "stale"
    assert (Path(again.agent_worktree_path()) / "cache.py").exists()
    _refuses_until_moved_out(again, leave, needle, "the PR's HEAD", candidate)


def test_a_partial_plan_dropped_by_drift_before_its_push_keeps_the_worktree_check(
    tmp_state_dir, monkeypatch
):
    """The plan was saved and the PR retargeted before its push: the plan is
    dropped unsent and the run stays BLOCKED with no partial fix recorded. The
    fixer's commit stays in the worktree, unpublished, so REVIEW of the PR's
    revision is not entered there, where the reviewer would read it, until the
    operator has moved it out; nothing is discarded."""
    gh = FakeGitHub()
    pushes = count_pushes(monkeypatch)
    eng = in_fix(tmp_state_dir, gh, _fixer_leaving(lambda cwd: None))
    reviewed = eng.state.reviewed_head_sha
    _power_loss_at(eng, EffectKind.PUSH, Stage.ATTEMPTED, saved=False)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    candidate = _worktree_head(eng.provider.calls[0])
    gh.prs[PR].base_ref = "release/1.x"
    again = _resumed(eng, tmp_state_dir, gh, "never")
    out = again.step()
    assert out.next_phase == "BLOCKED", out.message
    assert pushes == [] and again.origin.head(BRANCH) == reviewed
    assert load_state(again.paths.state_file).partial_fix == {}
    worktree = again.agent_worktree_path()
    raw = again.paths.state_file.read_text()
    refused = again.unblock("the operator set the secret")
    assert not refused.unblocked, refused.message
    assert (
        f"is at {candidate[:12]}, not at the PR's HEAD {reviewed[:12]}, so it may hold commits "
        "the PR does not"
    ) in refused.message
    assert f"with the worktree detached at {reviewed[:12]}" in refused.message
    assert again.paths.state_file.read_text() == raw
    assert git_out("-C", str(worktree), "rev-parse", "HEAD") == candidate
    git_out("-C", str(worktree), "checkout", "-q", "--detach", reviewed)
    applied = again.unblock("the operator kept the fixer's commit elsewhere")
    assert applied.unblocked and applied.phase == "REVIEW", applied.message


@pytest.mark.parametrize(("leave", "needle"), LEFT_BEHIND, ids=LEFT_BEHIND_IDS)
def test_a_plain_block_pushed_by_hand_keeps_the_worktree_check(tmp_state_dir, leave, needle):
    """No partial fix at all: the fixer committed, left work behind and
    reported a plain block, and the operator pushed exactly its commit by hand.
    The REVIEW the plain unblock enters is held to the same checks."""

    def blocks(req) -> str:
        _fixer_leaving(leave)(req)
        return plain_blocked(req)

    gh = FakeGitHub()
    eng = in_fix(tmp_state_dir, gh, blocks)
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    candidate = _worktree_head(eng.provider.calls[0])
    eng.origin.publish(eng.workdir, candidate, BRANCH)
    gh.prs[PR].head_sha = candidate
    assert load_state(eng.paths.state_file).partial_fix == {}
    _refuses_until_moved_out(eng, leave, needle, "the PR's HEAD", candidate)


# -- the persisted state (protocol 17) ----------------------------------------------------


def test_the_stored_message_bound_is_the_block_reason_bound():
    assert effects_module.MAX_STORED_FIX_MESSAGE_CHARS == state_module.MAX_BLOCK_REASON_CHARS


def _partial_state(**overrides) -> AutoForgeState:
    record = {
        "round": 1,
        "pr_url": PR,
        "reviewed_head_sha": A,
        "candidate_sha": B,
        "published_by": "controller",
        "resolved": ["R1-F1"],
        "unresolved": [stored("R1-F2")],
    }
    fields = dict(
        phase=Phase.BLOCKED,
        block_reason="partial",
        stop_code="partial-fix",
        current_pr_url=PR,
        current_head_sha=B,
        reviewed_pr_url=PR,
        reviewed_head_sha=A,
        review_round=1,
        last_review_comment_url=comment_url(PR, 100),
        open_findings=[_finding(1, 1), _finding(1, 2)],
        partial_fix=record,
    )
    fields.update(overrides)
    return make_state(**fields)


def test_a_partial_fix_record_round_trips(tmp_path):
    p = tmp_path / "state.json"
    save_state(_partial_state(), p)
    assert load_state(p).partial_fix["candidate_sha"] == B


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        ({"phase": Phase.READY_FOR_MERGE, "stop_code": "", "block_reason": ""}, "is set in phase"),
        ({"review_round": 2}, ".round must be the round being fixed"),
        ({"reviewed_head_sha": "c" * 40}, ".reviewed_head_sha must be"),
        ({"open_findings": [_finding(1, 1)]}, "must name exactly the findings"),
    ],
)
def test_a_partial_fix_record_out_of_its_binding_is_refused(tmp_path, change, needle):
    p = tmp_path / "state.json"
    data = _partial_state(**change).to_dict()
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateError, match="partial_fix") as exc:
        load_state(p)
    assert needle in str(exc.value)


@pytest.mark.parametrize(
    ("key", "value", "needle"),
    [
        ("candidate_sha", A, "other than the reviewed HEAD"),
        ("candidate_sha", "B" * 40, "full SHA"),
        ("published_by", "agent", "published_by"),
        ("resolved", [], "resolved must be"),
        ("unresolved", [{"finding_id": "R1-F2"}], "exactly a finding_id and a reason"),
        ("unresolved", [stored("R1-F1")], "names a finding twice"),
        ("extra", 1, "exactly the keys"),
    ],
)
def test_a_malformed_partial_fix_record_is_refused(tmp_path, key, value, needle):
    p = tmp_path / "state.json"
    data = _partial_state().to_dict()
    data["partial_fix"][key] = value
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert needle in str(exc.value)


def _protocol_16(data: dict) -> dict:
    data = dict(data, protocol_version="16")
    data.pop("partial_fix", None)
    context = data.get("completion_context")
    if isinstance(context, dict):
        context.pop("unresolved", None)
        context.pop("message", None)
    return data


def test_a_protocol_16_file_loads_with_no_partial_fix_and_is_relabelled(tmp_path):
    p = tmp_path / "state.json"
    data = _protocol_16(make_state(phase=Phase.REVIEW, current_pr_url=PR).to_dict())
    p.write_text(json.dumps(data), encoding="utf-8")
    loaded = load_state(p)
    assert loaded.protocol_version == "17" and loaded.partial_fix == {}
    save_state(loaded, p)
    raw = json.loads(p.read_text())
    assert raw["protocol_version"] == "17" and raw["partial_fix"] == {}


def test_a_protocol_16_fix_plan_loads_as_a_plan_that_resolves_every_finding(tmp_state_dir):
    """A FIX plan an older controller saved gains no unresolved finding and no
    message: it completes as the full fix it was."""
    gh = FakeGitHub()
    eng = in_fix(tmp_state_dir, gh, scripted(lambda req: fix_and_report(req)))
    _power_loss_at(eng, EffectKind.PUSH, Stage.ATTEMPTED, saved=False)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    data = _protocol_16(json.loads(eng.paths.state_file.read_text()))
    eng.paths.state_file.write_text(json.dumps(data), encoding="utf-8")
    again = _resumed(eng, tmp_state_dir, gh, "never")
    context = again.state.phase_effects().context
    assert context.unresolved == () and context.message == "" and not context.partial
    out = again.step()
    assert out.next_phase == "REVIEW", out.message


def fix_and_report(req) -> str:
    from tests.conftest import fix_payload

    reviewed = reviewed_head_in(req.prompt)
    head = fix_commit(req)
    return block(fix_payload(reviewed, head, [fixed(f["id"]) for f in FINDINGS]))


@pytest.mark.parametrize("where", ["field", "context"])
def test_an_older_file_carrying_partial_fix_state_is_refused(tmp_path, where):
    p = tmp_path / "state.json"
    data = _protocol_16(_partial_state().to_dict())
    if where == "field":
        data["partial_fix"] = {}
    else:
        data["completion_context"] = {"phase": "FIX", "unresolved": []}
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateError, match="labelled protocol_version '16', which never wrote"):
        load_state(p)


def test_a_protocol_17_file_without_the_field_is_refused(tmp_path):
    p = tmp_path / "state.json"
    data = make_state().to_dict()
    del data["partial_fix"]
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateError, match="'partial_fix'"):
        load_state(p)


def _saved_partial_plan(tmp_state_dir) -> tuple[FakeGitHub, object, dict]:
    gh = FakeGitHub()
    eng = in_fix(tmp_state_dir, gh, partial_fixer())
    _power_loss_at(eng, EffectKind.PUSH, Stage.ATTEMPTED, saved=False)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    return gh, eng, json.loads(eng.paths.state_file.read_text())


@pytest.mark.parametrize(
    ("damage", "needle"),
    [
        (lambda d: d["completion_context"].update(message=""), "is blank"),
        (
            lambda d: [
                r.update(
                    resolution="no_change_with_rationale",
                    rationale="x" * 60,
                    commit_sha="",
                )
                for r in d["completion_context"]["resolutions"]
            ],
            "resolve no finding as 'fixed'",
        ),
        (lambda d: d["completion_context"].update(unresolved=[]), "exactly the open findings"),
        (
            lambda d: d["completion_context"]["unresolved"][0].update(finding_id="R1-F1"),
            "names a finding the context resolves",
        ),
        (lambda d: d.update(effect_records=[]), "must end with the push of its candidate"),
    ],
    ids=["message", "kind", "coverage", "twice", "plan"],
)
def test_a_damaged_partial_plan_is_refused_on_load(tmp_state_dir, damage, needle):
    """The journal holds a partial fix to its shape: a message, a 'fixed'
    resolution, the open findings exactly once, and the push last."""
    _, eng, data = _saved_partial_plan(tmp_state_dir)
    damage(data)
    eng.paths.state_file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateError) as exc:
        load_state(eng.paths.state_file)
    assert needle in str(exc.value)
