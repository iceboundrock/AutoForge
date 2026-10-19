"""The supervision handle (#182): spawn, signal, poll and wait, with no workflow semantics."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from autoforge.errors import ExecutionError
from autoforge.executor_supervised import (
    SupervisedChild,
    process_start_time,
    spawn_supervised,
)

# A child that reports what it inherited, then waits for its gate file. It
# installs Python's SIGINT handler itself, as the worker does: one started by
# a parent that ignores SIGINT (a background job) would inherit that.
_REPORT = r"""
import json, os, signal, sys, time
signal.signal(signal.SIGINT, signal.default_int_handler)
fds = sorted(int(n) for n in os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else []
info = {
    "sid": os.getsid(0),
    "pid": os.getpid(),
    "stdin_is_devnull": os.path.samestat(os.fstat(0), os.stat(os.devnull)),
    "cwd": os.getcwd(),
    "env": os.environ.get("SUPERVISED_TEST", ""),
    "fds": fds,
}
sys.stdout.write(json.dumps(info) + "\n")
sys.stdout.flush()
sys.stderr.write("to stderr\n")
sys.stderr.flush()
gate = sys.argv[1]
deadline = time.monotonic() + 30
while not os.path.exists(gate) and time.monotonic() < deadline:
    time.sleep(0.01)
"""


def _report(log_path, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = log_path.read_text() if log_path.exists() else ""
        if "\n" in text:
            return json.loads(text.splitlines()[0])
        time.sleep(0.01)
    raise AssertionError("the child reported nothing")


def _spawn(tmp_path, *, pass_fds=(), script: str = _REPORT, args=()):
    log_path = tmp_path / "child.log"
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o600)
    try:
        child = spawn_supervised(
            [sys.executable, "-c", script, *args],
            pass_fds=pass_fds,
            stdout_fd=fd,
            cwd=tmp_path,
            env={**os.environ, "SUPERVISED_TEST": "yes"},
        )
    finally:
        os.close(fd)
    return child, log_path


def test_child_runs_in_its_own_session_with_only_the_passed_descriptors(tmp_path):
    gate = tmp_path / "gate"
    passed_r, passed_w = os.pipe()
    kept_r, kept_w = os.pipe()  # not passed: the child never sees it
    try:
        child, log = _spawn(tmp_path, pass_fds=(passed_r,), args=(str(gate),))
        info = _report(log)
        gate.touch()
        assert child.wait(time.monotonic() + 30) == 0
    finally:
        for fd in (passed_r, passed_w, kept_r, kept_w):
            os.close(fd)
    assert info["sid"] == info["pid"] != os.getsid(0)  # a session of its own
    assert info["stdin_is_devnull"] is True
    assert info["cwd"] == str(tmp_path)
    assert info["env"] == "yes"
    if info["fds"]:  # /proc is there: exactly 0, 1, 2 and the passed one (+ the listing's own)
        assert passed_r in info["fds"]
        assert (
            kept_r not in info["fds"] and kept_w not in info["fds"] and passed_w not in info["fds"]
        )
    # stdout and stderr both go to the one log, with no pipe to the parent.
    assert "to stderr" in log.read_text()


def test_wait_honours_the_deadline_and_poll_reaps(tmp_path):
    gate = tmp_path / "gate"
    child, log = _spawn(tmp_path, args=(str(gate),))
    _report(log)
    start = time.monotonic()
    assert child.wait(start + 0.2) is None
    assert child.poll() is None and child.returncode is None
    assert child.wait(start - 1) is None  # a past deadline is one poll
    gate.touch()
    assert child.wait(time.monotonic() + 30) == 0
    assert child.returncode == 0 and child.poll() == 0


def test_signal_interrupts_and_is_refused_once_reaped(tmp_path):
    gate = tmp_path / "gate"
    child, log = _spawn(tmp_path, args=(str(gate),))
    _report(log)
    assert child.signal(signal.SIGINT) is True
    status = child.wait(time.monotonic() + 30)
    assert status is not None and status != 0
    # Reaped: its pid may belong to anyone now, so nothing is sent.
    assert child.signal(signal.SIGINT) is False


def test_sigkill_is_never_sent(tmp_path):
    gate = tmp_path / "gate"
    child, log = _spawn(tmp_path, args=(str(gate),))
    _report(log)
    try:
        with pytest.raises(ValueError, match="SIGKILL"):
            child.signal(signal.SIGKILL)
        assert child.poll() is None  # still running: nothing was sent
    finally:
        gate.touch()
        assert child.wait(time.monotonic() + 30) == 0


def test_a_failed_start_is_an_execution_error(tmp_path):
    fd = os.open(tmp_path / "log", os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        with pytest.raises(ExecutionError, match="failed to start"):
            spawn_supervised(
                [str(tmp_path / "missing")], pass_fds=(), stdout_fd=fd, cwd=tmp_path, env={}
            )

        def refusing(*args, **kwargs):
            raise subprocess.SubprocessError("injected")

        with pytest.raises(ExecutionError, match="injected"):
            spawn_supervised(
                [sys.executable], pass_fds=(), stdout_fd=fd, cwd=tmp_path, env={}, popen=refusing
            )
        # Nothing is looked up on PATH.
        with pytest.raises(ExecutionError, match="absolute path"):
            spawn_supervised(["python3"], pass_fds=(), stdout_fd=fd, cwd=tmp_path, env={})
    finally:
        os.close(fd)


def test_popen_gets_the_supervision_options(tmp_path):
    seen: dict = {}

    class Recorded:
        pid = 4242
        returncode = None

        def poll(self):
            return None

    def popen(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        return Recorded()

    child = spawn_supervised(
        [sys.executable, "-c", "pass"],
        pass_fds=[7, 9],
        stdout_fd=5,
        cwd=tmp_path,
        env={"A": "1"},
        popen=popen,
    )
    assert isinstance(child, SupervisedChild) and child.pid == 4242
    assert seen["start_new_session"] is True and seen["close_fds"] is True
    assert seen["pass_fds"] == (7, 9)
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["stdout"] == 5 and seen["stderr"] == 5
    assert seen["env"] == {"A": "1"} and seen["cwd"] == str(tmp_path)
    assert "shell" not in seen


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="needs /proc")
def test_process_start_time_names_one_process():
    own = process_start_time(os.getpid())
    assert isinstance(own, int) and own > 0
    assert process_start_time(os.getpid()) == own
    assert process_start_time(2**22 + 12345) is None  # no such process
