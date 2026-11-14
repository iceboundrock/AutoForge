"""The container security policy on a real rootless runtime (ADR 0013 §7, #270). Opt-in.

What ADR 0013 §7 asks of #270 on a real runtime, with fake credentials only:

- the agent's UID is a subordinate one (read from the host), with no
  capabilities, no writable root and no host session-keyring key;
- the host loopback nonce listener (``127.0.0.1`` and ``::1``) and a host
  abstract socket are unreachable from the bridge, and ``--network none``
  has no interface but loopback and no route;
- every fake credential sentinel outside the projection is absent: not in
  the container's environment or files, and not in the runtime's record
  of the container (``inspect``), where only an environment credential's
  value persists, as D14 states; and none is on the argv of the create or
  of the measurement's ``exec``: the measurement takes its request, the
  sentinels included, on standard input.

The container is created by :func:`autoforge.sandbox_container.create_argv`
exactly as the policy renders it, PID 1 standing in for the watcher (#272),
and measured by :mod:`autoforge.container_measure` run as the agent through
:func:`~autoforge.sandbox_container.exec_argv`, judged by
:func:`~autoforge.sandbox_container.judge`, which also compares the device
and inode ``/source`` and the credential file show inside with what the
host's ``lstat`` of their sources reads. The staging root is a
:class:`~autoforge.sandbox_container.StagingRoot` in a git common dir of the
test's own, laid out as the controller lays one out; the host's nonce
listeners are on ``127.0.0.1`` and ``::1`` both, and a host with no IPv6
loopback skips.

**Opt-in.** Until #271 builds the image and the probe exists, nothing can
say a host is capable, so these tests run only when the operator names a
runtime and an image: ``AUTOFORGE_CONTAINER_TEST_RUNTIME`` (``podman`` or
``docker``, on ``PATH`` or as a path) and ``AUTOFORGE_CONTAINER_TEST_IMAGE``
(present locally, with ``python3``; nothing is pulled). A short setup
container stands in for the stager (#271) and hands the agent's UID the
fresh workspace volume. Otherwise, and on a
runtime that is not rootless or cannot start a container, each test skips
with the reason, never passes; ``AUTOFORGE_REQUIRE_CONTAINER_RUNTIME=1``
makes those a failure instead. Each object the test creates carries its
own nonce in its name and is removed in ``finally``.
"""

from __future__ import annotations

import json
import os
import platform
import pwd
import shutil
import socket
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from autoforge.sandbox_container import (
    AGENT_GID,
    AGENT_UID,
    HOME_TARGET,
    HOST_UID_MAP,
    SOURCE_TARGET,
    TMP_TARGET,
    WORKSPACE_TARGET,
    ContainerNetwork,
    ContainerRole,
    ContainerSpec,
    EnvCredential,
    Expectation,
    FileCredential,
    HostContext,
    Projection,
    RuntimeProduct,
    Secret,
    StagingRoot,
    client_environment,
    create_argv,
    exec_argv,
    judge,
    loopback_routes,
    subordinate_ranges,
)

RUNTIME_VAR = "AUTOFORGE_CONTAINER_TEST_RUNTIME"
IMAGE_VAR = "AUTOFORGE_CONTAINER_TEST_IMAGE"
REQUIRE_VAR = "AUTOFORGE_REQUIRE_CONTAINER_RUNTIME"
MEASURE = Path(__file__).resolve().parent.parent / "src" / "autoforge" / "container_measure.py"

SENTINEL_ENV = "fake-model-key-270-real-env-5d1"
SENTINEL_FILE = "fake-model-key-270-real-file-a09"
SENTINEL_GH = "fake-github-token-270-real-77c"
SENTINEL_OTHER = "fake-other-key-270-real-e3f"
# The places whose writability the agent tests; the judge allows only the
# policy's, and the runtime's own per-namespace temp.
PLACES = [
    "/",
    "/etc",
    "/usr",
    "/var",
    "/var/tmp",
    "/run",
    "/opt",
    "/root",
    "/home",
    "/dev",
    "/dev/shm",
    SOURCE_TARGET,
    WORKSPACE_TARGET,
    HOME_TARGET,
    TMP_TARGET,
]
_TIMEOUT = 120

# keyctl(2) and add_key(2), for joining a session keyring of the runtime
# client's own and putting a key in it.
_SYS_KEYCTL = {"x86_64": 250, "aarch64": 219}
_SYS_ADD_KEY = {"x86_64": 248, "aarch64": 217}
_KEYED = """
import ctypes, os, sys
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
keyctl, add_key, name = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
assert libc.syscall(keyctl, 1, None) > 0  # KEYCTL_JOIN_SESSION_KEYRING
assert libc.syscall(add_key, b"user", name.encode(), b"fake", 4, ctypes.c_long(-3)) > 0
os.execvp(sys.argv[4], sys.argv[4:])
"""


def _unavailable(reason: str) -> None:
    if os.environ.get(REQUIRE_VAR) == "1":
        pytest.fail(f"{REQUIRE_VAR}=1, but {reason}")
    pytest.skip(reason)


@dataclass(frozen=True)
class Runtime:
    product: RuntimeProduct
    executable: str
    image: str
    env: dict[str, str]
    keyed: tuple[str, ...]

    def run(
        self,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        check: bool = True,
        stdin: str | None = None,
    ):
        done = subprocess.run(
            [*self.keyed, self.executable, *args],
            env=env or self.env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
        if check and done.returncode != 0:
            raise AssertionError(f"{self.executable} {args[0]} failed: {done.stderr[-2000:]}")
        return done


@pytest.fixture(scope="module")
def runtime() -> Runtime:
    if not sys.platform.startswith("linux"):
        _unavailable("the container backend is Linux-only")
    name, image = os.environ.get(RUNTIME_VAR, ""), os.environ.get(IMAGE_VAR, "")
    if not name or not image:
        _unavailable(f"opt-in: set {RUNTIME_VAR} and {IMAGE_VAR} to run on a real runtime")
    executable = shutil.which(name)
    if executable is None:
        _unavailable(f"{name} is not on PATH")
    assert executable is not None
    base = os.path.basename(executable)
    product = RuntimeProduct.PODMAN if "podman" in base else RuntimeProduct.DOCKER
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "XDG_RUNTIME_DIR", "LANG")
        if key in os.environ
    }
    info = subprocess.run(
        [executable, "info", "--format", "json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
    )
    if info.returncode != 0:
        _unavailable(f"{name} info failed: {info.stderr.strip()[-400:]}")
    report = json.loads(info.stdout)
    if product is RuntimeProduct.PODMAN:
        rootless = report.get("host", {}).get("security", {}).get("rootless") is True
    else:
        rootless = any("rootless" in opt for opt in report.get("SecurityOptions", []))
    if not rootless:
        _unavailable(f"{name} is not rootless, which ADR 0013 D10 refuses")
    machine = platform.machine()
    if machine not in _SYS_KEYCTL:
        _unavailable(f"no keyring helper for {machine}")
    keyed = (
        sys.executable,
        "-I",
        "-c",
        _KEYED,
        str(_SYS_KEYCTL[machine]),
        str(_SYS_ADD_KEY[machine]),
    )
    found = subprocess.run(
        [executable, "image", "inspect", image], env=env, capture_output=True, timeout=_TIMEOUT
    )
    if found.returncode != 0:
        _unavailable(f"the image {image} is not present locally (nothing is pulled)")
    return Runtime(product, executable, image, env, keyed)


def _serve(server: socket.socket, nonce: bytes) -> None:
    def loop() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                conn.sendall(nonce)

    threading.Thread(target=loop, daemon=True).start()


@dataclass(frozen=True)
class Listeners:
    port: int
    loopback_nonce: str
    abstract_name: str
    abstract_nonce: str


@pytest.fixture
def listeners(runtime) -> Iterator[Listeners]:
    """A nonce TCP listener on host loopback, IPv4 and IPv6 on one port, and a host
    abstract Unix socket. Both families are required: a path to the one not listened on
    would go unseen, so a host with no IPv6 loopback skips."""
    nonce = f"nonce-loopback-{uuid.uuid4().hex}"
    v4 = socket.socket(socket.AF_INET)
    v4.bind(("127.0.0.1", 0))
    port = v4.getsockname()[1]
    servers = [v4]
    try:
        v6 = socket.socket(socket.AF_INET6)
        v6.bind(("::1", port))
        servers.append(v6)
    except OSError as exc:
        v4.close()
        _unavailable(f"no listener on the host's ::1 port {port} ({exc.strerror})")
    name = f"autoforge-270-{uuid.uuid4().hex}"
    abstract = socket.socket(socket.AF_UNIX)
    abstract.bind("\0" + name)
    abstract_nonce = f"nonce-abstract-{uuid.uuid4().hex}"
    for server in servers:
        server.listen(8)
        _serve(server, nonce.encode())
    abstract.listen(8)
    _serve(abstract, abstract_nonce.encode())
    try:
        yield Listeners(port, nonce, name, abstract_nonce)
    finally:
        for server in (*servers, abstract):
            server.close()


def _subordinate() -> tuple[tuple[int, int], ...]:
    try:
        text = Path("/etc/subuid").read_text(encoding="utf-8")
    except OSError:
        return ()
    return subordinate_ranges(text, pwd.getpwuid(os.getuid()).pw_name, os.getuid())


def _measure(
    runtime: Runtime,
    spec: ContainerSpec,
    host: HostContext,
    listeners: Listeners,
    sentinels: tuple[tuple[str, frozenset[str]], ...],
) -> tuple[dict, str, str]:
    nonce = uuid.uuid4().hex[:12]
    name = f"af-test-270-{nonce}"
    labels = {"io.autoforge.test": "270", "io.autoforge.nonce": nonce}
    key = f"autoforge-270-key-{nonce}"
    keyed = Runtime(
        runtime.product, runtime.executable, runtime.image, runtime.env, (*runtime.keyed, key)
    )
    base = {**runtime.env, "GH_TOKEN": SENTINEL_GH, "OTHER_API_KEY": SENTINEL_OTHER}
    client = client_environment(base, spec.projection)
    keyed.run(["volume", "create", spec.workspace_volume])
    try:
        _hand_over(keyed, spec.workspace_volume)
        argv = create_argv(
            runtime.product,
            spec,
            host,
            name=name,
            labels=labels,
            image=runtime.image,
            command=["python3", "-I", "-c", "import time; time.sleep(600)"],
        )
        keyed.run(argv, env=client)
        keyed.run(["start", name])
        request = {
            "places": PLACES,
            "connect_hosts": list(loopback_routes(runtime.product)),
            "connect_ports": [listeners.port],
            "abstract": listeners.abstract_name,
            "keyring": key,
            "sentinels": [value for value, _ in sentinels],
            "scan_roots": ["/"],
            "scan_skip": ["/proc", "/sys", "/dev"],
        }
        source = MEASURE.read_text(encoding="utf-8")
        # The request, every fake credential in it, goes on standard input: never on the
        # exec's argv, where every local process could read it (devcontainers/cli#1317).
        measure = exec_argv(runtime.product, name, ["python3", "-I", "-c", source])
        for word in (*argv, *measure, *keyed.keyed):
            assert not any(value in word for value, _ in sentinels), "a sentinel on an argv"
        measured = keyed.run(measure, stdin=json.dumps(request))
        observation = json.loads(measured.stdout)
        pid = keyed.run(["inspect", "--format", "{{.State.Pid}}", name]).stdout.strip()
        observation[HOST_UID_MAP] = Path(f"/proc/{pid}/uid_map").read_text(encoding="utf-8")
        container_id = keyed.run(["inspect", "--format", "{{.Id}}", name]).stdout.strip()
        record = keyed.run(["inspect", name]).stdout
        return observation, record, container_id
    finally:
        keyed.run(["rm", "--force", name], check=False)
        keyed.run(["volume", "rm", "--force", spec.workspace_volume], check=False)


def _hand_over(runtime: Runtime, volume: str) -> None:
    """Give the agent its workspace, as the stager will (#271): a fresh volume's root is
    container root's. Test scaffolding, offline and with CHOWN alone; never the policy's
    create, which no option of this call reaches."""
    runtime.run(
        [
            "run",
            "--rm",
            "--user=0:0",
            "--cap-drop=ALL",
            "--cap-add=CHOWN",
            "--network=none",
            "--pull=never",
            f"--mount=type=volume,source={volume},target={WORKSPACE_TARGET}",
            runtime.image,
            "python3",
            "-I",
            "-c",
            f"import os; os.chown({WORKSPACE_TARGET!r}, {AGENT_UID}, {AGENT_GID})",
        ]
    )


def _credential(tmp_path: Path) -> str:
    directory = tmp_path / "provider"
    directory.mkdir(mode=0o700)
    path = directory / "key"
    path.write_text(SENTINEL_FILE)
    path.chmod(0o644)
    return str(path)


def _common(tmp_path: Path) -> Path:
    """A git common dir of the test's own, inside its checkout as a main checkout's is."""
    common = tmp_path / "repo" / ".git"
    common.mkdir(parents=True, exist_ok=True)
    return common


def _staging(tmp_path: Path) -> StagingRoot:
    """A staging root laid out as the controller lays one out: 0700 directories, the root
    itself readable by the agent's subordinate UID, holding one staged file."""
    root = StagingRoot(str(_common(tmp_path)), "270", f"ANALYZE_EXECUTE-{uuid.uuid4().hex[:8]}")
    current = Path(root.common_dir)
    for part in root.layout.split("/"):
        current = current / part
        current.mkdir(exist_ok=True)
        current.chmod(0o700)
    current.chmod(0o755)
    (current / "README").write_text("staged\n")
    (current / "README").chmod(0o644)
    return root


def _host(tmp_path: Path) -> HostContext:
    """The controller's facts as the engine gives them: its common dir and checkout, and
    its denied roots (ADR 0005 D13.2) with the state directory at its default."""
    common = _common(tmp_path)
    checkout = common.parent
    state = checkout / ".autoforge"
    denied = (
        str(state),
        str(state / "logs"),
        str(common / "autoforge" / "controller.lock"),
        str(common / "autoforge"),
        str(checkout),
    )
    return HostContext.current(
        git_common_dir=str(common), checkouts=(str(checkout),), denied_roots=denied
    )


def _expectation(
    runtime: Runtime,
    spec: ContainerSpec,
    listeners: Listeners,
    container_id: str,
    sentinels: tuple[tuple[str, frozenset[str]], ...],
) -> Expectation:
    return Expectation.for_spec(
        spec,
        product=runtime.product,
        operator_uid=os.getuid(),
        subordinate=_subordinate(),
        container_id=container_id,
        loopback_nonce=listeners.loopback_nonce,
        loopback_listeners=(("127.0.0.1", listeners.port), ("::1", listeners.port)),
        abstract_nonce=listeners.abstract_nonce,
        keyring_checked=True,
        sentinels=tuple(allowed for _, allowed in sentinels),
    )


def test_an_agent_on_the_bridge_gets_the_policy_and_only_its_projection(
    runtime, listeners, tmp_path
):
    credential = _credential(tmp_path)
    # A host credential no launch projects, left where a bind of the wrong
    # source would show it.
    (tmp_path / "hosts.yml").write_text(f"oauth_token: {SENTINEL_GH}\n")
    file_item = FileCredential(credential, ".config/provider/key")
    spec = ContainerSpec(
        ContainerRole.LAUNCH,
        ContainerNetwork.BRIDGE,
        f"af-test-270-vol-{uuid.uuid4().hex[:12]}",
        source=_staging(tmp_path),
        projection=Projection(
            env=(EnvCredential("MODEL_API_KEY", Secret(SENTINEL_ENV)),), files=(file_item,)
        ),
    )
    sentinels = (
        (SENTINEL_ENV, frozenset({"env:MODEL_API_KEY"})),
        (SENTINEL_FILE, frozenset({file_item.path})),
        (SENTINEL_GH, frozenset()),
        (SENTINEL_OTHER, frozenset()),
    )
    observation, record, container_id = _measure(
        runtime, spec, _host(tmp_path), listeners, sentinels
    )
    expect = _expectation(runtime, spec, listeners, container_id, sentinels)
    findings = judge(observation, expect)
    assert findings == (), "\n".join(finding.describe() for finding in findings)
    # The runtime's record of the container: no host credential and no file
    # credential's content. The environment credential's value is there
    # until the container is removed: D14's stated residual, not checked away.
    for value in (SENTINEL_FILE, SENTINEL_GH, SENTINEL_OTHER):
        assert value not in record


def test_an_isolated_launch_has_no_interface_but_loopback_and_no_route(
    runtime, listeners, tmp_path
):
    spec = ContainerSpec(
        ContainerRole.LAUNCH,
        ContainerNetwork.NONE,
        f"af-test-270-vol-{uuid.uuid4().hex[:12]}",
        source=_staging(tmp_path),
    )
    sentinels = ((SENTINEL_GH, frozenset()), (SENTINEL_OTHER, frozenset()))
    observation, record, container_id = _measure(
        runtime, spec, _host(tmp_path), listeners, sentinels
    )
    expect = _expectation(runtime, spec, listeners, container_id, sentinels)
    findings = judge(observation, expect)
    assert findings == (), "\n".join(finding.describe() for finding in findings)
    assert SENTINEL_GH not in record and SENTINEL_OTHER not in record
