"""#271: the container backend's lifecycle on a fake runtime (ADR 0013 D2, D5, D12, D13, D15).

The runtime is the fake CLI of ``tests/container_fake.py``; the seed, the
staging root and the shared store are real (``tests/test_seed_pack.py``'s
review repository, ``SourceStaging``), and every launch goes through the
engine's real sequence (:class:`~autoforge.sandbox_lifecycle.LaunchLifecycle`)
and the real executor, with a journal that records what it is told. What
these show, for both staging forms:

- every lifecycle step in D13's order: the volume and both containers
  created with their mounts and networks, the stager's and the prover's
  execs, the launch container started only after the stager container's
  verified removal, the command run as the agent with its output streamed,
  a nonzero end, a timeout and a stop each the command's own, and the
  release, the export and the final clean-up leaving nothing;
- names and labels scoped to the run, the issue and the launch; a name
  collision and a denied create each refused, with what was made removed;
  an object of the same name with another nonce never removed;
- a prover that does not prove refuses the launch, and the launch
  container never starts;
- the export: the agent's candidate and its pack written as the
  artifact, which the strict import accepts with the same id;
- the shared-filesystem probe both ways, ``transfer: copy``, and no probe
  without a journal bound to the proved runtime;
- under this backend nothing runs on the host: a launch with no container
  is refused, and the run step's argv is the runtime's ``exec``;
- the run step's environment is the credential plan's allow-list (an
  agent) or the command's (a repository command), never the launch's
  environment filtered: an unrelated credential, a database URL and an
  inherited provider setting never reach a call, an argv or the command;
- ownership is proved before anything is used or removed: an export
  directory that was there already, or that this step made and did not
  record, is never removed; a volume Docker hands back for a name it held
  is refused; a probe object whose create lost its answer is removed by its
  nonce before its entry closes, and stays open when that is not proved;
- a staging form proved on one store is not reused once the store moved;
- a close whose kill is refused, times out or leaves a survivor is
  uncertain, and no later process of the launch starts in that container.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from autoforge.candidate_artifact import ARTIFACT_MANIFEST, ARTIFACT_PACK, import_artifact
from autoforge.container_auth import CredentialKind, CredentialRoutes, EnvRoute, plan_credentials
from autoforge.container_backend import (
    LABEL_NONCE,
    NO_PROBE_JOURNAL,
    ContainerBackend,
    ProbeObject,
    export_owner_path,
    object_name,
    run_hash,
)
from autoforge.container_image import ImageRef
from autoforge.container_runtime import RuntimeClient, detect
from autoforge.errors import (
    SandboxCleanupUncertainError,
    SandboxLaunchRefusedError,
    SandboxPolicyError,
)
from autoforge.executor import ExecutionRequest, execute
from autoforge.git_input import LaunchGitInput
from autoforge.git_transport import GitRemote, GitTransport
from autoforge.sandbox import LaunchDescription, NetworkPosition
from autoforge.sandbox_backend import (
    LaunchInputs,
    LaunchOwner,
    LifecycleObject,
    ObjectKind,
    Removal,
    RuntimeReport,
    bind,
)
from autoforge.sandbox_container import (
    AGENT_UID,
    HostContext,
    ProbeRoot,
    StagingMethod,
)
from autoforge.sandbox_lifecycle import LaunchLifecycle
from autoforge.sandbox_policy import LaunchClass, ProviderRequirements
from autoforge.seed_pack import SeedSettings

from . import container_fake as fake
from .test_seed_pack import out, review_repo, run_git
from .test_source_staging import KEY, RecordingLock, read_context, staging_for

IMAGE = ImageRef("a" * 64, "sha256:" + "b" * 64)
PLANNED = LaunchDescription(mounts=())

# A provider's declaration (#290) for the fake world's agent launches: one
# key, one setting, one isolation variable.
ROUTES = CredentialRoutes(
    scope="test",
    routes=(EnvRoute("AF_TEST_KEY", CredentialKind.API_KEY),),
    settings=("AF_TEST_SETTING",),
    isolation=(("AF_TEST_ISOLATION", "on"),),
)
PROJECTED = "sk-test-projected-value-0123456789"
# The launch's environment, as the engine's allow-list leaves it: the
# provider's key and setting, and what no rule may forward.
LAUNCH_ENV = {
    "AF_TEST_KEY": PROJECTED,
    "AF_TEST_SETTING": "visible-inside",
    "AF_TEST_ISOLATION": "off",
    "AWS_SECRET_ACCESS_KEY": "aws-secret-sentinel",
    "DATABASE_URL": "postgres://user:db-password-sentinel@db/x",
    "OPENCODE_CONFIG": "/repo/opencode.json",
    "LC_TIME": "C",
    "GIT_AUTHOR_NAME": "Agent",
}
LEAKED = ("aws-secret-sentinel", "db-password-sentinel", "/repo/opencode.json")


def planner(env: dict[str, str] | None = None):
    """``credentials`` for the backend: #290's real plan over ``env``."""

    def plan(launch: LaunchDescription):
        return plan_credentials(
            "test",
            ROUTES,
            ProviderRequirements(),
            LAUNCH_ENV if env is None else env,
            launch_class=LaunchClass.REMOTE_AGENT,
        )

    return plan


@pytest.fixture(autouse=True)
def temporary(tmp_path, monkeypatch) -> Path:
    import tempfile

    where = tmp_path / "temporary"
    where.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(where))
    return where


class Journal:
    """The lifecycle journal (#272's) as the test sees it: every entry and when it moved."""

    def __init__(self) -> None:
        self.open: list[LifecycleObject] = []
        self.events: list[tuple[str, tuple[str, ...]]] = []

    def open_entries(self) -> tuple[LifecycleObject, ...]:
        return tuple(self.open)

    def record_creates(self, runtime: RuntimeReport, objects: tuple[LifecycleObject, ...]) -> None:
        self.events.append(("creates", tuple(o.name for o in objects)))
        self.open.extend(objects)

    def record_removals(self, objects: tuple[LifecycleObject, ...]) -> None:
        self.events.append(("removals", tuple(o.name for o in objects)))

    def close_entries(self, objects: tuple[LifecycleObject, ...]) -> None:
        self.events.append(("closed", tuple(o.name for o in objects)))
        self.open = [o for o in self.open if o not in objects]


@dataclass
class ProbeJournal:
    """#272's probe journal as the probe needs it: a root, a bound runtime, what it recorded."""

    root: ProbeRoot
    runtime: RuntimeReport
    recorded: list[tuple[ProbeObject, ...]] = field(default_factory=list)
    closed: list[tuple[ProbeObject, ...]] = field(default_factory=list)

    def record(self, objects: tuple[ProbeObject, ...]) -> None:
        self.recorded.append(objects)

    def close(self, objects: tuple[ProbeObject, ...]) -> None:
        self.closed.append(objects)


@dataclass
class World:
    tmp: Path
    bin: Path
    review: object
    common: str
    backend: ContainerBackend
    runtime: RuntimeReport
    host: HostContext

    def calls(self) -> list[list[str]]:
        return [call["argv"] for call in fake.calls(self.bin)]

    def state(self) -> dict:
        return fake.state(self.bin)


def make_world(
    tmp_path: Path,
    *,
    transfer: str = "copy",
    runtime: str = "podman",
    credentials=None,
    **state,
) -> World:
    review = review_repo(tmp_path)
    common = out(review.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    bin_dir = tmp_path / "bin"
    reports = (
        {"version": fake.PODMAN_VERSION, "info": fake.podman_info()}
        if runtime == "podman"
        else {"version": fake.DOCKER_VERSION, "info": fake.docker_info()}
    )
    fake.install(bin_dir, runtime, {**reports, "images": {IMAGE.id: {}}, **state})
    env = {"PATH": os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]), "HOME": str(tmp_path)}
    client = RuntimeClient(detect(runtime, env), env)
    host = HostContext(
        home=str(tmp_path / "home"),
        environ={},
        uid=os.getuid(),
        git_common_dir=os.path.realpath(common),
        checkouts=(str(review.repo),),
    )
    lock = RecordingLock()
    backend = ContainerBackend(
        client,
        host=host,
        common_dir=common,
        sandbox_key=KEY,
        seed_settings=SeedSettings(),
        image=IMAGE,
        transfer=transfer,
        credentials=planner() if credentials is None else credentials,
        staging=lambda method: staging_for(review.repo, method, lock=lock),
    )
    report = backend.runtime_check()
    return World(tmp_path, bin_dir, review, common, backend, report, host)


def launch_input(
    world: World, launch: str = "L-1", phase: str = "ANALYZE_EXECUTE", *, command: bool = False
) -> LaunchGitInput:
    review = world.review
    return LaunchGitInput(
        phase=phase,
        run="run-1",
        issue=292,
        launch=launch,
        repository="owner/repo",
        object_format="sha1",
        seed=review.head,
        lower="" if command else review.head,
        carried="",
        export_command=command,
        entry_label="entry-1",
    )


def _read_context(tmp: Path) -> Path:
    where = tmp / "state" / "read-context" / "1"
    return where if where.exists() else read_context(tmp)


def inputs_for(world: World, gi: LaunchGitInput, *, export: bool = True) -> LaunchInputs:
    owner = LaunchOwner(gi.run, gi.issue, gi.phase, gi.launch)
    return LaunchInputs(
        owner,
        workspace="/nowhere",
        read_context=str(_read_context(world.tmp)),
        git_input=gi,
        export_dir=str(world.tmp / "export" / gi.launch) if export else "",
    )


def script_helpers(world: World, gi: LaunchGitInput, **answers) -> str:
    tree = out(world.review.repo, "rev-parse", f"{gi.seed}^{{tree}}")
    exec_answers = {
        "stage": [0, json.dumps({"stager": "staged", "role": "agent", "head": gi.seed, "tags": 0})],
        "prove": [0, json.dumps({"prover": "proved", "tree": tree})],
    }
    exec_answers.update(answers)
    fake.update(world.bin, exec=exec_answers)
    return tree


def run_launch(world: World, inputs: LaunchInputs, journal: Journal, command: list[str], **req):
    """One agent launch through the engine's sequence; its result and the lifecycle."""
    lifecycle = LaunchLifecycle(
        world.backend, journal, inputs, world.runtime, change_producing=True
    )
    with lifecycle:

        def run():
            handle = lifecycle.establish(PLANNED)
            return execute(
                ExecutionRequest(
                    command=command,
                    env_allowlist=(),
                    env=LAUNCH_ENV,
                    sandbox=bind(PLANNED, world.backend, handle),
                    timeout_seconds=req.pop("timeout_seconds", 60),
                    **req,
                )
            )

        result = lifecycle.launch(run)
        exported = lifecycle.export()
        lifecycle.saved()
    return result, exported, lifecycle


# -- one launch, start to end ----------------------------------------------------------


@pytest.mark.parametrize("transfer", ["copy", "auto"])
def test_an_agent_launch_runs_every_step_in_order_and_leaves_nothing(tmp_path, transfer):
    world = make_world(tmp_path, transfer=transfer)
    if transfer == "auto":
        root = ProbeRoot(world.host.git_common_dir, "probe-1")
        world.backend._probe_journal = ProbeJournal(root, world.runtime)  # noqa: SLF001
    method = world.backend.staging_method()
    assert method is (StagingMethod.COPY if transfer == "copy" else StagingMethod.BIND)
    gi = launch_input(world)
    script_helpers(world, gi, head=[0, json.dumps({"exporter": "head", "candidate": gi.seed})])
    pack = world.tmp / "candidate.pack"
    pack.write_bytes(
        subprocess.run(
            ["git", "-C", str(world.review.repo), "pack-objects", "--stdout", "--revs"],
            input=f"{gi.seed}\n^{gi.seed}\n".encode(),
            capture_output=True,
            check=True,
        ).stdout
    )
    fake.update(world.bin, pack_file=str(pack))
    journal = Journal()
    inputs = inputs_for(world, gi)
    result, exported, lifecycle = run_launch(
        world, inputs, journal, ["sh", "-c", 'echo "out:$AF_TEST_SETTING"; echo err >&2; exit 3']
    )
    # The command ran as the agent, its output streamed back, its end its own.
    assert (
        result.exit_code == 3 and "out:visible-inside" in result.stdout and "err" in result.stderr
    )
    assert result.sandbox is not None and result.sandbox.backend == "container"
    assert result.sandbox.runtime == "Podman" and result.sandbox.staging == method.value
    owner = inputs.owner
    stager = object_name(owner, ObjectKind.STAGER_CONTAINER)
    launch = object_name(owner, ObjectKind.LAUNCH_CONTAINER)
    volume = object_name(owner, ObjectKind.VOLUME)
    calls = world.calls()
    # D13's order: the volume, the stager container, the launch container;
    # the stager started, staged, proved and removed; then the launch started.
    create_at = [
        i for i, c in enumerate(calls) if c[0] == "create" and "--name=af-probe-" not in " ".join(c)
    ]
    names = [next(a for a in calls[i] if a.startswith("--name=")) for i in create_at]
    assert names[:2] == [f"--name={stager}", f"--name={launch}"]
    launch_volume = calls.index(
        next(c for c in calls if c[:2] == ["volume", "create"] and c[-1] == volume)
    )
    assert launch_volume < create_at[0]
    start_stager = calls.index(["start", stager])
    removed_stager = calls.index(["rm", "--force", stager])
    start_launch = calls.index(["start", launch])
    assert start_stager < removed_stager < start_launch
    execs = [c for c in calls if c[0] == "exec"]
    helpers = [next((w for w in c if w.endswith(".py")), "") for c in execs]
    assert any("container_stager.py" in h for h in helpers)
    assert any("container_prover.py" in h for h in helpers)
    if method is StagingMethod.COPY:
        assert any(c[-1] == "receive" for c in execs)
        # The read context is copied into the launch container after its start.
        copied = [c for c in execs if c[-1] == "/home/agent/read-context"]
        assert copied and launch in copied[0] and calls.index(copied[0]) > start_launch
        assert world.state()["read_contexts"] == [
            {"container": launch, "names": ["README.md", "files", "files/issue.md"]}
        ]
    else:
        assert not any(c[-1] == "receive" for c in execs)
        assert not any(c[-1] == "/home/agent/read-context" for c in execs)
    # Every exec runs as the agent's UID; the stager and the prover in the
    # stager container, offline.
    assert all(f"--user={AGENT_UID}:{AGENT_UID}" in c for c in execs)
    # The export, then the final clean-up: nothing of the launch is left.
    assert exported is not None and exported.artifact is not None and not exported.failure
    state = world.state()
    assert state["containers"] == {} and state["volumes"] == {}
    assert journal.open == []
    # Every create was journaled before it was made.
    first_create = next(e for e in journal.events if e[0] == "creates")
    assert volume in first_create[1] and stager in first_create[1] and launch in first_create[1]


def test_names_and_labels_are_scoped_to_the_run_the_issue_and_the_launch(tmp_path):
    world = make_world(tmp_path)
    world.backend.staging_method()
    gi = launch_input(world)
    owner = LaunchOwner(gi.run, gi.issue, gi.phase, gi.launch)
    seedless = world.backend.objects(inputs_for(world, gi), None, PLANNED)  # type: ignore[arg-type]
    names = {obj.name for obj in seedless}
    for other in (
        LaunchOwner("run-2", gi.issue, gi.phase, gi.launch),
        LaunchOwner(gi.run, 293, gi.phase, gi.launch),
        LaunchOwner(gi.run, gi.issue, gi.phase, "L-2"),
    ):
        for kind in (ObjectKind.VOLUME, ObjectKind.STAGER_CONTAINER, ObjectKind.LAUNCH_CONTAINER):
            assert object_name(other, kind) not in names
    by_kind = {obj.kind: obj for obj in seedless}
    assert by_kind[ObjectKind.STAGER_CONTAINER].network == "none"
    assert by_kind[ObjectKind.LAUNCH_CONTAINER].network == "bridge"
    isolated = LaunchDescription(mounts=(), network=NetworkPosition.ISOLATED)
    objs = world.backend.objects(inputs_for(world, gi), None, isolated)  # type: ignore[arg-type]
    assert {o.kind: o for o in objs}[ObjectKind.LAUNCH_CONTAINER].network == "none"
    assert len({obj.nonce for obj in seedless}) == len(seedless)
    assert all(name.startswith(f"af-{gi.issue}-{run_hash(owner.run)}-") for name in names)


@pytest.mark.parametrize(
    "fail, match",
    [
        ({"create": [125, 'Error: the container name "x" is already in use']}, "already in use"),
        ({"create": [125, "Error: permission denied"]}, "denied"),
        ({"volume create": [125, "Error: volume with name x already exists"]}, "already in use"),
    ],
)
def test_a_create_that_collides_or_is_denied_is_refused_and_what_was_made_removed(
    tmp_path, fail, match
):
    world = make_world(tmp_path, fail=fail)
    world.backend.staging_method()
    gi = launch_input(world)
    script_helpers(world, gi)
    journal = Journal()
    with pytest.raises(SandboxLaunchRefusedError, match=match):
        run_launch(world, inputs_for(world, gi), journal, ["true"])
    state = world.state()
    assert state["containers"] == {} and state["volumes"] == {}
    assert journal.open == []
    assert not any(c[0] == "start" for c in world.calls())


def test_a_name_held_by_another_launchs_object_is_never_removed(tmp_path):
    world = make_world(tmp_path)
    world.backend.staging_method()
    gi = launch_input(world)
    owner = LaunchOwner(gi.run, gi.issue, gi.phase, gi.launch)
    name = object_name(owner, ObjectKind.VOLUME)
    fake.update(world.bin, volumes={name: {"labels": {LABEL_NONCE: "someone-else"}}})
    entry = LifecycleObject(owner, ObjectKind.VOLUME, name, "mine")
    assert world.backend.remove(entry) is Removal.UNCERTAIN
    assert name in world.state()["volumes"]
    gone = LifecycleObject(owner, ObjectKind.VOLUME, "af-gone", "n")
    assert world.backend.remove(gone) is Removal.REMOVED


def test_a_prover_that_does_not_prove_refuses_and_the_launch_never_starts(tmp_path):
    world = make_world(tmp_path)
    world.backend.staging_method()
    gi = launch_input(world)
    script_helpers(
        world,
        gi,
        prove=[1, json.dumps({"prover": "failed", "problems": ["an extra ref: refs/heads/x"]})],
    )
    journal = Journal()
    with pytest.raises(SandboxLaunchRefusedError, match="an extra ref: refs/heads/x"):
        run_launch(world, inputs_for(world, gi), journal, ["true"])
    launch = object_name(inputs_for(world, gi).owner, ObjectKind.LAUNCH_CONTAINER)
    assert ["start", launch] not in world.calls()
    assert world.state()["containers"] == {} and world.state()["volumes"] == {}


def test_a_prover_naming_another_tree_does_not_prove(tmp_path):
    world = make_world(tmp_path)
    world.backend.staging_method()
    gi = launch_input(world)
    script_helpers(world, gi, prove=[0, json.dumps({"prover": "proved", "tree": "0" * 40})])
    with pytest.raises(SandboxLaunchRefusedError, match="did not prove"):
        run_launch(world, inputs_for(world, gi), Journal(), ["true"])


def test_the_prover_gets_the_engines_tree_and_ref_set(tmp_path):
    world = make_world(tmp_path)
    world.backend.staging_method()
    gi = launch_input(world)
    tree = script_helpers(world, gi, head=[1, ""])
    run_launch(world, inputs_for(world, gi), Journal(), ["true"])
    prove = next(e for e in world.state()["execs"] if "prove" in e["command"])
    assert "container_prover.py" in " ".join(prove["command"])
    assert tree == out(world.review.repo, "rev-parse", f"{gi.seed}^{{tree}}")


def test_a_timeout_and_a_stop_end_the_command_and_close_kills_the_agents_processes(tmp_path):
    world = make_world(tmp_path)
    world.backend.staging_method()
    gi = launch_input(world)
    script_helpers(world, gi, head=[1, ""])
    result, _, _ = run_launch(
        world, inputs_for(world, gi), Journal(), ["sleep", "30"], timeout_seconds=1
    )
    assert result.timed_out
    kills = [e for e in world.state()["execs"] if "os.kill(-1" in " ".join(e["command"])]
    assert kills, "close() killed every process of the agent's UID"
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    world2 = make_world(tmp_path / "again")
    world2.backend.staging_method()
    gi2 = launch_input(world2)
    script_helpers(world2, gi2, head=[1, ""])
    result, _, _ = run_launch(
        world2, inputs_for(world2, gi2), Journal(), ["sleep", "30"], stop=stop
    )
    assert result.stopped


# -- the export (D5) -------------------------------------------------------------------


def test_the_exported_artifact_is_imported_strictly_with_the_same_candidate(tmp_path):
    world = make_world(tmp_path)
    world.backend.staging_method()
    review = world.review
    gi = LaunchGitInput(
        phase="FIX", run="run-1", issue=292, launch="L-1", repository="owner/repo",
        object_format="sha1", seed=review.head, lower=review.merge_base, carried="",
        export_command=False, entry_label="entry-1",
    )  # fmt: skip
    # The agent's work: a commit above the seed, which the fake's exporter packs.
    run_git(review.repo, "checkout", "-q", "--detach", review.head)
    (review.repo / "agent.txt").write_text("work\n")
    run_git(review.repo, "add", "agent.txt")
    run_git(review.repo, "commit", "-q", "-m", "agent")
    candidate = out(review.repo, "rev-parse", "HEAD")
    pack = tmp_path / "candidate.pack"
    pack.write_bytes(
        subprocess.run(
            ["git", "-C", str(review.repo), "pack-objects", "--stdout", "--revs"],
            input=f"{candidate}\n^{review.head}\n".encode(), capture_output=True, check=True,
        ).stdout
    )  # fmt: skip
    fake.update(world.bin, pack_file=str(pack))
    script_helpers(world, gi, head=[0, json.dumps({"exporter": "head", "candidate": candidate})])
    inputs = inputs_for(world, gi)
    journal = Journal()
    lifecycle = LaunchLifecycle(
        world.backend, journal, inputs, world.runtime, change_producing=True
    )
    with lifecycle:
        lifecycle.launch(lambda: lifecycle.establish(PLANNED))
        exported = lifecycle.export()
        assert exported is not None and exported.artifact is not None, exported
        directory = Path(exported.artifact.directory)
        assert sorted(p.name for p in directory.iterdir()) == sorted(
            [ARTIFACT_MANIFEST, ARTIFACT_PACK]
        )
        manifest = json.loads((directory / ARTIFACT_MANIFEST).read_text())
        assert manifest["candidate"] == candidate and manifest["seed"] == review.head
        reader = GitTransport(
            object_directory=Path(world.common) / "objects",
            remote=GitRemote("file:///nonexistent"),
            in_base_history=lambda base, sha: False,
            git_lock=lambda operation: contextlib.nullcontext(),
        )
        imported = import_artifact(
            Path(world.common), directory, lifecycle.git_input, execute,
            read_commit=reader.read_local_commit,
            git_lock=lambda operation: contextlib.nullcontext(),
        )  # fmt: skip
        assert imported.candidate == candidate
        lifecycle.saved()
    exporter = object_name(inputs.owner, ObjectKind.EXPORTER)
    created = [c for c in world.calls() if c[0] == "create" and f"--name={exporter}" in c]
    assert created and "--network=none" in created[0]
    assert any("readonly=true" in a and "target=/workspace" in a for a in created[0])
    assert not any("target=/source" in a for a in created[0]), "the exporter binds nothing"
    assert not directory.exists(), "the final clean-up removed the export directory"


# -- reconciliation and removal --------------------------------------------------------


def test_reconciliation_only_removes_and_closes_what_it_proved_gone(tmp_path):
    world = make_world(tmp_path)
    owner = LaunchOwner("run-1", 292, "FIX", "L-9")
    mine = LifecycleObject(
        owner,
        ObjectKind.LAUNCH_CONTAINER,
        object_name(owner, ObjectKind.LAUNCH_CONTAINER),
        "n1",
        network="bridge",
    )
    theirs = LifecycleObject(owner, ObjectKind.VOLUME, object_name(owner, ObjectKind.VOLUME), "n2")
    fake.update(
        world.bin,
        containers={mine.name: {"labels": {LABEL_NONCE: "n1"}, "mounts": {}, "running": True}},
        volumes={theirs.name: {"labels": {LABEL_NONCE: "not-n2"}}},
    )
    before = len(world.calls())
    gone = world.backend.reconcile((mine, theirs), world.runtime)
    assert gone == (mine,)
    verbs = {c[0] for c in world.calls()[before:]}
    assert verbs <= {"container", "volume", "rm"}, "remove-only: no create, no start"
    assert theirs.name in world.state()["volumes"]
    with pytest.raises(SandboxLaunchRefusedError, match="the runtime the check just proved"):
        world.backend.reconcile((mine,), RuntimeReport(world.runtime.backend, store="another"))


# -- the shared-filesystem probe (D12) --------------------------------------------------


@pytest.mark.parametrize(
    "shares, expected", [(True, StagingMethod.BIND), (False, StagingMethod.COPY)]
)
def test_the_probe_proves_a_bind_only_by_reading_its_nonce_back(tmp_path, shares, expected):
    world = make_world(tmp_path, transfer="auto", shares_filesystem=shares)
    journal = ProbeJournal(ProbeRoot(world.host.git_common_dir, "probe-1"), world.runtime)
    world.backend._probe_journal = journal  # noqa: SLF001
    assert world.backend.staging_method() is expected
    # The journal named both objects before either was created, and closed them once gone.
    assert [o.kind for o in journal.recorded[0]] == ["volume", "container"]
    first_create = next(i for i, c in enumerate(world.calls()) if c[:2] == ["volume", "create"])
    assert first_create > 0
    assert {o.name for batch in journal.closed for o in batch} == {
        o.name for o in journal.recorded[0]
    }
    assert world.state()["containers"] == {} and world.state()["volumes"] == {}
    probe = next(c for c in world.calls() if c[0] == "create")
    assert "--network=none" in probe
    assert any("target=/source,readonly=true" in a for a in probe)
    assert not os.path.exists(os.path.join(journal.root.path, "nonce"))


def test_transfer_copy_forces_copy_in_with_no_probe_and_no_bind_is_ever_forced(tmp_path):
    world = make_world(tmp_path, transfer="copy", shares_filesystem=True)
    assert world.backend.staging_method() is StagingMethod.COPY
    assert not any(c[0] in ("create", "volume") for c in world.calls())
    with pytest.raises(SandboxLaunchRefusedError, match="not auto or copy"):
        ContainerBackend(
            world.backend._client,  # noqa: SLF001
            host=world.host, common_dir=world.common, sandbox_key=KEY,
            seed_settings=SeedSettings(), image=IMAGE, transfer="bind",
        )  # fmt: skip


def test_no_probe_object_is_created_without_a_journal_bound_to_the_proved_runtime(tmp_path):
    world = make_world(tmp_path, transfer="auto")
    world.backend._probe_journal = NO_PROBE_JOURNAL  # noqa: SLF001
    result = world.backend.probe(home=str(tmp_path), environ={})
    assert not result.supported and "#272" in result.reason
    world = make_world(tmp_path / "other", transfer="auto")
    stale = RuntimeReport(world.runtime.backend, product="Podman", store="podman:elsewhere")
    world.backend._probe_journal = ProbeJournal(  # noqa: SLF001
        ProbeRoot(world.host.git_common_dir, "probe-1"), stale
    )
    result = world.backend.probe(home=str(tmp_path), environ={})
    assert not result.supported and "another runtime" in result.reason
    assert not any(c[0] in ("create", "volume") for c in world.calls())


def test_the_probe_refuses_an_image_the_runtime_does_not_hold(tmp_path):
    world = make_world(tmp_path, images={})
    result = world.backend.probe(home=str(tmp_path), environ={})
    assert not result.supported and IMAGE.id in result.reason


def test_the_probe_refuses_an_image_whose_entrypoint_would_replace_the_watcher(tmp_path):
    world = make_world(tmp_path, entrypoints={IMAGE.id: ["/docker-entrypoint.sh"]})
    result = world.backend.probe(home=str(tmp_path), environ={})
    assert not result.supported and "watcher's place" in result.reason
    assert not any(c[0] in ("create", "volume") for c in world.calls())


# -- the run step: nothing on the host (D15) ---------------------------------------------


def test_a_launch_with_no_container_is_refused_and_never_run_on_the_host(tmp_path):
    world = make_world(tmp_path)
    with pytest.raises(SandboxLaunchRefusedError, match="nothing runs on the host"):
        world.backend.prepare(PLANNED, None, ["make", "test"], cwd=None, env={})
    with pytest.raises(SandboxLaunchRefusedError):
        execute(
            ExecutionRequest(
                command=["touch", str(tmp_path / "ran")], sandbox=bind(PLANNED, world.backend)
            )
        )
    assert not (tmp_path / "ran").exists()


def _prepared(world: World, inputs: LaunchInputs, env: dict[str, str]):
    """The run step's argv and client environment for one process of ``inputs``'s launch."""
    lifecycle = LaunchLifecycle(
        world.backend, Journal(), inputs, world.runtime, change_producing=False
    )
    with lifecycle:

        def run():
            handle = lifecycle.establish(PLANNED)
            prepared = world.backend.prepare(PLANNED, handle, ["claude", "-p"], cwd=None, env=env)
            try:
                return prepared.argv, prepared.spawn_env
            finally:
                prepared.close()

        return lifecycle.launch(run)


def test_the_run_steps_argv_is_the_runtimes_exec_with_values_never_on_it(tmp_path):
    env = {**LAUNCH_ENV, "PATH": "/x", "HOME": "/h", "LANG": "C.UTF-8", "TMPDIR": "/t",
           "DOCKER_HOST": "tcp://x"}  # fmt: skip
    world = make_world(tmp_path, credentials=planner(env))
    world.backend.staging_method()
    gi = launch_input(world)
    script_helpers(world, gi)
    inputs = inputs_for(world, gi, export=False)
    argv, spawn_env = _prepared(world, inputs, env)
    assert argv[0] == str(world.bin / "podman") and argv[1] == "exec"
    # The projected key went with the launch container's create, by name,
    # its value in that create's client environment alone; the exec does
    # not carry it.
    launch = object_name(inputs.owner, ObjectKind.LAUNCH_CONTAINER)
    create = next(
        c for c in fake.calls(world.bin) if c["argv"][:2] == ["create", f"--name={launch}"]
    )
    assert "--env=AF_TEST_KEY" in create["argv"] and "AF_TEST_KEY" in create["env"]
    assert "--env=AF_TEST_KEY" not in argv and "AF_TEST_KEY" not in spawn_env
    assert not any(PROJECTED in a for call in world.calls() for a in call)
    # The plan's allow-list passes by name, valued in the client's environment.
    assert "--env=AF_TEST_SETTING" in argv and spawn_env["AF_TEST_SETTING"] == "visible-inside"
    assert "--env=LANG" in argv and spawn_env["LANG"] == "C.UTF-8"
    assert "--env=GIT_AUTHOR_NAME" in argv and "--env=LC_TIME" in argv
    # The isolation is the plan's own, whatever the launch's environment held.
    assert spawn_env["AF_TEST_ISOLATION"] == "on"
    # The client's own names and the runtime's never pass; HOME is the
    # private home, set inside by env, never the host's.
    for reserved in ("PATH", "HOME", "DOCKER_HOST", "TMPDIR"):
        assert f"--env={reserved}" not in argv
    assert argv[argv.index("env") + 1] == "HOME=/home/agent" and argv[-2:] == ["claude", "-p"]
    # Nothing else of the launch's environment: by name or by value.
    for name in ("AWS_SECRET_ACCESS_KEY", "DATABASE_URL", "OPENCODE_CONFIG"):
        assert f"--env={name}" not in argv and name not in spawn_env
    assert not any(value in a for a in argv for value in LEAKED)


def test_an_unrelated_credential_or_setting_never_reaches_a_call_or_the_command(tmp_path):
    """End to end, through the lifecycle and the executor: what the command sees inside."""
    world = make_world(tmp_path)
    world.backend.staging_method()
    gi = launch_input(world)
    script_helpers(world, gi, head=[1, ""])
    result, _, _ = run_launch(world, inputs_for(world, gi), Journal(), ["env"])
    inside = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert inside["AF_TEST_SETTING"] == "visible-inside" and inside["AF_TEST_ISOLATION"] == "on"
    assert inside["AF_TEST_KEY"] == PROJECTED and inside["HOME"] == "/home/agent"
    for name in ("AWS_SECRET_ACCESS_KEY", "DATABASE_URL", "OPENCODE_CONFIG"):
        assert name not in inside
    for call in fake.calls(world.bin):
        assert not any(value in a for a in call["argv"] for value in (*LEAKED, PROJECTED))
        assert not {"AWS_SECRET_ACCESS_KEY", "DATABASE_URL", "OPENCODE_CONFIG"} & set(call["env"])
        # The key is in the client's environment of the launch container's create alone.
        if "AF_TEST_KEY" in call["env"]:
            assert call["argv"][0] == "create" and "--env=AF_TEST_KEY" in call["argv"]


def test_a_repository_command_gets_no_credential_and_no_provider_setting(tmp_path):
    def never(launch):
        raise AssertionError("a repository command asks for no credential plan")

    world = make_world(tmp_path, credentials=never)
    world.backend.staging_method()
    gi = launch_input(world, command=True)
    script_helpers(world, gi)
    inputs = inputs_for(world, gi, export=False)
    argv, spawn_env = _prepared(world, inputs, {**LAUNCH_ENV, "LANG": "C.UTF-8"})
    launch = object_name(inputs.owner, ObjectKind.LAUNCH_CONTAINER)
    create = next(c for c in world.calls() if c[:2] == ["create", f"--name={launch}"])
    assert not any(a.startswith("--env=") for a in create), "no projection for a command"
    names = sorted(a.removeprefix("--env=") for a in argv if a.startswith("--env="))
    assert names == ["LANG", "LC_TIME"]
    for name in ("AF_TEST_KEY", "AF_TEST_SETTING", "AF_TEST_ISOLATION", "GIT_AUTHOR_NAME"):
        assert name not in spawn_env


def test_an_agent_launch_without_a_credential_plan_makes_nothing(tmp_path):
    world = make_world(tmp_path)
    world.backend._credentials = None  # noqa: SLF001
    world.backend.staging_method()
    gi = launch_input(world)
    script_helpers(world, gi)
    with pytest.raises(SandboxLaunchRefusedError, match="credential plan"):
        run_launch(world, inputs_for(world, gi), Journal(), ["true"])
    assert not any(c[0] in ("create", "volume") for c in world.calls())
    world = make_world(tmp_path / "refused", credentials=planner({}))
    world.backend.staging_method()
    gi = launch_input(world)
    script_helpers(world, gi)
    with pytest.raises(SandboxLaunchRefusedError, match="AF_TEST_KEY"):
        run_launch(world, inputs_for(world, gi), Journal(), ["true"])
    assert not any(c[0] in ("create", "volume") for c in world.calls())


# -- ownership before use or removal (D6) ------------------------------------------------


def _exporting(world: World) -> LaunchGitInput:
    gi = launch_input(world)
    script_helpers(world, gi, head=[0, json.dumps({"exporter": "head", "candidate": gi.seed})])
    pack = world.tmp / "candidate.pack"
    pack.write_bytes(
        subprocess.run(
            ["git", "-C", str(world.review.repo), "pack-objects", "--stdout", "--revs"],
            input=f"{gi.seed}\n^{gi.seed}\n".encode(),
            capture_output=True,
            check=True,
        ).stdout
    )
    fake.update(world.bin, pack_file=str(pack))
    return gi


def test_an_export_directory_that_was_there_is_never_removed(tmp_path):
    world = make_world(tmp_path)
    world.backend.staging_method()
    gi = _exporting(world)
    inputs = inputs_for(world, gi)
    planted = Path(inputs.export_dir)
    planted.mkdir(parents=True)
    (planted / "sentinel").write_text("not the launch's\n")
    journal = Journal()
    lifecycle = LaunchLifecycle(
        world.backend, journal, inputs, world.runtime, change_producing=True
    )
    with pytest.raises(SandboxCleanupUncertainError, match="export directory"):
        with lifecycle:
            lifecycle.launch(lambda: lifecycle.establish(PLANNED))
            exported = lifecycle.export()
            assert exported is not None and "exists already" in exported.failure
            lifecycle.saved()
    assert (planted / "sentinel").read_text() == "not the launch's\n"
    assert any(o.kind is ObjectKind.EXPORT_DIR for o in journal.open), "its entry stays open"
    assert not os.path.exists(export_owner_path(str(planted)))


@pytest.mark.parametrize("record", ["none", "partial", "another nonce", "another inode"])
def test_an_export_directory_not_proved_the_entrys_is_left_in_place(tmp_path, record):
    world = make_world(tmp_path)
    owner = LaunchOwner("run-1", 292, "FIX", "L-1")
    directory = tmp_path / "export" / "L-1-0123"
    entry = LifecycleObject(owner, ObjectKind.EXPORT_DIR, str(directory), "nonce-1")
    directory.mkdir(parents=True)
    (directory / "sentinel").write_text("keep\n")
    info = os.lstat(directory)
    owned = {
        "schema": 1, "run": "run-1", "issue": 292, "phase": "FIX", "launch": "L-1",
        "nonce": "nonce-1", "path": str(directory), "device": info.st_dev, "inode": info.st_ino,
    }  # fmt: skip
    record_path = Path(export_owner_path(str(directory)))
    if record == "partial":
        record_path.write_text(json.dumps(owned)[:20])
    elif record == "another nonce":
        record_path.write_text(json.dumps({**owned, "nonce": "nonce-2"}))
    elif record == "another inode":
        record_path.write_text(json.dumps({**owned, "inode": info.st_ino + 1}))
    assert world.backend.remove(entry) is Removal.UNCERTAIN
    assert (directory / "sentinel").read_text() == "keep\n"
    # The entry's own record over the same directory: removed, record last.
    record_path.write_text(json.dumps(owned))
    assert world.backend.remove(entry) is Removal.REMOVED
    assert not directory.exists() and not record_path.exists()
    # A removal cut short after the directory went: its record goes now.
    record_path.write_text(json.dumps(owned))
    assert world.backend.remove(entry) is Removal.REMOVED and not record_path.exists()


def test_docker_handing_back_a_volume_it_held_is_refused_and_never_adopted(tmp_path):
    world = make_world(tmp_path, runtime="docker")
    assert world.runtime.product == "Docker Engine"
    world.backend.staging_method()
    gi = launch_input(world)
    script_helpers(world, gi)
    inputs = inputs_for(world, gi)
    name = object_name(inputs.owner, ObjectKind.VOLUME)
    foreign = {LABEL_NONCE: "a-foreign-nonce", "io.autoforge.launch": gi.launch}
    fake.update(world.bin, volumes={name: {"labels": foreign}})
    journal = Journal()
    with pytest.raises(SandboxLaunchRefusedError, match="already in use"):
        run_launch(world, inputs, journal, ["true"])
    # Docker's create succeeded; the volume it returned was not this entry's,
    # so nothing mounted it, and its removal (by nonce) left it in place.
    assert ["volume", "create"] == next(c for c in world.calls() if c[0] == "volume")[:2]
    assert not any(c[0] == "create" for c in world.calls())
    assert world.state()["volumes"] == {name: {"labels": foreign}}
    assert any(o.name == name for o in journal.open), "its entry stays open"


def test_a_create_that_took_effect_and_lost_its_answer_is_reconciled_by_nonce(tmp_path):
    world = make_world(tmp_path, fail_after={"create": [1, "Error: context deadline exceeded"]})
    world.backend.staging_method()
    gi = launch_input(world)
    script_helpers(world, gi)
    journal = Journal()
    with pytest.raises(SandboxLaunchRefusedError, match="deadline exceeded"):
        run_launch(world, inputs_for(world, gi), journal, ["true"])
    assert world.state()["containers"] == {} and world.state()["volumes"] == {}
    assert journal.open == []


# -- the probe's objects (D6 item 9) ------------------------------------------------------


def test_a_probe_create_that_lost_its_answer_is_removed_before_its_entry_closes(tmp_path):
    world = make_world(
        tmp_path,
        transfer="auto",
        fail_after={"volume create": [1, "Error: context deadline exceeded"]},
    )
    journal = ProbeJournal(ProbeRoot(world.host.git_common_dir, "probe-1"), world.runtime)
    world.backend._probe_journal = journal  # noqa: SLF001
    with pytest.raises(SandboxLaunchRefusedError, match="deadline exceeded"):
        world.backend.staging_method()
    # The volume existed though its create "failed": it was removed by its
    # nonce, and only then was its entry closed; the container never was.
    assert any(c[:3] == ["volume", "rm", "--force"] for c in world.calls())
    assert world.state()["volumes"] == {}
    assert {o.name for batch in journal.closed for o in batch} == {
        o.name for o in journal.recorded[0]
    }


def test_a_probe_object_not_proved_gone_keeps_its_entry_open(tmp_path):
    world = make_world(
        tmp_path,
        transfer="auto",
        fail_after={"volume create": [1, "Error: context deadline exceeded"]},
        fail={"volume inspect": [1, "Error: connection refused"]},
    )
    journal = ProbeJournal(ProbeRoot(world.host.git_common_dir, "probe-1"), world.runtime)
    world.backend._probe_journal = journal  # noqa: SLF001
    with pytest.raises(SandboxLaunchRefusedError):
        world.backend.staging_method()
    volume, container = journal.recorded[0]
    closed = {o.name for batch in journal.closed for o in batch}
    assert container.name in closed and volume.name not in closed
    assert volume.name in world.state()["volumes"]


# -- the staging form is bound to the store it was proved on (D6 item 10, D12) ----------


def test_a_bind_proved_on_one_store_is_not_reused_once_the_store_moved(tmp_path):
    first = fake.podman_info(graph_root="/home/op/.local/share/containers/storage")
    moved = fake.podman_info(graph_root="/srv/elsewhere/storage")
    world = make_world(tmp_path, transfer="auto", infos=[first, moved])
    journal = ProbeJournal(ProbeRoot(world.host.git_common_dir, "probe-1"), world.runtime)
    world.backend._probe_journal = journal  # noqa: SLF001
    assert world.backend.staging_method() is StagingMethod.BIND
    assert world.backend.method is StagingMethod.BIND
    report = world.backend.runtime_check()
    assert report.store != world.runtime.store, "the second check reports the moved store"
    with pytest.raises(SandboxLaunchRefusedError, match="probe has not run"):
        world.backend.method  # noqa: B018
    # The old journal is bound to the old store: no probe, no bind.
    probes = len(journal.recorded)
    with pytest.raises(SandboxLaunchRefusedError, match="another runtime"):
        world.backend.staging_method()
    assert len(journal.recorded) == probes
    # On the moved store, a probe of its own decides: here, copy-in.
    fake.update(world.bin, shares_filesystem=False)
    world.backend._probe_journal = ProbeJournal(  # noqa: SLF001
        ProbeRoot(world.host.git_common_dir, "probe-2"), report
    )
    assert world.backend.staging_method() is StagingMethod.COPY
    gi = launch_input(world)
    with pytest.raises(SandboxLaunchRefusedError, match="decided on another runtime"):
        world.backend._method_report = world.runtime  # noqa: SLF001
        world.backend.seed(inputs_for(world, gi), PLANNED)


# -- the run step's close (D13) -----------------------------------------------------------


@pytest.mark.parametrize(
    "teardown, match",
    [
        ([125, ""], "ended with 125"),
        ([1, json.dumps({"teardown": "survivors", "survivors": 2})], "2 process"),
        ([0, json.dumps({"teardown": "proved", "survivors": 0}), 5], "did not end in time"),
    ],
)
def test_a_close_whose_teardown_is_not_proved_is_uncertain_and_no_process_follows(
    tmp_path, monkeypatch, teardown, match
):
    from autoforge import container_backend

    monkeypatch.setattr(container_backend, "KILL_TIMEOUT_SECONDS", 1)
    world = make_world(tmp_path, teardown=teardown)
    world.backend.staging_method()
    gi = launch_input(world)
    script_helpers(world, gi, head=[1, ""])
    marker = tmp_path / "second-process-ran"
    lifecycle = LaunchLifecycle(
        world.backend, Journal(), inputs_for(world, gi), world.runtime, change_producing=True
    )
    seen: dict[str, BaseException] = {}
    with lifecycle:

        def run():
            # Pi's shape: an auth check, then the agent, in one launch.
            for name, command in (("first", ["true"]), ("second", ["touch", str(marker)])):
                handle = lifecycle.establish(PLANNED)
                try:
                    execute(
                        ExecutionRequest(
                            command=command, env_allowlist=(), env=LAUNCH_ENV,
                            sandbox=bind(PLANNED, world.backend, handle), timeout_seconds=30,
                        )
                    )  # fmt: skip
                except (SandboxCleanupUncertainError, SandboxLaunchRefusedError) as exc:
                    seen[name] = exc

        lifecycle.launch(run)
        lifecycle.export()
        lifecycle.saved()
    assert isinstance(seen["first"], SandboxCleanupUncertainError)
    assert match in str(seen["first"])
    assert isinstance(seen["second"], SandboxLaunchRefusedError)
    assert "not proved gone" in str(seen["second"])
    assert not marker.exists(), "no later process of the launch ran"
    # The release still removed the container, and with it what it held.
    assert world.state()["containers"] == {}


def test_a_close_with_an_exception_already_ending_the_launch_adds_a_note(tmp_path):
    world = make_world(tmp_path, teardown=[125, ""])
    world.backend.staging_method()
    gi = launch_input(world)
    script_helpers(world, gi)
    lifecycle = LaunchLifecycle(
        world.backend, Journal(), inputs_for(world, gi, export=False), world.runtime,
        change_producing=False,
    )  # fmt: skip
    with lifecycle:

        def run():
            handle = lifecycle.establish(PLANNED)
            prepared = world.backend.prepare(PLANNED, handle, ["true"], cwd=None, env=LAUNCH_ENV)
            try:
                raise KeyboardInterrupt
            finally:
                prepared.close()

        with pytest.raises(KeyboardInterrupt) as ended:
            lifecycle.launch(run)
    assert any("not proved gone" in note for note in ended.value.__notes__)


# -- the plan step: ADR 0005 C7 as amended ----------------------------------------------


@pytest.mark.parametrize("transfer", ["auto", "copy"])
def test_the_plan_admits_the_staging_root_at_source_in_the_bind_form_only(tmp_path, transfer):
    from autoforge.sandbox import (
        SOURCE_TARGET,
        Access,
        Bind,
        Origin,
        ProjectionRequest,
        SandboxRequest,
    )
    from autoforge.sandbox_container import StagingRoot

    world = make_world(tmp_path, transfer=transfer)
    if transfer == "auto":
        root = ProbeRoot(world.host.git_common_dir, "probe-1")
        world.backend._probe_journal = ProbeJournal(root, world.runtime)  # noqa: SLF001
    method = world.backend.staging_method()
    staging = StagingRoot(world.host.git_common_dir, KEY, "L-1").path
    os.makedirs(staging)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    request = SandboxRequest(
        home=os.path.realpath(home),
        controller_env={},
        system_runtime=False,
        denied_roots=(os.path.join(world.host.git_common_dir, "autoforge"),),
        projections=(
            ProjectionRequest(
                staging, Access.READ_ONLY, Origin.ENGINE, target=SOURCE_TARGET, carve_out=True
            ),
        ),
    )
    if method is StagingMethod.BIND:
        planned = world.backend.plan(request)
        assert Bind(staging, SOURCE_TARGET, Access.READ_ONLY, True) in planned.mounts
    else:
        with pytest.raises(SandboxPolicyError, match="copy-in form binds nothing"):
            world.backend.plan(request)


# -- the launch's facts: CLI versions read inside, the seed's diagnostics (D2, D7) ------


def _versioned_world(tmp_path: Path, **answers) -> World:
    world = make_world(tmp_path)
    world.backend._image = ImageRef(  # noqa: SLF001
        IMAGE.id, IMAGE.key, clis=(("claude_code", "2.0.14"), ("pi", "1.0.2"))
    )
    world.backend.staging_method()
    return world


def test_each_pinned_cli_version_is_read_inside_the_launch_container(tmp_path):
    world = _versioned_world(tmp_path)
    gi = launch_input(world)
    script_helpers(
        world,
        gi,
        head=[1, ""],
        claude=[0, "2.0.14 (Claude Code)\n"],
        pi=[0, "1.0.2\n"],
    )
    result, _, _ = run_launch(world, inputs_for(world, gi), Journal(), ["true"])
    assert result.sandbox is not None
    assert result.sandbox.cli_versions == (("claude", "2.0.14 (Claude Code)"), ("pi", "1.0.2"))
    assert result.sandbox.record()["cli_versions"] == {
        "claude": "2.0.14 (Claude Code)",
        "pi": "1.0.2",
    }
    launch = object_name(inputs_for(world, gi).owner, ObjectKind.LAUNCH_CONTAINER)
    versions = [e for e in world.state()["execs"] if "--version" in e["command"]]
    assert [e["container"] for e in versions] == [launch, launch]
    assert all(e["opts"]["user"] == ["10001:10001"] for e in versions), "as the agent"


@pytest.mark.parametrize(
    "answer, match",
    [([0, "2.0.15 (Claude Code)\n"], "not the pinned 2.0.14"), ([127, ""], "did not run")],
)
def test_an_image_whose_cli_is_not_its_pinned_version_is_refused(tmp_path, answer, match):
    world = _versioned_world(tmp_path)
    gi = launch_input(world)
    script_helpers(world, gi, claude=answer, pi=[0, "1.0.2\n"])
    marker = tmp_path / "ran"
    with pytest.raises(SandboxLaunchRefusedError, match=match):
        run_launch(world, inputs_for(world, gi), Journal(), ["touch", str(marker)])
    assert not marker.exists()
    assert world.state()["containers"] == {} and world.state()["volumes"] == {}


def test_a_repository_command_reads_no_cli_version(tmp_path):
    world = _versioned_world(tmp_path)
    gi = launch_input(world, command=True)
    script_helpers(world, gi, claude=[1, ""], pi=[1, ""])
    _prepared(world, inputs_for(world, gi, export=False), {})
    assert not any("--version" in e["command"] for e in world.state().get("execs", []))


def test_the_facts_carry_the_seeds_diagnostics(tmp_path):
    from autoforge.sandbox_backend import LaunchHandle, Seed

    world = make_world(tmp_path)
    world.backend.staging_method()
    gi = launch_input(world)
    diagnostics = ("a gitlink: vendor/lib is an empty directory", "Git LFS: content unsupported")
    handle = LaunchHandle(inputs_for(world, gi), Seed(diagnostics=diagnostics))
    facts = world.backend._facts(PLANNED, handle)  # noqa: SLF001
    assert facts.diagnostics == diagnostics
    assert facts.record()["diagnostics"] == list(diagnostics)
    assert "diagnostics" not in world.backend._facts(PLANNED).record()  # noqa: SLF001


def test_the_backends_policy_inputs_name_what_it_runs_on_and_nothing_unknown(tmp_path):
    world = make_world(tmp_path)
    inputs = dict(world.backend.policy_inputs())
    assert inputs["backend"] == "container" and inputs["runtime"] == "Podman"
    assert inputs["image"] == IMAGE.digest and inputs["staging"] == ""
    world.backend.staging_method()
    assert dict(world.backend.policy_inputs())["staging"] == "copy"
