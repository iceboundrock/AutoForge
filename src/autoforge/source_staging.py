"""The host side of a container launch's source: both staging forms, backend-neutral (#292).

ADR 0013 D2 and D12. A container launch never sees the operator's
checkout, a worktree or the git common dir. It gets one exact commit, as a
snapshot the controller made from the shared object store, in one of two
forms, and #271's probe chooses between them per runtime:

- **the bind form**, where the runtime is proven to share the controller's
  filesystem: a *staging root*, ``<git common dir>/autoforge/sandbox/<key>/
  stage/<launch id>`` (:class:`~autoforge.sandbox_container.StagingRoot`),
  bound read-only at ``/source`` into the launch's stager container and its
  launch container. It holds the materialized tree (:data:`STAGED_TREE`,
  the seed commit's, or the carried commit's for a relaunch), the seed pack
  and its manifest (:data:`STAGED_SEED`) and a copy of the launch's read
  context (:data:`STAGED_READ_CONTEXT`);
- **the copy-in form**, everywhere else: the seed alone, which the stage
  step copies into the stager container. Nothing is staged on the host
  beside it and no tree is kept, because the seed already carries the
  exact tree and the prover proves it inside the container (D2).

:class:`SourceStaging` is the interface a backend calls for both, and
holds no runtime: the seed step's :meth:`~SourceStaging.seed` (the seed
built and checked in a temporary directory, before any journal entry:
:mod:`autoforge.seed_pack`); the create step's
:meth:`~SourceStaging.make_staging_root` (bind form only, after the
lifecycle journaled the staging root and before any container's create);
:meth:`~SourceStaging.reprove`, immediately before the staging root is
handed to a container's create; and :meth:`~SourceStaging.remove_staging_root`,
the release's and reconciliation's.

**The staging root.**

- It is created through :mod:`autoforge.safefs` under L1 (the shared-Git
  lock, ADR 0008 D12.1): every directory from the common dir down is
  reached one component at a time, never through a link, and the launch's
  own directory is made exclusively, mode ``0700``. One that exists already
  is refused: a staging root is never reused or re-proved for another
  launch (ADR 0013 D3, D6), and reconciliation removes a leftover.
- The seed is copied into it only while its manifest still hashes to the
  digest the launch's git input binds, read from the bytes copied; the
  pack's SHA-256 is the manifest's. A seed whose digest differs is never
  written there (D4's hand-off).
- The tree is written by :func:`~autoforge.materialize.materialize_into`,
  the writer and hash-back proof of ADR 0006 D6.2 and D6.3, from blobs:
  nothing is checked out, so no attribute, filter driver, LFS smudge or
  hook of the repository applies, and a gitlink is an empty directory. Its
  git runs with no lazy fetch (:func:`~autoforge.seed_pack.no_lazy_fetch`).
- A read-only bind does not make the host directory immutable, so the
  tree is proved again right before each hand-off
  (:meth:`~SourceStaging.reprove`): the staging root is reached again from
  the common dir, must be the very directory created (its device and inode,
  so a directory substituted at its path is refused), hold exactly its
  three entries, have nothing mounted below it, hold the same seed, and
  hash back to the commit's tree read afresh from the store. A byte, a
  mode or an entry changed since the first proof fails it.
- What a container's user needs to read it is recorded as data
  (:class:`SourceOwnership`): the host owner and the modes, as written.
  Nothing here changes ownership for any backend (#271 decides).

Executor layer: no workflow, no journal, no state; the order, the journal
and every decision are the engine's lifecycle's
(:mod:`autoforge.sandbox_lifecycle`).
"""

from __future__ import annotations

import errno
import hashlib
import os
import posixpath
import re
import shutil
import stat
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from . import materialize as materialize_mod
from .errors import (
    AutoForgeError,
    CandidateNotMaterializableError,
    GitLockTimeout,
    LockError,
    MaterializationError,
    SandboxLaunchRefusedError,
)
from .executor import execute
from .git_input import SEED_MANIFEST_NAME, LaunchGitInput, seed_problem
from .locking import SharedGitLock
from .safefs import SafeRoot, StateError, split_relpath
from .sandbox_backend import NO_SEED, LaunchInputs, LaunchOwner, Removal, Seed
from .sandbox_container import SANDBOX_LAYOUT, STAGE_DIR, StagingMethod, StagingRoot
from .seed_pack import (
    SEED_FILES,
    SEED_PACK_NAME,
    BuiltSeed,
    Runner,
    SeedSettings,
    build_seed,
    discard,
    no_lazy_fetch,
    parse_manifest,
)

# The staging root's entries (D12): the materialized tree, the seed pack and
# its manifest, and the copy of the launch's read context.
STAGED_TREE = "tree"
STAGED_SEED = "seed"
STAGED_READ_CONTEXT = "read-context"
STAGED_ENTRIES = (STAGED_READ_CONTEXT, STAGED_SEED, STAGED_TREE)

# The mode safefs creates every directory of the staging root's own layout
# with, and the mode its copied files get: the operator's alone.
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

# A read context is a handful of bounded documents (ADR 0004 D10); a copy
# past these is refused rather than made.
MAX_READ_CONTEXT_ENTRIES = 1000
MAX_READ_CONTEXT_BYTES = 64 * 1024 * 1024

# One path component of a staging root's key and launch id, as #270's
# StagingRoot check accepts it.
_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_CHUNK_BYTES = 1024 * 1024

GitLock = Callable[[str], AbstractContextManager[object]]


def _refused(text: str) -> SandboxLaunchRefusedError:
    return SandboxLaunchRefusedError(f"the launch's staging root is refused: {text}")


@dataclass(frozen=True)
class SourceOwnership:
    """Who owns the staged files on the host, and their modes: data for #271, never acted on here.

    ``uid`` and ``gid`` own every file and directory of the staging root
    (the operator's, which a rootless runtime maps to the container's root);
    ``private_directory_mode`` is the staging root's own and its seed and
    read-context directories'; ``private_file_mode`` the seed's and the read
    context's files'; the tree's directories, files and executable files have
    :data:`~autoforge.materialize.DIRECTORY_MODE`,
    :data:`~autoforge.materialize.FILE_MODE` and
    :data:`~autoforge.materialize.EXECUTABLE_MODE`, whatever the umask.
    """

    uid: int
    gid: int
    private_directory_mode: int = PRIVATE_DIRECTORY_MODE
    private_file_mode: int = PRIVATE_FILE_MODE
    tree_directory_mode: int = materialize_mod.DIRECTORY_MODE
    tree_file_mode: int = materialize_mod.FILE_MODE
    tree_executable_mode: int = materialize_mod.EXECUTABLE_MODE


@dataclass(frozen=True)
class StagedSource:
    """A staging root the create step wrote and proved (the bind form).

    ``commit`` is the commit whose tree :data:`STAGED_TREE` holds, ``tree``
    that tree's id; ``seed_digest`` the digest of the seed copied into
    :data:`STAGED_SEED`; ``identity`` the ``(st_dev, st_ino)`` of the
    directory created, which every later proof requires its path to still
    name.
    """

    root: StagingRoot
    commit: str
    tree: str
    object_format: str
    files: int
    gitlinks: int
    seed_digest: str
    identity: tuple[int, int]
    ownership: SourceOwnership


def staged_commit(git_input: LaunchGitInput) -> str:
    """The commit a launch's tree is: the carried commit of a relaunch, else the seed (D4)."""
    return git_input.carried or git_input.seed


class SourceStaging:
    """The host side of one issue's container launches' source, in one staging form (D2, D12).

    ``common_dir`` is the controller's git common dir (the shared object
    store), ``key`` the issue's sandbox key (its worktree's name),
    ``settings`` the operator's seed settings, ``method`` the form #271's
    probe chose for the runtime. ``git_lock(operation)`` is L1 around the
    staging root's creation, by default :class:`~autoforge.locking.SharedGitLock`
    on the common dir.
    """

    def __init__(
        self,
        common_dir: str | Path,
        key: str,
        settings: SeedSettings,
        method: StagingMethod,
        *,
        runner: Runner | None = None,
        git_lock: GitLock | None = None,
    ) -> None:
        if not isinstance(method, StagingMethod):
            raise _refused(f"{method!r} is not a staging form")
        if not isinstance(key, str) or not _COMPONENT_RE.fullmatch(key):
            raise _refused(f"the sandbox key {key!r} is not one path component")
        self.common_dir = os.path.realpath(common_dir)
        self.key = key
        self.settings = settings
        self.method = method
        self._runner = runner or execute
        self._git_lock = git_lock or (
            lambda operation: SharedGitLock(self.common_dir, operation=operation)
        )

    # -- the seed step ------------------------------------------------------------
    def seed(self, inputs: LaunchInputs) -> Seed:
        """The launch's seed, built and checked in a temporary directory (D4, D13).

        Before any journal entry: a refusal leaves nothing to reconcile.
        Both forms build the same seed; only what the create step stages
        beside it differs.
        """
        if inputs.git_input is None:
            raise SandboxLaunchRefusedError(
                "the launch's seed is refused: the launch carries no git input to seed from"
            )
        built = self.build(inputs.git_input)
        return as_seed(built)

    def build(self, git_input: LaunchGitInput) -> BuiltSeed:
        """The checked seed of ``git_input``, with what its checks read."""
        return build_seed(Path(self.common_dir), git_input, self.settings, self._runner)

    def discard_seed(self, seed: Seed) -> None:
        """Remove the seed's temporary directory; idempotent."""
        if seed != NO_SEED and seed.directory:
            discard(seed.directory)

    # -- the create step (bind form) ---------------------------------------------
    def staging_root(self, owner: LaunchOwner) -> StagingRoot | None:
        """The launch's staging root in the bind form; ``None`` in the copy-in form."""
        if self.method is not StagingMethod.BIND:
            return None
        if not _COMPONENT_RE.fullmatch(owner.launch):
            raise _refused(f"the launch id {owner.launch!r} is not one path component")
        return StagingRoot(self.common_dir, self.key, owner.launch)

    def make_staging_root(
        self,
        root: StagingRoot,
        seed: Seed,
        git_input: LaunchGitInput | None,
        read_context: str = "",
    ) -> StagedSource:
        """Write ``root`` and prove it (the create step, after its journal entry).

        Refused, with nothing written, when the form stages no tree, the
        root is not this staging's own, or the seed's manifest is not the
        one ``git_input`` binds. The launch's directory is created
        exclusively under L1; then the seed (re-hashed as it is copied), the
        read context and the tree are written into it and the tree proved.
        A failure after the directory exists leaves it for the release (or
        reconciliation) to remove, as its journal entry says.
        """
        self._check_root(root)
        problem = seed_problem(seed.directory, git_input) if seed != NO_SEED else "no seed"
        if problem:
            raise _refused(f"its seed is not written into it: {problem}")
        assert git_input is not None
        commit = staged_commit(git_input)
        self._create(root)
        try:
            with SafeRoot.open(self.common_dir) as common, common.subroot(root.layout) as staged:
                identity = staged.identity
                info = os.fstat(staged.fd)
                digest = self._copy_seed(staged, seed, git_input.seed_manifest_digest)
                self._copy_read_context(staged, read_context)
                staged.ensure_dir(STAGED_TREE)
                with staged.subroot(STAGED_TREE) as tree_root:
                    os.fchmod(tree_root.fd, materialize_mod.DIRECTORY_MODE)
                    tree = materialize_mod.materialize_into(
                        no_lazy_fetch(self._runner),
                        self.common_dir,
                        commit,
                        git_input.object_format,
                        tree_root,
                    )
        except (MaterializationError, CandidateNotMaterializableError) as exc:
            raise _refused(f"its tree could not be written and proved: {exc}") from exc
        except (StateError, OSError) as exc:
            raise _refused(f"it could not be written ({type(exc).__name__}: {exc})") from exc
        return StagedSource(
            root=root,
            commit=commit,
            tree=tree.tree,
            object_format=git_input.object_format,
            files=tree.files,
            gitlinks=tree.gitlinks,
            seed_digest=digest,
            identity=identity,
            ownership=SourceOwnership(uid=info.st_uid, gid=info.st_gid),
        )

    def _check_root(self, root: StagingRoot, *, staging: bool = True) -> None:
        """``root`` is one of this issue's staging roots; with ``staging``, in the bind form."""
        if staging and self.method is not StagingMethod.BIND:
            raise _refused("the copy-in form stages nothing on the host")
        if not isinstance(root, StagingRoot):
            raise _refused("a staging root is the launch's StagingRoot, never a path alone")
        if root.common_dir != self.common_dir or root.key != self.key:
            raise _refused(f"{root.path} is not one of this issue's staging roots")
        if not _COMPONENT_RE.fullmatch(root.launch):
            raise _refused(f"the launch id {root.launch!r} is not one path component")
        try:
            split_relpath(root.layout)
        except StateError as exc:
            raise _refused(str(exc)) from exc

    def _create(self, root: StagingRoot) -> None:
        """The launch's directory, made exclusively below the layout, under L1."""
        parent = posixpath.join(SANDBOX_LAYOUT, self.key, STAGE_DIR)
        try:
            with self._git_lock("create a launch's staging root"):
                with SafeRoot.open(self.common_dir) as common:
                    common.ensure_dir(parent)
                    with common.subroot(parent) as stage:
                        os.mkdir(root.launch, PRIVATE_DIRECTORY_MODE, dir_fd=stage.fd)
        except FileExistsError as exc:
            raise _refused(
                f"{root.path} exists already; a staging root is never reused, and "
                "reconciliation removes a leftover"
            ) from exc
        except (GitLockTimeout, LockError) as exc:
            raise _refused(f"the shared-Git lock was not obtained ({exc})") from exc
        except (AutoForgeError, OSError) as exc:
            raise _refused(f"it could not be created ({type(exc).__name__}: {exc})") from exc

    def _copy_seed(self, staged: SafeRoot, seed: Seed, digest: str) -> str:
        """Copy the seed's two files: the manifest must hash to ``digest``, the pack to its own."""
        staged.ensure_dir(STAGED_SEED)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        src_dir = os.open(seed.directory, flags | os.O_DIRECTORY)
        try:
            if sorted(os.listdir(src_dir)) != sorted(SEED_FILES):
                raise _refused("its seed's directory does not hold exactly the seed's two files")
            with staged.subroot(STAGED_SEED) as out:
                manifest, manifest_sha = _copy_file(src_dir, out, SEED_MANIFEST_NAME)
                if "sha256:" + manifest_sha != digest:
                    raise _refused(
                        f"the seed manifest copied hashes to sha256:{manifest_sha}, not {digest}, "
                        "which the launch's git input binds"
                    )
                pack_sha = _copy_file(src_dir, out, SEED_PACK_NAME, keep=False)[1]
        finally:
            os.close(src_dir)
        if parse_manifest(manifest).pack_sha256 != pack_sha:
            raise _refused("the seed pack copied is not the one its manifest names")
        return digest

    def _copy_read_context(self, staged: SafeRoot, read_context: str) -> None:
        """A copy of the launch's read context: its directories and regular files, nothing else."""
        staged.ensure_dir(STAGED_READ_CONTEXT)
        if not read_context:
            return
        total = 0
        with (
            SafeRoot.open(read_context) as source,
            staged.subroot(STAGED_READ_CONTEXT) as out,
        ):
            for entry in source.walk(max_entries=MAX_READ_CONTEXT_ENTRIES):
                if entry.is_dir:
                    out.ensure_dir(entry.relpath)
                    continue
                if not entry.is_regular:
                    raise _refused(
                        f"the read context's {entry.relpath} is neither a file nor a directory"
                    )
                total += entry.st.st_size
                if total > MAX_READ_CONTEXT_BYTES:
                    raise _refused(f"the read context is over {MAX_READ_CONTEXT_BYTES} bytes")
                data = source.read_bytes(entry.relpath, limit=MAX_READ_CONTEXT_BYTES)
                if data is None:
                    raise _refused(f"the read context's {entry.relpath} vanished while copied")
                out.create_exclusive(entry.relpath, data, mode=PRIVATE_FILE_MODE)

    # -- the hand-off ---------------------------------------------------------------
    def reprove(self, staged: StagedSource) -> None:
        """Prove ``staged`` again, immediately before a container's create binds it (D2, D12).

        Refused when its path no longer names the directory created, its
        entries are not exactly its three, something is mounted below it,
        its seed is not the one copied, or its tree does not hash back to
        the commit's tree read afresh from the store.
        """
        self._check_root(staged.root)
        try:
            with (
                SafeRoot.open(self.common_dir) as common,
                common.subroot(staged.root.layout) as root,
            ):
                if root.identity != staged.identity:
                    raise _refused(
                        f"{staged.root.path} is no longer the directory created for the launch "
                        "(a directory was substituted at its path)"
                    )
                held = sorted(os.listdir(root.fd))
                if held != sorted(STAGED_ENTRIES):
                    raise _refused(
                        f"{staged.root.path} holds {', '.join(held) or 'nothing'}, not exactly "
                        f"{', '.join(STAGED_ENTRIES)}"
                    )
                _no_mount_below(root)
                with root.subroot(STAGED_SEED) as seed:
                    self._reprove_seed(seed, staged.seed_digest)
                with root.subroot(STAGED_TREE) as tree:
                    proved = materialize_mod.prove_tree(
                        no_lazy_fetch(self._runner),
                        self.common_dir,
                        staged.commit,
                        staged.object_format,
                        tree,
                    )
        except FileNotFoundError as exc:
            raise _refused(f"{staged.root.path} is gone") from exc
        except (MaterializationError, CandidateNotMaterializableError) as exc:
            raise _refused(f"its tree no longer proves: {exc}") from exc
        except (StateError, OSError) as exc:
            raise _refused(f"it could not be proved ({type(exc).__name__}: {exc})") from exc
        if proved.tree != staged.tree:
            raise _refused(f"its tree is {proved.tree}, not {staged.tree} as staged")

    def _reprove_seed(self, seed: SafeRoot, digest: str) -> None:
        if sorted(os.listdir(seed.fd)) != sorted(SEED_FILES):
            raise _refused("its seed directory does not hold exactly the seed's two files")
        manifest, manifest_sha = _hash_file(seed.fd, SEED_MANIFEST_NAME, keep=True)
        if "sha256:" + manifest_sha != digest:
            raise _refused("its seed manifest is not the one staged")
        if _hash_file(seed.fd, SEED_PACK_NAME)[1] != parse_manifest(manifest).pack_sha256:
            raise _refused("its seed pack is not the one its manifest names")

    # -- the release and reconciliation -------------------------------------------
    def remove_staging_root(self, root: StagingRoot) -> Removal:
        """Remove ``root``; :attr:`Removal.REMOVED` only when it is verified gone. Idempotent.

        Reached from the common dir one component at a time; the launch's
        directory is removed relative to its parent's descriptor, never
        following a link. A path that holds something other than a directory
        is left in place and uncertain: it is not what the launch made. In
        either form: reconciliation may remove a bind-form leftover once the
        runtime's form changed.
        """
        self._check_root(root, staging=False)
        parent = root.layout.rsplit("/", 1)[0]
        try:
            with SafeRoot.open(self.common_dir) as common:
                try:
                    stage = common.subroot(parent)
                except FileNotFoundError:
                    return Removal.REMOVED
                with stage:
                    try:
                        info = os.lstat(root.launch, dir_fd=stage.fd)
                    except FileNotFoundError:
                        return Removal.REMOVED
                    if not stat.S_ISDIR(info.st_mode):
                        return Removal.UNCERTAIN
                    shutil.rmtree(root.launch, dir_fd=stage.fd)
                    try:
                        os.lstat(root.launch, dir_fd=stage.fd)
                    except FileNotFoundError:
                        return Removal.REMOVED
                    return Removal.UNCERTAIN
        except (StateError, OSError):
            return Removal.UNCERTAIN


def as_seed(built: BuiltSeed) -> Seed:
    """The seed step's :class:`~autoforge.sandbox_backend.Seed` of ``built``, diagnostics too."""
    return Seed(
        identity=built.digest,
        directory=str(built.directory),
        diagnostics=tuple(item.render() for item in built.selection.diagnostics),
    )


def _copy_file(src_dir: int, out: SafeRoot, name: str, *, keep: bool = True) -> tuple[bytes, str]:
    """Copy ``name`` into ``out`` exclusively, hashing it; its bytes too when ``keep``."""
    src = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=src_dir)
    try:
        if not stat.S_ISREG(os.fstat(src).st_mode):
            raise _refused(f"its seed's {name} is not a regular file")
        os.set_blocking(src, True)
        dst = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            PRIVATE_FILE_MODE,
            dir_fd=out.fd,
        )
        digest = hashlib.sha256()
        kept = bytearray()
        try:
            while chunk := os.read(src, _CHUNK_BYTES):
                digest.update(chunk)
                if keep:
                    kept += chunk
                view = memoryview(chunk)
                while view:
                    view = view[os.write(dst, view) :]
            os.fsync(dst)
        finally:
            os.close(dst)
    finally:
        os.close(src)
    return bytes(kept), digest.hexdigest()


def _hash_file(dir_fd: int, name: str, *, keep: bool = False) -> tuple[bytes, str]:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _refused(f"its seed's {name} is not a regular file")
        os.set_blocking(fd, True)
        digest = hashlib.sha256()
        kept = bytearray()
        while chunk := os.read(fd, _CHUNK_BYTES):
            digest.update(chunk)
            if keep:
                kept += chunk
    finally:
        os.close(fd)
    return bytes(kept), digest.hexdigest()


def _no_mount_below(root: SafeRoot) -> None:
    """Nothing below ``root`` is on another device: no mount nests in the staging root."""
    device = os.fstat(root.fd).st_dev
    for entry in root.walk():
        if entry.is_dir and entry.st.st_dev != device:
            raise _refused(f"something is mounted at {entry.relpath} below it")
    try:
        parent = os.stat("..", dir_fd=root.fd, follow_symlinks=False)
    except OSError as exc:  # pragma: no cover - the root was just opened
        if exc.errno != errno.ENOENT:
            raise
        return
    if parent.st_dev != device:
        raise _refused("it is itself a mount point")
