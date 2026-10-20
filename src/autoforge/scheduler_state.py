"""Scheduler claims, the per-EPIC scheduler state and restart reconciliation (ADR 0008; #181).

The parallel EPIC scheduler's durable, local bookkeeping. None of it is live
authority: an issue has a live owner exactly when some process holds its run
lock (L2, :class:`~autoforge.locking.IssueRunLock`). A claim is recovery
data that says which generation of which invocation was admitted, so that a
restarted scheduler can tell an interrupted *assignment* from a settled one.

* **``claim.json``** (:class:`ClaimFile`, ``autoforge.scheduler-claim/1``,
  D7), one per issue namespace: ``run_key``, ``issue_id``, ``epic`` (copied
  from the namespace's binding), ``next_generation``, ``current`` (the one
  open :class:`Claim`, or none) and ``history`` (at most
  :data:`MAX_HISTORY` :class:`ClosedClaim`, oldest dropped). A claim is
  written before a worker exists and closed only once the worker's outcome
  is durably observable.
* **One closing rule** (D7.2). ``superseded`` only in the atomic write that
  installs the replacing claim (:func:`open_claim` with ``supersedes``); a
  claim closed without a replacement is ``abandoned`` when no worker was
  recorded on it and ``interrupted`` when one was; ``settled`` when the
  state's settlement is not ``in_progress``; ``archived`` by ``epic
  archive``. The writers enforce it and the loader refuses a file that
  breaks it.
* **The writers.** The only writer is the L0-holding scheduler. Every
  operation that opens or closes a claim takes the held
  :class:`~autoforge.locking.IssueRunLock` of the issue as a capability
  argument, and every write compares ``(claim_id, generation)`` on disk with
  the caller's record first: a mismatch is :class:`~autoforge.errors.ClaimConflict`,
  never an overwrite, so a stale claimant cannot finalize a newer
  generation. Writes are atomic (fsynced temporary, replace) through
  ``safefs`` from the coordination root, every component ``O_NOFOLLOW``,
  and refused while this process holds L1 (D11.2).
* **``scheduler.json``** (:class:`SchedulerState`, ``autoforge.scheduler/1``,
  D6), one per EPIC under ``scheduler/epics/<n>/``: run-level facts only. It
  holds no claim, so the two files have nothing to disagree about. A corrupt
  one is the scheduler's global uncertainty; ``epic run --force``
  quarantines it (:func:`quarantine_scheduler_state`).
* **Restart classification** (D14.2). :func:`classify` is pure: given #178's
  strict lookup and the claim file, read under L2, a namespace is
  ``uncertain(reason)``, ``settled(settlement)``, ``resumable(reason)`` or
  ``fresh``, checked in that order. The scheduler sees a state only through
  :func:`~autoforge.transitions.issue_settlement`.
* **Reconciliation** (:func:`reconcile_epic`) runs under L0 before any
  admission: it takes each namespace's L2 non-blocking (or reports it
  ``observed_live``), classifies, closes an open claim of a settled
  namespace as ``settled`` and keeps every other claim open, and returns the
  local facts #179's ``evaluate`` consumes. :func:`finalize_worker` closes a
  reaped worker's claim by the state it left.

Nothing here spawns a process, reads or writes GitHub, writes or quarantines
a per-issue state, or parses ``CONTROL_RESULT``; timestamps and PIDs are
diagnostics only. ``detail`` texts are redacted and bounded.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import NoReturn, TypeVar

from .config import MAX_PARALLEL_BOUNDS, POLL_INTERVAL_BOUNDS, STOP_GRACE_BOUNDS
from .epic_graph import LocalFact
from .errors import (
    ClaimConflict,
    ConfigurationError,
    IssueBusy,
    LockError,
    NamespaceMismatch,
    NetworkFilesystemError,
    SchedulerStateError,
    StateError,
)
from .locking import IssueRunLock, assert_shared_git_lock_free
from .redaction import redact
from .run_namespace import (
    AUTOFORGE_DIRNAME,
    CLAIM_FILENAME,
    MAX_ID,
    MAX_OWNER_CHARS,
    MAX_REPOSITORY_NAME_CHARS,
    MAX_TIMESTAMP_CHARS,
    MAX_WORKTREE_PATH_CHARS,
    SCHEDULER_DIRNAME,
    EpicRef,
    IssueNamespace,
    NamespaceKind,
    NamespaceLookup,
    RejectedEntry,
    RunBinding,
    RunKey,
    StateStatus,
    check_binding,
    list_issue_namespaces,
    load_issue_namespace,
)
from .runlog import RUN_ID_RE
from .safefs import ReadLimitExceeded, SafeRoot, entry_kind
from .state import AutoForgeState, archive_state_file, quarantine_state_file, utcnow_iso
from .transitions import Settlement, WorkflowMode, issue_settlement
from .validation import parse_issue_url

CLAIM_SCHEMA = "autoforge.scheduler-claim/1"
SCHEDULER_SCHEMA = "autoforge.scheduler/1"
EPICS_DIRNAME = "epics"
SCHEDULER_FILENAME = "scheduler.json"

#: Closed claims kept in ``history``; the oldest is dropped past it (D7.1).
MAX_HISTORY = 16
#: A closed claim's ``detail``, after redaction (D7.1).
MAX_DETAIL_CHARS = 500
# A classification's or a report's explanation quotes a loader's message:
# redacted, then bounded. Never persisted.
MAX_REASON_CHARS = 1000
# A claim file holds at most 17 claims of a few kilobytes each, its worst
# case JSON escaping of a PATH_MAX log path included; a larger file is
# refused unread, and a writer never produces one.
MAX_CLAIM_BYTES = 1024 * 1024
# A scheduler state is under a kilobyte.
MAX_SCHEDULER_BYTES = 16 * 1024
# A worker's exit status as Popen reports it: 0-255, or minus a signal number.
MAX_EXIT_STATUS = 255
# A last snapshot's counts are bounded by the namespaces one EPIC can have.
MAX_COUNT = 10_000
# A refusal quotes at most this many unknown field names, each cut to 80 characters.
MAX_QUOTED_NAMES = 5
# A pid fits a 32-bit signed integer on every platform the scheduler runs on.
MAX_PID = 2**31 - 1

# ADR 0008 D18.3, D18.4: the bounds a scheduler's configuration is held to are
# config.py's (MAX_PARALLEL_BOUNDS, POLL_INTERVAL_BOUNDS, STOP_GRACE_BOUNDS), the
# ones the ``scheduler.*`` keys are validated against.

_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")

E = TypeVar("E", bound=Enum)


# -- field checks -----------------------------------------------------------------------


def _fail(what: str, message: str) -> NoReturn:
    raise SchedulerStateError(f"{what} {message}")


def _quoted_names(names: list[str]) -> str:
    shown = ", ".join(f"{name!r:.80}" for name in names[:MAX_QUOTED_NAMES])
    more = len(names) - MAX_QUOTED_NAMES
    return f"[{shown}]" if more <= 0 else f"[{shown}] and {more} more"


def _exact(raw: object, keys: Iterable[str], what: str) -> dict:
    if not isinstance(raw, dict):
        _fail(what, f"must be an object, got {type(raw).__name__}")
    wanted = tuple(keys)
    unknown = sorted(str(k) for k in raw if k not in wanted)
    if unknown:
        _fail(what, f"has unknown field(s) {_quoted_names(unknown)}")
    missing = [k for k in wanted if k not in raw]
    if missing:
        _fail(what, f"is missing field(s) {missing}")
    return raw


def _check_int(value: object, what: str, low: int, high: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(what, f"must be an integer, got {type(value).__name__}")
    if value < low or value > high:
        got = str(value) if len(str(abs(value))) <= 40 else f"{len(str(abs(value)))} digits"
        _fail(what, f"must be an integer from {low} to {high}, got {got}")
    return value


def _check_id(value: object, what: str) -> int:
    return _check_int(value, what, 1, MAX_ID)


def _check_bool(value: object, what: str) -> bool:
    if not isinstance(value, bool):
        _fail(what, f"must be a boolean, got {type(value).__name__}")
    return value


def _check_str(value: object, what: str, max_chars: int, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        _fail(what, f"must be a string, got {type(value).__name__}")
    if not value and not empty:
        _fail(what, "must not be empty")
    if len(value) > max_chars:
        _fail(what, f"is {len(value)} characters, over its bound of {max_chars}")
    return value


def _check_hex(value: object, what: str, pattern: re.Pattern[str]) -> str:
    text = _check_str(value, what, 64)
    if not pattern.fullmatch(text):
        digits = 32 if pattern is _HEX32 else 64
        _fail(what, f"must be {digits} lowercase hexadecimal characters, got {text!r:.80}")
    return text


def _check_timestamp(value: object, what: str) -> str:
    text = _check_str(value, what, MAX_TIMESTAMP_CHARS)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        _fail(what, f"is not an ISO 8601 time: {text!r}")
    offset = moment.utcoffset()
    if offset is None or offset.total_seconds() != 0 or "T" not in text:
        _fail(what, f"must be an ISO 8601 UTC time as utcnow_iso writes it, got {text!r}")
    return text


def _check_path(value: object, what: str) -> str:
    text = _check_str(value, what, MAX_WORKTREE_PATH_CHARS)
    if not text.startswith("/") or any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        _fail(what, "must be an absolute path without control characters")
    return text


def _check_name(value: object, what: str, max_chars: int) -> str:
    text = _check_str(value, what, max_chars)
    if not _NAME_RE.fullmatch(text) or text in (".", ".."):
        _fail(what, f"is not a GitHub owner or repository name: {text!r}")
    return text


def _check_enum(cls: type[E], value: object, what: str) -> E:
    if isinstance(value, cls):
        return value
    try:
        return cls(value)
    except ValueError:
        allowed = ", ".join(repr(member.value) for member in cls)
        _fail(what, f"must be one of {allowed}, got {value!r:.80}")


def _check_type(value: object, cls: type, what: str) -> None:
    if not isinstance(value, cls):
        _fail(what, f"must be a {cls.__name__}, got {type(value).__name__}")


def _bounded(text: str, limit: int = MAX_REASON_CHARS) -> str:
    text = redact(text)
    if len(text) <= limit:
        return text
    marker = " [... truncated]"
    return text[: limit - len(marker)] + marker


def bounded_detail(text: str) -> str:
    """``text`` as a closed claim may hold it: redacted, then at most :data:`MAX_DETAIL_CHARS`."""
    return _bounded(text, MAX_DETAIL_CHARS)


def new_invocation_id() -> str:
    """A fresh ``invocation_id``: 32 lowercase hex characters, random (D6.1)."""
    return secrets.token_hex(16)


def _epic_dict(epic: EpicRef) -> dict:
    return {"number": epic.number, "issue_id": epic.issue_id}


def _epic_of(raw: object, what: str) -> EpicRef:
    data = _exact(raw, ("number", "issue_id"), what)
    _check_id(data["number"], f"{what} number")
    _check_id(data["issue_id"], f"{what} issue_id")
    return EpicRef(data["number"], data["issue_id"])


# -- claim.json -------------------------------------------------------------------------


class ClaimKind(Enum):
    """What an admission launches: a fresh issue, or an ordinary engine resume (D14.4)."""

    START = "start"
    RESUME = "resume"


class ClaimClassification(Enum):
    """How a claim was closed (D7.1, D7.2)."""

    SETTLED = "settled"
    INTERRUPTED = "interrupted"
    SUPERSEDED = "superseded"
    ABANDONED = "abandoned"
    ARCHIVED = "archived"


@dataclass(frozen=True)
class Admission:
    """The snapshot an admission was decided on (#179's digests): diagnostics."""

    snapshot_digest: str
    node_digest: str
    observed_at: str

    def __post_init__(self) -> None:
        _check_hex(self.snapshot_digest, "admission snapshot_digest", _HEX64)
        _check_hex(self.node_digest, "admission node_digest", _HEX64)
        _check_timestamp(self.observed_at, "admission observed_at")

    def to_dict(self) -> dict:
        return {
            "snapshot_digest": self.snapshot_digest,
            "node_digest": self.node_digest,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict(cls, raw: object) -> Admission:
        data = _exact(raw, ("snapshot_digest", "node_digest", "observed_at"), "claim admission")
        return cls(data["snapshot_digest"], data["node_digest"], data["observed_at"])


@dataclass(frozen=True)
class StateRef:
    """The namespace's state as it was at admission (D7.1).

    ``present`` is whether ``state/state.json`` existed; ``run_id`` and
    ``settlement`` are its run id and settlement then, and both are null
    when it did not. An open claim whose ``present`` is true while the state
    is absent is ``uncertain`` (D14.2).
    """

    present: bool
    run_id: str | None = None
    settlement: Settlement | None = None

    def __post_init__(self) -> None:
        _check_bool(self.present, "claim state_ref present")
        if self.run_id is not None:
            text = _check_str(self.run_id, "claim state_ref run_id", 128)
            if not RUN_ID_RE.match(text):
                _fail("claim state_ref run_id", f"is not a run id: {text!r:.80}")
        if self.settlement is not None:
            _check_type(self.settlement, Settlement, "claim state_ref settlement")
        if not self.present and (self.run_id is not None or self.settlement is not None):
            _fail("claim state_ref", "names a run_id or a settlement while present is false")
        if self.present and self.settlement is None:
            _fail("claim state_ref", "has no settlement while present is true")

    @classmethod
    def absent(cls) -> StateRef:
        return cls(present=False)

    @classmethod
    def of(cls, state: AutoForgeState) -> StateRef:
        """The reference to a state that was loaded at admission."""
        return cls(True, state.run_id or None, issue_settlement(state))

    @classmethod
    def from_lookup(cls, lookup: NamespaceLookup) -> StateRef:
        """The reference to the state #178's lookup found; a corrupt one admits nothing."""
        if lookup.state.status is StateStatus.LOADED:
            assert lookup.state.state is not None
            return cls.of(lookup.state.state)
        if lookup.state.status is StateStatus.ABSENT:
            return cls.absent()
        raise SchedulerStateError(
            f"the state of {lookup.namespace.directory} cannot be read, so nothing is admitted "
            f"for it ({_bounded(lookup.state.reason)})"
        )

    def to_dict(self) -> dict:
        return {
            "present": self.present,
            "run_id": self.run_id,
            "settlement": None if self.settlement is None else self.settlement.value,
        }

    @classmethod
    def from_dict(cls, raw: object) -> StateRef:
        data = _exact(raw, ("present", "run_id", "settlement"), "claim state_ref")
        settlement = data["settlement"]
        return cls(
            present=data["present"],
            run_id=data["run_id"],
            settlement=None
            if settlement is None
            else _check_enum(Settlement, settlement, "claim state_ref settlement"),
        )


@dataclass(frozen=True)
class WorkerDiagnostics:
    """A spawned worker, as an operator recognises it (D7.3): never authority.

    ``process_start_time`` is the kernel's start time of ``pid``
    (``/proc/<pid>/stat`` field 22 on Linux), null when it could not be
    read; ``log`` the worker log's absolute path.
    """

    pid: int
    process_start_time: int | None
    spawned_at: str
    log: str

    def __post_init__(self) -> None:
        _check_int(self.pid, "claim worker pid", 1, MAX_PID)
        if self.process_start_time is not None:
            _check_int(self.process_start_time, "claim worker process_start_time", 0, MAX_ID)
        _check_timestamp(self.spawned_at, "claim worker spawned_at")
        _check_path(self.log, "claim worker log")

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "process_start_time": self.process_start_time,
            "spawned_at": self.spawned_at,
            "log": self.log,
        }

    @classmethod
    def from_dict(cls, raw: object) -> WorkerDiagnostics:
        data = _exact(raw, ("pid", "process_start_time", "spawned_at", "log"), "claim worker")
        return cls(data["pid"], data["process_start_time"], data["spawned_at"], data["log"])


_CLAIM_KEYS = (
    "claim_id",
    "generation",
    "invocation_id",
    "kind",
    "admission",
    "state_ref",
    "created_at",
    "worker",
)


@dataclass(frozen=True)
class Claim:
    """One generation of one invocation owning an issue (D7.1); recovery data, never liveness."""

    claim_id: str
    generation: int
    invocation_id: str
    kind: ClaimKind
    admission: Admission
    state_ref: StateRef
    created_at: str
    worker: WorkerDiagnostics | None = None

    def __post_init__(self) -> None:
        _check_hex(self.claim_id, "claim claim_id", _HEX32)
        _check_id(self.generation, "claim generation")
        _check_hex(self.invocation_id, "claim invocation_id", _HEX32)
        _check_type(self.kind, ClaimKind, "claim kind")
        _check_type(self.admission, Admission, "claim admission")
        _check_type(self.state_ref, StateRef, "claim state_ref")
        _check_timestamp(self.created_at, "claim created_at")
        if self.worker is not None:
            _check_type(self.worker, WorkerDiagnostics, "claim worker")

    @property
    def identity(self) -> tuple[str, int]:
        """``(claim_id, generation)``: what every write compares on disk (D7.2)."""
        return (self.claim_id, self.generation)

    def to_dict(self) -> dict:
        return {
            "claim_id": self.claim_id,
            "generation": self.generation,
            "invocation_id": self.invocation_id,
            "kind": self.kind.value,
            "admission": self.admission.to_dict(),
            "state_ref": self.state_ref.to_dict(),
            "created_at": self.created_at,
            "worker": None if self.worker is None else self.worker.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: object, *, what: str = "claim") -> Claim:
        data = _exact(raw, _CLAIM_KEYS, what)
        return cls._of(data)

    @classmethod
    def _of(cls, data: dict) -> Claim:
        worker = data["worker"]
        return cls(
            claim_id=data["claim_id"],
            generation=data["generation"],
            invocation_id=data["invocation_id"],
            kind=_check_enum(ClaimKind, data["kind"], "claim kind"),
            admission=Admission.from_dict(data["admission"]),
            state_ref=StateRef.from_dict(data["state_ref"]),
            created_at=data["created_at"],
            worker=None if worker is None else WorkerDiagnostics.from_dict(worker),
        )


_CLOSED_KEYS = ("at", "classification", "settlement", "exit_status", "detail")


@dataclass(frozen=True)
class ClosedClaim:
    """A claim moved to ``history``: the claim plus how and when it was closed (D7.1).

    Constructing one enforces the closing rule (D7.2): ``abandoned`` only
    for a claim without a recorded worker, ``interrupted`` only for one
    with, and ``settled`` only with a settlement other than ``in_progress``.
    ``exit_status`` and ``detail`` are diagnostics; ``detail`` is redacted
    and at most :data:`MAX_DETAIL_CHARS`.
    """

    claim: Claim
    classification: ClaimClassification
    at: str
    settlement: Settlement | None = None
    exit_status: int | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        _check_type(self.claim, Claim, "closed claim")
        _check_type(self.classification, ClaimClassification, "closed claim classification")
        _check_timestamp(self.at, "closed claim at")
        if self.settlement is not None:
            _check_type(self.settlement, Settlement, "closed claim settlement")
        if self.exit_status is not None:
            _check_int(
                self.exit_status, "closed claim exit_status", -MAX_EXIT_STATUS, MAX_EXIT_STATUS
            )
        _check_str(self.detail, "closed claim detail", MAX_DETAIL_CHARS, empty=True)
        problem = closing_rule_problem(
            self.classification, worker_recorded=self.claim.worker is not None
        )
        if problem and self.classification is not ClaimClassification.SUPERSEDED:
            _fail(f"claim generation {self.claim.generation}", problem)
        if self.classification is ClaimClassification.SETTLED and (
            self.settlement is None or self.settlement is Settlement.IN_PROGRESS
        ):
            _fail(
                f"claim generation {self.claim.generation}",
                "is closed settled without a settled settlement; settled means the state's "
                "settlement is not in_progress",
            )

    def to_dict(self) -> dict:
        return {
            **self.claim.to_dict(),
            "closed": {
                "at": self.at,
                "classification": self.classification.value,
                "settlement": None if self.settlement is None else self.settlement.value,
                "exit_status": self.exit_status,
                "detail": self.detail,
            },
        }

    @classmethod
    def from_dict(cls, raw: object) -> ClosedClaim:
        data = _exact(raw, (*_CLAIM_KEYS, "closed"), "history claim")
        claim = Claim._of(data)
        closed = _exact(data["closed"], _CLOSED_KEYS, "history claim closed")
        settlement = closed["settlement"]
        return cls(
            claim=claim,
            classification=_check_enum(
                ClaimClassification, closed["classification"], "closed claim classification"
            ),
            at=closed["at"],
            settlement=None
            if settlement is None
            else _check_enum(Settlement, settlement, "closed claim settlement"),
            exit_status=closed["exit_status"],
            detail=closed["detail"],
        )


def closing_rule_problem(classification: ClaimClassification, *, worker_recorded: bool) -> str:
    """Why D7.2's closing rule refuses closing a claim as ``classification``; "" when it allows.

    ``superseded`` is refused here because only :func:`open_claim` closes a
    claim that way, in the write that installs its replacement.
    """
    if classification is ClaimClassification.SUPERSEDED:
        return (
            "cannot be closed superseded except by the write that installs its replacement "
            "(open_claim with supersedes)"
        )
    if classification is ClaimClassification.ABANDONED and worker_recorded:
        return "has a recorded worker, so it is closed interrupted, never abandoned"
    if classification is ClaimClassification.INTERRUPTED and not worker_recorded:
        return "has no recorded worker, so it is closed abandoned, never interrupted"
    return ""


_CLAIM_FILE_KEYS = (
    "schema",
    "run_key",
    "issue_id",
    "epic",
    "next_generation",
    "current",
    "history",
    "updated_at",
)


@dataclass(frozen=True)
class ClaimFile:
    """``claim.json``, schema ``autoforge.scheduler-claim/1`` (ADR 0008 D7.1).

    Validated on construction: ``next_generation`` is above every
    generation in the file, ``current`` (when set) is the last one
    allocated, ``history`` holds at most :data:`MAX_HISTORY` claims in
    ascending generation, and no claim id repeats.
    """

    run_key: RunKey
    issue_id: int
    epic: EpicRef
    next_generation: int
    current: Claim | None
    history: tuple[ClosedClaim, ...]
    updated_at: str

    def __post_init__(self) -> None:
        _check_type(self.run_key, RunKey, "claim file run_key")
        _check_id(self.issue_id, "claim file issue_id")
        _check_type(self.epic, EpicRef, "claim file epic")
        _check_id(self.next_generation, "claim file next_generation")
        if self.current is not None:
            _check_type(self.current, Claim, "claim file current")
        if not isinstance(self.history, tuple):
            _fail("claim file history", f"must be a tuple, got {type(self.history).__name__}")
        if len(self.history) > MAX_HISTORY:
            _fail("claim file history", f"holds {len(self.history)} claims, over {MAX_HISTORY}")
        for closed in self.history:
            _check_type(closed, ClosedClaim, "claim file history entry")
        _check_timestamp(self.updated_at, "claim file updated_at")
        generations = [closed.claim.generation for closed in self.history]
        if self.current is not None:
            generations.append(self.current.generation)
            if self.current.generation != self.next_generation - 1:
                _fail(
                    "claim file current",
                    f"has generation {self.current.generation}, not the last one allocated "
                    f"({self.next_generation - 1})",
                )
        if any(b <= a for a, b in zip(generations, generations[1:], strict=False)):
            _fail("claim file", f"holds generations {generations} out of ascending order")
        if generations and generations[-1] >= self.next_generation:
            _fail(
                "claim file next_generation",
                f"is {self.next_generation}, not above generation {generations[-1]}",
            )
        ids = [closed.claim.claim_id for closed in self.history]
        if self.current is not None:
            ids.append(self.current.claim_id)
        if len(set(ids)) != len(ids):
            _fail("claim file", "holds one claim_id twice")

    def to_dict(self) -> dict:
        return {
            "schema": CLAIM_SCHEMA,
            "run_key": {
                "repository_id": self.run_key.repository_id,
                "issue_number": self.run_key.issue_number,
            },
            "issue_id": self.issue_id,
            "epic": _epic_dict(self.epic),
            "next_generation": self.next_generation,
            "current": None if self.current is None else self.current.to_dict(),
            "history": [closed.to_dict() for closed in self.history],
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: object, *, where: str = CLAIM_FILENAME) -> ClaimFile:
        """Strictly parse a claim file: an unknown version or field is refused, never skipped."""
        try:
            if not isinstance(raw, dict):
                _fail("claim file", f"must be a JSON object, got {type(raw).__name__}")
            schema = raw.get("schema")
            if schema != CLAIM_SCHEMA:
                _fail(
                    "claim file",
                    f"has schema {schema!r:.80}; this controller reads only {CLAIM_SCHEMA}",
                )
            data = _exact(raw, _CLAIM_FILE_KEYS, "claim file")
            key = _exact(data["run_key"], ("repository_id", "issue_number"), "claim file run_key")
            _check_id(key["repository_id"], "claim file run_key repository_id")
            _check_id(key["issue_number"], "claim file run_key issue_number")
            history = data["history"]
            if not isinstance(history, list):
                _fail("claim file history", f"must be a list, got {type(history).__name__}")
            if len(history) > MAX_HISTORY:
                _fail("claim file history", f"holds {len(history)} claims, over {MAX_HISTORY}")
            current = data["current"]
            return cls(
                run_key=RunKey(key["repository_id"], key["issue_number"]),
                issue_id=data["issue_id"],
                epic=_epic_of(data["epic"], "claim file epic"),
                next_generation=data["next_generation"],
                current=None if current is None else Claim.from_dict(current, what="claim current"),
                history=tuple(ClosedClaim.from_dict(entry) for entry in history),
                updated_at=data["updated_at"],
            )
        except SchedulerStateError as exc:
            raise SchedulerStateError(f"invalid {CLAIM_SCHEMA} document {where}: {exc}") from exc


def _appended(history: tuple[ClosedClaim, ...], closed: ClosedClaim) -> tuple[ClosedClaim, ...]:
    return (*history, closed)[-MAX_HISTORY:]


# -- reading and writing a namespace's claim file -----------------------------------------


def _open_anchor(coordination_root: Path) -> SafeRoot:
    """A root on the coordination root; it is never created (it is the git common dir)."""
    try:
        return SafeRoot.open(coordination_root)
    except FileNotFoundError:
        raise SchedulerStateError(f"coordination root {coordination_root} does not exist") from None


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    data: dict = {}
    for key, value in pairs:
        if key in data:
            raise SchedulerStateError(f"has the field {key!r:.80} more than once")
        data[key] = value
    return data


def _no_constant(name: str) -> NoReturn:
    raise SchedulerStateError(f"holds the non-JSON number {name}")


def _read_document(root: SafeRoot, name: str, *, limit: int, where: str) -> object | None:
    """The JSON document at ``name`` under ``root``; ``None`` only when there is no file.

    Strict like the binding's read: a link or another non-regular entry is
    refused without being followed, a file over ``limit`` unread, and a
    duplicated key, a non-JSON number and the decoder's own limits are
    refusals (:class:`SchedulerStateError`). So is a file holding JSON
    ``null``: it is a corrupt file, kept until explicit recovery, never one
    that is not there.
    """
    st = root.lstat(name)
    if st is None:
        return None
    kind = entry_kind(st.st_mode)
    if kind is not None:
        raise SchedulerStateError(f"{where} is a {kind}, not a regular file; it is never followed")
    try:
        raw = root.read_bytes(name, limit=limit)
    except ReadLimitExceeded as exc:
        raise SchedulerStateError(
            f"{where} is larger than {limit} bytes, which no scheduler file can be"
        ) from exc
    if raw is None:
        return None  # removed between the lstat and the open
    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_no_constant
        )
    except UnicodeDecodeError as exc:
        raise SchedulerStateError(f"{where} is not valid UTF-8 ({exc})") from exc
    except json.JSONDecodeError as exc:
        raise SchedulerStateError(f"{where} is not valid JSON ({exc})") from exc
    except SchedulerStateError as exc:
        raise SchedulerStateError(f"{where} {exc}") from exc
    except (ValueError, RecursionError) as exc:
        raise SchedulerStateError(
            f"{where} exceeds the JSON decoder's integer or nesting limit "
            f"({type(exc).__name__}); no scheduler file comes near it"
        ) from exc
    if document is None:
        raise SchedulerStateError(f"{where} holds JSON null, not a JSON object")
    return document


def _payload(document: dict, *, limit: int, where: str) -> bytes:
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > limit:  # pragma: no cover - every field is bounded below it
        raise SchedulerStateError(f"{where} would be {len(payload)} bytes, over {limit}")
    return payload


def load_claim_file(namespace: IssueNamespace) -> ClaimFile | None:
    """The namespace's ``claim.json``, strictly; ``None`` when there is none. Read-only.

    A corrupt file (one holding JSON ``null`` included), one of an unknown
    schema or one that breaks a bound or the closing rule raises
    :class:`SchedulerStateError`; a link at any directory of the path raises
    :class:`~autoforge.safefs.UnsafePathError`. Whether the file names this
    namespace's run is the caller's check (:func:`classify`), as the
    binding's is.
    """
    with _open_anchor(namespace.coordination_root) as anchor:
        try:
            issue_root = anchor.subroot("/".join(namespace.relative))
        except FileNotFoundError:
            return None
    where = str(namespace.claim_file)
    with issue_root:
        raw = _read_document(issue_root, CLAIM_FILENAME, limit=MAX_CLAIM_BYTES, where=where)
    if raw is None:
        return None
    return ClaimFile.from_dict(raw, where=where)


def _write_claim_file(namespace: IssueNamespace, claim_file: ClaimFile) -> None:
    """Replace ``claim.json`` atomically: fsynced temporary, replace, fsynced directory."""
    where = str(namespace.claim_file)
    assert_shared_git_lock_free(f"write {where}")
    payload = _payload(claim_file.to_dict(), limit=MAX_CLAIM_BYTES, where=where)
    with _open_anchor(namespace.coordination_root) as anchor:
        anchor.write_bytes("/".join((*namespace.relative, CLAIM_FILENAME)), payload)


def _require_lock(namespace: IssueNamespace, lock: IssueRunLock, action: str) -> None:
    if not isinstance(lock, IssueRunLock) or not lock.held or lock.namespace != namespace:
        raise SchedulerStateError(
            f"cannot {action} for issue {namespace.issue_number}: this process does not hold "
            "its run lock (L2), which every claim write but the worker's diagnostics holds"
        )


def _claim_identity_problem(
    claim_file: ClaimFile, binding: RunBinding
) -> tuple[UncertainReason, str] | None:
    if (claim_file.run_key, claim_file.issue_id) != (binding.run_key, binding.issue_id):
        return (
            UncertainReason.CLAIM_MISMATCH,
            f"the claim file names run {claim_file.run_key} (issue id {claim_file.issue_id}), "
            f"the binding run {binding.run_key} (issue id {binding.issue_id})",
        )
    if claim_file.epic != binding.epic:
        return (
            UncertainReason.CLAIM_EPIC_MISMATCH,
            f"the claim file names EPIC #{claim_file.epic.number} (id {claim_file.epic.issue_id}), "
            f"the binding EPIC #{binding.epic.number} (id {binding.epic.issue_id})",
        )
    return None


def _load_for_write(namespace: IssueNamespace) -> ClaimFile:
    """The claim file a writer updates: the existing one, or a new one from the binding.

    A claim is only ever written into a bound namespace (the binding comes
    first, D2.2 step 5), and only over a file that names the binding's run:
    anything else is refused (:class:`SchedulerStateError`), never rewritten
    to agree.
    """
    lookup = load_issue_namespace(namespace.coordination_root, namespace.issue_number)
    binding = lookup.binding
    if binding is None:
        raise SchedulerStateError(
            f"the namespace of issue {namespace.issue_number} has no binding "
            f"({namespace.binding_file}); a claim is written only into a bound namespace"
        )
    claim_file = load_claim_file(namespace)
    if claim_file is None:
        return ClaimFile(
            run_key=binding.run_key,
            issue_id=binding.issue_id,
            epic=binding.epic,
            next_generation=1,
            current=None,
            history=(),
            updated_at=utcnow_iso(),
        )
    problem = _claim_identity_problem(claim_file, binding)
    if problem is not None:
        raise SchedulerStateError(f"{namespace.claim_file} is not this namespace's: {problem[1]}")
    return claim_file


def _matching_current(namespace: IssueNamespace, claim_file: ClaimFile, claim: Claim) -> Claim:
    current = claim_file.current
    if current is None or current.identity != claim.identity:
        on_disk = "no open claim" if current is None else f"generation {current.generation}"
        raise ClaimConflict(
            f"{namespace.claim_file} holds {on_disk}, not the caller's generation "
            f"{claim.generation} (claim {claim.claim_id}); a stale claimant never overwrites or "
            "finalizes a newer generation, and the file is left as it is"
        )
    return current


def _require_resumable(
    namespace: IssueNamespace, refusal: str, expected: ExpectedIdentity | None
) -> Classification:
    """The namespace's class, read under the caller's L2; refused unless ``resumable`` (D7.2).

    Only a ``resumable`` claim is replaced (:func:`open_claim`) or closed
    unready (:func:`close_unready`): a settled or uncertain one is refused
    (:class:`~autoforge.errors.SchedulerStateError`) and the file is left as
    it is. ``expected`` is :func:`classify`'s.
    """
    _check_optional_expected(expected)
    read = read_namespace(namespace)
    if read.absent:
        raise ClaimConflict(f"{namespace.claim_file} does not exist; there is no open claim")
    classification = classify(read.lookup, read.claim, expected=expected)
    if classification.kind is not ClassificationKind.RESUMABLE:
        raise SchedulerStateError(
            f"{refusal}; the namespace of issue {namespace.issue_number} is {classification}"
        )
    return classification


def _close(
    namespace: IssueNamespace,
    claim_file: ClaimFile,
    current: Claim,
    classification: ClaimClassification,
    *,
    settlement: Settlement | None,
    exit_status: int | None,
    detail: str,
) -> ClosedClaim:
    closed = ClosedClaim(
        current,
        classification,
        at=utcnow_iso(),
        settlement=settlement,
        exit_status=exit_status,
        detail=bounded_detail(detail),
    )
    _write_claim_file(
        namespace,
        replace(
            claim_file,
            current=None,
            history=_appended(claim_file.history, closed),
            updated_at=closed.at,
        ),
    )
    return closed


# -- claim operations (ADR 0008 D7.2) ---------------------------------------------------


def open_claim(
    namespace: IssueNamespace,
    lock: IssueRunLock,
    *,
    kind: ClaimKind,
    invocation_id: str,
    admission: Admission,
    state_ref: StateRef,
    supersedes: Claim | None = None,
    expected: ExpectedIdentity | None = None,
) -> Claim:
    """Write a new open claim, durably, before the worker exists; return it (D2.2 step 5).

    With ``supersedes=None`` it refuses (:class:`~autoforge.errors.ClaimConflict`)
    while ``current`` is set: at most one claim is open per issue. With
    ``supersedes`` set to the open claim, it compares it with ``current``,
    classifies the namespace under the held L2 and refuses
    (:class:`~autoforge.errors.SchedulerStateError`, nothing written) unless
    it is ``resumable`` (D7.2, D14.2): a settled or uncertain claim is never
    replaced. Then, in the same atomic write, it closes it as ``superseded``
    and installs the new claim: the only way a claim is ever closed so.
    ``expected`` is the classification's (:func:`classify`): the admission's
    repository and issue ids, and the live ``owner/name`` that lets a state
    written after a repository rename be recognised. The generation is
    ``next_generation``, which is then incremented. The file is written and
    fsynced before this returns.
    """
    _require_lock(namespace, lock, "open a claim")
    _check_type(kind, ClaimKind, "claim kind")
    claim_file = _load_for_write(namespace)
    current = claim_file.current
    if supersedes is None:
        if current is not None:
            raise ClaimConflict(
                f"{namespace.claim_file} already holds the open claim of generation "
                f"{current.generation}; only the write that supersedes it may replace it"
            )
    else:
        current = _matching_current(namespace, claim_file, supersedes)
        _require_resumable(
            namespace,
            f"refusing to supersede generation {current.generation}: only a resumable claim "
            "is replaced",
            expected,
        )
    now = utcnow_iso()
    claim = Claim(
        claim_id=secrets.token_hex(16),
        generation=claim_file.next_generation,
        invocation_id=invocation_id,
        kind=kind,
        admission=admission,
        state_ref=state_ref,
        created_at=now,
    )
    history = claim_file.history
    if current is not None:
        history = _appended(
            history,
            ClosedClaim(
                current,
                ClaimClassification.SUPERSEDED,
                at=now,
                detail=bounded_detail(
                    f"superseded by generation {claim.generation} ({kind.value} admission of "
                    f"invocation {invocation_id})"
                ),
            ),
        )
    _write_claim_file(
        namespace,
        replace(
            claim_file,
            next_generation=claim_file.next_generation + 1,
            current=claim,
            history=history,
            updated_at=now,
        ),
    )
    return claim


def record_worker(namespace: IssueNamespace, claim: Claim, diagnostics: WorkerDiagnostics) -> Claim:
    """Record the spawned worker's diagnostics on its open claim; return the updated claim.

    Compare-and-write on ``(claim_id, generation)``
    (:class:`~autoforge.errors.ClaimConflict` on a mismatch, or when a
    worker is already recorded: one claim, one worker). The diagnostics are
    never authority (D7.3). It takes no lock: the worker holds L2 by now.
    """
    _check_type(diagnostics, WorkerDiagnostics, "claim worker")
    claim_file = _load_for_write(namespace)
    current = _matching_current(namespace, claim_file, claim)
    if current.worker is not None:
        raise ClaimConflict(
            f"{namespace.claim_file}: generation {current.generation} already records worker "
            f"pid {current.worker.pid}; a claim has one worker"
        )
    updated = replace(current, worker=diagnostics)
    _write_claim_file(namespace, replace(claim_file, current=updated, updated_at=utcnow_iso()))
    return updated


def close_claim(
    namespace: IssueNamespace,
    lock: IssueRunLock,
    claim: Claim,
    classification: ClaimClassification,
    *,
    settlement: Settlement | None = None,
    exit_status: int | None = None,
    detail: str = "",
) -> ClosedClaim:
    """Close the open claim ``claim`` as ``classification`` and move it to ``history``.

    Compare-and-write (:class:`~autoforge.errors.ClaimConflict` when
    ``claim`` is not the open claim on disk). The closing rule decides on the
    claim as it is on disk: ``superseded`` is refused (only
    :func:`open_claim` closes a claim so), ``abandoned`` for a claim with a
    recorded worker and ``interrupted`` for one without are refused
    (:class:`~autoforge.errors.SchedulerStateError`), and nothing is written.
    ``detail`` is redacted and bounded.
    """
    _require_lock(namespace, lock, f"close a claim as {classification.value}")
    _check_type(classification, ClaimClassification, "claim classification")
    claim_file = _load_for_write(namespace)
    current = _matching_current(namespace, claim_file, claim)
    problem = closing_rule_problem(classification, worker_recorded=current.worker is not None)
    if problem:
        raise SchedulerStateError(
            f"refusing to close generation {current.generation} of issue "
            f"{namespace.issue_number} as {classification.value}: it {problem}"
        )
    return _close(
        namespace,
        claim_file,
        current,
        classification,
        settlement=settlement,
        exit_status=exit_status,
        detail=detail,
    )


def close_unready(
    namespace: IssueNamespace,
    lock: IssueRunLock,
    claim: Claim,
    *,
    detail: str = "",
    expected: ExpectedIdentity | None = None,
) -> ClosedClaim:
    """Close an open ``resumable`` claim whose issue a snapshot no longer shows as ready.

    ADR 0008 D14.2: a snapshot gave the issue a verdict other than ``ready``,
    or no longer lists it, so the earlier invocation's claim cannot be
    superseded by an admission and must not keep ``epic run`` refused. It
    is closed ``abandoned`` when no worker was recorded and ``interrupted``
    when one was. The namespace is classified again first, with
    ``expected`` (:func:`classify`): a claim that is not ``resumable``
    (settled, uncertain) is refused
    (:class:`~autoforge.errors.SchedulerStateError`) and left open.
    """
    _require_lock(namespace, lock, "close an unready claim")
    classification = _require_resumable(
        namespace, "close_unready closes only a resumable claim", expected
    )
    on_disk = classification.open_claim
    if on_disk is None or on_disk.identity != claim.identity:
        raise ClaimConflict(
            f"{namespace.claim_file} does not hold the caller's open claim of generation "
            f"{claim.generation}; the file is left as it is"
        )
    closing = (
        ClaimClassification.INTERRUPTED
        if on_disk.worker is not None
        else ClaimClassification.ABANDONED
    )
    return close_claim(
        namespace,
        lock,
        on_disk,
        closing,
        settlement=classification.settlement,
        detail=detail or "the issue is no longer ready for admission",
    )


class ArchiveAction(Enum):
    NOTHING = "nothing"  # no claim file, or no open claim: nothing changed
    CLOSED = "closed"  # the open claim was closed as archived
    MOVED_ASIDE = "moved-aside"  # a claim file the writers refuse was moved aside


@dataclass(frozen=True)
class ClaimArchive:
    """What :func:`archive_claim` did."""

    action: ArchiveAction
    closed: ClosedClaim | None = None
    moved_to: Path | None = None
    reason: str = ""


def archive_claim(namespace: IssueNamespace, lock: IssueRunLock) -> ClaimArchive:
    """``epic archive``'s claim step (ADR 0008 D17.4): run before the state is moved aside.

    An open claim is closed as ``archived``, whatever the state holds. A
    claim file the writers refuse to update (corrupt, of an unknown schema,
    naming another run than the binding, or in a namespace whose binding
    cannot be read) is moved aside as ``claim.json.archived-<UTC timestamp>``
    with the link-then-unlink primitive of the state's archive, so nothing
    is deleted. With no open claim and no such file it changes nothing, so a
    re-run after a crash completes the archive.
    """
    _require_lock(namespace, lock, "archive a claim")
    with _open_anchor(namespace.coordination_root) as anchor:
        try:
            issue_root = anchor.subroot("/".join(namespace.relative))
        except FileNotFoundError:
            return ClaimArchive(ArchiveAction.NOTHING)
    with issue_root:
        if issue_root.lstat(CLAIM_FILENAME) is None:
            return ClaimArchive(ArchiveAction.NOTHING)
        try:
            claim_file = _load_for_write(namespace)
        except StateError as exc:
            assert_shared_git_lock_free(f"move {namespace.claim_file} aside")
            moved = archive_state_file(namespace.claim_file, root=issue_root, what="claim file")
            return ClaimArchive(
                ArchiveAction.MOVED_ASIDE, moved_to=moved, reason=_bounded(str(exc))
            )
    if claim_file.current is None:
        return ClaimArchive(ArchiveAction.NOTHING)
    closed = _close(
        namespace,
        claim_file,
        claim_file.current,
        ClaimClassification.ARCHIVED,
        settlement=None,
        exit_status=None,
        detail="archived by 'epic archive'",
    )
    return ClaimArchive(ArchiveAction.CLOSED, closed=closed)


# -- restart classification (ADR 0008 D14.2) --------------------------------------------


class ClassificationKind(Enum):
    UNCERTAIN = "uncertain"
    SETTLED = "settled"
    RESUMABLE = "resumable"
    FRESH = "fresh"


class UncertainReason(Enum):
    """Why a namespace is ``uncertain``: a closed vocabulary (D14.2)."""

    #: The binding, or a directory above it, cannot be read.
    BINDING_UNREADABLE = "binding-unreadable"
    #: The binding names another issue or issue id than expected.
    BINDING_MISMATCH = "binding-mismatch"
    #: The binding names another repository: the scheduler's global uncertainty (D14.3).
    REPOSITORY_MISMATCH = "repository-mismatch"
    #: A claim file or a state exists, and ``binding.json`` does not.
    UNBOUND = "unbound"
    #: The state is corrupt or of an unknown protocol.
    STATE_CORRUPT = "state-corrupt"
    #: The claim file is corrupt or of an unknown schema.
    CLAIM_CORRUPT = "claim-corrupt"
    #: The claim file names another run (RunKey or issue id) than the binding.
    CLAIM_MISMATCH = "claim-mismatch"
    #: The claim file names another EPIC than the binding.
    CLAIM_EPIC_MISMATCH = "claim-epic-mismatch"
    #: The state names another repository, issue or EPIC than the binding, or is not a
    #: REMOTE run.
    STATE_MISMATCH = "state-mismatch"
    #: An open claim's ``state_ref.present`` is true, and the state is absent.
    STATE_MISSING = "state-missing"
    #: The caller's ``(claim_id, generation)`` is not the open claim on disk.
    CLAIM_CONFLICT = "claim-conflict"
    #: The run lock cannot be taken for a reason other than a live owner.
    RUN_LOCK_UNUSABLE = "run-lock-unusable"


class ResumableReason(Enum):
    #: The state loads and is ``in_progress``, whether or not a claim is open.
    IN_PROGRESS = "in-progress"
    #: A claim is open, no state exists, and none did at admission (a crash before
    #: the worker's first save).
    NOT_STARTED = "not-started"


@dataclass(frozen=True)
class Classification:
    """One namespace's restart class: total and mutually exclusive (D14.2).

    ``uncertain`` is set exactly for ``UNCERTAIN``, ``resumable`` exactly for
    ``RESUMABLE``. ``settlement`` is the state's settlement whenever a state
    was loaded. ``open_claim`` is the open claim on disk, when one could be
    read. ``detail`` explains the class, redacted and bounded.
    """

    kind: ClassificationKind
    uncertain: UncertainReason | None = None
    settlement: Settlement | None = None
    resumable: ResumableReason | None = None
    open_claim: Claim | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if (self.kind is ClassificationKind.UNCERTAIN) != (self.uncertain is not None):
            raise SchedulerStateError(
                f"classification {self.kind.value} with reason {self.uncertain}"
            )
        if (self.kind is ClassificationKind.RESUMABLE) != (self.resumable is not None):
            raise SchedulerStateError(
                f"classification {self.kind.value} with reason {self.resumable}"
            )
        if self.kind is ClassificationKind.SETTLED and (
            self.settlement is None or self.settlement is Settlement.IN_PROGRESS
        ):
            raise SchedulerStateError(f"classification settled with settlement {self.settlement}")

    def __str__(self) -> str:
        if self.uncertain is not None:
            return f"uncertain({self.uncertain.value})"
        if self.resumable is not None:
            return f"resumable({self.resumable.value})"
        if self.kind is ClassificationKind.SETTLED:
            assert self.settlement is not None
            return f"settled({self.settlement.value})"
        return self.kind.value


def _uncertain(reason: UncertainReason, detail: str, claim: Claim | None = None) -> Classification:
    return Classification(
        ClassificationKind.UNCERTAIN, uncertain=reason, open_claim=claim, detail=_bounded(detail)
    )


class ClaimFileStatus(Enum):
    ABSENT = "absent"
    LOADED = "loaded"
    CORRUPT = "corrupt"


@dataclass(frozen=True)
class ClaimFileRead:
    """The namespace's ``claim.json`` read strictly, read-only, without raising on corruption.

    ``claim_file`` is set when :attr:`status` is ``LOADED``; ``reason``
    (redacted and bounded) when it is ``CORRUPT``.
    """

    status: ClaimFileStatus
    claim_file: ClaimFile | None = None
    reason: str = ""

    @property
    def open_claim(self) -> Claim | None:
        return None if self.claim_file is None else self.claim_file.current


def read_claim_file(namespace: IssueNamespace) -> ClaimFileRead:
    """:func:`load_claim_file`, with a corrupt or unreachable file reported, not raised."""
    try:
        claim_file = load_claim_file(namespace)
    except (StateError, OSError) as exc:
        return ClaimFileRead(ClaimFileStatus.CORRUPT, reason=_bounded(str(exc)))
    if claim_file is None:
        return ClaimFileRead(ClaimFileStatus.ABSENT)
    return ClaimFileRead(ClaimFileStatus.LOADED, claim_file)


@dataclass(frozen=True)
class NamespaceUnreadable:
    """#178's lookup refused the namespace: its binding or a directory above it.

    ``field`` is the identity a :class:`~autoforge.errors.NamespaceMismatch`
    named (a binding of another issue than its directory), else "".
    """

    namespace: IssueNamespace
    reason: str
    field: str = ""


@dataclass(frozen=True)
class NamespaceRead:
    """One namespace read for classification: #178's lookup and the claim file."""

    namespace: IssueNamespace
    lookup: NamespaceLookup | NamespaceUnreadable
    claim: ClaimFileRead

    @property
    def absent(self) -> bool:
        """No binding, no claim file and no state: no namespace, which needs no classification."""
        return (
            isinstance(self.lookup, NamespaceLookup)
            and self.lookup.kind is NamespaceKind.ABSENT
            and self.claim.status is ClaimFileStatus.ABSENT
        )


def read_namespace(namespace: IssueNamespace) -> NamespaceRead:
    """Read a namespace's binding, state and claim file, strictly and read-only (D14.2 step 3).

    The caller holds the issue's L2, so no writer moves anything between
    the reads. Nothing raises for a corrupt file: each is reported for
    :func:`classify`.
    """
    lookup: NamespaceLookup | NamespaceUnreadable
    try:
        lookup = load_issue_namespace(namespace.coordination_root, namespace.issue_number)
    except NamespaceMismatch as exc:
        lookup = NamespaceUnreadable(namespace, _bounded(str(exc)), exc.field)
    except (StateError, OSError) as exc:
        lookup = NamespaceUnreadable(namespace, _bounded(str(exc)))
    return NamespaceRead(namespace, lookup, read_claim_file(namespace))


@dataclass(frozen=True)
class ExpectedIdentity:
    """What the caller knows a namespace must be bound to (D5.1, D14.2).

    ``repository_id`` is the live repository's; ``issue_ids`` maps an issue
    number to its GitHub issue id, as the caller's snapshot lists them. An
    issue the mapping does not name (one the snapshot no longer lists) has
    its id unchecked. ``repository`` is the ``owner/name`` the caller
    resolved together with ``repository_id``, or ``None`` when it has none:
    a state naming it is of the namespace's repository, also after a
    rename made the binding's recorded name an old one (D5.1).
    """

    repository_id: int
    issue_ids: Mapping[int, int] = field(default_factory=dict)
    repository: str | None = None

    def __post_init__(self) -> None:
        _check_id(self.repository_id, "expected repository_id")
        issue_ids = dict(self.issue_ids)
        for number, issue_id in issue_ids.items():
            _check_id(number, "expected issue number")
            _check_id(issue_id, f"expected issue id of #{number}")
        object.__setattr__(self, "issue_ids", issue_ids)
        if self.repository is not None:
            text = _check_str(
                self.repository,
                "expected repository",
                MAX_OWNER_CHARS + 1 + MAX_REPOSITORY_NAME_CHARS,
            )
            owner, slash, name = text.partition("/")
            if not slash:
                _fail("expected repository", f"is not an owner/name pair: {text!r}")
            _check_name(owner, "expected repository owner", MAX_OWNER_CHARS)
            _check_name(name, "expected repository name", MAX_REPOSITORY_NAME_CHARS)

    def binding_problem(self, binding: RunBinding) -> tuple[UncertainReason, str] | None:
        number = binding.run_key.issue_number
        try:
            check_binding(
                binding,
                run_key=RunKey(self.repository_id, number),
                issue_id=self.issue_ids.get(number, binding.issue_id),
            )
        except NamespaceMismatch as exc:
            reason = (
                UncertainReason.REPOSITORY_MISMATCH
                if exc.field == "repository_id"
                else UncertainReason.BINDING_MISMATCH
            )
            return (reason, str(exc))
        return None


def _check_optional_expected(expected: ExpectedIdentity | None) -> None:
    if expected is not None:
        _check_type(expected, ExpectedIdentity, "expected identity")


def _repository_names(binding: RunBinding, expected: ExpectedIdentity | None) -> tuple[str, ...]:
    """The ``owner/name`` pairs a state of this namespace may name (D5.1).

    The binding's, recorded with its ``repository_id`` at first admission,
    and the caller's, resolved with the live ``repository_id``: a rename
    since the binding is tolerated, and no other name is this repository's.
    A name the repository had only in between is not known here, so a state
    naming it is ``uncertain``.
    """
    names = [binding.repository]
    if expected is not None and expected.repository is not None:
        if expected.repository.lower() != binding.repository.lower():
            names.append(expected.repository)
    return tuple(names)


def _state_identity_problem(
    state: AutoForgeState, binding: RunBinding, expected: ExpectedIdentity | None
) -> str:
    """Why ``state`` is not the run ``binding`` names, or ``""``.

    A state is this namespace's only when its repository is one of
    :func:`_repository_names` (compared case-insensitively, as GitHub does),
    its issue and EPIC are in that repository, and their numbers are the
    binding's: a number alone names an issue of any repository. A URL the
    parser refuses (anything malformed, as :class:`ConfigurationError`)
    names no issue to compare, which is a mismatch too.
    """
    if state.mode is not WorkflowMode.REMOTE:
        return f"the state is a {state.mode.value} run, which never lives in a namespace"
    names = _repository_names(binding, expected)
    if not any(state.repository.lower() == name.lower() for name in names):
        known = " or ".join(repr(name) for name in names)
        return f"the state's repository is {state.repository!r:.200}, the namespace's {known}"
    number = binding.run_key.issue_number
    for what, url, want in (
        ("issue", state.current_issue_url, number),
        ("EPIC", state.epic_url, binding.epic.number),
    ):
        try:
            ref = parse_issue_url(url)
        except ConfigurationError:
            return f"the state names no {what} the binding can be compared with ({url!r:.200})"
        if not ref.same_repository(state.repository):
            return (
                f"the state's {what} {ref.canonical!r:.300} is not in its repository "
                f"{state.repository!r}"
            )
        if ref.number != want:
            return f"the state's {what} is #{ref.number}, the binding's #{want}"
    return ""


def classify(
    lookup: NamespaceLookup | NamespaceUnreadable,
    claim_file: ClaimFileRead,
    *,
    expected: ExpectedIdentity | None,
) -> Classification:
    """Classify one namespace read under L2 (ADR 0008 D14.2 step 4); pure.

    Checked in this order, so the classes are total and mutually exclusive:

    * ``uncertain(reason)`` (:class:`UncertainReason`): the binding cannot be
      read or does not match ``expected`` (``expected=None`` compares the
      namespace with its own binding only); a claim file or a state without
      a binding; a corrupt state or claim file; a claim file or a state
      naming another run or EPIC than the binding, a state of another
      repository included (one named neither as the binding recorded it nor
      as ``expected.repository`` names it); an open claim whose
      ``state_ref.present`` is true while the state is absent;
    * ``settled(settlement)``: the state's settlement is not ``in_progress``;
    * ``resumable(reason)``: the state is ``in_progress``, whether or not a
      claim is open; or a claim is open, the state is absent and its
      ``state_ref.present`` is false;
    * ``fresh``: the binding is valid, no claim is open and the state is
      absent.

    The ``state_ref.present`` rule applies only to an open claim: a state
    absent while no claim is open is never uncertain. A namespace #178
    reports as absent (no binding, no claim, no state) needs no
    classification and is refused (:class:`~autoforge.errors.SchedulerStateError`).
    ``observed_live`` comes from the lock layer, before this runs.
    """
    open_claim = claim_file.open_claim
    if isinstance(lookup, NamespaceUnreadable):
        reason = (
            UncertainReason.BINDING_MISMATCH if lookup.field else UncertainReason.BINDING_UNREADABLE
        )
        return _uncertain(reason, lookup.reason, open_claim)
    if lookup.kind is NamespaceKind.ABSENT and claim_file.status is ClaimFileStatus.ABSENT:
        raise SchedulerStateError(
            f"{lookup.namespace.directory} holds no binding, claim or state: no namespace, "
            "which needs no classification"
        )
    binding = lookup.binding
    if binding is None:
        return _uncertain(
            UncertainReason.UNBOUND,
            f"{lookup.namespace.directory} holds a claim file or a state but no binding.json",
            open_claim,
        )
    if expected is not None:
        mismatch = expected.binding_problem(binding)
        if mismatch is not None:
            return _uncertain(mismatch[0], mismatch[1], open_claim)
    if lookup.state.status is StateStatus.CORRUPT:
        return _uncertain(UncertainReason.STATE_CORRUPT, lookup.state.reason, open_claim)
    if claim_file.status is ClaimFileStatus.CORRUPT:
        return _uncertain(UncertainReason.CLAIM_CORRUPT, claim_file.reason)
    if claim_file.claim_file is not None:
        problem = _claim_identity_problem(claim_file.claim_file, binding)
        if problem is not None:
            return _uncertain(problem[0], problem[1], open_claim)
    state = lookup.state.state
    if state is not None:
        state_problem = _state_identity_problem(state, binding, expected)
        if state_problem:
            return _uncertain(UncertainReason.STATE_MISMATCH, state_problem, open_claim)
        settlement = issue_settlement(state)
        if settlement is not Settlement.IN_PROGRESS:
            return Classification(
                ClassificationKind.SETTLED,
                settlement=settlement,
                open_claim=open_claim,
                detail=f"the state is {settlement.value} (phase {state.phase.value})",
            )
        return Classification(
            ClassificationKind.RESUMABLE,
            resumable=ResumableReason.IN_PROGRESS,
            settlement=settlement,
            open_claim=open_claim,
            detail=f"the state is in progress (phase {state.phase.value})",
        )
    if open_claim is not None:
        if open_claim.state_ref.present:
            return _uncertain(
                UncertainReason.STATE_MISSING,
                f"the open claim of generation {open_claim.generation} was admitted with a "
                "state, and the state is absent",
                open_claim,
            )
        return Classification(
            ClassificationKind.RESUMABLE,
            resumable=ResumableReason.NOT_STARTED,
            open_claim=open_claim,
            detail=f"the open claim of generation {open_claim.generation} has no state yet",
        )
    return Classification(ClassificationKind.FRESH, detail="bound, no open claim and no state")


# -- reconciliation (ADR 0008 D14.1, D14.2) ---------------------------------------------


@dataclass(frozen=True)
class IssueReconciliation:
    """What reconciliation found for one namespace.

    ``fact`` is ``None`` for ``fresh`` and ``resumable``, which are
    evaluated like an issue without a namespace. ``classification`` is
    ``None`` where none ran: the caller's own child, a live owner, another
    EPIC's namespace of this repository, or a run lock that cannot be taken.
    ``closed`` is the claim reconciliation closed as ``settled``.
    """

    issue_number: int
    fact: LocalFact | None
    classification: Classification | None = None
    observed_live: bool = False
    own_child: bool = False
    closed: ClosedClaim | None = None
    detail: str = ""


@dataclass(frozen=True)
class ReconcileReport:
    """Every namespace of the scheduler root, as one EPIC's reconciliation saw it.

    ``issues`` is in ascending issue number and holds one entry per
    namespace other than an absent one. ``global_problems`` are D14.3's
    global uncertainty (a binding of another repository, a refused
    filesystem, a listing that cannot be read): no admission at all.
    ``rejected`` are the entries of ``issues/`` that are no namespace.
    """

    epic: EpicRef
    issues: tuple[IssueReconciliation, ...]
    global_problems: tuple[str, ...] = ()
    rejected: tuple[RejectedEntry, ...] = ()

    @property
    def admissible(self) -> bool:
        return not self.global_problems

    def local_facts(self) -> dict[int, LocalFact]:
        return {item.issue_number: item.fact for item in self.issues if item.fact is not None}


def reconcile_epic(
    root: str | Path,
    epic: EpicRef,
    *,
    own_children: Collection[int] = (),
    only: Collection[int] | None = None,
    expected: ExpectedIdentity,
) -> ReconcileReport:
    """Restart reconciliation of the namespaces under ``root`` for ``epic`` (D14.1, D14.2).

    The caller holds L0. For each namespace, in ascending issue number: the
    caller's own unreaped children are skipped as ``owned``; L2 is taken
    non-blocking, and a held one is ``owned`` (``observed_live``) for this
    pass; holding it, the namespace is read and classified. A namespace of
    this repository bound to another EPIC is ``held:other-epic`` and left
    alone; a binding of another repository is global uncertainty (D14.3),
    whatever EPIC it names. An open claim of a ``settled`` namespace is
    closed ``settled`` and the issue is ``held:settled-namespace``;
    ``resumable`` and ``uncertain`` claims are kept open (an admission's
    supersession, :func:`close_unready` or ``epic archive`` closes them
    later); ``uncertain`` is ``held:uncertain``; L2 is released by
    ``close``. It never spawns, never writes or quarantines a per-issue
    state and never contacts GitHub. ``only``, when given, limits the pass
    to those issues' namespaces: the scheduler's re-check of the namespaces
    it found live (D14.2 step 2).
    """
    _check_type(epic, EpicRef, "reconciled epic")
    _check_type(expected, ExpectedIdentity, "reconciliation's expected identity")
    try:
        listing = list_issue_namespaces(root)
    except (StateError, OSError) as exc:
        return ReconcileReport(
            epic, (), (f"the scheduler's namespaces cannot be listed ({_bounded(str(exc))})",)
        )
    own = frozenset(own_children)
    wanted = None if only is None else frozenset(only)
    problems: list[str] = []
    issues: list[IssueReconciliation] = []
    for namespace in sorted((*listing.bound, *listing.unbound), key=lambda ns: ns.issue_number):
        number = namespace.issue_number
        if wanted is not None and number not in wanted:
            continue
        if number in own:
            issues.append(
                IssueReconciliation(
                    number, LocalFact.OWNED, own_child=True, detail="this scheduler's worker"
                )
            )
            continue
        lock = IssueRunLock(namespace)
        try:
            lock.acquire()
        except IssueBusy as exc:
            issues.append(
                IssueReconciliation(
                    number, LocalFact.OWNED, observed_live=True, detail=_bounded(str(exc))
                )
            )
            continue
        except NetworkFilesystemError as exc:
            problems.append(_bounded(str(exc)))
            issues.append(
                IssueReconciliation(number, LocalFact.HELD_UNCERTAIN, detail=_bounded(str(exc)))
            )
            continue
        except (LockError, StateError, OSError) as exc:
            classification = _uncertain(UncertainReason.RUN_LOCK_UNUSABLE, str(exc))
            issues.append(
                IssueReconciliation(
                    number, LocalFact.HELD_UNCERTAIN, classification, detail=classification.detail
                )
            )
            continue
        try:
            item = _reconcile_held(namespace, lock, epic, expected, problems)
        finally:
            lock.release()
        if item is not None:
            issues.append(item)
    return ReconcileReport(epic, tuple(issues), tuple(problems), listing.rejected)


@dataclass(frozen=True)
class EpicClassification:
    """One namespace classified for one EPIC's reconciliation (D14.2 step 4); nothing closed.

    ``fact`` is what :func:`~autoforge.epic_graph.evaluate` is given: ``None``
    for ``fresh`` and ``resumable``, ``held:settled-namespace``,
    ``held:uncertain``, and ``held:other-epic`` for a namespace of this
    repository bound to another EPIC, which has no ``classification``.
    """

    fact: LocalFact | None
    classification: Classification | None
    detail: str

    @property
    def repository_mismatch(self) -> bool:
        """A binding of another repository: D14.3's global uncertainty."""
        found = self.classification
        return found is not None and found.uncertain is UncertainReason.REPOSITORY_MISMATCH


def classify_for_epic(
    read: NamespaceRead, epic: EpicRef, *, expected: ExpectedIdentity
) -> EpicClassification | None:
    """Classify one namespace read for ``epic`` (D14.2 step 4); pure, ``None`` when absent.

    A namespace of this repository bound to another EPIC is
    ``held:other-epic`` and is not classified; one bound to this EPIC's
    number with another issue id is ``uncertain``; a binding of another
    repository is classified (``uncertain``, a repository mismatch) whatever
    EPIC it names. Otherwise :func:`classify` decides. Reconciliation runs it
    under L2 and then closes a settled namespace's open claim; ``epic
    status`` and ``epic run --dry-run`` (#183) run it on a lock-free read and
    close nothing.
    """
    _check_type(epic, EpicRef, "classified epic")
    _check_type(expected, ExpectedIdentity, "classification's expected identity")
    if read.absent:
        return None
    lookup = read.lookup
    binding = lookup.binding if isinstance(lookup, NamespaceLookup) else None
    # A binding of another repository is global uncertainty whatever EPIC it
    # names (D14.3), so only this repository's bindings are compared by EPIC:
    # classify reports the other repository's.
    if binding is not None and binding.run_key.repository_id == expected.repository_id:
        bound = binding.epic
        if bound.number != epic.number:
            return EpicClassification(
                LocalFact.HELD_OTHER_EPIC,
                None,
                f"bound under EPIC #{bound.number}, not #{epic.number}",
            )
        if bound != epic:
            mismatch = _uncertain(
                UncertainReason.BINDING_MISMATCH,
                f"bound under EPIC #{bound.number} with issue id {bound.issue_id}, not "
                f"{epic.issue_id}",
                read.claim.open_claim,
            )
            return EpicClassification(LocalFact.HELD_UNCERTAIN, mismatch, mismatch.detail)
    classification = classify(lookup, read.claim, expected=expected)
    if classification.kind is ClassificationKind.UNCERTAIN:
        fact: LocalFact | None = LocalFact.HELD_UNCERTAIN
    elif classification.kind is ClassificationKind.SETTLED:
        fact = LocalFact.HELD_SETTLED_NAMESPACE
    else:
        fact = None
    return EpicClassification(fact, classification, classification.detail)


def _reconcile_held(
    namespace: IssueNamespace,
    lock: IssueRunLock,
    epic: EpicRef,
    expected: ExpectedIdentity,
    problems: list[str],
) -> IssueReconciliation | None:
    number = namespace.issue_number
    found = classify_for_epic(read_namespace(namespace), epic, expected=expected)
    if found is None:
        return None
    classification = found.classification
    if classification is None:
        return IssueReconciliation(number, found.fact, detail=found.detail)
    if found.repository_mismatch:
        problems.append(classification.detail)
    if classification.kind is not ClassificationKind.SETTLED:
        return IssueReconciliation(number, found.fact, classification, detail=found.detail)
    closed = None
    if classification.open_claim is not None:
        try:
            closed = close_claim(
                namespace,
                lock,
                classification.open_claim,
                ClaimClassification.SETTLED,
                settlement=classification.settlement,
                detail=f"closed at restart reconciliation: {classification.detail}",
            )
        except StateError as exc:
            failed = _uncertain(UncertainReason.CLAIM_CONFLICT, str(exc), classification.open_claim)
            return IssueReconciliation(
                number, LocalFact.HELD_UNCERTAIN, failed, detail=failed.detail
            )
    return IssueReconciliation(
        number,
        LocalFact.HELD_SETTLED_NAMESPACE,
        classification,
        closed=closed,
        detail=classification.detail,
    )


@dataclass(frozen=True)
class Finalization:
    """What :func:`finalize_worker` decided: the class it read, and the claim it closed."""

    classification: Classification
    closed: ClosedClaim | None = None


def finalize_worker(
    namespace: IssueNamespace,
    lock: IssueRunLock,
    claim: Claim,
    exit_status: int | None,
    *,
    expected: ExpectedIdentity | None = None,
) -> Finalization:
    """Close a reaped worker's claim by the state it left (ADR 0008 D7.2, D14.1).

    The caller re-acquired the issue's L2 after the worker exited. The
    namespace is read strictly and classified: ``settled`` closes the claim
    ``settled``; ``resumable`` (an ``in_progress`` state, or an absent one
    with ``state_ref.present`` false) closes it ``interrupted`` when a worker
    was recorded and ``abandoned`` when none was. Anything else (a corrupt
    state, a state missing while ``present`` is true, a mismatch, an open
    claim on disk that is not ``claim``) leaves it open as ``uncertain``.
    ``expected`` is :func:`classify`'s. ``exit_status`` is recorded as a
    diagnostic and never decides.
    """
    _require_lock(namespace, lock, "finalize a worker's claim")
    _check_optional_expected(expected)
    if exit_status is not None:
        _check_int(exit_status, "worker exit status", -MAX_EXIT_STATUS, MAX_EXIT_STATUS)
    read = read_namespace(namespace)
    if read.absent:
        return Finalization(
            _uncertain(UncertainReason.CLAIM_CONFLICT, f"{namespace.claim_file} does not exist")
        )
    classification = classify(read.lookup, read.claim, expected=expected)
    if classification.kind is ClassificationKind.UNCERTAIN:
        return Finalization(classification)
    on_disk = classification.open_claim
    if on_disk is None or on_disk.identity != claim.identity:
        held = "no open claim" if on_disk is None else f"generation {on_disk.generation}"
        return Finalization(
            _uncertain(
                UncertainReason.CLAIM_CONFLICT,
                f"{namespace.claim_file} holds {held}, not the reaped worker's generation "
                f"{claim.generation}; the file is left as it is",
                on_disk,
            )
        )
    if classification.kind is ClassificationKind.SETTLED:
        closing = ClaimClassification.SETTLED
    else:  # RESUMABLE: FRESH needs no open claim
        closing = (
            ClaimClassification.INTERRUPTED
            if on_disk.worker is not None
            else ClaimClassification.ABANDONED
        )
    detail = f"worker exited with status {exit_status}: {classification.detail}"
    try:
        closed = close_claim(
            namespace,
            lock,
            on_disk,
            closing,
            settlement=classification.settlement,
            exit_status=exit_status,
            detail=detail,
        )
    except ClaimConflict as exc:
        return Finalization(_uncertain(UncertainReason.CLAIM_CONFLICT, str(exc), on_disk))
    return Finalization(classification, closed)


# -- open-claim queries (ADR 0008 D14.4, D17.2) -----------------------------------------


@dataclass(frozen=True)
class OpenClaim:
    """An open claim found by :func:`open_claims_for_epic`.

    ``claim`` is ``None`` when the claim file cannot be read: whose it is and
    whether it is open are unknown, so it counts as open (fail-closed),
    with ``problem`` saying why.
    """

    issue_number: int
    claim: Claim | None
    problem: str = ""


def open_claims_for_epic(root: str | Path, epic: EpicRef) -> tuple[OpenClaim, ...]:
    """The claims bound to ``epic`` that are open, in ascending issue number (D14.4).

    Backs ``epic run``'s refusal, which points to ``epic resume``. A claim
    file names the EPIC its namespace was bound under; one that cannot be
    read is reported too, since it may be this EPIC's. Read-only and
    lock-free: a reader sees one whole file or the other. A listing that
    cannot be read raises.
    """
    _check_type(epic, EpicRef, "epic")
    listing = list_issue_namespaces(root)
    found: list[OpenClaim] = []
    for namespace in sorted((*listing.bound, *listing.unbound), key=lambda ns: ns.issue_number):
        read = read_claim_file(namespace)
        if read.status is ClaimFileStatus.CORRUPT:
            found.append(OpenClaim(namespace.issue_number, None, read.reason))
            continue
        claim_file = read.claim_file
        if claim_file is None or claim_file.current is None:
            continue
        if epic.number == claim_file.epic.number or epic.issue_id == claim_file.epic.issue_id:
            found.append(OpenClaim(namespace.issue_number, claim_file.current))
    return tuple(found)


# -- scheduler.json (ADR 0008 D6) -------------------------------------------------------


class EndReason(Enum):
    NOTHING_ADMISSIBLE = "nothing-admissible"
    DRAINED = "drained"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"
    DEGRADED = "degraded"


class AdmissionStatus(Enum):
    OPEN = "open"
    CLOSED = "closed"
    DEGRADED = "degraded"


class AdmissionReason(Enum):
    STOP_REQUESTED = "stop-requested"  # closed
    INTERRUPTED = "interrupted"  # closed
    SNAPSHOT_REFUSED = "snapshot-refused"  # degraded
    EPIC_NOT_ADMISSIBLE = "epic-not-admissible"  # degraded


_ADMISSION_REASONS: dict[AdmissionStatus, frozenset[AdmissionReason]] = {
    AdmissionStatus.OPEN: frozenset(),
    AdmissionStatus.CLOSED: frozenset(
        {AdmissionReason.STOP_REQUESTED, AdmissionReason.INTERRUPTED}
    ),
    AdmissionStatus.DEGRADED: frozenset(
        {AdmissionReason.SNAPSHOT_REFUSED, AdmissionReason.EPIC_NOT_ADMISSIBLE}
    ),
}


@dataclass(frozen=True)
class AdmissionState:
    """Whether the scheduler admits: ``open`` without a reason, else with one of its own."""

    status: AdmissionStatus
    reason: AdmissionReason | None = None

    def __post_init__(self) -> None:
        _check_type(self.status, AdmissionStatus, "scheduler admission status")
        if self.reason is not None:
            _check_type(self.reason, AdmissionReason, "scheduler admission reason")
        allowed = _ADMISSION_REASONS[self.status]
        if (self.reason is None) != (not allowed) or (
            self.reason is not None and self.reason not in allowed
        ):
            names = ", ".join(sorted(reason.value for reason in allowed)) or "none"
            _fail(
                "scheduler admission",
                f"{self.status.value} takes the reason(s) {names}, got "
                f"{None if self.reason is None else self.reason.value}",
            )

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "reason": None if self.reason is None else self.reason.value,
        }

    @classmethod
    def from_dict(cls, raw: object) -> AdmissionState:
        data = _exact(raw, ("status", "reason"), "scheduler admission")
        reason = data["reason"]
        return cls(
            _check_enum(AdmissionStatus, data["status"], "scheduler admission status"),
            None
            if reason is None
            else _check_enum(AdmissionReason, reason, "scheduler admission reason"),
        )


@dataclass(frozen=True)
class Invocation:
    """The scheduler process the file belongs to: ``ended_at`` and ``end_reason`` go together."""

    invocation_id: str
    started_at: str
    ended_at: str | None = None
    end_reason: EndReason | None = None

    def __post_init__(self) -> None:
        _check_hex(self.invocation_id, "scheduler invocation_id", _HEX32)
        _check_timestamp(self.started_at, "scheduler invocation started_at")
        if self.ended_at is not None:
            _check_timestamp(self.ended_at, "scheduler invocation ended_at")
        if self.end_reason is not None:
            _check_type(self.end_reason, EndReason, "scheduler invocation end_reason")
        if (self.ended_at is None) != (self.end_reason is None):
            _fail("scheduler invocation", "has only one of ended_at and end_reason")

    def to_dict(self) -> dict:
        return {
            "invocation_id": self.invocation_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "end_reason": None if self.end_reason is None else self.end_reason.value,
        }

    @classmethod
    def from_dict(cls, raw: object) -> Invocation:
        data = _exact(
            raw, ("invocation_id", "started_at", "ended_at", "end_reason"), "scheduler invocation"
        )
        end_reason = data["end_reason"]
        return cls(
            data["invocation_id"],
            data["started_at"],
            data["ended_at"],
            None
            if end_reason is None
            else _check_enum(EndReason, end_reason, "scheduler invocation end_reason"),
        )


@dataclass(frozen=True)
class SchedulerConfig:
    """The configuration a scheduler runs under (D18.3, D18.4) and its digest (D9.2 step 4)."""

    max_parallel: int
    poll_interval_seconds: int
    stop_grace_seconds: int
    config_digest: str

    def __post_init__(self) -> None:
        _check_int(self.max_parallel, "scheduler config max_parallel", *MAX_PARALLEL_BOUNDS)
        _check_int(
            self.poll_interval_seconds,
            "scheduler config poll_interval_seconds",
            *POLL_INTERVAL_BOUNDS,
        )
        _check_int(
            self.stop_grace_seconds, "scheduler config stop_grace_seconds", *STOP_GRACE_BOUNDS
        )
        _check_hex(self.config_digest, "scheduler config config_digest", _HEX64)

    def to_dict(self) -> dict:
        return {
            "max_parallel": self.max_parallel,
            "poll_interval_seconds": self.poll_interval_seconds,
            "stop_grace_seconds": self.stop_grace_seconds,
            "config_digest": self.config_digest,
        }

    @classmethod
    def from_dict(cls, raw: object) -> SchedulerConfig:
        data = _exact(
            raw,
            ("max_parallel", "poll_interval_seconds", "stop_grace_seconds", "config_digest"),
            "scheduler config",
        )
        return cls(
            data["max_parallel"],
            data["poll_interval_seconds"],
            data["stop_grace_seconds"],
            data["config_digest"],
        )


_COUNT_KEYS = ("children", "ready", "blocked", "closed", "unsupported", "owned", "held")


@dataclass(frozen=True)
class SnapshotSummary:
    """The last snapshot's digest, time and verdict counts (#179): diagnostics only."""

    digest: str
    observed_at: str
    children: int = 0
    ready: int = 0
    blocked: int = 0
    closed: int = 0
    unsupported: int = 0
    owned: int = 0
    held: int = 0

    def __post_init__(self) -> None:
        _check_hex(self.digest, "scheduler last_snapshot digest", _HEX64)
        _check_timestamp(self.observed_at, "scheduler last_snapshot observed_at")
        for name in _COUNT_KEYS:
            _check_int(getattr(self, name), f"scheduler last_snapshot count {name}", 0, MAX_COUNT)

    def to_dict(self) -> dict:
        return {
            "digest": self.digest,
            "observed_at": self.observed_at,
            "counts": {name: getattr(self, name) for name in _COUNT_KEYS},
        }

    @classmethod
    def from_dict(cls, raw: object) -> SnapshotSummary:
        data = _exact(raw, ("digest", "observed_at", "counts"), "scheduler last_snapshot")
        counts = _exact(data["counts"], _COUNT_KEYS, "scheduler last_snapshot counts")
        return cls(data["digest"], data["observed_at"], **counts)


_SCHEDULER_KEYS = (
    "schema",
    "repository",
    "epic",
    "invocation",
    "config",
    "admission",
    "last_snapshot",
    "created_at",
    "updated_at",
)


@dataclass(frozen=True)
class SchedulerState:
    """``scheduler.json``, schema ``autoforge.scheduler/1`` (ADR 0008 D6.1): run-level facts.

    It lists no claim (D6.2): claims live only in each issue's ``claim.json``.
    """

    repository_id: int
    repository_owner: str
    repository_name: str
    epic: EpicRef
    invocation: Invocation
    config: SchedulerConfig
    admission: AdmissionState
    last_snapshot: SnapshotSummary | None = None
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)

    def __post_init__(self) -> None:
        _check_id(self.repository_id, "scheduler repository_id")
        _check_name(self.repository_owner, "scheduler repository owner", MAX_OWNER_CHARS)
        _check_name(self.repository_name, "scheduler repository name", MAX_REPOSITORY_NAME_CHARS)
        _check_type(self.epic, EpicRef, "scheduler epic")
        _check_type(self.invocation, Invocation, "scheduler invocation")
        _check_type(self.config, SchedulerConfig, "scheduler config")
        _check_type(self.admission, AdmissionState, "scheduler admission")
        if self.last_snapshot is not None:
            _check_type(self.last_snapshot, SnapshotSummary, "scheduler last_snapshot")
        _check_timestamp(self.created_at, "scheduler created_at")
        _check_timestamp(self.updated_at, "scheduler updated_at")

    def to_dict(self) -> dict:
        return {
            "schema": SCHEDULER_SCHEMA,
            "repository": {
                "owner": self.repository_owner,
                "name": self.repository_name,
                "repository_id": self.repository_id,
            },
            "epic": _epic_dict(self.epic),
            "invocation": self.invocation.to_dict(),
            "config": self.config.to_dict(),
            "admission": self.admission.to_dict(),
            "last_snapshot": None if self.last_snapshot is None else self.last_snapshot.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: object, *, where: str = SCHEDULER_FILENAME) -> SchedulerState:
        """Strictly parse a scheduler state: an unknown version or field is refused."""
        try:
            if not isinstance(raw, dict):
                _fail("scheduler state", f"must be a JSON object, got {type(raw).__name__}")
            schema = raw.get("schema")
            if schema != SCHEDULER_SCHEMA:
                _fail(
                    "scheduler state",
                    f"has schema {schema!r:.80}; this controller reads only {SCHEDULER_SCHEMA}",
                )
            data = _exact(raw, _SCHEDULER_KEYS, "scheduler state")
            repo = _exact(
                data["repository"], ("owner", "name", "repository_id"), "scheduler repository"
            )
            snapshot = data["last_snapshot"]
            return cls(
                repository_id=repo["repository_id"],
                repository_owner=repo["owner"],
                repository_name=repo["name"],
                epic=_epic_of(data["epic"], "scheduler epic"),
                invocation=Invocation.from_dict(data["invocation"]),
                config=SchedulerConfig.from_dict(data["config"]),
                admission=AdmissionState.from_dict(data["admission"]),
                last_snapshot=None if snapshot is None else SnapshotSummary.from_dict(snapshot),
                created_at=data["created_at"],
                updated_at=data["updated_at"],
            )
        except SchedulerStateError as exc:
            raise SchedulerStateError(
                f"invalid {SCHEDULER_SCHEMA} document {where}: {exc}"
            ) from exc


def _epic_relative(epic_number: int) -> tuple[str, ...]:
    _check_id(epic_number, "epic number")
    return (AUTOFORGE_DIRNAME, SCHEDULER_DIRNAME, EPICS_DIRNAME, str(epic_number))


def scheduler_state_path(coordination_root: str | Path, epic_number: int) -> Path:
    """``<coordination root>/autoforge/scheduler/epics/<n>/scheduler.json``."""
    return Path(coordination_root).joinpath(*_epic_relative(epic_number), SCHEDULER_FILENAME)


def load_scheduler_state(coordination_root: str | Path, epic_number: int) -> SchedulerState | None:
    """EPIC ``epic_number``'s ``scheduler.json``, strictly; ``None`` when there is none.

    A corrupt file (one holding JSON ``null`` included), or one of an
    unknown version, raises :class:`~autoforge.errors.SchedulerStateError`:
    the scheduler's global uncertainty (D6.3), recovered only by
    ``epic run --force``. Read-only.
    """
    relative = _epic_relative(epic_number)
    where = str(scheduler_state_path(coordination_root, epic_number))
    with _open_anchor(Path(coordination_root)) as anchor:
        try:
            epic_root = anchor.subroot("/".join(relative))
        except FileNotFoundError:
            return None
    with epic_root:
        raw = _read_document(epic_root, SCHEDULER_FILENAME, limit=MAX_SCHEDULER_BYTES, where=where)
    if raw is None:
        return None
    state = SchedulerState.from_dict(raw, where=where)
    if state.epic.number != epic_number:
        raise SchedulerStateError(f"{where} names EPIC #{state.epic.number}, not #{epic_number}")
    return state


def write_scheduler_state(coordination_root: str | Path, state: SchedulerState) -> SchedulerState:
    """Write ``scheduler.json`` atomically, ``updated_at`` stamped; return what was written.

    The single writer is the L0 holder (D11.1). The directories from the
    coordination root down are created ``O_NOFOLLOW``, and the write is an
    fsynced temporary replaced over the name, refused while this process
    holds L1 (D11.2).
    """
    _check_type(state, SchedulerState, "scheduler state")
    written = replace(state, updated_at=utcnow_iso())
    path = scheduler_state_path(coordination_root, state.epic.number)
    assert_shared_git_lock_free(f"write {path}")
    payload = _payload(written.to_dict(), limit=MAX_SCHEDULER_BYTES, where=str(path))
    with _open_anchor(Path(coordination_root)) as anchor:
        anchor.write_bytes(
            "/".join((*_epic_relative(state.epic.number), SCHEDULER_FILENAME)), payload
        )
    return written


def quarantine_scheduler_state(coordination_root: str | Path, epic_number: int) -> Path | None:
    """Move a corrupt ``scheduler.json`` aside for ``epic run --force`` (D6.3).

    Renamed to ``scheduler.json.corrupt-<UTC timestamp>`` with the
    link-then-unlink primitive of ``run --force``'s quarantine, so nothing
    is lost; safe because the file holds no claim. Returns the new path, or
    ``None`` when there is no file. A file that loads is refused
    (:class:`~autoforge.errors.SchedulerStateError`): ``--force`` recovers
    from corruption only.
    """
    path = scheduler_state_path(coordination_root, epic_number)
    try:
        if load_scheduler_state(coordination_root, epic_number) is None:
            return None
    except SchedulerStateError:
        pass
    else:
        raise SchedulerStateError(
            f"{path} is a valid {SCHEDULER_SCHEMA} document; only a corrupt one is quarantined"
        )
    assert_shared_git_lock_free(f"quarantine {path}")
    with _open_anchor(Path(coordination_root)) as anchor:
        epic_root = anchor.subroot("/".join(_epic_relative(epic_number)))
    with epic_root:
        return quarantine_state_file(path, root=epic_root, what="scheduler state file")
