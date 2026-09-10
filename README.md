# AutoForge / 智铸

**Autonomous orchestration for AI-driven software development.**

智铸——AI 驱动的软件工程自动化控制器。

> **Early development (Phase 2).** The first real AI development loop is
> wired end to end — GitHub issue → Claude Code implementation → PR
> verification → OpenCode review → Claude Code remediation → repeated review
> → `READY_FOR_MERGE`. **Merging is opt-in and controller-owned:** by default
> the loop stops at `READY_FOR_MERGE` for a human; with the merge safety gate
> open (`safety.allow_merge: true` in config **and** `--allow-merge` on the
> CLI) the controller itself verifies the PR on GitHub and merges it — agents
> never merge. Nothing here is production ready yet.

## What AutoForge is (and is not)

AutoForge is **not another coding agent**. It is a **deterministic
orchestration layer** that drives existing agents through the software
lifecycle and refuses to trust anything they claim without checking it
against GitHub:

```text
Issue → ANALYZE_EXECUTE (Claude Code) → PR → REVIEW (OpenCode)
       → clean ───────────────────────────────▶ READY_FOR_MERGE
       → findings ────────────────────────────▶ FIX (Claude Code) → REVIEW …
       → excessive / low-finding stagnation ──▶ REPLAN_REEXECUTE (OpenCode)
                                                    → replacement PR → REVIEW round 1
                                       ├─ merge gate closed (default): stops here; human merges
                                       └─ gate open: controller verifies on GitHub
                                          → MERGE (gh pr merge by the controller, no agent)
                                          → UPDATE_EPIC (agent picks the next issue;
                                             controller verifies it on GitHub, or DONE)
```

AutoForge itself never writes business code. It:

- holds the workflow state machine (phases + legal transitions),
- selects the execution profile (which provider/model/effort for which round),
- renders strictly-validated prompts from file templates,
- invokes `claude` / `opencode` through provider adapters and `gh` through a
  typed `GitHubClient` (argv lists only, never a shell) — read-only except for
  the gated, controller-owned merge (`gh pr merge --match-head-commit`) and
  the disarming of any auto-merge that call left behind,
- parses the machine-readable `CONTROL_RESULT` protocol from agent stdout,
- **verifies every claim** (PR exists / is OPEN / HEAD SHA / branch, review
  comment exists on the right PR for the right round and SHA, follow-up issue
  exists, fix actually moved HEAD) before advancing state atomically,
- **merges only itself, only when told to, and only what was reviewed**: with
  the safety gate open it re-verifies the PR on GitHub (open, at the reviewed
  HEAD, checks green, mergeable, no auto-merge / merge queue), merges bound to
  that exact HEAD, and counts the merge only after GitHub reports `MERGED`,
- logs every invocation (redacted) under `.autoforge/logs/<run-id>/`.

It is a **single-machine client tool, not a distributed service**: one process
on your own machine, driving your `git`, `gh`, and agent CLIs under your
credentials, with local per-checkout state in `.autoforge/` and a single
OS-level repository lock instead of any coordinator. The expected failure modes
are a `Ctrl-C`, a killed process, and a reboot — which is why side effects are
checkpointed before they are performed. What *is* remote and concurrent is
GitHub and the humans acting on it, which is why nothing GitHub-side is
believed without being read back. Work happens on a dedicated feature branch,
ideally in a per-issue `git worktree`; AutoForge never commits to the default
branch and never creates or cleans up local branches or worktrees itself.

## Architecture

```text
src/autoforge/
    cli.py            argparse CLI: doctor / run / step / resume / status
    engine.py         ControllerEngine — step() primitive, run() loops step(),
                      per-phase verification via gh, recovery, correction retry
    transitions.py    Phase enum + legal edges + decide_next_phase (pure)
    state.py          AutoForgeState + atomic save/load (.autoforge/state.json)
    config.py         file-or-defaults config (yaml/toml/json), profiles, safety gate
    profiles.py       review-round routing plus replan_reexecute profile (pure function)
    providers.py      AgentProvider adapters: ClaudeCodeProvider, OpenCodeProvider,
                      ScriptedProvider (tests); CLI flags live only here
    executor.py       subprocess abstraction: argv, timeout, process-tree kill
    result_parser.py  <<<CONTROL_RESULT>>> extraction + typed per-phase results
    validation.py     typed GitHub URL refs (issue / PR / comment), remote parsing
    github.py         typed GitHubClient over `gh`: reads (PRs, issues, comments,
                      checks, merge queue) + the controller-owned merge / disarm writes
    doctor.py         read-only environment checks
    locking.py        flock(2) repository lock, keyed by the git common dir
                      (<repo>/.git/autoforge/controller.lock), never by state_dir
    runlog.py         per-run logs (.autoforge/logs/<run-id>/), redacted
    redaction.py      baseline secret masking for logs / CLI output
    errors.py         Configuration/State/Transition/Lock/Execution/
                      ControlResult/GitHub/Verification taxonomy
    replan.py         deterministic replan trigger + bounded historical evidence collector
    replan_txn.py     the REPLAN_REEXECUTE transaction: durable stages, the causal
                      provenance marker, and every acceptance predicate (pure)
    prompts/          common.md (trust boundary) + phase templates + correction.md
```

Key design points:

- **Transition logic lives only in `transitions.py`** — never in CLI handlers.
- **`step()` is the core primitive**; `run()`/`resume` just loop it until a
  stop phase (`READY_FOR_MERGE`, `DONE`, `BLOCKED`, `FAILED`). With the merge
  gate open (`safety.allow_merge: true` **and** `--allow-merge`)
  `READY_FOR_MERGE` is no longer a stop phase: the loop continues through the
  controller-side verification, `MERGE` and `UPDATE_EPIC`.
- **Never trust the agent.** A `CONTROL_RESULT` is a *claim*. The controller
  re-reads GitHub through `gh` and raises `VerificationError` (phase
  unchanged) on any mismatch. Agent claims that cannot be verified never
  advance the state machine.
- **Review SHA binding.** Before every review the controller fetches the PR
  HEAD and binds the round to that SHA. The reviewer must report the same SHA,
  the posted comment must carry the same SHA, and if HEAD moves during the
  review the round is marked stale and the latest HEAD is reviewed again.
- **Findings vs observations.** Any finding (`blocked` / `non-blocked` /
  `nit`) forces a fix round; the controller enforces
  `needs_fix_round == (len(findings) > 0)`. Non-actionable remarks belong in
  Observations and do not block.
- **Dry-run is side-effect-free**: no subprocess, no `gh` call, no state
  write, no lock — it only prints the plan (phase, provider, model/effort,
  round, template, variables, command, expected transition), redacted.
- **Trust boundary**: `prompts/common.md` declares GitHub issues/PRs/comments,
  source, tests, and logs **untrusted data**; controller instructions and the
  repo's `AGENTS.md`/`CLAUDE.md` outrank them.
- **Atomic persistence**: temp file + fsync + `os.replace`; corrupted state
  fails loudly and is never silently overwritten: `run`, `resume`, `step` and
  `status` all exit 2 on an unreadable or foreign-protocol `state.json`
  (bad JSON, unknown phase, invalid UTF-8, dangling symlink, or a
  non-regular entry such as a FIFO, socket, device or directory — the entry
  is inspected and opened non-blocking, so a FIFO never hangs the command),
  and `run --force` moves the entry aside as `state.json.corrupt-<timestamp>`
  instead of deleting it (a symlink is archived as a link; its target is
  never touched; a directory cannot be archived and is refused).
  `run` inspects, decides, quarantines, writes the first
  state **and executes it** under one continuous controller lock, so a
  concurrent controller can never be quarantined or overwritten on a stale
  verdict, nor slip in between the first save and the engine loop.
- **The REVIEW/FIX loop is bounded by the controller.** `workflow.max_review_rounds`
  (default 20) caps completed review rounds per PR; a round at the cap that
  still has findings triggers an eligible replan, or goes to `BLOCKED` instead
  of starting a FIX that could never be reviewed. Stagnation detection: consecutive rounds
  whose `required_resolution` texts are identical
  (`workflow.stagnation_identical_rounds`, default 2) or whose finding count
  does not change (`workflow.stagnation_unchanged_count_rounds`, default 3)
  go to `BLOCKED`, unless the loop has already reached
  `review.replan.soft_threshold` (default round 12), in which case they
  trigger an eligible replan instead.
  `workflow.max_total_steps` (default 300) is a cumulative budget for the whole
  run measured on the persisted `step_count`, so `resume` continues it rather
  than resetting it (`--max-steps` bounds one invocation only). Failed agent
  invocations consume neither a review round nor the history. Every bound
  ends in `BLOCKED` with the reason; findings and the PR stay for a human.
- **Replanning is controller policy, not reviewer advice.** After a verified
  review with findings, `review.replan` defaults to a hard trigger at round 20,
  or from round 12 (`soft_threshold`) either three trailing review rounds each
  containing at most two actionable findings, or a `workflow.stagnation_*`
  verdict. `soft_threshold` gates *every* stagnation trigger: before it, a
  stagnant loop is `BLOCKED` for a human as documented above, because one
  ineffective FIX round is too weak a signal to discard a whole PR. It
  preserves compact finding metadata and selected
  review comments, then asks the separate `replan_reexecute` high-effort
  profile to independently rebuild from the latest verified default branch.
  Closing the superseded PR is the controller's only destructive write on
  agent-produced work, so the whole phase runs as **one durable transaction**
  (`replan_txn.py`) with an explicit monotonic lifecycle — `PENDING`,
  `PREPARED`, `VERIFIED`, `SUPERSEDE_INTENT`, `COMPENSATING`, `SUPERSEDED`, or
  the terminal `REJECTED`. Before the agent is invoked, `PREPARED` checkpoints the source PR
  and its exact HEAD, the verified default branch, the complete review
  evidence, the identities of the PRs that already existed, and a random
  controller-generated transaction id. That id is the **only** accepted proof
  of causality: the replacement must publish it in an
  `<!-- autoforge-replan-transaction: {...} -->` marker in its PR body, which
  the controller reads back from GitHub. Shape is never proof — an unmarked PR
  is ignored, a PR that already existed is refused even if it carries a copied
  marker, and two claimants block. Candidates are searched repository-wide, so
  a marked PR the agent has not yet linked to the issue is refused for the
  missing linkage rather than missed and re-implemented. Before concluding no candidate
  exists, an exhaustive all-states listing (open, closed and merged) is consulted, so a
  replacement that was closed before recovery is rejected with the PR named instead of
  triggering a second implementation. The PR-number watermark is a proven numeric maximum
  over all states, never inferred from one creation-time-ordered node. The old PR is closed
  only while both sides still match their checkpoints — including the marker,
  re-read on the last read before the close. GitHub has no conditional close,
  so that comparison cannot be fused to the write: it is *completed after* it,
  and a checkpoint that moved inside the close window is compensated by
  reopening the source PR and blocking, never by accepting the close. That
  compensation is itself a decision, so `COMPENSATING` is persisted *before*
  the reopen: a crash after a successful reopen resumes into "finish undoing",
  never into a second close. `SUPERSEDE_INTENT` is persisted *before* the write
  so a crash resumes into disposition rather than a second attempt, and a separate
  `gh pr comment` posts an `<!-- autoforge-replan-close: … -->` receipt only after the
  controller observed its own close landing — never inside `gh pr close --comment`, whose
  comment predates the close. The receipt is what tells the controller's own close from a
  human's afterwards — a source found closed without this transaction's receipt blocks
  and is never reopened, a conclusive close failure is never adopted, and an open source
  already carrying the receipt (a prior close landed then reopened) is never closed again.
  `SUPERSEDED` is persisted before the replacement is installed into state, so
  both checkpoints are re-derived from GitHub once more on that last read; a
  replacement that was closed, moved or re-marked in that window blocks with
  both PRs named. A replan is refused outright while
  any recorded round's findings had to be truncated to stay within the state
  bounds, because closing the PR would be the moment those findings are lost.
  At most two replans per issue are allowed; a further eligible trigger blocks
  for human intervention. This retains failure knowledge while intentionally
  discarding implementation anchoring; it does not guarantee convergence.
  AutoForge deliberately performs no local branch or worktree cleanup during
  this lifecycle: leaving old local work untouched is safer than trying to
  infer ownership or discard uncommitted user changes.

## Workflow details

| Phase | Who | What the controller verifies afterwards |
|---|---|---|
| `INITIALIZING` | controller | cwd repo == issue repo, issue is in this repo, is not the EPIC, exists and is OPEN |
| `ANALYZE_EXECUTE` | Claude Code (`fable`, effort high) | existing open PR for the issue is recovered without re-running the agent; otherwise PR exists in this repo, is OPEN, HEAD SHA and branch match the claim |
| `REVIEW` | OpenCode (round 1 `openai/gpt-5.6-luna` high, rounds 2–5 `openai/gpt-5.6-terra` high, 6+ `openai/gpt-5.6-sol` medium, intentionally retained through the 20-round cap) | round number, reviewed SHA == bound HEAD, exactly one review comment on this PR with the `# AI Code Review — Round N` heading and the `ai-review-result` marker matching round/SHA/flag, findings invariant; then controller policy: an eligible replan (including workflow stagnation or the cap) enters `REPLAN_REEXECUTE`; an exhausted replan limit or no eligible replan blocks. Entering `REVIEW` past the cap (stale re-review, HEAD drift, resume) is refused before the reviewer runs |
| `FIX` | Claude Code (`fable`, effort high) | `previous_head_sha` == current HEAD, every open finding ID resolved (`fixed` / `follow_up_created` / `no_change_with_rationale`), follow-up issues exist in this repo and are OPEN, actual PR HEAD == `new_head_sha`, a `fixed` resolution moved HEAD |
| `REPLAN_REEXECUTE` | OpenCode (`replan_reexecute`, default `openai/gpt-5.6-terra`, effort high) | One durable transaction. `PREPARED` (written before the agent runs) checkpoints the source PR at its exact HEAD, the verified default branch, the complete historical findings, the identities of the already-open PRs, and a random transaction id; a round whose findings could not be persisted in full refuses the replan here and keeps the old PR. The replacement is found **only** by the transaction marker in its PR body — never by shape, never from the CONTROL_RESULT, and never among the pre-existing PRs; several claimants, a copied marker or an unusable one all block. It must additionally be a distinct OPEN PR of this repository, linked to the issue, on a distinct branch based on the verified default branch, and its marker must attest this transaction id, this execution attempt, passing tests and at least the preserved historical finding count. `VERIFIED` records its HEAD; immediately before the destructive write both sides are re-read and must still match the checkpoint exactly. `SUPERSEDE_INTENT` is persisted *before* `gh pr close` so a crash resumes into disposition, and a separate `gh pr comment` posts an `<!-- autoforge-replan-close: … -->` receipt only after the controller observed its own close landing (never inside `gh pr close --comment`); the receipt is what proves afterwards that the close was the controller's and not a human's; the close outcome is re-read from GitHub rather than inferred from the exit status, a conclusive close failure is never adopted, and an open source already carrying the receipt is never closed again. A checkpoint that moved inside the close window is undone under a durable `COMPENSATING` record written before the reopen, and the replacement is re-verified once more before it is installed into controller state. Only then does the controller close the old PR without merge and reset the replacement lifecycle so its next review is round 1. Every refusal is persisted as `REJECTED` and replayed by `resume`; a transient GitHub failure is left resumable instead. |
| `READY_FOR_MERGE` | nobody | holding state; `step`/`resume` refuse to continue unless the merge gate is open (`resume` only re-prints the banner). With the gate open (`step --allow-merge` / `resume --allow-merge`) it runs the full pre-merge verification below against GitHub *before* entering `MERGE`: closed / conflicting / failing / draft / queued PRs go to `BLOCKED` without ever reaching `MERGE`, HEAD drift -> `REVIEW`, an already-merged PR -> `MERGE` to reconcile; inconclusive data (checks running, mergeability unknown, GitHub unreachable / transient read failure) keeps the phase for `resume --allow-merge`, at most `merge.max_verification_attempts` times, then `BLOCKED`; a read that fails conclusively (bad credentials, permissions, unresolvable PR) -> `BLOCKED` at once |
| `MERGE` (gated) | controller, never an agent | last review clean and PR HEAD == reviewed HEAD; GitHub says PR is OPEN, not draft, every check succeeded, `mergeable=MERGEABLE`, `mergeStateStatus` `CLEAN`/`HAS_HOOKS`, no auto-merge armed, base branch has no merge queue; then `gh pr merge --<method> --match-head-commit <reviewed HEAD>`; counted only once GitHub reports `MERGED` at that HEAD. Conclusive negatives and conclusive read failures (bad credentials, permissions) -> `BLOCKED`; inconclusive data (checks running, mergeability unknown, transient read failure, post-merge re-read failed) stays in `MERGE` for `resume --allow-merge`, at most `merge.max_verification_attempts` times, then `BLOCKED`; HEAD drift -> `REVIEW` |
| `UPDATE_EPIC` | OpenCode (`update_epic` profile) | `next_issue_url` gets the `INITIALIZING` checks before the controller switches issues: parses as an issue URL of this repo (a foreign URL is never even queried), is neither the EPIC nor the just-finished issue (compared case-insensitively by repository + number, never by URL string), exists on GitHub and is OPEN. A rejected selection, or a transient GitHub failure while checking it, keeps the phase and `resume` asks the agent once more with the reason in its prompt; a second rejection -> `BLOCKED`. A conclusive GitHub failure (authentication, permissions, malformed data) -> `BLOCKED` immediately, without invoking the agent again. Only a verified issue reaches `ANALYZE_EXECUTE`; `null` -> `DONE` |

Recovery rules: if a step crashes after the agent created a PR, `resume`
re-enters `ANALYZE_EXECUTE`, finds the open PR (linked issue or
`autoforge/<n>` branch) and moves to `REVIEW` without running the agent. Two
or more candidate PRs → `BLOCKED` (the controller never guesses).
`REPLAN_REEXECUTE` has no separate recovery path at all: a fresh step and a
`resume` both call the same reducer over the persisted transaction, so the
normal and crash paths cannot drift apart about what is acceptable. Because
the transaction id is generated and persisted *before* the agent is invoked,
"crashed before invoking" and "crashed while the agent ran" are one state —
either a PR carrying that id exists, or none does — and a crash after the
replacement was created never causes a second implementation attempt.

Correction retry: when an agent exits 0 but its `CONTROL_RESULT` is missing or
invalid, the controller re-invokes it **once** with a correction prompt that
tells it to inspect real Git/GitHub state first and not repeat completed
operations. Non-zero exits, timeouts and verification failures are not
retried automatically; they leave the phase unchanged for `resume`.

## Prerequisites

- Python 3.11+ (managed via `uv`)
- `uv` ([install](https://docs.astral.sh/uv/getting-started/installation/))
- `git`, and `gh` (GitHub CLI, authenticated)
- `claude` (Claude Code CLI) for `analyze_execute` / `fix` profiles
- `opencode` (OpenCode CLI) for `review_*` profiles

Run `autoforge doctor` to check all of the above (read-only).

CLI flag syntax in `autoforge.example.yaml` was checked against the locally
installed CLIs (Claude Code 2.1.263, OpenCode 1.18.20, gh 2.100.0).

## Installation

```bash
uv sync                  # create .venv, install autoforge (editable) + dev tools
uv sync --extra yaml     # optional: full YAML config support (PyYAML)
uv run autoforge --help
```

## Basic usage

```bash
uv run autoforge doctor                 # environment checks (never mutates anything)
uv run autoforge doctor --json

# preview without touching anything (no subprocess, no gh call, no state file)
uv run autoforge run --epic https://github.com/owner/repo/issues/1 \
               --issue https://github.com/owner/repo/issues/2 --dry-run

# create a run and drive it until READY_FOR_MERGE / BLOCKED / FAILED
uv run autoforge run --epic https://github.com/owner/repo/issues/1 \
               --issue https://github.com/owner/repo/issues/2

uv run autoforge status            # human summary
uv run autoforge status --json     # machine-readable

uv run autoforge step              # exactly one phase step
uv run autoforge step --dry-run    # preview the next step

uv run autoforge resume            # continue until a stop phase / --max-steps
# --max-steps bounds one invocation; workflow.max_total_steps (config) bounds
# the whole run and is not reset by resume

# only with safety.allow_merge: true in config — the controller verifies on
# GitHub and merges itself (agents never merge); re-run to re-check
# inconclusive GitHub data (bounded by merge.max_verification_attempts)
uv run autoforge resume --allow-merge
```

When the loop reaches `READY_FOR_MERGE` with the merge gate closed (the
default) the CLI prints a banner with the issue, PR, review round and reviewed
HEAD, and states that automatic merge is disabled: a human merges the PR.
`resume` without the gate open re-prints the banner and runs nothing. With the
gate open (`resume --allow-merge` / `step --allow-merge`) the controller
continues through its own pre-merge verification, `MERGE` and `UPDATE_EPIC`.

URLs must be HTTPS GitHub issue URLs, and EPIC + issue must be in the **same
repository** as the current working directory (cross-repo runs are rejected).

## State directory

Default `.autoforge/` (overridable via `--state-dir` or config):

```text
.autoforge/
    state.json          # persisted run state (atomic writes)
    state.json.corrupt-<timestamp>   # unreadable state moved aside by 'run --force'
    logs/<run-id>/
        events.jsonl                       # one line per agent invocation
        <seq>-<phase>-<attempt>/
            request.json                   # profile, model, effort, command, timeout
            prompt.md                      # rendered prompt (redacted)
            execution.json                 # exit code, timing, timed_out, error
            stdout.log / stderr.log        # redacted
            control-result.json            # parsed CONTROL_RESULT (when valid)
```

The controller lock is deliberately **not** in the state directory. It lives
in the repository's git directory, `<repo>/.git/autoforge/controller.lock`
(resolved with `git rev-parse --git-common-dir` from the controller's working
directory), so it is the same file for every `--state-dir`, every
subdirectory and every linked `git worktree` of one checkout. `run`, `step`
and `resume` therefore require the working directory to be inside a git
repository (dry-run does not).

State records `current_pr_url`, `current_branch`, `current_head_sha`,
`reviewed_head_sha`, `review_round`, `last_review_comment_url`,
`last_review_needs_fix`, `open_findings`, `last_fix_resolutions`,
`review_history` (one entry per completed review round of the current PR:
round, reviewed SHA, result, finding count, fingerprint of the requested
resolutions), `step_count` (cumulative for the run, never reset), `attempt`
and `block_reason`. It also records `execution_attempt` (initial implementation
is 1), `escalation_count` (completed replans), `superseded_prs` (each entry
naming the transaction that superseded it), and the durable
`replan_transaction` record described above — the controller's intent journal,
which `resume` replays rather than re-deriving a decision from GitHub facts. State keeps compact finding summaries and review
comment URLs, rather than copying unbounded PR discussion bodies. Those
summaries are bounded (100 findings per round, 2000 characters per required
resolution); a round that hits either bound is marked `evidence_truncated`, and
because superseding a PR deletes the controller's only record of its findings,
such a round makes the replan policy block for a human instead.
Relevant controller verification failures are retained as a bounded per-issue
list and supplied to the replan prompt; all PR comment text remains GitHub
audit data rather than state payload.

## Security model

- One controller per repository (flock); a second instance exits with `LockError`.
  The lock is keyed by the **repository identity**, not by a caller-selectable
  path: it is `<git common dir>/autoforge/controller.lock`, resolved with
  `git rev-parse --git-common-dir` from the controller's working directory, so
  two controllers cannot escape each other by passing different `--state-dir`
  values, by starting from different subdirectories with the default relative
  `.autoforge`, or by using a linked `git worktree` of the same checkout. A
  working directory outside a git repository has no lock to take and is
  refused (exit 2, nothing written). Two independent *clones* of one GitHub
  repository are two repositories to this lock and are not coordinated.
  `run`, `step` and `resume` take the lock **before** `state.json` is read or
  created and keep it until their last step has been persisted, so a state
  snapshot loaded before the lock is never executed and no second controller
  can take over the repository mid-command. Dry-run takes no lock and runs no
  `git`. Below the git common dir no path component is followed through a
  symlink: the `autoforge` directory is created with `mkdir` and opened
  relative to the git directory's descriptor with `O_DIRECTORY | O_NOFOLLOW`,
  so a symlink or a file in its place is refused (exit 2) and its target is
  never touched; `controller.lock` is then opened relative to that validated
  directory descriptor. The lock entry itself must be a regular file with a
  single name: `controller.lock` is opened with `O_NOFOLLOW` and
  `fstat`-checked, so a symlink, FIFO, socket, device or directory in its
  place, or a hard link that shares its inode with another file, is refused
  with `LockError` (exit 2) before anything is written — a tampered path or
  entry can neither redirect the PID write to another file nor produce a
  traceback. These checks cover entries that are damaged or tampered *at
  rest*; a writer with write access to the git directory who races the
  check-then-write window, or swaps the `autoforge` directory for another
  directory between two controller invocations, has the controller's own
  privileges and is outside the trust boundary (the git directory is assumed
  to be writable only by the operating user).
- **Automatic merge is off by default and opt-in only.** The `MERGE` phase is
  reachable only from `READY_FOR_MERGE` and only when **both**
  `safety.allow_merge: true` is set in config **and** `--allow-merge` is
  passed on the CLI. With the gate closed `run`/`resume` stop at
  `READY_FOR_MERGE` and a human merges.
- **Agents never merge.** When the gate is open, the *controller* performs the
  merge itself: `gh pr merge --<merge.method> --match-head-commit <reviewed HEAD>`
  through `GitHubClient`, with no prompt and no agent invocation. Every agent
  prompt carries the unconditional rule "never merge a pull request".
- **Pre-merge verification is controller-side and fails closed.** It runs in
  `READY_FOR_MERGE` (before `MERGE` is entered) and again in `MERGE` (before
  the write), reading only controller state and GitHub — never an agent
  claim. GitHub must report: PR open at the reviewed HEAD, not a draft, every
  check in the status rollup succeeded (all checks, not only required ones),
  `mergeable = MERGEABLE`, `mergeStateStatus` in `CLEAN`/`HAS_HOOKS`, no
  auto-merge armed, and no merge queue on the base branch (`gh pr merge`
  would otherwise arm auto-merge or enqueue instead of merging, leaving an
  asynchronous merge the controller does not own). Conclusive negatives
  (closed PR, conflict, failing check, branch protection, queue) -> `BLOCKED`;
  inconclusive data (checks still running, `mergeable = UNKNOWN`, or the PR /
  merge-queue read itself failing transiently: timeout, connection error,
  5xx, rate limit) raises and keeps the phase so `resume --allow-merge` (or
  `step --allow-merge`) re-checks, one attempt per invocation, at most
  `merge.max_verification_attempts` times (default 5), then `BLOCKED`. A read
  that fails conclusively (bad credentials, missing permissions, a PR that no
  longer resolves) is `BLOCKED` immediately: re-running would not change it.
  Either way nothing is merged. HEAD drift -> `REVIEW`.
- **Post-merge is reconciled from GitHub.** The merge is counted only after
  GitHub reports `MERGED` at the reviewed HEAD (idempotently, across crashes).
  If `gh pr merge` returns but the PR is still open, any auto-merge that call
  armed is disabled again (`gh pr merge --disable-auto`) and the run is
  `BLOCKED` — unless the PR is open at a *different* HEAD (pushed between the
  verification and the write, so `--match-head-commit` refused it) and no
  asynchronous merge is pending: then nothing unreviewed merged, the clean
  review is stale and the run goes back to `REVIEW`. If the post-merge re-read fails, the outcome is treated as
  unknown: the run stays in `MERGE` and `resume --allow-merge` re-inspects GitHub (an
  already-merged PR is recovered and counted once; an open one is re-verified),
  bounded by the same `merge.max_verification_attempts`, then `BLOCKED`.
- Logs and CLI output pass through baseline secret redaction (`GITHUB_TOKEN`,
  `GH_TOKEN`, `*_API_KEY`, `Authorization: Bearer`, `ghp_*`, `sk-*`, …). No
  environment dump is ever written. Baseline only — no claim of completeness.
- No `os.system` / `shell=True` anywhere; prompts travel as a single argv
  element so shell metacharacters in issue text cannot be interpreted.
  Agents run in a new session and the whole process group is killed on timeout.
- **Runs cannot loop forever.** The review-round cap, stagnation detection
  and the cumulative step budget (`workflow:` in the config) are controller
  invariants checked before an agent is invoked; hitting one is `BLOCKED`, a
  terminal phase that `resume` does not re-enter.
- `doctor` is read-only apart from a temp file it creates and removes in the
  state directory.
- Runtime state, logs, locks, and local config overrides are git-ignored.

## Configuration

```bash
cp autoforge.example.yaml autoforge.yaml
```

Logical profile names (`analyze_execute`, `fix`, `review_round_1`,
`review_round_2_5`, `review_round_6_plus`, `replan_reexecute`, `update_epic`) are stable (there is
no `merge` profile: the controller merges, see `merge:` in the example file);
edit the file to change model identifiers, effort, timeouts and provider
options without touching controller source. Provider-specific flags are built
by the adapters in `providers.py`; the engine never hard-codes CLI syntax.
YAML (`uv sync --extra yaml` for PyYAML, else a minimal built-in subset
parser), TOML (stdlib), and JSON (stdlib) are accepted.

## Development

```bash
make sync       # uv sync (.venv + dev tools)
make test       # uv run pytest
make lint       # uv run ruff check src tests
make typecheck  # uv run mypy src
```

Tests never call real Claude Code, OpenCode or GitHub write APIs. Agents are
replaced by a `ScriptedProvider` and GitHub by an in-memory fake; the
executor tests use real local Python subprocesses only.

## Current maturity

**Supported now (Phase 2):** GitHub issue → Claude Code implementation → PR
verification via `gh` → OpenCode review with round-based model routing →
Claude Code remediation of finding IDs → repeated review with SHA binding →
`READY_FOR_MERGE`; recovery of an already-created PR; bounded correction
retry for malformed results; `doctor`; redacted per-invocation logs.

**Explicitly not yet:** automatic merge (gated off; when opened, the
controller-owned `MERGE` step — including its pre-merge mergeability / check
verification — and `UPDATE_EPIC` are exercised only against the in-memory
fake, never against real services), controller-owned EPIC batching (#13),
unattended production operation, CI checks as a review input, dequeuing a PR
from a merge queue (the controller refuses to merge into queue-protected
branches instead).
