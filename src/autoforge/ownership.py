"""Who may act on an issue: a standalone run or the parallel EPIC scheduler (ADR 0008 D17.2).

L0 excludes a standalone command and a scheduler *in time*; these guards
exclude them *in ownership*, so that no issue ever has two states that both
continue it.

* **Forward guard.** A standalone controller refuses an issue a scheduler
  namespace owns. :func:`scheduler_ownership` is the predicate: a namespace
  owns its issue when it holds any ``state/state.json``, read with #178's
  lookup (:func:`~autoforge.run_namespace.load_issue_namespace`), or when a
  live owner holds its run lock (L2, the lock-free probe
  :func:`~autoforge.locking.issue_run_lock_live`). #181 adds the open-claim
  half once the claim schema exists. It fails closed: a namespace that
  cannot be read, or a run lock that cannot be probed, owns its issue. The
  CLI applies it under L0 to the issue ``run``, ``step``, ``resume`` and
  ``unblock`` would act on (:func:`refuse_scheduler_owned_issue`), and hands
  a standalone engine the same predicate
  (:func:`standalone_ownership_predicate`) as one more rejection fact of
  ``_verify_issue_selectable``, which covers a sequential run's
  ``next_issue_url``; the engine never reads scheduler files itself. A
  worker engine is given no predicate, and ``unblock`` of a namespace's own
  state directory exempts that namespace.
* **Reverse guard.** :func:`standalone_hold` reports the issue the
  configured standalone state names as ``current_issue_url`` while its phase
  is anything but ``DONE`` (``BLOCKED`` and ``FAILED`` included), so a
  scheduler namespace never launders a standalone run's loop bounds or
  bypasses its ``unblock``; #182's admission holds it as
  ``held:standalone-state``. An unreadable standalone state is reported and
  holds nothing.

Everything here only reads: nothing is created, written, quarantined or
locked, and no GitHub or git process runs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError, LockError, SchedulerOwnedIssueError, StateError
from .locking import issue_run_lock_live
from .redaction import redact
from .run_namespace import StateStatus, issue_namespace, load_issue_namespace
from .safefs import SafeRoot
from .state import STATE_FILENAME, load_state
from .transitions import Phase, WorkflowMode
from .validation import parse_issue_url

# A reason quotes a loader's or a probe's message: redacted, then bounded.
MAX_REASON_CHARS = 1000


def _bounded(text: str) -> str:
    text = redact(text)
    if len(text) <= MAX_REASON_CHARS:
        return text
    marker = " [... truncated]"
    return text[: MAX_REASON_CHARS - len(marker)] + marker


def scheduler_ownership(coordination_root: str | Path, issue_number: int) -> str:
    """Why a scheduler namespace owns issue ``issue_number``; "" when none does.

    The state half of ADR 0008 D17.2's ownership predicate (#180): the
    namespace holds a state (loaded or not), or a live owner holds its run
    lock. Fail-closed: a namespace whose binding or directories cannot be
    read, or whose run lock cannot be probed, owns its issue. Read-only.
    """
    try:
        namespace = issue_namespace(coordination_root, issue_number)
    except StateError as exc:
        return f"no scheduler namespace can be named for it ({_bounded(str(exc))})"
    where = namespace.directory
    try:
        lookup = load_issue_namespace(namespace.coordination_root, issue_number)
    except (StateError, OSError) as exc:
        return (
            f"its scheduler namespace {where} cannot be read ({_bounded(str(exc))}), so "
            "it may own the issue"
        )
    if lookup.state.status is StateStatus.LOADED:
        assert lookup.state.state is not None
        return (
            f"its scheduler namespace {where} holds a state (phase "
            f"{lookup.state.state.phase.value})"
        )
    if lookup.state.status is StateStatus.CORRUPT:
        return (
            f"its scheduler namespace {where} holds a state that cannot be read "
            f"({lookup.state.reason})"
        )
    try:
        live = issue_run_lock_live(namespace)
    except (LockError, OSError) as exc:
        return (
            f"its run lock {namespace.run_lock_file} cannot be probed "
            f"({_bounded(str(exc))}), so a worker may own the issue"
        )
    if live:
        return f"a live scheduler worker holds its run lock {namespace.run_lock_file}"
    return ""


def standalone_ownership_predicate(
    coordination_root: str | Path, *, exempt_issue: int | None = None
) -> Callable[[int], str]:
    """The predicate a standalone engine is given (``ControllerEngine.issue_ownership``).

    ``exempt_issue`` is the namespace whose own state directory ``unblock``
    acts on; it is never refused. A worker engine is given no predicate.
    """
    root = Path(coordination_root)

    def owner(issue_number: int) -> str:
        if exempt_issue is not None and issue_number == exempt_issue:
            return ""
        return scheduler_ownership(root, issue_number)

    return owner


def refuse_scheduler_owned_issue(
    coordination_root: str | Path,
    issue_url: str,
    command: str,
    *,
    exempt_issue: int | None = None,
) -> None:
    """The CLI's forward guard: refuse ``command`` on a scheduler-owned issue.

    Called under L0, before anything is written, with the issue the command
    would act on (the ``run`` argument, or the loaded state's
    ``current_issue_url``). Raises
    :class:`~autoforge.errors.SchedulerOwnedIssueError` (exit 2) naming
    ``epic archive``. An issue URL that does not parse is the caller's
    validation to report, not a refusal here.
    """
    try:
        ref = parse_issue_url(issue_url)
    except ConfigurationError:
        return
    if exempt_issue is not None and ref.number == exempt_issue:
        return
    reason = scheduler_ownership(coordination_root, ref.number)
    if reason:
        raise SchedulerOwnedIssueError(
            f"'{command}' refuses issue {ref.canonical}: {reason}. Only the parallel EPIC "
            "scheduler continues an issue its namespace owns ('autoforge epic resume', "
            "inspected with 'autoforge epic status'); 'autoforge epic archive <epic> "
            f"{ref.number}' releases it to a standalone run. Nothing was written."
        )


@dataclass(frozen=True)
class StandaloneHold:
    """What the configured standalone state holds against a scheduler admission.

    ``issue_number`` (with ``issue_url`` and ``phase``) is set when the state
    names an issue in a phase other than ``DONE``; ``problem`` (redacted and
    bounded) when the state exists but cannot be read, which holds nothing
    and is reported.
    """

    state_file: Path
    issue_number: int | None = None
    issue_url: str = ""
    phase: str = ""
    problem: str = ""

    @property
    def holds(self) -> bool:
        return self.issue_number is not None


def standalone_hold(state_dir: str | Path) -> StandaloneHold:
    """The issue the standalone state in ``state_dir`` holds; read-only (ADR 0008 D17.2).

    ``state_dir`` is the configured one (``config.state_dir``); other
    ``--state-dir`` locations are unknowable, and the forward guard refuses
    to continue them instead. An absent state, a ``DONE`` state, a LOCAL
    state and a state naming no issue hold nothing. Nothing is created,
    quarantined or written.
    """
    state_file = Path(state_dir) / STATE_FILENAME
    try:
        # Read through a held root, like every state read (load_state's
        # ``root=``): the directory cannot be swapped between the checks.
        root = SafeRoot.open(state_dir)
    except FileNotFoundError:
        return StandaloneHold(state_file)
    except (StateError, OSError) as exc:
        return StandaloneHold(state_file, problem=_bounded(str(exc)))
    with root:
        try:
            if root.lstat(STATE_FILENAME) is None:
                return StandaloneHold(state_file)
            state = load_state(state_file, root=root)
        except StateError as exc:
            try:
                gone = root.lstat(STATE_FILENAME) is None  # removed since: nothing is there
            except StateError:
                gone = False
            if gone:
                return StandaloneHold(state_file)
            return StandaloneHold(state_file, problem=_bounded(str(exc)))
    if state.mode is WorkflowMode.LOCAL or state.phase is Phase.DONE:
        return StandaloneHold(state_file, phase=state.phase.value)
    if not state.current_issue_url:
        return StandaloneHold(state_file, phase=state.phase.value)
    try:
        ref = parse_issue_url(state.current_issue_url)
    except ConfigurationError as exc:
        return StandaloneHold(state_file, phase=state.phase.value, problem=_bounded(str(exc)))
    return StandaloneHold(
        state_file,
        issue_number=ref.number,
        issue_url=ref.canonical,
        phase=state.phase.value,
    )
