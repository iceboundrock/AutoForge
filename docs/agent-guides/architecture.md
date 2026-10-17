# Architectural boundaries

Read this before adding or moving responsibilities between the engine, a
provider adapter, the executor, the GitHub client, or the prompt system.
That includes any change that would make one of them know something
belonging to another: a CLI flag in the engine, workflow state in the
executor, a raw `gh --json` dictionary crossing into business logic, or a
long prompt as a Python string literal.

---

## Architectural boundaries

Keep these responsibilities separate.

### Controller / engine

Responsible for:

- state machine execution
- legal transitions
- phase orchestration
- execution profile selection
- retry policy
- timeout policy
- lock acquisition
- persistent state
- crash recovery
- idempotency
- result validation
- GitHub state verification
- safety gates

The engine must not contain Claude Code or OpenCode CLI-specific flag logic.

`step()` is the core primitive; `run()`/`resume` just loop it until a stop
phase (`READY_FOR_MERGE`, `DONE`, `BLOCKED`, `FAILED`). With the merge gate
open (`safety.allow_merge: true` **and** `--allow-merge`) `READY_FOR_MERGE`
is no longer a stop phase: the loop continues through the controller-side
verification, `MERGE` and `UPDATE_EPIC`. Which module owns which concern is
listed in the module map in `src/autoforge/AGENTS.md`.

### Provider adapters

Provider-specific code translates an abstract agent request into a real CLI invocation.

Examples:

```text
AgentProvider
  -> ClaudeCodeProvider
  -> OpenCodeProvider
  -> PiProvider          (EPIC #127, ADR 0003: config, argv and doctor checks
                          in #129; execution over RPC in #131; the resource,
                          tool and credential policy in #132, docs/pi-policy.md)
  -> ClaudeSdkProvider   (planned, ADR 0007: #151)
  -> OpenCodeSdkProvider (planned, ADR 0007: #152)
```

Provider adapters own:

- CLI argv construction
- model identifier mapping
- reasoning / effort mapping
- non-interactive execution details
- provider-specific compatibility handling
- the names of the environment variables its CLI reads
  (`AgentProvider.environment_names`, added to the engine's allow-list so a
  provider's key is never handed to another provider's CLI)
- what its CLI needs from the host inside the outer sandbox, as data
  (`AgentProvider.sandbox_requirements`, ADR 0005 D13.1; see
  [Launch routing](#launch-routing-adr-0005-168))

Every invocation is an argv list: there is no `os.system` / `shell=True`
anywhere, so shell metacharacters in issue text cannot be interpreted. The
adapter decides how the prompt reaches its CLI, and it never goes through a
shell:

- **One argv element** (Claude Code). The adapter's `build_command_for`
  puts it after `--`.
- **Raw bytes on stdin, then EOF** (OpenCode 2, #186, whose argv corrupts a
  message). The adapter's `stdin_payload` returns the bytes and the
  executor writes them (`ExecutionRequest.stdin_data`). The executor
  counts what the child left unread (`ExecutionResult.stdin_unread`), and
  the base `AgentProvider.execute` turns an exit 0 with any of the prompt
  unread into a `provider_failure`.
- **One record of a stdio protocol** (Pi over RPC, ADR 0003). The prompt is
  a JSON `prompt` record that the adapter writes through the duplex child
  handle (`executor_duplex.py`). Pi's protocol, not the executor, reports
  whether it was taken.

The two stdin paths keep the prompt out of argv, and so out of `ps`.

The provider layer is `providers.py` plus the protocol modules an adapter
owns. Pi's wire protocol (JSON encoding and decoding, request ids, the event
reducer, outcome classification) lives in `pi_rpc.py`, a pure reducer that
`PiProvider` drives over the duplex child handle (`executor_duplex.py`). It knows Pi's
command and event names, but no CLI flags and no processes. Claude Code's
`stream-json` output is reduced the same way by `claude_stream.py`, which
`ClaudeCodeProvider` drives over the same handle with stdin on `/dev/null`
(#192): the last `result` record's text becomes `stdout` (the CLI ends
every turn with one, and runs another turn when a background task or a
scheduled wakeup fires; while a background agent runs it holds the results
back and sends them after the last turn, in turn order, so the n-th result
belongs to the n-th turn's `system/init`), and an error result in any turn, a result
outside a turn, a malformed line or a turn without its result becomes
`provider_failure`. The CLI's exit, not EOF, ends its wait for lines: the
adapter looks at the CLI after every read, a line included, so a leftover
holding its stdout, silent or still writing, gets ADR 0002's exit grace and
kill, never the deadline, and the lines it wrote meanwhile are validated
like the rest. No CLI flag and no provider wire-protocol name appears
outside the provider layer.

Live progress crosses the boundary in one provider-neutral shape. The engine
puts a sink in `AgentRequest.progress`; the adapter maps its own records to
`ProgressEvent`s (`progress.py`: a closed `kind`, a tool name, one
allow-listed detail, a token estimate), already cleaned, redacted and
bounded, and the engine's `ProgressReporter` renders them as lines for the
step's `progress.log` and, when the CLI sets `progress_output`, stderr. The
engine and the CLI never see a provider record, and a sink that raises is
dropped for the rest of the invocation: progress never changes an outcome.
A provider whose output says nothing structured (OpenCode) reports only
`activity`, from the executor's `on_output` hook, which is told a chunk
arrived on which stream and never sees its bytes.

Loop detection (#194) lives in the provider layer too. `loop_detect.py` is
pure and provider-neutral: a `LoopMonitor` is fed monotonic times with tool
names and opaque digests, output bytes and provider retries, reads no
clock, and keeps no tool input, result, output line or digest. The protocol
reducers feed it each completed tool call as a fingerprint (tool name,
digest of the full input, digest of the full result), plus their retries
and turns (`claude_stream.py` through the `LoopObserver` protocol,
`pi_rpc.py` directly). The base `AgentProvider.execute` feeds it the output
bytes from the executor's `on_chunk` hook, for the repeated-lines signal.
Warnings go out as `ProgressEvent(loop_suspected)`, which the reporter
prints and never counts as the agent's activity. In `kill` mode a verdict
makes the adapter stop the child (`ExecutionRequest.stop` or
`DuplexChild.stop()`, ADR 0002 §4d). It then reports the outcome as a
timeout, `timed_out` with `timeout_limit` `loop`, plus the
`AgentExecutionResult.loop` report. The engine therefore handles a loop kill
on its timeout path, adds the report's description to the error and writes
the report to `execution.json`. In every mode the adapter merges the
monitor's calibration figures into `provider_summary`. The executor never
learns why it was stopped.

A provider can report that a run failed inside its protocol even though the
process exited 0. It does so through a provider-neutral optional
`AgentExecutionResult.provider_failure` (ADR 0003 §2.6), which the engine
treats like a non-zero exit: `ExecutionError`, state unchanged, stdout and
stderr recorded. The engine checks `timed_out` first, then
`provider_failure`, then the exit code, then the parser. A flat, bounded,
redacted `provider_summary` (scalars only) goes to `execution.json`. The engine names no provider in that
check. The adapter returns the text the parser reads as `stdout`, which for
Pi is the final assistant message, never raw protocol records.

An adapter may also report a failed run as a known transient failure of its
provider (`transient_failure`, a short fixed reason). It is a fact,
recognised from the CLI's own output by the one layer that knows that
output, and it never replaces the exit status or `provider_failure`: the
launch is logged and reported as the failed exit it was. Whether to relaunch
is the engine's policy, keyed on that field alone (no provider name, no
error text): in REMOTE mode at most `MAX_TRANSIENT_RETRIES` (2) relaunches
per step, counted apart from the CONTROL_RESULT corrections, each after the
checkout check and the phase-entry reconciliation, and none while a process
the failed launch started may still be running. Today the one
classification is OpenCode's: an exit 1 of an `openai/*` model whose
run-level errors, read from the error lines OpenCode itself prints rather
than from text anywhere in its trace, are all
`WebSocket closed with code 1000` (the adapter's docstring says what that
check cannot see).

#### SDK providers and interactions (planned, ADR 0007)

Nothing in this subsection is implemented yet. ADR 0007 decides how an SDK
provider fits these boundaries:

- **No SDK in the controller.** An SDK runs only inside a helper process
  (a Python helper around `ClaudeSDKClient`, or a Node bridge over
  `@opencode-ai/sdk`). The helper is launched like any agent, through the
  duplex handle, the allow-listed environment, containment and the outer
  sandbox, under the idle timeout and the ceiling.
- **The helper owns its runtime.** The CLI or `opencode serve` the helper
  starts is its child, in its process group, under one containment owner.
- **Pinned pairs.** SDK and runtime versions are pinned exactly, and a
  mismatch is a provider failure.
- **Interactions go through a provider-neutral contract and broker** in
  `interaction.py` (#134), handed to the adapter like `progress`. The
  adapter maps provider asks to it, and the engine never sees an SDK type.
  The `pause` policy, its state and recovery are #126's.
- **A capability exists only after its gate passes.** The gate is an
  isolated runtime probe, per ADR 0007 §3.

### Executor

The process executor owns:

- subprocess lifecycle
- timeout
- stdout/stderr capture
- exit status
- process termination
- timestamps
- environment selection: a request carrying `env_allowlist` starts the
  child from only the named variables (exact names or `PREFIX*`) of the
  controller's environment, never from a copy of all of it
- an optional output observer (`on_output`), told which stream a chunk
  arrived on and never its bytes, called by one stream at a time, and
  unhooked for both streams the first time it raises

It should remain independent of workflow semantics. Which names are
allowed is policy: the engine reads it from `execution.env_allowlist` (plus
`env_allowlist_extra`) and applies it to every agent launch,
`merge.verification_commands` entry and `local.validation_commands` entry;
the controller's own `git` / `gh` plumbing inherits the full environment.
A request's `env` mapping is layered over the selected variables; the
executor sets it without interpreting it. What goes into it is
`child_env.py` (#165), provider-neutral and called by the engine for all
three kinds of launch: the names no allow-list may carry (refused at
config load and in an adapter's `environment_names`), the per-launch
variables that leave a child no GitHub or git credential (an empty `gh`
configuration, a placeholder token, an identity-only global git
configuration, git command-scope resets, empty curl and wget
configuration), and the preflight that refuses a launch a repository
setting would still hand a credential. Provider adapters pass
`AgentRequest.env` through to the executor (Pi's auth preflight included)
and keep only what keeps their own CLI's integrations off: flags (Claude
Code's `--safe-mode`, `--strict-mcp-config` and `--setting-sources=user`,
Pi's #132 policy) or, where a flag does not reach, the variables of
`AgentProvider.integration_isolation` set over the controller's
(OpenCode's empty configuration directory, Claude Code's
`CLAUDE_ENV_FILE` cleaner and shell); an integration neither reaches is
refused by `AgentProvider.integration_preflight`, which the engine calls
with the credential preflight (OpenCode's connected organization, Claude
Code settings whose `env` would undo the launch's isolation); `doctor`
calls the same module and hook.
The engine also owns *where* an agent runs: in REMOTE mode a per-issue
`git worktree` it creates under `<git common dir>/autoforge/worktrees/<n>`
(or `execution.worktree_dir`) at the first launch and never deletes, and it
reads HEAD and branch of its own checkout before and after each invocation,
including before every relaunch within a step (a correction or a transient
retry), entering `BLOCKED` on a change before anything is relaunched.

Nothing an agent starts outlives its invocation
([ADR 0002](../adr/0002-executor-nothing-outlives-the-invocation.md)). The
child is started in a new session (`start_new_session`), so it leads a
process group of its own that the executor can reach and kill. An
invocation is complete when the child has exited, both pipes reached EOF
*and* no process is left in the child's process group. The child's exit is
bounded by the request's limits; past one the whole group is killed and the
result is a timeout. An agent launch has two (ADR 0002 §4c, #193): an idle
timeout (`ExecutionRequest.idle_timeout_seconds`), counted from the spawn
and then from the last chunk either reader saw, and a wall-clock ceiling
(`timeout_seconds`, `MAX_DEADLINE_SECONDS` when the profile sets none).
Whichever is due first fires, a tie goes to the ceiling, and
`ExecutionResult.timeout_limit` (`idle` or `max_runtime`) and
`last_activity_at` say which and when the child last wrote. A command that
is not an agent has no idle timeout, only `timeout_seconds`
(`execution.command_timeout_seconds`). Which limits a profile gets, and that
a provider whose output is no progress signal (Claude in text mode,
`reports_activity`) runs under its ceiling alone, is configuration
(`AutoForgeConfig.agent_limits`), not executor policy. The caller may also
end the invocation early (ADR 0002 §4d, #194): it sets
`ExecutionRequest.stop` (or calls `DuplexChild.stop()`), and the group is
killed through the same path; the result says `stopped`, never why.
`ExecutionRequest.on_chunk` hands the caller every chunk read, after it is
captured. The reason to stop, a loop, is the provider layer's
(`loop_detect.py`, below). Once the child has
exited on its own, the other two conditions
are given a short exit grace (`_EXIT_GRACE_SECONDS`): a child that left
nothing behind clears them at once, a helper it is shutting down as it
exits clears them within the grace, and a descendant still there past it
(a server holding the inherited pipes, or one with its stdio redirected
that only a group liveness check can see) is killed with the rest of the
group. The child's own exit status and output are then returned, so a valid
`CONTROL_RESULT` is not lost to a leftover and the phase does not cost the
whole timeout, and `ExecutionResult.descendants_killed` records the kill.
The two flags are exclusive: `timed_out` means the child itself overran,
`descendants_killed` that it exited and its leftovers were removed.

A request with `contain_orphans` (every agent launch and every
repository-defined command; not the controller's own `git`/`gh` plumbing)
also catches what left the group (ADR 0002 §4b). On Linux the controller is
a child subreaper for the invocation, so a detached process whose parent
died is re-parented to it. Such orphans get the same grace and kill, and
are reported as `orphans_killed` / `orphan_survived_kill`. Without a
subreaper the result says `orphans_unchecked`. One contained invocation
runs per controller process. Orphans that die while the child runs are
reaped as they die by a reaper thread. Otherwise their zombies would count
against the session's process limit until no fork succeeds.

`execute()` itself stays one-shot: what the child gets on stdin is fixed
before the spawn, and nothing the child writes back changes it. By
default (`ExecutionRequest.stdin_data` is `None`) its stdin is
`/dev/null`, so a CLI that waits for interactive input cannot hang. With
`stdin_data`, stdin is a pipe that a feeder thread fills with those bytes
and then closes, so the child reads the payload and then EOF (the
OpenCode prompt, above). The feeder writes non-blocking and the teardown
wakes and joins it, so a child that never reads its stdin still ends at
the deadline. It keeps the pipe's read end until the invocation is over,
so `ExecutionResult.stdin_unread` counts what the child left unread, not
only what was never written.

A child the controller must keep writing to while it runs, in step with
what it reads back (an RPC transport such as Pi's, ADR 0003 §2.7), uses
the duplex child handle in `src/autoforge/executor_duplex.py` instead:
`with start_duplex(DuplexRequest(...)) as child:` then
`send_line`, `read_line`, `exited`, `close_stdin` and `finish`. It reuses the
executor's spawn, environment selection, pipe draining and group kill, and
it is held to the same contract:

- stdout is split into records on LF (0x0A) only, one trailing CR stripped
  per record; U+2028/U+2029, NUL and every other byte are record content.
  Records are bytes and decoding them is the caller's. A record over
  `max_record_bytes` is reported as `Oversize` and dropped through its LF;
  an unterminated fragment at EOF is reported as `Fragment`, never as a
  record;
- both pipes are drained continuously whether or not the caller reads.
  Records wait in a queue bounded by `max_pending_records` and
  `max_pending_bytes`, and exceeding it is `Overflow`, which ends the
  invocation (a child still running is killed at `finish()`). stderr goes
  to the same head/tail capture as `execute()` and is never a record;
- `send_line` writes one whole record under one lock with non-blocking
  writes and a selector, so a child that stops reading stdin cannot hold
  the controller past the deadline. A child that closed its stdin raises
  `ChildStdinClosedError`;
- the same two limits (`DuplexRequest.idle_timeout_seconds` and
  `max_runtime_seconds`) bound every `send_line`, `read_line` and `finish`;
  only a chunk read from the child is activity, never a record sent to it.
  `limit_due()` is when the one due first falls due and `deadline_limit`
  names it; `deadline()` is that plus `wind_down_seconds` (0 unless a
  caller winds the child down in order: Pi asks for its abort window, so
  its `abort` goes out as the limit falls due, never before it, and the
  kill follows the window later); `pin_deadline()` fixes both so later
  output no longer moves them, which every Pi `abort` and the finish after
  it use to stay inside a fixed budget. Past
  the deadline the group is killed exactly as on `execute()`'s timeout, and
  the result is `timed_out` with `exit_code = -1` and `timeout_limit`. As in `execute()`, whose last
  look at the child is at the deadline, the child is looked at first: a
  `read_line` or `finish()` that meets the deadline after the child has
  exited is not a timeout, and settles the group with the exit grace and the
  kill, the child's status kept (`read_line` then returns the rest of
  stdout). A limit that is not
  finite, not positive or above `MAX_DEADLINE_SECONDS` (a week, within what
  every wait primitive can take) is refused with `ExecutionError` before
  anything is spawned;
- EOF is not the child's exit: a descendant holding stdout keeps EOF away
  for as long as it lives, and one that keeps writing keeps every read busy.
  `exited()` reports the child's own exit without waiting, so a caller that
  reads records until EOF checks it after every read, a record included,
  and calls `finish()` once the child has exited (the exit grace, then the
  kill, the child's status kept). After a `finish()` that did not time
  out, `read_line` returns the records still queued and then the end item,
  never waiting and never reporting the deadline;
- the `with` tears down on every exit, an exception or `KeyboardInterrupt`
  included: stdin is closed (an orderly-shutdown request), the child gets a
  bounded wait to exit (the deadline from `finish()`, at most the exit grace
  on an exception), then the exit grace and empty-group check, then the
  group kill (SIGTERM first, then SIGKILL). `DuplexResult` reports
  `descendants_killed`, `group_survived_kill` and `capture_abandoned`
  exactly as `ExecutionResult` does for the same leftovers, and the
  invocation takes at most the deadline (one idle timeout after the child's
  last output, or the ceiling, plus any wind-down) plus the exit grace plus
  two kill graces.

The handle knows no JSON, no provider and no workflow. A server child that
takes no input (#126) uses it with `stdin_pipe=False` and
`stdout_mode=StdoutMode.CAPTURE`.

The kill is complete only when no process is left in the group, not merely
when the child is reaped and its pipes closed: a descendant that closed its
inherited stdio and ignores SIGTERM is caught by that check and escalated
to SIGKILL. Every wait in the kill is bounded by the kill grace period, and
nothing is waited for past the SIGKILL grace: not a writer the group kill
cannot reach (a descendant that also called `setsid`), and not a member that
survives SIGKILL itself (uninterruptible in the kernel), the direct child
included. The capture is then abandoned and an unreaped child is left to the
`subprocess` module, so `execute()` returns within its deadline (one idle
timeout after the child's last output, or the wall-clock limit, whichever is
due first) plus the exit grace plus two kill grace periods whatever the
child left behind. What the
kill could not remove is reported rather than presented as a clean kill:
`group_survived_kill` (a member was still in the group after the SIGKILL
grace) and `capture_abandoned` (a pipe never reached EOF, so a writer
outside the group still holds it). `ExecutionResult.leftovers` renders the
three facts as one sentence; the engine records all three in
`execution.json` / `events.jsonl` for every invocation and appends the
sentence to a timeout's or a failed exit's error, so the operator looks for
the leftover process instead of a slow agent. None of them changes how the
child's own result is read: they are facts for the log, not workflow
semantics.

Capture is bounded and lossless in encoding terms: each stream is read as
bytes and decoded as UTF-8 with replacement (a stray byte in agent output is
data, never an exception), and each stream keeps at most
`ExecutionRequest.max_output_bytes` (default `DEFAULT_MAX_OUTPUT_BYTES`, well
above any legitimate transcript): the first half, an omission marker and the
last half. The bound is on what the reader retains, trimmed to the byte, so
it holds for any positive value, a bound smaller than one pipe read
included, and it is a bound on memory, not only on payload length: the tail
is a ring buffer, so a stream delivered one byte per read costs the bound
plus a constant, never a per-chunk object. Within the bound the head/tail
split is invisible (the stream is decoded whole, so a multi-byte character
that straddles it is intact); past the bound a character cut by the bound
decodes to replacement characters beside the marker, which is what was
captured. `ExecutionResult` reports `stdout_truncated` / `stderr_truncated`
and exposes `stdout_tail`, the part of stdout captured contiguously up to
EOF, which is the only part a CONTROL_RESULT parser may search. A caller that
parses a command's output as a whole (`gh --json`, `git` plumbing) treats a
truncated result as a failure, the way it treats a timeout; the executor's
`raise_if_failed()` and `ok` do the same. How the engine handles a truncated
agent stdout is in
[control-result-protocol.md](control-result-protocol.md).

#### The outer sandbox (ADR 0005, #167)

Both `ExecutionRequest` and `DuplexRequest` take an optional `sandbox`: a
`LaunchDescription` (`src/autoforge/sandbox.py`) that says what one launch
can reach. A request without one takes exactly the path it took before.
The description is typed, immutable and backend-neutral, and it knows no
workflow, provider or GitHub: a projection is a host path, an access mode
(read-only or writable) and the path it appears at; then come the masks,
the overlays, the private home and temp, the environment overrides for the
path-valued names (`HOME`, `XDG_*`, `TMPDIR`), the network position and
the `/proc`, `/dev` and `/sys` policy.

The policy compiler (`compile_launch`, same module) is the only way to
build one from facts. Its input, a `SandboxRequest`, is plain data: the
controller's facts (the operator's home, its environment for the
forbidden locations, the denied roots, the engine's projections, masks and
overlays) and the declared requirements (`Origin.DECLARED` projections, the
operator configuration roots, keyring logins). It applies ADR 0005's rules
before anything is spawned:

- D4.2 C1–C7: resolved sources; a controller-owned source that is or passes
  through a link is refused; directories and regular files only; denied
  roots masked or refused; carve-outs are the engine's; overlaps refused,
  except a declared read-only path already visible, which is dropped and
  recorded in `covered`; relocations only into the private home;
- D4.3: the forbidden sources (the home and its ancestors, the gh and git
  configuration and credentials, `~/.ssh`, `~/.gnupg`, keyrings, the
  password store, `/run`, `$XDG_RUNTIME_DIR`);
- D7.2: keyring logins, and writable declarations on an operator
  configuration root.

A refusal is `SandboxPolicyError`, naming the path and the rule, never a
file's contents. The compiler is pure except for `realpath`, `lstat` and
`readlink` of the paths it is given and of the fixed system paths. Which
paths a launch class gets is the engine's (#168), not the compiler's.

The Linux backend is `src/autoforge/sandbox_bwrap.py`, the only module
that knows a `bwrap` flag (a test pins it). `prepare()` turns a description
and a command into a `BwrapLaunch`, and the executor spawns that in the
command's place: the same `start_new_session`, pipes, limits, containment,
group kill and exit grace. Every launch gets:

- user, mount, PID and IPC namespaces; `--unshare-net` only when the
  description asks for an isolated network;
- a new procfs and a minimal `/dev`;
- an empty tmpfs root with exactly the description's mounts, with masks
  made read-only after their carve-outs;
- `--clearenv` and exactly the computed environment, passed with every
  option through a memory file (`--args`), so no value is on a command
  line;
- `--die-with-parent`;
- in the launch's own process, never in the controller: a new session
  keyring and a Landlock domain that scopes abstract Unix sockets.

Whether the command ran is read from `--json-status-fd`. A `bwrap` that
exited by itself without an `exit-code` record is a setup failure, raised
as `SandboxLaunchRefusedError` (an `ExecutionError`, on the
failure-to-spawn path) with nothing attributed to the command. A `bwrap`
ended by a signal the executor did not send is the command's abnormal
end.

Launches are refused, never run unsandboxed:

- on a platform that is not Linux;
- without `bwrap` on `PATH`;
- on a kernel without Landlock ABI 6;
- on an architecture whose keyring call is unknown.

`probe()` is the minimal launch that proves a host can enforce; its
operator-facing use is #169's.

What changes for a sandboxed launch (ADR 0002 §4e):

- When the command exits or `bwrap` is killed, the kernel kills
  everything left in the namespace at once. Inner processes get no grace,
  and they are never counted.
- `ExecutionResult.sandbox` / `DuplexResult.sandbox` (`SandboxFacts`)
  records the outcome, and the run log writes it to `execution.json` and
  `events.jsonl` (`ExecutionRecord.sandbox`):
  - whether the sandbox was enforced;
  - the backend and its version;
  - the teardown ("removed by namespace teardown, uncounted");
  - the network position;
  - the tmpfs bounds;
  - the unset variable names;
  - a setup failure's bounded, redacted reason.
- The thread that spawns a sandboxed launch must outlive it, because
  `--die-with-parent` fires when that *thread* exits. That thread calls
  `execute()` and waits, or calls `start_duplex()` and holds the handle
  until `finish()`.

The executor still owns no policy: it does not decide whether a launch is
sandboxed, which paths it gets, or what happens when the host cannot
enforce. That is the engine's routing (#168, below) and the configured
mode (`disabled` and `required` are #168's; `preferred` and the pre-launch
capability gate #169's, below). The facts are for the log, and the
controller never reads them (D16.1).

#### Launch routing (ADR 0005, #168)

When the outer sandbox is enforced (`sandbox.mode: required`, or
`preferred` on a host that can enforce) every launch AutoForge makes
outside itself carries a description (D3.1): every process of every agent provider
(Claude Code one-shot and stream-json, OpenCode, Pi RPC and Pi's
per-launch auth preflight), every LOCAL validation command and every
pre-merge verification command. The controller's own `git` and `gh`
plumbing stays outside. Under `disabled` no request carries one, and every
launch takes the path it took before #167. Each party contributes what it
alone knows, as data (D13.1, D13.2):

- **The engine** supplies `LaunchFacts` (`src/autoforge/sandbox_policy.py`):
  the launch class, the workspace, its `.git` entry, its git dir and the
  common dir, the issue's private object directory and overlay work
  directory, the cache, the read context, the directory the #165 overrides
  live in (`child_env.overrides_directory`), the network position and the
  denied roots. `ControllerEngine._remote_facts` and `_local_facts` derive
  them from the git directories, `_denied_roots` lists what no launch may
  see, and `_sandbox_dirs` creates the controller-owned directories through
  `SafeRoot` at the first enforced launch that needs them (a link in their
  path refuses the launch, D8.4). For an agent, `_agent_sandbox` wraps the
  facts in a `SandboxPlan` on `AgentRequest.sandbox` (`None` under
  `disabled`); for a repository command, `_command_sandbox` compiles the
  description itself and puts it on `ExecutionRequest.sandbox`.
- **A provider** declares what its CLI needs as `ProviderRequirements`
  (`AgentProvider.sandbox_requirements`): the executable resolved on the
  launch's `PATH` as the spawn would resolve it; its install root (the
  package directory under `node_modules`; else the `<prefix>` of
  `<prefix>/bin/<file>` when it has a `lib` directory, as an interpreter's
  standard library needs, unless that prefix is the home, an ancestor of it
  or `~/.local`; else its directory; never all of the home) and, for a
  script, its interpreter's root, whose directory goes first on `PATH`,
  and an absolute `#!` path as written (`spelled_paths`), `env`'s too,
  with its resolved program's root, whose links the compiler recreates
  inside without projecting what holds them (#225);
  configuration files (read-only) and state directories the adapter made
  for the launch (writable); auth items (`AuthItem`:
  `env`, `file-ro`, `dir-rw` or `keyring`, D7.1); its operator
  configuration roots (D7.2); and its path-valued variable names
  (`path_names`, D8.2). Claude Code, OpenCode and Pi each override it; what
  they declare is in
  [Configuration](../configuration.md#provider-credentials-under-required).
  `AgentProvider.sandboxed()` is the one place a declaration meets the
  plan: it builds the declaration from the process's environment, compiles
  through `SandboxPlan.describe` and names the executable by its resolved
  host path, which the description shows read-only. Every adapter calls it
  for every process it starts (the base one-shot `execute`, Claude's
  stream-json duplex, Pi's RPC duplex and Pi's auth preflight, which must
  find the credential exactly where Pi will). A provider never emits a
  mount, and no provider name appears in `sandbox_policy.py`.
- **`sandbox_policy.py`** is the engine-layer mapping of ADR 0005 §2.5:
  `sandbox_request` turns the facts and a declaration into the compiler's
  `SandboxRequest` by launch class alone (a repository command gets no
  provider resource whatever is passed), and `describe_launch` compiles it
  and rewrites the environment. It runs no subprocess and creates nothing;
  it is pure apart from the compiler's `realpath` and `lstat`, `realpath`
  of environment values and the first line of an executable. A compiler
  refusal becomes `SandboxLaunchRefusedError` naming the path and the rule
  (`policy_refusal`), before anything is spawned.

The launch classes (D5.1, D6.1, D6.6; ADR 0005 §2.5 is the full table):

| Launch class | Writable | Read-only | Masked |
|---|---|---|---|
| REMOTE agent | the issue worktree; its admin dir `<common>/worktrees/<id>`; an overlay at `<common>/objects` whose upper layer is the issue's private object directory | the worktree's `.git` file and the admin dir's `commondir` and `gitdir` (pinned); the git common dir; the phase's read context | `<common>/autoforge`, `<common>/worktrees`, `<common>/modules`, with the worktree (at its default location) and its admin dir carved out |
| LOCAL agent, LOCAL validation command | the repository root | the root's `.git`, the git dir (a linked checkout's admin dir) and the common dir | `<git dir>/autoforge`, `<common>/autoforge`, `<common>/worktrees` |
| export command (pre-merge) | the exported tree | | |

Every class also gets the system runtime, a private home and `/tmp`, the
#165 overrides directory read-only, and the engine's denied roots (D13.2):
the state directory and its run logs wherever they resolve, the lock,
`<common>/autoforge`, every other registered worktree, for REMOTE the
controller's checkout, and for LOCAL `<git dir>/autoforge` and the default
REMOTE state directory `<root>/.autoforge`. A persistent cache is mounted
at `~/.cache` only for the launches that produce a change
(`ANALYZE_EXECUTE`, `FIX`, `REPLAN_REEXECUTE`: per issue in REMOTE, per
checkout in LOCAL) and for LOCAL validation; every other agent launch and
an export command get the one in their private home. An agent always
shares the host network; a repository command gets
`sandbox.validation_network` or `sandbox.premerge_network` (D9.2).
`sandbox.read_only_paths` (D15.4) is read-only in every class, declared
after the provider's roots (C6 decides overlaps; one inside a provider root
is dropped, a provider root inside one is dropped in its favour); one under
the home appears at its spelling in the private home.

**The mode and the gate** (#169, D12.2, D13.5). `engine.probe_sandbox()`
runs `sandbox_bwrap.probe()`, one real minimal launch of a real launch's
shape whose refusal is typed (`Capability`: `bwrap` missing, the user, PID
or IPC namespace, procfs, bind, overlay, Landlock, the session keyring;
`bwrap`'s generic namespace failure is narrowed by one launch per
namespace). `ControllerEngine.sandbox_capability()` keeps its answer for
the engine's process. `_sandbox_gate()` runs at the start of
`_step_once`, before validation recovery, planning, the step budget and
anything saved, for a step that may launch (`_step_may_launch` in REMOTE;
ANALYZE_EXECUTE, REVIEW and FIX in LOCAL): an operator path over a denied
root, or a host that cannot enforce under `required` or REMOTE's
`preferred`, raises (`ConfigurationError`, `SandboxCapabilityError`) with
the state byte-identical. `_sandbox_enforced()` is what every launch site
asks (`_agent_sandbox`, `_command_sandbox`): `False` under `disabled`
without a probe; `True` on a capable host; under LOCAL's `preferred` on an
incapable one `False`, and `_fallback_facts()` / `_fallback_notice()` then
put `{"enforced": false, "mode": "preferred", "isolation": "logical
isolation only", "fallback": <reason>}` in the execution record and a line
on stderr (`progress_output`). The fallback is decided from the probe
alone: a launch the backend refuses after the gate stays a spawn failure
and is never re-run unenforced. The dry-run plan (`_sandbox_plan_notes`)
and `status` (`sandbox_mode_summary`) never probe; the plan shows
`sandbox_policy.class_summary` over `summary_facts` (role paths, nothing
created) for each class the run can make. `doctor_sandbox.py` holds
`doctor`'s rows. Their denied roots are the run's own: `doctor` binds an
engine as the CLI would for the run (its state directory, lock, git
directories and registered worktrees) and asks it for
`gate_denied_roots()`, the set the gate holds operator paths against. Each
provider's declaration compiles with them too, so a declaration that
projects what no launch may see is refused before its `--version` is
launched. That `--version` gets the environment the provider's launch
gets: the allow-listed variables with the adapter's
`integration_isolation()` over them, held until the launch has ended, and
the compiled description passed through `rewrite_environment`. Its output
is redacted before its first line is picked and clipped.

The environment is rewritten (D8.2, `rewrite_environment`): a path-valued
name AutoForge knows (the CA-bundle names of `DEFAULT_ENV_ALLOWLIST`,
`child_env.OVERRIDE_PATH_NAMES`, the provider's `path_names`) whose
absolute value is not visible inside (`LaunchDescription.inside_path`) is
unset; a value that is not an absolute path passes unchanged; `PATH` keeps
only its visible entries, after the provider's interpreter directories,
and is unset when none is left. A kept value, a kept `PATH` entry and the
variable an auth item names (`PI_CODING_AGENT_DIR`, `CLAUDE_CONFIG_DIR`)
are spelled where they are inside (`inside_spelling`): as given when that
spelling leads there inside (`LaunchDescription.resolve_inside` looks each
component up in the description, following the links it recreates and
those it shows), else where the compiler projected what they name: a link
in the private home or where the root is empty is absent inside, and the
cache appears at `~/.cache`. The launch's facts record every unset name.
A file the CLI finds by its path in the home (Claude Code's
`~/.claude/.credentials.json` and `~/.claude/settings.json`) is declared
at that path (an auth item's `target`, `home_config`), so it appears
there even when its host path is a link to a file elsewhere (C7).

**The git surface, in every mode** (`src/autoforge/agent_worktree.py`,
D6.3–D6.5). The redirect these close needs no sandbox, only a writable
`.git` file, so the engine applies them to a REMOTE issue worktree whatever
the mode. Every git process is the controller's own `local_git_request`
(ADR 0004 D7.3, D7.5), run through the executor.

- **Binding (D6.4).** `bound_admin_dir` finds the worktree's admin dir
  through the registry: exactly one `<common>/worktrees/<id>/gitdir` names
  `<worktree>/.git`, and that `.git` is a regular file naming the entry
  (absolute or relative). `_ensure_agent_worktree` binds a worktree it
  creates and one it reuses; otherwise the refusal (a `VerificationError`)
  names the inconsistency and `git worktree repair`. A worktree created
  before #168 is reused once it binds, never refused for its age (D15.2).
- **Candidate (D6.3).** `read_candidate` reads `worktrees/<id>/HEAD` with
  `git --git-dir=<common>`, after `lstat` found a regular file there
  (`head_problem`), never through the worktree's `.git`, the admin dir's
  `commondir` or `gitdir`, or `git -C <worktree>`; an attached `HEAD` is
  reported without resolving the reference it names. `_read_candidate`
  serves the `ANALYZE_EXECUTE`, `FIX` and `REPLAN_REEXECUTE` result
  checks, so a `.git` rewritten to a decoy repository cannot move the
  candidate.
- **Strict import (D6.5).** `import_private_objects` copies the objects the
  candidate reaches that the shared store lacks out of the issue's private
  object directory: `rev-list --objects` and `pack-objects` with the
  directory named through `GIT_ALTERNATE_OBJECT_DIRECTORIES` as one
  double-quoted entry, so that a `:` in its path does not split it (never
  added to the shared store's alternates), then `index-pack --stdin --fix-thin
  --strict` into the shared store. The commit is the object id `HEAD`
  holds, read by the controller itself after the same `lstat`: a `HEAD`
  that is not a regular file is an `ObjectImportError` before any git
  process runs (nothing packed or imported), and an attached one imports
  nothing. `private_objects_problem` walks the
  directory first without following a link: the directory itself, and each
  one between the common dir and it, must be a real directory (`lstat`), so
  a link, another file type or one that cannot be checked is
  `SandboxTamperError`, never an empty store (`holds_objects` is false only
  for an absent directory or an empty real one); inside it, an
  `info/alternates` or `info/http-alternates` file, or an entry that is
  neither a directory, a regular file nor an overlay whiteout, is
  `SandboxTamperError` too. The engine
  imports before every REMOTE launch (`_import_before_launch`: a failed
  import refuses the launch with `SandboxLaunchRefusedError`, before
  anything is saved) and after every launch that exited cleanly, before
  its result is parsed (`_import_after_launch`: a failure is not retried,
  and the candidate read refuses the candidate with it, a correction). The
  directory holds anything only after an enforced launch, so under
  `disabled` with no earlier `required` launch both are no-ops.

**Refusal paths.** Nothing sandboxed ever runs unsandboxed:

- an agent: a compiler refusal, a sandbox directory that cannot be
  prepared or a backend setup failure is `SandboxLaunchRefusedError`, an
  `ExecutionError` on the failure-to-spawn path: recorded, then raised with
  the phase unchanged and #189's "State unchanged." text;
- a LOCAL validation command: the refusal is logged and raised as a
  `VerificationError`; the phase is not verified;
- a pre-merge verification command: the refusal is logged and returned as
  the gate's problem, so the step enters `BLOCKED`;
- tampering with the private object directory: `SandboxTamperError`, a
  `VerificationError` that `_step_once` turns into `BLOCKED`, naming the
  entry.

`request.json` records `sandbox_mode` for every agent, validation and
pre-merge launch; `execution.json` records the `sandbox` facts of a launch
that carried a description, and of a refused one whose error carries them
(`enforced: false` and the redacted reason). The prompts say what a
narrowed view means (prompt version `v10`): `common.md` that the
operator's checkout, other worktrees, the controller's state and the home's
files may be absent and the shared git metadata read-only, and
`local_common.md` that the git directory may be read-only.

### GitHub client

Centralize GitHub integration behind a `GitHubClient` or equivalent abstraction.

Prefer typed return values rather than passing raw `gh --json` dictionaries throughout business logic.

The client is the only holder of a GitHub credential: everything else
AutoForge starts has none (#165). It is also the agent's only read path:
before a REMOTE launch the engine reads, through the client, what the
phase's prompt names and writes it into a per-launch read-context
directory; `read_context.py` renders and bounds those files with no I/O
(ADR 0004 D10, [GitHub safety](github-safety.md#the-agent-read-path)).

The client's verification reads keep their transient retry. Its typed effect
writes (ADR 0004: `create_issue_comment`, `create_pr_comment`,
`create_pull_request`, `write_pr_body`, `create_issue`, `write_issue_body`)
run outside that retry and return the created object's identity: an
ambiguous outcome is reported as unknown, never re-sent, and the effect layer
decides by reading. There is no generic passthrough of `gh` arguments.

### Controller effects and git transport

The effect layer sits between the engine and the GitHub client (ADR 0004):

- `effects.py` owns the persisted records, completion contexts and entry
  observations, their validation on load and their bounds, and the pure
  reconciliation decisions. No I/O.
- `effect_ops.py` owns one operation per effect kind, the Wave 1 set and
  #211's acceptance comment (the identity
  read, the precondition, the single write, the completion read-back) and
  `drive`, the reconcile-before-re-issue lifecycle. Each operation uses the
  typed client methods or the git transport; nothing else.
- `git_transport.py` is the controller's own git: exact-SHA,
  compare-and-swap, fast-forward-only pushes and objects-only fetches, in a
  private git directory over the shared object store, with an explicit
  remote and the credential from `gh auth git-credential`. Every process
  goes through the executor; the module has no workflow semantics. Its
  `local_git_request` is how every other controller git process in the
  operator's repository (worktree creation, the workspace reader, the lock,
  the pre-merge export) gets the same hooks-off, credential-free shape.

The engine decides *which* effects a phase plans and when it drives them; it
never issues a write around them. Provider adapters and the executor know
nothing about effects.

### Prompt system

Large prompts belong in template files under `src/autoforge/prompts/`, not in long Python string literals.

Missing required template variables must fail explicitly.

---

The prompt renderer (`src/autoforge/prompts/__init__.py`) enforces the
last rule: rendering fails when a required variable is missing and when a
placeholder is left unrendered. The agent-facing result contract those
templates must produce is in
[control-result-protocol.md](control-result-protocol.md).

The trust boundary is part of every prompt: `prompts/common.md` declares
GitHub issues/PRs/comments, source, tests, and logs untrusted data, and
controller instructions and the repository's `AGENTS.md`/`CLAUDE.md` outrank
them. It also tells the agent it holds no GitHub credential and names the
read-context directory (`{{READ_CONTEXT_DIR}}`) that replaces `gh` reads;
the phase templates name the files in it. The three REMOTE write templates
(`analyze_execute.md`, `fix.md`, `replan_reexecute.md`) require
`{{WORKTREE_STATE}}`, which the engine always supplies: empty for a clean
agent worktree, otherwise `prompts/worktree_state.md` rendered with the
worktree's branch, `HEAD` and a bounded list of status codes and paths
(#187). The instruction on what to do with those changes is in that
template; the engine supplies only the facts. LOCAL mode uses `local_common.md` and the `local_*` phase templates, which
tell the agent the run has no Issue, PR or remote branch and not to use
GitHub.
