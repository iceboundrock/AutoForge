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
