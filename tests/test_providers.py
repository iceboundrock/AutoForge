"""Provider adapters: exact CLI argv shapes, validation, scripted fake."""

import pytest

from autoforge.config import ProfileConfig, default_config
from autoforge.errors import ConfigurationError
from autoforge.providers import (
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
    assert argv[-1] == "review $(id)"


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
    assert "--auto" in argv and argv[-3:] == ["--agent", "reviewer", "x"]


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
