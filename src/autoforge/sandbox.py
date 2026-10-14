"""Outer sandbox: the launch description and the policy compiler (ADR 0005, #167).

A *launch description* (:class:`LaunchDescription`) says what one launch can
reach: the host paths projected into it (writable or read-only, at their own
path or relocated into the private home), the masks over subpaths of a
read-only projection, the overlays, the private home and temp, the
environment overrides for path-valued names, the network position and the
``/proc``, ``/dev`` and ``/sys`` policy. It is typed, immutable and
backend-neutral, and it knows nothing of a workflow, a provider or GitHub:
a projection is a path and an access mode, never "the worktree" or "the
read context". The Linux backend that enforces it is
:mod:`autoforge.sandbox_bwrap`; :mod:`autoforge.executor` and
:mod:`autoforge.executor_duplex` take one on a request.

The *policy compiler* (:func:`compile_launch`) turns the controller's facts
and the declared requirements, both plain data (:class:`SandboxRequest`),
into a description, applying ADR 0005's rules before anything is spawned:

- **D4.1** the root is empty; every visible path is a projection, a mask, an
  overlay, a link or a private mount the description names;
- **C1** every source is resolved with ``realpath`` on the host, and a
  relative or missing source is refused;
- **C2** a controller-owned (:attr:`Origin.ENGINE`) source must resolve to
  itself: a symbolic link in any component of it is refused;
- **C3** only directories and regular files are projected;
- **C4** a projection inside a denied root is refused unless it is an engine
  carve-out; a denied root inside a read-only projection must be masked by
  the engine; one inside a writable projection is masked when it is a
  direct child of that projection and refused when it lies deeper;
- **C5** only the engine projects inside a mask (a carve-out);
- **C6** a declared read-only projection already visible through a
  read-only engine projection is dropped and recorded; every other overlap
  with a declared projection is refused; engine entries nest and are
  ordered parents first;
- **C7** a projection appears at its host path unless its target lies in
  the private home;
- **D4.3** the forbidden sources (the operator's home and its ancestors, the
  GitHub and git credential and configuration locations, ``~/.ssh``,
  ``~/.gnupg``, keyrings and password stores, ``/run`` and
  ``$XDG_RUNTIME_DIR``) are refused for every projection, wherever it was
  declared;
- **D7.2** a keyring item is refused, and so is a writable declared
  directory on a provider's operator configuration root.

A refusal is a :class:`~autoforge.errors.SandboxPolicyError` naming the path
and the rule, never a file's contents. The compiler is pure except for
``realpath``, ``lstat`` and ``readlink`` of the paths it is given and of the
fixed system paths (``/usr``, ``/etc``, ``/bin``, ``/lib``, ``/lib64``,
``/sbin``, ``/etc/resolv.conf``); it launches nothing and writes nothing.
Which paths a launch class gets (ADR 0005 §2.5) is the engine's (#168).

:class:`SandboxFacts` is what an enforced or refused launch records:
bounded, with no environment value. The controller never reads it (D16.1).
"""

from __future__ import annotations

import enum
import os
import posixpath
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field

from .errors import SandboxPolicyError

# Path-valued names the compiler owns: they follow the private home and temp
# (ADR 0005 D8.1, D8.2), so a caller cannot set them.
_XDG_DIRS = (
    ("XDG_CONFIG_HOME", ".config"),
    ("XDG_DATA_HOME", ".local/share"),
    ("XDG_STATE_HOME", ".local/state"),
    ("XDG_CACHE_HOME", ".cache"),
)
_OWNED_ENV = frozenset({"HOME", "TMPDIR", "XDG_RUNTIME_DIR", *(name for name, _ in _XDG_DIRS)})
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

PRIVATE_TMP = "/tmp"
# The kernel filesystems the backend mounts itself; no projection goes there.
_KERNEL_ROOTS = ("/proc", "/dev", "/sys")
# The host's system runtime (ADR 0005 §2.5): read-only, and the top-level
# entries in the host's own form (links recreated as links).
_SYSTEM_DIRS = ("/usr", "/etc")
_SYSTEM_TOP = ("/bin", "/sbin", "/lib", "/lib64")
_RESOLV_CONF = "/etc/resolv.conf"

# Bounds of what a fact may carry (D10.4).
_MAX_FACT_TEXT = 1000
_MAX_FACT_NAMES = 64
_MAX_FACT_NAME = 128


class Access(enum.Enum):
    """How a projection is visible inside."""

    READ_ONLY = "read-only"
    WRITABLE = "writable"


class Origin(enum.Enum):
    """Who asked for a projection (ADR 0005 C2, C5, C6)."""

    # A controller fact (a workspace, an admin dir, a sandbox directory, the
    # read context): it must resolve to itself, it may nest inside other
    # engine projections, and only it can be a carve-out.
    ENGINE = "engine"
    # A provider declaration or an operator path: reached through links if
    # need be, never overlapping anything but a read-only engine projection
    # that already shows it.
    DECLARED = "declared"


class NetworkPosition(enum.Enum):
    """The launch's network (ADR 0005 D9.1, D9.2)."""

    # The host's network namespace, with host abstract Unix sockets
    # unreachable (the backend's Landlock scope). The default.
    HOST = "host"
    # A network namespace of its own: no loopback, no abstract socket, no
    # egress. Only when a description asks for it.
    ISOLATED = "isolated"


class ProcPolicy(enum.Enum):
    """``/proc``: always a new procfs in the launch's own PID namespace (D10.1)."""

    NEW = "new procfs"


class DevPolicy(enum.Enum):
    """``/dev``: always the backend's minimal device set (D5.1)."""

    MINIMAL = "minimal"


class SysPolicy(enum.Enum):
    """``/sys``."""

    READ_ONLY = "read-only"
    ABSENT = "absent"


@dataclass(frozen=True)
class Bind:
    """A host path visible inside: ``source`` (resolved) at ``target``."""

    source: str
    target: str
    access: Access
    is_dir: bool


@dataclass(frozen=True)
class Mask:
    """An empty directory over ``target``, read-only once its carve-outs are in."""

    target: str


@dataclass(frozen=True)
class Overlay:
    """``lower`` visible at ``target`` (its own path), writes landing in ``upper``."""

    lower: str
    upper: str
    work: str
    target: str


@dataclass(frozen=True)
class PrivateTmpfs:
    """A private, empty tmpfs at ``target`` (the home or the temp), gone with the launch.

    ``size_bytes`` is its bound; ``None`` leaves the kernel's default.
    """

    target: str
    mode: int
    size_bytes: int | None


@dataclass(frozen=True)
class Link:
    """A symbolic link at ``target`` whose contents are ``points_to``."""

    target: str
    points_to: str


Mount = Bind | Mask | Overlay | PrivateTmpfs | Link


def _clean(path: str) -> bool:
    """An absolute, normalized path with no NUL (``//x`` is not normalized here)."""
    return (
        path.startswith("/")
        and not path.startswith("//")
        and "\0" not in path
        and posixpath.normpath(path) == path
    )


@dataclass(frozen=True)
class LaunchDescription:
    """The compiled, backend-neutral description of one sandboxed launch.

    ``mounts`` are in the order they must be applied: parents before
    children, a mask after its parent and before its carve-outs (masks are
    made read-only after everything else). ``env_set`` and ``env_unset``
    are applied over the launch's environment; nothing else from the
    controller's environment reaches the command. ``covered`` names the
    declared read-only sources that were already visible and so were not
    projected again (C6). Built by :func:`compile_launch`; constructing one
    by hand is for tests, and a malformed one is refused.
    """

    mounts: tuple[Mount, ...]
    env_set: tuple[tuple[str, str], ...] = ()
    env_unset: tuple[str, ...] = ()
    network: NetworkPosition = NetworkPosition.HOST
    proc: ProcPolicy = ProcPolicy.NEW
    dev: DevPolicy = DevPolicy.MINIMAL
    sys: SysPolicy = SysPolicy.READ_ONLY
    covered: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        targets: set[str] = set()
        for mount in self.mounts:
            paths = [mount.target]
            if isinstance(mount, Bind):
                paths.append(mount.source)
            elif isinstance(mount, Overlay):
                paths += [mount.lower, mount.upper, mount.work]
            elif isinstance(mount, Link) and ("\0" in mount.points_to or not mount.points_to):
                raise SandboxPolicyError(mount.target, "malformed description", "empty link")
            for path in paths:
                if not _clean(path):
                    raise SandboxPolicyError(
                        path, "malformed description", "not an absolute, normalized path"
                    )
            if mount.target in targets:
                raise SandboxPolicyError(mount.target, "malformed description", "mounted twice")
            targets.add(mount.target)
        names = [name for name, _ in self.env_set] + list(self.env_unset)
        for name in names:
            if not _ENV_NAME_RE.match(name):
                raise SandboxPolicyError(
                    name, "malformed description", "not an environment variable name"
                )
        seen_names: set[str] = set()
        for name in names:
            if name in seen_names:
                raise SandboxPolicyError(name, "malformed description", "a variable set twice")
            seen_names.add(name)
        for _, value in self.env_set:
            if "\0" in value:
                raise SandboxPolicyError("env", "malformed description", "a value holds NUL")

    def tmpfs_bounds(self) -> tuple[tuple[str, int | None], ...]:
        """The private mounts and their size bounds (``None``: the kernel's default)."""
        return tuple(
            (mount.target, mount.size_bytes)
            for mount in self.mounts
            if isinstance(mount, PrivateTmpfs)
        )

    def inside_path(self, host_path: str) -> str | None:
        """Where the resolved host path ``host_path`` is visible inside, or ``None``.

        Through the bind or overlay that shows it, unless a deeper entry (a
        mask, a private mount, another bind) covers it there. For the
        engine's environment rewrite (ADR 0005 D8.2): a path-valued variable
        passes only when what it names is visible.
        """
        entries = list(self.mounts)
        for mount in entries:
            if isinstance(mount, Bind) and _inside(host_path, mount.source):
                inner = _rebase(host_path, mount.source, mount.target)
            elif isinstance(mount, Overlay) and _inside(host_path, mount.lower):
                inner = host_path
            else:
                continue
            if _innermost(entries, inner) is mount:
                return inner
        return None

    def resolve_inside(self, path: str) -> str | None:
        """Where the absolute ``path``, as spelled, leads inside: ``realpath`` there.

        Each component is looked up in the description as the kernel would
        look it up inside: a recreated link is followed; below a bind or an
        overlay, the host entry it shows is read (``lstat``, ``readlink``)
        and a link there is followed inside; a mount target, or a directory
        on the way to one, is a directory; anything else is absent. From the
        first absent component on, the rest is kept as spelled, as
        ``realpath`` does; ``None`` for a loop of links. For the engine's
        environment rewrite (ADR 0005 D8.2): a value whose spelling does not
        lead to :meth:`inside_path` of its host target is replaced by it.
        """
        entries = list(self.mounts)
        pending = [part for part in path.split("/") if part]
        current = "/"
        followed = 0
        while pending:
            part = pending.pop(0)
            if part == ".":
                continue
            if part == "..":
                current = posixpath.dirname(current)
                continue
            candidate = posixpath.join(current, part)
            exists, link = _lookup(entries, candidate)
            if not exists:
                return posixpath.normpath(posixpath.join(candidate, *pending))
            if link is None:
                current = candidate
                continue
            followed += 1
            if followed > _MAX_LINKS:
                return None
            if link.startswith("/"):
                current = "/"
            pending = [p for p in link.split("/") if p] + pending
        return current


# As many links as one lookup follows before it is a loop (the kernel's 40).
_MAX_LINKS = 40


def _lookup(entries: list[Mount], path: str) -> tuple[bool, str | None]:
    """Whether ``path`` (its parent resolved inside) exists inside, and the link it is."""
    covering = _innermost(entries, path)
    if isinstance(covering, Link):
        return (True, covering.points_to) if covering.target == path else (False, None)
    if any(_inside(entry.target, path) for entry in entries):
        # A mount target, or a directory the backend makes on the way to one:
        # none of them is a link (C1, C2).
        return True, None
    if isinstance(covering, Bind | Overlay):
        source = covering.source if isinstance(covering, Bind) else covering.lower
        host = _rebase(path, covering.target, source)
        try:
            if stat.S_ISLNK(os.lstat(host).st_mode):
                return True, os.readlink(host)
        except OSError:
            return False, None
        return True, None
    return False, None  # in a mask, a private mount or the empty root


TEARDOWN_UNCOUNTED = "removed by namespace teardown, uncounted"


def _bounded(text: str, limit: int = _MAX_FACT_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True)
class SandboxFacts:
    """What one launch carrying a description records (ADR 0005 D10.4, D10.8).

    ``enforced``: the backend established the description (the command ran
    inside it, or was ended inside it); False for a refusal, which then says
    why in ``setup_failure`` (bounded, redacted by its maker). ``teardown``
    is what happened to processes left inside: removed with the namespace,
    never counted (:data:`TEARDOWN_UNCOUNTED`), so a record never says
    "none". ``tmpfs`` gives each private mount's bound, ``None`` for the
    kernel's default; ``unset_env`` names the variables the description
    unset, names only. Nothing here is an environment value, and nothing
    here is read by the controller (D16.1).
    """

    enforced: bool
    backend: str
    backend_version: str = ""
    teardown: str = ""
    network: str = ""
    tmpfs: tuple[tuple[str, int | None], ...] = ()
    unset_env: tuple[str, ...] = ()
    setup_failure: str = ""

    def __post_init__(self) -> None:
        names = tuple(_bounded(name, _MAX_FACT_NAME) for name in self.unset_env)
        if len(names) > _MAX_FACT_NAMES:
            names = names[:_MAX_FACT_NAMES] + (f"... {len(names) - _MAX_FACT_NAMES} more",)
        object.__setattr__(self, "unset_env", names)
        object.__setattr__(self, "backend_version", _bounded(self.backend_version, 64))
        object.__setattr__(self, "setup_failure", _bounded(self.setup_failure))
        object.__setattr__(
            self, "tmpfs", tuple((_bounded(p, 512), s) for p, s in self.tmpfs[:_MAX_FACT_NAMES])
        )

    def record(self) -> dict[str, object]:
        """The facts as the run log writes them (``execution.json``, ``events.jsonl``)."""
        return {
            "enforced": self.enforced,
            "backend": self.backend,
            "backend_version": self.backend_version,
            "teardown": self.teardown,
            "network": self.network,
            "tmpfs": {
                path: ("kernel default" if size is None else size) for path, size in self.tmpfs
            },
            "unset_env": list(self.unset_env),
            "setup_failure": self.setup_failure,
        }


# ---------------------------------------------------------------- the request


@dataclass(frozen=True)
class ProjectionRequest:
    """One host path a launch should see.

    ``target`` is the path inside; ``None`` is the resolved source's own
    path, and anything else must lie in the private home (C7). Only an
    :attr:`Origin.ENGINE` projection may be a ``carve_out``: one that lies
    inside a mask or a denied root on purpose (C5). ``label`` names it in a
    refusal.
    """

    source: str
    access: Access
    origin: Origin
    target: str | None = None
    carve_out: bool = False
    label: str = ""


@dataclass(frozen=True)
class OverlayRequest:
    """An engine overlay: ``lower`` visible at its own path, writes into ``upper``.

    ``upper`` and ``work`` are controller-owned directories on one
    filesystem; they are layers, never visible inside. ``carve_out``: they
    may lie inside a denied root (the engine's sandbox directory).
    """

    lower: str
    upper: str
    work: str
    carve_out: bool = False
    label: str = ""


@dataclass(frozen=True)
class SandboxRequest:
    """What the compiler is given: the controller's facts and the declared needs.

    ``home`` is the operator's home as the controller's environment names it
    (the private home is mounted there, D8.1); ``controller_env`` is the
    controller's own environment, read for the locations D4.3 forbids
    (``GH_CONFIG_DIR``, ``XDG_CONFIG_HOME``, ``XDG_RUNTIME_DIR``), never the
    values a child is given. ``masks`` are engine masks, as host paths inside
    a projection; ``denied_roots`` are the engine's (C4);
    ``operator_config_roots`` are where providers load operator configuration
    from (D7.2); ``keyring_items`` are declared keyring logins, all refused.
    ``env`` and ``env_unset`` are the caller's overrides of path-valued
    names; ``HOME``, ``XDG_*`` and ``TMPDIR`` are the compiler's.
    """

    home: str
    controller_env: Mapping[str, str]
    projections: tuple[ProjectionRequest, ...] = ()
    masks: tuple[str, ...] = ()
    overlays: tuple[OverlayRequest, ...] = ()
    denied_roots: tuple[str, ...] = ()
    operator_config_roots: tuple[str, ...] = ()
    keyring_items: tuple[str, ...] = ()
    system_runtime: bool = True
    home_size_bytes: int | None = None
    tmp_size_bytes: int | None = None
    network: NetworkPosition = NetworkPosition.HOST
    sys: SysPolicy = SysPolicy.READ_ONLY
    env: tuple[tuple[str, str], ...] = ()
    env_unset: tuple[str, ...] = ()


# --------------------------------------------------------------- path helpers


def _inside(path: str, root: str) -> bool:
    """``path`` equals ``root`` or lies below it (both clean)."""
    return path == root or path.startswith(root.rstrip("/") + "/")


def _below(path: str, root: str) -> bool:
    """``path`` lies strictly below ``root``."""
    return path != root and _inside(path, root)


def _related(a: str, b: str) -> bool:
    """One of the two equals or contains the other."""
    return _inside(a, b) or _inside(b, a)


def _rebase(path: str, old: str, new: str) -> str:
    """``path`` (inside ``old``) as the same relative path inside ``new``."""
    if path == old:
        return new
    return posixpath.join(new, path[len(old.rstrip("/")) + 1 :])


def _parts(path: str) -> tuple[str, ...]:
    return tuple(part for part in path.split("/") if part)


def _kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "regular file"
    if stat.S_ISLNK(mode):
        return "symbolic link"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISFIFO(mode):
        return "FIFO"
    if stat.S_ISCHR(mode):
        return "character device"
    if stat.S_ISBLK(mode):
        return "block device"
    return "special file"


def _first_link(path: str) -> str | None:
    """The first component of ``path`` that is a symbolic link, if any."""
    current = "/"
    for part in _parts(path):
        current = posixpath.join(current, part)
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return current
        except OSError:
            return None
    return None


def _resolve_loose(path: str) -> str:
    """``realpath`` without requiring the path to exist (for rule anchors)."""
    return os.path.realpath(path)


@dataclass
class _Proj:
    source: str
    target: str
    access: Access
    origin: Origin
    carve_out: bool
    is_dir: bool
    label: str
    system: bool = False


@dataclass
class _Rules:
    """The fixed rule anchors of one compile: forbidden sources, denied roots."""

    home: str
    home_real: str
    forbidden: list[tuple[str, str]] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    operator_roots: list[str] = field(default_factory=list)


def _refuse(path: str, rule: str, detail: str) -> SandboxPolicyError:
    return SandboxPolicyError(path, rule, detail)


def _forbidden_sources(home: str, env: Mapping[str, str]) -> list[tuple[str, str]]:
    """D4.3's list, each resolved, with the name a refusal gives it."""
    candidates: list[tuple[str, str]] = []

    def add(value: str | None, name: str) -> None:
        if value and value.startswith("/"):
            candidates.append((value, name))

    gh_dir = env.get("GH_CONFIG_DIR")
    add(gh_dir, "the gh configuration directory ($GH_CONFIG_DIR)")
    xdg_config = env.get("XDG_CONFIG_HOME")
    if xdg_config:
        add(posixpath.join(xdg_config, "gh"), "the gh configuration directory")
        add(posixpath.join(xdg_config, "git"), "the git configuration directory")
    add(posixpath.join(home, ".config/gh"), "the gh configuration directory")
    add(posixpath.join(home, ".config/git"), "the git configuration directory")
    add(posixpath.join(home, ".git-credentials"), "the git credential store")
    add(posixpath.join(home, ".gitconfig"), "the git configuration")
    add(posixpath.join(home, ".netrc"), "~/.netrc")
    add(posixpath.join(home, ".ssh"), "~/.ssh")
    add(posixpath.join(home, ".gnupg"), "~/.gnupg")
    add(posixpath.join(home, ".local/share/keyrings"), "the keyrings")
    add(posixpath.join(home, ".password-store"), "the password store")
    add("/run", "/run")
    add(env.get("XDG_RUNTIME_DIR"), "$XDG_RUNTIME_DIR")
    resolved: list[tuple[str, str]] = []
    for path, name in candidates:
        for spelling in {posixpath.normpath(path), _resolve_loose(path)}:
            resolved.append((spelling, name))
    return resolved


def _check_forbidden(path: str, rules: _Rules, label: str) -> None:
    """D4.3: refuse a source that is or contains the home, or touches a forbidden one."""
    for home in (rules.home, rules.home_real):
        if _inside(home, path):
            raise _refuse(
                path,
                "D4.3 forbidden source",
                f"{label} is or contains the operator's home ({home})",
            )
    for forbidden, name in rules.forbidden:
        if _related(path, forbidden):
            raise _refuse(
                path,
                "D4.3 forbidden source",
                f"{label} is, contains or lies inside {name} ({forbidden})",
            )


def _resolve_engine(path: str, label: str) -> str:
    """C1 and C2 for a controller-owned path: absolute and resolving to itself."""
    if not path.startswith("/"):
        raise _refuse(path, "C1 resolved source", f"{label} is a relative path")
    norm = posixpath.normpath(path)
    if norm.startswith("//"):
        norm = "/" + norm.lstrip("/")
    link = _first_link(norm)
    if link is not None:
        raise _refuse(
            path,
            "C2 no link in a controller-owned path",
            f"{label} passes through the symbolic link {link}",
        )
    real = os.path.realpath(norm)
    if real != norm:
        raise _refuse(path, "C2 no link in a controller-owned path", f"{label} resolves to {real}")
    return norm


def _resolve_declared(path: str, label: str) -> str:
    """C1 for a declared path: absolute, then resolved through its links."""
    if not path.startswith("/"):
        raise _refuse(path, "C1 resolved source", f"{label} is a relative path")
    try:
        return os.path.realpath(path, strict=True)
    except OSError as exc:
        raise _refuse(
            path, "C1 resolved source", f"{label} cannot be resolved ({exc.strerror})"
        ) from None


def _kind_of(path: str, label: str) -> bool:
    """C3: True for a directory, False for a regular file, refused otherwise."""
    try:
        mode = os.lstat(path).st_mode
    except OSError as exc:
        raise _refuse(
            path, "C1 resolved source", f"{label} does not exist ({exc.strerror})"
        ) from None
    if stat.S_ISDIR(mode):
        return True
    if stat.S_ISREG(mode):
        return False
    raise _refuse(
        path,
        "C3 kinds",
        f"{label} is a {_kind(mode)}; only directories and regular files are projected",
    )


def _relocate(target: str, rules: _Rules, label: str) -> str:
    """C7: a declared sandbox path must lie in the private home; as its resolved spelling."""
    if not _clean(target):
        raise _refuse(target, "C7 relocations", f"{label}'s target is not a clean absolute path")
    for home in (rules.home, rules.home_real):
        if _below(target, home):
            return _rebase(target, home, rules.home_real)
    raise _refuse(
        target,
        "C7 relocations",
        f"{label} may appear elsewhere than at its host path only inside the private home "
        f"({rules.home})",
    )


def _check_target(target: str, label: str) -> None:
    for root in _KERNEL_ROOTS:
        if _inside(target, root):
            raise _refuse(target, "C7 relocations", f"{label} would land in {root}")
    if _inside(PRIVATE_TMP, target):
        raise _refuse(
            target, "C6 overlaps", f"{label} is or contains the private temp ({PRIVATE_TMP})"
        )


# ------------------------------------------------------------------ compiling


def _projection(req: ProjectionRequest, rules: _Rules) -> _Proj:
    label = req.label or f"{req.origin.value} projection"
    if req.origin is Origin.ENGINE:
        source = _resolve_engine(req.source, label)
    else:
        if req.carve_out:
            raise _refuse(req.source, "C5 carve-outs", f"{label}: only the engine carves out")
        source = _resolve_declared(req.source, label)
    is_dir = _kind_of(source, label)
    _check_forbidden(source, rules, label)
    for denied in rules.denied:
        if _inside(source, denied) and not req.carve_out:
            raise _refuse(source, "C4 denied roots", f"{label} lies in the denied root {denied}")
    if req.origin is Origin.DECLARED and req.access is Access.WRITABLE:
        if not is_dir:
            raise _refuse(
                source,
                "D7.1 no writable file",
                f"{label} is a single file; a writable declaration is a directory, since a "
                "single-file bind refuses rename",
            )
        for root in rules.operator_roots:
            if _related(source, root):
                raise _refuse(
                    source,
                    "D7.2 operator configuration root",
                    f"{label} is writable and is, contains or lies inside the operator "
                    f"configuration root {root}",
                )
    target = source if req.target is None else _relocate(req.target, rules, label)
    _check_target(target, label)
    return _Proj(source, target, req.access, req.origin, req.carve_out, is_dir, label)


def _system_runtime(rules: _Rules) -> tuple[list[_Proj], list[Link]]:
    """``/usr`` and ``/etc`` read-only, the top-level entries in the host's own
    form, and the target of ``/etc/resolv.conf`` when it lies elsewhere."""
    projections: list[_Proj] = []
    links: list[Link] = []
    for path in _SYSTEM_DIRS:
        real = os.path.realpath(path)
        if os.path.isdir(real):
            _check_forbidden(real, rules, path)
            projections.append(
                _Proj(real, real, Access.READ_ONLY, Origin.ENGINE, False, True, path, True)
            )
    for path in _SYSTEM_TOP:
        try:
            mode = os.lstat(path).st_mode
        except OSError:
            continue
        if stat.S_ISLNK(mode):
            links.append(Link(path, os.readlink(path)))
        elif stat.S_ISDIR(mode):
            _check_forbidden(path, rules, path)
            projections.append(
                _Proj(path, path, Access.READ_ONLY, Origin.ENGINE, False, True, path, True)
            )
    try:
        is_link = stat.S_ISLNK(os.lstat(_RESOLV_CONF).st_mode)
    except OSError:
        is_link = False
    if is_link:
        target = os.path.realpath(_RESOLV_CONF)
        visible = any(_inside(target, p.source) for p in projections)
        if not visible and os.path.isfile(target) and not os.path.islink(target):
            # The one system exception to D4.3: the resolver file under /run.
            rules_without_run = _Rules(
                rules.home,
                rules.home_real,
                [(path, name) for path, name in rules.forbidden if path != "/run"],
            )
            _check_forbidden(target, rules_without_run, "the target of /etc/resolv.conf")
            projections.append(
                _Proj(
                    target,
                    target,
                    Access.READ_ONLY,
                    Origin.ENGINE,
                    False,
                    False,
                    "the target of /etc/resolv.conf",
                    True,
                )
            )
    return projections, links


def _home_links(home: str) -> list[Link]:
    """The links on the way to the home, recreated inside (D8.1)."""
    links: list[Link] = []
    current = "/"
    for part in _parts(home):
        step = posixpath.join(current, part)
        try:
            is_link = stat.S_ISLNK(os.lstat(step).st_mode)
        except OSError:
            is_link = False
        if is_link:
            links.append(Link(step, os.readlink(step)))
            current = os.path.realpath(step)
        else:
            current = step
    return links


def _innermost(entries: list[Mount], path: str, exclude: Mount | None = None) -> Mount | None:
    """The deepest entry whose target is or contains ``path``."""
    best: Mount | None = None
    for entry in entries:
        if entry is exclude or not _inside(path, entry.target):
            continue
        if best is None or len(entry.target) > len(best.target):
            best = entry
    return best


def _overlay(req: OverlayRequest, rules: _Rules) -> Overlay:
    label = req.label or "overlay"
    lower = _resolve_engine(req.lower, f"{label} lower directory")
    upper = _resolve_engine(req.upper, f"{label} upper directory")
    work = _resolve_engine(req.work, f"{label} work directory")
    for path, what in ((lower, "lower"), (upper, "upper"), (work, "work")):
        if not _kind_of(path, f"{label} {what} directory"):
            raise _refuse(path, "C3 kinds", f"{label}'s {what} layer is not a directory")
        _check_forbidden(path, rules, f"{label} {what} directory")
    for path in (upper, work):
        for denied in rules.denied:
            if _inside(path, denied) and not req.carve_out:
                raise _refuse(path, "C4 denied roots", f"{label} lies in the denied root {denied}")
    for a, b in ((lower, upper), (lower, work), (upper, work)):
        if _related(a, b):
            raise _refuse(b, "C6 overlaps", f"{label}'s layers {a} and {b} overlap")
    _check_target(lower, label)
    return Overlay(lower, upper, work, lower)


def compile_launch(request: SandboxRequest) -> LaunchDescription:
    """Compile ``request`` into a :class:`LaunchDescription`, or refuse it.

    Raises :class:`~autoforge.errors.SandboxPolicyError` naming the path and
    the rule; nothing is spawned or written either way.
    """
    if not _clean(request.home) or request.home == "/":
        raise _refuse(request.home, "D8.1 private home", "the home is not a clean absolute path")
    for path in (*request.denied_roots, *request.operator_config_roots):
        if not path.startswith("/"):
            raise _refuse(path, "C1 resolved source", "a rule anchor is a relative path")
    rules = _Rules(request.home, os.path.realpath(request.home))
    rules.forbidden = _forbidden_sources(request.home, request.controller_env)
    rules.denied = sorted({_resolve_loose(p) for p in request.denied_roots})
    rules.operator_roots = sorted({_resolve_loose(p) for p in request.operator_config_roots})
    for item in request.keyring_items:
        raise _refuse(
            item,
            "D7.2 keyring",
            "a login stored in a keyring cannot be projected without exposing the keyring",
        )
    for size in (request.home_size_bytes, request.tmp_size_bytes):
        if size is not None and size <= 0:
            raise _refuse(str(size), "D8.1 private home", "a tmpfs bound must be positive")

    engine: list[_Proj] = []
    declared: list[_Proj] = []
    links = _home_links(request.home)
    if request.system_runtime:
        system, system_links = _system_runtime(rules)
        engine += system
        links += system_links
    for req in request.projections:
        proj = _projection(req, rules)
        (engine if req.origin is Origin.ENGINE else declared).append(proj)
    overlays = [_overlay(req, rules) for req in request.overlays]

    entries: list[Mount] = [
        PrivateTmpfs(rules.home_real, 0o700, request.home_size_bytes),
        PrivateTmpfs(PRIVATE_TMP, 0o1777, request.tmp_size_bytes),
    ]
    entries += links
    # Each engine bind's request, for the rules and the refusals that name it.
    proj_of: dict[int, _Proj] = {}
    for proj in engine:
        bind = Bind(proj.source, proj.target, proj.access, proj.is_dir)
        proj_of[id(bind)] = proj
        entries.append(bind)
    entries += overlays
    _check_unique(entries)
    _check_links(entries)

    masks = _masks(request.masks, entries)
    entries += masks
    entries += _denied_masks(rules, entries)
    _check_unique(entries)
    _check_engine_nesting(entries, proj_of, request.overlays)

    covered: list[str] = []
    for proj in declared:
        entry = _place_declared(proj, entries, declared)
        if entry is None:
            covered.append(proj.source)
        else:
            entries.append(entry)
    _check_links(entries)
    _check_hidden(rules, entries)

    entries.sort(key=lambda entry: (_parts(entry.target), isinstance(entry, Link)))
    env_set, env_unset = _environment(request)
    return LaunchDescription(
        mounts=tuple(entries),
        env_set=env_set,
        env_unset=env_unset,
        network=request.network,
        sys=request.sys,
        covered=tuple(covered),
    )


def _check_unique(entries: list[Mount]) -> None:
    seen: dict[str, Mount] = {}
    for entry in entries:
        other = seen.get(entry.target)
        if other is not None:
            raise _refuse(
                entry.target, "C6 overlaps", "two entries of the description share this path"
            )
        seen[entry.target] = entry


def _check_links(entries: list[Mount]) -> None:
    """Nothing is mounted at or below a recreated link: it would follow it."""
    for link in entries:
        if not isinstance(link, Link):
            continue
        for entry in entries:
            if entry is not link and _inside(entry.target, link.target):
                raise _refuse(
                    entry.target,
                    "C6 overlaps",
                    f"lies at or below the link {link.target} the sandbox recreates",
                )


def _enclosing_bind(entries: list[Mount], source: str) -> Bind | None:
    """The deepest bind whose source is or contains the host path ``source``."""
    best: Bind | None = None
    for entry in entries:
        if isinstance(entry, Bind) and entry.is_dir and _inside(source, entry.source):
            if best is None or len(entry.source) > len(best.source):
                best = entry
    return best


def _masks(paths: tuple[str, ...], entries: list[Mount]) -> list[Mount]:
    """The engine's masks: inside a read-only bind, or a direct child of a writable one."""
    masks: list[Mount] = []
    for raw in paths:
        path = _resolve_engine(raw, "mask")
        if not os.path.lexists(path):
            continue  # nothing there to hide
        if not _kind_of(path, "mask"):
            raise _refuse(path, "C3 kinds", "a mask covers a directory, not a file")
        bind = _enclosing_bind(entries, path)
        if bind is None or bind.source == path:
            raise _refuse(path, "C4 denied roots", "a mask lies inside no projection")
        if bind.access is Access.WRITABLE and posixpath.dirname(path) != bind.source:
            raise _refuse(
                path,
                "C4 denied roots",
                f"a mask inside the writable projection {bind.source} must be a direct child "
                "of it: a deeper one could be moved from inside by renaming an ancestor",
            )
        masks.append(Mask(_rebase(path, bind.source, bind.target)))
    return masks


def _denied_masks(rules: _Rules, entries: list[Mount]) -> list[Mount]:
    """C4 for each denied root inside a projection: masked, or refused."""
    added: list[Mount] = []
    for denied in rules.denied:
        if not os.path.lexists(denied):
            continue  # nothing there to show or hide
        bind = _enclosing_bind(entries, denied)
        if bind is None or bind.source == denied:
            continue  # absent, or an engine carve-out (checked per projection)
        inner = _rebase(denied, bind.source, bind.target)
        hiding = _innermost(entries + added, inner)
        if isinstance(hiding, Mask | PrivateTmpfs) and _inside(hiding.target, bind.target):
            continue  # already masked by the engine
        if bind.access is Access.READ_ONLY:
            raise _refuse(
                denied,
                "C4 denied roots",
                f"a denied root inside the read-only projection {bind.source} must be masked "
                "by the engine",
            )
        if posixpath.dirname(denied) != bind.source:
            raise _refuse(
                denied,
                "C4 denied roots",
                f"a denied root inside the writable projection {bind.source} is masked only "
                "as a direct child of it, and this one lies deeper",
            )
        if not _kind_of(denied, "denied root"):
            raise _refuse(
                denied, "C4 denied roots", "a file in a writable projection cannot be masked"
            )
        added.append(Mask(inner))
    return added


def _check_engine_nesting(
    entries: list[Mount], proj_of: dict[int, _Proj], requests: tuple[OverlayRequest, ...]
) -> None:
    """C5 and C6 among the engine's own entries."""
    carved = {posixpath.normpath(req.lower): req.carve_out for req in requests if req.lower}
    for entry in entries:
        outer = _innermost(entries, entry.target, exclude=entry)
        if isinstance(entry, Bind):
            proj = proj_of[id(entry)]
            if isinstance(outer, Mask) and not proj.carve_out:
                raise _refuse(
                    entry.source,
                    "C5 carve-outs",
                    f"{proj.label} lies inside the mask {outer.target} and is not a carve-out",
                )
            if isinstance(outer, Overlay):
                raise _refuse(entry.source, "C6 overlaps", f"{proj.label} lies inside an overlay")
        elif isinstance(entry, Overlay):
            if isinstance(outer, Mask) and not carved.get(entry.lower, False):
                raise _refuse(
                    entry.target,
                    "C5 carve-outs",
                    f"the overlay lies inside the mask {outer.target} and is not a carve-out",
                )
            if isinstance(outer, Overlay):
                raise _refuse(entry.target, "C6 overlaps", "an overlay lies inside an overlay")
            if isinstance(outer, Bind) and outer.access is Access.WRITABLE:
                raise _refuse(
                    entry.target, "C6 overlaps", "an overlay lies inside a writable projection"
                )
        elif isinstance(entry, Mask) and isinstance(outer, Overlay):
            raise _refuse(entry.target, "C6 overlaps", "a mask lies inside an overlay")
    for overlay in (entry for entry in entries if isinstance(entry, Overlay)):
        for path in (overlay.upper, overlay.work):
            if _visible(entries, path):
                raise _refuse(
                    path, "C6 overlaps", "an overlay layer would be visible inside the sandbox"
                )


def _visible(entries: list[Mount], host_path: str) -> bool:
    """Whether the host path is reachable inside through some bind or overlay."""
    for entry in entries:
        if isinstance(entry, Bind) and _inside(host_path, entry.source):
            inner = _rebase(host_path, entry.source, entry.target)
            if _innermost(entries, inner) is entry:
                return True
        if isinstance(entry, Overlay) and _inside(host_path, entry.lower):
            if _innermost(entries, host_path) is entry:
                return True
    return False


def _place_declared(proj: _Proj, entries: list[Mount], declared: list[_Proj]) -> Bind | None:
    """C5 and C6 for one declared projection; ``None`` when it is already visible."""
    for other in declared:
        if other is not proj and _related(other.target, proj.target):
            raise _refuse(
                proj.source,
                "C6 overlaps",
                f"{proj.label} overlaps the declared projection {other.source}",
            )
    for entry in entries:
        if _below(entry.target, proj.target) and not isinstance(entry, PrivateTmpfs):
            raise _refuse(
                proj.source,
                "C6 overlaps",
                f"{proj.label} contains {entry.target}, which the engine places",
            )
    outer = _innermost(entries, proj.target)
    if isinstance(outer, Mask):
        raise _refuse(
            proj.source,
            "C5 carve-outs",
            f"{proj.label} lies inside the mask {outer.target}; only the engine carves out",
        )
    if isinstance(outer, Bind):
        consistent = _rebase(proj.target, outer.target, outer.source) == proj.source
        if outer.access is Access.READ_ONLY and proj.access is Access.READ_ONLY and consistent:
            return None
        raise _refuse(
            proj.source, "C6 overlaps", f"{proj.label} overlaps the projection {outer.source}"
        )
    if isinstance(outer, Overlay | Link):
        raise _refuse(proj.source, "C6 overlaps", f"{proj.label} overlaps {outer.target}")
    return Bind(proj.source, proj.target, proj.access, proj.is_dir)


def _check_hidden(rules: _Rules, entries: list[Mount]) -> None:
    """The description shows no existing denied root itself, and no forbidden source."""
    for denied in rules.denied:
        if not os.path.lexists(denied):
            continue
        if _visible(entries, denied) and not any(
            isinstance(e, Bind) and e.source == denied for e in entries
        ):
            raise _refuse(denied, "C4 denied roots", "the denied root would be visible")
    for forbidden, name in rules.forbidden:
        if _visible(entries, forbidden):
            raise _refuse(forbidden, "D4.3 forbidden source", f"{name} would be visible")


def _environment(
    request: SandboxRequest,
) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    """HOME, XDG and TMPDIR follow the private mounts; the caller's overrides after."""
    home = request.home
    env: dict[str, str] = {"HOME": home}
    for name, rel in _XDG_DIRS:
        env[name] = posixpath.join(home, rel)
    env["TMPDIR"] = PRIVATE_TMP
    unset = {"XDG_RUNTIME_DIR"}
    for name, value in request.env:
        if name in _OWNED_ENV:
            raise _refuse(name, "D8.2 environment", "the private home and temp decide this name")
        if not _ENV_NAME_RE.match(name):
            raise _refuse(name, "D8.2 environment", "not an environment variable name")
        if name in env:
            raise _refuse(name, "D8.2 environment", "set twice")
        env[name] = value
    for name in request.env_unset:
        if name in env:
            raise _refuse(name, "D8.2 environment", "both set and unset")
        if not _ENV_NAME_RE.match(name):
            raise _refuse(name, "D8.2 environment", "not an environment variable name")
        unset.add(name)
    return tuple(sorted(env.items())), tuple(sorted(unset))
