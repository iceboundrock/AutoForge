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

### Provider adapters

Provider-specific code translates an abstract agent request into a real CLI invocation.

Examples:

```text
AgentProvider
  -> ClaudeCodeProvider
  -> OpenCodeProvider
```

Provider adapters own:

- CLI argv construction
- model identifier mapping
- reasoning / effort mapping
- non-interactive execution details
- provider-specific compatibility handling

### Executor

The process executor owns:

- subprocess lifecycle
- timeout
- stdout/stderr capture
- exit status
- process termination
- timestamps

It should remain independent of workflow semantics.

The timeout bounds the whole invocation, not only the child's exit: an
invocation is complete when the child has exited *and* both pipes reached
EOF. A descendant that inherited the pipes (a server the agent left running)
keeps them open past the child's exit, so the wait for EOF runs under the
same deadline, and at the deadline the child's whole process group is
killed and the result is a timeout, exactly as when the child itself
overruns. The kill is complete only when no process is left in the group,
not merely when the child is reaped and its pipes closed: a descendant that
closed its inherited stdio and ignores SIGTERM is caught by that check and
escalated to SIGKILL. Every wait in that kill is bounded by the kill grace
period, and nothing is waited for past the SIGKILL grace: not a writer the
group kill cannot reach (a descendant that also called `setsid`), and not a
member that survives SIGKILL itself (uninterruptible in the kernel), the
direct child included. The capture is then abandoned, an unreaped child is
left to the `subprocess` module, and the result is the timeout, so
`execute()` returns within the timeout plus two grace periods whatever the
child left behind.

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

### Prompt system

Large prompts belong in template files under `src/autoforge/prompts/`, not in long Python string literals.

Missing required template variables must fail explicitly.

---

The prompt renderer (`src/autoforge/prompts/__init__.py`) enforces the
last rule: rendering fails when a required variable is missing and when a
placeholder is left unrendered. The agent-facing result contract those
templates must produce is in
[control-result-protocol.md](control-result-protocol.md).
