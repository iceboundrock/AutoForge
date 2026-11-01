"""What the ``autoforge epic`` commands do besides running the scheduler (ADR 0008; #183).

``epic run`` and ``epic resume`` run :mod:`autoforge.scheduler`; the CLI
wires them. This module holds the rest, so the CLI only parses and prints:

* **``epic status``** (:func:`collect_status`): lock-free and read-only. It
  never probes L0, takes no lock and creates nothing. GitHub gives the
  snapshot (#179's four reads); each child's namespace is read strictly
  without a lock (:func:`~autoforge.scheduler_state.read_namespace`),
  classified for the EPIC (:func:`~autoforge.scheduler_state.classify_for_epic`)
  and probed (:func:`~autoforge.locking.issue_run_lock_live`, D8.3). Each
  child gets exactly one :class:`Category` (:func:`categorize`, pure) from
  the snapshot, the claim, the state and the probe, never from a log, a
  timestamp or a PID. A claim's worker PID and process start time are
  shown as diagnostics only: nothing is ever signalled.
* **``epic run --dry-run``** (:func:`dry_run_plan`, D9.8): the snapshot,
  the ordered verdicts and the admission plan, from the same lock-free
  reads; no lock, namespace, claim, worktree or worker.
* **``epic stop``** (:func:`stop_scheduler`, D16.4): #182's
  :func:`~autoforge.scheduler_stop.request_stop`, reported.
* **``epic archive``** (:func:`archive_issue`, D17.4): under the caller's L0
  and its own L2, the claim closed first and the state moved aside second;
  the logs, the worktree, the binding and the claim history are kept.
* **``epic run``'s refusal and ``--force``** (:func:`epic_run_guard`), run by
  the scheduler holding L0 before it writes anything, and the end-of-run
  check that no claim was left open (:func:`claims_left_open`).
* **The checkout's EPIC** (:func:`checkout_epic`): every ``epic`` command
  first refuses an EPIC of another repository than this checkout's
  ``origin``, before anything is read or written here. ``epic stop`` and
  ``epic archive`` also refuse a ``scheduler.json`` or a binding recorded
  for another repository, by its numeric id read from GitHub, so a rename
  is no refusal and a name taken by another repository is one
  (:func:`check_recorded_repository`); ``epic status`` reports either as a
  problem.

Nothing here names a provider, writes GitHub, records an effect or parses an
agent's result.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .epic_graph import (
    ChildVerdict,
    EpicSnapshot,
    GraphReader,
    LocalFact,
    Shape,
    VerdictKind,
    build_epic_snapshot,
    evaluate,
    ready_set,
)
from .errors import (
    ConfigurationError,
    ExecutionError,
    GitHubError,
    IssueBusy,
    LockError,
    NamespaceMismatch,
    OpenSchedulerClaimsError,
    SchedulerStateError,
    SnapshotRefused,
    StateError,
)
from .executor import ExecutionRequest, ExecutionResult, execute
from .git_transport import local_git_request
from .locking import IssueRunLock, issue_run_lock_live, repository_lock_path
from .ownership import StandaloneHold, admission_refusal, standalone_hold
from .redaction import redact, redact_dict
from .run_namespace import (
    EpicRef,
    IssueNamespace,
    NamespaceLookup,
    RejectedEntry,
    RunBinding,
    StateStatus,
    archive_issue_state,
    issue_namespace,
    list_issue_namespaces,
    load_issue_namespace,
)
from .scheduler import SPAWN_FAILURE_DETAILS
from .scheduler_state import (
    ArchiveAction,
    Claim,
    ClaimArchive,
    ClaimClassification,
    ClassificationKind,
    ClosedClaim,
    EpicClassification,
    ExpectedIdentity,
    NamespaceRead,
    OpenClaim,
    SchedulerState,
    UncertainReason,
    archive_claim,
    classify_for_epic,
    load_scheduler_state,
    open_claims_for_epic,
    quarantine_scheduler_state,
    read_namespace,
)
from .scheduler_stop import StopMode, StopRead, read_stop_request, request_stop
from .state import AutoForgeState
from .transitions import Phase, Settlement
from .validation import GitHubIssueRef, parse_issue_url, parse_remote_repository

STATUS_SCHEMA = "autoforge.epic-status/1"
# ``epic run`` and ``epic resume`` end with claims of the EPIC still open: an
# uncertain namespace's, or one whose run lock a process the scheduler did not
# start held to the end. Exit 1 is the scheduler's global uncertainty
# (``degraded``); 2 a held lock or a refusal; 130 a stop or an interrupt.
EXIT_OPEN_CLAIMS = 3
# A BLOCKED state's reason, as status quotes it, and a whole row's detail.
MAX_BLOCK_REASON_CHARS = 500
MAX_DETAIL_CHARS = 2000
# What the operator's ``unblock --reason`` is shown as in a printed command.
REASON_PLACEHOLDER = "<why>"

DRAIN_HELP = (
    "a drain cannot be escalated by another 'epic stop': press Ctrl-C in the scheduler's "
    "terminal to interrupt its workers"
)

Runner = Callable[[ExecutionRequest], ExecutionResult]


class Category(Enum):
    """One child's ``epic status`` category: exactly one per child (#183)."""

    #: Open, with a blocker not closed as completed.
    BLOCKED_BY_DEPENDENCY = "blocked-by-dependency"
    #: Open, unblocked, and nothing local holds it: the next admission takes it.
    READY = "ready"
    #: A claim is open and no live process holds the run lock: ``epic resume``.
    CLAIMED = "claimed"
    #: A live process holds the issue's run lock.
    RUNNING = "running"
    #: The state is at ``READY_FOR_MERGE``: a human merges.
    READY_FOR_MERGE = "ready-for-merge"
    #: The state is ``BLOCKED``: ``unblock`` on the namespace, then ``epic resume``.
    BLOCKED = "blocked"
    #: An in-progress state, no claim open: the next admission resumes it.
    RESUMABLE = "resumable"
    #: The namespace cannot be trusted: ``epic archive`` or a fix by hand.
    UNCERTAIN = "uncertain"
    #: ``DONE``, ``FAILED``, a hand-off phase or a pause: no more automatic work.
    SETTLED = "settled"
    #: Not admitted for a reason of its own (:class:`HoldReason`).
    HELD = "held"
    #: A child the scheduler never admits (another repository, a nested EPIC).
    UNSUPPORTED = "unsupported"
    #: Closed on GitHub.
    CLOSED = "closed"


class HoldReason(Enum):
    STANDALONE_STATE = "standalone-state"
    OTHER_EPIC = "other-epic"
    SPAWN_FAILED = "spawn-failed"


def command(flags: Iterable[str], *words: str) -> str:
    """An ``autoforge`` command line as the operator types it, ``--config`` included."""
    return shlex.join(["autoforge", *flags, *words])


def _bounded(text: str, limit: int) -> str:
    text = redact(text)
    if len(text) <= limit:
        return text
    marker = " [... truncated]"
    return text[: limit - len(marker)] + marker


def coordination_root_of(repository: str | Path) -> Path:
    """The git common dir L0 is keyed by: the scheduler's coordination root (D5.3)."""
    return repository_lock_path(repository).parent.parent


def checkout_repository(workdir: str | Path, runner: Runner = execute) -> str:
    """``owner/repo`` of the checkout containing ``workdir``: its ``origin`` remote.

    Read locally with the controller's hardened git, as ``doctor`` reads it,
    so ``epic stop`` and ``epic archive`` check it too without a GitHub
    read. No ``origin``, or one that is not a github.com repository, raises
    :class:`~autoforge.errors.ConfigurationError`: there is then nothing to
    bind an EPIC to.
    """
    try:
        res = runner(local_git_request(["remote", "get-url", "origin"], cwd=str(workdir)))
    except ExecutionError as exc:
        raise ConfigurationError(
            f"cannot read this checkout's 'origin' remote: git could not be run ({exc})"
        ) from exc
    if res.timed_out or res.truncated or res.exit_code != 0:
        detail = (res.stderr or res.stdout).strip().splitlines()
        shown = detail[0] if detail else f"git remote get-url exited {res.exit_code}"
        raise ConfigurationError(
            "an 'epic' command runs on an EPIC of this checkout's own repository, and its "
            f"'origin' remote cannot be read ({_bounded(shown, 500)})"
        )
    try:
        return parse_remote_repository(res.stdout)
    except ConfigurationError as exc:
        raise ConfigurationError(
            "an 'epic' command runs on an EPIC of this checkout's own repository, and its "
            f"'origin' remote does not name one: {_bounded(str(exc), 500)}"
        ) from exc


def checkout_epic(workdir: str | Path, epic_url: str, runner: Runner = execute) -> GitHubIssueRef:
    """The EPIC, refused unless it belongs to this checkout's repository (#183).

    Every ``epic`` command calls it first, before it takes a lock or reads
    the coordination root or GitHub. That root is this checkout's, and
    ``scheduler.json``, the stop request and every namespace are found by
    EPIC and issue number: another repository's EPIC would otherwise write
    its bindings and claims here (global uncertainty for every later
    scheduler), or stop or archive this checkout's EPIC of the same number.
    The names are compared case-insensitively, as GitHub does.
    """
    epic = parse_issue_url(epic_url)
    home = checkout_repository(workdir, runner)
    if home.lower() != epic.repository.lower():
        raise ConfigurationError(
            f"{epic.canonical} is an EPIC of {epic.repository}, and this checkout's 'origin' "
            f"is {home}: an 'epic' command runs only on an EPIC of the checkout's own "
            "repository; nothing was written"
        )
    return epic


def check_recorded_repository(
    subject: str,
    recorded: str,
    recorded_id: int,
    epic: GitHubIssueRef,
    repository_id: Callable[[str], int],
    *,
    refused: str,
) -> None:
    """Refuse a file recorded for another repository than the EPIC's (D5.1).

    ``recorded`` is the file's ``owner/name``, display only: a rename leaves
    the old name in ``scheduler.json`` until the next scheduler start, and
    in a binding for good, while a repository transferred or deleted leaves
    its name free for another one. So a name proves nothing either way, and
    only the numeric repository id tells the same repository from another:
    ``repository_id`` reads it from GitHub for the EPIC's repository, every
    time, and the recorded id must be it. Another id, or no answer, raises
    :class:`~autoforge.errors.SchedulerStateError`; the caller then writes
    nothing.
    """
    if recorded.lower() == epic.repository.lower():
        shown = recorded
        what = "another repository that took the name since"
    else:
        shown = f"{recorded}, not {epic.repository},"
        what = "a rename from another repository"
    try:
        live = repository_id(epic.repository)
    except (GitHubError, ConfigurationError, ExecutionError) as exc:
        raise SchedulerStateError(
            f"{subject} {shown} and the repository id that tells {what} cannot be read from "
            f"GitHub ({_bounded(str(exc), 500)}): {refused}"
        ) from exc
    if live != recorded_id:
        raise SchedulerStateError(
            f"{subject} {recorded} (repository id {recorded_id}), and {epic.repository} is "
            f"repository id {live}: {refused}"
        )


# -- one child's category (pure) --------------------------------------------------------


@dataclass(frozen=True)
class IssueFacts:
    """What status read about one issue; :func:`categorize` decides from these alone.

    ``verdict`` is the graph's own verdict (no local facts), ``None`` for a
    namespace the snapshot does not list or when no snapshot was taken.
    ``read`` is the namespace read lock-free, ``None`` when there is none;
    ``found`` its classification for the EPIC. ``probe_problem`` is set when
    the run lock could not be probed. ``standalone`` is the configured
    standalone state when it holds this issue; ``spawn_failed`` this
    invocation's claim of the issue closed ``abandoned`` because its worker
    never started. ``unidentified`` says why the namespace could not be
    classified (neither GitHub nor ``scheduler.json`` gave the identities).
    """

    number: int
    verdict: ChildVerdict | None = None
    read: NamespaceRead | None = None
    found: EpicClassification | None = None
    live: bool = False
    probe_problem: str = ""
    standalone: StandaloneHold | None = None
    spawn_failed: ClosedClaim | None = None
    unidentified: str = ""

    @property
    def lookup(self) -> NamespaceLookup | None:
        read = self.read
        return (
            read.lookup if read is not None and isinstance(read.lookup, NamespaceLookup) else None
        )

    @property
    def state(self) -> AutoForgeState | None:
        lookup = self.lookup
        if lookup is None or lookup.state.status is not StateStatus.LOADED:
            return None
        return lookup.state.state

    @property
    def open_claim(self) -> Claim | None:
        """The open claim on disk, as the classification read it, else the claim file's."""
        found = self.found
        if found is not None and found.classification is not None:
            if found.classification.open_claim is not None:
                return found.classification.open_claim
        return None if self.read is None else self.read.claim.open_claim


@dataclass(frozen=True)
class IssueStatus:
    """One row of ``epic status``.

    ``category`` is ``None`` only for a namespace outside the snapshot that
    holds nothing (no claim, no state, no live owner). ``next`` are the
    commands to run, in order. ``problem`` is set for an ``uncertain`` row
    only (a namespace that cannot be read, classified or probed): it is one
    of the report's problems, never a key of the row's document.
    """

    number: int
    category: Category | None
    detail: str
    hold_reason: HoldReason | None = None
    verdict: str | None = None
    blockers: tuple[str, ...] = ()
    phase: str | None = None
    settlement: str | None = None
    classification: str | None = None
    pr_url: str | None = None
    state_dir: str | None = None
    live: bool = False
    orphaned: bool = False
    claim: Claim | None = None
    next: tuple[str, ...] = ()
    problem: str = ""

    def to_dict(self) -> dict:
        claim = self.claim
        return {
            "number": self.number,
            "category": None if self.category is None else self.category.value,
            "hold_reason": None if self.hold_reason is None else self.hold_reason.value,
            "detail": self.detail,
            "verdict": self.verdict,
            "blockers": list(self.blockers),
            "phase": self.phase,
            "settlement": self.settlement,
            "classification": self.classification,
            "pr_url": self.pr_url,
            "state_dir": self.state_dir,
            "live": self.live,
            "orphaned": self.orphaned,
            "claim": None if claim is None else _claim_dict(claim),
            "next": list(self.next),
        }


def _claim_dict(claim: Claim) -> dict:
    worker = claim.worker
    return {
        "claim_id": claim.claim_id,
        "generation": claim.generation,
        "kind": claim.kind.value,
        "invocation_id": claim.invocation_id,
        "created_at": claim.created_at,
        "worker": None if worker is None else worker.to_dict(),
    }


def _claim_line(claim: Claim) -> str:
    return (
        f"claim generation {claim.generation} ({claim.kind.value}) of invocation "
        f"{claim.invocation_id}, opened {claim.created_at}"
    )


def worker_diagnostics(claim: Claim) -> str:
    """A claim's worker as an operator recognises it: diagnostics, never authority (D7.3)."""
    worker = claim.worker
    if worker is None:
        return "no worker recorded"
    started = "unknown" if worker.process_start_time is None else str(worker.process_start_time)
    return (
        f"worker diagnostics (never signalled; a PID may be reused): pid {worker.pid}, "
        f"process start time {started}, spawned {worker.spawned_at}, log {worker.log}"
    )


def categorize(
    facts: IssueFacts, *, epic_url: str, flags: tuple[str, ...] = (), orphaned: bool = False
) -> IssueStatus:
    """The one :class:`Category` of an issue and what to do about it; pure.

    First match wins: a probe that failed (uncertain); a live run lock
    (running); an uncertain namespace; a namespace of another EPIC (held);
    an open claim (claimed); ``DONE``, ``FAILED``, a hand-off phase or a
    pause (settled); closed and unsupported on GitHub; ``READY_FOR_MERGE``;
    ``BLOCKED``; an unsatisfied blocker (blocked by dependency); the
    configured standalone state's issue and a worker that never started
    this invocation (held); an in-progress state (resumable); ready.
    """
    found = facts.found
    classification = found.classification if found is not None else None
    state = facts.state
    namespace = None if facts.read is None else facts.read.namespace
    verdict = facts.verdict
    claim = facts.open_claim
    resume = command(flags, "epic", "resume", epic_url)
    archive = command(flags, "epic", "archive", epic_url, str(facts.number))

    def row(
        category: Category | None,
        detail: str,
        *,
        hold_reason: HoldReason | None = None,
        blockers: tuple[str, ...] = (),
        steps: tuple[str, ...] = (),
        problem: str = "",
    ) -> IssueStatus:
        return IssueStatus(
            number=facts.number,
            category=category,
            detail=_bounded(detail, MAX_DETAIL_CHARS),
            hold_reason=hold_reason,
            verdict=None if verdict is None else verdict.reason,
            blockers=blockers,
            phase=None if state is None else state.phase.value,
            settlement=(
                None
                if classification is None or classification.settlement is None
                else classification.settlement.value
            ),
            classification=None if classification is None else str(classification),
            pr_url=(state.current_pr_url or None) if state is not None else None,
            state_dir=None if namespace is None else str(namespace.state_dir),
            live=facts.live,
            orphaned=orphaned,
            claim=claim,
            next=steps,
            problem=_bounded(problem, MAX_DETAIL_CHARS),
        )

    if facts.probe_problem:
        return row(
            Category.UNCERTAIN,
            f"its run lock cannot be probed ({facts.probe_problem}); repair the namespace by "
            "hand or archive it",
            steps=(archive, resume),
            problem=f"its run lock cannot be probed ({facts.probe_problem})",
        )
    if facts.live:
        if claim is not None:
            detail = f"a live process holds its run lock; {_claim_line(claim)}"
        else:
            detail = (
                "a live process holds its run lock and no claim is open (an admission in "
                "progress, or a process the scheduler did not start)"
            )
        return row(Category.RUNNING, detail)
    if facts.unidentified:
        return row(Category.UNCERTAIN, facts.unidentified, problem=facts.unidentified)
    newer = facts.lookup.state.newer_protocol if facts.lookup is not None else ""
    if (
        classification is not None
        and classification.uncertain is UncertainReason.STATE_CORRUPT
        and newer
    ):
        # A newer controller's state (#265): it continues the run, so neither
        # setting the state aside nor repairing it is advice here.
        return row(
            Category.UNCERTAIN,
            f"{classification}: {classification.detail}; a controller that speaks protocol "
            f"{newer} reads and continues it ('epic status' and 'epic resume' with that "
            "controller, from this checkout); leave the namespace as it is",
            problem=f"{classification}: {classification.detail}",
        )
    if classification is not None and classification.kind is ClassificationKind.UNCERTAIN:
        return row(
            Category.UNCERTAIN,
            f"{classification}: {classification.detail}; 'epic archive' moves the claim "
            "and the state aside (keeping the logs, the worktree and the binding), or repair "
            "the files by hand",
            steps=(archive, resume),
            problem=f"{classification}: {classification.detail}",
        )
    if found is not None and found.fact is LocalFact.HELD_OTHER_EPIC:
        return row(
            Category.HELD,
            f"{found.detail}: that EPIC's scheduler runs it",
            hold_reason=HoldReason.OTHER_EPIC,
        )
    if claim is not None:
        phase = "no state yet" if state is None else f"the state is at {state.phase.value}"
        return row(
            Category.CLAIMED,
            f"{_claim_line(claim)} is open and no live process holds its run lock ({phase}): "
            "its scheduler ended without closing it; 'epic resume' classifies it",
            steps=(resume,),
        )
    settlement = None if classification is None else classification.settlement
    closed = verdict is not None and verdict.kind is VerdictKind.CLOSED
    if state is not None and settlement is not None and _settled(state.phase, settlement):
        if closed:
            return row(
                Category.SETTLED,
                f"the state is {settlement.value} (phase {state.phase.value}); the issue is closed",
            )
        return row(
            Category.SETTLED,
            f"the state is {settlement.value} (phase {state.phase.value}) and the issue is "
            "open: the scheduler launches nothing more for it until 'epic archive' releases "
            "it for a fresh run",
            steps=(archive, resume),
        )
    if verdict is not None and verdict.kind is VerdictKind.CLOSED:
        return row(Category.CLOSED, verdict.explain())
    if verdict is not None and verdict.kind is VerdictKind.UNSUPPORTED:
        return row(Category.UNSUPPORTED, verdict.explain())
    if state is not None and state.phase is Phase.READY_FOR_MERGE:
        pr = state.current_pr_url or "its PR"
        return row(
            Category.READY_FOR_MERGE,
            f"{pr} is at READY_FOR_MERGE: a human merges it (scheduler workers never "
            "merge); its dependents become ready once the issue is closed as completed",
        )
    if state is not None and state.phase is Phase.BLOCKED and namespace is not None:
        reason = _bounded(state.block_reason or "no reason recorded", MAX_BLOCK_REASON_CHARS)
        unblock = command(
            flags,
            "--state-dir",
            str(namespace.state_dir),
            "unblock",
            "--reason",
            REASON_PLACEHOLDER,
        )
        status = command(flags, "--state-dir", str(namespace.state_dir), "status")
        return row(
            Category.BLOCKED,
            f"BLOCKED: {reason}. 'unblock' on the namespace, while no scheduler runs (it "
            "takes the controller lock), re-inspects GitHub and re-enters a phase (an "
            f"acceptance action a FIX plan awaits is listed by '{status}' and decided with "
            "'unblock --approve-action' or '--decline-action'; an operator action, listed "
            "there too, is posted by the operator ('status --action X-...') or bound with "
            "'unblock --bind-evidence X-... <comment URL>', or declined with "
            "'--decline-action X-...'); 'epic resume' then continues it",
            steps=(unblock, resume),
        )
    if verdict is not None and verdict.kind is VerdictKind.BLOCKED:
        home = verdict.repository
        return row(
            Category.BLOCKED_BY_DEPENDENCY,
            verdict.explain(),
            blockers=tuple(status.describe(home) for status in verdict.blockers),
        )
    if facts.standalone is not None:
        hold = facts.standalone
        return row(
            Category.HELD,
            f"the standalone state {hold.state_file} runs it (phase {hold.phase}): finish it "
            "with the standalone commands; the scheduler admits the issue once that run is DONE",
            hold_reason=HoldReason.STANDALONE_STATE,
        )
    if facts.spawn_failed is not None:
        failed = facts.spawn_failed
        return row(
            Category.HELD,
            f"{failed.detail}: held for the rest of invocation {failed.claim.invocation_id}; "
            "the next 'epic run' or 'epic resume' tries again",
            hold_reason=HoldReason.SPAWN_FAILED,
        )
    if classification is not None and classification.kind is ClassificationKind.RESUMABLE:
        return row(
            Category.RESUMABLE,
            f"{classification.detail}: the next admission resumes it",
            steps=(resume,),
        )
    if verdict is not None and verdict.kind is VerdictKind.READY:
        return row(Category.READY, verdict.explain())
    return row(None, "a namespace holding no claim and no state: nothing to do")


def _settled(phase: Phase, settlement: Settlement) -> bool:
    """``DONE``, ``FAILED``, a hand-off phase or a pause: not ``READY_FOR_MERGE``, ``BLOCKED``."""
    if settlement in (Settlement.HANDOFF, Settlement.AWAITING_INPUT):
        return True
    return settlement is Settlement.TERMINAL and phase is not Phase.BLOCKED


# -- reading (lock-free) ----------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    """Who the EPIC and its repository are: from GitHub, else from ``scheduler.json``."""

    epic: EpicRef
    repository_id: int


def _probe(namespace: IssueNamespace) -> tuple[bool, str]:
    try:
        return issue_run_lock_live(namespace), ""
    except (LockError, StateError, OSError) as exc:
        return False, _bounded(str(exc), 500)


def _same_repository(url: str, repository: str) -> bool:
    try:
        return parse_issue_url(url).repository.lower() == repository.lower()
    except ConfigurationError:
        return False


def _spawn_failed(
    lookup_claims: tuple[ClosedClaim, ...], scheduler: SchedulerState | None
) -> ClosedClaim | None:
    """This invocation's last claim closed ``abandoned`` because its worker never started."""
    if scheduler is None or scheduler.invocation.ended_at is not None or not lookup_claims:
        return None
    last = lookup_claims[-1]
    if (
        last.classification is ClaimClassification.ABANDONED
        and last.claim.invocation_id == scheduler.invocation.invocation_id
        and last.detail.startswith(SPAWN_FAILURE_DETAILS)
    ):
        return last
    return None


def read_issue(
    root: Path,
    number: int,
    *,
    verdict: ChildVerdict | None,
    identity: Identity | None,
    expected: ExpectedIdentity | None,
    scheduler: SchedulerState | None,
    standalone: StandaloneHold | None,
    unidentified: str = "",
) -> IssueFacts:
    """One issue's facts, read lock-free; nothing is created (D8.3)."""
    if verdict is not None and verdict.shape is Shape.FOREIGN_REPOSITORY:
        return IssueFacts(number, verdict)  # another repository's number: no namespace here
    namespace = issue_namespace(root, number)
    read = read_namespace(namespace)
    live, problem = _probe(namespace)
    if read.absent:
        return IssueFacts(number, verdict, live=live, probe_problem=problem, standalone=standalone)
    found: EpicClassification | None = None
    if identity is not None and expected is not None:
        found = classify_for_epic(read, identity.epic, expected=expected)
    claim_file = read.claim.claim_file
    return IssueFacts(
        number,
        verdict,
        read,
        found,
        live=live,
        probe_problem=problem,
        standalone=standalone,
        spawn_failed=_spawn_failed(() if claim_file is None else claim_file.history, scheduler),
        unidentified=unidentified,
    )


def _bound_elsewhere(
    lookup: NamespaceLookup | None, identity: Identity | None, epic_number: int
) -> bool:
    """A namespace of this repository bound to another EPIC: not this EPIC's business."""
    if lookup is None or lookup.binding is None:
        return False
    binding = lookup.binding
    if identity is not None and binding.run_key.repository_id != identity.repository_id:
        return False  # another repository's binding: global uncertainty, shown
    return binding.epic.number != epic_number


@dataclass(frozen=True)
class StatusReport:
    """Everything ``epic status`` shows (schema :data:`STATUS_SCHEMA`)."""

    repository: str
    epic_url: str
    epic_number: int
    epic_issue_id: int | None
    flags: tuple[str, ...]
    scheduler: SchedulerState | None = None
    scheduler_problem: str = ""
    stop: StopRead | None = None
    stop_problem: str = ""
    snapshot: EpicSnapshot | None = None
    snapshot_problem: str = ""
    children: tuple[IssueStatus, ...] = ()
    other_namespaces: tuple[IssueStatus, ...] = ()
    rejected: tuple[RejectedEntry, ...] = ()
    problems: tuple[str, ...] = ()

    @property
    def any_live(self) -> bool:
        return any(row.live for row in (*self.children, *self.other_namespaces))

    @property
    def invocation_hint(self) -> str:
        """The not-ended hint: an invocation ``scheduler.json`` shows running while none is."""
        state = self.scheduler
        if state is None or state.invocation.ended_at is not None or self.any_live:
            return ""
        resume = command(self.flags, "epic", "resume", self.epic_url)
        return (
            "not ended: if no 'autoforge epic' process is running, it ended without "
            f"finalizing (crash or kill); run '{resume}'"
        )

    @property
    def exit_code(self) -> int:
        """0, or 1 when GitHub, ``scheduler.json`` or the namespaces could not all be read.

        An ``uncertain`` namespace (one that cannot be read, classified or
        probed), a child's or an orphaned one, a binding or a
        ``scheduler.json`` of another repository, and an entry under
        ``issues/`` that is not a namespace (:attr:`rejected`) are among
        :attr:`problems`.
        """
        return 1 if (self.snapshot is None or self.scheduler_problem or self.problems) else 0

    def to_dict(self) -> dict:
        state = self.scheduler
        stop = self.stop
        request = None if stop is None else stop.request
        stop_problem = self.stop_problem or (stop.problem if stop is not None else "")
        invocation = None
        if state is not None:
            invocation = {
                **state.invocation.to_dict(),
                "ended": state.invocation.ended_at is not None,
                "hint": self.invocation_hint or None,
            }
        if state is not None:
            scheduler_state = "readable"
        elif self.scheduler_problem:
            scheduler_state = "unreadable"
        else:
            scheduler_state = "absent"
        snapshot = self.snapshot
        document = {
            "schema": STATUS_SCHEMA,
            "repository": self.repository,
            "epic": {
                "number": self.epic_number,
                "url": self.epic_url,
                "issue_id": self.epic_issue_id,
            },
            "scheduler": {
                "state": scheduler_state,
                "problem": self.scheduler_problem or None,
                "invocation": invocation,
                "admission": None if state is None else state.admission.to_dict(),
                "max_parallel": None if state is None else state.config.max_parallel,
                "last_snapshot": (
                    None
                    if state is None or state.last_snapshot is None
                    else state.last_snapshot.to_dict()
                ),
            },
            "stop_request": {
                "present": bool(stop is not None and stop.present),
                "mode": None if stop is None or stop.mode is None else stop.mode.value,
                "invocation_id": None if request is None else request.invocation_id,
                "requested_at": None if request is None else request.requested_at,
                "applies": bool(
                    stop is not None
                    and state is not None
                    and stop.applies_to(state.invocation.invocation_id)
                ),
                "problem": stop_problem or None,
            },
            "snapshot": {
                "taken": snapshot is not None,
                "digest": None if snapshot is None else snapshot.digest,
                "observed_at": None if snapshot is None else snapshot.observed_at,
                "problem": self.snapshot_problem or None,
            },
            "problems": list(self.problems),
            "children": [row.to_dict() for row in self.children],
            "other_namespaces": [row.to_dict() for row in self.other_namespaces],
            "rejected": [{"name": entry.name, "reason": entry.reason} for entry in self.rejected],
        }
        return redact_dict(document)

    def lines(self) -> list[str]:
        """The human rendering, redacted."""
        out = [f"EPIC #{self.epic_number} of {self.repository} ({self.epic_url})"]
        state = self.scheduler
        if state is not None:
            inv = state.invocation
            ended = (
                f"ended {inv.ended_at} ({inv.end_reason.value})"
                if inv.ended_at is not None and inv.end_reason is not None
                else self.invocation_hint or "not ended: a worker of it is running"
            )
            out.append(
                f"last scheduler invocation: {inv.invocation_id}, started {inv.started_at}; {ended}"
            )
            admission = state.admission
            reason = f" ({admission.reason.value})" if admission.reason is not None else ""
            out.append(
                f"  admission {admission.status.value}{reason}, max_parallel "
                f"{state.config.max_parallel}"
            )
            last = state.last_snapshot
            if last is not None:
                counts = ", ".join(f"{k} {v}" for k, v in last.to_dict()["counts"].items())
                out.append(f"  last snapshot {last.digest[:12]} at {last.observed_at}: {counts}")
        elif self.scheduler_problem:
            out.append(
                f"scheduler.json cannot be read: {self.scheduler_problem} ('epic run --force' "
                "moves it aside)"
            )
        else:
            out.append("no scheduler has run for this EPIC here (no scheduler.json)")
        stop = self.stop
        if self.stop_problem:
            out.append(f"stop request: cannot be read ({self.stop_problem})")
        elif stop is not None and stop.present:
            mode = stop.mode.value if stop.mode is not None else "drain"
            if stop.request is None:
                out.append(f"stop request: does not parse, read as a drain ({stop.problem})")
            else:
                applies = (
                    "for the last invocation"
                    if state is not None and stop.applies_to(state.invocation.invocation_id)
                    else "of an earlier invocation (stale; the next scheduler removes it)"
                )
                out.append(f"stop request: {mode}, {applies}, at {stop.request.requested_at}")
        if self.snapshot is not None:
            snapshot = self.snapshot
            out.append(
                f"snapshot {snapshot.digest[:12]} at {snapshot.observed_at}: "
                f"{len(snapshot.children)} children"
            )
        else:
            out.append(f"no snapshot: {self.snapshot_problem}")
        for problem in self.problems:
            out.append(f"problem: {problem}")
        if self.children:
            out.append("children, in admission order:")
            out.extend(_row_lines(row) for row in self.children)
        if self.other_namespaces:
            out.append(
                "namespaces of this EPIC the snapshot does not list:"
                if self.snapshot is not None
                else "namespaces of this EPIC:"
            )
            out.extend(_row_lines(row) for row in self.other_namespaces)
        return [redact(line) for line in out]


def _row_lines(row: IssueStatus) -> str:
    category = "-" if row.category is None else row.category.value
    if row.hold_reason is not None:
        category += f":{row.hold_reason.value}"
    lines = [f"  #{row.number} {category}: {row.detail}"]
    if row.claim is not None:
        lines.append(f"      {worker_diagnostics(row.claim)}")
    lines.extend(f"      next: {step}" for step in row.next)
    return "\n".join(lines)


def _read_scheduler(root: Path, epic_number: int) -> tuple[SchedulerState | None, str]:
    try:
        return load_scheduler_state(root, epic_number), ""
    except (StateError, OSError) as exc:
        return None, _bounded(str(exc), 1000)


def _identity(
    graph: GraphReader, epic: GitHubIssueRef, scheduler: SchedulerState | None
) -> tuple[Identity | None, str]:
    """The EPIC's identity from GitHub, else the last invocation's ``scheduler.json``."""
    try:
        repository_id = graph.get_repository_id(epic.repository)
        node = graph.get_issue_node(epic.canonical)
        return Identity(EpicRef(epic.number, node.id), repository_id), ""
    except GitHubError as exc:
        problem = f"cannot read the repository or the EPIC on GitHub: {_bounded(str(exc), 500)}"
    if scheduler is not None:
        return Identity(scheduler.epic, scheduler.repository_id), problem
    return None, problem


def _snapshot(
    graph: GraphReader, epic: GitHubIssueRef, identity: Identity | None, problem: str
) -> tuple[EpicSnapshot | None, str]:
    if problem:
        return None, problem
    assert identity is not None
    try:
        return build_epic_snapshot(graph, epic.canonical, repository_id=identity.repository_id), ""
    except (SnapshotRefused, GitHubError) as exc:
        return None, f"the snapshot is refused: {_bounded(str(exc), 1000)}"


def _expected(
    identity: Identity | None, snapshot: EpicSnapshot | None, repository: str
) -> ExpectedIdentity | None:
    if identity is None:
        return None
    issue_ids: dict[int, int] = {}
    if snapshot is not None:
        for child in snapshot.children:
            if child.repository.lower() == repository.lower():
                issue_ids[child.number] = child.id
    return ExpectedIdentity(identity.repository_id, issue_ids, repository=repository)


def _standalone(state_dir: str | Path, repository: str) -> tuple[StandaloneHold | None, str, str]:
    """The standalone hold of this repository, the read's problem, and the admission refusal.

    The refusal is ``""`` unless the standalone state is of a protocol this
    controller does not read (:func:`~autoforge.ownership.admission_refusal`,
    #265): then ``epic run`` and ``epic resume`` admit nothing at all.
    """
    hold = standalone_hold(state_dir)
    if hold.problem:
        return (
            None,
            f"the standalone state {hold.state_file} cannot be read: {hold.problem}",
            admission_refusal(hold),
        )
    if hold.holds and _same_repository(hold.issue_url, repository):
        return hold, "", ""
    return None, "", ""


def collect_status(
    graph: GraphReader,
    *,
    repository_root: str | Path,
    epic_url: str,
    standalone_state_dir: str | Path,
    flags: tuple[str, ...] = (),
) -> StatusReport:
    """``epic status``: every child's category, lock-free and read-only (#183).

    GitHub is read for the identities and the snapshot only; when it cannot
    be, the local namespaces are still shown, identified by
    ``scheduler.json``, and when it can, a ``scheduler.json`` of another
    repository id is a problem. Nothing is created, locked, written or
    signalled.
    """
    epic = parse_issue_url(epic_url)
    repository = epic.repository
    root = coordination_root_of(repository_root)
    scheduler, scheduler_problem = _read_scheduler(root, epic.number)
    stop: StopRead | None = None
    stop_problem = ""
    try:
        stop = read_stop_request(root, epic.number)
    except (StateError, OSError) as exc:
        stop_problem = _bounded(str(exc), 500)
    identity, github_problem = _identity(graph, epic, scheduler)
    snapshot, snapshot_problem = _snapshot(graph, epic, identity, github_problem)
    expected = _expected(identity, snapshot, repository)
    unidentified = (
        ""
        if identity is not None
        else "not classified: neither GitHub nor scheduler.json identifies the EPIC and its "
        "repository"
    )
    hold, hold_problem, refusal = _standalone(standalone_state_dir, repository)
    problems: list[str] = [hold_problem] if hold_problem else []
    if refusal:
        # 'epic run' and 'epic resume' admit nothing while it is there (#265).
        problems.append(f"'epic run' and 'epic resume' admit nothing: {refusal}")
    if not github_problem:
        problems.extend(_foreign_scheduler(scheduler, identity, repository))
    try:
        listing = list_issue_namespaces(root)
        listed = sorted((*listing.bound, *listing.unbound), key=lambda ns: ns.issue_number)
        rejected = listing.rejected
    except (StateError, OSError) as exc:
        problems.append(f"the scheduler's namespaces cannot be listed: {_bounded(str(exc), 500)}")
        listed, rejected = [], ()
    # An entry no reader follows: what it holds is never shown or classified,
    # so it is a problem (exit 1), as an unreadable namespace is.
    problems.extend(
        f"issues/{entry.name} is not a namespace ({entry.reason}) and is never read as one: "
        "move it out of the scheduler's issues/ directory by hand"
        for entry in rejected
    )

    def facts_of(number: int, verdict: ChildVerdict | None) -> IssueFacts:
        standalone = hold if hold is not None and hold.issue_number == number else None
        return read_issue(
            root,
            number,
            verdict=verdict,
            identity=identity,
            expected=expected,
            scheduler=scheduler,
            standalone=standalone,
            unidentified=unidentified,
        )

    children: list[IssueStatus] = []
    shown: set[int] = set()
    if snapshot is not None:
        for verdict in evaluate(snapshot):
            facts = facts_of(verdict.number, verdict)
            if verdict.shape is not Shape.FOREIGN_REPOSITORY:
                shown.add(verdict.number)
            children.append(categorize(facts, epic_url=epic.canonical, flags=flags))
            _note_problem(facts, children[-1], problems)
    others: list[IssueStatus] = []
    for namespace in listed:
        number = namespace.issue_number
        if number in shown:
            continue
        facts = facts_of(number, None)
        if facts.read is None and not facts.live and not facts.probe_problem:
            continue  # nothing there but a free run lock or logs
        if _bound_elsewhere(facts.lookup, identity, epic.number):
            continue
        others.append(
            categorize(facts, epic_url=epic.canonical, flags=flags, orphaned=snapshot is not None)
        )
        _note_problem(facts, others[-1], problems)
    return StatusReport(
        repository=repository,
        epic_url=epic.canonical,
        epic_number=epic.number,
        epic_issue_id=None if identity is None else identity.epic.issue_id,
        flags=flags,
        scheduler=scheduler,
        scheduler_problem=scheduler_problem,
        stop=stop,
        stop_problem=stop_problem,
        snapshot=snapshot,
        snapshot_problem=snapshot_problem,
        children=tuple(children),
        other_namespaces=tuple(others),
        rejected=tuple(rejected),
        problems=tuple(problems),
    )


def _foreign_scheduler(
    scheduler: SchedulerState | None, identity: Identity | None, repository: str
) -> tuple[str, ...]:
    """``scheduler.json`` of another repository than GitHub's: global uncertainty (D14.3).

    Compared by the numeric id GitHub gave (D5.1): a rename keeps it, while
    ``origin`` pointed at another repository, or a name another repository
    took, does not, whatever the recorded name.
    """
    if scheduler is None or identity is None:
        return ()
    if scheduler.repository_id == identity.repository_id:
        return ()
    recorded = f"{scheduler.repository_owner}/{scheduler.repository_name}"
    return (
        f"scheduler.json of EPIC #{scheduler.epic.number} here was written for {recorded} "
        f"(repository id {scheduler.repository_id}), and {repository} is repository id "
        f"{identity.repository_id} (global uncertainty: no scheduler admits anything until it "
        "is resolved)",
    )


def _note_problem(facts: IssueFacts, row: IssueStatus, problems: list[str]) -> None:
    """An issue status cannot vouch for is a problem (exit 1), named once.

    A binding of another repository is global uncertainty; otherwise an
    ``uncertain`` row's reason: a namespace that cannot be read, classified
    or probed.
    """
    found = facts.found
    if found is not None and found.repository_mismatch and found.classification is not None:
        problems.append(
            f"issue #{facts.number}: {found.classification.detail} (global uncertainty: no "
            "scheduler admits anything until it is resolved)"
        )
    elif row.problem:
        problems.append(f"issue #{facts.number}: {row.problem}")


# -- epic run --dry-run (D9.8) ----------------------------------------------------------


@dataclass(frozen=True)
class DryRunPlan:
    """What ``epic run`` would do now: the verdicts in order and who is admitted."""

    lines: tuple[str, ...]
    exit_code: int


def dry_run_plan(
    graph: GraphReader,
    *,
    repository_root: str | Path,
    epic_url: str,
    max_parallel: int,
    standalone_state_dir: str | Path,
    force: bool = False,
    flags: tuple[str, ...] = (),
) -> DryRunPlan:
    """``epic run --dry-run``: the snapshot, the ordered verdicts and the plan (D9.8).

    The local facts are the ones the scheduler's startup would find, read
    lock-free: a live run lock is ``owned``; a settled, uncertain or other
    EPIC's namespace is held; the configured standalone state's issue is
    held. No lock is taken, and no namespace, claim, worktree or worker is
    created.
    """
    epic = parse_issue_url(epic_url)
    root = coordination_root_of(repository_root)
    out = ["dry run: nothing is locked, written or started"]
    scheduler, scheduler_problem = _read_scheduler(root, epic.number)
    identity, github_problem = _identity(graph, epic, None)
    snapshot, snapshot_problem = _snapshot(graph, epic, identity, github_problem)
    if snapshot is None or identity is None:
        out.append(f"no snapshot, so nothing would be admitted: {snapshot_problem}")
        return DryRunPlan(tuple(redact(line) for line in out), 1)
    expected = _expected(identity, snapshot, epic.repository)
    assert expected is not None
    blocked: list[str] = []
    if scheduler_problem:
        if force:
            out.append(
                f"scheduler.json cannot be read ({scheduler_problem}): --force would move it "
                "aside (a dry run moves nothing)"
            )
        else:
            blocked.append(
                f"scheduler.json cannot be read ({scheduler_problem}); 'epic run --force' "
                "moves it aside"
            )
    elif scheduler is not None and scheduler.repository_id != identity.repository_id:
        blocked.append(
            f"scheduler.json names repository id {scheduler.repository_id}, and the live "
            f"repository is {identity.repository_id}"
        )
    try:
        open_claims = open_claims_for_epic(root, identity.epic)
    except (StateError, OSError) as exc:
        blocked.append(f"the open claims cannot be listed: {_bounded(str(exc), 500)}")
        open_claims = ()
    if open_claims:
        blocked.append(open_claims_message(open_claims, flags, epic.canonical))
    facts: dict[int, LocalFact] = {}
    resumable: set[int] = set()
    try:
        listing = list_issue_namespaces(root)
        listed = sorted((*listing.bound, *listing.unbound), key=lambda ns: ns.issue_number)
    except (StateError, OSError) as exc:
        blocked.append(f"the scheduler's namespaces cannot be listed: {_bounded(str(exc), 500)}")
        listed = []
    for namespace in listed:
        number = namespace.issue_number
        live, problem = _probe(namespace)
        if live:
            facts[number] = LocalFact.OWNED
            continue
        if problem:
            facts[number] = LocalFact.HELD_UNCERTAIN
            continue
        found = classify_for_epic(read_namespace(namespace), identity.epic, expected=expected)
        if found is None:
            continue
        if found.repository_mismatch and found.classification is not None:
            blocked.append(f"issue #{number}: {found.classification.detail}")
        if found.fact is not None:
            facts[number] = found.fact
        if (
            found.classification is not None
            and found.classification.kind is ClassificationKind.RESUMABLE
        ):
            resumable.add(number)
    hold, hold_problem, refusal = _standalone(standalone_state_dir, epic.repository)
    if hold_problem:
        out.append(hold_problem)
    if refusal:
        blocked.append(refusal)
    if hold is not None and hold.issue_number is not None:
        facts.setdefault(hold.issue_number, LocalFact.HELD_STANDALONE_STATE)
    verdicts = evaluate(snapshot, facts)
    out.append(
        f"EPIC #{epic.number} of {epic.repository}: snapshot {snapshot.digest[:12]} at "
        f"{snapshot.observed_at}, {len(snapshot.children)} children"
    )
    out.append("verdicts, in admission order:")
    out.extend(f"  {verdict.explain()}" for verdict in verdicts)
    if blocked:
        out.append("'epic run' would admit nothing:")
        out.extend(f"  {line}" for line in blocked)
        return DryRunPlan(tuple(redact(line) for line in out), 0)
    ready = ready_set(verdicts)
    now, queued = ready[:max_parallel], ready[max_parallel:]
    out.append(f"admission plan (at most {max_parallel} worker(s)):")
    if not ready:
        out.append("  nothing is ready")
    else:
        admitted = ", ".join(
            f"#{v.number} ({'resume' if v.number in resumable else 'start'})" for v in now
        )
        out.append(f"  admitted now: {admitted}")
        if queued:
            out.append("  queued for a free slot: " + ", ".join(f"#{v.number}" for v in queued))
    return DryRunPlan(tuple(redact(line) for line in out), 0)


# -- epic run's refusal and --force (D14.4, D6.3) ---------------------------------------


def open_claims_message(claims: Iterable[OpenClaim], flags: tuple[str, ...], epic_url: str) -> str:
    listed = []
    for item in claims:
        if item.claim is None:
            listed.append(f"#{item.issue_number} (its claim file cannot be read: {item.problem})")
        else:
            listed.append(
                f"#{item.issue_number} (generation {item.claim.generation} of invocation "
                f"{item.claim.invocation_id})"
            )
    resume = command(flags, "epic", "resume", epic_url)
    return (
        f"claims of this EPIC are still open: {', '.join(listed)}; an earlier scheduler "
        f"ended without closing them. '{resume}' classifies them and continues"
    )


def epic_run_guard(
    *, force: bool, flags: tuple[str, ...], epic_url: str, say: Callable[[str], None]
) -> Callable[[Path, EpicRef], None]:
    """``epic run``'s ``before_start``: ``--force``'s quarantine, then the open-claim refusal.

    Called by the scheduler holding L0, before ``scheduler.json`` is read:
    ``--force`` moves a corrupt ``scheduler.json`` aside and nothing else
    (D6.3); any open claim of the EPIC refuses the run
    (:class:`~autoforge.errors.OpenSchedulerClaimsError`) with nothing
    written (D14.4).
    """

    def guard(root: Path, epic: EpicRef) -> None:
        if force:
            try:
                state = load_scheduler_state(root, epic.number)
            except SchedulerStateError:
                moved = quarantine_scheduler_state(root, epic.number)
                say(f"--force: the unreadable scheduler.json was moved aside to {moved}")
            else:
                found = "is readable" if state is not None else "does not exist"
                say(f"--force: scheduler.json {found}; nothing was moved aside")
        claims = open_claims_for_epic(root, epic)
        if claims:
            raise OpenSchedulerClaimsError(
                f"'epic run' refuses: {open_claims_message(claims, flags, epic_url)}"
            )

    return guard


def claims_left_open(root: Path, epic: EpicRef | None) -> tuple[OpenClaim, ...]:
    """The EPIC's claims still open after a scheduler ended: an exit-3 condition."""
    if epic is None:
        return ()
    try:
        return open_claims_for_epic(root, epic)
    except (StateError, OSError) as exc:
        return (OpenClaim(0, None, _bounded(str(exc), 500)),)


def left_open_lines(
    claims: Iterable[OpenClaim], flags: tuple[str, ...], epic_url: str
) -> list[str]:
    resume = command(flags, "epic", "resume", epic_url)
    out = []
    for item in claims:
        if item.claim is None:
            where = f"issue #{item.issue_number}" if item.issue_number else "the namespaces"
            out.append(f"{where}: the claims cannot be read ({item.problem})")
        else:
            out.append(f"issue #{item.issue_number}: {_claim_line(item.claim)} is still open")
    out.append(
        f"claims of this EPIC were left open; 'autoforge epic status' says why, and '{resume}' "
        "classifies them at its start"
    )
    return [redact(line) for line in out]


# -- epic stop (D16.4) ------------------------------------------------------------------


@dataclass(frozen=True)
class StopOutcome:
    lines: tuple[str, ...]
    exit_code: int


def live_namespaces(root: Path, epic_number: int) -> tuple[int, ...]:
    """The issues of this EPIC whose run lock a live process holds (the D8.3 probe)."""
    try:
        listing = list_issue_namespaces(root)
    except (StateError, OSError):
        return ()
    live: list[int] = []
    for namespace in listing.bound:
        try:
            lookup = load_issue_namespace(root, namespace.issue_number)
        except (NamespaceMismatch, StateError, OSError):
            continue
        if lookup.binding is None or lookup.binding.epic.number != epic_number:
            continue
        held, _ = _probe(namespace)
        if held:
            live.append(namespace.issue_number)
    return tuple(live)


def stop_scheduler(
    root: Path, epic: GitHubIssueRef, mode: StopMode, repository_id: Callable[[str], int]
) -> StopOutcome:
    """``epic stop``: publish a stop request (#182's ``request_stop``) and say what happened.

    Exit 0 when a request was delivered to the invocation ``scheduler.json``
    names; 1 when nothing was delivered (a request was already there, or
    the scheduler changed after the write). No ``scheduler.json`` (or one
    that does not load), or one of another repository than the EPIC's
    (:func:`check_recorded_repository`, which reads ``repository_id``),
    raises :class:`~autoforge.errors.SchedulerStateError`, with nothing
    created.

    The workers holding their run locks are always listed. An invocation
    that is not ended either still runs or died without finalizing (a
    SIGKILL leaves it so), and nothing here can tell which without trusting
    a PID: its workers get the survivors' caveat conditionally. Nothing is
    ever removed or signalled.
    """
    epic_number = epic.number
    state = load_scheduler_state(root, epic_number)  # absent: request_stop refuses
    if state is not None:
        check_recorded_repository(
            f"scheduler.json of EPIC #{epic_number} here was written for",
            f"{state.repository_owner}/{state.repository_name}",
            state.repository_id,
            epic,
            repository_id,
            refused="no stop request was written",
        )
    delivery = request_stop(root, epic_number, mode)
    out = [delivery.message]
    code = 0 if delivery.written and not delivery.scheduler_changed else 1
    if delivery.existing is not None:
        existing = delivery.existing
        if existing.mode is StopMode.DRAIN and mode is StopMode.NOW:
            out.append(DRAIN_HELP)
    elif delivery.written and mode is StopMode.DRAIN and not delivery.scheduler_ended:
        out.append(
            "admission is closed at the scheduler's next tick and each worker returns at its "
            f"next step boundary; {DRAIN_HELP}"
        )
    live = live_namespaces(root, epic_number)
    listed = ", ".join(f"#{n}" for n in live)
    if not live:
        out.append("no worker of this EPIC holds a run lock")
    elif delivery.scheduler_ended:
        out.append(
            f"workers still running (their run locks are held): {listed}; they drain at "
            "their next step boundary, and no process is ever signalled by a recorded PID"
        )
    elif delivery.scheduler_changed:
        out.append(f"workers holding their run locks: {listed}")
    else:
        out.append(
            f"workers holding their run locks: {listed}. If no 'autoforge epic' process of this "
            "EPIC is running, its invocation ended without finalizing (a crash or a kill) and "
            "these workers survived it: a stop request reaches each only at its next step "
            "boundary, '--now' cannot signal them, and no process is ever signalled by a "
            "recorded PID"
        )
    return StopOutcome(tuple(redact(line) for line in out), code)


# -- epic archive (D17.4) ---------------------------------------------------------------


@dataclass(frozen=True)
class ArchiveReport:
    """What ``epic archive`` did to one namespace, and what it kept."""

    namespace: IssueNamespace
    claim: ClaimArchive | None = None
    state_moved_to: Path | None = None
    binding: RunBinding | None = None
    binding_problem: str = ""

    @property
    def nothing_left(self) -> bool:
        claim_done = self.claim is not None and self.claim.action is not ArchiveAction.NOTHING
        return not claim_done and self.state_moved_to is None

    def lines(self) -> list[str]:
        ns = self.namespace
        number = ns.issue_number
        out: list[str] = []
        if self.binding_problem:
            out.append(f"issue #{number}: the binding cannot be read ({self.binding_problem})")
        claim = self.claim
        if claim is not None and claim.action is ArchiveAction.CLOSED and claim.closed:
            out.append(f"issue #{number}: {_claim_line(claim.closed.claim)} closed as archived")
        elif claim is not None and claim.action is ArchiveAction.MOVED_ASIDE:
            out.append(
                f"issue #{number}: the claim file could not be used ({claim.reason}); moved "
                f"aside to {claim.moved_to}"
            )
        if self.state_moved_to is not None:
            out.append(f"issue #{number}: the state was moved aside to {self.state_moved_to}")
        if self.nothing_left:
            out.append(f"issue #{number}: nothing left to archive (no open claim and no state)")
            return [redact(line) for line in out]
        worktree = self.binding.worktree_path if self.binding is not None else "(no binding)"
        out.append(
            f"kept: the run logs ({ns.state_dir / 'logs'}), the worker logs ({ns.workers_dir}), "
            f"the worktree ({worktree}), the binding ({ns.binding_file}) and the claim history "
            f"({ns.claim_file})"
        )
        if self.state_moved_to is not None:
            out.append(
                "the run's loop-bound evidence (review rounds, fix attempts, the replan count) "
                f"and its validation records now live only in {self.state_moved_to}: the next "
                "admission starts the issue fresh and adopts its marked PR"
            )
        out.append("nothing is archived automatically; this was the operator's explicit retry")
        return [redact(line) for line in out]


def archive_issue(
    root: Path, epic_url: str, issue: int, repository_id: Callable[[str], int]
) -> ArchiveReport:
    """``epic archive``: close the claim, then move the state aside (D17.4).

    The caller holds L0. A namespace that does not exist is left alone:
    nothing is created. One bound under another repository (by its numeric
    id, :func:`check_recorded_repository`, which reads ``repository_id``
    whatever name the binding keeps from its first admission) or under
    another EPIC is refused. L2 is taken non-blocking, so a live owner
    refuses the archive (:class:`~autoforge.errors.IssueBusy`). Each step is
    idempotent: a re-run after a crash completes the archive, and one with
    nothing left says so. No other GitHub read; ``scheduler.json`` is not
    written.
    """
    epic = parse_issue_url(epic_url)
    namespace = issue_namespace(root, issue)
    binding: RunBinding | None = None
    problem = ""
    try:
        lookup = load_issue_namespace(root, issue)
    except (NamespaceMismatch, StateError, OSError) as exc:
        problem = _bounded(str(exc), 500)
    else:
        if not lookup.claim_present and lookup.binding is None:
            if lookup.state.status is StateStatus.ABSENT:
                return ArchiveReport(namespace)
        binding = lookup.binding
    if binding is not None:
        check_recorded_repository(
            f"issue #{issue}'s namespace is bound under",
            binding.repository,
            binding.run_key.repository_id,
            epic,
            repository_id,
            refused="nothing was archived",
        )
    if binding is not None and binding.epic.number != epic.number:
        # The same repository (checked above), under its current name.
        other = f"https://github.com/{epic.repository}/issues/{binding.epic.number}"
        raise SchedulerStateError(
            f"issue #{issue}'s namespace is bound under EPIC #{binding.epic.number}, not "
            f"#{epic.number}: 'autoforge epic archive {other} {issue}' archives it"
        )
    lock = IssueRunLock(namespace)
    try:
        lock.acquire()
    except IssueBusy as exc:
        raise IssueBusy(
            f"issue #{issue} has a live owner ({exc}); 'epic archive' refuses while it runs"
        ) from exc
    try:
        claim = archive_claim(namespace, lock)
        moved = archive_issue_state(namespace)
    finally:
        lock.release()
    return ArchiveReport(namespace, claim, moved, binding, problem)
