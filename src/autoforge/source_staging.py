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
  and its manifest (:data:`STAGED_SEED`), a copy of the launch's read
  context (:data:`STAGED_READ_CONTEXT`) and its owner record
  (:data:`STAGED_OWNER`);
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

**The launch's identity** (PR #504 R1-F3). A seed is built only for the
launch its git input names: the launch's owner (run, issue, phase and
launch id) must be the input's, and a staging root is written only under
that launch's id. A mismatch is refused before anything is built or
created.

**The staging root.**

- It is created through :mod:`autoforge.safefs` under L1 (the shared-Git
  lock, ADR 0008 D12.1): every directory from the common dir down is
  reached one component at a time, never through a link, and must be on
  the common dir's own mount (no mount nests in its layout), checked
  before anything is created inside it. The launch's own directory is made
  exclusively, mode ``0700``, and the first thing written into it is its
  owner record: the sandbox key, the launch's owner and the nonce its
  journal entry carries (:class:`~autoforge.sandbox_backend.LifecycleObject`),
  which binds the directory to that entry. The record is published
  atomically (PR #504 R2-F2): its bytes are written and fsynced whole
  before it gets its name (:meth:`~autoforge.safefs.SafeRoot.create_exclusive`),
  so an interruption leaves no record or the whole one, never a part. One
  that exists already is refused: a staging root is never reused or
  re-proved for another launch (ADR 0013 D3, D6), and reconciliation
  removes a leftover.
- Every directory written in it, its own, its seed's and read context's,
  and each one the tree's writer makes, is made exclusively and checked on
  the descriptor it is held by, before its mode is set or anything is made
  in it: on the staging root's own mount and empty (PR #504 R2-F1). A mount
  made at its path between its ``mkdir`` and its open is refused there,
  with nothing written through it, and a file is written only through a
  directory held and checked that way, reached one component at a time.
- The seed is copied into it only while its manifest still hashes to the
  digest the launch's git input binds, read from the bytes copied, and
  names that input's launch; the pack's SHA-256 is the manifest's. A seed
  whose digest differs is never written there (D4's hand-off).
- The read context is copied as directories and regular files only, and
  what was copied (each entry's path, its kind and a file's SHA-256) is
  recorded (:class:`ContextEntry`).
- The tree is written by :func:`~autoforge.materialize.materialize_into`,
  the writer and hash-back proof of ADR 0006 D6.2 and D6.3, from blobs:
  nothing is checked out, so no attribute, filter driver, LFS smudge or
  hook of the repository applies, and a gitlink is an empty directory. Its
  git reads the store through a git directory of the controller's own
  (:func:`~autoforge.seed_pack.isolated_store`), so no git version fetches
  a partial clone's missing object lazily.
- A read-only bind does not make the host directory immutable, so it is
  proved again right before each hand-off (:meth:`~SourceStaging.reprove`):
  the staging root is reached again from the common dir with no mount in
  its layout, must be the very directory created (its device and inode, so
  a directory substituted at its path is refused), hold exactly its four
  entries and the same owner record, have every entry below it on its own
  mount (by the kernel's mount id, so a bind mount from the same
  filesystem and a file mount are found as well as another device), owned
  as recorded and with the permission bits its record says, hold the same
  seed and the same read context, and hash back to the commit's tree read
  afresh from the store. A byte, a mode, an owner or an entry changed since
  the first proof fails it. The bind form needs the kernel's mount ids
  (Linux's ``/proc/self/fdinfo``); without them it is refused.
- What a container's user needs to read it is recorded as data
  (:class:`SourceOwnership`): the host owner and the modes, as written, and
  every proof checks the staging root against that record. Nothing here
  changes ownership for any backend (#271 decides).
- Its removal (:meth:`~SourceStaging.remove_staging_root`) removes only the
  directory its journal entry names: one holding the owner record of that
  entry's launch and nonce, or nothing at all (a crash right after the
  directory was made), or, where the record went through a named
  temporary (no ``O_TMPFILE``), that temporary alone holding a prefix of
  exactly that record (a crash before it was published). Anything else at
  its path, a directory substituted there or a partial or foreign owner
  record included, is left in place and uncertain. Nothing mounted in it is
  ever descended into: a mount anywhere below it leaves it all in place,
  uncertain, before anything is removed, and the removal itself opens each
  directory without following a link and checks its mount before removing
  what is in it.

Executor layer: no workflow, no journal, no state; the order, the journal
and every decision are the engine's lifecycle's
(:mod:`autoforge.sandbox_lifecycle`).
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
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
from .git_input import MAX_SEED_MANIFEST_BYTES, SEED_MANIFEST_NAME, LaunchGitInput, seed_problem
from .locking import SharedGitLock
from .safefs import SafeRoot, StateError, WalkEntry, is_temporary_name, split_relpath
from .sandbox_backend import NO_SEED, LaunchInputs, LaunchOwner, Removal, Seed
from .sandbox_container import StagingMethod, StagingRoot
from .seed_pack import (
    SEED_FILES,
    SEED_PACK_NAME,
    BuiltSeed,
    Runner,
    SeedSettings,
    build_seed,
    discard,
    isolated_store,
    parse_manifest,
)

# The staging root's entries (D12): its owner record, the materialized
# tree, the seed pack and its manifest, and the copy of the read context.
STAGED_OWNER = "owner"
STAGED_TREE = "tree"
STAGED_SEED = "seed"
STAGED_READ_CONTEXT = "read-context"
STAGED_ENTRIES = (STAGED_OWNER, STAGED_READ_CONTEXT, STAGED_SEED, STAGED_TREE)

# The mode the staging root, its seed and read-context directories are
# set to, and the mode its owner record, seed and read-context files get:
# the operator's alone.
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

# A read context is a handful of bounded documents (ADR 0004 D10); a copy
# past these is refused rather than made.
MAX_READ_CONTEXT_ENTRIES = 1000
MAX_READ_CONTEXT_BYTES = 64 * 1024 * 1024

# The owner record: one line of JSON, far below this bound.
OWNER_RECORD_SCHEMA = 1
MAX_OWNER_RECORD_BYTES = 4096

# One path component of a staging root's key and launch id, as #270's
# StagingRoot check accepts it; a journal entry's nonce.
_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_NONCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}")
_CHUNK_BYTES = 1024 * 1024
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# Non-blocking, so a FIFO swapped in cannot stall a read.
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
# A descriptor of an entry itself, whatever it is, for its mount id: opened
# without following a link, reading nothing (Linux only; 0 elsewhere).
_O_PATH = getattr(os, "O_PATH", 0)
_FDINFO_BYTES = 4096
_OWNER_RWX = stat.S_IRWXU
_IDENTITY_FIELDS = ("run", "issue", "phase", "launch")

GitLock = Callable[[str], AbstractContextManager[object]]


def _refused(text: str) -> SandboxLaunchRefusedError:
    return SandboxLaunchRefusedError(f"the launch's staging root is refused: {text}")


@dataclass(frozen=True)
class SourceOwnership:
    """Who owns the staged files on the host, and their modes: data for #271, never acted on here.

    ``uid`` and ``gid`` own every entry of the staging root (the
    operator's, which a rootless runtime maps to the container's root);
    ``private_directory_mode`` is the staging root's own and every directory
    of its seed and read context; ``private_file_mode`` its owner record's
    and every file of its seed and read context; the tree's directories,
    files and executable files have exactly
    :data:`~autoforge.materialize.DIRECTORY_MODE`,
    :data:`~autoforge.materialize.FILE_MODE` and
    :data:`~autoforge.materialize.EXECUTABLE_MODE`, whatever the umask.
    Every proof of the staging root checks each entry against this record
    (PR #504 R1-F5), so what it says is what is handed off.
    """

    uid: int
    gid: int
    private_directory_mode: int = PRIVATE_DIRECTORY_MODE
    private_file_mode: int = PRIVATE_FILE_MODE
    tree_directory_mode: int = materialize_mod.DIRECTORY_MODE
    tree_file_mode: int = materialize_mod.FILE_MODE
    tree_executable_mode: int = materialize_mod.EXECUTABLE_MODE


@dataclass(frozen=True)
class ContextEntry:
    """One entry of a staged read context: its path, ``"dir"`` or ``"file"``, a file's SHA-256."""

    path: str
    kind: str
    sha256: str = ""


@dataclass(frozen=True)
class StagedSource:
    """A staging root the create step wrote and proved (the bind form).

    ``owner`` and ``nonce`` are the launch and the journal entry its owner
    record names; ``commit`` is the commit whose tree :data:`STAGED_TREE`
    holds, ``tree`` that tree's id; ``seed_digest`` the digest of the seed
    copied into :data:`STAGED_SEED`; ``read_context`` what was copied into
    :data:`STAGED_READ_CONTEXT`; ``identity`` the ``(st_dev, st_ino)`` of
    the directory created, which every later proof requires its path to
    still name.
    """

    root: StagingRoot
    owner: LaunchOwner
    nonce: str
    commit: str
    tree: str
    object_format: str
    files: int
    gitlinks: int
    seed_digest: str
    read_context: tuple[ContextEntry, ...]
    identity: tuple[int, int]
    ownership: SourceOwnership


def staged_commit(git_input: LaunchGitInput) -> str:
    """The commit a launch's tree is: the carried commit of a relaunch, else the seed (D4)."""
    return git_input.carried or git_input.seed


def owner_record(key: str, owner: LaunchOwner, nonce: str) -> bytes:
    """A staging root's owner record: the sandbox key, the launch's owner, its entry's nonce."""
    record = {
        "schema": OWNER_RECORD_SCHEMA,
        "key": key,
        "run": owner.run,
        "issue": owner.issue,
        "phase": owner.phase,
        "launch": owner.launch,
        "nonce": nonce,
    }
    return (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def identity_problem(owner: LaunchOwner, git_input: LaunchGitInput) -> str:
    """Why ``owner`` is not the launch ``git_input`` names, or "" (PR #504 R1-F3)."""
    for name in _IDENTITY_FIELDS:
        have, bound = getattr(owner, name), getattr(git_input, name)
        if have != bound:
            return f"the launch's {name} is {have!r}, and its git input binds {bound!r}"
    return ""


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
        beside it differs. Refused, with nothing built, when the launch's
        owner is not the launch its git input names.
        """
        if inputs.git_input is None:
            raise SandboxLaunchRefusedError(
                "the launch's seed is refused: the launch carries no git input to seed from"
            )
        problem = identity_problem(inputs.owner, inputs.git_input)
        if problem:
            raise SandboxLaunchRefusedError(f"the launch's seed is refused: {problem}")
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
        *,
        owner: LaunchOwner,
        nonce: str,
    ) -> StagedSource:
        """Write ``root`` and prove it (the create step, after its journal entry).

        ``owner`` and ``nonce`` are the launch and the journal entry the
        staging root belongs to. Refused, with nothing written, when the
        form stages no tree, the root is not this staging's own or not
        ``owner``'s, ``owner`` is not the launch ``git_input`` names, a
        directory of its layout is a mount point, or the seed's manifest is
        not the one ``git_input`` binds. The launch's directory is created
        exclusively under L1 and its owner record written first; then the
        seed (re-hashed as it is copied), the read context and the tree are
        written into it, the tree proved and every entry checked against
        the ownership record, with nothing mounted below it. Every directory
        is checked on its held descriptor before anything is written in it.
        A failure after the directory exists leaves it for the release (or
        reconciliation) to remove, as its journal entry says.
        """
        self._check_root(root)
        self._check_owner(root, owner, nonce, git_input)
        problem = seed_problem(seed.directory, git_input) if seed != NO_SEED else "no seed"
        if not problem:
            assert git_input is not None
            problem = _seed_launch_problem(seed.directory, git_input)
        if problem:
            raise _refused(f"its seed is not written into it: {problem}")
        assert git_input is not None
        commit = staged_commit(git_input)
        identity = self._create(root, owner_record(self.key, owner, nonce))
        try:
            with SafeRoot.open(self.common_dir) as common, self._layout(common, root) as staged:
                if staged.identity != identity:
                    raise _refused(
                        f"{root.path} is no longer the directory created for the launch "
                        "(a directory was substituted at its path)"
                    )
                mount = _mount_id(staged.fd, root.path)
                digest = self._copy_seed(staged, seed, git_input, mount)
                context = self._copy_read_context(staged, read_context, mount)
                tree_mode = materialize_mod.DIRECTORY_MODE
                with _new_dir(staged, STAGED_TREE, mount, tree_mode) as tree_root:
                    with isolated_store(self.common_dir, git_input.object_format, self._runner) as (
                        repo,
                        run,
                    ):
                        tree = materialize_mod.materialize_into(
                            run,
                            str(repo),
                            commit,
                            git_input.object_format,
                            tree_root,
                            check_directory=_tree_directory_check(tree_root, mount),
                        )
                info = os.fstat(staged.fd)
                ownership = SourceOwnership(uid=info.st_uid, gid=info.st_gid)
                _check_contents(staged, ownership, mount)
                with staged.subroot(STAGED_READ_CONTEXT) as copied:
                    _check_read_context(copied, context)
        except (MaterializationError, CandidateNotMaterializableError) as exc:
            raise _refused(f"its tree could not be written and proved: {exc}") from exc
        except (StateError, OSError) as exc:
            raise _refused(f"it could not be written ({type(exc).__name__}: {exc})") from exc
        return StagedSource(
            root=root,
            owner=owner,
            nonce=nonce,
            commit=commit,
            tree=tree.tree,
            object_format=git_input.object_format,
            files=tree.files,
            gitlinks=tree.gitlinks,
            seed_digest=digest,
            read_context=context,
            identity=identity,
            ownership=ownership,
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

    def _check_owner(
        self,
        root: StagingRoot,
        owner: LaunchOwner,
        nonce: str,
        git_input: LaunchGitInput | None = None,
    ) -> None:
        """``root`` is ``owner``'s, ``nonce`` a journal entry's, ``owner`` the input's launch."""
        if not isinstance(owner, LaunchOwner):
            raise _refused("a staging root's owner is the launch's LaunchOwner")
        if owner.launch != root.launch:
            raise _refused(f"{root.path} is launch {root.launch}'s, not launch {owner.launch}'s")
        if not isinstance(nonce, str) or not _NONCE_RE.fullmatch(nonce):
            raise _refused(f"{nonce!r} is not a journal entry's nonce")
        if git_input is not None:
            problem = identity_problem(owner, git_input)
            if problem:
                raise _refused(problem)

    def _layout(self, common: SafeRoot, root: StagingRoot) -> SafeRoot:
        """``root``'s directory, reached from the common dir with no mount in its layout."""
        return _open_layout(common, split_relpath(root.layout), create=False)

    def _create(self, root: StagingRoot, record: bytes) -> tuple[int, int]:
        """The launch's directory, made exclusively below the layout under L1; its identity.

        Each directory of the layout is checked to be on the common dir's
        mount before anything is created in it. The new directory, checked
        on its descriptor to be on that mount and empty, is set to mode
        ``0700`` and to the operator's own group (a setgid parent of a
        shared repository would give it another), and its owner record is
        the first thing written into it, published atomically
        (:func:`_publish_owner_record`).
        """
        parts = split_relpath(root.layout)
        try:
            with self._git_lock("create a launch's staging root"):
                with (
                    SafeRoot.open(self.common_dir) as common,
                    _open_layout(common, parts[:-1], create=True) as stage,
                ):
                    os.mkdir(root.launch, PRIVATE_DIRECTORY_MODE, dir_fd=stage.fd)
                    with stage.subroot(root.launch) as made:
                        if _mount_id(made.fd, root.path) != _mount_id(stage.fd, str(stage.path)):
                            raise _refused(f"{root.path} is a mount point")
                        if os.listdir(made.fd):
                            raise _refused(
                                f"{root.path} holds entries already, so it is not the directory "
                                "just made"
                            )
                        os.fchmod(made.fd, PRIVATE_DIRECTORY_MODE)
                        if os.fstat(made.fd).st_gid != os.getegid():
                            os.fchown(made.fd, -1, os.getegid())
                        _publish_owner_record(made, record)
                        return made.identity
        except FileExistsError as exc:
            raise _refused(
                f"{root.path} exists already; a staging root is never reused, and "
                "reconciliation removes a leftover"
            ) from exc
        except SandboxLaunchRefusedError:
            raise
        except (GitLockTimeout, LockError) as exc:
            raise _refused(f"the shared-Git lock was not obtained ({exc})") from exc
        except (AutoForgeError, OSError) as exc:
            raise _refused(f"it could not be created ({type(exc).__name__}: {exc})") from exc

    def _copy_seed(
        self, staged: SafeRoot, seed: Seed, git_input: LaunchGitInput, mount: int
    ) -> str:
        """Copy the seed's two files: the manifest must hash to the input's digest and name its
        launch, the pack hash to the manifest's."""
        digest = git_input.seed_manifest_digest
        src_dir = os.open(seed.directory, _DIR_FLAGS)
        try:
            if sorted(os.listdir(src_dir)) != sorted(SEED_FILES):
                raise _refused("its seed's directory does not hold exactly the seed's two files")
            with _new_dir(staged, STAGED_SEED, mount, PRIVATE_DIRECTORY_MODE) as out:
                manifest, manifest_sha = _copy_file(src_dir, out, SEED_MANIFEST_NAME)
                if "sha256:" + manifest_sha != digest:
                    raise _refused(
                        f"the seed manifest copied hashes to sha256:{manifest_sha}, not {digest}, "
                        "which the launch's git input binds"
                    )
                pack_sha = _copy_file(src_dir, out, SEED_PACK_NAME, keep=False)[1]
        finally:
            os.close(src_dir)
        parsed = parse_manifest(manifest)
        if parsed.pack_sha256 != pack_sha:
            raise _refused("the seed pack copied is not the one its manifest names")
        for name in _IDENTITY_FIELDS:
            if getattr(parsed, name) != getattr(git_input, name):
                raise _refused(f"the seed manifest copied names another launch's {name}")
        return digest

    def _copy_read_context(
        self, staged: SafeRoot, read_context: str, mount: int
    ) -> tuple[ContextEntry, ...]:
        """A copy of the launch's read context: its directories and regular files; what it holds.

        Each entry is written through its parent directory reached again from
        the copy's root one component at a time, every directory on the way
        checked on its descriptor to be on ``mount`` (:func:`_descend`), and
        each directory is made by :func:`_new_dir`: a mount made at a copied
        directory's path at any time receives nothing.
        """
        copied: list[ContextEntry] = []
        with _new_dir(staged, STAGED_READ_CONTEXT, mount, PRIVATE_DIRECTORY_MODE) as out:
            if not read_context:
                return ()
            total = 0
            with SafeRoot.open(read_context) as source:
                for entry in source.walk(max_entries=MAX_READ_CONTEXT_ENTRIES):
                    parent, name = posixpath.split(entry.relpath)
                    if entry.is_dir:
                        with (
                            _descend(out, parent, mount) as directory,
                            _new_dir(directory, name, mount, PRIVATE_DIRECTORY_MODE),
                        ):
                            pass
                        copied.append(ContextEntry(entry.relpath, "dir"))
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
                    with _descend(out, parent, mount) as directory:
                        _write_new(directory.fd, name, data)
                    digest = hashlib.sha256(data).hexdigest()
                    copied.append(ContextEntry(entry.relpath, "file", digest))
        return tuple(sorted(copied, key=lambda item: item.path))

    # -- the hand-off ---------------------------------------------------------------
    def reprove(self, staged: StagedSource) -> None:
        """Prove ``staged`` again, immediately before a container's create binds it (D2, D12).

        Refused when a directory of its layout is a mount point, its path no
        longer names the directory created, its entries are not exactly its
        four, its owner record is not the launch's, an entry below it is on
        another mount, has another owner or other permission bits than its
        record says, its seed or read context is not the one copied, or its
        tree does not hash back to the commit's tree read afresh from the
        store.
        """
        self._check_root(staged.root)
        try:
            with (
                SafeRoot.open(self.common_dir) as common,
                self._layout(common, staged.root) as root,
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
                        f"{', '.join(sorted(STAGED_ENTRIES))}"
                    )
                problem = _owner_record_problem(
                    root.fd, owner_record(self.key, staged.owner, staged.nonce)
                )
                if problem:
                    raise _refused(f"{staged.root.path} {problem}")
                _check_contents(root, staged.ownership, _mount_id(root.fd, staged.root.path))
                with root.subroot(STAGED_SEED) as seed:
                    self._reprove_seed(seed, staged.seed_digest)
                with root.subroot(STAGED_READ_CONTEXT) as copied:
                    _check_read_context(copied, staged.read_context)
                with (
                    root.subroot(STAGED_TREE) as tree,
                    isolated_store(self.common_dir, staged.object_format, self._runner) as (
                        repo,
                        run,
                    ),
                ):
                    proved = materialize_mod.prove_tree(
                        run, str(repo), staged.commit, staged.object_format, tree
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
    def remove_staging_root(self, root: StagingRoot, owner: LaunchOwner, nonce: str) -> Removal:
        """Remove ``root`` if it is the one ``owner``'s journal entry ``nonce`` names. Idempotent.

        :attr:`Removal.REMOVED` only when it is verified gone. Reached from
        the common dir one component at a time with no mount in its layout;
        the launch's directory is removed relative to its parent's
        descriptor, never following a link, and only when it is the
        operator's, not itself a mount point, and either empty (a crash
        right after it was made), holding the owner record of exactly
        that launch and nonce, or holding only the named temporary of that
        record's interrupted publication, its bytes a prefix of exactly that
        record (:func:`_publication_leftover_problem`). A path that holds
        anything else, a directory substituted there or a partial owner
        record included, is left in place and uncertain: it is not what the
        launch made. Before anything is removed, every entry
        below it must be on its own mount; one that is not leaves it all in
        place, uncertain. The removal opens each directory without following
        a link and checks its mount before removing what is in it, and
        removes the owner record last, so a removal cut short is still
        recognized as the launch's. In either form: reconciliation may
        remove a bind-form leftover once the runtime's form changed.
        """
        self._check_root(root, staging=False)
        self._check_owner(root, owner, nonce)
        record = owner_record(self.key, owner, nonce)
        parts = split_relpath(root.layout)
        try:
            with SafeRoot.open(self.common_dir) as common:
                try:
                    stage = _open_layout(common, parts[:-1], create=False)
                except FileNotFoundError:
                    return Removal.REMOVED
                with stage:
                    return _remove_launch_dir(stage, root.launch, record)
        except (SandboxLaunchRefusedError, StateError, OSError):
            return Removal.UNCERTAIN


def _seed_launch_problem(directory: str, git_input: LaunchGitInput) -> str:
    """Why the seed manifest in ``directory`` names another launch than ``git_input``, or "".

    Read before the staging root is created, so a seed bound into another
    launch's input is refused with nothing made (PR #504 R1-F3); the copy
    is checked again as it is written.
    """
    try:
        dir_fd = os.open(directory, _DIR_FLAGS)
        try:
            fd = os.open(SEED_MANIFEST_NAME, _READ_FLAGS, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return "its seed manifest is not a regular file"
            os.set_blocking(fd, True)
            raw = bytearray()
            while chunk := os.read(fd, MAX_SEED_MANIFEST_BYTES + 1 - len(raw)):
                raw += chunk
                if len(raw) > MAX_SEED_MANIFEST_BYTES:
                    return "its seed manifest is over its bound"
        finally:
            os.close(fd)
    except OSError as exc:
        return f"its seed manifest cannot be read ({exc.strerror or exc})"
    manifest = parse_manifest(bytes(raw))
    for name in _IDENTITY_FIELDS:
        named, bound = getattr(manifest, name), getattr(git_input, name)
        if named != bound:
            return (
                f"its seed manifest names the launch's {name} {named!r}, and the launch's git "
                f"input binds {bound!r}"
            )
    return ""


def as_seed(built: BuiltSeed) -> Seed:
    """The seed step's :class:`~autoforge.sandbox_backend.Seed` of ``built``, diagnostics too."""
    return Seed(
        identity=built.digest,
        directory=str(built.directory),
        diagnostics=tuple(item.render() for item in built.selection.diagnostics),
    )


# -- mounts ------------------------------------------------------------------------------


def _mount_id(fd: int, where: str) -> int:
    """The kernel's id of the mount ``fd`` is on (``mnt_id`` of ``/proc/self/fdinfo``).

    Two directories of one filesystem bound at two places have one device
    and two mount ids, so this is what tells a bind mount from the same
    filesystem, and a file mount, from an ordinary entry. Refused when the
    host does not report it: then no mount nesting can be ruled out.
    """
    try:
        with open(f"/proc/self/fdinfo/{fd}", "rb") as handle:
            info = handle.read(_FDINFO_BYTES)
    except OSError as exc:
        raise _refused(
            f"the mount of {where} cannot be read from /proc/self/fdinfo "
            f"({exc.strerror or exc}), so a mount nested in it cannot be ruled out"
        ) from exc
    for line in info.splitlines():
        name, _, value = line.partition(b":")
        if name == b"mnt_id" and value.strip().isdigit():
            return int(value.strip())
    raise _refused(
        f"/proc/self/fdinfo names no mount for {where}, so a mount nested in it cannot be ruled out"
    )


def _mount_id_at(dir_fd: int, name: str, seen: os.stat_result, where: str) -> int:
    """The mount id of the entry ``name`` of ``dir_fd``, which must still be the one ``seen``."""
    if not _O_PATH:
        raise _refused(
            f"this host cannot open {where} without reading it (no O_PATH), so a mount at it "
            "cannot be ruled out"
        )
    fd = os.open(name, _O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        now = os.fstat(fd)
        if (now.st_dev, now.st_ino) != (seen.st_dev, seen.st_ino):
            raise _refused(f"{where} changed while it was checked")
        return _mount_id(fd, where)
    finally:
        os.close(fd)


def _open_layout(common: SafeRoot, parts: tuple[str, ...], *, create: bool) -> SafeRoot:
    """The directory ``parts`` names below ``common``, each directory on the way on its mount.

    Reached one component at a time, never through a link. Each directory
    is checked to be on the common dir's own mount before the next is
    opened, and with ``create`` before the next is created in it, so
    nothing is ever created inside a mount nested in the layout. The caller
    closes the result. ``FileNotFoundError`` when one is missing and
    ``create`` is off.
    """
    want = _mount_id(common.fd, str(common.path))
    current = common.dup()
    try:
        for part in parts:
            if create:
                current.ensure_dir(part)
            found = current.subroot(part)
            current.close()
            current = found
            if _mount_id(current.fd, str(current.path)) != want:
                raise _refused(
                    f"{current.path} is a mount point; every directory of a staging root's "
                    "layout is on the git common dir's own mount"
                )
        return current
    except BaseException:
        current.close()
        raise


def _new_directory_problem(fd: int, mount: int, where: str) -> str:
    """Why the directory just made, held open as ``fd``, must not be written, or "" (R2-F1).

    Checked on the descriptor, before the directory's mode is set or
    anything is made in it: it must be on ``mount`` (a mount made at its
    path between its ``mkdir`` and its open is another mount, whatever
    filesystem it is from) and empty, as a directory just made is (one
    renamed to its path in that instant is not the one made).
    """
    if _mount_id(fd, where) != mount:
        return f"something is mounted at {where}"
    if os.listdir(fd):
        return f"{where} holds entries already, so it is not the directory just made"
    return ""


def _new_dir(parent: SafeRoot, name: str, mount: int, mode: int) -> SafeRoot:
    """The directory ``name``, made in ``parent`` and held open, set to ``mode``.

    ``parent`` is a directory held and checked already. The directory is
    made exclusively, opened without following a link, and checked on that
    descriptor (:func:`_new_directory_problem`) before its mode is set or
    anything is made in it. The caller closes it.
    """
    os.mkdir(name, mode, dir_fd=parent.fd)
    made = parent.subroot(name)
    try:
        problem = _new_directory_problem(made.fd, mount, str(made.path))
        if problem:
            raise _refused(problem)
        os.fchmod(made.fd, mode)
    except BaseException:
        made.close()
        raise
    return made


def _descend(parent: SafeRoot, relpath: str, mount: int) -> SafeRoot:
    """The directory ``relpath`` below ``parent`` (``parent`` itself for ""), held open.

    ``parent`` is a directory held and checked already. Reached one
    component at a time, never through a link, each directory checked on
    its own descriptor to be on ``mount`` before the next is opened, so
    what the caller writes through the result lands on no other mount,
    whatever was mounted at its path since it was made. The caller closes
    it.
    """
    current = parent.dup()
    try:
        for part in split_relpath(relpath) if relpath else ():
            found = current.subroot(part)
            current.close()
            current = found
            if _mount_id(current.fd, str(current.path)) != mount:
                raise _refused(f"something is mounted at {current.path}")
        return current
    except BaseException:
        current.close()
        raise


def _tree_directory_check(tree: SafeRoot, mount: int) -> materialize_mod.DirectoryCheck:
    """The check the tree's writer runs on each directory it uses (R2-F1).

    :func:`_new_directory_problem` on the directory's descriptor, before
    the writer sets its mode or makes anything in it; a refusal is the
    reason, which the writer raises as its own error.
    """

    def check(fd: int, path: bytes) -> str:
        where = str(tree.path / os.fsdecode(path)) if path else str(tree.path)
        try:
            return _new_directory_problem(fd, mount, where)
        except SandboxLaunchRefusedError as exc:
            return str(exc)

    return check


# -- what a staging root holds ---------------------------------------------------------


def _check_contents(root: SafeRoot, ownership: SourceOwnership, mount: int) -> None:
    """Every entry of the staging root is on ``mount``, owned and with the mode ``ownership`` says.

    The root itself too. An entry on another mount is found by its mount
    id whatever it is (a directory or a file, from the same filesystem or
    not), and is not descended into.
    """
    info = os.fstat(root.fd)
    problem = _owned_problem(info, ownership) or _mode_problem(
        info, (ownership.private_directory_mode,)
    )
    if problem:
        raise _refused(f"{root.path} {problem}")
    for entry in root.walk():
        if _mount_id_at(entry.dir_fd, entry.name, entry.st, entry.relpath) != mount:
            raise _refused(f"something is mounted at {entry.relpath} below it")
        problem = _entry_problem(entry, ownership)
        if problem:
            raise _refused(f"its {entry.relpath} {problem}")


def _owned_problem(info: os.stat_result, ownership: SourceOwnership) -> str:
    if (info.st_uid, info.st_gid) != (ownership.uid, ownership.gid):
        return (
            f"is owned by {info.st_uid}:{info.st_gid}, not {ownership.uid}:{ownership.gid} "
            "as recorded"
        )
    return ""


def _mode_problem(info: os.stat_result, allowed: tuple[int, ...]) -> str:
    mode = stat.S_IMODE(info.st_mode)
    if mode not in allowed:
        wanted = " or ".join(f"{item:04o}" for item in allowed)
        return f"has mode {mode:04o}, not {wanted} as recorded"
    return ""


def _entry_problem(entry: WalkEntry, ownership: SourceOwnership) -> str:
    """Why ``entry`` is not what its place in the staging root holds, or ""."""
    problem = _owned_problem(entry.st, ownership)
    if problem:
        return problem
    top = entry.relpath.split("/", 1)[0]
    private_dir: tuple[int, ...] = (ownership.private_directory_mode,)
    private_file: tuple[int, ...] = (ownership.private_file_mode,)
    allowed: tuple[int, ...] | None
    if entry.relpath == STAGED_OWNER:
        allowed = private_file if entry.is_regular else None
    elif entry.relpath in (STAGED_SEED, STAGED_READ_CONTEXT):
        allowed = private_dir if entry.is_dir else None
    elif top == STAGED_SEED:
        allowed = private_file if entry.is_regular else None
    elif top == STAGED_READ_CONTEXT:
        allowed = private_dir if entry.is_dir else private_file if entry.is_regular else None
    elif top == STAGED_TREE:
        if entry.is_symlink:
            return ""
        if entry.is_dir:
            allowed = (ownership.tree_directory_mode,)
        elif entry.is_regular:
            allowed = (ownership.tree_file_mode, ownership.tree_executable_mode)
        else:
            allowed = None
    else:
        return "is not an entry of a staging root"
    if allowed is None:
        return "is not of the kind a staging root holds there"
    return _mode_problem(entry.st, allowed)


def _check_read_context(copied: SafeRoot, expected: tuple[ContextEntry, ...]) -> None:
    """The staged read context holds exactly ``expected``: its entries, kinds and bytes (R1-F4)."""
    found: list[ContextEntry] = []
    total = 0
    for entry in copied.walk(max_entries=MAX_READ_CONTEXT_ENTRIES):
        if entry.is_dir:
            found.append(ContextEntry(entry.relpath, "dir"))
            continue
        if not entry.is_regular:
            raise _refused(f"its read context's {entry.relpath} is neither a file nor a directory")
        total += entry.st.st_size
        if total > MAX_READ_CONTEXT_BYTES:
            raise _refused(f"its read context is over {MAX_READ_CONTEXT_BYTES} bytes")
        digest = _hash_file(entry.dir_fd, entry.name, seen=entry.st)[1]
        found.append(ContextEntry(entry.relpath, "file", digest))
    difference = _context_difference(expected, sorted(found, key=lambda item: item.path))
    if difference:
        raise _refused(f"its read context is not the one copied: {difference}")


def _context_difference(expected: tuple[ContextEntry, ...], found: list[ContextEntry]) -> str:
    want = {item.path: item for item in expected}
    have = {item.path: item for item in found}
    for path in sorted(set(want) | set(have)):
        before, now = want.get(path), have.get(path)
        if before is None:
            return f"{path} was added"
        if now is None:
            return f"{path} was removed"
        if before.kind != now.kind:
            return f"{path} was a {before.kind} and is a {now.kind} now"
        if before.sha256 != now.sha256:
            return f"{path} was changed"
    return ""


def _read_record(dir_fd: int, name: str) -> tuple[os.stat_result, bytes]:
    """The ``fstat`` of the entry ``name`` of ``dir_fd`` and, for a regular file, its bytes.

    Opened without following a link or blocking; at most one byte past
    :data:`MAX_OWNER_RECORD_BYTES` is read. Raises :class:`OSError`.
    """
    fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        data = bytearray()
        if stat.S_ISREG(info.st_mode):
            os.set_blocking(fd, True)
            while chunk := os.read(fd, MAX_OWNER_RECORD_BYTES + 1 - len(data)):
                data += chunk
                if len(data) > MAX_OWNER_RECORD_BYTES:
                    break
    finally:
        os.close(fd)
    return info, bytes(data)


def _owner_record_problem(dir_fd: int, record: bytes) -> str:
    """Why the owner record in ``dir_fd`` is not exactly ``record``, or ""."""
    try:
        info, data = _read_record(dir_fd, STAGED_OWNER)
    except FileNotFoundError:
        return "holds no owner record"
    except OSError as exc:
        return f"has an owner record that cannot be read ({exc.strerror or exc})"
    if not stat.S_ISREG(info.st_mode):
        return "has an owner record that is not a regular file"
    if data != record:
        return "has an owner record of another launch or journal entry"
    return ""


def _publication_leftover_problem(dir_fd: int, names: list[str], record: bytes) -> str:
    """Why ``names``, the entries of a launch's directory with no owner record, are not what an
    interrupted publication of ``record`` leaves, or "" (PR #504 R2-F2).

    :func:`_publish_owner_record` gives the record its name only once its
    bytes are whole and fsynced. Where the filesystem has no ``O_TMPFILE``,
    those bytes are written first into a named temporary of
    :mod:`~autoforge.safefs`'s (:func:`~autoforge.safefs.is_temporary_name`),
    and a crash before the publication leaves that temporary alone in the
    directory, holding a prefix of the record: none of it, part or all.
    That is the launch's: one entry of that name, a regular file of the
    operator's with one name, whose bytes are a prefix of exactly
    ``record``. Anything else, a second entry, another launch's or entry's
    bytes, is not, and neither is a partial owner record, which no
    publication ever leaves.
    """
    if len(names) != 1 or not is_temporary_name(names[0]):
        return "holds no owner record"
    try:
        info, data = _read_record(dir_fd, names[0])
    except OSError as exc:
        return f"holds a temporary that cannot be read ({exc.strerror or exc})"
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
        return "holds a temporary that is not its owner record's"
    if not record.startswith(data):
        return "holds a temporary that is not part of its own owner record"
    return ""


def _publish_owner_record(launch: SafeRoot, record: bytes) -> None:
    """Publish ``record`` as :data:`STAGED_OWNER` in the launch's directory, atomically (R2-F2).

    Through :meth:`~autoforge.safefs.SafeRoot.create_exclusive`: the bytes
    are written and fsynced into an inode that gets its name only once they
    are whole (Linux ``O_TMPFILE``, else a named temporary linked to the
    name), and the name is made durable. An interruption leaves no record,
    or, without ``O_TMPFILE``, the temporary (which removal recognizes,
    :func:`_publication_leftover_problem`), or the whole record; never a
    partial record under its name. Its mode is then set to exactly
    :data:`PRIVATE_FILE_MODE` whatever the umask.
    """
    launch.create_exclusive(STAGED_OWNER, record, mode=PRIVATE_FILE_MODE)
    fd = os.open(STAGED_OWNER, _READ_FLAGS, dir_fd=launch.fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _refused(f"{launch.path}/{STAGED_OWNER} is not the owner record published")
        if stat.S_IMODE(info.st_mode) != PRIVATE_FILE_MODE:
            os.fchmod(fd, PRIVATE_FILE_MODE)
    finally:
        os.close(fd)


# -- removal -------------------------------------------------------------------------------


def _remove_launch_dir(stage: SafeRoot, name: str, record: bytes) -> Removal:
    """Remove the launch's directory ``name`` of ``stage`` if it is provably the launch's."""
    try:
        seen = os.lstat(name, dir_fd=stage.fd)
    except FileNotFoundError:
        return Removal.REMOVED
    if not stat.S_ISDIR(seen.st_mode):
        return Removal.UNCERTAIN
    mount = _mount_id(stage.fd, str(stage.path))
    with stage.subroot(name) as launch:
        info = os.fstat(launch.fd)
        if (info.st_dev, info.st_ino) != (seen.st_dev, seen.st_ino):
            return Removal.UNCERTAIN
        if info.st_uid != os.geteuid():
            return Removal.UNCERTAIN
        if _mount_id(launch.fd, str(launch.path)) != mount:
            return Removal.UNCERTAIN
        names = os.listdir(launch.fd)
        if names:
            owned = STAGED_OWNER in names
            if owned and _owner_record_problem(launch.fd, record):
                return Removal.UNCERTAIN
            if not owned and _publication_leftover_problem(launch.fd, names, record):
                return Removal.UNCERTAIN
            for entry in launch.walk():
                if _mount_id_at(entry.dir_fd, entry.name, entry.st, entry.relpath) != mount:
                    return Removal.UNCERTAIN
            if not info.st_mode & stat.S_IWUSR or not info.st_mode & stat.S_IXUSR:
                os.fchmod(launch.fd, PRIVATE_DIRECTORY_MODE)
            _remove_below(launch.fd, mount, keep=STAGED_OWNER)
            if owned:
                os.unlink(STAGED_OWNER, dir_fd=launch.fd)
    os.rmdir(name, dir_fd=stage.fd)
    try:
        os.lstat(name, dir_fd=stage.fd)
    except FileNotFoundError:
        return Removal.REMOVED
    return Removal.UNCERTAIN


def _remove_below(root_fd: int, mount: int, *, keep: str) -> None:
    """Remove every entry below ``root_fd`` but ``keep`` at its top, never crossing a mount.

    Depth first, by descriptors, iteratively: each directory is opened
    without following a link, must still be the entry listed, and must be
    on ``mount`` before anything in it is listed or removed; a directory
    that is not stops the removal (raises), leaving what is in it. An
    entry that is not a directory is unlinked, never followed.
    """
    frames: list[tuple[int, list[str], str]] = [
        (os.dup(root_fd), [item for item in os.listdir(root_fd) if item != keep], "")
    ]
    try:
        while frames:
            fd, pending, name = frames[-1]
            if pending:
                child = pending.pop()
                seen = os.lstat(child, dir_fd=fd)
                if not stat.S_ISDIR(seen.st_mode):
                    os.unlink(child, dir_fd=fd)
                    continue
                sub = os.open(child, _DIR_FLAGS, dir_fd=fd)
                try:
                    info = os.fstat(sub)
                    if (info.st_dev, info.st_ino) != (seen.st_dev, seen.st_ino):
                        raise _refused(f"{child} changed while it was removed")
                    if _mount_id(sub, child) != mount:
                        raise _refused(f"something is mounted at {child}; it is not removed")
                    if (info.st_mode & _OWNER_RWX) != _OWNER_RWX:
                        os.fchmod(sub, PRIVATE_DIRECTORY_MODE)
                    children = os.listdir(sub)
                except BaseException:
                    os.close(sub)
                    raise
                frames.append((sub, children, child))
                continue
            frames.pop()
            os.close(fd)
            if frames:
                os.rmdir(name, dir_fd=frames[-1][0])
    finally:
        for fd, _, _ in frames:
            os.close(fd)


# -- files ---------------------------------------------------------------------------------


def _write_new(dir_fd: int, name: str, data: bytes) -> None:
    """Create ``name`` in ``dir_fd`` exclusively, holding ``data``, mode ``0600``."""
    fd = os.open(name, _NEW_FILE_FLAGS, PRIVATE_FILE_MODE, dir_fd=dir_fd)
    try:
        os.fchmod(fd, PRIVATE_FILE_MODE)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_file(src_dir: int, out: SafeRoot, name: str, *, keep: bool = True) -> tuple[bytes, str]:
    """Copy ``name`` into ``out`` exclusively, hashing it; its bytes too when ``keep``."""
    src = os.open(name, _READ_FLAGS, dir_fd=src_dir)
    try:
        if not stat.S_ISREG(os.fstat(src).st_mode):
            raise _refused(f"its seed's {name} is not a regular file")
        os.set_blocking(src, True)
        dst = os.open(name, _NEW_FILE_FLAGS, PRIVATE_FILE_MODE, dir_fd=out.fd)
        digest = hashlib.sha256()
        kept = bytearray()
        try:
            os.fchmod(dst, PRIVATE_FILE_MODE)
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


def _hash_file(
    dir_fd: int, name: str, *, keep: bool = False, seen: os.stat_result | None = None
) -> tuple[bytes, str]:
    """SHA-256 of the regular file ``name`` of ``dir_fd`` (its bytes too when ``keep``).

    With ``seen``, the file opened must still be that entry.
    """
    fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _refused(f"its {name} is not a regular file")
        if seen is not None and (info.st_dev, info.st_ino) != (seen.st_dev, seen.st_ino):
            raise _refused(f"its {name} changed while it was proved")
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
