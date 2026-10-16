"""One boundary for every launch path, on the real backend (ADR 0005 D13.5, #169).

The same probe runs inside every way AutoForge launches a process: Claude
Code with text and with stream-json output, OpenCode, Pi's RPC child and its
``auth check`` preflight, a provider-neutral adapter that declares nothing
of its own, and the two classes of repository-defined command (a LOCAL
validation command and an export command). Each writes what it observed
into its working directory; the reports must agree on every sentinel and
fact: the same things hidden, the same things read-only, the same private
``/tmp`` and home, the same namespaces, the same abstract-socket scope.

The module skips with the probe's reason on a host that cannot enforce
(:func:`tests.conftest.real_sandbox_backend`); ``make test-sandbox`` fails
instead.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import uuid
from pathlib import Path

import pytest

from autoforge import sandbox_bwrap
from autoforge.config import ProfileConfig, default_config
from autoforge.executor import ExecutionRequest, execute
from autoforge.providers import (
    AgentProvider,
    AgentRequest,
    ClaudeCodeProvider,
    OpenCodeProvider,
    PiProvider,
)
from autoforge.sandbox import NetworkPosition
from autoforge.sandbox_policy import LaunchClass, LaunchFacts, SandboxPlan, describe_launch

from .conftest import GIT_IDENT, SYSTEM_PYTHON, real_sandbox_backend

PY = SYSTEM_PYTHON


@pytest.fixture(scope="module")
def backend(tmp_path_factory: pytest.TempPathFactory) -> str:
    return real_sandbox_backend(tmp_path_factory)


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _sentinel(path: Path, text: str = "sentinel\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# What every path's probe reports. ``name`` is where the report goes, in the
# launch's working directory; ``abstract`` is a host abstract socket that the
# Landlock scope must keep out of reach.
_PROBE = r"""
import errno, json, os, socket, sys

def attempt(fn):
    try:
        fn()
        return "ok"
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))

def read(path):
    with open(path) as fh:
        fh.read()

def write(path):
    with open(path, "a") as fh:
        fh.write("x")

def connect_abstract(name):
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect("\0" + name)
    finally:
        s.close()

def report(name, sentinels, abstract):
    seen = {key: attempt(lambda p=path: read(p)) for key, path in sentinels.items()}
    seen.update(
        workspace=attempt(lambda: write("parity-scratch")),
        tmp=attempt(lambda: write("/tmp/parity-scratch")),
        home=attempt(lambda: write(os.path.expanduser("~/parity-scratch"))),
        usr=attempt(lambda: write("/usr/parity-scratch")),
        etc=attempt(lambda: write("/etc/parity-scratch")),
        run=attempt(lambda: os.listdir("/run/user")),
        abstract=attempt(lambda: connect_abstract(abstract)),
        pid_namespace=os.readlink("/proc/self/ns/pid"),
        ipc_namespace=os.readlink("/proc/self/ns/ipc"),
        user_namespace=os.readlink("/proc/self/ns/user"),
        net_namespace=os.readlink("/proc/self/ns/net"),
        pid_one=open("/proc/1/comm").read().strip(),
        uid=os.getuid(),
        xdg_runtime_dir="XDG_RUNTIME_DIR" in os.environ,
        ssh_auth_sock="SSH_AUTH_SOCK" in os.environ,
    )
    with open(name, "w") as fh:
        json.dump(seen, fh)
"""


@pytest.fixture
def world(tmp_path: Path):
    """A checkout with an issue worktree and a neighbour, a state directory, a
    home with secrets, an export, the probe as a module, a host abstract socket."""
    base = Path(os.path.realpath(tmp_path))
    repo = base / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", str(repo))
    _sentinel(repo / "checkout.txt")
    _git("-C", str(repo), "add", "checkout.txt")
    _git("-C", str(repo), *GIT_IDENT, "commit", "-q", "-m", "initial")
    common = repo / ".git"
    worktrees = common / "autoforge" / "worktrees"
    _git("-C", str(repo), "worktree", "add", "-q", "--detach", str(worktrees / "7"), "HEAD")
    _git("-C", str(repo), "worktree", "add", "-q", "--detach", str(worktrees / "8"), "HEAD")
    _sentinel(worktrees / "8" / "neighbour.txt")
    sandbox = common / "autoforge" / "sandbox" / "7"
    for name in ("objects", "overlay-work", "cache"):
        (sandbox / name).mkdir(parents=True)
    state = base / "state"
    _sentinel(state / "state.json")
    home = base / "home"
    _sentinel(home / ".ssh" / "id_rsa", "operator key\n")
    _sentinel(home / ".config" / "gh" / "hosts.yml", "token: x\n")
    config = base / "autoforge-child-1"
    _sentinel(config / "gitconfig", "")
    export = base / "export"
    export.mkdir()
    tools = base / "tools"
    abstract = f"autoforge-parity-{uuid.uuid4().hex}"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind("\0" + abstract)
    listener.listen(8)
    paths = {
        "base": base,
        "repo": repo,
        "common": common,
        "worktree": worktrees / "7",
        "neighbour": worktrees / "8",
        "admin": common / "worktrees" / "7",
        "sandbox": sandbox,
        "state": state,
        "home": home,
        "config": config,
        "export": export,
        "tools": tools,
        "abstract": abstract,
    }
    try:
        yield paths
    finally:
        listener.close()
        sandbox_bwrap._make_removable(str(sandbox / "overlay-work"))


def _sentinels(paths) -> dict[str, str]:
    """What no launch of any class may read, by the name the report gives it."""
    return {
        "ssh_key": str(paths["home"] / ".ssh" / "id_rsa"),
        "gh_config": str(paths["home"] / ".config" / "gh" / "hosts.yml"),
        "state": str(paths["state"] / "state.json"),
        "neighbour": str(paths["neighbour"] / "neighbour.txt"),
        "controller_dir": str(paths["common"] / "autoforge" / "worktrees" / "8" / ".git"),
    }


def _probe_call(paths, name: str) -> str:
    """Python source of the probe, reporting into ``name``: inlined, since a
    launch sees an executable's own install root and nothing beside it."""
    return f"{_PROBE}\nreport({name!r}, {_sentinels(paths)!r}, {paths['abstract']!r})\n"


def _cli(paths, name: str, body: str) -> Path:
    """An executable ``name`` in a directory of its own: the probe, then ``body``."""
    script = f"#!{PY}\nimport json, os, sys\n{_probe_call(paths, f'parity-{name}.json')}\n{body}"
    tool = _sentinel(paths["tools"] / name / "bin" / name, script)
    tool.chmod(0o755)
    return tool


def _remote_facts(paths) -> LaunchFacts:
    return LaunchFacts(
        launch_class=LaunchClass.REMOTE_AGENT,
        workspace=str(paths["worktree"]),
        home=str(paths["home"]),
        controller_env={},
        git_entry=str(paths["worktree"] / ".git"),
        git_dir=str(paths["admin"]),
        common_dir=str(paths["common"]),
        private_objects=str(paths["sandbox"] / "objects"),
        overlay_work=str(paths["sandbox"] / "overlay-work"),
        cache=str(paths["sandbox"] / "cache"),
        controller_config=str(paths["config"]),
        denied_roots=(
            str(paths["state"]),
            str(paths["repo"]),
            str(paths["common"] / "autoforge"),
            str(paths["neighbour"]),
        ),
    )


def _request(paths, provider: str, command: Path, model="m/x", **options: str) -> AgentRequest:
    return AgentRequest(
        phase="ANALYZE_EXECUTE",
        prompt="implement",
        cwd=str(paths["worktree"]),
        profile=ProfileConfig(
            name="analyze_execute",
            provider=provider,
            model=model,
            effort="high",
            command=str(command),
            options=options,
        ),
        idle_timeout_seconds=None,
        max_runtime_seconds=120,
        env_allowlist=default_config().execution.environment_names(),
        env={"GIT_CONFIG_GLOBAL": str(paths["config"] / "gitconfig")},
        sandbox=SandboxPlan(_remote_facts(paths)),
    )


class NeutralAdapter(AgentProvider):
    """A provider-neutral adapter: the base class's launch and declaration alone."""

    name = "neutral"
    option_keys = ()

    def build_command_for(self, profile: ProfileConfig, prompt: str) -> list[str]:
        return [profile.command or "neutral"]


# Pi's RPC subset AutoForge speaks, enough for one settled prompt; the probe
# runs first, in the preflight and in the RPC child alike.
_PI_BODY = r"""
argv = sys.argv[1:]
if argv[:2] == ["auth", "check"]:
    provider = argv[argv.index("--model") + 1].split("/", 1)[0]
    print(json.dumps({"status": "ready", "provider": provider, "authType": "oauth"}))
    sys.exit(0)
model = argv[argv.index("--model") + 1]
provider, model_id = model.split("/", 1)
out = sys.stdout


def emit(obj):
    out.write(json.dumps(obj) + "\n")
    out.flush()


def respond(command, rid, data=None):
    record = {"type": "response", "id": rid, "command": command, "success": True}
    if data is not None:
        record["data"] = data
    emit(record)


for line in sys.stdin:
    command = json.loads(line)
    kind, rid = command["type"], command.get("id")
    if kind == "get_state":
        respond(kind, rid, {
            "model": {"provider": provider, "id": model_id, "name": "fake", "contextWindow": 1},
            "thinkingLevel": argv[argv.index("--thinking") + 1],
            "isStreaming": False,
        })
    elif kind == "get_available_models":
        respond(kind, rid, {"models": [{"provider": provider, "id": model_id}]})
    elif kind == "prompt":
        respond(kind, rid, {"disposition": "started"})
        emit({"type": "agent_start"})
        message = {"role": "assistant", "content": [{"type": "text", "text": "done"}],
                   "stopReason": "stop"}
        emit({"type": "message_end", "message": message})
        emit({"type": "agent_end", "messages": [], "willRetry": False})
        emit({"type": "agent_settled"})
    elif kind == "get_last_assistant_text":
        respond(kind, rid, {"text": "done"})
    elif kind == "abort":
        respond(kind, rid)
        emit({"type": "agent_settled"})
sys.exit(0)
"""


def _pi(paths, monkeypatch) -> Path:
    """A fake ``pi`` whose preflight and RPC child each write their own report."""
    script = (
        f"#!{PY}\nimport json, os, sys\n{_PROBE}\n"
        "kind = 'preflight' if sys.argv[1:3] == ['auth', 'check'] else 'rpc'\n"
        f"report('parity-pi-' + kind + '.json', {_sentinels(paths)!r}, {paths['abstract']!r})\n"
        + _PI_BODY
    )
    tool = _sentinel(paths["tools"] / "pi" / "bin" / "pi", script)
    tool.chmod(0o755)
    agent_dir = paths["base"] / "pi-autoforge"
    agent_dir.mkdir()
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    return tool


_STREAM_JSON = (
    "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,"
    " 'result': 'done'}))\n"
)


def _host(paths, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(paths["home"]))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(paths["base"] / "runtime"))
    monkeypatch.setenv("SSH_AUTH_SOCK", str(paths["base"] / "ssh-agent.sock"))


def _agent_paths(paths, monkeypatch) -> dict[str, dict]:
    """Launch every agent path; each one's report, read from the worktree."""
    _host(paths, monkeypatch)
    results = {}
    claude_text = _cli(paths, "claude-text", "print('done')\n")
    claude_stream = _cli(paths, "claude-stream-json", _STREAM_JSON)
    opencode = _cli(paths, "opencode", "print('done')\n")
    neutral = _cli(paths, "neutral", "print('done')\n")
    pi = _pi(paths, monkeypatch)
    launches = [
        (
            "claude-text",
            ClaudeCodeProvider(),
            _request(paths, "claude", claude_text, output_format="text"),
        ),
        (
            "claude-stream-json",
            ClaudeCodeProvider(),
            _request(paths, "claude", claude_stream, output_format="stream-json"),
        ),
        ("opencode", OpenCodeProvider(), _request(paths, "opencode", opencode)),
        ("neutral", NeutralAdapter(), _request(paths, "neutral", neutral)),
        (
            "pi-rpc",
            PiProvider(round_trip_seconds=10, abort_seconds=1),
            _request(paths, "pi", pi, model="openai/gpt-x"),
        ),
    ]
    for name, adapter, req in launches:
        result = adapter.execute(req)
        assert result.exit_code == 0, (name, result.stderr, result.provider_failure)
        assert result.sandbox is not None and result.sandbox.enforced, name
        results[name] = {"facts": result.sandbox.record()}
    for name in ("claude-text", "claude-stream-json", "opencode", "neutral", "pi-rpc"):
        report = paths["worktree"] / f"parity-{name}.json"
        results[name]["report"] = json.loads(report.read_text())
    preflight = paths["worktree"] / "parity-pi-preflight.json"
    results["pi-preflight"] = {"report": json.loads(preflight.read_text())}
    return results


def _command_paths(paths, monkeypatch) -> dict[str, dict]:
    """Launch both classes of repository-defined command; each one's report."""
    _host(paths, monkeypatch)
    git_dir = paths["common"]
    cache = git_dir / "autoforge" / "sandbox" / "local" / "cache"
    cache.mkdir(parents=True)
    classes = {
        "local-validation": LaunchFacts(
            launch_class=LaunchClass.LOCAL_VALIDATION,
            workspace=str(paths["repo"]),
            home=str(paths["home"]),
            controller_env={},
            git_entry=str(git_dir),
            git_dir=str(git_dir),
            common_dir=str(git_dir),
            cache=str(cache),
            controller_config=str(paths["config"]),
            denied_roots=(str(git_dir / "autoforge"), str(paths["state"])),
        ),
        "export-command": LaunchFacts(
            launch_class=LaunchClass.EXPORT_COMMAND,
            workspace=str(paths["export"]),
            home=str(paths["home"]),
            controller_env={},
            controller_config=str(paths["config"]),
            denied_roots=(
                str(paths["state"]),
                str(paths["repo"]),
                str(paths["common"] / "autoforge"),
            ),
            network=NetworkPosition.HOST,
        ),
    }
    results = {}
    for name, facts in classes.items():
        result = execute(
            ExecutionRequest(
                command=[PY, "-c", _probe_call(paths, f"parity-{name}.json")],
                cwd=facts.workspace,
                timeout_seconds=60,
                env_allowlist=default_config().execution.environment_names(),
                sandbox=describe_launch(facts, None, {}),
            )
        )
        assert result.exit_code == 0, (name, result.stderr)
        assert result.sandbox is not None and result.sandbox.enforced, name
        report = Path(facts.workspace) / f"parity-{name}.json"
        results[name] = {"facts": result.sandbox.record(), "report": json.loads(report.read_text())}
    return results


def _host_namespaces() -> dict[str, str]:
    return {
        key: os.readlink(f"/proc/self/ns/{ns}")
        for key, ns in (
            ("pid_namespace", "pid"),
            ("ipc_namespace", "ipc"),
            ("user_namespace", "user"),
            ("net_namespace", "net"),
        )
    }


# What every path must observe, whatever launched it.
_EXPECTED = {
    "ssh_key": "ENOENT",
    "gh_config": "ENOENT",
    "state": "ENOENT",
    "neighbour": "ENOENT",
    "controller_dir": "ENOENT",
    "workspace": "ok",
    "tmp": "ok",
    "home": "ok",
    "usr": "EROFS",
    "etc": "EROFS",
    "run": "ENOENT",
    "abstract": "EPERM",
    "xdg_runtime_dir": False,
    "ssh_auth_sock": False,
}


def test_every_launch_path_sees_the_same_boundary(backend, world, monkeypatch):
    results = {**_agent_paths(world, monkeypatch), **_command_paths(world, monkeypatch)}
    assert sorted(results) == [
        "claude-stream-json",
        "claude-text",
        "export-command",
        "local-validation",
        "neutral",
        "opencode",
        "pi-preflight",
        "pi-rpc",
    ]
    host = _host_namespaces()
    for name, result in results.items():
        report = result["report"]
        observed = {key: report[key] for key in _EXPECTED}
        assert observed == _EXPECTED, (name, observed)
        # New user, PID and IPC namespaces; the host network (D9.1); the
        # operator's UID mapped in.
        for key in ("pid_namespace", "ipc_namespace", "user_namespace"):
            assert report[key] != host[key], (name, key)
        assert report["net_namespace"] == host["net_namespace"], name
        assert report["uid"] == os.getuid(), name
        assert report["pid_one"] == "bwrap", (name, report["pid_one"])
    # Every recorded fact but the per-launch ones is the same, path to path.
    facts = {
        name: {
            key: value
            for key, value in result["facts"].items()
            if key in ("enforced", "backend", "backend_version", "network", "teardown")
        }
        for name, result in results.items()
        if "facts" in result
    }
    assert len({json.dumps(f, sort_keys=True) for f in facts.values()}) == 1, facts
    (shared,) = {json.dumps(f, sort_keys=True) for f in facts.values()}
    assert json.loads(shared)["enforced"] is True
    assert json.loads(shared)["backend_version"] == backend
