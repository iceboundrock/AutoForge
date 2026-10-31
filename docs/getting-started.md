# Getting started

This guide takes you from nothing to a first AutoForge run on a
repository of your own: install the command, prepare a throwaway
repository, write a configuration, check the environment with `doctor`,
preview a run, run it, and read where it stopped. It assumes you know git
and GitHub, and nothing about AutoForge.

AutoForge drives coding agents: Claude Code implements an issue, a reviewer
reads the pull request, and the controller checks every claim against
GitHub before the run moves on. With the defaults it **never merges**: a
run stops at `READY_FOR_MERGE` and you merge, or it stops at `BLOCKED` and
says why. A real run calls models you pay for, and the agents run as you,
on your machine. Read [Security boundaries](security.md) before the first
real run.

The steps that need no credential (installing, `--version`, `--help`,
`doctor`'s rows that need no sign-in, a dry run with each starter
configuration) are what `make dist-check` runs on every commit
([Development](development.md#the-built-distributions)). The steps that need
your logins are recorded by the [clean-room procedure](clean-room.md).

1. [Prerequisites](#1-prerequisites)
2. [Install AutoForge](#2-install-autoforge)
3. [Prepare a throwaway repository](#3-prepare-a-throwaway-repository)
4. [Write a configuration](#4-write-a-configuration)
5. [Check the environment: `doctor`](#5-check-the-environment-doctor)
6. [Preview, run, and read where it stopped](#6-preview-run-and-read-where-it-stopped)
7. [Common first-run failures](#7-common-first-run-failures)

## 1. Prerequisites

[Compatibility](compatibility.md) has every claim and its evidence; in
short:

| You need | Version | Why |
|---|---|---|
| Linux on x86_64 | | the platform the first prerelease claims; macOS is untested and Windows does not work |
| Python | 3.11 or 3.12 | the interpreters CI tests; `uv` can install one (`uv python install 3.12`) |
| [`uv`](https://docs.astral.sh/uv/) | | installs AutoForge into an environment of its own |
| git | | any recent version; `doctor` checks that `git --version` runs |
| [GitHub CLI](https://cli.github.com/) (`gh`), signed in | 2.48.0 or later | the controller works on GitHub with your `gh` login (`gh auth login`); [GitHub token permissions](security.md#github-token-permissions) lists what the token needs |
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude`), signed in | any release whose `claude --help` lists `--safe-mode`, `--strict-mcp-config` and `--setting-sources` | implements the issue and fixes review findings |
| [OpenCode](https://opencode.ai/) (`opencode`), with an OpenAI credential | 2.0.0 or later | reviews every round, and replans. Not needed with the Claude-only starter ([step 4](#4-write-a-configuration)) |

**Who pays.** AutoForge has no account and stores no model credential.
Each agent CLI uses your own login or key, and each vendor bills you:
Anthropic for Claude Code, OpenAI for the default OpenCode models
([Provider authentication and billing](compatibility.md#provider-authentication-and-billing)).
The CI your repository runs on the branches AutoForge pushes uses your
GitHub Actions minutes.

## 2. Install AutoForge

The distribution is **`autoforge-cli`**; the command is `autoforge`. Never
`pip install autoforge`: that is an unrelated project.

Install a published version from PyPI into an environment of its own:

```bash
uv tool install --python 3.12 --prerelease allow autoforge-cli
autoforge --version
autoforge --help
```

`--prerelease allow` is needed while every published version is a
prerelease. To try a commit instead, `uv tool install --python 3.12
'git+https://github.com/iceboundrock/AutoForge@<commit>'`; the README's
[Installation](../README.md#installation) has every method, upgrading and
removing.

If the shell then says `autoforge: command not found`, `uv` put the command
in a directory that is not on your `PATH`, and said so when it installed:

```text
warning: `/home/you/.local/bin` is not on your PATH. To use installed tools, run `export PATH="/home/you/.local/bin:$PATH"` or `uv tool update-shell`.
```

Run `uv tool update-shell` and open a new shell. `autoforge` then runs from
any directory; it always works on the git checkout you run it in.

## 3. Prepare a throwaway repository

Make the first run on a new repository **you control**, not on a project
you care about and not on a public repository where strangers comment.
Agents read the issue, its comments and the pull request, and anyone who
can write there can address them; an injected instruction runs as you
([Prompt injection, and public repositories in particular](security.md#prompt-injection-and-public-repositories-in-particular)).
A private repository keeps strangers out.

```bash
gh repo create first-run --private --add-readme --clone
cd first-run
```

`--add-readme` gives the default branch a first commit, which a run starts
from.

**Keep AutoForge's files out of git.** A run writes its state, prompts and
agent logs under `.autoforge/` in the checkout, and your configuration
will be `autoforge.yaml` there. Neither is ever to be committed. Exclude
them in this checkout only, without changing the repository:

```bash
printf '%s\n' '.autoforge/' 'autoforge.yaml' 'autoforge.local.*' >> .git/info/exclude
```

or add the same three lines to the repository's `.gitignore`. Everything
else AutoForge keeps is under `.git/autoforge/`, which git never tracks
([Keep AutoForge's files out of git](security.md#keep-autoforges-files-out-of-git)).

**The EPIC and the issue.** `autoforge run` takes two GitHub issue URLs,
both in this repository:

- the **issue** is the work: one change an agent can make and a reviewer can
  judge, with the acceptance criteria in its body. The controller hands its
  body and comments to the implementer, which gets no GitHub credential of
  its own;
- the **EPIC** is the issue the work belongs to. A run checks only that it
  is another issue of the same repository. AutoForge writes to it only
  after a merge it made itself, to post progress and pick the next issue
  (`UPDATE_EPIC`); with the merge gate closed, as here, the first run never
  writes to it.

Write the two bodies to files, for example `epic.md`:

```markdown
Tracking issue for trying AutoForge on this repository.

The first issue is a small script with a test, so that one run shows the
whole loop: implementation, pull request, review, and a stop at
READY_FOR_MERGE.
```

and `issue.md`:

```markdown
Add `greet.sh`, a POSIX shell script that prints a greeting.

## Acceptance criteria

- `sh greet.sh` prints `Hello, world!`.
- `sh greet.sh Ada` prints `Hello, Ada!`.
- `test_greet.sh` runs both cases and exits non-zero when either output
  differs.
- `README.md` gains a "Usage" section showing both commands.

## Out of scope

Anything else in the repository.
```

and create the issues, EPIC first:

```bash
gh issue create --title "Try AutoForge" --body-file epic.md
gh issue create --title "Add a greeting script" --body-file issue.md
rm epic.md issue.md
```

Each command prints the new issue's URL; in a new repository they are
`https://github.com/<you>/first-run/issues/1` and `.../issues/2`.

**Required checks.** AutoForge's merge gate (closed by default) trusts a
pull request whose checks all passed, and relies on a branch rule of the
default branch to say which check had to exist. `doctor` reads that rule
(the "default branch requires checks" row). A new repository has no CI and
no rule, and that is fine for a first run: with the gate closed the row is
a `WARN` (or a `SKIP` on a private repository of GitHub Free, which offers
no branch rules), the run stops at `READY_FOR_MERGE`, and you read the pull
request and its checks yourself before you merge. The row matters once you
want AutoForge to merge; then:

1. have CI report a status check on every pull request, and put its name in
   `safety.required_checks` (the starters say `ci`, AutoForge's own check);
2. add a ruleset on the default branch that requires that check
   (**Settings → Rules → Rulesets**, or classic branch protection); the
   row's text names the settings page and the rule to add;
3. run `doctor` again: the row is `OK`. Only then set `safety.allow_merge:
   true` ([Configuration](configuration.md), [GitHub safety: merge
   safety](agent-guides/github-safety.md#merge-safety)).

## 4. Write a configuration

Without `--config`, AutoForge uses its built-in routing. A starter
configuration writes a routing out, so you can see which models you pay for
and change them. Two are under [`examples/`](../examples):

| Starter | Phases | Needs | Review |
|---|---|---|---|
| [`starter-two-providers.yaml`](../examples/starter-two-providers.yaml) | Claude Code implements and fixes; OpenCode reviews, replans and updates the EPIC. Exactly the built-in routing | `claude` and `opencode`, two vendors | cross-vendor: another vendor's model reads what the implementer wrote |
| [`starter-claude-only.yaml`](../examples/starter-claude-only.yaml) | Claude Code for every phase | `claude` only, one vendor | **not cross-vendor**: the model that wrote the change also reviews it |

Save one as `autoforge.yaml` in the checkout (excluded from git in
step 3):

```bash
curl -fsSL -o autoforge.yaml \
  https://raw.githubusercontent.com/iceboundrock/AutoForge/main/examples/starter-two-providers.yaml
```

That is `main`'s copy; once the version you installed (`autoforge
--version`) has a tag with an `examples/` directory, `v<version>` in place
of `main` gives the copy that matches it.

Change a model if your account cannot use the one written there: `claude
--model` takes an alias (`fable`, `opus`, `sonnet`) or a full model name,
and `opencode models` lists OpenCode's. Every key is in
[`autoforge.example.yaml`](../autoforge.example.yaml) and
[Configuration](configuration.md).

Pass the file **before** the subcommand, on every command:

```bash
autoforge --config autoforge.yaml doctor
```

## 5. Check the environment: `doctor`

```bash
autoforge --config autoforge.yaml doctor
```

`doctor` reads; it starts no agent session, and writes nothing in the
checkout but an empty `.autoforge/`, to test that the state directory is
writable. Each row is one of:

- `OK`: checked and fine;
- `FAIL`: required; a run would fail on it. `doctor` exits 1 and says how
  many failed;
- `WARN`: not required; read it, and decide;
- `SKIP`: could not be checked, and the row says why.

On a first run, expect these rows:

| Row | What you may see | What it means, and what to do |
|---|---|---|
| `gh available`, `gh authenticated` | `FAIL` | `gh` is missing, older than 2.48.0, or not signed in. Install it, `gh auth login`, and run `doctor` again |
| `agent 'claude' available (...)`, `agent 'opencode' available (...)` | `FAIL ... executable not found: claude` | the CLI is not installed, or not on `PATH`. The row lists the profiles that use it: install it, or route those profiles to a CLI you have (the Claude-only starter) |
| `agent 'claude' keeps integrations off (...)` | `FAIL` | this Claude Code does not list `--safe-mode`, `--strict-mcp-config` and `--setting-sources` in `claude --help`: update it, since every launch passes them |
| | `SKIP ... stopped mid-line after 8192 bytes` | Claude Code wrote only part of its help to `doctor` (2.1.314 does when its output is a pipe), so the flags could not be looked up. A launch refuses a Claude Code without them anyway; to check by hand, run `claude --help > claude-help.txt` and look for the three flags in the file |
| `default branch requires checks` | `WARN ... no rule requires a status check` (with the merge gate closed) | expected on a new repository, see [Required checks](#3-prepare-a-throwaway-repository). It becomes a `FAIL` if you open the merge gate |
| | `SKIP` | the rules could not be read: the plan offers none (a private repository on GitHub Free), or the token cannot read them. The row names which, and the way out |
| `pull request check runs readable` | `SKIP ... UNKNOWN: no recently updated open pull request ... has a check result` | nothing to try it on yet. It matters only with the merge gate open |
| `outer sandbox: mode (disabled)` | `WARN` | agents run as you, without the outer sandbox. That is the default; [Security boundaries](security.md#the-outer-sandbox-is-opt-in-and-linux-only) explains the choice |
| `config` | `FAIL` | the file is missing or wrong; the row and [section 7](#7-common-first-run-failures) say which |

Every other row should be `OK`. `doctor --json` prints the same rows as
JSON, for a record or a script.

**Recording the versions.** The clean-room procedure and a bug report need
what you ran. `doctor` prints the versions it checks (`git available`,
`gh available`, each agent's row); record them all with:

```bash
{ autoforge --version; git --version; gh --version | head -1
  claude --version; opencode --version; } > versions.txt
```

## 6. Preview, run, and read where it stopped

**Preview.** A dry run prints the plan of the first phases, the agent
command and its limits, and the prompt; it starts no process, calls no
`gh`, and writes no state:

```bash
autoforge --config autoforge.yaml run \
  --epic https://github.com/<you>/first-run/issues/1 \
  --issue https://github.com/<you>/first-run/issues/2 --dry-run
```

It starts with:

```text
Phase:    INITIALIZING
Profile:  (none — deterministic transition) (provider=(none), model=(none), effort=(none))
Routing:  INITIALIZING -> ANALYZE_EXECUTE (verify repository + issue; no agent)
...
Phase:    ANALYZE_EXECUTE
Profile:  analyze_execute (provider=claude, model=fable, effort=high)
Template: analyze_execute.md
Command:  claude -p --output-format stream-json --verbose --model fable --effort high --permission-mode bypassPermissions --no-session-persistence --safe-mode --strict-mcp-config --setting-sources=user -- <prompt>
```

`--full-prompt` prints the whole prompt the implementer would get.

**Run.** The same command without `--dry-run` creates the run and drives it:
the implementer works in a git worktree of its own under
`.git/autoforge/worktrees/2`, never in your checkout; the controller checks
its commit, pushes it to the branch `autoforge/2`, opens the pull request,
has it reviewed, posts each review as a comment on the pull request, and
has findings fixed, until the run stops. Progress lines go to the
terminal. A run can take a while, and every model call costs money.

```bash
autoforge --config autoforge.yaml run \
  --epic https://github.com/<you>/first-run/issues/1 \
  --issue https://github.com/<you>/first-run/issues/2
```

While it runs, do not commit or switch branches in this checkout: the
controller compares its HEAD and branch around every agent launch, and a
change stops the run at `BLOCKED`. Uncommitted edits are not part of what
the agents start from. `Ctrl-C` is safe: `autoforge --config autoforge.yaml
resume` continues where it stopped.

**Status.** From the checkout, at any time:

```bash
autoforge --config autoforge.yaml status
```

prints the run, its issue and pull request, the phase, the review rounds,
the reviewed HEAD and, when blocked, the reason. A run that `gh` could not
start reads:

```text
Run:        af-20261031T044814Z-123831
Repository: you/first-run
EPIC:       #1 https://github.com/you/first-run/issues/1
Issue:      #2 https://github.com/you/first-run/issues/2
PR:         #- -
...
Phase:      INITIALIZING
```

**`READY_FOR_MERGE`.** The review found nothing left to fix. The run prints:

```text
========================================================================
AutoForge workflow reached READY_FOR_MERGE.
  Issue:         https://github.com/<you>/first-run/issues/2
  PR:            https://github.com/<you>/first-run/pull/3
  Review round:  1
  Reviewed HEAD: <commit>
  ...
The merge gate is closed: a human must review and merge the PR.
(AutoForge merges only with 'safety.allow_merge: true' in the config and --allow-merge.)
========================================================================
```

Read the pull request and the review comment, and merge it on GitHub if you
agree, or close it. The run stays at `READY_FOR_MERGE`; for the next issue,
`autoforge run --force --epic ... --issue ...` replaces it. Each issue keeps
its agent worktree under `.git/autoforge/worktrees/<number>`; `git worktree
list` shows them, and deleting the throwaway checkout removes everything.

**`BLOCKED`.** The controller found something it will not decide alone: a
check that failed, a claim it could not verify, a bound reached. The run
prints `run <id> is BLOCKED: <reason>`, and `autoforge status` shows the
reason again. Fix the cause, then:

```bash
autoforge --config autoforge.yaml unblock --reason "what you changed"
autoforge --config autoforge.yaml resume
```

`unblock` re-reads GitHub and re-enters the one phase that supports, or
refuses and stays `BLOCKED`. What each block means and how to leave it is
in [Running the remote workflow: BLOCKED](usage.md#blocked-fix-the-cause-then-unblock)
and [Troubleshooting and recovery](usage.md#troubleshooting-and-recovery).

## 7. Common first-run failures

The messages below are AutoForge's own, as it prints them; `<...>` stands
for your values. They cover everything up to a run that exists; once a run
has a phase, see [Troubleshooting and recovery](usage.md#troubleshooting-and-recovery).

**`autoforge: command not found`.** `uv`'s tool directory is not on `PATH`:
`uv tool update-shell`, then a new shell ([step 2](#2-install-autoforge)).

**The configuration file.**

| Message | Fix |
|---|---|
| `autoforge: error: unrecognized arguments: --config autoforge.yaml` | `--config` goes before the subcommand: `autoforge --config autoforge.yaml doctor` |
| `[FAIL] config: config file not found: autoforge.yaml` | run from the checkout that holds the file, or give its path |
| `autoforge: error: <path>: unknown key(s) under 'safety': allow_merges (known: acceptance_actions, allow_merge, protected_merge_paths, required_checks, verify_check_definition)` | a misspelt key; every section accepts only the keys it lists |

**The repository and the URLs.**

| Message | Fix |
|---|---|
| `autoforge: error: cannot derive the controller lock: <dir> is not inside a git repository (fatal: not a git repository ...)` | run AutoForge from the checkout of the repository the issues belong to |
| `autoforge: error: EPIC repo 'you/first-run' != issue repo 'someone/else'; cross-repository runs are not supported` | the EPIC and the issue must be issues of the same repository, the checkout's |
| `repository mismatch: this checkout's 'origin' is <repo> but the issue/epic belong to <repo>` | the checkout's `origin` is another repository: run from a clone of the issues' repository |
| `autoforge: error: GitHub URL must use https, got http scheme: '<url>'` | pass `https://github.com/...` URLs |
| `autoforge: error: expected a GitHub issue URL, got pr: '<url>'` | `--epic` and `--issue` take issues, not pull requests |

**`gh` and the agent CLIs.**

| Message | Fix |
|---|---|
| `[FAIL] gh available: ExecutionError: executable not found: gh (...)` | install `gh` |
| `[FAIL] gh authenticated: You are not logged into any GitHub hosts. To log in, run: gh auth login` | `gh auth login` |
| ``autoforge: error: `gh repo view <repo> --json nameWithOwner,defaultBranchRef` failed (exit 1): HTTP 401: Requires authentication ...`` | `run` was started before `gh auth login`. The run exists, at `INITIALIZING`: sign in, then `autoforge resume` |
| `[FAIL] agent 'claude' available (analyze_execute, fix): ExecutionError: executable not found: claude (...)` | install Claude Code, or move those profiles to a CLI you have; `opencode` the same way |
| a step fails at its first agent launch: the run prints the failure, then `State unchanged.`, the step's `logs:` directory and the `resume:` command | the agent CLI refused the launch, for example a model your account cannot use or a CLI not signed in. The CLI's own message is in that `logs:` directory. Sign in, or fix the model in `autoforge.yaml` (`resume` then asks for `--accept-config-change`: [The configuration a run was recorded under](usage.md#the-configuration-a-run-was-recorded-under)), then `resume` |

**A run that already exists, or another controller.**

| Message | Fix |
|---|---|
| `autoforge: error: existing run af-<id> in phase INITIALIZING — use 'resume' to continue or 'run --force' to discard it` | `autoforge resume` continues it; `run --force` replaces it |
| `autoforge: error: another AutoForge controller holds <repo>/.git/autoforge/controller.lock — refusing to run concurrently on the same repository (...)` | another `autoforge` (or a scheduler) is running in this repository: wait for it, or stop it |

**A dirty checkout** is not refused: agents work in a worktree of their
own, made from the default branch as GitHub has it, not from your working
tree. Committing or switching branches in the checkout during a run is what
stops it (`BLOCKED`).

**An agent that asks for a permission or a question** gets no answer in an
unattended run; what each CLI does then is in
[An agent asked for permission or input](usage.md#an-agent-asked-for-permission-or-input).
