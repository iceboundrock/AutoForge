"""PID 1 of every container AutoForge creates: the watcher's image side (ADR 0013 D6, D13; #271).

A container's PID 1 reaps what is re-parented to it and ends the container
when it ends: ``close()`` of a run step's prepared launch, a removal, or
the deadline. This is the part #271 puts in the image: it runs as
``WATCHER_UID`` (the create's user), takes one argument, the container's
deadline in whole seconds from its start, and exits at it, so a container
nothing stops still ends. ``SIGTERM``, ``SIGINT`` and ``SIGHUP`` end it at
once (a PID 1 ignores a signal it has no handler for, so the runtime's
``stop`` would otherwise wait out its grace).

The liveness stream to the controller and the death-watch that ends a
container whose controller died are #272's; until it lands, the deadline
and the lifecycle's removals bound a container's life.

It imports nothing of :mod:`autoforge` and only the standard library.
"""

from __future__ import annotations

import os
import signal
import sys
import time

MAX_DEADLINE_SECONDS = 7 * 24 * 3600
# Exit status at the deadline, as ``timeout(1)`` reports one.
DEADLINE_STATUS = 124


def _reap() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not argv[0].isdigit():
        sys.stderr.write("usage: container_watch.py <deadline seconds>\n")
        return 2
    deadline = time.monotonic() + min(int(argv[0]), MAX_DEADLINE_SECONDS)
    stopping = False

    def stop(signum: int, frame: object) -> None:
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop)
    signal.signal(signal.SIGCHLD, lambda signum, frame: None)
    while not stopping:
        _reap()
        left = deadline - time.monotonic()
        if left <= 0:
            return DEADLINE_STATUS
        time.sleep(min(left, 1.0))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
