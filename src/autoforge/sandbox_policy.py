"""Which host resources a launch gets, by launch class (ADR 0005 §2.5, #168).

The engine-layer mapping of D13.2. The engine supplies its facts as data
(:class:`LaunchFacts`), a provider declares what its CLI needs as data
(:class:`ProviderRequirements`, D13.1), and :func:`sandbox_request` turns the
two into the compiler's :class:`~autoforge.sandbox.SandboxRequest`. The
launch class alone decides which rows of §2.5's table apply: there is no
provider branch here and no workflow decision, and a provider never emits a
mount (D7.1).

- **REMOTE agent:** the issue worktree and its own admin dir writable, as
  carve-outs; the worktree's ``.git`` file and the admin dir's ``commondir``
  and ``gitdir`` pinned read-only (D6.6); the common dir read-only with
  ``autoforge/``, ``worktrees/`` and ``modules/`` masked; an overlay at
  ``<common>/objects`` whose upper layer is the issue's private object
  directory (D6.1); the read context read-only; the issue's cache at
  ``~/.cache`` for the launches that produce a change.
- **LOCAL agent and LOCAL validation command:** the repository root
  writable, its ``.git`` entry, git dir and common dir read-only (so a LOCAL
  agent cannot commit, stash or branch), ``<git dir>/autoforge``,
  ``<common>/autoforge`` and ``<common>/worktrees`` masked (the checkout's own
  admin dir carved out read-only); the checkout's cache at ``~/.cache`` for
  the launches that produce a change and for validation.
- **Export command (pre-merge verification):** the exported tree writable,
  nothing else of the repository, no persistent cache.

Every class gets the system runtime, a private home and temp (the
compiler's, D8.1), the directory #165 points the child's ``gh``, git, curl,
wget and zsh at (read-only), and the engine's denied roots. Agents run on
the host network; a repository-defined command runs on an isolated one when
the operator asks for it (D9.2).

:func:`describe_launch` compiles the request and rewrites the launch's
path-valued environment (D8.2): a name AutoForge knows to be a path passes
only when what it names is visible inside, ``PATH`` keeps only its visible
entries, and every unset name is recorded. A refusal of the compiler becomes
a launch refusal (:class:`~autoforge.errors.SandboxLaunchRefusedError`), on
the path a failure to spawn takes.

Pure except for the compiler's ``realpath`` and ``lstat``, ``realpath`` of
the environment's path values, and the first line of an executable read for
its interpreter; nothing is created or launched here.
"""

from __future__ import annotations

import dataclasses
import enum
import os
import posixpath
import shutil
from collections.abc import Mapping
from dataclasses import dataclass

from .child_env import OVERRIDE_PATH_NAMES
from .errors import SandboxLaunchRefusedError, SandboxPolicyError
from .redaction import redact
from .sandbox import (
    Access,
    LaunchDescription,
    NetworkPosition,
    Origin,
    OverlayRequest,
    ProjectionRequest,
    SandboxFacts,
    SandboxRequest,
    compile_launch,
)

# Where the controller-owned sandbox directories live (D8.4): under
# ``<git common dir>/autoforge/sandbox/<key>`` for REMOTE (the key is the
# worktree's name) and ``<git dir>/autoforge/sandbox/local`` for LOCAL.
SANDBOX_DIR = "autoforge/sandbox"
LOCAL_SANDBOX_KEY = "local"
PRIVATE_OBJECTS = "objects"
OVERLAY_WORK = "overlay-work"
CACHE = "cache"
# The admin dir's pointer files pinned read-only for a REMOTE agent (D6.6).
PINNED_ADMIN_FILES = ("commondir", "gitdir")

# The CA-bundle names of ``DEFAULT_ENV_ALLOWLIST``: path-valued, so they pass
# into a sandboxed launch only when what they name is visible (D8.2).
CA_BUNDLE_NAMES = (
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)

# How much of an executable is read to find its interpreter (``#!``).
_SHEBANG_BYTES = 256


class LaunchClass(enum.Enum):
    """The rows of ADR 0005 §2.5 a launch gets."""

    REMOTE_AGENT = "REMOTE agent"
    LOCAL_AGENT = "LOCAL agent"
    LOCAL_VALIDATION = "LOCAL validation command"
    EXPORT_COMMAND = "export command"


class AuthMode(enum.Enum):
    """How a provider's model credential reaches a sandboxed launch (D7.1)."""

    NONE = "none"
    # A variable of the launch's environment (passed by the allow-list,
    # subject to #165's refusals); nothing to project.
    ENV = "env"
    # One regular file, read-only, at its host path or a declared path in
    # the private home.
    FILE_RO = "file-ro"
    # One provider state directory, writable, never on the provider's
    # operator configuration root (D7.2).
    DIR_RW = "dir-rw"
    # A keychain or Secret Service login: declared so that it is refused.
    KEYRING = "keyring"


@dataclass(frozen=True)
class AuthItem:
    """One declared credential item (D7.1): what it is, never its contents.

    ``source`` is the host path (``file-ro``, ``dir-rw``) or the login's
    name (``keyring``); ``target`` the path inside, ``None`` for the host
    path; ``env_name`` a variable that names the item, set to its path
    inside.
    """

    mode: AuthMode
    label: str
    source: str = ""
    target: str | None = None
    env_name: str = ""


@dataclass(frozen=True)
class ProviderRequirements:
    """What a provider's CLI needs from the host, as data (ADR 0005 D13.1).

    ``executable`` is the resolved host path the launch names (empty: the
    command is left as it is); ``install_roots`` and ``config`` are
    projected read-only, ``state`` (directories the adapter made for this
    launch) writable; ``path_dirs`` are put first on ``PATH``;
    ``path_names`` are the provider's path-valued variables (D8.2).
    ``operator_config_roots`` are where the CLI loads the operator's own
    settings, hooks and extensions from (D7.2).
    """

    executable: str = ""
    install_roots: tuple[str, ...] = ()
    path_dirs: tuple[str, ...] = ()
    config: tuple[str, ...] = ()
    state: tuple[str, ...] = ()
    auth: tuple[AuthItem, ...] = ()
    operator_config_roots: tuple[str, ...] = ()
    path_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class LaunchFacts:
    """The controller's facts about one launch, as data (ADR 0005 D13.2).

    ``workspace`` is what the launch works in (the issue worktree, the
    repository root, the exported tree). ``git_entry`` is the workspace's
    ``.git``; ``git_dir`` is its own git directory (a REMOTE worktree's
    admin dir, a LOCAL checkout's git dir) and ``common_dir`` the shared
    one. ``private_objects`` and ``overlay_work`` are the issue's object
    overlay layers, ``cache`` the persistent cache when the launch gets one
    (empty: its cache is inside the private home), ``read_context`` the
    launch's read context, ``controller_config`` the directory #165's
    overrides point into. ``home`` is the operator's home and
    ``controller_env`` the controller's environment, for D4.3.
    """

    launch_class: LaunchClass
    workspace: str
    home: str
    controller_env: Mapping[str, str]
    git_entry: str = ""
    git_dir: str = ""
    common_dir: str = ""
    private_objects: str = ""
    overlay_work: str = ""
    cache: str = ""
    read_context: str = ""
    controller_config: str = ""
    denied_roots: tuple[str, ...] = ()
    network: NetworkPosition = NetworkPosition.HOST


@dataclass(frozen=True)
class SandboxPlan:
    """What the engine hands a provider for one agent launch: the facts.

    The provider adds its declaration for each process it starts (the
    agent, Pi's auth preflight) and gets the description to launch it with.
    """

    facts: LaunchFacts

    def describe(self, needs: ProviderRequirements, env: Mapping[str, str]) -> LaunchDescription:
        """The description of one process of the launch (:func:`describe_launch`)."""
        return describe_launch(self.facts, needs, env)


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def sandbox_request(
    facts: LaunchFacts, needs: ProviderRequirements | None = None
) -> SandboxRequest:
    """§2.5's rows for ``facts.launch_class``, with the provider's declaration.

    ``needs`` applies to an agent launch only; a repository-defined command
    gets no provider resource whatever is passed.
    """
    projections: list[ProjectionRequest] = []
    masks: list[str] = []
    overlays: list[OverlayRequest] = []
    writable, read_only = Access.WRITABLE, Access.READ_ONLY

    def engine(
        path: str,
        access: Access,
        label: str,
        *,
        carve_out: bool = True,
        target: str | None = None,
    ) -> None:
        projections.append(
            ProjectionRequest(
                path, access, Origin.ENGINE, target=target, carve_out=carve_out, label=label
            )
        )

    kind = facts.launch_class
    common = facts.common_dir
    cache_target = posixpath.join(facts.home, ".cache")
    if kind is LaunchClass.REMOTE_AGENT:
        # The worktree lies in the masked ``<common>/autoforge`` unless
        # ``execution.worktree_dir`` puts it elsewhere, where it is no carve-out.
        in_common = _inside(facts.workspace, common)
        engine(facts.workspace, writable, "the issue worktree", carve_out=in_common)
        engine(facts.git_entry, read_only, "the worktree's .git file", carve_out=in_common)
        engine(common, read_only, "the git common dir")
        engine(facts.git_dir, writable, "the worktree's admin dir")
        for name in PINNED_ADMIN_FILES:
            pinned = posixpath.join(facts.git_dir, name)
            if os.path.isfile(pinned) and not os.path.islink(pinned):
                engine(pinned, read_only, f"the admin dir's {name}")
        masks += [posixpath.join(common, name) for name in ("autoforge", "worktrees", "modules")]
        overlays.append(
            OverlayRequest(
                posixpath.join(common, "objects"),
                facts.private_objects,
                facts.overlay_work,
                carve_out=True,
                label="the shared object store",
            )
        )
        if facts.read_context:
            engine(facts.read_context, read_only, "the read context")
    elif kind in (LaunchClass.LOCAL_AGENT, LaunchClass.LOCAL_VALIDATION):
        engine(facts.workspace, writable, "the repository root", carve_out=False)
        engine(facts.git_entry, read_only, "the root's .git", carve_out=False)
        if facts.git_dir != facts.git_entry:
            # A linked worktree: its admin dir, carved out of the masked
            # ``<common>/worktrees``.
            engine(facts.git_dir, read_only, "the checkout's git dir")
        if common not in (facts.git_entry, facts.git_dir):
            engine(common, read_only, "the git common dir")
        for path in (
            posixpath.join(facts.git_dir, "autoforge"),
            posixpath.join(common, "autoforge"),
            posixpath.join(common, "worktrees"),
        ):
            if path not in masks:
                masks.append(path)
    else:
        engine(facts.workspace, writable, "the exported tree", carve_out=False)
    if facts.cache and kind is not LaunchClass.EXPORT_COMMAND:
        engine(facts.cache, writable, "the cache", target=cache_target)
    if facts.controller_config:
        engine(
            facts.controller_config,
            read_only,
            "the launch's gh, git and shell configuration",
            carve_out=False,
        )

    env: list[tuple[str, str]] = []
    keyring: list[str] = []
    operator_roots: tuple[str, ...] = ()
    agent = kind in (LaunchClass.REMOTE_AGENT, LaunchClass.LOCAL_AGENT)
    if needs is not None and agent:
        operator_roots = needs.operator_config_roots
        for path in _outermost([*needs.install_roots, *needs.config]):
            projections.append(
                ProjectionRequest(path, read_only, Origin.DECLARED, label=f"{path} (provider)")
            )
        for path in needs.state:
            projections.append(
                ProjectionRequest(path, writable, Origin.DECLARED, label=f"{path} (provider)")
            )
        for item in needs.auth:
            if item.mode is AuthMode.KEYRING:
                keyring.append(item.source or item.label)
                continue
            if item.mode not in (AuthMode.FILE_RO, AuthMode.DIR_RW):
                continue
            access = writable if item.mode is AuthMode.DIR_RW else read_only
            projections.append(
                ProjectionRequest(
                    item.source,
                    access,
                    Origin.DECLARED,
                    target=item.target,
                    label=f"{item.label} ({item.mode.value})",
                )
            )
            if item.env_name:
                env.append((item.env_name, item.target or item.source))
    network = NetworkPosition.HOST if agent else facts.network
    return SandboxRequest(
        home=facts.home,
        controller_env=facts.controller_env,
        projections=tuple(projections),
        masks=tuple(masks),
        overlays=tuple(overlays),
        denied_roots=tuple(dict.fromkeys(facts.denied_roots)),
        operator_config_roots=operator_roots,
        keyring_items=tuple(keyring),
        network=network,
        env=tuple(env),
    )


def _outermost(paths: list[str]) -> list[str]:
    """``paths`` resolved, without those inside another one (C6 would refuse them)."""
    resolved = list(dict.fromkeys(os.path.realpath(p) for p in paths if p))
    return [p for p in resolved if not any(q != p and _inside(p, q) for q in resolved)]


def describe_launch(
    facts: LaunchFacts, needs: ProviderRequirements | None, env: Mapping[str, str]
) -> LaunchDescription:
    """Compile one launch's description and rewrite its environment (D8.2).

    ``env`` is the environment the launch would get without a sandbox (the
    allow-listed variables and the controller's overrides). A request the
    compiler refuses is a :class:`SandboxLaunchRefusedError` naming the path
    and the rule, raised before anything is spawned.
    """
    try:
        description = compile_launch(sandbox_request(facts, needs))
    except SandboxPolicyError as exc:
        raise policy_refusal(exc, facts) from exc
    return rewrite_environment(description, env, needs)


def policy_refusal(exc: SandboxPolicyError, facts: LaunchFacts) -> SandboxLaunchRefusedError:
    """A compiler refusal as the launch refusal the engine records (D12.1)."""
    reason = redact(str(exc))
    return SandboxLaunchRefusedError(
        f"the sandbox cannot be established for the {facts.launch_class.value}: {reason}",
        SandboxFacts(enforced=False, backend="", network=facts.network.value, setup_failure=reason),
    )


def _shows(description: LaunchDescription, value: str) -> bool:
    return value.startswith("/") and description.inside_path(os.path.realpath(value)) is not None


def rewrite_environment(
    description: LaunchDescription,
    env: Mapping[str, str],
    needs: ProviderRequirements | None = None,
) -> LaunchDescription:
    """``description`` with the launch's path-valued names made true inside (D8.2).

    A name AutoForge knows to be a path (the CA-bundle names, #165's
    overrides, the provider's ``path_names``) whose absolute value names
    nothing visible inside is unset; a value that is not an absolute path
    (``OPENCODE_DB=:memory:``, a name relative to the provider's own data
    directory) passes unchanged, as any other variable does. ``PATH`` keeps
    its visible absolute entries, after the provider's ``path_dirs``, and is
    unset when none is left. The names the compiler sets are its own.
    """
    owned = {name for name, _ in description.env_set} | set(description.env_unset)
    names = [*CA_BUNDLE_NAMES, *OVERRIDE_PATH_NAMES, *(needs.path_names if needs else ())]
    unset: set[str] = set()
    for name in dict.fromkeys(names):
        value = env.get(name, "")
        if name in owned or not value.startswith("/"):
            continue
        if not _shows(description, value):
            unset.add(name)
    extra: list[tuple[str, str]] = []
    if "PATH" not in owned and ("PATH" in env or (needs and needs.path_dirs)):
        entries = [*(needs.path_dirs if needs else ()), *env.get("PATH", "").split(":")]
        kept = [entry for entry in dict.fromkeys(entries) if _shows(description, entry)]
        if kept:
            if ":".join(kept) != env.get("PATH"):
                extra.append(("PATH", ":".join(kept)))
        elif "PATH" in env:
            unset.add("PATH")
    if not unset and not extra:
        return description
    return dataclasses.replace(
        description,
        env_set=tuple(sorted([*description.env_set, *extra])),
        env_unset=tuple(sorted(set(description.env_unset) | unset)),
    )


# ------------------------------------------------- executables and install roots


def resolve_command(name: str, search_path: str | None) -> str:
    """The resolved host path ``name`` runs as, or "" when it cannot be told.

    A bare name is looked up on ``search_path`` (the launch's ``PATH``, as
    the executor's spawn would); a relative path with a separator is left
    alone, since it would be resolved against the launch's directory.
    """
    found = name if "/" in name else shutil.which(name, path=search_path)
    if not found or not found.startswith("/"):
        return ""
    real = os.path.realpath(found)
    return real if os.path.isfile(real) and os.access(real, os.X_OK) else ""


def install_root(path: str, home: str) -> str:
    """The read-only root a resolved executable is installed in (D13.1).

    The package directory for a file inside ``node_modules`` (its modules
    sit below it). Else the prefix of ``<prefix>/bin/<file>`` when the
    prefix also has a ``lib`` directory (an interpreter's standard library
    sits there), unless the prefix is the home, one of its ancestors or
    ``~/.local``. Else the file's directory, unless that directory is the
    home or one of its ancestors: then the file alone, never all of the home.
    """
    parts = path.split("/")
    if "node_modules" in parts:
        index = parts.index("node_modules")
        package = (
            index + 2
            if index + 1 < len(parts) and parts[index + 1].startswith("@")
            else (index + 1)
        )
        if package < len(parts) - 1:
            return "/".join(parts[: package + 1])
    parent = posixpath.dirname(path)
    homes = (home, os.path.realpath(home))
    if posixpath.basename(parent) == "bin":
        prefix = posixpath.dirname(parent)
        broad = any(_inside(anchor, prefix) for anchor in homes) or prefix in (
            posixpath.join(anchor, ".local") for anchor in homes
        )
        if prefix != "/" and not broad and os.path.isdir(posixpath.join(prefix, "lib")):
            return prefix
    for anchor in homes:
        if _inside(anchor, parent):
            return path
    return path if parent == "/" else parent


def interpreter_of(executable: str, search_path: str | None) -> str:
    """The resolved interpreter an executable script names (``#!``), or "".

    ``#!/usr/bin/env [-S] name`` is looked up on ``search_path``, as ``env``
    would do inside. Only the first line is read.
    """
    try:
        with open(executable, "rb") as handle:
            head = handle.read(_SHEBANG_BYTES)
    except OSError:
        return ""
    if not head.startswith(b"#!"):
        return ""
    line = head[2:].split(b"\n", 1)[0].decode("utf-8", "replace").strip()
    words = line.split()
    if not words:
        return ""
    program = words[0]
    if posixpath.basename(program) == "env":
        names = [word for word in words[1:] if not word.startswith("-") and "=" not in word]
        if not names:
            return ""
        return resolve_command(names[0], search_path)
    return resolve_command(program, search_path)


def executable_requirements(
    command: str, *, home: str, search_path: str | None
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """``(executable, install roots, PATH dirs)`` for the command a launch names.

    The executable resolved on the host; its install root; and, for a
    script, its interpreter's install root and directory, which goes first
    on ``PATH`` so that ``#!/usr/bin/env node`` finds it inside.
    """
    executable = resolve_command(command, search_path)
    if not executable:
        return "", (), ()
    roots = [install_root(executable, home)]
    path_dirs: list[str] = []
    interpreter = interpreter_of(executable, search_path)
    if interpreter:
        roots.append(install_root(interpreter, home))
        path_dirs.append(posixpath.dirname(interpreter))
    return executable, tuple(dict.fromkeys(roots)), tuple(path_dirs)
