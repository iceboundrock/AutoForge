"""Unattended policy for the SDK providers (#149; ADR 0007 D2.7, D10.1, D10.2).

What a ``claude-sdk`` or ``opencode-sdk`` launch may load, which tools it
gets and which of their calls may ask a human, where its configuration and
session state live, how it authenticates, which variables of the
controller's environment it inherits, and the bounds every piece of
interaction data is held to before it is normalized, persisted or shown.
``docs/sdk-policy.md`` records each decision with its evidence and whether
it was verified, and on which runtime.

Provider layer, and pure: no process, no file, no network, no SDK import,
and no change to the controller's own environment (the launch allow-list
reads the *names* in it, never a value). The engine and the executor name
nothing of it. Its consumers:

- the SDK adapters in :mod:`autoforge.providers`, whose
  ``validate_profile`` (config load and every launch) builds the policy, so
  an unknown option, a value outside its closed set or an escape hatch is a
  :class:`~autoforge.errors.ConfigurationError` before anything starts;
- the helpers of #151 and #152, which receive :meth:`ClaudeSdkPolicy.start_fields`
  or :meth:`OpenCodeSdkPolicy.start_fields` as their ``start`` record's
  ``policy`` and the policy's environment over the launch's;
- the interaction broker of #134, which bounds and renders a provider's ask
  with the functions at the end of this module.

There is no pass-through: no SDK option, CLI flag, settings document,
hook, MCP server, plugin or environment variable reaches a helper unless a
field here names it, and no profile option carries free text into one.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .child_env import refuse_forbidden_env
from .errors import ConfigurationError, InteractionDataError
from .progress import clean
from .redaction import redact, redact_obj

CLAUDE_SDK = "claude-sdk"
OPENCODE_SDK = "opencode-sdk"
SDK_PROVIDERS = (CLAUDE_SDK, OPENCODE_SDK)

# The version of the flat policy a helper receives (``start_fields``). It
# moves whenever a field is added or changes meaning, together with the
# helper envelope's protocol (ADR 0007 D4.5).
POLICY_SCHEMA = 1

# The profiles whose agent writes the worktree; every other profile (the
# reviewers, ``update_epic``) gets the read inventory. The same split as
# Pi's (docs/pi-policy.md §6): the read set is defence in depth, never a
# write barrier, because the shell stays.
WRITE_PROFILES = ("analyze_execute", "fix", "replan_reexecute")

# -- interaction policy and limits (ADR 0007 D7.1, D7.3) ----------------------
INTERACTION_FAIL = "fail"
INTERACTION_PROMPT = "prompt"
INTERACTION_PAUSE = "pause"
INTERACTION_POLICIES = (INTERACTION_FAIL, INTERACTION_PROMPT, INTERACTION_PAUSE)
DEFAULT_MAX_WAIT_SECONDS = 120
DEFAULT_MAX_INTERACTIONS = 8
# Bounds on what an operator may configure: a wait is a person at a
# terminal, and the aggregate bound keeps an agent from turning a run into
# a conversation.
MAX_WAIT_SECONDS_LIMIT = 24 * 3600
MAX_INTERACTIONS_LIMIT = 64
INTERACTION_OPTION_KEYS = ("interaction_policy", "max_wait_seconds", "max_interactions")

_WHOLE_NUMBER = re.compile(r"[1-9][0-9]{0,8}")


@dataclass(frozen=True)
class InteractionSettings:
    """A profile's ``interaction_policy`` and its limits, validated.

    ``fail`` (the default) fails the invocation on any ask; ``prompt`` asks
    the operator live. ``pause`` is refused until #126 lands and the
    provider's pause gates pass (ADR 0007 D7.1, D9.4, D3.4).
    """

    policy: str = INTERACTION_FAIL
    max_wait_seconds: int = DEFAULT_MAX_WAIT_SECONDS
    max_interactions: int = DEFAULT_MAX_INTERACTIONS

    @property
    def live(self) -> bool:
        """Whether an ask reaches a person at all (only under ``prompt``)."""
        return self.policy == INTERACTION_PROMPT

    def as_dict(self) -> dict[str, object]:
        return {
            "policy": self.policy,
            "max_wait_seconds": self.max_wait_seconds,
            "max_interactions": self.max_interactions,
        }


def _whole_number(options: Mapping[str, str], key: str, default: int, high: int, where: str) -> int:
    raw = options.get(key)
    if raw is None:
        return default
    if not _WHOLE_NUMBER.fullmatch(raw) or int(raw) > high:
        raise ConfigurationError(
            f"{where}: option {key} must be a whole number from 1 to {high}, got {raw!r}"
        )
    return int(raw)


def interaction_settings(options: Mapping[str, str], where: str) -> InteractionSettings:
    """The interaction settings ``options`` name, or :class:`ConfigurationError`."""
    policy = options.get("interaction_policy", INTERACTION_FAIL)
    if policy not in INTERACTION_POLICIES:
        raise ConfigurationError(
            f"{where}: option interaction_policy must be one of "
            f"{', '.join(INTERACTION_POLICIES)}, got {policy!r}"
        )
    if policy == INTERACTION_PAUSE:
        raise ConfigurationError(
            f"{where}: interaction_policy pause is not available: a pause needs the durable "
            "pending-interaction record of #126 and the provider's pause gates (ADR 0007 "
            "D7.1: G-C6 and G-C7 for Claude; G-O6 and, for a permission, G-O11 for OpenCode), "
            "and none has landed. Use fail (the default) or prompt"
        )
    return InteractionSettings(
        policy=policy,
        max_wait_seconds=_whole_number(
            options, "max_wait_seconds", DEFAULT_MAX_WAIT_SECONDS, MAX_WAIT_SECONDS_LIMIT, where
        ),
        max_interactions=_whole_number(
            options, "max_interactions", DEFAULT_MAX_INTERACTIONS, MAX_INTERACTIONS_LIMIT, where
        ),
    )


def _tool_list(
    raw: str, known: Sequence[str], key: str, where: str, provider: str
) -> tuple[str, ...]:
    """``raw`` as a tuple of tools of ``known``: comma-separated, no spaces, no duplicates."""
    names = raw.split(",") if raw else []
    problem = ""
    if not raw or any(not name or name != name.strip() for name in names):
        problem = "expected names separated by ',' with no spaces and no empty entry"
    elif unknown := [name for name in names if name not in known]:
        problem = f"unknown or unsupported tool(s) {', '.join(unknown)}"
    elif len(set(names)) != len(names):
        problem = "a tool is listed twice"
    if problem:
        raise ConfigurationError(
            f"{where}: {provider} option {key} must list tools of {', '.join(known)}: "
            f"{problem}, got {raw!r}"
        )
    return tuple(names)


# -- environment (ADR 0007 D2.1, D10.1) ---------------------------------------
# The variables of other model providers and other agent CLIs, credentials
# and the settings that route them alike. An SDK launch inherits none of
# them, whatever the allow-list says: its own names are added back after
# the filter (:func:`sdk_launch_allowlist`).
FOREIGN_PREFIXES = (
    "ANTHROPIC_",
    "CLAUDE_",
    "OPENAI_",
    "OPENCODE_",
    "PI_",
    "GEMINI_",
    "GOOGLE_",
    "VERTEX_",
    "CLOUD_ML_",
    "AWS_",
    "AZURE_",
    "BEDROCK_",
    "OPENROUTER_",
    "MISTRAL_",
    "GROQ_",
    "XAI_",
    "DEEPSEEK_",
    "TOGETHER_",
    "FIREWORKS_",
    "CEREBRAS_",
    "COHERE_",
    "PERPLEXITY_",
    "HF_",
    "HUGGINGFACE_",
    "OLLAMA_",
)
# A name shaped like a credential, whoever it belongs to.
CREDENTIAL_SUFFIXES = (
    "_API_KEY",
    "_KEY",
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_PASSWD",
    "_CREDENTIALS",
)


def foreign_name(name: str) -> bool:
    """Whether variable ``name`` is withheld from every SDK launch (compared upper-cased)."""
    upper = name.upper()
    return upper.startswith(FOREIGN_PREFIXES) or upper.endswith(CREDENTIAL_SUFFIXES)


def sdk_launch_allowlist(
    base: Iterable[str],
    own: Iterable[str],
    environ_names: Iterable[str],
    *,
    passthrough: Iterable[str] = (),
) -> tuple[str, ...]:
    """The exact variable names an SDK launch inherits.

    ``base`` is the configured allow-list (``execution.env_allowlist`` plus
    ``env_allowlist_extra``); a ``*`` prefix in it is expanded against
    ``environ_names`` (the names in the controller's environment, never a
    value), so the result holds exact names only and no prefix can carry a
    credential through. Every :func:`foreign_name` is dropped, unless it is
    in ``passthrough`` and ``base`` lists it exactly; ``own`` (the
    provider's credential) is added last. A forbidden name (#165) is
    refused, as for every launch.
    """
    base = tuple(base)
    refuse_forbidden_env(base, "an SDK launch allow-list")
    allowed = set(passthrough)
    environ = list(dict.fromkeys(environ_names))
    names: list[str] = []
    for entry in base:
        if entry.endswith("*"):
            prefix = entry[:-1]
            names += [n for n in environ if n.startswith(prefix) and not foreign_name(n)]
        elif entry in allowed or not foreign_name(entry):
            names.append(entry)
    result = tuple(dict.fromkeys([*names, *own]))
    refuse_forbidden_env(result, "an SDK launch allow-list")
    return result


# -- configuration and session roots (ADR 0007 D2.7; ADR 0005 C5, D8.3, D8.4) --
# Under the issue's controller-owned sandbox directory
# (``<git common dir>/autoforge/sandbox/<worktree>`` in REMOTE,
# ``<git dir>/autoforge/sandbox/local`` in LOCAL; ``sandbox_policy``), so a
# root is never inside a working tree, the LOCAL fingerprinted tree
# included (its git directory is excluded by identity), is per issue, and
# persists for a continuation on the same directory (D8.4).
SDK_ROOT_DIR = "sdk"
# How many days a dedicated root keeps a session transcript (Claude's
# ``cleanupPeriodDays``); a continuation whose transcript is gone is
# ``BLOCKED``, never a new session under the recorded id (D2.7, D9.3).
SESSION_RETENTION_DAYS = 30


def sdk_root(sandbox_dir: Path, provider: str) -> Path:
    """The root of ``provider``'s state under an issue's sandbox directory."""
    if provider not in SDK_PROVIDERS:
        raise ConfigurationError(f"{provider!r} is not an SDK provider")
    if not sandbox_dir.is_absolute():
        raise ConfigurationError(f"an SDK root needs an absolute sandbox directory: {sandbox_dir}")
    return sandbox_dir / SDK_ROOT_DIR / provider


def root_inside(root: Path, tree: Path) -> bool:
    """Whether ``root`` is, or lies inside, ``tree`` (lexically, both absolute)."""
    return root == tree or tree in root.parents


@dataclass(frozen=True)
class ClaudeSdkRoots:
    """Where a ``claude-sdk`` launch keeps its state: one directory.

    ``config`` is ``CLAUDE_CONFIG_DIR``: the CLI's global configuration
    (``.claude.json``), its backups and the session transcripts
    (``projects/<cwd>/<session id>.jsonl``), so it is the session root of
    ADR 0007 D2.7. AutoForge writes nothing there and copies no credential
    into it; ``setting_sources=[]`` keeps it from reading settings, agents,
    skills or ``CLAUDE.md`` there (docs/sdk-policy.md §2).
    """

    config: Path

    @classmethod
    def under(cls, sandbox_dir: Path) -> ClaudeSdkRoots:
        return cls(config=sdk_root(sandbox_dir, CLAUDE_SDK) / "config")

    def writable(self) -> tuple[Path, ...]:
        return (self.config,)


@dataclass(frozen=True)
class OpenCodeSdkRoots:
    """Where an ``opencode-sdk`` launch keeps its state.

    The server and every tool it starts get ``HOME`` and the four XDG base
    directories here, so no global configuration, plugin, package cache,
    credential store or session database of the operator's is read, and
    what the server writes (its database and session history, logs, caches)
    stays in the issue's root. ``empty_config`` is ``OPENCODE_CONFIG_DIR``:
    an empty directory projected read-only, so no agent, command or plugin
    directory is read from it.
    """

    home: Path
    config: Path
    data: Path
    cache: Path
    state: Path
    empty_config: Path

    @classmethod
    def under(cls, sandbox_dir: Path) -> OpenCodeSdkRoots:
        base = sdk_root(sandbox_dir, OPENCODE_SDK)
        return cls(
            home=base / "home",
            config=base / "config",
            data=base / "data",
            cache=base / "cache",
            state=base / "state",
            empty_config=base / "empty-config",
        )

    def writable(self) -> tuple[Path, ...]:
        return (self.home, self.config, self.data, self.cache, self.state)


def _digest(document: object) -> str:
    return hashlib.sha256(canonical_json(document)).hexdigest()


# -- Claude Agent SDK (ADR 0007 D1.1, D9.2, D10.1, D10.2) ---------------------
# The built-in tools a profile may list. Anything else is refused: the
# subagent tools (attribution is gate G-C8), skills, slash commands, plan
# mode, every MCP tool, whose servers are never configured, and
# ``TodoWrite``, which CLI 2.1.304 no longer offers (docs/sdk-policy.md §3).
CLAUDE_SDK_TOOLS = (
    "Read",
    "Glob",
    "Grep",
    "Edit",
    "Write",
    "NotebookEdit",
    "Bash",
    "WebFetch",
    "WebSearch",
)
CLAUDE_SDK_WRITE_TOOLS = ("Read", "Glob", "Grep", "Edit", "Write", "Bash")
CLAUDE_SDK_READ_TOOLS = ("Read", "Glob", "Grep", "Bash")
# Offered only under ``interaction_policy: prompt``: the CLI offers it only
# with a permission host (ADR 0007 D9.2), and under ``fail`` an ask could
# only fail the invocation.
CLAUDE_SDK_QUESTION_TOOL = "AskUserQuestion"
# Named in ``disallowed_tools`` whatever the inventory, as defence in depth
# beside ``tools``: the subagent tool under both of its names, skills,
# slash commands and plan mode.
CLAUDE_SDK_DENIED_TOOLS = ("Agent", "Task", "Skill", "SlashCommand", "ExitPlanMode")
# The permission mode whose calls outside ``allowed_tools`` reach the
# permission host (``can_use_tool``), so the PreToolUse hook and the host
# both run. CLI 2.1.304 calls it ``manual`` in its help, accepts ``default``
# too and reports ``default`` in ``system/init`` for both; never a bypass,
# auto-approval, plan or deny-everything mode, whose callbacks would not run.
CLAUDE_SDK_PERMISSION_MODE = "default"
# The only credential (D10.2): an API key, never a claude.ai login.
CLAUDE_SDK_CREDENTIAL = "ANTHROPIC_API_KEY"
# What ``system/init`` must report as the key's source once the CLI runs.
CLAUDE_SDK_KEY_SOURCE = "ANTHROPIC_API_KEY"
# A variable the operator may forward by listing it exactly: where the API
# is (a gateway, or the stand-in endpoint of the gates' probes).
CLAUDE_SDK_PASSTHROUGH = ("ANTHROPIC_BASE_URL",)
# Never in a claude-sdk launch: a subscription login's token, a gateway
# bearer token and the cloud routes, which are a separately tested
# extension. :func:`sdk_launch_allowlist` already withholds them; the auth
# check refuses them again by name.
CLAUDE_SDK_REFUSED_AUTH = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
# Set over the launch's environment besides ``CLAUDE_CONFIG_DIR``: no
# auto-updater, telemetry or error reporting.
CLAUDE_SDK_FIXED_ENV = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
# The one settings document the CLI gets (the SDK's ``settings``, which
# becomes ``--settings``): no auto memory, and the retention of the session
# root. No hook, permission rule, ``env``, ``apiKeyHelper`` or MCP entry.
CLAUDE_SDK_SETTINGS: dict[str, object] = {
    "autoMemoryEnabled": False,
    "cleanupPeriodDays": SESSION_RETENTION_DAYS,
}
# Claude Code's own system prompt, with nothing appended: repository
# instructions are task data the phase prompt points to, read from the
# worktree, never injected (docs/sdk-policy.md §4).
CLAUDE_SDK_SYSTEM_PROMPT: dict[str, str] = {"type": "preset", "preset": "claude_code"}
# The permission host's answer to every call it is asked about: the
# inventory is pre-approved and the asks go through the hook, so a call
# that still reaches the host is one the policy did not foresee.
CLAUDE_SDK_HOST_DENY = (
    "AutoForge's unattended policy denies this call: it is not one this profile "
    "approves or asks a person about (docs/sdk-policy.md)"
)
CLAUDE_SDK_OPTION_KEYS = ("tools", "ask_tools", *INTERACTION_OPTION_KEYS)

# What the PreToolUse hook does with a call (ADR 0007 D9.2).
PASS = "pass"  # decides nothing: ``allowed_tools`` approves the call
ASK = "ask"  # asks the broker and maps its decision or directive
DENY = "deny"  # denies with :data:`CLAUDE_SDK_HOST_DENY`


@dataclass(frozen=True)
class ClaudeSdkPolicy:
    """What one ``claude-sdk`` profile's launches get (docs/sdk-policy.md §2-6)."""

    profile: str
    tools: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    ask_tools: tuple[str, ...]
    disallowed_tools: tuple[str, ...]
    interaction: InteractionSettings
    permission_mode: str = CLAUDE_SDK_PERMISSION_MODE

    def disposition(self, tool_name: str) -> str:
        """What the PreToolUse hook does with a call of ``tool_name``.

        Every call goes through it, so a tool the runtime offers that the
        policy did not list (an MCP tool, a renamed or new built-in) is
        denied before any callback could approve it.
        """
        if tool_name in self.ask_tools:
            return ASK
        if tool_name in self.allowed_tools:
            return PASS
        return DENY

    def settings_document(self) -> str:
        """The ``settings`` JSON, fixed by the policy."""
        return json.dumps(CLAUDE_SDK_SETTINGS, sort_keys=True, separators=(",", ":"))

    def start_fields(self) -> dict[str, object]:
        """The ``policy`` of the helper's ``start`` record: every SDK option it
        sets, as flat values, and nothing else (ADR 0007 §5)."""
        return {
            "schema": POLICY_SCHEMA,
            "provider": CLAUDE_SDK,
            "tools": list(self.tools),
            "allowed_tools": list(self.allowed_tools),
            "disallowed_tools": list(self.disallowed_tools),
            "ask_tools": list(self.ask_tools),
            "permission_mode": self.permission_mode,
            "setting_sources": [],
            "strict_mcp_config": True,
            "mcp_servers": {},
            "plugins": [],
            "agents": {},
            "extra_args": {},
            "settings": self.settings_document(),
            "system_prompt": dict(CLAUDE_SDK_SYSTEM_PROMPT),
            "can_use_tool": DENY,
            "can_use_tool_message": CLAUDE_SDK_HOST_DENY,
            "hooks": ["PreToolUse"],
            # No approval outlives its call: the hook answers allow or deny,
            # never ``updated_permissions``, so no setting is written.
            "session_approval": False,
            "interaction": self.interaction.as_dict(),
        }

    def environment(self, roots: ClaudeSdkRoots) -> dict[str, str]:
        """What the launch sets over its allow-listed environment."""
        return {"CLAUDE_CONFIG_DIR": str(roots.config), **CLAUDE_SDK_FIXED_ENV}

    def digest(self) -> str:
        """The policy digest of ADR 0007 D5.1's ``SessionIdentity``: the fields
        and the fixed environment, not the per-issue roots (the identity
        records the session root on its own)."""
        return _digest({"start": self.start_fields(), "env": CLAUDE_SDK_FIXED_ENV})

    def init_problems(self, init: Mapping[str, object]) -> list[str]:
        """What a ``system/init`` record shows the runtime loaded or chose
        against this policy; empty when it matches.

        The pair is pinned (ADR 0007 D4.1), so a difference is a provider
        failure, never something to adapt to: a tool offered or missing, an
        MCP server, a plugin that is not built in, a permission mode other
        than :data:`CLAUDE_SDK_PERMISSION_MODE`, a key from anywhere but
        ``ANTHROPIC_API_KEY``, or auto memory. Names only, never a value.
        """
        problems: list[str] = []
        tools = init.get("tools")
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            problems.append("its tools are not a list of names")
        else:
            offered = set(tools)
            expected = set(self.tools)
            if extra := sorted(offered - expected):
                problems.append(f"it offers tools the policy does not list: {', '.join(extra)}")
            if missing := sorted(expected - offered):
                problems.append(f"it does not offer {', '.join(missing)}")
        if init.get("mcp_servers") not in ([], None):
            problems.append("it started MCP servers")
        plugins = init.get("plugins") or []
        if not isinstance(plugins, list) or any(
            not isinstance(p, dict) or p.get("path") != "builtin" for p in plugins
        ):
            problems.append("it loaded a plugin that is not built in")
        if init.get("permissionMode") != self.permission_mode:
            problems.append(f"its permission mode is not {self.permission_mode}")
        if init.get("apiKeySource") != CLAUDE_SDK_KEY_SOURCE:
            problems.append(f"its credential does not come from {CLAUDE_SDK_KEY_SOURCE}")
        if init.get("memory_paths"):
            problems.append("auto memory is on")
        return [clean(p, 300) for p in problems]


def claude_sdk_policy(profile_name: str, options: Mapping[str, str]) -> ClaudeSdkPolicy:
    """The policy of a ``claude-sdk`` profile, or :class:`ConfigurationError`."""
    where = f"profile {profile_name!r}"
    unknown = sorted(set(options) - set(CLAUDE_SDK_OPTION_KEYS))
    if unknown:
        raise ConfigurationError(
            f"{where}: unknown {CLAUDE_SDK} option(s) {', '.join(unknown)} "
            f"(accepted: {', '.join(CLAUDE_SDK_OPTION_KEYS)})"
        )
    interaction = interaction_settings(options, where)
    if "tools" in options:
        tools = _tool_list(options["tools"], CLAUDE_SDK_TOOLS, "tools", where, CLAUDE_SDK)
    else:
        tools = CLAUDE_SDK_WRITE_TOOLS if profile_name in WRITE_PROFILES else CLAUDE_SDK_READ_TOOLS
    ask: tuple[str, ...] = ()
    if "ask_tools" in options:
        ask = _tool_list(options["ask_tools"], tools, "ask_tools", where, CLAUDE_SDK)
        if not interaction.live:
            raise ConfigurationError(
                f"{where}: {CLAUDE_SDK} option ask_tools needs interaction_policy prompt: "
                "under fail every ask fails the invocation"
            )
    if interaction.live:
        ask = (*ask, CLAUDE_SDK_QUESTION_TOOL)
        tools = (*tools, CLAUDE_SDK_QUESTION_TOOL)
    allowed = tuple(t for t in tools if t not in ask)
    unoffered = [t for t in (*CLAUDE_SDK_TOOLS, CLAUDE_SDK_QUESTION_TOOL) if t not in tools]
    denied = tuple(dict.fromkeys([*CLAUDE_SDK_DENIED_TOOLS, *unoffered]))
    return ClaudeSdkPolicy(
        profile=profile_name,
        tools=tools,
        allowed_tools=allowed,
        ask_tools=ask,
        disallowed_tools=denied,
        interaction=interaction,
    )


def claude_sdk_auth_problems(env: Mapping[str, str]) -> list[str]:
    """Why a ``claude-sdk`` launch with environment ``env`` would not
    authenticate with an API key alone; empty when it would (D10.2).

    Names only: a value is never read past whether it is set.
    """
    problems: list[str] = []
    if not env.get(CLAUDE_SDK_CREDENTIAL):
        problems.append(
            f"{CLAUDE_SDK_CREDENTIAL} is not set: {CLAUDE_SDK} authenticates with an Anthropic "
            "API key only, and never falls back to a claude.ai login, CLAUDE_CODE_OAUTH_TOKEN, "
            "a `claude setup-token` token or a keychain. Set it in the environment autoforge "
            "runs in"
        )
    refused = sorted(name for name in CLAUDE_SDK_REFUSED_AUTH if env.get(name))
    if refused:
        problems.append(
            f"{', '.join(refused)} would reach the launch: a subscription login, a bearer "
            f"token or a cloud route is not a {CLAUDE_SDK} credential"
        )
    return problems


# -- OpenCode SDK and runtime (ADR 0007 D2.5, D3.1, D3.4, D4.3, D10.1, D10.2) --
# The tools a profile may list, by their OpenCode permission names.
OPENCODE_SDK_TOOLS = (
    "read",
    "glob",
    "grep",
    "list",
    "edit",
    "bash",
    "todowrite",
    "todoread",
    "webfetch",
    "websearch",
)
OPENCODE_SDK_WRITE_TOOLS = ("read", "glob", "grep", "list", "edit", "bash")
OPENCODE_SDK_READ_TOOLS = ("read", "glob", "grep", "list", "bash")
OPENCODE_SDK_QUESTION = "question"
# Denied whatever the inventory: subagents (attribution is gate G-O9),
# skills, code search, language-server queries, and the repeated-call ask,
# which nobody could answer (the controller's loop detector covers it).
OPENCODE_SDK_DENIED = ("task", "skill", "codesearch", "lsp", "doom_loop")
# The agent every session runs: the controller's own, with a complete
# ruleset, never a built-in agent patched (a built-in agent carries rules of
# its own; seen on 2.0.32: ``build`` allows ``*``).
OPENCODE_SDK_AGENT = "autoforge"
OPENCODE_SDK_HOSTNAME = "127.0.0.1"
OPENCODE_SDK_PASSWORD_ENV = "OPENCODE_SERVER_PASSWORD"
OPENCODE_SDK_USERNAME = "opencode"
# At least 128 bits from the operating system's random source (D3.4).
OPENCODE_SDK_PASSWORD_BYTES = 32
# Set over the launch's environment besides the roots and the document.
# Unknown names are inert, so the names of both release lines are set
# (docs/sdk-policy.md §7 says which line reads which).
OPENCODE_SDK_FIXED_ENV = {
    "OPENCODE_CONFIG": "",
    "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
    "OPENCODE_CONFIG_PROJECT_DISABLE": "1",
    "OPENCODE_DISABLE_AUTOUPDATE": "1",
    "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
    "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
}
OPENCODE_SDK_OPTION_KEYS = (
    "server_command",
    "credential_env",
    "tools",
    *INTERACTION_OPTION_KEYS,
)
# A provider API key's name: the one variable the server may read for its
# model credential (no store of ``opencode auth login`` is used).
_CREDENTIAL_ENV_RE = re.compile(r"[A-Z][A-Z0-9_]{0,55}_API_KEY")
_SERVER_COMMAND_RE = re.compile(r"[^\x00-\x20\x7f]{1,4096}")
_OPENCODE_MODEL_RE = re.compile(r"[a-z0-9][a-z0-9._-]*/[^\s#]+")


@dataclass(frozen=True)
class OpenCodeSdkPolicy:
    """What one ``opencode-sdk`` profile's launches get (docs/sdk-policy.md §7-9)."""

    profile: str
    tools: tuple[str, ...]
    credential_env: str
    server_command: str
    interaction: InteractionSettings

    def ruleset(self) -> dict[str, str]:
        """The permission rules, in order: everything denied, then the
        inventory allowed, then the fixed denials, which overlap no allowed
        name, so first-match and last-match evaluation agree.

        ``external_directory`` is allowed: the REMOTE read context lies
        outside the worktree. The boundary for paths is the outer sandbox
        (ADR 0005), never this ruleset (ADR 0007 D3.4, D10.1).
        """
        rules = {"*": "deny"}
        rules.update({tool: "allow" for tool in self.tools})
        rules["external_directory"] = "allow"
        rules[OPENCODE_SDK_QUESTION] = "allow" if self.interaction.live else "deny"
        rules.update({name: "deny" for name in OPENCODE_SDK_DENIED})
        return rules

    def config_document(self) -> dict[str, object]:
        """The inline configuration (``OPENCODE_CONFIG_CONTENT``), the only
        configuration the server reads besides the empty directory: no
        plugin, MCP server, instruction file, language server or formatter,
        no sharing or self-update, and the controller's agent."""
        rules = self.ruleset()
        return {
            "$schema": "https://opencode.ai/config.json",
            "autoupdate": False,
            "share": "disabled",
            "plugin": [],
            "mcp": {},
            "instructions": [],
            "lsp": False,
            "formatter": False,
            "permission": dict(rules),
            "agent": {
                OPENCODE_SDK_AGENT: {
                    "mode": "primary",
                    "description": "AutoForge's unattended agent (docs/sdk-policy.md)",
                    "permission": dict(rules),
                }
            },
        }

    def environment(self, roots: OpenCodeSdkRoots) -> dict[str, str]:
        """What the bridge and its server get over the allow-listed
        environment: the roots, the inline document and the fixed switches.
        The server password is the bridge's, per launch (D3.4), and is not
        part of the policy."""
        return {
            "HOME": str(roots.home),
            "XDG_CONFIG_HOME": str(roots.config),
            "XDG_DATA_HOME": str(roots.data),
            "XDG_CACHE_HOME": str(roots.cache),
            "XDG_STATE_HOME": str(roots.state),
            "OPENCODE_CONFIG_DIR": str(roots.empty_config),
            "OPENCODE_CONFIG_CONTENT": json.dumps(
                self.config_document(), sort_keys=True, separators=(",", ":")
            ),
            **OPENCODE_SDK_FIXED_ENV,
        }

    def server_argv(self, server_command: str) -> list[str]:
        """The server's argv; ``server_command`` is the resolved absolute
        path of :attr:`server_command` (D2.5). Loopback, an assigned port, no
        mDNS and no CORS origin; never the password."""
        return [server_command, "serve", f"--hostname={OPENCODE_SDK_HOSTNAME}", "--port=0"]

    def start_fields(self) -> dict[str, object]:
        """The ``policy`` of the bridge's ``start`` record."""
        return {
            "schema": POLICY_SCHEMA,
            "provider": OPENCODE_SDK,
            "agent": OPENCODE_SDK_AGENT,
            "tools": list(self.tools),
            "ruleset": self.ruleset(),
            "credential_env": self.credential_env,
            # Never ``always``, which approves a pattern for the rest of the
            # session: the server's rules stay the document's.
            "replies": ["once", "reject"],
            "session_approval": False,
            "interaction": self.interaction.as_dict(),
        }

    def digest(self) -> str:
        """The policy digest of ``SessionIdentity`` (ADR 0007 D5.1)."""
        return _digest(
            {
                "start": self.start_fields(),
                "document": self.config_document(),
                "env": OPENCODE_SDK_FIXED_ENV,
            }
        )


def opencode_sdk_policy(profile_name: str, options: Mapping[str, str]) -> OpenCodeSdkPolicy:
    """The policy of an ``opencode-sdk`` profile, or :class:`ConfigurationError`."""
    where = f"profile {profile_name!r}"
    unknown = sorted(set(options) - set(OPENCODE_SDK_OPTION_KEYS))
    if unknown:
        raise ConfigurationError(
            f"{where}: unknown {OPENCODE_SDK} option(s) {', '.join(unknown)} "
            f"(accepted: {', '.join(OPENCODE_SDK_OPTION_KEYS)})"
        )
    interaction = interaction_settings(options, where)
    server_command = options.get("server_command", "")
    if not _SERVER_COMMAND_RE.fullmatch(server_command):
        raise ConfigurationError(
            f"{where}: {OPENCODE_SDK} needs option server_command, the OpenCode runtime "
            "binary of the pinned release (a command name or an absolute path, with no "
            f"whitespace or control character; ADR 0007 D2.5), got {server_command!r}"
        )
    credential = options.get("credential_env", "")
    if not _CREDENTIAL_ENV_RE.fullmatch(credential) or (
        credential.startswith("OPENCODE_") and credential != "OPENCODE_API_KEY"
    ):
        raise ConfigurationError(
            f"{where}: {OPENCODE_SDK} needs option credential_env, the one variable that "
            "holds the model provider's API key (for example OPENAI_API_KEY): a name ending "
            "in _API_KEY. A sign-in stored by `opencode auth login` is not used "
            f"(docs/sdk-policy.md §9), got {credential!r}"
        )
    refuse_forbidden_env((credential,), f"{where}: {OPENCODE_SDK} option credential_env")
    if "tools" in options:
        tools = _tool_list(options["tools"], OPENCODE_SDK_TOOLS, "tools", where, OPENCODE_SDK)
    else:
        tools = (
            OPENCODE_SDK_WRITE_TOOLS if profile_name in WRITE_PROFILES else OPENCODE_SDK_READ_TOOLS
        )
    return OpenCodeSdkPolicy(
        profile=profile_name,
        tools=tools,
        credential_env=credential,
        server_command=server_command,
        interaction=interaction,
    )


def opencode_model_problem(model: str) -> str:
    """Why ``model`` is not an OpenCode ``provider/model`` id, or ''.

    The reasoning variant is the bridge's to map (#152), so a ``#`` suffix
    is refused here rather than passed as part of the id.
    """
    if not _OPENCODE_MODEL_RE.fullmatch(model):
        return "expected '<provider>/<model>' with no whitespace and no '#'"
    return ""


def opencode_sdk_auth_problems(policy: OpenCodeSdkPolicy, env: Mapping[str, str]) -> list[str]:
    """Why an ``opencode-sdk`` launch with ``env`` would have no model
    credential; names only (D10.2)."""
    if env.get(policy.credential_env):
        return []
    return [
        f"{policy.credential_env} (option credential_env) is not set in the environment "
        f"autoforge runs in: {OPENCODE_SDK} reads its model credential from that variable "
        "only, never from a stored sign-in"
    ]


def server_password() -> str:
    """A fresh server password for one launch (D3.4): never in an argv, a
    record, a log or state."""
    return secrets.token_urlsafe(OPENCODE_SDK_PASSWORD_BYTES)


# -- interaction data (ADR 0007 D5.1; docs/sdk-policy.md §10) ------------------
# Bounds on what a provider's ask may carry. A field that decides what a
# tool may do or what the agent is told is refused past its bound, never
# clipped: approving a clipped input would approve different work.
MAX_TOOL_NAME_CHARS = 128
MAX_INPUT_BYTES = 64 * 1024
MAX_QUESTIONS = 4
MAX_OPTIONS = 16
MAX_QUESTION_CHARS = 2000
MAX_OPTION_CHARS = 200
MAX_ANSWER_CHARS = 2000
# What an operator is shown of one value: one line, cleaned and redacted.
MAX_SHOWN_CHARS = 160
# The input keys a permission ask shows per tool: a path, a pattern, a
# query, a command and its description. Never a file's content, a diff, an
# edit's old or new text, a notebook cell or a web page.
DISPLAY_KEYS: dict[str, tuple[str, ...]] = {
    "Bash": ("command", "description"),
    "Read": ("file_path",),
    "Glob": ("pattern", "path"),
    "Grep": ("pattern", "path", "glob"),
    "Edit": ("file_path",),
    "Write": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "WebFetch": ("url",),
    "WebSearch": ("query",),
    "bash": ("command", "description"),
    "read": ("filePath",),
    "edit": ("filePath",),
    "write": ("filePath",),
    "glob": ("pattern", "path"),
    "grep": ("pattern", "path"),
    "list": ("path",),
    "webfetch": ("url",),
    "websearch": ("query",),
}


def canonical_json(value: object) -> bytes:
    """``value`` as canonical JSON: sorted keys, no spaces, UTF-8, no NaN.

    A value JSON cannot represent raises :class:`InteractionDataError`.
    """
    try:
        text = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise InteractionDataError(f"a value is not JSON ({type(exc).__name__})") from exc
    return text.encode("utf-8")


def input_digest(value: object, what: str, limit: int = MAX_INPUT_BYTES) -> str:
    """The SHA-256 of ``value``'s canonical JSON, the identity a decision is
    bound to (ADR 0007 D9.3); refused past ``limit`` bytes, never clipped."""
    data = canonical_json(value)
    if len(data) > limit:
        raise InteractionDataError(
            f"{what} is {len(data)} bytes of JSON, more than {limit}: refused rather than "
            "shown in part and approved whole"
        )
    return hashlib.sha256(data).hexdigest()


def _unsafe_character(text: str) -> bool:
    """Whether ``text`` holds a control, format or separator character (an
    escape sequence starts with one) other than a newline or a tab."""
    for ch in text:
        if ch in "\n\t":
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Zl", "Zp"):
            return True
    return False


def exact_text(value: object, what: str, limit: int) -> str:
    """``value`` as a string the controller may use as an identity or send back
    verbatim (an option a person picked, an answer, a tool name), or
    :class:`InteractionDataError`.

    Refused, never adjusted: a non-string, past ``limit`` characters, a
    control, format or separator character other than a newline or a tab
    (terminal injection), or anything :func:`autoforge.redaction.redact`
    would change, since its shown form would then differ from what is sent.
    """
    if not isinstance(value, str):
        raise InteractionDataError(f"{what} is not a string")
    if len(value) > limit:
        raise InteractionDataError(f"{what} is longer than {limit} characters")
    if _unsafe_character(value):
        raise InteractionDataError(f"{what} carries a control or escape character")
    if redact(value) != value:
        raise InteractionDataError(f"{what} carries a credential-shaped value")
    return value


def exact_input(value: object, what: str, limit: int = MAX_INPUT_BYTES) -> object:
    """A modified tool input the controller may send back, unchanged, or
    :class:`InteractionDataError`: JSON within ``limit`` bytes, and every
    string in it safe for :func:`exact_text` (redaction would change none)."""
    input_digest(value, what, limit)
    if redact_obj(value) != value:
        raise InteractionDataError(f"{what} carries a credential-shaped value")
    for text in _strings(value):
        if _unsafe_character(text.replace("\r", "")):
            raise InteractionDataError(f"{what} carries a control or escape character")
    return value


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def shown(value: object, limit: int = MAX_SHOWN_CHARS) -> str:
    """``value`` as one line an operator may be shown: escape sequences and
    control characters removed, redacted, then clipped. For display only:
    never an identity, never sent back."""
    return clean(value, limit)


@dataclass(frozen=True)
class ShownOption:
    """An option of a question: the raw label (the identity a reply carries)
    apart from what the operator is shown."""

    raw: str
    shown: str


def question_options(labels: object, what: str) -> tuple[ShownOption, ...]:
    """A question's options, each kept raw and shown apart; or
    :class:`InteractionDataError`.

    More than :data:`MAX_OPTIONS`, a label :func:`exact_text` refuses, or
    two labels shown alike (a person could not tell which one they pick) is
    refused.
    """
    if not isinstance(labels, list) or not labels:
        raise InteractionDataError(f"{what} has no list of options")
    if len(labels) > MAX_OPTIONS:
        raise InteractionDataError(f"{what} has more than {MAX_OPTIONS} options")
    options = tuple(
        ShownOption(raw=raw, shown=shown(raw, MAX_OPTION_CHARS))
        for raw in (
            exact_text(label, f"{what} option {n}", MAX_OPTION_CHARS)
            for n, label in enumerate(labels, 1)
        )
    )
    if len({option.shown.casefold() for option in options}) != len(options):
        raise InteractionDataError(f"{what} has two options shown alike")
    return options


def permission_summary(tool_name: str, tool_input: Mapping[str, object]) -> str:
    """What a permission ask shows of its input: the allow-listed keys of
    :data:`DISPLAY_KEYS` for ``tool_name``, each cleaned, redacted and
    bounded. Every other key is counted, never shown."""
    keys = DISPLAY_KEYS.get(tool_name, ())
    parts = [f"{key}: {shown(tool_input[key])}" for key in keys if key in tool_input]
    hidden = len([key for key in tool_input if key not in keys])
    if hidden:
        parts.append(f"(+{hidden} field(s) not shown)")
    return shown("; ".join(parts), 3 * MAX_SHOWN_CHARS)
