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
  agent (test runners, editors, servers) do not linger.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .errors import ExecutionError, ExecutionTimeoutError

_KILL_GRACE_SECONDS = 5.0


@dataclass
class ExecutionRequest:
    command: list[str]
    cwd: str | None = None
    env: dict[str, str] | None = None  # merged over os.environ when given
    timeout_seconds: int = 1800


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

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.exit_code == 0

    def raise_if_failed(self) -> ExecutionResult:
        if self.timed_out:
            raise ExecutionTimeoutError(f"command timed out: {' '.join(self.command)}")
        if self.exit_code != 0:
            raise ExecutionError(
                f"command exited {self.exit_code}: {' '.join(self.command)}\n"
                f"stderr (tail): {self.stderr[-2000:]}"
            )
        return self


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

    Timeouts and non-zero exits are *returned* (not raised) so callers can
    log stdout/stderr first; use ``raise_if_failed()`` to convert.
    ExecutionError is raised only when the process cannot be spawned.
    """
    if not req.command:
        raise ExecutionError("empty command")
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
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise ExecutionError(f"executable not found: {req.command[0]} ({exc})") from exc
    except OSError as exc:
        raise ExecutionError(f"failed to spawn {' '.join(req.command)}: {exc}") from exc
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return ExecutionResult(
            command=list(req.command),
            cwd=req.cwd,
            exit_code=proc.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
            started_at=started,
            finished_at=_now(),
        )
    except subprocess.TimeoutExpired:
        _terminate_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        return ExecutionResult(
            command=list(req.command),
            cwd=req.cwd,
            exit_code=-1,
            stdout=stdout or "",
            stderr=stderr or "",
            started_at=started,
            finished_at=_now(),
            timed_out=True,
        )
    except BaseException:
        _terminate_group(proc)
        raise


@dataclass
class AgentInvocation:
    """A single agent (Claude Code / OpenCode) invocation plan."""

    profile_name: str
    provider: str
    model: str
    effort: str
    command: list[str] = field(default_factory=list)
    timeout_seconds: int = 1800
