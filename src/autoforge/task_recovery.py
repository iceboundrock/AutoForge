"""Evidence-driven recovery of a producing step (#320; ADR 0015 D3, D6, D7).

A producing step (``ANALYZE_EXECUTE``, ``FIX``) launches its agent, and the
controller validates the exact candidate the agent's result names before
anything of it is published (ADR 0006 D5.1). This module holds the decisions
that follow that validation, as the controller's and nobody else's:

- **The verdict** (:class:`Verdict`) on one candidate, from the controller's
  own observation and never from the agent's claim: ``pass`` when every
  configured definition passed for that exact commit and tree; ``fail`` when
  one ended ``FAIL`` or ``TIMEOUT``, a definitive negative that vetoes the
  agent's ``success``; ``infra-error`` when the controller could not run the
  set at all (ADR 0006 D10.5); ``inconclusive`` when a run concluded but
  proves nothing either way (ADR 0015 D7.5's outcome, which only #319's
  independent verification produces); and ``not-run`` when no definition is
  configured. ``not-run`` is never a pass: nothing vetoes the candidate, so
  the step goes on under the existing review, but nothing was checked and
  no receipt, message or status line says otherwise.
- **Progress** (:class:`Progress`) between the task's previous conclusive
  judgment and this one, from objective deltas only: a definition that
  passes now and did not (``new-pass``), fewer passing definitions
  (``regressed``), the same number with another failure, by definition,
  outcome or exit status (``changed-failure``), the same failure on another
  tree (``changed-candidate``), or the same tree failing the same way
  (``none``). No text an agent wrote, and no output tail, enters it: a
  timing line in a test runner's output is not a changed failure.
- **Retry authorization** (:func:`authorize`): a pure function of the
  verdict, the progress, the task's earlier receipts, the phase's failure
  count (ADR 0006 D5.2's counter, ``MAX_FAILURE_COUNT``) and the policy.
  With the strategy change off, the default, it is exactly the rule the
  engine applied before this module: retry below the bound, stop at it.
- **A bounded strategy change** (``workflow.producer_strategy_change``): a
  relaunch whose candidate made no progress (``none``) is authorized as a
  ``retry-new-approach`` once per producing task, and its prompt asks the
  agent to change its approach. An agent may propose an approach; it never
  authorizes an attempt: every relaunch stays within the failure bound and
  the step budget, and a second stagnation is an ordinary retry that the
  bound stops.
- **Receipts** (:class:`Receipt`) in a ledger of the step
  (:class:`Ledger`, the state's ``producer_recovery``): every judged
  candidate, by commit and tree, with its verdict, progress and decision,
  saved in the write that acts on it: a failure's with its record and the
  count, a promoted candidate's with its plan, a stop's with the stop. The ledger
  outlives the step's stop and its transition, so a ``BLOCKED`` step keeps
  the candidates it judged for the operator, and is replaced only when
  another producing step judges a candidate.

An authorization is not a launch. The engine executes it only after its own
checks (the step budget, the agent's own stop, a partial ``FIX``), which may
stop the step instead, and that changes no receipt (ADR 0015 D6, item 3).

What this module does not do, and why: ADR 0015 assigns the rest of #320's
Memdoor addendum to #319 and #332, which have not landed. Independent
verification (checker inputs taken from the base, a pass read from a report,
the baseline, ``checker-altered``), a verifier's counterexample and its
reproduction, the last *verified* candidate's ref under ``refs/autoforge/``
and the persisted task table are theirs (D7.5 to D7.7, D19 item 4). The one
``Verdict`` member only they produce, ``inconclusive``, is handled here so
that it can never be read as a pass when they do.

Pure: no I/O, clock, randomness or environment. The engine judges, saves and
renders.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TypeVar

from . import errors
from .auto_recovery import StepKey
from .evidence import FAILURE_PHASES, MAX_FAILURE_COUNT, MAX_SEQ, Outcome, RecordView
from .memory import MAX_LAUNCH_SEQ

SCHEMA = 1
# Receipts kept per step, oldest dropped first. A producing task judges at
# most MAX_FAILURE_COUNT failing candidates before its bound stops it, so the
# bound only ever drops receipts of an earlier task of the step (before an
# `unblock`) or of a step that crashed between its pass and its plan and was
# resumed many times; it bounds the state, not a decision.
MAX_RECEIPTS = 16
# Producing tasks per step: each `unblock` into the step opens the next.
MAX_TASKS = MAX_SEQ

_HEX_RE = re.compile(r"[0-9a-f]+")
_OBJECT_ID_LENGTHS = (40, 64)
_DIGEST_LENGTH = 64


class Verdict(StrEnum):
    """What the controller's own validation says of one candidate."""

    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    INFRA_ERROR = "infra-error"
    NOT_RUN = "not-run"


#: The verdicts that conclude something about the candidate's code.
CONCLUSIVE = frozenset({Verdict.PASS, Verdict.FAIL})


class Progress(StrEnum):
    """How a judgment differs from the task's previous conclusive one."""

    FIRST = "first"
    NEW_PASS = "new-pass"
    CHANGED_FAILURE = "changed-failure"
    CHANGED_CANDIDATE = "changed-candidate"
    REGRESSED = "regressed"
    NONE = "none"


#: Objective progress: what is told apart from a repeated failure.
GENUINE_PROGRESS = frozenset(
    {Progress.NEW_PASS, Progress.CHANGED_FAILURE, Progress.CHANGED_CANDIDATE}
)


class Decision(StrEnum):
    """What the controller authorizes after a judgment; never a launch by itself."""

    #: The candidate may go on to the phase's publication (its plan save).
    PROMOTE = "promote"
    #: One more launch of the task, the failure in its prompt.
    RETRY = "retry"
    #: One more launch, asked for another approach: the bounded strategy change.
    RETRY_NEW_APPROACH = "retry-new-approach"
    #: No further launch of the task: the step stops for the operator.
    STOP = "stop"


class StopCause(StrEnum):
    """Why a ``stop`` was decided."""

    FAILURE_BOUND = "failure-bound"
    INFRA_ERROR = "infra-error"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class Policy:
    """The recovery policy in force: configuration only, never an agent's."""

    strategy_change: bool = False
    max_failures: int = MAX_FAILURE_COUNT


@dataclass(frozen=True)
class Failure:
    """The definition a failing candidate's validation set stopped at."""

    index: int
    definition_digest: str
    outcome: Outcome
    exit_code: int | None
    record_seq: int

    @property
    def signature(self) -> tuple[str, str, int | None]:
        """What a changed failure is compared on: never the output, which carries timings."""
        return (self.definition_digest, self.outcome.value, self.exit_code)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "definition_digest": self.definition_digest,
            "outcome": self.outcome.value,
            "exit_code": self.exit_code,
            "record_seq": self.record_seq,
        }


@dataclass(frozen=True)
class Judgment:
    """What the controller observed of one candidate, before anything is decided.

    ``candidate_tree`` is ``""`` when the controller did not read the
    commit's tree: under ``not-run`` nothing was validated, and an
    ``infra-error`` may be the failure to read it.
    """

    candidate_commit: str
    candidate_tree: str
    verdict: Verdict
    validation_set_digest: str
    definitions: int
    passed: int
    failure: Failure | None = None

    def __post_init__(self) -> None:
        problem = _judgment_problem(self)
        if problem:
            raise ValueError(f"an impossible judgment: {problem}")


@dataclass(frozen=True)
class Receipt:
    """One judged candidate of a producing task, and what the controller decided on it.

    ``failure_count`` and ``strategy_change`` are the decision's inputs as
    they stood, so the decision can be checked against :func:`authorize`
    from the receipts alone.
    """

    task: int
    launch_seq: int
    judgment: Judgment
    progress: Progress
    failure_count: int
    strategy_change: bool
    decision: Decision
    stop_cause: StopCause | None

    @property
    def candidate_commit(self) -> str:
        return self.judgment.candidate_commit

    @property
    def verdict(self) -> Verdict:
        return self.judgment.verdict

    def to_dict(self) -> dict:
        j = self.judgment
        return {
            "task": self.task,
            "launch_seq": self.launch_seq,
            "candidate_commit": j.candidate_commit,
            "candidate_tree": j.candidate_tree,
            "verdict": j.verdict.value,
            "validation_set_digest": j.validation_set_digest,
            "definitions": j.definitions,
            "passed": j.passed,
            "failure": None if j.failure is None else j.failure.to_dict(),
            "progress": self.progress.value,
            "failure_count": self.failure_count,
            "strategy_change": self.strategy_change,
            "decision": self.decision.value,
            "stop_cause": "" if self.stop_cause is None else self.stop_cause.value,
        }

    def describe(self) -> str:
        """One line for ``status`` and a stop's reason: controller facts only."""
        j = self.judgment
        tree = f" (tree {j.candidate_tree[:12]})" if j.candidate_tree else ""
        what = f"commit {j.candidate_commit[:12]}{tree}: {verdict_text(j)}"
        tail = f"progress {self.progress.value}, decision {self.decision.value}"
        if self.stop_cause is not None:
            tail += f" ({self.stop_cause.value})"
        return f"task {self.task}, launch {self.launch_seq}: {what}; {tail}"


def verdict_text(judgment: Judgment) -> str:
    """The verdict in words; ``not-run`` and ``inconclusive`` never read as a pass."""
    j = judgment
    if j.verdict is Verdict.PASS:
        return f"pass ({j.passed} of {j.definitions} validation definitions passed)"
    if j.verdict is Verdict.FAIL:
        assert j.failure is not None
        f = j.failure
        detail = "timed out" if f.outcome is Outcome.TIMEOUT else f"exit {f.exit_code}"
        return (
            f"fail at validation definition [{f.index}] ({f.outcome.value}, {detail}; "
            f"{j.passed} of {j.definitions} passed before it; record #{f.record_seq})"
        )
    if j.verdict is Verdict.NOT_RUN:
        return "not-run (no validation commands are configured: nothing was checked, not a pass)"
    if j.verdict is Verdict.INFRA_ERROR:
        return "infra-error (the controller could not run the validation: not a pass)"
    return "inconclusive (the validation proved nothing either way: not a pass)"


# -- judgments ----------------------------------------------------------------------------


def judged_run(
    commit: str,
    tree: str,
    validation_set_digest: str,
    definitions: int,
    failure: RecordView | None,
) -> Judgment:
    """The judgment of a validation run that concluded: ``pass``, or ``fail`` at ``failure``.

    The set runs in order and stops at its first non-``PASS`` (ADR 0006
    D10.6), so every definition before the failing one passed.
    """
    if failure is None:
        return Judgment(commit, tree, Verdict.PASS, validation_set_digest, definitions, definitions)
    if failure.outcome not in (Outcome.FAIL, Outcome.TIMEOUT):
        raise ValueError("a failing judgment names a FAIL or TIMEOUT record")
    return Judgment(
        commit,
        tree,
        Verdict.FAIL,
        validation_set_digest,
        definitions,
        failure.index,
        Failure(
            index=failure.index,
            definition_digest=failure.definition_digest,
            outcome=failure.outcome,
            exit_code=failure.exit_code,
            record_seq=failure.seq,
        ),
    )


def judged_without_a_run(
    commit: str, verdict: Verdict, validation_set_digest: str, definitions: int
) -> Judgment:
    """``not-run``, ``infra-error`` or ``inconclusive``: no definition concluded."""
    if verdict in CONCLUSIVE:
        raise ValueError(f"{verdict.value} is the verdict of a run")
    return Judgment(commit, "", verdict, validation_set_digest, definitions, 0)


def classify_progress(previous: Receipt | None, judgment: Judgment) -> Progress:
    """``judgment`` against ``previous``, the task's last conclusive receipt, by objective deltas.

    Comparable only under one validation set: another set (the operator
    changed ``validation.commands``) starts again from ``first``, as does a
    verdict that concluded nothing.
    """
    if (
        previous is None
        or judgment.verdict not in CONCLUSIVE
        or previous.verdict not in CONCLUSIVE
        or previous.judgment.validation_set_digest != judgment.validation_set_digest
    ):
        return Progress.FIRST
    before = previous.judgment
    if judgment.passed > before.passed:
        return Progress.NEW_PASS
    if judgment.passed < before.passed:
        return Progress.REGRESSED
    old = None if before.failure is None else before.failure.signature
    new = None if judgment.failure is None else judgment.failure.signature
    if old != new:
        return Progress.CHANGED_FAILURE
    if before.candidate_tree != judgment.candidate_tree:
        return Progress.CHANGED_CANDIDATE
    return Progress.NONE


def authorize(
    task_receipts: Sequence[Receipt],
    judgment: Judgment,
    progress: Progress,
    failure_count: int,
    policy: Policy,
) -> tuple[Decision, StopCause | None]:
    """The controller's decision on ``judgment``: pure, total and deterministic.

    ``task_receipts`` are the producing task's earlier receipts, oldest
    first. ``failure_count`` is the phase's failure counter after this
    judgment (ADR 0006 D5.2): it, not the receipts, is the bound's measure,
    so a run that crossed the upgrade mid-step keeps the failures it had
    already spent.
    """
    verdict = judgment.verdict
    if verdict in (Verdict.PASS, Verdict.NOT_RUN):
        return Decision.PROMOTE, None
    if verdict is Verdict.INFRA_ERROR:
        return Decision.STOP, StopCause.INFRA_ERROR
    if verdict is Verdict.INCONCLUSIVE:
        return Decision.STOP, StopCause.INCONCLUSIVE
    if failure_count >= policy.max_failures:
        return Decision.STOP, StopCause.FAILURE_BOUND
    if (
        policy.strategy_change
        and progress is Progress.NONE
        and not any(r.decision is Decision.RETRY_NEW_APPROACH for r in task_receipts)
    ):
        return Decision.RETRY_NEW_APPROACH, None
    return Decision.RETRY, None


# -- the ledger ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ledger:
    """The receipts of the step that judged a candidate last (``producer_recovery``)."""

    step: StepKey | None = None
    task: int = 1
    receipts: tuple[Receipt, ...] = ()

    @property
    def empty(self) -> bool:
        return self.step is None

    def for_step(self, step: StepKey) -> Ledger:
        """This ledger when it is ``step``'s, else a fresh one: a step never reads another's."""
        if self.step == step:
            return self
        return Ledger(step=step)

    def task_receipts(self) -> tuple[Receipt, ...]:
        """The current producing task's receipts, oldest first."""
        return tuple(r for r in self.receipts if r.task == self.task)

    def last(self) -> Receipt | None:
        """The current task's last receipt, or ``None``."""
        mine = self.task_receipts()
        return mine[-1] if mine else None

    def reopened(self) -> Ledger:
        """After an ``unblock`` into this step: a new producing task (ADR 0015 D3).

        The receipts stay as the step's history; the new task's progress,
        failures and strategy change start afresh, as the operator's
        override watermark makes the recorded failures historical.
        """
        if self.empty:
            return self
        return replace(self, task=min(self.task + 1, MAX_TASKS))

    def judged(
        self, judgment: Judgment, *, launch_seq: int, failure_count: int, policy: Policy
    ) -> tuple[Ledger, Receipt]:
        """The ledger with ``judgment``'s receipt appended, and that receipt."""
        if self.step is None:
            raise ValueError("a judgment belongs to a step: call for_step first")
        mine = self.task_receipts()
        previous = next((r for r in reversed(mine) if r.verdict in CONCLUSIVE), None)
        progress = classify_progress(previous, judgment)
        decision, cause = authorize(mine, judgment, progress, failure_count, policy)
        receipt = Receipt(
            task=self.task,
            launch_seq=launch_seq,
            judgment=judgment,
            progress=progress,
            failure_count=failure_count,
            strategy_change=policy.strategy_change,
            decision=decision,
            stop_cause=cause,
        )
        return replace(self, receipts=_bounded((*self.receipts, receipt), self.task)), receipt

    def best(self) -> Receipt | None:
        """The current task's receipt whose candidate passed the most definitions, latest on a tie.

        What a stop names as the fallback a human may start from: kept in
        the state by commit and tree, never published, and never a
        *verified* candidate (ADR 0015 D7.7 is #319's).
        """
        conclusive = [r for r in self.task_receipts() if r.verdict in CONCLUSIVE]
        if not conclusive:
            return None
        return max(reversed(conclusive), key=lambda r: r.judgment.passed)

    # -- persistence ------------------------------------------------------------------

    _KEYS = ("schema", "step", "task", "receipts")

    def to_dict(self) -> dict:
        if self.step is None:
            return {}
        return {
            "schema": SCHEMA,
            "step": self.step.to_dict(),
            "task": self.task,
            "receipts": [r.to_dict() for r in self.receipts],
        }

    @classmethod
    def from_dict(cls, raw: object) -> Ledger:
        """The ledger ``raw`` holds, validated whole: corruption is a :class:`StateError`.

        Every receipt is checked by itself: its shape, its verdict's
        consistency, and that its decision is the one :func:`authorize`
        gives from its own recorded inputs wherever those suffice. A
        decision an agent or a hand edit wrote into the file is refused,
        never followed.
        """
        if raw == {}:
            return cls()
        data = _exact(raw, cls._KEYS, "the ledger")
        _int(data["schema"], "schema", SCHEMA, SCHEMA)
        step = StepKey.from_dict(data["step"], "step")
        if step.phase.value not in FAILURE_PHASES:
            raise _fail("step.phase", f"names {step.phase.value}, which is no producing phase")
        task = _int(data["task"], "task", 1, MAX_TASKS)
        raw_receipts = data["receipts"]
        if not isinstance(raw_receipts, list) or len(raw_receipts) > MAX_RECEIPTS:
            raise _fail("receipts", f"must be a list of at most {MAX_RECEIPTS}")
        receipts = tuple(
            _parse_receipt(item, f"receipts[{i}]") for i, item in enumerate(raw_receipts)
        )
        _check_order(receipts, task)
        return cls(step=step, task=task, receipts=receipts)


def _bounded(receipts: tuple[Receipt, ...], task: int) -> tuple[Receipt, ...]:
    """At most :data:`MAX_RECEIPTS`, dropping what no decision of ``task`` reads.

    An earlier task's receipts go first, then the current task's oldest
    ``promote``: a pass judged again after a crash before its plan save. A
    task's failures and its strategy change, which :func:`authorize` reads,
    are at most ``MAX_FAILURE_COUNT`` + 1 receipts and are never dropped.
    """
    kept = list(receipts)
    while len(kept) > MAX_RECEIPTS:
        drop = next((i for i, r in enumerate(kept) if r.task != task), None)
        if drop is None:
            drop = next((i for i, r in enumerate(kept) if r.decision is Decision.PROMOTE), 0)
        del kept[drop]
    return tuple(kept)


# -- strict parsing -----------------------------------------------------------------------

_RECEIPT_KEYS = (
    "task",
    "launch_seq",
    "candidate_commit",
    "candidate_tree",
    "verdict",
    "validation_set_digest",
    "definitions",
    "passed",
    "failure",
    "progress",
    "failure_count",
    "strategy_change",
    "decision",
    "stop_cause",
)
_FAILURE_KEYS = ("index", "definition_digest", "outcome", "exit_code", "record_seq")
# Mirrors evidence's bounds: a set holds at most MAX_DEFINITIONS definitions.
_MAX_DEFINITIONS = 16
_MIN_EXIT_CODE = -128
_MAX_EXIT_CODE = 255


def _fail(what: str, message: str) -> errors.StateError:
    return errors.StateError(f"state field 'producer_recovery': {what} {message}")


def _exact(raw: object, keys: tuple[str, ...], what: str) -> dict:
    if not isinstance(raw, dict):
        raise _fail(what, "must be an object")
    missing = [k for k in keys if k not in raw]
    unknown = sorted(str(k) for k in raw if k not in keys)
    if missing or unknown:
        raise _fail(
            what,
            f"must hold exactly {', '.join(keys)}"
            + (f" (missing {', '.join(missing)})" if missing else "")
            + (f" (unknown {', '.join(repr(u)[:40] for u in unknown)})" if unknown else ""),
        )
    return raw


def _int(value: object, what: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise _fail(what, f"must be an integer from {minimum} to {maximum}")
    return value


def _bool(value: object, what: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(what, "must be true or false")
    return value


def _hex(value: object, what: str, lengths: tuple[int, ...], *, empty: bool = False) -> str:
    if empty and value == "":
        return ""
    if not isinstance(value, str) or len(value) not in lengths or not _HEX_RE.fullmatch(value):
        shapes = " or ".join(str(n) for n in lengths)
        raise _fail(
            what, f"must be {shapes} lower-case hex digits" + (" or empty" if empty else "")
        )
    return value


_E = TypeVar("_E", bound=StrEnum)


def _enum(cls: type[_E], value: object, what: str) -> _E:
    if isinstance(value, str):
        try:
            return cls(value)
        except ValueError:
            pass
    raise _fail(what, f"must be one of {', '.join(m.value for m in cls)}")


def _parse_failure(raw: object, what: str) -> Failure:
    data = _exact(raw, _FAILURE_KEYS, what)
    outcome_text = data["outcome"]
    if outcome_text not in (Outcome.FAIL.value, Outcome.TIMEOUT.value):
        raise _fail(f"{what}.outcome", "must be FAIL or TIMEOUT")
    outcome = Outcome(outcome_text)
    code = data["exit_code"]
    if outcome is Outcome.TIMEOUT:
        if code is not None:
            raise _fail(f"{what}.exit_code", "must be null for a TIMEOUT")
        exit_code = None
    else:
        exit_code = _int(code, f"{what}.exit_code", _MIN_EXIT_CODE, _MAX_EXIT_CODE)
        if exit_code == 0:
            raise _fail(f"{what}.exit_code", "0 is a pass, not a FAIL")
    return Failure(
        index=_int(data["index"], f"{what}.index", 0, _MAX_DEFINITIONS - 1),
        definition_digest=_hex(
            data["definition_digest"], f"{what}.definition_digest", (_DIGEST_LENGTH,)
        ),
        outcome=outcome,
        exit_code=exit_code,
        record_seq=_int(data["record_seq"], f"{what}.record_seq", 1, MAX_SEQ),
    )


def _judgment_problem(j: Judgment) -> str:
    """Why ``j`` cannot be: the verdict's shape, or ""."""
    if not 0 <= j.definitions <= _MAX_DEFINITIONS or not 0 <= j.passed <= j.definitions:
        return f"{j.passed} of {j.definitions} definitions passed"
    if j.verdict is Verdict.PASS:
        if j.failure is not None or j.passed != j.definitions or j.definitions == 0:
            return "a pass passes every definition of a non-empty set, with no failure"
        if not j.candidate_tree:
            return "a pass names the tree it validated"
    elif j.verdict is Verdict.FAIL:
        if j.failure is None or j.failure.index != j.passed or j.passed >= j.definitions:
            return "a failure names the definition its set stopped at, after the ones that passed"
        if not j.candidate_tree:
            return "a failure names the tree it validated"
    else:
        if j.failure is not None or j.passed != 0 or j.candidate_tree:
            return f"{j.verdict.value} concludes nothing of a definition or a tree"
        if j.verdict is Verdict.NOT_RUN and j.definitions != 0:
            return "not-run is the verdict of an empty set"
        if j.verdict is not Verdict.NOT_RUN and j.definitions == 0:
            return f"{j.verdict.value} is never the verdict of an empty set"
    return ""


def _parse_receipt(raw: object, what: str) -> Receipt:
    data = _exact(raw, _RECEIPT_KEYS, what)
    verdict = _enum(Verdict, data["verdict"], f"{what}.verdict")
    failure = (
        None if data["failure"] is None else _parse_failure(data["failure"], f"{what}.failure")
    )
    try:
        judgment = Judgment(
            candidate_commit=_hex(
                data["candidate_commit"], f"{what}.candidate_commit", _OBJECT_ID_LENGTHS
            ),
            candidate_tree=_hex(
                data["candidate_tree"], f"{what}.candidate_tree", _OBJECT_ID_LENGTHS, empty=True
            ),
            verdict=verdict,
            validation_set_digest=_hex(
                data["validation_set_digest"], f"{what}.validation_set_digest", (_DIGEST_LENGTH,)
            ),
            definitions=_int(data["definitions"], f"{what}.definitions", 0, _MAX_DEFINITIONS),
            passed=_int(data["passed"], f"{what}.passed", 0, _MAX_DEFINITIONS),
            failure=failure,
        )
    except ValueError as exc:
        raise _fail(what, str(exc)) from None
    progress = _enum(Progress, data["progress"], f"{what}.progress")
    decision = _enum(Decision, data["decision"], f"{what}.decision")
    cause_text = data["stop_cause"]
    cause = None if cause_text == "" else _enum(StopCause, cause_text, f"{what}.stop_cause")
    receipt = Receipt(
        task=_int(data["task"], f"{what}.task", 1, MAX_TASKS),
        launch_seq=_int(data["launch_seq"], f"{what}.launch_seq", 0, MAX_LAUNCH_SEQ),
        judgment=judgment,
        progress=progress,
        failure_count=_int(data["failure_count"], f"{what}.failure_count", 0, MAX_FAILURE_COUNT),
        strategy_change=_bool(data["strategy_change"], f"{what}.strategy_change"),
        decision=decision,
        stop_cause=cause,
    )
    problem = _decision_problem(receipt)
    if problem:
        raise _fail(what, problem)
    return receipt


def _decision_problem(r: Receipt) -> str:
    """Why ``r``'s decision is not :func:`authorize`'s on its own recorded inputs, or "".

    The one input a receipt does not hold is whether its task had already
    changed strategy; :func:`_check_order` checks that across the ledger.
    """
    if r.judgment.verdict is Verdict.FAIL and r.failure_count < 1:
        return "counts no failure for a failing candidate"
    if r.progress is not Progress.FIRST and r.verdict not in CONCLUSIVE:
        return f"compares a {r.verdict.value} judgment, which concludes nothing"
    expected = authorize((), r.judgment, r.progress, r.failure_count, _policy_of(r))
    if (r.decision, r.stop_cause) == expected:
        return ""
    # The one decision a receipt's own inputs cannot reproduce: a stagnation
    # after its task had changed strategy once is an ordinary retry.
    if expected == (Decision.RETRY_NEW_APPROACH, None) and (r.decision, r.stop_cause) == (
        Decision.RETRY,
        None,
    ):
        return ""
    shown = r.decision.value + (f" ({r.stop_cause.value})" if r.stop_cause else "")
    return f"records the decision {shown}, which its own inputs do not give"


def _check_order(receipts: Sequence[Receipt], task: int) -> None:
    previous: Receipt | None = None
    changed: set[int] = set()
    for i, r in enumerate(receipts):
        what = f"receipts[{i}]"
        if r.task > task:
            raise _fail(f"{what}.task", f"is {r.task}, past the ledger's task {task}")
        if previous is not None:
            if r.task < previous.task:
                raise _fail(f"{what}.task", "goes back to an earlier task")
            if r.launch_seq < previous.launch_seq:
                raise _fail(f"{what}.launch_seq", "goes back to an earlier launch")
        if r.decision is Decision.RETRY_NEW_APPROACH:
            if r.task in changed:
                raise _fail(what, f"changes task {r.task}'s strategy a second time")
            changed.add(r.task)
        elif (
            r.task == task
            and r.task not in changed
            and authorize((), r.judgment, r.progress, r.failure_count, _policy_of(r))
            == (Decision.RETRY_NEW_APPROACH, None)
        ):
            # The current task's receipts are never dropped (:func:`_bounded`),
            # so a stagnation retried without a change it had used is no
            # decision of :func:`authorize`.
            raise _fail(what, "retries a stagnation unchanged before its task changed strategy")
        previous = r


def _policy_of(r: Receipt) -> Policy:
    return Policy(strategy_change=r.strategy_change)
