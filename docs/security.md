# Security boundaries

This page is for operators. It says what AutoForge protects, what it does
not, which GitHub token permissions it needs, which of its files to keep out
of git, and how to try it on a repository you care about. Each section
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
  lock and other worktrees are absent.
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

In LOCAL mode the repository root is writable to every enforced launch,
and AutoForge reads its configuration file again at every command. Keep
the `--config` file outside the repository root when you rely on the
sandbox: a file inside it can be rewritten by a sandboxed agent, and the
next command would run on what it says, `sandbox.mode` included.

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

## GitHub token permissions

AutoForge uses the `gh` login of the user who runs it (or the `GH_TOKEN`
of the shell you start it from) for every GitHub call, and hands it to git
through `gh auth git-credential` for every fetch and push; agents never see
it. Every call AutoForge makes is about the run's repository: none reads
the user, an organization, a team or a project. The one exception is a
"blocked by" link of an EPIC's issue to an issue in another repository,
which `autoforge epic` reads; a token that cannot read it keeps that issue
blocked. So a fine-grained personal access token restricted to the one
repository is expected to be enough.

The table is derived from every `gh` call and git push in the code;
whether a real fine-grained token with exactly these permissions runs a
whole cycle on a scratch repository is still to be verified.

| Fine-grained repository permission | Merge gate closed (default) | Merge gate open | What needs it |
|---|---|---|---|
| Metadata | Read | Read | every fine-grained token; the repository and its default branch |
| Contents | Read and write | Read and write | fetching; pushing the issue's branch (fast-forward only, never the default branch); comparing branches; the merge |
| Pull requests | Read and write | Read and write | opening or adopting the PR, the review comment, closing and reopening a replanned PR, the merge |
| Issues | Read and write | Read and write | reading issues, comments and the EPIC's sub-issues and dependencies, including the comments you post on other issues for a fix, which the controller only reads; follow-up issues; the acceptance comment on the run's issue; the EPIC progress comment and roadmap (only after a merge) |
| Checks, Commit statuses | Read | Read | the check results of the PR's head |
| Actions | not needed | Read | the merge gate's check that a required check ran the base branch's workflow |
| Workflows | not recommended | not recommended | pushing commits that change `.github/workflows/`; without it GitHub refuses such a push and the run blocks |
| Administration | not needed | optional, Read | only `doctor`'s classic branch-protection row, which is skipped without it |

With a classic token or `gh auth login`, that is `repo` for a private
repository or `public_repo` for a public one. AutoForge's own calls need
neither `workflow`, `read:org`, `gist`, `admin:*` nor `delete_repo`; leave
out `workflow` unless agents are meant to change workflows. `autoforge
doctor` checks that `gh` is logged in, not which permissions the login has.

Contents: write cannot be limited to some branches, so protect the default
branch with a branch rule or ruleset that requires a pull request and the
`ci` check: AutoForge itself refuses to push to it, and `doctor` checks
that the check is required.

## Keep AutoForge's files out of git

AutoForge's runtime files live in the repository you run it in, and that
repository's `.gitignore` does not know them:

- `.autoforge/` (the default state directory: `state.json`, run logs with
  every prompt and agent output, and the read context), or whatever
  `--state-dir` or `state_dir` names inside the checkout;
- `autoforge.yaml` and `autoforge.local.*` (your configuration and local
  overrides, if you keep them in the checkout).

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
   `autoforge doctor`). In LOCAL mode, keep the `--config` file outside the
   repository root ([why](#the-outer-sandbox-is-opt-in-and-linux-only)).
3. **Use dedicated, spend-limited model keys,** not the keys you use
   elsewhere, so that a leaked or misused key costs little and can be
   revoked alone.
4. **Use a fine-grained GitHub token** restricted to the repositories you
   run AutoForge on, with the permissions [above](#github-token-permissions),
   and without Workflows: write unless you need it.
5. **Keep the merge gate closed** (the default) and merge yourself. If you
   open it, keep `safety.protected_merge_paths` at its default, require a
   CI check on the default branch (`doctor` checks that it is required),
   and keep `merge.delete_branch: false` (the default): with it, `gh pr
   merge --delete-branch` deletes the local branch of that name as well,
   and runs git for that in the checkout you started AutoForge from.
6. **Keep repository secrets away from branch CI** and make the default
   `GITHUB_TOKEN` read-only ([why](#pushed-code-runs-in-your-ci)).
7. **Keep AutoForge's files out of git** ([above](#keep-autoforges-files-out-of-git)).
8. **Install from a checkout** of this repository with `uv sync --locked`.
   AutoForge is not published on PyPI; the `autoforge` distribution there
   is an unrelated project.
9. **Run `autoforge doctor` first,** and read the launch environment, the
   credential preflight and the sandbox rows.
