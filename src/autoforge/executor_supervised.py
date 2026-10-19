"""A supervised child process: the scheduler's handle on one worker (ADR 0008 D9, D13; #182).

Executor layer, with no workflow semantics: it starts one process, signals
it and observes its exit, and knows nothing of what the process does.

* :func:`spawn_supervised` starts ``argv`` in a session of its own
  (``start_new_session=True``), with every descriptor closed but the three
  standard streams and the ones named in ``pass_fds`` (``close_fds=True``),
  stdin on ``/dev/null`` and stdout and stderr both on ``stdout_fd`` (the
  worker log; there is no pipe between the supervisor and the child). The
  new session keeps the terminal's signals away from the child: only the
  supervisor's own :meth:`SupervisedChild.signal` reaches it. Signal
  dispositions are inherited as ``subprocess`` leaves them (an ignored
  SIGINT stays ignored): a child that must be interruptible installs its
  handlers itself, as the worker does.
* :class:`SupervisedChild` signals only a child that this process has not
  reaped yet, so its pid cannot have been reused, and never sends
  ``SIGKILL``: a worker that is stopped returns through its own teardown.
* :func:`process_start_time` is the kernel's start time of a pid, for the
  claim's diagnostics; never authority.

Unlike the agent launches of :mod:`autoforge.executor` this is not a
contained invocation: the supervisor is no subreaper, kills no process
group and runs no sandbox (ADR 0002 is unchanged). A worker runs its own
contained agent invocations, one at a time.
"""

from __future__ import annotations

import os
import signal as signals
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence

from .errors import ExecutionError

Popen = Callable[..., subprocess.Popen[bytes]]


class SupervisedChild:
    """One started child, until it is reaped; what :func:`spawn_supervised` returns."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        """The exit status once reaped: ``0``-``255``, or minus the signal that ended it."""
        return self._process.returncode

    def poll(self) -> int | None:
        """Reap the child if it has exited; its exit status, or ``None`` while it lives."""
        return self._process.poll()

    def wait(self, deadline: float) -> int | None:
        """Wait until the child exits or ``time.monotonic()`` reaches ``deadline``.

        The exit status, or ``None`` when the deadline came first; a
        deadline already past is one :meth:`poll`.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return self.poll()
        try:
            return self._process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            return None

    def signal(self, sig: int) -> bool:
        """Send ``sig`` to the child; ``False`` (nothing sent) once it was reaped.

        ``SIGKILL`` is refused (ValueError): a supervised child is never
        killed, it is interrupted and returns through its own teardown.
        """
        if sig == signals.SIGKILL:
            raise ValueError("a supervised child is never sent SIGKILL")
        if self._process.poll() is not None:
            return False
        try:
            self._process.send_signal(sig)
        except ProcessLookupError:  # pragma: no cover - unreaped, so it exists
            return False
        return True


def spawn_supervised(
    argv: Sequence[str],
    *,
    pass_fds: Sequence[int],
    stdout_fd: int,
    cwd: str | os.PathLike[str],
    env: Mapping[str, str],
    popen: Popen = subprocess.Popen,
) -> SupervisedChild:
    """Start ``argv`` as a supervised child (see the module docstring).

    ``argv[0]`` is an absolute path: nothing is looked up on ``PATH``. A
    start that fails (the ``Popen`` call raised) is an
    :class:`~autoforge.errors.ExecutionError`, and no child exists; once
    this returns, one does. ``popen`` is the injection point of tests.
    """
    command = [str(arg) for arg in argv]
    if not command or not os.path.isabs(command[0]):
        raise ExecutionError(f"a supervised child is started by absolute path, got {command[:1]!r}")
    try:
        process = popen(  # noqa: S603 - argv list, no shell
            command,
            stdin=subprocess.DEVNULL,
            stdout=stdout_fd,
            stderr=stdout_fd,
            cwd=os.fspath(cwd),
            env=dict(env),
            pass_fds=tuple(pass_fds),
            close_fds=True,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExecutionError(f"failed to start {command[0]}: {exc}") from exc
    return SupervisedChild(process)


def process_start_time(pid: int) -> int | None:
    """``pid``'s start time in clock ticks since boot (``/proc/<pid>/stat`` field 22).

    ``None`` where it cannot be read (no ``/proc``, the process is gone).
    The pair ``(pid, start time)`` names one process across a pid reuse.
    """
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as fh:
            stat = fh.read()
    except OSError:
        return None
    # ``pid (comm) state ppid ...``: comm may hold spaces and parentheses, so
    # the fields are counted from the last ``)``.
    fields = stat[stat.rfind(b")") + 2 :].split()
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None
