"""Bounded automatic recovery of a failed agent step (#434; ADR 0012 as amended).

Some errors end a step and leave its phase unchanged: an agent killed by its
idle limit, an agent CLI that ended without delivering a result, a provider's
usage limit, a GitHub read that failed. Each used to end the command, and the
run waited for an operator's ``resume``, which reconciles the phase entry with
GitHub and relaunches the agent. For a narrow, explicit allowlist of those
errors this module lets the running ``run`` (or ``resume``, or a scheduler
worker) do that ``resume`` itself, once per step, under a fixed rule:

- **Typed facts only.** The decision reads the class of the error and the
  typed facts the engine recorded of the agent launch that ended the step
  (:class:`LaunchEnd`, set on the error as ``launch_end``): which limit
  killed it, whether a process it started may still run, whether its output
  holds a complete ``CONTROL_RESULT``, and what its adapter typed about the
  failure. Never the error's message, the agent's output or a stop
  classification: a reason code is evidence, never an authority (ADR 0011
  D3), and this module does not import :mod:`autoforge.stop_classification`.
- **The allowlist** (:data:`ALLOWLIST`, D3): an idle timeout, and a
  result-less exit the adapter typed as such (D5). Everything else is
  refused and stays the operator's: a hard runtime limit, a loop kill, a
  provider's usage limit (D4: never retried at once; no adapter reports a
  reset time it can trust, and no wait policy exists), a known-transient
  failure whose in-step bound is spent (D6), a request for a human decision,
  a sandbox refusal, a GitHub failure or drift (D6), a failed verification,
  an untyped exit and anything unknown.
- **One shared allowance per step** (:data:`MAX_AUTOMATIC_RELAUNCHES_PER_STEP`,
  D1, D7), persisted in the run's state (:class:`RecoveryJournal`), so a
  crash or a later ``resume`` never refills it. Failures are also counted by
  category, which grants nothing: a second failure of the step, of any
  category, is refused, so a timeout followed by a usage limit cannot loop.
- **Within the existing bounds** (D7): the relaunch is one more step of the
  run, charged against ``workflow.max_total_steps`` like any step, and one
  more step of the invocation's ``--max-steps``; a worker asked to stop is
  not relaunched. The in-step bounds (corrections, ``MAX_TRANSIENT_RETRIES``,
  the pushed-head wait) are unchanged and come first (D6).
- **Safe re-entry** (D5): the relaunch is the very path ``resume`` takes,
  the phase-entry reconciliation first (journal, effects, candidate,
  worktree), so a step whose work already landed is completed without an
  agent, and a conflict blocks. A launch that may have left a process
  running, or that delivered a complete result before it was killed (#285,
  whose acceptance #479 owns), is never relaunched.
- **Journal and dual audit** (D8): every decision, relaunch or refusal, is
  saved in the state before anything follows from it, then written to the
  run log and posted as a controller-owned comment (K13,
  :class:`autoforge.effect_ops.RecoveryAuditCommentOp`) on the run's PR, or
  on its issue while it has none, through the idempotent effect path: an
  ``attempted`` record is saved before the write and the comment is found by
  its marker before anything is sent again. Neither sink is a recovery input.

Pure: no I/O, no clock, no GitHub, no state write. The engine gathers the
facts, persists the journal and performs the audit.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum

from . import errors
from .claims import RECOVERY_AUDIT, scan
from .effects import (
    EffectKind,
    EffectOwner,
    EffectRecord,
    recovery_audit_comment_body,
)
from .redaction import redact
from .transitions import AGENT_PHASES, Phase
from .validation import parse_issue_url, parse_pr_url

#: D1, D5, D7: automatic paid relaunches of one step, whatever failed.
MAX_AUTOMATIC_RELAUNCHES_PER_STEP = 1
#: Decisions the journal keeps (those of the current step, and an older
#: step's while its audit comment is pending).
MAX_RECOVERY_DECISIONS = 16
_MAX_TEXT_CHARS = 64
_MAX_ERROR_CLASS_CHARS = 64
_ERROR_CLASS_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")


class LaunchEndKind(StrEnum):
    """How the last agent launch of a failed step ended, as the engine saw it."""

    #: A limit killed it (``LaunchEnd.timeout_limit`` says which).
    TIMEOUT = "timeout"
    #: It exited non-zero, or its provider's protocol failed the run.
    FAILED_EXIT = "failed-exit"
    #: A request of the agent's for a human decision ended it (ADR 0007 D7.6).
    INTERACTION = "interaction"


@dataclass(frozen=True)
class LaunchEnd:
    """The typed facts of the agent launch that ended a step (set as ``launch_end``).

    Every field is a fact the engine or the adapter established from typed
    values: the executor's limit, its survivor flags, the protocol's
    ``CONTROL_RESULT`` framing, an adapter's fixed reason. None is text the
    agent or its CLI printed.
    """

    kind: LaunchEndKind
    #: ``idle``, ``max_runtime`` or ``loop`` for a timeout; ``""`` when unknown.
    timeout_limit: str = ""
    #: A process the launch started may still be running (``may_have_survivor``).
    may_have_survivor: bool = False
    #: Its output holds a complete ``CONTROL_RESULT`` block (#285, #479).
    delivered_result: bool = False
    #: The adapter typed the failure as an exit without any result (D5).
    result_less_exit: bool = False
    #: The adapter's fixed reason for a provider usage or rate limit (D4).
    usage_limit: str = ""
    #: A reset time the provider reported in a form the adapter trusts.
    usage_reset_at: str = ""
    #: The adapter's fixed reason for a known-transient failure, whose in-step
    #: relaunches were spent or withheld (D6).
    transient_failure: str = ""
    #: The launch's provider, as configured (``claude``, ``opencode``, ``pi``).
    provider: str = ""

    def evidence(self) -> str:
        """The facts, for the audit: typed values only, safe to publish."""
        parts = [f"launch ended by {self.kind.value}"]
        if self.timeout_limit:
            parts.append(f"limit {self.timeout_limit}")
        if self.provider:
            parts.append(f"provider {self.provider}")
        if self.usage_limit:
            parts.append(f"provider limit '{self.usage_limit}'")
            parts.append(
                f"reset time {self.usage_reset_at}"
                if self.usage_reset_at
                else "no reset time reported"
            )
        if self.transient_failure:
            parts.append(f"transient '{self.transient_failure}'")
        if self.result_less_exit:
            parts.append("no result delivered before the exit")
        parts.append(
            "a process it started may still be running"
            if self.may_have_survivor
            else "no process it started is left"
        )
        if self.delivered_result:
            parts.append("its output holds a complete CONTROL_RESULT")
        return "; ".join(parts)


def launch_end_of(exc: BaseException) -> LaunchEnd | None:
    """The typed facts the engine set on ``exc``, or ``None``."""
    found = getattr(exc, "launch_end", None)
    return found if isinstance(found, LaunchEnd) else None


class RecoveryCategory(StrEnum):
    """What failed, by typed fact: the category a decision and its counter name."""

    IDLE_TIMEOUT = "idle-timeout"
    RESULT_LESS_EXIT = "result-less-exit"
    RUNTIME_LIMIT = "runtime-limit"
    LOOP_KILL = "loop-kill"
    TIMEOUT_UNTYPED = "timeout-untyped"
    PROVIDER_USAGE_LIMIT = "provider-usage-limit"
    TRANSIENT_EXHAUSTED = "transient-exhausted"
    INTERACTION_REQUIRED = "interaction-required"
    UNTYPED_EXIT = "untyped-exit"
    RESULT_REFUSED = "result-refused"
    SANDBOX_REFUSED = "sandbox-refused"
    SANDBOX_CLEANUP = "sandbox-cleanup"
    GITHUB = "github"
    RECONCILIATION = "reconciliation"
    UNKNOWN = "unknown"


_RC = RecoveryCategory

#: D3: the only categories that may be relaunched automatically.
ALLOWLIST: frozenset[RecoveryCategory] = frozenset({_RC.IDLE_TIMEOUT, _RC.RESULT_LESS_EXIT})

#: What each category is, and (for the refused ones) why it stays the operator's.
CATEGORY_TEXT: Mapping[RecoveryCategory, str] = {
    _RC.IDLE_TIMEOUT: "the agent made no progress for its idle limit and was killed",
    _RC.RESULT_LESS_EXIT: (
        "the agent's CLI ended without delivering any result, as its adapter typed it"
    ),
    _RC.RUNTIME_LIMIT: (
        "the agent reached its maximum runtime; hard runtime exhaustion is never "
        "repeated automatically"
    ),
    _RC.LOOP_KILL: "the loop detector killed the agent; a relaunch would likely loop again",
    _RC.TIMEOUT_UNTYPED: "a limit killed the agent, and nothing typed says which",
    _RC.PROVIDER_USAGE_LIMIT: (
        "the provider refused the launch at a usage or rate limit; a launch now meets "
        "the same limit"
    ),
    _RC.TRANSIENT_EXHAUSTED: (
        "a failure the adapter typed as transient, whose in-step relaunches were spent or withheld"
    ),
    _RC.INTERACTION_REQUIRED: "the agent asked for a human decision; a relaunch would ask again",
    _RC.UNTYPED_EXIT: (
        "the agent failed and nothing typed says why; an exit code alone does not prove "
        "a relaunch safe"
    ),
    _RC.RESULT_REFUSED: (
        "the agent's CONTROL_RESULT was missing or refused after its bounded corrections"
    ),
    _RC.SANDBOX_REFUSED: "the outer sandbox could not be established for the launch",
    _RC.SANDBOX_CLEANUP: "an object of the launch's sandbox could not be verified removed",
    _RC.GITHUB: (
        "a GitHub or git read or write failed after the in-step retries; drift and "
        "propagation stay the operator's"
    ),
    _RC.RECONCILIATION: (
        "a verification, an effect or a claim could not be completed without guessing"
    ),
    _RC.UNKNOWN: "nothing typed says what failed",
}

# The class of an error that ended a step without typed launch facts,
# nearest first along its MRO (:func:`categorize`).
_CLASS_CATEGORIES: Mapping[type[BaseException], RecoveryCategory] = {
    errors.ProviderUsageLimitError: _RC.PROVIDER_USAGE_LIMIT,
    errors.InteractionRequiredError: _RC.INTERACTION_REQUIRED,
    errors.SandboxLaunchRefusedError: _RC.SANDBOX_REFUSED,
    errors.SandboxCleanupUncertainError: _RC.SANDBOX_CLEANUP,
    errors.ExecutionTimeoutError: _RC.TIMEOUT_UNTYPED,
    errors.ExecutionError: _RC.UNTYPED_EXIT,
    errors.ControlResultError: _RC.RESULT_REFUSED,
    errors.GitHubError: _RC.GITHUB,
    errors.GitTransportError: _RC.GITHUB,
    errors.VerificationError: _RC.RECONCILIATION,
    errors.EffectConflictError: _RC.RECONCILIATION,
    errors.ClaimConflictError: _RC.RECONCILIATION,
    errors.PROwnerError: _RC.RECONCILIATION,
    errors.ValidationInterruptedError: _RC.RECONCILIATION,
}

# What the rule never decides about: an error that refused the command before
# it acted on the run (exit 2: nothing to recover), and the run log's refusal
# after a launch's attempt was saved (the run log it would write is the one
# that refused).
_NOT_DECIDED: tuple[type[BaseException], ...] = (
    errors.ConfigurationError,
    errors.StateError,
    errors.StateTransitionError,
    errors.LockError,
)


def decides(exc: BaseException) -> bool:
    """Whether ``exc`` ending a step of an agent phase gets a recovery decision.

    Every controller error but a refusal does: a relaunch for the
    allowlisted ones, a recorded refusal for the rest.
    """
    return isinstance(exc, errors.AutoForgeError) and not isinstance(exc, _NOT_DECIDED)


def categorize(exc: BaseException) -> RecoveryCategory:
    """The category of ``exc``: from its typed launch facts, else its class."""
    launch = launch_end_of(exc)
    if launch is not None:
        if launch.kind is LaunchEndKind.TIMEOUT:
            return {
                "idle": _RC.IDLE_TIMEOUT,
                "max_runtime": _RC.RUNTIME_LIMIT,
                "loop": _RC.LOOP_KILL,
            }.get(launch.timeout_limit, _RC.TIMEOUT_UNTYPED)
        if launch.kind is LaunchEndKind.INTERACTION:
            return _RC.INTERACTION_REQUIRED
        if launch.usage_limit:
            return _RC.PROVIDER_USAGE_LIMIT
        if launch.transient_failure:
            return _RC.TRANSIENT_EXHAUSTED
        if launch.result_less_exit:
            return _RC.RESULT_LESS_EXIT
        return _RC.UNTYPED_EXIT
    for cls in type(exc).__mro__:
        found = _CLASS_CATEGORIES.get(cls)
        if found is not None:
            return found
    return _RC.UNKNOWN


class Refusal(StrEnum):
    """Why a decision relaunches nothing; the first that applies, in this order."""

    NOT_ALLOWLISTED = "not-allowlisted"
    NO_TRUSTED_RESET = "no-trusted-reset"
    NO_WAIT_POLICY = "no-wait-policy"
    PROCESS_MAY_SURVIVE = "process-may-survive"
    RESULT_DELIVERED = "result-delivered"
    ALLOWANCE_SPENT = "allowance-spent"
    STEP_BUDGET = "step-budget"
    INVOCATION_LIMIT = "invocation-limit"
    STOP_REQUESTED = "stop-requested"


REFUSAL_TEXT: Mapping[Refusal, str] = {
    Refusal.NOT_ALLOWLISTED: "its category is not on the automatic-recovery allowlist",
    Refusal.NO_TRUSTED_RESET: (
        "the provider reported no reset time the controller can trust, so no wait is "
        "justified and a launch now meets the same limit"
    ),
    Refusal.NO_WAIT_POLICY: (
        "a wait for the provider's reset time needs an explicit bounded policy, and none "
        "is configured"
    ),
    Refusal.PROCESS_MAY_SURVIVE: (
        "a process the failed launch started may still be running, and a relaunch would "
        "race it in the same worktree"
    ),
    Refusal.RESULT_DELIVERED: (
        "the killed launch had delivered a complete CONTROL_RESULT, which may still be "
        "valid; a relaunch would discard it"
    ),
    Refusal.ALLOWANCE_SPENT: (
        "this step already used its one automatic relaunch, whatever failed then"
    ),
    Refusal.STEP_BUDGET: "the run's step budget (workflow.max_total_steps) has no step left",
    Refusal.INVOCATION_LIMIT: "this invocation's --max-steps has no step left",
    Refusal.STOP_REQUESTED: "the invocation was asked to stop",
}


@dataclass(frozen=True)
class Bounds:
    """What the step and the invocation have left, read from the persisted state."""

    #: The step's automatic relaunches so far (the journal's, for this step).
    relaunches_used: int
    #: ``workflow.max_total_steps`` minus the run's ``step_count``.
    step_budget_left: int
    #: Iterations of the invocation's loop left after the failed one.
    invocation_steps_left: int
    #: The invocation's ``should_continue`` said stop.
    stop_requested: bool = False


@dataclass(frozen=True)
class RecoveryDecision:
    """One decision of the rule: the category, and the refusal or ``None`` to relaunch."""

    category: RecoveryCategory
    refusal: Refusal | None
    error: str
    evidence: str

    @property
    def allowed(self) -> bool:
        return self.refusal is None

    def text(self) -> str:
        """One line: what failed, and what the rule decided. Typed values only."""
        what = f"{self.category.value} ({CATEGORY_TEXT[self.category]})"
        if self.refusal is None:
            return f"relaunch allowed: {what}"
        return f"relaunch refused ({self.refusal.value}): {what}; {REFUSAL_TEXT[self.refusal]}"


def decide(exc: BaseException, bounds: Bounds) -> RecoveryDecision:
    """The fixed rule's decision about the error ``exc`` that ended a step.

    The category first (an allowlisted one, or the reason it stays the
    operator's), then the launch's safety facts, then the step's shared
    allowance, then the run's and the invocation's bounds. ``DENY`` and
    ``INCONCLUSIVE`` are one result here: anything not proven allowed is
    refused (ADR 0012 D3).
    """
    category = categorize(exc)
    launch = launch_end_of(exc)
    evidence = launch.evidence() if launch is not None else "no agent launch ended it"
    return RecoveryDecision(
        category, _refusal(category, launch, exc, bounds), type(exc).__name__, evidence
    )


def _refusal(
    category: RecoveryCategory, launch: LaunchEnd | None, exc: BaseException, bounds: Bounds
) -> Refusal | None:
    if category is _RC.PROVIDER_USAGE_LIMIT:
        # D4: never at once; a wait only with a trusted reset time and an
        # explicit bounded policy, which nothing configures yet.
        reset = launch.usage_reset_at if launch is not None else getattr(exc, "reset_at", "")
        return Refusal.NO_WAIT_POLICY if reset else Refusal.NO_TRUSTED_RESET
    if category not in ALLOWLIST or launch is None:
        return Refusal.NOT_ALLOWLISTED
    if launch.may_have_survivor:
        return Refusal.PROCESS_MAY_SURVIVE
    if launch.delivered_result:
        return Refusal.RESULT_DELIVERED
    if bounds.relaunches_used >= MAX_AUTOMATIC_RELAUNCHES_PER_STEP:
        return Refusal.ALLOWANCE_SPENT
    if bounds.step_budget_left < 1:
        return Refusal.STEP_BUDGET
    if bounds.invocation_steps_left < 1:
        return Refusal.INVOCATION_LIMIT
    if bounds.stop_requested:
        return Refusal.STOP_REQUESTED
    return None


# -- the journal (D8) ---------------------------------------------------------------


def _fail(what: str, message: str) -> errors.StateError:
    return errors.StateError(f"state field 'auto_recovery': {what} {message}")


def _exact(raw: object, keys: tuple[str, ...], what: str) -> dict:
    if not isinstance(raw, dict):
        raise _fail(what, "must be an object")
    missing = [k for k in keys if k not in raw]
    unknown = sorted(k for k in raw if k not in keys)
    if missing or unknown:
        raise _fail(
            what,
            f"must hold exactly {', '.join(keys)}"
            + (f" (missing {', '.join(missing)})" if missing else "")
            + (f" (unknown {', '.join(repr(u)[:40] for u in unknown)})" if unknown else ""),
        )
    return raw


def _int(value: object, what: str, minimum: int, maximum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise _fail(what, f"must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise _fail(what, f"must be at most {maximum}")
    return value


def _str(value: object, what: str, max_chars: int) -> str:
    if not isinstance(value, str) or len(value) > max_chars:
        raise _fail(what, f"must be a string of at most {max_chars} characters")
    return value


def _phase(value: object, what: str) -> Phase:
    text = _str(value, what, _MAX_TEXT_CHARS)
    try:
        phase = Phase(text)
    except ValueError:
        raise _fail(what, "is not a phase") from None
    if phase not in AGENT_PHASES:
        raise _fail(what, f"names {phase.value}, which launches no agent")
    return phase


def _canonical_issue(value: object, what: str) -> str:
    text = _str(value, what, 512)
    try:
        if parse_issue_url(text).canonical == text:
            return text
    except errors.ConfigurationError:
        pass
    raise _fail(what, "must be a canonical issue URL")


def _canonical_pr(value: object, what: str) -> str:
    text = _str(value, what, 512)
    if not text:
        return text
    try:
        if parse_pr_url(text).canonical == text:
            return text
    except errors.ConfigurationError:
        pass
    raise _fail(what, "must be empty or a canonical PR URL")


@dataclass(frozen=True)
class StepKey:
    """Which step an allowance belongs to: the phase entry, by what binds it.

    A step is one phase of one issue, PR, review round and execution
    attempt, over every ``resume`` of it: the allowance is not refilled by a
    crash, a later command or an ``unblock`` back into the same phase. A
    phase change (the step completed, or reconciliation resolved it) is a
    new step with a fresh allowance.
    """

    phase: Phase
    issue_url: str
    pr_url: str
    review_round: int
    execution_attempt: int

    _KEYS = ("phase", "issue_url", "pr_url", "review_round", "execution_attempt")

    def to_dict(self) -> dict:
        return {
            "phase": self.phase.value,
            "issue_url": self.issue_url,
            "pr_url": self.pr_url,
            "review_round": self.review_round,
            "execution_attempt": self.execution_attempt,
        }

    @classmethod
    def from_dict(cls, raw: object, what: str) -> StepKey:
        data = _exact(raw, cls._KEYS, what)
        return cls(
            phase=_phase(data["phase"], f"{what}.phase"),
            issue_url=_canonical_issue(data["issue_url"], f"{what}.issue_url"),
            pr_url=_canonical_pr(data["pr_url"], f"{what}.pr_url"),
            review_round=_int(data["review_round"], f"{what}.review_round", 0),
            execution_attempt=_int(data["execution_attempt"], f"{what}.execution_attempt", 0),
        )


@dataclass(frozen=True)
class DecisionEntry:
    """One persisted decision: its identity, its accounting and its audit record."""

    seq: int
    decided_at: str
    #: The step the decision is about.
    step: StepKey
    #: The run's ``step_count`` and the phase's ``attempt`` when it was decided.
    step_count: int
    attempt: int
    #: The class of the error that ended the step (a name, never its message).
    error: str
    category: RecoveryCategory
    refusal: Refusal | None
    #: The step's automatic relaunches once this decision was taken.
    relaunches: int
    #: The K13 audit comment of the decision (D8).
    audit: EffectRecord

    _KEYS = (
        "seq",
        "decided_at",
        "step",
        "step_count",
        "attempt",
        "error",
        "category",
        "refusal",
        "relaunches",
        "audit",
    )

    @property
    def allowed(self) -> bool:
        return self.refusal is None

    @property
    def phase(self) -> Phase:
        return self.step.phase

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "decided_at": self.decided_at,
            "step": self.step.to_dict(),
            "step_count": self.step_count,
            "attempt": self.attempt,
            "error": self.error,
            "category": self.category.value,
            "refusal": self.refusal.value if self.refusal is not None else "",
            "relaunches": self.relaunches,
            "audit": self.audit.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: object, what: str, run_id: str) -> DecisionEntry:
        data = _exact(raw, cls._KEYS, what)
        seq = _int(data["seq"], f"{what}.seq", 1)
        decided_at = _str(data["decided_at"], f"{what}.decided_at", _MAX_TEXT_CHARS)
        if not _TIMESTAMP_RE.fullmatch(decided_at):
            raise _fail(f"{what}.decided_at", "must be an ISO 8601 UTC time")
        error = _str(data["error"], f"{what}.error", _MAX_ERROR_CLASS_CHARS)
        if not _ERROR_CLASS_RE.fullmatch(error):
            raise _fail(f"{what}.error", "must be an error class name")
        try:
            category = RecoveryCategory(_str(data["category"], f"{what}.category", 64))
        except ValueError:
            raise _fail(f"{what}.category", "is not a recovery category") from None
        refusal_text = _str(data["refusal"], f"{what}.refusal", 64)
        try:
            refusal = Refusal(refusal_text) if refusal_text else None
        except ValueError:
            raise _fail(f"{what}.refusal", "is not a refusal") from None
        if refusal is None and category not in ALLOWLIST:
            raise _fail(what, f"relaunches a {category.value} failure, which is not allowlisted")
        relaunches = _int(
            data["relaunches"], f"{what}.relaunches", 0, MAX_AUTOMATIC_RELAUNCHES_PER_STEP
        )
        if refusal is None and relaunches < 1:
            raise _fail(what, "relaunches without counting the relaunch")
        step = StepKey.from_dict(data["step"], f"{what}.step")
        try:
            audit = EffectRecord.from_dict(data["audit"], f"{what}.audit")
        except errors.StateError as exc:
            raise _fail(f"{what}.audit", f"is not a valid effect record ({exc})") from None
        if audit.kind is not EffectKind.RECOVERY_AUDIT_COMMENT:
            raise _fail(f"{what}.audit", f"must be a {EffectKind.RECOVERY_AUDIT_COMMENT.value}")
        if audit.position != 0:
            raise _fail(f"{what}.audit", "must be at position 0")
        if audit.owner.run_id != run_id:
            raise _fail(f"{what}.audit", "belongs to another run than the state's")
        if (
            audit.owner.phase is not step.phase
            or audit.owner.issue_url != step.issue_url
            or audit.owner.pr_url != step.pr_url
        ):
            raise _fail(f"{what}.audit", "belongs to another step than its decision")
        claim = scan(RECOVERY_AUDIT, audit.marker).claims[0]
        if claim.seq != seq:
            raise _fail(f"{what}.audit", "carries the marker of another decision")
        return cls(
            seq=seq,
            decided_at=decided_at,
            step=step,
            step_count=_int(data["step_count"], f"{what}.step_count", 0),
            attempt=_int(data["attempt"], f"{what}.attempt", 0),
            error=error,
            category=category,
            refusal=refusal,
            relaunches=relaunches,
            audit=audit,
        )


@dataclass(frozen=True)
class RecoveryJournal:
    """The persisted accounting and provenance of the rule (``state.auto_recovery``).

    ``step`` is the step the allowance and the counters belong to;
    ``relaunches`` its automatic relaunches (at most
    :data:`MAX_AUTOMATIC_RELAUNCHES_PER_STEP`); ``categories`` how many of
    its failures each category had, which grants nothing; ``seq`` the run's
    last decision number; ``decisions`` the decisions of this step, and an
    earlier step's while its audit comment is still pending, oldest first.
    The empty journal (no decision ever) is saved as ``{}``.
    """

    step: StepKey | None = None
    relaunches: int = 0
    categories: Mapping[str, int] = field(default_factory=dict)
    seq: int = 0
    decisions: tuple[DecisionEntry, ...] = ()

    _KEYS = ("step", "relaunches", "categories", "seq", "decisions")

    @property
    def empty(self) -> bool:
        return self.step is None

    def to_dict(self) -> dict:
        if self.step is None:
            return {}
        return {
            "step": self.step.to_dict(),
            "relaunches": self.relaunches,
            "categories": dict(sorted(self.categories.items())),
            "seq": self.seq,
            "decisions": [d.to_dict() for d in self.decisions],
        }

    @classmethod
    def from_dict(cls, raw: object, run_id: str) -> RecoveryJournal:
        """The journal ``raw`` holds, validated whole: corruption is a StateError."""
        if raw == {}:
            return cls()
        data = _exact(raw, cls._KEYS, "the journal")
        step = StepKey.from_dict(data["step"], "step")
        relaunches = _int(data["relaunches"], "relaunches", 0, MAX_AUTOMATIC_RELAUNCHES_PER_STEP)
        raw_categories = data["categories"]
        if not isinstance(raw_categories, dict):
            raise _fail("categories", "must be an object")
        categories: dict[str, int] = {}
        for key, count in raw_categories.items():
            try:
                RecoveryCategory(key)
            except ValueError:
                raise _fail("categories", f"names an unknown category {str(key)[:40]!r}") from None
            categories[key] = _int(count, f"categories[{key}]", 1)
        seq = _int(data["seq"], "seq", 1)
        raw_decisions = data["decisions"]
        if not isinstance(raw_decisions, list) or len(raw_decisions) > MAX_RECOVERY_DECISIONS:
            raise _fail("decisions", f"must be a list of at most {MAX_RECOVERY_DECISIONS}")
        decisions = tuple(
            DecisionEntry.from_dict(item, f"decisions[{i}]", run_id)
            for i, item in enumerate(raw_decisions)
        )
        previous = 0
        for entry in decisions:
            if entry.seq <= previous or entry.seq > seq:
                raise _fail("decisions", "must be in increasing seq order, none after 'seq'")
            previous = entry.seq
        allowed = sum(1 for d in decisions if d.allowed and d.step == step)
        if allowed > relaunches:
            raise _fail("relaunches", "counts fewer relaunches than its step's decisions made")
        for entry in decisions:
            if entry.step != step and not entry.audit.pending:
                raise _fail(
                    "decisions", "keeps a settled decision of an earlier step, which it drops"
                )
        return cls(step, relaunches, categories, seq, decisions)

    def for_step(self, key: StepKey) -> RecoveryJournal:
        """This journal for the step ``key``: unchanged for its own step; for
        another, a fresh allowance and counters, keeping only the earlier
        decisions whose audit comment is still pending."""
        if self.step == key:
            return self
        return RecoveryJournal(
            step=key,
            relaunches=0,
            categories={},
            seq=self.seq,
            decisions=tuple(d for d in self.decisions if d.audit.pending),
        )

    def with_decision(
        self, entry: DecisionEntry
    ) -> tuple[RecoveryJournal, tuple[DecisionEntry, ...]]:
        """The journal with ``entry`` appended and counted, and the entries it dropped.

        Past :data:`MAX_RECOVERY_DECISIONS` the oldest settled entries go
        first; only when none is settled does a pending one go (its audit is
        then the run log's alone, which the caller records).
        """
        if self.step is None or entry.step != self.step:
            raise errors.StateError("a recovery decision is journaled for its own step only")
        decisions = [*self.decisions, entry]
        dropped: list[DecisionEntry] = []
        while len(decisions) > MAX_RECOVERY_DECISIONS:
            settled = next((d for d in decisions[:-1] if not d.audit.pending), None)
            victim = settled if settled is not None else decisions[0]
            decisions.remove(victim)
            dropped.append(victim)
        categories = dict(self.categories)
        categories[entry.category.value] = categories.get(entry.category.value, 0) + 1
        journal = RecoveryJournal(
            step=self.step,
            relaunches=entry.relaunches,
            categories=categories,
            seq=entry.seq,
            decisions=tuple(decisions),
        )
        return journal, tuple(dropped)

    def with_audit(self, seq: int, audit: EffectRecord) -> RecoveryJournal:
        """The journal with decision ``seq``'s audit record replaced by ``audit``.

        An earlier step's decision is kept only while its audit is pending,
        so it is dropped once ``audit`` settles it.
        """
        if not any(d.seq == seq for d in self.decisions):
            raise errors.StateError(f"no recovery decision {seq} is journaled")
        decisions = tuple(
            replace(d, audit=audit) if d.seq == seq else d
            for d in self.decisions
            if not (d.seq == seq and d.step != self.step and not audit.pending)
        )
        return replace(self, decisions=decisions)

    def pending_audits(self) -> tuple[DecisionEntry, ...]:
        return tuple(d for d in self.decisions if d.audit.pending)


# -- the audit (D8) ----------------------------------------------------------------


def audit_target(owner: EffectOwner) -> str:
    """Where a decision's audit comment goes: the run's PR, else its issue."""
    return owner.pr_url or owner.issue_url


def audit_text(
    *,
    decision: RecoveryDecision,
    run_id: str,
    phase: Phase,
    review_round: int,
    seq: int,
    relaunches: int,
    step_count: int,
    max_total_steps: int,
) -> str:
    """The audit comment's text (D8): rule, typed reason, evidence, effect, budget.

    Controller text over typed values only, redacted whole; it names no
    issue number, no user and no local path (the operator's ``resume``
    command carries the paths of its flags), so publishing it links,
    notifies and discloses nothing.
    """
    if decision.refusal is None:
        verdict = "relaunch allowed"
        result = "relaunch the step once"
        effect = (
            "one paid agent launch, once this comment is confirmed, through the same "
            "phase-entry reconciliation a `resume` runs first; it is one more step of the "
            "run. If this comment cannot be confirmed, nothing is relaunched and the run "
            "stops for the operator"
        )
    else:
        verdict = "relaunch refused"
        result = f"refused (`{decision.refusal.value}`): {REFUSAL_TEXT[decision.refusal]}"
        effect = (
            "none: nothing was relaunched, and the run stops for the operator, whose "
            "`resume` reconciles the phase with GitHub first"
        )
    round_text = f" (review round {review_round})" if phase in (Phase.REVIEW, Phase.FIX) else ""
    lines = [
        f"**AutoForge automatic recovery: {verdict}**",
        "",
        f"The `{phase.value}` step of run `{run_id}`{round_text} ended with "
        f"`{decision.error}`, and the phase is unchanged.",
        "",
        "| | |",
        "|---|---|",
        (
            "| Rule | the controller's bounded automatic recovery: only `idle-timeout` and "
            f"`result-less-exit` are relaunched, at most {MAX_AUTOMATIC_RELAUNCHES_PER_STEP} "
            "time per step, whatever failed |"
        ),
        f"| Typed reason | `{decision.category.value}`: {CATEGORY_TEXT[decision.category]} |",
        f"| Evidence | {decision.evidence} |",
        f"| Decision | {result} |",
        f"| Effect | {effect} |",
        (
            f"| Budget | automatic relaunches of this step: {relaunches} of "
            f"{MAX_AUTOMATIC_RELAUNCHES_PER_STEP}; steps of the run: {step_count} of "
            f"{max_total_steps} (`workflow.max_total_steps`) |"
        ),
        "| Approval | none: a fixed controller rule, taken without a human |",
        f"| Decision number | {seq} of this run |",
    ]
    return redact("\n".join(lines))


def audit_record(owner: EffectOwner, seq: int, text: str) -> EffectRecord:
    """The ``intended`` K13 record of decision ``seq``, validated as it will be on load."""
    from .claims import render_recovery_audit_marker  # one renderer, kept beside its kind

    marker = render_recovery_audit_marker(owner.issue_url, owner.run_id, seq)
    target = audit_target(owner)
    return EffectRecord.plan(
        0,
        EffectKind.RECOVERY_AUDIT_COMMENT,
        owner,
        identity={"target_url": target, "marker": marker},
        target={"target_url": target},
        precondition={"absent": True},
        payload={"body": recovery_audit_comment_body(text, marker)},
    )


__all__ = [
    "ALLOWLIST",
    "CATEGORY_TEXT",
    "MAX_AUTOMATIC_RELAUNCHES_PER_STEP",
    "MAX_RECOVERY_DECISIONS",
    "REFUSAL_TEXT",
    "Bounds",
    "DecisionEntry",
    "LaunchEnd",
    "LaunchEndKind",
    "RecoveryCategory",
    "RecoveryDecision",
    "RecoveryJournal",
    "Refusal",
    "StepKey",
    "audit_record",
    "audit_target",
    "audit_text",
    "categorize",
    "decide",
    "decides",
    "launch_end_of",
]
