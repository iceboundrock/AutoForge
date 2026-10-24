# Changelog

Notable changes for operators. A default changes only where an entry says
so.

## Unreleased

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
  an empty rollup or retried.
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
