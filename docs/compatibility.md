# Compatibility

What AutoForge runs on, what it needs from the host and the tools it
drives, and the evidence for each claim. Each cell carries one of three
labels:

- **supported**: a CI job tests it on every commit of `main`, or a recorded
  run proved it; the cell cites which;
- **untested**: nothing proves it either way; it may work;
- **unsupported**: known not to work, with the reason.

The first prerelease claims Linux x86_64 with CPython 3.11 and 3.12 only
([#256 D3](https://github.com/iceboundrock/AutoForge/issues/256#issuecomment-6375246718)).
macOS, Linux ARM64 and Python 3.13 and later are later, separately tracked
work, and stay untested until CI or a recorded run proves them.

The CI jobs cited below are those of
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml), all on GitHub's
`ubuntu-latest` (x86_64) runner. They are required on `main` through the
aggregate `ci` check ([Development](development.md#continuous-integration)).
A release is supported only once its own artifacts pass the same checks
([Releasing](releasing.md)).

This page was last checked against the commit that added it (#256), on
2026-10-23. The EPIC's release-candidate step
([#255](https://github.com/iceboundrock/AutoForge/issues/255), Stage 5)
re-checks every cell against the release candidate's commit and the
then-current releases of each tool.

## Operating system and architecture

| OS / architecture | Status | Evidence or reason |
|---|---|---|
| Linux x86_64 | **supported** | `test (py3.11)`, `test (py3.12)`: the suite, with real git, local processes, file locks and process containment. `dist (py3.11)`, `dist (py3.12)`: the built wheel installed outside the checkout and run. Both on `ubuntu-latest`. |
| Linux ARM64 (aarch64) and other architectures | untested | No CI job or recorded run. AutoForge is pure Python with no architecture-specific code, but nothing has proved it on such a host. |
| macOS (Apple Silicon or Intel) | untested; not supported by the first prerelease | Never run in CI. The code has fallbacks (no child subreaper, so a process that leaves its group may outlive the invocation and is reported as `orphans_unchecked`; an `O_TMPFILE` fallback; a darwin `statfs` branch), but there is no outer sandbox (`sandbox.mode: required` is refused) and process containment is weaker than on Linux ([ADR 0002](adr/0002-executor-nothing-outlives-the-invocation.md), [Security boundaries](security.md#other-limits)). No parity with Linux is claimed. |
| Windows | **unsupported** | `src/autoforge/locking.py` imports `fcntl` unconditionally (POSIX `flock`), so AutoForge does not start, and the executor's process containment is POSIX-only. |
| WSL2 | untested | Not measured ([ADR 0005 §4](adr/0005-outer-sandbox-and-host-resource-capability-model.md#4-platform-behaviour)). |

AutoForge itself needs no minimum Linux kernel that is known; the outer
sandbox does ([below](#outer-sandbox)). The runner's kernel is recorded in
the `sandbox` job's summary on every run.

## Python

| Interpreter | Status | Evidence or reason |
|---|---|---|
| CPython 3.11 | **supported** | `test (py3.11)`, `dist (py3.11)` |
| CPython 3.12 | **supported** | `test (py3.12)`, `dist (py3.12)` |
| CPython 3.13, 3.14 and later | untested | Not in the CI matrix. |
| PyPy and other implementations | untested | Not in the CI matrix. |
| CPython 3.10 and older | **unsupported** | Below `requires-python`; the code uses 3.11's standard library (`tomllib`, among others). |

The supported interpreters are the CI matrix: the Makefile's
`CI_PYTHON_VERSIONS`, which `tests/test_ci_workflow.py` keeps equal to the
workflow's `test` and `dist` matrices, and `tests/test_packaging.py` to the
package's `Programming Language :: Python :: 3.X` classifiers.

`requires-python = ">=3.11"` in `pyproject.toml` is installer metadata,
not a support claim: it is the oldest interpreter the code can run on.
It has no upper bound on purpose:

- an upper bound below 3.15 would refuse the repository's own development
  pin (`.python-version` is 3.14, which `uv sync` and `make check` use), and
  one at 3.15 or above would still admit 3.13 and 3.14, which are untested,
  so it would claim nothing;
- installers treat an upper bound on `requires-python` as a reason to look
  for an older release that lacks it rather than as "unsupported", so it
  does not stop an install on a newer interpreter either.

The documented install commands therefore pin a tested interpreter
(`uv tool install --python 3.12 ...`, [README](../README.md#installation)):
without `--python`, `uv` may choose 3.13 or 3.14. The development pin is a
contributor convenience: a green `make check` on 3.14 proves the suite
passed on that interpreter on one machine, and claims nothing about 3.14.
`make check-matrix` runs the suite on each CI interpreter, and
`make dist-check` builds and installs on a CI interpreter (3.12 unless
`DIST_PYTHON` says otherwise), never on `.python-version`'s.

## git

| Version | Status | Evidence or reason |
|---|---|---|
| the version `ubuntu-latest` ships | **supported** | `test (py3.11)`, `test (py3.12)` run real git (worktrees, bare origins, fetches and pushes over `file://`); `dist (py3.11)`, `dist (py3.12)` print the version they used. |
| 2.55.0, 2.56.0 | **supported** | Recorded runs on the maintainer's host (Fedora 44): the measurements of [ADR 0004](adr/0004-authority-boundary-and-typed-external-effects.md), [ADR 0005](adr/0005-outer-sandbox-and-host-resource-capability-model.md) and [ADR 0006](adr/0006-candidate-validation-evidence-and-review-certificates.md), and `make check`. |
| any other | untested | No minimum is enforced or known: `doctor` checks only that `git --version` runs (`git available`). |

## GitHub CLI (`gh`)

| Version | Status | Evidence or reason |
|---|---|---|
| 2.100.0, 2.102.1 | **supported** | Recorded runs: 2.100.0 (the flag syntax of `autoforge.example.yaml`) and 2.102.1 (the operator-comment byte-fidelity capture of 2026-10-21, [Development](development.md#the-operator-comment-byte-fidelity-capture)). |
| any other version from 2.48.0 | untested | 2.48.0 is the floor `doctor` enforces (`gh >= 2.48.0 is required`): the client lists branch rules and a PR's changed files with `gh api --paginate --slurp`, which older releases reject. Nothing has run these versions: the test suite runs no real `gh`, and an in-memory fake, or a fake `gh` executable, stands in. |
| older than 2.48.0 | **unsupported** | Refused by `doctor`. |

`gh` must be authenticated (`gh auth login`, or a `GH_TOKEN` in the shell
you start AutoForge from) with the permissions in
[Security boundaries: GitHub token permissions](security.md#github-token-permissions),
which records what real fine-grained tokens did. A fine-grained token
cannot be used on a private repository whose pull requests have checks
([#291](https://github.com/iceboundrock/AutoForge/issues/291)): use
`gh auth login` or a classic token with `repo` there. LOCAL mode needs no
`gh` at all.

## Agent CLIs

`doctor` checks only the CLIs the configured profiles reach. The test suite
never runs a real agent: scripted providers and fake CLIs stand in for them
([tests/AGENTS.md](../tests/AGENTS.md)), so every version below rests on the
adapter's own verification or a recorded run, not on CI.

| CLI | Minimum `doctor` enforces | Versions with recorded evidence | Untested |
|---|---|---|---|
| Claude Code (`claude`) | none (below) | 2.1.263 (the flag syntax of `autoforge.example.yaml`); 2.1.291 to 2.1.293 (the `stream-json` runs the stream reducer was built from); 2.1.296 (the integration isolation and the Bash tool's shell, read from the build and checked against it). Source: the docstring of `src/autoforge/providers.py`. | before 2.1.263; after 2.1.296 (the maintainer's host had 2.1.306 on 2026-10-21, with no recorded run) |
| OpenCode (`opencode`) | 2.0.0 (`opencode >= 2.0.0 is required`; 1.x prints another version format and rejects the argv) | 2.0.23 (argv, stdin delivery and output, run against); 2.0.26 (the configuration isolation and the error lines, read from the build and checked with `opencode api --standalone`); 2.0.32 (the retry rule, read from the build). Source: the docstring of `src/autoforge/providers.py`. | after 2.0.32 (the maintainer's host had 2.1.1 on 2026-10-21, with no recorded run) |
| Pi (`pi`), optional | 1.0.0 (`pi >= 1.0.0 is required`; ADR 0003 §2.8) | 1.0.1 (the adapter's verification, [Pi operator guide](pi.md#install-pi-yourself)); 1.3.0 (the live smoke test on a DeepSeek API key, steps 1 to 6 passed, [#135](https://github.com/iceboundrock/AutoForge/issues/135#issuecomment-6308736531)) | the ChatGPT sign-in route on any version; versions other than those two |
| `claude-sdk`, `opencode-sdk` | not launchable | Their policy and configuration validate ([SDK policy](sdk-policy.md)), but the helpers that would launch them have not landed (#151, #152): `doctor` fails their `can launch` row and every launch is refused. | **unsupported** until the helpers land |

**Why Claude Code has no minimum version (#256 item 8).** Instead of a
version number, `doctor` checks what the adapter depends on: `claude
--version` must run, and `claude --help` must list `--safe-mode`,
`--strict-mcp-config` and `--setting-sources`, the flags every launch passes
so that no hook, plugin, MCP server or repository setting runs
(`agent 'claude' keeps integrations off`). A launch is refused without
them as well. A version floor would refuse nothing this check lets through
on the evidence there is (every release with the three flags that has been
run is listed above), and Claude Code's release cadence would make a floor
stale within days. The provider's argv is unchanged. Releases outside the
recorded range are untested: a changed `stream-json` record or result
shape surfaces as a provider failure of the step, not as a wrong state.

## Outer sandbox

`sandbox.mode: required`, and `preferred` where it enforces, need a host
that can enforce ADR 0005's outer sandbox. The default, `disabled`, needs
none of this. Supported on Linux x86_64 above does **not** mean that every
Linux host can use `required`: that is a property of the host, established
by a real minimal launch, never by a version number
([ADR 0005 D14.1](adr/0005-outer-sandbox-and-host-resource-capability-model.md#214-platform-capability-levels-item-14)).

The outer sandbox is experimental: it is opt-in enforcement on a host that
can enforce it, and it becomes supported only once a `make test-sandbox`
run on a capable host is recorded at the release candidate
([#255 C4](https://github.com/iceboundrock/AutoForge/issues/255#issuecomment-6377402004)).
The host table below therefore says *capable* where a host enforced, not
**supported**.

`doctor` reports it in two rows. `outer sandbox: capability (one minimal
sandboxed launch)` says `supported (bwrap <version>)`, or `unsupported:
this host cannot provide <capability> (<reason>)`, naming the first
requirement below that the probe found missing, in the words of the table's
second column. `outer sandbox: mode (<mode>)` says what the configured mode
does on this host: enforced, falling back visibly (`preferred` in LOCAL),
or refused before anything is saved or launched.

| Requirement | How `doctor` names it when missing |
|---|---|
| Linux | `a Linux kernel (the only platform with a backend)` |
| `bwrap` (bubblewrap) on the controller's `PATH` | `bwrap (bubblewrap) on the controller's PATH` |
| unprivileged user namespaces; refused, for example, by Ubuntu 23.10 and later's `kernel.apparmor_restrict_unprivileged_userns` or Debian's `kernel.unprivileged_userns_clone=0` | `an unprivileged user namespace` |
| PID namespaces | `a PID namespace` |
| IPC namespaces | `an IPC namespace` |
| a new procfs (rootless podman refuses one by default) | `a new procfs` |
| the sandbox's bind and tmpfs mounts | `the sandbox's bind and tmpfs mounts` |
| an unprivileged overlay mount, and a `bwrap` whose build can mount one (`--overlay`, bubblewrap 0.11 or later) | `an unprivileged overlay mount` |
| Landlock ABI 6 with Landlock enabled: Linux 6.12 or later | `the Landlock abstract-socket scope (Landlock ABI 6, Linux 6.12 or later)` |
| a new session keyring | `a new session keyring` |

| Host | Status | Evidence or reason |
|---|---|---|
| Fedora 44, kernel 7.2.x, SELinux enforcing, bubblewrap 0.12.0 | capable (enforces); experimental | Recorded: ADR 0005 §4 and `make test-sandbox` on the maintainer's host. Experimental until a release candidate records such a run ([#255 C4](https://github.com/iceboundrock/AutoForge/issues/255#issuecomment-6377402004)). |
| `ubuntu-latest` (hosted CI) | measured on every run | The `sandbox` job records bwrap's version and `--overlay`, the user-namespace policy, an unprivileged namespace launch and the Landlock ABI in its summary, and requires the real-backend tests wherever the runner measures capable. There is no fixed answer: each run's summary is the record ([Development](development.md#the-outer-sandbox)). |
| a kernel before 6.12 (for example the default kernels of Ubuntu 24.04 and Debian 12), or Landlock disabled | **unsupported** | No Landlock ABI 6. |
| unprivileged user namespaces disabled by a sysctl or an LSM policy | **unsupported** | Until the policy allows them. |
| rootless podman, default settings | **unsupported** | A new procfs is refused (measured, ADR 0005 §4); it works with `--security-opt unmask=/proc/* --security-opt label=disable`. |
| Docker (default seccomp profile), WSL2 | untested | Not measured. |
| macOS, Windows | **unsupported** | There is no backend. |

## Filesystem

| Requirement | Status | Evidence or reason |
|---|---|---|
| The state directory (`.autoforge/` for a remote run, `<git dir>/autoforge/state` for a LOCAL one, or `--state-dir` / `state_dir`) on a filesystem with hard links | **supported** on the runner's filesystem (`test (py3.11)`, `test (py3.12)`) and on XFS (the maintainer's host, `make check`) | Run logs and other controller files are published with `link(2)` so that a crash never leaves one half-written, and there is no non-atomic fallback. |
| A state directory on vfat/exFAT, or on a FUSE, SMB/CIFS or overlay mount without hard links | **unsupported** | `doctor`'s `state dir writable` row fails and names the requirement; a run refuses the directory the same way. |
| The worktrees (`<git common dir>/autoforge/worktrees/<n>` by default) | as the repository's own filesystem | Created with `git worktree`; under `required`, `doctor`'s `outer sandbox: hard links in writable trees` row reports regular files with more than one link there. |

## Provider authentication and billing

AutoForge stores, refreshes and pays for no model credential, and sends no
telemetry. Each agent CLI uses the credential you gave it yourself, under
your account with its vendor, and everything an agent reads goes to that
vendor ([Security boundaries](security.md#your-content-goes-to-model-vendors)).
The variables each provider receives are in
[Configuration: agent environment](configuration.md#agent-environment);
what the outer sandbox projects is in
[Provider credentials under `required`](configuration.md#provider-credentials-under-required).

| Provider | Credential | Who pays |
|---|---|---|
| Claude Code (default for `analyze_execute` and `fix`) | Your Claude Code login (`~/.claude`, or `CLAUDE_CONFIG_DIR`), or `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `CLAUDE_CODE_OAUTH_TOKEN` in the shell you start AutoForge from | Your Anthropic plan or API account |
| OpenCode (default for the reviews, `replan_reexecute` and `update_epic`, with `openai/...` models) | The provider credentials you configured in OpenCode, or the `OPENAI_*` (and, for other providers, `ANTHROPIC_*`, `GEMINI_*`, `GOOGLE_*`) variables of that shell | Your OpenAI account, or that of the provider a model names |
| Pi (optional, per profile) | A DeepSeek API key in Pi's store or forwarded on purpose, or the ChatGPT sign-in you made in Pi ([Pi operator guide](pi.md)) | Your DeepSeek account, or your ChatGPT plan's usage limits |
| GitHub (`gh` and git, the controller only) | Your `gh` login | Your GitHub plan; the CI your repository runs on the branches AutoForge pushes uses that repository's Actions minutes ([Security boundaries](security.md#pushed-code-runs-in-your-ci)) |
