"""Each provider's model credential under the container backend (#290, ADR 0013 D14).

With fake credentials and the real adapters' declarations, and no runtime:
each provider's login maps to exactly one of #270's mechanisms, and on a
copy-in runtime to the environment variable; the value is on no argv and in
no fact, description, refusal or helper container; a login that rotates its
refresh token on use, or that the CLI rewrites in place, is never projected
and the host's copy is left as it was; a relaunch gets a new projection; the
model credential reaches an agent launch and never a repository command.
"""

from __future__ import annotations

import json
import os
import pickle
from collections.abc import Mapping
from pathlib import Path

import pytest

from autoforge.config import ExecutionConfig, ProfileConfig
from autoforge.container_auth import (
    CredentialKind,
    CredentialPlan,
    CredentialRoutes,
    EnvRoute,
    Mechanism,
    Refresh,
    credential_looking,
    plan_credentials,
)
from autoforge.errors import SandboxPolicyError
from autoforge.executor import select_environment
from autoforge.providers import (
    CLAUDE_CREDENTIAL_ROUTES,
    CLAUDE_UNSUPPORTED_ROUTES,
    OPENCODE_KEY_NAMES,
    PI_KEY_NAMES,
    AgentProvider,
    ClaudeCodeProvider,
    ClaudeSdkProvider,
    OpenCodeProvider,
    OpenCodeSdkProvider,
    PiProvider,
    ScriptedProvider,
)
from autoforge.redaction import redact
from autoforge.sandbox_container import (
    ContainerNetwork,
    ContainerRole,
    ContainerSpec,
    EnvCredential,
    HostContext,
    Projection,
    RuntimeProduct,
    Secret,
    StagingMethod,
    check_projection,
    client_environment,
    create_argv,
)
from autoforge.sandbox_policy import LaunchClass, ProviderRequirements

SENTINEL = "fake-model-key-290-0b7d51"
SENTINEL_OTHER = "fake-model-key-290-other-9e24c3"
SENTINEL_ROTATED = "fake-model-key-290-rotated-41aa07"
SENTINEL_LOGIN = "fake-login-290-stored-c63f12"
IMAGE = "sha256:" + "b" * 64
AGENT = LaunchClass.REMOTE_AGENT
NO_NEEDS = ProviderRequirements()


def _profile(provider: str, model: str, **options: str) -> ProfileConfig:
    return ProfileConfig(
        name="analyze_execute", provider=provider, model=model, effort="high", options=options
    )


# Every supported route: the adapter, its profile, and the variable it projects.
ROUTES = [
    pytest.param(
        ClaudeCodeProvider(), _profile("claude", "fable"), "ANTHROPIC_API_KEY", id="claude-key"
    ),
    pytest.param(
        ClaudeCodeProvider(),
        _profile("claude", "fable"),
        "CLAUDE_CODE_OAUTH_TOKEN",
        id="claude-setup-token",
    ),
    *(
        pytest.param(
            OpenCodeProvider(), _profile("opencode", f"{scope}/m"), name, id=f"opencode-{scope}"
        )
        for scope, name in sorted(OPENCODE_KEY_NAMES.items())
    ),
    *(
        pytest.param(
            PiProvider(),
            _profile("pi", f"{scope}/m", require_oauth="false"),
            name,
            id=f"pi-{scope}",
        )
        for scope, name in sorted(PI_KEY_NAMES.items())
    ),
]


def _plan(
    provider: AgentProvider,
    profile: ProfileConfig,
    env: Mapping[str, str],
    needs: ProviderRequirements = NO_NEEDS,
    launch_class: LaunchClass = AGENT,
) -> CredentialPlan:
    return plan_credentials(
        provider.name, provider.credential_routes(profile), needs, env, launch_class=launch_class
    )


def _refusal(*args, **kwargs) -> SandboxPolicyError:
    with pytest.raises(SandboxPolicyError) as refused:
        _plan(*args, **kwargs)
    for sentinel in (SENTINEL, SENTINEL_OTHER, SENTINEL_ROTATED, SENTINEL_LOGIN):
        assert sentinel not in str(refused.value)
    assert refused.value.rule == "D14 provider credential mapping (#290)"
    return refused.value


@pytest.fixture
def host(tmp_path: Path) -> HostContext:
    home = tmp_path / "home"
    home.mkdir()
    common = tmp_path / "repo" / ".git"
    common.mkdir(parents=True)
    return HostContext(str(home), {}, os.getuid(), str(common), (str(common.parent),), ())


def _spec(plan: CredentialPlan, staging: StagingMethod, role=ContainerRole.LAUNCH):
    return ContainerSpec(
        role, ContainerNetwork.NONE, "af-vol-290", staging=staging, projection=plan.projection
    )


# -- each login maps to exactly one mechanism -----------------------------------------


@pytest.mark.parametrize(("provider", "profile", "name"), ROUTES)
@pytest.mark.parametrize("staging", list(StagingMethod))
def test_each_route_maps_to_the_environment_variable_on_either_runtime(
    provider, profile, name, staging, host
):
    """ADR 0013 §7: exactly one of #270's mechanisms, and on a copy-in runtime the
    variable: every supported route is a static value carried by one variable, which
    the create names alone and the runtime client values."""
    plan = _plan(provider, profile, {name: SENTINEL, "PATH": "/usr/bin"})
    assert (plan.provider, plan.name, plan.mechanism) == (provider.name, name, Mechanism.ENV)
    assert plan.refresh is Refresh.NONE
    assert plan.kind in (CredentialKind.API_KEY, CredentialKind.OAUTH_TOKEN)
    assert [env.name for env in plan.projection.env] == [name]
    assert plan.projection.files == ()
    check_projection(plan.projection, role=ContainerRole.LAUNCH, staging=staging, host=host)
    argv = create_argv(
        RuntimeProduct.PODMAN,
        _spec(plan, staging),
        host,
        name="af-launch-290",
        labels={"io.autoforge.run": "run-290"},
        image=IMAGE,
        command=["/opt/autoforge/watcher"],
    )
    assert f"--env={name}" in argv
    assert not any(SENTINEL in word for word in argv)
    assert client_environment({"PATH": "/usr/bin"}, plan.projection)[name] == SENTINEL


@pytest.mark.parametrize(
    "name",
    sorted(
        {r.name for r in CLAUDE_CREDENTIAL_ROUTES}
        | set(OPENCODE_KEY_NAMES.values())
        | set(PI_KEY_NAMES.values())
    ),
)
def test_no_declared_route_names_a_variable_the_projection_refuses(name, host):
    """A route's variable is never one the runtime client, a shell or the loader
    reads, nor a GitHub or git credential (#270's refusals), on either runtime."""
    projection = Projection(env=(EnvCredential(name, Secret(SENTINEL)),))
    for staging in StagingMethod:
        check_projection(projection, role=ContainerRole.LAUNCH, staging=staging, host=host)


# -- the value is nowhere but the projection ------------------------------------------


@pytest.mark.parametrize(("provider", "profile", "name"), ROUTES)
def test_the_value_is_in_no_fact_description_or_serialized_plan(provider, profile, name):
    plan = _plan(provider, profile, {name: SENTINEL})
    facts = plan.facts()
    assert facts["credential"] == name and facts["mechanism"] == Mechanism.ENV.value
    for text in (json.dumps(facts), plan.describe(), repr(plan), str(plan)):
        assert SENTINEL not in text
    assert name in plan.describe()
    with pytest.raises(TypeError):
        pickle.dumps(plan)


@pytest.mark.parametrize("role", [ContainerRole.STAGER, ContainerRole.EXPORTER])
@pytest.mark.parametrize(("provider", "profile", "name"), ROUTES)
def test_no_helper_container_gets_the_projection(provider, profile, name, role, host):
    plan = _plan(provider, profile, {name: SENTINEL})
    with pytest.raises(SandboxPolicyError) as refused:
        check_projection(plan.projection, role=role, staging=StagingMethod.BIND, host=host)
    assert SENTINEL not in str(refused.value)


def test_only_the_selected_variable_is_projected_and_the_others_are_withheld():
    """Scoped, never wholesale: an OpenCode launch for an OpenAI model carries the
    OpenAI key alone; another provider's key and every credential-looking variable is
    withheld, and an ordinary setting is not."""
    env = {
        "OPENAI_API_KEY": SENTINEL,
        "ANTHROPIC_API_KEY": SENTINEL_OTHER,
        "GEMINI_API_KEY": SENTINEL_OTHER,
        "OPENCODE_SERVER_PASSWORD": SENTINEL_OTHER,
        "OPENCODE_CONFIG_DIR": "/tmp/autoforge-opencode-1",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "8000",
        "EMPTY_API_KEY": "",
    }
    plan = _plan(OpenCodeProvider(), _profile("opencode", "openai/gpt-5.6-luna"), env)
    assert [item.name for item in plan.projection.env] == ["OPENAI_API_KEY"]
    assert plan.withheld == ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENCODE_SERVER_PASSWORD")
    assert SENTINEL_OTHER not in json.dumps(plan.facts())


@pytest.mark.parametrize(
    ("name", "looks"),
    [
        ("DEEPSEEK_API_KEY", True),
        ("ANTHROPIC_AUTH_TOKEN", True),
        ("MY_SECRET", True),
        ("CLAUDE_CODE_MAX_OUTPUT_TOKENS", False),
        ("OPENCODE_CONFIG_DIR", False),
        ("PATH", False),
    ],
)
def test_what_looks_like_a_credential(name, looks):
    assert credential_looking(name) is looks


# -- one credential, chosen by the operator -------------------------------------------


def test_two_claude_routes_at_once_are_refused_rather_than_left_to_precedence():
    """No implicit subscription to paid API-key fallback: with both, Claude Code would
    choose by its own precedence, so the launch is refused, naming both."""
    refused = _refusal(
        ClaudeCodeProvider(),
        _profile("claude", "fable"),
        {"ANTHROPIC_API_KEY": SENTINEL, "CLAUDE_CODE_OAUTH_TOKEN": SENTINEL_OTHER},
    )
    assert "ANTHROPIC_API_KEY" in str(refused) and "CLAUDE_CODE_OAUTH_TOKEN" in str(refused)


@pytest.mark.parametrize(("provider", "profile", "name"), ROUTES)
def test_an_absent_credential_is_refused_naming_what_to_set(provider, profile, name):
    refused = _refusal(provider, profile, {"PATH": "/usr/bin", name: ""})
    assert name in str(refused)
    assert "agent_env_allowlist_extra" in str(refused)


@pytest.mark.parametrize("route", CLAUDE_UNSUPPORTED_ROUTES, ids=lambda r: r.name)
def test_a_variable_that_selects_another_claude_route_is_refused(route):
    """A gateway token or a cloud switch would put Claude Code on a route ahead of
    both supported ones: refused, even beside a supported credential."""
    refused = _refusal(
        ClaudeCodeProvider(),
        _profile("claude", "fable"),
        {"CLAUDE_CODE_OAUTH_TOKEN": SENTINEL, route.name: SENTINEL_OTHER},
    )
    assert route.name in str(refused) and route.kind.value in str(refused)


@pytest.mark.parametrize(
    ("provider", "profile", "fragment"),
    [
        (OpenCodeProvider(), _profile("opencode", "groq/llama"), "'groq'"),
        (OpenCodeProvider(), _profile("opencode", "no-provider"), "''"),
        (PiProvider(), _profile("pi", "mistral/m", require_oauth="false"), "'mistral'"),
    ],
)
def test_an_unmapped_model_provider_is_refused(provider, profile, fragment):
    refused = _refusal(provider, profile, {"GROQ_API_KEY": SENTINEL, "OPENAI_API_KEY": SENTINEL})
    assert fragment in str(refused) and "never projected" in str(refused)


@pytest.mark.parametrize(
    "provider", [ClaudeSdkProvider(), OpenCodeSdkProvider(), ScriptedProvider()]
)
def test_a_provider_without_a_mapping_is_refused(provider):
    """``claude-sdk`` and ``opencode-sdk`` are not broadened to these routes (ADR 0007)."""
    profile = _profile(provider.name, "fable")
    assert provider.credential_routes(profile) is None
    refused = _refusal(provider, profile, {"ANTHROPIC_API_KEY": SENTINEL})
    assert "no credential mapping" in str(refused)


# -- refreshing logins are never projected --------------------------------------------


def test_a_rotating_refresh_token_is_refused_and_named():
    """ADR 0013 D14: a login that rotates its refresh token on use is never projected
    without a return path, and AutoForge has none."""
    refused = _refusal(
        ClaudeCodeProvider(),
        _profile("claude", "fable"),
        {"CLAUDE_CODE_OAUTH_REFRESH_TOKEN": SENTINEL_ROTATED},
    )
    assert "CLAUDE_CODE_OAUTH_REFRESH_TOKEN" in str(refused)
    assert "never projected" in str(refused)
    # Beside a supported token too: the rotating one would still reach the CLI.
    _refusal(
        ClaudeCodeProvider(),
        _profile("claude", "fable"),
        {"CLAUDE_CODE_OAUTH_TOKEN": SENTINEL, "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": SENTINEL_ROTATED},
    )


def test_any_route_declared_rotating_is_refused():
    """The rule is the mapping's, not one adapter's: a rotating route is refused."""
    routes = CredentialRoutes(
        "x", (EnvRoute("X_REFRESH", CredentialKind.OAUTH_REFRESH_TOKEN, Refresh.ROTATING),)
    )
    with pytest.raises(SandboxPolicyError) as refused:
        plan_credentials("fake", routes, NO_NEEDS, {"X_REFRESH": SENTINEL}, launch_class=AGENT)
    assert SENTINEL not in str(refused.value) and "rotates on use" in str(refused.value)


def test_pi_s_chatgpt_sign_in_is_refused():
    """``require_oauth`` (the default): the sign-in Pi refreshes in its agent directory
    has no return path, so a container launch is refused before any key is looked at."""
    for options in ({}, {"require_oauth": "true"}):
        refused = _refusal(
            PiProvider(), _profile("pi", "openai/gpt-5.6-terra", **options), {"OPENAI_API_KEY": ""}
        )
        assert "require_oauth" in str(refused) and "bwrap" in str(refused)


def _stored_logins(tmp_path: Path) -> list:
    """Each adapter's declaration with a stored login on a fake host: its file or
    directory holds a fake credential, which the plan must never read or project."""
    home = tmp_path / "home"
    claude = home / ".claude"
    claude.mkdir(parents=True)
    login = claude / ".credentials.json"
    login.write_text(SENTINEL_LOGIN)
    claude_dir = tmp_path / "claude-autoforge"
    opencode_dir = tmp_path / "opencode-autoforge"
    pi_dir = tmp_path / "pi-autoforge"
    for directory in (claude_dir, opencode_dir, pi_dir):
        directory.mkdir()
    (claude_dir / ".credentials.json").write_text(SENTINEL_LOGIN)
    (opencode_dir / "af.db").write_text(SENTINEL_LOGIN)
    (pi_dir / "auth.json").write_text(SENTINEL_LOGIN)
    return [
        (ClaudeCodeProvider(), _profile("claude", "fable"), {}, "ANTHROPIC_API_KEY", login),
        (
            ClaudeCodeProvider(),
            _profile("claude", "fable"),
            {"CLAUDE_CONFIG_DIR": str(claude_dir)},
            "CLAUDE_CODE_OAUTH_TOKEN",
            claude_dir / ".credentials.json",
        ),
        (
            OpenCodeProvider(),
            _profile("opencode", "openai/gpt-5.6-luna"),
            {"OPENCODE_DB": str(opencode_dir / "af.db")},
            "OPENAI_API_KEY",
            opencode_dir / "af.db",
        ),
        (
            PiProvider(),
            _profile("pi", "deepseek/deepseek-flash", require_oauth="false"),
            {"PI_CODING_AGENT_DIR": str(pi_dir)},
            "DEEPSEEK_API_KEY",
            pi_dir / "auth.json",
        ),
    ]


def test_a_stored_login_is_never_projected_and_the_host_copy_is_left_as_it_was(tmp_path):
    """Claude Code's login file and configuration directory, OpenCode's account store
    and Pi's agent directory: each is named as not projected, never read, and the host
    copy keeps its bytes and its time; with no variable beside it the launch is refused,
    naming why the store cannot be used."""
    home = str(tmp_path / "home")
    for provider, profile, env, name, stored in _stored_logins(tmp_path):
        before = (stored.read_bytes(), stored.stat().st_mtime_ns)
        needs = provider.sandbox_requirements(profile, [provider.name], env, home)
        plan = _plan(provider, profile, {**env, name: SENTINEL}, needs)
        assert [item.name for item in plan.projection.env] == [name]
        assert plan.projection.files == ()
        assert plan.not_projected, provider.name
        assert all(item.kind is CredentialKind.STORED_LOGIN for item in plan.not_projected)
        refused = _refusal(provider, profile, env, needs)
        assert "never projected" in str(refused) and "D7.2" in str(refused)
        assert (stored.read_bytes(), stored.stat().st_mtime_ns) == before


# -- a relaunch, and the launch classes -----------------------------------------------


@pytest.mark.parametrize(("provider", "profile", "name"), ROUTES)
def test_a_relaunch_gets_a_new_projection_from_the_host_as_it_is_then(provider, profile, name):
    """The operator rotated the key on the host between two launches: the second
    launch's projection carries the new value, in an object of its own, and nothing of
    the first launch (no file, no home)."""
    first = _plan(provider, profile, {name: SENTINEL})
    second = _plan(provider, profile, {name: SENTINEL_ROTATED})
    assert first == second  # the same route, kind and mechanism
    assert first.projection.env[0].value is not second.projection.env[0].value
    assert client_environment({}, second.projection) == {name: SENTINEL_ROTATED}
    assert second.projection.files == ()


@pytest.mark.parametrize(("provider", "profile", "name"), ROUTES)
def test_a_repository_command_gets_no_model_credential(provider, profile, name):
    """With #286 in place: the plan refuses an export command's launch class, and the
    repository commands' allow-list passes none of the routes' variables."""
    refused = _refusal(provider, profile, {name: SENTINEL}, launch_class=LaunchClass.EXPORT_COMMAND)
    assert "repository command" in str(refused)
    commands = select_environment(ExecutionConfig().command_environment_names(), {name: SENTINEL})
    assert name not in commands


def test_a_key_reaches_the_plan_only_through_the_agent_allow_list(monkeypatch):
    """The plan reads the launch's environment after its allow-list: Pi's DeepSeek key
    is forwarded only by ``execution.agent_env_allowlist_extra``."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", SENTINEL)
    provider = PiProvider()
    profile = _profile("pi", "deepseek/deepseek-flash", require_oauth="false")
    plain = ExecutionConfig()
    refused = _refusal(
        provider, profile, provider.launch_environment(plain.agent_environment_names())
    )
    assert "DEEPSEEK_API_KEY" in str(refused)
    forwarded = ExecutionConfig(agent_env_allowlist_extra=["DEEPSEEK_API_KEY"])
    env = provider.launch_environment(forwarded.agent_environment_names())
    assert _plan(provider, profile, env).name == "DEEPSEEK_API_KEY"


# -- what redaction recognises, as the compatibility matrix states it ------------------


def _shaped(*parts: str) -> str:
    """A fake value of a provider's key shape, assembled so no literal here has it."""
    return "".join(parts)


# The shape each route's value has, as the provider issues it (docs/agent-guides/
# github-safety.md, "Provider credential mapping"): every one is redacted by its
# shape, wherever it appears.
SHAPES = {
    "ANTHROPIC_API_KEY": _shaped("sk", "-ant-api03-", "Q" * 40),
    "CLAUDE_CODE_OAUTH_TOKEN": _shaped("sk", "-ant-oat01-", "Q" * 40),
    "OPENAI_API_KEY": _shaped("sk", "-proj-", "Q" * 40),
    "DEEPSEEK_API_KEY": _shaped("sk", "-", "0a" * 16),
    "GOOGLE_GENERATIVE_AI_API_KEY": _shaped("AI", "za", "Q" * 35),
}


def test_every_routed_variable_has_a_documented_shape():
    names = {r.name for r in CLAUDE_CREDENTIAL_ROUTES if r.refresh is Refresh.NONE}
    names |= set(OPENCODE_KEY_NAMES.values()) | set(PI_KEY_NAMES.values())
    assert names == set(SHAPES)


@pytest.mark.parametrize(("name", "value"), sorted(SHAPES.items()))
def test_redaction_recognises_each_routed_key_by_its_shape(name, value):
    for text in (
        value,
        f"{name}={value}",
        f'{{"{name}": "{value}"}}',
        f"error: key {value} denied",
    ):
        assert value not in redact(text), text


# Whether a ``NAME=value`` assignment is redacted by the name alone, for a value of no
# known shape: the matrix's second column. A "no" that redaction later learns (#285)
# fails here, so the documentation is brought along with it.
BY_NAME = {
    "ANTHROPIC_API_KEY": True,
    "CLAUDE_CODE_OAUTH_TOKEN": True,
    "OPENAI_API_KEY": True,
    "DEEPSEEK_API_KEY": False,
    "GOOGLE_GENERATIVE_AI_API_KEY": False,
}


@pytest.mark.parametrize(("name", "recognised"), sorted(BY_NAME.items()))
def test_redaction_recognises_these_routed_names_and_not_the_others(name, recognised):
    assert set(BY_NAME) == set(SHAPES)
    plain = "plainvalue290plainvalue"
    assert (plain not in redact(f"{name}={plain}")) is recognised
