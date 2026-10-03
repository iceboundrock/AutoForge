"""Provider adapters: exact CLI argv shapes, validation, scripted fake."""

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
        ("pi", "openai/gpt-5.6-terra", "tools"),  # #132 adds it; not accepted yet
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


def test_pi_execution_is_refused_until_the_rpc_adapter_lands():
    from autoforge.errors import ExecutionError

    seen = []
    prov = PiProvider(runner=_capture_runner(seen))
    req = AgentRequest("REVIEW", "p", "/tmp", _pi(), 7, env_allowlist=("PATH",))
    with pytest.raises(ExecutionError, match="not implemented"):
        prov.execute(req)
    assert seen == []  # nothing was launched


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
