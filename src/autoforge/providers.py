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
                 [extra_args] <message>

  * model ids are ``provider/model`` (``openai/gpt-5.6-luna``).
  * ``--variant`` carries the provider-specific reasoning effort.
  * ``--format default`` prints only the final assistant text to stdout
    (tool traces go to stderr); ``--format json`` would emit an event
    stream where the CONTROL_RESULT is JSON-escaped, so it is NOT used.
  * bash/edit tools run without ``--auto`` under the default agent;
    ``--auto`` is opt-in via ``options.auto_approve: true``.
  * exit code 0 on success; 1 on unknown model / server error.

Both CLIs are launched with cwd = repository root, stdin = /dev/null, and a
hard timeout (see executor.py).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .config import ProfileConfig
from .errors import ConfigurationError
from .executor import ExecutionRequest, ExecutionResult, execute

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

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.exit_code == 0

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

    def __init__(self, runner: Runner | None = None) -> None:
        self._runner: Runner = runner or execute

    # -- to override -----------------------------------------------------
    def build_command_for(self, profile: ProfileConfig, prompt: str) -> list[str]:
        raise NotImplementedError

    def validate_profile(self, profile: ProfileConfig) -> None:
        """Raise ConfigurationError for values this CLI cannot accept."""

    # -- shared behaviour -------------------------------------------------
    def build_command(self, req: AgentRequest) -> list[str]:
        return self.build_command_for(req.profile, req.prompt)

    def execute(self, req: AgentRequest) -> AgentExecutionResult:
        command = self.build_command(req)
        res = self._runner(
            ExecutionRequest(command=command, cwd=req.cwd, timeout_seconds=req.timeout_seconds)
        )
        return AgentExecutionResult.from_execution(res, req.profile)


CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
CLAUDE_PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")


class ClaudeCodeProvider(AgentProvider):
    name = "claude"

    def validate_profile(self, profile: ProfileConfig) -> None:
        if profile.effort and profile.effort not in CLAUDE_EFFORTS:
            raise ConfigurationError(
                f"profile {profile.name!r}: claude --effort must be one of {CLAUDE_EFFORTS}, "
                f"got {profile.effort!r}"
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

    def validate_profile(self, profile: ProfileConfig) -> None:
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
        argv.append(prompt)
        return argv


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
