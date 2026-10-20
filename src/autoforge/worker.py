"""The scheduler's issue worker: the unchanged engine on one issue's namespace (ADR 0008 D9; #182).

A worker is one process, ``sys.executable -P -m autoforge.cli epic _worker
...`` (a hidden subcommand), that the scheduler starts for one admitted
issue and that runs one :class:`~autoforge.engine.ControllerEngine` on that
issue's namespace until the issue's single-issue settlement boundary.

**Startup** (:func:`run_worker`, D9.2), each step before the next and every
refusal an exit 2 before any state is read or written:

1. adopt the inherited L0 and L2 descriptors through #180's fresh-descriptor
   proof of holding (:meth:`ControllerLock.adopt
   <autoforge.locking.ControllerLock.adopt>`, :meth:`IssueRunLock.adopt
   <autoforge.locking.IssueRunLock.adopt>`), never a re-asserted ``flock``;
2. mark them non-inheritable (the adoption does), so no launch of this
   process, an agent's included, inherits either;
3. strictly load the namespace's binding and its claim file, and require the
   open claim to be argv's ``(claim_id, generation, invocation_id)`` and both
   files to name argv's issue and EPIC;
4. require ``scheduler.json`` to name argv's invocation and the
   configuration this process loaded to have its ``config_digest``;
5. bind the engine to the namespace's state directory, anchored at the git
   common dir (:meth:`~autoforge.engine.ControllerEngine.bind_issue_namespace`),
   under the adopted L0.

**Policy** (D9.3): the engine runs with ``allow_merge=False`` whatever
``safety.allow_merge`` says, ``extra_stop_phases`` {``MERGE``,
``UPDATE_EPIC``} (so ``next_issue_url`` is never read and
``reset_for_new_issue`` never runs), and a ``should_continue`` predicate
that, before every step, proves L2 is still held and reads the stop request
(a request for this invocation, or one that does not parse, ends the run at
that boundary). A fresh issue starts exactly as ``autoforge run`` starts
it (INITIALIZING, with no ownership predicate); a namespace holding a state
passes #219's recorded-configuration gate first, as ``resume`` does
(D9.4). stdin is ``/dev/null``. #126's interaction policy has not landed,
so there is no ``prompt`` policy to refuse yet.

**Signals.** The worker installs two handlers, so the executor's existing
teardown of a running agent runs: SIGTERM raises ``KeyboardInterrupt``, and
SIGINT gets Python's own ``default_int_handler`` again, since a scheduler
started with SIGINT ignored (a background job of a non-interactive shell)
passes that disposition on, and Python then installs none. Either signal
ends the worker with exit 130 and the state as the last step saved it.

**Output** goes to the worker log the scheduler opened as stdout and stderr:
the ordinary, already-redacted CLI output, cut after
:data:`MAX_WORKER_OUTPUT_BYTES` bytes. It is never read for recovery.

Nothing here names a provider, writes GitHub, parses ``CONTROL_RESULT`` or
records an effect: everything the issue does is the engine's.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import signal
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, NoReturn, TextIO

from .config import AutoForgeConfig, config_digest, load_config_file
from .engine import ControllerEngine, StepOutcome
from .errors import (
    AutoForgeError,
    ConfigurationError,
    LockError,
    StateError,
    StateTransitionError,
    WorkerStartupError,
)
from .locking import ControllerLock, IssueRunLock, repository_lock_path
from .redaction import redact
from .run_namespace import IssueNamespace, issue_namespace, load_binding
from .scheduler_state import ClaimKind, load_claim_file, load_scheduler_state
from .scheduler_stop import read_stop_request
from .transitions import STOP_PHASES, Phase, issue_settlement
from .validation import parse_issue_url

if TYPE_CHECKING:
    from .github import GitHubClient
    from .providers import ProviderRegistry

# The phases a worker never executes, on top of the closed merge gate's stop
# phases (ADR 0008 D9.3): it returns before them, so a worker never merges,
# never updates the EPIC and never selects a next issue.
WORKER_EXTRA_STOP_PHASES = frozenset({Phase.MERGE, Phase.UPDATE_EPIC})
WORKER_STOP_PHASES = frozenset(STOP_PHASES) | WORKER_EXTRA_STOP_PHASES
# Steps per worker, as for a standalone ``run``: a run that needs more is left
# in progress, its claim closes ``interrupted`` and the next invocation resumes it.
WORKER_MAX_STEPS = 50
# The worker log's bound (D9.6): the CLI output of one issue's run, cut there.
MAX_WORKER_OUTPUT_BYTES = 8 * 1024 * 1024

EXIT_REFUSED = 2
EXIT_INTERRUPTED = 130

_HEX32 = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class WorkerArguments:
    """What a worker is started for: everything its argv carries (D9.1).

    ``repository`` is the operator's checkout the scheduler runs in, and
    ``config`` the absolute path of the configuration file ("" for the
    built-in defaults). ``l0_fd`` and ``l2_fd`` are the inherited descriptor
    numbers; no environment variable carries anything.
    """

    repository: str
    config: str
    epic: int
    issue: int
    claim_id: str
    generation: int
    invocation_id: str
    l0_fd: int
    l2_fd: int
    accept_config_change: bool = False

    def argv(self) -> list[str]:
        """The arguments after ``epic _worker``; :func:`parse_worker_arguments` reads them back."""
        words = ["--repository", self.repository]
        if self.config:
            words += ["--config", self.config]
        words += [
            "--epic",
            str(self.epic),
            "--issue",
            str(self.issue),
            "--claim-id",
            self.claim_id,
            "--generation",
            str(self.generation),
            "--invocation-id",
            self.invocation_id,
            "--l0-fd",
            str(self.l0_fd),
            "--l2-fd",
            str(self.l2_fd),
        ]
        if self.accept_config_change:
            words.append("--accept-config-change")
        return words


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise WorkerStartupError(f"invalid worker arguments: {message}")


def _number(text: str) -> int:
    if not text.isdigit() or text.startswith("0"):
        raise argparse.ArgumentTypeError(f"not a positive decimal number: {text!r:.40}")
    return int(text)


def _descriptor(text: str) -> int:
    if not text.isdigit() or int(text) < 3:
        raise argparse.ArgumentTypeError(f"not an inherited descriptor number: {text!r:.40}")
    return int(text)


def _hex32(text: str) -> str:
    if not _HEX32.fullmatch(text):
        raise argparse.ArgumentTypeError(f"not a 32-hex id: {text!r:.40}")
    return text


def _absolute(text: str) -> str:
    if not os.path.isabs(text):
        raise argparse.ArgumentTypeError(f"not an absolute path: {text!r:.200}")
    return text


def parse_worker_arguments(argv: Sequence[str]) -> WorkerArguments:
    """Strictly parse a worker's argv; :class:`~autoforge.errors.WorkerStartupError` otherwise."""
    parser = _Parser(prog="autoforge epic _worker", add_help=False, allow_abbrev=False)
    parser.add_argument("--repository", type=_absolute, required=True)
    # No string default: argparse would run it through ``type`` (and refuse "").
    parser.add_argument("--config", type=_absolute, default=None)
    parser.add_argument("--epic", type=_number, required=True)
    parser.add_argument("--issue", type=_number, required=True)
    parser.add_argument("--claim-id", type=_hex32, required=True)
    parser.add_argument("--generation", type=_number, required=True)
    parser.add_argument("--invocation-id", type=_hex32, required=True)
    parser.add_argument("--l0-fd", type=_descriptor, required=True)
    parser.add_argument("--l2-fd", type=_descriptor, required=True)
    parser.add_argument("--accept-config-change", action="store_true")
    args = parser.parse_args(list(argv))
    if args.l0_fd == args.l2_fd:
        raise WorkerStartupError("invalid worker arguments: L0 and L2 name one descriptor")
    return WorkerArguments(
        repository=args.repository,
        config=args.config or "",
        epic=args.epic,
        issue=args.issue,
        claim_id=args.claim_id,
        generation=args.generation,
        invocation_id=args.invocation_id,
        l0_fd=args.l0_fd,
        l2_fd=args.l2_fd,
        accept_config_change=args.accept_config_change,
    )


# -- output -------------------------------------------------------------------------------


class _Budget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.remaining = limit
        self.cut = False


class _BoundedStream(io.TextIOBase):
    """A text stream that passes at most the shared budget's bytes on, then one notice."""

    def __init__(self, inner: TextIO, budget: _Budget) -> None:
        super().__init__()
        self._inner = inner
        self._budget = budget

    def writable(self) -> bool:
        return True

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return getattr(self._inner, "encoding", "utf-8")

    def write(self, text: str) -> int:
        budget = self._budget
        if budget.cut:
            return len(text)
        data = text.encode("utf-8", "replace")
        if len(data) <= budget.remaining:
            budget.remaining -= len(data)
            self._inner.write(text)
            return len(text)
        self._inner.write(data[: budget.remaining].decode("utf-8", "ignore"))
        self._inner.write(
            f"\n[autoforge worker: output cut after {budget.limit} bytes; the run goes on, "
            "and its state and run log are complete]\n"
        )
        budget.remaining = 0
        budget.cut = True
        return len(text)

    def flush(self) -> None:
        self._inner.flush()


@contextmanager
def _bounded_output(limit: int = MAX_WORKER_OUTPUT_BYTES) -> Iterator[None]:
    budget = _Budget(limit)
    previous = (sys.stdout, sys.stderr)
    sys.stdout = _BoundedStream(previous[0], budget)  # type: ignore[assignment]
    sys.stderr = _BoundedStream(previous[1], budget)  # type: ignore[assignment]
    try:
        yield
    finally:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
        sys.stdout, sys.stderr = previous


def _say(text: str) -> None:
    print(redact(f"autoforge worker: {text}"), file=sys.stderr, flush=True)


def _progress_line(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _print_outcome(outcome: StepOutcome) -> None:
    from .cli import print_step_outcome  # the CLI's own rendering of a step

    print_step_outcome(outcome)
    sys.stdout.flush()


# -- signals ------------------------------------------------------------------------------


def _sigterm(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt


@contextmanager
def _interrupts() -> Iterator[None]:
    """SIGINT and SIGTERM as ``KeyboardInterrupt`` for the block (D9.3), then restored."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {
        signal.SIGINT: signal.signal(signal.SIGINT, signal.default_int_handler),
        signal.SIGTERM: signal.signal(signal.SIGTERM, _sigterm),
    }
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


# -- startup ------------------------------------------------------------------------------


def _refuse(message: str) -> NoReturn:
    raise WorkerStartupError(message)


@dataclass
class _Started:
    """What a worker that passed its startup protocol holds."""

    args: WorkerArguments
    root: Path
    namespace: IssueNamespace
    l0: ControllerLock
    l2: IssueRunLock
    config: AutoForgeConfig
    kind: ClaimKind
    state_ref_present: bool
    epic_url: str
    issue_url: str


def _adopt(args: WorkerArguments) -> tuple[Path, IssueNamespace, ControllerLock, IssueRunLock]:
    """D9.2 steps 1 and 2: both descriptors proven and made non-inheritable."""
    try:
        root = repository_lock_path(args.repository).parent.parent
    except (LockError, OSError) as exc:
        _refuse(f"{args.repository} is not a git checkout whose common dir can be read ({exc})")
    namespace = issue_namespace(root, args.issue)
    try:
        l0 = ControllerLock.adopt(args.l0_fd, expected_root=root)
    except (LockError, OSError) as exc:
        _refuse(f"the inherited L0 descriptor {args.l0_fd} was not proven: {exc}")
    try:
        l2 = IssueRunLock.adopt(args.l2_fd, namespace)
    except (LockError, OSError) as exc:
        _refuse(f"the inherited L2 descriptor {args.l2_fd} was not proven: {exc}")
    return root, namespace, l0, l2


def _start(args: WorkerArguments) -> _Started:
    """D9.2 steps 1 to 4. Nothing of the namespace's state is read here."""
    root, namespace, l0, l2 = _adopt(args)
    try:
        binding = load_binding(namespace)
        claim_file = load_claim_file(namespace)
        scheduler = load_scheduler_state(root, args.epic)
    except (StateError, OSError) as exc:
        _refuse(f"the namespace or scheduler file does not load: {exc}")
    if binding is None:
        _refuse(f"{namespace.binding_file} does not exist; the issue was never admitted")
    if binding.epic.number != args.epic:
        _refuse(f"the namespace is bound under EPIC #{binding.epic.number}, not #{args.epic}")
    current = None if claim_file is None else claim_file.current
    if claim_file is None or current is None:
        _refuse(f"{namespace.claim_file} holds no open claim; the worker has nothing to run")
    if (claim_file.run_key, claim_file.issue_id, claim_file.epic) != (
        binding.run_key,
        binding.issue_id,
        binding.epic,
    ):
        _refuse(f"{namespace.claim_file} names another run than {namespace.binding_file}")
    started_for = (args.claim_id, args.generation, args.invocation_id)
    on_disk = (current.claim_id, current.generation, current.invocation_id)
    if on_disk != started_for:
        _refuse(
            f"the open claim is generation {current.generation} (claim {current.claim_id}, "
            f"invocation {current.invocation_id}), not the generation {args.generation} (claim "
            f"{args.claim_id}, invocation {args.invocation_id}) this worker was started for"
        )
    if scheduler is None:
        _refuse(f"no scheduler.json for EPIC #{args.epic}; no scheduler started this worker")
    if scheduler.invocation.invocation_id != args.invocation_id:
        _refuse(
            f"scheduler.json names invocation {scheduler.invocation.invocation_id}, not "
            f"{args.invocation_id}"
        )
    if (scheduler.repository_id, scheduler.epic) != (binding.run_key.repository_id, binding.epic):
        _refuse("scheduler.json names another repository or EPIC than the namespace's binding")
    try:
        config = load_config_file(args.config or None)
    except ConfigurationError as exc:
        _refuse(f"the configuration does not load: {exc}")
    if config_digest(config) != scheduler.config.config_digest:
        _refuse(
            f"the configuration {args.config or '(built-in defaults)'} is not the one the "
            "scheduler started with (its digest differs from scheduler.json's): it was "
            "edited since; stop the scheduler and start it again"
        )
    repository = f"https://github.com/{scheduler.repository_owner}/{scheduler.repository_name}"
    return _Started(
        args=args,
        root=root,
        namespace=namespace,
        l0=l0,
        l2=l2,
        config=config,
        kind=current.kind,
        state_ref_present=current.state_ref.present,
        epic_url=f"{repository}/issues/{args.epic}",
        issue_url=f"{repository}/issues/{args.issue}",
    )


def _engine(
    started: _Started,
    github_factory: Callable[[AutoForgeConfig], GitHubClient] | None,
    provider_factory: Callable[[AutoForgeConfig], ProviderRegistry] | None,
) -> ControllerEngine:
    """D9.2 step 5: the engine on the namespace's state, with the scheduler policy's hints."""
    args = started.args
    config = started.config
    engine = ControllerEngine(
        config=config,
        workdir=args.repository,
        github=github_factory(config) if github_factory is not None else None,
        providers=provider_factory(config) if provider_factory is not None else None,
    )
    engine.bind_issue_namespace(started.namespace)
    config_flags: tuple[str, ...] = ("--config", args.config) if args.config else ()
    engine.cli_global_flags = (*config_flags, "--state-dir", str(started.namespace.state_dir))
    engine.continue_command = (*config_flags, "epic", "resume", started.epic_url)
    engine.config_file = args.config
    engine.require_local_lock_filesystem = True
    engine.progress_output = _progress_line
    return engine


def _should_continue(started: _Started) -> Callable[[], bool]:
    """The step-boundary predicate (D13.3, D16.4): L2 still held, no stop request for us."""

    def should_continue() -> bool:
        try:
            started.l2.prove_held()
        except (LockError, OSError) as exc:
            _say(f"stopping before the next step: the run lock (L2) is not proven held ({exc})")
            return False
        try:
            stop = read_stop_request(started.root, started.args.epic)
        except (StateError, OSError) as exc:
            _say(f"stopping before the next step: the stop request cannot be read ({exc})")
            return False
        if stop.applies_to(started.args.invocation_id):
            if stop.request is None:
                _say(f"a stop request that does not parse is a drain ({stop.problem})")
            _say("stopping at this step boundary: the scheduler is draining")
            return False
        return True

    return should_continue


def _run(
    started: _Started,
    github_factory: Callable[[AutoForgeConfig], GitHubClient] | None,
    provider_factory: Callable[[AutoForgeConfig], ProviderRegistry] | None,
) -> int:
    args = started.args
    engine = _engine(started, github_factory, provider_factory)
    with engine.locked(held=started.l0):
        existing = engine.existing_run()
        if existing is None:
            if started.state_ref_present:
                _refuse(
                    "the claim was admitted with a state, and the namespace holds none; "
                    "the scheduler classifies it again"
                )
            engine.validate_config()
            # The start path of ``autoforge run <epic> <issue>`` (D9.5).
            engine.new_run(started.epic_url, started.issue_url)
            engine.record_run_configuration()
            engine.save()
        else:
            if started.kind is ClaimKind.START:
                _refuse("the claim was admitted with no state, and the namespace holds one")
            state = engine.load()
            if (
                parse_issue_url(state.current_issue_url).number != args.issue
                or parse_issue_url(state.epic_url).number != args.epic
            ):
                _refuse(f"the namespace's state is not issue #{args.issue} of EPIC #{args.epic}")
            if state.phase not in WORKER_STOP_PHASES:
                notice = engine.check_run_configuration(
                    ("epic", "resume", started.epic_url),
                    accept_change=args.accept_config_change,
                )
                if notice:
                    _say(notice)
        engine.run(
            max_steps=WORKER_MAX_STEPS,
            allow_merge=False,
            on_outcome=_print_outcome,
            extra_stop_phases=WORKER_EXTRA_STOP_PHASES,
            should_continue=_should_continue(started),
        )
    final = engine.state
    assert final is not None
    _say(
        f"issue #{args.issue} stopped in phase {final.phase.value} (settlement "
        f"{issue_settlement(final).value})"
    )
    if final.phase in (Phase.BLOCKED, Phase.FAILED):
        _say(f"run {final.run_id} is {final.phase.value}: {final.block_reason or '-'}")
        return 1
    return 0


def run_worker(
    argv: Sequence[str],
    *,
    github_factory: Callable[[AutoForgeConfig], GitHubClient] | None = None,
    provider_factory: Callable[[AutoForgeConfig], ProviderRegistry] | None = None,
) -> int:
    """``autoforge epic _worker ARGV``: run one admitted issue; the process's exit status.

    0 once the run stopped at its boundary (READY_FOR_MERGE, a stop
    request, a step limit), 1 for a BLOCKED or FAILED run or another
    controller error, 2 for a refused start or a configuration, state or
    lock error (nothing was written by a refused start), 130 after SIGINT or
    SIGTERM. The status is a diagnostic: the scheduler decides by the state
    the worker left (#181's ``finalize_worker``). ``github_factory`` and
    ``provider_factory`` build the engine's client and providers from the
    loaded configuration (tests inject fakes); by default the engine builds
    its own.
    """
    with _bounded_output(), _interrupts():
        try:
            try:
                started = _start(parse_worker_arguments(argv))
            except WorkerStartupError as exc:
                _say(f"refused to start, nothing was read or written: {exc}")
                return EXIT_REFUSED
            return _run(started, github_factory, provider_factory)
        except KeyboardInterrupt:
            _say("interrupted; the state is as the last step saved it")
            return EXIT_INTERRUPTED
        except WorkerStartupError as exc:
            _say(f"refused to run, nothing was written: {exc}")
            return EXIT_REFUSED
        except (ConfigurationError, StateError, StateTransitionError, LockError) as exc:
            _say(f"error: {exc}")
            return EXIT_REFUSED
        except AutoForgeError as exc:
            _say(f"error: {exc}")
            return 1
