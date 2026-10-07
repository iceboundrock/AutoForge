"""Executor: real subprocesses (python -c) — argv safety, timeout, exit codes."""

import hashlib
import os
import random
import signal
import sys
import threading
import time
import tracemalloc
from pathlib import Path

import pytest

from autoforge import executor
from autoforge.errors import ExecutionError
from autoforge.executor import ExecutionRequest, _BoundedBuffer, execute

PY = sys.executable


def test_success_captures_streams():
    res = execute(
        ExecutionRequest(
            command=[PY, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
            timeout_seconds=30,
        )
    )
    assert res.ok and res.exit_code == 0
    assert res.stdout == "out\n" and res.stderr == "err\n"
    assert res.started_at <= res.finished_at
    assert not (res.descendants_killed or res.group_survived_kill or res.capture_abandoned)
    assert res.leftovers == ""
    res.raise_if_failed()


def test_nonzero_returned_not_raised():
    res = execute(ExecutionRequest(command=[PY, "-c", "raise SystemExit(3)"], timeout_seconds=30))
    assert not res.ok and res.exit_code == 3
    with pytest.raises(ExecutionError, match="exited 3"):
        res.raise_if_failed()


def test_shell_metacharacters_are_literal_argv():
    """A prompt full of shell syntax must arrive as ONE argv element, unevaluated."""
    tricky = "$(touch /tmp/pwned); `id`; rm -rf / && echo $HOME | cat > x; '\"; #"
    res = execute(
        ExecutionRequest(
            command=[PY, "-c", "import sys; print(repr(sys.argv[1]))", tricky],
            timeout_seconds=30,
        )
    )
    assert res.ok and res.stdout.strip() == repr(tricky)


def test_timeout_kills_process_tree():
    res = execute(
        ExecutionRequest(
            command=[PY, "-c", "import time; print('start', flush=True); time.sleep(30)"],
            timeout_seconds=1,
        )
    )
    assert res.timed_out and not res.ok and res.exit_code == -1
    assert "start" in res.stdout
    # The kill was clean: nothing was left behind, and nothing is reported.
    assert not (res.descendants_killed or res.group_survived_kill or res.capture_abandoned)
    assert res.leftovers == ""


# A child that spawns a descendant which inherits stdout/stderr, prints the
# descendant's pid and exits with the requested status; the descendant sleeps
# far past any timeout used here. ``setsid`` makes it leave the child's
# process group as well; ``linger`` makes the child itself overrun instead of
# exiting.
_ORPHAN = (
    "import subprocess, sys, time\n"
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
    "                     start_new_session=sys.argv[1] == 'setsid')\n"
    "print('child done', p.pid, flush=True)\n"
    "if 'linger' in sys.argv[3:]:\n"
    "    time.sleep(60)\n"
    "sys.exit(int(sys.argv[2]))\n"
)


def _orphan_pid(res) -> int:
    return int(res.stdout.split()[2])


def _gone(pid: int, within: float) -> bool:
    """True once ``pid`` no longer runs (an orphan lingers as a zombie until
    init reaps it, which is dead enough)."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            return True
        if state == "Z":
            return True
        time.sleep(0.05)
    return False


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


@pytest.mark.parametrize("status", [0, 3])
def test_descendant_holding_the_pipe_is_killed_after_the_exit_grace(monkeypatch, status):
    """The child exits at once but a descendant that inherited its pipes keeps
    them open (#85, point 1): the descendant is given the exit grace, then
    the group is killed, and the child's own exit status and output are
    returned with ``descendants_killed`` set. The invocation costs the grace
    plus the kill, never the timeout, and the result is not a timeout."""
    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 0.5)
    started = time.monotonic()
    res = execute(
        ExecutionRequest(command=[PY, "-c", _ORPHAN, "group", str(status)], timeout_seconds=30)
    )
    elapsed = time.monotonic() - started
    pid = _orphan_pid(res)
    try:
        assert not res.timed_out and res.exit_code == status
        assert res.ok is (status == 0)
        assert res.stdout.startswith("child done ")
        assert res.descendants_killed
        assert not res.group_survived_kill and not res.capture_abandoned
        assert "left processes behind" in res.leftovers
        assert 0.5 <= elapsed < 6, elapsed  # the grace plus at most the SIGTERM grace
        assert _gone(pid, within=5), "the pipe-holding descendant outlived execute()"
    finally:
        _kill_quietly(pid)


def test_descendant_that_exits_within_the_grace_is_not_killed(monkeypatch):
    """A helper the child is shutting down as it exits (an MCP server, a
    watcher) closes the pipes and leaves the group on its own within the
    exit grace; nothing is killed and nothing is reported (#85)."""
    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 2.0)
    script = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.3)'])\n"
        "print('child done', flush=True)\n"
    )
    started = time.monotonic()
    res = execute(ExecutionRequest(command=[PY, "-c", script], timeout_seconds=30))
    elapsed = time.monotonic() - started
    assert res.ok and res.exit_code == 0 and res.stdout == "child done\n"
    assert not (res.descendants_killed or res.group_survived_kill or res.capture_abandoned)
    assert 0.3 <= elapsed < 2, elapsed  # waited for the helper, not for the grace


def test_descendant_outside_the_group_cannot_hold_the_capture_open(monkeypatch):
    """A descendant that also called ``setsid`` cannot be reached by the group
    kill; the capture is abandoned after the grace period rather than waited
    for, so ``execute`` still returns within a bounded time, with the child's
    own result, and reports that a writer beyond its reach holds the pipes."""
    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    started = time.monotonic()
    res = execute(ExecutionRequest(command=[PY, "-c", _ORPHAN, "setsid", "0"], timeout_seconds=30))
    elapsed = time.monotonic() - started
    pid = _orphan_pid(res)
    try:
        assert not res.timed_out and res.exit_code == 0 and res.ok
        assert res.stdout.startswith("child done ")
        assert res.descendants_killed and res.capture_abandoned
        assert not res.group_survived_kill  # the escapee is not a member of the group
        assert "outside its group" in res.leftovers
        assert elapsed < 5, elapsed
        assert not _gone(pid, within=0.1)  # nothing here can kill it; it is not waited for
    finally:
        _kill_quietly(pid)


def test_timed_out_child_with_a_writer_outside_the_group_reports_the_abandoned_capture(
    monkeypatch,
):
    """On the timeout path the same escapee is reported the same way: the
    group kill is clean (the child itself dies), but the pipes never reach
    EOF and the capture is abandoned (#85 addendum: name what the kill did
    not reach)."""
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    started = time.monotonic()
    res = execute(
        ExecutionRequest(command=[PY, "-c", _ORPHAN, "setsid", "0", "linger"], timeout_seconds=1)
    )
    elapsed = time.monotonic() - started
    pid = _orphan_pid(res)
    try:
        assert res.timed_out and res.exit_code == -1
        assert not res.descendants_killed  # the child was killed, not its leftovers after it
        assert res.capture_abandoned and not res.group_survived_kill
        assert elapsed < 5, elapsed
        assert not _gone(pid, within=0.1)
    finally:
        _kill_quietly(pid)


# A child that spawns a descendant in its own process group with stdio sent
# to /dev/null and SIGTERM ignored, prints the descendant's pid and either
# sleeps past the timeout or exits at once. Its pipes reach EOF as soon as
# the child is gone while the descendant lives on; only a group liveness
# check can see it.
_DEAF_DESCENDANT = (
    "import subprocess, sys, time\n"
    "p = subprocess.Popen([sys.executable, '-c', 'import signal, time; "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'],\n"
    "                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "print('descendant', p.pid, flush=True)\n"
    "if sys.argv[1] == 'linger':\n"
    "    time.sleep(60)\n"
)


def _deaf_pid(res) -> int:
    return int(res.stdout.split()[1])


def test_same_group_descendant_with_closed_pipes_that_ignores_sigterm_is_killed(monkeypatch):
    """The child is reaped and both pipes reach EOF after SIGTERM, but a
    descendant that closed its stdio and ignores SIGTERM is still in the
    group (PR #84 review, P1): the kill must escalate to SIGKILL and return
    only once the group has no member left, not merely once the pipes closed."""
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    started = time.monotonic()
    res = execute(
        ExecutionRequest(command=[PY, "-c", _DEAF_DESCENDANT, "linger"], timeout_seconds=1)
    )
    elapsed = time.monotonic() - started
    pid = _deaf_pid(res)
    try:
        assert res.timed_out and res.exit_code == -1
        assert res.stdout.startswith("descendant ")
        assert not (res.descendants_killed or res.group_survived_kill or res.capture_abandoned)
        assert elapsed < 5, elapsed  # the timeout plus the two grace periods
        assert _gone(pid, within=0.5), "the SIGTERM-ignoring descendant outlived execute()"
    finally:
        _kill_quietly(pid)


def test_same_group_descendant_with_closed_pipes_is_killed_after_a_normal_exit(monkeypatch):
    """A descendant that holds neither the pipes nor the child's pid used to
    outlive a normal exit into the next phase (#85, point 2): the group is
    now checked after the child's exit too, and a member left past the grace
    is killed, the child's own result kept."""
    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    started = time.monotonic()
    res = execute(
        ExecutionRequest(command=[PY, "-c", _DEAF_DESCENDANT, "exit"], timeout_seconds=30)
    )
    elapsed = time.monotonic() - started
    pid = _deaf_pid(res)
    try:
        assert res.ok and not res.timed_out and res.exit_code == 0
        assert res.descendants_killed
        assert not res.group_survived_kill and not res.capture_abandoned
        assert elapsed < 5, elapsed  # the exit grace plus the two kill graces
        assert _gone(pid, within=0.5), "the SIGTERM-ignoring descendant outlived execute()"
    finally:
        _kill_quietly(pid)


def test_direct_child_that_survives_sigkill_is_not_waited_for(monkeypatch):
    """A direct child that neither SIGTERM nor SIGKILL removes within the grace
    (uninterruptible in the kernel, say) must not turn the timeout into an
    unbounded ``wait()`` (PR #84 review, P1): after both grace periods the
    capture is abandoned and the timeout is reported, and the child is left
    unreaped rather than waited for. The result names the survivor (#85
    addendum), so the run log does not present the kill as clean. The kill
    is neutered here to simulate the survivor, so the child really does
    outlive ``execute()``."""
    import subprocess

    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    real_killpg = os.killpg
    signalled: list[tuple[int, int]] = []

    def neutered_killpg(pgid, sig):
        signalled.append((pgid, int(sig)))
        if sig == 0:
            return real_killpg(pgid, sig)  # liveness probes still see the group

    monkeypatch.setattr(os, "killpg", neutered_killpg)
    real_wait = subprocess.Popen.wait
    procs: list[subprocess.Popen] = []
    waits: list[float | None] = []

    def spying_wait(self, timeout=None):
        procs.append(self)
        waits.append(timeout)
        return real_wait(self, timeout=timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", spying_wait)
    started = time.monotonic()
    res = execute(
        ExecutionRequest(
            command=[PY, "-c", "import time; print('start', flush=True); time.sleep(20)"],
            timeout_seconds=1,
        )
    )
    elapsed = time.monotonic() - started
    proc = procs[0]
    try:
        assert res.timed_out and not res.ok and res.exit_code == -1
        assert res.stdout == "start\n"
        assert res.group_survived_kill and res.capture_abandoned
        assert not res.descendants_killed
        assert "still had a member after SIGKILL" in res.leftovers
        assert elapsed < 4, elapsed  # the timeout plus the two grace periods, never the sleep
        assert None not in waits, "the child was waited for without a bound"
        assert [sig for _, sig in signalled if sig != 0] == [signal.SIGTERM, signal.SIGKILL]
        assert all(pgid == proc.pid for pgid, _ in signalled)
        assert proc.poll() is None, "the simulated survivor should still be running"
    finally:
        real_killpg(proc.pid, signal.SIGKILL)
        real_wait(proc, timeout=5)


def test_describe_leftovers_names_each_fact_and_nothing_when_clean():
    assert (
        executor.describe_leftovers(
            descendants_killed=False, group_survived_kill=False, capture_abandoned=False
        )
        == ""
    )
    text = executor.describe_leftovers(
        descendants_killed=True, group_survived_kill=True, capture_abandoned=True
    )
    assert "left processes behind" in text
    assert "still had a member after SIGKILL" in text
    assert "outside its group" in text
    assert text.count(";") == 2


def test_stdin_is_closed_not_interactive():
    res = execute(
        ExecutionRequest(
            command=[PY, "-c", "import sys; print(repr(sys.stdin.read()))"], timeout_seconds=30
        )
    )
    assert res.ok and res.stdout.strip() == "''"
    assert res.stdin_unread == 0


# Reads its stdin to EOF and prints the length and digest of what arrived.
_DIGEST_STDIN = (
    "import hashlib, sys\n"
    "data = sys.stdin.buffer.read()\n"
    "print(len(data), hashlib.sha256(data).hexdigest())\n"
)


def _digest(data: bytes) -> str:
    return f"{len(data)} {hashlib.sha256(data).hexdigest()}"


def test_stdin_data_arrives_verbatim_and_then_eof():
    """The payload reaches the child byte for byte, past one pipe buffer, and
    stdin is closed after it: the child's read-to-EOF returns."""
    head = '-x --help\r\n# "q" \'q\' `id` $(id) \\ ; | &\n{"phase": "REVIEW"}\né中\U0001f600\n'
    data = (head * 20_000).encode()  # ~1.4 MB, far past a 64 KiB pipe buffer
    res = execute(
        ExecutionRequest(command=[PY, "-c", _DIGEST_STDIN], stdin_data=data, timeout_seconds=30)
    )
    assert res.ok, res.stderr
    assert res.stdout.strip() == _digest(data)
    assert res.stdin_unread == 0


def test_empty_stdin_data_is_an_immediate_eof():
    res = execute(
        ExecutionRequest(command=[PY, "-c", _DIGEST_STDIN], stdin_data=b"", timeout_seconds=30)
    )
    assert res.ok and res.stdout.strip() == _digest(b"")
    assert res.stdin_unread == 0


def test_child_that_never_reads_its_stdin_times_out_and_the_feed_ends():
    """A payload larger than the pipe and a child that never reads it: the
    timeout still kills the group on time, the whole payload is reported
    unread, and no feeder thread outlives ``execute()``."""
    started = time.monotonic()
    res = execute(
        ExecutionRequest(
            command=[PY, "-c", "import time; time.sleep(30)"],
            stdin_data=b"x" * (4 * 1024 * 1024),
            timeout_seconds=1,
        )
    )
    assert res.timed_out and res.exit_code == -1
    assert res.stdin_unread == 4 * 1024 * 1024
    assert time.monotonic() - started < 10
    assert not [t for t in threading.enumerate() if t.name == "autoforge-feed-stdin"]


def test_child_that_exits_without_reading_its_stdin_reports_it():
    """The child exits 0 at once and leaves most of the payload unwritten: its
    own status is returned, promptly, with the whole payload unread."""
    started = time.monotonic()
    res = execute(
        ExecutionRequest(
            command=[PY, "-c", "print('bye')"],
            stdin_data=b"x" * (4 * 1024 * 1024),
            timeout_seconds=30,
        )
    )
    assert not res.timed_out and res.exit_code == 0 and res.stdout == "bye\n"
    assert res.stdin_unread == 4 * 1024 * 1024
    assert time.monotonic() - started < 10
    assert not [t for t in threading.enumerate() if t.name == "autoforge-feed-stdin"]


@pytest.mark.parametrize(
    ("size", "taken"),
    [
        (11, 0),  # the whole payload fits in the pipe: written, never read
        (11, 1),  # the PR #188 review: one byte of an 11-byte prompt, then exit 0
        (11, 10),
        (1024 * 1024, 100_000),  # unread both in the pipe and never written
    ],
)
def test_bytes_the_child_left_unread_are_counted_not_taken_as_delivered(size, taken):
    """Written is not read: a child that takes a prefix and exits 0 leaves the
    rest in the pipe, and that rest is reported exactly, whether or not the
    payload fit in the pipe buffer."""
    read_prefix = (
        "import os, sys\n"
        f"left = {taken}\n"
        "while left:\n"
        "    left -= len(os.read(0, left))\n"
        "print('read')\n"
    )
    res = execute(
        ExecutionRequest(
            command=[PY, "-c", read_prefix], stdin_data=b"y" * size, timeout_seconds=30
        )
    )
    assert res.exit_code == 0 and not res.timed_out and res.stdout == "read\n"
    assert res.stdin_unread == size - taken


def _open_fds() -> set[str]:
    return set(os.listdir("/dev/fd"))


def test_stdin_data_leaves_no_descriptor_behind():
    """The feeder holds a read end of the child's stdin past the child's
    exit; it is released with the rest, whether the child read everything,
    read nothing or the spawn failed."""
    before = _open_fds()
    for command in ([PY, "-c", _DIGEST_STDIN], [PY, "-c", "pass"]):
        execute(ExecutionRequest(command=command, stdin_data=b"z" * 200_000, timeout_seconds=30))
    with pytest.raises(ExecutionError, match="not found"):
        execute(
            ExecutionRequest(
                command=["autoforge-definitely-missing-binary-xyz"],
                stdin_data=b"z",
                timeout_seconds=5,
            )
        )
    assert _open_fds() == before


@pytest.mark.parametrize("failing_call", [1, 2], ids=["stdin-pipe", "wake-pipe"])
def test_a_stdin_pipe_that_cannot_be_opened_is_a_launch_failure(monkeypatch, failing_call):
    """The feeder opens its own pipes before the spawn. One that cannot be
    opened (here EMFILE) fails the launch as an ExecutionError, like a
    failed spawn, and the pipe already opened is closed again."""
    real_pipe = os.pipe
    calls = 0

    def pipe() -> tuple[int, int]:
        nonlocal calls
        calls += 1
        if calls == failing_call:
            raise OSError(24, "Too many open files")
        return real_pipe()

    before = _open_fds()
    monkeypatch.setattr(executor.os, "pipe", pipe)
    with pytest.raises(ExecutionError, match="failed to spawn .*Too many open files"):
        execute(ExecutionRequest(command=[PY, "-c", "pass"], stdin_data=b"z", timeout_seconds=5))
    monkeypatch.undo()
    assert calls == failing_call
    assert _open_fds() == before


@pytest.mark.parametrize(
    ("step", "nth", "error"),
    [
        ("feeder-start", 1, RuntimeError("can't start new thread")),
        ("reader-start", 1, RuntimeError("can't start new thread")),
        ("reader-start", 2, RuntimeError("can't start new thread")),
        ("reader-init", 2, OSError(24, "Too many open files")),
    ],
    ids=["stdin-feeder", "stdout-reader", "stderr-reader", "stderr-reader-pipe"],
)
def test_a_setup_failure_after_the_spawn_kills_the_child(monkeypatch, step, nth, error):
    """The PR #188 review: the threads that feed and read the child start
    after it is spawned. One the system refuses (a thread or descriptor
    limit) fails the launch as an ExecutionError, and the child, which would
    otherwise sleep on, is killed and reaped first; what did start is ended
    and nothing is left open."""
    calls = 0

    def refuse_nth(real):
        def call(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == nth:
                raise error
            return real(*args, **kwargs)

        return call

    target = {
        "feeder-start": (executor._StdinFeeder, "start", threading.Thread.start),
        "reader-start": (executor._BoundedReader, "start", threading.Thread.start),
        "reader-init": (executor._BoundedReader, "__init__", executor._BoundedReader.__init__),
    }[step]
    spawned = []
    real_spawn = executor._spawn

    def spawn(*args, **kwargs):
        spawned.append(real_spawn(*args, **kwargs))
        return spawned[-1]

    fds, threads = _open_fds(), set(threading.enumerate())
    monkeypatch.setattr(executor, "_spawn", spawn)
    monkeypatch.setattr(target[0], target[1], refuse_nth(target[2]))
    started = time.monotonic()
    try:
        with pytest.raises(ExecutionError, match=f"failed to start .*{error.args[-1]}"):
            execute(
                ExecutionRequest(
                    command=[PY, "-c", "import time; time.sleep(60)"],
                    stdin_data=b"z" * 200_000,
                    timeout_seconds=30,
                )
            )
        (proc,) = spawned
        assert proc.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(proc.pid, 0)
    finally:
        for proc in spawned:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
    assert time.monotonic() - started < 10
    monkeypatch.undo()
    assert calls == nth
    assert set(threading.enumerate()) == threads
    assert _open_fds() == fds


def test_cwd_is_applied(tmp_path):
    res = execute(
        ExecutionRequest(
            command=[PY, "-c", "import os; print(os.getcwd())"],
            cwd=str(tmp_path),
            timeout_seconds=30,
        )
    )
    assert res.stdout.strip() == str(tmp_path.resolve())


def test_missing_binary_raises_execution_error():
    with pytest.raises(ExecutionError, match="not found"):
        execute(
            ExecutionRequest(command=["autoforge-definitely-missing-binary-xyz"], timeout_seconds=5)
        )


def test_non_utf8_output_is_replaced_not_raised():
    """A stray byte in agent output (#17) must not escape ``execute`` as a
    UnicodeDecodeError: the invocation completes and the byte is replaced."""
    res = execute(
        ExecutionRequest(
            command=[
                PY,
                "-c",
                "import sys; sys.stdout.buffer.write(b'ok \\xff\\xfe end\\n'); "
                "sys.stderr.buffer.write(b'err \\xc3\\x28\\n')",
            ],
            timeout_seconds=30,
        )
    )
    assert res.ok and res.exit_code == 0
    assert res.stdout == "ok �� end\n"
    assert res.stderr == "err �(\n"


def test_multibyte_character_across_the_capture_split_is_intact():
    """Within the bound the head/tail split is internal (PR #84 review, P2):
    a character whose bytes straddle it must decode whole, not as two
    replacement characters on either side of an invisible seam."""
    res = execute(
        ExecutionRequest(
            command=[PY, "-c", "import sys; sys.stdout.buffer.write('ab€'.encode())"],
            timeout_seconds=30,
            max_output_bytes=6,
        )
    )
    assert res.ok and not res.stdout_truncated
    assert res.stdout == "ab€" and res.stdout_tail == "ab€"


# -- bounded capture (#53) ---------------------------------------------------
# A child that writes ``total`` bytes to ``stream``; the last line is the one
# a CONTROL_RESULT would occupy, so a kept tail must still end with it.
_FLOOD = (
    "import sys\n"
    "out = getattr(sys, sys.argv[1]).buffer\n"
    "total = int(sys.argv[2])\n"
    "line = b'x' * 1023 + b'\\n'\n"
    "written = 0\n"
    "while written + len(line) < total:\n"
    "    out.write(line); written += len(line)\n"
    "out.write(b'TAIL-MARKER\\n')\n"
)


def _flood(stream: str, total: int, bound: int):
    return execute(
        ExecutionRequest(
            command=[PY, "-c", _FLOOD, stream, str(total)],
            timeout_seconds=60,
            max_output_bytes=bound,
        )
    )


def test_output_within_bound_is_kept_whole():
    res = _flood("stdout", 100_000, 1_000_000)
    assert res.ok and not res.stdout_truncated and not res.stderr_truncated
    assert res.stdout.count("\n") == 100_000 // 1024 + 1
    assert res.stdout.endswith("TAIL-MARKER\n")
    assert res.stdout_tail == res.stdout


def test_stdout_past_bound_keeps_head_and_tail():
    bound = 64 * 1024
    res = _flood("stdout", 20 * 1024 * 1024, bound)
    assert res.exit_code == 0 and res.stdout_truncated and not res.stderr_truncated
    assert not res.ok
    with pytest.raises(ExecutionError, match="truncated"):
        res.raise_if_failed()
    # The kept text is head + marker + tail: about the bound, never the 20 MiB.
    assert len(res.stdout) < bound + 500
    assert res.stdout.startswith("x" * 1023 + "\n")
    assert res.stdout.endswith("TAIL-MARKER\n")
    assert "bytes of stdout omitted" in res.stdout
    # The tail is the part captured contiguously up to EOF: it is where a
    # CONTROL_RESULT lives, it starts after the marker and it is intact.
    tail = res.stdout_tail
    assert tail.endswith("TAIL-MARKER\n") and "omitted" not in tail
    assert bound // 2 - 1024 <= len(tail) <= bound // 2


def test_stderr_past_bound_is_truncated_independently():
    res = _flood("stderr", 2 * 1024 * 1024, 32 * 1024)
    assert res.stderr_truncated and not res.stdout_truncated
    assert res.stderr.endswith("TAIL-MARKER\n") and "bytes of stderr omitted" in res.stderr
    assert res.stdout == "" and res.stdout_tail == ""


def test_timeout_with_flooded_output_still_returns():
    res = execute(
        ExecutionRequest(
            command=[
                PY,
                "-c",
                "import sys, time\n"
                "sys.stdout.buffer.write(b'y' * 300_000); sys.stdout.flush()\n"
                "time.sleep(30)",
            ],
            timeout_seconds=1,
            max_output_bytes=16 * 1024,
        )
    )
    assert res.timed_out and res.stdout_truncated
    assert len(res.stdout) < 20 * 1024


def test_bound_below_one_read_is_honoured():
    """A bound smaller than the read chunk (PR #84 review, P2): the kept text
    is one head byte, the marker and one tail byte."""
    res = _flood("stdout", 1_000_000, 2)
    assert res.stdout_truncated and not res.ok
    assert res.stdout.startswith("x\n[autoforge: ") and res.stdout.endswith(" were kept]\n\n")
    assert res.stdout_tail == "\n"


def test_bounded_buffer_never_retains_more_than_the_limit():
    """Whatever the chunking -- one read far larger than the bound included --
    the buffer holds at most ``limit`` bytes at every step and still yields
    the first and last bytes of the stream."""
    rng = random.Random(53)
    for limit in (1, 2, 3, 7, 100, 4096):
        buf = _BoundedBuffer(limit, "stdout")
        stream = b""
        for _ in range(40):
            chunk = bytes(rng.randrange(256) for _ in range(rng.choice((1, 5, 100, 8192))))
            buf.feed(chunk)
            stream += chunk
            assert buf.retained <= limit, (limit, len(stream))
        head, tail = limit - limit // 2, limit // 2
        got = buf.captured()
        assert got.truncated
        expected_tail = stream[len(stream) - tail :] if tail else b""
        assert got.text.startswith(stream[:head].decode("utf-8", errors="replace"))
        assert got.text[got.tail_offset :] == expected_tail.decode("utf-8", errors="replace")


def test_bounded_buffer_is_exact_under_fragmented_input():
    """Thousands of one-to-three-byte reads (the tail ring wraps on nearly
    every one): the kept head and tail are exactly the first and last bytes
    of the stream, for limits where the tail is empty, one byte, or larger."""
    rng = random.Random(84)
    for limit in (1, 2, 3, 7, 64, 1000):
        buf = _BoundedBuffer(limit, "stdout")
        stream = bytearray()
        for _ in range(3000):
            chunk = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 4)))
            buf.feed(chunk)
            stream += chunk
            assert buf.retained <= limit
        head, tail = limit - limit // 2, limit // 2
        got = buf.captured()
        assert got.truncated
        assert got.text.startswith(stream[:head].decode("utf-8", errors="replace"))
        expected_tail = bytes(stream[len(stream) - tail :]) if tail else b""
        assert got.text[got.tail_offset :] == expected_tail.decode("utf-8", errors="replace")


def test_bounded_buffer_memory_is_the_limit_plus_a_constant():
    """The bound is on memory, not only on payload length (PR #84 review,
    P3): a writer that causes one-byte reads must not cost a per-chunk
    object each. With a deque of chunks this stream cost about forty times
    the limit; the ring costs the limit plus allocator slack."""
    limit = 400_000
    total = 3 * limit
    tracemalloc.start()
    try:
        buf = _BoundedBuffer(limit, "stdout")
        tracemalloc.reset_peak()
        baseline = tracemalloc.get_traced_memory()[0]
        for i in range(total):
            buf.feed(bytes((i & 0xFF,)))
        current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert buf.retained == limit
    assert current - baseline < limit * 1.5, current - baseline
    assert peak - baseline < limit * 1.5, peak - baseline
    got = buf.captured()
    expected_tail = bytes(i & 0xFF for i in range(total - limit // 2, total))
    assert got.truncated
    assert got.text[got.tail_offset :] == expected_tail.decode("utf-8", errors="replace")


def test_non_positive_bound_is_refused():
    with pytest.raises(ExecutionError, match="max_output_bytes"):
        execute(ExecutionRequest(command=[PY, "-c", "pass"], max_output_bytes=0))


# -- allow-listed environment (#10) -------------------------------------------------
def test_select_environment_takes_exact_names_and_prefixes_only():
    source = {
        "PATH": "/bin",
        "HOME": "/h",
        "ANTHROPIC_API_KEY": "k",
        "ANTHROPIC_BASE_URL": "u",
        "ANTHROPICS": "not a prefix match",
        "AWS_SECRET_ACCESS_KEY": "s",
    }
    got = executor.select_environment(["PATH", "ANTHROPIC_*", "MISSING"], source=source)
    assert got == {"PATH": "/bin", "ANTHROPIC_API_KEY": "k", "ANTHROPIC_BASE_URL": "u"}
    assert list(got) == ["PATH", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"]  # source order
    assert executor.select_environment([], source=source) == {}
    only = executor.select_environment(["ANTHROPICS"], source=source)
    assert only == {"ANTHROPICS": "not a prefix match"}


@pytest.mark.parametrize("bad", ["", "*", "A*B", "A-B", "1ABC", "A B", "A**"])
def test_select_environment_refuses_an_invalid_entry(bad):
    assert not executor.is_env_pattern(bad)
    with pytest.raises(ExecutionError, match="invalid environment allow-list entry"):
        executor.select_environment(["PATH", bad], source={"PATH": "/bin"})


def test_select_environment_defaults_to_this_process(monkeypatch):
    monkeypatch.setenv("AUTOFORGE_TEST_ONLY_X", "1")
    assert executor.select_environment(["AUTOFORGE_TEST_ONLY_*"]) == {"AUTOFORGE_TEST_ONLY_X": "1"}


_DUMP_ENV = "import json, os; print(json.dumps(dict(os.environ)))"


def _child_env(**kw) -> dict:
    import json

    res = execute(ExecutionRequest(command=[PY, "-c", _DUMP_ENV], timeout_seconds=30, **kw))
    assert res.ok, res.stderr
    return json.loads(res.stdout)


def test_execute_without_an_allowlist_inherits_the_whole_environment(monkeypatch):
    monkeypatch.setenv("AUTOFORGE_TEST_SECRET", "s3cret")
    assert _child_env()["AUTOFORGE_TEST_SECRET"] == "s3cret"


def test_execute_with_an_allowlist_starts_the_child_from_only_those_names(monkeypatch):
    monkeypatch.setenv("AUTOFORGE_TEST_SECRET", "s3cret")
    monkeypatch.setenv("AUTOFORGE_TEST_KEEP_A", "a")
    monkeypatch.setenv("AUTOFORGE_TEST_KEEP_B", "b")
    child = _child_env(env_allowlist=("PATH", "AUTOFORGE_TEST_KEEP_*"))
    assert "AUTOFORGE_TEST_SECRET" not in child
    assert child["AUTOFORGE_TEST_KEEP_A"] == "a" and child["AUTOFORGE_TEST_KEEP_B"] == "b"
    assert set(child) <= {"PATH", "AUTOFORGE_TEST_KEEP_A", "AUTOFORGE_TEST_KEEP_B"} | _PYTHON_OWN
    # An empty tuple is a real (empty) allow-list, not "inherit everything".
    assert set(_child_env(env_allowlist=())) <= _PYTHON_OWN


# Variables the interpreter itself may add to a child that started from nothing.
_PYTHON_OWN = {"LC_CTYPE", "PYTHONIOENCODING", "__CF_USER_TEXT_ENCODING"}


def test_execute_layers_explicit_env_over_the_allowlist(monkeypatch):
    monkeypatch.setenv("AUTOFORGE_TEST_KEEP_A", "a")
    child = _child_env(
        env_allowlist=("AUTOFORGE_TEST_KEEP_A",),
        env={"AUTOFORGE_TEST_KEEP_A": "override", "AUTOFORGE_TEST_ADDED": "x"},
    )
    assert child["AUTOFORGE_TEST_KEEP_A"] == "override" and child["AUTOFORGE_TEST_ADDED"] == "x"


def test_execute_refuses_to_launch_on_an_invalid_allowlist_entry():
    with pytest.raises(ExecutionError, match="invalid environment allow-list entry"):
        execute(ExecutionRequest(command=[PY, "-c", "pass"], env_allowlist=("not valid",)))


# -- orphan containment (ADR 0002 amendment, #132) ----------------------------

_linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the child subreaper is Linux-only"
)

# A child that starts a process the way Pi's bash tool starts every command
# (detached: a session of its own) with its stdio on /dev/null, so it holds
# none of the child's pipes, prints its pid, and exits (or lingers). Neither
# the group kill nor the pipe EOF can see it.
_DETACHED = (
    "import subprocess, sys, time\n"
    "p = subprocess.Popen([sys.executable, '-c', 'import signal, time; '\n"
    "                      + ('signal.signal(signal.SIGTERM, signal.SIG_IGN); '\n"
    "                         if 'deaf' in sys.argv[1:] else '')\n"
    "                      + 'time.sleep(60)'],\n"
    "                     start_new_session=True, stdin=subprocess.DEVNULL,\n"
    "                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "print('child done', p.pid, flush=True)\n"
    "if 'linger' in sys.argv[1:]:\n"
    "    time.sleep(60)\n"
)

# A detached intermediary (a bash-tool command) that backgrounds a process
# (``server &``) into its own group, writes the pid to a file and returns:
# the backgrounded process outlives a parent that the agent had detached.
_BACKGROUNDED = (
    "import subprocess, sys, time\n"
    "inner = (\n"
    "    'import subprocess, sys; '\n"
    '    \'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], \'\n'
    "    'stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); '\n"
    "    'open(sys.argv[1], \"w\").write(str(p.pid))'\n"
    ")\n"
    "d = subprocess.Popen([sys.executable, '-c', inner, sys.argv[1]], start_new_session=True,\n"
    "                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
    "                     stderr=subprocess.DEVNULL)\n"
    "d.wait()\n"
    "print('child done', flush=True)\n"
)


def _subreaper() -> int:
    import ctypes

    value = ctypes.c_int(-1)
    ctypes.CDLL(None, use_errno=True).prctl(37, ctypes.byref(value), 0, 0, 0)
    return value.value


def test_detached_orphan_outlives_an_uncontained_invocation_unreported(monkeypatch):
    """The gap the amendment closes: a detached process that holds none of
    the pipes is invisible to the group check and the pipe EOF, so without
    containment ``execute`` reports a clean exit and the process lives on."""
    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 0.5)
    res = execute(ExecutionRequest(command=[PY, "-c", _DETACHED], timeout_seconds=30))
    pid = _orphan_pid(res)
    try:
        assert res.ok and res.leftovers == ""
        assert not (res.orphans_killed or res.orphan_survived_kill or res.orphans_unchecked)
        assert not _gone(pid, within=0.1)
    finally:
        _kill_quietly(pid)


@_linux_only
def test_contained_detached_orphan_is_killed_after_a_normal_exit(monkeypatch):
    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    started = time.monotonic()
    res = execute(
        ExecutionRequest(command=[PY, "-c", _DETACHED], timeout_seconds=30, contain_orphans=True)
    )
    elapsed = time.monotonic() - started
    pid = _orphan_pid(res)
    try:
        assert res.ok and res.exit_code == 0 and res.stdout.startswith("child done ")
        assert res.orphans_killed and not (res.orphan_survived_kill or res.orphans_unchecked)
        # Its group was empty and its pipes closed: only the orphan is reported.
        assert not (res.descendants_killed or res.group_survived_kill or res.capture_abandoned)
        assert "outside its process group" in res.leftovers
        assert "left processes behind" not in res.leftovers
        assert elapsed < 5, elapsed
        assert _gone(pid, within=0.5), "the detached orphan outlived execute()"
        assert _subreaper() == 0, "the subreaper setting was not restored"
    finally:
        _kill_quietly(pid)


@_linux_only
def test_contained_setsid_writer_is_killed_and_the_capture_completes(monkeypatch):
    """The ``setsid`` writer that holds the pipes, which an uncontained
    invocation must abandon (see above), is an orphan here: it is killed,
    the pipes reach EOF and nothing is abandoned."""
    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    res = execute(
        ExecutionRequest(
            command=[PY, "-c", _ORPHAN, "setsid", "0"], timeout_seconds=30, contain_orphans=True
        )
    )
    pid = _orphan_pid(res)
    try:
        assert res.ok and res.orphans_killed and res.descendants_killed
        assert not (res.capture_abandoned or res.group_survived_kill or res.orphan_survived_kill)
        assert _gone(pid, within=0.5)
    finally:
        _kill_quietly(pid)


@_linux_only
def test_contained_process_backgrounded_by_a_detached_child_is_killed(monkeypatch, tmp_path):
    """A detached command that backgrounds a process and returns: the
    backgrounded process is re-parented to the controller and killed."""
    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    pid_file = tmp_path / "pid"
    res = execute(
        ExecutionRequest(
            command=[PY, "-c", _BACKGROUNDED, str(pid_file)],
            timeout_seconds=30,
            contain_orphans=True,
        )
    )
    pid = int(pid_file.read_text())
    try:
        assert res.ok and res.stdout == "child done\n"
        assert res.orphans_killed and not res.orphan_survived_kill
        assert _gone(pid, within=0.5), "the backgrounded process outlived execute()"
    finally:
        _kill_quietly(pid)


@_linux_only
def test_contained_orphan_is_killed_on_timeout_and_sigterm_escalates(monkeypatch):
    """On the timeout path the orphans get the group's SIGTERM and, when one
    ignores it, the SIGKILL; the result is a timeout with the orphans named."""
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    started = time.monotonic()
    res = execute(
        ExecutionRequest(
            command=[PY, "-c", _DETACHED, "deaf", "linger"],
            timeout_seconds=1,
            contain_orphans=True,
        )
    )
    elapsed = time.monotonic() - started
    pid = _orphan_pid(res)
    try:
        assert res.timed_out and res.exit_code == -1
        assert res.orphans_killed and not res.orphan_survived_kill
        assert not res.descendants_killed
        assert elapsed < 5, elapsed  # the timeout plus the two kill graces
        assert _gone(pid, within=0.5)
    finally:
        _kill_quietly(pid)


@_linux_only
def test_contained_clean_run_reports_nothing_and_leaves_other_children_alone():
    """A command that leaves nothing reports nothing, and a child the
    controller already had (an earlier invocation's survivor, say) is not
    mistaken for an orphan of this one."""
    import subprocess

    before = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        res = execute(
            ExecutionRequest(
                command=[PY, "-c", "print('hi')"], timeout_seconds=30, contain_orphans=True
            )
        )
        assert res.ok and res.stdout == "hi\n" and res.leftovers == ""
        assert not (res.orphans_killed or res.orphan_survived_kill or res.orphans_unchecked)
        assert before.poll() is None, "a pre-existing child was killed as an orphan"
        assert _subreaper() == 0
    finally:
        before.kill()
        before.wait()


# A child that orphans ``argv[1]`` short-lived processes the way a test suite
# run by an agent does: each is started by an intermediary that exits at once,
# so it is re-parented to the controller and dies as the controller's child;
# every other one leaves the child's group first (``setsid``). One more orphan
# lives on, detached. The child writes that one's pid to ``argv[2]``, runs on
# until ``argv[3]`` exists, and exits 7.
_ORPHAN_FACTORY = (
    "import os, sys, time\n"
    "def orphan(seconds, detach):\n"
    "    r, w = os.pipe()\n"
    "    if os.fork() == 0:\n"
    "        pid = os.fork()\n"
    "        if pid == 0:\n"
    "            os.close(r); os.close(w)\n"
    "            if detach:\n"
    "                os.setsid()\n"
    "            null = os.open(os.devnull, os.O_RDWR)\n"
    "            for fd in (0, 1, 2):\n"
    "                os.dup2(null, fd)\n"
    "            time.sleep(seconds)\n"
    "            os._exit(0)\n"
    "        os.write(w, str(pid).encode())\n"
    "        os._exit(0)\n"
    "    os.close(w)\n"
    "    os.wait()\n"
    "    pid = int(os.read(r, 32))\n"
    "    os.close(r)\n"
    "    return pid\n"
    "for i in range(int(sys.argv[1])):\n"
    "    orphan(0.01, detach=i % 2 == 1)\n"
    "kept = orphan(60, detach=True)\n"
    "with open(sys.argv[2] + '.tmp', 'w') as fh:\n"
    "    fh.write(str(kept))\n"
    "os.rename(sys.argv[2] + '.tmp', sys.argv[2])\n"
    "deadline = time.monotonic() + 30\n"
    "while not os.path.exists(sys.argv[3]) and time.monotonic() < deadline:\n"
    "    time.sleep(0.02)\n"
    "sys.exit(7)\n"
)


def _new_zombies(before: frozenset[tuple[int, int]]) -> int:
    """This process's dead, unreaped children that were not there ``before``."""
    return sum(1 for key, zombie in executor._children().items() if zombie and key not in before)


def _running(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


@_linux_only
def test_contained_dead_orphans_are_reaped_while_the_child_runs(monkeypatch, tmp_path):
    """The controller is the orphans' parent for the whole invocation, so it
    reaps them as they die, not only once the child has exited (#191). A long
    agent run whose tools orphan thousands of short-lived processes (a test
    suite) otherwise piles up zombies, which count against the session's
    pids.max until no fork succeeds and the agent itself aborts. Reaping
    signals nothing: a live orphan is left alone until the teardown kills it.
    The child's own exit status is never taken by the reaper, and the reaper
    ends with the invocation."""
    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    kept_file, go = tmp_path / "kept", tmp_path / "go"
    before = frozenset(executor._children())
    seen: list[int] = []
    kept: list[int] = []
    kept_ran = []

    def watch() -> None:
        deadline = time.monotonic() + 20
        while not kept_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        if kept_file.exists():
            kept.append(int(kept_file.read_text()))
        # Every short-lived orphan has died and the child still runs (it
        # waits for ``go``).
        settle = time.monotonic() + 3
        while True:
            seen.append(_new_zombies(before))
            if seen[-1] == 0 or time.monotonic() >= settle:
                break
            time.sleep(0.05)
        kept_ran.extend(_running(pid) for pid in kept)
        go.touch()

    watcher = threading.Thread(target=watch)
    watcher.start()
    try:
        res = execute(
            ExecutionRequest(
                command=[PY, "-c", _ORPHAN_FACTORY, "200", str(kept_file), str(go)],
                timeout_seconds=30,
                contain_orphans=True,
            )
        )
    finally:
        go.touch()
        watcher.join()
    try:
        assert res.exit_code == 7 and not res.timed_out
        assert seen and seen[-1] == 0, f"dead orphans left unreaped while the child ran: {seen}"
        assert kept_ran == [True], "a live orphan was killed while the child ran"
        # Only the orphan still alive at the end was killed.
        assert res.orphans_killed and not res.orphan_survived_kill
        assert not (res.descendants_killed or res.group_survived_kill or res.capture_abandoned)
        assert _gone(kept[0], within=1)
        assert _new_zombies(before) == 0
        assert _subreaper() == 0
        assert not any(thread.name == "autoforge-reaper" for thread in threading.enumerate())
    finally:
        for pid in kept:
            _kill_quietly(pid)


@_linux_only
def test_contained_reaper_leaves_a_pre_existing_childs_status_to_its_owner():
    """A child the controller already had when the invocation began is not
    one of its orphans: when it dies meanwhile, its exit status is left for
    whoever started it."""
    import subprocess

    before = subprocess.Popen([PY, "-c", "import time; time.sleep(0.2); raise SystemExit(5)"])
    try:
        res = execute(
            ExecutionRequest(
                command=[PY, "-c", "import time; time.sleep(1)"],
                timeout_seconds=30,
                contain_orphans=True,
            )
        )
        assert res.ok and res.leftovers == ""
        assert before.wait(timeout=10) == 5
    finally:
        before.kill()
        before.wait()


@_linux_only
def test_a_reaper_that_cannot_start_refuses_the_launch_before_the_spawn(monkeypatch, tmp_path):
    """A reaper thread the system refuses (a thread limit) fails the launch as
    an ExecutionError before anything is spawned, with the subreaper setting
    restored and the next contained invocation free to run."""
    real_start = threading.Thread.start

    def refuse_the_reaper(self):
        if self.name == "autoforge-reaper":
            raise RuntimeError("can't start new thread")
        return real_start(self)

    spawned = tmp_path / "spawned"
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", refuse_the_reaper)
        with pytest.raises(ExecutionError, match="failed to start the orphan reaper"):
            execute(
                ExecutionRequest(
                    command=[PY, "-c", f"open({str(spawned)!r}, 'w')"], contain_orphans=True
                )
            )
    assert not spawned.exists(), "the command ran without its reaper"
    assert _subreaper() == 0
    assert execute(ExecutionRequest(command=[PY, "-c", "pass"], contain_orphans=True)).ok


@_linux_only
def test_a_second_contained_invocation_at_once_is_refused():
    first = executor._Containment.begin()
    assert first is not None
    try:
        with pytest.raises(ExecutionError, match="already running"):
            execute(ExecutionRequest(command=[PY, "-c", "pass"], contain_orphans=True))
    finally:
        first.release()
    assert _subreaper() == 0
    assert execute(ExecutionRequest(command=[PY, "-c", "pass"], contain_orphans=True)).ok


def test_without_a_subreaper_the_limit_is_reported(monkeypatch):
    """Where the platform has no child subreaper the orphan is neither seen
    nor killed, and the result says the check could not be made."""
    monkeypatch.setattr(executor.sys, "platform", "darwin")
    assert executor._prctl() is None
    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 0.5)
    res = execute(
        ExecutionRequest(command=[PY, "-c", _DETACHED], timeout_seconds=30, contain_orphans=True)
    )
    pid = _orphan_pid(res)
    try:
        assert res.ok and res.orphans_unchecked
        assert not (res.orphans_killed or res.orphan_survived_kill)
        assert "could not be checked on this platform" in res.leftovers
        assert not _gone(pid, within=0.1)
    finally:
        _kill_quietly(pid)
