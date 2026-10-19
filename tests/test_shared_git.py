"""Shared-Git critical sections (ADR 0008 D12; #180), with real git.

Every controller write to git's shared data in the common dir holds the
shared-Git lock (L1) and runs with automatic maintenance off, and so does the
REMOTE creation of the sandbox directories there; read-only operations and
the agents' own commits take no L1.
Each claim of D12.5's table is one test here, run with concurrent real
processes in temporary repositories, and each ends with ``git fsck --full``
clean.

Processes start together through a pipe barrier, never through a sleep.
Each child reports the monotonic interval of every L1 section it held
(CLOCK_MONOTONIC is system-wide), and the test proves no two overlapped.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

from autoforge import locking
from autoforge.config import default_config
from autoforge.engine import ControllerEngine
from autoforge.executor import ExecutionRequest, ExecutionResult, execute
from autoforge.git_transport import NO_AUTO_MAINTENANCE
from autoforge.locking import SharedGitLock
from autoforge.premerge import export_commit_tree
from autoforge.safefs import SafeRoot
from tests.conftest import GIT_IDENT, ISSUE, FakeGitHub, git_repo, implement, make_engine

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# What every child shares: the import path, the barrier and a SharedGitLock
# that reports each section it held.
PRELUDE = textwrap.dedent(
    """
    import os, sys, time
    sys.path.insert(0, sys.argv[1])
    sys.path.insert(0, sys.argv[2])
    from autoforge import locking

    class Timed(locking.SharedGitLock):
        def acquire(self):
            lock = super().acquire()
            self.entered = time.monotonic()
            return lock

        def release(self):
            if self.held:
                print("section", self.entered, time.monotonic(), flush=True)
            super().release()

    def barrier():
        os.write(int(os.environ["READY_FD"]), b"r")
        os.read(int(os.environ["GO_FD"]), 1)
    """
)

ENSURE_WORKTREE = PRELUDE + textwrap.dedent(
    """
    import autoforge.engine as engine_mod
    from tests.conftest import make_engine

    repo, state_dir, issue = sys.argv[3:6]
    engine_mod.SharedGitLock = Timed
    eng = make_engine(state_dir, ["never"], workdir=repo)
    eng.state.current_issue_url = f"https://github.com/owner/repo/issues/{issue}"
    barrier()
    print("worktree", eng._ensure_agent_worktree(), flush=True)
    """
)

FETCH = PRELUDE + textwrap.dedent(
    """
    from pathlib import Path
    from autoforge.git_transport import GitRemote, GitTransport

    common, origin, gh, sha = sys.argv[3:7]

    def no_walk(base, sha):
        raise AssertionError("a fetch walks no range")

    transport = GitTransport(
        object_directory=Path(common) / "objects",
        remote=GitRemote(url=f"file://{origin}"),
        gh_command=gh,
        in_base_history=no_walk,
        git_lock=lambda operation: Timed(common, operation=operation),
    )
    barrier()
    transport.fetch([sha])
    print("fetched", sha, flush=True)
    """
)

IMPORT = PRELUDE + textwrap.dedent(
    """
    from pathlib import Path
    from autoforge.agent_worktree import import_private_objects

    common, admin, private = (Path(arg) for arg in sys.argv[3:6])
    barrier()
    head = import_private_objects(
        common, admin, private, None,
        git_lock=lambda operation: Timed(common, operation=operation),
    )
    print("imported", head, flush=True)
    """
)

AGENT_COMMITS = PRELUDE + textwrap.dedent(
    """
    import subprocess

    worktree, count = sys.argv[3], int(sys.argv[4])
    ident = ["-c", "user.name=agent", "-c", "user.email=agent@example.invalid"]
    barrier()
    for i in range(count):
        name = f"{os.path.basename(worktree)}-{i}.txt"
        with open(os.path.join(worktree, name), "w") as handle:
            handle.write(f"{name}\\n" * 50)
        for attempt in range(5):
            add = subprocess.run(
                ["git", "-C", worktree, "add", name], capture_output=True, text=True
            )
            done = add.returncode == 0 and subprocess.run(
                ["git", "-C", worktree, *ident, "commit", "-q", "-m", name],
                capture_output=True,
                text=True,
            )
            if done and done.returncode == 0:
                break
            failed = done if done else add
            print("failure", " ".join(failed.stderr.split()), flush=True)
        else:
            sys.exit(f"{name} was never committed")
    print("committed", count, flush=True)
    """
)

SANDBOX_DIRS = PRELUDE + textwrap.dedent(
    """
    from autoforge.engine import ControllerEngine

    from pathlib import Path

    anchors, key = [Path(a) for a in sys.argv[3].split(os.pathsep)], sys.argv[4]
    barrier()
    for anchor in anchors:
        made = ControllerEngine._sandbox_dirs(
            None,
            anchor,
            f"autoforge/sandbox/{key}",
            ("objects", "overlay-work", "cache"),
            git_lock=lambda operation: Timed(anchor, operation=operation),
        )
        print("made", made, flush=True)
    """
)


def _git(*args: str, cwd: Path | None = None) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout
    return out.strip()


def _commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content)
    _git("-C", str(repo), "add", name)
    _git("-C", str(repo), *GIT_IDENT, "commit", "-q", "-m", f"add {name}")
    return _git("-C", str(repo), "rev-parse", "HEAD")


def _fsck(git_dir: Path) -> None:
    res = subprocess.run(
        ["git", "--git-dir", str(git_dir), "fsck", "--full", "--no-dangling"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert not re.search(r"^(error|missing|broken|bad)", res.stdout + res.stderr, re.M)


def _run_together(scripts: list[list[str]]) -> list[list[str]]:
    """Start every child, release them at once, return each one's output lines.

    Each child gets the barrier's two descriptors (``READY_FD``, ``GO_FD``)
    and runs past it only once all of them are ready.
    """
    ready_r, ready_w = os.pipe()
    go_r, go_w = os.pipe()
    env = {**os.environ, "READY_FD": str(ready_w), "GO_FD": str(go_r)}
    children = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(SRC), str(ROOT), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            pass_fds=(ready_w, go_r),
        )
        for script, *args in scripts
    ]
    os.close(ready_w)
    os.close(go_r)
    try:
        arrived = 0
        while arrived < len(children):
            chunk = os.read(ready_r, len(children))
            assert chunk, "a child ended before the barrier"
            arrived += len(chunk)
        os.write(go_w, b"g" * len(children))
        results = [child.communicate(timeout=300) for child in children]
    finally:
        os.close(ready_r)
        os.close(go_w)
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()
    for child, (_, err) in zip(children, results, strict=True):
        assert child.returncode == 0, err
    return [out.splitlines() for out, _ in results]


def _sections(outputs: list[list[str]]) -> list[tuple[float, float]]:
    """Every L1 section the children reported, by start."""
    spans = (line.split()[1:] for lines in outputs for line in lines if line.startswith("section "))
    return sorted((float(start), float(end)) for start, end in spans)


def _assert_disjoint(sections: list[tuple[float, float]], expected: int) -> None:
    assert len(sections) == expected
    for (_, end), (start, _) in zip(sections, sections[1:], strict=False):
        assert end <= start, f"two L1 sections overlapped: {sections}"


def _worktree_entries(common: Path) -> dict[str, str]:
    """``git worktree list --porcelain``: worktree path -> HEAD."""
    out = _git("--git-dir", str(common), "worktree", "list", "--porcelain")
    entries: dict[str, str] = {}
    for block in out.split("\n\n"):
        fields = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
        entries[fields["worktree"]] = fields.get("HEAD", "")
    return entries


def _fake_gh(tmp_path: Path) -> Path:
    gh = tmp_path / "bin" / "gh"  # a file:// remote never asks it for a credential
    gh.parent.mkdir(exist_ok=True)
    gh.write_text("#!/bin/sh\nexit 1\n")
    gh.chmod(0o755)
    return gh


# -- D12.5's table ---------------------------------------------------------------------


def test_l1_concurrent_worktree_add_for_different_issues(tmp_path):
    """Four engines create four issues' worktrees at once: each check-then-create
    holds L1 alone, and the registry and the store stay consistent."""
    repo = git_repo(tmp_path / "repo")
    head = _commit(repo, "a.txt", "a\n")
    common = Path(os.path.realpath(repo / ".git"))
    issues = (2, 3, 4, 5)
    outputs = _run_together(
        [[ENSURE_WORKTREE, str(repo), str(tmp_path / f"state-{n}"), str(n)] for n in issues]
    )
    _assert_disjoint(_sections(outputs), expected=len(issues))
    made = {line.split(" ", 1)[1] for lines in outputs for line in lines if line.startswith("work")}
    expected = {str(common / "autoforge" / "worktrees" / str(n)) for n in issues}
    assert made == expected
    entries = _worktree_entries(common)
    assert set(entries) == expected | {str(Path(os.path.realpath(repo)))}
    assert all(entries[path] == head for path in expected)
    for path in expected:
        assert _git("-C", path, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"  # detached
    _fsck(common)


def test_l1_concurrent_controller_fetches_into_one_common_dir(tmp_path):
    """Four transports fetch four commits at once into one store: each fetch
    holds L1 alone, every object arrives, and no ref or FETCH_HEAD is made."""
    origin = git_repo(tmp_path / "origin")
    shas = []
    for n in range(4):
        _git("-C", str(origin), "checkout", "-q", "-b", f"b{n}", "main")
        shas.append(_commit(origin, f"f{n}.txt", f"{n}\n" * 2000))
    local = git_repo(tmp_path / "local")
    common = Path(os.path.realpath(local / ".git"))
    refs_before = _git("--git-dir", str(common), "for-each-ref")
    gh = _fake_gh(tmp_path)
    outputs = _run_together([[FETCH, str(common), str(origin), str(gh), sha] for sha in shas])
    _assert_disjoint(_sections(outputs), expected=len(shas))
    for sha in shas:
        assert _git("--git-dir", str(common), "cat-file", "-t", sha) == "commit"
    assert _git("--git-dir", str(common), "for-each-ref") == refs_before
    assert not (common / "FETCH_HEAD").exists()
    _fsck(common)


def _issue_worktree(base: Path) -> dict[str, Path]:
    """A repository with issue 2's worktree and its private object directory."""
    root = git_repo(base / "repo")
    common = Path(os.path.realpath(root / ".git"))
    worktree = common / "autoforge" / "worktrees" / "2"
    _git("-C", str(root), "worktree", "add", "-q", "--detach", str(worktree), "HEAD")
    private = common / "autoforge" / "sandbox" / "2" / "objects"
    private.mkdir(parents=True)
    return {
        "root": root,
        "common": common,
        "worktree": worktree,
        "admin": common / "worktrees" / "2",
        "private": private,
    }


def _commit_privately(paths: dict[str, Path], name: str) -> str:
    """What an enforced agent does: its new objects land in its private directory."""
    shared = str(paths["common"] / "objects").replace("\\", "\\\\").replace('"', '\\"')
    env = {
        **os.environ,
        "GIT_OBJECT_DIRECTORY": str(paths["private"]),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": f'"{shared}"',
    }
    worktree = str(paths["worktree"])
    (paths["worktree"] / name).write_text(f"{name}\n" * 500)
    for args in (["add", name], [*GIT_IDENT, "commit", "-q", "-m", f"add {name}"]):
        subprocess.run(["git", "-C", worktree, *args], check=True, env=env, capture_output=True)
    return subprocess.run(
        ["git", "-C", worktree, "rev-parse", "HEAD"],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_l1_import_concurrent_with_read_only_export(tmp_path):
    """The strict import writes the store under L1 while a byte-exact export of
    another commit reads it, at once: the export's proof holds, the import
    lands, the store stays sound. The export takes no L1: it completes while
    L1 is held."""
    paths = _issue_worktree(tmp_path)
    root, common = paths["root"], paths["common"]
    for n in range(40):
        (root / f"src-{n}.txt").write_text(f"{n}\n" * 200)
    _git("-C", str(root), "add", ".")
    _git("-C", str(root), *GIT_IDENT, "commit", "-q", "-m", "many files")
    exported = _git("-C", str(root), "rev-parse", "HEAD")
    for round_ in range(3):
        head = _commit_privately(paths, f"agent-{round_}.txt")
        ready_r, ready_w = os.pipe()
        go_r, go_w = os.pipe()
        importer = subprocess.Popen(
            [sys.executable, "-c", IMPORT, str(SRC), str(ROOT)]
            + [str(common), str(paths["admin"]), str(paths["private"])],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "READY_FD": str(ready_w), "GO_FD": str(go_r)},
            pass_fds=(ready_w, go_r),
        )
        os.close(ready_w)
        os.close(go_r)
        try:
            assert os.read(ready_r, 1) == b"r"
            os.write(go_w, b"g")
            with export_commit_tree(execute, str(root), exported) as tree:
                assert sorted(p.name for p in tree.root.iterdir()) == sorted(
                    f"src-{n}.txt" for n in range(40)
                )
                assert (tree.root / "src-7.txt").read_text() == "7\n" * 200
            out, err = importer.communicate(timeout=300)
        finally:
            os.close(ready_r)
            os.close(go_w)
            if importer.poll() is None:
                importer.kill()
                importer.wait()
        assert importer.returncode == 0, err
        assert f"imported {head}" in out
        assert len(_sections([out.splitlines()])) == 1
        assert _git("--git-dir", str(common), "cat-file", "-t", head) == "commit"
    with SharedGitLock(common, operation="stand in for another controller's write"):
        with export_commit_tree(execute, str(root), exported) as tree:
            assert (tree.root / "src-0.txt").exists()
    _fsck(common)


def test_concurrent_agent_commits_on_distinct_worktrees(tmp_path):
    """The `disabled` residual (D12.4): agents committing into the shared store
    from distinct detached worktrees at once lose no commit, and a failure, if
    any, is a transient git lock error that a retry got past."""
    repo = git_repo(tmp_path / "repo")
    base = _commit(repo, "a.txt", "a\n")
    common = Path(os.path.realpath(repo / ".git"))
    worktrees = [tmp_path / f"agent-{n}" for n in range(3)]
    for worktree in worktrees:
        _git("-C", str(repo), "worktree", "add", "-q", "--detach", str(worktree), "HEAD")
    outputs = _run_together([[AGENT_COMMITS, str(worktree), "20"] for worktree in worktrees])
    for lines in outputs:
        assert lines[-1] == "committed 20"
        for line in lines[:-1]:
            assert re.search(r"lock|Unable to create", line), f"not a transient failure: {line}"
    for worktree in worktrees:
        assert _git("-C", str(worktree), "rev-list", "--count", f"{base}..HEAD") == "20"
        assert _git("-C", str(worktree), "status", "--porcelain") == ""
    assert _sections(outputs) == [], "an agent's commit takes no L1"
    _fsck(common)


def test_l1_concurrent_sandbox_dirs_for_different_keys(tmp_path):
    """Eight issues prepare their sandbox directories at once in each of five
    fresh common dirs, racing on the shared `autoforge/sandbox` parent: in each
    common dir every creation, that parent's included, holds L1 alone, and
    every call succeeds."""
    anchors = []
    for n in range(5):
        anchor = tmp_path / f"common-{n}"
        anchor.mkdir()
        anchors.append(str(anchor))
    keys = [str(key) for key in range(2, 10)]
    outputs = _run_together([[SANDBOX_DIRS, os.pathsep.join(anchors), key] for key in keys])
    sections: dict[str, list[tuple[float, float]]] = {anchor: [] for anchor in anchors}
    for lines in outputs:
        # Each call reports its section as it releases L1, then what it made.
        assert len(lines) == 2 * len(anchors)
        for section, made in zip(lines[::2], lines[1::2], strict=True):
            assert section.startswith("section ") and made.startswith("made ")
            _, start, end = section.split()
            sections[str(Path(made.split(" ", 1)[1]).parents[2])].append((float(start), float(end)))
    for anchor in anchors:
        _assert_disjoint(sorted(sections[anchor]), expected=len(keys))
        assert (Path(anchor) / "autoforge" / "git.lock").is_file()
        sandbox = Path(anchor) / "autoforge" / "sandbox"
        assert sorted(p.name for p in sandbox.iterdir()) == sorted(keys)
        for key in keys:
            assert sorted(p.name for p in (sandbox / key).iterdir()) == [
                "cache",
                "objects",
                "overlay-work",
            ]


def test_l1_writes_disable_automatic_maintenance(tmp_path):
    """D12.3, verified against the installed git: in a store past
    `gc.autoPackLimit`, a fetch repacks it unless NO_AUTO_MAINTENANCE is given."""
    origin = git_repo(tmp_path / "origin")
    shas = [_commit(origin, f"f{n}.txt", f"{n}\n") for n in range(4)]

    def store_past_the_limit(name: str) -> Path:
        local = tmp_path / name
        _git("init", "-q", "-b", "main", str(local))
        for sha in shas[:3]:  # three packs
            _git(
                "-C",
                str(local),
                "-c",
                "fetch.unpackLimit=1",
                *NO_AUTO_MAINTENANCE,
                "fetch",
                "-q",
                "--no-auto-gc",
                f"file://{origin}",
                sha,
            )
        for key, value in (
            ("gc.autoPackLimit", "1"),
            ("gc.autoDetach", "false"),
            ("maintenance.autoDetach", "false"),
        ):
            _git("-C", str(local), "config", key, value)
        return local

    def packs(local: Path) -> int:
        return len(list((local / ".git" / "objects" / "pack").glob("*.pack")))

    def fetch(local: Path, *options: str) -> None:
        _git(
            "-C",
            str(local),
            "-c",
            "fetch.unpackLimit=1",
            *options,
            "fetch",
            "-q",
            f"file://{origin}",
            shas[3],
        )

    plain = store_past_the_limit("plain")
    assert packs(plain) == 3
    fetch(plain)
    assert packs(plain) == 1, "this git repacks after a fetch past the limit"
    guarded = store_past_the_limit("guarded")
    fetch(guarded, *NO_AUTO_MAINTENANCE)
    assert packs(guarded) == 4, "NO_AUTO_MAINTENANCE kept it from repacking"
    _fsck(guarded / ".git")


def test_every_common_dir_write_holds_l1(tmp_state_dir):
    """The fetch, the worktree check-then-create and the strict import's
    index-pack each run under L1 and with automatic maintenance off; the
    agent runs with no L1 held."""
    gh = FakeGitHub()
    gh.add_issue(ISSUE, "Feature")
    held_by_agent: list[str] = []

    def agent(req):
        held_by_agent.append(locking._shared_git_holder)
        return implement(req)

    eng = make_engine(tmp_state_dir, agent, github=gh, origin=True)
    eng.step()  # INITIALIZING
    seen: list[tuple[list[str], str]] = []
    real = eng._runner

    def recording(req: ExecutionRequest) -> ExecutionResult:
        seen.append((list(req.command), locking._shared_git_holder))
        return (real or execute)(req)

    eng._runner = recording
    assert eng.step().next_phase == "REVIEW"
    worktree = eng.agent_worktree_path()
    base = _git("-C", str(worktree), "rev-parse", "HEAD")
    private = eng._private_objects()
    private.mkdir(parents=True)
    common = Path(os.path.realpath(eng.coordination_root()))
    head = _commit_privately(
        {
            "common": common,
            "worktree": worktree,
            "private": private,
        },
        "agent.txt",
    )
    assert eng._import_agent_objects(worktree, exclude=(base,)) == head

    writes = {
        "fetch": [c for c, _ in seen if "fetch" in c],
        "worktree add": [c for c, _ in seen if _has(c, ["worktree", "add"])],
        "index-pack": [c for c, _ in seen if "index-pack" in c],
    }
    assert all(writes.values()), {k: len(v) for k, v in writes.items()}
    for command, holder in seen:
        if any(command in commands for commands in writes.values()):
            assert holder, f"{command} ran without L1"
            subcommand = next(w for w in ("fetch", "worktree", "index-pack") if w in command)
            assert _has(command[: command.index(subcommand)], list(NO_AUTO_MAINTENANCE))
    sections = {
        "check or create the agent worktree",
        "import the agent's objects into the shared store",
    }
    assert all(h in sections or h.startswith("fetch ") for _, h in seen if h)
    assert held_by_agent == [""]
    assert locking._shared_git_holder == ""


def test_remote_sandbox_dirs_are_created_under_l1_and_the_agent_runs_without_it(
    tmp_state_dir, monkeypatch
):
    """An enforced REMOTE launch creates its sandbox directories in the common
    dir, the shared `autoforge/sandbox` parent first, under L1 (D12.1), and
    the agent it launches next holds none."""
    gh = FakeGitHub()
    gh.add_issue(ISSUE, "Feature")
    held_by_agent: list[str] = []

    def agent(req):
        held_by_agent.append(locking._shared_git_holder)
        return implement(req)

    created: list[tuple[str, str]] = []
    real_ensure_dir = SafeRoot.ensure_dir

    def ensure_dir(root: SafeRoot, relpath: str) -> None:
        created.append((relpath, locking._shared_git_holder))
        real_ensure_dir(root, relpath)

    monkeypatch.setattr(SafeRoot, "ensure_dir", ensure_dir)
    cfg = default_config()
    cfg.sandbox.mode = "required"
    eng = make_engine(tmp_state_dir, agent, github=gh, cfg=cfg, origin=True)
    eng.step()  # INITIALIZING
    sandbox = Path(os.path.realpath(eng.coordination_root())) / "autoforge" / "sandbox"
    assert not sandbox.exists()
    assert eng.step().next_phase == "REVIEW"
    made = [(relpath, holder) for relpath, holder in created if relpath.startswith("autoforge/")]
    assert [relpath for relpath, _ in made] == [
        f"autoforge/sandbox/2/{name}" for name in ("objects", "overlay-work", "cache")
    ]
    assert {holder for _, holder in made} == {"prepare the issue's sandbox directories"}
    assert sorted(p.name for p in (sandbox / "2").iterdir()) == ["cache", "objects", "overlay-work"]
    assert held_by_agent == [""]
    assert locking._shared_git_holder == ""


def _has(command: list[str], words: list[str]) -> bool:
    return any(command[i : i + len(words)] == words for i in range(len(command)))


# The git subcommands that write git's data, and the one function each may run
# in: the critical sections of ADR 0008 D12.1. A git argv naming one anywhere
# else is a new common-dir write that #180's rule says must take L1 (D12.3).
GIT_WRITES = {
    "fetch",
    "index-pack",
    "worktree",
    "update-ref",
    "gc",
    "repack",
    "prune",
    "maintenance",
    "commit",
    "commit-tree",
    "hash-object",
    "mktree",
    "write-tree",
    "unpack-objects",
    "pack-refs",
    "push",
    "update-index",
    "checkout",
    "reset",
}
GUARDED = {
    ("git_transport.py", "fetch"): "fetch",
    ("agent_worktree.py", "import_private_objects"): "index-pack",
    ("engine.py", "_ensure_agent_worktree_unlocked"): "worktree",
    # A private GIT_DIR and no local ref: nothing in the common dir (D12.1).
    ("git_transport.py", "push"): "push",
}


def _git_subcommands(path: Path) -> list[tuple[str, str, int]]:
    """(enclosing function, subcommand, line) of every list literal naming a write."""
    found = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_List(self, node: ast.List) -> None:
            words = [
                e.value
                for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
            rest = [w for w in words if not w.startswith("-")]
            if rest and rest[0] in GIT_WRITES and len(words) > 1:
                found.append((self.stack[-1] if self.stack else "", rest[0], node.lineno))
            self.generic_visit(node)

    Visitor().visit(ast.parse(path.read_text(encoding="utf-8")))
    return found


def test_every_common_dir_write_in_the_source_is_a_guarded_section():
    """The inventory of D12.1, re-derived from the source: a controller git
    argv naming a writing subcommand appears only in a section that holds L1
    (or in the push, which writes a private GIT_DIR)."""
    found = {}
    for path in sorted((SRC / "autoforge").glob("*.py")):
        for function, subcommand, line in _git_subcommands(path):
            found.setdefault((path.name, function), set()).add((subcommand, line))
    stray = {
        f"{name}:{line} {function}() runs git {subcommand}"
        for (name, function), uses in found.items()
        for subcommand, line in uses
        if GUARDED.get((name, function)) != subcommand
    }
    assert not stray, sorted(stray)
    assert set(found) == set(GUARDED), "every guarded section still has its write"


def test_sandbox_dirs_is_a_plain_function_of_its_arguments(tmp_path):
    """The sandbox test above calls it without an engine: it reads no engine state.
    Without a lock factory (LOCAL's own git directory) it takes no L1."""
    no_engine: ControllerEngine = None  # type: ignore[assignment]
    made = ControllerEngine._sandbox_dirs(no_engine, tmp_path, "autoforge/sandbox/7", ("objects",))
    assert made == tmp_path / "autoforge" / "sandbox" / "7"
    assert (made / "objects").is_dir()
    assert not (tmp_path / "autoforge" / "git.lock").exists()
