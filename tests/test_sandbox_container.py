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
import errno
import io
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
    HOST_ALIASES,
    HOST_UID_MAP,
    LOOPBACK_ADDRESSES,
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
    StagingRoot,
    check_spec,
    client_environment,
    create_argv,
    exec_argv,
    judge,
    loopback_routes,
    network_for,
    subordinate_ranges,
)
from autoforge.sandbox_policy import SANDBOX_DIR

REPO = Path(__file__).resolve().parent.parent
MEASURE = REPO / "src" / "autoforge" / "container_measure.py"
# Fake credentials: never real, and shaped so that a leak is unmistakable.
SENTINEL_ENV = "fake-model-key-270-env-0f3a9c"
SENTINEL_FILE = "fake-model-key-270-file-7b1e44"
SENTINEL_GH = "fake-github-token-270-d2c8"
IMAGE = "sha256:" + "a" * 64
LABELS = {"io.autoforge.run": "run-1", "io.autoforge.nonce": "n-1"}


LAUNCH_ID = "ANALYZE_EXECUTE-1"


@pytest.fixture
def common(tmp_path: Path) -> Path:
    """The controller's git common dir, inside its checkout as a main checkout's is."""
    path = tmp_path / "repo" / ".git"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def host(tmp_path: Path, common: Path) -> HostContext:
    """A fake operator home, so D4.3's forbidden sources are this test's own, and the
    controller's common dir, checkout and a worktree of it."""
    home = tmp_path / "home"
    home.mkdir()
    runtime_dir = tmp_path / "xdg-runtime"
    runtime_dir.mkdir(mode=0o700)
    worktree = common / "autoforge" / "worktrees" / "270"
    worktree.mkdir(parents=True)
    (common / "autoforge").chmod(0o700)  # as safefs makes it, whatever the umask
    checkouts = (str(common.parent), str(worktree))
    return HostContext(
        str(home), {"XDG_RUNTIME_DIR": str(runtime_dir)}, os.getuid(), str(common), checkouts
    )


def _stage(common: Path, key: str = "270", launch: str = LAUNCH_ID) -> StagingRoot:
    """A staging root as the controller makes one: safefs's 0700 directories, the root
    itself readable by the agent's subordinate UID."""
    root = StagingRoot(str(common), key, launch)
    current = common
    for part in root.layout.split("/"):
        current = current / part
        current.mkdir(exist_ok=True)
        current.chmod(0o700)
    current.chmod(0o755)
    return root


@pytest.fixture
def staging(common: Path) -> StagingRoot:
    return _stage(common)


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


def _every_spec(
    staging: StagingRoot, credential: str
) -> Iterator[tuple[RuntimeProduct, ContainerSpec]]:
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
                source=staging if bound else None,
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
                f"--mount=type=bind,source={staging.path},target={SOURCE_TARGET},readonly=true"
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


def test_the_home_itself_or_an_ancestor_is_never_a_bind_source(host, staging):
    """D4.3 still applies to the staging root: never the home or one of its ancestors."""
    for home in (staging.path, f"{staging.path}/op"):
        with pytest.raises(SandboxPolicyError, match="operator's home"):
            check_spec(_launch(source=staging), dataclasses.replace(host, home=home))


# -- the staging root: the launch's own, and nothing else (D12) -----------------------


def test_the_launchs_own_staging_root_is_bound_read_only_at_source(staging, host, common):
    """The positive case: the StagingRoot of the launch, in the controller's common dir,
    in directories only the operator owns and can write, apart from every checkout (the
    main checkout holds the common dir, and so the root, which is how it must be)."""
    assert staging.path == f"{common}/{SANDBOX_DIR}/270/stage/{LAUNCH_ID}"
    assert common.parent.as_posix() in host.checkouts
    check_spec(_launch(source=staging), host)
    argv = _argv(_launch(source=staging), host)
    assert f"--mount=type=bind,source={staging.path},target={SOURCE_TARGET},readonly=true" in argv


@pytest.mark.parametrize("path", ["/usr", "/etc", "/", "checkout", "common", "admin", "staging"])
def test_a_path_alone_is_never_bound_at_source(path, host, common, staging):
    """D12: no directory reaches /source because it passes the generic checks, not /usr,
    /etc, the checkout, the common dir or a worktree's admin dir, and not even the staging
    root's own path: only the StagingRoot the engine builds."""
    admin = common / "worktrees" / "270"
    admin.mkdir(parents=True)
    named = {
        "checkout": str(common.parent),
        "common": str(common),
        "admin": str(admin),
        "staging": staging.path,
    }
    spec = _launch(source=named.get(path, path))
    with pytest.raises(SandboxPolicyError, match="a path alone is never bound"):
        check_spec(spec, host)


@pytest.mark.parametrize("other", ["/usr", "/etc", "/", "elsewhere", "admin"])
def test_a_staging_root_outside_the_controllers_common_dir_is_refused(
    other, host, common, tmp_path
):
    """A StagingRoot in another directory, a worktree's admin dir as the "common dir"
    included, is not the launch's: refused even where its layout exists."""
    places = {"elsewhere": tmp_path / "elsewhere", "admin": common / "worktrees" / "270"}
    base = places.get(other, Path(other))
    if other in places:
        base.mkdir(parents=True)
        _stage(base)
    with pytest.raises(SandboxPolicyError, match="not in the controller's own git common dir"):
        check_spec(_launch(source=StagingRoot(str(base), "270", LAUNCH_ID)), host)


def test_a_staging_root_needs_the_controllers_common_dir_to_be_known(staging, host):
    unknown = dataclasses.replace(host, git_common_dir="")
    with pytest.raises(SandboxPolicyError, match="no controller git common dir"):
        check_spec(_launch(source=staging), unknown)


@pytest.mark.parametrize(
    ("key", "launch"),
    [
        ("..", LAUNCH_ID),
        (".", LAUNCH_ID),
        ("270/../../..", LAUNCH_ID),
        ("", LAUNCH_ID),
        ("270", ".."),
        ("270", "../../../worktrees/270"),
        ("270", "a/b"),
        ("270", ""),
        ("270", "-x"),
    ],
)
def test_a_staging_roots_key_and_launch_are_one_component_each(key, launch, host, common):
    """No name climbs out of the layout, towards the common dir or a worktree's admin dir."""
    with pytest.raises(SandboxPolicyError, match="not one path component"):
        check_spec(_launch(source=StagingRoot(str(common), key, launch)), host)


def test_the_probes_directory_is_never_a_launchs_key(host, common):
    with pytest.raises(SandboxPolicyError, match="probes' directory"):
        check_spec(_launch(source=_stage(common, key="probe")), host)


def test_a_link_in_the_staging_roots_layout_is_refused(host, common):
    """A link anywhere from the common dir down: here ``stage`` leads to the common dir's
    worktree admin directories, so the "staging root" would be a worktree's admin dir."""
    admin = common / "worktrees" / LAUNCH_ID
    admin.mkdir(parents=True)
    issue = common / "autoforge" / "sandbox" / "270"
    issue.mkdir(parents=True)
    for directory in (issue, issue.parent, admin, admin.parent):
        directory.chmod(0o700)
    (issue / "stage").symlink_to(common / "worktrees")
    root = StagingRoot(str(common), "270", LAUNCH_ID)
    assert os.path.isdir(root.path)
    with pytest.raises(SandboxPolicyError, match="symbolic link"):
        check_spec(_launch(source=root), host)


@pytest.mark.parametrize("part", ["autoforge", "sandbox", "270", "stage", LAUNCH_ID])
def test_a_staging_root_another_user_can_write_is_refused(part, staging, host, common):
    """Every directory of the layout is the controller's: one that a group or others can
    write could have its contents replaced."""
    path = common
    for name in staging.layout.split("/"):
        path = path / name
        if name == part:
            break
    path.chmod(0o777 if part == LAUNCH_ID else 0o770)
    with pytest.raises(SandboxPolicyError, match="another user can write"):
        check_spec(_launch(source=staging), host)


def test_a_staging_root_the_operator_does_not_own_is_refused(staging, host):
    with pytest.raises(SandboxPolicyError, match="does not own"):
        check_spec(_launch(source=staging), dataclasses.replace(host, uid=host.uid + 1))


def test_a_staging_root_that_is_missing_or_not_a_directory_is_refused(host, common):
    with pytest.raises(SandboxPolicyError, match="cannot be checked"):
        check_spec(_launch(source=StagingRoot(str(common), "270", LAUNCH_ID)), host)
    root = _stage(common)
    Path(root.path).rmdir()
    Path(root.path).write_text("not a directory")
    with pytest.raises(SandboxPolicyError, match="not a directory"):
        check_spec(_launch(source=root), host)


@pytest.mark.parametrize("where", ["the root", "inside it", "around it"])
def test_a_staging_root_never_holds_or_lies_in_a_checkout_or_worktree(where, staging, host):
    """The checkout and every worktree are never bound: a staging root that is one, holds
    one, or lies in a worktree kept inside the common dir is refused."""
    worktree = {
        "the root": staging.path,
        "inside it": f"{staging.path}/wt",
        "around it": staging.path.rsplit("/", 2)[0],
    }[where]
    hosting = dataclasses.replace(host, checkouts=(*host.checkouts, worktree))
    with pytest.raises(SandboxPolicyError, match="checkout or worktree"):
        check_spec(_launch(source=staging), hosting)


def test_the_staging_layout_is_the_controllers_sandbox_directory(staging):
    """The staging root's layout is ADR 0005 D8.4's: under ``sandbox_policy.SANDBOX_DIR``."""
    assert staging.layout.startswith(SANDBOX_DIR + "/")
    assert staging.layout == f"{SANDBOX_DIR}/270/stage/{LAUNCH_ID}"


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
    staged_link.symlink_to(Path(staging.path).parent.parent)
    linked = StagingRoot(str(staged_link), "270", LAUNCH_ID)
    with pytest.raises(SandboxPolicyError, match="symbolic link"):
        check_spec(
            _launch(source=linked), dataclasses.replace(host, git_common_dir=str(staged_link))
        )


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


@pytest.fixture
def exposed() -> Iterator[Path]:
    """A directory in the system temp dir, none of whose ancestors is private to the operator."""
    base = Path(tempfile.gettempdir())
    for parent in (base, *base.parents):
        info = os.stat(parent)
        if info.st_uid == os.getuid() and not info.st_mode & (stat.S_IXGRP | stat.S_IXOTH):
            pytest.skip(f"{parent} is private to this user here")
    directory = Path(tempfile.mkdtemp(prefix="af-270-exposed-"))
    try:
        yield directory
    finally:
        for child in directory.iterdir():
            child.unlink()
        directory.rmdir()


@pytest.mark.parametrize("mode", [0o755, 0o751, 0o750, 0o710])
def test_a_credential_file_other_local_users_can_reach_is_refused(mode, exposed, host):
    """Every local user (``o+x``), or every member of the directory's group (``g+x``), can
    reach a 0644 file whose directories are all searchable to them: refused."""
    exposed.chmod(mode)
    source = _secret_file(exposed / "key")
    spec = _launch(projection=Projection(files=(FileCredential(source, "k"),)))
    with pytest.raises(SandboxPolicyError, match="other local users can reach this file"):
        check_spec(spec, host)


def test_a_credential_file_under_a_directory_only_the_operator_can_enter_is_accepted(exposed, host):
    exposed.chmod(0o700)
    source = _secret_file(exposed / "key")
    check_spec(_launch(projection=Projection(files=(FileCredential(source, "k"),))), host)


def test_a_credential_file_with_another_hard_link_is_refused(credential, tmp_path, host):
    """Another link could sit in a directory others can search."""
    os.link(credential, tmp_path / "elsewhere")
    spec = _launch(projection=Projection(files=(FileCredential(credential, "k"),)))
    with pytest.raises(SandboxPolicyError, match="another hard link"):
        check_spec(spec, host)


@pytest.mark.parametrize(
    ("listed", "accepted"),
    [
        (["system.nfs4_acl"], False),
        (["system.richacl"], False),
        (OSError(errno.EIO, "I/O error"), False),
        (["system.posix_acl_access", "user.note"], True),
        (OSError(errno.ENOTSUP, "not supported"), True),
    ],
)
def test_an_acl_the_mode_does_not_show_makes_a_directory_not_private(
    listed, accepted, credential, host, monkeypatch
):
    """A POSIX ACL's named entries are bounded by its mask, the mode's group bits, which a
    private directory has no search in; an NFSv4 or rich ACL is not, and a directory whose
    ACLs cannot be read is not private either."""

    def listxattr(path, *, follow_symlinks=True):
        if isinstance(listed, OSError):
            raise listed
        return listed

    monkeypatch.setattr(os, "listxattr", listxattr, raising=False)
    spec = _launch(projection=Projection(files=(FileCredential(credential, "k"),)))
    if accepted:
        check_spec(spec, host)
    else:
        with pytest.raises(SandboxPolicyError, match="other local users can reach this file"):
            check_spec(spec, host)


def test_a_directorys_acls_unreadable_on_this_platform_make_it_not_private(
    credential, host, monkeypatch
):
    """Without ``os.listxattr`` (not Linux) a directory's ACLs cannot be read, so whether
    another user can enter it is not established: refused."""
    monkeypatch.delattr(os, "listxattr", raising=False)
    spec = _launch(projection=Projection(files=(FileCredential(credential, "k"),)))
    with pytest.raises(SandboxPolicyError, match="other local users can reach this file"):
        check_spec(spec, host)


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
    comma = StagingRoot(str(Path(host.git_common_dir)), "270", "a,readonly=false")
    with pytest.raises(SandboxPolicyError, match="not one path component"):
        check_spec(_launch(source=comma), host)


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
    with pytest.raises(SandboxPolicyError, match="a path alone is never bound"):
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
CID = "c0ffee" + "0" * 58
OTHER_CID = "bad" + "0" * 61
USERDATA = f"/containers/overlay-containers/{CID}/userdata"
STAGED = f"/code/.git/{SANDBOX_DIR}/270/stage/{LAUNCH_ID}"
VOLUME_DATA = "/op/.local/share/containers/storage/volumes/af-vol-1/_data"
# A rootless Podman container as the policy creates it: the overlay root
# read-only; its own procfs, sysfs and /dev, with the runtime's masks (a
# read-only view of /proc/bus, the null device over /proc/kcore, an empty
# read-only tmpfs over /proc/acpi and /sys/firmware) and the host's device
# nodes it binds; its own files from its per-container storage; the policy's
# tmpfs, volume and read-only bind.
MOUNTINFO = "\n".join(
    [
        "500 400 0:50 / / ro,relatime - overlay overlay rw,lowerdir=/x",
        "501 500 0:51 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw",
        "502 500 0:52 / /dev rw,nosuid - tmpfs tmpfs rw,size=65536k,mode=755",
        "503 502 0:53 / /dev/pts rw,nosuid,noexec,relatime - devpts devpts rw,mode=620",
        "504 502 0:54 / /dev/mqueue rw,nosuid,nodev,noexec,relatime - mqueue mqueue rw",
        "505 502 0:55 / /dev/shm rw,nosuid,nodev,noexec - tmpfs shm rw,size=65536k",
        "506 502 0:5 /null /dev/null rw,nosuid,noexec - devtmpfs udev rw,size=8g",
        "507 502 0:5 /urandom /dev/urandom rw,nosuid,noexec - devtmpfs udev rw,size=8g",
        "508 500 0:56 / /sys ro,nosuid,nodev,noexec - sysfs sysfs ro",
        "509 508 0:57 / /sys/fs/cgroup ro,nosuid,nodev,noexec - cgroup2 cgroup2 rw",
        "510 508 0:58 / /sys/firmware ro,relatime - tmpfs tmpfs ro",
        f"511 500 0:59 {USERDATA}/hosts /etc/hosts rw,nosuid,nodev - tmpfs tmpfs rw",
        f"512 500 0:59 {USERDATA}/resolv.conf /etc/resolv.conf rw,nosuid,nodev - tmpfs tmpfs rw",
        f"513 500 0:59 {USERDATA}/.containerenv /run/.containerenv rw - tmpfs tmpfs rw",
        "514 500 0:60 / /tmp rw,nosuid,nodev - tmpfs tmpfs rw,mode=1777",
        "515 500 0:61 / /home/agent rw,nosuid,nodev - tmpfs tmpfs rw,mode=700,uid=110001",
        f"516 500 8:2 {VOLUME_DATA} /workspace rw,relatime - ext4 /dev/sda2 rw",
        f"517 500 8:1 {STAGED} /source ro,relatime - ext4 /dev/sda1 rw",
        "518 501 0:51 /bus /proc/bus ro,nosuid,nodev,noexec,relatime - proc proc rw",
        "519 501 0:5 /null /proc/kcore rw,nosuid - devtmpfs udev rw",
        r"520 501 0:62 / /proc/acpi ro,relatime - tmpfs tmpfs\040x ro",
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
PORT = 41000
GATEWAYS = ["192.168.1.1", "fe80::1%eth0"]


def _tried(host: str, *attempts: dict, port: int = PORT) -> dict:
    return {"host": host, "port": port, "attempts": list(attempts)}


def _refused(address: str, result: str = "ECONNREFUSED") -> dict:
    return {"address": address, "result": result}


# Every route the judge requires for Podman, each conclusively unreachable: a
# refusal, no path, a name that does not exist, or another service that
# answered without the nonce and closed.
CONNECT = [
    _tried("127.0.0.1", _refused("127.0.0.1")),
    _tried("::1", _refused("::1", "EADDRNOTAVAIL")),
    _tried("host.containers.internal", _refused("169.254.1.2")),
    {"host": "host.docker.internal", "port": PORT, "resolve_error": "EAI_NONAME"},
    _tried("192.168.1.1", _refused("192.168.1.1", "EHOSTUNREACH")),
    _tried(
        "fe80::1%eth0",
        {"address": "fe80::1%eth0", "result": "connected", "received": "SSH-2.0-x", "eof": True},
    ),
]
SCAN = {
    "roots": ["/"],
    "skip": ["/proc", "/sys", "/dev"],
    "files": 1200,
    "bytes": 52_000_000,
    "denied": 4,
    "special": 0,
    "incomplete": [],
    "incomplete_count": 0,
}
# What each tmpfs of its own held: the runtime's /dev and its device nodes,
# and every other one empty.
TMPFS = {
    "/dev": {"count": 6, "names": ["mqueue", "null", "pts", "shm", "urandom", "zero"]},
    "/dev/shm": {"count": 0, "names": []},
    "/sys/firmware": {"count": 0, "names": []},
    "/tmp": {"count": 0, "names": []},
    HOME_TARGET: {"count": 0, "names": []},
    "/proc/acpi": {"count": 0, "names": []},
}
GOOD = {
    "status": STATUS,
    "uid_map": "         0          0          1\n         1          1      65536\n",
    HOST_UID_MAP: UID_MAP,
    "mountinfo": MOUNTINFO,
    "pids_max": f"{PIDS_LIMIT}\n",
    "writable": WRITABLE,
    "tmpfs": TMPFS,
    "gateways": GATEWAYS,
    "connect": CONNECT,
    "abstract": {"result": "ECONNREFUSED"},
    "keyring": {"session": "EPERM", "user": "EPERM", "user-session": "EPERM"},
    "interfaces": ["eth0", "lo"],
    "routes": ["ipv4 via eth0"],
    "sentinels": {"0": ["env:MODEL_API_KEY"], "1": []},
    "scan": SCAN,
}


def _expect(**changes) -> Expectation:
    spec = ContainerSpec(
        ContainerRole.LAUNCH,
        ContainerNetwork.BRIDGE,
        "af-vol-1",
        source=StagingRoot("/code/.git", "270", LAUNCH_ID),
    )
    expect = Expectation.for_spec(
        spec,
        product=RuntimeProduct.PODMAN,
        operator_uid=1000,
        subordinate=((100000, 65536),),
        container_id=CID,
        loopback_nonce="nonce-loop-1",
        loopback_listeners=(("127.0.0.1", PORT), ("::1", PORT)),
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


def _mountinfo(point: str, line: str | None) -> str:
    """MOUNTINFO with the line at ``point`` replaced (``None`` drops it)."""
    lines = [entry for entry in MOUNTINFO.splitlines() if f" {point} " not in entry]
    return "\n".join(lines if line is None else [*lines, line])


def _connect(host: str, *attempts: dict, port: int = PORT) -> list:
    """CONNECT with ``host``'s entry replaced by these attempts (none drops it)."""
    kept = [entry for entry in CONNECT if entry["host"] != host]
    return kept + [_tried(host, *attempts, port=port)] if attempts else kept


def _connected(received: str, eof: bool, address: str = "127.0.0.1") -> dict:
    return {"address": address, "result": "connected", "received": received, "eof": eof}


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
        ({"keyring": {"session": "EPERM", "user": "EPERM"}}, ContainerCapability.SESSION_KEYRING),
        ({"sentinels": {"0": ["env:MODEL_API_KEY"], "1": ["/workspace/x"]}}, "CREDENTIALS"),
        ({"sentinels": {"0": ["env:MODEL_API_KEY", "env:OTHER"], "1": []}}, "CREDENTIALS"),
        ({"sentinels": {"0": [], "1": []}}, "CREDENTIALS"),
        ({"sentinels": None}, "CREDENTIALS"),
    ],
)
def test_each_item_the_container_does_not_show_is_named(change, capability):
    _assert_named(change, capability)


def _assert_named(change: dict, capability, expect: Expectation | None = None) -> None:
    observation = {**GOOD, **change}
    findings = judge(observation, expect or _expect())
    if isinstance(capability, ContainerCapability):
        expected = {capability}
    elif isinstance(capability, str):
        expected = {ContainerCapability[capability]}
    else:
        expected = {ContainerCapability[name] for name in capability}
    assert {finding.capability for finding in findings} == expected, findings
    for finding in findings:
        assert finding.describe().startswith(finding.capability.value + ": ")


_HOST_BIND = "8:1 /home/op"


@pytest.mark.parametrize(
    "mountinfo",
    [
        # A read-only bind that is writable: its probe failing proves nothing.
        MOUNTINFO.replace(" /source ro,relatime", " /source rw,relatime"),
        # The policy's mounts from the wrong source or of the wrong type.
        _mountinfo("/source", "600 500 8:1 /home/op/code/repo /source ro - ext4 /dev/sda1 rw"),
        _mountinfo("/source", f"600 500 8:1 {STAGED}-other /source ro - ext4 /dev/sda1 rw"),
        _mountinfo("/workspace", f"600 500 {_HOST_BIND} /workspace rw - ext4 /dev/sda1 rw"),
        _mountinfo(
            "/workspace",
            "600 500 8:2 /op/.local/share/containers/storage/volumes/af-vol-2/_data "
            "/workspace rw - ext4 /dev/sda2 rw",
        ),
        _mountinfo("/home/agent", f"600 500 {_HOST_BIND} /home/agent rw - ext4 /dev/sda1 rw"),
        _mountinfo("/tmp", "600 500 0:70 /x /tmp rw - tmpfs tmpfs rw"),
        # A policy mount missing, and one stacked over it.
        _mountinfo("/source", None),
        MOUNTINFO + f"\n600 517 {_HOST_BIND} /source ro - ext4 /dev/sda1 rw",
        # Something the policy never makes.
        MOUNTINFO + "\n600 500 0:60 / /run/secrets ro - tmpfs t ro",
        MOUNTINFO + f"\n601 500 {_HOST_BIND} /mnt/home ro - ext4 /dev/sda1 rw",
        MOUNTINFO + f"\n602 516 {_HOST_BIND} /workspace/home ro - ext4 /dev/sda1 rw",
        # Hostile mounts under the kernel trees, which are exempt no more.
        MOUNTINFO + f"\n603 502 {_HOST_BIND} /dev/hostdata rw - ext4 /dev/sda1 rw",
        MOUNTINFO + "\n604 502 0:5 /sda /dev/sda rw - devtmpfs udev rw",
        _mountinfo("/dev/null", "605 502 0:5 /sda /dev/null rw - devtmpfs udev rw"),
        MOUNTINFO + "\n606 502 0:5 / /dev/hostdev rw - devtmpfs udev rw",
        MOUNTINFO + f"\n607 501 {_HOST_BIND}/.ssh /proc/acpi/x ro - ext4 /dev/sda1 rw",
        MOUNTINFO + "\n608 501 0:99 /1 /proc/hostpid ro - proc proc rw",
        MOUNTINFO + "\n609 501 0:63 / /proc/scsi rw - tmpfs tmpfs rw",
        MOUNTINFO + "\n610 501 0:51 /sys /proc/sys rw - proc proc rw",
        MOUNTINFO + f"\n611 508 {_HOST_BIND} /sys/kernel/x ro - ext4 /dev/sda1 rw",
        MOUNTINFO + "\n612 508 0:64 / /sys/kernel/debug ro - debugfs debugfs rw",
        MOUNTINFO + "\n613 508 0:65 /user.slice /sys/fs/cgroup/x ro - cgroup2 cgroup2 rw",
        _mountinfo("/sys", "614 500 0:56 / /sys rw,nosuid - sysfs sysfs rw"),
        _mountinfo("/proc", "615 500 8:1 /proc /proc rw - ext4 /dev/sda1 rw"),
        _mountinfo("/dev", "616 500 0:5 / /dev rw - devtmpfs udev rw"),
        # The runtime's own files from anywhere but this container's directory.
        _mountinfo("/etc/hosts", f"617 500 {_HOST_BIND}/hosts /etc/hosts ro - ext4 /dev/sda1 rw"),
        _mountinfo(
            "/etc/resolv.conf",
            f"618 500 0:59 /containers/overlay-containers/{OTHER_CID}/userdata/resolv.conf "
            "/etc/resolv.conf rw - tmpfs tmpfs rw",
        ),
        _mountinfo(
            "/run/.containerenv",
            f"619 500 0:59 {USERDATA}/hosts /run/.containerenv rw - tmpfs tmpfs rw",
        ),
        # A root that is a host directory, not the runtime's overlay.
        MOUNTINFO.replace("- overlay overlay", "- ext4 /dev/sda1"),
        MOUNTINFO + "\ngarbage",
    ],
)
def test_a_mount_is_judged_by_its_source_type_and_options_not_its_name(mountinfo):
    """D12's closed set and a read-only /source are proved by what each mount is: its
    source, its type and its read-only option. A writable bind, a mount from the wrong
    source and a host mount under /dev, /proc or /sys are each named; the runtime's own
    mounts are admitted only as the runtime makes them."""
    _assert_named({"mountinfo": mountinfo}, ContainerCapability.MOUNTS)


_HELD = {"count": 2, "names": ["bus", "podman.sock"]}


@pytest.mark.parametrize(
    "tmpfs",
    [
        # A host tmpfs bound in whole (here the operator's /run/user/<uid>) where the
        # runtime masks a path, where the IPC namespace's shm is, or where the policy
        # makes its own: same type and root, but it holds the host's files.
        {**TMPFS, "/proc/acpi": _HELD},
        {**TMPFS, "/sys/firmware": _HELD},
        {**TMPFS, "/dev/shm": {"count": 1, "names": ["sem.host"]}},
        {**TMPFS, "/tmp": {"count": 1, "names": [".X11-unix"]}},
        {**TMPFS, HOME_TARGET: {"count": 1, "names": [".ssh"]}},
        # A listing cut short, failed or missing proves nothing.
        {**TMPFS, "/tmp": {"count": 30, "names": []}},
        {**TMPFS, "/proc/acpi": {"error": "EACCES"}},
        {key: value for key, value in TMPFS.items() if key != "/sys/firmware"},
        {**TMPFS, "/dev/shm": {"count": True, "names": []}},
        None,
    ],
)
def test_a_tmpfs_is_admitted_only_with_a_fresh_ones_content(tmpfs):
    """``mountinfo`` cannot tell a fresh tmpfs from a host tmpfs bound in whole: only its
    listing can, so a tmpfs holding anything but the policy's own mount points is named."""
    _assert_named({"tmpfs": tmpfs}, ContainerCapability.MOUNTS)


def test_the_home_tmpfs_may_hold_the_projections_mount_points(credential):
    """The runtime makes each projected file's first directory in the home: admitted, and
    nothing beside it."""
    spec = _launch(
        source=StagingRoot("/code/.git", "270", LAUNCH_ID),
        projection=Projection(files=(FileCredential(credential, ".config/p/key"),)),
    )
    expect = dataclasses.replace(
        _expect(),
        mounts=Expectation.for_spec(
            spec, product=RuntimeProduct.PODMAN, operator_uid=1000, subordinate=()
        ).mounts,
    )
    home = next(mount for mount in expect.mounts if mount.target == HOME_TARGET)
    assert home.entries == {".config"}
    bound = f"600 515 8:1 /op/.config/provider/key {HOME_TARGET}/.config/p/key ro - ext4 x rw"
    observation = {**GOOD, "mountinfo": f"{MOUNTINFO}\n{bound}"}
    held = {**TMPFS, HOME_TARGET: {"count": 1, "names": [".config"]}}
    assert judge({**observation, "tmpfs": held}, expect) == ()
    held = {**TMPFS, HOME_TARGET: {"count": 2, "names": [".config", ".netrc"]}}
    findings = judge({**observation, "tmpfs": held}, expect)
    assert [f.detail for f in findings] == [
        f"{HOME_TARGET}: it holds .netrc, which a fresh tmpfs would not"
    ]


def test_the_runtimes_own_files_need_the_containers_id():
    """Without the container's id, /etc/hosts cannot be told from a host file bound there."""
    findings = judge(GOOD, _expect(container_id=""))
    assert {f.capability for f in findings} == {ContainerCapability.MOUNTS}
    assert {f.detail.split(":")[0] for f in findings} == {
        "/etc/hosts",
        "/etc/resolv.conf",
        "/run/.containerenv",
    }


def test_the_exporters_workspace_must_be_read_only():
    exporter = ContainerSpec(ContainerRole.EXPORTER, ContainerNetwork.NONE, "af-vol-1")
    expect = Expectation.for_spec(
        exporter,
        product=RuntimeProduct.PODMAN,
        operator_uid=1000,
        subordinate=((100000, 65536),),
        container_id=CID,
    )
    mountinfo = _mountinfo("/source", None)
    writable = {**WRITABLE, WORKSPACE_TARGET: False}
    offline = {"interfaces": ["lo"], "routes": []}
    clean = {**GOOD, **offline, "mountinfo": mountinfo, "writable": writable}
    found = judge(clean, expect)
    assert {f.detail for f in found} == {
        "/workspace: mounted read-write, where the policy mounts it read-only"
    }
    readonly = mountinfo.replace(" /workspace rw,relatime", " /workspace ro,relatime")
    assert judge({**clean, "mountinfo": readonly}, expect) == ()


@pytest.mark.parametrize(
    "change",
    [
        # One timeout, a reset, an unsupported family: each proves nothing.
        {"connect": _connect("127.0.0.1", _refused("127.0.0.1", "timeout"))},
        {"connect": _connect("127.0.0.1", _refused("127.0.0.1", "ECONNRESET"))},
        {"connect": _connect("::1", _refused("::1", "EAFNOSUPPORT"))},
        {"connect": _connect("192.168.1.1", _refused("192.168.1.1", "timeout"))},
        # An address or name not tried, a family left out, a gateway not tried.
        {"connect": _connect("127.0.0.1")},
        {"connect": _connect("::1")},
        {"connect": _connect("host.containers.internal")},
        {"connect": _connect("fe80::1%eth0")},
        {"connect": _connect("127.0.0.1", _refused("127.0.0.1"), port=PORT + 1)},
        {"gateways": None},
        {"connect": []},
        {"connect": None},
        # A name that resolved to nothing tried, or whose lookup failed.
        {"connect": [*_connect("host.containers.internal"), _tried("host.containers.internal")]},
        {
            "connect": [
                *_connect("host.docker.internal"),
                {"host": "host.docker.internal", "port": PORT, "resolve_error": "EAI_AGAIN"},
            ]
        },
        # A connection with no nonce that neither closed nor said anything: inconclusive.
        {"connect": _connect("127.0.0.1", _connected("", eof=False))},
        {"connect": _connect("127.0.0.1", _connected("SSH-2.0-x", eof=False))},
        # Accepted and closed with nothing said: a forwarder that dropped it, or worse.
        {"connect": _connect("127.0.0.1", _connected("", eof=True))},
        {"connect": _connect("127.0.0.1", {"address": "127.0.0.1", "result": "connected"})},
        # The nonce, whole or cut short: reached.
        {"connect": _connect("::1", _connected("nonce-loop-1", eof=True, address="::1"))},
        {"connect": _connect("127.0.0.1", _connected("nonce-lo", eof=True))},
        {"connect": _connect("10.0.2.2", _connected("xx nonce-loop-1", eof=False))},
        # One address of a name proved, another not.
        {
            "connect": _connect(
                "host.containers.internal",
                _refused("169.254.1.2"),
                _refused("fd00::2", "timeout"),
            )
        },
    ],
)
def test_host_loopback_needs_a_conclusive_refusal_at_every_route(change):
    """Every route (both loopback families, the runtime's aliases, every gateway) at every
    listener's port must be refused, unreachable, a name that does not exist, or another
    service that answered without the nonce and closed. A timeout, a missing attempt, a
    failed lookup or a silent connection proves nothing; a nonce, even cut short, is a
    reached listener."""
    _assert_named(change, ContainerCapability.HOST_LOOPBACK)


def test_both_loopback_families_must_have_had_a_listener():
    """A host that listened on 127.0.0.1 alone would not see a path to its ::1."""
    findings = judge(GOOD, _expect(loopback_listeners=(("127.0.0.1", PORT),)))
    assert [f.detail for f in findings] == [
        "the host had no nonce listener on ::1, so a path to it would go unseen"
    ]


def test_the_routes_a_measurement_must_try_follow_the_runtime():
    """Rootless Docker's RootlessKit host address is tried there; Podman's host address is
    its alias's. The measurement's own default list covers both runtimes."""
    docker, podman = loopback_routes(RuntimeProduct.DOCKER), loopback_routes(RuntimeProduct.PODMAN)
    assert docker == (*LOOPBACK_ADDRESSES, *HOST_ALIASES, "10.0.2.2")
    assert podman == (*LOOPBACK_ADDRESSES, *HOST_ALIASES)
    assert set(docker) | set(podman) <= set(container_measure._HOST_ROUTES)
    findings = judge(GOOD, dataclasses.replace(_expect(), loopback_routes=docker))
    assert [f.detail for f in findings] == [f"no connection to 10.0.2.2 port {PORT} was tried"]


@pytest.mark.parametrize(
    "abstract",
    [
        None,
        {},
        {"result": "connected", "received": "", "eof": False},
        {"result": "connected", "received": "other", "eof": True},
        {"result": "connected", "received": "nonce-abstract-1", "eof": True},
        {"result": "timeout"},
        {"result": "EAFNOSUPPORT"},
    ],
)
def test_a_host_abstract_socket_needs_a_conclusive_refusal(abstract):
    """Its name is the host's own random one: anything that accepts a connection to it is
    reached, nonce or not; only a refusal or a denial proves it unreachable."""
    _assert_named({"abstract": abstract}, ContainerCapability.HOST_ABSTRACT_SOCKET)


@pytest.mark.parametrize("result", ["ECONNREFUSED", "EACCES", "EPERM"])
def test_a_refused_or_denied_abstract_socket_is_unreachable(result):
    assert judge({**GOOD, "abstract": {"result": result}}, _expect()) == ()


@pytest.mark.parametrize(
    "change",
    [
        {"scan": None},
        {"scan": {**SCAN, "incomplete": ["/data/big: EIO"], "incomplete_count": 1}},
        {"scan": {**SCAN, "incomplete": [], "incomplete_count": 25}},
        {"scan": {**SCAN, "incomplete_count": None}},
        {"scan": {key: value for key, value in SCAN.items() if key != "incomplete"}},
        {"scan": {**SCAN, "roots": ["/workspace"]}},
        {"scan": {**SCAN, "skip": [*SCAN["skip"], "/home"]}},
        # A sentinel the search reported nothing for, or nothing usable.
        {"sentinels": {"0": ["env:MODEL_API_KEY"]}},
        {"sentinels": {"0": ["env:MODEL_API_KEY"], "1": None}},
        {"sentinels": {"0": ["env:MODEL_API_KEY"], "1": "none"}},
    ],
)
def test_an_incomplete_sentinel_search_proves_no_credential_absent(change):
    """A place the search could not cover, a root it did not walk, a tree it left out
    beyond the kernel's, or a sentinel with no result is a finding, never a clean absence."""
    _assert_named(change, ContainerCapability.CREDENTIALS)


def test_under_network_none_only_loopback_and_no_route_pass():
    """With no interface but loopback and no route proved, a name the container could not
    look up leads nowhere; with a route, the same failed lookup proves nothing."""
    offline = _expect(network=ContainerNetwork.NONE)
    connect = [
        _tried("127.0.0.1", _refused("127.0.0.1")),
        _tried("::1", _refused("::1")),
        {"host": "host.containers.internal", "port": PORT, "resolve_error": "EAI_AGAIN"},
        {"host": "host.docker.internal", "port": PORT, "resolve_error": "EAI_NONAME"},
    ]
    clean = {**GOOD, "interfaces": ["lo"], "routes": [], "gateways": [], "connect": connect}
    assert judge(clean, offline) == ()
    for change in ({"interfaces": ["lo", "eth0"]}, {"routes": ["ipv4 via tap0"]}):
        findings = judge({**clean, **change}, offline)
        assert {f.capability for f in findings} == {
            ContainerCapability.NO_ROUTE,
            ContainerCapability.HOST_LOOPBACK,
        }
    on_bridge = judge({**clean, "interfaces": ["eth0", "lo"]}, _expect())
    assert {f.capability for f in on_bridge} == {ContainerCapability.HOST_LOOPBACK}


def test_an_observation_that_does_not_parse_proves_nothing():
    findings = judge("Traceback (most recent call last):", _expect())
    assert {f.capability for f in findings} == set(ContainerCapability)


def test_the_expectation_follows_the_spec(credential, staging):
    exporter = ContainerSpec(ContainerRole.EXPORTER, ContainerNetwork.NONE, "af-vol-1")
    expect = Expectation.for_spec(
        exporter, product=RuntimeProduct.DOCKER, operator_uid=1000, subordinate=()
    )
    assert WORKSPACE_TARGET not in expect.writable and WORKSPACE_TARGET in expect.mount_points
    workspace = next(m for m in expect.mounts if m.target == WORKSPACE_TARGET)
    assert workspace.read_only and workspace.source == "af-vol-1"
    assert "10.0.2.2" in expect.loopback_routes
    launch = _launch(
        cache_volume="af-cache",
        source=staging,
        projection=Projection(files=(FileCredential(credential, ".config/p/key"),)),
    )
    expect = Expectation.for_spec(
        launch, product=RuntimeProduct.PODMAN, operator_uid=1000, subordinate=()
    )
    assert CACHE_TARGET in expect.writable
    by_target = {m.target: m for m in expect.mounts}
    assert by_target[f"{HOME_TARGET}/.config/p/key"].source == "provider/key"
    assert by_target[f"{HOME_TARGET}/.config/p/key"].read_only
    assert by_target[SOURCE_TARGET].source == staging.layout
    assert by_target[SOURCE_TARGET].read_only
    assert not by_target[CACHE_TARGET].read_only


def test_the_operators_subordinate_ranges_are_read_by_name_or_uid():
    text = "root:100000:65536\nruoshi:165536:65536\n1000:300000:10\nbad line\nx:1:y\n"
    assert subordinate_ranges(text, "ruoshi", 1000) == ((165536, 65536), (300000, 10))
    assert subordinate_ranges(text, "nobody", 4242) == ()


# -- the measurement, on this host as its own negative control --------------------------


def _serve(server: socket.socket, nonce: bytes, *, silent: bool = False) -> None:
    def loop() -> None:
        held = []
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            if silent:
                held.append(conn)  # accepted, never answered, never closed
                continue
            with conn:
                conn.sendall(nonce)

    threading.Thread(target=loop, daemon=True).start()


def _listen(nonce: bytes, family: int = socket.AF_INET, port: int = 0, **kw) -> socket.socket:
    server = socket.socket(family, socket.SOCK_STREAM)
    server.bind(("127.0.0.1" if family == socket.AF_INET else "::1", port))
    server.listen(4)
    _serve(server, nonce, **kw)
    return server


def _abstract(name: str, nonce: bytes) -> socket.socket:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind("\0" + name)
    server.listen(4)
    _serve(server, nonce)
    return server


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads Linux's /proc")
def test_the_measurement_on_the_host_itself_is_refused_item_by_item(tmp_path):
    """Run as a plain host process, the measurement must fail the judge: the host's
    loopback listener and abstract socket answer, the UID is the operator's, and a fake
    credential outside the projection is found. So a container that leaked the same would
    be named too. Its request, the fake credentials included, goes on standard input."""
    nonce = f"nonce-loop-{os.getpid()}"
    abstract_name = f"autoforge-test-270-{os.getpid()}"
    listener = _listen(nonce.encode())
    port = listener.getsockname()[1]
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
    argv = [sys.executable, "-I", str(MEASURE)]
    try:
        done = subprocess.run(
            argv,
            input=json.dumps(spec),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    finally:
        listener.close()
        abstract.close()
    assert not any(value in word for word in argv for value in (SENTINEL_ENV, SENTINEL_GH))
    observation = json.loads(done.stdout)
    assert SENTINEL_ENV not in done.stdout and SENTINEL_GH not in done.stdout
    assert observation["writable"] == {str(writable): True, "/proc": False}
    assert observation["sentinels"] == {
        "0": ["env:MODEL_API_KEY"],
        "1": [str(scan / "deep" / "hosts.yml")],
    }
    assert observation["scan"]["incomplete_count"] == 0 and observation["scan"]["files"] == 1
    loopback = next(e for e in observation["connect"] if e["host"] == "127.0.0.1")
    assert loopback["attempts"] == [
        {"address": "127.0.0.1", "result": "connected", "received": nonce, "eof": True}
    ]
    expect = dataclasses.replace(
        _expect(),
        loopback_nonce=nonce,
        loopback_listeners=(("127.0.0.1", port), ("::1", port)),
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
        ContainerCapability.MOUNTS,
    } <= found


def test_the_measurement_records_each_attempt_as_it_ended(monkeypatch):
    """A refusal, the nonce with the peer's close, and a peer that accepts and stays silent
    (no EOF, so inconclusive), each recorded as such."""
    monkeypatch.setattr(container_measure, "_TIMEOUT_SECONDS", 0.3)
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    refused_port = closed.getsockname()[1]
    closed.close()
    answering = _listen(b"nonce-x")
    silent = _listen(b"", silent=True)
    try:
        refused = container_measure._route("127.0.0.1", refused_port)
        answered = container_measure._route("127.0.0.1", answering.getsockname()[1])
        quiet = container_measure._route("127.0.0.1", silent.getsockname()[1])
    finally:
        answering.close()
        silent.close()
    assert refused["attempts"] == [{"address": "127.0.0.1", "result": "ECONNREFUSED"}]
    assert answered["attempts"] == [
        {"address": "127.0.0.1", "result": "connected", "received": "nonce-x", "eof": True}
    ]
    assert quiet["attempts"] == [
        {"address": "127.0.0.1", "result": "connected", "received": "", "eof": False}
    ]
    assert container_measure._abstract(f"autoforge-test-270-none-{os.getpid()}") == {
        "result": "ECONNREFUSED"
    }


def _search(tmp_path: Path, values: list[str], **kw) -> tuple[dict, dict]:
    return container_measure._search(values, [str(tmp_path)], [], kw.get("max_bytes", 1 << 40))


def test_the_search_reads_every_file_whole(tmp_path):
    """No size limit: a sentinel past the first MiB, and one across a chunk's edge, are found."""
    chunk = container_measure._CHUNK_BYTES
    beyond = tmp_path / "beyond"
    beyond.write_bytes(b"x" * (chunk + 4096) + SENTINEL_GH.encode() + b"y" * 10)
    across = tmp_path / "across"
    across.write_bytes(b"x" * (chunk - 5) + SENTINEL_FILE.encode() + b"y" * 10)
    hits, scan = _search(tmp_path, [SENTINEL_GH, SENTINEL_FILE])
    assert hits == {"0": [str(beyond)], "1": [str(across)]}
    assert scan["incomplete"] == [] and scan["incomplete_count"] == 0
    assert scan["files"] == 2 and scan["bytes"] == beyond.stat().st_size + across.stat().st_size


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
def test_what_the_search_cannot_cover_is_reported_and_refused(tmp_path, monkeypatch):
    """A file or directory the agent owns but cannot read now (it could chmod it), one it
    can search but not list, and a spent byte budget make the search incomplete, which the
    judge refuses; a file it is denied and does not own is out of its reach, and counted."""
    locked = tmp_path / "locked"
    locked.write_text(SENTINEL_GH)
    locked.chmod(0o000)
    unlisted = tmp_path / "unlisted"
    unlisted.mkdir()
    (unlisted / "inside").write_text(SENTINEL_GH)
    unlisted.chmod(0o100)
    try:
        hits, scan = _search(tmp_path, [SENTINEL_GH])
        assert hits == {"0": []}
        assert scan["incomplete_count"] == 2
        assert sorted(entry.split(":")[0] for entry in scan["incomplete"]) == sorted(
            [str(locked), str(unlisted)]
        )
        observation = {**GOOD, "scan": {**scan, "roots": ["/"], "skip": SCAN["skip"]}}
        findings = judge(observation, _expect())
        assert {f.capability for f in findings} == {ContainerCapability.CREDENTIALS}
        # The same paths, owned by another UID: denied, out of the agent's reach.
        unlisted.chmod(0o000)
        monkeypatch.setattr(container_measure.os, "getuid", lambda: os.geteuid() + 1)
        hits, scan = _search(tmp_path, [SENTINEL_GH])
        assert scan["incomplete_count"] == 0 and scan["denied"] == 2
    finally:
        monkeypatch.undo()
        unlisted.chmod(0o700)
        locked.chmod(0o600)
    (tmp_path / "big").write_bytes(b"z" * 4096)
    _, spent = _search(tmp_path, [SENTINEL_GH], max_bytes=1024)
    assert spent["incomplete_count"] == 1 and "budget" in spent["incomplete"][0]


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
    tables = {path: text.splitlines() for path, text in files.items()}
    monkeypatch.setattr(container_measure, "_lines", lambda path, missing=None: tables[path])
    assert container_measure._gateways() == ["10.0.2.1", "fe80::1%eth0"]


@pytest.mark.parametrize(
    ("ipv4", "ipv6", "expected"),
    [
        (None, [], None),  # /proc/net/route unreadable
        ([], [], None),  # read empty, though it always has its header line
        (["Iface\tDestination\tGateway"], None, None),  # ipv6_route unreadable
        (["Iface\tDestination\tGateway"], [], []),  # read, with no default route
    ],
)
def test_a_routing_table_not_read_is_not_one_without_a_gateway(ipv4, ipv6, expected, monkeypatch):
    """``None`` makes the judge refuse (the gateways went unreported); only tables read
    whole give a list, which may be empty."""
    tables = {"/proc/net/route": ipv4, "/proc/net/ipv6_route": ipv6}
    monkeypatch.setattr(container_measure, "_lines", lambda path, missing=None: tables[path])
    assert container_measure._gateways() == expected


def test_a_routing_table_the_kernel_does_not_have_is_empty(tmp_path):
    """``/proc/net/ipv6_route`` is absent only from a kernel without IPv6: no IPv6 route."""
    assert container_measure._lines(str(tmp_path / "absent"), missing=[]) == []
    assert container_measure._lines(str(tmp_path / "absent")) is None
    assert container_measure._lines(str(tmp_path)) is None  # a directory: unreadable


def test_the_measurement_lists_each_tmpfs_of_its_own(tmp_path):
    """Only a tmpfs mounted from its own root is listed (a fresh one, or a host one bound in
    whole), with its entry count and first names, or the error that kept it unlisted."""
    empty = tmp_path / "empty"
    empty.mkdir()
    full = tmp_path / "with space"
    full.mkdir()
    for index in range(container_measure._MAX_REPORTED + 2):
        (full / f"f{index:02d}").write_text("x")
    escaped = str(full).replace(" ", "\\040")
    mountinfo = "\n".join(
        [
            f"1 0 0:1 / {empty} rw - tmpfs tmpfs rw",
            f"2 0 0:2 / {escaped} ro shared:3 - tmpfs tmpfs ro",
            f"3 0 0:3 / {tmp_path}/missing rw - tmpfs tmpfs rw",
            f"4 0 0:4 /sub {tmp_path} rw - tmpfs tmpfs rw",  # a tmpfs's subdirectory: a bind
            f"5 0 8:1 / {tmp_path} rw - ext4 /dev/sda1 rw",  # not a tmpfs
            "garbage",
        ]
    )
    listed = container_measure._tmpfs_contents(mountinfo)
    assert listed == {
        str(empty): {"count": 0, "names": []},
        str(full): {
            "count": container_measure._MAX_REPORTED + 2,
            "names": [f"f{index:02d}" for index in range(container_measure._MAX_REPORTED)],
        },
        f"{tmp_path}/missing": {"error": "ENOENT"},
    }


def test_the_measurement_imports_nothing_of_autoforge():
    """The image carries the file alone (#271): only the standard library."""
    source = MEASURE.read_text(encoding="utf-8")
    assert "autoforge" not in "".join(
        line for line in source.splitlines() if line.startswith(("import ", "from "))
    )


def test_the_measurement_reads_its_request_from_standard_input_only(capsys):
    """An argument is on a command line every local process can read, and on the runtime's
    exec argv: a request there is refused, so no fake credential can be put on one."""
    assert container_measure.main(["measure", '{"sentinels": ["x"]}']) == 2
    assert "standard input" in capsys.readouterr().err
    assert container_measure.main(["measure"], io.StringIO("[]")) == 2
    assert container_measure.main(["measure"], io.StringIO("{")) == 2
    assert container_measure.main(["measure"], io.StringIO('{"sentinels": [""]}')) == 2
    capsys.readouterr()
    assert container_measure.main(["-c"], io.StringIO('{"places": []}')) == 0
    observation = json.loads(capsys.readouterr().out)
    assert observation["writable"] == {} and "sentinels" not in observation
