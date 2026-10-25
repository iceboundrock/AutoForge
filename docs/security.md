# Security boundaries

This page is for operators. It says what AutoForge protects, what it does
not, which security issues are open, which GitHub token permissions it
needs, which of its files to keep out of git, and how to try it on a
repository you care about. Each section
names the document that holds the precise statement; this page is the
summary, and where they differ the linked document wins. To report a
vulnerability, see [`SECURITY.md`](../SECURITY.md).

## The short version

AutoForge launches coding agents (Claude Code, OpenCode, Pi) that **run as
you**: your user ID, your network, and the model credentials you give
them. It holds your GitHub credential itself and keeps it from those
agents, makes every GitHub write itself, and merges nothing unless you open
the merge gate twice. It does **not** sandbox agents unless you set
`sandbox.mode: required` on a Linux host that can enforce it, so by default
an agent, or whoever steers it through the issues, comments and code it
reads, can do anything your user can do on your machine without a GitHub
credential: read your files, use the network, spend your model budget.

Treat every agent run as running code from the people who can write the
issue, its comments and the repository: run AutoForge on repositories and
issues whose authors you trust, or inside a dedicated user, VM or container,
or under the outer sandbox.

## What AutoForge protects

- **No GitHub credential reaches an agent or a repository command through
  anything AutoForge controls.** Agents, LOCAL validation commands
  (`local.validation_commands`), REMOTE candidate validation commands
  (`validation.commands`) and pre-merge commands
  (`merge.verification_commands`) start from an allow-listed environment
  in which no variable `gh` or git reads a credential from can be listed,
  an empty
  `gh` configuration, a placeholder `GH_TOKEN`, a git configuration that
  switches off credential helpers, askpass, the SSH agent and SSH keys, an
  empty curl and wget configuration, and shells that read none of your
  startup files; the provider CLIs load no MCP server, plugin or hook. A
  launch is refused, before anything is saved or charged, while a
  repository git setting, `~/.netrc`, a login shell's startup file, a
  Claude Code settings file or an organization connected to OpenCode would
  still hand one over. `autoforge doctor` runs the same check (row "no
  credential reaches a launch").
  ([GitHub safety: agents hold no GitHub credential](agent-guides/github-safety.md#agents-hold-no-github-credential),
  [Configuration: agent environment](configuration.md#agent-environment))
- **Every GitHub write is the controller's.** Pushes, the PR, review
  comments, follow-up issues, EPIC updates and acceptance comments are
  typed controller effects: journaled before they are sent, read back
  after, and reconciled with GitHub on resume instead of being repeated.
  Agents read GitHub through redacted, bounded files the controller wrote.
  Comments on other issues that a fix asks for are posted by you, never by
  the controller.
  ([ADR 0004](adr/0004-authority-boundary-and-typed-external-effects.md),
  [GitHub safety: controller-owned effects](agent-guides/github-safety.md#controller-owned-effects))
- **Agent text is checked before it is published.** Text an agent asks the
  controller to publish under your identity is refused, never rewritten,
  when it forges a controller marker, carries a credential the redaction
  baseline recognises, uses a closing keyword on an issue reference, or
  mentions a user or team outside code; a pushed commit's message is held
  to the credential and closing-keyword rules.
  ([GitHub safety: published content](agent-guides/github-safety.md#published-content))
- **Merging is off by default and controller-only.** The controller merges
  only when `safety.allow_merge: true` is in the configuration **and**
  `--allow-merge` is on the command line, after re-verifying the PR, its
  checks and its review on GitHub, and only at the reviewed HEAD
  (`gh pr merge --match-head-commit`). A PR that changes
  `.github/workflows/` is never merged unattended. No agent is ever asked
  to merge. AutoForge never pushes to the default branch, pushes a branch
  only as a new branch or a fast-forward, deletes no worktree, and deletes
  no branch unless you set `merge.delete_branch: true`.
  ([GitHub safety: merge safety](agent-guides/github-safety.md#merge-safety))
- **Agent output is a claim.** State advances only on a strictly validated
  result plus the controller's own reads of GitHub and git; nothing an
  agent writes changes the routing, the review policy or the merge gate.
  ([CONTROL_RESULT protocol](agent-guides/control-result-protocol.md),
  [Workflow](agent-guides/workflow.md))
- **Redaction baseline.** Run logs, the state file's quoted text, the read
  context and everything the CLI prints pass through redaction of
  well-known token shapes, credential variables, authorization headers and
  URL passwords, and no environment dump is ever written.
  ([Secrets and logging](agent-guides/secrets-and-logging.md))
- **What an agent leaves running is stopped when its invocation ends,** on
  Linux: the executor kills the launch's process group and the orphans it
  catches as a subreaper, and reports what it could not remove. This is
  containment, not a promise that nothing outlives a launch: some
  processes escape it, and the executor cannot report those
  ([Other limits](#other-limits)).
  ([ADR 0002](adr/0002-executor-nothing-outlives-the-invocation.md))
- **The outer sandbox, when you enable it.** Under `sandbox.mode: required`
  every agent, validation command and pre-merge command runs inside
  bubblewrap and Landlock or is refused before the step: an empty root with
  the system runtime, its worktree, read-only git metadata, a private home
  and what its provider declares. Your home, your `gh` and git credential
  files, `~/.ssh`, the SSH agent, the session bus, controller state, the
  lock and other worktrees are absent. A run keeps the mode it was started
  under unless you accept a weaker one, and a configuration file a launch
  could rewrite is refused
  ([below](#the-outer-sandbox-is-opt-in-and-linux-only)).
  ([Usage: outer sandbox](usage.md#outer-sandbox),
  [GitHub safety: outer sandbox threat model](agent-guides/github-safety.md#outer-sandbox-threat-model-adr-0005))

## What AutoForge does not protect

These are limits by design. Each has a mitigation in
[Recommended settings](#recommended-settings-for-a-repository-you-care-about).

### Agents run as you

Under the default `sandbox.mode: disabled` an agent is an ordinary process
of your user with network access, and it runs unattended: Claude Code
profiles run with `--permission-mode bypassPermissions`, OpenCode's shell
and edit tools run without asking, and Pi runs its tools. An agent can:

- **read any file your user can,** by absolute path, or by a relative one:
  the default agent worktree lies under your checkout's `.git`. That
  includes your `gh` login (`~/.config/gh/hosts.yml`),
  `~/.git-credentials`, `~/.ssh/`, a keyring reachable over the session
  bus, cloud credentials, other projects, and every run's state, logs and
  read context. The credential separation above covers everything
  AutoForge hands a launch; it does not stop a process that goes and reads
  one of those files deliberately. This is the **same-user residual**.
- **write any file your user can,** including AutoForge's configuration
  file, its state directory and effect journal (neither carries a keyed
  integrity check), your shell startup files and your `gh`
  and git configuration, which the controller itself reads.
- **use the network** to send anything it can read, and reach services on
  loopback.
- **use, and spend, the model credential** its provider is given; every
  tool it runs can read that credential too.

What AutoForge checks around an agent is narrow: the HEAD and branch of
your checkout before and after each launch, the commits it reports, and
everything it claims about GitHub.

### Prompt injection, and public repositories in particular

Issue bodies, comments, review comments, pull request text and the code
itself are untrusted project data. Every prompt says so, names them as
quoted evidence and tells the agent to ignore instructions in them, and no
agent output can change the controller's policy, its routing or the merge
gate. A model can still follow injected instructions, and with the
defaults above an injected instruction runs as you.

On a **public repository**, anyone who can open or comment on the issue or
PR an agent reads can address that agent; AutoForge does not filter
comments by author. Injected text may lead to command execution on your
machine and to reading your model keys, SSH keys or `gh` credential file
(the same-user residual). It cannot make the controller publish a
credential the redaction baseline recognises, forge a controller marker,
close an issue or merge. Two choices are an agent's and so can be
steered: whether a review has findings, and, in a sequential EPIC run,
which open issue of the repository `UPDATE_EPIC` picks next (any open
issue, not only one the EPIC lists, so on a public repository possibly one
a stranger opened). `autoforge epic run` admits only the EPIC's own
sub-issues.

### The outer sandbox is opt-in and Linux-only

`sandbox.mode` defaults to `disabled`: logical isolation only, which is not
a sandbox. `required` needs a Linux host that can enforce it (`bwrap`,
unprivileged user, PID and IPC namespaces, an overlay mount, Landlock ABI 6;
in practice a 6.12 or newer kernel) and refuses a host that cannot;
`autoforge doctor` shows which you have. Hosted CI cannot enforce it, so its
real-backend tests run only on a capable host (`make test-sandbox`). Even
enforced, an agent still runs as your UID, keeps the host network (loopback
services included) and can read the model credential its provider is
given. There is no sandbox on macOS.

Every enforced launch writes a tree of the repository: the repository root
in LOCAL mode, the issue's agent worktree in REMOTE mode, and the
repository's sandbox directories in both. A launch also writes the
directory its provider is given read-write, which lies wherever you put
it and outlives the launch: a dedicated `CLAUDE_CONFIG_DIR`,
`PI_CODING_AGENT_DIR` or OpenCode database directory (`OPENCODE_DB`'s).
AutoForge reads its configuration file again at every command, so a file
an agent could rewrite would decide what the next command runs,
`sandbox.mode`, `github.command` and the profiles' commands included, and
a rewrite that keeps `sandbox.mode` as it was is not seen by the pin
below. Under `required` or `preferred` every command, a scheduler's worker
and `autoforge doctor` therefore refuse a `--config` file that lies in the
checkout, in any worktree registered to it or in a provider's read-write
directory, that is reached through a symbolic link in one, or that has a
second hard link; the refusal names the file, the directory and the fix
(move it outside, for example to `~/.config/autoforge/`, or set
`sandbox.mode: disabled`), and `doctor` runs none of the commands such a
file names. The path is followed as the command opens it, never tidied
first: a `..` after a symbolic link is taken where the link leads, and a
path that passes through such a directory and leaves it again by `..` is
refused too, since an agent could replace the entry it passes through.

The providers' directories are the ones every provider AutoForge knows
would be given under the environment the command runs in, the one its own
launches inherit: those of the providers the file's profiles name, and
those of the others too, since an agent that rewrote the file could name
them and another configuration's launches write their directories. A
directory a provider was given only under another environment (a
`CLAUDE_CONFIG_DIR` exported in one shell and not in the one a later
command runs from) is not known to that command. Run every command of a
run, and `doctor`, from the same environment.

A run is also pinned to the `sandbox.mode` it was created under. A later
`step`, `resume` or `unblock` under a weaker mode is refused before
anything runs: `required` to `preferred` or `disabled` in LOCAL mode, and
`required` or `preferred` to `disabled` in REMOTE mode, where `preferred`
is `required`. That includes a command run without the run's `--config`,
which falls back to the built-in `disabled`. To continue under the weaker
mode anyway, repeat the command with `--accept-config-change`: the change
is written to the run log first, and the run is pinned to the new mode.
A stronger mode is pinned without asking. `autoforge status` shows the pin.

### Redaction is a baseline

Redaction recognises well-known shapes and variable names and makes no
claim of completeness. A secret of an unrecognised shape that an agent
prints, or that a command echoes, can reach a run log, and agent text that
carries one can be published. The state directory is stored in the clear.
Among the shapes not recognised today are Google API keys (`AIza...`),
which an OpenCode launch receives whenever your shell sets a `GEMINI_*` or
`GOOGLE_*` variable, AWS access keys, private-key blocks, and a key
assigned to a prefixed name (`GEMINI_API_KEY=...`) whose value has no known
shape. Keep such variables out of the shell you run AutoForge from unless
a profile needs them.

### Your content goes to model vendors

Everything an agent is given or reads (the prompt, the issue and its
comments, PR text, review comments, the repository's code and command
output) is sent to the model vendor of the provider and model you
configured, under that vendor's terms. AutoForge itself makes no network
connection of its own and sends no telemetry: it reaches GitHub through
`gh` and git, and model vendors only through the agent CLIs it launches.

### Pushed code runs in your CI

The controller pushes the agent's commits to a branch of your repository
(`autoforge/<issue>`) before any review, and opens the PR. Your workflows
triggered by `push` or `pull_request` then run that code, with whatever
secrets and `GITHUB_TOKEN` permissions those workflows have. A token
without the Workflows permission keeps agents from changing a workflow
(the push is refused and the run blocks), but not from changing the code a
workflow runs. Keep secrets out of workflows those events trigger, or put
them behind an environment with required reviewers, and set the
repository's default `GITHUB_TOKEN` permissions to read-only.

### Other limits

- **One allow-list for every launch.** `execution.env_allowlist` and
  `env_allowlist_extra` apply to every agent and every repository command
  alike: a variable you add for a validation command reaches the agents
  too, and one you add for an agent reaches the code under review. Names
  `gh` and git read a credential from are refused, but a token you keep
  under a name of your own (`MY_GITHUB_PAT`) is not recognised and is
  passed if you list it. Each provider adds its own names: `ANTHROPIC_*`
  and `CLAUDE_*` for Claude Code; `OPENCODE_*`, `OPENAI_*`, `ANTHROPIC_*`,
  `GEMINI_*` and `GOOGLE_*` for OpenCode.
  ([Configuration: agent environment](configuration.md#agent-environment))
- **Repository commands are the PR's code.** `validation.commands`,
  `local.validation_commands` and `merge.verification_commands` come from
  your configuration, but what they run (tests, build scripts) is the
  candidate's own code, with your user, the allow-listed environment and,
  by default, the host network.
- **The committed tree is not filtered.** Only commit messages are checked;
  review and you own what a PR changes.
- **Same-user state.** An agent under `disabled` can change the
  controller's files by absolute path; the controller detects some of it
  (a moved checkout, a commit that is not the worktree's own, a GitHub write
  it did not journal) and blocks, not all of it.
- **Processes that outlive an invocation.** Process containment covers
  the launch's process group and the orphans re-parented to the
  controller; there is no cgroup. A descendant that makes itself a
  subreaper keeps its orphans out of the controller's reach, and anything
  handed to a service manager or another session's process (`systemd-run`,
  `at`, a container runtime, an SSH host) is outside containment
  altogether. The executor does not see either, so it reports neither. An
  unsandboxed agent also outlives a controller killed with SIGKILL. Under
  an enforced sandbox the kernel removes everything left in the launch's
  PID namespace when it ends, a self-made subreaper included, but not what
  the launch started through a service outside the namespace that it can
  still reach.
  ([ADR 0002 §4b](adr/0002-executor-nothing-outlives-the-invocation.md#4b-amendment-processes-that-leave-the-group-132),
  [§4e](adr/0002-executor-nothing-outlives-the-invocation.md#4e-amendment-sandboxed-launches-167-2026-10-12))
- **Shared hosts.** Claude Code receives its prompt on its command line,
  which other local users can read from `/proc` unless the host hides
  other users' processes. Prompts carry redacted project text and paths,
  not your credentials.
- **macOS.** There is no outer sandbox (`required` is refused), process
  containment has no subreaper, so a process that leaves its group may
  outlive the invocation (reported as `orphans_unchecked`), and `gh`'s
  keychain is a same-user resource (a launch's placeholder `GH_TOKEN` keeps
  `gh` from asking it).

## Open security issues

The security audit of
[#257](https://github.com/iceboundrock/AutoForge/issues/257), at commit
`388ecb3`, found these issues in what `main` does today. Each has its own
issue; the severities are the maintainer's
([findings](https://github.com/iceboundrock/AutoForge/issues/257#issuecomment-6347159802),
[#289 and #291](https://github.com/iceboundrock/AutoForge/issues/257#issuecomment-6370644928)).
The Critical and High ones block the Public Alpha release until they are
fixed ([#255](https://github.com/iceboundrock/AutoForge/issues/255)); until
then, apply the mitigation in the last column. A row marked *Fixed on
`main`* describes `main` after its fix, which no release carries yet; its
last column says what still applies.

| Issue | Severity | What happens today | Until it is fixed |
|---|---|---|---|
| [#280](https://github.com/iceboundrock/AutoForge/issues/280) | Critical | *Fixed on `main`.* Under `sandbox.mode: required` or `preferred`, every command, a scheduler's worker and `doctor` refuse a `--config` file an enforced launch could rewrite (in the checkout or a worktree of the repository, in a provider's read-write directory such as a dedicated `CLAUDE_CONFIG_DIR`, reached through a link in one, passing through one before a `..`, or with a second hard link), and a run is pinned to the `sandbox.mode` it was created under: a later command under a weaker one is refused unless `--accept-config-change`, which the run log records | Run every command of a run, and `doctor`, from the same environment: a provider directory given only under another one is not known ([why](#the-outer-sandbox-is-opt-in-and-linux-only)) |
| [#281](https://github.com/iceboundrock/AutoForge/issues/281) | High | A recognised token right after a line break, a tab or a character JSON escapes as `\u...` in a step's result is printed unredacted on the terminal and in a scheduler worker's log | Treat that output and the worker logs as sensitive, and do not share them |
| [#282](https://github.com/iceboundrock/AutoForge/issues/282) | High | Google API keys (`AIza...`) are neither redacted nor refused in published text, and OpenCode launches receive `GEMINI_*` and `GOOGLE_*` | Keep such variables out of the shell you run AutoForge from unless a profile needs them ([why](#redaction-is-a-baseline)) |
| [#283](https://github.com/iceboundrock/AutoForge/issues/283) | High | `merge.delete_branch: true` passes `--delete-branch` to `gh pr merge`, which, per `gh`, deletes the local branch of that name too, running git in the checkout you started AutoForge from; under `disabled`, a git hook an agent planted there runs with the controller's credential within reach | Keep `merge.delete_branch: false`, the default |
| [#284](https://github.com/iceboundrock/AutoForge/issues/284) | High | A recognised token directly after an ANSI escape sequence is not redacted, and OpenCode's saved `stderr.log` carries such sequences | Treat run logs as sensitive: never commit or share them ([how](#keep-autoforges-files-out-of-git)) |
| [#285](https://github.com/iceboundrock/AutoForge/issues/285) | Medium | Other credential shapes are not recognised: AWS access keys, private-key blocks, `gsk_`, `xai-`, `npm_`, `glpat-`, `hf_`, and a key of no known shape under a prefixed name such as `GEMINI_API_KEY` | As for #282 |
| [#286](https://github.com/iceboundrock/AutoForge/issues/286) | Medium | One allow-list serves every launch, and it refuses only the names `gh` and git read a credential from ([why](#other-limits)) | List no variable you would not give to the agents and to the code under review alike |
| [#287](https://github.com/iceboundrock/AutoForge/issues/287) | Medium | In a sequential EPIC run, `UPDATE_EPIC` may pick any open issue of the repository next, on a public repository possibly one a stranger opened | Prefer `autoforge epic run`, which admits only the EPIC's sub-issues |
| [#289](https://github.com/iceboundrock/AutoForge/issues/289) | Medium | *Fixed on `main`.* The merge gate refuses an empty check rollup, and one in which no check succeeded, under every configuration, `safety.verify_check_definition: false` and an empty `safety.required_checks` included; `doctor` names a GitHub Free plan restriction as the plan, never as a token permission, and its required-check row fails while `safety.allow_merge: true` and the requirement cannot be verified (the plan, a token without access, not found, a transient failure, or a hidden bypass list with no word on this token) | A branch rule is still what makes a check *required*: with the gate open, keep both settings at their defaults and a CI check required ([why](#check-runs-on-a-private-repository)) |
| [#291](https://github.com/iceboundrock/AutoForge/issues/291) | Medium | *Fixed on `main`.* With a fine-grained token on a private repository whose pull requests have checks, REVIEW and FIX now run without the check results (the reviewer is told they are unknown), the merge gate blocks with a reason naming the credential and its remedy, and `doctor`'s "pull request check runs readable" row reports it | With the merge gate open, use `gh auth login` or a classic token with `repo` there ([why](#check-runs-on-a-private-repository)) |

Also disclosed: pushed code runs in your CI before any review
([above](#pushed-code-runs-in-your-ci)); AutoForge's own CI pins its GitHub
Actions by tag, not by commit SHA
([#288](https://github.com/iceboundrock/AutoForge/issues/288)), while its
publishing workflow pins every action by commit SHA
([Releasing](releasing.md#publishing-to-pypi)); and the
`autoforge` distribution on PyPI is an unrelated project: AutoForge's is
`autoforge-cli`
([#256](https://github.com/iceboundrock/AutoForge/issues/256)), not yet
published there
([#295](https://github.com/iceboundrock/AutoForge/issues/295)).

## GitHub token permissions

AutoForge uses the `gh` login of the user who runs it (or the `GH_TOKEN`
of the shell you start it from) for every GitHub call, and hands it to git
through `gh auth git-credential` for every fetch and push; agents never see
it. Every call AutoForge makes is about the run's repository: none reads
the user, an organization, a team or a project. The one exception is a
"blocked by" link of an EPIC's issue to an issue in another repository,
which `autoforge epic` reads; a token that cannot read it keeps that issue
blocked. A fine-grained personal access token restricted to the one
repository covers every permission in the table below, but not on a
private repository whose pull requests have checks: there GitHub refuses
it the pull requests' check runs, which the merge gate needs, so a run with
the merge gate open blocks at READY_FOR_MERGE (see
[Check runs on a private repository](#check-runs-on-a-private-repository)).
Use `gh auth login` or a classic token there.

The table is derived from every `gh` call and git push in the code. The
last column says what real fine-grained tokens did on disposable
repositories ([evidence on #257](https://github.com/iceboundrock/AutoForge/issues/257#issuecomment-6357194136),
[private check runs](https://github.com/iceboundrock/AutoForge/issues/257#issuecomment-6369613898)).
"Private" results come from a private repository with the merge gate
closed. "Public" results come from a public repository, where every read
succeeds without any permission, so only the writes and denials there say
something about the token.

| Fine-grained repository permission | Merge gate closed (default) | Merge gate open | What needs it | Real-token result |
|---|---|---|---|---|
| Metadata | Read | Read | every fine-grained token; the repository and its default branch | private: works |
| Contents | Read and write | Read and write | fetching; pushing the issue's branch (fast-forward only, never the default branch); comparing branches; the merge | private: fetch, push, compare work; the merge worked on a public repository only |
| Pull requests | Read and write | Read and write | opening or adopting the PR, the review comment, closing and reopening a replanned PR, the merge | private: works on a pull request without check runs, except the merge, which was not tried there; on one with a check run, reading it with its check results fails (see *(Checks)*); public: squash merge through a ruleset requiring a pull request and `ci` worked |
| Issues | Read and write | Read and write | reading issues, comments and the EPIC's sub-issues and dependencies, including the comments you post on other issues for a fix, which the controller only reads; follow-up issues; the acceptance comment on the run's issue; the EPIC progress comment and roadmap (only after a merge) | private: works |
| Commit statuses | Read | Read | legacy commit statuses in the check results of the PR's head | private: the combined status read works |
| *(Checks)* | *not offered* | *not offered* | the check runs in the check results of the PR's head, which the merge gate, REVIEW's read context and `doctor`'s checks-read row request; no other read of a pull request does | fine-grained tokens have no Checks permission; private: refused, both the REST check-runs read (HTTP 403) and the check run in a pull request's `statusCheckRollup`, which fails the whole `gh pr view`; public: works without any permission |
| Actions | not needed | Read | the merge gate's check that a required check ran the base branch's workflow | private: not tested (denied without it, as expected); public only: works |
| Workflows | not recommended | not recommended | pushing commits that change `.github/workflows/`; without it GitHub refuses such a push and the run blocks | without it, the push is refused on private and public repositories |
| Administration | not needed | optional, Read | only `doctor`'s classic branch-protection row, which is skipped without it | without it: denied on private and public; with it: public only |

### Check runs on a private repository

A fine-grained personal access token cannot read a private repository's
check runs, and AutoForge has no way around that:

- **What GitHub offers.** A fine-grained token has no Checks permission to
  select, and GitHub's list of fine-grained token limitations includes
  calling the Checks API. GitHub's page for listing a commit's check runs
  names fine-grained tokens with a "Checks" read permission, but no such
  permission can be selected.
- **What GitHub did.** On a private repository it refused a fine-grained
  token the REST check-runs read (HTTP 403) and, on a pull request with a
  GitHub Actions check run, the check run inside the pull request's
  `statusCheckRollup` ("Resource not accessible by personal access
  token"), while it returned the pull request, its head commit and the
  rollup's overall state. On a pull request without check runs the same
  read worked. On a public repository both reads succeed for anyone, so a
  public result proves nothing about this.
- **What AutoForge calls.** It never calls the REST Checks API. It reads
  a pull request in one of two ways
  ([#291](https://github.com/iceboundrock/AutoForge/issues/291)): an
  identity read without `statusCheckRollup`, which every phase uses to bind
  and verify the pull request, and a checks read with it, which only the
  merge gate, REVIEW's read context and `doctor` make. `gh` fails the whole
  checks read when GitHub refuses part of it, and AutoForge classifies that
  answer as a refused checks read: never "no checks", never a pull request
  that does not exist, and never retried.

What follows for AutoForge (from the code and its tests; a complete run
was not tried):

- **REVIEW and FIX run.** ANALYZE_EXECUTE opens the pull request, and
  REVIEW and FIX bind and verify it, its HEAD and its base as on any other
  repository. The reviewer's read context says that GitHub refused the
  check results, so they are unknown: not "no check" and not "passed".
  With the merge gate closed the run stops at READY_FOR_MERGE as always;
  whoever merges reads the checks on GitHub.
- **The merge gate blocks.** With the gate open, READY_FOR_MERGE and MERGE
  read the check results, and GitHub's refusal is `BLOCKED` at once, with a
  reason naming the credential and the remedy below. Nothing is merged or
  counted, and no re-check is spent, since it would read the same refusal.
  Change the credential, then `unblock`. A pull request merged by hand, or
  by a `MERGE` that crashed before it recorded the merge, is named as
  merged in that reason, but the controller does not count a merge it
  could not check, so with such a credential the run cannot reach
  `UPDATE_EPIC`.
- **`doctor` detects it.** Its "pull request check runs readable" row makes
  the merge gate's checks read on an open pull request whose head has a
  check run: PASS when it read one, DENIED when GitHub refused it (a FAIL
  with `safety.allow_merge: true`, a WARN otherwise), and UNKNOWN, never
  PASS, when no such pull request exists or GitHub's answer was a plan
  limitation, a not-found or a transient failure.
- **No permission fixes it.** There is no Checks permission to add; adding
  Actions: Read was not tested and is not expected to help.
- **Use another credential there.** `gh auth login` (an OAuth token with
  the `repo` scope) read the same check run; a classic token with `repo` is
  documented to. Either reaches every repository its account can reach; a
  dedicated GitHub account that is a collaborator on that repository only
  narrows it. A GitHub App installation token can be given Checks: read,
  but it expires after an hour and AutoForge cannot renew it during a run.
- **The merge gate fails closed on a missing check,** with the
  defaults. It requires each `safety.required_checks` context (default
  `ci`) to appear exactly once in the rollup, as a GitHub Actions run at
  the reviewed HEAD that ran the base branch's workflow. A check missing
  from the rollup blocks the merge; it is never taken as passed. A read
  that fails conclusively (refused, not found) blocks the run; a transient
  one is retried within `merge.max_verification_attempts`.
- **An empty rollup never merges.** A rollup that was read and holds no
  check at all, for example because no CI ran, is `BLOCKED` under every
  configuration, `safety.verify_check_definition: false` and an empty
  `safety.required_checks` included
  ([#289](https://github.com/iceboundrock/AutoForge/issues/289)). So is a
  rollup in which nothing succeeded (only `NEUTRAL` or `SKIPPED` entries):
  a merge never passes only because no check failed. A refused read is
  never taken for an empty one.
- **The defaults still matter.** With `safety.verify_check_definition:
  false` or an empty `safety.required_checks`, the controller requires
  only that the rollup has a successful check and that none failed: one
  green check that is not the CI you meant is enough. Only a branch rule
  makes a particular check required.

### Private and public repositories

| | Merge gate closed | Merge gate open |
|---|---|---|
| Private repository | A fine-grained token worked at the API level for every call the probe made on a pull request without check runs, except the REST check-runs read. Once the pull request has a check run, GitHub refuses it the pull request's check results: AutoForge's REVIEW and FIX run without them and the run stops at READY_FOR_MERGE as with any closed gate. A whole `autoforge run` with a fine-grained token, `gh auth login` or a classic token is untested. | A fine-grained token cannot merge once the pull request has a check run: the merge gate blocks on the refused check results, so use `gh auth login` or a classic token with `repo`; on GitHub Free the branch cannot require checks |
| Public repository | Not tested on its own; the open-gate test covers the same calls | Tested at the API level: a fine-grained token without Administration merged a pull request through an active ruleset requiring a pull request and `ci`, after the check was green. Reads prove nothing on a public repository, and a whole `autoforge run` is untested. |

### GitHub Free and private repositories

On GitHub Free a private repository can have neither rulesets nor
classic branch protection. GitHub refuses those endpoints to every caller,
the owner included, with "Upgrade to GitHub Pro or make this repository
public to enable this feature." That answer is about the plan, not about
the token, and changing the token does not change it. On such a
repository:

- nothing can require a pull request or the `ci` check on the default
  branch, and Contents: write can push to it directly (AutoForge itself
  never does);
- `doctor`'s required-check row cannot read the branch rules. It says
  that this is the plan, not the token, and that no token permission
  changes it; it is a `FAIL` while `safety.allow_merge: true` and a `SKIP`
  with the gate closed, never an `OK`
  ([#289](https://github.com/iceboundrock/AutoForge/issues/289)).

Keep the merge gate closed there, or use a plan whose private
repositories can require checks. A public repository can, but it brings
the prompt-injection exposure described
[above](#prompt-injection-and-public-repositories-in-particular).

### Verification status

Real fine-grained tokens were run against AutoForge's call shapes on
disposable repositories, without AutoForge itself
([evidence](https://github.com/iceboundrock/AutoForge/issues/257#issuecomment-6357194136)).
The maintainer accepted this verification within the scope below
([decision](https://github.com/iceboundrock/AutoForge/issues/257#issuecomment-6370644928)):
it does not approve fine-grained tokens for private repositories whose
pull requests have checks, and it does not claim that a complete private
run or any other credential type was verified.

- **Verified:**
  - on a private repository with the merge gate closed: the reads and
    writes the test made, covering issues, comments, sub-issues and
    dependencies, branches and pushes, pull requests, the status rollup
    of a pull request without check runs, and commit statuses;
  - on a private repository, `gh auth login` with `repo` (the repository
    owner's account) reading a pull request's check run through
    AutoForge's own `gh pr view` fields;
  - that the token reaches no other private repository;
  - that a workflow push is refused without Workflows;
  - on a public repository with the gate open: the merge through an
    active ruleset without Administration.
- **Failed:** a fine-grained token reading check runs on a private
  repository, through the REST Checks API (HTTP 403) and in a pull
  request's `statusCheckRollup`, which fails the whole `gh pr view`: at the
  audit, every read AutoForge made of a pull request
  ([evidence](https://github.com/iceboundrock/AutoForge/issues/257#issuecomment-6369613898));
  since #291, its checks read only.
- **Inconclusive:** reading a private repository's branch rules, which
  GitHub Free does not offer.
- **Unverified:**
  - the merge gate on a private repository;
  - a complete `autoforge run` with any credential on a private
    repository, or with a fine-grained token;
  - a fine-grained token with Actions: Read, and a pull request with only
    a commit status;
  - classic tokens, GitHub App tokens, and `gh auth login` with an account
    that does not own the repository.

With a classic token or `gh auth login`, that is `repo` for a private
repository or `public_repo` for a public one. AutoForge's own calls need
neither `workflow`, `read:org`, `gist`, `admin:*` nor `delete_repo`; leave
out `workflow` unless agents are meant to change workflows. `autoforge
doctor` checks that `gh` is logged in, not which permissions the login has;
the one permission it tries is the merge gate's checks read, on an open
pull request with a check run.

Contents: write cannot be limited to some branches, so protect the default
branch with a branch rule or ruleset that requires a pull request and the
`ci` check: AutoForge itself refuses to push to it, and `doctor` checks
that the check is required. On GitHub Free that needs a public repository
([above](#github-free-and-private-repositories)).

## Keep AutoForge's files out of git

AutoForge's runtime files live in the repository you run it in, and that
repository's `.gitignore` does not know them:

- `.autoforge/` (the default state directory: `state.json`, run logs with
  every prompt and agent output, and the read context), or whatever
  `--state-dir` or `state_dir` names inside the checkout;
- `autoforge.yaml` and `autoforge.local.*` (your configuration and local
  overrides, if you keep them in the checkout; under `sandbox.mode:
  required` or `preferred` AutoForge refuses a configuration file there).

The run logs and read context are redacted, but only to a baseline: never
commit them. Add them to the repository's `.gitignore`, or, to keep the
change out of the repository, to `.git/info/exclude`:

```bash
printf '%s\n' '.autoforge/' 'autoforge.yaml' 'autoforge.local.*' >> .git/info/exclude
```

AutoForge's other files (the lock, agent worktrees, scheduler namespaces,
sandbox directories) are under `.git/autoforge/`, which git never tracks.

## Recommended settings for a repository you care about

1. **Run it only on issues and pull requests whose authors you trust,** or
   on a repository where you control who can comment. On a public
   repository, consider GitHub's interaction limits while a run is active.
2. **Isolate the agent's user.** Run AutoForge as a dedicated OS user, or
   in a VM or container, holding only the credentials it needs; or use
   `sandbox.mode: required` on a Linux host that can enforce it (check with
   `autoforge doctor`). Keep the `--config` file outside the repository,
   its worktrees and every directory you give a provider read-write, which
   that mode requires, and pass it to every command of a run, from the
   same environment: a weaker `sandbox.mode` is refused
   ([why](#the-outer-sandbox-is-opt-in-and-linux-only)).
3. **Use dedicated, spend-limited model keys,** not the keys you use
   elsewhere, so that a leaked or misused key costs little and can be
   revoked alone.
4. **Use the narrowest GitHub credential that works.** On a public
   repository, a fine-grained token restricted to it, with the permissions
   [above](#github-token-permissions), and without Workflows: write unless
   you need it. On a private repository whose pull requests have checks a
   fine-grained token cannot read the check results, so the merge gate
   blocks on it and the reviewer works without them
   ([why](#check-runs-on-a-private-repository)): use `gh auth login` or a
   classic token with `repo`, preferably for a dedicated account that can
   reach only that repository.
5. **Keep the merge gate closed** (the default) and merge yourself. If you
   open it, keep `safety.protected_merge_paths`, `safety.required_checks`
   and `safety.verify_check_definition` at their defaults, require a CI
   check on the default branch (`doctor` checks that it is required; a
   GitHub Free private repository cannot require one, see
   [above](#github-free-and-private-repositories)),
   and keep `merge.delete_branch: false` (the default): with it, `gh pr
   merge --delete-branch` deletes the local branch of that name as well,
   and runs git for that in the checkout you started AutoForge from.
6. **Keep repository secrets away from branch CI** and make the default
   `GITHUB_TOKEN` read-only ([why](#pushed-code-runs-in-your-ci)).
7. **Keep AutoForge's files out of git** ([above](#keep-autoforges-files-out-of-git)).
8. **Install from this repository,** pinned to a commit you chose
   ([Installation](../README.md#installation)), or from a clone with
   `uv sync --locked`. The distribution is `autoforge-cli`, not yet
   published on PyPI; the `autoforge` distribution there is an unrelated
   project.
9. **Run `autoforge doctor` first,** and read the launch environment, the
   credential preflight and the sandbox rows.
