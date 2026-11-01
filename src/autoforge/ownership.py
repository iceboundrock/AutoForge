"""Who may act on an issue: a standalone run or the parallel EPIC scheduler (ADR 0008 D17.2).

L0 excludes a standalone command and a scheduler *in time*; these guards
exclude them *in ownership*, so that no issue ever has two states that both
continue it.

* **Forward guard.** A standalone controller refuses an issue a scheduler
  namespace owns. :func:`scheduler_ownership` is the predicate: a namespace
  owns its issue when it holds any ``state/state.json``, read with #178's
  lookup (:func:`~autoforge.run_namespace.load_issue_namespace`), when its
  ``claim.json`` holds an open scheduler claim (#181,
  :func:`~autoforge.scheduler_state.load_claim_file`), or when a live owner
  holds its run lock (L2, the lock-free probe
  :func:`~autoforge.locking.issue_run_lock_live`). It fails closed: a
  namespace or a claim file that cannot be read (an unknown claim schema
  included), or a run lock that cannot be probed, owns its issue. The
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
  holds nothing, with one exception: one whose ``protocol_version`` this
  controller does not read (a newer protocol's, or a label no protocol has;
  :class:`~autoforge.errors.StateProtocolError`) may be a live run another
  controller continues, and which issue it holds is a field of the unloaded
  file, which nothing here trusts. It therefore blocks every admission
  (:attr:`StandaloneHold.blocks_admission`, :func:`admission_refusal`), as
  global uncertainty does, until a controller that reads it is used (#265).

Everything here only reads: nothing is created, written, quarantined or
locked, and no GitHub or git process runs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .errors import (
    ConfigurationError,
    LockError,
    SchedulerOwnedIssueError,
    StateError,
    StateProtocolError,
)
from .locking import issue_run_lock_live
from .provenance import shown, shown_path
from .redaction import redact
from .run_namespace import StateStatus, issue_namespace, load_issue_namespace
from .safefs import SafeRoot
from .scheduler_state import load_claim_file
from .state import STATE_FILENAME, load_state
from .transitions import Phase
from .validation import parse_issue_url

# A reason quotes a loader's or a probe's message: redacted, then bounded.
MAX_REASON_CHARS = 1000


def _bounded(text: str) -> str:
    text = redact(text)
    if len(text) <= MAX_REASON_CHARS:
        return text
    marker = " [... truncated]"
    return text[: MAX_REASON_CHARS - len(marker)] + marker


def _shown_reason(text: str) -> str:
    """A loader's message about the standalone state, as :attr:`StandaloneHold.problem` keeps it.

    Redacted, then escaped as one printable line when it holds a character
    that is not printable, then bounded (:func:`~autoforge.provenance.shown`):
    a loader's message names the state file as the process has it, and the
    configured directory may hold a newline or an escape sequence (#265
    R2-F1).
    """
    return shown(text, MAX_REASON_CHARS)


def scheduler_ownership(coordination_root: str | Path, issue_number: int) -> str:
    """Why a scheduler namespace owns issue ``issue_number``; "" when none does.

    ADR 0008 D17.2's ownership predicate: the namespace holds a state
    (loaded or not, #180), its claim file holds an open claim (#181), or a
    live owner holds its run lock (#180). Fail-closed: a namespace whose
    binding or directories cannot be read, a claim file that cannot be read
    or is of an unknown schema, or a run lock that cannot be probed, owns
    its issue. A claim file holding only closed claims owns nothing: the
    namespace's next run starts fresh. Read-only.
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
    if lookup.claim_present:
        try:
            claim_file = load_claim_file(namespace)
        except (StateError, OSError) as exc:
            return (
                f"its scheduler claim {namespace.claim_file} cannot be read "
                f"({_bounded(str(exc))}), so it may hold an open claim"
            )
        if claim_file is not None and claim_file.current is not None:
            current = claim_file.current
            return (
                f"its scheduler namespace {where} holds an open scheduler claim (generation "
                f"{current.generation}, a {current.kind.value} admission)"
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
    names an issue in a phase other than ``DONE``; ``problem`` (redacted, one
    printable line and bounded: :func:`~autoforge.provenance.shown`) when
    the state exists but cannot be read, which holds nothing and is
    reported. ``state_file`` is the path as the process has it, a fact that
    is not safe to print as it is: every report names it by
    :attr:`shown_state_file` (#265 R2-F1). ``refused_protocol`` is set, besides ``problem``, when
    what refused it is its ``protocol_version`` (#265): the
    :class:`~autoforge.state.ProtocolSupport` value, ``unsupported-newer``
    or ``unsupported-unknown``, with ``newer_protocol`` the newer label, a
    decimal number, or ``""``. Nothing else of that file is kept.
    """

    state_file: Path
    issue_number: int | None = None
    issue_url: str = ""
    phase: str = ""
    problem: str = ""
    refused_protocol: str = ""
    newer_protocol: str = ""

    @property
    def holds(self) -> bool:
        return self.issue_number is not None

    @property
    def shown_state_file(self) -> str:
        """``state_file`` as a report shows it (#265 R2-F1).

        Redacted, escaped as one printable line when it holds a character
        that is not printable (a newline, an escape sequence), and cut past
        :data:`~autoforge.provenance.MAX_PATH_CHARS`, in that order
        (:func:`~autoforge.provenance.shown_path`): the directory is the
        operator's configuration, and no character of it may forge or
        colour a line of the scheduler's output.
        """
        return shown_path(str(self.state_file))

    @property
    def blocks_admission(self) -> bool:
        """Whether no issue may be admitted while this state is there (#265 R1-F1).

        A state of a protocol this controller does not read was not loaded,
        so the issue it holds is unknown: it could be any issue of the EPIC,
        and a controller that reads it may be continuing it. Holding the one
        issue its ``current_issue_url`` names would trust a field of a file
        that was refused, so every admission is refused instead.
        """
        return bool(self.refused_protocol)


def unreadable_report(hold: StandaloneHold) -> str:
    """The line reporting that ``hold``'s state cannot be read; ``""`` when nothing is wrong.

    The one rendering of the problem every scheduler report prints (``epic
    run`` and ``epic resume``, ``epic status`` and ``epic run --dry-run``):
    the path is :attr:`StandaloneHold.shown_state_file` and the problem is
    already one printable line, so the whole line is (#265 R2-F1).
    """
    if not hold.problem:
        return ""
    return f"the standalone state {hold.shown_state_file} cannot be read: {hold.problem}"


def admission_refusal(hold: StandaloneHold) -> str:
    """Why a scheduler admits nothing while ``hold``'s state is there; ``""`` when it may.

    One line, its path shown safely (:func:`~autoforge.provenance.shown_path`),
    naming no field of the refused file but its newer label, a decimal
    number. A newer state points at a controller that speaks that protocol,
    which reads the standalone state and holds the one issue it names; a
    label no protocol has points at the operator's recovery in
    ``docs/usage.md``. Neither moves, edits or archives anything.
    """
    if not hold.blocks_admission:
        return ""
    if hold.newer_protocol:
        then = (
            f"a controller that speaks protocol {hold.newer_protocol} reads it and holds only "
            "the issue it runs: use that controller for 'epic run' and 'epic resume', from "
            "this checkout and with this --config"
        )
    else:
        then = (
            "no AutoForge protocol has its label, so no controller reads it as it is; see "
            "'A run a newer controller wrote' in docs/usage.md"
        )
    return (
        f"the standalone state {hold.shown_state_file} is of a protocol this "
        "controller does not read, so the issue its run holds is unknown and no issue is "
        f"admitted while it is there; {then}"
    )


def standalone_hold(state_dir: str | Path) -> StandaloneHold:
    """The issue the standalone state in ``state_dir`` holds; read-only (ADR 0008 D17.2).

    ``state_dir`` is the configured one (``config.state_dir``); other
    ``--state-dir`` locations are unknowable, and the forward guard refuses
    to continue them instead. An absent state, a ``DONE`` state and a state
    naming no issue hold nothing; one that does not load, a LOCAL one among
    them (ADR 0009 D4), holds nothing either and is the hold's ``problem``.
    One refused for its ``protocol_version`` is a ``problem`` too, and
    :attr:`StandaloneHold.blocks_admission` (#265). Nothing is created,
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
        return StandaloneHold(state_file, problem=_shown_reason(str(exc)))
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
            if isinstance(exc, StateProtocolError):
                return StandaloneHold(
                    state_file,
                    problem=_shown_reason(str(exc)),
                    refused_protocol=exc.support,
                    newer_protocol=exc.newer,
                )
            return StandaloneHold(state_file, problem=_shown_reason(str(exc)))
    if state.phase is Phase.DONE:
        return StandaloneHold(state_file, phase=state.phase.value)
    if not state.current_issue_url:
        return StandaloneHold(state_file, phase=state.phase.value)
    try:
        ref = parse_issue_url(state.current_issue_url)
    except ConfigurationError as exc:
        return StandaloneHold(state_file, phase=state.phase.value, problem=_shown_reason(str(exc)))
    return StandaloneHold(
        state_file,
        issue_number=ref.number,
        issue_url=ref.canonical,
        phase=state.phase.value,
    )
