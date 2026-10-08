# AutoForge / 智铸

**Autonomous orchestration for AI-driven software development.**

智铸：AI 驱动的软件工程自动化控制器。

> **Early development (Phase 2).** The first real AI development loop is
> wired end to end: GitHub issue → Claude Code implementation → controller
> push and PR → OpenCode review → Claude Code remediation → repeated review
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
Issue → ANALYZE_EXECUTE (Claude Code) → controller pushes, opens PR → REVIEW (OpenCode)
       → clean ───────────────────────────────▶ READY_FOR_MERGE
       → findings ────────────────────────────▶ FIX (Claude Code) → REVIEW …
       → excessive / low-finding stagnation ──▶ REPLAN_REEXECUTE (OpenCode)
                                                    → replacement PR → REVIEW round 1
                                       ├─ merge gate closed (default): stops here; human merges
                                       └─ gate open: controller verifies on GitHub
                                          → MERGE (gh pr merge by the controller, no agent)
                                          → UPDATE_EPIC (agent returns the progress text
                                             and the roadmap section and picks the next
                                             issue; the controller posts the progress comment,
                                             splices the section into the EPIC body every N
                                             merges, reads both back, and verifies the next
                                             issue on GitHub, or DONE)
```

AutoForge itself never writes business code. It holds the workflow state
machine, selects which provider, model and effort runs each phase, renders
strictly validated prompts from file templates, invokes `claude` /
`opencode` through provider adapters and `gh` through a typed client (argv
lists only, never a shell), parses the machine-readable `CONTROL_RESULT`
block from agent output, and verifies every claim on GitHub before it
advances state. Every invocation is logged, redacted, under
`<state dir>/logs/<run-id>/`; the state directory defaults to `.autoforge/`
for a remote run and to `<git dir>/autoforge/state` for a
[local run](docs/local-mode.md#resume-and-recovery).

It is a single-machine client tool, not a distributed service: one process
on your own machine, driving your `git`, `gh`, and agent CLIs under your
credentials, with local per-checkout state and a single
OS-level repository lock instead of any coordinator. The expected failure
modes are a `Ctrl-C`, a killed process, and a reboot, which is why side
effects are checkpointed before they are performed. What *is* remote and
concurrent is GitHub and the humans acting on it, which is why nothing
GitHub-side is believed without being read back. AutoForge never commits to
the default branch.

There is a second, explicit workflow for when there is no GitHub to
orchestrate: [Local mode](#local-mode-no-github) drives a feature Markdown
file against your working tree with the same controller and the same
verification discipline, and the controller makes zero `gh` invocations.

## Key properties

Each line is a summary; the linked document is the precise statement.

- **Agent output is a claim; the controller decides.** State advances only
  on a strictly validated `CONTROL_RESULT` block plus independent
  verification, never on prose such as "done" or "LGTM". An invalid
  transition fails explicitly, and when recovery cannot determine the safe
  state the run enters `BLOCKED` rather than guess.
  ([CONTROL_RESULT protocol](docs/agent-guides/control-result-protocol.md),
  [workflow](docs/agent-guides/workflow.md))
- **GitHub is read back, not believed.** Each phase re-reads what its agent
  claims to have done, or what the controller did on its behalf: the
  implementation commit is the worktree's own HEAD and descends from the
  default branch, and the PR the controller opened for it is open, at that
  HEAD, on the issue's branch; the review comment the controller rendered
  from the reviewer's result is on that PR, exactly once, for the right
  round and SHA; the fix commit is the fixer's own HEAD, descends from the
  reviewed HEAD and is the PR's head once the controller has pushed it; the
  follow-up issues the controller created or recorded a deferral in are the
  one open issue per finding; the EPIC update and the next issue exist.
  ([GitHub safety](docs/agent-guides/github-safety.md))
- **Agents never merge.** Every agent prompt carries the rule "never merge a
  pull request" (LOCAL prompts forbid `git merge` and have no PR to merge).
  The merge is performed by the controller itself
  (`gh pr merge --<method> --match-head-commit <reviewed HEAD>`), with no
  agent invocation.
- **Merging is off by default and opt-in.** `MERGE` is reachable only when
  both `safety.allow_merge: true` is set in config *and* `--allow-merge` is
  passed on the CLI; otherwise the run stops at `READY_FOR_MERGE` and a
  human merges. Before merging, the controller re-verifies the PR on GitHub
  and fails closed: a conclusive negative is `BLOCKED`, inconclusive data
  keeps the phase for a bounded number of `resume --allow-merge` re-checks
  and then is `BLOCKED`, and nothing is merged in either case. By default a PR that
  changes a protected path (`.github/workflows/`) is never merged
  unattended, and a required check is trusted only if it ran the
  base branch's job and step structure. `doctor` checks that the default
  branch still requires the CI check; the merge gate itself does not yet
  consult branch rules. The merge is counted only after GitHub reports
  `MERGED` at the reviewed HEAD into the reviewed base.
  ([Merge safety](docs/agent-guides/github-safety.md#merge-safety))
- **Reviews are bound to a revision.** A clean review is bound to the PR it
  was posted on (by repository and number), its HEAD SHA, its base branch
  and its merge base. If the HEAD, the base branch or the merge base moves
  after the review, the run goes back to `REVIEW` instead of merging, and a
  different PR than the reviewed one is `BLOCKED`; a local review is bound
  to a workspace fingerprint in the same way.
  ([Review binding](docs/agent-guides/workflow.md#bind-reviews-to-pr-head-sha-and-to-the-pr-identity))
- **State is durable and runs are resumable.** State is written atomically
  and side effects are checkpointed before they are performed. `resume`
  re-enters the interrupted phase and reconciles with GitHub first; a
  corrupt state file fails loudly instead of being reset. One controller
  runs per checkout under an OS file lock keyed by the git common dir
  (linked worktrees share it; independent clones are not coordinated). Review rounds, stagnation and a
  cumulative step budget bound every run, so it cannot loop forever.
  ([State and recovery](docs/agent-guides/state-and-recovery.md))
- **Agents are separated from your checkout and shell, not sandboxed.** A
  remote agent runs in a per-issue `git worktree` the controller creates,
  with an allow-listed environment rather than the operator's whole shell,
  and the controller blocks if its own checkout's HEAD or branch moved while
  the agent ran. Whatever an agent leaves running is killed when its
  invocation ends, and what the kill could not remove is reported rather
  than hidden
  ([ADR 0002](docs/adr/0002-executor-nothing-outlives-the-invocation.md)).
  The agent still runs as the operator, with network access and the
  allow-listed credentials.
  ([Usage](docs/usage.md#how-agents-are-separated-from-your-checkout))
- **The controller's local run is GitHub-free by construction, not by
  convention.** Local mode needs no GitHub: a local run never constructs the
  GitHub client, so no authentication is needed and none is consulted. The
  agent is outside that construction: it is asked not to touch GitHub, and
  only a moved git anchor (a commit, reset, checkout or branch switch) is
  detected; a `gh` write or a push from the agent is not
  (see [Local mode](#local-mode-no-github)).
- **Logs and CLI output are redacted.** Tokens, API keys and authorization
  headers are masked (a baseline, with no claim of completeness), and no
  environment dump is ever written.
  ([Secrets and logging](docs/agent-guides/secrets-and-logging.md))

## Prerequisites

- Python 3.11+ (managed via `uv`)
- `uv` ([install](https://docs.astral.sh/uv/getting-started/installation/))
- `git`, and `gh` (GitHub CLI **2.48.0 or newer**, authenticated): the
  client lists branch rules and a PR's changed files with
  `gh api --paginate --slurp`, which older releases reject; `autoforge doctor`
  refuses an older `gh` up front. `gh` is **not** needed for
  [local mode](#local-mode-no-github)
- `claude` (Claude Code CLI) for `analyze_execute` / `fix` profiles
- `opencode` (OpenCode CLI) 2.0.0 or newer for the `review_*`,
  `replan_reexecute` and `update_epic` profiles; 1.x is refused by `doctor`
  (see [OpenCode profiles](docs/configuration.md#opencode-profiles))
- `pi` 1.0.0 or newer, only if a profile you route to uses `provider: pi`
  (see [Pi profiles](docs/configuration.md#pi-profiles)). `doctor` checks
  only the agent CLIs your configured profiles actually reach
- a state directory (`.autoforge/` by default; `--state-dir` or
  `state_dir` in config) on a filesystem with hard links: run logs and
  other controller files are published by `link(2)` so that a crash never
  leaves one half-written, and there is no non-atomic fallback. vfat/exFAT
  and some FUSE, SMB/CIFS and overlay mounts have no hard links; a run, and
  `autoforge doctor`, refuse such a state directory and say why

Run `autoforge doctor` to check all of the above (read-only apart from
creating the state directory if it is missing and a probe file it removes),
or
`autoforge local doctor` for the local-mode subset (no `gh`, no GitHub
authentication, no `origin` remote).

CLI flag syntax in `autoforge.example.yaml` was checked against the locally
installed CLIs (Claude Code 2.1.263, OpenCode 2.0.23, gh 2.100.0).

## Installation

```bash
uv sync                  # create .venv, install autoforge (editable) + dev tools
uv sync --extra yaml     # optional: full YAML config support (PyYAML)
uv run autoforge --help
```

To configure profiles, bounds or the merge gate, copy
`autoforge.example.yaml` to `autoforge.yaml` and pass it with `--config`
before the subcommand; see [Configuration](docs/configuration.md).

## Quick start

Run these from a checkout of the repository the issues belong to. URLs must
be HTTPS GitHub issue URLs, and the EPIC and the issue must be in that same
repository (cross-repo runs are rejected).

```bash
# 1. check the environment (writes nothing but a state-dir probe)
uv run autoforge doctor

# 2. preview without touching anything (no subprocess, no gh call, no state file)
uv run autoforge run --epic https://github.com/owner/repo/issues/1 \
               --issue https://github.com/owner/repo/issues/2 --dry-run

# 3. create a run and drive it until READY_FOR_MERGE / BLOCKED / FAILED
uv run autoforge run --epic https://github.com/owner/repo/issues/1 \
               --issue https://github.com/owner/repo/issues/2

# 4. inspect it, and continue after an interruption
uv run autoforge status
uv run autoforge resume
```

With the merge gate closed (the default) the run stops at
`READY_FOR_MERGE` with a banner naming the issue, PR, review round and
reviewed HEAD, and a human merges the PR. Only with `safety.allow_merge:
true` in config does `uv run autoforge resume --allow-merge` let the
controller verify the PR on GitHub and merge it itself.

A run in `BLOCKED` stays there until you fix the cause and run
`autoforge unblock --reason "..."`, which re-inspects GitHub and either
re-enters the one phase that inspection supports or refuses. Every command,
where a run stops, and how to recover are in
[Running the remote workflow](docs/usage.md).

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

The *controller* never touches GitHub in a local run: no `gh` invocation,
no Issue, no PR, no comment, no push, no branch, no merge, no EPIC update, no
follow-up issue, and no replan. The `GitHubClient` is not even constructed.
Nothing is faked either: there are no placeholder PR URLs in local state or
local prompts.

That is a property of the controller, not of the agents it launches. An agent
is a same-UID subprocess with `gh` on `PATH` and network access; it is
instructed not to use them, and the controller can neither prevent nor detect
a `gh` write, a `git push` or a network request made by the agent. What the
controller *does* detect is a moved git anchor: HEAD and the checked-out branch
are compared with the values the run was pinned to before and after every
phase, so a commit, reset, checkout or branch switch blocks the run. It detects
nothing else. Real containment is a sandbox, which LOCAL v1 does not provide
([ADR 0001](docs/adr/0001-local-mode-workspace-identity-and-filesystem-boundary.md)
§2.2 and §8.1; isolation is tracked in #10).

```bash
uv run autoforge local doctor                    # config, git, agent CLIs; never gh
uv run autoforge local init add-transaction-filter
# a human (with or without an AI) refines features/add-transaction-filter.md
uv run autoforge local run features/add-transaction-filter.md --dry-run
uv run autoforge local run features/add-transaction-filter.md
```

The specification is frozen for the whole run (its SHA-256 is re-checked
before and after every agent phase), every review is bound to a fingerprint
of the working tree, and the controller never commits, stages, pushes,
stashes, resets or switches branches in a local run. The [Local mode guide](docs/local-mode.md) covers the
workspace fingerprint, dirty working trees, the review/fix bound, validation
commands, and resume and recovery.

## Documentation

The [documentation index](docs/README.md) lists everything by intent. The
most common entry points:

| I want to… | Read |
|---|---|
| run the remote workflow, merge, unblock, troubleshoot or recover a run | [Running the remote workflow](docs/usage.md), [troubleshooting and recovery](docs/usage.md#troubleshooting-and-recovery) |
| work without GitHub | [Local mode](docs/local-mode.md) |
| configure profiles, bounds and the merge gate | [Configuration](docs/configuration.md), [`autoforge.example.yaml`](autoforge.example.yaml) |
| understand the phases and legal transitions | [Workflow](docs/agent-guides/workflow.md) |
| know what is verified on GitHub, and what gates a merge | [GitHub safety](docs/agent-guides/github-safety.md) |
| know how state, locking and crash recovery work | [State and recovery](docs/agent-guides/state-and-recovery.md) |
| see how the code is divided | [Architecture](docs/agent-guides/architecture.md), [module map](src/autoforge/AGENTS.md) |
| contribute, run the checks, write tests | [Development](docs/development.md), [Testing](docs/agent-guides/testing.md) |
| read the design records | [ADR 0001](docs/adr/0001-local-mode-workspace-identity-and-filesystem-boundary.md), [ADR 0002](docs/adr/0002-executor-nothing-outlives-the-invocation.md), [ADR 0003](docs/adr/0003-pi-agent-provider.md) |

Coding agents working on this repository start at [AGENTS.md](AGENTS.md).

## Current maturity

**Supported now (Phase 2):** GitHub issue → Claude Code implementation → the
controller's push and PR, read back via `gh` → OpenCode review with
round-based model routing → Claude Code remediation of finding IDs →
repeated review with SHA binding → `READY_FOR_MERGE`; the bounded REVIEW/FIX
loop with stagnation detection and the `REPLAN_REEXECUTE` transaction;
journal-first recovery of an interrupted push or PR creation;
bounded correction retry for malformed results; `doctor`; redacted
per-invocation logs; LOCAL mode against a feature Markdown file.

**Implemented and verified, but exercised only against the in-memory fake:**
the controller-owned `MERGE` step behind the merge safety gate (off by
default), including its pre-merge verification, and `UPDATE_EPIC`. Every
claim these phases depend on is re-read from GitHub before state advances,
but no automated test has ever merged a real PR or edited a real EPIC, so
treat an opened gate as unproven against real services.

**Explicitly not yet:** unattended production operation; the controller
feeding CI results into the review prompt (the reviewer is asked to inspect
`gh pr checks` itself, and the merge gate reads the checks); the merge gate
consulting branch rules (`doctor` reads them, the gate does not); dequeuing a
PR from a merge queue (the controller refuses to merge into queue-protected
branches instead).
