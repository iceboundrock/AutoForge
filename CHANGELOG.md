# Changelog

Notable changes for operators. A default changes only where an entry says
so.

## 0.1.0a0 (unreleased)

The version is `0.1.0a0`, #295's registration prerelease of
`autoforge-cli`: a pre-Alpha snapshot, not validated for unattended
production use, and not the Public Alpha (#255). The heading says
`unreleased` until the release-prep commit the release's tag names gives it
its date ([Releasing](docs/releasing.md#the-changelog)).

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
  and the security preflight that keeps a public upload waiting while
  #280 to #284 are open.

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
  with `uv tool` from PyPI, where `autoforge-cli` is published as
  prereleases (#295), from a git commit or from a built wheel;
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
  have checks a fine-grained token cannot be used: GitHub refuses it the
  check runs, the pull request read fails and a run stops at `REVIEW`
  (#291). Use `gh auth login` or a classic token with `repo` there.
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
