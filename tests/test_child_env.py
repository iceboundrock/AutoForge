"""The environment of every process AutoForge launches outside itself (#165).

Real git and the real executor throughout; ``gh``, ``ssh``, ``curl``,
``wget`` and a git credential helper are PATH-injected fakes that succeed
only when they find a credential route, so each test shows the route closed
for a child and open for the controller's own calls. The real ``curl`` and
``wget``, where installed, are run against a server on the loopback
interface. No other network, and no real credential: every "token" here is
a fixed fake.
"""

from __future__ import annotations

import base64
import http.server
import json
import netrc
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from autoforge import child_env
from autoforge.child_env import (
    FORBIDDEN_ENV_NAMES,
    GH_PLACEHOLDER_TOKEN,
    HTTP_CLIENT_CONFIG,
    POSIX_SHELLS,
    SHELL_PROBE_MARKER,
    SSH_COMMAND,
    GitConfigEntry,
    LaunchIdentity,
    child_overrides,
    child_shell,
    forbidden_env_entry,
    git_config_problems,
    isolated_overrides,
    launch_shells,
    netrc_problems,
    override_names,
    parse_git_config_listing,
    preflight,
    probe_findings,
    read_git_config,
    render_git_config,
    shell_probe_script,
    shell_startup_problems,
    url_has_credential,
)
from autoforge.config import DEFAULT_ENV_ALLOWLIST, ProfileConfig
from autoforge.errors import ConfigurationError, ExecutionError
from autoforge.executor import ExecutionRequest, ExecutionResult, execute, select_environment
from autoforge.github import GitHubClient
from autoforge.providers import AgentRequest, ClaudeCodeProvider, OpenCodeProvider, PiProvider
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
    assert forbidden_env_entry("NET*") == ("NETRC", FORBIDDEN_ENV_NAMES["NETRC"])
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
    # A home of the test's own, so nothing reads the developer's dotfiles;
    # its curl and wget configuration add the token to every request.
    home = tmp_path / "home"
    home.mkdir()
    (home / ".curlrc").write_text(f'header = "Authorization: Bearer {FAKE_TOKEN}"\n')
    (home / ".wgetrc").write_text(f"header = Authorization: Bearer {FAKE_TOKEN}\n")
    monkeypatch.setenv("HOME", str(home))
    for name in ("XDG_CONFIG_HOME", "CURL_HOME", "WGETRC", "WGET2RC"):
        monkeypatch.delenv(name, raising=False)
    # A .netrc outside the home, which curl 8.16 and later, requests and Go
    # read in place of ~/.netrc.
    (tmp_path / "operator-netrc").write_text(
        f"machine github.com login x password {FAKE_PASSWORD}\n"
    )
    monkeypatch.setenv("NETRC", str(tmp_path / "operator-netrc"))
    # Startup files a shell would run before every command, each restoring
    # the token, and a shell that reads its configuration on every start.
    startup = tmp_path / "operator-startup"
    startup.mkdir()
    for name in ("bash-env", "env", ".zshenv"):
        (startup / name).write_text(f"export GH_TOKEN={FAKE_TOKEN}\n")
    monkeypatch.setenv("BASH_ENV", str(startup / "bash-env"))
    monkeypatch.setenv("ENV", str(startup / "env"))
    monkeypatch.setenv("ZDOTDIR", str(startup))
    monkeypatch.setenv("SHELL", "/usr/bin/fish")
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
        # curl and wget find a file of the controller's that sets nothing.
        assert (Path(env["CURL_HOME"]) / ".curlrc").read_text() == HTTP_CLIENT_CONFIG
        assert env["WGETRC"] == env["WGET2RC"]
        assert Path(env["WGETRC"]).read_text() == HTTP_CLIENT_CONFIG
        # No shell startup file of the operator's: zsh looks in an empty
        # directory, BASH_ENV and ENV are gone, and fish is not the shell.
        zdotdir = Path(env["ZDOTDIR"])
        assert zdotdir.is_dir() and list(zdotdir.iterdir()) == []
        assert "BASH_ENV" not in env and "ENV" not in env
        assert env["SHELL"] == child_shell() and Path(env["SHELL"]).name in POSIX_SHELLS
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
    env = child_overrides("/gh", "/gitconfig", "/curl", "/wgetrc", "/zsh", "/bin/sh")
    assert tuple(sorted(env)) == override_names()
    assert env["CURL_HOME"] == "/curl" and env["WGETRC"] == env["WGET2RC"] == "/wgetrc"
    assert env["ZDOTDIR"] == "/zsh" and env["SHELL"] == "/bin/sh"
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
PASSWD_HOME = {passwd_home!r}
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


def header(path):
    return "Authorization" in Path(path).read_text()


def curl():
    if token("GH_TOKEN", "GITHUB_TOKEN"):
        return True
    # curl reads the first default configuration file that exists, in this
    # order (the last is the password database's home, not HOME), unless
    # -q comes first.
    if args[:1] not in (["-q"], ["--disable"]):
        xdg = env.get("XDG_CONFIG_HOME")
        candidates = [
            env.get("CURL_HOME") and os.path.join(env["CURL_HOME"], ".curlrc"),
            # curl 8.18 reads curlrc there, 8.5 .curlrc.
            xdg and os.path.join(xdg, "curlrc"),
            xdg and os.path.join(xdg, ".curlrc"),
            env.get("HOME") and os.path.join(env["HOME"], ".curlrc"),
            os.path.join(PASSWD_HOME, ".curlrc"),
        ]
        found = next((c for c in candidates if c and os.path.exists(c)), None)
        if found and header(found):
            return True
    if "-n" in args or "--netrc" in args:
        # curl 8.16 and later read the file NETRC names before ~/.netrc.
        netrc = Path(env.get("NETRC") or os.path.join(env.get("HOME", "/"), ".netrc"))
        return netrc.exists() and "github.com" in netrc.read_text()
    return False


def wget():
    # wget 1.x reads the file WGETRC names, else ~/.wgetrc; wget2 the file
    # WGET2RC names, else its XDG location. Either fake reads one file.
    for variable, default in (
        ("WGETRC", os.path.join(env.get("HOME", "/"), ".wgetrc")),
        ("WGET2RC", os.path.join(
            env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", "/"), ".config"),
            "wget", "wget2rc",
        )),
    ):
        path = env.get(variable) or default
        if os.path.exists(path) and header(path):
            return True
    return False


def helper():
    # A git credential helper: answers `get` with a password.
    if args[-1:] == ["get"]:
        sys.stdin.read()
        print("username=x-access-token")
        print("password={password}")
    return True


ok = {{"gh": gh, "ssh": ssh, "curl": curl, "wget": wget, "git-credential-probe": helper}}[TOOL]()
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
        for tool in ("gh", "ssh", "curl", "wget", "git-credential-probe"):
            script = self.bin / tool
            script.write_text(
                f"#!{sys.executable}\n"
                + _PROBE.format(
                    tool=tool,
                    log=str(self.log),
                    keychain=str(self.keychain),
                    default_key=str(self.default_key),
                    passwd_home=str(self.default_key.parent.parent),
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
    """Try each route: gh, a git credential, git over SSH, curl with the
    netrc, and a plain curl and wget (their configuration files).

    git over SSH fails either way (the fake ssh speaks no protocol), so
    whether ssh found a credential is read from the probe log instead.
    """
    gh = _run(["gh", "api", "user"], cwd, env_allowlist, env)
    fill = _run(["git", "credential", "fill"], cwd, env_allowlist, env, stdin=_CREDENTIAL_QUERY)
    _run(["git", "ls-remote", "ssh://git@github.invalid/owner/repo.git"], cwd, env_allowlist, env)
    curl = _run(["curl", "-n", "https://api.github.com/user"], cwd, env_allowlist, env)
    plain_curl = _run(["curl", "https://api.github.com/user"], cwd, env_allowlist, env)
    plain_wget = _run(["wget", "https://api.github.com/user"], cwd, env_allowlist, env)
    return {
        "gh": gh.exit_code == 0,
        "git-credential": fill.exit_code == 0 and FAKE_PASSWORD in fill.stdout,
        "curl": curl.exit_code == 0,
        "curl-default": plain_curl.exit_code == 0,
        "wget-default": plain_wget.exit_code == 0,
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
    assert all(_routes(repo).values())
    assert _ssh_ok(probes) == [True]

    helper_calls = [e for e in probes.entries() if e["tool"] == "git-credential-probe"]
    assert helper_calls

    identity = child_env.operator_identity(read_git_config(str(repo)))
    with isolated_overrides(identity) as overrides:
        agent = _routes(repo, env_allowlist=DEFAULT_ENV_ALLOWLIST, env=overrides)
    assert agent == {
        "gh": False,
        "git-credential": False,
        "curl": False,
        "curl-default": False,
        "wget-default": False,
    }
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
        "  'curl-default': run(['curl', 'https://api.github.com/user']).returncode == 0,\n"
        "  'wget-default': run(['wget', 'https://api.github.com/user']).returncode == 0,\n"
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
    assert json.loads(report.read_text()) == {
        "gh": False,
        "git-credential": False,
        "curl": False,
        "curl-default": False,
        "wget-default": False,
    }


# -- the real curl and wget, against the loopback interface ----------------------


class _HeaderRecorder(http.server.BaseHTTPRequestHandler):
    """Answers every GET and records the ``Authorization`` header it carried."""

    def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
        self.server.seen.append(self.headers.get("Authorization"))  # type: ignore[attr-defined]
        body = b"ok\n"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def loopback():
    server = http.server.HTTPServer(("127.0.0.1", 0), _HeaderRecorder)
    server.seen = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _seen_header(server, command, tmp_path, *, isolated: bool) -> str | None:
    """Run ``command`` (``{url}`` is the server's) as the controller would
    (its whole environment) or as a launch would; the header the server got."""
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    argv = [arg.replace("{url}", url) for arg in command]
    if isolated:
        with isolated_overrides(LaunchIdentity()) as overrides:
            result = _run(argv, tmp_path, DEFAULT_ENV_ALLOWLIST, overrides)
    else:
        result = _run(argv, tmp_path)
    assert result.exit_code == 0, result.stderr
    return server.seen.pop()


_REAL_CURL = ["curl", "-s", "--noproxy", "*", "--max-time", "30", "{url}"]
_REAL_WGET = ["wget", "-q", "-O", "-", "--no-proxy", "--timeout=30", "--tries=1", "{url}"]


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")
@pytest.mark.parametrize("where", ["home", "xdg", "curl_home"])
def test_a_launchs_plain_curl_sends_no_header_of_the_operators_configuration(
    where, tmp_path, monkeypatch, loopback
):
    """A default request of the real curl: the operator's ``~/.curlrc``,
    the one in ``$XDG_CONFIG_HOME`` or ``$CURL_HOME/.curlrc`` adds the header
    for the controller, and none reaches the server from a launch, although
    the child keeps ``HOME`` and ``XDG_CONFIG_HOME``. The file in
    ``$XDG_CONFIG_HOME`` is ``curlrc`` for curl 8.18 and ``.curlrc`` for 8.5
    (CI's), so both names are written."""
    home, xdg, curl_home = (tmp_path / name for name in ("home", "xdg", "curl-home"))
    for directory in (home, xdg, curl_home):
        directory.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("CURL_HOME", raising=False)
    line = f'header = "Authorization: Bearer {FAKE_TOKEN}"\n'
    if where == "home":
        (home / ".curlrc").write_text(line)
    elif where == "xdg":
        (xdg / "curlrc").write_text(line)
        (xdg / ".curlrc").write_text(line)
    else:
        (curl_home / ".curlrc").write_text(line)
        monkeypatch.setenv("CURL_HOME", str(curl_home))
    assert _seen_header(loopback, _REAL_CURL, tmp_path, isolated=False) == f"Bearer {FAKE_TOKEN}"
    assert _seen_header(loopback, _REAL_CURL, tmp_path, isolated=True) is None


@pytest.mark.skipif(shutil.which("wget") is None, reason="wget is not installed")
def test_a_launchs_plain_wget_sends_no_header_of_the_operators_configuration(
    tmp_path, monkeypatch, loopback
):
    """The same for the real wget, 1.x (``~/.wgetrc``) or wget2
    (``~/.config/wget/wget2rc``): both files are written, each reads its own."""
    home = tmp_path / "home"
    (home / ".config" / "wget").mkdir(parents=True)
    line = f"header = Authorization: Bearer {FAKE_TOKEN}\n"
    (home / ".wgetrc").write_text(line)
    (home / ".config" / "wget" / "wget2rc").write_text(line)
    monkeypatch.setenv("HOME", str(home))
    for name in ("XDG_CONFIG_HOME", "WGETRC", "WGET2RC"):
        monkeypatch.delenv(name, raising=False)
    assert _seen_header(loopback, _REAL_WGET, tmp_path, isolated=False) == f"Bearer {FAKE_TOKEN}"
    assert _seen_header(loopback, _REAL_WGET, tmp_path, isolated=True) is None


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
        "machine github.com. login x password s3cr3t\n",
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


# netrc texts a real reader takes a GitHub credential from although no word
# of them is a bare ``machine github.com``, and the reader that does: curl
# (7.84 and later) quotes, unescapes inside quotes and matches keywords in
# any case; Python's ``netrc`` (``requests``) unescapes everywhere and reads
# a ``macdef`` given as a login as the login.
_DISGUISED_NETRC = [
    pytest.param('machine "github.com" login x password {pw}\n', "curl", id="quoted"),
    pytest.param('machine "git\\hub.com" login x password {pw}\n', "curl", id="escaped-quoted"),
    pytest.param("MACHINE github.com LOGIN x PASSWORD {pw}\n", "curl", id="upper-case"),
    pytest.param(
        "machine example.org login a password b\nDEFAULT login x password {pw}\n",
        "curl",
        id="upper-case-default",
    ),
    pytest.param("machine git\\hub.com login x password {pw}\n", "python", id="escaped"),
    pytest.param(
        "machine example.org login macdef password b\nmachine github.com login x password {pw}\n",
        "python",
        id="macdef-as-a-login",
    ),
]


@pytest.mark.parametrize(("text", "reader"), _DISGUISED_NETRC)
def test_a_netrc_entry_for_github_in_any_readers_syntax_refuses_the_launch(
    tmp_path, monkeypatch, text, reader
):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".netrc").write_text(text.format(pw=FAKE_PASSWORD))
    monkeypatch.setattr(child_env, "operator_home", lambda: str(home))
    with pytest.raises(ConfigurationError) as caught:
        preflight(str(git_repo(tmp_path / "repo")))
    message = str(caught.value)
    assert "an entry for GitHub (or a default entry) in ~/.netrc" in message
    assert FAKE_PASSWORD not in message


def _curl_version() -> tuple[int, ...] | None:
    found = re.match(
        r"curl (\d+)\.(\d+)\.(\d+)",
        subprocess.run(["curl", "--version"], capture_output=True, text=True).stdout,
    )
    return tuple(int(part) for part in found.groups()) if found else None


def _netrc_header(server, home: Path, env: dict[str, str] | None = None) -> str | None:
    """The header the real curl sends for ``github.com``, resolved to the
    loopback server, with ``home`` as ``HOME`` (and ``env`` over it), when it
    reads a netrc: ``--netrc-optional`` is ``-n`` that still sends the
    request when there is no file (curl 8.18 refuses ``-n`` without one)."""
    port = server.server_address[1]
    argv = [
        "curl", "-s", "--netrc-optional", "--noproxy", "*", "--max-time", "30",
        "--resolve", f"github.com:{port}:127.0.0.1", f"http://github.com:{port}/",
    ]  # fmt: skip
    result = _run(argv, home, ("PATH",), {"HOME": str(home), **(env or {})})
    assert result.exit_code == 0, result.stderr
    return server.seen.pop()


@pytest.mark.parametrize(("text", "reader"), _DISGUISED_NETRC)
def test_the_disguised_netrc_entries_are_ones_a_real_reader_uses(tmp_path, loopback, text, reader):
    """The texts above are not hypothetical: the real ``curl -n`` sends their
    credential to ``github.com`` (here the loopback server), or Python's
    ``netrc`` module returns it, and the preflight refuses each."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".netrc").write_text(text.format(pw=FAKE_PASSWORD))
    expected = ("x", "", FAKE_PASSWORD)
    if reader == "python":
        assert netrc.netrc(str(home / ".netrc")).authenticators("github.com") == expected
    else:
        version = shutil.which("curl") and _curl_version()
        if not version or version < (7, 84, 0):
            pytest.skip("needs curl 7.84 or later, which reads quoted netrc words")
        basic = base64.b64encode(f"x:{FAKE_PASSWORD}".encode()).decode()
        assert _netrc_header(loopback, home) == f"Basic {basic}"
    assert netrc_problems(str(home)) == ["an entry for GitHub (or a default entry) in ~/.netrc"]


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")
def test_a_launchs_curl_reads_no_netrc_the_operators_netrc_variable_names(
    tmp_path, monkeypatch, loopback
):
    """curl 8.16 and later read the file ``NETRC`` names instead of
    ``~/.netrc``: the operator's sends the credential for the controller and
    the preflight, which reads ``~/.netrc``, finds nothing; a launch never
    inherits ``NETRC`` (and no allow-list may name it), so its curl sends
    none. The same holds for requests and Go, which read ``NETRC`` too."""
    version = _curl_version()
    if not version or version < (8, 16, 0):
        pytest.skip("needs curl 8.16 or later, which reads NETRC")
    home = tmp_path / "home"
    home.mkdir()
    alternate = tmp_path / "alternate-netrc"
    alternate.write_text(f"machine github.com login x password {FAKE_PASSWORD}\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NETRC", str(alternate))
    monkeypatch.setattr(child_env, "operator_home", lambda: str(home))
    basic = base64.b64encode(f"x:{FAKE_PASSWORD}".encode()).decode()
    assert _netrc_header(loopback, home, {"NETRC": str(alternate)}) == f"Basic {basic}"
    identity = preflight(str(git_repo(tmp_path / "repo")))
    with isolated_overrides(identity) as overrides:
        env = _child_env(overrides)
        assert "NETRC" not in env
        assert _netrc_header(loopback, home, env) is None
    with pytest.raises(ConfigurationError, match="'NETRC'"):
        child_env.refuse_forbidden_env(["NETRC"], "execution.env_allowlist_extra")


def test_a_netrc_without_github_is_accepted(tmp_path):
    (tmp_path / ".netrc").write_text(
        "machine example.org login a password b\n"
        "machine github.example.com login e password f\n"
        "machine notgithub.com login g password h\n"
        '# a comment\nmachine "gitlab.com" login c password "d e"\n'
    )
    assert netrc_problems(str(tmp_path)) == []


def test_a_macro_body_or_a_comment_naming_github_refuses_too(tmp_path):
    """Readers disagree on where a ``macdef`` body or a comment ends
    (``macdef-as-a-login`` above), so neither is skipped: a word naming
    GitHub there refuses the file, though no reader may use it."""
    for text in (
        "macdef init\nmachine github.com login x password y\n\n",
        "# machine github.com login x password y\n",
    ):
        (tmp_path / ".netrc").write_text(text)
        assert netrc_problems(str(tmp_path)) != [], text


def test_the_windows_name_is_checked_too(tmp_path):
    (tmp_path / "_netrc").write_text("machine github.com password y\n")
    assert netrc_problems(str(tmp_path)) == ["an entry for GitHub (or a default entry) in ~/_netrc"]
    assert netrc_problems(None) == []


# -- shell startup files --------------------------------------------------------

_ZSH = shutil.which("zsh")
_BASH = shutil.which("bash")
needs_zsh = pytest.mark.skipif(_ZSH is None, reason="zsh is not installed")
needs_bash = pytest.mark.skipif(_BASH is None, reason="bash is not installed")


def _merges(probes: Probes) -> list[bool]:
    """Whether each ``gh`` the fakes saw found a token (a merge would land)."""
    return [e["ok"] for e in probes.entries() if e["tool"] == "gh"]


def _startup_home(tmp_path: Path, files: dict[str, str]) -> Path:
    home = tmp_path / "startup-home"
    home.mkdir()
    for name, text in files.items():
        (home / name).write_text(text)
    return home


@needs_zsh
def test_a_launchs_zsh_reads_no_startup_file_of_the_operators(probes, tmp_path):
    """zsh reads ``.zshenv`` on every start, ``zsh -c`` and a script with a
    zsh shebang included, from ``ZDOTDIR`` or else ``HOME``; an ``export
    GH_TOKEN`` there replaces the placeholder for everything the shell runs.
    A launch's zsh finds none of the operator's files in any mode; the same
    launch environment without the controller's ``ZDOTDIR`` hands ``gh`` the
    token, which is the route."""
    home = Path(os.environ["HOME"])
    assert home.is_relative_to(tmp_path)  # the fixture's, never the developer's
    for name in (".zshenv", ".zprofile", ".zshrc", ".zlogin"):
        (home / name).write_text(f"export GH_TOKEN={FAKE_TOKEN}\n")
    script = tmp_path / "check"
    script.write_text(f"#!{_ZSH}\ngh pr merge 1 --squash\n")
    script.chmod(0o755)
    with isolated_overrides(LaunchIdentity()) as overrides:
        for mode in (["-c"], ["-l", "-c"], ["-i", "-c"]):
            command = [_ZSH, *mode, "gh pr merge 1 --squash"]
            assert _run(command, tmp_path, DEFAULT_ENV_ALLOWLIST, overrides).exit_code != 0, mode
        assert _run([str(script)], tmp_path, DEFAULT_ENV_ALLOWLIST, overrides).exit_code != 0
        assert _merges(probes) == [False] * 4
        without = {k: v for k, v in overrides.items() if k != "ZDOTDIR"}
        command = [_ZSH, "-c", "gh pr merge 1 --squash"]
        assert _run(command, tmp_path, DEFAULT_ENV_ALLOWLIST, without).exit_code == 0
    assert _merges(probes)[-1] is True


@needs_bash
def test_a_launchs_bash_and_sh_run_no_startup_file_of_the_operators_for_a_command(probes, tmp_path):
    """Run for a command, bash reads only the file ``BASH_ENV`` names and sh
    none (``ENV`` is an interactive shell's): a launch inherits neither, and
    no allow-list may name one, so the operator's ``BASH_ENV``, which
    exports the token, is never run; with it, ``bash -c`` would merge."""
    shells = [_BASH, shutil.which("sh") or "/bin/sh", child_shell()]
    with isolated_overrides(LaunchIdentity()) as overrides:
        for shell in shells:
            command = [shell, "-c", "gh pr merge 1 --squash"]
            assert _run(command, tmp_path, DEFAULT_ENV_ALLOWLIST, overrides).exit_code != 0
        assert _merges(probes) == [False] * len(shells)
        leaky = {**overrides, "BASH_ENV": os.environ["BASH_ENV"]}
        command = [_BASH, "-c", "gh pr merge 1 --squash"]
        assert _run(command, tmp_path, DEFAULT_ENV_ALLOWLIST, leaky).exit_code == 0
    assert _merges(probes)[-1] is True
    for name in ("BASH_ENV", "ENV", "ZDOTDIR"):
        with pytest.raises(ConfigurationError, match=f"'{name}'"):
            child_env.refuse_forbidden_env([name], "execution.env_allowlist_extra")


# OpenCode 2.0.26's shell tool, as read from the build: ``$SHELL`` when it
# names a shell it accepts (it refuses fish and nu), else the ``bash`` on
# ``PATH``, else ``/bin/sh``, run as ``<shell> -c <command>`` with nothing
# that suppresses startup files.
_SHELL_TOOL_OPENCODE = r"""
import json, os, shutil, subprocess, sys

sys.stdin.read()
shell = os.environ.get("SHELL", "")
if not (os.path.isabs(shell) and os.path.isfile(shell)) or os.path.basename(shell) in (
    "fish",
    "nu",
):
    shell = shutil.which("bash") or "/bin/sh"
merge = subprocess.run([shell, "-c", "gh pr merge 1 --squash"])
print(json.dumps({"shell": shell, "merge_exit": merge.returncode}))
"""


@needs_zsh
def test_an_opencode_shell_tool_merges_nothing_through_the_operators_zshenv(
    probes, tmp_path, monkeypatch
):
    """R3-F1 as reported: the operator's shell is zsh and their ``~/.zshenv``
    exports their token. Through the real OpenCode adapter and executor,
    preflight included, the ``gh pr merge`` the agent's shell tool runs finds
    the placeholder and is refused; the same launch with zsh finding the
    operator's ``~/.zshenv`` (no ``ZDOTDIR``) would merge."""
    monkeypatch.setenv("SHELL", _ZSH)
    home = Path(os.environ["HOME"])
    assert home.is_relative_to(tmp_path)  # the fixture's, never the developer's
    (home / ".zshenv").write_text(f"export GH_TOKEN={FAKE_TOKEN}\n")
    fake = tmp_path / "opencode"
    fake.write_text(f"#!{sys.executable}\n{_SHELL_TOOL_OPENCODE}")
    fake.chmod(0o755)
    profile = ProfileConfig(
        name="analyze_execute", provider="opencode", model="openai/x", command=str(fake)
    )
    repo = git_repo(tmp_path / "repo")
    # The preflight probes this home and passes: a launch's zsh reads none of it.
    identity = preflight(str(repo), home=str(home), env_allowlist=DEFAULT_ENV_ALLOWLIST)
    with isolated_overrides(identity) as overrides:
        assert overrides["SHELL"] == _ZSH
        req = AgentRequest(
            "ANALYZE_EXECUTE",
            "p",
            str(repo),
            profile,
            None,
            60,
            env_allowlist=DEFAULT_ENV_ALLOWLIST,
            env=overrides,
        )
        result = OpenCodeProvider().execute(req)
        assert result.exit_code == 0, result.stderr
        assert json.loads(result.stdout.splitlines()[-1]) == {"shell": _ZSH, "merge_exit": 1}
        req.env = {k: v for k, v in overrides.items() if k != "ZDOTDIR"}
        leaked = OpenCodeProvider().execute(req)
        assert json.loads(leaked.stdout.splitlines()[-1]) == {"shell": _ZSH, "merge_exit": 0}
    assert _merges(probes) == [False, True]


def test_a_launchs_shell_is_one_that_reads_no_startup_file(tmp_path):
    fallback = shutil.which("bash") or "/bin/sh"
    not_executable = tmp_path / "zsh"
    not_executable.write_text("")
    for operator in (
        "/usr/bin/fish",
        "/bin/tcsh",
        "zsh",
        str(tmp_path / "missing" / "bash"),
        str(not_executable),
        str(tmp_path),
        "",
    ):
        assert child_shell(operator) == fallback, operator
    sh = shutil.which("sh")
    assert sh and child_shell(sh) == sh
    assert os.path.basename(child_shell()) in POSIX_SHELLS


def test_the_probe_names_what_changed_and_never_a_value():
    """The probe compares, in a process the shell starts, the environment it
    exports with the launch's: a controller-set variable changed, unset or
    no longer exported, and a withheld name set again, are named; a
    tolerated one is not; no value is printed."""
    overrides = child_overrides("/gh", "/g", "/c", "/w", "/z", "/bin/sh")
    script = shell_probe_script(overrides)
    names = set(overrides) | set(FORBIDDEN_ENV_NAMES)

    def probe(env, prefix=""):
        shell = _BASH if prefix else "/bin/sh"
        out = subprocess.run(
            [shell, "-c", prefix + script], env=env, capture_output=True, text=True, check=True
        ).stdout
        return probe_findings(out, names), out

    clean = {"PATH": os.environ["PATH"], **overrides}
    assert probe(clean)[0] == []
    changed = {
        **clean,
        "GH_TOKEN": FAKE_TOKEN,
        "GIT_ASKPASS": "/bin/echo",
        "BASH_ENV": "/home/x/.bashrc",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "XDG_RUNTIME_DIR": "/run/user/1",
    }
    del changed["CURL_HOME"]
    findings, out = probe(changed)
    assert findings == ["BASH_ENV", "CURL_HOME", "GH_TOKEN", "GIT_ASKPASS"]
    assert FAKE_TOKEN not in out and "/bin/echo" not in out
    if _BASH:  # set, but no longer exported: nothing the shell starts gets it
        assert probe(clean, "export -n GH_TOKEN\n")[0] == ["GH_TOKEN"]
    # What a startup file prints before the marker is never a finding.
    out = (
        f"GITHUB_TOKEN\n\n{SHELL_PROBE_MARKER}\nGH_TOKEN\n\n{SHELL_PROBE_MARKER}\nGIT_SSH\nnoise\n"
    )
    assert probe_findings(out, ["GH_TOKEN", "GIT_SSH", "GITHUB_TOKEN"]) == ["GIT_SSH"]
    assert probe_findings("no marker\nGH_TOKEN\n", ["GH_TOKEN"]) is None


def test_a_home_without_startup_files_has_no_startup_problem(tmp_path):
    home = _startup_home(tmp_path, {})
    assert shell_startup_problems(str(tmp_path), DEFAULT_ENV_ALLOWLIST, home=str(home)) == []


@needs_bash
def test_a_login_startup_file_that_restores_a_route_is_named_never_its_value(tmp_path, monkeypatch):
    """``bash -l`` reads ``~/.bash_profile``, ``sh -l`` ``~/.profile``, both
    through ``HOME``: each change to the launch's isolation is named with
    its shell, whether it adds a withheld name, unsets a controller-set one
    or redirects where another shell looks for its startup files."""
    monkeypatch.setenv("SHELL", _BASH)
    home = _startup_home(
        tmp_path,
        {
            ".profile": f"export GITHUB_TOKEN={FAKE_TOKEN}\n",
            ".bash_profile": (
                'unset GH_TOKEN\nexport GIT_ASKPASS=/bin/echo\nexport ZDOTDIR="$HOME"\n'
                'export BASH_ENV="$HOME/.bashrc"\n'
            ),
        },
    )
    problems = shell_startup_problems(str(tmp_path), DEFAULT_ENV_ALLOWLIST, home=str(home))
    sh = shutil.which("sh")
    assert f"the startup files of a login sh ({sh}), which change GITHUB_TOKEN" in problems
    assert (
        f"the startup files of a login bash ({_BASH}), which change "
        "BASH_ENV, GH_TOKEN, GIT_ASKPASS, ZDOTDIR"
    ) in problems
    assert FAKE_TOKEN not in json.dumps(problems)


def test_what_is_withheld_as_defence_in_depth_alone_is_no_startup_problem(tmp_path):
    """An SSH agent a profile starts, a runtime directory or a session bus it
    exports, a default host: none gives git or gh a credential while the
    probed variables stand (``IdentityAgent=none``, the placeholder)."""
    home = _startup_home(
        tmp_path,
        {
            ".profile": "export SSH_AUTH_SOCK=/tmp/agent.sock XDG_RUNTIME_DIR=/run/user/1 "
            "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1/bus GH_HOST=github.com\n",
        },
    )
    (home / ".bash_profile").write_text(". ~/.profile\n")
    assert shell_startup_problems(str(tmp_path), DEFAULT_ENV_ALLOWLIST, home=str(home)) == []


@needs_zsh
def test_zsh_startup_files_in_the_home_are_no_problem(tmp_path, monkeypatch):
    """A launch's zsh reads its startup files from the controller's empty
    ``ZDOTDIR``: the operator's are no route, so nothing is refused."""
    monkeypatch.setenv("SHELL", _ZSH)
    names = (".zshenv", ".zprofile", ".zshrc", ".zlogin")
    home = _startup_home(tmp_path, {n: f"export GITHUB_TOKEN={FAKE_TOKEN}\n" for n in names})
    assert shell_startup_problems(str(tmp_path), DEFAULT_ENV_ALLOWLIST, home=str(home)) == []


@needs_bash
def test_a_startup_file_that_ends_the_shell_could_not_be_checked(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL", _BASH)
    home = _startup_home(tmp_path, {".bash_profile": "exit 3\n"})
    problems = shell_startup_problems(str(tmp_path), DEFAULT_ENV_ALLOWLIST, home=str(home))
    assert f"the startup files of a login bash ({_BASH}), which could not be checked (exit 3)" in (
        problems
    )


def test_the_probe_runs_each_launch_shell_as_a_login_shell_in_the_launch_environment(tmp_path):
    seen: list[ExecutionRequest] = []

    def runner(req):
        seen.append(req)
        return ExecutionResult(
            req.command, req.cwd, 0, f"motd\n\n{SHELL_PROBE_MARKER}\n", "", "t", "t"
        )

    problems = shell_startup_problems(str(tmp_path), ("PATH", "HOME"), runner, home="/op/home")
    assert problems == []
    assert [req.command[0] for req in seen] == launch_shells(child_shell())
    for req in seen:
        assert req.command[1:3] == ["-l", "-c"] and req.cwd == str(tmp_path)
        assert req.env_allowlist == ("PATH", "HOME") and req.contain_orphans
        assert req.env["HOME"] == "/op/home" and Path(req.env["ZDOTDIR"]).name == "zsh"
        overrides = {name: req.env[name] for name in override_names()}
        assert req.command[3] == shell_probe_script(overrides)


def _result(code=0, stdout=f"\n{SHELL_PROBE_MARKER}\n", **kwargs) -> ExecutionResult:
    return ExecutionResult(["sh"], "/", code, stdout, "", "t", "t", **kwargs)


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (_result(code=1), "exit 1"),
        (_result(code=127, stdout=""), "exit 127"),
        (_result(stdout="motd only\n"), "no result"),
        (_result(stdout_truncated=True), "no result"),
        (_result(code=-9, timed_out=True), "timed out after 60s"),
        (ExecutionError("executable not found"), "ExecutionError"),
    ],
)
def test_a_probe_without_an_answer_could_not_be_checked(tmp_path, answer, reason):
    def runner(req):
        if isinstance(answer, Exception):
            raise answer
        return answer

    problems = shell_startup_problems(str(tmp_path), ("PATH",), runner, home=str(tmp_path))
    assert problems and all(p.endswith(f"which could not be checked ({reason})") for p in problems)


def test_the_preflight_runs_the_shells_only_for_a_launchs_allow_list(tmp_path):
    repo = git_repo(tmp_path / "repo")
    home = _startup_home(tmp_path, {".profile": f"export GITHUB_TOKEN={FAKE_TOKEN}\n"})
    (home / ".bash_profile").write_text(". ~/.profile\n")
    assert isinstance(preflight(str(repo), home=str(home)), LaunchIdentity)
    with pytest.raises(ConfigurationError) as caught:
        preflight(str(repo), home=str(home), env_allowlist=DEFAULT_ENV_ALLOWLIST)
    message = str(caught.value)
    assert "refusing to launch" in message and "startup files of a login sh" in message
    assert "which change GITHUB_TOKEN" in message and FAKE_TOKEN not in message
