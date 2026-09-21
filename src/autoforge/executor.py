"""CLI execution abstraction over subprocess.

Represents every external invocation (Claude Code, OpenCode, gh, git) as a
structured ExecutionRequest -> ExecutionResult.

Safety properties:
- argv lists only, never ``shell=True`` — prompt text containing quotes,
  ``$``, ``;``, ``|`` or ``$(...)`` is passed as one opaque argument;
- stdin is ``/dev/null`` so a CLI that expects an interactive TTY cannot
  hang waiting for input;
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
  executor only applies the selection.
"""

from __future__ import annotations

import os
import re
import selectors
import signal
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
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
    *, descendants_killed: bool, group_survived_kill: bool, capture_abandoned: bool
) -> str:
    """One sentence naming what an invocation left behind; empty when nothing.

    The three facts are the executor's (see :class:`ExecutionResult`); the
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


@dataclass(frozen=True)
class _Termination:
    """What a group kill left behind: nothing, when both are False."""

    # A process was still in the group after the SIGKILL grace.
    group_survived: bool
    # A pipe never reached EOF and the capture was abandoned.
    capture_abandoned: bool


def _terminate_group(
    pgid: int, proc: subprocess.Popen, readers: tuple[_BoundedReader, ...]
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
    """
    deadline = time.monotonic() + _KILL_GRACE_SECONDS
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not _signal_group(pgid, sig):
            break
        deadline = time.monotonic() + _KILL_GRACE_SECONDS
        if _reaped(proc, deadline) and _eof(readers, deadline) and _group_gone(pgid, deadline):
            return _Termination(group_survived=False, capture_abandoned=False)
    # The group is empty (the signal found nobody) or its remains are past
    # help; the remaining EOF wait runs out the deadline already in hand,
    # never a fresh one, so the bound above holds.
    abandoned = not _eof(readers, deadline)
    if abandoned:
        for reader in readers:
            reader.abandon()
    return _Termination(group_survived=_group_alive(pgid), capture_abandoned=abandoned)


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
    spawned or its output cannot be read.
    """
    if not req.command:
        raise ExecutionError("empty command")
    if req.max_output_bytes <= 0:
        raise ExecutionError(f"max_output_bytes must be > 0, got {req.max_output_bytes}")
    started = _now()
    env = dict(os.environ) if req.env_allowlist is None else select_environment(req.env_allowlist)
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
    descendants_killed = False
    left = _Termination(group_survived=False, capture_abandoned=False)
    try:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        if timed_out:
            left = _terminate_group(pgid, proc, readers)
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
            grace = time.monotonic() + _EXIT_GRACE_SECONDS
            if not (_eof(readers, grace) and _group_gone(pgid, grace)):
                descendants_killed = True
                left = _terminate_group(pgid, proc, readers)
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
        descendants_killed=descendants_killed,
        group_survived_kill=left.group_survived,
        capture_abandoned=left.capture_abandoned,
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
