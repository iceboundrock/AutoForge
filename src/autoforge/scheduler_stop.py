"""The scheduler's stop request: ``epics/<n>/stop-request`` (ADR 0008 D16; #182).

``autoforge.scheduler-stop/1`` holds ``invocation_id`` (the scheduler
invocation the request is for), ``mode`` (``drain``: admit nothing more and
let every worker return at its next step boundary; ``now``: as a Ctrl-C of
the scheduler) and ``requested_at``. It lives beside ``scheduler.json`` in
``<common>/autoforge/scheduler/epics/<n>/``.

It is the one scheduler file with a second writer that holds no lock, so
the protocol is publication by ``link(2)``:

* :func:`request_stop` (the library function behind ``epic stop``, #183)
  refuses when ``scheduler.json`` is absent and then creates no directory.
  It reads the invocation id from ``scheduler.json``, writes and fsyncs a
  uniquely named temporary in the same directory, links it to
  ``stop-request`` and unlinks the temporary. The file is never replaced,
  so no reader ever sees a partial request; when one exists (``EEXIST``) it
  reports the existing request and writes nothing. After the link it reads
  ``scheduler.json`` again and reports "scheduler changed; stop not
  delivered" when the invocation id is another one.
* Every reader (:func:`read_stop_request`) is lock-free and strict, and
  treats a request that does not parse as a drain, never as a crash; a
  leftover temporary is not a request.
* Only the L0 holder removes anything (:func:`remove_stale_stop_requests`):
  a request bound to another invocation at its start and on every tick, a
  request that does not parse and every leftover temporary at its next
  start.

Nothing here signals a process: the scheduler reads the request and acts.
"""

from __future__ import annotations

import errno
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import NoReturn

from .errors import SchedulerStateError, StateError
from .locking import assert_shared_git_lock_free
from .run_namespace import AUTOFORGE_DIRNAME, SCHEDULER_DIRNAME
from .safefs import ReadLimitExceeded, SafeRoot, entry_kind
from .scheduler_state import EPICS_DIRNAME, load_scheduler_state
from .state import utcnow_iso

STOP_SCHEMA = "autoforge.scheduler-stop/1"
STOP_FILENAME = "stop-request"
# A request is a few hundred bytes; anything past this is not one.
MAX_STOP_BYTES = 4096
MAX_TIMESTAMP_CHARS = 32
# A refusal quotes at most this many characters of what it read.
MAX_PROBLEM_CHARS = 500

_TMP_PREFIX = ".stop-request."
_TMP_SUFFIX = ".tmp"
_TMP_RE = re.compile(r"\.stop-request\.[0-9a-f]{16}\.tmp")
_HEX32 = re.compile(r"[0-9a-f]{32}")
_KEYS = ("schema", "invocation_id", "mode", "requested_at")
_TMP_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC

SCHEDULER_CHANGED = "scheduler changed; stop not delivered"


class StopMode(Enum):
    DRAIN = "drain"
    NOW = "now"


def _fail(message: str) -> NoReturn:
    raise SchedulerStateError(f"stop request {message}")


@dataclass(frozen=True)
class StopRequest:
    """One parsed ``stop-request``; validated on construction."""

    invocation_id: str
    mode: StopMode
    requested_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.invocation_id, str) or not _HEX32.fullmatch(self.invocation_id):
            _fail(f"invocation_id is not an invocation id: {self.invocation_id!r:.80}")
        if not isinstance(self.mode, StopMode):
            _fail(f"mode must be a StopMode, got {type(self.mode).__name__}")
        text = self.requested_at
        if not isinstance(text, str) or len(text) > MAX_TIMESTAMP_CHARS:
            _fail(f"requested_at is not a timestamp: {text!r:.80}")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            _fail(f"requested_at is not a timestamp: {text!r:.80}")
        if parsed.tzinfo is None:
            _fail(f"requested_at has no time zone: {text!r:.80}")

    def to_dict(self) -> dict:
        return {
            "schema": STOP_SCHEMA,
            "invocation_id": self.invocation_id,
            "mode": self.mode.value,
            "requested_at": self.requested_at,
        }

    @classmethod
    def from_dict(cls, raw: object) -> StopRequest:
        """Strictly parse a request: an unknown schema or field is refused, never skipped."""
        if not isinstance(raw, dict):
            _fail(f"must be a JSON object, got {type(raw).__name__}")
        if raw.get("schema") != STOP_SCHEMA:
            _fail(f"has schema {raw.get('schema')!r:.80}; this controller reads {STOP_SCHEMA}")
        if sorted(raw) != sorted(_KEYS):
            unknown = sorted(str(key)[:80] for key in raw if key not in _KEYS)[:5]
            missing = [key for key in _KEYS if key not in raw]
            _fail(f"has the fields {unknown} it may not have and lacks {missing}")
        mode = raw["mode"]
        if mode not in {m.value for m in StopMode}:
            _fail(f"mode is not drain or now: {mode!r:.80}")
        return cls(raw["invocation_id"], StopMode(mode), raw["requested_at"])


@dataclass(frozen=True)
class StopRead:
    """What a reader found at ``stop-request``.

    ``present`` is whether an entry of that name exists; ``request`` is set
    when it parsed and ``problem`` (bounded) when it did not. A request that
    does not parse is read as a drain for every invocation (D16).
    """

    present: bool
    request: StopRequest | None = None
    problem: str = ""

    @property
    def mode(self) -> StopMode | None:
        if self.request is not None:
            return self.request.mode
        return StopMode.DRAIN if self.present else None

    def applies_to(self, invocation_id: str) -> bool:
        """Whether invocation ``invocation_id`` stops on it: its own, or one that does not parse."""
        if not self.present:
            return False
        return self.request is None or self.request.invocation_id == invocation_id

    def bound_elsewhere(self, invocation_id: str) -> bool:
        """A request that parsed and names another invocation than ``invocation_id``."""
        return self.request is not None and self.request.invocation_id != invocation_id


def _relative(epic_number: int) -> str:
    if isinstance(epic_number, bool) or not isinstance(epic_number, int) or epic_number < 1:
        raise SchedulerStateError(f"epic number must be a positive integer, got {epic_number!r}")
    return "/".join((AUTOFORGE_DIRNAME, SCHEDULER_DIRNAME, EPICS_DIRNAME, str(epic_number)))


def stop_request_path(coordination_root: str | Path, epic_number: int) -> Path:
    """``<common>/autoforge/scheduler/epics/<n>/stop-request``."""
    return Path(coordination_root).joinpath(*_relative(epic_number).split("/"), STOP_FILENAME)


def _epic_root(coordination_root: str | Path, epic_number: int) -> SafeRoot | None:
    """The EPIC's directory, never created; ``None`` when it (or a parent) does not exist."""
    try:
        anchor = SafeRoot.open(coordination_root)
    except FileNotFoundError:
        raise SchedulerStateError(f"coordination root {coordination_root} does not exist") from None
    with anchor:
        try:
            return anchor.subroot(_relative(epic_number))
        except FileNotFoundError:
            return None


def _bounded(text: str) -> str:
    return text if len(text) <= MAX_PROBLEM_CHARS else text[: MAX_PROBLEM_CHARS - 3] + "..."


def _unique(pairs: list[tuple[str, object]]) -> dict:
    data: dict = {}
    for key, value in pairs:
        if key in data:
            raise SchedulerStateError(f"has the field {key!r:.80} more than once")
        data[key] = value
    return data


def _no_constant(name: str) -> NoReturn:
    raise SchedulerStateError(f"holds the non-JSON number {name}")


def _read_at(root: SafeRoot, where: str) -> StopRead:
    st = root.lstat(STOP_FILENAME)
    if st is None:
        return StopRead(False)
    kind = entry_kind(st.st_mode)
    if kind is not None:
        return StopRead(True, problem=f"{where} is a {kind}, not a regular file")
    try:
        raw = root.read_bytes(STOP_FILENAME, limit=MAX_STOP_BYTES)
    except ReadLimitExceeded:
        return StopRead(True, problem=f"{where} is larger than {MAX_STOP_BYTES} bytes")
    except (StateError, OSError) as exc:
        return StopRead(True, problem=_bounded(f"{where} cannot be read ({exc})"))
    if raw is None:
        return StopRead(False)  # removed between the lstat and the open
    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_no_constant
        )
        return StopRead(True, StopRequest.from_dict(document))
    except (SchedulerStateError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        return StopRead(True, problem=_bounded(f"{where} does not parse ({exc})"))


def read_stop_request(coordination_root: str | Path, epic_number: int) -> StopRead:
    """EPIC ``epic_number``'s stop request, strictly and lock-free; read-only.

    Never raises for what it finds: a request that does not parse (a link,
    a FIFO, another schema, bad JSON) is ``present`` with a ``problem`` and
    reads as a drain. No directory is created. A link at a directory above
    the file raises :class:`~autoforge.safefs.UnsafePathError` (a
    :class:`~autoforge.errors.StateError`), for the caller to fail closed.
    """
    where = str(stop_request_path(coordination_root, epic_number))
    root = _epic_root(coordination_root, epic_number)
    if root is None:
        return StopRead(False)
    with root:
        return _read_at(root, where)


@dataclass(frozen=True)
class StopDelivery:
    """What :func:`request_stop` did.

    ``written`` is whether this call published a request. ``mode`` is the
    request's mode, or the existing request's when one was there.
    ``scheduler_changed`` is the after-write check: ``scheduler.json`` names
    another invocation than the request, so no live scheduler of the
    request's invocation reads it. ``scheduler_ended`` is whether the
    invocation the request is for had already ended: running workers still
    drain, and ``now`` cannot signal them. ``message`` says it in a line.
    """

    written: bool
    mode: StopMode
    invocation_id: str
    message: str
    existing: StopRead | None = None
    scheduler_changed: bool = False
    scheduler_ended: bool = False


def _write_temporary(root: SafeRoot, payload: bytes) -> str:
    for _ in range(8):
        name = f"{_TMP_PREFIX}{secrets.token_hex(8)}{_TMP_SUFFIX}"
        try:
            fd = os.open(name, _TMP_FLAGS, 0o600, dir_fd=root.fd)
        except FileExistsError:
            continue
        try:
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            _unlink_quietly(root, name)
            raise
        os.close(fd)
        return name
    raise SchedulerStateError(  # pragma: no cover - 64 random bits, eight times
        "cannot name a temporary for the stop request"
    )


def _unlink_quietly(root: SafeRoot, name: str) -> None:
    try:
        os.unlink(name, dir_fd=root.fd)
    except OSError:
        pass


def _fsync_dir(root: SafeRoot) -> None:
    try:
        os.fsync(root.fd)
    except OSError as exc:  # pragma: no cover - fsync of a directory descriptor
        if exc.errno not in (errno.EINVAL, errno.EBADF):
            raise


def request_stop(coordination_root: str | Path, epic_number: int, mode: StopMode) -> StopDelivery:
    """Publish a stop request for EPIC ``epic_number``'s scheduler (D16; see the module).

    Refused (:class:`~autoforge.errors.SchedulerStateError`) when there is
    no ``scheduler.json`` (nothing is created then, no directory either) or
    when it does not load. Otherwise the request is bound to
    ``scheduler.json``'s invocation id and linked into place, or, when a
    request is already there, nothing is written and the existing one is
    reported.
    """
    if not isinstance(mode, StopMode):
        raise SchedulerStateError(f"stop mode must be drain or now, got {mode!r}")
    where = str(stop_request_path(coordination_root, epic_number))
    state = load_scheduler_state(coordination_root, epic_number)
    if state is None:
        raise SchedulerStateError(
            f"no scheduler has run for EPIC #{epic_number} in this repository (no "
            "scheduler.json); there is nothing to stop, and nothing was written"
        )
    invocation = state.invocation
    ended = invocation.ended_at is not None
    request = StopRequest(invocation.invocation_id, mode, utcnow_iso())
    payload = (json.dumps(request.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")
    assert_shared_git_lock_free(f"write {where}")
    root = _epic_root(coordination_root, epic_number)
    if root is None:  # pragma: no cover - scheduler.json was just read there
        raise SchedulerStateError(f"the directory of {where} is gone; nothing was written")
    with root:
        name = _write_temporary(root, payload)
        try:
            os.link(
                name,
                STOP_FILENAME,
                src_dir_fd=root.fd,
                dst_dir_fd=root.fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            existing = _read_at(root, where)
            mode_now = existing.mode or StopMode.DRAIN
            if existing.bound_elsewhere(invocation.invocation_id):
                message = (
                    f"a stop request of an earlier scheduler invocation is still at {where}; "
                    "the scheduler removes it at its next tick: repeat the stop then. "
                    "Nothing was written."
                )
            elif existing.request is None:
                message = (
                    f"a stop request that does not parse is at {where}; every reader takes "
                    "it as a drain. Nothing was written."
                )
            else:
                message = (
                    f"a {mode_now.value} stop request is already there ({where}); nothing "
                    "was written"
                )
            return StopDelivery(
                False,
                mode_now,
                invocation.invocation_id,
                message,
                existing=existing,
                scheduler_ended=ended,
            )
        except OSError as exc:
            raise SchedulerStateError(f"cannot publish {where}: {exc}") from exc
        finally:
            _unlink_quietly(root, name)
        _fsync_dir(root)
    after = load_scheduler_state(coordination_root, epic_number)
    if after is None or after.invocation.invocation_id != invocation.invocation_id:
        return StopDelivery(
            True,
            mode,
            invocation.invocation_id,
            SCHEDULER_CHANGED,
            scheduler_changed=True,
        )
    if ended:
        message = (
            f"{mode.value} stop requested for scheduler invocation "
            f"{invocation.invocation_id}, which has ended: workers still running drain at "
            "their next step boundary"
            + ("; 'now' cannot signal them" if mode is StopMode.NOW else "")
        )
    else:
        message = f"{mode.value} stop requested for scheduler invocation {invocation.invocation_id}"
    return StopDelivery(True, mode, invocation.invocation_id, message, scheduler_ended=ended)


def remove_stale_stop_requests(
    coordination_root: str | Path, epic_number: int, invocation_id: str, *, at_start: bool
) -> tuple[str, ...]:
    """The L0 holder's cleanup of ``stop-request`` (D16); the names it removed.

    A request bound to another invocation than ``invocation_id`` is removed
    always; one that does not parse, and every leftover temporary of an
    interrupted :func:`request_stop`, only ``at_start``. Called by the
    scheduler only, under L0: no other process removes anything here.
    """
    where = str(stop_request_path(coordination_root, epic_number))
    root = _epic_root(coordination_root, epic_number)
    if root is None:
        return ()
    removed: list[str] = []
    with root:
        found = _read_at(root, where)
        if found.present and (
            found.bound_elsewhere(invocation_id) or (at_start and found.request is None)
        ):
            root.unlink(STOP_FILENAME)
            removed.append(STOP_FILENAME)
        if at_start:
            try:
                names = os.listdir(root.fd)
            except OSError as exc:
                raise SchedulerStateError(f"cannot list the directory of {where}: {exc}") from exc
            for name in sorted(names):
                if _TMP_RE.fullmatch(name):
                    _unlink_quietly(root, name)
                    removed.append(name)
    return tuple(removed)
