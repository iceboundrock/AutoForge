# AutoForge / 智铸

**Autonomous orchestration for AI-driven software development.**

智铸——AI 驱动的软件工程自动化控制器。

> **Early development (Phase 2).** The first real AI development loop is
> wired end to end — GitHub issue → Claude Code implementation → PR
> verification → OpenCode review → Claude Code remediation → repeated review
> → `READY_FOR_MERGE`. **AutoForge does not merge pull requests in this
> milestone.** Nothing here is production ready yet.

## What AutoForge is (and is not)

AutoForge is **not another coding agent**. It is a **deterministic
orchestration layer** that drives existing agents through the software
lifecycle and refuses to trust anything they claim without checking it
against GitHub:

```text
Issue → ANALYZE_EXECUTE (Claude Code) → PR → REVIEW (OpenCode)
      → needs_fix_round? ── YES ──▶ FIX (Claude Code) → REVIEW …
                          └─ NO ───▶ READY_FOR_MERGE  (stops here; human merges)
```

AutoForge itself never writes business code. It:

- holds the workflow state machine (phases + legal transitions),
- selects the execution profile (which provider/model/effort for which round),
- renders strictly-validated prompts from file templates,
- invokes `claude` / `opencode` through provider adapters and `gh` through a
  typed read-only client (argv lists only, never a shell),
- parses the machine-readable `CONTROL_RESULT` protocol from agent stdout,
- **verifies every claim** (PR exists / is OPEN / HEAD SHA / branch, review
  comment exists on the right PR for the right round and SHA, follow-up issue
  exists, fix actually moved HEAD) before advancing state atomically,
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
    github.py         typed read-only GitHubClient over `gh` (PRs, issues, comments)
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
  stop phase (`READY_FOR_MERGE`, `DONE`, `BLOCKED`, `FAILED`).
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
  fails loudly and is never silently overwritten.

## Workflow details

| Phase | Who | What the controller verifies afterwards |
|---|---|---|
| `INITIALIZING` | controller | cwd repo == issue repo, issue exists and is OPEN |
| `ANALYZE_EXECUTE` | Claude Code (`fable`, effort high) | existing open PR for the issue is recovered without re-running the agent; otherwise PR exists in this repo, is OPEN, HEAD SHA and branch match the claim |
| `REVIEW` | OpenCode (round 1 `openai/gpt-5.6-luna` high, rounds 2–5 `openai/gpt-5.6-terra` high, 6+ `openai/gpt-5.6-sol` medium) | round number, reviewed SHA == bound HEAD, exactly one review comment on this PR with the `# AI Code Review — Round N` heading and the `ai-review-result` marker matching round/SHA/flag, findings invariant |
| `FIX` | Claude Code (`fable`, effort high) | `previous_head_sha` == current HEAD, every open finding ID resolved (`fixed` / `follow_up_created` / `no_change_with_rationale`), follow-up issues exist in this repo and are OPEN, actual PR HEAD == `new_head_sha`, a `fixed` resolution moved HEAD |
| `READY_FOR_MERGE` | nobody | holding state; `step` refuses to continue unless the merge gate is open |
| `MERGE` (gated) | controller, never an agent | last review clean and PR HEAD == reviewed HEAD; GitHub says not draft, every check succeeded, `mergeable=MERGEABLE`, `mergeStateStatus` `CLEAN`/`HAS_HOOKS`, no auto-merge armed, base branch has no merge queue; then `gh pr merge --<method> --match-head-commit <reviewed HEAD>`; counted only once GitHub reports `MERGED`. Conclusive negatives -> `BLOCKED`; inconclusive data (checks running, mergeability unknown, post-merge re-read failed) stays in `MERGE` for `resume`; HEAD drift -> `REVIEW` |

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
```

When the loop reaches `READY_FOR_MERGE` the CLI prints a banner with the
issue, PR, review round and reviewed HEAD, and states that automatic merge is
disabled. A human merges the PR.

URLs must be HTTPS GitHub issue URLs, and EPIC + issue must be in the **same
repository** as the current working directory (cross-repo runs are rejected).

## State directory

Default `.autoforge/` (overridable via `--state-dir` or config):

```text
.autoforge/
    state.json          # persisted run state (atomic writes)
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
`step_count`, `attempt` and `block_reason`.

## Security model

- One controller per repository (flock); a second instance exits with `LockError`.
- **Automatic merge is disabled in this milestone.** The `MERGE` phase is
  reachable only from `READY_FOR_MERGE` and only when **both**
  `safety.allow_merge: true` is set in config **and** `--allow-merge` is
  passed on the CLI. Default off; `run` normally stops at `READY_FOR_MERGE`.
- **Agents never merge.** When the gate is open, the *controller* performs the
  merge itself: `gh pr merge --<merge.method> --match-head-commit <reviewed HEAD>`
  through `GitHubClient`, with no prompt and no agent invocation. Every agent
  prompt carries the unconditional rule "never merge a pull request".
- **Pre-merge verification is controller-side and fails closed.** Before the
  write, GitHub must report: PR open at the reviewed HEAD, not a draft, every
  check in the status rollup succeeded (all checks, not only required ones),
  `mergeable = MERGEABLE`, `mergeStateStatus` in `CLEAN`/`HAS_HOOKS`, no
  auto-merge armed, and no merge queue on the base branch (`gh pr merge`
  would otherwise arm auto-merge or enqueue instead of merging, leaving an
  asynchronous merge the controller does not own). Conclusive negatives
  (conflict, failing check, branch protection, queue) -> `BLOCKED`;
  inconclusive data (checks still running, `mergeable = UNKNOWN`) raises and
  leaves the run in `MERGE` so `resume` re-checks. HEAD drift -> `REVIEW`.
- **Post-merge is reconciled from GitHub.** The merge is counted only after
  GitHub reports `MERGED` at the reviewed HEAD (idempotently, across crashes).
  If `gh pr merge` returns but the PR is still open, the run is `BLOCKED` and
  any auto-merge that call armed is disabled again (`gh pr merge
  --disable-auto`). If the post-merge re-read fails, the outcome is treated as
  unknown: the run stays in `MERGE` and `resume` re-inspects GitHub (an
  already-merged PR is recovered and counted once; an open one is re-verified).
- Logs and CLI output pass through baseline secret redaction (`GITHUB_TOKEN`,
  `GH_TOKEN`, `*_API_KEY`, `Authorization: Bearer`, `ghp_*`, `sk-*`, …). No
  environment dump is ever written. Baseline only — no claim of completeness.
- No `os.system` / `shell=True` anywhere; prompts travel as a single argv
  element so shell metacharacters in issue text cannot be interpreted.
  Agents run in a new session and the whole process group is killed on timeout.
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
