"""The environment of every process AutoForge launches outside itself (#165).

Real git and the real executor throughout; ``gh``, ``ssh``, ``curl`` and a
git credential helper are PATH-injected fakes that succeed only when they
find a credential route, so each test shows the route closed for a child
and open for the controller's own calls. No network, and no real
credential: every "token" here is a fixed fake.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from autoforge import child_env
from autoforge.child_env import (
    FORBIDDEN_ENV_NAMES,
    GH_PLACEHOLDER_TOKEN,
    SSH_COMMAND,
    GitConfigEntry,
    LaunchIdentity,
    child_overrides,
    forbidden_env_entry,
    git_config_problems,
    isolated_overrides,
    netrc_problems,
    override_names,
    parse_git_config_listing,
    preflight,
    read_git_config,
    render_git_config,
    url_has_credential,
)
from autoforge.config import DEFAULT_ENV_ALLOWLIST
from autoforge.errors import ConfigurationError, ExecutionError
from autoforge.executor import ExecutionRequest, ExecutionResult, execute, select_environment
from autoforge.github import GitHubClient
from autoforge.providers import ClaudeCodeProvider, OpenCodeProvider, PiProvider
from tests.conftest import git_repo

# Shaped like a classic personal access token, so the redactor would catch
# it; fixed, fake, and never valid anywhere.
FAKE_TOKEN = "ghp_" + "T" * 36
FAKE_PASSWORD = "probe-password-" + "P" * 12


def _git(cwd: Path, *args: str, env: dict | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout


# -- the refusal ----------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(FORBIDDEN_ENV_NAMES))
def test_every_forbidden_name_is_refused_with_its_rule(name):
    assert forbidden_env_entry(name) == (name, FORBIDDEN_ENV_NAMES[name])


def test_a_prefix_is_refused_when_it_covers_a_forbidden_name_or_prefix():
    assert forbidden_env_entry("GH_*") == ("GH_TOKEN", FORBIDDEN_ENV_NAMES["GH_TOKEN"])
    assert forbidden_env_entry("GIT_CONFIG_KEY_*")[0] == "GIT_CONFIG_KEY_*"
    assert forbidden_env_entry("GIT_CONFIG_VALUE_12*")[0] == "GIT_CONFIG_VALUE_*"
    assert forbidden_env_entry("*")[0] == "GH_TOKEN"
    for fine in ("GIT_AUTHOR_*", "LC_*", "GITLAB_TOKEN", "SSH_CONNECTION", "XDG_CACHE_HOME"):
        assert forbidden_env_entry(fine) is None, fine


def test_the_refusal_names_the_entry_and_the_rule_never_a_value(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", FAKE_TOKEN)
    with pytest.raises(ConfigurationError) as caught:
        child_env.refuse_forbidden_env(["PATH", "GH_*"], "somewhere")
    message = str(caught.value)
    assert "somewhere" in message and "'GH_*'" in message and "GH_TOKEN" in message
    assert FORBIDDEN_ENV_NAMES["GH_TOKEN"] in message
    assert FAKE_TOKEN not in message


def test_no_default_name_or_provider_name_is_forbidden():
    for names in (
        DEFAULT_ENV_ALLOWLIST,
        ClaudeCodeProvider.environment_names,
        OpenCodeProvider.environment_names,
        PiProvider.environment_names,
    ):
        assert [n for n in names if forbidden_env_entry(n)] == [], names


# -- the child's environment ----------------------------------------------------


@pytest.fixture
def polluted_operator(tmp_path, monkeypatch):
    """An operator environment holding every credential route there is.

    A token in each gh variable, a gh configuration with a login, a global
    git configuration naming a credential helper, askpass programs, an SSH
    agent socket, an ssh command, git configuration in the environment and
    the session bus. Returns the global configuration's path.
    """
    gh_dir = tmp_path / "operator-gh"
    gh_dir.mkdir()
    (gh_dir / "hosts.yml").write_text(f"github.com:\n    oauth_token: {FAKE_TOKEN}\n")
    global_config = tmp_path / "operator-gitconfig"
    global_config.write_text(
        "[user]\n\tname = Operator Name\n\temail = operator@example.invalid\n"
        "[credential]\n\thelper = probe\n"
        '[url "https://x-access-token:secret@github.com/"]\n\tinsteadOf = https://github.com/\n'
    )
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        monkeypatch.setenv(name, FAKE_TOKEN)
    monkeypatch.setenv("GH_HOST", "github.com")
    monkeypatch.setenv("GH_CONFIG_DIR", str(gh_dir))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_ASKPASS", "/bin/false")
    monkeypatch.setenv("SSH_ASKPASS", "/bin/false")
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "agent.sock"))
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /operator/key")
    monkeypatch.setenv("GIT_SSH", "/usr/bin/ssh")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "probe")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'credential.helper'='probe'")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={tmp_path / 'bus'}")
    # A home of the test's own, so nothing reads the developer's dotfiles.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return global_config


def _child_env(overrides: dict[str, str], allowlist=DEFAULT_ENV_ALLOWLIST) -> dict[str, str]:
    """What the executor hands a child: the allow-listed names, overrides on top."""
    env = select_environment(allowlist)
    env.update(overrides)
    return env


def test_the_child_environment_carries_no_operator_credential_route(polluted_operator, tmp_path):
    identity = preflight(str(git_repo(tmp_path / "repo")))
    with isolated_overrides(identity) as overrides:
        env = _child_env(overrides)
        # Every forbidden name the child holds is one the controller set...
        held = {name for name in env if forbidden_env_entry(name)}
        assert held == {n for n in override_names() if forbidden_env_entry(n)}
        assert "SSH_AUTH_SOCK" not in env and "GIT_ASKPASS" not in env
        assert "XDG_RUNTIME_DIR" not in env and "DBUS_SESSION_BUS_ADDRESS" not in env
        assert "GITHUB_TOKEN" not in env and "GH_HOST" not in env
        # ...and none carries the operator's value.
        assert env["GH_TOKEN"] == env["GH_ENTERPRISE_TOKEN"] == GH_PLACEHOLDER_TOKEN
        assert FAKE_TOKEN not in json.dumps(env)
        gh_dir = Path(env["GH_CONFIG_DIR"])
        assert gh_dir.is_dir() and list(gh_dir.iterdir()) == []
        assert env["GIT_CONFIG_GLOBAL"] != str(polluted_operator)
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        root = gh_dir.parent
    # The launch's directory does not outlive it.
    assert not root.exists()


def test_the_child_git_reads_the_controller_configuration_not_the_operators(
    polluted_operator, tmp_path
):
    repo = git_repo(tmp_path / "repo")
    identity = preflight(str(repo))
    assert identity.describe() == "user.name, user.email"
    with isolated_overrides(identity) as overrides:
        env = _child_env(overrides)
        listing = _git(repo, "config", "--list", "--show-scope", env=env)
        assert "global\tuser.name=Operator Name" in listing
        assert "global\tuser.email=operator@example.invalid" in listing
        assert "insteadof" not in listing.lower()
        helpers = _git(repo, "config", "--get-all", "credential.helper", env=env).splitlines()
        assert helpers[-1] == ""  # the command scope's reset comes last
        assert _git(repo, "config", "core.sshCommand", env=env).strip() == SSH_COMMAND
        # The identity reaches a commit.
        _git(repo, "commit", "--allow-empty", "-q", "-m", "child", env=env)
        assert _git(repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>").strip() == (
            "Operator Name <operator@example.invalid>|Operator Name <operator@example.invalid>"
        )
        # `git config --global` writes land in the launch's file.
        _git(repo, "config", "--global", "alias.st", "status", env=env)
        assert "alias" not in polluted_operator.read_text()


def test_identity_values_are_escaped_and_read_back_verbatim(tmp_path):
    name = 'Ann "Quote" O\\Brien\t#; x'
    text = render_git_config(LaunchIdentity((("user.name", name), ("user.email", "a@b.c"))))
    path = tmp_path / "cfg"
    path.write_text(text)
    assert _git(tmp_path, "config", "-f", str(path), "user.name").rstrip("\n") == name
    # A value git cannot hold in a file is dropped, never written raw.
    assert "user" not in render_git_config(LaunchIdentity((("user.name", "a\rb"),)))


def test_without_an_operator_identity_none_is_supplied(tmp_path, monkeypatch):
    empty = tmp_path / "empty-global"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    identity = preflight(str(git_repo(tmp_path / "repo")))
    assert identity == LaunchIdentity() and "none" in identity.describe()


def test_the_overrides_are_names_known_in_advance():
    env = child_overrides("/gh", "/gitconfig")
    assert tuple(sorted(env)) == override_names()
    assert env["GIT_CONFIG_COUNT"] == "4"
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(4)}
    assert pairs == {
        "credential.helper": "",
        "core.askPass": "",
        "core.sshCommand": SSH_COMMAND,
        "http.extraHeader": "",
    }


def test_a_launch_directory_that_cannot_be_prepared_is_an_execution_error(monkeypatch):
    def refuse(**_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(child_env.tempfile, "mkdtemp", refuse)
    with pytest.raises(ExecutionError, match="cannot prepare"):
        with isolated_overrides(LaunchIdentity()):
            pass


# -- PATH-injected fakes: agents fail, the controller succeeds -------------------

_PROBE = r"""
import json, os, sys
from pathlib import Path

TOOL = {tool!r}
LOG = {log!r}
KEYCHAIN = {keychain!r}
DEFAULT_KEY = {default_key!r}
PLACEHOLDER = {placeholder!r}
args = sys.argv[1:]
env = os.environ


def token(*names):
    for name in names:
        if name in env:
            return env[name] not in ("", PLACEHOLDER)
    return None


def gh():
    # gh: a token in the environment first (used as is, the keyring never
    # asked); then hosts.yml; then the keyring (reached through the session
    # bus on Linux, through the keychain by any process on macOS).
    found = token("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
    if found is not None:
        return found
    config = env.get("GH_CONFIG_DIR") or os.path.join(
        env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", "/"), ".config"), "gh"
    )
    hosts = Path(config, "hosts.yml")
    if hosts.exists() and "oauth_token" in hosts.read_text():
        return True
    if "DBUS_SESSION_BUS_ADDRESS" in env or "XDG_RUNTIME_DIR" in env:
        return True
    return os.path.exists(KEYCHAIN)


def ssh():
    # The agent, unless the command turned it off; a default key, found
    # through the password database (not HOME), unless no identity file.
    agent = "SSH_AUTH_SOCK" in env and "IdentityAgent=none" not in args
    key = os.path.exists(DEFAULT_KEY) and "IdentityFile=none" not in args
    return agent or key


def curl():
    if token("GH_TOKEN", "GITHUB_TOKEN"):
        return True
    if "-n" in args or "--netrc" in args:
        netrc = Path(env.get("HOME", "/"), ".netrc")
        return netrc.exists() and "github.com" in netrc.read_text()
    return False


def helper():
    # A git credential helper: answers `get` with a password.
    if args[-1:] == ["get"]:
        sys.stdin.read()
        print("username=x-access-token")
        print("password={password}")
    return True


ok = {{"gh": gh, "ssh": ssh, "curl": curl, "git-credential-probe": helper}}[TOOL]()
with open(LOG, "a", encoding="utf-8") as f:
    f.write(json.dumps({{"tool": TOOL, "argv": args, "ok": ok,
                        "env": sorted(n for n in env if not n.startswith("LC_"))}}) + "\n")
sys.exit(0 if ok else 1)
"""


class Probes:
    """The fake tools on a ``bin`` directory, and what each invocation found."""

    def __init__(self, root: Path) -> None:
        self.bin = root / "bin"
        self.bin.mkdir(parents=True)
        self.log = root / "probe.jsonl"
        self.keychain = root / "keychain"
        self.default_key = root / "passwd-home" / ".ssh" / "id_ed25519"
        self.default_key.parent.mkdir(parents=True)
        self.default_key.write_text("fake key\n")
        self.keychain.write_text("fake keychain\n")
        for tool in ("gh", "ssh", "curl", "git-credential-probe"):
            script = self.bin / tool
            script.write_text(
                f"#!{sys.executable}\n"
                + _PROBE.format(
                    tool=tool,
                    log=str(self.log),
                    keychain=str(self.keychain),
                    default_key=str(self.default_key),
                    placeholder=GH_PLACEHOLDER_TOKEN,
                    password=FAKE_PASSWORD,
                )
            )
            script.chmod(0o755)

    def entries(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


@pytest.fixture
def probes(tmp_path, monkeypatch, polluted_operator):
    p = Probes(tmp_path / "probes")
    monkeypatch.setenv("PATH", f"{p.bin}{os.pathsep}{os.environ['PATH']}")
    return p


_CREDENTIAL_QUERY = b"protocol=https\nhost=github.com\npath=owner/repo.git\n\n"


def _run(command, cwd, env_allowlist=None, env=None, stdin=None) -> ExecutionResult:
    return execute(
        ExecutionRequest(
            command=command,
            cwd=str(cwd),
            timeout_seconds=60,
            env_allowlist=env_allowlist,
            env=env,
            stdin_data=stdin,
        )
    )


def _routes(cwd: Path, *, env_allowlist=None, env=None) -> dict[str, bool]:
    """Try each route: gh, a git credential, git over SSH, curl.

    git over SSH fails either way (the fake ssh speaks no protocol), so
    whether ssh found a credential is read from the probe log instead.
    """
    gh = _run(["gh", "api", "user"], cwd, env_allowlist, env)
    fill = _run(["git", "credential", "fill"], cwd, env_allowlist, env, stdin=_CREDENTIAL_QUERY)
    _run(["git", "ls-remote", "ssh://git@github.invalid/owner/repo.git"], cwd, env_allowlist, env)
    curl = _run(["curl", "-n", "https://api.github.com/user"], cwd, env_allowlist, env)
    return {
        "gh": gh.exit_code == 0,
        "git-credential": fill.exit_code == 0 and FAKE_PASSWORD in fill.stdout,
        "curl": curl.exit_code == 0,
    }


def _ssh_ok(probes: Probes) -> list[bool]:
    return [e["ok"] for e in probes.entries() if e["tool"] == "ssh"]


def test_an_agent_finds_no_credential_route_the_controller_finds_every_one(probes, tmp_path):
    repo = git_repo(tmp_path / "repo")
    # The shared common dir names a helper and an askpass too: the command
    # scope beats it (the preflight would refuse it; this shows the reset).
    _git(repo, "config", "--local", "credential.helper", "probe")
    _git(repo, "config", "--local", "core.askPass", str(probes.bin / "git-credential-probe"))
    _git(repo, "config", "--local", "core.sshCommand", "ssh -i /operator/key")

    # The controller's own calls run in its whole environment.
    assert _routes(repo) == {"gh": True, "git-credential": True, "curl": True}
    assert _ssh_ok(probes) == [True]

    helper_calls = [e for e in probes.entries() if e["tool"] == "git-credential-probe"]
    assert helper_calls

    identity = child_env.operator_identity(read_git_config(str(repo)))
    with isolated_overrides(identity) as overrides:
        agent = _routes(repo, env_allowlist=DEFAULT_ENV_ALLOWLIST, env=overrides)
    assert agent == {"gh": False, "git-credential": False, "curl": False}
    assert _ssh_ok(probes) == [True, False]
    ssh_call = [e for e in probes.entries() if e["tool"] == "ssh"][-1]
    for option in ("IdentityFile=none", "IdentityAgent=none", "BatchMode=yes"):
        assert option in ssh_call["argv"], option
    assert "SSH_AUTH_SOCK" not in ssh_call["env"]
    # Not one helper call came from the child.
    assert [e for e in probes.entries() if e["tool"] == "git-credential-probe"] == helper_calls


def test_a_helper_in_the_worktree_configuration_is_reset_too(probes, tmp_path):
    repo = git_repo(tmp_path / "repo")
    worktree = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "--detach", str(worktree))
    _git(repo, "config", "extensions.worktreeConfig", "true")
    _git(worktree, "config", "--worktree", "credential.https://github.com.helper", "probe")
    with isolated_overrides(LaunchIdentity()) as overrides:
        fill = _run(
            ["git", "credential", "fill"],
            worktree,
            DEFAULT_ENV_ALLOWLIST,
            overrides,
            stdin=_CREDENTIAL_QUERY,
        )
    assert fill.exit_code != 0 and FAKE_PASSWORD not in fill.stdout
    assert not [e for e in probes.entries() if e["tool"] == "git-credential-probe"]


def test_gh_without_the_placeholder_would_reach_the_keychain(probes, tmp_path):
    """Why the placeholder token is set: a gh with neither a token nor a
    hosts.yml asks the keyring, which any same-UID process reaches on macOS."""
    repo = git_repo(tmp_path / "repo")
    with isolated_overrides(LaunchIdentity()) as overrides:
        bare = {k: v for k, v in overrides.items() if not k.endswith("_TOKEN")}
        assert _run(["gh", "auth", "status"], repo, DEFAULT_ENV_ALLOWLIST, bare).exit_code == 0
        assert _run(["gh", "auth", "status"], repo, DEFAULT_ENV_ALLOWLIST, overrides).exit_code


def test_the_controllers_github_client_keeps_its_credentials(probes):
    ok, _detail = GitHubClient(gh_command="gh").auth_status()
    assert ok
    assert [e["ok"] for e in probes.entries() if e["tool"] == "gh"] == [True]


def test_an_agent_adapter_launch_finds_no_credential_route(probes, tmp_path):
    """Through a real adapter (OpenCode's one-shot path), as the engine
    launches it: allow-list plus the adapter's names, overrides on top."""
    from autoforge.config import ProfileConfig
    from autoforge.providers import AgentRequest

    repo = git_repo(tmp_path / "repo")
    report = tmp_path / "agent-report.json"
    agent = tmp_path / "agent-bin" / "opencode"
    agent.parent.mkdir()
    agent.write_text(
        f"#!{sys.executable}\n"
        "import json, subprocess, sys\n"
        "sys.stdin.read()\n"
        "def run(argv, stdin=None):\n"
        "    return subprocess.run(argv, input=stdin, capture_output=True, text=True)\n"
        "fill = run(['git', 'credential', 'fill'], 'protocol=https\\nhost=github.com\\n\\n')\n"
        "out = {\n"
        "  'gh': run(['gh', 'api', 'user']).returncode == 0,\n"
        f"  'git-credential': {FAKE_PASSWORD!r} in fill.stdout,\n"
        "  'curl': run(['curl', '-n', 'https://api.github.com/user']).returncode == 0,\n"
        "}\n"
        f"open({str(report)!r}, 'w').write(json.dumps(out))\n"
        "print('done')\n"
    )
    agent.chmod(0o755)
    profile = ProfileConfig(
        name="review_round_1", provider="opencode", model="openai/x", command=str(agent)
    )
    identity = preflight(str(repo))
    with isolated_overrides(identity) as overrides:
        req = AgentRequest(
            "REVIEW",
            "p",
            str(repo),
            profile,
            None,
            60,
            env_allowlist=DEFAULT_ENV_ALLOWLIST,
            env=overrides,
        )
        assert OpenCodeProvider().execute(req).exit_code == 0
    assert json.loads(report.read_text()) == {"gh": False, "git-credential": False, "curl": False}


# -- the preflight --------------------------------------------------------------


def _problems(repo: Path) -> list[str]:
    return git_config_problems(read_git_config(str(repo)))


@pytest.mark.parametrize(
    ("args", "shape"),
    [
        (["credential.helper", "store"], "credential helper in local-scope"),
        (["credential.https://github.com.helper", "store"], "(credential.<url>.helper)"),
        (
            ["remote.origin.url", "https://x-access-token:s3cr3t@github.com/o/r.git"],
            "remote.origin.url",
        ),
        (
            ["remote.origin.pushurl", f"https://{FAKE_TOKEN}@github.com/o/r.git"],
            "remote.origin.pushurl",
        ),
        (
            ["url.https://u:s3cr3t@github.com/.insteadOf", "https://github.com/"],
            "(url.<url>.insteadof)",
        ),
        (["http.extraHeader", "AUTHORIZATION: basic s3cr3t"], "(http.extraHeader)"),
        (
            ["http.https://github.com/.extraHeader", "AUTHORIZATION: s3cr3t"],
            "(http.<url>.extraHeader)",
        ),
    ],
)
def test_the_preflight_refuses_a_credential_in_the_repository_configuration(tmp_path, args, shape):
    repo = git_repo(tmp_path / "repo")
    _git(repo, "config", "--local", *args)
    with pytest.raises(ConfigurationError) as caught:
        preflight(str(repo))
    message = str(caught.value)
    assert shape in message and "refusing to launch" in message
    assert "s3cr3t" not in message and FAKE_TOKEN not in message
    assert "github.com" not in message  # no URL from a key or a value


def test_a_worktree_scope_helper_is_refused(tmp_path):
    repo = git_repo(tmp_path / "repo")
    _git(repo, "config", "extensions.worktreeConfig", "true")
    _git(repo, "config", "--worktree", "credential.helper", "cache")
    assert _problems(repo) == [
        "a credential helper in worktree-scope git configuration (credential.helper)"
    ]


def test_a_helper_reached_through_an_include_is_refused(tmp_path):
    repo = git_repo(tmp_path / "repo")
    included = tmp_path / "included"
    included.write_text("[credential]\n\thelper = store\n")
    _git(repo, "config", "--local", "include.path", str(included))
    assert _problems(repo) == [
        "a credential helper in local-scope git configuration (credential.helper)"
    ]


@pytest.mark.parametrize(
    "args",
    [
        ["credential.helper", ""],  # a reset, not a helper
        ["remote.origin.url", "git@github.com:o/r.git"],
        ["remote.origin.url", "https://github.com/o/r.git"],
        ["remote.origin.url", "https://octocat@github.com/o/r.git"],
        ["remote.origin.url", "ssh://git@github.com/o/r.git"],
        ["url.git@github.com:.insteadOf", "https://github.com/"],
        ["credential.username", "octocat"],
    ],
)
def test_the_preflight_accepts_configuration_that_carries_no_credential(tmp_path, args):
    repo = git_repo(tmp_path / "repo")
    _git(repo, "config", "--local", *args)
    assert _problems(repo) == []
    preflight(str(repo))


def test_a_global_helper_is_not_refused_because_the_child_never_reads_it(tmp_path, monkeypatch):
    config = tmp_path / "global"
    config.write_text("[credential]\n\thelper = osxkeychain\n[user]\n\tname = Op\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    assert preflight(str(git_repo(tmp_path / "repo"))).entries == (("user.name", "Op"),)


def test_a_configuration_git_cannot_read_refuses_the_launch(tmp_path):
    repo = git_repo(tmp_path / "repo")
    (repo / ".git" / "config").write_text("[broken\n")
    with pytest.raises(ConfigurationError, match="cannot read the git configuration"):
        preflight(str(repo))


def test_an_output_the_parser_does_not_understand_refuses_the_launch(tmp_path):
    def runner(req):
        return ExecutionResult(req.command, req.cwd, 0, "local\0no-terminator", "", "t", "t")

    with pytest.raises(ConfigurationError, match="unterminated"):
        preflight(str(tmp_path), runner)


def test_the_listing_parser_keeps_scopes_keys_and_values():
    out = "global\0user.name\nA B\0local\0core.bare\nfalse\0command\0x.flag\0"
    assert parse_git_config_listing(out) == [
        GitConfigEntry("global", "user.name", "A B"),
        GitConfigEntry("local", "core.bare", "false"),
        GitConfigEntry("command", "x.flag", None),
    ]
    for bad in ("weird\0a.b\nc\0", "local\0", "local\0\nv\0"):
        with pytest.raises(ExecutionError):
            parse_git_config_listing(bad)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/o/r", False),
        ("https://user@github.com/o/r", False),
        ("https://user:pw@github.com/o/r", True),
        (f"https://{FAKE_TOKEN}@github.com/o/r", True),
        ("ssh://git:pw@github.com/o/r", True),
        ("ssh://git@github.com/o/r", False),
        ("git@github.com:o/r.git", False),
        ("/srv/repo.git", False),
    ],
)
def test_url_has_credential(url, expected):
    assert url_has_credential(url) is expected


# -- ~/.netrc -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "machine github.com login x password s3cr3t\n",
        "machine api.github.com\n  login x\n  password s3cr3t\n",
        "machine example.org login a password b\ndefault login x password s3cr3t\n",
        "machine\nGitHub.com login x password s3cr3t\n",
    ],
)
def test_a_netrc_entry_for_github_refuses_the_launch(tmp_path, monkeypatch, text):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".netrc").write_text(text)
    monkeypatch.setattr(child_env, "operator_home", lambda: str(home))
    with pytest.raises(ConfigurationError) as caught:
        preflight(str(git_repo(tmp_path / "repo")))
    assert "~/.netrc" in str(caught.value) and "s3cr3t" not in str(caught.value)


def test_a_netrc_without_github_and_a_macro_body_mentioning_it_are_accepted(tmp_path):
    (tmp_path / ".netrc").write_text(
        "machine example.org login a password b\n"
        "macdef init\nmachine github.com login x password y\n\n"
        "machine gitlab.com login c password d\n"
    )
    assert netrc_problems(str(tmp_path)) == []


def test_the_windows_name_is_checked_too(tmp_path):
    (tmp_path / "_netrc").write_text("machine github.com password y\n")
    assert netrc_problems(str(tmp_path)) == ["an entry for GitHub (or a default entry) in ~/_netrc"]
    assert netrc_problems(None) == []
