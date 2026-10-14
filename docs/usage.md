# Running the remote workflow

How to drive an EPIC and its issues through AutoForge's GitHub workflow:
the commands, where a run stops and what to do there, and how to recover.
For the GitHub-free workflow see [Local mode](local-mode.md); for the config
file see [Configuration](configuration.md). Installation and prerequisites
are in the [README](../README.md#prerequisites).

## Commands

```bash
uv run autoforge doctor                 # environment checks (writes nothing but a state-dir probe)
uv run autoforge doctor --json

# preview without touching anything (no subprocess, no gh call, no state file)
uv run autoforge run --epic https://github.com/owner/repo/issues/1 \
               --issue https://github.com/owner/repo/issues/2 --dry-run

# create a run and drive it until READY_FOR_MERGE / BLOCKED / FAILED
uv run autoforge run --epic https://github.com/owner/repo/issues/1 \
               --issue https://github.com/owner/repo/issues/2

uv run autoforge status            # human summary
uv run autoforge status --json     # machine-readable; `replan_journal` says whether the
                                   # raw `replan_transaction` beside it is readable, and
                                   # `acceptance_actions` lists the acceptance actions
uv run autoforge status --action A-0123456789abcdef   # the exact payload bytes of
                                   # an acceptance action (exit 1 for an unknown id)

uv run autoforge step              # exactly one phase step
uv run autoforge step --dry-run    # preview the next step

uv run autoforge resume            # continue until a stop phase / --max-steps
# --max-steps bounds one invocation; workflow.max_total_steps (config) bounds
# the whole run and is not reset by resume

# only with safety.allow_merge: true in config — the controller verifies on
# GitHub and merges itself (agents never merge); re-run to re-check
# inconclusive GitHub data (bounded by merge.max_verification_attempts)
uv run autoforge resume --allow-merge

# leave BLOCKED explicitly: the controller re-inspects GitHub and re-enters
# the one phase that inspection supports, or refuses and stays BLOCKED;
# then `resume`. --dry-run previews the decision without writing anything
uv run autoforge unblock --reason "reran the flaky check; it is green"

# a FIX acceptance action awaits you (safety.acceptance_actions: approve):
# approve exactly that action id, or decline it (FIX runs the fixer again);
# then `resume`. Mutually exclusive; --dry-run works here too
uv run autoforge unblock --approve-action A-0123456789abcdef --reason "checked the text"
uv run autoforge unblock --decline-action A-0123456789abcdef --reason "overstates coverage"
```

Pass a config file with the global option, before the subcommand:
`uv run autoforge --config autoforge.yaml run ...`. `run --force` discards an
existing non-terminal run (an unreadable state file is moved aside rather
than deleted; see [Troubleshooting](#troubleshooting-and-recovery)).
`--full-prompt` prints the whole rendered prompt in a dry run.

URLs must be HTTPS GitHub issue URLs, and EPIC + issue must be in the same
repository as the current working directory (cross-repo runs are rejected).
`run`, `step` and `resume` must be run inside a git checkout of that
repository, because the repository lock lives in its git directory (dry-run
does not need one).

## What a run does

```text
Issue → ANALYZE_EXECUTE (Claude Code) → controller pushes, opens PR → REVIEW (OpenCode)
       → clean ───────────────────────────────▶ READY_FOR_MERGE
       → findings ────────────────────────────▶ FIX (Claude Code) → REVIEW …
       → excessive / low-finding stagnation ──▶ REPLAN_REEXECUTE (OpenCode)
                                                    → replacement PR → REVIEW round 1
```

Every phase ends with the controller checking the agent's claims against
GitHub before the state advances; an agent claim that cannot be verified
never advances the state machine. In brief:

| Phase | Who | What the controller checks |
|---|---|---|
| `INITIALIZING` | controller | cwd repo == issue repo, issue is in this repo, is not the EPIC, exists and is OPEN |
| `ANALYZE_EXECUTE` | Claude Code (`analyze_execute`), which makes no GitHub write | an open PR already carrying the issue's `ai-implementation` marker is adopted without running the agent (two block); the agent commits on its worktree's detached HEAD, and the controller checks that HEAD itself (the reported SHA, descent from the default branch head and from the branch head, every commit message), pushes it to `autoforge/<n>`, opens the PR with the agent's title and body (or adopts the open unmarked PR on that branch), journalling each write before it is sent, and reads the PR back at exactly the pushed HEAD; a marker PR or a branch head it did not write blocks |
| `REVIEW` | OpenCode (`review_round_1`, `review_round_2_5`, `review_round_6_plus`), which makes no GitHub write | the round is bound to the PR, HEAD, base and merge base read from GitHub, and a comment already carrying that round's marker blocks (the controller never adopts one it did not post); the reviewer's round and HEAD must match the binding and `needs_fix_round == (len(findings) > 0)`; the controller renders the review comment from the result, journals it, posts it and reads it back, so a crash or `resume` never posts a second one; then controller policy picks `FIX`, `READY_FOR_MERGE`, `REPLAN_REEXECUTE` or `BLOCKED` |
| `FIX` | Claude Code (`fix`), which makes no GitHub write | the PR is still at the reviewed revision (else back to `REVIEW` without running the fixer); the open issues already marked as a finding's follow-up are handed to the fixer to reuse, and one the controller did not hand over or create blocks; the fixer commits on its worktree's detached HEAD and resolves every open finding exactly once, and the controller checks that HEAD itself (the reported SHA, descent from the reviewed HEAD, every commit message, a commit for each `fixed` finding); it creates the follow-up issues the fixer asks for, appends the finding markers to the handed-over issues it defers to, then pushes the commit last, journalling each write before it is sent, and reads the follow-ups and the PR head back, so a crash or `resume` never creates or pushes twice; a push it did not make sends the PR back to `REVIEW` |
| `REPLAN_REEXECUTE` | OpenCode (`replan_reexecute`), which makes no GitHub write | one durable transaction; the agent commits on its worktree's detached HEAD at the default branch head the controller recorded, and the controller checks that HEAD itself (the reported SHA, descent from that base, every commit message), pushes it to `autoforge/<n>-replan-<transaction id>`, creates the replacement PR with the agent's title and body followed by the controller's markers, journalling each write before it is sent, and binds the PR only at exactly the pushed HEAD, so a crash or `resume` never pushes or creates twice; a replacement branch or a marker PR it did not write rejects the transaction; the old PR is closed by the controller only while both PRs still match their checkpoints |
| `READY_FOR_MERGE` | nobody | holding state; with the merge gate open, the full pre-merge verification runs before `MERGE` is entered |
| `MERGE` (gated) | controller, never an agent | `gh pr merge --match-head-commit <reviewed HEAD>`, counted only once GitHub reports `MERGED` at that HEAD into the reviewed base |
| `UPDATE_EPIC` | OpenCode (`update_epic`), which makes no GitHub write | the controller (not the agent) posts the progress comment and writes the roadmap section, journals each write before performing it and reads it back, so a crash or `resume` never posts a second comment; a marker comment it did not post blocks; the next issue gets the `INITIALIZING` checks; `null` -> `DONE` |

The complete per-phase verification, including what each phase reads
*before* it launches an agent so a re-entry never duplicates work, is in
[GitHub safety](agent-guides/github-safety.md); the transitions and
review-round routing are in [Workflow](agent-guides/workflow.md); the replan
transaction is in [Replan transaction](agent-guides/replan-transaction.md).

### Loop bounds and replanning

The REVIEW/FIX loop is bounded by the controller, never by prompt wording:
a cap on review rounds per PR (`workflow.max_review_rounds`, default 20),
stagnation detection (`workflow.stagnation_*`), and a cumulative step budget
for the whole run (`workflow.max_total_steps`, default 300, measured on the
persisted step count so `resume` continues it). Failed agent invocations
consume neither a review round nor the history. Every bound ends in
`BLOCKED` with the reason; findings and the PR stay for a human.

Replanning is controller policy, not reviewer advice. After a verified
review with findings, `review.replan` may decide to abandon a
patch-accumulated PR: it preserves compact finding metadata and selected
review comments, then asks the separate `replan_reexecute` high-effort
profile to independently rebuild from the latest verified default branch.
At most two replans per issue are allowed by default
(`review.replan.max_replans_per_issue`); a further eligible trigger blocks
for human intervention. This retains failure knowledge while intentionally
discarding implementation anchoring; it does not guarantee convergence. The
exact triggers are in [Workflow: loop bounds](agent-guides/workflow.md#loop-bounds).

## Where a run stops

`run` and `resume` loop one step at a time until a stop phase:
`READY_FOR_MERGE`, `DONE`, `BLOCKED` or `FAILED`.

### READY_FOR_MERGE: a human merges, unless the gate is open

When the loop reaches `READY_FOR_MERGE` with the merge gate closed (the
default) the CLI prints a banner with the issue, PR, review round and reviewed
HEAD, and states that automatic merge is disabled: a human merges the PR.
`resume` without the gate open re-prints the banner and runs nothing. With the
gate open (`resume --allow-merge` / `step --allow-merge`) the controller
continues through its own pre-merge verification, `MERGE` and `UPDATE_EPIC`.

The gate opens only when both `safety.allow_merge: true` is set in config and
`--allow-merge` is passed on the CLI. Pre-merge verification fails closed:
conclusive negatives are `BLOCKED`, inconclusive GitHub data keeps the phase
for another `resume --allow-merge` (at most
`merge.max_verification_attempts` times, then `BLOCKED`), and a PR that
changes `safety.protected_merge_paths` (default `.github/workflows/`) is
never merged unattended. The full gate, including the check-definition
comparison and the optional controller-run `merge.verification_commands`,
is in [GitHub safety: merge safety](agent-guides/github-safety.md#merge-safety).

### BLOCKED: fix the cause, then unblock

A run in `BLOCKED` is terminal for `step` and `resume`. `autoforge status`
shows the `block_reason`. After fixing the cause on GitHub (closing a
duplicate PR, rerunning a check, raising a bound in the config),
`autoforge unblock --reason "..."` asks the controller to decide again from
live GitHub: it re-runs the recovery inspection of the phase it would
re-enter and either re-enters it (`ANALYZE_EXECUTE`, `REVIEW`, `FIX`,
`READY_FOR_MERGE` or `UPDATE_EPIC`, through the same transition validation as
any other step) or refuses and leaves the run `BLOCKED`, exit code 1, when
the safe state still cannot be determined (two candidate PRs, a closed PR, a
merge no review decided on, a replan transaction on record, an exhausted
bound). The reason is recorded in the state file and the run log, `status`
shows the last one, and no agent runs until `resume`. A controller write
that blocked as a conflict (for example the `UPDATE_EPIC` progress comment
after its two attempts, or a marker comment the controller did not post)
names the effect in the reason: perform the write by hand or remove the
conflicting object, then `unblock`; the controller reconciles the write
against GitHub again before it issues anything. `ANALYZE_EXECUTE` blocks the
same way on anything it did not write: a PR carrying the issue's marker
that appeared while the agent ran (if it is the implementation, `unblock`
binds it), a push to, creation or deletion of `autoforge/<n>` it did not
record (inspect the branch; `unblock` reads it again and the agent continues
from its head), a closed or merged PR on that branch (reopen it to have it
adopted, or finish the issue by hand), an open PR on that branch the entry
did not record, such as one opened while the agent ran (`unblock` reads the
branch again and adopts it, so close it first if it is not the issue's), a
default branch renamed or switched before the plan was saved (`unblock`
starts a fresh entry against the new one), and a push whose branch moved after
the plan was saved (put the branch back, or delete it if it did not exist,
then `unblock`: the plan completes without launching the agent). A default
branch changed after the plan was saved blocks before the planned PR is
created: make the planned branch the default again, then `unblock`.
`REVIEW` blocks on a comment carrying the round's review marker that it did
not post, whether it was there before the reviewer ran (a human's, or one a
reviewer of an earlier AutoForge version posted) or appeared while it ran:
the controller never adopts, edits or duplicates it. Delete the comment or
its marker, then `unblock`, and the round runs (or, when the reviewer's
result was already accepted, its comment is posted from the plan without
running the reviewer again). `FIX` blocks on follow-up issues it did not
write: two open issues carrying one finding's follow-up marker, or an
unreadable follow-up marker on any open issue (close or repair the extra
or unreadable issue, then `unblock`); a follow-up issue that appeared or
disappeared while the fixer ran (if it is the finding's follow-up,
`unblock` starts a fresh entry that hands it to the fixer; otherwise close
it or remove its marker first); an issue a deferral names that is no
longer open or has moved to another repository (reopen it, or `unblock`
to run the fixer afresh); an issue whose body now holds something shaped
like a credential, so the marker cannot be appended to it (remove it, then
`unblock`); after the plan was saved, a follow-up issue (reused, or one
the controller already created or appended to) that is no longer the one
open issue carrying its finding's marker, or an open issue carrying the
marker of a finding the plan resolved without a deferral (reopen the
controller's issue, or close or unmark the one it did not write, then
`unblock`); a PR whose head repository GitHub no longer reports, or whose
head moved to a fork or another branch, after the plan was saved; and a
write that conflicts after the plan was saved (reopen the follow-up issue
the controller created, put the issue body back, or leave the PR branch at
the reviewed HEAD or the planned commit, then `unblock`: the plan
completes without running the fixer again). A FIX acceptance action that
awaits you also blocks the run; see the next section. A push to the PR branch the controller did not make, before
or after the fixer ran, is not a block: the PR goes back to `REVIEW` of the
pushed HEAD; so does a PR retargeted to another base, or a base rewritten
under its name, at any point of the phase. A LOCAL
run cannot be unblocked. The decision table is in
[Workflow: leaving BLOCKED](agent-guides/workflow.md#leaving-blocked-the-operators-unblock).

### BLOCKED on an acceptance action

A finding or an acceptance criterion sometimes asks for text to be posted
on the issue itself (a summary of results, say). The fixer cannot post it:
it resolves the finding as `acceptance_action` with the comment's text, and
the controller publishes one comment on the run's issue, with its own header
line above the text and the finding's `ai-acceptance` marker below it. It
publishes nothing else of the `FIX` plan first: the follow-up issues and the
push of the fixer's commit wait for the comment.

With `safety.acceptance_actions: approve` (the default) the step blocks
before anything is sent. `status` shows an `Acceptance action A-…` block:
the target issue, the PR, the round and the finding, who authorizes it, its
state (`awaiting approval`, `approved, not yet published`, `published, not
yet verified`, `verified: <url>` or `verification failed: …`), the payload's
size and SHA-256, the push held back and the agent worktree that alone holds
the unpushed commit (do not reset or remove it), and the next steps. Read the
exact comment with `autoforge status --action A-…`, then either:

- **approve it:** `autoforge unblock --approve-action A-… --reason "..."`,
  then `resume`, which posts the comment, reads it back, and pushes;
- **post it yourself:** post exactly those bytes (marker included) on the
  issue, then `autoforge unblock --reason "..."` and `resume`, which adopt
  your comment and send no second one;
- **decline it:** `autoforge unblock --decline-action A-… --reason "..."`;
  the unsent plan is dropped and `resume` runs the fixer again with your
  reason in its prompt. The same comment for the same finding is refused if
  asked for again. After 16 declines in one review round the controller
  requests no further acceptance comment in that round: the fixer resolves
  the finding another way.

The id names the exact bytes and the target, so an id of another plan or a
changed payload never matches. A plain `unblock` with nothing on the issue is
refused: your reason is not evidence, and a comment without the marker, or
with other bytes, does not fulfil the action (a marked comment with other
bytes is a conflict: delete it, or edit it back to the payload). Approving or
declining is refused, with nothing written, after a push to the PR or a
change of its base; a plain `unblock` then re-enters `FIX`, which sends
nothing, records the action `stale` and goes back to `REVIEW`. A closed issue
holds the comment until it is reopened, and so does the exact payload already
posted on it: nothing else of the fix is published until then. With `auto` the
controller publishes the comment during the `FIX` step without stopping; with
`off` the fixer is told the resolution is unavailable and a result using it is
refused. The comment counts only after the controller read it back from
GitHub, and the next review still judges whether it resolves the finding.

### DONE and FAILED

`DONE` is reached when `UPDATE_EPIC` reports no next issue. `DONE` and
`FAILED` have no outgoing edges; `autoforge run` starts a new run.

## Watching an agent run

While an agent runs, `run`, `step` and `resume` (and `local run`) print live
progress lines to **stderr**, and each step's outcome to stdout as soon as
that step finishes, not after the whole loop. A progress line is prefixed
with the time since the launch, the phase and, in a remote run, the issue:

```text
[00:00:00 ANALYZE_EXECUTE #160] launching analyze_execute (claude, model fable, effort high), idle timeout 900s, max runtime unset, loop detection kill, attempt 1, worktree /repo/.git/autoforge/worktrees/160, log .autoforge/logs/<run-id>/001-analyze_execute-1
[00:00:04 ANALYZE_EXECUTE #160] agent started (claude-fable-5-1)
[00:00:31 ANALYZE_EXECUTE #160] thinking… ~12k tokens
[00:01:02 ANALYZE_EXECUTE #160] Read src/autoforge/engine.py
[00:01:09 ANALYZE_EXECUTE #160] Grep def _invoke_phase
[00:03:40 ANALYZE_EXECUTE #160] Edit src/autoforge/engine.py
[00:04:12 ANALYZE_EXECUTE #160] Bash Run the test suite
[00:06:12 ANALYZE_EXECUTE #160] still running, last activity 14s ago, 312 events
[00:09:55 ANALYZE_EXECUTE #160] agent exited 0, 841 progress events
[run-…] ANALYZE_EXECUTE -> REVIEW: pushed 3f2a9c1d0b7e to autoforge/160 and opened PR https://github.com/…/pull/161; ANALYZE_EXECUTE -> REVIEW
```

- **Before the launch**, one line names the profile, provider, model and
  effort, both agent limits (the idle timeout and the maximum runtime, see
  [Agent limits](#agent-limits)), the loop detection mode
  ([Loop detection](#loop-detection)), the attempt, the working directory
  and the step's log directory.
- **While it runs**: one line per tool the agent starts, with one
  allow-listed input (the file of a read, edit or write, the pattern of a
  search, the *description* of a shell command, never the command itself,
  a tool's result, thinking or the agent's text); thinking and writing are
  shown at most once every 30 s; a failed tool says so; when nothing has
  been printed for two minutes a heartbeat says how long ago the agent last
  did anything; and a `possible loop: …` line warns that the agent may be
  repeating itself.
- **After it returns**, one line says how it ended (exit status, the limit
  that killed it, or provider failure).

What a provider can report differs. Claude Code (with the default
`output_format: stream-json`) and Pi report each tool and their thinking;
OpenCode reports only that it is active, so its run shows the pre-launch
line, heartbeats that say when it last wrote output, and the end line. A
Claude profile with `output_format: text` reports nothing while it runs
([Configuration: Claude profiles](configuration.md#claude-profiles)).

Every line is stripped of escape and control characters, redacted and
bounded before it is printed. The same lines are appended to `progress.log`
in the step's log directory, created before the launch, so
`tail -f .autoforge/logs/<run-id>/<step>/progress.log` follows a step from
another terminal and the record survives a killed run. Progress never goes
to stdout and is never part of `status --json`; redirecting stderr
(`2>/dev/null`) hides it without affecting the run. A dry run launches no
agent and prints no progress.

## Agent limits

An agent is killed when it has made no progress, meaning it wrote nothing
to stdout or stderr, for its profile's idle timeout (default 900 s). Every
tool call Claude or Pi reports, and every line OpenCode prints, resets that
timer, so a long task is not killed for being long. Configure it per
profile; see [Configuration: Agent limits](configuration.md#agent-limits).

- **`max_runtime_seconds` is the only wall-clock bound on an active
  agent.** It is unset by default. Without it, an agent that keeps
  producing output while going in circles is killed by loop detection
  ([Loop detection](#loop-detection)), and one that keeps doing new things
  runs until the executor's one-week backstop. Set `max_runtime_seconds`
  on a profile when an unattended run must end by a known time, and always
  where loop detection only warns or is off: with `warn` or `off`, and by
  default on a Pi profile and in `UPDATE_EPIC`.
- **A silent tool call counts as no progress.** A test suite that runs for
  twenty minutes inside one shell command and prints nothing until it ends
  looks exactly like a hang. If your project has one, raise
  `idle_timeout_seconds` on the profiles that run it.
- **A Claude profile with `output_format: text`** writes nothing until it
  exits, so it has no idle timeout and must set `max_runtime_seconds`.
- **Only the agent's own output counts.** Nothing the controller does
  resets the timer, and neither will a wait for a human answer once agents
  can ask one (#126).

When a limit fires, the error names it, the agent's last output and the
configuration key that set it, for example `agent 'analyze_execute' made no
progress for 900s (last activity 08:31:02 UTC) and was killed. The limit is
set by execution.default_idle_timeout_seconds`, or `... reached its 7200s
maximum runtime and was killed. The limit is set by
profiles.analyze_execute.max_runtime_seconds`. The state is unchanged,
nothing is retried automatically, the error says where to look and how to
resume ([Troubleshooting](#troubleshooting-and-recovery)), and the step's
`execution.json` records `timeout_limit` (`idle` or `max_runtime`), both
configured values and `last_activity_at`.

## Loop detection

An agent can keep producing output while it repeats itself: the same
command, the same file read, the same answer, round and round. Its output
resets the idle timer, so the idle timeout never fires. The controller
watches what the agent does and reports a loop when the same actions, with
the same inputs and the same results, keep coming back with nothing new in
between ([Configuration: Loop detection](configuration.md#loop-detection)).

From half a threshold the progress output warns, and at the threshold the
agent is killed:

```text
[00:06:40 FIX #160] possible loop: 2-step cycle (Bash, Read) repeated 4×
[00:09:14 FIX #160] agent killed: the same 2-step action cycle (Bash, Read) repeated 8 times over 9m14s with no new action, 1834 progress events
```

The warning repeats at most every two minutes while the loop goes on, and
the step's `execution.json` records it (`loop`, `loop_warnings`).

- **In `kill` mode (the default, except on a Pi profile and in
  `UPDATE_EPIC`)** the agent is killed at the threshold,
  exactly as on a timeout: its whole process group is ended, the state is
  unchanged and nothing is retried automatically. The error names the
  pattern, followed by where to look and how to resume
  ([Troubleshooting](#troubleshooting-and-recovery)):

  ```text
  agent 'fix' was killed: the same 2-step action cycle (Bash, Read) repeated 8 times over 9m14s with no new action
  State unchanged.
  ...
  ```

  The step's `execution.json` says `ended_by: loop` and `timeout_limit:
  loop`, with a `loop` entry naming the signal (`action_cycle`,
  `no_novelty`, `repeated_lines` or `retry_storm`), the cycle's period, its
  repeat count, the tool names and when the loop started and was cut,
  measured from the launch. It records no tool input, tool result or
  output line.
- **In `warn` mode** the agent runs on past the threshold, and the
  progress output says `loop detected, not killed (mode warn): …` instead.
  Watch for these lines, and stop the run (`Ctrl-C`) if the agent is
  stuck. A config that does not set `mode` runs a Pi profile and
  `UPDATE_EPIC` this way, because no real run has measured either yet;
  `mode: kill` kills there too. The launch line names the mode in force.

**After a loop kill**, look at the agent worktree and the PR the way you
would after a timeout: the agent may have pushed part of its work. Then
`resume` relaunches the same phase in the same worktree. A loop that comes
back on every attempt usually means the task, or a flaky command it keeps
re-running, needs a person: fix that before resuming. Raising the
thresholds is the last resort.

In every mode, `off` included, `provider_summary` in `execution.json`
records the `loop_*` calibration fields: how close the invocation came to
each threshold. The defaults were checked against those fields from real
runs (#199,
[Configuration: How the defaults were set](configuration.md#how-the-defaults-were-set)).

## Dry run

A dry run invokes no agent, runs no verification command, writes nothing to
GitHub, git or the state directory (no state, no log), takes no lock and
creates no agent worktree. It prints the plan (phase, provider,
model/effort, agent limits, loop detection, round, template, variables,
command, expected transition),
redacted. What it reads depends on the command:

- `run --dry-run` previews a new run and launches no subprocess: no `git`,
  no `gh`.
- `step --dry-run` and `resume --dry-run` preview the existing run and make
  no `gh` call. Without `--state-dir` they first run two read-only
  `git rev-parse` calls to find which run is meant, a remote one under
  `.autoforge/` or a local one in the git directory; `--state-dir` skips
  that lookup.
- `unblock --dry-run` does the same lookup, then reads GitHub to report the
  decision it would make (with `--approve-action` or `--decline-action` too).

## Where things live

- **State and logs:** `.autoforge/` in the working directory by default
  (`--state-dir` or `state_dir` in config): `state.json` plus one log
  directory per run with the redacted prompt, output, and parsed result of
  every agent invocation, and `read-context/<run-id>/`, the GitHub data each
  REMOTE launch was handed. Layout:
  [State and recovery: runtime artifacts](agent-guides/state-and-recovery.md#runtime-artifacts).
  The directory must be on a filesystem with hard links: run logs are
  published by `link(2)`, so vfat/exFAT and some FUSE, SMB/CIFS and overlay
  mounts are refused before the first launch (and by `doctor`) with an
  error that says so; point `--state-dir` at another filesystem.
- **Repository lock:** `<repo>/.git/autoforge/controller.lock`, the same
  file for every `--state-dir`, subdirectory and linked worktree of one
  checkout. A second controller exits with `LockError`.
- **Agent worktrees:** `<repo>/.git/autoforge/worktrees/<issue-number>` (or
  under `execution.worktree_dir`), added detached at the checkout's HEAD the
  first time an agent is launched for the issue and reused by every later
  phase of that issue. AutoForge never deletes it: remove it with
  `git worktree remove` once the issue is done.
- **Sandbox directories** (`sandbox.mode: required` only):
  `<repo>/.git/autoforge/sandbox/<issue-number>/` holds the issue's private
  object directory (`objects/`, where an agent's commits land until the
  controller imports them), the overlay's work directory and the cache its
  `ANALYZE_EXECUTE`, `FIX` and `REPLAN_REEXECUTE` launches share. They are
  created at the issue's first enforced launch and never deleted: remove
  them with the worktree
  ([State and recovery: runtime artifacts](agent-guides/state-and-recovery.md#runtime-artifacts)).

### How agents are separated from your checkout

Every REMOTE agent runs in that per-issue worktree, so its working directory
contains neither `.autoforge/`, the lock, nor the operator's uncommitted
work, and a `git clean -fdx` there cannot reach controller state. It starts
from an allow-listed environment: `execution.env_allowlist` (a documented
default of PATH, HOME, locale, temporary directory, XDG directories, git
author settings and the proxy and CA variables) plus `env_allowlist_extra`,
plus the names the provider adapter declares for its own CLI (`ANTHROPIC_*`
and `CLAUDE_*` for Claude Code; `OPENCODE_*`, `OPENAI_*`, `ANTHROPIC_*`,
`GEMINI_*`, `GOOGLE_*` for OpenCode); nothing else in the operator's
environment is inherited, and the names (never the values) are recorded in
the invocation's `request.json`, with the names the controller sets
(`env_set`). Before and after each invocation, including before every
relaunch within a step, the controller reads HEAD and the checked-out branch
of its own checkout; a change enters `BLOCKED` with the drift in the reason,
nothing is rolled back, and `unblock` reconciles the agent's GitHub work. Under
the default `sandbox.mode: disabled` this is process and filesystem-level
separation, not a sandbox: the agent still runs as the operator with network
access. `sandbox.mode: required` adds ADR 0005's outer sandbox to every
launch: the agent sees the system runtime, a private home and `/tmp`, its
worktree, the repository's git metadata read-only, its read context and
what its provider declares; your checkout, the state directory, the lock,
other worktrees and the rest of your home are absent. It still runs as your
UID on the host network, and a host that cannot enforce refuses the launch
rather than running it without the sandbox
([Configuration: outer sandbox](configuration.md#outer-sandbox)).

The agent holds no GitHub or git credential (#165). The controller pushes,
opens the PR, posts the review comment, creates follow-up issues and
updates the EPIC itself; the agent writes the text and commits locally. No
credential variable can be allow-listed, and each launch gets an empty `gh`
configuration, a placeholder `GH_TOKEN`, a git configuration holding only
your author identity, and git settings no repository configuration can
override that disable credential helpers, askpass and the SSH agent and
keys ([Configuration: agent environment](configuration.md#agent-environment)).
What the agent needs from GitHub (the issue, the PR and its checks, the
review comment it acts on, the EPIC) the controller reads first and writes,
redacted and bounded, into `<state dir>/read-context/<run-id>/`; the prompt
names that directory. Before every launch the controller refuses to start
when a setting would still hand over a credential (a credential helper in
the repository's git configuration, a token in a remote URL, a GitHub entry
in `~/.netrc`, an `export GH_TOKEN=...` in `~/.bash_profile` or
`~/.profile`, an organization connected to OpenCode), naming the setting,
never its value; the run is left untouched, so remove the setting and run
the same command again.
`autoforge doctor` runs the same check. What this does not stop (a
deliberate read of `~/.config/gh/hosts.yml` by absolute path, for one) is
in [GitHub safety](agent-guides/github-safety.md#threat-model); ADR 0005's
outer sandbox, `sandbox.mode: required`, closes it for every enforced launch
([GitHub safety: outer sandbox threat model](agent-guides/github-safety.md#outer-sandbox-threat-model-adr-0005)).

AutoForge never commits to the default branch, never deletes a worktree it
created, and never creates or cleans up local branches or any other
worktree: leaving old local work untouched is safer than trying to infer
ownership or discard uncommitted user changes.

## Troubleshooting and recovery

- **The process was interrupted** (`Ctrl-C`, a killed process, a reboot):
  run `autoforge resume`. State is written atomically, and every phase that
  launches an agent reads its journal and GitHub first, so work already
  done (an open PR, a review comment the controller posted, a pushed fix)
  is adopted or completed rather than repeated. See
  [Workflow: re-entering a phase](agent-guides/workflow.md#re-entering-a-phase).
- **An agent failed, timed out or returned a malformed result:** a malformed
  `CONTROL_RESULT` from an agent that exited 0 is retried with a correction
  prompt (`execution.max_correction_attempts`, default once), unless that
  launch may have left a process running: then the error says so
  (`Not re-invoked: a process the launch started may still be running`,
  followed by which one); stop it before `resume`. The one failed
  exit relaunched automatically is a launch its adapter reports as transient
  (today an OpenCode `openai/*` run whose only error is `WebSocket closed
  with code 1000`): a REMOTE run relaunches it at most twice per step, after
  re-reading GitHub, and when it stops the error says why ("not relaunched:
  ..."). Any other non-zero exit, a timeout or a verification failure is not
  retried automatically and leaves the phase unchanged for `resume`. Inspect
  each invocation under `<state dir>/logs/<run-id>/`; a transient failure's
  `error.txt` says `(transient: ...)`. A timeout says which limit fired and
  the key that set it; see [Agent limits](#agent-limits) before raising one.

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
    left behind. The count is left out when that read fails. A LOCAL run
    prints no `worktree` line: its agent works in your checkout. AutoForge
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
    started with, as you typed them; run it from the same directory. The
    CLI does not look for a configuration file, so a bare `resume` after a
    run started with them finds no run (no `--state-dir`) or runs on the
    built-in defaults (no `--config`): another model, other limits and
    another worktree. Nothing checks yet that `resume` uses the
    configuration the run started with, so check the plan of the dry run
    first.
- **The run is `BLOCKED`:** read `autoforge status`, fix the cause, then
  `autoforge unblock --reason "..."` and `autoforge resume` (see above),
  with the `--config` / `--state-dir` the run was started with; `unblock`
  prints that `resume` command. A LOCAL run cannot be unblocked: start a new
  `local run`. A run blocked because a checkout moved (the checkout the
  controller is run from, or a LOCAL run's repository: an agent committed,
  reset or switched branches) ends with where to look and what to run,
  already carrying those flags:

  ```text
    checkout:  /home/me/code/AutoForge
    unblock:   autoforge --config autoforge.yaml --state-dir .autoforge/AutoForge-issue-159 unblock --reason '<what you found>'
    resume:    autoforge --config autoforge.yaml --state-dir .autoforge/AutoForge-issue-159 resume
  Inspect the checkout and the agent's GitHub work first: 'unblock' re-inspects GitHub and re-enters a phase, and only then does 'resume' continue the run.
  ```

  A LOCAL run prints a `new run:` line (`autoforge ... local run <the
  feature's absolute path>`) instead of `unblock` and `resume`.
- **"refusing to launch: a process AutoForge launches would be handed a
  GitHub or git credential":** the message names the setting (for example
  a credential helper in the repository's git configuration, a remote URL
  with a token, a GitHub entry in `~/.netrc`, or the startup files of a
  login shell that change a variable such as `GH_TOKEN`). Nothing was recorded:
  remove the setting (`autoforge doctor` lists it too) and run the same
  command again. A config that fails to load because `env_allowlist` names
  a credential variable: delete that entry
  ([Configuration: agent environment](configuration.md#agent-environment)).
- **"refusing to launch opencode: it would load the configuration of an
  organization connected to it":** OpenCode would load an organization's
  configuration, which may name an MCP server with a GitHub credential of
  its own, and nothing turns it off. Nothing was recorded: give AutoForge's
  OpenCode launches a database of their own (`OPENCODE_DB`, see
  [Configuration: agent environment](configuration.md#agent-environment))
  and run the same command again.
- **"the sandbox cannot be established ..."** (`SandboxLaunchRefusedError`,
  `sandbox.mode: required`): the launch did not run, and nothing ran
  without the sandbox. The message names the cause: a host that cannot
  enforce (not Linux, no `bwrap`, user namespaces refused, a kernel without
  Landlock ABI 6), a sandbox directory that is a link, or a path and the
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
  `GIT_ALTERNATE_OBJECT_DIRECTORIES=<repo>/.git/autoforge/sandbox/<n>/objects git -C <worktree> log`.
- **Merge verification keeps saying "inconclusive":** checks still running,
  mergeability unknown or a transient GitHub failure; re-run
  `autoforge resume --allow-merge`. After `merge.max_verification_attempts`
  attempts the run is `BLOCKED`.
- **The state file is unreadable:** `run`, `resume`, `step` and `status`
  exit 2 and never overwrite it. `run --force` moves it aside as
  `state.json.corrupt-<timestamp>` (never deletes it) and starts a new run;
  see [State and recovery](agent-guides/state-and-recovery.md#persistent-state).
- **`LockError`:** another controller holds the repository lock, or the lock
  entry under `.git/autoforge/` is not a regular file with a single name; see
  [State and recovery: locking](agent-guides/state-and-recovery.md#locking).
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

## Logging and redaction

Every agent invocation is logged under `<state dir>/logs/<run-id>/`. Logs
and CLI output pass through baseline secret redaction (`GITHUB_TOKEN`,
`GH_TOKEN`, `*_API_KEY`, `Authorization: Bearer` / `Basic`, `ghp_*`,
`github_pat_*`, `sk-*`, JWTs, the `user:password@` part of a URL, …),
state-derived output included: `status` and `status --json` redact what they
print (a `block_reason` echoes agent text; a journal can be hand-edited)
without rewriting `state.json`. No environment dump is ever written.
Baseline only, with no claim of completeness. See
[Secrets and logging](agent-guides/secrets-and-logging.md).
