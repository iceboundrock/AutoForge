"""The container backend's security policy, network mapping and credential delivery (#270).

ADR 0013 gives #270 four things, each configured at a container's create
and nowhere later, since a runtime fixes a container's user, mounts,
network and environment there:

- **the security policy** (D10, rootless only): the agent and repository
  commands run as :data:`AGENT_UID`, which a rootless runtime's user
  namespace maps to one of the operator's subordinate UIDs, and PID 1 (the
  watcher, #272) as :data:`WATCHER_UID`; every capability dropped,
  ``no-new-privileges``, a read-only root, the runtime's default seccomp
  profile, a PID limit (:data:`PIDS_LIMIT`), a private IPC namespace. The
  only writable places are the ``/workspace`` volume, the per-issue cache
  volume, the private home and ``/tmp``, with the IPC namespace's own
  shared memory and message queues (:data:`RUNTIME_TEMP`);
- **the network mapping** (D9): a launch container on the runtime's
  rootless network in a namespace of its own (:attr:`ContainerNetwork.BRIDGE`),
  never the host's, or ``none``; every helper container (the stager, the
  exporter) ``none``. :class:`ContainerNetwork` has no host member, so no
  spec can ask for the host's network;
- **the credential-delivery mechanisms** (D14), a closed set: an
  environment variable, configured at the create by its name alone
  (``--env NAME``), its value only in the runtime client's environment
  (:func:`client_environment`), never on an argv; and, on a bind runtime
  only, a read-only file bound at its path in the private home. Only the
  launch container gets a projection;
- **the closed set of bind sources** (D12): the launch's staging root,
  read-only at ``/source``, in the bind form only, and the credential
  files above. Nothing else is bound, and the copy-in form binds nothing.

:func:`create_argv` renders a checked :class:`ContainerSpec` into a
runtime's ``create`` arguments: the only place a create's flags are
written, with no field that takes a raw flag, mount or option, so no
configuration can add ``--privileged``, a capability, a device, a host
namespace or a mount. :func:`exec_argv` runs a command as the agent.

**Measured, never assumed** (D10, D16 items 4, 5 and 7): what a runtime
actually enforced is observed inside a container by
:mod:`autoforge.container_measure` and judged here
(:func:`judge`, :class:`Expectation`), each missing item named as a
:class:`ContainerCapability`. A runtime that cannot enforce an item is
refused, not trusted. The probe that runs it is #271's and #273's.

**What a runtime does not reproduce**, refused rather than claimed:
bwrap's Landlock abstract-socket scope (ADR 0005 D9.1) has no container
equivalent; a network namespace of the container's own replaces it, so a
host abstract socket is unreachable by construction and the probe proves
it. A rootful runtime is refused before anything is saved or launched
(:func:`autoforge.sandbox_backend.runtime_refusal`, D10).

Executor layer: no workflow, no journal, no state, no process. It reads
the host only through ``lstat``, ``stat`` and ``realpath`` of the paths it
is given and their directories, never a credential's contents (ADR 0005
D7.2). A refusal is a
:class:`~autoforge.errors.SandboxPolicyError` naming the path or the
variable and the rule, never a value. Not selectable before #273: until
then no launch reaches this module.
"""

from __future__ import annotations

import enum
import json
import os
import posixpath
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .child_env import undoes_isolation
from .errors import SandboxPolicyError
from .sandbox import NetworkPosition, check_declared_path

# -- the fixed identities (D10) ------------------------------------------------------

# The agent and every repository command. Never 0, and never the operator's
# own UID: under a rootless runtime container root is the operator, and every
# other UID below the subordinate range's size is one of the operator's
# subordinate UIDs (``/etc/subuid``, 65536 by default). Numeric, so an image's
# ``/etc/passwd`` cannot redefine it.
AGENT_UID = 10001
AGENT_GID = 10001
# PID 1, the watcher (ADR 0013 D6, #272): another non-root UID, which the
# agent cannot signal (it holds no capability).
WATCHER_UID = 10002
WATCHER_GID = 10002
# The PID limit (cgroup ``pids.max``), threads included. Builds that run a
# job per core and JVM or Node test runners reach a few hundred; a fork bomb
# reaches it in a second.
PIDS_LIMIT = 4096

# -- the fixed places (D2, D10, D12) -------------------------------------------------

SOURCE_TARGET = "/source"
WORKSPACE_TARGET = "/workspace"
CACHE_TARGET = "/cache"
HOME_TARGET = "/home/agent"
TMP_TARGET = "/tmp"
# Temp the runtime itself provides, per IPC namespace, that the agent may
# find writable: shared memory and POSIX message queues. Allowed, never
# required (Podman's ``--read-only-tmpfs=false`` makes ``/dev/shm`` read-only).
RUNTIME_TEMP = frozenset({"/dev/shm", "/dev/mqueue"})
# Files a runtime mounts into every container from its own per-container
# storage, never from an operator path: allowed by the mount check.
RUNTIME_FILES = frozenset({"/etc/hosts", "/etc/hostname", "/etc/resolv.conf", "/run/.containerenv"})
_KERNEL_ROOTS = ("/proc", "/sys", "/dev")
# The observation's field for the container's UID map as the host reads it
# (``/proc/<PID 1's host PID>/uid_map``): the prober adds it beside what the
# measurement printed from inside.
HOST_UID_MAP = "host_uid_map"

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_LABEL_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,255}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
# A ``--mount`` value is a comma-separated list of key=value fields; Docker
# reads it as CSV, so a quote starts a quoted field. A path holding either
# could add a field of its own.
_MOUNT_UNSAFE = frozenset({",", '"', "\n", "\r", "\0"})

_RULE_PROJECTION = "D14 credential projection"
_RULE_BIND = "D12 closed set of bind sources"
_RULE_NETWORK = "D9 network position"
_RULE_SPEC = "D10 container policy"


class RuntimeProduct(enum.Enum):
    """The rootless runtimes ADR 0013 admits (D8): their CLI's name."""

    DOCKER = "docker"
    PODMAN = "podman"


class ContainerRole(enum.Enum):
    """Which of a launch's containers a create makes (ADR 0013 D13)."""

    # The agent's, or an export command's: the one with a network position
    # of its own and the only one with a projection.
    LAUNCH = "launch container"
    # The stager and the prover, offline (D9).
    STAGER = "stager container"
    # The exporter, offline; it only reads the volume (D5).
    EXPORTER = "exporter container"


class ContainerNetwork(enum.Enum):
    """A container's network position (ADR 0013 D9). There is no host member.

    The values are :mod:`autoforge.sandbox_backend`'s ``NETWORK_BRIDGE`` and
    ``NETWORK_NONE``, which a lifecycle object carries.
    """

    # A network namespace of the container's own, on the runtime's rootless
    # network: general egress, host loopback and host abstract sockets
    # unreachable (the probe proves both). Never ``--network host``.
    BRIDGE = "bridge"
    # A network namespace with loopback only, and no route.
    NONE = "none"


class StagingMethod(enum.Enum):
    """How a launch's inputs reach its containers (ADR 0013 D12)."""

    # A read-only bind of the staging root, where the probe proved the
    # runtime shares the controller's filesystem.
    BIND = "bind"
    # Copied in; nothing is bound.
    COPY = "copy"


def network_for(role: ContainerRole, position: NetworkPosition) -> ContainerNetwork:
    """The network a container gets (ADR 0013 D9): never the host's.

    Every helper container is offline. A launch container is on the bridge
    for :attr:`NetworkPosition.HOST`: for a container launch "host" (an
    agent's position, and ``*_network: host``, the default) means outbound
    access, not the host's network namespace. ``isolated`` is ``none``.
    """
    if role is not ContainerRole.LAUNCH:
        return ContainerNetwork.NONE
    if position is NetworkPosition.HOST:
        return ContainerNetwork.BRIDGE
    return ContainerNetwork.NONE


# -- the credential projection (D14) -------------------------------------------------


class Secret:
    """A projected value: never printed, never compared by value, never on an argv.

    Its only reader is :func:`client_environment`, which puts it in the
    runtime client's environment. ``repr`` and ``str`` show no part of it.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not value or "\0" in value:
            raise SandboxPolicyError(
                "<projected value>", _RULE_PROJECTION, "a projected value is a non-empty string"
            )
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("a projected value is never serialized")


@dataclass(frozen=True)
class EnvCredential:
    """A credential delivered as an environment variable of the launch container.

    The create names it alone (``--env NAME``); the runtime client takes the
    value from its own environment (:func:`client_environment`), so the
    value is on no argv. It persists in the runtime's container record,
    which ``inspect`` shows, until the container is removed (D14's residual).
    """

    name: str
    value: Secret = field(repr=False, compare=False)


@dataclass(frozen=True)
class FileCredential:
    """A credential file bound read-only at a path in the private home (bind runtimes only).

    ``source`` is the host file, given resolved: it must resolve to itself.
    ``target`` is its path relative to :data:`HOME_TARGET`. AutoForge never
    reads it: the runtime binds it.
    """

    source: str
    target: str

    @property
    def path(self) -> str:
        """Where the launch sees it."""
        return posixpath.join(HOME_TARGET, self.target)


@dataclass(frozen=True)
class Projection:
    """One launch's credential projection: D14's two mechanisms, nothing else."""

    env: tuple[EnvCredential, ...] = ()
    files: tuple[FileCredential, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.env or self.files)


NO_PROJECTION = Projection()


# The controller's variables D4.3's forbidden sources are located by
# (``autoforge.sandbox``): locations, never credentials.
_LOCATION_NAMES = ("GH_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR")


@dataclass(frozen=True)
class HostContext:
    """The controller's own facts a check needs: never a credential.

    ``home`` is the operator's home, ``environ`` the variables that locate
    D4.3's forbidden sources (:data:`_LOCATION_NAMES`), ``uid`` the
    operator's UID.
    """

    home: str
    environ: Mapping[str, str] = field(repr=False)
    uid: int

    @classmethod
    def current(cls) -> HostContext:
        located = {name: os.environ[name] for name in _LOCATION_NAMES if name in os.environ}
        return cls(os.path.expanduser("~"), located, os.getuid())


# What a projected variable must never be named, compared upper-cased: what
# the runtime client reads for its own behaviour (the value sits in its
# environment), what a shell, the loader or the controller's own overrides
# read, and what would hand a launch another credential (child_env).
_CLIENT_NAMES = frozenset(
    {
        "HOME",
        "PATH",
        "TMPDIR",
        "SHELL",
        "USER",
        "LOGNAME",
        "PWD",
        "HOSTNAME",
        "NOTIFY_SOCKET",
        "REGISTRY_AUTH_FILE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "FTP_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
)
_CLIENT_PREFIXES = (
    "DOCKER_",
    "CONTAINER_",
    "CONTAINERS_",
    "PODMAN_",
    "BUILDAH_",
    "STORAGE_",
    "XDG_",
    "LD_",
    "BASH_FUNC_",
)


def _refuse(subject: str, rule: str, detail: str) -> SandboxPolicyError:
    return SandboxPolicyError(subject, rule, detail)


def _check_env_name(name: str) -> None:
    if not isinstance(name, str) or not _ENV_NAME_RE.match(name):
        raise _refuse(repr(name), _RULE_PROJECTION, "not a variable name")
    upper = name.upper()
    if upper in _CLIENT_NAMES or upper.startswith(_CLIENT_PREFIXES):
        raise _refuse(
            name,
            _RULE_PROJECTION,
            "the runtime client, a shell or the loader reads this name for its own "
            "behaviour, and the projected value sits in the client's environment",
        )
    if undoes_isolation(name):
        raise _refuse(
            name,
            _RULE_PROJECTION,
            "a variable that would hand the launch a GitHub or git credential, or undo "
            "its isolation, is never projected",
        )


def _check_mount_text(text: str, subject: str, rule: str) -> None:
    unsafe = sorted(ch for ch in _MOUNT_UNSAFE if ch in text)
    if unsafe:
        raise _refuse(
            subject,
            rule,
            f"holds {unsafe[0]!r}, which would end the mount's field and could add one",
        )


def _first_link(path: str) -> str | None:
    """The first component of ``path`` (absolute, normalized) that is a symbolic link."""
    current = "/"
    for part in path.strip("/").split("/"):
        current = posixpath.join(current, part)
        if os.path.islink(current):
            return current
    return None


def _resolved_self(path: str, subject: str, rule: str) -> str:
    """C1 and C2 for a bind source: absolute, normalized, passing through no link."""
    if not isinstance(path, str) or not path.startswith("/"):
        raise _refuse(str(path), rule, f"{subject} is not an absolute path")
    if posixpath.normpath(path) != path or path.startswith("//"):
        raise _refuse(path, rule, f"{subject} is not a normalized absolute path")
    _check_mount_text(path, path, rule)
    link = _first_link(path)
    if link is not None:
        raise _refuse(
            path,
            rule,
            f"{subject} passes through the symbolic link {link}; a bind follows it, so "
            "what it names could change after the check",
        )
    if os.path.realpath(path) != path:
        raise _refuse(path, rule, f"{subject} does not resolve to itself")
    return path


def _check_file_credential(item: FileCredential, host: HostContext) -> None:
    rule = _RULE_PROJECTION
    source = _resolved_self(item.source, "a credential file", rule)
    try:
        info = os.lstat(source)
    except OSError as exc:
        raise _refuse(
            source, rule, f"a credential file that cannot be checked ({exc.strerror})"
        ) from None
    if not stat.S_ISREG(info.st_mode):
        raise _refuse(
            source,
            rule,
            "a credential is delivered only as a regular file; a directory, socket, FIFO "
            "or device is never bound",
        )
    # D4.3: never the home or an ancestor, never under a forbidden source
    # (gh's and git's configuration, ~/.ssh, ~/.gnupg, keyrings, /run and
    # $XDG_RUNTIME_DIR, where the runtime's own socket lives).
    check_declared_path(source, host.home, host.environ, "a credential file")
    if info.st_uid != host.uid:
        raise _refuse(
            source,
            rule,
            "a credential file the operator does not own could be replaced by its owner",
        )
    if not info.st_mode & stat.S_IROTH:
        raise _refuse(
            source,
            rule,
            f"the agent runs as UID {AGENT_UID}, a subordinate UID, which reads the file "
            "only through its permission bits for others; this one is unreadable inside, "
            "so it is refused rather than delivered unusable (keep it in a directory only "
            "you can enter, readable by others: a 0700 directory holding a 0644 file)",
        )
    if _reachable_by_others(source):
        raise _refuse(
            source,
            rule,
            "every local user can read this file: no directory above it denies them "
            "search; keep it in a directory only you can enter (mode 0700)",
        )
    target = item.target
    if not isinstance(target, str) or not target or target.startswith("/"):
        raise _refuse(str(target), rule, "a credential file's target is a path in the home")
    if posixpath.normpath(target) != target or any(
        part in ("", ".", "..") for part in target.split("/")
    ):
        raise _refuse(target, rule, "a credential file's target is a normalized path in the home")
    _check_mount_text(target, target, rule)


def _reachable_by_others(path: str) -> bool:
    """Whether every directory above ``path`` lets others search it."""
    current = posixpath.dirname(path)
    while True:
        try:
            if not os.stat(current).st_mode & stat.S_IXOTH:
                return False
        except OSError:
            return False
        if current == "/":
            return True
        current = posixpath.dirname(current)


def check_projection(
    projection: Projection, *, role: ContainerRole, staging: StagingMethod, host: HostContext
) -> None:
    """D14's rules for one container's projection; a refusal names the rule, never a value.

    Only the launch container gets one. An environment variable's name must
    not be one the runtime client, a shell or the loader reads, nor one
    :func:`autoforge.child_env.undoes_isolation` names (a GitHub token, an
    SSH agent socket, a git configuration variable). A file is delivered
    only on a bind runtime: a copy-in runtime would have to read it (ADR
    0005 D7.2). It is a regular file the operator owns, resolving to itself,
    outside D4.3's forbidden sources, readable by the agent's subordinate
    UID through its bits for others, and in a directory others cannot
    search.
    """
    if not projection:
        return
    if role is not ContainerRole.LAUNCH:
        raise _refuse(
            role.value,
            _RULE_PROJECTION,
            "only the launch container gets a projection; a helper container never does",
        )
    names: set[str] = set()
    for env in projection.env:
        if not isinstance(env.value, Secret):
            raise _refuse(env.name, _RULE_PROJECTION, "a projected value is held as a Secret")
        _check_env_name(env.name)
        if env.name in names:
            raise _refuse(env.name, _RULE_PROJECTION, "projected twice")
        names.add(env.name)
    if projection.files and staging is not StagingMethod.BIND:
        raise _refuse(
            projection.files[0].source,
            _RULE_PROJECTION,
            "a copy-in runtime delivers only environment credentials: copying a file in "
            "would mean reading it (ADR 0005 D7.2)",
        )
    targets: set[str] = set()
    for item in projection.files:
        _check_file_credential(item, host)
        if any(_related(item.path, other) for other in targets):
            raise _refuse(item.target, _RULE_PROJECTION, "two credential files overlap")
        targets.add(item.path)


def _related(a: str, b: str) -> bool:
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def client_environment(base: Mapping[str, str], projection: Projection) -> dict[str, str]:
    """The runtime client's environment for a create: ``base`` and the projected values.

    The create names each variable alone (``--env NAME``), and the client
    reads its value from here, so no value is on an argv. ``base`` is the
    controller's hardened environment for its runtime calls (#271); a name
    it already holds is refused, never overwritten.
    """
    env = dict(base)
    for item in projection.env:
        _check_env_name(item.name)
        if item.name in env:
            raise _refuse(
                item.name,
                _RULE_PROJECTION,
                "the runtime client's environment already holds this name",
            )
        env[item.name] = item.value.reveal()
    return env


# -- the container spec and its create (D10, D12) ------------------------------------


@dataclass(frozen=True)
class ContainerSpec:
    """Everything a create configures that the policy decides: no raw flag, mount or option.

    ``workspace_volume`` is the launch's ``/workspace`` volume, read-only for
    the exporter; ``cache_volume`` the per-issue cache volume, a
    change-producing launch container's only ("" for none); ``source`` the
    launch's staging root, bound read-only at ``/source`` into the stager
    and launch containers in the bind form ("" in the copy-in form);
    ``projection`` the launch container's credentials. The tmpfs sizes are
    in bytes, ``None`` for the kernel's default.
    """

    role: ContainerRole
    network: ContainerNetwork
    workspace_volume: str
    staging: StagingMethod = StagingMethod.BIND
    cache_volume: str = ""
    source: str = ""
    projection: Projection = NO_PROJECTION
    home_size_bytes: int | None = None
    tmp_size_bytes: int | None = None

    @property
    def user(self) -> tuple[int, int]:
        """The create's user: PID 1, the watcher. The agent's is :func:`exec_argv`'s."""
        return (WATCHER_UID, WATCHER_GID)


def _check_volume(name: str, label: str) -> None:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise _refuse(
            repr(name),
            _RULE_SPEC,
            f"the {label} is named by a runtime volume name, never a host path",
        )


def check_spec(spec: ContainerSpec, host: HostContext) -> None:
    """The spec against D9, D10, D12 and D14, before anything is created.

    A helper container is offline, with no projection and no cache; the
    copy-in form binds nothing; the bind form binds only the
    staging root (a directory resolving to itself, outside D4.3's
    forbidden sources) at ``/source`` and the projection's files.
    """
    if not isinstance(spec.role, ContainerRole) or not isinstance(spec.network, ContainerNetwork):
        raise _refuse(repr(spec.network), _RULE_NETWORK, "not a container network position")
    if spec.role is not ContainerRole.LAUNCH and spec.network is not ContainerNetwork.NONE:
        raise _refuse(
            spec.role.value,
            _RULE_NETWORK,
            "a helper container is created with --network none (ADR 0013 D9)",
        )
    _check_volume(spec.workspace_volume, "workspace volume")
    if spec.cache_volume:
        if spec.role is not ContainerRole.LAUNCH:
            raise _refuse(spec.role.value, _RULE_SPEC, "only a launch container mounts the cache")
        _check_volume(spec.cache_volume, "cache volume")
        if spec.cache_volume == spec.workspace_volume:
            raise _refuse(spec.cache_volume, _RULE_SPEC, "the cache is a volume of its own")
    if spec.source:
        if spec.staging is not StagingMethod.BIND:
            raise _refuse(spec.source, _RULE_BIND, "the copy-in form binds nothing")
        if spec.role is ContainerRole.EXPORTER:
            raise _refuse(spec.source, _RULE_BIND, "the exporter binds nothing")
        source = _resolved_self(spec.source, "the staging root", _RULE_BIND)
        if not os.path.isdir(source):
            raise _refuse(source, _RULE_BIND, "the staging root is a directory")
        check_declared_path(source, host.home, host.environ, "the staging root")
    for size in (spec.home_size_bytes, spec.tmp_size_bytes):
        if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 1):
            raise _refuse(str(size), _RULE_SPEC, "a tmpfs size is a positive number of bytes")
    check_projection(spec.projection, role=spec.role, staging=spec.staging, host=host)
    for item in spec.projection.files:
        if spec.source and _related(item.source, spec.source):
            raise _refuse(item.source, _RULE_BIND, "a credential file overlaps the staging root")


def _tmpfs(target: str, mode: int, size: int | None, owner: tuple[int, int] | None) -> str:
    options = ["rw", "exec", "nosuid", "nodev", f"mode={mode:04o}"]
    if owner is not None:
        options += [f"uid={owner[0]}", f"gid={owner[1]}"]
    if size is not None:
        options.append(f"size={size}")
    return f"--tmpfs={target}:{','.join(options)}"


def _volume(name: str, target: str, *, read_only: bool = False) -> str:
    value = f"type=volume,source={name},target={target}"
    return f"--mount={value},readonly=true" if read_only else f"--mount={value}"


def _bind(source: str, target: str) -> str:
    return f"--mount=type=bind,source={source},target={target},readonly=true"


def _network_option(product: RuntimeProduct, network: ContainerNetwork) -> str:
    if network is ContainerNetwork.NONE:
        return "--network=none"
    if product is RuntimeProduct.PODMAN:
        # pasta, in the container's own namespace. Podman gives it
        # --no-map-gw (no host access through the gateway address), -T none
        # and -U none (no forwarding of the container's loopback ports to the
        # host's) unless an option undoes them; none is given here, and the
        # probe proves host loopback unreachable (ADR 0013 D9).
        return "--network=pasta"
    # Docker Engine's bridge, inside rootless Docker's own network namespace:
    # host loopback is closed by RootlessKit's --disable-host-loopback, the
    # daemon's setting and its default; the probe proves it.
    return "--network=bridge"


def create_options(product: RuntimeProduct, spec: ContainerSpec) -> list[str]:
    """The create's options the policy owns, in a fixed order (the spec must be checked).

    The user and group are numeric. ``--pull=never``: the image is resolved
    by id, never fetched at a create. Podman's ``--read-only-tmpfs=false``
    keeps ``/run`` and ``/var/tmp`` read-only, and its ``--http-proxy=false``
    keeps the client's proxy variables out of the container. Nothing names
    ``--privileged``, ``--cap-add``, a device, a host namespace, a user
    namespace mode, a seccomp, AppArmor or SELinux override, a label
    relabel, ``--volume`` or ``--env-file``.
    """
    uid, gid = spec.user
    options = [
        f"--user={uid}:{gid}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
    ]
    if product is RuntimeProduct.PODMAN:
        options += ["--read-only-tmpfs=false", "--http-proxy=false"]
    options += [
        f"--pids-limit={PIDS_LIMIT}",
        "--ipc=private",
        _network_option(product, spec.network),
        "--pull=never",
        _tmpfs(TMP_TARGET, 0o1777, spec.tmp_size_bytes, None),
        _tmpfs(HOME_TARGET, 0o700, spec.home_size_bytes, (AGENT_UID, AGENT_GID)),
        _volume(
            spec.workspace_volume,
            WORKSPACE_TARGET,
            read_only=spec.role is ContainerRole.EXPORTER,
        ),
    ]
    if spec.cache_volume:
        options.append(_volume(spec.cache_volume, CACHE_TARGET))
    if spec.source:
        options.append(_bind(spec.source, SOURCE_TARGET))
    for item in spec.projection.files:
        options.append(_bind(item.source, item.path))
    for env in spec.projection.env:
        options.append(f"--env={env.name}")
    return options


def create_argv(
    product: RuntimeProduct,
    spec: ContainerSpec,
    host: HostContext,
    *,
    name: str,
    labels: Mapping[str, str],
    image: str,
    command: Iterable[str],
) -> list[str]:
    """``create`` and its arguments, after the executable: the spec checked, then rendered.

    ``name`` and ``labels`` are the object's (#272), ``image`` the image's
    id (#271) and ``command`` PID 1's. None of them is a flag: the image
    comes after every option, and a value that a create would read as an
    option is refused. A projected value found in any argument refuses the
    create (D14: never on an argv).
    """
    check_spec(spec, host)
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise _refuse(repr(name), _RULE_SPEC, "not a container name")
    if not isinstance(image, str) or not _IMAGE_RE.match(image):
        raise _refuse(repr(image), _RULE_SPEC, "not an image reference")
    argv = ["create", f"--name={name}"]
    for key, value in sorted(labels.items()):
        if not _LABEL_KEY_RE.match(key) or any(ch in value for ch in "\0\n\r"):
            raise _refuse(repr(key), _RULE_SPEC, "not a label")
        argv.append(f"--label={key}={value}")
    argv += create_options(product, spec)
    argv.append(image)
    for word in command:
        if not isinstance(word, str) or "\0" in word:
            raise _refuse(repr(word), _RULE_SPEC, "not a command word")
        argv.append(word)
    _refuse_values_on_argv(argv, spec.projection)
    return argv


def _refuse_values_on_argv(argv: list[str], projection: Projection) -> None:
    for env in projection.env:
        value = env.value.reveal()
        if any(value in word for word in argv):
            raise _refuse(
                env.name,
                _RULE_PROJECTION,
                "its value would appear on the runtime's command line, so nothing was created",
            )


def exec_argv(
    product: RuntimeProduct, container: str, command: Iterable[str], *, workdir: str = ""
) -> list[str]:
    """``exec`` and its arguments: ``command`` as the agent, in the launch container.

    The agent's UID and GID, numeric; no environment option (the projection
    is the create's, and an ``exec -e NAME=VALUE`` would put a value on an
    argv), no ``--privileged``.
    """
    if not isinstance(container, str) or not _NAME_RE.match(container):
        raise _refuse(repr(container), _RULE_SPEC, "not a container name")
    workdir = workdir or WORKSPACE_TARGET
    if posixpath.normpath(workdir) != workdir or not workdir.startswith("/"):
        raise _refuse(workdir, _RULE_SPEC, "not a normalized absolute directory")
    argv = [
        "exec",
        "--interactive",
        f"--user={AGENT_UID}:{AGENT_GID}",
        f"--workdir={workdir}",
        container,
    ]
    for word in command:
        if not isinstance(word, str) or "\0" in word:
            raise _refuse(repr(word), _RULE_SPEC, "not a command word")
        argv.append(word)
    return argv


# -- the measurement (D10; D16 items 4, 5 and 7) -------------------------------------


class ContainerCapability(enum.Enum):
    """What a container launch must be proved to have; the probe names a missing one."""

    ROOTLESS = "a rootless runtime: the user namespace maps no container UID to a host one"
    AGENT_USER = "the agent's non-root UID, mapped to one of the operator's subordinate UIDs"
    NO_CAPABILITIES = "every capability dropped"
    NO_NEW_PRIVILEGES = "no-new-privileges"
    SECCOMP = "the runtime's default seccomp profile"
    READ_ONLY_ROOT = "a read-only root filesystem"
    PID_LIMIT = "a PID limit"
    WRITABLE_PLACES = "writable only in the volume, the cache volume, the private home and temp"
    MOUNTS = "no mount but the policy's: no host path but the bind sources"
    SESSION_KEYRING = "no host session-keyring key reachable"
    HOST_LOOPBACK = "a host loopback listener unreachable"
    HOST_ABSTRACT_SOCKET = "a host abstract Unix socket unreachable"
    NO_ROUTE = "under --network none, no interface but loopback and no route"
    CREDENTIALS = "no credential outside the projection"


@dataclass(frozen=True)
class Finding:
    """One item the measurement did not prove, and what it saw instead (no value)."""

    capability: ContainerCapability
    detail: str

    def describe(self) -> str:
        return f"{self.capability.value}: {self.detail}"


@dataclass(frozen=True)
class Expectation:
    """What one measured container must show; built from its spec (:meth:`for_spec`).

    ``operator_uid`` and ``subordinate`` are the host's (``/etc/subuid``'s
    ranges for the operator, ``(start, count)``); ``writable`` the places the
    agent must be able to write, and the only ones, with
    :data:`RUNTIME_TEMP`; ``mounts`` the mount points the policy makes;
    ``loopback_nonce`` and ``abstract_nonce`` what the host's listeners
    send; ``sentinels`` the places each fake credential may appear, by the
    index the measurement was given it under (``env:NAME`` for a variable).
    """

    network: ContainerNetwork
    operator_uid: int
    subordinate: tuple[tuple[int, int], ...]
    writable: frozenset[str]
    mounts: frozenset[str]
    loopback_nonce: str = ""
    abstract_nonce: str = ""
    keyring_checked: bool = False
    sentinels: tuple[frozenset[str], ...] = ()
    uid: int = AGENT_UID
    gid: int = AGENT_GID
    pids_limit: int = PIDS_LIMIT

    @classmethod
    def for_spec(
        cls,
        spec: ContainerSpec,
        *,
        operator_uid: int,
        subordinate: tuple[tuple[int, int], ...],
        loopback_nonce: str = "",
        abstract_nonce: str = "",
        keyring_checked: bool = False,
        sentinels: tuple[frozenset[str], ...] = (),
    ) -> Expectation:
        writable = {HOME_TARGET, TMP_TARGET}
        if spec.role is not ContainerRole.EXPORTER:
            writable.add(WORKSPACE_TARGET)
        mounts = {HOME_TARGET, TMP_TARGET, WORKSPACE_TARGET}
        if spec.cache_volume:
            writable.add(CACHE_TARGET)
            mounts.add(CACHE_TARGET)
        if spec.source:
            mounts.add(SOURCE_TARGET)
        mounts.update(item.path for item in spec.projection.files)
        return cls(
            network=spec.network,
            operator_uid=operator_uid,
            subordinate=subordinate,
            writable=frozenset(writable),
            mounts=frozenset(mounts),
            loopback_nonce=loopback_nonce,
            abstract_nonce=abstract_nonce,
            keyring_checked=keyring_checked,
            sentinels=sentinels,
        )


def subordinate_ranges(text: str, user: str, uid: int) -> tuple[tuple[int, int], ...]:
    """The operator's ranges in an ``/etc/subuid`` text (``name:start:count`` lines)."""
    ranges = []
    for line in text.splitlines():
        parts = line.strip().split(":")
        if len(parts) != 3 or parts[0] not in (user, str(uid)):
            continue
        try:
            ranges.append((int(parts[1]), int(parts[2])))
        except ValueError:
            continue
    return tuple(ranges)


def _status_fields(text: str) -> dict[str, str]:
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def _id_map(text: str) -> list[tuple[int, int, int]]:
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            rows.append((int(parts[0]), int(parts[1]), int(parts[2])))
    return rows


def _outside(rows: list[tuple[int, int, int]], inside: int) -> int | None:
    for start, outside, count in rows:
        if start <= inside < start + count:
            return outside + inside - start
    return None


def _unescape(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def _mounts(text: str) -> list[tuple[str, set[str]]]:
    """``/proc/self/mountinfo``'s mount points and per-mount options, in order."""
    mounts = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 6:
            mounts.append((_unescape(parts[4]), set(parts[5].split(","))))
    return mounts


def _runtime_mount(point: str) -> bool:
    return point in RUNTIME_FILES or any(
        point == root or point.startswith(root + "/") for root in _KERNEL_ROOTS
    )


def _judge_identity(obs: Mapping[str, object], expect: Expectation) -> list[Finding]:
    findings: list[Finding] = []
    status = _status_fields(str(obs.get("status") or ""))
    uids = status.get("Uid", "").split()
    gids = status.get("Gid", "").split()
    for label, ids, wanted in (("UIDs", uids, expect.uid), ("GIDs", gids, expect.gid)):
        if not ids or any(value != str(wanted) for value in ids):
            findings.append(
                Finding(
                    ContainerCapability.AGENT_USER,
                    f"the command ran with {label} {ids or 'unknown'}, not {wanted}",
                )
            )
    # The map as the host reads it for the container's PID 1 (HOST_UID_MAP):
    # inside, /proc/self/uid_map is relative to the parent user namespace,
    # which a nested namespace (Podman's keep-id) makes something else than
    # the host's.
    rows = _id_map(str(obs.get(HOST_UID_MAP) or ""))
    if not rows:
        findings.append(
            Finding(
                ContainerCapability.AGENT_USER,
                "the container's UID map was not read from the host",
            )
        )
    elif any(outside == 0 for _, outside, _ in rows):
        findings.append(
            Finding(ContainerCapability.ROOTLESS, "host root is mapped into the container")
        )
    outside = _outside(rows, expect.uid)
    if rows and outside is None:
        findings.append(Finding(ContainerCapability.AGENT_USER, f"UID {expect.uid} is not mapped"))
    elif outside is not None and outside == expect.operator_uid:
        findings.append(
            Finding(
                ContainerCapability.AGENT_USER,
                "the agent's UID maps to the operator's own (a keep-id-style mapping)",
            )
        )
    elif outside is not None and not any(
        start <= outside < start + count for start, count in expect.subordinate
    ):
        findings.append(
            Finding(
                ContainerCapability.AGENT_USER,
                f"the agent's UID maps to host UID {outside}, not a subordinate one",
            )
        )
    for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
        value = status.get(key, "")
        try:
            held = int(value, 16)
        except ValueError:
            held = -1
        if held != 0:
            findings.append(
                Finding(ContainerCapability.NO_CAPABILITIES, f"{key} is {value or 'unknown'}")
            )
    if status.get("NoNewPrivs") != "1":
        findings.append(
            Finding(
                ContainerCapability.NO_NEW_PRIVILEGES,
                f"NoNewPrivs is {status.get('NoNewPrivs', 'unknown')}",
            )
        )
    if status.get("Seccomp") != "2":
        findings.append(
            Finding(
                ContainerCapability.SECCOMP,
                f"Seccomp is {status.get('Seccomp', 'unknown')}, not a filter",
            )
        )
    return findings


def _judge_filesystem(obs: Mapping[str, object], expect: Expectation) -> list[Finding]:
    findings: list[Finding] = []
    mounts = _mounts(str(obs.get("mountinfo") or ""))
    roots = [options for point, options in mounts if point == "/"]
    if not roots or "ro" not in roots[-1]:
        findings.append(
            Finding(ContainerCapability.READ_ONLY_ROOT, "the root is not mounted read-only")
        )
    for point, _options in mounts:
        if point != "/" and point not in expect.mounts and not _runtime_mount(point):
            findings.append(
                Finding(
                    ContainerCapability.MOUNTS,
                    f"{point} is mounted, and the policy made no mount there",
                )
            )
    for point in sorted(expect.mounts):
        if point not in {p for p, _ in mounts}:
            findings.append(Finding(ContainerCapability.MOUNTS, f"{point} is not mounted"))
    writable_obs = obs.get("writable")
    writable = writable_obs if isinstance(writable_obs, Mapping) else {}
    for place, ok in sorted(writable.items()):
        allowed = place in expect.writable or place in RUNTIME_TEMP
        if ok is True and not allowed:
            findings.append(
                Finding(ContainerCapability.WRITABLE_PLACES, f"the agent can write {place}")
            )
    for place in sorted(expect.writable):
        if writable.get(place) is not True:
            findings.append(
                Finding(ContainerCapability.WRITABLE_PLACES, f"the agent cannot write {place}")
            )
    pids = str(obs.get("pids_max") or "").strip()
    if pids != str(expect.pids_limit):
        findings.append(
            Finding(ContainerCapability.PID_LIMIT, f"pids.max is {pids or 'unreadable'}")
        )
    return findings


_KEY_ABSENT = frozenset({"ENOKEY", "EPERM", "EACCES", "ENOSYS", "EKEYREVOKED", "EKEYEXPIRED"})


def _judge_network(obs: Mapping[str, object], expect: Expectation) -> list[Finding]:
    findings: list[Finding] = []
    if expect.keyring_checked:
        keyring = obs.get("keyring")
        results = keyring if isinstance(keyring, Mapping) else {}
        if not results:
            findings.append(
                Finding(ContainerCapability.SESSION_KEYRING, "the keyring was not searched")
            )
        for where, result in sorted(results.items()):
            if result not in _KEY_ABSENT:
                findings.append(
                    Finding(
                        ContainerCapability.SESSION_KEYRING,
                        f"searching the {where} keyring for the host's key gave {result}",
                    )
                )
    connects = obs.get("connect")
    attempts = [a for a in connects if isinstance(a, Mapping)] if isinstance(connects, list) else []
    if expect.loopback_nonce and not attempts:
        findings.append(
            Finding(ContainerCapability.HOST_LOOPBACK, "no connection to the listener was tried")
        )
    for attempt in attempts:
        received = str(attempt.get("received") or "")
        if expect.loopback_nonce and expect.loopback_nonce in received:
            findings.append(
                Finding(
                    ContainerCapability.HOST_LOOPBACK,
                    f"{attempt.get('host')}:{attempt.get('port')} reached the host's loopback "
                    "listener",
                )
            )
    abstract = obs.get("abstract")
    if expect.abstract_nonce:
        if not isinstance(abstract, Mapping) or not abstract.get("result"):
            findings.append(
                Finding(
                    ContainerCapability.HOST_ABSTRACT_SOCKET,
                    "no connection to the abstract socket was tried",
                )
            )
        received = str(abstract.get("received") or "") if isinstance(abstract, Mapping) else ""
        if expect.abstract_nonce in received:
            findings.append(
                Finding(
                    ContainerCapability.HOST_ABSTRACT_SOCKET,
                    "the host's abstract socket answered",
                )
            )
    if expect.network is ContainerNetwork.NONE:
        interfaces = obs.get("interfaces")
        names = sorted(interfaces) if isinstance(interfaces, list) else []
        if names != ["lo"]:
            findings.append(Finding(ContainerCapability.NO_ROUTE, f"interfaces: {names}"))
        routes = obs.get("routes")
        if not isinstance(routes, list) or routes:
            findings.append(Finding(ContainerCapability.NO_ROUTE, f"routes: {routes}"))
    return findings


def _judge_sentinels(obs: Mapping[str, object], expect: Expectation) -> list[Finding]:
    findings: list[Finding] = []
    hits = obs.get("sentinels")
    found = hits if isinstance(hits, Mapping) else {}
    if expect.sentinels and not isinstance(hits, Mapping):
        findings.append(Finding(ContainerCapability.CREDENTIALS, "the sentinels were not searched"))
    for index, allowed in enumerate(expect.sentinels):
        places = found.get(str(index))
        seen = set(places) if isinstance(places, list) else set()
        for place in sorted(seen - allowed):
            findings.append(
                Finding(
                    ContainerCapability.CREDENTIALS, f"fake credential {index} found at {place}"
                )
            )
        for place in sorted(allowed - seen):
            findings.append(
                Finding(
                    ContainerCapability.CREDENTIALS,
                    f"projected fake credential {index} missing at {place}",
                )
            )
    return findings


def judge(observation: str | Mapping[str, object], expect: Expectation) -> tuple[Finding, ...]:
    """Each item ``observation`` (what :mod:`autoforge.container_measure` printed) does not prove.

    The prober adds :data:`HOST_UID_MAP`, the container's UID map as the
    host reads it, since the one seen inside is relative to the parent user
    namespace. Empty when the container showed everything
    :class:`Expectation` asks. An observation that does not parse proves
    nothing: every item is a finding.
    """
    if isinstance(observation, str):
        try:
            parsed = json.loads(observation)
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict):
            return tuple(
                Finding(capability, "the measurement printed no observation")
                for capability in ContainerCapability
            )
        observation = parsed
    findings = [
        *_judge_identity(observation, expect),
        *_judge_filesystem(observation, expect),
        *_judge_network(observation, expect),
        *_judge_sentinels(observation, expect),
    ]
    return tuple(findings)
