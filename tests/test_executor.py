"""Executor: real subprocesses (python -c) — argv safety, timeout, exit codes."""

import os
import random
import signal
import sys
import time
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


# A child that spawns a descendant which inherits stdout/stderr, prints the
# descendant's pid and exits at once; the descendant sleeps far past the
# timeout. ``setsid`` makes it leave the child's process group as well.
_ORPHAN = (
    "import subprocess, sys\n"
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
    "                     start_new_session=sys.argv[1] == 'setsid')\n"
    "print('child done', p.pid, flush=True)\n"
)


def _orphan_pid(res) -> int:
    return int(res.stdout.split()[-1])


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


def test_descendant_holding_the_pipe_is_bounded_by_the_timeout():
    """The child exits at once but a descendant that inherited its pipes keeps
    them open (PR #84 review, P1): the invocation is not over until EOF, that
    wait runs under the same timeout, and at the timeout the group is killed
    so the descendant does not outlive the invocation."""
    started = time.monotonic()
    res = execute(ExecutionRequest(command=[PY, "-c", _ORPHAN, "group"], timeout_seconds=1))
    elapsed = time.monotonic() - started
    assert res.timed_out and not res.ok and res.exit_code == -1
    assert res.stdout.startswith("child done ")
    assert elapsed < 10, elapsed  # the timeout plus the SIGTERM grace, never the 60 s sleep
    assert _gone(_orphan_pid(res), within=5)


def test_descendant_outside_the_group_cannot_hold_the_capture_open(monkeypatch):
    """A descendant that also called ``setsid`` cannot be reached by the group
    kill; the capture is abandoned after the grace period rather than waited
    for, so ``execute`` still returns within a bounded time."""
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    started = time.monotonic()
    res = execute(ExecutionRequest(command=[PY, "-c", _ORPHAN, "setsid"], timeout_seconds=1))
    elapsed = time.monotonic() - started
    pid = _orphan_pid(res)
    try:
        assert res.timed_out and res.exit_code == -1
        assert res.stdout.startswith("child done ")
        assert elapsed < 5, elapsed
        assert not _gone(pid, within=0.1)  # nothing here can kill it; it is not waited for
    finally:
        os.kill(pid, signal.SIGKILL)


def test_stdin_is_closed_not_interactive():
    res = execute(
        ExecutionRequest(
            command=[PY, "-c", "import sys; print(repr(sys.stdin.read()))"], timeout_seconds=30
        )
    )
    assert res.ok and res.stdout.strip() == "''"


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


def test_non_positive_bound_is_refused():
    with pytest.raises(ExecutionError, match="max_output_bytes"):
        execute(ExecutionRequest(command=[PY, "-c", "pass"], max_output_bytes=0))
