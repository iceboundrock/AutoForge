"""The outer sandbox's backend seam (ADR 0013 D13, D16; #268).

- bwrap behind the seam: each step delegates to :mod:`autoforge.sandbox_bwrap`
  or does nothing, and only the seam imports that module. That no byte of a
  bwrap launch changed is the unchanged ``tests/test_sandbox.py``,
  ``tests/test_sandbox_bwrap.py``, ``tests/test_sandbox_routing.py`` and
  ``tests/test_sandbox_parity.py``.
- The executor and the duplex executor on a fake backend's prepared launch
  (the run step): the launch is prepared by the backend it is bound to, with
  the handle it is bound to, never by its type; the exact argv, stdin and
  environment, streaming, a stop, the refusals before and after the spawn,
  and ``close()`` on every exit path, ``Ctrl-C`` and an exception inside the
  launch guard included.
- Backend-qualified :class:`~autoforge.sandbox.SandboxFacts`.

The order of the other steps, and the engine's use of them, are
``tests/test_sandbox_lifecycle.py``'s.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from autoforge import sandbox_backend, sandbox_bwrap
from autoforge.errors import SandboxLaunchRefusedError
from autoforge.executor import ExecutionRequest, execute
from autoforge.executor_duplex import DuplexRequest, Record, start_duplex
from autoforge.sandbox import (
    TEARDOWN_UNCOUNTED,
    BackendIdentity,
    BackendKind,
    LaunchDescription,
    SandboxFacts,
    SandboxRequest,
    compile_launch,
)
from autoforge.sandbox_backend import (
    BWRAP,
    BWRAP_RUNTIME,
    NO_SEED,
    LaunchBinding,
    LaunchHandle,
    LaunchInputs,
    LaunchOwner,
    LifecycleObject,
    ObjectKind,
    Removal,
    backend_for,
    bind,
    default_backend,
)

SRC = Path(__file__).resolve().parent.parent / "src" / "autoforge"
OWNER = LaunchOwner(run="run-1", issue=268, phase="REVIEW", launch="REVIEW-1")
INPUTS = LaunchInputs(OWNER, workspace="/work/268")
# The handle of the environment a lifecycle made for the launch.
HANDLE = LaunchHandle(
    INPUTS,
    NO_SEED,
    (LifecycleObject(OWNER, ObjectKind.LAUNCH_CONTAINER, "launch", "n-launch", "bridge"),),
)
_linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="pass_fds and /proc are Linux-only here"
)

# The fake backend's own process in the command's place: it marks the
# command as run inside, then becomes it.
WRAPPER = "import os, sys\nos.environ['FAKE_INSIDE'] = '1'\nos.execvp(sys.argv[1], sys.argv[1:])\n"
CONTAINER_FACTS = SandboxFacts(
    enforced=True,
    backend="container",
    teardown="removed with the container's PID namespace, uncounted",
    network="bridge",
    runtime="podman",
    runtime_version="5.2.0",
    rootless=True,
    image_id="sha256:0123abcd",
    staging="copy",
    watch="the container is removed within 5s of the controller's death",
)


class FakeDescription(LaunchDescription):
    """A launch type no backend registered: only its binding says who prepares it."""


class FakeLaunch:
    """A prepared launch as :class:`~autoforge.sandbox_backend.PreparedLaunch` says.

    It runs the command through :data:`WRAPPER` with exactly the environment
    the executor selected, plus a liveness stream of its own on a descriptor
    that is not the command's stdin. ``fail`` makes :meth:`outcome` raise.
    """

    def __init__(self, command, cwd, env, calls: list[str], fail: BaseException | None) -> None:
        self.calls = calls
        self.fail = fail
        self.liveness_r, self.liveness_w = os.pipe()
        self.argv = [sys.executable, "-c", WRAPPER, *command]
        self.spawn_env = {**env, "FAKE_LIVENESS_FD": str(self.liveness_r)}
        self.pass_fds = (self.liveness_r,)
        self.preexec_fn = _nothing
        self.ended_by_executor: bool | None = None

    def spawned(self) -> None:
        self.calls.append("spawned")

    def spawn_error(self, exc: BaseException) -> SandboxLaunchRefusedError:
        self.calls.append("spawn_error")
        return SandboxLaunchRefusedError(f"fake: the launch's process failed ({exc})")

    def collect(self) -> None:
        self.calls.append("collect")

    def outcome(self, *, returncode, ended_by_executor, stderr) -> SandboxFacts:
        self.calls.append("outcome")
        self.ended_by_executor = ended_by_executor
        if self.fail is not None:
            raise self.fail
        return CONTAINER_FACTS

    def close(self) -> None:
        self.calls.append("close")
        for fd in (self.liveness_r, self.liveness_w):
            try:
                os.close(fd)
            except OSError:
                pass

    def liveness_bytes(self) -> bytes:
        """What reached the liveness stream: nothing the executor wrote."""
        os.set_blocking(self.liveness_r, False)
        try:
            return os.read(self.liveness_r, 65536)
        except BlockingIOError:
            return b""


def _nothing() -> None:
    return None


class FakeBackend:
    """The run step of a fake backend: what it was asked to prepare, and its launches."""

    def __init__(self) -> None:
        self.prepared: list[tuple] = []
        self.handles: list[LaunchHandle | None] = []
        self.launches: list[FakeLaunch] = []
        self.calls: list[str] = []
        self.refuse = False
        self.fail: BaseException | None = None

    def prepare(self, launch, handle, command, *, cwd, env):
        self.prepared.append((type(launch), list(command), cwd, dict(env)))
        self.handles.append(handle)
        if self.refuse:
            raise SandboxLaunchRefusedError("fake: the runtime refused the launch")
        made = FakeLaunch(command, cwd, env, self.calls, self.fail)
        self.launches.append(made)
        return made


@pytest.fixture
def fake_backend() -> FakeBackend:
    return FakeBackend()


def _description(backend: FakeBackend) -> LaunchDescription:
    """A launch the fake backend planned, bound to it and to its environment's handle."""
    return bind(FakeDescription(mounts=()), backend, HANDLE)  # type: ignore[arg-type]


def _py(script: str) -> list[str]:
    return [sys.executable, "-c", script]


# -- only the seam knows bwrap -------------------------------------------------------


def _imports_of(path: Path) -> set[str]:
    """The ``autoforge`` modules ``path`` imports, by their last name."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {alias.name.rsplit(".", 1)[-1] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.module in (None, "autoforge") or (node.level and not node.module):
                found |= {alias.name for alias in node.names}
            elif node.module:
                found.add(node.module.rsplit(".", 1)[-1])
    return found


def test_only_the_seam_imports_the_bwrap_backend():
    """The engine, the executors, ``doctor`` and the policy reach bwrap through the
    seam, for a launch and for a probe; a direct import fails here (#268)."""
    importers = sorted(
        path.name for path in SRC.glob("*.py") if "sandbox_bwrap" in _imports_of(path)
    )
    assert importers == ["sandbox_backend.py"]
    for name in ("engine.py", "executor.py", "executor_duplex.py", "doctor_sandbox.py"):
        assert "sandbox_backend" in _imports_of(SRC / name) or name == "executor_duplex.py"


def test_the_seam_knows_no_workflow():
    """The seam's modules, and the git input they carry (#269), which is plain
    data importing nothing of the package."""
    allowed = {
        "sandbox_backend.py": {"errors", "git_input", "sandbox", "sandbox_bwrap"},
        "sandbox_lifecycle.py": {"errors", "git_input", "sandbox", "sandbox_backend"},
        "git_input.py": set(),
    }
    for name, modules in allowed.items():
        tree = ast.parse((SRC / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                imported = [node.module] if node.module else [a.name for a in node.names]
                assert set(imported) <= modules, (name, imported)


# -- bwrap behind the seam, unchanged ------------------------------------------------


def test_bwrap_is_the_default_and_prepares_every_launch_description():
    assert default_backend() is BWRAP
    assert backend_for(LaunchDescription(mounts=())) is BWRAP
    assert BWRAP.kind is BackendKind.BWRAP
    assert BackendKind.BWRAP.value == sandbox_bwrap.BACKEND


def test_bwraps_run_step_and_probe_are_the_backends_own(monkeypatch, tmp_path):
    calls: list[tuple] = []
    sentinel = object()

    def prepare(description, command, *, cwd, env):
        calls.append(("prepare", description, command, cwd, env))
        return sentinel

    def probe(home=None, environ=None):
        calls.append(("probe", home, environ))
        return sentinel

    monkeypatch.setattr(sandbox_bwrap, "prepare", prepare)
    monkeypatch.setattr(sandbox_bwrap, "probe", probe)
    description = LaunchDescription(mounts=())
    env = {"PATH": "/usr/bin"}
    assert sandbox_backend.prepare(description, ["true"], cwd="/tmp", env=env) is sentinel
    # Bound by bwrap's plan, with the empty handle of its lifecycle: the same call.
    bound = bind(description, BWRAP, LaunchHandle(INPUTS, NO_SEED))
    assert sandbox_backend.prepare(bound, ["true"], cwd="/tmp", env=env) is sentinel
    assert BWRAP.probe(home=str(tmp_path), environ={"HOME": "x"}) is sentinel
    assert calls == [
        ("prepare", description, ["true"], "/tmp", env),
        ("prepare", description, ["true"], "/tmp", env),
        ("probe", str(tmp_path), {"HOME": "x"}),
    ]
    assert calls[1][1].binding == LaunchBinding(BWRAP, LaunchHandle(INPUTS, NO_SEED))


def test_bwraps_plan_is_the_compiler_as_it_is(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    request = SandboxRequest(home=str(home), controller_env={}, system_runtime=False)
    assert BWRAP.plan(request) == compile_launch(request)


def test_bwrap_seeds_creates_stages_cleans_up_and_reconciles_nothing():
    obj = LifecycleObject(OWNER, ObjectKind.VOLUME, "vol", "n-vol")
    planned = LaunchDescription(mounts=())
    empty = LaunchHandle(INPUTS, NO_SEED)
    assert BWRAP.runtime_check() == BWRAP_RUNTIME
    assert BWRAP.seed(INPUTS, planned) == NO_SEED
    assert BWRAP.objects(INPUTS, NO_SEED, planned) == ()
    assert BWRAP.exporter(empty) == ()
    assert BWRAP.reconcile((obj,), BWRAP_RUNTIME) == ()
    BWRAP.discard_seed(NO_SEED)
    for step in (
        lambda: BWRAP.make(obj, NO_SEED, planned),
        lambda: BWRAP.stage(obj, NO_SEED),
        lambda: BWRAP.start(obj),
        lambda: BWRAP.export((obj,), empty),
        # A handle naming an object is another backend's environment.
        lambda: BWRAP.prepare(planned, HANDLE, ["true"], cwd=None, env={}),
    ):
        with pytest.raises(SandboxLaunchRefusedError, match="bwrap"):
            step()
    # Never "removed": bwrap made no such object, so it can prove nothing.
    assert BWRAP.remove(obj) is Removal.UNCERTAIN


# -- the run step on a fake backend --------------------------------------------------

_REPORT = (
    "import json, os, sys\n"
    "print(json.dumps({'stdin': sys.stdin.read(), 'x': os.environ.get('X'),\n"
    "    'inside': os.environ.get('FAKE_INSIDE'), 'secret': os.environ.get('AF_TEST_SECRET'),\n"
    "    'cwd': os.getcwd(), 'argv': sys.argv[1:]}))\n"
)


@_linux_only
def test_the_executor_hands_the_backend_the_command_its_cwd_and_the_selected_environment(
    tmp_path, monkeypatch, fake_backend
):
    monkeypatch.setenv("AF_TEST_SECRET", "not allow-listed")
    command = [*_py(_REPORT), "one", "two words"]
    result = execute(
        ExecutionRequest(
            command=command,
            cwd=str(tmp_path),
            env_allowlist=("PATH",),
            env={"X": "y"},
            stdin_data=b"hello\nworld\n",
            timeout_seconds=30,
            sandbox=_description(fake_backend),
        )
    )
    # Exactly the command, its cwd and the environment the executor selected,
    # with the handle the launch is bound to.
    assert fake_backend.prepared == [
        (FakeDescription, command, str(tmp_path), {"PATH": os.environ["PATH"], "X": "y"})
    ]
    assert fake_backend.handles == [HANDLE]
    assert result.exit_code == 0, result.stderr
    report = json.loads(result.stdout)
    assert report == {
        "stdin": "hello\nworld\n",
        "x": "y",
        "inside": "1",
        "secret": None,
        "cwd": str(tmp_path),
        "argv": ["one", "two words"],
    }
    assert result.sandbox is CONTAINER_FACTS
    assert fake_backend.calls == ["spawned", "outcome", "close"]
    assert fake_backend.launches[0].ended_by_executor is False


@_linux_only
def test_a_fake_backends_launch_streams_through_the_duplex_executor(fake_backend):
    """Records go to the agent's stdin and come back framed; the backend's liveness
    stream, on a descriptor of its own, is never the agent's stdin and receives
    nothing the executor writes (ADR 0013 D13: a liveness channel that is not
    the agent's stdin)."""
    echo = (
        "import os, sys\n"
        "live = int(os.environ['FAKE_LIVENESS_FD'])\n"
        "print('stdin-is-liveness', os.fstat(0).st_ino == os.fstat(live).st_ino, flush=True)\n"
        "for line in sys.stdin:\n"
        "    print(line.strip()[::-1], flush=True)\n"
    )
    request = DuplexRequest(
        command=_py(echo),
        env_allowlist=("PATH",),
        max_runtime_seconds=30,
        sandbox=_description(fake_backend),
    )
    with start_duplex(request) as child:
        assert child.read_line(10) == Record(b"stdin-is-liveness False")
        for text in (b"abc", b"stream"):
            child.send_line(text)
            assert child.read_line(10) == Record(text[::-1])
        launch = fake_backend.launches[0]
        assert launch.liveness_bytes() == b""
        child.close_stdin()
        done = child.finish()
    assert done.sandbox is CONTAINER_FACTS
    # The duplex teardown collects and closes the launch; finish() then
    # classifies it from what was collected (ADR 0002's teardown, unchanged).
    assert fake_backend.calls == ["spawned", "collect", "close", "outcome"]
    assert launch.ended_by_executor is False


@_linux_only
def test_a_stop_ends_a_fake_backends_launch_as_the_executors_own(fake_backend):
    stop = threading.Event()
    timer = threading.Timer(0.2, stop.set)
    timer.start()
    try:
        result = execute(
            ExecutionRequest(
                command=_py("import time; time.sleep(30)"),
                env_allowlist=("PATH",),
                timeout_seconds=30,
                stop=stop,
                sandbox=_description(fake_backend),
            )
        )
    finally:
        timer.cancel()
    assert result.stopped and result.exit_code == -1
    assert fake_backend.launches[0].ended_by_executor is True
    assert fake_backend.calls == ["spawned", "outcome", "close"]


@_linux_only
@pytest.mark.parametrize("duplex", [False, True])
def test_a_backend_that_refuses_to_prepare_spawns_nothing(tmp_path, fake_backend, duplex):
    fake_backend.refuse = True
    marker = tmp_path / "ran"
    command = _py(f"open({str(marker)!r}, 'w').close()")
    with pytest.raises(SandboxLaunchRefusedError, match="refused the launch"):
        if duplex:
            start_duplex(DuplexRequest(command=command, sandbox=_description(fake_backend)))
        else:
            execute(
                ExecutionRequest(
                    command=command, timeout_seconds=30, sandbox=_description(fake_backend)
                )
            )
    assert not marker.exists() and fake_backend.calls == []


@_linux_only
@pytest.mark.parametrize("duplex", [False, True])
def test_a_launch_no_backend_prepares_is_refused_and_never_run(tmp_path, duplex):
    class Unknown(LaunchDescription):
        pass

    marker = tmp_path / "ran"
    command = _py(f"open({str(marker)!r}, 'w').close()")
    with pytest.raises(SandboxLaunchRefusedError, match="no sandbox backend prepares a Unknown"):
        if duplex:
            start_duplex(DuplexRequest(command=command, sandbox=Unknown(mounts=())))
        else:
            execute(
                ExecutionRequest(command=command, timeout_seconds=30, sandbox=Unknown(mounts=()))
            )
    assert not marker.exists()


def test_a_bound_launch_is_prepared_by_the_backend_that_planned_it_never_by_its_type(
    monkeypatch,
):
    """A ``LaunchDescription`` is bwrap's type; bound to another backend, that
    backend prepares it, in the environment its handle names (ADR 0013 D13)."""
    monkeypatch.setattr(
        sandbox_bwrap, "prepare", lambda *a, **k: pytest.fail("bwrap prepared it by its type")
    )
    planner, other = FakeBackend(), FakeBackend()
    launch = sandbox_backend.prepare(
        bind(LaunchDescription(mounts=()), planner, HANDLE),  # type: ignore[arg-type]
        ["true"],
        cwd="/work",
        env={"PATH": "/usr/bin"},
    )
    assert planner.launches == [launch] and planner.handles == [HANDLE]
    assert planner.prepared == [(LaunchDescription, ["true"], "/work", {"PATH": "/usr/bin"})]
    assert other.prepared == []
    launch.close()


def test_a_launch_bound_to_no_backend_is_refused():
    stray = dataclasses.replace(LaunchDescription(mounts=()), binding="bwrap, please")
    with pytest.raises(SandboxLaunchRefusedError, match="names no sandbox backend"):
        sandbox_backend.prepare(stray, ["true"], cwd=None, env={})


@_linux_only
def test_a_setup_failure_after_the_spawn_is_a_refusal_and_the_launch_is_closed(fake_backend):
    fake_backend.fail = SandboxLaunchRefusedError("fake: the container never started the command")
    with pytest.raises(SandboxLaunchRefusedError, match="never started"):
        execute(
            ExecutionRequest(
                command=_py("print('x')"), timeout_seconds=30, sandbox=_description(fake_backend)
            )
        )
    assert fake_backend.calls == ["spawned", "outcome", "close"]


@_linux_only
@pytest.mark.parametrize("interruption", [KeyboardInterrupt, RuntimeError])
def test_the_prepared_launch_is_closed_on_every_exit_path_of_execute(fake_backend, interruption):
    """An interruption inside the launch guard, after the spawn: ``close()`` still runs."""
    fake_backend.fail = interruption("inside the guard")
    with pytest.raises(interruption, match="inside the guard"):
        execute(
            ExecutionRequest(
                command=_py("print('x')"), timeout_seconds=30, sandbox=_description(fake_backend)
            )
        )
    assert fake_backend.calls == ["spawned", "outcome", "close"]


@_linux_only
@pytest.mark.parametrize("interruption", [KeyboardInterrupt, RuntimeError])
def test_the_prepared_launch_is_closed_on_every_exit_path_of_a_duplex_launch(
    fake_backend, interruption
):
    request = DuplexRequest(
        command=_py("import sys\nfor line in sys.stdin: print(line, flush=True)"),
        max_runtime_seconds=30,
        sandbox=_description(fake_backend),
    )
    with pytest.raises(interruption):
        with start_duplex(request) as child:
            child.send_line(b"one")
            raise interruption("inside the guard")
    assert fake_backend.calls.count("close") == 1
    assert fake_backend.calls[-1] == "close"


# -- backend-qualified facts (ADR 0013 D16) ------------------------------------------

# What a bwrap launch's record held before the seam, key for key.
BWRAP_RECORD_KEYS = [
    "enforced",
    "backend",
    "backend_version",
    "teardown",
    "network",
    "tmpfs",
    "unset_env",
    "setup_failure",
]


def test_bwrap_facts_name_their_backend_and_record_what_they_recorded_before():
    facts = SandboxFacts(
        enforced=True,
        backend=sandbox_bwrap.BACKEND,
        backend_version="0.11.0",
        teardown=TEARDOWN_UNCOUNTED,
        network="host",
        tmpfs=(("/tmp", 1024),),
        unset_env=("XDG_RUNTIME_DIR",),
    )
    assert facts.identity == BackendIdentity(BackendKind.BWRAP, version="0.11.0")
    assert facts.identity.describe() == "enforced by bubblewrap 0.11.0"
    assert list(facts.record()) == BWRAP_RECORD_KEYS
    assert facts.record() == {
        "enforced": True,
        "backend": "bubblewrap",
        "backend_version": "0.11.0",
        "teardown": "removed by namespace teardown, uncounted",
        "network": "host",
        "tmpfs": {"/tmp": 1024},
        "unset_env": ["XDG_RUNTIME_DIR"],
        "setup_failure": "",
    }


def test_container_facts_name_the_runtime_and_their_own_teardown():
    identity = CONTAINER_FACTS.identity
    assert identity is not None and identity.kind is BackendKind.CONTAINER
    assert identity.describe() == "enforced by a rootless Podman container"
    assert BackendIdentity(BackendKind.CONTAINER, runtime="docker", rootless=False).describe() == (
        "enforced by a rootful Docker Engine container"
    )
    record = CONTAINER_FACTS.record()
    assert record["teardown"] != TEARDOWN_UNCOUNTED
    assert {k: record[k] for k in ("runtime", "runtime_version", "rootless", "staging")} == {
        "runtime": "podman",
        "runtime_version": "5.2.0",
        "rootless": True,
        "staging": "copy",
    }
    assert record["image_id"] == "sha256:0123abcd" and record["watch"].startswith("the container")


def test_facts_that_name_no_backend_have_no_identity_and_long_facts_are_bounded():
    assert SandboxFacts(enforced=False, backend="", setup_failure="refused").identity is None
    long = SandboxFacts(enforced=True, backend="container", runtime="p" * 500, watch="w" * 5000)
    assert len(long.runtime) == 128 and len(long.watch) == 1000
