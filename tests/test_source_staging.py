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
import os
import stat
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

import pytest

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
from autoforge.sandbox_lifecycle import LaunchLifecycle
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
    STAGED_READ_CONTEXT,
    STAGED_SEED,
    STAGED_TREE,
    SourceStaging,
    StagedSource,
    as_seed,
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


def bound(staging: SourceStaging, gi: LaunchGitInput) -> tuple[Seed, LaunchGitInput]:
    """The seed step and its end: the seed built, the input completed with its digest."""
    seed = staging.seed(LaunchInputs(OWNER, workspace="/nowhere", git_input=gi))
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
    owner = dataclasses.replace(OWNER, launch=launch)
    root = staging.staging_root(owner)
    assert root is not None
    staged = staging.make_staging_root(root, seed, gi, str(read_context(tmp_path)))
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


def test_nothing_mounted_below_the_staging_root_passes_its_proof(tmp_path, monkeypatch):
    """A directory on another device below the root (a mount nested in it) is refused."""
    review = review_repo(tmp_path)
    staging, staged, _ = stage(tmp_path, review)
    real = os.lstat

    def lstat(path, *args, **kwargs):
        found = real(path, *args, **kwargs)
        if path == "src" and stat.S_ISDIR(found.st_mode):
            values = list(found)
            values[stat.ST_DEV] = found.st_dev + 1
            return os.stat_result(values)
        return found

    monkeypatch.setattr(os, "lstat", lstat)
    with pytest.raises(SandboxLaunchRefusedError, match="mounted at tree/src"):
        staging.reprove(staged)


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
            staging.make_staging_root(root, seed, refused_input)
    with pytest.raises(SandboxLaunchRefusedError, match="seed is not written"):
        staging.make_staging_root(root, NO_SEED, bound_input)
    assert not Path(root.path).exists()
    # Swapped after the input was bound: the copy is hashed, and refused.
    manifest = Path(seed.directory) / SEED_MANIFEST_NAME
    manifest.write_bytes(manifest.read_bytes().replace(b'"issue":292', b'"issue":293'))
    with pytest.raises(SandboxLaunchRefusedError):
        staging.make_staging_root(root, seed, bound_input)
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
        staging.make_staging_root(staged.root, seed, gi)


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
            staging.make_staging_root(root, seed, gi)  # type: ignore[arg-type]
    for key in ("..", "a/b", ""):
        with pytest.raises(SandboxLaunchRefusedError):
            SourceStaging(staging.common_dir, key, SeedSettings(), StagingMethod.BIND)
    with pytest.raises(SandboxLaunchRefusedError):
        staging.staging_root(dataclasses.replace(OWNER, launch="a/b"))


def test_removing_the_staging_root_is_idempotent_and_scoped_to_its_launch(tmp_path):
    review = review_repo(tmp_path)
    staging, first, _ = stage(tmp_path / "a", review, "L-1")
    _, second, _ = stage(tmp_path / "b", review, "L-2")
    assert staging.remove_staging_root(first.root) is Removal.REMOVED
    assert not Path(first.root.path).exists()
    assert Path(second.root.path).exists()  # another launch's is untouched
    assert staging.remove_staging_root(first.root) is Removal.REMOVED
    never = StagingRoot(staging.common_dir, KEY, "L-9")
    assert staging.remove_staging_root(never) is Removal.REMOVED
    other = SourceStaging(staging.common_dir, "999", SeedSettings(), StagingMethod.BIND)
    assert other.remove_staging_root(StagingRoot(staging.common_dir, "999", "L-1")) is (
        Removal.REMOVED
    )
    # A bind-form leftover is removed after the runtime's form became copy-in.
    copy = SourceStaging(staging.common_dir, KEY, SeedSettings(), StagingMethod.COPY)
    assert copy.remove_staging_root(second.root) is Removal.REMOVED
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
    assert staging.remove_staging_root(staged.root) is Removal.UNCERTAIN
    assert (target / "keep").read_text() == "keep\n"


# -- the copy-in form (D12) ---------------------------------------------------------------------


def test_the_copy_in_form_gets_the_seed_alone_and_keeps_no_snapshot(tmp_path):
    review = review_repo(tmp_path)
    staging = staging_for(review.repo, StagingMethod.COPY)
    seed, gi = bound(staging, review_input(review))
    assert staging.staging_root(OWNER) is None
    with pytest.raises(SandboxLaunchRefusedError, match="copy-in form stages nothing"):
        staging.make_staging_root(StagingRoot(staging.common_dir, KEY, "L-1"), seed, gi)
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
    root = staging.staging_root(dataclasses.replace(OWNER, phase="ANALYZE_EXECUTE"))
    assert root is not None
    staged = staging.make_staging_root(root, seed, gi)
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


def test_the_existing_materialize_path_still_fetches_a_partial_clones_blob_lazily(tmp_path):
    """Documents a defect of the existing shared-store path, deliberately not fixed here.

    ``materialize()`` (the pre-merge export's and validation's tree) runs
    the hardened ``local_git_request``, which does not set
    ``GIT_NO_LAZY_FETCH``: in a partial clone, reading a missing blob
    contacts the promisor remote and writes the fetched objects to the
    shared store. ADR 0013 §7 has #292 record this and file it as an issue
    of its own. The seed step and the staging root do not inherit it (the
    tests above). When the existing path is fixed, this test is turned
    around to assert no connection.
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
                root, seed, self.inputs.git_input, self.inputs.read_context
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
            outcome = self.staging.remove_staging_root(root)
            self.events.append(("host", "staging root removed", outcome))
            return outcome
        self.events.append(("runtime", "rm", obj.kind))
        return Removal.REMOVED

    # -- unused at a launch ---------------------------------------------------------
    def runtime_check(self) -> RuntimeReport:  # pragma: no cover
        return RUNTIME

    def reconcile(self, entries, runtime):  # pragma: no cover
        return ()

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
    root = staging.staging_root(dataclasses.replace(OWNER, phase="FIX"))
    assert root is not None
    staged = staging.make_staging_root(root, seed, gi)
    assert staged.commit == carried
    assert (Path(root.path) / STAGED_TREE / "fix.txt").read_bytes() == b"fixed\n"
    staging.reprove(staged)
