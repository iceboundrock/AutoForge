# Changelog

Notable changes for operators. A default changes only where an entry says
so.

## Unreleased

### An idle timeout names when the silence began, and a Claude run's summary records where its stream stood (#343)

- `last_activity_at` in `execution.json`, and the time in the error
  (`made no progress for 900s (last activity ...)`), counted what an agent
  wrote while it was being killed: Claude flushes a few records on
  SIGTERM, and Pi answers its `abort`. In all nine idle kills in one
  operator's logs it fell 0.04 to 2 s before `finished_at`, so the error
  named a time one idle timeout after the agent had gone silent. It is now
  the last output before the kill or a loop stop. What follows is drained
  but not counted, and only part of it is kept: every `stderr.log` keeps
  what the CLI writes there, and an OpenCode or text-mode Claude run's
  `stdout.log` its stdout, but a stream-json Claude run's and a Pi run's
  stdout records are reduced into `provider_summary` and never logged, and
  Claude's records written while it is killed are not reduced either.
- A Claude run's `provider_summary` adds `turns` (opened) and
  `open_tool_calls` (calls whose result had not arrived). With `results`
  they suggest whether a killed run was between turns, its CLI kept alive
  by a background task or wakeup the agent had left pending, or inside a
  tool call. They are not a verdict: a turn that ends while a background
  agent runs gets its result late and that agent's own calls count as
  open, a skipped oversize `tool_result` leaves its call open, and at most
  1024 open calls are tracked. Five of the nine kills were between turns,
  three of those waiting for a test suite the agent had started in the
  background. The other four were three OpenCode runs hung after a
  dropped connection and one Pi `bash` call that printed nothing.
  [Reading an idle timeout](docs/usage.md#reading-an-idle-timeout) says
  what helps in each case, how to tell from the heartbeats in
  `progress.log` when the silence began, and that the last line printed
  is not always the last thing the agent did.
- Nothing is killed sooner or later than before, and nothing is retried
  that was not. No default, configuration key or prompt changes.

### Agents are told the block goes in their final reply, and a correction quotes the reply it corrects (#393)

- The controller reads the `CONTROL_RESULT` block only from the end of an
  agent's final reply, but the prompts said "stdout", and three Claude
  runs in one operator's logs finished a phase with a reply that held no
  block, one of them after printing the block from a Bash command. Every
  prompt now says the final reply, and that command output, tool results
  and files are never read for it; the rule that an unfinished phase still
  ends with a `failure` or `blocked` block says it covers a reply written
  while work the agent started is still running.
- A correction was a new session that learned only the parser's error. Its
  prompt now also quotes the end of the refused reply, after the
  diagnosis: redacted, then cut to its last 4,000 characters (a line
  outside the fence says how many were dropped), fenced as untrusted data
  that is neither an instruction nor evidence. The correction still checks
  what it relies on against the worktree, and its result passes every
  check a first launch's does. Its `request.json` records the quote's
  sizes (`metadata.correction_reply`); nothing of it is persisted in state.
- Redaction, everywhere it applies, now recognises a credential's name as
  a quoted key (`"GITHUB_TOKEN": "..."`, `"api_key": "..."`,
  `"Authorization": "Bearer ..."`, as JSON, a Python repr, TOML or YAML
  write it). Before, a value of no known token shape under such a key was
  kept in the clear, and a quoted reply would have passed it to the
  correction. A line that starts a code fence is no longer taken for an
  assignment's value, so a quoted reply that ends in an empty
  `GITHUB_TOKEN=` keeps its closing fence in `prompt.md`. Published text
  that holds a credential under a quoted key is now refused by name, as
  the unquoted form already was.
- Limits: this is a mitigation. It does not address a Claude turn that ends
  while a background task the agent started is still running: such a
  reply is corrected within `execution.max_correction_attempts` like any
  reply without a block, and the controller neither tracks the task nor
  reads the reply for whether it finished. A `resume` after a crash,
  `Ctrl-C` or a spent correction budget launches without the quote, as
  before.
- The prompt version is `v13`. A LOCAL run created under `v12` refuses to
  resume, as for every prompt change; start it again. A REMOTE run resumed
  by this version renders the new templates and keeps its recorded label.
  Nothing changes a default.

### A push GitHub has not yet shown on the PR is waited for, not left to `resume` (#403)

- GitHub moves a PR's head a few seconds after a push to its branch. When
  `FIX` read the PR back right after pushing the fixer's commit and still
  found the reviewed HEAD, `run` stopped with `GitHub has not caught up
  yet` and the run waited in `FIX` until an operator ran `resume`. That
  read, and `ANALYZE_EXECUTE`'s two reads after its push (before adopting
  an existing PR, and the read-back of the published PR), now read the PR
  again within the step, after pauses of 1, 2, 4, 8, 15, 15 and 15 s, and
  go on as soon as GitHub shows the pushed commit. The progress output
  says `waiting up to 60s for GitHub to move it`. A PR still at the old
  head after 60 s stops the step as before (`GitHub has not caught up
  within 60s`), and `resume` reads it again. Each read is held to the same
  rules as before: a head that turns into another commit is still a push
  the controller did not make, and another base or branch still blocks.
  In `FIX`, a PR retargeted, its base rewritten or its branch changed is
  routed at once even while its head still lags, not waited out. What a
  step read before the wait is read again after it: `FIX` checks the
  follow-up issues again before recording the fix, and `ANALYZE_EXECUTE`
  blocks when the default branch changed during the wait. The extra reads
  save nothing and send nothing. Nothing changes a default, and there is
  no new configuration key.

### A Claude run that failed on a model API outage is relaunched, as an OpenCode one is (#395)

- Once Claude Code has spent its own retries of a request to the model
  API, the turn ends in an error, and the step failed with the phase
  unchanged until an operator ran `resume`. Under `stream-json` (the
  default), a turn that ended this way with HTTP status 408 or any 5xx
  (529 is overloaded) is now reported as transient, and a REMOTE run
  relaunches it at most twice per step, after re-reading GitHub, as it has
  relaunched OpenCode's listed OpenAI errors since #164. `error.txt` and
  the step's error say `(transient: claude: API error 529)`, and
  `not relaunched: ...` once the bound is spent. A relaunch is the launch
  `resume` would make: a new agent, told what the worktree holds
  uncommitted (#187). The relaunched CLI goes through its own retries again
  before it gives up, so no delay is added. Not relaunched, as before: any
  other 4xx (a 429 may be a plan's usage limit), an error with no status (a
  refused or dropped connection, the CLI's own request timeout), any
  failure of an `output_format: text` profile, and every failure in LOCAL
  mode. Nothing changes a default.

### A Claude run whose stream has failed is stopped, not left to its ceiling (#394)

- A failed turn (an API error once the CLI's retries are spent, a
  non-`success` result, a malformed line) fails the step whatever the CLI
  does next, but it does not end `claude -p`: a background task the agent
  left pending keeps the CLI working on another turn. The controller
  showed nothing more of that work, only heartbeats with an unchanging
  event count; the CLI's output kept the idle limit away; and the run went
  on until `max_runtime_seconds`, whose timeout then replaced the real
  error in the step's message. A CLI still running 10 s after its stream
  failed is now stopped with everything it started, and the step's error
  and end line name the failure (`...; claude was still running 10s later
  and was stopped`). `execution.json` says `ended_by: provider_failure`
  with exit code -1. So is a CLI that closes its stdout and runs on, after
  the failure or without the turn's result. A CLI that exits by itself
  within the 10 s keeps its own status, as before. The state is unchanged and `resume` relaunches
  the phase, as for any failed launch. Nothing changes a default.

### A `prompt_version` other than the controller's own is refused (#302)

- `autoforge.example.yaml` set `prompt_version: v1`, and the controller
  records that key as the run's prompt version, so a config copied from it
  made every run's state file, run logs and prompt headers say `v1` while
  the controller rendered its current templates (`v12`). The key never
  selected templates. The example no longer sets it, and a value other
  than the controller's built-in label is now a load error naming that
  label. The error quotes the configured value only when it is shaped like
  a label (`v1`); anything else is named by its type, never repeated.
- If your config has `prompt_version: v1`, every command, `doctor`
  included, refuses it until you delete the line. A run created under that
  line keeps `v1` as its recorded version: a REMOTE one resumes and goes on
  saying `v1`, as every REMOTE run keeps its creation-time label across an
  upgrade; a LOCAL one refuses to resume once the line is gone, since its
  contract recorded `v1`, so start it again. Nothing changes a default.

### Claude Code's Bash tool keeps the launch's `PATH` in a login shell (#347)

- When Claude Code has no shell snapshot (writing one failed), it runs
  each Bash tool command in a login shell, which reads `/etc/profile`.
  Debian's sets `PATH` outright, so such a command ran with Debian's
  default `PATH` (plus what your login file adds) and missed any other
  directory the launch had on it. The launch's `CLAUDE_ENV_FILE` now sets
  the `PATH` Claude Code starts with back in a login shell, inside the
  sandbox as the sandbox rewrites it. Commands that run after a snapshot
  are unchanged. Nothing changes a default.

### A provider path under `~/.cache` is refused naming `~/.cache` (#301)

- Under `required`, a launch that gets the persistent cache shows it at
  `~/.cache`, so it refuses a provider CLI or a provider's directory under
  your `~/.cache` (C6), as before. The refusal named only the cache's
  directory in the repository (`.git/autoforge/sandbox/<n>/cache`), which
  does not contain the path refused; it now also names `~/.cache`, where
  that directory appears. Nothing changes a default.

## 0.1.0a0 (2026-10-28)

`autoforge-cli` `0.1.0a0` is #295's registration prerelease: a pre-Alpha
snapshot, not validated for unattended production use, and not the Public
Alpha (#255).

### A request an agent CLI refused is named (#258)

- Claude Code and OpenCode never put an agent's permission request or
  question to a person in an unattended run: the CLI refuses it and tells
  the agent to go on without it, and the run usually exits 0 (Claude Code
  2.1.312 and OpenCode 2.1.4, run against a loopback stand-in of the
  model API). Such a run's outcome is unchanged, and its `execution.json`
  now counts the refused requests and names the first
  (`provider_summary.refused_requests`, `refused_request`).
- An OpenCode exit 1 with no error of its own after a refused request (how
  2.0.26 was read to end such a run) now fails the step as
  `the agent asked for permission (read: .env), which an unattended run
  cannot give`, or `an answer to a question`, with what to do, instead of
  a bare `exited 1`. It is never corrected or relaunched; the phase is
  unchanged. Any other failed run keeps its reason and relaunch behaviour
  and adds the refusal to its text. Nothing changes a default
  ([An agent asked for permission or input](docs/usage.md#an-agent-asked-for-permission-or-input)).

### `gh` runs outside your checkout; `merge.delete_branch` deletes only the remote branch (#283)

- Every `gh` the controller starts, `doctor`'s included, runs in an empty
  directory of its own with `GIT_DIR` naming it, so no git `gh` runs can
  reach the checkout you started AutoForge from or any other repository.
  Before, `gh` ran in that checkout, and every `gh pr merge` asked git
  there about the PR's head branch.
- `merge.delete_branch: true` no longer passes `--delete-branch` to
  `gh pr merge`, which deleted the local branch of that name too, checking
  out and pulling another branch first when the checkout had it. After a
  verified merge the controller deletes the PR's head branch on GitHub
  only (`DELETE repos/<owner>/<repo>/git/refs/heads/<branch>`), at most
  once, and never a fork's branch, the base, the default branch, or a
  branch pushed to after the merge. The outcome, read back from GitHub, is
  a note in the step's message, and never blocks the run. The default,
  `false`, deletes nothing
  ([GitHub safety](docs/agent-guides/github-safety.md#deleting-the-merged-head-branch)).
- A run checks the repository it belongs to against the checkout's
  `origin` remote, read with the controller's hardened git and named as
  GitHub names it now (a renamed repository still matches). Before, `gh`
  chose one of the checkout's remotes by its own rules, which can prefer
  `upstream` or `github` over `origin`, so a checkout whose `origin` is a
  fork of the run's repository is now refused.
  So is one whose `origin` does not name a github.com repository in a form
  the `epic` commands and `doctor` already require (`https://github.com/`,
  `ssh://git@github.com/` or `git@github.com:`; an SSH host alias such as
  `git@github-work:` is not one), at the first step, naming the remote.
- A relative `github.command` (`./bin/gh`) is resolved against the
  directory AutoForge starts in, as before; a bare name is still looked up
  on `PATH`, and a relative `PATH` entry (`bin`, `.`, an empty one) still
  means a directory under the one AutoForge starts in, for `gh` and for
  the programs `gh` runs.

### Google API keys are redacted and refused (#282)

- A Google API key (`AIza` and 35 characters of `[0-9A-Za-z_-]`) is
  recognised by its shape, under any name or none: the `GEMINI_*` and
  `GOOGLE_*` variables an OpenCode launch inherits, a URL's `?key=`, JSON,
  and directly after an escape sequence. It is redacted in every run log
  (`stdout.log` and `stderr.log` included), in state and in the CLI's
  output, and published agent text or a commit message carrying one is
  refused under the new class `google-api-key`. Before, an agent that
  printed its environment saved the key in the clear, and agent text
  carrying one could be published under your identity
  ([Secrets and logging](docs/agent-guides/secrets-and-logging.md#google-api-keys-282)).
- Only the whole token is a key: a run of the key's characters longer or
  shorter than 39, a wrong prefix (the shape is case-sensitive) and a shape
  inside a longer identifier are left. OpenCode launches still receive
  `GEMINI_*` and `GOOGLE_*`, and a value of no known shape under such a
  name is still not recognised (#285).

### A step's result is redacted before it is printed as JSON (#281)

- The `result=` line `run`, `step`, `resume` and a scheduler worker print
  for a step redacts each string of the agent's result before it
  serializes the result, rather than the JSON text after: JSON writes a
  quote as `\"`, and a quoted assignment in a result (`GH_TOKEN="..."`,
  `password: "..."`, `refresh_token="..."`) kept its value in the clear on
  the terminal and in the worker's log. The replan escalation `status` and
  a dry run print is redacted the same way, and every note of a dry run's
  plan is redacted now, as its variables were.
- Text that is already escaped, such as the JSON an agent prints to the
  `stdout.log` of its run log, has the quoted value of such an assignment
  redacted behind an escaped quote too (`\"`, `\'`, JSON inside a JSON
  string up to three levels deep), in every run log, in state and in the
  CLI's output; published agent text carrying one is refused. The escaped
  quotes are kept, so redacted JSON text still parses
  ([Secrets and logging](docs/agent-guides/secrets-and-logging.md#escaped-text-redact-the-structure-first-281)).

### Redaction after an escape sequence (#284)

- A recognised credential directly after an ANSI escape sequence is
  redacted: OpenCode writes `ESC[0m` right before an error's text on
  stderr, the patterns started at a word boundary and there is none after
  its `m`, so a token there reached `stderr.log` in the clear. The same holds after a JSON, C or shell
  escape written out (`\n`, `\t`, `\u00e9`, `\u001b[0m`, `\033[0m`,
  `\e[0m`, caret notation's `^[[0m`), after a non-ASCII letter, and after
  any other character that cannot be part of the token, in every run log,
  in state and in the CLI's output; published agent text carrying one is
  refused. The escape itself is kept. A backslash before an escape letter
  counts as an escape wherever it stands, so a token after a path
  separator such as `C:\Users\nghp_...` is redacted too
  ([Secrets and logging](docs/agent-guides/secrets-and-logging.md#where-a-recognised-credential-starts-284)).
- Unchanged: a shape inside a longer identifier (`xghp_...`,
  `risk-assessment`) is still no credential, and a known name behind a
  prefix (`GEMINI_API_KEY=`) is still not recognised (#285).

### A PR, issue or comment whose text `gh` rewrites is no longer a conflict (#327)

- `gh` prints control characters, and the text of their JSON escapes (a
  body explaining terminal output that holds `\u001b`), as caret notation,
  so the read-back of a PR, issue or comment the controller had just
  created byte for byte could show another body, and the run blocked on its
  own object. The read-back now reads that one object's exact text from
  `https://api.github.com/` (a `GET` with the token `gh auth token`
  prints, no redirect, bounded in size and time) and compares it byte for
  byte; every other identity condition is still checked. A payload holding
  a `^` is read this way even when `gh`'s print matches, since an object
  holding the control character it names prints the same. A failure of that
  read never leads to a second write. A run already blocked this way
  recovers with `unblock`
  ([GitHub safety](docs/agent-guides/github-safety.md#ghs-sanitizer-and-the-exact-read-327)).

### The configuration file and a run's sandbox mode (#280)

- Under `sandbox.mode: required` or `preferred`, every command, a
  scheduler's worker and `autoforge doctor` refuse a `--config` file that
  an enforced launch could rewrite: one inside the checkout or inside any
  worktree registered to the repository (the agent worktrees included),
  one inside a directory a provider is given read-write (a dedicated
  `CLAUDE_CONFIG_DIR`, `PI_CODING_AGENT_DIR` or OpenCode database
  directory, of every provider AutoForge knows under the command's
  environment, whether or not the file's profiles name it), one reached
  through a symbolic link in such a directory, one whose path passes
  through one before a `..` (the path is followed as the command opens
  it, a `..` after a link included, never tidied first), and one with a
  second hard link. The command exits 2 with nothing run,
  launched or written, naming the file and the fix; `doctor`'s config row
  fails and runs none of the commands the file names. **Behaviour
  change:** under those modes an `autoforge.yaml` kept in the checkout is
  refused by every command, `status` included. Move it outside, for
  example to `~/.config/autoforge/`, or set `sandbox.mode: disabled`. A
  provider directory given only under another environment than the
  command's is not known to it: run a run's commands from one environment
  ([Security boundaries](docs/security.md#the-outer-sandbox-is-opt-in-and-linux-only)).
- Every run, LOCAL and REMOTE, is pinned to the `sandbox.mode` it was
  created under by `run`, `local run` or a scheduler's worker. `step`,
  `resume` and `unblock` refuse a weaker mode with exit 2 before any phase
  executes, writing nothing; that includes a command run without the
  run's `--config`, which falls back to the built-in `disabled`. Weaker is
  what a launch gets: `disabled`, then LOCAL's `preferred`, then
  `required`; in a REMOTE run `preferred` is `required`. Repeated with
  `--accept-config-change`, the command writes the change to the run log
  first (a `<PHASE>-config-change` record naming the pin it replaces),
  pins the run to the weaker mode and continues. A stronger mode is pinned
  without asking. `status` shows the pin and whether this configuration
  keeps it. **Behaviour change:** before, `sandbox.mode` could change
  between the commands of a run without notice
  ([Configuration: outer sandbox](docs/configuration.md#outer-sandbox)).
- A run whose state file predates the pin is not checked once: its first
  `step`, `resume` or `unblock` pins the mode it runs under.
- The state file is protocol 12: an older AutoForge refuses it, and this
  one refuses a file labelled older that carries `sandbox_pin`, even
  empty, and a protocol-12 or unlabelled file without it.

### PyPI publishing through Trusted Publishing (#295)

- New workflow `pypi-publish` (`.github/workflows/pypi-publish.yml`), run
  only for a version tag `v*`: a `build` job without OIDC builds the sdist
  and the wheel once with the dist check CI runs, after checking that the
  tag is `v` and the package's version, and records their SHA-256; a
  `publish` job in the protected `pypi` environment, the only one with
  `id-token: write`, re-verifies exactly those files and uploads them with
  attestations. Every action is pinned to a commit SHA; no PyPI token or
  repository secret exists. An upload needs the maintainer's one-time
  setup, a version tag the maintainer pushed and the maintainer's approval
  of that run's deployment.
- The release build's tools are pinned by content: uv by version and the
  SHA-256 of its archive, the same pin as CI's `dist` job, and the build
  backend by `build-constraints.txt`, whose setuptools `uv build` installs
  only with a listed SHA-256 (`make dist-check` and the `dist` job too).
  The publish action's container image is pulled by a registry tag, which
  a commit pin does not fix; [Releasing](docs/releasing.md#the-workflow)
  records this residual.
- Between releases `main` carries a development version `X.Y.Z[aN].devM`
  (after `0.1.0a0`, `0.1.0a1.dev0`); a release build refuses one.
- [Releasing](docs/releasing.md#publishing-to-pypi) documents that setup
  (the PyPI pending trusted publisher, the `pypi` environment, the tag
  ruleset), rehearsals, troubleshooting and the checks after an upload,
  and the security preflight that keeps a public upload waiting while a
  Critical or High security issue is open. For this version that is #280
  to #284, fixed above; the preflight checks that the tagged commit
  carries each fix and that each issue is closed.

### An empty check rollup never merges; GitHub Free is a plan limit (#289)

- The merge gate refuses a pull request whose status rollup holds no check
  at all, or in which no check succeeded (only `NEUTRAL` or `SKIPPED`
  entries), under every configuration. Before, `safety.verify_check_definition:
  false` or `safety.required_checks: []` let such a PR through unless a
  branch rule required a check. A pull request with a successful check and
  none failing merges as before, and a refused checks read (#291) is never
  taken for an empty rollup.
- GitHub's plan-entitlement answer ("Upgrade to GitHub Pro or make this
  repository public to enable this feature.", GitHub Free's private
  repositories) is a typed error, never an access denial. `doctor`'s
  required-check row names it as the plan and never sends you to the
  token's permissions.
- That row is now a `FAIL` when `safety.allow_merge: true` and the branch
  rules cannot be verified (the plan, a token without access, not found, a
  transient failure, or a ruleset whose bypass list GitHub withholds
  without saying whether this token may bypass it), and a `SKIP` that says
  why with the gate closed. **Behaviour change:** a configuration with the
  merge gate open on a repository whose rules doctor cannot verify now
  fails `doctor`.

### Check results a credential may not read (#291)

- AutoForge reads a pull request in one of two ways: an identity read
  (state, HEAD, base, branch, body, links) that no longer requests the
  check results, and a checks read that does. Only the merge gate and
  `REVIEW`'s read context make the checks read, so a credential GitHub
  refuses a private repository's check runs (a fine-grained personal
  access token, which cannot be given Checks: read) no longer stops
  `REVIEW` and `FIX` with an unclassified error that every `resume`
  repeated. The reviewer's `checks.md` says the check results are unknown,
  not absent.
- `READY_FOR_MERGE` and `MERGE` block at once on a refused checks read, with
  a reason that names the credential and the remedy: `gh auth login`, or a
  classic token with the `repo` scope (a GitHub App installation token
  needs Checks: read). Nothing is merged, and the refusal is never read as
  an empty rollup or retried. A PR that is already merged (a crash during
  `MERGE`, or a merge by hand) is named as merged in that reason, but its
  merge is not reconciled or counted without the checks read, so with
  such a credential the run cannot go on to `UPDATE_EPIC`.
- GitHub's refusal (`Resource not accessible by personal access token`, or
  `by integration`, on a `statusCheckRollup` path) is a typed, conclusive
  error. A 404, a transient failure and bad credentials are classified as
  before, and a PR whose read was refused or failed is no longer reported
  as one that does not exist.
- `autoforge doctor` has a "pull request check runs readable" row: it makes
  the merge gate's checks read on an open pull request with a check run.
  PASS, DENIED (a FAIL with `safety.allow_merge: true`, a WARN otherwise)
  or UNKNOWN (no such pull request, a plan limit, a not-found or a
  transient failure; never a PASS).

### License, packaging and installation (#256)

- AutoForge is licensed under the MIT License ([`LICENSE`](LICENSE)),
  declared as PEP 639 metadata (`License-Expression: MIT`).
- The distribution is renamed **`autoforge-cli`**; the import package and
  the `autoforge` command keep their names. The `autoforge` distribution on
  PyPI is an unrelated project. In a development clone, `make sync` (or
  `uv sync`) replaces the old editable install.
- The wheel now ships every prompt template. Before, a wheel built from
  this repository had none of them, so an installed `autoforge` could not
  render a prompt; only the editable install of a clone worked.
- New CI job `dist` (`make dist-check`, on Python 3.11 and 3.12, part of
  the required `ci` check): builds the sdist and a wheel from it, checks
  their files and metadata, installs the wheel outside the checkout, runs
  `autoforge --version` and `--help`, renders every template from the
  installed package and runs a LOCAL and a REMOTE dry run (the REMOTE one
  calls no `gh`).
- [Compatibility](docs/compatibility.md): what AutoForge runs on, each
  entry labelled supported, untested or unsupported with its evidence. The
  first prerelease claims Linux x86_64 with CPython 3.11 and 3.12 only;
  Windows is unsupported.
- The [README](README.md#installation) documents installing the command
  with `uv tool` from PyPI once `autoforge-cli` is published (#295), from a
  git commit or from a built wheel;
  [Releasing](docs/releasing.md) documents the version and tag scheme and
  the release procedure.

### Security policy and security boundaries (#257)

- [`SECURITY.md`](SECURITY.md): supported versions (the latest commit of
  `main`), private reporting through GitHub's private vulnerability
  reporting, what to expect, and what is in and out of scope.
- [Security boundaries](docs/security.md), for operators: what AutoForge
  protects and what it does not (agents run as you with network access
  and your model credentials; the same-user residual under the default
  `sandbox.mode: disabled`; prompt injection on public repositories; the
  opt-in, Linux-only sandbox; the redaction baseline and the credential
  shapes it misses; what goes to model vendors; pushed code running in
  your CI), the open security issues the audit found, each with its issue
  and a mitigation until it is fixed, the GitHub token permissions it
  needs, the runtime files to keep out of git, and recommended settings.
  No behaviour changes.
- The token permissions record what real fine-grained tokens did on
  disposable repositories. On a private repository whose pull requests
  have checks GitHub refuses a fine-grained token the check runs, so it
  cannot be used to merge there (#291, above). Use `gh auth login` or a
  classic token with `repo` there.
- The documentation no longer says that no credential variable can be
  allow-listed: the names `gh` and git read a credential from are refused,
  but a token under a name of your own is not recognised and reaches every
  launch if you list it. `autoforge.yaml` and `.autoforge/` are git-ignored
  only in AutoForge's own repository; add them, and `autoforge.local.*`,
  to the `.gitignore` or `.git/info/exclude` of the repository you run it
  on. Process containment is described with the escapes ADR 0002
  records (a self-made subreaper's orphans, anything handed to
  `systemd-run`, `at`, a container runtime or an SSH host), and the
  documentation index and configuration reference no longer call #169's
  `preferred`, pre-launch gate, `sandbox.read_only_paths` and `doctor`
  rows unimplemented.

### Operator actions on related issues (#243)

- A `FIX` agent may resolve a finding as `operator_acceptance`: one to four
  comments (four at most per result) on *other* issues of the run's
  repository, those the run's issue body references or the EPIC, as `FIX`
  read them when it started. The controller never posts them: the run
  blocks before anything else of the plan is sent, `status` lists each
  `X-…` action with its target and payload hash, `status --action X-…`
  prints the exact bytes, and the operator posts each, binds an existing
  comment (`unblock --bind-evidence X-… <comment URL>`) or withdraws them
  (`unblock --decline-action X-…`, now repeatable). `FIX` verifies every
  comment on GitHub before it publishes the rest of the plan, one comment
  for one action only, and the next review reads the verified comments
  ([BLOCKED on operator actions](docs/usage.md#blocked-on-operator-actions)).
  Post through the pipe `status` prints: a capture on a scratch issue
  confirmed that it comes back byte for byte, while a paste into the web
  UI did not and is a conflict to repair.
- `safety.acceptance_actions: off` refuses that resolution too; `approve`
  (the default) and `auto` offer it. Acceptance and operator declines share
  the bound of 16 per review round.
- A plain `unblock` of a run waiting on operator actions is refused, with
  nothing written, until one of them is on GitHub or a recorded conflict is
  repaired; a target or comment listing it cannot read refuses it as well,
  naming the target. `--decline-action` reads every pending action's
  target first, as `--bind-evidence` does, and refuses, naming it, on one
  that cannot be read or was transferred; a target that is not found can
  still be declined. `run` refuses to replace a `BLOCKED` or `FAILED` run
  that holds operator actions without `--force`.
- When `FIX` cannot verify an action, because a bound comment was edited or
  deleted or a target cannot be read, `status` and `status --json` report
  it as `verification_failed` with the reason until the next check or a new
  binding, not as merely pending or bound.
- The state file is protocol 11 (prompt version 12): an older AutoForge
  refuses it, and this one refuses a file labelled older that carries
  operator state.

### Pi: DeepSeek and the ChatGPT subscription (EPIC #127)

- `provider: pi` runs any phase through [Pi](docs/pi.md) 1.0.0 or newer, an
  optional multi-model provider: on a DeepSeek API key, its first target,
  or on the ChatGPT sign-in you made in Pi yourself, with no OpenAI
  Platform API key in AutoForge's environment. The RPC adapter, read-only
  `doctor` checks and a per-launch auth check, and the unattended loading
  policy ([ADR 0003](docs/adr/0003-pi-agent-provider.md),
  [Pi policy](docs/pi-policy.md); #129 to #133). It is opt-in per profile:
  every default profile still runs Claude Code or OpenCode.
- The [Pi operator guide](docs/pi.md) (#135): installing Pi and giving it a
  credential yourself, a DeepSeek profile (`deepseek/deepseek-flash`,
  `require_oauth: false`, the key in Pi's store or forwarded with the
  global `execution.env_allowlist_extra`, a dedicated `PI_CODING_AGENT_DIR`
  under the sandbox) and a ChatGPT one to copy, model and thinking-level
  choices, subscription versus API key, each `doctor` Pi row with its
  remedy, security, LOCAL mode, limitations and unattended use.
- `doctor`'s Pi auth row on an API key now reads
  `authType api_key; authenticated ...`: with a `:` there, the redaction
  `doctor` prints through masked the word `authenticated`.
- `doctor` gains a row for Pi profiles, `pi's detached tool processes are
  contained`: OK where the controller can make itself a child subreaper
  (Linux), a warning elsewhere, including a Linux host whose security
  policy denies it. There a process a Pi tool command backgrounds can
  outlive the launch and Pi is attended only (#135). A warning, never a
  failure.
- An opt-in live smoke test, `tests/test_live_pi.py` (#135): one LOCAL
  REVIEW through your own Pi and credential, on the route you name with
  `AUTOFORGE_LIVE_PI_AUTH`: `api_key` (DeepSeek by default) or `oauth` (the
  ChatGPT sign-in, optional), then a credential scan of everything
  AutoForge wrote and checks for left-behind sessions and processes; its
  report names the AutoForge commit, the route and the `authType`. It is
  marked `live` and deselected by default, so `make check` and CI do not
  run it; it needs `-m live` and `AUTOFORGE_LIVE_PI=1`
  ([Development](docs/development.md#the-live-pi-smoke-test)).
