"""A reply delivered before its launch failed (#479; ADR 0012 D12).

The rule (:mod:`autoforge.delivered_result`) and the block shape it reads
(:func:`autoforge.result_parser.reply_block_shape`) are held on their own
first. Then the engine, through provider-neutral fixtures: a scripted launch
that reports each typed end (an idle kill, a runtime kill, a failing exit)
after a delivered reply, which is accepted only with every fact and only by
the checks a clean exit's reply meets, never corrected, never relaunched
over, and never run again once accepted. Last, the real adapters: Claude
Code's ``stream-json`` (a fake ``claude``), Pi's RPC (a fake ``pi``) and
OpenCode's plain text, which types no reply and so never has one accepted.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from autoforge.auto_recovery import RecoveryCategory, RecoveryJournal
from autoforge.auto_recovery import Refusal as RecoveryRefusal
from autoforge.delivered_result import (
    ACCEPTABLE_LIMITS,
    REFUSAL_TEXT,
    SILENT_REFUSALS,
    EndFacts,
    KeptReply,
    ProcessEnd,
    Refusal,
    outcome_record,
    outcome_text,
    refusal,
    settled_record,
    settled_text,
)
from autoforge.errors import (
    ExecutionError,
    ExecutionTimeoutError,
    GitHubUnavailableError,
    ProviderUsageLimitError,
    StateError,
)
from autoforge.providers import AgentExecutionResult, ScriptedProvider
from autoforge.result_parser import BlockShape, reply_block_shape
from autoforge.state import load_state
from autoforge.transitions import Phase

from .conftest import ISSUE, PR, analyze_payload, block, commit_in, implement, make_engine

# -- the rule ------------------------------------------------------------------------

#: Every fact a reply needs to go to the checks, for a launch an idle kill ended.
TAKEN = EndFacts(
    process_end=ProcessEnd.KILLED,
    limit="idle",
    reply_delivered=True,
    blocks=BlockShape.ONE,
)


@pytest.mark.parametrize(
    "facts",
    [
        TAKEN,
        replace(TAKEN, limit="max_runtime"),
        replace(TAKEN, process_end=ProcessEnd.FAILED_EXIT, limit=""),
    ],
    ids=["idle-kill", "runtime-kill", "failed-exit"],
)
def test_a_reply_goes_to_the_checks_only_with_every_fact(facts):
    assert refusal(facts) is None
    assert ACCEPTABLE_LIMITS == {"idle", "max_runtime"}


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"interaction": True}, Refusal.INTERACTION),
        ({"provider_failed": True}, Refusal.PROVIDER_FAILURE),
        ({"limit": "loop"}, Refusal.LIMIT),
        ({"limit": ""}, Refusal.LIMIT),
        ({"may_have_survivor": True}, Refusal.SURVIVOR),
        ({"orphans_unchecked": True}, Refusal.ORPHANS_UNCHECKED),
        ({"reply_delivered": False}, Refusal.NO_REPLY),
        ({"blocks": BlockShape.NONE}, Refusal.NO_BLOCK),
        ({"blocks": BlockShape.INCOMPLETE}, Refusal.INCOMPLETE_BLOCK),
        ({"blocks": BlockShape.CONFLICTING}, Refusal.CONFLICTING_BLOCKS),
        ({"truncated": True}, Refusal.TRUNCATED),
    ],
    ids=lambda v: v.value if isinstance(v, Refusal) else None,
)
def test_each_missing_fact_refuses_the_reply(change, expected):
    assert refusal(replace(TAKEN, **change)) is expected


def test_a_failing_exit_names_no_limit_and_needs_none():
    """The limit check is a kill's: a failing exit has no limit to be wrong."""
    assert refusal(replace(TAKEN, process_end=ProcessEnd.FAILED_EXIT, limit="loop")) is None


def test_the_refusals_come_in_their_declared_order():
    """Everything wrong at once names the first in :class:`Refusal`'s order;
    putting each right in turn names the next."""
    facts = EndFacts(
        process_end=ProcessEnd.KILLED,
        limit="loop",
        reply_delivered=False,
        blocks=BlockShape.CONFLICTING,
        truncated=True,
        provider_failed=True,
        interaction=True,
        may_have_survivor=True,
        orphans_unchecked=True,
    )
    fixes = [
        ("interaction", False),
        ("provider_failed", False),
        ("limit", "idle"),
        ("may_have_survivor", False),
        ("orphans_unchecked", False),
        ("reply_delivered", True),
        ("blocks", BlockShape.ONE),
        ("truncated", False),
    ]
    seen = []
    for name, value in fixes:
        seen.append(refusal(facts))
        facts = replace(facts, **{name: value})
    assert refusal(facts) is None
    assert seen == [
        Refusal.INTERACTION,
        Refusal.PROVIDER_FAILURE,
        Refusal.LIMIT,
        Refusal.SURVIVOR,
        Refusal.ORPHANS_UNCHECKED,
        Refusal.NO_REPLY,
        Refusal.CONFLICTING_BLOCKS,
        Refusal.TRUNCATED,
    ]
    assert seen == [r for r in Refusal if r in seen], "the order the enum declares"


def test_every_refusal_has_its_text_and_the_record_holds_typed_values_only():
    assert set(REFUSAL_TEXT) == set(Refusal)
    assert SILENT_REFUSALS == {Refusal.INTERACTION, Refusal.PROVIDER_FAILURE}
    # R1-F4 of PR #481: the checks of the reply make it provisional, never accepted.
    provisional = outcome_record(TAKEN, None)
    assert provisional == {
        "outcome": "provisional",
        "process_end": "killed",
        "limit": "idle",
        "reply_delivered": True,
        "blocks": "one",
        "truncated": False,
    }
    refused = outcome_record(TAKEN, Refusal.CHECKS)
    assert refused["outcome"] == "refused" and refused["reason"] == "checks"
    settled = settled_record(
        TAKEN,
        accepted=False,
        reason="checkout-drift",
        launch_log="003-x-1",
        kept_across_restart=True,
    )
    assert settled == {
        **provisional,
        "outcome": "not-accepted",
        "reason": "checkout-drift",
        "launch_step": "003-x-1",
        "kept_across_restart": True,
    }
    accepted = settled_text(TAKEN, accepted=True, kept_across_restart=False)
    assert "it was accepted" in accepted and "#479" in accepted
    stopped = settled_text(TAKEN, accepted=False, kept_across_restart=False)
    assert "it was not accepted" in stopped and "applied it" not in stopped
    exited = replace(TAKEN, process_end=ProcessEnd.FAILED_EXIT, limit="")
    assert outcome_text(exited, Refusal.SURVIVOR) == (
        "The agent exited with a failing status; its output was not accepted as the step's "
        f"result (survivor: {REFUSAL_TEXT[Refusal.SURVIVOR]})."
    )


def test_a_reply_is_one_only_its_adapter_vouched_for_or_whose_output_holds_a_block():
    assert not EndFacts(ProcessEnd.KILLED).has_reply
    assert EndFacts(ProcessEnd.KILLED, reply_delivered=True).has_reply
    assert EndFacts(ProcessEnd.KILLED, blocks=BlockShape.CONFLICTING).has_reply


# -- the block shape -----------------------------------------------------------------

ONE = {"phase": "REVIEW", "status": "success"}
OTHER = {"phase": "REVIEW", "status": "failure"}


@pytest.mark.parametrize(
    ("text", "shape"),
    [
        (block(ONE), BlockShape.ONE),
        ("Done.\n" + block(ONE) + "\nThat is all.", BlockShape.ONE),
        (block(ONE) + block(ONE), BlockShape.ONE),
        ("", BlockShape.NONE),
        ("The suite is still running; I'll report back.", BlockShape.NONE),
        ("<<<CONTROL_RESULT>>>\n[1]\n<<<END_CONTROL_RESULT>>>", BlockShape.NONE),
        ("<<<CONTROL_RESULT>>>\n{not json\n<<<END_CONTROL_RESULT>>>", BlockShape.NONE),
        ('<<<CONTROL_RESULT>>>\n{"phase": "REVIEW"', BlockShape.NONE),
        (block(ONE) + '<<<CONTROL_RESULT>>>\n{"phase": "REVIEW"', BlockShape.INCOMPLETE),
        (block(OTHER) + block(ONE), BlockShape.CONFLICTING),
        (block("{ ... single JSON object ... }") + block(ONE), BlockShape.CONFLICTING),
        (block({"n": 1}) + block({"n": True}), BlockShape.CONFLICTING),
        (block({"n": 1}) + block({"n": 1.0}), BlockShape.CONFLICTING),
    ],
    ids=[
        "one",
        "prose-around-one",
        "repeated",
        "empty",
        "prose",
        "not-an-object",
        "not-json",
        "never-closed",
        "begun-after",
        "two-different",
        "quoted-schema",
        "one-and-true",
        "one-and-one-point-zero",
    ],
)
def test_a_reply_is_taken_only_when_its_blocks_say_one_thing(text, shape):
    assert reply_block_shape(text) is shape


# -- the engine, through provider-neutral fixtures -----------------------------------


class _Ends(ScriptedProvider):
    """One launch per outcome: ``(reply, facts)``. The reply is text, or a
    handler that does the agent's work and returns it (such as
    :func:`implement`); the facts are how the launch ended, as its adapter and
    executor would report them."""

    def __init__(self, *outcomes) -> None:
        super().__init__()
        self.outcomes = list(outcomes)

    def execute(self, req):
        self.calls.append(req)
        reply, facts = self.outcomes.pop(0)
        facts = dict(facts)
        exit_code = facts.pop("exit_code", -1 if facts.get("timed_out") else 0)
        return AgentExecutionResult(
            command=["x"],
            exit_code=exit_code,
            stdout=reply(req) if callable(reply) else reply,
            stderr="boom",
            started_at="t",
            finished_at="t",
            **facts,
        )


IDLE = {"timed_out": True, "timeout_limit": "idle", "reply_delivered": True}
RUNTIME = {"timed_out": True, "timeout_limit": "max_runtime", "reply_delivered": True}
EXITED = {"exit_code": 3, "reply_delivered": True}
ENDS = {"idle": IDLE, "runtime": RUNTIME, "exit": EXITED}


def _analyzing(tmp_state_dir, gh, *outcomes):
    eng = make_engine(tmp_state_dir, [], github=gh, origin=True)
    provider = _Ends(*outcomes)
    eng.providers._overrides = {"claude": provider, "opencode": provider}
    eng.provider = provider
    eng.state.phase = Phase.ANALYZE_EXECUTE
    return eng, provider


def _agent_step(eng):
    """The run log directory of the run's last agent step."""
    run_dir = eng.paths.logs_dir / eng.state.run_id
    # An agent step's directory holds its progress log; a recovery decision's does not.
    return sorted(p for p in run_dir.iterdir() if (p / "progress.log").exists())[-1]


def _execution(eng) -> dict:
    """The ``execution.json`` of the run's last agent step."""
    return json.loads((_agent_step(eng) / "execution.json").read_text(encoding="utf-8"))


def _settled(eng) -> dict:
    """The step's own record of what became of its provisional reply (R1-F4 of PR #481)."""
    run_dir = eng.paths.logs_dir / eng.state.run_id
    (step,) = [p for p in sorted(run_dir.iterdir()) if "-delivered-result-" in p.name][-1:]
    return json.loads((step / "execution.json").read_text(encoding="utf-8"))["delivered_result"]


def _decisions(eng):
    state = load_state(eng.paths.state_file)
    return RecoveryJournal.from_dict(state.auto_recovery, state.run_id).decisions


@pytest.mark.parametrize("end", ENDS, ids=list(ENDS))
def test_a_reply_delivered_before_a_failed_end_is_accepted_and_the_step_goes_on(
    tmp_state_dir, fake_github, end
):
    """The #285 shape, with the proof: a complete reply the adapter vouches
    for, then an idle kill, a runtime kill or a failing exit, with nothing
    left running. The reply passes the checks a clean exit's would, and the
    step goes on from it: the PR is opened once, nothing is relaunched or
    decided, and the run log keeps how the launch ended."""
    eng, provider = _analyzing(tmp_state_dir, fake_github, (implement, ENDS[end]))
    (outcome,) = eng.run(max_steps=1)

    assert outcome.next_phase == "REVIEW", outcome.message
    assert outcome.message.startswith("The agent ") and "(#479)" in outcome.message
    assert len(provider.calls) == 1
    state = load_state(eng.paths.state_file)
    assert state.phase == Phase.REVIEW and state.current_pr_url == PR
    assert state.step_count == 1 and state.auto_recovery == {}
    assert [w[0] for w in fake_github.effect_writes] == ["create_pull_request"]
    execution = _execution(eng)
    assert execution["error"] == ""
    assert execution["timed_out"] is (end != "exit")
    facts = {
        "process_end": "failed-exit" if end == "exit" else "killed",
        "limit": {"idle": "idle", "runtime": "max_runtime", "exit": ""}[end],
        "reply_delivered": True,
        "blocks": "one",
        "truncated": False,
    }
    # R1-F4 of PR #481: provisional at the launch, accepted by the step.
    assert execution["delivered_result"] == {"outcome": "provisional", **facts}
    assert _settled(eng) == {
        "outcome": "accepted",
        **facts,
        "launch_step": _agent_step(eng).name,
        "kept_across_restart": False,
    }
    assert "it was accepted" in outcome.message
    assert state.delivered_reply == {}


def test_a_clean_exit_records_no_delivered_result(tmp_state_dir, fake_github):
    """Nothing changes for a launch that ended well: its reply is read as always."""
    eng, _ = _analyzing(tmp_state_dir, fake_github, (implement, {"reply_delivered": True}))
    (outcome,) = eng.run(max_steps=1)
    assert outcome.next_phase == "REVIEW" and "#479" not in outcome.message
    assert "delivered_result" not in _execution(eng)


def _drifted(req) -> str:
    """The reply names a commit, then the agent commits again before the kill."""
    reported = block(analyze_payload(commit_in(req.cwd)))
    commit_in(req.cwd)
    return reported


def _other_issue(req) -> str:
    return implement(req, issue_url="https://github.com/owner/repo/issues/9")


@pytest.mark.parametrize(
    ("reply", "facts", "reason", "recovery"),
    [
        # The #285 example as #434 left it: a block, but no adapter fact.
        (implement, {**IDLE, "reply_delivered": False}, "no-reply", "result-delivered"),
        (implement, {**IDLE, "group_survived_kill": True}, "survivor", "process-may-survive"),
        (implement, {**IDLE, "capture_abandoned": True}, "survivor", "process-may-survive"),
        (implement, {**IDLE, "orphan_survived_kill": True}, "survivor", "process-may-survive"),
        (
            implement,
            {**IDLE, "orphans_unchecked": True},
            "orphans-unchecked",
            "process-may-survive",
        ),
        (implement, {**IDLE, "timeout_limit": "loop"}, "limit", "not-allowlisted"),
        (implement, {**IDLE, "timeout_limit": ""}, "limit", "not-allowlisted"),
        (implement, {**IDLE, "stdout_truncated": True}, "truncated", "result-delivered"),
        (
            lambda req: block(analyze_payload("a" * 40)) + implement(req),
            IDLE,
            "conflicting-blocks",
            "result-delivered",
        ),
        (
            lambda req: implement(req) + "<<<CONTROL_RESULT>>>\n{",
            IDLE,
            "incomplete-block",
            "result-delivered",
        ),
        # The checks a clean exit's reply meets: a malformed result, another
        # issue's (wrong invocation), a candidate that moved after the reply.
        (
            block({"phase": "ANALYZE_EXECUTE", "status": "success"}),
            IDLE,
            "checks",
            "result-delivered",
        ),
        (
            block({**analyze_payload("a" * 40), "phase": "REVIEW"}),
            IDLE,
            "checks",
            "result-delivered",
        ),
        (_other_issue, IDLE, "checks", "result-delivered"),
        (_drifted, IDLE, "checks", "result-delivered"),
        (_drifted, EXITED, "checks", "not-allowlisted"),
    ],
    ids=[
        "not-vouched-for",
        "group-survived",
        "capture-abandoned",
        "orphan-survived",
        "orphans-unchecked",
        "loop-kill",
        "unnamed-limit",
        "truncated",
        "conflicting",
        "incomplete",
        "malformed",
        "wrong-phase",
        "wrong-issue",
        "candidate-drift",
        "candidate-drift-after-exit",
    ],
)
def test_a_reply_without_its_proof_is_never_accepted_and_the_step_stops_safely(
    tmp_state_dir, fake_github, reply, facts, reason, recovery
):
    """Each missing fact, and each refusal of the checks, keeps the launch's
    failure as the step's outcome: nothing is published, nothing corrects
    the reply, and the recovery rule refuses to relaunch over it (or over the
    process that may still run). The run log and the decision say why."""
    eng, provider = _analyzing(tmp_state_dir, fake_github, (reply, facts), (implement, {}))
    error = ExecutionTimeoutError if facts.get("timed_out") else ExecutionError
    with pytest.raises(error) as caught:
        eng.run(max_steps=5)

    message = str(caught.value)
    assert f"relaunch refused ({recovery})" in message
    assert f"its output was not accepted as the step's result ({reason}: " in message
    assert len(provider.calls) == 1, "no correction and no relaunch"
    assert fake_github.effect_writes == [] or all(
        w[0] == "create_issue_comment" for w in fake_github.effect_writes
    ), "nothing but the recovery audit was written"
    state = load_state(eng.paths.state_file)
    assert state.phase == Phase.ANALYZE_EXECUTE and state.current_pr_url in ("", None)
    record = _execution(eng)["delivered_result"]
    assert (record["outcome"], record["reason"]) == ("refused", reason)
    assert record["process_end"] == ("killed" if facts.get("timed_out") else "failed-exit")
    (decision,) = _decisions(eng)
    assert decision.refusal is RecoveryRefusal(recovery)
    assert f"not accepted ({reason})" in decision.evidence


def test_a_reply_refused_by_its_checks_says_why_in_the_run_log(tmp_state_dir, fake_github):
    """The checks' own error goes to the record beside the launch's failure."""
    payload = block({"phase": "ANALYZE_EXECUTE", "status": "success"})
    eng, _ = _analyzing(tmp_state_dir, fake_github, (payload, IDLE))
    with pytest.raises(ExecutionTimeoutError):
        eng.step()
    error = _execution(eng)["error"]
    assert error.startswith("made no progress for 900s")
    assert "; its reply was not accepted: ControlResultValidationError: " in error


@pytest.mark.parametrize(
    ("facts", "error"),
    [
        (
            {**EXITED, "provider_failure": "pi: the RPC conversation failed the run"},
            ExecutionError,
        ),
        (
            {**IDLE, "failed_before_limit": "claude: API error 401"},
            ExecutionTimeoutError,
        ),
        (
            {**IDLE, "usage_limit": "claude: API error 429"},
            ProviderUsageLimitError,
        ),
    ],
    ids=["provider-failure", "failed-before-limit", "usage-limit"],
)
def test_a_failure_the_provider_established_keeps_its_own_words(
    tmp_state_dir, fake_github, facts, error
):
    """A run the provider's protocol had failed has no reply in question: the
    error says what it said before #479, and the run log records the refusal."""
    eng, provider = _analyzing(tmp_state_dir, fake_github, (implement, facts))
    with pytest.raises(error) as caught:
        eng.step()
    assert "not accepted as the step's result" not in str(caught.value)
    assert _execution(eng)["delivered_result"]["reason"] == "provider-failure"
    assert len(provider.calls) == 1


def test_a_failed_launch_with_nothing_delivered_records_nothing_about_a_reply(
    tmp_state_dir, fake_github
):
    eng, _ = _analyzing(tmp_state_dir, fake_github, ("", {**IDLE, "reply_delivered": False}))
    with pytest.raises(ExecutionTimeoutError) as caught:
        eng.step()
    assert "not accepted" not in str(caught.value)
    assert "delivered_result" not in _execution(eng)


def test_an_accepted_reply_is_never_run_again_after_a_restart(tmp_state_dir, fake_github):
    """Accepted after an idle kill, the reply plans the push and the PR, and
    the PR create fails before reaching GitHub. A new controller process
    completes the plan from the journal, once, and launches no agent: the
    result was durable from the phase's first save, as any result is."""
    from .test_engine import _resumed

    fake_github.write_failures.append(
        ("create_pull_request", GitHubUnavailableError("gh: HTTP 502"), False)
    )
    eng, provider = _analyzing(tmp_state_dir, fake_github, (implement, IDLE))
    with pytest.raises(GitHubUnavailableError):
        eng.step()
    assert len(provider.calls) == 1
    candidate = eng.origin.head("autoforge/2")
    assert load_state(eng.paths.state_file).phase == Phase.ANALYZE_EXECUTE

    eng2 = _resumed(eng, tmp_state_dir, fake_github)
    later = _Ends()
    eng2.providers._overrides = {"claude": later, "opencode": later}
    eng2.provider = later
    outcome = eng2.step()
    assert outcome.next_phase == "REVIEW", outcome.message
    assert later.calls == [], "the accepted result was not run again"
    assert eng2.state.current_pr_url == PR and eng2.state.current_head_sha == candidate
    assert [w[0] for w in fake_github.effect_writes] == ["create_pull_request"] * 2
    assert len([p for p in fake_github.prs.values() if ISSUE in p.body]) == 1


def test_an_accepted_reply_whose_pr_create_landed_is_adopted_never_sent_twice(
    tmp_state_dir, fake_github
):
    fake_github.write_failures.append(
        ("create_pull_request", GitHubUnavailableError("gh: HTTP 502"), True)
    )
    eng, provider = _analyzing(tmp_state_dir, fake_github, (implement, IDLE))
    outcome = eng.step()
    assert outcome.next_phase == "REVIEW", outcome.message
    assert len(provider.calls) == 1
    assert [w[0] for w in fake_github.effect_writes] == ["create_pull_request"]


# -- the kept reply across a crash (R1-F1 of PR #481) -------------------------------


class _Crash(BaseException):
    """The controller process dying (a kill, a closed laptop) at a chosen point."""


def _crash_before_the_plan(eng, monkeypatch) -> None:
    """Kill the controller after the reply was kept and logged, before the
    phase's first save: the window R1-F1 names."""

    def crash(*_):
        raise _Crash

    monkeypatch.setattr(eng, "_verify_and_apply", crash)


def _no_agent(eng):
    """A provider that records any launch, for an engine that must launch none."""
    later = _Ends()
    eng.providers._overrides = {"claude": later, "opencode": later}
    eng.provider = later
    return later


def test_a_provisional_reply_is_kept_in_the_state_before_the_step_goes_on_from_it(
    tmp_state_dir, fake_github, monkeypatch
):
    """The crash R1-F1 names: the reply passed the checks of a clean exit's
    reply and was logged, then the controller died before the phase's first
    save. The state on disk holds the reply with its binding, so the next
    process goes on from it: no agent is launched, the PR is opened once,
    and the step's record says it was accepted, kept across the restart."""
    from .test_engine import _resumed

    eng, provider = _analyzing(tmp_state_dir, fake_github, (implement, IDLE))
    _crash_before_the_plan(eng, monkeypatch)
    with pytest.raises(_Crash):
        eng.step()
    on_disk = load_state(eng.paths.state_file)
    kept = KeptReply.from_dict(on_disk.delivered_reply)
    assert (kept.phase, kept.attempt, kept.launch_label) == (
        "ANALYZE_EXECUTE",
        on_disk.attempt,
        on_disk.launch_label,
    )
    assert kept.issue_url == ISSUE and kept.process_end is ProcessEnd.KILLED
    assert kept.limit == "idle" and kept.payload["head_sha"] == kept.candidate
    assert kept.run_log == _agent_step(eng).name
    assert _execution(eng)["delivered_result"]["outcome"] == "provisional"
    assert on_disk.phase == Phase.ANALYZE_EXECUTE and on_disk.effect_records == []

    eng2 = _resumed(eng, tmp_state_dir, fake_github)
    later = _no_agent(eng2)
    outcome = eng2.step()
    assert outcome.next_phase == "REVIEW", outcome.message
    assert later.calls == [], "the kept reply was not run again"
    assert "kept in the run's state across an interruption" in outcome.message
    assert "it was accepted" in outcome.message
    assert eng2.state.current_head_sha == kept.candidate
    assert [w[0] for w in fake_github.effect_writes] == ["create_pull_request"]
    assert _settled(eng2)["kept_across_restart"] is True
    assert _settled(eng2)["outcome"] == "accepted"
    assert load_state(eng2.paths.state_file).delivered_reply == {}


def test_a_kept_review_is_posted_once_after_a_restart_and_the_reviewer_not_run_again(
    tmp_state_dir, fake_github, monkeypatch
):
    """Another phase, its own checks at the restart: the review names the
    HEAD and round the restart binds again, and the round's comment is
    posted once, by the process that goes on from the kept reply."""
    from .conftest import SHA_A, controller_review_comment, review_result
    from .test_engine import _in_review

    eng = _in_review(tmp_state_dir, fake_github, [])
    provider = _Ends((block(review_result(1, SHA_A, [])), IDLE))
    eng.providers._overrides = {"claude": provider, "opencode": provider}
    eng.provider = provider
    _crash_before_the_plan(eng, monkeypatch)
    with pytest.raises(_Crash):
        eng.step()
    assert KeptReply.from_dict(load_state(eng.paths.state_file).delivered_reply).candidate == (
        SHA_A.lower()
    )
    eng.close()

    eng2 = make_engine(tmp_state_dir, [], github=fake_github)
    eng2.load()
    later = _no_agent(eng2)
    outcome = eng2.step()
    assert outcome.next_phase == "READY_FOR_MERGE", outcome.message
    assert later.calls == [] and len(provider.calls) == 1
    assert controller_review_comment(eng2, 1) is not None
    assert [w[0] for w in fake_github.effect_writes] == ["create_pr_comment"]


def test_a_kept_reply_whose_candidate_moved_before_the_restart_stops_safely(
    tmp_state_dir, fake_github, monkeypatch
):
    """Candidate revalidation: between the crash and the restart the worktree
    gained a commit, so the reply names a HEAD it no longer has. The restart
    re-checks it, refuses it, and stops in BLOCKED with nothing launched or
    published and the reply dropped; 'unblock' launches afresh."""
    from .test_engine import _resumed

    eng, provider = _analyzing(tmp_state_dir, fake_github, (implement, IDLE))
    _crash_before_the_plan(eng, monkeypatch)
    with pytest.raises(_Crash):
        eng.step()
    commit_in(provider.calls[0].cwd, "a commit after the reply")

    eng2 = _resumed(eng, tmp_state_dir, fake_github)
    later = _no_agent(eng2)
    outcome = eng2.step()
    assert outcome.next_phase == "BLOCKED", outcome.message
    assert later.calls == [] and fake_github.effect_writes == []
    state = load_state(eng2.paths.state_file)
    assert state.delivered_reply == {} and state.stop_code == "control-result-invalid"
    assert "no longer passes the checks of a clean exit's reply" in state.block_reason
    assert "'unblock'" in state.block_reason


def test_a_kept_reply_bound_to_another_launch_is_never_gone_on_from(
    tmp_state_dir, fake_github, monkeypatch
):
    """A binding that no longer matches the run (here, another review round
    recorded by hand) is a safe stop, never a reply taken for this entry."""
    from .test_engine import _resumed

    eng, _ = _analyzing(tmp_state_dir, fake_github, (implement, IDLE))
    _crash_before_the_plan(eng, monkeypatch)
    with pytest.raises(_Crash):
        eng.step()
    eng2 = _resumed(eng, tmp_state_dir, fake_github)
    eng2.state.delivered_reply = {**eng2.state.delivered_reply, "review_round": 4}
    eng2.save()
    later = _no_agent(eng2)
    outcome = eng2.step()
    assert outcome.next_phase == "BLOCKED" and later.calls == []
    assert "review round 4" in eng2.state.block_reason


def test_a_kept_reply_outlives_an_unavailable_github_and_resume_goes_on_from_it(
    tmp_state_dir, fake_github
):
    """GitHub stops answering while the step verifies the reply: the step
    ends without an outcome, the automatic recovery relaunches nothing, the
    error says the reply is kept, and the operator's resume goes on from it
    with no agent and one PR."""
    from .test_engine import _resumed

    def reply_then_outage(req):
        text = implement(req)
        fake_github.get_repo_error = GitHubUnavailableError("gh: HTTP 502")
        return text

    eng, provider = _analyzing(tmp_state_dir, fake_github, (reply_then_outage, IDLE))
    with pytest.raises(GitHubUnavailableError) as caught:
        eng.run(max_steps=5)
    assert "is kept in the run's state: 'resume' checks it again" in str(caught.value)
    assert len(provider.calls) == 1
    assert load_state(eng.paths.state_file).delivered_reply != {}

    fake_github.get_repo_error = None
    eng2 = _resumed(eng, tmp_state_dir, fake_github)
    later = _no_agent(eng2)
    outcome = eng2.step()
    assert outcome.next_phase == "REVIEW", outcome.message
    assert later.calls == []
    assert [w[0] for w in fake_github.effect_writes if w[0] != "create_issue_comment"] == [
        "create_pull_request"
    ]


def test_a_reply_whose_keeping_save_fails_is_still_recorded_and_never_gone_on_from(
    tmp_state_dir, fake_github, monkeypatch
):
    """The save that keeps the reply is refused (a full disk, say): the step
    ends with that failure, the launch is recorded in the run log as every
    launch is, and nothing goes on from a reply the state does not hold."""
    eng, _ = _analyzing(tmp_state_dir, fake_github, (implement, IDLE))
    save = eng._save

    def refusing_save():
        if eng.state.delivered_reply:
            raise StateError("no space left on device")
        save()

    monkeypatch.setattr(eng, "_save", refusing_save)
    with pytest.raises(StateError, match="no space left on device"):
        eng.step()
    assert (
        "its reply could not be kept in the run's state: StateError" in (_execution(eng)["error"])
    )
    assert load_state(eng.paths.state_file).delivered_reply == {}
    assert fake_github.effect_writes == []


def test_a_new_launch_and_a_phase_change_drop_a_kept_reply(tmp_state_dir, fake_github):
    """Every save settles it: another attempt, or another phase, is not the
    launch that delivered it."""
    eng, _ = _analyzing(tmp_state_dir, fake_github)
    state = eng.state
    state.attempt, state.launch_label = 1, "controller_publishes"
    kept = KeptReply(
        phase="ANALYZE_EXECUTE",
        attempt=1,
        launch_label="controller_publishes",
        issue_url=ISSUE,
        pr_url="",
        review_round=0,
        candidate="a" * 40,
        process_end=ProcessEnd.KILLED,
        limit="idle",
        run_log="001-analyze_execute-1",
        payload=analyze_payload("a" * 40),
    )
    state.delivered_reply = kept.to_dict()
    eng.save()
    assert load_state(eng.paths.state_file).delivered_reply == kept.to_dict()
    state.attempt = 2
    eng.save()
    assert load_state(eng.paths.state_file).delivered_reply == {}
    state.attempt, state.delivered_reply = 1, kept.to_dict()
    state.phase = Phase.BLOCKED
    eng.save()
    assert load_state(eng.paths.state_file).delivered_reply == {}


@pytest.mark.parametrize(
    "change",
    [
        {"payload_sha256": "0" * 64},
        {"phase": "MERGE"},
        {"attempt": 0},
        {"candidate": "A" * 40},
        {"process_end": "killed", "limit": ""},
        {"process_end": "failed-exit", "limit": "idle"},
        {"run_log": "../escape"},
        {"payload": {"phase": "REVIEW", "status": "success"}},
        {"extra": 1},
    ],
    ids=[
        "digest",
        "not-an-agent-phase",
        "no-attempt",
        "uppercase-sha",
        "kill-without-limit",
        "exit-with-limit",
        "run-log",
        "payload-of-another-phase",
        "extra-key",
    ],
)
def test_a_kept_reply_is_loaded_only_in_its_whole_shape(change):
    kept = KeptReply(
        phase="ANALYZE_EXECUTE",
        attempt=1,
        launch_label="controller_publishes",
        issue_url=ISSUE,
        pr_url="",
        review_round=0,
        candidate="a" * 40,
        process_end=ProcessEnd.KILLED,
        limit="idle",
        run_log="001-analyze_execute-1",
        payload=analyze_payload("a" * 40),
    )
    assert KeptReply.from_dict(kept.to_dict()) == kept
    with pytest.raises(StateError, match="state field 'delivered_reply'"):
        KeptReply.from_dict({**kept.to_dict(), **change})


def test_a_state_file_keeping_a_reply_of_another_launch_is_refused(tmp_state_dir, fake_github):
    eng, _ = _analyzing(tmp_state_dir, fake_github)
    eng.save()
    raw = json.loads(eng.paths.state_file.read_text())
    raw["attempt"], raw["launch_label"] = 2, "controller_publishes"
    raw["delivered_reply"] = KeptReply(
        phase="ANALYZE_EXECUTE",
        attempt=1,
        launch_label="controller_publishes",
        issue_url=ISSUE,
        pr_url="",
        review_round=0,
        candidate="",
        process_end=ProcessEnd.FAILED_EXIT,
        limit="",
        run_log="001-analyze_execute-1",
        payload=analyze_payload("a" * 40),
    ).to_dict()
    eng.paths.state_file.write_text(json.dumps(raw))
    with pytest.raises(StateError, match="is bound to ANALYZE_EXECUTE attempt 1"):
        load_state(eng.paths.state_file)


# -- acceptance only after the step's own checks (R1-F4 of PR #481) -----------------


def test_a_reply_the_checkout_drift_check_refuses_after_the_parse_is_never_accepted(
    tmp_state_dir, fake_github
):
    """A post-parse rejection: the reply passes the checks of a clean exit's
    reply, then the operator's checkout is found moved. The launch's record
    says provisional, the step's record says not accepted with the stop's
    code, the message claims no acceptance, and no reply is kept."""
    from .test_engine import _commit

    def reply_and_move_the_checkout(req):
        text = implement(req)
        _commit(tmp_state_dir.parent, "sneaky.txt", "1", "moved by the agent")
        return text

    eng, provider = _analyzing(tmp_state_dir, fake_github, (reply_and_move_the_checkout, IDLE))
    outcome = eng.step()
    assert outcome.next_phase == "BLOCKED", outcome.message
    assert outcome.message.startswith("The agent was killed at its idle limit")
    assert "it was not accepted" in outcome.message and "it was accepted" not in outcome.message
    assert _execution(eng)["delivered_result"]["outcome"] == "provisional"
    settled = _settled(eng)
    assert (settled["outcome"], settled["reason"]) == ("not-accepted", "checkout-drift")
    state = load_state(eng.paths.state_file)
    assert state.delivered_reply == {} and fake_github.effect_writes == []
    assert len(provider.calls) == 1


def test_a_reply_the_phase_verification_stops_before_planning_is_never_accepted(
    tmp_state_dir, fake_github, monkeypatch
):
    """The phase's verification stops before anything is planned (the
    producer gate's bound, here): not accepted, nothing published."""
    eng, _ = _analyzing(tmp_state_dir, fake_github, (implement, IDLE))
    monkeypatch.setattr(eng, "_gate_producer", lambda *_: "the candidate's validation failed")
    outcome = eng.step()
    assert outcome.next_phase == "BLOCKED", outcome.message
    assert "it was not accepted" in outcome.message
    assert _settled(eng)["outcome"] == "not-accepted"
    assert load_state(eng.paths.state_file).delivered_reply == {}
    assert fake_github.effect_writes == []


def _reviewing(tmp_state_dir, gh, reply):
    """A REVIEW of round 1 whose reviewer delivers ``reply`` and is then killed."""
    from .test_engine import _in_review

    eng = _in_review(tmp_state_dir, gh, [])
    provider = _Ends((reply, IDLE))
    eng.providers._overrides = {"claude": provider, "opencode": provider}
    eng.provider = provider
    return eng, provider


def test_a_review_applied_before_the_round_cap_stopped_the_step_was_accepted(
    tmp_state_dir, fake_github
):
    """R2-F4 of PR #481: with one review round allowed, the delivered review's
    comment is posted and its round recorded, then the cap stops the step on
    its finding. The review was applied, and the save of its plan says so,
    although the plan's completion dropped the plan before the stop."""
    from .conftest import SHA_A, controller_review_comment, review_result
    from .test_engine import _finding

    eng, provider = _reviewing(
        tmp_state_dir, fake_github, block(review_result(1, SHA_A, [_finding(1)]))
    )
    eng.config.workflow.max_review_rounds = 1
    outcome = eng.step()
    assert outcome.next_phase == "BLOCKED", outcome.message
    state = load_state(eng.paths.state_file)
    assert state.stop_code == "review-round-cap" and state.review_round == 1
    assert state.completion_context == {}, "the plan completed before the stop"
    assert controller_review_comment(eng, 1) is not None
    assert "it was accepted" in outcome.message and "not accepted" not in outcome.message
    assert _settled(eng) == {**_settled(eng), "outcome": "accepted"}
    assert "reason" not in _settled(eng)
    assert len(provider.calls) == 1


def test_a_review_refused_before_its_plan_was_saved_was_not_accepted(
    tmp_state_dir, fake_github, monkeypatch
):
    """Beside it, a genuine refusal before application, in the same phase and
    under the same cap: the PR's comments cannot be read before the round's
    comment would be posted, so nothing is planned, posted or recorded."""
    from autoforge.errors import GitHubError

    from .conftest import SHA_A, review_result
    from .test_engine import _finding

    def reply_then_unreadable_comments(req):
        def unreadable(*_args):
            raise GitHubError("gh: HTTP 403")

        monkeypatch.setattr(eng, "_review_comments", unreadable)
        return block(review_result(1, SHA_A, [_finding(1)]))

    eng, _ = _reviewing(tmp_state_dir, fake_github, reply_then_unreadable_comments)
    eng.config.workflow.max_review_rounds = 1
    outcome = eng.step()
    assert outcome.next_phase == "BLOCKED", outcome.message
    state = load_state(eng.paths.state_file)
    assert state.stop_code == "github-read-failed" and state.review_round == 0
    assert "it was not accepted" in outcome.message
    settled = _settled(eng)
    assert (settled["outcome"], settled["reason"]) == ("not-accepted", "github-read-failed")
    assert fake_github.effect_writes == []


def test_a_fix_whose_pr_moved_goes_to_review_without_accepting_the_reply(
    tmp_state_dir, fake_github
):
    """R2-F4 of PR #481: a phase that moves on without applying the reply is
    no acceptance either. The PR moved past the reviewed HEAD while the fixer
    ran, so FIX goes to REVIEW of the actual revision with no fix recorded."""
    from .conftest import SHA_B, fixed, fixer
    from .test_engine import _in_fix

    def fix_then_push(req):
        text = fixer(fixed("R1-F1"))(req)
        fake_github.prs[PR] = replace(fake_github.prs[PR], head_sha=SHA_B)
        return text

    eng = _in_fix(tmp_state_dir, fake_github, None, origin=True)
    provider = _Ends((fix_then_push, IDLE))
    eng.providers._overrides = {"claude": provider, "opencode": provider}
    eng.provider = provider
    outcome = eng.step()
    assert outcome.next_phase == "REVIEW", outcome.message
    assert "no fix recorded" in outcome.message and "it was not accepted" in outcome.message
    settled = _settled(eng)
    assert (settled["outcome"], settled["reason"]) == ("not-accepted", "REVIEW")
    assert load_state(eng.paths.state_file).last_fix_resolutions == []


def test_a_reply_the_producer_gate_refuses_below_its_bound_is_dropped_and_relaunched(
    tmp_state_dir, fake_github, monkeypatch
):
    """A conclusive validation failure below the bound: the step relaunches
    the agent at this entry next time, so the reply it refused is not kept
    for the next step to go on from."""
    from autoforge import engine as engine_module

    eng, provider = _analyzing(tmp_state_dir, fake_github, (implement, IDLE), (implement, {}))
    calls = []

    def gate(*_):
        calls.append(1)
        if len(calls) == 1:
            raise engine_module._ProducerValidationFailed("validation failed: 1 of 1")
        return ""

    monkeypatch.setattr(eng, "_gate_producer", gate)
    first = eng.step()
    assert first.next_phase == "ANALYZE_EXECUTE" and "it was not accepted" in first.message
    assert load_state(eng.paths.state_file).delivered_reply == {}
    second = eng.step()
    assert second.next_phase == "REVIEW", second.message
    assert len(provider.calls) == 2, "the next step launched the agent afresh"


def test_an_accepted_reply_after_a_kill_is_charged_one_step_and_decides_nothing(
    tmp_state_dir, fake_github
):
    """The bounds stay as they were: the launch was charged before it started,
    and accepting its reply launches nothing more, so a run with one step left
    completes the step, and the next one meets the budget with nothing launched."""
    eng, provider = _analyzing(tmp_state_dir, fake_github, (implement, IDLE))
    eng.config.workflow.max_total_steps = 1
    accepted, budget = eng.run(max_steps=5)
    assert accepted.next_phase == "REVIEW", accepted.message
    assert budget.next_phase == "BLOCKED" and "max_total_steps" in budget.message
    assert len(provider.calls) == 1
    state = load_state(eng.paths.state_file)
    assert state.step_count == 1 and state.auto_recovery == {}


def test_step_accepts_a_delivered_reply_as_run_does(tmp_state_dir, fake_github):
    """Accepting is no relaunch: ``step``, which never relaunches, accepts too."""
    eng, provider = _analyzing(tmp_state_dir, fake_github, (implement, IDLE))
    assert eng.step().next_phase == "REVIEW"
    assert len(provider.calls) == 1


def test_a_dry_run_launches_and_accepts_nothing(tmp_state_dir, fake_github):
    eng, provider = _analyzing(tmp_state_dir, fake_github, (implement, IDLE))
    eng.run(max_steps=2, dry_run=True)
    assert provider.calls == [] and fake_github.effect_writes == []


def test_a_review_delivered_before_an_idle_kill_is_accepted_and_posted_once(
    tmp_state_dir, fake_github
):
    """Another phase, its own checks: the reviewer's result names the bound
    HEAD and round, the controller posts the round's comment once."""
    from .conftest import SHA_A, controller_review_comment, review_result
    from .test_engine import _in_review

    eng = _in_review(tmp_state_dir, fake_github, [])
    provider = _Ends((block(review_result(1, SHA_A, [])), IDLE))
    eng.providers._overrides = {"claude": provider, "opencode": provider}
    eng.provider = provider
    outcome = eng.step()
    assert outcome.next_phase == "READY_FOR_MERGE", outcome.message
    assert "(#479)" in outcome.message and len(provider.calls) == 1
    assert controller_review_comment(eng, 1) is not None


def test_a_review_for_another_head_delivered_before_a_kill_is_never_accepted(
    tmp_state_dir, fake_github
):
    """A stale result: it reviews a HEAD the PR no longer has."""
    from .conftest import SHA_B, review_result
    from .test_engine import _in_review

    eng = _in_review(tmp_state_dir, fake_github, [])
    provider = _Ends((block(review_result(1, SHA_B, [])), IDLE))
    eng.providers._overrides = {"claude": provider, "opencode": provider}
    eng.provider = provider
    with pytest.raises(ExecutionTimeoutError, match=r"not accepted as the step's result \(checks"):
        eng.step()
    assert load_state(eng.paths.state_file).phase == Phase.REVIEW
    assert len(provider.calls) == 1


# -- the real adapters ---------------------------------------------------------------


@pytest.mark.parametrize("status", [0, 3])
def test_claude_vouches_for_a_reply_that_decided_its_run_whatever_its_exit(
    tmp_path, monkeypatch, status
):
    """Adapter level: the stream's ``replied``, read to its end, is the fact;
    the exit status after it does not change it."""
    from autoforge import providers

    from .test_providers import _claude_run, _pending_after, _reply

    monkeypatch.setattr(providers, "CLAUDE_REPLIED_EXIT_GRACE_SECONDS", 5.0)
    _, res = _claude_run(tmp_path, [*_pending_after(_reply()), {"sleep": 0.5}], exit_code=status)
    assert res.reply_delivered and res.failed_exit is bool(status) and not res.timed_out


def test_claude_vouches_for_no_reply_with_a_call_open_or_after_a_failure(tmp_path, monkeypatch):
    from autoforge import executor, providers
    from tests import claude_fake

    from .test_providers import _claude_run, _reply

    monkeypatch.setattr(providers, "CLAUDE_REPLIED_EXIT_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    call = claude_fake.tool_use("a1", "Agent", description="Run the suite")
    lines = [claude_fake.init(), call, claude_fake.result(_reply()), {"sleep": 60}]
    (tmp_path / "open").mkdir()
    _, res = _claude_run(tmp_path / "open", lines, timeout=30, idle=1)
    assert res.timed_out and not res.reply_delivered
    failed = [claude_fake.init(), claude_fake.result(_reply(), is_error=True)]
    (tmp_path / "failed").mkdir()
    _, res = _claude_run(tmp_path / "failed", failed, exit_code=1)
    assert res.provider_failure and not res.reply_delivered


def _reply_lines(text: str) -> list:
    from tests import claude_fake

    return [
        claude_fake.init(),
        claude_fake.tool_use("t1", "Bash", command="make check", description="Run checks"),
        claude_fake.tool_result("t1", "ok"),
        claude_fake.result(text),
    ]


def _claude(tmp_path, eng, lines, *, exit_code=0, idle=None):
    """Route the run's REVIEW to the real Claude adapter over the fake ``claude``."""
    from autoforge.profiles import profile_for_phase

    from .test_engine import _review_on_fake_claude

    home = _review_on_fake_claude(tmp_path, eng, lines, exit_code=exit_code)
    if idle is not None:
        profile = profile_for_phase(eng.config, Phase.REVIEW, eng.state.review_round)
        eng.config.profiles[profile.name] = replace(profile, idle_timeout_seconds=idle)
    return home


def test_a_claude_cli_that_exits_badly_after_its_reply_has_the_reply_accepted(
    tmp_path, tmp_state_dir, fake_github
):
    """Claude Code's ``stream-json``, adapter to engine: every turn closed, no
    call open, a success holding a block, then an exit 3. The adapter vouches
    for the reply, and the review it holds completes the step."""
    from tests import claude_fake

    from .conftest import SHA_A, review_result
    from .test_engine import _in_review

    eng = _in_review(tmp_state_dir, fake_github, [])
    lines = _reply_lines(block(review_result(1, SHA_A, [])))
    home = _claude(tmp_path, eng, lines, exit_code=3)
    outcome = eng.step()
    assert outcome.next_phase == "READY_FOR_MERGE", outcome.message
    assert "exited with a failing status after delivering" in outcome.message
    assert claude_fake.launches(home) == 1
    execution = _execution(eng)
    assert execution["exit_code"] == 3 and execution["ended_by"] == "exit"
    assert execution["delivered_result"]["outcome"] == "provisional"
    assert _settled(eng)["outcome"] == "accepted"


def test_a_claude_reply_with_a_call_still_open_is_never_accepted(
    tmp_path, tmp_state_dir, fake_github, monkeypatch
):
    """An open write-capable call (a background agent's, say) keeps the reply
    from deciding the run, so the idle kill finds no delivered reply: the
    block in the output is not accepted, and no relaunch goes over it."""
    from autoforge import executor
    from tests import claude_fake

    from .conftest import SHA_A, review_result
    from .test_engine import _in_review

    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    eng = _in_review(tmp_state_dir, fake_github, [])
    call = claude_fake.tool_use("a1", "Agent", description="Edit the files")
    reply = claude_fake.result(block(review_result(1, SHA_A, [])))
    home = _claude(tmp_path, eng, [claude_fake.init(), call, reply, {"sleep": 60}], idle=1)
    with pytest.raises(ExecutionTimeoutError) as caught:
        eng.run(max_steps=5)
    assert "(no-reply: " in str(caught.value)
    assert "relaunch refused (result-delivered)" in str(caught.value)
    assert claude_fake.launches(home) == 1
    assert load_state(eng.paths.state_file).phase == Phase.REVIEW


def test_a_claude_reply_overtaken_by_a_later_turn_is_stale_and_never_accepted(
    tmp_path, tmp_state_dir, fake_github, monkeypatch
):
    """A reply, then a new turn the CLI opens and never ends before the idle
    kill: the reply is no longer the run's last word."""
    from autoforge import executor, providers
    from tests import claude_fake

    from .conftest import SHA_A, review_result
    from .test_engine import _in_review

    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(providers, "CLAUDE_REPLIED_EXIT_GRACE_SECONDS", 5.0)
    eng = _in_review(tmp_state_dir, fake_github, [])
    lines = [
        *_reply_lines(block(review_result(1, SHA_A, []))),
        claude_fake.init(),
        claude_fake.tool_use("t2", "Edit", file_path="src/a.py"),
        {"sleep": 60},
    ]
    home = _claude(tmp_path, eng, lines, idle=1)
    with pytest.raises(ExecutionTimeoutError, match=r"\(no-reply: "):
        eng.step()
    assert claude_fake.launches(home) == 1
    assert load_state(eng.paths.state_file).phase == Phase.REVIEW


def _pi_engine(tmp_path_factory, tmp_state_dir, gh, turn, **profile_fields):
    """An ANALYZE_EXECUTE run whose implementer is the real Pi adapter over the
    fake ``pi``: it commits, then plays ``turn`` with the result as its text."""
    from tests.pi_fake import PiFake, make_pi_engine, route_to_pi

    fake = PiFake(tmp_path_factory.mktemp("pi"))

    def agent(req):
        return replace(turn, text=implement(req))

    eng = make_pi_engine(
        tmp_state_dir, fake, agent, github=gh, names=["analyze_execute"], origin=True
    )
    route_to_pi(eng.config, fake, ["analyze_execute"], **profile_fields)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    return eng, fake


def test_a_pi_that_hangs_on_shutdown_after_a_settled_run_has_its_reply_accepted(
    tmp_path_factory, tmp_state_dir, fake_github
):
    """Pi's RPC, adapter to engine: the conversation settled and its final
    text was read back, then Pi ignored the closed stdin until the idle limit
    killed it. The reply was delivered; the step goes on from it."""
    from tests.pi_fake import PiTurn

    eng, fake = _pi_engine(
        tmp_path_factory, tmp_state_dir, fake_github, PiTurn(linger=60), idle_timeout_seconds=2
    )
    (outcome,) = eng.run(max_steps=1)
    assert outcome.next_phase == "REVIEW", outcome.message
    assert "killed at its idle limit after delivering" in outcome.message
    assert len(fake.launches()) == 1
    (result,) = eng.pi.results
    assert result.timed_out and result.timeout_limit == "idle" and result.reply_delivered
    assert _execution(eng)["delivered_result"]["outcome"] == "provisional"
    assert _settled(eng)["outcome"] == "accepted"
    assert load_state(eng.paths.state_file).current_pr_url == PR


def test_a_pi_that_exits_badly_after_a_settled_run_has_its_reply_accepted(
    tmp_path_factory, tmp_state_dir, fake_github
):
    from tests.pi_fake import PiTurn

    eng, fake = _pi_engine(tmp_path_factory, tmp_state_dir, fake_github, PiTurn(close_exit=7))
    (outcome,) = eng.run(max_steps=1)
    assert outcome.next_phase == "REVIEW", outcome.message
    (result,) = eng.pi.results
    assert result.exit_code == 7 and result.failed_exit and result.reply_delivered


@pytest.mark.parametrize(
    ("ending", "error"),
    [({"close_exit": 7}, ExecutionError), ({"linger": 60}, ExecutionTimeoutError)],
    ids=["exit", "idle-kill"],
)
def test_a_pi_reply_with_a_tool_execution_left_open_is_never_accepted(
    tmp_path_factory, tmp_state_dir, fake_github, ending, error
):
    """R1-F3 of PR #481, adapter to engine: Pi reports a tool execution
    started and never ended, then settles with its final text and ends badly.
    Its protocol does not prove the reply the run's last word, so the adapter
    vouches for none: the failure stands, nothing is published, and no
    relaunch goes over the block in the output."""
    from tests.pi_fake import PiTurn

    eng, fake = _pi_engine(
        tmp_path_factory,
        tmp_state_dir,
        fake_github,
        PiTurn(open_call="call-1", **ending),
        idle_timeout_seconds=2,
    )
    with pytest.raises(error) as caught:
        eng.run(max_steps=5)
    assert "(no-reply: " in str(caught.value)
    assert len(fake.launches()) == 1
    (result,) = eng.pi.results
    assert result.stdout and not result.reply_delivered
    assert result.provider_summary["open_tool_executions"] == 1
    assert _execution(eng)["delivered_result"]["reason"] == "no-reply"
    state = load_state(eng.paths.state_file)
    assert state.phase == Phase.ANALYZE_EXECUTE and state.current_pr_url in ("", None)
    assert fake_github.effect_writes == [] or all(
        w[0] == "create_issue_comment" for w in fake_github.effect_writes
    )


#: A later turn Pi opens while it shuts down: it starts a write-capable tool.
_LATER_TURN = [
    {"type": "agent_start"},
    {"type": "turn_start"},
    {"type": "tool_execution_start", "toolCallId": "later", "toolName": "edit", "args": {}},
]


@pytest.mark.parametrize(
    ("ending", "error"),
    [({"close_exit": 7}, ExecutionError), ({"linger": 60}, ExecutionTimeoutError)],
    ids=["exit", "idle-kill"],
)
def test_a_pi_reply_overtaken_by_a_later_turn_in_its_shutdown_is_never_accepted(
    tmp_path_factory, tmp_state_dir, fake_github, ending, error
):
    """R1-F3 of PR #481, adapter to engine: the conversation settled and its
    text was read back, then Pi, its stdin closed, opened another turn before
    it exited badly or a limit killed it. The adapter reads stdout through
    the shutdown, so the reply is no longer the run's last word."""
    from tests.pi_fake import PiTurn

    eng, fake = _pi_engine(
        tmp_path_factory,
        tmp_state_dir,
        fake_github,
        PiTurn(shutdown=_LATER_TURN, **ending),
        idle_timeout_seconds=2,
    )
    with pytest.raises(error) as caught:
        eng.run(max_steps=5)
    assert "(no-reply: " in str(caught.value)
    assert len(fake.launches()) == 1
    (result,) = eng.pi.results
    assert result.stdout and not result.reply_delivered
    assert result.provider_summary["records_after_settled"] == len(_LATER_TURN)
    assert load_state(eng.paths.state_file).phase == Phase.ANALYZE_EXECUTE


#: Records Pi may write while it shuts down whose kind cannot be known (R2-F3
#: of PR #481), as the fake's raw lines: hex of the bytes written.
_UNREADABLE_SHUTDOWN = {
    "no-type": b'{"toolCallId": "later", "toolName": "edit"}'.hex(),
    "array": b'[{"type": "turn_start"}]'.hex(),
    "not-json": b"shutting down".hex(),
    "not-utf8": b'{"type": "turn_start", "x": "\xff"}'.hex(),
}


@pytest.mark.parametrize(
    "record", list(_UNREADABLE_SHUTDOWN.values()), ids=list(_UNREADABLE_SHUTDOWN)
)
def test_a_pi_reply_followed_by_an_unreadable_shutdown_record_is_never_accepted(
    tmp_path_factory, tmp_state_dir, fake_github, record
):
    """R2-F3 of PR #481, adapter to engine: the conversation settled and its
    text was read back, then Pi wrote a record whose kind cannot be known (no
    'type', an array, not JSON, not UTF-8) and exited 7. It may have been a
    later turn, so the adapter vouches for no reply: the failure stands and
    nothing is published."""
    from tests.pi_fake import PiTurn

    eng, fake = _pi_engine(
        tmp_path_factory, tmp_state_dir, fake_github, PiTurn(shutdown=[record], close_exit=7)
    )
    with pytest.raises(ExecutionError) as caught:
        eng.run(max_steps=5)
    assert "(no-reply: " in str(caught.value)
    assert len(fake.launches()) == 1
    (result,) = eng.pi.results
    assert result.exit_code == 7 and result.stdout and not result.reply_delivered
    assert result.provider_summary["records_after_settled"] == 1
    state = load_state(eng.paths.state_file)
    assert state.phase == Phase.ANALYZE_EXECUTE and state.delivered_reply == {}
    assert all(w[0] == "create_issue_comment" for w in fake_github.effect_writes)


def test_a_pi_that_exits_cleanly_after_an_unreadable_record_keeps_its_reply_as_before(
    tmp_path_factory, tmp_state_dir, fake_github
):
    """Normal clean-exit handling is unchanged: the proof is read only for a
    launch that ended badly."""
    from tests.pi_fake import PiTurn

    eng, _ = _pi_engine(
        tmp_path_factory,
        tmp_state_dir,
        fake_github,
        PiTurn(shutdown=[_UNREADABLE_SHUTDOWN["no-type"]]),
    )
    (outcome,) = eng.run(max_steps=1)
    assert outcome.next_phase == "REVIEW", outcome.message
    (result,) = eng.pi.results
    assert not result.reply_delivered and not result.failed_exit and result.provider_failure is None


def test_a_pi_that_exits_cleanly_after_a_later_turn_keeps_its_reply_as_before(
    tmp_path_factory, tmp_state_dir, fake_github
):
    """The proof is only read for a launch that ended badly: a Pi that exits
    0 is read as it always was, whatever it wrote while shutting down."""
    from tests.pi_fake import PiTurn

    eng, _ = _pi_engine(tmp_path_factory, tmp_state_dir, fake_github, PiTurn(shutdown=_LATER_TURN))
    (outcome,) = eng.run(max_steps=1)
    assert outcome.next_phase == "REVIEW", outcome.message
    (result,) = eng.pi.results
    assert not result.reply_delivered and not result.failed_exit


def test_a_pi_cut_short_by_its_limit_delivered_no_reply(
    tmp_path_factory, tmp_state_dir, fake_github
):
    """A conversation the limit cut short never settled: no reply to take,
    and the timeout stands as before (an idle kill, relaunched by #434)."""
    from tests.pi_fake import PiTurn

    eng, fake = _pi_engine(
        tmp_path_factory, tmp_state_dir, fake_github, PiTurn(outcome="hang"), idle_timeout_seconds=1
    )
    with pytest.raises(ExecutionTimeoutError):
        eng.step()
    (result,) = eng.pi.results
    assert result.timed_out and not result.reply_delivered
    assert "delivered_result" not in _execution(eng)


def test_opencode_types_no_reply_so_a_block_before_its_kill_is_never_accepted(
    tmp_state_dir, fake_github
):
    """OpenCode's plain-text output shows no turn: a review it printed before
    an idle kill is not vouched for (unsupported evidence fails safely), and
    no relaunch goes over it."""
    from autoforge.executor import ExecutionResult
    from autoforge.providers import OpenCodeProvider

    from .conftest import SHA_A, review_result
    from .test_engine import _in_review

    eng = _in_review(tmp_state_dir, fake_github, [])
    calls = []

    def runner(req):
        calls.append(req)
        reply = block(review_result(1, SHA_A, []))
        return ExecutionResult(
            req.command, req.cwd, -1, reply, "", "t", "t", timed_out=True, timeout_limit="idle"
        )

    eng.providers._overrides["opencode"] = OpenCodeProvider(runner=runner)
    with pytest.raises(ExecutionTimeoutError) as caught:
        eng.run(max_steps=5)
    assert "(no-reply: " in str(caught.value)
    assert "relaunch refused (result-delivered)" in str(caught.value)
    assert len(calls) == 1
    (decision,) = _decisions(eng)
    assert decision.category is RecoveryCategory.IDLE_TIMEOUT
    assert "not accepted (no-reply)" in decision.evidence
    assert load_state(eng.paths.state_file).phase == Phase.REVIEW
