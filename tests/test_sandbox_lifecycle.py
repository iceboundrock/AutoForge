"""The engine's sequence of a sandbox backend's lifecycle, on a fake backend (ADR 0013 D13, #268).

No runtime is needed. :class:`FakeRuntime` is a container runtime that, as
Docker and Podman do, accepts a bind, a volume or a network only in a
container's create call; every other call it has names a container and
nothing it could add to it; and it refuses to remove a volume that a
container still references, running or not. :class:`FakeBackend` is a container-shaped
backend over it (both staging forms, ``bind`` and ``copy``), whose run step
prepares a command to run in the launch container its handle names;
:class:`FakeJournal` records what the engine journals, and
:class:`FakeSource` is the controller's verify step and its save. They share
one event list, so a test reads the order across all of them. The engine's
own use of the sequence is at the end of this module, after a launch
through a real provider adapter and the real executor.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import autoforge.engine as engine_module
from autoforge.candidate_artifact import ARTIFACT_MANIFEST, ARTIFACT_PACK, CandidateManifest
from autoforge.candidate_source import CandidateReceipts
from autoforge.claims import render_progress_marker
from autoforge.config import ProfileConfig, default_config
from autoforge.effects import EntryObservation
from autoforge.engine import ControllerEngine
from autoforge.errors import (
    ControlResultValidationError,
    ExecutionError,
    SandboxCleanupUncertainError,
    SandboxLaunchRefusedError,
    SandboxPolicyError,
)
from autoforge.executor import ExecutionRequest, execute
from autoforge.git_input import SEED_MANIFEST_NAME, LaunchGitInput, seed_manifest_digest
from autoforge.providers import AgentRequest, OpenCodeProvider, ProviderRequirements
from autoforge.run_namespace import issue_namespace
from autoforge.sandbox import BackendKind, LaunchDescription, NetworkPosition, SandboxFacts
from autoforge.sandbox_backend import (
    BWRAP,
    NETWORK_NONE,
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
from autoforge.sandbox_lifecycle import (
    NO_CANDIDATE_SOURCE,
    NO_JOURNAL,
    Exported,
    LaunchLifecycle,
    Verified,
    enter,
    reconcile,
)
from autoforge.sandbox_policy import LaunchClass, LaunchFacts, SandboxPlan
from autoforge.state import load_state
from autoforge.transitions import Phase
from tests.conftest import (
    EPIC,
    GIT_IDENT,
    ISSUE,
    PR,
    analyze_payload,
    block,
    commit_in,
    connect_origin,
    git_out,
    git_repo,
    implement,
    isolated_git,
    make_engine,
    review_result,
)

OWNER = LaunchOwner(run="run-1", issue=268, phase="ANALYZE_EXECUTE", launch="ANALYZE_EXECUTE-1")
# The revision the engine bound for the launch (#269): what the seed is built from.
GIT_INPUT = LaunchGitInput(
    phase="ANALYZE_EXECUTE",
    run="run-1",
    issue=268,
    launch="ANALYZE_EXECUTE-1",
    repository="owner/repo",
    object_format="sha1",
    seed="a" * 40,
    lower="a" * 40,
)
# ``export_dir`` is the export directory the engine chose for the launch (#269):
# the fake's exporter fills it under that name.
INPUTS = LaunchInputs(
    OWNER,
    workspace="/work/268",
    read_context="/state/read-context/1",
    git_input=GIT_INPUT,
    export_dir="out",
)


def _bound(seed: Seed, inputs: LaunchInputs = INPUTS) -> LaunchInputs:
    """``inputs`` as the seed step's end completes them: the seed's manifest bound."""
    assert inputs.git_input is not None
    return dataclasses.replace(inputs, git_input=inputs.git_input.with_seed_manifest(seed.identity))


RUNTIME = RuntimeReport(
    BackendKind.CONTAINER, "podman", "5.2.0", True, "unix:///run/user/1000/podman.sock", "store-1"
)
# What the plan step compiled, as a provider's ``SandboxPlan.describe`` hands it on.
PLANNED = LaunchDescription(mounts=())
_linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the executor's containment is Linux-only here"
)

# The fake runtime's ``exec``: it runs the command as running in the
# container argv names, and says so in the command's environment.
RUNTIME_EXEC = (
    "import os, sys\n"
    "os.environ['FAKE_CONTAINER'], os.environ['FAKE_NONCE'] = sys.argv[1], sys.argv[2]\n"
    "os.execv(sys.argv[3], sys.argv[3:])\n"
)


class FakeRuntime:
    """A rootless runtime's CLI as the lifecycle sees it: what was called, in order.

    A bind, a volume and a network exist only as arguments of
    :meth:`create_container`; ``start``, ``exec``, ``cp``, ``kill`` and ``rm``
    take a container's name and nothing they could configure it with. As
    Docker and Podman do, ``rm`` refuses a volume that a container still
    references, even a stopped one (:class:`VolumeInUse`), and removes nothing.
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

    def kill(self, name: str) -> None:
        self.events.append(("runtime", "kill", name))
        if name in self.containers:
            self.containers[name]["running"] = False

    def rm(self, name: str) -> None:
        users = sorted(c for c, spec in self.containers.items() if name in spec["mounts"])
        if name in self.volumes and users:
            self.events.append(("runtime", "rm refused", name, tuple(users)))
            raise VolumeInUse(f"fake: volume {name} is in use by {', '.join(users)}")
        self.events.append(("runtime", "rm", name))
        self.containers.pop(name, None)
        self.volumes.discard(name)


class VolumeInUse(RuntimeError):
    """The fake runtime's refusal to remove a volume a container references."""


CONTAINER_FACTS = SandboxFacts(
    enforced=True,
    backend="container",
    teardown="removed with the container's PID namespace, uncounted",
    network="bridge",
    runtime="podman",
    runtime_version="5.2.0",
    rootless=True,
    staging="bind",
)


class ContainerLaunch:
    """The fake backend's prepared launch: the command, run in one container by name.

    Its argv is the fake runtime's ``exec`` into the container the handle
    names; the spawn finds that container running; ``close()`` ends its PID
    namespace (ADR 0013 D13), as killing the attached client would not.
    """

    def __init__(self, backend: FakeBackend, container: LifecycleObject, command, env) -> None:
        self.backend = backend
        self.container = container
        self.argv = [sys.executable, "-c", RUNTIME_EXEC, container.name, container.nonce, *command]
        self.spawn_env = dict(env)
        self.pass_fds: tuple[int, ...] = ()
        self.preexec_fn = _nothing

    def spawned(self) -> None:
        running = self.backend.runtime.containers[self.container.name]["running"]
        self.backend.events.append(("executor", "spawned", self.container.name, running))

    def spawn_error(self, exc: BaseException) -> SandboxLaunchRefusedError:
        return SandboxLaunchRefusedError(f"fake: the exec failed ({exc})")

    def collect(self) -> None:
        return None

    def outcome(self, *, returncode, ended_by_executor, stderr) -> SandboxFacts:
        return CONTAINER_FACTS

    def close(self) -> None:
        self.backend.runtime.kill(self.container.name)


def _nothing() -> None:
    return None


def _network(launch: LaunchDescription) -> str:
    """The launch container's network for the compiled launch's position (D9)."""
    return "bridge" if launch.network is NetworkPosition.HOST else NETWORK_NONE


class FakeBackend:
    """A container-shaped backend over :class:`FakeRuntime` (the bind or the copy-in form).

    ``fail`` names one operation that raises a setup failure; ``uncertain``
    the kinds whose removal it cannot verify; ``deny_plan`` makes the plan
    step refuse, as the policy compiler does.
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
        self.deny_plan = False
        self.made: list[str] = []
        self.seeded: list[tuple[LaunchInputs, LaunchDescription]] = []
        self.seeds: list[Seed] = []
        # Called with each object as the create step makes it: a test swaps
        # the seed's manifest there, after the git input bound it (#269).
        self.on_make = None
        self.prepared: list[tuple[LaunchHandle | None, list[str]]] = []
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
        gone = []
        for entry in entries:
            try:
                self.runtime.rm(entry.name)
            except VolumeInUse:
                continue
            gone.append(entry)
        return tuple(gone)

    def probe(self, *, home, environ) -> ProbeResult:
        self.events.append(("runtime", "probe"))
        return ProbeResult(True, "", "fake")

    def plan(self, request) -> LaunchDescription:
        self.events.append(("host", "plan"))
        if self.deny_plan:
            raise SandboxPolicyError("/home/operator/.ssh", "a forbidden source", "fake")
        return LaunchDescription(mounts=(), network=request.network)

    def seed(self, inputs: LaunchInputs, launch: LaunchDescription) -> Seed:
        """A seed directory holding a manifest of the bound revision (#292 builds the real one)."""
        self.events.append(("host", "seed"))
        self.seeded.append((inputs, launch))
        self._maybe_fail("seed")
        assert inputs.git_input is not None, "a container launch is seeded from its git input"
        directory = tempfile.mkdtemp(prefix="autoforge-fake-seed-")
        manifest = {"seed": inputs.git_input.seed, "launch": inputs.owner.launch}
        Path(directory, SEED_MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        self.seeds.append(Seed(seed_manifest_digest(directory), directory))
        return self.seeds[-1]

    def discard_seed(self, seed: Seed) -> None:
        self.events.append(("host", "discard seed"))
        shutil.rmtree(seed.directory, ignore_errors=True)

    def objects(
        self, inputs: LaunchInputs, seed: Seed, launch: LaunchDescription
    ) -> tuple[LifecycleObject, ...]:
        if self.objects_override is not None:
            return self.objects_override
        return container_objects(inputs.owner, self.staging, _network(launch))

    def make(self, obj: LifecycleObject, seed: Seed, launch: LaunchDescription) -> None:
        assert seed == self.seeds[-1]
        if self.on_make is not None:
            self.on_make(obj, seed)
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

    def prepare(self, launch, handle, command, *, cwd, env):
        self.prepared.append((handle, list(command)))
        container = handle.find(ObjectKind.LAUNCH_CONTAINER) if handle is not None else None
        if container is None:
            raise SandboxLaunchRefusedError("fake: no launch container was made for this launch")
        self.events.append(("executor", "prepare", container.name))
        return ContainerLaunch(self, container, command, env)

    def exporter(self, handle: LaunchHandle) -> tuple[LifecycleObject, ...]:
        if not self.has_exporter:
            return ()
        owner = handle.inputs.owner
        out = handle.inputs.export_dir
        return (
            LifecycleObject(
                owner, ObjectKind.EXPORTER, "exporter", "n-exp", NETWORK_NONE, ("vol", out)
            ),
            LifecycleObject(owner, ObjectKind.EXPORT_DIR, out, "n-out"),
        )

    def export(self, objects: tuple[LifecycleObject, ...], handle: LaunchHandle) -> Artifact:
        (exporter,) = (obj for obj in objects if obj.kind is ObjectKind.EXPORTER)
        assert handle.find(ObjectKind.VOLUME) is not None
        self.runtime.create_container(
            exporter.name, network=exporter.network, mounts=exporter.mounts
        )
        self.runtime.start(exporter.name)
        self.runtime.exec(exporter.name, "exporter")
        self._maybe_fail("export")
        return Artifact(handle.inputs.export_dir)

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


def container_objects(
    owner: LaunchOwner, staging: str, network: str = "bridge"
) -> tuple[LifecycleObject, ...]:
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
            network,
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
        self.created: list[LifecycleObject] = []
        self.refuse = ""

    def open_entries(self) -> tuple[LifecycleObject, ...]:
        return tuple(self.entries)

    def record_creates(self, runtime, objects) -> None:
        assert runtime == RUNTIME
        self.events.append(("journal", "creates", tuple(obj.name for obj in objects)))
        self.entries.extend(objects)
        self.created.extend(objects)

    def record_removals(self, objects) -> None:
        if self.refuse == "removals":
            raise OSError("fake: the journal cannot be written")
        self.events.append(("journal", "removals", tuple(obj.name for obj in objects)))

    def close_entries(self, objects) -> None:
        self.events.append(("journal", "close", tuple(obj.name for obj in objects)))
        for obj in objects:
            self.entries.remove(obj)


class FakeSource:
    """The controller's verify step and its save: the worktree's ``rev``, or ``verdict``.

    ``rev`` is ``HEAD`` unless a test makes the step accept another commit.
    """

    def __init__(self, events: list[tuple], verdict: Verified | None = None) -> None:
        self.events = events
        self.verdict = verdict
        self.rev = "HEAD"
        self.accepted: list[str] = []

    def verify(self, exported: Exported, handle: LaunchHandle) -> Verified:
        directory = exported.artifact.directory if exported.artifact is not None else None
        self.events.append(("engine", "verify", directory, handle.inputs.owner.launch))
        if exported.artifact is None:
            return Verified(refusal=f"the launch's export failed: {exported.failure}")
        if self.verdict is not None:
            return self.verdict
        head = subprocess.run(
            ["git", "-C", handle.inputs.workspace, "rev-parse", f"{self.rev}^{{commit}}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.accepted.append(head)
        return Verified(candidate=head)

    def save(self, outcome: Verified) -> None:
        self.events.append(("engine", "save", outcome.accepted))


def _index(events: list[tuple], *event) -> int:
    return events.index(tuple(event))


def _first(events: list[tuple], predicate) -> int:
    return next(i for i, event in enumerate(events) if predicate(event))


def _lifecycle(backend, journal, *, change: bool = True) -> LaunchLifecycle:
    return LaunchLifecycle(backend, journal, INPUTS, RUNTIME, change_producing=change)


def _container(handle: LaunchHandle) -> str:
    found = handle.find(ObjectKind.LAUNCH_CONTAINER)
    return found.name if found is not None else ""


def _launch(lifecycle: LaunchLifecycle, events: list[tuple], *, interruption=None):
    """The provider's launch as its ``SandboxPlan.describe`` makes it: the plan
    compiled (:data:`PLANNED`), the environment established for it, then the run."""

    def provider() -> str:
        handle = lifecycle.establish(PLANNED)
        events.append(("executor", "run", _container(handle)))
        if interruption is not None:
            raise interruption("the run step was interrupted")
        return "result"

    return lifecycle.launch(provider)


def _full(backend, journal, events, *, change: bool = True) -> LaunchLifecycle:
    """Every step, a verify the test stands in for, and its outcome saved."""
    with _lifecycle(backend, journal, change=change) as lifecycle:
        assert _launch(lifecycle, events) == "result"
        exported = lifecycle.export()
        assert exported is not None
        events.append(("engine", "verify", exported.artifact))
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

    handed: list[LifecycleObject] = []

    class Partial(FakeBackend):
        def reconcile(self, entries, runtime):
            handed.extend(entries)
            return entries[:1]

    still_open = reconcile(Partial(events), journal, RUNTIME)
    # The backend gets them in removal order: each container before what it mounts.
    assert [obj.name for obj in handed] == ["launch", "stager", "projection", "staging", "vol"]
    assert still_open == tuple(handed[1:])
    assert ("journal", "close", ("launch",)) in events
    assert journal.entries == [obj for obj in entries if obj.name != "launch"]


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
    # The seed step took the launch's inputs and the plan's compiled launch.
    assert backend.seeded == [(INPUTS, PLANNED)]
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
    # The run, in the launch container the handle names; then the release:
    # what holds the credential goes, the volume stays.
    run = _index(events, "executor", "run", "launch")
    release_intent = _index(events, "journal", "removals", ("launch", "projection", *source))
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
    assert _index(events, "engine", "verify", Artifact("out")) < saved
    assert journal.entries == [] and lifecycle.uncertain == []
    assert backend.runtime.containers == {} and backend.runtime.volumes == set()
    assert events[-1] == ("host", "discard seed")


def test_the_handle_names_the_objects_made_for_the_launch_and_still_in_place():
    events: list[tuple] = []
    backend = FakeBackend(events)
    with _lifecycle(backend, FakeJournal(events)) as lifecycle:
        handles: list[LaunchHandle] = []
        lifecycle.launch(lambda: handles.append(lifecycle.establish(PLANNED)))
        lifecycle.export()
    (handle,) = handles
    assert handle is lifecycle.handle
    assert handle.seed == backend.seeds[-1]
    assert handle.inputs == _bound(handle.seed)
    # The stager container was removed before the launch container started.
    assert {obj.name for obj in handle.objects} == {"staging", "projection", "vol", "launch"}
    assert all(obj.owner == OWNER for obj in handle.objects)


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
            _launch(lifecycle, events)
    assert events == [("host", "seed"), ("fail", "seed")]
    assert journal.entries == []


# -- the launch's git input at the seed step (ADR 0013 D4, D19; #269) -----------------


def _bound_lifecycle(backend, journal, events, *, staging_change: bool = True) -> LaunchLifecycle:
    """A lifecycle whose ``on_seed`` records the completed git input, as the engine saves it."""

    def on_seed(git_input: LaunchGitInput) -> None:
        events.append(("engine", "bind seed", git_input.seed_manifest_digest))

    return LaunchLifecycle(
        backend, journal, INPUTS, RUNTIME, change_producing=staging_change, on_seed=on_seed
    )


@pytest.mark.parametrize("staging", ["bind", "copy"])
def test_the_git_input_is_bound_at_the_end_of_the_seed_step_before_any_journal_entry(staging):
    events: list[tuple] = []
    backend = FakeBackend(events, staging=staging)
    journal = FakeJournal(events)
    with _bound_lifecycle(backend, journal, events) as lifecycle:
        _launch(lifecycle, events)
        lifecycle.export()
        lifecycle.saved()
    (seed,) = backend.seeds
    bound = _index(events, "engine", "bind seed", seed.identity)
    assert _index(events, "host", "seed") < bound < _first(events, lambda e: e[0] == "journal")
    # The seed step got the bare input; every later step the completed one.
    ((inputs, _),) = backend.seeded
    assert inputs == INPUTS and inputs.git_input.seed_manifest_digest == ""
    handle = lifecycle.handle
    assert handle is not None and handle.inputs == _bound(seed)
    assert handle.inputs.git_input.seed == GIT_INPUT.seed
    assert handle.inputs.git_input.lower == GIT_INPUT.lower


def _swap(seed: Seed, content: dict) -> None:
    Path(seed.directory, SEED_MANIFEST_NAME).write_text(json.dumps(content), encoding="utf-8")


@pytest.mark.parametrize(
    "content",
    [
        {"seed": "b" * 40, "launch": "ANALYZE_EXECUTE-1"},
        # The same commits with another tag set (D11): another manifest.
        {"seed": "a" * 40, "launch": "ANALYZE_EXECUTE-1", "tags": {"refs/tags/v1": "c" * 40}},
    ],
)
def test_a_seed_swapped_after_the_input_was_bound_never_reaches_a_container_bind_form(content):
    """Bind form: refused before the staging root is written, with nothing
    journaled or made, and the stager never runs."""
    events: list[tuple] = []
    backend = FakeBackend(events, staging="bind")
    journal = FakeJournal(events)

    def on_seed(git_input: LaunchGitInput) -> None:
        events.append(("engine", "bind seed", git_input.seed_manifest_digest))
        _swap(backend.seeds[-1], content)

    with pytest.raises(SandboxLaunchRefusedError, match="not handed on to the create step"):
        with LaunchLifecycle(
            backend, journal, INPUTS, RUNTIME, change_producing=True, on_seed=on_seed
        ) as lifecycle:
            _launch(lifecycle, events)
    assert not any(event[0] in ("journal", "runtime") for event in events)
    assert ("host", "write staging root", "staging") not in events
    assert backend.made == [] and journal.entries == []


def test_a_seed_swapped_after_the_input_was_bound_never_reaches_a_container_copy_form():
    """Copy-in form: refused before the copy-in, the stager never runs, and what
    was made is removed."""
    events: list[tuple] = []
    backend = FakeBackend(events, staging="copy")
    journal = FakeJournal(events)

    def swap_at_the_last_create(obj: LifecycleObject, seed: Seed) -> None:
        if obj.kind is ObjectKind.LAUNCH_CONTAINER:
            _swap(seed, {"seed": "b" * 40})

    backend.on_make = swap_at_the_last_create
    with pytest.raises(SandboxLaunchRefusedError, match="not handed on to the stager container"):
        with _bound_lifecycle(backend, journal, events) as lifecycle:
            _launch(lifecycle, events)
    assert not any(event[:2] in (("runtime", "cp"), ("runtime", "exec")) for event in events)
    assert ("runtime", "start", "stager") not in events
    assert journal.entries == [] and backend.runtime.containers == {}
    assert backend.runtime.volumes == set()


def test_a_seed_whose_step_names_another_manifest_is_refused_before_any_journal_entry():
    events: list[tuple] = []
    backend = FakeBackend(events)
    journal = FakeJournal(events)
    original = backend.seed

    def misnamed(inputs, launch):
        seed = original(inputs, launch)
        return Seed("sha256:" + "0" * 64, seed.directory)

    backend.seed = misnamed  # type: ignore[method-assign]
    with pytest.raises(SandboxLaunchRefusedError, match="names the manifest"):
        with _bound_lifecycle(backend, journal, events) as lifecycle:
            _launch(lifecycle, events)
    assert ("engine", "bind seed") not in [event[:2] for event in events]
    assert journal.entries == [] and backend.made == []


def test_a_seed_for_a_launch_with_no_git_input_is_refused_before_any_journal_entry():
    events: list[tuple] = []
    backend = FakeBackend(events)
    journal = FakeJournal(events)
    bare = dataclasses.replace(INPUTS, git_input=None)
    original = backend.seed

    def seed_anyway(inputs, launch):
        return original(INPUTS, launch)

    backend.seed = seed_anyway  # type: ignore[method-assign]
    with pytest.raises(SandboxLaunchRefusedError, match="carries no git input"):
        with LaunchLifecycle(backend, journal, bare, RUNTIME, change_producing=True) as lifecycle:
            _launch(lifecycle, events)
    assert journal.entries == [] and backend.made == []


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
            _launch(lifecycle, events)
    assert not any(event[:2] == ("executor", "run") for event in events)
    # The launch container never starts after a failed stage.
    assert ("runtime", "start", "launch") not in events
    # Both parts of the clean-up ran, with no export: every entry closed, the
    # volume included, every object gone.
    assert not any(
        event[:2] == ("journal", "creates") and "exporter" in event[2] for event in events
    )
    assert journal.entries == [] and lifecycle.uncertain == []
    assert backend.runtime.containers == {} and backend.runtime.volumes == set()


def test_an_environment_that_failed_is_never_made_again():
    events: list[tuple] = []
    backend = FakeBackend(events, fail="volume")
    journal = FakeJournal(events)

    def provider() -> None:
        with pytest.raises(SandboxLaunchRefusedError, match="volume failed"):
            lifecycle.establish(PLANNED)
        # A provider that plans again after a refusal gets a refusal, not a second create.
        lifecycle.establish(PLANNED)

    with pytest.raises(SandboxLaunchRefusedError, match="not made again"):
        with _lifecycle(backend, journal) as lifecycle:
            lifecycle.launch(provider)
    assert [event for event in events if event[:2] == ("journal", "creates")] == [
        ("journal", "creates", ("staging", "projection", "vol", "stager", "launch"))
    ]
    assert journal.entries == []


def test_a_later_process_of_the_launch_runs_in_the_environment_already_made():
    """Pi's auth check, then Pi: each is planned, and both run in one environment."""
    events: list[tuple] = []
    backend = FakeBackend(events)
    journal = FakeJournal(events)
    with _lifecycle(backend, journal, change=False) as lifecycle:

        def provider() -> tuple[LaunchHandle, LaunchHandle]:
            return lifecycle.establish(PLANNED), lifecycle.establish(PLANNED)

        first, second = lifecycle.launch(provider)
    assert first is second
    assert len([event for event in events if event[:2] == ("journal", "creates")]) == 1
    assert backend.seeded == [(INPUTS, PLANNED)]

    events.clear()
    other = LaunchDescription(mounts=(), network=NetworkPosition.ISOLATED)
    with pytest.raises(SandboxLaunchRefusedError, match="created with host"):
        with _lifecycle(FakeBackend(events), FakeJournal(events), change=False) as lifecycle:
            lifecycle.launch(lambda: (lifecycle.establish(PLANNED), lifecycle.establish(other)))


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, RuntimeError])
def test_the_release_follows_the_run_on_every_path(interruption):
    events: list[tuple] = []
    backend = FakeBackend(events)
    journal = FakeJournal(events)
    with pytest.raises(interruption):
        with _lifecycle(backend, journal) as lifecycle:
            _launch(lifecycle, events, interruption=interruption)
    run_at = _index(events, "executor", "run", "launch")
    assert _index(events, "journal", "removals", ("launch", "projection", "staging")) > run_at
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
        _launch(lifecycle, events)
        exported = lifecycle.export()
        assert exported is not None and exported.artifact == Artifact("out")
        # The outcome names the export directory the lifecycle journaled (#269).
        assert exported.origin in journal.created
        assert exported.origin.kind is ObjectKind.EXPORT_DIR and exported.origin.name == "out"
        # The engine leaves before it saved verify's outcome.
    assert backend.runtime.volumes == {"vol"}
    assert {obj.name for obj in journal.entries} == {"vol", "out"}


def test_a_failed_export_is_an_outcome_and_keeps_the_volume_until_it_is_saved():
    events: list[tuple] = []
    backend = FakeBackend(events, fail="export")
    journal = FakeJournal(events)
    with _lifecycle(backend, journal) as lifecycle:
        _launch(lifecycle, events)
        exported = lifecycle.export()
        assert exported is not None and exported.artifact is None
        assert "export failed" in exported.failure
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
        _launch(lifecycle, events)
        assert ("runtime", "rm", "vol") in events
        assert lifecycle.export() is None
    assert not any(event[:3] == ("runtime", "create", "exporter") for event in events)
    assert journal.entries == []


def test_the_fake_runtime_refuses_to_remove_a_volume_a_container_references():
    """As Docker and Podman do, even once the container has stopped (R3-F1)."""
    events: list[tuple] = []
    runtime = FakeRuntime(events)
    runtime.create_volume("vol")
    runtime.create_container("launch", network="bridge", mounts=("vol",))
    runtime.start("launch")
    runtime.kill("launch")
    with pytest.raises(VolumeInUse, match="in use by launch"):
        runtime.rm("vol")
    assert runtime.volumes == {"vol"} and ("runtime", "rm", "vol") not in events
    runtime.rm("launch")
    runtime.rm("vol")
    assert runtime.containers == {} and runtime.volumes == set()


@pytest.mark.parametrize("staging", ["bind", "copy"])
def test_a_launch_with_nothing_to_export_removes_each_container_before_its_volume(staging):
    """REVIEW and UPDATE_EPIC: the release removes the volume too, after the
    launch container that mounts it, so the launch succeeds (R3-F1)."""
    events: list[tuple] = []
    backend = FakeBackend(events, staging=staging)
    journal = FakeJournal(events)
    with _lifecycle(backend, journal, change=False) as lifecycle:
        assert _launch(lifecycle, events) == "result"
        assert lifecycle.export() is None
    source = ("staging",) if staging == "bind" else ()
    intent = _index(events, "journal", "removals", ("launch", "projection", *source, "vol"))
    launch_rm = _index(events, "runtime", "rm", "launch")
    vol_rm = _index(events, "runtime", "rm", "vol")
    closed = _index(events, "journal", "close", ("launch", "projection", *source, "vol"))
    assert _index(events, "executor", "run", "launch") < intent < launch_rm < vol_rm < closed
    assert not any(event[1] == "rm refused" for event in events)
    assert journal.entries == [] and lifecycle.uncertain == []
    assert backend.runtime.containers == {} and backend.runtime.volumes == set()


def _removed_after_their_containers(events: list[tuple]) -> None:
    """Every object a container mounts is removed only after that container."""
    mounted_by: dict[str, set[str]] = {}
    for event in events:
        if event[:2] == ("runtime", "create"):
            for source in event[4]:
                mounted_by.setdefault(source, set()).add(event[2])
    removed: list[str] = [event[2] for event in events if event[1] == "rm"]
    for name in removed:
        for container in mounted_by.get(name, ()):
            assert container in removed, f"{name} removed while {container} was never removed"
            assert removed.index(container) < removed.index(name), (
                f"{name} removed before {container}, which mounts it"
            )


@pytest.mark.parametrize("staging", ["bind", "copy"])
@pytest.mark.parametrize("change", [True, False])
def test_every_removal_takes_each_container_before_what_it_mounts(staging, change):
    events: list[tuple] = []
    backend = FakeBackend(events, staging=staging)
    journal = FakeJournal(events)
    if change:
        _full(backend, journal, events)
    else:
        with _lifecycle(backend, journal, change=False) as lifecycle:
            _launch(lifecycle, events)
            lifecycle.export()
    _removed_after_their_containers(events)
    assert not any(event[1] == "rm refused" for event in events)
    assert journal.entries == [] and backend.runtime.volumes == set()


def test_a_backend_with_no_exporter_skips_the_export_and_then_cleans_up():
    events: list[tuple] = []
    backend = FakeBackend(events, exporter=False)
    journal = FakeJournal(events)
    with _lifecycle(backend, journal) as lifecycle:
        _launch(lifecycle, events)
        assert lifecycle.export() is None
    assert journal.entries == [] and backend.runtime.volumes == set()


def test_a_launch_whose_environment_was_never_made_exports_nothing():
    events: list[tuple] = []
    backend = FakeBackend(events)
    journal = FakeJournal(events)
    with _lifecycle(backend, journal) as lifecycle:
        assert lifecycle.launch(lambda: "no plan") == "no plan"
        assert lifecycle.export() is None
    assert events == [] and journal.entries == []


# -- an uncertain clean-up (D17) is never read as done ------------------------------


def test_an_uncertain_release_replaces_the_result_and_nothing_is_exported():
    events: list[tuple] = []
    backend = FakeBackend(events, uncertain=frozenset({ObjectKind.PROJECTION}))
    journal = FakeJournal(events)
    with pytest.raises(SandboxCleanupUncertainError, match="release") as raised:
        with _lifecycle(backend, journal) as lifecycle:
            _launch(lifecycle, events)
    assert raised.value.objects == ("credential projection projection",)
    # The launch container may still be there: nothing reads the volume, which
    # stays journaled with the projection for reconciliation.
    assert not any(event[:3] == ("journal", "creates", ("exporter", "out")) for event in events)
    assert {obj.name for obj in journal.entries} == {"projection", "vol"}
    assert lifecycle.uncertain == [o for o in journal.entries if o.name == "projection"]
    assert not any(e[:2] == ("journal", "close") and "projection" in e[2] for e in events)


def test_a_launch_container_not_verified_removed_keeps_the_volume_it_mounts():
    """No removal of the volume is issued while its container may exist; the
    credential projection and the staging root still leave the host (R3-F1)."""
    events: list[tuple] = []
    backend = FakeBackend(events, uncertain=frozenset({ObjectKind.LAUNCH_CONTAINER}))
    journal = FakeJournal(events)
    with pytest.raises(SandboxCleanupUncertainError, match="release") as raised:
        with _lifecycle(backend, journal, change=False) as lifecycle:
            _launch(lifecycle, events)
            pytest.fail("a launch whose release is uncertain returned its result")
    assert raised.value.objects == ("launch container launch", "volume vol")
    assert not any(event[1] in ("rm", "rm refused") and event[2] == "vol" for event in events)
    assert ("host", "rm", "projection") in events and ("host", "rm", "staging") in events
    assert {obj.name for obj in journal.entries} == {"launch", "vol"}
    assert backend.runtime.volumes == {"vol"}


def test_an_exporter_not_verified_removed_returns_no_artifact():
    events: list[tuple] = []
    backend = FakeBackend(events, uncertain=frozenset({ObjectKind.EXPORTER}))
    journal = FakeJournal(events)
    with pytest.raises(SandboxCleanupUncertainError, match="export") as raised:
        with _lifecycle(backend, journal) as lifecycle:
            _launch(lifecycle, events)
            lifecycle.export()
            pytest.fail("an artifact an exporter may still write was returned")
    assert raised.value.objects == ("exporter container exporter",)
    # Nothing saved, so the volume and the export directory stay too.
    assert {obj.name for obj in journal.entries} == {"vol", "exporter", "out"}
    assert backend.runtime.volumes == {"vol"}


def test_an_uncertain_final_clean_up_fails_the_launch_and_keeps_its_entry():
    events: list[tuple] = []
    backend = FakeBackend(events, fail="remove volume")
    journal = FakeJournal(events)
    with pytest.raises(SandboxCleanupUncertainError, match="final clean-up") as raised:
        _full(backend, journal, events)
    assert raised.value.objects == ("volume vol",)
    assert [obj.name for obj in journal.entries] == ["vol"]


def test_an_uncertain_removal_never_masks_the_exception_ending_the_launch():
    events: list[tuple] = []
    backend = FakeBackend(events, uncertain=frozenset({ObjectKind.PROJECTION}))
    journal = FakeJournal(events)
    with pytest.raises(KeyboardInterrupt) as raised:
        with _lifecycle(backend, journal) as lifecycle:
            _launch(lifecycle, events, interruption=KeyboardInterrupt)
    (note,) = raised.value.__notes__
    assert "credential projection projection" in note and "reconciliation" in note
    assert {obj.name for obj in journal.entries} == {"projection", "vol"}


def test_a_journal_that_refuses_a_removal_intent_removes_nothing():
    events: list[tuple] = []
    backend = FakeBackend(events)
    journal = FakeJournal(events)
    with pytest.raises(SandboxCleanupUncertainError) as raised:
        with _lifecycle(backend, journal, change=False) as lifecycle:

            def provider() -> None:
                lifecycle.establish(PLANNED)
                events.append(("executor", "run"))
                journal.refuse = "removals"

            lifecycle.launch(provider)
    after_run = events[_index(events, "executor", "run") :]
    assert not any(event[1] in ("rm", "rm uncertain") for event in after_run)
    assert {obj.name for obj in lifecycle.uncertain} == {"staging", "projection", "vol", "launch"}
    assert len(raised.value.objects) == 4


def test_a_stager_container_not_verified_removed_never_lets_the_launch_container_start():
    events: list[tuple] = []
    backend = FakeBackend(events, uncertain=frozenset({ObjectKind.STAGER_CONTAINER}))
    journal = FakeJournal(events)
    with pytest.raises(SandboxLaunchRefusedError, match="never starts"):
        with _lifecycle(backend, journal) as lifecycle:
            _launch(lifecycle, events)
    assert ("runtime", "start", "launch") not in events
    # The stager container may still exist and it mounts the volume, so no
    # removal of the volume is issued: both stay open, uncertain.
    assert [obj.name for obj in lifecycle.uncertain] == ["stager", "vol"]
    assert not any(event[1] in ("rm", "rm refused") and event[2] == "vol" for event in events)
    assert {obj.name for obj in journal.entries} == {"stager", "vol"}


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
            _launch(lifecycle, events)
    assert journal.entries == [] and backend.made == []
    assert not any(event[0] in ("journal", "runtime") for event in events)


def test_a_step_out_of_order_is_refused():
    events: list[tuple] = []
    with _lifecycle(FakeBackend(events), FakeJournal(events)) as lifecycle:
        with pytest.raises(SandboxLaunchRefusedError, match="during its launch, not start"):
            lifecycle.establish(PLANNED)
        with pytest.raises(SandboxLaunchRefusedError, match="comes after the launch step"):
            lifecycle.export()
        with pytest.raises(
            SandboxLaunchRefusedError, match="after the export step, not after start"
        ):
            lifecycle.saved()
    assert events == []
    # Once the launch returned, its environment is released: none is made or handed out.
    with _lifecycle(FakeBackend(events), FakeJournal(events)) as lifecycle:
        _launch(lifecycle, events)
        with pytest.raises(SandboxLaunchRefusedError, match="during its launch, not launch"):
            lifecycle.establish(PLANNED)


def test_verifys_outcome_is_saved_only_after_the_export_step():
    events: list[tuple] = []
    backend = FakeBackend(events)
    journal = FakeJournal(events)
    with _lifecycle(backend, journal) as lifecycle:
        _launch(lifecycle, events)
        with pytest.raises(SandboxLaunchRefusedError, match="not after launch"):
            lifecycle.saved()
    # The export never ran, so the volume is still there, journaled.
    assert backend.runtime.volumes == {"vol"}
    assert [obj.name for obj in journal.entries] == ["vol"]


def test_without_a_journal_a_backend_that_makes_an_object_is_refused_before_it_makes_one():
    events: list[tuple] = []
    backend = FakeBackend(events)
    with pytest.raises(SandboxLaunchRefusedError, match="no lifecycle journal exists"):
        with _lifecycle(backend, NO_JOURNAL) as lifecycle:
            _launch(lifecycle, events)
    assert backend.made == []
    assert not any(event[0] == "runtime" for event in events)


@pytest.mark.parametrize("change", [True, False])
def test_bwrap_does_nothing_but_the_run(change):
    """Seed, create, stage, the clean-up and reconcile do nothing for bwrap (D13)."""
    events: list[tuple] = []
    with LaunchLifecycle(
        BWRAP, NO_JOURNAL, INPUTS, BWRAP.runtime_check(), change_producing=change
    ) as lc:
        assert _launch(lc, events) == "result"
        assert lc.handle == LaunchHandle(INPUTS, Seed())
        assert lc.export() is None
        lc.saved()
    assert events == [("executor", "run", "")]
    assert lc.uncertain == [] and lc.open_objects == ()
    entry = enter(BWRAP, NO_JOURNAL, lambda runtime: ProbeResult(True, "", "x"))
    assert entry.runtime.backend is BackendKind.BWRAP and entry.still_open == ()


# -- the plan step first: a provider's SandboxPlan (D13) ----------------------------


def _facts(tmp_path: Path) -> LaunchFacts:
    """A REMOTE agent's facts, as the engine's are; only paths, nothing is read."""
    base = str(tmp_path)
    return LaunchFacts(
        launch_class=LaunchClass.REMOTE_AGENT,
        workspace=f"{base}/work",
        home=f"{base}/home",
        controller_env={},
        git_entry=f"{base}/work/.git",
        git_dir=f"{base}/common/worktrees/work",
        common_dir=f"{base}/common",
        private_objects=f"{base}/sandbox/objects",
        overlay_work=f"{base}/sandbox/overlay-work",
        read_context=f"{base}/read-context",
    )


def test_the_plan_compiles_before_the_environment_is_made_and_binds_it(tmp_path):
    events: list[tuple] = []
    backend = FakeBackend(events)
    journal = FakeJournal(events)
    with _lifecycle(backend, journal, change=False) as lifecycle:
        plan = SandboxPlan(_facts(tmp_path), backend, lifecycle)
        description = lifecycle.launch(lambda: plan.describe(ProviderRequirements(), {}))
    assert events[:3] == [
        ("host", "plan"),
        ("host", "seed"),
        ("journal", "creates", ("staging", "projection", "vol", "stager", "launch")),
    ]
    binding = description.binding
    assert binding.backend is backend and binding.handle is lifecycle.handle
    assert _container(binding.handle) == "launch"
    # The seed step got what the plan compiled.
    ((inputs, planned),) = backend.seeded
    assert inputs == INPUTS and planned == description
    assert binding.handle.inputs == _bound(binding.handle.seed)


def test_a_plan_the_policy_refuses_makes_and_starts_nothing(tmp_path):
    events: list[tuple] = []
    backend = FakeBackend(events)
    backend.deny_plan = True
    journal = FakeJournal(events)
    with pytest.raises(SandboxLaunchRefusedError, match="a forbidden source"):
        with _lifecycle(backend, journal) as lifecycle:
            plan = SandboxPlan(_facts(tmp_path), backend, lifecycle)
            lifecycle.launch(lambda: plan.describe(ProviderRequirements(), {}))
    assert events == [("host", "plan")]
    assert backend.seeded == [] and backend.made == [] and journal.entries == []


def test_a_plan_without_a_lifecycle_binds_its_backend_and_no_environment(tmp_path):
    events: list[tuple] = []
    backend = FakeBackend(events)
    description = SandboxPlan(_facts(tmp_path), backend).describe(ProviderRequirements(), {})
    assert description.binding.backend is backend and description.binding.handle is None
    assert events == [("host", "plan")]
    # The default plan is bwrap's, as every existing caller builds one.
    assert SandboxPlan(_facts(tmp_path)).backend is BWRAP


# -- a real provider adapter and the real executor, on the fake backend ------------


def _cli(tmp_path: Path) -> Path:
    """A fake ``opencode`` that reports where it ran: its container, nonce and argv."""
    tool = tmp_path / "tools" / "opencode"
    tool.parent.mkdir(parents=True)
    tool.write_text(
        f"#!{sys.executable}\nimport json, os, sys\nsys.stdin.read()\n"
        "print(json.dumps({'container': os.environ.get('FAKE_CONTAINER'),"
        " 'nonce': os.environ.get('FAKE_NONCE'), 'argv': sys.argv[1:]}))\n"
    )
    tool.chmod(0o755)
    return tool


@_linux_only
def test_a_providers_command_runs_in_the_container_created_and_staged_for_its_launch(
    tmp_path, monkeypatch
):
    """OpenCode's adapter completes the plan, the lifecycle makes the environment,
    and the executor runs the adapter's command in exactly that launch container,
    prepared by the backend that planned it (ADR 0013 D13)."""
    events: list[tuple] = []
    backend = FakeBackend(events)
    journal = FakeJournal(events)
    cli = _cli(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    profile = ProfileConfig(
        name="review", provider="opencode", model="openai/gpt-x", effort="", command=str(cli)
    )
    with _lifecycle(backend, journal, change=False) as lifecycle:
        req = AgentRequest(
            phase="REVIEW",
            prompt="review the change",
            cwd=str(work),
            profile=profile,
            idle_timeout_seconds=None,
            max_runtime_seconds=60,
            env_allowlist=default_config().execution.environment_names(),
            env={},
            sandbox=SandboxPlan(_facts(tmp_path), backend, lifecycle),
        )
        result = lifecycle.launch(lambda: OpenCodeProvider().execute(req))
    assert result.exit_code == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    (launch,) = [obj for obj in journal.created if obj.kind is ObjectKind.LAUNCH_CONTAINER]
    # The command ran in the launch container this launch created and staged.
    assert (report["container"], report["nonce"]) == (launch.name, launch.nonce)
    assert report["argv"][:2] == ["run", "--standalone"]
    ((handle, command),) = backend.prepared
    assert handle is lifecycle.handle and handle.find(ObjectKind.LAUNCH_CONTAINER) == launch
    assert command[0] == str(cli)
    assert result.sandbox == CONTAINER_FACTS
    # Plan, seed, create, stage, then the prepare and the spawn into a running
    # container, closed before the release.
    order = [
        _index(events, "host", "plan"),
        _index(events, "host", "seed"),
        _index(events, "journal", "creates", ("staging", "projection", "vol", "stager", "launch")),
        _index(events, "runtime", "rm", "stager"),
        _index(events, "runtime", "start", "launch"),
        _index(events, "executor", "prepare", "launch"),
        _index(events, "executor", "spawned", "launch", True),
        _index(events, "runtime", "kill", "launch"),
        _index(events, "journal", "removals", ("launch", "projection", "staging", "vol")),
        _index(events, "runtime", "rm", "launch"),
        _index(events, "runtime", "rm", "vol"),
    ]
    assert order == sorted(order)
    assert not any(event[1] == "rm refused" for event in events)
    assert journal.entries == [] and lifecycle.uncertain == []
    assert backend.runtime.containers == {} and backend.runtime.volumes == set()


@_linux_only
def test_a_launch_no_lifecycle_made_an_environment_for_is_refused_by_a_backend_that_needs_one(
    tmp_path,
):
    events: list[tuple] = []
    backend = FakeBackend(events)
    marker = tmp_path / "ran"
    description = SandboxPlan(_facts(tmp_path), backend).describe(ProviderRequirements(), {})
    with pytest.raises(SandboxLaunchRefusedError, match="no launch container was made"):
        execute(
            ExecutionRequest(
                command=[sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
                timeout_seconds=30,
                sandbox=description,
            )
        )
    assert not marker.exists()
    assert [handle for handle, _command in backend.prepared] == [None]


# -- the engine's sequence (ADR 0013 §7) --------------------------------------------


def _required():
    cfg = default_config()
    cfg.sandbox.mode = "required"
    return cfg


@pytest.fixture
def through_fake(monkeypatch):
    """The engine's backend is a :class:`FakeBackend`, its journal a :class:`FakeJournal`
    and its verify step a :class:`FakeSource` that accepts the worktree's ``HEAD``.

    The probe the engine caches (``probe_sandbox``) records itself too, and
    so does every agent the scripted provider runs.
    """
    events: list[tuple] = []
    backend = FakeBackend(events)
    journal = FakeJournal(events)
    source = FakeSource(events)
    monkeypatch.setattr(engine_module, "launch_backend", lambda: backend)
    monkeypatch.setattr(ControllerEngine, "_lifecycle_journal", lambda self: journal)
    monkeypatch.setattr(ControllerEngine, "_candidate_source", lambda self: source)
    # The fake's export directory keeps its short name; the engine's own (a
    # fresh path under the sandbox directories) is what ``through_packing`` uses.
    monkeypatch.setattr(ControllerEngine, "_export_dir", lambda self, owner: "out")

    def probe() -> ProbeResult:
        events.append(("engine", "probe"))
        return ProbeResult(True, "", "fake")

    monkeypatch.setattr(engine_module, "probe_sandbox", probe)
    return events, backend, journal, source


def _recording(events: list[tuple]):
    def agent(req):
        events.append(("agent", req.phase))
        return implement(req)

    return agent


def test_the_engine_enters_then_runs_a_launch_through_every_step_in_order(
    tmp_state_dir, fake_github, through_fake
):
    events, backend, journal, _source = through_fake
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
    # The plan step, then the seed step, before any journal entry.
    assert events[3:5] == [("host", "plan"), ("host", "seed")]
    # The provider's description is the one the fake planned, bound to it and to
    # the launch container its lifecycle created.
    binding = eng.provider.descriptions[-1].binding
    assert binding.backend is backend
    assert _container(binding.handle) == "launch"
    launch = binding.handle.inputs.owner.launch
    assert launch.startswith("ANALYZE_EXECUTE-")
    order = [
        _index(events, "journal", "creates", ("staging", "projection", "vol", "stager", "launch")),
        _index(events, "runtime", "rm", "stager"),
        _index(events, "runtime", "start", "launch"),
        _index(events, "agent", "ANALYZE_EXECUTE"),
        _index(events, "journal", "removals", ("launch", "projection", "staging")),
        _index(events, "journal", "creates", ("exporter", "out")),
        _index(events, "runtime", "rm", "exporter"),
        _index(events, "engine", "verify", "out", launch),
        _index(events, "engine", "save", True),
        _index(events, "journal", "removals", ("vol", "out")),
        _index(events, "runtime", "rm", "vol"),
    ]
    assert order == sorted(order)
    assert journal.entries == [] and backend.runtime.volumes == set()


def test_the_engine_completes_a_launch_with_nothing_to_export(
    tmp_state_dir, fake_github, through_fake
):
    """REVIEW is not change-producing: its release removes the volume after the
    launch container that mounts it, and the step completes (R3-F1)."""
    events, backend, journal, _source = through_fake

    def agent(req) -> str:
        events.append(("agent", req.phase))
        if req.phase == "ANALYZE_EXECUTE":
            return implement(req)
        return block(review_result(1, fake_github.prs[PR].head_sha))

    eng = make_engine(tmp_state_dir, agent, github=fake_github, cfg=_required(), origin=True)
    eng.step()
    assert eng.step().next_phase == "REVIEW"
    events.clear()
    out = eng.step()
    assert out.next_phase == "READY_FOR_MERGE", out.message
    order = [
        _index(events, "journal", "creates", ("staging", "projection", "vol", "stager", "launch")),
        _index(events, "agent", "REVIEW"),
        _index(events, "journal", "removals", ("launch", "projection", "staging", "vol")),
        _index(events, "runtime", "rm", "launch"),
        _index(events, "runtime", "rm", "vol"),
        _index(events, "journal", "close", ("launch", "projection", "staging", "vol")),
    ]
    assert order == sorted(order)
    assert not any(
        event[:2] in (("engine", "verify"), ("runtime", "rm refused")) for event in events
    )
    assert ("journal", "creates", ("exporter", "out")) not in events
    assert journal.entries == [] and backend.runtime.containers == {}
    assert backend.runtime.volumes == set()


def test_the_engine_plans_before_it_seeds_and_a_denied_plan_makes_nothing(
    tmp_state_dir, fake_github, through_fake
):
    events, backend, journal, _source = through_fake
    backend.deny_plan = True
    eng = make_engine(
        tmp_state_dir, _recording(events), github=fake_github, cfg=_required(), origin=True
    )
    eng.step()
    with pytest.raises(SandboxLaunchRefusedError, match="a forbidden source"):
        eng.step()
    assert events == [
        ("runtime", "check"),
        ("runtime", "reconcile", ()),
        ("engine", "probe"),
        ("host", "plan"),
    ]
    assert backend.seeded == [] and backend.made == [] and journal.entries == []
    assert load_state(eng.paths.state_file).phase is Phase.ANALYZE_EXECUTE


@pytest.mark.parametrize("source", ["none", "refusing"])
def test_an_artifact_the_verify_step_refuses_is_never_a_candidate(
    tmp_state_dir, fake_github, through_fake, monkeypatch, source
):
    """Without #269's candidate source, or with one that refuses: the refusal is
    saved, the final clean-up follows it, and the candidate read refuses."""
    events, backend, journal, _source = through_fake
    if source == "none":
        monkeypatch.setattr(ControllerEngine, "_candidate_source", lambda self: NO_CANDIDATE_SOURCE)
    else:
        _source.verdict = Verified(refusal="the artifact's manifest disagrees with its input")
    eng = make_engine(
        tmp_state_dir, _recording(events), github=fake_github, cfg=_required(), origin=True
    )
    eng.step()
    with pytest.raises(ControlResultValidationError):
        eng.step()
    assert load_state(eng.paths.state_file).phase is Phase.ANALYZE_EXECUTE
    agents = [i for i, event in enumerate(events) if event == ("agent", "ANALYZE_EXECUTE")]
    finals = [
        i for i, event in enumerate(events) if event == ("journal", "removals", ("vol", "out"))
    ]
    # Every launch, corrections included, exported and was cleaned up after it.
    assert len(agents) == len(finals) > 0
    assert all(a < f for a, f in zip(agents, finals, strict=True))
    if source == "refusing":
        assert ("engine", "save", False) in events
    assert journal.entries == [] and backend.runtime.volumes == set()


@pytest.mark.parametrize("accepted", ["HEAD", "HEAD~1"])
def test_only_the_commit_the_verify_step_accepted_is_published(
    tmp_state_dir, fake_github, through_fake, accepted
):
    """R2-F1 of PR #473, under #269's candidate read: the agent makes two commits
    and reports the second. A verify step that accepted that commit publishes it.
    One that accepted the first makes the first the candidate, and no other
    commit: the worktree is not read (ImportedArtifact), the report of another
    commit is a correction on every launch, and nothing is published."""
    events, backend, journal, source = through_fake
    source.rev = accepted
    reported: list[str] = []

    def two_commits(req):
        events.append(("agent", req.phase))
        commit_in(req.cwd, "Lay the groundwork (#2)")
        out = implement(req)
        reported.append(git_out("-C", str(req.cwd), "rev-parse", "HEAD"))
        return out

    eng = make_engine(tmp_state_dir, two_commits, github=fake_github, cfg=_required(), origin=True)
    eng.step()
    if accepted == "HEAD":
        out = eng.step()
        assert out.next_phase == "REVIEW", out.message
        assert reported == source.accepted
        assert eng.origin.head("autoforge/2") == reported[-1] == eng.state.current_head_sha
        return
    with pytest.raises(ControlResultValidationError, match="field 'head_sha' is"):
        eng.step()
    # Every launch, corrections included, was verified, and the step accepted a
    # commit other than the one the result reported, each time.
    assert len(source.accepted) == len(reported) > 1
    assert all(a != r for a, r in zip(source.accepted, reported, strict=True))
    for call, (first, second) in zip(
        eng.provider.calls[1:], zip(source.accepted, reported, strict=True), strict=False
    ):
        assert call.correction
        assert f"field 'head_sha' is {second}, but the worktree's HEAD is {first}" in call.prompt
    # Nothing was pushed, created or advanced, and the clean-up still ran.
    assert eng.origin.head("autoforge/2") is None and fake_github.effect_writes == []
    assert load_state(eng.paths.state_file).phase is Phase.ANALYZE_EXECUTE
    assert journal.entries == [] and backend.runtime.volumes == set()


def test_an_uncertain_release_fails_the_step_and_the_next_entry_reconciles_it(
    tmp_state_dir, fake_github, through_fake
):
    events, backend, journal, _source = through_fake
    backend.uncertain = frozenset({ObjectKind.PROJECTION})
    eng = make_engine(
        tmp_state_dir, _recording(events), github=fake_github, cfg=_required(), origin=True
    )
    eng.step()
    with pytest.raises(SandboxCleanupUncertainError) as raised:
        eng.step()
    assert raised.value.objects == ("credential projection projection",)
    # Nothing exported or verified; the projection and the volume stay open.
    assert not any(event[:2] == ("engine", "verify") for event in events)
    assert ("journal", "creates", ("exporter", "out")) not in events
    assert {obj.name for obj in journal.entries} == {"projection", "vol"}
    assert load_state(eng.paths.state_file).phase is Phase.ANALYZE_EXECUTE
    # The next command's entry reconciles both, before its probe and its launch.
    backend.uncertain = frozenset()
    events.clear()
    assert eng.step().next_phase == "REVIEW"
    assert events[:5] == [
        ("runtime", "check"),
        ("journal", "removals", ("projection", "vol")),
        ("runtime", "reconcile", ("projection", "vol")),
        ("runtime", "rm", "projection"),
        ("runtime", "rm", "vol"),
    ]
    assert journal.entries == []


def test_an_exporter_not_verified_removed_is_never_verified(
    tmp_state_dir, fake_github, through_fake
):
    events, backend, journal, _source = through_fake
    backend.uncertain = frozenset({ObjectKind.EXPORTER})
    eng = make_engine(
        tmp_state_dir, _recording(events), github=fake_github, cfg=_required(), origin=True
    )
    eng.step()
    with pytest.raises(SandboxCleanupUncertainError) as raised:
        eng.step()
    assert raised.value.objects == ("exporter container exporter",)
    assert not any(event[:2] in (("engine", "verify"), ("engine", "save")) for event in events)
    assert {obj.name for obj in journal.entries} == {"vol", "exporter", "out"}
    assert load_state(eng.paths.state_file).phase is Phase.ANALYZE_EXECUTE


def test_the_engine_releases_after_an_interrupted_launch(tmp_state_dir, fake_github, through_fake):
    events, backend, journal, _source = through_fake

    def interrupted(req):
        events.append(("agent", req.phase))
        raise KeyboardInterrupt

    eng = make_engine(tmp_state_dir, interrupted, github=fake_github, cfg=_required(), origin=True)
    eng.step()
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    agent = _index(events, "agent", "ANALYZE_EXECUTE")
    assert _index(events, "journal", "removals", ("launch", "projection", "staging")) > agent
    assert backend.runtime.containers == {}
    assert [obj.name for obj in journal.entries] == ["vol"]


def test_an_interrupted_launch_with_an_uncertain_release_is_still_ctrl_c(
    tmp_state_dir, fake_github, through_fake
):
    events, backend, journal, _source = through_fake
    backend.uncertain = frozenset({ObjectKind.PROJECTION})

    def interrupted(req):
        events.append(("agent", req.phase))
        raise KeyboardInterrupt

    eng = make_engine(tmp_state_dir, interrupted, github=fake_github, cfg=_required(), origin=True)
    eng.step()
    with pytest.raises(KeyboardInterrupt) as raised:
        eng.step()
    assert any("credential projection projection" in note for note in raised.value.__notes__)
    assert {obj.name for obj in journal.entries} == {"projection", "vol"}


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
    events, backend, journal, _source = through_fake
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
    events, _backend, _journal, _source = through_fake
    eng = make_engine(tmp_state_dir, _recording(events), github=fake_github, origin=True)
    eng.step()
    assert eng.step().next_phase == "REVIEW"
    assert events == [("agent", "ANALYZE_EXECUTE")]


def test_a_scheduler_workers_engine_runs_the_same_sequence(tmp_path, fake_github, through_fake):
    """A worker runs the engine itself on its issue's namespace (ADR 0008 D9.2):
    its entry and its launch take the same steps, in the same order."""
    events, _backend, journal, _source = through_fake
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
    assert events[:6] == [
        ("runtime", "check"),
        ("runtime", "reconcile", ()),
        ("engine", "probe"),
        ("host", "plan"),
        ("host", "seed"),
        ("journal", "creates", ("staging", "projection", "vol", "stager", "launch")),
    ]
    order = [
        _index(events, "agent", "ANALYZE_EXECUTE"),
        _index(events, "journal", "removals", ("launch", "projection", "staging")),
        _first(events, lambda e: e[:2] == ("engine", "verify")),
        _index(events, "engine", "save", True),
        _index(events, "journal", "removals", ("vol", "out")),
    ]
    assert order == sorted(order)
    assert journal.entries == []


# -- the engine's seeding and candidate source (ADR 0013 D4, D5; #269) -------------------


def _seeded(backend: FakeBackend, phase: str) -> list[LaunchGitInput]:
    """The git input each launch of ``phase`` was seeded from, in order."""
    found = [inputs.git_input for inputs, _ in backend.seeded if inputs.owner.phase == phase]
    assert all(item is not None for item in found)
    return found  # type: ignore[return-value]


def _operator_push(eng: ControllerEngine, message: str) -> str:
    """Another actor's commit on the PR branch, on top of its head; no worktree moves."""
    head = eng.origin.head("autoforge/2")
    tree = git_out("-C", str(eng.workdir), "rev-parse", f"{head}^{{tree}}")
    sha = git_out(
        "-C", str(eng.workdir), *GIT_IDENT, "commit-tree", tree, "-p", head, "-m", message
    )
    eng.origin.publish(eng.workdir, sha, "autoforge/2")
    return sha


def _nit(round_: int) -> dict:
    return {
        "id": f"R{round_}-F1",
        "classification": "nit",
        "title": "typo",
        "location": "src/x.py:1",
        "required_resolution": "fix the typo",
    }


def test_every_launch_is_seeded_from_the_revision_its_phase_bound(
    tmp_state_dir, fake_github, through_fake
):
    """ANALYZE_EXECUTE from the base its entry fetched, REVIEW from the PR HEAD and
    merge base bound to the round; each input saved before any journal entry."""
    events, backend, journal, _source = through_fake

    def agent(req) -> str:
        events.append(("agent", req.phase))
        if req.phase == "ANALYZE_EXECUTE":
            return implement(req)
        return block(review_result(1, fake_github.prs[PR].head_sha))

    eng = make_engine(tmp_state_dir, agent, github=fake_github, cfg=_required(), origin=True)
    base = eng.origin.head("main")
    eng.step()
    assert eng.step().next_phase == "REVIEW"
    assert eng.step().next_phase == "READY_FOR_MERGE"
    (analyze,) = _seeded(backend, "ANALYZE_EXECUTE")
    assert (analyze.seed, analyze.lower, analyze.carried) == (base, base, "")
    assert analyze.repository == "owner/repo" and analyze.object_format == "sha1"
    assert analyze.issue == 2 and analyze.run == eng.state.run_id
    (review,) = _seeded(backend, "REVIEW")
    assert review.seed == eng.state.reviewed_head_sha == eng.origin.head("autoforge/2")
    assert review.lower == eng.state.reviewed_merge_base_sha == base
    assert review.carried == ""
    receipts = CandidateReceipts(eng.state_root())
    for bound, seed in zip((analyze, review), backend.seeds, strict=True):
        (saved,) = receipts.launches(bound.entry)
        assert saved.git_input == bound.with_seed_manifest(seed.identity)


def test_a_default_branch_that_moves_after_binding_leaves_the_launch_on_its_bound_base(
    tmp_state_dir, fake_github, through_fake
):
    """The entry reads the default branch once; it moves before the launch, and
    neither the seed nor the prompt follow it."""
    events, backend, journal, _source = through_fake
    eng = make_engine(
        tmp_state_dir, _recording(events), github=fake_github, cfg=_required(), origin=True
    )
    base = eng.origin.head("main")
    read = fake_github.get_branch_head_sha
    moved: list[str] = []

    def read_then_move(repository: str, branch: str) -> str:
        answer = read(repository, branch)
        if branch == "main" and not moved:
            moved.append(commit_in(eng.workdir, "Land something else on main (#9)"))
            eng.origin.publish(eng.workdir, moved[0], "main")
        return answer

    fake_github.get_branch_head_sha = read_then_move  # type: ignore[method-assign]
    eng.step()
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert moved and eng.origin.head("main") == moved[0] != base
    (analyze,) = _seeded(backend, "ANALYZE_EXECUTE")
    assert analyze.seed == analyze.lower == base
    assert moved[0] not in eng.provider.calls[0].prompt


def _in_update_epic(eng: ControllerEngine, gh) -> None:
    """Park ``eng`` in UPDATE_EPIC right after the controller merged the run's PR."""
    head = eng.origin.head("main")
    gh.add_pr(head_sha=head, state="MERGED")
    eng.state.phase = Phase.UPDATE_EPIC
    eng.state.current_pr_url = PR
    eng.state.current_branch = "autoforge/2"
    eng.state.current_head_sha = head
    eng.state.reviewed_head_sha = head
    eng.state.last_review_result = "clean"
    eng.state.review_round = 1
    eng.state.record_merge(PR)
    eng._save()


def _resumed(eng: ControllerEngine, gh, script) -> ControllerEngine:
    """A new controller process on the same state, git remote and GitHub."""
    eng.close()
    again = make_engine(eng.paths.state_dir, script, github=gh, cfg=_required())
    connect_origin(again, eng.origin)
    again.origin = eng.origin  # type: ignore[attr-defined]
    again.load()
    return again


EPIC_DONE = block(
    {
        "phase": "UPDATE_EPIC",
        "status": "success",
        "progress": "Implemented issue 2 in PR 42; the test suite passes.",
        "roadmap_section": "## Roadmap\n- [x] #2 (PR #42)",
        "next_issue_url": None,
    }
)


def test_update_epic_binds_the_default_branch_head_once_and_a_resume_keeps_it(
    tmp_state_dir, fake_github, through_fake
):
    """R1-F3: the entry's first launch binds the default branch head and records it
    in the entry's observation. The controller is stopped during that launch, the
    default branch moves, and a new process resumes: its relaunch of the same entry
    seeds from the bound head, never the newer one, and the entry completes."""
    events, backend, journal, _source = through_fake

    def stopped(req):
        events.append(("agent", req.phase))
        raise KeyboardInterrupt

    eng = make_engine(tmp_state_dir, stopped, github=fake_github, cfg=_required(), origin=True)
    _in_update_epic(eng, fake_github)
    bound = eng.origin.head("main")
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    (first,) = _seeded(backend, "UPDATE_EPIC")
    assert (first.seed, first.lower, first.carried) == (bound, bound, "")
    saved = load_state(eng.paths.state_file).phase_effects().observation
    assert saved is not None and saved.base_sha == bound
    assert dict(saved.refs) == {"refs/heads/main": bound}
    later = commit_in(eng.workdir, "Land something else on main (#9)")
    eng.origin.publish(eng.workdir, later, "main")
    reads = len([c for c in fake_github.calls if c[0] == "get_branch_head_sha"])
    resumed = _resumed(eng, fake_github, lambda req: EPIC_DONE)
    out = resumed.step()
    assert out.next_phase == "DONE", out.message
    _, second = _seeded(backend, "UPDATE_EPIC")
    assert second.seed == bound != later and second.entry == first.entry
    assert second.launch != first.launch
    # The relaunch read the binding back; it never read the default branch again.
    assert len([c for c in fake_github.calls if c[0] == "get_branch_head_sha"]) == reads


def test_an_update_epic_entry_observed_with_no_head_binds_it_at_its_first_seeded_launch(
    tmp_state_dir, fake_github, through_fake
):
    """An observation that recorded no head (an entry first launched under bwrap,
    say) binds the default branch head as it is at the first seeded launch; the
    observation goes with the entry."""
    events, backend, journal, _source = through_fake
    eng = make_engine(
        tmp_state_dir, lambda req: EPIC_DONE, github=fake_github, cfg=_required(), origin=True
    )
    _in_update_epic(eng, fake_github)
    eng.state.entry_observation = EntryObservation(
        Phase.UPDATE_EPIC, ISSUE, PR, {}, None, {render_progress_marker(ISSUE, PR): None}
    ).to_dict()
    eng._save()
    later = commit_in(eng.workdir, "Land something else on main (#9)")
    eng.origin.publish(eng.workdir, later, "main")
    assert eng.step().next_phase == "DONE"
    (only,) = _seeded(backend, "UPDATE_EPIC")
    assert only.seed == later
    assert load_state(eng.paths.state_file).entry_observation == {}


def replace_launch(owner: LaunchOwner, launch: str) -> LaunchOwner:
    return dataclasses.replace(owner, launch=launch)


def test_replan_seeds_from_its_transactions_verified_base(tmp_state_dir, fake_github, through_fake):
    events, backend, journal, _source = through_fake
    eng = make_engine(
        tmp_state_dir, _recording(events), github=fake_github, cfg=_required(), origin=True
    )
    eng.step()
    base = eng.origin.head("main")
    eng._replan_entry = engine_module._ReplanEntry("txn-1", "autoforge/2-r1", base)
    assert eng._bound_revision(Phase.REPLAN_REEXECUTE).seed == base
    owner = LaunchOwner(eng.state.run_id, 2, "REPLAN_REEXECUTE", "REPLAN_REEXECUTE-1")
    git_input = eng._launch_git_input(Phase.REPLAN_REEXECUTE, owner)
    assert (git_input.seed, git_input.lower) == (base, base)
    eng._replan_entry = None
    with pytest.raises(SandboxLaunchRefusedError, match="no revision its entry bound"):
        eng._launch_git_input(Phase.REPLAN_REEXECUTE, owner)


def _to_fix(eng: ControllerEngine) -> None:
    eng.step()
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message


def test_review_seeds_from_another_actors_push_and_fix_from_that_reviewed_head(
    tmp_state_dir, fake_github, through_fake
):
    events, backend, journal, _source = through_fake

    def agent(req) -> str:
        events.append(("agent", req.phase))
        if req.phase == "ANALYZE_EXECUTE":
            return implement(req)
        if req.phase == "REVIEW":
            return block(review_result(1, fake_github.prs[PR].head_sha, [_nit(1)]))
        return "the fixer reports nothing"

    eng = make_engine(tmp_state_dir, agent, github=fake_github, cfg=_required(), origin=True)
    _to_fix(eng)
    own = eng.state.current_head_sha
    operator = _operator_push(eng, "An operator's follow-up (#2)")
    out = eng.step()
    assert out.next_phase == "FIX", out.message
    (review,) = _seeded(backend, "REVIEW")
    assert review.seed == operator != own and review.carried == ""
    assert review.lower == eng.state.reviewed_merge_base_sha
    with pytest.raises(ControlResultValidationError):
        eng.step()
    fixes = _seeded(backend, "FIX")
    assert fixes and all(fix.seed == operator for fix in fixes)
    assert all(fix.lower == review.lower for fix in fixes)
    assert len({fix.entry for fix in fixes}) == 1


def test_drift_before_fix_goes_back_to_review_with_no_container_and_no_seed(
    tmp_state_dir, fake_github, through_fake
):
    events, backend, journal, _source = through_fake
    rounds: list[str] = []

    def agent(req) -> str:
        events.append(("agent", req.phase))
        if req.phase == "ANALYZE_EXECUTE":
            return implement(req)
        head = fake_github.prs[PR].head_sha
        rounds.append(head)
        findings = [_nit(len(rounds))] if len(rounds) == 1 else []
        return block(review_result(len(rounds), head, findings))

    eng = make_engine(tmp_state_dir, agent, github=fake_github, cfg=_required(), origin=True)
    _to_fix(eng)
    assert eng.step().next_phase == "FIX"
    moved = _operator_push(eng, "An operator's push after the review (#2)")
    seeded, created = len(backend.seeded), len(journal.created)
    out = eng.step()
    assert out.next_phase == "REVIEW" and "past the reviewed HEAD" in out.message
    assert len(backend.seeded) == seeded and len(journal.created) == created
    assert _seeded(backend, "FIX") == []
    out = eng.step()
    assert out.next_phase == "READY_FOR_MERGE", out.message
    first, second = _seeded(backend, "REVIEW")
    assert second.seed == moved != first.seed and second.entry != first.entry


def test_drift_during_review_leaves_the_seed_at_the_bound_head(
    tmp_state_dir, fake_github, through_fake
):
    """The PR moves while the reviewer works: the round is stale, its seed stays
    the HEAD bound to it, and the next round binds the new HEAD."""
    events, backend, journal, _source = through_fake
    moved: list[str] = []
    eng_box: list[ControllerEngine] = []

    def agent(req) -> str:
        events.append(("agent", req.phase))
        if req.phase == "ANALYZE_EXECUTE":
            return implement(req)
        head = eng_box[0].state.current_head_sha
        if not moved:
            moved.append(_operator_push(eng_box[0], "A push while the reviewer works (#2)"))
        return block(review_result(eng_box[0].state.review_round + 1, head))

    eng = make_engine(tmp_state_dir, agent, github=fake_github, cfg=_required(), origin=True)
    eng_box.append(eng)
    _to_fix(eng)
    out = eng.step()
    assert out.next_phase == "REVIEW" and eng.state.last_review_result == "stale", out.message
    (stale,) = _seeded(backend, "REVIEW")
    assert stale.seed != moved[0]
    assert eng.step().next_phase == "READY_FOR_MERGE"
    _, fresh = _seeded(backend, "REVIEW")
    assert fresh.seed == moved[0] and stale.seed != fresh.seed


def test_a_relaunch_of_the_same_review_round_seeds_from_the_same_head_with_no_carried_commit(
    tmp_state_dir, fake_github, through_fake
):
    events, backend, journal, _source = through_fake
    reviews: list[int] = []

    def agent(req) -> str:
        events.append(("agent", req.phase))
        if req.phase == "ANALYZE_EXECUTE":
            return implement(req)
        reviews.append(1)
        if len(reviews) == 1:
            return "the reviewer forgot its CONTROL_RESULT"
        return block(review_result(1, fake_github.prs[PR].head_sha))

    eng = make_engine(tmp_state_dir, agent, github=fake_github, cfg=_required(), origin=True)
    _to_fix(eng)
    assert eng.step().next_phase == "READY_FOR_MERGE"
    first, second = _seeded(backend, "REVIEW")
    assert first.entry == second.entry and first.launch != second.launch
    assert (first.seed, first.lower) == (second.seed, second.lower)
    assert first.carried == second.carried == ""


def test_a_bound_revision_the_store_cannot_seed_refuses_the_launch_before_any_journal_entry(
    tmp_state_dir, fake_github, through_fake
):
    """A merge base that is not an ancestor of the bound HEAD in the shared store:
    the REVIEW launch is refused before anything of it is journaled or created."""
    events, backend, journal, _source = through_fake

    def agent(req) -> str:
        events.append(("agent", req.phase))
        return implement(req)

    eng = make_engine(tmp_state_dir, agent, github=fake_github, cfg=_required(), origin=True)
    _to_fix(eng)
    tree = git_out("-C", str(eng.workdir), "rev-parse", "HEAD^{tree}")
    stray = git_out("-C", str(eng.workdir), *GIT_IDENT, "commit-tree", tree, "-m", "Unrelated")
    eng.origin.publish(eng.workdir, stray, "unrelated")
    head = fake_github.prs[PR].head_sha
    fake_github.merge_bases[("main", head)] = stray
    created = len(journal.created)
    with pytest.raises(SandboxLaunchRefusedError, match="is not an ancestor of the seed"):
        eng.step()
    assert _seeded(backend, "REVIEW") == [] and len(journal.created) == created
    assert ("agent", "REVIEW") not in events
    assert load_state(eng.paths.state_file).phase is Phase.REVIEW


class PackingBackend(FakeBackend):
    """A fake container backend whose exporter leaves a real artifact (#271's will).

    The agent of this fake runs in the host worktree, so the exporter packs
    that worktree's ``HEAD`` above the launch's seed and writes the manifest
    for the launch's git input, into the launch's export directory.
    """

    def __init__(self, events: list[tuple], out: Path) -> None:
        super().__init__(events)
        self.out = out
        self.artifacts: list[Path] = []

    def export(self, objects, handle) -> Artifact:
        super().export(objects, handle)
        git_input = handle.inputs.git_input
        assert git_input is not None and git_input.seed_manifest_digest
        workspace = handle.inputs.workspace
        head = git_out("-C", workspace, "rev-parse", "HEAD")
        pack = isolated_git(
            "-C", workspace, "pack-objects", "--revs", "--stdout", "-q",
            input=f"{head}\n^{git_input.seed}\n".encode(),
        )  # fmt: skip
        # The exporter fills the export directory the controller chose (#269).
        directory = Path(handle.inputs.export_dir)
        directory.mkdir()
        manifest = CandidateManifest.for_input(
            git_input,
            candidate=head,
            pack_bytes=len(pack),
            pack_objects=int.from_bytes(pack[8:12], "big"),
            pack_sha256=hashlib.sha256(pack).hexdigest(),
        )
        (directory / ARTIFACT_PACK).write_bytes(pack)
        (directory / ARTIFACT_MANIFEST).write_bytes(manifest.render())
        self.artifacts.append(directory)
        return Artifact(str(directory))


@pytest.fixture
def through_packing(monkeypatch, tmp_path):
    """The engine's backend exports real artifacts, verified by its own candidate source."""
    events: list[tuple] = []
    backend = PackingBackend(events, tmp_path / "exports")
    journal = FakeJournal(events)
    monkeypatch.setattr(engine_module, "launch_backend", lambda: backend)
    monkeypatch.setattr(ControllerEngine, "_lifecycle_journal", lambda self: journal)
    monkeypatch.setattr(engine_module, "probe_sandbox", lambda: ProbeResult(True, "", "fake"))
    return events, backend, journal


def test_the_engines_candidate_source_imports_the_exported_artifact_and_publishes_it(
    tmp_state_dir, fake_github, through_packing
):
    events, backend, journal = through_packing
    eng = make_engine(
        tmp_state_dir, _recording(events), github=fake_github, cfg=_required(), origin=True
    )
    eng.step()
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    (analyze,) = _seeded(backend, "ANALYZE_EXECUTE")
    (saved,) = CandidateReceipts(eng.state_root()).launches(analyze.entry)
    assert saved.accepted and saved.candidate == eng.state.current_head_sha
    assert eng.origin.head("autoforge/2") == saved.candidate
    assert journal.entries == [] and backend.runtime.volumes == set()


def test_a_correction_relaunch_carries_the_head_the_controller_imported(
    tmp_state_dir, fake_github, through_packing
):
    """The first launch commits, leaves a file uncommitted and reports another
    commit: corrected. The relaunch of the same entry carries the commit the
    controller imported, never the uncommitted file, and its candidate is
    published."""
    events, backend, journal = through_packing
    heads: list[str] = []

    def agent(req) -> str:
        events.append(("agent", req.phase))
        heads.append(commit_in(req.cwd, "Implement the feature (#2)"))
        if len(heads) == 1:
            Path(req.cwd, "scratch.txt").write_text("uncommitted")
            return block(analyze_payload("0" * 40))
        return block(analyze_payload(heads[-1]))

    eng = make_engine(tmp_state_dir, agent, github=fake_github, cfg=_required(), origin=True)
    eng.step()
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    first, second = _seeded(backend, "ANALYZE_EXECUTE")
    assert first.entry == second.entry and first.seed == second.seed
    assert first.carried == "" and second.carried == heads[0]
    files = git_out("-C", str(eng.workdir), "ls-tree", "-r", "--name-only", second.carried)
    assert "scratch.txt" not in files.split()
    launches = CandidateReceipts(eng.state_root()).launches(first.entry)
    assert [item.candidate for item in launches] == heads
    assert eng.origin.head("autoforge/2") == heads[1] == eng.state.current_head_sha


def test_an_artifact_for_another_launch_is_refused_and_nothing_is_published(
    tmp_state_dir, fake_github, through_packing
):
    events, backend, journal = through_packing
    export = backend.export

    def mislabelled(objects, handle):
        artifact = export(objects, handle)
        manifest_path = Path(artifact.directory, ARTIFACT_MANIFEST)
        data = json.loads(manifest_path.read_text())
        data["launch"] = "ANALYZE_EXECUTE-99"
        manifest_path.write_text(json.dumps(data))
        return artifact

    backend.export = mislabelled  # type: ignore[method-assign]
    eng = make_engine(
        tmp_state_dir, _recording(events), github=fake_github, cfg=_required(), origin=True
    )
    eng.step()
    with pytest.raises(ControlResultValidationError, match="its manifest names launch"):
        eng.step()
    assert eng.origin.head("autoforge/2") is None and fake_github.effect_writes == []
    assert load_state(eng.paths.state_file).phase is Phase.ANALYZE_EXECUTE
    (analyze, *_rest) = _seeded(backend, "ANALYZE_EXECUTE")
    assert CandidateReceipts(eng.state_root()).carried(analyze.entry) == ""
    assert journal.entries == [] and backend.runtime.volumes == set()


def test_a_second_fix_round_at_the_same_head_carries_nothing_from_the_first(
    tmp_state_dir, fake_github, through_fake
):
    """Two FIX entries bind the same reviewed HEAD (a round whose fix moved
    nothing): the second is an entry of its own, and never carries a commit the
    first one imported; a relaunch of the first does."""
    events, backend, journal, _source = through_fake

    def agent(req) -> str:
        events.append(("agent", req.phase))
        if req.phase == "ANALYZE_EXECUTE":
            return implement(req)
        return block(review_result(1, fake_github.prs[PR].head_sha, [_nit(1)]))

    eng = make_engine(tmp_state_dir, agent, github=fake_github, cfg=_required(), origin=True)
    _to_fix(eng)
    assert eng.step().next_phase == "FIX"
    reviewed = eng.state.reviewed_head_sha
    tree = git_out("-C", str(eng.workdir), "rev-parse", f"{reviewed}^{{tree}}")
    fixed = git_out(
        "-C", str(eng.workdir), *GIT_IDENT, "commit-tree", tree, "-p", reviewed, "-m", "Fix (#2)"
    )
    owner = LaunchOwner(eng.state.run_id, 2, "FIX", "FIX-1")
    first = eng._launch_git_input(Phase.FIX, owner)
    assert (first.seed, first.carried) == (reviewed, "")
    eng._candidate_receipts().record(first, Verified(candidate=fixed))
    relaunch = eng._launch_git_input(Phase.FIX, replace_launch(owner, "FIX-2"))
    assert relaunch.entry == first.entry and relaunch.carried == fixed
    eng.state.review_round += 1  # the next round, at the same reviewed HEAD
    next_round = eng._launch_git_input(Phase.FIX, owner)
    assert next_round.seed == reviewed and next_round.entry != first.entry
    assert next_round.carried == ""


def test_a_replacement_prs_fix_round_carries_nothing_from_the_replaced_prs(
    tmp_state_dir, fake_github, through_fake
):
    """R1-F9: a replan's replacement PR restarts its review rounds at one. Reviewed
    at the same HEAD and merge base as the replaced PR's round 1, its FIX entry is
    still an entry of its own: it never carries the replaced PR's fixer commit."""
    events, backend, journal, _source = through_fake

    def agent(req) -> str:
        events.append(("agent", req.phase))
        if req.phase == "ANALYZE_EXECUTE":
            return implement(req)
        return block(review_result(1, fake_github.prs[PR].head_sha, [_nit(1)]))

    eng = make_engine(tmp_state_dir, agent, github=fake_github, cfg=_required(), origin=True)
    _to_fix(eng)
    assert eng.step().next_phase == "FIX"
    reviewed, merge_base = eng.state.reviewed_head_sha, eng.state.reviewed_merge_base_sha
    tree = git_out("-C", str(eng.workdir), "rev-parse", f"{reviewed}^{{tree}}")
    fixed = git_out(
        "-C", str(eng.workdir), *GIT_IDENT, "commit-tree", tree, "-p", reviewed, "-m", "Fix (#2)"
    )
    owner = LaunchOwner(eng.state.run_id, 2, "FIX", "FIX-1")
    replaced = eng._launch_git_input(Phase.FIX, owner)
    eng._candidate_receipts().record(replaced, Verified(candidate=fixed))
    assert eng._launch_git_input(Phase.FIX, replace_launch(owner, "FIX-2")).carried == fixed
    # What the replacement's activation changes (``_activate_replacement``): a new
    # execution attempt and PR, its rounds counted from zero again; its first
    # review then binds round 1 at the same HEAD and merge base.
    eng.state.execution_attempt += 1
    eng.state.current_pr_url = "https://github.com/owner/repo/pull/43"
    eng.state.reviewed_pr_url = eng.state.current_pr_url
    eng.state.review_round = 1
    replacement = eng._launch_git_input(Phase.FIX, owner)
    assert (replacement.seed, replacement.lower) == (reviewed, merge_base)
    assert replacement.entry_label != replaced.entry_label
    assert replacement.entry != replaced.entry and replacement.carried == ""


# -- an existing issue branch, under a backend that seeds (R1-F4) ------------------------


def _earlier_work(eng: ControllerEngine) -> str:
    """A commit for this issue on ``autoforge/2``, pushed before the run, above main."""
    main = eng.origin.head("main")
    tree = git_out("-C", str(eng.workdir), "rev-parse", f"{main}^{{tree}}")
    sha = git_out(
        "-C", str(eng.workdir), *GIT_IDENT, "commit-tree", tree, "-p", main, "-m", "Earlier (#2)"
    )
    return eng.origin.publish(eng.workdir, sha, "autoforge/2")


def _from_start(req) -> str:
    """The agent the prompt asks for: detached at the start commit, then one commit."""
    start = re.search(r"Start commit: `([0-9a-f]{40})`", req.prompt)
    assert start, "the prompt names no start commit"
    git_out("-C", req.cwd, "checkout", "-q", "--detach", start.group(1))
    return implement(req)


@pytest.mark.parametrize("adopt", [False, True])
def test_an_implementation_launch_carries_the_branch_work_it_continues(
    tmp_state_dir, fake_github, through_packing, adopt
):
    """The entry finds earlier work on ``autoforge/2`` (with no PR, or an open PR it
    adopts): the agent is told to start from that head and its candidate must
    descend from it, so the seed carries it above the bound base, and the
    candidate imported from the artifact is published as a fast-forward."""
    events, backend, journal = through_packing
    eng = make_engine(tmp_state_dir, _from_start, github=fake_github, cfg=_required(), origin=True)
    base = eng.origin.head("main")
    earlier = _earlier_work(eng)
    pr = "https://github.com/owner/repo/pull/41"
    if adopt:
        fake_github.add_pr(pr, head_sha=earlier, branch="autoforge/2", body="Started by hand.")
    eng.step()
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    (analyze,) = _seeded(backend, "ANALYZE_EXECUTE")
    assert (analyze.seed, analyze.lower, analyze.carried) == (base, base, earlier)
    (req,) = eng.provider.calls
    assert f"Start commit: `{earlier}`" in req.prompt
    head = eng.state.current_head_sha
    assert git_out("-C", str(eng.workdir), "rev-parse", f"{head}^") == earlier
    (saved,) = CandidateReceipts(eng.state_root()).launches(analyze.entry)
    assert saved.accepted and saved.candidate == head
    assert eng.origin.head("autoforge/2") == head
    assert eng.state.current_pr_url == (pr if adopt else PR)


def test_a_relaunch_carries_its_own_import_above_the_branch_work(
    tmp_state_dir, fake_github, through_packing
):
    """The entry's first launch imports a commit above the branch's head: the
    relaunch carries that commit, which already holds the branch's work."""
    events, backend, journal = through_packing
    heads: list[str] = []

    def agent(req) -> str:
        if not heads:
            _from_start(req)
        heads.append(commit_in(req.cwd, "More work (#2)"))
        return block(analyze_payload("0" * 40 if len(heads) == 1 else heads[-1]))

    eng = make_engine(tmp_state_dir, agent, github=fake_github, cfg=_required(), origin=True)
    earlier = _earlier_work(eng)
    eng.step()
    assert eng.step().next_phase == "REVIEW"
    first, second = _seeded(backend, "ANALYZE_EXECUTE")
    assert first.carried == earlier and second.carried == heads[0] != earlier


def test_branch_work_that_does_not_descend_from_the_base_refuses_the_launch(
    tmp_state_dir, fake_github, through_packing
):
    """The default branch moved past the branch's base: no seed holds both, so the
    launch is refused before anything of it is journaled or created."""
    events, backend, journal = through_packing
    eng = make_engine(tmp_state_dir, _from_start, github=fake_github, cfg=_required(), origin=True)
    earlier = _earlier_work(eng)
    moved = commit_in(eng.workdir, "Land something else on main (#9)")
    eng.origin.publish(eng.workdir, moved, "main")
    eng.step()
    with pytest.raises(SandboxLaunchRefusedError, match=f"branch head {earlier}.*does not descend"):
        eng.step()
    assert backend.seeded == [] and journal.created == [] and eng.provider.calls == []
    assert load_state(eng.paths.state_file).phase is Phase.ANALYZE_EXECUTE


# -- the workspace note under each backend (R1-F5) ---------------------------------------

CONTAINER_NOTE = "## Your workspace starts from a bound revision"
HOST_NOTE = "## Uncommitted changes in this worktree"


def test_a_container_relaunch_is_never_told_the_host_worktrees_changes(
    tmp_state_dir, fake_github, through_fake
):
    """The first launch leaves a file uncommitted in the host worktree and is
    corrected. Neither launch's prompt describes the host worktree as its
    workspace: each says its workspace is seeded and that uncommitted work is
    discarded between launches."""
    events, backend, journal, _source = through_fake
    heads: list[str] = []

    def agent(req) -> str:
        events.append(("agent", req.phase))
        heads.append(commit_in(req.cwd, "Implement the feature (#2)"))
        if len(heads) == 1:
            Path(req.cwd, "scratch.txt").write_text("uncommitted")
            return block(analyze_payload("0" * 40))
        return block(analyze_payload(heads[-1]))

    eng = make_engine(tmp_state_dir, agent, github=fake_github, cfg=_required(), origin=True)
    eng.step()
    assert eng.step().next_phase == "REVIEW"
    first, second = eng.provider.calls
    assert second.correction and Path(second.cwd, "scratch.txt").exists()
    for req in (first, second):
        assert CONTAINER_NOTE in req.prompt and HOST_NOTE not in req.prompt
        assert "scratch.txt" not in req.prompt
        assert "uncommitted when you finish is lost" in " ".join(req.prompt.split())


def test_the_workspace_note_follows_the_backend(tmp_state_dir, fake_github, monkeypatch):
    """bwrap and ``disabled`` read the host worktree as before; a backend that seeds
    reads none of it."""
    eng = make_engine(tmp_state_dir, [], github=fake_github, cfg=_required(), origin=True)
    worktree = eng._ensure_agent_worktree()
    (worktree / "scratch.txt").write_text("left by an earlier launch")
    assert engine_module.launch_backend() is BWRAP
    hint, note = eng._read_worktree_state(str(worktree))
    assert HOST_NOTE in hint and "scratch.txt" in hint
    assert note == "1 uncommitted change(s), described in the prompt"
    monkeypatch.setattr(engine_module, "launch_backend", lambda: FakeBackend([]))
    hint, note = eng._read_worktree_state(str(worktree))
    assert CONTAINER_NOTE in hint and "scratch.txt" not in hint
    assert note.startswith("a seeded launch: the host worktree is not its workspace")
    eng.config.sandbox.mode = "disabled"
    hint, note = eng._read_worktree_state(str(worktree))
    assert HOST_NOTE in hint and "scratch.txt" in hint


# -- a refused launch after an accepted one, a failed export, a stop (R1-F6, F7, F8) -------


def test_a_relaunch_after_a_refused_artifact_carries_the_earlier_accepted_import(
    tmp_state_dir, fake_github, through_packing
):
    """Launch 1's artifact is accepted (its report is corrected), launch 2's is
    refused, and launch 3 still carries what launch 1 imported."""
    events, backend, journal = through_packing
    export = backend.export
    heads: list[str] = []

    def second_mislabelled(objects, handle):
        artifact = export(objects, handle)
        if len(backend.artifacts) == 2:
            path = Path(artifact.directory, ARTIFACT_MANIFEST)
            data = json.loads(path.read_text())
            data["launch"] = "ANALYZE_EXECUTE-99"
            path.write_text(json.dumps(data))
        return artifact

    def agent(req) -> str:
        heads.append(commit_in(req.cwd, "Implement the feature (#2)"))
        return block(analyze_payload("0" * 40 if len(heads) == 1 else heads[-1]))

    backend.export = second_mislabelled  # type: ignore[method-assign]
    eng = make_engine(tmp_state_dir, agent, github=fake_github, cfg=_required(), origin=True)
    eng.step()
    try:
        out = eng.step()
    except ControlResultValidationError:
        out = eng.step()  # 'resume': the next step relaunches the same entry
    assert out.next_phase == "REVIEW", out.message
    first, second, third = _seeded(backend, "ANALYZE_EXECUTE")
    assert first.entry == second.entry == third.entry
    assert (first.carried, second.carried, third.carried) == ("", heads[0], heads[0])
    launches = CandidateReceipts(eng.state_root()).launches(first.entry)
    assert [item.accepted for item in launches] == [True, False, True]
    assert eng.origin.head("autoforge/2") == heads[2] == eng.state.current_head_sha


def test_a_failed_export_is_recorded_against_the_launchs_input_before_the_volume_goes(
    tmp_state_dir, fake_github, through_packing
):
    """The exporter fails; the controller dies while removing the volume. A new
    process reads the refusal back, bound to the launch's git input."""
    events, backend, journal = through_packing
    backend.fail = "export"
    remove = backend.remove
    on_disk: list[list] = []

    def crash_at_the_volume(obj):
        if obj.kind is ObjectKind.VOLUME:
            (bound,) = _seeded(backend, "ANALYZE_EXECUTE")
            on_disk.append(CandidateReceipts(eng.state_root()).launches(bound.entry))
            raise KeyboardInterrupt
        return remove(obj)

    backend.remove = crash_at_the_volume  # type: ignore[method-assign]
    eng = make_engine(
        tmp_state_dir, _recording(events), github=fake_github, cfg=_required(), origin=True
    )
    eng.step()
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    (bound,) = _seeded(backend, "ANALYZE_EXECUTE")
    # Recorded before the volume's removal was asked for.
    ((before,),) = on_disk
    assert before.verified and "the launch's export failed: fake: export failed" in before.refusal
    resumed = _resumed(eng, fake_github, _recording(events))
    (after,) = resumed._candidate_receipts().launches(bound.entry)
    assert after == before
    assert after.git_input == bound.with_seed_manifest(backend.seeds[0].identity)
    assert {obj.name for obj in journal.entries} >= {"vol"}


def test_a_launch_stopped_after_it_committed_exports_its_work_for_the_relaunch(
    tmp_state_dir, fake_github, through_packing
):
    """The agent commits, then its run ends by an error (a stop the provider
    reports as one): the work is exported, imported and saved before the error
    propagates, the volume goes after that, and the resumed relaunch carries the
    commit."""
    events, backend, journal = through_packing
    heads: list[str] = []

    def agent(req) -> str:
        heads.append(commit_in(req.cwd, "Implement the feature (#2)"))
        if len(heads) == 1:
            raise ExecutionError("the agent's run was stopped")
        return block(analyze_payload(heads[-1]))

    eng = make_engine(tmp_state_dir, agent, github=fake_github, cfg=_required(), origin=True)
    eng.step()
    with pytest.raises(ExecutionError, match="the agent's run was stopped"):
        eng.step()
    (first,) = _seeded(backend, "ANALYZE_EXECUTE")
    (saved,) = CandidateReceipts(eng.state_root()).launches(first.entry)
    assert saved.accepted and saved.candidate == heads[0]
    assert journal.entries == [] and backend.runtime.volumes == set()
    assert eng.origin.head("autoforge/2") is None
    resumed = _resumed(eng, fake_github, agent)
    out = resumed.step()
    assert out.next_phase == "REVIEW", out.message
    _, second = _seeded(backend, "ANALYZE_EXECUTE")
    assert second.entry == first.entry and second.carried == heads[0]
    assert resumed.origin.head("autoforge/2") == heads[1] == resumed.state.current_head_sha


def test_a_stopped_launch_with_an_uncertain_release_exports_nothing(
    tmp_state_dir, fake_github, through_packing
):
    """The launch container may still write the volume: nothing is exported, the
    error propagates with the uncertainty noted, and the volume stays."""
    events, backend, journal = through_packing
    backend.uncertain = frozenset({ObjectKind.LAUNCH_CONTAINER})

    def agent(req) -> str:
        commit_in(req.cwd, "Implement the feature (#2)")
        raise ExecutionError("the agent's run was stopped")

    eng = make_engine(tmp_state_dir, agent, github=fake_github, cfg=_required(), origin=True)
    eng.step()
    with pytest.raises(ExecutionError, match="the agent's run was stopped") as raised:
        eng.step()
    assert any("launch container launch" in note for note in raised.value.__notes__)
    assert backend.artifacts == []
    (first,) = _seeded(backend, "ANALYZE_EXECUTE")
    assert all(
        not item.verified for item in CandidateReceipts(eng.state_root()).launches(first.entry)
    )
    assert "vol" in {obj.name for obj in journal.entries}


# -- the artifact's origin, through the engine (R1-F2) -----------------------------------


def test_a_correctly_labelled_artifact_from_another_location_is_never_imported(
    tmp_state_dir, fake_github, through_packing, tmp_path
):
    """The exporter leaves a valid artifact for exactly this launch, but outside the
    launch's export directory: refused, and nothing is published."""
    events, backend, journal = through_packing
    export = backend.export

    def elsewhere(objects, handle):
        artifact = export(objects, handle)
        planted = tmp_path / "planted" / Path(artifact.directory).name
        shutil.copytree(artifact.directory, planted)
        return Artifact(str(planted))

    backend.export = elsewhere  # type: ignore[method-assign]
    eng = make_engine(
        tmp_state_dir, _recording(events), github=fake_github, cfg=_required(), origin=True
    )
    eng.step()
    with pytest.raises(ControlResultValidationError, match="not in the launch's export directory"):
        eng.step()
    assert eng.origin.head("autoforge/2") is None and fake_github.effect_writes == []
    (analyze, *_rest) = _seeded(backend, "ANALYZE_EXECUTE")
    assert CandidateReceipts(eng.state_root()).carried(analyze.entry) == ""


def test_a_correctly_labelled_artifact_in_an_earlier_launchs_export_directory_is_refused(
    tmp_state_dir, fake_github, through_packing
):
    """Launch 2's exporter hands back launch 1's export directory, its manifest
    rewritten to name launch 2: the artifact is not from launch 2's own export
    directory, so it is refused."""
    events, backend, journal = through_packing
    export = backend.export
    heads: list[str] = []

    def reuse_the_first(objects, handle):
        artifact = export(objects, handle)
        if len(backend.artifacts) == 1:
            return artifact
        first = backend.artifacts[0]
        shutil.copy(Path(artifact.directory, ARTIFACT_PACK), first / ARTIFACT_PACK)
        shutil.copy(Path(artifact.directory, ARTIFACT_MANIFEST), first / ARTIFACT_MANIFEST)
        return Artifact(str(first))

    def agent(req) -> str:
        heads.append(commit_in(req.cwd, "Implement the feature (#2)"))
        return block(analyze_payload("0" * 40 if len(heads) == 1 else heads[-1]))

    backend.export = reuse_the_first  # type: ignore[method-assign]
    eng = make_engine(tmp_state_dir, agent, github=fake_github, cfg=_required(), origin=True)
    eng.step()
    with pytest.raises(ControlResultValidationError):
        eng.step()
    first, second = _seeded(backend, "ANALYZE_EXECUTE")[:2]
    launches = CandidateReceipts(eng.state_root()).launches(first.entry)
    refused = next(item for item in launches if item.git_input.launch == second.launch)
    assert "not in the launch's export directory" in refused.refusal
    assert eng.origin.head("autoforge/2") is None and fake_github.effect_writes == []
