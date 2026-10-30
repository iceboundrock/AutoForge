# ADR 0002. Executor: nothing an agent starts outlives its invocation

- **Status:** accepted, implemented for #85; amended by #132 (§4b), #193 (§4c),
  #194 (§4d) and #167 (§4e)
- **Decides:** #85 (follow-up of PR #84's review and #53)
- **Where:** `src/autoforge/executor.py` (`execute()`, `_terminate_group()`,
  `_Containment`), `src/autoforge/executor_duplex.py`,
  `src/autoforge/sandbox_bwrap.py` (§4e),
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
  dies is signalled when it appears. Dead orphans are reaped as they die,
  while the child runs, not only at teardown (below).
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

**Reaping while the child runs.** Being a subreaper makes the controller
the parent of everything the invocation orphans, for as long as the
invocation lasts, and a dead child that nobody waits for stays a zombie.
A zombie holds its pid, and pids count against the session's `pids.max`
(systemd's `TasksMax`; 9332 on the machine where this was found). An
agent's tools orphan short-lived processes all along: one run of this
repository's test suite under an agent orphans about 1,750. When they were
reaped only at teardown, five suite runs inside one hour-long agent
invocation filled the limit. From then on no fork or thread creation in
the operator's whole session succeeded, and the agent (Claude Code)
aborted on SIGABRT with no output: `agent 'analyze_execute' exited -6`.

So the containment starts a reaper thread that, every
`_REAP_INTERVAL_SECONDS` (0.25 s), reaps the controller's new zombies:

- It never reaps the child. The child's exit status belongs to its `Popen`,
  which would otherwise report 0.
- It never reaps a child the controller had before the invocation; that
  child is left to its owner.
- It reaps nothing until the child's pid is known.
- The first look at the orphans after the child's run (the exit grace, the
  kill) stops and joins it. From then on only the thread that signals the
  orphans reaps them, so an orphan cannot be reaped, and its pid reused,
  between being listed and being signalled.
- A reaper the system refuses to start fails the launch as `ExecutionError`
  before anything is spawned.
- From the spawn until the caller holds the result or the handle, every
  step that can fail runs under one guard. Whatever fails or interrupts it
  kills the group, releases the containment, which stops the reaper, and
  closes every pipe taken, before the launch fails. A reader opens its
  wake pipe only as it starts, once the guard holds it, so a reader that
  fails while it is built (its buffer cannot be allocated) has taken
  nothing to close. A setup step the system refuses (a reader thread, a
  reader's pipe) fails it as `ExecutionError`; an allocation failure or
  Ctrl-C, including one while the duplex handle or a reader is being
  built, is raised as itself. This holds for `execute()` and the duplex
  handle alike, so the reaper never outlives the invocation.

**Limits.**

- **Linux only.** Elsewhere a contained request runs uncontained and the
  result carries `orphans_unchecked`. The prompt rule of §3 is then the
  only mitigation. #135 gates unattended Pi use on this.
- **One contained invocation per controller process.** Orphans are told
  apart from the controller's other children by being new, so a second
  contained invocation while one runs raises `ExecutionError` instead of
  waiting. The engine runs invocations one after another, so this does not
  happen in practice. The reaper applies the same rule for the whole
  invocation. If the controller spawned a process some other way while a
  contained invocation runs, that process would be reaped as an orphan once
  it died, and its owner would lose its exit status. Nothing does this:
  every subprocess goes through the executor, and nothing else spawns while
  a contained child runs.
- **What still escapes:** a descendant that makes itself a subreaper (its
  orphans go to it, not to the controller), and anything handed to a
  service manager or another session's process (`systemd-run`, `at`, a
  container runtime, an SSH host).
- An orphan that survives SIGKILL stays the controller's unreaped child
  until the controller exits. It is reported (`orphan_survived_kill`), not
  hidden.

## 4c. Amendment: an idle timeout instead of one wall-clock deadline (#193)

The decision above is unchanged; what bounds the child's exit is not. An
agent invocation used to be bounded by one wall-clock `timeout_seconds`
sized for the slowest legitimate task, so a stuck agent ran for the whole
budget and a long productive one was killed with its work uncommitted
(#159, #160). An agent invocation now has two limits:

- **Idle timeout.** The child is killed once it has written nothing to
  stdout or stderr for `idle_timeout_seconds`, counted from the spawn and
  then from the last chunk either reader saw. The reader threads already
  see every chunk; each records the monotonic time of its chunk before
  handing it on.
- **Maximum runtime.** An optional wall-clock ceiling, counted from the
  spawn. Unset, it is `MAX_DEADLINE_SECONDS` (one week), the backstop that
  bounds every invocation.

Whichever is due first fires; if both are due at once, the ceiling is
reported. `execute()` and the duplex handle wait in bounded slices and
re-read that deadline after each one. When it has passed, the group is
killed through the same `_terminate_group`, so containment, orphan
reaping, the exit grace, the kill graces and the three leftover facts are
exactly those of the old timeout path. A child that has exited is looked
at before the deadline, as before, so a child that exits as a limit falls
due is not a timeout.

**The bound.** `execute()` now returns within the idle timeout after the
child's last output, or within `max_runtime_seconds` of the spawn when that
is due first, plus the exit grace plus two kill graces. Without a ceiling
the outer bound is the one-week backstop. That is deliberate: an agent that
keeps producing output is working, or looping; the idle timeout does not
tell those apart, and #194 is the issue that will (§4d).

**The handle.** The duplex handle applies the same two limits to every
`send_line`, `read_line` and `finish`. Activity means a chunk read from the
child, never a record the controller sent. An operation that must finish
inside a fixed budget, such as Pi's `abort` and the finish that follows it,
first pins the deadline: from then on, output no longer moves it, so a
child cannot stretch its own shutdown by writing. A caller that winds the
child down in order once a limit falls due asks for a wind-down
(`wind_down_seconds`): the limit still falls due on time, and the kill
comes that much later. Pi asks for its abort window, so its `abort` follows
the limit and never shortens it.

**Human waits.** #147 counts a wait for a human answer inside the
executor's deadline and never silently extends a duplex deadline. Activity
here is output from the child and nothing else, so a future human wait
(#126) is not activity and does not reset the idle timer. The two rules
agree: a wait neither stops the clocks nor resets them, and an interaction
design that needs a longer wait must say so explicitly.

**Policy stays out of the executor.** The executor takes the two values
and reports which one fired (`timeout_limit`: `idle` or `max_runtime`) and
the wall-clock time of the last activity (`last_activity_at`). Which values
a profile gets, and that a provider unable to report activity (Claude with
`output_format: text`) runs under its ceiling alone and must have one, is
configuration (`AutoForgeConfig.agent_limits`), and the provider says only
whether its output is a progress signal (`reports_activity`). Commands that
are not agents (`merge.verification_commands`,
`local.validation_commands`, the controller's `git`) keep one wall-clock
limit, `execution.command_timeout_seconds`.

Tests for §4c: `tests/test_executor.py` and `tests/test_executor_duplex.py`
run a child that writes on stdout, or on stderr, for longer than the idle
timeout (not killed), a silent child (killed at the idle timeout, the whole
group with it, `last_activity_at` unset), a child that goes silent after
writing (killed one idle timeout after its last output), and an active
child under a ceiling (killed at `max_runtime`); `_Limits` is also driven
by a fake clock (first due wins, a tie goes to the ceiling, a pinned
deadline stays put). `tests/test_providers.py` and `tests/test_pi_rpc.py`
carry the same cases through the Claude stream and a fake Pi.
`tests/test_engine.py` checks the error text and `execution.json` for each
limit and that a text-mode Claude profile without a ceiling is never
launched.

## 4d. Amendment: the caller may stop the invocation (#194)

The decision above is unchanged, and so is every bound. An agent that keeps
producing output while it repeats itself resets §4c's idle timer forever,
so without a ceiling it runs to the one-week backstop. #194 adds a
controller-side loop detector (`src/autoforge/loop_detect.py`); this
amendment records only what the executor gives it.

- **A stop, set by the caller.** `ExecutionRequest.stop` is a
  `threading.Event` the caller may set from any thread; `execute()` waits
  in slices of at most `_STOP_POLL_SECONDS` (0.1 s) while one is given,
  and once it sees the event set while the child is still running it kills
  the group through the same `_terminate_group`. The duplex handle has the
  same as a method, `DuplexChild.stop()`. Containment, orphan reaping, the
  exit grace, the kill graces and the three leftover facts are exactly
  those of the timeout path. A child that has exited before the stop is
  seen is not stopped: `stop()` returns False and changes nothing, and the
  result is that of its own exit.
- **What the caller sees.** `ExecutionRequest.on_chunk(stream, bytes)` is
  handed every chunk either reader reads, after the chunk is captured, so a
  detector of repeated lines sees the bytes; like `on_output`, a hook that
  raises is unhooked and the drain goes on. The result says `stopped`
  (exit status -1), never `timed_out`: why the caller stopped the child is
  the caller's.

**The bound.** A stop only ends the invocation sooner. `execute()` returns
within `_STOP_POLL_SECONDS` of the stop plus the two kill graces, and
within §4c's bound when nobody stops it.

**Policy stays out of the executor.** The executor never sets the stop and
knows nothing of loops. The detector is pure and provider-neutral: the
provider adapters feed it the agent's completed tool calls as opaque
fingerprints (`claude_stream.py`, `pi_rpc.py`) and their retries, and
`on_chunk` feeds it output lines. The adapter that stops a child reports
the outcome as a timeout (`timed_out`, `timeout_limit` `loop`), so the
engine's existing timeout path handles it: state unchanged, a typed
`ExecutionTimeoutError`, `resume` relaunches the phase. A Pi conversation
stopped for a loop is not sent `abort` first: the run is discarded as on a
timeout, and the abort window exists for an orderly wind-down that a run
being discarded does not need. Whether to stop at all is configuration
(`execution.loop_detection.mode`: `kill` (the default since #199, except
on a Pi profile and in `UPDATE_EPIC`, which warn until real runs have
measured them), `warn` or `off`).

**A second reason (#394, 2026-10-30).** A failed turn does not end
`claude -p`: a background task still pending keeps the CLI alive for another
turn, and its records keep §4c's idle limit away, so a run whose outcome the
stream has already decided would otherwise run on to its ceiling. The Claude
adapter stops a CLI still running `CLAUDE_FAILED_EXIT_GRACE_SECONDS` (10 s)
after its stream failed, through the same `DuplexChild.stop()`, and reports
the stream's failure, not a timeout. The executor is unchanged and still
knows nothing of why.

Tests for §4d: `tests/test_executor.py` stops a child that keeps writing
from its `on_chunk` hook (the result is `stopped`, not `timed_out`) and
unhooks a raising `on_chunk` without unhooking `on_output`;
`tests/test_executor_duplex.py` stops a running child and finds a stop
after the child's own exit a no-op. `tests/test_providers.py`,
`tests/test_pi_rpc.py` and `tests/test_loop_detect.py` carry a loop kill
through the Claude stream, a text profile's output lines, a fake Pi and the
engine.

## 4e. Amendment: sandboxed launches (#167, 2026-10-12)

The decision above is unchanged for the outer process group, and so is
every bound. ADR 0005 adds an optional outer sandbox to both requests
(`ExecutionRequest.sandbox`, `DuplexRequest.sandbox`): the Linux backend,
`bwrap`, is spawned in the command's place, and it runs the command in
user, mount, PID and IPC namespaces of its own. A request without one is
unchanged. For one with a sandbox, this amendment records what changes
(ADR 0005 D10.1 to D10.8).

- **The outer process is the child.** `bwrap` is spawned exactly as any
  command is: `start_new_session`, the same pipes, limits, stop and
  containment, and `_terminate_group` for the group. `--new-session` is
  never passed, so the command stays in the group the executor signals.
  There is no new process owner (D10.2).
- **Teardown inside is the kernel's, and it has no grace (D10.3, D10.5).**
  When the command exits, the kernel SIGKILLs everything left in its PID
  namespace at once, a `setsid` daemon included; `bwrap` returns the
  command's status afterwards. On a timeout or a stop, the group's SIGTERM
  reaches the outer `bwrap` too, which exits on it, and the namespace goes
  the same way. Inner processes therefore get neither the exit grace nor a
  kill grace; the outer group still gets both. The duplex handle still
  closes stdin and waits for the command to exit before any signal (§4a),
  so a command that shuts down on end of input still does so cleanly.
- **Inner leftovers are not counted (D10.4).** There is no moment at which
  the controller could count them: by the time `bwrap` returns, the kernel
  has removed them. Every enforced launch's result carries
  `SandboxFacts.teardown` ("removed by namespace teardown, uncounted"),
  and `describe_leftovers` says so (`sandbox_teardown`). It never reports
  them as none. `descendants_killed`, `group_survived_kill`,
  `capture_abandoned` and the orphan facts keep their meaning for the
  outer group and the controller's own children.
- **Containment adopts the backend's dead.** With `contain_orphans`, the
  controller is a subreaper, so when the outer `bwrap` is killed, its
  process inside the namespace is re-parented to the controller and dies at
  once, a zombie still in the group. For a sandboxed launch only
  (`_Containment.adopts_group`), the group check reaps it while it polls.
  Otherwise it would wait out a grace for a member that is already dead.
  Unsandboxed invocations are unchanged. One contained invocation per
  controller process still holds. Orphans inside the namespace never reach
  the subreaper: the namespace's own init reaps them.
- **Controller death kills the sandbox (D10.6).** `--die-with-parent` is
  `PR_SET_PDEATHSIG`, which fires when the *thread* that spawned `bwrap`
  exits. A sandboxed launch is therefore spawned only from a thread that
  outlives it: the thread that calls `execute()` and waits, or that calls
  `start_duplex()` and holds the handle until `finish()`. This is new: an
  unsandboxed agent outlives a controller killed with SIGKILL.
- **A setup failure is a launch refusal (D10.8).** A `bwrap` that exited by
  itself with no `exit-code` record on its `--json-status-fd` never ran
  the command. Examples: a bind source gone, an executable the sandbox
  cannot run, a namespace refused. It is
  `SandboxLaunchRefusedError` (an `ExecutionError`), raised after the
  teardown, so nothing is left. Its reason is `bwrap`'s message, bounded
  and redacted. A `bwrap` ended by a signal the executor did not send is
  the command's abnormal end, as for any command.

**The bound.** Unchanged: the deadline plus the exit grace plus two kill
graces. In practice a sandboxed timeout or stop ends within one poll of
the SIGTERM, since nothing inside can ignore it for long.

Tests for §4e: `tests/test_sandbox_bwrap.py` checks the following on the
real backend:

- a `setsid` daemon and a background child that ignores SIGTERM are gone
  after a normal exit, a timeout and a stop, with and without
  containment, well within one kill grace;
- a command that kills its own process group is an abnormal end;
- the duplex round trip, shutdown and deadline work through the backend;
- a duplex started from a thread that then exits dies with it.

`tests/test_sandbox.py` checks the wiring on a fake `bwrap`: a setup
failure is a refusal while exit 1 is not, the facts are recorded, and a
refusal releases the containment and every descriptor.
`tests/test_runlog.py` checks that the facts reach `execution.json` and
`events.jsonl`, redacted.

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
handle, including when the `with` block raises. Reaping while the child
runs, through both `execute()` and the handle: a contained child orphans
200 short-lived processes and keeps running. Their zombies are gone before
it exits, its own exit status is kept, and nothing is reported. A child the
controller already had keeps its exit status for its owner. A reaper that
cannot start refuses the launch before the spawn and restores the
subreaper setting. A reader thread, reader pipe or reader buffer that
cannot be set up after the spawn (through `execute()` or the handle), or a
failure or Ctrl-C while the handle is built, kills and reaps the child, and
leaves no reaper, thread, descriptor or held containment behind.
`tests/test_providers.py`: every agent launch asks for containment and
the facts cross the provider boundary. `tests/test_pi_rpc.py`: a process a
fake Pi's tool detached is killed after Pi exits, the result kept.
`tests/test_engine.py`: a pre-merge verification command's detached
process is killed and journaled and the merge proceeds; an agent's orphan
facts are journaled and named in a failed exit's error.
