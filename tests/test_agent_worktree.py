"""The agent worktree's git surface (ADR 0005 D6.3-D6.5, #168).

Real repositories, real git: the binding reads the registry, the candidate
read ignores everything the agent can write, and the strict import copies
an agent's commit out of a private object directory into the shared store.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from autoforge.agent_worktree import (
    ObjectImportError,
    bound_admin_dir,
    holds_objects,
    import_private_objects,
    private_objects_problem,
    read_candidate,
)
from autoforge.errors import SandboxTamperError, VerificationError

from .conftest import GIT_IDENT


def _git(*args: str, env: dict[str, str] | None = None) -> str:
    out = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path) -> dict[str, Path]:
    base = Path(os.path.realpath(tmp_path))
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


def test_a_head_naming_no_commit_reads_as_none(repo):
    (repo["admin"] / "HEAD").write_text("0" * 40 + "\n")
    candidate = read_candidate(repo["common"], repo["admin"], str(repo["worktree"]), None)
    assert candidate.head == "" and candidate.attached is False


# ------------------------------------------------------------ strict import


def _commit_privately(repo: dict[str, Path], name: str = "b.txt") -> str:
    """What an enforced agent does: its new objects land in the private directory."""
    env = {
        "GIT_OBJECT_DIRECTORY": str(repo["private"]),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(repo["common"] / "objects"),
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
    assert name in (private_objects_problem(repo["private"]) or "")
    with pytest.raises(SandboxTamperError, match="tampered"):
        import_private_objects(repo["common"], repo["admin"], repo["private"], None)


def test_a_link_or_a_fifo_is_tampering(repo):
    (repo["private"] / "pack").mkdir()
    (repo["private"] / "pack" / "x.pack").symlink_to("/etc/hostname")
    assert "x.pack" in (private_objects_problem(repo["private"]) or "")
    (repo["private"] / "pack" / "x.pack").unlink()
    os.mkfifo(repo["private"] / "pack" / "fifo")
    assert "fifo" in (private_objects_problem(repo["private"]) or "")
    (repo["private"] / "pack" / "fifo").unlink()
    assert private_objects_problem(repo["private"]) is None
