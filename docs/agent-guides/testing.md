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
  abandoned capture; a clean exit and a clean kill report nothing
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

### Agent isolation (REMOTE)

- the agent's cwd is the per-issue worktree under the git common dir (or
  `execution.worktree_dir`), detached at the checkout's HEAD, without the
  operator's uncommitted work or `.autoforge/`; it is reused across phases
  as the agent left it
- a path that exists but is not a worktree root of this repository, or a
  location inside the operator's working tree, is refused, not adopted
- a dry run creates no worktree and runs no git
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

### Recovery

- external side effect completed before state write
- resume without duplicating work

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
