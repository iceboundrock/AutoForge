"""``doctor``'s outer-sandbox rows (ADR 0005 D4.4, D7.3, D8.5, D9.3, D12.4, D14.1, D15.4; #169).

The capability probe is the conftest's stand-in; a provider's sandboxed
``--version`` goes to a fake runner that records it. Real ``git`` reads the
temporary repository; nothing reaches GitHub.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from autoforge import cli, doctor_sandbox
from autoforge.config import default_config
from autoforge.doctor import Doctor
from autoforge.errors import ConfigurationError, SandboxPolicyError
from autoforge.executor import ExecutionResult, execute
from autoforge.sandbox import Link, compile_launch
from autoforge.sandbox_bwrap import Capability, ProbeResult
from autoforge.sandbox_policy import AuthItem, AuthMode, ProviderRequirements, provider_request
from tests.conftest import git_out, git_repo, make_local_engine
from tests.local_helpers import local_repo

SECRET = "sk-ant-doctor-test-value-0123456789"


class Runner:
    """Real ``git``; a sandboxed launch recorded and answered; no ``gh`` ever."""

    def __init__(self, stdout: str = "fake-cli 1.2.3\n") -> None:
        self.sandboxed: list = []
        self.commands: list[list[str]] = []
        self.stdout = stdout
        # Per sandboxed launch: each path its ``env`` names, and whether it
        # existed while the launch ran.
        self.present: list[dict[str, bool]] = []

    def __call__(self, req):
        self.commands.append(list(req.command))
        assert req.command[0] != "gh", req.command
        if req.sandbox is not None:
            self.sandboxed.append(req)
            paths = {v for v in (req.env or {}).values() if v.startswith("/")}
            self.present.append({path: os.path.exists(path) for path in paths})
            return ExecutionResult(req.command, req.cwd, 0, self.stdout, "", "t", "t")
        return execute(req)

    def launch_of(self, name: str):
        """The sandboxed launch of the executable ``name``, and what it found present."""
        (index,) = [i for i, req in enumerate(self.sandboxed) if req.command[0].endswith(name)]
        return self.sandboxed[index], self.present[index]


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A home, a PATH with fake ``claude`` and ``opencode``, an empty /proc/net."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    bin_dir = tmp_path / "opt" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("claude", "opencode"):
        script = bin_dir / name
        script.write_text("#!/bin/sh\necho fake-cli 1.2.3\n")
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    for name in list(os.environ):
        if name.startswith(("CLAUDE_", "ANTHROPIC_", "OPENCODE_", "PI_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    proc_net = tmp_path / "proc-net"
    proc_net.mkdir()
    return {"home": home, "bin": bin_dir, "proc_net": proc_net}


def _doctor(
    cwd: Path,
    host,
    mode: str = "required",
    runner: Runner | None = None,
    state_dir: str | None = None,
) -> Doctor:
    d = Doctor(cwd=str(cwd), runner=runner or Runner(), state_dir=state_dir)
    d.config = default_config()
    d.config.sandbox.mode = mode
    d.proc_net = str(host["proc_net"])
    return d


def _rows(results) -> dict[str, object]:
    return {r.name.removeprefix("outer sandbox: "): r for r in results}


def test_local_doctor_shows_every_sandbox_row_and_never_calls_github(tmp_path, host):
    repo = git_repo(tmp_path / "repo")
    runner = Runner()
    d = _doctor(repo, host, runner=runner)
    results = d.sandbox_checks(local=True)
    assert all(r.name.startswith("outer sandbox: ") for r in results)
    rows = _rows(results)
    assert rows["capability (one minimal sandboxed launch)"].detail == (
        "supported (bubblewrap 0.0-test)"
    )
    assert rows["mode (required)"].ok
    assert "LOCAL agent" in " ".join(rows) and "LOCAL validation command" in " ".join(rows)
    providers = [r for name, r in rows.items() if name.startswith("provider ")]
    assert {r.name.split("'")[1] for r in providers} == {"claude", "opencode"}
    assert all(r.ok for r in providers), [(r.name, r.detail) for r in providers]
    # Each provider's --version ran inside the sandbox, by its resolved path.
    assert sorted(req.command[0] for req in runner.sandboxed) == [
        str(host["bin"] / "claude"),
        str(host["bin"] / "opencode"),
    ]
    assert all(req.command[1:] == ["--version"] for req in runner.sandboxed)
    claude = next(r for r in providers if "'claude'" in r.name)
    assert "credential env: ANTHROPIC_API_KEY" in claude.detail
    assert "inside the sandbox: fake-cli 1.2.3" in claude.detail
    # Paths and classes only: never a value.
    for r in results:
        assert SECRET not in r.detail and SECRET not in r.name
    assert not any(c[0] == "gh" for c in runner.commands)


def test_a_token_across_the_version_clip_leaves_no_fragment(tmp_path, host, capsys):
    """The version line is redacted before it is clipped (PR #231 R2-F3).

    The token starts seven characters before the clip: clipped first, the
    ``ghp_`` prefix and three characters would be too short for the
    redactor and would be printed as they are.
    """
    token = "ghp_" + "A1b2C3d4" * 4
    prefix = "x" * (doctor_sandbox.VERSION_CHARS - 8) + " "
    assert len(prefix) + 7 == doctor_sandbox.VERSION_CHARS
    repo = git_repo(tmp_path / "repo")
    runner = Runner(stdout=f"{prefix}{token} trailing\nsecond line\n")
    results = _doctor(repo, host, runner=runner).sandbox_checks(local=True)
    providers = [r for r in results if r.name.startswith("outer sandbox: provider ")]
    assert providers and all(r.ok for r in providers)
    assert all("inside the sandbox: xxx" in r.detail for r in providers)
    for as_json in (False, True):
        cli._report_checks(results, as_json, "doctor")
        out = capsys.readouterr().out
        assert "ghp_" not in out and "A1b" not in out
        assert "second line" not in out


def test_remote_doctor_appends_the_sandbox_rows(tmp_path, host):
    repo = git_repo(tmp_path / "repo")
    rows = _rows(_doctor(repo, host).sandbox_checks(local=False))
    assert "policy, REMOTE agent" in rows and "policy, export command" in rows


@pytest.mark.parametrize(
    ("mode", "local", "supported", "label", "words"),
    [
        ("disabled", True, True, "WARN", "logical isolation only"),
        ("disabled", False, False, "WARN", "logical isolation only"),
        ("required", True, True, "OK  ", "enforced"),
        ("required", True, False, "FAIL", "cannot provide a new procfs"),
        ("required", False, False, "FAIL", "cannot provide a new procfs"),
        ("preferred", False, False, "FAIL", "REMOTE run treats as required"),
        ("preferred", True, False, "WARN", "falls back to logical isolation only"),
        ("preferred", True, True, "OK  ", "enforced"),
    ],
)
def test_the_mode_row_says_what_the_mode_does_on_this_host(mode, local, supported, label, words):
    cfg = default_config()
    cfg.sandbox.mode = mode
    probe = (
        ProbeResult(True, "", "0.12.0")
        if supported
        else ProbeResult(False, "bwrap: Can't mount proc", "", Capability.PROCFS)
    )
    row = doctor_sandbox.mode_row(cfg, probe, local)
    assert row.label == label and words in row.detail, row.detail
    # A launch that falls back is never called sandboxed (D12.2).
    assert "is sandboxed" not in row.detail and "runs sandboxed" not in row.detail


def test_an_unsupported_capability_is_named(tmp_path, host, sandbox_probe):
    sandbox_probe.unsupported(Capability.USER_NAMESPACE, "userns disabled by sysctl")
    repo = git_repo(tmp_path / "repo")
    runner = Runner()
    rows = _rows(_doctor(repo, host, runner=runner).sandbox_checks(local=True))
    capability = rows["capability (one minimal sandboxed launch)"]
    assert capability.label == "WARN"
    assert "an unprivileged user namespace (userns disabled by sysctl)" in capability.detail
    assert rows["mode (required)"].label == "FAIL"
    # No sandboxed launch on a host that cannot enforce; the declarations still compile.
    assert runner.sandboxed == []
    assert all(
        "the declaration compiles" in r.detail for n, r in rows.items() if n.startswith("provider")
    )


def test_a_dir_rw_login_the_policy_refuses_is_named_with_the_alternatives(
    tmp_path, host, monkeypatch
):
    """D7.3: ``CLAUDE_CONFIG_DIR`` in ``~/.claude`` is the operator's own root (D7.2)."""
    root = host["home"] / ".claude"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    repo = git_repo(tmp_path / "repo")
    rows = _rows(_doctor(repo, host).sandbox_checks(local=True))
    (claude,) = [r for n, r in rows.items() if n.startswith("provider claude")]
    assert claude.label == "FAIL"
    assert "every launch of it is refused" in claude.detail
    assert "dir-rw login may not be the provider's own configuration root" in claude.detail
    assert "(file-ro)" in claude.detail and "(env)" in claude.detail


def test_an_operator_opencode_config_dir_is_replaced_as_at_every_launch(
    tmp_path, host, monkeypatch
):
    """The probe runs in the launch's own isolation (PR #231 R2-F2).

    Every OpenCode launch replaces the operator's ``OPENCODE_CONFIG_DIR``
    (here their configuration root, which no launch may be given) with an
    empty directory of its own, so the operator's refuses no launch and must
    not refuse the probe either. That directory exists while the probe runs
    and is removed with the launch.
    """
    own = host["home"] / ".config" / "opencode"
    own.mkdir(parents=True)
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(own))
    repo = git_repo(tmp_path / "repo")
    runner = Runner()
    rows = _rows(_doctor(repo, host, runner=runner).sandbox_checks(local=True))
    (opencode,) = [r for n, r in rows.items() if n.startswith("provider opencode")]
    assert opencode.ok and "inside the sandbox: fake-cli 1.2.3" in opencode.detail, opencode.detail
    req, present = runner.launch_of("opencode")
    config_dir = req.env["OPENCODE_CONFIG_DIR"]
    assert config_dir != str(own) and present[config_dir]
    assert req.env["OPENCODE_CONFIG_PROJECT_DISABLE"] == "1"
    assert f"writable {config_dir}" in opencode.detail
    assert not os.path.exists(config_dir)


def test_a_linked_claude_config_dir_is_spelled_where_it_leads_inside(tmp_path, host, monkeypatch):
    """The probe's environment is rewritten as a launch's is (PR #231 R2-F2, D8.2).

    ``CLAUDE_CONFIG_DIR`` names a link in the home, which the private home
    does not recreate: the probe's CLI is given the directory it leads to,
    where it is inside, as every launch's is. Claude's isolation (the
    launch's ``CLAUDE_ENV_FILE``) exists while the probe runs.
    """
    real = Path(os.path.realpath(tmp_path)) / "claude-autoforge"
    real.mkdir()
    alias = host["home"] / ".claude-autoforge"
    alias.symlink_to(real)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(alias))
    repo = git_repo(tmp_path / "repo")
    runner = Runner()
    rows = _rows(_doctor(repo, host, runner=runner).sandbox_checks(local=True))
    (claude,) = [r for n, r in rows.items() if n.startswith("provider claude")]
    assert claude.ok, claude.detail
    req, present = runner.launch_of("claude")
    env = dict(req.sandbox.env_set)
    assert env["CLAUDE_CONFIG_DIR"] == str(real)
    assert req.sandbox.inside_path(str(real)) == str(real)
    assert present[req.env["CLAUDE_ENV_FILE"]]


@pytest.mark.skipif(not os.path.exists("/bin/sh"), reason="no /bin/sh on this host")
def test_a_shebang_through_a_link_is_reached_as_written(tmp_path, host):
    """#225: a CLI whose ``#!`` names a link to the shell in a directory no
    root projects. The ``--version`` launch is compiled with that link
    recreated, as every launch of the CLI is, and the row says so."""
    shell = os.path.realpath("/bin/sh")
    alias = Path(os.path.realpath(tmp_path)) / "alias" / "bin" / "sh"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(shell)
    (host["bin"] / "claude").write_text(f"#!{alias}\necho fake-cli 1.2.3\n")
    repo = git_repo(tmp_path / "repo")
    runner = Runner()
    rows = _rows(_doctor(repo, host, runner=runner).sandbox_checks(local=True))
    (claude,) = [r for n, r in rows.items() if n.startswith("provider claude")]
    assert claude.ok, claude.detail
    assert f"reached as written, through its links: {alias}" in claude.detail
    req, _ = runner.launch_of("claude")
    assert Link(str(alias), shell) in req.sandbox.mounts
    assert req.sandbox.resolve_inside(str(alias)) == shell


def test_a_keyring_login_is_refused_and_the_alternatives_named(tmp_path):
    needs = ProviderRequirements(auth=(AuthItem(AuthMode.KEYRING, "the keychain login", "x"),))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(SandboxPolicyError):
        compile_launch(
            provider_request(
                needs, home=str(tmp_path / "home"), controller_env={}, workspace=str(workspace)
            )
        )
    alternatives = doctor_sandbox._login_alternatives(needs)
    assert "keyring login cannot reach a sandboxed launch" in alternatives
    assert "(file-ro)" in alternatives and "(dir-rw)" in alternatives and "(env)" in alternatives


def test_hard_links_in_the_local_workspace_are_listed_by_path(tmp_path, host):
    repo = git_repo(tmp_path / "repo")
    outside = tmp_path / "outside.txt"
    outside.write_text("shared inode\n")
    os.link(outside, repo / "linked.txt")
    (repo / "plain.txt").write_text("alone\n")
    row = _rows(_doctor(repo, host).sandbox_checks(local=True))["hard links in writable trees"]
    assert row.label == "WARN"
    assert str(repo / "linked.txt") in row.detail and "plain.txt" not in row.detail
    assert "shared inode" not in row.detail


def test_the_sandbox_directories_and_their_sizes_are_listed(tmp_path, host):
    repo = git_repo(tmp_path / "repo")
    cache = repo / ".git" / "autoforge" / "sandbox" / "local" / "cache"
    cache.mkdir(parents=True)
    (cache / "blob").write_bytes(b"x" * 2048)
    rows = _rows(_doctor(repo, host).sandbox_checks(local=True))
    sizes = rows["sandbox directories"]
    assert sizes.ok and f"{repo / '.git' / 'autoforge' / 'sandbox' / 'local'} 2.0 KiB" in (
        sizes.detail
    )
    # The sandbox directory is one of the trees scanned for hard links.
    assert "local" in rows["hard links in writable trees"].detail


def test_the_walk_reads_no_more_of_a_directory_than_its_bound(tmp_path, monkeypatch):
    """One crowded directory cannot defeat the walk's bound (PR #231 review R1-F3).

    Entries are read one at a time and the bound checked per entry, so the
    walk stops reading the directory at the entry past the bound instead of
    materializing all of it first.
    """
    crowded = tmp_path / "crowded"
    crowded.mkdir()
    for index in range(50):
        (crowded / f"f{index}").touch()
    scandir = os.scandir
    read: list[str] = []

    class Counting:
        """``os.scandir`` counting the entries the walk actually reads."""

        def __init__(self, path):
            self._entries = scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self._entries.close()

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self._entries)
            read.append(entry.name)
            return entry

    monkeypatch.setattr(doctor_sandbox, "MAX_WALK_ENTRIES", 5)
    monkeypatch.setattr(doctor_sandbox.os, "scandir", Counting)
    assert len(list(doctor_sandbox._walk(crowded, skip_git=False))) == 5
    # The bound, plus the one entry that reached past it.
    assert len(read) == 6


def test_operator_paths_over_a_denied_root_are_named(tmp_path, host):
    repo = git_repo(tmp_path / "repo")
    d = _doctor(repo, host)
    d.config.sandbox.read_only_paths = [str(repo / ".git" / "autoforge")]
    row = _rows(d.sandbox_checks(local=True))["operator read-only paths (sandbox.read_only_paths)"]
    assert row.label == "FAIL" and "overlaps" in row.detail
    d.config.sandbox.read_only_paths = [str(tmp_path / "opt")]
    row = _rows(d.sandbox_checks(local=True))["operator read-only paths (sandbox.read_only_paths)"]
    assert row.ok and "readable by every tool in the sandbox" in row.detail


# Where a LOCAL run keeps its state: (``--state-dir``, ``state_dir``), and the
# path in the run's denied roots an operator path or a provider may not name.
_LOCAL_STATE = {
    # The default state is <git dir>/autoforge/state; <root>/.autoforge, the
    # REMOTE default inside the root, is denied to a LOCAL launch as well.
    "default": (None, None, ".autoforge"),
    "relative --state-dir": ("../cli-state", None, "../cli-state"),
    "relative state_dir": (None, "../config-state", "../config-state"),
}


@pytest.mark.parametrize("where", sorted(_LOCAL_STATE))
def test_local_doctor_refuses_what_the_local_gate_refuses(tmp_path, host, monkeypatch, where):
    """The doctor's denied roots are the LOCAL launch's own (PR #231 R2-F1).

    ``<root>/.autoforge`` and a state directory named relative to the
    working directory were missing from the doctor's copy, which showed an
    OK row for an operator path the gate refuses before the first launch.
    """
    explicit, configured, denied = _LOCAL_STATE[where]
    root = local_repo(tmp_path / "repo")
    monkeypatch.chdir(root)
    target = os.path.realpath(root / denied)
    cfg = default_config()
    cfg.sandbox.mode = "required"
    if configured:
        cfg.state_dir = configured
    eng = make_local_engine(root, "features/add-filter.md", cfg=cfg, state_dir=explicit)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    d = _doctor(root, host, state_dir=explicit)
    d.config = cfg
    layout = doctor_sandbox._layout(d, cfg, local=True)
    assert layout is not None
    assert layout.denied == eng.gate_denied_roots(Path(eng.local_contract().repository_root))
    assert any(os.path.realpath(r) == target for r in layout.denied), layout.denied

    cfg.sandbox.read_only_paths = [target]
    row = _rows(d.sandbox_checks(local=True))["operator read-only paths (sandbox.read_only_paths)"]
    assert row.label == "FAIL" and "overlaps" in row.detail, row.detail
    with pytest.raises(ConfigurationError, match=r"sandbox\.read_only_paths\[0\].*overlaps"):
        eng.step()
    assert eng.provider.calls == []


@pytest.mark.parametrize("where", sorted(_LOCAL_STATE))
def test_a_provider_declaration_over_the_state_is_refused_before_its_launch(
    tmp_path, host, monkeypatch, where
):
    """A provider probe compiles with the run's denied roots (PR #231 R2-F1).

    OpenCode's database directory (``dir-rw``) put in the state directory
    would show it to every launch: the row fails, and its ``--version``
    is never launched.
    """
    explicit, configured, denied = _LOCAL_STATE[where]
    repo = git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    state = repo / denied
    state.mkdir(parents=True)
    monkeypatch.setenv("OPENCODE_DB", str(state / "opencode.db"))
    runner = Runner()
    d = _doctor(repo, host, runner=runner, state_dir=explicit)
    if configured:
        d.config.state_dir = configured
    rows = _rows(d.sandbox_checks(local=True))
    (opencode,) = [r for n, r in rows.items() if n.startswith("provider opencode")]
    assert opencode.label == "FAIL" and "every launch of it is refused" in opencode.detail
    assert f"lies in the denied root {os.path.realpath(state)}" in opencode.detail
    assert "Keep it clear of what no launch may see" in opencode.detail
    assert [req.command[0] for req in runner.sandboxed] == [str(host["bin"] / "claude")]


def _tcp(*entries: tuple[str, str]) -> str:
    header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt\n"
    lines = [
        f"   {i}: {local} 00000000:0000 {state} 00000000:00000000 00:00000000 00000000\n"
        for i, (local, state) in enumerate(entries)
    ]
    return header + "".join(lines)


def test_loopback_and_wildcard_listeners_are_listed_and_x11_flagged(tmp_path, host):
    (host["proc_net"] / "tcp").write_text(
        _tcp(
            ("0100007F:1770", "0A"),  # 127.0.0.1:6000, X11 display :0
            ("00000000:1F90", "0A"),  # 0.0.0.0:8080
            ("0201A8C0:0016", "0A"),  # 192.168.1.2:22, not reachable as loopback
            ("0100007F:0050", "01"),  # established, not a listener
        )
    )
    (host["proc_net"] / "tcp6").write_text(
        _tcp(("00000000000000000000000001000000:1538", "0A"))  # [::1]:5432
    )
    row = doctor_sandbox.listeners_row(str(host["proc_net"]))
    assert row.label == "WARN"
    assert "127.0.0.1 port 6000" in row.detail and "0.0.0.0 port 8080" in row.detail
    assert "::1 port 5432" in row.detail
    assert "192.168" not in row.detail and "port 80," not in row.detail
    assert "X11 port(s) 6000" in row.detail
    (host["proc_net"] / "tcp").write_text(_tcp(("00000000:1F90", "0A")))
    row = doctor_sandbox.listeners_row(str(host["proc_net"]))
    assert row.ok and "0.0.0.0 port 8080" in row.detail


def test_outside_a_repository_the_layout_rows_are_skipped(tmp_path, host):
    plain = tmp_path / "plain"
    plain.mkdir()
    rows = _rows(_doctor(plain, host).sandbox_checks(local=True))
    assert rows["hard links in writable trees"].label == "SKIP"
    assert rows["sandbox directories"].label == "SKIP"


# -- REMOTE's rows (ADR 0009 A.2.3 E, #386) -------------------------------------
# Each test below is the REMOTE replacement of a LOCAL one above: the same row
# through ``sandbox_checks(local=False)``, which ``run_all`` (``autoforge
# doctor``) appends, with REMOTE's facts: its launch classes, the profiles a
# REMOTE run reaches, its issue worktrees and per-issue sandbox directories,
# and its denied roots, the operator's checkout among them. The LOCAL
# originals stay until LOCAL is removed (#387, #388).


def _remote_doctor(repo: Path, host, monkeypatch, **kwargs) -> Doctor:
    """``_doctor`` run from the checkout, as ``autoforge doctor`` is.

    A REMOTE run's state directory (``.autoforge`` by default, or a relative
    ``--state-dir`` or ``state_dir``) is relative to the working directory,
    and is one of the run's denied roots.
    """
    monkeypatch.chdir(repo)
    return _doctor(repo, host, **kwargs)


def _issue_worktree(repo: Path, where: Path) -> Path:
    """An issue worktree at ``where``, detached at the checkout's ``HEAD``, as REMOTE adds one."""
    git_out("-C", str(repo), "worktree", "add", "-q", "--detach", str(where), "HEAD")
    return where


def test_remote_doctor_shows_every_sandbox_row_and_never_calls_github(tmp_path, host, monkeypatch):
    """ADR 0009 A.2.3 E (#386): the REMOTE replacement of
    test_doctor_sandbox.py::test_local_doctor_shows_every_sandbox_row_and_never_calls_github.

    REMOTE's policy rows are its own launch classes: an agent in its issue
    worktree, over the shared object store, and an export command, whose
    network is ``sandbox.premerge_network``. Each provider row names the
    profiles a REMOTE run reaches, ``replan_reexecute`` and ``update_epic``
    among them, which no LOCAL run does. The sandbox rows call no ``gh``:
    ``run_all``'s GitHub rows are its own.
    """
    repo = git_repo(tmp_path / "repo")
    runner = Runner()
    d = _remote_doctor(repo, host, monkeypatch, runner=runner)
    d.config.sandbox.premerge_network = "isolated"
    results = d.sandbox_checks(local=False)
    assert all(r.name.startswith("outer sandbox: ") for r in results)
    rows = _rows(results)
    assert rows["capability (one minimal sandboxed launch)"].detail == (
        "supported (bubblewrap 0.0-test)"
    )
    assert rows["mode (required)"].ok
    assert not any("LOCAL" in name for name in rows)
    agent, export = rows["policy, REMOTE agent"], rows["policy, export command"]
    assert "writable: the issue worktree" in agent.detail
    assert "overlay: the shared object store" in agent.detail
    assert "every other worktree and the controller checkout" in agent.detail
    assert "network: the host's" in agent.detail  # an agent never runs without one
    assert "writable: the exported tree" in export.detail
    assert "network: isolated" in export.detail
    providers = {name: r for name, r in rows.items() if name.startswith("provider ")}
    assert sorted(providers) == [
        "provider claude 'claude' (analyze_execute, fix)",
        "provider opencode 'opencode' (review_round_1, review_round_2_5, review_round_6_plus, "
        "replan_reexecute, update_epic)",
    ]
    assert all(r.ok for r in providers.values()), [(n, r.detail) for n, r in providers.items()]
    # Each provider's --version ran inside the sandbox, by its resolved path.
    assert sorted(req.command[0] for req in runner.sandboxed) == [
        str(host["bin"] / "claude"),
        str(host["bin"] / "opencode"),
    ]
    assert all(req.command[1:] == ["--version"] for req in runner.sandboxed)
    claude = next(r for name, r in providers.items() if "'claude'" in name)
    assert "credential env: ANTHROPIC_API_KEY" in claude.detail
    assert "inside the sandbox: fake-cli 1.2.3" in claude.detail
    # Paths and classes only: never a value.
    for r in results:
        assert SECRET not in r.detail and SECRET not in r.name
    assert not any(c[0] == "gh" for c in runner.commands)


def test_a_token_across_the_version_clip_leaves_no_fragment_in_remote(
    tmp_path, host, monkeypatch, capsys
):
    """ADR 0009 A.2.3 E (#386): the REMOTE replacement of
    test_doctor_sandbox.py::test_a_token_across_the_version_clip_leaves_no_fragment.

    The version line is redacted before it is clipped (PR #231 R2-F3), in
    every provider row a REMOTE run reaches.
    """
    token = "ghp_" + "A1b2C3d4" * 4
    prefix = "x" * (doctor_sandbox.VERSION_CHARS - 8) + " "
    assert len(prefix) + 7 == doctor_sandbox.VERSION_CHARS
    repo = git_repo(tmp_path / "repo")
    runner = Runner(stdout=f"{prefix}{token} trailing\nsecond line\n")
    d = _remote_doctor(repo, host, monkeypatch, runner=runner)
    results = d.sandbox_checks(local=False)
    providers = [r for r in results if r.name.startswith("outer sandbox: provider ")]
    assert len(providers) == 2 and all(r.ok for r in providers)
    assert all("inside the sandbox: xxx" in r.detail for r in providers)
    for as_json in (False, True):
        cli._report_checks(results, as_json, "doctor")
        out = capsys.readouterr().out
        assert "ghp_" not in out and "A1b" not in out
        assert "second line" not in out


def test_an_unsupported_capability_is_named_for_a_remote_run(
    tmp_path, host, monkeypatch, sandbox_probe
):
    """ADR 0009 A.2.3 E (#386): the REMOTE replacement of
    test_doctor_sandbox.py::test_an_unsupported_capability_is_named.

    REMOTE by design: the mode row offers ``disabled`` alone as the way
    out, never ``preferred``, which a REMOTE run treats as required.
    """
    sandbox_probe.unsupported(Capability.USER_NAMESPACE, "userns disabled by sysctl")
    repo = git_repo(tmp_path / "repo")
    runner = Runner()
    rows = _rows(_remote_doctor(repo, host, monkeypatch, runner=runner).sandbox_checks(local=False))
    capability = rows["capability (one minimal sandboxed launch)"]
    assert capability.label == "WARN"
    assert "an unprivileged user namespace (userns disabled by sysctl)" in capability.detail
    mode = rows["mode (required)"]
    assert mode.label == "FAIL" and "sandbox.mode: disabled" in mode.detail
    assert "preferred" not in mode.detail, mode.detail
    # No sandboxed launch on a host that cannot enforce; the declarations still compile.
    assert runner.sandboxed == []
    providers = [r for n, r in rows.items() if n.startswith("provider")]
    assert len(providers) == 2
    assert all("the declaration compiles" in r.detail for r in providers)


def test_a_dir_rw_login_the_policy_refuses_is_named_with_the_alternatives_in_remote(
    tmp_path, host, monkeypatch
):
    """ADR 0009 A.2.3 E (#386): the REMOTE replacement of
    test_doctor_sandbox.py::test_a_dir_rw_login_the_policy_refuses_is_named_with_the_alternatives.

    D7.3: ``CLAUDE_CONFIG_DIR`` in ``~/.claude`` is the operator's own root
    (D7.2), refused in every REMOTE launch of Claude Code.
    """
    root = host["home"] / ".claude"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    repo = git_repo(tmp_path / "repo")
    runner = Runner()
    rows = _rows(_remote_doctor(repo, host, monkeypatch, runner=runner).sandbox_checks(local=False))
    (claude,) = [r for n, r in rows.items() if n.startswith("provider claude")]
    assert claude.label == "FAIL"
    assert "every launch of it is refused" in claude.detail
    assert "dir-rw login may not be the provider's own configuration root" in claude.detail
    assert "(file-ro)" in claude.detail and "(env)" in claude.detail
    assert [req.command[0] for req in runner.sandboxed] == [str(host["bin"] / "opencode")]


def test_an_operator_opencode_config_dir_is_replaced_as_at_every_remote_launch(
    tmp_path, host, monkeypatch
):
    """ADR 0009 A.2.3 E (#386): the REMOTE replacement of
    test_doctor_sandbox.py::test_an_operator_opencode_config_dir_is_replaced_as_at_every_launch.

    The probe runs in the launch's own isolation (PR #231 R2-F2): the
    operator's ``OPENCODE_CONFIG_DIR`` (their configuration root) is replaced
    with an empty directory of the launch's own, which exists while the
    probe runs and is removed with it.
    """
    own = host["home"] / ".config" / "opencode"
    own.mkdir(parents=True)
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(own))
    repo = git_repo(tmp_path / "repo")
    runner = Runner()
    rows = _rows(_remote_doctor(repo, host, monkeypatch, runner=runner).sandbox_checks(local=False))
    (opencode,) = [r for n, r in rows.items() if n.startswith("provider opencode")]
    assert opencode.ok and "inside the sandbox: fake-cli 1.2.3" in opencode.detail, opencode.detail
    req, present = runner.launch_of("opencode")
    config_dir = req.env["OPENCODE_CONFIG_DIR"]
    assert config_dir != str(own) and present[config_dir]
    assert req.env["OPENCODE_CONFIG_PROJECT_DISABLE"] == "1"
    assert f"writable {config_dir}" in opencode.detail
    assert not os.path.exists(config_dir)


def test_a_linked_claude_config_dir_is_spelled_where_it_leads_inside_in_remote(
    tmp_path, host, monkeypatch
):
    """ADR 0009 A.2.3 E (#386): the REMOTE replacement of
    test_doctor_sandbox.py::test_a_linked_claude_config_dir_is_spelled_where_it_leads_inside.

    ``CLAUDE_CONFIG_DIR`` names a link in the home, which the private home
    does not recreate: the probe's CLI is given the directory it leads to
    (D8.2), and the launch's ``CLAUDE_ENV_FILE`` exists while it runs.
    """
    real = Path(os.path.realpath(tmp_path)) / "claude-autoforge"
    real.mkdir()
    alias = host["home"] / ".claude-autoforge"
    alias.symlink_to(real)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(alias))
    repo = git_repo(tmp_path / "repo")
    runner = Runner()
    rows = _rows(_remote_doctor(repo, host, monkeypatch, runner=runner).sandbox_checks(local=False))
    (claude,) = [r for n, r in rows.items() if n.startswith("provider claude")]
    assert claude.ok, claude.detail
    req, present = runner.launch_of("claude")
    env = dict(req.sandbox.env_set)
    assert env["CLAUDE_CONFIG_DIR"] == str(real)
    assert req.sandbox.inside_path(str(real)) == str(real)
    assert present[req.env["CLAUDE_ENV_FILE"]]


@pytest.mark.skipif(not os.path.exists("/bin/sh"), reason="no /bin/sh on this host")
def test_a_shebang_through_a_link_is_reached_as_written_in_remote(tmp_path, host, monkeypatch):
    """ADR 0009 A.2.3 E (#386): the REMOTE replacement of
    test_doctor_sandbox.py::test_a_shebang_through_a_link_is_reached_as_written.

    #225: a CLI whose ``#!`` names a link to the shell in a directory no
    root projects. The ``--version`` launch is compiled with that link
    recreated, and the row names the spelled path.
    """
    shell = os.path.realpath("/bin/sh")
    alias = Path(os.path.realpath(tmp_path)) / "alias" / "bin" / "sh"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(shell)
    (host["bin"] / "claude").write_text(f"#!{alias}\necho fake-cli 1.2.3\n")
    repo = git_repo(tmp_path / "repo")
    runner = Runner()
    rows = _rows(_remote_doctor(repo, host, monkeypatch, runner=runner).sandbox_checks(local=False))
    (claude,) = [r for n, r in rows.items() if n.startswith("provider claude")]
    assert claude.ok, claude.detail
    assert f"reached as written, through its links: {alias}" in claude.detail
    req, _ = runner.launch_of("claude")
    assert Link(str(alias), shell) in req.sandbox.mounts
    assert req.sandbox.resolve_inside(str(alias)) == shell


@pytest.mark.parametrize("holder", ["default", "execution.worktree_dir"])
def test_hard_links_in_the_remote_issue_worktrees_are_listed_by_path(
    tmp_path, host, monkeypatch, holder
):
    """ADR 0009 A.2.3 E (#386): the REMOTE replacement of
    test_doctor_sandbox.py::test_hard_links_in_the_local_workspace_are_listed_by_path.

    REMOTE by design: the trees a REMOTE agent writes are its issue
    worktrees, under ``<git common dir>/autoforge/worktrees`` or
    ``execution.worktree_dir``. A hard link there is listed; one in the
    operator's checkout is not, since no REMOTE launch sees the checkout.
    """
    base = Path(os.path.realpath(tmp_path))
    repo = git_repo(base / "repo")
    if holder == "default":
        worktrees, configured = repo / ".git" / "autoforge" / "worktrees", ""
    else:
        worktrees, configured = base / "issue-worktrees", "../issue-worktrees"
    worktree = _issue_worktree(repo, worktrees / "7")
    outside = base / "outside.txt"
    outside.write_text("shared inode\n")
    os.link(outside, worktree / "linked.txt")
    (worktree / "plain.txt").write_text("alone\n")
    os.link(outside, repo / "in-the-checkout.txt")
    d = _remote_doctor(repo, host, monkeypatch)
    d.config.execution.worktree_dir = configured
    row = _rows(d.sandbox_checks(local=False))["hard links in writable trees"]
    assert row.label == "WARN"
    assert str(worktree / "linked.txt") in row.detail
    assert "plain.txt" not in row.detail and "in-the-checkout.txt" not in row.detail
    assert "shared inode" not in row.detail
    assert "remove a REMOTE worktree or sandbox directory to have it recreated" in row.detail


def test_the_remote_sandbox_directories_and_their_sizes_are_listed(tmp_path, host, monkeypatch):
    """ADR 0009 A.2.3 E (#386): the REMOTE replacement of
    test_doctor_sandbox.py::test_the_sandbox_directories_and_their_sizes_are_listed.

    A REMOTE run keeps one sandbox directory per issue,
    ``<git common dir>/autoforge/sandbox/<issue>`` (ADR 0005 D8.4): each is
    listed with its size and scanned for hard links.
    """
    repo = git_repo(Path(os.path.realpath(tmp_path)) / "repo")
    sandbox = repo / ".git" / "autoforge" / "sandbox"
    (sandbox / "7" / "cache").mkdir(parents=True)
    (sandbox / "7" / "cache" / "blob").write_bytes(b"x" * 2048)
    (sandbox / "8" / "objects").mkdir(parents=True)
    rows = _rows(_remote_doctor(repo, host, monkeypatch).sandbox_checks(local=False))
    sizes = rows["sandbox directories"]
    assert sizes.ok
    assert f"{sandbox / '7'} 2.0 KiB" in sizes.detail and f"{sandbox / '8'} 0 B" in sizes.detail
    # Each sandbox directory is one of the trees scanned for hard links.
    scanned = rows["hard links in writable trees"]
    assert (
        scanned.ok
        and str(sandbox / "7") in scanned.detail
        and str(sandbox / "8") in (scanned.detail)
    )


def test_remote_operator_paths_over_a_denied_root_are_named(tmp_path, host, monkeypatch):
    """ADR 0009 A.2.3 E (#386): the REMOTE replacement of
    test_doctor_sandbox.py::test_operator_paths_over_a_denied_root_are_named.

    REMOTE's denied roots (D13.2): the controller's directory under the git
    common dir, the state directory, every registered worktree, and, by
    design, the operator's checkout itself, which a LOCAL launch works in
    and no REMOTE launch sees.
    """
    base = Path(os.path.realpath(tmp_path))
    repo = git_repo(base / "repo")
    (repo / "docs").mkdir()
    worktree = _issue_worktree(repo, base / "issue-worktrees" / "7")
    d = _remote_doctor(repo, host, monkeypatch)
    name = "operator read-only paths (sandbox.read_only_paths)"
    for path, named in (
        (repo / ".git" / "autoforge", None),
        (repo / ".autoforge", repo / ".autoforge"),  # the state directory
        (repo / "docs", repo),  # the checkout
        (worktree, worktree),
    ):
        d.config.sandbox.read_only_paths = [str(path)]
        row = _rows(d.sandbox_checks(local=False))[name]
        assert row.label == "FAIL" and "overlaps" in row.detail, (path, row.detail)
        if named is not None:
            assert f"overlaps {str(named)!r}" in row.detail, (path, row.detail)
    d.config.sandbox.read_only_paths = [str(base / "opt")]
    row = _rows(d.sandbox_checks(local=False))[name]
    assert row.ok and "readable by every tool in the sandbox" in row.detail


# What a REMOTE run denies a provider declaration: its state directory, by
# (``--state-dir``, ``state_dir``), each relative to the working directory as
# the CLI gives them; and the operator's checkout. Each row: the two settings,
# the directory the declaration names and the denied root the refusal names,
# both relative to the checkout. The default state, ``<checkout>/.autoforge``,
# lies in the checkout, itself a REMOTE denied root, and the compiler names
# the outer of the two (it holds them sorted).
_REMOTE_DENIED = {
    "default state": (None, None, ".autoforge", "."),
    "relative --state-dir": ("../cli-state", None, "../cli-state", "../cli-state"),
    "relative state_dir": (None, "../config-state", "../config-state", "../config-state"),
    "the checkout": (None, None, "opencode-data", "."),
}


@pytest.mark.parametrize("where", sorted(_REMOTE_DENIED))
def test_a_provider_declaration_over_a_remote_denied_root_is_refused_before_its_launch(
    tmp_path, host, monkeypatch, where
):
    """ADR 0009 A.2.3 E (#386): the REMOTE replacement of
    test_doctor_sandbox.py::test_a_provider_declaration_over_the_state_is_refused_before_its_launch.

    A provider probe compiles with the REMOTE run's denied roots (PR #231
    R2-F1): OpenCode's database directory (``dir-rw``) in the state
    directory, or anywhere in the operator's checkout, would show it to
    every launch. The row fails and its ``--version`` is never launched.
    """
    explicit, configured, directory, denied = _REMOTE_DENIED[where]
    repo = git_repo(Path(os.path.realpath(tmp_path)) / "repo")
    state = repo / directory
    state.mkdir(parents=True)
    monkeypatch.setenv("OPENCODE_DB", str(state / "opencode.db"))
    runner = Runner()
    d = _remote_doctor(repo, host, monkeypatch, runner=runner, state_dir=explicit)
    if configured:
        d.config.state_dir = configured
    rows = _rows(d.sandbox_checks(local=False))
    (opencode,) = [r for n, r in rows.items() if n.startswith("provider opencode")]
    assert opencode.label == "FAIL" and "every launch of it is refused" in opencode.detail
    assert f"lies in the denied root {os.path.realpath(repo / denied)}" in opencode.detail
    assert "Keep it clear of what no launch may see" in opencode.detail
    assert "in REMOTE the operator's checkout" in opencode.detail
    assert [req.command[0] for req in runner.sandboxed] == [str(host["bin"] / "claude")]


def test_outside_a_repository_the_remote_layout_rows_are_skipped(tmp_path, host, monkeypatch):
    """ADR 0009 A.2.3 E (#386): the REMOTE replacement of
    test_doctor_sandbox.py::test_outside_a_repository_the_layout_rows_are_skipped."""
    plain = tmp_path / "plain"
    plain.mkdir()
    rows = _rows(_remote_doctor(plain, host, monkeypatch).sandbox_checks(local=False))
    assert rows["hard links in writable trees"].label == "SKIP"
    assert rows["sandbox directories"].label == "SKIP"
