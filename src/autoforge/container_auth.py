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
  §2.5, #286): :func:`plan_credentials` refuses its launch class.
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
- **Scoped, never wholesale.** Only the selected variable is projected.
  Every other credential-looking variable of the launch's environment is
  :attr:`CredentialPlan.withheld`: the container backend (#271) passes none
  of them into the container.

Every supported route is a static value (an API key, or Claude Code's
long-lived ``claude setup-token`` token), so every route maps to the
environment variable, which both staging forms deliver: no provider maps to
the file mechanism, and a copy-in runtime loses no route. Nothing is
refreshed inside a container, so concurrent workers share no writable
credential state, and a relaunch gets a new projection from the host's
environment as it is then.

Pure: no process, no file read, no state. The projection's value is a
:class:`~autoforge.sandbox_container.Secret`; :meth:`CredentialPlan.facts`
and :meth:`CredentialPlan.describe` carry names, kinds and capability only.
Not reachable before #271 builds the container backend and #273 makes it
selectable.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field

from .child_env import credential_looking_word
from .errors import SandboxPolicyError
from .sandbox_container import EnvCredential, Projection, Secret
from .sandbox_policy import AuthItem, AuthMode, LaunchClass, ProviderRequirements

_RULE = "D14 provider credential mapping (#290)"


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
    # operator rotates it on the host, and the next launch's projection
    # carries the new value.
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


@dataclass(frozen=True)
class CredentialRoutes:
    """A provider adapter's declaration for one profile: names and kinds, no value.

    ``scope`` is the model provider the launch authenticates to (``anthropic``,
    ``openai``, ``deepseek``); ``routes`` the variables the profile may take
    its credential from; ``unsupported`` the variables that select a route
    the container backend refuses; ``refusal`` why no route of this profile
    can be projected at all ("" for none), with what the operator can do.
    """

    scope: str
    routes: tuple[EnvRoute, ...] = ()
    unsupported: tuple[UnsupportedRoute, ...] = ()
    refusal: str = ""


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
    declared logins no projection carries, ``withheld`` the credential-looking
    variables of the launch's environment the container must not receive.
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
    withheld: tuple[str, ...] = ()
    projection: Projection = field(default_factory=Projection, repr=False, compare=False)

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
        if self.withheld:
            text += "; withheld: " + ", ".join(self.withheld)
        return text


def _refuse(subject: str, detail: str) -> SandboxPolicyError:
    return SandboxPolicyError(subject, _RULE, detail)


def _present(env: Mapping[str, str], name: str) -> bool:
    return bool(env.get(name))


def credential_looking(name: str) -> bool:
    """Whether variable ``name`` looks like it holds a credential (#286's words, or a key)."""
    upper = name.upper()
    return upper.endswith("_API_KEY") or bool(credential_looking_word(upper))


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
    stored logins), ``env`` the launch's environment after its allow-list. A
    refusal is a :class:`SandboxPolicyError` naming variables, kinds and the
    fix, never a value.
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
    withheld = tuple(
        sorted(
            name for name in env if name != chosen.name and env[name] and credential_looking(name)
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
        withheld=withheld,
        projection=Projection(env=(EnvCredential(chosen.name, Secret(env[chosen.name])),)),
    )
