"""Per-issue run identities and their on-disk namespaces (ADR 0008 D5, D17.3; #178).

A standalone controller keeps one run's state in one state directory and
reuses it for the next issue (``reset_for_new_issue``). The parallel EPIC
scheduler instead gives every issue a stable identity and a directory of its
own, found again from the repository alone after a crash:

* :class:`RunKey` is ``(repository_id, issue_number)``: GitHub's numeric
  repository id and the issue number. Titles, URLs and the owner/name pair
  are never part of it, so a renamed repository keeps its namespaces.
* :func:`issue_namespace` is the layout, a pure function of the coordination
  root (the git common dir, the key the repository lock already uses) and
  the issue number::

      <git common dir>/autoforge/scheduler/issues/<n>/
        run.lock          L2, reserved: #180 creates it, nothing here does
        binding.json      autoforge.run-binding/1 (this module)
        claim.json        autoforge.scheduler-claim/1: scheduler_state.py (#181), never here
        state/            an ordinary state directory (state.json, logs/, read-context/)
        workers/          worker console logs (#182)

  The common dir is the anchor: every component below it is AutoForge's own
  and is opened descriptor-relative with ``O_NOFOLLOW`` (``safefs``,
  :class:`autoforge.state.StatePaths`). The issue's
  worktree is not in the namespace: it keeps the engine's derivation
  (:meth:`autoforge.engine.ControllerEngine.issue_worktree_path`), so a
  worker reuses the worktree a standalone run made for the same issue.
* ``binding.json`` (:class:`RunBinding`) records whose namespace it is. It is
  strict, written once with create-exclusive semantics and never rewritten;
  :func:`check_binding` turns a difference from the caller's expectation
  into a typed :class:`~autoforge.errors.NamespaceMismatch`.
* :func:`load_issue_namespace`, :func:`load_binding` (the binding alone, a
  worker's first read) and :func:`list_issue_namespaces` only read:
  they never create a directory, quarantine or write. A corrupt state is
  reported for its own namespace and no other.
* :func:`archive_issue_state` moves ``state/state.json`` aside with the
  link-then-unlink primitive of ``run --force``'s quarantine.
* :func:`check_standalone_state_dir` is the standalone commands' refusal of
  a state directory inside the scheduler root (D17.3), and
  :func:`namespace_issue_of_state_dir` names the namespace whose own state
  directory ``unblock`` acts on (the forward guard exempts it, D17.2).

Nothing here takes a lock, creates a claim or reads GitHub: the caller
supplies the ids (#179), holds the issue's L2 for a write (#180), and decides
what a lookup means (#181). Every name below the anchor is a constant or a
validated decimal issue number, never a value read from a file.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import NoReturn

from .errors import NamespaceError, NamespaceMismatch, SchedulerStateDirError, StateError
from .redaction import redact
from .safefs import ReadLimitExceeded, SafeRoot, WalkBudgetExceeded, entry_kind
from .state import (
    STATE_FILENAME,
    AutoForgeState,
    StatePaths,
    archive_state_file,
    load_state,
    utcnow_iso,
)

AUTOFORGE_DIRNAME = "autoforge"  # inside the git common dir, beside controller.lock
SCHEDULER_DIRNAME = "scheduler"
ISSUES_DIRNAME = "issues"
BINDING_FILENAME = "binding.json"
# Reserved in every issue namespace for the children that own them: the
# per-issue run lock (L2, #180) and the scheduler claim (#181). This module
# reads whether a claim exists and nothing else about either.
RUN_LOCK_FILENAME = "run.lock"
CLAIM_FILENAME = "claim.json"
STATE_DIRNAME = "state"
WORKERS_DIRNAME = "workers"

BINDING_SCHEMA = "autoforge.run-binding/1"

# More entries than this under ``issues/`` refuses the listing (D18.2): no
# EPIC this controller can drive has that many children, and the bound is
# applied while the directory is read, before anything is sorted or opened.
MAX_ISSUE_NAMESPACES = 10_000
# A binding is a few hundred bytes; a larger file is refused unread.
MAX_BINDING_BYTES = 16 * 1024
# GitHub's ids are 64-bit signed integers.
MAX_ID = 2**63 - 1
MAX_OWNER_CHARS = 39  # GitHub's limit on a user or organization name
MAX_REPOSITORY_NAME_CHARS = 100  # GitHub's limit on a repository name
MAX_WORKTREE_PATH_CHARS = 4096  # PATH_MAX
MAX_TIMESTAMP_CHARS = 32  # utcnow_iso(): 2026-01-01T00:00:00.000000+00:00
# The reason a namespace's state could not be loaded, as reported: redacted,
# then bounded, because it quotes the state loader's message.
MAX_STATE_REASON_CHARS = 2000
# A binding's refusal quotes the file only this far: at most this many
# unknown field names (each cut to 80 characters), and an out-of-range id
# only while its decimal rendering is this short (otherwise its length).
MAX_QUOTED_NAMES = 5
MAX_QUOTED_INT_CHARS = 40

_BINDING_KEYS = (
    "schema",
    "run_key",
    "issue_id",
    "repository",
    "epic",
    "worktree_path",
    "created_at",
)
_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
# An issue directory's name: the canonical decimal rendering of a positive
# issue number, so ``011`` can never alias ``11``. Nineteen digits reach
# past MAX_ID; the listing checks the value as well.
_ISSUE_DIR_RE = re.compile(r"[1-9][0-9]{0,18}")


# -- field checks -----------------------------------------------------------------------


def _fail(what: str, message: str) -> NoReturn:
    raise NamespaceError(f"{what} {message}")


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


def _check_id(value: object, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(what, f"must be an integer, got {type(value).__name__}")
    if value < 1 or value > MAX_ID:
        digits = str(abs(value))
        got = str(value) if len(digits) <= MAX_QUOTED_INT_CHARS else f"{len(digits)} digits"
        _fail(what, f"must be a positive integer of at most {MAX_ID}, got {got}")
    return value


def _quoted_names(names: list[str]) -> str:
    shown = ", ".join(f"{name!r:.80}" for name in names[:MAX_QUOTED_NAMES])
    more = len(names) - MAX_QUOTED_NAMES
    return f"[{shown}]" if more <= 0 else f"[{shown}] and {more} more"


def _check_str(value: object, what: str, max_chars: int) -> str:
    if not isinstance(value, str):
        _fail(what, f"must be a string, got {type(value).__name__}")
    if not value:
        _fail(what, "must not be empty")
    if len(value) > max_chars:
        _fail(what, f"is {len(value)} characters, over its bound of {max_chars}")
    return value


def _check_name(value: object, what: str, max_chars: int) -> str:
    text = _check_str(value, what, max_chars)
    if not _NAME_RE.fullmatch(text) or text in (".", ".."):
        _fail(what, f"is not a GitHub owner or repository name: {text!r}")
    return text


def _check_path(value: object, what: str) -> str:
    text = _check_str(value, what, MAX_WORKTREE_PATH_CHARS)
    if not os.path.isabs(text) or any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        _fail(what, "must be an absolute path without control characters")
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


# -- identity and layout ----------------------------------------------------------------


@dataclass(frozen=True)
class RunKey:
    """The identity of one issue's run and of its namespace (ADR 0008 D5.1).

    ``repository_id`` is GitHub's numeric repository id and ``issue_number``
    the issue's number, both positive. ``str(key)`` is the stable
    ``<repository_id>/<issue_number>`` diagnostics print.
    """

    repository_id: int
    issue_number: int

    def __post_init__(self) -> None:
        _check_id(self.repository_id, "run key repository_id")
        _check_id(self.issue_number, "run key issue_number")

    def __str__(self) -> str:
        return f"{self.repository_id}/{self.issue_number}"


def scheduler_root(coordination_root: str | Path) -> Path:
    """``<coordination root>/autoforge/scheduler``: the scheduler's private tree."""
    return Path(coordination_root) / AUTOFORGE_DIRNAME / SCHEDULER_DIRNAME


@dataclass(frozen=True)
class IssueNamespace:
    """Where one issue's scheduler run lives (ADR 0008 D5.3); paths only, no I/O.

    Build it with :func:`issue_namespace`. ``coordination_root`` is the git
    common dir and the anchor of every path here; :attr:`relative` is the
    chain below it that is walked with ``O_NOFOLLOW``.
    """

    coordination_root: Path
    issue_number: int

    @property
    def relative(self) -> tuple[str, ...]:
        """The namespace directory's components below the coordination root."""
        return (AUTOFORGE_DIRNAME, SCHEDULER_DIRNAME, ISSUES_DIRNAME, str(self.issue_number))

    @property
    def directory(self) -> Path:
        return self.coordination_root.joinpath(*self.relative)

    @property
    def binding_file(self) -> Path:
        return self.directory / BINDING_FILENAME

    @property
    def run_lock_file(self) -> Path:
        """L2's path (#180); reserved, never created here."""
        return self.directory / RUN_LOCK_FILENAME

    @property
    def claim_file(self) -> Path:
        """The scheduler claim's path (#181); reserved, never created here."""
        return self.directory / CLAIM_FILENAME

    @property
    def state_dir(self) -> Path:
        return self.directory / STATE_DIRNAME

    @property
    def state_file(self) -> Path:
        return self.state_dir / STATE_FILENAME

    @property
    def workers_dir(self) -> Path:
        return self.directory / WORKERS_DIRNAME

    def state_paths(self) -> StatePaths:
        """The namespace's state directory, anchored at the coordination root.

        What a worker's engine runs on (:meth:`ControllerEngine.bind_issue_namespace
        <autoforge.engine.ControllerEngine.bind_issue_namespace>`): every
        component from ``autoforge`` down to ``state`` is opened ``O_NOFOLLOW``
        from the common dir's descriptor, never resolved from the working
        directory, ``--state-dir`` or ``config.state_dir``.
        """
        return StatePaths.from_state_dir(self.state_dir, anchor=self.coordination_root)


def issue_namespace(coordination_root: str | Path, issue_number: int) -> IssueNamespace:
    """The namespace of ``issue_number`` under ``coordination_root``; pure and deterministic.

    ``coordination_root`` is the git common dir as an absolute path (the
    caller resolves it, as :func:`autoforge.locking.repository_lock_path`
    does); it is normalised lexically and never looked up, so two calls with
    the same arguments give the same value whatever the filesystem holds.
    """
    _check_id(issue_number, "issue number")
    return IssueNamespace(_coordination_root(coordination_root), issue_number)


def _coordination_root(coordination_root: str | Path) -> Path:
    root = str(coordination_root)
    if not os.path.isabs(root):
        raise NamespaceError(
            f"coordination root {root!r} must be an absolute path (the resolved git common dir)"
        )
    return Path(os.path.normpath(root))


# -- binding.json -----------------------------------------------------------------------


@dataclass(frozen=True)
class EpicRef:
    """The EPIC a namespace was first bound under: its number and GitHub issue id."""

    number: int
    issue_id: int

    def __post_init__(self) -> None:
        _check_id(self.number, "epic number")
        _check_id(self.issue_id, "epic issue_id")


@dataclass(frozen=True)
class RunBinding:
    """``binding.json``, schema ``autoforge.run-binding/1`` (ADR 0008 D5.2).

    ``run_key``, ``issue_id`` and ``epic`` are identity, compared by
    :func:`check_binding`; ``repository_owner``/``repository_name`` are for
    display and ``worktree_path`` for diagnostics only. Every field is
    validated on construction, so a value that exists is one the file may
    hold.
    """

    run_key: RunKey
    issue_id: int
    repository_owner: str
    repository_name: str
    epic: EpicRef
    worktree_path: str
    created_at: str = field(default_factory=utcnow_iso)

    def __post_init__(self) -> None:
        if not isinstance(self.run_key, RunKey):
            _fail("binding run_key", f"must be a RunKey, got {type(self.run_key).__name__}")
        _check_id(self.issue_id, "binding issue_id")
        _check_name(self.repository_owner, "binding repository owner", MAX_OWNER_CHARS)
        _check_name(self.repository_name, "binding repository name", MAX_REPOSITORY_NAME_CHARS)
        if not isinstance(self.epic, EpicRef):
            _fail("binding epic", f"must be an EpicRef, got {type(self.epic).__name__}")
        _check_path(self.worktree_path, "binding worktree_path")
        _check_timestamp(self.created_at, "binding created_at")

    @property
    def repository(self) -> str:
        """``owner/name``, for display."""
        return f"{self.repository_owner}/{self.repository_name}"

    def to_dict(self) -> dict:
        return {
            "schema": BINDING_SCHEMA,
            "run_key": {
                "repository_id": self.run_key.repository_id,
                "issue_number": self.run_key.issue_number,
            },
            "issue_id": self.issue_id,
            "repository": {"owner": self.repository_owner, "name": self.repository_name},
            "epic": {"number": self.epic.number, "issue_id": self.epic.issue_id},
            "worktree_path": self.worktree_path,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: object, *, where: str = BINDING_FILENAME) -> RunBinding:
        """Strictly parse a binding: an unknown version or field is refused, never skipped.

        ``where`` names the file in every refusal (:class:`NamespaceError`).
        """
        try:
            if not isinstance(raw, dict):
                _fail("binding", f"must be a JSON object, got {type(raw).__name__}")
            # The version decides what the other fields mean, so it is read
            # first: a later schema is refused as such, not as unknown fields.
            schema = raw.get("schema")
            if schema != BINDING_SCHEMA:
                _fail(
                    "binding",
                    f"has schema {schema!r:.80}; this controller reads only {BINDING_SCHEMA}",
                )
            data = _exact(raw, _BINDING_KEYS, "binding")
            key = _exact(data["run_key"], ("repository_id", "issue_number"), "binding run_key")
            repo = _exact(data["repository"], ("owner", "name"), "binding repository")
            epic = _exact(data["epic"], ("number", "issue_id"), "binding epic")
            return cls(
                run_key=RunKey(key["repository_id"], key["issue_number"]),
                issue_id=data["issue_id"],
                repository_owner=repo["owner"],
                repository_name=repo["name"],
                epic=EpicRef(epic["number"], epic["issue_id"]),
                worktree_path=data["worktree_path"],
                created_at=data["created_at"],
            )
        except NamespaceError as exc:
            raise NamespaceError(f"invalid {BINDING_SCHEMA} document {where}: {exc}") from exc


def check_binding(
    binding: RunBinding, *, run_key: RunKey, issue_id: int, epic: EpicRef | None = None
) -> None:
    """Refuse a binding that names another run than the caller expects.

    Raises :class:`~autoforge.errors.NamespaceMismatch` with ``field`` set to
    the first identity that differs, in the order ``repository_id`` (another
    repository behind this common dir: the scheduler's global uncertainty,
    D14.3), ``issue_number``, ``issue_id`` (the issue was deleted and its
    number reused) and ``epic``. ``epic=None`` skips the EPIC: a namespace
    first bound under another EPIC is the caller's ``held:other-epic``, not
    a broken binding (D5.5).
    """
    checks: list[tuple[str, object, object]] = [
        ("repository_id", binding.run_key.repository_id, run_key.repository_id),
        ("issue_number", binding.run_key.issue_number, run_key.issue_number),
        ("issue_id", binding.issue_id, issue_id),
    ]
    if epic is not None:
        checks.append(
            (
                "epic",
                (binding.epic.number, binding.epic.issue_id),
                (epic.number, epic.issue_id),
            )
        )
    for name, recorded, expected in checks:
        if recorded != expected:
            raise NamespaceMismatch(
                f"the namespace of run {binding.run_key} is bound with {name} {recorded}, "
                f"not the expected {expected}; a binding is never rewritten to agree",
                field=name,
            )


def write_binding(namespace: IssueNamespace, binding: RunBinding) -> Path:
    """Create the namespace's ``binding.json``, exactly once; returns its path.

    The bytes are written and fsynced into a temporary and *linked* to the
    final name (:meth:`SafeRoot.create_exclusive`), so a crash leaves either
    no binding or a complete one, and an existing entry of that name (a
    binding, a link, anything) refuses the write with
    :class:`NamespaceError`. The directories from the coordination root down
    are created ``O_NOFOLLOW``, so a link planted at any of them refuses the
    write instead of redirecting it. The caller (#182's admission) holds the
    issue's L2 and decides when to call this.
    """
    if binding.run_key.issue_number != namespace.issue_number:
        raise NamespaceMismatch(
            f"binding for run {binding.run_key} cannot be written into the namespace of "
            f"issue {namespace.issue_number}",
            field="issue_number",
        )
    payload = json.dumps(binding.to_dict(), indent=2, sort_keys=True) + "\n"
    with _open_anchor(namespace.coordination_root) as anchor:
        try:
            anchor.create_exclusive(
                "/".join((*namespace.relative, BINDING_FILENAME)), payload.encode("utf-8")
            )
        except FileExistsError:
            raise NamespaceError(
                f"the namespace of issue {namespace.issue_number} is already bound "
                f"({namespace.binding_file} exists); binding.json is written once and never "
                "rewritten"
            ) from None
    return namespace.binding_file


# -- lookup -----------------------------------------------------------------------------


class NamespaceKind(Enum):
    """What :func:`load_issue_namespace` found (ADR 0008 D14.2 classifies it)."""

    #: No binding, no claim file and no ``state/state.json``. A directory
    #: holding only ``run.lock`` (a crash between taking L2 and writing the
    #: binding), logs or worker logs is absent too.
    ABSENT = "absent"
    #: A claim file or a state exists, and ``binding.json`` does not.
    UNBOUND = "unbound"
    #: ``binding.json`` exists and is a valid binding of this issue.
    BOUND = "bound"


class StateStatus(Enum):
    ABSENT = "absent"
    LOADED = "loaded"
    CORRUPT = "corrupt"


@dataclass(frozen=True)
class NamespaceState:
    """The namespace's ``state/state.json``, read strictly and read-only.

    ``state`` is set when :attr:`status` is ``LOADED``; ``reason`` (redacted
    and bounded) when it is ``CORRUPT``: unreadable, unparseable, of an
    unknown protocol, not a regular file, or reached through a link.
    """

    status: StateStatus
    state: AutoForgeState | None = None
    reason: str = ""


@dataclass(frozen=True)
class NamespaceLookup:
    """One namespace as it is on disk; nothing was created, moved or written to get it."""

    namespace: IssueNamespace
    kind: NamespaceKind
    binding: RunBinding | None
    state: NamespaceState
    #: Whether ``claim.json`` exists (as any kind of entry); parsing it is #181's.
    claim_present: bool


def load_issue_namespace(coordination_root: str | Path, issue_number: int) -> NamespaceLookup:
    """Read issue ``issue_number``'s namespace, strictly and without writing anything.

    A binding is parsed strictly: a corrupt one (including one past the JSON
    decoder's integer or nesting limit), or one that names another
    issue than its directory, raises (:class:`NamespaceError`,
    :class:`~autoforge.errors.NamespaceMismatch`), and the caller reports
    that namespace as uncertain. A symbolic link or another kind of entry at
    any directory of the namespace's path raises
    :class:`~autoforge.safefs.UnsafePathError` without following it. The
    state is loaded with :func:`autoforge.state.load_state` through a root
    opened ``O_NOFOLLOW`` from the coordination root, never created and
    never quarantined; a state that does not load is reported as
    ``CORRUPT`` for this namespace only.
    """
    namespace = issue_namespace(coordination_root, issue_number)
    with _open_anchor(namespace.coordination_root) as anchor:
        try:
            issue_root = anchor.subroot("/".join(namespace.relative))
        except FileNotFoundError:
            return NamespaceLookup(
                namespace, NamespaceKind.ABSENT, None, NamespaceState(StateStatus.ABSENT), False
            )
    with issue_root:
        binding = _read_binding(issue_root, namespace)
        claim_present = issue_root.lstat(CLAIM_FILENAME) is not None
        state = _read_state(issue_root, namespace)
    if binding is not None:
        kind = NamespaceKind.BOUND
    elif claim_present or state.status is not StateStatus.ABSENT:
        kind = NamespaceKind.UNBOUND
    else:
        kind = NamespaceKind.ABSENT
    return NamespaceLookup(namespace, kind, binding, state, claim_present)


def load_binding(namespace: IssueNamespace) -> RunBinding | None:
    """The namespace's ``binding.json`` alone, strictly; ``None`` when there is none.

    What a worker checks before it reads anything else of its namespace
    (#182): :func:`load_issue_namespace`'s strict binding read, without the
    state. Read-only; a corrupt binding, one of another issue and a link at
    any directory of the path raise as they do there.
    """
    with _open_anchor(namespace.coordination_root) as anchor:
        try:
            issue_root = anchor.subroot("/".join(namespace.relative))
        except FileNotFoundError:
            return None
    with issue_root:
        return _read_binding(issue_root, namespace)


def _read_binding(issue_root: SafeRoot, namespace: IssueNamespace) -> RunBinding | None:
    where = str(namespace.binding_file)
    st = issue_root.lstat(BINDING_FILENAME)
    if st is None:
        return None
    kind = entry_kind(st.st_mode)
    if kind is not None:
        raise NamespaceError(f"{where} is a {kind}, not a regular file; it is never followed")
    try:
        raw = issue_root.read_bytes(BINDING_FILENAME, limit=MAX_BINDING_BYTES)
    except ReadLimitExceeded as exc:
        raise NamespaceError(
            f"{where} is larger than {MAX_BINDING_BYTES} bytes, which no binding can be"
        ) from exc
    if raw is None:
        # Removed between the lstat and the open: no binding is what is there now.
        return None
    try:
        data = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_keys,
            parse_constant=_no_constant,
        )
    except UnicodeDecodeError as exc:
        raise NamespaceError(f"{where} is not valid UTF-8 ({exc})") from exc
    except json.JSONDecodeError as exc:
        raise NamespaceError(f"{where} is not valid JSON ({exc})") from exc
    except NamespaceError as exc:  # a duplicated key or a non-JSON number
        raise NamespaceError(f"{where}: {exc}") from exc
    except (ValueError, RecursionError) as exc:
        # The decoder's own limits, which are not JSONDecodeError: an integer
        # past ``sys.get_int_max_str_digits()`` and nesting past the recursion
        # limit or the C stack, both inside MAX_BINDING_BYTES. Only the type
        # is reported, never the input; an interrupt still propagates.
        raise NamespaceError(
            f"{where} exceeds the JSON decoder's integer or nesting limit "
            f"({type(exc).__name__}); no binding comes near it"
        ) from exc
    binding = RunBinding.from_dict(data, where=where)
    if binding.run_key.issue_number != namespace.issue_number:
        raise NamespaceMismatch(
            f"{where} binds run {binding.run_key}, not issue {namespace.issue_number}",
            field="issue_number",
        )
    return binding


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    data: dict = {}
    for key, value in pairs:
        if key in data:
            raise NamespaceError(f"binding has the field {key!r:.80} more than once")
        data[key] = value
    return data


def _no_constant(name: str) -> NoReturn:
    raise NamespaceError(f"binding holds the non-JSON number {name}")


def _read_state(issue_root: SafeRoot, namespace: IssueNamespace) -> NamespaceState:
    relpath = f"{STATE_DIRNAME}/{STATE_FILENAME}"
    try:
        if issue_root.lstat(relpath) is None:
            return NamespaceState(StateStatus.ABSENT)
        with issue_root.subroot(STATE_DIRNAME) as state_root:
            state = load_state(namespace.state_file, root=state_root)
    except FileNotFoundError:
        # The state directory went away between the lstat and the open.
        return NamespaceState(StateStatus.ABSENT)
    except StateError as exc:
        return NamespaceState(StateStatus.CORRUPT, reason=_bounded_reason(str(exc)))
    return NamespaceState(StateStatus.LOADED, state=state)


def _bounded_reason(text: str) -> str:
    text = redact(text)
    if len(text) <= MAX_STATE_REASON_CHARS:
        return text
    marker = " [... reason truncated]"
    return text[: MAX_STATE_REASON_CHARS - len(marker)] + marker


# -- enumeration ------------------------------------------------------------------------


@dataclass(frozen=True)
class RejectedEntry:
    """An entry of ``issues/`` that is not an issue directory: reported, never followed."""

    name: str
    reason: str


@dataclass(frozen=True)
class NamespaceListing:
    """Every entry of ``<common>/autoforge/scheduler/issues``, each in one place.

    ``bound``: issue directories holding a ``binding.json`` entry, in
    ascending issue number (load each with :func:`load_issue_namespace`);
    ``unbound``: issue directories without one, also ascending;
    ``rejected``: everything else, by name.
    """

    bound: tuple[IssueNamespace, ...]
    unbound: tuple[IssueNamespace, ...]
    rejected: tuple[RejectedEntry, ...]


def list_issue_namespaces(coordination_root: str | Path) -> NamespaceListing:
    """List the namespaces under ``coordination_root``, read-only and bounded.

    More than :data:`MAX_ISSUE_NAMESPACES` entries refuses the listing
    (:class:`NamespaceError`) as soon as the directory read passes the
    bound. An entry whose name is not a canonical decimal issue number (or
    is one past :data:`MAX_ID`), or that is a symbolic link, a regular file
    or any other non-directory, is reported in
    :attr:`NamespaceListing.rejected` and never opened; it never hides the
    other entries. A link in place of ``autoforge``, ``scheduler`` or
    ``issues`` itself raises :class:`~autoforge.safefs.UnsafePathError`. No
    directory is created: a repository without a scheduler tree lists
    nothing.
    """
    root = _coordination_root(coordination_root)
    issues_relpath = "/".join((AUTOFORGE_DIRNAME, SCHEDULER_DIRNAME, ISSUES_DIRNAME))
    with _open_anchor(root) as anchor:
        try:
            issues = anchor.subroot(issues_relpath)
        except FileNotFoundError:
            return NamespaceListing((), (), ())
    bound: list[IssueNamespace] = []
    unbound: list[IssueNamespace] = []
    rejected: list[RejectedEntry] = []
    with issues:
        entries: list[tuple[str, int]] = []
        try:
            for entry in issues.walk(max_entries=MAX_ISSUE_NAMESPACES):
                entry.skip = True  # one level: an issue directory's contents are its own
                entries.append((entry.name, entry.st.st_mode))
        except WalkBudgetExceeded as exc:
            raise NamespaceError(
                f"{root / issues_relpath} holds more than {MAX_ISSUE_NAMESPACES} entries; "
                "refusing to list it"
            ) from exc
        for name, mode in entries:
            if not _ISSUE_DIR_RE.fullmatch(name):
                rejected.append(RejectedEntry(name, "not a decimal issue number"))
                continue
            if int(name) > MAX_ID:
                rejected.append(RejectedEntry(name, f"past the largest issue number {MAX_ID}"))
                continue
            if not stat.S_ISDIR(mode):
                kind = entry_kind(mode) or "regular file"
                rejected.append(RejectedEntry(name, f"a {kind}, not a directory"))
                continue
            namespace = issue_namespace(root, int(name))
            try:
                has_binding = issues.lstat(f"{name}/{BINDING_FILENAME}") is not None
            except StateError as exc:
                # Replaced by a link or made unreadable since it was listed.
                rejected.append(RejectedEntry(name, _bounded_reason(str(exc))))
                continue
            (bound if has_binding else unbound).append(namespace)
    return NamespaceListing(
        bound=tuple(sorted(bound, key=lambda ns: ns.issue_number)),
        unbound=tuple(sorted(unbound, key=lambda ns: ns.issue_number)),
        rejected=tuple(sorted(rejected, key=lambda r: r.name)),
    )


# -- archive ----------------------------------------------------------------------------


def archive_issue_state(namespace: IssueNamespace) -> Path | None:
    """Move the namespace's ``state/state.json`` aside; ``None`` when there is none.

    The state is renamed to ``state.json.archived-<UTC timestamp>`` by
    :func:`autoforge.state.archive_state_file`, through a root on the
    namespace's ``state/`` reached ``O_NOFOLLOW`` from the coordination root:
    the archived name is reserved by ``link(2)`` before ``state.json`` is
    unlinked, so a crash between the two leaves both names and calling this
    again completes the move. With no ``state.json`` (or no state directory)
    nothing is changed and ``None`` is returned. The logs, the worker logs,
    the binding, the claim and the worktree are never touched.

    The caller must hold the issue's L2 (#180) so that no worker writes the
    state while it is moved; ``epic archive`` (#183) closes the claim first
    (ADR 0008 D17.4).
    """
    with _open_anchor(namespace.coordination_root) as anchor:
        try:
            state_root = anchor.subroot("/".join((*namespace.relative, STATE_DIRNAME)))
        except FileNotFoundError:
            return None
    with state_root:
        if state_root.lstat(STATE_FILENAME) is None:
            return None
        return archive_state_file(namespace.state_file, root=state_root)


# -- the standalone commands' refusal ---------------------------------------------------


def inside_scheduler_root(path: str | Path, coordination_root: str | Path) -> bool:
    """Whether ``path`` resolves to the scheduler root or anything below it.

    Decided on resolved paths, so a relative path and one reached through a
    symbolic link are judged by where they lead. Reads nothing but the two
    paths' resolution.
    """
    resolved = Path(os.path.realpath(path))
    root = Path(os.path.realpath(scheduler_root(coordination_root)))
    return resolved == root or root in resolved.parents


def namespace_issue_of_state_dir(
    state_dir: str | Path, coordination_root: str | Path
) -> int | None:
    """The issue whose namespace ``state_dir`` is the state directory of, else ``None``.

    Decided on resolved paths, like :func:`inside_scheduler_root`: only
    ``<scheduler root>/issues/<n>/state`` itself, with ``<n>`` a canonical
    decimal issue number, names a namespace. ``unblock`` acting on that
    directory exempts issue ``<n>`` from the forward guard (ADR 0008 D17.2).
    """
    resolved = Path(os.path.realpath(state_dir))
    issues = Path(os.path.realpath(scheduler_root(coordination_root))) / ISSUES_DIRNAME
    if resolved.name != STATE_DIRNAME or resolved.parent.parent != issues:
        return None
    name = resolved.parent.name
    if not _ISSUE_DIR_RE.fullmatch(name) or int(name) > MAX_ID:
        return None
    return int(name)


def check_standalone_state_dir(
    state_dir: str | Path, coordination_root: str | Path, command: str
) -> None:
    """Refuse a standalone command's state directory inside the scheduler root (D17.3).

    A relative ``--state-dir``, one reached through a symbolic link and the
    scheduler root itself are all refused (:func:`inside_scheduler_root`)
    with :class:`~autoforge.errors.SchedulerStateDirError`, whose message
    names the scheduler commands instead.
    """
    if not inside_scheduler_root(state_dir, coordination_root):
        return
    resolved = Path(os.path.realpath(state_dir))
    root = Path(os.path.realpath(scheduler_root(coordination_root)))
    raise SchedulerStateDirError(
        f"'{command}' refuses state directory {state_dir}: it resolves to {resolved}, inside "
        f"the parallel EPIC scheduler's root {root}, whose per-issue namespaces only the "
        "scheduler runs. Continue a scheduler-owned issue with 'autoforge epic resume' and "
        "inspect it with 'autoforge epic status'; 'autoforge epic archive <epic> <issue>' "
        "releases it to a standalone run. 'status' reads this state directory and 'unblock' "
        "acts on it."
    )


# -- shared -----------------------------------------------------------------------------


def _open_anchor(coordination_root: Path) -> SafeRoot:
    """A root on the coordination root; it is never created (it is the git common dir)."""
    try:
        return SafeRoot.open(coordination_root)
    except FileNotFoundError:
        raise NamespaceError(f"coordination root {coordination_root} does not exist") from None
