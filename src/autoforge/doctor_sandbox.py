"""``doctor``'s outer-sandbox rows (ADR 0005 §2.12, §2.14; #169).

Both ``autoforge doctor`` and ``autoforge local doctor`` show, read-only and
with no GitHub call:

- the capability probe's result (D14.1) and what ``sandbox.mode`` will do on
  this host (D12.2, D12.4): ``disabled`` is reported as a risk, logical
  isolation only; ``required``, and ``preferred`` in REMOTE, fail on a host
  that cannot enforce; LOCAL's ``preferred`` warns that it falls back;
- the policy by launch class (§2.5), as the dry-run plan shows it;
- per provider, its compiled declaration -- paths and classes, never a
  value -- and, when ``sandbox.mode`` enables the sandbox and the host can
  enforce, its ``--version`` launched inside it (D14.1); a keyring login,
  or a ``dir-rw`` login D7.2 refuses, is named with the supported
  alternatives (D7.3);
- ``sandbox.read_only_paths`` the run would refuse (D15.4);
- regular files with more than one link in the writable trees an agent
  gets (D4.4), the sandbox directories and their sizes (D8.5), and the TCP
  listeners on loopback and wildcard addresses, flagging X11's (D9.3).

Every walk is bounded; a path is shown, a file's contents never are.
"""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from . import engine as _engine
from .config import SANDBOX_PREFERRED, AutoForgeConfig, ProfileConfig
from .errors import AutoForgeError, ConfigurationError, ExecutionError, SandboxPolicyError
from .executor import ExecutionRequest
from .profiles import REQUIRED_PROFILES, local_required_profiles
from .providers import AgentProvider, PiProvider, provider_for
from .redaction import redact
from .sandbox import NetworkPosition, compile_launch
from .sandbox_bwrap import ProbeResult
from .sandbox_policy import (
    OPERATOR_PATHS_KEY,
    SANDBOX_DIR,
    AuthMode,
    LaunchClass,
    ProviderRequirements,
    class_summary,
    operator_path_problem,
    provider_request,
    rewrite_environment,
    summary_facts,
)

if TYPE_CHECKING:
    from .doctor import CheckResult, Doctor

# Every row's name starts with this, so that a caller can tell them apart.
ROW_PREFIX = "outer sandbox"

# Bounds of the read-only walks (D4.4, D8.5): entries visited per tree, and
# paths shown per row.
MAX_WALK_ENTRIES = 200_000
MAX_SHOWN = 20
# How much of a provider's sandboxed ``--version`` line a row shows.
VERSION_CHARS = 120

# The ports an X server listening on TCP uses (D9.3): 6000 + display.
X11_PORTS = range(6000, 6064)

_TCP_LISTEN = "0A"


@dataclass
class _Layout:
    """Where this repository's run would keep what an agent writes, or ``None`` outside git."""

    # The trees an agent writes in: the LOCAL root, or the REMOTE issue worktrees.
    workspaces: list[Path] = field(default_factory=list)
    # ``<git dir>/autoforge/sandbox/<key>`` (D8.4).
    sandbox_dirs: list[Path] = field(default_factory=list)
    # What no launch may see (D13.2): the run's own denied roots, which the
    # gate holds operator paths against (D15.4) and provider probes compile with.
    denied: tuple[str, ...] = ()


def sandbox_checks(doctor: Doctor, local: bool) -> list[CheckResult]:
    """The outer sandbox's rows, in order; none when the configuration did not load."""
    cfg = doctor.config
    if cfg is None:
        return []
    probe = _engine.probe_sandbox()
    layout = _layout(doctor, cfg, local)
    denied = layout.denied if layout is not None else ()
    return [
        capability_row(probe),
        mode_row(cfg, probe, local),
        *policy_rows(cfg, local),
        *provider_rows(doctor, cfg, local, probe, denied),
        operator_paths_row(cfg, layout),
        hard_links_row(layout),
        sandbox_dirs_row(layout),
        listeners_row(doctor.proc_net),
    ]


def _row(name: str, ok: bool, detail: str, required: bool = True) -> CheckResult:
    from .doctor import CheckResult

    return CheckResult(f"{ROW_PREFIX}: {name}", ok, redact(detail), required=required)


def _skip(name: str, detail: str) -> CheckResult:
    from .doctor import CheckResult

    return CheckResult.skip(f"{ROW_PREFIX}: {name}", redact(detail))


# -- capability and mode (D12.2, D12.4, D14.1) ---------------------------------
def capability_row(probe: ProbeResult) -> CheckResult:
    """The probe's result: informational, the mode row decides whether it fails."""
    return _row(
        "capability (one minimal sandboxed launch)", probe.supported, probe.describe(), False
    )


def mode_row(cfg: AutoForgeConfig, probe: ProbeResult, local: bool) -> CheckResult:
    """What ``sandbox.mode`` does on this host: OK, a risk (WARN), or a refusal (FAIL)."""
    sandbox = cfg.sandbox
    name = f"mode ({sandbox.mode})"
    if not sandbox.enabled:
        return _row(
            name,
            False,
            f"{_engine.LOGICAL_ISOLATION_ONLY}: agents and repository-defined commands run as "
            "the operator's UID without the outer sandbox and can reach whatever the operator "
            "can by absolute path, controller state and credentials included (a risk; set "
            "sandbox.mode: required"
            + (" or preferred" if local else "")
            + " where the capability row is supported)",
            required=False,
        )
    if probe.supported:
        return _row(name, True, "every agent launch and repository-defined command is enforced")
    missing = (
        probe.capability.value if probe.capability else "a capability"
    ) + f" ({probe.reason})"
    if sandbox.refuses_unenforced(local):
        how = (
            "required"
            if sandbox.mode != SANDBOX_PREFERRED
            else "preferred, which a REMOTE run treats as required"
        )
        alternatives = f"sandbox.mode: disabled ({_engine.LOGICAL_ISOLATION_ONLY})"
        if local:
            alternatives += ", or preferred (falls back visibly)"
        return _row(
            name,
            False,
            f"sandbox.mode is {how}, and this host cannot provide {missing}: every step that "
            f"launches is refused before anything is saved or charged; make the host capable, "
            f"or set {alternatives}",
        )
    return _row(
        name,
        False,
        f"this host cannot provide {missing}, so under preferred every LOCAL launch falls back "
        f"to {_engine.LOGICAL_ISOLATION_ONLY}, said on stderr at the launch and in its execution "
        "record (enforced: false); it is not sandboxed",
        required=False,
    )


def policy_rows(cfg: AutoForgeConfig, local: bool) -> list[CheckResult]:
    """§2.5's policy for each launch class the run can make, by role (as the dry-run plan)."""
    sandbox = cfg.sandbox
    validation = NetworkPosition(sandbox.validation_network)
    paths = tuple(sandbox.read_only_paths)
    classes = (
        [(LaunchClass.LOCAL_AGENT, validation), (LaunchClass.LOCAL_VALIDATION, validation)]
        if local
        else [
            (LaunchClass.REMOTE_AGENT, validation),
            (LaunchClass.EXPORT_COMMAND, NetworkPosition(sandbox.premerge_network)),
        ]
    )
    return [
        _row(
            f"policy, {launch_class.value}",
            True,
            class_summary(summary_facts(launch_class, network, cache=True, operator_paths=paths)),
            required=False,
        )
        for launch_class, network in classes
    ]


# -- providers (D7.3, D14.1) ---------------------------------------------------
def _reachable(cfg: AutoForgeConfig, local: bool) -> list[str]:
    if local:
        try:
            return local_required_profiles(cfg)
        except ConfigurationError:
            return []
    return [*REQUIRED_PROFILES, *(n for n in ("update_epic",) if n in cfg.profiles)]


def provider_rows(
    doctor: Doctor,
    cfg: AutoForgeConfig,
    local: bool,
    probe: ProbeResult,
    denied: tuple[str, ...] = (),
) -> list[CheckResult]:
    """One row per distinct (provider, command) the run can reach, ``scripted`` excepted.

    ``denied`` are the run's denied roots: a declaration that projects one
    is refused, as every launch of it would be, before anything is launched.
    """
    groups: dict[tuple[str, str], list[ProfileConfig]] = {}
    for name in _reachable(cfg, local):
        profile = cfg.profiles.get(name)
        if profile is None or profile.provider == "scripted":
            continue
        command = profile.command or profile.provider
        groups.setdefault((profile.provider, command), []).append(profile)
    return [
        _provider_row(doctor, cfg, profiles, probe, denied)
        for (_provider, _command), profiles in groups.items()
    ]


def _provider_row(
    doctor: Doctor,
    cfg: AutoForgeConfig,
    profiles: list[ProfileConfig],
    probe: ProbeResult,
    denied: tuple[str, ...],
) -> CheckResult:
    profile = profiles[0]
    name = (
        f"provider {profile.provider} '{profile.command or profile.provider}' "
        f"({', '.join(p.name for p in profiles)})"
    )
    enforced = cfg.sandbox.enabled
    try:
        provider = provider_for(profile)
        version = (
            provider.version_command(profile)
            if isinstance(provider, PiProvider)
            else [profile.command or profile.provider, "--version"]
        )
    except (AutoForgeError, OSError) as exc:
        return _row(name, False, f"its declaration cannot be read: {exc}", required=enforced)
    try:
        # As a launch is (``AgentProvider.execute``): the adapter's isolation
        # over the allow-listed environment, its temporary files kept until
        # the launch has ended.
        with provider.integration_isolation() as isolation:
            return _provider_probe(
                doctor, cfg, provider, profile, version, isolation, probe, denied, name
            )
    except ExecutionError as exc:
        return _row(name, False, f"its launch environment cannot be prepared: {exc}", enforced)


def _provider_probe(
    doctor: Doctor,
    cfg: AutoForgeConfig,
    provider: AgentProvider,
    profile: ProfileConfig,
    version: list[str],
    isolation: dict[str, str],
    probe: ProbeResult,
    denied: tuple[str, ...],
    name: str,
) -> CheckResult:
    """The declaration, compiled and launched with the environment a launch gets.

    That environment is the allow-listed one with the adapter's isolation
    over it (an operator's ``OPENCODE_CONFIG_DIR`` is replaced as at every
    launch), and its path-valued names are spelled where they lead inside
    (``rewrite_environment``, D8.2): a ``CLAUDE_CONFIG_DIR`` reached through
    a link names the directory, which is what the CLI is given.
    """
    enforced = cfg.sandbox.enabled
    allowlist = cfg.execution.environment_names()
    env = {**provider.launch_environment(allowlist), **isolation}
    home = _engine._operator_home()
    try:
        needs = provider.sandbox_requirements(profile, version, env, home)
    except (AutoForgeError, OSError) as exc:
        return _row(name, False, f"its declaration cannot be read: {exc}", required=enforced)
    shown = describe_requirements(needs)
    with tempfile.TemporaryDirectory(prefix="autoforge-doctor-") as scratch:
        workspace = os.path.realpath(scratch)
        try:
            compiled = compile_launch(
                provider_request(
                    needs,
                    home=home,
                    controller_env=os.environ,
                    workspace=workspace,
                    operator_paths=tuple(cfg.sandbox.read_only_paths),
                    denied_roots=denied,
                )
            )
            description = rewrite_environment(compiled, env, needs)
        except SandboxPolicyError as exc:
            return _row(
                name,
                False,
                f"{shown}; every launch of it is refused: {exc}. {_refusal_hint(needs, exc)}",
                required=enforced,
            )
        except (AutoForgeError, OSError) as exc:
            return _row(name, False, f"{shown}; it cannot be compiled: {exc}", enforced)
        if not enforced:
            return _row(
                name,
                True,
                f"{shown}; the declaration compiles (no sandboxed launch under sandbox.mode "
                "disabled)",
                required=False,
            )
        if not probe.supported:
            return _row(
                name,
                True,
                f"{shown}; the declaration compiles; no sandboxed launch (the host cannot enforce)",
                required=False,
            )
        if not needs.executable:
            return _row(
                name,
                False,
                f"{shown}; {version[0]!r} is not on the launch's PATH, so no sandboxed launch",
                required=enforced,
            )
        try:
            result = doctor.runner(
                ExecutionRequest(
                    command=[needs.executable, *version[1:]],
                    cwd=workspace,
                    timeout_seconds=doctor.timeout,
                    env_allowlist=provider.launch_allowlist(allowlist),
                    env=isolation or None,
                    sandbox=description,
                )
            )
        except ExecutionError as exc:
            return _row(name, False, f"{shown}; the sandboxed launch was refused: {exc}", enforced)
    if result.timed_out or result.exit_code != 0:
        why = "timed out" if result.timed_out else f"exited {result.exit_code}"
        return _row(
            name,
            False,
            f"{shown}; {' '.join(version[1:])} inside the sandbox {why}: the declaration does "
            "not give the CLI what it needs",
            required=enforced,
        )
    # Redacted whole before a line is picked and clipped: a clip first could
    # leave a token too short for the redactor to recognize.
    first = (redact(result.stdout).strip().splitlines() or [""])[0][:VERSION_CHARS]
    return _row(name, True, f"{shown}; inside the sandbox: {first or '(no output)'}", False)


def describe_requirements(needs: ProviderRequirements) -> str:
    """A provider's declaration by path and class only: never a variable's value."""
    parts = [f"executable {needs.executable or '(not found)'}"]
    if needs.install_roots or needs.config:
        parts.append("read-only " + ", ".join([*needs.install_roots, *needs.config]))
    if needs.home_config:
        parts.append("read-only in the private home " + ", ".join(needs.home_config))
    if needs.state:
        parts.append("writable " + ", ".join(needs.state))
    for item in needs.auth:
        where = f" {item.source}" if item.source and item.mode is not AuthMode.ENV else ""
        parts.append(f"credential {item.mode.value}: {item.label}{where}")
    if not needs.auth:
        parts.append("credential: none declared")
    return "; ".join(parts)


def _refusal_hint(needs: ProviderRequirements, exc: SandboxPolicyError) -> str:
    """What to do about a declaration the policy refuses: the rule decides."""
    if exc.rule.startswith("C4"):
        return (
            "Keep it clear of what no launch may see (ADR 0005 D13.2: the state directory and "
            "its run logs, the lock, the controller's directory under the git common dir, "
            "another worktree, and in REMOTE the operator's checkout), or set sandbox.mode: "
            "disabled."
        )
    return _login_alternatives(needs)


def _login_alternatives(needs: ProviderRequirements) -> str:
    """D7.3: what to use instead of a keyring or refused ``dir-rw`` login."""
    kinds = {item.mode for item in needs.auth}
    if AuthMode.KEYRING in kinds:
        lead = "A keyring login cannot reach a sandboxed launch without exposing the keyring"
    elif AuthMode.DIR_RW in kinds:
        lead = (
            "A dir-rw login may not be the provider's own configuration root, nor lie in it "
            "(ADR 0005 D7.2)"
        )
    else:
        return "Fix the path the message names, or set sandbox.mode: disabled."
    return (
        f"{lead}; use a login file the provider reads (file-ro), a dedicated credential "
        "directory outside its configuration root (dir-rw), or an API key variable (env), "
        "as docs/usage.md describes"
    )


# -- the repository's layout, operator paths (D15.4) ---------------------------
def _git_lines(doctor: Doctor, *args: str) -> list[str] | None:
    try:
        res = doctor.runner(
            ExecutionRequest(command=["git", *args], cwd=doctor.cwd, timeout_seconds=doctor.timeout)
        )
    except Exception:  # a missing git is reported by its own row
        return None
    if res.timed_out or res.exit_code != 0:
        return None
    return [line for line in (res.stdout or "").splitlines() if line.strip()]


def _layout(doctor: Doctor, cfg: AutoForgeConfig, local: bool) -> _Layout | None:
    lines = _git_lines(
        doctor,
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
        "--git-dir",
        "--git-common-dir",
    )
    if lines is None or len(lines) != 3 or not all(line.startswith("/") for line in lines):
        return None
    root, git_dir, common = (Path(os.path.realpath(line)) for line in lines)
    try:
        denied = _run_engine(doctor, cfg, local).gate_denied_roots(root if local else None)
    except (AutoForgeError, OSError):
        return None
    if local:
        workspaces = [root]
    else:
        configured = cfg.execution.worktree_dir
        holder = (
            Path(os.path.realpath(Path(doctor.cwd) / configured))
            if configured
            else common / "autoforge" / "worktrees"
        )
        workspaces = sorted(p for p in _children(holder) if p.is_dir())
    sandbox_dirs: list[Path] = []
    for base in dict.fromkeys([common, git_dir]):
        sandbox_dirs += sorted(p for p in _children(base / SANDBOX_DIR) if p.is_dir())
    return _Layout(workspaces, sandbox_dirs, denied)


def _run_engine(doctor: Doctor, cfg: AutoForgeConfig, local: bool) -> _engine.ControllerEngine:
    """An engine bound as this invocation's run would be, so its denied roots are the run's.

    The state directory is the one the CLI gives a run (``--state-dir``,
    else ``state_dir``; LOCAL's resolved as ``bind_local_state_dir`` does,
    the default ``<git dir>/autoforge/state``), relative to the working
    directory as there; the lock, the git directories and the registered
    worktrees are read as a launch reads them. Nothing is created or locked.
    """
    eng = _engine.ControllerEngine(
        cfg, state_dir=doctor.state_dir or cfg.state_dir, workdir=doctor.cwd, runner=doctor.runner
    )
    if local:
        eng.paths = _engine.local_state_paths(
            eng.workspace(), explicit=doctor.state_dir, configured=cfg.state_dir
        )
    return eng


def _children(path: Path) -> list[Path]:
    try:
        if path.is_symlink() or not path.is_dir():
            return []
        return [child for child in path.iterdir() if not child.is_symlink()]
    except OSError:
        return []


def operator_paths_row(cfg: AutoForgeConfig, layout: _Layout | None) -> CheckResult:
    """``sandbox.read_only_paths``: what the run would refuse (D15.4)."""
    paths = tuple(cfg.sandbox.read_only_paths)
    name = f"operator read-only paths ({OPERATOR_PATHS_KEY})"
    if not paths:
        return _row(name, True, "(none configured)", required=False)
    if layout is None:
        return _skip(
            name,
            f"{', '.join(paths)}; not checked against the run's denied roots "
            "(not a git repository)",
        )
    problem = operator_path_problem(paths, layout.denied)
    if problem:
        return _row(name, False, f"refused at the first launch: {problem}", cfg.sandbox.enabled)
    return _row(
        name,
        True,
        f"{', '.join(paths)}: read-only in every launch; a token in one of these files is "
        "readable by every tool in the sandbox",
        required=False,
    )


# -- hard links (D4.4), sandbox directories (D8.5) ------------------------------
def _walk(root: Path, skip_git: bool) -> Iterator[tuple[str, os.stat_result]]:
    """``(path, lstat)`` of every entry below ``root``, never following a link, bounded.

    Each directory is read an entry at a time and the bound is checked per
    entry, so no more than ``MAX_WALK_ENTRIES`` are ever read, however many
    one directory holds. A directory that cannot be read, or stops being
    readable part way, is skipped from there.
    """
    seen = 0
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    seen += 1
                    if seen > MAX_WALK_ENTRIES:
                        return
                    if skip_git and entry.name == ".git":
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    yield entry.path, info
                    if stat.S_ISDIR(info.st_mode):
                        stack.append(entry.path)
        except OSError:
            continue


def hard_links_row(layout: _Layout | None) -> CheckResult:
    """Regular files with ``st_nlink > 1`` in the trees an agent writes (D4.4)."""
    name = "hard links in writable trees"
    if layout is None:
        return _skip(name, "not a git repository")
    trees = [*layout.workspaces, *layout.sandbox_dirs]
    if not trees:
        return _row(name, True, "nothing to scan (no workspace or sandbox directory yet)", False)
    linked: list[str] = []
    truncated = False
    for tree in trees:
        for path, info in _walk(tree, skip_git=True):
            if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                linked.append(path)
                if len(linked) > MAX_SHOWN:
                    truncated = True
                    break
        if truncated:
            break
    scanned = ", ".join(str(t) for t in trees[:MAX_SHOWN])
    if not linked:
        return _row(name, True, f"none in {scanned}", required=False)
    shown = ", ".join(linked[:MAX_SHOWN]) + (" and more" if truncated else "")
    return _row(
        name,
        False,
        f"{shown}: each still aliases an inode outside, which a sandboxed launch can read and "
        "write through; remove a REMOTE worktree or sandbox directory to have it recreated, "
        "or break the link in the LOCAL tree (ADR 0005 D4.4)",
        required=False,
    )


def _size(root: Path) -> tuple[int, bool]:
    total = 0
    complete = True
    for count, (_path, info) in enumerate(_walk(root, skip_git=False), 1):
        if count >= MAX_WALK_ENTRIES:
            complete = False
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
    return total, complete


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def sandbox_dirs_row(layout: _Layout | None) -> CheckResult:
    """The sandbox directories and their sizes; AutoForge never deletes them (D8.5)."""
    name = "sandbox directories"
    if layout is None:
        return _skip(name, "not a git repository")
    if not layout.sandbox_dirs:
        return _row(name, True, "(none yet)", required=False)
    shown = []
    for path in layout.sandbox_dirs[:MAX_SHOWN]:
        size, complete = _size(path)
        shown.append(f"{path} {_human(size)}" + ("" if complete else "+"))
    more = len(layout.sandbox_dirs) - MAX_SHOWN
    return _row(
        name,
        True,
        "; ".join(shown)
        + (f"; and {more} more" if more > 0 else "")
        + " (never deleted by AutoForge; remove one to reclaim it, it is recreated empty)",
        required=False,
    )


# -- TCP listeners (D9.3) --------------------------------------------------------
def _listeners(proc_net: str) -> list[tuple[str, int]]:
    """``(address, port)`` of the TCP sockets listening on loopback or a wildcard."""
    found: list[tuple[str, int]] = []
    for family in ("tcp", "tcp6"):
        try:
            text = Path(proc_net, family).read_text()
        except OSError:
            continue
        for line in text.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 4 or fields[3] != _TCP_LISTEN:
                continue
            address, _, port = fields[1].partition(":")
            shown = _address(address)
            if shown is not None:
                found.append((shown, int(port, 16)))
    return sorted(set(found), key=lambda item: (item[1], item[0]))


def _address(raw: str) -> str | None:
    """The address of a ``/proc/net/tcp*`` entry when it is loopback or a wildcard."""
    try:
        data = bytes.fromhex(raw)
    except ValueError:
        return None
    # Each 32-bit word is in host byte order (little-endian on every Linux
    # AutoForge runs on).
    words = [data[i : i + 4][::-1] for i in range(0, len(data), 4)]
    packed = b"".join(words)
    if len(packed) == 4:
        if packed == b"\0\0\0\0":
            return "0.0.0.0"
        if packed[0] == 127:
            return ".".join(str(b) for b in packed)
        return None
    if packed == bytes(16):
        return "::"
    if packed == bytes(15) + b"\1":
        return "::1"
    if packed[:12] == bytes(10) + b"\xff\xff" and packed[12] == 127:
        return "::ffff:" + ".".join(str(b) for b in packed[12:])
    return None


def listeners_row(proc_net: str) -> CheckResult:
    """Every loopback and wildcard TCP listener is reachable from inside (D9.3)."""
    name = "TCP listeners reachable from inside"
    if not os.path.isdir(proc_net):
        return _skip(name, f"{proc_net} cannot be read on this host")
    listeners = _listeners(proc_net)
    if not listeners:
        return _row(name, True, "none on loopback or a wildcard address", required=False)
    shown = ", ".join(f"{address} port {port}" for address, port in listeners)
    x11 = sorted({port for _address, port in listeners if port in X11_PORTS})
    if x11:
        return _row(
            name,
            False,
            f"{shown}; X11 port(s) {', '.join(map(str, x11))}: an X server listening on TCP "
            "lets a launch read and inject input; launches share the host network namespace",
            required=False,
        )
    return _row(
        name,
        True,
        f"{shown} (launches share the host network namespace, so each is reachable)",
        required=False,
    )
