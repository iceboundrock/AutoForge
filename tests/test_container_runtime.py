"""#271: runtime detection, the runtime client and the runtime's identity (ADR 0013 D8, D13, D16).

Every runtime here is the fake CLI of ``tests/container_fake.py``, installed
as ``podman`` or ``docker`` on a ``PATH`` of the test's own; no real
runtime is called. ADR 0013 §7's fake-runtime tests for #271: the
detection order and its absence of fall-through; identity from the
runtime's own report and not its name; the store's fields from each
runtime's own ``info``, read again at every call (a store that moves
between two calls in one process is reported moved by the second); the
empty client configuration; a refused non-local endpoint.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from autoforge.container_runtime import (
    CAPABILITY_ENDPOINT,
    CAPABILITY_IDENTITY,
    CLIENT_ENV_ALLOWLIST,
    RuntimeClient,
    detect,
    endpoint_refusal,
    parse_identity,
)
from autoforge.errors import SandboxCapabilityError
from autoforge.sandbox_backend import runtime_refusal
from autoforge.sandbox_container import RuntimeProduct

from . import container_fake as fake


def install(tmp_path: Path, name: str, **state) -> Path:
    bin_dir = tmp_path / f"bin-{name}"
    defaults = (
        {"version": fake.PODMAN_VERSION, "info": fake.podman_info()}
        if name == "podman"
        else {"version": fake.DOCKER_VERSION, "info": fake.docker_info()}
    )
    fake.install(bin_dir, name, {**defaults, **state})
    return bin_dir


def environ(*bins: Path, **extra: str) -> dict[str, str]:
    """A controller environment whose PATH holds only the fakes: never a host runtime."""
    return {"PATH": os.pathsep.join(str(b) for b in bins), "HOME": "/home/op", **extra}


# -- detection (D8) --------------------------------------------------------------------


def test_unset_podman_is_used_when_on_path_and_docker_otherwise(tmp_path):
    podman, docker = install(tmp_path, "podman"), install(tmp_path, "docker")
    both = detect(None, environ(docker, podman))
    assert both.named is RuntimeProduct.PODMAN and both.executable == str(podman / "podman")
    assert "podman is on the controller's PATH" in both.how
    only_docker = detect(None, environ(docker))
    assert only_docker.named is RuntimeProduct.DOCKER
    with pytest.raises(SandboxCapabilityError, match="neither podman nor docker"):
        detect(None, environ(tmp_path / "empty"))


def test_the_setting_names_the_runtime_and_a_missing_one_is_never_replaced(tmp_path):
    podman, docker = install(tmp_path, "podman"), install(tmp_path, "docker")
    assert detect("docker", environ(podman, docker)).named is RuntimeProduct.DOCKER
    with pytest.raises(SandboxCapabilityError, match="names docker, and no docker") as refused:
        detect("docker", environ(podman))
    assert "never tried instead" in str(refused.value)
    with pytest.raises(SandboxCapabilityError, match="names podman or docker"):
        detect("crio", environ(podman))


def test_a_runtime_that_fails_its_check_is_refused_and_the_other_never_tried(tmp_path):
    podman = install(tmp_path, "podman", fail={"version": [125, "Error: cannot connect"]})
    docker = install(tmp_path, "docker")
    env = environ(podman, docker)
    client = RuntimeClient(detect(None, env), env)
    with pytest.raises(SandboxCapabilityError, match="podman version exited 125") as refused:
        client.identity()
    assert "the other runtime is never tried" in str(refused.value)
    assert fake.calls(docker) == []


# -- identity (D8, D16) ---------------------------------------------------------------


def test_a_docker_that_is_podman_is_identified_as_podman(tmp_path):
    """Fedora's podman-docker: a ``docker`` whose own report is Podman's."""
    wrapper = install(tmp_path, "docker", version=fake.PODMAN_VERSION, info=fake.podman_info())
    env = environ(wrapper)
    identity = RuntimeClient(detect("docker", env), env).identity()
    assert identity.product is RuntimeProduct.PODMAN and identity.named is RuntimeProduct.DOCKER
    assert identity.behind_another_name
    assert identity.report().product == "Podman"
    assert "reached through docker" in identity.describe()


def test_docker_engine_reports_its_rootlessness_and_a_rootful_one_is_refused(tmp_path):
    docker = install(tmp_path, "docker")
    env = environ(docker)
    identity = RuntimeClient(detect("docker", env), env).identity()
    assert identity.product is RuntimeProduct.DOCKER and identity.rootless is True
    assert identity.version == "27.3.1" and identity.api_version == "1.47"
    assert runtime_refusal(identity.report()) is None
    fake.update(docker, info=fake.docker_info(rootless=False))
    rootful = RuntimeClient(detect("docker", env), env).identity()
    assert rootful.rootless is False
    refusal = runtime_refusal(rootful.report())
    assert refusal is not None and "reports itself rootful" in str(refusal)


def test_the_store_fields_come_from_each_runtimes_own_info(tmp_path):
    docker = install(tmp_path, "docker")
    env = environ(docker)
    identity = RuntimeClient(detect("docker", env), env).identity()
    assert identity.store == (
        ("ID", "ABCD:EFGH"),
        ("DockerRootDir", "/home/op/.local/share/docker"),
    )
    podman = install(tmp_path, "podman")
    env = environ(podman)
    identity = RuntimeClient(detect("podman", env), env).identity()
    assert [key for key, _ in identity.store] == [
        "store.graphRoot",
        "store.runRoot",
        "store.graphDriverName",
    ]
    assert identity.report().store.startswith("podman:store.graphRoot=")


def test_a_store_that_moves_between_two_calls_is_reported_moved_by_the_second(tmp_path):
    podman = install(
        tmp_path,
        "podman",
        infos=[fake.podman_info("/old/storage"), fake.podman_info("/new/storage")],
    )
    env = environ(podman)
    client = RuntimeClient(detect("podman", env), env)
    first, second = client.identity(), client.identity()
    assert first.report().store != second.report().store
    assert "/new/storage" in second.report().store
    # Read at every call: two version and two info calls, nothing cached.
    assert [c["argv"][0] for c in fake.calls(podman)] == ["version", "info", "version", "info"]


@pytest.mark.parametrize(
    "version, info, match",
    [
        ("not json", {}, "not JSON"),
        ({}, {}, "neither Docker Engine's nor Podman's"),
        (fake.DOCKER_VERSION, {**fake.docker_info(), **fake.podman_info()}, "neither"),
        (
            fake.DOCKER_VERSION,
            {**fake.docker_info(), "OperatingSystem": "Docker Desktop"},
            "Desktop",
        ),
        (
            fake.PODMAN_VERSION,
            {
                **fake.podman_info(),
                "host": {"serviceIsRemote": True, "security": {"rootless": True}},
            },
            "remote service",
        ),
        (fake.PODMAN_VERSION, {"host": {}, "store": {"runRoot": "/x"}}, "no store.graphRoot"),
    ],
)
def test_a_report_that_is_not_verified_is_refused(version, info, match):
    raw_version = version if isinstance(version, str) else __import__("json").dumps(version)
    with pytest.raises(SandboxCapabilityError, match=match) as refused:
        parse_identity(
            raw_version,
            __import__("json").dumps(info),
            named=RuntimeProduct.DOCKER,
            executable="/usr/bin/docker",
            endpoint="unix:///run/user/1000/docker.sock",
        )
    assert refused.value.capability in (CAPABILITY_IDENTITY, CAPABILITY_ENDPOINT)


# -- the client (D13) -----------------------------------------------------------------


def test_every_call_gets_a_fresh_empty_configuration_and_only_allow_listed_variables(tmp_path):
    podman = install(tmp_path, "podman")
    env = environ(podman, GITHUB_TOKEN="ghp_fake", AWS_SECRET_ACCESS_KEY="fake", SSH_AUTH_SOCK="/s")
    client = RuntimeClient(detect("podman", env), env)
    client.identity()
    recorded = fake.calls(podman)
    assert len(recorded) == 2
    for call in recorded:
        assert call["config_listing"] == []
        assert set(call["env"]) <= {*CLIENT_ENV_ALLOWLIST, "DOCKER_CONFIG", "REGISTRY_AUTH_FILE"}
        assert call["cwd"] == call["config_dir"]
        assert not os.path.exists(call["config_dir"]), "removed after the call"
    assert recorded[0]["config_dir"] != recorded[1]["config_dir"]


@pytest.mark.parametrize(
    "name, value",
    [
        ("DOCKER_HOST", "tcp://10.0.0.5:2375"),
        ("DOCKER_HOST", "ssh://build@remote"),
        ("DOCKER_HOST", "unix://relative/docker.sock"),
        ("CONTAINER_HOST", "ssh://core@127.0.0.1:2222/run/podman/podman.sock"),
        ("DOCKER_CONTEXT", "remote-builder"),
        ("CONTAINER_CONNECTION", "machine-default"),
    ],
)
def test_a_non_local_endpoint_is_refused_before_any_call(tmp_path, name, value):
    docker = install(tmp_path, "docker")
    env = environ(docker, **{name: value})
    with pytest.raises(SandboxCapabilityError) as refused:
        RuntimeClient(detect("docker", env), env)
    assert refused.value.capability == CAPABILITY_ENDPOINT
    assert name in str(refused.value) and value not in str(refused.value)
    assert fake.calls(docker) == []


def test_a_local_socket_endpoint_is_passed_on_and_names_the_report(tmp_path):
    docker = install(tmp_path, "docker")
    socket = "unix:///run/user/1000/docker.sock"
    env = environ(docker, DOCKER_HOST=socket, DOCKER_CONTEXT="default")
    assert endpoint_refusal(env) is None
    identity = RuntimeClient(detect("docker", env), env).identity()
    assert identity.endpoint == socket
    assert "DOCKER_HOST" in fake.calls(docker)[0]["env"]
    assert "DOCKER_CONTEXT" not in fake.calls(docker)[0]["env"]
