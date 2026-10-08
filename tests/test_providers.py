"""Provider adapters: exact CLI argv shapes, validation, scripted fake."""

import dataclasses
import hashlib
import json
import sys
import time

import pytest

from autoforge.config import ProfileConfig, default_config
from autoforge.errors import ConfigurationError
from autoforge.executor import MAX_DEADLINE_SECONDS
from autoforge.providers import (
    AgentExecutionResult,
    AgentRequest,
    ClaudeCodeProvider,
    OpenCodeProvider,
    PiProvider,
    ProviderRegistry,
    ScriptedProvider,
    provider_for,
)
from tests.conftest import DECODER_LIMIT_RECORDS


def _text(profile: ProfileConfig) -> ProfileConfig:
    """``profile`` with Claude's one-shot ``output_format: text`` opt-out (#192)."""
    return dataclasses.replace(profile, options={**profile.options, "output_format": "text"})


def test_claude_argv_shape():
    p = default_config().profile("analyze_execute")
    argv = ClaudeCodeProvider().build_command_for(p, "do it; rm -rf /")
    assert argv[0] == "claude"
    assert argv[1] == "-p"
    # stream-json is the default and the CLI refuses it under -p without --verbose.
    assert argv[2:5] == ["--output-format", "stream-json", "--verbose"]
    assert argv[argv.index("--model") + 1] == "fable"
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--no-session-persistence" in argv
    assert argv[-2:] == ["--", "do it; rm -rf /"]  # prompt is one literal element


def test_claude_text_opt_out_argv_has_no_verbose():
    p = _text(default_config().profile("analyze_execute"))
    ClaudeCodeProvider().validate_profile(p)
    argv = ClaudeCodeProvider().build_command_for(p, "do it")
    assert argv[2:4] == ["--output-format", "text"]
    assert "--verbose" not in argv
    assert argv[-2:] == ["--", "do it"]


def test_claude_rejects_an_unknown_output_format():
    # Regression: a claude profile with opencode's `output_format: default`
    # must fail at config validation, not at runtime with
    # `option '--output-format <format>' argument 'default' is invalid`.
    # `json` prints one object at exit: no progress, so it is refused too.
    for bad in ("default", "json", "stream_json", "TEXT"):
        p = ProfileConfig(
            name="x",
            provider="claude",
            model="fable",
            effort="high",
            options={"output_format": bad},
        )
        with pytest.raises(ConfigurationError, match="output_format"):
            ClaudeCodeProvider().validate_profile(p)


def test_claude_rejects_bad_effort_and_mode():
    p = ProfileConfig(name="x", provider="claude", model="fable", effort="ultra")
    with pytest.raises(ConfigurationError, match="effort"):
        ClaudeCodeProvider().validate_profile(p)
    p2 = ProfileConfig(
        name="x",
        provider="claude",
        model="fable",
        effort="high",
        options={"permission_mode": "yolo"},
    )
    with pytest.raises(ConfigurationError, match="permission_mode"):
        ClaudeCodeProvider().validate_profile(p2)


def test_opencode_argv_shape():
    """#186: v2's argv. The variant rides on the model, the private server
    keeps the agent inside this invocation, and the prompt is not in argv."""
    p = default_config().profile("review_round_1")
    argv = OpenCodeProvider().build_command_for(p, "review $(id)")
    assert argv == [
        "opencode",
        "run",
        "--standalone",
        "-m",
        "openai/gpt-5.6-luna#high",
        "--format",
        "default",
    ]
    assert "--variant" not in argv and "--" not in argv


@pytest.mark.parametrize("prompt", ["--help", "-x", "review $(id)", "", "#heading"])
def test_opencode_prompt_never_reaches_argv(prompt):
    """v2 duplicates a message after `--`, re-quotes one without it, and
    takes one that starts with `-` for a flag: the prompt goes on stdin."""
    p = default_config().profile("review_round_1")
    prov = OpenCodeProvider()
    argv = prov.build_command_for(p, prompt)
    assert argv == prov.build_command_for(p, "anything else")
    assert prov.stdin_payload(AgentRequest("REVIEW", prompt, ".", p, None, 5)) == prompt.encode()


def test_opencode_without_an_effort_sends_no_variant():
    p = ProfileConfig(name="r", provider="opencode", model="openai/gpt-5.6-luna", effort="")
    argv = OpenCodeProvider().build_command_for(p, "x")
    assert argv[argv.index("-m") + 1] == "openai/gpt-5.6-luna"
    assert not any("#" in a for a in argv)


def test_opencode_auto_flag_and_extra_args():
    p = ProfileConfig(
        name="r",
        provider="opencode",
        model="openai/gpt-5.6-sol",
        effort="medium",
        extra_args=["--agent", "reviewer"],
        options={"auto_approve": "true"},
    )
    argv = OpenCodeProvider().build_command_for(p, "x")
    assert argv[argv.index("-m") + 1] == "openai/gpt-5.6-sol#medium"
    assert argv[-3:] == ["--auto", "--agent", "reviewer"]


def test_opencode_rejects_a_variant_in_the_model():
    p = ProfileConfig(name="r", provider="opencode", model="openai/gpt-5.6-luna#high", effort="")
    with pytest.raises(ConfigurationError, match="effort") as err:
        OpenCodeProvider().validate_profile(p)
    assert "'r'" in str(err.value) and "#variant" in str(err.value)


def test_opencode_requires_provider_slash_model():
    p = ProfileConfig(name="r", provider="opencode", model="gpt-5.6-luna", effort="high")
    with pytest.raises(ConfigurationError, match="provider/model"):
        OpenCodeProvider().validate_profile(p)


def test_opencode_hands_the_prompt_to_the_runner_as_stdin():
    seen = []
    p = default_config().profile("review_round_1")
    prompt = 'review \ud800 \u00e9 "q"'
    OpenCodeProvider(runner=_capture_runner(seen)).execute(
        AgentRequest("REVIEW", prompt, "/tmp", p, None, 7)
    )
    # A lone surrogate cannot be UTF-8; it becomes U+FFFD rather than failing.
    assert seen[0].stdin_data == 'review \ufffd \u00e9 "q"'.encode()
    assert prompt not in seen[0].command


def test_claude_keeps_stdin_on_dev_null():
    seen = []
    p = _text(default_config().profile("fix"))
    ClaudeCodeProvider(runner=_capture_runner(seen)).execute(
        AgentRequest("FIX", "p", "/tmp", p, None, 7)
    )
    assert seen[0].stdin_data is None


@pytest.mark.parametrize(
    ("exit_code", "timed_out", "failure"),
    [(0, False, True), (1, False, False), (-9, True, False)],
)
def test_a_prompt_the_cli_did_not_take_whole_is_a_provider_failure(exit_code, timed_out, failure):
    """A clean exit with part of the prompt unread must not read as an answer
    to the whole prompt; a failed exit or a timeout already reports itself."""
    from autoforge.executor import ExecutionResult

    def runner(req):
        return ExecutionResult(
            req.command,
            req.cwd,
            exit_code,
            "ok",
            "",
            "t",
            "t",
            timed_out=timed_out,
            stdin_unread=10,
        )

    p = default_config().profile("review_round_1")
    res = OpenCodeProvider(runner=runner).execute(
        AgentRequest("REVIEW", "p" * 11, "/tmp", p, None, 7)
    )
    assert bool(res.provider_failure) is failure
    if failure:
        assert res.provider_failure == (
            "opencode: the prompt was not delivered: the CLI exited without reading "
            "the last 10 bytes of it"
        )


# -- a fake `claude` streaming stream-json under the duplex handle (#192) ------------
def _claude_run(tmp_path, lines, timeout=30, events=None, idle=None, **kw):
    from tests import claude_fake

    home, profile = claude_fake.fake_claude(tmp_path, lines, **kw)
    sink = None if events is None else events.append
    req = AgentRequest("FIX", "fix it", str(tmp_path), profile, idle, timeout, progress=sink)
    return home, ClaudeCodeProvider().execute(req)


def test_a_fake_claude_stream_is_reduced_to_the_result_text_verbatim(tmp_path):
    from autoforge.progress import ProgressKind
    from autoforge.result_parser import extract_last_block
    from tests import claude_fake
    from tests.conftest import block

    payload = {"phase": "FIX", "status": "success", "message": 'quoted "x" \\ and 中'}
    text = "Fixed it.\n" + block(payload)
    events = []
    home, res = _claude_run(
        tmp_path,
        [
            claude_fake.init(),
            claude_fake.thinking("let me think"),
            claude_fake.tool_use("t1", "Read", file_path=str(tmp_path / "src" / "a.py")),
            claude_fake.tool_result("t1", "contents"),
            claude_fake.tool_use("t2", "Bash", command="make check", description="Run checks"),
            claude_fake.tool_result("t2", "boom", is_error=True),
            claude_fake.result(text),
        ],
        events=events,
    )
    assert res.ok and res.provider_failure is None, (res.exit_code, res.stderr)
    assert res.stdout == text and res.stdout_tail == text
    assert json.loads(extract_last_block(res.stdout_tail)) == payload
    log = claude_fake.log(home)
    assert log["argv"][:4] == ["-p", "--output-format", "stream-json", "--verbose"]
    assert log["argv"][-2:] == ["--", "fix it"]
    assert log["stdin_is_devnull"] is True
    assert log["cwd"] == str(tmp_path)
    shown = [(e.kind, e.tool, e.detail) for e in events if e.kind != ProgressKind.ACTIVITY]
    assert shown == [
        (ProgressKind.STARTED, "", "claude-fable-5-1"),
        (ProgressKind.THINKING, "", ""),
        (ProgressKind.TOOL_STARTED, "Read", "src/a.py"),
        (ProgressKind.TOOL_FINISHED, "Read", ""),
        (ProgressKind.TOOL_STARTED, "Bash", "Run checks"),
        (ProgressKind.TOOL_FAILED, "Bash", ""),
    ]
    summary = res.provider_summary
    assert summary["cost_micro_usd"] == 12_500 and summary["num_turns"] == 3
    assert summary["tool_calls"] == 2 and summary["tool_errors"] == 1
    assert summary["failure"] == "" and summary["records"] == 7
    assert all(type(v) in (str, int, bool) for v in summary.values())


def test_the_last_turns_result_is_the_stdout_of_a_multi_turn_run(tmp_path):
    """A background task or a scheduled wakeup still pending at the end of a
    turn keeps the CLI alive for another turn, which ends in its own result
    (claude 2.1.293): the run is not a protocol violation, and its stdout is
    the last result's text, as ``--output-format text`` prints it."""
    from tests import claude_fake
    from tests.conftest import block

    text = "Done.\n" + block({"phase": "FIX", "status": "success"})
    _, res = _claude_run(
        tmp_path,
        [
            claude_fake.init(),
            claude_fake.tool_use("t1", "ScheduleWakeup", delaySeconds=60),
            claude_fake.tool_result("t1", "Next wakeup scheduled"),
            claude_fake.result("Waiting for the checks."),
            claude_fake.line({"type": "command_lifecycle", "state": "started"}),
            claude_fake.init(),
            claude_fake.result(text, total_cost_usd=0.02),
            claude_fake.line({"type": "command_lifecycle", "state": "completed"}),
        ],
    )
    assert res.ok and res.provider_failure is None, (res.exit_code, res.stderr)
    assert res.stdout == text
    summary = res.provider_summary
    assert summary["results"] == 2 and summary["num_turns"] == 6
    assert summary["cost_micro_usd"] == 20_000 and summary["records_after_result"] == 2


def _looping(n: int, *, tail: list | None = None) -> list:
    """``n`` copies of the same two tool calls (Bash then Read), each with
    the same input and the same result: an agent going round in circles."""
    from tests import claude_fake

    lines: list = [claude_fake.init()]
    for i in range(n):
        lines += [
            claude_fake.tool_use(f"b{i}", "Bash", command="make test"),
            claude_fake.tool_result(f"b{i}", "1 failed"),
            claude_fake.tool_use(f"r{i}", "Read", file_path="src/a.py"),
            claude_fake.tool_result(f"r{i}", "contents"),
        ]
    return lines + (tail or [])


def _loop_run(tmp_path, lines, mode, events=None, profile=None):
    from autoforge.config import LoopDetectionConfig
    from tests import claude_fake

    home, fake = claude_fake.fake_claude(tmp_path, lines)
    sink = None if events is None else events.append
    req = AgentRequest(
        "FIX",
        "fix it",
        str(tmp_path),
        profile(fake) if profile else fake,
        None,
        60,
        progress=sink,
        loop_detection=LoopDetectionConfig(mode=mode),
    )
    started = time.monotonic()
    res = ClaudeCodeProvider().execute(req)
    return res, time.monotonic() - started


def test_a_looping_claude_stream_is_killed_in_kill_mode_as_a_timeout(tmp_path):
    """#194: eight copies of the same 2-step cycle end the invocation at
    once, though the agent would otherwise have run on; the outcome is a
    timeout with the ``loop`` limit, and the report names the cycle."""
    from autoforge.loop_detect import LIMIT_LOOP
    from autoforge.progress import ProgressKind

    events = []
    res, elapsed = _loop_run(tmp_path, _looping(12, tail=[{"sleep": 30}]), "kill", events)
    assert elapsed < 20
    assert res.timed_out and res.timeout_limit == LIMIT_LOOP and not res.ok
    assert res.provider_failure is None
    assert res.loop is not None and res.loop.action == "killed"
    record = res.loop.record()
    assert (record["signal"], record["period"], record["repeats"]) == ("action_cycle", 2, 8)
    assert record["tools"] == ["Bash", "Read"]
    assert res.loop.describe().startswith(
        "the same 2-step action cycle (Bash, Read) repeated 8 times over "
    )
    warned = [e.detail for e in events if e.kind == ProgressKind.LOOP_SUSPECTED]
    assert warned[0] == "possible loop: 2-step cycle (Bash, Read) repeated 4×"
    summary = res.provider_summary
    assert summary["loop_max_cycle_repeats"] == 8 and summary["loop_max_cycle_period"] == 2
    assert summary["loop_actions"] >= 16


def test_a_looping_claude_stream_is_not_killed_in_warn_mode(tmp_path):
    from autoforge.progress import ProgressKind
    from tests import claude_fake

    events = []
    lines = _looping(10, tail=[claude_fake.result("done")])
    res, _ = _loop_run(tmp_path, lines, "warn", events)
    assert res.ok and not res.timed_out and res.stdout == "done"
    assert res.loop is not None and res.loop.action == "warned" and res.loop.warnings >= 1
    warned = [e.detail for e in events if e.kind == ProgressKind.LOOP_SUSPECTED]
    assert warned[0].startswith("possible loop: 2-step cycle (Bash, Read)")
    assert any(w.startswith("loop detected, not killed (mode warn): ") for w in warned)
    assert res.provider_summary["loop_max_cycle_repeats"] == 10


def test_loop_calibration_is_recorded_with_detection_off(tmp_path):
    from autoforge.progress import ProgressKind
    from tests import claude_fake

    events = []
    lines = _looping(10, tail=[claude_fake.result("done")])
    res, _ = _loop_run(tmp_path, lines, "off", events)
    assert res.ok and res.loop is None
    assert not [e for e in events if e.kind == ProgressKind.LOOP_SUSPECTED]
    assert res.provider_summary["loop_max_cycle_repeats"] == 10
    assert res.provider_summary["loop_actions"] == 20


def test_repeated_output_lines_kill_a_text_mode_agent_in_kill_mode(tmp_path):
    """Without a structured stream the repeated-lines signal stands in: the
    same line (digits masked) 200 times ends the invocation through the
    executor's stop."""
    from autoforge.loop_detect import LIMIT_LOOP

    lines: list = [f"waiting for lock (attempt {n})" for n in range(250)] + [{"sleep": 30}]
    res, elapsed = _loop_run(tmp_path, lines, "kill", profile=_text)
    assert elapsed < 20
    assert res.timed_out and res.timeout_limit == LIMIT_LOOP
    assert res.loop is not None and res.loop.action == "killed"
    record = res.loop.record()
    assert (record["signal"], record["stream"], record["period"]) == ("repeated_lines", "stdout", 1)
    assert record["repeats"] == 200
    assert res.provider_summary["loop_max_line_repeats"] >= 200


@pytest.mark.parametrize(
    ("lines", "exit_code", "failure"),
    [
        (
            ["RESULT_OK_IS_ERROR"],
            0,
            "claude: the run ended in an error (subtype success, terminal_reason completed)",
        ),
        (["RESULT_MAX_TURNS"], 0, "claude: the run did not succeed (subtype error_max_turns)"),
        (
            ["not json", "RESULT_OK"],
            0,
            "claude: stream-json protocol violation: a stdout line is not JSON",
        ),
        ([], 0, "claude: exited without a result event (exit 0)"),
        ([], 1, "claude: exited without a result event (exit 1)"),
        *(
            pytest.param(
                [line.decode(), "RESULT_OK"],
                0,
                "claude: stream-json protocol violation: a stdout line exceeds the JSON "
                "decoder's integer or nesting limit",
                id=name,
            )
            for name, line in DECODER_LIMIT_RECORDS.items()
        ),
    ],
)
def test_a_fake_claude_failure_is_a_provider_failure_with_no_stdout(
    tmp_path, lines, exit_code, failure
):
    from tests import claude_fake

    expand = {
        "RESULT_OK": claude_fake.result("done"),
        "RESULT_OK_IS_ERROR": claude_fake.result("done", is_error=True),
        "RESULT_MAX_TURNS": claude_fake.result("done", subtype="error_max_turns"),
    }
    _, res = _claude_run(
        tmp_path, [claude_fake.init(), *(expand.get(x, x) for x in lines)], exit_code=exit_code
    )
    assert res.provider_failure is not None and res.provider_failure.startswith(failure)
    assert res.provider_summary["failure"] == res.provider_failure
    assert res.stdout == "" and res.exit_code == exit_code and not res.timed_out


def test_a_fake_claude_that_overruns_its_timeout_is_killed_and_timed_out(tmp_path):
    import time

    from tests import claude_fake

    started = time.monotonic()
    _, res = _claude_run(tmp_path, [claude_fake.init(), {"sleep": 60}], timeout=1)
    assert time.monotonic() - started < 30
    assert res.timed_out and res.provider_failure is None and res.stdout == ""
    assert res.exit_code != 0


def test_a_fake_claude_that_goes_silent_is_killed_at_its_idle_timeout(tmp_path):
    """#193: the stream reports progress, so silence past the idle limit ends
    the run, named as the idle limit with the time of the last event."""
    import time

    from tests import claude_fake

    started = time.monotonic()
    _, res = _claude_run(tmp_path, [claude_fake.init(), {"sleep": 60}], timeout=30, idle=1)
    assert time.monotonic() - started < 15
    assert res.timed_out and res.timeout_limit == "idle" and res.provider_failure is None
    assert res.last_activity_at is not None


def test_a_fake_claude_that_keeps_streaming_outlives_its_idle_timeout(tmp_path):
    import time

    from tests import claude_fake

    lines = [claude_fake.init()]
    for n in range(6):
        lines += [{"sleep": 0.3}, claude_fake.tool_use(f"t{n}", "Bash", command=f"echo {n}")]
    started = time.monotonic()
    _, res = _claude_run(tmp_path, [*lines, claude_fake.result("done")], timeout=30, idle=1)
    assert time.monotonic() - started >= 1.8
    assert res.ok and res.stdout == "done" and not res.timed_out and res.timeout_limit == ""


def test_a_fake_claude_that_keeps_streaming_is_killed_at_its_maximum_runtime(tmp_path):
    from tests import claude_fake

    lines = [claude_fake.init()]
    for n in range(200):
        lines += [{"sleep": 0.05}, claude_fake.tool_use(f"t{n}", "Bash", command=f"echo {n}")]
    _, res = _claude_run(tmp_path, lines, timeout=1, idle=5)
    assert res.timed_out and res.timeout_limit == "max_runtime"


@pytest.mark.parametrize(
    ("status", "lines", "failure"),
    [
        (0, ["RESULT"], None),
        (3, [], "claude: exited without a result event (exit 3)"),
    ],
)
def test_a_pipe_holding_leftover_after_the_cli_exits_is_killed_not_a_timeout(
    tmp_path, monkeypatch, status, lines, failure
):
    """R3-F1, ADR 0002: the CLI exits while a helper it started still holds
    its stdout. EOF never comes on its own, so the driver must watch the
    CLI's exit, not EOF: the helper gets the exit grace, is killed with the
    group, and the CLI's own status and outcome are kept. Before the fix the
    driver waited for EOF until the deadline and reported a timeout."""
    import time

    from autoforge import executor
    from tests import claude_fake
    from tests.test_executor import _gone, _kill_quietly

    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    text = "Fixed.\n" + "done"
    expand = {"RESULT": claude_fake.result(text)}
    started = time.monotonic()
    home, res = _claude_run(
        tmp_path,
        [claude_fake.init(), {"spawn_holder": 60}, *(expand[x] for x in lines)],
        timeout=20,
        exit_code=status,
    )
    elapsed = time.monotonic() - started
    (pid,) = claude_fake.holders(home)
    try:
        assert not res.timed_out and res.exit_code == status
        assert res.provider_failure == failure
        assert res.stdout == ("" if failure else text)
        assert res.descendants_killed and "left processes behind" in res.leftovers
        assert not (res.group_survived_kill or res.capture_abandoned)
        assert elapsed < 10, elapsed  # the grace and the kill, never the deadline
        assert _gone(pid, within=5), "the pipe-holding helper outlived the invocation"
    finally:
        _kill_quietly(pid)


def test_a_cli_that_exits_just_before_its_deadline_is_not_a_timeout(tmp_path, monkeypatch):
    """R3-F1: the CLI exits in time, but a helper holding its stdout makes the
    exit grace and the kill run past the deadline. The CLI did not overrun,
    so the rest of stdout is still read and the result kept, as ``execute()``
    keeps it; the deadline bounds the CLI's exit, not the cleanup after it."""
    from autoforge import executor
    from tests import claude_fake
    from tests.test_executor import _kill_quietly

    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 1.5)
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    home, res = _claude_run(
        tmp_path,
        [claude_fake.init(), {"spawn_holder": 60}, {"sleep": 1.0}, claude_fake.result("done")],
        timeout=2,
    )
    (pid,) = claude_fake.holders(home)
    try:
        assert not res.timed_out and res.exit_code == 0
        assert res.provider_failure is None and res.stdout == "done"
        assert res.descendants_killed
    finally:
        _kill_quietly(pid)


def test_a_leftover_that_keeps_writing_does_not_hide_the_cli_exit(tmp_path, monkeypatch):
    """R4-F1, ADR 0002: a helper the CLI left behind writes a valid line every
    30 ms, so every read finds one and no read ever goes quiet. The CLI's exit
    is still seen while the lines keep coming: the helper gets the exit
    grace and is killed with the group, its lines are fed like the rest, and
    the CLI's own status and result are kept. Before the fix the driver only
    looked at the CLI after a quiet read and reported a timeout."""
    import time

    from autoforge import executor
    from tests import claude_fake
    from tests.test_executor import _gone, _kill_quietly

    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    hook = claude_fake.line({"type": "system", "subtype": "hook_response"}) + "\n"
    started = time.monotonic()
    home, res = _claude_run(
        tmp_path,
        [
            claude_fake.init(),
            {"spawn_holder": 60, "every": 0.03, "then": hook},
            {"sleep": 0.3},
            claude_fake.result("done"),
        ],
        timeout=10,
    )
    elapsed = time.monotonic() - started
    (pid,) = claude_fake.holders(home)
    try:
        assert not res.timed_out and res.exit_code == 0
        assert res.provider_failure is None and res.stdout == "done"
        assert res.descendants_killed and not (res.group_survived_kill or res.capture_abandoned)
        assert elapsed < 8, elapsed  # the grace and the kill, never the deadline
        assert _gone(pid, within=5), "the chatty helper outlived the invocation"
    finally:
        _kill_quietly(pid)


def test_a_cli_that_overruns_while_a_leftover_keeps_writing_is_timed_out(tmp_path, monkeypatch):
    """R4-F1: lines that keep arriving never hide a genuine overrun. The CLI is
    still running at its deadline, so the group is killed and the run is a
    timeout, however busy its stdout was."""
    import time

    from autoforge import executor
    from tests import claude_fake
    from tests.test_executor import _kill_quietly

    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    hook = claude_fake.line({"type": "system", "subtype": "hook_response"}) + "\n"
    started = time.monotonic()
    home, res = _claude_run(
        tmp_path,
        [claude_fake.init(), {"spawn_holder": 60, "every": 0.03, "then": hook}, {"sleep": 60}],
        timeout=1,
    )
    elapsed = time.monotonic() - started
    try:
        assert res.timed_out and res.exit_code == -1
        assert res.provider_failure is None
        assert elapsed < 10, elapsed
    finally:
        for pid in claude_fake.holders(home):
            _kill_quietly(pid)


@pytest.mark.parametrize(("linger", "timed_out"), [(0.3, False), (60, True)])
def test_the_deadline_ending_a_read_checks_the_cli_exit_first(
    tmp_path, monkeypatch, linger, timed_out
):
    """R4-F1: the CLI writes its result, lingers ``linger`` seconds and
    exits, while a silent helper holds its stdout, and the read waiting for
    the next line runs into the deadline (the poll is longer than the whole
    run here, so that read is the final polling interval). A CLI that exited
    before the deadline did not overrun it, as ``execute()``'s last look at
    the child is at the deadline: the helper gets the exit grace and the
    kill and the CLI's status and result are kept. A CLI still running at
    the deadline is a timeout, as before."""
    from autoforge import executor, providers
    from tests import claude_fake
    from tests.test_executor import _kill_quietly

    monkeypatch.setattr(providers, "CLAUDE_EXIT_POLL_SECONDS", 60.0)
    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(executor, "_KILL_GRACE_SECONDS", 0.5)
    home, res = _claude_run(
        tmp_path,
        [
            claude_fake.init(),
            {"spawn_holder": 60},
            claude_fake.result("done"),
            {"sleep": linger},
        ],
        timeout=3,
    )
    (pid,) = claude_fake.holders(home)
    try:
        assert res.timed_out is timed_out and res.provider_failure is None
        if timed_out:
            assert res.exit_code == -1
        else:
            assert res.exit_code == 0 and res.stdout == "done"
            assert res.descendants_killed
    finally:
        _kill_quietly(pid)


def test_a_helper_that_exits_within_the_grace_is_neither_killed_nor_reported(tmp_path, monkeypatch):
    """R3-F1, ADR 0002: a helper the CLI is shutting down as it exits holds
    stdout briefly and leaves on its own within the exit grace. The result is
    kept, nothing is killed or reported, and the wait is the helper's, not
    the grace's."""
    import time

    from autoforge import executor
    from tests import claude_fake

    monkeypatch.setattr(executor, "_EXIT_GRACE_SECONDS", 5.0)
    started = time.monotonic()
    _, res = _claude_run(
        tmp_path, [claude_fake.init(), {"spawn_holder": 0.3}, claude_fake.result("done")]
    )
    elapsed = time.monotonic() - started
    assert res.ok and res.exit_code == 0 and not res.timed_out
    assert res.provider_failure is None and res.stdout == "done"
    assert res.leftovers == ""
    assert elapsed < 5, elapsed


@pytest.mark.parametrize(
    ("then", "failure"),
    [
        ("RESULT", "claude: stream-json protocol violation: a result event outside a turn"),
        (
            "ERROR_TURN",
            "claude: the run ended in an error (subtype success, terminal_reason completed): again",
        ),
        ("TURN", "claude: exited inside a turn that has no result event (exit 0)"),
        ("not json\n", "claude: stream-json protocol violation: a stdout line is not JSON"),
        (
            '{"type": "system"',
            "claude: stream-json protocol violation: stdout ended inside an unterminated line",
        ),
        (
            "HOOK_LINES",
            "claude: stream-json protocol violation: stdout lines arrived faster than they "
            "were read",
        ),
    ],
)
def test_what_stdout_carries_after_the_cli_exits_is_still_validated(
    tmp_path, monkeypatch, then, failure
):
    """R3-F1: a helper that writes to the CLI's stdout after the CLI has exited
    (within the exit grace) is held to the stream's rules: the lines are fed
    to the reducer after the exit, so a result outside a turn, a turn that
    ends in an error result or without its result, a malformed line, an
    unterminated last line and an overflow of the pending queue still fail
    the run, with the CLI's own exit status."""
    from autoforge import providers
    from tests import claude_fake

    monkeypatch.setattr(providers, "CLAUDE_MAX_RECORD_BYTES", 4096)
    monkeypatch.setattr(providers, "CLAUDE_MAX_PENDING_BYTES", 8192)
    hook = claude_fake.line({"type": "system", "subtype": "hook_response"})
    expand = {
        "RESULT": claude_fake.result("again") + "\n",
        "ERROR_TURN": claude_fake.init() + "\n" + claude_fake.result("again", is_error=True) + "\n",
        "TURN": claude_fake.init() + "\n",
        "HOOK_LINES": (hook + "\n") * 400,
    }
    _, res = _claude_run(
        tmp_path,
        [
            claude_fake.init(),
            {"spawn_holder": 0.5, "then": expand.get(then, then)},
            claude_fake.result("done"),
        ],
    )
    assert res.provider_failure == failure and res.stdout == ""
    assert res.exit_code == 0 and not res.timed_out
    assert res.leftovers == ""


def test_an_oversize_tool_result_line_is_skipped_and_counted(tmp_path, monkeypatch):
    from autoforge import providers
    from tests import claude_fake

    monkeypatch.setattr(providers, "CLAUDE_MAX_RECORD_BYTES", 4096)
    monkeypatch.setattr(providers, "CLAUDE_MAX_PENDING_BYTES", 8192)
    _, res = _claude_run(
        tmp_path,
        [
            claude_fake.tool_use("t1", "Read", file_path="big.txt"),
            claude_fake.tool_result("t1", "x" * 50_000),
            claude_fake.result("done"),
        ],
    )
    assert res.ok and res.provider_failure is None and res.stdout == "done"
    assert res.provider_summary["oversize_records"] == 1


def test_an_oversize_result_line_fails_the_run(tmp_path, monkeypatch):
    from autoforge import providers
    from tests import claude_fake

    monkeypatch.setattr(providers, "CLAUDE_MAX_RECORD_BYTES", 4096)
    monkeypatch.setattr(providers, "CLAUDE_MAX_PENDING_BYTES", 8192)
    _, res = _claude_run(tmp_path, [claude_fake.result("y" * 50_000)])
    assert res.stdout == ""
    assert res.provider_failure == (
        "claude: exited without a result event (1 line(s) past the per-line bound were "
        "skipped; the result must arrive whole in one line) (exit 0)"
    )


def test_the_claude_stream_launch_contains_orphans_and_keeps_stdin_closed(tmp_path, monkeypatch):
    from autoforge import providers
    from tests import claude_fake

    seen = []
    real = providers.start_duplex

    def spy(req):
        seen.append(req)
        return real(req)

    monkeypatch.setattr(providers, "start_duplex", spy)
    home, profile = claude_fake.fake_claude(tmp_path, [claude_fake.result("done")])
    req = AgentRequest(
        "FIX", "p", str(tmp_path), profile, 900, 30, env_allowlist=("PATH", "HOME", "CLAUDE_*")
    )
    res = ClaudeCodeProvider().execute(req)
    assert res.ok and res.stdout == "done"
    (duplex,) = seen
    assert duplex.contain_orphans and duplex.stdin_pipe is False
    # The provider's names are added once, never duplicated.
    assert duplex.env_allowlist == ("PATH", "HOME", "CLAUDE_*", "ANTHROPIC_*")
    assert duplex.command == ClaudeCodeProvider().build_command(req)


def test_a_progress_sink_that_raises_never_fails_the_claude_run(tmp_path):
    from tests import claude_fake

    calls = []

    def sink(event):
        calls.append(event)
        raise RuntimeError("terminal gone")

    home, profile = claude_fake.fake_claude(
        tmp_path, [claude_fake.init(), claude_fake.thinking("x"), claude_fake.result("done")]
    )
    res = ClaudeCodeProvider().execute(
        AgentRequest("FIX", "p", str(tmp_path), profile, None, 30, progress=sink)
    )
    assert res.ok and res.stdout == "done" and res.provider_failure is None
    assert len(calls) == 1  # dropped after its first failure


# -- a fake `opencode` behind the real executor (#186) -------------------------------
_FAKE_OPENCODE = r"""
import hashlib, json, os, sys

home = sys.argv[1]
argv = sys.argv[2:]
with open(os.path.join(home, "mode"), encoding="utf-8") as f:
    mode = f.read()
if mode == "skip-stdin":
    data = b""
elif mode == "read-one-byte":
    data = os.read(0, 1)
else:
    data = sys.stdin.buffer.read()
with open(os.path.join(home, "log.json"), "w", encoding="utf-8") as f:
    json.dump({"argv": argv, "sha256": hashlib.sha256(data).hexdigest(), "len": len(data)}, f)
sys.stderr.write("> build \u00b7 tool traces go to stderr\n")
with open(os.path.join(home, "answer"), encoding="utf-8") as f:
    sys.stdout.write(f.read())
"""


def _fake_opencode(tmp_path, mode="read", answer="done\n"):
    home = tmp_path / "fake-opencode"
    home.mkdir()
    (home / "fake.py").write_text(_FAKE_OPENCODE, encoding="utf-8")
    (home / "mode").write_text(mode, encoding="utf-8")
    (home / "answer").write_text(answer, encoding="utf-8")
    command = home / "opencode"
    command.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{home / "fake.py"}" "{home}" "$@"\n',
        encoding="utf-8",
    )
    command.chmod(0o755)
    profile = ProfileConfig(
        name="review_round_1",
        provider="opencode",
        model="openai/gpt-5.6-luna",
        effort="high",
        command=str(command),
        options={"output_format": "default"},
    )
    return home, profile


def test_a_fake_opencode_receives_the_prompt_verbatim_and_answers_on_stdout(tmp_path):
    from autoforge.result_parser import parse_control_result
    from autoforge.transitions import Phase
    from tests.conftest import PR, SHA_A, block, comment_url

    review = {
        "phase": "REVIEW",
        "status": "success",
        "round": 1,
        "reviewed_head_sha": SHA_A,
        "review_comment_url": comment_url(PR, 1),
        "needs_fix_round": False,
        "findings": [],
    }
    home, profile = _fake_opencode(tmp_path, answer=block(review))
    head = '-x --help\r\n# Review "PR" `code` $(id) \\ back\n{"k": "v"}\n\u00e9\u4e2d\n'
    prompt = head + "filler line\n" * 20_000  # well over one pipe buffer
    res = OpenCodeProvider().execute(
        AgentRequest("REVIEW", prompt, str(tmp_path), profile, None, 30)
    )
    assert res.ok and not res.provider_failure, (res.exit_code, res.stderr)
    log = json.loads((home / "log.json").read_text(encoding="utf-8"))
    sent = prompt.encode("utf-8")
    assert log["len"] == len(sent) and log["sha256"] == hashlib.sha256(sent).hexdigest()
    assert log["argv"] == [
        "run",
        "--standalone",
        "-m",
        "openai/gpt-5.6-luna#high",
        "--format",
        "default",
    ]
    assert "tool traces" in res.stderr and "tool traces" not in res.stdout
    assert parse_control_result(res.stdout_tail, Phase.REVIEW) == review


@pytest.mark.parametrize(
    ("mode", "prompt"),
    [
        ("skip-stdin", "x" * (4 * 1024 * 1024)),  # more than any pipe buffer holds
        ("skip-stdin", "Review PR #1."),  # fits in the pipe: written, never read
        ("read-one-byte", "Review PR #1."),  # a prefix read, then a clean exit
    ],
    ids=["no-read-large", "no-read-short", "prefix-short"],
)
def test_a_fake_opencode_that_exits_without_reading_its_whole_prompt_fails(tmp_path, mode, prompt):
    """An exit 0 whose answer parses is still a failed run when the CLI did
    not read the whole prompt, however short the prompt is: the answer
    cannot be to a prompt the CLI never saw (the PR #188 review)."""
    from autoforge.result_parser import parse_control_result
    from autoforge.transitions import Phase
    from tests.conftest import PR, SHA_A, block, comment_url

    review = {
        "phase": "REVIEW",
        "status": "success",
        "round": 1,
        "reviewed_head_sha": SHA_A,
        "review_comment_url": comment_url(PR, 1),
        "needs_fix_round": False,
        "findings": [],
    }
    _, profile = _fake_opencode(tmp_path, mode=mode, answer=block(review))
    res = OpenCodeProvider().execute(
        AgentRequest("REVIEW", prompt, str(tmp_path), profile, None, 30)
    )
    assert res.exit_code == 0 and not res.timed_out
    assert parse_control_result(res.stdout_tail, Phase.REVIEW) == review
    taken = 1 if mode == "read-one-byte" else 0
    assert res.provider_failure == (
        "opencode: the prompt was not delivered: the CLI exited without reading "
        f"the last {len(prompt) - taken} bytes of it"
    )


@pytest.mark.parametrize(
    "text, expected",
    [
        ("opencode v2.0.23", (2, 0, 23)),
        ("opencode v2.0.23\n", (2, 0, 23)),
        ("v2.1.0", (2, 1, 0)),
        ("1.18.34", (1, 18, 34)),
        ("opencode v2.0.0-beta.1", (2, 0, -1)),
        ("", None),
        ("opencode", None),
        ("opencode version 2.0.0", None),
        ("opencode v2.0", None),
        ("opencode v2.0.23\nextra", None),
        ("pi 2.0.0", None),
    ],
)
def test_parse_opencode_version(text, expected):
    from autoforge.providers import OPENCODE_MIN_VERSION, parse_opencode_version

    assert parse_opencode_version(text) == expected
    if expected is not None:
        assert (expected >= OPENCODE_MIN_VERSION) is (
            text.strip() in ("opencode v2.0.23", "v2.1.0")
        )


def test_unknown_provider_rejected():
    with pytest.raises(ConfigurationError, match="unknown provider"):
        provider_for(ProfileConfig(name="x", provider="magic", model="m"))


def test_scripted_provider_records_calls():
    sp = ScriptedProvider(["one", "two"], exit_code=0)
    p = ProfileConfig(name="x", provider="scripted", model="m")
    req = AgentRequest(
        phase="REVIEW",
        prompt="p",
        cwd=".",
        profile=p,
        idle_timeout_seconds=None,
        max_runtime_seconds=5,
    )
    assert sp.execute(req).stdout == "one"
    assert sp.execute(req).stdout == "two"
    assert sp.execute(req).stdout == ""
    assert len(sp.calls) == 3 and sp.calls[0].phase == "REVIEW"


def test_registry_overrides_by_provider_name():
    fake = ScriptedProvider([])
    reg = ProviderRegistry(overrides={"claude": fake})
    assert reg.get(default_config().profile("fix")) is fake
    assert isinstance(reg.get(default_config().profile("review_round_1")), OpenCodeProvider)


def test_provider_execute_uses_injected_runner():
    seen = []

    def runner(req):
        from autoforge.executor import ExecutionResult

        seen.append(req)
        return ExecutionResult(
            command=req.command,
            cwd=req.cwd,
            exit_code=0,
            stdout="ok",
            stderr="",
            started_at="t",
            finished_at="t",
        )

    prov = ClaudeCodeProvider(runner=runner)
    p = _text(default_config().profile("fix"))
    res = prov.execute(AgentRequest("FIX", "prompt", "/tmp", p, None, 7))
    assert res.ok and res.stdout == "ok" and res.model == "fable"
    assert seen[0].timeout_seconds == 7 and seen[0].cwd == "/tmp"
    assert seen[0].command[-1] == "prompt"


@pytest.mark.parametrize(
    ("provider", "profile"), [(ClaudeCodeProvider, "fix"), (OpenCodeProvider, "review_round_1")]
)
def test_one_shot_agent_launch_asks_for_orphan_containment(provider, profile):
    """What an agent detaches from its process group is caught too
    (ADR 0002 §4b, #132), whichever provider launches it."""
    seen = []
    p = default_config().profile(profile)
    if provider is ClaudeCodeProvider:
        p = _text(p)  # the one-shot path; the stream path is tested with the fake below
    provider(runner=_capture_runner(seen)).execute(
        AgentRequest("FIX", "prompt", "/tmp", p, None, 7)
    )
    assert seen[0].contain_orphans


def test_agent_result_carries_capture_truncation_and_tail():
    """The engine parses ``stdout_tail``, so the executor's truncation facts
    must survive the provider boundary unchanged (#53)."""
    from autoforge.executor import ExecutionResult

    res = ExecutionResult(
        command=["x"],
        cwd=None,
        exit_code=0,
        stdout="head\n[marker]\ntail",
        stderr="e",
        started_at="t",
        finished_at="t",
        stdout_truncated=True,
        stderr_truncated=True,
        stdout_tail_offset=len("head\n[marker]\n"),
        descendants_killed=True,
        group_survived_kill=True,
        capture_abandoned=True,
        orphans_killed=True,
        orphan_survived_kill=True,
        orphans_unchecked=True,
    )
    p = default_config().profile("analyze_execute")
    got = AgentExecutionResult.from_execution(res, p)
    assert got.stdout_truncated and got.stderr_truncated
    assert got.stdout_tail == "tail"
    assert got.stdout == "head\n[marker]\ntail"
    # What the invocation left behind crosses the boundary too (#85), and
    # is described by the executor's sentence.
    assert got.descendants_killed and got.group_survived_kill and got.capture_abandoned
    assert got.orphans_killed and got.orphan_survived_kill and got.orphans_unchecked
    assert got.leftovers == res.leftovers != ""


# -- allow-listed environment (#10) -------------------------------------------------
def _capture_runner(seen):
    def runner(req):
        from autoforge.executor import ExecutionResult

        seen.append(req)
        return ExecutionResult(req.command, req.cwd, 0, "ok", "", "t", "t")

    return runner


@pytest.mark.parametrize(("ceiling", "expected"), [(None, MAX_DEADLINE_SECONDS), (120, 120)])
def test_a_one_shot_launch_carries_both_limits(ceiling, expected):
    """#193: the idle limit reaches the executor as is; an unset ceiling is
    the executor's one-week backstop, never 'no timeout'."""
    seen = []
    OpenCodeProvider(runner=_capture_runner(seen)).execute(
        AgentRequest(
            "REVIEW", "p", "/tmp", default_config().profile("review_round_1"), 300, ceiling
        )
    )
    assert seen[0].idle_timeout_seconds == 300 and seen[0].timeout_seconds == expected


def test_a_one_shot_result_names_the_limit_that_fired():
    from autoforge.executor import ExecutionResult

    def runner(req):
        return ExecutionResult(
            req.command,
            req.cwd,
            -1,
            "",
            "",
            "t",
            "t",
            timed_out=True,
            timeout_limit="idle",
            last_activity_at="2026-10-07T08:31:02+00:00",
        )

    res = OpenCodeProvider(runner=runner).execute(
        AgentRequest("REVIEW", "p", "/tmp", default_config().profile("review_round_1"), 300, None)
    )
    assert res.timed_out and res.timeout_limit == "idle"
    assert res.last_activity_at == "2026-10-07T08:31:02+00:00"


def test_only_claude_text_mode_reports_no_activity():
    cfg = default_config()
    fix = cfg.profile("fix")
    assert ClaudeCodeProvider().reports_activity(fix)
    assert not ClaudeCodeProvider().reports_activity(_text(fix))
    assert OpenCodeProvider().reports_activity(cfg.profile("review_round_1"))


def test_provider_passes_no_allowlist_through_when_the_request_has_none():
    seen = []
    prov = ClaudeCodeProvider(runner=_capture_runner(seen))
    prov.execute(
        AgentRequest("FIX", "prompt", "/tmp", _text(default_config().profile("fix")), None, 7)
    )
    assert seen[0].env_allowlist is None


def test_provider_adds_its_own_environment_names_to_the_request_allowlist():
    seen = []
    prov = ClaudeCodeProvider(runner=_capture_runner(seen))
    req = AgentRequest(
        "FIX",
        "prompt",
        "/tmp",
        _text(default_config().profile("fix")),
        None,
        7,
        env_allowlist=("PATH", "HOME"),
    )
    prov.execute(req)
    assert seen[0].env_allowlist == ("PATH", "HOME", "ANTHROPIC_*", "CLAUDE_*")
    assert prov.environment_allowlist(req) == seen[0].env_allowlist


def test_provider_environment_names_are_deduplicated_not_repeated():
    prov = OpenCodeProvider()
    req = AgentRequest(
        "FIX",
        "p",
        "/tmp",
        default_config().profile("fix"),
        None,
        7,
        env_allowlist=("ANTHROPIC_*", "PATH", "PATH"),
    )
    got = prov.environment_allowlist(req)
    assert got is not None and len(got) == len(set(got))
    assert got[:2] == ("ANTHROPIC_*", "PATH") and "OPENCODE_*" in got and "OPENAI_*" in got


def test_every_real_provider_declares_only_valid_environment_patterns():
    from autoforge.executor import is_env_pattern

    for cls in (ClaudeCodeProvider, OpenCodeProvider, PiProvider):
        assert cls.environment_names, cls
        assert all(is_env_pattern(n) for n in cls.environment_names), cls.environment_names
    assert ScriptedProvider.environment_names == ()


# -- strict per-provider option keys (#129) ---------------------------------------
@pytest.mark.parametrize(
    "provider, model, key",
    [
        ("claude", "fable", "permision_mode"),
        ("claude", "fable", "auto_approve"),  # an OpenCode knob on a Claude profile
        ("opencode", "openai/gpt-5.6-luna", "permission_mode"),
        ("opencode", "openai/gpt-5.6-luna", "autoapprove"),
        ("pi", "openai/gpt-5.6-terra", "require_oath"),
        ("pi", "openai/gpt-5.6-terra", "tool"),
        ("pi", "openai/gpt-5.6-terra", "extensions"),  # #132: no knob re-enables them
    ],
)
def test_an_unknown_option_key_is_rejected_with_the_profile_and_accepted_set(provider, model, key):
    p = ProfileConfig(name="r2", provider=provider, model=model, options={key: "x"})
    accepted = provider_for(p).option_keys
    assert accepted is not None
    with pytest.raises(ConfigurationError) as err:
        provider_for(p).validate_profile(p)
    message = str(err.value)
    assert "'r2'" in message and key in message
    assert all(k in message for k in accepted), message


def test_the_shipped_option_keys_are_accepted():
    for name in ("analyze_execute", "fix", "review_round_1", "update_epic"):
        p = default_config().profile(name)
        provider_for(p).validate_profile(p)
    claude = ProfileConfig(
        name="x", provider="claude", model="fable", options={"session_persistence": "true"}
    )
    ClaudeCodeProvider().validate_profile(claude)


def test_the_scripted_test_provider_leaves_option_keys_unchecked():
    p = ProfileConfig(name="x", provider="scripted", model="m", options={"anything": "1"})
    assert ScriptedProvider.option_keys is None
    ScriptedProvider().validate_profile(p)


# -- Pi (#129, ADR 0003) -----------------------------------------------------------
def _pi(**kwargs):
    fields = {"name": "review_round_2_5", "provider": "pi", "model": "openai/gpt-5.6-terra"}
    fields.update(kwargs)
    return ProfileConfig(**fields)


def test_pi_argv_shape():
    p = _pi(effort="high")
    argv = PiProvider().build_command_for(p, "review $(id); --approve")
    assert argv == [
        "pi",
        "--mode",
        "rpc",
        "--no-session",
        "--no-approve",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--offline",
        "--tools",
        "read,bash",
        "--model",
        "openai/gpt-5.6-terra",
        "--thinking",
        "high",
    ]
    # The prompt travels on stdin as an RPC record (#131), never in argv.
    assert not any("review" in a for a in argv)
    assert PiProvider().build_command_for(_pi(command="/opt/pi/bin/pi"), "x")[0] == "/opt/pi/bin/pi"


def test_pi_is_registered_and_known_to_config():
    from autoforge.config import KNOWN_PROVIDERS

    assert "pi" in KNOWN_PROVIDERS
    assert isinstance(provider_for(_pi()), PiProvider)
    assert _pi().build_command("prompt")[:2] == ["pi", "--mode"]


@pytest.mark.parametrize("effort", ["off", "minimal", "low", "medium", "high", "xhigh", "max"])
def test_pi_accepts_every_thinking_level(effort):
    PiProvider().validate_profile(_pi(effort=effort))


@pytest.mark.parametrize("effort", ["", "none", "HIGH", "ultra", " high"])
def test_pi_rejects_a_missing_or_unknown_effort(effort):
    with pytest.raises(ConfigurationError, match="'review_round_2_5'.*effort"):
        PiProvider().validate_profile(_pi(effort=effort))


@pytest.mark.parametrize(
    "model",
    [
        "openai/gpt-5.6-terra",
        "openai-codex/gpt-5.6",
        "openrouter/vendor:model-id",  # a colon that is not a thinking level is part of the id
        "openai/gpt-5.6:latest",
        "a1-b2/x",
    ],
)
def test_pi_accepts_a_provider_slash_id_model(model):
    PiProvider().validate_profile(_pi(model=model))


@pytest.mark.parametrize(
    "model, problem",
    [
        ("", "empty"),
        ("gpt-5.6-terra", "exactly one '/'"),
        ("openai/vendor/model", "exactly one '/'"),
        ("/gpt-5.6", "provider part"),
        ("OpenAI/gpt-5.6", "provider part"),
        ("open_ai/gpt-5.6", "provider part"),
        ("openai/", "model id is empty"),
        ("openai/gpt 5.6", "whitespace"),
        ("openai/gpt-5.6\t", "whitespace"),
        ("openai/gpt-5.6-terra:high", "':<thinking>'"),
        ("openai/vendor:model:off", "':<thinking>'"),
    ],
)
def test_pi_rejects_a_malformed_model(model, problem):
    with pytest.raises(ConfigurationError) as err:
        PiProvider().validate_profile(_pi(model=model))
    assert "'review_round_2_5'" in str(err.value) and problem in str(err.value)


@pytest.mark.parametrize(
    "extra",
    [
        ["--approve"],
        ["-a"],
        ["--api-key", "sk-test"],
        ["--session-id", "abc"],
        ["--system-prompt", "x"],
        ["--mode", "json"],
        ["@notes.md"],
        ["--verbose"],  # harmless-looking flags are refused too: no pass-through at all
    ],
)
def test_pi_rejects_any_extra_args(extra):
    with pytest.raises(ConfigurationError) as err:
        PiProvider().validate_profile(_pi(extra_args=extra))
    message = str(err.value)
    assert "'review_round_2_5'" in message and "extra_args" in message


@pytest.mark.parametrize("value", ["true", "false"])
def test_pi_require_oauth_is_a_boolean(value):
    p = _pi(options={"require_oauth": value})
    PiProvider().validate_profile(p)
    assert PiProvider.require_oauth(p) is (value == "true")
    assert PiProvider.require_oauth(_pi()) is True  # the default


@pytest.mark.parametrize("value", ["", "yes", "1", "False"])
def test_pi_require_oauth_rejects_a_non_boolean(value):
    with pytest.raises(ConfigurationError, match="require_oauth"):
        PiProvider().validate_profile(_pi(options={"require_oauth": value}))


def test_pi_execution_never_goes_through_the_one_shot_runner(tmp_path):
    """Pi runs over the duplex handle (#131); the injected runner carries
    only the read-only auth preflight (#132), and a missing executable is a
    spawn failure, not a fallback."""
    from autoforge.errors import ExecutionError

    seen = []
    prov = PiProvider(runner=_capture_runner(seen))
    profile = _pi(command=str(tmp_path / "no-such-pi"), options={"require_oauth": "false"})
    req = AgentRequest("REVIEW", "p", str(tmp_path), profile, None, 7, env_allowlist=("PATH",))
    with pytest.raises(ExecutionError, match="no-such-pi"):
        prov.execute(req)
    assert seen == []  # the one-shot runner was never called


def test_pi_auth_preflight_runs_in_the_launch_environment_through_the_runner(tmp_path):
    seen = []
    prov = PiProvider(runner=_capture_runner(seen))
    profile = _pi(command="pi")
    req = AgentRequest("REVIEW", "p", str(tmp_path), profile, None, 7, env_allowlist=("PATH",))
    res = prov.execute(req)  # the captured runner prints "ok": not a result Pi would give
    [check] = seen
    assert check.command == prov.auth_check_command(profile) and check.cwd == str(tmp_path)
    assert check.env_allowlist == prov.environment_allowlist(req) and check.contain_orphans
    assert res.provider_failure.startswith("pi: the auth preflight refused the launch: ")


def test_pi_environment_names_are_explicit_and_carry_no_provider_key():
    import fnmatch

    names = PiProvider.environment_names
    assert names and all(not n.endswith("*") for n in names), names
    for pattern in ("OPENAI_*", "ANTHROPIC_*"):
        assert not fnmatch.filter(names, pattern), names
    assert "PI_CODING_AGENT_DIR" in names


def test_pi_probe_commands_are_read_only():
    p = _pi(command="pi")
    assert PiProvider().version_command(p) == ["pi", "--version"]
    argv = PiProvider().auth_check_command(p)
    assert argv[:3] == ["pi", "auth", "check"]
    assert argv[argv.index("--model") + 1] == "openai/gpt-5.6-terra"
    assert "--json" in argv and "--no-refresh" in argv
    assert "--credentials" not in argv
    assert not any(a.startswith("print-") for a in argv)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("1.0.0", (1, 0, 0)),
        ("1.0.1\n", (1, 0, 1)),
        ("12.34.56", (12, 34, 56)),
        ("1.0.0-rc.1", (1, 0, -1)),
        ("1.1.0-beta", (1, 1, -1)),
        ("", None),
        ("pi 1.0.0", None),
        ("v1.0.0", None),
        ("1.0", None),
        ("1.0.0\nextra", None),
        ("one.two.three", None),
    ],
)
def test_parse_pi_version(text, expected):
    from autoforge.providers import PI_MIN_VERSION, parse_pi_version

    assert parse_pi_version(text) == expected
    if text == "1.0.0-rc.1":
        assert parse_pi_version(text) < PI_MIN_VERSION


@pytest.mark.parametrize(
    "stdout, code, expected",
    [
        ('{"status":"ready","provider":"openai","authType":"oauth"}', 0, ("ready", "", "oauth")),
        (
            '{"status":"ready","provider":"openai","authType":"api_key"}\n',
            0,
            ("ready", "", "api_key"),
        ),
        (
            '{"status":"not_ready","provider":"openai","reason":"credentials_not_configured"}',
            1,
            ("not_ready", "credentials_not_configured", ""),
        ),
        (
            '{"status":"invalid","provider":"openai/gpt-5.6","reason":"invalid_state"}',
            2,
            ("invalid", "invalid_state", ""),
        ),
    ],
)
def test_parse_pi_auth_check_accepts_the_documented_shapes(stdout, code, expected):
    from autoforge.providers import parse_pi_auth_check

    got = parse_pi_auth_check(stdout, code)
    assert (got.status, got.reason, got.auth_type) == expected


@pytest.mark.parametrize(
    "stdout, code",
    [
        ("", 2),
        ("ready", 0),
        ("[]", 0),
        ('{"status":"ready","provider":"openai","authType":"oauth"}\n{}', 0),
        ('{"status":"ready","provider":"openai","authType":"oauth"}', 1),  # exit disagrees
        ('{"status":"not_ready","provider":"openai","reason":"credentials_not_configured"}', 0),
        ('{"status":"ready","provider":"openai"}', 0),  # ready without authType
        ('{"status":"ready","provider":"openai","authType":"token"}', 0),
        ('{"status":"ready","provider":"openai","authType":"oauth","reason":"x"}', 0),
        ('{"status":"not_ready","provider":"openai"}', 1),
        ('{"status":"not_ready","provider":"openai","reason":"expired"}', 1),
        ('{"status":"maybe","provider":"openai"}', 1),
        ('{"status":"ready","authType":"oauth"}', 0),
        ('{"status":"ready","provider":"","authType":"oauth"}', 0),
        ('{"status":"ready","provider":"open ai","authType":"oauth"}', 0),
        ('{"status":"ready","provider":"openai","authType":"oauth","credentials":"sk-x"}', 0),
    ],
)
def test_parse_pi_auth_check_fails_closed_on_drift(stdout, code):
    from autoforge.providers import parse_pi_auth_check

    with pytest.raises(ValueError) as err:
        parse_pi_auth_check(stdout, code)
    assert "sk-x" not in str(err.value)
