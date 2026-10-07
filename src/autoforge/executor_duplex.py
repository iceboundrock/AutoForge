"""Duplex child handle: a subprocess the controller writes to while it runs.

:func:`autoforge.executor.execute` is one-shot: the child's stdin is
``/dev/null``, or a payload fixed before the spawn followed by EOF
(``ExecutionRequest.stdin_data``), and nothing the child writes back changes
what it is sent. An RPC transport needs a child it can keep writing records
to while reading records back. This module is the narrowest primitive that
does that and keeps every guarantee ``execute()`` gives (ADR 0002: nothing
the child starts outlives the invocation). It deals in bytes and LF-framed
records only: nothing here knows an encoding, a provider, a phase or
controller state; decoding a record is the caller's.

Guarantees:

- the spawn is ``execute()``'s: an argv list, ``cwd``, the allow-listed
  environment plus ``env``, a session (and so a process group) of its own,
  the terminal never inherited, a spawn failure an :class:`ExecutionError`,
  and so is a setup failure after the spawn (the group is killed first and
  nothing taken for the invocation is kept);
- stdout is split into records on LF (0x0A) **only**, one trailing CR
  stripped per record; no other byte or Unicode line separator (U+2028,
  U+2029, NUL, VT, FF, NEL) is a boundary. A record longer than
  ``max_record_bytes`` is reported as :class:`Oversize` and dropped through
  its LF, and an unterminated fragment at EOF is reported as
  :class:`Fragment`, never as a record;
- stdout and stderr are drained continuously on reader threads, whether or
  not the caller reads, so the child never blocks on a full pipe. Complete
  records wait in a queue bounded by ``max_pending_records`` and
  ``max_pending_bytes``; exceeding it is :class:`Overflow`, which ends the
  invocation. stderr goes to a bounded head/tail capture and never becomes
  a record. Memory is bounded by those limits plus a constant;
- writes are whole records, serialized by one lock and non-blocking under a
  selector, so a child that stops reading stdin cannot hold the controller
  past the deadline; a child that closed its stdin raises
  :class:`ChildStdinClosedError`;
- one deadline bounds every :meth:`DuplexChild.send_line`,
  :meth:`DuplexChild.read_line` and :meth:`DuplexChild.finish`: the
  ``execute()`` limits (#193), an idle limit that every chunk the child
  writes on stdout or stderr restarts (``idle_timeout_seconds``) and a
  wall-clock ceiling from the spawn (``max_runtime_seconds``, or
  :data:`MAX_DEADLINE_SECONDS` when unset), whichever is due first. A
  record written to the child is not activity. Past the deadline the whole
  group is killed exactly as ``execute()`` kills it on timeout, and the
  result is ``timed_out`` and names the limit; except that, as in
  ``execute()``, the child is looked at first, and a ``read_line`` or
  ``finish()`` that meets the deadline after the child has exited settles
  the group with the exit grace instead, the child's status kept. A
  caller that winds a child down in order once a limit falls due asks for
  ``wind_down_seconds``: the limit still falls due on time
  (:meth:`DuplexChild.limit_due`) and the deadline, the kill, comes that
  much later. :meth:`DuplexChild.pin_deadline` stops output from moving
  either, for a caller winding the child down;
- EOF is not the child's exit: a descendant holding stdout keeps EOF away,
  and one that keeps writing keeps every read busy.
  :meth:`DuplexChild.exited` says whether the child itself has exited,
  without waiting, so a caller reading records until EOF asks it after
  every read and calls :meth:`DuplexChild.finish` once it has (ADR 0002's
  exit grace, then the kill, the child's status kept). After a
  ``finish()`` that did not time out, :meth:`DuplexChild.read_line`
  returns the records still queued and then the end item, never waiting;
- teardown runs on every exit from the ``with`` block, an exception or
  ``KeyboardInterrupt`` included: stdin is closed, the child is given a
  bounded wait to exit, then the ADR 0002 exit grace and empty-group check,
  then the group kill if anything is left. The leftover facts
  (``descendants_killed``, ``group_survived_kill``, ``capture_abandoned``)
  mean what they mean in :class:`autoforge.executor.ExecutionResult`. With a
  hostile child the whole invocation takes at most the deadline (the idle
  limit after the last output, or the ceiling, plus any wind-down) plus the
  exit grace plus two kill graces, the bound ``execute()`` has;
- ``contain_orphans`` catches what left the group exactly as in
  ``execute()`` (the subreaper is held from before the spawn to the end of
  the teardown), with the same three orphan facts.

A server child that takes no input and is not spoken to over stdout (#126)
uses the same handle with ``stdin_pipe=False`` and
``stdout_mode=StdoutMode.CAPTURE``.
"""

from __future__ import annotations

import enum
import os
import selectors
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from types import TracebackType
from typing import IO

from . import executor
from .errors import ChildStdinClosedError, ExecutionError, ExecutionTimeoutError
from .executor import (
    DEFAULT_MAX_OUTPUT_BYTES,
    LIMIT_IDLE,
    MAX_DEADLINE_SECONDS,
    _BoundedReader,
    _child_environment,
    _Containment,
    _eof,
    _group_gone,
    _Limits,
    _now,
    _PipeDrain,
    _reaped,
    _spawn,
    _terminate_group,
    _Termination,
    check_limit,
    describe_leftovers,
)

# A single record above this is not a protocol message but a runaway; the
# bound is per record, the queue below bounds what waits to be read.
DEFAULT_MAX_RECORD_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_PENDING_RECORDS = 1024
DEFAULT_MAX_PENDING_BYTES = 16 * 1024 * 1024


class StdoutMode(enum.Enum):
    """What the handle does with the child's stdout."""

    # LF-framed records, read with :meth:`DuplexChild.read_line`.
    RECORDS = "records"
    # Bounded head/tail capture, returned in :attr:`DuplexResult.stdout`.
    CAPTURE = "capture"


@dataclass(frozen=True)
class Record:
    """One complete record: the bytes before an LF, one trailing CR stripped."""

    data: bytes


@dataclass(frozen=True)
class Oversize:
    """A record exceeded ``limit`` bytes; it was dropped through its LF."""

    limit: int


@dataclass(frozen=True)
class Fragment:
    """Bytes after the last LF when stdout reached EOF: not a record."""

    data: bytes


@dataclass(frozen=True)
class Eof:
    """stdout ended and every record before it was read. Sticky."""


@dataclass(frozen=True)
class Overflow:
    """The pending-record queue exceeded its bound; records after the ones
    already returned were dropped and the invocation is over. Sticky."""


@dataclass(frozen=True)
class Timeout:
    """No record arrived in time. ``deadline_exceeded``: the invocation's
    deadline passed and the group was killed (sticky); otherwise only the
    ``read_line`` timeout ran out and the child is still running."""

    deadline_exceeded: bool


ReadOutcome = Record | Oversize | Fragment | Eof | Overflow | Timeout


@dataclass
class DuplexRequest:
    command: list[str]
    cwd: str | None = None
    # Layered over the inherited (or allow-listed) environment when given.
    env: dict[str, str] | None = None
    # As :attr:`autoforge.executor.ExecutionRequest.env_allowlist`.
    env_allowlist: tuple[str, ...] | None = None
    # Kill the child once it has written nothing on stdout or stderr for
    # this long (every chunk restarts it); ``None``: no idle limit.
    idle_timeout_seconds: float | None = None
    # The wall-clock ceiling, measured from the spawn; ``None``:
    # :data:`MAX_DEADLINE_SECONDS`. With the idle limit it bounds every call
    # on the handle.
    max_runtime_seconds: float | None = None
    # How long after a limit falls due a child still running is given before
    # its group is killed, for a caller that winds it down in order (Pi's
    # `abort`): the limit falls due on time (:meth:`DuplexChild.limit_due`),
    # the kill comes this much later (:meth:`DuplexChild.deadline`). 0: as
    # the limit falls due.
    wind_down_seconds: float = 0.0
    max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES
    max_pending_records: int = DEFAULT_MAX_PENDING_RECORDS
    max_pending_bytes: int = DEFAULT_MAX_PENDING_BYTES
    max_stderr_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    # False: the child's stdin is ``/dev/null`` and ``send_line`` refuses.
    stdin_pipe: bool = True
    stdout_mode: StdoutMode = StdoutMode.RECORDS
    # The capture bound in ``StdoutMode.CAPTURE``; unused for records.
    max_stdout_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    # As :attr:`autoforge.executor.ExecutionRequest.contain_orphans`.
    contain_orphans: bool = False


@dataclass
class DuplexResult:
    command: list[str]
    cwd: str | None
    # The child's own exit status; -1 on a timeout (as in ``execute()``) and
    # when the controller killed the child before it exited (an overflow, or
    # an exception in the ``with``).
    exit_code: int
    stderr: str
    started_at: str
    finished_at: str
    timed_out: bool = False
    stderr_truncated: bool = False
    # The pending-record queue overflowed (see :class:`Overflow`).
    records_overflowed: bool = False
    # ``StdoutMode.CAPTURE`` only: the bounded capture, as in ``execute()``.
    stdout: str = ""
    stdout_truncated: bool = False
    # The ADR 0002 facts, as in :class:`autoforge.executor.ExecutionResult`.
    descendants_killed: bool = False
    group_survived_kill: bool = False
    capture_abandoned: bool = False
    orphans_killed: bool = False
    orphan_survived_kill: bool = False
    orphans_unchecked: bool = False
    # As in :class:`autoforge.executor.ExecutionResult`: the limit that
    # killed the child when ``timed_out`` (``"idle"`` or ``"max_runtime"``),
    # and when it last wrote anything.
    timeout_limit: str = ""
    last_activity_at: str | None = None
    # The caller ended the invocation with :meth:`DuplexChild.stop` before
    # the child exited; ``exit_code`` is then -1. Never set with ``timed_out``.
    stopped: bool = False

    @property
    def leftovers(self) -> str:
        """What the invocation left behind, for a log line; empty when nothing."""
        return describe_leftovers(
            descendants_killed=self.descendants_killed,
            group_survived_kill=self.group_survived_kill,
            capture_abandoned=self.capture_abandoned,
            orphans_killed=self.orphans_killed,
            orphan_survived_kill=self.orphan_survived_kill,
            orphans_unchecked=self.orphans_unchecked,
        )


class _LineFramer:
    """Split a byte stream into records on LF only, holding at most one record.

    The partial record is held in one ``bytearray`` of at most ``limit + 1``
    bytes (the extra byte is room for the CR an LF ending strips). Past
    that, the record is reported once as :class:`Oversize` the moment it
    crosses the bound and its remaining bytes are counted out up to the
    next LF, so a stream with no LF at all costs the bound, not its length.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._buf = bytearray()
        self._dropping = False

    @property
    def retained(self) -> int:
        return len(self._buf)

    def feed(self, chunk: bytes) -> list[Record | Oversize]:
        out: list[Record | Oversize] = []
        start = 0
        while True:
            lf = chunk.find(b"\n", start)
            if lf < 0:
                self._take(chunk[start:], out)
                return out
            self._take(chunk[start:lf], out)
            if self._dropping:
                self._dropping = False
            else:
                record = bytes(self._buf)
                self._buf.clear()
                if record.endswith(b"\r"):
                    record = record[:-1]
                out.append(Oversize(self._limit) if len(record) > self._limit else Record(record))
            start = lf + 1

    def _take(self, piece: bytes, out: list[Record | Oversize]) -> None:
        if self._dropping or not piece:
            return
        if len(self._buf) + len(piece) > self._limit + 1:
            self._buf.clear()
            self._dropping = True
            out.append(Oversize(self._limit))
            return
        self._buf += piece

    def finish(self) -> list[Fragment]:
        """At EOF: the unterminated tail, if one is held."""
        if self._dropping or not self._buf:
            return []
        fragment = Fragment(bytes(self._buf))
        self._buf.clear()
        return [fragment]


class _RecordQueue:
    """Records waiting to be read, bounded by count and bytes; never blocks a put.

    A put past either bound ends the queue with :class:`Overflow` instead
    of waiting, so the reader thread keeps draining the pipe. The end item
    (:class:`Eof` or :class:`Overflow`) is returned after every queued item,
    and then on every later get.
    """

    def __init__(self, max_items: int, max_bytes: int) -> None:
        self._max_items = max_items
        self._max_bytes = max_bytes
        self._items: deque[Record | Oversize | Fragment] = deque()
        self._bytes = 0
        self._end: Eof | Overflow | None = None
        self._cond = threading.Condition()

    @property
    def ended(self) -> bool:
        with self._cond:
            return self._end is not None

    @property
    def overflowed(self) -> bool:
        with self._cond:
            return isinstance(self._end, Overflow)

    def put(self, item: Record | Oversize | Fragment) -> bool:
        """Queue ``item``; False once the queue has ended (overflow included)."""
        size = 0 if isinstance(item, Oversize) else len(item.data)
        with self._cond:
            if self._end is not None:
                return False
            if len(self._items) >= self._max_items or self._bytes + size > self._max_bytes:
                self._end = Overflow()
                self._cond.notify_all()
                return False
            self._items.append(item)
            self._bytes += size
            self._cond.notify_all()
            return True

    def end(self) -> None:
        with self._cond:
            if self._end is None:
                self._end = Eof()
            self._cond.notify_all()

    def get(self, until: float) -> Record | Oversize | Fragment | Eof | Overflow | None:
        """The next item, or None when ``until`` (``time.monotonic()``) passes first."""
        with self._cond:
            while True:
                if self._items:
                    item = self._items.popleft()
                    if not isinstance(item, Oversize):
                        self._bytes -= len(item.data)
                    return item
                if self._end is not None:
                    return self._end
                remaining = until - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)


class _RecordReader(_PipeDrain):
    """Drain stdout through a :class:`_LineFramer` into a :class:`_RecordQueue`.

    After an overflow the pipe is still drained, and what it carries
    dropped, so the child never blocks on it and the teardown sees EOF the
    way it does for any other child.
    """

    def __init__(
        self,
        stream: IO[bytes],
        max_record_bytes: int,
        queue: _RecordQueue,
        activity: _Limits | None = None,
    ) -> None:
        super().__init__(stream, "stdout", activity)
        self.framer = _LineFramer(max_record_bytes)
        self._queue = queue

    def _feed(self, chunk: bytes) -> None:
        if self._queue.ended:
            return
        for item in self.framer.feed(chunk):
            if not self._queue.put(item):
                return

    def _ended(self, eof: bool) -> None:
        if eof:
            for fragment in self.framer.finish():
                self._queue.put(fragment)
        self._queue.end()


def _validate(req: DuplexRequest) -> None:
    if not req.command:
        raise ExecutionError("empty command")
    check_limit("idle_timeout_seconds", req.idle_timeout_seconds)
    check_limit("max_runtime_seconds", req.max_runtime_seconds)
    if req.wind_down_seconds != 0:
        check_limit("wind_down_seconds", req.wind_down_seconds)
    for name in (
        "max_record_bytes",
        "max_pending_records",
        "max_pending_bytes",
        "max_stderr_bytes",
        "max_stdout_bytes",
    ):
        value = getattr(req, name)
        if value <= 0:
            raise ExecutionError(f"{name} must be > 0, got {value}")
    if req.max_record_bytes > req.max_pending_bytes:
        raise ExecutionError(
            f"max_record_bytes ({req.max_record_bytes}) must not exceed "
            f"max_pending_bytes ({req.max_pending_bytes}): one record would overflow the queue"
        )


def start_duplex(req: DuplexRequest) -> DuplexChild:
    """Spawn the child and start draining it; use the result as a context manager.

    ``with start_duplex(req) as child: ... result = child.finish()``. The
    ``with`` guarantees the teardown whatever happens inside it. A child
    that cannot be spawned, or whose readers cannot be set up, raises
    :class:`ExecutionError`; anything else that fails or interrupts the
    launch after the spawn is raised as itself. Either way the group is
    killed first.
    """
    _validate(req)
    started = _now()
    contained = _Containment.begin() if req.contain_orphans else None
    try:
        proc = _spawn(
            req.command,
            cwd=req.cwd,
            env=_child_environment(req.env_allowlist, req.env),
            stdin=subprocess.PIPE if req.stdin_pipe else subprocess.DEVNULL,
        )
    except BaseException:
        if contained is not None:
            contained.release()
        raise
    if contained is not None:
        contained.child = proc.pid
    # The child is running from here on, and the caller has no handle to
    # tear down until this returns, so every step from here to the return
    # runs under one guard, as in ``execute()``: whatever fails or interrupts it
    # (a thread or descriptor limit, an allocation, Ctrl-C) kills the group
    # and releases the containment and the pipes before the launch fails,
    # never leaving the child, the orphan reaper or the subreaper setting
    # behind.
    child: DuplexChild | None = None
    try:
        try:
            child = DuplexChild(req, proc, started, contained)
            child._start()
        except (OSError, RuntimeError) as exc:
            raise ExecutionError(f"failed to start {' '.join(req.command)}: {exc}") from exc
    except BaseException:
        _abort_start(proc, () if child is None else child._readers, contained)
        raise
    return child


def _abort_start(
    proc: subprocess.Popen[bytes],
    readers: tuple[_PipeDrain, ...],
    contained: _Containment | None,
) -> None:
    """The launch failed after the spawn: kill the group at once and release
    everything taken so far, since no teardown will run."""
    # A reader whose thread never started has nothing to stop.
    started = tuple(reader for reader in readers if reader.ident is not None)
    try:
        _terminate_group(proc.pid, proc, started, contained)
    finally:
        if contained is not None:
            contained.release()
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass  # nothing was written; the fd is closed either way
        for reader in readers:
            reader.close()
        assert proc.stdout is not None and proc.stderr is not None
        proc.stdout.close()
        proc.stderr.close()


class DuplexChild:
    """A running child with a writable stdin and a framed stdout.

    ``send_line`` and ``read_line`` may be called from different threads;
    writes are serialized, so a record is never interleaved with another.
    Every call is bounded by the request's deadline. Made by
    :func:`start_duplex`, which starts it under its launch guard.
    """

    def __init__(
        self,
        req: DuplexRequest,
        proc: subprocess.Popen[bytes],
        started: str,
        contained: _Containment | None = None,
    ) -> None:
        self._req = req
        self._contained = contained
        self._proc = proc
        self._started = started
        ceiling = req.max_runtime_seconds
        if ceiling is None:
            ceiling = MAX_DEADLINE_SECONDS
        self._limits = _Limits(req.idle_timeout_seconds, ceiling)
        self._pgid = proc.pid  # start_new_session: the child leads a group of its own
        self._write_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._stdin_open = proc.stdin is not None
        self._queue: _RecordQueue | None = None
        self._capture: _BoundedReader | None = None
        self._readers: tuple[_PipeDrain, ...] = ()
        # Set once the group has been dealt with: killed, or found empty after
        # the child's own exit.
        self._left: _Termination | None = None
        self._timed_out = False
        self._timeout_limit = ""
        self._stopped = False
        self._child_killed = False
        self._descendants_killed = False
        self._closed = False
        self._result: DuplexResult | None = None

    def _start(self) -> None:
        """Start draining the child's pipes. A reader takes its wake pipe
        only as it starts, and each is in ``_readers`` before it starts, so
        a failure part-way leaves everything taken where
        :func:`start_duplex`'s guard releases it."""
        proc, req = self._proc, self._req
        assert proc.stdout is not None and proc.stderr is not None
        if proc.stdin is not None:
            os.set_blocking(proc.stdin.fileno(), False)
        stdout_reader: _PipeDrain
        if req.stdout_mode is StdoutMode.RECORDS:
            self._queue = _RecordQueue(req.max_pending_records, req.max_pending_bytes)
            stdout_reader = _RecordReader(
                proc.stdout, req.max_record_bytes, self._queue, self._limits
            )
        else:
            self._capture = _BoundedReader(
                proc.stdout, req.max_stdout_bytes, "stdout", activity=self._limits
            )
            stdout_reader = self._capture
        self._readers = (stdout_reader,)
        self._stderr = _BoundedReader(
            proc.stderr, req.max_stderr_bytes, "stderr", activity=self._limits
        )
        self._readers += (self._stderr,)
        for reader in self._readers:
            reader.start()

    @property
    def pid(self) -> int:
        """The child's pid, which is also its process group id."""
        return self._proc.pid

    def limit_due(self) -> float:
        """When a limit falls due unless the child writes first
        (``time.monotonic()``): the idle limit after its last output or the
        ceiling, whichever is due first. Recomputed on every call."""
        due = self._limits.deadline()
        assert due is not None  # the ceiling always applies
        return due

    def deadline(self) -> float:
        """When the invocation is over unless the child writes first
        (``time.monotonic()``): :meth:`limit_due` plus the request's
        ``wind_down_seconds``. Recomputed on every call."""
        return self.limit_due() + self._req.wind_down_seconds

    @property
    def deadline_limit(self) -> str:
        """The limit :meth:`deadline` belongs to: ``"idle"`` or ``"max_runtime"``."""
        return self._limits.limit()

    def pin_deadline(self) -> None:
        """Fix the deadline where it is: output from here on no longer moves it.

        For a caller that has decided to end the invocation (an abort, an
        orderly shutdown) and gives the child a bounded chance to settle
        within the deadline it had; a child that keeps writing meanwhile
        cannot stretch that chance. :meth:`limit_due` is fixed with it.
        Idempotent.
        """
        self._limits.pin()

    def _expired(self) -> bool:
        return time.monotonic() >= self.deadline()

    def __enter__(self) -> DuplexChild:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.finish()
            return
        # An exception (KeyboardInterrupt included) is in flight: the child is
        # given a short chance to exit on stdin EOF and run its own cleanup,
        # never the rest of the deadline, and then the group goes.
        exit_by = min(self.deadline(), time.monotonic() + executor._EXIT_GRACE_SECONDS)
        try:
            self._teardown(exit_by)
        except ExecutionError:
            pass  # the exception in flight is the one to report

    def send_line(self, data: bytes) -> None:
        """Write ``data`` and an LF as one whole record, bounded by the deadline.

        ``data`` must not contain an LF: one call is one record. A child
        that closed its stdin raises :class:`ChildStdinClosedError`; the
        deadline passing mid-write kills the group and raises
        :class:`ExecutionTimeoutError` (the stream is then over, so a partly
        written record is never continued).
        """
        if b"\n" in data:
            raise ExecutionError("a record must not contain LF (0x0A)")
        with self._write_lock:
            if self._timed_out:
                raise self._timeout_error()
            if not self._stdin_open:
                raise ExecutionError("the child's stdin is not open for writing")
            assert self._proc.stdin is not None
            fd = self._proc.stdin.fileno()
            view = memoryview(data + b"\n")
            with selectors.DefaultSelector() as sel:
                sel.register(fd, selectors.EVENT_WRITE)
                while view:
                    remaining = self.deadline() - time.monotonic()
                    if remaining <= 0:
                        self._expire()
                        raise self._timeout_error()
                    if not sel.select(remaining):
                        continue
                    try:
                        written = os.write(fd, view)
                    except BlockingIOError:
                        continue
                    except BrokenPipeError as exc:
                        raise ChildStdinClosedError(
                            f"the child closed its stdin: {' '.join(self._req.command)}"
                        ) from exc
                    view = view[written:]

    def exited(self) -> bool:
        """Whether the child itself has exited; never waits.

        Its group may live on and hold its stdout, so EOF can come long
        after this, or never; and a descendant that keeps writing keeps
        every read busy, so no read going quiet says so either. A caller
        that reads records until EOF asks this after every read, a record
        included, and once it is True calls :meth:`finish`, which gives
        what is left ADR 0002's exit grace and then kills it, keeping the
        child's own status; :meth:`read_line` then returns what stdout
        carried up to its end.
        """
        return self._proc.poll() is not None

    def read_line(self, timeout: float | None = None) -> ReadOutcome:
        """The next stdout outcome, waiting at most ``timeout`` seconds and
        never past the deadline (see :data:`ReadOutcome`). After a
        :meth:`finish` that did not time out it never waits: the records
        still queued, then the end item, whatever the deadline. When the
        deadline comes, a child still running is an overrun; one that has
        exited by then is not, and is settled as :meth:`finish` settles it
        (see :meth:`_deadline_reached`)."""
        if self._queue is None:
            raise ExecutionError("stdout is captured, not framed into records")
        if self._timed_out:
            return Timeout(deadline_exceeded=True)
        if self._closed:
            return self._rest()
        now = time.monotonic()
        give_up = None if timeout is None else now + max(0.0, timeout)
        while True:
            # Output (stderr included) moves the deadline while this waits,
            # so a wait that ends at the deadline it started with looks again.
            deadline = self.deadline()
            if now >= deadline:
                return self._deadline_reached()
            item = self._queue.get(deadline if give_up is None else min(deadline, give_up))
            if item is not None:
                return item
            now = time.monotonic()
            if give_up is not None and now >= give_up and now < self.deadline():
                return Timeout(deadline_exceeded=False)

    def _rest(self) -> ReadOutcome:
        """Torn down: every reader has ended, so the rest of stdout is queued
        up to its end item, and there is nothing left to wait for and no
        deadline left to enforce."""
        assert self._queue is not None
        rest = self._queue.get(time.monotonic())
        assert rest is not None
        return rest

    def _deadline_reached(self) -> ReadOutcome:
        """A read met the deadline. The child is looked at first, as
        ``execute()``'s last look at it is at the deadline: one
        still running overran it, and the group is killed and the read is
        ``Timeout``. One that has exited ran within it, whatever still
        holds its stdout, so the group gets ADR 0002's exit grace and the
        kill exactly as in :meth:`finish`, the child's status is kept, and
        the read returns the rest of stdout."""
        if not self.exited():
            self._expire()
            return Timeout(deadline_exceeded=True)
        self._teardown(time.monotonic())
        return self._rest()

    def close_stdin(self) -> None:
        """Close the child's stdin (its orderly-shutdown request). Idempotent."""
        with self._write_lock:
            if not self._stdin_open:
                return
            self._stdin_open = False
            assert self._proc.stdin is not None
            try:
                self._proc.stdin.close()
            except OSError:
                pass  # nothing was buffered; the fd is closed either way

    def finish(self) -> DuplexResult:
        """Close stdin, wait for the child within the deadline, tear down, report.

        Legal at any time: a running child is asked to stop by stdin EOF and
        given until the deadline; past it the group is killed and the
        result is ``timed_out``. Once the child has exited on its own, ADR
        0002's exit grace applies exactly as in ``execute()``. After an
        :class:`Overflow` a child still running is killed at once. A second
        call returns the same result.
        """
        if self._result is not None:
            return self._result
        self._teardown(None)
        for reader in self._readers:
            if reader.error is not None:
                raise ExecutionError(
                    f"failed to read {reader.name.removeprefix('autoforge-capture-')} of "
                    f"{' '.join(self._req.command)}: {reader.error}"
                ) from reader.error
        err = self._stderr.captured()
        out = self._capture.captured() if self._capture is not None else None
        assert self._left is not None
        self._result = DuplexResult(
            command=list(self._req.command),
            cwd=self._req.cwd,
            exit_code=-1 if self._timed_out or self._child_killed else self._proc.returncode,
            stderr=err.text,
            started_at=self._started,
            finished_at=_now(),
            timed_out=self._timed_out,
            stderr_truncated=err.truncated,
            records_overflowed=self._queue is not None and self._queue.overflowed,
            stdout=out.text if out is not None else "",
            stdout_truncated=out.truncated if out is not None else False,
            descendants_killed=self._descendants_killed,
            group_survived_kill=self._left.group_survived,
            capture_abandoned=self._left.capture_abandoned,
            orphans_killed=self._contained is not None and self._contained.found,
            orphan_survived_kill=self._left.orphan_survived,
            orphans_unchecked=self._req.contain_orphans and self._contained is None,
            timeout_limit=self._timeout_limit,
            last_activity_at=self._limits.last_activity_at(),
            stopped=self._stopped,
        )
        return self._result

    def _timeout_error(self) -> ExecutionTimeoutError:
        if (self._timeout_limit or self._limits.limit()) == LIMIT_IDLE:
            what = f"wrote nothing for {self._limits.idle:g}s"
        else:
            what = f"reached its {self._limits.ceiling:g}s maximum runtime"
        return ExecutionTimeoutError(
            f"command {what} and was killed: {' '.join(self._req.command)}"
        )

    def _mark_timed_out(self) -> None:
        """Record the timeout and the limit it was; caller holds the lock."""
        self._timed_out = True
        self._timeout_limit = self._limits.limit()

    def _kill(self) -> None:
        """Kill the group before the child exited on its own; caller holds the lock."""
        self._child_killed = self._proc.poll() is None
        self._left = _terminate_group(self._pgid, self._proc, self._readers, self._contained)

    def stop(self) -> bool:
        """End the invocation now, for a reason of the caller's (#194's loop
        detector): the group is killed as on a timeout, through the same
        path and within the same bounds, and the result says ``stopped``.
        False, and nothing done, once the child has exited on its own or the
        group has been dealt with (a limit fired first): the caller then
        settles it as usual. The caller follows a stop with :meth:`finish`."""
        with self._lifecycle_lock:
            if self._left is not None or self._proc.poll() is not None:
                return False
            self._stopped = True
            self._kill()
            return True

    def _expire(self) -> None:
        """The deadline passed: kill the group as ``execute()`` does on timeout."""
        with self._lifecycle_lock:
            if self._left is not None:
                return
            self._mark_timed_out()
            self._kill()

    def _teardown(self, exit_by: float | None) -> None:
        """Close stdin, give the child until ``exit_by`` to exit (``None``:
        until the deadline, which output still moves), apply the exit grace
        or kill the group, and release the pipes. Idempotent."""
        self.close_stdin()
        with self._lifecycle_lock:
            try:
                self._settle(exit_by)
            except BaseException:
                # Interrupted (Ctrl-C) before the group was dealt with: kill it
                # and the orphans while the subreaper still catches them, as
                # ``execute()`` does, and only then restore the setting.
                try:
                    if self._left is None:
                        self._kill()
                finally:
                    if self._contained is not None:
                        self._contained.release()
                raise
            if self._closed:
                return
            self._closed = True
            if self._contained is not None:
                self._contained.release()
            for reader in self._readers:
                reader.close()
            assert self._proc.stdout is not None and self._proc.stderr is not None
            self._proc.stdout.close()
            self._proc.stderr.close()

    def _exits_by(self, exit_by: float | None) -> bool:
        """Wait for the child's exit until ``exit_by``, or (``None``) until the
        deadline as output moves it; True when it exited."""
        if exit_by is not None:
            return _reaped(self._proc, exit_by)
        while not _reaped(self._proc, self.deadline()):
            if self._expired():
                return False
        return True

    def _settle(self, exit_by: float | None) -> None:
        """Deal with the group once: the exit grace, or the kill; caller holds the lock."""
        if self._left is None:
            overflowed = self._queue is not None and self._queue.overflowed
            if overflowed and self._proc.poll() is None:
                self._kill()
            elif not self._exits_by(exit_by):
                if self._expired():
                    self._mark_timed_out()
                self._kill()
            else:
                # The child exited on its own: ADR 0002's exit grace, then
                # whatever is still in the group (or holds the pipes) is
                # killed and the child's own status kept.
                grace = time.monotonic() + executor._EXIT_GRACE_SECONDS
                contained = self._contained
                group_settled = _eof(self._readers, grace) and _group_gone(self._pgid, grace)
                if group_settled and (contained is None or contained.settled(grace)):
                    self._left = _Termination(group_survived=False, capture_abandoned=False)
                else:
                    # Only an orphan left over is an orphan kill, not a group one.
                    self._descendants_killed = not group_settled
                    self._left = _terminate_group(self._pgid, self._proc, self._readers, contained)
