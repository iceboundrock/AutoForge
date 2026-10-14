"""Every launch class through the real backend (ADR 0005, #168).

What ``test_sandbox_policy.py`` reads from a description, run: a fake agent
started by the Claude Code adapter in a REMOTE issue worktree commits
through the overlay and the controller imports its commit; a LOCAL
validation command cannot write the git directory; an export command sees
only its tree, without a network when asked. The module skips with the
probe's reason on a host that cannot enforce a launch, as
``test_sandbox_bwrap.py`` does.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

from autoforge import sandbox_bwrap
from autoforge.agent_worktree import import_private_objects, read_candidate
from autoforge.config import ProfileConfig, default_config
from autoforge.errors import SandboxTamperError
from autoforge.executor import ExecutionRequest, execute
from autoforge.providers import AgentRequest, ClaudeCodeProvider, OpenCodeProvider
from autoforge.sandbox import NetworkPosition
from autoforge.sandbox_policy import LaunchClass, LaunchFacts, SandboxPlan, describe_launch
from autoforge.transitions import Phase

from .conftest import GIT_IDENT, FakeGitHub, make_local_engine

PY = "/usr/bin/python3"

# Prepended to every probe run inside: each operation's outcome as a word.
_PROBE = """
import errno, json, os, subprocess, sys
def attempt(fn):
    try:
        fn()
        return "ok"
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))
def write(path, text="x"):
    with open(path, "a") as fh:
        fh.write(text)
def read(path):
    with open(path) as fh:
        fh.read()
"""


@pytest.fixture(scope="module")
def backend(tmp_path_factory: pytest.TempPathFactory) -> str:
    if not sys.platform.startswith("linux"):
        pytest.skip("the outer sandbox's backend is Linux-only")
    if not os.path.exists(PY):
        pytest.skip(f"{PY} is not installed, so no fake command can run inside")
    home = tmp_path_factory.mktemp("probe-home")
    result = sandbox_bwrap.probe(home=str(home), environ={})
    if not result.supported:
        pytest.skip(f"this host cannot enforce the outer sandbox: {result.reason}")
    return result.version


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _sentinel(path: Path, text: str = "sentinel\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def layout(tmp_path: Path):
    """A checkout, its issue worktree and a neighbour, a state directory, a fake home."""
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
    state = repo / ".autoforge"
    _sentinel(state / "state.json")
    context = state / "read-context" / "run" / "analyze_execute-1-abc"
    _sentinel(context / "issue.md", "the issue\n")
    home = base / "home"
    _sentinel(home / ".ssh" / "id_rsa", "operator key\n")
    _sentinel(home / ".claude" / ".credentials.json", "{}\n")
    config = base / "autoforge-child-1"
    _sentinel(config / "gitconfig", "")
    paths = {
        "base": base,
        "repo": repo,
        "common": common,
        "worktree": worktrees / "7",
        "neighbour": worktrees / "8",
        "admin": common / "worktrees" / "7",
        "sandbox": sandbox,
        "state": state,
        "context": context,
        "home": home,
        "config": config,
    }
    yield paths
    sandbox_bwrap._make_removable(str(sandbox / "overlay-work"))


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
        read_context=str(paths["context"]),
        controller_config=str(paths["config"]),
        denied_roots=(
            str(paths["state"]),
            str(paths["repo"]),
            str(paths["common"] / "autoforge"),
            str(paths["neighbour"]),
        ),
    )


def _fake_agent(paths, name: str, output: str, marker: str) -> Path:
    """A fake agent CLI that probes what it can reach, commits in its worktree
    and leaves a detached process behind; ``output`` is how it prints the
    report: ``text``, or ``stream-json`` (one ``result`` record, as Claude Code
    ends its stream)."""
    worktree = paths["worktree"]
    probes = {
        "credentials": f"read({str(paths['home'] / '.claude' / '.credentials.json')!r})",
        "ssh_key": f"read({str(paths['home'] / '.ssh' / 'id_rsa')!r})",
        "ssh_agent": f"connect_unix({str(paths['base'] / 'ssh-agent.sock')!r})",
        "checkout": f"read({str(paths['repo'] / 'checkout.txt')!r})",
        "state": f"read({str(paths['state'] / 'state.json')!r})",
        "dotdot": f"read({str(worktree) + '/../../../../.autoforge/state.json'!r})",
        "link": f"read_through_link({str(paths['state'] / 'state.json')!r})",
        "neighbour": f"read({str(paths['neighbour'] / 'neighbour.txt')!r})",
        "neighbour_admin": f"read({str(paths['common'] / 'worktrees' / '8' / 'HEAD')!r})",
        "read_context": f"read({str(paths['context'] / 'issue.md')!r})",
        "common_head": f"write({str(paths['common'] / 'HEAD')!r})",
        "branch_ref": f"write({str(paths['common'] / 'refs' / 'heads' / 'agent')!r})",
        "config": f"write({str(paths['common'] / 'config')!r})",
        "hook": f"write({str(paths['common'] / 'hooks' / 'pre-commit')!r})",
        "dotgit": f"write({str(worktree / '.git')!r})",
        "commondir": f"write({str(paths['admin'] / 'commondir')!r})",
        "cache": "write(os.path.expanduser('~/.cache/tool'))",
        "tmp": "write('/tmp/scratch')",
    }
    body = "\n".join(f"report[{k!r}] = attempt(lambda: {v})" for k, v in probes.items())
    emit = (
        "print(json.dumps(report))\n"
        if output == "text"
        else "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,"
        " 'result': json.dumps(report)}))\n"
    )
    script = (
        f"#!{PY}\n{_PROBE}\n"
        "import socket\n"
        "def connect_unix(path):\n"
        "    s = socket.socket(socket.AF_UNIX)\n"
        "    try:\n"
        "        s.connect(path)\n"
        "    finally:\n"
        "        s.close()\n"
        "def read_through_link(target):\n"
        "    os.symlink(target, 'escape')\n"
        "    try:\n"
        "        read('escape')\n"
        "    finally:\n"
        "        os.unlink('escape')\n"
        f"report = {{}}\n{body}\n"
        "write('inside.txt', 'made inside\\n')\n"
        "git = ['git', '-c', 'user.name=Agent', '-c', 'user.email=agent@example.invalid',"
        " '-c', 'gc.auto=0']\n"
        "subprocess.run(git + ['add', 'inside.txt'], check=True)\n"
        "subprocess.run(git + ['commit', '-q', '-m', 'inside (#7)'], check=True)\n"
        "report['head'] = subprocess.run(git + ['rev-parse', 'HEAD'], check=True,"
        " capture_output=True, text=True).stdout.strip()\n"
        # A process that leaves the agent's session: the launch's end still ends it.
        f"subprocess.Popen([{PY!r}, '-c', 'import time; time.sleep(600)', {marker!r}],"
        " start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,"
        " stderr=subprocess.DEVNULL)\n" + emit
    )
    tool = _sentinel(paths["base"] / "tools" / name / "bin" / name, script)
    tool.chmod(0o755)
    return tool


_PROVIDER_PATHS = {
    # path id: (adapter, executable, profile options, how the fake prints)
    "claude-text": (ClaudeCodeProvider, "claude", {"output_format": "text"}, "text"),
    "claude-stream-json": (
        ClaudeCodeProvider,
        "claude",
        {"output_format": "stream-json"},
        "stream-json",
    ),
    "opencode": (OpenCodeProvider, "opencode", {}, "text"),
}


@pytest.mark.parametrize("path", sorted(_PROVIDER_PATHS))
def test_a_remote_agent_commits_through_the_overlay_and_reaches_nothing_else(
    backend, layout, monkeypatch, path
):
    paths = layout
    adapter, name, options, output = _PROVIDER_PATHS[path]
    marker = f"autoforge-routing-{uuid.uuid4().hex}"
    tool = _fake_agent(paths, name, output, marker)
    agent_socket = socket.socket(socket.AF_UNIX)
    agent_socket.bind(str(paths["base"] / "ssh-agent.sock"))
    agent_socket.listen(1)
    monkeypatch.setenv("HOME", str(paths["home"]))
    monkeypatch.setenv("PATH", f"{tool.parent}:/usr/bin:/bin")
    monkeypatch.setenv("SSH_AUTH_SOCK", str(paths["base"] / "ssh-agent.sock"))
    base = _git("-C", str(paths["repo"]), "rev-parse", "HEAD")
    req = AgentRequest(
        phase="ANALYZE_EXECUTE",
        prompt="implement",
        cwd=str(paths["worktree"]),
        profile=ProfileConfig(
            name="analyze_execute", provider=name, model="m/x", effort="high", options=options
        ),
        idle_timeout_seconds=None,
        max_runtime_seconds=120,
        env_allowlist=default_config().execution.environment_names(),
        env={"GIT_CONFIG_GLOBAL": str(paths["config"] / "gitconfig")},
        sandbox=SandboxPlan(_remote_facts(paths)),
    )
    try:
        result = adapter().execute(req)
    finally:
        agent_socket.close()
    assert result.exit_code == 0, result.stderr
    assert result.sandbox is not None and result.sandbox.enforced
    report = json.loads(result.stdout.strip().splitlines()[-1])
    head = report.pop("head")
    for reachable in ("read_context", "cache", "tmp"):
        assert report.pop(reachable) == "ok", reachable
    # Claude's login file is projected read-only for Claude alone.
    assert report.pop("credentials") == ("ok" if name == "claude" else "ENOENT")
    hidden = (
        "ssh_key",
        "ssh_agent",
        "checkout",
        "state",
        "dotdot",
        "link",
        "neighbour",
        "neighbour_admin",
    )
    for probe in hidden:
        assert report.pop(probe) == "ENOENT", probe
    # Everything left is a write the sandbox refuses.
    assert report == {
        probe: "EROFS"
        for probe in ("common_head", "branch_ref", "config", "hook", "dotgit", "commondir")
    }
    # Nothing it started survived the launch.
    left = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
    assert left.stdout == "", left.stdout
    # Its objects are in the private directory, not the shared store: the
    # candidate names no commit until the strict import ran.
    common, admin = paths["common"], paths["admin"]
    assert not (common / "refs" / "heads" / "agent").exists()
    assert read_candidate(common, admin, "w", None).head == ""
    private = paths["sandbox"] / "objects"
    assert import_private_objects(common, admin, private, None, exclude=(base,)) == head
    assert read_candidate(common, admin, "w", None).head == head
    assert _git("--git-dir", str(common), "show", f"{head}:inside.txt") == "made inside"
    assert (paths["sandbox"] / "cache" / "tool").exists()


def test_an_alternates_file_written_through_the_overlay_is_tampering(backend, layout):
    """``objects/info/alternates`` inside lands in the private directory; the
    import refuses to read anything from it (D6.5)."""
    paths = layout
    description = describe_launch(_remote_facts(paths), None, {})
    alternates = paths["common"] / "objects" / "info" / "alternates"
    code = (
        f"report = {{'alternates': attempt(lambda: write({str(alternates)!r},"
        f" {str(paths['repo'] / '.git' / 'objects')!r} + '\\n'))}}\n"
        "print(json.dumps(report))\n"
    )
    assert _probe(description, paths["worktree"], code) == {"alternates": "ok"}
    assert not alternates.exists()  # the shared store is untouched
    private = paths["sandbox"] / "objects"
    assert (private / "info" / "alternates").exists()
    with pytest.raises(SandboxTamperError, match="info/alternates"):
        import_private_objects(paths["common"], paths["admin"], private, None)


def _local_facts(paths, network=NetworkPosition.HOST) -> LaunchFacts:
    git_dir = paths["common"]
    cache = git_dir / "autoforge" / "sandbox" / "local" / "cache"
    cache.mkdir(parents=True)
    return LaunchFacts(
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
        network=network,
    )


def _probe(description, cwd: Path, code: str, **env: str) -> dict:
    res = execute(
        ExecutionRequest(
            command=[PY, "-c", _PROBE + code],
            cwd=str(cwd),
            timeout_seconds=60,
            env=env or None,
            sandbox=description,
        )
    )
    assert res.exit_code == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_a_local_validation_command_cannot_write_the_git_directory(backend, layout):
    paths = layout
    description = describe_launch(_local_facts(paths), None, {})
    code = (
        "git = ['git', '-c', 'user.name=V', '-c', 'user.email=v@example.invalid']\n"
        "report = {\n"
        "  'tree': attempt(lambda: write('built.txt')),\n"
        "  'head': attempt(lambda: write('.git/HEAD')),\n"
        f"  'state': attempt(lambda: read({str(paths['state'] / 'state.json')!r})),\n"
        f"  'neighbour': attempt(lambda: read({str(paths['neighbour'] / 'neighbour.txt')!r})),\n"
        "  'commit': subprocess.run(git + ['commit', '-q', '--allow-empty', '-m', 'v'],"
        " capture_output=True).returncode,\n"
        "  'stash': subprocess.run(git + ['stash', 'push', '-q'],"
        " capture_output=True).returncode,\n"
        "  'branch': subprocess.run(git + ['branch', 'agent-branch'],"
        " capture_output=True).returncode,\n"
        "}\n"
        "print(json.dumps(report))\n"
    )
    report = _probe(description, paths["repo"], code)
    assert report["tree"] == "ok" and report["head"] == "EROFS"
    assert report["state"] == "ENOENT" and report["neighbour"] == "ENOENT"
    assert report["commit"] != 0 and report["stash"] != 0 and report["branch"] != 0
    assert _git("-C", str(paths["repo"]), "branch", "--list", "agent-branch") == ""
    assert _git("-C", str(paths["repo"]), "stash", "list") == ""
    assert (paths["repo"] / "built.txt").exists()


def test_an_export_command_sees_its_tree_and_no_network_when_isolated(backend, layout):
    paths = layout
    tree = paths["base"] / "export" / "tree"
    _sentinel(tree / "Makefile", "check:\n")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    accepted: list[bool] = []

    def accept() -> None:
        listener.settimeout(10)
        try:
            conn, _ = listener.accept()
            accepted.append(True)
            conn.close()
        except OSError:
            pass

    code = (
        "import socket\n"
        "def connect():\n"
        "    s = socket.create_connection(('127.0.0.1', PORT), timeout=5)\n"
        "    s.close()\n"
        "report = {\n"
        "  'tree': attempt(lambda: write('out.txt')),\n"
        f"  'checkout': attempt(lambda: read({str(paths['repo'] / 'checkout.txt')!r})),\n"
        f"  'objects': attempt(lambda: os.listdir({str(paths['common'] / 'objects')!r})),\n"
        "  'network': attempt(connect),\n"
        "}\n"
        "print(json.dumps(report))\n"
    ).replace("PORT", str(port))
    try:
        for network, reaches in ((NetworkPosition.ISOLATED, False), (NetworkPosition.HOST, True)):
            facts = LaunchFacts(
                launch_class=LaunchClass.EXPORT_COMMAND,
                workspace=str(tree),
                home=str(paths["home"]),
                controller_env={},
                controller_config=str(paths["config"]),
                denied_roots=(str(paths["state"]), str(paths["repo"])),
                network=network,
            )
            waiter = threading.Thread(target=accept)
            waiter.start()
            report = _probe(describe_launch(facts, None, {}), tree, code)
            if not reaches:
                # Nothing connects; release the waiter.
                socket.create_connection(("127.0.0.1", port), timeout=5).close()
            waiter.join()
            assert report["tree"] == "ok"
            assert report["checkout"] == "ENOENT" and report["objects"] == "ENOENT"
            assert (report["network"] == "ok") is reaches, report
    finally:
        listener.close()


def test_a_required_local_run_validates_inside_the_sandbox(backend, tmp_path):
    """The engine routes a LOCAL validation command through the backend: it
    cannot create a file in the git directory (outside, it could), and the
    run still advances on its exit status."""
    from .test_local import impl_result, local_repo, review_result, scripted, touch_impl

    root = local_repo(tmp_path)
    code = (
        _PROBE
        + "assert attempt(lambda: write('.git/validation-probe')) == 'EROFS'\n"
        + "assert not os.path.exists('.git/autoforge/state')\n"
    )
    cfg = default_config()
    cfg.sandbox.mode = "required"
    cfg.local.validation_commands = [[PY, "-c", code]]
    eng = make_local_engine(root, "features/add-filter.md", cfg=cfg)
    eng.provider._handler = scripted(
        eng,
        root,
        [
            (lambda r: touch_impl(r, "v1\n"), lambda e: impl_result()),
            (None, lambda e: review_result(e.state.workspace_fingerprint)),
        ],
    )
    eng.run(max_steps=5)
    assert eng.state.phase == Phase.DONE
    assert not (root / ".git" / "validation-probe").exists()
    assert len(eng.provider.descriptions) == 2
    run_dir = eng.paths.logs_dir / eng.state.run_id
    (step,) = [p for p in run_dir.iterdir() if "-validation-" in p.name]
    request = json.loads((step / "request.json").read_text(encoding="utf-8"))
    execution = json.loads((step / "execution.json").read_text(encoding="utf-8"))
    assert request["metadata"]["sandbox_mode"] == "required"
    assert execution["sandbox"]["enforced"] is True


def test_a_required_premerge_command_runs_in_the_export_alone(
    backend, tmp_state_dir, fake_github, monkeypatch
):
    """The engine routes ``merge.verification_commands`` through the backend:
    the command sees the exported tree and not the operator's checkout or the
    controller's state, and the merge proceeds on its exit status."""
    from .test_engine import _in_merge_on_commit

    checkout = tmp_state_dir.parent
    code = (
        _PROBE
        + "assert open('proof.txt').read() == 'v1\\n'\n"
        + f"assert attempt(lambda: read({str(checkout / 'proof.txt')!r})) == 'ENOENT'\n"
        + f"assert not os.path.exists({str(tmp_state_dir)!r})\n"
        + f"assert not os.path.exists({str(checkout / '.git')!r})\n"
    )
    eng, sha = _in_merge_on_commit(tmp_state_dir, fake_github, [[PY, "-c", code]])
    eng.config.sandbox.mode = "required"
    eng.config.sandbox.premerge_network = "isolated"
    eng.config.execution.command_timeout_seconds = 60
    out = eng.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC", out.message
    assert len(fake_github.merges) == 1 and eng.state.premerge_verified_head_sha == sha
    run_dir = eng.paths.logs_dir / eng.state.run_id
    (step,) = [p for p in run_dir.iterdir() if "premerge-verification" in p.name]
    request = json.loads((step / "request.json").read_text(encoding="utf-8"))
    execution = json.loads((step / "execution.json").read_text(encoding="utf-8"))
    assert request["metadata"]["sandbox_mode"] == "required"
    assert execution["sandbox"]["enforced"] is True
    assert execution["sandbox"]["network"] == "isolated"


def test_an_all_pi_run_is_sandboxed_in_every_child(
    backend, tmp_state_dir, tmp_path_factory, monkeypatch
):
    """Every Pi launch and its auth preflight run inside, with Pi's own
    directory (``PI_CODING_AGENT_DIR``) its one writable login location, and
    the run reaches DONE through the controller's merge."""
    from .pi_fake import PiFake, make_pi_engine
    from .test_integration import _six_round_agent

    fake = PiFake(tmp_path_factory.mktemp("pi"))
    # The fake re-executes itself with the system Python, which the sandbox
    # shows, from an install directory of its own (its home is Pi's
    # writable directory, which no read-only root may overlap).
    script = fake.home / "fake_pi.py"
    fake.command = tmp_path_factory.mktemp("pi-install") / "pi"
    fake.command.write_text(f'#!/bin/sh\nexec {PY} "{script}" "{fake.home}" "$@"\n')
    fake.command.chmod(0o755)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(fake.home))
    cfg = default_config()
    cfg.sandbox.mode = "required"
    gh = FakeGitHub()
    eng = make_pi_engine(tmp_state_dir, fake, _six_round_agent(gh), github=gh, cfg=cfg, origin=True)
    eng.config.safety.allow_merge = True
    eng._save()
    overlay_work = tmp_state_dir.parent / ".git" / "autoforge" / "sandbox" / "2" / "overlay-work"
    try:
        outcomes = eng.run(max_steps=50, allow_merge=True)
    finally:
        sandbox_bwrap._make_removable(str(overlay_work))
    assert outcomes[-1].next_phase == "DONE", outcomes[-1].message
    launches = fake.launches()
    assert len(launches) == len(eng.pi.calls) == len(fake.auth_checks()) == 13
    assert all(req.sandbox is not None for req in eng.pi.calls)
    # Inside, the controller's isolation still holds (#165).
    assert all(e["isolation"]["gh_config_dir_empty"] for e in launches)
    run_dir = eng.paths.logs_dir / eng.state.run_id
    executions = [
        json.loads((p / "execution.json").read_text(encoding="utf-8"))
        for p in sorted(run_dir.iterdir())
        if (p / "execution.json").exists()
    ]
    assert executions and all(e["sandbox"]["enforced"] for e in executions)
