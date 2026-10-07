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

### Execution

- success
- non-zero exit
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

### Agent isolation (REMOTE)

- the agent's cwd is the per-issue worktree under the git common dir (or
  `execution.worktree_dir`), detached at the checkout's HEAD, without the
  operator's uncommitted work or `.autoforge/`; it is reused across phases
  as the agent left it
- a path that exists but is not a worktree root of this repository, or a
  location inside the operator's working tree, is refused, not adopted
- a dry run creates no worktree and runs no git
- worktree creation, its identity and registry reads, and the workspace
  reader are hardened (ADR 0004 D7.3): a planted `post-checkout` or
  `reference-transaction` hook (in `hooks/` or a configured `core.hooksPath`)
  and a configured `core.fsmonitor` do not run, a filter driver sees no
  token and no operator `GIT_*`, and an operator `GIT_DIR` redirects neither
  the worktree, the workspace reads nor the lock
- the agent request and the run log carry the configured allow-list; pre-merge
  and validation commands run under the same one
- HEAD or branch of the operator's checkout changing while the agent ran
  enters BLOCKED (also when the invocation itself failed); agent commits in
  its own worktree are not drift

### GitHub verification

- valid PR
- missing PR
- HEAD mismatch
- comment verification
- the merge gate re-reading the clean review's comment (#94): a state
  parked in `READY_FOR_MERGE` / `MERGE` whose comment is gone, on another
  PR, names another round, HEAD, base or merge base, or says `needs_fix_round: true`
  blocks without a merge; the happy path reads it exactly once per pass
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

### Live progress (#192)

Progress is observability, never part of an outcome. Keep covered:

- the Claude `stream-json` reducer (`tests/test_claude_stream.py`): the
  `result` text verbatim, `is_error` checked before the subtype, a
  malformed line (including one the JSON decoder refuses for its integer or
  nesting limit, which must not escape as an exception), a missing or
  second result and an oversize result line each a `provider_failure`, an
  oversize ordinary line counted and skipped,
  a flat scalar summary, and no command, thinking, assistant text or tool
  result in any event
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
  second result, a malformed or unterminated line, an overflow) still fails
  the run
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
- REPLAN_REEXECUTE on Pi: the same transaction, markers, close and
  activation; a replacement PR the fake GitHub lacks is refused identically
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
- run logs: argv without the prompt and the env allow-list (Pi's names, no
  `OPENAI_*`) in `request.json`, the Pi summary in `execution.json`, only
  the final text in `stdout.log`, stderr separate, and a credential-shaped
  string redacted in every file
- dry-run with Pi profiles prints the Pi argv and starts nothing
