"""Agent provider adapters: the only place that knows real CLI flags.

The engine speaks :class:`AgentRequest` / :class:`AgentExecutionResult`;
each provider translates a logical execution profile (provider, model,
effort, timeout, options) into a concrete non-interactive CLI invocation.

Verified against locally installed CLIs (2026-09):

Claude Code (``claude 2.1.x``; the stream run against 2.1.291 to 2.1.293)::

    claude -p --output-format stream-json --verbose --model <alias|id>
           --effort <low|medium|high|xhigh|max> --permission-mode <mode>
           --no-session-persistence --safe-mode --strict-mcp-config
           [extra_args] -- <prompt>

  * ``-p/--print`` = non-interactive. With ``stream-json`` (the default,
    #192) stdout is one JSON record per line while the agent runs
    (``system`` init / thinking_tokens / api_retry, ``assistant`` content
    blocks, ``user`` tool results, ``rate_limit_event``) and a ``result``
    record that ends the turn, whose ``result`` field is its final
    assistant text; a background task or a scheduled wakeup still pending
    then keeps the CLI alive for another turn with its own ``result``
    (held back to the end, in turn order, while a background agent runs, on
    2.1.293; sent at once on 2.1.326, whose background agent then goes on
    writing records of its own, #507). 2.1.326 also stops background tasks
    ten minutes after the last turn unless the launch turns that off, which
    every launch does (:data:`CLAUDE_LIFECYCLE_ENV`).
    The CLI refuses ``stream-json`` under ``--print`` without ``--verbose``.
    The child runs under the duplex handle and the stream is reduced in
    claude_stream.py: ``stdout`` of the result is the last ``result``
    text verbatim (so the CONTROL_RESULT block survives untouched), the
    other records become live progress (:mod:`autoforge.progress`), and an
    error result in any turn, a result outside a turn, a malformed line or
    a turn without its result is a
    :attr:`AgentExecutionResult.provider_failure`; a CLI still running
    :data:`CLAUDE_FAILED_EXIT_GRACE_SECONDS` after it is stopped (#394). So
    is one still running :data:`CLAUDE_REPLIED_EXIT_GRACE_SECONDS` after a
    reply holding a CONTROL_RESULT block with no turn or tool call left open,
    and that reply stands (:attr:`AgentExecutionResult.stopped_on_reply`, #439).
    The agent is told to leave nothing pending (#440,
    ``prompts/claude_background_work.md``).
  * ``options.output_format: text`` is the opt-out: the final text is
    printed once, at exit, and the run reports no progress and no activity
    while it runs.
  * ``--permission-mode bypassPermissions`` is required for unattended
    edits and local git; ``--permission-prompts none`` would silently
    deny anything that prompts. Configure via profile ``options``.
  * nothing asks a person (#258; run on 2.1.312 against a loopback
    stand-in of the Messages API, so no model was called): a tool call the
    profile's mode does not allow, or that a deny rule of the user settings
    names, is refused at once and the agent goes on without it (exit 0),
    and ``AskUserQuestion`` is not offered under ``-p``. The stream says
    which (claude_stream.py); the adapter names them in a run that fails
    anyway and in the run log (:func:`_name_refused`). ``text`` output
    carries none of it.
  * ``--safe-mode`` and ``--strict-mcp-config`` (#165): no hook, installed
    plugin or MCP server of the user's, the project's or a plugin's
    configuration runs, so none can hand the agent a GitHub token of its
    own; skills, custom agents and commands and CLAUDE.md auto-discovery
    are off too (every phase prompt tells the agent to read ``AGENTS.md`` /
    ``CLAUDE.md``). Checked against 2.1.296 with marker hooks and MCP
    servers and no login (they run before the model is asked): user and
    project settings, ``.mcp.json``, an installed marketplace plugin,
    ``--settings``, ``--plugin-dir`` and ``--mcp-config`` all ran without
    the flag and none ran with it, ``CLAUDE_CODE_SAFE_MODE=0`` set or not;
    a real ``stream-json`` run is unchanged by it (the ``init`` record still
    names the enabled plugins). Auth, model, tools and permission mode work
    as before; managed (administrator) settings still apply.
    ``--setting-sources=user``: no project or local settings, whose ``env``
    the repository could use to undo the Bash tool's isolation (below).
    :data:`CLAUDE_INTEGRATION_FLAGS` in ``extra_args`` are refused, and
    ``autoforge doctor`` requires all three in ``claude --help``.
  * the Bash tool runs no function or alias of a shell startup file (#165):
    every launch sets ``CLAUDE_ENV_FILE`` to a file of its own holding
    :data:`CLAUDE_SHELL_CLEANER`, ``CLAUDE_CODE_SHELL`` to a bash or zsh
    and ``CLAUDE_CODE_SHELL_PREFIX`` to empty
    (:meth:`ClaudeCodeProvider.integration_isolation`), and is refused while
    the ``env`` of the user's settings or of the global configuration would
    set a variable over them or over the controller's
    (:func:`claude_configuration_problems`).
  * a Bash tool command that runs in a login shell (without a snapshot)
    gets back the ``PATH`` of the launch, which ``/etc/profile`` may set
    outright: the same file sets it there (:func:`claude_login_path`, #347).
  * exit code 0 on success; 1 on model/auth errors (message on stdout).
  * a turn that ends on a model API error once the CLI's own retries are
    spent (#395; run on 2.1.314 against a loopback stand-in of the Messages
    API) is an error ``result`` with ``terminal_reason: api_error`` and the
    HTTP status as ``api_error_status``. With a status of 408 or 5xx (529 is
    overloaded) the failure is also a
    :attr:`AgentExecutionResult.transient_failure` (``claude: API error
    <status>``); any other 4xx, 429 included, an error without a status (a
    refused or dropped connection, the CLI's own request timeout) and any
    other failure are not (claude_stream.py says why). A 429 is typed as the
    provider's usage or rate limit instead (#434):
    :attr:`AgentExecutionResult.usage_limit` (``claude: API error 429``), which
    the engine never relaunches, whatever ended the CLI after it (a limit
    included). A CLI that exited on its own inside a running session (its
    ``system/init`` and a tool call of the model's seen) before a single
    result record, with no line lost, is a
    :attr:`AgentExecutionResult.result_less_exit`; an empty stream is not.
    A stream that had failed the run before a limit killed the CLI (a turn
    ended on a model API error, 401 or 403 included, an error result, a
    protocol violation) keeps that failure's fixed kind as
    :attr:`AgentExecutionResult.failed_before_limit`, never read as the
    limit's (#434). The CLI's own typed message for a model API error
    (an ``assistant`` record with ``is_api_error_message`` and
    ``api_error_status``) counts without the ``result`` that follows it,
    which a CLI killed or exiting first never writes (R3-F1 of PR #480): its
    429 is the usage limit, any status is the failure before a limit, and
    after one no exit is result-less.
    Whether to relaunch is the engine's policy. A relaunched CLI begins with its own retries, so the
    relaunch itself waits through another retry schedule before giving up.
    ``text`` output is not classified.

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
  * a run-level error prints ``ESC[91mESC[1mError: ESC[0m<message>`` on
    stderr (whether or not it is a terminal) and sets exit 1, even when the
    session goes on after it and finishes its turn; a tool's error
    is the same line right after the tool's ``ESC[0m✗ ESC[0m<title> failed``
    line and fails nothing (read from the 2.0.26 build). An exit 1 of an
    ``openai/*`` model whose run-level errors are all on a short list, each
    compared as a whole message (:data:`_OPENCODE_OPENAI_TRANSIENT_ERRORS`),
    is reported as a :attr:`AgentExecutionResult.transient_failure`:
    ``WebSocket closed with code 1000`` (the socket to the model closed
    under a running turn, #164), ``WebSocket closed with code 1008`` and
    OpenAI's generic ``server_error: An error occurred while processing your
    request. You can retry your request, ...`` (#226). OpenAI documents no
    close code for its Responses WebSocket (its 60-minute connection limit
    is an error event, not a code), and 1008 is RFC 6455's generic policy
    code; 1008 is listed because OpenCode's own retry rule (read from the
    2.0.32 build) retries a socket closed with any code but 1009 (message
    too big) as it retries a ``server_error``, and each of the two runs of
    #226 reconnected after one and finished its turn. The ``server_error``
    message is compared whole, not by its ``server_error: `` prefix: another
    text under that code (one naming a request ID, say) is not the one that
    says to retry, and stays a plain failed exit. A transient exit stays a
    failed exit too (a CONTROL_RESULT it printed is not read), and whether
    to relaunch is the engine's policy. Text anywhere else in the trace (a
    diff, a file, a command's output) is not an error line, and a truncated
    ``stderr`` or a timeout is never classified. Not visible to this check:
    an error the CLI does not print (it prints one execution failure only
    when no step failed before it) and an exit 1 without any error line (a
    cancelled permission prompt, below); both are failed exits all the same.
    A failed or timed-out run whose run-level errors include OpenCode's
    usage-limit error, compared whole (:data:`_OPENCODE_USAGE_LIMIT_ERROR`,
    whatever the model), is reported as a
    :attr:`AgentExecutionResult.usage_limit` and never as transient (#434).
    A timed-out run whose stderr holds any run-level error had failed before
    the limit killed it: :attr:`AgentExecutionResult.failed_before_limit`
    (#434; the error's message is not read for it).
  * bash/edit tools run without ``--auto`` under the default agent;
    ``--auto`` is opt-in via ``options.auto_approve: true``.
  * a request of the agent's is refused, never put to a person (#258; run
    on 2.1.4 against a loopback stand-in of the Messages API, so no model
    was called, and read from its build). Without ``--auto``, a permission
    a rule asks for (with the empty configuration below, a read of ``.env``
    asked; a read outside the worktree did not) prints
    ``ESC[93mESC[1m! ESC[0mpermission requested: <permission> (<patterns>);
    auto-rejecting`` and is rejected. The ``question`` tool is offered, and
    a question is cancelled: its failed line, then the error line
    :data:`_OPENCODE_QUESTION_CANCELLED`, with no question text. Either way
    the model is told to go on without it, and the run goes on and exits 0.
    2.0.26 was read, not run, to exit 1 with no error line after a cancelled
    permission prompt; whether it printed the line above is not recorded.
    So an exit 1 with no run-level error after such a line is that refusal,
    reported as ADR 0007 D7.6's failure; any other failed run only has the
    refusals added to its text (:func:`_name_refused`).
  * ``opencode --version`` prints ``opencode v2.0.23``; 1.x printed the
    bare version and rejects this argv, so ``autoforge doctor`` requires
    :data:`OPENCODE_MIN_VERSION`.
  * no flag disables the MCP servers or plugins of the user's or the
    project's ``opencode.json`` (2.0.26 has neither ``--pure`` nor an MCP
    switch), so every launch sets :func:`opencode_isolation_env` instead
    (#165): an empty global configuration directory of its own, no project
    configuration, no explicit file and no inline document. Read from the
    2.0.26 build and checked against it with ``opencode api --standalone
    GET /api/config`` (the configuration a launch resolves): without these
    variables the global document and ``plugin/`` directory, the project's,
    and an ``opencode.json`` two directories above the worktree were all
    loaded; with them, only the empty directory, the operator's
    ``OPENCODE_CONFIG``, ``OPENCODE_CONFIG_CONTENT`` and
    ``OPENCODE_CONFIG_PROJECT_DISABLE=0`` notwithstanding. The settings in
    those files (custom providers, permissions, agents, instructions) are
    therefore not read either; the model credentials are.

Pi (``pi 1.0.x``, ADR 0003; read from the v1.0.0 source and docs, not yet
run against an installed Pi)::

    pi --mode rpc --no-session --model <pi-provider>/<model-id> --thinking <effort>

  * the prompt never travels in argv: it is one JSON ``prompt`` record on
    stdin. The child runs under the duplex handle (executor_duplex.py) and
    the conversation (``get_state``, ``get_available_models``, ``prompt``,
    events until ``agent_settled``, ``get_last_assistant_text``) is the
    reducer in pi_rpc.py; ``stdout`` of the result is the final assistant
    text, never a protocol record, and an in-protocol failure is reported
    in :attr:`AgentExecutionResult.provider_failure`. One a limit then cut
    short (inside its abort, say) is reported as
    :attr:`AgentExecutionResult.failed_before_limit` (#434).
  * ``--thinking`` takes ``off|minimal|low|medium|high|xhigh|max``; the model
    is ``provider/id`` with no ``:<thinking>`` suffix (ADR 0003 §2.2, §2.3).
  * no ``extra_args``: every flag that matters is the adapter's or #132's
    policy's, and a pass-through could undo it (``--approve``,
    ``--api-key``, ``--session-id``, ``--system-prompt``, ``--mode``, ...).
  * ``pi --version`` prints the bare version; ``pi auth check --model <m>
    --json --no-refresh`` reports, read-only, whether Pi holds a credential
    for the model's provider (``autoforge doctor``).
  * nothing asks (#258; read from the 1.3.0 build and docs): Pi has no
    permission rule and no question tool, and under ``--no-extensions`` and
    ``--no-approve`` no dialog opens. An extension dialog would be
    cancelled and fail the run, naming its method (pi_rpc.py).

The CLIs are launched with cwd = the directory the engine chose (the
run's per-issue worktree), stdin = /dev/null or the adapter's payload
(:meth:`AgentProvider.stdin_payload`), an idle limit and an optional
wall-clock ceiling (:class:`AgentRequest`; #193), and an allow-listed
environment (see executor.py): the engine passes the configured allow-list
in :attr:`AgentRequest.env_allowlist` and each adapter adds the variables
its own CLI reads (:attr:`AgentProvider.environment_names`), so the key of
one provider is never handed to another. Neither list may name a GitHub or
git credential route (:func:`autoforge.child_env.refuse_forbidden_env`), a
name listed for repository commands alone may not be one an adapter's own
names pass (:meth:`autoforge.config.ExecutionConfig.environment_overlap`),
and the variables the controller sets for the launch
(:attr:`AgentRequest.env`, #165) reach every process the adapter starts,
with the adapter's own (:meth:`AgentProvider.integration_isolation`) over
them where its CLI has no flag to keep an integration, or a shell startup
file's functions and aliases, from loading. An integration or a setting
neither can keep off refuses the launch before it starts
(:meth:`AgentProvider.integration_preflight`).

The SDK providers ``claude-sdk`` and ``opencode-sdk`` (ADR 0007) have their
static surface here: names, option keys and validation, the launch
allow-list, the sandbox declaration, the helper's argv and a dry run's
plan notes, and the commands ``doctor``'s read-only version probes run
(#150; the OpenCode runtime is never run, its version is read from its
package). Their policy (tools, permissions, setting sources, configuration
and session roots, authentication, the environment) is
:mod:`autoforge.sdk_policy`'s (#149, docs/sdk-policy.md); their helper
programs and pinned pairs are recorded in the packaged manifest
(:mod:`autoforge.sdk_assets`).

Claude Agent SDK (``claude-agent-sdk`` 0.2.163 with its bundled Claude Code
2.1.286, #151; the gate probes ran that pair against a loopback stand-in of
the Messages API, ADR 0007 §3's amendment)::

    <helper environment's python> -I <per-launch dir>/claude_helper.py
                                                (one JSON record per line on stdin/stdout)

  * the helper (``sdk_helpers/claude_helper.py``, package data, staged per
    launch after its sha256 is checked) runs ``ClaudeSDKClient``: one
    ``connect``, one ``query`` with the prompt, the response to its first
    result, ``interrupt`` on a cancellation or stdin EOF, ``disconnect``.
    The envelope and its reducer are :mod:`autoforge.claude_sdk`; ``stdout``
    of the result is the completed main turn's ``result`` text only.
  * every SDK option is a field of the policy: no ``extra_args``, no setting
    source, no MCP server or plugin, the bundled CLI as ``cli_path``, a
    caller UUID as ``session_id``, the ``PreToolUse`` hook on every call and
    a permission host that denies, ``verbatim_prompts``. The launch gets
    ``ANTHROPIC_API_KEY`` and no other credential, the issue's configuration
    and session root as ``CLAUDE_CONFIG_DIR`` and #165's shell isolation.
  * an ``AskUserQuestion`` call or an ``ask_tools`` call is put to the
    launch's broker (``interaction_policy: prompt``); its decision reaches
    the model through the hook's answer.

``opencode-sdk``'s bridge and transport are #152's, and until they land
every launch of it is refused before anything starts.

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

import dataclasses
import enum
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .child_env import (
    PYTHON_STARTUP_NAMES,
    child_shell,
    refuse_forbidden_env,
    undoes_isolation,
)
from .claude_sdk import (
    Ask,
    ClaudeSdkSession,
    answer_input,
    ask_questions,
    modified_input_problem,
    start_record,
)
from .claude_stream import ClaudeStream
from .config import LoopDetectionConfig, ProfileConfig
from .container_auth import (
    CredentialKind,
    CredentialRoutes,
    EnvRoute,
    PrivateStore,
    Refresh,
    UnsupportedRoute,
    private_path,
)
from .errors import (
    AutoForgeError,
    ChildStdinClosedError,
    ConfigurationError,
    ExecutionError,
    ExecutionTimeoutError,
    InteractionDataError,
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
    DuplexResult,
    Eof,
    Fragment,
    Overflow,
    Oversize,
    Record,
    Timeout,
    start_duplex,
)
from .interaction import (
    NO_CAPABILITIES,
    Allow,
    Answer,
    Capabilities,
    DecisionKind,
    Deliver,
    Deny,
    Fail,
    InteractionBroker,
    InteractionOutcome,
    Lifecycle,
    RequestKind,
)
from .loop_detect import LIMIT_LOOP, LoopMonitor, LoopReport
from .pi_rpc import PiConversation, encodable
from .progress import ProgressEvent, ProgressKind, ProgressSink, clean, guarded
from .redaction import redact
from .result_parser import holds_control_result
from .sandbox import LaunchDescription, SandboxFacts
from .sandbox_policy import (
    AuthItem,
    AuthMode,
    ProbeSandbox,
    ProviderRequirements,
    SandboxPlan,
    declared_writable,
    executable_requirements,
    resolve_command,
)
from .sdk_assets import (
    CLAUDE_ENVIRONMENT_PROBE,
    CLAUDE_ENVIRONMENT_PROBE_FLAGS,
    HelperPin,
    helper_pin,
    helper_problem,
    helpers_dir,
)
from .sdk_policy import (
    CLAUDE_SDK,
    CLAUDE_SDK_CREDENTIAL,
    CLAUDE_SDK_OPTION_KEYS,
    CLAUDE_SDK_PASSTHROUGH,
    OPENCODE_SDK,
    OPENCODE_SDK_OPTION_KEYS,
    SDK_ROOT_DIR,
    ClaudeSdkPolicy,
    ClaudeSdkRoots,
    InteractionSettings,
    OpenCodeSdkPolicy,
    OpenCodeSdkRoots,
    claude_sdk_auth_problems,
    claude_sdk_policy,
    collection_text,
    interaction_settings,
    opencode_model_problem,
    opencode_sdk_policy,
    permission_summary,
    sdk_launch_allowlist,
)

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
    # The controller's allow-list for an agent launch
    # (``ExecutionConfig.agent_environment_names``); the provider adds its own
    # names. ``None`` inherits the whole environment and is never what the
    # engine sends.
    env_allowlist: tuple[str, ...] | None = None
    # What the controller sets over the allow-listed environment for this
    # launch (#165, :func:`autoforge.child_env.isolated_overrides`): an empty
    # gh configuration, a placeholder token, the git configuration that
    # disables every credential route, the author identity. Every adapter
    # passes it to every process it starts for the launch, a preflight
    # included; ``None`` sets nothing.
    env: dict[str, str] | None = None
    # Where the adapter reports live progress (#192): provider-neutral,
    # already bounded and redacted events, never a raw provider record.
    # ``None`` reports nothing. A sink that raises is dropped for the rest of
    # the invocation (:func:`autoforge.progress.guarded`); progress never
    # changes an outcome.
    progress: ProgressSink | None = None
    # How the adapter watches the invocation for a loop (#194): the engine
    # passes ``execution.loop_detection``, and the request keeps it with the
    # mode its own provider and phase run under (#199).
    loop_detection: LoopDetectionConfig = field(default_factory=LoopDetectionConfig)
    # The outer sandbox every process of the launch runs in (ADR 0005, #168):
    # the engine's facts, to which the adapter adds its CLI's declaration
    # (:meth:`AgentProvider.sandbox_requirements`). ``None`` launches as
    # before the sandbox existed (``sandbox.mode: disabled``).
    sandbox: SandboxPlan | None = None
    # Where the adapter takes the agent's requests for a decision (ADR 0007
    # D5.2, #134): the launch's broker, created by the engine for a provider
    # whose ``interaction_settings`` names a policy, as it hands over
    # ``progress``. The adapter mints each provider ask through it and does
    # what the directive says; it never sees the policy. ``None`` (every
    # provider through which nothing can ask) keeps today's behaviour.
    interaction: InteractionBroker | None = None
    # The issue's controller-owned sandbox directory
    # (``<git common dir>/autoforge/sandbox/<worktree>``), under which the
    # engine created the directories :meth:`AgentProvider.state_directories`
    # names for this provider, persistent and per issue, before the launch
    # (#151); a sandboxed launch has each projected writable as an engine
    # carve-out. "" for a provider that names none.
    state_dir: str = ""
    # The launch must start with a fresh context (ADR 0015 D14): no
    # conversation of an earlier launch and no provider-side memory written by
    # one. The engine sets it for a phase Issue Memory projects into while the
    # run's Memory setting is on (#317); each adapter keeps what its proof
    # rests on (:meth:`AgentProvider.fresh_context_environment`). ``False``
    # launches exactly as before Memory existed.
    fresh_context: bool = False
    # The runtime the engine established, before the launch-intent save,
    # that such a launch may start (:meth:`AgentProvider.fresh_context_probe`;
    # R4-F1 of PR #513): the adapter starts exactly that executable, or
    # refuses. ``None`` for an adapter whose proof rests on its argv alone.
    fresh_context_runtime: FreshContextRuntime | None = None

    def __post_init__(self) -> None:
        self.loop_detection = self.loop_detection.for_invocation(self.profile.provider, self.phase)


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
    # only when the CLI exited 0 without reading the whole prompt from stdin,
    # or when a run that failed anyway had a request of the agent's refused
    # by the CLI, which it names (#258, :func:`_name_refused`).
    provider_failure: str | None = None
    # The adapter stopped a CLI still running after its protocol had already
    # failed the run (#394): ``provider_failure`` names that failure and the
    # stop, ``exit_code`` is -1, and nothing timed out.
    stopped_on_failure: bool = False
    # The adapter stopped a CLI still running after a reply that decided the
    # run (#439): ``stdout`` is that reply, read as any other, ``exit_code`` is
    # -1 and says only that it was stopped (:attr:`failed_exit`), and nothing
    # timed out.
    stopped_on_reply: bool = False
    # A failed run the adapter recognised as a known transient failure of
    # the provider (#164): a short, fixed reason naming it, never text taken
    # from the output. It is a fact about this failure, not a decision: it
    # never replaces the exit status or ``provider_failure``, and whether to
    # relaunch is the engine's policy, which reads no provider name or error
    # text of its own.
    transient_failure: str | None = None
    # A failed run the adapter recognised as its provider refusing it at a
    # usage or rate limit (#434): a short, fixed reason naming it, never text
    # taken from the output; never set with ``transient_failure``. Kept when a
    # limit then killed the CLI (``timed_out``): the refusal was established
    # before it (R1-F2 of PR #480). So is one the provider typed in a message
    # of its own when the record that would have repeated it never arrived
    # (R3-F1). The engine raises ProviderUsageLimitError for it and relaunches
    # nothing.
    # ``usage_reset_at`` is a reset time the provider reported in a form the
    # adapter trusts (UTC, ISO 8601); no adapter reads one today, so it is "".
    usage_limit: str | None = None
    usage_reset_at: str = ""
    # A failed run the adapter recognised as an exit without any result
    # (#434): the CLI exited on its own inside a session its protocol stream
    # shows was running, before a single result record, with nothing lost
    # from the stream on the way. Never an empty stream, whose cause nothing
    # typed tells (R1-F1 of PR #480), nor one in which the provider had
    # reported a typed error (R3-F1). A fact, not a decision: whether to
    # relaunch is the engine's recovery rule.
    result_less_exit: bool = False
    # A run the provider's protocol had already failed when a limit then killed
    # the CLI (``timed_out``, which drops ``provider_failure``: a timeout wins
    # over what the protocol said, ADR 0003 §2.6): a short, fixed reason naming
    # what failed it (an authentication or permission refusal, a protocol
    # violation, an error result, a model API error the CLI reported in a
    # typed message of its own before the result that would have failed the
    # run, R3-F1), never text taken from the output; ``None`` without a
    # timeout (#434). The failure, established before the kill, ended
    # the run, and a relaunch now may meet it again, so it is never read as the
    # limit's: the engine's recovery rule refuses it (R2-F1 of PR #480).
    failed_before_limit: str | None = None
    # The adapter's protocol shows a completed final reply that ended the run,
    # handed over whole as ``stdout``, however the CLI ended after it (#479):
    # Claude Code's ``stream-json`` held a reply that decides the run
    # (ClaudeStream.replied: every turn opened has its result, the last a
    # success holding a block, no tool call open) when the stream ended, with
    # no line of it skipped past the per-line bound (ClaudeStream.reply_proven,
    # R3-F1 of PR #481) and every tool call it started tracked to its end
    # (R4-F1 of PR #481); Pi's
    # conversation settled and its last assistant text read back, before any
    # limit cut it short, with every tool execution ended and nothing but that
    # text's response on stdout after the run settled, through Pi's shutdown
    # (R1-F3 of PR #481). A fact of the protocol, never of text: an output
    # that types no turn (``text``, OpenCode) never sets it. Whether a launch
    # that a limit killed or that exited with a failing status is completed
    # on that reply is the engine's (``delivered_result.py``, ADR 0012 D12).
    reply_delivered: bool = False
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
    # What the outer sandbox recorded for a sandboxed launch (ADR 0005
    # D10.4), for the run log; ``None`` without one. Never read for a decision.
    sandbox: SandboxFacts | None = None
    # How an invocation through which the agent could ask ended (ADR 0007
    # D10.5, #134): ``completed``, ``failed`` (the adapter also reports
    # ``provider_failure``, and the engine raises InteractionRequiredError,
    # never correcting or relaunching), ``interrupted``, or ``paused`` with
    # the pending request (#126 persists it). ``None`` for a launch without
    # a broker, with one exception: a CLI adapter, which has no broker and
    # declares no capability, reports ``failed`` when its CLI's output shows
    # that a request the CLI refused is what failed the run (#258, D7.6).
    interaction: InteractionOutcome | None = None

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
    def may_have_survivor(self) -> bool:
        """Whether a process the invocation started may still be running: a
        member of its group outlived SIGKILL, a writer beyond the kill's reach
        still held its pipes, or a re-parented process outlived its kill. Not
        ``orphans_unchecked``, which is a fact of the platform (no subreaper)
        and true of every invocation there, not evidence of a survivor."""
        return self.group_survived_kill or self.capture_abandoned or self.orphan_survived_kill

    @property
    def failed_exit(self) -> bool:
        """Whether the exit status says the run failed: any but 0, except the
        -1 of a CLI stopped after its reply (:attr:`stopped_on_reply`)."""
        return self.exit_code != 0 and not self.stopped_on_reply

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
        return not self.timed_out and not self.failed_exit and not self.stdout_truncated

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
            sandbox=res.sandbox,
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


# Where an operator reads what to do about a request the CLI refused (#258).
REFUSED_REQUEST_GUIDE = "docs/usage.md, 'An agent asked for permission or input'"
MAX_REFUSED_SUBJECT_CHARS = 160


def _requests(refused: list[tuple[RequestKind, str]], count: int) -> str:
    """What the agent asked for, for a failure: the first request, and how many more."""
    kind, subject = refused[0]
    if kind is RequestKind.PERMISSION:
        text = f"permission ({subject})" if subject else "permission"
    else:
        text = "an answer to a question"
    if count > 1:
        text += f" and {count - 1} more request{'s' if count > 2 else ''}"
    return text


def _name_refused(
    result: AgentExecutionResult,
    refused: list[tuple[RequestKind, str]],
    count: int,
    name: str,
    cli: str,
    *,
    ended_by_it: str = "",
) -> AgentExecutionResult:
    """Name on ``result`` the ``count`` requests of the agent's the CLI refused (#258).

    ``refused`` is the first of them, each its kind and its cleaned subject;
    ``name`` is the adapter's and ``cli`` the CLI's, as a reader knows it.
    The run log always gets the count and the first one (``provider_summary``).
    A run that ends usable is left as it is: the CLI told the agent it was
    refused, and the agent went on without it. A timeout keeps its own text.

    ``ended_by_it``, the adapter's finding that the refusal is all that
    failed the run (with what to do about it), makes the failure the named
    one of ADR 0007 D7.6: ``provider_failure`` says what the agent asked for
    and ``interaction`` is ``failed``, so the engine raises
    InteractionRequiredError, never corrected and never relaunched: a
    relaunch would ask again. The adapter declares no capability for it
    (D5.3). Any other failure keeps its own reason, its exit status and a
    transient finding, and only has the refusal added to its text: the
    refusal may not be why it failed.
    """
    if not count:
        return result
    kind, subject = refused[0]
    result.provider_summary["refused_requests"] = count
    result.provider_summary["refused_request"] = f"{kind} ({subject})" if subject else str(kind)
    if result.timed_out:
        return result
    asked = _requests(refused, count)
    if ended_by_it:
        result.provider_failure = (
            f"{name}: the agent asked for {asked}, which an unattended run "
            f"cannot give; {ended_by_it}"
        )
        result.transient_failure = None
        result.interaction = InteractionOutcome(Lifecycle.FAILED)
    elif result.provider_failure or result.failed_exit:
        failure = result.provider_failure or f"{name}: exited {result.exit_code}"
        result.provider_failure = (
            f"{failure}; during the run the agent asked for {asked}, which {cli} refused "
            f"({REFUSED_REQUEST_GUIDE})"
        )
    return result


def _without_engine_state(
    needs: ProviderRequirements, engine_state: tuple[str, ...]
) -> ProviderRequirements:
    """``needs`` without a writable directory the engine itself projects (#151):
    a provider state directory of :meth:`AgentProvider.state_directories` lies
    under the masked ``<git common dir>/autoforge``, where only the engine may
    carve out (ADR 0005 C5), and the engine's projection already shows it
    writable at its host path, so a variable naming it stays true inside."""
    if not engine_state:
        return needs
    own = set(engine_state)
    return dataclasses.replace(
        needs,
        state=tuple(path for path in needs.state if path not in own),
        auth=tuple(
            item
            for item in needs.auth
            if not (item.mode is AuthMode.DIR_RW and item.source in own and item.target is None)
        ),
    )


def _truthy(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class RuntimeProbe:
    """What an adapter asks of the runtime a fresh-context launch would start
    (ADR 0015 D18's dynamic check; R3-F5, R4-F1 of PR #513).

    The engine runs ``command`` before the launch-intent save as a launch of
    the profile runs: its executable resolved on the launch's ``PATH`` as
    the launch resolves it, in the launch's environment, and inside the
    outer sandbox whenever the launch would be, around a scratch directory
    as ``doctor``'s sandboxed ``--version`` is (ADR 0005 D14.1). The adapter
    judges the result (:meth:`AgentProvider.fresh_context_runtime`).
    """

    command: tuple[str, ...]
    timeout_seconds: int
    max_output_bytes: int


#: A file's identity: its device, inode, size, and modification and change
#: times in nanoseconds. The change time moves with every write to the file and
#: every change of its metadata, and no process sets it back.
ExecutableIdentity = tuple[int, int, int, int, int]


def _identity(st: os.stat_result) -> ExecutableIdentity:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


class PinnedExecutable:
    """An executable held open from its check to its start (R5-F1 of PR #513).

    Opened read-only, without following a link at its name, from a resolved
    path (:func:`~autoforge.sandbox_policy.resolve_command`), and identified
    by :func:`_identity` of what was opened. A program started through
    :attr:`fd` (:attr:`~autoforge.executor.ExecutionRequest.executable_fd`)
    is this very file, whatever its name leads to by then: so the version a
    probe reads is the opened file's, and so is the program a launch starts.
    OSError when it cannot be opened, or is no regular file.
    """

    def __init__(self, path: str) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise OSError(f"{path} is not a regular file")
        except BaseException:
            os.close(fd)
            raise
        self.path = path
        self.fd = fd
        self.identity = _identity(st)

    def unchanged(self) -> bool:
        """Whether the file held is still as it was opened: nothing wrote to it since."""
        return _identity(os.fstat(self.fd)) == self.identity

    def named_by(self, path: str) -> bool:
        """Whether ``path`` (resolved) names the file held, unchanged."""
        return path == self.path and FreshContextRuntime.identity_of(path) == self.identity

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> PinnedExecutable:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@dataclass(frozen=True)
class FreshContextRuntime:
    """The executable a fresh-context launch was established on, and what it said.

    ``executable`` is its resolved host path, and ``identity`` the identity of
    the file the probe started (:class:`PinnedExecutable`), the same before it
    answered and after: a launch starts that very file, through a descriptor
    opened and checked against ``identity`` before its spawn, or is refused
    (:meth:`AgentProvider.fresh_context_launch`), so an update or another
    ``PATH`` between the check and the launch voids nothing silently, and no
    file put at the name after that last check is the one started.
    """

    executable: str
    identity: ExecutableIdentity
    version: str

    @staticmethod
    def identity_of(executable: str) -> ExecutableIdentity | None:
        """What ``executable`` names now, by its identity; ``None`` when it cannot be read."""
        try:
            return _identity(os.stat(executable))
        except OSError:
            return None


def no_fresh_context(profile: ProfileConfig, why: str, remedy: str) -> ConfigurationError:
    """The refusal of a profile a Memory phase routes to that cannot prove a fresh context.

    ADR 0015 D14 and D18: it names the profile, the capability and the remedy,
    never a value of the profile.
    """
    return ConfigurationError(
        f"profile {profile.name!r} ({profile.provider}) cannot prove that each launch starts "
        "with a fresh context, with no conversation of an earlier launch and no provider-side "
        "memory written by one, which Issue Memory needs (memory.enabled; ADR 0015 D14): "
        f"{why}. {remedy}, or turn memory.enabled off"
    )


def outside_allow_list(args: Iterable[str], allowed: Mapping[str, bool]) -> str:
    """The first of ``args`` an allow-list of flags does not take, as a refusal names it.

    ``allowed`` maps each flag to whether it takes a value, given as
    ``--flag=value`` or as the next argument, which must not look like a
    flag. ``""`` when every argument is taken. What is named is a flag's
    name, never a value: an argument that is no flag is "an argument that is
    not a flag".
    """
    items = list(args)
    i = 0
    while i < len(items):
        arg = items[i]
        name, eq, _ = arg.partition("=")
        if not arg.startswith("--") or name not in allowed:
            return repr(name) if arg.startswith("-") else "an argument that is not a flag"
        if eq and not allowed[name]:
            return f"{name!r} with a value"
        if allowed[name] and not eq:
            if i + 1 >= len(items) or items[i + 1].startswith("-"):
                return f"{name!r} without its value"
            i += 1
        i += 1
    return ""


class AgentProvider:
    """Base adapter. Subclasses implement ``build_command_for``, and
    ``stdin_payload`` when the prompt does not travel in argv."""

    name = "base"
    # The environment variables this provider's CLI reads for its own
    # authentication and configuration, added to the request's allow-list.
    # Names, never values: the executor selects them from the environment.
    environment_names: tuple[str, ...] = ()
    # The credentials this CLI signs in to its model provider with, which its
    # own names pass by design: the documented alternative to a stored login,
    # so no warning is said of them on every run (#286).
    sign_in_names: tuple[str, ...] = ()
    # The profile ``options`` keys this adapter reads. Any other key is a
    # configuration error naming the profile and this set, so a misspelled
    # knob cannot be a silent no-op. ``None`` leaves the keys unchecked; only
    # the scripted test provider, which launches no real CLI, uses it.
    option_keys: tuple[str, ...] | None = None
    # The variables :meth:`integration_isolation` sets for this CLI, names
    # only (the run log records them; the values vary by launch).
    isolation_names: tuple[str, ...] = ()
    # The variables this CLI reads that name a path (ADR 0005 D8.2): a
    # sandboxed launch keeps one only when what it names is visible inside.
    path_names: tuple[str, ...] = ()
    # The prompt template holding what an agent must be told of this CLI alone,
    # rendered into common.md's {{PROVIDER_SECTION}}; "" for none (#440).
    prompt_section: str = ""

    def __init__(self, runner: Runner | None = None) -> None:
        self._runner: Runner = runner or execute

    # -- to override -----------------------------------------------------
    def build_command_for(self, profile: ProfileConfig, prompt: str) -> list[str]:
        raise NotImplementedError

    def stdin_payload(self, req: AgentRequest) -> bytes | None:
        """What :meth:`execute` writes to the CLI's stdin; ``None`` is ``/dev/null``."""
        return None

    @contextmanager
    def integration_isolation(self) -> Iterator[dict[str, str]]:
        """The variables this CLI needs, for one launch, so that no MCP server,
        plugin or hook of the operator's or the repository's configuration
        loads and no function or alias of a shell startup file runs in its
        shell tool (#165): one may hold a GitHub token of its own.

        Set over the controller's (:attr:`AgentRequest.env`) by
        :meth:`execute`, whatever the allow-list passed. An adapter whose CLI
        has a flag for it says so in its argv instead and sets nothing here.
        """
        yield {}

    def complete_isolation(
        self, isolation: Mapping[str, str], environment: Mapping[str, str]
    ) -> None:
        """Finish what :meth:`integration_isolation` prepared for one launch
        once ``environment``, everything its CLI starts with
        (:meth:`started_environment`), is known: after the sandbox's
        description and before the CLI starts. One that cannot is an
        :class:`ExecutionError`, and nothing is launched. The base adapter has
        nothing to finish.
        """

    def integration_preflight(
        self, env_allowlist: tuple[str, ...] | None, *, fresh_context: bool = False
    ) -> None:
        """Refuse a launch whose CLI would still load an integration that
        neither its flags nor :meth:`integration_isolation` keep off (#165).

        ``env_allowlist`` is the configured allow-list (this provider adds
        its own names, as for the launch). ``fresh_context`` is the launch's
        :attr:`AgentRequest.fresh_context`: an adapter refuses, too, a
        configuration that would undo what its fresh-context proof rests on
        (ADR 0015 D18's dynamic check). Raises :class:`ConfigurationError`
        before anything starts: the engine calls this with the credential
        preflight, before a launch is charged, and :meth:`execute` calls it
        again before it launches. The base adapter has nothing to refuse.
        """

    def fresh_context_environment(self, fresh_context: bool) -> dict[str, str]:
        """What this adapter sets over a launch's environment so that a launch
        of :attr:`AgentRequest.fresh_context` starts with one (ADR 0015 D14),
        ``{}`` for any other launch. Part of the proof
        :meth:`fresh_context_proof` names; the base adapter's proofs rest on
        their argv alone, so it sets nothing.
        """
        return {}

    def fresh_context_probe(self, profile: ProfileConfig) -> RuntimeProbe | None:
        """What establishes that the runtime a launch of ``profile`` would start
        honours what :meth:`fresh_context_proof` rests on (ADR 0015 D18's
        dynamic check; R3-F5, R4-F1 of PR #513), for a launch of
        :attr:`AgentRequest.fresh_context` only; ``None`` when nothing needs
        establishing. The engine runs it before the launch-intent save, as the
        launch would run (:class:`RuntimeProbe`), and hands the adapter's
        verdict (:meth:`fresh_context_runtime`) to the launch. The base
        adapters' proofs rest on the argv they build, which no runtime
        changes.
        """
        return None

    def establish_fresh_context(
        self,
        profile: ProfileConfig,
        env_allowlist: tuple[str, ...] | None,
        env: Mapping[str, str],
        *,
        workspace: str,
        home: str,
        sandbox: ProbeSandbox | None,
    ) -> FreshContextRuntime | None:
        """Run :meth:`fresh_context_probe` as a launch of ``profile`` would run, and judge it.

        ADR 0015 D18's dynamic check (R4-F1 of PR #513), before the
        launch-intent save. ``env`` is what the launch sets over its
        allow-listed environment (#165's overrides, :meth:`integration_isolation`
        and :meth:`fresh_context_environment`); the probe's executable is
        resolved on that environment's ``PATH`` as :meth:`sandboxed` resolves
        the launch's, and run with this adapter's runner, in the scratch
        ``workspace`` (never the worktree), inside ``sandbox`` whenever the
        launch would be enforced, never without it then. ``None`` when the
        adapter asks nothing; the refusal (:func:`no_fresh_context`) when the
        answer establishes nothing, before anything is charged.

        The answer is bound to the file that gave it (R5-F1 of PR #513): the
        executable is opened before the probe (:class:`PinnedExecutable`)
        and the probe starts that open file, by its descriptor on the host
        and as a read-only mount of it inside the sandbox, never by its name.
        A file written to while it answered, or one the launch's ``PATH`` no
        longer leads to once it has, is refused here, before the save; the
        runtime carries the identity the probe's file had throughout.
        """
        probe = self.fresh_context_probe(profile)
        if probe is None:
            return None
        command = list(probe.command)
        launch_env = {**self.launch_environment(env_allowlist), **env}
        try:
            needs = self.sandbox_requirements(profile, command, launch_env, home)
        except (AutoForgeError, OSError) as exc:
            return self.fresh_context_runtime(profile, command[0], None, str(exc))
        if not needs.executable:
            return self.fresh_context_runtime(profile, "", None)
        try:
            pinned = PinnedExecutable(needs.executable)
        except OSError as exc:
            why = f"it cannot be opened ({exc.strerror or exc})"
            return self.fresh_context_runtime(profile, needs.executable, None, why)
        with pinned:
            try:
                description = None if sandbox is None else sandbox.describe(needs, launch_env)
                result = self._runner(
                    ExecutionRequest(
                        command=[needs.executable, *command[1:]],
                        cwd=workspace,
                        timeout_seconds=probe.timeout_seconds,
                        max_output_bytes=probe.max_output_bytes,
                        env_allowlist=(
                            None if env_allowlist is None else self.launch_allowlist(env_allowlist)
                        ),
                        env=dict(env),
                        contain_orphans=True,
                        sandbox=description,
                        executable_fd=pinned.fd,
                    )
                )
            except ExecutionError as exc:
                return self.fresh_context_runtime(profile, needs.executable, None, str(exc))
            if not pinned.unchanged() or not pinned.named_by(
                resolve_command(command[0], launch_env.get("PATH"))
            ):
                raise no_fresh_context(
                    profile,
                    f"{needs.executable} changed while its runtime was being established, so "
                    "its answer is no proof of the executable a launch would start",
                    "Repeat the command once the CLI is no longer being updated",
                )
            return self.fresh_context_runtime(
                profile, needs.executable, result, identity=pinned.identity
            )

    def fresh_context_runtime(
        self,
        profile: ProfileConfig,
        executable: str,
        result: ExecutionResult | None,
        failure: str = "",
        *,
        identity: ExecutableIdentity | None = None,
    ) -> FreshContextRuntime:
        """The runtime :meth:`fresh_context_probe`'s answer establishes, or the
        refusal (:func:`no_fresh_context`, a ConfigurationError).

        ``executable`` is the resolved path the probe ran (``""``: none was
        found on the launch's ``PATH``), ``result`` its answer, ``identity``
        the identity of the file that gave it (:class:`PinnedExecutable`),
        and ``failure`` why it could not be run (``result`` is then
        ``None``). An adapter that asks nothing establishes nothing.
        """
        raise no_fresh_context(
            profile, "its provider establishes no runtime", "Route the phase elsewhere"
        )

    @contextmanager
    def fresh_context_launch(
        self, req: AgentRequest, command: list[str], env: Mapping[str, str]
    ) -> Iterator[tuple[list[str], int | None]]:
        """``command`` as a launch of :attr:`AgentRequest.fresh_context` starts it,
        and the descriptor of the program it starts (``None``: by its name).

        Unchanged, and ``None``, for any other launch and for an adapter whose
        :meth:`fresh_context_probe` asks nothing. Otherwise the launch starts
        the very file :attr:`AgentRequest.fresh_context_runtime` was
        established on (R4-F1, R5-F1 of PR #513): ``command[0]`` resolved on
        ``env``'s ``PATH`` (the launch's, :meth:`started_environment`) as the
        spawn would resolve it must be that executable's path; it is opened
        (:class:`PinnedExecutable`) and must have the identity the probe's
        file had, and the launch then starts that open file through the
        descriptor yielded, named by its resolved path, so no file put at that
        name after this check is the one started. Held open until the block
        ends. A launch with no established runtime, another executable, or
        one changed since it answered is refused (:func:`no_fresh_context`)
        before anything is spawned. Nothing is run.
        """
        if not req.fresh_context or self.fresh_context_probe(req.profile) is None:
            yield command, None
            return
        runtime = req.fresh_context_runtime
        remedy = "Run it from the engine, which establishes its runtime before the launch"
        if runtime is None:
            raise no_fresh_context(
                req.profile,
                "no runtime was established for this launch before it was charged",
                remedy,
            )
        found = resolve_command(command[0], env.get("PATH"))
        if not found or found != runtime.executable:
            raise no_fresh_context(
                req.profile,
                f"the launch would start {found or 'no executable'}, not "
                f"{runtime.executable}, whose runtime was established",
                remedy,
            )
        try:
            pinned = PinnedExecutable(found)
        except OSError as exc:
            raise no_fresh_context(
                req.profile,
                f"{found} cannot be opened since its runtime was established "
                f"({exc.strerror or exc})",
                "Repeat the command once the CLI is no longer being updated",
            ) from None
        with pinned:
            if pinned.identity != runtime.identity:
                raise no_fresh_context(
                    req.profile,
                    f"{found} changed after its runtime was established",
                    "Repeat the command once the CLI is no longer being updated",
                )
            yield [runtime.executable, *command[1:]], pinned.fd

    def own_names(self, profile: ProfileConfig | None) -> tuple[str, ...]:
        """The variables this provider adds to the allow-list of ``profile``'s agents."""
        return self.environment_names

    def launch_environment(self, env_allowlist: tuple[str, ...] | None) -> dict[str, str]:
        """The variables of the controller's environment a launch with
        ``env_allowlist`` inherits, before any the controller sets."""
        if env_allowlist is None:
            return dict(os.environ)
        return select_environment(self.launch_allowlist(env_allowlist))

    def reports_activity(self, profile: ProfileConfig) -> bool:
        """Whether the CLI, launched for ``profile``, writes output while it works.

        A fact about the CLI, not a policy: the controller decides what to do
        with a CLI that is silent until it exits (it cannot run under an idle
        limit, see ``AutoForgeConfig.agent_limits``).
        """
        return True

    def interaction_settings(self, profile: ProfileConfig) -> InteractionSettings | None:
        """``profile``'s interaction policy and limits (ADR 0007 D7.1, D7.3), or
        ``None`` for a provider through which nothing can ask. Validated:
        :class:`ConfigurationError` for a value the policy refuses."""
        return None

    def plan_notes(
        self, profile: ProfileConfig, env_allowlist: tuple[str, ...] | None
    ) -> list[str]:
        """What a dry run's plan says of a launch of ``profile`` beyond its
        argv, for a provider whose launch the argv does not describe (#150).

        ``env_allowlist`` is the configured allow-list of an agent launch.
        Pure: names and paths by role only, never a value, and nothing is
        started, imported, written or resolved. The CLI adapters, whose argv
        is the launch, say nothing more.
        """
        return []

    def state_directories(self, profile: ProfileConfig) -> tuple[str, ...]:
        """The directories a launch of ``profile`` keeps its provider state in,
        relative to the issue's sandbox directory (#151): the engine creates
        them through its filesystem boundary before the launch, in every
        sandbox mode, passes the sandbox directory as
        :attr:`AgentRequest.state_dir` and, under an enforced mode, projects
        each writable as an engine carve-out. Persistent and per issue, never
        inside a working tree; AutoForge never deletes them. Names only: the
        engine knows nothing of what they hold. None for a CLI adapter.
        """
        return ()

    def interaction_capabilities(self, profile: ProfileConfig) -> Capabilities:
        """What this provider can carry of a decision for ``profile`` (ADR 0007
        D5.1, D5.3), for the launch's broker.

        A capability is declared true only once every ADR 0007 §3 gate of its
        row passed on the provider's pinned pair. None has yet, so every
        adapter declares :data:`~autoforge.interaction.NO_CAPABILITIES`: an
        ask fails the invocation under every policy.
        """
        return NO_CAPABILITIES

    def fresh_context_proof(self, profile: ProfileConfig) -> str:
        """How every launch of ``profile`` starts with a fresh context (ADR 0015 D14).

        A fresh context is no conversation of an earlier launch and no
        provider-side memory written by one, so that all a launch knows of
        earlier ones comes from its prompt and its read context. The proof is
        a property of the argv or the helper policy this adapter builds from
        ``profile``, never the provider's claim. Returns what it rests on;
        :class:`ConfigurationError` (:func:`no_fresh_context`) when the
        profile voids it or the adapter has none. The base adapter has none.
        """
        raise no_fresh_context(
            profile, "its provider declares no way to prove one", "Route the phase elsewhere"
        )

    def validate_profile(self, profile: ProfileConfig) -> None:
        """Raise ConfigurationError for values this CLI cannot accept.

        An override calls this first: it refuses an environment name that
        would hand the CLI a GitHub or git credential (#165) and checks the
        ``options`` keys.
        """
        refuse_forbidden_env(self.environment_names, f"provider {self.name!r}")
        if self.option_keys is None:
            return
        unknown = sorted(set(profile.options) - set(self.option_keys))
        if unknown:
            raise ConfigurationError(
                f"profile {profile.name!r}: unknown {self.name} option(s) "
                f"{', '.join(unknown)} (accepted: {', '.join(self.option_keys) or 'none'})"
            )

    def sandbox_requirements(
        self, profile: ProfileConfig, command: list[str], env: Mapping[str, str], home: str
    ) -> ProviderRequirements:
        """What a sandboxed launch of ``command`` needs from the host, as data (ADR 0005 D13.1).

        ``env`` is the launch's environment (the allow-listed variables, the
        controller's overrides and :meth:`integration_isolation`'s) and
        ``home`` the operator's home. The base declaration is the executable,
        resolved on the launch's ``PATH`` as the spawn would resolve it, its
        install root and its interpreter's, an absolute ``#!`` path as
        written (a spelled path, #225), and :attr:`path_names`; an
        adapter adds its configuration, credential and state items. Names
        and paths only: no file is read but an executable's first line.
        """
        executable, roots, path_dirs, spelled = executable_requirements(
            command[0], home=home, search_path=env.get("PATH")
        )
        return ProviderRequirements(
            executable=executable,
            install_roots=roots,
            path_dirs=path_dirs,
            spelled_paths=spelled,
            path_names=self.path_names,
        )

    def credential_routes(self, profile: ProfileConfig) -> CredentialRoutes | None:
        """Where ``profile``'s model credential may come from under the container
        backend (ADR 0013 D14, #290), as data for
        :func:`autoforge.container_auth.plan_credentials`: names and kinds,
        never a value. ``None``: this adapter maps no credential, and a
        container launch of it is refused. Only Claude Code, OpenCode and Pi
        map one; the SDK providers keep their own policy (ADR 0007).
        """
        return None

    def written_directories(
        self, profile: ProfileConfig, env_allowlist: tuple[str, ...] | None, home: str
    ) -> tuple[tuple[str, str], ...]:
        """The host directories a sandboxed launch of ``profile`` writes that outlive it (#280).

        Each directory :meth:`sandbox_requirements` makes writable, with what
        it is (:func:`~autoforge.sandbox_policy.declared_writable`), declared
        over the environment a launch with ``env_allowlist`` inherits from
        this process. The names :meth:`integration_isolation` sets are left
        out of it: every launch sets them to a directory or file made for it
        and removed afterwards, which nothing the operator keeps is in. What
        a ``--config`` file may not lie in under an enforced mode, since an
        agent could rewrite it there
        (:func:`autoforge.engine.config_file_placement_problem`). Names and
        paths only: nothing is created, and no file is read but an
        executable's first line.
        """
        isolated = set(self.isolation_names)
        env = {
            name: value
            for name, value in self.launch_environment(env_allowlist).items()
            if name not in isolated
        }
        needs = self.sandbox_requirements(profile, [profile.command or self.name], env, home)
        return declared_writable(needs)

    # -- shared behaviour -------------------------------------------------
    def build_command(self, req: AgentRequest) -> list[str]:
        return self.build_command_for(req.profile, req.prompt)

    def sandboxed(
        self, req: AgentRequest, command: list[str], env: Mapping[str, str] | None
    ) -> tuple[list[str], LaunchDescription | None]:
        """``command`` as it runs inside the request's sandbox, and its description.

        Unchanged, and ``None``, without :attr:`AgentRequest.sandbox`. Inside,
        the executable is named by its resolved host path, which the
        description shows read-only (D13.1). ``env`` is what the adapter sets
        over the allow-listed environment for this process. A launch the
        policy refuses raises
        :class:`~autoforge.errors.SandboxLaunchRefusedError` before anything
        is spawned.
        """
        if req.sandbox is None:
            return command, None
        launch_env = {**self.launch_environment(req.env_allowlist), **(env or {})}
        needs = self.sandbox_requirements(req.profile, command, launch_env, req.sandbox.facts.home)
        needs = _without_engine_state(needs, req.sandbox.facts.provider_state)
        description = req.sandbox.describe(needs, launch_env)
        if needs.executable:
            command = [needs.executable, *command[1:]]
        return command, description

    def started_environment(
        self,
        req: AgentRequest,
        env: Mapping[str, str] | None,
        description: LaunchDescription | None,
    ) -> dict[str, str]:
        """Everything the CLI of ``req`` starts with: the variables of this
        process its allow-list passes, ``env`` (what :meth:`sandboxed` was
        given) over them and, in a sandbox, ``description``'s over those
        (D8.2), as the executor and the backend compose them."""
        started = {**self.launch_environment(req.env_allowlist), **(env or {})}
        if description is None:
            return started
        started = {
            name: value for name, value in started.items() if name not in description.env_unset
        }
        started.update(description.env_set)
        return started

    def launch_allowlist(self, base: tuple[str, ...]) -> tuple[str, ...]:
        """``base`` (the configured allow-list) plus this provider's own names.

        Neither may name a GitHub or git credential route (#165): config load
        and :meth:`validate_profile` refuse one first, and this refuses it
        again for an allow-list that did not come through either.
        """
        names = tuple(dict.fromkeys([*base, *self.environment_names]))
        refuse_forbidden_env(names, f"the {self.name} launch allow-list")
        return names

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

        self.integration_preflight(req.env_allowlist, fresh_context=req.fresh_context)
        with self.integration_isolation() as isolation:
            isolation = {**isolation, **self.fresh_context_environment(req.fresh_context)}
            env = {**(req.env or {}), **isolation} if isolation else req.env
            command, description = self.sandboxed(req, command, env)
            started = self.started_environment(req, env, description)
            with self.fresh_context_launch(req, command, started) as (command, program):
                self.complete_isolation(isolation, started)
                res = self._runner(
                    ExecutionRequest(
                        command=command,
                        cwd=req.cwd,
                        timeout_seconds=req.max_runtime_seconds or MAX_DEADLINE_SECONDS,
                        idle_timeout_seconds=req.idle_timeout_seconds,
                        env_allowlist=self.environment_allowlist(req),
                        env=env,
                        # Nothing an agent starts outlives its invocation (ADR
                        # 0002), including what it detached from its process group.
                        contain_orphans=True,
                        stdin_data=self.stdin_payload(req),
                        on_output=_activity_hook(req.progress),
                        on_chunk=on_chunk,
                        stop=stop,
                        sandbox=description,
                        executable_fd=program,
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
# How long a CLI still running once its stream has failed the run is given
# to exit on its own before the driver stops it (#394). A failed turn does
# not end ``claude -p``: a background task still pending keeps it alive for
# another turn, whose work no later turn can redeem. A CLI whose failing
# record was its last exits well within it and keeps its own status.
CLAUDE_FAILED_EXIT_GRACE_SECONDS = 10.0
# How long a CLI still running after a reply that decides the run is given to
# exit on its own before the driver stops it (#439). ``claude -p`` emits a
# turn's ``result`` at once, but a background task still pending keeps it alive
# for a turn that may never come, silently, until the idle limit. The agent is
# told to leave nothing pending (``prompts/claude_background_work.md``). The
# value is #394's, not a measurement of how long a CLI takes to exit after its
# last ``result``: one that takes longer loses its own status, not its reply.
CLAUDE_REPLIED_EXIT_GRACE_SECONDS = 10.0
# What every launch adds so that no integration of the operator's or the
# repository's configuration runs (#165): one may hold a GitHub token of its
# own. ``--safe-mode`` keeps hooks, installed plugins, MCP servers, skills,
# custom agents and commands, and CLAUDE.md from loading, those of
# ``--settings``, ``--plugin-dir`` and ``--mcp-config`` included, and wins
# over ``CLAUDE_CODE_SAFE_MODE=0``; ``--strict-mcp-config`` with no
# ``--mcp-config`` keeps MCP servers off on its own. ``--setting-sources=user``
# reads no project or local settings (``.claude/settings.json`` and
# ``settings.local.json`` of the worktree, which the repository controls):
# 2.1.296 applies their ``env`` over the launch's under ``--safe-mode`` too,
# so a ``CLAUDE_ENV_FILE`` there would undo :data:`CLAUDE_SHELL_CLEANER`
# (seen with a loopback API). The user's settings stay, checked instead
# (:func:`claude_configuration_problems`).
CLAUDE_ISOLATION_FLAGS = ("--safe-mode", "--strict-mcp-config", "--setting-sources=user")
# The extra_args a Claude Code profile may carry when a Memory phase routes
# to it (#317, ADR 0015 D14), each mapped to whether it takes a value: none
# resumes, forks or names a session, loads settings or reaches memory an
# earlier launch wrote. Read from `claude --help` of Claude Code 2.1.328; a
# flag a later CLI adds is refused until someone checks it and adds it here.
CLAUDE_MEMORY_ARGS: Mapping[str, bool] = {
    "--allowed-tools": True,
    "--allowedTools": True,
    "--disallowed-tools": True,
    "--disallowedTools": True,
    "--fallback-model": True,
    "--max-budget-usd": True,
    "--verbose": False,
}
# The same for an OpenCode profile, read from `opencode run --help` of
# OpenCode 2.3.0: none continues, forks or names a session or reaches a
# server whose sessions persist.
OPENCODE_MEMORY_ARGS: Mapping[str, bool] = {
    "--archived": False,
    "--log-level": True,
    "--print-logs": False,
    "--title": True,
}
# The flags that would bring an integration or the repository's settings
# back, refused in ``extra_args``: MCP servers, plugins, settings (hooks
# among them), setting sources and agent definitions, and the browser and
# IDE connections.
CLAUDE_INTEGRATION_FLAGS = (
    "--mcp-config",
    "--plugin-dir",
    "--plugin-url",
    "--settings",
    "--setting-sources",
    "--agents",
    "--chrome",
    "--ide",
)


def _integration_flag(arg: str) -> str | None:
    """The integration flag ``arg`` is (``--flag`` or ``--flag=value``); None if none."""
    for flag in CLAUDE_INTEGRATION_FLAGS:
        if arg == flag or arg.startswith(flag + "="):
            return flag
    return None


# The shell Claude Code's Bash tool runs a command in (#165). 2.1.296 (read
# from the build, checked against it with a loopback API) starts
# ``$CLAUDE_CODE_SHELL`` when that names a bash or a zsh, else picks one
# itself. Once per session it runs that shell as ``<shell> -c -l``, sources
# ``$HOME/.zshrc`` or ``$HOME/.bashrc`` there (by ``HOME``, whatever
# ``ZDOTDIR`` says) and writes the functions, aliases and options it then
# has to a snapshot; every command sources the snapshot first or, without
# one, runs in a login shell (``-c -l``, which reads ``~/.bash_profile``).
# A function or alias of the operator's there (``gh() { GH_TOKEN=... command
# gh "$@"; }``) would hand the agent's ordinary ``gh`` a credential, and no
# flag or variable turns the snapshot off. What 2.1.296 runs next in the
# same shell, before the command and in both cases, is the text of the file
# ``CLAUDE_ENV_FILE`` names, so every launch names a file of its own holding
# :data:`CLAUDE_SHELL_CLEANER`; it also empties ``CLAUDE_CODE_SHELL_PREFIX``,
# a program 2.1.296 would run every command through, and sets
# ``CLAUDE_CODE_SHELL`` itself, which could otherwise name a program of the
# operator's to start as the shell.
CLAUDE_SHELLS = ("bash", "zsh")
CLAUDE_ENV_FILE_NAME = "env.sh"
# Inlined by 2.1.296 after the snapshot is sourced and before the command
# (``<snapshot> && <this>\n: && ... && eval <command>``): it removes every
# function and alias, the operator's and Claude Code's own (its ``rg`` and
# ``pkill`` wrappers) alike, then checks that none is left and otherwise
# ends the shell, so the command does not run. The block is one line after
# the comment, joined to the snapshot line by ``&&``, so it is parsed before
# the snapshot defines an alias; every command word is ``\``-quoted, so an
# alias of the login files cannot expand either. In bash, assigning
# ``POSIXLY_CORRECT`` (no function shadows an assignment) puts the special
# builtin ``unset`` ahead of any function, which then removes whatever
# function shadows ``builtin`` or ``read``. zsh has no such mode: whatever
# ``builtin`` cannot remove, the count of ``$functions`` and the alias
# tables (parameters, which nothing shadows) refuses. ``${x:?}`` of an
# empty ``x``, an expansion and no command, ends a non-interactive shell.
_CLAUDE_CLEANER_LEFT = (
    "__af_left=; __af_x=${__af_left:?AutoForge: a function or alias of a shell startup "
    "file is still defined, so this command is not run (#165)}"
)
CLAUDE_SHELL_CLEANER = (
    "# Written by AutoForge for one Claude Code launch (#165): no function or alias of a "
    "shell startup file runs in a Bash tool command.\n"
    "{ if [[ -n ${BASH_VERSION-} ]]; then "
    "case :${SHELLOPTS-}: in (*:posix:*) __af_posix=1 ;; "
    "(*) __af_posix= ; POSIXLY_CORRECT=y ;; esac; "
    "\\unset -f builtin command declare read unset unalias alias; "
    "[[ -n $__af_posix ]] || \\unset POSIXLY_CORRECT; "
    'while \\builtin read -r __af_f; do \\builtin unset -f -- "${__af_f##* }" || __af_rc=1; '
    "done < <(\\builtin declare -F); "
    "\\builtin unalias -a 2>/dev/null || __af_rc=1; "
    "[[ -z $(\\builtin declare -F) && -z $(\\builtin alias) ]] || { "
    + _CLAUDE_CLEANER_LEFT
    + "; }; "
    "\\builtin unset __af_f __af_posix __af_rc __af_x __af_left; "
    "elif [[ -n ${ZSH_VERSION-} ]]; then "
    "\\builtin unfunction -m '*' 2>/dev/null || __af_rc=1; "
    "\\builtin unalias -a 2>/dev/null || __af_rc=1; "
    "\\builtin unalias -as 2>/dev/null || __af_rc=1; "
    "(( ${+functions} && ${+aliases} && ${+galiases} && ${+saliases} "
    "&& ${#functions} + ${#aliases} + ${#galiases} + ${#saliases} == 0 )) || { "
    + _CLAUDE_CLEANER_LEFT
    + "; }; "
    "\\builtin unset __af_rc __af_x __af_left; "
    "else __af_left=; __af_x=${__af_left:?AutoForge: this command would run in a shell "
    "AutoForge cannot clear of shell startup functions and aliases (#165)}; fi; }\n"
)


def claude_login_path(path: str) -> str:
    """What a launch's ``CLAUDE_ENV_FILE`` holds after
    :data:`CLAUDE_SHELL_CLEANER` when the launch starts Claude Code with
    ``path`` as its ``PATH`` (#347): a login shell sets ``PATH`` back to it.

    Without a snapshot, 2.1.296 runs a command in a login shell (2.1.310 to
    2.1.312 too, read from the build), whose ``/etc/profile`` may set
    ``PATH`` outright (Debian's does), so the command would not find what
    the launch put on ``PATH``. With one the shell is no login shell, and
    ``PATH`` stays as the snapshot left it (2.1.310 to 2.1.312 write their
    own ``PATH`` there). The block is a line of its own, parsed after the
    cleaner has removed every function and alias, and its command words are
    ``\\``-quoted all the same.
    """
    return (
        "# Written by AutoForge for the same launch (#347): a login shell, in which a "
        "command runs without a shell snapshot, gets back the PATH of the launch.\n"
        "{ if [[ -n ${BASH_VERSION-} ]] && \\builtin shopt -q login_shell || "
        "[[ -n ${ZSH_VERSION-} && -o login ]]; then "
        f"PATH={shlex.quote(path)}; \\builtin export PATH; fi; }}\n"
    )


def claude_shell(shell: str) -> str:
    """The shell a Claude Code launch's Bash tool runs in (``CLAUDE_CODE_SHELL``):
    ``shell`` (the launch's ``SHELL``, :func:`autoforge.child_env.child_shell`)
    when it is a bash or a zsh, else the ``bash`` or ``zsh`` on ``PATH``;
    empty when there is neither (2.1.296 then looks for one itself, and
    :data:`CLAUDE_SHELL_CLEANER` clears either)."""
    if os.path.basename(shell) in CLAUDE_SHELLS:
        return shell
    return shutil.which("bash") or shutil.which("zsh") or ""


def claude_isolation_env(env_file: str, shell: str) -> dict[str, str]:
    """What a Claude Code launch sets so that its Bash tool runs no function
    or alias of a shell startup file and no program of the operator's
    around a command: ``env_file`` holds :data:`CLAUDE_SHELL_CLEANER`."""
    return {
        "CLAUDE_ENV_FILE": env_file,
        "CLAUDE_CODE_SHELL": shell,
        "CLAUDE_CODE_SHELL_PREFIX": "",
    }


# What every ``claude`` launch sets for the CLI's own lifecycle (#507). 2.1.326
# stops every background task (a background agent, a shell, a monitor) ten
# minutes after the last turn and exits 0 without another turn, so an agent
# that ended its turn to wait for its background agents loses their work and
# never gets the turn it waited for. ``0`` turns that ceiling off (read from
# the build: ``CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS``, default 600000): the
# controller's idle limit and maximum runtime bound the wait instead, as they
# bound everything else a launch does. Not for ``claude-sdk``, whose pinned
# Claude Code predates the ceiling.
CLAUDE_LIFECYCLE_ENV = {"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0"}
# What a launch that needs a fresh context sets (ADR 0015 D14; R2-F7 of PR
# #513): no auto memory, which an earlier launch of the same worktree path
# would otherwise have written under the user's Claude configuration and this
# one would read. Read from the 2.1.328 build: the CLI decides auto memory
# from this variable before any setting, and a true value turns it off
# whatever ``autoMemoryEnabled`` the user, the project or ``--settings`` says
# (``0`` forces it on instead, so a configuration whose ``env`` sets it is
# refused, :meth:`ClaudeCodeProvider.integration_preflight`). ``--safe-mode``
# alone was not shown to turn it off, and ``--bare``, whose help names it,
# also changes how the CLI authenticates. Only for such a launch: every other
# launch is unchanged. Not for ``claude-sdk``, whose policy turns auto memory
# off in its settings and checks ``init`` for it (docs/sdk-policy.md).
CLAUDE_FRESH_CONTEXT_ENV = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
# The Claude Code builds the variable above was read from (R3-F5, R4-F1 of PR
# #513): 2.1.328 decides auto memory from it before any setting. A launch that
# needs a fresh context runs only a CLI whose `claude --version` reports one of
# these builds, a release and not a pre-release, asked of the very executable
# the launch then starts, in its environment and under the launch's isolation,
# before the launch is charged (:meth:`ClaudeCodeProvider.fresh_context_probe`).
# An exact list, not a floor: a later build is refused until someone reads how
# it treats the variable and adds it here, as for a flag of CLAUDE_MEMORY_ARGS,
# since a build nobody checked proves nothing.
CLAUDE_FRESH_CONTEXT_VERSIONS: frozenset[tuple[int, int, int]] = frozenset({(2, 1, 328)})
# The probe's bounds: `--version` answers at once and prints one line.
CLAUDE_VERSION_PROBE_SECONDS = 30
CLAUDE_VERSION_PROBE_MAX_OUTPUT_BYTES = 4096
# What `claude --version` prints: `2.1.328 (Claude Code)`.
_CLAUDE_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?(?: \(Claude Code\))?")


def claude_release(text: str) -> tuple[tuple[int, int, int] | None, bool]:
    """The release ``claude --version`` output names, and whether it is a pre-release.

    ``(None, False)`` when the output is not one line of the version's shape.
    """
    lines = text.strip().splitlines()
    match = _CLAUDE_VERSION_RE.fullmatch(lines[0].strip()) if len(lines) == 1 else None
    if match is None:
        return None, False
    major, minor, patch = (int(part) for part in match.group(1, 2, 3))
    return (major, minor, patch), bool(match.group(4))


@contextmanager
def claude_shell_isolation() -> Iterator[dict[str, str]]:
    """A :data:`CLAUDE_SHELL_CLEANER` file for one Claude Code launch's
    ``CLAUDE_ENV_FILE``, a bash or zsh to run Bash tool commands in and no
    shell prefix (:func:`claude_isolation_env`): no function or alias of
    the operator's shell startup files runs in one (#165). For the ``claude``
    CLI and for the Claude Code the ``claude-sdk`` helper starts (#151).

    The directory (``mkdtemp``, mode 0700) is removed when the launch
    ends; one that cannot be made or written is an
    :class:`ExecutionError`, before anything is launched.
    """
    try:
        env_dir = tempfile.mkdtemp(prefix="autoforge-claude-")
    except OSError as exc:
        raise ExecutionError(f"cannot prepare the claude shell environment: {exc}") from exc
    try:
        env_file = os.path.join(env_dir, CLAUDE_ENV_FILE_NAME)
        try:
            fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(CLAUDE_SHELL_CLEANER)
        except OSError as exc:
            raise ExecutionError(f"cannot prepare the claude shell environment: {exc}") from exc
        yield claude_isolation_env(env_file, claude_shell(child_shell()))
    finally:
        shutil.rmtree(env_dir, ignore_errors=True)


def complete_claude_isolation(isolation: Mapping[str, str], environment: Mapping[str, str]) -> None:
    """Add :func:`claude_login_path` of the ``PATH`` Claude Code starts with
    (inside a sandbox, as rewritten there) to the launch's
    ``CLAUDE_ENV_FILE``; nothing when it starts with no ``PATH``, or
    :func:`claude_shell_isolation` named no such file (#347)."""
    env_file = isolation.get("CLAUDE_ENV_FILE")
    path = environment.get("PATH")
    if not env_file or not path:
        return
    try:
        fd = os.open(env_file, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(claude_login_path(path))
    except OSError as exc:
        raise ExecutionError(f"cannot prepare the claude shell environment: {exc}") from exc


# Read whole to find its ``env``, and refused past this size: Claude Code's
# global configuration grows with its project history.
CLAUDE_MAX_CONFIG_BYTES = 32 * 1024 * 1024


def claude_config_files(env: Mapping[str, str]) -> list[Path]:
    """The operator's files whose ``env`` a Claude Code launch with ``env``
    sets over its own environment, and so over every command its Bash tool
    runs, ``--safe-mode`` notwithstanding (2.1.296, read from the build and
    seen replacing the placeholder ``GH_TOKEN`` and ``CLAUDE_ENV_FILE``): the
    user settings, ``settings.json`` in the configuration directory
    (``CLAUDE_CONFIG_DIR``, else ``~/.claude``), and the global
    configuration, ``.config.json`` there when it exists, else
    ``.claude.json`` (``.claude-custom-oauth.json`` with
    ``CLAUDE_CODE_CUSTOM_OAUTH_URL``) in ``CLAUDE_CONFIG_DIR``, else in the
    home (``HOME``, else the password database's)."""
    home = env.get("HOME")
    if not home:
        try:
            home = pwd.getpwuid(os.getuid()).pw_dir
        except KeyError:
            home = "/"
    explicit = env.get("CLAUDE_CONFIG_DIR")
    config_dir = Path(explicit or Path(home, ".claude"))
    legacy = config_dir / ".config.json"
    suffix = "-custom-oauth" if env.get("CLAUDE_CODE_CUSTOM_OAUTH_URL") else ""
    global_config = legacy if legacy.exists() else Path(explicit or home, f".claude{suffix}.json")
    return [config_dir / "settings.json", global_config]


def _claude_config_env_problems(path: Path, also: tuple[str, ...] = ()) -> list[str]:
    """The names in the ``env`` of ``path`` that would undo a launch's
    isolation, or one of ``also``, or that it could not be read; names only,
    never a value."""
    if not path.exists():
        return []
    refused = _not_a_regular_file(path)
    if refused:
        return [refused]
    try:
        with path.open("rb") as handle:
            data = handle.read(CLAUDE_MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return []
    except OSError as exc:
        return [f"{path} could not be checked ({type(exc).__name__})"]
    if len(data) > CLAUDE_MAX_CONFIG_BYTES:
        return [f"{path} is larger than {CLAUDE_MAX_CONFIG_BYTES} bytes and was not checked"]
    try:
        document = json.loads(data)
    except ValueError:
        return [f"{path} could not be checked (not JSON)"]
    env = document.get("env") if isinstance(document, dict) else None
    if not isinstance(env, dict):
        return []
    isolation = {name.upper() for name in (*claude_isolation_env("", ""), *also)}
    hits = sorted(
        name
        for name in env
        if isinstance(name, str) and (undoes_isolation(name) or name.upper() in isolation)
    )
    if not hits:
        return []
    return [f"{', '.join(redact(name) for name in hits)} in the env of {path}"]


def claude_configuration_problems(env: Mapping[str, str], also: tuple[str, ...] = ()) -> list[str]:
    """What, in the configuration a Claude Code launch with ``env`` reads,
    would set a variable over the launch's isolation, or one of ``also``
    (empty: nothing would).

    ``env`` is the environment the launch inherits (``HOME``,
    ``CLAUDE_CONFIG_DIR`` and ``CLAUDE_CODE_CUSTOM_OAUTH_URL`` decide where
    2.1.296 looks, :func:`claude_config_files`). A file that exists but
    cannot be read is a problem too: a launch is refused rather than assumed
    clean. Managed (administrator) settings are not read: the operator can
    change them no more than a launch can.
    """
    problems: list[str] = []
    for path in claude_config_files(env):
        problems += _claude_config_env_problems(path, also)
    return problems


# Claude Code's credential routes under the container backend (#290), read
# from 2.1.325: an API key, or the long-lived token `claude setup-token`
# prints, which the operator makes on the host and which no launch refreshes.
# The refresh token variable is a route too, refused: its use rotates it.
CLAUDE_CREDENTIAL_ROUTES = (
    EnvRoute("ANTHROPIC_API_KEY", CredentialKind.API_KEY),
    EnvRoute("CLAUDE_CODE_OAUTH_TOKEN", CredentialKind.OAUTH_TOKEN),
    EnvRoute(
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN", CredentialKind.OAUTH_REFRESH_TOKEN, Refresh.ROTATING
    ),
)
# What would put Claude Code on another route, which the container backend
# does not take: a gateway's bearer token, or a cloud provider's credentials,
# which come from the cloud's own configuration.
CLAUDE_UNSUPPORTED_ROUTES = (
    UnsupportedRoute(
        "ANTHROPIC_AUTH_TOKEN",
        CredentialKind.GATEWAY_TOKEN,
        "a gateway's bearer token is not a route #290 maps",
    ),
    *(
        UnsupportedRoute(
            name,
            CredentialKind.CLOUD,
            f"{cloud} authenticates with the cloud's own credentials, which are never projected",
        )
        for name, cloud in (
            ("CLAUDE_CODE_USE_BEDROCK", "Amazon Bedrock"),
            ("CLAUDE_CODE_USE_VERTEX", "Google Vertex AI"),
            ("CLAUDE_CODE_USE_FOUNDRY", "Microsoft Foundry"),
        )
    ),
)
# Where Claude Code keeps its login and settings, in a container launch: the
# private home's own directory, never the host's or one in the workspace.
CLAUDE_PRIVATE_STORES = (PrivateStore("CLAUDE_CONFIG_DIR", ".claude"),)


class ClaudeCodeProvider(AgentProvider):
    name = "claude"
    # ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` and the base URL and
    # model overrides the CLI reads; ``CLAUDE_CONFIG_DIR`` and the
    # ``CLAUDE_CODE_*`` feature switches.
    environment_names = ("ANTHROPIC_*", "CLAUDE_*")
    sign_in_names = ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
    option_keys = ("permission_mode", "output_format", "session_persistence")
    isolation_names = tuple(sorted({**claude_isolation_env("", ""), **CLAUDE_LIFECYCLE_ENV}))
    path_names = ("CLAUDE_ENV_FILE", "CLAUDE_CODE_SHELL", "CLAUDE_CONFIG_DIR")
    # The end of a turn does not end this CLI while a background task is
    # pending, which is true of no other adapter's (#440).
    prompt_section = "claude_background_work.md"

    def sandbox_requirements(
        self, profile: ProfileConfig, command: list[str], env: Mapping[str, str], home: str
    ) -> ProviderRequirements:
        """Claude Code's declaration (ADR 0005 D7.1, D13.1).

        The launch's ``CLAUDE_ENV_FILE`` read-only. With ``CLAUDE_CONFIG_DIR``
        set to an absolute path, that directory writable where it resolves
        (``dir-rw``: the CLI rewrites its login there; the variable names it
        there inside), refused when it is or lies in ``~/.claude`` (D7.2).
        Without it, the login file ``~/.claude/.credentials.json`` read-only
        (``file-ro``; a token the CLI must refresh cannot be written back)
        and the user settings ``~/.claude/settings.json`` read-only, both
        when present, each at that path in the private home, where the CLI
        looks for it, even when the host path is a link to a file elsewhere.
        A key in ``ANTHROPIC_*`` or ``CLAUDE_CODE_OAUTH_TOKEN`` is an ``env``
        item. ``~/.claude`` and ``~/.claude.json`` are the operator
        configuration roots.
        """
        needs = super().sandbox_requirements(profile, command, env, home)
        root = os.path.join(home, ".claude")
        config: list[str] = []
        home_config: list[str] = []
        auth = [
            AuthItem(AuthMode.ENV, name)
            for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
            if env.get(name)
        ]
        env_file = env.get("CLAUDE_ENV_FILE", "")
        if env_file.startswith("/"):
            config.append(env_file)
        explicit = env.get("CLAUDE_CONFIG_DIR", "")
        if explicit.startswith("/"):
            auth.append(
                AuthItem(
                    AuthMode.DIR_RW,
                    "the Claude Code configuration directory ($CLAUDE_CONFIG_DIR)",
                    explicit,
                    env_name="CLAUDE_CONFIG_DIR",
                )
            )
        else:
            login = os.path.join(root, ".credentials.json")
            if os.path.isfile(login):
                auth.append(
                    AuthItem(AuthMode.FILE_RO, "the Claude Code login", login, target=login)
                )
            settings = os.path.join(root, "settings.json")
            if os.path.isfile(settings):
                home_config.append(settings)
        return dataclasses.replace(
            needs,
            config=(*needs.config, *config),
            home_config=(*needs.home_config, *home_config),
            auth=tuple(auth),
            operator_config_roots=(root, os.path.join(home, ".claude.json")),
        )

    def credential_routes(self, profile: ProfileConfig) -> CredentialRoutes:
        """``ANTHROPIC_API_KEY`` or ``CLAUDE_CODE_OAUTH_TOKEN``, exactly one
        (:data:`CLAUDE_CREDENTIAL_ROUTES`); the login in ``~/.claude`` or
        ``$CLAUDE_CONFIG_DIR`` is never projected, since the CLI refreshes it
        in place, and inside the container ``CLAUDE_CONFIG_DIR`` names an
        empty directory of the private home (:data:`CLAUDE_PRIVATE_STORES`).
        No other ``ANTHROPIC_*`` or ``CLAUDE_*`` variable is passed."""
        return CredentialRoutes(
            "anthropic",
            CLAUDE_CREDENTIAL_ROUTES,
            CLAUDE_UNSUPPORTED_ROUTES,
            stores=CLAUDE_PRIVATE_STORES,
        )

    @contextmanager
    def integration_isolation(self) -> Iterator[dict[str, str]]:
        """A :data:`CLAUDE_SHELL_CLEANER` file for this launch's
        ``CLAUDE_ENV_FILE``, a bash or zsh to run Bash tool commands in and no
        shell prefix (:func:`claude_shell_isolation`, #165), and the CLI's
        background-wait ceiling off (:data:`CLAUDE_LIFECYCLE_ENV`, #507)."""
        with claude_shell_isolation() as isolation:
            yield {**isolation, **CLAUDE_LIFECYCLE_ENV}

    def complete_isolation(
        self, isolation: Mapping[str, str], environment: Mapping[str, str]
    ) -> None:
        """Add the login ``PATH`` to the launch's ``CLAUDE_ENV_FILE``
        (:func:`complete_claude_isolation`, #347)."""
        complete_claude_isolation(isolation, environment)

    def integration_preflight(
        self, env_allowlist: tuple[str, ...] | None, *, fresh_context: bool = False
    ) -> None:
        """Refuse a launch while Claude Code's own configuration would set a
        variable over its isolation (:func:`claude_configuration_problems`):
        2.1.296 applies the ``env`` of the user settings and of the global
        configuration over the environment it is started with, under
        ``--safe-mode`` too, so a ``GH_TOKEN`` there would replace the
        placeholder and a ``CLAUDE_ENV_FILE`` there the cleaner. For a launch
        that needs a fresh context, a ``CLAUDE_CODE_DISABLE_AUTO_MEMORY``
        there too: ``0`` would force the auto memory on that
        :data:`CLAUDE_FRESH_CONTEXT_ENV` turns off (ADR 0015 D14, D18;
        R2-F7 of PR #513).
        """
        problems = claude_configuration_problems(
            self.launch_environment(env_allowlist),
            also=tuple(CLAUDE_FRESH_CONTEXT_ENV) if fresh_context else (),
        )
        if problems:
            raise ConfigurationError(
                "refusing to launch claude: its own configuration sets variables over the "
                "environment AutoForge gives a launch, which would hand its commands a GitHub "
                "or git credential or let a shell startup file's functions and aliases run in "
                f"them: {'; '.join(problems)}. Remove them from that env (Claude Code reads "
                "the variables of the environment it is started in; set them there and allow "
                "them through execution.agent_env_allowlist_extra unless they name a credential), "
                "or route these profiles to another provider (docs/configuration.md, "
                "'Agent environment')"
            )

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
        for arg in profile.extra_args:
            flag = _integration_flag(arg)
            if flag is not None:
                # The flag only, never its value: a settings string may hold a key.
                raise ConfigurationError(
                    f"profile {profile.name!r}: claude extra_args cannot carry {flag}: it "
                    "loads an integration (an MCP server, a plugin, hooks, an agent "
                    "definition, a browser or IDE connection) or settings, which may hold "
                    "a GitHub token of its own or set variables over the launch's, and "
                    f"AutoForge launches Claude Code with {', '.join(CLAUDE_ISOLATION_FLAGS)} "
                    "so that none applies (docs/configuration.md, 'Agent environment')"
                )

    @staticmethod
    def output_format(profile: ProfileConfig) -> str:
        return profile.options.get("output_format") or CLAUDE_DEFAULT_OUTPUT_FORMAT

    def reports_activity(self, profile: ProfileConfig) -> bool:
        # `text` prints the final text once, at exit, and nothing before it.
        return self.output_format(profile) == "stream-json"

    def fresh_context_environment(self, fresh_context: bool) -> dict[str, str]:
        """:data:`CLAUDE_FRESH_CONTEXT_ENV` for a launch that needs a fresh context."""
        return dict(CLAUDE_FRESH_CONTEXT_ENV) if fresh_context else {}

    def fresh_context_probe(self, profile: ProfileConfig) -> RuntimeProbe | None:
        """``<command> --version``, of the profile's own command (R3-F5, R4-F1 of PR
        #513): :data:`CLAUDE_FRESH_CONTEXT_ENV` is known to turn auto memory off
        only on :data:`CLAUDE_FRESH_CONTEXT_VERSIONS`, so the proof holds only
        for an executable that reports one of them. The engine runs it as the
        launch runs (:class:`RuntimeProbe`), and the launch starts the
        file that answered (:meth:`fresh_context_launch`)."""
        return RuntimeProbe(
            command=(profile.command or "claude", "--version"),
            timeout_seconds=CLAUDE_VERSION_PROBE_SECONDS,
            max_output_bytes=CLAUDE_VERSION_PROBE_MAX_OUTPUT_BYTES,
        )

    def fresh_context_runtime(
        self,
        profile: ProfileConfig,
        executable: str,
        result: ExecutionResult | None,
        failure: str = "",
        *,
        identity: ExecutableIdentity | None = None,
    ) -> FreshContextRuntime:
        """The executable that answered, when it reports one of
        :data:`CLAUDE_FRESH_CONTEXT_VERSIONS`; the refusal otherwise.

        Refused, too: no executable on the launch's ``PATH``, a probe that
        could not be run (inside the sandbox included), timed out, exited with
        a failure or printed no version this controller reads, a pre-release,
        and a build not in the list, older or later. Names the version it read
        and the command, never the CLI's output.
        """
        command = profile.command or "claude"
        probe = f"`{command} --version`"
        checked = ", ".join(
            ".".join(str(part) for part in version)
            for version in sorted(CLAUDE_FRESH_CONTEXT_VERSIONS)
        )
        remedy = (
            f"Run a Claude Code build checked to honour {', '.join(CLAUDE_FRESH_CONTEXT_ENV)} "
            f"({checked}) for this profile, or route the phase to another provider"
        )
        if not executable:
            why = f"{command!r} is not on the launch's PATH"
        elif result is None:
            why = f"{probe} could not be run ({redact(failure)})"
        elif result.timed_out:
            why = f"{probe} did not answer within {CLAUDE_VERSION_PROBE_SECONDS}s"
        elif result.exit_code != 0:
            why = f"{probe} exited {result.exit_code} (output not shown)"
        else:
            found, pre = (
                (None, False) if result.stdout_truncated else claude_release(result.stdout or "")
            )
            if found is None:
                why = f"{probe} printed no version this controller reads (output not shown)"
            else:
                shown = ".".join(str(part) for part in found)
                if pre:
                    why = f"{probe} reports a pre-release of {shown}"
                elif found not in CLAUDE_FRESH_CONTEXT_VERSIONS:
                    why = (
                        f"{probe} reports {shown}, and only {checked} is known to turn its "
                        f"auto memory off by {', '.join(CLAUDE_FRESH_CONTEXT_ENV)}: a build "
                        "nobody checked proves nothing"
                    )
                elif identity is None:
                    why = f"{executable} was not held open while it answered"
                else:
                    return FreshContextRuntime(executable, identity, shown)
        raise no_fresh_context(profile, why, remedy)

    def fresh_context_proof(self, profile: ProfileConfig) -> str:
        """``-p`` with ``--no-session-persistence``, and no flag outside
        :data:`CLAUDE_MEMORY_ARGS` (ADR 0015 D14): an allow-list, since a deny
        list would miss a resuming flag a later CLI adds. Each launch that
        needs a fresh context also has its auto memory off
        (:data:`CLAUDE_FRESH_CONTEXT_ENV`), refused while the user's
        configuration would set that variable over it (R2-F7 of PR #513) or
        unless the CLI it starts reports a build that variable was read from
        (:meth:`fresh_context_probe`; R3-F5, R4-F1)."""
        if _truthy(profile.options.get("session_persistence"), default=False) is not False:
            raise no_fresh_context(
                profile,
                "its option session_persistence saves each launch's session for a later one "
                "to resume",
                "Remove the option",
            )
        outside = outside_allow_list(profile.extra_args, CLAUDE_MEMORY_ARGS)
        if outside:
            raise no_fresh_context(
                profile,
                f"its extra_args carry {outside}, and a Memory phase takes only "
                f"{', '.join(sorted(CLAUDE_MEMORY_ARGS))} there",
                "Remove it from extra_args",
            )
        return (
            "claude -p --no-session-persistence, with CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 on a "
            "Claude Code build checked to honour it, which each launch establishes of the "
            "executable it starts: a new session per launch, never saved, and no auto memory "
            "read or written"
        )

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
        # No hook, plugin or MCP server of the operator's or the repository's
        # configuration runs (#165): one may hold a GitHub token of its own.
        argv += CLAUDE_ISOLATION_FLAGS
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
        Once the stream has failed the run, a CLI still running
        :data:`CLAUDE_FAILED_EXIT_GRACE_SECONDS` later is stopped (#394):
        nothing it does after that can change the outcome. Once the stream
        holds a reply that decides the run (:attr:`ClaudeStream.replied`), a
        CLI still running :data:`CLAUDE_REPLIED_EXIT_GRACE_SECONDS` later is
        stopped too and the reply stands (#439), as it does when a limit falls
        due within that grace or with the reply received but not yet read: the
        timeout would otherwise discard it. Neither happens on a reply that
        lines already received overtake, or after lines were lost to the
        queue's bound (:func:`_read_claude_stream`).
        ``exit_code`` is always the real process status, -1 when the CLI was
        killed. A failure that is a turn ended by a transient model API error
        is also a ``transient_failure`` (#395); ``text`` output carries no
        record to tell it by. A stream that, read to its end with no line
        skipped past the per-line bound and every tool call it started tracked
        to its end, holds a reply that decides the run is ``reply_delivered``
        whatever the CLI did after it (#479, R3-F1 and R4-F1 of PR #481),
        which the engine reads for a CLI that then exited with a failing
        status; ``text`` output vouches for no reply.
        """
        if self.output_format(req.profile) != "stream-json":
            return super().execute(req)
        emit = guarded(req.progress)
        monitor = _loop_monitor(req, emit)
        stream = ClaudeStream(
            cwd=req.cwd, emit=emit, loop=_ClockedLoop(monitor), is_reply=holds_control_result
        )
        self.integration_preflight(req.env_allowlist, fresh_context=req.fresh_context)
        with self.integration_isolation() as isolation:
            env = {
                **(req.env or {}),
                **isolation,
                **self.fresh_context_environment(req.fresh_context),
            }
            command, description = self.sandboxed(req, self.build_command(req), env)
            started = self.started_environment(req, env, description)
            with self.fresh_context_launch(req, command, started) as (command, program):
                self.complete_isolation(isolation, started)
                duplex = DuplexRequest(
                    command=command,
                    cwd=req.cwd,
                    env_allowlist=self.environment_allowlist(req),
                    env=env,
                    idle_timeout_seconds=req.idle_timeout_seconds,
                    max_runtime_seconds=req.max_runtime_seconds,
                    max_record_bytes=CLAUDE_MAX_RECORD_BYTES,
                    max_pending_bytes=CLAUDE_MAX_PENDING_BYTES,
                    # The prompt travels in argv; stdin stays /dev/null so the CLI
                    # never waits on it.
                    stdin_pipe=False,
                    # Nothing an agent starts outlives its invocation (ADR 0002).
                    contain_orphans=True,
                    sandbox=description,
                    executable_fd=program,
                )
                with start_duplex(duplex) as child:
                    cut = _read_claude_stream(child, stream, monitor)
                    res = child.finish()
        replied = cut is _Cut.REPLIED
        timed_out = not replied and (cut in (_Cut.DEADLINE, _Cut.LOOP) or res.timed_out)
        failure = stream.failure
        if cut is _Cut.FAILED:
            # A stream that failed at EOF said "exited": the CLI had only
            # closed its stdout (R1-F1).
            closed = " closed its stdout but" if stream.exited_early else ""
            failure = (
                f"{failure}; claude{closed} was still running "
                f"{CLAUDE_FAILED_EXIT_GRACE_SECONDS:g}s later and was stopped"
            )
        elif stream.exited_early:
            failure = f"{failure} (exit {res.exit_code})"
        elif res.records_overflowed and failure is None:
            failure = "claude: stream-json lines arrived faster than they were read"
        summary = stream.summary()
        summary["failure"] = failure or ""
        text = stream.text if failure is None else None
        # #434: a turn ended at a usage or rate limit, by the failing result's
        # status or, when that result never arrived, by the CLI's own typed
        # API-error message (R3-F1 of PR #480). Established by the stream, it
        # stands whatever ended the CLI after it: one still running at its
        # idle limit during the failure's exit grace was refused at that limit
        # all the same, and a relaunch now meets it again (R1-F2). Only for a
        # run that failed: a limit a subagent met in a run that then succeeded
        # is no outcome.
        usage_limit = (stream.limit or stream.api_limit) if timed_out or failure else None
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
            stopped_on_failure=cut is _Cut.FAILED,
            stopped_on_reply=replied,
            # A failure the stream recognised as a transient model API error
            # (#395) stays one when the CLI was stopped after it (#394); never
            # beside a usage limit (#434).
            transient_failure=(
                None if timed_out or failure is None or usage_limit else stream.transient
            ),
            usage_limit=usage_limit,
            # #434: the stream had failed the run (an authentication or
            # permission refusal, a protocol violation) before a limit killed
            # the CLI, which drops the failure itself; one still running at its
            # idle limit inside the failure's exit grace failed all the same
            # (R2-F1 of PR #480). So had a CLI that reported a model API error
            # in its own typed message and was killed before the result that
            # would have failed the run (R3-F1). Its fixed kind, never the
            # stream's text.
            failed_before_limit=(stream.failure_kind or stream.api_error) if timed_out else None,
            # #479: the stream, read to its end, holds a reply that decides the
            # run and failed nothing (an overflow is a failure). A limit that
            # finds one is no timeout (#439), so a launch whose reply this
            # leaves for the engine to decide on is one that exited with a
            # failing status after it, or that the loop detector stopped.
            # Only on a stream of which no line was skipped past the per-line
            # bound (ClaudeStream.reply_proven, R3-F1 of PR #481): a record
            # never read may have opened a later turn or a tool call, or
            # failed the run; and only with every tool call the stream
            # started tracked to its end (R4-F1): one without a usable id or
            # past the reducer's bound is never counted open, so no result
            # shows it ended. #439's stop on a reply reads ``replied`` as
            # before.
            reply_delivered=failure is None and stream.reply_proven,
            # #434: a CLI that died inside a running session before any
            # result, on its own (never one a limit or the adapter stopped),
            # with no line lost on the way and no model API error reported
            # (ClaudeStream.result_less).
            result_less_exit=(
                cut is None
                and not timed_out
                and failure is not None
                and stream.result_less
                and not res.records_overflowed
            ),
            provider_summary=summary,
            timeout_limit="" if replied else res.timeout_limit,
            last_activity_at=res.last_activity_at,
            sandbox=res.sandbox,
        )
        result = _finish_loop(result, monitor, killed=cut is _Cut.LOOP)
        # A refusal never ends a -p run (claude_stream.py): the agent goes on
        # without what it asked for, so a failure is never the refusal's alone.
        return _name_refused(result, stream.refused, stream.refused_count, self.name, "Claude Code")


class _Cut(enum.Enum):
    """Why :func:`_read_claude_stream` ended an invocation whose CLI was still running."""

    # A limit fell due: a timeout.
    DEADLINE = "deadline"
    # Stopped on the loop detector's verdict (#194): a timeout whose limit is ``loop``.
    LOOP = "loop"
    # Stopped once the stream had failed the run (#394): that failure.
    FAILED = "failed"
    # Stopped, or reached by a limit, once a reply had decided the run (#439): that reply.
    REPLIED = "replied"


def _read_claude_stream(
    child: DuplexChild, stream: ClaudeStream, loop: LoopMonitor | None = None
) -> _Cut | None:
    """Feed every stdout outcome to ``stream`` through its end; what cut it short, if anything.

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

    So does a stream that has failed the run with the CLI still running
    :data:`CLAUDE_FAILED_EXIT_GRACE_SECONDS` after the driver first saw the
    failure (#394): a later turn cannot redeem it and the reducer reads
    nothing more, so the CLI would otherwise go on working, unseen and for
    nothing, until its ceiling, its output keeping the idle limit away. A
    limit that falls due within the grace still wins. EOF ends neither the
    grace nor the wait (R1-F1): a CLI that closed its stdout after the
    failure, or whose stream EOF fails for want of a result, is waited for
    and stopped the same way, where ``finish()`` would wait for it until a
    limit. Only EOF on a stream that has neither failed nor been decided by a
    reply, or from a CLI that has exited, ends the wait.

    A CLI still running :data:`CLAUDE_REPLIED_EXIT_GRACE_SECONDS` after the
    stream first held a reply that decides the run is stopped the same way,
    its output not moving the grace (#439). Unlike a failure, a reply can be
    overtaken inside the grace: a turn that opens is waited for, as a turn
    that may revise the reply, and its own ``result`` decides again with a
    grace of its own. Past that grace the agent's pending work is not waited
    for, which ``prompts/common.md`` tells it. A limit that falls due within
    the grace does not outrank the reply either: it would end the CLI just
    as a stop does, and a timeout would discard the reply.

    The reducer can be behind the reader, so the lines that overtake a reply
    can already be queued when its grace runs out or a limit falls due. So
    the stop waits until every line queued has been read, each going through
    the rules above, and an overflow among them fails the run as any overflow
    does: the lines it dropped could have been a later turn or a failure. A
    limit does not wait: what it left unread is read after the kill, whatever
    the stream held before it, since the reply can be among those lines as well
    as what overtakes one. The run completes only on a reply that decides it
    on all of them, and otherwise the limit is the outcome. Either way the
    queue is sealed at that moment, so lines written while the CLI dies neither
    revise the reply nor count as an overflow.
    """
    failed_at: float | None = None
    replied_at: float | None = None
    ended = False
    while True:
        item = child.read_line(timeout=CLAUDE_EXIT_POLL_SECONDS)
        if isinstance(item, Eof):
            if not ended:
                ended = True
                stream.stream_ended()
            if child.exited() or not (stream.failure is not None or stream.replied):
                return None
        elif isinstance(item, Timeout):
            if item.deadline_exceeded:
                for unread in child.unread_at_deadline():
                    _reduce(stream, unread)
                return _Cut.REPLIED if stream.replied else _Cut.DEADLINE
        else:
            _reduce(stream, item)
            if isinstance(item, Overflow):
                return None
        if loop is not None and loop.verdict is not None and child.stop():
            return _Cut.LOOP
        if child.exited():
            child.finish()
            continue
        now = time.monotonic()
        if stream.failure is not None:
            failed_at = now if failed_at is None else failed_at
            stop_at, cut = failed_at + CLAUDE_FAILED_EXIT_GRACE_SECONDS, _Cut.FAILED
        elif stream.replied:
            replied_at = now if replied_at is None else replied_at
            stop_at, cut = replied_at + CLAUDE_REPLIED_EXIT_GRACE_SECONDS, _Cut.REPLIED
        else:
            replied_at = None
            continue
        if now >= stop_at and child.stop(once_read=cut is _Cut.REPLIED):
            return cut
        if ended:
            # Past EOF every read returns at once: wait for the CLI here,
            # never past the grace or the deadline, which the next read
            # enforces as it does before EOF.
            time.sleep(
                max(0.0, min(CLAUDE_EXIT_POLL_SECONDS, stop_at - now, child.deadline() - now))
            )


def _reduce(stream: ClaudeStream, item: Record | Oversize | Fragment | Overflow) -> None:
    """Give ``stream`` one thing read from stdout, short of its end."""
    if isinstance(item, Record):
        stream.feed(item.data)
    elif isinstance(item, Oversize):
        stream.oversize(item.limit)
    elif isinstance(item, Fragment):
        stream.framing_error("stdout ended inside an unterminated line")
    else:
        stream.framing_error("stdout lines arrived faster than they were read")


def opencode_isolation_env(config_dir: str) -> dict[str, str]:
    """What an OpenCode launch sets so that it reads no configuration of the
    operator's or the repository's (#165): no flag of 2.0.26 disables an MCP
    server or a plugin, and either may hold a GitHub token of its own.

    - ``OPENCODE_CONFIG_DIR``: the global configuration directory, instead
      of ``$XDG_CONFIG_HOME/opencode``; ``config_dir`` is empty, so no
      ``opencode.json(c)`` and no ``plugin(s)/`` directory is read from it.
    - ``OPENCODE_CONFIG_PROJECT_DISABLE`` and its older name
      ``OPENCODE_DISABLE_PROJECT_CONFIG``: no ``opencode.json(c)``,
      ``.opencode/``, ``.claude/`` or ``.agents/`` of the launch directory or
      of any directory above it (the search does not stop at the worktree, so
      it would reach the operator's checkout and home). OpenCode reads the
      first name and falls back to the second, so setting both leaves no
      value of the operator's for either in force.
    - ``OPENCODE_CONFIG`` and ``OPENCODE_CONFIG_CONTENT`` empty: no explicit
      file and no inline document, whatever the operator's shell passed
      through ``OPENCODE_*``.

    The model credentials (``opencode auth login``, kept in the data
    directory) and the providers' API-key variables are untouched; so is
    the organization configuration kept with them, which no variable turns
    off and which refuses the launch instead
    (:func:`opencode_organization_problems`).
    """
    return {
        "OPENCODE_CONFIG_DIR": config_dir,
        "OPENCODE_CONFIG_PROJECT_DISABLE": "1",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_CONFIG": "",
        "OPENCODE_CONFIG_CONTENT": "",
    }


# Where OpenCode 2.0.26 keeps the organizations connected to it (a
# ``.well-known/opencode`` integration, read from the build): the ``kv``
# row ``wellknown:sources`` of its database lists their origins. Every
# launch, ``Config.load`` fetches each one's manifest and configuration
# again and, without a usable credential for it (``opencode auth logout``
# removes only the credential), falls back to the copies cached in the same
# table; either may name an MCP server or a plugin holding a GitHub
# credential of its own, and no variable or flag keeps it from loading.
# ``auth.json``, the credential file of earlier releases, is imported when
# a database is created: an entry of ``"type": "wellknown"`` there becomes
# a source.
OPENCODE_WELLKNOWN_SOURCES = "wellknown:sources"
OPENCODE_LEGACY_AUTH_FILE = "auth.json"
OPENCODE_MAX_AUTH_BYTES = 1024 * 1024
# How long reading the database may wait for an OpenCode writing to it.
OPENCODE_DATABASE_TIMEOUT_SECONDS = 5


def opencode_data_dir(env: Mapping[str, str]) -> Path:
    """The data directory of an OpenCode launch with ``env``:
    ``$XDG_DATA_HOME/opencode``, else ``~/.local/share/opencode`` (``HOME``,
    else the password database's home), where 2.0.26 keeps its database."""
    base = env.get("XDG_DATA_HOME")
    if not base:
        home = env.get("HOME")
        if not home:
            try:
                home = pwd.getpwuid(os.getuid()).pw_dir
            except KeyError:
                home = "/"
        base = os.path.join(home, ".local", "share")
    return Path(base, "opencode")


def opencode_databases(env: Mapping[str, str], data_dir: Path) -> list[Path]:
    """The databases an OpenCode launch with ``env`` may open: the file
    ``OPENCODE_DB`` names (relative to ``data_dir``; ``:memory:`` is none),
    else each ``opencode*.db`` in ``data_dir`` (the name depends on the
    build's release channel)."""
    named = env.get("OPENCODE_DB")
    if named is not None:
        return [] if named == ":memory:" else [data_dir / named]
    return sorted(data_dir.glob("opencode*.db"))


def _not_a_regular_file(path: Path) -> str | None:
    """Why ``path``, which exists, is not opened (a FIFO would block the
    launch forever, a directory has nothing to read); None for a file."""
    if path.is_file():
        return None
    return f"{path} could not be checked (not a regular file)"


def _opencode_database_problems(database: Path) -> list[str]:
    """The organizations ``database`` lists, or that it could not be read.

    Opened read-only (a URI with ``mode=ro``), never created or written;
    only the sources row is read, and only the number of origins is
    reported, never one.
    """
    if not database.exists():
        return []
    refused = _not_a_regular_file(database)
    if refused:
        return [refused]
    try:
        uri = f"{database.resolve().as_uri()}?mode=ro"
        with closing(
            sqlite3.connect(uri, uri=True, timeout=OPENCODE_DATABASE_TIMEOUT_SECONDS)
        ) as db:
            tables = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'kv'")
            if tables.fetchone() is None:
                return []
            row = db.execute(
                "SELECT value FROM kv WHERE key = ?", (OPENCODE_WELLKNOWN_SOURCES,)
            ).fetchone()
    except (sqlite3.Error, OSError, ValueError) as exc:
        return [f"{database} could not be checked ({type(exc).__name__})"]
    if row is None:
        return []
    try:
        sources = json.loads(row[0])
    except (TypeError, ValueError):
        sources = None
    if sources == []:
        return []
    if not isinstance(sources, list):
        return [f"{database} could not be checked (an unreadable list of organizations)"]
    return [f"{len(sources)} organization(s) connected to OpenCode in {database}"]


def _opencode_legacy_auth_problems(data_dir: Path) -> list[str]:
    """An organization in the legacy ``auth.json``, or that it could not be read.

    Parsed in memory only: no value of the file is ever reported.
    """
    path = data_dir / OPENCODE_LEGACY_AUTH_FILE
    if not path.exists():
        return []
    refused = _not_a_regular_file(path)
    if refused:
        return [refused]
    try:
        with path.open("rb") as handle:
            data = handle.read(OPENCODE_MAX_AUTH_BYTES + 1)
    except FileNotFoundError:
        return []
    except OSError as exc:
        return [f"{path} could not be checked ({type(exc).__name__})"]
    if len(data) > OPENCODE_MAX_AUTH_BYTES:
        return [f"{path} is larger than {OPENCODE_MAX_AUTH_BYTES} bytes and was not checked"]
    try:
        entries = json.loads(data)
    except ValueError:
        return [f"{path} could not be checked (not JSON)"]
    if isinstance(entries, dict) and any(
        isinstance(entry, dict) and entry.get("type") == "wellknown" for entry in entries.values()
    ):
        return [
            f'an organization (a "wellknown" entry) in {path}, which OpenCode imports '
            "into every database it creates"
        ]
    return []


def opencode_organization_problems(env: Mapping[str, str]) -> list[str]:
    """What, in the data an OpenCode launch with ``env`` reads, would make it
    load an organization's configuration (empty: nothing would).

    ``env`` is the environment the launch inherits (``HOME``,
    ``XDG_DATA_HOME`` and ``OPENCODE_DB`` decide where 2.0.26 looks). A
    database or file that exists but cannot be read is a problem too: a
    launch is refused rather than assumed clean.
    """
    data_dir = opencode_data_dir(env)
    problems: list[str] = []
    for database in opencode_databases(env, data_dir):
        problems += _opencode_database_problems(database)
    problems += _opencode_legacy_auth_problems(data_dir)
    return problems


# The API-key variable OpenCode reads for a model provider, under the
# container backend (#290): names present in the 2.3.0 build, for the
# providers AutoForge documents. A provider not listed has no container
# route; its login, OpenCode's account store, is never projected.
OPENCODE_KEY_NAMES = {
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "google": "GOOGLE_GENERATIVE_AI_API_KEY",
    "openai": "OPENAI_API_KEY",
}
# Where OpenCode keeps its account store, in a container launch: a database
# file in the private home, which starts empty, so a stored account never
# takes precedence over the projected key.
OPENCODE_PRIVATE_STORES = (PrivateStore("OPENCODE_DB", ".local/share/opencode/opencode.db"),)
# OpenCode's integration isolation in a container launch (#165; ADR 0013 D7):
# no flag keeps an MCP server, a plugin or the repository's configuration
# off, so the plan sets :func:`opencode_isolation_env` itself, whatever the
# launch's environment held, with a global configuration directory of the
# private home, which starts empty. Run against 2.3.0 with a fresh home and
# only the private home and database set, `opencode api --standalone GET
# /api/config` loaded the workspace's `opencode.json` and `.opencode/`; with
# these as well, only that directory, which OpenCode made.
OPENCODE_CONTAINER_ISOLATION = tuple(
    sorted(opencode_isolation_env(private_path(".config/opencode")).items())
)


class OpenCodeProvider(AgentProvider):
    name = "opencode"
    # OpenCode's own settings plus the API keys of the model providers it
    # routes to; a provider not listed here is added through
    # ``execution.agent_env_allowlist_extra``.
    environment_names = ("OPENCODE_*", "OPENAI_*", "ANTHROPIC_*", "GEMINI_*", "GOOGLE_*")
    # Claude Code's, which ``ANTHROPIC_*`` passes here too when both are routed.
    sign_in_names = ("ANTHROPIC_AUTH_TOKEN",)
    option_keys = ("output_format", "auto_approve")
    isolation_names = tuple(sorted(opencode_isolation_env("")))
    path_names = ("OPENCODE_CONFIG_DIR", "OPENCODE_DB")

    def sandbox_requirements(
        self, profile: ProfileConfig, command: list[str], env: Mapping[str, str], home: str
    ) -> ProviderRequirements:
        """OpenCode's declaration (ADR 0005 D7.1, D13.1).

        The launch's empty ``OPENCODE_CONFIG_DIR`` writable (it is the
        launch's own, removed afterwards). An absolute ``OPENCODE_DB``: its
        directory writable at its own path (``dir-rw``: the database holds
        the login and is rewritten), refused when it is or lies in the
        operator's data directory (D7.2). A relative or ``:memory:`` one
        names a database inside the private home. The providers' API keys
        are ``env`` items. The configuration, data and cache directories
        OpenCode loads settings, organizations and code from are the
        operator configuration roots.
        """
        needs = super().sandbox_requirements(profile, command, env, home)
        state: list[str] = []
        auth = [AuthItem(AuthMode.ENV, name) for name in env if name.endswith("_API_KEY")]
        config_dir = env.get("OPENCODE_CONFIG_DIR", "")
        if config_dir.startswith("/"):
            state.append(config_dir)
        database = env.get("OPENCODE_DB", "")
        if database.startswith("/"):
            auth.append(
                AuthItem(
                    AuthMode.DIR_RW,
                    "the OpenCode database directory ($OPENCODE_DB)",
                    os.path.dirname(database),
                )
            )
        config_home = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        cache_home = env.get("XDG_CACHE_HOME") or os.path.join(home, ".cache")
        roots = (
            os.path.join(config_home, "opencode"),
            str(opencode_data_dir({**env, "HOME": home})),
            os.path.join(cache_home, "opencode"),
        )
        return dataclasses.replace(
            needs,
            state=(*needs.state, *state),
            auth=tuple(auth),
            operator_config_roots=roots,
        )

    def credential_routes(self, profile: ProfileConfig) -> CredentialRoutes:
        """The API key of the model's provider alone (:data:`OPENCODE_KEY_NAMES`),
        never another provider's key nor ``OPENCODE_*`` or ``OPENAI_*``
        wholesale. The account store (the database ``auth login`` writes,
        ``$OPENCODE_DB``) is never projected: OpenCode rewrites it and refreshes
        the logins it holds. Inside the container ``OPENCODE_DB`` names a new
        database in the private home (:data:`OPENCODE_PRIVATE_STORES`), whatever
        the launch's environment held, so the store is empty and the key is
        what the launch authenticates with. The integration isolation every
        launch sets (:func:`opencode_isolation_env`) is set there too, with an
        empty configuration directory of the private home
        (:data:`OPENCODE_CONTAINER_ISOLATION`), never taken from the launch's
        environment."""
        scope = profile.model.split("/", 1)[0] if "/" in profile.model else ""
        name = OPENCODE_KEY_NAMES.get(scope)
        if name is None:
            return CredentialRoutes(
                scope or "(no provider)",
                refusal=(
                    f"no API-key variable is mapped for OpenCode's model provider {scope!r} "
                    f"(mapped: {', '.join(sorted(OPENCODE_KEY_NAMES))}), and OpenCode's "
                    "account store is never projected: route the profile to a mapped provider"
                ),
            )
        return CredentialRoutes(
            scope,
            (EnvRoute(name, CredentialKind.API_KEY),),
            stores=OPENCODE_PRIVATE_STORES,
            isolation=OPENCODE_CONTAINER_ISOLATION,
        )

    @contextmanager
    def integration_isolation(self) -> Iterator[dict[str, str]]:
        """An empty global configuration directory, for this launch only, and
        no project, explicit or inline configuration (:func:`opencode_isolation_env`).

        The directory (``mkdtemp``, mode 0700) is removed when the launch
        ends, whatever OpenCode wrote into it; one that cannot be made is an
        :class:`ExecutionError`, before anything is launched.
        """
        try:
            config_dir = tempfile.mkdtemp(prefix="autoforge-opencode-")
        except OSError as exc:
            raise ExecutionError(f"cannot prepare the opencode configuration: {exc}") from exc
        try:
            yield opencode_isolation_env(config_dir)
        finally:
            shutil.rmtree(config_dir, ignore_errors=True)

    def data_dir(self, env_allowlist: tuple[str, ...] | None) -> Path:
        """The data directory a launch with ``env_allowlist`` uses (:func:`opencode_data_dir`)."""
        return opencode_data_dir(self.launch_environment(env_allowlist))

    def integration_preflight(
        self, env_allowlist: tuple[str, ...] | None, *, fresh_context: bool = False
    ) -> None:
        """Refuse a launch while OpenCode would load an organization's
        configuration (:func:`opencode_organization_problems`): 2.0.26 loads
        it ahead of every configuration file, and no variable of
        :func:`opencode_isolation_env` reaches it.
        """
        problems = opencode_organization_problems(self.launch_environment(env_allowlist))
        if problems:
            raise ConfigurationError(
                "refusing to launch opencode: it would load the configuration of an "
                "organization connected to it (a .well-known/opencode integration), which can "
                "name an MCP server or a plugin holding a GitHub credential of its own, and "
                f"nothing keeps it from loading: {'; '.join(problems)}. Give AutoForge's "
                "launches an OpenCode database without one (set OPENCODE_DB, which every "
                "launch inherits, to a file of their own and log in to the model provider "
                "with it set, or to ':memory:' with the provider's API-key variable), remove "
                'a "wellknown" entry from auth.json, or route these profiles to another '
                "provider (docs/configuration.md, 'Agent environment')"
            )

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

    def fresh_context_proof(self, profile: ProfileConfig) -> str:
        """``run --standalone`` with no session flag, and no flag outside
        :data:`OPENCODE_MEMORY_ARGS` (ADR 0015 D14)."""
        outside = outside_allow_list(profile.extra_args, OPENCODE_MEMORY_ARGS)
        if outside:
            raise no_fresh_context(
                profile,
                f"its extra_args carry {outside}, and a Memory phase takes only "
                f"{', '.join(sorted(OPENCODE_MEMORY_ARGS))} there",
                "Remove it from extra_args",
            )
        return "opencode run --standalone: a new session per launch on a private server"

    def stdin_payload(self, req: AgentRequest) -> bytes:
        # A lone surrogate (legal in the JSON GitHub returns, not in UTF-8)
        # becomes U+FFFD rather than failing the launch.
        return encodable(req.prompt).encode("utf-8")

    def execute(self, req: AgentRequest) -> AgentExecutionResult:
        result = super().execute(req)
        if (
            result.exit_code == 1
            and not result.timed_out
            and not result.stderr_truncated
            and req.profile.model.partition("/")[0] == "openai"
        ):
            errors = _opencode_run_errors(result.stderr)
            if errors and all(e in _OPENCODE_OPENAI_TRANSIENT_ERRORS for e in errors):
                reasons = dict.fromkeys(_OPENCODE_OPENAI_TRANSIENT_ERRORS[e] for e in errors)
                result.transient_failure = "opencode: " + ", ".join(reasons)
        if (result.failed_exit or result.timed_out) and _OPENCODE_USAGE_LIMIT_ERROR in (
            _opencode_run_errors(result.stderr)
        ):
            # #434: OpenCode's run-level usage-limit error, whatever the model:
            # a launch now meets the same limit, and the error says nothing of
            # when it resets. Once printed it stands whatever ended the CLI
            # after it, a limit included: one that hung after the error until
            # its idle limit was refused at the usage limit all the same
            # (R1-F2 of PR #480).
            result.usage_limit = f"opencode: {_OPENCODE_USAGE_LIMIT_ERROR}"
            result.transient_failure = None
        if result.timed_out and _opencode_run_errors(result.stderr):
            # #434: a run-level error, which fails the run, printed before a
            # limit killed the CLI: the run had failed (an authentication or
            # permission refusal among them), and the kill does not make it an
            # idle timeout to relaunch (R2-F1 of PR #480). Its message is not
            # read for this.
            result.failed_before_limit = _OPENCODE_FAILED_BEFORE_LIMIT
        refused = _opencode_refused_requests(result.stderr)
        ended_by_it = ""
        if (
            refused
            and result.exit_code == 1
            and result.provider_failure is None
            and not result.stderr_truncated
            and not _opencode_run_errors(result.stderr)
        ):
            # An exit 1 with no run-level error of its own: what the 2.0.x
            # builds did once they had refused a permission (module docstring).
            remedy = (
                "Keep the issue's work inside the worktree, or change the issue so the agent "
                "need not ask; options.auto_approve: true approves every permission no rule "
                "denies, and is dangerous"
                if any(kind is RequestKind.PERMISSION for kind, _ in refused)
                else "Change the issue so the agent need not ask"
            )
            if any(kind is RequestKind.QUESTION for kind, _ in refused):
                remedy += "; a question cannot be answered until #126 lands"
            ended_by_it = (
                f"OpenCode refused {'them' if len(refused) > 1 else 'it'} and exited 1 with no "
                f"error of its own. {remedy}. 'resume' starts a fresh session, which may ask "
                f"again ({REFUSED_REQUEST_GUIDE})"
            )
        return _name_refused(
            result, refused, len(refused), self.name, "OpenCode", ended_by_it=ended_by_it
        )


# How OpenCode 2.0.26 prints an error on stderr: `ESC[91mESC[1mError: ESC[0m`
# and the message's first line (no terminal check), and a failed tool: its
# `ESC[0m✗ ESC[0m<title> failed` line, then the tool's error in the same form.
_OPENCODE_ERROR_LINE_RE = re.compile(r"\x1b\[91m\x1b\[1mError: \x1b\[0m(.*)")
_OPENCODE_TOOL_FAILED_LINE_RE = re.compile(r"\x1b\[0m✗ \x1b\[0m.* failed")
# The run-level errors that make an `openai/*` exit 1 transient, each the
# whole message OpenCode prints, mapped to the fixed reason reported for it:
# the socket to OpenAI closed under a running turn, normally (1000, #164) or
# with the policy code (1008, #226), and OpenAI's generic server error, whose
# own text says to retry (#226). A message that only starts the same way
# (another `server_error` text, one naming a request ID) is not listed.
_OPENCODE_OPENAI_TRANSIENT_ERRORS = {
    "WebSocket closed with code 1000": "OpenAI WebSocket closed with code 1000",
    "WebSocket closed with code 1008": "OpenAI WebSocket closed with code 1008",
    (
        "server_error: An error occurred while processing your request. You can retry "
        "your request, or contact us through our help center at help.openai.com if the "
        "error persists."
    ): "OpenAI server_error",
}


# The run-level error OpenCode prints when the provider's usage limit is
# reached, as #434's survey recorded it (reported from real runs, not run
# against a CLI here): the whole message, compared whole. It names no reset
# time.
_OPENCODE_USAGE_LIMIT_ERROR = "The usage limit has been reached"
# What a timed-out run whose stderr holds a run-level error reports (#434).
_OPENCODE_FAILED_BEFORE_LIMIT = "opencode: a run-level error was printed"


# How OpenCode prints a request it refuses (#258; 2.1.4, run against a
# loopback stand-in, and read from its build). A permission an agent's rule
# asks for, without `--auto`: `ESC[93mESC[1m! ESC[0mpermission requested:
# <permission> (<patterns, comma-separated>); auto-rejecting`. A question,
# cancelled: the question tool's failed line, then this as its error line.
_OPENCODE_PERMISSION_LINE_RE = re.compile(
    r"\x1b\[93m\x1b\[1m! \x1b\[0mpermission requested: (\S+) \((.*)\); auto-rejecting"
)
_OPENCODE_QUESTION_CANCELLED = (
    "This non-interactive run cannot ask the user questions, so the question was cancelled. "
    "Continue without an answer; make reasonable assumptions and state them."
)


def _opencode_refused_requests(stderr: str) -> list[tuple[RequestKind, str]]:
    """The requests OpenCode printed on ``stderr`` that it refused, in order:
    each permission line, with its permission and patterns as the subject,
    and each failed tool whose error is the cancelled question's (no subject:
    OpenCode prints no question text). Lines as in :func:`_opencode_run_errors`;
    text that is not such a line on its own (a diff, a file, a command's
    output that quotes one) is no request."""
    refused: list[tuple[RequestKind, str]] = []
    previous = ""
    for line in stderr.split("\n"):
        line = line.removesuffix("\r")
        permission = _OPENCODE_PERMISSION_LINE_RE.fullmatch(line)
        error = _OPENCODE_ERROR_LINE_RE.fullmatch(line)
        if permission:
            subject = clean(f"{permission[1]}: {permission[2]}", MAX_REFUSED_SUBJECT_CHARS)
            refused.append((RequestKind.PERMISSION, subject))
        elif (
            error
            and error[1] == _OPENCODE_QUESTION_CANCELLED
            and _OPENCODE_TOOL_FAILED_LINE_RE.fullmatch(previous)
        ):
            refused.append((RequestKind.QUESTION, ""))
        previous = line
    return refused


def _opencode_run_errors(stderr: str) -> list[str]:
    """The run-level errors OpenCode printed on ``stderr``, in order: each
    error line's message, except a failed tool's (the line right after its
    ``✗ ... failed`` line), which does not fail the run. Text that is not
    such a line, wherever it says ``Error:``, is not an error. Lines end
    where OpenCode ends them (``os.EOL``), not at every character
    :meth:`str.splitlines` breaks on."""
    errors: list[str] = []
    previous = ""
    for line in stderr.split("\n"):
        line = line.removesuffix("\r")
        error = _OPENCODE_ERROR_LINE_RE.fullmatch(line)
        if error and not _OPENCODE_TOOL_FAILED_LINE_RE.fullmatch(previous):
            errors.append(error.group(1))
        previous = line
    return errors


# #186: the CLI whose argv and stdin delivery the adapter speaks. No upper bound.
OPENCODE_MIN_VERSION = (2, 0, 0)
# What `opencode --version` prints: `opencode v2.0.23`; 1.x printed `1.18.34`.
_OPENCODE_VERSION_RE = re.compile(r"(?:opencode )?v?(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?")


def parse_opencode_version(text: str) -> tuple[int, int, int] | None:
    """``(major, minor, patch)`` from ``opencode --version`` output, or None
    when unreadable; a pre-release sorts below its release, as for Pi."""
    return _parse_version(text, _OPENCODE_VERSION_RE)


PI_EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
# What a timed-out run whose conversation had already failed reports (#434).
PI_FAILED_BEFORE_LIMIT = "pi: the RPC conversation had failed the run"
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
# The per-record bound is sized for one message: a final text past the
# stdout bound must still arrive whole in one `get_last_assistant_text`
# record so that its tail can be kept, and JSON escaping can double a text.
# It is not Pi's worst case, which has no bound: `agent_end` repeats every
# message of the run (207 KB for a 200 KB prompt and one `read` on Pi
# 1.0.1, and compaction does not shrink it), and `turn_end` a turn's
# assistant message and all its tool results. Those two are dropped past
# the bound and tolerated from their leading bytes, since the reducer needs
# neither's content; any other record past it is a protocol violation
# (`PiConversation.oversize`, #144). Memory stays at the bound either way.
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
# The API-key variable Pi reads for a model provider when a profile sets
# `require_oauth: false`, under the container backend (#290; docs/pi.md).
PI_KEY_NAMES = {"deepseek": "DEEPSEEK_API_KEY", "openai": "OPENAI_API_KEY"}
# Where Pi keeps its credential store and model state, in a container launch:
# an empty agent directory in the private home, so a key stored in an
# inherited one never takes precedence over the projected key.
PI_PRIVATE_STORES = (PrivateStore("PI_CODING_AGENT_DIR", ".pi/agent"),)
# Pi's switches a container launch receives from the launch's environment:
# none is a credential or a path.
PI_CONTAINER_SETTINGS = ("PI_OFFLINE", "PI_SKIP_VERSION_CHECK", "PI_TELEMETRY")
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
    # `execution.agent_env_allowlist_extra` or `env_allowlist_extra`, never by default.
    environment_names = (
        "PI_CODING_AGENT_DIR",
        "PI_OFFLINE",
        "PI_SKIP_VERSION_CHECK",
        "PI_TELEMETRY",
    )
    option_keys = ("require_oauth", "tools", "context_files")
    path_names = ("PI_CODING_AGENT_DIR",)

    def sandbox_requirements(
        self, profile: ProfileConfig, command: list[str], env: Mapping[str, str], home: str
    ) -> ProviderRequirements:
        """Pi's declaration (ADR 0005 D7.1, D13.1; docs/pi-policy.md §8).

        An absolute ``PI_CODING_AGENT_DIR``: that directory writable at its
        own path (``dir-rw``: Pi writes ``auth.json`` and
        ``models-store.json`` there by rename at startup), refused when it is
        or lies in ``~/.pi/agent``, the operator configuration root (D7.2).
        Without one, Pi finds no login inside, and its auth preflight says
        so. A provider key is an ``env`` item.
        """
        needs = super().sandbox_requirements(profile, command, env, home)
        auth = [AuthItem(AuthMode.ENV, name) for name in PI_API_KEY_NAMES if env.get(name)]
        agent_dir = env.get("PI_CODING_AGENT_DIR", "")
        if agent_dir.startswith("/"):
            auth.append(
                AuthItem(
                    AuthMode.DIR_RW,
                    "Pi's agent directory ($PI_CODING_AGENT_DIR)",
                    agent_dir,
                    env_name="PI_CODING_AGENT_DIR",
                )
            )
        default = os.path.join(home, PI_DEFAULT_AGENT_DIR.removeprefix("~/"))
        return dataclasses.replace(needs, auth=tuple(auth), operator_config_roots=(default,))

    def credential_routes(self, profile: ProfileConfig) -> CredentialRoutes:
        """With ``require_oauth: false``, the API key of the model's provider
        alone (:data:`PI_KEY_NAMES`). With ``require_oauth`` (the default), none:
        the ChatGPT sign-in lives in Pi's agent directory, which Pi refreshes in
        place by rotating its refresh token, and no controller-owned path
        returns a refreshed sign-in to the host, so it is never projected. Nor
        is a key Pi holds in its own store: inside the container
        ``PI_CODING_AGENT_DIR`` names an empty directory of the private home
        (:data:`PI_PRIVATE_STORES`), whatever the launch's environment held."""
        scope = self.model_provider(profile)
        if self.require_oauth(profile):
            return CredentialRoutes(
                scope,
                refusal=(
                    "require_oauth: Pi's ChatGPT sign-in is refreshed in Pi's agent directory "
                    "by rotating its refresh token, and no controller-owned path returns a "
                    "refreshed sign-in to the host, so it is never projected (ADR 0013 D14): "
                    "run this profile under bwrap, or set require_oauth: false with a "
                    "provider API key"
                ),
            )
        name = PI_KEY_NAMES.get(scope)
        if name is None:
            return CredentialRoutes(
                scope,
                refusal=(
                    f"no API-key variable is mapped for Pi's model provider {scope!r} "
                    f"(mapped: {', '.join(sorted(PI_KEY_NAMES))}), and Pi's own credential "
                    "store is never projected: route the profile to a mapped provider"
                ),
            )
        return CredentialRoutes(
            scope,
            (EnvRoute(name, CredentialKind.API_KEY),),
            stores=PI_PRIVATE_STORES,
            settings=PI_CONTAINER_SETTINGS,
        )

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

    def fresh_context_proof(self, profile: ProfileConfig) -> str:
        """``--no-session`` in an argv the adapter owns whole (ADR 0015 D14)."""
        if profile.extra_args:
            raise no_fresh_context(profile, "pi takes no extra_args", "Remove extra_args")
        return "pi --no-session: no session is kept, and no profile setting adds one"

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
        status. A spawn failure raises :class:`ExecutionError`. A conversation
        that settled with its final text read back before any limit cut it
        short is ``reply_delivered`` (#479), however Pi then shut down: a Pi
        that ignores its closed stdin until a limit kills it, or that exits
        with a failing status, delivered that reply all the same. Only when
        its protocol proves the reply the run's last word, though (R1-F3 of
        PR #481): every tool execution it started ended, and nothing but the
        text's response reached stdout after the run settled, read through
        Pi's shutdown to the end of stdout or the kill that ended it
        (:meth:`_read_after_reply`, :attr:`PiConversation.reply_settled`).

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
        command, description = self.sandboxed(req, self.build_command(req), req.env)
        duplex = DuplexRequest(
            command=command,
            cwd=req.cwd,
            env_allowlist=allowlist,
            env=req.env,
            idle_timeout_seconds=req.idle_timeout_seconds,
            max_runtime_seconds=req.max_runtime_seconds,
            # The abort comes after a limit, so the kill waits for it.
            wind_down_seconds=self.abort_seconds,
            max_record_bytes=PI_MAX_RECORD_BYTES,
            max_pending_bytes=PI_MAX_PENDING_BYTES,
            # Pi's bash tool starts every command detached (ADR 0003 §6).
            contain_orphans=True,
            sandbox=description,
        )
        with start_duplex(duplex) as child:
            cut_short = self._converse(child, conversation, monitor)
            # #434: the conversation had failed the run before any limit cut
            # it short (the deadline's own failure, below, is no such fact).
            failed_first = conversation.failure is not None
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
            if conversation.text is not None and not cut_short:
                # #479 (R1-F3 of PR #481): what Pi wrote after its final
                # text, through its shutdown, is reduced too, so a later turn
                # or an open tool call is seen before the reply is vouched for.
                self._read_after_reply(child, conversation)
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
            # #434: except that a failure established before the limit (a
            # limit falling due inside its abort) is kept as a fixed fact, so
            # the kill is not read as an idle timeout (R2-F1 of PR #480).
            failed_before_limit=PI_FAILED_BEFORE_LIMIT if timed_out and failed_first else None,
            # #479: the conversation settled with its final text read back
            # before any limit cut it short, and nothing failed it after (an
            # overflow while Pi shut down is a failure). Pi may still hang on
            # its shutdown until a limit kills it, or exit with a failing
            # status: the reply was delivered all the same, provided its
            # protocol proves it the run's last word (R1-F3 of PR #481): every
            # tool execution it started ended, and nothing but the text's
            # response reached stdout after the run settled, up to the end of
            # the stream or the kill that ended Pi's shutdown.
            reply_delivered=text is not None and not cut_short and conversation.reply_settled,
            provider_summary=summary,
            timeout_limit=(res.timeout_limit or cut_limit) if timed_out else "",
            last_activity_at=res.last_activity_at,
            sandbox=res.sandbox,
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
            "allow-list; remove it from the execution allow-lists (or unset it), or set "
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
        # In the launch's own sandbox, with the same declaration: the check
        # must find the credential exactly where Pi will (ADR 0005 D3.1).
        command, description = self.sandboxed(req, self.auth_check_command(req.profile), req.env)
        res = self._runner(
            ExecutionRequest(
                command=command,
                cwd=req.cwd,
                timeout_seconds=PI_AUTH_CHECK_SECONDS,
                max_output_bytes=PI_AUTH_CHECK_MAX_OUTPUT_BYTES,
                env_allowlist=allowlist,
                env=req.env,
                contain_orphans=True,
                sandbox=description,
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
            sandbox=res.sandbox,
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
                    conversation.oversize(item.head, item.limit)
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

    @staticmethod
    def _read_after_reply(child: DuplexChild, conversation: PiConversation) -> None:
        """Reduce what stdout carried after the final text, to its end (#479).

        Called after :meth:`DuplexChild.finish`, so it never waits: a Pi that
        exited leaves the rest of its stdout queued up to its end item; one a
        limit killed in its shutdown leaves what had reached the queue by the
        kill (what the group wrote as it was killed is never read, and the
        kill ended it). Every item is fed to the conversation, which counts
        it against the reply (:attr:`PiConversation.reply_settled`); stdin is
        closed, so nothing it would answer is sent.
        """
        items: list[Record | Oversize | Fragment | Overflow] = []
        item = child.read_line(timeout=0)
        while not isinstance(item, Eof):
            if isinstance(item, Timeout):
                # Only a kill's: after finish() nothing else waits.
                items += child.unread_at_deadline()
                break
            items.append(item)
            if isinstance(item, Overflow):
                break
            item = child.read_line(timeout=0)
        for rest in items:
            if isinstance(rest, Record):
                conversation.feed(rest.data, time.monotonic())
            elif isinstance(rest, Oversize):
                conversation.oversize(rest.head, rest.limit)
            elif isinstance(rest, Fragment):
                conversation.framing_error("stdout ended inside an unterminated record")
            else:
                conversation.framing_error("stdout records arrived faster than they were read")

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
            # `;`, not `:`: the redactor reads `api_key: <word>` as a key's value.
            return True, (
                f"{fields}; authenticated for provider {configured!r} only; whether this "
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
                    "through execution.agent_env_allowlist_extra"
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


# -- SDK providers (ADR 0007; policy: sdk_policy.py, #149) --------------------
_SDK_MODEL_RE = re.compile(r"[A-Za-z0-9][^\s#]{0,127}")
_OPENCODE_SDK_EFFORT_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")


class _SdkProvider(AgentProvider):
    """What the two SDK adapters share: a fixed policy, no pass-through, and
    no launch until their helper and transport land (``claude-sdk``'s did in
    #151; ``opencode-sdk``'s is #152's).

    ``command`` names the helper's interpreter (Python for ``claude-sdk``,
    Node for ``opencode-sdk``; ADR 0007 D2.3, D2.5) and is required: no
    default could name the operator's separate runtime environment.
    ``extra_args`` are refused: every option the helper gets is a field of
    the policy (:mod:`autoforge.sdk_policy`), and the SDKs' ``extra_args``
    and ``settings`` are the pass-through #149 closes. The launch allow-list
    holds exact names only, without any other provider's variables or a
    credential-shaped name (:func:`autoforge.sdk_policy.sdk_launch_allowlist`).
    """

    # The issue whose helper launches this provider, the helper's
    # interpreter as ``command`` names it, and the decision that says so.
    transport_issue = ""
    # Whether this release has the provider's transport: its adapter drives
    # the helper (``execute``). Until then every launch is refused.
    transport_landed = False
    interpreter = ""
    runtime_decision = ""
    # Foreign names the operator may still forward by listing them exactly.
    passthrough: tuple[str, ...] = ()
    # What the helper's argv passes its interpreter before the helper's path.
    interpreter_flags: tuple[str, ...] = ()
    # What ``doctor``'s version probes never carry, whatever the allow-list
    # names (:meth:`probe_allowlist`).
    probe_withheld: tuple[str, ...] = ()

    def validate_profile(self, profile: ProfileConfig) -> None:
        super().validate_profile(profile)
        where = f"profile {profile.name!r}"
        command = profile.command
        if not command or any(ch.isspace() for ch in command) or collection_text(command):
            raise ConfigurationError(
                f"{where}: {self.name} needs 'command', the {self.interpreter} of the "
                "separately installed runtime environment its helper runs in (ADR 0007 "
                f"{self.runtime_decision}): one executable, with no whitespace, never a "
                f"list; got {command!r}"
            )
        if profile.extra_args:
            # Never the arguments themselves: one may carry a settings
            # document or a key.
            raise ConfigurationError(
                f"{where}: {self.name} takes no extra_args ({len(profile.extra_args)} given): "
                "every SDK option, flag and setting is fixed by AutoForge's unattended policy "
                "(docs/sdk-policy.md), and a pass-through could load settings, hooks, MCP "
                "servers or plugins, or widen the tools"
            )
        self.policy(profile)

    # -- to override ---------------------------------------------------------
    def policy(self, profile: ProfileConfig) -> ClaudeSdkPolicy | OpenCodeSdkPolicy:
        """``profile``'s policy, or :class:`ConfigurationError`."""
        raise NotImplementedError

    def interaction_settings(self, profile: ProfileConfig) -> InteractionSettings | None:
        return interaction_settings(profile.options, f"profile {profile.name!r}")

    def policy_environment_names(self, profile: ProfileConfig) -> tuple[str, ...]:
        """The variables the policy sets over the launch's allow-listed ones,
        by name (docs/sdk-policy.md §5, §6, §9)."""
        raise NotImplementedError

    def runtime_plan(self, profile: ProfileConfig, pin: HelperPin) -> str:
        """What the plan says of the provider runtime the helper starts."""
        raise NotImplementedError

    # -- shared --------------------------------------------------------------
    def helper(self) -> HelperPin:
        """This provider's helper program and pinned pair, from the packaged
        manifest (:mod:`autoforge.sdk_assets`)."""
        return helper_pin(self.name)

    def launch_problem(self) -> str:
        """Why no launch of this provider can start in this release, or "" when
        one can: its helper is shipped and matches the manifest's sha256, and
        its transport has landed."""
        pin = self.helper()
        if not pin.shipped:
            return (
                f"its helper program {pin.describe()} is not shipped with this AutoForge: it "
                f"and its transport are {pin.implemented_by}'s and have not landed"
            )
        problem = helper_problem(pin)
        if problem:
            return problem
        if not self.transport_landed:
            return f"its transport is #{self.transport_issue}'s and has not landed"
        return ""

    def refusal_text(self) -> str:
        """Why every launch of this provider is refused, as an operator reads it."""
        return (
            f"provider {self.name} cannot launch in this release: its policy is in place "
            f"(docs/sdk-policy.md), but {self.launch_problem()}. Route this profile to "
            "another provider"
        )

    def _refuse_launch(self) -> ConfigurationError:
        return ConfigurationError(self.refusal_text())

    def build_command_for(self, profile: ProfileConfig, prompt: str) -> list[str]:
        """The helper's argv as a launch runs it (ADR 0007 D2.2, D2.3, D2.5):
        ``command`` with the helper staged per launch, by absolute path.

        Never the prompt, which travels in the helper's ``start`` record on
        stdin (ADR 0007 §5), so a dry run's plan and the run log name the
        same argv. The staged directory is made for the launch, so it is
        named by its role. Building it launches nothing; every launch is
        still refused (:meth:`integration_preflight`, :meth:`execute`).
        """
        return [profile.command, *self.interpreter_flags, self.helper().staged_path()]

    def integration_preflight(
        self, env_allowlist: tuple[str, ...] | None, *, fresh_context: bool = False
    ) -> None:
        """Refused while :meth:`launch_problem` names a reason, so the engine
        stops here, before a launch is charged."""
        if self.launch_problem():
            raise self._refuse_launch()

    def execute(self, req: AgentRequest) -> AgentExecutionResult:
        raise self._refuse_launch()

    def profile_allowlist(self, base: tuple[str, ...], profile: ProfileConfig) -> tuple[str, ...]:
        """The exact names a launch of ``profile`` inherits from ``base``, the
        profile's own credential included (``opencode-sdk`` names it per
        profile)."""
        return self._allowlist(base, profile)

    def probe_allowlist(self, base: tuple[str, ...]) -> tuple[str, ...]:
        """What ``doctor``'s version probes of this provider run with: the
        launch allow-list's filter of ``base`` without the provider's own
        credential, which no version probe needs (#150), and without
        :attr:`probe_withheld`, listed exactly or matched by a prefix. The
        filter's names are exact, so none of them reaches the probe."""
        withheld = set(self.probe_withheld)
        names = sdk_launch_allowlist(base, (), os.environ.keys())
        return tuple(name for name in names if name not in withheld)

    def plan_notes(
        self, profile: ProfileConfig, env_allowlist: tuple[str, ...] | None
    ) -> list[str]:
        """The helper, its runtime and pinned pair, the policy, the
        environment by name, the state root by role, and why the launch is
        refused (#150). Pure: the manifest is read; nothing is started,
        imported, created or resolved."""
        pin = self.helper()
        policy = self.policy(profile)
        interaction = policy.interaction
        if env_allowlist is None:
            inherited = "the controller's whole environment (no allow-list)"
        else:
            names = self.profile_allowlist(env_allowlist, profile)
            inherited = f"{len(names)} exact name(s): {', '.join(names) or '(none)'}"
        floor = ".".join(str(part) for part in pin.interpreter_minimum)
        return [
            f"{self.name} helper: {pin.describe()}, its sha256 checked against the packaged "
            f"manifest, staged into a per-launch directory and run by absolute path with "
            f"{profile.command!r} ({pin.interpreter} >= {floor}); the prompt travels in its "
            "start record on stdin, never in argv",
            f"{self.name} runtime: {self.runtime_plan(profile, pin)}; supported pair "
            f"{pin.pair()} (ADR 0007 §2.4)",
            f"{self.name} policy (docs/sdk-policy.md): interaction_policy {interaction.policy} "
            f"(max_wait_seconds {interaction.max_wait_seconds}, max_interactions "
            f"{interaction.max_interactions}); tools {', '.join(policy.tools)}; policy digest "
            f"{policy.digest()[:16]}",
            f"{self.name} environment: {inherited}, other providers' variables and "
            "credential-shaped names withheld; set over them: "
            f"{', '.join(self.policy_environment_names(profile))}",
            f"{self.name} state root: <issue sandbox dir>/sdk/{self.name}/, per issue, never "
            "inside the worktree (docs/sdk-policy.md §6)",
            self.launch_plan(profile),
        ]

    def launch_plan(self, profile: ProfileConfig) -> str:
        """The plan's last note: how a launch runs, or why every one is refused."""
        problem = self.launch_problem()
        if problem:
            return (
                f"{self.name} launch: every launch is refused in this release, before "
                f"anything starts: {problem}"
            )
        return f"{self.name} launch: {self.launch_description(profile)}"

    def launch_description(self, profile: ProfileConfig) -> str:
        """What a launch does, for :meth:`launch_plan` once it can start."""
        raise NotImplementedError

    def launch_allowlist(self, base: tuple[str, ...]) -> tuple[str, ...]:
        """``base`` with every other provider's variables and every
        credential-shaped name removed and ``*`` prefixes expanded, plus this
        provider's own names (:func:`autoforge.sdk_policy.sdk_launch_allowlist`)."""
        return self._allowlist(base, None)

    def environment_allowlist(self, req: AgentRequest) -> tuple[str, ...] | None:
        if req.env_allowlist is None:
            return None
        return self._allowlist(req.env_allowlist, req.profile)

    def _allowlist(self, base: tuple[str, ...], profile: ProfileConfig | None) -> tuple[str, ...]:
        return sdk_launch_allowlist(
            base,
            self.own_names(profile),
            os.environ.keys(),
            passthrough=self.passthrough,
        )


# The helper's records are bounded like a stream-json line: the final
# ``result`` must arrive whole, and JSON escaping (the helper writes ASCII)
# can grow a final text that is itself up to the stdout bound.
CLAUDE_SDK_MAX_RECORD_BYTES = CLAUDE_MAX_RECORD_BYTES
CLAUDE_SDK_MAX_PENDING_BYTES = 2 * CLAUDE_SDK_MAX_RECORD_BYTES
# How long a helper whose run is being ended (a limit fell due, the run
# failed, an ask failed it) is given to interrupt its turn, drain the
# interrupted result and write its ``end``, after the interrupt is sent: the
# helper's own drain bound plus a margin. The kill comes this long after a
# limit falls due (``wind_down_seconds``).
CLAUDE_SDK_INTERRUPT_SECONDS = 15.0
# #434 (R2-F1 of PR #489): an ask had failed the run (ADR 0007 D7.6) when a
# limit killed the helper winding down. The fixed fact kept through the kill
# (``failed_before_limit``): the recovery rule reads it before the timeout
# and never relaunches the step, since a relaunch would ask again.
CLAUDE_SDK_ASK_FAILED_BEFORE_LIMIT = "claude-sdk: an ask for a decision had failed the run"
# What ``claude-sdk`` can carry of a decision on its pinned pair (ADR 0007
# D5.3, §3's amendment of #151): a live question (one to four, several
# labels, an answer of the operator's own words: gate G-C3), a live
# permission approved once, with a modified input, or denied with guidance
# (G-C4), and an interrupt (G-C5); probed through this adapter and helper
# on SDK 0.2.163 with its bundled Claude Code 2.1.286 against a loopback
# stand-in of the Messages API. Never a session approval (the policy writes
# no permission rule: docs/sdk-policy.md §3), and no pause: replay and
# continuation are #126's, gated by G-C6 and G-C7.
CLAUDE_SDK_CAPABILITIES = Capabilities(
    live_questions=True,
    live_permissions=True,
    interrupt=True,
    unsupported=frozenset({DecisionKind.ALLOW_SESSION}),
)
# Where a launch's state directory lies under the issue's sandbox directory
# (ClaudeSdkRoots.under): ``sdk/claude-sdk/config``.
CLAUDE_SDK_STATE_DIR = f"{SDK_ROOT_DIR}/{CLAUDE_SDK}/config"


@contextmanager
def _staged_helper(pin: HelperPin) -> Iterator[str]:
    """The helper program, staged for one launch (ADR 0007 D2.2): its bytes
    read once from the installed package, checked against the manifest's
    sha256 and written into a directory made for this launch (``mkdtemp``,
    mode 0700, outside every repository), read-only; the path of the copy.
    The directory is removed when the launch ends. A file that is missing or
    does not match refuses the launch (:class:`ConfigurationError`); one that
    cannot be written is an :class:`ExecutionError`, before anything starts.
    """
    try:
        body = (helpers_dir() / pin.file).read_bytes()
    except OSError:
        body = None
    if body is None or hashlib.sha256(body).hexdigest() != pin.sha256:
        raise ConfigurationError(
            f"provider {pin.provider} cannot launch: {helper_problem(pin) or 'its helper changed'}"
        )
    try:
        directory = tempfile.mkdtemp(prefix="autoforge-claude-sdk-")
    except OSError as exc:
        raise ExecutionError(f"cannot stage the {pin.provider} helper: {exc}") from exc
    try:
        path = os.path.join(directory, pin.file)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
            os.chmod(path, 0o400)
            os.chmod(directory, 0o500)
        except OSError as exc:
            raise ExecutionError(f"cannot stage the {pin.provider} helper: {exc}") from exc
        yield path
    finally:
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        shutil.rmtree(directory, ignore_errors=True)


def _spelled_interpreter(command: str, search_path: str | None) -> str:
    """The absolute path ``command`` is started by, links not followed, or ""."""
    found = command if "/" in command else shutil.which(command, path=search_path)
    return os.path.abspath(found) if found and os.path.isabs(found) else ""


def _python_environment(interpreter: str) -> str:
    """The virtual environment whose ``bin/`` ``interpreter`` lies in (its
    ``pyvenv.cfg`` beside ``bin/``), or "" when it lies in none."""
    if not interpreter:
        return ""
    prefix = os.path.dirname(os.path.dirname(interpreter))
    return prefix if os.path.isfile(os.path.join(prefix, "pyvenv.cfg")) else ""


class _SdkCut(enum.Enum):
    """Why :meth:`ClaudeSdkProvider._converse` ended a helper still running."""

    # A limit fell due: a timeout.
    DEADLINE = "deadline"
    # Stopped on the loop detector's verdict (#194): a timeout whose limit is ``loop``.
    LOOP = "loop"
    # The run failed (the envelope, the policy, the pair) or an ask failed it:
    # the helper is interrupted and wound down.
    FAILED = "failed"


class ClaudeSdkProvider(_SdkProvider):
    """``claude-sdk``: the Claude Agent SDK in a helper process (ADR 0007; #151).

    Authenticates with ``ANTHROPIC_API_KEY`` alone, never a claude.ai login
    (D10.2); a Bedrock, Vertex AI or Foundry route is not supported in this
    release (docs/sdk-policy.md §5).

    The static surface is #149's and #150's; the launch is #151's
    (:meth:`execute`): the staged helper (``sdk_helpers/claude_helper.py``)
    under the duplex handle, its envelope reduced by
    :class:`~autoforge.claude_sdk.ClaudeSdkSession`, and each of its asks put
    to the launch's broker.
    """

    name = CLAUDE_SDK
    environment_names = (CLAUDE_SDK_CREDENTIAL,)
    option_keys = CLAUDE_SDK_OPTION_KEYS
    # The SDK's bundled Claude Code is the CLI #165 isolates: its Bash tool
    # gets the same shell isolation (docs/sdk-policy.md §2, "startup scripts").
    isolation_names = tuple(sorted(claude_isolation_env("", "")))
    path_names = ("CLAUDE_CONFIG_DIR", "CLAUDE_ENV_FILE", "CLAUDE_CODE_SHELL")
    passthrough = CLAUDE_SDK_PASSTHROUGH
    transport_issue = "151"
    transport_landed = True

    def fresh_context_proof(self, profile: ProfileConfig) -> str:
        """A new session id per launch, with no resume in the helper's policy (ADR 0015 D14)."""
        if profile.extra_args:
            raise no_fresh_context(profile, f"{self.name} takes no extra_args", "Remove extra_args")
        return f"{self.name}: a new session id per launch, and no resume in its policy"

    @contextmanager
    def integration_isolation(self) -> Iterator[dict[str, str]]:
        """The ``claude`` CLI's shell isolation, for the Claude Code the helper
        starts (:func:`claude_shell_isolation`)."""
        with claude_shell_isolation() as isolation:
            yield isolation

    def complete_isolation(
        self, isolation: Mapping[str, str], environment: Mapping[str, str]
    ) -> None:
        complete_claude_isolation(isolation, environment)

    interpreter = "Python interpreter"
    runtime_decision = "D2.3"
    # ``-I`` keeps out ``PYTHON*`` variables, the user site directory and the
    # script's own directory (D2.3).
    interpreter_flags = ("-I",)
    # The two variables CPython reads at startup even under ``-I``: either
    # would make the probe's interpreter read a ``._pth`` file doctor did not
    # look for (R3-F1 of #150's review).
    probe_withheld = PYTHON_STARTUP_NAMES

    def __init__(
        self,
        runner: Runner | None = None,
        *,
        capabilities: Capabilities = CLAUDE_SDK_CAPABILITIES,
        interrupt_seconds: float = CLAUDE_SDK_INTERRUPT_SECONDS,
        max_text_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        max_record_bytes: int = CLAUDE_SDK_MAX_RECORD_BYTES,
    ) -> None:
        super().__init__(runner)
        self.capabilities = capabilities
        self.interrupt_seconds = interrupt_seconds
        self.max_text_bytes = max_text_bytes
        self.max_record_bytes = max_record_bytes

    def validate_profile(self, profile: ProfileConfig) -> None:
        super().validate_profile(profile)
        where = f"profile {profile.name!r}"
        if profile.effort and profile.effort not in CLAUDE_EFFORTS:
            raise ConfigurationError(
                f"{where}: {self.name} effort must be one of {', '.join(CLAUDE_EFFORTS)}, "
                f"got {profile.effort!r}"
            )
        if not _SDK_MODEL_RE.fullmatch(profile.model):
            raise ConfigurationError(
                f"{where}: {self.name} model must be a Claude model alias or id, with no "
                f"whitespace and no '#', got {profile.model!r}"
            )

    def policy(self, profile: ProfileConfig) -> ClaudeSdkPolicy:
        return claude_sdk_policy(profile.name, profile.options)

    def state_directories(self, profile: ProfileConfig) -> tuple[str, ...]:
        """The configuration and session root, ``sdk/claude-sdk/config``
        (:class:`~autoforge.sdk_policy.ClaudeSdkRoots`, docs/sdk-policy.md §6)."""
        return (CLAUDE_SDK_STATE_DIR,)

    def interaction_capabilities(self, profile: ProfileConfig) -> Capabilities:
        """:data:`CLAUDE_SDK_CAPABILITIES`, the gates that passed on the pinned pair."""
        return self.capabilities

    def integration_preflight(
        self, env_allowlist: tuple[str, ...] | None, *, fresh_context: bool = False
    ) -> None:
        """Refuse a launch whose helper is not usable (missing, or not the
        manifest's sha256) or whose environment would not authenticate with
        ``ANTHROPIC_API_KEY`` alone (:func:`~autoforge.sdk_policy.claude_sdk_auth_problems`,
        D10.2), before a launch is charged. Names only, never a value."""
        super().integration_preflight(env_allowlist, fresh_context=fresh_context)
        problems = claude_sdk_auth_problems(self.launch_environment(env_allowlist))
        if problems:
            raise ConfigurationError(f"refusing to launch {self.name}: {'; '.join(problems)}")

    def launch_description(self, profile: ProfileConfig) -> str:
        return (
            "one helper per launch under the duplex handle, its group contained (ADR 0002), "
            f"{CLAUDE_SDK_CREDENTIAL} required, a fresh session id each launch, and only the "
            "completed main turn's result text read as the reply"
        )

    def policy_environment_names(self, profile: ProfileConfig) -> tuple[str, ...]:
        roots = ClaudeSdkRoots.under(Path("/"))  # by name only: no path is used
        return tuple(self.policy(profile).environment(roots))

    def runtime_plan(self, profile: ProfileConfig, pin: HelperPin) -> str:
        return (
            f"the {pin.runtime} executable bundled in {pin.sdk_package}'s platform wheel, "
            "passed to the SDK as its cli_path, never one found on PATH (ADR 0007 D2.4)"
        )

    def environment_probe_command(self, profile: ProfileConfig) -> list[str]:
        """``doctor``'s read-only probe of the helper environment ``command``
        names (ADR 0007 D2.6 and its amendment): its Python, and the installed
        SDK's and its bundled CLI's versions from package files, never an SDK
        import (:data:`autoforge.sdk_assets.CLAUDE_ENVIRONMENT_PROBE`). Not
        the launch's ``-I`` alone: ``-S`` keeps the environment's ``site``
        hooks from running, and ``-B`` writes no bytecode."""
        return [profile.command, *CLAUDE_ENVIRONMENT_PROBE_FLAGS, "-c", CLAUDE_ENVIRONMENT_PROBE]

    def sandbox_requirements(
        self, profile: ProfileConfig, command: list[str], env: Mapping[str, str], home: str
    ) -> ProviderRequirements:
        """``claude-sdk``'s declaration (ADR 0005 D7.1, D13.1; ADR 0007 D10.2).

        ``ANTHROPIC_API_KEY`` is an ``env`` item; there is no login file and
        no keyring item, ever. The policy's ``CLAUDE_CONFIG_DIR``
        (:class:`autoforge.sdk_policy.ClaudeSdkRoots`) writable at its own
        path (``dir-rw``), refused when it is or lies in ``~/.claude``
        (D7.2). ``~/.claude`` and ``~/.claude.json`` are the operator
        configuration roots.
        """
        needs = super().sandbox_requirements(profile, command, env, home)
        # The staged helper's directory (D2.2), read-only like #165's
        # overrides: ``[interpreter, -I, <staged helper>]``.
        staged = command[-1] if len(command) > 1 else ""
        config = (os.path.dirname(staged),) if os.path.isabs(staged) else ()
        # The launch's CLAUDE_ENV_FILE (#165's shell cleaner), read-only.
        env_file = env.get("CLAUDE_ENV_FILE", "")
        if env_file.startswith("/"):
            config = (*config, env_file)
        # The helper environment (D2.3): its prefix read-only, and its
        # interpreter as spelled, which must lead inside where it leads here.
        spelled = _spelled_interpreter(command[0], env.get("PATH"))
        environment = _python_environment(spelled)
        roots = (*needs.install_roots, environment) if environment else needs.install_roots
        spelled_paths = (
            (*needs.spelled_paths, spelled)
            if spelled and spelled != needs.executable
            else needs.spelled_paths
        )
        auth = (
            [AuthItem(AuthMode.ENV, CLAUDE_SDK_CREDENTIAL)]
            if env.get(CLAUDE_SDK_CREDENTIAL)
            else []
        )
        config_dir = env.get("CLAUDE_CONFIG_DIR", "")
        if config_dir.startswith("/"):
            auth.append(
                AuthItem(
                    AuthMode.DIR_RW,
                    "the claude-sdk configuration and session root ($CLAUDE_CONFIG_DIR)",
                    config_dir,
                    env_name="CLAUDE_CONFIG_DIR",
                )
            )
        return dataclasses.replace(
            needs,
            install_roots=tuple(dict.fromkeys(roots)),
            spelled_paths=spelled_paths,
            config=(*needs.config, *config),
            auth=tuple(auth),
            operator_config_roots=(
                os.path.join(home, ".claude"),
                os.path.join(home, ".claude.json"),
            ),
        )

    def sandboxed(
        self, req: AgentRequest, command: list[str], env: Mapping[str, str] | None
    ) -> tuple[list[str], LaunchDescription | None]:
        """As every adapter's, but the interpreter keeps the path ``command``
        spells (:meth:`sandbox_requirements` declares it): CPython finds its
        virtual environment's ``pyvenv.cfg`` beside the path it was started
        by, so the resolved base interpreter would run without the helper
        environment and its SDK (seen under ``bwrap``)."""
        launch_path = {**self.launch_environment(req.env_allowlist), **(env or {})}.get("PATH")
        spelled = _spelled_interpreter(command[0], launch_path)
        command, description = super().sandboxed(req, command, env)
        if description is not None and spelled:
            command = [spelled, *command[1:]]
        return command, description

    # -- the launch (#151) ---------------------------------------------------
    def execute(self, req: AgentRequest) -> AgentExecutionResult:
        """One helper, one session, one query, one final text (ADR 0007 D1.1).

        Refused before anything starts (:class:`ConfigurationError` or
        :class:`ExecutionError`): a helper that is not the manifest's, no
        state directory from the engine, or an environment that would not
        authenticate with ``ANTHROPIC_API_KEY`` alone. Otherwise the staged
        helper runs under the duplex handle with the allow-listed
        environment, the controller's overrides and the policy's variables
        (``CLAUDE_CONFIG_DIR`` in the issue's state directory), the idle limit
        and the ceiling, ``contain_orphans`` and the request's sandbox: one
        containment owner for the helper, the SDK's Claude Code and
        everything that starts (D3.2).

        The session reducer (:class:`~autoforge.claude_sdk.ClaudeSdkSession`)
        checks ``hello`` against the pinned pair before ``start`` is sent and
        decides the outcome; each ask goes to the broker
        (:meth:`_answer`). When a limit falls due, the run fails or an ask
        fails it, the deadline is pinned, the helper is sent ``interrupt``
        and given :attr:`interrupt_seconds` to drain its turn and end, and
        then its stdin is closed; the duplex handle kills what is left. A
        timeout wins over what the envelope said (ADR 0003 §2.6), and
        ``exit_code`` is always the helper's real status. ``stdout`` is the
        completed main turn's ``result`` text only, tail-bounded.
        """
        policy = self.policy(req.profile)
        pin = self.helper()
        problem = self.launch_problem()
        if problem:
            raise self._refuse_launch()
        if not req.state_dir:
            raise ExecutionError(
                f"{self.name}: the launch has no state directory: the engine creates the "
                "issue's configuration and session root before every launch (#151)"
            )
        roots = ClaudeSdkRoots.under(Path(req.state_dir))
        if not roots.config.is_dir():
            raise ExecutionError(
                f"{self.name}: the launch's configuration and session root does not exist"
            )
        base = {**(req.env or {}), **policy.environment(roots)}
        inherited = {**self.launch_environment(req.env_allowlist), **base}
        auth = claude_sdk_auth_problems(inherited)
        if auth:
            raise ExecutionError(f"refusing to launch {self.name}: {'; '.join(auth)}")
        # The root persists between launches and its agent can write it: an
        # ``env`` left in its settings or global configuration would be set
        # over the launch's isolation, as for the CLI provider (#165).
        problems = claude_configuration_problems(inherited)
        if problems:
            raise ExecutionError(
                f"refusing to launch {self.name}: its configuration and session root sets "
                f"variables over the environment AutoForge gives a launch: {'; '.join(problems)}"
            )
        emit = guarded(req.progress)
        monitor = _loop_monitor(req, emit)
        session_id = str(uuid.uuid4())
        start = start_record(
            session_id=session_id,
            prompt=req.prompt,
            cwd=req.cwd,
            model=req.profile.model,
            effort=req.profile.effort,
            policy=policy,
            max_record_bytes=self.max_record_bytes,
        )
        session = ClaudeSdkSession(
            pin=pin,
            policy=policy,
            session_id=session_id,
            start=start,
            cwd=req.cwd,
            emit=emit,
            loop=_ClockedLoop(monitor),
        )
        asked: list[str] = []
        with _staged_helper(pin) as helper, self.integration_isolation() as isolation:
            env = {**(req.env or {}), **isolation, **policy.environment(roots)}
            command = [req.profile.command, *self.interpreter_flags, helper]
            command, description = self.sandboxed(req, command, env)
            self.complete_isolation(isolation, self.started_environment(req, env, description))
            duplex = DuplexRequest(
                command=command,
                cwd=req.cwd,
                env_allowlist=self.environment_allowlist(req),
                env=env,
                idle_timeout_seconds=req.idle_timeout_seconds,
                max_runtime_seconds=req.max_runtime_seconds,
                # The interrupt comes after a limit, so the kill waits for it.
                wind_down_seconds=self.interrupt_seconds,
                max_record_bytes=self.max_record_bytes,
                max_pending_bytes=2 * self.max_record_bytes,
                # Nothing an agent starts outlives its invocation (ADR 0002).
                contain_orphans=True,
                sandbox=description,
            )
            with start_duplex(duplex) as child:
                cut = self._converse(child, session, req.interaction, monitor, asked)
                cut_limit = child.deadline_limit if cut is _SdkCut.DEADLINE else ""
                if cut in (_SdkCut.DEADLINE, _SdkCut.FAILED) and session.started:
                    child.pin_deadline()
                    self._wind_down(child, session)
                child.close_stdin()
                session.stdin_closed()
                res = child.finish()
                self._read_rest(child, session)
        return self._result(req, res, session, cut, cut_limit, asked, monitor)

    def _send(self, child: DuplexChild, session: ClaudeSdkSession, line: bytes) -> None:
        try:
            child.send_line(line)
        except ChildStdinClosedError:
            session.framing_error("the helper closed its stdin")

    def _flush(self, child: DuplexChild, session: ClaudeSdkSession) -> None:
        while session.outbox:
            self._send(child, session, session.outbox.pop(0))

    def _converse(
        self,
        child: DuplexChild,
        session: ClaudeSdkSession,
        broker: InteractionBroker | None,
        monitor: LoopMonitor,
        asked: list[str],
    ) -> _SdkCut | None:
        """Drive the session to its end; why it was cut short, or ``None``.

        The limit is recomputed on every turn, since the helper's output
        moves it; once it falls due the caller pins it and winds the helper
        down. A loop verdict (#194) stops a helper still running at once, as
        on a timeout. ``asked`` collects the reason of an ask that failed the
        run (a broker ``Fail``, or data the broker could not be given).
        """
        try:
            while not session.done:
                self._flush(child, session)
                if asked:
                    return _SdkCut.FAILED
                now = time.monotonic()
                due = child.limit_due()
                if now >= due:
                    return _SdkCut.DEADLINE
                item = child.read_line(timeout=max(0.0, due - now))
                if isinstance(item, Record):
                    ask = session.feed(item.data)
                    if monitor.verdict is not None and child.stop():
                        return _SdkCut.LOOP
                    if ask is not None:
                        self._answer(child, session, ask, broker, asked)
                elif isinstance(item, Oversize | Fragment | Overflow):
                    self._framing(session, item)
                elif isinstance(item, Eof):
                    session.stream_ended()
                elif item.deadline_exceeded:
                    return _SdkCut.DEADLINE
            self._flush(child, session)
        except ExecutionTimeoutError:
            return _SdkCut.DEADLINE  # a write overran the deadline; the group is gone
        if session.failure is not None and session.started and not session.helper_failed:
            return _SdkCut.FAILED
        return None

    def _answer(
        self,
        child: DuplexChild,
        session: ClaudeSdkSession,
        ask: Ask,
        broker: InteractionBroker | None,
        asked: list[str],
    ) -> None:
        """Put ``ask`` to the broker and send the helper what its directive says
        (ADR 0007 D9.2): an answer or an approval is ``allow`` (an answer with
        the questions and their answers as the call's input, a modified input
        as validated), a denial or a cancellation ``deny`` with the operator's
        guidance, and a ``Fail`` ``fail``, which also ends the run (D7.6). An
        ask the broker cannot be given (no broker, data past its bounds or not
        renderable) fails the same way (D10.4); so does a modified input the
        tool would not take, an empty one included
        (:func:`~autoforge.claude_sdk.modified_input_problem`). A ``Pause``
        cannot come (no pause is available until #126) and fails too."""
        reason = ""
        request = None
        if broker is None:
            reason = "the agent asked for a decision, and this launch has no broker to ask"
        elif ask.problem:
            reason = f"the agent asked for a decision AutoForge cannot show: {ask.problem}"
        else:
            try:
                if ask.kind is RequestKind.QUESTION:
                    request = broker.question(ask.tool_use_id, ask_questions(ask), agent=ask.agent)
                else:
                    assert ask.tool_input is not None
                    request = broker.permission(
                        ask.tool_use_id,
                        ask.tool_name,
                        permission_summary(ask.tool_name, ask.tool_input),
                        ask.input_sha256,
                        agent=ask.agent,
                    )
            except InteractionDataError as exc:
                reason = f"the agent asked for a decision AutoForge cannot show: {exc}"
        if request is not None and broker is not None:
            directive = broker.handle(request, child.limit_due())
            if isinstance(directive, Deliver):
                decision = directive.decision
                if isinstance(decision, Answer):
                    self._send(
                        child,
                        session,
                        session.decide(ask, "allow", updated_input=answer_input(ask, decision)),
                    )
                    return
                if isinstance(decision, Allow):
                    updated = decision.input_to_send()
                    # Every modified input is checked, an empty one included;
                    # only none at all runs the call's own input.
                    problem = (
                        ""
                        if updated is None
                        else modified_input_problem(ask.tool_name, updated, ask.tool_input)
                    )
                    if not problem:
                        self._send(
                            child, session, session.decide(ask, "allow", updated_input=updated)
                        )
                        return
                    reason = f"the decision was not delivered: {problem}"
                else:
                    guidance = decision.guidance if isinstance(decision, Deny) else None
                    self._send(child, session, session.decide(ask, "deny", guidance=guidance))
                    return
            elif isinstance(directive, Fail):
                reason = directive.reason
            else:
                reason = (
                    f"the agent asked for a decision ({request.what}) and the broker paused, "
                    "but no pending-interaction record exists to pause into (#126)"
                )
        asked.append(clean(reason, 400))
        self._send(
            child,
            session,
            session.decide(ask, "fail", guidance="AutoForge cannot give this decision"),
        )

    def _wind_down(self, child: DuplexChild, session: ClaudeSdkSession) -> None:
        """Interrupt the helper's turn and read it to its ``end``, within
        :attr:`interrupt_seconds` (ADR 0007 D3.3): what it writes meanwhile
        cannot stretch the deadline the caller pinned. An ask that arrives
        meanwhile is denied. A framing violation fails the session here as
        in :meth:`_converse` (:meth:`_framing`), so a result read after a
        record was lost is never the reply (R4-F1 of PR #489)."""
        end = time.monotonic() + self.interrupt_seconds
        try:
            self._send(child, session, session.interrupt())
            while not session.done:
                now = time.monotonic()
                if now >= end:
                    return
                item = child.read_line(timeout=end - now)
                if isinstance(item, Record):
                    ask = session.feed(item.data)
                    if ask is not None:
                        self._send(
                            child,
                            session,
                            session.decide(ask, "deny", guidance="AutoForge is ending this run"),
                        )
                elif isinstance(item, Oversize | Fragment | Overflow):
                    self._framing(session, item)
                    if isinstance(item, Overflow):
                        return
                elif isinstance(item, Eof):
                    session.stream_ended()
                    return
                elif item.deadline_exceeded:
                    return
        except ExecutionTimeoutError:
            return

    @staticmethod
    def _framing(session: ClaudeSdkSession, item: Oversize | Fragment | Overflow) -> None:
        """A framing violation of the helper's stdout, failed alike wherever
        it is read (:meth:`_converse`, :meth:`_wind_down`, :meth:`_read_rest`):
        the helper bounds and terminates every record it writes, so a record
        past the bound, an unterminated tail or an overflow means the stream
        was not read whole, and no reply is taken from it."""
        if isinstance(item, Oversize):
            session.oversize(item.limit)
        elif isinstance(item, Fragment):
            session.framing_error("stdout ended inside an unterminated record")
        else:
            session.framing_error("stdout records arrived faster than they were read")

    @staticmethod
    def _read_rest(child: DuplexChild, session: ClaudeSdkSession) -> None:
        """Everything stdout still held after :meth:`DuplexChild.finish`, to its
        end, each item reduced, before the outcome is read: the helper's
        ``end`` written as its stdin was closed, what it wrote before a limit
        killed it, and whatever followed an ``end`` already read. The reducer
        fails the run on anything after ``end`` (a record, one past the bound,
        an unterminated tail, an overflow), so a reply is accepted only from a
        stream read whole. Never waits; nothing is sent."""
        item = child.read_line(timeout=0)
        rest: list[Record | Oversize | Fragment | Overflow] = []
        while not isinstance(item, Eof):
            if isinstance(item, Timeout):
                rest += child.unread_at_deadline()
                break
            rest.append(item)
            if isinstance(item, Overflow):
                break
            item = child.read_line(timeout=0)
        for entry in rest:
            # Fed even once the session is done: a record after ``end`` fails
            # it, and one after a failure changes nothing.
            if isinstance(entry, Record):
                session.feed(entry.data)
            else:
                ClaudeSdkProvider._framing(session, entry)
        if isinstance(item, Eof):
            session.stream_ended()

    def _result(
        self,
        req: AgentRequest,
        res: DuplexResult,
        session: ClaudeSdkSession,
        cut: _SdkCut | None,
        cut_limit: str,
        asked: list[str],
        monitor: LoopMonitor,
    ) -> AgentExecutionResult:
        """The invocation's result from the duplex facts and the session's."""
        stream = session.stream
        timed_out = cut in (_SdkCut.DEADLINE, _SdkCut.LOOP) or res.timed_out or res.stopped
        broker = req.interaction
        ask_failure = asked[0] if asked else None
        failure = ask_failure or session.failed
        if failure is None and res.records_overflowed:
            failure = f"{self.name}: the helper's records arrived faster than they were read"
        if failure is None and session.outcome == "interrupted":
            failure = f"{self.name}: the helper ended its turn interrupted"
        if session.helper_failed and failure is not None and res.exit_code:
            failure = f"{failure} (exit {res.exit_code})"
        text = session.text if failure is None else None
        stdout, truncated = _keep_tail(encodable(text or ""), self.max_text_bytes)
        interaction: InteractionOutcome | None = None
        if broker is not None or ask_failure is not None:
            outcome = broker.outcome() if broker is not None else None
            failed = ask_failure is not None or (
                outcome is not None and outcome.lifecycle is Lifecycle.FAILED
            )
            interaction = (
                InteractionOutcome(Lifecycle.FAILED)
                if failed
                else (outcome or InteractionOutcome(Lifecycle.COMPLETED))
            )
        usage_limit = (stream.limit or stream.api_limit) if timed_out or failure else None
        summary = session.summary()
        summary["failure"] = failure or ""
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
            # A timeout wins over whatever the envelope said (ADR 0003 §2.6).
            provider_failure=None if timed_out else failure,
            # A model API error the stream typed as transient (#395), never
            # beside a usage limit (#434) or a failure of the envelope's own.
            transient_failure=(
                None
                if timed_out or failure is None or usage_limit or session.failure
                else stream.transient
            ),
            usage_limit=usage_limit,
            # #434: what had failed the run before a limit killed the helper,
            # an ask that failed it first (R2-F1 of PR #489): the ask cut the
            # run, so a limit that then fell due in the wind-down is the
            # teardown's, never an idle timeout to relaunch (ADR 0007 D7.6).
            failed_before_limit=(
                (
                    CLAUDE_SDK_ASK_FAILED_BEFORE_LIMIT
                    if ask_failure is not None
                    else (session.failure_kind or stream.failure_kind or stream.api_error)
                )
                if timed_out
                else None
            ),
            # #479: the helper's own account proves the reply the run's last
            # word: the completed main turn, the end after it, every record read.
            reply_delivered=failure is None and session.reply_proven,
            provider_summary=summary,
            timeout_limit=(res.timeout_limit or cut_limit) if timed_out else "",
            last_activity_at=res.last_activity_at,
            sandbox=res.sandbox,
            interaction=interaction,
        )
        result = _finish_loop(result, monitor, killed=cut is _SdkCut.LOOP)
        return _name_refused(
            result, stream.refused, stream.refused_count, self.name, "the Claude Agent SDK"
        )


class OpenCodeSdkProvider(_SdkProvider):
    """``opencode-sdk``: the OpenCode SDK in a Node bridge (ADR 0007; #152).

    Its model credential is the one variable the profile names in
    ``options.credential_env``; no stored sign-in is read (D10.2).
    """

    name = OPENCODE_SDK
    environment_names = ()
    option_keys = OPENCODE_SDK_OPTION_KEYS
    path_names = ("OPENCODE_CONFIG_DIR",)
    transport_issue = "152"
    interpreter = "Node executable"
    runtime_decision = "D2.5"

    def fresh_context_proof(self, profile: ProfileConfig) -> str:
        """None: every launch is refused until #152 (ADR 0015 D14)."""
        raise no_fresh_context(
            profile,
            f"every {self.name} launch is refused until #152, so none is proven",
            "Route the phase to another provider",
        )

    def validate_profile(self, profile: ProfileConfig) -> None:
        super().validate_profile(profile)
        where = f"profile {profile.name!r}"
        problem = opencode_model_problem(profile.model)
        if problem:
            raise ConfigurationError(
                f"{where}: {self.name} model must be an OpenCode model id: {problem}, "
                f"got {profile.model!r}"
            )
        if profile.effort and not _OPENCODE_SDK_EFFORT_RE.fullmatch(profile.effort):
            raise ConfigurationError(
                f"{where}: {self.name} effort must be a reasoning variant name (lower-case "
                f"letters, digits, '_' and '-'), got {profile.effort!r}"
            )

    def policy(self, profile: ProfileConfig) -> OpenCodeSdkPolicy:
        return opencode_sdk_policy(profile.name, profile.options)

    def policy_environment_names(self, profile: ProfileConfig) -> tuple[str, ...]:
        roots = OpenCodeSdkRoots.under(Path("/"))  # by name only: no path is used
        return tuple(self.policy(profile).environment(roots))

    def runtime_plan(self, profile: ProfileConfig, pin: HelperPin) -> str:
        policy = self.policy(profile)
        argv = policy.server_argv(policy.server_command)
        return (
            f"options.server_command {policy.server_command!r}, resolved on the launch's "
            f"PATH by the adapter and started by the bridge as `{' '.join(argv)}`, with a "
            "fresh server password in its environment only (ADR 0007 D2.5, D3.1, D3.4)"
        )

    def node_version_command(self, profile: ProfileConfig) -> list[str]:
        """``doctor``'s read-only probe of the Node ``command`` names (D2.6, D4.4)."""
        return [profile.command, "--version"]

    def own_names(self, profile: ProfileConfig | None) -> tuple[str, ...]:
        if profile is None:
            return ()
        return (self.policy(profile).credential_env,)

    def sandbox_requirements(
        self, profile: ProfileConfig, command: list[str], env: Mapping[str, str], home: str
    ) -> ProviderRequirements:
        """``opencode-sdk``'s declaration (ADR 0005 D7.1, D13.1; ADR 0007 D10.2).

        The profile's ``credential_env`` is an ``env`` item, the only
        credential; no OpenCode store is projected. The policy's roots
        (:class:`autoforge.sdk_policy.OpenCodeSdkRoots`) are #152's to
        project. The operator's OpenCode configuration, data and cache
        directories are the operator configuration roots.
        """
        needs = super().sandbox_requirements(profile, command, env, home)
        credential = self.policy(profile).credential_env
        auth = (AuthItem(AuthMode.ENV, credential),) if env.get(credential) else ()
        return dataclasses.replace(
            needs,
            auth=auth,
            operator_config_roots=(
                os.path.join(home, ".config", "opencode"),
                os.path.join(home, ".local", "share", "opencode"),
                os.path.join(home, ".cache", "opencode"),
                os.path.join(home, ".opencode"),
            ),
        )


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
        # The description each sandboxed request compiled to: the engine's
        # facts alone, since a scripted agent declares nothing (ADR 0005).
        self.descriptions: list[LaunchDescription] = []

    def build_command_for(self, profile: ProfileConfig, prompt: str) -> list[str]:
        return ["scripted-agent", f"--profile={profile.name}", "--", prompt]

    def fresh_context_proof(self, profile: ProfileConfig) -> str:
        """The tests' provider runs no process and keeps no session (ADR 0015 D14)."""
        return "scripted: no process, so no session"

    def execute(self, req: AgentRequest) -> AgentExecutionResult:
        self.calls.append(req)
        if req.sandbox is not None:
            self.descriptions.append(req.sandbox.describe(ProviderRequirements(), req.env or {}))
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
    CLAUDE_SDK: ClaudeSdkProvider,
    OPENCODE_SDK: OpenCodeSdkProvider,
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


def configured_providers(
    profiles: Iterable[ProfileConfig],
) -> list[tuple[AgentProvider, tuple[str, ...]]]:
    """Each provider ``profiles`` route to, once, and the names it adds to their agents.

    A profile whose provider cannot say (an unknown provider, options its
    policy refuses) is passed over: a config may carry a profile a run never
    reaches, and :func:`autoforge.config.validate_profile` refuses one it does.
    """
    found: dict[str, tuple[AgentProvider, list[str]]] = {}
    for profile in profiles:
        try:
            provider = provider_for(profile)
            names = provider.own_names(profile)
        except ConfigurationError:
            continue
        found.setdefault(profile.provider, (provider, []))[1].extend(names)
    return [(provider, tuple(dict.fromkeys(names))) for provider, names in found.values()]


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
