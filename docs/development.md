# Development

How to set up a development environment, run the checks CI runs, and what
the hosted `ci` check does and does not prove. The testing contract (what
must be covered, and what a test may never do) is in
[testing.md](agent-guides/testing.md) and
[tests/AGENTS.md](../tests/AGENTS.md).

## Commands

```bash
make sync         # uv sync (.venv + dev tools)
make test         # uv run pytest
make lint         # uv run ruff check src tests
make fmt          # uv run ruff format src tests
make fmt-check    # uv run ruff format --check src tests
make typecheck    # uv run mypy src
make lock-check   # uv lock --check (what CI's `uv sync --locked` enforces)
make check        # lock-check + the four CI commands, on the local interpreter
make check-matrix # pytest on every CI Python (3.11, 3.12), opt-in
make test-sandbox # the real-backend sandbox tests; fail (not skip) without a backend
make test-sandbox-measured # what CI's `sandbox` job runs: measure, then require where capable
```

Run `make check` before declaring a change complete, and `make check-matrix`
when a change could be interpreter-sensitive. Run `make test-sandbox` too
when a change touches the outer sandbox (`sandbox*.py`, the executor, a
provider's declaration, the engine's launch routing).

## The live Pi smoke test

`tests/test_live_pi.py` (#135) is the one test that calls a real agent and
a real model: one LOCAL REVIEW through the operator's own Pi and ChatGPT
sign-in. It is opt-in twice over, so `make check` and CI never run it and
stay offline:

- `pyproject.toml` registers a `live` marker and adds `-m 'not live'` to
  pytest's default options, so a default run deselects it (it is collected
  and deselected, never run). Passing `-m live` selects it, since a later
  `-m` wins.
- Selected, it skips with its reason unless `AUTOFORGE_LIVE_PI=1`. Any
  other non-empty value fails rather than being read as "no".

```bash
AUTOFORGE_LIVE_PI=1 uv run pytest -m live tests/test_live_pi.py
```

The prerequisites (Pi installed and signed in by you, `OPENAI_API_KEY`
unset), its seven steps, its cost (one small REVIEW of subscription quota)
and what it never does are in the
[Pi operator guide](pi.md#prove-it-on-your-machine-the-live-smoke-test).
Its report carries no account detail; post it on #135 as the evidence of a
run. `tests/test_live_pi_support.py` runs in `make check`: it checks the
deselection in a pytest process of its own, the credential scan and the
other checks, and rehearses the whole flow against the fake `pi`.

## The outer sandbox

`tests/test_sandbox_bwrap.py`, `tests/test_sandbox_routing.py` and
`tests/test_sandbox_parity.py` launch the real backend (`bwrap`). Each
first runs the backend's own capability probe
(`autoforge.sandbox_bwrap.probe`, ADR 0005 D14.1) and, on a host that
cannot enforce, skips with the probe's typed reason: which capability is
missing (`bwrap` itself, an unprivileged user, PID or IPC namespace, a new
procfs, a bind mount, an overlay mount, Landlock ABI 6). `pytest` is
configured with `-rs`, so every run, CI's included, prints each skip and
its reason; a silent pass of zero tests cannot hide one.
`AUTOFORGE_REQUIRE_SANDBOX_BACKEND` decides what such a host does instead:

- `1` (`make test-sandbox`): each skip is a failure. The target for a host
  known to be capable (a Linux 6.12+ workstation with `bwrap` 0.11 or later
  and unprivileged user namespaces).
- `auto` (`make test-sandbox-measured`): `tests/sandbox_host.py` first
  measures the host, independently of the probe, and each skip is a failure
  exactly when that measurement says the host can enforce. A probe that
  wrongly answers "unsupported" therefore cannot turn a capable host's
  coverage into a skip.
- unset: each test skips with the reason. Any other value fails.

The measurement records what a host provides for the backend:

- the platform, the kernel and the distribution;
- which `bwrap` is on `PATH`, its version, and whether it has `--overlay`;
- the user-namespace policy: `kernel.unprivileged_userns_clone`, Ubuntu's
  AppArmor `kernel.apparmor_restrict_unprivileged_userns` and
  `user.max_user_namespaces`, and whether an unprivileged `bwrap` gets new
  user, PID and IPC namespaces and a new procfs;
- the kernel's Landlock ABI.

It calls the host capable when it is Linux, its `bwrap` can mount an
overlay, the namespace launch succeeds and the Landlock ABI is at least 6.
`uv run python -m tests.sandbox_host` prints it on its own.

**Hosted CI measures its runner on every run.** The `sandbox` job of
`.github/workflows/ci.yml` installs `bubblewrap` from the runner's
distribution (`apt-get install bubblewrap` on `ubuntu-latest`) and runs
`make test-sandbox-measured`. That target writes the measurement into the
job's log and its summary, then runs the three modules under `auto`:

- on a runner that measures capable, the real-backend tests run and must
  pass;
- on one that does not, they skip with the probe's reason, and the summary
  records what the runner lacks, so `make test-sandbox` on a capable host
  is what covers them.

The repository records no fixed answer for "can GitHub's runner enforce?".
That answer follows the image GitHub ships: its Ubuntu release, its kernel
(Landlock ABI 6 needs Linux 6.12), its `bubblewrap` package (`--overlay`
needs 0.11) and its AppArmor policy on unprivileged user namespaces. Each
run's summary is the record for that run's runner. A green `sandbox` job
whose summary says "cannot enforce" proves the measurement, not the
backend.

Neither sandbox target is part of `make check`, which runs on any
development host. The rest of the suite stands in for the probe (`tests/conftest.py`'s
`sandbox_probe` fixture answers "supported" without a launch), so every
mode and the pre-launch gate are tested on any host.

## Continuous integration

The same checks run hosted on every pull request and every push to
`main` (`.github/workflows/ci.yml`), each after a locked install
(`uv sync --locked`): `pytest` on Python 3.11 and 3.12, and `ruff check` /
`ruff format --check` / `mypy` once. A third job, `sandbox`, runs the outer
sandbox's real backend on the runner (`make test-sandbox-measured`,
[The outer sandbox](#the-outer-sandbox)). `make check` runs the same commands
plus the lockfile check, but only on the interpreter the local environment
resolves (`.python-version`); `make check-matrix` runs `pytest` on each CI
interpreter in its own isolated environment (`uv run --isolated --locked
--python <v>`), leaving `.venv` alone. The versions live in the Makefile's
`CI_PYTHON_VERSIONS`; `tests/test_ci_workflow.py` fails when that list, the
workflow's matrix, or the mirrored commands drift apart, or when the
`sandbox` job stops running exactly `make test-sandbox-measured` over every
real-backend module.

The workflow uses no repository or organization secrets, but its jobs are
not credential-free: GitHub gives every run an automatic `GITHUB_TOKEN`, and
the workflow's `permissions: contents: read` limits it to reading the
repository. `actions/checkout` and `setup-uv` use that token by default
(checkout also leaves it in the checkout's git config for the rest of the
job); the tests themselves need no credential. The aggregate `ci` job is
a single stable check name that survives adding or renaming a matrix entry,
and it is required on `main` by a repository ruleset. That is what gives the
controller's pre-merge gate ("every check on the PR succeeded") something
real to verify instead of a vacuously green PR. The same ruleset requires a
pull request (with zero required approvals, since GitHub forbids
self-approval and any higher count would deadlock the controller's own
merge) and blocks force-push and deletion of `main`. Because that ruleset is
repository configuration rather than code, `autoforge doctor` re-reads it
(see [GitHub safety: doctor's branch-rule check](agent-guides/github-safety.md#doctors-branch-rule-check))
so that disabling it, renaming the context or adding a bypass actor is
noticed before an unattended run relies on it.

## What a green `ci` does and does not prove

It proves the suite passed on GitHub's runners for that commit, which is
strictly more than an agent's claim that it ran the tests. It is not a
signal independent of the PR: the workflow that defines the check, and the
code the check runs, both come from the PR. Four things bound that.

- The controller refuses to merge a PR that touches
  `safety.protected_merge_paths` (default `.github/workflows/`), so a PR
  cannot redefine the check that clears it; that refusal lives in the
  controller, in version control and under test, rather than in a repository
  setting that can drift unnoticed.
- With `safety.verify_check_definition` it also compares the jobs and steps
  the `ci` run actually executed with the base branch's own run of the same
  workflow, so a check that was redefined through something the protected
  paths do not cover is refused too.
- With `merge.verification_commands` set, the controller runs the
  repository's own checks (`pytest`, `ruff`, `mypy`, …) on an export of the
  reviewed commit before it merges (its committed bytes, proven to hash back
  to the commit's tree): evidence it produced itself, not a check name it
  read.
- A PR that weakens what its tests assert still has to pass the review
  phase, whose findings are what the loop bounds act on.

None of this replaces a human reading the diff (the local commands still run
the PR's tests, just under the controller's eye rather than the PR's
workflow), which is why `safety.allow_merge` is off by default. The gates
themselves are specified in
[GitHub safety: merge safety](agent-guides/github-safety.md#merge-safety).

## Testing

Tests never call real Claude Code, OpenCode or GitHub write APIs. Agents are
replaced by a `ScriptedProvider` and GitHub by an in-memory fake; the
executor tests use real local Python subprocesses only, and the sandbox's
real-backend tests real `bwrap` around fake CLIs
([The outer sandbox](#the-outer-sandbox)). The one exception, a real Pi and
a real model call, is the opt-in live smoke test
([The live Pi smoke test](#the-live-pi-smoke-test)), which no default run
selects.

- [testing.md](agent-guides/testing.md): the coverage contract, including
  the high-priority areas every behavioural change there must test.
- [tests/AGENTS.md](../tests/AGENTS.md): fixtures and fakes, what a test may
  never do, and where each behaviour is tested.

## Where the code lives

The module map (which module owns what) is in
[src/autoforge/AGENTS.md](../src/autoforge/AGENTS.md); the responsibility
boundaries between the engine, provider adapters, executor, GitHub client
and prompt system are in [architecture.md](agent-guides/architecture.md).
Contributor rules for the whole repository are in
[AGENTS.md](../AGENTS.md).
