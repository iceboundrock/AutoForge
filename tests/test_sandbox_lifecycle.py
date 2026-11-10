"""The engine's sequence of a sandbox backend's lifecycle, on a fake backend (ADR 0013 D13, #268).

No runtime is needed. :class:`FakeRuntime` is a container runtime that, as
Docker and Podman do, accepts a bind, a volume or a network only in a
container's create call; every other call it has names a container and
nothing it could add to it. :class:`FakeBackend` is a container-shaped
backend over it (both staging forms, ``bind`` and ``copy``), and
:class:`FakeJournal` records what the engine journals. They share one
event list, so a test reads the order across all three. The engine's own
use of the sequence is at the end of this module.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import autoforge.engine as engine_module
from autoforge.config import default_config
from autoforge.engine import ControllerEngine
from autoforge.errors import ControlResultValidationError, SandboxLaunchRefusedError
from autoforge.run_namespace import issue_namespace
from autoforge.sandbox import BackendKind, LaunchDescription
from autoforge.sandbox_backend import (
    BWRAP,
    NETWORK_NONE,
    Artifact,
    LaunchOwner,
    LifecycleObject,
    ObjectKind,
    ProbeResult,
    Removal,
    RuntimeReport,
    Seed,
)
from autoforge.sandbox_lifecycle import NO_JOURNAL, LaunchLifecycle, enter, reconcile
from autoforge.state import load_state
from autoforge.transitions import Phase
from tests.conftest import EPIC, ISSUE, git_repo, implement, make_engine

OWNER = LaunchOwner(run="run-1", issue=268, phase="ANALYZE_EXECUTE", launch="ANALYZE_EXECUTE-1")
RUNTIME = RuntimeReport(
    BackendKind.CONTAINER, "podman", "5.2.0", True, "unix:///run/user/1000/podman.sock", "store-1"
)


class FakeRuntime:
    """A rootless runtime's CLI as the lifecycle sees it: what was called, in order.

    A bind, a volume and a network exist only as arguments of
    :meth:`create_container`; ``start``, ``exec``, ``cp`` and ``rm`` take a
    container's name and nothing they could configure it with.
    """

    def __init__(self, events: list[tuple]) -> None:
        self.events = events
        self.containers: dict[str, dict] = {}
        self.volumes: set[str] = set()

    def create_volume(self, name: str) -> None:
        self.events.append(("runtime", "volume create", name))
        self.volumes.add(name)

    def create_container(self, name: str, *, network: str, mounts: tuple[str, ...]) -> None:
        self.events.append(("runtime", "create", name, network, mounts))
        self.containers[name] = {"network": network, "mounts": mounts, "running": False}

    def start(self, name: str) -> None:
        self.events.append(("runtime", "start", name))
        self.containers[name]["running"] = True

    def exec(self, name: str, helper: str) -> None:
        assert self.containers[name]["running"], f"exec into {name}, which is not running"
        self.events.append(("runtime", "exec", name, helper))

    def cp(self, name: str, what: str) -> None:
        self.events.append(("runtime", "cp", name, what))

    def rm(self, name: str) -> None:
        self.events.append(("runtime", "rm", name))
        self.containers.pop(name, None)
        self.volumes.discard(name)


class FakeBackend:
    """A container-shaped backend over :class:`FakeRuntime` (the bind or the copy-in form).

    ``fail`` names one operation that raises a setup failure;
    ``uncertain`` the kinds whose removal it cannot verify.
    """

    def __init__(
        self,
        events: list[tuple],
        *,
        staging: str = "bind",
        fail: str = "",
        uncertain: frozenset[ObjectKind] = frozenset(),
        exporter: bool = True,
    ) -> None:
        self.events = events
        self.runtime = FakeRuntime(events)
        self.staging = staging
        self.fail = fail
        self.uncertain = uncertain
        self.has_exporter = exporter
        self.made: list[str] = []
        self.objects_override: tuple[LifecycleObject, ...] | None = None

    def _maybe_fail(self, what: str) -> None:
        if self.fail == what:
            self.events.append(("fail", what))
            raise SandboxLaunchRefusedError(f"fake: {what} failed")

    @property
    def kind(self) -> BackendKind:
        return BackendKind.CONTAINER

    def runtime_check(self) -> RuntimeReport:
        self.events.append(("runtime", "check"))
        self._maybe_fail("runtime check")
        return RUNTIME

    def reconcile(self, entries, runtime):
        self.events.append(("runtime", "reconcile", tuple(e.name for e in entries)))
        assert runtime == RUNTIME
        for entry in entries:
            self.runtime.rm(entry.name)
        return entries

    def probe(self, *, home, environ) -> ProbeResult:
        self.events.append(("runtime", "probe"))
        return ProbeResult(True, "", "fake")

    def plan(self, request) -> LaunchDescription:
        return LaunchDescription(mounts=())

    def seed(self, owner: LaunchOwner) -> Seed:
        self.events.append(("host", "seed"))
        self._maybe_fail("seed")
        return Seed("sha256:manifest", "/tmp/seed")

    def discard_seed(self, seed: Seed) -> None:
        self.events.append(("host", "discard seed"))

    def objects(self, owner: LaunchOwner, seed: Seed) -> tuple[LifecycleObject, ...]:
        if self.objects_override is not None:
            return self.objects_override
        return container_objects(owner, self.staging)

    def make(self, obj: LifecycleObject, seed: Seed) -> None:
        assert seed.identity == "sha256:manifest"
        if obj.kind is ObjectKind.STAGING_ROOT:
            self.events.append(("host", "write staging root", obj.name))
            self._maybe_fail("staging root")
            self.events.append(("host", "prove staging root", obj.name))
        elif obj.kind is ObjectKind.PROJECTION:
            self.events.append(("host", "write projection", obj.name))
        elif obj.kind is ObjectKind.VOLUME:
            self._maybe_fail("volume")
            self.runtime.create_volume(obj.name)
        else:
            self._maybe_fail(f"create {obj.kind.value}")
            self.runtime.create_container(obj.name, network=obj.network, mounts=obj.mounts)
        self.made.append(obj.name)

    def stage(self, stager: LifecycleObject, seed: Seed) -> None:
        self.runtime.start(stager.name)
        if self.staging == "copy":
            self.runtime.cp(stager.name, "seed")
            self.runtime.cp(stager.name, "manifest")
        self.runtime.exec(stager.name, "stager")
        self.runtime.exec(stager.name, "prover")
        self._maybe_fail("prover")

    def start(self, container: LifecycleObject) -> None:
        self._maybe_fail("start")
        self.runtime.start(container.name)
        if self.staging == "copy":
            self.runtime.cp(container.name, "read context")

    def prepare(self, launch, command, *, cwd, env):  # the executor's; unused here
        raise AssertionError("the run step's prepare is the executor's")

    def exporter(self, owner: LaunchOwner) -> tuple[LifecycleObject, ...]:
        if not self.has_exporter:
            return ()
        return (
            LifecycleObject(
                owner, ObjectKind.EXPORTER, "exporter", "n-exp", NETWORK_NONE, ("vol", "out")
            ),
            LifecycleObject(owner, ObjectKind.EXPORT_DIR, "out", "n-out"),
        )

    def export(self, objects: tuple[LifecycleObject, ...]) -> Artifact:
        (exporter,) = (obj for obj in objects if obj.kind is ObjectKind.EXPORTER)
        self.runtime.create_container(
            exporter.name, network=exporter.network, mounts=exporter.mounts
        )
        self.runtime.start(exporter.name)
        self.runtime.exec(exporter.name, "exporter")
        self._maybe_fail("export")
        return Artifact("/tmp/out")

    def remove(self, obj: LifecycleObject) -> Removal:
        self._maybe_fail(f"remove {obj.kind.value}")
        if obj.kind in self.uncertain:
            self.events.append(("runtime", "rm uncertain", obj.name))
            return Removal.UNCERTAIN
        if obj.kind in (ObjectKind.STAGING_ROOT, ObjectKind.PROJECTION, ObjectKind.EXPORT_DIR):
            self.events.append(("host", "rm", obj.name))
        else:
            self.runtime.rm(obj.name)
        return Removal.REMOVED


def container_objects(owner: LaunchOwner, staging: str) -> tuple[LifecycleObject, ...]:
    """D13's create objects; in the bind form the staging root is ``/source`` in both containers."""
    source = ("staging",) if staging == "bind" else ()
    objects = [
        LifecycleObject(owner, ObjectKind.PROJECTION, "projection", "n-proj"),
        LifecycleObject(owner, ObjectKind.VOLUME, "vol", "n-vol"),
        LifecycleObject(
            owner,
            ObjectKind.LAUNCH_CONTAINER,
            "launch",
            "n-launch",
            "bridge",
            (*source, "vol", "projection"),
        ),
        LifecycleObject(
            owner, ObjectKind.STAGER_CONTAINER, "stager", "n-stager", NETWORK_NONE, (*source, "vol")
        ),
    ]
    if staging == "bind":
        objects.append(LifecycleObject(owner, ObjectKind.STAGING_ROOT, "staging", "n-staging"))
    # Out of order on purpose: the engine makes them in D13's order.
    return tuple(objects)


class FakeJournal:
    """What the engine journals, as events; open entries as a list."""

    def __init__(self, events: list[tuple], entries: tuple[LifecycleObject, ...] = ()) -> None:
        self.events = events
        self.entries = list(entries)
        self.refuse = ""

    def open_entries(self) -> tuple[LifecycleObject, ...]:
        return tuple(self.entries)

    def record_creates(self, runtime, objects) -> None:
        assert runtime == RUNTIME
        self.events.append(("journal", "creates", tuple(obj.name for obj in objects)))
        self.entries.extend(objects)

    def record_removals(self, objects) -> None:
        if self.refuse == "removals":
            raise OSError("fake: the journal cannot be written")
        self.events.append(("journal", "removals", tuple(obj.name for obj in objects)))

    def close_entries(self, objects) -> None:
        self.events.append(("journal", "close", tuple(obj.name for obj in objects)))
        for obj in objects:
            self.entries.remove(obj)


def _index(events: list[tuple], *event) -> int:
    return events.index(tuple(event))


def _first(events: list[tuple], predicate) -> int:
    return next(i for i, event in enumerate(events) if predicate(event))


def _lifecycle(backend, journal, *, change: bool = True) -> LaunchLifecycle:
    return LaunchLifecycle(backend, journal, OWNER, RUNTIME, change_producing=change)


def _launch(events: list[tuple]):
    def run() -> str:
        events.append(("executor", "run"))
        return "result"

    return run


def _full(backend, journal, events, *, change: bool = True) -> LaunchLifecycle:
    """Every step, a verify the test stands in for, and its outcome saved."""
    with _lifecycle(backend, journal, change=change) as lifecycle:
        lifecycle.seed()
        lifecycle.create()
        lifecycle.stage()
        assert lifecycle.run(_launch(events)) == "result"
        artifact = lifecycle.export()
        events.append(("engine", "verify", artifact))
        events.append(("engine", "verify outcome saved"))
        lifecycle.saved()
    return lifecycle


# -- at a command's entry (D16) -----------------------------------------------------


def test_the_entry_runs_the_runtime_check_then_reconciliation_then_the_probe():
    events: list[tuple] = []
    backend = FakeBackend(events)
    leftover = container_objects(OWNER, "bind")[:2]
    journal = FakeJournal(events, leftover)

    def probe(runtime: RuntimeReport) -> ProbeResult:
        events.append(("engine", "probe", runtime))
        return ProbeResult(True, "", "fake")

    entry = enter(backend, journal, probe)

    assert events == [
        ("runtime", "check"),
        ("journal", "removals", ("projection", "vol")),
        ("runtime", "reconcile", ("projection", "vol")),
        ("runtime", "rm", "projection"),
        ("runtime", "rm", "vol"),
        ("journal", "close", ("projection", "vol")),
        ("engine", "probe", RUNTIME),
    ]
    assert entry.runtime == RUNTIME and entry.probe.supported and entry.still_open == ()
    assert journal.entries == []


def test_a_refused_runtime_check_writes_nothing_and_reconciles_nothing():
    events: list[tuple] = []
    backend = FakeBackend(events, fail="runtime check")
    journal = FakeJournal(events, container_objects(OWNER, "bind"))
    probes: list[RuntimeReport] = []
    with pytest.raises(SandboxLaunchRefusedError):
        enter(backend, journal, probes.append)  # type: ignore[arg-type]
    assert events == [("runtime", "check"), ("fail", "runtime check")]
    assert probes == [] and len(journal.entries) == 5


def test_reconciliation_closes_only_the_entries_verified_gone():
    events: list[tuple] = []
    entries = container_objects(OWNER, "bind")
    journal = FakeJournal(events, entries)

    class Partial(FakeBackend):
        def reconcile(self, entries, runtime):
            return entries[:1]

    still_open = reconcile(Partial(events), journal, RUNTIME)
    assert still_open == entries[1:]
    assert ("journal", "close", (entries[0].name,)) in events
    assert journal.entries == list(entries[1:])


def test_reconciliation_closes_nothing_the_journal_does_not_name():
    events: list[tuple] = []
    journal = FakeJournal(events, ())
    stray = container_objects(OWNER, "bind")[:1]

    class Inventing(FakeBackend):
        def reconcile(self, entries, runtime):
            return stray

    with pytest.raises(SandboxLaunchRefusedError, match="does not name"):
        reconcile(Inventing(events), journal, RUNTIME)
    assert not any(event[:2] == ("journal", "close") for event in events)


# -- one launch (D13) ---------------------------------------------------------------


@pytest.mark.parametrize("staging", ["bind", "copy"])
def test_a_launch_runs_d13s_steps_in_order(staging):
    events: list[tuple] = []
    backend = FakeBackend(events, staging=staging)
    journal = FakeJournal(events)
    lifecycle = _full(backend, journal, events)

    seeded = _index(events, "host", "seed")
    # No journal entry and no runtime call before the seed step has finished.
    assert all(event[0] == "host" for event in events[: seeded + 1])
    # One write names every object, before the staging root is written.
    names = ("staging", "projection", "vol", "stager", "launch")
    names = names if staging == "bind" else names[1:]
    created = _index(events, "journal", "creates", names)
    assert created == seeded + 1
    vol = _index(events, "runtime", "volume create", "vol")
    stager_create = _first(events, lambda e: e[:3] == ("runtime", "create", "stager"))
    launch_create = _first(events, lambda e: e[:3] == ("runtime", "create", "launch"))
    if staging == "bind":
        written = _index(events, "host", "write staging root", "staging")
        proved = _index(events, "host", "prove staging root", "staging")
        assert created < written < proved < vol
    assert created < vol < stager_create < launch_create
    # Each container's create configures every mount and its network.
    source = ("staging",) if staging == "bind" else ()
    assert events[stager_create] == ("runtime", "create", "stager", NETWORK_NONE, (*source, "vol"))
    assert events[launch_create] == (
        "runtime",
        "create",
        "launch",
        "bridge",
        (*source, "vol", "projection"),
    )
    # Nothing after a container's create adds or changes a mount or a network.
    for event in events:
        if event[0] == "runtime" and event[1] not in ("create", "volume create"):
            assert len(event) <= 4 and not any(isinstance(part, tuple) for part in event)
    # The stage step: inside the stager container, which is removed before the
    # launch container starts.
    stager_start = _index(events, "runtime", "start", "stager")
    stager_exec = _index(events, "runtime", "exec", "stager", "stager")
    prover_exec = _index(events, "runtime", "exec", "stager", "prover")
    stager_intent = _index(events, "journal", "removals", ("stager",))
    stager_rm = _index(events, "runtime", "rm", "stager")
    stager_closed = _index(events, "journal", "close", ("stager",))
    launch_start = _index(events, "runtime", "start", "launch")
    assert (
        launch_create
        < stager_start
        < stager_exec
        < prover_exec
        < stager_intent
        < stager_rm
        < stager_closed
        < launch_start
    )
    if staging == "copy":
        seed_in = _index(events, "runtime", "cp", "stager", "seed")
        assert stager_start < seed_in < _index(events, "runtime", "cp", "stager", "manifest")
        assert seed_in < stager_exec
        assert launch_start < _index(events, "runtime", "cp", "launch", "read context")
    else:
        assert not any(event[1] == "cp" for event in events)
    # The run, then the release: what holds the credential goes; the volume stays.
    run = _index(events, "executor", "run")
    released = ("launch", "projection", *source)
    release_intent = _index(events, "journal", "removals", tuple(sorted(released, key=names.index)))
    assert launch_start < run < release_intent < _index(events, "runtime", "rm", "launch")
    # The export: the exporter's own entry before its create, its removal journaled.
    exporter_entry = _index(events, "journal", "creates", ("exporter", "out"))
    exporter_create = _first(events, lambda e: e[:3] == ("runtime", "create", "exporter"))
    exporter_intent = _index(events, "journal", "removals", ("exporter",))
    assert release_intent < exporter_entry < exporter_create < exporter_intent
    assert exporter_intent < _index(events, "runtime", "rm", "exporter")
    assert events[exporter_create] == (
        "runtime",
        "create",
        "exporter",
        NETWORK_NONE,
        ("vol", "out"),
    )
    # The final part: the volume and the export directory, only once verify's
    # outcome is saved, intent first.
    saved = _index(events, "engine", "verify outcome saved")
    final_intent = _index(events, "journal", "removals", ("vol", "out"))
    assert exporter_intent < saved < final_intent < _index(events, "runtime", "rm", "vol")
    assert _index(events, "engine", "verify", Artifact("/tmp/out")) < saved
    assert journal.entries == [] and lifecycle.uncertain == []
    assert backend.runtime.containers == {} and backend.runtime.volumes == set()
    assert events[-1] == ("host", "discard seed")


def test_every_removal_is_journaled_first_and_closed_only_after_it():
    events: list[tuple] = []
    _full(FakeBackend(events), FakeJournal(events), events)
    pending: set[str] = set()
    for event in events:
        if event[:2] == ("journal", "removals"):
            pending |= set(event[2])
        elif event[1] in ("rm", "rm uncertain"):
            assert event[2] in pending, f"{event[2]} removed before its removal was journaled"
        elif event[:2] == ("journal", "close"):
            assert set(event[2]) <= pending
    created: set[str] = set()
    for event in events:
        if event[:2] == ("journal", "creates"):
            created |= set(event[2])
        elif event[0] == "runtime" and event[1] in ("create", "volume create"):
            assert event[2] in created, f"{event[2]} created before its entry"
        elif event[0] == "host" and event[1] in ("write staging root", "write projection"):
            assert event[2] in created, f"{event[2]} written before its entry"


def test_a_failed_seed_leaves_no_journal_entry_and_no_runtime_call():
    events: list[tuple] = []
    journal = FakeJournal(events)
    with pytest.raises(SandboxLaunchRefusedError, match="seed failed"):
        with _lifecycle(FakeBackend(events, fail="seed"), journal) as lifecycle:
            lifecycle.seed()
            lifecycle.create()
    assert events == [("host", "seed"), ("fail", "seed")]
    assert journal.entries == []


@pytest.mark.parametrize(
    "fail",
    [
        "staging root",
        "volume",
        "create stager container",
        "create launch container",
        "prover",
        "start",
    ],
)
def test_a_refusal_at_a_step_before_the_run_removes_what_was_made_and_runs_nothing(fail):
    events: list[tuple] = []
    backend = FakeBackend(events, fail=fail)
    journal = FakeJournal(events)
    with pytest.raises(SandboxLaunchRefusedError, match="failed"):
        with _lifecycle(backend, journal) as lifecycle:
            lifecycle.seed()
            lifecycle.create()
            lifecycle.stage()
            lifecycle.run(_launch(events))
    assert ("executor", "run") not in events
    # The launch container never starts after a failed stage.
    assert ("runtime", "start", "launch") not in events
    # Both parts of the clean-up ran, with no export: every entry closed, the
    # volume included, every object gone.
    assert not any(
        event[:2] == ("journal", "creates") and "exporter" in event[2] for event in events
    )
    assert journal.entries == [] and lifecycle.uncertain == []
    assert backend.runtime.containers == {} and backend.runtime.volumes == set()


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, RuntimeError])
def test_the_release_follows_the_run_on_every_path(interruption):
    events: list[tuple] = []
    backend = FakeBackend(events)
    journal = FakeJournal(events)

    def run() -> None:
        events.append(("executor", "run"))
        raise interruption("the run step was interrupted")

    with pytest.raises(interruption):
        with _lifecycle(backend, journal) as lifecycle:
            lifecycle.seed()
            lifecycle.create()
            lifecycle.stage()
            lifecycle.run(run)
    run_at = _index(events, "executor", "run")
    assert _index(events, "journal", "removals", ("staging", "projection", "launch")) > run_at
    # A change-producing launch that never reached a saved verify outcome keeps
    # its volume, journaled, for reconciliation.
    assert lifecycle.open_objects == tuple(o for o in journal.entries if o.name == "vol")
    assert backend.runtime.volumes == {"vol"}
    assert ("runtime", "rm", "vol") not in events


def test_the_volume_is_never_removed_before_verifys_outcome_is_saved():
    events: list[tuple] = []
    backend = FakeBackend(events)
    journal = FakeJournal(events)
    with _lifecycle(backend, journal) as lifecycle:
        lifecycle.seed()
        lifecycle.create()
        lifecycle.stage()
        lifecycle.run(_launch(events))
        assert lifecycle.export() == Artifact("/tmp/out")
        # The engine leaves before it saved verify's outcome.
    assert backend.runtime.volumes == {"vol"}
    assert {obj.name for obj in journal.entries} == {"vol", "out"}


def test_a_failed_export_keeps_the_volume_until_the_refusal_is_saved():
    events: list[tuple] = []
    backend = FakeBackend(events, fail="export")
    journal = FakeJournal(events)
    with _lifecycle(backend, journal) as lifecycle:
        lifecycle.seed()
        lifecycle.create()
        lifecycle.stage()
        lifecycle.run(_launch(events))
        with pytest.raises(SandboxLaunchRefusedError, match="export failed"):
            lifecycle.export()
        # The exporter itself is gone, its removal journaled; the volume stays.
        assert ("runtime", "rm", "exporter") in events
        assert backend.runtime.volumes == {"vol"}
        events.append(("engine", "refusal saved"))
        lifecycle.saved()
    assert _index(events, "engine", "refusal saved") < _index(events, "runtime", "rm", "vol")
    assert journal.entries == []


def test_a_launch_with_nothing_to_export_removes_its_volume_at_the_release():
    events: list[tuple] = []
    backend = FakeBackend(events)
    journal = FakeJournal(events)
    with _lifecycle(backend, journal, change=False) as lifecycle:
        lifecycle.seed()
        lifecycle.create()
        lifecycle.stage()
        lifecycle.run(_launch(events))
        assert ("runtime", "rm", "vol") in events
        assert lifecycle.export() is None
    assert not any(event[:3] == ("runtime", "create", "exporter") for event in events)
    assert journal.entries == []


def test_a_backend_with_no_exporter_skips_the_export_and_then_cleans_up():
    events: list[tuple] = []
    backend = FakeBackend(events, exporter=False)
    journal = FakeJournal(events)
    with _lifecycle(backend, journal) as lifecycle:
        lifecycle.seed()
        lifecycle.create()
        lifecycle.stage()
        lifecycle.run(_launch(events))
        assert lifecycle.export() is None
    assert journal.entries == [] and backend.runtime.volumes == set()


def test_an_uncertain_removal_is_reported_and_its_entry_stays_open():
    events: list[tuple] = []
    backend = FakeBackend(events, uncertain=frozenset({ObjectKind.PROJECTION}))
    journal = FakeJournal(events)
    lifecycle = _full(backend, journal, events)
    (projection,) = (obj for obj in journal.entries)
    assert projection.kind is ObjectKind.PROJECTION
    assert lifecycle.uncertain == [projection]
    assert lifecycle.open_objects == (projection,)
    assert not any(e[:2] == ("journal", "close") and "projection" in e[2] for e in events)


def test_a_removal_that_raises_is_uncertain_not_done():
    events: list[tuple] = []
    backend = FakeBackend(events, fail="remove volume")
    journal = FakeJournal(events)
    lifecycle = _full(backend, journal, events)
    assert [obj.name for obj in lifecycle.uncertain] == ["vol"]
    assert [obj.name for obj in journal.entries] == ["vol"]


def test_a_journal_that_refuses_a_removal_intent_removes_nothing():
    events: list[tuple] = []
    backend = FakeBackend(events)
    journal = FakeJournal(events)
    with _lifecycle(backend, journal, change=False) as lifecycle:
        lifecycle.seed()
        lifecycle.create()
        lifecycle.stage()
        journal.refuse = "removals"
        lifecycle.run(_launch(events))
    after_run = events[_index(events, "executor", "run") :]
    assert not any(event[1] in ("rm", "rm uncertain") for event in after_run)
    assert {obj.name for obj in lifecycle.uncertain} == {"staging", "projection", "vol", "launch"}


def test_a_stager_container_not_verified_removed_never_lets_the_launch_container_start():
    events: list[tuple] = []
    backend = FakeBackend(events, uncertain=frozenset({ObjectKind.STAGER_CONTAINER}))
    journal = FakeJournal(events)
    with pytest.raises(SandboxLaunchRefusedError, match="never starts"):
        with _lifecycle(backend, journal) as lifecycle:
            lifecycle.seed()
            lifecycle.create()
            lifecycle.stage()
    assert ("runtime", "start", "launch") not in events
    assert [obj.name for obj in lifecycle.uncertain] == ["stager"]


def _bent(**changes) -> tuple[LifecycleObject, ...]:
    """D13's objects with one rule broken."""
    objects = {obj.kind: obj for obj in container_objects(OWNER, "bind")}
    for kind, replace in changes.items():
        objects[ObjectKind[kind]] = replace(objects[ObjectKind[kind]])
    return tuple(objects.values())


def _with(**fields):
    def replace(obj: LifecycleObject) -> LifecycleObject:
        return LifecycleObject(**{**obj.__dict__, **fields})

    return replace


@pytest.mark.parametrize(
    ("objects", "why"),
    [
        (_bent(STAGER_CONTAINER=_with(network="bridge")), "not created offline"),
        (
            _bent(STAGER_CONTAINER=_with(mounts=("staging", "vol", "projection"))),
            "would mount the projection",
        ),
        (_bent(LAUNCH_CONTAINER=_with(mounts=("staging", "vol"))), "does not mount the projection"),
        (_bent(LAUNCH_CONTAINER=_with(network="")), "names no network position"),
        (
            _bent(
                LAUNCH_CONTAINER=_with(mounts=("staging", "vol", "projection", "/home/operator"))
            ),
            "which no entry of",
        ),
        (_bent(VOLUME=_with(nonce="n-proj")), "no name and nonce of its own"),
        (_bent(VOLUME=_with(mounts=("x",))), "mounts nothing"),
        (
            _bent(VOLUME=_with(owner=LaunchOwner("run-2", 268, "FIX", "FIX-1"))),
            "another launch",
        ),
        (
            tuple(
                o
                for o in container_objects(OWNER, "bind")
                if o.kind is not ObjectKind.STAGER_CONTAINER
            ),
            "staged only by a stager container",
        ),
        (
            (
                *container_objects(OWNER, "bind"),
                LifecycleObject(OWNER, ObjectKind.EXPORTER, "early", "n-early", NETWORK_NONE),
            ),
            "not made at this step",
        ),
    ],
)
def test_the_create_step_refuses_objects_that_break_d13_before_journaling_any(objects, why):
    events: list[tuple] = []
    backend = FakeBackend(events)
    backend.objects_override = objects
    journal = FakeJournal(events)
    with pytest.raises(SandboxLaunchRefusedError, match=why):
        with _lifecycle(backend, journal) as lifecycle:
            lifecycle.seed()
            lifecycle.create()
    assert journal.entries == [] and backend.made == []
    assert not any(event[0] in ("journal", "runtime") for event in events)


def test_a_step_out_of_order_is_refused():
    events: list[tuple] = []
    with _lifecycle(FakeBackend(events), FakeJournal(events)) as lifecycle:
        with pytest.raises(SandboxLaunchRefusedError, match="comes after the stage step"):
            lifecycle.run(_launch(events))
        lifecycle.seed()
        with pytest.raises(SandboxLaunchRefusedError, match="comes after the create step"):
            lifecycle.stage()
        with pytest.raises(
            SandboxLaunchRefusedError, match="after the export step, not after seed"
        ):
            lifecycle.saved()
    assert ("executor", "run") not in events


def test_verifys_outcome_is_saved_only_after_the_export_step():
    events: list[tuple] = []
    backend = FakeBackend(events)
    journal = FakeJournal(events)
    with _lifecycle(backend, journal) as lifecycle:
        lifecycle.seed()
        lifecycle.create()
        lifecycle.stage()
        lifecycle.run(_launch(events))
        with pytest.raises(SandboxLaunchRefusedError, match="not after run"):
            lifecycle.saved()
    # The export never ran, so the volume is still there, journaled.
    assert backend.runtime.volumes == {"vol"}
    assert [obj.name for obj in journal.entries] == ["vol"]


def test_without_a_journal_a_backend_that_makes_an_object_is_refused_before_it_makes_one():
    events: list[tuple] = []
    backend = FakeBackend(events)
    with pytest.raises(SandboxLaunchRefusedError, match="no lifecycle journal exists"):
        with _lifecycle(backend, NO_JOURNAL) as lifecycle:
            lifecycle.seed()
            lifecycle.create()
    assert backend.made == []
    assert not any(event[0] == "runtime" for event in events)


@pytest.mark.parametrize("change", [True, False])
def test_bwrap_does_nothing_but_the_run(change):
    """Seed, create, stage, the clean-up and reconcile do nothing for bwrap (D13)."""
    events: list[tuple] = []
    with LaunchLifecycle(
        BWRAP, NO_JOURNAL, OWNER, BWRAP.runtime_check(), change_producing=change
    ) as lc:
        assert lc.seed() == Seed()
        lc.create()
        lc.stage()
        assert lc.run(_launch(events)) == "result"
        assert lc.export() is None
        lc.saved()
    assert events == [("executor", "run")]
    assert lc.uncertain == [] and lc.open_objects == ()
    entry = enter(BWRAP, NO_JOURNAL, lambda runtime: ProbeResult(True, "", "x"))
    assert entry.runtime.backend is BackendKind.BWRAP and entry.still_open == ()


# -- the engine's sequence (ADR 0013 §7) --------------------------------------------


def _required():
    cfg = default_config()
    cfg.sandbox.mode = "required"
    return cfg


@pytest.fixture
def through_fake(monkeypatch):
    """The engine's backend is a :class:`FakeBackend` and its journal a :class:`FakeJournal`.

    The probe the engine caches (``probe_sandbox``) records itself too, and
    so does every agent the scripted provider runs.
    """
    events: list[tuple] = []
    backend = FakeBackend(events)
    journal = FakeJournal(events)
    monkeypatch.setattr(engine_module, "launch_backend", lambda: backend)
    monkeypatch.setattr(ControllerEngine, "_lifecycle_journal", lambda self: journal)

    def probe() -> ProbeResult:
        events.append(("engine", "probe"))
        return ProbeResult(True, "", "fake")

    monkeypatch.setattr(engine_module, "probe_sandbox", probe)
    return events, backend, journal


def _recording(events: list[tuple]):
    def agent(req):
        events.append(("agent", req.phase))
        return implement(req)

    return agent


def test_the_engine_enters_then_runs_a_launch_through_every_step_in_order(
    tmp_state_dir, fake_github, through_fake
):
    events, backend, journal = through_fake
    eng = make_engine(
        tmp_state_dir, _recording(events), github=fake_github, cfg=_required(), origin=True
    )
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    assert events == []  # INITIALIZING launches nothing, so it is not gated
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    # At the command's entry: the runtime check, reconciliation, the probe, and
    # nothing of a launch before them.
    assert events[:3] == [("runtime", "check"), ("runtime", "reconcile", ()), ("engine", "probe")]
    seeded = _index(events, "host", "seed")
    assert seeded == 3
    created = _index(
        events, "journal", "creates", ("staging", "projection", "vol", "stager", "launch")
    )
    launch_start = _index(events, "runtime", "start", "launch")
    agent = _index(events, "agent", "ANALYZE_EXECUTE")
    release = _index(events, "journal", "removals", ("staging", "projection", "launch"))
    assert (
        seeded
        < created
        < _index(events, "runtime", "rm", "stager")
        < launch_start
        < agent
        < release
    )
    # Export and verify are #269's to wire: the change-producing launch's volume
    # is kept, journaled, never removed before a verify outcome is saved.
    assert [obj.name for obj in journal.entries] == ["vol"]
    assert ("runtime", "rm", "vol") not in events
    # The next command's entry reconciles it, before its probe.
    backend.has_exporter = False
    events.clear()
    with pytest.raises(ControlResultValidationError):
        eng.step()  # REVIEW's agent answers nothing; the entry has run by then
    assert events[:5] == [
        ("runtime", "check"),
        ("journal", "removals", ("vol",)),
        ("runtime", "reconcile", ("vol",)),
        ("runtime", "rm", "vol"),
        ("journal", "close", ("vol",)),
    ]
    assert ("engine", "probe") not in events  # cached for the same runtime report
    assert journal.entries == []


def test_the_engine_releases_after_an_interrupted_launch(tmp_state_dir, fake_github, through_fake):
    events, backend, journal = through_fake

    def interrupted(req):
        events.append(("agent", req.phase))
        raise KeyboardInterrupt

    eng = make_engine(tmp_state_dir, interrupted, github=fake_github, cfg=_required(), origin=True)
    eng.step()
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    agent = _index(events, "agent", "ANALYZE_EXECUTE")
    assert _index(events, "journal", "removals", ("staging", "projection", "launch")) > agent
    assert backend.runtime.containers == {}
    assert [obj.name for obj in journal.entries] == ["vol"]


def test_the_engine_refuses_a_launch_whose_objects_no_journal_records(
    tmp_state_dir, fake_github, monkeypatch
):
    """Until #272's journal exists, a backend that makes an object is refused at create."""
    events: list[tuple] = []
    backend = FakeBackend(events)
    monkeypatch.setattr(engine_module, "launch_backend", lambda: backend)
    eng = make_engine(
        tmp_state_dir, _recording(events), github=fake_github, cfg=_required(), origin=True
    )
    eng.step()
    with pytest.raises(SandboxLaunchRefusedError, match="no lifecycle journal exists"):
        eng.step()
    assert backend.made == [] and ("agent", "ANALYZE_EXECUTE") not in events
    assert not any(
        event[0] == "runtime" and event[1] != "check" and event[1] != "reconcile"
        for event in events
    )


def test_a_refused_runtime_check_leaves_the_run_byte_identical(
    tmp_state_dir, fake_github, through_fake
):
    events, backend, journal = through_fake
    eng = make_engine(
        tmp_state_dir, _recording(events), github=fake_github, cfg=_required(), origin=True
    )
    eng.step()
    before = Path(eng.paths.state_file).read_bytes()
    backend.fail = "runtime check"
    with pytest.raises(SandboxLaunchRefusedError):
        eng.step()
    assert Path(eng.paths.state_file).read_bytes() == before
    assert events == [("runtime", "check"), ("fail", "runtime check")]
    assert load_state(eng.paths.state_file).phase is Phase.ANALYZE_EXECUTE


def test_under_disabled_the_backend_is_never_consulted(tmp_state_dir, fake_github, through_fake):
    events, _backend, _journal = through_fake
    eng = make_engine(tmp_state_dir, _recording(events), github=fake_github, origin=True)
    eng.step()
    assert eng.step().next_phase == "REVIEW"
    assert events == [("agent", "ANALYZE_EXECUTE")]


def test_a_scheduler_workers_engine_runs_the_same_sequence(tmp_path, fake_github, through_fake):
    """A worker runs the engine itself on its issue's namespace (ADR 0008 D9.2):
    its entry and its launch take the same steps, in the same order."""
    events, _backend, journal = through_fake
    repo = git_repo(tmp_path / "repo")
    common = Path(os.path.realpath(repo / ".git"))
    eng = make_engine(
        tmp_path / "unused",
        _recording(events),
        github=fake_github,
        cfg=_required(),
        workdir=repo,
        origin=True,
    )
    eng.bind_issue_namespace(issue_namespace(common, 2))
    eng.new_run(EPIC, ISSUE)
    assert eng.paths.state_file.is_relative_to(common)
    eng.step()
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert events[:5] == [
        ("runtime", "check"),
        ("runtime", "reconcile", ()),
        ("engine", "probe"),
        ("host", "seed"),
        ("journal", "creates", ("staging", "projection", "vol", "stager", "launch")),
    ]
    assert _index(events, "agent", "ANALYZE_EXECUTE") < _index(
        events, "journal", "removals", ("staging", "projection", "launch")
    )
    assert [obj.name for obj in journal.entries] == ["vol"]
