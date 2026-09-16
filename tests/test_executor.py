"""Executor: real subprocesses (python -c) — argv safety, timeout, exit codes."""

import sys

import pytest

from autoforge.errors import ExecutionError
from autoforge.executor import ExecutionRequest, execute

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


def test_non_positive_bound_is_refused():
    with pytest.raises(ExecutionError, match="max_output_bytes"):
        execute(ExecutionRequest(command=[PY, "-c", "pass"], max_output_bytes=0))
