"""The agent worktree's git surface (ADR 0005 D6.3-D6.5, #168).

Real repositories, real git: the binding and the registry listing read the
registry, the candidate read ignores everything the agent can write, and the
strict import copies an agent's commit out of a private object directory
into the shared store.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from autoforge import agent_worktree
from autoforge.agent_worktree import (
    Candidate,
    ObjectImportError,
    alternate_entry,
    bound_admin_dir,
    holds_objects,
    import_private_objects,
    private_objects_problem,
    read_candidate,
    registered_worktree_roots,
)
from autoforge.errors import SandboxTamperError, VerificationError
from autoforge.executor import ExecutionRequest, execute

from .conftest import GIT_IDENT, watched_fifo


def _git(*args: str, env: dict[str, str] | None = None) -> str:
    out = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    return out.stdout.strip()


def _make_repo(base: Path) -> dict[str, Path]:
    """A repository under ``base`` with an issue worktree and its private object directory."""
    root = base / "repo"
    root.mkdir()
    _git("init", "-q", "-b", "main", str(root))
    (root / "a.txt").write_text("a\n")
    _git("-C", str(root), "add", "a.txt")
    _git("-C", str(root), *GIT_IDENT, "commit", "-q", "-m", "initial")
    common = root / ".git"
    worktree = common / "autoforge" / "worktrees" / "2"
    _git("-C", str(root), "worktree", "add", "-q", "--detach", str(worktree), "HEAD")
    admin = common / "worktrees" / "2"
    private = common / "autoforge" / "sandbox" / "2" / "objects"
    private.mkdir(parents=True)
    return {
        "base": base,
        "root": root,
        "common": common,
        "worktree": worktree,
        "admin": admin,
        "private": private,
    }


@pytest.fixture
def repo(tmp_path) -> dict[str, Path]:
    return _make_repo(Path(os.path.realpath(tmp_path)))


# ------------------------------------------------------------------ binding


def test_the_registry_binds_the_worktree_to_its_admin_dir(repo):
    assert bound_admin_dir(repo["common"], repo["worktree"]) == repo["admin"]


def test_relative_worktree_pointers_bind_too(repo):
    worktree, admin = repo["worktree"], repo["admin"]
    (worktree / ".git").write_text(f"gitdir: {os.path.relpath(admin, worktree)}\n")
    (admin / "gitdir").write_text(f"{os.path.relpath(worktree / '.git', admin)}\n")
    assert bound_admin_dir(repo["common"], worktree) == admin


def test_a_rewritten_dotgit_is_refused_with_the_repair_command(repo, tmp_path):
    decoy = repo["base"] / "decoy"
    _git("init", "-q", str(decoy))
    (repo["worktree"] / ".git").write_text(f"gitdir: {decoy / '.git'}\n")
    with pytest.raises(VerificationError, match="git worktree repair"):
        bound_admin_dir(repo["common"], repo["worktree"])


def test_a_dotgit_directory_is_refused(repo):
    dotgit = repo["worktree"] / ".git"
    dotgit.unlink()
    dotgit.mkdir()
    with pytest.raises(VerificationError, match="not a regular file"):
        bound_admin_dir(repo["common"], repo["worktree"])


def test_two_registry_entries_naming_the_worktree_are_refused(repo):
    twin = repo["common"] / "worktrees" / "twin"
    twin.mkdir()
    (twin / "gitdir").write_text(f"{repo['worktree'] / '.git'}\n")
    with pytest.raises(VerificationError, match="more than one entry"):
        bound_admin_dir(repo["common"], repo["worktree"])


def test_an_unregistered_worktree_is_refused(repo):
    stray = repo["base"] / "stray"
    stray.mkdir()
    (stray / ".git").write_text(f"gitdir: {repo['admin']}\n")
    with pytest.raises(VerificationError, match="no entry"):
        bound_admin_dir(repo["common"], stray)


# ----------------------------------------------------------------- registry


def _listed_roots(cwd: Path) -> set[Path]:
    """The roots `git worktree list` registers, as the engine read them before
    #223: each resolved, a `prunable` one left out."""
    out = _git("-C", str(cwd), "worktree", "list", "--porcelain")
    roots: set[Path] = set()
    for entry in out.split("\n\n"):
        lines = [line.strip() for line in entry.splitlines() if line.strip()]
        if not lines or not lines[0].startswith("worktree "):
            continue
        if any(line == "prunable" or line.startswith("prunable ") for line in lines[1:]):
            continue
        roots.add(Path(os.path.realpath(lines[0][len("worktree ") :])))
    return roots


def test_the_registry_lists_the_roots_git_worktree_list_registers(repo):
    """The operator's checkout, the issue worktree and another linked one; a
    prunable entry (its tree is gone) left out, and a locked one whose tree
    is gone kept, as git keeps it: the roots `git worktree list` reports,
    asked from the checkout or from a linked worktree, read without git."""
    root, base = repo["root"], repo["base"]
    for name in ("other", "gone", "locked"):
        _git("-C", str(root), "worktree", "add", "-q", "--detach", str(base / name), "HEAD")
    _git("-C", str(root), "worktree", "lock", str(base / "locked"))
    shutil.rmtree(base / "gone")
    shutil.rmtree(base / "locked")
    roots = registered_worktree_roots(repo["common"])
    assert roots == {root, repo["worktree"], base / "other", base / "locked"}
    assert roots == _listed_roots(root) == _listed_roots(base / "other")


def test_a_repository_without_linked_worktrees_lists_its_checkout(tmp_path):
    root = Path(os.path.realpath(tmp_path)) / "plain"
    _git("init", "-q", "-b", "main", str(root))
    assert not (root / ".git" / "worktrees").exists()
    assert registered_worktree_roots(root / ".git") == {root} == _listed_roots(root)


def test_relative_registry_pointers_list_the_worktree(repo):
    """`worktree.useRelativePaths`: the `gitdir` names `.git` relative to the entry."""
    worktree, admin = repo["worktree"], repo["admin"]
    (worktree / ".git").write_text(f"gitdir: {os.path.relpath(admin, worktree)}\n")
    (admin / "gitdir").write_text(f"{os.path.relpath(worktree / '.git', admin)}\n")
    assert registered_worktree_roots(repo["common"]) == {repo["root"], worktree}


@pytest.mark.parametrize("kind", ["separate git dir", "bare"])
def test_the_main_worktree_is_named_as_git_names_it(repo, kind):
    """The common dir less a trailing `/.git`: for a separate git dir and a
    bare repository that is the git dir itself, as `git worktree list` says."""
    base = repo["base"]
    if kind == "bare":
        common = base / "bare.git"
        _git("clone", "-q", "--bare", str(repo["root"]), str(common))
    else:
        common = base / "separate"
        _git("init", "-q", "-b", "main", f"--separate-git-dir={common}", str(base / "work"))
        _git("-C", str(base / "work"), *GIT_IDENT, "commit", "-q", "--allow-empty", "-m", "one")
    _git("--git-dir", str(common), "worktree", "add", "-q", "--detach", str(base / "linked"))
    roots = registered_worktree_roots(common)
    assert roots == {common, base / "linked"} == _listed_roots(base / "linked")


@pytest.mark.parametrize("planted", ["entry link", "gitdir link", "gitdir fifo"])
def test_a_registry_entry_git_would_read_through_names_no_root(repo, planted):
    """An entry that is a link, or whose `gitdir` is a link or a FIFO, is
    never read through (git would follow it): it names no root, and the
    binding refuses the worktree."""
    admin, elsewhere = repo["admin"], repo["base"] / "elsewhere"
    if planted == "entry link":
        admin.rename(elsewhere)
        admin.symlink_to(elsewhere)
    elif planted == "gitdir link":
        (admin / "gitdir").rename(elsewhere)
        (admin / "gitdir").symlink_to(elsewhere)
    else:
        (admin / "gitdir").unlink()
    fifo = planted == "gitdir fifo"
    with watched_fifo(admin / "gitdir") if fifo else contextlib.nullcontext([]) as opened:
        assert registered_worktree_roots(repo["common"]) == {repo["root"]}
        with pytest.raises(VerificationError, match="no entry"):
            bound_admin_dir(repo["common"], repo["worktree"])
    assert opened == []


def test_a_registry_bigger_than_the_bound_is_refused(repo, monkeypatch):
    monkeypatch.setattr(agent_worktree, "MAX_REGISTRY_ENTRIES", 1)
    _git("-C", str(repo["root"]), "worktree", "add", "-q", "--detach", str(repo["base"] / "o"))
    with pytest.raises(VerificationError, match="cannot list the repository's worktrees"):
        registered_worktree_roots(repo["common"])


def test_a_registry_that_is_a_link_is_refused_without_reading_through_it(repo, monkeypatch):
    """The registry ``<common>/worktrees`` itself a link to a directory
    outside, which still holds the worktree's entry, as an agent without an
    enforced sandbox can leave it: the listing and the binding refuse it on
    ``lstat``, and the candidate read and the import name it, before any of
    them opens or lists a path through it. Once the operator puts the
    directory back, each answers again (#223)."""
    head = _commit_privately(repo)
    registry, elsewhere = repo["common"] / "worktrees", repo["base"] / "elsewhere"
    registry.rename(elsewhere)
    registry.symlink_to(elsewhere)
    # The layout is sharp: a listing by path, the read before this check,
    # finds the worktree's entry through the link.
    assert [entry.name for entry in os.scandir(registry)] == ["2"]
    reached: list[object] = []
    real_open, real_scandir = os.open, os.scandir

    def opening(path, *args, **kwargs):
        reached.append(path)
        return real_open(path, *args, **kwargs)

    def listing(path="."):
        reached.append(path)
        return real_scandir(path)

    seen: list[ExecutionRequest] = []
    with monkeypatch.context() as patch:
        patch.setattr(os, "open", opening)
        patch.setattr(os, "scandir", listing)
        with pytest.raises(VerificationError, match=re.escape(f"{registry} is a link")):
            registered_worktree_roots(repo["common"])
        with pytest.raises(VerificationError, match=re.escape(f"{registry} is a link")):
            bound_admin_dir(repo["common"], repo["worktree"])
        candidate = read_candidate(repo["common"], repo["admin"], "w", _recording(seen))
        with pytest.raises(ObjectImportError, match=re.escape(f"{registry} is a link")):
            import_private_objects(repo["common"], repo["admin"], repo["private"], _recording(seen))
    # A descriptor would be the registry's: nothing listed it, by path or not.
    through = [
        path
        for path in reached
        if isinstance(path, int) or str(path).startswith((str(registry), str(elsewhere)))
    ]
    assert through == [] and seen == []
    assert candidate.head == "" and f"{registry} is a link" in candidate.problem
    assert not _shared_has(repo, head)
    registry.unlink()
    elsewhere.rename(registry)
    assert registered_worktree_roots(repo["common"]) == {repo["root"], repo["worktree"]}
    assert bound_admin_dir(repo["common"], repo["worktree"]) == repo["admin"]
    assert import_private_objects(repo["common"], repo["admin"], repo["private"], None) == head
    assert read_candidate(repo["common"], repo["admin"], "w", None).head == head


@pytest.mark.parametrize("kind", ["file", "fifo"])
def test_a_registry_that_is_not_a_directory_is_refused(repo, kind):
    registry = repo["common"] / "worktrees"
    shutil.rmtree(registry)
    if kind == "file":
        registry.write_text("not a registry\n")
    else:
        os.mkfifo(registry)
    with pytest.raises(VerificationError, match=re.escape(f"{registry} is not a directory")):
        registered_worktree_roots(repo["common"])
    with pytest.raises(VerificationError, match=re.escape(f"{registry} is not a directory")):
        bound_admin_dir(repo["common"], repo["worktree"])


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a directory without permission")
def test_a_registry_that_cannot_be_read_is_refused(repo):
    registry = repo["common"] / "worktrees"
    registry.chmod(0)
    try:
        with pytest.raises(VerificationError, match="cannot list the repository's worktrees"):
            registered_worktree_roots(repo["common"])
    finally:
        registry.chmod(0o755)


# ---------------------------------------------------------------- candidate


def test_the_candidate_is_the_detached_head(repo):
    head = _git("-C", str(repo["root"]), "rev-parse", "HEAD")
    candidate = read_candidate(repo["common"], repo["admin"], str(repo["worktree"]), None)
    assert candidate.attached is False and candidate.head == head and not candidate.problem


def test_an_attached_head_is_reported(repo):
    _git("-C", str(repo["worktree"]), "switch", "-q", "-c", "agent-branch")
    candidate = read_candidate(repo["common"], repo["admin"], str(repo["worktree"]), None)
    assert candidate.attached is True


def test_a_head_link_is_not_read(repo):
    head = repo["admin"] / "HEAD"
    target = repo["base"] / "elsewhere-HEAD"
    target.write_text(head.read_text())
    head.unlink()
    head.symlink_to(target)
    candidate = read_candidate(repo["common"], repo["admin"], str(repo["worktree"]), None)
    assert candidate.head == "" and "not a regular file" in candidate.problem


def test_pointer_rewrites_do_not_move_the_candidate(repo):
    """The decoy repository's HEAD is never what the controller reads (D6.3)."""
    head = _git("-C", str(repo["root"]), "rev-parse", "HEAD")
    decoy = repo["base"] / "decoy"
    _git("init", "-q", "-b", "main", str(decoy))
    _git("-C", str(decoy), *GIT_IDENT, "commit", "-q", "--allow-empty", "-m", "decoy")
    (repo["worktree"] / ".git").write_text(f"gitdir: {decoy / '.git'}\n")
    (repo["admin"] / "commondir").write_text(f"{decoy / '.git'}\n")
    (repo["admin"] / "gitdir").write_text(f"{decoy / '.git'}\n")
    candidate = read_candidate(repo["common"], repo["admin"], str(repo["worktree"]), None)
    assert candidate.head == head


def test_an_attached_head_naming_a_linked_reference_opens_nothing(repo):
    """``HEAD`` a regular file naming a reference of its own admin dir,
    ``worktrees/<id>/refs/worktree/redirect``, which is a link to a FIFO
    outside the store: the candidate read tells ``HEAD`` attached without
    opening the FIFO (a reader blocks until git's timeout), and the import
    runs no git at all (#223)."""
    head = _commit_privately(repo)
    refs = repo["admin"] / "refs" / "worktree"
    refs.mkdir(parents=True)
    outside = repo["base"] / "outside-fifo"
    (refs / "redirect").symlink_to(outside)
    (repo["admin"] / "HEAD").write_text("ref: worktrees/2/refs/worktree/redirect\n")
    seen: list[ExecutionRequest] = []
    with watched_fifo(outside) as opened:
        imported = import_private_objects(
            repo["common"], repo["admin"], repo["private"], _recording(seen)
        )
        assert seen == []
        candidate = read_candidate(repo["common"], repo["admin"], "w", _recording(seen))
    assert opened == []
    assert imported == "" and not _shared_has(repo, head)
    assert candidate == Candidate(True, "")
    assert [r.command[-2:] for r in seen] == [["--no-recurse", "worktrees/2/HEAD"]]
    # The layout is sharp: `symbolic-ref` without `--no-recurse`, the read
    # before #223, opens the FIFO.
    (refs / "redirect").unlink()
    (refs / "redirect").symlink_to(repo["base"] / "outside-fifo-2")
    with watched_fifo(repo["base"] / "outside-fifo-2") as opened:
        subprocess.run(
            ["git", "--git-dir", str(repo["common"]), "symbolic-ref", "-q", "worktrees/2/HEAD"],
            capture_output=True,
            timeout=60,
        )
    assert opened


def test_a_head_link_to_a_fifo_is_opened_by_no_controller_read(repo):
    """``HEAD`` a link to a FIFO outside the store: the candidate read and
    the import refuse it on ``lstat``, and the registry listing and the
    binding read no ``HEAD``, so none of them opens the FIFO and each still
    answers (#223)."""
    head = _commit_privately(repo)
    head_file = repo["admin"] / "HEAD"
    outside = repo["base"] / "outside-fifo"
    head_file.unlink()
    head_file.symlink_to(outside)
    seen: list[ExecutionRequest] = []
    with watched_fifo(outside) as opened:
        candidate = read_candidate(repo["common"], repo["admin"], "w", _recording(seen))
        with pytest.raises(ObjectImportError, match="is not a regular file"):
            import_private_objects(repo["common"], repo["admin"], repo["private"], _recording(seen))
        roots = registered_worktree_roots(repo["common"])
        admin = bound_admin_dir(repo["common"], repo["worktree"])
    assert opened == [] and seen == []
    assert candidate.head == "" and "is not a regular file" in candidate.problem
    assert roots == {repo["root"], repo["worktree"]} and admin == repo["admin"]
    assert not _shared_has(repo, head)
    # The layout is sharp: `git worktree list`, the listing before #223,
    # opens the FIFO.
    head_file.unlink()
    head_file.symlink_to(repo["base"] / "outside-fifo-2")
    with watched_fifo(repo["base"] / "outside-fifo-2") as opened:
        subprocess.run(
            ["git", "-C", str(repo["root"]), "worktree", "list", "--porcelain"],
            capture_output=True,
            timeout=60,
        )
    assert opened


def test_a_head_naming_no_commit_reads_as_none(repo):
    (repo["admin"] / "HEAD").write_text("0" * 40 + "\n")
    candidate = read_candidate(repo["common"], repo["admin"], str(repo["worktree"]), None)
    assert candidate.head == "" and candidate.attached is False


# ------------------------------------------------------------ strict import


def _commit_privately(repo: dict[str, Path], name: str = "b.txt") -> str:
    """What an enforced agent does: its new objects land in the private directory."""
    # Quoted as git unquotes it, so that a ``:`` in the path does not split it.
    shared = str(repo["common"] / "objects").replace("\\", "\\\\").replace('"', '\\"')
    env = {
        "GIT_OBJECT_DIRECTORY": str(repo["private"]),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": f'"{shared}"',
    }
    worktree = str(repo["worktree"])
    (repo["worktree"] / name).write_text(f"{name}\n")
    _git("-C", worktree, "add", name, env=env)
    _git("-C", worktree, *GIT_IDENT, "commit", "-q", "-m", f"add {name}", env=env)
    return _git("-C", worktree, "rev-parse", "HEAD", env=env)


def _shared_has(repo: dict[str, Path], sha: str) -> bool:
    res = subprocess.run(
        ["git", "--git-dir", str(repo["common"]), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True,
    )
    return res.returncode == 0


def test_an_empty_private_directory_imports_nothing(repo):
    assert not holds_objects(repo["private"])
    assert import_private_objects(repo["common"], repo["admin"], repo["private"], None) == ""


def test_the_agents_commit_is_imported_into_the_shared_store(repo):
    head = _commit_privately(repo)
    assert not _shared_has(repo, head)
    assert read_candidate(repo["common"], repo["admin"], "w", None).head == ""
    base = _git("-C", str(repo["root"]), "rev-parse", "HEAD")
    imported = import_private_objects(
        repo["common"], repo["admin"], repo["private"], None, exclude=(base,)
    )
    assert imported == head and _shared_has(repo, head)
    _git("--git-dir", str(repo["common"]), "fsck", "--no-dangling", "--connectivity-only")
    assert read_candidate(repo["common"], repo["admin"], "w", None).head == head
    # Idempotent: a second import finds everything already there.
    assert import_private_objects(repo["common"], repo["admin"], repo["private"], None) == head


def _recording(seen: list[ExecutionRequest]):
    def runner(request: ExecutionRequest):
        seen.append(request)
        return execute(request)

    return runner


@pytest.mark.parametrize(
    ("path", "entry"),
    [
        ("/r/objects", '"/r/objects"'),
        ("/a:b/objects", '"/a:b/objects"'),
        ('/a"b/objects', '"/a\\"b/objects"'),
        ("/a\\b/objects", '"/a\\\\b/objects"'),
    ],
)
def test_the_alternate_entry_is_quoted_the_way_git_unquotes_it(path, entry):
    assert alternate_entry(Path(path)) == entry


@pytest.mark.parametrize("name", ["a:b", 'a"b', "a\\b", 'a:"\\b'])
def test_a_private_directory_under_a_path_git_would_split_is_imported(tmp_path, name):
    """A checkout under a directory whose name holds a ``:`` (where git
    splits an unquoted ``GIT_ALTERNATE_OBJECT_DIRECTORIES``), a ``"`` or a
    ``\\`` (what the quoting escapes): the agent's commit is imported and
    the candidate read returns it (#224). Unquoted, git read no object
    through the alternate and the import took the commit for none."""
    base = Path(os.path.realpath(tmp_path)) / name
    base.mkdir()
    repo = _make_repo(base)
    head = _commit_privately(repo)
    seen: list[ExecutionRequest] = []
    imported = import_private_objects(
        repo["common"], repo["admin"], repo["private"], _recording(seen)
    )
    assert imported == head and _shared_has(repo, head)
    assert read_candidate(repo["common"], repo["admin"], str(repo["worktree"]), None).head == head
    named = {
        r.env["GIT_ALTERNATE_OBJECT_DIRECTORIES"]
        for r in seen
        if r.env and "GIT_ALTERNATE_OBJECT_DIRECTORIES" in r.env
    }
    assert named == {alternate_entry(repo["private"])}


@pytest.mark.parametrize("kind", ["link", "dangling link"])
def test_a_head_link_is_never_followed_by_the_import(repo, kind):
    """The agent's commit is in the private directory and its ``HEAD`` is a
    link to a file outside the store naming it (one the agent could not see
    inside, or none yet): the import refuses before any git process reads
    ``HEAD``, and nothing reaches the shared store (D6.3, D6.5)."""
    head = _commit_privately(repo)
    head_file = repo["admin"] / "HEAD"
    outside = repo["base"] / "outside-HEAD"
    if kind == "link":
        outside.write_text(head + "\n")
    head_file.unlink()
    head_file.symlink_to(outside)
    seen: list[ExecutionRequest] = []
    with pytest.raises(ObjectImportError, match="is not a regular file") as caught:
        import_private_objects(repo["common"], repo["admin"], repo["private"], _recording(seen))
    assert "nothing was read or published" in str(caught.value)
    assert seen == [] and not _shared_has(repo, head)
    candidate = read_candidate(repo["common"], repo["admin"], "w", None)
    assert candidate.head == "" and "is not a regular file" in candidate.problem


def test_an_attached_head_imports_nothing_and_resolves_no_agent_reference(repo):
    """``HEAD`` a regular file naming a per-worktree reference that is a link
    to a file outside naming the agent's commit: the import reads the
    object id ``HEAD`` holds itself and finds none, so git resolves nothing
    the agent wrote; the candidate is attached and names no commit."""
    head = _commit_privately(repo)
    outside = repo["base"] / "outside-ref"
    outside.write_text(head + "\n")
    refs = repo["admin"] / "refs" / "worktree"
    refs.mkdir(parents=True)
    (refs / "agent").symlink_to(outside)
    (repo["admin"] / "HEAD").write_text("ref: refs/worktree/agent\n")
    seen: list[ExecutionRequest] = []
    assert (
        import_private_objects(repo["common"], repo["admin"], repo["private"], _recording(seen))
        == ""
    )
    assert seen == [] and not _shared_has(repo, head)
    candidate = read_candidate(repo["common"], repo["admin"], "w", _recording(seen))
    assert candidate.attached is True and candidate.head == ""
    assert [r.command[-1] for r in seen] == ["worktrees/2/HEAD"]  # symbolic-ref alone


def test_a_corrupt_private_object_is_not_imported(repo):
    head = _commit_privately(repo)
    loose = repo["private"] / head[:2] / head[2:]
    loose.chmod(0o644)
    loose.write_bytes(b"not a zlib stream")
    with pytest.raises(ObjectImportError, match="nothing was published"):
        import_private_objects(repo["common"], repo["admin"], repo["private"], None)
    assert not _shared_has(repo, head)


@pytest.mark.parametrize("name", ["info/alternates", "info/http-alternates"])
def test_an_alternates_file_is_tampering(repo, name):
    _commit_privately(repo)
    (repo["private"] / "info").mkdir(exist_ok=True)
    (repo["private"] / name).write_text("/somewhere/else/objects\n")
    assert name in (private_objects_problem(repo["common"], repo["private"]) or "")
    with pytest.raises(SandboxTamperError, match="tampered"):
        import_private_objects(repo["common"], repo["admin"], repo["private"], None)


def test_a_link_or_a_fifo_is_tampering(repo):
    (repo["private"] / "pack").mkdir()
    (repo["private"] / "pack" / "x.pack").symlink_to("/etc/hostname")
    assert "x.pack" in (private_objects_problem(repo["common"], repo["private"]) or "")
    (repo["private"] / "pack" / "x.pack").unlink()
    os.mkfifo(repo["private"] / "pack" / "fifo")
    assert "fifo" in (private_objects_problem(repo["common"], repo["private"]) or "")
    (repo["private"] / "pack" / "fifo").unlink()
    assert private_objects_problem(repo["common"], repo["private"]) is None


def _replace_with_link(directory: Path, target: Path) -> None:
    """Move ``directory`` to ``target`` and leave a link to it in its place."""
    directory.rename(target)
    directory.symlink_to(target)


@pytest.mark.parametrize("kind", ["link", "dangling link", "file"])
def test_a_private_root_that_is_not_a_real_directory_is_tampering(repo, kind):
    """The root itself is checked without following a link: never read
    through, and never taken for an empty store."""
    head = _commit_privately(repo)
    private = repo["private"]
    elsewhere = repo["base"] / "elsewhere-objects"
    if kind == "link":
        _replace_with_link(private, elsewhere)
    elif kind == "dangling link":
        _replace_with_link(private, elsewhere)
        shutil.rmtree(elsewhere)
    else:
        shutil.rmtree(private)
        private.write_text("not an object store\n")
    assert holds_objects(private)
    problem = private_objects_problem(repo["common"], private) or ""
    assert str(private) in problem
    assert ("is a link" if kind != "file" else "is not a directory") in problem
    with pytest.raises(SandboxTamperError, match="tampered"):
        import_private_objects(repo["common"], repo["admin"], private, None)
    assert not _shared_has(repo, head)


def test_a_link_above_the_private_root_is_tampering(repo):
    """A real directory reached through a link is outside the store too."""
    head = _commit_privately(repo)
    issue_dir = repo["private"].parent
    _replace_with_link(issue_dir, repo["base"] / "elsewhere")
    assert holds_objects(repo["private"])
    problem = private_objects_problem(repo["common"], repo["private"]) or ""
    assert f"{issue_dir} is a link" in problem
    with pytest.raises(SandboxTamperError, match="tampered"):
        import_private_objects(repo["common"], repo["admin"], repo["private"], None)
    assert not _shared_has(repo, head)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a directory without permission")
def test_a_private_root_that_cannot_be_listed_is_tampering(repo):
    head = _commit_privately(repo)
    repo["private"].chmod(0)
    try:
        assert holds_objects(repo["private"])
        assert "cannot be" in (private_objects_problem(repo["common"], repo["private"]) or "")
        with pytest.raises(SandboxTamperError, match="tampered"):
            import_private_objects(repo["common"], repo["admin"], repo["private"], None)
    finally:
        repo["private"].chmod(0o700)
    assert not _shared_has(repo, head)


def test_an_absent_private_root_imports_nothing(repo):
    repo["private"].rmdir()
    assert not holds_objects(repo["private"])
    assert import_private_objects(repo["common"], repo["admin"], repo["private"], None) == ""
    assert "cannot be checked" in (private_objects_problem(repo["common"], repo["private"]) or "")


def test_a_private_root_outside_the_common_dir_is_refused(repo):
    stray = repo["base"] / "objects"
    stray.mkdir()
    assert "not a directory below" in (private_objects_problem(repo["common"], stray) or "")
    dotdot = repo["common"] / "autoforge" / ".." / "objects"
    assert "not a directory below" in (private_objects_problem(repo["common"], dotdot) or "")
