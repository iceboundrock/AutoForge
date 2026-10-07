"""Provider adapters: exact CLI argv shapes, validation, scripted fake."""

import dataclasses
import hashlib
import json
import sys

import pytest

from autoforge.config import ProfileConfig, default_config
from autoforge.errors import ConfigurationError
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
    assert prov.stdin_payload(AgentRequest("REVIEW", prompt, ".", p, 5)) == prompt.encode()


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
        AgentRequest("REVIEW", prompt, "/tmp", p, 7)
    )
    # A lone surrogate cannot be UTF-8; it becomes U+FFFD rather than failing.
    assert seen[0].stdin_data == 'review \ufffd \u00e9 "q"'.encode()
    assert prompt not in seen[0].command


def test_claude_keeps_stdin_on_dev_null():
    seen = []
    p = _text(default_config().profile("fix"))
    ClaudeCodeProvider(runner=_capture_runner(seen)).execute(AgentRequest("FIX", "p", "/tmp", p, 7))
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
    res = OpenCodeProvider(runner=runner).execute(AgentRequest("REVIEW", "p" * 11, "/tmp", p, 7))
    assert bool(res.provider_failure) is failure
    if failure:
        assert res.provider_failure == (
            "opencode: the prompt was not delivered: the CLI exited without reading "
            "the last 10 bytes of it"
        )


# -- a fake `claude` streaming stream-json under the duplex handle (#192) ------------
def _claude_run(tmp_path, lines, timeout=30, events=None, **kw):
    from tests import claude_fake

    home, profile = claude_fake.fake_claude(tmp_path, lines, **kw)
    sink = None if events is None else events.append
    req = AgentRequest("FIX", "fix it", str(tmp_path), profile, timeout, progress=sink)
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
        "FIX", "p", str(tmp_path), profile, 30, env_allowlist=("PATH", "HOME", "CLAUDE_*")
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
        AgentRequest("FIX", "p", str(tmp_path), profile, 30, progress=sink)
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
    res = OpenCodeProvider().execute(AgentRequest("REVIEW", prompt, str(tmp_path), profile, 30))
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
    res = OpenCodeProvider().execute(AgentRequest("REVIEW", prompt, str(tmp_path), profile, 30))
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
    req = AgentRequest(phase="REVIEW", prompt="p", cwd=".", profile=p, timeout_seconds=5)
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
    res = prov.execute(AgentRequest("FIX", "prompt", "/tmp", p, 7))
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
    provider(runner=_capture_runner(seen)).execute(AgentRequest("FIX", "prompt", "/tmp", p, 7))
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


def test_provider_passes_no_allowlist_through_when_the_request_has_none():
    seen = []
    prov = ClaudeCodeProvider(runner=_capture_runner(seen))
    prov.execute(AgentRequest("FIX", "prompt", "/tmp", _text(default_config().profile("fix")), 7))
    assert seen[0].env_allowlist is None


def test_provider_adds_its_own_environment_names_to_the_request_allowlist():
    seen = []
    prov = ClaudeCodeProvider(runner=_capture_runner(seen))
    req = AgentRequest(
        "FIX",
        "prompt",
        "/tmp",
        _text(default_config().profile("fix")),
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
    req = AgentRequest("REVIEW", "p", str(tmp_path), profile, 7, env_allowlist=("PATH",))
    with pytest.raises(ExecutionError, match="no-such-pi"):
        prov.execute(req)
    assert seen == []  # the one-shot runner was never called


def test_pi_auth_preflight_runs_in_the_launch_environment_through_the_runner(tmp_path):
    seen = []
    prov = PiProvider(runner=_capture_runner(seen))
    profile = _pi(command="pi")
    req = AgentRequest("REVIEW", "p", str(tmp_path), profile, 7, env_allowlist=("PATH",))
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
