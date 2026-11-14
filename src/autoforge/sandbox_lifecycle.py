"""The engine's sequence of a sandbox backend's lifecycle (ADR 0013 D13, D16; #268).

:mod:`autoforge.sandbox_backend` says what a backend can do; this module
says in which order the engine asks, and what it journals first. It is
the engine layer's half of the seam: the backend never orders its own
steps, never writes the journal and never decides what is kept.

**At a command's entry** (:func:`enter`), before anything of a launch, in
D16's order: the runtime check (read-only, never cached; a refusal, a
container runtime that is not rootless included (ADR 0013 D10), leaves
everything as it was and reconciliation is not attempted), then
reconciliation (removal intents first, an entry closed only once its
object is verified gone), then the probe, which the caller caches for the
runtime report it was proved on. The pin check comes before all three and
is the engine's own (#280).

**One launch** (:class:`LaunchLifecycle`), in D13's order. The caller's
launch (:meth:`LaunchLifecycle.launch`) is the provider's, whose plan the
engine bound to the lifecycle (``SandboxPlan``):

1. *plan*: the provider completes the engine's facts with its declaration
   and the backend compiles them, pure; a policy refusal raises there, and
   nothing of the launch exists yet. The plan then hands the compiled
   launch to :meth:`LaunchLifecycle.establish`, which runs
2. *seed*: before any journal entry, so a failure there leaves nothing to
   reconcile. The backend builds the seed from the launch's git input
   (:class:`~autoforge.git_input.LaunchGitInput`, which the engine bound
   before the launch and which names the revision); the step's end binds
   the seed's manifest digest into that input, once, and hands the
   completed input to the engine to save (``on_seed``), still before any
   journal entry (ADR 0013 D4, D19). From then on the seed is handed on
   only while its manifest still has that digest: checked before the
   create step (the bind form writes the staging root there) and again
   before the stage step (the copy-in form copies it there), so a seed
   swapped after the input was bound never reaches a container;
3. *create*: the objects checked against D13's rules, then one journal
   write that names them all, then each made in :data:`CREATE_ORDER`
   (every bind source before any container, the volume before the stager
   container, that before the launch container);
4. *stage*: inside the stager container only; its removal journaled, then
   verified, before the launch container starts, which never happens after
   a failed stage. :meth:`~LaunchLifecycle.establish` returns the
   :class:`~autoforge.sandbox_backend.LaunchHandle` of what was made, which
   the compiled launch carries to the executor;
5. *run*: the executor prepares the command in that handle's environment,
   by the backend that planned it, and closes its prepared launch on every
   path; then
6. *the release*, right after the launch on every path, ``Ctrl-C`` and an
   exception included: what holds the credential is removed, and the
   volume too when there is nothing to export;
7. *export*, for a change-producing launch: the exporter's own entry before
   its create, its removal journaled after. Its export directory must be
   the one the engine chose for the launch (``LaunchInputs.export_dir``),
   and the outcome names that journaled object (``Exported.origin``): the
   verify step reads an artifact from nowhere else (#269). A launch the
   engine handed an export directory is never skipped: a backend that
   makes it no exporter is a failed export, refused and saved by verify
   before the final clean-up (:attr:`LaunchLifecycle.exports`);
8. *verify* is the caller's (#269, :mod:`autoforge.candidate_source`), a
   failed export included; once its outcome is saved,
   :meth:`LaunchLifecycle.saved` runs. A launch that an exception ended
   may still be exported, by the caller, once
   :attr:`LaunchLifecycle.released_safely` holds (the engine does so for
   an ``ExecutionError`` after the run, never for ``Ctrl-C``)
9. *the final clean-up*: the volume and the export directory. Never before
   that outcome is saved: a launch that ends between its run and that save
   keeps its volume for reconciliation.

Every removal is journaled first, and its entry is closed only after the
backend verified it. Each goes in :data:`REMOVAL_ORDER`: every container
before any object it mounts, since a runtime refuses to remove a volume a
container still references, stopped or not. A volume that one of the
launch's containers may still hold is not removed at all, and is uncertain
too. A removal that cannot be verified is
:attr:`~autoforge.sandbox_backend.Removal.UNCERTAIN`: its entry is left open
for reconciliation, it is never reported as done, and it is surfaced as
:class:`~autoforge.errors.SandboxCleanupUncertainError` where it happened,
so the caller never reads it as an ordinary launch: after the release, the
launch's result is not returned and nothing is exported (the launch
container may still write the volume); after the export, the artifact is
not returned and never verified (the exporter may still write it); after
the final clean-up, the step fails. When another exception is already
ending the launch, that one propagates, with a note naming them. A failure
of a step up to the run refuses the launch
(:class:`~autoforge.errors.SandboxLaunchRefusedError`), and both parts of
the clean-up then remove what was made; nothing is ever run without the
sandbox instead.

For bwrap every step but the plan and the run does nothing, writes nothing
and makes nothing: its launches are what they were.

The journal is :class:`LifecycleJournal`. Its file, format, entries,
runtime binding and the reconciliation logic are #272's; until it lands,
the engine's journal is :data:`NO_JOURNAL`, which holds no entry and
refuses every write, so a backend that makes an object is refused before
it makes one.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, TypeVar

from .errors import SandboxCleanupUncertainError, SandboxLaunchRefusedError
from .git_input import GitInputError, LaunchGitInput, seed_manifest_digest, seed_problem
from .sandbox import LaunchDescription
from .sandbox_backend import (
    CONTAINER_KINDS,
    CONTAINER_NETWORKS,
    CREATE_ORDER,
    EXPORT_KINDS,
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
    SandboxBackend,
    Seed,
    runtime_refusal,
)

T = TypeVar("T")


class LifecycleJournal(Protocol):
    """Where the engine records a backend's objects before it acts on them (ADR 0013 D6).

    #272 owns its file (beside ``state.json``, never part of it), its format
    and its runtime binding; this is what the sequence needs of it.
    """

    def open_entries(self) -> tuple[LifecycleObject, ...]:
        """Every entry not yet closed."""
        ...

    def record_creates(self, runtime: RuntimeReport, objects: tuple[LifecycleObject, ...]) -> None:
        """One atomic write of every entry, bound to ``runtime``, before any is made."""
        ...

    def record_removals(self, objects: tuple[LifecycleObject, ...]) -> None:
        """The intent to remove each, before any removal."""
        ...

    def close_entries(self, objects: tuple[LifecycleObject, ...]) -> None:
        """Close each entry, after its object's removal was verified."""
        ...


class _NoJournal:
    """No lifecycle journal exists yet (#272): it holds no entry and records none.

    A backend that makes no object (bwrap) never writes to it. One that
    would is refused here, before anything is made, rather than making an
    object no journal names.
    """

    def open_entries(self) -> tuple[LifecycleObject, ...]:
        return ()

    def _refuse(self, objects: tuple[LifecycleObject, ...]) -> SandboxLaunchRefusedError:
        names = ", ".join(f"{obj.kind.value} {obj.name}" for obj in objects)
        return SandboxLaunchRefusedError(
            f"the launch would make {names}, and no lifecycle journal exists to record it "
            "first (#272), so nothing was made"
        )

    def record_creates(self, runtime: RuntimeReport, objects: tuple[LifecycleObject, ...]) -> None:
        raise self._refuse(objects)

    def record_removals(self, objects: tuple[LifecycleObject, ...]) -> None:
        raise self._refuse(objects)

    def close_entries(self, objects: tuple[LifecycleObject, ...]) -> None:
        raise self._refuse(objects)


NO_JOURNAL: LifecycleJournal = _NoJournal()


# -- the verify step (D13; #269) ----------------------------------------------------


@dataclass(frozen=True)
class Exported:
    """The export step's outcome: the artifact, untrusted, or why the export failed.

    ``origin`` is the export directory the lifecycle journaled for the
    launch before its exporter ran (``None`` only for an outcome no
    lifecycle made): the verify step accepts an artifact only from it (#269).
    """

    artifact: Artifact | None = None
    failure: str = ""
    origin: LifecycleObject | None = None


@dataclass(frozen=True)
class Verified:
    """The verify step's outcome: the candidate it imported, or why it refused the artifact.

    ``candidate`` is the commit the controller imported from the artifact;
    ``refusal`` the reason there is none. An outcome is a candidate only
    with a commit and no refusal (:attr:`accepted`).
    """

    candidate: str = ""
    refusal: str = ""

    @property
    def accepted(self) -> bool:
        return bool(self.candidate) and not self.refusal

    @property
    def problem(self) -> str:
        """Why there is no candidate, as the candidate read refuses it; "" when accepted."""
        if self.accepted:
            return ""
        return self.refusal or "the verify step named no candidate"


class CandidateSource(Protocol):
    """The controller's verify step for an exported artifact, and its save (D13, D19; #269).

    The engine calls :meth:`verify` with the export step's outcome (its
    artifact untrusted, or why the export failed) and the launch's handle,
    then :meth:`save` with the outcome, and only after :meth:`save` returned
    does the final clean-up remove the volume. #269's candidate source
    imports strictly and binds the outcome, a refusal included, in state.
    """

    def verify(self, exported: Exported, handle: LaunchHandle) -> Verified:
        """The artifact checked and imported, or refused; never trusted as it is.

        A failed export is a refusal (D17).
        """
        ...

    def save(self, outcome: Verified) -> None:
        """Make ``outcome`` durable before the volume it was read from is removed."""
        ...


class _NoCandidateSource:
    """A verify step that refuses every artifact: for a caller with no candidate source.

    An exported volume is never read as a candidate through it; a refusal
    leaves nothing to import and so needs nothing of the volume kept, and
    there is no binding to save. The engine's own is
    :class:`autoforge.candidate_source.ArtifactCandidateSource` (#269).
    """

    def verify(self, exported: Exported, handle: LaunchHandle) -> Verified:
        return Verified(
            refusal=(
                "no candidate source verifies this launch's exported artifact, so its "
                "export is refused, never read as its candidate"
            )
        )

    def save(self, outcome: Verified) -> None:
        return None


NO_CANDIDATE_SOURCE: CandidateSource = _NoCandidateSource()


# -- at a command's entry (D16) ---------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """What a command's entry proved: the runtime, fresh, and the probe for it."""

    runtime: RuntimeReport
    probe: ProbeResult
    # The entries reconciliation could not prove gone, still open.
    still_open: tuple[LifecycleObject, ...] = ()


def enter(
    backend: SandboxBackend,
    journal: LifecycleJournal,
    probe: Callable[[RuntimeReport], ProbeResult],
) -> Entry:
    """The runtime check, reconciliation, then the probe, before any launch (D16).

    A runtime check that refuses raises with nothing written and nothing
    reconciled: a listing from another runtime or store would read as
    "nothing left". ``probe`` is called last, with the report the check
    proved, so that the caller can reuse a result proved on that report.
    A container runtime that is not rootless is refused the same way, as
    the runtime check's own refusal (ADR 0013 D10).
    """
    runtime = backend.runtime_check()
    refusal = runtime_refusal(runtime)
    if refusal is not None:
        raise refusal
    still_open = reconcile(backend, journal, runtime)
    return Entry(runtime, probe(runtime), still_open)


def reconcile(
    backend: SandboxBackend, journal: LifecycleJournal, runtime: RuntimeReport
) -> tuple[LifecycleObject, ...]:
    """Remove the journal's open entries through ``backend``; return those still open.

    Remove-only, intents first; an entry closes only when the backend
    verified its object gone. The backend gets the entries in
    :data:`REMOVAL_ORDER`, each container before what it mounts. What
    ownership it cannot prove, and the ``BLOCKED`` that follows, is #272's.
    """
    entries = _removal_order(journal.open_entries())
    if entries:
        journal.record_removals(entries)
    gone = backend.reconcile(entries, runtime)
    stray = [obj for obj in gone if obj not in entries]
    if stray:
        raise SandboxLaunchRefusedError(
            f"reconciliation reported {stray[0].kind.value} {stray[0].name} removed, which "
            "the journal does not name; no entry was closed"
        )
    if gone:
        journal.close_entries(tuple(gone))
    return tuple(obj for obj in entries if obj not in gone)


# -- one launch (D13) ---------------------------------------------------------------


def _refused(text: str) -> SandboxLaunchRefusedError:
    return SandboxLaunchRefusedError(f"the sandbox's lifecycle refuses the launch: {text}")


def _check_create(objects: tuple[LifecycleObject, ...], owner: LaunchOwner) -> None:
    """The create step's objects against D13's rules, before anything is journaled."""
    _check_owned(objects, owner, CREATE_ORDER)
    by_kind = {obj.kind: obj for obj in objects}
    stager = by_kind.get(ObjectKind.STAGER_CONTAINER)
    launch = by_kind.get(ObjectKind.LAUNCH_CONTAINER)
    projection = by_kind.get(ObjectKind.PROJECTION)
    if (stager is None) != (launch is None):
        raise _refused(
            "a launch container is staged only by a stager container of its own, and a "
            "stager container serves only a launch container"
        )
    if stager is not None and launch is not None:
        if stager.network != NETWORK_NONE:
            raise _refused(f"the stager container {stager.name} is not created offline")
        if projection is not None and projection.name in stager.mounts:
            raise _refused(f"the stager container {stager.name} would mount the projection")
        if projection is not None and projection.name not in launch.mounts:
            raise _refused(f"the launch container {launch.name} does not mount the projection")
        if not launch.network:
            raise _refused(f"the launch container {launch.name} names no network position")
        if launch.network not in CONTAINER_NETWORKS:
            raise _refused(
                f"the launch container {launch.name} would be created with network "
                f"{launch.network!r}; a container gets a namespace of its own, never the host's "
                "(ADR 0013 D9)"
            )
    sources = {obj.name for obj in objects if obj.kind not in CONTAINER_KINDS}
    _check_mounts(objects, sources)


def _check_export(
    objects: tuple[LifecycleObject, ...], owner: LaunchOwner, sources: set[str], export_dir: str
) -> LifecycleObject:
    """The export step's objects: one exporter, offline, mounting no projection, and
    one export directory, the one the engine chose for the launch; that one is returned."""
    _check_owned(objects, owner, EXPORT_KINDS)
    exporters = [obj for obj in objects if obj.kind is ObjectKind.EXPORTER]
    if len(exporters) != 1:
        raise _refused("an export runs in exactly one exporter container")
    (exporter,) = exporters
    if exporter.network != NETWORK_NONE:
        raise _refused(f"the exporter {exporter.name} is not created offline")
    directories = [obj for obj in objects if obj.kind is ObjectKind.EXPORT_DIR]
    if not export_dir:
        raise _refused("the launch names no export directory, so nothing is exported into one")
    if len(directories) != 1 or directories[0].name != export_dir:
        named = directories[0].name if len(directories) == 1 else f"{len(directories)} of them"
        raise _refused(
            f"an export fills exactly the export directory the controller chose for the launch "
            f"({export_dir}), not {named}"
        )
    _check_mounts(
        objects, sources | {obj.name for obj in objects if obj.kind not in CONTAINER_KINDS}
    )
    return directories[0]


def _check_owned(
    objects: tuple[LifecycleObject, ...], owner: LaunchOwner, kinds: tuple[ObjectKind, ...]
) -> None:
    seen: set[ObjectKind] = set()
    names: set[str] = set()
    nonces: set[str] = set()
    for obj in objects:
        if obj.owner != owner:
            raise _refused(f"{obj.kind.value} {obj.name} names another launch as its owner")
        if obj.kind not in kinds:
            raise _refused(f"a {obj.kind.value} is not made at this step")
        if obj.kind in seen:
            raise _refused(f"a launch makes one {obj.kind.value}, not two")
        if not obj.name or not obj.nonce or obj.name in names or obj.nonce in nonces:
            raise _refused(f"{obj.kind.value} {obj.name!r} has no name and nonce of its own")
        seen.add(obj.kind)
        names.add(obj.name)
        nonces.add(obj.nonce)


def _check_mounts(objects: tuple[LifecycleObject, ...], sources: set[str]) -> None:
    for obj in objects:
        if obj.kind not in CONTAINER_KINDS:
            if obj.network or obj.mounts:
                raise _refused(f"a {obj.kind.value} has no network and mounts nothing")
            continue
        unknown = [name for name in obj.mounts if name not in sources]
        if unknown:
            raise _refused(
                f"{obj.kind.value} {obj.name} would mount {unknown[0]}, which no entry of "
                "the launch names"
            )


def _in_order(objects: Iterable[LifecycleObject]) -> tuple[LifecycleObject, ...]:
    return tuple(sorted(objects, key=lambda obj: CREATE_ORDER.index(obj.kind)))


# The order every removal follows, reconciliation's included: each container
# before any object it mounts. A runtime refuses to remove a volume that a
# container still references, even a stopped one, so a container goes first.
# Then the credential projection, the first of the rest, so the credential
# leaves the host first; then the staging root, the volume and the export
# directory: the release's order in ADR 0013 D6 item 5.
REMOVAL_ORDER = (
    ObjectKind.EXPORTER,
    ObjectKind.LAUNCH_CONTAINER,
    ObjectKind.STAGER_CONTAINER,
    ObjectKind.PROJECTION,
    ObjectKind.STAGING_ROOT,
    ObjectKind.VOLUME,
    ObjectKind.EXPORT_DIR,
)


def _removal_order(objects: Iterable[LifecycleObject]) -> tuple[LifecycleObject, ...]:
    return tuple(sorted(objects, key=lambda obj: REMOVAL_ORDER.index(obj.kind)))


def _named(objects: Iterable[LifecycleObject]) -> tuple[str, ...]:
    return tuple(f"{obj.kind.value} {obj.name}" for obj in objects)


class LaunchLifecycle:
    """One launch's steps, in D13's order, journaled; a context manager.

    ``with LaunchLifecycle(...) as lifecycle:``, the engine binds the
    provider's plan to it (``SandboxPlan(facts, backend, lifecycle)``), then
    :meth:`launch` with the provider's launch, whose plan calls
    :meth:`establish` once it compiled; then :meth:`export` and, for an
    artifact, once the verify step's outcome is saved, :meth:`saved`.
    Leaving the ``with`` block on any path runs the release if the launch
    did not, and the final clean-up when it is allowed; the original
    exception, ``Ctrl-C`` included, is the one that propagates. A step
    called out of order is refused. An uncertain removal is surfaced as
    :class:`~autoforge.errors.SandboxCleanupUncertainError` (module
    docstring).
    """

    def __init__(
        self,
        backend: SandboxBackend,
        journal: LifecycleJournal,
        inputs: LaunchInputs,
        runtime: RuntimeReport,
        *,
        change_producing: bool,
        on_seed: Callable[[LaunchGitInput], None] | None = None,
    ) -> None:
        self._backend = backend
        # Saves the launch's completed git input at the end of its seed step,
        # before any journal entry (ADR 0013 D19); never for a seedless launch.
        self._on_seed = on_seed
        self._journal = journal
        self._inputs = inputs
        self._owner = inputs.owner
        self._runtime = runtime
        self._change_producing = change_producing
        self._step = "start"
        # True only while the provider's launch runs (:meth:`launch`).
        self._launching = False
        self._seed: Seed | None = None
        self._open: list[LifecycleObject] = []
        # The environment: "" not yet, "making", "ready" (the launch
        # container started, so the command may have run), or "failed".
        self._environment = ""
        self._planned: LaunchDescription | None = None
        self._handle: LaunchHandle | None = None
        self._released = False
        self._export_skipped = False
        self._saved = False
        self._finished = False
        # Every object whose removal could not be verified: its entry stays
        # open for reconciliation (ADR 0013 D17).
        self.uncertain: list[LifecycleObject] = []
        # Those already surfaced to the caller, by an error or a note.
        self._surfaced: list[LifecycleObject] = []

    # -- the context ------------------------------------------------------------
    def __enter__(self) -> LaunchLifecycle:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            self._release()
            if self._may_finish():
                self._finish()
        finally:
            if self._seed is not None:
                seed, self._seed = self._seed, None
                try:
                    self._backend.discard_seed(seed)
                except Exception:  # noqa: BLE001 - a leftover temporary directory only
                    pass
        pending = self._unsurfaced()
        if not pending:
            return
        if exc is None:
            self._surface(pending, "the launch's clean-up")
        else:
            # Never masked: the exception that ends the launch propagates.
            self._surfaced.extend(pending)
            exc.add_note(_uncertain_text(pending, "the launch's clean-up"))

    @property
    def open_objects(self) -> tuple[LifecycleObject, ...]:
        """The launch's objects whose entries are still open."""
        return tuple(self._open)

    @property
    def handle(self) -> LaunchHandle | None:
        """What :meth:`establish` made for the launch; ``None`` before."""
        return self._handle

    @property
    def released_safely(self) -> bool:
        """Whether the launch's run ended and its release verified every removal.

        Only then may the export step run after a launch that ended by an
        exception (#269): nothing of the launch but its volume is left, and
        no launch container that could still write the volume may exist.
        """
        return (
            self._step == "launch"
            and not self._launching
            and self._released
            and not (self.uncertain)
        )

    # -- the steps --------------------------------------------------------------
    def _advance(self, step: str, after: str) -> None:
        if self._step != after:
            raise _refused(f"the {step} step comes after the {after} step, not after {self._step}")
        self._step = step

    def launch(self, run: Callable[[], T]) -> T:
        """The provider's launch: its plan, the environment it establishes, the run step.

        The release follows it on every path. A release that cannot verify
        what it removed is :class:`~autoforge.errors.SandboxCleanupUncertainError`
        in place of the launch's result.
        """
        self._advance("launch", "start")
        self._launching = True
        try:
            result = run()
        finally:
            self._launching = False
            self._release()
        self._raise_if_uncertain("the release after the run step")
        return result

    def establish(self, launch: LaunchDescription) -> LaunchHandle:
        """The seed, create and stage steps for the compiled ``launch``; their handle.

        Called by the plan the engine bound to this lifecycle, once it
        compiled the launch: so after the plan step, and never for a launch
        the policy refused. Only during :meth:`launch`. A later process of
        the same launch (Pi's auth check, then Pi) is planned too and runs in
        the environment already made, which must serve its network position;
        how a container's launch serves a second process after the first
        one's ``close()`` is #271's to decide. An environment that failed is
        never made again.
        """
        if not self._launching:
            raise _refused(f"a launch's environment is made during its launch, not {self._step}")
        if self._environment == "ready":
            assert self._handle is not None and self._planned is not None
            if launch.network != self._planned.network:
                raise _refused(
                    f"a process of the launch needs the {launch.network.value} network, and "
                    f"its environment was created with {self._planned.network.value}"
                )
            return self._handle
        if self._environment:
            raise _refused("its environment could not be made, and is not made again")
        self._environment = "making"
        self._planned = launch
        try:
            seed = self._seed = self._backend.seed(self._inputs, launch)
            self._bind(seed)
            self._check_seed(seed, "to the create step")
            self._create(launch, seed)
            self._stage(seed)
        except BaseException:
            self._environment = "failed"
            raise
        self._environment = "ready"
        self._handle = LaunchHandle(self._inputs, seed, tuple(self._open))
        return self._handle

    def _bind(self, seed: Seed) -> None:
        """The seed step's end: the git input completed with the seed's manifest digest.

        A backend that seeds nothing (:data:`NO_SEED`, bwrap) binds nothing
        and saves nothing. Otherwise the launch must carry a git input, and
        the seed's manifest, read again from the seed's directory, must hash
        to the identity the seed step names; the completed input replaces
        the bare one for every later step, and ``on_seed`` saves it, all
        before any journal entry.
        """
        if seed == NO_SEED:
            return
        git_input = self._inputs.git_input
        if git_input is None:
            raise _refused(
                "its backend built a seed, and the launch carries no git input that binds one"
            )
        try:
            digest = seed_manifest_digest(seed.directory)
        except OSError as exc:
            raise _refused(f"its seed's manifest cannot be read ({exc})") from exc
        if digest != seed.identity:
            raise _refused(
                f"its seed step names the manifest {seed.identity!r}, and the manifest it wrote "
                f"is {digest}"
            )
        try:
            completed = git_input.with_seed_manifest(digest)
        except GitInputError as exc:
            raise _refused(str(exc)) from exc
        self._inputs = dataclasses.replace(self._inputs, git_input=completed)
        if self._on_seed is not None:
            self._on_seed(completed)

    def _check_seed(self, seed: Seed, where: str) -> None:
        """The seed is handed on only while its manifest is the one the git input binds (D4)."""
        if seed == NO_SEED:
            return
        problem = seed_problem(seed.directory, self._inputs.git_input)
        if problem:
            raise _refused(f"its seed is not handed on {where}: {problem}")

    def _create(self, launch: LaunchDescription, seed: Seed) -> None:
        """One journal write for every object, then each made in D13's order."""
        objects = tuple(self._backend.objects(self._inputs, seed, launch))
        _check_create(objects, self._owner)
        objects = _in_order(objects)
        if not objects:
            return
        self._journal.record_creates(self._runtime, objects)
        self._open = list(objects)
        for obj in objects:
            self._backend.make(obj, seed, launch)

    def _stage(self, seed: Seed) -> None:
        """The stager container's work, its journaled removal, then the launch container's start."""
        stager = self._find(ObjectKind.STAGER_CONTAINER)
        launch = self._find(ObjectKind.LAUNCH_CONTAINER)
        if stager is None or launch is None:
            return
        self._check_seed(seed, "to the stager container")
        self._backend.stage(stager, seed)
        self._remove((stager,))
        if stager in self._open:
            raise _refused(
                f"the stager container {stager.name} was not verified removed, and the launch "
                "container never starts while it may exist"
            )
        self._backend.start(launch)

    @property
    def exports(self) -> bool:
        """Whether the launch's candidate is its exported artifact (ADR 0013 D5; #269).

        The engine's contract, never the backend's choice: a change-producing
        launch the engine handed an export directory (``LaunchInputs.export_dir``,
        which it does for every launch of a backend that seeds). Such a
        launch's candidate is the artifact its verify step accepts, or none.
        """
        return self._change_producing and bool(self._inputs.export_dir)

    @property
    def git_input(self) -> LaunchGitInput | None:
        """The launch's git input, completed with its seed's manifest once its seed step ended."""
        return self._inputs.git_input

    def export(self) -> Exported | None:
        """The export step; ``None`` when nothing is exported, which skips verify.

        A launch that :attr:`exports` and whose environment was made is never
        skipped: when its backend makes no exporter, the step's outcome is a
        failed export, which the verify step refuses and saves like any other
        (D17), and the volume stays until that refusal is saved
        (:meth:`saved`). A launch whose environment was never made ran no
        command and has nothing to export: ``None``, and the caller refuses
        the candidate of a launch that :attr:`exports` itself.

        The exporter's entry is written before its create, and its removal
        journaled after it ran, whatever the export's outcome. An exporter
        not verified removed may still write the artifact: neither the
        artifact nor the export's failure is returned, and
        :class:`~autoforge.errors.SandboxCleanupUncertainError` is raised,
        so nothing verifies it and the volume stays.
        """
        self._advance("export", "launch")
        handle = self._handle if self._environment == "ready" else None
        exporter = (
            self._backend.exporter(handle) if self._change_producing and handle is not None else ()
        )
        if not exporter and handle is not None and self.exports:
            return Exported(
                failure=f"its {self._backend.kind.value} backend made no exporter for a launch "
                "whose candidate is its exported artifact, so nothing was exported"
            )
        if not exporter or handle is None:
            self._export_skipped = True
            return None
        # The exporter mounts the volume (read-only) and its export directory,
        # never the projection, even one whose removal was uncertain.
        volumes = {obj.name for obj in self._open if obj.kind is ObjectKind.VOLUME}
        origin = _check_export(exporter, self._owner, volumes, self._inputs.export_dir)
        self._journal.record_creates(self._runtime, exporter)
        self._open.extend(exporter)
        try:
            outcome = Exported(self._backend.export(exporter, handle), origin=origin)
        except SandboxLaunchRefusedError as exc:
            outcome = Exported(failure=str(exc), origin=origin)
        finally:
            self._remove(tuple(obj for obj in exporter if obj.kind is ObjectKind.EXPORTER))
        self._raise_if_uncertain("the export")
        return outcome

    def saved(self) -> None:
        """The verify step's outcome is saved: the final clean-up runs now.

        Only after an export that returned an outcome: the volume is never
        removed before what verify read from it is saved, nor while an
        exporter may still be running.
        """
        if self._step != "export":
            raise _refused(
                f"a verify outcome is saved after the export step, not after {self._step}"
            )
        self._saved = True
        self._finish()
        self._raise_if_uncertain("the final clean-up")

    # -- the clean-up -----------------------------------------------------------
    def _find(self, kind: ObjectKind) -> LifecycleObject | None:
        return next((obj for obj in self._open if obj.kind is kind), None)

    def _awaits_verify(self) -> bool:
        """A change-producing launch whose command may have run, whose export was not skipped."""
        return self._change_producing and self._environment == "ready" and not self._export_skipped

    def _may_finish(self) -> bool:
        return self._saved or not self._awaits_verify()

    def _release(self) -> None:
        """Remove what holds the credential, and the volume when nothing awaits export."""
        if self._released:
            return
        self._released = True
        keep = {ObjectKind.VOLUME} if self._awaits_verify() else set()
        self._remove(tuple(obj for obj in self._open if obj.kind not in keep))

    def _finish(self) -> None:
        """The final clean-up: what the release kept for the export."""
        if self._finished:
            return
        self._finished = True
        self._remove(tuple(self._open))

    def _remove(self, objects: tuple[LifecycleObject, ...]) -> None:
        """Journal the intent, remove each, close what was verified gone; never raises.

        In :data:`REMOVAL_ORDER`: every container before any object it
        mounts. A volume that a container of the launch still mounts once
        that container's removal was tried, because it was not verified
        gone, is not removed: the runtime would refuse it, and an exporter
        may still read it. A journal that refuses the intent removes
        nothing. Each of these, a removal that fails or cannot be verified,
        and an entry the journal will not close, is uncertain and stays open.
        """
        if not objects:
            return
        objects = _removal_order(objects)
        try:
            self._journal.record_removals(objects)
        except Exception:  # noqa: BLE001 - reported, the entries stay open
            self._uncertain(objects)
            return
        gone: list[LifecycleObject] = []
        for obj in objects:
            if self._still_mounted(obj, gone):
                self._uncertain((obj,))
                continue
            try:
                outcome = self._backend.remove(obj)
            except Exception:  # noqa: BLE001 - reported, the entry stays open
                outcome = Removal.UNCERTAIN
            if outcome is Removal.REMOVED:
                gone.append(obj)
            else:
                self._uncertain((obj,))
        if not gone:
            return
        try:
            self._journal.close_entries(tuple(gone))
        except Exception:  # noqa: BLE001 - removed, but the journal still names them
            self._uncertain(tuple(gone))
            return
        self._open = [obj for obj in self._open if obj not in gone]
        # A removal retried by the final part and verified now is no longer uncertain.
        self.uncertain = [obj for obj in self.uncertain if obj not in gone]

    def _still_mounted(self, obj: LifecycleObject, gone: list[LifecycleObject]) -> bool:
        """A volume that a container of the launch, not verified gone, mounts.

        A bind source (the staging root, the projection, the export
        directory) is the controller's own host path, which no runtime holds
        by reference, so its removal goes ahead: the credential leaves the
        host even when its container's removal is uncertain.
        """
        return obj.kind is ObjectKind.VOLUME and any(
            other.kind in CONTAINER_KINDS and obj.name in other.mounts and other not in gone
            for other in self._open
        )

    def _uncertain(self, objects: tuple[LifecycleObject, ...]) -> None:
        self.uncertain.extend(obj for obj in objects if obj not in self.uncertain)

    def _unsurfaced(self) -> list[LifecycleObject]:
        return [obj for obj in self.uncertain if obj not in self._surfaced]

    def _raise_if_uncertain(self, where: str) -> None:
        pending = self._unsurfaced()
        if pending:
            self._surface(pending, where)

    def _surface(self, pending: list[LifecycleObject], where: str) -> None:
        self._surfaced.extend(pending)
        raise SandboxCleanupUncertainError(_uncertain_text(pending, where), _named(pending))


def _uncertain_text(objects: list[LifecycleObject], where: str) -> str:
    one = len(objects) == 1
    entries = "its journal entry stays" if one else "their journal entries stay"
    return (
        f"{where} could not verify that {', '.join(_named(objects))} "
        f"{'is' if one else 'are'} gone: {entries} open for the next command's "
        "reconciliation, and nothing of the launch is read as done"
    )
