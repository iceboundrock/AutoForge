"""Each provider's model credential, mapped to one of #270's delivery mechanisms (#290).

ADR 0013 D14: identity stays on the host, and the execution environment can
be rebuilt. The operator's login and its refresh state are long-lived and
the operator's; a container launch gets only a projection, made for that
launch from the host and removed with it. #270
(:mod:`autoforge.sandbox_container`) delivers a projection by a closed set
of mechanisms: an environment variable, named on the create and valued only
in the runtime client's environment, and, on a bind runtime only, a
read-only file in the private home. This module decides, for one agent
launch, which credential is projected and by which mechanism, or refuses
the launch with a reason that names variables and kinds, never a value.

The rules, applied to what a provider adapter declares
(:class:`CredentialRoutes`, from ``AgentProvider.credential_routes``) and
to the launch's environment:

- **Agent launches only.** A repository command (``validation.commands``,
  ``merge.verification_commands``) gets no model credential (ADR 0005
  §2.5, #286): :func:`plan_credentials` refuses its launch class, and its
  container receives :func:`command_environment` alone.
- **One credential, chosen by the operator.** Exactly one of the profile's
  supported routes must be present. Two would let the CLI's own precedence
  choose between them, so a subscription token beside a paid API key never
  falls back silently from one to the other; none is a refusal that says
  which variable to set.
- **Never a refreshing login.** A credential whose refresh rotates a
  refresh token (:attr:`Refresh.ROTATING`) is never projected: no
  controller-owned path returns a refreshed login to the host, so the
  container's first refresh would invalidate the host's copy. A login held
  in a file or a directory (the adapter's ``file-ro``, ``dir-rw`` and
  ``keyring`` items: Claude Code's login, OpenCode's database, Pi's agent
  directory) is never projected either: the CLI rewrites it, and AutoForge
  never reads, copies or logs a credential's contents (ADR 0005 D7.2).
- **A positive allow-list, never a filter.** Only the selected variable is
  projected, and of the rest of the launch's environment the container
  receives only what :attr:`CredentialPlan.environment` holds: the closed
  list :data:`PASSED_NAMES` (locale, time zone, terminal, the git author
  identity) and the provider's declared settings
  (:attr:`CredentialRoutes.settings`), each by its exact name. Every other
  variable, whatever its name, is :attr:`CredentialPlan.withheld`: a
  credential is kept out by not being listed, not by looking like one, so a
  name no rule recognises (``AZURE_OPENAI_KEY``, ``DATABASE_URL``) is
  withheld too. The container backend (#271) gives the launch container
  :meth:`CredentialPlan.container_environment` and the projection, and of
  the launch's environment nothing else.
- **A fresh private store, never an inherited one.** ``HOME`` and each
  variable that locates the CLI's own credential store
  (:attr:`CredentialRoutes.stores`: ``CLAUDE_CONFIG_DIR``, ``OPENCODE_DB``,
  ``PI_CODING_AGENT_DIR``) are set to a path in the launch container's
  private home, whatever the launch's environment held: a host path would
  be absent or unwritable inside, and a path into the workspace or a
  volume could select a store that the repository or an earlier launch
  left there, whose login the CLI would use ahead of the projected key.
  The private home is a tmpfs made at the container's create
  (:func:`autoforge.sandbox_container.create_options`), so each store
  starts empty and goes with the container.
- **The CLI's integration isolation, set and never inherited.** What keeps
  a CLI's MCP servers, plugins and the repository's own configuration off
  (#165; ADR 0013 D7: an installed integration stays disabled) is on the
  argv for Claude Code and Pi, which a container launch runs unchanged,
  and in variables for OpenCode, which has no flag for it. The adapter
  declares those variables with their values
  (:attr:`CredentialRoutes.isolation`), a path among them naming a place
  in the private home, and the plan sets each one whatever the launch's
  environment held: an operator's ``OPENCODE_CONFIG_PROJECT_DISABLE=0`` or
  ``OPENCODE_CONFIG`` is withheld like any other variable. Without them,
  OpenCode would load the ``opencode.json`` and ``.opencode/`` of the
  workspace, which the repository controls. Claude Code's shell variables
  (``claude_isolation_env``) are not among them: they keep the functions
  of the operator's shell startup files out of its Bash tool, and the
  private home, a new tmpfs, holds no such file; ``CLAUDE_CODE_SHELL`` and
  ``CLAUDE_CODE_SHELL_PREFIX`` are withheld, so neither names a program of
  the operator's.

Every supported route is a static value (an API key, or Claude Code's
long-lived ``claude setup-token`` token), so every route maps to the
environment variable, which both staging forms deliver: no provider maps to
the file mechanism, and a copy-in runtime loses no route. Nothing is
refreshed inside a container, so concurrent workers share no writable
credential state. Each launch's plan is made from the environment it is
given, which is the controller process's own: a value the operator changes
in a shell reaches the launches of a controller started after the change,
never those of one already running.

Pure: no process, no file read, no state. The projection's value is a
:class:`~autoforge.sandbox_container.Secret`; :meth:`CredentialPlan.facts`
and :meth:`CredentialPlan.describe` carry names, kinds and capability only.
Not reachable before #271 builds the container backend and #273 makes it
selectable.
"""

from __future__ import annotations

import enum
import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from .child_env import credential_looking_word, undoes_isolation
from .errors import SandboxPolicyError
from .sandbox_container import HOME_TARGET, EnvCredential, Projection, Secret
from .sandbox_policy import AuthItem, AuthMode, LaunchClass, ProviderRequirements

_RULE = "D14 provider credential mapping (#290)"

# What a launch container receives of the launch's environment whatever its
# provider, by exact name, never a prefix: the locale, the time zone, the
# terminal type and the git author identity (an agent commits in the
# workspace). None is a credential or locates one. ``PATH``, ``HOME``, the
# ``XDG_*`` directories, ``TMPDIR`` and the proxy variables are host facts
# (a proxy URL can carry a password), and the image and the private home
# give the container its own.
PASSED_NAMES = frozenset(
    {
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_ADDRESS",
        "LC_COLLATE",
        "LC_CTYPE",
        "LC_IDENTIFICATION",
        "LC_MEASUREMENT",
        "LC_MESSAGES",
        "LC_MONETARY",
        "LC_NAME",
        "LC_NUMERIC",
        "LC_PAPER",
        "LC_TELEPHONE",
        "LC_TIME",
        "TZ",
        "TERM",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    }
)

# What a repository command's container (``validation.commands``,
# ``merge.verification_commands``) receives of the launch's environment, by
# exact name: the locale, the time zone and the terminal type. No model
# credential, no store, no provider setting and no git identity: it runs
# the candidate's own code and commits nothing (ADR 0005 §2.5, #286).
COMMAND_PASSED_NAMES = frozenset(
    name for name in PASSED_NAMES if not name.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))
)

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
# Words that make a declared setting's name read as a credential, beyond
# #286's (``TOKEN``, ``PAT``, ``SECRET``, ``PASSWORD``, ``GITHUB``): a guard
# on what an adapter declares, never the boundary, which is the list itself.
_KEY_WORD_RE = re.compile(r"(?:^|_)(?:API_?KEY|KEY|CREDENTIALS?|AUTH|PASS|PASSWD|COOKIE)(?:_|$)")


class CredentialKind(enum.Enum):
    """What a credential is, as a plan records it: never its value."""

    API_KEY = "provider API key"
    # ``claude setup-token``: an OAuth access token valid for a year, with no
    # refresh token; the operator makes it on the host and renews it there.
    OAUTH_TOKEN = "long-lived OAuth token"
    OAUTH_REFRESH_TOKEN = "OAuth refresh token"
    GATEWAY_TOKEN = "gateway bearer token"
    CLOUD = "cloud provider credentials"
    STORED_LOGIN = "stored login"
    KEYRING = "keychain login"


class Refresh(enum.Enum):
    """Who refreshes a credential, and how."""

    # Never refreshed by a launch: valid until it expires or is revoked; the
    # operator rotates it on the host, and the first launch of a controller
    # started with the new value projects it (a running controller keeps the
    # environment it started with).
    NONE = "never refreshed in a launch"
    # Refreshed by the CLI, which rotates its refresh token on use.
    ROTATING = "refreshed by the CLI, rotating its refresh token"


class Mechanism(enum.Enum):
    """#270's delivery mechanisms (ADR 0013 D14), a closed set."""

    ENV = "an environment variable of the launch container"
    FILE = "a read-only file in the launch container's private home"


@dataclass(frozen=True)
class EnvRoute:
    """A credential a CLI takes from one environment variable."""

    name: str
    kind: CredentialKind
    refresh: Refresh = Refresh.NONE


@dataclass(frozen=True)
class UnsupportedRoute:
    """A variable that selects a route the container backend does not take.

    Present in the launch's environment, it refuses the launch rather than
    letting the CLI switch to that route, or away from it, unseen.
    """

    name: str
    kind: CredentialKind
    reason: str


def private_path(relative: str) -> str:
    """Where ``relative``, a path of the launch container's private home, is inside it."""
    return posixpath.join(HOME_TARGET, relative)


def _in_private_home(path: str) -> bool:
    return path.startswith(HOME_TARGET + "/") and posixpath.normpath(path) == path


@dataclass(frozen=True)
class PrivateStore:
    """A variable that locates the CLI's own credential store, and its private place.

    ``path`` is relative to the launch container's private home
    (:data:`~autoforge.sandbox_container.HOME_TARGET`): the plan sets
    ``name`` to it, whatever the launch's environment held.
    """

    name: str
    path: str

    @property
    def value(self) -> str:
        """The variable's value inside the launch container."""
        return private_path(self.path)


@dataclass(frozen=True)
class CredentialRoutes:
    """A provider adapter's declaration for one profile: names and kinds, no value.

    ``scope`` is the model provider the launch authenticates to (``anthropic``,
    ``openai``, ``deepseek``); ``routes`` the variables the profile may take
    its credential from; ``unsupported`` the variables that select a route
    the container backend refuses; ``refusal`` why no route of this profile
    can be projected at all ("" for none), with what the operator can do.
    ``stores`` are the variables that locate the CLI's own credential store,
    each set to a fresh place in the private home; ``settings`` the exact
    names of the CLI's settings, none a credential, that the container
    receives from the launch's environment besides :data:`PASSED_NAMES`.
    ``isolation`` is the CLI's integration isolation that its argv cannot
    carry (#165, ADR 0013 D7), as ``(name, value)`` pairs the container
    receives whatever the launch's environment held: a value that is a
    path names a place in the private home.
    """

    scope: str
    routes: tuple[EnvRoute, ...] = ()
    unsupported: tuple[UnsupportedRoute, ...] = ()
    refusal: str = ""
    stores: tuple[PrivateStore, ...] = ()
    settings: tuple[str, ...] = ()
    isolation: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class NotProjected:
    """A login the launch's provider declared that no projection carries."""

    label: str
    kind: CredentialKind
    reason: str


_STORED_REASON = (
    "the CLI refreshes or rewrites it in place, and no controller-owned path returns "
    "what a launch wrote to the host's copy, so it is never projected (ADR 0013 D14); "
    "AutoForge never reads or copies it (ADR 0005 D7.2)"
)
_KEYRING_REASON = "a keychain or Secret Service login is never projected (ADR 0005 D7.1)"


def _not_projected(item: AuthItem) -> NotProjected | None:
    if item.mode is AuthMode.KEYRING:
        return NotProjected(item.label, CredentialKind.KEYRING, _KEYRING_REASON)
    if item.mode in (AuthMode.FILE_RO, AuthMode.DIR_RW):
        return NotProjected(
            f"{item.label} ({item.mode.value})", CredentialKind.STORED_LOGIN, _STORED_REASON
        )
    return None


@dataclass(frozen=True)
class CredentialPlan:
    """One agent launch's credential: what is projected, how, and what is not.

    ``name`` is the variable projected, ``kind`` and ``refresh`` what it is,
    ``mechanism`` how #270 delivers it. ``not_projected`` are the provider's
    declared logins no projection carries. ``environment`` is every other
    variable the launch container receives, as ``(name, value)`` pairs: the
    private home, the private stores, the CLI's integration isolation and
    the allow-listed names the launch's environment holds, none a
    credential. ``withheld`` names every variable
    of the launch's environment the container does not receive from it.
    ``projection`` holds the value, as a :class:`Secret`, for the launch
    container's create alone; it is in no ``repr``, comparison or fact.
    """

    provider: str
    scope: str
    name: str
    kind: CredentialKind
    refresh: Refresh
    mechanism: Mechanism
    not_projected: tuple[NotProjected, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
    withheld: tuple[str, ...] = ()
    projection: Projection = field(default_factory=Projection, repr=False, compare=False)

    def container_environment(self) -> dict[str, str]:
        """The launch container's variables besides the projection, and of the
        launch's environment the only ones: no secret is among them."""
        return dict(self.environment)

    def facts(self) -> dict[str, object]:
        """What a journal, a dry run or ``doctor`` may record: presence, kind and capability."""
        return {
            "provider": self.provider,
            "scope": self.scope,
            "credential": self.name,
            "kind": self.kind.value,
            "refresh": self.refresh.value,
            "mechanism": self.mechanism.value,
            "not_projected": [item.label for item in self.not_projected],
            "environment": [name for name, _ in self.environment],
            "withheld": list(self.withheld),
        }

    def describe(self) -> str:
        """One line for an operator: the variable, its kind and how it is delivered."""
        text = (
            f"{self.provider}: {self.name} ({self.kind.value}, for {self.scope}) as "
            f"{self.mechanism.value}; {self.refresh.value}"
        )
        if self.not_projected:
            text += "; not projected: " + ", ".join(item.label for item in self.not_projected)
        text += "; the container also receives: " + ", ".join(name for name, _ in self.environment)
        if self.withheld:
            text += "; withheld: " + ", ".join(self.withheld)
        return text


def _refuse(subject: str, detail: str) -> SandboxPolicyError:
    return SandboxPolicyError(subject, _RULE, detail)


def command_environment(env: Mapping[str, str]) -> dict[str, str]:
    """A repository command's container environment: never a model credential (#286).

    ``HOME`` set to the private home, whatever ``env`` held, and of ``env``
    only the names :data:`COMMAND_PASSED_NAMES` lists: a positive allow-list,
    so a provider's key, a cloud credential, a database URL or a provider
    setting is withheld by not being listed, whatever its name. The
    container backend (#271) passes this, and nothing else of the launch's
    environment, to a command's run step; the command's container is
    created with no projection.
    """
    environment = {"HOME": HOME_TARGET}
    environment.update(
        (name, value) for name, value in env.items() if value and name in COMMAND_PASSED_NAMES
    )
    return environment


def _present(env: Mapping[str, str], name: str) -> bool:
    return bool(env.get(name))


def credential_looking(name: str) -> bool:
    """Whether variable ``name`` reads as a credential: a guard on what may be
    listed as passed (:data:`PASSED_NAMES`, an adapter's settings), never the
    filter of a launch's environment, which is the list itself."""
    upper = name.upper()
    return bool(credential_looking_word(upper) or _KEY_WORD_RE.search(upper))


def _check_declaration(provider: str, routes: CredentialRoutes) -> None:
    """Refuse a declaration that would pass a credential, leave a store inherited,
    or set an isolation variable that is a credential or names a place outside
    the private home."""
    credentials = {r.name for r in routes.routes} | {u.name for u in routes.unsupported}
    stores: set[str] = set()
    for store in routes.stores:
        path = store.path
        if (
            not _ENV_NAME_RE.match(store.name)
            or store.name in credentials
            or store.name == "HOME"
            or not path
            or path.startswith("/")
            or posixpath.normpath(path) != path
            or path.split("/")[0] == ".."
        ):
            raise _refuse(
                provider,
                f"the adapter declares {store.name!r} as a store at {path!r}: a store is a "
                "variable that names no credential, set to a normalized path inside the "
                "private home",
            )
        stores.add(store.name)
    for name in routes.settings:
        if (
            not _ENV_NAME_RE.match(name)
            or name in credentials
            or name in stores
            or credential_looking(name)
            or undoes_isolation(name)
        ):
            raise _refuse(
                provider,
                f"the adapter declares {name!r} as a setting the container receives, and it "
                "is a credential, a store or a name that could hand the launch one: a "
                "setting is never a credential",
            )
    taken = credentials | stores | set(routes.settings) | {"HOME"}
    for name, value in routes.isolation:
        if (
            not _ENV_NAME_RE.match(name)
            or name in taken
            or credential_looking(name)
            or undoes_isolation(name)
            or "\0" in value
            or ("/" in value and not _in_private_home(value))
        ):
            raise _refuse(
                provider,
                f"the adapter declares {name!r} as an isolation variable: one names no "
                "credential, store, passed setting or other isolation variable, and its "
                "value, when a path, is a normalized place inside the private home",
            )
        taken.add(name)


def plan_credentials(
    provider: str,
    routes: CredentialRoutes | None,
    needs: ProviderRequirements,
    env: Mapping[str, str],
    *,
    launch_class: LaunchClass,
) -> CredentialPlan:
    """The projection of one agent launch under the container backend, or a refusal.

    ``provider`` is the adapter's name, ``routes`` its declaration for the
    launch's profile (``None``: the adapter maps none), ``needs`` its sandbox
    declaration (whose ``file-ro``, ``dir-rw`` and ``keyring`` items are the
    stored logins), ``env`` the launch's environment after its allow-list.
    The plan's :attr:`~CredentialPlan.environment` is all the container
    receives besides the projection: the private home and stores and the
    CLI's integration isolation (``routes.isolation``), each set whatever
    ``env`` held, and of ``env`` the names :data:`PASSED_NAMES` and
    ``routes.settings`` list; every other variable of ``env`` is withheld. A
    refusal is a :class:`SandboxPolicyError` naming variables, kinds and the
    fix, never a value; so is a declaration that lists a credential as a
    setting, or a store or an isolation path outside the private home.
    """
    if launch_class is not LaunchClass.REMOTE_AGENT:
        raise _refuse(
            launch_class.value,
            "a repository command gets no model credential: only an agent launch's "
            "container gets a projection (ADR 0005 §2.5, #286)",
        )
    if routes is None:
        raise _refuse(
            provider,
            "no credential mapping for this provider under the container backend: #290 "
            "maps Claude Code, OpenCode 2.x and Pi; route the profile to one of them",
        )
    _check_declaration(provider, routes)
    stores = tuple(item for item in map(_not_projected, needs.auth) if item is not None)
    if routes.refusal:
        raise _refuse(provider, routes.refusal)
    for other in routes.unsupported:
        if _present(env, other.name):
            raise _refuse(
                other.name,
                f"a {other.kind.value} selects a route the container backend does not "
                f"take: {other.reason}; unset it",
            )
    present = [route for route in routes.routes if _present(env, route.name)]
    for route in present:
        if route.refresh is Refresh.ROTATING:
            raise _refuse(
                route.name,
                f"a {route.kind.value} rotates on use, and no controller-owned path returns "
                "the rotated login to the host, so the container's first refresh would "
                "invalidate the host's copy: it is never projected (ADR 0013 D14); unset it",
            )
    if len(present) > 1:
        names = " and ".join(f"{r.name} ({r.kind.value})" for r in present)
        raise _refuse(
            provider,
            f"{names} would both reach the launch, and the CLI's own precedence would "
            "choose between them: keep exactly one in the environment AutoForge runs in",
        )
    if not present:
        wanted = ", ".join(
            f"{r.name} ({r.kind.value})" for r in routes.routes if r.refresh is Refresh.NONE
        )
        detail = (
            f"no credential for {routes.scope} reaches the launch: set one of {wanted} in "
            "the environment AutoForge runs in (a name the provider's own allow-list does "
            "not pass is forwarded with execution.agent_env_allowlist_extra)"
            if wanted
            else f"no route of this profile delivers a credential for {routes.scope}"
        )
        if stores:
            detail += "; " + "; ".join(f"{item.label}: {item.reason}" for item in stores)
        raise _refuse(provider, detail)
    chosen = present[0]
    # Set whatever the launch's environment held: the private home, each
    # store in it and the CLI's integration isolation. Then the allow-listed
    # names the launch's environment holds, and nothing else of it.
    environment = {"HOME": HOME_TARGET}
    environment.update((store.name, store.value) for store in routes.stores)
    environment.update(routes.isolation)
    allowed = PASSED_NAMES.union(routes.settings)
    passed = {
        name: value
        for name, value in env.items()
        if value and name in allowed and name not in environment and name != chosen.name
    }
    environment.update(passed)
    withheld = tuple(
        sorted(
            name
            for name, value in env.items()
            if value and name != chosen.name and name not in passed
        )
    )
    return CredentialPlan(
        provider=provider,
        scope=routes.scope,
        name=chosen.name,
        kind=chosen.kind,
        refresh=chosen.refresh,
        # Every route is a variable, which both staging forms deliver (D14).
        mechanism=Mechanism.ENV,
        not_projected=stores,
        environment=tuple(sorted(environment.items())),
        withheld=withheld,
        projection=Projection(env=(EnvCredential(chosen.name, Secret(env[chosen.name])),)),
    )
