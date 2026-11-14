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
  The staging root is never a path alone: it is a :class:`StagingRoot`,
  the launch's own ``<git common dir>/autoforge/sandbox/<key>/stage/<launch
  id>``, proved to lie in the controller's own common dir, in directories
  the operator alone owns and can write, and apart from every checkout.

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
import errno
import ipaddress
import json
import os
import posixpath
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

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
# storage (``<storage>/containers/<id>/`` for Docker,
# ``.../overlay-containers/<id>/userdata/`` for Podman), never from an
# operator path: the mount check admits each only from a directory named by
# the measured container's own id.
RUNTIME_FILES = frozenset({"/etc/hosts", "/etc/hostname", "/etc/resolv.conf", "/run/.containerenv"})
# The device nodes a rootless runtime binds from the host's ``/dev``, since
# it cannot make one: the only host device a container gets.
RUNTIME_DEVICES = frozenset({"null", "zero", "full", "tty", "random", "urandom"})
# The root filesystem's types: the runtime's overlay of the image, never a
# host directory.
ROOT_FILESYSTEMS = frozenset({"overlay", "fuse.fuse-overlayfs"})
# The kernel's own trees, which the runtime mounts and masks itself; what it
# may mount below them is a closed list (:func:`judge`). The sentinel search
# leaves exactly these out.
KERNEL_ROOTS = frozenset({"/proc", "/sys", "/dev"})
# What the ``/dev`` a runtime makes holds, and nothing else. The sentinel
# search leaves ``/dev`` out, so its listing is what shows a host tmpfs
# bound there: each device node (:data:`RUNTIME_DEVICES`) and each of these
# directories must be a mount of its own, which the judge admits on its own
# terms (the host's node under its own name; a ``devpts`` holding only
# ``ptmx``, the IPC namespace's empty ``shm`` and ``mqueue``), and each of
# the symbolic links every OCI runtime adds must point where it does.
DEV_MOUNTS = frozenset({"pts", "shm", "mqueue"})
DEV_LINKS: Mapping[str, str] = MappingProxyType(
    {
        "fd": "/proc/self/fd",
        "stdin": "/proc/self/fd/0",
        "stdout": "/proc/self/fd/1",
        "stderr": "/proc/self/fd/2",
        "core": "/proc/kcore",
        "ptmx": "pts/ptmx",
    }
)
# The container's own cgroup tree, the one mount below ``/sys`` that is not
# sysfs's or a mask: cgroup v2, read-only, rooted at its cgroup namespace's
# root. A rootless runtime enforces no PID limit on cgroup v1, so its
# controller mounts are refused with the rest.
CGROUP_TARGET = "/sys/fs/cgroup"

# The controller's sandbox directories (ADR 0005 D8.4, as
# ``sandbox_policy.SANDBOX_DIR``) and, in an issue's, its launches' staging
# roots (ADR 0013 D12); the probes' directory is a sibling of every issue's.
SANDBOX_LAYOUT = "autoforge/sandbox"
STAGE_DIR = "stage"
PROBE_DIR = "probe"

# Where a container could reach the host's loopback through (D9): its own
# loopback, in both families, where a port forwarder could carry a
# connection out; the runtimes' host aliases, by name, wherever the
# runtime's ``/etc/hosts`` or DNS gives them; and for rootless Docker,
# RootlessKit's host address, which its ``--disable-host-loopback`` closes.
# The measurement adds each default gateway. Every one must be refused
# conclusively (:func:`judge`).
LOOPBACK_ADDRESSES = ("127.0.0.1", "::1")
HOST_ALIASES = ("host.containers.internal", "host.docker.internal")
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


# RootlessKit's address for the host in rootless Docker's namespace
# (slirp4netns's and pasta's ``10.0.2.2``); Podman's pasta maps its own host
# address under ``host.containers.internal``, which the alias covers.
_RUNTIME_HOST_ADDRESSES = {RuntimeProduct.DOCKER: ("10.0.2.2",), RuntimeProduct.PODMAN: ()}


def loopback_routes(product: RuntimeProduct) -> tuple[str, ...]:
    """The addresses and names a measurement must try for ``product`` (D9).

    The measurement adds each default gateway it finds; the judge requires
    a conclusive refusal at every one, for every port the host listens on.
    """
    return (*LOOPBACK_ADDRESSES, *HOST_ALIASES, *_RUNTIME_HOST_ADDRESSES[product])


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
    operator's UID. ``git_common_dir`` is the controller's git common dir,
    resolved, which every staging root must lie in ("" for none, and then
    no staging root is bound); ``checkouts`` the controller's checkout and
    every worktree it knows, which a staging root must never hold or lie in
    (D12).
    """

    home: str
    environ: Mapping[str, str] = field(repr=False)
    uid: int
    git_common_dir: str = ""
    checkouts: tuple[str, ...] = ()

    @classmethod
    def current(cls, git_common_dir: str = "", checkouts: Iterable[str] = ()) -> HostContext:
        located = {name: os.environ[name] for name in _LOCATION_NAMES if name in os.environ}
        return cls(os.path.expanduser("~"), located, os.getuid(), git_common_dir, tuple(checkouts))


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
    if info.st_nlink != 1:
        raise _refuse(
            source,
            rule,
            "the file has another hard link, through which another local user could "
            "reach it without passing a directory only you can enter",
        )
    if _private_directory(source, host.uid) is None:
        raise _refuse(
            source,
            rule,
            "other local users can reach this file: no directory above it is one only "
            "you can enter (yours, with no search permission for its group or others, "
            "and no access-control list the mode does not show); keep it in a directory "
            "of mode 0700",
        )
    target = item.target
    if not isinstance(target, str) or not target or target.startswith("/"):
        raise _refuse(str(target), rule, "a credential file's target is a path in the home")
    if posixpath.normpath(target) != target or any(
        part in ("", ".", "..") for part in target.split("/")
    ):
        raise _refuse(target, rule, "a credential file's target is a normalized path in the home")
    _check_mount_text(target, target, rule)


# Access-control lists whose grants the mode bits do not show. A POSIX ACL's
# mask is the mode's group bits and bounds every named entry, so a mode with
# no search for group or others bounds it; an NFSv4 or rich ACL is not.
_OPAQUE_ACLS = frozenset({"system.nfs4_acl", "system.richacl"})


def _opaque_acl(path: str) -> bool:
    """Whether ``path`` may carry an ACL its mode does not show (unknown counts as yes)."""
    listxattr = getattr(os, "listxattr", None)
    if listxattr is None:
        return True  # no way to read them here (not Linux), so privacy is not established
    try:
        names = listxattr(path, follow_symlinks=False)
    except OSError as exc:
        return exc.errno not in (errno.ENOTSUP, errno.EOPNOTSUPP)
    return any(name in _OPAQUE_ACLS for name in names)


def _private_directory(path: str, uid: int) -> str | None:
    """The nearest directory above ``path`` that only ``uid`` (and root) can search.

    The file itself is readable by others, as the agent's subordinate UID
    needs, so who can read it is who can reach it: a user who cannot search
    one directory above it cannot. That directory must be ``uid``'s, so no
    one else can change its mode, with no search permission for its group
    (a POSIX ACL's named entries included, which its mask bounds) or for
    others, and no ACL the mode does not show. ``None`` when there is none.
    """
    current = posixpath.dirname(path)
    while True:
        try:
            info = os.lstat(current)
        except OSError:
            return None
        if (
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == uid
            and not info.st_mode & (stat.S_IXGRP | stat.S_IXOTH)
            and not _opaque_acl(current)
        ):
            return current
        if current == "/":
            return None
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
    UID through its bits for others, with no other hard link, and below a
    directory only the operator can search: no search permission for its
    group or others, and no ACL its mode does not show.
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
class StagingRoot:
    """A launch's staging root (ADR 0013 D12; ADR 0005 D8.4 as amended): never a path alone.

    ``<common_dir>/autoforge/sandbox/<key>/stage/<launch>``: ``common_dir``
    is the controller's git common dir, ``key`` the issue's sandbox key (its
    worktree's name, ``<n>`` or ``epic-<n>``), ``launch`` the launch's id.
    The engine alone builds one, for the launch it creates; no provider or
    operator names it, and no other directory can be expressed by one.
    :func:`check_spec` proves its common dir is the controller's own
    (:attr:`HostContext.git_common_dir`), each directory of its layout the
    operator's, a real directory nobody else can write, and the root apart
    from every checkout. A probe's staging root (ADR 0013 D6, item 9) is
    #271's to add here.
    """

    common_dir: str
    key: str
    launch: str

    @property
    def layout(self) -> str:
        """Its path below the common dir: what a measured ``/source``'s root must end with."""
        return posixpath.join(SANDBOX_LAYOUT, self.key, STAGE_DIR, self.launch)

    @property
    def path(self) -> str:
        """The host directory bound at ``/source``."""
        return posixpath.join(self.common_dir, self.layout)


@dataclass(frozen=True)
class ContainerSpec:
    """Everything a create configures that the policy decides: no raw flag, mount or option.

    ``workspace_volume`` is the launch's ``/workspace`` volume, read-only for
    the exporter; ``cache_volume`` the per-issue cache volume, a
    change-producing launch container's only ("" for none); ``source`` the
    launch's :class:`StagingRoot`, bound read-only at ``/source`` into the
    stager and launch containers in the bind form (``None`` in the copy-in
    form); ``projection`` the launch container's credentials. The tmpfs
    sizes are in bytes, ``None`` for the kernel's default.
    """

    role: ContainerRole
    network: ContainerNetwork
    workspace_volume: str
    staging: StagingMethod = StagingMethod.BIND
    cache_volume: str = ""
    source: StagingRoot | None = None
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


def _check_staging_root(root: object, host: HostContext) -> str:
    """D12 for ``/source``'s source: the launch's own staging root, and nothing else.

    Not any directory that passes the generic checks: the
    :class:`StagingRoot` of a launch, in the controller's own common dir,
    each directory from the common dir down a real one that the operator
    owns and nobody else can write (``safefs`` makes them ``0700``), the
    root resolving to itself, outside D4.3's forbidden sources and the
    home, and never holding or lying in the controller's checkout or a
    worktree. Returns its path.
    """
    rule = _RULE_BIND
    if not isinstance(root, StagingRoot):
        raise _refuse(
            str(root),
            rule,
            "the staging root is the launch's StagingRoot, which the engine builds; a path "
            "alone is never bound at /source",
        )
    if not host.git_common_dir:
        raise _refuse(
            root.path, rule, "no controller git common dir is known to prove the staging root in"
        )
    common = _resolved_self(host.git_common_dir, "the controller's git common dir", rule)
    if root.common_dir != common:
        raise _refuse(
            root.path,
            rule,
            f"the staging root is not in the controller's own git common dir {common}",
        )
    for label, name in (("sandbox key", root.key), ("launch id", root.launch)):
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise _refuse(repr(name), rule, f"the staging root's {label} is not one path component")
    if root.key == PROBE_DIR:
        raise _refuse(root.path, rule, "the probes' directory is never a launch's sandbox key")
    path = root.path
    _check_mount_text(path, path, rule)
    current = common
    for part in root.layout.split("/"):
        current = posixpath.join(current, part)
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise _refuse(
                current, rule, f"the staging root's directory cannot be checked ({exc.strerror})"
            ) from None
        if stat.S_ISLNK(info.st_mode):
            raise _refuse(
                current,
                rule,
                "the staging root passes through a symbolic link; a bind follows it, so what "
                "it names could change after the check",
            )
        if not stat.S_ISDIR(info.st_mode):
            raise _refuse(
                current, rule, "the staging root's layout holds something not a directory"
            )
        if info.st_uid != host.uid:
            raise _refuse(
                current,
                rule,
                "the operator does not own this directory of the staging root, so it is not "
                "the controller's",
            )
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise _refuse(
                current,
                rule,
                "another user can write this directory of the staging root, and could replace "
                "what it holds",
            )
    if os.path.realpath(path) != path:
        raise _refuse(path, rule, "the staging root does not resolve to itself")
    check_declared_path(path, host.home, host.environ, "the staging root")
    for checkout in host.checkouts:
        for spelling in {posixpath.normpath(checkout), os.path.realpath(checkout)}:
            holds = path == spelling or spelling.startswith(path + "/")
            # The main checkout holds its common dir, and so the staging root
            # through it: only a checkout that does not hold the common dir is
            # refused for holding the root.
            through_common = common == spelling or common.startswith(spelling + "/")
            within = path.startswith(spelling + "/") and not through_common
            if holds or within:
                raise _refuse(
                    path,
                    rule,
                    f"the staging root {'holds' if holds else 'lies in'} the checkout or "
                    f"worktree {checkout}, which is never bound",
                )
    return path


def check_spec(spec: ContainerSpec, host: HostContext) -> None:
    """The spec against D9, D10, D12 and D14, before anything is created.

    A helper container is offline, with no projection and no cache; the
    copy-in form binds nothing; the bind form binds only the launch's
    staging root (:func:`_check_staging_root`) at ``/source`` and the
    projection's files.
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
    source = ""
    if spec.source is not None:
        if spec.staging is not StagingMethod.BIND:
            raise _refuse(str(spec.source), _RULE_BIND, "the copy-in form binds nothing")
        if spec.role is ContainerRole.EXPORTER:
            raise _refuse(str(spec.source), _RULE_BIND, "the exporter binds nothing")
        source = _check_staging_root(spec.source, host)
    for size in (spec.home_size_bytes, spec.tmp_size_bytes):
        if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 1):
            raise _refuse(str(size), _RULE_SPEC, "a tmpfs size is a positive number of bytes")
    check_projection(spec.projection, role=spec.role, staging=spec.staging, host=host)
    for item in spec.projection.files:
        if source and _related(item.source, source):
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
    if spec.source is not None:
        options.append(_bind(spec.source.path, SOURCE_TARGET))
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


class MountKind(enum.Enum):
    """How the policy makes one of its mounts, which fixes what a measurement must show."""

    # A fresh tmpfs: of type tmpfs, its root the filesystem's own.
    TMPFS = "tmpfs"
    # A runtime volume: its root the volume's data directory, ``volumes/<name>/_data``.
    VOLUME = "volume"
    # A bind of a host path: its root ending with that path's tail.
    BIND = "bind"


@dataclass(frozen=True)
class PolicyMount:
    """One mount the policy makes, as a measurement must find it.

    ``source`` is a volume's name, or for a bind the tail of the host path
    its root must end with: a mount's root is its path in its filesystem,
    which a mount point or a subvolume can shorten or lengthen at the
    front, so only the tail is the same everywhere. A tail alone does not
    tell the host's file from another with the same tail, so a bind also
    carries ``identity``: its host source's device (major, minor) and inode
    as ``lstat`` reads them on the host (:func:`host_identity`), which the
    target must show from inside; ``None`` when the source could not be
    read, which proves nothing. ``read_only``: the mount must be ``ro``,
    whatever any writability probe says. ``entries``: for a tmpfs, the
    names it may hold (the mount points the policy puts in it), since only
    its content tells a fresh one from a host tmpfs bound in whole.
    """

    target: str
    kind: MountKind
    source: str = ""
    read_only: bool = False
    entries: frozenset[str] = frozenset()
    identity: tuple[int, int, int] | None = None


def _bind_tail(path: str) -> str:
    """A credential file's tail: its private directory and its name."""
    return "/".join(path.strip("/").split("/")[-2:])


def host_identity(path: str) -> tuple[int, int, int] | None:
    """``path``'s device (major, minor) and inode, by ``lstat``; ``None`` if unreadable.

    What a bind of ``path`` shows at its target from inside, whatever
    namespace reads it: the same inode on the same device. Metadata only,
    never the contents (ADR 0005 D7.2).
    """
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)


@dataclass(frozen=True)
class Expectation:
    """What one measured container must show; built from its spec (:meth:`for_spec`).

    ``operator_uid`` and ``subordinate`` are the host's (``/etc/subuid``'s
    ranges for the operator, ``(start, count)``); ``writable`` the places the
    agent must be able to write, and the only ones, with
    :data:`RUNTIME_TEMP`; ``mounts`` every mount the policy makes, each
    with the source, type and read-only option it must show;
    ``container_id`` the measured container's full id, which the runtime's
    own files must be bound from; each bind's host identity is read by
    :meth:`for_spec`, when the expectation is built. ``loopback_listeners``
    are the host's nonce listeners, ``(address, port)``, which must cover
    ``127.0.0.1`` and ``::1``; ``loopback_routes`` the addresses and names the measurement
    must have tried at each listener's port (:func:`loopback_routes`), to
    which every default gateway it reports is added; ``loopback_nonce`` and
    ``abstract_nonce`` what the host's listeners send. ``sentinels`` are
    the places each fake credential may appear, by the index the
    measurement was given it under (``env:NAME`` for a variable), and
    ``scan_roots`` and ``scan_skip`` what the search must have walked and
    may have left out.
    """

    network: ContainerNetwork
    operator_uid: int
    subordinate: tuple[tuple[int, int], ...]
    writable: frozenset[str]
    mounts: tuple[PolicyMount, ...]
    container_id: str = ""
    loopback_nonce: str = ""
    loopback_listeners: tuple[tuple[str, int], ...] = ()
    loopback_routes: tuple[str, ...] = (*LOOPBACK_ADDRESSES, *HOST_ALIASES)
    abstract_nonce: str = ""
    keyring_checked: bool = False
    sentinels: tuple[frozenset[str], ...] = ()
    scan_roots: tuple[str, ...] = ("/",)
    scan_skip: frozenset[str] = KERNEL_ROOTS
    uid: int = AGENT_UID
    gid: int = AGENT_GID
    pids_limit: int = PIDS_LIMIT

    @property
    def mount_points(self) -> frozenset[str]:
        return frozenset(mount.target for mount in self.mounts)

    @classmethod
    def for_spec(
        cls,
        spec: ContainerSpec,
        *,
        product: RuntimeProduct,
        operator_uid: int,
        subordinate: tuple[tuple[int, int], ...],
        container_id: str = "",
        loopback_nonce: str = "",
        loopback_listeners: tuple[tuple[str, int], ...] = (),
        abstract_nonce: str = "",
        keyring_checked: bool = False,
        sentinels: tuple[frozenset[str], ...] = (),
    ) -> Expectation:
        exporter = spec.role is ContainerRole.EXPORTER
        writable = {HOME_TARGET, TMP_TARGET}
        if not exporter:
            writable.add(WORKSPACE_TARGET)
        # The home holds the projection's files, each under its first component.
        home = frozenset(item.target.split("/")[0] for item in spec.projection.files)
        mounts = [
            PolicyMount(HOME_TARGET, MountKind.TMPFS, entries=home),
            PolicyMount(TMP_TARGET, MountKind.TMPFS),
            PolicyMount(
                WORKSPACE_TARGET, MountKind.VOLUME, spec.workspace_volume, read_only=exporter
            ),
        ]
        if spec.cache_volume:
            writable.add(CACHE_TARGET)
            mounts.append(PolicyMount(CACHE_TARGET, MountKind.VOLUME, spec.cache_volume))
        if spec.source is not None:
            mounts.append(
                PolicyMount(
                    SOURCE_TARGET,
                    MountKind.BIND,
                    spec.source.layout,
                    read_only=True,
                    identity=host_identity(spec.source.path),
                )
            )
        mounts.extend(
            PolicyMount(
                item.path,
                MountKind.BIND,
                _bind_tail(item.source),
                read_only=True,
                identity=host_identity(item.source),
            )
            for item in spec.projection.files
        )
        return cls(
            network=spec.network,
            operator_uid=operator_uid,
            subordinate=subordinate,
            writable=frozenset(writable),
            mounts=tuple(mounts),
            container_id=container_id,
            loopback_nonce=loopback_nonce,
            loopback_listeners=loopback_listeners,
            loopback_routes=loopback_routes(product),
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


@dataclass(frozen=True)
class _Mount:
    """One line of ``/proc/self/mountinfo``: the fields the judge reads."""

    device: str
    root: str
    point: str
    options: frozenset[str]
    fstype: str


def _mounts(text: str) -> tuple[list[_Mount], int]:
    """``/proc/self/mountinfo``'s mounts, in order, and how many lines did not parse."""
    mounts = []
    malformed = 0
    for line in text.splitlines():
        parts = line.split()
        try:
            separator = parts.index("-", 6)
        except ValueError:
            malformed += bool(line.strip())
            continue
        if separator + 1 >= len(parts):
            malformed += 1
            continue
        mounts.append(
            _Mount(
                device=parts[2],
                root=_unescape(parts[3]),
                point=_unescape(parts[4]),
                options=frozenset(parts[5].split(",")),
                fstype=parts[separator + 1],
            )
        )
    return mounts, malformed


def _below(path: str, root: str) -> bool:
    return path.startswith(root + "/")


def _ends_with(root: str, tail: str) -> bool:
    """Whether a mount's ``root`` ends with the path ``tail``, at a component boundary."""
    return bool(tail) and (root == "/" + tail or root.endswith("/" + tail))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _contents_problem(contents: object, allowed: Iterable[str] = ()) -> str:
    """What is wrong with a fresh filesystem's listing ("" for nothing).

    A host tmpfs bound in whole (the operator's ``/run/user/<uid>``, a
    tmpfs ``/tmp`` or ``/dev/shm``), or a host ``mqueue`` or ``devpts``,
    shows the same type and root in ``mountinfo`` as one the runtime makes;
    only what it holds tells them apart. A fresh one holds nothing but the
    names ``allowed`` (the mount points the policy puts in it), and a
    listing that is missing, failed or cut short proves nothing.
    """
    if not isinstance(contents, Mapping):
        return "its content was not listed, so a host filesystem bound there would go unseen"
    names, count = contents.get("names"), contents.get("count")
    if not isinstance(names, list) or not _is_int(count):
        return f"its content could not be listed ({contents.get('error') or 'no listing'})"
    extra = sorted({str(name) for name in names} - set(allowed))
    if extra or count != len(names):
        held = ", ".join(extra[:3]) or f"{count} entries, more than were listed"
        return f"it holds {held}, which a fresh one would not"
    return ""


def _dev_problem(contents: object, points: frozenset[str]) -> str:
    """What is wrong with the ``/dev`` tmpfs's listing ("" for nothing).

    The sentinel search leaves ``/dev`` out, so only its listing tells the
    runtime's from a host tmpfs bound there: nothing in it but
    :data:`RUNTIME_DEVICES` and :data:`DEV_MOUNTS`, each a mount of its own
    (``points``, which the judge checks one by one) of the kind it must be,
    and :data:`DEV_LINKS`, each a link to its fixed target. An entry that
    is no mount would show what the host tmpfs holds under that name.
    """
    problem = _contents_problem(contents, (*RUNTIME_DEVICES, *DEV_MOUNTS, *DEV_LINKS))
    if problem or not isinstance(contents, Mapping):
        return problem
    kinds, links = contents.get("kinds"), contents.get("links")
    if not isinstance(kinds, Mapping) or not isinstance(links, Mapping):
        return "what kind each entry is was not reported, so a host file there would go unseen"
    for name in sorted(str(name) for name in contents.get("names") or ()):
        kind = kinds.get(name) or "unreported"
        target = DEV_LINKS.get(name)
        if target is not None:
            if kind != "link" or links.get(name) != target:
                return f"its {name} is {kind}, not the runtime's link to {target}"
            continue
        if f"/dev/{name}" not in points:
            return (
                f"its {name} is no mount of the runtime's, so the host file there would go "
                "unseen (the sentinel search leaves /dev out)"
            )
        wanted = "char" if name in RUNTIME_DEVICES else "dir"
        if kind != wanted:
            return f"its {name} is {kind}, not a {wanted}"
    return ""


def _identity_problem(wanted: PolicyMount, seen: object) -> str:
    """Whether a bind shows its host source's own device and inode ("" if it does)."""
    if wanted.identity is None:
        return (
            f"its host source .../{wanted.source} could not be identified, so a bind of "
            "another file with the same path's tail would go unseen"
        )
    if not isinstance(seen, list) or len(seen) != 3 or not all(_is_int(part) for part in seen):
        reported = seen if isinstance(seen, str) else "not reported"
        return f"its device and inode were not measured ({reported})"
    if tuple(seen) != wanted.identity:
        return (
            f"bound from another file than the host's .../{wanted.source}: its device and "
            "inode are not the source's"
        )
    return ""


def _policy_mount_problem(
    mount: _Mount, wanted: PolicyMount, contents: object, identity: object
) -> str:
    """What is wrong with a mount at one of the policy's targets ("" for nothing)."""
    if wanted.kind is MountKind.TMPFS:
        if mount.fstype != "tmpfs" or mount.root != "/":
            return f"{mount.fstype} from {mount.root}, not a tmpfs of its own"
        problem = _contents_problem(contents, wanted.entries)
        if problem:
            return problem
    elif wanted.kind is MountKind.VOLUME:
        if not _ends_with(mount.root, f"volumes/{wanted.source}/_data"):
            return f"mounted from {mount.root}, not from the volume {wanted.source}"
    elif not _ends_with(mount.root, wanted.source):
        return f"bound from {mount.root}, not from .../{wanted.source}"
    else:
        problem = _identity_problem(wanted, identity)
        if problem:
            return problem
    if wanted.read_only and "ro" not in mount.options:
        return "mounted read-write, where the policy mounts it read-only"
    return ""


def _kernel_mount_problem(mount: _Mount, devices: Mapping[str, str], contents: object) -> str:
    """What is wrong with a mount below ``/proc`` or ``/sys`` ("" for nothing).

    The runtime masks and narrows its own procfs and sysfs: a read-only
    view of the same filesystem, an empty read-only tmpfs (listed empty), or
    the null device over a path; and it mounts the container's own cgroup
    tree, once, read-only, at :data:`CGROUP_TARGET`. Nothing else, and no
    host filesystem, is admitted there.
    """
    kernel = "/proc" if _below(mount.point, "/proc") else "/sys"
    own = "proc" if kernel == "/proc" else "sysfs"
    read_only = "ro" in mount.options
    if mount.fstype == own and mount.device == devices.get(kernel) and read_only:
        return ""
    if mount.fstype == "tmpfs" and mount.root == "/" and read_only:
        return _contents_problem(contents)
    if mount.root == "/null" and (
        mount.fstype == "devtmpfs"
        or (mount.fstype == "tmpfs" and mount.device == devices.get("/dev"))
    ):
        return ""
    if mount.fstype in ("cgroup2", "cgroup"):
        if (
            mount.point == CGROUP_TARGET
            and mount.fstype == "cgroup2"
            and mount.root == "/"
            and read_only
        ):
            return ""
        return (
            f"{mount.fstype} from {mount.root}{'' if read_only else ', read-write'}: the "
            f"runtime mounts only the container's own cgroup2 tree, read-only, at {CGROUP_TARGET}"
        )
    return f"{mount.fstype} from {mount.root}, not one the runtime makes there"


def _runtime_mount_problem(
    mount: _Mount,
    expect: Expectation,
    devices: Mapping[str, str],
    contents: object,
    points: frozenset[str],
) -> str:
    """What is wrong with a mount the policy does not make ("" for one the runtime needs)."""
    point, fstype, root = mount.point, mount.fstype, mount.root
    if point in RUNTIME_FILES:
        if not expect.container_id:
            return (
                "the container's id was not given, so the runtime's own file cannot be "
                "told from a host bind"
            )
        parts = root.split("/")
        if parts[-1] != posixpath.basename(point) or expect.container_id not in parts:
            return f"bound from {root}, not from the runtime's directory for this container"
        return ""
    fresh = root == "/"
    if point == "/proc":
        return "" if fstype == "proc" and fresh else f"{fstype} from {root}, not a procfs"
    if point == "/sys":
        if fstype == "sysfs" and fresh and "ro" in mount.options:
            return ""
        return f"{fstype} from {root}, not a read-only sysfs"
    if point == "/dev":
        if fstype == "tmpfs" and fresh:
            return _dev_problem(contents, points)
        return f"{fstype} from {root}, not a tmpfs"
    # The IPC namespace's own shm and mqueue (``--ipc=private``), made empty,
    # and a devpts instance of the container's own, holding only its ptmx
    # (no terminal is allocated): a host one bound there holds more.
    for own, fs_type, allowed in (
        ("/dev/shm", "tmpfs", ()),
        ("/dev/mqueue", "mqueue", ()),
        ("/dev/pts", "devpts", ("ptmx",)),
    ):
        if point == own:
            if fstype == fs_type and fresh:
                return _contents_problem(contents, allowed)
            return f"{fstype} from {root}, not a {fs_type} of its own"
    if _below(point, "/dev"):
        name = point.removeprefix("/dev/")
        if name in RUNTIME_DEVICES and fstype == "devtmpfs" and root == f"/{name}":
            return ""
        return f"{fstype} from {root}, not one the runtime makes there"
    if _below(point, "/proc") or _below(point, "/sys"):
        return _kernel_mount_problem(mount, devices, contents)
    return "the policy made no mount there"


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
    mounts, malformed = _mounts(str(obs.get("mountinfo") or ""))
    if malformed:
        findings.append(
            Finding(ContainerCapability.MOUNTS, f"{malformed} line(s) of mountinfo do not parse")
        )
    roots = [mount for mount in mounts if mount.point == "/"]
    if not roots or "ro" not in roots[-1].options:
        findings.append(
            Finding(ContainerCapability.READ_ONLY_ROOT, "the root is not mounted read-only")
        )
    elif roots[-1].fstype not in ROOT_FILESYSTEMS:
        findings.append(
            Finding(
                ContainerCapability.MOUNTS,
                f"the root is {roots[-1].fstype}, not the runtime's overlay of the image",
            )
        )
    policy = {mount.target: mount for mount in expect.mounts}
    devices = {mount.point: mount.device for mount in mounts if mount.point in KERNEL_ROOTS}
    points = frozenset(mount.point for mount in mounts)
    listed = obs.get("listings")
    listings = listed if isinstance(listed, Mapping) else {}
    measured = obs.get("identities")
    identities = measured if isinstance(measured, Mapping) else {}
    for mount in mounts:
        if mount.point == "/":
            continue
        wanted = policy.get(mount.point)
        contents = listings.get(mount.point)
        if wanted is not None:
            identity = identities.get(mount.point)
            problem = _policy_mount_problem(mount, wanted, contents, identity)
        else:
            problem = _runtime_mount_problem(mount, expect, devices, contents, points)
        if problem:
            findings.append(Finding(ContainerCapability.MOUNTS, f"{mount.point}: {problem}"))
    seen = {mount.point for mount in mounts}
    for point in sorted(policy):
        if point not in seen:
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


# What a searched keyring may answer when the host's key is not reachable.
_KEY_ABSENT = frozenset({"ENOKEY", "EPERM", "EACCES", "ENOSYS", "EKEYREVOKED", "EKEYEXPIRED"})
_KEYRINGS = ("session", "user", "user-session")
# What proves an address unreachable from inside: a refusal, or no path to it.
# A timeout, a reset or any other error proves nothing: a slow or lossy path
# to the host's listener looks the same.
_ROUTE_REFUSED = frozenset(
    {"ECONNREFUSED", "ENETUNREACH", "EHOSTUNREACH", "EADDRNOTAVAIL", "EACCES", "EPERM"}
)
# A name that does not exist leads nowhere; a failed lookup proves nothing.
_NO_SUCH_NAME = frozenset({"EAI_NONAME", "EAI_NODATA", "EAI_ADDRFAMILY"})
# A lookup that reached no resolver: once the container proved it has no
# interface but loopback and no route, a name it could not look up for that
# reason leads nowhere either. Any other failure (``EAI_SYSTEM``,
# ``EAI_MEMORY``, a bad request, an exception) is the measurement's, not the
# network's, and proves nothing on any network.
_NO_RESOLVER = frozenset({"EAI_AGAIN"})
_ABSTRACT_REFUSED = frozenset({"ECONNREFUSED", "EACCES", "EPERM"})


def _carries(received: object, nonce: str) -> bool:
    """Whether ``received`` holds the nonce, or the start of it (a truncated answer)."""
    if not isinstance(received, str) or not received or not nonce:
        return False
    return nonce in received or nonce.startswith(received)


def _attempts(entry: Mapping[str, object]) -> list[Mapping[str, object]]:
    attempts = entry.get("attempts")
    if not isinstance(attempts, list):
        return []
    return [attempt for attempt in attempts if isinstance(attempt, Mapping)]


def _literal(route: str) -> bool:
    """Whether ``route`` is an address (a scoped IPv6 one included), which no lookup needs."""
    try:
        ipaddress.ip_address(route.partition("%")[0])
    except ValueError:
        return False
    return True


def _route_unproved(route: str, entry: Mapping[str, object], nonce: str, offline: bool) -> str:
    """Why one tried address or name does not prove the listener unreachable ("" if it does).

    An address (both loopback families, a host address, a gateway) needs a
    conclusive attempt on every network: it is never looked up, so a lookup
    error there means no connection was measured, and no route elsewhere
    proves its own loopback unreachable. A name may instead not exist; and
    ``offline``, once the container proved it has no interface but loopback
    and no route, it may also have reached no resolver (:data:`_NO_RESOLVER`).
    """
    error = entry.get("resolve_error")
    if error is not None:
        if not isinstance(error, str):
            return "the lookup's failure was not reported"
        if _literal(route):
            return f"the address was never tried, its lookup having failed ({error})"
        if error in _NO_SUCH_NAME or (offline and error in _NO_RESOLVER):
            return ""
        return f"the name could not be resolved ({error})"
    attempts = entry.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        return "it resolved to no address that was tried"
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            return "an attempt was not reported"
        result = attempt.get("result")
        address = attempt.get("address")
        if not isinstance(result, str):
            return f"{address} gave no result"
        if result in _ROUTE_REFUSED or _carries(attempt.get("received"), nonce):
            continue  # refused; or reached, which is reported on its own
        if result == "connected":
            received = attempt.get("received")
            if attempt.get("eof") is True and isinstance(received, str) and received:
                continue  # another service, which answered without the nonce and closed
            return (
                f"{address} accepted a connection that neither sent the nonce nor answered "
                "otherwise and closed"
            )
        return f"{address} gave {result or 'no result'}"
    return ""


def _judge_loopback(obs: Mapping[str, object], expect: Expectation, offline: bool) -> list[Finding]:
    capability = ContainerCapability.HOST_LOOPBACK
    nonce = expect.loopback_nonce
    findings: list[Finding] = []
    listened = {address for address, _ in expect.loopback_listeners}
    for family in LOOPBACK_ADDRESSES:
        if family not in listened:
            findings.append(
                Finding(
                    capability,
                    f"the host had no nonce listener on {family}, so a path to it would go unseen",
                )
            )
    connects = obs.get("connect")
    entries = [e for e in connects if isinstance(e, Mapping)] if isinstance(connects, list) else []
    for entry in entries:
        for attempt in _attempts(entry):
            if _carries(attempt.get("received"), nonce):
                findings.append(
                    Finding(
                        capability,
                        f"{entry.get('host')} port {entry.get('port')} "
                        f"({attempt.get('address')}) reached the host's loopback listener",
                    )
                )
    gateways = obs.get("gateways")
    if not isinstance(gateways, list) or not all(isinstance(g, str) for g in gateways):
        findings.append(Finding(capability, "the default gateways were not reported"))
        gateways = []
    routes = dict.fromkeys((*LOOPBACK_ADDRESSES, *expect.loopback_routes, *gateways))
    for port in sorted({port for _, port in expect.loopback_listeners}):
        for route in routes:
            tried = [e for e in entries if e.get("host") == route and e.get("port") == port]
            if not tried:
                findings.append(
                    Finding(capability, f"no connection to {route} port {port} was tried")
                )
            for entry in tried:
                why = _route_unproved(route, entry, nonce, offline)
                if why:
                    findings.append(
                        Finding(capability, f"{route} port {port}: {why}, which proves nothing")
                    )
    return findings


def _judge_abstract(obs: Mapping[str, object], expect: Expectation) -> list[Finding]:
    capability = ContainerCapability.HOST_ABSTRACT_SOCKET
    abstract = obs.get("abstract")
    if not isinstance(abstract, Mapping) or not abstract.get("result"):
        return [Finding(capability, "no connection to the abstract socket was tried")]
    result = abstract.get("result")
    if result == "connected":
        # Its name is the host's random one: whatever accepted it is the host's socket.
        answered = _carries(abstract.get("received"), expect.abstract_nonce)
        return [
            Finding(
                capability,
                "the host's abstract socket accepted a connection"
                + (" and sent its nonce" if answered else ""),
            )
        ]
    if result in _ABSTRACT_REFUSED:
        return []
    return [Finding(capability, f"connecting to it gave {result}, which proves nothing")]


def _judge_keyring(obs: Mapping[str, object]) -> list[Finding]:
    findings: list[Finding] = []
    keyring = obs.get("keyring")
    results = keyring if isinstance(keyring, Mapping) else {}
    if not results:
        return [Finding(ContainerCapability.SESSION_KEYRING, "the keyring was not searched")]
    for where in _KEYRINGS:
        if where not in results:
            findings.append(
                Finding(
                    ContainerCapability.SESSION_KEYRING, f"the {where} keyring was not searched"
                )
            )
    for where, result in sorted(results.items()):
        if result not in _KEY_ABSENT:
            findings.append(
                Finding(
                    ContainerCapability.SESSION_KEYRING,
                    f"searching the {where} keyring for the host's key gave {result}",
                )
            )
    return findings


def _judge_network(obs: Mapping[str, object], expect: Expectation) -> list[Finding]:
    findings = _judge_keyring(obs) if expect.keyring_checked else []
    no_route: list[Finding] = []
    if expect.network is ContainerNetwork.NONE:
        interfaces = obs.get("interfaces")
        names = sorted(interfaces) if isinstance(interfaces, list) else []
        if names != ["lo"]:
            no_route.append(Finding(ContainerCapability.NO_ROUTE, f"interfaces: {names}"))
        routes = obs.get("routes")
        if not isinstance(routes, list) or routes:
            no_route.append(Finding(ContainerCapability.NO_ROUTE, f"routes: {routes}"))
    offline = expect.network is ContainerNetwork.NONE and not no_route
    if expect.loopback_nonce:
        findings += _judge_loopback(obs, expect, offline)
    if expect.abstract_nonce:
        findings += _judge_abstract(obs, expect)
    return findings + no_route


def _judge_scan(scan: object, expect: Expectation) -> list[Finding]:
    """The sentinel search's coverage: what it did not cover proves nothing absent there."""
    capability = ContainerCapability.CREDENTIALS
    if not isinstance(scan, Mapping):
        return [Finding(capability, "the search reported no coverage")]
    findings: list[Finding] = []
    roots, skip = scan.get("roots"), scan.get("skip")
    if not isinstance(roots, list) or not set(expect.scan_roots) <= set(roots):
        findings.append(
            Finding(capability, f"the search walked {roots}, not {list(expect.scan_roots)}")
        )
    if not isinstance(skip, list) or not set(skip) <= expect.scan_skip:
        findings.append(
            Finding(capability, f"the search left out {skip}, more than {sorted(expect.scan_skip)}")
        )
    incomplete, count = scan.get("incomplete"), scan.get("incomplete_count")
    if not isinstance(count, int) or isinstance(count, bool) or count != 0 or incomplete != []:
        examples = "; ".join(str(x) for x in incomplete[:3]) if isinstance(incomplete, list) else ""
        findings.append(
            Finding(
                capability,
                f"the search could not cover {count if isinstance(count, int) else 'some'} "
                f"place(s) ({examples or 'unreported'}), so a credential there would go unseen",
            )
        )
    return findings


def _judge_sentinels(obs: Mapping[str, object], expect: Expectation) -> list[Finding]:
    if not expect.sentinels:
        return []
    capability = ContainerCapability.CREDENTIALS
    hits = obs.get("sentinels")
    if not isinstance(hits, Mapping):
        return [Finding(capability, "the sentinels were not searched")]
    findings = _judge_scan(obs.get("scan"), expect)
    for index, allowed in enumerate(expect.sentinels):
        places = hits.get(str(index))
        if not isinstance(places, list) or not all(isinstance(p, str) for p in places):
            findings.append(Finding(capability, f"fake credential {index} was not searched for"))
            continue
        seen = set(places)
        for place in sorted(seen - allowed):
            findings.append(Finding(capability, f"fake credential {index} found at {place}"))
        for place in sorted(allowed - seen):
            findings.append(
                Finding(capability, f"projected fake credential {index} missing at {place}")
            )
    return findings


def judge(observation: str | Mapping[str, object], expect: Expectation) -> tuple[Finding, ...]:
    """Each item ``observation`` (what :mod:`autoforge.container_measure` printed) does not prove.

    The prober adds :data:`HOST_UID_MAP`, the container's UID map as the
    host reads it, since the one seen inside is relative to the parent user
    namespace. Empty when the container showed everything
    :class:`Expectation` asks. An observation that does not parse proves
    nothing: every item is a finding. An item it did not measure, or
    measured inconclusively, is a finding too: a mount it cannot attribute
    to the policy or the runtime (a bind whose device and inode are not its
    host source's, a ``/dev`` entry that is not the runtime's), a
    connection that was neither refused nor answered and closed, an address
    never tried, a search that did not cover every place.
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
