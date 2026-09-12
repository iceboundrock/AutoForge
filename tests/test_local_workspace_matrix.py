"""Workspace identity as a matrix over repository shapes, not a list of bugs.

PR #44's first design asked git which files mattered (`git status
--porcelain --untracked-files=all`) and hashed those. Every review round then
found another shape git does not report, or reports as something no digest
can bind: an ignored file, `assume-unchanged`, `skip-worktree`,
`core.fileMode=false`, a symbolic link, a submodule directory, an unreadable
path. Each was closed with another branch in the same function.

The rewrite asks the filesystem instead. The controller walks the tree itself
and every entry it finds lands in exactly one of four states:

    included and hashed  |  represented by metadata
    excluded with an enforceable, disclosed reason  |  refused, fail-closed

There is deliberately no fifth state -- "git did not report it, so I did not
see it" -- and `test_the_classification_is_total` is what makes that a
property of the code rather than a claim in a docstring.

Each row below is `snapshot -> mutate -> snapshot` and asserts the outcome the
row's state requires. A row that a future git flag or filesystem feature adds
should need a row here and nothing else.
"""

import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from autoforge.errors import VerificationError
from autoforge.local_workspace import (
    KIND_DIR,
    KIND_EXCLUDED,
    KIND_FILE,
    KIND_LINK,
    LocalWorkspace,
)

from .conftest import commit_all, write_feature

CLOSED_KINDS = {KIND_FILE, KIND_DIR, KIND_LINK, KIND_EXCLUDED}


def base_repo(path) -> Path:
    """A committed repository: one source file, one feature spec, clean."""
    root = Path(path)
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    write_feature(root)
    commit_all(root, "base")
    return root


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True)


# -- the matrix ----------------------------------------------------------------
BOUND = "bound"  # in the fingerprint: a change to it changes the fingerprint
REFUSED = "refused"  # cannot be bound, so the snapshot fails closed
EXCLUDED = "excluded"  # deliberately outside the fingerprint, and disclosed


@dataclass
class Shape:
    name: str
    state: str
    setup: object  # (root) -> None
    mutate: object  # (root) -> None
    exclude: tuple[str, ...] = ()
    note: str = ""


def _write(rel: str, text: str):
    def go(root):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    return go


SHAPES = [
    Shape(
        "tracked-clean",
        BOUND,
        lambda root: None,
        _write("src/app.py", "VALUE = 2\n"),
        note="the case the old design handled; a change git reports",
    ),
    Shape(
        "tracked-dirty-at-baseline",
        BOUND,
        _write("src/app.py", "VALUE = 99\n"),
        _write("src/app.py", "VALUE = 100\n"),
        note="#45: with --allow-dirty the *contents* are pinned, not just the path",
    ),
    Shape(
        "tracked-staged",
        BOUND,
        lambda root: (_write("src/app.py", "VALUE = 3\n")(root), git(root, "add", "src/app.py")),
        _write("src/app.py", "VALUE = 4\n"),
    ),
    Shape(
        "untracked",
        BOUND,
        _write("src/new.py", "NEW = 1\n"),
        _write("src/new.py", "NEW = 2\n"),
    ),
    Shape(
        "ignored",
        BOUND,
        lambda root: (
            _write(".gitignore", "build/\n")(root),
            _write("build/out.js", "one\n")(root),
            commit_all(root, "ignore build"),
        ),
        _write("build/out.js", "two\n"),
        note="git status hides it entirely; the reviewer can still read it",
    ),
    Shape(
        "assume-unchanged",
        BOUND,
        lambda root: git(root, "update-index", "--assume-unchanged", "src/app.py"),
        _write("src/app.py", "VALUE = 5\n"),
        note="git is *told* to report it as clean, and does",
    ),
    Shape(
        "skip-worktree",
        BOUND,
        lambda root: git(root, "update-index", "--skip-worktree", "src/app.py"),
        _write("src/app.py", "VALUE = 6\n"),
    ),
    Shape(
        "core.filemode-false",
        BOUND,
        lambda root: git(root, "config", "core.fileMode", "false"),
        lambda root: (root / "src" / "app.py").chmod(0o755),
        note="git is configured not to see the executable bit at all",
    ),
    Shape(
        "mode-only-change",
        BOUND,
        lambda root: None,
        lambda root: (root / "src" / "app.py").chmod(0o755),
        note="no byte of content changes; the mode is part of identity",
    ),
    Shape(
        "clean-tracked-symlink",
        BOUND,
        lambda root: (
            (root / "src" / "alias.py").symlink_to("app.py"),
            commit_all(root, "add link"),
        ),
        lambda root: (
            (root / "src" / "alias.py").unlink(),
            (root / "src" / "alias.py").symlink_to("other.py"),
        ),
        note="the link *text* is the bound fact; git reports nothing here",
    ),
    Shape(
        "untracked-symlink",
        BOUND,
        lambda root: (root / "src" / "alias.py").symlink_to("app.py"),
        lambda root: (
            (root / "src" / "alias.py").unlink(),
            (root / "src" / "alias.py").symlink_to("app.py.bak"),
        ),
    ),
    Shape(
        "empty-directory",
        BOUND,
        lambda root: (root / "src" / "empty").mkdir(),
        _write("src/empty/inner.py", "x\n"),
        note="git cannot represent an empty directory; the walk can",
    ),
    Shape(
        "directory-mode",
        BOUND,
        lambda root: (root / "src" / "d").mkdir(mode=0o755),
        lambda root: (root / "src" / "d").chmod(0o700),
        note="a directory is represented by metadata, and that metadata is bound",
    ),
    Shape(
        "external-symlink",
        REFUSED,
        lambda root: (root / "src" / "outside.py").symlink_to(root.parent / "external.py"),
        lambda root: None,
        note="its bytes are readable by the reviewer and unbindable by us",
    ),
    Shape(
        "symlink-into-the-git-directory",
        REFUSED,
        lambda root: (root / "src" / "cfg").symlink_to(root / ".git" / "config"),
        lambda root: None,
        note="excluded targets are as unbindable as external ones",
    ),
    Shape(
        "symlink-into-an-operator-exclusion",
        REFUSED,
        lambda root: (
            _write("build/out.js", "one\n")(root),
            (root / "src" / "out.js").symlink_to(root / "build" / "out.js"),
        ),
        lambda root: None,
        exclude=("build",),
    ),
    Shape(
        "symlink-above-an-operator-exclusion",
        REFUSED,
        lambda root: (
            _write("build/out.js", "one\n")(root),
            (root / "src" / "escape").symlink_to(".."),
        ),
        lambda root: None,
        exclude=("build",),
        note="R9-F1: src/escape/build/out.js reads excluded bytes at an unexcluded path",
    ),
    Shape(
        "symlink-to-the-root",
        REFUSED,
        lambda root: (root / "src" / "top").symlink_to(root),
        lambda root: None,
        note="R9-F1: the root contains every exclusion, the git directory included",
    ),
    Shape(
        "symlink-to-a-directory-holding-no-exclusion",
        BOUND,
        lambda root: (
            _write("lib/util.py", "U = 1\n")(root),
            (root / "src" / "shared").symlink_to("../lib"),
        ),
        _write("lib/util.py", "U = 2\n"),
        exclude=("build",),
        note="R9-F1: the bytes reachable through the link are bound at their own path",
    ),
    Shape(
        "fifo",
        REFUSED,
        lambda root: os.mkfifo(root / "src" / "pipe"),
        lambda root: None,
    ),
    Shape(
        "nested-repository",
        REFUSED,
        lambda root: subprocess.run(["git", "init", "-q", str(root / "vendor")], check=True),
        lambda root: None,
    ),
    Shape(
        "gitdir",
        EXCLUDED,
        lambda root: None,
        lambda root: (root / ".git" / "scratch").write_text("x\n", encoding="utf-8"),
        note="identified by inode, not by the name '.git'",
    ),
    Shape(
        "operator-exclusion",
        EXCLUDED,
        _write("build/out.js", "one\n"),
        _write("build/out.js", "two\n"),
        exclude=("build",),
        note="declared unreviewed on purpose, and disclosed to the reviewer",
    ),
]


@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: s.name)
def test_workspace_shape_matrix(tmp_path, shape):
    """snapshot -> mutate -> snapshot, for every shape the tree can hold."""
    root = base_repo(tmp_path / "repo")
    (tmp_path / "external.py").write_text("EXTERNAL = 1\n", encoding="utf-8")
    shape.setup(root)
    ws = LocalWorkspace(workdir=root, exclude=shape.exclude)

    if shape.state == REFUSED:
        with pytest.raises(VerificationError) as exc:
            ws.snapshot()
        # A refusal must be actionable, not just loud: it names the entry it
        # could not bind and an exclusion the operator can declare instead.
        message = str(exc.value)
        assert "cannot bind the working tree" in message
        assert "exclude" in message, "a refusal must name the way out"
        return

    before = ws.snapshot()
    shape.mutate(root)
    after = ws.snapshot()

    if shape.state == BOUND:
        assert after.fingerprint != before.fingerprint, (
            f"{shape.name}: the tree changed and the fingerprint did not -- "
            "a reviewer's clean review would still be accepted"
        )
    elif shape.state == EXCLUDED:
        assert after.fingerprint == before.fingerprint
        # Excluded is not invisible: the reviewer is told.
        if shape.exclude:
            for pattern in shape.exclude:
                assert f"exclude:{pattern}" in before.describe_exclusions()
    else:  # pragma: no cover - guard against a typo in the matrix
        raise AssertionError(shape.state)


@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: s.name)
def test_the_classification_is_total(tmp_path, shape):
    """Every entry on disk is in the snapshot, in exactly one closed category.

    This is the invariant that replaced `git status`: the controller's own
    walk is the enumerator, so "the snapshot did not mention it" and "it is
    not there" are the same statement. A shape that cannot be classified is
    refused before a fingerprint exists, which is why the refusing rows return
    early here rather than being exempted.
    """
    root = base_repo(tmp_path / "repo")
    (tmp_path / "external.py").write_text("EXTERNAL = 1\n", encoding="utf-8")
    shape.setup(root)
    ws = LocalWorkspace(workdir=root, exclude=shape.exclude)
    if shape.state == REFUSED:
        with pytest.raises(VerificationError):
            ws.snapshot()
        return

    snap = ws.snapshot()
    classified = {e.path: e for e in snap.entries}
    assert len(classified) == len(snap.entries), "a path may be classified once"
    for entry in snap.entries:
        assert entry.kind in CLOSED_KINDS, entry

    # An independent enumeration of the same tree, not sharing a line of code
    # with the implementation. Everything it sees must be accounted for; the
    # only subtrees it may not reach into are the ones the snapshot marked
    # excluded.
    excluded_prefixes = [e.path for e in snap.entries if e.kind == KIND_EXCLUDED]

    def is_under_exclusion(rel: str) -> bool:
        return any(rel == p or rel.startswith(p + "/") for p in excluded_prefixes)

    seen = set()
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        # os.walk follows nothing by default for dirnames listing, but it does
        # descend into symlinked directories' names only if followlinks=True.
        for name in list(dirnames) + filenames:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if is_under_exclusion(rel):
                if rel in excluded_prefixes and name in dirnames:
                    dirnames.remove(name)
                continue
            seen.add(rel)
    # Guard against the meta-test passing because it walked nothing.
    assert len(seen) >= 3, "the independent walk found almost nothing; it is not testing"
    missing = seen - set(classified)
    assert not missing, (
        f"{shape.name}: entries on disk that the snapshot never mentioned: {missing}"
    )


def test_a_snapshot_is_stable_when_nothing_changes(tmp_path):
    """The converse of the matrix: identity must not be noisy.

    A fingerprint that moved on its own would make every review stale and the
    whole binding useless, so this is as load-bearing as the refusals.
    """
    root = base_repo(tmp_path / "repo")
    ws = LocalWorkspace(workdir=root)
    first = ws.snapshot()
    assert ws.snapshot().fingerprint == first.fingerprint
    # Reading the tree does not change it; neither does a second workspace
    # object, or a git command that only reads.
    subprocess.run(["git", "-C", str(root), "status", "--porcelain"], check=True)
    assert LocalWorkspace(workdir=root).snapshot().fingerprint == first.fingerprint


def test_a_commit_moves_the_anchor_and_not_the_tree_fingerprint(tmp_path):
    """The two facts are bound separately because they are two facts.

    Folding HEAD into the tree fingerprint would report a plain `git commit`
    -- which changes no byte of the working tree -- as "the reviewer modified
    the workspace", and an operator would learn to ignore that message.
    """
    root = base_repo(tmp_path / "repo")
    ws = LocalWorkspace(workdir=root)
    before = ws.snapshot()
    (root / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    dirty = ws.snapshot()
    assert dirty.fingerprint != before.fingerprint
    commit_all(root, "commit the change")
    committed = ws.snapshot()
    assert committed.fingerprint == dirty.fingerprint, "the bytes did not move"
    assert committed.anchor != dirty.anchor, "HEAD did"


def test_the_exclusion_rules_are_part_of_the_identity(tmp_path):
    """Two runs with different exclusions must never compare equal.

    Otherwise a fingerprint taken under `exclude: [build]` would satisfy a
    review taken under none, and the operator could not tell which promise
    they were holding.
    """
    root = base_repo(tmp_path / "repo")
    (root / "build").mkdir()
    (root / "build" / "out.js").write_text("one\n", encoding="utf-8")
    strict = LocalWorkspace(workdir=root).snapshot()
    loose = LocalWorkspace(workdir=root, exclude=("build",)).snapshot()
    looser = LocalWorkspace(workdir=root, exclude=("build", "dist")).snapshot()
    assert len({strict.fingerprint, loose.fingerprint, looser.fingerprint}) == 3
    assert "exclude:build" in loose.describe_exclusions()
    # The git directory is excluded unconditionally, so "no exclusions at all"
    # is not a reachable state -- what "strict" means is no *operator* ones.
    assert "exclude:" not in strict.describe_exclusions()
    assert "[gitdir]" in strict.describe_exclusions()


def test_a_path_containing_a_newline_cannot_be_confused_with_two_paths(tmp_path):
    """The fingerprint is length-prefixed, so no path can forge a record boundary."""
    root = base_repo(tmp_path / "repo")
    ws = LocalWorkspace(workdir=root)
    (root / "src" / "a\nb.py").write_text("x\n", encoding="utf-8")
    one = ws.snapshot()
    (root / "src" / "a\nb.py").unlink()
    (root / "src" / "a").mkdir()
    (root / "src" / "a" / "b.py").write_text("x\n", encoding="utf-8")
    assert ws.snapshot().fingerprint != one.fingerprint


def test_the_cost_bounds_fail_closed_rather_than_hashing_less(tmp_path):
    """#46: a tree too big to bind is refused, never bound approximately."""
    root = base_repo(tmp_path / "repo")
    tiny = LocalWorkspace(workdir=root, max_entries=3)
    with pytest.raises(VerificationError, match="more than 3 entries"):
        tiny.snapshot()
    small = LocalWorkspace(workdir=root, max_bytes=4)
    with pytest.raises(VerificationError, match="more than 4 bytes"):
        small.snapshot()
    # The refusal says where the weight is, so the operator can act on it.
    with pytest.raises(VerificationError) as exc:
        LocalWorkspace(workdir=root, max_entries=3).snapshot()
    assert "local.max_workspace_entries" in str(exc.value)


def test_snapshot_refuses_a_file_that_grows_during_hashing(tmp_path, monkeypatch):
    from autoforge import local_workspace as workspace_mod

    root = base_repo(tmp_path / "repo")
    real_open = workspace_mod.open_regular_at
    grown = False

    def open_then_grow(dir_fd, name, flags, **kwargs):
        nonlocal grown
        fd = real_open(dir_fd, name, flags, **kwargs)
        if kwargs.get("where") == "src/app.py" and not grown:
            grown = True
            with (root / "src" / "app.py").open("ab") as app:
                app.write(b"changed while reading\n")
        return fd

    monkeypatch.setattr(workspace_mod, "open_regular_at", open_then_grow)
    with pytest.raises(VerificationError, match="grew or shrank"):
        LocalWorkspace(workdir=root).snapshot()


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
def test_an_executable_bit_survives_a_round_trip_through_the_fingerprint(tmp_path):
    """Mode is metadata, and metadata that is bound must be bound exactly."""
    root = base_repo(tmp_path / "repo")
    ws = LocalWorkspace(workdir=root)
    (root / "src" / "app.py").chmod(0o644)
    plain = ws.snapshot()
    (root / "src" / "app.py").chmod(0o755)
    executable = ws.snapshot()
    assert plain.fingerprint != executable.fingerprint
    (root / "src" / "app.py").chmod(0o644)
    assert ws.snapshot().fingerprint == plain.fingerprint
    entry = next(e for e in executable.entries if e.path == "src/app.py")
    assert entry.kind == KIND_FILE
    assert stat.S_IXUSR & int(entry.mode, 8)
