# State, persistence, locking, recovery, and error classification

Read this before changing the persisted state file or its schema
(`src/autoforge/state.py`, `src/autoforge/run_contract.py`), atomic writes and
the runtime filesystem boundary (`src/autoforge/safefs.py`), `resume` or any
other crash-recovery path, merge counting, the repository lock
(`src/autoforge/locking.py`), the error taxonomy (`src/autoforge/errors.py`),
or which failures are retried and which block.

---

## Persistent state

Runtime state belongs under `.autoforge/` and must not be committed.

Expected state includes data such as:

- protocol version
- controller version
- prompt version
- run ID
- repository
- EPIC URL
- current issue URL
- current PR URL
- phase
- review round
- reviewed HEAD SHA
- current HEAD SHA
- latest review comment URL
- latest review result
- merged-since-EPIC-update count
- already-counted merged PRs
- attempt number
- timestamps

State writes must be atomic.

Preferred pattern:

```text
write temp file
flush/fsync when appropriate
atomic replace
```

A corrupt existing state file must fail loudly. Never silently replace corrupted state with a fresh run.

The `protocol_version` is the state file's schema, the nested replan journal included, and it is what tells an old-controller file from a corrupt one: a field a controller of *this* protocol always writes is corruption when absent, so a schema change that tightens what a stage requires must bump the protocol rather than let the old shape be diagnosed as corruption. Old-controller compatibility is decided at the state boundary by the version label, never by shape, and it is explicit and tested per stage: protocol 1 → 2 added the review decision's PR and issue binding to the replan journal, so a protocol-1 file with no replan in flight (an empty or `REJECTED` journal) is loaded and relabelled, while one with an in-flight journal is refused with its stage, PRs and the fate of the source PR named, and never migrated by filling the decision from the run's current PR and issue (that is the rebinding the fields exist to forbid), and never handed to the journal loader to be called corrupt. `run --force` moves such a file aside as it does any unreadable one; it is never overwritten in place.

---

## Idempotency and crash recovery

Assume the process may crash after an external side effect but before local state is persisted.

Examples:

- a PR may already have been created
- a review comment may already exist
- a fix may already have been pushed
- a PR may already have merged

Resume logic must inspect actual Git/GitHub state before repeating destructive or duplicative actions.

If recovery cannot determine the safe state with sufficient confidence, enter `BLOCKED` rather than guessing.

Merge counters must be idempotent. Track which PRs have already contributed to the counter so a resumed workflow cannot count the same merge twice.

---

## Locking

Only one AutoForge controller instance may operate on a repository at a time.

Use a repository-scoped lock backed by reliable OS-level locking, keyed by the repository identity (the git common dir, e.g. `<repo>/.git/autoforge/controller.lock`) rather than by a caller-selectable path such as the state directory: different `--state-dir` values, invocation directories or linked worktrees of one checkout must all contend for the same lock.

A second controller must fail clearly rather than run concurrently.

---

## Runtime artifacts

Runtime artifacts must remain untracked.

At minimum `.gitignore` should exclude appropriate entries such as:

```text
.autoforge/
__pycache__/
*.pyc
*.egg-info/
.pytest_cache/
.ruff_cache/
.mypy_cache/
.venv/
```

Do not commit generated controller state, logs, lock files, credentials, or local virtual environments.

---

## Error handling

Prefer meaningful typed errors over generic exceptions.

Useful conceptual categories include:

```text
ConfigurationError
StateError
StateTransitionError
LockError
ExecutionError
ExecutionTimeoutError
ControlResultError
ControlResultValidationError
GitHubError
VerificationError
```

Do not retry every failure blindly.

Potentially transient failures may be retried with bounded retry/backoff, such as:

- temporary process failure
- temporary GitHub/network error
- malformed CONTROL_RESULT correction attempt

Do not blindly retry semantic or safety failures such as:

- real test failures
- repository mismatch
- authentication failure
- permission denial
- merge conflict
- agent-reported real blocker
- controller invariant violation

Those should become a clear failure or `BLOCKED` state.

---

## Related contracts

- The replan journal nested in the state file has stage-specific completeness
  rules and a rejection-monotonicity rule of its own:
  [replan-transaction.md](replan-transaction.md).
- What must be re-verified on GitHub before repeating a side effect:
  [github-safety.md](github-safety.md).
