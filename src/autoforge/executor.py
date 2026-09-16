"""CLI execution abstraction over subprocess.

Represents every external invocation (Claude Code, OpenCode, gh, git) as a
structured ExecutionRequest -> ExecutionResult.

Safety properties:
- argv lists only, never ``shell=True`` — prompt text containing quotes,
  ``$``, ``;``, ``|`` or ``$(...)`` is passed as one opaque argument;
- stdin is ``/dev/null`` so a CLI that expects an interactive TTY cannot
  hang waiting for input;
- the child runs in its own session/process group; on timeout the whole
  group is terminated (SIGTERM, then SIGKILL) and the kill is complete only
  when no process is left in the group, so grandchildren spawned by an agent
  (test runners, editors, servers) do not linger, whether or not they still
  hold the agent's pipes. The timeout bounds the whole invocation: the
  child's exit *and* EOF on its pipes. A descendant that inherited them (a
  server the agent left running) keeps them open past the child's exit, and
  one that also left the process group cannot be killed from here, so the
  capture is abandoned rather than waited for;
- capture is bounded: each stream keeps at most ``max_output_bytes`` (the
  first half and the last half of what the child wrote) in a buffer whose
  memory is that bound plus a constant, so a runaway or adversarial child
  costs the controller a bounded amount of memory, not the size of its
  output, however it chunks it. The tail is kept because the CONTROL_RESULT
  block is the last thing on stdout; :attr:`ExecutionResult.stdout_tail` is
  the part known to be contiguous up to EOF, so a caller that parses a
  truncated stream never sees text that spans the cut.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import IO

from .errors import ExecutionError, ExecutionTimeoutError

_KILL_GRACE_SECONDS = 5.0
_GROUP_POLL_SECONDS = 0.02
_READ_CHUNK_BYTES = 64 * 1024

# Per-stream capture bound. A Claude Code ``-p`` transcript is kilobytes and
# an OpenCode ``run`` transcript at most a few megabytes, so the default is
# well above any legitimate agent output; ``gh``/``git`` plumbing output is
# smaller still. It is a request field rather than configuration because
# nothing legitimate should reach it.
DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024 * 1024


@dataclass
class ExecutionRequest:
    command: list[str]
    cwd: str | None = None
    env: dict[str, str] | None = None  # merged over os.environ when given
    timeout_seconds: int = 1800
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES


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


class _BoundedReader(threading.Thread):
    """Drain one pipe into a :class:`_BoundedBuffer` until EOF or abandoned.

    The pipe is drained whatever the child writes, so the child never blocks
    on a full pipe the way it would if the controller stopped reading. EOF
    arrives only once every writer is gone; :meth:`abandon` ends the read
    without it, for a writer nothing here can kill.
    """

    def __init__(self, stream: IO[bytes], limit: int, name: str) -> None:
        super().__init__(name=f"autoforge-capture-{name}", daemon=True)
        self._fd = stream.fileno()
        self.buffer = _BoundedBuffer(limit, name)
        self._wake_r, self._wake_w = os.pipe()
        self.error: OSError | None = None

    def run(self) -> None:
        try:
            with selectors.DefaultSelector() as sel:
                sel.register(self._fd, selectors.EVENT_READ)
                sel.register(self._wake_r, selectors.EVENT_READ)
                while True:
                    ready = {key.fd for key, _ in sel.select()}
                    if self._wake_r in ready:
                        return
                    chunk = os.read(self._fd, _READ_CHUNK_BYTES)
                    if not chunk:
                        return
                    self.buffer.feed(chunk)
        except OSError as exc:
            self.error = exc

    def wait(self, deadline: float | None) -> bool:
        """Join until EOF or ``deadline`` (``time.monotonic()``); True on EOF."""
        self.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
        return not self.is_alive()

    def abandon(self) -> None:
        """Stop reading without EOF; what was read so far is what is captured."""
        os.write(self._wake_w, b"\0")
        self.join()

    def close(self) -> None:
        for fd in (self._wake_r, self._wake_w):
            try:
                os.close(fd)
            except OSError:
                pass

    def captured(self) -> _Captured:
        """Decode what was kept; call only once the thread has ended."""
        return self.buffer.captured()


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


def _eof(readers: tuple[_BoundedReader, ...], deadline: float | None) -> bool:
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


def _terminate_group(
    pgid: int, proc: subprocess.Popen, readers: tuple[_BoundedReader, ...]
) -> None:
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
    it finally dies; ``execute()`` reports the timeout either way, and never
    a stale exit status, since a timed-out result carries no exit code.
    """
    deadline = time.monotonic() + _KILL_GRACE_SECONDS
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not _signal_group(pgid, sig):
            break
        deadline = time.monotonic() + _KILL_GRACE_SECONDS
        if _reaped(proc, deadline) and _eof(readers, deadline) and _group_gone(pgid, deadline):
            return
    # The group is empty (the signal found nobody) or its remains are past
    # help; the remaining EOF wait runs out the deadline already in hand,
    # never a fresh one, so the bound above holds.
    if not _eof(readers, deadline):
        for reader in readers:
            reader.abandon()


def execute(req: ExecutionRequest) -> ExecutionResult:
    """Run one subprocess to completion, capturing output.

    Completion is the child's exit *and* EOF on both pipes, bounded together
    by ``timeout_seconds``: a descendant that inherited the pipes and outlives
    the child keeps the invocation open, and past the timeout the whole group
    is killed and the result is a timeout, as it is when the child itself
    overruns.

    Timeouts, non-zero exits and truncated output are *returned* (not
    raised) so callers can log stdout/stderr first; use ``raise_if_failed()``
    to convert. ExecutionError is raised only when the process cannot be
    spawned or its output cannot be read.
    """
    if not req.command:
        raise ExecutionError("empty command")
    if req.max_output_bytes <= 0:
        raise ExecutionError(f"max_output_bytes must be > 0, got {req.max_output_bytes}")
    started = _now()
    env = dict(os.environ)
    if req.env:
        env.update(req.env)
    timeout = req.timeout_seconds if req.timeout_seconds > 0 else None
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv list, no shell
            req.command,
            cwd=req.cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise ExecutionError(f"executable not found: {req.command[0]} ({exc})") from exc
    except OSError as exc:
        raise ExecutionError(f"failed to spawn {' '.join(req.command)}: {exc}") from exc
    assert proc.stdout is not None and proc.stderr is not None
    pgid = proc.pid  # start_new_session: the child leads a group of its own
    deadline = None if timeout is None else time.monotonic() + timeout
    # Output is read as bytes and decoded with replacement: agent output is
    # untrusted (a dumped binary, a mis-encoded file the agent cats), and a
    # decode error is not an AutoForgeError, so it would leave the
    # invocation unlogged (#17).
    readers = (
        _BoundedReader(proc.stdout, req.max_output_bytes, "stdout"),
        _BoundedReader(proc.stderr, req.max_output_bytes, "stderr"),
    )
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        # The child has exited, but the invocation is over only at EOF, which
        # a descendant holding the inherited pipes can delay indefinitely;
        # the wait for it runs under the same deadline.
        if not timed_out and not _eof(readers, deadline):
            timed_out = True
        if timed_out:
            _terminate_group(pgid, proc, readers)
    except BaseException:
        _terminate_group(pgid, proc, readers)
        raise
    finally:
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
    )


@dataclass
class AgentInvocation:
    """A single agent (Claude Code / OpenCode) invocation plan."""

    profile_name: str
    provider: str
    model: str
    effort: str
    command: list[str] = field(default_factory=list)
    timeout_seconds: int = 1800
