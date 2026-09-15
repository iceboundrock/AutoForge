# Architectural boundaries

Read this before adding or moving responsibilities between the engine, a
provider adapter, the executor, the GitHub client, or the prompt system —
including any change that would make one of them know something that belongs
to another (a CLI flag in the engine, workflow state in the executor, a raw
`gh --json` dictionary crossing into business logic, a long prompt as a
Python string literal).

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

Provider-specific code is responsible for translating an abstract agent request into a real CLI invocation.

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

### GitHub client

GitHub integration should be centralized behind a `GitHubClient` or equivalent abstraction.

Prefer typed return values rather than passing raw `gh --json` dictionaries throughout business logic.

### Prompt system

Large prompts belong in prompt template files under `src/autoforge/prompts/` rather than embedded as long Python string literals.

Missing required template variables must fail explicitly.

---

The prompt renderer (`src/autoforge/prompts/__init__.py`) enforces the
last rule: rendering fails when a required variable is missing and when a
placeholder is left unrendered. The agent-facing result contract those
templates must produce is in
[control-result-protocol.md](control-result-protocol.md).
