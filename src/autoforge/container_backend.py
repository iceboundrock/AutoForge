"""The container backend: ADR 0013's lifecycle on a rootless Docker Engine or Podman (#271).

:class:`ContainerBackend` implements :class:`~autoforge.sandbox_backend.SandboxBackend`
through the runtime's CLI (:mod:`autoforge.container_runtime`), with every
create's arguments rendered by the security policy
(:mod:`autoforge.sandbox_container`, the only place a runtime flag is
written) and the source staged by :mod:`autoforge.source_staging`. The
engine sequences and journals the steps (:mod:`autoforge.sandbox_lifecycle`);
nothing here orders a step, writes a journal or reads state.

**Names, labels and ownership** (D6). Every object a launch makes is named
``af-<issue>-<run hash>-<launch>-<kind>``: the run's key (hashed, so any
run key fits a runtime name), the issue and the launch id, so two runs,
two issues or two launches never share a name, and a worker can never
remove another's object by name. Each also carries labels (:data:`LABELS`):
the run hash, the issue, the phase, the launch, its kind and the nonce its
journal entry holds. A removal removes a container or volume only when its
nonce label is the entry's (:meth:`ContainerBackend.remove`): an object of
the same name that is not the entry's (another launch, a stale leftover)
is left in place and the removal is uncertain, never "done".

**A create that collides or is denied fails safely.** A name the runtime
already holds is refused (never adopted, never removed: ownership is
proved by nonce, not by name), and so is a create the runtime denies; the
lifecycle's clean-up then removes what the launch did make. A create is
proved by the object's own labels, read back: Docker's ``volume create``
succeeds for a name it holds and returns that volume unchanged, so a
volume or container that does not carry exactly this entry's labels and
nonce is a collision, refused and never mounted. The export directory,
the controller's own path, is proved the same way by an owner record
beside it (:func:`export_owner_path`): one that was there already, or that
an interrupted step made and did not record, is never removed.

**The steps**, as ADR 0013 D13 orders them:

- *seed*: the seed built and checked on the host (#292), before any
  journal entry;
- *create*: the staging root (bind form; written and proved by
  ``SourceStaging``, re-proved before each create that binds it), the
  ``/workspace`` volume, the stager container (``--network none``, no
  projection) and the launch container (the launch's network position and
  projection), every mount configured at its create; PID 1 of each is the
  image's watcher (``container_watch.py``) with the container's deadline;
- *stage*: the stager container started; in the copy-in form the seed pack
  streamed to the stager's ``receive`` on its standard input; the stager's
  exec; then the prover's, with the tree id read from the shared store and
  the ref set from the controller's own manifest; a verdict that is not a
  proof refuses the launch (D17). The stager container is removed by the
  lifecycle before the launch container starts;
- *run*: :class:`ContainerLaunch`, the command as the agent through
  ``exec``, attached, with the credential plan's positive allow-list (an
  agent, #290) or the command's (a repository command, no model
  credential) and nothing else of the launch's environment; ``close()``
  kills every process of the agent's UID in the container and proves none
  is left, so nothing of the run outlives it while the container (PID 1,
  another UID) serves a later process of the same launch (Pi's auth check,
  then Pi). A teardown not proved is uncertain, and no later process of the
  launch starts there. The release removes the container;
- *export*: the exporter container (offline, the volume read-only, binding
  nothing), its ``head`` and ``pack`` execs; the pack streamed into the
  launch's export directory, bounded, and the manifest written from the
  launch's git input, the claimed candidate and the pack measured on the
  host (``CandidateManifest.for_input``). The artifact is untrusted until
  the engine's verify step imports it strictly;
- *removal* and *reconciliation*: remove-only, verified by the runtime's
  own listing; never a create or a start.

**The shared-filesystem probe** (D12, :meth:`ContainerBackend.staging_method`).
``sandbox.container.transfer: copy`` gives copy-in with no probe. Otherwise
a nonce file is written into the probe's own staging root
(:class:`~autoforge.sandbox_container.ProbeRoot`), which a probe container
binds read-only at ``/source``, and read back from inside: only the same
nonce proves a bind; anything else (another file, none, an error) is
copy-in. The probe's objects are created only after the probe's journal
names them, and only on the runtime that journal is bound to (D6 item 9).
The journal, its lock and its sweep are #272's (:class:`ProbeJournal`);
until they land :data:`NO_PROBE_JOURNAL` refuses, so no probe object is
ever created without one. A probe entry is closed only once its object is
proved gone by its nonce, whatever its create answered (a create can take
effect and lose its answer); one not proved gone stays open. The staging
form decided is bound to the runtime report it was proved on, and a later
runtime check that reports another runtime or store drops it.

**Not selectable yet.** The configuration loader has no ``sandbox.backend``
(#273), and the engine's journal is ``NO_JOURNAL`` until #272: no launch
reaches this module. Executor layer: no workflow, no journal, no state.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import posixpath
import re
import secrets
import shutil
import stat
import struct
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from . import materialize as materialize_mod
from .candidate_artifact import (
    ARTIFACT_MANIFEST,
    ARTIFACT_PACK,
    EXPORT_DEADLINE_SECONDS,
    MAX_ARTIFACT_PACK_BYTES,
    CandidateManifest,
)
from .container_auth import CredentialPlan, command_environment
from .container_image import CLI_COMMANDS, HELPER_DIR, HELPERS, ImageRef, entrypoint_problem
from .container_runtime import RuntimeClient, RuntimeIdentity
from .errors import (
    CandidateNotMaterializableError,
    MaterializationError,
    SandboxCleanupUncertainError,
    SandboxLaunchRefusedError,
    SandboxPolicyError,
)
from .executor import ExecutionRequest, ExecutionResult, execute
from .git_input import SEED_MANIFEST_NAME
from .redaction import redact
from .safefs import SafeRoot
from .sandbox import (
    BackendKind,
    LaunchDescription,
    SandboxFacts,
    SandboxRequest,
    compile_container_launch,
)
from .sandbox_backend import (
    NETWORK_NONE,
    Artifact,
    Capability,
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
from .sandbox_container import (
    HOME_TARGET,
    NO_PROJECTION,
    WORKSPACE_TARGET,
    ContainerNetwork,
    ContainerRole,
    ContainerSpec,
    HostContext,
    ProbeRoot,
    Projection,
    RuntimeProduct,
    StagingMethod,
    StagingRoot,
    check_exec_env_name,
    client_environment,
    create_argv,
    exec_argv,
    network_for,
)
from .seed_pack import SEED_PACK_NAME, SeedSettings, isolated_store, parse_manifest
from .source_staging import SourceStaging, StagedSource

# The labels every object carries (D6): the run, the issue, the phase, the
# launch, the object's kind and its journal entry's nonce.
LABEL_RUN = "io.autoforge.run"
LABEL_ISSUE = "io.autoforge.issue"
LABEL_PHASE = "io.autoforge.phase"
LABEL_LAUNCH = "io.autoforge.launch"
LABEL_KIND = "io.autoforge.kind"
LABEL_NONCE = "io.autoforge.nonce"
LABEL_PROBE = "io.autoforge.probe"
LABELS = (LABEL_RUN, LABEL_ISSUE, LABEL_PHASE, LABEL_LAUNCH, LABEL_KIND, LABEL_NONCE)

_KIND_SUFFIX = {
    ObjectKind.VOLUME: "workspace",
    ObjectKind.STAGER_CONTAINER: "stager",
    ObjectKind.LAUNCH_CONTAINER: "launch",
    ObjectKind.EXPORTER: "exporter",
}
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")
_MAX_NAME = 120

# Bounds of the lifecycle's runtime calls.
CALL_TIMEOUT_SECONDS = 120
STAGE_TIMEOUT_SECONDS = 1800
# The teardown at a run step's close: how long the agent's processes are
# given to vanish after the kill, and the bound of the exec that kills them.
TEARDOWN_WAIT_SECONDS = 10
KILL_TIMEOUT_SECONDS = 60
# How long a container lives at most, whatever ends it first: its
# watcher's deadline (container_watch.py); the release removes it sooner.
DEFAULT_DEADLINE_SECONDS = 24 * 3600
# What a container's teardown records: every process of the agent's UID
# killed at close, the container (its PID namespace) removed at the release.
TEARDOWN = "agent processes killed at close; the container removed at the release, uncounted"
WATCH = "PID 1 is the image's watcher, with the container's deadline (#272 adds the death-watch)"

_NOT_FOUND = re.compile(r"no such|not found|does not exist|no container|no volume", re.I)
# A version as a CLI's ``--version`` prints it, among other words.
_VERSION_TOKEN = re.compile(r"(?<![0-9.])[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?")
_EXISTS = re.compile(r"already in use|already exists|conflict", re.I)
_DENIED = re.compile(r"permission denied|access denied|not permitted|unauthori[sz]ed", re.I)
# The runtimes' own exit statuses for an ``exec`` that never ran its command.
_EXEC_SETUP_STATUSES = frozenset({125, 126, 127})


def _refused(text: str) -> SandboxLaunchRefusedError:
    return SandboxLaunchRefusedError(f"the container backend refuses the launch: {text}")


def _detail(res: ExecutionResult) -> str:
    text = (res.stderr or res.stdout or "").strip()
    return redact(text.splitlines()[-1] if text else "")[:300]


def run_hash(run: str) -> str:
    """The run key as a runtime name allows it: 12 hex digits of its SHA-256."""
    return hashlib.sha256(run.encode("utf-8")).hexdigest()[:12]


def object_name(owner: LaunchOwner, kind: ObjectKind) -> str:
    """The runtime name of ``owner``'s object of ``kind``: run, issue and launch scoped."""
    launch = _SAFE.sub("-", owner.launch)[:48]
    name = f"af-{owner.issue}-{run_hash(owner.run)}-{launch}-{_KIND_SUFFIX[kind]}"
    return name[:_MAX_NAME]


def labels_for(obj: LifecycleObject) -> dict[str, str]:
    """The labels ``obj`` is created with."""
    return {
        LABEL_RUN: run_hash(obj.owner.run),
        LABEL_ISSUE: str(obj.owner.issue),
        LABEL_PHASE: obj.owner.phase,
        LABEL_LAUNCH: obj.owner.launch,
        LABEL_KIND: _KIND_SUFFIX.get(obj.kind, obj.kind.name.lower()),
        LABEL_NONCE: obj.nonce,
    }


def _helper(name: str) -> list[str]:
    return ["python3", "-I", f"{HELPER_DIR}/{HELPERS[name]}"]


def _is_command(inputs: LaunchInputs) -> bool:
    """Whether the launch is a repository command (``EXPORT_COMMAND``), not an agent."""
    return inputs.git_input is not None and inputs.git_input.export_command


# The teardown, run as the agent's UID in the launch container: every other
# process of that UID killed (``kill(-1)`` spares PID 1 and the caller),
# then the container's own ``/proc`` read; killed and read again until two
# reads 0.1 s apart find none alive (a zombie is dead), within ``argv[1]``
# seconds. Its verdict is one JSON line; it exits 0 only when the teardown
# is proved.
_TEARDOWN = (
    "import json, os, signal, sys, time\n"
    "me, uid = os.getpid(), str(os.getuid())\n"
    "def alive():\n"
    "    left = 0\n"
    "    for pid in os.listdir('/proc'):\n"
    "        if not pid.isdigit() or int(pid) in (1, me):\n"
    "            continue\n"
    "        try:\n"
    "            with open('/proc/' + pid + '/status') as f:\n"
    "                lines = dict(l.split(':', 1) for l in f.read().splitlines() if ':' in l)\n"
    "        except OSError:\n"
    "            continue\n"
    "        state = lines.get('State', '').split()[:1]\n"
    "        if uid in lines.get('Uid', '').split() and state not in (['Z'], ['X']):\n"
    "            left += 1\n"
    "    return left\n"
    "def kill():\n"
    "    try:\n"
    "        os.kill(-1, signal.SIGKILL)\n"
    "    except ProcessLookupError:\n"
    "        pass\n"
    "end = time.monotonic() + float(sys.argv[1])\n"
    "while True:\n"
    "    kill()\n"
    "    time.sleep(0.05)\n"
    "    left = alive()\n"
    "    if not left:\n"
    "        time.sleep(0.1)\n"
    "        left = alive()\n"
    "        if not left:\n"
    "            break\n"
    "    if time.monotonic() >= end:\n"
    "        break\n"
    "print(json.dumps({'teardown': 'survivors' if left else 'proved', 'survivors': left}))\n"
    "sys.exit(1 if left else 0)\n"
)


# -- the probe's journal (D6 item 9; #272) ---------------------------------------------


@dataclass(frozen=True)
class ProbeObject:
    """One object a probe creates: its kind (``volume`` or ``container``), name and nonce."""

    kind: str
    name: str
    nonce: str


class ProbeJournal(Protocol):
    """What the shared-filesystem probe needs of #272's probe journal (D6 item 9).

    ``root`` is the probe's own staging root, in the probe's directory the
    journal owns (made, with its lock, under L1); ``runtime`` the runtime
    the journal is bound to. ``record`` writes the probe's objects before
    any is created; ``close`` closes them once verified gone.
    """

    @property
    def root(self) -> ProbeRoot: ...

    @property
    def runtime(self) -> RuntimeReport: ...

    def record(self, objects: tuple[ProbeObject, ...]) -> None: ...

    def close(self, objects: tuple[ProbeObject, ...]) -> None: ...


class _NoProbeJournal:
    """The probe journal before #272: it names no root and no runtime, and refuses every write."""

    @staticmethod
    def _missing() -> SandboxLaunchRefusedError:
        return _refused(
            "the shared-filesystem probe creates objects only through the probe's journal, "
            "which #272 implements; none exists yet"
        )

    @property
    def root(self) -> ProbeRoot:
        raise self._missing()

    @property
    def runtime(self) -> RuntimeReport:
        raise self._missing()

    def record(self, objects: tuple[ProbeObject, ...]) -> None:
        raise self._missing()

    def close(self, objects: tuple[ProbeObject, ...]) -> None:
        raise self._missing()


NO_PROBE_JOURNAL: ProbeJournal = _NoProbeJournal()


# -- the run step (D13) -----------------------------------------------------------------


class ContainerLaunch:
    """The run step's prepared launch: ``exec`` of the command in the launch container.

    The attached client is the executor's child. Killing it does not end
    what it started in the container, so :meth:`close` kills every process
    of the agent's UID there (``kill -KILL -1`` as that UID, which spares
    PID 1, the watcher, of another UID) and proves, from the container's
    own ``/proc``, that none is left (:data:`_TEARDOWN`); the release then
    removes the container. The client's configuration directory lives until
    ``close``.

    A teardown that is not proved (the kill's exec refused, timed out, or
    reporting a survivor) is uncertain, never a clean close: the backend
    refuses every later process of the launch in that container
    (:meth:`ContainerBackend.prepare`), and ``close`` raises
    :class:`~autoforge.errors.SandboxCleanupUncertainError`, or, when
    another exception is already ending the launch, adds a note to it.
    """

    def __init__(
        self,
        backend: ContainerBackend,
        container: str,
        argv: list[str],
        env: dict[str, str],
        config_dir: str,
        facts: SandboxFacts,
    ) -> None:
        self._backend = backend
        self._container = container
        self.argv = argv
        self.spawn_env = env
        self.pass_fds: tuple[int, ...] = ()
        self.preexec_fn: Callable[[], None] = lambda: None
        self._config_dir = config_dir
        self._facts = facts
        self._closed = False

    def spawned(self) -> None:
        return None

    def spawn_error(self, exc: BaseException) -> SandboxLaunchRefusedError:
        failure = f"the runtime client could not be started ({type(exc).__name__})"
        return SandboxLaunchRefusedError(
            _refused(failure).args[0],
            dataclasses.replace(self._facts, enforced=False, setup_failure=failure),
        )

    def collect(self) -> None:
        return None

    def outcome(
        self, *, returncode: int | None, ended_by_executor: bool, stderr: str
    ) -> SandboxFacts:
        """Enforced, unless the runtime itself says the command never ran (D17)."""
        if ended_by_executor or returncode is None or returncode not in _EXEC_SETUP_STATUSES:
            return self._facts
        first = (stderr or "").strip().splitlines()[:1]
        line = first[0] if first else ""
        if line.startswith(("Error response from daemon", "Error: ", "OCI runtime exec failed")):
            reason = redact(line)[:300]
            raise SandboxLaunchRefusedError(
                _refused(f"the command never ran in its container: {reason}").args[0],
                dataclasses.replace(self._facts, enforced=False, setup_failure=reason),
            )
        return self._facts

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            problem = self._backend.end_agent_processes(self._container)
        finally:
            shutil.rmtree(self._config_dir, ignore_errors=True)
        if not problem:
            return
        text = (
            f"the agent's processes in the launch container {self._container} were not proved "
            f"gone ({problem}): no later process of the launch starts there, and the release "
            "removes the container"
        )
        ending = sys.exc_info()[1]
        if ending is not None:
            # Never masked: the exception that ends the launch propagates.
            ending.add_note(text)
            return
        raise SandboxCleanupUncertainError(text, (f"launch container {self._container}",))


# -- the backend ------------------------------------------------------------------------


class ContainerBackend:
    """ADR 0013's lifecycle on one rootless runtime (see the module docstring).

    ``client`` is the runtime's (:func:`~autoforge.container_runtime.detect`
    chose it); ``host`` the controller's facts the policy checks;
    ``common_dir`` the git common dir, the shared store the seed and the
    tree id are read from; ``sandbox_key`` the issue's; ``image`` the image
    every container runs, by id; ``transfer`` the operator's
    ``sandbox.container.transfer``; ``credentials`` makes an agent
    launch's credential plan (#290's :func:`~autoforge.container_auth.plan_credentials`
    for the launch's provider, profile and environment): its projection is
    the launch container's create's, and its positive allow-list
    (:meth:`~autoforge.container_auth.CredentialPlan.container_environment`)
    is all the run step passes of the launch's environment. An agent launch
    without one is refused; a repository command never asks for one and
    gets no model credential (:func:`~autoforge.container_auth.command_environment`).
    ``probe_journal`` is #272's; ``nonce`` makes each object's nonce.
    """

    def __init__(
        self,
        client: RuntimeClient,
        *,
        host: HostContext,
        common_dir: str,
        sandbox_key: str,
        seed_settings: SeedSettings,
        image: ImageRef | None,
        transfer: str = "auto",
        credentials: Callable[[LaunchDescription], CredentialPlan] | None = None,
        probe_journal: ProbeJournal = NO_PROBE_JOURNAL,
        deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
        nonce: Callable[[], str] | None = None,
        staging: Callable[[StagingMethod], SourceStaging] | None = None,
        runner: Callable[[ExecutionRequest], ExecutionResult] | None = None,
    ) -> None:
        if transfer not in ("auto", "copy"):
            raise _refused(f"sandbox.container.transfer is {transfer!r}, not auto or copy")
        self._client = client
        self._host = host
        self._common = os.path.realpath(common_dir)
        self._key = sandbox_key
        self._settings = seed_settings
        self._image = image
        self._transfer = transfer
        self._credentials = credentials
        self._probe_journal = probe_journal
        self._deadline = deadline_seconds
        self._nonce = nonce or (lambda: secrets.token_hex(12))
        self._make_staging = staging or (
            lambda method: SourceStaging(self._common, self._key, self._settings, method)
        )
        self._runner = runner or execute
        self._identity: RuntimeIdentity | None = None
        self._method: StagingMethod | None = None
        # The runtime report the staging form was decided on: the decision
        # holds only while each runtime check reports exactly that runtime
        # and store (ADR 0013 D6 item 10, D12).
        self._method_report: RuntimeReport | None = None
        # Each launch's inputs as the create step was given them, completed
        # with the seed's manifest digest (the lifecycle binds it first).
        self._launches: dict[LaunchOwner, LaunchInputs] = {}
        # An agent launch's credential plan until its launch container's
        # create (the projection's value is needed there alone), and the
        # plan's allow-listed environment, which holds no credential, until
        # the launch container is removed.
        self._plans: dict[LaunchOwner, CredentialPlan] = {}
        self._run_env: dict[LaunchOwner, dict[str, str]] = {}
        # Launch containers whose agent processes were not proved gone at a
        # close: no later process of the launch starts in one.
        self._tainted: set[str] = set()
        # Each agent CLI's version as a launch container of the image
        # reported it (D7), by image id: the image is content-addressed, so
        # what one container of it reports, every container of it holds.
        self._versions: dict[str, tuple[tuple[str, str], ...]] = {}
        self._staging: SourceStaging | None = None
        self._staged: dict[str, StagedSource] = {}

    # -- identity ------------------------------------------------------------------
    @property
    def kind(self) -> BackendKind:
        return BackendKind.CONTAINER

    @property
    def product(self) -> RuntimeProduct:
        if self._identity is None:
            raise _refused("the runtime check has not run, so no runtime is known")
        return self._identity.product

    @property
    def method(self) -> StagingMethod:
        if self._method is None:
            raise _refused("no staging form is decided: the probe has not run (ADR 0013 D12)")
        if self._identity is None or self._method_report != self._identity.report():
            raise _refused(
                "the staging form was decided on another runtime or store than the one the "
                "last runtime check reported, so it is not reused (ADR 0013 D6 item 10, D12)"
            )
        return self._method

    def _image_id(self) -> str:
        if self._image is None:
            raise _refused("no image is configured (sandbox.container.image, ADR 0013 D7)")
        return self._image.id

    def _facts(self, launch: LaunchDescription, handle: LaunchHandle | None = None) -> SandboxFacts:
        """The launch's facts (ADR 0013 D16): the runtime, the image and its CLIs' versions
        as read inside, the staging form, and the seed's diagnostics (D2)."""
        identity = self._identity
        image = self._image
        return SandboxFacts(
            enforced=True,
            backend=BackendKind.CONTAINER.value,
            teardown=TEARDOWN,
            network=network_for(ContainerRole.LAUNCH, launch.network).value,
            runtime=identity.name if identity else "",
            runtime_version=identity.version if identity else "",
            rootless=identity.rootless if identity else None,
            image_id=image.digest if image else "",
            staging=self._method.value if self._method else "",
            watch=WATCH,
            diagnostics=handle.seed.diagnostics if handle is not None else (),
            cli_versions=self._versions.get(image.id, ()) if image else (),
        )

    # -- at a command's entry (D16) ---------------------------------------------------
    def runtime_check(self) -> RuntimeReport:
        """The runtime's own report, read now: never from a cache (D6 item 10).

        A staging form decided on another report (another runtime, or the
        same one with its store moved) is dropped here: it was proved on a
        store this runtime no longer reports, so the next launch decides
        again, through its own probe (D12).
        """
        identity = self._client.identity()
        self._identity = identity
        report = identity.report()
        if self._method_report is not None and self._method_report != report:
            self._method = None
            self._method_report = None
            self._staging = None
        return report

    def reconcile(
        self, entries: tuple[LifecycleObject, ...], runtime: RuntimeReport
    ) -> tuple[LifecycleObject, ...]:
        """Remove what ``entries`` name, in their order; those verified gone. Remove-only."""
        if self._identity is None or self._identity.report() != runtime:
            raise _refused("reconciliation runs only on the runtime the check just proved")
        return tuple(obj for obj in entries if self.remove(obj) is Removal.REMOVED)

    def probe(self, *, home: str, environ: Mapping[str, str]) -> ProbeResult:
        """The image resolvable by id, and the staging form decided (D16 items 3 and 6, D12).

        The rest of D16's minimal launch (user, namespaces, network, the
        death-watch, the deadline) is measured by #270's measurement and
        #272's watcher, which the backend's selection (#273) runs; until
        then the backend is not selectable.
        """
        identity = self._identity
        if identity is None:
            return ProbeResult(False, "the runtime check has not run", "", Capability.UNKNOWN)
        try:
            image = self._image_id()
        except SandboxLaunchRefusedError as exc:
            return ProbeResult(False, str(exc), identity.version, Capability.UNKNOWN)
        res = self._client.run(["image", "inspect", "--format", "{{.Id}}", image])
        if res.exit_code != 0:
            return ProbeResult(
                False,
                f"the image {image} is not in the runtime's store ({_detail(res)})",
                identity.version,
                Capability.UNKNOWN,
            )
        problem = entrypoint_problem(self._client, image)
        if problem:
            return ProbeResult(False, problem, identity.version, Capability.UNKNOWN)
        try:
            self.staging_method()
        except SandboxLaunchRefusedError as exc:
            return ProbeResult(False, str(exc), identity.version, Capability.BIND)
        return ProbeResult(True, "", f"{identity.name} {identity.version}")

    # -- the shared-filesystem probe (D12) ----------------------------------------------
    def staging_method(self) -> StagingMethod:
        """The staging form for this runtime: ``copy`` when forced, else what the probe proves.

        Decided once per runtime report: the decision is bound to the report
        the last runtime check read, and a later check reporting another
        runtime or store drops it (:meth:`runtime_check`).
        """
        identity = self._identity
        if identity is None:
            raise _refused("the runtime check has not run, so no staging form can be decided")
        report = identity.report()
        if self._method is not None and self._method_report == report:
            return self._method
        if self._transfer == "copy":
            method = StagingMethod.COPY
        else:
            method = StagingMethod.BIND if self._shared_filesystem() else StagingMethod.COPY
        self._method = method
        self._method_report = report
        self._staging = self._make_staging(method)
        return method

    def _shared_filesystem(self) -> bool:
        """Whether a nonce written on the host reads back, unchanged, from a container's /source."""
        journal = self._probe_journal
        root = journal.root
        identity = self._identity
        if identity is None or journal.runtime != identity.report():
            raise _refused(
                "the probe's journal is bound to another runtime than the one proved, so the "
                "probe creates nothing (ADR 0013 D6 item 9)"
            )
        _make_probe_root(root)
        nonce = secrets.token_hex(16)
        nonce_path = os.path.join(root.path, "nonce")
        fd = os.open(nonce_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w") as handle:
            handle.write(nonce)
        probe_id = root.probe
        volume = ProbeObject(
            "volume", f"af-probe-{_SAFE.sub('-', probe_id)[:40]}-workspace", self._nonce()
        )
        container = ProbeObject(
            "container", f"af-probe-{_SAFE.sub('-', probe_id)[:40]}", self._nonce()
        )
        journal.record((volume, container))
        shared = False
        left: tuple[ProbeObject, ...] = ()
        try:
            labels = {LABEL_PROBE: probe_id}
            self._volume_create(volume.name, {**labels, LABEL_NONCE: volume.nonce})
            spec = ContainerSpec(
                ContainerRole.STAGER,
                ContainerNetwork.NONE,
                volume.name,
                staging=StagingMethod.BIND,
                source=root,
            )
            self._container_create(spec, container.name, {**labels, LABEL_NONCE: container.nonce})
            self._start(container.name)
            res = self._exec(
                container.name,
                [*_helper_read(), "/source/nonce"],
                timeout=CALL_TIMEOUT_SECONDS,
            )
            shared = res.exit_code == 0 and res.stdout == nonce
        finally:
            # Every recorded object, whether or not its create returned: a
            # create can take effect on the runtime and lose its answer (a
            # timeout, a dropped connection), so an entry closes only once its
            # object is proved gone by its nonce, never because a create
            # "failed". The container goes first: it mounts the volume.
            left = self._close_probe_objects(journal, (container, volume))
            try:
                os.unlink(nonce_path)
            except FileNotFoundError:
                pass
        if left:
            raise _refused(
                "the probe could not prove "
                + ", ".join(f"its {obj.kind} {obj.name}" for obj in left)
                + " gone; the probe journal's entries stay open for its sweep (#272), and no "
                "staging form is decided from it"
            )
        return shared

    def _close_probe_objects(
        self, journal: ProbeJournal, objects: tuple[ProbeObject, ...]
    ) -> tuple[ProbeObject, ...]:
        """Remove each probe object by its nonce, close those proved gone; the rest, open."""
        gone: list[ProbeObject] = []
        left: list[ProbeObject] = []
        for obj in objects:
            try:
                outcome = self._remove_named(obj.kind, obj.name, obj.nonce)
            except Exception:  # noqa: BLE001 - not proved gone: its entry stays open
                outcome = Removal.UNCERTAIN
            (gone if outcome is Removal.REMOVED else left).append(obj)
        if gone:
            journal.close(tuple(gone))
        return tuple(left)

    # -- one launch (D13) ----------------------------------------------------------
    def plan(self, request: SandboxRequest) -> LaunchDescription:
        """The compiled launch, as the container's policy compiler refuses or admits it: pure.

        :func:`~autoforge.sandbox.compile_container_launch`: ADR 0005 C7 as
        amended admits the engine's own staging root at ``/source``, in the
        bind form only, read-only, from the issue's staging directory under
        the controller's common dir, and nothing else there.
        """
        stage_dir = None
        if self.method is StagingMethod.BIND:
            stage_dir = posixpath.dirname(StagingRoot(self._common, self._key, "launch").path)
        return compile_container_launch(request, stage_dir=stage_dir)

    def seed(self, inputs: LaunchInputs, launch: LaunchDescription) -> Seed:
        self.method  # noqa: B018 - the staging form is decided before any seed
        assert self._staging is not None
        return self._staging.seed(inputs)

    def discard_seed(self, seed: Seed) -> None:
        if self._staging is not None:
            self._staging.discard_seed(seed)

    def objects(
        self, inputs: LaunchInputs, seed: Seed, launch: LaunchDescription
    ) -> tuple[LifecycleObject, ...]:
        owner = inputs.owner
        self._launches[owner] = inputs
        # The credential plan first: a launch it refuses makes nothing.
        self._plan_launch(owner, launch)
        objects: list[LifecycleObject] = []
        mounts: list[str] = []
        if self.method is StagingMethod.BIND:
            assert self._staging is not None
            root = self._staging.staging_root(owner)
            assert root is not None
            objects.append(
                LifecycleObject(owner, ObjectKind.STAGING_ROOT, root.path, self._nonce())
            )
            mounts.append(root.path)
        volume = object_name(owner, ObjectKind.VOLUME)
        objects.append(LifecycleObject(owner, ObjectKind.VOLUME, volume, self._nonce()))
        mounts.append(volume)
        objects.append(
            LifecycleObject(
                owner,
                ObjectKind.STAGER_CONTAINER,
                object_name(owner, ObjectKind.STAGER_CONTAINER),
                self._nonce(),
                network=NETWORK_NONE,
                mounts=tuple(mounts),
            )
        )
        objects.append(
            LifecycleObject(
                owner,
                ObjectKind.LAUNCH_CONTAINER,
                object_name(owner, ObjectKind.LAUNCH_CONTAINER),
                self._nonce(),
                network=network_for(ContainerRole.LAUNCH, launch.network).value,
                mounts=tuple(mounts),
            )
        )
        return tuple(objects)

    def _inputs_of(self, obj: LifecycleObject) -> LaunchInputs:
        inputs = self._launches.get(obj.owner)
        if inputs is None:
            raise _refused(f"{obj.kind.value} {obj.name} belongs to no launch this backend seeded")
        return inputs

    def _plan_launch(self, owner: LaunchOwner, launch: LaunchDescription) -> None:
        """The launch's credential plan and run environment, made once (ADR 0013 D14; #290).

        A repository command (``validation.commands``, the pre-merge
        commands) gets no model credential and no provider setting: only
        :func:`~autoforge.container_auth.command_environment`'s names, at the
        run step. An agent launch gets its plan from ``credentials``, and
        none is a refusal: the container never receives the launch's
        environment by any other rule.
        """
        inputs = self._launches[owner]
        if _is_command(inputs):
            return
        if owner in self._run_env:
            return
        if self._credentials is None:
            raise _refused(
                "an agent launch's container gets its credential and its environment only "
                "from #290's credential plan (container_auth.plan_credentials), and none was "
                "given for this launch"
            )
        try:
            plan = self._credentials(launch)
        except SandboxPolicyError as exc:
            raise _refused(str(exc)) from exc
        self._plans[owner] = plan
        self._run_env[owner] = plan.container_environment()

    def _projection_for(self, obj: LifecycleObject) -> Projection:
        """The launch container's projection: the plan's, taken once; none for a command."""
        inputs = self._inputs_of(obj)
        if _is_command(inputs):
            return NO_PROJECTION
        plan = self._plans.pop(obj.owner, None)
        if plan is None:
            raise _refused(
                f"the launch container {obj.name} has no credential plan to create it with; "
                "one is made at most once per launch, before its objects"
            )
        return plan.projection

    def make(self, obj: LifecycleObject, seed: Seed, launch: LaunchDescription) -> None:
        """Make one journaled object, with every mount and its network (D13)."""
        if obj.kind is ObjectKind.STAGING_ROOT:
            inputs = self._inputs_of(obj)
            assert self._staging is not None
            root = self._staging.staging_root(obj.owner)
            if root is None or root.path != obj.name:
                raise _refused(f"{obj.name} is not the launch's staging root")
            staged = self._staging.make_staging_root(
                root,
                seed,
                inputs.git_input,
                inputs.read_context,
                owner=obj.owner,
                nonce=obj.nonce,
            )
            self._staged[obj.name] = staged
        elif obj.kind is ObjectKind.VOLUME:
            self._volume_create(obj.name, labels_for(obj))
        elif obj.kind in (ObjectKind.STAGER_CONTAINER, ObjectKind.LAUNCH_CONTAINER):
            self._create_container(obj, launch)
        else:
            raise _refused(f"the create step makes no {obj.kind.value}")

    def _source(self, obj: LifecycleObject) -> StagingRoot | None:
        """The staging root ``obj`` binds, re-proved right before its create (D2, D12)."""
        for name in obj.mounts:
            staged = self._staged.get(name)
            if staged is not None:
                assert self._staging is not None
                self._staging.reprove(staged)
                return staged.root
        return None

    def _create_container(self, obj: LifecycleObject, launch: LaunchDescription) -> None:
        volume = next((m for m in obj.mounts if not m.startswith("/")), "")
        method = self.method
        if obj.kind is ObjectKind.STAGER_CONTAINER:
            spec = ContainerSpec(
                ContainerRole.STAGER,
                ContainerNetwork.NONE,
                volume,
                staging=method,
                source=self._source(obj),
            )
        else:
            spec = ContainerSpec(
                ContainerRole.LAUNCH,
                ContainerNetwork(obj.network),
                volume,
                staging=method,
                source=self._source(obj),
                projection=self._projection_for(obj),
            )
        self._container_create(spec, obj.name, labels_for(obj))

    def stage(self, stager: LifecycleObject, seed: Seed) -> None:
        """The stager container: started, the copy-in, the stager, the prover (D2, D13)."""
        inputs = self._inputs_of(stager)
        git_input = inputs.git_input
        if git_input is None:
            raise _refused("the launch carries no git input to stage")
        manifest_bytes = _read_manifest(seed)
        manifest = parse_manifest(manifest_bytes)
        role = "export" if git_input.export_command else "agent"
        form = self.method.value
        self._start(stager.name)
        if self.method is StagingMethod.COPY:
            res = self._exec(
                stager.name,
                [*_helper("stager"), "receive"],
                stdin_path=os.path.join(seed.directory, SEED_PACK_NAME),
                timeout=STAGE_TIMEOUT_SECONDS,
            )
            verdict = _verdict(res, "stager")
            if verdict.get("stager") != "received" or verdict.get("sha256") != manifest.pack_sha256:
                raise _refused(f"the seed's copy-in was not received whole: {_said(verdict, res)}")
        request = {
            "role": role,
            "form": form,
            "object_format": git_input.object_format,
            "manifest": manifest_bytes.decode("utf-8"),
            "manifest_digest": git_input.seed_manifest_digest,
        }
        res = self._exec(
            stager.name,
            [*_helper("stager"), "stage"],
            stdin_data=json.dumps(request).encode("utf-8"),
            timeout=STAGE_TIMEOUT_SECONDS,
        )
        verdict = _verdict(res, "stager")
        if res.exit_code != 0 or verdict.get("stager") != "staged":
            raise _refused(f"the stager failed: {_said(verdict, res)}")
        head = git_input.carried or git_input.seed
        tree = self.tree_of(head, git_input.object_format)
        prove: dict[str, object] = {
            "role": role,
            "form": form,
            "object_format": git_input.object_format,
            "tree": tree,
        }
        if role == "agent":
            prove.update(
                head=head,
                tags=[{"ref": tag.ref, "object": tag.object} for tag in manifest.tags],
                shallow=list(manifest.boundaries),
            )
        res = self._exec(
            stager.name,
            [*_helper("prover"), "prove"],
            stdin_data=json.dumps(prove).encode("utf-8"),
            timeout=STAGE_TIMEOUT_SECONDS,
        )
        verdict = _verdict(res, "prover")
        if res.exit_code != 0 or verdict.get("prover") != "proved" or verdict.get("tree") != tree:
            raise _refused(
                f"the prover did not prove /workspace against tree {tree}: {_said(verdict, res)}"
            )

    def start(self, container: LifecycleObject) -> None:
        """The launch container started; in the copy-in form its read context copied in (D12).

        The bind form's read context is ``/source/read-context``. The copy-in
        form binds nothing, so the controller's read context is streamed, as
        a tar archive on an exec's standard input, into
        :data:`COPIED_READ_CONTEXT` in the agent's private home, before the
        run step. A copy that fails refuses the launch.
        """
        self._start(container.name)
        inputs = self._inputs_of(container)
        if not _is_command(inputs):
            self._detect_versions(container.name)
        if self.method is not StagingMethod.COPY:
            return
        read_context = inputs.read_context
        if not read_context:
            return
        with tempfile.TemporaryDirectory(prefix="autoforge-read-context-") as scratch:
            archive = os.path.join(scratch, "read-context.tar")
            _archive_read_context(read_context, archive)
            res = self._exec(
                container.name,
                ["python3", "-I", "-c", _EXTRACT, COPIED_READ_CONTEXT],
                stdin_path=archive,
                timeout=CALL_TIMEOUT_SECONDS,
            )
        if res.exit_code != 0:
            raise _refused(f"the read context was not copied into the container: {_detail(res)}")

    def _detect_versions(self, container: str) -> None:
        """Each agent CLI the image pinned, its ``--version`` read inside ``container`` (D7).

        As the agent, in the launch container, never from a host
        installation. A CLI that does not run, or that reports a version
        other than the one the image's inputs pinned, refuses the launch:
        the image is not what its key says. Read once per image id.
        """
        image = self._image
        if image is None or not image.clis or image.id in self._versions:
            return
        found: list[tuple[str, str]] = []
        for key, pinned in image.clis:
            command = CLI_COMMANDS[key]
            res = self._exec(
                container,
                ["env", f"HOME={HOME_TARGET}", command, "--version"],
                timeout=CALL_TIMEOUT_SECONDS,
            )
            lines = (res.stdout or "").strip().splitlines()
            line = redact(lines[0].strip())[:120] if lines else ""
            if res.exit_code != 0 or res.timed_out:
                raise _refused(
                    f"the image's {command} --version did not run in the launch container "
                    f"({_detail(res) or f'it ended with {res.exit_code}'}); no launch uses an "
                    "image whose CLI does not run (ADR 0013 D7)"
                )
            if pinned not in _VERSION_TOKEN.findall(line):
                raise _refused(
                    f"the image's {command} reports {line!r} inside the launch container, not "
                    f"the pinned {pinned}; the image is not what its key says (ADR 0013 D7)"
                )
            found.append((command, line))
        self._versions[image.id] = tuple(found)

    def prepare(
        self,
        launch: LaunchDescription,
        handle: LaunchHandle | None,
        command: list[str],
        *,
        cwd: str | None,
        env: Mapping[str, str],
    ) -> ContainerLaunch:
        """The run step: ``command`` as the agent in the launch container ``handle`` names."""
        if handle is None:
            raise _refused(
                "no lifecycle made this launch's container, so it is refused; under the "
                "container backend nothing runs on the host instead (ADR 0013 D15)"
            )
        container = handle.find(ObjectKind.LAUNCH_CONTAINER)
        if container is None:
            raise _refused("the launch's handle names no launch container")
        if container.name in self._tainted:
            raise _refused(
                f"an earlier process of the launch left processes in {container.name} that were "
                "not proved gone, so no later process of the launch starts there"
            )
        if not command:
            raise _refused("the run step has no command")
        if "=" in command[0] or command[0].startswith("-"):
            raise _refused(
                "the command's first word holds '=' or starts with '-', which env would read as "
                "a variable or an option"
            )
        environment = self._run_environment(handle.inputs, env)
        # Of the launch's environment, only the plan's allow-list (an agent)
        # or the command's (a repository command): no credential, store or
        # provider setting is ever forwarded by any other rule. Each passes by
        # name, its value only in the client's environment; one the client
        # reads for itself (HOME, LANG, ...) holds no credential and is set
        # by ``env`` inside the container.
        names: list[str] = []
        inline: list[str] = []
        for name, value in sorted(environment.items()):
            try:
                check_exec_env_name(name)
                own = self._client.reserves(name)
            except SandboxPolicyError:
                own = True
            if own:
                inline.append(f"{name}={value}")
            else:
                names.append(name)
        argv = [
            self._client.executable,
            *exec_argv(
                self.product,
                container.name,
                ["env", *inline, *command],
                workdir=WORKSPACE_TARGET,
                env_names=names,
            ),
        ]
        config_dir = tempfile.mkdtemp(prefix="autoforge-runtime-client-")
        spawn_env = self._client.environment(
            config_dir, {name: environment[name] for name in names}
        )
        return ContainerLaunch(
            self, container.name, argv, spawn_env, config_dir, self._facts(launch, handle)
        )

    def _run_environment(self, inputs: LaunchInputs, env: Mapping[str, str]) -> dict[str, str]:
        """What the run step passes into the container, by the launch's class (D14; #290)."""
        if _is_command(inputs):
            return command_environment(env)
        environment = self._run_env.get(inputs.owner)
        if environment is None:
            raise _refused(
                "the agent launch has no credential plan, so none of its environment is passed "
                "and it is refused"
            )
        return dict(environment)

    def end_agent_processes(self, container: str) -> str:
        """Kill every process of the agent's UID in ``container``; "" once none is left.

        One exec as the agent (:data:`_TEARDOWN`): ``kill(-1, SIGKILL)``,
        which spares PID 1 (the watcher, another UID) and itself, then the
        container's ``/proc`` read until no other process of that UID is
        alive, within a bound. Anything else (the exec refused, timed out or
        stopped, a survivor, no verdict) is what was not proved, and the
        container is tainted: no later process of the launch starts there.
        """
        res = self._exec(
            container,
            ["python3", "-I", "-c", _TEARDOWN, str(TEARDOWN_WAIT_SECONDS)],
            timeout=KILL_TIMEOUT_SECONDS,
        )
        verdict = _verdict(res, "teardown")
        if res.timed_out or res.stopped:
            problem = "the kill's exec did not end in time"
        elif res.exit_code != 0 or verdict.get("teardown") != "proved":
            survivors = verdict.get("survivors")
            problem = (
                f"{survivors} process(es) of the agent's UID survived the kill"
                if isinstance(survivors, int) and survivors
                else f"the kill's exec ended with {res.exit_code}"
                + (f" ({_detail(res)})" if _detail(res) else "")
            )
        else:
            return ""
        self._tainted.add(container)
        return problem

    def exporter(self, handle: LaunchHandle) -> tuple[LifecycleObject, ...]:
        export_dir = handle.inputs.export_dir
        if not export_dir:
            return ()
        owner = handle.inputs.owner
        volume = handle.find(ObjectKind.VOLUME)
        if volume is None:
            raise _refused("the launch has no volume to export from")
        return (
            LifecycleObject(
                owner,
                ObjectKind.EXPORTER,
                object_name(owner, ObjectKind.EXPORTER),
                self._nonce(),
                network=NETWORK_NONE,
                mounts=(volume.name,),
            ),
            LifecycleObject(owner, ObjectKind.EXPORT_DIR, export_dir, self._nonce()),
        )

    def export(self, objects: tuple[LifecycleObject, ...], handle: LaunchHandle) -> Artifact:
        """The exporter's ``head`` and ``pack``, written as the artifact; untrusted (D5)."""
        exporter = next(obj for obj in objects if obj.kind is ObjectKind.EXPORTER)
        directory = next(obj for obj in objects if obj.kind is ObjectKind.EXPORT_DIR).name
        git_input = handle.inputs.git_input
        if git_input is None:
            raise _refused("the launch carries no git input to export against")
        spec = ContainerSpec(ContainerRole.EXPORTER, ContainerNetwork.NONE, exporter.mounts[0])
        self._container_create(spec, exporter.name, labels_for(exporter))
        self._start(exporter.name)
        res = self._exec(
            exporter.name, [*_helper("exporter"), "head"], timeout=CALL_TIMEOUT_SECONDS
        )
        try:
            claim = json.loads(res.stdout)
        except ValueError:
            claim = {}
        candidate = claim.get("candidate") if isinstance(claim, dict) else None
        if res.exit_code != 0 or not isinstance(candidate, str):
            raise _refused(f"the exporter named no candidate: {_detail(res)}")
        _make_export_dir(next(obj for obj in objects if obj.kind is ObjectKind.EXPORT_DIR))
        writer = _PackWriter(os.path.join(directory, ARTIFACT_PACK), MAX_ARTIFACT_PACK_BYTES)
        request = {
            "object_format": git_input.object_format,
            "candidate": candidate,
            "exclude": [oid for oid in (git_input.seed, git_input.carried) if oid],
        }
        try:
            res = self._exec(
                exporter.name,
                [*_helper("exporter"), "pack"],
                stdin_data=json.dumps(request).encode("utf-8"),
                timeout=EXPORT_DEADLINE_SECONDS,
                on_chunk=writer,
                stop=writer.stop,
                max_output_bytes=64 * 1024,
            )
        finally:
            writer.close()
        if writer.failure:
            raise _refused(f"the exported pack was not written: {writer.failure}")
        if res.exit_code != 0 or res.timed_out or res.stopped:
            raise _refused(f"the exporter's pack failed: {_detail(res)}")
        manifest = CandidateManifest.for_input(
            git_input,
            candidate=candidate,
            pack_bytes=writer.size,
            pack_objects=writer.objects,
            pack_sha256=writer.digest.hexdigest(),
        )
        path = os.path.join(directory, ARTIFACT_MANIFEST)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle_out:
            handle_out.write(manifest.render())
        return Artifact(directory)

    def remove(self, obj: LifecycleObject) -> Removal:
        """Remove ``obj``; :attr:`Removal.REMOVED` only when the runtime shows it gone."""
        if obj.kind is ObjectKind.STAGING_ROOT:
            staging = self._staging or self._make_staging(StagingMethod.BIND)
            root = staging.staging_root(obj.owner) if staging.method is StagingMethod.BIND else None
            if root is None or root.path != obj.name:
                return Removal.UNCERTAIN
            outcome = staging.remove_staging_root(root, obj.owner, obj.nonce)
            if outcome is Removal.REMOVED:
                self._staged.pop(obj.name, None)
            return outcome
        if obj.kind is ObjectKind.EXPORT_DIR:
            return _remove_export_dir(obj)
        if obj.kind is ObjectKind.PROJECTION:
            # Nothing outside the container holds an environment credential;
            # its value leaves the runtime's record with the launch container.
            return Removal.REMOVED
        kind = "volume" if obj.kind is ObjectKind.VOLUME else "container"
        outcome = self._remove_named(kind, obj.name, obj.nonce)
        if outcome is Removal.REMOVED and obj.kind is ObjectKind.LAUNCH_CONTAINER:
            self._plans.pop(obj.owner, None)
            self._run_env.pop(obj.owner, None)
            self._tainted.discard(obj.name)
        return outcome

    def policy_inputs(self) -> tuple[tuple[str, str], ...]:
        """P's container inputs (ADR 0006 D8.1 as amended): "" for what is not known yet."""
        identity = self._identity
        method = ""
        if self._method is not None and identity is not None:
            if self._method_report == identity.report():
                method = self._method.value
        return (
            ("backend", BackendKind.CONTAINER.value),
            ("runtime", identity.name if identity else ""),
            ("image", self._image.digest if self._image else ""),
            ("staging", method),
            # The tree is proved inside the container by the prover (D6.3 as amended).
            ("materialization", "container-prover"),
        )

    # -- the runtime's calls ------------------------------------------------------
    def _call(self, args: list[str], **kwargs: object) -> ExecutionResult:
        return self._client.run(args, **kwargs)  # type: ignore[arg-type]

    def _volume_create(self, name: str, labels: Mapping[str, str]) -> None:
        """Create the volume ``name``, then prove the runtime's volume is that new one.

        Docker's ``volume create`` succeeds for a name it already holds and
        returns that volume as it is (its labels unchanged); Podman's
        refuses it. Either way a create is proved only by the volume's own
        labels, read back: exactly ``labels``, the entry's nonce among them.
        Any other volume under the name is a collision, refused and never
        mounted, adopted or removed (its removal checks the nonce too).
        """
        args = ["volume", "create"]
        for key, value in sorted(labels.items()):
            args.append(f"--label={key}={value}")
        res = self._call([*args, name])
        if res.exit_code != 0:
            raise _refused(_create_failure("volume", name, res))
        self._prove_created("volume", name, labels)

    def _prove_created(self, kind: str, name: str, labels: Mapping[str, str]) -> None:
        """The object under ``name`` carries exactly ``labels``, so it is the one just made."""
        found = self._labels(kind, name)
        if found is None:
            raise _refused(
                f"the {kind} {name} was created, and the runtime could not show its labels, "
                "so it is not proved to be the launch's own and is not used"
            )
        if not found or found.get(LABEL_NONCE) != labels.get(LABEL_NONCE):
            raise _refused(
                f"the {kind} name {name} is already in use on the runtime by an object that "
                "is not this entry's (its nonce label differs); an object is never adopted "
                "or removed by its name alone, and reconciliation decides by its nonce"
            )
        if {key: found.get(key) for key in labels} != dict(labels):
            raise _refused(
                f"the {kind} {name} carries this entry's nonce and not all of its labels, so "
                "it is not used"
            )

    def _container_create(self, spec: ContainerSpec, name: str, labels: Mapping[str, str]) -> None:
        argv = create_argv(
            self.product,
            spec,
            self._host,
            name=name,
            labels=labels,
            image=self._image_id(),
            command=[*_helper("watch"), str(self._deadline)],
        )
        projection = spec.projection

        def with_projection(env: dict[str, str]) -> dict[str, str]:
            return client_environment(env, projection)

        res = self._call(argv, extra_env=with_projection if projection else None)
        if res.exit_code != 0:
            raise _refused(_create_failure("container", name, res))
        self._prove_created("container", name, labels)

    def _start(self, name: str) -> None:
        res = self._call(["start", name])
        if res.exit_code != 0:
            raise _refused(f"the container {name} did not start: {_detail(res)}")

    def _exec(
        self,
        container: str,
        command: list[str],
        *,
        timeout: int,
        stdin_data: bytes | None = None,
        stdin_path: str | None = None,
        on_chunk: Callable[[str, bytes], None] | None = None,
        stop: threading.Event | None = None,
        max_output_bytes: int = 1024 * 1024,
    ) -> ExecutionResult:
        argv = exec_argv(self.product, container, command)
        return self._client.run(
            argv,
            timeout_seconds=timeout,
            stdin_data=stdin_data,
            stdin_path=stdin_path,
            on_chunk=on_chunk,
            stop=stop,
            max_output_bytes=max_output_bytes,
        )

    def _labels(self, kind: str, name: str) -> dict[str, str] | None:
        """``name``'s labels, ``{}`` when it is gone, ``None`` when the runtime cannot say."""
        what = "volume" if kind == "volume" else "container"
        res = self._call([what, "inspect", "--format", "{{json .Labels}}", name])
        if res.exit_code != 0:
            return {} if _NOT_FOUND.search(res.stderr or res.stdout or "") else None
        try:
            labels = json.loads(res.stdout or "null")
        except ValueError:
            return None
        return labels if isinstance(labels, dict) else ({} if labels is None else None)

    def _remove_named(self, kind: str, name: str, nonce: str) -> Removal:
        labels = self._labels(kind, name)
        if labels is None:
            return Removal.UNCERTAIN
        if labels == {} and not self._exists(kind, name):
            return Removal.REMOVED
        if labels.get(LABEL_NONCE) != nonce:
            # Another object under this name: never removed by name alone.
            return Removal.UNCERTAIN
        if kind == "volume":
            res = self._call(["volume", "rm", "--force", name])
        else:
            res = self._call(["rm", "--force", name])
        if res.exit_code != 0 and not _NOT_FOUND.search(res.stderr or res.stdout or ""):
            return Removal.UNCERTAIN
        return Removal.REMOVED if not self._exists(kind, name) else Removal.UNCERTAIN

    def _exists(self, kind: str, name: str) -> bool:
        what = "volume" if kind == "volume" else "container"
        res = self._call([what, "inspect", "--format", "{{.Name}}", name])
        if res.exit_code == 0:
            return True
        return not _NOT_FOUND.search(res.stderr or res.stdout or "")

    # -- the shared store ---------------------------------------------------------
    def tree_of(self, commit: str, object_format: str) -> str:
        """``commit``'s tree id from the shared store, the commit re-hashed (ADR 0004 D7.5).

        Read through a git directory of the controller's own whose only
        object store is the shared one (``seed_pack.isolated_store``), so no
        lazy fetch, configuration or graft of the store applies; never from
        the seed's pack or manifest (ADR 0013 D2).
        """
        try:
            with isolated_store(self._common, object_format, self._runner) as (repo, run):
                return materialize_mod._commit_tree(run, str(repo), commit, object_format)
        except (MaterializationError, CandidateNotMaterializableError) as exc:
            raise _refused(
                f"the seed commit's tree cannot be read from the shared store ({exc})"
            ) from exc


# Where the copy-in form puts the read context in the launch container: the
# agent's private home (a tmpfs only the agent's UID can enter).
COPIED_READ_CONTEXT = f"{HOME_TARGET}/read-context"
# The in-container extraction: directories and regular files only, into a
# directory it creates (``filter="data"`` refuses links, devices, absolute
# names and ``..``; an interpreter without it is refused, never trusted).
_EXTRACT = (
    "import os, sys, tarfile\n"
    "os.mkdir(sys.argv[1], 0o700)\n"
    "with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as t:\n"
    "    t.extractall(sys.argv[1], filter='data')\n"
)


def _archive_read_context(source: str, archive: str) -> None:
    """``source``'s directories and regular files as a tar archive; anything else is refused."""
    import tarfile

    with tarfile.open(archive, "w") as tar:
        for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
            dirnames.sort()
            for name in sorted([*dirnames, *filenames]):
                path = os.path.join(dirpath, name)
                info = os.lstat(path)
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise _refused(f"the read context holds {name}, not a file or directory")
                member = tar.gettarinfo(path, arcname=os.path.relpath(path, source))
                member.uid = member.gid = 0
                member.uname = member.gname = ""
                member.mode = 0o755 if member.isdir() else 0o644
                if member.isreg():
                    with open(path, "rb") as handle:
                        tar.addfile(member, handle)
                else:
                    tar.addfile(member)


def _helper_read() -> list[str]:
    return ["python3", "-I", "-c", "import sys; sys.stdout.write(open(sys.argv[1]).read())"]


def _read_manifest(seed: Seed) -> bytes:
    path = os.path.join(seed.directory, SEED_MANIFEST_NAME)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as handle:
        return handle.read(4 * 1024 * 1024 + 1)


def _verdict(res: ExecutionResult, helper: str) -> dict[str, object]:
    for line in reversed((res.stdout or "").strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and helper in data:
            return data
    return {}


def _said(verdict: Mapping[str, object], res: ExecutionResult) -> str:
    problems = verdict.get("problems") or verdict.get("problem")
    if isinstance(problems, list):
        text = "; ".join(str(p) for p in problems[:5])
    elif problems:
        text = str(problems)
    else:
        text = f"it ended with {res.exit_code}" + (f" ({_detail(res)})" if _detail(res) else "")
    return redact(text)[:600]


def _create_failure(kind: str, name: str, res: ExecutionResult) -> str:
    detail = _detail(res)
    text = res.stderr or res.stdout or ""
    if _EXISTS.search(text):
        return (
            f"the {kind} name {name} is already in use on the runtime; an object is never "
            "adopted or removed by its name alone, and reconciliation decides by its nonce"
        )
    if _DENIED.search(text):
        return f"the runtime denied the {kind} {name}'s create: {detail}"
    return f"the {kind} {name} could not be created: {detail}"


def _make_probe_root(root: ProbeRoot) -> None:
    """The probe's staging root: its layout made as the controller's sandbox directories are.

    Every directory from the common dir down to the probe's own through
    ``safefs`` (mode 0700, never through a link), then ``stage`` itself,
    mode 0755 so that the probe container's agent UID reads the nonce, as
    it reads a launch's ``/source`` (``source_staging.STAGED_DIRECTORY_MODE``).
    """
    parent, _, leaf = root.layout.rpartition("/")
    with SafeRoot.open(root.common_dir) as common:
        common.ensure_dir(parent)
    try:
        os.mkdir(root.path, 0o755)
    except FileExistsError:
        if os.path.islink(root.path) or not os.path.isdir(root.path):
            raise _refused(f"the probe's staging root {root.path} is not a directory") from None
    os.chmod(root.path, 0o755, follow_symlinks=False)


# The export directory's owner record: beside the directory, in the
# controller's ``export`` directory, since the artifact directory holds
# exactly its manifest and its pack (``candidate_artifact``).
EXPORT_OWNER_SUFFIX = ".owner"
EXPORT_OWNER_SCHEMA = 1
_MAX_EXPORT_OWNER_BYTES = 4096


def export_owner_path(directory: str) -> str:
    """Where the owner record of the export directory ``directory`` is."""
    return directory + EXPORT_OWNER_SUFFIX


def _export_owner_record(obj: LifecycleObject, info: os.stat_result) -> bytes:
    owner = obj.owner
    return json.dumps(
        {
            "schema": EXPORT_OWNER_SCHEMA,
            "run": owner.run,
            "issue": owner.issue,
            "phase": owner.phase,
            "launch": owner.launch,
            "nonce": obj.nonce,
            "path": obj.name,
            "device": info.st_dev,
            "inode": info.st_ino,
        },
        sort_keys=True,
    ).encode("utf-8")


def _make_export_dir(obj: LifecycleObject) -> None:
    """The launch's export directory, made fresh (mode 0700) where the engine chose it.

    Its parent is the controller's ``export`` directory under the issue's
    sandbox directory, made if missing and refused when it is reached
    through a link; the directory itself is made exclusively, so nothing
    planted at the path is ever written into (the verify step opens it from
    the common dir again, never following a link). Then its owner record
    (:func:`export_owner_path`) is written, exclusively: the journal entry's
    owner and nonce and the directory's device and inode as made. Only a
    directory with that record is ever removed (:func:`_remove_export_dir`):
    one that was already there is refused here and left alone there, and
    one this step made but did not record (an interrupted step) is
    ambiguous and left alone too.
    """
    directory = obj.name
    parent = os.path.dirname(directory)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    if os.path.realpath(parent) != os.path.normpath(parent):
        raise _refused(f"the export directory's parent {parent} is reached through a link")
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        raise _refused(
            f"the export directory {directory} exists already; it is never reused, written "
            "into or removed"
        ) from None
    info = os.lstat(directory)
    try:
        fd = os.open(
            export_owner_path(directory),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except FileExistsError:
        # Another record is there: the directory just made is empty and ours,
        # and is taken back; what the record names is left alone.
        try:
            os.rmdir(directory)
        except OSError:
            pass
        raise _refused(
            f"the export directory {directory} has an owner record already; nothing is "
            "exported into it"
        ) from None
    with os.fdopen(fd, "wb") as handle:
        handle.write(_export_owner_record(obj, info))
        handle.flush()
        os.fsync(handle.fileno())


def _read_export_owner(path: str) -> bytes | None:
    """The owner record at ``path``, bounded; ``None`` when there is none."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError(f"{path} is not a regular file")
        return handle.read(_MAX_EXPORT_OWNER_BYTES + 1)


def _remove_export_dir(obj: LifecycleObject) -> Removal:
    """The export directory removed only when its owner record proves it the entry's.

    :attr:`Removal.REMOVED` once the directory is gone and its record with
    it. The directory is removed only when its owner record is exactly the
    one :func:`_make_export_dir` wrote for this entry (its owner and nonce,
    and the directory's device and inode, which a directory put in its
    place does not have), never through a link; the record goes last, so a
    removal cut short is still recognized as the entry's. A directory with
    no record, another entry's record, a partial record, or another inode
    is not the entry's: it is left in place, uncertain.
    """
    directory = obj.name
    record_path = export_owner_path(directory)
    try:
        info: os.stat_result | None = os.lstat(directory)
    except FileNotFoundError:
        info = None
    try:
        record = _read_export_owner(record_path)
    except OSError:
        return Removal.UNCERTAIN
    if info is None:
        # Nothing at the path. A record of this entry's is the end of a
        # removal cut short, and goes; one of another's is left alone.
        if record is not None and _owns(record, obj, None):
            try:
                os.unlink(record_path)
            except FileNotFoundError:
                pass
            except OSError:
                return Removal.UNCERTAIN
        return Removal.REMOVED
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return Removal.UNCERTAIN
    if record is None or not _owns(record, obj, info):
        return Removal.UNCERTAIN
    shutil.rmtree(directory, ignore_errors=True)
    if os.path.lexists(directory):
        return Removal.UNCERTAIN
    try:
        os.unlink(record_path)
    except FileNotFoundError:
        pass
    except OSError:
        return Removal.UNCERTAIN
    return Removal.REMOVED


def _owns(record: bytes, obj: LifecycleObject, info: os.stat_result | None) -> bool:
    """Whether ``record`` is ``obj``'s owner record (and, given ``info``, of that directory)."""
    if len(record) > _MAX_EXPORT_OWNER_BYTES:
        return False
    try:
        data = json.loads(record)
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    owner = obj.owner
    expected = {
        "schema": EXPORT_OWNER_SCHEMA,
        "run": owner.run,
        "issue": owner.issue,
        "phase": owner.phase,
        "launch": owner.launch,
        "nonce": obj.nonce,
        "path": obj.name,
    }
    if any(data.get(key) != value for key, value in expected.items()):
        return False
    if info is None:
        return True
    return data.get("device") == info.st_dev and data.get("inode") == info.st_ino


class _PackWriter:
    """The exporter's pack, written to a new file as it arrives; stopped past ``limit``."""

    def __init__(self, path: str, limit: int) -> None:
        self._fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self._limit = limit
        self.size = 0
        self.digest = hashlib.sha256()
        self._header = b""
        self.failure = ""
        self.stop = threading.Event()

    @property
    def objects(self) -> int:
        if len(self._header) < 12:
            return 0
        return int(struct.unpack(">I", self._header[8:12])[0])

    def __call__(self, stream: str, chunk: bytes) -> None:
        if stream != "stdout" or self.failure:
            return
        if self.size + len(chunk) > self._limit:
            self.failure = f"the pack is over its bound of {self._limit} bytes"
            self.stop.set()
            return
        if len(self._header) < 12:
            self._header += chunk[: 12 - len(self._header)]
        try:
            view = memoryview(chunk)
            while view:
                view = view[os.write(self._fd, view) :]
        except OSError as exc:
            self.failure = f"it could not be written ({exc.strerror})"
            self.stop.set()
            return
        self.size += len(chunk)
        self.digest.update(chunk)

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1
