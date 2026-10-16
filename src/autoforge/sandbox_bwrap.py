"""The Linux backend of the outer sandbox: Bubblewrap (ADR 0005 D13.4, #167).

The only module that knows a ``bwrap`` flag. :func:`prepare` turns a
:class:`~autoforge.sandbox.LaunchDescription` and a command into a
:class:`BwrapLaunch` that the executor spawns in place of the command
(``executor.py``, ``executor_duplex.py``); it never spawns the command
itself, and it creates no process owner: ``bwrap`` is the executor's
child for exactly one invocation, in a session of its own as any command
is, and ADR 0002's group kill, exit grace and containment apply to it
unchanged (D10.2).

What every launch gets, whatever the description says:

- user, mount, PID and IPC namespaces (``--unshare-user``,
  ``--unshare-pid``, ``--unshare-ipc``; D10.1), a new procfs and the
  minimal ``/dev``; ``--unshare-net`` only for a description that asks for
  an isolated network, never by default (D9.1);
- an empty tmpfs root (``bwrap``'s own) with exactly the description's
  mounts, in its order, and every mask made read-only after its carve-outs
  are in (``--remount-ro`` last; ADR 0005 §1.2);
- ``--clearenv`` followed by exactly the computed environment (the
  executor's selection with the description's overrides), handed over in a
  memory file through ``--args`` with every other option, so no value
  appears on a command line any process can read (``/proc/<pid>/cmdline``);
- ``--die-with-parent``: ``PR_SET_PDEATHSIG``, which fires when the
  *thread* that spawned ``bwrap`` exits, so a sandboxed launch is spawned
  only from a thread that outlives it (D10.6);
- in the launch's own process, before ``bwrap`` runs and never in the
  controller: a new anonymous session keyring (D10.1) and a Landlock domain
  scoped to abstract Unix sockets (``LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET``,
  Landlock ABI 6; D9.1), so a host abstract socket (an X server, a session
  bus) cannot be connected from inside;
- ``--json-status-fd``: whether the command ran is read from its status
  records after the fact (D10.8, :meth:`BwrapLaunch.outcome`);
- never ``--disable-userns`` (D11.1), ``--new-session`` (D10.2) or
  ``--as-pid-1``.

A launch that cannot be established is a
:class:`~autoforge.errors.SandboxLaunchRefusedError`, never a run without
the sandbox: a platform that is not Linux, no ``bwrap`` on the controller's
``PATH``, a kernel without the Landlock scope, an architecture whose
keyring call is unknown (D14.2), and, after the spawn, a setup failure that
``bwrap`` reported (a bind source gone, an executable the sandbox cannot
run, a namespace refused).

Teardown inside the namespace is the kernel's: when the command exits, or
``bwrap`` is killed, everything still inside is SIGKILLed at once, so inner
processes get no exit grace and no kill grace, and they are never counted
(D10.3-D10.5, ADR 0002 §4e).

:func:`probe` is the minimal launch that proves a host can enforce
(D14.1's shape) and names the :class:`Capability` a host lacks; its
operator-facing uses, the engine's pre-launch gate and ``doctor``, are
#169's (D13.5).
"""

from __future__ import annotations

import ctypes
import enum
import errno
import json
import mmap
import os
import platform
import shutil
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import NoReturn

from .errors import SandboxLaunchRefusedError, SandboxPolicyError
from .redaction import redact
from .sandbox import (
    TEARDOWN_UNCOUNTED,
    Access,
    Bind,
    Dir,
    LaunchDescription,
    Link,
    Mask,
    NetworkPosition,
    Origin,
    Overlay,
    OverlayRequest,
    PrivateTmpfs,
    ProjectionRequest,
    SandboxFacts,
    SandboxRequest,
    SysPolicy,
    compile_launch,
)

BACKEND = "bubblewrap"
EXECUTABLE = "bwrap"

# Landlock (linux/landlock.h). The syscall numbers are the same on every
# architecture that has the generic table, which is every one Linux added
# them to after 5.13.
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET = 1
# The first ABI with the abstract-socket scope (Linux 6.12).
_LANDLOCK_MIN_ABI = 6
# keyctl(2): its number is per architecture.
_SYS_KEYCTL = {
    "x86_64": 250,
    "amd64": 250,
    "aarch64": 219,
    "arm64": 219,
    "riscv64": 219,
    "loongarch64": 219,
    "ppc64le": 271,
    "ppc64": 271,
    "s390x": 280,
}
_KEYCTL_JOIN_SESSION_KEYRING = 1
_PR_SET_NO_NEW_PRIVS = 38
# The steps of :meth:`_ProcessScope.apply`, in order: the call, and what the
# launch's process could not do when it failed.
_SCOPE_STEPS = (
    ("keyctl(KEYCTL_JOIN_SESSION_KEYRING)", "join a new session keyring"),
    ("prctl(PR_SET_NO_NEW_PRIVS)", "set no_new_privs, which the Landlock scope requires"),
    ("landlock_create_ruleset", "create its Landlock abstract-socket scope"),
    ("landlock_restrict_self", "restrict itself to the Landlock abstract-socket scope"),
)

# Bounds on what is read back from ``bwrap``.
_MAX_STATUS_BYTES = 64 * 1024
_MAX_REASON_CHARS = 500
_PROBE_TIMEOUT_SECONDS = 30


class _LandlockAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


def _refused(reason: str, facts: SandboxFacts | None = None) -> SandboxLaunchRefusedError:
    return SandboxLaunchRefusedError(f"the sandbox cannot be established: {reason}", facts)


@dataclass(frozen=True)
class _BackendInfo:
    version: str
    has_size: bool
    has_perms: bool
    has_overlay: bool


_INFO_LOCK = threading.Lock()
_INFO: dict[tuple[str, int, int], _BackendInfo] = {}


def _backend_info(path: str) -> _BackendInfo:
    """``bwrap``'s version and the options this build has, once per binary.

    Read with ``--version`` and ``--help`` through the executor, uncontained
    and outside any sandbox, before the invocation's own containment begins.
    Cached for the controller process, keyed by the binary's identity, so a
    replaced binary is read again.
    """
    try:
        st = os.stat(path)
    except OSError as exc:
        raise _refused(f"{path} cannot be read ({exc.strerror})") from None
    key = (path, st.st_ino, st.st_mtime_ns)
    with _INFO_LOCK:
        cached = _INFO.get(key)
    if cached is not None:
        return cached
    from . import executor  # the executor imports this module

    def run(flag: str) -> str:
        res = executor.execute(
            executor.ExecutionRequest(
                command=[path, flag], timeout_seconds=10, env_allowlist=(), max_output_bytes=65536
            )
        )
        if not res.ok:
            raise _refused(f"{path} {flag} failed (exit {res.exit_code})")
        return res.stdout

    version_text = run("--version").strip()
    help_text = run("--help")
    version = version_text.split()[-1] if version_text else ""
    info = _BackendInfo(
        version=version[:64],
        has_size="--size" in help_text,
        has_perms="--perms" in help_text,
        has_overlay="--overlay" in help_text,
    )
    with _INFO_LOCK:
        _INFO[key] = info
    return info


def _landlock_abi(syscall: Callable[..., int]) -> int:
    """The kernel's Landlock ABI version; 0 when Landlock is absent or disabled."""
    abi = syscall(
        ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET),
        ctypes.c_void_p(None),
        ctypes.c_size_t(0),
        ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION),
    )
    return max(int(abi), 0)


class _ProcessScope:
    """What the launch's own process does between fork and exec (D9.1, D10.1).

    Everything it calls is resolved before the fork, so the child runs only
    foreign calls on prepared arguments: join a new anonymous session
    keyring, set ``no_new_privs`` (Landlock's precondition, and what
    ``bwrap`` sets for the command anyway), and restrict itself to a
    Landlock domain that handles no filesystem or network access and only
    scopes abstract Unix sockets. ``bwrap`` and the command inherit the
    domain and the keyring; the controller keeps neither. A failing step
    raises, which fails the spawn (:meth:`BwrapLaunch.spawn_error`).

    Python replaces an exception raised between fork and exec with a
    generic ``SubprocessError`` ("Exception occurred in preexec_fn."),
    errno included, so the step that failed and its errno are also written
    to a shared anonymous mapping, which the forked child and the
    controller both see: :meth:`failure` reads them back after the spawn.
    """

    def __init__(self) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        self._syscall = libc.syscall
        self._syscall.restype = ctypes.c_long
        self._prctl = libc.prctl
        machine = platform.machine().lower()
        keyctl = _SYS_KEYCTL.get(machine)
        if keyctl is None:
            raise _refused(
                f"the session keyring call is not known for this architecture ({machine})"
            )
        abi = _landlock_abi(self._syscall)
        if abi < _LANDLOCK_MIN_ABI:
            raise _refused(
                f"the kernel's Landlock ABI is {abi or 'unavailable'}; the abstract-socket scope "
                f"needs ABI {_LANDLOCK_MIN_ABI} (Linux 6.12) with Landlock enabled"
            )
        # Every argument is built here, so the forked child allocates as
        # little as it can: other threads of the controller may hold the
        # allocator's locks at the fork.
        self._attr = _LandlockAttr(0, 0, _LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET)
        self._join = (
            ctypes.c_long(keyctl),
            ctypes.c_long(_KEYCTL_JOIN_SESSION_KEYRING),
            ctypes.c_void_p(None),
        )
        self._no_new_privs = (
            ctypes.c_int(_PR_SET_NO_NEW_PRIVS),
            ctypes.c_ulong(1),
            ctypes.c_ulong(0),
            ctypes.c_ulong(0),
            ctypes.c_ulong(0),
        )
        self._create = (
            ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET),
            ctypes.byref(self._attr),
            ctypes.c_size_t(ctypes.sizeof(self._attr)),
            ctypes.c_uint32(0),
        )
        self._restrict = ctypes.c_long(_SYS_LANDLOCK_RESTRICT_SELF)
        self._no_flags = ctypes.c_uint32(0)
        # MAP_SHARED and anonymous: byte 0 is the failed step's number plus
        # one (0: none failed), byte 1 its errno, capped at 255.
        self._record = mmap.mmap(-1, 2)

    def apply(self) -> None:
        """Runs in the forked child, before ``bwrap`` is executed."""
        if self._syscall(*self._join) < 0:
            self._fail(0)
        if self._prctl(*self._no_new_privs) != 0:
            self._fail(1)
        fd = self._syscall(*self._create)
        if fd < 0:
            self._fail(2)
        try:
            if self._syscall(self._restrict, ctypes.c_long(fd), self._no_flags) != 0:
                self._fail(3)
        finally:
            os.close(fd)

    def _fail(self, step: int) -> NoReturn:
        """Record ``step`` and its errno for the controller, then fail the spawn."""
        code = ctypes.get_errno()
        self._record[1] = min(max(code, 0), 255)
        self._record[0] = step + 1
        raise OSError(code, _SCOPE_STEPS[step][0])

    def failure(self) -> str:
        """The step that failed in the child, by its call and errno; "" when none is recorded."""
        if self._record.closed or not 0 < self._record[0] <= len(_SCOPE_STEPS):
            return ""
        call, purpose = _SCOPE_STEPS[self._record[0] - 1]
        code = self._record[1]
        why = f"{errno.errorcode.get(code, code)}: {os.strerror(code)}" if code else "no errno"
        return f"could not {purpose}: {call} failed ({why})"

    def close(self) -> None:
        """Release the shared record. Idempotent."""
        self._record.close()


def _options(
    description: LaunchDescription,
    info: _BackendInfo,
    *,
    status_fd: int,
    cwd: str | None,
    env: Mapping[str, str],
) -> list[str]:
    """The ``bwrap`` options for ``description``, in order."""
    opts = ["--unshare-user", "--unshare-pid", "--unshare-ipc"]
    if description.network is NetworkPosition.ISOLATED:
        opts.append("--unshare-net")
    opts += ["--die-with-parent", "--json-status-fd", str(status_fd)]
    opts += ["--proc", "/proc", "--dev", "/dev"]
    if description.sys is SysPolicy.READ_ONLY:
        opts += ["--ro-bind", "/sys", "/sys"]
    masks: list[str] = []
    for mount in description.mounts:
        if isinstance(mount, Bind):
            flag = "--ro-bind" if mount.access is Access.READ_ONLY else "--bind"
            opts += [flag, mount.source, mount.target]
        elif isinstance(mount, Mask):
            if info.has_perms:
                opts += ["--perms", "0755"]
            opts += ["--tmpfs", mount.target]
            masks.append(mount.target)
        elif isinstance(mount, PrivateTmpfs):
            if info.has_perms:
                opts += ["--perms", f"{mount.mode:04o}"]
            if mount.size_bytes is not None and info.has_size:
                opts += ["--size", str(mount.size_bytes)]
            opts += ["--tmpfs", mount.target]
        elif isinstance(mount, Overlay):
            opts += ["--overlay-src", mount.lower, "--overlay", mount.upper, mount.work]
            opts.append(mount.target)
        elif isinstance(mount, Dir):
            opts += ["--dir", mount.target]
        elif isinstance(mount, Link):
            opts += ["--symlink", mount.points_to, mount.target]
    for target in masks:
        opts += ["--remount-ro", target]
    if cwd is not None:
        opts += ["--chdir", cwd]
    opts.append("--clearenv")
    for name, value in env.items():
        opts += ["--setenv", name, value]
    return opts


def _environment(description: LaunchDescription, env: Mapping[str, str]) -> dict[str, str]:
    """The command's environment: ``env`` with the description's overrides."""
    out = {name: value for name, value in env.items() if name not in description.env_unset}
    out.update(description.env_set)
    return out


def _facts(
    description: LaunchDescription,
    info: _BackendInfo | None,
    *,
    enforced: bool,
    setup_failure: str = "",
) -> SandboxFacts:
    bounds = tuple(
        (path, size if info is not None and info.has_size else None)
        for path, size in description.tmpfs_bounds()
    )
    return SandboxFacts(
        enforced=enforced,
        backend=BACKEND,
        backend_version="" if info is None else info.version,
        teardown=TEARDOWN_UNCOUNTED if enforced else "",
        network=description.network.value,
        tmpfs=bounds,
        unset_env=description.env_unset,
        setup_failure=setup_failure,
    )


class BwrapLaunch:
    """One prepared sandboxed launch: the argv to spawn and what to read back.

    Made by :func:`prepare`; the executor spawns :attr:`argv` with
    :attr:`spawn_env`, :attr:`pass_fds` and :attr:`preexec_fn`, calls
    :meth:`spawned` once the spawn returned or failed, :meth:`collect` once
    ``bwrap`` has been reaped, :meth:`outcome` to classify the end, and
    :meth:`close` in every case.
    """

    def __init__(
        self,
        description: LaunchDescription,
        info: _BackendInfo,
        executable: str,
        command: list[str],
        *,
        cwd: str | None,
        env: Mapping[str, str],
    ) -> None:
        self.description = description
        self._info = info
        self._scope = _ProcessScope()
        self._status_r, self._status_w = os.pipe()
        self._args_fd = -1
        try:
            options = _options(
                description,
                info,
                status_fd=self._status_w,
                cwd=cwd,
                env=_environment(description, env),
            )
            data = b"".join(os.fsencode(option) + b"\0" for option in options)
            self._args_fd = os.memfd_create("autoforge-bwrap-args", os.MFD_CLOEXEC)
            view = memoryview(data)
            while view:
                view = view[os.write(self._args_fd, view) :]
            os.lseek(self._args_fd, 0, os.SEEK_SET)
        except BaseException:
            self.close()
            raise
        self.argv = [executable, "--args", str(self._args_fd), "--", *command]
        # ``bwrap``'s own environment: nothing; the command's is in the options.
        self.spawn_env: dict[str, str] = {}
        self.pass_fds = (self._args_fd, self._status_w)
        self.preexec_fn: Callable[[], None] = self._scope.apply
        self._status = b""
        self._collected = False
        self._closed = False

    def spawned(self) -> None:
        """Drop the controller's copies of what only ``bwrap`` holds. Idempotent."""
        for name in ("_args_fd", "_status_w"):
            fd = getattr(self, name)
            if fd >= 0:
                os.close(fd)
                setattr(self, name, -1)

    def spawn_error(self, exc: BaseException) -> SandboxLaunchRefusedError:
        """The refusal for a failure of the launch's own process before ``bwrap`` ran.

        It names the step that failed, as the process recorded it
        (:meth:`_ProcessScope.failure`): ``exc`` is Python's generic error
        for any failure between fork and exec. A failure the process did not
        record names neither step, so :func:`classify` cannot tell which
        capability is missing; the launch is refused all the same.
        """
        failed = self._scope.failure()
        if not failed:
            failed = (
                "could not join a new session keyring or restrict itself to the Landlock "
                f"abstract-socket scope ({exc})"
            )
        return _refused(
            f"the launch's process {failed}",
            _facts(self.description, self._info, enforced=False, setup_failure=failed),
        )

    def collect(self) -> None:
        """Read the status records ``bwrap`` wrote, without waiting. Idempotent.

        Called once ``bwrap`` has been reaped, so its records are in the
        pipe; nothing inside holds the pipe (``bwrap`` does not pass it on,
        and the namespace is gone), and the read is non-blocking and bounded
        anyway.
        """
        if self._collected or self._status_r < 0:
            return
        self._collected = True
        os.set_blocking(self._status_r, False)
        chunks: list[bytes] = []
        size = 0
        while size < _MAX_STATUS_BYTES:
            try:
                chunk = os.read(self._status_r, _MAX_STATUS_BYTES - size)
            except BlockingIOError:
                break
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        self._status = b"".join(chunks)

    def _command_ran(self) -> bool:
        """Whether ``bwrap`` wrote an ``exit-code`` record: only then did the command run."""
        for line in self._status.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and "exit-code" in record:
                return True
        return False

    def outcome(
        self, *, returncode: int | None, ended_by_executor: bool, stderr: str
    ) -> SandboxFacts:
        """Classify the end of the launch (D10.8); raise a refusal for a setup failure.

        - The executor ended it (a limit, a stop, a kill before the exit):
          the command's end, whatever the records say.
        - ``bwrap`` was ended by a signal the executor did not send (the OOM
          killer, a kill from outside, the command signalling its own
          process group, which ``bwrap`` is in): the command's abnormal end,
          never a refusal, because a killed ``bwrap`` leaves the same records
          as a setup failure.
        - ``bwrap`` exited by itself with an ``exit-code`` record: the
          command ran; its status is ``bwrap``'s.
        - ``bwrap`` exited by itself without one: a setup failure. The
          command never ran, and the refusal carries ``bwrap``'s message,
          bounded and redacted.
        """
        self.collect()
        if ended_by_executor or returncode is None or returncode < 0 or self._command_ran():
            return _facts(self.description, self._info, enforced=True)
        reason = _setup_reason(stderr, returncode)
        raise _refused(
            f"{BACKEND} failed to set the sandbox up: {reason}",
            _facts(self.description, self._info, enforced=False, setup_failure=reason),
        )

    def close(self) -> None:
        """Release every descriptor, and the scope's record. Idempotent."""
        self._closed = True
        self._scope.close()
        for name in ("_args_fd", "_status_w", "_status_r"):
            fd = getattr(self, name, -1)
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, name, -1)


def _setup_reason(stderr: str, returncode: int) -> str:
    """``bwrap``'s own message about the failure: its last lines, bounded and redacted."""
    lines = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
    text = " / ".join(lines[-3:]) if lines else f"exit status {returncode}, no message"
    text = redact(text)
    if len(text) > _MAX_REASON_CHARS:
        text = text[: _MAX_REASON_CHARS - 1] + "…"
    return text


def prepare(
    description: LaunchDescription,
    command: list[str],
    *,
    cwd: str | None,
    env: Mapping[str, str],
) -> BwrapLaunch:
    """Prepare ``command`` to run inside ``description``, or refuse (D14.2).

    ``env`` is the environment the executor selected for the command; the
    description's overrides are applied over it and ``bwrap`` hands exactly
    that to the command. Nothing is spawned except ``bwrap --version`` and
    ``bwrap --help``, once per binary per controller process.
    """
    if not sys.platform.startswith("linux"):
        raise _refused(f"the outer sandbox has no backend on {sys.platform}; only Linux has one")
    executable = shutil.which(EXECUTABLE)
    if executable is None:
        raise _refused(f"{EXECUTABLE} was not found on the controller's PATH")
    info = _backend_info(executable)
    if any(isinstance(mount, Overlay) for mount in description.mounts) and not info.has_overlay:
        raise _refused(f"{executable} {info.version} cannot mount an overlay")
    return BwrapLaunch(description, info, executable, command, cwd=cwd, env=env)


# ---------------------------------------------------------------- the probe


class Capability(enum.Enum):
    """What a host must provide for a launch to be enforced (ADR 0005 D14.1).

    The probe names the first one it finds missing; the value is how the
    gate's refusal, ``doctor`` and the documentation name it.
    """

    PLATFORM = "a Linux kernel (the only platform with a backend)"
    BACKEND = "bwrap (bubblewrap) on the controller's PATH"
    LANDLOCK = "the Landlock abstract-socket scope (Landlock ABI 6, Linux 6.12 or later)"
    SESSION_KEYRING = "a new session keyring"
    USER_NAMESPACE = "an unprivileged user namespace"
    PID_NAMESPACE = "a PID namespace"
    IPC_NAMESPACE = "an IPC namespace"
    PROCFS = "a new procfs"
    BIND = "the sandbox's bind and tmpfs mounts"
    OVERLAY = "an unprivileged overlay mount"
    UNKNOWN = "a setup step the probe could not name"

    @property
    def key(self) -> str:
        """The machine-readable name: ``user_namespace``, ``procfs``, ..."""
        return self.name.lower()


# How a refusal's text names the capability it lacks, first match wins:
# this module's own refusals, then ``bwrap``'s messages (bubblewrap 0.12.0's
# strings; an older build words them the same way). A refusal of the
# launch's own process (:meth:`BwrapLaunch.spawn_error`) is matched by the
# call that failed, which the process recorded; one it did not record names
# no call and is ``UNKNOWN``.
_CLASSIFIERS: tuple[tuple[Capability, tuple[str, ...]], ...] = (
    (Capability.PLATFORM, ("has no backend on",)),
    (
        Capability.BACKEND,
        (
            "was not found on the controller's path",
            "cannot be read (",
            "--version failed",
            "--help failed",
        ),
    ),
    (Capability.SESSION_KEYRING, ("keyctl", "session keyring call")),
    (Capability.LANDLOCK, ("landlock abi", "landlock_", "pr_set_no_new_privs")),
    (
        Capability.OVERLAY,
        ("cannot mount an overlay", "overlay mount", "overlay workdir", "overlay source"),
    ),
    (
        Capability.USER_NAMESPACE,
        (
            "non-privileged user namespaces",
            "does not support user namespaces",
            "setting up uid map",
            "setting up gid map",
            "unprivileged_userns",
        ),
    ),
    (Capability.PROCFS, ("mount proc", "creating proc", "mounting proc")),
    (
        Capability.BIND,
        (
            "bind mount",
            "find source path",
            "open source",
            "type of source",
            "remount readonly",
            "mount tmpfs",
            "mount devpts",
            "mount mqueue",
            "mkdir parents",
        ),
    ),
)
# ``bwrap`` creates the user, PID and IPC namespaces in one call and words a
# refusal of any of them the same way; :func:`_narrow_namespaces` tells them
# apart.
_NAMESPACE_FAILURE = "creating new namespace failed"


def classify(reason: str) -> Capability:
    """The capability a refusal's text says is missing; ``UNKNOWN`` when none."""
    text = reason.lower()
    for capability, needles in _CLASSIFIERS:
        if any(needle in text for needle in needles):
            return capability
    return Capability.UNKNOWN


@dataclass(frozen=True)
class ProbeResult:
    """Whether this host can enforce a launch, and if not, which capability it lacks.

    ``capability`` is ``None`` exactly when ``supported``; ``reason`` is the
    refusal, bounded and redacted as every refusal is; ``version`` the
    backend's.
    """

    supported: bool
    reason: str = ""
    version: str = ""
    capability: Capability | None = None

    def describe(self) -> str:
        """One line for the gate's refusal and ``doctor``; no value but the reason."""
        if self.supported:
            return f"supported ({BACKEND} {self.version or 'version unknown'})"
        missing = (self.capability or Capability.UNKNOWN).value
        return f"unsupported: this host cannot provide {missing} ({self.reason})"


def _true() -> str:
    return "/usr/bin/true" if os.path.exists("/usr/bin/true") else "/bin/true"


def _narrow_namespaces(executable: str) -> Capability:
    """Which namespace a refusal ``bwrap`` words generically was about.

    Each is tried on its own, in the order a launch needs them, with the
    trivial command, the host root bound read-only and nothing else: the
    first that ``bwrap`` refuses is named. Uncontained and outside any
    sandbox, like ``--version``; nothing is written.
    """
    from . import executor  # the executor imports this module

    steps = (
        (Capability.USER_NAMESPACE, ["--unshare-user"]),
        (Capability.PID_NAMESPACE, ["--unshare-user", "--unshare-pid"]),
        (Capability.IPC_NAMESPACE, ["--unshare-user", "--unshare-pid", "--unshare-ipc"]),
    )
    for capability, flags in steps:
        try:
            result = executor.execute(
                executor.ExecutionRequest(
                    command=[executable, *flags, "--ro-bind", "/", "/", _true()],
                    env_allowlist=(),
                    timeout_seconds=_PROBE_TIMEOUT_SECONDS,
                    max_output_bytes=65536,
                )
            )
        except Exception:
            return Capability.UNKNOWN
        if result.timed_out or result.exit_code != 0:
            return capability
    return Capability.UNKNOWN


def _unsupported(reason: str) -> ProbeResult:
    capability = classify(reason)
    if capability is Capability.UNKNOWN and _NAMESPACE_FAILURE in reason.lower():
        executable = shutil.which(EXECUTABLE)
        if executable is not None:
            capability = _narrow_namespaces(executable)
    return ProbeResult(False, reason, "", capability)


def probe(home: str | None = None, environ: Mapping[str, str] | None = None) -> ProbeResult:
    """Run one trivial command under a description of a real launch's shape (D14.1).

    User, PID, mount and IPC namespaces, a new procfs, a minimal ``/dev``, a
    read-only bind, a mask with a carve-out, an overlay with a writable upper
    directory, the Landlock scope, a new session keyring and the status
    descriptor. Writes nothing outside a temporary directory it removes.
    A refusal is typed: :attr:`ProbeResult.capability` names what the host
    lacks, read from the refusal's text, and a namespace refusal ``bwrap``
    words generically is narrowed by trying each namespace alone.
    """
    from . import executor  # the executor imports this module

    environ = os.environ if environ is None else environ
    home = home or environ.get("HOME", "")
    true = _true()
    with tempfile.TemporaryDirectory(prefix="autoforge-sandbox-probe-") as raw:
        base = os.path.realpath(raw)
        for rel in ("ro/masked/carved", "ro/lower", "upper", "work"):
            os.makedirs(os.path.join(base, rel))
        request = SandboxRequest(
            home=home,
            controller_env=environ,
            projections=(
                ProjectionRequest(os.path.join(base, "ro"), Access.READ_ONLY, Origin.ENGINE),
                ProjectionRequest(
                    os.path.join(base, "ro/masked/carved"),
                    Access.WRITABLE,
                    Origin.ENGINE,
                    carve_out=True,
                ),
            ),
            masks=(os.path.join(base, "ro/masked"),),
            overlays=(
                OverlayRequest(
                    os.path.join(base, "ro/lower"),
                    os.path.join(base, "upper"),
                    os.path.join(base, "work"),
                ),
            ),
            tmp_size_bytes=1024 * 1024,
        )
        try:
            description = compile_launch(request)
            result = executor.execute(
                executor.ExecutionRequest(
                    command=[true],
                    env_allowlist=(),
                    timeout_seconds=_PROBE_TIMEOUT_SECONDS,
                    sandbox=description,
                )
            )
        except (SandboxPolicyError, SandboxLaunchRefusedError) as exc:
            return _unsupported(str(exc))
        finally:
            _make_removable(os.path.join(base, "work"))
        if result.exit_code != 0:
            return ProbeResult(
                False, f"the probe command exited {result.exit_code}", "", Capability.UNKNOWN
            )
        version = result.sandbox.backend_version if result.sandbox is not None else ""
        return ProbeResult(True, "", version)


def _make_removable(path: str) -> None:
    """overlayfs leaves its work directory's entries mode 000; let the cleanup in."""
    for root, dirs, _files in os.walk(path):
        for name in dirs:
            try:
                os.chmod(os.path.join(root, name), 0o700)
            except OSError:
                pass
