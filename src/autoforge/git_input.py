"""A launch's git input: the revision the controller bound before the launch (ADR 0013 D4; #269).

Every launch, in every phase, starts from a revision the controller
verified and bound to the phase entry before it, never from a commit a
container names and never from an earlier container's workspace or volume.
:class:`LaunchGitInput` is that binding for one launch, typed and
immutable: the phase and the launch's identity, the repository's identity,
the expected seed commit and its lower boundary, the carried commit of a
relaunch, and, once the seed is built (#292), the digest of the seed's
manifest. Which revision a phase binds is the engine's rule
(:func:`autoforge.candidate_source.bound_git_input`); this module holds the
record, the invariants every one of them keeps, and the digest a seed's
manifest is checked against at the hand-off.

- **No later switch.** Nothing replaces a field once the record exists. The
  one completion is :meth:`LaunchGitInput.with_seed_manifest`, which fills
  the manifest's digest once, after the seed step built the seed; a second,
  different digest is refused.
- **The verify step compares against it.** An exported artifact whose
  manifest names another launch, repository, object format, seed commit,
  lower boundary, carried commit or seed-manifest digest is a candidate
  refusal (D5; :func:`autoforge.candidate_artifact.manifest_disagreement`).
- **The persisted part** is :meth:`LaunchGitInput.identity`, D19's "seed's
  identity". It is plain data and carries nothing a credential could hide
  in: names the controller chose and object ids.

Plain data, no I/O but :func:`seed_manifest_digest`, which reads one file
of a controller-owned seed directory without following a link. Executor
layer: :class:`~autoforge.sandbox_backend.LaunchInputs` carries the record
to a backend's seed step, which never decides anything about it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass

# The phases whose launches bind a git input (ADR 0013 D4). An export
# command (``validation.commands``, ``merge.verification_commands``) is not
# a phase of its own: it binds in the phase it runs in, with
# ``export_command`` set.
GIT_INPUT_PHASES = frozenset(
    {"ANALYZE_EXECUTE", "REVIEW", "FIX", "REPLAN_REEXECUTE", "UPDATE_EPIC"}
)
# The phases whose seed's lower boundary is the bound merge base rather
# than the seed itself (D4): a review's diff and a fix's are the PR's,
# against its base.
MERGE_BASE_PHASES = frozenset({"REVIEW", "FIX"})
# The phases that export a candidate, and so may carry one into a relaunch
# of the same entry (D4). ``REVIEW`` and ``UPDATE_EPIC`` export nothing.
CARRYING_PHASES = frozenset({"ANALYZE_EXECUTE", "FIX", "REPLAN_REEXECUTE"})

# The seed manifest's name in the seed's directory and the form of its
# digest (#292 writes the manifest; the input binds its bytes).
SEED_MANIFEST_NAME = "manifest.json"
DIGEST_PREFIX = "sha256:"
# A seed manifest names commits, boundaries and tags: one issue's seed is
# far below this. A larger one is refused, never read.
MAX_SEED_MANIFEST_BYTES = 4 * 1024 * 1024

_OID = {"sha1": re.compile(r"[0-9a-f]{40}"), "sha256": re.compile(r"[0-9a-f]{64}")}
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
# GitHub owner and repository names, as the git transport accepts them.
_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")
# A run key, a launch id: the controller's own names, printable, no space.
_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")


class GitInputError(ValueError):
    """A git input that breaks one of its invariants: never bound, never launched."""


def _is_oid(value: str, object_format: str) -> bool:
    pattern = _OID.get(object_format)
    return pattern is not None and pattern.fullmatch(value) is not None


def valid_repository(repository: str) -> bool:
    """Whether ``repository`` is an ``owner/name`` the controller accepts."""
    parts = repository.split("/")
    return len(parts) == 2 and all(
        _NAME_RE.fullmatch(part) and part not in (".", "..") for part in parts
    )


@dataclass(frozen=True)
class BoundRevision:
    """The revision a phase entry bound: the seed commit and its lower boundary.

    ``lower`` is the bound merge base for ``REVIEW`` and ``FIX`` and the
    base (the seed itself) for the other phases; "" for an export command,
    which carries no history.
    """

    seed: str
    lower: str


@dataclass(frozen=True)
class LaunchGitInput:
    """One launch's bound git input (ADR 0013 D4): immutable, checked on creation.

    ``phase`` is the phase's name, ``run``, ``issue`` and ``launch`` the
    launch's identity (the labels its objects carry), ``repository`` the
    ``owner/name`` and ``object_format`` its object format; ``seed`` the
    expected seed commit, ``lower`` its lower boundary, ``carried`` the
    carried commit ("" for none: the commit an earlier launch of the entry
    exported and the controller imported, or the earlier work on the issue's
    branch an ``ANALYZE_EXECUTE`` entry continues), ``seed_manifest_digest``
    the digest of the seed's manifest ("" until the seed step built one),
    ``export_command`` whether the launch is an export command rather than
    an agent, and ``entry_label`` the engine's name for the phase entry the
    launch belongs to (the review or fix round within its execution
    lifecycle, the lifecycle, the replan transaction), which tells apart
    two entries that bind the same revision.
    """

    phase: str
    run: str
    issue: int
    launch: str
    repository: str
    object_format: str
    seed: str
    lower: str
    carried: str = ""
    seed_manifest_digest: str = ""
    export_command: bool = False
    entry_label: str = ""

    def __post_init__(self) -> None:
        problem = self._problem()
        if problem:
            raise GitInputError(f"the launch's git input is refused: {problem}")

    def _problem(self) -> str:
        if self.phase not in GIT_INPUT_PHASES:
            return f"{self.phase!r} is not a phase that binds a git input"
        for name in ("run", "launch"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _KEY_RE.fullmatch(value):
                return f"its {name} {value!r} is not a controller name"
        if self.entry_label and (
            not isinstance(self.entry_label, str) or not _KEY_RE.fullmatch(self.entry_label)
        ):
            return f"its entry label {self.entry_label!r} is not a controller name"
        if type(self.issue) is not int or self.issue < 1:
            return f"its issue {self.issue!r} is not an issue number"
        if not isinstance(self.repository, str) or not valid_repository(self.repository):
            return f"its repository {self.repository!r} is not an owner/name"
        if self.object_format not in _OID:
            return f"its object format {self.object_format!r} is neither sha1 nor sha256"
        if not _is_oid(self.seed, self.object_format):
            return f"its seed {self.seed!r} is not a full {self.object_format} object id"
        if self.export_command:
            if self.lower or self.carried:
                return "an export command carries no history: no lower boundary, no carried commit"
        else:
            if not _is_oid(self.lower, self.object_format):
                return (
                    f"its lower boundary {self.lower!r} is not a full {self.object_format} "
                    "object id"
                )
            if self.phase not in MERGE_BASE_PHASES and self.lower != self.seed:
                return (
                    f"a {self.phase} launch's lower boundary is its base, the seed itself, "
                    f"not {self.lower}"
                )
        if self.carried:
            if self.phase not in CARRYING_PHASES:
                return f"a {self.phase} launch exports nothing, so it carries no commit"
            if not _is_oid(self.carried, self.object_format):
                return f"its carried commit {self.carried!r} is not a full object id"
            if self.carried == self.seed:
                return "its carried commit is the seed itself, which carries nothing"
        if self.seed_manifest_digest and not _DIGEST_RE.fullmatch(self.seed_manifest_digest):
            return f"its seed manifest digest {self.seed_manifest_digest!r} is not sha256:<hex>"
        return ""

    def with_seed_manifest(self, digest: str) -> LaunchGitInput:
        """The input completed with the digest of the seed the seed step built; once only."""
        if self.seed_manifest_digest and self.seed_manifest_digest != digest:
            raise GitInputError(
                "the launch's git input already binds the seed manifest "
                f"{self.seed_manifest_digest}, and a seed with {digest} would replace it"
            )
        return dataclasses.replace(self, seed_manifest_digest=digest)

    @property
    def entry(self) -> str:
        """The phase entry this launch belongs to: what every relaunch of it shares.

        A relaunch of the same entry binds the same revision (D4), so the
        entry is the run, the issue, the phase, the engine's label for the
        entry and that revision. Two entries that bind the same revision
        (a second fix round at a HEAD the first did not move) differ by
        their label. The carried commit, the launch id and the seed's
        manifest are not part of it: they are what differs between the
        entry's launches.
        """
        kind = "export" if self.export_command else "agent"
        material = "\0".join(
            (self.run, str(self.issue), self.phase, self.entry_label, kind, self.seed, self.lower)
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def identity(self) -> dict[str, object]:
        """The persisted part (ADR 0013 D19's "seed's identity"), as plain data."""
        return {
            "phase": self.phase,
            "run": self.run,
            "issue": self.issue,
            "launch": self.launch,
            "repository": self.repository,
            "object_format": self.object_format,
            "seed": self.seed,
            "lower": self.lower,
            "carried": self.carried,
            "seed_manifest_digest": self.seed_manifest_digest,
            "export_command": self.export_command,
            "entry_label": self.entry_label,
        }

    @property
    def digest(self) -> str:
        """``sha256:<hex>`` of :meth:`identity` in canonical JSON."""
        canonical = json.dumps(self.identity(), sort_keys=True, separators=(",", ":"))
        return DIGEST_PREFIX + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def from_identity(cls, data: object) -> LaunchGitInput:
        """The input :meth:`identity` saved, read back strictly: exact keys, exact types."""
        if not isinstance(data, dict) or set(data) != set(_IDENTITY_TYPES):
            raise GitInputError("a saved git input does not hold exactly its fields")
        for key, kind in _IDENTITY_TYPES.items():
            if type(data[key]) is not kind:
                raise GitInputError(f"a saved git input's {key} is not a {kind.__name__}")
        return cls(**data)


_IDENTITY_TYPES: dict[str, type] = {
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
}


def seed_manifest_digest(directory: str) -> str:
    """``sha256:<hex>`` of the seed manifest in ``directory``; raise when it cannot be read.

    The manifest is opened without following a link, beneath a directory
    opened the same way, and must be a regular file no larger than
    :data:`MAX_SEED_MANIFEST_BYTES`: a link, a FIFO or an oversized file is
    refused before a byte of it is read. What is hashed is what was read
    from that descriptor.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    dir_fd = os.open(directory, flags | os.O_DIRECTORY)
    try:
        fd = os.open(SEED_MANIFEST_NAME, flags | os.O_NONBLOCK, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"the seed manifest in {directory} is not a regular file")
        if st.st_size > MAX_SEED_MANIFEST_BYTES:
            raise OSError(
                f"the seed manifest in {directory} holds {st.st_size} bytes, over the bound "
                f"of {MAX_SEED_MANIFEST_BYTES}"
            )
        digest = hashlib.sha256()
        read = 0
        while chunk := os.read(fd, 65536):
            read += len(chunk)
            if read > MAX_SEED_MANIFEST_BYTES:
                raise OSError(f"the seed manifest in {directory} grew past its bound")
            digest.update(chunk)
    finally:
        os.close(fd)
    return DIGEST_PREFIX + digest.hexdigest()


def seed_problem(directory: str, git_input: LaunchGitInput | None) -> str:
    """Why the seed in ``directory`` is not the one ``git_input`` binds, or "" (D4's hand-off).

    A launch with no git input, or an input with no manifest digest, binds
    no seed: any seed it is handed is refused, never trusted unbound.
    """
    if git_input is None or not git_input.seed_manifest_digest:
        return "the launch's git input binds no seed manifest, so no seed is handed on"
    try:
        found = seed_manifest_digest(directory)
    except OSError as exc:
        return f"its seed manifest cannot be read ({exc})"
    if found != git_input.seed_manifest_digest:
        return (
            f"its seed manifest is {found}, not {git_input.seed_manifest_digest}, which the "
            "launch's git input binds"
        )
    return ""
