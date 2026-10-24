# Testing expectations

Read this before adding or changing tests, or before changing behaviour in an
area listed under "High-priority coverage" (each such change must come with
the corresponding test). The test-scope conventions (fakes, fixtures and what
a test may never do) are in `tests/AGENTS.md`; this document is the coverage
contract.

---

## Testing requirements

Every behavioral change should include or update automated tests.

Do not call real Claude Code/OpenCode/Pi/GitHub write APIs from unit tests.
The one test that calls a real agent and model, the live Pi smoke test
(#135), is opt-in and deselected by default (below).

Use mocks, fakes, fixtures, and scripted providers.

High-priority coverage includes:

### State

- serialization/deserialization
- atomic persistence
- corrupt-state handling
- idempotent merge counting
- effect state (ADR 0004): records round-trip in every stage; a corrupt or
  incomplete record fails loudly; LOCAL state carrying effect state is
  refused; a previous-protocol file loads with no effect state, and one
  that carries effect fields is refused
- acceptance-action state (#211, `tests/test_state.py`): it round-trips
  under protocol 8; a protocol-7 file loads migrated and is relabelled; a
  protocol-7 or older file carrying outcomes, an `acceptance_comment_url`,
  an unblock `action`, a K9 record or an `acceptance_source` is refused,
  unchanged; each malformed outcome and recorded URL is refused; a LOCAL
  run carries none; a new issue resets the outcomes
- operator-action state (#243, `tests/test_state.py`): every outcome
  round-trips under protocol 11; a protocol-10 file, a `FIX` plan included,
  loads in every phase migrated and is relabelled; an older file carrying
  `operator_comment_outcomes`, a K10 record, `operator_targets`,
  `operator_sources` or `operator_evidence` is refused, unchanged; a
  protocol-11 file missing the outcomes is refused; each malformed outcome
  is refused, and a `verified` one is unique per action and comment; the
  outcome bound fits one round and drops the oldest entries, never the
  round being fixed; declines of a round count K9's and K10's together; a
  LOCAL run carries none; the outcomes belong to the issue they were
  recorded for
- recorded configuration (#219, `tests/test_config_fingerprint.py`,
  `tests/test_state.py`, `tests/test_cli.py`): every fingerprinted profile
  key and the worktree root is compared and named, and no other key is; a
  REMOTE run records it at creation; `step`, `resume` and `unblock` refuse
  a difference (a `resume` without the run's `--config`, a changed model, a
  changed worktree root) before any launch, GitHub call or write, and the
  command they print to restore the recorded configuration names the state
  directory that found the run by its absolute path, also when it came from
  the configuration's `state_dir`, and continues that run;
  `--accept-config-change` logs the change before recording it; a dry run
  writes nothing; a protocol-8 file loads, resumes and records one; a
  protocol-8 or older file carrying the field, or a protocol-9 file missing
  it, is refused; a LOCAL run carries none; a configuration the record could
  not hold (a model or effort over 512 characters, an `update_epic` provider
  or limit out of bounds, a path over 4096 characters) is refused by `run`,
  its dry run and `doctor` before any state is written, and by an upgrade or
  an accepted change before anything is recorded or logged, while a model
  at the bound is recorded and the run loads again
- validation evidence (#172, `tests/test_evidence.py`,
  `tests/test_validation_evidence.py`, `tests/test_state.py`): the
  definition, set and policy digests are stable across processes and change
  with each input (argv, index, mode, template, allow-list, timeout,
  `contain_orphans`, method); the policy template is the concrete request
  with every path a role, so two exports give one `policy_digest`; every
  record field loads at its bound and is refused one past it, and a
  malformed SHA, digest, timestamp or enumeration, a hand-edited record, a
  mixed candidate, another mode's, repository's or run's record, a binding
  or certificate naming a missing, edited, `INTERRUPTED` or misplaced
  record, a counter naming no `FAIL`, and a watermark out of range are
  refused; exit 0 is `PASS`, any other exit (127 included) `FAIL`, a
  timeout `TIMEOUT`, and a launch failure no record; a tail is redacted
  before it is cut (a secret on the cut), escaped without splitting an
  escape, and bounded with its truncation facts; eviction never removes a
  named record; recovery appends one `INTERRUPTED` record per marker,
  never a `PASS`, is idempotent, ignores records at or below the
  watermark, and blocks on the third; the six fields at their bound stay
  far inside the state file's; REMOTE validation of a real commit binds
  its commit and tree, stops at the first non-`PASS`, gives each
  definition a fresh export (a mutating validator leaves the next one and
  the repository byte-identical), refuses a commit not in the store before
  any launch, and redacts a secret-shaped argv and output in state,
  `status` and the run log; a setup failure of the candidate (finding the
  shared store, launched or timing out, reading its commit, the
  object-format read or a git process the executor could not launch, an
  `OSError` creating the directory) gets one fresh attempt and is then
  inconclusive with nothing written, while a commit or a blob missing
  from the store is refused without one; a sandbox refusal under
  `required` leaves the state byte-identical and is not retried as a
  materialization failure; an interrupt leaves the marker, `status` and
  the dry run report it without writing, and the next step records it;
  LOCAL validation records start and end fingerprints and a contract
  beyond the bounds blocks before the agent; protocol-9 and older files
  load in every phase with none of the six fields, an older file carrying
  one, or a protocol-10 file missing one, is refused; `validation.commands`
  and `local.validation_commands` beyond the bounds, a scope other than
  `candidate-tree` and an unknown key are load errors; `doctor` lists the
  set and runs nothing
- candidate validation gates (#173, `tests/test_validation_gates.py`,
  `tests/test_evidence.py`, the replan rejection in `tests/test_replan.py`):
  a failing `ANALYZE_EXECUTE` or `FIX` candidate pushes, creates and
  reviews nothing, its record and the failure counter are one save, the
  relaunch prompt carries the failure section with fenced tails, the same
  candidate reuses its terminal record and counts again, the bound blocks
  naming the candidate, the definition and the record (a crash between
  failures keeps the count, one at the bound blocks without relaunching),
  and the counter is cleared by the write that leaves the phase; a failing
  replacement takes the `tests_passed: false` rejection, and a crash after
  it replays it; a candidate the check refuses (an import that fails, a
  wrong `head_sha`, decoy worktree metadata, history off the base) leaves no
  record, a private-object candidate is validated after the strict import,
  and a corrected result on a new commit is the one validated; REVIEW reuses
  the producer's records, validates a human's push and presents only the
  head's records, reuses its binding on a relaunch and a correction,
  replaces it for another round, head (a drift after a crash, with a set
  and an empty one), policy or an overridden `FAIL` or `TIMEOUT`, and a
  journal-completed round validates nothing; the evidence section shows
  provenance for every record, tails only for a non-`PASS` one, fenced, an
  argv escaped onto one line, hostile output (a fake section, a marker
  opener, a `CONTROL_RESULT`, a fence breaker) inert, and the empty-set
  wording; an evidence key in a result is ignored, and a clean result over
  a failure is corrected, then at the bound refused with nothing
  published; LOCAL converges (an excluded writer without a rerun, an
  idempotent generator with one confirming run), a non-idempotent validator
  is not verified and then blocks, a failure is final only for the tree it
  ran on, REVIEW needs evidence for the bound fingerprint itself and blocks
  a legacy state without validating, a changed contract is refused before
  anything runs, and no path calls GitHub; an applied unblock alone sets
  the watermark, and the same candidate is validated again; Claude Code,
  OpenCode and Pi behave alike
- review certificates (#174, `tests/test_certificate.py`,
  `tests/test_review_certificate.py`): a clean round's write issues the
  certificate of the exact candidate, records, set and review binding, an
  empty set included, in the save that enters `READY_FOR_MERGE` (a failed
  save leaves neither; #162's journal completes the round and issues it
  from the observed comment) or LOCAL `DONE` (no `gh` call; a fix round
  certifies the new workspace only); a needs-fix round issues none; a
  binding of another round, a non-`PASS` record, another head or PR, a
  comment URL that does not parse, or no binding issues none and blocks; a
  set or policy changed after presentation (timeout, sandbox mode,
  allow-list, the set; with a set and an empty one) issues it from the
  binding and blocks, after which `unblock` decides `REVIEW` under the new
  configuration and `READY_FOR_MERGE` under the restored one;
  `certificate_problem` refuses each field that stops matching (kind,
  digest, round, PR, HEAD, base, merge base, comment, candidate, LOCAL
  fingerprint and contract, set, policy, a record no longer eligible, the
  live revision) and does no I/O; an edited certificate fails the load;
  `unblock` of an open PR at its clean-reviewed revision re-enters
  `READY_FOR_MERGE` only with an applicable certificate and otherwise
  `REVIEW` under the cap, a merged uncounted PR needing none; `status` and
  `status --json` show it, its applicability and the not-a-permission
  note; no prompt, read context or published payload carries it; Claude
  Code, OpenCode and Pi issue the same certificate
- the merge gate's certificate check (#175, `tests/test_merge_certificate.py`,
  with the existing merge tests in `tests/test_engine.py` certifying their
  hand-built clean reviews): in `READY_FOR_MERGE` and in `MERGE`, an
  applicable certificate continues to the existing merge with every live
  gate, `merge.verification_commands` and its cache included; none held, a
  set or policy changed since issuance (timeout, sandbox mode, allow-list,
  the set), and each other requirement failing alone (digest, PR, base,
  merge base, comment, round, a missing or non-`PASS` record) block in one
  write naming it, before the comment is re-read or any command runs, with
  nothing merged or counted; a restored configuration unblocks to
  `READY_FOR_MERGE` and merges with no new round, an unchanged one unblocks
  to a fresh validated `REVIEW`; HEAD drift, failing CI, a failing
  verification command, a deleted comment and `BEHIND` still refuse with a
  certificate held; the dry-run plan and the closed-gate `resume` banner say
  whether it applies, from state, with no GitHub read; the invalidation
  matrix: a push (a new commit, the same tree, a force push) certifies the
  new head alone, a base that only advanced keeps the certificate, a
  rewritten base or a retarget reuses the records and certifies the new
  base, an edited or doubled comment is refused by the #94 re-read, a
  certificate of an earlier round stays as history while a producer
  validates a new set, a replan replacement inherits nothing, and LOCAL
  fingerprint drift certifies nothing, with no GitHub call; the crash
  windows: a saved gate pass reruns the whole gate in `MERGE`, a crash
  after the merge call is counted once with or without a certificate, a
  crash during a verification command reruns it after the certificate
  check, a push while the `REVIEW` entry validates is a stale round that
  certifies nothing and the next round certifies the new head, a restart
  merges, and a legacy state in `REVIEW`,
  `READY_FOR_MERGE`, `MERGE` or `BLOCKED` reaches a validated, certified
  merge; Claude Code, OpenCode and Pi route the gate alike
- per-issue scheduler namespaces (#178, ADR 0008 D5, D17.3,
  `tests/test_run_namespace.py`, `tests/test_cli.py`): the RunKey and the
  layout are pure and independent of `--state-dir`, `config.state_dir` and
  the working directory; two issues bound at once keep their states and run
  logs in disjoint files; `binding.json` round-trips, is written once (a
  second write, for the same or another EPIC, is refused and the first
  kept; a crash before the link leaves none), and each unknown field or
  schema version, missing field, out-of-bound string, non-positive or
  non-integer id, duplicated key and non-JSON file is refused; a wrong
  `repository_id`, issue number, `issue_id` or EPIC is a `NamespaceMismatch`
  naming the field, while a renamed repository keeps its namespace; the
  lookup classes (absent, a bare `run.lock` included; unbound with a claim
  or a state; bound) and the listing create, quarantine and modify nothing;
  a corrupt state is reported for its own namespace only; a link at
  `autoforge`, `scheduler`, `issues`, `<n>` or `state`, a non-decimal entry,
  a regular file and a FIFO named like an issue are reported or refused and
  never followed or written through; the listing is bounded; the archive
  keeps logs, binding, claim and worktree, reports nothing to archive the
  second time, and completes after a crash between link and unlink; the
  engine binding refuses another repository's namespace; legacy standalone
  and LOCAL states are never touched; `run`, `step`, `resume` and
  `local run` refuse a state dir inside the scheduler root, also through a
  relative path, a link or `state_dir`, before the lock (`step` and
  `resume` also before a LOCAL run's state is looked for), while `status`
  reads it, `unblock` acts on it, a dry run writes nothing and a sibling
  directory is not refused
- lock domains (#180, ADR 0008 D9.2, D10–D12, D17.2,
  `tests/test_lock_domains.py`, `tests/test_shared_git.py`,
  `tests/test_ownership.py`, `tests/test_cli.py`): real child processes
  talking over pipes, never a sleep for correctness. L0: the standalone
  release still unlocks; `share` switches to close-only before a spawn; a
  scheduler stand-in that returns, raises or is SIGKILLed leaves L0 and L2
  with its surviving worker until the worker exits; adoption needs the
  fresh-descriptor proof and refuses a descriptor that holds nothing, names
  another object, is not open or is reached through a link. L2: a held lock
  is `IssueBusy` at once, it is handed to exactly one child and released by
  close only, the probe creates nothing and collides only with an
  acquisition; a link, a FIFO, a directory or a hard link at `run.lock` or
  `git.lock` (or a link above them) is refused untouched; SIGKILL releases
  every lock with nothing to clean. L1: a waiter proceeds once the holder
  releases, its bounded wait expires as `GitLockTimeout`, and the bound
  covers every guarded operation; the L1 holder takes no lock, calls no
  GitHub, saves nothing and launches nothing; 1000 randomized acquisitions
  in order from four processes never deadlock and admit one holder at a
  time. Two workers sharing L0, each with its own issue's L2, are inside
  their executor-launched fake providers at once (each says so on a FIFO,
  and neither is released before both did): meanwhile no process holds L1,
  another write takes it without waiting, and a third acquisition of issue
  1's L2 is `IssueBusy`. The scheduler's filesystem refusal (faked `statfs`:
  NFS, CIFS, SMB2, 9p, FUSE, Ceph and macOS names refused; ext4, xfs, btrfs,
  tmpfs and apfs accepted; a lock file on NFS below an ext4 root refused)
  and the real `statfs`. No launch inherits a lock descriptor. With real git
  (D12.5): concurrent worktree creation, fetches and an import beside a
  read-only export hold L1 in disjoint sections and leave `git fsck --full`
  clean; concurrent sandbox directories for different issues also hold L1 in
  disjoint sections; an enforced REMOTE launch creates its sandbox
  directories under L1 and launches its agent without it, and one that
  cannot get L1 in time is refused and proceeds on the next step; agents'
  concurrent commits take no L1;
  the installed git repacks after a fetch unless automatic maintenance is
  off; every git write in the source is a guarded section. The ownership
  guards: `run` (also `--force`, before the quarantine), `step`, `resume`
  and `unblock` refuse an issue whose namespace holds a state (a corrupt
  one included), whose run lock is live or whose namespace cannot be read,
  writing nothing; `unblock` of a namespace's own state dir is exempt; a
  `next_issue_url` the predicate refuses takes the bounded rejection path;
  the reverse read holds every phase but `DONE` and reports an unreadable
  state
- scheduler claims, scheduler state and restart reconciliation (#181, ADR
  0008 D6, D7, D14, D15.1, D17.2, `tests/test_scheduler_state.py`,
  `tests/test_transitions.py`, `tests/test_ownership.py`,
  `tests/test_cli.py`): `claim.json` and `scheduler.json` round-trip, and
  each unknown field (at every level), unknown schema, out-of-bound or
  mistyped value, history over 16, broken generation order, history the
  closing rule forbids, duplicated key, `NaN`, non-UTF-8, oversized or
  linked file is refused untouched, as is a `claims` field in
  `scheduler.json`; only a corrupt `scheduler.json` is quarantined. A claim
  is fsynced before `open_claim` returns, one is open per issue,
  generations are monotonic, a supersede is exactly one write, compares
  with the open claim and replaces only a `resumable` one (a settled or
  uncertain claim is refused, the file untouched), the closing rule is
  decided on the claim on disk, `detail` is redacted and bounded,
  `record_worker` records once, and a writer of generation 3 cannot close,
  record or finalize generation 4.
  Every claim write needs the issue's held L2 and is refused under L1, and
  only over its own namespace's claim file. Every row of #181's crash-window
  table, with real child processes holding L2 (and L0) through `pass_fds`:
  nothing before the claim, a binding alone `fresh`, a claim before the
  spawn `resumable` and kept open (superseded on re-admission or closed
  `abandoned` by `close_unready`), a live worker `owned` and never
  reclassified while it holds L2 (then classified by its state), a crashed
  scheduler's L0 and two L2s with its worker (a new L0 refused; then each
  issue by its state), a settled worker not finalized (`BLOCKED`, `DONE`,
  `FAILED`, `READY_FOR_MERGE`, `MERGE`, `UPDATE_EPIC`) closed `settled` and
  never relaunched, a killed worker `resumable`, and both `epic archive`
  windows. Each uncertain trigger keeps its claim open and holds the issue
  with its reason; another EPIC's namespace is `held:other-epic` and left
  alone; reconciliation skips its own children, never spawns or writes a
  state, and reports a repository mismatch (whatever EPIC the binding
  names), a network-filesystem run lock and an unlistable root as global;
  finalization's matrix (exit status never decides); `archive_claim`
  closes, moves aside and is idempotent;
  `open_claims_for_epic` fails closed. Every phase has exactly one
  settlement and a durable pause comes first; an open claim without a
  state owns its issue for `run`, a claim file of closed claims owns
  nothing, and an unreadable one owns its issue

### Transitions

- every legal transition
- illegal transitions

### Routing

- review round 1
- review round 2
- review round 5
- review round 6+

### CONTROL_RESULT

- valid result
- multiple blocks
- malformed JSON
- missing/incomplete markers
- wrong phase
- missing fields
- review invariant mismatch
- the published-content policy on agent text the controller publishes
  (marker openers, credential classes, closing references, mentions), and
  each `UPDATE_EPIC` re-request accepting only its own keys
- the `acceptance_action` resolution (#211, `tests/test_result_parser.py`):
  its closed shape, every target key refused by name, an unknown key or
  kind, the body bounds and the published-content policy, the rationale
  rule, no commit or follow-up beside it, at most one per result, and the
  LOCAL refusal
- the `operator_acceptance` resolution (#243, `tests/test_result_parser.py`):
  the documented bounds, inclusive; its closed shape at both levels, every
  target key refused by name, an unknown key or kind; `issue` a JSON
  integer, never twice in one finding (two findings may name one issue);
  the body bounds, the published-content policy and no image or raw HTML
  outside code; the link rule (only into the run's repository, also once
  decoded, in any link form) applied only with a repository; at most four
  actions per result, never beside an `acceptance_action`; the rationale
  rule; and the LOCAL refusal

### Execution

- success
- non-zero exit
- a failed run the adapter reports as transient: OpenCode's classification
  reads only the run-level error lines it prints (the phrase in a tool's
  trace or in a tool's error is not one) and compares each, as a whole
  message, with its list, every listed message tested (an unlisted
  run-level error beside it, a message that only starts the same way, a
  truncated stderr or a timeout is never transient); the engine relaunches
  it after the phase-entry reconciliation, which may resolve or block the
  phase instead, at most `MAX_TRANSIENT_RETRIES` times per step however it
  interleaves with corrections, never in LOCAL mode or while a process the
  launch started may still be running, and the exhausted failure keeps its
  stderr tail and leaves the phase unchanged
- a malformed result from a launch that may have left a process running
  (each of `group_survived_kill`, `capture_abandoned`,
  `orphan_survived_kill`) is not corrected, in REMOTE and LOCAL mode, and
  the error names the leftover; `orphans_unchecked` alone is still corrected
  (#209)
- an agent failure that leaves the state unchanged (a timeout, a failed
  exit, a provider failure, a launch the executor refused, a malformed
  result not corrected, with or without a survivor) says `State unchanged`
  and names the worktree with its uncommitted count, the step's log
  directory and a `resume` command with the `--config` / `--state-dir`
  given, in REMOTE and LOCAL mode (no worktree line there); the count is
  left out when the status read fails, a token-shaped value in the
  configuration never reaches the text, and the CLI prints the command,
  which then resumes the run (#189)
- a LOCAL launch that moved the git anchor (a commit in a write phase, a
  branch switch in `REVIEW`) and returned a malformed result is not
  corrected: the step blocks with the anchor reason, which names the run
  log of the attempt that ended, the checkout and the `local run` command
  that continues with the `--config` / `--state-dir` given, which the CLI
  prints and which then starts the next run while `resume` and `unblock`
  refuse (#189), and the refused correction is not charged to
  `local_pending_attempts` (#209); a REMOTE checkout that moved blocks
  with the checkout and the `unblock` and `resume` commands with those
  flags, and `unblock` prints the `resume` command with them (#189)
- timeout, including a same-group descendant that closed its stdio and
  ignores SIGTERM (escalated to SIGKILL, gone before `execute()` returns),
  a writer outside the group via `setsid` (the capture abandoned after the
  grace and reported as such), and a direct child that survives SIGKILL
  (the kill neutered), which is abandoned after the grace rather than waited
  for and reported as a group that survived the kill
- leftovers after a normal exit (#85): a descendant holding the inherited
  pipes and one with closed stdio are each killed after the exit grace with
  the child's own exit status (0 and non-zero) and output kept and
  `descendants_killed` set; a helper that exits within the grace is neither
  killed nor reported; a `setsid` escapee holding the pipes is reported as an
  abandoned capture; a clean exit and a clean kill report nothing. Through
  the duplex handle, `exited()` reports the child's exit while a descendant
  still holds stdout, `read_line` after `finish()` returns the rest of
  stdout at once, never the deadline, and a `read_line` that meets the
  deadline (waiting into it or starting past it) after the child's exit
  settles the group with the exit grace and returns the rest, the child's
  status kept and no timeout reported
- agent limits (#193), through `execute()` and the duplex handle with short
  real subprocesses: a child that keeps writing, on stdout or on stderr, for
  longer than the idle timeout is not killed; a silent child is killed at
  the idle timeout with its whole group (`timeout_limit == "idle"`,
  `last_activity_at` unset); a child that goes silent after writing is
  killed one idle timeout after its last output; an active child is killed
  at `max_runtime` when a ceiling is set; a limit out of range is refused
  before the spawn; and the deadline arithmetic on a fake clock (first due
  wins, a tie goes to the ceiling, a pinned deadline is not moved by
  output); a wind-down lets a limit fall due without a kill and kills that
  much later, and a child that exits within it is not a timeout. The same
  cases cross the Claude stream and a fake Pi; Pi's `abort` goes out no
  earlier than the limit, a Pi silent for most of its idle limit is not cut
  short, and a protocol failure followed by continued output is still
  killed by its pinned deadline. The engine names the limit and the last
  activity in the error and in `execution.json`, a text-mode Claude
  profile without a ceiling is never launched, and configuration rejects
  the legacy `timeout_seconds` keys by name, out-of-range limits and that
  profile; dry-run and `doctor` show both values
- loop detection (#194): the pure detector with explicit times
  (`tests/test_loop_detect.py`) covers:
  - cycles of period 1 to `max_cycle_period`, at the repeat threshold and
    one short of it;
  - irregular replay (no novelty) and its action floor;
  - masked repeated lines, ended by LF, by a CRLF split across chunks or
    by a bare CR (a retry message redrawn in place is a line; a redrawn
    progress bar or spinner frame is not);
  - a retry storm;
  - each known legitimate repetition staying below every threshold: an
    edit/test loop, `git status` between edits, a few CI polls, pytest dot
    lines, `\r` progress bars and one long silent call;
  - the largest figures real runs recorded (#199) drawing no warning under
    the default configuration;
  - the unset mode resolving to `kill` per invocation, and to `warn` on a
    Pi profile and in `UPDATE_EPIC`; a monitor refusing an unresolved mode;
  - warnings from half a threshold at bounded intervals;
  - calibration in every mode, `off` included;
  - a report that holds names, counts and times only.

  Other files cover each layer:
  - The executor stops a child from `on_chunk` (`stopped`, not
    `timed_out`).
  - The duplex handle's `stop()` is a no-op once the child has exited.
  - The Claude stream and Pi reducers fingerprint input and result, not
    the call id.
  - Pi's failed attempts, in its own order (`message_end` with `error`,
    `agent_end` with `willRetry`, `auto_retry_start`), keep a retry streak
    going until it is conclusive and counted in the calibration; a completed
    response ends it.
  - A loop kill crosses the fake `claude` (stream and text mode) and a fake
    Pi as a timeout with the `loop` limit.
  - End to end on the engine: the default `kill` mode leaves the phase
    unchanged, names the cycle in the error and writes `ended_by: loop` and
    the `loop` report to `execution.json`. `warn` mode never kills and
    warns twice for forty copies of a cycle. Under the default,
    `UPDATE_EPIC` launches in `warn` mode unless the config sets `kill`,
    and the launch line, `request.json` and `execution.json` name the mode
    it ran under; a looping Pi on the fake `pi` is warned about and runs to
    its end. A token-shaped string in the looping tool's input and result,
    and its digests, reach no line and no file.
- bounded capture: a stream past the bound keeps its head and tail, the
  retained size honours a bound smaller than one pipe read, and memory stays
  at the bound plus a constant under one-byte reads
- non-UTF-8 output is replaced, not raised; a multi-byte character across
  the internal head/tail split is intact when nothing was omitted
- arguments containing shell metacharacters
- the allow-listed environment: an exact name and a `PREFIX*` select from
  the controller's environment, an absent name adds no placeholder, an
  invalid entry refuses to launch, no allow-list inherits everything, and
  an explicit `env` is layered over the selection
- stdin (#186): without `stdin_data` it is `/dev/null`; `stdin_data` larger
  than a pipe buffer arrives byte for byte and is followed by EOF; a child
  that never reads it still times out at the deadline with no feeder left
  behind; what a child left unread is counted exactly (`stdin_unread`),
  including a short payload that fit in the pipe and was only partly read,
  which the OpenCode adapter turns into a `provider_failure` when the exit
  would otherwise read as success; and no descriptor of the stdin pipe
  outlives the invocation, a failed spawn included
- setup after the spawn, through `execute()` and the duplex handle: a
  feeder or capture thread the system refuses to start (or a capture pipe
  it refuses to open) kills and reaps the child before the
  `ExecutionError`, and leaves no thread or descriptor behind; so does
  Ctrl-C, or a failure while a capture reader or the duplex handle itself
  is built, raised as itself; a contained invocation also releases the
  containment, its reaper thread included

### Outer sandbox (#167, #168, #169, ADR 0005)

By what they need from the host. Every test outside the real-backend
modules runs with `tests/conftest.py`'s autouse `sandbox_probe` fixture
standing in for the capability probe (`autoforge.engine.probe_sandbox`):
it answers "supported" without a launch, counts its calls, and
`sandbox_probe.unsupported(capability, reason)` makes it answer that the
host lacks one. The real-backend modules call `real_sandbox_backend()`,
which skips with the probe's typed reason, or fails under
`AUTOFORGE_REQUIRE_SANDBOX_BACKEND=1` (`make test-sandbox`), and under
`auto` (`make test-sandbox-measured`, hosted CI's `sandbox` job) fails
exactly when `tests/sandbox_host.py` measures the host capable
([development.md](../development.md#the-outer-sandbox)).

- `tests/test_sandbox_host.py` covers that measurement against a fake
  `bwrap` on `PATH`: an old `bwrap` without `--overlay`, with user
  namespaces refused and a Landlock ABI below 6, is measured incapable and
  each lack named; a capable one is measured capable; no `bwrap` is named;
  the measurement is printed and appended to the CI job summary. It also
  covers what each value of the variable makes of a probe that says
  "unsupported": unset skips, `1` fails, `auto` fails on a host measured
  capable and skips with the measurement otherwise, and any other value
  fails. `tests/test_ci_workflow.py` pins the `sandbox` job to
  `make test-sandbox-measured`, that target to the measurement and then
  every real-backend module under `auto`, and the module list to the one
  `make test-sandbox` requires.
- `tests/test_sandbox.py` runs on any host with Python (the fake-backend
  part on any Linux). It covers:
  - the policy compiler on paths under `tmp_path`: every refusal of D4.2,
    D4.3 and D7.2 names the path and the rule, never quotes a file, and
    happens with process creation disabled; what compiles is ordered
    parents first; C4 masks a denied root that is a direct child of a
    writable projection; C6 records a covered read-only declaration; a
    spelled path (#225) gets each link on its way recreated in the empty
    root, the private temp or the private home, relative contents and
    all, and nothing beside it, and each directory it leaves by `..` made
    there, empty (`--dir`), unless a link below it makes it (PR #232
    R2-F1), needs none when a projection shows its links, and is refused
    through a link in a mask, a denied root, `~/.ssh` or `/proc`, leaving
    a directory in a denied root or `~/.ssh`, or to an interpreter
    nothing shows;
  - the backend's options: the namespaces and `--die-with-parent` always,
    `--unshare-net` only on request, never `--disable-userns`,
    `--new-session` or `--as-pid-1`; masks remounted read-only after their
    carve-outs; `--clearenv` then exactly the environment;
  - two drift guards: no `bwrap` flag in any other source module, and no
    workflow import in the sandbox modules;
  - the executor and duplex wiring against a fake `bwrap` on `PATH`, which
    reads its options from `--args`, runs the command and writes `bwrap`'s
    status records, or fails its setup. Here a setup failure is a typed
    refusal and exit 1 is not; no environment value is on a command line;
    the facts are recorded; and a refusal leaves no descriptor and no held
    containment. Also refused: the platforms without a backend, a missing
    `bwrap`, a backend without overlays, a kernel without Landlock ABI 6,
    an unknown architecture and a failing process scope;
  - the typed capability probe (#169, D14.1): each refusal text (this
    module's own and `bwrap` 0.12's) classified as the capability it
    names; on the fake backend, a probe that passes with a real launch's
    shape, a setup failure named by its step (procfs, bind, overlay, the
    user namespace), a generic "Creating new namespace failed" narrowed
    to the user, PID or IPC namespace by one launch per namespace, a
    missing `bwrap` and one without overlays, and nothing left behind;
  - the launch's own process scope failing in the child of a real
    `Popen` (PR #231 R2-F4): the real scope with its kernel calls scripted
    to refuse `keyctl`, `prctl`, `landlock_create_ruleset` or
    `landlock_restrict_self`. Python's preexec error is generic, but the
    refusal names the call and its errno, and the probe types it as the
    session keyring or Landlock. `bwrap` never runs.
- `tests/test_sandbox_bwrap.py` runs real `bwrap`. A session fixture runs
  `sandbox_bwrap.probe()` once and skips the whole module with the probe's
  reason when this host cannot enforce. Causes include: no `bwrap`,
  unprivileged user namespaces refused (Ubuntu's AppArmor restriction), a
  kernel before Landlock ABI 6 (Linux 6.12), or no `/usr/bin/python3` to
  run the fake commands inside. It skips rather than fails because
  capability is a property of the host (ADR 0005 D14.1): a host that
  cannot enforce refuses every sandboxed launch, which `test_sandbox.py`
  already covers. `make test-sandbox` fails instead. It covers the real
  probe's success (supported, the version, nothing left in the temporary
  directory) and:
  - forbidden sentinels under a fake operator home, a fake agent socket, a
    planted symlink and `..`;
  - read-only, writable, masked, carved-out, pinned and single-file binds
    (`EROFS`, `EXDEV`, `EBUSY`);
  - an overlay that leaves its lower layer intact and puts writes and a
    whiteout in its upper layer;
  - a bounded temp;
  - SysV IPC, a session-keyring key made by a helper in a keyring of its
    own, and a host abstract socket (`EPERM`, while one made inside
    works);
  - an isolated network;
  - a nested namespace that cannot unmask or remount;
  - ADR 0005 D6.1's git common-dir layout with a linked worktree, for both
    ref backends git supports;
  - host processes invisible;
  - the processes a command leaves behind, gone after exit, timeout and
    stop, with and without containment;
  - `kill(0, SIGKILL)` as an abnormal end;
  - duplex through the backend;
  - the spawn-thread invariant;
  - a missing bind source and an exec failure as refusals.
- `tests/test_runlog.py` checks that the facts reach `execution.json` and
  `events.jsonl`, redacted, and only for a sandboxed launch.

The existing executor and duplex suites are the no-description regression:
they run unchanged.

### Launch routing and the agent worktree's git surface (#168, ADR 0005)

Four more files, the same split by what they need from the host:

- `tests/test_sandbox_policy.py` launches nothing: each test compiles a
  description from real paths under `tmp_path` and reads it. It covers:
  - the §2.5 mapping per launch class: a REMOTE agent's writable worktree,
    read-only `.git`, writable admin dir, read-only common dir, the masks
    and the overlay over the shared object store; no persistent cache for
    a REVIEW; a LOCAL launch's read-only git dir with its state masked; a
    LOCAL validation command that may run without a network and an agent
    that never does; an export command that sees only its tree;
  - the D8.2 rewrite: a path-valued name passes only when what it names is
    visible; a value, a `PATH` entry or an auth item's variable reached
    through a link the sandbox does not show (in the private home or
    where the root is empty), or naming the relocated cache, is set to
    where it is inside, with no further host path shown; one through a
    link the sandbox recreates (`/bin`) keeps its spelling;
  - the D7.2 refusals, each naming the path and the rule: a declared
    writable directory on an operator root, a keyring login, a declared
    path inside a denied root; an auth directory named by its variable;
  - install roots (the package or the directory, a prefix with its `lib`,
    never the home) and a script's interpreter; an absolute `#!` through
    a link outside every root as a spelled path whose one link is
    recreated, not its directory, which `PATH` loses too, an `env` there
    included, the system's or one in its own prefix, whose root is
    projected while `env` still finds its name on `PATH` (#225);
  - each adapter's declaration: Claude Code's login, settings and shell
    file, its login and settings at their home paths when they are links
    into a dotfiles directory, its own `CLAUDE_CONFIG_DIR` writable and the
    operator's refused;
    OpenCode's own database and configuration directory; Pi's
    `PI_CODING_AGENT_DIR`, never the operator's.
- `tests/test_agent_worktree.py` runs real git on real repositories. It
  covers:
  - the D6.4 binding: a registry entry binds the worktree, with relative
    pointers too; a rewritten `.git` file (refused naming
    `git worktree repair`), a `.git` directory, two entries naming the
    worktree and an unregistered worktree are refused;
  - the registry listing (#223): the roots `git worktree list` reports for
    the checkout, linked worktrees, a prunable entry (left out) and a
    locked one whose tree is gone (kept), asked from the checkout or a
    linked worktree, with relative pointers, a separate git dir and a bare
    repository; an entry or `gitdir` that is a link or a FIFO names no
    root and is never opened; a registry over its bound or unreadable is
    refused; so is a registry that is itself a link, by the listing, the
    binding, the candidate read and the import, none of which opens or
    lists a path through it, each answering again once the directory is
    put back; and a registry that is a file or a FIFO;
  - the D6.3 candidate read: the detached HEAD, an attached HEAD reported,
    a HEAD that is a link not read, pointer rewrites that do not move the
    candidate, a HEAD naming no commit read as none;
  - a `HEAD` that is a link (to a file outside naming the agent's commit,
    or dangling): the import fails before any git process runs and
    nothing reaches the shared store; an attached `HEAD` whose
    per-worktree reference is such a link imports nothing, and neither
    the import nor the candidate read resolves it;
  - a FIFO outside (#223), watched by `watched_fifo`, that a `HEAD` link
    or a reference `worktrees/<id>/refs/worktree/...` a regular `HEAD`
    names leads to: no controller read opens it (the candidate read, the
    import, the registry listing, the binding), while the git commands
    they replaced (`symbolic-ref` without `--no-recurse`,
    `git worktree list`) do;
  - the D6.5 import: an empty or absent private directory imports
    nothing, an agent's commit reaches the shared store, a corrupt object
    does not, and `info/alternates`, `info/http-alternates`, a link or a
    FIFO is tampering; so is a root that is a link (dangling or not), a
    regular file, a directory reached through a link, one that cannot be
    listed, or one outside the common dir, none taken for an empty store.
- `tests/test_engine_sandbox.py` drives the engine with the scripted
  provider, which records the description each launch compiles and
  launches nothing. It covers: `disabled` launching without one and
  recording the mode in `request.json`; a `required` REMOTE agent's
  worktree, overlay and cache, with the checkout, state directory and
  run logs absent; a REVIEW without a cache; a refused launch (a link
  where the sandbox directories go) recorded, with nothing run and the
  phase unchanged; an alternates file in the private store, or the store
  replaced by a link under `disabled`, entering `BLOCKED` with nothing
  pushed; a `.git` pointed at a decoy repository,
  whose HEAD is never the candidate and whose next launch is refused
  naming the repair; a `HEAD` link, or a reference a regular `HEAD`
  names, to a FIFO outside, which no controller read opens across the
  candidate refusal, the relaunches and the operator's repair (#223); a
  registry the agent replaced with a link under `disabled`, refusing the
  candidate and every relaunch until the operator puts it back (#223); a
  refusal on the spawn path; and the dry-run note.
- `tests/test_sandbox_gate.py` (#169, D12.2, D12.4) drives the engine with
  the probe's stand-in. It covers: under `required` and REMOTE's
  `preferred`, a host that cannot enforce refusing the step with a
  `SandboxCapabilityError` that names the capability and the
  alternatives, the state and run-log directories byte-identical and
  nothing launched; INITIALIZING not gated; the probe asked once per
  engine and again by a new one; a capable host launching inside;
  `disabled` never probing and saying "logical isolation only"; an
  operator read-only path over the state directory refused before
  anything; in LOCAL, the gate checking operator paths against the LOCAL
  launch's own denied roots, so `.git/config` (in the checkout's read-only
  git projection) runs to DONE dropped as covered while `.git/autoforge`
  or another registered worktree is still refused; the dry run's policy by launch class with no probe; `status`
  without a probe; LOCAL `required` refused before the launch is charged;
  LOCAL `preferred` falling back visibly (the plan, `status`, a stderr
  line at every agent and validation launch, every execution record's
  `sandbox` facts) and enforced on a capable host; and a setup failure
  after the gate never re-run without the sandbox.
- `tests/test_doctor_sandbox.py` (#169) runs `doctor`'s sandbox rows with
  real `git` in a temporary repository and a runner that answers every
  sandboxed launch: the capability and mode rows for every mode, host and
  workflow; the policy by class; each provider's declaration (paths and
  credential classes, never a value) and its `--version` launched inside
  by its resolved path; a `dir-rw` login in the provider's own root and a
  keyring login refused with the alternatives (D7.3); hard links in the
  workspace (D4.4), the walk reading a crowded directory an entry at a
  time and no further than its bound; the sandbox directories and their sizes (D8.5);
  loopback and wildcard TCP listeners from a fake `/proc/net`, X11 flagged
  (D9.3); operator paths over a denied root (D15.4); and no `gh` call in
  LOCAL. Its denied roots are the run's own (PR #231 R2-F1): for the
  default LOCAL state, a relative `--state-dir` and a relative
  `state_dir`, they equal the gate's. Its operator-path row fails exactly
  where the gate refuses the step, and an `OPENCODE_DB` in the state
  directory refuses OpenCode's row with nothing launched. A provider's
  `--version` gets a launch's environment (R2-F2): an operator's
  `OPENCODE_CONFIG_DIR` is replaced by the launch's own directory, which
  exists while the probe runs. A `CLAUDE_CONFIG_DIR` link in the home is
  spelled as the directory it leads to. A token across the version line's
  clip leaves no fragment in the text or JSON output (R2-F3). A CLI whose
  `#!` names a link outside every root is compiled with that link
  recreated, and its row names the spelled path (#225).
- `tests/test_sandbox_routing.py` runs real `bwrap` and skips its whole
  module with the probe's reason when this host cannot enforce, as
  `test_sandbox_bwrap.py` does (also off Linux and without
  `/usr/bin/python3`). It covers: a REMOTE agent, for each of Claude
  Code's text and `stream-json` paths and OpenCode, that reaches its read
  context, cache and temp (and Claude alone its login file), finds no SSH
  key or agent socket, checkout, state directory, neighbouring worktree or
  its admin dir, gets `EROFS` writing the common dir's HEAD, a branch ref,
  the configuration, a hook, its `.git` or `commondir`, leaves no process
  behind, and commits through the overlay into the private directory,
  where the candidate names no commit until the strict import ran; an
  alternates file written through the overlay as tampering; a
  LOCAL validation command that gets `EROFS` on the git directory; an
  export command that sees its tree and no network when isolated; a
  `required` LOCAL run that reaches `DONE`; a `required` pre-merge command
  that sees only the export; an all-Pi run sandboxed in every child,
  the auth preflight included; a `HEAD` an agent replaced with a link to a
  file invisible inside, which the import refuses before git reads it,
  leaving the commit out of the shared store; such a `HEAD` link, or a
  reference a regular `HEAD` names, to a FIFO on the host that neither the
  import, the candidate read, the registry listing nor the binding opens
  (#223); and the linked provider
  paths found inside: Pi's auth preflight (a fake that is ready only when
  it reads `$PI_CODING_AGENT_DIR/auth.json`) with the directory or a link
  to it, a fake Claude Code reading and writing through a linked
  `CLAUDE_CONFIG_DIR` and reading linked default login and settings files,
  and a fake OpenCode creating its database through a linked directory.
  A fake agent whose absolute `#!` names a link outside every root, one
  link, one passing through a directory it leaves by `..` (PR #232
  R2-F1), or a Homebrew-shaped `opt/` to `Cellar/` chain, runs inside with
  its interpreter seeing that spelling, and every directory on the way
  holds only the next link, the one left by `..` nothing; so does one
  whose `#!` names a link there to `env`, the system's or a copy in its
  own prefix, that finds the interpreter on `PATH` (#225).
  It also covers `doctor`'s sandboxed `--version` run through the same
  backend, with the environment a launch gets (PR #231 R2-F2). A fake
  Claude Code needs its linked `CLAUDE_CONFIG_DIR` to lead to a directory
  inside. A fake OpenCode needs an empty, writable `OPENCODE_CONFIG_DIR`
  of its own, although the operator's names their configuration root.
  Both rows pass, and the operator's directory is left unwritten.
- `tests/test_sandbox_parity.py` (#169, D13.5) runs real `bwrap` and skips
  as the other two do. One probe runs in every launch path: Claude Code
  text and `stream-json`, OpenCode, Pi's RPC child and its `auth check`
  preflight, a provider-neutral adapter (the base class's launch and
  declaration alone), a LOCAL validation command and an export command.
  Every path must report the same: the operator's SSH key and `gh`
  configuration, the state directory, a neighbouring worktree and the
  controller's directory absent; its workspace, `/tmp` and private home
  writable; `/usr` and `/etc` read-only; `/run/user` absent; a host
  abstract socket refused by the Landlock scope; new user, PID and IPC
  namespaces with the host network; no `XDG_RUNTIME_DIR` or
  `SSH_AUTH_SOCK`; and the same recorded facts.

`test_integration.py`'s full loops and `test_replan.py`'s hard-threshold
replan run in both modes, and every launch under `required` carries a
description. `test_config.py` pins the section (`disabled` by default,
both formats read, `preferred` falling back in LOCAL only, any other mode
refused, `read_only_paths` read and each forbidden source, the home or an
ancestor, a kernel root, a link to a forbidden source and an overlap
refused at load) and the example file; `test_evidence.py` pins which
modes require enforced evidence; `test_prompts.py` pins the narrowed-view wording both common
prompts carry in every mode.

### Agent isolation (REMOTE)

- the agent's cwd is the per-issue worktree under the git common dir (or
  `execution.worktree_dir`), detached at the checkout's HEAD, without the
  operator's uncommitted work or `.autoforge/`; it is reused across phases
  as the agent left it
- what it holds uncommitted is described to every launch of a write phase
  (`ANALYZE_EXECUTE`, `FIX`, `REPLAN_REEXECUTE`), a relaunch after a
  timeout included: a clean worktree renders nothing; a dirty one renders
  its branch, `HEAD` and each entry's status code and path, never file
  content; the list is cut after `MAX_WORKTREE_STATE_ENTRIES` with how many
  more, each path escaped onto one line and bounded, inside a fence no path
  can close, and the branch name escaped and inside a code span no
  backtick in it can close; a status read that fails renders nothing and
  the run log says why, and the launch proceeds; the three write templates
  (and no other) require the variable, so a missing value fails rendering,
  and the instruction is in `worktree_state.md` (`test_engine.py`,
  `test_replan.py`, `test_prompts.py`; the parsed status codes, renames and
  copies: `test_local.py`) (#187)
- a path that exists but is not a worktree root of this repository, or a
  location inside the operator's working tree, is refused, not adopted
- a dry run creates no worktree and runs no git
- worktree creation, its identity read, and the workspace
  reader are hardened (the registry is read without git, #223) (ADR 0004 D7.3): a planted `post-checkout` or
  `reference-transaction` hook (in `hooks/` or a configured `core.hooksPath`)
  and a configured `core.fsmonitor` do not run, a filter driver sees no
  token and no operator `GIT_*`, and an operator `GIT_DIR` redirects neither
  the worktree, the workspace reads nor the lock
- the agent request and the run log carry the configured allow-list; pre-merge
  and validation commands run under the same one
- HEAD or branch of the operator's checkout changing while the agent ran
  enters BLOCKED (also when the invocation itself failed, and before any
  relaunch: a launch that moved it and then failed transiently or returned
  a result to correct is neither reconciled nor relaunched); agent commits
  in its own worktree are not drift

### No GitHub credential in a launch (#165)

Each route in the inventory of
[github-safety.md](github-safety.md#credential-routes) has its test there or
a stated reason it has none. The coverage that must keep working:

- config load refuses every forbidden name and every `*` prefix covering
  one, in `env_allowlist` and `env_allowlist_extra`, naming the entry and
  the rule and never a value; an adapter's `environment_names` and its
  launch allow-list are refused the same way; the default list carries no
  forbidden name (`test_config.py`, `test_child_env.py`,
  `test_providers.py`)
- with an operator environment that holds every route (tokens, `gh`
  configuration, a session bus, an SSH agent, askpass, git configuration
  from the environment, a global helper), a child gets none of them and
  every controller-set variable; PATH-injected fake `gh`, `ssh`, `curl`,
  `wget` and a credential helper show that an agent finds no route the
  controller's own process finds, through a repository- or worktree-scope
  helper and askpass too, and that the controller's `GitHubClient` keeps
  its credential; the real `curl` and `wget`, where installed, send the
  header of the operator's configuration file to a loopback server for the
  controller and none from a launch (`test_child_env.py`)
- the preflight refuses a repository or worktree helper (also through an
  include), a credential in a remote URL or an `insteadOf` base, an
  `http.extraHeader`, a GitHub or `default` netrc entry in any reader's
  syntax (quoted, escaped, upper-case, a `macdef` given as a login; the
  real `curl -n` against a loopback server or Python's `netrc` takes each
  one's credential), and git configuration or output it cannot read; it
  accepts a global helper the child never reads; its message names no
  value; a launch never inherits `NETRC`, whose file the real curl 8.16
  and later reads for the controller (`test_child_env.py`)
- a launch's shells run no startup file of the operator's to run a
  command: the real zsh (`-c`, `-l -c`, `-i -c`, a zsh shebang) with the
  operator's zsh files exporting a token, the real bash and sh with the
  operator's `BASH_ENV`, and R3-F1's report through the real OpenCode
  adapter and executor (a fake `opencode` choosing `$SHELL` as 2.0.26
  does) all leave a fake `gh pr merge` refused, and the same launch
  without the controller's `ZDOTDIR` or with the operator's `BASH_ENV`
  merges; `SHELL` is never fish or a csh; the login-shell probe names the
  shell and variable of a `~/.profile` or `~/.bash_profile` that restores
  a route, never its value, tolerates an SSH agent or session bus alone,
  ignores zsh files in the home, and refuses a probe without an answer;
  an agent, validation or pre-merge launch and `doctor` refuse on it
  (`test_child_env.py`, `test_engine.py`, `test_local.py`,
  `test_doctor.py`)
- an agent preflight refusal, the credential preflight's or the
  adapter's own (`integration_preflight`), leaves the state file byte for
  byte; a
  validation-command refusal leaves the phase unverified; a pre-merge
  refusal blocks the merge (`test_engine.py`, `test_local.py`)
- every launch of a full REMOTE lifecycle, for scripted providers and for
  an all-Pi run (the RPC child and its auth preflight), runs with the
  isolation in force and its phase's read context; a LOCAL run's launches
  get the same isolation and no read context (`test_integration.py`,
  `test_local.py`)
- a write through a credential that slipped through anyway is not
  journaled and blocks (`test_engine.py`)
- `doctor` refuses a token-bearing origin without printing it, refuses
  the configuration routes, a netrc entry and an OpenCode organization,
  lists the launch environment
  by name only, gives Pi's probes the same overrides, and `local doctor`
  keeps the environment rows with zero `gh` calls (`test_doctor.py`)
- Claude Code launches with `--safe-mode`, `--strict-mcp-config` and
  `--setting-sources=user`, and a profile whose `extra_args` would load an
  integration or settings is refused without its value
  (`test_providers.py`, `test_config.py`); `doctor` fails a `claude` whose
  `--help` lacks one of the three (`test_doctor.py`); its Bash tool, in a
  fake `claude` that snapshots and runs a command as 2.1.296 does with the
  real bash and zsh, runs none of the operator's shell functions and
  aliases (snapshot and login shell, a startup file that shadows the
  cleaner's builtins, an operator's `CLAUDE_CODE_SHELL` or
  `CLAUDE_CODE_SHELL_PREFIX`, the repository's settings `env`), while the
  same launch without the adapter's isolation does; a launch whose Claude
  user settings or global configuration set a variable that would undo
  its isolation, or cannot be read, is refused before the CLI starts,
  naming the file and the variable, never the value (`test_providers.py`,
  `test_doctor.py`); an
  OpenCode launch, through the real executor, reads no MCP server or
  plugin of any configuration a fake `opencode` that resolves its
  configuration as 2.0.26 does would load, and is refused before the
  CLI starts while OpenCode's database (`OPENCODE_DB` included) lists an
  organization, cached or not, or its legacy `auth.json` has one, while a
  launch with no organization proceeds; every adapter passes
  `AgentRequest.env` to the executor (`test_providers.py`)
- the read context: redacted, bounded with explicit omission markers,
  project text fenced so it cannot close its fence; an unreadable item is a
  file that names the error type and the launch proceeds; a dry run writes
  none and shows a placeholder (`test_read_context.py`, `test_engine.py`)
- no REMOTE prompt template tells the agent to read GitHub with `gh`, and
  each names the read-context files its phase gets (`test_prompts.py`)

### GitHub verification

- valid PR
- missing PR
- HEAD mismatch
- comment verification
- the merge gate re-reading the clean review's comment (#94): a state
  parked in `READY_FOR_MERGE` / `MERGE` whose comment is gone, on another
  PR, names another round, HEAD, base or merge base, or says `needs_fix_round: true`
  blocks without a merge; the happy path reads it exactly once per pass
- the PR identity read and the checks read (#291, `tests/test_github.py`,
  `tests/test_engine.py`, `tests/test_integration.py`,
  `tests/test_doctor.py`): the identity read never requests
  `statusCheckRollup` and reads as "not read", never as "no checks";
  GitHub's checks refusal for a fine-grained token or an integration on a
  `statusCheckRollup` path is `GitHubChecksAccessError`, asked once, never
  a not-found, while the same phrase on another path, a bare 403, a 404, a
  transient failure and bad credentials keep their classes; a malformed
  rollup is conclusive; `pr_exists` answers `False` only on a not-found;
  under the refusal `REVIEW` and `FIX` launch (the reviewer's `checks.md`
  says the checks are unknown) and `READY_FOR_MERGE` / `MERGE` block with
  the credential's remedy after one read, nothing merged; `doctor`'s
  checks-read row is PASS only on a check run read, DENIED (FAIL with the
  gate open, WARN closed) on a refusal, UNKNOWN without a candidate, on
  statuses only, a plan limit, a not-found or a transient failure, and
  prints no token
- follow-up issue verification
- typed effect writes are never retried on an ambiguous outcome, while reads
  keep their transient retry (`tests/test_github.py`)
- every Wave 1 effect kind at the operation level, against `FakeGitHub` and
  a local bare repository (`tests/test_effects.py`): success, already
  exists, conflicting identity, duplicate markers, a precondition violated
  between intent and write, a write lost or landed with its reply lost, a
  crash after the write before the save, a duplicate invocation after
  restart, and the attempt bound
- the controller git transport (`tests/test_git_transport.py`): the default
  branch, a non-fast-forward and a lease mismatch are refused, and a
  `pushurl`, `insteadOf`, `core.sshCommand`, `core.hooksPath` or `pre-push`
  hook planted in the shared repository neither redirects a push nor runs
- the byte-exact export (#171, `tests/test_materialize.py`, in sha1 and
  sha256 repositories): every exported blob equals `git cat-file blob` under
  a candidate `.gitattributes` with `eol=crlf`, `ident` and a filter, the
  repository's `info/attributes`, `core.attributesFile`, `core.autocrlf`
  and an operator smudge program, which never runs; modes `0644` and
  `0755`, symlinks as link text (relative, absolute, `..`-escaping,
  dangling), a gitlink as an empty, counted directory whose commit is
  absent, an empty subtree, names and contents that are not UTF-8, a blob
  past every capture bound and any chunking of the stream; the proof
  refuses each tamper class (a byte, an added or removed entry, a flipped
  executable bit, a retargeted link, a link replaced by a file and back, a
  non-empty or replaced gitlink, a FIFO), a legacy `100664` mode and a
  simulated case-folding collision; `.`, `..` and `.git` in any case, and
  names holding `/`, are refused before anything is written; a commit or a
  blob missing from the shared store, a commit only in a private object
  directory before the strict import (it exports after it), and anything
  but a full commit id are not materializable; a replacement object changes
  nothing; the directory is removed after a pass, a failure and an
  exception. The merge gate's commands see the committed bytes, and an
  unexportable reviewed HEAD stays inconclusive (`tests/test_engine.py`)
- `ANALYZE_EXECUTE`'s publication (#161, `tests/test_engine.py`, engine
  built with `origin=True`): the controller pushes the agent's commit and
  opens the PR (or adopts the open unmarked PR on the branch) and reads it
  back; every candidate refusal (another issue, a `head_sha` other than the
  worktree's, an attached `HEAD`, nothing committed, unrelated history, a
  commit message closing another issue or carrying a credential, a body
  linking the issue itself) is a correction with nothing published; the
  entry blocks before launching on a PR it would not publish to; a marker
  PR the agent opened itself, a branch pushed by someone else, or GitHub
  changing under the run blocks with nothing published; an unmarked PR
  that appears on the branch after the entry is never adopted, at the plan
  or at a re-entry; a default branch renamed during the run, across a
  re-entry, or after the plan (also with the create attempted) blocks with
  no PR created, and an adopted PR retargeted after the plan is not
  written to
- `REVIEW`'s publication (#162, `tests/test_engine.py`, `test_k4_*` and the
  review tests): the controller renders the round's comment from the
  validated result, posts it and reads it back, and the reviewer posts
  nothing; a round or HEAD other than the bound one, prose or a finding the
  published-content policy refuses, fields valid alone that compose a
  mention outside code, and a rendered comment over GitHub's limit are
  corrections with nothing posted, and a saved plan with such fields is
  refused on load; a title's unpaired backtick and a resolution opening
  with a fence keep their code in the posted comment; a round comment the controller
  did not journal blocks at the entry (nothing launched) and at the
  precondition read (nothing planned); a stale round still gets its
  comment; the entry fetches the bound HEAD and merge base, and a failed
  fetch launches nothing; the merge gate re-reads the comment the
  controller posted, and blocks once an edit drops its marker; a dry run,
  with or without a saved plan, reads, fetches, launches and posts nothing
- `FIX`'s publication (#163, `tests/test_engine.py`, the `test_fix_*`
  tests): the fixer commits and publishes nothing, and the controller
  creates the follow-up issues it asks for, appends the markers to the
  handed-over issues it defers to and pushes its commit, in that order and
  the push last; a `previous_head_sha` or `head_sha` other than the
  reviewed HEAD or the worktree's `HEAD`, a `HEAD` attached to a branch or
  not descending from the reviewed HEAD, a published commit message the
  policy refuses, a `fixed` finding with no commit, a `commit_sha` outside
  the published range, resolutions other than exactly the open findings,
  a deferral to the current issue or to an issue not handed over, a
  finding's own follow-up resolved otherwise, and a follow-up title or
  body the published-content policy refuses are corrections with nothing
  created, appended or pushed; the entry fetches the reviewed HEAD before
  the launch and hands over each finding's follow-up and the earlier
  rounds'; the current issue carrying an open finding's marker blocks
  before the launch, and a saved plan deferring to it (replayed or not)
  blocks with nothing sent; a PR headed in a fork, with no readable head
  repository or with no readable head branch blocks before the launch, and
  one whose head repository is gone when the fixer returns blocks before
  the plan; a PR closed, losing its head repository or retargeted after a
  follow-up is created or appended is read before the push and pushes
  nothing; a push target lost when the push lands records no fix, and
  `unblock` completes the plan once it is restored; a
  follow-up issue the controller did not journal, a reused one
  no longer the finding's, an append target closed, moved or holding a
  credential block with nothing sent; a push by someone else, while the
  fixer ran or before the controller's (refused by the lease), goes to
  `REVIEW` with nothing more sent; the next `REVIEW` prompt renders the
  recorded resolutions; Pi reaches the same outcome as the scripted
  provider; a dry run, with or without a saved plan, reads, fetches,
  launches and writes nothing
- acceptance actions (#211, the "acceptance actions" section of
  `tests/test_engine.py`, K9 in `tests/test_effects.py`, the status and
  unblock flags in `tests/test_cli.py`): under `approve` the plan is saved
  with K9 first and the push last and the step blocks with nothing sent and
  a reason naming the id, the finding, the payload digest and the held-back
  push; an approval publishes the comment, reads it back and pushes,
  without the fixer, and the next reviewer gets its URL; `auto` completes
  in one step; `off` is a correction; a closed issue, or one outside the
  repository, holds the comment, and the exact payload already there, a
  landed `attempted` one included, until it qualifies again, with no
  later write of the plan sent; an issue that stops qualifying between the
  comment's write and its read-back blocks the same step with that reason,
  the record `attempted` and the reason saying the comment was sent, and
  an unblock while it still does not qualify sends nothing; once it does,
  the one comment is adopted (PR #221 R2-F1); the exact payload an
  operator posts is adopted; a decline drops the unsent plan, relaunches
  the fixer with the decline in its prompt and refuses the same comment
  again; repeated declines past the outcome history's bound keep every
  decline of the round, and once the round holds the most declines any
  further acceptance action of it is refused (PR #221 R3-F1); a push
  while it waits refuses an approval as stale and records the action
  `stale`;
  every refused unblock (unknown id, no evidence, a posted payload
  declined, an edited or duplicated comment, a closed issue, a stale
  decline) leaves the state byte-identical, also as a dry run; the
  generic per-kind effect tests run for K9, with its hold, approval and
  load rules; `status`, `status --json` and `status --action` show the
  action and its exact bytes
- operator actions (#243, ADR 0004 K10; `tests/test_operator_actions.py`
  for the engine, unblock and `status --action`, K10 in
  `tests/test_effects.py`, the marker in `tests/test_claims.py`,
  `operator-evidence.md` in `tests/test_read_context.py`, the typed
  wrong-target read in `tests/test_github.py`, `status`, `unblock` and the
  `run` guard in `tests/test_cli.py`): under `approve` and `auto` alike the
  plan is saved with the K10 records first and the step blocks with
  nothing sent; exact payloads complete it without relaunching the fixer;
  a plain unblock on an assertion, and every other refused unblock, leaves
  the state byte-identical, also as a dry run; changed line endings, a
  payload posted twice or on the wrong issue are conflicts; a request
  outside the rules, and a target that is a PR or missing, are
  corrections, an unreadable target blocks and a transient read
  propagates; `off` offers nothing; the allowed set is the entry's even
  when the issue body changes; a `#0` in the issue body, shorthand or URL,
  is no target, an ordinary `FIX` over it proceeds and every state it
  saves loads again, and an entry observation the loader would refuse is
  never saved (PR #254 R3-F1); bound evidence is verified as
  operator-bound, an edited bound comment holds, the binding rules refuse
  and one comment is never evidence for two actions (another action's
  exact payload is never bound, and the prefix pass, a plain unblock and
  drift never verify one comment twice, PR #254 R2-F1), while an earlier
  round's payload may be bound; a binding reads every pending target and
  refuses on one it cannot read, named or not, also as a dry run (R2-F3);
  `--approve-action X-…` is refused; a decline keeps a verified comment,
  withdraws the other pending actions, is refused while anything of the
  plan is on GitHub, reads every pending target and refuses on one it
  cannot read or that answers as another object, named or not, also as a
  dry run, while one that is not found is declined or withdrawn (R4-F1),
  and shares the round's bound with K9; drift records each action
  `verified` or `stale`; `status` names a committed candidate held behind
  the actions, and says nothing was committed only when nothing was
  (R2-F2); a bound comment edited or deleted, and a target or
  its comments that cannot be read or are gone, leave the reloaded
  `status` and `status --json` reporting `verification_failed` with the
  reason, until a new binding or the next pass replaces or clears it
  (R3-F3); every K10 read-back row and each of D2.1a's six
  layers in `tests/test_effects.py`, a comment past the first hundred and
  a target GitHub answers with another object through the real client
  included; the maintainer-captured byte-fidelity fixture replayed through
  the real client in `tests/test_operator_comment_fidelity.py` (the
  captured pipe comment is the payload byte for byte and exact evidence;
  the captured web paste lost a hard break's trailing spaces and gained
  trailing bytes, and is a conflict; see `docs/development.md`)
- `REPLAN_REEXECUTE`'s publication (#164, `tests/test_replan.py`, "The
  controller publishes the replacement"): the replan agent commits on the
  base its prompt names and publishes nothing, and the controller pushes
  the commit to the branch derived from the transaction id and creates the
  replacement PR, its body the agent's text followed by the controller's
  closing block, implementation marker and transaction marker attesting
  the result's counts; a marker in the agent's body, a commit message
  closing another issue, a `HEAD` attached to a local branch and a
  malformed result are corrections with nothing pushed or created; claim
  mismatches, failed tests, a `head_sha` other than the worktree's `HEAD`,
  a candidate that is the base or does not descend from it are rejections
  with nothing pushed or created; a PR the agent opens itself is never the
  replacement, and a PR carrying the transaction's marker at `PREPARED`
  with no saved plan is refused, except on the D13.3 / D13.8 legacy re-entry,
  which binds it; a replacement branch that exists before the launch or
  appears while the agent ran rejects with nothing sent, one moved between
  the push and the create creates no PR, and one moved after the create is
  never bound; a crash after the plan is saved completes it on `resume`
  without the agent, a create lost in flight is reconciled and issued once
  more, a create that landed before the crash is observed and not
  repeated, and a replacement a human closed after the crash is never
  bound; a crash after the agent ran relaunches it on the recorded base; a
  provider failure publishes nothing and the relaunch is published; Pi
  reaches the same outcome as the scripted provider; a dry run over a
  saved plan sends nothing and launches no agent
- the EPIC dependency graph (#179, ADR 0008 D3, D4, D2.2 step 4,
  `tests/test_epic_graph.py`, `tests/test_github.py`,
  `tests/test_validation.py`). Every test is offline: a fake `gh` serves
  synthetic graphs and the fixtures under `tests/fixtures/github_graph`
  to the real client, and refuses anything but a plain GET. The fixtures
  are GitHub's answers for EPIC #176, captured live. The cases:
  - the typed reads decode the fixtures' fields and refuse each malformed
    row, an answer for another issue, a pull request, a short page before
    the last and a non-paginated answer, and keep the transient retry of
    reads; an issue without a parent, which GitHub answers without a
    `parent_issue_url` field, reads as parentless;
  - the recorded graph builds and evaluates (#179 ready, #182 and #183
    blocked, the four closed children closed), its answers hold both
    pinned behaviours, and the recorded child is `unchanged` on re-read;
  - independent children, a chain and a diamond are released in the
    specified order;
  - cross-EPIC and cross-repository blockers count by live state, and an
    unreadable one blocks;
  - only `completed` satisfies: `not_planned`, `duplicate`, no reason and
    an unknown reason are `closed-unsatisfied` with the reason shown, and
    a reopened blocker blocks again;
  - cycles of length 1, 2 and 3 refuse with the canonical cycle under
    shuffled API order, and a cycle through a closed child does not
    refuse;
  - a duplicated child or edge is de-duplicated by id, and a duplicate that
    contradicts itself refuses;
  - a removed child disappears, a completed child is `closed`, and a
    nested parent and a foreign child are unsupported (never traversed);
  - verdict precedence holds with every `LocalFact`;
  - a 30-child random DAG shuffled 200 times (API and insertion order)
    gives an identical verdict tuple, snapshot digest and node digests,
    and the fan-out key matches an independent count;
  - each completeness failure refuses: a short or over-long child or
    blocker listing, each bound (before the listing is read when the
    summary already exceeds it), a repository-id mismatch, a closed EPIC,
    a failed read (transient or not, with its cause) and an inconsistent
    parent; children without blockers and closed children are not
    queried;
  - the fixtures with closed children or closed blockers removed from the
    counts are refused;
  - the digests change for every eligibility fact and for no excluded
    field (hostile titles, bodies, labels, timestamps, `observed_at`),
    whatever the input order, and #181's `Admission` accepts them;
  - the re-read is `stale` for a blocker added, reopened or removed, the
    child closed, re-parented, given sub-issues, given another id or
    reopened, and an incomplete or over-bound blocker listing; it is
    `unchanged` otherwise, raises on a failed read and never changes the
    snapshot;
  - human changes between snapshots show in the next one;
  - diagnostics never quote a title or a body and are bounded;
  - an AST scan finds no GitHub write, no client method beyond the four
    reads and no process, file or lock module in `epic_graph.py`.

### Recovery

- external side effect completed before state write
- resume without duplicating work
- the `UPDATE_EPIC` progress comment across every crash window of the
  effect lifecycle (`test_k8_*` in `tests/test_engine.py`), the one-shot
  legacy re-entry (D13.7) including a crash after its adoption was saved,
  and an unjournaled comment blocking (D9.7)
- an `UPDATE_EPIC` completion context persisted with an empty plan and no
  adopted comment (or a plan beside an adoption, or no entry observation)
  is refused on load, with nothing published, and completion without a
  posted or adopted comment writes no roadmap and switches no issue
- dry-run executing no effect: the `UPDATE_EPIC` plan is built with a
  GitHub client that fails the test on any use
- `ANALYZE_EXECUTE` across its crash windows: a crash after the plan was
  saved completes from the journal without the agent; a PR create whose
  reply was lost is read back, not resent, and one that never landed is
  sent again by the next process; a branch moved after the plan conflicts
  and `unblock` completes the plan; a dry run plans the publication and
  sends nothing
- `REVIEW` across its crash windows (`test_k4_*` in `tests/test_engine.py`):
  a crash before the plan is saved relaunches the reviewer and posts once;
  an intent saved and never issued posts once without a relaunch; a
  comment that landed before the save is observed, and the round completes
  exactly as an uninterrupted one would (open findings, the stale path's
  carried findings, the review history, the next phase); a write lost in
  flight is reconciled and issued once more; a second matching comment, or
  one posted by someone else after the intent, blocks naming it; `unblock`
  with a saved plan completes the round without the reviewer; a persisted
  comment body edited after the save (alone, or with the context's prose
  section it renders) is refused on load and never posted
  (`tests/test_state.py` covers each edit: a body that is not the
  rendering of its context, and a section the parser would refuse)
- `FIX` across its crash windows
  (`test_fix_crash_window_resumes_from_the_journal_and_writes_each_effect_once`):
  a crash with a create, an append or the push intended, landed with its
  save lost, or observed, completes the plan from the journal without the
  fixer and writes each effect exactly once; a PR head at the planned
  candidate completes with no `REVIEW` detour; a duplicate invocation after
  the effects writes nothing again; an issue body edited after the plan is
  rebased, and one edited to hold a credential conflicts with no write; a
  follow-up the controller created and a human closed before the read-back
  blocks; a persisted follow-up create whose title or body the parser would
  refuse, or whose body is not the fixer's text followed by the controller's
  reference and marker, is refused on load (the `k5-*` cases of
  `test_a_corrupt_record_fails_loudly` in `tests/test_effects.py`)
- an acceptance comment (#211) that landed before its observation was
  saved (or with it saved) is adopted by the next process, which pushes
  without the fixer and never posts it twice; one whose write never landed
  is sent once more within the bound; a persisted K9 whose approval names
  other bytes, or that is `attempted` without one, is refused on load (the
  `k9-*` cases of `test_a_corrupt_record_fails_loudly`); a write whose issue
  stops qualifying before its read-back is held, never `EffectPending`,
  with or without its comment on the issue, and a refused one is a
  `conflict` naming the issue (PR #221 R2-F1)
- operator comments (#243): a process killed after either observation save
  of the prefix pass, or before the completion save, is completed by the
  next process from the journal with one marked comment per target, two
  `verified` outcomes, one fixer launch and the PR at the reviewed HEAD
  (`test_operator_notes_survive_a_restart_at_every_window` in
  `tests/test_integration.py`); `verify` saves an observation or a conflict
  once and a hold never, and a target or listing that cannot be read
  raises with nothing saved (`tests/test_effects.py`)

### Integration

Maintain a fake/scripted-provider loop that can exercise:

```text
INITIALIZING
-> ANALYZE_EXECUTE
-> REVIEW (findings)
-> FIX
-> REVIEW (clean)
-> READY_FOR_MERGE
```

without external writes.

The operator-action path (#243, `tests/test_integration.py`) runs it
with REVIEW raising a finding the fixer resolves as two operator actions
on closed related issues (PR #242's reproduction): blocked with nothing
posted; a ticked checkbox, a "done" comment and a reason with URLs
fulfilling nothing; each exact payload verified as the operator posts it,
across a restart; REVIEW round 2 reading both as evidence and coming back
clean; one fixer launch, no push, no merge and no comment the controller
posted on either issue. Its bound and withdrawn variants run the same
loop.

Maintain, too, the scheduler's end-to-end loop (#183, ADR 0008;
`tests/test_epic_commands.py`, `tests/test_epic_recovery.py`): offline and
deterministic, `autoforge epic` run through `cli.main` as an operator runs
it, the EPIC's graph served by the fake `gh` behind the real read-only
client, every worker a real subprocess running the production worker on
the in-memory GitHub and scripted agents. It must keep covering:

- every row of D14.5's crash-window table, by SIGKILL of a real scheduler,
  worker or `epic archive` process at the window (a fault-injection hook
  in `tests/epic_cli.py` or `tests/scheduler_worker.py`, never timing),
  recovered by `epic resume` (or a re-run `epic archive`) with no second
  worker for an issue and no second PR; the validator-marker row is the
  engine's own crash tests (ADR 0006 D10.7);
- `epic run` refusing an open claim and pointing to `epic resume`, also
  with `--force`, which moves only a corrupt `scheduler.json` aside; exit 3
  for claims left open, 1 for global uncertainty, 2 for a held lock;
- every `epic status` category with its next command, the strict
  `autoforge.epic-status/1` keys, a hung worker's diagnostics, a spawn
  failure held, the not-ended hint, an entry of `issues/` that is not a
  namespace, a corrupt namespace and a run lock that cannot be probed (a
  child's and an orphaned one) and a `scheduler.json` of another
  repository id than GitHub's, with no namespace showing it, as problems
  (exit 1), a rename keeping the id as none, and that it takes no lock,
  signals nothing and writes nothing; `epic run --dry-run` creating
  nothing;
- every `epic` command refusing an EPIC of another repository than the
  checkout's `origin` (and a checkout without one) with nothing written
  locally and nothing read from GitHub; `epic stop` refusing a
  `scheduler.json` of another repository and `epic archive` a namespace
  bound under one, by repository id, also when another repository took the
  recorded name (stop request, claim, state and `scheduler.json`
  untouched), and both going on after a rename with the binding never
  rewritten;
- every `epic stop` outcome, a drain and a `--now` of a running scheduler,
  and a stop after a SIGKILLed scheduler listing its surviving worker with
  the survivors' caveat;
  `epic archive` closing then moving aside, idempotent, refused while the
  issue has a live owner or another EPIC, an unusable claim file moved
  aside;
- GitHub changing between ticks: a blocker closed and another reopened, a
  child completed or given a dependency between snapshot and claim, a
  child removed or re-parented, the EPIC closed, a human push to an active
  PR;
- standalone guards both ways, LOCAL unaffected, and no worker merging or
  writing the EPIC with the merge gate open.

### Live progress (#192)

Progress is observability, never part of an outcome. Keep covered:

- the Claude `stream-json` reducer (`tests/test_claude_stream.py`): the
  `result` text verbatim, `is_error` checked before the subtype, a
  malformed line (including one the JSON decoder refuses for its integer or
  nesting limit, which must not escape as an exception), a missing result,
  an oversize result line, an error result in any turn, a result outside a
  turn and stdout ending inside a later turn each a `provider_failure`, the
  last turn's result the outcome of a run of several, also when the CLI
  holds the results back until after the last turn (too few or too many of
  them a `provider_failure`), an oversize ordinary
  line counted and skipped, a flat scalar summary (a per-turn sum left out
  once one result lacks its count), and no command, thinking, assistant
  text or tool result in any event
- the real `ClaudeCodeProvider` on the fake `claude` (`tests/claude_fake.py`):
  argv, stdin on `/dev/null`, orphan containment, the timeout, and a sink
  that raises never failing the run
- the CLI's exit, not EOF, ends the stream (ADR 0002): a helper still
  holding the CLI's stdout after it exits is killed after the exit grace,
  the CLI's own status and outcome kept and no timeout reported, also when
  the grace runs past the deadline, when the helper keeps writing lines so
  that no read goes quiet, and when the CLI exits during the read that runs
  into the deadline; a CLI still running at its deadline is a timeout
  however busy its stdout; a helper that exits within the grace is
  neither killed nor reported; and what stdout carries after the exit (a
  result outside a turn, a turn ending in an error result or without its
  result, a malformed or unterminated line, an overflow) still fails the run
- cleaning (`tests/test_progress.py`): escapes and controls stripped
  before redaction and redaction before the clip, so a token split by an
  escape or cut by the bound is never shown
- the acceptance test: a fake `claude` whose tool inputs carry a seeded
  token, a token split by an escape, escape sequences and an environment
  value runs through the engine, and none of them reaches a progress line,
  `progress.log` or any file under the state directory; a terminal that
  fails is dropped without changing the outcome
- the step directory published before the launch, the bounded
  `progress.log` and its omitted-lines note, a `progress.log` replaced
  while the agent ran receiving nothing, and the sequence counting a step
  whose launch never returned
- the CLI: each outcome printed as its step ends, progress on stderr only,
  nothing on a dry run

### Provider parity (Pi, #133)

Pi is held to what the scripted provider is held to, at the engine
boundary: the real `PiProvider` and the real duplex child run against the
fake `pi` in `tests/pi_fake.py` (`PiFake`, `ScriptedPi`), whose agent work
is scripted with the same handlers `ScriptedProvider` takes. Keep covered:

- REMOTE routing with every profile on Pi through review rounds 1, 2-5 and
  6+, MERGE (gate open in the test config only) and UPDATE_EPIC: each RPC
  child's `--model` / `--thinking` is that of the routed profile, its cwd
  is the per-issue worktree; and the mixed configurations (Claude Code
  writers with Pi reviewers, and the reverse)
- REPLAN_REEXECUTE on Pi: the same transaction, controller-published
  replacement, markers, close and activation; a `head_sha` the worktree's
  `HEAD` does not back is refused identically
- a Pi claim the fake GitHub does not back fails with the same error and
  state as the same claim from `ScriptedProvider`
- LOCAL on Pi: cwd is the repository root, no GitHub access, LOCAL prompts
  over RPC, every write-capable launch (corrections included) charged to
  the durable checkpoint, and a Pi process writing into the tree during
  REVIEW refused by the existing drift check
- a correction is a second, separate Pi process with identical argv and no
  session reuse, its RPC prompt carries the parse error, the entry
  reconciliation runs before it, and `max_correction_attempts` bounds it
- failures leave state unchanged and `resume` relaunches: the deadline
  (`ExecutionTimeoutError`, with the leftover sentence), the credential
  preflight, a rejected prompt, `stopReason: error`, an exit before
  `agent_settled` and a model mismatch (`ExecutionError`, stdout and stderr
  recorded)
- an `agent_end` past the real record bound (#144) is no failure: the
  phase advances and `execution.json` counts it. Below the engine
  (`tests/test_pi_rpc.py`), an oversize `agent_end` or `turn_end` on the
  fake `pi` is tolerated with the framer holding no more than the bound,
  while an oversize answer stays a protocol violation, and so, in the
  reducer, does every other head (another event, another serialisation,
  a head cut short); the duplex handle's `Oversize` carries exactly the
  record's head, across reads and never past the bound, and the head
  counts against the pending bytes (`tests/test_executor_duplex.py`)
- run logs: argv without the prompt and the env allow-list (Pi's names, no
  `OPENAI_*`) in `request.json`, the Pi summary in `execution.json`, only
  the final text in `stdout.log`, stderr separate, and a credential-shaped
  string redacted in every file
- dry-run with Pi profiles prints the Pi argv and starts nothing

### The live Pi smoke test (#135)

`tests/test_live_pi.py` runs one LOCAL REVIEW through the operator's real
Pi and credential, on the auth route `AUTOFORGE_LIVE_PI_AUTH` names:
`api_key` (DeepSeek first, `require_oauth: false`) or `oauth` (the ChatGPT
sign-in, optional). It is marked `live`: `pyproject.toml`'s
`-m 'not live'` deselects it from every default run, and selected with
`-m live` it skips unless `AUTOFORGE_LIVE_PI=1`. Its flow and checks live
in `tests/live_pi.py`; `tests/test_live_pi_support.py` keeps covered,
offline:

- the deselection: a default pytest process deselects it and runs nothing,
  and `-m live` without the variable skips with its reason
- the settings: the route is never defaulted, each route has its default
  model, and only the `api_key` route forwards a key, named like one
- the credential scan finds each pattern (JWT, `Bearer`, `refresh_token`,
  `"refresh"`, `"access"`, `chatgpt-account-id`, `chatgpt_account_id`,
  `sk-` keys, a DeepSeek-shaped key among them, which redaction masks) in
  synthetic data, also escaped inside a JSON string, and a forwarded key's
  value whatever its shape, and reports the file and the pattern name,
  never the match; prose and symbolic links are not hits
- a Pi failure is reported by its category as the provider's refusal or
  AutoForge's failure, never with its text
- the session-directory, tree and process checks
- the whole flow against the fake `pi`, through the real
  `autoforge local doctor` in a process of its own and the real engine and
  `PiProvider`: it passes on the `api_key` route (no key in any launch,
  two read-only auth checks, the report naming the commit, the route and
  the `authType`, nothing in it redacted) and on the `oauth` route; a key
  forwarded on purpose reaches the launch, is named and never shown, and
  its value is found where AutoForge wrote it; on the `api_key` route an
  auth check that is not ready, names another provider, is unreadable, or
  is `invalid` for a model Pi does not resolve fails step 1 and doctor and
  starts no review; a credential of the other route's type fails either
  route; a review answer without a valid `CONTROL_RESULT` fails step 4 as
  AutoForge's; a usage limit or a model error is named by its category
  while the later checks still run; a reviewer that writes and a new
  session entry each fail; an `OPENAI_API_KEY` or `DEEPSEEK_API_KEY` fails
  step 2 on either route, is not unset and starts no Pi; a forward of an
  unset key fails step 2; a missing `pi` fails the preconditions
- `tests/test_doctor.py`: the `api_key` route's OK auth row reads the same
  after the redaction `autoforge doctor` prints it through

### SDK policy (#149, docs/sdk-policy.md)

The SDK providers cannot launch yet, so their policy is held at its own
boundary (`tests/test_sdk_policy.py`). Keep covered:

- every option outside its closed set is a `ConfigurationError` naming the
  key and never quoting an `extra_args` value. That includes the SDKs' own
  names (`permission_mode`, `setting_sources`, `settings`, `mcp_servers`,
  `plugins`, `extra_args`), `pause`, an `ask_tools` without `prompt`, and
  a `credential_env` that is not a `*_API_KEY` name;
- the inventories per profile, and the PreToolUse disposition. Every
  unlisted tool, every `mcp__*` name and every unknown built-in is `deny`;
- the start fields close every escape hatch: no setting source, strict MCP
  with no server, no plugin, no `extra_args`, the fixed settings document,
  permission mode `default`, and no session approval. The digest moves
  with the policy and never with a root;
- the `system/init` check names each departure without quoting a value;
- authentication: `claude-sdk` without `ANTHROPIC_API_KEY`, or with a
  login token or a cloud route, is refused by name only;
- the launch allow-list. A real subprocess started with synthetic secrets
  for other providers in the controller's environment receives none of
  them, gets its own credential, and leaves `os.environ` unchanged. A `*`
  prefix is expanded and never carries a foreign name, and #165's
  refusals still apply;
- the roots: writing every root leaves a LOCAL fingerprint unchanged, and
  REMOTE roots lie outside the worktree;
- interaction data: an oversized, escape-carrying or credential-shaped
  input, option, answer or modified input is refused, never clipped. Two
  options shown alike are refused, and a permission ask shows only
  allow-listed keys;
- every launch path refuses (`integration_preflight`, `build_command_for`,
  `execute`), and `doctor` fails the "can launch" row;
- the interaction wait is below both agent limits under `prompt`.

The opt-in probes (`AUTOFORGE_CLAUDE_SDK_PROBE_CLI`,
`AUTOFORGE_OPENCODE_SDK_PROBE_SERVER`) run an installed runtime under the
policy against sentinel hooks, MCP servers, skills, agents and instruction
files, and assert that none loads. A control run without the policy
asserts that the probe can see them load. Re-run both whenever a pin, a
policy field or a runtime changes.

### Interaction contract and broker (#134, ADR 0007)

No adapter can ask yet (every one declares no capability), so the contract,
the broker, the terminal resolver and the engine's handling are held at
their own boundaries (`tests/test_interaction.py`). Keep covered:

- the contract refuses what it cannot render or bind (an oversized
  question, an escape-carrying or credential-shaped label, identity or
  answer, two options or questions shown alike, offers a request cannot
  take), naming the rule and never the value. Shown fields are cleaned,
  redacted and clipped;
- `decision_problem`: a decision outside the request's offers or its
  options, two picks for a single choice, or custom text where none is
  allowed is never delivered;
- the broker's table: `fail` never asks. `prompt` asks until the earlier of
  the wait limit and the invocation's limit, delivers only an offered and
  fitting decision, and fails on no terminal, end of input, expiry, a late
  decision, a resolver error, no live capability or the aggregate limit.
  An expiry the invocation's limit caused is never a pause, a pause needs
  both `pause_available` and the kind's capability, a request is handled
  once and only as it was minted, and Ctrl-C propagates;
- the run-log record keeps no answer text, guidance or modified input, and
  no more than `max_interactions` entries and the first one past them, however
  often a provider asks after its interactions ended;
- a modified input is a read-only snapshot of what was validated: changing
  the caller's object, or what was sent, afterwards never changes what an
  adapter sends;
- one Claude-shaped and one OpenCode-shaped question payload become equal
  requests with one rendering, and the answer maps back to each shape;
- `interaction.py` imports only the standard library, the errors and the
  interaction-data bounds, and names no provider or SDK;
- the terminal resolver, through a pipe, and a real pseudo-terminal for
  type-ahead: only offered choices are shown, bad input (an option number
  that is not a few ASCII digits included) is asked again, and text it
  could not send back exactly is refused;
- the terminal resolver's own line editing at a real pseudo-terminal:
  canonical mode and echo are off while it reads (Linux keeps 4095 bytes
  of a canonical line and drops the rest silently), so an answer or
  guidance of up to `MAX_ANSWER_CHARS` characters of any width arrives
  whole, a line past a bound is refused whole, the terminal's erase,
  word-erase, kill and end-of-file keys work, the echo goes to the
  terminal and never to stderr, and the settings come back on an answer,
  an expiry, an exception and a real Ctrl-C at the controlling terminal;
- no terminal write holds the resolver past its deadline: output flow
  control is off while it reads, so the stop and start keys are never part
  of a line and suspend nothing (also when IXON comes back on mid-wait),
  and output a stop key suspended before it asked comes back. Output
  another program suspends (`tcflow`), on the terminal it reads or another
  one it writes to, holds neither the echo nor its messages: an answer is
  delivered at once, without one the wait ends at its deadline, and past
  `_MAX_UNSHOWN_BYTES` not taken it reads no more keys. The stop-key and
  `tcflow` cases run in a child process whose stdin and stderr are one
  real pseudo-terminal, as in the CLI;
- no write to a redirected stderr holds the resolver past its deadline:
  an output that is not a terminal is never written to, and the request
  is shown on the terminal it reads instead (also with no stderr at all,
  never on stdout; nothing is asked when there is no terminal to show it
  on). In a child process whose stdin is a
  real pseudo-terminal and whose stderr is a full pipe no one reads, an
  answer is delivered at once, a line it cannot use is explained on the
  terminal, without an answer the wait ends at its deadline, nothing
  reaches the pipe, and the terminal's settings come back;
- the engine: `InteractionRequiredError` with the phase unchanged, one
  launch despite corrections and a transient report, a resolver asked
  under the repository lock, a broker per launch, a claimed pause with
  nothing persisted, a LOCAL launch charge kept, and a dry run that asks
  no one.
