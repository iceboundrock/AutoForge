"""Each provider's model credential under the container backend (#290, ADR 0013 D14).

With fake credentials, the real adapters' declarations and no runtime: each
provider's login maps to exactly one of #270's mechanisms, and on a copy-in
runtime to the environment variable; the value is on no argv and in no fact,
description, refusal or helper container; a login that rotates its refresh
token on use, or that the CLI rewrites in place, is never projected and the
host's copy is left as it was; a relaunch gets a new projection; the model
credential reaches an agent launch and never a repository command. Of the
launch's environment the container receives only a positive allow-list, and
every store variable names a fresh place in the private home.

Then with fake provider CLIs (``tests/credential_fake.py``), which find their
credential by each CLI's own rules: each route, launched with the adapter's
own argv and exactly what the launch container receives, authenticates with
the projected key, in a private store that starts empty, and sees no other
credential; an inherited store at a host path or in the workspace is never
selected; a refused route starts nothing; a replacement launch sees nothing
of the first.
"""

from __future__ import annotations

import json
import os
import pickle
from collections.abc import Mapping
from pathlib import Path

import pytest

from autoforge.child_env import undoes_isolation
from autoforge.config import ExecutionConfig, ProfileConfig
from autoforge.container_auth import (
    PASSED_NAMES,
    CredentialKind,
    CredentialPlan,
    CredentialRoutes,
    EnvRoute,
    Mechanism,
    PrivateStore,
    Refresh,
    credential_looking,
    plan_credentials,
)
from autoforge.errors import SandboxPolicyError
from autoforge.executor import ExecutionRequest, ExecutionResult, execute, select_environment
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
    HOME_TARGET,
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
    create_options,
)
from autoforge.sandbox_policy import LaunchClass, ProviderRequirements
from tests.credential_fake import Container, digest, store_login

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


# Every variable a route of some adapter reads, refused routes included.
ROUTE_NAMES = sorted(
    {r.name for r in CLAUDE_CREDENTIAL_ROUTES}
    | set(OPENCODE_KEY_NAMES.values())
    | set(PI_KEY_NAMES.values())
)


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


# Credentials no naming rule recognises, each forwarded on purpose by an allow-list
# entry: none ends in ``_API_KEY``, and some name nothing like a credential at all.
UNNAMED_CREDENTIALS = {
    "AZURE_OPENAI_KEY": SENTINEL_OTHER,
    "MY_PROVIDER_CREDENTIAL": SENTINEL_OTHER,
    "DATABASE_URL": f"postgres://agent:{SENTINEL_OTHER}@db.invalid/app",
    "AWS_SECRET_ACCESS_KEY": SENTINEL_OTHER,
    "OPENCODE_SERVER_PASSWORD": SENTINEL_OTHER,
    "HTTPS_PROXY": f"http://agent:{SENTINEL_OTHER}@proxy.invalid:3128",
    "SERVICE_BLOB": SENTINEL_OTHER,
}
# What a container launch receives of the launch's environment, besides the
# projected variable, written out here rather than read from the plan.
PRIVATE_STORES = {
    "claude": {"CLAUDE_CONFIG_DIR": "/home/agent/.claude"},
    "opencode": {"OPENCODE_DB": "/home/agent/.local/share/opencode/opencode.db"},
    "pi": {"PI_CODING_AGENT_DIR": "/home/agent/.pi/agent"},
}
HOST_SETTINGS = {
    "PATH": "/usr/local/bin:/usr/bin",
    "HOME": "/home/operator",
    "USER": "operator",
    "TMPDIR": "/run/user/1000/tmp",
    "XDG_DATA_HOME": "/home/operator/.local/share",
    "XDG_CONFIG_HOME": "/home/operator/.config",
    "LANG": "C.UTF-8",
    "TZ": "UTC",
    "GIT_AUTHOR_NAME": "Operator",
    "PI_OFFLINE": "1",
    "OPENCODE_CONFIG_DIR": "/tmp/autoforge-opencode-1",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "8000",
    "ANTHROPIC_BASE_URL": "https://gateway.invalid",
}


def _expected_environment(provider: AgentProvider) -> dict[str, str]:
    passed = {"LANG": "C.UTF-8", "TZ": "UTC", "GIT_AUTHOR_NAME": "Operator"}
    if provider.name == "pi":
        passed["PI_OFFLINE"] = "1"
    return {"HOME": "/home/agent", **PRIVATE_STORES[provider.name], **passed}


@pytest.mark.parametrize(("provider", "profile", "name"), ROUTES)
def test_the_container_receives_a_positive_allow_list_and_nothing_else(provider, profile, name):
    """Fail closed: of the launch's environment the container receives the projected
    variable, the private home and stores, and the allow-listed settings, each by its
    exact name. A credential no rule names (``AZURE_OPENAI_KEY``, a password in a URL),
    another provider's key, the host's paths and every other setting are withheld."""
    others = {
        other: SENTINEL_OTHER
        for other in ROUTE_NAMES
        if other not in {r.name for r in provider.credential_routes(profile).routes}
    }
    env = {**HOST_SETTINGS, **UNNAMED_CREDENTIALS, **others, name: SENTINEL}
    plan = _plan(provider, profile, env)
    assert plan.container_environment() == _expected_environment(provider)
    assert [item.name for item in plan.projection.env] == [name]
    passed = set(_expected_environment(provider)) - {"HOME", *PRIVATE_STORES[provider.name]}
    assert set(plan.withheld) == set(env) - passed - {name}
    assert set(UNNAMED_CREDENTIALS) | set(others) <= set(plan.withheld)
    for value in plan.container_environment().values():
        assert SENTINEL_OTHER not in value and SENTINEL not in value
    assert SENTINEL_OTHER not in json.dumps(plan.facts()) + plan.describe()


def test_every_passed_name_and_declared_setting_is_no_credential():
    """The allow-list itself holds no credential, no store and nothing that could hand
    the launch one: no prefix, no key, token or password."""
    settings = {
        setting
        for provider, profile, _ in (p.values for p in ROUTES)
        for setting in provider.credential_routes(profile).settings
    }
    assert settings == {"PI_OFFLINE", "PI_SKIP_VERSION_CHECK", "PI_TELEMETRY"}
    for name in PASSED_NAMES | settings:
        assert "*" not in name and not credential_looking(name), name
        assert not undoes_isolation(name), name
    assert not PASSED_NAMES & (set(ROUTE_NAMES) | {"HOME", "PATH"})


_X_ROUTE = (EnvRoute("X_KEY", CredentialKind.API_KEY),)


@pytest.mark.parametrize(
    "routes",
    [
        pytest.param(CredentialRoutes("x", _X_ROUTE, settings=("AZURE_OPENAI_KEY",)), id="key"),
        pytest.param(CredentialRoutes("x", _X_ROUTE, settings=("SERVICE_TOKEN",)), id="token"),
        pytest.param(CredentialRoutes("x", _X_ROUTE, settings=("X_KEY",)), id="route"),
        pytest.param(CredentialRoutes("x", _X_ROUTE, settings=("GH_TOKEN",)), id="github"),
        pytest.param(CredentialRoutes("x", _X_ROUTE, settings=("LC_*",)), id="prefix"),
        pytest.param(
            CredentialRoutes("x", _X_ROUTE, stores=(PrivateStore("X_DIR", "/abs"),)),
            id="absolute",
        ),
        pytest.param(
            CredentialRoutes("x", _X_ROUTE, stores=(PrivateStore("X_DIR", "../x"),)),
            id="parent",
        ),
        pytest.param(
            CredentialRoutes("x", _X_ROUTE, stores=(PrivateStore("X_KEY", "k"),)),
            id="store-is-route",
        ),
        pytest.param(
            CredentialRoutes(
                "x", _X_ROUTE, stores=(PrivateStore("X_DIR", "d"),), settings=("X_DIR",)
            ),
            id="store-as-setting",
        ),
    ],
)
def test_a_declaration_that_would_pass_a_credential_or_leave_a_store_is_refused(routes):
    """The allow-list fails closed on a bad declaration too: a credential listed as a
    setting, a prefix, or a store outside the private home refuses every launch."""
    with pytest.raises(SandboxPolicyError) as refused:
        plan_credentials("fake", routes, NO_NEEDS, {"X_KEY": SENTINEL}, launch_class=AGENT)
    assert SENTINEL not in str(refused.value) and "declares" in str(refused.value)


# -- a fresh private store, never an inherited one ------------------------------------


@pytest.mark.parametrize(("provider", "profile", "name"), ROUTES)
@pytest.mark.parametrize(
    "inherited",
    [
        pytest.param({}, id="unset"),
        pytest.param(
            {
                "CLAUDE_CONFIG_DIR": "/home/operator/claude-autoforge",
                "OPENCODE_DB": "/home/operator/opencode-autoforge/af.db",
                "PI_CODING_AGENT_DIR": "/home/operator/pi-autoforge",
            },
            id="host-path",
        ),
        pytest.param(
            {
                "CLAUDE_CONFIG_DIR": ".claude",
                "OPENCODE_DB": "/workspace/.opencode/opencode.db",
                "PI_CODING_AGENT_DIR": ".pi/agent",
            },
            id="workspace-path",
        ),
    ],
)
def test_every_store_variable_names_a_place_in_the_private_home(provider, profile, name, inherited):
    """Whatever the launch's environment held, the home and the CLI's store are the
    private home's: never the host path (absent or unwritable inside), never one in the
    workspace or a volume (a store left there would win over the projected key)."""
    plan = _plan(provider, profile, {**inherited, "HOME": "/home/operator", name: SENTINEL})
    received = plan.container_environment()
    assert received["HOME"] == HOME_TARGET
    for store, value in PRIVATE_STORES[provider.name].items():
        assert received[store] == value and value.startswith(HOME_TARGET + "/")
        if store in inherited:
            assert store in plan.withheld
    for value in inherited.values():
        assert value not in received.values()


def test_the_private_home_is_a_tmpfs_each_create_makes_anew():
    """Where every store lies: the launch container's create mounts a tmpfs of its own
    there, mode 0700 and the agent's, and binds nothing at it or below it."""
    spec = ContainerSpec(ContainerRole.LAUNCH, ContainerNetwork.NONE, "af-vol-290")
    options = create_options(RuntimeProduct.PODMAN, spec)
    homes = [o for o in options if o.startswith(f"--tmpfs={HOME_TARGET}:")]
    assert len(homes) == 1 and "mode=0700" in homes[0]
    assert not any(o.startswith("--mount") and HOME_TARGET in o for o in options)


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
def test_a_relaunch_gets_a_new_projection_from_the_environment_it_is_given(provider, profile, name):
    """Each launch's projection is made from the environment it is given (the
    controller process's own, which a shell does not change once it runs): a plan made
    from a rotated value carries it, in an object of its own, and nothing of the first
    launch (no file, no home)."""
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


# -- the routes through fake provider CLIs (ADR 0013 §7) -------------------------------


def _host_environment(provider: AgentProvider, profile: ProfileConfig, name: str) -> dict:
    """The operator's shell for a launch of ``profile``: the route's key, every other
    route's variable that is no route of this profile, credentials no rule names, the
    host's settings and the CLI's store variables at host paths."""
    own = {r.name for r in provider.credential_routes(profile).routes}
    return {
        **HOST_SETTINGS,
        **UNNAMED_CREDENTIALS,
        **{other: SENTINEL_OTHER for other in ROUTE_NAMES if other not in own},
        "CLAUDE_CONFIG_DIR": "/home/operator/claude-autoforge",
        "OPENCODE_DB": "/home/operator/opencode-autoforge/af.db",
        "PI_CODING_AGENT_DIR": "/home/operator/pi-autoforge",
        name: SENTINEL,
    }


def _launch_environment(monkeypatch, provider: AgentProvider, shell: Mapping[str, str]) -> dict:
    """What the adapter's launch inherits from a controller started in ``shell``, with
    every variable of it forwarded by ``execution.agent_env_allowlist_extra``: the
    worst an operator's configuration can do."""
    for key in list(os.environ):
        monkeypatch.delenv(key)
    for key, value in shell.items():
        monkeypatch.setenv(key, value)
    config = ExecutionConfig(agent_env_allowlist_extra=sorted(shell))
    return provider.launch_environment(config.agent_environment_names())


def _run(container: Container, provider, profile, environment: Mapping[str, str]):
    """The adapter's own argv, with the fake CLI in place of the CLI, run by the
    executor in the container's ``/workspace`` with ``environment`` and nothing else."""
    result = execute(
        ExecutionRequest(
            container.argv(provider.build_command_for(profile, "PROMPT-290")),
            cwd=str(container.workspace),
            env=dict(environment),
            env_allowlist=(),
            timeout_seconds=60,
        )
    )
    return result, container.read_report()


def _launch(container: Container, provider, profile, env, needs=NO_NEEDS):
    """One container launch as #271 will make it, short of the runtime: the plan, then
    the CLI started with exactly what the launch container receives, its environment
    and the projected variable as the runtime client's environment values it."""
    plan = _plan(provider, profile, env, needs)
    inside = {**plan.container_environment(), **client_environment({}, plan.projection)}
    return (plan, *_run(container, provider, profile, inside))


def _assert_no_value_in(result: ExecutionResult, report: dict, *texts: str) -> None:
    for text in (result.stdout, result.stderr, json.dumps(report["argv"]), *texts):
        for sentinel in (SENTINEL, SENTINEL_OTHER, SENTINEL_ROTATED, SENTINEL_LOGIN):
            assert sentinel not in text


@pytest.mark.parametrize(("provider", "profile", "name"), ROUTES)
def test_each_route_authenticates_the_cli_with_the_projected_key_alone(
    provider, profile, name, tmp_path, monkeypatch, host
):
    """ADR 0013 §7 with fake CLIs: launched with its adapter's argv and exactly what the
    launch container receives, each CLI finds the projected key under the route's
    variable, in a private store that did not exist before it started, and sees no
    other credential, whatever the operator's shell held and forwarded; no value is on
    its argv, the create's, or in anything it or the plan says."""
    env = _launch_environment(monkeypatch, provider, _host_environment(provider, profile, name))
    container = Container.make(tmp_path / "launch")
    plan, result, report = _launch(container, provider, profile, env)
    assert result.exit_code == 0, result.stderr
    assert report["source"] == f"env:{name}"
    assert report["digest"] == digest(SENTINEL)
    assert report["home"] == "/home/agent"
    assert report["store"].startswith("/home/agent/") and report["store_existed"] is False
    assert report["wrote"] is True
    expected = {**_expected_environment(provider), name: SENTINEL}
    assert report["env"] == {key: digest(value) for key, value in sorted(expected.items())}
    others = {digest(v) for v in (SENTINEL_OTHER, *UNNAMED_CREDENTIALS.values())}
    assert not others & set(report["env"].values())
    create = create_argv(
        RuntimeProduct.PODMAN,
        _spec(plan, StagingMethod.BIND),
        host,
        name="af-launch-290",
        labels={"io.autoforge.run": "run-290"},
        image=IMAGE,
        command=["/opt/autoforge/watcher"],
    )
    _assert_no_value_in(
        result, report, " ".join(create), plan.describe(), json.dumps(plan.facts()), repr(plan)
    )


# Where a store an inherited variable names would be found, per CLI: the variable,
# the value the operator's shell held, and the store's path inside the container.
WORKSPACE_STORES = {
    "claude": ("CLAUDE_CONFIG_DIR", ".claude", "/workspace/.claude"),
    "opencode": (
        "OPENCODE_DB",
        "/workspace/.opencode/opencode.db",
        "/workspace/.opencode/opencode.db",
    ),
    "pi": ("PI_CODING_AGENT_DIR", ".pi/agent", "/workspace/.pi/agent"),
}


@pytest.mark.parametrize(("provider", "profile", "name"), ROUTES)
def test_a_populated_store_in_the_workspace_is_never_selected(
    provider, profile, name, tmp_path, monkeypatch
):
    """The repository under work holds a login in a store the operator's variable
    names inside the container. Passed on (the control), the CLI would read that store
    and write into the workspace, and OpenCode and Pi would take its login ahead of the
    key. Under the plan the CLI uses a fresh store of the private home and the key."""
    variable, value, store = WORKSPACE_STORES[provider.name]
    shell = {**_host_environment(provider, profile, name), variable: value}
    env = _launch_environment(monkeypatch, provider, shell)
    scope = provider.credential_routes(profile).scope

    control = Container.make(tmp_path / "control")
    store_login(control.path(store), provider.name, scope, SENTINEL_LOGIN)
    inherited = {key: env[key] for key in env if key not in {"HOME", "PATH"}}
    _, report = _run(control, provider, profile, {**inherited, "HOME": "/home/agent"})
    assert report["store"] == store and report["store_existed"] is True
    if provider.name != "claude":  # Claude Code takes its environment's key first
        assert report["source"].startswith("store:")
        assert report["digest"] == digest(SENTINEL_LOGIN)

    container = Container.make(tmp_path / "launch")
    store_login(container.path(store), provider.name, scope, SENTINEL_LOGIN)
    _, result, report = _launch(container, provider, profile, env)
    assert result.exit_code == 0, result.stderr
    assert report["source"] == f"env:{name}" and report["digest"] == digest(SENTINEL)
    assert report["store"] == PRIVATE_STORES[provider.name][variable]
    assert report["store_existed"] is False
    assert not any("written_by" in p.read_text() for p in _files(container.workspace))


@pytest.mark.parametrize(("provider", "profile", "name"), ROUTES)
def test_a_store_at_a_host_path_is_never_selected(provider, profile, name, tmp_path, monkeypatch):
    """The operator's store variables name host directories (as the bwrap backend
    wants them). Passed on (the control), the CLI finds nothing there and cannot write
    its store, which the container does not hold; under the plan it uses the private
    home's."""
    env = _launch_environment(monkeypatch, provider, _host_environment(provider, profile, name))
    control = Container.make(tmp_path / "control")
    inherited = {key: env[key] for key in env if key not in {"HOME", "PATH"}}
    result, report = _run(control, provider, profile, {**inherited, "HOME": "/home/agent"})
    assert report["store"].startswith("/home/operator/") and report["store_existed"] is False
    assert result.exit_code == 4 and report["wrote"] is False

    container = Container.make(tmp_path / "launch")
    _, result, report = _launch(container, provider, profile, env)
    assert result.exit_code == 0, result.stderr
    assert report["store"] == next(iter(PRIVATE_STORES[provider.name].values()))


def _files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


@pytest.mark.parametrize(("provider", "profile", "name"), ROUTES)
def test_a_replacement_launch_sees_nothing_of_the_first(
    provider, profile, name, tmp_path, monkeypatch
):
    """Container A's CLI writes its store; container B, a replacement made by a
    controller started after the operator rotated the key, shares A's workspace and
    cache volume. B's CLI finds an empty store and the rotated key, and nothing A wrote.
    The control: a store variable that named the cache volume, passed on, would hand B
    what A left there."""
    workspace, cache = tmp_path / "workspace", tmp_path / "cache"
    workspace.mkdir()
    cache.mkdir()
    shell = _host_environment(provider, profile, name)
    first = Container.make(tmp_path / "a", workspace=workspace, cache=cache)
    plan_a, result, report = _launch(
        first, provider, profile, _launch_environment(monkeypatch, provider, shell)
    )
    assert result.exit_code == 0 and report["wrote"] is True
    assert _files(first.path(HOME_TARGET))  # A wrote into its own private home

    rotated = {**shell, name: SENTINEL_ROTATED}
    second = Container.make(tmp_path / "b", workspace=workspace, cache=cache)
    plan_b, result, report = _launch(
        second, provider, profile, _launch_environment(monkeypatch, provider, rotated)
    )
    assert result.exit_code == 0, result.stderr
    assert report["digest"] == digest(SENTINEL_ROTATED) and report["store_existed"] is False
    assert plan_b.projection.env[0].value is not plan_a.projection.env[0].value
    assert not _files(workspace) and not _files(cache)

    variable = next(iter(PRIVATE_STORES[provider.name]))
    on_cache = "/cache/store.db" if provider.name == "opencode" else "/cache/store"
    for launch in ("c", "d"):
        control = Container.make(tmp_path / launch, workspace=workspace, cache=cache)
        environment = {"HOME": "/home/agent", variable: on_cache, name: SENTINEL}
        _, report = _run(control, provider, profile, environment)
    assert report["store_existed"] is True


@pytest.mark.parametrize(
    ("provider", "profile", "env", "launch_class"),
    [
        pytest.param(
            ClaudeCodeProvider(),
            _profile("claude", "fable"),
            {"ANTHROPIC_API_KEY": SENTINEL, "CLAUDE_CODE_OAUTH_TOKEN": SENTINEL_OTHER},
            AGENT,
            id="two-claude-routes",
        ),
        pytest.param(
            ClaudeCodeProvider(),
            _profile("claude", "fable"),
            {"CLAUDE_CODE_OAUTH_REFRESH_TOKEN": SENTINEL_ROTATED},
            AGENT,
            id="rotating-refresh-token",
        ),
        pytest.param(
            ClaudeCodeProvider(),
            _profile("claude", "fable"),
            {"CLAUDE_CODE_OAUTH_TOKEN": SENTINEL, "ANTHROPIC_AUTH_TOKEN": SENTINEL_OTHER},
            AGENT,
            id="gateway-token",
        ),
        pytest.param(
            PiProvider(),
            _profile("pi", "openai/gpt-5.6-terra"),
            {"OPENAI_API_KEY": SENTINEL},
            AGENT,
            id="pi-chatgpt-sign-in",
        ),
        pytest.param(
            OpenCodeProvider(),
            _profile("opencode", "groq/llama"),
            {"GROQ_API_KEY": SENTINEL},
            AGENT,
            id="unmapped-model-provider",
        ),
        pytest.param(
            PiProvider(),
            _profile("pi", "deepseek/deepseek-flash", require_oauth="false"),
            {"PATH": "/usr/bin"},
            AGENT,
            id="no-credential",
        ),
        pytest.param(
            OpenCodeProvider(),
            _profile("opencode", "openai/gpt-5.6-luna"),
            {"OPENAI_API_KEY": SENTINEL},
            LaunchClass.EXPORT_COMMAND,
            id="repository-command",
        ),
    ],
)
def test_a_refused_route_starts_no_cli(provider, profile, env, launch_class, tmp_path):
    """A refusal comes before anything is started: the fake CLI never runs, and the
    refusal names variables and kinds, never a value."""
    container = Container.make(tmp_path / "launch")
    with pytest.raises(SandboxPolicyError) as refused:
        _launch_refused(container, provider, profile, env, launch_class)
    for sentinel in (SENTINEL, SENTINEL_OTHER, SENTINEL_ROTATED):
        assert sentinel not in str(refused.value)
    assert container.read_report() is None


def _launch_refused(container, provider, profile, env, launch_class):
    plan = plan_credentials(
        provider.name, provider.credential_routes(profile), NO_NEEDS, env, launch_class=launch_class
    )
    inside = {**plan.container_environment(), **client_environment({}, plan.projection)}
    _run(container, provider, profile, inside)
