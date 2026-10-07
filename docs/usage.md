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
                                   # raw `replan_transaction` beside it is readable

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
Issue → ANALYZE_EXECUTE (Claude Code) → PR → REVIEW (OpenCode)
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
| `ANALYZE_EXECUTE` | Claude Code (`analyze_execute`) | an open PR already carrying the issue's `ai-implementation` marker is adopted without running the agent (two block); afterwards the PR exists in this repo, is OPEN, its HEAD SHA and branch match the claim, and it is the only open PR carrying the marker |
| `REVIEW` | OpenCode (`review_round_1`, `review_round_2_5`, `review_round_6_plus`) | the round is bound to the PR, HEAD, base and merge base read from GitHub; exactly one review comment for that round and revision; `needs_fix_round == (len(findings) > 0)`; then controller policy picks `FIX`, `READY_FOR_MERGE`, `REPLAN_REEXECUTE` or `BLOCKED` |
| `FIX` | Claude Code (`fix`) | the PR is still at the reviewed revision (else back to `REVIEW` without running the fixer); every open finding resolved; a `fixed` resolution moved HEAD; a claimed follow-up issue exists and is OPEN |
| `REPLAN_REEXECUTE` | OpenCode (`replan_reexecute`) | one durable transaction; the replacement PR is found only by the controller's transaction marker, and the old PR is closed by the controller only while both PRs still match their checkpoints |
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
against GitHub again before it issues anything. A LOCAL run cannot be
unblocked. The decision table is in
[Workflow: leaving BLOCKED](agent-guides/workflow.md#leaving-blocked-the-operators-unblock).

### DONE and FAILED

`DONE` is reached when `UPDATE_EPIC` reports no next issue. `DONE` and
`FAILED` have no outgoing edges; `autoforge run` starts a new run.

## Watching an agent run

While an agent runs, `run`, `step` and `resume` (and `local run`) print live
progress lines to **stderr**, and each step's outcome to stdout as soon as
that step finishes, not after the whole loop. A progress line is prefixed
with the time since the launch, the phase and, in a remote run, the issue:

```text
[00:00:00 ANALYZE_EXECUTE #160] launching analyze_execute (claude, model fable, effort high), timeout 1800s, attempt 1, worktree /repo/.git/autoforge/worktrees/160, log .autoforge/logs/<run-id>/001-analyze_execute-1
[00:00:04 ANALYZE_EXECUTE #160] agent started (claude-fable-5-1)
[00:00:31 ANALYZE_EXECUTE #160] thinking… ~12k tokens
[00:01:02 ANALYZE_EXECUTE #160] Read src/autoforge/engine.py
[00:01:09 ANALYZE_EXECUTE #160] Grep def _invoke_phase
[00:03:40 ANALYZE_EXECUTE #160] Edit src/autoforge/engine.py
[00:04:12 ANALYZE_EXECUTE #160] Bash Run the test suite
[00:06:12 ANALYZE_EXECUTE #160] still running, last activity 14s ago, 312 events
[00:09:55 ANALYZE_EXECUTE #160] agent exited 0, 841 progress events
[run-…] ANALYZE_EXECUTE -> REVIEW: PR https://github.com/…/pull/161 verified
```

- **Before the launch**, one line names the profile, provider, model and
  effort, the timeout, the attempt, the working directory and the step's log
  directory.
- **While it runs**: one line per tool the agent starts, with one
  allow-listed input (the file of a read, edit or write, the pattern of a
  search, the *description* of a shell command, never the command itself,
  a tool's result, thinking or the agent's text); thinking and writing are
  shown at most once every 30 s; a failed tool says so; and when nothing has
  been printed for two minutes a heartbeat says how long ago the agent last
  did anything.
- **After it returns**, one line says how it ended (exit status, timeout or
  provider failure).

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

## Dry run

A dry run invokes no agent, runs no verification command, writes nothing to
GitHub, git or the state directory (no state, no log), takes no lock and
creates no agent worktree. It prints the plan (phase, provider,
model/effort, round, template, variables, command, expected transition),
redacted. What it reads depends on the command:

- `run --dry-run` previews a new run and launches no subprocess: no `git`,
  no `gh`.
- `step --dry-run` and `resume --dry-run` preview the existing run and make
  no `gh` call. Without `--state-dir` they first run two read-only
  `git rev-parse` calls to find which run is meant, a remote one under
  `.autoforge/` or a local one in the git directory; `--state-dir` skips
  that lookup.
- `unblock --dry-run` does the same lookup, then reads GitHub to report the
  decision it would make.

## Where things live

- **State and logs:** `.autoforge/` in the working directory by default
  (`--state-dir` or `state_dir` in config): `state.json` plus one log
  directory per run with the redacted prompt, output, and parsed result of
  every agent invocation. Layout:
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

### How agents are separated from your checkout

Every REMOTE agent runs in that per-issue worktree, so its working directory
contains neither `.autoforge/`, the lock, nor the operator's uncommitted
work, and a `git clean -fdx` there cannot reach controller state. It starts
from an allow-listed environment: `execution.env_allowlist` (a documented
default of PATH, HOME, locale, temporary directory, XDG directories,
`SSH_AUTH_SOCK`, git author and SSH settings, `GH_TOKEN` / `GITHUB_TOKEN`
and the proxy and CA variables) plus `env_allowlist_extra`, plus the names
the provider adapter declares for its own CLI (`ANTHROPIC_*` and `CLAUDE_*`
for Claude Code; `OPENCODE_*`, `OPENAI_*`, `ANTHROPIC_*`, `GEMINI_*`,
`GOOGLE_*` for OpenCode); nothing else in the operator's environment is
inherited, and the names (never the values) are recorded in the invocation's
`request.json`. Before and after each invocation the controller reads HEAD
and the checked-out branch of its own checkout; a change enters `BLOCKED`
with the drift in the reason, nothing is rolled back, and `unblock`
reconciles the agent's GitHub work. This is process and filesystem-level
separation, not a sandbox: the agent still runs as the operator with network
access and the allow-listed credentials.

AutoForge never commits to the default branch, never deletes a worktree it
created, and never creates or cleans up local branches or any other
worktree: leaving old local work untouched is safer than trying to infer
ownership or discard uncommitted user changes.

## Troubleshooting and recovery

- **The process was interrupted** (`Ctrl-C`, a killed process, a reboot):
  run `autoforge resume`. State is written atomically, and every phase that
  launches an agent reads GitHub first, so work the agent already did (an
  open PR, a posted review comment, a pushed fix) is adopted rather than
  repeated. See
  [Workflow: re-entering a phase](agent-guides/workflow.md#re-entering-a-phase).
- **An agent failed, timed out or returned a malformed result:** a malformed
  `CONTROL_RESULT` from an agent that exited 0 is retried with a correction
  prompt (`execution.max_correction_attempts`, default once). A non-zero
  exit, a timeout or a verification failure is not retried automatically and
  leaves the phase unchanged for `resume`. Inspect the invocation under
  `<state dir>/logs/<run-id>/`.
- **The run is `BLOCKED`:** read `autoforge status`, fix the cause, then
  `autoforge unblock --reason "..."` and `autoforge resume` (see above).
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
  `resume` continues the transaction from its persisted stage. A transient
  GitHub failure inside the transaction leaves it resumable the same way.
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
