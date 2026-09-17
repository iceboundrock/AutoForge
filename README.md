# AutoForge / 智铸

**Autonomous orchestration for AI-driven software development.**

智铸：AI 驱动的软件工程自动化控制器。

> **Early development (Phase 2).** The first real AI development loop is
> wired end to end: GitHub issue → Claude Code implementation → PR
> verification → OpenCode review → Claude Code remediation → repeated review
> → `READY_FOR_MERGE`. Merging is opt-in and controller-owned: by default the
> loop stops at `READY_FOR_MERGE` for a human; with the merge safety gate
> open (`safety.allow_merge: true` in config and `--allow-merge` on the CLI)
> the controller itself verifies the PR on GitHub and merges it. Agents never
> merge. Nothing here is production ready yet.

## What AutoForge is (and is not)

AutoForge is not another coding agent. It is a deterministic orchestration
layer that drives existing agents through the software lifecycle and refuses
to trust anything they claim without checking it against GitHub:

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

There is a second, explicit workflow for when there is no GitHub to
orchestrate: an interview, a scratch repository, an offline machine. Local
mode (`autoforge local`) drives a feature Markdown file against your working
tree with the same controller and the same verification discipline, and makes
zero `gh` invocations:

```text
features/<slug>.md → ANALYZE_EXECUTE → REVIEW → clean ────▶ DONE
                                             → findings ──▶ FIX → REVIEW …
```

See [Local mode (no GitHub)](#local-mode-no-github).

AutoForge itself never writes business code. It:

- holds the workflow state machine (phases + legal transitions),
- selects the execution profile (which provider/model/effort for which round),
- renders strictly-validated prompts from file templates,
- invokes `claude` / `opencode` through provider adapters and `gh` through a
  typed `GitHubClient` (argv lists only, never a shell), all of it read-only
  except for the gated, controller-owned merge
  (`gh pr merge --match-head-commit`) and the disarming of any auto-merge
  that call left behind,
- parses the machine-readable `CONTROL_RESULT` protocol from agent stdout,
- verifies every claim (PR exists / is OPEN / HEAD SHA / branch, review
  comment exists on the right PR for the right round and SHA, follow-up issue
  exists, fix actually moved HEAD) before advancing state atomically,
- merges only itself, only when told to, and only what was reviewed: with
  the safety gate open it re-verifies the PR on GitHub (open, at the reviewed
  HEAD, checks green, mergeable, no auto-merge / merge queue), merges bound to
  that exact HEAD, and counts the merge only after GitHub reports `MERGED`,
- logs every invocation (redacted) under `.autoforge/logs/<run-id>/`.

It is a single-machine client tool, not a distributed service: one process
on your own machine, driving your `git`, `gh`, and agent CLIs under your
credentials, with local per-checkout state in `.autoforge/` and a single
OS-level repository lock instead of any coordinator. The expected failure modes
are a `Ctrl-C`, a killed process, and a reboot, which is why side effects are
checkpointed before they are performed. What *is* remote and concurrent is
GitHub and the humans acting on it, which is why nothing GitHub-side is
believed without being read back. Work happens on a dedicated feature branch,
ideally in a per-issue `git worktree`; AutoForge never commits to the default
branch and never creates or cleans up local branches or worktrees itself.

## Architecture

```text
src/autoforge/
    cli.py            argparse CLI: doctor / run / step / resume / status /
                      local (init, run, doctor)
    engine.py         ControllerEngine — step() primitive, run() loops step(),
                      per-phase verification via gh, recovery, correction retry
    transitions.py    Phase enum + legal edges + decide_next_phase (pure)
    state.py          AutoForgeState + atomic save/load (.autoforge/state.json)
    config.py         file-or-defaults config (yaml/toml/json), profiles, safety gate
    profiles.py       review-round routing plus replan_reexecute profile (pure function)
    providers.py      AgentProvider adapters: ClaudeCodeProvider, OpenCodeProvider,
                      ScriptedProvider (tests); CLI flags live only here
    executor.py       subprocess abstraction: argv, timeout, process-tree kill, bounded capture
    result_parser.py  <<<CONTROL_RESULT>>> extraction + typed per-phase results
    validation.py     typed GitHub URL refs (issue / PR / comment), remote parsing
    github.py         typed GitHubClient over `gh`: reads (PRs, issues, comments,
                      checks, merge queue) + the controller-owned merge / disarm writes
    local_workspace.py  LOCAL mode's trust boundary: the controller's own walk of
                      the working tree (total classification -> workspace
                      fingerprint), the git anchor (HEAD/branch), and the frozen
                      feature specification (resolve / hash / re-verify)
    safefs.py         the one filesystem capability boundary: SafeRoot, every name
                      below it opened with dir_fd= and O_NOFOLLOW; whole-file
                      writes replace a name, never an inode
    doctor.py         read-only environment checks (remote + the local subset)
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
                      + local_common.md and the local_* phase templates
```

Design points:

- **Transition logic lives only in `transitions.py`**, never in CLI handlers.
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
  Observations and do not block. A review round is also bounded in size
  (findings per round, `required_resolution` / `title` / `location` / `id`
  length; see `result_parser.py`): an oversized result is rejected whole and the
  reviewer is asked to re-emit it, never clipped, because the findings are
  the work the next fix round has to act on. A fix round is bounded the same
  way (resolutions per fix, `rationale` length, the shape of `commit_sha`,
  the length of any URL), because every resolution is persisted whole. A
  finding's `title` and `location` are one line of printable text (no
  control characters), and a `required_resolution` or `rationale` may carry
  newlines and tabs only; the parser refuses, rather than escapes, anything
  the fix prompt could not show as written.
- **Dry-run is side-effect-free**: no subprocess, no `gh` call, no state
  write, no lock. It only prints the plan (phase, provider, model/effort,
  round, template, variables, command, expected transition), redacted.
- **Trust boundary**: `prompts/common.md` declares GitHub issues/PRs/comments,
  source, tests, and logs untrusted data; controller instructions and the
  repo's `AGENTS.md`/`CLAUDE.md` outrank them.
- **Atomic persistence**: temp file + fsync + `os.replace`; corrupted state
  fails loudly and is never silently overwritten: `run`, `resume`, `step` and
  `status` all exit 2 on an unreadable or foreign-protocol `state.json`
  (bad JSON, unknown phase, invalid UTF-8, dangling symlink, or a
  non-regular entry such as a FIFO, socket, device or directory; the entry
  is inspected and opened non-blocking, so a FIFO never hangs the command),
  and `run --force` moves the entry aside as `state.json.corrupt-<timestamp>`
  instead of deleting it (a symlink is archived as a link; its target is
  never touched; a directory cannot be archived and is refused).
  `run` inspects, decides, quarantines, writes the first
  state and executes it under one continuous controller lock, so a
  concurrent controller can never be quarantined or overwritten on a stale
  verdict, nor slip in between the first save and the engine loop.
- **The REVIEW/FIX loop is bounded by the controller.** `workflow.max_review_rounds`
  (default 20) caps completed review rounds per PR; a round at the cap that
  still has findings triggers an eligible replan, or goes to `BLOCKED` instead
  of starting a FIX that could never be reviewed. Stagnation detection: consecutive rounds
  whose `required_resolution` texts are identical
  (`workflow.stagnation_identical_rounds`, default 2) or whose finding count
  does not change while some `required_resolution` recurs across them, an
  A/B/A ping-pong (`workflow.stagnation_unchanged_count_rounds`, default 3),
  go to `BLOCKED`, unless the loop has already reached
  `review.replan.soft_threshold` (default round 12), in which case they
  trigger an eligible replan instead. A reviewer that raises a genuinely new
  finding every round is progress and only meets the round cap. Both
  stagnation settings are `0` (rule disabled) or `>= 2`: they compare
  consecutive rounds, so a window of `1` is rejected by the config loader
  rather than silently disabling the rule. The recurrence evidence persisted
  per round is bounded, and a round whose digests were clipped keeps the
  count-only behaviour instead of being read as "nothing recurred".
  `workflow.max_total_steps` (default 300) is a cumulative budget for the whole
  run measured on the persisted `step_count`, so `resume` continues it rather
  than resetting it (`--max-steps` bounds one invocation only). Failed agent
  invocations consume neither a review round nor the history. Every bound
  ends in `BLOCKED` with the reason; findings and the PR stay for a human.
- **Replanning is controller policy, not reviewer advice.** After a verified
  review with findings, `review.replan` defaults to a hard trigger at round 20,
  or from round 12 (`soft_threshold`) either three trailing review rounds each
  containing at most two actionable findings, or a `workflow.stagnation_*`
  verdict. The three-round window deliberately counts rounds of entirely new
  findings (no recurring resolution is required): recurrence is what the
  `workflow.stagnation_*` rules detect, so the window rule is the trigger for
  the long tail of small, fresh findings that never ends. `soft_threshold`
  gates *every* stagnation trigger: before it, a stagnant loop is `BLOCKED`
  for a human as documented above, because one ineffective FIX round is too
  weak a signal to discard a whole PR. It preserves compact finding metadata
  and selected
  review comments, then asks the separate `replan_reexecute` high-effort
  profile to independently rebuild from the latest verified default branch.
  Closing the superseded PR is the controller's only destructive write on
  agent-produced work, so the whole phase runs as **one durable transaction**
  (`replan_txn.py`) with an explicit monotonic lifecycle: `PENDING`,
  `PREPARED`, `VERIFIED`, `SUPERSEDE_INTENT`, `COMPENSATING`, `SUPERSEDED`, or
  the terminal `REJECTED`. Before the agent is invoked, `PREPARED` checkpoints the source PR
  and its exact HEAD, the verified default branch, the complete review
  evidence, the identities of the PRs that already existed, and a random
  controller-generated transaction id. That id is the **only** accepted proof
  of causality: the replacement must publish it in an
  `<!-- autoforge-replan-transaction: {...} -->` marker in its PR body, which
  the controller reads back from GitHub. Shape is never proof: an unmarked PR
  is ignored, a PR that already existed is refused even if it carries a copied
  marker, and two claimants block. Candidates are searched repository-wide, so
  a marked PR the agent has not yet linked to the issue is refused for the
  missing linkage rather than missed and re-implemented. Before concluding no candidate
  exists, an exhaustive all-states listing (open, closed and merged) is consulted, so a
  replacement that was closed before recovery is rejected with the PR named instead of
  triggering a second implementation. The PR-number watermark is a proven numeric maximum
  over all states, never inferred from one creation-time-ordered node. The old PR is closed
  only while both sides still match their checkpoints, including the marker,
  re-read on the last read before the close. GitHub has no conditional close,
  so that comparison cannot be fused to the write: it is *completed after* it,
  and a checkpoint that moved inside the close window is compensated by
  reopening the source PR and blocking, never by accepting the close. That
  compensation is itself a decision, so `COMPENSATING` is persisted *before*
  the reopen: a crash after a successful reopen resumes into "finish undoing",
  never into a second close. `SUPERSEDE_INTENT` is persisted *before* the write
  so a crash resumes into disposition rather than a second attempt, and a separate
  `gh pr comment` posts an `<!-- autoforge-replan-close: … -->` receipt only after the
  controller observed its own close landing, never inside `gh pr close --comment`, whose
  comment predates the close. The receipt is what tells the controller's own close from a
  human's afterwards. A source found closed without this transaction's receipt blocks
  and is never reopened, a conclusive close failure is never adopted, and an open source
  found under a recorded intent is never closed from a resume: with the receipt it is a
  prior close a human reopened, and without it the journal cannot tell "the close never
  ran" from "it landed, lost its receipt to a crash, and was reopened", so both block.
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
| `REVIEW` | OpenCode (round 1 `openai/gpt-5.6-luna` high, rounds 2 to 5 `openai/gpt-5.6-terra` high, 6+ `openai/gpt-5.6-sol` medium, intentionally retained through the 20-round cap) | round number, reviewed SHA == bound HEAD, exactly one review comment on this PR with the `# AI Code Review — Round N` heading and the `ai-review-result` marker matching round/SHA/flag, findings invariant; then controller policy: an eligible replan (including workflow stagnation or the cap) enters `REPLAN_REEXECUTE`; an exhausted replan limit or no eligible replan blocks. Entering `REVIEW` past the cap (stale re-review, HEAD drift, resume) is refused before the reviewer runs |
| `FIX` | Claude Code (`fable`, effort high) | `previous_head_sha` == current HEAD, every open finding ID resolved (`fixed` / `follow_up_created` / `no_change_with_rationale`), follow-up issues exist in this repo and are OPEN, actual PR HEAD == `new_head_sha`, a `fixed` resolution moved HEAD |
| `REPLAN_REEXECUTE` | OpenCode (`replan_reexecute`, default `openai/gpt-5.6-terra`, effort high) | One durable transaction. `PREPARED` (written before the agent runs) checkpoints the source PR at its exact HEAD, the verified default branch, the complete historical findings, the identities of the already-open PRs, and a random transaction id; a round whose findings could not be persisted in full refuses the replan here and keeps the old PR. The replacement is found **only** by the transaction marker in its PR body, never by shape, never from the CONTROL_RESULT, and never among the pre-existing PRs; several claimants, a copied marker or an unusable one all block. It must also be a distinct OPEN PR of this repository, linked to the issue, on a distinct branch based on the verified default branch, and its marker must attest this transaction id, this execution attempt, passing tests and at least the preserved historical finding count. `VERIFIED` records its HEAD; immediately before the destructive write both sides are re-read and must still match the checkpoint exactly. `SUPERSEDE_INTENT` is persisted *before* `gh pr close` so a crash resumes into disposition, and a separate `gh pr comment` posts an `<!-- autoforge-replan-close: … -->` receipt only after the controller observed its own close landing (never inside `gh pr close --comment`); the receipt is what proves afterwards that the close was the controller's and not a human's; the close outcome is re-read from GitHub rather than inferred from the exit status, a conclusive close failure is never adopted, and an open source under a recorded intent is never closed from a resume, with or without the receipt. A checkpoint that moved inside the close window is undone under a durable `COMPENSATING` record written before the reopen, and the replacement is re-verified once more before it is installed into controller state. Only then does the controller close the old PR without merge and reset the replacement lifecycle so its next review is round 1. Every refusal is persisted as `REJECTED` and replayed by `resume`; a transient GitHub failure is left resumable instead. |
| `READY_FOR_MERGE` | nobody | holding state; `step`/`resume` refuse to continue unless the merge gate is open (`resume` only re-prints the banner). With the gate open (`step --allow-merge` / `resume --allow-merge`) it runs the full pre-merge verification below against GitHub *before* entering `MERGE`: closed / conflicting / failing / draft / queued PRs, a required check whose job/step structure differs from the base branch's own run, and a failing `merge.verification_commands` command on the exported reviewed HEAD go to `BLOCKED` without ever reaching `MERGE`, HEAD drift -> `REVIEW`, an already-merged PR -> `MERGE` to reconcile; inconclusive data (checks running, the base branch's own run still running, mergeability unknown, GitHub unreachable / transient read failure, reviewed commit not fetchable) keeps the phase for `resume --allow-merge`, at most `merge.max_verification_attempts` times, then `BLOCKED`; a read that fails conclusively (bad credentials, permissions, unresolvable PR) -> `BLOCKED` at once |
| `MERGE` (gated) | controller, never an agent | last review clean and PR HEAD == reviewed HEAD; GitHub says PR is OPEN, not draft, every check succeeded, `mergeable=MERGEABLE`, `mergeStateStatus` `CLEAN`/`HAS_HOOKS`, no auto-merge armed, base branch has no merge queue, every `safety.required_checks` run has the same jobs and steps as the base branch's own run of that workflow (`safety.verify_check_definition`); then every `merge.verification_commands` command passes in a temporary export of the reviewed HEAD (persisted per HEAD + command list, so a resume does not repeat it); then `gh pr merge --<method> --match-head-commit <reviewed HEAD>`; counted only once GitHub reports `MERGED` at that HEAD. Conclusive negatives (a failing local command included) and conclusive read failures (bad credentials, permissions) -> `BLOCKED`; inconclusive data (checks running, base run still running, mergeability unknown, transient read failure, unfetchable reviewed commit, post-merge re-read failed) stays in `MERGE` for `resume --allow-merge`, at most `merge.max_verification_attempts` times, then `BLOCKED`; HEAD drift -> `REVIEW` |
| `UPDATE_EPIC` | OpenCode (`update_epic` profile) | `next_issue_url` gets the `INITIALIZING` checks before the controller switches issues: parses as an issue URL of this repo (a foreign URL is never even queried; a string that is not an issue URL at all is refused as a malformed result through the ordinary correction retry, before it can count as a selection), is neither the EPIC nor the just-finished issue (compared case-insensitively by repository + number, never by URL string), exists on GitHub and is OPEN. A rejected selection, or a transient GitHub failure while checking it, keeps the phase and `resume` asks the agent once more with the reason in its prompt; a second rejection -> `BLOCKED`. A conclusive GitHub failure (authentication, permissions, malformed data) -> `BLOCKED` immediately, without invoking the agent again. Only a verified issue reaches `ANALYZE_EXECUTE`; `null` -> `DONE` |

Recovery rules: if a step crashes after the agent created a PR, `resume`
re-enters `ANALYZE_EXECUTE`, finds the open PR (linked issue or
`autoforge/<n>` branch) and moves to `REVIEW` without running the agent. Two
or more candidate PRs → `BLOCKED` (the controller never guesses).
`REPLAN_REEXECUTE` has no separate recovery path at all: a fresh step and a
`resume` both call the same reducer over the persisted transaction, so the
normal and crash paths cannot drift apart about what is acceptable. Because
the transaction id is generated and persisted *before* the agent is invoked,
"crashed before invoking" and "crashed while the agent ran" are one state
(either a PR carrying that id exists, or none does), and a crash after the
replacement was created never causes a second implementation attempt.

Correction retry: when an agent exits 0 but its `CONTROL_RESULT` is missing or
invalid, the controller re-invokes it (`execution.max_correction_attempts`,
default once) with a correction prompt that tells it to inspect real
Git/GitHub state first and not repeat completed operations. In LOCAL mode a
correction is one more write-capable launch, so it is charged against and
checkpointed in the same durable per-phase bound as the launch before it (see
**Recovery** under LOCAL mode); the setting can never multiply that bound.
Non-zero exits, timeouts and verification failures are not retried
automatically; they leave the phase unchanged for `resume`.

## Prerequisites

- Python 3.11+ (managed via `uv`)
- `uv` ([install](https://docs.astral.sh/uv/getting-started/installation/))
- `git`, and `gh` (GitHub CLI **2.48.0 or newer**, authenticated): the
  client lists branch rules and a PR's changed files with
  `gh api --paginate --slurp`, which older releases reject; `autoforge doctor`
  refuses an older `gh` up front. `gh` is **not** needed for
  [local mode](#local-mode-no-github)
- `claude` (Claude Code CLI) for `analyze_execute` / `fix` profiles
- `opencode` (OpenCode CLI) for `review_*` profiles

Run `autoforge doctor` to check all of the above (read-only), or
`autoforge local doctor` for the local-mode subset (no `gh`, no GitHub
authentication, no `origin` remote).

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

URLs must be HTTPS GitHub issue URLs, and EPIC + issue must be in the same
repository as the current working directory (cross-repo runs are rejected).

## Local mode (no GitHub)

Sometimes there is no GitHub to orchestrate: a coding interview, a scratch
repository, an air-gapped machine, or simply a change you want driven by the
same review loop without opening an Issue. `autoforge local` is a separate,
explicit workflow for exactly that: the same controller, the same
"never trust the agent" discipline, none of the GitHub lifecycle.

```text
features/<slug>.md  →  INITIALIZING → ANALYZE_EXECUTE → REVIEW
                                         → clean ─────────────▶ DONE
                                         → findings ──────────▶ FIX → REVIEW
                                         → findings after the fix budget ─▶ BLOCKED
```

A local run never touches GitHub: no `gh` invocation, no Issue, no PR, no
comment, no push, no branch, no merge, no EPIC update, no follow-up issue, and
no replan. The `GitHubClient` is not even constructed. Nothing is faked
either: there are no placeholder PR URLs in local state or local prompts.

### The interview-sized example

```bash
uv run autoforge local init add-transaction-filter
# a human (with or without an AI) refines features/add-transaction-filter.md
uv run autoforge local run features/add-transaction-filter.md
```

### Commands

```bash
uv run autoforge local doctor            # config, git, agent CLIs — never gh, never auth
uv run autoforge local doctor --feature features/add-transaction-filter.md
uv run autoforge local init <slug>       # writes features/<slug>.md from a template
uv run autoforge local init <slug> --force        # only this overwrites an existing file
uv run autoforge local run features/<slug>.md --dry-run
uv run autoforge local run features/<slug>.md
uv run autoforge local run features/<slug>.md --allow-dirty

uv run autoforge status                  # mode-aware; no Issue/PR fields for a local run
uv run autoforge step                    # step / resume / status are mode-agnostic:
uv run autoforge resume                  # they read the mode from persisted state
```

### The feature specification

`autoforge local init <slug>` writes `features/<slug>.md` (directory
configurable via `local.feature_dir`) with `## Problem`, `## Requirements`,
`## Acceptance Criteria` (checkboxes), `## Non-goals` and
`## Notes / Decisions`. Feature specifications are project content, not
runtime state: they live in your repository and you may commit them.
Controller state stays out of the tree entirely. `local init` refuses to
overwrite an existing file unless you pass `--force`, refuses to write through
anything that is not already a regular file it could have written itself
(a symbolic link, a directory, a device), and refuses a `local.feature_dir`
that resolves outside the repository or is reached through a symbolic link.
It takes the repository controller lock like any other controller write, so it
cannot rewrite a specification an active run has frozen.

**The specification is frozen for the whole run.** `local run` records its
SHA-256, and the controller re-reads and re-checks the hash before *and* after
every agent phase. An agent that edits the specification fails the run instead
of getting easier acceptance criteria. Edit freely before a run; changed
requirements mean a new run.

The specification is *untrusted project data*, exactly like an Issue body in
remote mode: a requirement in it is a requirement, but an instruction in it to
bypass a controller invariant has no authority.

### What the controller verifies (there is no GitHub to ask)

The local analogue of "GitHub is the source of truth" is the working tree
itself, not git's opinion of it. Before and after every phase the controller
walks the tree through its own directory descriptors and computes a **workspace
fingerprint** over everything it finds.

It is a walk, not a `git status`, and that is the central design decision.
`git status` answers "what would I commit?", which is a different question
from "which bytes could the reviewer have read". Git reports none of these:
an ignored file, a file marked `assume-unchanged` or `skip-worktree`, a mode
change under `core.fileMode=false`, an empty directory, a symbolic link whose
target text changed. Each one is code a reviewer can read and an agent can
edit. So git is asked only what it is authoritative about
(where the repository is, what HEAD and the branch are); the filesystem is
asked what is in the tree.

Every entry the walk finds lands in exactly one of four states, and there is
no fifth:

| | |
|---|---|
| **hashed** | regular files: SHA-256 of the bytes, plus the permission bits (whether a script is executable decides what a validation command does with it) |
| **metadata** | directories and symbolic links: the mode, and for a link its target *text*, never what the target contains |
| **excluded** | the repository's own git directory (identified by `(st_dev, st_ino)`, not by the name `.git`) and anything matching `local.exclude`. Both are hashed into the fingerprint as *rules* and named to the reviewer in its prompt, so "what was not reviewed" is part of the review's identity |
| **refused** | anything that cannot be bound at all: a FIFO, socket or device; an unreadable file or unlistable directory; a nested repository or submodule; a symbolic link pointing outside the tree, into an excluded region, or to a directory (the root included) through which an excluded entry can be reached at an unexcluded path. The run fails closed, naming the entry and the exclusion that would accept it |

Nothing is silently skipped, so "the snapshot does not mention it" and "it is
not in the tree" are the same statement.

Two things are deliberately *not* in the fingerprint. The first is HEAD and
the branch: they are bound separately, so an ordinary `git commit`, which
changes no byte of the working tree, is reported as what it is (the git
anchor moved, and the run blocks) rather than as "the reviewer modified the
workspace". The second is the run's own state, which lives outside the
reviewed tree entirely, under `<git dir>/autoforge/state`: a controller that
writes into the tree it fingerprints would either invalidate its own review on
every step or have to carve a region out by name, a region an agent could
then write into without moving the fingerprint. An explicit `--state-dir`
inside the working tree is refused for the same reason.

The walk is bounded by `local.max_workspace_entries` and
`local.max_workspace_bytes`. These are refusal thresholds, not sampling ones:
a tree too large is refused with the largest subtrees named, because a
fingerprint that fell back to metadata for the remainder would accept an
equal-sized replacement with a restored mtime.

Runtime writes go through one capability boundary (`safefs.py`): the state
directory is opened once as a descriptor, and every name below it is resolved
with `dir_fd=` and `O_NOFOLLOW`, so no path component can be redirected
between the check and the use. Whole-file artifacts are written as a fresh
`O_CREAT|O_EXCL` temporary in the target's own directory and renamed over the
name, which means a symbolic link, hard link, FIFO or device planted at an
artifact's name is *replaced*: it is never opened, so whatever it pointed at
is provably untouched. The append-only `events.jsonl` is the one artifact that
must be opened in place, and there the open is `O_NOFOLLOW|O_NONBLOCK` and
refuses a hard link outright. Every read of it is bounded, at recovery and at
the append after an agent returns alike, and the run's log directory is
listed under a budget of its own, so a same-user agent that plants an
oversized journal or a million names there makes the controller refuse, not
run out of memory. `run_id`, which names `logs/<run_id>`, is
validated as a single safe path component whenever state is loaded, not
trusted because the controller generated it once.

With that, the controller checks for itself:

| Phase | Verified independently of what the agent claimed |
|---|---|
| `ANALYZE_EXECUTE` | feature spec hash unchanged; the workspace really changed (and matches the agent's `changed_workspace` claim); every configured validation command exits 0 |
| `REVIEW` | feature spec hash unchanged; the tree still matches the fingerprint the controller bound after the last verified write phase (`REVIEW` never binds a new one; a tree that drifted, whatever moved it, is `BLOCKED` before the reviewer is launched); the reviewed fingerprint is exactly that value; the reviewer did not modify the workspace; `needs_fix_round == (findings > 0)`; finding IDs unique and in-round |
| `FIX` | feature spec hash unchanged; every open finding has a resolution; a `fixed` resolution actually changed the workspace; validation commands still pass |

**No commit is ever required and HEAD never has to move.** The implementation
and its fixes live in the working tree; AutoForge never commits, stages,
pushes, stashes, resets or switches branches on your behalf.

### Dirty working trees

v1 policy, chosen for correctness over convenience: the working tree must be
clean apart from the feature specification itself (so a brand-new,
uncommitted `features/<slug>.md` is fine). Otherwise `local run` refuses and
names the paths, so you can commit or stash them. `--allow-dirty` starts
anyway and records those paths in the run: they are reported in `status` and
to the reviewer, never silently absorbed into the implementation baseline.
Separating pre-existing edits from agent edits in the same file is a heuristic,
and a heuristic is not a trust boundary.

What `--allow-dirty` does *not* do is loosen the binding: the snapshot taken
at the start of the run hashes those dirty files like every other entry, so
their contents at the moment the run began are pinned exactly as a clean
file's are. The recorded paths are disclosure, not an exemption.

### Review/fix bound

Local mode does not use the 20-round remote machinery. The default is one
fix round: `REVIEW → FIX → REVIEW`, then `DONE` if clean and `BLOCKED` if
findings remain. Configure it with `local.max_fix_rounds` (0 means a single
review pass, and any finding blocks). A blocked local run leaves every change
in your working tree, untouched, for you to inspect.

Review rounds are routed to profiles exactly as in remote mode, so a bound of
five or more fix rounds reaches `review_round_6_plus`; that profile is then
required by `local doctor` and at the start of the run.

### Validation commands

Optional, controller-owned and controller-run: argv arrays, never shell
strings, never auto-detected from your stack:

```yaml
local:
  feature_dir: features
  max_fix_rounds: 1
  validation_commands:
    - ["./gradlew", "test"]
    - ["npm", "--prefix", "frontend", "run", "build"]
```

They run after `ANALYZE_EXECUTE` and after `FIX`, are logged like any other
invocation, and a non-zero exit (or a timeout) means the phase is *not*
verified: the run stops with the phase unchanged for `resume`. `--dry-run`
lists them and executes none of them. The fingerprint a review is bound to
is taken *after* they ran, so a build cache or a generated file they leave
in the tree is part of the reviewed tree rather than a drift the next phase
would refuse.

### Dry run and resume

`local run --dry-run` is side-effect-free in the usual AutoForge sense: it
prints the mode, the feature path, its frozen hash, the current fingerprint,
the phase, the selected profile, the prompt template, the validation commands
that *would* run and the legal next transitions. It invokes no agent, runs
no validation command, and writes no state file.

A local run is durable and resumable exactly like a remote one: state lives in
`<git dir>/autoforge/state/state.json`, is written atomically, and holds the
mode, feature path, frozen hash, base HEAD, bound and reviewed fingerprints,
review/fix rounds and open findings. `Ctrl-C` then `autoforge resume` continues
from the persisted phase, re-reading the real working tree.

It also holds the run's contract: the repository root, the state
directory, the workspace policy (`local.exclude`, both cost bounds and the
snapshot algorithm), `local.validation_commands`, `local.max_fix_rounds`,
`workflow.max_total_steps` and the prompt version, as they were when the run
started. A resumed run may
revalidate its contract but never redefines it: every later invocation
(`resume`, `step`, `status`, a dry run, a crash recovery) compares what it
would define against the record before it binds the run, and refuses with
each moved field named (`local.exclude: run: [] current: ["src"]`) rather
than reviewing a tree under rules nobody reviewed it under. Restore the
setting to resume, or start a new run under the new one. A LOCAL state file
without a readable contract is refused, never filled in from today's
configuration.

Recovery never trusts what the dead process believed. Every fact the next
transition depends on (the fingerprint, the git anchor, the specification
hash) is re-derived after the restart, and an illegal combination of fields
fails at the moment the state file is read rather than somewhere downstream.
The one thing that *is* carried across is a checkpoint written **before** a
write-capable agent is launched, recording the phase and the fingerprint it
started from. That is what makes "crashed before implementing" and "crashed
after implementing" distinguishable without asking the agent: the resumed
attempt is judged against the tree from before the *first* attempt, so work
already in the tree counts, and re-entry is bounded rather than endless:
three launches per phase entry, counting every launch (the entry's own, a
correction retry after a malformed `CONTROL_RESULT`, a resumed attempt),
each written to the state file immediately before the agent starts, then
`BLOCKED`. Only a launch is counted: a step that is refused before the agent
starts (a corrupted event journal, an unusable execution profile, a prompt
template that cannot be rendered) charges nothing, so repairing the cause and
resuming does not spend the bound. The checkpoint belongs to the phase that
wrote it: a state file holding one
under a different live phase is refused at load rather than closed by that
phase without the recorded work ever being examined; only a run that ended
in `BLOCKED` or `FAILED` keeps it, as evidence.

## State directory

Remote mode defaults to `.autoforge/` in the working directory (overridable
via `--state-dir` or config). A local run defaults to
`<git dir>/autoforge/state` instead, outside the tree it fingerprints, since
its own writes would otherwise keep invalidating its own review. Same layout
either way:

```text
<state dir>/
    state.json          # persisted run state (atomic writes)
    state.json.corrupt-<timestamp>   # unreadable state moved aside by 'run --force'
    logs/<run-id>/
        events.jsonl                       # one line per agent invocation
        <seq>-<phase>-<attempt>/
            request.json                   # profile, model, effort, command, timeout
            prompt.md                      # rendered prompt (redacted)
            execution.json                 # exit code, timing, timed_out, truncation, error
            stdout.log / stderr.log        # redacted; head + marker + tail past the capture bound
            control-result.json            # parsed CONTROL_RESULT (when valid)
```

The controller lock is deliberately not in the state directory. It lives
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
`replan_transaction` record described above: the controller's intent journal,
which `resume` replays rather than re-deriving a decision from GitHub facts. State keeps compact finding summaries and review
comment URLs, rather than copying unbounded PR discussion bodies. Those
summaries are bounded (100 findings per round, above the 50 findings the parser
accepts; 6000 characters per required resolution, which is the parser's
2000-character bound times the most redaction can lengthen a text); a round
that hits either bound is marked
`evidence_truncated`, and because superseding a PR deletes the controller's
only record of its findings, such a round makes the replan policy block for a
human instead. Because the parser refuses an oversized review before it is
recorded, only state written by an older controller or edited by hand can
carry that mark.
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
  with `LockError` (exit 2) before anything is written: a tampered path or
  entry can neither redirect the PID write to another file nor produce a
  traceback. These checks cover entries that are damaged or tampered *at
  rest*; a writer with write access to the git directory who races the
  check-then-write window, or swaps the `autoforge` directory for another
  directory between two controller invocations, has the controller's own
  privileges and is outside the trust boundary (the git directory is assumed
  to be writable only by the operating user).
- **Automatic merge is off by default and opt-in only.** The `MERGE` phase is
  reachable only from `READY_FOR_MERGE` and only when both
  `safety.allow_merge: true` is set in config and `--allow-merge` is
  passed on the CLI. With the gate closed `run`/`resume` stop at
  `READY_FOR_MERGE` and a human merges. `safety.allow_merge` is the *only*
  config key that opens the gate: the historical `execution.allow_merge` is
  rejected on load rather than read, and an unknown key under `safety` (a
  typo such as `allow_merges`) is a configuration error, so the gate can
  never be "disabled" in one place while still open in another. The same
  rule holds for every other section and for the top level (see
  "Configuration"): a misspelled loop bound keeps no looser default behind
  the operator's back.
  `autoforge doctor` prints the effective gate state and the file that set it.
- **`doctor` verifies that the default branch still requires the CI check.**
  The pre-merge gate below trusts "every check on the PR succeeded", which
  says nothing about whether any check *had* to exist: with the branch
  ruleset gone, a PR with no check runs at all is vacuously green. That
  ruleset lives in repository settings, outside version control, so
  `autoforge doctor` reads the effective rules of the default branch
  (`gh api repos/{owner}/{repo}/rules/branches/{branch}`, every page) and
  reports which contexts a `required_status_checks` rule names, whether each
  of `safety.required_checks` (default `ci`) is among them, whether the
  ruleset's enforcement is `active`, and whether it has bypass actors. Any
  of those missing is a `FAIL` with the settings URL and the rule to add. A
  repository still on classic branch protection is checked through that
  instead (`enforce_admins` standing in for "no bypass actors"). A read the
  token is not allowed to make (no credentials, a plan that hides rulesets,
  a non-admin token and no ruleset) or a transient GitHub failure is `SKIP`,
  never a false alarm; so is a *partial* read: GitHub returns a ruleset's
  `bypass_actors` only to a token with write access to it, and a rule this
  token can see but whose bypass list it cannot is `SKIP` rather than `OK`
  (a token that GitHub says may itself bypass the rule is a `FAIL` either
  way). `doctor --json` carries a skip as `"skipped": true`. The
  check is read-only and runs in `doctor` only: the `READY_FOR_MERGE` gate
  itself does not yet consult branch rules.
- **Agents never merge.** When the gate is open, the *controller* performs the
  merge itself: `gh pr merge --<merge.method> --match-head-commit <reviewed HEAD>`
  through `GitHubClient`, with no prompt and no agent invocation. Every agent
  prompt carries the unconditional rule "never merge a pull request".
- **Pre-merge verification is controller-side and fails closed.** It runs in
  `READY_FOR_MERGE` (before `MERGE` is entered) and again in `MERGE` (before
  the write), reading only controller state and GitHub, never an agent
  claim. GitHub must report: PR open at the reviewed HEAD, not a draft, no
  change to any `safety.protected_merge_paths` entry (see below), every
  check in the status rollup succeeded (all checks, not only required ones),
  `mergeable = MERGEABLE`, `mergeStateStatus` in `CLEAN`/`HAS_HOOKS`, no
  auto-merge armed, and no merge queue on the base branch (`gh pr merge`
  would otherwise arm auto-merge or enqueue instead of merging, leaving an
  asynchronous merge the controller does not own). Conclusive negatives
  (closed PR, conflict, failing check, branch protection, queue) -> `BLOCKED`;
  inconclusive data (checks still running, `mergeable = UNKNOWN`, or the PR /
  changed-file / merge-queue read itself failing transiently: timeout, connection error,
  5xx, rate limit) raises and keeps the phase so `resume --allow-merge` (or
  `step --allow-merge`) re-checks, one attempt per invocation, at most
  `merge.max_verification_attempts` times (default 5), then `BLOCKED`. A read
  that fails conclusively (bad credentials, missing permissions, a PR that no
  longer resolves) is `BLOCKED` immediately: re-running would not change it.
  Either way nothing is merged. HEAD drift -> `REVIEW`.
- **A PR is never merged unattended if it redefines its own checks.** The
  hosted `ci` result the gate above trusts is produced by the workflow files
  *in the PR*: GitHub runs the PR's copy of `.github/workflows/` and reports
  it under the same check name, and a branch ruleset cannot help, because the
  required check is defined by the branch it gates. So the controller reads
  the PR's changed files and `BLOCK`s when any of them matches
  `safety.protected_merge_paths` (default `.github/workflows/`), naming the
  paths; a human reviews and merges that PR themselves. Both ends of a
  rename count, so moving a protected file *out* of the protected range is
  refused like an edit to it. The listing is read in full
  (`gh api --paginate --slurp`, every page), so a PR is judged on all of
  its files rather than on the first hundred; GitHub itself stops the
  endpoint at 3,000 files, and a listing that stays short of GitHub's own
  `changedFiles` count is refused: a short listing cannot prove a protected
  path was left alone, and a PR that large is not one to merge unattended
  anyway. Setting the list to `[]` disables the gate; leaving the
  key empty (`null`) is a configuration error rather than a silent opt-out.
  What this gates is the *definition* of the checks, not the
  trustworthiness of a green run: the commands still execute the PR's own
  code, so a PR can weaken what its tests assert without touching a
  protected path. The two gates below narrow that gap; what remains is why
  merge stays behind `safety.allow_merge` + `--allow-merge`.
- **A required check is trusted only if it ran the base branch's definition.**
  With `safety.verify_check_definition` (default `true`) the controller
  resolves every `safety.required_checks` context in the PR's status rollup
  to its GitHub Actions run through the check's details URL and requires the
  run to be in this repository, at the reviewed HEAD and completed; a
  context that appears zero or several times, or that is not an Actions
  run, is refused. It then reads the run's jobs and their steps (every page,
  a short listing is refused) and compares that structure with the base
  branch's own `push` run of the same workflow at the branch's current tip:
  same job names, same step names in the same order, job order ignored.
  The first difference (a job or step missing, added or renamed) is
  `BLOCKED` with the difference named. The reference must be a completed,
  successful run; a base branch without one at its tip is `BLOCKED` (make
  it green first), and a reference still running is inconclusive. This
  catches a PR that rewrites what the check *is* through an untouched
  workflow file (a reusable action, a `Makefile` target the workflow calls
  by a different job or step name) but it is structural: a step whose name
  stayed the same while its command changed passes. Setting the key to
  `false`, or leaving `required_checks` empty, skips the comparison.
- **The controller can run its own verification on the reviewed commit.**
  `merge.verification_commands` (argv lists, empty by default) run on the
  operator's machine after every GitHub-side fact above has passed, in a
  fresh temporary export of the reviewed HEAD: `git read-tree` +
  `git checkout-index` into `autoforge-premerge-*`, so no worktree or branch
  is created, the operator's checkout is untouched and there is no `.git`
  for a command to reach; the export is deleted afterwards. That also means
  a command that needs git metadata (`git describe`, `setuptools-scm` and
  other version stamping) or populated submodules fails in the export, so
  keep those out of the list or make them tolerate a plain tree. A commit
  that is not local yet is fetched from `origin` as `refs/pull/<n>/head`,
  objects only, without creating a local ref. A non-zero exit or a timeout
  (`execution.default_timeout_seconds`) is `BLOCKED` with the redacted
  output tail; an unfetchable or unexportable commit is inconclusive and
  re-checked by `resume --allow-merge`. A pass is persisted with the HEAD
  and the command list, so a resume at the same HEAD does not rerun it and a
  changed command list or a new HEAD does; it is cleared when the run moves
  to the next issue. Each run is logged like an agent invocation. Dry-run
  runs nothing and lists the commands in the plan; `autoforge doctor`
  reports what is configured without running it. Submodules are not
  populated in the export and the commands inherit the operator's
  environment (environment allow-listing remains a separate task).
- **Post-merge is reconciled from GitHub.** The merge is counted only after
  GitHub reports `MERGED` at the reviewed HEAD (idempotently, across crashes).
  If `gh pr merge` returns but the PR is still open, any auto-merge that call
  armed is disabled again (`gh pr merge --disable-auto`) and the run is
  `BLOCKED`, unless the PR is open at a *different* HEAD (pushed between the
  verification and the write, so `--match-head-commit` refused it) and no
  asynchronous merge is pending: then nothing unreviewed merged, the clean
  review is stale and the run goes back to `REVIEW`. If the post-merge re-read fails, the outcome is treated as
  unknown: the run stays in `MERGE` and `resume --allow-merge` re-inspects GitHub (an
  already-merged PR is recovered and counted once; an open one is re-verified),
  bounded by the same `merge.max_verification_attempts`, then `BLOCKED`.
- Logs and CLI output pass through baseline secret redaction (`GITHUB_TOKEN`,
  `GH_TOKEN`, `*_API_KEY`, `Authorization: Bearer`, `ghp_*`, `sk-*`, …). No
  environment dump is ever written. Baseline only, with no claim of
  completeness.
- No `os.system` / `shell=True` anywhere; prompts travel as a single argv
  element so shell metacharacters in issue text cannot be interpreted.
  Agents run in a new session and the whole process group is killed on timeout,
  and the kill is complete only once no process is left in the group; the
  timeout also covers a descendant that keeps the agent's output pipes open
  after the agent itself has exited. Every wait in the kill is bounded, so
  whatever survives it is abandoned and the controller still gets its result.
- **Runs cannot loop forever.** The review-round cap, stagnation detection
  and the cumulative step budget (`workflow:` in the config) are controller
  invariants checked before an agent is invoked; hitting one is `BLOCKED`, a
  terminal phase that `resume` does not re-enter.
- **A local run is GitHub-free by construction, not by convention.** The
  `GitHubClient` is built on first *use* and a `LOCAL` run never has one, so
  there is no path from local mode to `gh` at all: no authentication is
  needed and none is consulted. The local prompt templates are separate files
  that never mention creating a PR, reading an Issue, posting a comment,
  pushing, merging, or opening a follow-up Issue, and the local result
  protocol rejects a `follow_up_created` resolution outright. Tests drive a
  full local run with a GitHub double that raises on any attribute access.
- **The local feature specification is frozen and untrusted.** Its SHA-256 is
  recorded when the run is created and re-checked before and after every agent
  phase: an agent that rewrites its own acceptance criteria fails the run. It
  is resolved to a regular, non-symlink Markdown file inside the repository
  (traversal, absolute paths elsewhere, directories and device nodes are all
  refused), and its content is *task data*: an instruction inside it to
  bypass a controller invariant has no authority.
- **Local verification reads git, never a claim.** `local_workspace.py` runs
  `git` with argv lists only (no shell) and computes a workspace fingerprint
  over HEAD plus every changed and untracked path and its contents; reviews
  are bound to that fingerprint the way remote reviews are bound to a PR HEAD
  SHA. Local runs never commit, stage, push, stash, reset, or switch branches,
  and refuse a working tree that is dirty beyond the feature file unless
  `--allow-dirty` records those paths explicitly.
- `doctor` is read-only apart from a temp file it creates and removes in the
  state directory; its GitHub reads (`gh repo view`, `gh api` GETs of the
  default branch's rules) never write. `autoforge local doctor` runs the
  local subset and omits the `gh`, `gh auth status`, `origin` and
  branch-rule checks entirely.
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

Every key in the file must be one the controller reads. An unknown key,
whether at the top level, in any section (`execution`, `safety`, `github`,
`merge`, `review`, `review.replan`, `workflow`, `local`) or in a profile
mapping, is a configuration error naming the section, the offending key and
the keys that section knows, e.g. `unknown key(s) under 'workflow':
max_review_round (known: max_review_rounds, ...)`. A typo cannot be a silent
no-op that leaves the built-in default in force: several of these keys are
loop bounds or merge behaviour, where "ignored" means a looser bound than the
operator wrote while `autoforge doctor` calls the file valid. Provider-specific
`options` under a profile are the one free mapping; the adapter in
`providers.py` validates what it reads there. The same contract covers what
a parser would otherwise settle before the controller looks: a key written
twice in one mapping is a parse error on every format (PyYAML and
`json.loads` would keep the last copy and drop the first, typo included), a
YAML document whose root is not a mapping is refused on both YAML backends
(only an empty or comment-only file means "all defaults"), and a profile
name must be a non-empty string (PyYAML types unquoted `1:` as an integer).

The `local:` block configures [local mode](#local-mode-no-github):
`feature_dir`, `max_fix_rounds` and the argv-array `validation_commands`. A
local run needs only the profiles its configured bound can reach: with the
default `max_fix_rounds: 1` that is `analyze_execute`, `fix`, `review_round_1`
and `review_round_2_5`. Local review rounds are routed exactly like remote
ones, so `max_fix_rounds: 5` or more also requires `review_round_6_plus`.
`local doctor` and the start of a `local run` check that, rather than leaving
it to fail five fix rounds in. `replan_reexecute` and `update_epic` belong to
the remote lifecycle only.

## Development

```bash
make sync       # uv sync (.venv + dev tools)
make test       # uv run pytest
make lint       # uv run ruff check src tests
make fmt        # uv run ruff format src tests
make fmt-check  # uv run ruff format --check src tests
make typecheck  # uv run mypy src
make check      # the four CI commands, in the local environment
```

The same four checks run hosted on every pull request and every push to
`main` (`.github/workflows/ci.yml`), each after a locked install
(`uv sync --locked`): `pytest` on Python 3.11 and 3.12, and `ruff check` /
`ruff format --check` / `mypy` once. The workflow needs no
secrets and is granted none. Its aggregate `ci` job is a single stable check
name that survives adding or renaming a matrix entry, and it is required on
`main` by a repository ruleset. That is what gives the controller's
pre-merge gate ("every check on the PR succeeded") something real to verify
instead of a vacuously green PR. The same ruleset requires a pull request
(with zero required approvals, since GitHub forbids self-approval and any
higher count would deadlock the controller's own merge) and blocks force-push
and deletion of `main`. Because that ruleset is repository configuration
rather than code, `autoforge doctor` re-reads it (see "Security model")
so that disabling it, renaming the context or adding a bypass
actor is noticed before an unattended run relies on it.

**What a green `ci` does and does not prove.** It proves the suite passed on
GitHub's runners for that commit, which is strictly more than an agent's
claim that it ran the tests. It is not a signal independent of the PR: the
workflow that defines the check, and the code the check runs, both come from
the PR. Four things bound that. The controller refuses to merge a PR that
touches `safety.protected_merge_paths` (default `.github/workflows/`), so a
PR cannot redefine the check that clears it; that refusal lives in the
controller, in version control and under test, rather than in a repository
setting that can drift unnoticed. With `safety.verify_check_definition` it
also compares the jobs and steps the `ci` run actually executed with the
base branch's own run of the same workflow, so a check that was redefined
through something the protected paths do not cover is refused too. With
`merge.verification_commands` set, the controller runs the repository's own
checks (`pytest`, `ruff`, `mypy`, …) on an export of the reviewed commit
before it merges: evidence it produced itself, not a check name it read.
And a PR that weakens what its tests assert still has to pass the review
phase, whose findings are what the loop bounds act on. None of this replaces
a human reading the diff (the local commands still run the PR's tests, just
under the controller's eye rather than the PR's workflow), which is why
`safety.allow_merge` is off by default.

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
controller-owned `MERGE` step, including its pre-merge mergeability / check
verification, and `UPDATE_EPIC` are exercised only against the in-memory
fake, never against real services), controller-owned EPIC batching (#13),
unattended production operation, CI checks as a review input, dequeuing a PR
from a merge queue (the controller refuses to merge into queue-protected
branches instead).
