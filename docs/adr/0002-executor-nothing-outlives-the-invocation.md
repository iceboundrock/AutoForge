# ADR 0002. Executor: nothing an agent starts outlives its invocation

- **Status:** accepted, implemented for #85
- **Decides:** #85 (follow-up of PR #84's review and #53)
- **Where:** `src/autoforge/executor.py` (`execute()`, `_terminate_group()`),
  `docs/agent-guides/architecture.md` (executor section), the common prompt
  templates

## 1. Problem

Since #84 the executor treats an invocation as complete only when the child
has exited *and* both of its pipes reached EOF, bounded together by the
timeout, and kills the whole process group at the deadline. That restored
the pre-#53 `communicate(timeout=...)` contract and deliberately left two
policy questions open, because they change orchestration semantics rather
than restore them:

1. **A pipe-holding descendant cost the full timeout and discarded a good
   result.** The agent exits 0 with a valid `CONTROL_RESULT`, but a server it
   started with `&` inherited its stdout/stderr and keeps them open. The
   controller waited the remaining `timeout_seconds` (up to 30 minutes with
   the default), killed the group, and reported a timeout with
   `exit_code = -1`: the child's real exit code and its result were lost and
   the phase was treated as a timeout.
2. **A descendant that does not hold the pipes lingered after a normal
   exit.** The group was killed only on the timeout path, so a background
   process the agent redirected to `/dev/null` or a file survived the
   invocation and the next phase ran next to it: ports held, files written
   into the worktree, and with per-issue worktrees (#10) a leftover from
   issue N holding a port while issue N+1 runs.
3. **The run log could not say what happened.** It said the agent timed out
   although it had exited, and (addendum from PR #84's third round) it said
   the agent "was killed" while a member of the group may in fact have
   survived SIGKILL and still be running; `ExecutionResult` carried only
   `timed_out`.

## 2. Options

**A. Status quo, leave leftovers to the operator.** Deterministic in the
sense that the controller never kills on the normal-exit path, but an
unattended multi-issue run (the EPIC's Level 5) pays a full timeout per
phase for every leftover and cannot be trusted to find its own ports free.
The controller's own promise ("grandchildren do not linger") was only true
for the timeout case.

**B. Short post-exit grace, then kill the group and keep the child's result,
but only when the pipes are held.** Solves 1, not 2: a descendant with its
stdio redirected is exactly as much of a leftover and is the harder one to
notice.

**C. Kill the group at the end of every invocation, after a short grace,
and keep the child's own result.** Solves 1 and 2 with one rule. Forbids an
agent from deliberately leaving a service running for a later phase.

**D. A configuration knob under `execution:`** choosing between A and C.
Adds a setting whose non-default value re-creates the problems of A for
whoever sets it, for a use case (a service handed from one phase to the
next) the workflow does not have: each phase is an independent agent
invocation that must find its state on GitHub and in the worktree, not in a
process the previous agent left behind.

## 3. Decision

**C.** Nothing an agent starts outlives its invocation.

- An invocation is complete when the child has exited, both pipes reached
  EOF *and* no process is left in the child's process group.
- The child's exit is bounded by `timeout_seconds`; past it the group is
  killed and the result is a timeout, as before.
- Once the child has exited on its own, EOF and an empty group are given an
  exit grace (`_EXIT_GRACE_SECONDS`, 2 s). The common case, a child that
  left nothing behind, clears both at once and pays nothing; a helper the
  child is shutting down as it exits (an MCP server, a watcher) clears them
  within the grace and is neither killed nor reported. What is still there
  past the grace is killed with the group exactly as on the timeout path
  (SIGTERM, then SIGKILL, complete only when the group is empty, every wait
  bounded), the child's own exit status and output are returned, and
  `ExecutionResult.descendants_killed` records the kill.
- `timed_out` and `descendants_killed` are exclusive: the first means the
  child itself overran, the second that it exited and its leftovers were
  removed. A pipe-holding leftover after a clean exit is therefore no longer
  a timeout and no longer costs the timeout: the engine parses the child's
  `CONTROL_RESULT` normally.
- What the kill could not remove is reported, never presented as a clean
  kill: `group_survived_kill` (a member was still in the group after the
  SIGKILL grace: stuck in the kernel, or not signallable) and
  `capture_abandoned` (a pipe never reached EOF, so a writer that left the
  group with `setsid` still holds it). `ExecutionResult.leftovers` renders
  the three facts as one sentence.
- The executor reports; it decides nothing about the workflow. The engine
  records the three facts in `execution.json` and `events.jsonl` for every
  invocation (agent launches, validation commands, pre-merge verification
  commands) and appends the sentence to a timeout's or a failed exit's error,
  so `timed out after Ns and was killed` is followed by `(its process group
  still had a member after SIGKILL, ...)` when that is the case. None of the
  facts changes whether the child's result is accepted: a leftover is an
  operator matter, and GitHub remains the source of truth for what the agent
  did.
- The agents are told (`common.md`, `local_common.md`): nothing they start
  outlives their invocation; stop what you start, never rely on a background
  process for a later phase. The prompt version moves to `v3`.

## 4. Consequences

- A phase whose agent leaves a server behind now costs the exit grace plus
  at most two kill grace periods instead of the remaining timeout, and its
  result is kept.
- The next invocation starts with no process of the previous one holding a
  port or writing into the worktree, in REMOTE and LOCAL mode alike; the
  executor's docstring promise ("grandchildren do not linger") is now true
  on every path.
- An agent cannot hand a running service to a later phase. This is the
  intended constraint, not a limitation to configure around: a phase is
  verified from GitHub and the worktree, never from a process.
- The group id is checked with `killpg(pgid, 0)` after the child has been
  reaped. POSIX reserves the id while any member lives; once the group is
  empty the id is free for reuse, so only a pid wrap-around inside the exit
  grace could make the liveness probe name an unrelated new session leader.
  The timeout path has had the same window since #84; it is accepted as
  negligible on a single operator machine.
- Kill graces and the exit grace are module constants, not configuration:
  no legitimate helper needs longer than 2 s to follow its parent out, and
  a knob here would be option D by another name. Easy to expose later if an
  operator hits it.

## 5. Tests

`tests/test_executor.py`: a pipe-holding descendant after exit 0 and after
exit 3 (killed after the grace, the status kept, `descendants_killed`); a
helper that exits within the grace (nothing killed, nothing reported); a
`setsid` escapee after a normal exit (result kept, `capture_abandoned`, the
escapee left alive and killed by the test) and on the timeout path; a
closed-stdio, SIGTERM-ignoring descendant on the timeout path (unchanged)
and after a normal exit (killed, result kept); a direct child that survives
SIGKILL (`group_survived_kill`, `capture_abandoned`, still not waited for);
a clean exit and a clean kill report nothing; `describe_leftovers`.
`tests/test_providers.py`: the facts cross the provider boundary.
`tests/test_engine.py`: a pre-merge verification command that leaves a
server behind is killed and the merge proceeds; an agent whose leftovers
were killed advances the phase and is journaled; a timeout with a survivor
names it in the error, `execution.json`, `error.txt` and `events.jsonl`.
`tests/test_prompts.py`: both common templates carry the rule; the prompt
version moved past `v2`.
