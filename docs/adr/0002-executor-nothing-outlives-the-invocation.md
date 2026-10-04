# ADR 0002. Executor: nothing an agent starts outlives its invocation

- **Status:** accepted, implemented for #85; amended by #132 (§4b)
- **Decides:** #85 (follow-up of PR #84's review and #53)
- **Where:** `src/autoforge/executor.py` (`execute()`, `_terminate_group()`,
  `_Containment`), `src/autoforge/executor_duplex.py`,
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

## 4a. Note: the duplex child handle (#130)

The duplex child handle (`src/autoforge/executor_duplex.py`, ADR 0003 §2.7)
is held to this decision unchanged. That clarifies the decision's scope; it
does not change the decision. An invocation through the handle is complete
when the child has exited, both pipes reached EOF and the group is empty.
Its one deadline plays the role of `timeout_seconds`. Closing the child's
stdin is the orderly-shutdown request: a child that exits after it has made
a normal exit and gets the exit grace, and its own status is kept. The same
`_terminate_group` does the kill, starting with SIGTERM. The same three
facts are reported for the same leftovers, and the same bound holds:
deadline plus exit grace plus two kill graces. When an exception (or
`KeyboardInterrupt`) leaves the `with` block, stdin is still closed first
and the child gets at most the exit grace to exit before the group is
killed. `tests/test_executor_duplex.py` runs the leftover scenarios of §5
through both `execute()` and the handle and asserts that the facts match.

## 4b. Amendment: processes that leave the group (#132)

**Problem.** §3 removes what is in the child's process group. A process
that called `setsid`, or was started detached, is not in it. If it does not
hold the child's pipes either, no check of §3 sees it: it survives the
invocation and nothing is reported. Before #132 that was an accepted gap
for a misbehaving command. Pi's `bash` tool makes it the normal case: every
tool command is started detached, so whatever a command backgrounds
(`server &`) outlives Pi's normal exit (`docs/pi-policy.md` §10).

**Decision.** Containment in the executor layer, provider-neutral and on
request (`ExecutionRequest.contain_orphans`, `DuplexRequest.contain_orphans`):

- For the invocation, the controller marks itself a Linux child subreaper
  (`prctl(PR_SET_CHILD_SUBREAPER)`, restored afterwards). A process whose
  parent dies is then re-parented to the controller rather than to init.
- The invocation's orphans are the controller's children that were not
  there when it began and are not the child itself. They are listed from
  `/proc/self/task/*/children` (a `/proc` scan where that file is missing),
  keyed by pid and start time. A child still in the child's group is left
  to the group checks. Unreaped children keep their pids, so signalling
  them cannot hit an unrelated process.
- An orphan counts as part of what must be gone, within §3's bounds: it gets
  the exit grace, then each signal of the kill (SIGTERM, then SIGKILL)
  together with its own process group, and one that appears as its parent
  dies is signalled when it appears. Dead orphans are reaped.
- Reported as three more facts, recorded with the others in
  `execution.json` and `events.jsonl` and rendered by `describe_leftovers`:
  `orphans_killed`, `orphan_survived_kill`, and `orphans_unchecked` (the
  platform has no subreaper). An orphan alone does not set
  `descendants_killed`, which stays a fact about the group. As in §3, none
  of them changes whether the child's result is accepted.
- **Who is contained.** Every agent launch (Claude Code, OpenCode, Pi and
  Pi's auth preflight) and every repository-defined command (validation,
  pre-merge verification). The controller's own `git`/`gh` plumbing is
  not, so a `git gc --auto` that git daemonized on purpose is left alone.

**Limits.**

- **Linux only.** Elsewhere a contained request runs uncontained and the
  result carries `orphans_unchecked`. The prompt rule of §3 is then the
  only mitigation. #135 gates unattended Pi use on this.
- **One contained invocation per controller process.** Orphans are told
  apart from the controller's other children by being new, so a second
  contained invocation while one runs raises `ExecutionError` instead of
  waiting. The engine runs invocations one after another, so this does not
  happen in practice.
- **What still escapes:** a descendant that makes itself a subreaper (its
  orphans go to it, not to the controller), and anything handed to a
  service manager or another session's process (`systemd-run`, `at`, a
  container runtime, an SSH host).
- An orphan that survives SIGKILL stays the controller's unreaped child
  until the controller exits. It is reported (`orphan_survived_kill`), not
  hidden.

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

#132 (§4b): `tests/test_executor.py` shows a detached orphan surviving an
uncontained invocation unreported; it is killed and reported after a
normal exit, as a `setsid` pipe writer (the capture then completes), as a
process backgrounded by a detached child, and on the timeout path with
SIGTERM escalating to SIGKILL. A clean contained run reports nothing and
leaves the controller's other children alone. A second contained
invocation at once is refused, and without a subreaper the limit is
reported. `tests/test_executor_duplex.py` does the same through the
handle, including when the `with` block raises.
`tests/test_providers.py`: every agent launch asks for containment and
the facts cross the provider boundary. `tests/test_pi_rpc.py`: a process a
fake Pi's tool detached is killed after Pi exits, the result kept.
`tests/test_engine.py`: a pre-merge verification command's detached
process is killed and journaled and the merge proceeds; an agent's orphan
facts are journaled and named in a failed exit's error.
