"""CLI execution abstraction over subprocess.

Represents every external invocation (Claude Code, OpenCode, gh, git) as a
structured ExecutionRequest -> ExecutionResult.

Safety properties:
- argv lists only, never ``shell=True`` — prompt text containing quotes,
  ``$``, ``;``, ``|`` or ``$(...)`` is passed as one opaque argument;
- stdin is ``/dev/null`` so a CLI that expects an interactive TTY cannot
  hang waiting for input, unless the request carries ``stdin_data``: that
  is written in full by a non-blocking feeder and stdin is then closed, so
  the child sees EOF, a child that never reads costs the controller
  nothing past the invocation, and what the child did not read is counted
  (:class:`_StdinFeeder`, :attr:`ExecutionResult.stdin_unread`);
- the child runs in its own session/process group, and nothing in that
  group outlives the invocation: on timeout the whole group is terminated
  (SIGTERM, then SIGKILL), and after a normal exit a group that still has
  members or pipes still open past a short grace is terminated the same
  way, so grandchildren spawned by an agent (test runners, editors, servers)
  do not linger into the next phase, whether or not they hold the agent's
  pipes. The child's own exit status and output are kept in the second
  case; the result records that descendants were killed. The kill is
  complete only when no process is left in the group, and every wait in it
  is bounded: what survives SIGKILL, or left the group (``setsid``) and so
  holds the pipes out of reach, is abandoned rather than waited for, and the
  result says so (:attr:`ExecutionResult.group_survived_kill`,
  :attr:`ExecutionResult.capture_abandoned`) so an operator looks for the
  leftover instead of assuming the kill was clean;
- capture is bounded: each stream keeps at most ``max_output_bytes`` (the
  first half and the last half of what the child wrote) in a buffer whose
  memory is that bound plus a constant, so a runaway or adversarial child
  costs the controller a bounded amount of memory, not the size of its
  output, however it chunks it. The tail is kept because the CONTROL_RESULT
  block is the last thing on stdout; :attr:`ExecutionResult.stdout_tail` is
  the part known to be contiguous up to EOF, so a caller that parses a
  truncated stream never sees text that spans the cut;
- the environment is allow-listed on request: a request carrying
  ``env_allowlist`` starts the child from only the named variables of the
  controller's environment (:func:`select_environment`) instead of a copy
  of all of it, so an agent or a repository-defined command never sees a
  credential that was in the operator's shell but has nothing to do with
  the run. Which names are allowed is policy and belongs to the caller; the
  executor only applies the selection;
- on request (``contain_orphans``), what left the group is caught too: on
  Linux the controller is a child subreaper for the invocation
  (``prctl(PR_SET_CHILD_SUBREAPER)``), so a process that called ``setsid``
  (or was started detached) and whose parent then died is re-parented to the
  controller instead of to init; the ones that die while the child runs are
  reaped as they die, so they cannot pile up as zombies against the
  session's process limit, and at teardown every orphan still alive is
  killed (SIGTERM, then SIGKILL, its own process group with it) and reported
  (:attr:`ExecutionResult.orphans_killed`,
  :attr:`ExecutionResult.orphan_survived_kill`). Where no subreaper exists
  the result says the check could not be made
  (:attr:`ExecutionResult.orphans_unchecked`). See the ADR 0002 amendment.
"""

from __future__ import annotations

import ctypes
import os
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import IO

from .errors import ExecutionError, ExecutionTimeoutError

_KILL_GRACE_SECONDS = 5.0
# After the child has exited on its own, how long its pipes may stay open
# and its group may keep a member before the group is killed. A child that
# left nothing behind clears both at once; the grace is for a helper the
# child is shutting down as it exits, not for a server it meant to leave.
_EXIT_GRACE_SECONDS = 2.0
_GROUP_POLL_SECONDS = 0.02
_READ_CHUNK_BYTES = 64 * 1024

# Per-stream capture bound. A Claude Code ``-p`` transcript is kilobytes and
# an OpenCode ``run`` transcript at most a few megabytes, so the default is
# well above any legitimate agent output; ``gh``/``git`` plumbing output is
# smaller still. It is a request field rather than configuration because
# nothing legitimate should reach it.
DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024 * 1024

# An environment allow-list entry: a variable name, or a name prefix followed
# by ``*`` (``LC_*``, ``ANTHROPIC_*``). Nothing else -- no other glob
# characters, no empty prefix -- so an entry cannot quietly widen into
# "everything".
ENV_PATTERN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\*?$")


def is_env_pattern(name: str) -> bool:
    """Whether ``name`` is a valid environment allow-list entry."""
    return bool(ENV_PATTERN_RE.match(name))


def select_environment(
    names: Iterable[str], source: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The variables of ``source`` (default: this process's environment) named by ``names``.

    An entry is an exact variable name or a prefix ending in ``*``; the
    result is the subset of ``source`` some entry names, in ``source``'s
    order, and a name that is absent from ``source`` is simply not there
    (the child gets no empty placeholder). An entry that is not a valid
    pattern raises :class:`ExecutionError`, so a typo in a configured list
    is a refusal to launch, never a silently narrower or wider environment.
    """
    env = os.environ if source is None else source
    exact: set[str] = set()
    prefixes: list[str] = []
    for name in names:
        if not is_env_pattern(name):
            raise ExecutionError(
                f"invalid environment allow-list entry {name!r}: expected a variable name "
                "or a name prefix followed by '*'"
            )
        if name.endswith("*"):
            prefixes.append(name[:-1])
        else:
            exact.add(name)
    return {
        key: value
        for key, value in env.items()
        if key in exact or any(key.startswith(prefix) for prefix in prefixes)
    }


@dataclass
class ExecutionRequest:
    command: list[str]
    cwd: str | None = None
    # Layered over the inherited (or allow-listed) environment when given.
    env: dict[str, str] | None = None
    timeout_seconds: int = 1800
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    # ``None``: the child inherits the controller's whole environment (the
    # controller's own git/gh plumbing). A tuple of names/prefixes: the child
    # starts from only those variables (see :func:`select_environment`); an
    # empty tuple is an empty environment.
    env_allowlist: tuple[str, ...] | None = None
    # Catch and kill what the child started outside its process group (see
    # :class:`_Containment`). For agents and repository-defined commands; the
    # controller's own git/gh plumbing leaves it off, so a ``git gc --auto``
    # that git daemonized on purpose is left alone.
    contain_orphans: bool = False
    # Written to the child's stdin, which is then closed; ``None`` keeps
    # stdin on ``/dev/null``. For a CLI whose argv cannot carry a payload
    # verbatim (the OpenCode v2 message). Bounded by the timeout, not by size.
    stdin_data: bytes | None = None
    # Called on the reader thread with the stream's name (``"stdout"`` or
    # ``"stderr"``) each time a chunk of it arrives: a liveness signal for an
    # agent that streams nothing parseable (#192). It never sees the bytes,
    # so what the child wrote stays in the bounded capture alone. Calls from
    # the two streams are serialized; one that raises is not called again
    # for this invocation, on either stream, and both drains go on.
    on_output: Callable[[str], None] | None = None


@dataclass
class ExecutionResult:
    command: list[str]
    cwd: str | None
    exit_code: int
    stdout: str
    stderr: str
    started_at: str
    finished_at: str
    timed_out: bool = False
    # True when the stream exceeded the capture bound: ``stdout``/``stderr``
    # then hold its head, an omission marker and its tail.
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    # Index into ``stdout`` where the contiguous-to-EOF tail begins (0 when
    # nothing was omitted, so ``stdout_tail`` is then the whole stream).
    stdout_tail_offset: int = 0
    # The child exited on its own but left processes behind (its pipes were
    # still open or its group still had a member past the exit grace), so
    # the group was killed; ``exit_code`` and the streams are the child's own.
    # Never set together with ``timed_out``.
    descendants_killed: bool = False
    # A process was still in the group after the SIGKILL grace (stuck in the
    # kernel, or not signallable from here): the kill was not clean and a
    # leftover may still be running.
    group_survived_kill: bool = False
    # A pipe never reached EOF: a writer the group kill could not reach
    # (one that left the group) still holds it; what was read before the
    # capture was abandoned is what the streams contain.
    capture_abandoned: bool = False
    # ``contain_orphans`` only. A process the invocation started had left the
    # group and outlived its parent (re-parented to the controller), so it
    # was killed; set on any path, a timeout included.
    orphans_killed: bool = False
    # Such an orphan was still alive after the SIGKILL grace.
    orphan_survived_kill: bool = False
    # Containment was requested but this platform has no child subreaper, so
    # a process that left the group was neither seen nor killed.
    orphans_unchecked: bool = False
    # Bytes of ``stdin_data`` the child never read: never written because
    # the invocation ended first, or written and still in the pipe when it
    # ended. Always the payload's tail, since the child reads a prefix. Read
    # by any process holding the child's stdin counts as read; whether the
    # child then used what it read is beyond what the executor can see.
    stdin_unread: int = 0

    @property
    def stdout_tail(self) -> str:
        """The part of stdout captured contiguously up to EOF.

        This is what a CONTROL_RESULT parser should search: a block found
        here is one the child actually wrote whole, never one assembled
        across the omission marker or a stale earlier block from the head.
        """
        return self.stdout[self.stdout_tail_offset :]

    @property
    def truncated(self) -> bool:
        return self.stdout_truncated or self.stderr_truncated

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

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.exit_code == 0 and not self.truncated

    def raise_if_failed(self) -> ExecutionResult:
        if self.timed_out:
            raise ExecutionTimeoutError(f"command timed out: {' '.join(self.command)}")
        if self.exit_code != 0:
            raise ExecutionError(
                f"command exited {self.exit_code}: {' '.join(self.command)}\n"
                f"stderr (tail): {self.stderr[-2000:]}"
            )
        if self.truncated:
            raise ExecutionError(
                f"command output was truncated at the capture bound: {' '.join(self.command)}"
            )
        return self


def describe_leftovers(
    *,
    descendants_killed: bool,
    group_survived_kill: bool,
    capture_abandoned: bool,
    orphans_killed: bool = False,
    orphan_survived_kill: bool = False,
    orphans_unchecked: bool = False,
) -> str:
    """One sentence naming what an invocation left behind; empty when nothing.

    The facts are the executor's (see :class:`ExecutionResult`); the
    sentence is for a run log or an error, so an operator is pointed at a
    leftover process rather than at a slow agent or a clean kill.
    """
    parts: list[str] = []
    if descendants_killed:
        parts.append(
            "the child exited but left processes behind (its output pipes stayed open or "
            "its process group still had a member past the exit grace), so the group was killed"
        )
    if group_survived_kill:
        parts.append(
            "its process group still had a member after SIGKILL, so a leftover process "
            "may still be running"
        )
    if capture_abandoned:
        parts.append(
            "its output pipes never reached EOF, so a process outside its group (one that "
            "called setsid) still holds them and may still be running"
        )
    if orphans_killed:
        parts.append(
            "it left processes outside its process group (re-parented to the controller), "
            "so they were killed"
        )
    if orphan_survived_kill:
        parts.append(
            "a process it left outside its process group was still alive after SIGKILL, so "
            "it may still be running"
        )
    if orphans_unchecked:
        parts.append(
            "processes it may have left outside its process group could not be checked on "
            "this platform (no child subreaper), so one may still be running"
        )
    return "; ".join(parts)


@dataclass
class _Captured:
    text: str
    truncated: bool
    tail_offset: int


class _BoundedBuffer:
    """Keep at most ``limit`` bytes of a stream: its head and its tail.

    The first ``limit - limit // 2`` bytes are the head and the last
    ``limit // 2`` the tail; everything between is counted and dropped. The
    tail is a ring buffer (one ``bytearray``, filled once and then
    overwritten in place), so the memory the buffer holds is the limit plus a
    constant, whatever the stream's chunking: a writer that produces one byte
    per read costs no per-chunk objects. The tail is trimmed to the byte, so
    what is retained never exceeds ``limit``, and a bound smaller than one
    read is honoured too.
    """

    def __init__(self, limit: int, name: str) -> None:
        self._name = name
        self._limit = limit
        self._head_limit = limit - limit // 2
        self._tail_limit = limit // 2
        self._head = bytearray()
        # The ring grows by appending until it holds ``_tail_limit`` bytes and
        # is overwritten in place from ``_pos`` after that; ``_pos`` is the
        # oldest byte once the ring is full and 0 before.
        self._ring = bytearray()
        self._pos = 0
        self._total = 0

    @property
    def retained(self) -> int:
        """Bytes currently held; never more than the limit."""
        return len(self._head) + len(self._ring)

    def feed(self, chunk: bytes) -> None:
        self._total += len(chunk)
        room = self._head_limit - len(self._head)
        if room > 0:
            self._head += chunk[:room]
            chunk = chunk[room:]
        size = self._tail_limit
        if not chunk or size == 0:
            return
        if len(self._ring) < size:
            fill = size - len(self._ring)
            self._ring += chunk[:fill]
            chunk = chunk[fill:]
            if not chunk:
                return
        if len(chunk) >= size:
            self._ring[:] = chunk[len(chunk) - size :]
            self._pos = 0
            return
        end = self._pos + len(chunk)
        if end <= size:
            self._ring[self._pos : end] = chunk
        else:
            split = size - self._pos
            self._ring[self._pos :] = chunk[:split]
            self._ring[: len(chunk) - split] = chunk[split:]
        self._pos = end % size

    def _tail(self) -> bytes:
        return bytes(self._ring[self._pos :] + self._ring[: self._pos])

    def captured(self) -> _Captured:
        tail = self._tail()
        omitted = self._total - len(self._head) - len(tail)
        if omitted <= 0:
            # Nothing was dropped, so the split between head and tail is an
            # internal detail: decode the stream whole, or a multi-byte
            # character that straddles it would come out as replacements.
            return _Captured((self._head + tail).decode("utf-8", errors="replace"), False, 0)
        # Past the bound the head and the tail are separate pieces of the
        # stream; a character cut by the bound decodes to replacements on
        # either side of the marker, which is what was captured.
        head_text = self._head.decode("utf-8", errors="replace")
        tail_text = tail.decode("utf-8", errors="replace")
        marker = (
            f"\n[autoforge: {omitted} bytes of {self._name} omitted; the capture bound is "
            f"{self._limit} bytes, the first {len(self._head)} and last {len(tail)} "
            "bytes were kept]\n"
        )
        return _Captured(head_text + marker + tail_text, True, len(head_text) + len(marker))


class _PipeDrain(threading.Thread):
    """Drain one pipe until EOF or abandoned, handing each chunk to :meth:`_feed`.

    The pipe is drained whatever the child writes, so the child never blocks
    on a full pipe the way it would if the controller stopped reading. EOF
    arrives only once every writer is gone; :meth:`abandon` ends the read
    without it, for a writer nothing here can kill. What a chunk becomes
    (bounded capture here, LF-framed records in ``executor_duplex``) is the
    subclass's.

    Building a reader takes nothing that must be given back: the wake pipe
    is opened by :meth:`start` and released by :meth:`close`. A launch holds
    each reader before it starts it, so its cleanup reaches every pipe
    taken, and a reader that fails while it is built (its buffer cannot be
    allocated, say) holds nothing for it to reach.
    """

    def __init__(self, stream: IO[bytes], name: str) -> None:
        super().__init__(name=f"autoforge-capture-{name}", daemon=True)
        self._fd = stream.fileno()
        # (read end, write end): opened by :meth:`start`, closed by :meth:`close`.
        self._wake: tuple[int, int] | None = None
        self.error: OSError | None = None

    def start(self) -> None:
        """Open the wake pipe and start the thread. A thread that cannot be
        started leaves the pipe open for :meth:`close`."""
        self._wake = os.pipe()
        super().start()

    def run(self) -> None:
        assert self._wake is not None  # opened by start() before the thread began
        wake_r = self._wake[0]
        eof = False
        try:
            with selectors.DefaultSelector() as sel:
                sel.register(self._fd, selectors.EVENT_READ)
                sel.register(wake_r, selectors.EVENT_READ)
                while True:
                    ready = {key.fd for key, _ in sel.select()}
                    if wake_r in ready:
                        return
                    chunk = os.read(self._fd, _READ_CHUNK_BYTES)
                    if not chunk:
                        eof = True
                        return
                    self._feed(chunk)
        except OSError as exc:
            self.error = exc
        finally:
            self._ended(eof)

    def _feed(self, chunk: bytes) -> None:
        raise NotImplementedError

    def _ended(self, eof: bool) -> None:
        """Called once on the reader thread when the read ends; ``eof`` is
        False when it was abandoned or failed."""

    def wait(self, deadline: float | None) -> bool:
        """Join until EOF or ``deadline`` (``time.monotonic()``); True on EOF."""
        self.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
        return not self.is_alive()

    def abandon(self) -> None:
        """Stop reading without EOF; what was read so far is what is captured."""
        assert self._wake is not None  # only a started reader is abandoned
        os.write(self._wake[1], b"\0")
        self.join()

    def close(self) -> None:
        """Release the wake pipe, if :meth:`start` opened one. Idempotent."""
        if self._wake is None:
            return
        for fd in self._wake:
            try:
                os.close(fd)
            except OSError:
                pass
        self._wake = None


class _OutputObserver:
    """:attr:`ExecutionRequest.on_output`, shared by both readers of one
    invocation.

    Calls are serialized, and the first exception the observer raises
    unhooks it for both streams: a call the other reader was waiting to make
    finds it gone, so a failing observer is called once and can never stop
    either drain.
    """

    def __init__(self, on_output: Callable[[str], None]) -> None:
        self._on_output: Callable[[str], None] | None = on_output
        self._lock = threading.Lock()

    def __call__(self, stream: str) -> None:
        with self._lock:
            if self._on_output is None:
                return
            try:
                self._on_output(stream)
            except Exception:
                self._on_output = None


class _BoundedReader(_PipeDrain):
    """Drain one pipe into a :class:`_BoundedBuffer` until EOF or abandoned.

    ``observer``, when given, is told the stream's name after each chunk.
    """

    def __init__(
        self,
        stream: IO[bytes],
        limit: int,
        name: str,
        observer: _OutputObserver | None = None,
    ) -> None:
        super().__init__(stream, name)
        self.buffer = _BoundedBuffer(limit, name)
        self._stream_name = name
        self._observer = observer

    def _feed(self, chunk: bytes) -> None:
        self.buffer.feed(chunk)
        if self._observer is not None:
            self._observer(self._stream_name)

    def captured(self) -> _Captured:
        """Decode what was kept; call only once the thread has ended."""
        return self.buffer.captured()


class _StdinFeeder(threading.Thread):
    """Write ``data`` to the child's stdin, close it so the child sees EOF,
    and count what the child never read.

    The child's stdin is :attr:`child_end`, the read end of a pipe the
    feeder owns. The feeder holds that read end too, until :meth:`stop`:
    written is not read, and a pipe discards what is in it once its last
    reader is gone, so only a read end kept past the child's exit can show
    that the child left part of the payload unread (:attr:`unread`). With a
    reader always present a write never fails with EPIPE: a child that
    stops reading leaves the pipe full and the thread parked.

    The write end is non-blocking and the thread waits on it and on a wake
    pipe, so a child that never reads its stdin (or a descendant that holds
    the pipe and never reads) costs a parked thread that :meth:`stop` ends
    at once, never a hung invocation.
    """

    def __init__(self, data: bytes) -> None:
        super().__init__(name="autoforge-feed-stdin", daemon=True)
        self._data = data
        self.child_end, self._write_end = os.pipe()
        try:
            self._wake_r, self._wake_w = os.pipe()
        except BaseException:
            os.close(self.child_end)
            os.close(self._write_end)
            raise
        self._unwritten = len(data)
        # Final once :meth:`stop` has returned.
        self.unread = len(data)
        self.error: OSError | None = None

    def run(self) -> None:
        view = memoryview(self._data)
        try:
            os.set_blocking(self._write_end, False)
            with selectors.DefaultSelector() as sel:
                sel.register(self._write_end, selectors.EVENT_WRITE)
                sel.register(self._wake_r, selectors.EVENT_READ)
                while view:
                    ready = {key.fd for key, _ in sel.select()}
                    if self._wake_r in ready:
                        return
                    try:
                        view = view[os.write(self._write_end, view) :]
                    except BlockingIOError:
                        continue
        except OSError as exc:
            self.error = exc
        finally:
            self._unwritten = len(view)
            os.close(self._write_end)

    def stop(self) -> None:
        """End the feed if it is still writing, wait for the thread, count
        what was left unread and release the pipes.

        Called once the invocation is over (or its spawn failed, in which
        case the thread never started and nothing was written).
        """
        if self.ident is None:
            os.close(self._write_end)
        else:
            os.write(self._wake_w, b"\0")
            self.join()
        written = len(self._data) - self._unwritten
        self.unread = self._unwritten + _drain(self.child_end, written)
        for fd in (self.child_end, self._wake_r, self._wake_w):
            os.close(fd)


def _drain(fd: int, limit: int) -> int:
    """Read and count what is left in the pipe ``fd`` reads from, at most
    ``limit`` bytes (all that was ever written to it).

    Every write end is closed by then, so the pipe reads to EOF; the
    descriptor is non-blocking anyway, so a writer that is somehow still
    there ends the count rather than hanging it.
    """
    os.set_blocking(fd, False)
    count = 0
    while count < limit:
        try:
            chunk = os.read(fd, min(limit - count, 1 << 16))
        except BlockingIOError:
            break
        if not chunk:
            break
        count += len(chunk)
    return count


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _signal_group(pgid: int, sig: signal.Signals) -> bool:
    """Signal a process group; False when no process is left in it."""
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    return True


def _reaped(proc: subprocess.Popen, deadline: float) -> bool:
    try:
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return False
    return True


def _eof(readers: tuple[_PipeDrain, ...], deadline: float | None) -> bool:
    return all(reader.wait(deadline) for reader in readers)


def _group_alive(pgid: int) -> bool:
    """True while any process still belongs to the group.

    The group id stays reserved while a member lives, however the member was
    started and wherever its output goes; ``EPERM`` names a member that
    cannot be signalled from here, which is still a member.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _group_gone(pgid: int, deadline: float) -> bool:
    """Poll until the group has no member left or ``deadline``; True when gone."""
    while _group_alive(pgid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(_GROUP_POLL_SECONDS)
    return True


# prctl(2) options; Linux only.
_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37
# How often a contained invocation reaps the orphans that died while its
# child runs. Each zombie holds a pid against the session's pids.max until it
# is reaped, so this bounds the backlog to what the child's tools can orphan
# in one interval.
_REAP_INTERVAL_SECONDS = 0.25

# One contained invocation at a time per controller process: the orphans of
# an invocation are told apart from the controller's other children by being
# new, which holds only while nothing else spawns meanwhile. The engine runs
# its invocations one after another, so this never waits; a second one is a
# refusal, not a queue.
_CONTAINMENT_LOCK = threading.Lock()


def _prctl() -> Callable[..., int] | None:
    """libc's ``prctl``, or None where there is none (not Linux)."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        return ctypes.CDLL(None, use_errno=True).prctl  # type: ignore[no-any-return]
    except (OSError, AttributeError):
        return None


def _children() -> dict[tuple[int, int], bool]:
    """This process's children as ``(pid, start time) -> is a zombie``.

    The start time (``/proc/<pid>/stat`` field 22) makes the key unique across
    a pid reuse. Children are listed through ``/proc/self/task/*/children``
    when the kernel provides it and by a scan of ``/proc`` otherwise; a
    process that is gone by the time it is read is simply not listed.
    """
    own = os.getpid()
    pids: set[int] = set()
    try:
        for task in os.scandir(f"/proc/{own}/task"):
            with open(f"{task.path}/children") as fh:
                pids.update(int(pid) for pid in fh.read().split())
        scan = False
    except OSError:
        scan = True
    if scan:
        pids = {int(entry.name) for entry in os.scandir("/proc") if entry.name.isdigit()}
    found: dict[tuple[int, int], bool] = {}
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat", "rb") as fh:
                stat = fh.read()
        except OSError:
            continue
        # ``pid (comm) state ppid ...``: comm may hold spaces and parentheses,
        # so the fields are counted from the last ``)``.
        fields = stat[stat.rfind(b")") + 2 :].split()
        if len(fields) < 20 or int(fields[1]) != own:
            continue
        found[(pid, int(fields[19]))] = fields[0] == b"Z"
    return found


class _Containment:
    """Catch the orphans of one invocation (the ADR 0002 amendment, #132).

    A process that calls ``setsid`` (or is spawned detached, as Pi's bash tool
    spawns every command) leaves the child's process group, and the group kill
    cannot reach it. On Linux the controller marks itself a child subreaper
    for the invocation, so when such a process's parent dies it is
    re-parented to the controller rather than to init: the orphans are then
    exactly the controller's children that were not there before the
    invocation and are not the child itself. Being the controller's unreaped
    children, their pids cannot be reused, so signalling them is safe.

    Each orphan is signalled with its process group (unless that is the
    controller's own), which reaches what it started in turn; whatever is
    re-parented as its parents die is signalled when it appears. Dead orphans
    are reaped here, since nothing else waits for them, and from the start:
    a reaper thread reaps them every ``_REAP_INTERVAL_SECONDS`` while the
    child runs. An agent's tools orphan short-lived processes all along (a
    test suite's helpers, a shell's background jobs), and every zombie
    holds a pid against the session's ``pids.max``, so over an hour-long
    run they pile up until no fork in the session succeeds and the agent
    itself aborts. The reaper never reaps the child (its exit status
    belongs to its ``Popen``) or a child the controller had before. The
    first look at the orphans after the child's run (the exit grace, the
    kill) stops it, so from there the caller's own polling reaps and
    signals alone. :meth:`release` restores the previous subreaper setting;
    an orphan still alive then stays the controller's child, and is
    reported, not hidden.
    """

    def __init__(self, prctl: Callable[..., int], previous: int) -> None:
        self._prctl = prctl
        self._previous = previous
        self._before = frozenset(_children())
        self.child: int | None = None
        self.found = False
        self._signalled: dict[signal.Signals, set[tuple[int, int]]] = {}
        self._released = False
        self._stop_reaper = threading.Event()
        self._reaper = threading.Thread(
            target=self._reap_until_stopped, name="autoforge-reaper", daemon=True
        )

    @classmethod
    def begin(cls) -> _Containment | None:
        """Start containing; None when this platform has no child subreaper.

        Raises :class:`ExecutionError` when another contained invocation is
        running in this process, or the reaper thread cannot be started
        (nothing has been spawned yet, so nothing is left behind).
        """
        prctl = _prctl()
        if prctl is None:
            return None
        if not _CONTAINMENT_LOCK.acquire(blocking=False):
            raise ExecutionError(
                "another orphan-contained invocation is already running in this process"
            )
        try:
            previous = ctypes.c_int(0)
            if prctl(_PR_GET_CHILD_SUBREAPER, ctypes.byref(previous), 0, 0, 0) != 0:
                _CONTAINMENT_LOCK.release()
                return None
            contained = cls(prctl, previous.value)
            if prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
                _CONTAINMENT_LOCK.release()
                return None
        except BaseException:
            _CONTAINMENT_LOCK.release()
            raise
        try:
            contained._reaper.start()
        except BaseException as exc:
            contained.release()
            if isinstance(exc, RuntimeError):
                raise ExecutionError(f"failed to start the orphan reaper: {exc}") from exc
            raise
        return contained

    def _reap_until_stopped(self) -> None:
        """The reaper thread: reap the dead orphans until told to stop."""
        while not self._stop_reaper.wait(_REAP_INTERVAL_SECONDS):
            child = self.child
            if child is None:
                continue  # not spawned yet: a zombie now could be the child itself
            try:
                found = _children()
            except OSError:
                continue  # /proc unreadable for a moment; the next pass looks again
            for key, zombie in found.items():
                if zombie and key not in self._before and key[0] != child:
                    try:
                        os.waitpid(key[0], os.WNOHANG)
                    except ChildProcessError:
                        pass

    def _stop_reaping(self) -> None:
        """Stop the reaper thread and wait for it; from here the caller reaps.

        Only the thread that signals the orphans reaps them from then on, so
        an orphan it is about to signal cannot be reaped, and its pid reused,
        in between.
        """
        self._stop_reaper.set()
        if self._reaper.ident is not None:
            self._reaper.join()

    def _live(self) -> set[tuple[int, int]]:
        """The orphans still alive; the dead ones are reaped on the way.

        A process still in the child's group was re-parented here too when
        its parent died, but it is the group kill's, not an orphan: it is
        reaped once dead and otherwise left to the group checks. The first
        call stops the reaper thread.
        """
        self._stop_reaping()
        live: set[tuple[int, int]] = set()
        for key, zombie in _children().items():
            pid = key[0]
            if key in self._before or pid == self.child:
                continue
            if zombie:
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
                for sent in self._signalled.values():
                    sent.discard(key)
                continue
            try:
                in_group = os.getpgid(pid) == self.child
            except OSError:
                continue  # gone meanwhile; reaped on the next look
            if not in_group:
                live.add(key)
        return live

    def _send(self, key: tuple[int, int], sig: signal.Signals) -> None:
        pid = key[0]
        self._signalled.setdefault(sig, set()).add(key)
        self.found = True
        try:
            pgid = os.getpgid(pid)
            if pgid != os.getpgrp():
                os.killpg(pgid, sig)
            os.kill(pid, sig)
        except OSError:
            pass  # exited meanwhile; it is reaped on the next look

    def send_all(self, sig: signal.Signals) -> bool:
        """Send ``sig`` to every live orphan; False when there is none."""
        live = self._live()
        for key in live:
            self._send(key, sig)
        return bool(live)

    def gone(self, deadline: float, sig: signal.Signals) -> bool:
        """Poll until no orphan is alive or ``deadline``; True when none is.

        An orphan that appears meanwhile (re-parented as its parent died) is
        sent ``sig`` too.
        """
        while True:
            live = self._live()
            for key in live - self._signalled.get(sig, set()):
                self._send(key, sig)
            if not live:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_GROUP_POLL_SECONDS)

    def settled(self, deadline: float) -> bool:
        """Poll until no orphan is alive or ``deadline``, signalling nothing."""
        while self._live():
            if time.monotonic() >= deadline:
                return False
            time.sleep(_GROUP_POLL_SECONDS)
        return True

    def alive(self) -> bool:
        return bool(self._live())

    def release(self) -> None:
        """Stop the reaper, reap what has died and restore the previous
        subreaper setting. Idempotent."""
        if self._released:
            return
        self._released = True
        try:
            self._live()
        finally:
            self._prctl(_PR_SET_CHILD_SUBREAPER, self._previous, 0, 0, 0)
            _CONTAINMENT_LOCK.release()


@dataclass(frozen=True)
class _Termination:
    """What a group kill left behind: nothing, when both are False."""

    # A process was still in the group after the SIGKILL grace.
    group_survived: bool
    # A pipe never reached EOF and the capture was abandoned.
    capture_abandoned: bool
    # An orphan (see :class:`_Containment`) was still alive after SIGKILL.
    orphan_survived: bool = False


def _terminate_group(
    pgid: int,
    proc: subprocess.Popen,
    readers: tuple[_PipeDrain, ...],
    contained: _Containment | None = None,
) -> _Termination:
    """SIGTERM the child's process group, escalate to SIGKILL, and stop reading.

    ``pgid`` is signalled rather than ``proc``: the child leads its own group
    (``start_new_session``), so its descendants can be reached after the
    child itself has exited and been reaped, and the group id stays reserved
    while any of them lives. The group is dead when the child is reaped, its
    pipes reached EOF *and* no process is left in the group; the last check
    is what catches a descendant that closed its inherited pipes and ignores
    SIGTERM, which the first two cannot see. Every wait here is bounded by
    the kill grace, so the whole function returns within two grace periods
    (SIGTERM, then SIGKILL) whatever the group left behind. What survives
    the SIGKILL grace cannot be dealt with from here and is not waited for:
    a member stuck in the kernel (uninterruptible sleep) or not signallable,
    the direct child included, and a writer that left the group (``setsid``)
    and so was never reached. The capture is then abandoned, and a child
    still unreaped is left to the ``subprocess`` module, which reaps it when
    it finally dies. The return value names what was left: a member still
    in the group, and a capture that had to be abandoned, so the caller can
    report an unclean kill instead of a clean one.

    With ``contained``, the invocation's orphans get each signal with the
    group, and the kill is complete only once none is alive either; a
    ``setsid`` writer is then one of them, so its pipes reach EOF instead of
    being abandoned.
    """
    deadline = time.monotonic() + _KILL_GRACE_SECONDS
    for sig in (signal.SIGTERM, signal.SIGKILL):
        signalled = _signal_group(pgid, sig)
        if contained is not None and contained.send_all(sig):
            signalled = True
        if not signalled:
            break
        deadline = time.monotonic() + _KILL_GRACE_SECONDS
        if (
            _reaped(proc, deadline)
            and _eof(readers, deadline)
            and _group_gone(pgid, deadline)
            and (contained is None or contained.gone(deadline, sig))
        ):
            return _Termination(group_survived=False, capture_abandoned=False)
    # The group is empty (the signal found nobody) or its remains are past
    # help; the remaining EOF wait runs out the deadline already in hand,
    # never a fresh one, so the bound above holds.
    abandoned = not _eof(readers, deadline)
    if abandoned:
        for reader in readers:
            reader.abandon()
    return _Termination(
        group_survived=_group_alive(pgid),
        capture_abandoned=abandoned,
        orphan_survived=contained is not None and contained.alive(),
    )


def _child_environment(
    env_allowlist: tuple[str, ...] | None, env: Mapping[str, str] | None
) -> dict[str, str]:
    """The child's environment: the whole of this process's, or only the
    allow-listed names (:func:`select_environment`), with ``env`` layered over."""
    child = dict(os.environ) if env_allowlist is None else select_environment(env_allowlist)
    if env:
        child.update(env)
    return child


def _spawn(
    command: list[str], *, cwd: str | None, env: dict[str, str], stdin: int
) -> subprocess.Popen[bytes]:
    """Start ``command`` in a session of its own with stdout and stderr piped.

    ``start_new_session`` makes the child lead a process group the executor
    can reach and kill after the child itself is gone; the terminal is never
    inherited. A spawn failure is an :class:`ExecutionError`.
    """
    try:
        return subprocess.Popen(  # noqa: S603 - argv list, no shell
            command,
            cwd=cwd,
            env=env,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise ExecutionError(f"executable not found: {command[0]} ({exc})") from exc
    except OSError as exc:
        raise ExecutionError(f"failed to spawn {' '.join(command)}: {exc}") from exc


def execute(req: ExecutionRequest) -> ExecutionResult:
    """Run one subprocess to completion, capturing output.

    Completion is the child's exit, EOF on both pipes and an empty process
    group. The child's exit is bounded by ``timeout_seconds``; past it the
    whole group is killed and the result is a timeout. Once the child has
    exited on its own, the other two are given ``_EXIT_GRACE_SECONDS``: a
    descendant that outlives the child (a server it left running, holding
    the inherited pipes or with its stdio redirected) is then killed with
    the rest of the group, the child's own exit status and output are
    returned, and ``descendants_killed`` records the kill. Nothing an agent
    starts outlives its invocation; the next invocation begins with no
    process of the previous one holding a port or writing into the tree.

    Every wait past the child's exit is bounded, so what a kill cannot
    remove is reported (``group_survived_kill``, ``capture_abandoned``)
    rather than waited for, and ``execute()`` returns within the timeout
    plus the exit grace plus two kill grace periods whatever the child left
    behind.

    Timeouts, non-zero exits and truncated output are *returned* (not
    raised) so callers can log stdout/stderr first; use ``raise_if_failed()``
    to convert. ExecutionError is raised only when the process cannot be
    spawned, the threads that feed, read and reap for it cannot be started
    (the group is killed first once it runs), or its output cannot be read.

    With ``contain_orphans`` the invocation's orphans (:class:`_Containment`)
    count as part of what must be gone: they get the exit grace and the kill
    the group gets, within the same bounds. Those that die while the child
    runs are reaped meanwhile.
    """
    if not req.command:
        raise ExecutionError("empty command")
    if req.max_output_bytes <= 0:
        raise ExecutionError(f"max_output_bytes must be > 0, got {req.max_output_bytes}")
    contained = _Containment.begin() if req.contain_orphans else None
    try:
        return _execute(req, contained)
    finally:
        if contained is not None:
            contained.release()


def _execute(req: ExecutionRequest, contained: _Containment | None) -> ExecutionResult:
    started = _now()
    timeout = req.timeout_seconds if req.timeout_seconds > 0 else None
    try:
        feeder = None if req.stdin_data is None else _StdinFeeder(req.stdin_data)
    except OSError as exc:
        raise ExecutionError(f"failed to spawn {' '.join(req.command)}: {exc}") from exc
    readers: tuple[_BoundedReader, ...] = ()
    timed_out = False
    descendants_killed = False
    left = _Termination(group_survived=False, capture_abandoned=False)
    try:
        proc = _spawn(
            req.command,
            cwd=req.cwd,
            env=_child_environment(req.env_allowlist, req.env),
            stdin=subprocess.DEVNULL if feeder is None else feeder.child_end,
        )
    except BaseException:
        if feeder is not None:
            feeder.stop()
        raise
    assert proc.stdout is not None and proc.stderr is not None
    if contained is not None:
        contained.child = proc.pid
    pgid = proc.pid  # start_new_session: the child leads a group of its own
    try:
        # The child is running from here on, so everything that serves it
        # is set up under the same guard as the wait, and nothing that can
        # fail runs before it: a thread the system refuses (a thread or
        # descriptor limit) kills the group and fails the launch as a
        # refused spawn would, and anything else (an allocation, Ctrl-C)
        # kills it too, never leaving the child behind.
        try:
            if feeder is not None:
                feeder.start()
            # Output is read as bytes and decoded with replacement: agent
            # output is untrusted (a dumped binary, a mis-encoded file the
            # agent cats), and a decode error is not an AutoForgeError, so it
            # would leave the invocation unlogged (#17).
            observer = None if req.on_output is None else _OutputObserver(req.on_output)
            for stream, name in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
                readers += (_BoundedReader(stream, req.max_output_bytes, name, observer),)
                readers[-1].start()
        except (OSError, RuntimeError) as exc:
            raise ExecutionError(f"failed to start {' '.join(req.command)}: {exc}") from exc
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        if timed_out:
            left = _terminate_group(pgid, proc, readers, contained)
        else:
            # The child has exited, but the invocation is over only at EOF
            # and once no process is left in its group. A child that left
            # nothing behind clears both at once; a descendant that holds
            # the inherited pipes or merely stays in the group is given the
            # exit grace and then killed with the group, the child's own
            # result kept. The group id cannot name an unrelated process
            # while a member lives; once the group is empty the id is free
            # for reuse, which only a pid wrap-around inside this window
            # could bring about.
            # An orphan still alive past the same grace is killed with
            # them, and reported as an orphan, not as a group member.
            grace = time.monotonic() + _EXIT_GRACE_SECONDS
            group_settled = _eof(readers, grace) and _group_gone(pgid, grace)
            if not (group_settled and (contained is None or contained.settled(grace))):
                descendants_killed = not group_settled
                left = _terminate_group(pgid, proc, readers, contained)
    except BaseException:
        # A reader whose thread never started has nothing to stop.
        started_readers = tuple(reader for reader in readers if reader.ident is not None)
        _terminate_group(pgid, proc, started_readers, contained)
        raise
    finally:
        if feeder is not None:
            feeder.stop()
        for reader in readers:
            reader.close()
        proc.stdout.close()
        proc.stderr.close()
    for reader in readers:
        if reader.error is not None:
            raise ExecutionError(
                f"failed to read {reader.name.removeprefix('autoforge-capture-')} of "
                f"{' '.join(req.command)}: {reader.error}"
            ) from reader.error
    if feeder is not None and feeder.error is not None:
        raise ExecutionError(
            f"failed to write the stdin of {' '.join(req.command)}: {feeder.error}"
        ) from feeder.error
    out, err = (reader.captured() for reader in readers)
    return ExecutionResult(
        command=list(req.command),
        cwd=req.cwd,
        exit_code=-1 if timed_out else proc.returncode,
        stdout=out.text,
        stderr=err.text,
        started_at=started,
        finished_at=_now(),
        timed_out=timed_out,
        stdout_truncated=out.truncated,
        stderr_truncated=err.truncated,
        stdout_tail_offset=out.tail_offset,
        descendants_killed=descendants_killed,
        group_survived_kill=left.group_survived,
        capture_abandoned=left.capture_abandoned,
        orphans_killed=contained is not None and contained.found,
        orphan_survived_kill=left.orphan_survived,
        orphans_unchecked=req.contain_orphans and contained is None,
        stdin_unread=0 if feeder is None else feeder.unread,
    )
