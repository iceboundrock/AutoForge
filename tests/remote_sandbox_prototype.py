"""A remote execution sandbox's transfer and shutdown: ADR 0014's prototype (#276).

No test, and not part of the package. ADR 0014 records a no-go for a remote
production adapter (#277), so nothing here ships: it shows, against a fake
provider, that the integrity boundary ADR 0013 designs for copy-in runtimes
holds when the runtime is on another machine, and that every interruption
and every expiry fails closed. A reopening that goes moves what it needs
into ``src/autoforge`` under its own review; until then nothing in the
package imports this module (``tests/test_remote_sandbox_prototype.py``
checks that).

What it holds, each named after the part of ADR 0014's contract (§4) it
prototypes:

- **R1, identity.** :class:`RemoteIdentity` is the journal entry the
  controller writes before it asks a provider for anything: the launch's
  identity (run, issue, phase, launch id), a nonce, the provider and the
  scope the controller's credential reaches, and the launch's ceiling. The
  resource's name and its tags derive from it alone, so after a crash the
  entry finds its resource again by name, and proves it is the run's by
  the tags and the nonce. :func:`create_remote` re-issues an ambiguous
  create only after a lookup by that name, and a name is unique in its
  scope, so a re-issue can never make a second resource.
- **R2 and R9, credentials.** :func:`create_request_problem` refuses a
  create that would hand the remote a GitHub or git credential, the
  provider's own credential, a credential the provider injects for a
  GitHub host, or one the provider manages.
- **R3 and R7, the deadline.** Every resource is created with a deadline
  the provider enforces, at most one renewal window ahead and never past
  the ceiling. :func:`keep_deadline` renews it while the controller runs,
  so a controller that dies leaves a resource the provider ends within one
  window. Both read the deadline back, and a provider that holds the
  resource past the deadline asked for ends the launch, its entry kept for
  the removal: the bound is never assumed of the provider. That is the
  controller's request to the provider, not a protocol between AutoForge
  processes: nothing on the remote side decides anything.
- **R4 and R5, the transfer.** :func:`send_seed` checks the seed against
  the launch's git input (ADR 0013 D4's hand-off) before a byte leaves,
  streams each file with its size declared, checks the remote's digest of
  what it stored, and refuses a pack other than the one the bound manifest
  names by digest. :func:`stage_seed` is the stager's own check of the
  same binding, on the remote side, before anything is indexed.
  :func:`receive_artifact` streams the artifact's manifest and pack into
  the launch's export directory under the export bound, hashes the pack as
  it reads it, writes every byte it counts, stops at the deadline, and
  deletes whatever it wrote when anything fails. Neither is the content
  boundary: the strict import (:func:`autoforge.candidate_artifact.import_artifact`)
  is, and it checks everything again.
- **R6 and R8, termination and recovery.** :func:`terminate` destroys an
  owned resource and closes nothing until a passive lookup no longer finds
  it. :func:`reconcile` is remove-only: it never reads a file of a remote
  resource, so it never exports or imports anything, and it blocks on what
  it cannot prove the run's.

Every refusal of a launch, a transfer or a removal is one of the package's
typed errors, as ADR 0013 D17 types the same failure of a local container:
a seed that cannot be handed on, or a resource created with a deadline past
its bound, is a setup failure
(:class:`~autoforge.errors.SandboxLaunchRefusedError`), an artifact that
cannot be received a candidate refusal
(:class:`~autoforge.agent_worktree.ObjectImportError`), a removal that
cannot be verified :class:`~autoforge.errors.SandboxCleanupUncertainError`,
and a scope other than the entries' a
:class:`~autoforge.errors.ConfigurationError`. A renewal the provider holds
past its bound is :class:`DeadlineUnbounded`: no local container renews a
deadline, so D17 has no class for it.

:class:`FakeRemote` stands in for the provider. It enforces the deadline
itself (an expired resource stops and loses its files, as a non-persistent
sandbox does), and injects the faults the tests need: a lost create
response, a deadline kept past the one asked for, a stream cut with an
error, a stream that ends short or runs long, a stored file that differs
from what was sent, a destroy that returns but leaves the resource, a
lookup that fails.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import io
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from autoforge.agent_worktree import ObjectImportError
from autoforge.candidate_artifact import (
    ARTIFACT_MANIFEST,
    ARTIFACT_PACK,
    EXPORT_BOUND,
    ArtifactLimits,
    CandidateManifest,
    parse_manifest,
)
from autoforge.child_env import forbidden_env_entry
from autoforge.errors import (
    ConfigurationError,
    ExecutionError,
    SandboxCleanupUncertainError,
    SandboxLaunchRefusedError,
)
from autoforge.git_input import (
    DIGEST_PREFIX,
    MAX_SEED_MANIFEST_BYTES,
    SEED_MANIFEST_NAME,
    LaunchGitInput,
    seed_problem,
)

Clock = Callable[[], float]

REMOTE_ENTRY_SCHEMA = 1
# The seed's pack beside its manifest (#292 names the real one).
SEED_PACK = "seed.pack"
SEED_FILES = (SEED_MANIFEST_NAME, SEED_PACK)
# The stand-in manifest's field naming the pack by its sha256: how the git
# input, which binds the manifest's bytes, binds the pack's too.
SEED_PACK_DIGEST = "pack_sha256"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
# A stand-in for #292's seed_max_bytes, which does not exist yet.
SEED_MAX_BYTES = 512 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
# No transfer starts this close to the resource's deadline: it could be cut.
TRANSFER_MARGIN_SECONDS = 30.0
# Bounded retries of a removal and of a lookup, as ADR 0013 D6 bounds them.
REMOVAL_ATTEMPTS = 3
LOOKUP_ATTEMPTS = 3

# A resource's name: DNS-label safe and at most 63 characters, which every
# provider ADR 0014 compares accepts (a VM name, a sandbox name).
NAME_PREFIX = "af-"
NAME_RE = re.compile(r"af-[0-9a-f]{40}")
# Five tags, the most Vercel Sandbox keeps on one sandbox; a tag value is at
# most 256 characters there, which a run key (200 at most) and a launch id
# fit.
TAG_RUN = "autoforge.run"
TAG_ISSUE = "autoforge.issue"
TAG_LAUNCH = "autoforge.launch"
TAG_KIND = "autoforge.kind"
TAG_NONCE = "autoforge.nonce"
TAG_KEYS = (TAG_RUN, TAG_ISSUE, TAG_LAUNCH, TAG_KIND, TAG_NONCE)
KIND = "launch"
MAX_TAG_VALUE = 256
_NONCE_RE = re.compile(r"[0-9a-f]{32}")
_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
_SCOPE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:-]{0,199}")
_PROVIDER_RE = re.compile(r"[a-z][a-z0-9-]{0,39}")

# Hosts a brokered credential would make GitHub write authority for (R9).
GITHUB_HOSTS = (
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "uploads.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
)


# -- R1: the resource's identity --------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """What a passive lookup of one resource returned: a claim of the provider."""

    name: str
    tags: Mapping[str, str]
    state: str  # "running", "stopping" or "stopped"
    deadline: float  # the provider-side deadline, seconds since the epoch


@dataclass(frozen=True)
class RemoteIdentity:
    """The journal entry of one remote resource, written before it is created (R1).

    ``provider`` and ``scope`` bind the entry to where its resource lives:
    the provider, and the account, project and region the controller's
    credential reaches, as the provider reported them. A lookup in another
    scope would find nothing, and nothing must be read as gone there
    (ADR 0013 D6 item 10's rule for a store). ``ceiling`` is the launch's
    runtime ceiling plus ADR 0002's graces, as a time: no deadline the
    controller asks for passes it. ``window`` is the renewal window.
    """

    provider: str
    scope: str
    run: str
    issue: int
    phase: str
    launch: str
    nonce: str
    ceiling: float
    window: float

    def __post_init__(self) -> None:
        problem = self._problem()
        if problem:
            raise ValueError(f"a remote resource's journal entry is refused: {problem}")

    def _problem(self) -> str:
        if not isinstance(self.provider, str) or not _PROVIDER_RE.fullmatch(self.provider):
            return f"its provider {self.provider!r} is not a provider name"
        if not isinstance(self.scope, str) or not _SCOPE_RE.fullmatch(self.scope):
            return f"its scope {self.scope!r} is not a scope"
        for name in ("run", "phase", "launch"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _KEY_RE.fullmatch(value):
                return f"its {name} {value!r} is not a controller name"
        if type(self.issue) is not int or self.issue < 1:
            return f"its issue {self.issue!r} is not an issue number"
        if not isinstance(self.nonce, str) or not _NONCE_RE.fullmatch(self.nonce):
            return "its nonce is not 32 hexadecimal digits"
        for name in ("ceiling", "window"):
            if type(getattr(self, name)) is not float or getattr(self, name) <= 0:
                return f"its {name} is not a positive number of seconds"
        return ""

    @classmethod
    def new(
        cls,
        git_input: LaunchGitInput,
        *,
        provider: str,
        scope: str,
        ceiling: float,
        window: float,
        nonce: str | None = None,
    ) -> RemoteIdentity:
        """The entry of a launch's resource, under a fresh nonce unless one is given."""
        return cls(
            provider=provider,
            scope=scope,
            run=git_input.run,
            issue=git_input.issue,
            phase=git_input.phase,
            launch=git_input.launch,
            nonce=nonce or secrets.token_hex(16),
            ceiling=float(ceiling),
            window=float(window),
        )

    @property
    def name(self) -> str:
        """The resource's name: a function of the entry alone, unique in its scope."""
        material = "\0".join((self.run, str(self.issue), self.launch, self.nonce))
        return NAME_PREFIX + hashlib.sha256(material.encode()).hexdigest()[:40]

    def tags(self) -> dict[str, str]:
        """The tags the resource carries, and that prove it the run's."""
        return {
            TAG_RUN: self.run,
            TAG_ISSUE: str(self.issue),
            TAG_LAUNCH: self.launch,
            TAG_KIND: KIND,
            TAG_NONCE: self.nonce,
        }

    def owns(self, observed: Observation) -> bool:
        """Whether ``observed`` is this entry's resource: its name, every tag and the nonce."""
        return observed.name == self.name and dict(observed.tags) == self.tags()

    def render(self) -> bytes:
        data = {"schema": REMOTE_ENTRY_SCHEMA, **{k: getattr(self, k) for k in _ENTRY_TYPES}}
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()

    @classmethod
    def parse(cls, raw: bytes) -> RemoteIdentity:
        """An entry :meth:`render` wrote, read back strictly: exact keys, exact types."""
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"a remote journal entry does not parse ({exc})") from None
        if not isinstance(data, dict) or set(data) != {"schema", *_ENTRY_TYPES}:
            raise ValueError("a remote journal entry does not hold exactly its fields")
        if data["schema"] != REMOTE_ENTRY_SCHEMA or type(data["schema"]) is not int:
            raise ValueError(f"a remote journal entry of schema {data['schema']!r} is not read")
        for key, kind in _ENTRY_TYPES.items():
            if type(data[key]) is not kind:
                raise ValueError(f"a remote journal entry's {key} is not a {kind.__name__}")
        return cls(**{key: data[key] for key in _ENTRY_TYPES})


_ENTRY_TYPES: dict[str, type] = {
    "provider": str,
    "scope": str,
    "run": str,
    "issue": int,
    "phase": str,
    "launch": str,
    "nonce": str,
    "ceiling": float,
    "window": float,
}


# -- the fake provider ------------------------------------------------------------------


class RemoteGone(ConnectionError):
    """The resource has ended: its deadline, a destroy, or the provider."""


class RemoteExists(Exception):
    """A create named a resource its scope already holds."""


class AmbiguousRequest(ConnectionError):
    """A request whose outcome the transport lost: it may or may not have happened."""


@dataclass
class _Resource:
    tags: dict[str, str]
    deadline: float
    state: str = "running"
    files: dict[str, bytes] = field(default_factory=dict)


class FakeRemote:
    """A provider of remote execution sandboxes, in memory, with its own clock.

    It keeps ``max_session`` as a provider's longest session, ends a
    resource at its deadline (it stops, and its files are discarded, as a
    non-persistent sandbox's are), and never resumes one. Every call is
    recorded in ``calls``. Each chunk a transfer moves advances the clock by
    ``chunk_seconds``. The faults are attributes a test sets.
    """

    def __init__(self, *, scope: str = "team/project/iad1", now: float = 1_000_000.0) -> None:
        self.resources: dict[str, _Resource] = {}
        self.now = now
        self.reported_scope = scope
        self.max_session = 24 * 3600.0
        self.chunk_seconds = 0.0
        self.calls: list[tuple[str, ...]] = []
        # Faults.
        self.lose_create_response = False  # the create happens, its answer is lost
        self.fail_create_before = False  # the create is lost before it happens
        self.deadline_overshoot = 0.0  # a create or extend keeps the resource this much longer
        self.fault_path: str | None = None  # the stream faults apply to; None: every one
        self.cut_after: int | None = None  # a stream raises after this many bytes
        self.short_after: int | None = None  # a stream ends, silently, after this many
        self.trailing = b""  # a download yields these bytes past the file
        self.corrupt_uploads = False  # what is stored differs from what was sent
        self.destroys_ignored = 0  # destroys that return and leave the resource
        self.lookup_failures = 0  # lookups and listings that raise
        self.scope_after: list[str] = []  # the scope reported by later reads, in turn

    def clock(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self._expire()

    def _expire(self) -> None:
        for resource in self.resources.values():
            if resource.state != "stopped" and self.now >= resource.deadline:
                resource.state = "stopped"
                resource.files.clear()

    def _live(self, name: str) -> _Resource:
        self._expire()
        resource = self.resources.get(name)
        if resource is None or resource.state != "running":
            raise RemoteGone(f"{name} is not running")
        return resource

    def _observe(self, name: str, resource: _Resource) -> Observation:
        return Observation(name, dict(resource.tags), resource.state, resource.deadline)

    def scope(self) -> str:
        self.calls.append(("scope",))
        if self.scope_after:
            self.reported_scope = self.scope_after.pop(0)
        return self.reported_scope

    def create(self, name: str, tags: Mapping[str, str], deadline: float) -> None:
        self.calls.append(("create", name))
        self._expire()
        if self.fail_create_before:
            self.fail_create_before = False
            raise AmbiguousRequest("the connection broke before the provider answered")
        if name in self.resources:
            raise RemoteExists(name)
        if deadline - self.now > self.max_session:
            raise ValueError("the deadline is past the provider's longest session")
        if len(tags) > 5 or any(len(v) > MAX_TAG_VALUE for v in tags.values()):
            raise ValueError("the provider keeps five tags of at most 256 characters")
        self.resources[name] = _Resource(dict(tags), deadline + self.deadline_overshoot)
        if self.lose_create_response:
            self.lose_create_response = False
            raise AmbiguousRequest("the connection broke before the provider answered")

    def lookup(self, name: str) -> Observation | None:
        """A passive lookup: it never starts or resumes anything."""
        self.calls.append(("lookup", name))
        if self.lookup_failures:
            self.lookup_failures -= 1
            raise ConnectionError("the provider's API did not answer")
        self._expire()
        resource = self.resources.get(name)
        return None if resource is None else self._observe(name, resource)

    def list_tagged(self, key: str, value: str) -> list[Observation]:
        self.calls.append(("list", key, value))
        if self.lookup_failures:
            self.lookup_failures -= 1
            raise ConnectionError("the provider's API did not answer")
        self._expire()
        return [
            self._observe(name, r) for name, r in self.resources.items() if r.tags.get(key) == value
        ]

    def extend(self, name: str, deadline: float) -> None:
        self.calls.append(("extend", name))
        resource = self._live(name)
        if deadline - self.now > self.max_session:
            raise ValueError("the deadline is past the provider's longest session")
        resource.deadline = max(resource.deadline, deadline + self.deadline_overshoot)

    def _faulted(self, path: str) -> bool:
        return self.fault_path is None or self.fault_path == path

    def put(self, name: str, path: str, chunks: Iterable[bytes], size: int) -> None:
        self.calls.append(("put", name, path))
        cut = self.cut_after if self._faulted(path) else None
        received = bytearray()
        for chunk in chunks:
            self._live(name)
            if cut is not None and len(received) + len(chunk) > cut:
                raise ConnectionError("the upload's connection was reset")
            received += chunk
            self.advance(self.chunk_seconds)
        if len(received) != size:
            raise ValueError(f"the upload produced {len(received)} bytes, not {size}")
        data = bytes(received)
        if self.corrupt_uploads and data:
            data = bytes([data[0] ^ 0xFF]) + data[1:]
        self._live(name).files[path] = data

    def digest(self, name: str, path: str) -> str:
        """The remote's own sha256 of a stored file: a claim, read by a command inside."""
        self.calls.append(("digest", name, path))
        return hashlib.sha256(self._live(name).files[path]).hexdigest()

    def get(self, name: str, path: str) -> Iterator[bytes]:
        self.calls.append(("get", name, path))
        faulted = self._faulted(path)
        cut = self.cut_after if faulted else None
        short = self.short_after if faulted else None
        data = self._live(name).files[path] + (self.trailing if faulted else b"")
        sent = 0
        while sent < len(data):
            self._live(name)
            if short is not None and sent >= short:
                return
            step = CHUNK_BYTES if short is None else min(CHUNK_BYTES, short - sent)
            chunk = data[sent : sent + step]
            if cut is not None and sent + len(chunk) > cut:
                raise ConnectionError("the download's connection was reset")
            sent += len(chunk)
            self.advance(self.chunk_seconds)
            yield chunk

    def destroy(self, name: str) -> None:
        self.calls.append(("destroy", name))
        if name not in self.resources:
            return
        if self.destroys_ignored:
            self.destroys_ignored -= 1
            self.resources[name].state = "stopping"
            return
        del self.resources[name]

    # What a test plays on the remote side: the stager's and the exporter's work.
    def stored(self, name: str, path: str) -> bytes:
        return self.resources[name].files[path]

    def stored_files(self, name: str) -> dict[str, bytes]:
        return dict(self.resources[name].files)

    def store(self, name: str, path: str, data: bytes) -> None:
        self.resources[name].files[path] = data


# -- R2 and R9: what a create may hand the remote ---------------------------------------


@dataclass(frozen=True)
class CreateRequest:
    """What a create would configure that a credential could hide in.

    ``env`` names the variables the remote's commands get (their values are
    never read here); ``brokered`` maps each domain pattern for which the
    provider injects a credential into outgoing requests to the headers it
    sets; ``managed`` names credentials the provider would set up itself.
    """

    env: tuple[str, ...] = ()
    brokered: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    managed: tuple[str, ...] = ()


def _domain_matches(pattern: str, host: str) -> bool:
    """Whether a domain pattern matches ``host``, with whole-label wildcards.

    ``*`` matches every host; ``*.example.com`` every subdomain at any depth
    and not the apex; any other ``*`` label exactly one label.
    """
    pattern, host = pattern.lower().rstrip("."), host.lower().rstrip(".")
    if pattern == "*":
        return True
    if pattern.startswith("*."):
        return host.endswith(pattern[1:])
    p_labels, h_labels = pattern.split("."), host.split(".")
    return len(p_labels) == len(h_labels) and all(
        p in ("*", h) for p, h in zip(p_labels, h_labels, strict=True)
    )


def create_request_problem(request: CreateRequest, provider_credential_names: Iterable[str]) -> str:
    """Why a create must not be sent as asked (R2, R9), or "".

    The remote gets no GitHub or git credential route (the names
    :func:`autoforge.child_env.forbidden_env_entry` refuses for every
    agent), not the provider's own credential, no credential the provider
    would inject into a request to a GitHub host, and none the provider
    manages. Each problem names a variable, a domain or a credential kind,
    never a value.
    """
    provider_names = set(provider_credential_names)
    for name in request.env:
        forbidden = forbidden_env_entry(name)
        if forbidden is not None:
            return f"the remote's environment would pass {forbidden[0]}: {forbidden[1]}"
        if name in provider_names:
            return f"the remote's environment would pass {name}, the provider's own credential"
    for pattern in request.brokered:
        hosts = [host for host in GITHUB_HOSTS if _domain_matches(pattern, host)]
        if hosts:
            return (
                f"a credential brokered for {pattern!r} would reach {hosts[0]}: GitHub write "
                "authority belongs to the controller alone"
            )
    if request.managed:
        return (
            f"the provider would set up {request.managed[0]!r} credentials in the remote, "
            "which no record admits"
        )
    return ""


# -- R1, R3 and R7: create, renew -------------------------------------------------------


class DeadlineUnbounded(ExecutionError):
    """A renewal the provider holds past the deadline asked for: the launch ends (R3).

    The controller asked for at most one window more, never past the
    ceiling, and read back a later deadline: a controller that died now
    would leave a resource that outlives its window. The launch's result is
    not used, and the resource's entry stays open until a removal is
    verified (:func:`terminate`, or reconciliation). No local container
    renews a deadline, so ADR 0013 D17 has no class for this; a reopening
    types it under ADR 0011.
    """


def first_deadline(identity: RemoteIdentity, now: float) -> float:
    """The deadline a create asks for: one window ahead, never past the ceiling."""
    return min(now + identity.window, identity.ceiling)


def _deadline_problem(observed: Observation, asked: float) -> str:
    """Why the deadline the provider holds breaks the one asked for, or "".

    ``asked`` is at most one window ahead of when it was asked and never past
    the ceiling (:func:`first_deadline`, :func:`keep_deadline`), so a
    deadline read back later than it breaks both. One read back earlier is
    the provider's to choose: the transfers respect it.
    """
    if observed.deadline > asked:
        return (
            f"the provider holds {observed.name} until {observed.deadline:.0f}, "
            f"{observed.deadline - asked:.0f} s past the deadline of {asked:.0f} asked for, so "
            "a dead controller's resource would outlive its renewal window"
        )
    return ""


def create_remote(
    remote: FakeRemote,
    identity: RemoteIdentity,
    request: CreateRequest,
    *,
    write_entry: Callable[[bytes], None],
    provider_credential_names: Iterable[str] = (),
) -> Observation:
    """Create ``identity``'s resource, its journal entry first; return what was made.

    Before anything is written: the request hands the remote no credential
    it must not have, the provider's longest session holds the launch's
    ceiling, and the provider's credential reaches the entry's scope, read
    fresh. An ambiguous create (its answer lost) is resolved by a lookup by
    name before anything is re-issued (ADR 0004 D4.3's rule, applied to a
    remote object): the resource is the entry's, or there is none and the
    create is issued once more under the same name, or the name holds
    another resource and the launch is refused, naming it. Never two
    resources for one entry. The scope is read again after the create, as
    ADR 0013 D6 item 10 reads a store after its last create.
    """
    problem = create_request_problem(request, provider_credential_names)
    if problem:
        raise SandboxLaunchRefusedError(f"the remote launch is refused: {problem}")
    if identity.ceiling - remote.clock() > remote.max_session:
        raise SandboxLaunchRefusedError(
            "the remote launch is refused: its ceiling is past the provider's longest session "
            f"of {remote.max_session:.0f} s"
        )
    scope = remote.scope()
    if scope != identity.scope:
        raise ConfigurationError(
            f"the provider's credential reaches {scope}, not {identity.scope}, which the "
            "launch's entry names"
        )
    write_entry(identity.render())
    observed: Observation | None = None
    asked = 0.0
    for _ in range(2):
        # A re-issue asks for a later deadline than the first create did, so
        # the last one asked bounds whichever create made the resource.
        asked = first_deadline(identity, remote.clock())
        try:
            remote.create(identity.name, identity.tags(), asked)
        except (AmbiguousRequest, RemoteExists):
            pass
        except ValueError as exc:
            raise SandboxLaunchRefusedError(f"the provider refused the create ({exc})") from None
        observed = _lookup(remote, identity.name)
        if observed is not None:
            break
    if observed is None:
        raise SandboxLaunchRefusedError(
            f"the remote launch is refused: {identity.name} was not created; its entry stays "
            "for reconciliation"
        )
    if not identity.owns(observed):
        raise SandboxLaunchRefusedError(
            f"the remote launch is refused: {identity.name} holds a resource whose tags are "
            "not its journal entry's; nothing was removed"
        )
    problem = _deadline_problem(observed, asked)
    if problem:
        raise SandboxLaunchRefusedError(
            f"the remote launch is refused: {problem}. Its entry stays open until its removal "
            "is verified"
        )
    if remote.scope() != scope:
        raise SandboxLaunchRefusedError(
            "the remote launch is refused: the provider's scope changed during the create; "
            "its entry stays for reconciliation"
        )
    return observed


def keep_deadline(remote: FakeRemote, identity: RemoteIdentity, deadline: float) -> float:
    """Renew the resource's deadline while the controller runs (R3); return the new one.

    ``deadline`` is the one this last returned (or the create's). When less
    than half a window is left, ask for one window more, never past the
    ceiling, and read the deadline back: the provider's answer is what the
    transfers then respect. A deadline read back past the one asked for
    raises :class:`DeadlineUnbounded`, never clipped here: a clipped value
    would not shorten the resource's life. A controller that stops calling
    this leaves a resource the provider ends at the last deadline.
    """
    now = remote.clock()
    if deadline - now >= identity.window / 2:
        return deadline
    wanted = min(now + identity.window, identity.ceiling)
    if wanted > deadline:
        remote.extend(identity.name, wanted)
    observed = _lookup(remote, identity.name)
    if observed is None or not identity.owns(observed) or observed.state != "running":
        raise RemoteGone(f"{identity.name} is no longer running")
    problem = _deadline_problem(observed, max(deadline, wanted))
    if problem:
        raise DeadlineUnbounded(
            f"the remote launch is ended: {problem}. Its entry stays open until its removal "
            "is verified"
        )
    return observed.deadline


def _lookup(remote: FakeRemote, name: str) -> Observation | None:
    """A passive lookup, retried within :data:`LOOKUP_ATTEMPTS`; never "gone" on an error."""
    for attempt in range(LOOKUP_ATTEMPTS):
        try:
            return remote.lookup(name)
        except ConnectionError:
            if attempt == LOOKUP_ATTEMPTS - 1:
                raise
    raise AssertionError("unreachable")


# -- R4 and R5: the transfer ------------------------------------------------------------


def _time_problem(deadline: float, clock: Clock, what: str) -> str:
    left = deadline - clock()
    if left < TRANSFER_MARGIN_SECONDS:
        return (
            f"{what} would start {left:.0f} s before the resource's deadline, under the "
            f"{TRANSFER_MARGIN_SECONDS:.0f} s margin"
        )
    return ""


def _open_regular(dir_fd: int, name: str) -> tuple[int, int]:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    fd = os.open(name, flags, dir_fd=dir_fd)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise OSError(f"{name} is not a regular file")
    return fd, st.st_size


def _upload(
    read: Callable[[int], bytes], sent: Any, remote: FakeRemote, deadline: float
) -> Iterator[bytes]:
    """What ``read`` yields, in chunks, hashed into ``sent``; stops at the deadline."""
    while chunk := read(CHUNK_BYTES):
        if remote.clock() >= deadline:
            raise RemoteGone("the resource's deadline passed during the upload")
        sent.update(chunk)
        yield chunk


def _read_bounded(fd: int, bound: int) -> bytes:
    """The file behind ``fd``, read once; refused when it holds more than ``bound``."""
    data = bytearray()
    while chunk := os.read(fd, CHUNK_BYTES):
        data += chunk
        if len(data) > bound:
            raise OSError(f"it grew past its bound of {bound} bytes")
    return bytes(data)


def seed_pack_digest(manifest: bytes) -> str:
    """The sha256 of the pack a seed manifest names, or "" when it names none."""
    try:
        data = json.loads(manifest)
    except (ValueError, RecursionError):
        return ""
    value = data.get(SEED_PACK_DIGEST) if isinstance(data, dict) else None
    return value if isinstance(value, str) and _SHA256_RE.fullmatch(value) else ""


def _send(
    remote: FakeRemote,
    identity: RemoteIdentity,
    name: str,
    read: Callable[[int], bytes],
    size: int,
    deadline: float,
) -> str:
    """Upload one file of the seed, its size declared; return the sha256 of what was sent.

    The remote's own digest of what it stored must be that one.
    """
    sent = hashlib.sha256()
    try:
        remote.put(identity.name, name, _upload(read, sent, remote, deadline), size)
        stored = remote.digest(identity.name, name)
    except (ConnectionError, OSError, ValueError, KeyError) as exc:
        raise SandboxLaunchRefusedError(
            f"the seed's {name} did not reach the remote whole ({exc})"
        ) from None
    if stored != sent.hexdigest():
        raise SandboxLaunchRefusedError(f"the remote stored a {name} other than the one sent")
    return sent.hexdigest()


def send_seed(
    remote: FakeRemote,
    identity: RemoteIdentity,
    seed_dir: Path,
    git_input: LaunchGitInput,
    *,
    deadline: float,
    max_bytes: int = SEED_MAX_BYTES,
) -> None:
    """Copy the seed into the remote (R4), or refuse it as a setup failure.

    Before a byte leaves: the seed's manifest is the one the launch's git
    input binds (ADR 0013 D4's hand-off), the deadline leaves the margin,
    each file is a regular file within its bound, and the manifest names
    the pack by its sha256. The manifest is read once, and the bytes
    checked are the bytes sent. The pack is read once from its descriptor
    and hashed as it is sent, and it must be the pack the manifest names:
    the input binds the manifest's bytes, and through them the pack's. Each
    file goes with its size declared and is checked against the remote's
    digest of what it stored. An upload that errors, ends short, crosses
    the deadline, is stored otherwise or carries another pack refuses the
    launch, and no stager runs on it; the stager checks the same binding
    itself (:func:`stage_seed`). The prover's tree proof inside is still
    the only tree proof (ADR 0013 D2).
    """
    problem = seed_problem(str(seed_dir), git_input) or _time_problem(
        deadline, remote.clock, "the seed's upload"
    )
    if problem:
        raise SandboxLaunchRefusedError(f"the seed is not handed on: {problem}")
    bounds = {SEED_MANIFEST_NAME: min(max_bytes, MAX_SEED_MANIFEST_BYTES), SEED_PACK: max_bytes}
    dir_fd = os.open(seed_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    opened: dict[str, tuple[int, int]] = {}
    try:
        for name in SEED_FILES:
            try:
                opened[name] = _open_regular(dir_fd, name)
            except OSError as exc:
                raise SandboxLaunchRefusedError(f"the seed's {name} is refused ({exc})") from None
            size = opened[name][1]
            if size > bounds[name]:
                raise SandboxLaunchRefusedError(
                    f"the seed's {name} holds {size} bytes, over the bound of {bounds[name]}"
                )
        try:
            manifest = _read_bounded(opened[SEED_MANIFEST_NAME][0], bounds[SEED_MANIFEST_NAME])
        except OSError as exc:
            raise SandboxLaunchRefusedError(
                f"the seed's {SEED_MANIFEST_NAME} is refused ({exc})"
            ) from None
        if DIGEST_PREFIX + hashlib.sha256(manifest).hexdigest() != git_input.seed_manifest_digest:
            raise SandboxLaunchRefusedError(
                "the seed manifest read is not the one the launch's git input binds"
            )
        pack_digest = seed_pack_digest(manifest)
        if not pack_digest:
            raise SandboxLaunchRefusedError(
                f"the bound seed manifest names no pack by its {SEED_PACK_DIGEST}, so no pack "
                "is bound and none is sent"
            )
        _send(
            remote, identity, SEED_MANIFEST_NAME, io.BytesIO(manifest).read, len(manifest),
            deadline,
        )  # fmt: skip
        pack_fd, pack_size = opened[SEED_PACK]
        sent = _send(
            remote, identity, SEED_PACK, functools.partial(os.read, pack_fd), pack_size, deadline
        )
        if sent != pack_digest:
            raise SandboxLaunchRefusedError(
                "the seed pack sent is not the one its bound manifest names"
            )
    finally:
        for fd, _ in opened.values():
            os.close(fd)
        os.close(dir_fd)


def stage_seed(files: Mapping[str, bytes], seed_manifest_digest: str) -> bytes:
    """The stager's own check of the seed, on the remote side (R4); return the pack.

    Nothing runs inside a remote here, so test code plays the stager, and
    this is what it checks before it indexes a byte. ``files`` is what the
    remote holds. ``seed_manifest_digest`` comes with the stager's command,
    from the launch's git input, never from a file of the remote. The
    manifest must have that digest, and the pack the digest the manifest
    names; anything else is a setup failure, and nothing is staged. This
    guards the transfer and the remote's storage. A provider that alters
    what runs inside could alter this check too: ADR 0014 §3.3 trusts it
    for that, as it trusts the agent's execution.
    """
    manifest, pack = files.get(SEED_MANIFEST_NAME), files.get(SEED_PACK)
    if manifest is None or pack is None:
        raise SandboxLaunchRefusedError("the stager refuses the seed: it is not whole")
    found = DIGEST_PREFIX + hashlib.sha256(manifest).hexdigest()
    if not seed_manifest_digest or found != seed_manifest_digest:
        raise SandboxLaunchRefusedError(
            "the stager refuses the seed: its manifest is not the one the launch's git input binds"
        )
    expected = seed_pack_digest(manifest)
    if not expected or hashlib.sha256(pack).hexdigest() != expected:
        raise SandboxLaunchRefusedError(
            "the stager refuses the seed: its pack is not the one its bound manifest names"
        )
    return pack


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte of ``data`` to ``fd``, or raise.

    A short write is resumed where it stopped; one that writes nothing is
    refused, so a byte counted as received is a byte on disk.
    """
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(f"a write stopped with {len(view)} bytes of the chunk unwritten")
        view = view[written:]


def _download(
    remote: FakeRemote,
    identity: RemoteIdentity,
    path: str,
    dir_fd: int,
    limit: int,
    deadline: float,
    written: list[str],
    *,
    keep: bool = False,
) -> tuple[int, str, bytes]:
    """Stream ``path`` of the resource into ``dir_fd`` within ``limit`` bytes.

    Returns its size, its sha256 and, with ``keep`` (the manifest, which its
    bound keeps small), its bytes: each counted only once it is written
    whole. Reads at most one chunk past ``limit``, so a stream that runs
    long is refused without being read whole.
    """
    problem = _time_problem(deadline, remote.clock, f"the download of {path}")
    if problem:
        raise ObjectImportError(f"the artifact is refused: {problem}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path, flags, 0o600, dir_fd=dir_fd)
    written.append(path)
    digest = hashlib.sha256()
    kept = bytearray()
    total = 0
    try:
        with contextlib.closing(remote.get(identity.name, path)) as stream:
            for chunk in stream:
                if remote.clock() >= deadline:
                    raise ObjectImportError(
                        f"the artifact is refused: the resource's deadline passed while {path} "
                        "was read"
                    )
                total += len(chunk)
                if total > limit:
                    raise ObjectImportError(
                        f"the artifact is refused: {path} runs past {limit} bytes"
                    )
                _write_all(fd, chunk)
                digest.update(chunk)
                if keep:
                    kept += chunk
    except (ConnectionError, OSError, KeyError) as exc:
        raise ObjectImportError(
            f"the artifact is refused: the download of {path} was interrupted ({exc})"
        ) from None
    finally:
        os.close(fd)
    return total, digest.hexdigest(), bytes(kept)


def receive_artifact(
    remote: FakeRemote,
    identity: RemoteIdentity,
    export_dir: Path,
    *,
    deadline: float,
    limits: ArtifactLimits = EXPORT_BOUND,
) -> CandidateManifest:
    """Bring the artifact back into the launch's empty export directory (R4, R5).

    The manifest first, within its bound, parsed strictly; then the pack,
    within the export bound and the size the manifest claims, hashed as it
    is read. A claim over the bound is refused before the pack is read. A
    stream that errors, ends short, runs long, differs from the claim or
    crosses the deadline, and a local write that fails or stops, is a
    candidate refusal, and every file this wrote is deleted: a partial
    artifact is never left for an import. What comes back is still
    untrusted: the strict import checks it again.
    """
    dir_fd = os.open(export_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    written: list[str] = []
    try:
        if os.listdir(dir_fd):
            raise ObjectImportError("the artifact is refused: the export directory is not empty")
        try:
            _, _, raw = _download(
                remote, identity, ARTIFACT_MANIFEST, dir_fd, limits.max_manifest_bytes,
                deadline, written, keep=True,
            )  # fmt: skip
            manifest = parse_manifest(raw)
            if manifest.pack_bytes > limits.max_pack_bytes:
                raise ObjectImportError(
                    f"the artifact is refused: its manifest claims a pack of "
                    f"{manifest.pack_bytes} bytes, over the bound of {limits.max_pack_bytes}"
                )
            size, sha, _ = _download(
                remote, identity, ARTIFACT_PACK, dir_fd, manifest.pack_bytes, deadline, written
            )
            if size != manifest.pack_bytes or sha != manifest.pack_sha256:
                raise ObjectImportError(
                    "the artifact is refused: the pack received is not the one its manifest "
                    "describes"
                )
        except BaseException:
            for name in written:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=dir_fd)
            raise
    finally:
        os.close(dir_fd)
    return manifest


# -- R6 and R8: termination and reconciliation ------------------------------------------


def _same_scope(remote: FakeRemote, identity: RemoteIdentity) -> bool:
    try:
        return remote.scope() == identity.scope
    except ConnectionError:
        return False


def terminate(remote: FakeRemote, identity: RemoteIdentity) -> str:
    """Destroy the entry's resource and verify it gone (R6); return "removed" or "gone".

    The scope is read fresh before and after, as ADR 0013 D6 item 10 reads
    a store. A resource whose tags are not the entry's is never destroyed.
    A destroy's own answer proves nothing: only a passive lookup that finds
    nothing does, within :data:`REMOVAL_ATTEMPTS`. Otherwise the entry stays
    open for reconciliation, and the error says so.
    """
    if not _same_scope(remote, identity):
        raise SandboxCleanupUncertainError(
            f"the provider's credential no longer reaches {identity.scope}; "
            f"{identity.name}'s entry stays open",
            objects=(f"remote {identity.name}",),
        )
    observed = _lookup(remote, identity.name)
    if observed is None:
        outcome = "gone"
    elif not identity.owns(observed):
        raise SandboxCleanupUncertainError(
            f"{identity.name} holds a resource whose tags are not its entry's; it was not removed",
            objects=(f"remote {identity.name}",),
        )
    else:
        outcome = ""
        for _ in range(REMOVAL_ATTEMPTS):
            remote.destroy(identity.name)
            if _lookup(remote, identity.name) is None:
                outcome = "removed"
                break
        if not outcome:
            raise SandboxCleanupUncertainError(
                f"{identity.name} is still listed after {REMOVAL_ATTEMPTS} destroys; its "
                "entry stays open",
                objects=(f"remote {identity.name}",),
            )
    if not _same_scope(remote, identity):
        raise SandboxCleanupUncertainError(
            f"the provider's scope changed during the removal of {identity.name}; its entry "
            "stays open",
            objects=(f"remote {identity.name}",),
        )
    return outcome


@dataclass(frozen=True)
class Reconciliation:
    """What reconciliation did: the entries it may close, and what blocks the run."""

    closed: tuple[str, ...]
    blocked: tuple[str, ...]


def reconcile(remote: FakeRemote, run: str, entries: Iterable[RemoteIdentity]) -> Reconciliation:
    """Remove what a dead launch of ``run`` left (R8): remove-only, never an import.

    Every entry's scope must be the one the controller's credential reaches
    now, or nothing is listed or removed and the command is refused: a
    lookup in another scope finds nothing, which is not "gone". Each entry's
    resource is then terminated, or found gone. A resource with the run's
    tag that no entry names, or an entry whose name holds a resource with
    other tags, blocks the run, named, and is never removed. A listing that
    keeps failing blocks too. No file of a resource is read, so nothing is
    exported or imported, and a crashed launch's work is lost (ADR 0013 D4).
    The scope is read again at the end, and nothing is closed unless it is
    unchanged.
    """
    entries = list(entries)
    scope = remote.scope()
    for entry in entries:
        if entry.run != run:
            raise ValueError(f"the remote entry {entry.name} is run {entry.run}'s, not {run}'s")
        if entry.scope != scope:
            raise ConfigurationError(
                f"the remote entry {entry.name} names {entry.scope}, and the provider's "
                f"credential reaches {scope}: nothing is listed or removed"
            )
    closed: list[str] = []
    blocked: list[str] = []
    for entry in entries:
        try:
            terminate(remote, entry)
        except SandboxCleanupUncertainError as exc:
            blocked.append(f"{entry.name}: {exc}")
        except ConnectionError as exc:
            blocked.append(f"{entry.name}: the provider did not answer ({exc})")
        else:
            closed.append(entry.name)
    known = {entry.name for entry in entries}
    try:
        listed = _list(remote, run)
    except ConnectionError as exc:
        blocked.append(f"the run's resources could not be listed ({exc})")
    else:
        blocked.extend(
            f"{o.name}: carries the run's tag and no entry names it; it was not removed"
            for o in listed
            if o.name not in known
        )
    if remote.scope() != scope:
        return Reconciliation((), (*blocked, "the provider's scope changed: nothing is closed"))
    return Reconciliation(tuple(closed), tuple(blocked))


def _list(remote: FakeRemote, run: str) -> list[Observation]:
    for attempt in range(LOOKUP_ATTEMPTS):
        try:
            return remote.list_tagged(TAG_RUN, run)
        except ConnectionError:
            if attempt == LOOKUP_ATTEMPTS - 1:
                raise
    raise AssertionError("unreachable")
