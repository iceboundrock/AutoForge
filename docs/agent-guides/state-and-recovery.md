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
- current PR URL and its branch
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
- block reason (redacted and then bounded to `state.MAX_BLOCK_REASON_CHARS`
  (8000 characters) by every engine writer, through `state.bound_block_reason`: `_block` and
  the two agent-message writers; a longer reason keeps its head, an
  omission marker naming the count dropped, and its last
  `BLOCK_REASON_TAIL_CHARS` (1000), so what happened and what the operator must do
  both survive. It is operator diagnostics, not work a later phase acts on,
  which is why it may be clipped where a finding or resolution is rejected
  instead; what is clipped is still whole in `last_fix_resolutions` (a
  LOCAL fix round's unresolved rationales) or the run log (an agent's
  `message`). The bound is a writer's rule, not a loader's: a longer value in a
  file, written by an older controller, loads and is not rewritten), and the
  operator unblock history (one entry per applied
  `autoforge unblock`: timestamp, reason, the block reason it cleared, the
  phase re-entered and the controller's detail; refused unblocks are only in
  the run log; the list is kept across issue switches, redacted before it is
  written, and validated on load like every other list field)
- timestamps

Besides these, the state records the review loop: `review_round`,
`last_review_needs_fix`, `open_findings`, `prior_findings` (a stale round's
findings, carried to the next review to re-check), `last_fix_resolutions`,
`review_history` (one entry per completed review round of the current PR:
round, reviewed SHA, result, finding count, fingerprint of the requested
resolutions) and `step_count` (cumulative for the run, never reset). A
REMOTE run also records its replan history: `execution_attempt` (initial
implementation is 1),
`escalation_count` (completed replans), `superseded_prs` (each entry naming
the transaction that superseded it), and the durable `replan_transaction`
record: the controller's intent journal, which `resume` replays rather than
re-deriving a decision from GitHub facts
([replan-transaction.md](replan-transaction.md)).

A REMOTE run also records the effect state of the current phase entry (ADR
0004, `src/autoforge/effects.py`; [github-safety.md](github-safety.md),
"Controller-owned effects"):

- `effect_records`: one record per controller write the entry plans, in
  plan order. Each holds the kind (the closed Wave 1 set: push,
  implementation PR, adopt PR, review comment, follow-up issue, follow-up
  append, replacement PR, progress comment), its owner (run, phase, issue,
  PR, transaction), the identity, the exact target, the precondition, the
  payload exactly as it is sent, the attempt count (at most
  `MAX_EFFECT_ATTEMPTS`, 2), the stage (`intended`, `attempted`, `observed`,
  `conflict`), the observed object and a conflict reason. Only redacted,
  bounded values are stored: a payload that is not redaction-invariant is
  refused, never redacted into something else, and no credential, header or
  environment value is ever stored. At most `MAX_EFFECTS_PER_PLAN` records,
  and every payload string of the plan plus the completion context at its
  stored bound fit in `MAX_EFFECT_STATE_CHARS`.
- `entry_observation`: what the entry's identity reads saw before the
  phase's first launch (D4.4), so that an object found later is explained by
  a record, by this observation, or not at all (D9.1).
- `completion_context`: what the phase needs to finish without relaunching
  its agent once its result is accepted (D4.6). For `UPDATE_EPIC` this is
  the roadmap section, the validated selection, the digests of the EPIC body
  outside the markers, and which input a persisted rejection voided.
- `launch_label` (D13.3): whether the current entry's launches ran under the
  agent-publishes or the controller-publishes contract of its phase.

Each record changes stage in a save of its own, and the `attempted` save
precedes the write (D4.1). The effect state belongs to one phase entry, and
every save settles it (`settle_phase_effects`, D2.4): a save in another
phase drops it, except `BLOCKED` and `FAILED`, which keep it for the
operator and for `unblock` back into the phase (which reconciles a
`conflict` record again, with its attempt count kept). The fields are
validated on load against the run's issue, PR and phase, and a corrupt or
incomplete value fails loudly, never replaced by a default. Every protocol-7
save writes all four fields, so a protocol-7 file missing one is refused:
an absent `effect_records` read as its empty default would be an empty plan,
and the phase would complete from its context without the write it had
planned. An `UPDATE_EPIC` completion context is checked against the plan and
the entry observation of its own phase for the same reason: it loads with
the one `progress_comment` record of its own (issue, PR) marker, or with no
record only when the entry observation records that marker's comment as
adopted (the D13.7 legacy re-entry). Neither, both, or no entry observation
is refused, and completion itself refuses to splice the roadmap section or
switch issues without a posted or adopted comment. Text the recovery path
publishes with no agent result in between is validated again under the
parser's rules for its field, not only for its bounds and redaction
invariance: the `UPDATE_EPIC` roadmap section in the
completion context (`validate_roadmap_section`) and the progress text of a
progress-comment record (`validate_progress_text`). A stored value the
result path would have refused is a `StateError`, never rewritten. LOCAL
state carrying any of them is refused as corrupt: a LOCAL run performs no
external effect.

State keeps compact finding summaries and review comment URLs, rather than
copying unbounded PR discussion bodies. Those summaries are bounded
(`loop_guard.MAX_PERSISTED_FINDINGS_PER_ROUND`, 100 findings per round,
above the 50 findings the parser accepts; `MAX_REQUIRED_RESOLUTION_CHARS`,
6000 characters per required resolution, which is the parser's
2000-character bound times the most redaction can lengthen a text). A round
that hits either bound is marked `evidence_truncated`, and because
superseding a PR deletes the controller's only record of its findings, such
a round makes the replan policy block for a human instead. Because the
parser refuses an oversized review before it is recorded, only state written
by an older controller or edited by hand can carry that mark. Relevant
controller verification failures are retained as a bounded per-issue list
(`verification_failures`) and supplied to the replan prompt; all PR comment
text remains GitHub audit data rather than state payload.

State writes must be atomic.

Preferred pattern:

```text
write temp file
flush/fsync when appropriate
atomic replace
```

A corrupt existing state file must fail loudly. Never silently replace corrupted state with a fresh run.

As implemented: temp file + fsync + `os.replace`. `run`, `resume`, `step`
and `status` all exit 2 on an unreadable or foreign-protocol `state.json`
(bad JSON, unknown phase, invalid UTF-8, dangling symlink, or a non-regular
entry such as a FIFO, socket, device or directory; the entry is inspected
and opened non-blocking, so a FIFO never hangs the command).

`run --force` moves the entry aside as `state.json.corrupt-<timestamp>`
instead of deleting it (a symlink is archived as a link; its target is never
touched; a directory cannot be archived and is refused). The archive name is
reserved with `link(2)`, which never replaces an existing archive; on a
filesystem without hard links (vfat/exFAT, some FUSE, SMB and overlay
mounts) the entry is copied into an exclusively created name instead, with
the source's permission bits regardless of the umask where the filesystem
holds any (where it holds none and refuses `chmod` as it refuses `link`, the
copy is made anyway and is at most as permissive as the source, never more;
a FIFO, socket or device cannot be copied and is refused there); a copy that
fails at any point after its name exists removes that name again, so a
failed archive never sits beside an untouched original. The original is
unlinked only after the archive exists, so a crash in between leaves both,
and the next `run --force` archives the leftover again under a new name: a
duplicate copy, never a lost one. `run` inspects, decides, quarantines,
writes the first state and executes it under one continuous controller
lock, so a concurrent controller can never be quarantined or overwritten on
a stale verdict, nor slip in between the first save and the engine loop.

The `protocol_version` is the state file's schema, the nested replan journal included, and it is what tells an old-controller file from a corrupt one: a field a controller of *this* protocol always writes is corruption when absent, so a schema change that tightens what a stage requires must bump the protocol rather than let the old shape be diagnosed as corruption. Old-controller compatibility is decided at the state boundary by the version label, never by shape, and it is explicit and tested per stage: protocol 1 → 2 added the review decision's PR and issue binding to the replan journal, so a protocol-1 file with no replan in flight (an empty or `REJECTED` journal) is loaded and relabelled, while one with an in-flight journal is refused with its stage, PRs and the fate of the source PR named, and never migrated by filling the decision from the run's current PR and issue (that is the rebinding the fields exist to forbid), and never handed to the journal loader to be called corrupt. Protocol 2 → 3 added the reviewed PR and base branch to the review binding (`reviewed_pr_url`, `reviewed_base_ref`, `current_base_ref`) and the reviewed base to the replan journal (`decision_base_ref` at `PENDING`, `source_base_ref` at `PREPARED`): a protocol-2 file with a replan in flight is refused exactly as a protocol-1 one is, naming the base as the binding it lacks instead of the PR and issue, and never migrated by filling it from the source PR's current base; a protocol-2 (or protocol-1) file with no replan in flight and in any phase other than `READY_FOR_MERGE` / `MERGE` is loaded and relabelled, because the next review writes the binding, while one parked in those phases is refused with the PR and HEAD named, and never migrated by filling the binding from the run's current PR (the same rebinding rule). Protocol 3 → 4 added the reviewed merge base to the review binding (`reviewed_merge_base_sha`, `current_merge_base_sha`; both validated on load as a full lower-case SHA or empty, matched in full so a trailing newline is not a SHA) and to the replan journal (`decision_merge_base_sha` at `PENDING`, `source_merge_base_sha` at `PREPARED`), so both rules apply to a protocol-3 file exactly as they apply to a protocol-2 one, for the merge base: one with a replan in flight is refused with its stage, PRs and the fate of the source PR named, naming the merge base as the binding it lacks, and never migrated by reading the merge base GitHub reports now (the base may have been rewritten since the review, which is exactly what the field exists to detect), and never handed to the journal loader to be called corrupt; one with no replan in flight and parked in `READY_FOR_MERGE` / `MERGE` is refused, naming the merge base as the review binding it lacks, and never migrated by reading the merge base after the fact for the same reason; in any other phase with no replan in flight it is loaded and relabelled, because the next review writes the binding. Protocol 4 → 5 added the close watermark to the replan journal (`source_closed_event_count` present and `close_attempts` non-zero at `SUPERSEDE_INTENT`, #69) and changed nothing about the review binding, so the rule is scoped to the stages whose record changed (`replan_txn._LEGACY_GAP_STAGES`): a protocol-4 file whose journal has reached `SUPERSEDE_INTENT`, `COMPENSATING` or `SUPERSEDED` is refused in every phase, naming the closed-event count and attempt count as the binding it lacks, and never migrated by reading the count GitHub reports now (that would record the very close the watermark exists to detect as if it predated the intent, and license the retry the field was added to forbid in that case); one whose journal is empty, `REJECTED`, or at `PENDING`, `PREPARED` or `VERIFIED` is byte-for-byte what protocol 5 writes there and is loaded and relabelled in every phase, the merge phases included. `run --force` moves such a file aside as it does any unreadable one; it is never overwritten in place.

Protocol 5 → 7 (#160) added the effect state above (`effect_records`, `entry_observation`, `completion_context`, `launch_label`). Protocol 6 is skipped: #23 reserves it for #126, and the number this bump takes must be settled on #23 before it merges. State persisted under protocol 5, or under any older protocol that its own rules load, carries no effect state and loads as none; a legacy file that does carry one of these fields is a hand edit or a foreign file and is refused. A legacy REMOTE file with `attempt >= 1` gets the launch label `agent_publishes` for a phase whose agent published under the previous contract, so that the first entry of this version knows an agent may already have published (D13.3). `UPDATE_EPIC` is the one such phase whose publication this version performs itself: its one-shot legacy re-entry adopts the progress comment that agent posted, with no record, and asks for the selection only (D13.7; [github-safety.md](github-safety.md), "Before UPDATE_EPIC"). LOCAL state, whatever its protocol, carries no effect state.

---

## Idempotency and crash recovery

Assume the process may crash after an external side effect but before local state is persisted.

Examples:

- a PR may already have been created
- a review comment may already exist
- a fix may already have been pushed
- a PR may already have merged

Resume logic must inspect actual Git/GitHub state before repeating destructive or duplicative actions.

A controller effect (ADR 0004) is resumed from its record and never re-sent blind. Every crash window has one answer: a crash before the intent is saved means nothing happened and the step re-runs; an `intended` or `attempted` record is reconciled by its identity before anything is sent, so a write that landed before the save is adopted as `observed` (no second write) and one that did not is issued again within the attempt bound; an object that a human created or changed between the intent and the write, a conflicting identity, or two marker-bearing objects make the record `conflict` (`BLOCKED`, naming the object); an exhausted bound is `BLOCKED` naming the record and the manual step; and a transient GitHub failure during reconciliation leaves the state unchanged for `resume`. A phase whose effect landed finishes from its completion context without relaunching its agent. The run log's `events.jsonl` is never read by any recovery path.

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

As implemented (`flock(2)`, `src/autoforge/locking.py`):

- The lock is `<git common dir>/autoforge/controller.lock`, resolved with
  `git rev-parse --git-common-dir` from the controller's working directory,
  so two controllers cannot escape each other by passing different
  `--state-dir` values, by starting from different subdirectories with the
  default relative `.autoforge`, or by using a linked `git worktree` of the
  same checkout. A second instance exits with `LockError`. Two independent
  *clones* of one GitHub repository are two repositories to this lock and
  are not coordinated.
- A working directory outside a git repository has no lock to take and is
  refused (exit 2, nothing written), so `run`, `step` and `resume` require
  one (a REMOTE dry-run does not).
- `run`, `step` and `resume` take the lock **before** `state.json` is read
  or created and keep it until their last step has been persisted, so a
  state snapshot loaded before the lock is never executed and no second
  controller can take over the repository mid-command. Dry-run takes no lock
  and writes nothing. A REMOTE `run --dry-run` runs no `git`. `step`,
  `resume`, `unblock` and `status` without `--state-dir`, dry runs
  included, first run two read-only `git rev-parse` calls (`--git-dir`,
  `--git-common-dir`) to find which run is meant (`cli._bind_existing_run`:
  a remote one under `state_dir` or a local one in the git directory);
  `--state-dir` skips that lookup. A LOCAL dry run needs a git repository
  and reads it: `local run --dry-run` reads HEAD, branch and the workspace
  walk to build its plan, and a dry run of an existing local run reads the
  repository to check its run contract and current fingerprint.
- Below the git common dir no path component is followed through a symlink:
  the `autoforge` directory is created with `mkdir` and opened relative to
  the git directory's descriptor with `O_DIRECTORY | O_NOFOLLOW`, so a
  symlink or a file in its place is refused (exit 2) and its target is never
  touched; `controller.lock` is then opened relative to that validated
  directory descriptor. The lock entry itself must be a regular file with a
  single name: `controller.lock` is opened with `O_NOFOLLOW` and
  `fstat`-checked, so a symlink, FIFO, socket, device or directory in its
  place, or a hard link that shares its inode with another file, is refused
  with `LockError` (exit 2) before anything is written: a tampered path or
  entry can neither redirect the PID write to another file nor produce a
  traceback.
- These checks cover entries that are damaged or tampered *at rest*; a
  writer with write access to the git directory who races the
  check-then-write window, or swaps the `autoforge` directory for another
  directory between two controller invocations, has the controller's own
  privileges and is outside the trust boundary (the git directory is assumed
  to be writable only by the operating user).

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

The state directory defaults to `.autoforge/` in the working directory for
a REMOTE run (overridable via `--state-dir` or `state_dir` in config) and to
`<git dir>/autoforge/state` for a LOCAL run, outside the tree it
fingerprints, since its own writes would otherwise keep invalidating its own
review. Either way it must be on a filesystem with hard links:
`SafeRoot.create_exclusive` publishes a complete, fsynced temporary by
`link(2)` and deliberately has no fallback that writes the final name
directly (ADR 0001, known limitation 8), so on vfat/exFAT and some FUSE,
SMB/CIFS and overlay mounts it raises `HardLinksUnavailable`, and the
run-log probe (before every launch) and `doctor` refuse with the
requirement named (#120). Same layout either way:

```text
<state dir>/
    state.json          # persisted run state (atomic writes)
    state.json.corrupt-<timestamp>   # unreadable state moved aside by 'run --force'
    logs/<run-id>/
        events.jsonl                       # one line per agent invocation, append-only
        <seq>-<phase>-<attempt>/
            request.json                   # profile, model, effort, command, timeout
            prompt.md                      # rendered prompt (redacted)
            execution.json                 # exit code, timing, timed_out, truncation, leftovers, error
            stdout.log / stderr.log        # redacted; head + marker + tail past the capture bound
            control-result.json            # parsed CONTROL_RESULT (when valid)
```

The controller also keeps runtime artifacts inside the repository's git
directories, which git never tracks: in the git common dir the controller
lock (`.git/autoforge/controller.lock`) and, in REMOTE mode, one detached
agent worktree per issue (`.git/autoforge/worktrees/<issue-number>`, unless
`execution.worktree_dir` points elsewhere; a configured location must lie
outside the checkout's working tree). A worktree is created detached at the
checkout's HEAD at the first agent launch for its issue, reused as the agent
left it by every later phase of that issue
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
