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
      → needs_fix_round? ── YES ──▶ FIX (Claude Code) → REVIEW …
                          └─ NO ───▶ READY_FOR_MERGE
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

## Architecture

```text
src/autoforge/
    cli.py            argparse CLI: doctor / run / step / resume / status
    engine.py         ControllerEngine — step() primitive, run() loops step(),
                      per-phase verification via gh, recovery, correction retry
    transitions.py    Phase enum + legal edges + decide_next_phase (pure)
    state.py          AutoForgeState + atomic save/load (.autoforge/state.json)
    config.py         file-or-defaults config (yaml/toml/json), profiles, safety gate
    profiles.py       review-round routing: 1 / 2-5 / 6+ (pure function)
    providers.py      AgentProvider adapters: ClaudeCodeProvider, OpenCodeProvider,
                      ScriptedProvider (tests); CLI flags live only here
    executor.py       subprocess abstraction: argv, timeout, process-tree kill
    result_parser.py  <<<CONTROL_RESULT>>> extraction + typed per-phase results
    validation.py     typed GitHub URL refs (issue / PR / comment), remote parsing
    github.py         typed GitHubClient over `gh`: reads (PRs, issues, comments,
                      checks, merge queue) + the controller-owned merge / disarm writes
    doctor.py         read-only environment checks
    locking.py        flock(2) repository lock (.autoforge/controller.lock)
    runlog.py         per-run logs (.autoforge/logs/<run-id>/), redacted
    redaction.py      baseline secret masking for logs / CLI output
    errors.py         Configuration/State/Transition/Lock/Execution/
                      ControlResult/GitHub/Verification taxonomy
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
  (default 6) caps completed review rounds per PR; a round at the cap that
  still has findings goes to `BLOCKED` instead of starting a FIX that could
  never be reviewed. Stagnation detection blocks earlier: consecutive rounds
  whose `required_resolution` texts are identical
  (`workflow.stagnation_identical_rounds`, default 2) or whose finding count
  does not change (`workflow.stagnation_unchanged_count_rounds`, default 3).
  `workflow.max_total_steps` (default 300) is a cumulative budget for the whole
  run measured on the persisted `step_count`, so `resume` continues it rather
  than resetting it (`--max-steps` bounds one invocation only). Failed agent
  invocations consume neither a review round nor the history. Every bound
  ends in `BLOCKED` with the reason; findings and the PR stay for a human.

## Workflow details

| Phase | Who | What the controller verifies afterwards |
|---|---|---|
| `INITIALIZING` | controller | cwd repo == issue repo, issue is in this repo, is not the EPIC, exists and is OPEN |
| `ANALYZE_EXECUTE` | Claude Code (`fable`, effort high) | existing open PR for the issue is recovered without re-running the agent; otherwise PR exists in this repo, is OPEN, HEAD SHA and branch match the claim |
| `REVIEW` | OpenCode (round 1 `openai/gpt-5.6-luna` high, rounds 2–5 `openai/gpt-5.6-terra` high, 6+ `openai/gpt-5.6-sol` medium) | round number, reviewed SHA == bound HEAD, exactly one review comment on this PR with the `# AI Code Review — Round N` heading and the `ai-review-result` marker matching round/SHA/flag, findings invariant; then the loop bounds: round == `workflow.max_review_rounds` with findings, or stagnation across the recorded `review_history` -> `BLOCKED` (no further FIX). Entering `REVIEW` past the cap (stale re-review, HEAD drift, resume) is refused before the reviewer runs |
| `FIX` | Claude Code (`fable`, effort high) | `previous_head_sha` == current HEAD, every open finding ID resolved (`fixed` / `follow_up_created` / `no_change_with_rationale`), follow-up issues exist in this repo and are OPEN, actual PR HEAD == `new_head_sha`, a `fixed` resolution moved HEAD |
| `READY_FOR_MERGE` | nobody | holding state; `step`/`resume` refuse to continue unless the merge gate is open (`resume` only re-prints the banner). With the gate open (`step --allow-merge` / `resume --allow-merge`) it runs the full pre-merge verification below against GitHub *before* entering `MERGE`: closed / conflicting / failing / draft / queued PRs go to `BLOCKED` without ever reaching `MERGE`, HEAD drift -> `REVIEW`, an already-merged PR -> `MERGE` to reconcile; inconclusive data (checks running, mergeability unknown, GitHub unreachable / transient read failure) keeps the phase for `resume --allow-merge`, at most `merge.max_verification_attempts` times, then `BLOCKED`; a read that fails conclusively (bad credentials, permissions, unresolvable PR) -> `BLOCKED` at once |
| `MERGE` (gated) | controller, never an agent | last review clean and PR HEAD == reviewed HEAD; GitHub says PR is OPEN, not draft, every check succeeded, `mergeable=MERGEABLE`, `mergeStateStatus` `CLEAN`/`HAS_HOOKS`, no auto-merge armed, base branch has no merge queue; then `gh pr merge --<method> --match-head-commit <reviewed HEAD>`; counted only once GitHub reports `MERGED` at that HEAD. Conclusive negatives and conclusive read failures (bad credentials, permissions) -> `BLOCKED`; inconclusive data (checks running, mergeability unknown, transient read failure, post-merge re-read failed) stays in `MERGE` for `resume --allow-merge`, at most `merge.max_verification_attempts` times, then `BLOCKED`; HEAD drift -> `REVIEW` |
| `UPDATE_EPIC` | OpenCode (`update_epic` profile) | `next_issue_url` gets the `INITIALIZING` checks before the controller switches issues: parses as an issue URL of this repo (a foreign URL is never even queried), is neither the EPIC nor the just-finished issue (compared case-insensitively by repository + number, never by URL string), exists on GitHub and is OPEN. A rejected selection, or a transient GitHub failure while checking it, keeps the phase and `resume` asks the agent once more with the reason in its prompt; a second rejection -> `BLOCKED`. A conclusive GitHub failure (authentication, permissions, malformed data) -> `BLOCKED` immediately, without invoking the agent again. Only a verified issue reaches `ANALYZE_EXECUTE`; `null` -> `DONE` |

Recovery rules: if a step crashes after the agent created a PR, `resume`
re-enters `ANALYZE_EXECUTE`, finds the open PR (linked issue or
`autoforge/<n>` branch) and moves to `REVIEW` without running the agent. Two
or more candidate PRs → `BLOCKED` (the controller never guesses).

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
    controller.lock     # flock(2): one controller per repo
    logs/<run-id>/
        events.jsonl                       # one line per agent invocation
        <seq>-<phase>-<attempt>/
            request.json                   # profile, model, effort, command, timeout
            prompt.md                      # rendered prompt (redacted)
            execution.json                 # exit code, timing, timed_out, error
            stdout.log / stderr.log        # redacted
            control-result.json            # parsed CONTROL_RESULT (when valid)
```

State records `current_pr_url`, `current_branch`, `current_head_sha`,
`reviewed_head_sha`, `review_round`, `last_review_comment_url`,
`last_review_needs_fix`, `open_findings`, `last_fix_resolutions`,
`review_history` (one entry per completed review round of the current PR:
round, reviewed SHA, result, finding count, fingerprint of the requested
resolutions), `step_count` (cumulative for the run, never reset), `attempt`
and `block_reason`.

## Security model

- One controller per repository (flock); a second instance exits with `LockError`.
  `run`, `step` and `resume` take the lock **before** `state.json` is read or
  created and keep it until their last step has been persisted, so a state
  snapshot loaded before the lock is never executed and no second controller
  can take over the repository mid-command. Dry-run takes no lock. The lock
  entry itself must be a regular file: `controller.lock` is opened with
  `O_NOFOLLOW`, so a symlink, FIFO, socket, device or directory in its place is
  refused with `LockError` (exit 2) before anything is written — a tampered
  entry can neither redirect the PID write to another file nor produce a
  traceback.
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
`review_round_2_5`, `review_round_6_plus`, `update_epic`) are stable (there is
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
