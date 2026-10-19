"""The parallel EPIC scheduler: bounded issue workers it owns (ADR 0008 D2, D9, D16; #182).

One foreground process runs up to ``scheduler.max_parallel`` issues of one
EPIC at once. It holds the repository's controller lock (L0) for its whole
life, admits issues one transaction at a time, and starts one worker process
per admitted issue (:mod:`autoforge.worker`), which runs the unchanged
single-issue engine on that issue's namespace and stops at its settlement
boundary. The scheduler itself never invokes an agent, never writes GitHub
(its only GitHub calls are #179's four graph reads), never records an
effect and never parses an agent's result.

**Startup.** Take L0 and switch it to close-only release at once
(:meth:`~autoforge.locking.ControllerLock.share`), so neither a normal exit
nor an exit by exception releases it while a worker that inherited it
lives; refuse a coordination root whose ``flock`` is not local; read the
repository id and the EPIC; refuse a corrupt ``scheduler.json`` or one of
another repository (global uncertainty, D14.3) without writing it; write
``scheduler.json`` for a new invocation; remove stale stop requests; print
the ``allow_merge`` notice when the configuration enables merging (the
configuration is not changed); install the SIGINT, SIGTERM and SIGHUP
handlers, which only set a flag; run #181's reconciliation, whose global
problems admit nothing; apply the reverse guard (the configured standalone
state's issue is ``held:standalone-state`` in any phase but ``DONE``).

**Each tick** (D9.7): reap exited workers and finalize their claims under a
re-acquired L2 (#181's ``finalize_worker``); handle signals and the stop
request; then, while admission is open and slots are free, take a snapshot
(at most once per ``poll_interval_seconds``, or at once after a stale
re-read), close each earlier invocation's ``resumable`` claim whose issue is
no longer ready (``close_unready``), and admit ready issues in order up to
the free slots. It exits once no worker is live and nothing is admissible.

**Admission** (D2.2), holding L0: L2 non-blocking; under it, the strict
read and classification of the namespace (only none, ``fresh`` and
``resumable`` go on); #179's re-read of the issue and its blockers (a
``stale`` one writes no claim and makes the next tick re-snapshot); the
binding on first admission; the claim (superseding a still open
``resumable`` one); the worker spawned with L0 and L2 by inheritance, its
diagnostics recorded, and the scheduler's L2 copy closed (``close`` only).
A spawn that raised is D2.3's: L2 probed afresh, the claim closed
``abandoned`` and the issue ``held:spawn-failed`` when it is free, the claim
kept open and the issue live when it is not.

**No respawn within an invocation:** a worker that exits leaves its issue
held for the rest of the invocation (settled, ``resumable`` or uncertain).

**Stop** (D16): the first signal, or a ``now`` stop request, closes
admission, sends SIGINT to every live worker and waits up to
``stop_grace_seconds``, finalizing each worker that exits; a second signal,
or the grace expiring, forwards a second SIGINT and exits at once. A
``drain`` request closes admission and lets every worker return at its next
step boundary. The scheduler never sends SIGKILL, and a terminal write that
fails (``EIO`` after SIGHUP) is ignored, never aborting finalization.

Nothing here names a provider: every provider runs through the same worker.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import FrameType

from .config import MAX_PARALLEL_BOUNDS, AutoForgeConfig, config_digest
from .engine import ControllerEngine
from .epic_graph import (
    ChildVerdict,
    EpicSnapshot,
    GraphReader,
    LocalFact,
    VerdictKind,
    build_epic_snapshot,
    evaluate,
    ready_set,
    reread_node,
)
from .errors import (
    AutoForgeError,
    ConfigurationError,
    ExecutionError,
    GitHubError,
    IssueBusy,
    LockError,
    SnapshotRefused,
    StateError,
)
from .executor_supervised import SupervisedChild, process_start_time, spawn_supervised
from .locking import (
    ControllerLock,
    IssueRunLock,
    check_local_lock_filesystem,
    repository_lock_path,
)
from .ownership import standalone_hold
from .redaction import redact
from .run_namespace import (
    WORKERS_DIRNAME,
    EpicRef,
    IssueNamespace,
    NamespaceKind,
    NamespaceLookup,
    RunBinding,
    RunKey,
    issue_namespace,
    write_binding,
)
from .safefs import SafeRoot
from .scheduler_state import (
    Admission,
    AdmissionReason,
    AdmissionState,
    AdmissionStatus,
    Claim,
    ClaimClassification,
    ClaimKind,
    ClassificationKind,
    EndReason,
    ExpectedIdentity,
    Invocation,
    SchedulerConfig,
    SchedulerState,
    SnapshotSummary,
    StateRef,
    WorkerDiagnostics,
    bounded_detail,
    classify,
    close_claim,
    close_unready,
    finalize_worker,
    load_scheduler_state,
    new_invocation_id,
    open_claim,
    read_namespace,
    reconcile_epic,
    record_worker,
    write_scheduler_state,
)
from .scheduler_stop import StopMode, read_stop_request, remove_stale_stop_requests
from .state import utcnow_iso
from .validation import parse_issue_url
from .worker import MAX_WORKER_OUTPUT_BYTES, WorkerArguments

# How a worker is started (D9.1): the scheduler's own interpreter, by
# absolute path, never a PATH lookup; ``-P`` keeps the working directory off
# ``sys.path``, so a checkout's own ``autoforge/`` directory is never imported.
DEFAULT_WORKER_COMMAND: tuple[str, ...] = (sys.executable, "-P", "-m", "autoforge.cli")
# The loop's tick: how soon an exited worker is reaped and a signal handled.
TICK_SECONDS = 0.1
# Stale admission re-reads in a row that each take a new snapshot at once;
# after that the next snapshot waits for the poll interval.
MAX_STALE_RESNAPSHOTS = 3

ALLOW_MERGE_NOTICE = (
    "safety.allow_merge is enabled in configuration, but scheduler workers stop at "
    "READY_FOR_MERGE and never merge in this version; configuration is not changed."
)

EXIT_CODES = {
    EndReason.NOTHING_ADMISSIBLE: 0,
    EndReason.DRAINED: 0,
    EndReason.STOPPED: 130,
    EndReason.INTERRUPTED: 130,
    EndReason.DEGRADED: 1,
}

_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

Spawn = Callable[..., SupervisedChild]


@dataclass
class SchedulerInputs:
    """What one scheduler invocation runs on; the CLI (#183) fills it in.

    ``repository`` is the operator's checkout (absolute), ``epic_url`` the
    EPIC, ``config`` the loaded configuration and ``config_path`` its
    absolute file path ("" for the built-in defaults), which every worker
    loads again. ``graph`` makes #179's four reads. ``max_parallel``
    overrides ``scheduler.max_parallel`` within the same bounds.
    ``accept_config_change`` is forwarded to every ``resume`` worker (D9.4).
    ``standalone_state_dir`` is the configured standalone state directory
    the reverse guard reads (``config.state_dir``, resolved against the
    current directory, by default). ``env`` is the workers' environment
    (this process's by default). ``worker_command``, ``spawn``, ``output``,
    ``on_tick``, ``clock`` and ``sleep`` are injection points.
    """

    repository: str
    epic_url: str
    config: AutoForgeConfig
    graph: GraphReader
    config_path: str = ""
    max_parallel: int | None = None
    accept_config_change: bool = False
    standalone_state_dir: str | None = None
    env: Mapping[str, str] | None = None
    worker_command: Sequence[str] = DEFAULT_WORKER_COMMAND
    spawn: Spawn = spawn_supervised
    output: Callable[[str], None] | None = None
    on_tick: Callable[[EpicScheduler], None] | None = None
    tick_seconds: float = TICK_SECONDS
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


@dataclass
class _Worker:
    """One live worker: its issue, its claim and its supervision handle."""

    issue: int
    namespace: IssueNamespace
    claim: Claim
    child: SupervisedChild
    status: int | None = None


class _Admit(Enum):
    ADMITTED = "admitted"
    SKIPPED = "skipped"
    STALE = "stale"


@dataclass
class _Snapshot:
    snapshot: EpicSnapshot
    taken_at: float
    expected: ExpectedIdentity
    verdicts: tuple[ChildVerdict, ...] = field(default_factory=tuple)


def _default_output(line: str) -> None:
    """One line on stderr; a terminal that is gone (``EIO`` after SIGHUP) is ignored (D16.1)."""
    try:
        print(line, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


class EpicScheduler:
    """One scheduler invocation for one EPIC; :meth:`run` is the whole of it."""

    def __init__(self, inputs: SchedulerInputs) -> None:
        self.inputs = inputs
        self.config = inputs.config
        settings = inputs.config.scheduler
        self.max_parallel = (
            settings.max_parallel if inputs.max_parallel is None else inputs.max_parallel
        )
        low, high = MAX_PARALLEL_BOUNDS
        if (
            isinstance(self.max_parallel, bool)
            or not isinstance(self.max_parallel, int)
            or not low <= self.max_parallel <= high
        ):
            raise ConfigurationError(
                f"max_parallel must be between {low} and {high}, got {self.max_parallel!r} "
                "(there is no unbounded mode)"
            )
        self.poll_interval = settings.poll_interval_seconds
        self.stop_grace = settings.stop_grace_seconds
        self.epic = parse_issue_url(inputs.epic_url)
        self.repository = self.epic.repository
        self.repository_root = os.path.abspath(inputs.repository)
        self.coordination_root = Path("/")
        self.invocation_id = new_invocation_id()
        self.repository_id = 0
        self.epic_ref: EpicRef | None = None
        self.admission = AdmissionState(AdmissionStatus.OPEN)
        self.end_reason: EndReason | None = None
        self._output = inputs.output or _default_output
        self._lock: ControllerLock | None = None
        self._l0_fd = -1
        self._state: SchedulerState | None = None
        self._live: dict[int, _Worker] = {}
        self._unfinalized: dict[int, _Worker] = {}
        # Facts held for the rest of the invocation (D4.1, D9.6).
        self._held: dict[int, LocalFact] = {}
        # Live owners this scheduler did not start (``observed_live``), re-checked
        # at every snapshot (D14.2 step 2).
        self._observed_live: set[int] = set()
        self._observed_rechecked = False
        # Earlier invocations' open resumable claims (D14.2): superseded by an
        # admission or closed by close_unready.
        self._resumable_open: dict[int, Claim] = {}
        self._snapshot: _Snapshot | None = None
        self._force_snapshot = False
        self._stale_streak = 0
        self._signal_count = 0
        self._now_requested = False
        self._interrupting = False
        self._grace_deadline: float | None = None
        self._exit_now = False
        self._previous_handlers: dict[int, object] = {}
        self._path_engine: ControllerEngine | None = None
        self.children: list[SupervisedChild] = []

    # -- output ---------------------------------------------------------------------------

    def say(self, text: str) -> None:
        """One line of the scheduler's output, redacted; a failed write is ignored (D16.1)."""
        try:
            self._output(redact(f"autoforge epic: {text}"))
        except (OSError, ValueError):
            pass

    # -- facts ----------------------------------------------------------------------------

    def live_issues(self) -> tuple[int, ...]:
        """The issues whose worker this scheduler started and has not reaped, ascending."""
        return tuple(sorted(self._live))

    def local_facts(self) -> dict[int, LocalFact]:
        """The facts :func:`~autoforge.epic_graph.evaluate` is given now (D4.1)."""
        facts = dict(self._held)
        for number in (*self._observed_live, *self._live, *self._unfinalized):
            facts[number] = LocalFact.OWNED
        return facts

    # -- signals --------------------------------------------------------------------------

    def _on_signal(self, signum: int, frame: FrameType | None) -> None:
        self._signal_count += 1

    def request_interrupt(self) -> None:
        """What a signal does: count it (D16.1); the loop acts at its next tick."""
        self._signal_count += 1

    def _install_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for signum in _SIGNALS:
            self._previous_handlers[signum] = signal.signal(signum, self._on_signal)

    def _restore_handlers(self) -> None:
        for signum, previous in self._previous_handlers.items():
            signal.signal(signum, previous)  # type: ignore[arg-type]
        self._previous_handlers.clear()

    # -- scheduler.json -------------------------------------------------------------------

    def _write_state(self, **changes: object) -> None:
        state = self._state
        assert state is not None
        updated = SchedulerState(
            repository_id=state.repository_id,
            repository_owner=state.repository_owner,
            repository_name=state.repository_name,
            epic=state.epic,
            invocation=changes.get("invocation", state.invocation),  # type: ignore[arg-type]
            config=state.config,
            admission=self.admission,
            last_snapshot=changes.get("last_snapshot", state.last_snapshot),  # type: ignore[arg-type]
            created_at=state.created_at,
            updated_at=state.updated_at,
        )
        try:
            self._state = write_scheduler_state(self.coordination_root, updated)
        except (StateError, OSError) as exc:
            self.say(f"cannot write scheduler.json: {exc}")
            self._state = updated

    def _close_admission(self, status: AdmissionStatus, reason: AdmissionReason) -> None:
        if self.admission.status is not AdmissionStatus.OPEN:
            return
        self.admission = AdmissionState(status, reason)
        self._write_state()

    # -- the run --------------------------------------------------------------------------

    def run(self) -> int:
        """Run the invocation to its end; the exit status (``EXIT_CODES``).

        A held controller lock is :class:`~autoforge.errors.LockError`, raised
        before anything is written; so are the refused-filesystem and other
        startup refusals of their own types.
        """
        lock_path = repository_lock_path(self.repository_root)
        self.coordination_root = lock_path.parent.parent
        lock = ControllerLock(lock_path).acquire()
        self._lock = lock
        # Close-only from here on (D10.3): no exit of this process, by
        # exception included, releases L0 while a worker that inherited it lives.
        self._l0_fd = lock.share()
        try:
            check_local_lock_filesystem(
                self.coordination_root, what=f"the coordination root {self.coordination_root}"
            )
            if not self._start():
                return EXIT_CODES[EndReason.DEGRADED]
            try:
                self._loop()
            finally:
                self._restore_handlers()
            return self._finish()
        finally:
            self._restore_handlers()
            self._lock = None
            lock.release()

    def _start(self) -> bool:
        """The startup path up to the first tick; ``False`` when nothing may be admitted."""
        try:
            self.repository_id = self.inputs.graph.get_repository_id(self.repository)
            epic_node = self.inputs.graph.get_issue_node(self.epic.canonical)
        except GitHubError as exc:
            self.say(f"cannot read the repository or the EPIC on GitHub: {exc}")
            return False
        self.epic_ref = EpicRef(self.epic.number, epic_node.id)
        try:
            existing = load_scheduler_state(self.coordination_root, self.epic.number)
        except StateError as exc:
            self.say(
                f"scheduler.json cannot be read, so nothing is admitted ({exc}); "
                "'epic run --force' moves it aside"
            )
            return False
        if existing is not None and existing.repository_id != self.repository_id:
            self.say(
                f"scheduler.json names repository id {existing.repository_id}, and the live "
                f"repository is {self.repository_id}: nothing is admitted"
            )
            return False
        owner, _, name = self.repository.partition("/")
        now = utcnow_iso()
        self._state = write_scheduler_state(
            self.coordination_root,
            SchedulerState(
                repository_id=self.repository_id,
                repository_owner=owner,
                repository_name=name,
                epic=self.epic_ref,
                invocation=Invocation(self.invocation_id, now),
                config=SchedulerConfig(
                    self.max_parallel,
                    self.poll_interval,
                    self.stop_grace,
                    config_digest(self.config),
                ),
                admission=self.admission,
                created_at=existing.created_at if existing is not None else now,
                updated_at=now,
            ),
        )
        for name in remove_stale_stop_requests(
            self.coordination_root, self.epic.number, self.invocation_id, at_start=True
        ):
            self.say(f"removed the stale {name} of an earlier invocation")
        self.say(
            f"EPIC #{self.epic.number} of {self.repository}: invocation {self.invocation_id}, "
            f"at most {self.max_parallel} worker(s)"
        )
        if self.config.merge_allowed_by_config:
            self.say(ALLOW_MERGE_NOTICE)
        self._install_handlers()
        expected = ExpectedIdentity(self.repository_id, repository=self.repository)
        report = reconcile_epic(self.coordination_root, self.epic_ref, expected=expected)
        for problem in report.global_problems:
            self.say(f"global uncertainty, nothing is admitted: {problem}")
        for item in report.issues:
            if item.observed_live:
                self._observed_live.add(item.issue_number)
                self.say(f"issue #{item.issue_number}: its run lock is held ({item.detail})")
            elif item.fact is not None:
                self._held[item.issue_number] = item.fact
                if item.fact is LocalFact.HELD_UNCERTAIN:
                    self.say(f"issue #{item.issue_number} is uncertain: {item.detail}")
            classification = item.classification
            if (
                classification is not None
                and classification.kind is ClassificationKind.RESUMABLE
                and classification.open_claim is not None
            ):
                self._resumable_open[item.issue_number] = classification.open_claim
        if not report.admissible:
            self.admission = AdmissionState(
                AdmissionStatus.DEGRADED, AdmissionReason.EPIC_NOT_ADMISSIBLE
            )
            self.end_reason = EndReason.DEGRADED
            self._finish()
            return False
        self._apply_reverse_guard()
        return True

    def _apply_reverse_guard(self) -> None:
        """D17.2: the configured standalone state's issue is not admitted."""
        state_dir = self.inputs.standalone_state_dir or os.path.abspath(self.config.state_dir)
        hold = standalone_hold(state_dir)
        if hold.problem:
            self.say(f"the standalone state {hold.state_file} cannot be read: {hold.problem}")
        if not hold.holds:
            return
        assert hold.issue_number is not None
        try:
            same = parse_issue_url(hold.issue_url).repository.lower() == self.repository.lower()
        except ConfigurationError:
            same = False
        if same:
            self._held.setdefault(hold.issue_number, LocalFact.HELD_STANDALONE_STATE)
            self.say(
                f"issue #{hold.issue_number} is held: the standalone state {hold.state_file} "
                f"runs it (phase {hold.phase})"
            )

    def _loop(self) -> None:
        while True:
            self._reap()
            self._handle_stop()
            if self._exit_now:
                return
            if self.admission.status is AdmissionStatus.OPEN and self._free_slots() > 0:
                self._admit_ready()
            if not self._live and not self._unfinalized and not self._admissible():
                return
            if self.inputs.on_tick is not None:
                self.inputs.on_tick(self)
            self.inputs.sleep(self.inputs.tick_seconds)

    def _free_slots(self) -> int:
        return self.max_parallel - len(self._live)

    def _admissible(self) -> bool:
        """Whether the loop has anything left to admit: an open admission and a ready issue."""
        if self.admission.status is not AdmissionStatus.OPEN:
            return False
        if self._force_snapshot or self._snapshot is None:
            return True
        if ready_set(evaluate(self._snapshot.snapshot, self.local_facts())):
            return True
        if self._observed_live and not self._observed_rechecked:
            # A run lock seen held once may have been a status probe: one more
            # snapshot re-checks it before the scheduler ends (D14.2 step 2).
            self._observed_rechecked = True
            self._force_snapshot = True
            return True
        for number in sorted(self._observed_live):
            self.say(
                f"issue #{number}: its run lock stays held by a process this scheduler did "
                "not start; it was not admitted"
            )
        return False

    def _finish(self) -> int:
        reason = self.end_reason
        if reason is None:
            reason = (
                EndReason.DEGRADED
                if self.admission.status is AdmissionStatus.DEGRADED
                else EndReason.NOTHING_ADMISSIBLE
            )
            self.end_reason = reason
        state = self._state
        if state is not None:
            self._write_state(
                invocation=Invocation(
                    state.invocation.invocation_id,
                    state.invocation.started_at,
                    utcnow_iso(),
                    reason,
                )
            )
        remaining = sorted({*self._live, *self._unfinalized})
        if remaining:
            listed = ", ".join(f"#{n}" for n in remaining)
            self.say(
                f"exiting ({reason.value}) while the workers of {listed} still run: they keep "
                "the controller lock until they exit, and their claims are classified at the "
                "next start"
            )
        else:
            self.say(f"done ({reason.value})")
        return EXIT_CODES[reason]

    # -- reaping --------------------------------------------------------------------------

    def _reap(self) -> None:
        for number in sorted(self._live):
            worker = self._live[number]
            status = worker.child.poll()
            if status is None:
                continue
            del self._live[number]
            worker.status = status
            self._unfinalized[number] = worker
        for number in sorted(self._unfinalized):
            if self._finalize(self._unfinalized[number]):
                del self._unfinalized[number]

    def _expected(self) -> ExpectedIdentity:
        if self._snapshot is not None:
            return self._snapshot.expected
        return ExpectedIdentity(self.repository_id, repository=self.repository)

    def _finalize(self, worker: _Worker) -> bool:
        """Close a reaped worker's claim by the state it left; ``False`` to retry next tick."""
        number = worker.issue
        lock = IssueRunLock(worker.namespace)
        try:
            lock.acquire()
        except IssueBusy:
            return False  # a status probe at this instant: the next tick takes it
        except (LockError, StateError, OSError) as exc:
            self._held[number] = LocalFact.HELD_UNCERTAIN
            self.say(f"issue #{number}: the run lock cannot be taken to finalize ({exc})")
            return True
        try:
            finalization = finalize_worker(
                worker.namespace,
                lock,
                worker.claim,
                worker.status,
                expected=self._expected(),
            )
        except (StateError, OSError) as exc:
            self._held[number] = LocalFact.HELD_UNCERTAIN
            self.say(f"issue #{number}: its claim cannot be finalized ({exc}); it stays open")
            return True
        finally:
            lock.release()
        classification = finalization.classification
        if classification.kind is ClassificationKind.SETTLED:
            self._held[number] = LocalFact.HELD_SETTLED_NAMESPACE
        elif classification.kind is ClassificationKind.RESUMABLE:
            self._held[number] = LocalFact.HELD_RESUMABLE
        else:
            self._held[number] = LocalFact.HELD_UNCERTAIN
        closed = (
            f"claim closed {finalization.closed.classification.value}"
            if finalization.closed is not None
            else "claim left open"
        )
        self.say(
            f"issue #{number}: worker exited with status {worker.status}: {classification} "
            f"({closed})"
        )
        return True

    # -- stop and signals -----------------------------------------------------------------

    def _handle_stop(self) -> None:
        try:
            stop = read_stop_request(self.coordination_root, self.epic.number)
        except (StateError, OSError) as exc:
            self.say(f"the stop request cannot be read ({exc}); it is taken as a drain")
            stop = None
            self._drain()
        if stop is not None:
            if stop.bound_elsewhere(self.invocation_id):
                try:
                    remove_stale_stop_requests(
                        self.coordination_root,
                        self.epic.number,
                        self.invocation_id,
                        at_start=False,
                    )
                except (StateError, OSError) as exc:
                    self.say(f"cannot remove a stale stop request ({exc})")
            elif stop.applies_to(self.invocation_id):
                if stop.request is None and self.admission.status is AdmissionStatus.OPEN:
                    self.say(f"a stop request that does not parse is a drain ({stop.problem})")
                if stop.mode is StopMode.NOW:
                    self._now_requested = True
                else:
                    self._drain()
        interrupts = self._signal_count + (1 if self._now_requested else 0)
        if interrupts >= 1 and not self._interrupting:
            # The first signal, or a ``now`` request: as Ctrl-C (D16.1, D16.4).
            self._interrupting = True
            if self._signal_count:
                self.end_reason = EndReason.INTERRUPTED
                self._close_admission(AdmissionStatus.CLOSED, AdmissionReason.INTERRUPTED)
            else:
                self.end_reason = EndReason.STOPPED
                self._close_admission(AdmissionStatus.CLOSED, AdmissionReason.STOP_REQUESTED)
            self._grace_deadline = self.inputs.clock() + self.stop_grace
            self._signal_live(signal.SIGINT)
            self.say(
                f"stopping: admission closed; SIGINT sent to {len(self._live)} worker(s), "
                f"waiting up to {self.stop_grace}s for them to return"
            )
            return
        if not self._interrupting:
            return
        expired = self._grace_deadline is not None and self.inputs.clock() >= self._grace_deadline
        if interrupts >= 2 or expired:
            self._signal_live(signal.SIGINT)
            self._exit_now = True
            self.say(
                "a second interrupt"
                if interrupts >= 2
                else f"the stop grace of {self.stop_grace}s expired"
            )

    def _drain(self) -> None:
        if self.admission.status is AdmissionStatus.OPEN:
            self._close_admission(AdmissionStatus.CLOSED, AdmissionReason.STOP_REQUESTED)
            if self.end_reason is None:
                self.end_reason = EndReason.DRAINED
            self.say("draining: admission closed; each worker returns at its next step boundary")

    def _signal_live(self, sig: int) -> None:
        """``sig`` to every unreaped worker through its handle: never a pid read from a file."""
        for worker in self._live.values():
            worker.child.signal(sig)

    # -- snapshots ------------------------------------------------------------------------

    def _take_snapshot(self) -> _Snapshot | None:
        try:
            snapshot = build_epic_snapshot(
                self.inputs.graph, self.epic.canonical, repository_id=self.repository_id
            )
        except SnapshotRefused as exc:
            later = " (it may succeed later: run the scheduler again)" if exc.transient else ""
            self.say(
                f"the EPIC snapshot was refused ({exc.reason}){later}: {exc}; admission closes, "
                "running workers continue"
            )
            self.admission = AdmissionState(
                AdmissionStatus.DEGRADED, AdmissionReason.SNAPSHOT_REFUSED
            )
            self._write_state()
            return None
        home = self.repository.lower()
        expected = ExpectedIdentity(
            self.repository_id,
            {
                child.number: child.id
                for child in snapshot.children
                if (child.repository or "").lower() == home
            },
            repository=self.repository,
        )
        self._observed_live.clear()
        taken = _Snapshot(snapshot, self.inputs.clock(), expected)
        self._snapshot = taken
        self._force_snapshot = False
        self._close_unready(taken)
        taken.verdicts = evaluate(snapshot, self.local_facts())
        self._write_state(last_snapshot=self._summary(taken))
        return taken

    def _summary(self, taken: _Snapshot) -> SnapshotSummary:
        counts = dict.fromkeys(("ready", "blocked", "closed", "unsupported", "owned", "held"), 0)
        for verdict in taken.verdicts:
            counts[verdict.kind.value] += 1
        return SnapshotSummary(
            taken.snapshot.digest,
            taken.snapshot.observed_at,
            children=len(taken.verdicts),
            **counts,
        )

    def _close_unready(self, taken: _Snapshot) -> None:
        """D14.2: an earlier invocation's resumable claim of an issue no longer ready is closed."""
        verdicts = {verdict.number: verdict for verdict in evaluate(taken.snapshot, {})}
        for number in sorted(self._resumable_open):
            verdict = verdicts.get(number)
            if verdict is not None and verdict.kind not in (
                VerdictKind.CLOSED,
                VerdictKind.BLOCKED,
                VerdictKind.UNSUPPORTED,
            ):
                continue
            why = (
                "the issue is no longer a child of the EPIC"
                if verdict is None
                else f"the snapshot gives it {verdict.reason}"
            )
            namespace = issue_namespace(self.coordination_root, number)
            lock = IssueRunLock(namespace)
            try:
                lock.acquire()
            except IssueBusy:
                continue  # live now: the next snapshot looks again
            except (LockError, StateError, OSError) as exc:
                self.say(f"issue #{number}: the run lock cannot be taken ({exc})")
                continue
            try:
                closed = close_unready(
                    namespace,
                    lock,
                    self._resumable_open[number],
                    detail=bounded_detail(f"not ready at invocation {self.invocation_id}: {why}"),
                    expected=taken.expected,
                )
            except (StateError, OSError) as exc:
                self.say(f"issue #{number}: its open claim is not closed ({exc})")
                continue
            finally:
                lock.release()
            del self._resumable_open[number]
            if verdict is None:
                self._held[number] = LocalFact.HELD_ORPHANED
            self.say(
                f"issue #{number}: the earlier invocation's claim is closed "
                f"{closed.classification.value}: {why}"
            )

    # -- admission ------------------------------------------------------------------------

    def _admit_ready(self) -> None:
        taken = self._snapshot
        if (
            taken is None
            or self._force_snapshot
            or self.inputs.clock() - taken.taken_at >= self.poll_interval
        ):
            taken = self._take_snapshot()
            if taken is None:
                return
        for verdict in ready_set(evaluate(taken.snapshot, self.local_facts())):
            if self._free_slots() <= 0 or self.admission.status is not AdmissionStatus.OPEN:
                return
            outcome = self._admit(taken, verdict)
            if outcome is _Admit.STALE:
                # No claim; the next tick takes a new snapshot (D2.2 step 4). A
                # graph that keeps changing waits for the poll interval instead.
                self._stale_streak += 1
                self._force_snapshot = self._stale_streak <= MAX_STALE_RESNAPSHOTS
                return
            if outcome is _Admit.ADMITTED:
                self._stale_streak = 0

    def _worker_log(self, namespace: IssueNamespace, generation: int) -> Path:
        return namespace.workers_dir / f"{generation}.log"

    def _worktree_path(self, number: int) -> str:
        if self._path_engine is None:
            # Path derivation only (ADR 0008 D5.4): this engine runs nothing.
            self._path_engine = ControllerEngine(config=self.config, workdir=self.repository_root)
        return str(self._path_engine.issue_worktree_path(number))

    def _admit(self, taken: _Snapshot, verdict: ChildVerdict) -> _Admit:
        """D2.2 steps 2 to 6 for one ready issue."""
        number = verdict.number
        namespace = issue_namespace(self.coordination_root, number)
        lock = IssueRunLock(namespace)
        try:
            lock.acquire()
        except IssueBusy:
            self._observed_live.add(number)
            self.say(f"issue #{number}: its run lock is held; it is skipped for now")
            return _Admit.SKIPPED
        except (LockError, StateError, OSError) as exc:
            self._held[number] = LocalFact.HELD_UNCERTAIN
            self.say(f"issue #{number}: the run lock cannot be taken ({exc})")
            return _Admit.SKIPPED
        try:
            return self._admit_holding(taken, verdict, namespace, lock)
        except (StateError, OSError) as exc:
            # A binding or claim write refused: nothing was spawned for it.
            self._held[number] = LocalFact.HELD_UNCERTAIN
            self.say(f"issue #{number} is not admitted: {exc}")
            return _Admit.SKIPPED
        finally:
            lock.release()

    def _admit_holding(
        self,
        taken: _Snapshot,
        verdict: ChildVerdict,
        namespace: IssueNamespace,
        lock: IssueRunLock,
    ) -> _Admit:
        number = verdict.number
        assert self.epic_ref is not None
        read = read_namespace(namespace)
        supersedes: Claim | None = None
        bound = False
        if read.absent:
            kind, state_ref = ClaimKind.START, StateRef.absent()
        else:
            lookup = read.lookup
            if (
                isinstance(lookup, NamespaceLookup)
                and lookup.binding is not None
                and lookup.binding.epic != self.epic_ref
            ):
                self._held[number] = LocalFact.HELD_OTHER_EPIC
                self.say(f"issue #{number} is bound under EPIC #{lookup.binding.epic.number}")
                return _Admit.SKIPPED
            classification = classify(lookup, read.claim, expected=taken.expected)
            if classification.kind is ClassificationKind.UNCERTAIN:
                self._held[number] = LocalFact.HELD_UNCERTAIN
                self.say(f"issue #{number} is {classification}: {classification.detail}")
                return _Admit.SKIPPED
            if classification.kind is ClassificationKind.SETTLED:
                self._held[number] = LocalFact.HELD_SETTLED_NAMESPACE
                if classification.open_claim is not None:
                    close_claim(
                        namespace,
                        lock,
                        classification.open_claim,
                        ClaimClassification.SETTLED,
                        settlement=classification.settlement,
                        detail=f"closed at admission: {classification.detail}",
                    )
                return _Admit.SKIPPED
            assert isinstance(lookup, NamespaceLookup)
            bound = lookup.kind is NamespaceKind.BOUND
            supersedes = classification.open_claim
            if classification.kind is ClassificationKind.FRESH:
                kind, state_ref = ClaimKind.START, StateRef.absent()
            else:
                kind, state_ref = ClaimKind.RESUME, StateRef.from_lookup(lookup)
        try:
            reread = reread_node(self.inputs.graph, taken.snapshot, number)
        except GitHubError as exc:
            self.say(f"issue #{number}: the admission re-read failed ({exc}); re-snapshotting")
            return _Admit.STALE
        if not reread.unchanged:
            listed = "; ".join(reread.differences)
            self.say(f"issue #{number} changed since the snapshot ({listed}); no claim")
            return _Admit.STALE
        if not bound:
            owner, _, name = self.repository.partition("/")
            write_binding(
                namespace,
                RunBinding(
                    run_key=RunKey(self.repository_id, number),
                    issue_id=verdict.issue_id,
                    repository_owner=owner,
                    repository_name=name,
                    epic=self.epic_ref,
                    worktree_path=self._worktree_path(number),
                ),
            )
        claim = open_claim(
            namespace,
            lock,
            kind=kind,
            invocation_id=self.invocation_id,
            admission=Admission(
                taken.snapshot.digest, reread.node_digest, taken.snapshot.observed_at
            ),
            state_ref=state_ref,
            supersedes=supersedes,
            expected=taken.expected,
        )
        self._resumable_open.pop(number, None)
        self._spawn(namespace, lock, claim)
        return _Admit.ADMITTED

    def _spawn(self, namespace: IssueNamespace, lock: IssueRunLock, claim: Claim) -> None:
        """D2.2 step 6 and D2.3: the worker started with L0 and L2, or the claim closed."""
        number = namespace.issue_number
        log_path = self._worker_log(namespace, claim.generation)
        arguments = WorkerArguments(
            repository=self.repository_root,
            config=self.inputs.config_path,
            epic=self.epic.number,
            issue=number,
            claim_id=claim.claim_id,
            generation=claim.generation,
            invocation_id=self.invocation_id,
            l0_fd=self._l0_fd,
            l2_fd=lock.handoff(),
            accept_config_change=(
                self.inputs.accept_config_change and claim.kind is ClaimKind.RESUME
            ),
        )
        argv = [*self.inputs.worker_command, "epic", "_worker", *arguments.argv()]
        try:
            with SafeRoot.open(self.coordination_root) as anchor:
                log = anchor.open_append(
                    "/".join((*namespace.relative, WORKERS_DIRNAME, log_path.name)),
                    limit=MAX_WORKER_OUTPUT_BYTES,
                )
        except (StateError, OSError) as exc:
            # No process exists, and this scheduler holds L2: the claim is closed now.
            self._abandon(namespace, lock, claim, f"the worker log cannot be opened ({exc})")
            return
        try:
            try:
                child = self.inputs.spawn(
                    argv,
                    pass_fds=(self._l0_fd, arguments.l2_fd),
                    stdout_fd=log.fd,
                    cwd=self.repository_root,
                    env=dict(os.environ if self.inputs.env is None else self.inputs.env),
                )
            except (ExecutionError, OSError, subprocess.SubprocessError) as exc:
                self._spawn_failed(namespace, lock, claim, exc)
                return
        finally:
            log.close()
        self.children.append(child)
        self._live[number] = _Worker(number, namespace, claim, child)
        try:
            self._live[number].claim = record_worker(
                namespace,
                claim,
                WorkerDiagnostics(
                    child.pid, process_start_time(child.pid), utcnow_iso(), str(log_path)
                ),
            )
        except (AutoForgeError, OSError) as exc:
            # A worker exists: its claim stays open and is finalized after the reap (D2.3).
            self.say(f"issue #{number}: the worker's diagnostics were not recorded ({exc})")
        self.say(
            f"admitted issue #{number} ({claim.kind.value}, generation {claim.generation}): "
            f"worker pid {child.pid}, log {log_path}"
        )

    def _abandon(
        self, namespace: IssueNamespace, lock: IssueRunLock, claim: Claim, why: str
    ) -> None:
        number = namespace.issue_number
        try:
            close_claim(namespace, lock, claim, ClaimClassification.ABANDONED, detail=why)
        except (StateError, OSError) as exc:
            self._held[number] = LocalFact.HELD_UNCERTAIN
            self.say(f"issue #{number}: {why}, and its claim is not closed ({exc})")
            return
        self._held[number] = LocalFact.HELD_SPAWN_FAILED
        self.say(f"issue #{number}: {why}; its claim is closed abandoned")

    def _spawn_failed(
        self, namespace: IssueNamespace, lock: IssueRunLock, claim: Claim, exc: BaseException
    ) -> None:
        """D2.3: our L2 copy closed, then L2 probed on a fresh descriptor."""
        number = namespace.issue_number
        lock.release()
        probe = IssueRunLock(namespace)
        try:
            probe.acquire()
        except IssueBusy:
            # A child exists after all: it holds L2, so the claim stays open.
            self._held[number] = LocalFact.OWNED
            self.say(
                f"issue #{number}: the worker start failed ({exc}), and its run lock is still "
                "held: a child exists, so the claim stays open"
            )
            return
        except (LockError, StateError, OSError) as probe_exc:
            self._held[number] = LocalFact.HELD_UNCERTAIN
            self.say(f"issue #{number}: the worker start failed ({exc}); {probe_exc}")
            return
        try:
            self._abandon(namespace, probe, claim, f"the worker could not be started ({exc})")
        finally:
            probe.release()


def run_scheduler(inputs: SchedulerInputs) -> int:
    """Run one scheduler invocation (:class:`EpicScheduler`); its exit status."""
    return EpicScheduler(inputs).run()
