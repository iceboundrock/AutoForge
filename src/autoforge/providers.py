"""Agent provider adapters: the only place that knows real CLI flags.

The engine speaks :class:`AgentRequest` / :class:`AgentExecutionResult`;
each provider translates a logical execution profile (provider, model,
effort, timeout, options) into a concrete non-interactive CLI invocation.

Verified against locally installed CLIs (2026-09):

Claude Code (``claude 2.1.x``)::

    claude -p --output-format text --model <alias|id> --effort <low|medium|high|xhigh|max>
           --permission-mode <mode> --no-session-persistence [extra_args] -- <prompt>

  * ``-p/--print`` = non-interactive; final assistant text goes to stdout
    verbatim (so the CONTROL_RESULT block survives untouched).
  * ``--permission-mode bypassPermissions`` is required for unattended
    edits / git / gh calls; ``--permission-prompts none`` would silently
    deny anything that prompts. Configure via profile ``options``.
  * exit code 0 on success; 1 on model/auth errors (message on stdout).

OpenCode (``opencode 1.18.x``)::

    opencode run -m <provider/model> --variant <effort> --format default [--auto]
                 [extra_args] -- <message>

  * model ids are ``provider/model`` (``openai/gpt-5.6-luna``).
  * ``--`` ends option parsing (yargs), so a message that starts with ``-``
    is the message and not a flag (checked against 1.18.31: after ``--``,
    ``--help`` is sent as the message rather than printing the help).
  * ``--variant`` carries the provider-specific reasoning effort.
  * ``--format default`` prints only the final assistant text to stdout
    (tool traces go to stderr); ``--format json`` would emit an event
    stream where the CONTROL_RESULT is JSON-escaped, so it is NOT used.
  * bash/edit tools run without ``--auto`` under the default agent;
    ``--auto`` is opt-in via ``options.auto_approve: true``.
  * exit code 0 on success; 1 on unknown model / server error.

Pi (``pi 1.0.x``, ADR 0003; read from the v1.0.0 source and docs, not yet
run against an installed Pi)::

    pi --mode rpc --no-session --model <pi-provider>/<model-id> --thinking <effort>

  * the prompt never travels in argv: it is one JSON ``prompt`` record on
    stdin. Running that RPC conversation is #131; until it lands
    :meth:`PiProvider.execute` refuses with a typed error, and a dry run
    renders the argv above.
  * ``--thinking`` takes ``off|minimal|low|medium|high|xhigh|max``; the model
    is ``provider/id`` with no ``:<thinking>`` suffix (ADR 0003 §2.2, §2.3).
  * no ``extra_args``: every flag that matters is the adapter's or #132's
    policy's, and a pass-through could undo it (``--approve``,
    ``--api-key``, ``--session-id``, ``--system-prompt``, ``--mode``, ...).
  * ``pi --version`` prints the bare version; ``pi auth check --model <m>
    --json --no-refresh`` reports, read-only, whether Pi holds a credential
    for the model's provider (``autoforge doctor``).

The CLIs are launched with cwd = the directory the engine chose (a
per-issue worktree for a REMOTE run, the contract's repository root for a
LOCAL one), stdin = /dev/null, a hard timeout, and an allow-listed
environment (see executor.py): the engine passes the configured allow-list
in :attr:`AgentRequest.env_allowlist` and each adapter adds the variables
its own CLI reads (:attr:`AgentProvider.environment_names`), so the key of
one provider is never handed to another.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from .config import ProfileConfig
from .errors import ConfigurationError, ExecutionError
from .executor import ExecutionRequest, ExecutionResult, describe_leftovers, execute

Runner = Callable[[ExecutionRequest], ExecutionResult]


@dataclass
class AgentRequest:
    phase: str
    prompt: str
    cwd: str
    profile: ProfileConfig
    timeout_seconds: int
    attempt: int = 1
    correction: bool = False
    # The controller's environment allow-list (``execution.env_allowlist``
    # plus ``env_allowlist_extra``); the provider adds its own names. ``None``
    # inherits the whole environment and is never what the engine sends.
    env_allowlist: tuple[str, ...] | None = None


@dataclass
class AgentExecutionResult:
    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    started_at: str
    finished_at: str
    timed_out: bool = False
    provider: str = ""
    model: str = ""
    effort: str = ""
    # Capture facts from the executor (see :class:`ExecutionResult`): the
    # engine logs the whole marked ``stdout`` but parses only ``stdout_tail``.
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_tail_offset: int = 0
    # What the invocation left behind (see :class:`ExecutionResult`): the
    # engine records these in the run log; none of them changes whether the
    # child's own result is read.
    descendants_killed: bool = False
    group_survived_kill: bool = False
    capture_abandoned: bool = False

    @property
    def stdout_tail(self) -> str:
        """The part of stdout captured contiguously up to EOF."""
        return self.stdout[self.stdout_tail_offset :]

    @property
    def leftovers(self) -> str:
        """What the invocation left behind, for a log line; empty when nothing."""
        return describe_leftovers(
            descendants_killed=self.descendants_killed,
            group_survived_kill=self.group_survived_kill,
            capture_abandoned=self.capture_abandoned,
        )

    @property
    def ok(self) -> bool:
        """Whether the invocation ran to a usable end.

        Narrower than :attr:`ExecutionResult.ok` on purpose: for plumbing
        (``gh --json``, ``git``) the whole output is the reply, so either
        stream past the bound spoils it, whereas an agent's reply is the
        CONTROL_RESULT on stdout and its stderr is diagnostics (an OpenCode
        tool trace), which the engine logs but never parses; a stderr past
        the bound is recorded, not a failure. The engine reads the underlying
        facts (``timed_out``, ``exit_code``, ``stdout_tail``) rather than
        this property.
        """
        return not self.timed_out and self.exit_code == 0 and not self.stdout_truncated

    @classmethod
    def from_execution(cls, res: ExecutionResult, profile: ProfileConfig) -> AgentExecutionResult:
        return cls(
            command=list(res.command),
            exit_code=res.exit_code,
            stdout=res.stdout,
            stderr=res.stderr,
            started_at=res.started_at,
            finished_at=res.finished_at,
            timed_out=res.timed_out,
            stdout_truncated=res.stdout_truncated,
            stderr_truncated=res.stderr_truncated,
            stdout_tail_offset=res.stdout_tail_offset,
            descendants_killed=res.descendants_killed,
            group_survived_kill=res.group_survived_kill,
            capture_abandoned=res.capture_abandoned,
            provider=profile.provider,
            model=profile.model,
            effort=profile.effort,
        )


def _truthy(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


class AgentProvider:
    """Base adapter. Subclasses implement ``build_command_for``."""

    name = "base"
    # The environment variables this provider's CLI reads for its own
    # authentication and configuration, added to the request's allow-list.
    # Names, never values: the executor selects them from the environment.
    environment_names: tuple[str, ...] = ()
    # The profile ``options`` keys this adapter reads. Any other key is a
    # configuration error naming the profile and this set, so a misspelled
    # knob cannot be a silent no-op. ``None`` leaves the keys unchecked; only
    # the scripted test provider, which launches no real CLI, uses it.
    option_keys: tuple[str, ...] | None = None

    def __init__(self, runner: Runner | None = None) -> None:
        self._runner: Runner = runner or execute

    # -- to override -----------------------------------------------------
    def build_command_for(self, profile: ProfileConfig, prompt: str) -> list[str]:
        raise NotImplementedError

    def validate_profile(self, profile: ProfileConfig) -> None:
        """Raise ConfigurationError for values this CLI cannot accept.

        An override calls this first: it checks the ``options`` keys.
        """
        if self.option_keys is None:
            return
        unknown = sorted(set(profile.options) - set(self.option_keys))
        if unknown:
            raise ConfigurationError(
                f"profile {profile.name!r}: unknown {self.name} option(s) "
                f"{', '.join(unknown)} (accepted: {', '.join(self.option_keys) or 'none'})"
            )

    # -- shared behaviour -------------------------------------------------
    def build_command(self, req: AgentRequest) -> list[str]:
        return self.build_command_for(req.profile, req.prompt)

    def launch_allowlist(self, base: tuple[str, ...]) -> tuple[str, ...]:
        """``base`` (the configured allow-list) plus this provider's own names."""
        return tuple(dict.fromkeys([*base, *self.environment_names]))

    def environment_allowlist(self, req: AgentRequest) -> tuple[str, ...] | None:
        """The allow-list the CLI is launched with: the request's plus this provider's."""
        if req.env_allowlist is None:
            return None
        return self.launch_allowlist(req.env_allowlist)

    def execute(self, req: AgentRequest) -> AgentExecutionResult:
        command = self.build_command(req)
        res = self._runner(
            ExecutionRequest(
                command=command,
                cwd=req.cwd,
                timeout_seconds=req.timeout_seconds,
                env_allowlist=self.environment_allowlist(req),
            )
        )
        return AgentExecutionResult.from_execution(res, req.profile)


CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
CLAUDE_PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")


class ClaudeCodeProvider(AgentProvider):
    name = "claude"
    # ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` and the base URL and
    # model overrides the CLI reads; ``CLAUDE_CONFIG_DIR`` and the
    # ``CLAUDE_CODE_*`` feature switches.
    environment_names = ("ANTHROPIC_*", "CLAUDE_*")
    option_keys = ("permission_mode", "output_format", "session_persistence")

    def validate_profile(self, profile: ProfileConfig) -> None:
        super().validate_profile(profile)
        if profile.effort and profile.effort not in CLAUDE_EFFORTS:
            raise ConfigurationError(
                f"profile {profile.name!r}: claude --effort must be one of {CLAUDE_EFFORTS}, "
                f"got {profile.effort!r}"
            )
        output_format = profile.options.get("output_format", "text") or "text"
        if output_format != "text":
            raise ConfigurationError(
                f"profile {profile.name!r}: claude output_format must be 'text' so the "
                f"CONTROL_RESULT block reaches stdout verbatim (got {output_format!r}; "
                "the claude CLI accepts text|json|stream-json, but json event streams escape it)"
            )
        mode = profile.options.get("permission_mode", "bypassPermissions")
        if mode and mode not in CLAUDE_PERMISSION_MODES:
            raise ConfigurationError(
                f"profile {profile.name!r}: unknown claude permission_mode {mode!r}"
            )

    def build_command_for(self, profile: ProfileConfig, prompt: str) -> list[str]:
        argv = [profile.command or "claude", "-p"]
        output_format = profile.options.get("output_format", "text") or "text"
        argv += ["--output-format", output_format]
        if profile.model:
            argv += ["--model", profile.model]
        if profile.effort:
            argv += ["--effort", profile.effort]
        mode = profile.options.get("permission_mode", "bypassPermissions")
        if mode:
            argv += ["--permission-mode", mode]
        if _truthy(profile.options.get("session_persistence"), default=False) is False:
            argv.append("--no-session-persistence")
        argv += list(profile.extra_args)
        # ``--`` guarantees the prompt is never parsed as an option.
        argv += ["--", prompt]
        return argv


class OpenCodeProvider(AgentProvider):
    name = "opencode"
    # OpenCode's own settings plus the API keys of the model providers it
    # routes to; a provider not listed here is added through
    # ``execution.env_allowlist_extra``.
    environment_names = ("OPENCODE_*", "OPENAI_*", "ANTHROPIC_*", "GEMINI_*", "GOOGLE_*")
    option_keys = ("output_format", "auto_approve")

    def validate_profile(self, profile: ProfileConfig) -> None:
        super().validate_profile(profile)
        if "/" not in profile.model:
            raise ConfigurationError(
                f"profile {profile.name!r}: opencode model must be 'provider/model' "
                f"(e.g. openai/gpt-5.6-luna), got {profile.model!r}"
            )
        fmt = profile.options.get("output_format", "default") or "default"
        if fmt != "default":
            raise ConfigurationError(
                f"profile {profile.name!r}: opencode output_format must be 'default' so the "
                "CONTROL_RESULT block reaches stdout verbatim (json event streams escape it)"
            )

    def build_command_for(self, profile: ProfileConfig, prompt: str) -> list[str]:
        argv = [profile.command or "opencode", "run"]
        if profile.model:
            argv += ["-m", profile.model]
        if profile.effort:
            argv += ["--variant", profile.effort]
        argv += ["--format", profile.options.get("output_format", "default") or "default"]
        if _truthy(profile.options.get("auto_approve"), default=False):
            argv.append("--auto")
        argv += list(profile.extra_args)
        # `--` ends option parsing: the prompt is one literal positional
        # even when it begins with `-`.
        argv += ["--", prompt]
        return argv


PI_EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
# ADR 0003 §2.8: the release its RPC, model and auth facts were read from.
# No upper bound.
PI_MIN_VERSION = (1, 0, 0)
PI_MODEL_PROVIDER_RE = re.compile(r"[a-z0-9-]+")
# What `pi --version` prints: the bare version, optionally a pre-release.
_PI_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?")


def parse_pi_version(text: str) -> tuple[int, int, int] | None:
    """``(major, minor, patch)`` from ``pi --version`` output, or None when unreadable.

    A pre-release sorts below its release: ``1.0.0-rc.1`` reads as
    ``(1, 0, -1)``, which is below the 1.0.0 minimum, and ``1.1.0-rc.1`` as
    ``(1, 1, -1)``, which is above it.
    """
    lines = text.strip().splitlines()
    match = _PI_VERSION_RE.fullmatch(lines[0].strip()) if len(lines) == 1 else None
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.group(1, 2, 3))
    if match.group(4):
        patch -= 1
    return major, minor, patch


PI_AUTH_STATUSES = ("ready", "not_ready", "invalid")
PI_AUTH_REASONS = (
    "provider_not_found",
    "credentials_not_configured",
    "credential_not_available",
    "invalid_state",
)
PI_AUTH_TYPES = ("api_key", "oauth")
# `pi auth check` exits 0 for ready, 1 for not_ready, 2 for invalid.
_PI_AUTH_EXIT_CODES = {"ready": 0, "not_ready": 1, "invalid": 2}
# The `provider` field is quoted back to the operator: a bounded run of
# printable ASCII (a provider id, or the model string on `invalid`).
_PI_AUTH_PROVIDER_RE = re.compile(r"[\x21-\x7e]{1,200}")


@dataclass(frozen=True)
class PiAuthCheck:
    """The parsed ``pi auth check --json`` result: these four fields, nothing else."""

    status: str
    provider: str
    reason: str = ""
    auth_type: str = ""


def parse_pi_auth_check(stdout: str, exit_code: int) -> PiAuthCheck:
    """Parse ``pi auth check --json`` strictly, or raise :class:`ValueError`.

    The output is one JSON object ``{status, provider, reason?, authType?}``
    and the exit code agrees with ``status``. Anything else is contract
    drift and fails closed. The error text never quotes the output: with
    the wrong flags it could hold a credential (``--credentials`` adds one).
    """
    lines = stdout.strip().splitlines()
    if len(lines) != 1:
        raise ValueError(f"expected one JSON line on stdout, got {len(lines)} line(s)")
    try:
        data = json.loads(lines[0])
    except json.JSONDecodeError:
        raise ValueError("stdout is not JSON") from None
    if not isinstance(data, dict):
        raise ValueError("stdout is not a JSON object")
    unknown = sorted(set(data) - {"status", "provider", "reason", "authType"})
    if unknown:
        # Counted, not named: a key is Pi output too, and may carry a secret.
        raise ValueError(f"{len(unknown)} unexpected field(s)")
    status = data.get("status")
    provider = data.get("provider")
    reason = data.get("reason", "")
    auth_type = data.get("authType", "")
    if status not in PI_AUTH_STATUSES:
        raise ValueError("unknown or missing 'status'")
    if not isinstance(provider, str) or not _PI_AUTH_PROVIDER_RE.fullmatch(provider):
        raise ValueError("missing or unreadable 'provider'")
    if status == "ready":
        if reason != "" or auth_type not in PI_AUTH_TYPES:
            raise ValueError("'ready' without a known 'authType', or with a 'reason'")
    elif auth_type != "" or reason not in PI_AUTH_REASONS:
        raise ValueError(f"{status!r} without a known 'reason', or with an 'authType'")
    if exit_code != _PI_AUTH_EXIT_CODES[status]:
        raise ValueError(f"exit code {exit_code} does not match status {status!r}")
    return PiAuthCheck(status=status, provider=provider, reason=reason, auth_type=auth_type)


class PiProvider(AgentProvider):
    """Pi (``@earendil-works/pi-coding-agent``) over its stdio RPC mode (ADR 0003).

    This class carries Pi's static surface: profile validation, the argv,
    the environment names and the read-only ``doctor`` probes. Running the
    RPC conversation is #131; until then :meth:`execute` refuses.
    """

    name = "pi"
    # Pi's own process settings (docs/environment-variables.md at v1.0.0):
    # where its config and credential store live, a Nix-style package
    # directory, and the network, version-check, telemetry and prompt-cache
    # switches. Explicit names, never a prefix: Pi exports `PI_SESSION_*`,
    # `PI_PROVIDER`, `PI_MODEL` to its tool children, and a provider API key
    # (`OPENAI_API_KEY`, ...) reaches Pi only through
    # `execution.env_allowlist_extra`, never by default.
    environment_names = (
        "PI_CODING_AGENT_DIR",
        "PI_PACKAGE_DIR",
        "PI_OFFLINE",
        "PI_SKIP_VERSION_CHECK",
        "PI_TELEMETRY",
        "PI_CACHE_RETENTION",
    )
    option_keys = ("require_oauth",)

    def validate_profile(self, profile: ProfileConfig) -> None:
        super().validate_profile(profile)
        where = f"profile {profile.name!r}"
        if profile.effort not in PI_EFFORTS:
            raise ConfigurationError(
                f"{where}: pi effort (--thinking) is required and must be one of "
                f"{', '.join(PI_EFFORTS)}, got {profile.effort!r}"
            )
        problem = _pi_model_problem(profile.model)
        if problem:
            raise ConfigurationError(
                f"{where}: pi model must be '<pi-provider>/<model-id>' "
                f"(e.g. openai/gpt-5.6-terra): {problem}, got {profile.model!r}"
            )
        if profile.extra_args:
            raise ConfigurationError(
                f"{where}: pi takes no extra_args (got {profile.extra_args!r}); every Pi "
                "flag that matters is set by the adapter, and a pass-through could re-enable "
                "project trust, put an API key in argv, load code or prompts, or keep a session"
            )
        if profile.options.get("require_oauth", "true") not in ("true", "false"):
            raise ConfigurationError(
                f"{where}: pi option require_oauth must be true or false, "
                f"got {profile.options['require_oauth']!r}"
            )

    def build_command_for(self, profile: ProfileConfig, prompt: str) -> list[str]:
        # The prompt is deliberately absent: it reaches Pi as a JSON `prompt`
        # record on stdin (ADR 0003 §2.7), never in argv.
        return [
            self.command(profile),
            "--mode",
            "rpc",
            "--no-session",
            "--model",
            profile.model,
            "--thinking",
            profile.effort,
        ]

    def execute(self, req: AgentRequest) -> AgentExecutionResult:
        raise ExecutionError(
            f"profile {req.profile.name!r}: pi execution is not implemented yet "
            "(the RPC adapter is #131); route this phase to another provider"
        )

    # -- read-only probes for `autoforge doctor` ---------------------------
    @staticmethod
    def command(profile: ProfileConfig) -> str:
        return profile.command or "pi"

    @staticmethod
    def require_oauth(profile: ProfileConfig) -> bool:
        return profile.options.get("require_oauth", "true") != "false"

    @staticmethod
    def model_provider(profile: ProfileConfig) -> str:
        return profile.model.split("/", 1)[0]

    def version_command(self, profile: ProfileConfig) -> list[str]:
        return [self.command(profile), "--version"]

    def auth_check_command(self, profile: ProfileConfig) -> list[str]:
        """Whether Pi holds a credential for the model's provider, read-only.

        ``--no-refresh`` makes Pi open its credential store read-only (no
        OAuth refresh is written), and ``--credentials``, which would print
        the credential, is never passed.
        """
        return [
            self.command(profile),
            "auth",
            "check",
            "--model",
            profile.model,
            "--json",
            "--no-refresh",
        ]

    def auth_verdict(self, profile: ProfileConfig, exit_code: int, stdout: str) -> tuple[bool, str]:
        """``(ok, detail)`` for the result of :meth:`auth_check_command`.

        The detail quotes only the four fields of the result, never stdout
        or stderr. ``ready`` shows that Pi holds a credential for the
        provider, not that the model exists or that the credential may use
        it: Pi builds a placeholder for an id outside its catalog and still
        reports ``ready`` (ADR 0003 §4 item 2).
        """
        command = " ".join(self.auth_check_command(profile))
        try:
            check = parse_pi_auth_check(stdout, exit_code)
        except ValueError as exc:
            return False, (
                f"`pi auth check` exited {exit_code} without a result AutoForge can read "
                f"({exc}); Pi's output is not shown, run `{command}` yourself to see it"
            )
        configured = self.model_provider(profile)
        fields = f"status {check.status}, provider {check.provider!r}"
        if check.reason:
            fields += f", reason {check.reason}"
        if check.auth_type:
            fields += f", authType {check.auth_type}"
        # An `invalid` result names what was asked for (the model, when Pi
        # failed before resolving it), so only the others name a provider.
        if check.status != "invalid" and check.provider != configured:
            return False, (
                f"{fields}: Pi resolved model {profile.model!r} to provider "
                f"{check.provider!r}, not the configured {configured!r}"
            )
        if check.status == "ready":
            if check.auth_type == "api_key" and self.require_oauth(profile):
                return False, (
                    f"{fields}: this profile requires Pi's ChatGPT sign-in; Pi resolved an "
                    "API key (set options.require_oauth: false to run on an API key)"
                )
            return True, (
                f"{fields}: authenticated for provider {configured!r} only; whether this "
                f"credential may use {profile.model!r} cannot be checked read-only (doctor "
                "neither refreshes nor uses it), the first real run is the proof"
            )
        if check.reason == "credentials_not_configured":
            if configured in ("openai", "openai-codex"):
                remedy = (
                    f"run `pi`, then `/login {configured}` (an operator step; AutoForge "
                    "never logs in for you)"
                )
            else:
                remedy = (
                    f"give Pi a credential for {configured!r}; an API key reaches Pi only "
                    "through execution.env_allowlist_extra"
                )
            return False, f"{fields}: Pi has no credential for {configured!r}; {remedy}"
        if check.reason == "provider_not_found":
            return False, (
                f"{fields}: Pi does not know the provider {configured!r} named by "
                f"model {profile.model!r}"
            )
        if check.reason == "credential_not_available":
            return False, f"{fields}: Pi could not produce a usable credential for {configured!r}"
        return False, (
            f"{fields}: Pi's model or credential configuration is invalid; run `{command}` "
            "yourself to see why"
        )


def _pi_model_problem(model: str) -> str:
    """Why ``model`` is not a valid Pi ``provider/id`` (ADR 0003 §2.2), or ''."""
    if not model:
        return "it is empty"
    if any(ch.isspace() for ch in model):
        return "it contains whitespace"
    if model.count("/") != 1:
        return "it must contain exactly one '/'"
    provider, model_id = model.split("/")
    if not PI_MODEL_PROVIDER_RE.fullmatch(provider):
        return "the provider part must be non-empty and use only a-z, 0-9 and '-'"
    if not model_id:
        return "the model id is empty"
    if ":" in model and model.rsplit(":", 1)[1] in PI_EFFORTS:
        return "a ':<thinking>' suffix is not allowed; set the level with 'effort'"
    return ""


ScriptHandler = Callable[[AgentRequest], str]


class ScriptedProvider(AgentProvider):
    """Test/integration provider: returns canned stdout, never spawns a process.

    ``script`` is either a list of stdout strings consumed in order, or a
    callable ``(AgentRequest) -> stdout``. Every request is recorded in
    ``calls`` so tests can assert on prompts, phases and cwd.
    """

    name = "scripted"

    def __init__(
        self,
        script: list[str] | ScriptHandler | None = None,
        exit_code: int = 0,
        stderr: str = "",
    ) -> None:
        super().__init__(runner=None)
        self._queue: list[str] = list(script) if isinstance(script, list) else []
        self._handler: ScriptHandler | None = script if callable(script) else None
        self.exit_code = exit_code
        self.stderr = stderr
        self.calls: list[AgentRequest] = []

    def build_command_for(self, profile: ProfileConfig, prompt: str) -> list[str]:
        return ["scripted-agent", f"--profile={profile.name}", "--", prompt]

    def execute(self, req: AgentRequest) -> AgentExecutionResult:
        self.calls.append(req)
        if self._handler is not None:
            stdout = self._handler(req)
        elif self._queue:
            stdout = self._queue.pop(0)
        else:
            stdout = ""
        return AgentExecutionResult(
            command=self.build_command(req),
            exit_code=self.exit_code,
            stdout=stdout,
            stderr=self.stderr,
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:00:01+00:00",
            provider="scripted",
            model=req.profile.model,
            effort=req.profile.effort,
        )


_REGISTRY: dict[str, type[AgentProvider]] = {
    "claude": ClaudeCodeProvider,
    "opencode": OpenCodeProvider,
    "pi": PiProvider,
    "scripted": ScriptedProvider,
}


def provider_for(profile: ProfileConfig, runner: Runner | None = None) -> AgentProvider:
    cls = _REGISTRY.get(profile.provider)
    if cls is None:
        raise ConfigurationError(
            f"profile {profile.name!r}: unknown provider {profile.provider!r} "
            f"(known: {', '.join(sorted(_REGISTRY))})"
        )
    if cls is ScriptedProvider:
        return ScriptedProvider()
    return cls(runner=runner)


class ProviderRegistry:
    """Resolves providers per profile; allows injecting fakes per provider name."""

    def __init__(
        self,
        runner: Runner | None = None,
        overrides: dict[str, AgentProvider] | None = None,
    ) -> None:
        self._runner = runner
        self._overrides = dict(overrides or {})

    def get(self, profile: ProfileConfig) -> AgentProvider:
        if profile.provider in self._overrides:
            return self._overrides[profile.provider]
        return provider_for(profile, runner=self._runner)
