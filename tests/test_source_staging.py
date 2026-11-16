"""#292: the host side of a container launch's source, in both staging forms (ADR 0013 D2, D12).

No container runtime is needed. The staging root is written into a real
git common dir under ``tmp_path`` and proved against the store; the
lifecycle runs on :class:`StagingBackend`, a container-shaped fake whose
seed, staging root and removal are the production :class:`SourceStaging`,
and whose creates render #270's real create options, so a test reads how
``/source`` and ``/workspace`` are presented from the arguments a runtime
would get. The hostile-input and partial-clone cases run real git with the
operator's global configuration pointed at a home of the test's own.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

import pytest

from autoforge import safefs, source_staging
from autoforge.errors import SandboxLaunchRefusedError
from autoforge.executor import execute
from autoforge.git_input import SEED_MANIFEST_NAME, LaunchGitInput, seed_manifest_digest
from autoforge.materialize import DIRECTORY_MODE, EXECUTABLE_MODE, FILE_MODE, materialize
from autoforge.sandbox import BackendKind, LaunchDescription
from autoforge.sandbox_backend import (
    NETWORK_BRIDGE,
    NETWORK_NONE,
    NO_SEED,
    Artifact,
    LaunchHandle,
    LaunchInputs,
    LaunchOwner,
    LifecycleObject,
    ObjectKind,
    ProbeResult,
    Removal,
    RuntimeReport,
    Seed,
)
from autoforge.sandbox_container import (
    ContainerNetwork,
    ContainerRole,
    ContainerSpec,
    HostContext,
    RuntimeProduct,
    StagingMethod,
    StagingRoot,
    check_spec,
    create_options,
)
from autoforge.sandbox_lifecycle import LaunchLifecycle, reconcile
from autoforge.seed_pack import (
    CONVERSIONS,
    LFS,
    PARTIAL_CLONE,
    SEED_PACK_NAME,
    SPARSE,
    SUBMODULE,
    SeedSettings,
)
from autoforge.source_staging import (
    PRIVATE_DIRECTORY_MODE,
    STAGED_ENTRIES,
    STAGED_OWNER,
    STAGED_READ_CONTEXT,
    STAGED_SEED,
    STAGED_TREE,
    SourceStaging,
    StagedSource,
    as_seed,
    owner_record,
)

from .test_seed_pack import (
    Review,
    commit_files,
    common_of,
    git_input,
    init_repo,
    out,
    review_repo,
    run_git,
    snapshot,
    stage_seed,
)

KEY = "292"
OWNER = LaunchOwner(run="run-1", issue=292, phase="REVIEW", launch="L-1")
NONCE = "n-root"
RUNTIME = RuntimeReport(
    BackendKind.CONTAINER, "podman", "5.2.0", True, "unix:///run/user/1000/podman.sock", "store-1"
)
PLANNED = LaunchDescription(mounts=())


@pytest.fixture(autouse=True)
def temporary(tmp_path, monkeypatch) -> Path:
    """Where ``tempfile.mkdtemp`` creates seeds and scratch directories during a test."""
    where = tmp_path / "temporary"
    where.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(where))
    return where


class RecordingLock:
    """L1 as a test sees it: each operation, and whether the launch's directory existed."""

    def __init__(self, probe: Callable[[], bool] | None = None) -> None:
        self.events: list[tuple[str, str, bool]] = []
        self.probe = probe or (lambda: False)

    def __call__(self, operation: str):
        @contextmanager
        def held():
            self.events.append(("enter", operation, self.probe()))
            try:
                yield self
            finally:
                self.events.append(("exit", operation, self.probe()))

        return held()


def staging_for(
    repo: Path,
    method: StagingMethod = StagingMethod.BIND,
    settings: SeedSettings | None = None,
    lock: RecordingLock | None = None,
) -> SourceStaging:
    return SourceStaging(
        common_of(repo), KEY, settings or SeedSettings(), method, git_lock=lock or RecordingLock()
    )


def review_input(review: Review, launch: str = "L-1") -> LaunchGitInput:
    return git_input("REVIEW", review.head, review.merge_base, launch=launch)


def owner_of(gi: LaunchGitInput) -> LaunchOwner:
    """The launch ``gi`` names, as its objects' labels say."""
    return LaunchOwner(run=gi.run, issue=gi.issue, phase=gi.phase, launch=gi.launch)


def bound(staging: SourceStaging, gi: LaunchGitInput) -> tuple[Seed, LaunchGitInput]:
    """The seed step and its end: the seed built, the input completed with its digest."""
    seed = staging.seed(LaunchInputs(owner_of(gi), workspace="/nowhere", git_input=gi))
    return seed, gi.with_seed_manifest(seed.identity)


def read_context(tmp_path: Path) -> Path:
    where = tmp_path / "state" / "read-context" / "1"
    (where / "files").mkdir(parents=True)
    (where / "README.md").write_text("read context\n")
    (where / "files" / "issue.md").write_text("the issue\n")
    return where


def stage(
    tmp_path: Path, review: Review, launch: str = "L-1", **kwargs
) -> tuple[SourceStaging, StagedSource, Seed]:
    staging = staging_for(review.repo, **kwargs)
    gi = review_input(review, launch)
    seed, gi = bound(staging, gi)
    owner = owner_of(gi)
    root = staging.staging_root(owner)
    assert root is not None
    staged = staging.make_staging_root(
        root, seed, gi, str(read_context(tmp_path)), owner=owner, nonce=NONCE
    )
    return staging, staged, seed


def tree_files(root: Path) -> list[tuple[str, str, int, bytes]]:
    """Every entry below ``root``: path, kind, permission bits, bytes or link text."""
    found = []
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        rel = str(path.relative_to(root))
        if stat.S_ISLNK(info.st_mode):
            found.append((rel, "link", 0, os.readlink(path).encode()))
        elif stat.S_ISDIR(info.st_mode):
            found.append((rel, "dir", stat.S_IMODE(info.st_mode), b""))
        else:
            found.append((rel, "file", stat.S_IMODE(info.st_mode), path.read_bytes()))
    return found


def checkout_state(repo: Path) -> tuple:
    """What the operator's checkout looks like: HEAD, branch, status, index and working files."""
    return (
        out(repo, "rev-parse", "HEAD"),
        out(repo, "symbolic-ref", "-q", "HEAD"),
        out(repo, "--no-optional-locks", "status", "--porcelain=v1", "--ignored", "-uall"),
        (repo / ".git" / "index").read_bytes(),
        {k: v for k, v in snapshot(repo).items() if not k.startswith(".git/")},
    )


# -- the bind form's staging root (D2, D12) --------------------------------------------------


def test_the_bind_form_writes_the_proved_tree_its_seed_and_the_read_context(tmp_path):
    review = review_repo(tmp_path)
    staging, staged, seed = stage(tmp_path, review)
    root = Path(staged.root.path)
    common = common_of(review.repo)

    assert root == common / "autoforge" / "sandbox" / KEY / "stage" / "L-1"
    assert sorted(os.listdir(root)) == sorted(STAGED_ENTRIES)
    assert stat.S_IMODE(root.stat().st_mode) == PRIVATE_DIRECTORY_MODE
    # The tree is the reviewed HEAD's, byte for byte, and nothing else: no
    # .git, no file of the checkout's that the commit does not hold.
    assert staged.commit == review.head
    assert staged.tree == out(review.repo, "rev-parse", f"{review.head}^{{tree}}")
    with materialize(execute, str(common), review.head, "sha1", prefix="control-") as control:
        assert tree_files(root / STAGED_TREE) == tree_files(control.root)
    assert not (root / STAGED_TREE / ".git").exists()
    # The seed, exactly as the input binds it.
    assert seed_manifest_digest(str(root / STAGED_SEED)) == seed.identity == staged.seed_digest
    assert (root / STAGED_SEED / SEED_PACK_NAME).read_bytes() == (
        Path(seed.directory) / SEED_PACK_NAME
    ).read_bytes()
    # The read context's copy.
    assert (root / STAGED_READ_CONTEXT / "README.md").read_text() == "read context\n"
    assert (root / STAGED_READ_CONTEXT / "files" / "issue.md").read_text() == "the issue\n"
    # Ownership and modes are recorded, as data, for #271: nothing is chowned.
    assert (staged.ownership.uid, staged.ownership.gid) == (os.getuid(), os.getgid())
    assert stat.S_IMODE((root / STAGED_TREE).stat().st_mode) == DIRECTORY_MODE
    modes = {mode for _, kind, mode, _ in tree_files(root / STAGED_TREE) if kind == "file"}
    assert modes <= {FILE_MODE, EXECUTABLE_MODE}
    staging.reprove(staged)  # proves as written


def test_two_stagings_of_one_base_are_byte_identical_and_leave_the_checkout_unchanged(tmp_path):
    review = review_repo(tmp_path)
    repo = review.repo
    # Modes and links: an executable, a symlink, an empty-ish nested tree.
    (repo / "bin").mkdir()
    (repo / "bin" / "run").write_bytes(b"#!/bin/sh\necho run\n")
    (repo / "bin" / "run").chmod(0o755)
    (repo / "link").symlink_to("bin/run")
    run_git(repo, "add", "bin/run", "link")
    run_git(repo, "commit", "-q", "-m", "modes and links")
    head = out(repo, "rev-parse", "HEAD")
    review = dataclasses.replace(review, head=head)
    (repo / "untracked.txt").write_text("the operator's work in progress\n")
    before = checkout_state(repo)

    _, first, _ = stage(tmp_path / "a", review, "L-1")
    _, second, _ = stage(tmp_path / "b", review, "L-2")

    a = tree_files(Path(first.root.path) / STAGED_TREE)
    b = tree_files(Path(second.root.path) / STAGED_TREE)
    assert a == b
    assert ("link", "link", 0, b"bin/run") in a
    assert ("bin/run", "file", EXECUTABLE_MODE, b"#!/bin/sh\necho run\n") in a
    assert checkout_state(repo) == before


def test_the_snapshot_holds_no_git_dir_home_content_or_untracked_or_ignored_file(
    tmp_path, monkeypatch
):
    review = review_repo(tmp_path)
    repo = review.repo
    (repo / ".gitignore").write_text("secret.env\n")
    run_git(repo, "add", ".gitignore")
    run_git(repo, "commit", "-q", "-m", "ignore")
    review = dataclasses.replace(review, head=out(repo, "rev-parse", "HEAD"))
    (repo / "secret.env").write_text("TOKEN=not-a-real-one\n")
    (repo / "scratch.txt").write_text("untracked\n")
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_test").write_text("not a key\n")
    monkeypatch.setenv("HOME", str(home))

    _, staged, _ = stage(tmp_path, review)
    names = {rel for rel, *_ in tree_files(Path(staged.root.path))}
    assert not {n for n in names if n.split("/")[-1] in ("secret.env", "scratch.txt", ".ssh")}
    assert not {n for n in names if ".git/" in n or n.endswith("/.git") or n == ".git"}
    tracked = set(out(repo, "ls-tree", "-r", "--name-only", review.head).splitlines())
    in_tree = {
        rel for rel, kind, *_ in tree_files(Path(staged.root.path) / STAGED_TREE) if kind != "dir"
    }
    assert in_tree == tracked


@pytest.mark.parametrize("change", ["byte", "mode", "extra", "removed", "seed"])
def test_the_staging_root_fails_its_proof_after_the_host_changed_it(tmp_path, change):
    review = review_repo(tmp_path)
    staging, staged, _ = stage(tmp_path, review)
    tree = Path(staged.root.path) / STAGED_TREE
    target = tree / "src" / "c.py"
    if change == "byte":
        data = bytearray(target.read_bytes())
        data[0] ^= 0x01
        target.write_bytes(bytes(data))
    elif change == "mode":
        target.chmod(0o755)
    elif change == "extra":
        (tree / "src" / "planted.py").write_text("planted\n")
    elif change == "removed":
        target.unlink()
    else:
        pack = Path(staged.root.path) / STAGED_SEED / SEED_PACK_NAME
        pack.write_bytes(pack.read_bytes()[:-1] + b"\x00")
    with pytest.raises(SandboxLaunchRefusedError, match="staging root is refused"):
        staging.reprove(staged)


def test_a_directory_substituted_at_the_staging_roots_path_is_refused(tmp_path):
    """Even a faithful copy: the path must name the very directory created and proved."""
    review = review_repo(tmp_path)
    staging, staged, _ = stage(tmp_path, review)
    root = Path(staged.root.path)
    copy = root.with_name("copy")
    subprocess.run(["cp", "-a", str(root), str(copy)], check=True)
    root.rename(root.with_name("moved"))
    copy.rename(root)
    with pytest.raises(SandboxLaunchRefusedError, match="substituted"):
        staging.reprove(staged)
    # A link planted there instead is never followed.
    os.rename(root, root.with_name("copy2"))
    root.symlink_to(root.with_name("moved"))
    with pytest.raises(SandboxLaunchRefusedError):
        staging.reprove(staged)


def mounted_at(monkeypatch, *suffixes: str) -> None:
    """Report another mount id for each entry whose path ends with one of ``suffixes``.

    The kernel's mount id is what the staging root's checks compare, so a
    bind mount from the same filesystem and a file mount read as what they
    are; the cases with real mounts are :func:`test_real_mounts_are_refused`.
    """
    real = source_staging._mount_id

    def mount_id(fd: int, where: str) -> int:
        found = real(fd, where)
        return found + 1 if str(where).endswith(suffixes) else found

    monkeypatch.setattr(source_staging, "_mount_id", mount_id)


@pytest.mark.parametrize(
    "where", ["tree/src", "tree/README", "tree/src/c.py", "seed/seed.pack", "read-context/files"]
)
def test_a_mount_below_the_staging_root_fails_its_proof(tmp_path, monkeypatch, where):
    """A directory or a file on another mount below the root, from any filesystem, is refused."""
    review = review_repo(tmp_path)
    staging, staged, _ = stage(tmp_path, review)
    mounted_at(monkeypatch, where)
    with pytest.raises(SandboxLaunchRefusedError, match=f"mounted at {where} below it"):
        staging.reprove(staged)


@pytest.mark.parametrize("where", ["/autoforge/sandbox", f"/{KEY}", "/stage", "/stage/L-1"])
def test_a_mount_in_the_staging_roots_layout_fails_its_proof(tmp_path, monkeypatch, where):
    review = review_repo(tmp_path)
    staging, staged, _ = stage(tmp_path, review)
    mounted_at(monkeypatch, where)
    with pytest.raises(SandboxLaunchRefusedError, match="is a mount point"):
        staging.reprove(staged)


READ_CONTEXT_CHANGES = {
    "added": "files/planted.md was added",
    "changed": "README.md was changed",
    "removed": "README.md was removed",
    "file to directory": "README.md was a file and is a dir now",
    "directory to file": "files was a dir and is a file now",
    "file to link": "read-context/README.md is not of the kind a staging root holds there",
    "file to fifo": "read-context/README.md is not of the kind a staging root holds there",
}


@pytest.mark.parametrize("change", sorted(READ_CONTEXT_CHANGES))
def test_the_staging_root_fails_its_proof_after_its_read_context_changed(tmp_path, change):
    """The copied read context is proved at each hand-off too, entry by entry (PR #504 R1-F4).

    Every change keeps the modes the record says, so what refuses it is
    the read context's own proof, not the permission check.
    """
    review = review_repo(tmp_path)
    staging, staged, _ = stage(tmp_path, review)
    copied = Path(staged.root.path) / STAGED_READ_CONTEXT
    target = copied / "README.md"
    if change == "added":
        (copied / "files" / "planted.md").write_text("planted\n")
        (copied / "files" / "planted.md").chmod(0o600)
    elif change == "changed":
        target.write_text("another read context\n")
    elif change == "removed":
        target.unlink()
    elif change == "file to directory":
        target.unlink()
        target.mkdir()
        target.chmod(PRIVATE_DIRECTORY_MODE)
    elif change == "directory to file":
        shutil.rmtree(copied / "files")
        (copied / "files").write_text("the issue\n")
        (copied / "files").chmod(0o600)
    elif change == "file to link":
        target.unlink()
        target.symlink_to("files/issue.md")
    else:
        target.unlink()
        os.mkfifo(target, 0o600)
    with pytest.raises(SandboxLaunchRefusedError, match="staging root is refused") as refused:
        staging.reprove(staged)
    assert READ_CONTEXT_CHANGES[change] in str(refused.value)


PERMISSION_CHANGES = [
    # A file a non-owner container user can no longer read: the tree proof
    # reads only the owner's execute bit, so this is the permission check's.
    ("tree/src/c.py", 0o600),
    ("tree/src/c.py", 0o664),
    ("tree/src", 0o700),
    ("tree", 0o711),
    ("seed/seed.pack", 0o644),
    ("seed", 0o755),
    ("read-context/README.md", 0o644),
    ("read-context/files", 0o755),
    ("owner", 0o644),
    ("", 0o750),
]


@pytest.mark.parametrize(("where", "mode"), PERMISSION_CHANGES)
def test_the_staging_root_fails_its_proof_after_a_permission_changed(tmp_path, where, mode):
    """Every entry has exactly the mode its ownership record says (PR #504 R1-F5)."""
    review = review_repo(tmp_path)
    staging, staged, _ = stage(tmp_path, review)
    root = Path(staged.root.path)
    (root / where if where else root).chmod(mode)
    with pytest.raises(SandboxLaunchRefusedError, match=f"has mode {mode:04o}, not "):
        staging.reprove(staged)


@pytest.mark.parametrize("umask", [0o077, 0o002, 0o022])
def test_the_ownership_record_is_what_every_staged_entry_has_whatever_the_umask(tmp_path, umask):
    previous = os.umask(umask)
    try:
        review = review_repo(tmp_path)
        staging, staged, _ = stage(tmp_path, review)
    finally:
        os.umask(previous)
    own = staged.ownership
    root = Path(staged.root.path)
    assert (own.uid, own.gid) == (os.geteuid(), os.getegid())
    for path in [root, *root.rglob("*")]:
        info = path.lstat()
        assert (info.st_uid, info.st_gid) == (own.uid, own.gid), path
        if stat.S_ISLNK(info.st_mode):
            continue
        mode = stat.S_IMODE(info.st_mode)
        parts = path.relative_to(root).parts
        if parts and parts[0] == STAGED_TREE:
            allowed = (
                {own.tree_directory_mode}
                if path.is_dir()
                else {own.tree_file_mode, own.tree_executable_mode}
            )
        else:
            allowed = {own.private_directory_mode if path.is_dir() else own.private_file_mode}
        assert mode in allowed, (path, oct(mode))
    staging.reprove(staged)


def test_the_owner_record_names_the_launch_and_its_journal_entry(tmp_path):
    review = review_repo(tmp_path)
    staging, staged, _ = stage(tmp_path, review)
    record = (Path(staged.root.path) / STAGED_OWNER).read_bytes()
    assert record == owner_record(KEY, staged.owner, NONCE)
    assert json.loads(record) == {
        "schema": 1,
        "key": KEY,
        "run": "run-1",
        "issue": 292,
        "phase": "REVIEW",
        "launch": "L-1",
        "nonce": NONCE,
    }
    (Path(staged.root.path) / STAGED_OWNER).write_bytes(owner_record(KEY, staged.owner, "n-other"))
    with pytest.raises(SandboxLaunchRefusedError, match="owner record of another launch"):
        staging.reprove(staged)


@pytest.mark.parametrize(
    "where", ["/autoforge", "/autoforge/sandbox", f"/sandbox/{KEY}", "/stage", "/stage/L-1"]
)
def test_a_mount_in_the_layout_refuses_the_staging_root_before_anything_is_written_in_it(
    tmp_path, monkeypatch, where
):
    """Each directory is checked before anything is created in it (PR #504 R1-F2)."""
    review = review_repo(tmp_path)
    staging = staging_for(review.repo)
    seed, gi = bound(staging, review_input(review))
    mounted_at(monkeypatch, where)
    root = staging.staging_root(owner_of(gi))
    assert root is not None
    with pytest.raises(SandboxLaunchRefusedError, match="is a mount point"):
        staging.make_staging_root(root, seed, gi, owner=owner_of(gi), nonce=NONCE)
    full = Path(root.path)
    mounted = next(path for path in (full, *full.parents) if str(path).endswith(where))
    assert mounted.is_dir() and list(mounted.iterdir()) == []


@pytest.mark.parametrize("where", ["/L-1/seed", "/L-1/read-context", "/L-1/tree"])
def test_a_mount_below_the_new_staging_root_is_refused_before_it_is_written(
    tmp_path, monkeypatch, where
):
    review = review_repo(tmp_path)
    staging = staging_for(review.repo)
    seed, gi = bound(staging, review_input(review))
    mounted_at(monkeypatch, where)
    root = staging.staging_root(owner_of(gi))
    assert root is not None
    with pytest.raises(SandboxLaunchRefusedError, match="mounted at"):
        staging.make_staging_root(
            root, seed, gi, str(read_context(tmp_path)), owner=owner_of(gi), nonce=NONCE
        )
    assert list(Path(root.path + where.removeprefix("/L-1")).iterdir()) == []


@pytest.mark.parametrize("where", ["tree/src", "read-context/files"])
def test_a_mount_at_a_directory_made_while_writing_is_refused_before_it_is_written(
    tmp_path, monkeypatch, where
):
    """Each directory made below the root, the tree writer's included, is checked on its held
    descriptor before its mode is set or anything is made in it (PR #504 R2-F1)."""
    review = review_repo(tmp_path)
    staging = staging_for(review.repo)
    seed, gi = bound(staging, review_input(review))
    context = str(read_context(tmp_path))
    mounted_at(monkeypatch, where)
    root = staging.staging_root(owner_of(gi))
    assert root is not None
    old = os.umask(0o077)
    try:
        with pytest.raises(SandboxLaunchRefusedError, match=f"something is mounted at .*/{where}"):
            staging.make_staging_root(root, seed, gi, context, owner=owner_of(gi), nonce=NONCE)
    finally:
        os.umask(old)
    made = Path(root.path) / where
    assert list(made.iterdir()) == []
    assert stat.S_IMODE(made.stat().st_mode) == 0o700  # its mkdir's under the umask, never set


def test_a_mount_at_a_read_context_directory_after_its_check_receives_nothing(
    tmp_path, monkeypatch
):
    """A file is written only through its directory reached again and checked on its descriptor
    (PR #504 R2-F1): a mount made at ``read-context/files`` after it was made is refused."""
    review = review_repo(tmp_path)
    staging = staging_for(review.repo)
    seed, gi = bound(staging, review_input(review))
    context = str(read_context(tmp_path))
    real = source_staging._mount_id
    checked: list[str] = []

    def mount_id(fd: int, where: str) -> int:
        if not str(where).endswith("read-context/files"):
            return real(fd, where)
        checked.append(where)
        return real(fd, where) + (1 if len(checked) > 1 else 0)

    monkeypatch.setattr(source_staging, "_mount_id", mount_id)
    root = staging.staging_root(owner_of(gi))
    assert root is not None
    with pytest.raises(SandboxLaunchRefusedError, match="something is mounted at .*/files"):
        staging.make_staging_root(root, seed, gi, context, owner=owner_of(gi), nonce=NONCE)
    assert len(checked) == 2  # made and checked once, refused when reached again
    assert list((Path(root.path) / "read-context" / "files").iterdir()) == []


def test_a_seed_whose_digest_differs_from_the_inputs_is_never_written_into_a_staging_root(
    tmp_path,
):
    review = review_repo(tmp_path)
    staging = staging_for(review.repo)
    gi = review_input(review)
    seed, bound_input = bound(staging, gi)
    other = gi.with_seed_manifest("sha256:" + "0" * 64)
    root = staging.staging_root(OWNER)
    assert root is not None
    for refused_input in (other, gi, None):
        with pytest.raises(SandboxLaunchRefusedError, match="seed is not written"):
            staging.make_staging_root(root, seed, refused_input, owner=OWNER, nonce=NONCE)
    with pytest.raises(SandboxLaunchRefusedError, match="seed is not written"):
        staging.make_staging_root(root, NO_SEED, bound_input, owner=OWNER, nonce=NONCE)
    assert not Path(root.path).exists()
    # Swapped after the input was bound: the copy is hashed, and refused.
    manifest = Path(seed.directory) / SEED_MANIFEST_NAME
    manifest.write_bytes(manifest.read_bytes().replace(b'"issue":292', b'"issue":293'))
    with pytest.raises(SandboxLaunchRefusedError):
        staging.make_staging_root(root, seed, bound_input, owner=OWNER, nonce=NONCE)
    assert not (Path(root.path) / STAGED_TREE).exists()


def test_a_staging_root_is_created_under_l1_and_never_reused(tmp_path):
    review = review_repo(tmp_path)
    root_path = common_of(review.repo) / "autoforge" / "sandbox" / KEY / "stage" / "L-1"
    lock = RecordingLock(root_path.exists)
    staging, staged, seed = stage(tmp_path, review, lock=lock)
    assert lock.events == [
        ("enter", "create a launch's staging root", False),
        ("exit", "create a launch's staging root", True),
    ]
    gi = review_input(review).with_seed_manifest(seed.identity)
    with pytest.raises(SandboxLaunchRefusedError, match="never reused"):
        staging.make_staging_root(staged.root, seed, gi, owner=OWNER, nonce=NONCE)


@pytest.mark.parametrize("planted", ["sandbox key", "stage"])
def test_a_link_in_the_staging_roots_layout_is_refused_and_nothing_written_through_it(
    tmp_path, planted
):
    review = review_repo(tmp_path)
    common = common_of(review.repo)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    sandbox = common / "autoforge" / "sandbox"
    sandbox.mkdir(parents=True)
    if planted == "sandbox key":
        (sandbox / KEY).symlink_to(elsewhere)
    else:
        (sandbox / KEY).mkdir()
        (sandbox / KEY / "stage").symlink_to(elsewhere)
    with pytest.raises(SandboxLaunchRefusedError, match="could not be created"):
        stage(tmp_path, review)
    assert list(elsewhere.iterdir()) == []


def test_a_staging_root_that_is_not_this_issues_own_is_refused(tmp_path):
    review = review_repo(tmp_path)
    staging = staging_for(review.repo)
    seed, gi = bound(staging, review_input(review))
    for root in (
        StagingRoot(str(tmp_path), KEY, "L-1"),
        StagingRoot(staging.common_dir, "293", "L-1"),
        StagingRoot(staging.common_dir, KEY, "../L-1"),
        "/tmp/not-a-staging-root",
    ):
        with pytest.raises(SandboxLaunchRefusedError):
            staging.make_staging_root(root, seed, gi, owner=OWNER, nonce=NONCE)  # type: ignore[arg-type]
    for key in ("..", "a/b", ""):
        with pytest.raises(SandboxLaunchRefusedError):
            SourceStaging(staging.common_dir, key, SeedSettings(), StagingMethod.BIND)
    with pytest.raises(SandboxLaunchRefusedError):
        staging.staging_root(dataclasses.replace(OWNER, launch="a/b"))


def test_removing_the_staging_root_is_idempotent_and_scoped_to_its_launch(tmp_path):
    review = review_repo(tmp_path)
    staging, first, _ = stage(tmp_path / "a", review, "L-1")
    _, second, _ = stage(tmp_path / "b", review, "L-2")
    assert staging.remove_staging_root(first.root, first.owner, first.nonce) is Removal.REMOVED
    assert not Path(first.root.path).exists()
    assert Path(second.root.path).exists()  # another launch's is untouched
    assert staging.remove_staging_root(first.root, first.owner, first.nonce) is Removal.REMOVED
    never = StagingRoot(staging.common_dir, KEY, "L-9")
    nobody = dataclasses.replace(OWNER, launch="L-9")
    assert staging.remove_staging_root(never, nobody, NONCE) is Removal.REMOVED
    other = SourceStaging(staging.common_dir, "999", SeedSettings(), StagingMethod.BIND)
    assert other.remove_staging_root(
        StagingRoot(staging.common_dir, "999", "L-1"), OWNER, NONCE
    ) is (Removal.REMOVED)
    # Another launch's root is never removed for this one's entry.
    with pytest.raises(SandboxLaunchRefusedError, match="not launch L-1's"):
        staging.remove_staging_root(second.root, OWNER, NONCE)
    # A bind-form leftover is removed after the runtime's form became copy-in.
    copy = SourceStaging(staging.common_dir, KEY, SeedSettings(), StagingMethod.COPY)
    assert copy.remove_staging_root(second.root, second.owner, second.nonce) is Removal.REMOVED
    assert not Path(second.root.path).exists()


def test_what_is_not_the_launchs_directory_at_its_path_is_left_and_uncertain(tmp_path):
    review = review_repo(tmp_path)
    staging, staged, _ = stage(tmp_path, review)
    root = Path(staged.root.path)
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep").write_text("keep\n")
    root.rename(root.with_name("away"))
    root.symlink_to(target)
    assert staging.remove_staging_root(staged.root, staged.owner, staged.nonce) is (
        Removal.UNCERTAIN
    )
    assert (target / "keep").read_text() == "keep\n"


# -- the launch's identity (PR #504 R1-F3) ----------------------------------------------------

ANOTHER = [("run", "run-2"), ("issue", 293), ("phase", "FIX"), ("launch", "L-2")]


@pytest.mark.parametrize(("field", "value"), ANOTHER)
def test_a_seed_for_another_launch_than_its_owner_is_refused_before_it_is_built(
    tmp_path, temporary, field, value
):
    review = review_repo(tmp_path)
    staging = staging_for(review.repo)
    gi = review_input(review)
    owner = dataclasses.replace(owner_of(gi), **{field: value})
    with pytest.raises(SandboxLaunchRefusedError, match=f"seed is refused: the launch's {field} "):
        staging.seed(LaunchInputs(owner, workspace="/nowhere", git_input=gi))
    assert list(temporary.iterdir()) == []  # nothing was built


@pytest.mark.parametrize(("field", "value"), ANOTHER)
def test_a_staging_root_for_another_launch_than_its_input_is_refused_before_it_is_created(
    tmp_path, field, value
):
    review = review_repo(tmp_path)
    staging = staging_for(review.repo)
    seed, gi = bound(staging, review_input(review))
    owner = dataclasses.replace(owner_of(gi), **{field: value})
    root = staging.staging_root(owner)
    assert root is not None
    with pytest.raises(SandboxLaunchRefusedError, match=f"the launch's {field} is "):
        staging.make_staging_root(root, seed, gi, owner=owner, nonce=NONCE)
    assert not (common_of(review.repo) / "autoforge").exists()


def test_a_staging_root_under_another_launchs_id_or_for_another_launchs_seed_is_refused(tmp_path):
    """Nothing is created: the root's launch id, the owner, the input and the seed agree first."""
    review = review_repo(tmp_path)
    staging = staging_for(review.repo)
    seed, gi = bound(staging, review_input(review))
    owner = owner_of(gi)
    other_root = StagingRoot(staging.common_dir, KEY, "L-2")
    with pytest.raises(SandboxLaunchRefusedError, match="is launch L-2's, not launch L-1's"):
        staging.make_staging_root(other_root, seed, gi, owner=owner, nonce=NONCE)
    # L-1's seed bound into L-2's input: its manifest names L-1.
    other_input = review_input(review, "L-2").with_seed_manifest(seed.identity)
    other_owner = owner_of(other_input)
    with pytest.raises(SandboxLaunchRefusedError, match="manifest names the launch's launch 'L-1'"):
        staging.make_staging_root(other_root, seed, other_input, owner=other_owner, nonce=NONCE)
    root = staging.staging_root(owner)
    assert root is not None
    for nonce in ("", "a b", "../n", "n" * 300):
        with pytest.raises(SandboxLaunchRefusedError, match="nonce"):
            staging.make_staging_root(root, seed, gi, owner=owner, nonce=nonce)
    assert not (common_of(review.repo) / "autoforge").exists()


def test_removal_leaves_a_directory_substituted_at_the_staging_roots_path(tmp_path):
    """Only the directory that holds its entry's owner record is removed (PR #504 R1-F1)."""
    review = review_repo(tmp_path)
    staging, staged, _ = stage(tmp_path, review)
    root = Path(staged.root.path)
    root.rename(root.with_name("away"))
    # A real directory of someone else's, owned by the operator, at the path.
    root.mkdir(mode=0o700)
    (root / STAGED_TREE).mkdir()
    (root / STAGED_TREE / "operator.txt").write_text("someone else's\n")
    assert staging.remove_staging_root(staged.root, staged.owner, staged.nonce) is (
        Removal.UNCERTAIN
    )
    assert (root / STAGED_TREE / "operator.txt").read_text() == "someone else's\n"
    # One whose owner record names another journal entry is not this one's either.
    (root / STAGED_OWNER).write_bytes(owner_record(KEY, staged.owner, "n-other"))
    assert staging.remove_staging_root(staged.root, staged.owner, staged.nonce) is (
        Removal.UNCERTAIN
    )
    assert (root / STAGED_TREE / "operator.txt").exists()
    # Nor another launch's staging root moved to this launch's path.
    shutil.rmtree(root)
    _, second, _ = stage(tmp_path / "b", review, "L-2")
    Path(second.root.path).rename(root)
    assert staging.remove_staging_root(staged.root, staged.owner, staged.nonce) is (
        Removal.UNCERTAIN
    )
    assert (root / STAGED_TREE / "README").exists()
    # The launch's own, back at its path, is removed.
    shutil.rmtree(root)
    root.with_name("away").rename(root)
    assert staging.remove_staging_root(staged.root, staged.owner, staged.nonce) is Removal.REMOVED
    assert not root.exists()


@pytest.mark.parametrize(
    "where", ["tree/src", "tree/README", "tree/src/c.py", "read-context/files", "seed"]
)
def test_removal_removes_nothing_when_a_mount_is_below_the_staging_root(
    tmp_path, monkeypatch, where
):
    """Every entry is checked before anything is removed: a mount leaves it all in place."""
    review = review_repo(tmp_path)
    staging, staged, _ = stage(tmp_path, review)
    root = Path(staged.root.path)
    before = snapshot(root)
    mounted_at(monkeypatch, where)
    assert staging.remove_staging_root(staged.root, staged.owner, staged.nonce) is (
        Removal.UNCERTAIN
    )
    assert snapshot(root) == before


@pytest.mark.parametrize("where", ["/stage", "/stage/L-1"])
def test_removal_leaves_a_staging_root_that_is_or_lies_below_a_mount_point(
    tmp_path, monkeypatch, where
):
    review = review_repo(tmp_path)
    staging, staged, _ = stage(tmp_path, review)
    before = snapshot(Path(staged.root.path))
    mounted_at(monkeypatch, where)
    assert staging.remove_staging_root(staged.root, staged.owner, staged.nonce) is (
        Removal.UNCERTAIN
    )
    assert snapshot(Path(staged.root.path)) == before


def test_a_mount_met_while_removing_stops_the_removal_before_it_descends(tmp_path, monkeypatch):
    """A directory mounted after the check is never descended into; what is left is still the
    launch's (its owner record goes last), and a later removal finishes it."""
    review = review_repo(tmp_path)
    staging, staged, _ = stage(tmp_path, review)
    root = Path(staged.root.path)
    real = source_staging._mount_id

    def mount_id(fd: int, where: str) -> int:
        # The removal opens each directory by its name alone; the check
        # before it names entries by their path below the root.
        return real(fd, where) + (1 if where == "src" else 0)

    monkeypatch.setattr(source_staging, "_mount_id", mount_id)
    assert staging.remove_staging_root(staged.root, staged.owner, staged.nonce) is (
        Removal.UNCERTAIN
    )
    assert (root / STAGED_TREE / "src" / "c.py").exists()
    assert (root / STAGED_OWNER).exists()
    monkeypatch.setattr(source_staging, "_mount_id", real)
    assert staging.remove_staging_root(staged.root, staged.owner, staged.nonce) is Removal.REMOVED
    assert not root.exists()


def _reconciled(tmp_path: Path, staging: SourceStaging, repo: Path, roots):
    """Reconcile journal entries for ``roots`` (launch ids) through the fake backend."""
    entries = [
        LifecycleObject(
            dataclasses.replace(OWNER, launch=launch),
            ObjectKind.STAGING_ROOT,
            StagingRoot(staging.common_dir, KEY, launch).path,
            NONCE,
        )
        for launch in roots
    ]
    events: list[tuple] = []
    journal = Journal(events, lambda: False)
    journal.entries.extend(entries)
    backend = StagingBackend(events, staging, _host(repo, tmp_path))
    still_open = reconcile(backend, journal, RUNTIME)
    assert list(still_open) == journal.entries
    return {obj.owner.launch for obj in still_open}


def test_reconciliation_removes_a_launchs_leftovers_and_leaves_what_is_not_its_own(tmp_path):
    review = review_repo(tmp_path)
    staging, staged, _ = stage(tmp_path, review, "L-1")  # a crash after the root was written
    path = {
        f"L-{n}": Path(StagingRoot(staging.common_dir, KEY, f"L-{n}").path) for n in range(1, 6)
    }
    # A crash right after the directory was made: it holds nothing.
    path["L-2"].mkdir(mode=0o700)
    # A crash right after its owner record was written.
    path["L-3"].mkdir(mode=0o700)
    (path["L-3"] / STAGED_OWNER).write_bytes(
        owner_record(KEY, dataclasses.replace(OWNER, launch="L-3"), NONCE)
    )
    # A real directory substituted at a launch's path.
    (path["L-4"] / STAGED_TREE).mkdir(parents=True)
    (path["L-4"] / STAGED_TREE / "keep.txt").write_text("keep\n")
    # Something else than a directory there.
    path["L-5"].write_text("keep\n")
    still_open = _reconciled(tmp_path, staging, review.repo, ["L-1", "L-2", "L-3", "L-4", "L-5"])
    assert still_open == {"L-4", "L-5"}
    for launch in ("L-1", "L-2", "L-3"):
        assert not path[launch].exists()
    assert (path["L-4"] / STAGED_TREE / "keep.txt").read_text() == "keep\n"
    assert path["L-5"].read_text() == "keep\n"


def test_reconciliation_descends_into_no_mount_below_a_leftover(tmp_path, monkeypatch):
    review = review_repo(tmp_path)
    staging, staged, _ = stage(tmp_path, review, "L-1")
    root = Path(staged.root.path)
    before = snapshot(root)
    mounted_at(monkeypatch, "tree/src")
    assert _reconciled(tmp_path, staging, review.repo, ["L-1"]) == {"L-1"}
    assert snapshot(root) == before


# -- the owner record's publication, cut short (PR #504 R2-F2) ------------------------------------

# Where the controller is killed while it publishes a launch's owner record,
# and what its directory then holds: each entry by name (``tmp`` for
# safefs's named temporary) and its bytes, given the record. ``unnamed``
# points take the O_TMPFILE path, ``named`` ones the named temporary a
# filesystem without O_TMPFILE gets.
INTERRUPTIONS: dict[str, Callable[[bytes], list[tuple[str, bytes]]]] = {
    "made, nothing written": lambda record: [],
    "unnamed, part written": lambda record: [],
    "unnamed, whole and not linked": lambda record: [],
    "named, part written": lambda record: [("tmp", record[:8])],
    "named, whole and not linked": lambda record: [("tmp", record)],
    "named, linked and not removed": lambda record: [(STAGED_OWNER, record), ("tmp", record)],
    "published": lambda record: [(STAGED_OWNER, record)],
}
_INTERRUPT_CHILD = (
    "import sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from tests.test_source_staging import interrupted_staging\n"
    "interrupted_staging(Path(sys.argv[2]), *sys.argv[3:])\n"
)
NOT_REACHED, NO_TMPFILE = 3, 4


def interrupted_staging(tmp: Path, head: str, merge_base: str, launch: str, point: str) -> None:
    """Stage ``launch`` and end this process at ``point`` (the child process runs this).

    The end is ``os._exit``, as a kill is: no ``finally``, no handler and
    no clean-up of the controller's runs. :data:`NOT_REACHED` says the point
    was never met, :data:`NO_TMPFILE` that an ``unnamed`` point cannot be
    met on this filesystem.
    """
    tempfile.tempdir = str(tmp / "temporary")
    staging = staging_for(tmp / "repo")
    seed, gi = bound(staging, git_input("REVIEW", head, merge_base, launch=launch))
    root = staging.staging_root(owner_of(gi))
    assert root is not None
    real_write, real_link, real_unlink, real_fdopen = os.write, os.link, os.unlink, os.fdopen
    create_exclusive = safefs.SafeRoot.create_exclusive

    def killed(*_args, **_kwargs):
        os._exit(0)

    if point.startswith("named"):
        safefs._O_TMPFILE = 0  # as on a filesystem without O_TMPFILE
    elif point.startswith("unnamed"):
        safefs.SafeRoot._durable_tmp_at = lambda *_a, **_k: os._exit(NO_TMPFILE)  # type: ignore[method-assign]
    if point == "made, nothing written":
        safefs.SafeRoot.create_exclusive = killed  # type: ignore[method-assign]
    elif point == "published":

        def published(self, *args, **kwargs):
            create_exclusive(self, *args, **kwargs)
            os._exit(0)

        safefs.SafeRoot.create_exclusive = published  # type: ignore[method-assign]
    elif point == "unnamed, part written":

        def write(fd, data):
            if bytes(data[:9]) == b'{"issue":':
                real_write(fd, bytes(data[:8]))
                os._exit(0)
            return real_write(fd, data)

        os.write = write
    elif point == "named, part written":

        class Cut:
            def __init__(self, fd: int) -> None:
                self.fd = fd

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def write(self, data):
                real_write(self.fd, bytes(data[:8]))
                os._exit(0)

        def fdopen(fd, mode="r", *args, **kwargs):
            if mode == "wb":
                return Cut(fd)
            return real_fdopen(fd, mode, *args, **kwargs)

        os.fdopen = fdopen
    elif point.endswith("whole and not linked"):

        def link(src, dst, *args, **kwargs):
            if dst == STAGED_OWNER:
                os._exit(0)
            return real_link(src, dst, *args, **kwargs)

        os.link = link
    elif point == "named, linked and not removed":

        def unlink(name, *args, **kwargs):
            if safefs.is_temporary_name(str(name)):
                os._exit(0)
            return real_unlink(name, *args, **kwargs)

        os.unlink = unlink
    staging.make_staging_root(root, seed, gi, owner=owner_of(gi), nonce=NONCE)
    os._exit(NOT_REACHED)


@pytest.mark.parametrize("point", list(INTERRUPTIONS))
def test_an_owner_record_publication_cut_short_leaves_what_reconciliation_removes(tmp_path, point):
    """Killed anywhere in its owner record's publication, a staging root is never left with a
    partial record, and lifecycle reconciliation removes what is left and closes its entry."""
    review = review_repo(tmp_path)
    launch = "L-7"
    res = subprocess.run(
        [
            sys.executable,
            "-c",
            _INTERRUPT_CHILD,
            str(REPO_ROOT),
            str(tmp_path),
            review.head,
            review.merge_base,
            launch,
            point,
        ],
        capture_output=True,
        timeout=300,
        cwd=str(REPO_ROOT),
    )
    if res.returncode == NO_TMPFILE:
        pytest.skip("this filesystem has no O_TMPFILE: the unnamed publication is untested here")
    assert res.returncode == 0, res.stderr.decode(errors="replace")[-4000:]
    staging = staging_for(review.repo)
    path = Path(StagingRoot(staging.common_dir, KEY, launch).path)
    record = owner_record(KEY, dataclasses.replace(OWNER, launch=launch), NONCE)
    held = sorted(
        ("tmp" if safefs.is_temporary_name(name) else name, (path / name).read_bytes())
        for name in os.listdir(path)
    )
    assert held == INTERRUPTIONS[point](record)
    assert _reconciled(tmp_path, staging, review.repo, [launch]) == set()
    assert not path.exists()


TEMPORARY = ".af-tmp-1f-0123456789abcdef"
NOT_ITS_OWN: dict[str, Callable[[bytes], dict[str, bytes | None]]] = {
    "a partial owner record": lambda record: {STAGED_OWNER: record[:8]},
    "an empty owner record": lambda record: {STAGED_OWNER: b""},
    "a temporary of another entry's record": lambda record: {
        TEMPORARY: owner_record(KEY, OWNER, "n-other")
    },
    "a temporary longer than the record": lambda record: {TEMPORARY: record + b"\n"},
    "a temporary beside another entry": lambda record: {TEMPORARY: record[:8], STAGED_TREE: None},
    "two temporaries": lambda record: {TEMPORARY: record[:8], ".af-tmp-1f-fedcba9876543210": b""},
    "a name that is not safefs's temporary": lambda record: {".af-tmp-x": record[:8]},
}


@pytest.mark.parametrize("case", list(NOT_ITS_OWN))
def test_removal_accepts_no_owner_record_a_publication_never_leaves(tmp_path, case):
    """Only the launch's own leftovers are removed: a partial or foreign record is not one."""
    review = review_repo(tmp_path)
    staging = staging_for(review.repo)
    root = StagingRoot(staging.common_dir, KEY, "L-1")
    path = Path(root.path)
    path.mkdir(parents=True, mode=0o700)
    for name, data in NOT_ITS_OWN[case](owner_record(KEY, OWNER, NONCE)).items():
        if data is None:
            (path / name).mkdir()
        else:
            (path / name).write_bytes(data)
    before = snapshot(path)
    assert staging.remove_staging_root(root, OWNER, NONCE) is Removal.UNCERTAIN
    assert snapshot(path) == before


def test_removal_leaves_a_temporary_with_another_name(tmp_path):
    """A temporary that is a hard link of a file elsewhere is not the publication's own."""
    review = review_repo(tmp_path)
    staging = staging_for(review.repo)
    root = StagingRoot(staging.common_dir, KEY, "L-1")
    path = Path(root.path)
    path.mkdir(parents=True, mode=0o700)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(owner_record(KEY, OWNER, NONCE)[:8])
    os.link(elsewhere, path / TEMPORARY)
    assert staging.remove_staging_root(root, OWNER, NONCE) is Removal.UNCERTAIN
    assert (path / TEMPORARY).exists() and elsewhere.read_bytes() == b'{"issue"'


# -- real mounts, in a user and mount namespace of the test's own (PR #504 R1-F1, R1-F2) ----

REPO_ROOT = Path(__file__).resolve().parents[1]
MOUNT_SCENARIOS = ("directory", "file", "tmpfs", "root", "layout")
_CHILD = (
    "import json, sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from tests.test_source_staging import real_mount_scenario\n"
    "print(json.dumps(real_mount_scenario(Path(sys.argv[2]), sys.argv[3])))\n"
)


def _namespaces(tmp_path: Path) -> list[str] | None:
    """``unshare``'s argv for a user and mount namespace, or ``None`` when this host refuses one."""
    unshare = shutil.which("unshare")
    if unshare is None or shutil.which("mount") is None:
        return None
    argv = [unshare, "--user", "--map-root-user", "--mount"]
    probe = tmp_path / "probe"
    (probe / "a").mkdir(parents=True)
    (probe / "b").mkdir()
    res = subprocess.run(
        [*argv, "mount", "--bind", str(probe / "a"), str(probe / "b")],
        capture_output=True,
        timeout=60,
    )
    shutil.rmtree(probe)
    return argv if res.returncode == 0 else None


def _mount(*args: str) -> None:
    subprocess.run(["mount", *args], check=True, capture_output=True, timeout=60)


def real_mount_scenario(tmp: Path, scenario: str) -> dict[str, object]:
    """Stage, mount, prove and remove, inside the namespace (the child process runs this).

    The bind mounts come from the staging root's own filesystem (one
    device, another mount); the tmpfs is another device. The namespace and
    every mount in it end with the child.
    """
    tempfile.tempdir = str(tmp / "temporary")
    Path(tempfile.tempdir).mkdir(exist_ok=True)
    review = review_repo(tmp)
    outside = tmp / "outside"
    (outside / "dir").mkdir(parents=True)
    (outside / "dir" / "keep.txt").write_text("keep\n")
    (outside / "file.txt").write_text("keep\n")
    report: dict[str, object] = {}
    if scenario in WRITE_MOUNTS:
        return _mounted_while_written(tmp, review, outside / "dir", scenario)
    if scenario == "layout":
        staging = staging_for(review.repo)
        seed, gi = bound(staging, review_input(review))
        stage_dir = common_of(review.repo) / "autoforge" / "sandbox" / KEY / "stage"
        stage_dir.mkdir(parents=True)
        _mount("--bind", str(outside / "dir"), str(stage_dir))
        root = staging.staging_root(owner_of(gi))
        assert root is not None
        try:
            staging.make_staging_root(root, seed, gi, owner=owner_of(gi), nonce=NONCE)
            report["made"] = "made"
        except SandboxLaunchRefusedError as exc:
            report["made"] = str(exc)
        report["outside"] = sorted(os.listdir(outside / "dir"))
        return report
    staging, staged, _ = stage(tmp, review)
    root = Path(staged.root.path)
    if scenario == "directory":
        _mount("--bind", str(outside / "dir"), str(root / STAGED_TREE / "src"))
    elif scenario == "file":
        _mount("--bind", str(outside / "file.txt"), str(root / STAGED_TREE / "README"))
    elif scenario == "tmpfs":
        _mount("-t", "tmpfs", "tmpfs", str(root / STAGED_TREE / "src"))
        (root / STAGED_TREE / "src" / "keep.txt").write_text("keep\n")
    else:
        _mount("--bind", str(outside / "dir"), str(root))
    try:
        staging.reprove(staged)
        report["reprove"] = "proved"
    except SandboxLaunchRefusedError as exc:
        report["reprove"] = str(exc)
    report["removal"] = staging.remove_staging_root(staged.root, staged.owner, staged.nonce).value
    report["outside"] = sorted(os.listdir(outside / "dir"))
    report["outside file"] = (outside / "file.txt").read_text()
    report["tmpfs"] = sorted(os.listdir(root / STAGED_TREE / "src")) if scenario == "tmpfs" else []
    report["owner record"] = (root / STAGED_OWNER).exists()
    return report


# A bind mount from the staging root's own filesystem made while the root is
# written (PR #504 R2-F1): at a directory's path between its mkdir and its
# open, by the tree's writer and by the read context's copy, and at a
# read-context directory's path after it was made and checked, before a
# file is written through it.
WRITE_MOUNTS = {
    "tree/src at its mkdir": "tree/src",
    "read-context/files at its mkdir": "read-context/files",
    "read-context/files after its check": "read-context/files",
}


def _outside_state(directory: Path) -> list[tuple[str, int, str]]:
    """``directory`` and what it holds: each entry's name, permission bits and text."""
    found = [("", stat.S_IMODE(directory.stat().st_mode), "")]
    for path in sorted(directory.iterdir()):
        text = path.read_text(errors="replace") if path.is_file() else "<not a file>"
        found.append((path.name, stat.S_IMODE(path.lstat().st_mode), text))
    return found


def _mounted_while_written(
    tmp: Path, review: Review, outside: Path, scenario: str
) -> dict[str, object]:
    staging = staging_for(review.repo)
    seed, gi = bound(staging, review_input(review))
    context = str(read_context(tmp))
    root = staging.staging_root(owner_of(gi))
    assert root is not None
    outside.chmod(0o700)
    report: dict[str, object] = {"before": _outside_state(outside)}
    target = Path(root.path) / WRITE_MOUNTS[scenario]
    mounted: list[str] = []

    def mount() -> None:
        if not mounted:
            _mount("--bind", str(outside), str(target))
            mounted.append(str(target))

    if scenario.endswith("at its mkdir"):
        module, name, real = os, "mkdir", os.mkdir

        def mkdir(path, *args, **kwargs):
            real(path, *args, **kwargs)
            if target.is_dir():
                mount()

        patched = mkdir
    else:
        module, name, real = source_staging, "_descend", source_staging._descend

        def descend(parent, relpath, mount_id):
            if relpath == "files":
                mount()
            return real(parent, relpath, mount_id)

        patched = descend
    setattr(module, name, patched)
    try:
        staging.make_staging_root(root, seed, gi, context, owner=owner_of(gi), nonce=NONCE)
        report["made"] = "made"
    except SandboxLaunchRefusedError as exc:
        report["made"] = str(exc)
    finally:
        setattr(module, name, real)
    report["mounted"] = mounted
    report["after"] = _outside_state(outside)
    report["removal"] = staging.remove_staging_root(root, owner_of(gi), NONCE).value
    report["after removal"] = _outside_state(outside)
    return report


@pytest.mark.parametrize("scenario", list(WRITE_MOUNTS))
def test_a_real_mount_made_while_the_root_is_written_receives_nothing(tmp_path, scenario):
    """PR #504 R2-F1, with real mounts: the directory outside is left as it was, its entries,
    bytes and permission bits, by the write that met the mount and by the removal after it.

    Skipped, and so untested, where the host refuses an unprivileged user
    namespace; the same checks run on simulated mount ids above.
    """
    argv = _namespaces(tmp_path)
    if argv is None:
        pytest.skip("this host gives no unprivileged user and mount namespace: untested here")
    res = subprocess.run(
        [*argv, sys.executable, "-c", _CHILD, str(REPO_ROOT), str(tmp_path), scenario],
        capture_output=True,
        timeout=300,
        cwd=str(REPO_ROOT),
    )
    assert res.returncode == 0, res.stderr.decode(errors="replace")[-4000:]
    report = json.loads(res.stdout.decode().strip().splitlines()[-1])
    assert report["mounted"], "the mount was never made"
    before = [tuple(item) for item in report["before"]]
    assert [(name, text) for name, _, text in before] == [("", ""), ("keep.txt", "keep\n")]
    assert before[0][1] == 0o700  # the writer would have set 0755 on a directory it wrote
    assert [tuple(item) for item in report["after"]] == before
    assert f"something is mounted at {report['mounted'][0]}" in str(report["made"])
    assert report["removal"] == Removal.UNCERTAIN.value
    assert [tuple(item) for item in report["after removal"]] == before


@pytest.mark.parametrize("scenario", MOUNT_SCENARIOS)
def test_real_mounts_are_refused_and_never_removed_through(tmp_path, scenario):
    """Real mounts, from the same filesystem and another, on a directory and on a file.

    Skipped, and so untested, where the host refuses an unprivileged user
    namespace; the same checks run on simulated mount ids above.
    """
    argv = _namespaces(tmp_path)
    if argv is None:
        pytest.skip("this host gives no unprivileged user and mount namespace: untested here")
    res = subprocess.run(
        [*argv, sys.executable, "-c", _CHILD, str(REPO_ROOT), str(tmp_path), scenario],
        capture_output=True,
        timeout=300,
        cwd=str(REPO_ROOT),
    )
    assert res.returncode == 0, res.stderr.decode(errors="replace")[-4000:]
    report = json.loads(res.stdout.decode().strip().splitlines()[-1])
    if scenario == "layout":
        assert "is a mount point" in str(report["made"])
        assert report["outside"] == ["keep.txt"]  # nothing was created in the mount
        return
    expected = {
        "directory": "mounted at tree/src below it",
        "file": "mounted at tree/README below it",
        "tmpfs": "mounted at tree/src below it",
        "root": "is a mount point",
    }[scenario]
    assert expected in str(report["reprove"])
    assert report["removal"] == Removal.UNCERTAIN.value
    assert report["outside"] == ["keep.txt"]
    assert report["outside file"] == "keep\n"
    if scenario == "tmpfs":
        assert report["tmpfs"] == ["keep.txt"]
    if scenario != "root":
        assert report["owner record"] is True  # nothing of the launch's was removed either


# -- the copy-in form (D12) ---------------------------------------------------------------------


def test_the_copy_in_form_gets_the_seed_alone_and_keeps_no_snapshot(tmp_path):
    review = review_repo(tmp_path)
    staging = staging_for(review.repo, StagingMethod.COPY)
    seed, gi = bound(staging, review_input(review))
    assert staging.staging_root(OWNER) is None
    with pytest.raises(SandboxLaunchRefusedError, match="copy-in form stages nothing"):
        staging.make_staging_root(
            StagingRoot(staging.common_dir, KEY, "L-1"), seed, gi, owner=OWNER, nonce=NONCE
        )
    assert not (common_of(review.repo) / "autoforge").exists()
    assert sorted(os.listdir(seed.directory)) == sorted([SEED_MANIFEST_NAME, SEED_PACK_NAME])
    # The seed carries the exact tree: a stager writes it from the pack alone.
    staged = stage_seed(Path(seed.directory), tmp_path / "workspace")
    assert out(staged, "rev-parse", "HEAD^{tree}") == out(
        review.repo, "rev-parse", f"{review.head}^{{tree}}"
    )
    staging.discard_seed(seed)
    assert not Path(seed.directory).exists()


# -- hostile input on the host path (D2) --------------------------------------------------------


def _blob(repo: Path, content: bytes) -> str:
    return out(repo, "hash-object", "-w", "--no-filters", "--stdin", data=content)


def _tree(repo: Path, entries: list[tuple[str, str, str, str]]) -> str:
    data = "".join(f"{mode} {kind} {oid}\t{name}\0" for mode, kind, oid, name in entries).encode()
    return out(repo, "mktree", "-z", "--missing", data=data)


HOSTILE_ATTRIBUTES = (
    b"*.txt filter=evil\n"
    b"*.id ident\n"
    b"*.crlf eol=crlf\n"
    b"*.text text\n"
    b"*.bin filter=lfs diff=lfs merge=lfs -text\n"
)
POINTER = b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"a" * 64 + b"\nsize 12\n"


def test_hostile_attributes_lfs_and_a_gitlink_stage_as_the_committed_bytes(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    files = {
        ".gitattributes": HOSTILE_ATTRIBUTES,
        "a.txt": b"filtered?\n",
        "b.id": b"$Id$\n",
        "c.crlf": b"one\ntwo\n",
        "d.text": b"mixed\r\nlf\n",
        "e.bin": POINTER,
    }
    blobs = {name: _blob(repo, content) for name, content in files.items()}
    submodule = commit_files(init_repo(tmp_path / "sub"), "sub", {"x": b"x\n"})
    tree = _tree(
        repo,
        [("100644", "blob", oid, name) for name, oid in blobs.items()]
        + [("160000", "commit", submodule, "sub")],
    )
    commit = out(repo, "commit-tree", tree, "-m", "hostile")
    # The operator's global configuration: a filter driver and git-lfs, each
    # writing a sentinel and rewriting bytes, and autocrlf.
    sentinel = tmp_path / "sentinel"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    lfs = bin_dir / "git-lfs"
    lfs.write_text(f"#!/bin/sh\ntouch {sentinel}\necho smudged\n")
    lfs.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        f'[filter "evil"]\n\tclean = sh -c "touch {sentinel}; tr a-z A-Z"\n'
        f'\tsmudge = sh -c "touch {sentinel}; tr a-z A-Z"\n\trequired = true\n'
        '[filter "lfs"]\n\tclean = git-lfs clean -- %f\n\tsmudge = git-lfs smudge -- %f\n'
        "\tprocess = git-lfs filter-process\n\trequired = true\n"
        "[core]\n\tautocrlf = true\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    staging = staging_for(repo)
    gi = git_input("ANALYZE_EXECUTE", commit, commit)
    built = staging.build(gi)
    seed = as_seed(built)
    gi = gi.with_seed_manifest(seed.identity)
    root = staging.staging_root(owner_of(gi))
    assert root is not None
    staged = staging.make_staging_root(root, seed, gi, owner=owner_of(gi), nonce=NONCE)
    staging.reprove(staged)

    tree_root = Path(staged.root.path) / STAGED_TREE
    for name, content in files.items():
        assert (tree_root / name).read_bytes() == content, name
    assert (tree_root / "e.bin").read_bytes().startswith(b"version https://git-lfs")
    assert (tree_root / "sub").is_dir() and not os.listdir(tree_root / "sub")
    # The pack's blobs are the same bytes.
    unpacked = stage_seed(Path(seed.directory), tmp_path / "unpacked")
    for name, oid in blobs.items():
        assert run_git(unpacked, "cat-file", "blob", oid) == files[name], name
    assert not sentinel.exists()
    cases = {line.split(":")[0] for line in seed.diagnostics}
    assert {SUBMODULE, LFS, CONVERSIONS} <= cases


def test_a_sparse_checkout_is_diagnosed_and_the_whole_tree_is_staged(tmp_path):
    review = review_repo(tmp_path)
    run_git(review.repo, "sparse-checkout", "set", "--no-cone", "/README")
    staging, staged, seed = stage(tmp_path, review)
    assert any(line.startswith(SPARSE) for line in seed.diagnostics)
    tracked = set(out(review.repo, "ls-tree", "-r", "--name-only", review.head).splitlines())
    tree = Path(staged.root.path) / STAGED_TREE
    assert {str(p.relative_to(tree)) for p in tree.rglob("*") if p.is_file()} == tracked


# -- a partial-clone shared store (regression of the shared-store path) ---------------------------


@dataclasses.dataclass
class PartialClone:
    clone: Path
    commit: str
    connections: Path


def partial_clone(tmp_path: Path, *, checkout: bool) -> PartialClone:
    """A blobless clone of a ``file://`` promisor remote, which records every connection."""
    origin = init_repo(tmp_path / "origin")
    commit = commit_files(origin, "one", {"a.txt": b"hello\n", "b/c.txt": b"world\n"})
    run_git(origin, "config", "uploadpack.allowFilter", "true")
    clone = tmp_path / "clone"
    # The clone's own checkout fetches HEAD's blobs lazily, so this one git
    # may: every other runs with GIT_NO_LAZY_FETCH.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "clone",
            "-q",
            *([] if checkout else ["--no-checkout"]),
            "--filter=blob:none",
            f"file://{origin}",
            str(clone),
        ],
        check=True,
        capture_output=True,
        env=env,
    )
    connections = tmp_path / "connections.log"
    recorder = tmp_path / "recording-upload-pack"
    recorder.write_text(f'#!/bin/sh\necho "$@" >> {connections}\nexec git-upload-pack "$@"\n')
    recorder.chmod(0o755)
    run_git(clone, "config", "remote.origin.uploadpack", str(recorder))
    return PartialClone(clone, commit, connections)


def test_a_partial_clones_missing_blob_refuses_the_seed_with_no_fetch_and_no_write(tmp_path):
    """The seed step reads the store with no lazy fetch: the promisor remote is never
    contacted, and the store's files are byte-identical afterwards (ADR 0013 §7, #292)."""
    store = partial_clone(tmp_path, checkout=False)
    common = common_of(store.clone)
    before = snapshot(common)
    staging = SourceStaging(
        common, KEY, SeedSettings(), StagingMethod.BIND, git_lock=RecordingLock()
    )
    with pytest.raises(SandboxLaunchRefusedError, match="seed is refused"):
        staging.build(git_input("ANALYZE_EXECUTE", store.commit, store.commit))
    assert not store.connections.exists()
    assert snapshot(common) == before


def test_a_partial_clone_holding_every_needed_object_seeds_with_its_diagnostic(tmp_path):
    store = partial_clone(tmp_path, checkout=True)  # the checkout fetched HEAD's blobs
    staging = SourceStaging(
        common_of(store.clone), KEY, SeedSettings(), StagingMethod.BIND, git_lock=RecordingLock()
    )
    built = staging.build(git_input("ANALYZE_EXECUTE", store.commit, store.commit))
    assert any(line.startswith(PARTIAL_CLONE) for line in as_seed(built).diagnostics)
    assert not store.connections.exists()


def ignoring_no_lazy_fetch(requests: list | None = None):
    """A runner as a git older than 2.44 sees it: ``GIT_NO_LAZY_FETCH`` is not there."""

    def run(request):
        request.env = {k: v for k, v in (request.env or {}).items() if k != "GIT_NO_LAZY_FETCH"}
        if requests is not None:
            requests.append(request)
        return execute(request)

    return run


def test_a_git_that_ignores_no_lazy_fetch_still_fetches_nothing_for_the_seed(tmp_path):
    """No promisor remote is configured where the store's objects are read (PR #504 R1-F6)."""
    store = partial_clone(tmp_path, checkout=False)
    common = common_of(store.clone)
    run_git(store.clone, "tag", "v1", store.commit)
    before = snapshot(common)
    staging = SourceStaging(
        common,
        KEY,
        SeedSettings(history_tags=True),
        StagingMethod.BIND,
        runner=ignoring_no_lazy_fetch(),
        git_lock=RecordingLock(),
    )
    with pytest.raises(SandboxLaunchRefusedError, match="seed is refused"):
        staging.build(git_input("ANALYZE_EXECUTE", store.commit, store.commit))
    assert not store.connections.exists()
    assert snapshot(common) == before


def test_a_git_that_ignores_no_lazy_fetch_still_fetches_nothing_for_the_staging_root(tmp_path):
    """The bind form's tree reads the store the same way: a blob it lacks refuses the tree."""
    store = partial_clone(tmp_path, checkout=True)  # HEAD's blobs are fetched
    clone = store.clone
    common = common_of(clone)
    # A commit of the clone's own: its blob is a loose object of the store,
    # which the promisor remote does not have.
    commit = commit_files(clone, "local", {"local.txt": b"only here\n"})
    blob = out(clone, "rev-parse", f"{commit}:local.txt")
    staging = SourceStaging(
        common,
        KEY,
        SeedSettings(),
        StagingMethod.BIND,
        runner=ignoring_no_lazy_fetch(),
        git_lock=RecordingLock(),
    )
    seed, gi = bound(staging, git_input("ANALYZE_EXECUTE", commit, commit))
    # The blob leaves the store after the seed was built and checked.
    (common / "objects" / blob[:2] / blob[2:]).unlink()
    before = snapshot(common)
    root = staging.staging_root(owner_of(gi))
    assert root is not None
    with pytest.raises(SandboxLaunchRefusedError, match="tree could not be written"):
        staging.make_staging_root(root, seed, gi, owner=owner_of(gi), nonce=NONCE)
    assert not store.connections.exists()
    after = {k: v for k, v in snapshot(common).items() if not k.startswith("autoforge")}
    assert after == before


def _git_dir_of(request) -> str:
    command = list(request.command)
    if "--git-dir" in command:
        return command[command.index("--git-dir") + 1]
    return (request.env or {}).get("GIT_DIR", "")


def test_every_git_that_reads_the_stores_objects_runs_in_a_directory_of_the_controllers(
    tmp_path,
):
    """The seed step, the staging root's tree and its reproof (PR #504 R1-F6).

    Each git that reads an object reads the store as an alternate from a
    private git directory with no configuration of the operator's; the
    store itself is asked only for its tag refs and two configuration
    values, which read no object.
    """
    review = review_repo(tmp_path)
    requests: list = []
    staging = SourceStaging(
        common_of(review.repo),
        KEY,
        SeedSettings(history_tags=True),
        StagingMethod.BIND,
        runner=ignoring_no_lazy_fetch(requests),
        git_lock=RecordingLock(),
    )
    seed, gi = bound(staging, review_input(review))
    root = staging.staging_root(owner_of(gi))
    assert root is not None
    staged = staging.make_staging_root(root, seed, gi, owner=owner_of(gi), nonce=NONCE)
    staging.reprove(staged)
    common = staging.common_dir
    in_store = [r for r in requests if _git_dir_of(r) == common]
    private = [r for r in requests if _git_dir_of(r) != common]
    assert private and in_store
    for request in in_store:
        command = list(request.command)
        sub = command[command.index("--git-dir") + 2 :]
        assert sub[0] in ("config", "for-each-ref"), sub
        assert "objecttype" not in " ".join(sub)
    for request in private:
        env = request.env
        assert env["GIT_DIR"] == _git_dir_of(request)
        assert not env["GIT_DIR"].startswith(common)
        assert common not in request.command
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert env["HOME"] == env["GIT_DIR"]
        alternate = env.get("GIT_ALTERNATE_OBJECT_DIRECTORIES")
        assert alternate in (None, f'"{common}/objects"')
    # The tree's and the reproof's git read the store through the alternate.
    reads = [r for r in private if "-C" in r.command]
    assert reads and all(r.env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] for r in reads)


def test_the_existing_materialize_path_still_fetches_a_partial_clones_blob_lazily(tmp_path):
    """Documents a defect of the existing shared-store path, deliberately not fixed here.

    ``materialize()`` (the pre-merge export's and validation's tree) runs
    the hardened ``local_git_request``, which does not set
    ``GIT_NO_LAZY_FETCH``: in a partial clone, reading a missing blob
    contacts the promisor remote and writes the fetched objects to the
    shared store. ADR 0013 §7 has #292 record this and leave it to an
    issue of its own (PR #504's R1-F7 asks for that follow-up). The seed
    step and the staging root do not inherit it (the tests above). When the
    existing path is fixed, this test is turned around to assert no
    connection.
    """
    store = partial_clone(tmp_path, checkout=False)
    with materialize(execute, str(store.clone), store.commit, "sha1", prefix="existing-") as tree:
        assert (tree.root / "a.txt").read_bytes() == b"hello\n"
    assert store.connections.exists()


# -- the lifecycle, on a container-shaped fake backend (D13) --------------------------------------


class Journal:
    """What the engine journals, as events, with the staging root's existence at each write."""

    def __init__(self, events: list[tuple], staging_root: Callable[[], bool]) -> None:
        self.events = events
        self.entries: list[LifecycleObject] = []
        self.staging_root = staging_root

    def open_entries(self) -> tuple[LifecycleObject, ...]:
        return tuple(self.entries)

    def record_creates(self, runtime, objects) -> None:
        self.events.append(
            ("journal", "creates", tuple(o.kind for o in objects), self.staging_root())
        )
        self.entries.extend(objects)

    def record_removals(self, objects) -> None:
        self.events.append(("journal", "removals", tuple(o.kind for o in objects)))

    def close_entries(self, objects) -> None:
        self.events.append(("journal", "close", tuple(o.kind for o in objects)))
        for obj in objects:
            self.entries.remove(obj)


class StagingBackend:
    """A container-shaped backend whose source is the production :class:`SourceStaging`.

    Its creates check and render #270's :class:`ContainerSpec` for the stager
    and the launch container, so the arguments a runtime would get say how
    ``/source`` and ``/workspace`` are presented. ``on_staged`` runs right
    after the staging root is written and proved, before any container's
    create: a test changes the host directory there.
    """

    def __init__(self, events: list[tuple], staging: SourceStaging, host: HostContext) -> None:
        self.events = events
        self.staging = staging
        self.host = host
        self.inputs: LaunchInputs | None = None
        self.staged: StagedSource | None = None
        self.creates: dict[ObjectKind, list[str]] = {}
        self.copied: list[tuple[str, tuple[str, ...]]] = []
        self.on_staged: Callable[[StagedSource], None] | None = None

    @property
    def kind(self) -> BackendKind:
        return BackendKind.CONTAINER

    def seed(self, inputs: LaunchInputs, launch: LaunchDescription) -> Seed:
        seed = self.staging.seed(inputs)
        self.events.append(("host", "seed", Path(seed.directory).is_dir()))
        return seed

    def discard_seed(self, seed: Seed) -> None:
        self.staging.discard_seed(seed)

    def objects(self, inputs, seed, launch) -> tuple[LifecycleObject, ...]:
        self.inputs = inputs
        owner = inputs.owner
        root = self.staging.staging_root(owner)
        source = (root.path,) if root is not None else ()
        objects = [
            LifecycleObject(owner, ObjectKind.VOLUME, "vol-1", "n-vol"),
            LifecycleObject(
                owner,
                ObjectKind.STAGER_CONTAINER,
                "stager-1",
                "n-stg",
                NETWORK_NONE,
                (*source, "vol-1"),
            ),
            LifecycleObject(
                owner,
                ObjectKind.LAUNCH_CONTAINER,
                "launch-1",
                "n-lch",
                NETWORK_BRIDGE,
                (*source, "vol-1"),
            ),
        ]
        if root is not None:
            objects.append(LifecycleObject(owner, ObjectKind.STAGING_ROOT, root.path, "n-root"))
        return tuple(objects)

    def make(self, obj: LifecycleObject, seed: Seed, launch: LaunchDescription) -> None:
        assert self.inputs is not None
        if obj.kind is ObjectKind.STAGING_ROOT:
            root = self.staging.staging_root(self.inputs.owner)
            assert root is not None and root.path == obj.name
            self.staged = self.staging.make_staging_root(
                root,
                seed,
                self.inputs.git_input,
                self.inputs.read_context,
                owner=obj.owner,
                nonce=obj.nonce,
            )
            self.events.append(("host", "staging root written and proved"))
            if self.on_staged is not None:
                self.on_staged(self.staged)
            return
        if obj.kind is ObjectKind.VOLUME:
            self.events.append(("runtime", "volume create"))
            return
        role = (
            ContainerRole.STAGER
            if obj.kind is ObjectKind.STAGER_CONTAINER
            else ContainerRole.LAUNCH
        )
        source = None
        if self.staging.method is StagingMethod.BIND:
            # The hand-off: the staging root proved again right before a
            # create binds it.
            assert self.staged is not None
            self.staging.reprove(self.staged)
            self.events.append(("host", "reproved", role))
            source = self.staged.root
        spec = ContainerSpec(
            role=role,
            network=ContainerNetwork(obj.network),
            workspace_volume="vol-1",
            staging=self.staging.method,
            source=source,
        )
        check_spec(spec, self.host)
        self.creates[obj.kind] = create_options(RuntimeProduct.PODMAN, spec)
        self.events.append(("runtime", "create", role))

    def stage(self, stager: LifecycleObject, seed: Seed) -> None:
        if self.staging.method is StagingMethod.COPY:
            self.copied.append((stager.name, tuple(sorted(os.listdir(seed.directory)))))
        self.events.append(("runtime", "stage"))

    def start(self, container: LifecycleObject) -> None:
        self.events.append(("runtime", "start"))

    def prepare(self, launch, handle, command, *, cwd, env):  # pragma: no cover - not run
        raise AssertionError("the tests stop before the run step")

    def exporter(self, handle: LaunchHandle) -> tuple[LifecycleObject, ...]:
        return ()

    def export(self, objects, handle) -> Artifact:  # pragma: no cover - nothing exported
        raise AssertionError("nothing is exported")

    def remove(self, obj: LifecycleObject) -> Removal:
        if obj.kind is ObjectKind.STAGING_ROOT:
            root = self.staging.staging_root(obj.owner)
            assert root is not None
            outcome = self.staging.remove_staging_root(root, obj.owner, obj.nonce)
            self.events.append(("host", "staging root removed", outcome))
            return outcome
        self.events.append(("runtime", "rm", obj.kind))
        return Removal.REMOVED

    # -- unused at a launch ---------------------------------------------------------
    def runtime_check(self) -> RuntimeReport:  # pragma: no cover
        return RUNTIME

    def reconcile(self, entries, runtime) -> tuple[LifecycleObject, ...]:
        """Remove-only: a staging root through the production removal, in either form."""
        gone = []
        for obj in entries:
            if obj.kind is ObjectKind.STAGING_ROOT:
                root = StagingRoot(self.staging.common_dir, self.staging.key, obj.owner.launch)
                outcome = self.staging.remove_staging_root(root, obj.owner, obj.nonce)
                self.events.append(("host", "staging root reconciled", outcome))
                if outcome is Removal.REMOVED:
                    gone.append(obj)
                continue
            self.events.append(("runtime", "rm", obj.kind))
            gone.append(obj)
        return tuple(gone)

    def probe(self, *, home, environ) -> ProbeResult:  # pragma: no cover
        return ProbeResult(True, "", "fake")

    def plan(self, request) -> LaunchDescription:  # pragma: no cover
        return PLANNED


def _host(repo: Path, tmp_path: Path) -> HostContext:
    common = str(common_of(repo))
    home = tmp_path / "operator-home"
    home.mkdir(exist_ok=True)
    return HostContext(
        home=str(home),
        environ={},
        uid=os.getuid(),
        git_common_dir=common,
        checkouts=(str(repo),),
        denied_roots=(str(Path(common) / "autoforge"), str(repo)),
    )


def _lifecycle_run(tmp_path: Path, method: StagingMethod, *, settings=None, on_staged=None):
    review = review_repo(tmp_path)
    staging = staging_for(review.repo, method, settings)
    root = staging.staging_root(OWNER)
    exists = (lambda: Path(root.path).exists()) if root is not None else (lambda: False)
    events: list[tuple] = []
    journal = Journal(events, exists)
    backend = StagingBackend(events, staging, _host(review.repo, tmp_path))
    backend.on_staged = on_staged
    inputs = LaunchInputs(
        OWNER,
        workspace=str(review.repo),
        read_context=str(read_context(tmp_path)),
        git_input=review_input(review),
    )
    saved: list[LaunchGitInput] = []
    error: BaseException | None = None
    try:
        with LaunchLifecycle(
            backend, journal, inputs, RUNTIME, change_producing=False, on_seed=saved.append
        ) as lifecycle:
            lifecycle.launch(lambda: lifecycle.establish(PLANNED))
    except SandboxLaunchRefusedError as exc:
        error = exc
    return review, staging, backend, journal, events, saved, root, error


def _at(events: list[tuple], predicate) -> int:
    return next(i for i, event in enumerate(events) if predicate(event))


def test_the_bind_form_stages_after_its_entry_and_presents_source_read_only(tmp_path):
    review, staging, backend, journal, events, saved, root, error = _lifecycle_run(
        tmp_path, StagingMethod.BIND
    )
    assert error is None
    assert root is not None
    # The seed was built and checked, in its temporary directory, before any entry.
    seeded = _at(events, lambda e: e[:2] == ("host", "seed"))
    entry = _at(events, lambda e: e[:2] == ("journal", "creates"))
    assert seeded < entry and events[seeded][2] is True
    assert saved and saved[0].seed_manifest_digest
    # The staging root's entry exists before the root does; it is written and
    # proved after it, and proved again before each create that binds it.
    assert events[entry][3] is False
    assert ObjectKind.STAGING_ROOT in events[entry][2]
    written = _at(events, lambda e: e == ("host", "staging root written and proved"))
    stager = _at(events, lambda e: e == ("runtime", "create", ContainerRole.STAGER))
    launch = _at(events, lambda e: e == ("runtime", "create", ContainerRole.LAUNCH))
    assert entry < written < stager < launch
    assert events[stager - 1] == ("host", "reproved", ContainerRole.STAGER)
    assert events[launch - 1] == ("host", "reproved", ContainerRole.LAUNCH)
    # /source is the staging root, bound read-only; /workspace the volume, writable.
    bind = f"--mount=type=bind,source={root.path},target=/source,readonly=true"
    volume = "--mount=type=volume,source=vol-1,target=/workspace"
    for kind in (ObjectKind.STAGER_CONTAINER, ObjectKind.LAUNCH_CONTAINER):
        assert bind in backend.creates[kind]
        assert volume in backend.creates[kind]
        assert not any("target=/workspace" in a and "readonly" in a for a in backend.creates[kind])
    # The release removed the staging root right after the run step.
    assert ("host", "staging root removed", Removal.REMOVED) in events
    assert not Path(root.path).exists()
    assert journal.entries == []


def test_the_copy_in_form_hands_its_stager_the_seed_alone_and_stages_nothing_on_the_host(
    tmp_path,
):
    review, staging, backend, journal, events, saved, root, error = _lifecycle_run(
        tmp_path, StagingMethod.COPY
    )
    assert error is None and root is None
    created = next(e for e in events if e[:2] == ("journal", "creates"))
    assert ObjectKind.STAGING_ROOT not in created[2]
    assert backend.copied == [("stager-1", tuple(sorted([SEED_MANIFEST_NAME, SEED_PACK_NAME])))]
    for kind in (ObjectKind.STAGER_CONTAINER, ObjectKind.LAUNCH_CONTAINER):
        assert not any("target=/source" in arg for arg in backend.creates[kind])
        assert "--mount=type=volume,source=vol-1,target=/workspace" in backend.creates[kind]
    assert not (common_of(review.repo) / "autoforge").exists()


def test_a_seed_that_fails_its_checks_leaves_no_journal_entry_and_no_create(tmp_path, temporary):
    review, staging, backend, journal, events, saved, root, error = _lifecycle_run(
        tmp_path, StagingMethod.BIND, settings=SeedSettings(max_objects=2)
    )
    assert isinstance(error, SandboxLaunchRefusedError)
    assert "sandbox.container.seed_max_objects" in str(error)
    assert not [e for e in events if e[0] in ("journal", "runtime")]
    assert saved == []
    assert root is not None and not Path(root.path).exists()
    assert list(temporary.iterdir()) == []


def test_a_tree_changed_between_its_proof_and_the_hand_off_is_refused_and_removed(tmp_path):
    def tamper(staged: StagedSource) -> None:
        target = Path(staged.root.path) / STAGED_TREE / "README"
        target.write_bytes(target.read_bytes() + b"tampered\n")

    review, staging, backend, journal, events, saved, root, error = _lifecycle_run(
        tmp_path, StagingMethod.BIND, on_staged=tamper
    )
    assert isinstance(error, SandboxLaunchRefusedError)
    assert "does not hash back" in str(error)
    assert not [e for e in events if e[:2] == ("runtime", "create")]
    assert root is not None and not Path(root.path).exists()
    assert ("host", "staging root removed", Removal.REMOVED) in events
    assert journal.entries == []


def test_the_seed_carries_d2s_diagnostics_to_the_launch(tmp_path):
    review = review_repo(tmp_path)
    repo = review.repo
    (repo / ".gitattributes").write_text("*.bin filter=lfs -text\n")
    run_git(repo, "add", ".gitattributes")
    run_git(repo, "commit", "-q", "-m", "lfs")
    head = out(repo, "rev-parse", "HEAD")
    staging = staging_for(repo)
    seed, _ = bound(staging, git_input("REVIEW", head, review.merge_base))
    assert any(line.startswith(f"{LFS}: unsupported for content") for line in seed.diagnostics)


def test_the_staged_tree_is_the_carried_commits_for_a_relaunch(tmp_path):
    review = review_repo(tmp_path)
    run_git(review.repo, "checkout", "-q", "--detach", review.head)
    carried = commit_files(review.repo, "earlier launch", {"fix.txt": b"fixed\n"})
    staging = staging_for(review.repo)
    gi = git_input("FIX", review.head, review.merge_base, carried=carried)
    seed, gi = bound(staging, gi)
    root = staging.staging_root(owner_of(gi))
    assert root is not None
    staged = staging.make_staging_root(root, seed, gi, owner=owner_of(gi), nonce=NONCE)
    assert staged.commit == carried
    assert (Path(root.path) / STAGED_TREE / "fix.txt").read_bytes() == b"fixed\n"
    staging.reprove(staged)
