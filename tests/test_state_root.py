"""The state root under a running REMOTE engine (ADR 0009 A.2.3 C, #386).

An engine opens its state directory once, by pathname, and holds the
descriptor for the rest of the run (:meth:`ControllerEngine.state_root`).
Every later read or write goes through that descriptor, and every later use
first proves the pathname still names it. So a state directory replaced
while an agent runs, by a symbolic link, a FIFO, a prepared directory or a
rename, is a refusal and never a redirection, and ``state.json`` is
published by name, never written through whatever the name reaches.

These are the REMOTE forms of the LOCAL tests of
``tests/test_durable_run_contract.py``, which LOCAL's removal deleted with
LOCAL's code (#388). ``tests/test_state.py`` has the engine's refusal of a
rename and of a link at ``state_root()`` without a run; these tests swap the
directory under a run whose agent is launched. The module also keeps that
file's shared tests: the structural guards on state access, the bounded
walk and ``create_exclusive``'s publication, and where a run's agents work.
"""

import ast
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import autoforge
from autoforge import cli
from autoforge.config import default_config
from autoforge.engine import ControllerEngine
from autoforge.errors import StateError
from autoforge.providers import ProviderRegistry, ScriptedProvider
from autoforge.run_namespace import issue_namespace
from autoforge.safefs import SafeRoot, UnsafePathError, WalkBudgetExceeded
from autoforge.state import STATE_FILENAME
from autoforge.transitions import Phase, WorkflowMode

from .conftest import (
    EPIC,
    ISSUE,
    FakeGitHub,
    Origin,
    attach_origin,
    connect_origin,
    git_repo,
    github_origin,
    implement,
    make_engine,
    state_tree,
)

# =============================================================================
# The state directory replaced while the agent runs
# =============================================================================


def _mid_run(tmp_path, swap):
    """A REMOTE run in ANALYZE_EXECUTE whose agent implements, then ``swap(state_dir, outside)``.

    The state directory is ``run/state`` beside the checkout, so its parent
    can be renamed without moving the repository, and the origin lives
    outside both.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.txt").write_text("operator's own file\n")
    state_dir = tmp_path / "run" / "state"

    def implement_then_swap(req):
        out = implement(req)
        swap(state_dir, outside)
        return out

    gh = FakeGitHub()
    eng = make_engine(
        state_dir, implement_then_swap, github=gh, workdir=git_repo(tmp_path / "repo")
    )
    remote = tmp_path / "remote"
    remote.mkdir()
    eng.origin = attach_origin(eng, remote)  # type: ignore[attr-defined]
    eng.save()
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    return eng, gh, state_dir, outside


def _swap_symlink(state_dir, outside):
    shutil.rmtree(state_dir)
    state_dir.symlink_to(outside)


def _swap_fifo(state_dir, outside):
    shutil.rmtree(state_dir)
    os.mkfifo(state_dir)


def _swap_directory(state_dir, outside):
    shutil.rmtree(state_dir)
    os.rename(outside, state_dir)
    outside.mkdir()  # so the "nothing arrived" check has a place to look


def _swap_rename_aside(state_dir, outside):
    os.rename(state_dir, state_dir.with_name("aside"))
    state_dir.mkdir()


def _swap_parent(state_dir, outside):
    parent = state_dir.parent
    os.rename(parent, parent.with_name("parent-aside"))
    state_dir.mkdir(parents=True)


STATE_ROOT_ROWS = [
    ("symlink-to-outside", _swap_symlink, "symbolic link"),
    ("fifo-at-the-name", _swap_fifo, "state directory .* replaced"),
    ("prepared-directory-moved-in", _swap_directory, "state directory .* replaced"),
    ("renamed-aside-and-recreated", _swap_rename_aside, "state directory .* replaced"),
    ("parent-renamed-and-recreated", _swap_parent, "state directory .* replaced"),
]


@pytest.mark.parametrize("name,swap,pattern", STATE_ROOT_ROWS, ids=[r[0] for r in STATE_ROOT_ROWS])
def test_a_remote_state_directory_replaced_mid_run_is_refused_and_reaches_nothing(
    tmp_path, name, swap, pattern
):
    """A state directory replaced while the agent ran stops the run; nothing follows it.

    The engine's next use of its held root refuses the pathname, so no state,
    log or read context reaches the replacement or the directory a link
    points to, the implementation is never checkpointed, and nothing is
    pushed or published: the save that journals the push comes first.

    ADR 0009 A.2.3 C (#386): the REMOTE replacement of
    tests/test_durable_run_contract.py::test_a_state_directory_replaced_mid_run_is_refused_and_reaches_nothing
    """
    eng, gh, state_dir, outside = _mid_run(tmp_path, swap)
    with pytest.raises(StateError, match=pattern):  # UnsafePathError is a StateError
        eng.run(max_steps=6, dry_run=False, allow_merge=False)
    assert len(eng.provider.calls) == 1, "the agent ran once, and nothing after it"
    # Nothing of the controller's reached the replacement or the outside: no
    # state, no log, no read context.
    assert sorted(p.name for p in outside.iterdir()) in ([], ["notes.txt"])
    if state_dir.is_dir() and not state_dir.is_symlink():
        assert {p.name for p in state_dir.iterdir()} <= {"notes.txt"}
    # The engine did not advance: the implementation was never checkpointed,
    # pushed or published.
    assert eng.state.phase == Phase.ANALYZE_EXECUTE
    assert gh.effect_writes == [] and gh.prs == {}
    assert eng.origin.head(eng.state.current_branch or "autoforge/2") is None
    eng.close()


# =============================================================================
# state.json itself: published by name, never written through
# =============================================================================


def test_a_remote_state_json_replaced_by_a_symlink_is_replaced_back_not_written_through(
    tmp_path,
):
    """A name is published, never a truncation of whatever the name reaches.

    ADR 0009 A.2.3 C (#386): the REMOTE replacement of
    tests/test_durable_run_contract.py::test_state_json_replaced_by_a_symlink_is_replaced_back_not_written_through
    """
    victim = tmp_path / "victim.txt"
    victim.write_text("do not overwrite\n")
    eng = make_engine(tmp_path / ".autoforge")
    eng.save()
    state_file = Path(eng.paths.state_file)
    state_file.unlink()
    state_file.symlink_to(victim)
    eng.save()
    assert victim.read_text() == "do not overwrite\n"
    assert not state_file.is_symlink()
    assert json.loads(state_file.read_text())["run_id"] == eng.state.run_id
    assert eng.load().mode == WorkflowMode.REMOTE
    eng.close()


def test_a_remote_state_json_hard_linked_elsewhere_keeps_the_old_bytes_at_the_other_link(
    tmp_path,
):
    """A save replaces the name; a second link to the old inode keeps the old bytes.

    ADR 0009 A.2.3 C (#386): the REMOTE replacement of
    tests/test_durable_run_contract.py::test_state_json_hard_linked_elsewhere_keeps_the_old_bytes_at_the_other_link
    """
    eng = make_engine(tmp_path / ".autoforge")
    eng.save()
    state_file = Path(eng.paths.state_file)
    other = tmp_path / "other-link.json"
    os.link(state_file, other)
    before = other.read_bytes()
    eng.state.step_count += 1
    eng.save()
    assert other.read_bytes() == before
    assert json.loads(state_file.read_text())["step_count"] == eng.state.step_count
    assert os.stat(other).st_nlink == 1
    eng.close()


def test_documented_limit_a_remote_state_directory_replaced_in_place_by_a_copy_is_not_detectable(
    tmp_path,
):
    """What the design does *not* promise, stated as a test so it cannot drift.

    Between two processes there is no held descriptor. A same-UID process
    that replaces the state directory *at the same pathname* with a directory
    holding a byte-identical ``state.json`` has produced exactly the input a
    legitimate reboot produces, so the next engine loads it. What the copy
    can contain is only what the run already was: any other change to it is
    the state file's own validation's to catch.

    ADR 0009 A.2.3 C (#386): the REMOTE replacement of
    tests/test_durable_run_contract.py::test_documented_limit_an_in_place_replacement_with_copied_contents_is_not_detectable
    """
    gh = FakeGitHub()
    first = make_engine(tmp_path / ".autoforge", github=gh)
    first.save()
    assert first.step().next_phase == "ANALYZE_EXECUTE"
    first.close()
    state_dir = Path(first.paths.state_dir)
    replacement = tmp_path / "replacement"
    shutil.copytree(state_dir, replacement)
    shutil.rmtree(state_dir)
    os.rename(replacement, state_dir)

    eng = ControllerEngine(default_config(), state_dir=state_dir, workdir=tmp_path, github=gh)
    eng.load()  # indistinguishable from a reboot: accepted, by design
    assert eng.state.phase == Phase.ANALYZE_EXECUTE
    assert eng.state.to_dict() == first.state.to_dict()
    eng.close()


# =============================================================================
# Bootstrap: the first open of the state directory
# =============================================================================


@pytest.fixture
def cli_fakes(monkeypatch, tmp_path, tmp_path_factory):
    """Every engine the CLI builds talks to a FakeGitHub and a scripted agent.

    The checkout is ``tmp_path/repo``; its origin is a bare repository
    outside it. A test asserts that neither is called.
    """
    repo = github_origin(git_repo(tmp_path / "repo"))
    gh = FakeGitHub()
    gh.add_issue(ISSUE, "Feature")
    origin = Origin.seeded(tmp_path_factory.mktemp("state-root-origin"), repo)
    gh.attach_origin(origin)
    provider = ScriptedProvider(lambda req: "")
    real = ControllerEngine.__init__

    def patched(self, *a, **k):
        k.setdefault("github", gh)
        k.setdefault(
            "providers", ProviderRegistry(overrides={"claude": provider, "opencode": provider})
        )
        real(self, *a, **k)
        connect_origin(self, origin)

    monkeypatch.setattr(ControllerEngine, "__init__", patched)
    monkeypatch.chdir(repo)
    return gh, provider


def _plant_state(directory: Path) -> None:
    """An operator's file where a link's target would hold ``state.json``.

    Unreadable as state, so ``run --force`` would quarantine it (rename it
    aside) if the bootstrap reached it through the link.
    """
    (directory / STATE_FILENAME).write_text("not state")


def test_bootstrap_does_not_follow_a_link_at_the_remote_state_directory(
    tmp_path, capsys, cli_fakes
):
    """``run`` and ``run --force`` refuse a state directory that is a symbolic link.

    The state directory is the one component of a standalone REMOTE state
    path that AutoForge opens ``O_NOFOLLOW`` from its parent's descriptor, so
    a link there is refused before anything is created, moved or quarantined
    through it, whether it was named by ``--state-dir`` or by
    ``config.state_dir``; no GitHub call is made and no agent is launched.

    A link at its *parent* is followed: a standalone REMOTE state directory's
    parent is the anchor :class:`autoforge.state.StatePaths` resolves by
    pathname, the operator's own directory. The REMOTE state directory that
    has an anchor further up, a scheduler namespace's, is the next test.

    ADR 0009 A.2.3 C (#386): the REMOTE replacement of
    tests/test_durable_run_contract.py::test_bootstrap_does_not_follow_a_link_at_the_state_directory_or_its_parent
    """
    gh, provider = cli_fakes
    outside = tmp_path / "outside"
    outside.mkdir()
    _plant_state(outside)
    before = state_tree(outside)
    link = tmp_path / "run" / "state"
    link.parent.mkdir()
    link.symlink_to(outside, target_is_directory=True)
    config = tmp_path / "autoforge.json"
    config.write_text(json.dumps({"version": 1, "state_dir": str(link)}), encoding="utf-8")
    run = ["run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"]
    for chosen in (["--state-dir", str(link)], ["--config", str(config)]):
        for extra in ([], ["--force"]):
            assert cli.main([*chosen, *run, *extra]) != 0, (chosen, extra)
            assert "symbolic link" in capsys.readouterr().err
            # Nothing was created, moved, quarantined or rewritten through the link.
            assert state_tree(outside) == before, (chosen, extra)
            assert link.is_symlink() and os.readlink(link) == str(outside)
    assert gh.calls == [] and gh.effect_writes == [] and provider.calls == []


@pytest.mark.parametrize("linked", ["state", "parent"])
def test_bootstrap_of_an_anchored_remote_state_directory_does_not_follow_a_link_below_the_anchor(
    tmp_path, linked
):
    """A REMOTE state directory anchored at the git common dir follows no link below it.

    A scheduler worker's engine runs on its issue namespace's state
    directory, opened component by component ``O_NOFOLLOW`` from the common
    dir's descriptor. A link planted at the state directory or at its parent
    is refused by every step of the worker's bootstrap: looking for an
    existing run, the ``--force`` quarantine and the first save. Nothing is
    created, moved or quarantined through it.

    ADR 0009 A.2.3 C (#386): the REMOTE replacement of
    tests/test_durable_run_contract.py::test_bootstrap_does_not_follow_a_link_at_the_state_directory_or_its_parent
    """
    repo = git_repo(tmp_path / "repo")
    common = Path(os.path.realpath(repo / ".git"))
    ns = issue_namespace(common, 2)
    outside = tmp_path / "outside"
    link = ns.state_dir if linked == "state" else ns.directory
    link.parent.mkdir(parents=True)
    # What the link's target holds where the bootstrap would look.
    target_state_dir = outside if linked == "state" else outside / ns.state_dir.name
    target_state_dir.mkdir(parents=True)
    _plant_state(target_state_dir)
    link.symlink_to(outside, target_is_directory=True)
    before = state_tree(outside)

    eng = make_engine(tmp_path / "unused", [], workdir=repo)
    eng.bind_issue_namespace(ns)
    with pytest.raises(UnsafePathError, match="symbolic link"):
        eng.existing_run()
    assert state_tree(outside) == before
    with pytest.raises(UnsafePathError, match="symbolic link"):
        eng.quarantine_state()
    assert state_tree(outside) == before
    eng.new_run(EPIC, ISSUE)
    with pytest.raises(UnsafePathError, match="symbolic link"):
        eng.save()
    assert state_tree(outside) == before
    assert not (tmp_path / "unused").exists()
    eng.close()


# =============================================================================
# Where a run's agents work
# =============================================================================


def test_a_remote_run_launches_agents_in_a_controller_owned_worktree(tmp_path):
    """GitHub is the run's source of truth; its agents run in a per-issue
    worktree under the git common dir, not in the operator's checkout (#10).

    Moved from tests/test_durable_run_contract.py when LOCAL's removal
    deleted that module (#388)."""
    git_repo(tmp_path)
    eng = make_engine(tmp_path / ".autoforge")
    assert eng.mode.value == "REMOTE"
    cwd = Path(eng._execution_cwd())
    assert cwd == tmp_path / ".git" / "autoforge" / "worktrees" / "2"
    assert cwd != Path(eng.workdir)
    listed = subprocess.run(
        ["git", "-C", str(tmp_path), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"worktree {cwd.resolve()}" in listed


# =============================================================================
# Structural guards: the architecture, not a scenario
# =============================================================================
#
# Moved from tests/test_durable_run_contract.py when LOCAL's removal deleted
# that module (#388).

SRC = Path(autoforge.__file__).parent


def _calls(tree: ast.AST, names: set[str]):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in names:
                yield name, node


def test_production_never_reads_or_writes_state_by_pathname_outside_state_py():
    """Every state.json read/write goes through a held root capability.

    ``save_state``/``load_state``/``quarantine_state_file``/``archive_state_file``
    accept a pathname for the *message*, and a ``root=`` capability for the
    *access*. Outside :mod:`autoforge.state` itself, a call without ``root=``
    would be a fresh pathname resolution -- exactly the rebinding that a
    replaced state directory exploits.
    """
    names = {"save_state", "load_state", "quarantine_state_file", "archive_state_file"}
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "state.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name, call in _calls(tree, names):
            if not any(kw.arg == "root" for kw in call.keywords):
                offenders.append(f"{path.relative_to(SRC)}:{call.lineno} {name}(...) without root=")
    assert offenders == []


def test_no_module_reinterprets_a_loaded_run_through_the_current_configuration():
    """``resume -> load state -> load today's config -> reinterpret`` is banned.

    The configuration is loaded exactly once per process, by the CLI, before
    any state is read; no production module calls ``load_config`` after
    loading state. (A test of shape rather than of one scenario.) A
    scheduler worker is a process of its own (``epic _worker``, #182): its
    entry point loads the configuration before it reads any state, and the
    worker tests pin that order.
    """
    for path in sorted(SRC.rglob("*.py")):
        if path.name in ("cli.py", "doctor.py", "config.py", "worker.py"):  # entry points
            continue
        text = path.read_text(encoding="utf-8")
        assert "load_config" not in text, f"{path.name} loads configuration"


# =============================================================================
# The bounded walk and create_exclusive's publication (safefs)
# =============================================================================
#
# Moved from tests/test_durable_run_contract.py when LOCAL's removal deleted
# that module (#388). The walk outlived LOCAL's workspace snapshot: the run
# log and the scheduler namespaces walk through it.


def test_the_entry_budget_is_enforced_while_listing_not_after_materialising(tmp_path):
    """A directory with N entries costs O(budget) to refuse, never O(N)."""
    root = tmp_path / "big"
    root.mkdir()
    for i in range(50):
        (root / f"f{i:03}").write_text("x")
    fs = SafeRoot.open(root)
    try:
        seen = []
        with pytest.raises(WalkBudgetExceeded) as exc:
            for entry in fs.walk(max_entries=10):
                seen.append(entry.relpath)
        assert seen == [], "entries were yielded before the budget was checked"
        assert exc.value.where == "."
    finally:
        fs.close()


def test_the_walk_is_iterative_so_depth_is_bounded_by_the_budget_not_the_stack(tmp_path):
    import sys

    root = tmp_path / "deep"
    p = root
    for _ in range(300):
        p = p / "d"
    p.mkdir(parents=True)
    fs = SafeRoot.open(root)
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(120)
    try:
        assert sum(1 for _ in fs.walk(max_entries=10_000)) == 300
    finally:
        sys.setrecursionlimit(old)
        fs.close()


def test_create_exclusive_publishes_by_link_so_a_failure_leaves_no_final_name(
    tmp_path, monkeypatch
):
    root = tmp_path / "r"
    root.mkdir()
    fs = SafeRoot.open(root)
    try:

        def boom(*a, **k):
            raise OSError(5, "injected")

        monkeypatch.setattr(os, "link", boom)
        with pytest.raises(StateError, match="cannot create"):
            fs.create_exclusive("new.txt", b"data")
        assert list(root.iterdir()) == [], "a temporary or a partial final name survived"
        monkeypatch.undo()

        victim = tmp_path / "victim"
        victim.write_text("keep")
        (root / "taken").symlink_to(victim)
        with pytest.raises(FileExistsError):
            fs.create_exclusive("taken", b"data")
        assert victim.read_text() == "keep"
        assert sorted(p.name for p in root.iterdir()) == ["taken"]
    finally:
        fs.close()


def test_a_create_exclusive_whose_write_fails_leaves_no_partial_file(tmp_path, monkeypatch):
    """R7-F5: a file made by ``create_exclusive`` is either whole or absent.

    The failure is injected at ``fsync`` -- after the bytes were written,
    before they were durable -- which is the moment a crash used to leave a
    truncated file that the retry then refused to replace.
    """
    root = tmp_path / "r"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "existing.md").write_text("keep\n")
    before = sorted(p.name for p in (root / "sub").iterdir())
    real_fsync = os.fsync

    def failing_fsync(fd):
        raise OSError(5, "injected I/O error")

    with SafeRoot.open(root) as fs:
        monkeypatch.setattr(os, "fsync", failing_fsync)
        with pytest.raises(StateError, match="cannot write .*sub/broken.md.*injected"):
            fs.create_exclusive("sub/broken.md", b"# whole\n")
        assert sorted(p.name for p in (root / "sub").iterdir()) == before, (
            "a partial file or a temporary survived the failed write"
        )
        monkeypatch.setattr(os, "fsync", real_fsync)
        # The name is free, so the retry creates the file whole.
        fs.create_exclusive("sub/broken.md", b"# whole\n")
    assert (root / "sub" / "broken.md").read_bytes() == b"# whole\n"
    assert sorted(p.name for p in (root / "sub").iterdir()) == sorted([*before, "broken.md"])
