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
- reviewed PR URL, reviewed HEAD SHA, reviewed base branch and reviewed
  merge base SHA (the revision the last review round decided on, the
  merge base being the commit its diff was computed from; the merge gate
  requires all four)
- current HEAD SHA, current base branch and current merge base SHA (read
  from GitHub before the review is launched; the merge base is cleared
  wherever the HEAD or base is rebound outside `REVIEW`, and the next
  `REVIEW` entry reads it again)
- latest review comment URL
- latest review result
- merged-since-EPIC-update count (merges not yet reflected in the EPIC's
  roadmap section; drives `workflow.epic_update_every` and is reset only
  after the controller's roadmap write is read back)
- already-counted merged PRs (in merge order; the last
  merged-since-EPIC-update entries are the batch handed to the agent)
- attempt number
- block reason, and the operator unblock history (one entry per applied
  `autoforge unblock`: timestamp, reason, the block reason it cleared, the
  phase re-entered and the controller's detail; refused unblocks are only in
  the run log; the list is kept across issue switches, redacted before it is
  written, and validated on load like every other list field)
- timestamps

State writes must be atomic.

Preferred pattern:

```text
write temp file
flush/fsync when appropriate
atomic replace
```

A corrupt existing state file must fail loudly. Never silently replace corrupted state with a fresh run.

The `protocol_version` is the state file's schema, the nested replan journal included, and it is what tells an old-controller file from a corrupt one: a field a controller of *this* protocol always writes is corruption when absent, so a schema change that tightens what a stage requires must bump the protocol rather than let the old shape be diagnosed as corruption. Old-controller compatibility is decided at the state boundary by the version label, never by shape, and it is explicit and tested per stage: protocol 1 → 2 added the review decision's PR and issue binding to the replan journal, so a protocol-1 file with no replan in flight (an empty or `REJECTED` journal) is loaded and relabelled, while one with an in-flight journal is refused with its stage, PRs and the fate of the source PR named, and never migrated by filling the decision from the run's current PR and issue (that is the rebinding the fields exist to forbid), and never handed to the journal loader to be called corrupt. Protocol 2 → 3 added the reviewed PR and base branch to the review binding (`reviewed_pr_url`, `reviewed_base_ref`, `current_base_ref`) and the reviewed base to the replan journal (`decision_base_ref` at `PENDING`, `source_base_ref` at `PREPARED`): a protocol-2 file with a replan in flight is refused exactly as a protocol-1 one is, naming the base as the binding it lacks instead of the PR and issue, and never migrated by filling it from the source PR's current base; a protocol-2 (or protocol-1) file with no replan in flight and in any phase other than `READY_FOR_MERGE` / `MERGE` is loaded and relabelled, because the next review writes the binding, while one parked in those phases is refused with the PR and HEAD named, and never migrated by filling the binding from the run's current PR (the same rebinding rule). Protocol 3 → 4 added the reviewed merge base to the review binding (`reviewed_merge_base_sha`, `current_merge_base_sha`; both validated on load as a full lower-case SHA or empty, matched in full so a trailing newline is not a SHA) and to the replan journal (`decision_merge_base_sha` at `PENDING`, `source_merge_base_sha` at `PREPARED`), so both rules apply to a protocol-3 file exactly as they apply to a protocol-2 one, for the merge base: one with a replan in flight is refused with its stage, PRs and the fate of the source PR named, naming the merge base as the binding it lacks, and never migrated by reading the merge base GitHub reports now (the base may have been rewritten since the review, which is exactly what the field exists to detect), and never handed to the journal loader to be called corrupt; one with no replan in flight and parked in `READY_FOR_MERGE` / `MERGE` is refused, naming the merge base as the review binding it lacks, and never migrated by reading the merge base after the fact for the same reason; in any other phase with no replan in flight it is loaded and relabelled, because the next review writes the binding. `run --force` moves such a file aside as it does any unreadable one; it is never overwritten in place.

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

`BLOCKED` is left only by `autoforge unblock`, which re-runs the same
recovery inspection against live GitHub and re-enters the one phase it
supports through `validate_transition`, or refuses and leaves the state file
untouched when the safe state still cannot be determined (`workflow.md`,
"Leaving BLOCKED"). The operator's reason is recorded; it is never the
evidence the decision rests on.

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

The controller also keeps runtime artifacts inside the repository's git
common dir, which git never tracks: the controller lock
(`.git/autoforge/controller.lock`), a LOCAL run's default state directory
(`.git/autoforge/state`) and, in REMOTE mode, one detached agent worktree
per issue (`.git/autoforge/worktrees/<issue-number>`, unless
`execution.worktree_dir` points elsewhere). A worktree is created at the
first agent launch for its issue, reused by every later phase of that issue
across `resume`, and never deleted, moved or pruned by the controller; the
operator removes it with `git worktree remove` when the issue is done. A
path that exists there but is not a worktree of this repository is refused,
never adopted: a symbolic link at the path is refused on the entry itself,
whatever it points to (a link to the operator's checkout or to another
worktree would otherwise answer git as that tree), a directory is
reused only when `git worktree list` registers it as a worktree root, and a
path reached through a symbolic link above it (`.git/autoforge/worktrees`
linked into the working tree, say) is refused before anything is created,
so a worktree only ever appears at the literal derived path.

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
