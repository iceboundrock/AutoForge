"""A blocked FIX that publishes the commits it made for some findings (#433).

The fixer resolves what it can and reports ``blocked`` for a finding only a
human can act on, listing the ``fixed`` findings with its candidate and the
others as ``unresolved``. The controller checks that claim as it checks a
successful FIX, plans the push of the exact candidate (K1, the only record),
pushes and reads it back, and stops ``BLOCKED`` with every finding of the
round still open. Nothing else is published, nothing re-reviews or relaunches
on its own, and the commits are never discarded, reset or forced.
"""

from __future__ import annotations

import json

import pytest

from autoforge import effects as effects_module
from autoforge import state as state_module
from autoforge.effects import EffectKind, Stage
from autoforge.errors import ControlResultValidationError, StateError
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
    PR,
    FakeGitHub,
    block,
    comment_url,
    commit_in,
    fix_commit,
    fixed,
    git_out,
    no_change,
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


def unresolved(finding_id: str, reason: str = REASON) -> dict:
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


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        ({"unresolved": []}, "'unresolved' must be a non-empty list"),
        ({"unresolved": "R1-F2"}, "'unresolved' must be a non-empty list"),
        ({"resolutions": []}, "resolves no finding"),
        ({"head_sha": None}, "missing required field 'head_sha'"),
        ({"previous_head_sha": "abc"}, "must be a full git SHA"),
        ({"resolutions": [no_change("R1-F1")]}, "carries only 'fixed' resolutions"),
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
            "carries only 'fixed' resolutions",
        ),
        ({"unresolved": [unresolved("R1-F1")]}, "names a finding twice"),
        ({"unresolved": [{"finding_id": "R1-F2"}]}, "missing required field 'reason'"),
        ({"unresolved": [{"finding_id": "R1-F2", "reason": "   "}]}, "'reason'"),
        ({"unresolved": [{**unresolved("R1-F2"), "url": "x"}]}, "carries 'url'"),
        ({"unresolved": [unresolved("bogus")]}, "invalid finding_id"),
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
        "unresolved": [unresolved("R1-F3")],
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
    assert context["unresolved"] == [unresolved("R1-F3")]
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


def _with_an_acceptance_action(req) -> str:
    """A partial fix that also asks for a comment: a write that may wait for the operator."""
    reviewed = reviewed_head_in(req.prompt)
    head = fix_commit(req)
    action = {
        "finding_id": "R1-F2",
        "resolution": "acceptance_action",
        "rationale": "The finding asks for the evidence to be posted on the issue itself.",
        "acceptance_action": {"kind": "issue_comment", "body": "The deploy log is attached."},
    }
    return block(partial_payload(reviewed, head, [fixed("R1-F1"), action], []))


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
        (_with_an_acceptance_action, "carries only 'fixed' resolutions"),
    ],
    ids=[
        "head",
        "no-commit",
        "commit-sha",
        "coverage",
        "previous",
        "ancestry",
        "message",
        "acceptance-action",
    ],
)
def test_an_unverifiable_partial_fix_is_corrected_and_publishes_nothing(
    tmp_state_dir, monkeypatch, first, needle
):
    """Mismatched claims, candidate identity, ancestry or commit policy, or a
    resolution that would publish something else or wait for the operator: the
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
    applied = eng.unblock("the operator set the secret")
    assert applied.unblocked and applied.phase == "REVIEW"
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
    assert f"verified and published `{candidate}`" in req.prompt
    assert "for R1-F1, R1-F2" in req.prompt and "left R1-F3 unresolved" in req.prompt
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
        "controller's push): R1-F1, R1-F2 claimed fixed, R1-F3 left open"
    ) in out
    assert "partial-fix" in out


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
        "unresolved": [unresolved("R1-F2")],
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
        ("unresolved", [unresolved("R1-F1")], "names a finding twice"),
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
            lambda d: d["completion_context"]["resolutions"][0].update(
                resolution="no_change_with_rationale"
            ),
            "are 'fixed' only",
        ),
        (lambda d: d["completion_context"].update(unresolved=[]), "exactly the open findings"),
        (
            lambda d: d["completion_context"]["unresolved"][0].update(finding_id="R1-F1"),
            "names a finding the context resolves",
        ),
        (lambda d: d.update(effect_records=[]), "exactly the push of its candidate"),
    ],
    ids=["message", "kind", "coverage", "twice", "plan"],
)
def test_a_damaged_partial_plan_is_refused_on_load(tmp_state_dir, damage, needle):
    """The journal holds a partial fix to its shape: a message, only 'fixed'
    resolutions, the open findings exactly once, and the push alone."""
    _, eng, data = _saved_partial_plan(tmp_state_dir)
    damage(data)
    eng.paths.state_file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateError) as exc:
        load_state(eng.paths.state_file)
    assert needle in str(exc.value)
