# Troubleshooting and recovery

What to do once a run exists and has stopped, failed or refused: find what
you see in the [symptom index](#symptom-index), then follow its section.
Anything before a run exists (installing, the configuration, `doctor`, the
credentials, the first dry run) is in
[Getting started: common first-run failures](getting-started.md#7-common-first-run-failures);
the commands and the phases are in [Running the remote workflow](usage.md).

Two rules hold for every section below:

- **Repeat the run's flags.** The CLI does not look for a configuration
  file, so every command of a run needs the `--config` and `--state-dir` it
  was started with, from the same directory. Every `resume:` and `unblock:`
  line AutoForge prints already carries them.
- **GitHub is the source of truth.** Look at the PR, its branch and its
  comments before you act. When the controller cannot tell what the safe
  next state is, it stops in `BLOCKED` rather than guess, and every way out
  of `BLOCKED` re-reads GitHub.

[Recovery scenarios](#recovery-scenarios) lists each scenario with the
test that holds its behaviour; [Known limitations](#known-limitations) says
what no test covers.

## Symptom index

| What you see | Section |
|---|---|
| `run <id> is BLOCKED: <reason>` (exit 1) | [BLOCKED: the controller will not decide alone](#blocked-the-controller-will-not-decide-alone) |
| `run <id> is FAILED: <reason>` (exit 1) | [BLOCKED, FAILED or "State unchanged"](#blocked-failed-or-state-unchanged) |
| `autoforge: error: agent '<profile>' ...`, then `State unchanged.` and `worktree:`, `logs:`, `resume:` lines (exit 1) | [State unchanged: an agent launch failed](#state-unchanged-an-agent-launch-failed) |
| `... made no progress for <N>s ... and was killed`, `... reached its <N>s maximum runtime and was killed`, `... was killed: the same ... repeated ...` | [An agent was killed: idle timeout, maximum runtime, loop](#an-agent-was-killed-idle-timeout-maximum-runtime-loop) |
| `failed: claude: the run ended in an error (...)`, `failed: pi: the auth preflight refused the launch: ...`, an `exited 1. stderr tail:` holding the CLI's own error, `(transient: ...)`, `not relaunched: ...` | [Provider authentication, model and API errors](#provider-authentication-model-and-api-errors) |
| `the agent asked for permission (...)`, `an answer to a question` | [An agent asked for permission or input](usage.md#an-agent-asked-for-permission-or-input) |
| `status` shows an `Acceptance action A-…` or an `Operator actions (FIX, ...)` block | [The run waits for you: acceptance and operator actions](#the-run-waits-for-you-acceptance-and-operator-actions) |
| a traceback ending in `KeyboardInterrupt`; a killed process, a closed terminal, a reboot | [The process was interrupted](#the-process-was-interrupted) |
| `run <id> is in terminal phase BLOCKED: ...` (from `resume`, exit 1), `cannot step from terminal phase BLOCKED` (from `step`, exit 2) | [resume, unblock or a new run](#resume-unblock-or-a-new-run) |
| `Automatic merge is disabled: MERGE requires BOTH config 'safety.allow_merge: true' AND the CLI flag '--allow-merge'.` (from `step`, exit 1) | [resume, unblock or a new run](#resume-unblock-or-a-new-run) |
| `no state file at <path> — run 'autoforge run --epic ... --issue ...' first; 'resume' never creates a new run silently` (exit 2) | [Find the run and read its state](#find-the-run-and-read-its-state) |
| `run ... was recorded under ..., and this command's configuration differs from it` (exit 2) | [The configuration a run was recorded under](usage.md#the-configuration-a-run-was-recorded-under) |
| `run ... is pinned to sandbox.mode ..., and this command's sandbox.mode, ..., contains its launches less` (exit 2) | [The outer sandbox](#the-outer-sandbox) |
| `existing run <id> in phase <P> — use 'resume' to continue or 'run --force' to discard it` (exit 2) | [Starting over](#starting-over) |
| `another AutoForge controller holds <path> — refusing to run concurrently on the same repository (...)` (exit 2) | [Another controller holds the lock](#another-controller-holds-the-lock) |
| `'<command>' refuses issue <issue>: ... Only the parallel EPIC scheduler continues an issue its namespace owns ...` (exit 2) | [Another controller holds the lock](#another-controller-holds-the-lock) |
| `unsupported protocol_version '<n>' (controller speaks '13'): a newer AutoForge wrote it` (exit 2) | [A run a newer controller wrote](usage.md#a-run-a-newer-controller-wrote) |
| `refusing to start a new run over an unreadable state file`, `corrupted state file ...` (exit 2) | [The state file](#the-state-file) |
| `PR <url> is CLOSED; the workflow only operates on OPEN PRs` (exit 1), `PR <url> has merge conflicts (mergeable=CONFLICTING)` | [When someone changed something on GitHub](#when-someone-changed-something-on-github) |
| the reason names the checkout the controller runs from (`checkout:`, `unblock:`, `resume:` lines) | [When someone changed something on GitHub](#when-someone-changed-something-on-github) |
| `refusing to launch: a process AutoForge launches would be handed a GitHub or git credential`, `refusing to launch opencode: ...` | [Credentials and the agent environment](#credentials-and-the-agent-environment) |
| `this host cannot enforce the outer sandbox`, `the configuration file ... lies in ...`, `the sandbox cannot be established ...`, `sandbox.read_only_paths[N] ... overlaps ...` | [The outer sandbox](#the-outer-sandbox) |
| `the agent worktree ... is not bound to the repository`, `the private object directory ... has been tampered with` | [The agent worktree and its objects](#the-agent-worktree-and-its-objects) |
| merge verification keeps saying "inconclusive" | [The merge gate](#the-merge-gate) |
| `status` shows a replan transaction (`REPLAN_REEXECUTE`, or `BLOCKED` with a journal) | [A replan](#a-replan) |

## Find the run and read its state

A run is the `state.json` in its state directory: `.autoforge/` in the
directory the command runs from, unless `--state-dir` or the
configuration's `state_dir` names another. `no state file at <path>` almost
always means a command without the run's `--state-dir` (or `--config`, when
that sets `state_dir`), or run from another directory.

- **`autoforge status`** prints the run as saved, and reads nothing from
  GitHub: the issue, PR and branch, the configuration the run is on
  (`Config:`), the `Phase:`, the review round, the reviewed HEAD and base,
  the open findings, `Reason:` (the block or failure reason), the last
  `Unblocked:` entry, any acceptance or operator actions with their next
  commands, the outer sandbox and its pin, and the validation evidence.
- **`autoforge status --json`** prints the same state as one JSON document,
  with derived fields beside the raw ones: `replan_journal`,
  `acceptance_actions`, `operator_actions`, `review_certificate_status` and
  `outer_sandbox`. Both outputs are redacted.
- **`<state dir>/logs/<run-id>/`** holds one directory per agent launch,
  `<seq>-<phase>-<attempt>/`, and `events.jsonl`, one line per launch and
  per operator action (an `unblock`, an accepted configuration change). In
  a launch's directory: `progress.log` (the live progress lines, written
  while the agent runs, so it survives a killed run), `request.json` (the
  profile, model, limits, environment names), `prompt.md`,
  `execution.json` (exit status, timing, `timeout_limit`, `ended_by`, what
  the launch left running, `provider_summary`), `stdout.log`, `stderr.log`,
  `error.txt` (the controller's error, when the launch failed) and
  `control-result.json` (the parsed result, when it was valid). The full
  layout is in
  [State and recovery: runtime artifacts](agent-guides/state-and-recovery.md#runtime-artifacts).
- **`<state dir>/read-context/<run-id>/`** is what the controller read from
  GitHub and handed each launch.
- **The agent worktree**, `<repo>/.git/autoforge/worktrees/<issue number>`
  (or under `execution.worktree_dir`), is where every agent of the issue
  worked: `git -C <worktree> status` and `git -C <worktree> log` show what
  the last one left.
- **Two dry runs preview without writing.** `resume --dry-run --max-steps 1`
  shows the next step's phase, profile, limits and worktree, and runs no
  `gh`; `unblock --dry-run --reason "..."` reads GitHub and says which phase
  `unblock` would re-enter, or why it would refuse.

Never edit `state.json`. A hand edit can make the controller misread a run
it then acts on, and nothing AutoForge suggests requires one.

## BLOCKED, FAILED or "State unchanged"

A stopped run is in one of three situations, and each allows something
different next:

| | `BLOCKED` | `FAILED` | "State unchanged" |
|---|---|---|---|
| What happened | the controller would not decide alone: a claim it could not verify, an object on GitHub it did not write, a bound reached, a check failing, an action waiting for you; or the agent reported `blocked` | the agent's own result reported `failure` | an agent launch ended without a usable result (a failed exit, a provider failure, a timeout, a loop kill, an agent that could not be started, a malformed result not corrected), or a GitHub read or a verification failed; the phase was not advanced |
| What you see | `run <id> is BLOCKED: <reason>`, exit 1; `status` shows `Phase: BLOCKED` and `Reason:` | `run <id> is FAILED: <reason>`, exit 1 | `autoforge: error: <what failed>`, exit 1; an agent failure adds `State unchanged.` and the `worktree:`, `logs:` and `resume:` lines |
| `resume` | refuses, exit 1, and runs nothing (`step` exits 2) | refuses, exit 1 | re-enters the same phase |
| The way out | fix the cause, then `unblock --reason "..."`, then `resume` | a new run ([Starting over](#starting-over)) | inspect, then `resume` |

`FAILED` has no outgoing edge, and `BLOCKED` is left only by `unblock`
([Workflow: leaving BLOCKED](agent-guides/workflow.md#leaving-blocked-the-operators-unblock)).

## BLOCKED: the controller will not decide alone

Read `Reason:` in `autoforge status`: it says what the controller found,
and usually what to do. The common causes:

| The reason names | What to do |
|---|---|
| a bound: `workflow.max_review_rounds`, a stagnation rule, `workflow.max_total_steps`, the replan limit | decide by hand whether the PR is worth continuing; to continue, raise the bound in the configuration (no `--accept-config-change` is needed for bounds) and `unblock`. `workflow.max_total_steps` counts every step of the run, failed launches included, and `resume` does not reset it |
| a PR, a branch push or a comment carrying a controller marker that the controller did not write | close the extra PR, delete the comment or its marker, or put the branch back, as the reason says, then `unblock` |
| a check that failed, is missing or ended with no success, at the merge gate | make CI report a success on the PR's head, then `unblock`, or merge by hand |
| the checkout the controller runs from moved | [When someone changed something on GitHub](#when-someone-changed-something-on-github) |
| `validation.commands` failed three times for the phase | fix the cause; when it was the environment (a missing tool, a full disk), `unblock` makes the next gate run the same commit again ([Candidate validation evidence](usage.md#candidate-validation-evidence)) |
| no applicable review certificate | `unblock`: it decides a fresh `REVIEW` round, or `READY_FOR_MERGE` once the certificate applies again ([Candidate validation evidence](usage.md#candidate-validation-evidence)) |
| an acceptance action or operator actions | [The run waits for you](#the-run-waits-for-you-acceptance-and-operator-actions) |
| a replan transaction | [A replan](#a-replan) |

The full list of what each phase blocks on, and how to leave each, is in
[Running the remote workflow: BLOCKED](usage.md#blocked-fix-the-cause-then-unblock).

`autoforge unblock --reason "<what you did>"` asks the controller to decide
again from live GitHub. It either re-enters the one phase that inspection
supports and exits 0, printing `continue with: <the resume command>`, or
refuses, leaves the run `BLOCKED` with nothing written and exits 1. No agent
runs until `resume`. The reason is recorded in the state and the run log,
and is never the evidence the decision rests on: a refusal means GitHub
still does not show what the controller needs. `unblock --dry-run` reports
the decision without writing anything.

## The run waits for you: acceptance and operator actions

A fixer that resolved a finding with text on an issue cannot post it. The
run blocks before anything of the `FIX` plan is published: the follow-up
issues and the push of the fixer's commit wait for the comment. The agent
worktree holds the unpushed commit, so do not reset or remove it.

- **An acceptance action** (`A-…`, a comment on the run's own issue that
  the controller posts once you approve it). Read the exact bytes with
  `autoforge status --action A-…`, then `unblock --approve-action A-…`
  (the next `resume` posts it and pushes), post exactly those bytes
  yourself and run a plain `unblock`, or `unblock --decline-action A-…`
  (the fixer runs again with your reason). See
  [BLOCKED on an acceptance action](usage.md#blocked-on-an-acceptance-action).
- **Operator actions** (`X-…`, comments on another issue, which the
  controller never posts). For each one, post it with
  `autoforge status --action X-… | gh issue comment <target> --body-file -`,
  bind a comment already there with `unblock --bind-evidence X-… <comment URL>`,
  or withdraw them with `unblock --decline-action X-…` (repeatable). Then
  `unblock` and `resume`. See
  [BLOCKED on operator actions](usage.md#blocked-on-operator-actions).

## State unchanged: an agent launch failed

Every agent failure that leaves the phase unchanged (a timeout or loop
kill, a failed exit or provider failure, an agent that could not be
started, a malformed result that is not corrected) ends the same way:

```text
autoforge: error: agent 'analyze_execute' made no progress for 1800s (last activity 08:31:02 UTC) and was killed. The limit is set by profiles.analyze_execute.idle_timeout_seconds
State unchanged.
  worktree:  /home/me/code/.worktree/159   (3 uncommitted paths)
  logs:      .autoforge/AutoForge-issue-159/logs/af-20261006T101500Z-3f2a9c/001-analyze_execute-1/
  resume:    autoforge --config autoforge.yaml --state-dir .autoforge/AutoForge-issue-159 resume
Inspect the real Git/GitHub state first; 'resume --dry-run --max-steps 1' with the same flags shows the profile, limits and worktree it will use.
```

- `worktree` is the issue's agent worktree, with the number of paths
  `git status` reports there as changed or untracked: what the attempt
  left behind. The count is left out when that read fails. AutoForge
  leaves those changes where they are, and the next launch of a phase
  that writes (`ANALYZE_EXECUTE`, `FIX`, `REPLAN_REEXECUTE`) is told
  about them: its prompt names the worktree's branch and `HEAD` and lists
  each change's status code and path (at most 50, never file content),
  and asks the agent to inspect them and then continue from them or
  discard them deliberately, saying which. The invocation's
  `request.json` records what was found (`metadata.worktree_state`), or
  why the read failed, in which case the prompt describes nothing.
- `logs` is the failed launch's step directory (its progress, output and
  error).
- `resume` repeats the `--config` and `--state-dir` this command was
  started with, as you typed them; run it from the same directory. A bare
  `resume` after a run started with them finds no run (no `--state-dir`)
  or would run on the built-in defaults (no `--config`): another model,
  other limits and another worktree. A run refuses that: see
  [the configuration a run was recorded under](usage.md#the-configuration-a-run-was-recorded-under).

What the controller already did before it gave up:

- **A malformed `CONTROL_RESULT`** from an agent that exited 0 is retried
  with a correction prompt (`execution.max_correction_attempts`, default
  once): the message says `did not return a valid CONTROL_RESULT after <N>
  attempt(s)` once that budget is spent. The controller reads the block
  only from the end of the agent's final reply, never from a command's
  output or a file, and the correction prompt quotes the end of the refused
  reply (its last 4,000 characters, redacted, fenced as untrusted data)
  after the controller's diagnosis, so the new session can see what the
  previous one said it had done; it still checks that against the
  worktree. The correction's `request.json` records the quote's sizes
  (`metadata.correction_reply`), its `prompt.md` the quote as sent. A
  `resume` after a crash or a spent correction budget quotes nothing.
- **No correction or relaunch** is launched when the refused launch may
  have left a process running: the error then says so
  (`Not re-invoked: a process the launch started may still be running`,
  followed by which one). Stop that process before `resume`.
- **A transient provider failure** is relaunched inside the step, at most
  twice ([Provider errors](#provider-authentication-model-and-api-errors)).
  Any other non-zero exit, a timeout or a verification failure is not
  retried automatically.

Then look at the worktree (the agent may have committed, or left changes)
and at the PR, read the step's `stderr.log` and `error.txt`, fix what
failed, and run the printed `resume`. It starts a fresh agent session with the same prompt, and the
re-entry reads GitHub first, so work that already landed is adopted, not
repeated ([Workflow: re-entering a phase](agent-guides/workflow.md#re-entering-a-phase)).
Each `resume` that launches counts one step toward
`workflow.max_total_steps`.

## An agent was killed: idle timeout, maximum runtime, loop

The error names the limit that fired and the configuration key that set
it: `... made no progress for 900s (last activity 08:31:02 UTC) and was
killed. The limit is set by execution.default_idle_timeout_seconds`, or
`... reached its 7200s maximum runtime and was killed. The limit is set by
profiles.analyze_execute.max_runtime_seconds`. The step's `execution.json`
says the same in `timeout_limit` (`idle`, `max_runtime` or `loop`) and
`ended_by`, with both configured values and `last_activity_at`.

- **An idle timeout** says that nothing reached the controller for that
  long, not why. Read
  [Reading an idle timeout](usage.md#reading-an-idle-timeout) before
  raising the limit: a test suite run in the background past the limit,
  a silent tool call and a dropped OpenCode connection each call for
  something different, and a higher limit also lets a hung agent hang
  longer.
- **The maximum runtime** (`max_runtime_seconds`, unset by default) is the
  only wall-clock bound on an agent that keeps producing output
  ([Agent limits](usage.md#agent-limits)).
- **A loop kill** (`was killed: the same 2-step action cycle (Bash, Read)
  repeated 8 times ...`) means the agent kept repeating the same actions
  with nothing new in between. Look at the worktree and the PR as after a
  timeout; a loop that comes back on every attempt usually means the task,
  or a flaky command the agent keeps re-running, needs a person. Raising
  the thresholds is the last resort ([Loop detection](usage.md#loop-detection)).

A limit is part of the configuration the run was recorded under. After you
raise it, `resume` refuses (exit 2) and names the change; repeat it with
`--accept-config-change` to continue under the new limit
([The configuration a run was recorded under](usage.md#the-configuration-a-run-was-recorded-under)).

## Provider authentication, model and API errors

An agent CLI that cannot authenticate, or a model it cannot use, fails the
launch like any other failed exit: the message, then `State unchanged.`
The CLI's own words are in the message's `stderr tail:` and in the step's
`stderr.log`.

| CLI | How it reads | What it usually means |
|---|---|---|
| Claude Code (`output_format: stream-json`, the default) | `agent '<profile>' failed: claude: the run ended in an error (subtype ..., terminal_reason api_error, api_error_status <status>): <the CLI's message>` | 400, 401, 403 or 404: the request, the credential or the model; 429: possibly a plan's usage limit, which resets hours later. 408 and 5xx (529 is overloaded) are transient, once the CLI's own retries are spent. A refused or dropped connection (`api_error: connection_failed`, which a wrong `ANTHROPIC_BASE_URL` gives too) has no status and is not transient |
| Claude Code (`output_format: text`) | `agent '<profile>' exited <N>. stderr tail: ...` | no stream to classify: read the stderr tail |
| OpenCode | `agent '<profile>' exited 1. stderr tail: ...`, OpenCode's `Error:` line among it | the credential, the model or the provider OpenCode was given. For an `openai/*` model, exits whose errors are all `WebSocket closed with code 1000`, `WebSocket closed with code 1008` or OpenAI's generic `server_error: An error occurred while processing your request. ...` are transient |
| Pi | `agent '<profile>' failed: pi: the auth preflight refused the launch: status <status>, provider '<provider>', reason <reason>: <remedy>` | Pi holds no usable sign-in for the model's provider; the reason is one of [`doctor`'s Pi rows](pi.md#doctors-pi-rows), with its remedy |
| Pi on an API key (`require_oauth: false`) | `pi: prompt rejected before acceptance`, `pi: model error after acceptance` | a missing or invalid key, or a model the key cannot use: no preflight runs on this route ([DeepSeek on an API key](pi.md#deepseek-on-an-api-key)) |

**Transient failures are relaunched inside the step.** A launch its adapter
reports as transient (today the OpenCode `openai/*` errors and the Claude
Code statuses above) is relaunched at most twice per step, each time after
re-reading GitHub. That includes an OpenCode session that recovered from
such an error and printed a result: OpenCode exits 1 once it has printed
the error, and a result from a failed exit is not read. A transient failure's `error.txt` says `(transient:
...)`, and when the step stops relaunching, the message says why (`not
relaunched: this step already relaunched it 2 time(s), the bound of 2`,
or that a process the launch started may still be running). Any other
failure, a 429 among them, is not relaunched: it waits for `resume`.

To recover: sign the CLI in, or fix the profile's model or provider in the
configuration, run `autoforge doctor` (it checks that each CLI is there,
and Pi's credential, without launching an agent), then `resume`. A changed
provider, model or effort is a change of the recorded configuration, so
that `resume` needs `--accept-config-change`.

## The process was interrupted

What happens depends on how the controller was stopped:

- **`Ctrl-C`** in the terminal running `run`, `step` or `resume`: the
  controller ends the agent's whole process group (SIGTERM, then SIGKILL)
  before it exits, and Python prints a traceback ending in
  `KeyboardInterrupt` (exit 130).
- **`kill` (SIGTERM), a closed terminal (SIGHUP) or `kill -9`**: the
  controller ends at once and does not stop the agent. Under the default
  `sandbox.mode: disabled` the agent runs in a session of its own, so it,
  or what it started (a test suite, a server), can keep running in the
  issue's worktree: it holds no GitHub credential, so it pushes nothing,
  but it can go on changing the worktree. Under an enforced outer sandbox
  (`sandbox.mode: required`) it dies with the controller. Look for it
  before you resume: an agent CLI process (`claude`, `opencode`, `pi`), or
  a child of one, whose working directory is the issue's worktree (on
  Linux, `ls -l /proc/<pid>/cwd`). Stop it, or let it finish.
- **A reboot** ends both.
- **A closed laptop** suspends both. What follows when it wakes is not
  verified ([Known limitations](#known-limitations)); if the run stopped,
  it stopped in one of the ways this guide describes, and recovers the
  same way.

In every case the lock is released by the kernel when the controller's
process ends, so there is no lock file to remove. Then run
`autoforge status` (with the run's flags) to see the phase, and `resume`.
The state is written atomically, a launch is recorded before the agent
starts, and every phase that launches an agent reads its journal and
GitHub first, so work already done (an open PR, a review comment the
controller posted, a pushed fix) is adopted or completed rather than
repeated
([Workflow: re-entering a phase](agent-guides/workflow.md#re-entering-a-phase)).
What the interrupted agent left uncommitted in the worktree is handed to
the next launch, as after a failure
([State unchanged](#state-unchanged-an-agent-launch-failed)).

Two cases resume in a particular way:

- **A validation command was running.** The next `step` or `resume`
  records it `INTERRUPTED` and runs it again when its phase does; the third
  interruption of one definition for the same candidate blocks the run.
- **A replan was running.** See [A replan](#a-replan).

## resume, unblock or a new run

| The run is | Run |
|---|---|
| in a working phase (`INITIALIZING`, `ANALYZE_EXECUTE`, `REVIEW`, `FIX`, `REPLAN_REEXECUTE`, `MERGE`, `UPDATE_EPIC`) | `resume` (or `step` for one step) |
| `READY_FOR_MERGE` | nothing: a human merges the PR. `resume` re-prints the banner and exits 0; `step` exits 1 with the merge-gate message. With `safety.allow_merge: true`, `resume --allow-merge` runs the controller's pre-merge verification ([READY_FOR_MERGE](usage.md#ready_for_merge-a-human-merges-unless-the-gate-is-open)) |
| `BLOCKED` | fix the cause, `unblock --reason "..."`, then `resume` |
| `FAILED` or `DONE` | a new run ([Starting over](#starting-over)) |

`--accept-config-change` is for `step`, `resume` and `unblock` when the
configuration differs from the one the run was recorded under: its profile
routing (provider, model, effort, limits), its agent worktree root, or a
weaker `sandbox.mode` than the run is pinned to. Without it the command
exits 2, names each change and writes nothing; with it the change is
written to the run log first and becomes the run's configuration. Bounds,
the merge gate, the environment allow-list and the rest may change without
it ([The configuration a run was recorded under](usage.md#the-configuration-a-run-was-recorded-under)).

### Starting over

`autoforge run --epic ... --issue ...` replaces a `BLOCKED`, `FAILED` or
`DONE` run without asking, except a `BLOCKED` or `FAILED` one that holds
operator actions, which needs `--force` (the comments stay on GitHub
either way). A run in any other phase needs `run --force`. Replacing a run
replaces `state.json`: its review history, its loop-bound counts, its
replan count and its validation records go with it, so read `status`
first. Its run logs and worktree stay.

The new run's `ANALYZE_EXECUTE` adopts the one open PR carrying the issue's
`ai-implementation` marker without running the implementer, and blocks on
two. Close the old PR first if you want a fresh implementation; leave it
open to continue it.

## Stopping a run safely

A standalone run has no stop command (`epic stop` is the scheduler's).
Press `Ctrl-C` in its terminal: the agent is stopped with the controller,
and the phase stays where it was for `resume`. To stop at a step boundary
instead, run it a step at a time (`step`, or `resume --max-steps N`).

Whatever stopped it, this is what is left, and AutoForge removes none of
it:

| What | Where | What becomes of it |
|---|---|---|
| the run | `<state dir>/state.json` | stays in its phase; `resume` continues it, a new `run` replaces it |
| logs and read context | `<state dir>/logs/`, `<state dir>/read-context/` | kept |
| the agent worktree | `<repo>/.git/autoforge/worktrees/<n>` | as the last agent left it, uncommitted work included; never deleted, moved or pruned: `git worktree remove` it once the issue is done |
| sandbox directories | `<repo>/.git/autoforge/sandbox/<n>/` (enforced launches only) | kept; remove them with the worktree |
| the branch and the PR | GitHub: `autoforge/<n>` and its PR, review comments, follow-up issues | stay open. The controller closes a PR only when a replan replaces it, and deletes a head branch only after merging it itself with `merge.delete_branch` |
| local branches | none | the controller creates none (agents commit on a detached `HEAD`; the controller pushes to the remote branch) and deletes none |

To abandon an issue: close its PR on GitHub (and delete its branch if you
like), remove its worktree, and start the next issue with `run` (`--force`
when the stopped run is in a working phase).

## Another controller holds the lock

`another AutoForge controller holds <repo>/.git/autoforge/controller.lock
— refusing to run concurrently on the same repository (an AutoForge
scheduler or its surviving workers may hold it)`, exit 2, nothing written.
One lock covers the whole checkout: every state directory, every
subdirectory and every linked worktree of it. (Two separate clones of one
repository are not coordinated.) It is held by:

- another `run`, `step`, `resume` or `unblock`, in any terminal;
- an `epic run`, `epic resume` or `epic archive`, for its whole life; or
- a scheduler's worker that survived its scheduler, until it exits.
  `epic status <EPIC URL>` lists the workers that hold their run lock.

Wait for it, or stop it (`Ctrl-C` in its terminal; `epic stop` for a
scheduler). Never delete the lock file: the kernel releases the lock when
the holder's process ends, `kill -9` included, and nothing ever needs
cleaning up. `controller.lock` holds the PID of the last controller that
took it, which is not cleared when it exits, so check that the PID is still
an `autoforge` process before acting on it.

An issue that belongs to a parallel EPIC scheduler (its namespace holds a
state or an open claim, or a live worker holds its run lock) is refused by
`run`, `step`, `resume` and `unblock`: `'<command>' refuses issue <issue>:
<why>. Only the parallel EPIC scheduler continues an issue its namespace
owns ('autoforge epic resume', inspected with 'autoforge epic status');
'autoforge epic archive <epic> <n>' releases it to a standalone run.
Nothing was written.` (exit 2).

Other lock messages:

- **`cannot derive the controller lock: <dir> is not inside a git
  repository`**: `run`, `step` and `resume` must run inside the checkout of
  the issues' repository.
- **A lock entry that is not a regular file with a single name**
  (`LockError`, exit 2): a link, a FIFO or a hard-linked file at
  `.git/autoforge/controller.lock`; the message says what to remove
  ([State and recovery: locking](agent-guides/state-and-recovery.md#locking)).
- **A shared git operation that waited too long** for
  `.git/autoforge/git.lock` (a fetch, the agent worktree's creation, the
  import of an agent's objects) is reported as that operation's failure;
  `resume` tries again.

## When someone changed something on GitHub

The controller verifies on GitHub before it advances, and blocks on what it
cannot account for. What happens when a person, or another tool, changed
something under a run:

| What changed | What the controller does | What to do |
|---|---|---|
| someone pushed to the PR branch | in `FIX`, and with the merge gate open in `READY_FOR_MERGE` or `MERGE`: back to `REVIEW` of the pushed head, not a block. In `ANALYZE_EXECUTE`: `BLOCKED` on a push it did not record | nothing; or inspect the branch, then `unblock` |
| the PR was retargeted, or its base rewritten | back to `REVIEW` | nothing |
| the PR was closed | a step that needs it refuses with `PR <url> is CLOSED; the workflow only operates on OPEN PRs` (exit 1), with the phase unchanged and nothing published; `unblock` of a `BLOCKED` run refuses: `the controller will not choose between reopening it and reimplementing the issue` | reopen it, then `resume` (or `unblock`); or [start over](#starting-over) |
| the PR was merged by hand | with the gate closed, the run stays `READY_FOR_MERGE`. With the gate open, `resume --allow-merge` counts a merge at the reviewed revision once, and blocks on one at another revision (`the controller never reviewed the merged change`) | with the gate closed, start the next issue with `run --force`; a block on an unreviewed merge is yours to inspect |
| the PR has merge conflicts | with the gate open: `BLOCKED`, `PR <url> has merge conflicts (mergeable=CONFLICTING)` | resolve them on the PR branch; `unblock` then decides `REVIEW` of the new head |
| a comment carrying a controller marker, or a second PR carrying the issue's marker, appeared | `BLOCKED`, naming it: the controller never adopts, edits or duplicates an object it did not write | delete the comment or its marker, or close the PR that is not the issue's, then `unblock` |
| an earlier version blocked on the controller's own PR, issue or comment whose text held a character like `\u001b` (#327) | the read-back now reads the exact text from GitHub | `unblock`; nothing is written again |
| the checkout the controller runs from moved while an agent ran (a commit, a reset, a branch switch) | `BLOCKED`, with the drift in the reason, nothing rolled back, and `checkout:`, `unblock:` and `resume:` lines carrying the run's flags | inspect the checkout and the agent's GitHub work, then run the printed `unblock`, then `resume` |

A run blocked because its checkout moved ends its reason with where to
look and what to run, already carrying the run's flags:

```text
  checkout:  /home/me/code/AutoForge
  unblock:   autoforge --config autoforge.yaml --state-dir .autoforge/AutoForge-issue-159 unblock --reason '<what you found>'
  resume:    autoforge --config autoforge.yaml --state-dir .autoforge/AutoForge-issue-159 resume
Inspect the checkout and the agent's GitHub work first: 'unblock' re-inspects GitHub and re-enters a phase, and only then does 'resume' continue the run.
```

The full catalogue of what each phase blocks on is in
[Running the remote workflow: BLOCKED](usage.md#blocked-fix-the-cause-then-unblock).

## Messages

The messages below are AutoForge's own refusals, grouped by subject.

### Credentials and the agent environment

- **"refusing to launch: a process AutoForge launches would be handed a
  GitHub or git credential":** the message names the setting (for example
  a credential helper in the repository's git configuration, a remote URL
  with a token, a GitHub entry in `~/.netrc`, or the startup files of a
  login shell that change a variable such as `GH_TOKEN`). Nothing was recorded:
  remove the setting (`autoforge doctor` lists it too) and run the same
  command again. A config that fails to load because `env_allowlist` names
  a variable `gh` or git reads a credential from: delete that entry
  ([Configuration: agent environment](configuration.md#agent-environment)).
- **"refusing to launch opencode: it would load the configuration of an
  organization connected to it":** OpenCode would load an organization's
  configuration, which may name an MCP server with a GitHub credential of
  its own, and nothing turns it off. Nothing was recorded: give AutoForge's
  OpenCode launches a database of their own (`OPENCODE_DB`, see
  [Configuration: agent environment](configuration.md#agent-environment))
  and run the same command again.

### The outer sandbox

- **"this host cannot enforce the outer sandbox: it cannot provide ..."**
  (`SandboxCapabilityError`, exit 2; `sandbox.mode: required` or
  `preferred`): the pre-launch gate refused the step. Nothing was launched,
  saved or charged. `autoforge doctor` shows the same capability row; make
  the host capable (install `bwrap`, allow unprivileged user namespaces, a
  kernel with Landlock ABI 6 that booted with Landlock enabled; the row's
  reason says which of the two a kernel lacks, see
  [Compatibility](compatibility.md#outer-sandbox)), or set
  `sandbox.mode: disabled` (logical isolation only), and run the same
  command again.
- **"the configuration file ... lies in ..., a tree that launches inside
  the outer sandbox write"** (exit 2; also "is reached through ...", "has
  N hard links" or "opens another file than its lookup walk was followed
  to"; "the sandbox declaration of profile ... cannot be read" when a
  provider's declaration could not be asked): under `required` or
  `preferred` the `--config` file is where an enforced launch could
  rewrite it, or is not shown not to be (#280). Nothing was run, launched or
  written. "Is reached through" names a directory of such a tree the path
  looks a name up in: a symbolic link leads there, or the path passes
  through it before a `..`, which is followed as the command opens it,
  never tidied. When the directory is a provider's (the message names
  `$CLAUDE_CONFIG_DIR`, `$PI_CODING_AGENT_DIR` or `$OPENCODE_DB` and the
  profiles whose launches write it), every enforced launch of that
  provider writes it, wherever it lies. Move the file outside the
  checkout, every worktree of the repository and every such directory,
  for example to `~/.config/autoforge/`, and pass its own path; or give
  the provider another directory; or set `sandbox.mode: disabled`.
  `doctor` reports the same in its config row and runs none of the
  commands the file names.
- **"run ... is pinned to sandbox.mode ..., and this command's
  sandbox.mode, ..., contains its launches less"** (exit 2): the command
  runs under a weaker mode than the run was created under, often because
  it was run without the run's `--config`. Nothing was written. Run it with
  a configuration that sets the pinned mode, or repeat it with
  `--accept-config-change` to continue under the weaker one (recorded in
  the run log).
- **"sandbox.read_only_paths[N] ... overlaps ..., which no launch may see":**
  an operator read-only path names, contains or lies inside the state
  directory, the lock, `<git common dir>/autoforge`, another worktree or
  your checkout. Nothing was launched; narrow or remove the entry.
- **"the sandbox cannot be established ..."** (`SandboxLaunchRefusedError`,
  the sandbox enforced): the launch did not run, and nothing ran
  without the sandbox. The message names the cause: a backend that failed
  to set the sandbox up after the gate passed, a sandbox directory that is a link, or a path and the
  policy rule it breaks ("sandbox policy refuses <path>: <rule>"), such as
  a `CLAUDE_CONFIG_DIR`, `OPENCODE_DB` directory or `PI_CODING_AGENT_DIR`
  inside the provider's own configuration directory
  ([Configuration: provider credentials](configuration.md#provider-credentials-under-required)).
  For an agent the phase is unchanged: fix the cause, or set
  `sandbox.mode: disabled`, and `resume`. A refused validation command
  leaves the phase unverified; a refused pre-merge command blocks the merge
  like a failing one. "the agent was not launched: the strict import ...
  failed" is the same refusal, in either mode, for objects an earlier
  enforced launch left in the issue's private object directory that the
  controller cannot import; inspect the directory before resuming.

### The agent worktree and its objects

- **"the agent worktree ... is not bound to the repository":** the
  worktree's `.git` file and the repository's worktree registry disagree
  (the worktree or the repository was moved, or something rewrote the
  pointer). The controller reads a worktree only through the registry entry
  that names it, so nothing was launched. Run `git worktree repair` in the
  repository (or `git worktree repair <worktree>`), then `resume`.
- **`BLOCKED`: "the private object directory ... has been tampered with":**
  an `info/alternates` file, a link or another entry no object store holds
  appeared in `<repo>/.git/autoforge/sandbox/<n>/objects`, or that
  directory, or one above it up to `<repo>/.git`, is not a real directory
  (a link, say) or cannot be checked. Nothing was read
  from it and nothing was published. Inspect it, remove what does not
  belong there, then `unblock` and `resume`. To look at an agent's commits
  while they are still only in that directory, read them through it
  without writing anything:
  `GIT_ALTERNATE_OBJECT_DIRECTORIES='"<repo>/.git/autoforge/sandbox/<n>/objects"' git -C <worktree> log`.
  The inner double quotes keep git from splitting a path that contains a
  `:`; inside them, write a `"` or a `\` of the path as `\"` or `\\`. The
  outer single quotes are the shell's, and end at a `'`: write a `'` of the
  path as `'\''` (close the quotes, an escaped `'`, reopen them), after
  git's escapes.

### The merge gate

- **Merge verification keeps saying "inconclusive":** checks still running,
  mergeability unknown or a transient GitHub failure; re-run
  `autoforge resume --allow-merge`. After `merge.max_verification_attempts`
  attempts the run is `BLOCKED`.
- **A conclusive refusal** (a failing or missing check, merge conflicts, a
  PR that changes `safety.protected_merge_paths`, a review certificate that
  does not apply, check results the credential cannot read) is `BLOCKED`
  with the reason, never merged and never re-checked by itself
  ([READY_FOR_MERGE](usage.md#ready_for_merge-a-human-merges-unless-the-gate-is-open)).

### The state file

- **The state file is unreadable:** `run`, `resume`, `step` and `status`
  exit 2 and never overwrite it. `run --force` moves it aside as
  `state.json.corrupt-<timestamp>` (never deletes it) and starts a new run;
  see [State and recovery](agent-guides/state-and-recovery.md#persistent-state).
  A file refused for its protocol label alone is not this entry's: see
  the next one.
- **"unsupported protocol_version '14' (controller speaks '13'): a newer
  AutoForge wrote it"** or **"... no AutoForge protocol has that label"**
  (exit 2), followed by an `AutoForge controller/state protocol mismatch`
  block: the command imported a controller older than the one that wrote
  the run, or the label is none a controller writes. The state file and
  its run logs were not rewritten. See
  [A run a newer controller wrote](usage.md#a-run-a-newer-controller-wrote).

### A replan

- **A replan was interrupted:** the phase is still `REPLAN_REEXECUTE`.
  `autoforge status` prints the transaction id, stage and both PRs, and
  `resume` continues the transaction from its persisted stage. Once the
  replacement's push and PR are journaled, `resume` completes them without
  running the agent again; before that it runs the agent again on the same
  base. A transient GitHub failure inside the transaction leaves it
  resumable the same way.
- **The run is `BLOCKED` with a replan transaction on record** (a rejected
  replacement or source, or a journal the controller could not read): this
  run is over. `resume` exits 1 as for any `BLOCKED` run, `unblock` refuses
  while the journal exists, and repairing the replacement PR does not reopen
  a rejection. `autoforge status` shows the block reason and the journal's
  stage and both PRs; a rejection's reason also says what the transaction
  did to the source PR. Inspect those PRs on GitHub and decide by hand
  which one, if either, carries the issue forward. To continue, start a new
  run; it replaces `state.json`, journal included, so read `status` first.
  Its `ANALYZE_EXECUTE` adopts the one open PR carrying the issue's
  `ai-implementation` marker and blocks on two, so close the PR you do not
  keep. See [Replan transaction](agent-guides/replan-transaction.md).

## Exit codes

The exit codes of the single-issue commands. Every command also exits 2 on
a usage error (an unknown option, a missing argument). The `epic` commands
have [their own table](usage.md#recovery-and-exit-codes).

| Command | 0 | 1 | 2 |
|---|---|---|---|
| `run` | stopped at `READY_FOR_MERGE` or `DONE`, or `--max-steps` spent in a working phase; a dry run | the run ended `BLOCKED` or `FAILED`; an error that left the phase unchanged (an agent failure, a GitHub or verification failure) | a configuration error (the sandbox's capability and the config file's placement included); an existing run it does not replace without `--force`; a state that does not load; the lock held; an issue a scheduler owns |
| `step` | the step ran and the phase is not `BLOCKED` or `FAILED`; the run is `DONE`; a dry run | the step ended `BLOCKED` or `FAILED`; an error that left the phase unchanged; `READY_FOR_MERGE` with the merge gate closed | the run is `BLOCKED` or `FAILED` (`cannot step from terminal phase`); a configuration that differs from the recorded one, or a weaker `sandbox.mode`, without `--accept-config-change`; no state, or one that does not load; the lock held; an issue a scheduler owns |
| `resume` | stopped at `READY_FOR_MERGE` (the banner) or `DONE`, or `--max-steps` spent; a dry run | the run is or ends `BLOCKED` or `FAILED`; an error that left the phase unchanged | as `step` |
| `unblock` | a phase was re-entered (then `resume`); a dry run, whatever it would decide | refused: the run stays `BLOCKED`; an action id that names no waiting action; a GitHub failure | the run is not `BLOCKED`; a malformed action id; a configuration that differs, without `--accept-config-change`; no state, or one that does not load; the lock held; an issue a scheduler owns |
| `status` | printed | `--action` names no saved action | no state, or one that does not load |
| `doctor` | every required check passed (a `WARN` or a skipped check does not fail) | a required check failed | a usage error only |

`Ctrl-C` ends any of them with Python's status for an interrupt, 130. The
codes are diagnostics for a person or a wrapper script; `status --json` and
`doctor --json` are the machine-readable answers. `autoforge local ...` no
longer exists: LOCAL mode was removed (ADR 0009), and the parser refuses it
(`invalid choice: 'local'`, exit 2).

## Recovery scenarios

Each row is a recovery scenario: what triggers it, what AutoForge does,
how to recover, and the automated test that holds the behaviour. A test is
named `file::function`; a parametrized one covers every case its
parameters list. Where the tests leave part of a scenario uncovered,
[Known limitations](#known-limitations) says which part.

<!-- recovery-scenarios -->
| # | Scenario | What AutoForge does | How to recover | Evidence |
|---|---|---|---|---|
| S1 | `Ctrl-C` while an agent runs | ends the agent's process group before it exits; the launch was recorded before it started, and the phase is unchanged | `resume`: the re-entry reads the journal and GitHub first | `tests/test_executor_duplex.py::test_exception_inside_the_with_leaves_no_process_in_the_group`; `tests/test_executor.py::test_a_setup_failure_after_the_spawn_kills_the_child`; `tests/test_engine.py::test_launch_is_persisted_before_the_agent_runs`; `tests/test_engine.py::test_fix_crash_window_resumes_from_the_journal_and_writes_each_effect_once` |
| S2 | the controller killed mid-phase (`kill -9`, a crash, power loss) | nothing to clean up: the kernel releases the lock; state saved atomically; a write that landed before its save is adopted, not repeated | stop a surviving unsandboxed agent, then `resume` | `tests/test_engine.py::test_analyze_crash_after_the_plan_save_completes_from_the_journal`; `tests/test_engine.py::test_analyze_a_crash_after_a_landed_pr_gh_prints_otherwise_resumes_without_side_effects`; `tests/test_engine.py::test_k8_crash_after_the_write_before_the_save_is_observed_without_a_second_post`; `tests/test_epic_recovery.py::test_a_worker_killed_after_opening_its_pr_adopts_it_on_resume`; `tests/test_lock_domains.py::test_sigkill_of_the_last_holder_releases_every_lock_and_leaves_nothing_to_clean` |
| S3 | the agent exits non-zero | `State unchanged.` with the worktree, logs and `resume` lines, exit 1; no correction, no relaunch | inspect, then the printed `resume` | `tests/test_cli.py::test_an_agent_failure_prints_a_resume_command_with_the_flags_given`; `tests/test_engine.py::test_an_agent_failure_names_its_worktree_its_log_and_the_resume_command`; `tests/test_engine.py::test_fix_provider_failure_changes_nothing_and_the_relaunch_is_pushed` |
| S4 | a malformed `CONTROL_RESULT` | one correction launch by default; then the step fails with the phase unchanged; no correction while a process of the launch may still run | inspect, then `resume` | `tests/test_engine.py::test_malformed_result_triggers_one_correction`; `tests/test_engine.py::test_correction_is_bounded`; `tests/test_engine.py::test_a_malformed_result_is_not_corrected_while_a_process_its_launch_started_may_run` |
| S5 | an idle timeout or the maximum runtime | the agent killed; the error names the limit and its key; `execution.json` records `timeout_limit`; the phase unchanged; not retried | read the records, raise the limit if that is the cause, `resume --accept-config-change` | `tests/test_engine.py::test_an_idle_timeout_names_the_limit_and_the_last_activity`; `tests/test_engine.py::test_a_pi_deadline_is_a_timeout_naming_what_it_left_behind`; `tests/test_engine.py::test_a_maximum_runtime_timeout_names_the_ceiling`; `tests/test_executor.py::test_an_idle_timeout_kills_the_whole_group`; `tests/test_cli.py::test_accept_config_change_records_the_change_in_the_run_log_and_continues` |
| S6 | a loop kill | the agent killed at the threshold; `ended_by: loop`; the phase unchanged | fix what the agent repeats, then `resume` | `tests/test_loop_detect.py::test_e2e_the_default_kills_a_looping_agent_and_leaves_the_phase_unchanged` |
| S7 | a transient provider failure | relaunched inside the step at most twice, after re-reading GitHub; a 429 or another failure is not relaunched | `resume` once the provider is back | `tests/test_engine.py::test_a_transient_openai_failure_is_relaunched_after_reconciling`; `tests/test_engine.py::test_openai_websocket_disconnect_is_relaunched_at_most_twice_per_step`; `tests/test_engine.py::test_a_claude_api_error_that_persists_or_is_not_transient_fails_the_step` |
| S8 | `BLOCKED` | `resume` exits 1 and runs nothing; `unblock` re-enters a phase (exit 0) or refuses and changes nothing (exit 1); `--dry-run` writes nothing | fix the cause, `unblock`, `resume` | `tests/test_cli.py::test_unblock_reenters_a_phase_and_resume_continues`; `tests/test_cli.py::test_unblock_refusal_leaves_the_run_blocked_and_exits_1`; `tests/test_cli.py::test_unblock_dry_run_previews_and_writes_nothing` |
| S9 | an acceptance action approved | `status --action` prints the exact payload; after `unblock --approve-action`, `resume` posts exactly it once and reads it back | `status --action A-…`, `unblock --approve-action A-…`, `resume` | `tests/test_cli.py::test_status_action_prints_the_exact_payload_bytes`; `tests/test_cli.py::test_unblock_approve_action_publishes_on_resume` |
| S10 | an acceptance action declined | the unsent plan dropped; the fixer launched again with the decline in its prompt; an id that names no action refused | `unblock --decline-action A-…`, `resume` | `tests/test_engine.py::test_fix_acceptance_action_declined_reruns_the_fixer_with_the_decline`; `tests/test_cli.py::test_unblock_acceptance_flags_exit_codes` |
| S11 | operator actions | the controller posts nothing; it verifies a comment posted with the exact payload, or one bound to the action, and drops the plan on a decline | post through the `status --action` pipe, or `unblock --bind-evidence`, or `unblock --decline-action`; then `unblock` and `resume` | `tests/test_operator_actions.py::test_operator_actions_complete_from_exact_payloads_without_relaunching`; `tests/test_cli.py::test_status_action_prints_an_operator_payload_and_a_bound_state`; `tests/test_operator_actions.py::test_bound_comments_are_verified_as_operator_bound_evidence`; `tests/test_cli.py::test_unblock_declines_several_operator_actions_at_once`; `tests/test_operator_comment_fidelity.py::test_the_documented_pipe_round_trips_the_payload_byte_for_byte` |
| S12 | the configuration changed between commands | `step`, `resume` and `unblock` refuse (exit 2), name each change and write nothing; `--accept-config-change` writes it to the run log first | run the printed command, or repeat with `--accept-config-change` | `tests/test_cli.py::test_resume_without_the_config_a_run_started_with_refuses_before_anything_runs`; `tests/test_cli.py::test_accept_config_change_records_the_change_in_the_run_log_and_continues`; `tests/test_cli.py::test_unblock_refuses_another_configuration_and_records_an_accepted_one`; `tests/test_cli.py::test_a_resume_without_the_runs_sandbox_mode_refuses_until_accepted` |
| S13 | a second process while the lock is held | refused with exit 2, no agent launched, nothing written; the message names the scheduler and its surviving workers | wait for the holder, or stop it | `tests/test_cli.py::test_second_controller_with_another_state_dir_is_refused_while_the_first_runs`; `tests/test_cli.py::test_a_scheduler_holding_the_controller_lock_excludes_a_standalone_command`; `tests/test_scheduler.py::test_a_scheduler_exit_with_a_live_worker_keeps_standalone_commands_out` |
| S14 | the agent asks for a permission or a question | the CLI refuses it; a run that goes on is left to its result; a refusal that alone failed the run is named and neither corrected nor relaunched | change the issue, or the permission rules, then `resume` | `tests/test_engine.py::test_a_refused_request_that_failed_the_run_is_named_and_neither_corrected_nor_relaunched`; `tests/test_providers.py::test_a_refused_request_the_agent_went_on_without_changes_nothing`; `tests/test_engine.py::test_a_claude_refusal_the_agent_went_on_without_leaves_the_outcome_to_its_result` |
| S15 | a state a newer controller wrote | every command refuses (exit 2) with the protocol mismatch block and writes nothing; `run` does not suggest `--force` | run that controller ([A run a newer controller wrote](usage.md#a-run-a-newer-controller-wrote)) | `tests/test_cli.py::test_a_newer_controllers_run_is_refused_before_anything_is_written`; `tests/test_protocol_compat.py::test_an_older_controller_imported_from_elsewhere_names_itself_and_writes_nothing` |
| S16 | an unreadable state file | `status`, `step`, `resume` and `unblock` refuse (exit 2) and change nothing; `run --force` moves it aside whole | `run --force` starts over; the old file is kept | `tests/test_cli.py::test_status_step_resume_and_unblock_refuse_a_state_that_does_not_load`; `tests/test_cli.py::test_run_refuses_a_state_that_does_not_load_and_force_moves_it_aside_whole` |
| S17 | the operator's checkout moved during an agent run | `BLOCKED`, nothing rolled back, with `checkout:`, `unblock:` and `resume:` lines; no relaunch | inspect, then the printed `unblock` and `resume` | `tests/test_engine.py::test_a_commit_to_the_operators_checkout_during_the_agent_run_blocks`; `tests/test_engine.py::test_drift_left_by_a_launch_blocks_before_any_relaunch` |
| S18 | the PR closed by a human | `FIX` refuses with the phase unchanged and nothing pushed; once reopened, `resume` completes the plan; `unblock` refuses while it is closed | reopen it, then `resume` or `unblock`; or start over | `tests/test_engine.py::test_fix_pr_closed_while_the_fixer_ran_is_refused`; `tests/test_engine.py::test_fix_pr_closed_after_a_follow_up_write_pushes_once_it_is_reopened`; `tests/test_unblock.py::test_unblock_refuses_a_closed_pr` |
| S19 | merge conflicts at the merge gate | `BLOCKED`, nothing merged or counted | resolve them on the branch; `unblock` decides `REVIEW` of the new head | `tests/test_engine.py::test_merge_blocks_on_conflicting_pr_without_calling_gh`; `tests/test_unblock.py::test_unblock_open_pr_whose_head_moved_reenters_review_and_carries_findings` |
| S20 | an interrupted replan | the phase stays `REPLAN_REEXECUTE`; a journaled push and PR are completed without the agent; before that, the agent runs again on the recorded base | `resume` | `tests/test_replan.py::test_a_crash_after_the_plan_is_saved_publishes_on_resume_without_the_agent`; `tests/test_replan.py::test_a_crash_after_the_agent_ran_relaunches_it_on_the_recorded_base` |
| S21 | an interrupted validation command | the next step records it `INTERRUPTED` and runs it again; the third interruption blocks | `resume` | `tests/test_validation_evidence.py::test_an_interrupted_validator_leaves_its_marker_and_the_next_step_records_it`; `tests/test_validation_evidence.py::test_the_third_interruption_blocks_in_the_same_write` |
<!-- /recovery-scenarios -->

`tests/test_troubleshooting_guide.py` checks that every test this table
names exists, and that every row names one.

## Known limitations

These parts of the scenarios above are held by design and code reading,
not by a test, and no recorded manual run covers them yet:

- **A real signal to a standalone controller.** S1 and S2 interrupt the
  controller by raising `KeyboardInterrupt` or another exception at a chosen
  point and then reload the state in a new engine. No test sends SIGINT,
  SIGTERM, SIGHUP or SIGKILL to an `autoforge run`, `step` or `resume`
  process, and none interrupts the engine while an agent subprocess is
  running. Real SIGKILLs are tested for the scheduler's workers, which run
  the same engine (S2's `test_epic_recovery.py` test).
- **The exit status of `Ctrl-C`.** 130 is Python's status for an uncaught
  `KeyboardInterrupt`, which the CLI does not catch; no test checks it.
- **An agent outliving a killed controller.** That an unsandboxed agent
  keeps running after the controller is killed with SIGTERM, SIGHUP or
  SIGKILL, and that an enforced sandbox's agent dies with it, follows from
  the executor's design
  ([ADR 0002 §4e](adr/0002-executor-nothing-outlives-the-invocation.md#4e-amendment-sandboxed-launches-167-2026-10-12));
  no test kills a standalone controller to show either.
- **A suspended machine.** No test or recorded run covers a closed laptop.
- **The permission and question rows through the CLI.** S14 is tested at
  the engine and adapter level; no test drives it through
  `autoforge run`, `step` or `resume`.
- **The operator-action pipe.** S11's
  `status --action X-… | gh issue comment ... --body-file -` is checked
  against a recorded capture of a real run, not run by the suite (no test
  writes to GitHub).
- **A Claude Code 408.** It is classified as transient at the adapter
  level; the engine-level relaunch tests use 529 and 503.
