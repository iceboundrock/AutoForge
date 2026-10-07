"""Duplex child handle (#130): real local Python children, no Pi.

Framing, bounded writes and reads, the deadline, and teardown on every exit
path, held to ADR 0002 exactly as ``execute()`` is.
"""

import ast
import ctypes
import hashlib
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from autoforge import executor, executor_duplex
from autoforge.errors import ChildStdinClosedError, ExecutionError, ExecutionTimeoutError
from autoforge.executor import ExecutionRequest, execute
from autoforge.executor_duplex import (
    MAX_DEADLINE_SECONDS,
    DuplexRequest,
    Eof,
    Fragment,
    Overflow,
    Oversize,
    Record,
    StdoutMode,
    Timeout,
    _LineFramer,
    start_duplex,
)

from .test_executor import _baseline_fds, _open_fds

PY = sys.executable

# Writes each chunk of ``argv[1]`` (a Python literal list of bytes) to stdout
# with a flush and ``argv[2]`` seconds between chunks, then waits for stdin
# EOF before exiting, so the stream ends when the caller says so.
_WRITER = (
    "import ast, sys, time\n"
    "out = sys.stdout.buffer\n"
    "for chunk in ast.literal_eval(sys.argv[1]):\n"
    "    out.write(chunk); out.flush()\n"
    "    time.sleep(float(sys.argv[2]))\n"
    "sys.stdin.buffer.read()\n"
)

# Echoes every stdin record as ``<length>:<sha256>`` until stdin EOF, then
# exits with ``argv[1]``.
_ECHO = (
    "import hashlib, sys\n"
    "for line in sys.stdin.buffer:\n"
    "    body = line[:-1] if line.endswith(b'\\n') else line\n"
    "    digest = hashlib.sha256(body).hexdigest().encode()\n"
    "    sys.stdout.buffer.write(b'%d:%s\\n' % (len(body), digest))\n"
    "    sys.stdout.buffer.flush()\n"
    "sys.exit(int(sys.argv[1]))\n"
)


def _writer(chunks: list[bytes], delay: float = 0.0, **kw) -> DuplexRequest:
    return DuplexRequest(
        command=[PY, "-c", _WRITER, repr(chunks), str(delay)], deadline_seconds=30, **kw
    )


def _drain(child, timeout: float = 10) -> list:
    """Every outcome up to and including the end (Eof/Overflow/Timeout)."""
    got = []
    while True:
        item = child.read_line(timeout=timeout)
        got.append(item)
        if isinstance(item, Eof | Overflow | Timeout):
            return got


def _records_until_eof(chunks: list[bytes], delay: float = 0.0, **kw) -> list:
    """Read everything the writer produces, then let it exit."""
    with start_duplex(_writer(chunks, delay, **kw)) as child:
        expected = sum(chunk.count(b"\n") for chunk in chunks)
        got = [child.read_line(timeout=10) for _ in range(expected)]
        child.close_stdin()
        got += _drain(child)
        res = child.finish()
    assert res.exit_code == 0 and not res.timed_out, res
    return got


def _gone(pid: int, within: float) -> bool:
    """True once ``pid`` no longer runs (a zombie awaiting init counts as gone)."""
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


# -- framing -------------------------------------------------------------------
def test_records_split_across_one_byte_writes():
    payload = b"first\nsecond record\n\nlast\n"
    got = _records_until_eof([bytes([b]) for b in payload], delay=0.001)
    assert got == [Record(b"first"), Record(b"second record"), Record(b""), Record(b"last"), Eof()]


def test_framer_reassembles_any_chunking_exactly():
    payload = b"alpha\r\nbeta\n\xe2\x80\xa8gamma\x00\ndelta"
    for size in (1, 2, 3, 7, len(payload)):
        framer = _LineFramer(64)
        got: list = []
        for i in range(0, len(payload), size):
            got += framer.feed(payload[i : i + size])
        got += framer.finish()
        assert got == [
            Record(b"alpha"),
            Record(b"beta"),
            Record(b"\xe2\x80\xa8gamma\x00"),
            Fragment(b"delta"),
        ], size


def test_many_records_in_one_write():
    payload = b"".join(b"record-%d\n" % i for i in range(500))
    got = _records_until_eof([payload])
    assert got == [Record(b"record-%d" % i) for i in range(500)] + [Eof()]


def test_crlf_endings_strip_exactly_one_cr():
    got = _records_until_eof([b"one\r\ntwo\r\r\n\r\nbare\rcr\n"])
    assert got == [Record(b"one"), Record(b"two\r"), Record(b""), Record(b"bare\rcr"), Eof()]


def test_unicode_line_separators_and_nul_are_not_boundaries():
    """U+2028/U+2029, NEL, VT, FF, the C0 separators and NUL all survive inside
    one record; only LF ends it (Pi's RPC framing, ADR 0003 §2.8)."""
    body = "a b c\x85d".encode() + b"\x0b\x0c\x1c\x1d\x1e\x00\x85end"
    got = _records_until_eof([body + b"\n"])
    assert got == [Record(body), Eof()]


def test_invalid_utf8_is_returned_as_bytes():
    got = _records_until_eof([b"ok \xff\xfe \xc3\x28 end\n"])
    assert got == [Record(b"ok \xff\xfe \xc3\x28 end"), Eof()]


def test_oversize_record_is_reported_and_the_next_one_is_read():
    limit = 16
    got = _records_until_eof(
        [
            b"x" * limit + b"\n",  # exactly the limit: a record
            b"y" * limit + b"\r\n",  # the limit plus the stripped CR: a record
            b"z" * (limit + 1) + b"\n",  # one byte over: oversize
            b"w" * 10_000 + b"\n",  # far over, across reads
            b"after\n",
        ],
        max_record_bytes=limit,
    )
    assert got == [
        Record(b"x" * limit),
        Record(b"y" * limit),
        Oversize(limit),
        Oversize(limit),
        Record(b"after"),
        Eof(),
    ]


def test_unterminated_fragment_at_eof_is_reported_not_returned_as_a_record():
    script = "import sys; sys.stdout.buffer.write(b'whole\\npartial')"
    with start_duplex(DuplexRequest(command=[PY, "-c", script], deadline_seconds=30)) as child:
        got = _drain(child)
        res = child.finish()
    assert got == [Record(b"whole"), Fragment(b"partial"), Eof()]
    assert res.exit_code == 0
    # Eof is sticky.
    assert child.read_line(timeout=0) == Eof()


# -- writes --------------------------------------------------------------------
def test_partial_and_large_writes_are_delivered_intact():
    """A record far larger than the pipe buffer is written in many partial
    non-blocking writes and arrives whole, between two small ones."""
    big = bytes(range(256)).replace(b"\n", b"") * 8192  # ~2 MiB, no LF
    records = [b"small", big, b"\x00\xff\r tail"]
    with start_duplex(DuplexRequest(command=[PY, "-c", _ECHO, "0"], deadline_seconds=30)) as child:
        for record in records:
            child.send_line(record)
        got = [child.read_line(timeout=10) for _ in records]
        child.close_stdin()
        assert child.read_line(timeout=10) == Eof()
        res = child.finish()
    # The child's line reader strips only the LF; a record ending in "\r tail"
    # keeps its CR, and the reply's sha proves every byte arrived.
    assert got == [
        Record(b"%d:%s" % (len(r), hashlib.sha256(r).hexdigest().encode())) for r in records
    ]
    assert res.exit_code == 0 and not res.timed_out and res.leftovers == ""


def test_record_containing_lf_is_refused():
    with start_duplex(DuplexRequest(command=[PY, "-c", _ECHO, "0"], deadline_seconds=30)) as child:
        with pytest.raises(ExecutionError, match="must not contain LF"):
            child.send_line(b"two\nrecords")
        child.finish()


def test_send_to_a_child_that_never_reads_is_bounded_by_the_deadline(monkeypatch):
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    started = time.monotonic()
    with start_duplex(
        DuplexRequest(command=[PY, "-c", "import time; time.sleep(60)"], deadline_seconds=1)
    ) as child:
        with pytest.raises(ExecutionTimeoutError, match="deadline"):
            child.send_line(b"x" * (8 * 1024 * 1024))
        res = child.finish()
        assert child.read_line(timeout=5) == Timeout(deadline_exceeded=True)
        with pytest.raises(ExecutionTimeoutError):
            child.send_line(b"more")
    elapsed = time.monotonic() - started
    assert res.timed_out and res.exit_code == -1
    assert not (res.descendants_killed or res.group_survived_kill or res.capture_abandoned)
    assert 1 <= elapsed < 3, elapsed
    assert not executor._group_alive(child.pid)


def test_child_that_closed_stdin_raises_the_typed_broken_pipe_error():
    script = "import os, time; os.close(0); print('closed', flush=True); time.sleep(1)"
    with start_duplex(DuplexRequest(command=[PY, "-c", script], deadline_seconds=30)) as child:
        assert child.read_line(timeout=10) == Record(b"closed")
        with pytest.raises(ChildStdinClosedError, match="closed its stdin"):
            child.send_line(b"hello")
        with pytest.raises(ExecutionError):
            # nothing the child could still read: the stream is over
            child.send_line(b"again")
        res = child.finish()
    assert res.exit_code == 0 and not res.timed_out
    assert not executor._group_alive(child.pid)


# -- bounds --------------------------------------------------------------------
def test_stdout_flood_without_newlines_is_oversize_with_bounded_memory():
    limit = 64 * 1024
    script = (
        "import sys\n"
        "for _ in range(320):\n"
        "    sys.stdout.buffer.write(b'x' * 65536)\n"
        "sys.stdout.buffer.write(b'\\nnext\\n'); sys.stdout.flush()\n"
        "sys.stdin.buffer.read()\n"
    )
    with start_duplex(
        DuplexRequest(command=[PY, "-c", script], deadline_seconds=30, max_record_bytes=limit)
    ) as child:
        assert child.read_line(timeout=10) == Oversize(limit)
        assert child.read_line(timeout=10) == Record(b"next")
        reader = child._readers[0]
        assert reader.framer.retained <= limit + 1
        child.close_stdin()
        assert child.read_line(timeout=10) == Eof()
        res = child.finish()
    assert res.exit_code == 0 and not res.records_overflowed


def test_framer_holds_at_most_the_limit_whatever_the_stream():
    limit = 1000
    framer = _LineFramer(limit)
    got = []
    for _ in range(2000):
        got += framer.feed(b"y" * 4096)
        assert framer.retained <= limit + 1
    got += framer.feed(b"\nok\n")
    assert got == [Oversize(limit), Record(b"ok")]
    assert framer.finish() == []


def test_record_queue_overflow_ends_the_invocation(monkeypatch):
    """The caller does not read while the child produces more records than the
    queue holds: the queued ones are returned, then Overflow (sticky); the
    child, still running, is killed at finish rather than awaited."""
    script = (
        "import sys\n"
        "for i in range(100):\n"
        "    sys.stdout.buffer.write(b'r%d\\n' % i)\n"
        "sys.stdout.flush()\n"
        "print('done', file=sys.stderr, flush=True)\n"
        "sys.stdin.buffer.read()\n"
        "import time; time.sleep(60)\n"
    )
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    started = time.monotonic()
    with start_duplex(
        DuplexRequest(command=[PY, "-c", script], deadline_seconds=30, max_pending_records=10)
    ) as child:
        while child._stderr.buffer.retained == 0:
            time.sleep(0.01)
        got = _drain(child)
        assert child.read_line(timeout=0) == Overflow()
        res = child.finish()
    assert got == [Record(b"r%d" % i) for i in range(10)] + [Overflow()]
    assert res.records_overflowed and res.exit_code == -1 and not res.timed_out
    assert time.monotonic() - started < 5
    assert not executor._group_alive(child.pid)


def test_record_queue_overflow_by_bytes():
    script = "import sys; sys.stdout.buffer.write((b'z' * 1000 + b'\\n') * 20)"
    with start_duplex(
        DuplexRequest(
            command=[PY, "-c", script],
            deadline_seconds=30,
            max_record_bytes=1000,
            max_pending_bytes=5000,
        )
    ) as child:
        assert child.finish().records_overflowed
    assert _drain(child) == [Record(b"z" * 1000)] * 5 + [Overflow()]


def test_stderr_flood_is_bounded_and_flagged():
    bound = 32 * 1024
    script = "import sys; sys.stderr.buffer.write(b'e' * 2_000_000 + b'\\nEND\\n')"
    with start_duplex(
        DuplexRequest(command=[PY, "-c", script], deadline_seconds=30, max_stderr_bytes=bound)
    ) as child:
        assert _drain(child) == [Eof()]
        res = child.finish()
    assert res.stderr_truncated and res.exit_code == 0
    assert len(res.stderr) < bound + 500 and res.stderr.endswith("END\n")


def test_stderr_never_appears_as_a_record():
    script = (
        "import sys\n"
        "print('err-line', file=sys.stderr, flush=True)\n"
        "print('out-line', flush=True)\n"
        "print('err-two', file=sys.stderr, flush=True)\n"
    )
    with start_duplex(DuplexRequest(command=[PY, "-c", script], deadline_seconds=30)) as child:
        got = _drain(child)
        res = child.finish()
    assert got == [Record(b"out-line"), Eof()]
    assert res.stderr == "err-line\nerr-two\n" and not res.stderr_truncated


# -- lifecycle -----------------------------------------------------------------
def test_orderly_close_stdin_then_exit_zero():
    with start_duplex(DuplexRequest(command=[PY, "-c", _ECHO, "0"], deadline_seconds=30)) as child:
        child.send_line(b"ping")
        assert isinstance(child.read_line(timeout=10), Record)
        child.close_stdin()
        child.close_stdin()  # idempotent
        assert child.read_line(timeout=10) == Eof()
        res = child.finish()
        assert child.finish() is res
    assert res.exit_code == 0 and not res.timed_out and not res.records_overflowed
    assert res.leftovers == "" and res.started_at <= res.finished_at
    with pytest.raises(ExecutionError, match="not open"):
        child.send_line(b"late")


def test_child_death_mid_conversation_gives_eof_and_the_real_exit_code():
    script = "import sys\nsys.stdin.buffer.readline()\nprint('bye', flush=True)\nsys.exit(7)\n"
    with start_duplex(DuplexRequest(command=[PY, "-c", script], deadline_seconds=30)) as child:
        child.send_line(b"hello")
        assert child.read_line(timeout=10) == Record(b"bye")
        assert child.read_line(timeout=10) == Eof()
        res = child.finish()
    assert res.exit_code == 7 and not res.timed_out and res.leftovers == ""


def test_per_call_read_timeout_leaves_the_child_running():
    with start_duplex(DuplexRequest(command=[PY, "-c", _ECHO, "0"], deadline_seconds=30)) as child:
        assert child.read_line(timeout=0.1) == Timeout(deadline_exceeded=False)
        child.send_line(b"still here")
        assert isinstance(child.read_line(timeout=10), Record)
        res = child.finish()
    assert res.exit_code == 0 and not res.timed_out


def test_child_ignoring_stdin_close_is_killed_at_the_deadline(monkeypatch):
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    started = time.monotonic()
    with start_duplex(
        DuplexRequest(command=[PY, "-c", "import time; time.sleep(60)"], deadline_seconds=1)
    ) as child:
        res = child.finish()
    elapsed = time.monotonic() - started
    assert res.timed_out and res.exit_code == -1
    assert not (res.descendants_killed or res.group_survived_kill or res.capture_abandoned)
    assert 1 <= elapsed < 3, elapsed
    assert child.read_line(timeout=1) == Timeout(deadline_exceeded=True)


def test_read_past_the_deadline_kills_the_group(monkeypatch):
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    with start_duplex(
        DuplexRequest(command=[PY, "-c", "import time; time.sleep(60)"], deadline_seconds=0.5)
    ) as child:
        assert child.read_line() == Timeout(deadline_exceeded=True)
        assert not executor._group_alive(child.pid)
        res = child.finish()
    assert res.timed_out and res.exit_code == -1


# A child that starts a descendant in its own group (both ignore stdin EOF),
# prints the descendant's pid and sleeps.
_FAMILY = (
    "import subprocess, sys, time\n"
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
    "                     stdin=subprocess.DEVNULL)\n"
    "print(p.pid, flush=True)\n"
    "time.sleep(60)\n"
)


@pytest.mark.parametrize("raised", [RuntimeError, KeyboardInterrupt])
def test_exception_inside_the_with_leaves_no_process_in_the_group(monkeypatch, raised):
    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 0.3)
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    started = time.monotonic()
    descendant = None
    with pytest.raises(raised):
        with start_duplex(DuplexRequest(command=[PY, "-c", _FAMILY], deadline_seconds=60)) as child:
            item = child.read_line(timeout=10)
            assert isinstance(item, Record)
            descendant = int(item.data)
            raise raised("boom")
    elapsed = time.monotonic() - started
    assert descendant is not None
    try:
        assert not executor._group_alive(child.pid)
        assert _gone(descendant, within=1)
        assert elapsed < 3, elapsed  # a short wait for exit and the kill, never the deadline
    finally:
        _kill_quietly(descendant)


def test_exception_lets_a_child_that_exits_on_stdin_eof_exit_by_itself(monkeypatch):
    """The exception path still closes stdin first: a child that exits on EOF
    is not killed (Pi exits 0 on stdin EOF and runs its cleanup)."""
    signalled: list[int] = []
    real_killpg = os.killpg

    def spying_killpg(pgid, sig):
        signalled.append(int(sig))
        return real_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", spying_killpg)
    with pytest.raises(RuntimeError):
        with start_duplex(
            DuplexRequest(command=[PY, "-c", _ECHO, "0"], deadline_seconds=30)
        ) as child:
            raise RuntimeError("boom")
    assert signal.SIGTERM not in signalled and signal.SIGKILL not in signalled
    assert not executor._group_alive(child.pid)


# A child that waits for stdin EOF, then starts a descendant that inherits its
# stdout/stderr (``setsid`` also leaves the group), prints the descendant's
# pid and exits with ``argv[2]``.
# ``group``: a descendant in the child's group holding its pipes; ``setsid``:
# the same outside the group; ``detached``: outside the group holding nothing,
# the way Pi's bash tool starts a command.
_ORPHAN = (
    "import subprocess, sys\n"
    "sys.stdin.buffer.read()\n"
    "quiet = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}\n"
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
    "                     stdin=subprocess.DEVNULL, start_new_session=sys.argv[1] != 'group',\n"
    "                     **(quiet if sys.argv[1] == 'detached' else {}))\n"
    "print(p.pid, flush=True)\n"
    "sys.exit(int(sys.argv[2]))\n"
)


def _orphan(mode: str, status: int, contain: bool = False):
    with start_duplex(
        DuplexRequest(
            command=[PY, "-c", _ORPHAN, mode, str(status)],
            deadline_seconds=30,
            contain_orphans=contain,
        )
    ) as child:
        child.close_stdin()
        item = child.read_line(timeout=10)
        assert isinstance(item, Record)
        return child.finish(), int(item.data)


@pytest.mark.parametrize("status", [0, 3])
def test_pipe_holding_descendant_after_exit_is_killed_and_reported(monkeypatch, status):
    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 0.5)
    started = time.monotonic()
    res, pid = _orphan("group", status)
    elapsed = time.monotonic() - started
    try:
        assert not res.timed_out and res.exit_code == status
        assert res.descendants_killed
        assert not res.group_survived_kill and not res.capture_abandoned
        assert "left processes behind" in res.leftovers
        assert 0.5 <= elapsed < 6, elapsed
        assert _gone(pid, within=5)
    finally:
        _kill_quietly(pid)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only subreaper")
def test_contained_detached_orphan_is_reported_as_an_orphan_only(monkeypatch):
    """A detached process holding none of the pipes is the orphan alone: the
    group was empty and the pipes closed, so no group kill is reported."""
    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    res, pid = _orphan("detached", 0, contain=True)
    try:
        assert res.exit_code == 0 and res.orphans_killed
        assert not (res.descendants_killed or res.capture_abandoned or res.group_survived_kill)
        assert "left processes behind" not in res.leftovers
        assert _gone(pid, within=1)
    finally:
        _kill_quietly(pid)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only subreaper")
def test_contained_setsid_escapee_is_killed_and_reported(monkeypatch):
    """With ``contain_orphans`` the same escapee is re-parented to the
    controller once the child exits, killed, and reported; the pipes reach
    EOF, so nothing is abandoned (ADR 0002 amendment, #132)."""
    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    res, pid = _orphan("setsid", 0, contain=True)
    try:
        assert not res.timed_out and res.exit_code == 0
        assert res.orphans_killed and res.descendants_killed
        assert not (res.capture_abandoned or res.group_survived_kill or res.orphan_survived_kill)
        assert not res.orphans_unchecked
        assert "outside its process group" in res.leftovers
        assert _gone(pid, within=1)
    finally:
        _kill_quietly(pid)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only subreaper")
def test_contained_orphan_is_killed_when_the_with_block_raises(monkeypatch):
    """Teardown on an exception sweeps the orphans too and releases the
    subreaper, so the next contained invocation can start."""
    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    pid = None
    with pytest.raises(RuntimeError, match="boom"):
        with start_duplex(
            DuplexRequest(
                command=[PY, "-c", _ORPHAN, "setsid", "0"],
                deadline_seconds=30,
                contain_orphans=True,
            )
        ) as child:
            child.close_stdin()
            item = child.read_line(timeout=10)
            assert isinstance(item, Record)
            pid = int(item.data)
            raise RuntimeError("boom")
    assert pid is not None
    try:
        assert _gone(pid, within=1)
        with start_duplex(
            DuplexRequest(command=[PY, "-c", "pass"], deadline_seconds=30, contain_orphans=True)
        ) as again:
            assert again.finish().leftovers == ""
    finally:
        _kill_quietly(pid)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only subreaper")
def test_contained_orphan_is_killed_when_finish_is_interrupted(monkeypatch):
    """A Ctrl-C inside ``finish()``'s wait for the child kills the still-running
    child and its detached process while the subreaper still catches the
    latter, and only then restores the subreaper setting (#132)."""
    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    real_reaped = executor_duplex._reaped
    interrupted = []

    def interrupting_reaped(proc, deadline):
        if not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt
        return real_reaped(proc, deadline)

    monkeypatch.setattr(executor_duplex, "_reaped", interrupting_reaped)
    # Starts a detached process the way Pi's bash tool does, then keeps
    # running past stdin EOF, so finish() has to wait for it.
    detacher = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        "                     start_new_session=True, stdin=subprocess.DEVNULL,\n"
        "                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    previous = _subreaper()
    pid = None
    with pytest.raises(KeyboardInterrupt):
        with start_duplex(
            DuplexRequest(command=[PY, "-c", detacher], deadline_seconds=30, contain_orphans=True)
        ) as child:
            item = child.read_line(timeout=10)
            assert isinstance(item, Record)
            pid = int(item.data)
            child.finish()
    assert interrupted and pid is not None
    try:
        assert not executor._group_alive(child.pid)
        assert _gone(pid, within=1)
        assert _subreaper() == previous, "the subreaper setting was not restored"
    finally:
        _kill_quietly(pid)


# Orphans ``argv[1]`` short-lived processes (each started by an intermediary
# that exits at once, so it dies as the controller's child; every other one
# leaves the child's group first), says so, runs on until stdin EOF, and
# exits 7.
_ORPHAN_FACTORY = (
    "import os, sys, time\n"
    "for i in range(int(sys.argv[1])):\n"
    "    if os.fork() == 0:\n"
    "        if os.fork() == 0:\n"
    "            if i % 2:\n"
    "                os.setsid()\n"
    "            time.sleep(0.01)\n"
    "            os._exit(0)\n"
    "        os._exit(0)\n"
    "    os.wait()\n"
    "print('forked', flush=True)\n"
    "sys.stdin.buffer.read()\n"
    "sys.exit(7)\n"
)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only subreaper")
def test_contained_dead_orphans_are_reaped_while_the_child_runs():
    """Through the handle as through ``execute()`` (#191): what the child
    orphans is reaped as it dies, while the child still runs, so zombies
    cannot pile up against the session's pids.max over a long conversation;
    the child's own exit status is kept."""
    previous = _subreaper()
    before = frozenset(executor._children())

    def new_zombies() -> int:
        return sum(
            1 for key, zombie in executor._children().items() if zombie and key not in before
        )

    with start_duplex(
        DuplexRequest(
            command=[PY, "-c", _ORPHAN_FACTORY, "200"], deadline_seconds=30, contain_orphans=True
        )
    ) as child:
        assert child.read_line(timeout=20) == Record(b"forked")
        settle = time.monotonic() + 3
        while new_zombies() and time.monotonic() < settle:
            time.sleep(0.05)
        left = new_zombies()
        child.close_stdin()
        res = child.finish()
    assert left == 0, f"{left} dead orphans were left unreaped while the child ran"
    assert res.exit_code == 7 and not res.timed_out
    assert not (res.orphans_killed or res.orphan_survived_kill) and res.leftovers == ""
    assert new_zombies() == 0
    assert _subreaper() == previous


def _subreaper() -> int:
    value = ctypes.c_int(0)
    ctypes.CDLL(None, use_errno=True).prctl(37, ctypes.byref(value), 0, 0, 0)
    return value.value


def test_setsid_escapee_gives_capture_abandoned(monkeypatch):
    """ADR 0002 §5: the escapee is out of the group kill's reach, so the capture
    is abandoned after the grace rather than waited for, and reported."""
    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    started = time.monotonic()
    res, pid = _orphan("setsid", 0)
    elapsed = time.monotonic() - started
    try:
        assert not res.timed_out and res.exit_code == 0
        assert res.descendants_killed and res.capture_abandoned
        assert not res.group_survived_kill
        assert "outside its group" in res.leftovers
        assert elapsed < 5, elapsed
        assert not _gone(pid, within=0.1)  # nothing here can kill it
    finally:
        _kill_quietly(pid)


def test_sigterm_ignoring_child_is_escalated_to_sigkill(monkeypatch):
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    signalled: list[int] = []
    real_killpg = os.killpg

    def spying_killpg(pgid, sig):
        if sig != 0:
            signalled.append(int(sig))
        return real_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", spying_killpg)
    script = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "sys.stdin.buffer.read()\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    with start_duplex(DuplexRequest(command=[PY, "-c", script], deadline_seconds=1)) as child:
        assert child.read_line(timeout=10) == Record(b"ready")
        res = child.finish()
    elapsed = time.monotonic() - started
    assert res.timed_out and res.exit_code == -1
    assert signalled == [signal.SIGTERM, signal.SIGKILL]
    assert not (res.group_survived_kill or res.capture_abandoned)
    assert 1.5 <= elapsed < 4, elapsed
    assert not executor._group_alive(child.pid)


# The same leftover scenarios through ``execute()`` and through the handle
# report the same ADR 0002 facts. Each child ignores stdin, so closing it at
# once makes the duplex run the one-shot run.
_PARITY = {
    "clean": "print('ok')",
    "pipe-holder": (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        "                 stdin=subprocess.DEVNULL)\n"
        "sys.exit(3)\n"
    ),
    "deaf-descendant": (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'],\n"
        "                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
        "                 stderr=subprocess.DEVNULL)\n"
    ),
    "overrun": "import time; time.sleep(60)",
}


@pytest.mark.parametrize("scenario", sorted(_PARITY))
def test_adr_0002_facts_match_execute(monkeypatch, scenario):
    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 0.3)
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.3)
    command = [PY, "-c", _PARITY[scenario]]
    one_shot = execute(ExecutionRequest(command=command, timeout_seconds=1))
    with start_duplex(DuplexRequest(command=command, deadline_seconds=1)) as child:
        child.close_stdin()
        duplex = child.finish()

    def facts(res):
        return (
            res.exit_code,
            res.timed_out,
            res.descendants_killed,
            res.group_survived_kill,
            res.capture_abandoned,
            res.leftovers,
        )

    assert facts(duplex) == facts(one_shot)


# -- environment, spawn, request validation --------------------------------------
_PYTHON_OWN = {"LC_CTYPE", "PYTHONIOENCODING", "__CF_USER_TEXT_ENCODING"}


def test_allowlist_applies_and_env_additions_are_present(monkeypatch):
    monkeypatch.setenv("AUTOFORGE_TEST_SECRET", "s3cret")
    monkeypatch.setenv("AUTOFORGE_TEST_KEEP_A", "a")
    script = "import json, os; print(json.dumps(dict(os.environ)), flush=True)"
    with start_duplex(
        DuplexRequest(
            command=[PY, "-c", script],
            deadline_seconds=30,
            env_allowlist=("PATH", "AUTOFORGE_TEST_KEEP_*"),
            env={"AUTOFORGE_TEST_ADDED": "x"},
        )
    ) as child:
        item = child.read_line(timeout=10)
        child.finish()
    assert isinstance(item, Record)
    env = json.loads(item.data)
    assert "AUTOFORGE_TEST_SECRET" not in env
    assert env["AUTOFORGE_TEST_KEEP_A"] == "a" and env["AUTOFORGE_TEST_ADDED"] == "x"
    assert set(env) <= {"PATH", "AUTOFORGE_TEST_KEEP_A", "AUTOFORGE_TEST_ADDED"} | _PYTHON_OWN


def test_cwd_is_applied(tmp_path):
    script = "import os; print(os.getcwd(), flush=True)"
    with start_duplex(
        DuplexRequest(command=[PY, "-c", script], cwd=str(tmp_path), deadline_seconds=30)
    ) as child:
        assert child.read_line(timeout=10) == Record(str(tmp_path.resolve()).encode())
        child.finish()


def test_missing_binary_raises_execution_error():
    with pytest.raises(ExecutionError, match="not found"):
        start_duplex(DuplexRequest(command=["autoforge-definitely-missing-binary-xyz"]))


@pytest.mark.parametrize(
    "contain",
    [
        False,
        pytest.param(
            True,
            marks=pytest.mark.skipif(
                not sys.platform.startswith("linux"), reason="Linux-only subreaper"
            ),
        ),
    ],
    ids=["plain", "contained"],
)
@pytest.mark.parametrize(
    ("step", "name", "error"),
    [
        ("start", "stdout", RuntimeError("can't start new thread")),
        ("start", "stderr", RuntimeError("can't start new thread")),
        ("pipe", "stderr", OSError(24, "Too many open files")),
    ],
    ids=["stdout-reader", "stderr-reader", "stderr-reader-pipe"],
)
def test_a_setup_failure_after_the_spawn_leaves_nothing_behind(
    monkeypatch, step, name, error, contain
):
    """#190 and the PR #196 review: the reader threads start, and their wake
    pipes open, after the child is spawned and before the caller has a
    handle to tear down. One the system refuses (a thread or descriptor
    limit) fails the launch as an ExecutionError, and first the child, which
    would otherwise sleep on, is killed and reaped; the containment is
    released with its reaper thread, so the next contained invocation runs;
    and no thread or descriptor is left open."""
    refused = []
    real_start, real_init = threading.Thread.start, executor._PipeDrain.__init__

    def start(self):
        if self.name == f"autoforge-capture-{name}":
            refused.append(self.name)
            raise error
        return real_start(self)

    def init(self, stream, reader_name):
        if reader_name == name:
            refused.append(reader_name)
            raise error
        return real_init(self, stream, reader_name)

    spawned = []
    real_spawn = executor_duplex._spawn

    def spawn(*args, **kwargs):
        spawned.append(real_spawn(*args, **kwargs))
        return spawned[-1]

    previous = _subreaper() if contain else None
    fds, threads = _baseline_fds(), set(threading.enumerate())
    monkeypatch.setattr(executor_duplex, "_spawn", spawn)
    if step == "start":
        monkeypatch.setattr(threading.Thread, "start", start)
    else:
        monkeypatch.setattr(executor._PipeDrain, "__init__", init)
    started = time.monotonic()
    try:
        with pytest.raises(ExecutionError, match=f"failed to start .*{error.args[-1]}"):
            start_duplex(
                DuplexRequest(
                    command=[PY, "-c", "import time; time.sleep(60)"],
                    deadline_seconds=30,
                    contain_orphans=contain,
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
    assert len(refused) == 1
    assert set(threading.enumerate()) == threads
    assert _open_fds() == fds
    assert not executor._CONTAINMENT_LOCK.locked()
    if contain:
        assert _subreaper() == previous
    with start_duplex(
        DuplexRequest(command=[PY, "-c", "pass"], deadline_seconds=30, contain_orphans=contain)
    ) as child:
        res = child.finish()
    assert res.exit_code == 0 and res.leftovers == ""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command", []),
        ("deadline_seconds", 0),
        ("deadline_seconds", -1),
        ("deadline_seconds", float("nan")),
        ("deadline_seconds", float("inf")),
        ("deadline_seconds", float("-inf")),
        ("deadline_seconds", MAX_DEADLINE_SECONDS + 1),
        ("deadline_seconds", 1e10),
        ("max_record_bytes", 0),
        ("max_pending_records", 0),
        ("max_pending_bytes", 0),
        ("max_stderr_bytes", 0),
        ("max_stdout_bytes", 0),
        ("max_record_bytes", DuplexRequest.max_pending_bytes + 1),
    ],
)
def test_invalid_request_is_refused_before_spawning(field, value, monkeypatch):
    def no_spawn(*args, **kwargs):
        raise AssertionError("an invalid request must not spawn")

    monkeypatch.setattr(executor_duplex, "_spawn", no_spawn)
    req = DuplexRequest(command=[PY, "-c", "pass"])
    setattr(req, field, value)
    with pytest.raises(ExecutionError, match=field if field != "command" else "empty"):
        start_duplex(req)


def test_the_largest_accepted_deadline_is_one_every_wait_can_take():
    """An unbounded read and a write both wait on the whole remaining deadline."""
    req = DuplexRequest(command=[PY, "-c", _ECHO, "0"], deadline_seconds=MAX_DEADLINE_SECONDS)
    with start_duplex(req) as child:
        child.send_line(b"ping")
        record = child.read_line()
        assert isinstance(record, Record) and record.data.startswith(b"4:")
        assert child.finish().exit_code == 0


def test_server_mode_has_devnull_stdin_and_captured_stdout():
    """The shape #126's server child needs: no stdin, stdout as bounded capture."""
    script = "import sys; print(repr(sys.stdin.read())); print('line two')"
    with start_duplex(
        DuplexRequest(
            command=[PY, "-c", script],
            deadline_seconds=30,
            stdin_pipe=False,
            stdout_mode=StdoutMode.CAPTURE,
        )
    ) as child:
        with pytest.raises(ExecutionError, match="not open"):
            child.send_line(b"x")
        with pytest.raises(ExecutionError, match="captured"):
            child.read_line(timeout=0)
        res = child.finish()
    assert res.exit_code == 0 and res.stdout == "''\nline two\n" and not res.stdout_truncated


def test_module_knows_no_encoding_provider_or_workflow():
    """No JSON, no Pi names, no phase or state imports (#130 acceptance)."""
    path = Path(executor_duplex.__file__)
    tree = ast.parse(path.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
            if node.module is None:
                imported |= {"." + alias.name for alias in node.names}
    local = {name for name in imported if name.startswith(".")}
    assert local <= {".", ".executor", ".errors"}, local
    assert not imported & {"json", "pickle", "marshal"}, imported
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.FunctionDef | ast.ClassDef):
            identifiers.add(node.name)
        elif isinstance(node, ast.arg):
            identifiers.add(node.arg)
    for name in identifiers:
        lowered = name.lower()
        assert not any(word in lowered for word in ("json", "phase", "control_result", "state")), (
            name
        )
        assert not (lowered == "pi" or lowered.startswith("pi_")), name
