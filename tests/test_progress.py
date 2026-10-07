"""Live agent progress (#192): safe events, the line renderer, and what reaches disk.

The unit tests drive :mod:`autoforge.progress` with an injected clock. The
acceptance tests run the engine against a fake ``claude`` streaming
stream-json (``tests/claude_fake.py``) whose tool inputs carry seeded
secrets, escape sequences and an environment sentinel, and check that none
of them reaches a progress line, ``progress.log`` or any other file under
the state directory.
"""

import json
import threading
import uuid
from dataclasses import replace

import pytest

from autoforge.errors import ExecutionError
from autoforge.progress import (
    ELLIPSIS,
    MAX_DETAIL_CHARS,
    MAX_LINE_CHARS,
    MAX_RAW_CHARS,
    MAX_TOKENS,
    MAX_TOOL_CHARS,
    ProgressEvent,
    ProgressKind,
    ProgressReporter,
    clean,
    guarded,
    relative_path,
)
from autoforge.providers import ClaudeCodeProvider
from autoforge.runlog import StepLog
from autoforge.state import load_state
from tests import claude_fake
from tests.conftest import (
    BRANCH,
    ISSUE,
    PR,
    SHA_A,
    block,
    implementation_pr_body,
    make_engine,
)

TOKEN = "ghp_" + "Z9" * 18
# A token an escape sequence splits in two: neither half matches a pattern.
SPLIT_HEAD, SPLIT_TAIL = "ghp_abc", "DEFGHIJKLMNOPQRS"
SPLIT = SPLIT_HEAD + "\x1b[0m" + SPLIT_TAIL
REDACTED = "***REDACTED***"


# -- clean ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("\x1b[31mred\x1b[0m", "red"),
        ("\x9b31mred", "red"),
        ("a\x1b]0;title\x07b", "ab"),
        ("a\x1b]8;;https://example.com\x1b\\b", "ab"),
        ("a\x1b]8;;unterminated", "a"),
        ("a\x1bPdevice\x1b\\b", "ab"),
        ("a\x1b(Bb", "ab"),
        ("a\x00b\x07c\x7fd\x85e", "abcde"),
        ("a\tb\nc\r\nd", "a b c d"),
        ("a b c", "a b c"),
        ("‮abc​⁦d", "abcd"),
        ("a\ud800b", "a�b"),
        ("  spaced   out  ", "spaced out"),
    ],
)
def test_clean_strips_escapes_and_controls(raw, shown):
    assert clean(raw, 100) == shown


def test_clean_rejoins_a_token_split_by_an_escape_before_redacting():
    assert clean(SPLIT, 100) == REDACTED


def test_clean_redacts_before_it_clips():
    # Clipped first, the token would be cut short of its pattern and kept.
    shown = clean("x" * 10 + " " + TOKEN, 20)
    assert "ghp_" not in shown and "Z9" not in shown
    assert shown == "xxxxxxxxxx ***REDAC" + ELLIPSIS
    assert len(shown) == 20


def test_clean_shows_an_oversized_or_non_string_value_as_nothing_useful():
    assert clean("a" * (MAX_RAW_CHARS + 1), 100) == ELLIPSIS
    assert clean("a" * MAX_RAW_CHARS, 10) == "a" * 9 + ELLIPSIS
    assert clean(None, 100) == "" and clean(42, 100) == "" and clean(["x"], 100) == ""
    assert clean("x", 0) == ""


# -- ProgressEvent, guarded, relative_path ------------------------------------
def test_an_event_is_made_safe_when_it_is_built():
    event = ProgressEvent(
        "tool_started", tool="T" * 200, detail=f"\x1b[1m{TOKEN}\x1b[0m " + "d" * 500
    )
    assert event.kind is ProgressKind.TOOL_STARTED
    assert len(event.tool) == MAX_TOOL_CHARS and event.tool.endswith(ELLIPSIS)
    assert event.detail.startswith(REDACTED + " ") and len(event.detail) == MAX_DETAIL_CHARS
    assert ProgressEvent(ProgressKind.ACTIVITY, detail=b"bytes").detail == ""  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ProgressEvent("tool_result")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("tokens", "kept"),
    [(12, 12), (0, 0), (-5, 0), (True, 0), ("3", 0), (1.5, 0), (10**15, MAX_TOKENS)],
)
def test_an_event_token_estimate_is_a_bounded_non_negative_int(tokens, kept):
    assert ProgressEvent(ProgressKind.THINKING, tokens=tokens).tokens == kept


def test_a_guarded_sink_is_dropped_after_its_first_failure():
    assert guarded(None) is None
    calls = []

    def sink(event):
        calls.append(event)
        raise OSError("terminal gone")

    emit = guarded(sink)
    assert emit is not None
    event = ProgressEvent(ProgressKind.ACTIVITY)
    emit(event)
    emit(event)
    assert calls == [event]


def test_a_guarded_sink_does_not_swallow_an_interrupt():
    def sink(event):
        raise KeyboardInterrupt

    emit = guarded(sink)
    assert emit is not None
    with pytest.raises(KeyboardInterrupt):
        emit(ProgressEvent(ProgressKind.ACTIVITY))


@pytest.mark.parametrize(
    ("path", "cwd", "shown"),
    [
        ("/work/tree/src/a.py", "/work/tree", "src/a.py"),
        ("/work/tree/src/a.py", "/work/tree/", "src/a.py"),
        ("/work/tree", "/work/tree", "/work/tree"),
        ("/work/tree2/a.py", "/work/tree", "/work/tree2/a.py"),
        ("/etc/hosts", "/work/tree", "/etc/hosts"),
        ("src/a.py", "", "src/a.py"),
        (None, "/work/tree", ""),
        (["/work/tree/a"], "/work/tree", ""),
    ],
)
def test_relative_path(path, cwd, shown):
    assert relative_path(path, cwd) == shown


# -- ProgressReporter ----------------------------------------------------------
class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _reporter(outputs, clock, **kw) -> ProgressReporter:
    return ProgressReporter("FIX #2", outputs, clock=clock, **kw)


def _event(kind, at_clock: Clock, **kw) -> ProgressEvent:
    return ProgressEvent(kind, at=at_clock.now, **kw)


def test_the_reporter_prefixes_elapsed_time_and_label():
    clock, out = Clock(), []
    reporter = _reporter([out.append], clock)
    reporter.line("launching fix")
    clock.now += 3725
    reporter.line("agent exited 0")
    assert out == ["[00:00:00 FIX #2] launching fix", "[01:02:05 FIX #2] agent exited 0"]


def test_the_reporter_renders_each_kind_and_counts_every_event():
    clock, out = Clock(), []
    reporter = _reporter([out.append], clock)
    for kind, kw in [
        (ProgressKind.STARTED, {"detail": "claude-fable-5-1"}),
        (ProgressKind.TOOL_STARTED, {"tool": "Read", "detail": "src/a.py"}),
        (ProgressKind.TOOL_FINISHED, {"tool": "Read"}),
        (ProgressKind.TOOL_STARTED, {"tool": "Bash", "detail": "Run checks"}),
        (ProgressKind.TOOL_FAILED, {"tool": "Bash"}),
        (ProgressKind.TOOL_STARTED, {}),
        (ProgressKind.PROVIDER_RETRY, {"detail": "attempt 2 of 10"}),
        (ProgressKind.PROVIDER_RETRY, {}),
        (ProgressKind.ACTIVITY, {}),
        (ProgressKind.STARTED, {}),
    ]:
        reporter.sink(_event(kind, clock, **kw))
    assert [line.split("] ", 1)[1] for line in out] == [
        "agent started (claude-fable-5-1)",
        "Read src/a.py",
        "Bash Run checks",
        "Bash failed",
        "tool",
        "provider retry (attempt 2 of 10)",
        "provider retry",
        "agent started",
    ]
    assert reporter.events == 10


def test_thinking_and_writing_are_coalesced_to_one_line_per_interval():
    clock, out = Clock(), []
    reporter = _reporter([out.append], clock, thinking_interval=30)
    reporter.sink(_event(ProgressKind.THINKING, clock))
    clock.now += 10
    reporter.sink(_event(ProgressKind.THINKING, clock, tokens=12_345))
    reporter.sink(_event(ProgressKind.ASSISTANT_TEXT, clock))
    clock.now += 21
    reporter.sink(_event(ProgressKind.THINKING, clock))
    clock.now += 30
    reporter.sink(_event(ProgressKind.ASSISTANT_TEXT, clock))
    assert out == [
        "[00:00:00 FIX #2] thinking…",
        "[00:00:31 FIX #2] thinking… ~12k tokens",
        "[00:01:01 FIX #2] writing…",
    ]
    assert reporter.events == 5


def test_the_heartbeat_says_how_long_ago_the_agent_last_did_anything():
    clock, out = Clock(), []
    reporter = _reporter([out.append], clock, heartbeat_after=120)
    reporter.poll()
    clock.now += 119
    reporter.poll()
    assert out == []
    clock.now += 1
    reporter.poll()
    assert out == ["[00:02:00 FIX #2] still running, no activity yet"]
    clock.now += 10
    reporter.sink(_event(ProgressKind.ACTIVITY, clock))  # counted, not printed
    clock.now += 109
    reporter.poll()
    assert len(out) == 1  # 119 s since the last printed line
    clock.now += 1
    reporter.poll()
    assert out[-1] == "[00:04:00 FIX #2] still running, last activity 1m50s ago, 1 events"


def test_a_line_is_cleaned_and_bounded():
    clock, out = Clock(), []
    reporter = ProgressReporter(f"FIX \x1b[2J#2\n{TOKEN}", [out.append], clock=clock)
    reporter.line(f"failed: \x1b[31m{TOKEN}\x1b[0m\nnext {SPLIT}")
    reporter.line("x" * 5000)
    reporter.line("y" * 1000)
    first, second, third = out
    assert first == f"[00:00:00 FIX #2 {REDACTED}] failed: {REDACTED} next {REDACTED}"
    assert second.endswith("] " + ELLIPSIS)
    assert len(third) == MAX_LINE_CHARS and third.endswith(ELLIPSIS)


def test_a_failing_output_is_dropped_with_one_note_on_the_others():
    clock, good, bad_calls = Clock(), [], []

    def bad(line):
        bad_calls.append(line)
        raise OSError(f"disk full {TOKEN}")

    reporter = _reporter([bad, good.append], clock)
    reporter.line("one")
    reporter.line("two")
    assert bad_calls == ["[00:00:00 FIX #2] one"]
    assert good == [
        "[00:00:00 FIX #2] one",
        f"[00:00:00 FIX #2] progress output dropped: OSError: disk full {REDACTED}",
        "[00:00:00 FIX #2] two",
    ]


def test_the_sink_never_raises_even_when_every_output_fails():
    def bad(line):
        raise OSError("gone")

    reporter = _reporter([bad, bad], Clock())
    reporter.sink(ProgressEvent(ProgressKind.STARTED))
    reporter.sink(ProgressEvent(ProgressKind.STARTED))
    reporter.line("still fine")


def _failing(calls, error, when=lambda line: True):
    """An output that records each line and raises ``error`` on the lines ``when`` picks."""

    def output(line):
        calls.append(line)
        if when(line):
            raise error

    return output


def test_outputs_failing_on_the_same_line_are_each_dropped_once():
    clock, good, disk_calls, terminal_calls = Clock(), [], [], []
    disk = _failing(disk_calls, OSError("disk full"))
    terminal = _failing(terminal_calls, BrokenPipeError("stderr closed"))
    reporter = _reporter([disk, terminal, good.append], clock)
    reporter.line("one")
    reporter.line("two")
    assert disk_calls == terminal_calls == ["[00:00:00 FIX #2] one"]
    assert good == [
        "[00:00:00 FIX #2] one",
        "[00:00:00 FIX #2] progress output dropped: OSError: disk full",
        "[00:00:00 FIX #2] progress output dropped: BrokenPipeError: stderr closed",
        "[00:00:00 FIX #2] two",
    ]


def test_a_controller_line_never_raises_when_every_output_fails_on_it():
    disk_calls, terminal_calls = [], []
    disk = _failing(disk_calls, OSError("disk full"))
    terminal = _failing(terminal_calls, BrokenPipeError("stderr closed"))
    reporter = _reporter([disk, terminal], Clock())
    reporter.line("one")
    reporter.line("two")
    reporter.sink(ProgressEvent(ProgressKind.STARTED))
    reporter.poll()
    assert disk_calls == terminal_calls == ["[00:00:00 FIX #2] one"]


def test_an_output_failing_on_another_outputs_note_is_dropped_in_turn():
    clock, good, disk_calls, terminal_calls = Clock(), [], [], []
    disk = _failing(disk_calls, OSError("disk full"))
    terminal = _failing(
        terminal_calls, BrokenPipeError("stderr closed"), when=lambda line: "dropped" in line
    )
    reporter = _reporter([disk, terminal, good.append], clock)
    reporter.line("one")
    reporter.line("two")
    assert disk_calls == ["[00:00:00 FIX #2] one"]
    assert terminal_calls == [
        "[00:00:00 FIX #2] one",
        "[00:00:00 FIX #2] progress output dropped: OSError: disk full",
    ]
    assert good == [
        "[00:00:00 FIX #2] one",
        "[00:00:00 FIX #2] progress output dropped: OSError: disk full",
        "[00:00:00 FIX #2] progress output dropped: BrokenPipeError: stderr closed",
        "[00:00:00 FIX #2] two",
    ]


def test_a_controller_line_does_not_swallow_an_interrupt():
    reporter = _reporter([_failing([], KeyboardInterrupt())], Clock())
    with pytest.raises(KeyboardInterrupt):
        reporter.line("one")


def test_the_heartbeat_thread_runs_inside_the_with_and_is_joined_on_exit():
    out: list[str] = []
    with ProgressReporter("FIX", [out.append], heartbeat_after=0, poll_seconds=0.01) as reporter:
        names = [t.name for t in threading.enumerate()]
        assert "autoforge-progress" in names
        for _ in range(500):
            if out:
                break
            threading.Event().wait(0.01)
    assert out and out[0].endswith("still running, no activity yet")
    assert reporter._thread is None
    assert all(t.name != "autoforge-progress" or not t.is_alive() for t in threading.enumerate())


def test_a_refused_heartbeat_thread_leaves_the_reporter_working(monkeypatch):
    def refuse(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", refuse)
    out: list[str] = []
    with ProgressReporter("FIX", [out.append], clock=Clock()) as reporter:
        reporter.sink(ProgressEvent(ProgressKind.TOOL_STARTED, tool="Read", detail="a.py"))
    assert out == [
        "[00:00:00 FIX] no heartbeat: the progress thread could not be started",
        "[00:00:00 FIX] Read a.py",
    ]
    assert reporter._thread is None


# -- acceptance: a fake claude through the engine ------------------------------
ANALYZE_OK = {
    "phase": "ANALYZE_EXECUTE",
    "status": "success",
    "issue_url": ISSUE,
    "pr_url": PR,
    "head_sha": SHA_A,
    "branch": BRANCH,
}


class ClaudeThatOpensThePr(ClaudeCodeProvider):
    """The real Claude adapter on the fake CLI; the "agent" opens the PR."""

    def __init__(self, github) -> None:
        super().__init__()
        self.github = github

    def execute(self, req):
        result = super().execute(req)
        self.github.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2], body=implementation_pr_body())
        return result


def _engine_on_fake_claude(tmp_path, fake_github, lines):
    state_dir = tmp_path / ".autoforge"
    eng = make_engine(state_dir, [], github=fake_github)
    eng.step()  # INITIALIZING
    _, fake = claude_fake.fake_claude(tmp_path, lines)
    cfg_profile = eng.config.profiles["analyze_execute"]
    eng.config.profiles["analyze_execute"] = replace(
        cfg_profile, command=fake.command, options={**fake.options, "output_format": "stream-json"}
    )
    eng.providers._overrides["claude"] = ClaudeThatOpensThePr(fake_github)
    return eng, state_dir


def test_seeded_secrets_escapes_and_env_values_reach_no_line_and_no_file(
    tmp_path, fake_github, monkeypatch
):
    sentinel = f"sentinel-{uuid.uuid4().hex}"
    monkeypatch.setenv("CLAUDE_TEST_SENTINEL", sentinel)
    eng, state_dir = _engine_on_fake_claude(
        tmp_path,
        fake_github,
        [
            claude_fake.init(),
            claude_fake.thinking("$SENTINEL"),
            claude_fake.tool_use("t1", "Read", file_path="$CWD/src/a.py"),
            claude_fake.tool_result("t1", "$SENTINEL"),
            claude_fake.tool_use("t2", "Read", file_path=f"$CWD/{TOKEN}.txt"),
            claude_fake.tool_use("t3", "Grep", pattern="\x1b[31mneedle\x1b[0m\x1b]0;pwned\x07"),
            claude_fake.tool_use("t4", "Edit", file_path=f"$CWD/{SPLIT}"),
            claude_fake.tool_use(
                "t5",
                "Bash",
                command="curl -H 'X-Key: $SENTINEL' https://example.com",
                description=f"Deploy with {TOKEN}",
            ),
            claude_fake.tool_result("t5", f"$SENTINEL {TOKEN}", is_error=True),
            claude_fake.tool_use("t6", "Bash", command="echo $SENTINEL"),
            claude_fake.result(block(ANALYZE_OK)),
        ],
    )
    lines: list[str] = []
    eng.progress_output = lines.append
    out = eng.step()
    assert out.next_phase == "REVIEW"
    assert load_state(eng.paths.state_file).current_pr_url == PR

    shown = [line.split("] ", 1)[1] for line in lines]
    assert shown[0].startswith(
        "launching analyze_execute (claude, model fable, effort high), "
        "idle timeout 900s, max runtime unset, attempt "
    )
    assert shown[1:] == [
        "agent started (claude-fable-5-1)",
        "thinking…",
        "Read src/a.py",
        f"Read {REDACTED}.txt",
        "Grep needle",
        f"Edit {REDACTED}",
        f"Bash Deploy with {REDACTED}",
        "Bash failed",
        "Bash",
        "agent exited 0, 10 progress events",
    ]
    assert all(line.startswith("[00:00:0") and " ANALYZE_EXECUTE #2] " in line for line in lines)

    (progress_log,) = state_dir.rglob("progress.log")
    assert progress_log.read_text(encoding="utf-8").splitlines() == lines
    assert (progress_log.parent / "execution.json").is_file()

    seeded = [TOKEN, SPLIT_HEAD, SPLIT_TAIL, sentinel, "pwned"]
    for line in lines:
        for value in seeded:
            assert value not in line
        assert not any(ord(ch) < 0x20 or 0x7F <= ord(ch) < 0xA0 for ch in line)
    files = [p for p in state_dir.rglob("*") if p.is_file()]
    assert len(files) > 3
    for path in files:
        data = path.read_bytes()
        for value in seeded:
            assert value.encode() not in data, (path, value)
        assert b"\x1b" not in data and b"\\u001b" not in data, path


def test_a_failing_terminal_never_changes_the_outcome(tmp_path, fake_github):
    eng, state_dir = _engine_on_fake_claude(
        tmp_path,
        fake_github,
        [
            claude_fake.init(),
            claude_fake.tool_use("t1", "Read", file_path="$CWD/src/a.py"),
            claude_fake.result(block(ANALYZE_OK)),
        ],
    )
    calls = []

    def terminal(line):
        calls.append(line)
        raise BrokenPipeError("stderr closed")

    eng.progress_output = terminal
    assert eng.step().next_phase == "REVIEW"
    assert len(calls) == 1
    (progress_log,) = state_dir.rglob("progress.log")
    logged = [line.split("] ", 1)[1] for line in progress_log.read_text().splitlines()]
    assert logged[0].startswith("launching analyze_execute")
    assert logged[1:] == [
        "progress output dropped: BrokenPipeError: stderr closed",
        "agent started (claude-fable-5-1)",
        "Read src/a.py",
        "agent exited 0, 2 progress events",
    ]


@pytest.mark.parametrize("failing_line", ["launching ", "agent exited "])
def test_disk_and_terminal_failing_on_one_controller_line_never_change_the_outcome(
    tmp_path, fake_github, monkeypatch, failing_line
):
    """Both outputs fail on the pre-launch line, or both on the end line (R5-F1)."""
    eng, state_dir = _engine_on_fake_claude(
        tmp_path,
        fake_github,
        [
            claude_fake.init(),
            claude_fake.tool_use("t1", "Read", file_path="$CWD/src/a.py"),
            claude_fake.result(block(ANALYZE_OK)),
        ],
    )
    disk_calls, terminal_calls = [], []
    append = StepLog.append

    def disk(self, line):
        disk_calls.append(line)
        if failing_line in line:
            raise OSError("disk full")
        append(self, line)

    monkeypatch.setattr(StepLog, "append", disk)
    eng.progress_output = _failing(
        terminal_calls, BrokenPipeError("stderr closed"), when=lambda line: failing_line in line
    )

    assert eng.step().next_phase == "REVIEW"
    assert load_state(eng.paths.state_file).current_pr_url == PR
    for calls in (disk_calls, terminal_calls):
        # The failing line was each output's last: no note, nothing after it.
        assert failing_line in calls[-1]
        assert sum(failing_line in line for line in calls) == 1
        assert not any("dropped" in line for line in calls)
    shown = [line.split("] ", 1)[1] for line in terminal_calls]
    if failing_line == "launching ":
        assert len(shown) == 1
    else:
        assert shown[1:] == [
            "agent started (claude-fable-5-1)",
            "Read src/a.py",
            "agent exited 0, 2 progress events",
        ]
    (progress_log,) = state_dir.rglob("progress.log")
    assert progress_log.read_text(encoding="utf-8").splitlines() == disk_calls[:-1]
    execution = json.loads((progress_log.parent / "execution.json").read_text(encoding="utf-8"))
    assert execution["exit_code"] == 0 and execution["error"] == ""
    result = json.loads((progress_log.parent / "control-result.json").read_text(encoding="utf-8"))
    assert result["pr_url"] == PR


def test_a_failed_stream_ends_with_its_failure_and_logs_its_summary(tmp_path, fake_github):
    eng, state_dir = _engine_on_fake_claude(
        tmp_path,
        fake_github,
        [claude_fake.init(), claude_fake.result("Overloaded", is_error=True)],
    )
    lines: list[str] = []
    eng.progress_output = lines.append
    with pytest.raises(ExecutionError, match="agent 'analyze_execute' failed: claude: the run"):
        eng.step()
    assert lines[-1].endswith(
        "agent failed: claude: the run ended in an error (subtype success, terminal_reason "
        "completed): Overloaded, 1 progress events"
    )
    (progress_log,) = state_dir.rglob("progress.log")
    assert progress_log.read_text(encoding="utf-8").splitlines() == lines
    execution = json.loads((progress_log.parent / "execution.json").read_text(encoding="utf-8"))
    assert execution["provider_summary"]["is_error"] is True
    assert execution["provider_summary"]["failure"].startswith("claude: the run ended in an error")
