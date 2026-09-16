"""CLI execution abstraction over subprocess.

Represents every external invocation (Claude Code, OpenCode, gh, git) as a
structured ExecutionRequest -> ExecutionResult.

Safety properties:
- argv lists only, never ``shell=True`` — prompt text containing quotes,
  ``$``, ``;``, ``|`` or ``$(...)`` is passed as one opaque argument;
- stdin is ``/dev/null`` so a CLI that expects an interactive TTY cannot
  hang waiting for input;
- the child runs in its own session/process group; on timeout the whole
  group is terminated (SIGTERM, then SIGKILL) so grandchildren spawned by an
  agent (test runners, editors, servers) do not linger;
- capture is bounded: each stream keeps at most ``max_output_bytes`` (the
  first half and the last half of what the child wrote), so a runaway or
  adversarial child costs the controller a bounded amount of memory, not the
  size of its output. The tail is kept because the CONTROL_RESULT block is
  the last thing on stdout; :attr:`ExecutionResult.stdout_tail` is the part
  known to be contiguous up to EOF, so a caller that parses a truncated
  stream never sees text that spans the cut.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import IO

from .errors import ExecutionError, ExecutionTimeoutError

_KILL_GRACE_SECONDS = 5.0
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


class _BoundedReader(threading.Thread):
    """Drain one pipe, keeping at most ``limit`` bytes of it.

    The first ``limit - limit // 2`` bytes are kept as the head and the last
    ``limit // 2`` as a rolling tail (a deque of chunks, so trimming is
    O(chunk), not O(tail)); everything between is counted and dropped. The
    pipe is drained to EOF whatever the child writes, so the child never
    blocks on a full pipe the way it would if the controller stopped reading.
    """

    def __init__(self, stream: IO[bytes], limit: int, name: str) -> None:
        super().__init__(name=f"autoforge-capture-{name}", daemon=True)
        self._fd = stream.fileno()
        self._name = name
        self._limit = limit
        self._head_limit = limit - limit // 2
        self._tail_limit = limit // 2
        self._head = bytearray()
        self._tail: deque[bytes] = deque()
        self._tail_len = 0
        self._total = 0
        self.error: OSError | None = None

    def run(self) -> None:
        try:
            while True:
                chunk = os.read(self._fd, _READ_CHUNK_BYTES)
                if not chunk:
                    return
                self._feed(chunk)
        except OSError as exc:
            self.error = exc

    def _feed(self, chunk: bytes) -> None:
        self._total += len(chunk)
        room = self._head_limit - len(self._head)
        if room > 0:
            self._head += chunk[:room]
            chunk = chunk[room:]
        if not chunk:
            return
        self._tail.append(chunk)
        self._tail_len += len(chunk)
        while self._tail and self._tail_len - len(self._tail[0]) >= self._tail_limit:
            self._tail_len -= len(self._tail.popleft())

    def captured(self) -> _Captured:
        """Decode what was kept; call only after :meth:`join`."""
        tail = b"".join(self._tail)[-self._tail_limit :] if self._tail_limit else b""
        omitted = self._total - len(self._head) - len(tail)
        head_text = self._head.decode("utf-8", errors="replace")
        tail_text = tail.decode("utf-8", errors="replace")
        if omitted <= 0:
            return _Captured(head_text + tail_text, False, 0)
        marker = (
            f"\n[autoforge: {omitted} bytes of {self._name} omitted; the capture bound is "
            f"{self._limit} bytes, the first {len(self._head)} and last {len(tail)} "
            "bytes were kept]\n"
        )
        return _Captured(head_text + marker + tail_text, True, len(head_text) + len(marker))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _terminate_group(proc: subprocess.Popen) -> None:
    """SIGTERM the child's process group, escalate to SIGKILL after a grace period."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + _KILL_GRACE_SECONDS
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.05)


def execute(req: ExecutionRequest) -> ExecutionResult:
    """Run one subprocess to completion, capturing output.

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
            _terminate_group(proc)
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    except BaseException:
        _terminate_group(proc)
        raise
    # EOF arrives once every writer of the pipe is gone, which the group
    # kill guarantees for the timeout path.
    for reader in readers:
        reader.join()
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
