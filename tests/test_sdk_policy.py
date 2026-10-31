"""The unattended SDK policy (#149; ADR 0007 D2.7, D10.1, D10.2; docs/sdk-policy.md).

Fake fixtures only, except the two opt-in probes at the end, which run an
installed Claude Code CLI or OpenCode runtime under the policy against
sentinel configuration (``AUTOFORGE_CLAUDE_SDK_PROBE_CLI``,
``AUTOFORGE_OPENCODE_SDK_PROBE_SERVER``). Every secret here is synthetic.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from autoforge.config import (
    DEFAULT_ENV_ALLOWLIST,
    AutoForgeConfig,
    ExecutionConfig,
    ProfileConfig,
    validate_profile,
)
from autoforge.errors import ConfigurationError, InteractionDataError
from autoforge.executor import ExecutionRequest, execute
from autoforge.providers import (
    AgentRequest,
    ClaudeSdkProvider,
    OpenCodeSdkProvider,
    provider_for,
)
from autoforge.redaction import MAX_GROWTH_FACTOR, redact
from autoforge.sandbox_policy import SANDBOX_DIR, AuthMode
from autoforge.sdk_policy import (
    ASK,
    CLAUDE_SDK_DENIED_TOOLS,
    CLAUDE_SDK_HOST_DENY,
    CLAUDE_SDK_QUESTION_TOOL,
    CLAUDE_SDK_READ_TOOLS,
    CLAUDE_SDK_SETTINGS,
    CLAUDE_SDK_TOOLS,
    CLAUDE_SDK_WRITE_TOOLS,
    DENY,
    MAX_INPUT_BYTES,
    MAX_OPTIONS,
    OPENCODE_SDK_AGENT,
    OPENCODE_SDK_DENIED,
    OPENCODE_SDK_FIXED_ENV,
    OPENCODE_SDK_PASSWORD_ENV,
    OPENCODE_SDK_READ_TOOLS,
    OPENCODE_SDK_WRITE_TOOLS,
    PASS,
    SESSION_RETENTION_DAYS,
    ClaudeSdkRoots,
    OpenCodeSdkRoots,
    canonical_json,
    claude_sdk_auth_problems,
    claude_sdk_policy,
    exact_input,
    exact_text,
    foreign_name,
    input_digest,
    interaction_settings,
    opencode_sdk_auth_problems,
    opencode_sdk_policy,
    permission_summary,
    question_options,
    root_inside,
    sdk_launch_allowlist,
    sdk_root,
    server_password,
    shown,
)
from tests.conftest import git_repo

# Synthetic credentials, one per route an SDK launch must not inherit.
FOREIGN_SECRETS = {
    "OPENAI_API_KEY": "sk-proj-autoforgeSyntheticOpenAi0001",
    "GEMINI_API_KEY": "autoforge-synthetic-gemini-0002",
    "GOOGLE_APPLICATION_CREDENTIALS": "/nonexistent/autoforge-synthetic-adc.json",
    "AWS_SECRET_ACCESS_KEY": "autoforge-synthetic-aws-0003",
    "AWS_PROFILE": "autoforge-synthetic-profile",
    "AZURE_OPENAI_API_KEY": "autoforge-synthetic-azure-0004",
    "OPENROUTER_API_KEY": "autoforge-synthetic-openrouter-0005",
    "CLAUDE_CODE_OAUTH_TOKEN": "autoforge-synthetic-oauth-0006",
    "ANTHROPIC_AUTH_TOKEN": "autoforge-synthetic-bearer-0007",
    "OPENCODE_SERVER_PASSWORD": "autoforge-synthetic-password-0008",
    "OPENCODE_API_KEY": "autoforge-synthetic-opencode-0009",
    "PI_CODING_AGENT_DIR": "/nonexistent/autoforge-synthetic-pi",
    "MY_SERVICE_TOKEN": "autoforge-synthetic-token-0010",
    "DEPLOY_SECRET": "autoforge-synthetic-secret-0011",
}
ANTHROPIC_KEY = "sk-ant-api03-autoforgeSyntheticAnthropic0012"


def _claude_profile(name: str = "analyze_execute", **options: str) -> ProfileConfig:
    return ProfileConfig(
        name=name,
        provider="claude-sdk",
        model="fable",
        effort="high",
        command="/opt/claude-sdk/bin/python",
        options=dict(options),
    )


def _opencode_profile(name: str = "review_round_1", **options: str) -> ProfileConfig:
    opts = {"server_command": "opencode", "credential_env": "OPENAI_API_KEY"}
    opts.update(options)
    return ProfileConfig(
        name=name,
        provider="opencode-sdk",
        model="openai/gpt-5.6-luna",
        effort="high",
        command="node",
        options=opts,
    )


# -- interaction policy and limits (ADR 0007 D7.1, D7.3) ----------------------------
def test_interaction_defaults_to_fail_with_the_adr_limits():
    settings = interaction_settings({}, "p")
    assert (settings.policy, settings.max_wait_seconds, settings.max_interactions) == (
        "fail",
        120,
        8,
    )
    assert not settings.live
    assert interaction_settings({"interaction_policy": "prompt"}, "p").live


def test_pause_is_refused_until_its_record_and_gates_land():
    with pytest.raises(ConfigurationError, match=r"pause is not available.*#126"):
        interaction_settings({"interaction_policy": "pause"}, "profile 'fix'")


@pytest.mark.parametrize(
    "key, raw",
    [
        ("interaction_policy", "ask"),
        ("interaction_policy", "Prompt"),
        ("max_wait_seconds", "0"),
        ("max_wait_seconds", "-5"),
        ("max_wait_seconds", "1.5"),
        ("max_wait_seconds", " 30"),
        ("max_wait_seconds", "030"),
        ("max_wait_seconds", "86401"),
        ("max_interactions", "65"),
        ("max_interactions", "true"),
    ],
)
def test_interaction_values_outside_their_closed_sets_are_refused(key, raw):
    with pytest.raises(ConfigurationError, match=key):
        interaction_settings({key: raw}, "p")


# -- the Claude policy (docs/sdk-policy.md §2-6) -------------------------------------
def test_claude_inventory_follows_the_profile_and_nothing_else_is_offered():
    write = claude_sdk_policy("analyze_execute", {})
    read = claude_sdk_policy("review_round_1", {})
    assert write.tools == CLAUDE_SDK_WRITE_TOOLS
    assert read.tools == CLAUDE_SDK_READ_TOOLS
    for policy in (write, read):
        assert policy.allowed_tools == policy.tools and policy.ask_tools == ()
        assert set(CLAUDE_SDK_DENIED_TOOLS) <= set(policy.disallowed_tools)
        assert CLAUDE_SDK_QUESTION_TOOL in policy.disallowed_tools
        assert not set(policy.tools) & set(policy.disallowed_tools)
        assert set(policy.tools) | set(policy.disallowed_tools) >= set(CLAUDE_SDK_TOOLS)


def test_claude_prompt_policy_offers_the_question_tool_and_asks_for_it():
    policy = claude_sdk_policy(
        "fix",
        {
            "interaction_policy": "prompt",
            "ask_tools": "Bash,WebFetch",
            "tools": "Read,Bash,WebFetch",
        },
    )
    assert policy.tools == ("Read", "Bash", "WebFetch", CLAUDE_SDK_QUESTION_TOOL)
    assert policy.ask_tools == ("Bash", "WebFetch", CLAUDE_SDK_QUESTION_TOOL)
    assert policy.allowed_tools == ("Read",)
    assert policy.disposition("Bash") == ASK
    assert policy.disposition(CLAUDE_SDK_QUESTION_TOOL) == ASK
    assert policy.disposition("Read") == PASS


@pytest.mark.parametrize(
    "tool", ["Edit", "Agent", "Task", "Skill", "mcp__github__create_issue", "NewBuiltin", ""]
)
def test_claude_hook_denies_every_call_the_policy_did_not_list(tool):
    policy = claude_sdk_policy("review_round_1", {})
    assert policy.disposition(tool) == DENY


@pytest.mark.parametrize(
    "options, match",
    [
        ({"tools": "Read, Bash"}, "no spaces"),
        ({"tools": ""}, "no spaces"),
        ({"tools": "Read,,Bash"}, "no spaces"),
        ({"tools": "Read,Agent"}, "unknown or unsupported tool"),
        ({"tools": "Read,mcp__x__y"}, "unknown or unsupported tool"),
        ({"tools": "Read,AskUserQuestion"}, "unknown or unsupported tool"),
        ({"tools": "Read,Read"}, "listed twice"),
        ({"ask_tools": "Bash"}, "needs interaction_policy prompt"),
        ({"interaction_policy": "prompt", "ask_tools": "Edit"}, "unknown or unsupported tool"),
        ({"permission_mode": "bypassPermissions"}, "unknown claude-sdk option"),
        ({"setting_sources": "user"}, "unknown claude-sdk option"),
        ({"settings": "{}"}, "unknown claude-sdk option"),
        ({"mcp_servers": "{}"}, "unknown claude-sdk option"),
        ({"plugins": "x"}, "unknown claude-sdk option"),
        ({"extra_args": "--x"}, "unknown claude-sdk option"),
        ({"interaction_policy": "pause"}, "pause is not available"),
    ],
)
def test_claude_policy_fails_closed_on_any_other_option_or_value(options, match):
    with pytest.raises(ConfigurationError, match=match):
        claude_sdk_policy("review_round_1", options)


def test_claude_start_fields_close_every_escape_hatch():
    fields = claude_sdk_policy("fix", {}).start_fields()
    assert fields["setting_sources"] == []
    assert fields["strict_mcp_config"] is True
    assert fields["mcp_servers"] == {} and fields["plugins"] == [] and fields["agents"] == {}
    assert fields["extra_args"] == {}
    assert fields["permission_mode"] == "default"
    assert fields["system_prompt"] == {"type": "preset", "preset": "claude_code"}
    assert fields["can_use_tool"] == DENY and fields["can_use_tool_message"] == CLAUDE_SDK_HOST_DENY
    assert json.loads(str(fields["settings"])) == CLAUDE_SDK_SETTINGS
    assert CLAUDE_SDK_SETTINGS == {
        "autoMemoryEnabled": False,
        "cleanupPeriodDays": SESSION_RETENTION_DAYS,
    }
    # Flat values only: the helper's envelope carries JSON, nothing callable.
    assert json.loads(json.dumps(fields)) == fields


def test_claude_digest_tracks_the_policy_not_the_roots(tmp_path):
    a = claude_sdk_policy("fix", {})
    assert a.digest() == claude_sdk_policy("fix", {}).digest()
    assert a.digest() != claude_sdk_policy("fix", {"tools": "Read,Bash"}).digest()
    assert a.digest() != claude_sdk_policy("fix", {"interaction_policy": "prompt"}).digest()
    assert re.fullmatch(r"[0-9a-f]{64}", a.digest())
    roots = ClaudeSdkRoots.under(tmp_path / "sandbox")
    env = a.environment(roots)
    assert env == {
        "CLAUDE_CONFIG_DIR": str(tmp_path / "sandbox" / "sdk" / "claude-sdk" / "config"),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    assert str(tmp_path) not in json.dumps(a.start_fields())


def _good_init(policy) -> dict:
    return {
        "type": "system",
        "subtype": "init",
        "tools": list(policy.tools),
        "mcp_servers": [],
        "plugins": [{"name": "telemetry", "path": "builtin"}],
        "permissionMode": "default",
        "apiKeySource": "ANTHROPIC_API_KEY",
    }


def test_claude_init_record_matching_the_policy_has_no_problem():
    policy = claude_sdk_policy("fix", {})
    assert policy.init_problems(_good_init(policy)) == []


@pytest.mark.parametrize(
    "change, match",
    [
        ({"tools": ["Read", "Glob", "Grep", "Edit", "Write", "Bash", "Agent"]}, "Agent"),
        ({"tools": ["Read"]}, "does not offer"),
        ({"tools": "Read"}, "not a list"),
        ({"mcp_servers": [{"name": "sentinel", "status": "connected"}]}, "MCP"),
        ({"plugins": [{"name": "sentinel", "path": "/home/x/.claude/plugins/s"}]}, "plugin"),
        ({"permissionMode": "bypassPermissions"}, "permission mode"),
        ({"apiKeySource": "none"}, "credential"),
        ({"apiKeySource": "/login managed key"}, "credential"),
        ({"memory_paths": {"auto": "/x"}}, "auto memory"),
    ],
)
def test_claude_init_record_departing_from_the_policy_is_named(change, match):
    policy = claude_sdk_policy("fix", {})
    init = {**_good_init(policy), **change}
    problems = policy.init_problems(init)
    assert problems and any(match in p for p in problems), problems
    assert "/home/x" not in " ".join(problems)


def test_claude_auth_is_an_api_key_and_problems_never_carry_a_value():
    assert claude_sdk_auth_problems({"ANTHROPIC_API_KEY": ANTHROPIC_KEY}) == []
    missing = claude_sdk_auth_problems({})
    assert len(missing) == 1 and "ANTHROPIC_API_KEY is not set" in missing[0]
    env = {
        "ANTHROPIC_API_KEY": ANTHROPIC_KEY,
        "CLAUDE_CODE_OAUTH_TOKEN": FOREIGN_SECRETS["CLAUDE_CODE_OAUTH_TOKEN"],
        "ANTHROPIC_AUTH_TOKEN": FOREIGN_SECRETS["ANTHROPIC_AUTH_TOKEN"],
        "CLAUDE_CODE_USE_BEDROCK": "1",
    }
    problems = claude_sdk_auth_problems(env)
    text = " ".join(problems)
    assert "CLAUDE_CODE_OAUTH_TOKEN" in text and "CLAUDE_CODE_USE_BEDROCK" in text
    assert all(value not in text for value in env.values() if len(value) > 1)


# -- the OpenCode policy (docs/sdk-policy.md §7-10) ----------------------------------
def test_opencode_ruleset_denies_everything_first_and_overlaps_nothing():
    policy = opencode_sdk_policy(
        "fix", {"server_command": "opencode", "credential_env": "OPENAI_API_KEY"}
    )
    rules = policy.ruleset()
    assert next(iter(rules.items())) == ("*", "deny")
    assert [k for k, v in rules.items() if v == "allow"] == [
        *OPENCODE_SDK_WRITE_TOOLS,
        "external_directory",
    ]
    assert all(rules[name] == "deny" for name in (*OPENCODE_SDK_DENIED, "question"))
    read = opencode_sdk_policy(
        "update_epic", {"server_command": "opencode", "credential_env": "OPENAI_API_KEY"}
    )
    assert read.tools == OPENCODE_SDK_READ_TOOLS and read.ruleset().get("edit") is None
    live = opencode_sdk_policy(
        "fix",
        {
            "server_command": "opencode",
            "credential_env": "OPENAI_API_KEY",
            "interaction_policy": "prompt",
        },
    )
    assert live.ruleset()["question"] == "allow"
    assert live.start_fields()["replies"] == ["once", "reject"]  # never "always"


def test_opencode_document_and_environment_load_nothing_of_the_operators(tmp_path):
    policy = opencode_sdk_policy(
        "fix", {"server_command": "opencode", "credential_env": "OPENAI_API_KEY"}
    )
    doc = policy.config_document()
    assert doc["plugin"] == [] and doc["mcp"] == {} and doc["instructions"] == []
    assert doc["lsp"] is False and doc["formatter"] is False
    assert doc["autoupdate"] is False and doc["share"] == "disabled"
    agent = doc["agent"][OPENCODE_SDK_AGENT]
    assert agent["mode"] == "primary" and agent["permission"] == policy.ruleset()
    roots = OpenCodeSdkRoots.under(tmp_path / "sandbox")
    env = policy.environment(roots)
    assert env["HOME"] == str(roots.home)
    assert env["XDG_CONFIG_HOME"] == str(roots.config)
    assert env["XDG_DATA_HOME"] == str(roots.data)
    assert env["OPENCODE_CONFIG_DIR"] == str(roots.empty_config)
    assert env["OPENCODE_CONFIG"] == ""
    assert json.loads(env["OPENCODE_CONFIG_CONTENT"]) == doc
    assert OPENCODE_SDK_FIXED_ENV.items() <= env.items()
    for name in ("OPENCODE_PERMISSION", OPENCODE_SDK_PASSWORD_ENV, "OPENCODE_SERVER_USERNAME"):
        assert name not in env
    assert policy.server_argv("/usr/bin/opencode") == [
        "/usr/bin/opencode",
        "serve",
        "--hostname=127.0.0.1",
        "--port=0",
    ]


@pytest.mark.parametrize(
    "options, match",
    [
        ({"credential_env": "OPENAI_API_KEY"}, "server_command"),
        ({"server_command": "open code", "credential_env": "OPENAI_API_KEY"}, "server_command"),
        ({"server_command": "opencode"}, "credential_env"),
        ({"server_command": "opencode", "credential_env": "openai_api_key"}, "credential_env"),
        ({"server_command": "opencode", "credential_env": "OPENAI_TOKEN"}, "credential_env"),
        (
            {"server_command": "opencode", "credential_env": "OPENCODE_SERVER_PASSWORD"},
            "credential_env",
        ),
        (
            {"server_command": "opencode", "credential_env": "OPENCODE_CONFIG_API_KEY"},
            "credential_env",
        ),
        (
            {"server_command": "opencode", "credential_env": "OPENAI_API_KEY", "ask_tools": "x"},
            "unknown opencode-sdk option",
        ),
        (
            {"server_command": "opencode", "credential_env": "OPENAI_API_KEY", "auto_approve": "1"},
            "unknown opencode-sdk option",
        ),
        (
            {
                "server_command": "opencode",
                "credential_env": "OPENAI_API_KEY",
                "tools": "read,task",
            },
            "unknown or unsupported tool",
        ),
        (
            {
                "server_command": "opencode",
                "credential_env": "OPENAI_API_KEY",
                "interaction_policy": "pause",
            },
            "pause is not available",
        ),
    ],
)
def test_opencode_policy_fails_closed_on_any_other_option_or_value(options, match):
    with pytest.raises(ConfigurationError, match=match):
        opencode_sdk_policy("fix", options)


def test_opencode_auth_names_the_variable_only():
    policy = opencode_sdk_policy(
        "fix", {"server_command": "opencode", "credential_env": "OPENAI_API_KEY"}
    )
    assert opencode_sdk_auth_problems(policy, {"OPENAI_API_KEY": "x"}) == []
    problems = opencode_sdk_auth_problems(policy, {"OPENCODE_API_KEY": "autoforge-synthetic"})
    assert len(problems) == 1 and "OPENAI_API_KEY" in problems[0]
    assert "autoforge-synthetic" not in problems[0]


def test_server_password_is_fresh_and_long():
    first, second = server_password(), server_password()
    assert first != second and len(first) >= 43


# -- the environment (ADR 0007 D10.1) ------------------------------------------------
def test_foreign_and_credential_shaped_names_are_withheld():
    for name in [*FOREIGN_SECRETS, "ANTHROPIC_API_KEY", "anthropic_api_key", "X_PASSWD"]:
        assert foreign_name(name), name
    for name in ("PATH", "HOME", "LANG", "LC_ALL", "XDG_CONFIG_HOME", "HTTPS_PROXY", "KEYBOARD"):
        assert not foreign_name(name), name


def test_launch_allowlist_expands_prefixes_and_adds_only_its_own_credential():
    environ = ["LC_ALL", "LC_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY"]
    base = ("PATH", "LC_*", "ANTHROPIC_*", "OPENAI_API_KEY", "ANTHROPIC_BASE_URL")
    names = sdk_launch_allowlist(
        base, ("ANTHROPIC_API_KEY",), environ, passthrough=("ANTHROPIC_BASE_URL",)
    )
    assert names == ("PATH", "LC_ALL", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY")
    assert all(not n.endswith("*") for n in names)
    # The passthrough needs the exact name: a prefix never carries it.
    assert "ANTHROPIC_BASE_URL" not in sdk_launch_allowlist(
        ("ANTHROPIC_*",), (), environ, passthrough=("ANTHROPIC_BASE_URL",)
    )


def test_launch_allowlist_still_refuses_a_forbidden_route():
    with pytest.raises(ConfigurationError, match="GIT_ASKPASS"):
        sdk_launch_allowlist(("PATH", "GIT_ASKPASS"), (), [])
    with pytest.raises(ConfigurationError, match="GH_"):
        sdk_launch_allowlist(("GH_*",), (), [])


_PRINT_ENV = "import json, os; print(json.dumps(sorted(os.environ)))"


def _launched_names(allowlist, overlay, tmp_path) -> list[str]:
    res = execute(
        ExecutionRequest(
            command=[sys.executable, "-I", "-c", _PRINT_ENV],
            cwd=str(tmp_path),
            timeout_seconds=60,
            env_allowlist=allowlist,
            env=overlay,
        )
    )
    assert res.exit_code == 0, res.stderr
    return json.loads(res.stdout)


@pytest.mark.parametrize("provider", ["claude-sdk", "opencode-sdk"])
def test_no_other_providers_secret_reaches_a_launched_child(provider, tmp_path, monkeypatch):
    for name, value in FOREIGN_SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    monkeypatch.setenv("LC_AUTOFORGE_PROBE", "1")
    profile = _claude_profile() if provider == "claude-sdk" else _opencode_profile()
    adapter = provider_for(profile)
    base = (*DEFAULT_ENV_ALLOWLIST, "ANTHROPIC_*", "OPENAI_*", "AWS_*", "MY_SERVICE_TOKEN")
    before = dict(os.environ)
    request = AgentRequest(
        phase="analyze_execute",
        prompt="x",
        cwd=str(tmp_path),
        profile=profile,
        idle_timeout_seconds=None,
        max_runtime_seconds=None,
        env_allowlist=base,
    )
    allowlist = adapter.environment_allowlist(request)
    assert allowlist is not None
    sandbox = tmp_path / "sandbox"
    if provider == "claude-sdk":
        overlay = claude_sdk_policy(profile.name, profile.options).environment(
            ClaudeSdkRoots.under(sandbox)
        )
        own = {"ANTHROPIC_API_KEY"}
    else:
        overlay = opencode_sdk_policy(profile.name, profile.options).environment(
            OpenCodeSdkRoots.under(sandbox)
        )
        own = {"OPENAI_API_KEY"}
    assert dict(os.environ) == before  # names read, nothing set or removed
    names = set(_launched_names(allowlist, overlay, tmp_path))
    assert "LC_AUTOFORGE_PROBE" in names  # a prefix still expands
    for name in [*FOREIGN_SECRETS, "ANTHROPIC_API_KEY"]:
        if name in own:
            assert name in names
        elif name in overlay:
            continue  # the policy's own value, never the controller's
        else:
            assert name not in names, name
    assert dict(os.environ) == before


# -- configuration and session roots (ADR 0007 D2.7) ---------------------------------
def test_roots_live_under_the_sandbox_directory_and_need_an_absolute_one(tmp_path):
    assert sdk_root(tmp_path, "claude-sdk") == tmp_path / "sdk" / "claude-sdk"
    with pytest.raises(ConfigurationError):
        sdk_root(Path("relative"), "claude-sdk")
    with pytest.raises(ConfigurationError):
        sdk_root(tmp_path, "claude")
    roots = OpenCodeSdkRoots.under(tmp_path)
    assert roots.empty_config not in roots.writable()
    assert len({*roots.writable(), roots.empty_config}) == 6


def _write_everywhere(paths) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
        (path / "session.jsonl").write_text('{"synthetic": true}\n', encoding="utf-8")


def test_remote_roots_are_outside_the_issue_worktree(tmp_path):
    repo = git_repo(tmp_path / "repo")
    worktree = tmp_path / "repo" / ".git" / "autoforge" / "worktrees" / "issue-149"
    sandbox = repo / ".git" / SANDBOX_DIR / "issue-149"
    for root in (
        *ClaudeSdkRoots.under(sandbox).writable(),
        *OpenCodeSdkRoots.under(sandbox).writable(),
    ):
        assert not root_inside(root, worktree)
        assert not root_inside(root, repo / "src")


# -- interaction data (docs/sdk-policy.md §11) ---------------------------------------
def test_canonical_json_is_stable_and_refuses_what_json_cannot_hold():
    assert canonical_json({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode()
    with pytest.raises(InteractionDataError):
        canonical_json({"x": float("nan")})
    with pytest.raises(InteractionDataError):
        canonical_json({"x": object()})


def test_a_lone_surrogate_follows_the_refusal_path_not_a_unicode_error():
    # Legal as a JSON escape, so a provider's ask can carry one; UTF-8 cannot.
    ask = json.loads('{"command": "echo \\ud800"}')
    assert ask == {"command": "echo \ud800"}
    with pytest.raises(InteractionDataError, match="lone surrogate"):
        canonical_json(ask)
    with pytest.raises(InteractionDataError, match="lone surrogate"):
        input_digest(ask, "tool input")
    with pytest.raises(InteractionDataError, match="lone surrogate"):
        exact_input(ask, "modified input")


def test_input_digest_binds_the_whole_input_and_refuses_an_oversized_one():
    a = input_digest({"command": "ls"}, "tool input")
    assert a == input_digest({"command": "ls"}, "tool input")
    assert a != input_digest({"command": "ls -a"}, "tool input")
    with pytest.raises(InteractionDataError, match="refused rather than shown in part"):
        input_digest({"content": "x" * MAX_INPUT_BYTES}, "tool input")


@pytest.mark.parametrize(
    "value, match",
    [
        ("Yes\x1b[2J", "control or escape"),
        ("Yes‮", "control or escape"),
        ("Yes\x00", "control or escape"),
        ("Yes\rNo", "control or escape"),
        ("Yes\ud800", "control or escape"),
        ("x" * 201, "longer than"),
        ("use GITHUB_TOKEN=ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "credential-shaped"),
        (3, "not a string"),
    ],
)
def test_exact_text_is_refused_never_rewritten(value, match):
    with pytest.raises(InteractionDataError, match=match) as info:
        exact_text(value, "option 1", 200)
    assert "ghp_" not in str(info.value)


def test_exact_text_keeps_a_safe_value_verbatim():
    value = "Use the second approach:\n\t- keep the file"
    assert exact_text(value, "answer", 2000) is value


def test_exact_input_refuses_a_modified_input_redaction_would_change():
    assert exact_input({"command": "pytest -q"}, "modified input") == {"command": "pytest -q"}
    with pytest.raises(InteractionDataError, match="credential-shaped"):
        exact_input({"command": "OPENAI_API_KEY=sk-aaaaaaaaaaaaaaaaaaaa run"}, "modified input")
    with pytest.raises(InteractionDataError, match="control or escape"):
        exact_input({"command": "echo \x1b]0;x\x07"}, "modified input")


@pytest.mark.parametrize(
    "value",
    [
        # Shown, the text after the carriage return redraws what came before it.
        {"command": "rm -rf ~/work\recho tidy"},
        {"content": "line one\r\nline two"},
        {"cmd\r": "ls"},
        {"edits": [{"new_string": "x\r"}]},
    ],
)
def test_exact_input_refuses_a_carriage_return_bare_or_in_crlf(value):
    with pytest.raises(InteractionDataError, match="control or escape"):
        exact_input(value, "modified input")


def test_question_options_keep_the_raw_identity_apart_from_what_is_shown():
    options = question_options(["Keep it", "Drop it"], "question 1")
    assert [(o.raw, o.shown) for o in options] == [("Keep it", "Keep it"), ("Drop it", "Drop it")]
    with pytest.raises(InteractionDataError, match="shown alike"):
        question_options(["Keep", "keep"], "question 1")
    with pytest.raises(InteractionDataError, match="more than"):
        question_options([f"o{i}" for i in range(MAX_OPTIONS + 1)], "question 1")
    with pytest.raises(InteractionDataError, match="no list"):
        question_options("Keep", "question 1")


def test_permission_summary_shows_only_allow_listed_keys():
    summary = permission_summary(
        "Write",
        {"file_path": "src/x.py", "content": "SECRET_BODY " + "x" * 5000},
    )
    assert "src/x.py" in summary and "SECRET_BODY" not in summary
    assert "(+1 field(s) not shown)" in summary
    bash = permission_summary(
        "Bash", {"command": "curl -H 'Authorization: Bearer abcdefghijklmnop' x\x1b[2J"}
    )
    assert "abcdefghijklmnop" not in bash and "\x1b" not in bash
    assert len(permission_summary("Unknown", {"a": "b"})) < 40


def test_shown_text_is_bounded_and_redaction_growth_holds():
    text = "HF_TOKEN=x " * 400
    out = shown(text, 160)
    assert len(out) <= 160 and "\n" not in out
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "OPENCODE_SERVER_PASSWORD"):
        sample = f"{name}=x"
        assert redact(sample) == f"{name}=***REDACTED***"
        assert len(redact(sample)) <= MAX_GROWTH_FACTOR * len(sample)


# -- the adapters and config (ADR 0007 D6.3) -----------------------------------------
def test_sdk_providers_are_registered_and_validate_through_config():
    validate_profile(_claude_profile())
    validate_profile(_opencode_profile())
    assert isinstance(provider_for(_claude_profile()), ClaudeSdkProvider)
    assert isinstance(provider_for(_opencode_profile()), OpenCodeSdkProvider)


@pytest.mark.parametrize(
    "change, match",
    [
        ({"command": ""}, "needs 'command'"),
        ({"command": "python -I"}, "needs 'command'"),
        ({"extra_args": ["--settings", '{"env":{}}']}, "takes no extra_args"),
        ({"effort": "extreme"}, "effort"),
        ({"model": "fable #high"}, "model"),
        ({"options": {"permission_mode": "bypassPermissions"}}, "unknown claude-sdk option"),
    ],
)
def test_claude_sdk_profile_refusals(change, match):
    import dataclasses

    profile = dataclasses.replace(_claude_profile(), **change)
    with pytest.raises(ConfigurationError, match=match) as info:
        validate_profile(profile)
    assert '{"env"' not in str(info.value)


@pytest.mark.parametrize(
    "change, match",
    [
        ({"model": "gpt-5.6"}, "provider>/<model"),
        ({"model": "openai/gpt-5.6#high"}, "provider>/<model"),
        ({"effort": "High Effort"}, "effort"),
        ({"extra_args": ["--print-logs"]}, "takes no extra_args"),
    ],
)
def test_opencode_sdk_profile_refusals(change, match):
    import dataclasses

    profile = dataclasses.replace(_opencode_profile(), **change)
    with pytest.raises(ConfigurationError, match=match):
        validate_profile(profile)


@pytest.mark.parametrize("profile", [_claude_profile(), _opencode_profile()])
def test_sdk_providers_refuse_every_launch_until_their_helper_lands(profile, tmp_path):
    adapter = provider_for(profile)
    for call in (
        lambda: adapter.integration_preflight(("PATH",)),
        lambda: adapter.build_command_for(profile, "x"),
        lambda: adapter.execute(
            AgentRequest(
                phase="fix",
                prompt="x",
                cwd=str(tmp_path),
                profile=profile,
                idle_timeout_seconds=None,
                max_runtime_seconds=None,
            )
        ),
    ):
        with pytest.raises(ConfigurationError, match="cannot launch in this release"):
            call()


@pytest.mark.parametrize("profile", [_claude_profile(), _opencode_profile()])
def test_doctor_reports_that_an_sdk_profile_cannot_launch_yet(profile, tmp_path):
    from autoforge.doctor import Doctor

    row = Doctor(cwd=str(tmp_path)).check_sdk_launch(profile.provider, [profile])
    assert not row.ok and row.required
    assert "cannot launch in this release" in row.detail


def test_claude_sdk_declares_an_api_key_and_never_a_login_file(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
    config_dir = tmp_path / "sandbox" / "sdk" / "claude-sdk" / "config"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "ANTHROPIC_API_KEY": ANTHROPIC_KEY,
        "CLAUDE_CONFIG_DIR": str(config_dir),
    }
    needs = ClaudeSdkProvider().sandbox_requirements(
        _claude_profile(), [sys.executable], env, str(home)
    )
    modes = {
        (item.mode, item.label if item.mode is AuthMode.ENV else item.source) for item in needs.auth
    }
    assert (AuthMode.ENV, "ANTHROPIC_API_KEY") in modes
    assert (AuthMode.DIR_RW, str(config_dir)) in modes
    assert all(item.mode not in (AuthMode.FILE_RO, AuthMode.KEYRING) for item in needs.auth)
    assert str(home / ".claude") in needs.operator_config_roots


def test_opencode_sdk_declares_its_credential_variable_only(tmp_path):
    env = {"PATH": os.environ.get("PATH", ""), "OPENAI_API_KEY": "x", "GEMINI_API_KEY": "y"}
    needs = OpenCodeSdkProvider().sandbox_requirements(
        _opencode_profile(), [sys.executable], env, str(tmp_path)
    )
    assert [(i.mode, i.label) for i in needs.auth] == [(AuthMode.ENV, "OPENAI_API_KEY")]


def _cfg(profile: ProfileConfig, idle: int | None, ceiling: int | None) -> AutoForgeConfig:
    execution = ExecutionConfig(
        default_idle_timeout_seconds=idle, default_max_runtime_seconds=ceiling
    )
    return AutoForgeConfig(execution=execution, profiles={profile.name: profile})


def test_a_live_wait_must_end_before_either_limit():
    live = _claude_profile(interaction_policy="prompt", max_wait_seconds="300")
    with pytest.raises(ConfigurationError, match=r"max_wait_seconds \(300\).*idle timeout"):
        _cfg(live, 300, 3600).agent_limits(live)
    with pytest.raises(ConfigurationError, match=r"max_wait_seconds \(300\).*max_runtime"):
        _cfg(live, 3600, 200).agent_limits(live)
    _cfg(live, 301, 3600).agent_limits(live)
    # Under fail nothing waits, so nothing is compared.
    fail = _claude_profile(max_wait_seconds="300")
    _cfg(fail, 60, 60).agent_limits(fail)


# -- opt-in probes against installed runtimes ----------------------------------------
_SENTINEL = "AUTOFORGE_SENTINEL_149"


class _StandIn(BaseHTTPRequestHandler):
    """A loopback stand-in for the Messages API: records each body, answers ``ok``."""

    bodies: list[str] = []

    def log_message(self, *args) -> None:
        pass

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.end_headers()

    def do_GET(self) -> None:
        self._json({})

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        type(self).bodies.append(body.decode("utf-8", "replace"))
        try:
            request = json.loads(body)
        except ValueError:
            request = {}
        if "count_tokens" in self.path:
            self._json({"input_tokens": 1})
            return
        message = {
            "id": "msg_probe",
            "type": "message",
            "role": "assistant",
            "model": request.get("model", "m"),
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        if not request.get("stream"):
            self._json(message)
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        events = [
            ("message_start", {"message": {**message, "content": [], "stop_reason": None}}),
            ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "ok"}}),
            ("content_block_stop", {"index": 0}),
            (
                "message_delta",
                {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
            ),
            ("message_stop", {}),
        ]
        for name, data in events:
            payload = json.dumps({"type": name, **data})
            self.wfile.write(f"event: {name}\ndata: {payload}\n\n".encode())

    def _json(self, obj: object) -> None:
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _sentinel_project(tmp_path: Path, markers: Path) -> Path:
    """A repository below a parent directory, each carrying every kind of
    configuration a runtime could load, each of which leaves a marker file or
    a sentinel string behind when it is loaded."""
    parent = tmp_path / "parent"
    project = git_repo(parent / "project")
    hook = {"hooks": [{"type": "command", "command": f"touch {markers}/hook"}]}
    settings = {"hooks": {"SessionStart": [hook], "UserPromptSubmit": [hook]}}
    (project / ".claude" / "skills" / "sentinel").mkdir(parents=True)
    (project / ".claude" / "agents").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    (project / ".claude" / "settings.local.json").write_text(json.dumps(settings), encoding="utf-8")
    (project / ".claude" / "skills" / "sentinel" / "SKILL.md").write_text(
        f"---\nname: sentinel-skill\ndescription: {_SENTINEL} skill\n---\nbody\n", encoding="utf-8"
    )
    (project / ".claude" / "agents" / "sentinel.md").write_text(
        f"---\nname: sentinel-agent\ndescription: {_SENTINEL} agent\n---\nbody\n", encoding="utf-8"
    )
    mcp = {"mcpServers": {"sentinel": {"command": "sh", "args": ["-c", f"touch {markers}/mcp"]}}}
    (project / ".mcp.json").write_text(json.dumps(mcp), encoding="utf-8")
    (project / "CLAUDE.md").write_text(f"{_SENTINEL} project CLAUDE.md\n", encoding="utf-8")
    (project / "AGENTS.md").write_text(f"{_SENTINEL} project AGENTS.md\n", encoding="utf-8")
    (parent / "CLAUDE.md").write_text(f"{_SENTINEL} parent CLAUDE.md\n", encoding="utf-8")
    oc = {
        "model": "sentinel/project-model",
        "mcp": {"sentinel": {"type": "local", "command": ["sh", "-c", f"touch {markers}/ocmcp"]}},
        "instructions": ["AGENTS.md"],
    }
    (project / "opencode.json").write_text(json.dumps(oc), encoding="utf-8")
    (parent / "opencode.json").write_text(
        json.dumps({"model": "sentinel/parent"}), encoding="utf-8"
    )
    return project


def _claude_cli_flags(fields: dict) -> list[str]:
    """The flags the Claude Agent SDK passes for the policy's options (ADR 0007
    SC :729-730 and its neighbours), so the probe runs what the helper will."""
    return [
        "--setting-sources=",
        "--strict-mcp-config",
        "--tools",
        ",".join(fields["tools"]),
        "--allowedTools",
        ",".join(fields["allowed_tools"]),
        "--disallowedTools",
        ",".join(fields["disallowed_tools"]),
        "--permission-mode",
        fields["permission_mode"],
        "--settings",
        fields["settings"],
    ]


def _claude_probe(tmp_path: Path, flags: list[str], policy) -> tuple[dict, list[str], list[str]]:
    """Run the installed CLI once in a sentinel project with ``flags``; its
    ``init`` record, the markers left behind and the request bodies sent."""
    cli = os.environ["AUTOFORGE_CLAUDE_SDK_PROBE_CLI"]
    markers = tmp_path / "markers"
    markers.mkdir()
    project = _sentinel_project(tmp_path, markers)
    roots = ClaudeSdkRoots.under(tmp_path / "sandbox")
    roots.config.mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    user_hook = {"hooks": [{"type": "command", "command": f"touch {markers}/user-hook"}]}
    user_settings = {"hooks": {"SessionStart": [user_hook]}}
    (roots.config / "settings.json").write_text(json.dumps(user_settings), encoding="utf-8")
    (roots.config / "CLAUDE.md").write_text(f"{_SENTINEL} user CLAUDE.md\n", encoding="utf-8")
    _StandIn.bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StandIn)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(home),
            "ANTHROPIC_API_KEY": ANTHROPIC_KEY,
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_address[1]}",
            **policy.environment(roots),
        }
        argv = [cli, "-p", "--output-format", "stream-json", "--verbose", "--model", "fable"]
        res = subprocess.run(
            [*argv, *flags, "--", "Reply with ok."],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)
    assert ANTHROPIC_KEY not in res.stdout + res.stderr
    assert list((roots.config / "projects").rglob("*.jsonl"))  # the transcript is in the root
    records = [json.loads(line) for line in res.stdout.splitlines() if line.startswith("{")]
    init = next(r for r in records if r.get("type") == "system" and r.get("subtype") == "init")
    return init, sorted(os.listdir(markers)), list(_StandIn.bodies)


_CLAUDE_PROBE = pytest.mark.skipif(
    not os.environ.get("AUTOFORGE_CLAUDE_SDK_PROBE_CLI"),
    reason="opt-in: set AUTOFORGE_CLAUDE_SDK_PROBE_CLI to an installed claude executable",
)


@_CLAUDE_PROBE
def test_probe_installed_claude_loads_nothing_under_the_policy(tmp_path):
    policy = claude_sdk_policy("fix", {})
    init, markers, bodies = _claude_probe(
        tmp_path, _claude_cli_flags(policy.start_fields()), policy
    )
    assert policy.init_problems(init) == []
    assert not [n for n in init.get("skills", []) if "sentinel" in str(n)]
    assert not [n for n in init.get("agents", []) if "sentinel" in str(n)]
    assert markers == []
    assert bodies
    assert all(_SENTINEL not in body for body in bodies)


@_CLAUDE_PROBE
def test_probe_installed_claude_control_run_loads_the_sentinels(tmp_path):
    """The same project without the policy's flags: the probe can see a load."""
    policy = claude_sdk_policy("fix", {})
    init, markers, bodies = _claude_probe(tmp_path, [], policy)
    assert "hook" in markers and "mcp" in markers
    assert any(_SENTINEL in body for body in bodies)
    assert policy.init_problems(init)


def _opencode_probe(tmp_path: Path, isolated: bool) -> tuple[str, list[str], bool]:
    """Start the installed server in a sentinel project, under the policy's
    environment or (``isolated`` false) without its project switches; the
    configuration it reports, the markers left behind, and whether every
    write stayed in the policy's roots. Asserts an unauthenticated request is
    refused."""
    server_command = os.environ["AUTOFORGE_OPENCODE_SDK_PROBE_SERVER"]
    markers = tmp_path / "markers"
    markers.mkdir()
    project = _sentinel_project(tmp_path, markers)
    policy = opencode_sdk_policy(
        "fix", {"server_command": server_command, "credential_env": "OPENAI_API_KEY"}
    )
    roots = OpenCodeSdkRoots.under(tmp_path / "sandbox")
    for path in (*roots.writable(), roots.empty_config):
        path.mkdir(parents=True)
    sdk_base = roots.home.parent

    def outside_roots() -> set[Path]:
        return {p for p in tmp_path.rglob("*") if sdk_base not in p.parents}

    before = outside_roots()
    password = server_password()
    env = {
        "PATH": "/usr/bin:/bin",
        "OPENAI_API_KEY": FOREIGN_SECRETS["OPENAI_API_KEY"],
        **policy.environment(roots),
        OPENCODE_SDK_PASSWORD_ENV: password,
    }
    if not isolated:
        for name in ("OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_CONFIG_PROJECT_DISABLE"):
            del env[name]
    process = subprocess.Popen(
        policy.server_argv(server_command),
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert process.stdout is not None
        line = process.stdout.readline()
        found = re.search(r"http://127\.0\.0\.1:[0-9]+", line)
        assert found, line
        url = found.group(0)
        auth = "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()
        config, config_route = "", ""
        for route in ("/config", "/api/config"):
            request = urllib.request.Request(
                url + route, headers={"Authorization": auth, "x-opencode-directory": str(project)}
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    body = response.read().decode()
            except urllib.error.HTTPError:
                continue
            # 1.x answers /config with the merged document; 2.x serves its
            # web app there and lists the configuration sources at /api/config.
            if body.lstrip().startswith(("{", "[")):
                config, config_route = body, route
                break
        assert config
        with pytest.raises(urllib.error.HTTPError) as unauthenticated:
            urllib.request.urlopen(url + config_route, timeout=30)
        assert unauthenticated.value.code == 401
    finally:
        process.terminate()
        try:
            process.wait(15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(15)
    return config, sorted(os.listdir(markers)), outside_roots() == before


_OPENCODE_PROBE = pytest.mark.skipif(
    not os.environ.get("AUTOFORGE_OPENCODE_SDK_PROBE_SERVER"),
    reason="opt-in: set AUTOFORGE_OPENCODE_SDK_PROBE_SERVER to an installed opencode executable",
)


@_OPENCODE_PROBE
def test_probe_installed_opencode_server_loads_nothing_under_the_policy(tmp_path):
    config, markers, contained = _opencode_probe(tmp_path, isolated=True)
    assert "sentinel" not in config and _SENTINEL not in config
    assert markers == []
    assert contained  # every write stayed in the policy's roots


@_OPENCODE_PROBE
def test_probe_installed_opencode_control_run_loads_the_sentinels(tmp_path):
    """The same project with project configuration allowed: the probe can see a load."""
    config, _markers, _contained = _opencode_probe(tmp_path, isolated=False)
    assert "sentinel" in config
