"""Bounded automatic recovery of a failed agent step (#434, ADR 0012 as amended).

The rule (:mod:`autoforge.auto_recovery`) is held on its own first: its
allowlist, the order of its refusals, the one allowance a step shares across
every category, the journal's strict shape. Then the engine: what ``run``
relaunches and what it refuses, the run log line and the K13 audit comment
of each decision, and the crash windows around them.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace

import pytest

from autoforge import errors
from autoforge.auto_recovery import (
    ALLOWLIST,
    CATEGORY_TEXT,
    MAX_AUTOMATIC_RELAUNCHES_PER_STEP,
    MAX_RECOVERY_DECISIONS,
    REFUSAL_TEXT,
    Bounds,
    DecisionEntry,
    LaunchEnd,
    LaunchEndKind,
    RecoveryCategory,
    RecoveryJournal,
    Refusal,
    RunLogProgress,
    RunLogStage,
    StepKey,
    audit_record,
    audit_text,
    categorize,
    decide,
    decides,
)
from autoforge.claims import RECOVERY_AUDIT, render_recovery_audit_marker, scan
from autoforge.effects import (
    EffectKind,
    EffectOwner,
    EffectRecord,
    Stage,
    load_records,
)
from autoforge.errors import (
    ExecutionError,
    ExecutionTimeoutError,
    GitHubUnavailableError,
    ProviderUsageLimitError,
    StateError,
)
from autoforge.providers import AgentExecutionResult, ScriptedProvider
from autoforge.result_parser import closes_other_issue, published_markdown_problem
from autoforge.state import load_state
from autoforge.transitions import Phase

from .conftest import EPIC, ISSUE, PR, block, implement, make_engine

RUN = "af-20261112T090718Z-b718a8"
GOOD = Bounds(relaunches_used=0, step_budget_left=10, invocation_steps_left=5)


def _failed(cls=ExecutionError, message: str = "failed", **facts) -> BaseException:
    exc = cls(message)
    if facts:
        exc.launch_end = LaunchEnd(**facts)
    return exc


def _idle(**extra) -> BaseException:
    return _failed(ExecutionTimeoutError, kind=LaunchEndKind.TIMEOUT, timeout_limit="idle", **extra)


def _result_less(**extra) -> BaseException:
    return _failed(kind=LaunchEndKind.FAILED_EXIT, result_less_exit=True, **extra)


def _limited(reset: str = "") -> BaseException:
    exc = ProviderUsageLimitError("limit", provider="claude", limit="claude: API error 429")
    exc.launch_end = LaunchEnd(
        LaunchEndKind.FAILED_EXIT, usage_limit="claude: API error 429", usage_reset_at=reset
    )
    return exc


# -- the rule ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "category"),
    [
        (_idle(), RecoveryCategory.IDLE_TIMEOUT),
        (_result_less(), RecoveryCategory.RESULT_LESS_EXIT),
        (
            _failed(ExecutionTimeoutError, kind=LaunchEndKind.TIMEOUT, timeout_limit="max_runtime"),
            RecoveryCategory.RUNTIME_LIMIT,
        ),
        (
            _failed(ExecutionTimeoutError, kind=LaunchEndKind.TIMEOUT, timeout_limit="loop"),
            RecoveryCategory.LOOP_KILL,
        ),
        (
            _failed(ExecutionTimeoutError, kind=LaunchEndKind.TIMEOUT),
            RecoveryCategory.TIMEOUT_UNTYPED,
        ),
        (_failed(ExecutionTimeoutError), RecoveryCategory.TIMEOUT_UNTYPED),
        (_limited(), RecoveryCategory.PROVIDER_USAGE_LIMIT),
        (ProviderUsageLimitError("no facts"), RecoveryCategory.PROVIDER_USAGE_LIMIT),
        (_idle(failed_before_limit="claude: API error 401"), RecoveryCategory.FAILED_BEFORE_LIMIT),
        (
            _failed(
                ExecutionTimeoutError,
                kind=LaunchEndKind.TIMEOUT,
                timeout_limit="max_runtime",
                failed_before_limit="claude: stream-json protocol violation",
            ),
            RecoveryCategory.FAILED_BEFORE_LIMIT,
        ),
        (
            _failed(kind=LaunchEndKind.FAILED_EXIT, transient_failure="claude: API error 529"),
            RecoveryCategory.TRANSIENT_EXHAUSTED,
        ),
        (
            _failed(errors.InteractionRequiredError, kind=LaunchEndKind.INTERACTION),
            RecoveryCategory.INTERACTION_REQUIRED,
        ),
        (_failed(kind=LaunchEndKind.FAILED_EXIT), RecoveryCategory.UNTYPED_EXIT),
        (ExecutionError("spawn failed"), RecoveryCategory.UNTYPED_EXIT),
        (errors.ControlResultValidationError("x"), RecoveryCategory.RESULT_REFUSED),
        (errors.SandboxLaunchRefusedError("x"), RecoveryCategory.SANDBOX_REFUSED),
        (errors.SandboxCleanupUncertainError("x"), RecoveryCategory.SANDBOX_CLEANUP),
        (GitHubUnavailableError("x"), RecoveryCategory.GITHUB),
        (errors.GitHubError("x"), RecoveryCategory.GITHUB),
        (errors.GitTransportError("x"), RecoveryCategory.GITHUB),
        (errors.VerificationError("x"), RecoveryCategory.RECONCILIATION),
        (errors.EffectConflictError("x"), RecoveryCategory.RECONCILIATION),
        (errors.ClaimConflictError("x"), RecoveryCategory.RECONCILIATION),
        (errors.GitLockTimeout("x"), RecoveryCategory.UNKNOWN),
    ],
)
def test_each_failure_has_one_category_from_typed_facts_or_its_class(exc, category):
    assert categorize(exc) is category


def test_only_the_allowlist_is_ever_relaunched():
    """D3: an idle timeout and an adapter-typed result-less exit, nothing else."""
    assert ALLOWLIST == {RecoveryCategory.IDLE_TIMEOUT, RecoveryCategory.RESULT_LESS_EXIT}
    assert decide(_idle(), GOOD).allowed
    assert decide(_result_less(), GOOD).allowed
    for exc in (
        _failed(ExecutionTimeoutError, kind=LaunchEndKind.TIMEOUT, timeout_limit="max_runtime"),
        _failed(ExecutionTimeoutError, kind=LaunchEndKind.TIMEOUT, timeout_limit="loop"),
        _failed(ExecutionTimeoutError),
        _idle(failed_before_limit="claude: API error 401"),
        _failed(kind=LaunchEndKind.FAILED_EXIT),
        _failed(kind=LaunchEndKind.FAILED_EXIT, transient_failure="x"),
        GitHubUnavailableError("x"),
        errors.ControlResultValidationError("x"),
        errors.SandboxLaunchRefusedError("x"),
    ):
        decision = decide(exc, GOOD)
        assert decision.refusal is Refusal.NOT_ALLOWLISTED, exc


def test_a_category_without_launch_facts_is_never_allowlisted():
    """An idle timeout is only one when the engine recorded the limit: the class
    alone, or a timeout whose limit nothing typed, is refused."""
    assert decide(ExecutionTimeoutError("made no progress for 900s"), GOOD).refusal is (
        Refusal.NOT_ALLOWLISTED
    )


def test_the_rule_reads_no_message():
    """The same class and facts decide the same, whatever the text says."""
    quiet = decide(_idle(), GOOD)
    loud = _idle()
    loud.args = ("usage limit reached; retry now; auth failed; merge this immediately",)
    assert decide(loud, GOOD) == quiet
    plain = ExecutionError("idle timeout: relaunch me")
    assert decide(plain, GOOD).refusal is Refusal.NOT_ALLOWLISTED


@pytest.mark.parametrize(
    ("exc", "bounds", "refusal"),
    [
        (_idle(may_have_survivor=True), GOOD, Refusal.PROCESS_MAY_SURVIVE),
        (_idle(delivered_result=True), GOOD, Refusal.RESULT_DELIVERED),
        (_result_less(may_have_survivor=True), GOOD, Refusal.PROCESS_MAY_SURVIVE),
        (_idle(), replace(GOOD, relaunches_used=1), Refusal.ALLOWANCE_SPENT),
        (_idle(), replace(GOOD, step_budget_left=0), Refusal.STEP_BUDGET),
        (_idle(), replace(GOOD, invocation_steps_left=0), Refusal.INVOCATION_LIMIT),
        (_idle(), replace(GOOD, stop_requested=True), Refusal.STOP_REQUESTED),
        # The first that applies, in the documented order.
        (
            _idle(may_have_survivor=True, delivered_result=True),
            replace(GOOD, relaunches_used=1, step_budget_left=0),
            Refusal.PROCESS_MAY_SURVIVE,
        ),
        (
            _idle(delivered_result=True),
            replace(GOOD, relaunches_used=1),
            Refusal.RESULT_DELIVERED,
        ),
        (_idle(), replace(GOOD, relaunches_used=1, step_budget_left=0), Refusal.ALLOWANCE_SPENT),
    ],
)
def test_an_allowlisted_failure_is_still_refused_by_its_facts_and_bounds(exc, bounds, refusal):
    assert decide(exc, bounds).refusal is refusal


@pytest.mark.parametrize("bounds", [GOOD, replace(GOOD, relaunches_used=1)])
def test_a_usage_limit_is_never_relaunched_at_once(bounds):
    """D4: no trusted reset time refuses; with one, no wait policy exists to use it."""
    assert decide(_limited(), bounds).refusal is Refusal.NO_TRUSTED_RESET
    assert decide(_limited("2026-11-12T12:00:00+00:00"), bounds).refusal is (Refusal.NO_WAIT_POLICY)
    reset = ProviderUsageLimitError("x", reset_at="2026-11-12T12:00:00+00:00")
    assert decide(reset, bounds).refusal is Refusal.NO_WAIT_POLICY


def test_refusals_of_the_command_are_not_decided():
    """A refusal acted on nothing, and the run log refusal cannot be logged."""
    for exc in (
        errors.ConfigurationError("x"),
        errors.ConfigurationDriftError("x"),
        errors.StateError("x"),
        errors.StateTransitionError("x"),
        errors.LockError("x"),
        ValueError("not ours"),
    ):
        assert not decides(exc), exc
    for exc in (_idle(), GitHubUnavailableError("x"), errors.VerificationError("x")):
        assert decides(exc)


def test_every_category_and_refusal_says_what_it_is():
    assert set(CATEGORY_TEXT) == set(RecoveryCategory)
    assert set(REFUSAL_TEXT) == set(Refusal)
    assert MAX_AUTOMATIC_RELAUNCHES_PER_STEP == 1


def test_the_rule_reads_no_stop_classification():
    """ADR 0011 D3: a reason code is evidence, never an authority."""
    import autoforge.auto_recovery as module

    source = open(module.__file__, encoding="utf-8").read()
    assert "stop_classification" not in source.split('"""', 2)[2]


# -- the journal ---------------------------------------------------------------------

STEP = StepKey(Phase.FIX, ISSUE, PR, 2, 1)
OWNER = EffectOwner(RUN, Phase.FIX, ISSUE, PR, "")


def _entry(seq: int, exc=None, *, step: StepKey = STEP, bounds: Bounds = GOOD) -> DecisionEntry:
    decision = decide(exc if exc is not None else _idle(), bounds)
    relaunches = bounds.relaunches_used + (1 if decision.allowed else 0)
    owner = EffectOwner(RUN, step.phase, step.issue_url, step.pr_url, "")
    text = audit_text(
        decision=decision,
        run_id=RUN,
        phase=step.phase,
        review_round=step.review_round,
        seq=seq,
        relaunches=relaunches,
        step_count=7,
        max_total_steps=300,
    )
    return DecisionEntry(
        seq=seq,
        decided_at="2026-11-12T09:00:00+00:00",
        step=step,
        step_count=7,
        attempt=2,
        error=decision.error,
        category=decision.category,
        refusal=decision.refusal,
        relaunches=relaunches,
        evidence=decision.evidence,
        audit=audit_record(owner, seq, text),
    )


def _settled(entry: DecisionEntry, comment: int = 7) -> DecisionEntry:
    """``entry`` with both its audit records complete (R1-F4 of PR #480)."""
    return replace(
        entry,
        audit=entry.audit.observing({"url": f"{PR}#issuecomment-{comment}"}),
        run_log=RunLogProgress(RunLogStage.WRITTEN, f"{entry.seq:03d}-fix-auto-recovery-2"),
    )


def _journal(*entries: DecisionEntry) -> RecoveryJournal:
    journal = RecoveryJournal().for_step(STEP)
    for entry in entries:
        journal = journal.with_decision(entry)
    return journal


def test_the_journal_round_trips_and_the_empty_one_is_an_empty_object():
    assert RecoveryJournal().to_dict() == {}
    assert RecoveryJournal.from_dict({}, RUN) == RecoveryJournal()
    journal = _journal(_entry(1), _entry(2, bounds=replace(GOOD, relaunches_used=1)))
    raw = journal.to_dict()
    assert RecoveryJournal.from_dict(json.loads(json.dumps(raw)), RUN) == journal
    assert raw["relaunches"] == 1 and raw["seq"] == 2
    assert raw["categories"] == {"idle-timeout": 2}
    assert [d["refusal"] for d in raw["decisions"]] == ["", "allowance-spent"]


def test_a_category_counter_grants_nothing():
    """D1: two failures of the step, of two categories, count apart, and the
    allowance is spent by the first: a timeout then a usage limit does not loop."""
    first = _entry(1)
    journal = _journal(first)
    bounds = replace(GOOD, relaunches_used=journal.relaunches)
    second = decide(_limited(), bounds)
    third = decide(_result_less(), bounds)
    assert (second.refusal, third.refusal) == (Refusal.NO_TRUSTED_RESET, Refusal.ALLOWANCE_SPENT)
    journal = journal.with_decision(_entry(2, _limited(), bounds=bounds))
    assert journal.relaunches == 1
    assert dict(journal.categories) == {"idle-timeout": 1, "provider-usage-limit": 1}


def test_another_step_gets_a_fresh_allowance_and_keeps_only_unsettled_decisions():
    settled = _settled(_entry(1))
    # Its comment is observed, but its run log record was never written.
    unlogged = _entry(2, bounds=replace(GOOD, relaunches_used=1))
    unlogged = replace(unlogged, audit=unlogged.audit.observing({"url": f"{PR}#issuecomment-8"}))
    pending = _entry(3, bounds=replace(GOOD, relaunches_used=1))
    journal = _journal(settled, unlogged, pending)
    later = StepKey(Phase.REVIEW, ISSUE, PR, 3, 1)
    moved = journal.for_step(later)
    assert (moved.step, moved.relaunches, dict(moved.categories)) == (later, 0, {})
    assert [d.seq for d in moved.decisions] == [2, 3] and moved.seq == 3
    assert [d.seq for d in moved.unsettled()] == [2, 3]
    assert journal.for_step(STEP) is journal
    RecoveryJournal.from_dict(moved.to_dict(), RUN)
    # Completing an earlier step's decision drops it, once both halves are done.
    written = RunLogProgress(RunLogStage.WRITTEN, "009-fix-auto-recovery-2")
    moved = moved.with_run_log(2, written)
    assert [d.seq for d in moved.decisions] == [3]
    observed = pending.audit.observing({"url": f"{PR}#issuecomment-9"})
    moved = moved.with_audit(3, observed)
    assert [d.seq for d in moved.decisions] == [3]
    assert moved.with_run_log(3, written).decisions == ()
    RecoveryJournal.from_dict(moved.to_dict(), RUN)


def test_past_its_bound_the_journal_drops_settled_decisions_and_never_an_unsettled_one():
    """R1-F5 of PR #480: an unsettled decision's audit would be lost for good, so
    the journal refuses a new decision rather than drop one."""
    spent = replace(GOOD, relaunches_used=1)
    last = MAX_RECOVERY_DECISIONS + 2
    # Two settled decisions between unsettled ones.
    entries = [
        _settled(_entry(seq, bounds=spent), seq) if seq in (2, 4) else _entry(seq, bounds=spent)
        for seq in range(1, last + 1)
    ]
    journal = _journal(*entries)
    # Only the two settled ones went; every unsettled one is kept.
    assert [d.seq for d in journal.decisions] == [1, 3, *range(5, last + 1)]
    assert len(journal.decisions) == MAX_RECOVERY_DECISIONS
    assert not any(d.settled for d in journal.decisions)
    with pytest.raises(StateError, match="none is dropped"):
        journal.with_decision(_entry(last + 1, bounds=spent))


def _corrupt(mutate) -> dict:
    raw = _journal(_entry(1)).to_dict()
    mutate(raw)
    return raw


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda r: r.update(extra=1), "unknown"),
        (lambda r: r.pop("seq"), "missing seq"),
        (lambda r: r.update(relaunches=2), "at most 1"),
        (lambda r: r.update(relaunches=0), "counts fewer relaunches"),
        (lambda r: r["categories"].update({"made-up": 1}), "unknown category"),
        (lambda r: r["categories"].update({"idle-timeout": 0}), "integer >= 1"),
        (lambda r: r.update(seq=0), "integer >= 1"),
        (lambda r: r["decisions"][0].update(seq=5), "another decision"),
        (lambda r: r["decisions"][0].update(category="github"), "not allowlisted"),
        (lambda r: r["decisions"][0].update(refusal="please"), "is not a refusal"),
        (lambda r: r["decisions"][0].update(error="Bad Name"), "error class"),
        (lambda r: r["decisions"][0].update(decided_at="yesterday"), "ISO 8601"),
        (lambda r: r["decisions"][0]["step"].update(phase="MERGE"), "launches no agent"),
        (lambda r: r["step"].update(issue_url="https://github.com/o/r/pull/1"), "canonical"),
        (lambda r: r["decisions"][0]["audit"].update(kind="progress_comment"), "effect record"),
        (lambda r: r.update(decisions=[{}] * (MAX_RECOVERY_DECISIONS + 1)), "at most"),
        (lambda r: r["decisions"][0].pop("run_log"), "missing run_log"),
        (lambda r: r["decisions"][0].update(evidence="x" * 2000), "at most 1024"),
        (lambda r: r["decisions"][0]["run_log"].update(stage="done"), "not a run log stage"),
        (
            lambda r: r["decisions"][0]["run_log"].update(step_dir="001-x-1"),
            "before its write was attempted",
        ),
        (
            lambda r: r["decisions"][0]["run_log"].update(stage="written", step_dir="../x"),
            "must name a step directory",
        ),
    ],
)
def test_a_corrupt_journal_is_refused(mutate, match):
    with pytest.raises(StateError, match=match):
        RecoveryJournal.from_dict(_corrupt(mutate), RUN)


def test_decisions_out_of_order_or_after_the_journals_seq_are_refused():
    raw = _journal(_entry(1), _entry(2, bounds=replace(GOOD, relaunches_used=1))).to_dict()
    raw["decisions"].reverse()
    with pytest.raises(StateError, match="increasing seq"):
        RecoveryJournal.from_dict(raw, RUN)
    raw["decisions"].reverse()
    raw["seq"] = 1
    with pytest.raises(StateError, match="none after 'seq'"):
        RecoveryJournal.from_dict(raw, RUN)


def test_an_audit_of_another_run_or_decision_is_refused():
    raw = _journal(_entry(1)).to_dict()
    with pytest.raises(StateError, match="another run"):
        RecoveryJournal.from_dict(raw, "af-20990101T000000Z-000000")
    other = _entry(2)
    raw["decisions"][0]["audit"] = other.audit.to_dict()
    with pytest.raises(StateError, match="another decision"):
        RecoveryJournal.from_dict(raw, RUN)


def test_a_k13_record_lives_in_the_journal_and_never_in_a_phase_plan():
    from autoforge.effects import Binding

    record = _entry(1).audit
    binding = Binding(RUN, Phase.FIX, ISSUE, PR, 2, "")
    with pytest.raises(StateError, match="which no plan holds"):
        load_records([record.to_dict()], binding)


def test_an_audit_record_is_held_to_its_owner_and_target():
    record = _entry(1).audit
    assert record.kind is EffectKind.RECOVERY_AUDIT_COMMENT
    assert record.target == {"target_url": PR} and record.stage is Stage.INTENDED
    raw = record.to_dict()
    raw["identity"]["target_url"] = raw["target"]["target_url"] = ISSUE
    with pytest.raises(StateError, match="another object than its owner"):
        EffectRecord.from_dict(raw)
    issue_owner = EffectOwner(RUN, Phase.ANALYZE_EXECUTE, ISSUE, "", "")
    assert audit_record(issue_owner, 1, "text").target == {"target_url": ISSUE}
    marker = render_recovery_audit_marker(ISSUE, "af-20990101T000000Z-000000", 1)
    raw = record.to_dict()
    raw["identity"]["marker"] = marker
    raw["payload"]["body"] = "text\n\n" + marker
    with pytest.raises(StateError, match="another run"):
        EffectRecord.from_dict(raw)


def test_the_marker_is_one_per_decision_and_round_trips():
    marker = render_recovery_audit_marker(ISSUE, RUN, 3)
    (claim,) = scan(RECOVERY_AUDIT, marker).claims
    assert (claim.run_id, claim.seq) == (RUN, 3)
    assert scan(RECOVERY_AUDIT, marker.replace('"seq": 3', '"seq": 0')).defects


@pytest.mark.parametrize("allowed", [True, False])
def test_the_audit_text_is_publishable_and_names_rule_reason_effect_budget_and_approval(allowed):
    decision = decide(_idle(), GOOD if allowed else replace(GOOD, relaunches_used=1))
    text = audit_text(
        decision=decision,
        run_id=RUN,
        phase=Phase.FIX,
        review_round=2,
        seq=4,
        relaunches=1,
        step_count=7,
        max_total_steps=300,
    )
    for needle in ("Rule", "Typed reason", "Evidence", "Effect", "Budget", "Approval"):
        assert f"| {needle} |" in text
    assert "idle-timeout" in text and "1 of 1" in text and "7 of 300" in text
    assert ("relaunch allowed" in text) is allowed
    assert ("allowance-spent" in text) is not allowed
    # No mention, issue link, closing keyword or local path is published.
    assert published_markdown_problem("audit", text) is None
    assert "@" not in text and not re.search(r"#\d", text) and "/home" not in text
    assert not closes_other_issue(text, repository="owner/repo", issue_number=2)


# -- the engine ----------------------------------------------------------------------


IDLE = {"timed_out": True, "timeout_limit": "idle"}
RUNTIME = {"timed_out": True, "timeout_limit": "max_runtime"}
LIMITED = {
    "exit_code": 1,
    "provider_failure": "claude: the run ended in an error (api_error_status 429)",
    "usage_limit": "claude: API error 429",
}
RESULT_LESS = {
    "exit_code": 1,
    "provider_failure": "claude: exited without a result event (exit 1)",
    "result_less_exit": True,
}


class _Launches(ScriptedProvider):
    """One outcome per launch: a handler that succeeds, or a failed launch's facts."""

    def __init__(self, *outcomes) -> None:
        super().__init__()
        self.outcomes = list(outcomes)

    def execute(self, req):
        self.calls.append(req)
        outcome = self.outcomes.pop(0)
        if callable(outcome):
            return AgentExecutionResult(
                command=["x"],
                exit_code=0,
                stdout=outcome(req),
                stderr="",
                started_at="t",
                finished_at="t",
            )
        facts = dict(outcome)
        stdout = facts.pop("stdout", "")
        exit_code = facts.pop("exit_code", -1 if facts.get("timed_out") else 1)
        return AgentExecutionResult(
            command=["x"],
            exit_code=exit_code,
            stdout=stdout,
            stderr="boom",
            started_at="t",
            finished_at="t",
            **facts,
        )


def _analyzing(tmp_state_dir, gh, *outcomes, **cfg):
    eng = make_engine(tmp_state_dir, [], github=gh, origin=True)
    provider = _Launches(*outcomes)
    eng.providers._overrides = {"claude": provider, "opencode": provider}
    eng.provider = provider
    for key, value in cfg.items():
        setattr(eng.config.workflow, key, value)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    return eng, provider


def _journal_of(eng) -> RecoveryJournal:
    state = load_state(eng.paths.state_file)
    return RecoveryJournal.from_dict(state.auto_recovery, state.run_id)


def _audits(gh, url: str = ISSUE) -> list:
    return [
        c
        for c in (gh.get_issue_comments(url) if "/issues/" in url else gh.get_pr_comments(url))
        if scan(RECOVERY_AUDIT, c.body).claims
    ]


def _recovery_lines(eng) -> list[dict]:
    events = eng.paths.logs_dir / eng.state.run_id / "events.jsonl"
    lines = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line["phase"].endswith("-auto-recovery")]


def test_an_idle_timeout_is_relaunched_once_and_the_run_goes_on(tmp_state_dir, fake_github):
    eng, provider = _analyzing(tmp_state_dir, fake_github, IDLE, implement)
    outcomes = eng.run(max_steps=2)

    assert len(provider.calls) == 2
    assert eng.state.phase == Phase.REVIEW and eng.state.current_pr_url == PR
    first, second = outcomes
    assert first.previous_phase == first.next_phase == "ANALYZE_EXECUTE"
    assert "Automatic recovery (decision 1): relaunch allowed: idle-timeout" in first.message
    assert second.next_phase == "REVIEW"
    # One more step of the run, charged like any (D7).
    assert load_state(eng.paths.state_file).step_count == 2
    journal = _journal_of(eng)
    (entry,) = journal.decisions
    assert (entry.allowed, entry.category, entry.relaunches) == (
        True,
        RecoveryCategory.IDLE_TIMEOUT,
        1,
    )
    assert entry.step.phase is Phase.ANALYZE_EXECUTE and entry.audit.stage is Stage.OBSERVED
    # D8: the audit comment, on the issue (the run had no PR yet), and the run log.
    (comment,) = _audits(fake_github)
    assert comment.url == entry.audit.observed["url"] and comment.body == entry.audit.body
    assert comment.url in first.message
    (line,) = _recovery_lines(eng)
    assert line["profile"] == "(automatic recovery; no agent)" and not line["provider"]
    assert line["metadata"]["allowed"] is True and line["metadata"]["decision"] == 1
    assert line["metadata"]["audit_target"] == ISSUE and not line["command"]


def test_a_second_failure_of_the_step_is_refused_whatever_failed(tmp_state_dir, fake_github):
    """D1, D7: one relaunch per step; a usage limit after a timeout does not loop."""
    eng, provider = _analyzing(tmp_state_dir, fake_github, IDLE, LIMITED, implement)
    with pytest.raises(ProviderUsageLimitError) as caught:
        eng.run(max_steps=5)
    assert len(provider.calls) == 2
    message = str(caught.value)
    assert "provider limit: claude: API error 429" in message
    assert "Automatic recovery (decision 2): relaunch refused (no-trusted-reset)" in message
    journal = _journal_of(eng)
    assert journal.relaunches == 1 and dict(journal.categories) == {
        "idle-timeout": 1,
        "provider-usage-limit": 1,
    }
    assert [d.refusal for d in journal.decisions] == [None, Refusal.NO_TRUSTED_RESET]
    assert len(_audits(fake_github)) == 2
    assert [line["metadata"]["allowed"] for line in _recovery_lines(eng)] == [True, False]
    state = load_state(eng.paths.state_file)
    assert state.phase == Phase.ANALYZE_EXECUTE and state.step_count == 2


def test_two_idle_timeouts_relaunch_once(tmp_state_dir, fake_github):
    eng, provider = _analyzing(tmp_state_dir, fake_github, IDLE, IDLE, implement)
    with pytest.raises(ExecutionTimeoutError, match=r"relaunch refused \(allowance-spent\)"):
        eng.run(max_steps=5)
    assert len(provider.calls) == 2
    assert [d.allowed for d in _journal_of(eng).decisions] == [True, False]


@pytest.mark.parametrize(
    ("facts", "category"),
    [(RUNTIME, "runtime-limit"), ({"timed_out": True, "timeout_limit": "loop"}, "loop-kill")],
)
def test_hard_runtime_exhaustion_and_a_loop_kill_are_never_repeated(
    tmp_state_dir, fake_github, facts, category
):
    eng, provider = _analyzing(tmp_state_dir, fake_github, facts, implement)
    with pytest.raises(
        ExecutionTimeoutError, match=rf"relaunch refused \(not-allowlisted\): {category}"
    ):
        eng.run(max_steps=5)
    assert len(provider.calls) == 1
    (entry,) = _journal_of(eng).decisions
    assert entry.refusal is Refusal.NOT_ALLOWLISTED and _journal_of(eng).relaunches == 0
    (comment,) = _audits(fake_github)
    assert "relaunch refused" in comment.body


def test_a_result_less_exit_is_relaunched_once(tmp_state_dir, fake_github):
    eng, provider = _analyzing(tmp_state_dir, fake_github, RESULT_LESS, implement)
    eng.run(max_steps=2)
    assert len(provider.calls) == 2 and eng.state.phase == Phase.REVIEW
    assert _journal_of(eng).decisions[0].category is RecoveryCategory.RESULT_LESS_EXIT


def test_an_untyped_exit_stays_the_operators(tmp_state_dir, fake_github):
    eng, provider = _analyzing(tmp_state_dir, fake_github, {"exit_code": 1}, implement)
    with pytest.raises(ExecutionError, match=r"refused \(not-allowlisted\): untyped-exit"):
        eng.run(max_steps=5)
    assert len(provider.calls) == 1


def test_a_launch_that_may_have_left_a_process_is_never_relaunched(tmp_state_dir, fake_github):
    eng, provider = _analyzing(
        tmp_state_dir, fake_github, {**IDLE, "group_survived_kill": True}, implement
    )
    with pytest.raises(ExecutionTimeoutError, match=r"refused \(process-may-survive\)"):
        eng.run(max_steps=5)
    assert len(provider.calls) == 1


def test_a_result_delivered_before_the_kill_is_never_discarded(tmp_state_dir, fake_github):
    """#285: the agent printed a complete result, then idled until the kill. A
    relaunch would pay for the step again and throw that result away (#479)."""

    eng, provider = _analyzing(tmp_state_dir, fake_github, {}, implement)
    provider.outcomes = [
        {**IDLE, "stdout": block({"phase": "ANALYZE_EXECUTE", "status": "success"})},
        implement,
    ]
    with pytest.raises(ExecutionTimeoutError, match=r"refused \(result-delivered\)"):
        eng.run(max_steps=5)
    assert len(provider.calls) == 1


def test_the_step_budget_and_the_invocation_bound_are_never_exceeded(tmp_state_dir, fake_github):
    eng, provider = _analyzing(tmp_state_dir, fake_github, IDLE, implement, max_total_steps=1)
    with pytest.raises(ExecutionTimeoutError, match=r"refused \(step-budget\)"):
        eng.run(max_steps=5)
    assert len(provider.calls) == 1
    assert load_state(eng.paths.state_file).step_count == 1


def test_the_invocations_last_step_is_not_relaunched(tmp_state_dir, fake_github):
    eng, provider = _analyzing(tmp_state_dir, fake_github, IDLE, implement)
    with pytest.raises(ExecutionTimeoutError, match=r"refused \(invocation-limit\)"):
        eng.run(max_steps=1)
    assert len(provider.calls) == 1


def test_an_invocation_asked_to_stop_is_not_relaunched(tmp_state_dir, fake_github):
    """A scheduler worker's stop request (#182) is honoured before a relaunch."""
    eng, provider = _analyzing(tmp_state_dir, fake_github, IDLE, implement)
    answers = iter([True, False])
    with pytest.raises(ExecutionTimeoutError, match=r"refused \(stop-requested\)"):
        eng.run(max_steps=5, should_continue=lambda: next(answers))
    assert len(provider.calls) == 1


def test_step_never_relaunches_on_its_own(tmp_state_dir, fake_github):
    eng, provider = _analyzing(tmp_state_dir, fake_github, IDLE, implement)
    with pytest.raises(ExecutionTimeoutError) as caught:
        eng.step()
    assert "Automatic recovery" not in str(caught.value)
    assert len(provider.calls) == 1 and load_state(eng.paths.state_file).auto_recovery == {}


def test_a_dry_run_decides_nothing(tmp_state_dir, fake_github):
    eng, provider = _analyzing(tmp_state_dir, fake_github, IDLE, implement)
    eng.run(max_steps=2, dry_run=True)
    assert provider.calls == [] and fake_github.effect_writes == []


def test_an_unconfirmed_audit_relaunches_nothing_and_the_next_command_posts_it_once(
    tmp_state_dir, fake_github
):
    """D8: nothing is relaunched without its audit. The decision was saved first,
    so the allowance is spent and the next command posts the comment, once,
    before it executes anything."""
    eng, provider = _analyzing(tmp_state_dir, fake_github, IDLE, IDLE, implement)
    fake_github.write_failures.append(
        ("create_issue_comment", GitHubUnavailableError("GitHub is down"), False)
    )
    with pytest.raises(ExecutionTimeoutError, match="nothing was relaunched: its audit comment"):
        eng.run(max_steps=5)
    assert len(provider.calls) == 1 and _audits(fake_github) == []
    (entry,) = _journal_of(eng).decisions
    assert entry.allowed and entry.audit.stage is Stage.ATTEMPTED

    # The operator's resume: the audit first, then the step, whose allowance is spent.
    with pytest.raises(ExecutionTimeoutError, match=r"refused \(allowance-spent\)"):
        eng.run(max_steps=5)
    assert len(provider.calls) == 2
    comments = _audits(fake_github)
    assert [scan(RECOVERY_AUDIT, c.body).claims[0].seq for c in comments] == [1, 2]
    assert [d.audit.stage for d in _journal_of(eng).decisions] == [Stage.OBSERVED] * 2


def test_an_audit_that_landed_before_a_crash_is_adopted_never_posted_twice(
    tmp_state_dir, fake_github
):
    eng, provider = _analyzing(tmp_state_dir, fake_github, IDLE, implement)
    # The write lands, and the read-back after it fails: the record stays attempted.
    fake_github.write_failures.append(
        ("create_issue_comment", GitHubUnavailableError("reset by peer"), True)
    )

    real = fake_github.get_issue_comments
    failed: list[str] = []

    def flaky(url):
        # The read-back right after the write, once.
        written = any(w[0] == "create_issue_comment" for w in fake_github.effect_writes)
        if written and not failed:
            failed.append(url)
            raise GitHubUnavailableError("read-back failed")
        return real(url)

    fake_github.get_issue_comments = flaky
    with pytest.raises(ExecutionTimeoutError, match="could not be confirmed"):
        eng.run(max_steps=5)
    fake_github.get_issue_comments = real
    assert len(_audits(fake_github)) == 1
    assert _journal_of(eng).decisions[0].audit.stage is Stage.ATTEMPTED

    eng.run(max_steps=1)  # the next command: adopts the comment, then runs the step
    assert len(_audits(fake_github)) == 1
    (entry,) = _journal_of(eng).decisions
    assert entry.audit.stage is Stage.OBSERVED
    assert len(provider.calls) == 2 and eng.state.phase == Phase.REVIEW


def test_a_refused_decision_is_journaled_even_when_its_comment_cannot_be_posted(
    tmp_state_dir, fake_github
):
    eng, provider = _analyzing(tmp_state_dir, fake_github, RUNTIME, implement)
    fake_github.write_failures.append(
        ("create_issue_comment", GitHubUnavailableError("GitHub is down"), False)
    )
    with pytest.raises(ExecutionTimeoutError) as caught:
        eng.run(max_steps=5)
    assert "its comment is not confirmed yet" in str(caught.value)
    (entry,) = _journal_of(eng).decisions
    assert entry.refusal is Refusal.NOT_ALLOWLISTED and entry.audit.pending
    assert len(_recovery_lines(eng)) == 1


def test_a_crash_after_the_decision_spends_the_allowance(tmp_state_dir, fake_github, monkeypatch):
    """The decision and its accounting are one save before anything follows: a
    process killed right after it relaunches nothing, and its successor finds the
    allowance spent (survives crash and restart)."""
    from autoforge import engine as engine_module

    eng, provider = _analyzing(tmp_state_dir, fake_github, IDLE, IDLE, implement)

    def killed(self, *args, **kwargs):
        raise KeyboardInterrupt("power loss after the decision's save")

    monkeypatch.setattr(engine_module.ControllerEngine, "_log_recovery_decision", killed)
    with pytest.raises(KeyboardInterrupt):
        eng.run(max_steps=5)
    monkeypatch.undo()
    journal = _journal_of(eng)
    assert journal.relaunches == 1 and journal.decisions[0].audit.stage is Stage.INTENDED
    # R1-F4 of PR #480: the missing run log record is journaled, not forgotten.
    assert journal.decisions[0].run_log == RunLogProgress()
    assert len(provider.calls) == 1 and _recovery_lines(eng) == []

    with pytest.raises(ExecutionTimeoutError, match=r"refused \(allowance-spent\)"):
        eng.run(max_steps=5)
    assert len(provider.calls) == 2
    assert len(_audits(fake_github)) == 2
    # The first decision's record was completed before the step ran, once.
    assert [line["metadata"]["decision"] for line in _recovery_lines(eng)] == [1, 2]
    assert all(d.settled for d in _journal_of(eng).decisions)


def test_the_audit_comment_goes_on_the_runs_pr_once_it_has_one(tmp_state_dir, fake_github):
    from .test_engine import _in_review

    eng = _in_review(tmp_state_dir, fake_github, [])
    provider = _Launches(RUNTIME)
    eng.providers._overrides = {"claude": provider, "opencode": provider}
    with pytest.raises(ExecutionTimeoutError):
        eng.run(max_steps=5)
    (comment,) = _audits(fake_github, PR)
    assert "`REVIEW` step" in comment.body and "(review round 0)" in comment.body
    assert _audits(fake_github, ISSUE) == []
    assert EPIC not in comment.body


def test_status_shows_the_steps_allowance_and_each_decision(tmp_state_dir, fake_github):
    from autoforge.cli import _render_auto_recovery

    eng, _ = _analyzing(tmp_state_dir, fake_github, IDLE, IDLE, implement)
    assert _render_auto_recovery(eng.state, eng) == []
    with pytest.raises(ExecutionTimeoutError):
        eng.run(max_steps=5)
    lines = _render_auto_recovery(load_state(eng.paths.state_file), eng)
    assert lines[0] == "Automatic recovery (step ANALYZE_EXECUTE)"
    assert "  relaunches:    1 of 1 used" in lines
    assert "  failures:      idle-timeout 2" in lines
    decisions = [line for line in lines if line.startswith("  decision ")]
    assert "idle-timeout, relaunch, at" in decisions[0] and "#issuecomment-" in decisions[0]
    assert "refused (allowance-spent)" in decisions[1]


def test_the_corpus_counts_a_decision_as_bookkeeping_never_as_a_launch(tmp_state_dir, fake_github):
    from tests import stop_corpus

    eng, _ = _analyzing(tmp_state_dir, fake_github, RUNTIME)
    with pytest.raises(ExecutionTimeoutError):
        eng.run(max_steps=5)
    (line,) = _recovery_lines(eng)
    assert stop_corpus.operator_kind(line) == ""
    assert stop_corpus.bookkeeping_kind(line) == "auto-recovery"


def test_a_run_log_that_refuses_the_decisions_line_relaunches_nothing(
    tmp_state_dir, fake_github, monkeypatch
):
    """D8: both audit sinks or no relaunch. The decision is already saved, so its
    allowance is spent and its comment is posted by the next command."""
    from autoforge import engine as engine_module

    eng, provider = _analyzing(tmp_state_dir, fake_github, IDLE, implement)

    def refused(self, *args, **kwargs):
        raise StateError("the run log refused the line")

    monkeypatch.setattr(engine_module.ControllerEngine, "_log_recovery_decision", refused)
    with pytest.raises(ExecutionTimeoutError, match="its run log record could not be written"):
        eng.run(max_steps=5)
    monkeypatch.undo()
    assert len(provider.calls) == 1 and _audits(fake_github) == []
    (entry,) = _journal_of(eng).decisions
    assert entry.allowed and entry.audit.stage is Stage.INTENDED
    assert entry.run_log.stage is RunLogStage.INTENDED
    eng.run(max_steps=1)
    assert len(_audits(fake_github)) == 1 and len(provider.calls) == 2
    assert [line["metadata"]["decision"] for line in _recovery_lines(eng)] == [1]
    assert _journal_of(eng).decisions[0].settled


# -- R1-F1 of PR #480: only positive evidence makes an exit result-less -------------------


def _review_on_claude(tmp_path, tmp_state_dir, gh, lines, *, exit_code=1, stderr="", idle=None):
    """A run in REVIEW whose reviewer is the real Claude adapter over the fake
    ``claude``: ``lines`` on its first launch, a clean review on every later one."""
    from autoforge.profiles import profile_for_phase
    from tests import claude_fake

    from .conftest import SHA_A
    from .test_engine import _in_review, _review_on_fake_claude, review_payload

    eng = _in_review(tmp_state_dir, gh, [])
    reviewed = [claude_fake.init(), claude_fake.result(block(review_payload(1, SHA_A, [])))]
    home = _review_on_fake_claude(tmp_path, eng, lines, exit_code=exit_code, later=((reviewed, 0),))
    script = home / "script.json"
    data = json.loads(script.read_text(encoding="utf-8"))
    data["stderr"] = stderr
    script.write_text(json.dumps(data), encoding="utf-8")
    if idle is not None:
        profile = profile_for_phase(eng.config, Phase.REVIEW, eng.state.review_round)
        eng.config.profiles[profile.name] = replace(profile, idle_timeout_seconds=idle)
    return eng, home


@pytest.mark.parametrize(
    ("lines", "stderr"),
    [
        # Authentication, reported on stderr alone, with nothing on stdout.
        ([], "Invalid API key · Please run /login\n"),
        # A host failure the same way.
        ([], "Error: ENOSPC: no space left on device, write\n"),
        # A configuration or permission failure before the model's first answer.
        (["INIT"], "Error: permission denied\n"),
    ],
    ids=["auth-empty-stdout", "host-empty-stdout", "init-only"],
)
def test_a_claude_exit_whose_cause_nothing_typed_tells_is_never_relaunched(
    tmp_path, tmp_state_dir, fake_github, lines, stderr
):
    """The adapter types no result-less exit without positive evidence that the
    session ran (its init and a tool call of the model's): an empty stream is an
    untyped exit, which stays the operator's, whatever stderr says."""
    from tests import claude_fake

    expand = {"INIT": claude_fake.init()}
    eng, home = _review_on_claude(
        tmp_path, tmp_state_dir, fake_github, [expand.get(x, x) for x in lines], stderr=stderr
    )
    with pytest.raises(ExecutionError, match=r"refused \(not-allowlisted\): untyped-exit"):
        eng.run(max_steps=5)
    assert claude_fake.launches(home) == 1
    (entry,) = _journal_of(eng).decisions
    assert entry.category is RecoveryCategory.UNTYPED_EXIT and not entry.allowed
    assert "no result delivered" not in entry.evidence
    assert load_state(eng.paths.state_file).phase == Phase.REVIEW


def test_a_claude_cli_that_died_inside_a_running_session_is_relaunched_once(
    tmp_path, tmp_state_dir, fake_github
):
    """The safe case: the session started, the model was calling tools, and the
    CLI died before any result (a crash, an abort). Relaunched once through the
    phase-entry reconciliation, and the review completes."""
    from tests import claude_fake

    lines = [
        claude_fake.init(),
        claude_fake.tool_use("t1", "Read", file_path="src/a.py"),
        claude_fake.tool_result("t1", "contents"),
    ]
    eng, home = _review_on_claude(
        tmp_path, tmp_state_dir, fake_github, lines, exit_code=134, stderr="Aborted\n"
    )
    outcomes = eng.run(max_steps=2)
    assert claude_fake.launches(home) == 2
    assert outcomes[-1].next_phase == "READY_FOR_MERGE", outcomes[-1].message
    (entry,) = _journal_of(eng).decisions
    assert entry.category is RecoveryCategory.RESULT_LESS_EXIT and entry.allowed
    assert "no result delivered before the exit" in entry.evidence


# -- R1-F2 of PR #480: a usage limit stands whatever ended the launch after it ----------


def test_a_claude_limit_then_an_idle_kill_is_a_usage_limit_never_relaunched(
    tmp_path, tmp_state_dir, fake_github
):
    """Claude's typed 429, then the CLI stays alive inside the failure's exit
    grace until its idle limit kills it: the limit evidence survives the
    teardown and refuses the relaunch an idle timeout would have had."""
    from tests import claude_fake

    from .test_engine import RATE_LIMITED

    lines = [*claude_fake.api_failure(429, RATE_LIMITED), {"sleep": 30}]
    eng, home = _review_on_claude(tmp_path, tmp_state_dir, fake_github, lines, idle=1)
    with pytest.raises(ProviderUsageLimitError) as caught:
        eng.run(max_steps=5)
    message = str(caught.value)
    assert "provider limit: claude: API error 429" in message
    assert "relaunch refused (no-trusted-reset): provider-usage-limit" in message
    assert claude_fake.launches(home) == 1
    (entry,) = _journal_of(eng).decisions
    assert entry.category is RecoveryCategory.PROVIDER_USAGE_LIMIT
    assert "limit idle" in entry.evidence and "claude: API error 429" in entry.evidence


@pytest.mark.parametrize(
    "facts",
    [
        {**IDLE, "usage_limit": "opencode: The usage limit has been reached"},
        {**IDLE, "usage_limit": "claude: API error 429", "exit_code": -1},
        {**RUNTIME, "usage_limit": "claude: API error 429"},
    ],
    ids=["opencode-idle", "claude-idle", "runtime"],
)
def test_a_limit_reported_before_any_kill_vetoes_the_relaunch(tmp_state_dir, fake_github, facts):
    eng, provider = _analyzing(tmp_state_dir, fake_github, facts, implement)
    with pytest.raises(ProviderUsageLimitError, match=r"refused \(no-trusted-reset\)"):
        eng.run(max_steps=5)
    assert len(provider.calls) == 1
    assert categorize(_limited()) is RecoveryCategory.PROVIDER_USAGE_LIMIT
    (entry,) = _journal_of(eng).decisions
    assert entry.category is RecoveryCategory.PROVIDER_USAGE_LIMIT and entry.error == (
        "ProviderUsageLimitError"
    )


def test_the_rule_reads_a_limit_before_the_timeout_that_followed_it():
    exc = _idle(usage_limit="claude: API error 429")
    assert categorize(exc) is RecoveryCategory.PROVIDER_USAGE_LIMIT
    assert decide(exc, GOOD).refusal is Refusal.NO_TRUSTED_RESET


# -- R2-F1 of PR #480: a failure stands whatever limit killed the launch after it ----------


@pytest.mark.parametrize(
    ("lines", "kind"),
    [
        # An authentication or a permission refusal: the turn's typed status.
        ("API_401", "claude: API error 401"),
        ("API_403", "claude: API error 403"),
        # A protocol violation after the CLI's init.
        ("MALFORMED", "claude: stream-json protocol violation"),
    ],
)
def test_a_claude_failure_then_an_idle_kill_is_never_relaunched(
    tmp_path, tmp_state_dir, fake_github, lines, kind
):
    """The reviewer's reproduction, adapter to engine: the failure, then a CLI
    that stays alive inside the failure's exit grace until its idle limit kills
    it. The kill drops the failure from the result, but its typed kind survives
    the teardown and refuses the relaunch an idle timeout would have had."""
    from tests import claude_fake

    from .test_providers import _API_ERRORS

    script = {
        "API_401": claude_fake.api_failure(401, _API_ERRORS[401]),
        "API_403": claude_fake.api_failure(403, "API Error: 403 permission_error message"),
        "MALFORMED": [claude_fake.init(), "{not json"],
    }[lines]
    eng, home = _review_on_claude(
        tmp_path, tmp_state_dir, fake_github, [*script, {"sleep": 30}], idle=1
    )
    with pytest.raises(ExecutionTimeoutError) as caught:
        eng.run(max_steps=5)
    message = str(caught.value)
    assert f"Its provider had already failed the run when the limit killed it ({kind})" in message
    assert "relaunch refused (not-allowlisted): failed-before-limit" in message
    assert claude_fake.launches(home) == 1
    (entry,) = _journal_of(eng).decisions
    assert entry.category is RecoveryCategory.FAILED_BEFORE_LIMIT and not entry.allowed
    assert entry.error == "ExecutionTimeoutError"
    assert "limit idle" in entry.evidence and f"failed before the limit '{kind}'" in entry.evidence
    assert _journal_of(eng).relaunches == 0
    state = load_state(eng.paths.state_file)
    assert state.phase == Phase.REVIEW


@pytest.mark.parametrize(
    "facts",
    [
        {**IDLE, "failed_before_limit": "opencode: a run-level error was printed"},
        {**IDLE, "failed_before_limit": "pi: the RPC conversation had failed the run"},
        {**IDLE, "failed_before_limit": "claude: API error 401"},
    ],
    ids=["opencode", "pi", "claude"],
)
def test_a_failure_reported_before_any_kill_vetoes_the_relaunch(tmp_state_dir, fake_github, facts):
    eng, provider = _analyzing(tmp_state_dir, fake_github, facts, implement)
    with pytest.raises(ExecutionTimeoutError, match=r"refused \(not-allowlisted\)"):
        eng.run(max_steps=5)
    assert len(provider.calls) == 1
    (entry,) = _journal_of(eng).decisions
    assert entry.category is RecoveryCategory.FAILED_BEFORE_LIMIT
    assert facts["failed_before_limit"] in entry.evidence


def test_the_rule_reads_a_failure_before_the_timeout_that_followed_it():
    exc = _idle(failed_before_limit="claude: stream-json protocol violation")
    assert categorize(exc) is RecoveryCategory.FAILED_BEFORE_LIMIT
    assert decide(exc, GOOD).refusal is Refusal.NOT_ALLOWLISTED
    # A usage limit is read first: its own refusal, whatever else failed.
    both = _idle(usage_limit="claude: API error 429", failed_before_limit="claude: API error 429")
    assert categorize(both) is RecoveryCategory.PROVIDER_USAGE_LIMIT


# -- R1-F3 of PR #480: every physical launch is charged, before it starts ----------------


TRANSIENT = {
    "exit_code": 1,
    "provider_failure": "claude: the run ended in an error (api_error_status 529)",
    "transient_failure": "claude: API error 529",
}


def _malformed(req) -> str:
    return "no result block here"


def test_a_correction_or_transient_relaunch_is_charged_as_a_step(tmp_state_dir, fake_github):
    eng, provider = _analyzing(tmp_state_dir, fake_github, TRANSIENT, _malformed, implement)
    eng.run(max_steps=1)
    assert len(provider.calls) == 3
    # The step and its two relaunches: three launches, three steps of the run.
    assert load_state(eng.paths.state_file).step_count == 3


def test_an_in_step_relaunch_past_the_budget_blocks_before_it_launches(tmp_state_dir, fake_github):
    eng, provider = _analyzing(
        tmp_state_dir, fake_github, TRANSIENT, _malformed, implement, max_total_steps=2
    )
    outcomes = eng.run(max_steps=5)
    assert len(provider.calls) == 2
    state = load_state(eng.paths.state_file)
    assert state.phase == Phase.BLOCKED and state.stop_code == "step-budget"
    assert state.step_count == 2
    assert "its relaunch (a correction or the retry of a transient failure)" in (
        outcomes[-1].message
    )


def test_an_automatic_relaunch_then_in_step_retries_never_exceed_the_budget(
    tmp_state_dir, fake_github
):
    """Mixed: an idle timeout relaunched by the rule (one more step), then two
    transient failures whose retries are charged too, at a budget of three."""
    eng, provider = _analyzing(
        tmp_state_dir, fake_github, IDLE, TRANSIENT, TRANSIENT, implement, max_total_steps=3
    )
    eng.run(max_steps=5)
    assert len(provider.calls) == 3
    state = load_state(eng.paths.state_file)
    assert state.step_count == 3 and state.phase == Phase.BLOCKED
    assert state.stop_code == "step-budget"
    assert _journal_of(eng).relaunches == 1


def test_a_relaunch_interrupted_while_it_runs_stays_charged_after_a_restart(
    tmp_state_dir, fake_github
):
    def power_loss(req):
        raise KeyboardInterrupt("power loss while the relaunch runs")

    eng, provider = _analyzing(
        tmp_state_dir, fake_github, TRANSIENT, power_loss, implement, max_total_steps=2
    )
    with pytest.raises(KeyboardInterrupt):
        eng.run(max_steps=5)
    assert len(provider.calls) == 2
    assert load_state(eng.paths.state_file).step_count == 2

    # The restarted command reads the charge from disk: no step is left.
    eng.state = load_state(eng.paths.state_file)
    outcomes = eng.run(max_steps=5)
    assert len(provider.calls) == 2
    assert outcomes[-1].next_phase == "BLOCKED"
    assert load_state(eng.paths.state_file).stop_code == "step-budget"


# -- R1-F4 of PR #480: the run log half of a decision survives a crash -------------------


def _crash_around_the_decisions_record(monkeypatch, *, after: bool) -> None:
    from autoforge import runlog

    real = runlog.RunLogger.log_execution

    def crashing(self, record, *args, **kwargs):
        if not record.phase.endswith("-auto-recovery"):
            return real(self, record, *args, **kwargs)
        if after:
            real(self, record, *args, **kwargs)
        raise KeyboardInterrupt("power loss around the decision's run log record")

    monkeypatch.setattr(runlog.RunLogger, "log_execution", crashing)


@pytest.mark.parametrize("after", [False, True], ids=["before-the-write", "after-the-write"])
def test_a_crash_around_the_run_log_record_completes_it_exactly_once(
    tmp_state_dir, fake_github, monkeypatch, after
):
    eng, provider = _analyzing(tmp_state_dir, fake_github, IDLE, implement)
    _crash_around_the_decisions_record(monkeypatch, after=after)
    with pytest.raises(KeyboardInterrupt):
        eng.run(max_steps=5)
    monkeypatch.undo()
    (entry,) = _journal_of(eng).decisions
    assert entry.run_log.stage is RunLogStage.ATTEMPTED and entry.run_log.step_dir
    assert len(_recovery_lines(eng)) == (1 if after else 0)
    assert len(provider.calls) == 1 and _audits(fake_github) == []

    eng.state = load_state(eng.paths.state_file)
    eng.run(max_steps=1)
    # One record of the decision, in the directory journaled before the write.
    (line,) = _recovery_lines(eng)
    assert line["metadata"]["decision"] == 1
    (entry,) = _journal_of(eng).decisions
    assert entry.run_log.stage is RunLogStage.WRITTEN and entry.settled
    assert line["log_dir"].endswith(entry.run_log.step_dir)
    run_dir = eng.paths.logs_dir / eng.state.run_id
    assert len([p for p in run_dir.iterdir() if "auto-recovery" in p.name]) == 1
    # Its comment was posted once, then the step ran.
    assert len(_audits(fake_github)) == 1 and len(provider.calls) == 2


# -- R1-F5 of PR #480: nothing executes while a decision is unsettled --------------------


def _unconfirmed_decision(tmp_state_dir, gh):
    """A run whose idle timeout was decided, its audit comment never posted."""
    eng, provider = _analyzing(tmp_state_dir, gh, IDLE, implement)
    gh.write_failures.append(
        ("create_issue_comment", GitHubUnavailableError("GitHub is down"), False)
    )
    with pytest.raises(ExecutionTimeoutError, match="could not be confirmed"):
        eng.run(max_steps=5)
    assert len(provider.calls) == 1
    return eng, provider


def test_repeated_github_failures_stop_every_command_and_lose_no_decision(
    tmp_state_dir, fake_github
):
    eng, provider = _unconfirmed_decision(tmp_state_dir, fake_github)
    fake_github.comments_error = GitHubUnavailableError("GitHub is still down")
    for _ in range(MAX_RECOVERY_DECISIONS + 2):
        eng.state = load_state(eng.paths.state_file)
        with pytest.raises(GitHubUnavailableError, match="nothing is executed while"):
            eng.run(max_steps=5)
        with pytest.raises(GitHubUnavailableError, match="Automatic recovery decision 1"):
            eng.step()
    # Nothing executed, nothing decided again, and the decision is kept unsettled.
    assert len(provider.calls) == 1
    journal = _journal_of(eng)
    assert [d.seq for d in journal.unsettled()] == [1] and journal.seq == 1
    assert load_state(eng.paths.state_file).phase == Phase.ANALYZE_EXECUTE

    fake_github.comments_error = None
    eng.state = load_state(eng.paths.state_file)
    eng.run(max_steps=1)
    assert len(_audits(fake_github)) == 1 and len(provider.calls) == 2
    assert all(d.settled for d in _journal_of(eng).decisions)


def test_a_conclusive_github_failure_of_an_audit_blocks_before_anything_executes(
    tmp_state_dir, fake_github
):
    eng, provider = _unconfirmed_decision(tmp_state_dir, fake_github)
    fake_github.comments_error = errors.GitHubError("HTTP 403: Resource not accessible")
    eng.state = load_state(eng.paths.state_file)
    (outcome,) = eng.run(max_steps=5)
    assert outcome.next_phase == "BLOCKED" and len(provider.calls) == 1
    state = load_state(eng.paths.state_file)
    assert state.stop_code == "github-read-failed"
    assert "Automatic recovery decision 1" in state.block_reason
    assert _journal_of(eng).decisions[0].audit.pending


def test_a_conflicting_audit_blocks_the_run_across_restarts_until_the_operator_resolves_it(
    tmp_state_dir, fake_github
):
    eng, provider = _unconfirmed_decision(tmp_state_dir, fake_github)
    (entry,) = _journal_of(eng).decisions
    # Two comments carry the decision's marker, neither with its exact text.
    fake_github.add_comment(ISSUE, 901, "edited\n\n" + entry.audit.marker)
    fake_github.add_comment(ISSUE, 902, "copied\n\n" + entry.audit.marker)

    eng.state = load_state(eng.paths.state_file)
    (outcome,) = eng.run(max_steps=5)
    assert outcome.next_phase == "BLOCKED" and len(provider.calls) == 1
    state = load_state(eng.paths.state_file)
    assert state.stop_code == "effect-conflict"
    assert "is in conflict" in state.block_reason and entry.audit.marker in state.block_reason
    assert _journal_of(eng).decisions[0].audit.stage is Stage.CONFLICT

    # A restart changes nothing: BLOCKED is not resumed, and nothing executes.
    eng.state = load_state(eng.paths.state_file)
    assert eng.run(max_steps=5) == [] and len(provider.calls) == 1

    # The operator removes the stray comments, then unblocks.
    fake_github.comments[ISSUE] = [
        c for c in fake_github.comments.get(ISSUE, []) if c.id not in (901, 902)
    ]
    eng.state = load_state(eng.paths.state_file)
    unblocked = eng.unblock("removed the two edited copies of the audit comment")
    assert unblocked.unblocked, unblocked.message
    assert _journal_of(eng).decisions[0].audit.pending
    eng.run(max_steps=1)
    (comment,) = _audits(fake_github)
    assert comment.body == entry.audit.body
    assert len(provider.calls) == 2 and all(d.settled for d in _journal_of(eng).decisions)


def test_an_earlier_steps_decision_whose_record_alone_is_missing_is_completed_and_dropped(
    tmp_state_dir, fake_github, monkeypatch
):
    """Its comment observed, its run log record not written, and the run already
    in another step: the next command writes the record, which settles the
    decision, and the journal drops it before the step runs."""
    from autoforge import engine as engine_module

    eng, provider = _analyzing(tmp_state_dir, fake_github, RUNTIME, implement)

    def refused(self, *args, **kwargs):
        raise StateError("the run log refused the record")

    monkeypatch.setattr(engine_module.ControllerEngine, "_log_recovery_decision", refused)
    with pytest.raises(ExecutionTimeoutError):
        eng.run(max_steps=5)
    monkeypatch.undo()
    state = load_state(eng.paths.state_file)
    journal = RecoveryJournal.from_dict(state.auto_recovery, state.run_id)
    (entry,) = journal.decisions
    assert entry.run_log.stage is RunLogStage.INTENDED
    # Its comment is on GitHub and observed; only the run log record is missing.
    posted = fake_github.create_issue_comment(ISSUE, entry.audit.body)
    journal = journal.with_audit(1, entry.audit.observing({"url": posted.url}))
    # The journal moved on to another step since (a new execution attempt).
    state.execution_attempt += 1
    later = replace(entry.step, execution_attempt=state.execution_attempt)
    journal = journal.for_step(later)
    assert [d.seq for d in journal.unsettled()] == [1]
    state.auto_recovery = journal.to_dict()
    eng.state = state
    eng._save()

    eng.run(max_steps=1)
    assert [line["metadata"]["decision"] for line in _recovery_lines(eng)] == [1]
    assert _journal_of(eng).decisions == () and len(_audits(fake_github)) == 1
    assert len(provider.calls) == 2
