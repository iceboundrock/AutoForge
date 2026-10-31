"""The controller's git reader, mode-independent (``autoforge.git_workspace``).

REMOTE reads the checkout it runs in, and an agent worktree, through
:class:`~autoforge.git_workspace.GitWorkspace`: the agent worktree's root
and creation, the sandbox's facts, the candidate read, the checkout anchor
around an invocation and the uncommitted-work note. Until #386 those reads
were LOCAL's reader, built from ``config.local``, and their hardening was
tested only through it (``tests/test_local.py``, deleted with LOCAL, #388).
ADR 0009 C1 moved the reader out; the tests of its hardening moved with it
(A.2.3 A), and the last section fails if ``engine.workspace()`` reads any
configuration, or a REMOTE read asks the configuration for a key it does
not declare, ``local`` first among them.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from autoforge.config import AutoForgeConfig, default_config
from autoforge.engine import ControllerEngine, config_file_placement_problem
from autoforge.errors import VerificationError
from autoforge.executor import ExecutionResult, execute
from autoforge.git_transport import LOCAL_GIT_ENV_ALLOWLIST, LOCAL_GIT_SWITCHES
from autoforge.git_workspace import GitWorkspace, StatusEntry, uncommitted_paths
from autoforge.transitions import Phase

from .conftest import NO_AUTO_MAINTENANCE, git_repo, implement, make_engine

# Every read of the reader: the controller's hardened git process (ADR 0004
# D7.3, D7.5), taking no optional lock.
_GIT_READ = ["git", *LOCAL_GIT_SWITCHES, "--no-optional-locks"]

_STATUS = ["status", "--porcelain=v1", "-z", "--untracked-files=all"]


def _committed_repo(tmp_path: Path) -> Path:
    """A repository with one committed file, ``src/app.py``."""
    root = git_repo(tmp_path / "repo")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, *NO_AUTO_MAINTENANCE, "commit", "-q", "-m", "initial")
    return root


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=AutoForge tests",
            "-c",
            "user.email=tests@example.invalid",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _every_read(ws: GitWorkspace) -> None:
    """Each read the engine makes through the reader, once."""
    ws.root()
    ws.git_dirs()
    ws.head_sha()
    ws.branch()
    ws.status_entries()
    ws.dirty_paths()


def _failed(req, stderr: str, exit_code: int = 128) -> ExecutionResult:
    return ExecutionResult(
        command=list(req.command),
        cwd=req.cwd,
        exit_code=exit_code,
        stdout="",
        stderr=stderr,
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:01+00:00",
    )


# =============================================================================
# A.2.3 A: the hardened git reads REMOTE uses
# =============================================================================


def test_no_read_takes_the_optional_index_lock_or_refreshes_the_index(tmp_path):
    """A `git status` refreshes a stale index by default, rewriting `.git/index`.

    The reader runs against the operator's own live checkout, where an IDE
    or a concurrent `git` can hold `index.lock`, and against an agent
    worktree it only inspects: no read may contend for that lock or write
    the index. A tracked file touched without a change leaves the index
    stale, which a refreshing `git status` would rewrite.

    ADR 0009 A.2.3 A (#386): the mode-neutral replacement of
    tests/test_local.py::test_workspace_git_reads_do_not_take_the_optional_index_lock.
    """
    root = _committed_repo(tmp_path)
    app = root / "src" / "app.py"
    st = app.stat()
    os.utime(app, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    index = root / ".git" / "index"
    before = (index.stat().st_mtime_ns, index.read_bytes())
    seen: list[list[str]] = []

    def runner(req):
        seen.append(list(req.command))
        return execute(req)

    ws = GitWorkspace(root, runner=runner)
    _every_read(ws)
    assert uncommitted_paths(root, runner) == []
    assert ws.status_entries() == []  # a touched file with its bytes unchanged
    assert len(seen) >= 7, seen
    for cmd in seen:
        assert cmd[: len(_GIT_READ)] == _GIT_READ, cmd
    assert (index.stat().st_mtime_ns, index.read_bytes()) == before, "a read rewrote the index"
    assert not (root / ".git" / "index.lock").exists()


def test_reads_run_no_fsmonitor_hold_no_credential_and_ignore_the_operators_git_dir(
    tmp_path, tmp_path_factory, monkeypatch
):
    """ADR 0004 D7.3 (PR #195 R3-F2): the reader is a controller git process.

    `git status` would run the repository's configured file-system monitor,
    which an agent can set, with the controller's whole environment, GitHub
    token included; and an operator's `GIT_DIR` would redirect every read
    to another repository. The reads report the same paths and anchor
    either way: the hardening changes what runs, not what is read.

    ADR 0009 A.2.3 A (#386): the mode-neutral replacement of
    tests/test_local.py::test_workspace_git_reads_run_no_fsmonitor_and_hold_no_credential.
    """
    root = _committed_repo(tmp_path)
    (root / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    planted = tmp_path_factory.mktemp("planted")  # outside the working tree
    monitor = planted / "fsmonitor"
    monitor.write_text(f'#!/bin/sh\nenv > "{planted}/fsmonitor-ran"\n')
    monitor.chmod(0o755)
    _git(root, "config", "core.fsmonitor", str(monitor))
    decoy = git_repo(planted / "decoy")
    head = _git(root, "rev-parse", "HEAD")
    monkeypatch.setenv("GH_TOKEN", "ghp_controllertokenvalue")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    requests = []

    def runner(req):
        requests.append(req)
        return execute(req)

    ws = GitWorkspace(root, runner=runner)
    assert ws.dirty_paths() == ["src/app.py"]
    assert ws.root() == Path(os.path.realpath(root))
    assert ws.head_sha() == head and ws.branch() == "main"
    assert ws.git_dirs() == (Path(os.path.realpath(root / ".git")),)
    assert not (planted / "fsmonitor-ran").exists(), "the repository's fsmonitor ran"
    assert requests
    for req in requests:
        assert req.command[: len(_GIT_READ)] == _GIT_READ
        assert req.env_allowlist == LOCAL_GIT_ENV_ALLOWLIST
        assert "GH_TOKEN" not in LOCAL_GIT_ENV_ALLOWLIST and "GIT_DIR" not in (req.env or {})
        assert (req.env or {})["GIT_NO_REPLACE_OBJECTS"] == "1"


def test_uncommitted_paths_is_one_hardened_status_read_and_never_guesses(tmp_path):
    """#189: the status read behind an agent failure's worktree line.

    It reads any working tree (a REMOTE agent worktree is not the
    controller's checkout), lists untracked files one by one, takes no
    optional lock, and refuses rather than answers when git cannot read a
    working tree there.

    ADR 0009 A.2.3 A (#386): the mode-neutral replacement of
    tests/test_local.py::test_uncommitted_paths_is_one_hardened_status_read_and_never_guesses.
    """
    repo = git_repo(tmp_path / "repo")
    (repo / "notes").mkdir()
    for name in ("draft.py", "notes/plan.md", "notes/todo.md"):
        (repo / name).write_text("work in progress\n", encoding="utf-8")
    requests = []

    def runner(req):
        requests.append(req)
        return execute(req)

    assert uncommitted_paths(repo, runner) == ["draft.py", "notes/plan.md", "notes/todo.md"]
    (req,) = requests
    assert req.command == [*_GIT_READ, *_STATUS]
    assert req.cwd == str(repo) and req.env_allowlist == LOCAL_GIT_ENV_ALLOWLIST
    # Inside a git directory there is no working tree to read.
    with pytest.raises(VerificationError, match="failed in"):
        uncommitted_paths(repo / ".git")


def test_status_entries_keep_each_code_and_read_a_rename_or_a_copy_as_one_record(tmp_path):
    """#187: the read a write phase's prompt describes the agent worktree with.

    A rename and a copy (`status.renames=copies`) carry their source in a
    second field, which is never read as a record of its own; the paths
    `uncommitted_paths` counts come from the same records, sources included.

    ADR 0009 A.2.3 A (#386): the mode-neutral replacement of
    tests/test_local.py::test_status_entries_keep_each_code_and_read_a_rename_or_a_copy_as_one_record.
    """
    listing = (
        " M src/app.py\0"
        "?? notes/new file.md\0"
        "R  src/new.py\0src/old.py\0"
        "C  src/copy.py\0src/app.py\0"
        "A  added.py\0"
    )
    requests = []

    def fake_git(req):
        requests.append(req)
        return ExecutionResult(req.command, req.cwd, 0, listing, "", "t", "t")

    ws = GitWorkspace(tmp_path, runner=fake_git)
    assert ws.status_entries() == [
        StatusEntry(" M", "src/app.py"),
        StatusEntry("??", "notes/new file.md"),
        StatusEntry("R ", "src/new.py", "src/old.py"),
        StatusEntry("C ", "src/copy.py", "src/app.py"),
        StatusEntry("A ", "added.py"),
    ]
    assert requests[0].command == [*_GIT_READ, *_STATUS]
    assert ws.dirty_paths() == [
        "added.py",
        "notes/new file.md",
        "src/app.py",
        "src/copy.py",
        "src/new.py",
        "src/old.py",
    ]

    # The same records from real git: a staged rename and copy.
    root = _committed_repo(tmp_path / "real")
    _git(root, "config", "status.renames", "copies")
    _git(root, "mv", "src/app.py", "src/main.py")
    (root / "src" / "copy.py").write_text("def main():\n    pass\n", encoding="utf-8")
    _git(root, "add", "-A")
    entries = GitWorkspace(root).status_entries()
    assert StatusEntry("R ", "src/main.py", "src/app.py") in entries
    assert all(e.code[0] in "RCA" and e.path.startswith("src/") for e in entries), entries
    assert {e.orig_path for e in entries} <= {"", "src/app.py"}


def test_a_truncated_or_timed_out_read_is_refused_never_parsed(tmp_path):
    """#53: a listing past the capture bound is head + marker + tail, not the
    working tree, and a read that timed out answered nothing; every read
    refuses either rather than reading it as an answer.

    ADR 0009 A.2.3 A (#386): the mode-neutral replacement of
    tests/test_local.py::test_local_workspace_refuses_truncated_git_output.
    """
    root = _committed_repo(tmp_path)

    def truncated(req):
        return replace(execute(req), stdout_truncated=True)

    def timed_out(req):
        return replace(execute(req), timed_out=True)

    reads = {
        "root": lambda ws: ws.root(),
        "git_dirs": lambda ws: ws.git_dirs(),
        "head_sha": lambda ws: ws.head_sha(),
        "branch": lambda ws: ws.branch(),
        "status_entries": lambda ws: ws.status_entries(),
    }
    for read in reads.values():
        with pytest.raises(VerificationError, match="truncated"):
            read(GitWorkspace(root, runner=truncated))
        with pytest.raises(VerificationError, match="timed out"):
            read(GitWorkspace(root, runner=timed_out))
    with pytest.raises(VerificationError, match="truncated"):
        uncommitted_paths(root, truncated)


def test_a_failed_anchor_read_is_refused_and_only_exit_1_is_a_fact(tmp_path):
    """The checkout anchor a REMOTE invocation is pinned to is HEAD and the
    branch: a read that failed (git's 128) is not evidence they did not
    move, so it raises; exit 1 is the fact it always was (an unborn HEAD, a
    detached HEAD)."""
    root = _committed_repo(tmp_path)
    head_argv = ["rev-parse", "--verify", "--quiet", "HEAD"]
    branch_argv = ["symbolic-ref", "--quiet", "--short", "HEAD"]

    def breaking(argv):
        def runner(req):
            if req.command[len(_GIT_READ) :] == argv:
                return _failed(req, "fatal: bad object HEAD")
            return execute(req)

        return runner

    with pytest.raises(VerificationError, match="cannot read HEAD"):
        GitWorkspace(root, runner=breaking(head_argv)).head_sha()
    with pytest.raises(VerificationError, match="cannot read the checked-out branch"):
        GitWorkspace(root, runner=breaking(branch_argv)).branch()

    _git(root, "checkout", "-q", "--detach")
    assert GitWorkspace(root).branch() == ""
    unborn = tmp_path / "empty"
    subprocess.run(["git", "init", "-q", str(unborn)], check=True)
    assert GitWorkspace(unborn).head_sha() == ""


# =============================================================================
# ADR 0009 C1: `engine.workspace()` reads no configuration; no REMOTE read
# asks for a key the configuration does not declare
# =============================================================================

_CONFIG_KEYS = frozenset(f.name for f in dataclasses.fields(AutoForgeConfig))


def _watched(cfg: AutoForgeConfig) -> tuple[AutoForgeConfig, list[str]]:
    """A copy of ``cfg`` that records every key read of it, and every public
    name it does not declare (``getattr(cfg, "local", None)`` among them),
    which it refuses as the plain configuration would."""
    reads: list[str] = []

    class Watched(AutoForgeConfig):
        def __getattribute__(self, name: str):
            if name in _CONFIG_KEYS:
                reads.append(name)
            return object.__getattribute__(self, name)

        def __getattr__(self, name: str):
            if not name.startswith("_"):
                reads.append(name)
            raise AttributeError(name)

    watched = Watched.__new__(Watched)
    watched.__dict__.update(vars(cfg))
    assert reads == []
    watched.sandbox  # noqa: B018 - the watch itself is checked to record
    assert getattr(watched, "local", None) is None
    assert reads == ["sandbox", "local"]
    reads.clear()
    return watched, reads


def test_engine_workspace_reads_no_configuration(tmp_path):
    """`engine.workspace()` is REMOTE's git reader and reads no configuration.

    Before #386 it built LOCAL's reader from `config.local.exclude` and the
    two walk bounds for every run, so the `local:` key could not be removed
    while REMOTE still needed the reader (ADR 0009 C1). Every read the
    reader makes, and the checkout anchor read through it, reads no key.
    """
    root = _committed_repo(tmp_path)
    eng = ControllerEngine(config=default_config(), state_dir=tmp_path / "state", workdir=root)
    eng.config, reads = _watched(eng.config)
    ws = eng.workspace()
    assert type(ws) is GitWorkspace
    _every_read(ws)
    assert eng._checkout_anchor() == (_git(root, "rev-parse", "HEAD"), "main")
    assert reads == [], f"engine.workspace() read the configuration: {reads}"
    # The control: the worktree root, read through the reader, reads its key.
    assert eng.agent_worktree_root() == Path(os.path.realpath(root / ".git")) / (
        "autoforge/worktrees"
    )
    assert reads == ["execution"]


def test_the_config_file_placement_check_asks_for_no_undeclared_key(tmp_path):
    """`config_file_placement_problem` asks the git reader where the working
    trees are, and reads only keys the configuration declares to build it
    (ADR 0009 C1)."""
    root = _committed_repo(tmp_path)
    cfg = default_config()
    cfg.sandbox.mode = "required"
    cfg, reads = _watched(cfg)
    inside = root / "autoforge.yaml"
    inside.write_text("{}\n", encoding="utf-8")
    problem = config_file_placement_problem(cfg, str(inside), workdir=root)
    assert str(inside) in problem  # inside the checkout an agent launch writes
    outside = tmp_path / "operator" / "autoforge.yaml"
    outside.parent.mkdir()
    outside.write_text("{}\n", encoding="utf-8")
    assert config_file_placement_problem(cfg, str(outside), workdir=root) == ""
    assert "sandbox" in reads
    assert set(reads) <= _CONFIG_KEYS, f"the placement check asked for: {set(reads) - _CONFIG_KEYS}"


def test_a_remote_implementation_phase_asks_for_no_undeclared_key(tmp_state_dir, fake_github):
    """A whole REMOTE ANALYZE_EXECUTE (the agent worktree's root and
    creation, the checkout anchor around the launch, the worktree-state
    read for the prompt, the candidate read, the push and the PR) reads
    only keys the configuration declares: no `local:` key, which is gone
    (ADR 0009 C1, D12 S1a, S3)."""
    cfg, reads = _watched(default_config())
    eng = make_engine(tmp_state_dir, implement, github=fake_github, cfg=cfg, origin=True)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    assert eng.step().next_phase == "REVIEW"
    assert eng.state.phase == Phase.REVIEW and eng.state.current_pr_url
    (req,) = eng.provider.calls
    assert Path(req.cwd).parent == eng.agent_worktree_root()
    assert "local" not in _CONFIG_KEYS and reads
    assert set(reads) <= _CONFIG_KEYS, f"a REMOTE run asked for: {set(reads) - _CONFIG_KEYS}"
