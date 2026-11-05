"""Typed stop classification (#369, ADR 0011): what a stop is, never what it permits.

A command of a run ends in one of a few ways, and each allows something
different next. ``BLOCKED`` is persisted and left only by the operator's
``unblock``; ``FAILED`` is persisted and has no outgoing edge; an error that
leaves the persisted phase unchanged is continued by ``resume``; a refusal
acted on nothing; ``READY_FOR_MERGE`` and ``DONE`` are stops by design. This
module names each of them (:class:`StopOutcome`), names why
(:class:`StopReasonCode`), says where that came from (:class:`ReasonSource`)
and which documented operator routes apply (:class:`OperatorRoute`), so that
recovery advice is derived from typed facts instead of being read out of
free text.

Classification is not authorization. Nothing here decides a phase, a
transition, a retry, a write, a model's permission or a budget, and nothing
that does may read a :class:`StopClassification`: ``unblock`` re-reads
GitHub and may refuse whatever the code says, ``resume`` reconciles the
phase entry with GitHub and is held to the run's step budget, and the merge
gate re-verifies. A route is advice about which of those existing commands
applies, never a grant (``tests/test_stop_classification.py`` pins that no
deciding module imports this one).

Every classification comes from a typed source and never from the text of a
reason: a controller sink that named its code (``ControllerEngine._block``),
the validated ``status`` of a ``CONTROL_RESULT``, the class of a raised
:class:`~autoforge.errors.AutoForgeError` (and the one typed attribute the
engine sets on a run log's refusal after a launch's attempt was saved), or
a loaded state's phase and settlement. No code is persisted (ADR 0011 D5): a state loaded later
classifies its ``BLOCKED`` or ``FAILED`` with :attr:`StopReasonCode.UNKNOWN`,
and ``block_reason`` stays text for a human. Where nothing typed says why,
the code is ``UNKNOWN``, and an ``UNKNOWN`` never adds a route.

Pure: no I/O, no clock, no GitHub, no state write.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from . import errors
from .transitions import Phase, Settlement, durable_pause, issue_settlement

if TYPE_CHECKING:
    # Typing only: state imports transitions, and nothing here needs more.
    from .state import AutoForgeState


class StopOutcome(Enum):
    """How a command of a run stopped: the one fact recovery advice depends on."""

    #: Persisted holding state; left only by the operator's ``unblock``.
    BLOCKED = "blocked"
    #: Persisted terminal state with no outgoing edge: a new run, not a recovery.
    FAILED = "failed"
    #: The command failed and the persisted phase is unchanged: ``resume`` re-enters it.
    RESUMABLE_ERROR = "resumable_error"
    #: The command refused before it acted on the run (exit 2): nothing of it ran.
    REFUSED = "refused"
    #: #126's durable pause. Reserved: no state can hold one until #126 lands.
    AWAITING_INPUT = "awaiting_input"
    #: The merge gate is closed at a clean, certified review: a stop by design.
    READY_FOR_MERGE = "ready_for_merge"
    #: A scheduler worker's stop at MERGE or UPDATE_EPIC, which it never executes.
    HANDOFF = "handoff"
    #: The workflow completed.
    DONE = "done"
    #: The command stopped at its own limit (``step``, ``--max-steps``) mid-workflow.
    IN_PROGRESS = "in_progress"
    #: Nothing typed says how the command ended.
    UNKNOWN = "unknown"


class ReasonSource(Enum):
    """Where a classification's reason came from: its provenance."""

    #: A controller sink named its code (``ControllerEngine._block``).
    CONTROLLER = "controller"
    #: A validated CONTROL_RESULT ``status``: the controller applied the outcome,
    #: and the agent's message is a claim, quoted, never evidence.
    AGENT = "agent"
    #: The class of the typed error that ended the command.
    EXCEPTION = "exception"
    #: A loaded state's phase and settlement; no reason code is persisted.
    STATE = "state"


class OperatorRoute(Enum):
    """A documented operator command that applies to a stop. Never a grant:
    each command re-checks everything it acts on, and may refuse."""

    UNBLOCK = "unblock"
    RESUME = "resume"
    REPEAT = "repeat"
    NEW_RUN = "new_run"
    MERGE = "merge"
    MERGE_GATE = "merge_gate"
    SCHEDULER = "scheduler"
    INVESTIGATE = "investigate"


ROUTE_TEXT: dict[OperatorRoute, str] = {
    OperatorRoute.UNBLOCK: (
        "fix the cause, then 'unblock --reason \"...\"' (it re-reads GitHub and may refuse), "
        "then 'resume' ('epic resume' for a scheduler's issue)"
    ),
    OperatorRoute.RESUME: (
        "'resume' (it reconciles the phase with GitHub first, within the run's step budget)"
    ),
    OperatorRoute.REPEAT: "the same command once the cause is fixed",
    OperatorRoute.NEW_RUN: "a new run ('run --force' replaces this one)",
    OperatorRoute.MERGE: (
        "merge the PR by hand on GitHub (and close its issue if the merge does not)"
    ),
    OperatorRoute.MERGE_GATE: (
        "open the merge gate (safety.allow_merge and --allow-merge) and 'resume'"
    ),
    OperatorRoute.SCHEDULER: "'epic resume', or 'epic archive' to release the issue",
    OperatorRoute.INVESTIGATE: "inspect 'status', the run's logs and GitHub; no route is implied",
}


class ReasonFamily(Enum):
    """The group a reason code belongs to (ADR 0011's inventory)."""

    BOUND = "bound"
    OPERATOR_WAIT = "operator_wait"
    IDENTITY = "identity"
    GITHUB = "github"
    MERGE_GATE = "merge_gate"
    WORKSPACE = "workspace"
    AGENT = "agent"
    EXECUTION = "execution"
    CONFIGURATION = "configuration"
    STATE = "state"
    INTENTIONAL = "intentional"
    UNKNOWN = "unknown"


class StopReasonCode(Enum):
    """Why a run stopped, as a typed sink named it. A code says nothing of what may follow."""

    # Bounds a human decides past (BLOCKED).
    STEP_BUDGET = "step-budget"
    REVIEW_ROUND_CAP = "review-round-cap"
    REVIEW_STAGNATION = "review-stagnation"
    REPLAN_REFUSED = "replan-refused"
    PRODUCER_VALIDATION_BOUND = "producer-validation-bound"
    VALIDATION_INTERRUPTED = "validation-interrupted"
    VALIDATION_INCONCLUSIVE = "validation-inconclusive"
    SELECTION_REJECTED = "selection-rejected"
    # K9 / K10: the run waits for the operator (BLOCKED).
    ACCEPTANCE_ACTION = "acceptance-action"
    OPERATOR_ACTIONS = "operator-actions"
    # PR, comment, branch and effect identity (BLOCKED).
    UNJOURNALED_WRITE = "unjournaled-write"
    EFFECT_CONFLICT = "effect-conflict"
    CLAIM_AMBIGUOUS = "claim-ambiguous"
    PR_OWNER = "pr-owner"
    DEFAULT_BRANCH = "default-branch"
    READ_BACK_MISMATCH = "read-back-mismatch"
    FIX_PLAN_REFUSED = "fix-plan-refused"
    EPIC_ROADMAP = "epic-roadmap"
    # GitHub answered conclusively (BLOCKED) or not at all.
    GITHUB_READ_FAILED = "github-read-failed"
    GITHUB_CHECKS_REFUSED = "github-checks-refused"
    GITHUB_UNAVAILABLE = "github-unavailable"
    GITHUB_ERROR = "github-error"
    GIT_TRANSPORT = "git-transport"
    # The merge gate (BLOCKED).
    MERGE_INCONCLUSIVE = "merge-inconclusive"
    PR_IDENTITY = "pr-identity"
    PR_NOT_OPEN = "pr-not-open"
    UNREVIEWED_MERGE = "unreviewed-merge"
    MERGE_CHECKS = "merge-checks"
    MERGE_CONFLICT = "merge-conflict"
    MERGE_NOT_READY = "merge-not-ready"
    MERGE_WRITE = "merge-write"
    PREMERGE_VERIFICATION = "premerge-verification"
    REVIEW_CERTIFICATE = "review-certificate"
    REVIEW_NOT_BACKED = "review-not-backed"
    # The operator's checkout and the agent worktree.
    CHECKOUT_DRIFT = "checkout-drift"
    SANDBOX_TAMPER = "sandbox-tamper"
    # The agent's own CONTROL_RESULT status.
    AGENT_BLOCKED = "agent-blocked"
    AGENT_FAILURE = "agent-failure"
    # A launch or its result (the phase unchanged).
    EXECUTION_FAILED = "execution-failed"
    EXECUTION_TIMEOUT = "execution-timeout"
    SANDBOX_LAUNCH_REFUSED = "sandbox-launch-refused"
    INTERACTION_REQUIRED = "interaction-required"
    CONTROL_RESULT_INVALID = "control-result-invalid"
    VERIFICATION_FAILED = "verification-failed"
    # The run log refused a launch's step directory or record, its attempt persisted.
    RUN_LOG_REFUSED = "run-log-refused"
    # Refusals before the command acted (exit 2).
    CONFIGURATION_INVALID = "configuration-invalid"
    CONFIGURATION_DRIFT = "configuration-drift"
    SANDBOX_CAPABILITY = "sandbox-capability"
    SCHEDULER_OWNED = "scheduler-owned"
    STATE_INVALID = "state-invalid"
    STATE_PROTOCOL = "state-protocol"
    STATE_TRANSITION = "state-transition"
    LOCK_HELD = "lock-held"
    # Stops by design.
    READY_FOR_MERGE = "ready-for-merge"
    HANDOFF = "handoff"
    DONE = "done"
    IN_PROGRESS = "in-progress"
    # #126: reserved, produced by nothing yet.
    AWAITING_INPUT = "awaiting-input"
    # Nothing typed says why.
    UNKNOWN = "unknown"


_C = StopReasonCode
_F = ReasonFamily

# Total over StopReasonCode (``test_stop_classification`` fails without an entry).
REASON_FAMILY: dict[StopReasonCode, ReasonFamily] = {
    _C.STEP_BUDGET: _F.BOUND,
    _C.REVIEW_ROUND_CAP: _F.BOUND,
    _C.REVIEW_STAGNATION: _F.BOUND,
    _C.REPLAN_REFUSED: _F.BOUND,
    _C.PRODUCER_VALIDATION_BOUND: _F.BOUND,
    _C.VALIDATION_INTERRUPTED: _F.BOUND,
    _C.VALIDATION_INCONCLUSIVE: _F.BOUND,
    _C.SELECTION_REJECTED: _F.BOUND,
    _C.ACCEPTANCE_ACTION: _F.OPERATOR_WAIT,
    _C.OPERATOR_ACTIONS: _F.OPERATOR_WAIT,
    _C.UNJOURNALED_WRITE: _F.IDENTITY,
    _C.EFFECT_CONFLICT: _F.IDENTITY,
    _C.CLAIM_AMBIGUOUS: _F.IDENTITY,
    _C.PR_OWNER: _F.IDENTITY,
    _C.DEFAULT_BRANCH: _F.IDENTITY,
    _C.READ_BACK_MISMATCH: _F.IDENTITY,
    _C.FIX_PLAN_REFUSED: _F.IDENTITY,
    _C.EPIC_ROADMAP: _F.IDENTITY,
    _C.GITHUB_READ_FAILED: _F.GITHUB,
    _C.GITHUB_CHECKS_REFUSED: _F.GITHUB,
    _C.GITHUB_UNAVAILABLE: _F.GITHUB,
    _C.GITHUB_ERROR: _F.GITHUB,
    _C.GIT_TRANSPORT: _F.GITHUB,
    _C.MERGE_INCONCLUSIVE: _F.MERGE_GATE,
    _C.PR_IDENTITY: _F.MERGE_GATE,
    _C.PR_NOT_OPEN: _F.MERGE_GATE,
    _C.UNREVIEWED_MERGE: _F.MERGE_GATE,
    _C.MERGE_CHECKS: _F.MERGE_GATE,
    _C.MERGE_CONFLICT: _F.MERGE_GATE,
    _C.MERGE_NOT_READY: _F.MERGE_GATE,
    _C.MERGE_WRITE: _F.MERGE_GATE,
    _C.PREMERGE_VERIFICATION: _F.MERGE_GATE,
    _C.REVIEW_CERTIFICATE: _F.MERGE_GATE,
    _C.REVIEW_NOT_BACKED: _F.MERGE_GATE,
    _C.CHECKOUT_DRIFT: _F.WORKSPACE,
    _C.SANDBOX_TAMPER: _F.WORKSPACE,
    _C.AGENT_BLOCKED: _F.AGENT,
    _C.AGENT_FAILURE: _F.AGENT,
    _C.EXECUTION_FAILED: _F.EXECUTION,
    _C.EXECUTION_TIMEOUT: _F.EXECUTION,
    _C.SANDBOX_LAUNCH_REFUSED: _F.EXECUTION,
    _C.INTERACTION_REQUIRED: _F.EXECUTION,
    _C.CONTROL_RESULT_INVALID: _F.EXECUTION,
    _C.VERIFICATION_FAILED: _F.EXECUTION,
    _C.RUN_LOG_REFUSED: _F.EXECUTION,
    _C.CONFIGURATION_INVALID: _F.CONFIGURATION,
    _C.CONFIGURATION_DRIFT: _F.CONFIGURATION,
    _C.SANDBOX_CAPABILITY: _F.CONFIGURATION,
    _C.SCHEDULER_OWNED: _F.CONFIGURATION,
    _C.STATE_INVALID: _F.STATE,
    _C.STATE_PROTOCOL: _F.STATE,
    _C.STATE_TRANSITION: _F.STATE,
    _C.LOCK_HELD: _F.STATE,
    _C.READY_FOR_MERGE: _F.INTENTIONAL,
    _C.HANDOFF: _F.INTENTIONAL,
    _C.DONE: _F.INTENTIONAL,
    _C.IN_PROGRESS: _F.INTENTIONAL,
    _C.AWAITING_INPUT: _F.INTENTIONAL,
    _C.UNKNOWN: _F.UNKNOWN,
}

_O = StopOutcome
_R = OperatorRoute

# The routes an outcome allows. They restate the existing exit rules and
# change none: BLOCKED is left only by ``unblock`` (or abandoned for a new
# run), FAILED only by a new run, an unchanged phase by ``resume``. A
# refusal's routes depend on what refused it (``REFUSAL_ROUTES``).
OUTCOME_ROUTES: dict[StopOutcome, tuple[OperatorRoute, ...]] = {
    _O.BLOCKED: (_R.UNBLOCK, _R.NEW_RUN),
    _O.FAILED: (_R.INVESTIGATE, _R.NEW_RUN),
    _O.RESUMABLE_ERROR: (_R.RESUME,),
    _O.REFUSED: (),
    # #126 has not landed: no route answers a pause, and none is invented.
    _O.AWAITING_INPUT: (),
    _O.READY_FOR_MERGE: (_R.MERGE, _R.MERGE_GATE),
    _O.HANDOFF: (_R.SCHEDULER,),
    _O.DONE: (),
    _O.IN_PROGRESS: (_R.RESUME,),
    _O.UNKNOWN: (_R.INVESTIGATE,),
}

# What a standalone route is for a scheduler namespace's state (ADR 0008):
# 'resume' and 'run' refuse its state directory (D17.3), so the scheduler's
# commands continue or release it; and its workers stop at READY_FOR_MERGE
# whatever the configuration says (D15.3), so no merge gate a namespace's
# operator opens moves it on (``None``: the route is dropped).
SCHEDULER_ROUTES: dict[OperatorRoute, OperatorRoute | None] = {
    _R.RESUME: _R.SCHEDULER,
    _R.NEW_RUN: _R.SCHEDULER,
    _R.MERGE_GATE: None,
}


def _as_scheduler_routes(routes: tuple[OperatorRoute, ...]) -> tuple[OperatorRoute, ...]:
    scheduled = (SCHEDULER_ROUTES.get(r, r) for r in routes)
    return tuple(dict.fromkeys(r for r in scheduled if r is not None))


REFUSAL_ROUTES: dict[StopReasonCode, tuple[OperatorRoute, ...]] = {
    _C.CONFIGURATION_INVALID: (_R.REPEAT,),
    _C.CONFIGURATION_DRIFT: (_R.REPEAT,),
    _C.SANDBOX_CAPABILITY: (_R.REPEAT,),
    _C.SCHEDULER_OWNED: (_R.SCHEDULER,),
    # A missing state is a new run; a corrupt one is repaired or moved aside
    # by 'run --force'. Which of the two it is, nothing typed says.
    _C.STATE_INVALID: (_R.INVESTIGATE, _R.NEW_RUN),
    _C.STATE_PROTOCOL: (_R.INVESTIGATE,),
    _C.STATE_TRANSITION: (_R.INVESTIGATE,),
    _C.LOCK_HELD: (_R.REPEAT,),
    _C.UNKNOWN: (_R.INVESTIGATE,),
}

# Whether acting on the outcome's route needs live facts (GitHub, the
# worktree) re-read first, as every route that continues a run already does.
# Unknown is conservative.
LIVE_FACTS_REQUIRED: dict[StopOutcome, bool] = {
    _O.BLOCKED: True,
    _O.FAILED: False,
    _O.RESUMABLE_ERROR: True,
    _O.REFUSED: False,
    _O.AWAITING_INPUT: True,
    _O.READY_FOR_MERGE: True,
    _O.HANDOFF: True,
    _O.DONE: False,
    _O.IN_PROGRESS: True,
    _O.UNKNOWN: True,
}


@dataclass(frozen=True)
class StopClassification:
    """One stop, classified from a typed source; grants nothing.

    ``evidence`` names the typed fact the classification rests on (the
    sink and phase, the exception class, the persisted phase), never a
    reason's text, so it is safe to print as it is.
    """

    outcome: StopOutcome
    code: StopReasonCode
    source: ReasonSource
    evidence: str
    #: The parallel EPIC scheduler's view of a namespace's state (ADR 0008):
    #: the scheduler's commands continue or release it, never 'resume' or
    #: 'run', which refuse a namespace's state directory (D17.3), and no merge
    #: gate moves its worker on (D15.3). ``None`` when which view applies is
    #: not known (no git repository to find the scheduler root in, a dry run
    #: that runs no git): only the routes both views share are given.
    scheduler: bool | None = False

    @property
    def family(self) -> ReasonFamily:
        return REASON_FAMILY[self.code]

    @property
    def view(self) -> str:
        """``standalone``, ``scheduler`` or ``unknown``: whose routes these are."""
        if self.scheduler is None:
            return "unknown"
        return "scheduler" if self.scheduler else "standalone"

    @property
    def routes(self) -> tuple[OperatorRoute, ...]:
        if self.outcome is StopOutcome.REFUSED:
            # A refusal's routes depend on its code; a namespace's state, read
            # by the corpus, takes the scheduler's view of them like any other.
            standalone = REFUSAL_ROUTES.get(self.code, (OperatorRoute.INVESTIGATE,))
        else:
            standalone = OUTCOME_ROUTES[self.outcome]
        if self.scheduler is False:
            return standalone
        scheduled = _as_scheduler_routes(standalone)
        if self.scheduler:
            return scheduled
        # Unknown view: a route of one view only could be refused or useless.
        shared = tuple(r for r in standalone if r in scheduled)
        if shared or not standalone:
            return shared
        return (OperatorRoute.INVESTIGATE,)

    @property
    def live_facts_required(self) -> bool:
        return LIVE_FACTS_REQUIRED[self.outcome]

    def line(self) -> str:
        """One line for the operator: the outcome, the code and its source, the routes."""
        head = (
            f"stop: {self.outcome.value} (reason {self.code.value}, from "
            f"{self.source.value}: {self.evidence})"
        )
        routes = self.routes
        if not routes:
            if self.outcome is StopOutcome.AWAITING_INPUT:
                return f"{head}; no route answers a pause until #126 lands"
            return f"{head}; nothing to do"
        return f"{head}; next: " + "; or ".join(ROUTE_TEXT[r] for r in routes)

    def as_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "code": self.code.value,
            "family": self.family.value,
            "source": self.source.value,
            "evidence": self.evidence,
            "view": self.view,
            "routes": [r.value for r in self.routes],
            "live_facts_required": self.live_facts_required,
        }


class CodedReason(str):
    """A BLOCKED reason's text carrying the code the sink that wrote it chose.

    The engine's verify and apply steps return ``(Phase.BLOCKED, reason)``
    to the one ``_block`` that persists it; this keeps that return type a
    ``str`` while the code travels with it. Any string operation returns a
    plain ``str``, so a reason rebuilt on the way loses its code and
    classifies ``UNKNOWN`` (:func:`reason_code`): never a wrong code.
    """

    code: StopReasonCode

    def __new__(cls, code: StopReasonCode, text: str) -> CodedReason:
        made = super().__new__(cls, text)
        made.code = code
        return made


def reason_code(reason: str, default: StopReasonCode = StopReasonCode.UNKNOWN) -> StopReasonCode:
    """The code ``reason`` carries, or ``default`` for plain text (never parsed)."""
    return reason.code if isinstance(reason, CodedReason) else default


def blocked_by_controller(code: StopReasonCode, phase: Phase) -> StopClassification:
    """A BLOCKED a controller sink entered, with the code it named."""
    return StopClassification(
        StopOutcome.BLOCKED,
        code,
        ReasonSource.CONTROLLER,
        f"the controller entered BLOCKED from {phase.value}",
    )


def agent_status(status: str, phase: Phase) -> StopClassification:
    """The outcome the controller applied for a validated ``blocked`` or ``failure`` status."""
    if status == "failure":
        outcome, code = StopOutcome.FAILED, StopReasonCode.AGENT_FAILURE
    elif status == "blocked":
        outcome, code = StopOutcome.BLOCKED, StopReasonCode.AGENT_BLOCKED
    else:
        raise ValueError(f"not a stopping CONTROL_RESULT status: {status!r}")
    return StopClassification(
        outcome, code, ReasonSource.AGENT, f"the {phase.value} agent's status {status!r}"
    )


# The class of an error that ended ``run``, ``step`` or ``resume``, nearest
# first along its MRO. Every class of ``errors.py`` has an entry
# (``test_stop_classification``), so a parent's entry decides only for a
# class defined elsewhere. REFUSED is exactly the classes ``cli.main``
# exits 2 for; every other class exits 1 with the persisted phase unchanged.
# One typed fact overrides the class (:func:`classify_exception`): a run log's
# refusal the engine re-raised after it persisted a launch's attempt
# (``StateError.interrupted_attempt``) still exits 2, but the command acted on
# the run and left its phase for ``resume``, so it is no refusal.
EXCEPTION_CLASSES: dict[type[errors.AutoForgeError], tuple[StopOutcome, StopReasonCode]] = {
    errors.AutoForgeError: (_O.UNKNOWN, _C.UNKNOWN),
    # Refusals: the command did not act on the run.
    errors.ConfigurationError: (_O.REFUSED, _C.CONFIGURATION_INVALID),
    errors.ConfigurationDriftError: (_O.REFUSED, _C.CONFIGURATION_DRIFT),
    errors.EpicBindingError: (_O.REFUSED, _C.CONFIGURATION_INVALID),
    errors.EpicRebindError: (_O.REFUSED, _C.CONFIGURATION_INVALID),
    errors.SandboxCapabilityError: (_O.REFUSED, _C.SANDBOX_CAPABILITY),
    errors.SchedulerStateDirError: (_O.REFUSED, _C.SCHEDULER_OWNED),
    errors.SchedulerOwnedIssueError: (_O.REFUSED, _C.SCHEDULER_OWNED),
    errors.StateError: (_O.REFUSED, _C.STATE_INVALID),
    errors.StateProtocolError: (_O.REFUSED, _C.STATE_PROTOCOL),
    errors.NamespaceError: (_O.REFUSED, _C.STATE_INVALID),
    errors.NamespaceMismatch: (_O.REFUSED, _C.STATE_INVALID),
    errors.SchedulerStateError: (_O.REFUSED, _C.STATE_INVALID),
    errors.ClaimConflict: (_O.REFUSED, _C.STATE_INVALID),
    errors.OpenSchedulerClaimsError: (_O.REFUSED, _C.STATE_INVALID),
    errors.StateTransitionError: (_O.REFUSED, _C.STATE_TRANSITION),
    errors.LockError: (_O.REFUSED, _C.LOCK_HELD),
    errors.IssueBusy: (_O.REFUSED, _C.LOCK_HELD),
    errors.LockProofError: (_O.REFUSED, _C.LOCK_HELD),
    errors.NetworkFilesystemError: (_O.REFUSED, _C.LOCK_HELD),
    # A launch, its result or its verification failed; the phase is unchanged.
    errors.ExecutionError: (_O.RESUMABLE_ERROR, _C.EXECUTION_FAILED),
    errors.ExecutionTimeoutError: (_O.RESUMABLE_ERROR, _C.EXECUTION_TIMEOUT),
    errors.InteractionDataError: (_O.RESUMABLE_ERROR, _C.EXECUTION_FAILED),
    errors.InteractionRequiredError: (_O.RESUMABLE_ERROR, _C.INTERACTION_REQUIRED),
    errors.ChildStdinClosedError: (_O.RESUMABLE_ERROR, _C.EXECUTION_FAILED),
    errors.SandboxLaunchRefusedError: (_O.RESUMABLE_ERROR, _C.SANDBOX_LAUNCH_REFUSED),
    errors.ControlResultError: (_O.RESUMABLE_ERROR, _C.CONTROL_RESULT_INVALID),
    errors.ControlResultValidationError: (_O.RESUMABLE_ERROR, _C.CONTROL_RESULT_INVALID),
    errors.GitHubError: (_O.RESUMABLE_ERROR, _C.GITHUB_ERROR),
    errors.GitHubUnavailableError: (_O.RESUMABLE_ERROR, _C.GITHUB_UNAVAILABLE),
    errors.GitHubNotFoundError: (_O.RESUMABLE_ERROR, _C.GITHUB_ERROR),
    errors.GitHubChecksAccessError: (_O.RESUMABLE_ERROR, _C.GITHUB_CHECKS_REFUSED),
    errors.GitHubPlanLimitedError: (_O.RESUMABLE_ERROR, _C.GITHUB_ERROR),
    errors.GitHubWrongTargetError: (_O.RESUMABLE_ERROR, _C.GITHUB_ERROR),
    errors.GitTransportError: (_O.RESUMABLE_ERROR, _C.GIT_TRANSPORT),
    errors.EffectConflictError: (_O.RESUMABLE_ERROR, _C.EFFECT_CONFLICT),
    errors.ClaimConflictError: (_O.RESUMABLE_ERROR, _C.CLAIM_AMBIGUOUS),
    # The step that meets it blocks (#414); one that escapes a command left
    # the phase as it was.
    errors.PROwnerError: (_O.RESUMABLE_ERROR, _C.PR_OWNER),
    errors.VerificationError: (_O.RESUMABLE_ERROR, _C.VERIFICATION_FAILED),
    errors.CheckoutDriftError: (_O.RESUMABLE_ERROR, _C.CHECKOUT_DRIFT),
    errors.SandboxTamperError: (_O.RESUMABLE_ERROR, _C.SANDBOX_TAMPER),
    errors.CandidateNotMaterializableError: (_O.RESUMABLE_ERROR, _C.VERIFICATION_FAILED),
    errors.MaterializationError: (_O.RESUMABLE_ERROR, _C.VERIFICATION_FAILED),
    errors.ValidationInconclusiveError: (_O.RESUMABLE_ERROR, _C.VALIDATION_INCONCLUSIVE),
    errors.ValidationInterruptedError: (_O.RESUMABLE_ERROR, _C.VALIDATION_INTERRUPTED),
    # Never expected to end a lifecycle command: a scheduler's, a worker's or
    # a controller bug's. Nothing typed says what state they leave.
    errors.GitLockTimeout: (_O.UNKNOWN, _C.UNKNOWN),
    errors.LockOrderError: (_O.UNKNOWN, _C.UNKNOWN),
    errors.WorkerStartupError: (_O.UNKNOWN, _C.UNKNOWN),
    errors.SandboxPolicyError: (_O.UNKNOWN, _C.UNKNOWN),
    errors.SnapshotRefused: (_O.UNKNOWN, _C.UNKNOWN),
    errors.ValidationDefinitionError: (_O.UNKNOWN, _C.UNKNOWN),
}


def classify_exception(exc: BaseException) -> StopClassification:
    """The stop a typed error that ended ``run``, ``step`` or ``resume`` is.

    Decided by the error's class, nearest mapped class first, and by one
    typed attribute; its message is never read. A class says that a
    ``StateError`` exits 2, not that nothing ran: one whose
    ``interrupted_attempt`` the engine set is a run log's refusal after the
    launch's attempt was persisted (the agent may have run, its side effects
    may be on GitHub), the phase is unchanged, and the error itself says to
    repair the log directory and ``resume``. An exception that is no
    :class:`~autoforge.errors.AutoForgeError` is ``UNKNOWN``. Only for the
    lifecycle commands: an error of ``unblock`` leaves the run ``BLOCKED``,
    which ``resume`` refuses, so its routes would be wrong there.
    """
    if isinstance(exc, errors.StateError) and exc.interrupted_attempt is True:
        return StopClassification(
            StopOutcome.RESUMABLE_ERROR,
            StopReasonCode.RUN_LOG_REFUSED,
            ReasonSource.EXCEPTION,
            f"{type(exc).__name__} raised by the run log after the launch's attempt was saved",
        )
    for cls in type(exc).__mro__:
        entry = EXCEPTION_CLASSES.get(cls)
        if entry is not None:
            outcome, code = entry
            return StopClassification(
                outcome, code, ReasonSource.EXCEPTION, f"{type(exc).__name__} raised"
            )
    return StopClassification(
        StopOutcome.UNKNOWN,
        StopReasonCode.UNKNOWN,
        ReasonSource.EXCEPTION,
        f"{type(exc).__name__} raised",
    )


_SETTLED_PHASE: dict[Phase, tuple[StopOutcome, StopReasonCode]] = {
    Phase.BLOCKED: (_O.BLOCKED, _C.UNKNOWN),
    Phase.FAILED: (_O.FAILED, _C.UNKNOWN),
    Phase.DONE: (_O.DONE, _C.DONE),
}


def classify_state(
    state: AutoForgeState,
    *,
    scheduler: bool | None = False,
    paused: Callable[[AutoForgeState], bool] = durable_pause,
) -> StopClassification:
    """The stop a strictly loaded state is, from its phase and settlement alone.

    ``block_reason`` is never read: no code is persisted, so a ``BLOCKED``
    or ``FAILED`` state classifies with :attr:`StopReasonCode.UNKNOWN`,
    which changes no route. ``scheduler`` takes the parallel EPIC
    scheduler's view (ADR 0008 D15.1): a hand-off phase is the scheduler's
    to settle and an unsettled one is continued by ``epic resume``; a
    standalone run continues both with ``resume``. ``None`` is a view not
    known: the outcome is the standalone one, and the routes only those
    both views share (:attr:`StopClassification.routes`). ``paused`` is #126's
    durable pause predicate, checked first, as
    :func:`~autoforge.transitions.issue_settlement` does.
    """
    settlement = issue_settlement(state, paused=paused)
    phase = state.phase
    evidence = f"phase {phase.value}, settlement {settlement.value}"
    if settlement is Settlement.AWAITING_INPUT:
        outcome, code = _O.AWAITING_INPUT, _C.AWAITING_INPUT
    elif settlement is Settlement.TERMINAL:
        outcome, code = _SETTLED_PHASE[phase]
    elif settlement is Settlement.READY_FOR_MERGE:
        outcome, code = _O.READY_FOR_MERGE, _C.READY_FOR_MERGE
    elif settlement is Settlement.HANDOFF and scheduler:
        outcome, code = _O.HANDOFF, _C.HANDOFF
    else:
        outcome, code = _O.IN_PROGRESS, _C.IN_PROGRESS
    return StopClassification(outcome, code, ReasonSource.STATE, evidence, scheduler=scheduler)
