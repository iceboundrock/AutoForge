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
entries, each is spelled where it is inside (a link on its host path the
sandbox does not recreate, a relocated projection), and every unset name is
recorded. A refusal of the compiler becomes a launch refusal
(:class:`~autoforge.errors.SandboxLaunchRefusedError`), on the path a
failure to spawn takes.

:func:`config_file_problem` answers the converse question for the
controller's own input (#280): whether the ``--config`` file lies where an
enforced launch writes, the repository's trees and what the providers'
declarations make writable (:func:`declared_writable`), so that the next
command would run on what an agent left there.

Pure except for the compiler's ``realpath`` and ``lstat``, ``realpath`` of
the environment's path values and ``lstat`` and ``readlink`` on their way,
the first line of an executable read for its interpreter, and the
configuration file's lookup walk (``lstat`` and ``readlink``, from the
working directory for a relative path), its ``stat`` and link count;
nothing is created or launched here.
"""

from __future__ import annotations

import dataclasses
import enum
import os
import posixpath
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

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
# ``<git common dir>/autoforge/sandbox/<key>``, the key being the worktree's
# name.
SANDBOX_DIR = "autoforge/sandbox"
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
    projected read-only where their host paths resolve, ``home_config``
    (files in the operator's home the CLI finds by that path) read-only at
    that path in the private home, wherever it resolves on the host (C7,
    D8.3), ``state`` (directories the adapter made for this launch)
    writable; ``path_dirs`` are put first on ``PATH``; ``path_names`` are
    the provider's path-valued variables (D8.2). ``operator_config_roots``
    are where the CLI loads the operator's own settings, hooks and
    extensions from (D7.2). ``spelled_paths`` are paths the launch names as
    written (a script's absolute ``#!`` interpreter): each leads inside
    where it leads on the host, through the links on its way (#225).
    """

    executable: str = ""
    install_roots: tuple[str, ...] = ()
    path_dirs: tuple[str, ...] = ()
    spelled_paths: tuple[str, ...] = ()
    config: tuple[str, ...] = ()
    home_config: tuple[str, ...] = ()
    state: tuple[str, ...] = ()
    auth: tuple[AuthItem, ...] = ()
    operator_config_roots: tuple[str, ...] = ()
    path_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class LaunchFacts:
    """The controller's facts about one launch, as data (ADR 0005 D13.2).

    ``workspace`` is what the launch works in (the issue worktree, the
    repository root, the exported tree). ``git_entry`` is the workspace's
    ``.git``; ``git_dir`` is its own git directory (the worktree's admin
    dir) and ``common_dir`` the shared one. ``private_objects`` and
    ``overlay_work`` are the issue's object overlay layers, ``cache`` the
    persistent cache when the launch gets one
    (empty: its cache is inside the private home), ``read_context`` the
    launch's read context, ``controller_config`` the directory #165's
    overrides point into. ``home`` is the operator's home and
    ``controller_env`` the controller's environment, for D4.3.
    ``operator_paths`` are ``sandbox.read_only_paths``, as configured
    (D15.4): read-only for every launch class.
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
    operator_paths: tuple[str, ...] = ()


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

    agent = kind is LaunchClass.REMOTE_AGENT
    declared = _declared(needs if agent else None, facts.operator_paths, facts.home)
    projections += declared.projections
    network = NetworkPosition.HOST if agent else facts.network
    return SandboxRequest(
        home=facts.home,
        controller_env=facts.controller_env,
        projections=tuple(projections),
        masks=tuple(masks),
        overlays=tuple(overlays),
        denied_roots=tuple(dict.fromkeys(facts.denied_roots)),
        operator_config_roots=declared.operator_roots,
        keyring_items=declared.keyring,
        spelled_paths=declared.spelled,
        network=network,
        env=declared.env,
    )


OPERATOR_PATHS_KEY = "sandbox.read_only_paths"


def operator_path_problem(operator_paths: tuple[str, ...], denied_roots: tuple[str, ...]) -> str:
    """The first operator read-only path that overlaps a denied root, as a reason; "" if none.

    ADR 0005 D15.4: an operator path may not name, sit inside or contain
    what no launch may see -- the state directory, the lock, the
    controller's directory under the git common dir, another worktree, the
    operator's checkout in REMOTE. The loader cannot know these (they are
    the run's), so the engine's gate and ``doctor`` ask before a launch
    would; the compiler refuses the same overlap at the launch regardless.
    Both sides are compared resolved, as the compiler compares them.
    """
    denied = [(root, os.path.realpath(root)) for root in denied_roots]
    for index, path in enumerate(operator_paths):
        real = os.path.realpath(path)
        for root, resolved in denied:
            if _inside(real, resolved) or _inside(resolved, real):
                return (
                    f"{OPERATOR_PATHS_KEY}[{index}] {path!r} overlaps {root!r}, which no "
                    "launch may see (ADR 0005 D15.4: the state directory, the lock, the "
                    "controller's directory under the git common dir, another worktree, and "
                    "in REMOTE the operator's checkout)"
                )
    return ""


# The links a lookup follows before it is a loop, as the kernel counts them
# (``MAXSYMLINKS``): past it ``open`` fails, so nothing further is reached.
_MAX_LOOKUP_LINKS = 40


def absolute_spelling(path: str | os.PathLike[str]) -> str:
    """``path`` as the configuration loader opens it, made absolute with every ``..`` kept (#280).

    The loader opens ``Path(path)``, which drops only ``.`` components and
    repeated slashes, and a relative path from the working directory: so
    the working directory is joined to it and nothing else is collapsed.
    ``os.path.abspath`` would remove ``name/..`` before ``name`` is
    followed, and when ``name`` is a symbolic link what is left names
    another file than the one ``open`` reaches. Whatever checks where the
    file lies, records it or hands it to another process spells it so.
    """
    return os.fspath(Path(path).absolute())


def lookup_directories(path: str) -> tuple[str, ...]:
    """Each directory, resolved, in which opening ``path`` looks up a name (#280).

    Whoever can write one of them can replace what ``path`` opens: move the
    entry away and put another file or a link in its place. A relative path
    is walked from the working directory, and the path is taken as spelled
    (:func:`absolute_spelling`): a ``..`` leads to the parent of the
    directory the walk has reached, any link before it already followed, as
    the kernel takes it, and a directory a name was looked up in stays
    listed when a ``..`` leads back out of it. Symbolic links are followed
    as the kernel does (a relative target from the directory holding the
    link, an absolute one from ``/``), so a link inside a tree and a link
    into one both show. It stops at a name that is not there, and after more
    links than a lookup follows: nothing past either is reached yet, and the
    directory the name was looked up in is already listed. ``lstat`` and
    ``readlink`` only; nothing is opened.
    """
    return _lookup(path)[0]


def _lookup(path: str) -> tuple[tuple[str, ...], str]:
    """:func:`lookup_directories`, and the entry the walk ends at, resolved.

    The entry is "" when the walk stops before the last name.
    """
    looked_up: list[str] = []
    current = "/"
    pending = [part for part in absolute_spelling(path).split("/") if part]
    links = 0
    while pending:
        name = pending.pop(0)
        if name == ".":
            continue
        if name == "..":
            current = posixpath.dirname(current)
            continue
        if current not in looked_up:
            looked_up.append(current)
        entry = posixpath.join(current, name)
        try:
            is_link = os.path.islink(entry)
            if not is_link and not os.path.lexists(entry):
                return tuple(looked_up), ""
            target = os.readlink(entry) if is_link else ""
        except OSError:
            return tuple(looked_up), ""
        if not is_link:
            current = entry
            continue
        links += 1
        if links > _MAX_LOOKUP_LINKS:
            return tuple(looked_up), ""
        if target.startswith("/"):
            current = "/"
        pending[:0] = [part for part in target.split("/") if part]
    return tuple(looked_up), current


def declared_writable(needs: ProviderRequirements) -> tuple[tuple[str, str], ...]:
    """Each host directory a provider's declaration makes writable, with what it is (#280).

    Its state directories and the directory of every ``dir-rw`` credential
    item, as :func:`sandbox_request` projects them read-write: what a
    launch of the provider writes on the host besides the engine's roots,
    a dedicated ``CLAUDE_CONFIG_DIR``, ``PI_CODING_AGENT_DIR`` or OpenCode
    database directory among them. Each is ``(path, phrase)``, the path as
    declared.
    """
    state = [(path, f"a state directory its provider declares ({path})") for path in needs.state]
    auth = [(item.source, item.label) for item in needs.auth if item.mode is AuthMode.DIR_RW]
    return (*state, *auth)


def config_file_problem(
    config_file: str,
    written_roots: tuple[str, ...],
    mode: str,
    declared: Mapping[str, str] | None = None,
) -> str:
    """Why an enforced launch could rewrite the configuration file, as a refusal; "" if none.

    ``written_roots`` are the trees the repository's enforced launches write
    and that outlive the launch: its working trees (the checkout, and every
    worktree registered to it, the agent worktrees included) and its sandbox
    directories (the caches). ``declared`` are
    the directories the providers' declarations make writable
    (:func:`declared_writable`), each with the phrase that says what it is
    and whose launches write it. A file is within reach when opening it
    looks up a name in one of either (:func:`lookup_directories`), and when
    it has a second hard link, which may lie in one (ADR 0005 D4.4). The
    next command would run on what such a file says: the sandbox mode, the
    controller's GitHub command and the commands of every profile
    ``doctor`` checks (#280). Both sides are compared resolved.

    ``config_file`` is the path the loader was given, relative or not. It
    is walked as spelled (:func:`absolute_spelling`), never normalized
    first: a ``..`` after a link is taken where the link leads, so a file
    in a root is not passed under the name of one beside it, and a path
    that looks up a name in a root and leaves it again is within reach
    there. The file examined is the one the path opens: the walk has to end
    at it, or nothing shows where it is reached from and it is refused, and
    its own link count is read.
    """
    config_file = absolute_spelling(config_file)
    direct = os.path.realpath(config_file) == config_file
    roots = [(root, os.path.realpath(root), "") for root in written_roots]
    roots += [(root, os.path.realpath(root), what) for root, what in (declared or {}).items()]
    walked, reached = _lookup(config_file)
    for directory in walked:
        for root, resolved, what in roots:
            if _inside(directory, resolved):
                # The deepest directory of the walk in this root names the lookup.
                deepest = [d for d in walked if _inside(d, resolved)][-1]
                where = "lies in" if direct else f"is reached through {deepest}, in"
                if what:
                    return (
                        f"the configuration file {config_file} {where} {root}, {what} "
                        f"(sandbox.mode is {mode}): a sandboxed agent could rewrite it, and the "
                        "next command would run on what it says, sandbox.mode, github.command "
                        "and the profiles' commands included (#280). Move it outside that "
                        "directory and every working tree of this repository (for example to "
                        "~/.config/autoforge/) and pass that path with --config, give the "
                        "provider another directory, or set sandbox.mode: disabled"
                    )
                return (
                    f"the configuration file {config_file} {where} {root}, a tree that "
                    f"launches inside the outer sandbox write (sandbox.mode is {mode}): a "
                    "sandboxed agent could rewrite it, and the next command would run on what "
                    "it says, sandbox.mode, github.command and the profiles' commands "
                    "included (#280). Move it outside every working tree of this repository "
                    "(for example to ~/.config/autoforge/) and pass that path with --config, "
                    "or set sandbox.mode: disabled"
                )
    try:
        opened = os.stat(config_file)
    except OSError:
        return ""
    try:
        followed = bool(reached) and os.path.samestat(opened, os.stat(reached))
    except OSError:
        followed = False
    if not followed:
        return _not_followed(config_file, mode)
    links = opened.st_nlink
    if links > 1:
        return (
            f"the configuration file {config_file} has {links} hard links, and another one "
            "may lie in a tree that launches inside the outer sandbox write (sandbox.mode is "
            f"{mode}; ADR 0005 D4.4): a sandboxed agent could rewrite it through that link "
            "(#280). Copy it to a file of its own outside every working tree of this "
            "repository and every directory a provider's launches write, and pass that path "
            "with --config, or set sandbox.mode: disabled"
        )
    return ""


def _not_followed(config_file: str, mode: str) -> str:
    return (
        f"the configuration file {config_file} opens another file than its lookup walk was "
        "followed to, so nothing shows that it lies outside every tree that launches inside "
        f"the outer sandbox write (sandbox.mode is {mode}; #280). Pass the file's own path "
        "with --config, or set sandbox.mode: disabled"
    )


@dataclass(frozen=True)
class _Declared:
    projections: tuple[ProjectionRequest, ...]
    env: tuple[tuple[str, str], ...]
    keyring: tuple[str, ...]
    operator_roots: tuple[str, ...]
    spelled: tuple[str, ...] = ()


def _declared(
    needs: ProviderRequirements | None, operator_paths: tuple[str, ...], home: str
) -> _Declared:
    """The declared projections of a launch: the provider's, then the operator's.

    Every one is :attr:`Origin.DECLARED`, so C6 decides its overlaps. The
    provider's read-only install roots and configuration are kept outermost,
    as before; an operator path outside the home that one of them already
    shows is dropped for this launch, and a provider root inside an
    operator path is dropped in its favour (both are read-only, so the outer
    one shows the inner). An operator path is projected by its spelling
    (the compiler resolves it, C1), so a launch class without a provider
    sees exactly the configured list; one inside the home appears at that
    spelling in the private home (C7, D15.4), as a provider's home
    configuration does.
    """
    projections: list[ProjectionRequest] = []
    env: list[tuple[str, str]] = []
    keyring: list[str] = []
    in_home = [p for p in operator_paths if _inside(p, home) and p != home]
    elsewhere = [p for p in operator_paths if p not in in_home]
    roots = _outermost([*needs.install_roots, *needs.config]) if needs is not None else []
    if roots:
        real = {p: os.path.realpath(p) for p in elsewhere}
        elsewhere = [p for p in elsewhere if not any(_inside(real[p], r) for r in roots)]
        roots = [r for r in roots if not any(_inside(r, real[p]) for p in elsewhere)]
    for path in roots:
        projections.append(
            ProjectionRequest(path, Access.READ_ONLY, Origin.DECLARED, label=f"{path} (provider)")
        )
    for path in elsewhere:
        projections.append(
            ProjectionRequest(
                path, Access.READ_ONLY, Origin.DECLARED, label=f"{path} ({OPERATOR_PATHS_KEY})"
            )
        )
    for path in in_home:
        projections.append(
            ProjectionRequest(
                path,
                Access.READ_ONLY,
                Origin.DECLARED,
                target=path,
                label=f"{path} ({OPERATOR_PATHS_KEY})",
            )
        )
    if needs is None:
        return _Declared(tuple(projections), (), (), ())
    for path in needs.home_config:
        projections.append(
            ProjectionRequest(
                path, Access.READ_ONLY, Origin.DECLARED, target=path, label=f"{path} (provider)"
            )
        )
    for path in needs.state:
        projections.append(
            ProjectionRequest(path, Access.WRITABLE, Origin.DECLARED, label=f"{path} (provider)")
        )
    for item in needs.auth:
        if item.mode is AuthMode.KEYRING:
            keyring.append(item.source or item.label)
            continue
        if item.mode not in (AuthMode.FILE_RO, AuthMode.DIR_RW):
            continue
        access = Access.WRITABLE if item.mode is AuthMode.DIR_RW else Access.READ_ONLY
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
    return _Declared(
        tuple(projections),
        tuple(env),
        tuple(keyring),
        tuple(needs.operator_config_roots),
        tuple(needs.spelled_paths),
    )


def provider_request(
    needs: ProviderRequirements,
    *,
    home: str,
    controller_env: Mapping[str, str],
    workspace: str,
    operator_paths: tuple[str, ...] = (),
    denied_roots: tuple[str, ...] = (),
) -> SandboxRequest:
    """A provider's declaration alone, around a scratch ``workspace`` (ADR 0005 D14.1).

    What ``doctor`` compiles to say whether a declaration is allowed, and
    launches the provider's ``--version`` in to prove it is enough: the
    rows every agent launch gets from its provider and the operator, the
    workspace writable, and none of a workflow's (no repository, no cache,
    no read context). The same rules as :func:`sandbox_request`'s.
    """
    declared = _declared(needs, operator_paths, home)
    workspace_projection = ProjectionRequest(
        workspace, Access.WRITABLE, Origin.ENGINE, label="the scratch workspace"
    )
    return SandboxRequest(
        home=home,
        controller_env=controller_env,
        projections=(workspace_projection, *declared.projections),
        denied_roots=tuple(dict.fromkeys(denied_roots)),
        operator_config_roots=declared.operator_roots,
        keyring_items=declared.keyring,
        spelled_paths=declared.spelled,
        env=declared.env,
    )


# The phrases :func:`class_summary` shows for a role path (:func:`role`).
_ROLE_RE = re.compile(r"/@([a-z-]+)")


def _shown(path: str) -> str:
    return _ROLE_RE.sub(lambda m: f"<{m.group(1).replace('-', ' ')}>", path)


def summary_facts(
    launch_class: LaunchClass,
    network: NetworkPosition,
    *,
    cache: bool,
    operator_paths: tuple[str, ...] = (),
) -> LaunchFacts:
    """A launch class's facts with every controller path a role name, for display.

    The shape :meth:`ControllerEngine` builds for a launch of the class
    (D13.2), before anything is created: what the dry-run plan and
    ``doctor`` describe. ``cache`` says whether the launch gets the
    persistent cache (§2.5).
    """
    facts = LaunchFacts(
        launch_class=launch_class,
        workspace=role("workspace"),
        home=role("home"),
        controller_env={},
        controller_config=role("controller-config"),
        denied_roots=(role("denied-roots"),),
        network=network,
        operator_paths=operator_paths,
        cache=role("cache") if cache and launch_class is not LaunchClass.EXPORT_COMMAND else "",
    )
    if launch_class is LaunchClass.EXPORT_COMMAND:
        return facts
    return dataclasses.replace(
        facts,
        git_entry=posixpath.join(role("workspace"), ".git"),
        git_dir=role("admin-dir"),
        common_dir=role("common-dir"),
        private_objects=role("private-objects"),
        overlay_work=role("overlay-work"),
        read_context=role("read-context"),
    )


def class_summary(facts: LaunchFacts) -> str:
    """What a launch of ``facts.launch_class`` gets, in words, from §2.5's mapping.

    Derived from :func:`sandbox_request` over :func:`summary_facts`, so the
    words cannot drift from what a launch is given: the writable and
    read-only projections by label, the masks, the overlays, what the
    compiler adds to every launch, the denied roots and the network. A
    provider's own declaration is named, never listed: it is per provider
    (``doctor`` lists it).
    """
    request = sandbox_request(facts)
    writable = [p.label for p in request.projections if p.access is Access.WRITABLE]
    read_only = [p.label for p in request.projections if p.access is Access.READ_ONLY]
    parts = []
    if writable:
        parts.append("writable: " + ", ".join(writable))
    if read_only:
        parts.append("read-only: " + ", ".join(read_only))
    if request.masks:
        parts.append("masked: " + ", ".join(_shown(m) for m in request.masks))
    for overlay in request.overlays:
        parts.append(
            f"overlay: {overlay.label or _shown(overlay.lower)}, its writes landing in "
            f"{_shown(overlay.upper)}"
        )
    if facts.launch_class is LaunchClass.REMOTE_AGENT:
        parts.append(
            "the provider's executable, install roots, configuration and model credential "
            "as its adapter declares them"
        )
    parts.append(
        "always: /usr and /etc read-only, a private home and /tmp per launch, a new /proc, "
        "a minimal /dev, /sys read-only"
    )
    parts.append(
        "absent: the state directory and run logs, the lock, <git common dir>/autoforge "
        "but the launch's own carve-outs, every other worktree"
        + (" and the controller checkout" if facts.launch_class is LaunchClass.REMOTE_AGENT else "")
        + "; credentials, keyrings, /run and everything not listed"
    )
    parts.append(
        "network: "
        + (
            "isolated (no loopback, no abstract socket, no egress)"
            if request.network is NetworkPosition.ISOLATED
            else "the host's (abstract sockets of the host unreachable; not sandboxed)"
        )
    )
    return "; ".join(parts)


# The version label of :func:`launch_template`'s encoding. The compiler's
# fixed choices (a private home and temp, a new /proc, a minimal /dev) are
# not in a request, so a change to one of them is a change of this label.
TEMPLATE_VERSION = "autoforge-sandbox-template-v1"


def role(name: str) -> str:
    """The stand-in path of role ``name`` in a policy template (ADR 0006 D8.1)."""
    return f"/@{name}"


def template_facts(
    launch_class: LaunchClass,
    network: NetworkPosition,
    *,
    operator_paths: tuple[str, ...] = (),
) -> LaunchFacts:
    """A repository-defined command's facts with every concrete path a role name.

    The role-level inputs the engine passes the compiler for a validator
    (ADR 0006 D8.1), before any path is bound: the export is
    ``candidate-root``, and so on, and every denied root is one
    ``denied-roots`` role, since how many worktrees are registered is not a
    fact of the policy. ``operator_paths`` (``sandbox.read_only_paths``,
    D15.4) are configuration, not paths of the launch: they are in the
    template as configured. Only the export command has a template.
    """
    workspace = role("candidate-root")
    facts = LaunchFacts(
        launch_class=launch_class,
        workspace=workspace,
        home=role("home"),
        controller_env={},
        controller_config=role("controller-config"),
        denied_roots=(role("denied-roots"),),
        network=network,
        operator_paths=operator_paths,
    )
    if launch_class is not LaunchClass.EXPORT_COMMAND:
        raise ValueError(f"no policy template for the {launch_class.value}")
    return facts


def launch_template(facts: LaunchFacts) -> dict:
    """The path-independent policy template of a command launch (ADR 0006 D8.1).

    §2.5's request for ``facts`` (:func:`sandbox_request`, pure for a
    command class), as canonical data: the projections and their access,
    the masks, the overlays, the denied roots, the network position and the
    compiler's options. Labels are prose and are left out. Given
    :func:`template_facts`, no concrete path is in it, so two exports of one
    candidate in two temporary directories give one template.
    """
    request = sandbox_request(facts)
    return {
        "version": TEMPLATE_VERSION,
        "launch_class": facts.launch_class.name,
        "projections": [
            {
                "source": p.source,
                "access": p.access.value,
                "origin": p.origin.value,
                "target": p.target,
                "carve_out": p.carve_out,
            }
            for p in request.projections
        ],
        "masks": list(request.masks),
        "overlays": [
            {"lower": o.lower, "upper": o.upper, "work": o.work, "carve_out": o.carve_out}
            for o in request.overlays
        ],
        "denied_roots": list(request.denied_roots),
        "operator_config_roots": list(request.operator_config_roots),
        "system_runtime": request.system_runtime,
        "home_size_bytes": request.home_size_bytes,
        "tmp_size_bytes": request.tmp_size_bytes,
        "network": request.network.value,
        "sys": request.sys.value,
        "env": [list(pair) for pair in request.env],
        "env_unset": list(request.env_unset),
    }


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


def inside_spelling(description: LaunchDescription, value: str) -> str | None:
    """The absolute host path ``value`` as a launch must spell it inside (D8.2).

    ``None`` when nothing of the description shows what it names. Its own
    spelling when that leads inside to where its host target is shown;
    else that place: a link on its host path that the sandbox does not
    recreate (a link in the private home, or anywhere the root is empty),
    or a projection that appears elsewhere than at its host path (the cache).
    """
    if not value.startswith("/"):
        return None
    inner = description.inside_path(os.path.realpath(value))
    if inner is None or description.resolve_inside(value) == inner:
        return None if inner is None else value
    return inner


def _auth_variables(needs: ProviderRequirements | None) -> dict[str, str]:
    """The variables naming a projected auth item at its host path: name to host path."""
    if needs is None:
        return {}
    return {
        item.env_name: item.source
        for item in needs.auth
        if item.env_name
        and item.target is None
        and item.mode in (AuthMode.FILE_RO, AuthMode.DIR_RW)
    }


def rewrite_environment(
    description: LaunchDescription,
    env: Mapping[str, str],
    needs: ProviderRequirements | None = None,
) -> LaunchDescription:
    """``description`` with the launch's path-valued names made true inside (D8.2).

    A name AutoForge knows to be a path (the CA-bundle names, #165's
    overrides, the provider's ``path_names``) whose absolute value names
    nothing visible inside is unset, and one whose spelling leads elsewhere
    inside is set to where what it names is (:func:`inside_spelling`); a
    value that is not an absolute path (``OPENCODE_DB=:memory:``, a name
    relative to the provider's own data directory) passes unchanged, as any
    other variable does. ``PATH`` keeps its visible absolute entries, each so
    spelled, after the provider's ``path_dirs``, and is unset when none is
    left. The compiler's own names (``HOME``, ``XDG_*``, ``TMPDIR``) are
    left alone; the variable an auth item names (``PI_CODING_AGENT_DIR``)
    is set by the compiler to the item's host path and spelled here too.
    """
    compiled = dict(description.env_set)
    auth = {name: path for name, path in _auth_variables(needs).items() if name in compiled}
    owned = (set(compiled) | set(description.env_unset)) - set(auth)
    names = [*CA_BUNDLE_NAMES, *OVERRIDE_PATH_NAMES, *(needs.path_names if needs else ())]
    values = {name: env.get(name, "") for name in dict.fromkeys(names)}
    values.update(auth)
    moved: dict[str, str] = {}
    unset: set[str] = set()
    for name, value in values.items():
        if name in owned or not value.startswith("/"):
            continue
        spelled = inside_spelling(description, value)
        if spelled is None:
            unset.add(name)
        elif spelled != value:
            moved[name] = spelled
    if "PATH" not in owned and ("PATH" in env or (needs and needs.path_dirs)):
        entries = [*(needs.path_dirs if needs else ()), *env.get("PATH", "").split(":")]
        spelled_entries = (inside_spelling(description, entry) for entry in entries)
        kept = list(dict.fromkeys(entry for entry in spelled_entries if entry))
        if kept:
            if ":".join(kept) != env.get("PATH"):
                moved["PATH"] = ":".join(kept)
        elif "PATH" in env:
            unset.add("PATH")
    if not unset and not moved:
        return description
    env_set = {name: value for name, value in compiled.items() if name not in unset}
    env_set.update(moved)
    return dataclasses.replace(
        description,
        env_set=tuple(sorted(env_set.items())),
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


def _shebang(executable: str) -> list[str]:
    """The words of an executable's ``#!`` line, ``[]`` without one. Only
    the first line is read."""
    try:
        with open(executable, "rb") as handle:
            head = handle.read(_SHEBANG_BYTES)
    except OSError:
        return []
    if not head.startswith(b"#!"):
        return []
    return head[2:].split(b"\n", 1)[0].decode("utf-8", "replace").split()


def _interpreter(words: list[str], search_path: str | None) -> str:
    if not words:
        return ""
    program = words[0]
    if posixpath.basename(program) == "env":
        names = [word for word in words[1:] if not word.startswith("-") and "=" not in word]
        if not names:
            return ""
        return resolve_command(names[0], search_path)
    return resolve_command(program, search_path)


def interpreter_of(executable: str, search_path: str | None) -> str:
    """The resolved interpreter an executable script names (``#!``), or "".

    ``#!/usr/bin/env [-S] name`` is looked up on ``search_path``, as ``env``
    would do inside. Only the first line is read.
    """
    return _interpreter(_shebang(executable), search_path)


def executable_requirements(
    command: str, *, home: str, search_path: str | None
) -> tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """``(executable, install roots, PATH dirs, spelled paths)`` for the command a launch names.

    The executable resolved on the host; its install root; and, for a
    script, its interpreter's install root and directory, which goes first
    on ``PATH`` so that ``#!/usr/bin/env node`` finds it inside. An
    absolute ``#!`` path, ``env``'s too, is also a spelled path, as
    written, and its resolved program's install root is projected: the
    kernel looks it up so, and a link on its way, such as Homebrew's
    ``opt/`` into ``Cellar/``, need not lie in any root projected (#225).
    """
    executable = resolve_command(command, search_path)
    if not executable:
        return "", (), (), ()
    roots = [install_root(executable, home)]
    path_dirs: list[str] = []
    spelled: list[str] = []
    words = _shebang(executable)
    program = resolve_command(words[0], search_path) if words and words[0].startswith("/") else ""
    if program:
        # What the kernel runs, as written: ``env`` is no exception (R1-F1).
        roots.append(install_root(program, home))
        spelled.append(words[0])
    interpreter = _interpreter(words, search_path)
    if interpreter:
        roots.append(install_root(interpreter, home))
        path_dirs.append(posixpath.dirname(interpreter))
    return executable, tuple(dict.fromkeys(roots)), tuple(path_dirs), tuple(spelled)
