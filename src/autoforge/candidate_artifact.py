"""An exported candidate artifact, and its strict, bounded import (ADR 0013 D4, D5, D6; #269).

A container launch leaves its work only as an *artifact*: one pack and a
manifest, written by an exporter into the launch's export directory (D5).
Everything in it is untrusted data. This module says what an artifact is,
what bounds it, and how the controller turns one into a candidate in the
shared object store, or refuses it. It generalizes the strict import of
ADR 0005 D6.5 (:func:`autoforge.agent_worktree.import_private_objects`):
only the input changes, from a host directory the controller packs itself
to a pack someone else wrote.

**The artifact** is a directory holding exactly :data:`ARTIFACT_MANIFEST`
and :data:`ARTIFACT_PACK`, both regular files. The manifest
(:class:`CandidateManifest`, :data:`MANIFEST_SCHEMA`) names the launch it
claims to come from (run key, issue, phase, launch id), the repository and
its object format, the seed commit, its lower boundary, the carried commit,
the seed manifest's digest, the claimed candidate, and the pack's byte
size, object count and SHA-256. Every one of those is a claim; the expected
values come from the launch's git input (:mod:`autoforge.git_input`), never
from the artifact.

**The export bound** (D6 item 4): an exporter ends by
:data:`EXPORT_DEADLINE_SECONDS`, and an artifact's pack holds at most
:data:`MAX_ARTIFACT_PACK_BYTES` bytes and :data:`MAX_ARTIFACT_OBJECTS`
objects. An artifact over either bound is a candidate refusal.

**The import** (:func:`import_artifact`), in this order, each failure an
:class:`~autoforge.agent_worktree.ObjectImportError` (a candidate refusal,
never retried, nothing published):

1. The directory and both files are opened without following a link; an
   entry that is not one of the two, a link, or anything but a regular file
   is refused before anything is read.
2. The manifest's size is checked against :data:`MAX_MANIFEST_BYTES` before
   it is read, then it is parsed strictly (exact keys, exact types) and
   compared with the launch's git input (:func:`manifest_disagreement`).
3. The pack's size, from its descriptor, is checked against the bound and
   the manifest before a byte is read; then its 12-byte header (``PACK``,
   version 2 or 3, the object count) against the count bound and the
   manifest.
4. The pack is copied in bounded chunks into a controller-owned quarantine
   (``mkdtemp``, mode 0700, outside every repository), hashed as it is
   copied; a pack that grows past its size, or whose hash is not the
   manifest's, is refused. It is never held in memory whole.
5. ``index-pack --strict`` indexes the quarantined copy in the quarantine,
   with the shared store as the repository its links resolve against: every
   object is checked by fsck and every link must resolve in the pack or the
   store. Nothing is written to the shared store.
6. With the quarantine as an alternate of the shared store: the claimed
   candidate is a commit and descends from the seed; every commit of the
   pack descends from the seed and is an ancestor of the candidate; every
   object of the pack is reachable from the candidate and not from the
   seed. A pack carrying anything else is refused. And the closure: every
   object the candidate reaches above the seed (and above the carried
   commit of a relaunch), commits and their parents included, is in the
   pack. Step 5 lets a link resolve in the shared store, which holds
   objects no launch was given (another branch's blob, another run's
   commit, an unrelated merge parent); an artifact that names one without
   carrying it is refused, so the candidate never exposes an object the
   container was not given.
7. Publication: the objects the shared store lacks are published by
   ``index-pack --stdin --strict`` of the quarantined copy, streamed from
   the file, under the shared-Git lock (L1, ADR 0008 D12.1) with automatic
   maintenance off (D12.3). It is the only step that writes the shared
   store. When the store already holds every object (a re-import after a
   crash past publication) nothing is written.
8. The candidate is read back from the shared store alone and re-hashed
   (ADR 0004 D7.5, through the caller's ``read_commit``).

**Re-import is idempotent.** Every step reads the artifact and the store
again and decides from them alone, so an import that ended at any point (a
crash before L1, during ``index-pack``, after publication, before the
caller saved its outcome) gives, when run again on the same artifact, the
same candidate or the same typed refusal. ``index-pack`` writes its pack
under a temporary name and makes it visible only by renaming its index
into place, so an interrupted publication leaves no object visible, only a
``tmp_pack_*`` file that git ignores and ``gc`` removes.

**What the host never does** (D5): it never runs git with the container's
repository as its repository, never reads that repository's configuration
or hooks, never applies a diff and never copies working-tree files back.
Every git process is the controller's hardened local request
(:func:`~autoforge.git_transport.local_git_request`, ADR 0004 D7.3).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import struct
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from .agent_worktree import IMPORT_TIMEOUT_SECONDS, ObjectImportError, alternate_entry
from .errors import GitLockTimeout, GitTransportError, LockError
from .executor import ExecutionRequest, ExecutionResult, execute
from .git_input import LaunchGitInput, valid_repository
from .git_transport import NO_AUTO_MAINTENANCE, local_git_request
from .locking import SharedGitLock
from .redaction import redact

Runner = Callable[[ExecutionRequest], ExecutionResult]
# A commit read from the shared store as its own bytes (ADR 0004 D7.5):
# ``None`` when the store does not hold it; a mismatch raises.
ReadCommit = Callable[[str], object | None]

# The artifact's two files, and nothing else.
ARTIFACT_MANIFEST = "manifest.json"
ARTIFACT_PACK = "candidate.pack"
MANIFEST_SCHEMA = 1
MAX_MANIFEST_BYTES = 64 * 1024

# The export bound (ADR 0013 D6 item 4): the exporter's fixed deadline, the
# timeout of the controller's call that runs it, and the bounds of what it
# leaves. One issue's new commits are far below these; a pack over them is
# refused before a byte of it is read, and an exporter that writes past the
# size ends the export (#271 enforces that while the stream is read).
EXPORT_DEADLINE_SECONDS = 600
MAX_ARTIFACT_PACK_BYTES = 512 * 1024 * 1024
MAX_ARTIFACT_OBJECTS = 100_000

# The copy into the quarantine reads at most this much at a time.
_CHUNK_BYTES = 1024 * 1024
_PACK_SIGNATURE = b"PACK"
_PACK_VERSIONS = (2, 3)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_OID = {"sha1": re.compile(r"[0-9a-f]{40}"), "sha256": re.compile(r"[0-9a-f]{64}")}
_OBJECT_TYPES = frozenset({"commit", "tree", "blob", "tag"})


@dataclass(frozen=True)
class ArtifactLimits:
    """The bounds an artifact is checked against (the export bound, by default)."""

    max_pack_bytes: int = MAX_ARTIFACT_PACK_BYTES
    max_objects: int = MAX_ARTIFACT_OBJECTS
    max_manifest_bytes: int = MAX_MANIFEST_BYTES


EXPORT_BOUND = ArtifactLimits()


@dataclass(frozen=True)
class CandidateManifest:
    """What an artifact's manifest claims (schema :data:`MANIFEST_SCHEMA`): untrusted.

    The first twelve fields are the launch's git input as the exporter was
    told it; ``candidate`` is the claimed ``HEAD``; ``pack_bytes``,
    ``pack_objects`` and ``pack_sha256`` describe the pack beside it.
    """

    phase: str
    run: str
    issue: int
    launch: str
    repository: str
    object_format: str
    seed: str
    lower: str
    carried: str
    seed_manifest_digest: str
    export_command: bool
    entry_label: str
    candidate: str
    pack_bytes: int
    pack_objects: int
    pack_sha256: str

    @classmethod
    def for_input(
        cls,
        git_input: LaunchGitInput,
        *,
        candidate: str,
        pack_bytes: int,
        pack_objects: int,
        pack_sha256: str,
    ) -> CandidateManifest:
        """The manifest an exporter writes for ``git_input`` (for #271's exporter, and tests)."""
        return cls(
            **git_input.identity(),  # type: ignore[arg-type]
            candidate=candidate,
            pack_bytes=pack_bytes,
            pack_objects=pack_objects,
            pack_sha256=pack_sha256,
        )

    def render(self) -> bytes:
        """The manifest's bytes: canonical JSON, with its schema."""
        data = {"schema": MANIFEST_SCHEMA, **{key: getattr(self, key) for key in _TYPES}}
        return (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


_TYPES: dict[str, type] = {
    "phase": str,
    "run": str,
    "issue": int,
    "launch": str,
    "repository": str,
    "object_format": str,
    "seed": str,
    "lower": str,
    "carried": str,
    "seed_manifest_digest": str,
    "export_command": bool,
    "entry_label": str,
    "candidate": str,
    "pack_bytes": int,
    "pack_objects": int,
    "pack_sha256": str,
}


def _refused(text: str) -> ObjectImportError:
    return ObjectImportError(
        f"the launch's exported artifact is refused: {text}; nothing was published"
    )


def parse_manifest(raw: bytes) -> CandidateManifest:
    """``raw`` as a manifest, strictly: exact keys, exact types, well-formed values."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _refused(f"its manifest does not parse ({type(exc).__name__})") from exc
    if not isinstance(data, dict):
        raise _refused("its manifest is not a JSON object")
    if data.get("schema") != MANIFEST_SCHEMA or type(data.get("schema")) is not int:
        raise _refused(f"its manifest is not of schema {MANIFEST_SCHEMA}")
    keys = set(data) - {"schema"}
    if keys != set(_TYPES):
        extra = sorted(keys - set(_TYPES))
        missing = sorted(set(_TYPES) - keys)
        raise _refused(
            "its manifest does not hold exactly its fields"
            + (f" (unknown: {redact(', '.join(extra))[:200]})" if extra else "")
            + (f" (missing: {', '.join(missing)})" if missing else "")
        )
    for key, kind in _TYPES.items():
        if type(data[key]) is not kind:
            raise _refused(f"its manifest's {key} is not a {kind.__name__}")
    manifest = CandidateManifest(**{key: data[key] for key in _TYPES})
    pattern = _OID.get(manifest.object_format)
    if pattern is None:
        raise _refused("its manifest names an unknown object format")
    if not pattern.fullmatch(manifest.candidate):
        raise _refused("its manifest's candidate is not a full object id")
    if manifest.pack_bytes < 0 or manifest.pack_objects < 0:
        raise _refused("its manifest's pack size or object count is negative")
    if not _SHA256_RE.fullmatch(manifest.pack_sha256):
        raise _refused("its manifest's pack_sha256 is not a SHA-256")
    if not valid_repository(manifest.repository):
        raise _refused("its manifest's repository is not an owner/name")
    return manifest


def manifest_disagreement(manifest: CandidateManifest, git_input: LaunchGitInput) -> str:
    """Where ``manifest`` disagrees with the launch's git input, or "" (D5).

    Each expected value is the input's, which the controller bound before
    the launch; the manifest is compared with it field by field.
    """
    expected = git_input.identity()
    for key, value in expected.items():
        claimed = getattr(manifest, key)
        if claimed != value:
            shown = claimed if isinstance(claimed, int) else redact(str(claimed))[:80]
            return f"its manifest names {key} {shown!r}, and the launch's git input binds {value!r}"
    return ""


@dataclass(frozen=True)
class ImportedCandidate:
    """What :func:`import_artifact` accepted: the candidate, and the pack it came in.

    ``published`` is whether this import wrote the shared store; a re-import
    of an artifact whose objects the store already holds writes nothing.
    """

    candidate: str
    pack_sha256: str
    objects: int
    published: bool


# -- reading the artifact -----------------------------------------------------------


def _open_entry(dir_fd: int, name: str) -> tuple[int, os.stat_result]:
    """One file of the artifact, opened without following a link; a regular file only."""
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd
        )
    except OSError as exc:
        raise _refused(
            f"its {name} cannot be opened without following a link ({exc.strerror})"
        ) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _refused(f"its {name} is not a regular file")
        os.set_blocking(fd, True)
    except BaseException:
        os.close(fd)
        raise
    return fd, st


def _read_bounded(fd: int, size: int, limit: int, name: str) -> bytes:
    """Exactly ``size`` bytes of ``fd``, never more than ``limit``; a file that grew is refused."""
    if size > limit:
        raise _refused(f"its {name} holds {size} bytes, over the bound of {limit}")
    data = b""
    while len(data) <= size:
        chunk = os.read(fd, min(_CHUNK_BYTES, size + 1 - len(data)))
        if not chunk:
            break
        data += chunk
    if len(data) != size:
        raise _refused(f"its {name} changed size while it was read")
    return data


def _open_artifact(directory: Path) -> int:
    """The artifact's directory, opened without following a link, holding exactly its files."""
    try:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise _refused(
            f"its directory {directory} cannot be opened as a directory without following a "
            f"link ({exc.strerror})"
        ) from exc
    try:
        names = sorted(os.listdir(dir_fd))
    except OSError as exc:
        os.close(dir_fd)
        raise _refused(f"its directory {directory} cannot be listed ({exc.strerror})") from exc
    expected = sorted((ARTIFACT_MANIFEST, ARTIFACT_PACK))
    if names != expected:
        os.close(dir_fd)
        extra = [name for name in names if name not in expected]
        missing = [name for name in expected if name not in names]
        what = []
        if extra:
            what.append(f"holds {redact(extra[0])[:80]!r}, which an artifact does not")
        if missing:
            what.append(f"lacks {', '.join(missing)}")
        raise _refused(f"its directory {directory} " + " and ".join(what))
    return dir_fd


def _pack_header(fd: int, size: int, limits: ArtifactLimits, manifest: CandidateManifest) -> None:
    """The pack's size and header against the bounds and the manifest, before the copy."""
    if size > limits.max_pack_bytes:
        raise _refused(
            f"its pack holds {size} bytes, over the export bound of {limits.max_pack_bytes}"
        )
    if size != manifest.pack_bytes:
        raise _refused(f"its pack holds {size} bytes, and its manifest says {manifest.pack_bytes}")
    header = os.pread(fd, 12, 0)
    if len(header) != 12:
        raise _refused("its pack is shorter than a pack header")
    signature, version, count = struct.unpack(">4sII", header)
    if signature != _PACK_SIGNATURE or version not in _PACK_VERSIONS:
        raise _refused("its pack does not start with a pack header")
    if count > limits.max_objects:
        raise _refused(
            f"its pack holds {count} objects, over the export bound of {limits.max_objects}"
        )
    if count != manifest.pack_objects:
        raise _refused(
            f"its pack holds {count} objects, and its manifest says {manifest.pack_objects}"
        )


def _quarantine(object_format: str) -> Path:
    """A controller-owned minimal git directory (mode 0700) the pack is indexed in.

    Written here, not by ``git init``, so no template or hook is copied in:
    ``HEAD``, an empty ``refs/``, ``objects/pack`` and a ``config`` that
    turns gc and maintenance off.
    """
    root = Path(tempfile.mkdtemp(prefix="autoforge-artifact-"))
    (root / "refs").mkdir()
    (root / "objects" / "pack").mkdir(parents=True)
    (root / "HEAD").write_text("ref: refs/heads/autoforge-unborn\n", encoding="ascii")
    config = [
        "[core]",
        f"\trepositoryformatversion = {1 if object_format == 'sha256' else 0}",
        "\tbare = true",
        "\tcommitGraph = false",
        "[gc]",
        "\tauto = 0",
        "[maintenance]",
        "\tauto = false",
    ]
    if object_format == "sha256":
        config += ["[extensions]", "\tobjectFormat = sha256"]
    (root / "config").write_text("\n".join(config) + "\n", encoding="ascii")
    return root


def _copy_pack(fd: int, size: int, target: Path, expected_sha256: str) -> None:
    """Copy ``size`` bytes of ``fd`` to ``target`` in bounded chunks, hashing them."""
    digest = hashlib.sha256()
    copied = 0
    out = os.open(
        target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    try:
        while True:
            chunk = os.read(fd, _CHUNK_BYTES)
            if not chunk:
                break
            copied += len(chunk)
            if copied > size:
                raise _refused(f"its pack grew past the {size} bytes it held while it was copied")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                view = view[os.write(out, view) :]
        os.fsync(out)
    finally:
        os.close(out)
    if copied != size:
        raise _refused(f"its pack shrank to {copied} bytes while it was copied")
    if digest.hexdigest() != expected_sha256:
        raise _refused("its pack's SHA-256 is not the one its manifest names")


# -- the import --------------------------------------------------------------------------


def _ended(res: ExecutionResult) -> str:
    return "timed out" if res.timed_out else f"exited {res.exit_code}"


def _detail(res: ExecutionResult) -> str:
    lines = (res.stderr or res.stdout or "").strip().splitlines()
    return f": {redact(lines[-1])[:200]}" if lines else ""


def bound_revision_problem(
    common: Path, git_input: LaunchGitInput, runner: Runner | None = None
) -> str:
    """Why the shared store cannot seed ``git_input``, or "" (D4: before any journal entry).

    The seed (and the carried commit) must be commits the shared store
    holds; for an agent launch the lower boundary must be one too, and an
    ancestor of the seed, and the carried commit must descend from the
    seed. Read with the controller's hardened local git and no alternate:
    nothing a container wrote is consulted. A missing revision is refused
    here; a later ``resume`` fetches again.
    """
    run = runner or execute

    def git(args: list[str]) -> ExecutionResult:
        res = run(local_git_request(["--git-dir", str(common), *args]))
        if res.timed_out or res.truncated:
            raise ObjectImportError(f"reading the shared store {_ended(res)}{_detail(res)}")
        return res

    def is_commit(sha: str) -> bool:
        res = git(["rev-parse", "--verify", "-q", f"{sha}^{{commit}}"])
        return res.exit_code == 0 and res.stdout.strip() == sha

    def is_ancestor(ancestor: str, descendant: str) -> bool:
        res = git(["merge-base", "--is-ancestor", ancestor, descendant])
        if res.exit_code not in (0, 1):
            raise ObjectImportError(f"checking ancestry {_ended(res)}{_detail(res)}")
        return res.exit_code == 0

    try:
        if not is_commit(git_input.seed):
            return f"the bound seed {git_input.seed} is not a commit the shared store holds"
        if not git_input.export_command:
            if not is_commit(git_input.lower):
                return (
                    f"the bound lower boundary {git_input.lower} is not a commit the shared "
                    "store holds"
                )
            if not is_ancestor(git_input.lower, git_input.seed):
                return (
                    f"the bound lower boundary {git_input.lower} is not an ancestor of the "
                    f"seed {git_input.seed} in the shared store"
                )
        if git_input.carried:
            if not is_commit(git_input.carried):
                return (
                    f"the carried commit {git_input.carried} is not a commit the shared store holds"
                )
            if not is_ancestor(git_input.seed, git_input.carried):
                return (
                    f"the carried commit {git_input.carried} does not descend from the seed "
                    f"{git_input.seed}"
                )
    except ObjectImportError as exc:
        return f"the shared store could not be read: {exc}"
    return ""


def import_artifact(
    common: Path,
    directory: Path,
    git_input: LaunchGitInput,
    runner: Runner | None,
    *,
    read_commit: ReadCommit,
    git_lock: Callable[[str], AbstractContextManager[object]] | None = None,
    limits: ArtifactLimits = EXPORT_BOUND,
) -> ImportedCandidate:
    """Import the artifact in ``directory`` strictly into the shared store of ``common``.

    The steps and their order are the module's. Returns the candidate the
    artifact carried; any refusal is an
    :class:`~autoforge.agent_worktree.ObjectImportError`, after which the
    shared store holds nothing the artifact brought. ``read_commit`` reads
    a commit from the shared store as its own bytes (ADR 0004 D7.5; the
    engine's git transport). ``git_lock(operation)`` is the shared-Git lock
    (L1) around the one write, by default :class:`SharedGitLock` on
    ``common``; a lock not obtained within its bound refuses the import.
    """
    run = runner or execute
    dir_fd = _open_artifact(directory)
    try:
        manifest_fd, manifest_st = _open_entry(dir_fd, ARTIFACT_MANIFEST)
        try:
            raw = _read_bounded(
                manifest_fd, manifest_st.st_size, limits.max_manifest_bytes, "manifest"
            )
        finally:
            os.close(manifest_fd)
        manifest = parse_manifest(raw)
        disagreement = manifest_disagreement(manifest, git_input)
        if disagreement:
            raise _refused(disagreement)
        pack_fd, pack_st = _open_entry(dir_fd, ARTIFACT_PACK)
    finally:
        os.close(dir_fd)
    try:
        _pack_header(pack_fd, pack_st.st_size, limits, manifest)
        quarantine = _quarantine(git_input.object_format)
        try:
            pack = quarantine / "objects" / "pack" / "incoming.pack"
            try:
                _copy_pack(pack_fd, pack_st.st_size, pack, manifest.pack_sha256)
            finally:
                os.close(pack_fd)
                pack_fd = -1
            return _import_quarantined(
                common, quarantine, pack, git_input, manifest, run, read_commit, git_lock
            )
        finally:
            shutil.rmtree(quarantine, ignore_errors=True)
    finally:
        if pack_fd >= 0:
            os.close(pack_fd)


def _import_quarantined(
    common: Path,
    quarantine: Path,
    pack: Path,
    git_input: LaunchGitInput,
    manifest: CandidateManifest,
    run: Runner,
    read_commit: ReadCommit,
    git_lock: Callable[[str], AbstractContextManager[object]] | None,
) -> ImportedCandidate:
    """Steps 5 to 8 of the import, on the quarantined copy of the pack."""
    objects_dir = quarantine / "objects"
    alternate = {"GIT_ALTERNATE_OBJECT_DIRECTORIES": alternate_entry(objects_dir)}

    def git(
        args: list[str],
        what: str,
        *,
        git_dir: Path = common,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        stdin_path: str | None = None,
        allow: tuple[int, ...] = (0,),
    ) -> ExecutionResult:
        request = local_git_request(
            ["--git-dir", str(git_dir), *args], env=env, timeout_seconds=IMPORT_TIMEOUT_SECONDS
        )
        request.stdin_data = stdin
        request.stdin_path = stdin_path
        res = run(request)
        if res.timed_out or res.truncated or res.exit_code not in allow:
            raise _refused(f"{what} {_ended(res)}{_detail(res)}")
        return res

    _index_in_quarantine(git, pack)
    listed = git(
        ["cat-file", "--batch-all-objects", "--batch-check=%(objecttype) %(objectname)"],
        "listing its pack's objects",
        git_dir=quarantine,
    )
    in_pack: dict[str, str] = {}
    for line in listed.stdout.splitlines():
        kind, _, oid = line.partition(" ")
        if kind not in _OBJECT_TYPES or not _OID[git_input.object_format].fullmatch(oid):
            raise _refused("its pack lists an object git does not name")
        in_pack[oid] = kind
    if len(in_pack) != manifest.pack_objects:
        raise _refused(
            f"its pack indexes {len(in_pack)} distinct objects, and its manifest says "
            f"{manifest.pack_objects}"
        )
    candidate = manifest.candidate
    found = git(
        ["rev-parse", "--verify", "-q", f"{candidate}^{{commit}}"],
        "reading its claimed HEAD",
        env=alternate,
        allow=(0, 1),
    )
    if found.exit_code != 0 or found.stdout.strip() != candidate:
        raise _refused(f"its claimed HEAD {candidate} is not a commit")
    if candidate != git_input.seed:
        descends = git(
            ["merge-base", "--is-ancestor", git_input.seed, candidate],
            "checking that its HEAD descends from the seed",
            env=alternate,
            allow=(0, 1),
        )
        if descends.exit_code != 0:
            raise _refused(
                f"its claimed HEAD {candidate} does not descend from the seed {git_input.seed}"
            )
    pack_commits = {oid for oid, kind in in_pack.items() if kind == "commit"}
    if pack_commits:
        path = git(
            ["rev-list", "--ancestry-path", candidate, "--not", git_input.seed],
            "listing the commits between the seed and its HEAD",
            env=alternate,
        )
        between = set(path.stdout.split())
        stray = sorted(pack_commits - between)
        if stray:
            raise _refused(
                f"its pack holds commit {stray[0]}, which is not between the seed "
                f"{git_input.seed} and its HEAD {candidate}"
            )
    above_seed = _reached(git, candidate, [git_input.seed], alternate)
    stray = sorted(set(in_pack) - above_seed)
    if stray:
        raise _refused(
            f"its pack holds {in_pack[stray[0]]} {stray[0]}, which its HEAD does not "
            "reach above the seed"
        )
    _check_closure(git, candidate, git_input, in_pack, above_seed, alternate)
    missing = _missing(git, list(in_pack) or [candidate])
    published = False
    if missing and in_pack:
        _publish(git, pack, git_lock or (lambda op: SharedGitLock(common, operation=op)))
        published = True
        missing = _missing(git, list(in_pack))
    if missing or _missing(git, [candidate]):
        raise _refused(f"its HEAD {candidate} is not in the shared object store after the import")
    try:
        record = read_commit(candidate)
    except GitTransportError as exc:
        raise _refused(f"its HEAD {candidate} does not read as its own bytes ({exc})") from exc
    if record is None:
        raise _refused(f"its HEAD {candidate} is not in the shared object store")
    return ImportedCandidate(
        candidate=candidate,
        pack_sha256=manifest.pack_sha256,
        objects=len(in_pack),
        published=published,
    )


def _reached(
    git: Callable[..., ExecutionResult],
    candidate: str,
    boundary: list[str],
    alternate: dict[str, str],
) -> set[str]:
    """Every object ``candidate`` reaches above ``boundary``: ``rev-list --objects``.

    The same walk an exporter's ``pack-objects --revs`` makes: the commits
    of ``candidate`` that no boundary commit reaches, and every object their
    trees name, except what the trees of the boundary commits they stand on
    already hold. Read with the quarantine as an alternate, so a pack's
    objects are found; a walk whose output is over the executor's bound is
    refused, never read in part.
    """
    reach = git(
        ["rev-list", "--objects", candidate, "--not", *boundary],
        "listing the objects its HEAD reaches",
        env=alternate,
    )
    return {line.split(" ", 1)[0] for line in reach.stdout.splitlines() if line}


def _check_closure(
    git: Callable[..., ExecutionResult],
    candidate: str,
    git_input: LaunchGitInput,
    in_pack: dict[str, str],
    above_seed: set[str],
    alternate: dict[str, str],
) -> None:
    """The candidate's closure is the authorized revisions plus the pack, and nothing else.

    What the container was given is the seed (and the carried commit of a
    relaunch); what it gives back is the pack. So every object the
    candidate reaches above those, commits and their parents included, must
    be in the pack. ``index-pack --strict`` lets a link resolve in the
    shared store, which also holds objects no launch was given (another
    branch's blob, another run's commit, a merge parent the seed never
    reached): such an object never completes a candidate, so an artifact
    that names one without carrying it is refused, and nothing is
    published. A candidate with an empty pack is therefore only the seed
    itself, or the carried commit.
    """
    needed = above_seed
    if git_input.carried:
        needed = _reached(git, candidate, [git_input.seed, git_input.carried], alternate)
    outside = sorted(needed - set(in_pack))
    if outside:
        given = "the seed and the carried commit" if git_input.carried else "the seed"
        raise _refused(
            f"its HEAD {candidate} reaches object {outside[0]}, which neither its pack carries "
            f"nor {given} reaches: an object the shared store holds for another reason never "
            "completes a candidate"
        )


def _index_in_quarantine(git: Callable[..., ExecutionResult], pack: Path) -> None:
    """``index-pack --strict`` of the quarantined pack, into the quarantine alone.

    The shared store is the repository its links resolve against, read
    only: the index (and its reverse index) are written beside the pack,
    in the controller's quarantine, and nothing is written to the common
    dir, so no lock is taken.
    """
    git(
        ["index-pack", "--strict", "-o", str(pack.with_suffix(".idx")), str(pack)],
        "indexing its pack strictly",
    )


def _publish(
    git: Callable[..., ExecutionResult],
    pack: Path,
    lock: Callable[[str], AbstractContextManager[object]],
) -> None:
    """The import's one write: the checked pack into the shared store, under L1.

    ``index-pack --stdin --strict``, streamed from the quarantined file and
    never read into memory, with automatic maintenance off (ADR 0008 D12.1,
    D12.3). A lock not obtained within its bound refuses the import.
    """
    try:
        with lock("import an exported candidate into the shared store"):
            git(
                [*NO_AUTO_MAINTENANCE, "index-pack", "--stdin", "--strict"],
                "publishing its pack strictly into the shared store",
                stdin_path=str(pack),
            )
    except (GitLockTimeout, LockError) as exc:
        raise _refused(f"the shared-Git lock was not obtained ({exc})") from exc


def _missing(git: Callable[..., ExecutionResult], ids: list[str]) -> set[str]:
    """Which of ``ids`` the shared store alone (no quarantine) does not hold."""
    res = git(
        ["cat-file", "--batch-check=%(objectname)"],
        "checking which objects the shared store holds",
        stdin=("\n".join(ids) + "\n").encode("ascii"),
    )
    present = {line.strip() for line in res.stdout.splitlines() if not line.endswith(" missing")}
    return set(ids) - present
