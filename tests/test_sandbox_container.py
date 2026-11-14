"""The container backend's security policy, network mapping and credential delivery (#270).

ADR 0013 D9, D10, D12's bind sources and D14's mechanisms, with no runtime:
every create the policy can render is checked for what it must hold and
for what it must never contain; every refusal the projection and the bind
sources make is driven with fake credentials, fake sockets and links under
``tmp_path``; the judge of what a container actually enforced is driven
with recorded observations, and the in-container measurement runs on this
host as its own negative control (the host is not a container, so the
judge must name what it does not prove). The real-runtime tests are
``tests/test_container_security_real.py``.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import os
import pickle
import socket
import stat
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from autoforge import container_measure
from autoforge.config import load_config_file
from autoforge.errors import ConfigurationError, SandboxCapabilityError, SandboxPolicyError
from autoforge.sandbox import BackendKind, NetworkPosition
from autoforge.sandbox_backend import CONTAINER_NETWORKS, RuntimeReport, runtime_refusal
from autoforge.sandbox_container import (
    AGENT_GID,
    AGENT_UID,
    CACHE_TARGET,
    HOME_TARGET,
    HOST_UID_MAP,
    NO_PROJECTION,
    PIDS_LIMIT,
    SOURCE_TARGET,
    TMP_TARGET,
    WATCHER_GID,
    WATCHER_UID,
    WORKSPACE_TARGET,
    ContainerCapability,
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
    StagingMethod,
    check_spec,
    client_environment,
    create_argv,
    exec_argv,
    judge,
    network_for,
    subordinate_ranges,
)

REPO = Path(__file__).resolve().parent.parent
MEASURE = REPO / "src" / "autoforge" / "container_measure.py"
# Fake credentials: never real, and shaped so that a leak is unmistakable.
SENTINEL_ENV = "fake-model-key-270-env-0f3a9c"
SENTINEL_FILE = "fake-model-key-270-file-7b1e44"
SENTINEL_GH = "fake-github-token-270-d2c8"
IMAGE = "sha256:" + "a" * 64
LABELS = {"io.autoforge.run": "run-1", "io.autoforge.nonce": "n-1"}


@pytest.fixture
def host(tmp_path: Path) -> HostContext:
    """A fake operator home, so D4.3's forbidden sources are this test's own."""
    home = tmp_path / "home"
    home.mkdir()
    runtime_dir = tmp_path / "xdg-runtime"
    runtime_dir.mkdir(mode=0o700)
    return HostContext(str(home), {"XDG_RUNTIME_DIR": str(runtime_dir)}, os.getuid())


@pytest.fixture
def staging(tmp_path: Path) -> str:
    root = tmp_path / "common" / "autoforge" / "sandbox" / "k" / "stage" / "1"
    root.mkdir(parents=True)
    return str(root)


@pytest.fixture
def credential(tmp_path: Path) -> str:
    """A credential file as D14's mechanism admits it: 0644 in a 0700 directory."""
    directory = tmp_path / "provider"
    directory.mkdir(mode=0o700)
    path = directory / "key"
    path.write_text(SENTINEL_FILE)
    path.chmod(0o644)
    return str(path)


def _env(name: str = "MODEL_API_KEY", value: str = SENTINEL_ENV) -> EnvCredential:
    return EnvCredential(name, Secret(value))


def _argv(spec: ContainerSpec, host: HostContext, product=RuntimeProduct.DOCKER, **kw) -> list:
    return create_argv(
        product,
        spec,
        host,
        name=kw.pop("name", "af-launch-1"),
        labels=kw.pop("labels", LABELS),
        image=kw.pop("image", IMAGE),
        command=kw.pop("command", ["/opt/autoforge/watcher"]),
    )


def _options(argv: list[str]) -> list[str]:
    """The create's options: everything before the image."""
    return argv[: argv.index(IMAGE)]


def _launch(**fields) -> ContainerSpec:
    return ContainerSpec(ContainerRole.LAUNCH, ContainerNetwork.BRIDGE, "af-vol-1", **fields)


# -- every spec the policy can render ------------------------------------------------


def _every_spec(staging: str, credential: str) -> Iterator[tuple[RuntimeProduct, ContainerSpec]]:
    projection = Projection(env=(_env(),), files=(FileCredential(credential, ".config/p/key"),))
    for product, role, network, form, cache, project in itertools.product(
        RuntimeProduct,
        ContainerRole,
        ContainerNetwork,
        StagingMethod,
        (False, True),
        (False, True),
    ):
        launch = role is ContainerRole.LAUNCH
        if not launch and (network is not ContainerNetwork.NONE or cache or project):
            continue
        if project and form is StagingMethod.COPY:
            projected = Projection(env=projection.env)
        else:
            projected = projection if project else NO_PROJECTION
        bound = form is StagingMethod.BIND and role is not ContainerRole.EXPORTER
        yield (
            product,
            ContainerSpec(
                role,
                network,
                "af-vol-1",
                staging=form,
                cache_volume="af-cache-270" if cache else "",
                source=staging if bound else "",
                projection=projected,
            ),
        )


# Flags no create may ever carry: a privilege, a device, a host namespace or a
# user-namespace mode, a bind by -v, an environment taken from a file or from
# the client's whole environment; and no confinement override.
_NEVER = frozenset(
    {
        "--privileged",
        "--cap-add",
        "--device",
        "--device-cgroup-rule",
        "--volume",
        "-v",
        "--volumes-from",
        "--env-file",
        "--env-host",
        "--env-merge",
        "--userns",
        "--uidmap",
        "--gidmap",
        "--subuidname",
        "--subgidname",
        "--pid",
        "--uts",
        "--cgroupns",
        "--cgroup-parent",
        "--group-add",
        "--sysctl",
        "--add-host",
        "--net",
        "--hostuser",
        "--secret",
        "--init",
        "--rootfs",
    }
)
_NEVER_SECURITY = ("seccomp", "apparmor", "label", "systempaths", "mask", "unmask", "proc-opts")


def test_every_create_carries_the_policy_and_nothing_that_undoes_it(staging, credential, host):
    """D10: non-root numeric user, every capability dropped, no-new-privileges, a
    read-only root, the PID limit, a private IPC namespace, no pull; D9: no host network;
    and no option that would grant, undo or widen any of it."""
    seen = 0
    for product, spec in _every_spec(staging, credential):
        options = _options(_argv(spec, host, product))
        seen += 1
        for required in (
            f"--user={WATCHER_UID}:{WATCHER_GID}",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--read-only",
            f"--pids-limit={PIDS_LIMIT}",
            "--ipc=private",
            "--pull=never",
        ):
            assert required in options, (product, spec, required)
        if product is RuntimeProduct.PODMAN:
            assert "--read-only-tmpfs=false" in options and "--http-proxy=false" in options
        for option in options:
            flag, _, value = option.partition("=")
            assert flag not in _NEVER, (product, spec.role, option)
            if flag == "--security-opt":
                assert not value.startswith(_NEVER_SECURITY), option
            assert "host" not in value.split(":")[0].split(","), option
        networks = [o for o in options if o.startswith("--network")]
        assert len(networks) == 1
        assert networks[0] in ("--network=none", "--network=bridge", "--network=pasta")
    assert seen == 40  # 16 launch and 4 helper specs, on each runtime


def test_no_network_position_is_the_hosts():
    """ADR 0013 D9: the only positions are the bridge and none; ``host`` cannot be asked."""
    assert {network.value for network in ContainerNetwork} == CONTAINER_NETWORKS
    assert "host" not in CONTAINER_NETWORKS
    with pytest.raises(ValueError):
        ContainerNetwork("host")


@pytest.mark.parametrize(
    ("role", "position", "network"),
    [
        (ContainerRole.LAUNCH, NetworkPosition.HOST, ContainerNetwork.BRIDGE),
        (ContainerRole.LAUNCH, NetworkPosition.ISOLATED, ContainerNetwork.NONE),
        (ContainerRole.STAGER, NetworkPosition.HOST, ContainerNetwork.NONE),
        (ContainerRole.STAGER, NetworkPosition.ISOLATED, ContainerNetwork.NONE),
        (ContainerRole.EXPORTER, NetworkPosition.HOST, ContainerNetwork.NONE),
        (ContainerRole.EXPORTER, NetworkPosition.ISOLATED, ContainerNetwork.NONE),
    ],
)
def test_the_network_mapping_is_d9s(role, position, network):
    """An agent and an export command on ``*_network: host`` (outbound access) get the
    bridge; ``isolated`` gets none; every helper container is offline."""
    assert network_for(role, position) is network


@pytest.mark.parametrize(
    ("product", "rendered"),
    [(RuntimeProduct.DOCKER, "--network=bridge"), (RuntimeProduct.PODMAN, "--network=pasta")],
)
def test_the_bridge_closes_host_loopback_by_the_helpers_defaults(product, rendered, host):
    """Podman's pasta gets no option, so its documented defaults hold (--no-map-gw, -T none,
    -U none); none that undoes them (--map-gw, -T, -U, --map-host-loopback) is given."""
    options = _options(_argv(_launch(), host, product))
    assert [o for o in options if o.startswith("--network")] == [rendered]
    for undo in ("--map-gw", "-T", "-U", "--map-host-loopback", "allow_host_loopback"):
        assert not any(undo in option for option in options)
    isolated = dataclasses.replace(_launch(), network=ContainerNetwork.NONE)
    assert "--network=none" in _options(_argv(isolated, host, product))


@pytest.mark.parametrize("role", [ContainerRole.STAGER, ContainerRole.EXPORTER])
def test_a_helper_container_on_the_bridge_is_refused(role, host):
    spec = ContainerSpec(role, ContainerNetwork.BRIDGE, "af-vol-1")
    with pytest.raises(SandboxPolicyError, match="--network none"):
        check_spec(spec, host)


def test_the_writable_places_are_the_volumes_the_home_and_temp(staging, credential, host):
    """D10: the root read-only; the only writable mounts are /tmp and the home (tmpfs, the
    home owned by the agent), the workspace volume (read-only for the exporter) and the
    cache volume; every bind is read-only."""
    for product, spec in _every_spec(staging, credential):
        options = _options(_argv(spec, host, product))
        tmpfs = sorted(o.split("=", 1)[1].split(":", 1)[0] for o in options if "--tmpfs" in o)
        assert tmpfs == sorted([HOME_TARGET, TMP_TARGET])
        home = next(o for o in options if o.startswith(f"--tmpfs={HOME_TARGET}:"))
        assert f"uid={AGENT_UID}" in home and f"gid={AGENT_GID}" in home and "mode=0700" in home
        mounts = [o.removeprefix("--mount=") for o in options if o.startswith("--mount=")]
        writable = sorted(m.split("target=")[1] for m in mounts if "readonly=true" not in m)
        expected = [] if spec.role is ContainerRole.EXPORTER else [WORKSPACE_TARGET]
        if spec.cache_volume:
            expected.append(CACHE_TARGET)
        assert writable == sorted(expected), (spec.role, writable)
        assert all(m.startswith("type=volume,") for m in mounts if "readonly=true" not in m)


def test_the_bind_sources_are_a_closed_set(staging, credential, host):
    """D12: the staging root at /source and the projection's files in the home, read-only,
    and nothing else; the copy-in form and the exporter bind nothing."""
    for product, spec in _every_spec(staging, credential):
        options = _options(_argv(spec, host, product))
        binds = [o for o in options if "type=bind" in o]
        expected = []
        if spec.source:
            expected.append(
                f"--mount=type=bind,source={staging},target={SOURCE_TARGET},readonly=true"
            )
        for item in spec.projection.files:
            expected.append(
                f"--mount=type=bind,source={item.source},target={item.path},readonly=true"
            )
        assert binds == expected, (spec.role, spec.staging)
        if spec.staging is StagingMethod.COPY or spec.role is ContainerRole.EXPORTER:
            assert binds == []
        for option in options:
            assert not any(flag in option for flag in (",z", ",Z", "relabel", "bind-propagation"))


def test_the_spec_has_no_field_that_takes_a_raw_flag_mount_or_option():
    """No configuration surface for raw flags, mounts, ``--privileged`` or a host PID
    namespace: the spec's fields are these, each typed, and a new one is a reviewed change."""
    assert [f.name for f in dataclasses.fields(ContainerSpec)] == [
        "role",
        "network",
        "workspace_volume",
        "staging",
        "cache_volume",
        "source",
        "projection",
        "home_size_bytes",
        "tmp_size_bytes",
    ]


@pytest.mark.parametrize(
    "sandbox",
    [
        {"container": {"run_args": ["--privileged"]}},
        {"container": {"privileged": True}},
        {"container": {"mounts": ["/var/run/docker.sock:/var/run/docker.sock"]}},
        {"container": {"pid": "host"}},
        {"container": {"network": "host"}},
        {"run_args": ["--cap-add=SYS_ADMIN"]},
        {"privileged": True},
        {"extra_mounts": ["/home"]},
        {"devices": ["/dev/kvm"]},
        {"security_opt": ["seccomp=unconfined"]},
    ],
)
def test_no_configuration_key_admits_a_raw_flag_or_mount(tmp_path, sandbox):
    """The MVP has no raw-flag, mount, privileged or host-namespace setting: each is an
    unknown key, refused at load (strict key validation)."""
    config = tmp_path / "autoforge.yaml"
    config.write_text(json.dumps({"version": 1, "sandbox": sandbox}), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="unknown key"):
        load_config_file(config)


@pytest.mark.parametrize(
    ("name", "why"),
    [
        ("/var/run/docker.sock", "never a host path"),
        ("../vol", "never a host path"),
        ("", "never a host path"),
        ("-v", "never a host path"),
        ("vol,type=bind", "never a host path"),
    ],
)
def test_a_volume_is_named_never_a_path(name, why, host):
    with pytest.raises(SandboxPolicyError, match=why):
        check_spec(ContainerSpec(ContainerRole.LAUNCH, ContainerNetwork.BRIDGE, name), host)


@pytest.mark.parametrize(
    ("field", "value", "why"),
    [
        ("image", "--privileged", "not an image"),
        ("image", "", "not an image"),
        ("name", "--name=x", "not a container name"),
        ("labels", {"Bad Key": "x"}, "not a label"),
        ("labels", {"io.autoforge.x": "a\nb"}, "not a label"),
    ],
)
def test_no_value_of_a_create_is_read_as_an_option(field, value, why, host):
    with pytest.raises(SandboxPolicyError, match=why):
        _argv(_launch(), host, **{field: value})


def test_the_command_follows_the_image_so_it_is_never_an_option(host):
    argv = _argv(_launch(), host, command=["--privileged", "x"])
    assert argv[-3:] == [IMAGE, "--privileged", "x"]
    assert "--privileged" not in _options(argv)


def test_exec_runs_the_agent_as_its_non_root_uid_with_no_environment_option():
    argv = exec_argv(RuntimeProduct.PODMAN, "af-launch-1", ["claude", "-p", "x"])
    assert argv == [
        "exec",
        "--interactive",
        f"--user={AGENT_UID}:{AGENT_GID}",
        f"--workdir={WORKSPACE_TARGET}",
        "af-launch-1",
        "claude",
        "-p",
        "x",
    ]
    assert AGENT_UID not in (0, os.getuid()) and AGENT_UID != WATCHER_UID
    assert not any(o.startswith(("--env", "-e", "--privileged")) for o in argv[:5])


# -- the projection (D14) -------------------------------------------------------------


def test_an_environment_credential_is_named_on_the_argv_and_valued_only_in_the_client(host):
    """The value never reaches an argv, a repr or a pickle; the client's environment is
    the only place it is put, under its name."""
    spec = _launch(projection=Projection(env=(_env(),)))
    for product in RuntimeProduct:
        argv = _argv(spec, host, product)
        assert "--env=MODEL_API_KEY" in argv
        assert not any(SENTINEL_ENV in word for word in argv)
    assert SENTINEL_ENV not in repr(spec) and SENTINEL_ENV not in str(spec.projection.env[0].value)
    with pytest.raises(TypeError):
        pickle.dumps(spec.projection.env[0].value)
    env = client_environment({"PATH": "/usr/bin"}, spec.projection)
    assert env == {"PATH": "/usr/bin", "MODEL_API_KEY": SENTINEL_ENV}


def test_a_projected_value_found_on_the_argv_refuses_the_create(host):
    spec = _launch(projection=Projection(env=(_env(),)))
    with pytest.raises(SandboxPolicyError, match="command line") as refused:
        _argv(spec, host, command=["/opt/watcher", SENTINEL_ENV])
    assert SENTINEL_ENV not in str(refused.value)
    with pytest.raises(SandboxPolicyError, match="command line"):
        _argv(spec, host, labels={"io.autoforge.x": SENTINEL_ENV})


def test_the_client_environment_never_overwrites_a_name_it_holds():
    with pytest.raises(SandboxPolicyError, match="already holds"):
        client_environment({"MODEL_API_KEY": "other"}, Projection(env=(_env(),)))


@pytest.mark.parametrize(
    "name",
    [
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "SSH_AUTH_SOCK",
        "GIT_ASKPASS",
        "GIT_CONFIG_GLOBAL",
        "HOME",
        "PATH",
        "DOCKER_HOST",
        "DOCKER_CONFIG",
        "CONTAINER_HOST",
        "CONTAINERS_CONF",
        "PODMAN_USERNS",
        "XDG_RUNTIME_DIR",
        "LD_PRELOAD",
        "HTTP_PROXY",
        "https_proxy",
        "BASH_FUNC_x",
        "NAME*",
        "A B",
        "1X",
        "",
    ],
)
def test_a_variable_the_client_reads_or_that_hands_over_a_credential_is_never_projected(name, host):
    spec = _launch(projection=Projection(env=(EnvCredential(name, Secret(SENTINEL_GH)),)))
    with pytest.raises(SandboxPolicyError) as refused:
        check_spec(spec, host)
    assert SENTINEL_GH not in str(refused.value)


@pytest.mark.parametrize("role", [ContainerRole.STAGER, ContainerRole.EXPORTER])
def test_only_the_launch_container_gets_a_projection(role, credential, host):
    for projection in (
        Projection(env=(_env(),)),
        Projection(files=(FileCredential(credential, "key"),)),
    ):
        spec = ContainerSpec(role, ContainerNetwork.NONE, "af-vol-1", projection=projection)
        with pytest.raises(SandboxPolicyError, match="only the launch container"):
            check_spec(spec, host)


def test_a_projected_value_is_never_empty_or_held_as_a_string(host):
    with pytest.raises(SandboxPolicyError, match="non-empty"):
        Secret("")
    plain = EnvCredential("MODEL_API_KEY", SENTINEL_ENV)  # type: ignore[arg-type]
    with pytest.raises(SandboxPolicyError, match="held as a Secret"):
        check_spec(_launch(projection=Projection(env=(plain,))), host)


def test_a_credential_file_is_bound_read_only_in_the_home_on_a_bind_runtime(credential, host):
    item = FileCredential(credential, ".config/provider/key.json")
    argv = _argv(_launch(projection=Projection(files=(item,))), host)
    target = f"{HOME_TARGET}/.config/provider/key.json"
    assert f"--mount=type=bind,source={credential},target={target},readonly=true" in argv
    # The controller never reads it: only its path is on the argv.
    assert not any(SENTINEL_FILE in word for word in argv)


def test_a_copy_in_runtime_delivers_no_credential_file(credential, host):
    """ADR 0005 D7.2: copying a file in would mean reading it."""
    spec = _launch(
        staging=StagingMethod.COPY, projection=Projection(files=(FileCredential(credential, "k"),))
    )
    with pytest.raises(SandboxPolicyError, match="copy-in runtime delivers only environment"):
        check_spec(spec, host)
    env_only = _launch(staging=StagingMethod.COPY, projection=Projection(env=(_env(),)))
    check_spec(env_only, host)


def _secret_file(path: Path, content: str = SENTINEL_FILE, mode: int = 0o644) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)
    return str(path)


@pytest.mark.parametrize(
    ("relative", "why"),
    [
        (".ssh/id_ed25519", "~/.ssh"),
        (".config/gh/hosts.yml", "gh configuration"),
        (".git-credentials", "git credential store"),
        (".netrc", "netrc"),
        (".gnupg/private-keys-v1.d/x.key", "gnupg"),
        (".local/share/keyrings/login.keyring", "keyrings"),
        (".config/git/credentials", "git configuration"),
    ],
)
def test_a_credential_file_under_a_forbidden_host_source_is_refused(relative, why, host):
    """D4.3: the host's GitHub, git and SSH credentials and keyrings never reach a container."""
    source = _secret_file(Path(host.home) / relative, SENTINEL_GH)
    Path(host.home).chmod(0o700)
    spec = _launch(projection=Projection(files=(FileCredential(source, "key"),)))
    with pytest.raises(SandboxPolicyError, match="D4.3 forbidden source") as refused:
        check_spec(spec, host)
    assert why.split()[-1] in str(refused.value)
    assert SENTINEL_GH not in str(refused.value)


def test_the_home_itself_or_an_ancestor_is_never_a_bind_source(host, tmp_path):
    for source in (host.home, str(tmp_path)):
        spec = _launch(source=source)
        with pytest.raises(SandboxPolicyError, match="operator's home"):
            check_spec(spec, host)


def test_a_runtime_socket_is_never_bound(host, tmp_path):
    """The runtime's API socket is authority equal to the operator's (D10): a socket is
    never a credential file, and $XDG_RUNTIME_DIR (rootless Docker's socket) and /run are
    forbidden sources."""
    sock_path = Path(host.environ["XDG_RUNTIME_DIR"]) / "docker.sock"
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(sock_path))
        spec = _launch(projection=Projection(files=(FileCredential(str(sock_path), "s"),)))
        with pytest.raises(SandboxPolicyError, match="regular file"):
            check_spec(spec, host)
    plain = _secret_file(Path(host.environ["XDG_RUNTIME_DIR"]) / "podman" / "token")
    spec = _launch(projection=Projection(files=(FileCredential(plain, "t"),)))
    with pytest.raises(SandboxPolicyError, match="XDG_RUNTIME_DIR"):
        check_spec(spec, host)
    for path in ("/run/docker.sock", "/var/run/docker.sock", "/run/user/1000/podman/podman.sock"):
        spec = _launch(projection=Projection(files=(FileCredential(path, "s"),)))
        with pytest.raises(SandboxPolicyError):
            check_spec(spec, host)


def test_a_link_anywhere_in_a_bind_source_is_refused(tmp_path, credential, staging, host):
    """A bind follows a link, so what it names could change after the check: a link to a
    credential, a link in a directory above it, and a link to the staging root are refused."""
    link = tmp_path / "link-to-key"
    link.symlink_to(credential)
    linked_dir = tmp_path / "linked-dir"
    linked_dir.symlink_to(Path(credential).parent)
    ssh = _secret_file(Path(host.home) / ".ssh" / "id_rsa", SENTINEL_GH, 0o600)
    to_ssh = tmp_path / "innocent"
    to_ssh.symlink_to(ssh)
    for source in (str(link), str(linked_dir / "key"), str(to_ssh)):
        spec = _launch(projection=Projection(files=(FileCredential(source, "key"),)))
        with pytest.raises(SandboxPolicyError, match="symbolic link"):
            check_spec(spec, host)
    staged_link = tmp_path / "staged"
    staged_link.symlink_to(staging)
    with pytest.raises(SandboxPolicyError, match="symbolic link"):
        check_spec(_launch(source=str(staged_link)), host)


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_only_a_regular_file_is_a_credential(kind, tmp_path, host):
    path = tmp_path / "private" / "item"
    path.parent.mkdir(mode=0o700)
    if kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    spec = _launch(projection=Projection(files=(FileCredential(str(path), "k"),)))
    with pytest.raises(SandboxPolicyError, match="regular file"):
        check_spec(spec, host)


def test_a_credential_file_the_agent_could_not_read_is_refused_not_delivered(tmp_path, host):
    """The agent runs as a subordinate UID: a 0600 file is unreadable inside."""
    source = _secret_file(tmp_path / "private" / "key", mode=0o600)
    spec = _launch(projection=Projection(files=(FileCredential(source, "k"),)))
    with pytest.raises(SandboxPolicyError, match="unreadable inside"):
        check_spec(spec, host)


def test_a_credential_file_every_local_user_can_read_is_refused(host):
    base = Path(tempfile.gettempdir())
    chain = [base, *base.parents]
    if not all(os.stat(p).st_mode & stat.S_IXOTH for p in chain):
        pytest.skip(f"{base} is not searchable by others here")
    directory = Path(tempfile.mkdtemp(prefix="af-270-exposed-"))
    try:
        directory.chmod(0o755)
        source = _secret_file(directory / "key")
        spec = _launch(projection=Projection(files=(FileCredential(source, "k"),)))
        with pytest.raises(SandboxPolicyError, match="every local user can read"):
            check_spec(spec, host)
    finally:
        for child in directory.iterdir():
            child.unlink()
        directory.rmdir()


def test_a_credential_file_another_user_owns_is_refused(credential, host):
    other = dataclasses.replace(host, uid=host.uid + 1)
    spec = _launch(projection=Projection(files=(FileCredential(credential, "k"),)))
    with pytest.raises(SandboxPolicyError, match="does not own"):
        check_spec(spec, other)


@pytest.mark.parametrize(
    "target",
    ["/etc/passwd", "../outside", "a/../../b", "", ".", "a/./b", "a/", "a,b", 'a"b', "a\nb"],
)
def test_a_credential_file_lands_only_at_a_clean_path_in_the_home(target, credential, host):
    spec = _launch(projection=Projection(files=(FileCredential(credential, target),)))
    with pytest.raises(SandboxPolicyError):
        check_spec(spec, host)


def test_a_bind_source_that_would_end_a_mount_field_is_refused(tmp_path, host):
    directory = tmp_path / "a,readonly=false"
    directory.mkdir(mode=0o700)
    source = _secret_file(directory / "key")
    spec = _launch(projection=Projection(files=(FileCredential(source, "k"),)))
    with pytest.raises(SandboxPolicyError, match="end the mount's field"):
        check_spec(spec, host)
    with pytest.raises(SandboxPolicyError, match="end the mount's field"):
        check_spec(_launch(source=str(directory)), host)


def test_two_credential_files_never_overlap(tmp_path, credential, host):
    second = _secret_file(Path(credential).parent / "other")
    files = (FileCredential(credential, "a/key"), FileCredential(second, "a/key"))
    with pytest.raises(SandboxPolicyError, match="overlap"):
        check_spec(_launch(projection=Projection(files=files)), host)


def test_the_staging_root_is_bound_only_where_d12_admits_it(staging, credential, host):
    with pytest.raises(SandboxPolicyError, match="copy-in form binds nothing"):
        check_spec(_launch(source=staging, staging=StagingMethod.COPY), host)
    exporter = ContainerSpec(
        ContainerRole.EXPORTER, ContainerNetwork.NONE, "af-vol-1", source=staging
    )
    with pytest.raises(SandboxPolicyError, match="exporter binds nothing"):
        check_spec(exporter, host)
    with pytest.raises(SandboxPolicyError, match="is a directory"):
        check_spec(_launch(source=credential), host)
    cache_on_helper = ContainerSpec(
        ContainerRole.STAGER, ContainerNetwork.NONE, "af-vol-1", cache_volume="af-cache"
    )
    with pytest.raises(SandboxPolicyError, match="only a launch container mounts the cache"):
        check_spec(cache_on_helper, host)


# -- the rootless refusal (D10) ---------------------------------------------------------


@pytest.mark.parametrize(
    ("report", "refused"),
    [
        (RuntimeReport(BackendKind.CONTAINER, "docker", "27.0", False), True),
        (RuntimeReport(BackendKind.CONTAINER, "podman", "5.4.2", None), True),
        (RuntimeReport(BackendKind.CONTAINER, "podman", "5.4.2", True), False),
        (RuntimeReport(BackendKind.BWRAP), False),
    ],
)
def test_a_runtime_that_is_not_rootless_is_refused(report, refused):
    refusal = runtime_refusal(report)
    assert (refusal is not None) is refused
    if refusal is not None:
        assert isinstance(refusal, SandboxCapabilityError)
        assert refusal.capability == "rootless" and "ADR 0013 D10" in str(refusal)


# -- the judge of a measurement (D10; D16 items 4, 5, 7) ------------------------------

# What a rootless runtime enforcing the policy shows to the agent (subordinate
# range 100000 for 65536, the operator UID 1000). Recorded in the formats the
# kernel prints, abbreviated to the lines the judge reads.
STATUS = (
    "Name:\tpython3\n"
    f"Uid:\t{AGENT_UID}\t{AGENT_UID}\t{AGENT_UID}\t{AGENT_UID}\n"
    f"Gid:\t{AGENT_GID}\t{AGENT_GID}\t{AGENT_GID}\t{AGENT_GID}\n"
    "CapInh:\t0000000000000000\nCapPrm:\t0000000000000000\nCapEff:\t0000000000000000\n"
    "CapBnd:\t0000000000000000\nCapAmb:\t0000000000000000\n"
    "NoNewPrivs:\t1\nSeccomp:\t2\nSeccomp_filters:\t1\n"
)
UID_MAP = "         0       1000          1\n         1     100000      65536\n"
MOUNTINFO = "\n".join(
    [
        "500 400 0:50 / / ro,relatime - overlay overlay rw,lowerdir=/x",
        "501 500 0:51 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw",
        "502 500 0:52 / /dev rw,nosuid - tmpfs tmpfs rw,size=65536k,mode=755",
        "503 502 0:53 / /dev/shm rw,nosuid,nodev,noexec - tmpfs shm rw,size=65536k",
        "504 500 0:54 / /sys ro,nosuid,nodev,noexec - sysfs sysfs ro",
        "505 504 0:55 / /sys/fs/cgroup ro - cgroup2 cgroup2 rw",
        "506 500 8:1 /home/op/.local/share/docker/c/hosts /etc/hosts rw - ext4 /dev/sda1",
        "507 500 0:56 / /tmp rw,nosuid,nodev - tmpfs tmpfs rw,mode=1777",
        "508 500 0:57 / /home/agent rw,nosuid,nodev - tmpfs tmpfs rw,mode=700,uid=10001",
        "509 500 0:58 / /workspace rw - ext4 /dev/sda1 rw",
        "510 500 8:1 /code/.git/autoforge/sandbox/k/stage/1 /source ro - ext4 /dev/sda1 rw",
        r"511 501 0:59 / /proc/acpi ro,relatime - tmpfs tmpfs\040x ro",
    ]
)
WRITABLE = {
    "/": False,
    "/etc": False,
    "/usr": False,
    "/var/tmp": False,
    "/run": False,
    "/source": False,
    "/dev/shm": True,
    WORKSPACE_TARGET: True,
    HOME_TARGET: True,
    TMP_TARGET: True,
}
GOOD = {
    "status": STATUS,
    "uid_map": "         0          0          1\n         1          1      65536\n",
    HOST_UID_MAP: UID_MAP,
    "mountinfo": MOUNTINFO,
    "pids_max": f"{PIDS_LIMIT}\n",
    "writable": WRITABLE,
    "connect": [
        {"host": "127.0.0.1", "port": 41000, "result": "ECONNREFUSED"},
        {"host": "10.0.2.2", "port": 41000, "result": "connected", "received": "SSH-2.0-x"},
    ],
    "abstract": {"result": "ECONNREFUSED"},
    "keyring": {"session": "EPERM", "user": "EPERM", "user-session": "EPERM"},
    "interfaces": ["eth0", "lo"],
    "routes": ["ipv4 via eth0"],
    "sentinels": {"0": ["env:MODEL_API_KEY"], "1": []},
}


def _expect(**changes) -> Expectation:
    spec = ContainerSpec(
        ContainerRole.LAUNCH,
        ContainerNetwork.BRIDGE,
        "af-vol-1",
        source="/code/.git/autoforge/sandbox/k/stage/1",
    )
    expect = Expectation.for_spec(
        spec,
        operator_uid=1000,
        subordinate=((100000, 65536),),
        loopback_nonce="nonce-loop-1",
        abstract_nonce="nonce-abstract-1",
        keyring_checked=True,
        sentinels=(frozenset({"env:MODEL_API_KEY"}), frozenset()),
    )
    return dataclasses.replace(expect, **changes)


def test_a_container_that_shows_every_item_has_no_finding():
    assert judge(json.dumps(GOOD), _expect()) == ()


def _status(**fields) -> str:
    lines = []
    for line in STATUS.splitlines():
        key = line.split(":", 1)[0]
        lines.append(f"{key}:\t{fields[key]}" if key in fields else line)
    return "\n".join(lines)


@pytest.mark.parametrize(
    ("change", "capability"),
    [
        ({HOST_UID_MAP: "0 0 4294967295\n"}, {"ROOTLESS", "AGENT_USER"}),
        ({HOST_UID_MAP: "0 1000 1\n1 100000 65536\n70000 0 1\n"}, "ROOTLESS"),
        # Podman's keep-id, with the agent's UID as the kept one.
        (
            {HOST_UID_MAP: "0 100000 10001\n10001 1000 1\n10002 110001 55534\n"},
            ContainerCapability.AGENT_USER,
        ),
        ({HOST_UID_MAP: "0 1000 1\n"}, ContainerCapability.AGENT_USER),
        ({HOST_UID_MAP: "0 1000 1\n1 5000 65536\n"}, ContainerCapability.AGENT_USER),
        ({HOST_UID_MAP: ""}, ContainerCapability.AGENT_USER),
        # Inside, the map is relative to the parent namespace: never read as the host's.
        ({"uid_map": "0 1000 1\n10001 1000 1\n"}, set()),
        ({"status": _status(Uid="0\t0\t0\t0")}, ContainerCapability.AGENT_USER),
        ({"status": _status(CapEff="00000000a80425fb")}, ContainerCapability.NO_CAPABILITIES),
        ({"status": _status(CapBnd="000001ffffffffff")}, ContainerCapability.NO_CAPABILITIES),
        ({"status": _status(NoNewPrivs="0")}, ContainerCapability.NO_NEW_PRIVILEGES),
        ({"status": _status(Seccomp="0")}, ContainerCapability.SECCOMP),
        (
            {"status": ""},
            {"AGENT_USER", "NO_CAPABILITIES", "NO_NEW_PRIVILEGES", "SECCOMP"},
        ),
        (
            {"mountinfo": MOUNTINFO.replace("/ / ro,relatime", "/ / rw,relatime")},
            ContainerCapability.READ_ONLY_ROOT,
        ),
        (
            {"mountinfo": MOUNTINFO + "\n600 500 0:60 / /run/secrets ro - tmpfs t ro"},
            ContainerCapability.MOUNTS,
        ),
        (
            {"mountinfo": MOUNTINFO + "\n601 500 8:1 /home/op /mnt/home ro - ext4 /dev/sda1 rw"},
            ContainerCapability.MOUNTS,
        ),
        (
            {"mountinfo": MOUNTINFO.replace(" /source ro", " /elsewhere ro")},
            ContainerCapability.MOUNTS,
        ),
        ({"pids_max": "max\n"}, ContainerCapability.PID_LIMIT),
        ({"pids_max": ""}, ContainerCapability.PID_LIMIT),
        ({"writable": {**WRITABLE, "/var/tmp": True}}, ContainerCapability.WRITABLE_PLACES),
        ({"writable": {**WRITABLE, "/source": True}}, ContainerCapability.WRITABLE_PLACES),
        ({"writable": {**WRITABLE, WORKSPACE_TARGET: False}}, ContainerCapability.WRITABLE_PLACES),
        ({"keyring": {"session": "found"}}, ContainerCapability.SESSION_KEYRING),
        (
            {"keyring": {"session": "unknown architecture mips"}},
            ContainerCapability.SESSION_KEYRING,
        ),
        ({"keyring": {}}, ContainerCapability.SESSION_KEYRING),
        (
            {
                "connect": [
                    {
                        "host": "::1",
                        "port": 41000,
                        "result": "connected",
                        "received": "nonce-loop-1",
                    }
                ]
            },
            ContainerCapability.HOST_LOOPBACK,
        ),
        ({"connect": []}, ContainerCapability.HOST_LOOPBACK),
        (
            {"abstract": {"result": "connected", "received": "nonce-abstract-1"}},
            ContainerCapability.HOST_ABSTRACT_SOCKET,
        ),
        ({"abstract": None}, ContainerCapability.HOST_ABSTRACT_SOCKET),
        ({"sentinels": {"0": ["env:MODEL_API_KEY"], "1": ["/workspace/x"]}}, "CREDENTIALS"),
        ({"sentinels": {"0": ["env:MODEL_API_KEY", "env:OTHER"]}}, "CREDENTIALS"),
        ({"sentinels": {"0": []}}, "CREDENTIALS"),
        ({"sentinels": None}, "CREDENTIALS"),
    ],
)
def test_each_item_the_container_does_not_show_is_named(change, capability):
    observation = {**GOOD, **change}
    findings = judge(observation, _expect())
    if isinstance(capability, ContainerCapability):
        expected = {capability}
    elif isinstance(capability, str):
        expected = {ContainerCapability[capability]}
    else:
        expected = {ContainerCapability[name] for name in capability}
    assert {finding.capability for finding in findings} == expected, findings
    for finding in findings:
        assert finding.describe().startswith(finding.capability.value + ": ")


def test_under_network_none_only_loopback_and_no_route_pass():
    offline = _expect(network=ContainerNetwork.NONE)
    clean = {**GOOD, "interfaces": ["lo"], "routes": []}
    assert judge(clean, offline) == ()
    for change in ({"interfaces": ["lo", "eth0"]}, {"routes": ["ipv4 via tap0"]}):
        findings = judge({**clean, **change}, offline)
        assert {f.capability for f in findings} == {ContainerCapability.NO_ROUTE}


def test_an_observation_that_does_not_parse_proves_nothing():
    findings = judge("Traceback (most recent call last):", _expect())
    assert {f.capability for f in findings} == set(ContainerCapability)


def test_the_expectation_follows_the_spec(credential):
    exporter = ContainerSpec(ContainerRole.EXPORTER, ContainerNetwork.NONE, "af-vol-1")
    expect = Expectation.for_spec(exporter, operator_uid=1000, subordinate=())
    assert WORKSPACE_TARGET not in expect.writable and WORKSPACE_TARGET in expect.mounts
    launch = _launch(
        cache_volume="af-cache",
        projection=Projection(files=(FileCredential(credential, ".config/p/key"),)),
    )
    expect = Expectation.for_spec(launch, operator_uid=1000, subordinate=())
    assert CACHE_TARGET in expect.writable and f"{HOME_TARGET}/.config/p/key" in expect.mounts


def test_the_operators_subordinate_ranges_are_read_by_name_or_uid():
    text = "root:100000:65536\nruoshi:165536:65536\n1000:300000:10\nbad line\nx:1:y\n"
    assert subordinate_ranges(text, "ruoshi", 1000) == ((165536, 65536), (300000, 10))
    assert subordinate_ranges(text, "nobody", 4242) == ()


# -- the measurement, on this host as its own negative control --------------------------


def _listen(nonce: bytes) -> tuple[socket.socket, int]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(4)

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                conn.sendall(nonce)

    threading.Thread(target=serve, daemon=True).start()
    return server, server.getsockname()[1]


def _abstract(name: str, nonce: bytes) -> socket.socket:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind("\0" + name)
    server.listen(4)

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                conn.sendall(nonce)

    threading.Thread(target=serve, daemon=True).start()
    return server


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads Linux's /proc")
def test_the_measurement_on_the_host_itself_is_refused_item_by_item(tmp_path):
    """Run as a plain host process, the measurement must fail the judge: the host's
    loopback listener and abstract socket answer, the UID is the operator's, and a fake
    credential outside the projection is found. So a container that leaked the same would
    be named too."""
    nonce = f"nonce-loop-{os.getpid()}"
    abstract_name = f"autoforge-test-270-{os.getpid()}"
    listener, port = _listen(nonce.encode())
    abstract = _abstract(abstract_name, b"nonce-abstract-host")
    scan = tmp_path / "scan"
    (scan / "deep").mkdir(parents=True)
    (scan / "deep" / "hosts.yml").write_text(f"oauth_token: {SENTINEL_GH}\n")
    writable = tmp_path / "writable"
    writable.mkdir()
    spec = {
        "places": [str(writable), "/proc"],
        "connect_hosts": ["127.0.0.1"],
        "connect_ports": [port],
        "abstract": abstract_name,
        "sentinels": [SENTINEL_ENV, SENTINEL_GH],
        "scan_roots": [str(scan)],
        "scan_skip": [],
    }
    env = {"PATH": os.environ.get("PATH", "/usr/bin"), "MODEL_API_KEY": SENTINEL_ENV}
    try:
        done = subprocess.run(
            [sys.executable, "-I", str(MEASURE), json.dumps(spec)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    finally:
        listener.close()
        abstract.close()
    observation = json.loads(done.stdout)
    assert SENTINEL_ENV not in done.stdout and SENTINEL_GH not in done.stdout
    assert observation["writable"] == {str(writable): True, "/proc": False}
    assert observation["sentinels"] == {
        "0": ["env:MODEL_API_KEY"],
        "1": [str(scan / "deep" / "hosts.yml")],
    }
    expect = dataclasses.replace(
        _expect(),
        loopback_nonce=nonce,
        abstract_nonce="nonce-abstract-host",
        keyring_checked=False,
        operator_uid=os.getuid(),
    )
    found = {finding.capability for finding in judge(observation, expect)}
    assert {
        ContainerCapability.HOST_LOOPBACK,
        ContainerCapability.HOST_ABSTRACT_SOCKET,
        ContainerCapability.AGENT_USER,
        ContainerCapability.CREDENTIALS,
    } <= found


# Spellings only a container runtime's create or exec takes.
_RUNTIME_FLAGS = (
    "--cap-drop",
    "--security-opt",
    "--read-only-tmpfs",
    "--pids-limit",
    "--http-proxy",
    "--pull=",
    "type=volume",
)


def test_only_the_container_module_writes_a_runtime_flag():
    """As no module but sandbox_bwrap.py knows a bwrap flag: the container's flags are
    written in sandbox_container.py alone (ADR 0013 D13's placement)."""
    writers = sorted(
        path.name
        for path in (REPO / "src" / "autoforge").glob("*.py")
        if any(flag in path.read_text(encoding="utf-8") for flag in _RUNTIME_FLAGS)
    )
    assert writers == ["sandbox_container.py"]


def test_the_matrix_maps_every_resource_of_adr_0005():
    """Each resource row of ADR 0005 §2.5 has its row, verbatim, in the container
    capability matrix of github-safety.md: an enforced mechanism or a refusal."""

    def first_cells(text: str, header: str) -> list[str]:
        lines = text[text.index(header) :].splitlines()[2:]
        cells = []
        for line in lines:
            if not line.startswith("|"):
                break
            cells.append(line.split("|")[1].strip())
        return cells

    adr = REPO / "docs" / "adr" / "0005-outer-sandbox-and-host-resource-capability-model.md"
    resources = first_cells(adr.read_text(encoding="utf-8"), "| Resource | REMOTE agent |")
    guide = (REPO / "docs" / "agent-guides" / "github-safety.md").read_text(encoding="utf-8")
    matrix = first_cells(guide, "| Resource (ADR 0005 §2.5) | Container launch |")
    assert len(resources) == 28
    assert matrix == resources
    section = guide[guide.index("### Container launch threat model") :]
    section = " ".join(section[: section.index("\n## ")].split())
    for needed in ("R3:", "R4:", "does not restrict traffic to model endpoints", "#294"):
        assert needed in section


def test_the_measurement_adds_each_default_gateway_to_its_attempts(monkeypatch):
    """The gateway is where a rootless network helper could lead to host loopback."""
    word = int.from_bytes(socket.inet_aton("10.0.2.1"), sys.byteorder)
    files = {
        "/proc/net/route": (
            "Iface\tDestination\tGateway\tFlags\n"
            f"eth0\t00000000\t{word:08X}\t0003\n"
            f"eth0\t0002000A\t00000000\t0001\n"
        ),
        "/proc/net/ipv6_route": (
            f"{'0' * 32} 00 {'0' * 32} 00 fe80{'0' * 27}1 00000400 00000001 0 00000003 eth0\n"
        ),
    }
    monkeypatch.setattr(container_measure, "_read", lambda path: files.get(path, ""))
    assert container_measure._gateways() == ["10.0.2.1", "fe80::1"]


def test_the_measurement_imports_nothing_of_autoforge():
    """The image carries the file alone (#271): only the standard library."""
    source = MEASURE.read_text(encoding="utf-8")
    assert "autoforge" not in "".join(
        line for line in source.splitlines() if line.startswith(("import ", "from "))
    )
    assert container_measure.main(["measure", "[]"]) == 2
