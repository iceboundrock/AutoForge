"""The engine's sequence of a sandbox backend's lifecycle (ADR 0013 D13, D16; #268).

:mod:`autoforge.sandbox_backend` says what a backend can do; this module
says in which order the engine asks, and what it journals first. It is
the engine layer's half of the seam: the backend never orders its own
steps, never writes the journal and never decides what is kept.

**At a command's entry** (:func:`enter`), before anything of a launch, in
D16's order: the runtime check (read-only, never cached; a refusal leaves
everything as it was and reconciliation is not attempted), then
reconciliation (removal intents first, an entry closed only once its
object is verified gone), then the probe, which the caller caches for the
runtime report it was proved on. The pin check comes before all three and
is the engine's own (#280).

**One launch** (:class:`LaunchLifecycle`), in D13's order:

1. *seed*: before any journal entry, so a failure there leaves nothing to
   reconcile;
2. *create*: the objects checked against D13's rules, then one journal
   write that names them all, then each made in :data:`CREATE_ORDER`
   (every bind source before any container, the volume before the stager
   container, that before the launch container);
3. *stage*: inside the stager container only; its removal journaled, then
   verified, before the launch container starts, which never happens after
   a failed stage;
4. *run*: the caller's launch through the executor, whose prepared launch
   is closed on every path; then
5. *the release*, right after the run step on every path, ``Ctrl-C`` and an
   exception included: what holds the credential is removed, and the
   volume too when there is nothing to export;
6. *export*, for a change-producing launch: the exporter's own entry before
   its create, its removal journaled after;
7. *verify* is the caller's (#269); once its outcome is saved,
   :meth:`LaunchLifecycle.saved` runs
8. *the final clean-up*: the volume and the export directory. Never before
   that outcome is saved: a launch that ends between its run and that save
   keeps its volume for reconciliation.

Every removal is journaled first, and its entry is closed only after the
backend verified it. A removal that cannot be verified is
:attr:`~autoforge.sandbox_backend.Removal.UNCERTAIN`: reported in
:attr:`LaunchLifecycle.uncertain`, the entry left open for reconciliation,
never reported as done. A failure of a step up to the run refuses the
launch (:class:`~autoforge.errors.SandboxLaunchRefusedError`), and both
parts of the clean-up then remove what was made; nothing is ever run
without the sandbox instead.

For bwrap every step but the run does nothing, writes nothing and makes
nothing: its launches are what they were.

The journal is :class:`LifecycleJournal`. Its file, format, entries,
runtime binding and the reconciliation logic are #272's; until it lands,
the engine's journal is :data:`NO_JOURNAL`, which holds no entry and
refuses every write, so a backend that makes an object is refused before
it makes one.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, TypeVar

from .errors import SandboxLaunchRefusedError
from .sandbox_backend import (
    CONTAINER_KINDS,
    CREATE_ORDER,
    EXPORT_KINDS,
    NETWORK_NONE,
    Artifact,
    LaunchOwner,
    LifecycleObject,
    ObjectKind,
    ProbeResult,
    Removal,
    RuntimeReport,
    SandboxBackend,
    Seed,
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
    """
    runtime = backend.runtime_check()
    still_open = reconcile(backend, journal, runtime)
    return Entry(runtime, probe(runtime), still_open)


def reconcile(
    backend: SandboxBackend, journal: LifecycleJournal, runtime: RuntimeReport
) -> tuple[LifecycleObject, ...]:
    """Remove the journal's open entries through ``backend``; return those still open.

    Remove-only, intents first; an entry closes only when the backend
    verified its object gone. What ownership it cannot prove, and the
    ``BLOCKED`` that follows, is #272's.
    """
    entries = journal.open_entries()
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
    sources = {obj.name for obj in objects if obj.kind not in CONTAINER_KINDS}
    _check_mounts(objects, sources)


def _check_export(
    objects: tuple[LifecycleObject, ...], owner: LaunchOwner, sources: set[str]
) -> None:
    """The export step's objects: one exporter, offline, mounting no projection."""
    _check_owned(objects, owner, EXPORT_KINDS)
    exporters = [obj for obj in objects if obj.kind is ObjectKind.EXPORTER]
    if len(exporters) != 1:
        raise _refused("an export runs in exactly one exporter container")
    (exporter,) = exporters
    if exporter.network != NETWORK_NONE:
        raise _refused(f"the exporter {exporter.name} is not created offline")
    _check_mounts(
        objects, sources | {obj.name for obj in objects if obj.kind not in CONTAINER_KINDS}
    )


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


class LaunchLifecycle:
    """One launch's steps, in D13's order, journaled; a context manager.

    ``with LaunchLifecycle(...) as lifecycle:`` then :meth:`seed`,
    :meth:`create`, :meth:`stage`, :meth:`run`, and for a change-producing
    launch :meth:`export` and, once the verify step's outcome is saved,
    :meth:`saved`. Leaving the ``with`` block on any path runs the release
    if the run step did not, and the final clean-up when it is allowed;
    it never raises for a removal, and the original exception, ``Ctrl-C``
    included, is the one that propagates. A step called out of order is
    refused.
    """

    def __init__(
        self,
        backend: SandboxBackend,
        journal: LifecycleJournal,
        owner: LaunchOwner,
        runtime: RuntimeReport,
        *,
        change_producing: bool,
    ) -> None:
        self._backend = backend
        self._journal = journal
        self._owner = owner
        self._runtime = runtime
        self._change_producing = change_producing
        self._step = "start"
        self._seed: Seed | None = None
        self._open: list[LifecycleObject] = []
        self._ran = False
        self._released = False
        self._export_skipped = False
        self._saved = False
        self._finished = False
        # Every object whose removal could not be verified: its entry stays
        # open for reconciliation (ADR 0013 D17).
        self.uncertain: list[LifecycleObject] = []

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

    @property
    def open_objects(self) -> tuple[LifecycleObject, ...]:
        """The launch's objects whose entries are still open."""
        return tuple(self._open)

    # -- the steps --------------------------------------------------------------
    def _advance(self, step: str, after: str) -> None:
        if self._step != after:
            raise _refused(f"the {step} step comes after the {after} step, not after {self._step}")
        self._step = step

    def seed(self) -> Seed:
        """The seed step, before any journal entry; a failure leaves nothing to reconcile."""
        self._advance("seed", "start")
        self._seed = self._backend.seed(self._owner)
        return self._seed

    def create(self) -> None:
        """One journal write for every object, then each made in D13's order."""
        self._advance("create", "seed")
        assert self._seed is not None
        objects = tuple(self._backend.objects(self._owner, self._seed))
        _check_create(objects, self._owner)
        objects = _in_order(objects)
        if not objects:
            return
        self._journal.record_creates(self._runtime, objects)
        self._open = list(objects)
        for obj in objects:
            self._backend.make(obj, self._seed)

    def stage(self) -> None:
        """The stager container's work, its journaled removal, then the launch container's start."""
        self._advance("stage", "create")
        assert self._seed is not None
        stager = self._find(ObjectKind.STAGER_CONTAINER)
        launch = self._find(ObjectKind.LAUNCH_CONTAINER)
        if stager is None or launch is None:
            return
        self._backend.stage(stager, self._seed)
        self._remove((stager,))
        if stager in self._open:
            raise _refused(
                f"the stager container {stager.name} was not verified removed, and the launch "
                "container never starts while it may exist"
            )
        self._backend.start(launch)

    def run(self, launch: Callable[[], T]) -> T:
        """The run step; the release follows it on every path."""
        self._advance("run", "stage")
        self._ran = True
        try:
            return launch()
        finally:
            self._release()

    def export(self) -> Artifact | None:
        """The export step; ``None`` when nothing is exported, which skips verify.

        The exporter's entry is written before its create, and its removal
        journaled after it ran, whatever the export's outcome.
        """
        self._advance("export", "run")
        exporter = self._backend.exporter(self._owner) if self._change_producing else ()
        if not exporter:
            self._export_skipped = True
            return None
        # The exporter mounts the volume (read-only) and its export directory,
        # never the projection, even one whose removal was uncertain.
        volumes = {obj.name for obj in self._open if obj.kind is ObjectKind.VOLUME}
        _check_export(exporter, self._owner, volumes)
        self._journal.record_creates(self._runtime, exporter)
        self._open.extend(exporter)
        try:
            return self._backend.export(exporter)
        finally:
            self._remove(tuple(obj for obj in exporter if obj.kind is ObjectKind.EXPORTER))

    def saved(self) -> None:
        """The verify step's outcome is saved: the final clean-up runs now.

        Only after the export step, which ran, failed or was skipped: the
        volume is never removed before what verify read from it is saved.
        """
        if self._step != "export":
            raise _refused(
                f"a verify outcome is saved after the export step, not after {self._step}"
            )
        self._saved = True
        self._finish()

    # -- the clean-up -----------------------------------------------------------
    def _find(self, kind: ObjectKind) -> LifecycleObject | None:
        return next((obj for obj in self._open if obj.kind is kind), None)

    def _awaits_verify(self) -> bool:
        """A change-producing launch that ran, whose export was not skipped."""
        return self._change_producing and self._ran and not self._export_skipped

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

        A journal that refuses the intent removes nothing; a removal that
        fails or cannot be verified, and an entry the journal will not
        close, is uncertain and stays open.
        """
        if not objects:
            return
        try:
            self._journal.record_removals(objects)
        except Exception:  # noqa: BLE001 - reported, the entries stay open
            self._uncertain(objects)
            return
        gone: list[LifecycleObject] = []
        for obj in objects:
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

    def _uncertain(self, objects: tuple[LifecycleObject, ...]) -> None:
        self.uncertain.extend(obj for obj in objects if obj not in self.uncertain)
