"""#414 review round 2: a K11 with nothing left to send never strands its run.

A takeover that conflicted, or was written and then overtaken, is settled by a
fresh read of its PR and the journaled record: the operator's decision is on
record, so a marker that names this run is bound with nothing written, and a
PR no longer open is routed as if no takeover were journaled. No field
remembers an owner. The other findings of the round are here too: every stop
these paths print names a way out that works.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from autoforge.claims import render_implementation_marker
from autoforge.effects import EffectKind, Stage
from autoforge.engine import ControllerEngine
from autoforge.errors import GitHubError, GitHubUnavailableError
from autoforge.stop_classification import StopReasonCode
from autoforge.transitions import Phase
from tests.conftest import (
    PR,
    FakeGitHub,
    analyze_payload,
    block,
    commit_in,
    implementation_pr_body,
    make_engine,
)
from tests.test_engine import SHA_A, _in_ready, _next_process, _power_loss_at, _stopped_mid_plan
from tests.test_pr_owner_sweep import _origin_refs
from tests.test_replan import _park_at_hard_threshold, _replan_agent

OTHER_RUN = "af-20261031T040500Z-0a0b0c"
REASON = "the operator inspected the PR"
PLANS = pytest.mark.parametrize("phase", [Phase.REVIEW, Phase.FIX], ids=["review", "fix"])


def _edited(gh: FakeGitHub, body: str) -> None:
    """Someone edits the PR's body and keeps its marker: a bot appending a line is enough."""
    gh.prs[PR].body = body + "\nAppended by a bot.\n"


def _appended_conflict(tmp_path, phase: Phase, *, landed: bool, owner: str | None = OTHER_RUN):
    """A mid-plan run whose appended K11 stopped ``conflict`` because the body changed.

    ``landed``: the write landed before the edit, so the marker names this run
    and only the record does not know it. Otherwise nothing was written, and
    the marker still names the other run.
    """
    eng, gh, _sent = _stopped_mid_plan(tmp_path, phase, owner)
    assert eng.step().stop.code is StopReasonCode.PR_OWNER
    assert eng.unblock(REASON, take_over=PR).unblocked
    taken = eng.state.phase_effects().appended_takeover.body
    _edited(gh, taken if landed else implementation_pr_body(owner_run_id=owner))
    out = eng.step()
    assert out.next_phase == "BLOCKED" and out.stop.code is StopReasonCode.EFFECT_CONFLICT
    return eng, gh


def _alone_conflict(tmp_path, *, landed: bool):
    """A bound run with K11 alone (its REVIEW plan is not journaled), stopped ``conflict``."""
    gh = FakeGitHub()
    gh.add_pr(head_sha=SHA_A)
    eng = _in_ready(tmp_path / "state", gh)
    gh.prs[PR].body = implementation_pr_body(owner_run_id=None)
    assert eng.step(allow_merge=True).stop.code is StopReasonCode.PR_OWNER
    assert eng.unblock(REASON, take_over=PR).unblocked
    (record,) = eng.state.phase_effects().records
    _edited(gh, record.body if landed else implementation_pr_body(owner_run_id=None))
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED" and out.stop.code is StopReasonCode.EFFECT_CONFLICT
    return eng, gh


def _unbound_conflict(tmp_path, *, landed: bool):
    """A fresh run that took an open PR of its issue over, stopped ``conflict`` unbound."""
    gh = FakeGitHub()
    gh.add_pr(head_sha=SHA_A, body=implementation_pr_body(owner_run_id=OTHER_RUN))
    eng = make_engine(tmp_path / "state", [], github=gh)
    eng.state.phase = Phase.BLOCKED
    eng.state.block_reason = "an open PR carries the marker"
    eng._save()
    assert eng.unblock(REASON, take_over=PR).unblocked
    (record,) = eng.state.phase_effects().records
    _edited(gh, record.body if landed else implementation_pr_body(owner_run_id=OTHER_RUN))
    out = eng.step()
    assert out.next_phase == "BLOCKED" and out.stop.code is StopReasonCode.EFFECT_CONFLICT
    return eng, gh


# -- The marker already names this run: settled, with nothing written. ------------


@PLANS
@pytest.mark.parametrize("flag", ["plain", "take-over"])
def test_an_appended_takeover_that_landed_then_was_edited_is_settled(tmp_path, phase, flag):
    eng, gh = _appended_conflict(tmp_path, phase, landed=True)
    body = gh.prs[PR].body
    writes = list(gh.effect_writes)
    out = eng.unblock(REASON, **({"take_over": PR} if flag == "take-over" else {}))
    assert out.unblocked, out.message
    assert eng.state.phase is phase
    assert eng.state.phase_effects().appended_takeover.stage is Stage.OBSERVED
    resumed = _next_process(eng, gh)
    out = resumed.step()
    assert out.next_phase != "BLOCKED", out.message
    assert resumed.provider.calls == []  # the journaled plan completes: no agent again
    assert gh.prs[PR].body == body  # the body is left as the edit left it
    assert not [w for w in gh.effect_writes[len(writes) :] if w[0] == "write_pr_body"]
    assert all(
        r.kind is not EffectKind.OWNER_TAKEOVER for r in resumed.state.phase_effects().records
    )


@pytest.mark.parametrize("flag", ["plain", "take-over"])
def test_a_takeover_alone_that_landed_then_was_edited_binds_with_nothing_written(tmp_path, flag):
    eng, gh = _alone_conflict(tmp_path, landed=True)
    body = gh.prs[PR].body
    out = eng.unblock(REASON, **({"take_over": PR} if flag == "take-over" else {}))
    assert out.unblocked, out.message
    assert eng.step().next_phase == "REVIEW"
    assert gh.prs[PR].body == body
    assert eng.state.last_review_result == "stale"  # rebound: its old verdict is not current


@pytest.mark.parametrize("flag", ["plain", "take-over"])
def test_an_unbound_takeover_that_landed_then_was_edited_binds(tmp_path, flag):
    eng, gh = _unbound_conflict(tmp_path, landed=True)
    body = gh.prs[PR].body
    out = eng.unblock(REASON, **({"take_over": PR} if flag == "take-over" else {}))
    assert out.unblocked, out.message
    assert eng.step().next_phase == "REVIEW"
    assert eng.state.current_pr_url == PR and gh.prs[PR].body == body


@pytest.mark.parametrize("merged", [False, True], ids=["open", "merged"])
def test_a_dry_run_of_a_settled_takeover_changes_nothing(tmp_path, merged):
    eng, gh = _alone_conflict(tmp_path, landed=True)
    if merged:
        gh.prs[PR].state = "MERGED"
    raw = eng.paths.state_file.read_bytes()
    writes = list(gh.effect_writes)
    out = eng.unblock(REASON, dry_run=True)
    assert out.dry_run and "would re-enter" in out.message
    assert eng.paths.state_file.read_bytes() == raw and gh.effect_writes == writes


def test_a_settled_takeover_is_not_the_rereview_it_was_asked_for(tmp_path):
    eng, _gh = _appended_conflict(tmp_path, Phase.FIX, landed=True)
    out = eng.unblock(REASON, rereview=True)
    assert not out.unblocked and "--rereview" in out.message
    assert eng.state.phase is Phase.BLOCKED


def test_a_marker_naming_this_run_with_no_k11_journaled_hands_nothing_over(tmp_path):
    """The authority is the operator's journaled K11, never the public marker text (D9.3)."""
    gh = FakeGitHub()
    eng = make_engine(tmp_path / "state", [], github=gh)
    gh.add_pr(head_sha=SHA_A, body=implementation_pr_body(owner_run_id=eng.state.run_id))
    eng.state.phase = Phase.BLOCKED
    eng.state.block_reason = "an open PR carries the marker"
    eng._save()
    assert not eng.unblock(REASON, take_over=PR).unblocked
    assert not eng.unblock(REASON).unblocked


def test_a_settled_takeover_with_a_second_marked_pr_is_refused_with_the_reason(tmp_path):
    eng, gh = _alone_conflict(tmp_path, landed=True)
    other = "https://github.com/owner/repo/pull/77"
    gh.add_pr(url=other, head_sha=SHA_A, branch="elsewhere", body=gh.prs[PR].body)
    out = eng.unblock(REASON)
    assert not out.unblocked
    assert other in out.message and "close the stale" in out.message


def test_the_replanned_takeover_of_a_marker_naming_another_run_is_unchanged(tmp_path):
    eng, gh = _appended_conflict(tmp_path, Phase.REVIEW, landed=False)
    refused = eng.unblock(REASON)
    assert not refused.unblocked and "--take-over" in refused.message
    assert eng.unblock(REASON, take_over=PR).unblocked
    record = eng.state.phase_effects().appended_takeover
    assert record.stage is Stage.INTENDED
    assert eng.step().next_phase != "BLOCKED"
    assert gh.prs[PR].body == record.body


@pytest.mark.parametrize("flag", ["approve", "decline", "rereview"])
def test_another_flag_on_a_settled_takeover_says_the_takeover_comes_first(tmp_path, flag):
    """R3-F5: never "this run has none", when the plan may hold exactly that action."""
    eng, _gh = _appended_conflict(tmp_path, Phase.FIX, landed=True)
    kwargs = {
        "approve": {"approve_action": "A-0123456789abcdef"},
        "decline": {"decline_action": "A-0123456789abcdef"},
        "rereview": {"rereview": True},
    }[flag]
    out = eng.unblock(REASON, **kwargs)
    assert not out.unblocked
    assert "takeover journaled" in out.message and "this run has none" not in out.message
    assert "this run has no such FIX" not in out.message


def _bound_or_unbound(tmp_path, bound: bool):
    return (
        _alone_conflict(tmp_path, landed=True)
        if bound
        else _unbound_conflict(tmp_path, landed=True)
    )


@pytest.mark.parametrize("bound", [True, False], ids=["bound", "unbound"])
def test_take_over_of_another_pr_never_settles_this_one(tmp_path, bound):
    eng, gh = _bound_or_unbound(tmp_path, bound)
    other = "https://github.com/owner/repo/pull/77"
    gh.add_pr(url=other, head_sha=SHA_A, branch="elsewhere", body="No marker.")
    history = list(eng.state.unblock_history)
    out = eng.unblock(REASON, take_over=other)
    assert not out.unblocked, out.message
    assert eng.state.phase is Phase.BLOCKED and eng.state.unblock_history == history
    record = eng.state.phase_effects().records[-1]
    assert record.stage is Stage.CONFLICT and record.target["pr_url"] == PR


def test_an_unbound_settled_read_answered_with_another_pr_is_refused(tmp_path):
    eng, gh = _unbound_conflict(tmp_path, landed=True)
    real = gh.get_pr
    gh.get_pr = lambda url: replace(real(url), url="https://github.com/owner/repo/pull/99")
    out = eng.unblock(REASON)
    assert not out.unblocked and "another PR" in out.message
    assert eng.state.phase is Phase.BLOCKED


def test_an_unbound_settled_read_that_fails_is_refused_or_propagates(tmp_path):
    eng, gh = _unbound_conflict(tmp_path, landed=True)
    gh.get_pr_error = GitHubError("HTTP 403: forbidden")
    out = eng.unblock(REASON)
    assert not out.unblocked and "cannot be read" in out.message
    gh.get_pr_error = GitHubUnavailableError("connection reset")
    with pytest.raises(GitHubUnavailableError):
        eng.unblock(REASON)
    assert eng.state.phase is Phase.BLOCKED


def test_a_takeover_attempted_before_it_conflicted_keeps_its_attempts_when_settled(tmp_path):
    """The write landed, the process died before the read-back, and then the body was edited."""
    eng, gh, _sent = _stopped_mid_plan(tmp_path, Phase.REVIEW, OTHER_RUN)
    assert eng.step().stop.code is StopReasonCode.PR_OWNER
    assert eng.unblock(REASON, take_over=PR).unblocked
    record = eng.state.phase_effects().appended_takeover
    state = eng.state.effect_records
    state[record.position] = record.attempting().to_dict()
    eng._save()
    _edited(gh, record.body)
    assert eng.step().stop.code is StopReasonCode.EFFECT_CONFLICT
    assert eng.unblock(REASON).unblocked
    settled = eng.state.phase_effects().appended_takeover
    assert settled.stage is Stage.OBSERVED and settled.attempts == 1
    assert _next_process(eng, gh).step().next_phase != "BLOCKED"


def test_the_conflict_stop_names_the_plain_unblock_for_what_needs_no_new_takeover(tmp_path):
    eng, gh, _sent = _stopped_mid_plan(tmp_path, Phase.REVIEW, OTHER_RUN)
    eng.step()
    assert eng.unblock(REASON, take_over=PR).unblocked
    _edited(gh, implementation_pr_body(owner_run_id=OTHER_RUN))
    out = eng.step()
    assert "--take-over" in out.message and "'unblock' decides from the PR as it is" in out.message


# -- An unbound takeover whose marker is gone: one refusal, one route (R3-F1). -----


MARKERLESS = {
    "absent": "No marker any more.",
    "empty": "",
    "other-issue": "Another issue.\n"
    + render_implementation_marker("https://github.com/owner/repo/issues/9", OTHER_RUN),
}


@pytest.mark.parametrize("flag", ["plain", "take-over"])
@pytest.mark.parametrize("stage", ["conflict", "observed"])
@pytest.mark.parametrize("body", sorted(MARKERLESS))
def test_an_unbound_takeover_whose_marker_is_gone_states_one_route(tmp_path, body, stage, flag):
    eng, gh = _unbound_conflict(tmp_path, landed=False)
    if stage == "observed":
        (record,) = eng.state.phase_effects().records
        eng.state.effect_records = [record.reopened().observing({"url": PR}).to_dict()]
        eng._save()
    gh.prs[PR].body = MARKERLESS[body]
    gh.unbodied.discard(PR)
    out = eng.unblock(REASON, **({"take_over": PR} if flag == "take-over" else {}))
    assert not out.unblocked, out.message
    assert eng.state.phase is Phase.BLOCKED and eng.provider.calls == []
    assert "Repair the body" in out.message and "start a new run" in out.message
    assert "will launch" not in out.message


def test_an_unbound_takeover_whose_marker_is_repaired_is_taken_over_again(tmp_path):
    eng, gh = _unbound_conflict(tmp_path, landed=False)
    gh.prs[PR].body = "No marker any more."
    assert not eng.unblock(REASON).unblocked
    gh.prs[PR].body = implementation_pr_body(owner_run_id=None)
    assert eng.unblock(REASON, take_over=PR).unblocked
    assert eng.step().next_phase == "REVIEW"


# -- A PR no longer open: routed as if no takeover were journaled. ----------------


@pytest.mark.parametrize("landed", [True, False], ids=["written", "not-written"])
def test_a_bound_takeover_of_a_pr_merged_by_hand_reconciles_the_merge(tmp_path, landed):
    """K11 alone on a run with a clean review bound: the merge is counted as with no K11."""
    eng, gh = _alone_conflict(tmp_path, landed=landed)
    gh.prs[PR].state = "MERGED"
    out = eng.unblock(REASON)
    assert out.unblocked, out.message
    assert eng.state.phase is Phase.READY_FOR_MERGE
    assert eng.state.effect_records == []
    assert eng.step(allow_merge=True).next_phase != "BLOCKED"
    assert eng.provider.calls == [] and not [
        w for w in gh.effect_writes if w[0] == "write_pr_body" and w[2] != gh.prs[PR].body
    ]


@PLANS
def test_an_appended_takeover_of_a_pr_closed_by_hand_gets_the_ordinary_route(tmp_path, phase):
    eng, gh = _appended_conflict(tmp_path, phase, landed=False, owner=None)
    gh.prs[PR].state = "CLOSED"
    out = eng.unblock(REASON)
    assert not out.unblocked
    assert "Reopen the PR, or start a new run" in out.message and "--take-over" not in out.message


@PLANS
def test_an_appended_takeover_of_a_closed_pr_another_run_owns_is_left_to_that_run(tmp_path, phase):
    eng, gh = _appended_conflict(tmp_path, phase, landed=False)
    gh.prs[PR].state = "CLOSED"
    out = eng.unblock(REASON)
    assert not out.unblocked
    assert "that run's to reconcile" in out.message and "--take-over" not in out.message


def test_an_appended_takeover_of_a_pr_reopened_later_is_taken_over_again(tmp_path):
    eng, gh = _appended_conflict(tmp_path, Phase.REVIEW, landed=False)
    gh.prs[PR].state = "CLOSED"
    assert not eng.unblock(REASON).unblocked
    gh.prs[PR].state = "OPEN"
    assert eng.unblock(REASON, take_over=PR).unblocked
    assert eng.step().next_phase != "BLOCKED"


def test_a_written_takeover_whose_pr_closed_before_its_read_back_does_not_loop(tmp_path):
    """An observed K11 alone on a PR no longer open would otherwise stop the same way forever."""
    gh = FakeGitHub()
    gh.add_pr(head_sha=SHA_A)
    eng = _in_ready(tmp_path / "state", gh)
    gh.prs[PR].body = implementation_pr_body(owner_run_id=None)
    assert eng.step(allow_merge=True).stop.code is StopReasonCode.PR_OWNER
    assert eng.unblock(REASON, take_over=PR).unblocked
    (record,) = eng.state.phase_effects().records
    eng.state.effect_records = [record.attempting().observing({"url": PR}).to_dict()]
    eng._save()
    gh.prs[PR].state = "MERGED"
    assert eng.step(allow_merge=True).next_phase == "BLOCKED"
    assert eng.unblock(REASON).unblocked
    assert eng.state.phase is Phase.READY_FOR_MERGE and eng.state.effect_records == []


def test_the_audit_record_of_an_unblock_says_a_takeover_was_dropped(tmp_path):
    eng, gh = _alone_conflict(tmp_path, landed=True)
    gh.prs[PR].state = "MERGED"
    eng.state.block_reason = "something that names no takeover"
    eng._save()
    assert eng.unblock(REASON).unblocked
    detail = eng.state.unblock_history[-1]["detail"]
    assert "owner_takeover" in detail and "dropped" in detail


@pytest.mark.parametrize("stage", ["conflict", "observed"])
def test_an_unbound_takeover_of_a_merged_pr_refuses_another_flag_with_the_same_route(
    tmp_path, stage
):
    eng, gh = _unbound_conflict(tmp_path, landed=False)
    if stage == "observed":
        (record,) = eng.state.phase_effects().records
        eng.state.effect_records = [record.reopened().observing({"url": PR}).to_dict()]
        eng._save()
    gh.prs[PR].state = "MERGED"
    other = "https://github.com/owner/repo/pull/77"
    gh.add_pr(url=other, head_sha=SHA_A, branch="elsewhere", body="No marker.")
    out = eng.unblock(REASON, take_over=other)
    assert not out.unblocked and "Start a new run" in out.message


@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_take_over_of_a_bound_pr_no_longer_open_is_refused_and_names_the_plain_unblock(
    tmp_path, state
):
    eng, gh = _alone_conflict(tmp_path, landed=True)
    gh.prs[PR].state = state
    out = eng.unblock(REASON, take_over=PR)
    assert not out.unblocked
    assert f"it is {state}" in out.message and "unblock without the flag" in out.message
    assert "already names this run" not in out.message
    assert eng.state.phase_effects().records[0].stage is Stage.CONFLICT  # nothing was dropped


@pytest.mark.parametrize(
    "state,route",
    [("MERGED", "Start a new run"), ("CLOSED", "Reopen the PR, or start a new run")],
)
@pytest.mark.parametrize("flag", ["plain", "take-over"])
def test_an_unbound_takeover_of_a_pr_no_longer_open_states_the_route(tmp_path, state, route, flag):
    eng, gh = _unbound_conflict(tmp_path, landed=False)
    gh.prs[PR].state = state
    out = eng.unblock(REASON, **({"take_over": PR} if flag == "take-over" else {}))
    assert not out.unblocked
    assert state in out.message and route in out.message
    assert "run 'autoforge unblock --take-over" not in out.message
    assert eng.provider.calls == []


# -- R2-F2: the push of a saved K1+K2 plan reads the branch's PRs first. ----------


def _saved_k1_k2(tmp_path, gh: FakeGitHub, monkeypatch) -> ControllerEngine:
    """A run whose K1+K2 plan is saved with nothing sent, on a branch with no PR."""
    eng = make_engine(
        tmp_path / "state",
        lambda req: block(analyze_payload(commit_in(req.cwd))),
        github=gh,
        origin=True,
    )
    base = eng.origin.head("main")
    eng.origin.publish(eng.workdir, base, "autoforge/2")
    assert eng.step().next_phase == "ANALYZE_EXECUTE"

    def dies(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(ControllerEngine, "_complete_analyze", dies)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    monkeypatch.undo()
    assert [r.kind for r in eng.state.phase_effects().records] == [
        EffectKind.PUSH,
        EffectKind.IMPLEMENTATION_PR,
    ]
    return eng


def test_a_saved_push_stops_for_a_pr_another_run_owns_on_its_branch(tmp_path, monkeypatch):
    gh = FakeGitHub()
    eng = _saved_k1_k2(tmp_path, gh, monkeypatch)
    head = eng.origin.head("autoforge/2")
    gh.add_pr(
        head_sha=head, branch="autoforge/2", body=implementation_pr_body(owner_run_id=OTHER_RUN)
    )
    refs = _origin_refs(eng)
    out = eng.step()
    assert out.next_phase == "BLOCKED" and out.stop.code is StopReasonCode.PR_OWNER, out.message
    assert OTHER_RUN in out.message
    assert _origin_refs(eng) == refs
    assert not [w for w in gh.effect_writes if w[0] == "create_pull_request"]


def test_a_saved_push_is_not_stopped_by_a_pr_on_another_branch(tmp_path, monkeypatch):
    """A marked PR on another branch has no head this push moves: K2's reconcile decides."""
    gh = FakeGitHub()
    eng = _saved_k1_k2(tmp_path, gh, monkeypatch)
    head = eng.origin.head("autoforge/2")
    gh.add_pr(
        head_sha=head, branch="elsewhere", body=implementation_pr_body(owner_run_id=OTHER_RUN)
    )
    out = eng.step()
    assert not (out.stop is not None and out.stop.code is StopReasonCode.PR_OWNER), out.message


def test_a_saved_push_whose_listing_fails_conclusively_sends_nothing(tmp_path, monkeypatch):
    gh = FakeGitHub()
    eng = _saved_k1_k2(tmp_path, gh, monkeypatch)
    refs = _origin_refs(eng)

    def refuses(repo, branch):
        raise GitHubError("HTTP 403: forbidden")

    gh.list_prs_for_head = refuses
    out = eng.step()
    assert out.next_phase == "BLOCKED" and out.stop.code is StopReasonCode.GITHUB_READ_FAILED
    assert _origin_refs(eng) == refs


def test_the_stop_of_a_saved_push_names_the_route_that_works(tmp_path, monkeypatch):
    """R3-F2: closing the PR leaves K2's precondition unmet for good, so it is not offered."""
    gh = FakeGitHub()
    eng = _saved_k1_k2(tmp_path, gh, monkeypatch)
    head = eng.origin.head("autoforge/2")
    gh.add_pr(
        head_sha=head, branch="autoforge/2", body=implementation_pr_body(owner_run_id=OTHER_RUN)
    )
    out = eng.step()
    assert "close it" not in out.message
    assert "start a fresh run" in out.message and f"--take-over {PR}" in out.message


@pytest.mark.parametrize(
    "state,owner",
    [("OPEN", None), ("OPEN", "this-run"), ("OPEN", "no-marker"), ("CLOSED", OTHER_RUN)],
    ids=["ownerless", "this-run", "no-marker", "closed"],
)
def test_a_saved_push_never_moves_a_branch_its_pr_create_would_refuse(
    tmp_path, monkeypatch, state, owner
):
    """R3-F3: K2 requires no PR in any state on the branch, and K2 is sent only after K1, so
    a PR there while K1 is pending is not this plan's, and K2 would refuse it after the push."""
    gh = FakeGitHub()
    eng = _saved_k1_k2(tmp_path, gh, monkeypatch)
    head = eng.origin.head("autoforge/2")
    if owner == "no-marker":
        body = "A PR with no marker."
    else:
        named = eng.state.run_id if owner == "this-run" else owner
        body = implementation_pr_body(owner_run_id=named)
    gh.add_pr(head_sha=head, branch="autoforge/2", body=body, state=state)
    refs = _origin_refs(eng)
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop.code is StopReasonCode.UNJOURNALED_WRITE, out.message
    assert "start a fresh run" in out.message and PR in out.message
    assert _origin_refs(eng) == refs
    assert not [w for w in gh.effect_writes if w[0] == "create_pull_request"]


def test_the_read_before_the_push_stops_once_the_push_is_observed(tmp_path, monkeypatch):
    """K2 alone is left: its own reconcile judges the branch, as before."""
    gh = FakeGitHub()
    eng = _saved_k1_k2(tmp_path, gh, monkeypatch)
    _power_loss_at(eng, EffectKind.IMPLEMENTATION_PR, Stage.ATTEMPTED, saved=False)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    again = _next_process(eng, gh)
    assert again.state.phase_effects().records[0].stage is Stage.OBSERVED
    head = again.origin.head("autoforge/2")
    gh.add_pr(
        head_sha=head, branch="autoforge/2", body=implementation_pr_body(owner_run_id=OTHER_RUN)
    )
    out = again.step()
    assert out.stop.code is StopReasonCode.EFFECT_CONFLICT, out.message


# -- R2-F3: the K4 gate on a PR no longer open offers no takeover. ----------------


@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_the_review_comment_gate_offers_no_takeover_of_a_pr_no_longer_open(tmp_path, state):
    eng, gh, _sent = _stopped_mid_plan(tmp_path, Phase.REVIEW, None)
    gh.prs[PR].state = state
    out = eng.step()
    assert out.next_phase == "BLOCKED" and out.stop.code is StopReasonCode.PR_OWNER, out.message
    assert "--take-over" not in out.message
    assert state in out.message


@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_the_review_comment_gate_on_an_ownerless_pr_names_no_owner_and_the_unblock(tmp_path, state):
    """R3-F4: no run owns it, and 'unblock' routes it."""
    eng, gh, _sent = _stopped_mid_plan(tmp_path, Phase.REVIEW, None)
    gh.prs[PR].state = state
    out = eng.step()
    assert "that run's" not in out.message and "the run that owns it" not in out.message
    assert "'unblock' decides from the PR as it is" in out.message


def test_the_review_comment_gate_leaves_a_pr_another_run_owns_to_that_run(tmp_path):
    eng, gh, _sent = _stopped_mid_plan(tmp_path, Phase.REVIEW, OTHER_RUN)
    gh.prs[PR].state = "MERGED"
    out = eng.step()
    assert out.stop.code is StopReasonCode.PR_OWNER
    assert "that run's to reconcile" in out.message and "'unblock' decides" not in out.message


@pytest.mark.parametrize("answer", ["another-pr", "no-state"])
def test_the_review_comment_gate_answered_with_nothing_it_can_judge_sends_nothing(
    tmp_path, monkeypatch, answer
):
    """The same class as the replan's fence: a read that establishes nothing is no pass."""
    eng, gh, sent = _stopped_mid_plan(tmp_path, Phase.REVIEW, None)
    gh.prs[PR].body = implementation_pr_body(owner_run_id=eng.state.run_id)
    real, complete = gh.get_pr, eng._complete_review
    armed = {"on": False}

    def judged(url):
        pr = real(url)
        if not armed["on"]:
            return pr
        if answer == "another-pr":
            return replace(pr, url="https://github.com/owner/repo/pull/99")
        return replace(pr, state="")

    def completing(*args, **kwargs):
        armed["on"] = True
        return complete(*args, **kwargs)

    gh.get_pr = judged
    eng._complete_review = completing
    out = eng.step()
    assert out.next_phase == "BLOCKED" and out.stop.code is StopReasonCode.PR_OWNER, out.message
    assert gh.effect_writes == sent


# -- R2-F4: the replan's fence passes only an open source, read as itself. --------


@pytest.mark.parametrize("answer", ["another-pr", "no-state"])
def test_a_replan_fence_answered_with_nothing_it_can_judge_publishes_nothing(tmp_path, answer):
    gh = FakeGitHub()
    eng = _park_at_hard_threshold(tmp_path / "state", gh, _replan_agent(gh))
    eng._save()
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    refs = _origin_refs(eng)
    real = gh.get_pr
    armed = {"on": False}
    persist = eng._save

    def fenced(url):
        pr = real(url)
        if not armed["on"] or url != PR:
            return pr
        if answer == "another-pr":
            return replace(pr, url="https://github.com/owner/repo/pull/99")
        return replace(pr, state="")

    def save():
        persist()
        if eng.state.replan_transaction and eng.state.effect_records:
            armed["on"] = True

    eng._save = save
    gh.get_pr = fenced
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert not [w for w in gh.effect_writes if w[0] == "create_pull_request"]
    assert _origin_refs(eng) == refs, out.message


# -- R2-F5: the stop for a PR the run did not open names the route that exists. ---


def test_the_stop_for_a_pr_the_run_did_not_open_names_only_the_takeover(tmp_path):
    gh = FakeGitHub()
    eng = make_engine(tmp_path / "state", [], github=gh)
    text = eng._unjournaled_pr_text(PR)
    assert "--take-over" in text
    assert "marker to write" not in text and "names the marker" not in text
