"""Provider adapters: exact CLI argv shapes, validation, scripted fake."""

import pytest

from autoforge.config import ProfileConfig, default_config
from autoforge.errors import ConfigurationError
from autoforge.providers import (
    AgentExecutionResult,
    AgentRequest,
    ClaudeCodeProvider,
    OpenCodeProvider,
    ProviderRegistry,
    ScriptedProvider,
    provider_for,
)


def test_claude_argv_shape():
    p = default_config().profile("analyze_execute")
    argv = ClaudeCodeProvider().build_command_for(p, "do it; rm -rf /")
    assert argv[0] == "claude"
    assert argv[1] == "-p"
    assert argv[argv.index("--output-format") + 1] == "text"
    assert argv[argv.index("--model") + 1] == "fable"
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--no-session-persistence" in argv
    assert argv[-2:] == ["--", "do it; rm -rf /"]  # prompt is one literal element


def test_claude_rejects_non_text_output_format():
    # Regression: a claude profile with opencode's `output_format: default`
    # must fail at config validation, not at runtime with
    # `option '--output-format <format>' argument 'default' is invalid`.
    for bad in ("default", "json", "stream-json"):
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
    p = default_config().profile("review_round_1")
    argv = OpenCodeProvider().build_command_for(p, "review $(id)")
    assert argv[:2] == ["opencode", "run"]
    assert argv[argv.index("-m") + 1] == "openai/gpt-5.6-luna"
    assert argv[argv.index("--variant") + 1] == "high"
    assert argv[argv.index("--format") + 1] == "default"
    assert "--auto" not in argv
    assert argv[-2:] == ["--", "review $(id)"]  # prompt is one literal element


def test_opencode_prompt_starting_with_a_dash_is_not_a_flag():
    """#7 (item 5): `--` ends option parsing, so a prompt beginning with `-`
    reaches opencode as the message instead of being parsed as an option."""
    p = default_config().profile("review_round_1")
    argv = OpenCodeProvider().build_command_for(p, "--help")
    assert argv[-2:] == ["--", "--help"]
    # Every option precedes the separator; nothing follows it but the prompt.
    assert argv.index("--") > argv.index("--format")


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
    assert "--auto" in argv and argv[-4:] == ["--agent", "reviewer", "--", "x"]


def test_opencode_requires_provider_slash_model():
    p = ProfileConfig(name="r", provider="opencode", model="gpt-5.6-luna", effort="high")
    with pytest.raises(ConfigurationError, match="provider/model"):
        OpenCodeProvider().validate_profile(p)


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
    p = default_config().profile("fix")
    res = prov.execute(AgentRequest("FIX", "prompt", "/tmp", p, 7))
    assert res.ok and res.stdout == "ok" and res.model == "fable"
    assert seen[0].timeout_seconds == 7 and seen[0].cwd == "/tmp"
    assert seen[0].command[-1] == "prompt"


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
    )
    p = default_config().profile("analyze_execute")
    got = AgentExecutionResult.from_execution(res, p)
    assert got.stdout_truncated and got.stderr_truncated
    assert got.stdout_tail == "tail"
    assert got.stdout == "head\n[marker]\ntail"
    # What the invocation left behind crosses the boundary too (#85), and
    # is described by the executor's sentence.
    assert got.descendants_killed and got.group_survived_kill and got.capture_abandoned
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
    prov.execute(AgentRequest("FIX", "prompt", "/tmp", default_config().profile("fix"), 7))
    assert seen[0].env_allowlist is None


def test_provider_adds_its_own_environment_names_to_the_request_allowlist():
    seen = []
    prov = ClaudeCodeProvider(runner=_capture_runner(seen))
    req = AgentRequest(
        "FIX", "prompt", "/tmp", default_config().profile("fix"), 7, env_allowlist=("PATH", "HOME")
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

    for cls in (ClaudeCodeProvider, OpenCodeProvider):
        assert cls.environment_names, cls
        assert all(is_env_pattern(n) for n in cls.environment_names), cls.environment_names
    assert ScriptedProvider.environment_names == ()
