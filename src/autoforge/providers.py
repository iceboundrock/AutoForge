"""Agent provider adapters: the only place that knows real CLI flags.

The engine speaks :class:`AgentRequest` / :class:`AgentExecutionResult`;
each provider translates a logical execution profile (provider, model,
effort, timeout, options) into a concrete non-interactive CLI invocation.

Verified against locally installed CLIs (2026-09):

Claude Code (``claude 2.1.x``; the stream run against 2.1.291 to 2.1.293)::

    claude -p --output-format stream-json --verbose --model <alias|id>
           --effort <low|medium|high|xhigh|max> --permission-mode <mode>
           --no-session-persistence [extra_args] -- <prompt>

  * ``-p/--print`` = non-interactive. With ``stream-json`` (the default,
    #192) stdout is one JSON record per line while the agent runs
    (``system`` init / thinking_tokens / api_retry, ``assistant`` content
    blocks, ``user`` tool results, ``rate_limit_event``) and a ``result``
    record that ends the turn, whose ``result`` field is its final
    assistant text; a background task or a scheduled wakeup still pending
    then keeps the CLI alive for another turn with its own ``result``. The
    CLI refuses ``stream-json`` under ``--print`` without ``--verbose``.
    The child runs under the duplex handle and the stream is reduced in
    claude_stream.py: ``stdout`` of the result is the last ``result``
    text verbatim (so the CONTROL_RESULT block survives untouched), the
    other records become live progress (:mod:`autoforge.progress`), and an
    error result in any turn, a result outside a turn, a malformed line or
    a turn without its result is a
    :attr:`AgentExecutionResult.provider_failure`.
  * ``options.output_format: text`` is the opt-out: the final text is
    printed once, at exit, and the run reports no progress and no activity
    while it runs.
  * ``--permission-mode bypassPermissions`` is required for unattended
    edits / git / gh calls; ``--permission-prompts none`` would silently
    deny anything that prompts. Configure via profile ``options``.
  * exit code 0 on success; 1 on model/auth errors (message on stdout).

OpenCode (``opencode 2.0.x``, #186; run against 2.0.23)::

    opencode run --standalone -m <provider/model>[#<effort>] --format default [--auto]
                 [extra_args]                                   (message on stdin)

  * the message travels on stdin, written whole and then closed: in argv,
    v2 duplicates a message given after ``--`` and wraps one given without
    it in quotes with ``"`` and backslashes escaped, and takes one that starts
    with ``-`` for a flag (it prints the help and exits 0). stdin arrives
    verbatim. A CLI that exits 0 without having read all of it is a
    :attr:`AgentExecutionResult.provider_failure`.
  * ``--standalone`` runs the agent on a private server that is the
    client's own child; without it the run goes to a shared background
    service (``opencode serve --service``) whose tools run outside this
    invocation's process group, cwd and environment, and outlive it.
  * model ids are ``provider/model`` (``openai/gpt-5.6-luna``); the
    reasoning effort is the ``#<variant>`` suffix (v2 removed
    ``--variant``), so a configured model carries no ``#`` of its own. An
    unknown model or variant exits 1 with nothing on stdout.
  * ``--format default`` prints only the final assistant text to stdout
    (tool traces go to stderr). ``--format json`` is NOT used: on 2.0.23 it
    emits ``step_start``, ``text``, ``tool_use`` and ``step_finish``
    records, but a ``tool_use`` record only once the tool has completed (no
    record when it starts) and no terminal record carrying the final text
    or the outcome, so it could neither show a tool as it starts nor be
    reduced to the CONTROL_RESULT text the way Claude's stream is (#192).
    Live progress is therefore liveness only: each chunk on stdout or
    stderr is an ``activity`` event, which prints nothing but keeps the
    heartbeat's "last activity" current; the trace itself reaches the
    operator only in ``stderr.log``.
  * bash/edit tools run without ``--auto`` under the default agent;
    ``--auto`` is opt-in via ``options.auto_approve: true``.
  * ``opencode --version`` prints ``opencode v2.0.23``; 1.x printed the
    bare version and rejects this argv, so ``autoforge doctor`` requires
    :data:`OPENCODE_MIN_VERSION`.

Pi (``pi 1.0.x``, ADR 0003; read from the v1.0.0 source and docs, not yet
run against an installed Pi)::

    pi --mode rpc --no-session --model <pi-provider>/<model-id> --thinking <effort>

  * the prompt never travels in argv: it is one JSON ``prompt`` record on
    stdin. The child runs under the duplex handle (executor_duplex.py) and
    the conversation (``get_state``, ``get_available_models``, ``prompt``,
    events until ``agent_settled``, ``get_last_assistant_text``) is the
    reducer in pi_rpc.py; ``stdout`` of the result is the final assistant
    text, never a protocol record, and an in-protocol failure is reported
    in :attr:`AgentExecutionResult.provider_failure`.
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
LOCAL one), stdin = /dev/null or the adapter's payload
(:meth:`AgentProvider.stdin_payload`), an idle limit and an optional
wall-clock ceiling (:class:`AgentRequest`; #193), and an allow-listed
environment (see executor.py): the engine passes the configured allow-list
in :attr:`AgentRequest.env_allowlist` and each adapter adds the variables
its own CLI reads (:attr:`AgentProvider.environment_names`), so the key of
one provider is never handed to another.

Every adapter runs a :class:`~autoforge.loop_detect.LoopMonitor` under
:attr:`AgentRequest.loop_detection` (#194): Claude's stream and Pi's events
report each completed tool call as an action, and a one-shot CLI's output
(OpenCode's stderr trace, Claude's ``text`` output) is digested line by
line as it arrives. A verdict in ``kill`` mode ends the invocation through
the same group kill as a timeout, and the result is a timeout whose limit is
``loop``; a warning is a ``loop_suspected`` progress event. The monitor's
calibration figures join ``provider_summary`` in every mode.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .claude_stream import ClaudeStream
from .config import LoopDetectionConfig, ProfileConfig
from .errors import (
    ChildStdinClosedError,
    ConfigurationError,
    ExecutionError,
    ExecutionTimeoutError,
)
from .executor import (
    DEFAULT_MAX_OUTPUT_BYTES,
    MAX_DEADLINE_SECONDS,
    ExecutionRequest,
    ExecutionResult,
    describe_leftovers,
    execute,
    select_environment,
)
from .executor_duplex import (
    DuplexChild,
    DuplexRequest,
    Eof,
    Fragment,
    Overflow,
    Oversize,
    Record,
    Timeout,
    start_duplex,
)
from .loop_detect import LIMIT_LOOP, LoopMonitor, LoopReport
from .pi_rpc import PiConversation, encodable
from .progress import ProgressEvent, ProgressKind, ProgressSink, guarded

Runner = Callable[[ExecutionRequest], ExecutionResult]


@dataclass
class AgentRequest:
    phase: str
    prompt: str
    cwd: str
    profile: ProfileConfig
    # The limits the invocation runs under (#193; the engine resolves them
    # with ``AutoForgeConfig.agent_limits``): the CLI is killed once it has
    # written nothing on stdout or stderr for ``idle_timeout_seconds``
    # (``None``: no idle limit, for a CLI that reports no activity while it
    # works), or once it has run ``max_runtime_seconds`` (``None``: only the
    # executor's backstop, ``MAX_DEADLINE_SECONDS``).
    idle_timeout_seconds: int | None
    max_runtime_seconds: int | None
    attempt: int = 1
    correction: bool = False
    # The controller's environment allow-list (``execution.env_allowlist``
    # plus ``env_allowlist_extra``); the provider adds its own names. ``None``
    # inherits the whole environment and is never what the engine sends.
    env_allowlist: tuple[str, ...] | None = None
    # Where the adapter reports live progress (#192): provider-neutral,
    # already bounded and redacted events, never a raw provider record.
    # ``None`` reports nothing. A sink that raises is dropped for the rest of
    # the invocation (:func:`autoforge.progress.guarded`); progress never
    # changes an outcome.
    progress: ProgressSink | None = None
    # How the adapter watches the invocation for a loop (#194): the engine
    # passes ``execution.loop_detection``.
    loop_detection: LoopDetectionConfig = field(default_factory=LoopDetectionConfig)


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
    orphans_killed: bool = False
    orphan_survived_kill: bool = False
    orphans_unchecked: bool = False
    # A run that failed inside the provider's protocol although the process
    # may have exited 0 (Pi shuts down in order whether its run failed or
    # not): a short, bounded, already-redacted reason. The engine treats it
    # like a non-zero exit (ADR 0003 §2.6). A one-shot CLI adapter sets it
    # only when the CLI exited 0 without reading the whole prompt from stdin.
    provider_failure: str | None = None
    # A small flat mapping of scalars, bounded and redacted by the adapter,
    # that the engine writes to ``execution.json`` under this key without
    # interpreting it. Never raw protocol records or message contents.
    provider_summary: dict[str, str | int | bool] = field(default_factory=dict)
    # Which limit killed the CLI when ``timed_out`` (``"idle"``,
    # ``"max_runtime"``, or ``"loop"`` when the loop detector did, #194), and
    # when it last wrote anything (UTC, ISO 8601; ``None`` when it wrote
    # nothing). See :class:`ExecutionResult`.
    timeout_limit: str = ""
    last_activity_at: str | None = None
    # What the loop detector found (#194): the finding it killed for, or the
    # strongest it warned about; ``None`` when neither. Names and counts only.
    loop: LoopReport | None = None

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
            orphans_killed=self.orphans_killed,
            orphan_survived_kill=self.orphan_survived_kill,
            orphans_unchecked=self.orphans_unchecked,
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
            orphans_killed=res.orphans_killed,
            orphan_survived_kill=res.orphan_survived_kill,
            orphans_unchecked=res.orphans_unchecked,
            provider=profile.provider,
            model=profile.model,
            effort=profile.effort,
            timeout_limit=res.timeout_limit,
            last_activity_at=res.last_activity_at,
        )


def _activity_hook(progress: ProgressSink | None) -> Callable[[str], None] | None:
    """An executor output hook that reports each chunk as bare activity.

    For a one-shot CLI whose output is not a protocol the adapter reads
    while it runs (OpenCode's ``--format default``, Claude's ``text``): a
    chunk on either stream is a sign of life and nothing more. Its bytes
    are never looked at, so nothing the CLI printed reaches the progress
    output.
    """
    emit = guarded(progress)
    if emit is None:
        return None

    def on_output(stream: str) -> None:
        emit(ProgressEvent(ProgressKind.ACTIVITY))

    return on_output


def _loop_monitor(req: AgentRequest, emit: ProgressSink | None) -> LoopMonitor:
    """The invocation's loop detector (#194), warning through ``emit``."""

    def warn(text: str) -> None:
        if emit is not None:
            emit(ProgressEvent(ProgressKind.LOOP_SUSPECTED, detail=text))

    return LoopMonitor(req.loop_detection, time.monotonic(), warn)


class _ClockedLoop:
    """A :class:`LoopMonitor` as a clock-free reducer reports to it: each
    event is timed as it is reduced, so the reducer reads no clock."""

    def __init__(self, monitor: LoopMonitor) -> None:
        self._monitor = monitor

    def action(self, name: str, fingerprint: bytes) -> None:
        self._monitor.action(name, fingerprint, time.monotonic())

    def retry(self) -> None:
        self._monitor.retry(time.monotonic())

    def turn(self) -> None:
        self._monitor.turn(time.monotonic())


def _finish_loop(
    result: AgentExecutionResult, monitor: LoopMonitor, *, killed: bool
) -> AgentExecutionResult:
    """Record the loop detector's outcome on ``result`` (#194).

    ``killed``: the invocation was ended on the verdict. It is then a
    timeout whose limit is ``loop``, which the engine and the run log treat
    exactly as the other limits: whatever the protocol said is not read. The
    calibration figures join ``provider_summary`` whatever the mode.
    """
    result.provider_summary.update(monitor.calibration())
    result.loop = monitor.report(killed=killed)
    if killed:
        result.timed_out = True
        result.timeout_limit = LIMIT_LOOP
        result.provider_failure = None
    return result


def _truthy(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


class AgentProvider:
    """Base adapter. Subclasses implement ``build_command_for``, and
    ``stdin_payload`` when the prompt does not travel in argv."""

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

    def stdin_payload(self, req: AgentRequest) -> bytes | None:
        """What :meth:`execute` writes to the CLI's stdin; ``None`` is ``/dev/null``."""
        return None

    def reports_activity(self, profile: ProfileConfig) -> bool:
        """Whether the CLI, launched for ``profile``, writes output while it works.

        A fact about the CLI, not a policy: the controller decides what to do
        with a CLI that is silent until it exits (it cannot run under an idle
        limit, see ``AutoForgeConfig.agent_limits``).
        """
        return True

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
        monitor = _loop_monitor(req, guarded(req.progress))
        stop = threading.Event()

        def on_chunk(stream: str, chunk: bytes) -> None:
            # The output is not a protocol: its lines are digested as they
            # arrive and dropped (#194); the capture keeps its own copy.
            if monitor.output(stream, chunk, time.monotonic()) is not None:
                stop.set()

        res = self._runner(
            ExecutionRequest(
                command=command,
                cwd=req.cwd,
                timeout_seconds=req.max_runtime_seconds or MAX_DEADLINE_SECONDS,
                idle_timeout_seconds=req.idle_timeout_seconds,
                env_allowlist=self.environment_allowlist(req),
                # Nothing an agent starts outlives its invocation (ADR 0002),
                # including what it detached from its process group.
                contain_orphans=True,
                stdin_data=self.stdin_payload(req),
                on_output=_activity_hook(req.progress),
                on_chunk=on_chunk,
                stop=stop,
            )
        )
        result = AgentExecutionResult.from_execution(res, req.profile)
        if res.stdin_unread and res.exit_code == 0 and not res.timed_out and not res.stopped:
            # A clean exit would otherwise be read as an answer to the whole
            # prompt; a timeout or a failed exit already says more.
            result.provider_failure = (
                f"{self.name}: the prompt was not delivered: the CLI exited without "
                f"reading the last {res.stdin_unread} bytes of it"
            )
        return _finish_loop(result, monitor, killed=res.stopped)


CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
CLAUDE_PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")
# `stream-json` (the default) reports progress while the agent runs and
# carries the final text in its last `result` line; `text` prints only the
# final text, at exit, and is kept as an explicit opt-out. `json` prints one
# object at exit: no progress, so nothing over `text`, and not accepted.
CLAUDE_OUTPUT_FORMATS = ("stream-json", "text")
CLAUDE_DEFAULT_OUTPUT_FORMAT = "stream-json"
# One stream-json line is bounded like a Pi record: the `result` line must
# arrive whole, and JSON escaping can double a final text that is itself
# up to the stdout bound. A longer line (a `tool_result` quoting a large
# file) is skipped and counted, and is never buffered whole.
CLAUDE_MAX_RECORD_BYTES = 2 * DEFAULT_MAX_OUTPUT_BYTES + 1024 * 1024
CLAUDE_MAX_PENDING_BYTES = 2 * CLAUDE_MAX_RECORD_BYTES
# How long the stream driver waits for a line before it checks whether the
# CLI has exited (it also checks after every line): a descendant holding
# stdout keeps EOF away, and this is how late its exit grace (ADR 0002) can
# start.
CLAUDE_EXIT_POLL_SECONDS = 0.25


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
        output_format = self.output_format(profile)
        if output_format not in CLAUDE_OUTPUT_FORMATS:
            raise ConfigurationError(
                f"profile {profile.name!r}: claude output_format must be one of "
                f"{', '.join(CLAUDE_OUTPUT_FORMATS)} (got {output_format!r}): stream-json "
                "reports progress and is reduced to the final text, text is the final text "
                "alone with no progress, and json gives neither"
            )
        mode = profile.options.get("permission_mode", "bypassPermissions")
        if mode and mode not in CLAUDE_PERMISSION_MODES:
            raise ConfigurationError(
                f"profile {profile.name!r}: unknown claude permission_mode {mode!r}"
            )

    @staticmethod
    def output_format(profile: ProfileConfig) -> str:
        return profile.options.get("output_format") or CLAUDE_DEFAULT_OUTPUT_FORMAT

    def reports_activity(self, profile: ProfileConfig) -> bool:
        # `text` prints the final text once, at exit, and nothing before it.
        return self.output_format(profile) == "stream-json"

    def build_command_for(self, profile: ProfileConfig, prompt: str) -> list[str]:
        argv = [profile.command or "claude", "-p"]
        output_format = self.output_format(profile)
        argv += ["--output-format", output_format]
        if output_format == "stream-json":
            # The CLI refuses stream-json under --print without it.
            argv.append("--verbose")
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

    def execute(self, req: AgentRequest) -> AgentExecutionResult:
        """Run the CLI; with stream-json, reduce its stream to the final text.

        ``output_format: text`` is the one-shot path every adapter shares.
        With ``stream-json`` the child runs under the duplex handle with its
        stdin on ``/dev/null``: the handle already frames LF-terminated
        records against a per-record bound and drops a longer one through
        its LF without buffering it, which is exactly what this stream needs
        (a ``tool_result`` line quotes whole files), so a line callback in
        the one-shot executor would have duplicated that framer. Every line
        goes to :class:`~autoforge.claude_stream.ClaudeStream`, which emits
        progress and decides the outcome. The CLI's exit, not EOF, ends the
        wait, so a leftover holding stdout costs ADR 0002's exit grace and
        kill, never the timeout. ``stdout`` of the result is the last
        ``result`` text, tail-bounded as the executor's capture is; a failure
        inside the stream is ``provider_failure`` and a timeout wins over it.
        ``exit_code`` is always the real process status.
        """
        if self.output_format(req.profile) != "stream-json":
            return super().execute(req)
        emit = guarded(req.progress)
        monitor = _loop_monitor(req, emit)
        stream = ClaudeStream(cwd=req.cwd, emit=emit, loop=_ClockedLoop(monitor))
        duplex = DuplexRequest(
            command=self.build_command(req),
            cwd=req.cwd,
            env_allowlist=self.environment_allowlist(req),
            idle_timeout_seconds=req.idle_timeout_seconds,
            max_runtime_seconds=req.max_runtime_seconds,
            max_record_bytes=CLAUDE_MAX_RECORD_BYTES,
            max_pending_bytes=CLAUDE_MAX_PENDING_BYTES,
            # The prompt travels in argv; stdin stays /dev/null so the CLI
            # never waits on it.
            stdin_pipe=False,
            # Nothing an agent starts outlives its invocation (ADR 0002).
            contain_orphans=True,
        )
        with start_duplex(duplex) as child:
            cut_short = _read_claude_stream(child, stream, monitor)
            res = child.finish()
        timed_out = cut_short or res.timed_out or res.stopped
        failure = stream.failure
        if stream.exited_early:
            failure = f"{failure} (exit {res.exit_code})"
        elif res.records_overflowed and failure is None:
            failure = "claude: stream-json lines arrived faster than they were read"
        summary = stream.summary()
        summary["failure"] = failure or ""
        text = stream.text if failure is None else None
        stdout, truncated = _keep_tail(encodable(text or ""), DEFAULT_MAX_OUTPUT_BYTES)
        result = AgentExecutionResult(
            command=list(res.command),
            exit_code=res.exit_code,
            stdout=stdout,
            stderr=res.stderr,
            started_at=res.started_at,
            finished_at=res.finished_at,
            timed_out=timed_out,
            provider=req.profile.provider,
            model=req.profile.model,
            effort=req.profile.effort,
            stdout_truncated=truncated,
            stderr_truncated=res.stderr_truncated,
            stdout_tail_offset=0,
            descendants_killed=res.descendants_killed,
            group_survived_kill=res.group_survived_kill,
            capture_abandoned=res.capture_abandoned,
            orphans_killed=res.orphans_killed,
            orphan_survived_kill=res.orphan_survived_kill,
            orphans_unchecked=res.orphans_unchecked,
            provider_failure=None if timed_out else failure,
            provider_summary=summary,
            timeout_limit=res.timeout_limit,
            last_activity_at=res.last_activity_at,
        )
        return _finish_loop(result, monitor, killed=res.stopped)


def _read_claude_stream(
    child: DuplexChild, stream: ClaudeStream, loop: LoopMonitor | None = None
) -> bool:
    """Feed every stdout outcome to ``stream`` through its end; True on the deadline.

    The CLI's exit, not EOF, ends the wait: a descendant still holding its
    stdout, silent or writing, must not hold the invocation to the deadline
    (ADR 0002). After every read, a line included, the driver looks at the
    CLI; once it has exited, ``finish()`` gives what is left the exit grace
    and kills it, keeping the CLI's own status, and the rest of stdout,
    queued up to its end by then, is fed and validated like every line
    before it. A read that meets the deadline after the CLI's exit does the
    same (``read_line`` looks at the CLI first), so only a CLI still running
    at the deadline is a timeout. A verdict of ``loop`` (#194) on a CLI
    still running stops it, and the caller's ``finish()`` reports
    ``stopped``.
    """
    while True:
        item = child.read_line(timeout=CLAUDE_EXIT_POLL_SECONDS)
        if isinstance(item, Record):
            stream.feed(item.data)
        elif isinstance(item, Oversize):
            stream.oversize(item.limit)
        elif isinstance(item, Fragment):
            stream.framing_error("stdout ended inside an unterminated line")
        elif isinstance(item, Overflow):
            stream.framing_error("stdout lines arrived faster than they were read")
            return False
        elif isinstance(item, Eof):
            stream.stream_ended()
            return False
        elif item.deadline_exceeded:
            return True
        if loop is not None and loop.verdict is not None and child.stop():
            return False
        if child.exited():
            child.finish()


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
        if "#" in profile.model:
            raise ConfigurationError(
                f"profile {profile.name!r}: opencode model must not carry a '#variant' "
                f"suffix (got {profile.model!r}); set the variant as the profile's effort"
            )
        fmt = profile.options.get("output_format", "default") or "default"
        if fmt != "default":
            raise ConfigurationError(
                f"profile {profile.name!r}: opencode output_format must be 'default' so the "
                "CONTROL_RESULT block reaches stdout verbatim (the json event stream escapes "
                "it and has no final result record)"
            )

    def build_command_for(self, profile: ProfileConfig, prompt: str) -> list[str]:
        """The argv, without the prompt: it goes on stdin (:meth:`stdin_payload`)."""
        argv = [profile.command or "opencode", "run", "--standalone"]
        if profile.model:
            variant = f"#{profile.effort}" if profile.effort else ""
            argv += ["-m", profile.model + variant]
        argv += ["--format", profile.options.get("output_format", "default") or "default"]
        if _truthy(profile.options.get("auto_approve"), default=False):
            argv.append("--auto")
        argv += list(profile.extra_args)
        return argv

    def stdin_payload(self, req: AgentRequest) -> bytes:
        # A lone surrogate (legal in the JSON GitHub returns, not in UTF-8)
        # becomes U+FFFD rather than failing the launch.
        return encodable(req.prompt).encode("utf-8")


# #186: the CLI whose argv and stdin delivery the adapter speaks. No upper bound.
OPENCODE_MIN_VERSION = (2, 0, 0)
# What `opencode --version` prints: `opencode v2.0.23`; 1.x printed `1.18.34`.
_OPENCODE_VERSION_RE = re.compile(r"(?:opencode )?v?(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?")


def parse_opencode_version(text: str) -> tuple[int, int, int] | None:
    """``(major, minor, patch)`` from ``opencode --version`` output, or None
    when unreadable; a pre-release sorts below its release, as for Pi."""
    return _parse_version(text, _OPENCODE_VERSION_RE)


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
    return _parse_version(text, _PI_VERSION_RE)


def _parse_version(text: str, pattern: re.Pattern[str]) -> tuple[int, int, int] | None:
    lines = text.strip().splitlines()
    match = pattern.fullmatch(lines[0].strip()) if len(lines) == 1 else None
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.group(1, 2, 3))
    if match.group(4):
        patch -= 1
    return major, minor, patch


# ADR 0003 §3: each stdio round trip around the prompt (`get_state`,
# `get_available_models`, `get_last_assistant_text`) is bounded inside the
# invocation's limits. The first two also cover Pi's start-up (Node, its
# settings and its model catalog), hence more than a bare round trip needs.
PI_ROUND_TRIP_SECONDS = 15.0
# How long an `abort` is given to settle a running prompt before stdin is
# closed. It follows a limit falling due and never shortens it (#193): the
# group is killed this long after the limit, Pi's own exit included.
PI_ABORT_SECONDS = 5.0
# A final text past the stdout bound must still arrive whole in one
# `get_last_assistant_text` record so that its tail can be kept, and JSON
# escaping can double a text. Pi's `agent_end` is not bounded by this: it
# repeats every message of the run (verified on Pi 1.0.1), so a very long run
# can overflow the bound and fail closed as a protocol violation (#144).
PI_MAX_RECORD_BYTES = 2 * DEFAULT_MAX_OUTPUT_BYTES + 1024 * 1024
PI_MAX_PENDING_BYTES = 2 * PI_MAX_RECORD_BYTES


# Pi's built-in tools (dist/core/tools/index.js at 1.0.1). Pi drops a name
# it does not know without a word, so a profile's `tools` is checked here.
PI_TOOLS = ("read", "bash", "edit", "write", "grep", "find", "ls", "powershell")
# The tool set a profile gets when it names none: the implementation phases
# write the tree, review and EPIC maintenance read it. `bash` stays in both
# (an agent needs `git` and `gh`), so the read set is defence in depth, not a
# write barrier (docs/pi-policy.md §6).
PI_WRITE_PROFILES = ("analyze_execute", "fix", "replan_reexecute")
PI_WRITE_TOOLS = "read,bash,edit,write"
PI_READ_TOOLS = "read,bash"
_PI_TOOLS_RE = re.compile(r"[a-z]+(,[a-z]+)*")
# Provider API keys Pi would use (or hand to its tools) in place of a
# stored ChatGPT sign-in. A profile with `require_oauth` refuses to launch
# when one of them would reach Pi (docs/pi-policy.md §8).
PI_API_KEY_NAMES = ("OPENAI_API_KEY",)
# What Pi loads from its agent directory and from every directory between
# `/` and the cwd whatever the resource flags say (docs/pi-policy.md §4-5):
# the first context file name found in each directory (none with
# `--no-context-files`), and the agent directory's system prompt files
# (always; no flag turns them off). `doctor` reports their presence only.
PI_CONTEXT_FILES = ("AGENTS.override.md", "AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD")
PI_SYSTEM_PROMPT_FILES = ("SYSTEM.md", "APPEND_SYSTEM.md")
PI_DEFAULT_AGENT_DIR = "~/.pi/agent"
# The per-launch `pi auth check` reads one file and prints one line.
PI_AUTH_CHECK_SECONDS = 60
PI_AUTH_CHECK_MAX_OUTPUT_BYTES = 64 * 1024

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

    This class carries Pi's static surface (profile validation, the argv,
    the environment names, the read-only ``doctor`` probes) and the driver
    that couples the conversation in :mod:`autoforge.pi_rpc` to the duplex
    child handle. The driver owns time and the process; the reducer owns
    every Pi command and event name.
    """

    name = "pi"
    # Pi's own process settings (docs/environment-variables.md at v1.0.0):
    # where its config and credential store live, and the network,
    # version-check and telemetry switches (docs/pi-policy.md §8). Explicit
    # names, never a prefix: Pi exports `PI_SESSION_*`, `PI_PROVIDER`,
    # `PI_MODEL` to its tool children, and a provider API key
    # (`OPENAI_API_KEY`, ...) reaches Pi only through
    # `execution.env_allowlist_extra`, never by default.
    environment_names = (
        "PI_CODING_AGENT_DIR",
        "PI_OFFLINE",
        "PI_SKIP_VERSION_CHECK",
        "PI_TELEMETRY",
    )
    option_keys = ("require_oauth", "tools", "context_files")

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
        for key in ("require_oauth", "context_files"):
            if profile.options.get(key, "true") not in ("true", "false"):
                raise ConfigurationError(
                    f"{where}: pi option {key} must be true or false, got {profile.options[key]!r}"
                )
        if "tools" in profile.options:
            problem = _pi_tools_problem(profile.options["tools"])
            if problem:
                raise ConfigurationError(
                    f"{where}: pi option tools must be a comma-separated list of Pi's "
                    f"built-in tools ({', '.join(PI_TOOLS)}): {problem}, "
                    f"got {profile.options['tools']!r}"
                )

    def build_command_for(self, profile: ProfileConfig, prompt: str) -> list[str]:
        # The prompt is deliberately absent: it reaches Pi as a JSON `prompt`
        # record on stdin (ADR 0003 §2.7), never in argv. Every flag before
        # `--tools` is unconditional; no option and no `extra_args` (which a
        # Pi profile cannot have) removes one (docs/pi-policy.md §1-3).
        command = [
            self.command(profile),
            "--mode",
            "rpc",
            "--no-session",
            # Project trust off, whatever `trust.json` saved for the path:
            # without it a trusted project's `.pi/SYSTEM.md` and packages load
            # even with every resource flag below.
            "--no-approve",
            # No executable resource from the project, the agent dir or Pi's
            # built-in extensions (native MCP included).
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            # No package install or update at startup (npm or git, from the
            # global settings), no catalog refresh; model calls still go out.
            "--offline",
            "--tools",
            self.tools(profile),
        ]
        if not self.context_files(profile):
            command.append("--no-context-files")
        return [*command, "--model", profile.model, "--thinking", profile.effort]

    def __init__(
        self,
        runner: Runner | None = None,
        *,
        round_trip_seconds: float = PI_ROUND_TRIP_SECONDS,
        abort_seconds: float = PI_ABORT_SECONDS,
        max_text_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        super().__init__(runner)
        self.round_trip_seconds = round_trip_seconds
        self.abort_seconds = abort_seconds
        self.max_text_bytes = max_text_bytes

    def execute(self, req: AgentRequest) -> AgentExecutionResult:
        """One ``pi --mode rpc`` child, one prompt, one final text (ADR 0003).

        The child runs under the request's idle limit and ceiling, which
        the duplex handle turns into one deadline that Pi's output keeps
        moving (#193). When a limit falls due with the conversation still
        running, and not before, it is cut short: the deadline is pinned
        (so what Pi writes while it settles cannot stretch it), ``abort``
        is sent, stdin is closed, and the duplex handle kills the group if
        Pi has not exited ``abort_seconds`` after the limit. The result is
        then ``timed_out`` whatever Pi's exit status, and names the limit
        that fell due. A failure inside the protocol is
        ``provider_failure``; its abort runs under the deadline pinned when
        the failure is acted on. ``exit_code`` is always the real process
        status. A spawn failure raises :class:`ExecutionError`.

        With ``require_oauth`` (the default) the launch is refused before
        anything is spawned when a provider API key would reach Pi, and a
        ``pi auth check`` in the same environment must report a ChatGPT
        sign-in first; a refused check is a ``provider_failure`` naming it,
        and Pi is never started (docs/pi-policy.md §8).
        """
        allowlist = self.environment_allowlist(req)
        if self.require_oauth(req.profile):
            self._refuse_api_keys(req, allowlist)
            refused = self._auth_preflight(req, allowlist)
            if refused is not None:
                return refused
        emit = guarded(req.progress)
        monitor = _loop_monitor(req, emit)
        conversation = PiConversation(
            model=req.profile.model,
            thinking=req.profile.effort,
            prompt=req.prompt,
            round_trip_seconds=self.round_trip_seconds,
            emit=emit,
            cwd=req.cwd,
            loop=monitor,
        )
        duplex = DuplexRequest(
            command=self.build_command(req),
            cwd=req.cwd,
            env_allowlist=allowlist,
            idle_timeout_seconds=req.idle_timeout_seconds,
            max_runtime_seconds=req.max_runtime_seconds,
            # The abort comes after a limit, so the kill waits for it.
            wind_down_seconds=self.abort_seconds,
            max_record_bytes=PI_MAX_RECORD_BYTES,
            max_pending_bytes=PI_MAX_PENDING_BYTES,
            # Pi's bash tool starts every command detached (ADR 0003 §6).
            contain_orphans=True,
        )
        with start_duplex(duplex) as child:
            cut_short = self._converse(child, conversation, monitor)
            # The limit that fell due when the conversation was cut short:
            # pinned by then, so Pi's own exit inside the abort keeps it.
            cut_limit = child.deadline_limit if cut_short else ""
            if cut_short:
                conversation.deadline_reached()
            if conversation.failure is not None:
                # Every abort ends by a fixed deadline: what Pi writes while
                # it settles, or after, cannot stretch its shutdown.
                child.pin_deadline()
                self._abort(child, conversation, self.abort_seconds)
            child.close_stdin()
            res = child.finish()
        timed_out = cut_short or res.timed_out or res.stopped
        failure = conversation.failure
        if conversation.exited_early:
            failure = f"{failure} (exit {res.exit_code})"
        elif res.records_overflowed and failure is None:
            failure = "pi: protocol violation: stdout records arrived faster than they were read"
        summary = conversation.summary()
        if failure is not None:
            summary["failure"] = failure
        text = conversation.text if failure is None else None
        stdout, truncated = _keep_tail(encodable(text or ""), self.max_text_bytes)
        result = AgentExecutionResult(
            command=list(res.command),
            exit_code=res.exit_code,
            stdout=stdout,
            stderr=res.stderr,
            started_at=res.started_at,
            finished_at=res.finished_at,
            timed_out=timed_out,
            provider=req.profile.provider,
            model=req.profile.model,
            effort=req.profile.effort,
            stdout_truncated=truncated,
            stderr_truncated=res.stderr_truncated,
            stdout_tail_offset=0,
            descendants_killed=res.descendants_killed,
            group_survived_kill=res.group_survived_kill,
            capture_abandoned=res.capture_abandoned,
            orphans_killed=res.orphans_killed,
            orphan_survived_kill=res.orphan_survived_kill,
            orphans_unchecked=res.orphans_unchecked,
            # A timeout wins over whatever the protocol said (ADR 0003 §2.6).
            provider_failure=None if timed_out else failure,
            provider_summary=summary,
            timeout_limit=(res.timeout_limit or cut_limit) if timed_out else "",
            last_activity_at=res.last_activity_at,
        )
        return _finish_loop(result, monitor, killed=res.stopped)

    def _refuse_api_keys(self, req: AgentRequest, allowlist: tuple[str, ...] | None) -> None:
        """Raise :class:`ExecutionError` when a set provider API key would reach Pi."""
        problem = self.api_key_problem(req.profile, allowlist)
        if problem:
            raise ExecutionError(problem)

    @staticmethod
    def api_key_problem(profile: ProfileConfig, allowlist: tuple[str, ...] | None) -> str:
        """Why ``profile`` may not launch with ``allowlist``'s API keys, or ''.

        A profile with ``require_oauth`` must not hand Pi (and through it
        every command Pi's `bash` tool runs) a provider API key: Pi would
        prefer a stored sign-in, so the key would sit in the tools'
        environment for nothing, or bill the run when the sign-in lapses.
        Only the names are checked and named, never a value.
        """
        if not PiProvider.require_oauth(profile):
            return ""
        env = os.environ if allowlist is None else select_environment(allowlist)
        present = [name for name in PI_API_KEY_NAMES if env.get(name)]
        if not present:
            return ""
        return (
            f"profile {profile.name!r} requires Pi's ChatGPT sign-in (options.require_oauth), "
            f"but {', '.join(present)} is set and would reach Pi through the environment "
            "allow-list; remove it from execution.env_allowlist_extra (or unset it), or set "
            "options.require_oauth: false to run on an API key"
        )

    def _auth_preflight(
        self, req: AgentRequest, allowlist: tuple[str, ...] | None
    ) -> AgentExecutionResult | None:
        """Run ``pi auth check`` as the launch would; None when it allows the launch.

        Read-only (``--no-refresh``), in the cwd and environment the launch
        uses, judged by :meth:`auth_verdict`: ``ready`` with authType
        ``oauth`` for the configured provider. A refusal is returned as a
        result whose ``provider_failure`` quotes only the check's fields;
        its output is never kept.
        """
        res = self._runner(
            ExecutionRequest(
                command=self.auth_check_command(req.profile),
                cwd=req.cwd,
                timeout_seconds=PI_AUTH_CHECK_SECONDS,
                max_output_bytes=PI_AUTH_CHECK_MAX_OUTPUT_BYTES,
                env_allowlist=allowlist,
                contain_orphans=True,
            )
        )
        if res.timed_out:
            detail = f"`pi auth check` did not answer within {PI_AUTH_CHECK_SECONDS}s"
        elif res.stdout_truncated:
            detail = "`pi auth check` printed more than a result"
        else:
            ok, detail = self.auth_verdict(req.profile, res.exit_code, res.stdout)
            if ok:
                return None
        return AgentExecutionResult(
            command=list(res.command),
            exit_code=res.exit_code,
            stdout="",
            stderr="",
            started_at=res.started_at,
            finished_at=res.finished_at,
            provider=req.profile.provider,
            model=req.profile.model,
            effort=req.profile.effort,
            descendants_killed=res.descendants_killed,
            group_survived_kill=res.group_survived_kill,
            capture_abandoned=res.capture_abandoned,
            orphans_killed=res.orphans_killed,
            orphan_survived_kill=res.orphan_survived_kill,
            orphans_unchecked=res.orphans_unchecked,
            provider_failure=f"pi: the auth preflight refused the launch: {detail}",
            provider_summary={"auth_preflight": "refused"},
        )

    @staticmethod
    def _send(child: DuplexChild, conversation: PiConversation, lines: list[bytes]) -> None:
        for line in lines:
            try:
                child.send_line(line)
            except ChildStdinClosedError:
                conversation.stdin_closed()
                return

    def _converse(
        self,
        child: DuplexChild,
        conversation: PiConversation,
        loop: LoopMonitor | None = None,
    ) -> bool:
        """Drive the conversation to its outcome; True when a limit cut it short.

        The conversation is cut short as the child's limit falls due, which
        Pi's output keeps moving, so it is recomputed on every turn; once
        cut short the deadline is pinned, the abort window after the limit.
        A verdict of ``loop`` (#194) stops a Pi still running at once, with
        no ``abort``: the run is discarded, as on a timeout, and the caller's
        ``finish()`` reports ``stopped``.
        """
        try:
            self._send(child, conversation, conversation.start(time.monotonic()))
            while not conversation.done:
                now = time.monotonic()
                due = child.limit_due()
                if now >= due:
                    child.pin_deadline()
                    return True
                conversation.tick(now)
                if conversation.done:
                    break
                wake = conversation.next_wakeup
                until = due if wake is None else min(due, wake)
                item = child.read_line(timeout=max(0.0, until - now))
                if isinstance(item, Record):
                    replies = conversation.feed(item.data, time.monotonic())
                    if loop is not None and loop.verdict is not None and child.stop():
                        return False
                    self._send(child, conversation, replies)
                elif isinstance(item, Oversize):
                    conversation.framing_error(f"a stdout record exceeded {item.limit} bytes")
                elif isinstance(item, Fragment):
                    conversation.framing_error("stdout ended inside an unterminated record")
                elif isinstance(item, Overflow):
                    conversation.framing_error("stdout records arrived faster than they were read")
                elif isinstance(item, Eof):
                    conversation.stream_ended()
                elif item.deadline_exceeded:
                    return True
        except ExecutionTimeoutError:
            return True  # a write overran the deadline; the group is gone
        return False

    def _abort(self, child: DuplexChild, conversation: PiConversation, window: float) -> None:
        """Ask Pi to abort a running prompt and give it ``window`` seconds to settle.

        Never SIGINT: Pi has no SIGINT handler in RPC mode (ADR 0003 §2.8).
        Dialogs that arrive meanwhile are still answered.
        """
        end = time.monotonic() + window
        try:
            self._send(child, conversation, conversation.begin_abort(time.monotonic()))
            while not conversation.abort_settled:
                now = time.monotonic()
                if now >= end:
                    return
                item = child.read_line(timeout=end - now)
                if isinstance(item, Record):
                    replies = conversation.feed(item.data, time.monotonic())
                    self._send(child, conversation, replies)
                elif isinstance(item, Eof | Overflow):
                    return
                elif isinstance(item, Timeout) and item.deadline_exceeded:
                    return
        except ExecutionTimeoutError:
            return

    # -- read-only probes for `autoforge doctor` ---------------------------
    @staticmethod
    def command(profile: ProfileConfig) -> str:
        return profile.command or "pi"

    @staticmethod
    def require_oauth(profile: ProfileConfig) -> bool:
        return profile.options.get("require_oauth", "true") != "false"

    @staticmethod
    def context_files(profile: ProfileConfig) -> bool:
        return profile.options.get("context_files", "true") != "false"

    @staticmethod
    def tools(profile: ProfileConfig) -> str:
        """The ``--tools`` list: the profile's ``tools``, or its phase's default."""
        configured = profile.options.get("tools")
        if configured:
            return configured
        return PI_WRITE_TOOLS if profile.name in PI_WRITE_PROFILES else PI_READ_TOOLS

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


def _keep_tail(text: str, limit: int) -> tuple[str, bool]:
    """``text``, or its last ``limit`` UTF-8 bytes and True when it is longer.

    A character the cut splits decodes to U+FFFD, as the executor's capture
    does at its bound.
    """
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text, False
    return data[-limit:].decode("utf-8", errors="replace"), True


def _pi_tools_problem(value: str) -> str:
    """Why ``value`` is not a valid ``tools`` option, or ''."""
    if not _PI_TOOLS_RE.fullmatch(value):
        return "expected names separated by ',' with no spaces and no empty entry"
    names = value.split(",")
    unknown = [name for name in names if name not in PI_TOOLS]
    if unknown:
        return f"unknown tool(s) {', '.join(unknown)}"
    if len(set(names)) != len(names):
        return "a tool is listed twice"
    return ""


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
