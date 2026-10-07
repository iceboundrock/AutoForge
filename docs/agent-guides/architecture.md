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
(#192): the `result` record's text becomes `stdout`, and an error result, a
malformed line or a missing result becomes `provider_failure`. No CLI flag
and no provider wire-protocol name appears outside the provider layer.

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

A provider can report that a run failed inside its protocol even though the
process exited 0. It does so through a provider-neutral optional
`AgentExecutionResult.provider_failure` (ADR 0003 §2.6), which the engine
treats like a non-zero exit: `ExecutionError`, state unchanged, stdout and
stderr recorded. The engine checks `timed_out` first, then
`provider_failure`, then the exit code, then the parser. A flat, bounded,
redacted `provider_summary` (scalars only) goes to `execution.json`. The engine names no provider in that
check. The adapter returns the text the parser reads as `stdout`, which for
Pi is the final assistant message, never raw protocol records.

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
The engine also owns *where* an agent runs: in REMOTE mode a per-issue
`git worktree` it creates under `<git common dir>/autoforge/worktrees/<n>`
(or `execution.worktree_dir`) at the first launch and never deletes, and it
reads HEAD and branch of its own checkout before and after each invocation,
entering `BLOCKED` on a change.

Nothing an agent starts outlives its invocation
([ADR 0002](../adr/0002-executor-nothing-outlives-the-invocation.md)). The
child is started in a new session (`start_new_session`), so it leads a
process group of its own that the executor can reach and kill. An
invocation is complete when the child has exited, both pipes reached EOF
*and* no process is left in the child's process group. The child's exit is
bounded by the timeout; past it the whole group is killed and the result is
a timeout. Once the child has exited on its own, the other two conditions
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
`send_line`, `read_line`, `close_stdin` and `finish`. It reuses the
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
- one absolute deadline bounds every `send_line`, `read_line` and `finish`.
  Past it the group is killed exactly as on `execute()`'s timeout, and the
  result is `timed_out` with `exit_code = -1`. A deadline that is not
  finite, not positive or above `MAX_DEADLINE_SECONDS` (a week, within what
  every wait primitive can take) is refused with `ExecutionError` before
  anything is spawned;
- the `with` tears down on every exit, an exception or `KeyboardInterrupt`
  included: stdin is closed (an orderly-shutdown request), the child gets a
  bounded wait to exit (the deadline from `finish()`, at most the exit grace
  on an exception), then the exit grace and empty-group check, then the
  group kill (SIGTERM first, then SIGKILL). `DuplexResult` reports
  `descendants_killed`, `group_survived_kill` and `capture_abandoned`
  exactly as `ExecutionResult` does for the same leftovers, and the
  invocation takes at most the deadline plus the exit grace plus two kill
  graces.

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
`subprocess` module, so `execute()` returns within the timeout plus the exit
grace plus two kill grace periods whatever the child left behind. What the
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

### GitHub client

Centralize GitHub integration behind a `GitHubClient` or equivalent abstraction.

Prefer typed return values rather than passing raw `gh --json` dictionaries throughout business logic.

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
- `effect_ops.py` owns one operation per Wave 1 effect kind (the identity
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
them. LOCAL mode uses `local_common.md` and the `local_*` phase templates, which
tell the agent the run has no Issue, PR or remote branch and not to use
GitHub.
