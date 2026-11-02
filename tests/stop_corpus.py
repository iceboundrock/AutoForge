"""How a set of AutoForge runs stopped, from their state files and run logs, read only (#369).

No test: the corpus protocol of ADR 0011 (D7), which a maintainer runs by
hand over the state directories of past runs. It finds every run a state
directory still holds evidence of, classifies each one once with
:mod:`autoforge.stop_classification`, counts its run log's launches,
operator actions and bookkeeping lines from typed fields only, and prints a
summary (``--json``: the ``autoforge.stop-corpus/4`` document)::

    uv run python -m tests.stop_corpus ~/code/AutoForge/.autoforge
    uv run python -m tests.stop_corpus --json RUN_DIR [RUN_DIR ...] > corpus.json

A path is a state directory or a directory whose subdirectories, four
levels down at most (so ``<git common dir>/autoforge`` reaches every
scheduler namespace's ``scheduler/issues/<n>/state``), are. A state
directory holds a ``state.json``, a state moved aside (``state.json.archived-*``
by ``epic archive``, ``state.json.corrupt-*`` by ``run --force``) or a run
log (``logs/<run id>/``). Nothing is written, locked, launched or sent, and
no state file or run log that is not a regular file is opened: a state is
read by the controller's own strict loader
(:func:`autoforge.state.load_state`), which writes nothing, and a run log
descriptor-relative, links unfollowed, within its size bound
(:class:`autoforge.safefs.SafeRoot`).

**Units, each counted once in its own denominator, never summed across:**

- a *run* is one run id, wherever its evidence is: the current state, a
  state moved aside, a run log (a run a new standalone run replaced keeps
  only its log). Found twice (a directory given twice, a copied one), it is
  still one run, classified by one state file and counted from one run log;
  the copies are counted, not added. It has one eligibility
  (:data:`ELIGIBILITY`): ``intentional`` (DONE, READY_FOR_MERGE, a
  scheduler's hand-off), ``deterministic`` (a phase ``resume`` re-enters),
  ``needs_human`` (BLOCKED, FAILED, a pause) or ``undecidable`` (no state of
  it loads, its outcome is UNKNOWN, or it depends on a view nothing
  establishes). Each state is classified in its directory's view
  (:func:`state_dir_view`): a scheduler namespace's, where MERGE and
  UPDATE_EPIC are hand-offs and every route is the scheduler's, a
  standalone run's, or unknown;
- a state file that does not load and names no run id that can be read (a
  torn file) is counted apart, never as a run: it may be one of the runs, or
  another;
- a *launch* is one run log line of an agent or a controller-run command,
  told apart by the producer it names (an agent's provider and profile, a
  controller command's ``(controller ...)`` profile), never by a limit, and
  of kind ``unknown`` when its producer cannot be established; it failed
  when it timed out, exited non-zero or recorded an error; a failed agent
  launch's code comes from ``timed_out``, ``ended_by`` and ``exit_code``,
  and is ``unknown`` when only the error text says more;
- an *operator action* is one run log line of an operator's ``unblock``
  (applied or refused) or accepted configuration change. It is no launch.
  Each applied ``unblock`` ended one BLOCKED episode, which needed a human
  by the exit rule, whatever its reason;
- a *bookkeeping line* is one run log line the controller wrote of itself,
  recording no invocation: a ``(...; no agent)`` profile no operator action
  has, with no provider and no command (the EPIC a new run binds, #293). It
  is no launch, so it is in no launch total and no failure rate.

**What it cannot say** (printed with every summary, :data:`LIMITATIONS`):
no reason code is persisted (ADR 0011 D5), so a saved BLOCKED or FAILED
classifies ``unknown`` and the free text of ``block_reason`` and of a
launch's ``error`` is never read; a run whose state is gone or does not
load, and a run log that was moved, deleted, torn or holds a line of an
unexpected type, leave that run's history incomplete (each such run is
flagged, and each bad line counted, never parsed around); a path given or
found on the way down that cannot be read is reported with the stage that
failed, and the summary says its input is incomplete; and the corpus is
the directories given, nothing else: whatever selected them is its bias.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from autoforge.errors import StateError
from autoforge.run_namespace import (
    AUTOFORGE_DIRNAME,
    BINDING_FILENAME,
    CLAIM_FILENAME,
    SCHEDULER_DIRNAME,
    STATE_DIRNAME,
    namespace_issue_of_state_dir,
)
from autoforge.runlog import MAX_EVENT_JOURNAL_BYTES, RUN_ID_RE
from autoforge.safefs import ReadLimitExceeded, SafeRoot, UnsafePathError, entry_kind
from autoforge.state import (
    ARCHIVED_SUFFIX,
    CORRUPT_SUFFIX,
    LOGS_DIRNAME,
    MAX_STATE_FILE_BYTES,
    STATE_FILENAME,
    AutoForgeState,
    load_state,
)
from autoforge.stop_classification import (
    ReasonSource,
    StopClassification,
    StopOutcome,
    StopReasonCode,
    classify_exception,
    classify_state,
)

SCHEMA = "autoforge.stop-corpus/4"
# How deep below a given path a state directory is looked for.
MAX_DEPTH = 4
JOURNAL_NAME = "events.jsonl"

# The eligibility of each final outcome; total over StopOutcome (tested).
ELIGIBILITY: dict[StopOutcome, str] = {
    StopOutcome.DONE: "intentional",
    StopOutcome.READY_FOR_MERGE: "intentional",
    StopOutcome.HANDOFF: "intentional",
    StopOutcome.IN_PROGRESS: "deterministic",
    StopOutcome.RESUMABLE_ERROR: "deterministic",
    StopOutcome.BLOCKED: "needs_human",
    StopOutcome.FAILED: "needs_human",
    StopOutcome.AWAITING_INPUT: "needs_human",
    StopOutcome.REFUSED: "undecidable",
    StopOutcome.UNKNOWN: "undecidable",
}
ELIGIBILITIES = ("intentional", "deterministic", "needs_human", "undecidable")

# A run whose run log survives and none of whose state files does: nothing
# typed says how it stopped.
NO_STATE = StopClassification(
    StopOutcome.UNKNOWN,
    StopReasonCode.UNKNOWN,
    ReasonSource.STATE,
    "no state file of this run was found",
)

LIMITATIONS = (
    "no reason code is persisted: a saved BLOCKED or FAILED classifies 'unknown', and no "
    "block_reason or error text is read",
    "a failed agent launch that exited 0 (a malformed result, a failed verification, a "
    "refused run log) has no typed cause in the journal: 'unknown'",
    "a launch is an agent's or a controller command's by the producer its line names (an "
    "agent's provider and profile, a '(controller ...)' profile), never by a limit, which "
    "agent lines also carried before #193; a line that names neither, or both, is kind "
    "'unknown'",
    "a line is bookkeeping, no launch, only when its profile says '(...; no agent)', it is "
    "no operator's, and it names no provider and no command; one that says no agent and "
    "names either contradicts itself and is a launch of kind 'unknown'",
    "a run is a run id: one whose state is gone (a new standalone run replaced it) or does "
    "not load counts as 'undecidable', and its run log is still counted",
    "a state file that does not load and names no readable run id is counted apart, never "
    "as a run: it may be one of the runs counted, or another",
    "a run log that was moved, deleted, torn, too large or not a regular file, or that holds "
    "a line of another run or of an unexpected type, leaves its run's history incomplete; "
    "such runs are flagged and each bad line counted, never parsed around",
    "whose routes a run takes, a standalone run's or the scheduler's, is read from where its "
    "state directory lies (a namespace's .../autoforge/scheduler/issues/<n>/state): one with "
    "namespace evidence but not that layout (named 'state', under a scheduler root, beside a "
    "binding.json or claim.json) is 'unknown', and a MERGE or UPDATE_EPIC there is "
    "'undecidable'; a namespace's state directory copied elsewhere under another name with "
    "nothing of its namespace beside it reads as a standalone run's",
    "a path given, or a directory found on the way down, that cannot be listed or read is "
    "reported with the stage that failed, and what it holds is in no denominator: the "
    "summary says the input is incomplete",
    "the corpus is the directories given and nothing else: what selected them is its "
    "selection bias, and no denominator reaches past them",
)

# A state file's name: the current one, or one the controller moved aside.
_STATE_KIND_RANK = {"current": 0, "archived": 1, "quarantined": 2}


def state_file_kind(name: str) -> str | None:
    """``current``, ``archived`` or ``quarantined`` for a state file's name, else ``None``."""
    if name == STATE_FILENAME:
        return "current"
    for suffix, kind in ((ARCHIVED_SUFFIX, "archived"), (CORRUPT_SUFFIX, "quarantined")):
        prefix = STATE_FILENAME + suffix
        if name.startswith(prefix) and len(name) > len(prefix):
            return kind
    return None


def _run_id_name(name: str) -> bool:
    return bool(RUN_ID_RE.match(name)) and name not in (".", "..")


# -- discovery ------------------------------------------------------------------------------


# A path that could not be read, the stage that failed and why: never dropped.
Unreadable = tuple[str, str, str]


@dataclass
class Discovery:
    """The state directories found, and every path on the way that could not be read."""

    state_dirs: list[Path] = field(default_factory=list)
    #: ``(path, error)``: a path given, or found on the way down, that could
    #: not be listed or looked at, so what it holds is in no denominator.
    errors: dict[str, str] = field(default_factory=dict)

    def failed(self, path: Path | str, why: str) -> None:
        self.errors.setdefault(str(path), why)


def _is_state_dir(directory: Path, discovery: Discovery) -> bool:
    """Whether ``directory`` holds a state file or a run log; nothing in it is opened.

    What cannot be listed or looked at (the directory, its ``logs``, a run's
    log directory) is reported on ``discovery``, never taken for an absence.
    """
    try:
        names = os.listdir(directory)
    except OSError as exc:
        discovery.failed(directory, type(exc).__name__)
        return False
    if any(state_file_kind(name) for name in names):
        return True
    if LOGS_DIRNAME not in names:
        return False
    logs = directory / LOGS_DIRNAME
    try:
        if not stat.S_ISDIR(os.lstat(logs).st_mode):
            return False
        runs = sorted(name for name in os.listdir(logs) if _run_id_name(name))
    except OSError as exc:
        discovery.failed(logs, type(exc).__name__)
        return False
    problems: list[tuple[Path, str]] = []
    for name in runs:
        try:
            os.lstat(logs / name / JOURNAL_NAME)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            problems.append((logs / name, type(exc).__name__))
            continue
        # A state directory: the scan reads, or flags, each of its run logs.
        return True
    for path, why in problems:
        discovery.failed(path, why)
    return False


def discover(paths: Iterable[Path], max_depth: int = MAX_DEPTH) -> Discovery:
    """Every state directory at or below each path, links unfollowed, each once, sorted.

    A path given that does not exist, is a link (never followed) or is no
    directory, and a directory on the way down that cannot be listed, is
    reported with why; every other path is still searched.
    """
    discovery = Discovery()
    found: set[Path] = set()
    for given in paths:
        try:
            mode = os.lstat(given).st_mode
        except OSError as exc:
            discovery.failed(given, type(exc).__name__)
            continue
        if stat.S_ISLNK(mode):
            discovery.failed(given, "a symbolic link, not followed")
            continue
        if not stat.S_ISDIR(mode):
            discovery.failed(given, "not a directory")
            continue
        level = [given]
        for depth in range(max_depth + 1):
            below: list[Path] = []
            for directory in level:
                if _is_state_dir(directory, discovery):
                    found.add(directory.resolve())
                    continue
                if depth == max_depth or str(directory) in discovery.errors:
                    continue
                try:
                    with os.scandir(directory) as entries:
                        children = sorted(
                            e.path for e in entries if e.is_dir(follow_symlinks=False)
                        )
                except OSError as exc:
                    discovery.failed(directory, type(exc).__name__)
                    continue
                below += [Path(child) for child in children]
            level = below
    discovery.state_dirs = sorted(found)
    return discovery


def find_state_dirs(paths: Iterable[Path], max_depth: int = MAX_DEPTH) -> list[Path]:
    """Every state directory at or below each path (:func:`discover`, its errors dropped)."""
    return discover(paths, max_depth).state_dirs


# -- whose routes a state directory's runs take ----------------------------------------------

VIEWS = ("standalone", "scheduler", "unknown")


def state_dir_view(state_dir: Path) -> bool | None:
    """The view a state directory's runs are classified in (ADR 0008 D17.3, ADR 0011 D6).

    ``True``, the scheduler's, for a namespace's own state directory,
    ``<...>/autoforge/scheduler/issues/<n>/state``, by the controller's own
    predicate (:func:`autoforge.run_namespace.namespace_issue_of_state_dir`,
    with the directory four levels up as the coordination root): its workers
    never execute ``MERGE`` or ``UPDATE_EPIC``, and ``resume`` and ``run``
    refuse it. ``None``, not known, where there is namespace evidence
    without that layout (a directory named ``state``, one below an
    ``autoforge/scheduler`` directory, one beside a namespace's
    ``binding.json`` or ``claim.json``: a namespace copied elsewhere) or the
    evidence cannot be read. ``False``, a standalone run's, otherwise. Reads
    nothing but the path and whether two names exist beside it.
    """
    parents = state_dir.parents
    if len(parents) > 4 and namespace_issue_of_state_dir(state_dir, parents[4]) is not None:
        return True
    parts = state_dir.parts
    if state_dir.name == STATE_DIRNAME or any(
        a == AUTOFORGE_DIRNAME and b == SCHEDULER_DIRNAME
        for a, b in zip(parts, parts[1:], strict=False)
    ):
        return None
    for name in (BINDING_FILENAME, CLAIM_FILENAME):
        try:
            os.lstat(state_dir.parent / name)
        except FileNotFoundError:
            continue
        except OSError:
            return None
        return None
    return False


# -- one state directory, read only ----------------------------------------------------------


@dataclass
class StateSource:
    """One state file: the run it names and how it stopped, or why it could not say."""

    state_dir: str
    name: str
    kind: str
    run_id: str = ""
    state: AutoForgeState | None = None
    final: StopClassification = NO_STATE
    #: The view is not known and the two views classify the state apart (a
    #: hand-off phase: a standalone run's ``resume``, a scheduler's hand-off).
    view_decides: bool = False

    def rank(self) -> tuple[int, int]:
        return (0 if self.state is not None else 1, _STATE_KIND_RANK[self.kind])


@dataclass
class JournalCounts:
    """One run log, counted from its lines' typed fields alone."""

    state_dir: str
    run_id: str
    status: str = "absent"  # read | absent | too-large | not-regular | unreadable
    launches: Counter[str] = field(default_factory=Counter)
    failed_launches: Counter[str] = field(default_factory=Counter)
    operator_actions: Counter[str] = field(default_factory=Counter)
    #: Lines the controller wrote of itself, recording no invocation.
    bookkeeping: Counter[str] = field(default_factory=Counter)
    torn_lines: int = 0
    foreign_lines: int = 0
    malformed_lines: int = 0


@dataclass
class DirScan:
    state_dir: str
    scheduler: bool | None = False
    states: list[StateSource] = field(default_factory=list)
    journals: list[JournalCounts] = field(default_factory=list)
    error: str = ""


def _open_dir(directory: Path) -> SafeRoot:
    """A root on ``directory`` whose last component is opened without following a link."""
    with SafeRoot.open(directory.parent) as parent:
        return parent.subroot(directory.name)


def _identity_of(root: SafeRoot, name: str) -> str:
    """The run id a state file the loader refused names, or ``""``.

    Only for deduplicating runs: the file is read only when it is a
    regular file within the loader's bound, and nothing but a well-formed
    ``run_id`` is taken from it; the classification stays the refusal.
    """
    try:
        st = root.lstat(name)
        if st is None or entry_kind(st.st_mode) is not None or st.st_size > MAX_STATE_FILE_BYTES:
            return ""
        raw = root.read_bytes(name, limit=MAX_STATE_FILE_BYTES)
        data = json.loads(raw) if raw is not None else None
    except (StateError, OSError, ValueError, RecursionError):
        return ""
    run_id = data.get("run_id") if isinstance(data, dict) else None
    return run_id if isinstance(run_id, str) and _run_id_name(run_id) else ""


def _read_state(
    root: SafeRoot, state_dir: Path, name: str, kind: str, scheduler: bool | None
) -> StateSource:
    """One state file, classified in the view of the directory that holds it."""
    source = StateSource(state_dir=str(state_dir), name=name, kind=kind)
    try:
        state = load_state(state_dir / name, root=root)
    except Exception as exc:  # noqa: BLE001 - every refusal is a classification
        # StateError for every refusal; anything else classifies 'unknown'.
        source.final = replace(classify_exception(exc), scheduler=scheduler)
        source.run_id = _identity_of(root, name)
        return source
    source.state, source.run_id = state, state.run_id
    source.final = classify_state(state, scheduler=scheduler)
    if scheduler is None:
        views = {classify_state(state, scheduler=view).outcome for view in (False, True)}
        source.view_decides = len(views) > 1
    return source


def _journal_bytes(root: SafeRoot, relpath: str) -> tuple[str, bytes]:
    """A run log's bytes, or why there are none: never opens what is not a regular file."""
    try:
        st = root.lstat(relpath)
    except UnsafePathError:
        return "not-regular", b""  # a link or a non-directory on the way
    except (StateError, OSError):
        return "unreadable", b""
    if st is None:
        return "absent", b""
    if entry_kind(st.st_mode) is not None:
        return "not-regular", b""
    if st.st_size > MAX_EVENT_JOURNAL_BYTES:
        return "too-large", b""
    try:
        # Opened O_NOFOLLOW | O_NONBLOCK and checked on the descriptor, so a
        # file replaced since the lstat is refused, and read one byte past
        # the bound at most, whatever it has grown to.
        data = root.read_bytes(relpath, limit=MAX_EVENT_JOURNAL_BYTES)
    except ReadLimitExceeded:
        return "too-large", b""
    except UnsafePathError:
        return "not-regular", b""
    except (StateError, OSError):
        return "unreadable", b""
    if data is None:
        return "absent", b""
    return "read", data


# The typed fields a count reads, and what each may hold. A line with one of
# another type is malformed: counted, never converted or guessed at.
_FIELD_TYPES: dict[str, tuple[type, ...]] = {
    "run_id": (str,),
    "phase": (str,),
    "profile": (str,),
    "provider": (str,),
    "error": (str,),
    "ended_by": (str,),
    "timed_out": (bool,),
    "exit_code": (int, type(None)),
    "metadata": (dict,),
    "command": (list,),
}


def well_typed(record: dict) -> bool:
    """Whether every field a count reads has its type (a bool is no integer here)."""
    for name, kinds in _FIELD_TYPES.items():
        if name not in record:
            continue
        value = record[name]
        if isinstance(value, bool) and bool not in kinds:
            return False
        if not isinstance(value, kinds):
            return False
    return True


def operator_kind(record: dict) -> str | None:
    """An operator action's kind, ``""`` for none, ``None`` when its typed fields do not say."""
    if not record.get("profile", "").startswith("(operator"):
        return ""
    phase = record.get("phase", "")
    if phase.endswith("-unblock"):
        applied = (record.get("metadata") or {}).get("applied")
        if not isinstance(applied, bool):
            return None
        return "unblock-applied" if applied else "unblock-refused"
    if phase.endswith("-config-change"):
        return "configuration-change"
    return "other"


# The profile every controller-run command's record names: a validation or
# pre-merge verification command, in every engine that wrote a run log.
CONTROLLER_PROFILE_PREFIX = "(controller "
# How every engine ends the profile of a run log line that records no agent:
# an operator's action, or the controller's own bookkeeping.
NO_AGENT_SUFFIX = "; no agent)"


def bookkeeping_kind(record: dict) -> str:
    """A bookkeeping line's kind, ``""`` for none; read after :func:`operator_kind`.

    The controller writes a line of itself that records no invocation under
    a ``(...; no agent)`` profile, with no provider and no command: the EPIC
    a new run binds (``<phase>-epic-binding``, #293) is ``epic-binding``,
    another such line ``other``. It is no launch. A line that says no agent
    but names a provider or a command, and a ``(controller ...)`` profile,
    is none: what it records is not established, so it stays a launch for
    :func:`launch_kind` to type.
    """
    profile = record.get("profile", "")
    if (
        not (profile.startswith("(") and profile.endswith(NO_AGENT_SUFFIX))
        or profile.startswith(CONTROLLER_PROFILE_PREFIX)
        or record.get("provider", "")
        or record.get("command", [])
    ):
        return ""
    return "epic-binding" if record.get("phase", "").endswith("-epic-binding") else "other"


def launch_kind(record: dict) -> str:
    """``agent``, ``command`` or ``unknown``: from the record's producer identity.

    Every engine that wrote a run log named an agent launch's provider (the
    profile's, which the configuration requires) and its profile's name,
    and named a controller-run command by a ``(controller ...)`` profile
    and no provider. ``timeout_seconds`` says neither: an agent's record
    carried its wall-clock limit there until #193. A record that names both,
    or neither (a provider with a profile that says no agent, a ``(...; no
    agent)`` profile with a command, no producer at all), is ``unknown``,
    never guessed. A bookkeeping line (:func:`bookkeeping_kind`) is no
    launch and never reaches here.
    """
    profile, provider = record.get("profile", ""), record.get("provider", "")
    controller = profile.startswith(CONTROLLER_PROFILE_PREFIX)
    if provider and profile and not controller and not profile.endswith(NO_AGENT_SUFFIX):
        return "agent"
    if controller and not provider:
        return "command"
    return "unknown"


def failed(record: dict) -> bool:
    return (
        record.get("timed_out", False)
        or record.get("exit_code") not in (0, None)
        or bool(record.get("error", ""))
    )


def launch_code(record: dict) -> StopReasonCode:
    """A failed launch's code from ``timed_out``, ``ended_by`` and ``exit_code`` alone."""
    ended_by = record.get("ended_by", "")
    if record.get("timed_out", False) or ended_by in ("idle", "max_runtime"):
        return StopReasonCode.EXECUTION_TIMEOUT
    if ended_by in ("loop", "provider_failure") or record.get("exit_code") not in (0, None):
        return StopReasonCode.EXECUTION_FAILED
    return StopReasonCode.UNKNOWN


def count_journal(counts: JournalCounts, data: bytes) -> None:
    """Count each run log line once: a launch, an operator action, bookkeeping or a bad line.

    An operator's line and a bookkeeping line are each told apart before a
    launch is counted, so neither is in a launch total or a failure rate.
    """
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            counts.torn_lines += 1
            continue
        if not isinstance(record, dict):
            counts.torn_lines += 1
            continue
        if not well_typed(record):
            counts.malformed_lines += 1
            continue
        if record.get("run_id") != counts.run_id:
            counts.foreign_lines += 1
            continue
        operator = operator_kind(record)
        if operator is None:
            counts.malformed_lines += 1
        elif operator:
            counts.operator_actions[operator] += 1
        elif bookkeeping := bookkeeping_kind(record):
            counts.bookkeeping[bookkeeping] += 1
        else:
            kind = launch_kind(record)
            counts.launches[kind] += 1
            if failed(record):
                code = launch_code(record) if kind == "agent" else StopReasonCode.UNKNOWN
                counts.failed_launches[f"{kind}:{code.value}"] += 1


def _read_journal(root: SafeRoot, state_dir: Path, run_id: str) -> JournalCounts:
    counts = JournalCounts(state_dir=str(state_dir), run_id=run_id)
    counts.status, data = _journal_bytes(root, f"{LOGS_DIRNAME}/{run_id}/{JOURNAL_NAME}")
    count_journal(counts, data)
    return counts


def _logged_run_ids(root: SafeRoot) -> list[str]:
    """The run ids ``logs/`` has an entry for, whatever the entry is."""
    st = root.lstat(LOGS_DIRNAME)
    if st is None or not stat.S_ISDIR(st.st_mode):
        return []
    with root.subroot(LOGS_DIRNAME) as logs:
        return sorted(name for name in os.listdir(logs.fd) if _run_id_name(name))


def scan_state_dir(state_dir: Path) -> DirScan:
    """Every state file and run log of one state directory, read only.

    One that cannot be read is classified or flagged, never fatal: the
    directory's other runs, and every other directory, are still counted.
    """
    scan = DirScan(state_dir=str(state_dir), scheduler=state_dir_view(state_dir))
    try:
        with _open_dir(state_dir) as root:
            for name in sorted(os.listdir(root.fd)):
                kind = state_file_kind(name)
                if kind is not None:
                    scan.states.append(_read_state(root, state_dir, name, kind, scan.scheduler))
            logged = set(_logged_run_ids(root))
            named = {s.run_id for s in scan.states if s.run_id}
            for run_id in sorted(logged | named):
                scan.journals.append(_read_journal(root, state_dir, run_id))
    except Exception as exc:  # noqa: BLE001 - one directory never stops the corpus
        scan.error = type(exc).__name__
    return scan


# -- the corpus ---------------------------------------------------------------------------


@dataclass
class RunReport:
    """One run id: its final stop and its run log's counts, each source chosen once."""

    run_id: str
    state_dirs: list[str] = field(default_factory=list)
    state: str = "absent"  # loaded | refused | absent
    state_file: str = ""
    state_copies: int = 0
    final: StopClassification = NO_STATE
    eligibility: str = "undecidable"
    unblock_history: int | None = None
    journal: JournalCounts | None = None
    journal_copies: int = 0

    @property
    def journal_status(self) -> str:
        return self.journal.status if self.journal is not None else "absent"

    @property
    def incomplete(self) -> bool:
        """Whether the run's history cannot be read whole."""
        if self.state != "loaded" or self.journal is None or self.journal.status != "read":
            return True
        j = self.journal
        if j.torn_lines or j.foreign_lines or j.malformed_lines:
            return True
        return self.unblock_history != j.operator_actions["unblock-applied"]

    def as_dict(self) -> dict[str, object]:
        j = self.journal or JournalCounts(state_dir="", run_id=self.run_id)
        return {
            "run_id": self.run_id,
            "state_dirs": self.state_dirs,
            "state": self.state,
            "state_file": self.state_file,
            "state_copies": self.state_copies,
            "final": self.final.as_dict(),
            "eligibility": self.eligibility,
            "launches": dict(sorted(j.launches.items())),
            "failed_launches": dict(sorted(j.failed_launches.items())),
            "operator_actions": dict(sorted(j.operator_actions.items())),
            "bookkeeping": dict(sorted(j.bookkeeping.items())),
            "unblock_history": self.unblock_history,
            "journal": self.journal_status,
            "journal_copies": self.journal_copies,
            "torn_lines": j.torn_lines,
            "foreign_lines": j.foreign_lines,
            "malformed_lines": j.malformed_lines,
            "incomplete": self.incomplete,
        }


@dataclass
class Corpus:
    runs: list[RunReport]
    unidentified_states: list[StateSource]
    #: ``(path, stage, error)``: ``discovery`` for a path given or on the way
    #: down that could not be listed, ``scan`` for a state directory found
    #: whose reading failed. Its runs are in no denominator.
    unreadable: list[Unreadable] = field(default_factory=list)


def _pick_state(sources: list[StateSource]) -> StateSource:
    """The state file a run is classified by: one that loads before one that does not,
    the current one before one moved aside, the newest of those moved aside first."""
    newest_first = sorted(sources, key=lambda s: (s.state_dir, s.name), reverse=True)
    return min(newest_first, key=StateSource.rank)


def build_corpus(scans: Sequence[DirScan], discovery: Discovery | None = None) -> Corpus:
    """Each run id once, however many directories hold its evidence."""
    states: dict[str, list[StateSource]] = {}
    journals: dict[str, list[JournalCounts]] = {}
    dirs: dict[str, list[str]] = {}
    views = {scan.state_dir: scan.scheduler for scan in scans}
    unidentified: list[StateSource] = []
    for scan in scans:
        for source in scan.states:
            if not source.run_id:
                unidentified.append(source)
                continue
            states.setdefault(source.run_id, []).append(source)
            dirs.setdefault(source.run_id, []).append(scan.state_dir)
        for counts in scan.journals:
            journals.setdefault(counts.run_id, []).append(counts)
            dirs.setdefault(counts.run_id, []).append(scan.state_dir)
    runs: list[RunReport] = []
    for run_id in sorted(dirs):
        report = RunReport(run_id=run_id, state_dirs=sorted(set(dirs[run_id])))
        sources = states.get(run_id, [])
        home = ""
        view_decides = False
        if sources:
            chosen = _pick_state(sources)
            home = chosen.state_dir
            report.state = "loaded" if chosen.state is not None else "refused"
            report.state_file = str(Path(chosen.state_dir) / chosen.name)
            report.state_copies = len(sources) - 1
            report.final = chosen.final
            view_decides = chosen.view_decides
            if chosen.state is not None:
                report.unblock_history = len(chosen.state.unblock_history)
        logs = [j for j in journals.get(run_id, []) if j.status != "absent"]
        if logs:
            # The run log beside the state that classified it, else the first.
            beside = [j for j in logs if j.state_dir == home]
            report.journal = (beside or logs)[0]
            report.journal_copies = len(logs) - 1
        if not sources:
            # No state: 'unknown' in every view, named in its run log's.
            where = report.journal.state_dir if report.journal else report.state_dirs[0]
            report.final = replace(NO_STATE, scheduler=views.get(where, None))
        # A run whose outcome depends on a view nothing establishes is undecided.
        report.eligibility = "undecidable" if view_decides else ELIGIBILITY[report.final.outcome]
        runs.append(report)
    unreadable: list[Unreadable] = [
        (path, "discovery", why) for path, why in (discovery.errors if discovery else {}).items()
    ]
    unreadable += [(s.state_dir, "scan", s.error) for s in scans if s.error]
    return Corpus(runs=runs, unidentified_states=unidentified, unreadable=sorted(unreadable))


def read_corpus(paths: Iterable[Path]) -> Corpus:
    discovery = discover(paths)
    return build_corpus([scan_state_dir(d) for d in discovery.state_dirs], discovery)


def summarize(corpus: Corpus) -> dict[str, object]:
    """The corpus summary: each unit counted once in its own denominator."""
    runs = corpus.runs
    eligibility = Counter(r.eligibility for r in runs)
    outcomes = Counter(r.final.outcome.value for r in runs)
    codes = Counter(f"{r.final.outcome.value}:{r.final.code.value}" for r in runs)
    by_state = Counter(r.state for r in runs)
    by_view = Counter(r.final.view for r in runs)
    launches: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    operator: Counter[str] = Counter()
    bookkeeping: Counter[str] = Counter()
    lines: Counter[str] = Counter()
    for r in runs:
        if r.journal is None:
            continue
        launches.update(r.journal.launches)
        failures.update(r.journal.failed_launches)
        operator.update(r.journal.operator_actions)
        bookkeeping.update(r.journal.bookkeeping)
        lines.update(
            torn=r.journal.torn_lines,
            foreign=r.journal.foreign_lines,
            malformed=r.journal.malformed_lines,
        )
    blocked_now = sum(1 for r in runs if r.final.outcome is StopOutcome.BLOCKED)
    unidentified = Counter(
        f"{s.final.outcome.value}:{s.final.code.value}" for s in corpus.unidentified_states
    )
    return {
        "schema": SCHEMA,
        "runs": {
            "total": len(runs),
            "by_state": {name: by_state.get(name, 0) for name in ("loaded", "refused", "absent")},
            "by_view": {name: by_view.get(name, 0) for name in VIEWS},
            "by_eligibility": {name: eligibility.get(name, 0) for name in ELIGIBILITIES},
            "by_outcome": dict(sorted(outcomes.items())),
            "by_outcome_and_code": dict(sorted(codes.items())),
            "incomplete_history": sum(1 for r in runs if r.incomplete),
            "with_copies": sum(1 for r in runs if r.state_copies or r.journal_copies),
        },
        # Never runs: each may be one of the runs above, or another.
        "unidentified_states": {
            "total": len(corpus.unidentified_states),
            "by_outcome_and_code": dict(sorted(unidentified.items())),
            "files": sorted(str(Path(s.state_dir) / s.name) for s in corpus.unidentified_states),
        },
        "launches": {
            "total": sum(launches.values()),
            "by_kind": dict(sorted(launches.items())),
            "failed": sum(failures.values()),
            "failed_by_kind_and_code": dict(sorted(failures.items())),
        },
        "operator_actions": {
            "total": sum(operator.values()),
            "by_kind": dict(sorted(operator.items())),
            "unblocks_in_state_histories": sum(r.unblock_history or 0 for r in runs),
        },
        # Never launches: the controller's own lines, recording no invocation.
        "bookkeeping": {
            "total": sum(bookkeeping.values()),
            "by_kind": dict(sorted(bookkeeping.items())),
        },
        # Every BLOCKED episode needed a human (the exit rule): those an
        # applied unblock ended, and those still BLOCKED.
        "blocked_episodes": {
            "ended_by_unblock": operator["unblock-applied"],
            "still_blocked": blocked_now,
        },
        "journal_lines": {name: lines.get(name, 0) for name in ("torn", "foreign", "malformed")},
        # A path that could not be read leaves the corpus incomplete: what it
        # holds is in no denominator above.
        "input_incomplete": bool(corpus.unreadable),
        "unreadable_directories": [
            {"path": path, "stage": stage, "error": error}
            for path, stage, error in corpus.unreadable
        ],
        "limitations": list(LIMITATIONS),
        "run_reports": [r.as_dict() for r in runs],
    }


def render(summary: dict) -> str:
    runs, launches = summary["runs"], summary["launches"]
    operator, unidentified = summary["operator_actions"], summary["unidentified_states"]
    bookkeeping = summary["bookkeeping"]
    lines = [
        f"runs: {runs['total']} (each run id counted once, by one state file)",
        f"  by state: {runs['by_state']}",
        f"  by view (whose routes): {runs['by_view']}",
        *(f"  {name}: {count}" for name, count in runs["by_eligibility"].items()),
        "  by outcome and code:",
        *(f"    {key}: {count}" for key, count in runs["by_outcome_and_code"].items()),
        f"  with an incomplete history: {runs['incomplete_history']}",
        f"  found more than once (copies counted once): {runs['with_copies']}",
        f"state files that name no run (counted apart, never as runs): {unidentified['total']}",
        *(f"  {key}: {count}" for key, count in unidentified["by_outcome_and_code"].items()),
        f"launches: {launches['total']} {launches['by_kind']}",
        f"  failed: {launches['failed']}",
        *(f"    {key}: {count}" for key, count in launches["failed_by_kind_and_code"].items()),
        f"operator actions (no launch): {operator['total']} {operator['by_kind']} "
        f"({operator['unblocks_in_state_histories']} unblocks in the states' histories)",
        f"bookkeeping lines (no launch): {bookkeeping['total']} {bookkeeping['by_kind']}",
        f"BLOCKED episodes (each needed a human): {summary['blocked_episodes']}",
        f"bad journal lines: {summary['journal_lines']}",
        f"paths that could not be read: {len(summary['unreadable_directories'])}"
        + (
            " (the corpus is incomplete: what they hold is counted nowhere)"
            if summary["input_incomplete"]
            else ""
        ),
        *(
            f"  {entry['path']} ({entry['stage']}): {entry['error']}"
            for entry in summary["unreadable_directories"]
        ),
        "limitations:",
        *(f"  - {text}" for text in summary["limitations"]),
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.stop_corpus", description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="state directories, or their parents")
    parser.add_argument("--json", action="store_true", help=f"print the {SCHEMA} document")
    args = parser.parse_args(argv)
    summary = summarize(read_corpus(args.paths))
    print(json.dumps(summary, indent=2, sort_keys=True) if args.json else render(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
