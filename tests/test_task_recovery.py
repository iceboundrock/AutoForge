"""The pure recovery decisions of a producing step (#320, ``autoforge.task_recovery``).

What is decided, and by what: the verdict on a candidate from the controller's
own records; progress from objective deltas (the passing count, the failure's
definition, outcome and exit status, the tree), never from output or agent
text; retry authorization as a pure function of the receipts, the failure
count and the policy, equal to the rule the engine applied before this module
when the strategy change is off; and a ledger that refuses, on load, a
decision its own recorded inputs do not give. The engine's use of them,
end to end, is in ``tests/test_producer_recovery.py``.
"""

from __future__ import annotations

import itertools

import pytest

from autoforge.auto_recovery import StepKey
from autoforge.errors import StateError
from autoforge.evidence import (
    MAX_FAILURE_COUNT,
    Outcome,
    RecordView,
    RemoteCandidate,
    WorktreeProvenance,
)
from autoforge.task_recovery import (
    CONCLUSIVE,
    GENUINE_PROGRESS,
    MAX_RECEIPTS,
    Decision,
    Judgment,
    Ledger,
    Policy,
    Progress,
    Receipt,
    StopCause,
    Verdict,
    authorize,
    classify_progress,
    judged_run,
    judged_without_a_run,
    verdict_text,
)
from autoforge.transitions import Phase

ISSUE = "https://github.com/owner/repo/issues/2"
PR = "https://github.com/owner/repo/pull/7"
SET = "5" * 64
OTHER_SET = "6" * 64
DIGESTS = ["a" * 64, "b" * 64, "c" * 64]
STEP = StepKey(Phase.ANALYZE_EXECUTE, ISSUE, "", 0, 1)
OFF = Policy()
ON = Policy(strategy_change=True)


def commit(n: int) -> str:
    return f"{n:040x}"


def tree(n: int) -> str:
    return f"{n + 1000:040x}"


def record(
    index: int,
    *,
    outcome: Outcome = Outcome.FAIL,
    exit_code: int | None = 1,
    seq: int = 1,
    candidate: int = 1,
    candidate_tree: int | None = None,
) -> RecordView:
    """A failing record of definition ``index`` for candidate ``candidate``."""
    return RecordView(
        raw={},
        seq=seq,
        digest="d" * 64,
        candidate=RemoteCandidate(
            repository="owner/repo",
            object_format="sha1",
            commit=commit(candidate),
            tree=tree(candidate if candidate_tree is None else candidate_tree),
            provenance=WorktreeProvenance(2, "2", True, commit(0)),
        ),
        index=index,
        argv=("check",),
        definition_digest=DIGESTS[index],
        policy_digest="e" * 64,
        mode="disabled",
        outcome=outcome,
        run_log="af-20260101T000000Z-abcdef/0001-ANALYZE_EXECUTE-candidate-validation",
        exit_code=None if outcome is Outcome.TIMEOUT else exit_code,
    )


def failing(index: int = 1, *, candidate: int = 1, same_tree_as: int | None = None, **kw):
    view = record(index, candidate=candidate, candidate_tree=same_tree_as, **kw)
    return judged_run(view.candidate.commit, view.candidate.tree, SET, 3, view)


def passing(candidate: int = 9) -> Judgment:
    return judged_run(commit(candidate), tree(candidate), SET, 3, None)


def receipt_for(judgment: Judgment, *, task: int = 1, launch: int = 1) -> Receipt:
    progress = Progress.FIRST
    decision, cause = authorize((), judgment, progress, 1, OFF)
    return Receipt(task, launch, judgment, progress, 1, False, decision, cause)


# -- verdicts -------------------------------------------------------------------------------


def test_a_concluded_run_is_a_pass_or_a_fail_at_the_definition_it_stopped_at():
    passed = passing()
    assert (passed.verdict, passed.passed, passed.definitions, passed.failure) == (
        Verdict.PASS,
        3,
        3,
        None,
    )
    failed = failing(1)
    assert failed.verdict is Verdict.FAIL and failed.passed == 1
    assert failed.failure is not None and failed.failure.index == 1
    timed_out = failing(2, outcome=Outcome.TIMEOUT)
    assert timed_out.verdict is Verdict.FAIL and timed_out.failure.exit_code is None


@pytest.mark.parametrize("verdict", [Verdict.NOT_RUN, Verdict.INFRA_ERROR, Verdict.INCONCLUSIVE])
def test_a_verdict_that_concluded_nothing_is_never_read_as_a_pass(verdict):
    size = 0 if verdict is Verdict.NOT_RUN else 3
    judgment = judged_without_a_run(commit(1), verdict, SET, size)
    text = verdict_text(judgment)
    assert "not a pass" in text and not text.startswith("pass")
    assert judgment.verdict not in CONCLUSIVE
    assert (judgment.passed, judgment.candidate_tree, judgment.failure) == (0, "", None)


def test_missing_validation_is_told_apart_from_passed_validation():
    """An empty set is ``not-run``, and a pass needs a non-empty set: the two never meet."""
    not_run = judged_without_a_run(commit(1), Verdict.NOT_RUN, SET, 0)
    assert not_run.verdict is Verdict.NOT_RUN and "nothing was checked" in verdict_text(not_run)
    with pytest.raises(ValueError, match="non-empty set"):
        Judgment(commit(1), tree(1), Verdict.PASS, SET, 0, 0)
    with pytest.raises(ValueError, match="empty set"):
        judged_without_a_run(commit(1), Verdict.NOT_RUN, SET, 2)
    with pytest.raises(ValueError, match="verdict of a run"):
        judged_without_a_run(commit(1), Verdict.PASS, SET, 2)
    # A not-run candidate is promoted because nothing vetoes it, and it shows
    # no progress against a failure: it is not a new passing check.
    previous = receipt_for(failing(1))
    assert classify_progress(previous, not_run) is Progress.FIRST


@pytest.mark.parametrize(
    "kwargs",
    [
        {"verdict": Verdict.FAIL, "definitions": 3, "passed": 1, "failure": None},
        {"verdict": Verdict.PASS, "definitions": 3, "passed": 2, "failure": None},
        {"verdict": Verdict.NOT_RUN, "definitions": 0, "passed": 0, "failure": None, "tree": "x"},
        {"verdict": Verdict.INFRA_ERROR, "definitions": 0, "passed": 0, "failure": None},
        {"verdict": Verdict.PASS, "definitions": 3, "passed": 3, "failure": None, "tree": ""},
    ],
)
def test_an_impossible_judgment_is_refused(kwargs):
    candidate_tree = kwargs.pop("tree", tree(1))
    if candidate_tree == "x":
        candidate_tree = tree(1)
    with pytest.raises(ValueError, match="impossible judgment"):
        Judgment(commit(1), candidate_tree, validation_set_digest=SET, **kwargs)


# -- progress: objective deltas only --------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after", "progress"),
    [
        (failing(1), failing(2, candidate=2), Progress.NEW_PASS),
        (failing(1), passing(), Progress.NEW_PASS),
        (failing(2), failing(0, candidate=2), Progress.REGRESSED),
        (failing(1), failing(1, candidate=2, exit_code=2), Progress.CHANGED_FAILURE),
        (failing(1), failing(1, candidate=2, outcome=Outcome.TIMEOUT), Progress.CHANGED_FAILURE),
        (failing(1), failing(1, candidate=2), Progress.CHANGED_CANDIDATE),
        # Another commit with the very same tree changed no code.
        (failing(1), failing(1, candidate=2, same_tree_as=1), Progress.NONE),
        (failing(1), failing(1, seq=5), Progress.NONE),
    ],
    ids=[
        "a-check-passes-now",
        "the-set-passes-now",
        "fewer-checks-pass",
        "another-exit-status",
        "a-timeout-now",
        "same-failure-other-code",
        "same-tree-other-commit",
        "same-candidate-again",
    ],
)
def test_progress_is_told_apart_from_a_repeated_failure_by_objective_deltas(
    before, after, progress
):
    assert classify_progress(receipt_for(before), after) is progress
    assert (progress in GENUINE_PROGRESS) == (
        progress in (Progress.NEW_PASS, Progress.CHANGED_FAILURE, Progress.CHANGED_CANDIDATE)
    )


def test_progress_never_reads_the_output_or_anything_an_agent_wrote():
    """Two failures of one tree differ in their records' output and seq only: no progress.

    A judgment carries no output tail and no agent text at all, so a test
    runner's timing line, or an agent claiming it fixed the bug, cannot read
    as a changed failure.
    """
    assert set(Judgment.__dataclass_fields__) == {
        "candidate_commit",
        "candidate_tree",
        "verdict",
        "validation_set_digest",
        "definitions",
        "passed",
        "failure",
    }
    first, again = failing(1, seq=3), failing(1, seq=4)
    assert classify_progress(receipt_for(first), again) is Progress.NONE


def test_progress_starts_again_under_another_validation_set_or_with_no_conclusion():
    before = receipt_for(failing(1))
    other_set = judged_run(commit(2), tree(2), OTHER_SET, 3, record(1, candidate=2))
    assert classify_progress(before, other_set) is Progress.FIRST
    assert classify_progress(None, failing(1)) is Progress.FIRST
    infra = judged_without_a_run(commit(2), Verdict.INFRA_ERROR, SET, 3)
    assert classify_progress(before, infra) is Progress.FIRST


# -- retry authorization --------------------------------------------------------------------

VERDICT_JUDGMENTS = {
    Verdict.PASS: passing(),
    Verdict.FAIL: failing(1),
    Verdict.NOT_RUN: judged_without_a_run(commit(1), Verdict.NOT_RUN, SET, 0),
    Verdict.INFRA_ERROR: judged_without_a_run(commit(1), Verdict.INFRA_ERROR, SET, 3),
    Verdict.INCONCLUSIVE: judged_without_a_run(commit(1), Verdict.INCONCLUSIVE, SET, 3),
}


@pytest.mark.parametrize(
    ("verdict", "progress", "count", "policy"),
    list(itertools.product(list(Verdict), list(Progress), range(MAX_FAILURE_COUNT + 1), [OFF, ON])),
)
def test_authorization_is_total_and_deterministic(verdict, progress, count, policy):
    judgment = VERDICT_JUDGMENTS[verdict]
    first = authorize((), judgment, progress, count, policy)
    assert first == authorize((), judgment, progress, count, policy)
    decision, cause = first
    assert (decision is Decision.STOP) == (cause is not None)
    if verdict is not Verdict.FAIL:
        assert decision is not Decision.RETRY and decision is not Decision.RETRY_NEW_APPROACH


def test_a_negative_verdict_is_never_promoted_whatever_the_agent_reported():
    for progress, count, policy in itertools.product(list(Progress), range(4), [OFF, ON]):
        decision, _ = authorize((), failing(1), progress, count, policy)
        assert decision is not Decision.PROMOTE


@pytest.mark.parametrize("verdict", [Verdict.INFRA_ERROR, Verdict.INCONCLUSIVE])
def test_an_inconclusive_or_infrastructure_verdict_stops_and_promotes_nothing(verdict):
    decision, cause = authorize((), VERDICT_JUDGMENTS[verdict], Progress.FIRST, 0, ON)
    assert decision is Decision.STOP and cause.value == verdict.value


@pytest.mark.parametrize("progress", list(Progress))
@pytest.mark.parametrize("count", range(1, MAX_FAILURE_COUNT + 1))
def test_with_the_strategy_change_off_the_rule_is_the_one_before_it(progress, count):
    """ADR 0006 D5.2 as the engine applied it: retry below the bound, stop at it."""
    decision, cause = authorize((), failing(1), progress, count, OFF)
    if count >= MAX_FAILURE_COUNT:
        assert (decision, cause) == (Decision.STOP, StopCause.FAILURE_BOUND)
    else:
        assert (decision, cause) == (Decision.RETRY, None)


def test_a_stagnation_changes_strategy_once_per_task_and_the_bound_still_stops_it():
    assert authorize((), failing(1), Progress.NONE, 2, ON) == (Decision.RETRY_NEW_APPROACH, None)
    for progress in set(Progress) - {Progress.NONE}:
        assert authorize((), failing(1), progress, 2, ON) == (Decision.RETRY, None)
    changed = Receipt(1, 2, failing(1), Progress.NONE, 2, True, Decision.RETRY_NEW_APPROACH, None)
    assert authorize((changed,), failing(1), Progress.NONE, 2, ON) == (Decision.RETRY, None)
    assert authorize((), failing(1), Progress.NONE, MAX_FAILURE_COUNT, ON) == (
        Decision.STOP,
        StopCause.FAILURE_BOUND,
    )


@pytest.mark.parametrize("policy", [OFF, ON])
def test_no_sequence_of_candidates_launches_past_the_failure_bound(policy):
    """Whatever the agent hands back, the task's relaunches end at the bound."""
    for shapes in itertools.product(["same", "new", "better"], repeat=MAX_FAILURE_COUNT + 2):
        ledger = Ledger(step=STEP)
        relaunches = 0
        candidate = 1
        index = 0
        for count, shape in enumerate(shapes, start=1):
            if shape != "same":
                candidate += 1
            if shape == "better":
                index = min(index + 1, 2)
            view = record(index, candidate=candidate, seq=count)
            judgment = judged_run(view.candidate.commit, view.candidate.tree, SET, 3, view)
            ledger, receipt = ledger.judged(
                judgment, launch_seq=count, failure_count=count, policy=policy
            )
            if receipt.decision is Decision.STOP:
                break
            relaunches += 1
        assert relaunches == MAX_FAILURE_COUNT - 1
        assert sum(r.decision is Decision.RETRY_NEW_APPROACH for r in ledger.receipts) <= 1
        assert Ledger.from_dict(ledger.to_dict()) == ledger


# -- the ledger -----------------------------------------------------------------------------


def judge(ledger: Ledger, judgment: Judgment, count: int, launch: int, policy: Policy = ON):
    return ledger.judged(judgment, launch_seq=launch, failure_count=count, policy=policy)


def test_the_ledger_compares_with_the_tasks_last_conclusive_receipt():
    ledger = Ledger(step=STEP)
    ledger, first = judge(ledger, failing(1), 1, 1)
    infra = judged_without_a_run(commit(2), Verdict.INFRA_ERROR, SET, 3)
    ledger, stop = judge(ledger, infra, 1, 2)
    ledger, again = judge(ledger, failing(1, seq=9), 2, 3)
    assert (first.progress, stop.progress, again.progress) == (
        Progress.FIRST,
        Progress.FIRST,
        Progress.NONE,
    )
    assert stop.decision is Decision.STOP and stop.stop_cause is StopCause.INFRA_ERROR
    assert again.decision is Decision.RETRY_NEW_APPROACH


def test_a_step_never_reads_another_steps_receipts():
    """Stale-node input: a new round, issue or phase entry starts with no history."""
    ledger, _ = judge(Ledger(step=STEP), failing(1), 1, 1)
    ledger, changed = judge(ledger, failing(1), 2, 2)
    assert changed.decision is Decision.RETRY_NEW_APPROACH
    fix = StepKey(Phase.FIX, ISSUE, PR, 1, 1)
    fresh = ledger.for_step(fix)
    assert fresh.receipts == () and fresh.task == 1 and fresh.step == fix
    assert ledger.for_step(STEP) is ledger
    fresh, receipt = judge(fresh, failing(1), 1, 3)
    assert receipt.progress is Progress.FIRST


def test_an_unblock_opens_a_new_task_whose_strategy_change_is_unspent():
    ledger, _ = judge(Ledger(step=STEP), failing(1), 1, 1)
    ledger, changed = judge(ledger, failing(1), 2, 2)
    ledger, stopped = judge(ledger, failing(1), 3, 3)
    assert stopped.decision is Decision.STOP and stopped.stop_cause is StopCause.FAILURE_BOUND
    reopened = ledger.reopened()
    assert reopened.task == 2 and reopened.receipts == ledger.receipts
    assert reopened.task_receipts() == () and reopened.last() is None
    reopened, first = judge(reopened, failing(1), 1, 4)
    assert first.progress is Progress.FIRST and first.task == 2
    reopened, again = judge(reopened, failing(1), 2, 5)
    assert again.decision is Decision.RETRY_NEW_APPROACH
    assert Ledger().reopened() == Ledger()


def test_the_best_candidate_is_the_one_that_passed_the_most_checks_latest_on_a_tie():
    ledger = Ledger(step=STEP)
    ledger, _ = judge(ledger, failing(2, candidate=1), 1, 1)
    ledger, _ = judge(ledger, failing(0, candidate=2), 2, 2)
    assert ledger.best().candidate_commit == commit(1)
    ledger, _ = judge(ledger, failing(2, candidate=3), 3, 3)
    assert ledger.best().candidate_commit == commit(3)
    assert Ledger(step=STEP).best() is None


def test_the_bound_never_drops_what_a_decision_of_the_current_task_reads():
    ledger, _ = judge(Ledger(step=STEP), failing(1), 1, 1)
    ledger = ledger.reopened()
    ledger, _ = judge(ledger, failing(1), 1, 2)
    ledger, changed = judge(ledger, failing(1), 2, 3)
    assert changed.decision is Decision.RETRY_NEW_APPROACH
    for launch in range(4, 4 + MAX_RECEIPTS + 3):
        # A pass judged again after each crash before its plan save.
        ledger, _ = judge(ledger, passing(), 2, launch)
    assert len(ledger.receipts) == MAX_RECEIPTS
    assert all(r.task == 2 for r in ledger.receipts), "the earlier task's went first"
    assert changed in ledger.receipts
    assert Ledger.from_dict(ledger.to_dict()) == ledger


def test_the_ledger_round_trips_and_the_empty_one_is_empty():
    ledger, _ = judge(Ledger(step=StepKey(Phase.FIX, ISSUE, PR, 2, 1)), failing(1), 1, 7)
    data = ledger.to_dict()
    assert data["schema"] == 1 and data["step"]["pr_url"] == PR
    assert Ledger.from_dict(data) == ledger
    assert Ledger.from_dict({}) == Ledger() and Ledger().to_dict() == {}


def _ledger_data(*receipts: dict, task: int = 1, step: dict | None = None) -> dict:
    return {
        "schema": 1,
        "step": step or STEP.to_dict(),
        "task": task,
        "receipts": list(receipts),
    }


def _receipt(**changes) -> dict:
    _, receipt = judge(Ledger(step=STEP), failing(1), 1, 1)
    return {**receipt.to_dict(), **changes}


@pytest.mark.parametrize(
    ("data", "needle"),
    [
        (_ledger_data(), None),
        ({**_ledger_data(), "extra": 1}, "unknown 'extra'"),
        ({**_ledger_data(), "schema": 2}, "schema"),
        (_ledger_data(step={**STEP.to_dict(), "phase": "REVIEW"}), "no producing phase"),
        (_ledger_data(*[_receipt()] * (MAX_RECEIPTS + 1)), "at most"),
        (_ledger_data(_receipt(decision="promote")), "records the decision promote"),
        (_ledger_data(_receipt(failure_count=3)), "records the decision retry"),
        (_ledger_data(_receipt(failure_count=0)), "counts no failure"),
        (_ledger_data(_receipt(decision="stop", stop_cause="failure-bound")), "stop"),
        (
            _ledger_data(
                _receipt(progress="none", strategy_change=False, decision="retry-new-approach")
            ),
            "records the decision retry-new-approach",
        ),
        (
            _ledger_data(
                _receipt(progress="none", failure_count=2, strategy_change=True, decision="retry")
            ),
            "before its task changed strategy",
        ),
        (
            _ledger_data(
                _receipt(progress="none", strategy_change=True, decision="retry-new-approach"),
                _receipt(
                    progress="none",
                    failure_count=2,
                    strategy_change=True,
                    decision="retry-new-approach",
                ),
            ),
            "a second time",
        ),
        (_ledger_data(_receipt(task=2)), "past the ledger's task"),
        (
            _ledger_data(_receipt(launch_seq=5), _receipt(launch_seq=4, failure_count=2)),
            "earlier launch",
        ),
        (_ledger_data(_receipt(verdict="pass")), "impossible judgment"),
        (_ledger_data(_receipt(verdict="maybe")), "verdict"),
        (_ledger_data(_receipt(candidate_commit="ABC")), "candidate_commit"),
        (
            _ledger_data(_receipt(failure={**_receipt()["failure"], "exit_code": 0})),
            "0 is a pass",
        ),
        (
            _ledger_data(_receipt(failure={**_receipt()["failure"], "outcome": "TIMEOUT"})),
            "null for a TIMEOUT",
        ),
    ],
    ids=lambda v: v if isinstance(v, str) else None,
)
def test_a_ledger_that_does_not_hold_is_refused_never_followed(data, needle):
    if needle is None:
        assert Ledger.from_dict(data) == Ledger(step=STEP)
        return
    with pytest.raises(StateError, match="state field 'producer_recovery'") as exc:
        Ledger.from_dict(data)
    assert needle in str(exc.value)


def test_an_infra_receipt_that_claims_progress_is_refused():
    infra = judged_without_a_run(commit(2), Verdict.INFRA_ERROR, SET, 3)
    _, receipt = judge(Ledger(step=STEP), infra, 0, 1)
    data = _ledger_data({**receipt.to_dict(), "progress": "new-pass"})
    with pytest.raises(StateError, match="concludes nothing"):
        Ledger.from_dict(data)
    assert Ledger.from_dict(_ledger_data(receipt.to_dict())).receipts == (receipt,)
