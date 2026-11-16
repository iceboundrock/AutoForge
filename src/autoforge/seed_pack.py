"""A launch's seed: the pack and manifest its container's repository starts from (#292).

ADR 0013 D4 and D11. A container launch gets the history its repository
needs as one pack and a manifest, built on the host from the shared object
store and checked there before anything of the launch exists (the *seed
step*, ADR 0013 D13). This module builds and checks them. It never
decides the revision: the launch's git input
(:class:`~autoforge.git_input.LaunchGitInput`, #269) names the seed commit,
its lower boundary and the carried commit, and the seed is built from those
alone, never from a revision read again here.

**What the seed holds** (D4, D11), :func:`select_seed`, read from the
shared store:

- the seed commit and its lower boundary; for a relaunch also the carried
  commit; and every commit reachable from the seed (and the carried
  commit) and not from the lower boundary: for ``REVIEW`` and ``FIX`` the
  PR's commits above the bound merge base, for the other phases the
  carried commit's commits above the base;
- as shallow boundaries, the lower boundary and each parent of one of those
  commits that lies outside that range (a merge of the base branch into a
  PR branch adds such parents), each with its tree and blobs, so every
  commit's parent tree is present and ``git diff`` from the merge base to
  the HEAD is the PR's;
- with ``sandbox.container.history_depth`` at ``n``, also every commit up to
  ``n`` parents below a boundary, and with ``full`` every ancestor; the
  boundaries are then the included commits with a parent left out;
- with ``sandbox.container.history_tags`` on, the store's ``refs/tags/``
  whose chain of tag objects peels to a commit the seed holds, each with
  every tag object on its chain; a tag that peels to a tree, a blob or a
  commit outside the seed is left out. Off, the default, no tag;
- never another branch, a remote, a reflog, a stash, notes or replacement
  refs: a pack holds objects, and the manifest names no ref but the tags;
- for an export command, the commit it validates and its tree, no history.

**The pack** is ``pack-objects`` of exactly the objects the walk lists, in
a controller-owned git directory whose only object store is the shared
store, as an alternate, and whose ``shallow`` file names the boundaries:
nothing is checked out, so no attribute, filter driver, LFS smudge or hook
of the repository applies (D2). **The manifest** (:class:`SeedManifest`)
names the launch's identity, the repository and its object format, the
seed commit, its lower boundary, every shallow boundary, the carried
commit, the selected tags with their object ids, the history settings, and
the pack's size, object count and SHA-256. Its digest is what the launch's
git input binds (D4), so it binds the pack's bytes too.

**The seed's own checks** (D4, :func:`check_seed`), before it is handed on
and before any journal entry, each a refusal
(:class:`~autoforge.errors.SandboxLaunchRefusedError`, a setup failure,
D17): the directory holds exactly the manifest and the pack, regular files;
the manifest parses strictly and agrees with the git input, the settings
and the selection; the pack is within its bounds (``seed_max_bytes``,
``seed_max_objects``; a refusal names them, and a history setting that made
the seed larger), its size, header and SHA-256 are the manifest's; then
``index-pack --strict`` indexes it into a controller-owned quarantine whose
``shallow`` file names the manifest's boundaries, never into the shared
store, so a corrupt object or a missing tree, blob, parent or tag target
fails there; in the quarantine the pack holds the seed commit and the
carried commit, exactly the commits the selection names, exactly the tag
objects the selected tags reach, each tag peels to a commit of the pack,
and the closure above the boundaries is every object the pack holds.

**What staging supports** (D2's table), as :class:`StagingDiagnostic`: a
submodule is an empty directory at its gitlink; Git LFS is unsupported for
content (the pointer files, byte for byte); a filter, ``ident``, ``eol``,
``text`` or ``working-tree-encoding`` attribute is not applied (each file
is its blob's bytes); a sparse checkout of the operator's checkout is not
applied (the whole tree); a partial clone's shared store is read with no
lazy fetch, so a missing object refuses the seed rather than reaching the
promisor remote.

Every git process is the controller's hardened local request
(:func:`~autoforge.git_transport.local_git_request`). Each one that reads
an object of the shared store runs in a controller-owned git directory
that reads the store as its alternate and no configuration of the
operator's or the store's (:func:`isolated_store`): no promisor remote is
configured there, so no git version fetches a missing object lazily, and
``GIT_NO_LAZY_FETCH`` (which only git 2.44 and later read) is set as a
second guard. The store itself is asked only for its tag refs and two
configuration values, which read no object. Reading the shared store
never contacts a remote and never writes it. Executor layer: git only, no
workflow, no journal, no state.
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
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .agent_worktree import alternate_entry
from .errors import SandboxLaunchRefusedError
from .executor import ExecutionRequest, ExecutionResult, execute
from .git_input import (
    MAX_SEED_MANIFEST_BYTES,
    SEED_MANIFEST_NAME,
    LaunchGitInput,
    seed_manifest_digest,
)
from .git_transport import NO_AUTO_MAINTENANCE, local_git_request
from .redaction import redact

Runner = Callable[[ExecutionRequest], ExecutionResult]

# The seed directory's two files, and nothing else.
SEED_PACK_NAME = "seed.pack"
SEED_FILES = (SEED_MANIFEST_NAME, SEED_PACK_NAME)
SEED_MANIFEST_SCHEMA = 1

# The seed's bounds (D4, D19), operator settings. A depth-1 seed holds the
# seed commit's whole tree, so they are sized for a large repository's
# tree, not for one issue's commits: a checkout of the Linux kernel is
# about 90,000 objects and a few hundred MB packed. A seed over either is
# refused before any journal entry, naming the setting.
DEFAULT_SEED_MAX_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_SEED_MAX_OBJECTS = 1_000_000
FULL_HISTORY: Literal["full"] = "full"

# The settings a refusal names (D19's keys).
SETTING_DEPTH = "sandbox.container.history_depth"
SETTING_TAGS = "sandbox.container.history_tags"
SETTING_MAX_BYTES = "sandbox.container.seed_max_bytes"
SETTING_MAX_OBJECTS = "sandbox.container.seed_max_objects"

# A partial clone's missing object is "missing", never fetched from its
# promisor remote. Only git 2.44 and later read this variable, and no
# minimum git is enforced, so it is never what that rests on: every git
# process that reads the shared store's objects runs in a controller-owned
# git directory (:func:`isolated_store`) where no promisor remote can be
# configured, which no git version fetches lazily from. The variable is
# kept as a second guard.
NO_LAZY_FETCH = {"GIT_NO_LAZY_FETCH": "1"}

# Listing, packing and indexing a large tree are bounded by time, as the
# materialized export is.
_GIT_TIMEOUT_SECONDS = 600
# What the executor keeps of a streamed process's output: the stream goes
# through ``on_chunk``, so the capture serves only the error tail.
_STREAM_CAPTURE_BYTES = 64 * 1024
# A tag of a tag of a tag: a longer chain is refused, never followed.
MAX_TAG_CHAIN = 32
# A ``.gitattributes`` file is read for D2's diagnostics up to this size;
# the rest of a larger one is not looked at.
_MAX_ATTRIBUTES_BYTES = 1024 * 1024
_MAX_ATTRIBUTES_FILES = 1000
_CHUNK_BYTES = 1024 * 1024
# A recursive tree listing's record: mode, type, id and a path. Past this
# many bytes per object the seed's object bound refuses the tree first.
_LISTING_BYTES_PER_ENTRY = 320

_HEX_LENGTH = {"sha1": 40, "sha256": 64}
_OID = {"sha1": re.compile(r"[0-9a-f]{40}"), "sha256": re.compile(r"[0-9a-f]{64}")}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PACK_SIGNATURE = b"PACK"
_PACK_VERSIONS = (2, 3)
_TAG_REF_PREFIX = "refs/tags/"
_OBJECT_TYPES = frozenset({"commit", "tree", "blob", "tag"})
# Attributes that would make a checkout write other bytes than the blob's.
_CONVERTING = ("filter", "ident", "eol", "text", "crlf", "working-tree-encoding")


def _refused(text: str) -> SandboxLaunchRefusedError:
    return SandboxLaunchRefusedError(f"the launch's seed is refused: {text}")


def _isolated_env(git_dir: Path, objects: Path | None = None) -> dict[str, str]:
    """The environment of a git process in the controller-owned git directory ``git_dir``.

    ``GIT_DIR`` names it, so no other repository is discovered. No system
    or global configuration is read, by variable and again by a ``HOME``
    and ``XDG_CONFIG_HOME`` that hold none (a git older than
    ``GIT_CONFIG_GLOBAL`` reads ``$HOME``): the only configuration is the
    one :func:`_private_git_dir` wrote, which names no remote. So no
    ``remote.<name>.promisor`` and no ``extensions.partialClone`` applies,
    and git, of any version, has no promisor remote to fetch a missing
    object from. ``objects``, when given, is read as the directory's only
    alternate (the shared store's ``objects``), never written.
    """
    env = {
        "GIT_DIR": str(git_dir),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "HOME": str(git_dir),
        "XDG_CONFIG_HOME": str(git_dir / "no-xdg-config"),
        **NO_LAZY_FETCH,
    }
    if objects is not None:
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = alternate_entry(objects)
    return env


@contextmanager
def isolated_store(
    common: str | Path, object_format: str, runner: Runner
) -> Iterator[tuple[Path, Runner]]:
    """The shared store of ``common``, read through a git directory of the controller's own.

    Yields that directory and ``runner`` with :func:`_isolated_env` layered
    over every request's environment, for a reader that names its
    repository itself (:func:`~autoforge.materialize.materialize_into` and
    :func:`~autoforge.materialize.prove_tree` take the directory as
    theirs). The store's objects are its only alternate; the store's
    configuration, promisor remote, hooks and ``shallow`` grafts never
    apply, and nothing is written to it. A missing object is "missing"
    whatever the git version (#292, PR #504 R1-F6). The directory is made
    by ``mkdtemp`` (mode 0700), outside every repository, and removed after.
    """
    root = _private_git_dir(object_format, "autoforge-store-read-")
    env = _isolated_env(root, Path(common) / "objects")

    def run(request: ExecutionRequest) -> ExecutionResult:
        request.env = {**(request.env or {}), **env}
        return runner(request)

    try:
        yield root, run
    finally:
        shutil.rmtree(root, ignore_errors=True)


# -- the settings ---------------------------------------------------------------------


HistoryDepth = int | Literal["full"]


@dataclass(frozen=True)
class SeedSettings:
    """The operator's history and bound settings (D11, D4, D19): never the repository's.

    ``history_depth`` is how many commits below a boundary the seed adds
    (0, the default, adds none) or :data:`FULL_HISTORY`; ``history_tags``
    whether tags that peel to a seeded commit come with it; ``max_bytes``
    and ``max_objects`` the pack's bounds.
    """

    history_depth: HistoryDepth = 0
    history_tags: bool = False
    max_bytes: int = DEFAULT_SEED_MAX_BYTES
    max_objects: int = DEFAULT_SEED_MAX_OBJECTS

    def __post_init__(self) -> None:
        depth = self.history_depth
        if depth != FULL_HISTORY and (type(depth) is not int or depth < 0):
            raise ValueError(
                f"{SETTING_DEPTH} must be a number of commits or 'full', not {depth!r}"
            )
        if type(self.history_tags) is not bool:
            raise ValueError(f"{SETTING_TAGS} must be true or false, not {self.history_tags!r}")
        for name, value in (
            (SETTING_MAX_BYTES, self.max_bytes),
            (SETTING_MAX_OBJECTS, self.max_objects),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive number, not {value!r}")

    def history(self) -> tuple[str, ...]:
        """The history settings not at their default, as a refusal names them."""
        named = []
        if self.history_depth != 0:
            named.append(f"{SETTING_DEPTH}: {self.history_depth}")
        if self.history_tags:
            named.append(f"{SETTING_TAGS}: true")
        return tuple(named)


DEFAULT_SEED_SETTINGS = SeedSettings()


# -- what staging supports (D2's table) -------------------------------------------------


@dataclass(frozen=True)
class StagingDiagnostic:
    """One case of D2's table, as a launch's facts and its agent's prompt state it."""

    case: str
    support: str
    detail: str

    def render(self) -> str:
        return f"{self.case}: {self.support}: {self.detail}"


SUBMODULE = "a submodule (gitlink)"
LFS = "Git LFS"
CONVERSIONS = "a filter or conversion attribute"
SPARSE = "a sparse checkout"
PARTIAL_CLONE = "a partial clone"


# -- the selection --------------------------------------------------------------------


@dataclass(frozen=True)
class SeedTag:
    """A tag the seed carries: its ref and the object it names (a tag object or a commit)."""

    ref: str
    object: str


@dataclass(frozen=True)
class SeedSelection:
    """What D11 names for one launch, read from the shared store: never from a pack.

    ``commits`` are every commit the pack must hold, ``boundaries`` the
    shallow ones among them (sorted), ``tags`` the selected tags (sorted by
    ref) and ``tag_objects`` every tag object on their chains; ``tips`` the
    commits the seed is walked from (the seed, then the carried commit);
    ``diagnostics`` D2's cases the seed commit's tree and the store show.
    """

    tips: tuple[str, ...]
    commits: frozenset[str]
    boundaries: tuple[str, ...]
    tags: tuple[SeedTag, ...] = ()
    tag_objects: frozenset[str] = frozenset()
    diagnostics: tuple[StagingDiagnostic, ...] = ()


# -- the manifest ---------------------------------------------------------------------


@dataclass(frozen=True)
class SeedManifest:
    """The seed's manifest (schema :data:`SEED_MANIFEST_SCHEMA`): what the git input binds.

    The first eleven fields are the launch's git input as the seed step was
    given it (its manifest digest aside, which is this manifest's own);
    then the shallow boundaries, the selected tags, the history settings,
    and the pack beside it.
    """

    phase: str
    run: str
    issue: int
    launch: str
    entry_label: str
    export_command: bool
    repository: str
    object_format: str
    seed: str
    lower: str
    carried: str
    boundaries: tuple[str, ...]
    tags: tuple[SeedTag, ...]
    history_depth: HistoryDepth
    history_tags: bool
    pack_bytes: int
    pack_objects: int
    pack_sha256: str

    def render(self) -> bytes:
        """The manifest's bytes: canonical JSON, with its schema."""
        data: dict[str, object] = {"schema": SEED_MANIFEST_SCHEMA}
        for key in _MANIFEST_TYPES:
            value = getattr(self, key)
            if key == "boundaries":
                value = list(value)
            elif key == "tags":
                value = [{"ref": tag.ref, "object": tag.object} for tag in value]
            data[key] = value
        return (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")

    def identity(self) -> dict[str, object]:
        """The git input's fields as the manifest names them."""
        return {key: getattr(self, key) for key in _IDENTITY_KEYS}


_IDENTITY_KEYS = (
    "phase",
    "run",
    "issue",
    "launch",
    "entry_label",
    "export_command",
    "repository",
    "object_format",
    "seed",
    "lower",
    "carried",
)
_MANIFEST_TYPES: dict[str, type | tuple[type, ...]] = {
    "phase": str,
    "run": str,
    "issue": int,
    "launch": str,
    "entry_label": str,
    "export_command": bool,
    "repository": str,
    "object_format": str,
    "seed": str,
    "lower": str,
    "carried": str,
    "boundaries": list,
    "tags": list,
    "history_depth": (int, str),
    "history_tags": bool,
    "pack_bytes": int,
    "pack_objects": int,
    "pack_sha256": str,
}


def parse_manifest(raw: bytes) -> SeedManifest:
    """``raw`` as a seed manifest, strictly: exact keys, exact types, well-formed values."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise _refused(f"its manifest does not parse ({type(exc).__name__})") from exc
    if not isinstance(data, dict):
        raise _refused("its manifest is not a JSON object")
    if type(data.get("schema")) is not int or data.get("schema") != SEED_MANIFEST_SCHEMA:
        raise _refused(f"its manifest is not of schema {SEED_MANIFEST_SCHEMA}")
    keys = set(data) - {"schema"}
    if keys != set(_MANIFEST_TYPES):
        extra = sorted(keys - set(_MANIFEST_TYPES))
        missing = sorted(set(_MANIFEST_TYPES) - keys)
        raise _refused(
            "its manifest does not hold exactly its fields"
            + (f" (unknown: {redact(', '.join(extra))[:200]})" if extra else "")
            + (f" (missing: {', '.join(missing)})" if missing else "")
        )
    for key, kind in _MANIFEST_TYPES.items():
        value = data[key]
        kinds = kind if isinstance(kind, tuple) else (kind,)
        if type(value) not in kinds:
            raise _refused(
                f"its manifest's {key} is not a {' or '.join(k.__name__ for k in kinds)}"
            )
    object_format = data["object_format"]
    pattern = _OID.get(object_format)
    if pattern is None:
        raise _refused("its manifest names an unknown object format")
    boundaries = data["boundaries"]
    if not all(isinstance(oid, str) and pattern.fullmatch(oid) for oid in boundaries):
        raise _refused("its manifest's boundaries are not full object ids")
    tags: list[SeedTag] = []
    for tag in data["tags"]:
        if (
            not isinstance(tag, dict)
            or set(tag) != {"ref", "object"}
            or not isinstance(tag["ref"], str)
            or not tag["ref"].startswith(_TAG_REF_PREFIX)
            or not isinstance(tag["object"], str)
            or not pattern.fullmatch(tag["object"])
        ):
            raise _refused("its manifest's tags are not each a tag ref and a full object id")
        tags.append(SeedTag(tag["ref"], tag["object"]))
    depth = data["history_depth"]
    if depth != FULL_HISTORY and (type(depth) is not int or depth < 0):
        raise _refused("its manifest's history_depth is neither a number of commits nor 'full'")
    if data["pack_bytes"] < 0 or data["pack_objects"] < 0:
        raise _refused("its manifest's pack size or object count is negative")
    if not _SHA256_RE.fullmatch(data["pack_sha256"]):
        raise _refused("its manifest's pack_sha256 is not a SHA-256")
    values = {key: data[key] for key in _MANIFEST_TYPES}
    values["boundaries"] = tuple(boundaries)
    values["tags"] = tuple(tags)
    return SeedManifest(**values)


# -- git ------------------------------------------------------------------------------


def _ended(res: ExecutionResult) -> str:
    return "timed out" if res.timed_out else f"exited {res.exit_code}"


def _detail(res: ExecutionResult) -> str:
    lines = (res.stderr or res.stdout or "").strip().splitlines()
    return f": {redact(lines[-1])[:200]}" if lines else ""


class _Stream:
    """An ``on_chunk`` hook keeping a process's stdout, stopping it past ``limit`` bytes."""

    def __init__(self, limit: int) -> None:
        self.data = bytearray()
        self.limit = limit
        self.over = False
        self.stop = threading.Event()

    def __call__(self, stream: str, chunk: bytes) -> None:
        if stream != "stdout" or self.over:
            return
        if len(self.data) + len(chunk) > self.limit:
            self.over = True
            self.stop.set()
            return
        self.data += chunk


class _Git:
    """The seed step's git, in one git directory, through the hardened local request."""

    def __init__(
        self, runner: Runner, git_dir: Path, object_format: str, env: dict[str, str] | None = None
    ) -> None:
        self._runner = runner
        self.git_dir = git_dir
        self.object_format = object_format
        self._env = env or {}

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        return self._runner(request)

    def request(self, args: Sequence[str]) -> ExecutionRequest:
        return local_git_request(
            ["--git-dir", str(self.git_dir), *args],
            env={**self._env, **NO_LAZY_FETCH},
            timeout_seconds=_GIT_TIMEOUT_SECONDS,
        )

    def run(
        self,
        args: Sequence[str],
        what: str,
        *,
        stdin: bytes | None = None,
        stdin_path: str | None = None,
        allow: tuple[int, ...] = (0,),
    ) -> ExecutionResult:
        request = self.request(args)
        request.stdin_data = stdin
        request.stdin_path = stdin_path
        res = self._runner(request)
        if res.timed_out or res.stdout_truncated or res.exit_code not in allow:
            raise _refused(f"{what} {_ended(res)}{_detail(res)}")
        return res

    def stream(
        self, args: Sequence[str], what: str, limit: int, *, stdin: bytes | None = None
    ) -> _Stream | None:
        """A process's whole stdout as bytes, or ``None`` when it passed ``limit``."""
        request = self.request(args)
        collector = _Stream(limit)
        request.stdin_data = stdin
        request.on_chunk = collector
        request.stop = collector.stop
        request.max_output_bytes = _STREAM_CAPTURE_BYTES
        res = self._runner(request)
        if collector.over:
            return None
        if res.timed_out or res.exit_code != 0 or res.capture_abandoned:
            raise _refused(f"{what} {_ended(res)}{_detail(res)}")
        return collector

    def kinds(self, ids: Iterable[str], what: str) -> dict[str, str]:
        """The type of each of ``ids``, ``"missing"`` for one the store lacks; no content read."""
        wanted = list(dict.fromkeys(ids))
        if not wanted:
            return {}
        width = _HEX_LENGTH[self.object_format] + len(" missing\n")
        collector = self.stream(
            ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
            what,
            limit=len(wanted) * width,
            stdin=("\n".join(wanted) + "\n").encode("ascii"),
        )
        if collector is None:
            raise _refused(f"{what}: git's answer passed its bound")
        lines = bytes(collector.data).decode("ascii", "replace").splitlines()
        if len(lines) != len(wanted):
            raise _refused(f"{what}: git answered {len(lines)} lines for {len(wanted)} objects")
        found: dict[str, str] = {}
        for oid, line in zip(wanted, lines, strict=True):
            named, _, kind = line.partition(" ")
            if named != oid or (kind != "missing" and kind not in _OBJECT_TYPES):
                raise _refused(f"{what}: git's answer for {oid} is not its type")
            found[oid] = kind
        return found

    def objects(self, ids: Iterable[str], what: str) -> Iterator[tuple[str, str, bytes]]:
        """``(id, type, content)`` of each of ``ids``, re-hashed to its id (ADR 0004 D7.5).

        An object the store lacks is ``(id, "missing", b"")``.
        """
        wanted = list(dict.fromkeys(ids))
        if not wanted:
            return
        collector = self.stream(
            ["cat-file", "--batch"],
            what,
            # Commits and tag objects are small; a store answer past this is refused.
            limit=max(64 * 1024 * 1024, len(wanted) * 4096),
            stdin=("\n".join(wanted) + "\n").encode("ascii"),
        )
        if collector is None:
            raise _refused(f"{what}: git's answer passed its bound")
        data = bytes(collector.data)
        at = 0
        object_format = self.object_format
        for oid in wanted:
            end = data.find(b"\n", at)
            if end < 0:
                raise _refused(f"{what}: git's answer ends early")
            header = data[at:end].split(b" ")
            at = end + 1
            if header == [oid.encode(), b"missing"]:
                yield oid, "missing", b""
                continue
            if len(header) != 3 or header[0] != oid.encode() or not header[2].isdigit():
                raise _refused(f"{what}: git's answer for {oid} is not an object header")
            kind = header[1].decode("ascii", "replace")
            size = int(header[2])
            content = data[at : at + size]
            if len(content) != size or data[at + size : at + size + 1] != b"\n":
                raise _refused(f"{what}: git's answer for {oid} ends early")
            at += size + 1
            if kind not in _OBJECT_TYPES:
                raise _refused(f"{what}: {oid} is of an unknown type")
            computed = hashlib.new(
                object_format, f"{kind} {size}\0".encode("ascii") + content
            ).hexdigest()
            if computed != oid:
                raise _refused(
                    f"{what}: the bytes the shared store holds for {oid} do not hash to its id "
                    "(a rewritten or substituted object); it is not used"
                )
            yield oid, kind, content


def _private_git_dir(object_format: str, prefix: str) -> Path:
    """A controller-owned minimal git directory (``mkdtemp``, mode 0700, outside every repository).

    Written here, not by ``git init``, so no template or hook is copied in:
    ``HEAD``, an empty ``refs/``, ``objects/pack`` and a ``config`` that
    turns gc, maintenance and the commit-graph off.
    """
    root = Path(tempfile.mkdtemp(prefix=prefix))
    try:
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
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return root


@contextmanager
def _walk_dir(common: Path, object_format: str, runner: Runner) -> Iterator[_Git]:
    """A private git directory reading the shared store as its only alternate; removed after.

    The walks run here rather than in the store, so the store's own
    configuration (a promisor remote, its ``shallow`` grafts, its hooks) is
    never read by them, and nothing is ever written to it; nor is any
    configuration of the operator's (:func:`_isolated_env`), so no git
    version fetches a missing object lazily.
    """
    root = _private_git_dir(object_format, "autoforge-seed-walk-")
    try:
        yield _Git(runner, root, object_format, _isolated_env(root, Path(common) / "objects"))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _parents(content: bytes) -> list[str]:
    """The parent ids a commit object's own bytes name, in order."""
    found: list[str] = []
    for line in content.split(b"\n"):
        if not line:
            break
        if line.startswith(b"parent "):
            found.append(line[7:].decode("ascii", "replace"))
    return found


def _read_commits(git: _Git, ids: Iterable[str], what: str) -> dict[str, list[str] | None]:
    """Each commit's parents from its own re-hashed bytes; ``None`` for one the store lacks."""
    found: dict[str, list[str] | None] = {}
    for oid, kind, content in git.objects(ids, what):
        if kind == "missing":
            found[oid] = None
        elif kind != "commit":
            raise _refused(f"{what}: {oid} is a {kind}, not a commit")
        else:
            found[oid] = _parents(content)
    return found


# -- the selection --------------------------------------------------------------------


def _too_many(count: int, settings: SeedSettings, what: str) -> SandboxLaunchRefusedError:
    return _refused(
        f"{what} holds {count} objects or more, over the bound of {settings.max_objects} "
        f"({SETTING_MAX_OBJECTS})" + _history_note(settings)
    )


def _history_note(settings: SeedSettings) -> str:
    named = settings.history()
    if not named:
        return ""
    return f"; the history settings made it larger ({', '.join(named)})"


def _check_bound_revision(git: _Git, git_input: LaunchGitInput) -> None:
    """D4: the seed (and carried commit) and the lower boundary are commits the store holds,
    the lower boundary an ancestor of the seed, the carried commit a descendant of it."""
    wanted = [git_input.seed, *([git_input.lower] if git_input.lower else [])]
    if git_input.carried:
        wanted.append(git_input.carried)
    commits = _read_commits(git, wanted, "reading the bound revision from the shared store")
    for name, oid in (("seed", git_input.seed), ("lower boundary", git_input.lower)):
        if oid and commits.get(oid) is None:
            raise _refused(f"the bound {name} {oid} is not a commit the shared store holds")
    if git_input.carried and commits.get(git_input.carried) is None:
        raise _refused(f"the carried commit {git_input.carried} is not in the shared store")

    def is_ancestor(ancestor: str, descendant: str) -> bool:
        res = git.run(
            ["merge-base", "--is-ancestor", ancestor, descendant],
            "checking the bound revision's ancestry",
            allow=(0, 1),
        )
        return res.exit_code == 0

    if git_input.lower and not is_ancestor(git_input.lower, git_input.seed):
        raise _refused(
            f"the bound lower boundary {git_input.lower} is not an ancestor of the seed "
            f"{git_input.seed}"
        )
    if git_input.carried and not is_ancestor(git_input.seed, git_input.carried):
        raise _refused(
            f"the carried commit {git_input.carried} does not descend from the seed "
            f"{git_input.seed}"
        )


def _ids(collector: _Stream, object_format: str, what: str) -> list[str]:
    pattern = _OID[object_format]
    out: list[str] = []
    for line in bytes(collector.data).decode("ascii", "replace").splitlines():
        if not pattern.fullmatch(line):
            raise _refused(f"{what}: git listed something that is not an object id")
        out.append(line)
    return out


def _line_limit(settings: SeedSettings, object_format: str) -> int:
    return (settings.max_objects + 1) * (_HEX_LENGTH[object_format] + 1)


def _deepen(
    git: _Git,
    included: set[str],
    frontier: set[str],
    parents: dict[str, list[str] | None],
    settings: SeedSettings,
    object_format: str,
) -> None:
    """Add up to ``history_depth`` generations below ``frontier`` (D11), in place."""
    if settings.history_depth == FULL_HISTORY:
        # Every ancestor, each with its parents. A parent the store lacks (a
        # shallow store's grafted history) fails the walk: full history is
        # what the operator asked for, and the store does not hold it.
        collector = git.stream(
            ["rev-list", "--parents", *sorted(frontier)],
            "listing the whole history below the seed's boundaries",
            _line_limit(settings, object_format) * 4,
        )
        if collector is None:
            raise _too_many(settings.max_objects + 1, settings, "the seed's history")
        pattern = _OID[object_format]
        for line in bytes(collector.data).decode("ascii", "replace").splitlines():
            ids = line.split()
            if not ids or not all(pattern.fullmatch(oid) for oid in ids):
                raise _refused("listing the history: git listed something that is not an object id")
            parents.setdefault(ids[0], ids[1:])
            included.add(ids[0])
        if len(included) > settings.max_objects:
            raise _too_many(len(included), settings, "the seed's history")
        return
    depth = settings.history_depth
    assert isinstance(depth, int)
    for _ in range(depth):
        unread = [oid for oid in frontier if oid not in parents]
        parents.update(_read_commits(git, unread, "reading the history below the boundaries"))
        candidates = {
            parent
            for oid in frontier
            for parent in (parents.get(oid) or [])
            if parent not in included
        }
        found = _read_commits(git, candidates, "reading the history below the boundaries")
        # A parent the store lacks (a shallow store's grafted history) stays
        # out: the commit above it is a boundary.
        frontier = {oid for oid, read in found.items() if read is not None}
        parents.update({oid: read for oid, read in found.items() if read is not None})
        included |= frontier
        if len(included) > settings.max_objects:
            raise _too_many(len(included), settings, "the seed's history")
        if not frontier:
            return


def _select_tags(
    store: _Git, walk: _Git, commits: frozenset[str], object_format: str
) -> tuple[tuple[SeedTag, ...], frozenset[str]]:
    """D11's tags: each store tag that peels to a seeded commit, and its chain's tag objects.

    The store is asked for its tag refs' names and ids alone, which git
    answers from the refs without reading an object; every object is read
    through ``walk``, the controller's own git directory, so a missing one
    is never fetched lazily (PR #504 R1-F6).
    """
    collector = store.stream(
        ["for-each-ref", "--format=%(refname)%00%(objectname)", _TAG_REF_PREFIX],
        "listing the shared store's tags",
        64 * 1024 * 1024,
    )
    if collector is None:
        raise _refused(f"the shared store's tags are too many to list ({SETTING_TAGS})")
    pattern = _OID[object_format]
    refs: list[tuple[str, str]] = []
    for line in bytes(collector.data).split(b"\n"):
        if not line:
            continue
        fields = line.split(b"\0")
        if len(fields) != 2:
            raise _refused(f"the shared store's tag list is unreadable ({SETTING_TAGS})")
        try:
            ref = fields[0].decode("utf-8")
        except UnicodeDecodeError:
            raise _refused(
                f"a tag of the shared store has a name that is not UTF-8 ({SETTING_TAGS})"
            ) from None
        oid = fields[1].decode("ascii", "replace")
        if not ref.startswith(_TAG_REF_PREFIX) or not pattern.fullmatch(oid):
            raise _refused(f"the shared store's tag list is unreadable ({SETTING_TAGS})")
        refs.append((ref, oid))
    kinds = walk.kinds((oid for _, oid in refs), "reading the types of the shared store's tags")
    for ref, oid in refs:
        if kinds[oid] == "missing":
            raise _refused(
                f"the tag {redact(ref)[:120]} names {oid}, which the shared store lacks; a "
                f"partial clone's missing object is never fetched ({SETTING_TAGS})"
            )
    listed = [(ref, oid, kinds[oid]) for ref, oid in refs]
    # Each tag's chain, peeled one level at a time for every tag at once: one
    # read of the store per level, not one per tag object.
    chains: dict[str, list[str]] = {ref: [] for ref, _, _ in listed}
    ends = {ref: (oid, kind) for ref, oid, kind in listed}
    for _ in range(MAX_TAG_CHAIN):
        peeling = {ref: oid for ref, (oid, kind) in ends.items() if kind == "tag"}
        if not peeling:
            break
        read = {
            oid: (kind, content)
            for oid, kind, content in walk.objects(peeling.values(), "reading the tags' objects")
        }
        for ref, oid in peeling.items():
            kind, content = read[oid]
            if kind != "tag":
                raise _refused(f"the tag {ref} names {oid}, which is not a tag object")
            chains[ref].append(oid)
            ends[ref] = _tag_target(content, ref)
    else:
        if any(kind == "tag" for _, kind in ends.values()):
            raise _refused(f"a tag has a chain of tag objects longer than {MAX_TAG_CHAIN}")
    selected = [
        SeedTag(ref, oid)
        for ref, oid, _ in listed
        if ends[ref][1] == "commit" and ends[ref][0] in commits
    ]
    reached = {tag for item in selected for tag in chains[item.ref]}
    return tuple(sorted(selected, key=lambda tag: tag.ref)), frozenset(reached)


def _tag_target(content: bytes, ref: str) -> tuple[str, str]:
    """The ``object`` and ``type`` a tag object's own bytes name."""
    head = content.split(b"\n\n", 1)[0].split(b"\n")
    if len(head) < 2 or not head[0].startswith(b"object ") or not head[1].startswith(b"type "):
        raise _refused(f"the tag {ref} has a tag object git does not read")
    return head[0][7:].decode("ascii", "replace"), head[1][5:].decode("ascii", "replace")


def _diagnostics(
    store: _Git, walk: _Git, commit: str, settings: SeedSettings
) -> tuple[StagingDiagnostic, ...]:
    """D2's cases for the tree a launch gets and the store it is read from."""
    found: list[StagingDiagnostic] = []
    collector = walk.stream(
        ["ls-tree", "-r", "-z", "--full-tree", commit],
        "listing the seed commit's tree",
        settings.max_objects * _LISTING_BYTES_PER_ENTRY,
    )
    if collector is None:
        raise _too_many(settings.max_objects + 1, settings, "the seed commit's tree")
    entries = bytes(collector.data).split(b"\0")
    gitlinks = 0
    attributes: list[str] = []
    for record in entries:
        meta, _, path = record.partition(b"\t")
        fields = meta.split(b" ")
        if len(fields) != 3:
            continue
        if fields[0] == b"160000":
            gitlinks += 1
        elif fields[1] == b"blob" and path.rpartition(b"/")[2] == b".gitattributes":
            attributes.append(fields[2].decode("ascii", "replace"))
    if gitlinks:
        found.append(
            StagingDiagnostic(
                SUBMODULE,
                "supported as an empty directory",
                f"{gitlinks} gitlink(s) are staged as empty directories; no submodule commit is "
                "fetched, read or staged",
            )
        )
    lfs = converts = False
    for _, kind, content in walk.objects(
        attributes[:_MAX_ATTRIBUTES_FILES], "reading the tree's .gitattributes"
    ):
        if kind != "blob":
            continue
        text = content[:_MAX_ATTRIBUTES_BYTES].decode("utf-8", "replace")
        for line in text.splitlines():
            words = line.split()[1:] if not line.lstrip().startswith("#") else []
            for word in words:
                # "-text" or "!eol" turns a conversion off: only a set or
                # valued attribute would make a checkout rewrite bytes.
                name = word.partition("=")[0]
                if word == "filter=lfs":
                    lfs = True
                elif name in _CONVERTING:
                    converts = True
    if lfs:
        found.append(
            StagingDiagnostic(
                LFS,
                "unsupported for content",
                "the pointer files are staged byte for byte; no LFS object is fetched or smudged",
            )
        )
    if converts:
        found.append(
            StagingDiagnostic(
                CONVERSIONS,
                "not applied",
                "each file is staged with its blob's bytes; no filter driver, ident, eol, text or "
                "working-tree-encoding conversion runs",
            )
        )
    sparse = store.run(
        ["config", "--type=bool", "--get", "core.sparseCheckout"],
        "reading the shared store's sparse-checkout setting",
        allow=(0, 1),
    )
    if sparse.stdout.strip() == "true":
        found.append(
            StagingDiagnostic(
                SPARSE,
                "unsupported",
                "the launch gets the seed commit's whole tree; no sparse pattern is read or "
                "applied",
            )
        )
    promisor = store.run(
        ["config", "--get-regexp", r"^(extensions\.partialclone|remote\..*\.promisor)$"],
        "reading the shared store's partial-clone setting",
        allow=(0, 1),
    )
    if promisor.stdout.strip():
        found.append(
            StagingDiagnostic(
                PARTIAL_CLONE,
                "unsupported for missing objects",
                "the shared store is read with no lazy fetch; an object it lacks refuses the seed "
                "and the promisor remote is never contacted",
            )
        )
    return tuple(found)


def select_seed(
    common: Path, git_input: LaunchGitInput, settings: SeedSettings, runner: Runner | None = None
) -> SeedSelection:
    """What the seed of ``git_input`` holds (D4, D11), read from the shared store of ``common``."""
    run = runner or execute
    with _walk_dir(common, git_input.object_format, run) as walk:
        return _select(Path(common), walk, git_input, settings, run)


def _select(
    common: Path, walk: _Git, git_input: LaunchGitInput, settings: SeedSettings, run: Runner
) -> SeedSelection:
    object_format = git_input.object_format
    store = _Git(run, common, object_format)
    _check_bound_revision(walk, git_input)
    tips = (git_input.seed, *((git_input.carried,) if git_input.carried else ()))
    diagnostics = _diagnostics(store, walk, git_input.carried or git_input.seed, settings)
    if git_input.export_command:
        return SeedSelection(
            tips=tips,
            commits=frozenset({git_input.seed}),
            boundaries=(git_input.seed,),
            diagnostics=diagnostics,
        )
    collector = walk.stream(
        ["rev-list", *tips, "--not", git_input.lower],
        "listing the commits above the lower boundary",
        _line_limit(settings, object_format),
    )
    if collector is None:
        raise _too_many(settings.max_objects + 1, settings, "the range above the lower boundary")
    in_range = set(_ids(collector, object_format, "listing the range"))
    parents = _read_commits(walk, [*in_range, git_input.lower], "reading the range's commits")
    if any(read is None for read in parents.values()):
        missing = sorted(oid for oid, read in parents.items() if read is None)
        raise _refused(f"the range's commit {missing[0]} is not in the shared store")
    outside = {
        parent for oid in in_range for parent in (parents[oid] or []) if parent not in in_range
    }
    base = outside | {git_input.lower}
    found = _read_commits(walk, outside - set(parents), "reading the range's boundary parents")
    lacking = sorted(oid for oid, read in found.items() if read is None)
    if lacking:
        raise _refused(
            f"the parent {lacking[0]} of a commit above the lower boundary is not in the shared "
            "store, so the seed would miss a parent tree"
        )
    parents.update(found)
    included = in_range | base
    if len(included) > settings.max_objects:
        raise _too_many(len(included), settings, "the seed's commits")
    if settings.history_depth == 0:
        boundaries = base
    else:
        _deepen(walk, included, set(base), parents, settings, object_format)
        boundaries = {
            oid
            for oid in included - in_range
            if parents.get(oid) is None or any(p not in included for p in parents[oid] or [])
        }
    commits = frozenset(included)
    tags: tuple[SeedTag, ...] = ()
    tag_objects: frozenset[str] = frozenset()
    if settings.history_tags:
        tags, tag_objects = _select_tags(store, walk, commits, object_format)
    return SeedSelection(
        tips=tips,
        commits=commits,
        boundaries=tuple(sorted(boundaries)),
        tags=tags,
        tag_objects=tag_objects,
        diagnostics=diagnostics,
    )


# -- the build ------------------------------------------------------------------------


@dataclass(frozen=True)
class BuiltSeed:
    """A checked seed in its controller-owned temporary directory (mode 0700).

    ``digest`` is ``sha256:<hex>`` of its manifest's bytes, what the
    launch's git input binds; ``directory`` holds exactly
    :data:`SEED_FILES`. The caller removes it (:func:`discard`).
    """

    directory: Path
    digest: str
    manifest: SeedManifest
    selection: SeedSelection = field(repr=False)


class _PackFile:
    """``pack-objects --stdout`` written to a file as it arrives, stopped past ``limit`` bytes."""

    def __init__(self, fd: int, limit: int) -> None:
        self._fd = fd
        self.limit = limit
        self.written = 0
        self.over = False
        self.error: OSError | None = None
        self.digest = hashlib.sha256()
        self.stop = threading.Event()

    def __call__(self, stream: str, chunk: bytes) -> None:
        if stream != "stdout" or self.over or self.error is not None:
            return
        if self.written + len(chunk) > self.limit:
            self.over = True
            self.stop.set()
            return
        try:
            view = memoryview(chunk)
            while view:
                view = view[os.write(self._fd, view) :]
        except OSError as exc:
            self.error = exc
            self.stop.set()
            return
        self.written += len(chunk)
        self.digest.update(chunk)


def _pack(
    walk: _Git,
    selection: SeedSelection,
    settings: SeedSettings,
    object_format: str,
    target: Path,
) -> tuple[int, int, str]:
    """Pack exactly the selection's objects into ``target``; its bytes, objects and SHA-256."""
    (walk.git_dir / "shallow").write_text(
        "".join(f"{oid}\n" for oid in selection.boundaries), encoding="ascii"
    )
    collector = walk.stream(
        ["rev-list", "--objects", "--no-object-names", *selection.tips],
        "listing the seed's objects",
        _line_limit(settings, object_format),
    )
    if collector is None:
        raise _too_many(settings.max_objects + 1, settings, "the seed")
    ids = list(
        dict.fromkeys([*_ids(collector, object_format, "listing"), *sorted(selection.tag_objects)])
    )
    if len(ids) > settings.max_objects:
        raise _too_many(len(ids), settings, "the seed")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        sink = _PackFile(fd, settings.max_bytes)
        request = walk.request([*NO_AUTO_MAINTENANCE, "pack-objects", "--stdout", "-q"])
        request.stdin_data = ("".join(f"{oid}\n" for oid in ids)).encode("ascii")
        request.on_chunk = sink
        request.stop = sink.stop
        request.max_output_bytes = _STREAM_CAPTURE_BYTES
        res = walk.execute(request)
        if sink.over:
            raise _refused(
                f"its pack passed the bound of {settings.max_bytes} bytes ({SETTING_MAX_BYTES})"
                + _history_note(settings)
            )
        if sink.error is not None:
            raise _refused(f"writing its pack failed ({sink.error.strerror or sink.error})")
        if res.timed_out or res.exit_code != 0 or res.capture_abandoned:
            raise _refused(f"packing its objects {_ended(res)}{_detail(res)}")
        os.fsync(fd)
    finally:
        os.close(fd)
    return sink.written, len(ids), sink.digest.hexdigest()


def _write_new(directory: Path, name: str, data: bytes) -> None:
    fd = os.open(
        directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)


def build_seed(
    common: Path,
    git_input: LaunchGitInput,
    settings: SeedSettings = DEFAULT_SEED_SETTINGS,
    runner: Runner | None = None,
) -> BuiltSeed:
    """The checked seed of ``git_input`` (the seed step, D4, D13), in a new temporary directory.

    Built from the shared store of ``common`` (the git common dir) after the
    phase's fetch, through the hardened local git with no lazy fetch; then
    checked (:func:`check_seed`). The directory is made by ``mkdtemp``, mode
    ``0700``, outside every repository, and removed again on any failure. A
    refusal is :class:`~autoforge.errors.SandboxLaunchRefusedError`, a setup
    failure before any journal entry.
    """
    if git_input.seed_manifest_digest:
        raise _refused("the launch's git input already binds a seed manifest; a seed is built once")
    run = runner or execute
    directory = Path(tempfile.mkdtemp(prefix="autoforge-seed-"))
    try:
        with _walk_dir(common, git_input.object_format, run) as walk:
            selection = _select(Path(common), walk, git_input, settings, run)
            pack_bytes, pack_objects, pack_sha256 = _pack(
                walk, selection, settings, git_input.object_format, directory / SEED_PACK_NAME
            )
        manifest = SeedManifest(
            phase=git_input.phase,
            run=git_input.run,
            issue=git_input.issue,
            launch=git_input.launch,
            entry_label=git_input.entry_label,
            export_command=git_input.export_command,
            repository=git_input.repository,
            object_format=git_input.object_format,
            seed=git_input.seed,
            lower=git_input.lower,
            carried=git_input.carried,
            boundaries=selection.boundaries,
            tags=selection.tags,
            history_depth=0 if git_input.export_command else settings.history_depth,
            history_tags=False if git_input.export_command else settings.history_tags,
            pack_bytes=pack_bytes,
            pack_objects=pack_objects,
            pack_sha256=pack_sha256,
        )
        _write_new(directory, SEED_MANIFEST_NAME, manifest.render())
        check_seed(directory, git_input, settings, selection, run)
        return BuiltSeed(directory, seed_manifest_digest(str(directory)), manifest, selection)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def discard(directory: str | Path) -> None:
    """Remove a seed's temporary directory; idempotent."""
    if str(directory):
        shutil.rmtree(directory, ignore_errors=True)


# -- the seed's own checks (D4) ----------------------------------------------------------


def _open_seed_dir(directory: Path) -> int:
    try:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise _refused(
            f"its directory cannot be opened without following a link ({exc.strerror})"
        ) from exc
    try:
        names = sorted(os.listdir(dir_fd))
    except OSError as exc:
        os.close(dir_fd)
        raise _refused(f"its directory cannot be listed ({exc.strerror})") from exc
    if names != sorted(SEED_FILES):
        os.close(dir_fd)
        raise _refused(
            f"its directory holds {', '.join(redact(n)[:80] for n in names) or 'nothing'}, not "
            f"exactly {' and '.join(SEED_FILES)}"
        )
    return dir_fd


def _open_regular(dir_fd: int, name: str) -> tuple[int, os.stat_result]:
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd
        )
    except OSError as exc:
        raise _refused(
            f"its {name} cannot be opened without following a link ({exc.strerror})"
        ) from exc
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise _refused(f"its {name} is not a regular file")
    os.set_blocking(fd, True)
    return fd, st


def _read_manifest(dir_fd: int) -> SeedManifest:
    fd, st = _open_regular(dir_fd, SEED_MANIFEST_NAME)
    try:
        if st.st_size > MAX_SEED_MANIFEST_BYTES:
            raise _refused(f"its manifest holds {st.st_size} bytes, over its bound")
        raw = os.read(fd, MAX_SEED_MANIFEST_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) != st.st_size:
        raise _refused("its manifest changed size while it was read")
    return parse_manifest(raw)


def _check_manifest(
    manifest: SeedManifest,
    git_input: LaunchGitInput,
    settings: SeedSettings,
    selection: SeedSelection,
) -> None:
    expected = {key: value for key, value in git_input.identity().items() if key in _IDENTITY_KEYS}
    named = manifest.identity()
    for key, value in expected.items():
        if named[key] != value:
            shown = named[key] if isinstance(named[key], int) else redact(str(named[key]))[:80]
            raise _refused(
                f"its manifest names {key} {shown!r}, and the launch's git input binds {value!r}"
            )
    depth = 0 if git_input.export_command else settings.history_depth
    tags = False if git_input.export_command else settings.history_tags
    if (manifest.history_depth, manifest.history_tags) != (depth, tags):
        raise _refused(
            f"its manifest names the history settings {manifest.history_depth!r} and "
            f"{manifest.history_tags!r}, and the operator's are {depth!r} and {tags!r}"
        )
    if manifest.boundaries != selection.boundaries:
        raise _refused("its manifest's shallow boundaries are not the ones D11 names")
    if manifest.tags != selection.tags:
        raise _refused("its manifest's tag set is not the one the shared store's tags select")


def _check_pack_file(dir_fd: int, manifest: SeedManifest, settings: SeedSettings) -> None:
    fd, st = _open_regular(dir_fd, SEED_PACK_NAME)
    try:
        if st.st_size > settings.max_bytes:
            raise _refused(
                f"its pack holds {st.st_size} bytes, over the bound of {settings.max_bytes} "
                f"({SETTING_MAX_BYTES})" + _history_note(settings)
            )
        if st.st_size != manifest.pack_bytes:
            raise _refused(
                f"its pack holds {st.st_size} bytes, and its manifest says {manifest.pack_bytes}"
            )
        header = os.pread(fd, 12, 0)
        if len(header) != 12:
            raise _refused("its pack is shorter than a pack header")
        signature, version, count = struct.unpack(">4sII", header)
        if signature != _PACK_SIGNATURE or version not in _PACK_VERSIONS:
            raise _refused("its pack does not start with a pack header")
        if count > settings.max_objects:
            raise _too_many(count, settings, "its pack")
        if count != manifest.pack_objects:
            raise _refused(
                f"its pack holds {count} objects, and its manifest says {manifest.pack_objects}"
            )
        digest = hashlib.sha256()
        read = 0
        while chunk := os.read(fd, _CHUNK_BYTES):
            read += len(chunk)
            if read > st.st_size:
                raise _refused("its pack grew while it was hashed")
            digest.update(chunk)
        if read != st.st_size:
            raise _refused("its pack shrank while it was hashed")
        if digest.hexdigest() != manifest.pack_sha256:
            raise _refused("its pack's SHA-256 is not the one its manifest names")
    finally:
        os.close(fd)


def check_seed(
    directory: Path,
    git_input: LaunchGitInput,
    settings: SeedSettings,
    selection: SeedSelection,
    runner: Runner | None = None,
) -> SeedManifest:
    """The seed's own checks (D4): ``directory`` holds the seed ``selection`` names, or raise.

    ``selection`` is what :func:`select_seed` read from the shared store for
    ``git_input`` under ``settings``. Nothing is written but a quarantine of
    the controller's own, removed afterwards; the shared store is not read.
    """
    run = runner or execute
    dir_fd = _open_seed_dir(Path(directory))
    try:
        manifest = _read_manifest(dir_fd)
        _check_manifest(manifest, git_input, settings, selection)
        _check_pack_file(dir_fd, manifest, settings)
    finally:
        os.close(dir_fd)
    quarantine = _private_git_dir(git_input.object_format, "autoforge-seed-check-")
    try:
        _check_quarantined(
            _Git(run, quarantine, git_input.object_format, _isolated_env(quarantine)),
            Path(directory) / SEED_PACK_NAME,
            manifest,
            selection,
            git_input,
            settings,
        )
    finally:
        shutil.rmtree(quarantine, ignore_errors=True)
    return manifest


def _check_quarantined(
    git: _Git,
    pack: Path,
    manifest: SeedManifest,
    selection: SeedSelection,
    git_input: LaunchGitInput,
    settings: SeedSettings,
) -> None:
    if manifest.boundaries:
        (git.git_dir / "shallow").write_text(
            "".join(f"{oid}\n" for oid in manifest.boundaries), encoding="ascii"
        )
    git.run(
        [*NO_AUTO_MAINTENANCE, "index-pack", "--stdin", "--strict"],
        "its pack fails index-pack --strict against its shallow boundaries:",
        stdin_path=str(pack),
    )
    width = _HEX_LENGTH[git_input.object_format] + 1
    listed = git.stream(
        ["cat-file", "--batch-all-objects", "--batch-check=%(objecttype) %(objectname)"],
        "listing its pack's objects",
        (manifest.pack_objects + 1) * (width + len("commit ")),
    )
    if listed is None:
        raise _refused("its pack lists more objects than its manifest says")
    pattern = _OID[git_input.object_format]
    in_pack: dict[str, str] = {}
    for line in bytes(listed.data).decode("ascii", "replace").splitlines():
        kind, _, oid = line.partition(" ")
        if kind not in _OBJECT_TYPES or not pattern.fullmatch(oid):
            raise _refused("its pack lists an object git does not name")
        in_pack[oid] = kind
    if len(in_pack) != manifest.pack_objects:
        raise _refused(
            f"its pack indexes {len(in_pack)} distinct objects, and its manifest says "
            f"{manifest.pack_objects}"
        )
    for name, oid in (("seed commit", git_input.seed), ("carried commit", git_input.carried)):
        if oid and in_pack.get(oid) != "commit":
            raise _refused(f"its pack lacks the {name} {oid}")
    commits = {oid for oid, kind in in_pack.items() if kind == "commit"}
    if commits - selection.commits:
        raise _refused(
            f"its pack holds the commit {sorted(commits - selection.commits)[0]}, which D11 does "
            "not name"
        )
    if selection.commits - commits:
        raise _refused(
            f"its pack lacks the commit {sorted(selection.commits - commits)[0]}, which D11 names"
        )
    tag_objects = {oid for oid, kind in in_pack.items() if kind == "tag"}
    if tag_objects - selection.tag_objects:
        raise _refused(
            f"its pack holds the tag object {sorted(tag_objects - selection.tag_objects)[0]}, "
            "which the manifest's tag set does not reach"
        )
    if selection.tag_objects - tag_objects:
        raise _refused(
            f"its pack lacks the tag object {sorted(selection.tag_objects - tag_objects)[0]}, "
            "which the manifest's tag set reaches"
        )
    for tag in manifest.tags:
        res = git.run(
            ["rev-parse", "--verify", "-q", f"{tag.object}^{{commit}}"],
            f"peeling the tag {tag.ref}",
            allow=(0, 1),
        )
        if res.exit_code != 0 or res.stdout.strip() not in commits:
            raise _refused(f"the tag {redact(tag.ref)[:120]} peels to no commit in its pack")
    reached = git.stream(
        [
            "rev-list",
            "--objects",
            "--no-object-names",
            *selection.tips,
            *(tag.object for tag in manifest.tags),
        ],
        "walking its closure above its shallow boundaries",
        (len(in_pack) + 1) * width,
    )
    if reached is None:
        raise _refused("its closure above its shallow boundaries reaches more than its pack holds")
    closure = set(_ids(reached, git_input.object_format, "walking its closure"))
    if set(in_pack) - closure:
        stray = sorted(set(in_pack) - closure)[0]
        raise _refused(
            f"its pack holds the {in_pack[stray]} {stray}, which nothing the seed names reaches"
        )
    if closure - set(in_pack):
        raise _refused(
            f"its closure needs {sorted(closure - set(in_pack))[0]}, which its pack lacks"
        )
