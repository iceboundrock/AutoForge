"""The outer sandbox's backend seam: one lifecycle for every backend (ADR 0013 D13, #268).

A bwrap launch is one process whose death ends the sandbox; a container is
not. So the seam between the controller and a backend is a *lifecycle*,
not only "prepare a command". This module is that seam's executor-layer
half: the backend-neutral interface (:class:`SandboxBackend`), the data it
passes (:class:`RuntimeReport`, :class:`LaunchInputs`,
:class:`LifecycleObject`, :class:`Seed`, :class:`LaunchHandle`,
:class:`Artifact`, :class:`Removal`), the run step's prepared launch
(:class:`PreparedLaunch`), and bwrap behind it (:class:`BwrapBackend`).
The engine-layer half, which sequences the steps and journals them, is
:mod:`autoforge.sandbox_lifecycle`.

One launch's steps hand each other explicit data, all of it the same
launch's, on the same backend: the plan's compiled launch and the
launch's inputs go to the seed and the create step, the seed to the
create and the stage step, and the :class:`LaunchHandle` of what they
made to the run step's :meth:`SandboxBackend.prepare`, so that the command
runs in exactly the environment created and staged for it. The compiled
launch carries that :class:`LaunchBinding` (the backend that planned it
and the handle) to the executor, whose request is not wrapped.

The steps, in D13's order, and what bwrap does in each:

- **plan** (pure): the role-level request, completed by the provider's
  declaration, compiled into a typed, immutable launch. bwrap:
  :func:`~autoforge.sandbox.compile_launch`, as it is.
- **seed**: the launch's git input and its checked seed, in a
  controller-owned temporary directory, before any journal entry (#269,
  #292). bwrap: nothing.
- **create**: every object of the launch (the staging root, the credential
  projection, the volume, the stager container, the launch container),
  each container with all of its mounts and its network configured by its
  create, after one journal write that names them all. bwrap: nothing.
- **stage**: inside the stager container only; then the stager container
  is removed and the launch container started. bwrap: nothing.
- **run**: the provider's prepared launch, spawned by the executor
  (:func:`prepare`), in the environment the launch's handle names. This is
  the existing prepare seam, kept: executor requests are not wrapped.
  bwrap: :func:`autoforge.sandbox_bwrap.prepare`.
- **release**, right after the run step on every path: what holds the
  credential is removed (and the volume, when there is nothing to export).
  bwrap: nothing.
- **export**, for a change-producing launch: the artifact, untrusted, in
  the launch's export directory. bwrap: nothing is exported; the candidate
  is the worktree's ``HEAD`` (ADR 0005 D6.3), imported strictly (D6.5).
- **verify** is the engine's (#269), never a backend's.
- **the final clean-up**, once the verify step's outcome is saved: the
  volume and the export directory. bwrap: nothing.
- **reconcile** and **probe**, at a command's entry, after the read-only
  **runtime check**. bwrap: its runtime check reads nothing, it has
  nothing to reconcile, and its probe is :func:`autoforge.sandbox_bwrap.probe`.

Nothing a bwrap launch does changes behind the seam: every byte of its
argv, environment and options is :mod:`autoforge.sandbox_bwrap`'s, which
the seam calls through its module so that a test standing in for it
still reaches every launch.

What the seam keeps apart, so that no failure reads as another (ADR 0013
D17): a host that is **unsupported** is a :class:`ProbeResult` naming the
capability, which the gate refuses (``SandboxCapabilityError``); a
**policy denial** is the plan step's ``SandboxPolicyError``, raised as a
launch refusal; a **setup failure** of any step up to the run is
:class:`~autoforge.errors.SandboxLaunchRefusedError`, never an agent result;
a **command failure** is the command's own end, as today; and an
**uncertain** removal is :attr:`Removal.UNCERTAIN`, reported and left for
reconciliation, never "done". None of them ever runs a launch without the
sandbox: a launch no backend can prepare is refused (:func:`prepare`).

Executor layer: no workflow semantics, no journal, no state. A
:class:`LaunchOwner` is the labels an object carries, plain data. Only
this module imports :mod:`autoforge.sandbox_bwrap`; the engine, the
executor and ``doctor`` reach bwrap through it (a test pins that).
"""

from __future__ import annotations

import dataclasses
import enum
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from . import sandbox_bwrap
from .errors import SandboxLaunchRefusedError
from .sandbox import (
    BackendKind,
    LaunchDescription,
    SandboxFacts,
    SandboxRequest,
    compile_launch,
)

# The probe's result and the capabilities it names are bwrap's today; the
# seam passes them on, so that nothing else imports the backend for them.
from .sandbox_bwrap import Capability as Capability
from .sandbox_bwrap import ProbeResult as ProbeResult

# The network position of a helper container: no interface but loopback
# (ADR 0013 D9).
NETWORK_NONE = "none"


class PreparedLaunch(Protocol):
    """The run step's prepared launch: what the executor spawns in the command's place.

    The executor spawns :attr:`argv` with :attr:`spawn_env`,
    :attr:`pass_fds` and :attr:`preexec_fn` in a session of its own,
    calls :meth:`spawned` once the spawn returned or failed,
    :meth:`spawn_error` for a failure of the launch's own process before
    the backend ran, :meth:`collect` once the backend is reaped,
    :meth:`outcome` to classify the end (a setup failure raises), and
    :meth:`close` on every path. For a container, ``close()`` ends the
    container's PID namespace (ADR 0013 D13): killing the attached client
    does not stop the container. :class:`autoforge.sandbox_bwrap.BwrapLaunch`
    is one.
    """

    @property
    def argv(self) -> list[str]: ...

    @property
    def spawn_env(self) -> dict[str, str]: ...

    @property
    def pass_fds(self) -> tuple[int, ...]: ...

    @property
    def preexec_fn(self) -> Callable[[], None]: ...

    def spawned(self) -> None: ...

    def spawn_error(self, exc: BaseException) -> SandboxLaunchRefusedError: ...

    def collect(self) -> None: ...

    def outcome(
        self, *, returncode: int | None, ended_by_executor: bool, stderr: str
    ) -> SandboxFacts: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class RuntimeReport:
    """What the runtime check proved: the runtime's own report, read-only (ADR 0013 D16).

    For a container backend: the runtime's product and version, whether it
    is rootless, its local endpoint and its store (#271 names the fields it
    reads). It is read afresh at every command's entry, never from a cache,
    and a probe's result is reused only for the report it was proved on.
    bwrap has no runtime to read: its report is :data:`BWRAP_RUNTIME`.
    """

    backend: BackendKind
    product: str = ""
    version: str = ""
    rootless: bool | None = None
    endpoint: str = ""
    store: str = ""


BWRAP_RUNTIME = RuntimeReport(BackendKind.BWRAP)


@dataclass(frozen=True)
class LaunchOwner:
    """Whose an object is, as its labels say (ADR 0013 D6): plain data, no workflow.

    ``run`` is the run key, ``issue`` the issue number, ``phase`` the
    phase's name and ``launch`` the launch's id within the run.
    """

    run: str
    issue: int
    phase: str
    launch: str


@dataclass(frozen=True)
class LaunchInputs:
    """What one launch's steps take besides its plan (ADR 0013 D13): plain data, no workflow.

    ``owner`` is the labels its objects carry; ``workspace`` the issue
    worktree the launch works in; ``read_context`` the controller-written
    read context, "" for none. #269 adds the launch's git input (the bound
    revision), #290 the credential projection's items.
    """

    owner: LaunchOwner
    workspace: str
    read_context: str = ""


class ObjectKind(enum.Enum):
    """What a lifecycle object is (ADR 0013 D13, D6)."""

    STAGING_ROOT = "staging root"
    PROJECTION = "credential projection"
    VOLUME = "volume"
    STAGER_CONTAINER = "stager container"
    LAUNCH_CONTAINER = "launch container"
    EXPORTER = "exporter container"
    EXPORT_DIR = "export directory"


# The create step's objects, in the order they are made: every bind source
# before any container, the volume before the stager container, and that
# before the launch container (ADR 0013 D13).
CREATE_ORDER = (
    ObjectKind.STAGING_ROOT,
    ObjectKind.PROJECTION,
    ObjectKind.VOLUME,
    ObjectKind.STAGER_CONTAINER,
    ObjectKind.LAUNCH_CONTAINER,
)
# The export step's objects: the exporter and the directory it fills.
EXPORT_KINDS = (ObjectKind.EXPORTER, ObjectKind.EXPORT_DIR)
CONTAINER_KINDS = frozenset(
    {ObjectKind.STAGER_CONTAINER, ObjectKind.LAUNCH_CONTAINER, ObjectKind.EXPORTER}
)


@dataclass(frozen=True)
class LifecycleObject:
    """One object a launch makes outside the controller's process, with its owner.

    ``name`` is the object's name on its runtime or host, ``nonce`` the
    value only this object carries (an ambiguous create is reconciled by
    label and nonce, never re-issued blindly). A container's ``network``
    and ``mounts`` (the names of the other objects it mounts) are fixed by
    its create: a runtime configures them only then, so no later call can
    add or change one.
    """

    owner: LaunchOwner
    kind: ObjectKind
    name: str
    nonce: str
    network: str = ""
    mounts: tuple[str, ...] = ()


class Removal(enum.Enum):
    """What a removal could prove (ADR 0013 D13, D17)."""

    # Verified gone: removed now, or already gone as its labels and nonce show.
    REMOVED = "removed"
    # Not verified: the entry stays open for reconciliation, and the
    # removal is reported as uncertain, never as done.
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class Seed:
    """The seed step's result (ADR 0013 D4; #269 the git input, #292 the seed).

    ``identity`` is what state saves at the step's end (the manifest's
    digest); ``directory`` the controller-owned temporary directory that
    holds the seed until the launch's steps have used it. Both are empty
    for a backend that seeds nothing (:data:`NO_SEED`).
    """

    identity: str = ""
    directory: str = ""


NO_SEED = Seed()


@dataclass(frozen=True)
class Artifact:
    """What an exporter left in the launch's export directory: untrusted until verified."""

    directory: str


@dataclass(frozen=True)
class LaunchHandle:
    """What the create and stage steps made for one launch, as its later steps need it (D13).

    ``objects`` are the launch's objects still in place once its
    environment is ready (the launch container, started; the volume; what
    it mounts), each with the name and nonce its journal entry holds. The
    run step's :meth:`SandboxBackend.prepare` runs the command in the launch
    container named here and nowhere else. bwrap's handle names no object.
    """

    inputs: LaunchInputs
    seed: Seed
    objects: tuple[LifecycleObject, ...] = ()

    def find(self, kind: ObjectKind) -> LifecycleObject | None:
        """The launch's object of ``kind``, or ``None``."""
        return next((obj for obj in self.objects if obj.kind is kind), None)


@dataclass(frozen=True)
class LaunchBinding:
    """Which backend planned a compiled launch, and the handle of its environment (D13).

    Set by the plan step (:func:`bind`) on the launch it compiled, and again
    by ``SandboxPlan.describe`` once the engine's lifecycle made the launch's
    environment; carried to the executor on the launch itself, so the
    request is not wrapped.
    ``handle`` is ``None`` for a launch no lifecycle made an environment for
    (a repository-defined command): a backend that needs one refuses it.
    """

    backend: SandboxBackend
    handle: LaunchHandle | None = None


class LaunchEnvironment(Protocol):
    """What makes a planned launch's environment: the engine's lifecycle (D13).

    The plan a provider completes (``SandboxPlan.describe``) calls
    :meth:`establish` with the launch it just compiled, so that the seed,
    create and stage steps come after the plan step, and a launch the
    policy refuses makes nothing.
    """

    def establish(self, launch: LaunchDescription) -> LaunchHandle:
        """The seed, create and stage steps for ``launch``; the handle of what they made."""
        ...


class SandboxBackend(Protocol):
    """One backend's operations, which the engine sequences and journals (ADR 0013 D13).

    Every operation is the backend's mechanism only. The order, the journal
    and every decision are the engine's (:mod:`autoforge.sandbox_lifecycle`),
    and nothing here reads or writes controller state. A refusal of a step
    up to the run is :class:`~autoforge.errors.SandboxLaunchRefusedError`.
    """

    @property
    def kind(self) -> BackendKind: ...

    # -- at a command's entry (D16) -------------------------------------
    def runtime_check(self) -> RuntimeReport:
        """The runtime's own report, read-only, creating nothing; raise when it refuses."""
        ...

    def reconcile(
        self, entries: tuple[LifecycleObject, ...], runtime: RuntimeReport
    ) -> tuple[LifecycleObject, ...]:
        """Remove what ``entries`` name on ``runtime``; return those verified gone.

        Remove-only: it lists, stops and removes, and never creates or
        starts anything. An entry it cannot prove gone stays open.
        """
        ...

    def probe(self, *, home: str, environ: Mapping[str, str]) -> ProbeResult:
        """One real minimal launch of a real launch's shape (ADR 0005 D14.1, ADR 0013 D16)."""
        ...

    # -- one launch's steps (D13) -----------------------------------------
    def plan(self, request: SandboxRequest) -> LaunchDescription:
        """The compiled launch, typed and immutable; pure. A policy refusal raises."""
        ...

    def seed(self, inputs: LaunchInputs, launch: LaunchDescription) -> Seed:
        """The launch's checked seed, in a temporary directory; no runtime call."""
        ...

    def discard_seed(self, seed: Seed) -> None:
        """Remove the seed's temporary directory. Idempotent."""
        ...

    def objects(
        self, inputs: LaunchInputs, seed: Seed, launch: LaunchDescription
    ) -> tuple[LifecycleObject, ...]:
        """The objects the create step will make for ``launch``, each with its nonce.

        Makes nothing. The launch container's network and mounts follow the
        compiled launch, and every object carries ``inputs.owner``.
        """
        ...

    def make(self, obj: LifecycleObject, seed: Seed, launch: LaunchDescription) -> None:
        """Make one object the create step journaled, with every mount and its network."""
        ...

    def stage(self, stager: LifecycleObject, seed: Seed) -> None:
        """Inside the stager container: start it, the copy-in, the stager, the prover.

        Raise when the prover refuses. The stager container is not removed
        here: its removal is journaled first, as every removal is.
        """
        ...

    def start(self, container: LifecycleObject) -> None:
        """Start the launch container, once the stager container is gone."""
        ...

    def prepare(
        self,
        launch: LaunchDescription,
        handle: LaunchHandle | None,
        command: list[str],
        *,
        cwd: str | None,
        env: Mapping[str, str],
    ) -> PreparedLaunch:
        """The run step: ``command`` prepared to run inside ``launch``, or a refusal.

        In the launch container ``handle`` names, created and staged for
        this launch; ``None`` when no lifecycle made one, which a backend
        that needs an environment refuses.
        """
        ...

    def exporter(self, handle: LaunchHandle) -> tuple[LifecycleObject, ...]:
        """The exporter and its export directory; empty when nothing is exported."""
        ...

    def export(self, objects: tuple[LifecycleObject, ...], handle: LaunchHandle) -> Artifact:
        """Create and run the exporter the engine journaled; the artifact, untrusted.

        An export that fails raises :class:`SandboxLaunchRefusedError`: the
        engine saves it as the verify step's refusal (D17).
        """
        ...

    def remove(self, obj: LifecycleObject) -> Removal:
        """Remove one object; :attr:`Removal.REMOVED` only when it is verified gone."""
        ...


def _no_object(obj: LifecycleObject) -> SandboxLaunchRefusedError:
    return SandboxLaunchRefusedError(
        f"the bwrap backend makes no {obj.kind.value}, so it cannot act on {obj.name}"
    )


class BwrapBackend:
    """bwrap behind the seam, unchanged (ADR 0013 D13; ADR 0005).

    Plan is :func:`~autoforge.sandbox.compile_launch`; run is
    :func:`autoforge.sandbox_bwrap.prepare`; the probe is
    :func:`autoforge.sandbox_bwrap.probe`, which writes nothing outside a
    temporary directory it removes and so needs no journal. Seed, create,
    stage, the clean-up and reconcile do nothing: a bwrap launch makes no
    object that outlives its process. Asked to act on an object, it
    refuses, since it never made one.
    """

    @property
    def kind(self) -> BackendKind:
        return BackendKind.BWRAP

    def runtime_check(self) -> RuntimeReport:
        return BWRAP_RUNTIME

    def reconcile(
        self, entries: tuple[LifecycleObject, ...], runtime: RuntimeReport
    ) -> tuple[LifecycleObject, ...]:
        # bwrap journals nothing, so an entry is not one of its own: it
        # stays open for the backend that made it.
        return ()

    def probe(self, *, home: str, environ: Mapping[str, str]) -> ProbeResult:
        return sandbox_bwrap.probe(home=home, environ=environ)

    def plan(self, request: SandboxRequest) -> LaunchDescription:
        return compile_launch(request)

    def seed(self, inputs: LaunchInputs, launch: LaunchDescription) -> Seed:
        return NO_SEED

    def discard_seed(self, seed: Seed) -> None:
        return None

    def objects(
        self, inputs: LaunchInputs, seed: Seed, launch: LaunchDescription
    ) -> tuple[LifecycleObject, ...]:
        return ()

    def make(self, obj: LifecycleObject, seed: Seed, launch: LaunchDescription) -> None:
        raise _no_object(obj)

    def stage(self, stager: LifecycleObject, seed: Seed) -> None:
        raise _no_object(stager)

    def start(self, container: LifecycleObject) -> None:
        raise _no_object(container)

    def prepare(
        self,
        launch: LaunchDescription,
        handle: LaunchHandle | None,
        command: list[str],
        *,
        cwd: str | None,
        env: Mapping[str, str],
    ) -> PreparedLaunch:
        # bwrap makes its namespace at the spawn: a handle names nothing it made.
        if handle is not None and handle.objects:
            raise _no_object(handle.objects[0])
        return sandbox_bwrap.prepare(launch, command, cwd=cwd, env=env)

    def exporter(self, handle: LaunchHandle) -> tuple[LifecycleObject, ...]:
        return ()

    def export(self, objects: tuple[LifecycleObject, ...], handle: LaunchHandle) -> Artifact:
        raise SandboxLaunchRefusedError(
            "the bwrap backend exports nothing: its candidate is the worktree's HEAD "
            "(ADR 0005 D6.3)"
        )

    def remove(self, obj: LifecycleObject) -> Removal:
        # Never "removed": bwrap made no such object, so it can prove nothing.
        return Removal.UNCERTAIN


BWRAP = BwrapBackend()

# Which backend prepares a launch that no plan step bound (a description
# built by hand, as the backend's own tests do), by its exact type: only
# bwrap, which needs no other step, so a type no backend registered is
# refused, never handed to bwrap.
_BY_LAUNCH: dict[type, SandboxBackend] = {LaunchDescription: BWRAP}


def default_backend() -> SandboxBackend:
    """The backend every enforced launch uses: bwrap, the only one (#273 makes one selectable)."""
    return BWRAP


def backend_for(launch: object) -> SandboxBackend:
    """The backend that prepares an unbound ``launch``; one no backend knows is refused.

    Never a fallback: a launch meant to be enforced is refused rather than
    run without the sandbox, or under a backend that did not plan it.
    """
    backend = _BY_LAUNCH.get(type(launch))
    if backend is None:
        raise SandboxLaunchRefusedError(
            f"no sandbox backend prepares a {type(launch).__name__}, so the launch is refused; "
            "it never runs without the sandbox"
        )
    return backend


def bind(
    launch: LaunchDescription, backend: SandboxBackend, handle: LaunchHandle | None = None
) -> LaunchDescription:
    """``launch`` bound to the backend that planned it and, once made, its environment."""
    return dataclasses.replace(launch, binding=LaunchBinding(backend, handle))


def prepare(
    launch: LaunchDescription,
    command: list[str],
    *,
    cwd: str | None,
    env: Mapping[str, str],
) -> PreparedLaunch:
    """The run step's prepare seam: ``command`` inside ``launch``.

    By the backend that planned the launch, in the environment its handle
    names (:class:`LaunchBinding`); a launch no plan step bound, by its
    type (:func:`backend_for`).
    """
    binding = launch.binding
    if binding is None:
        return backend_for(launch).prepare(launch, None, command, cwd=cwd, env=env)
    if not isinstance(binding, LaunchBinding):
        raise SandboxLaunchRefusedError(
            f"the launch is bound to a {type(binding).__name__}, which names no sandbox backend, "
            "so it is refused; it never runs without the sandbox"
        )
    return binding.backend.prepare(launch, binding.handle, command, cwd=cwd, env=env)
