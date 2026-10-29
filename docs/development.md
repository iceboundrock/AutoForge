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
make test-parallel # pytest on 4 pytest-xdist workers, opt-in, not a gate
make test-sandbox # the real-backend sandbox tests; fail (not skip) without a backend
make test-sandbox-measured # what CI's `sandbox` job runs: measure, then require where capable
make dist-check   # what CI's `dist` job runs: build, check, install and run the artifacts
```

Run `make check` before declaring a change complete, and `make check-matrix`
when a change could be interpreter-sensitive. Run `make test-sandbox` too
when a change touches the outer sandbox (`sandbox*.py`, the executor, a
provider's declaration, the engine's launch routing), and `make dist-check`
when a change touches packaging, a resource the package reads at runtime
(a prompt template), or the command line's entry point
([The built distributions](#the-built-distributions)).

The distribution is named `autoforge-cli`; the import package and the
command are `autoforge`. A `.venv` made before the rename still lists the
old name: `make sync` replaces it.

## The live Pi smoke test

`tests/test_live_pi.py` (#135) is the one test that calls a real agent and
a real model: one LOCAL REVIEW through the operator's own Pi and the
credential the operator gave it, on the auth route named by
`AUTOFORGE_LIVE_PI_AUTH`: `api_key` (DeepSeek first, with
`require_oauth: false`; the acceptance run) or `oauth` (the ChatGPT
sign-in; an optional regression run). It is opt-in twice over, so
`make check` and CI never run it and stay offline:

- `pyproject.toml` registers a `live` marker and adds `-m 'not live'` to
  pytest's default options, so a default run deselects it (it is collected
  and deselected, never run). Passing `-m live` selects it, since a later
  `-m` wins.
- Selected, it skips with its reason unless `AUTOFORGE_LIVE_PI=1`. Any
  other non-empty value fails rather than being read as "no". It then
  fails unless `AUTOFORGE_LIVE_PI_AUTH` names a route: it has no default.

```bash
AUTOFORGE_LIVE_PI=1 AUTOFORGE_LIVE_PI_AUTH=api_key uv run pytest -m live tests/test_live_pi.py
```

The prerequisites (Pi installed and given its credential by you,
`OPENAI_API_KEY` and `DEEPSEEK_API_KEY` unset unless one is forwarded on
purpose), its seven steps, its cost (one small REVIEW of the account's
quota) and what it never does are in the
[Pi operator guide](pi.md#prove-it-on-your-machine-the-live-smoke-test).
Its report names the AutoForge commit, the route and the `authType` and
carries no key or account detail; post it on #135 as the evidence of a run.
`tests/test_live_pi_support.py` runs in `make check`: it checks the
deselection in a pytest process of its own, the route settings, the
credential scan and the other checks, and rehearses the whole flow on both
routes against the fake `pi`.

## The operator-comment byte-fidelity capture

Exact evidence for an operator action (K10, #243) rests on a premise no
offline test establishes: a payload posted through the documented pipe,
`autoforge status --action X-… | gh issue comment <target> --body-file -`,
comes back from `GitHubClient.get_issue_comments` byte for byte. A
maintainer settles it once, by hand, on a scratch issue they own, from the
repository root, with a UTF-8 locale and their own `gh` login. The tool
never writes to GitHub; you post, as an operator would:

```bash
S=https://github.com/<owner>/<repo>/issues/<scratch>
uv run python -m tests.operator_comment_fidelity render --via pipe --issue "$S" \
  | gh issue comment "$S" --body-file -
uv run python -m tests.operator_comment_fidelity render --via web --issue "$S" > web.md
# open web.md, copy all of it, paste it into the comment box of $S on github.com, submit
uv run python -m tests.operator_comment_fidelity capture --issue "$S"
```

`capture` writes `tests/fixtures/operator_comment_fidelity/capture.json`:
the raw GraphQL node of each comment, `gh --version`, the commit and the
time. Commit it as captured, never edited.
`tests/test_operator_comment_fidelity.py` replays it and pins the
controller's decision: the pipe's comment must be exact evidence, and a web
paste that is not the payload byte for byte is a conflict. If the pipe does
not round-trip, those tests fail on the committed capture: #243's M2 stops,
and ADR 0004 revisits exact evidence.

The committed fixture was captured on 2026-10-21 on scratch issue #264,
with gh 2.102.1 at commit `6de4227`. The pipe's comment came back byte for
byte, so exact evidence stands. The web paste did not: it lost the two
trailing spaces of the hard-break line and gained `%` and two line feeds
after the marker (apparently the shell's end-of-line mark, copied with the
text from a terminal), and the controller decides a conflict on it. Capture
again, and commit the new fixture as captured, when the payload's header,
marker or text rules change: the replay's first test fails then. A new
capture's web paste may differ in other ways; its test pins this capture's
bytes, so update it to what the new one shows, never the fixture.

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

## The built distributions

`make check` tests the source tree, through an editable install; it never
looks at what a user installs. `make dist-check` (`tests/dist_check.py`,
standard library only) tests the artifacts themselves, offline apart from
fetching the build backend, with no agent, no GitHub access and no
credential:

1. copies the files git tracks or would track (`git ls-files --cached
   --others --exclude-standard`) to a scratch directory outside the
   checkout, so a stale `*.egg-info` or an ignored file of the working tree
   cannot reach an artifact;
2. builds the sdist from that copy and the wheel from the sdist
   (`uv build`), which is how an installer builds the sdist too, with the
   build backend [`build-constraints.txt`](../build-constraints.txt) pins:
   that setuptools, installed only if its SHA-256 is one the file lists
   (`--build-constraints`, `--require-hashes`), and the wheel must name it
   as its `Generator`;
3. checks both: the exact set of files each ships (every module and every
   prompt template, `LICENSE`, and nothing else; `MANIFEST.in` keeps the
   tests out of the sdist), `LICENSE` byte for byte, and the core metadata
   (Metadata-Version 2.4 or later, `Name: autoforge-cli`, the version,
   `License-Expression: MIT`, `License-File: LICENSE`, `Requires-Python`, the
   classifiers, no dependency outside an extra, and the `autoforge` console
   script);
4. installs the wheel, not editable and from no index, into a fresh virtual
   environment outside the checkout, and runs `autoforge --version` and
   `autoforge --help` there;
5. from that environment's interpreter, imports `autoforge` from the
   installed package (never the checkout), checks that the prompt templates
   on disk are exactly the registered ones, and renders every one of them
   the way the engine does, each phase template with its common template;
6. runs a LOCAL dry run (`autoforge local init`, then
   `autoforge local run ... --dry-run --full-prompt`) in a scratch
   repository and checks it rendered the prompt, wrote no state and changed
   nothing but the feature file, then a REMOTE dry run (`autoforge run
   --epic ... --issue ... --dry-run --full-prompt`) in another, with a fake
   `gh` first on `PATH` that records any call, and checks it rendered the
   ANALYZE_EXECUTE prompt, called no `gh` and left the repository
   byte-identical;
7. prints the SHA-256 of both files (and, with `--sha256sums FILE`, writes
   them in `sha256sum` format).

It runs on one interpreter, `DIST_PYTHON` (default 3.12):
`make dist-check DIST_PYTHON=3.11` checks the other one CI runs. It removes
its scratch directory when it passes and keeps it, naming it, when it fails.
`--out-dir DIR` keeps the two files, which is how a release builds them once
([Releasing](releasing.md)). `tests/test_packaging.py`, part of `make check`,
is the cheap half: it fails when a non-Python file under
`src/autoforge/` is neither declared as package data nor deliberately left
out, when code reads a package resource from a module it does not know,
when the two version strings differ, or when the classifiers drift from
the Python versions CI runs.

## The suite on several workers

`make test-parallel` runs the default suite on pytest-xdist workers:
`PYTEST_WORKERS` of them (4 by default), with `--dist=load`. It is a faster
way to run the suite while working, not a gate: `make test`, `make check`
and every CI job still run it serially, and `make check` is still what to
run before declaring a change complete.

The evidence behind it is `tests/parallel_bench.py`, a tool run by hand
rather than a test. It runs the default suite once per configuration and
repeat, interleaving the configurations so that a change in the host's
load spreads over all of them. It reconciles every run's per-test outcomes
(its JUnit XML) with the first serial run's, so a test that a configuration
loses, adds, skips or fails is named rather than folded into a total. It
reports each configuration's median and p95 (nearest rank: with fewer than
20 repeats, the slowest run) and the host's provenance, and the same
command on the same `--out` resumes an interrupted benchmark:

```bash
uv run --isolated --locked --python 3.12 python -m tests.parallel_bench \
    --repeats 3 --config n4-load --config n2-load --once serial --out /var/tmp/bench
```

What #340 measured, on a 4-CPU, 8 GB Fedora 44 workstation that was running
other work at the same time (Git 2.56, pytest 9.1.1, pytest-xdist 3.8.0).
Times are wall seconds of the whole `pytest` process; a run is clean when it
exits 0 with every test's outcome the serial run's. 236f261 is before #340's
fixes to the tests below, 722d88f after them:

| Python, commit | configuration | runs | clean | median | p95 | min | speedup |
|---|---|---|---|---|---|---|---|
| 3.12.15, 722d88f | serial | 1 | 1 | 1431 | 1431 | 1431 | 1.00x |
| 3.12.15, 722d88f | n2-load | 3 | 3 | 847 | 849 | 827 | 1.69x |
| 3.12.15, 722d88f | n4-load | 3 | 3 | 627 | 642 | 626 | 2.28x |
| 3.11.17, 236f261 | serial | 2 | 2 | 1443 | 1445 | 1442 | 1.00x |
| 3.11.17, 236f261 | n2-load | 1 | 0 | 818 | 818 | 818 | 1.76x |
| 3.11.17, 236f261 | n4-load | 1 | 0 | 628 | 628 | 628 | 2.30x |
| 3.11.17, 236f261 | n4-worksteal | 1 | 1 | 635 | 635 | 635 | 2.27x |
| 3.11.17, 236f261 | n2-loadfile | 1 | 1 | 854 | 854 | 854 | 1.69x |
| 3.11.17, 236f261 | n4-loadfile | 1 | 0 | 665 | 665 | 665 | 2.17x |

- **Four workers give about 2.3x, not 4x.** The 3.11 serial run's tests
  add up to 1430 seconds (11,585 tests, none longer than 11 seconds), so
  four workers could reach about 360; each test takes longer when four run
  at once (the load average reached about 5 on the 4 CPUs).
- **`load`, not `loadfile`.** `worksteal` was within a run's noise of
  `load`. `loadfile` keeps a module on one worker, and `test_engine.py`
  alone is 22% of the serial time.
- **Isolation.** Every serial run was clean. On 236f261 the parallel runs
  failed four tests that pass serially; before that, five real-backend
  sandbox tests had failed under `-n 4` with a 76-character `TMPDIR`, and
  `tests/dist_check.py`'s REMOTE dry-run check failed under CPU load. All
  but one were a race or a limit that the workers' load or their
  `popen-gw<N>/` directories exposed, not state shared between tests, and
  #340 fixed them (the rules are in
  [tests/AGENTS.md](../tests/AGENTS.md#what-a-test-may-never-do)). One
  failure has no explanation yet: under `n4-loadfile`,
  `test_ctrl_c_at_a_real_controlling_terminal_still_interrupts_and_settings_come_back`
  (`tests/test_interaction.py`) waited 30 seconds for its child to exit
  after Ctrl-C at a pseudo-terminal. 40 runs of the same child under CPU
  load all ended at once, and it did not recur in the 3.12 runs above or in
  a 3.11 `-n 4` run after the fixes.

The recommendation that follows:

- **Locally**, `make test-parallel` with as many workers as the host has
  CPUs, and no more than its memory holds: a serial run's resident size
  peaked at 0.5 GB, and each worker is such a process. On a 2-CPU host,
  `PYTEST_WORKERS=2`. Not `-n auto`: it counts CPUs and nothing else.
- **In CI**, the suite stays serial. Running it on workers is a change to
  `.github/workflows/ci.yml`, a protected path that takes a human's review.
  Make that change once the Ctrl-C failure above is explained, after this
  benchmark has run on the runner itself (it records the runner's usable
  CPUs), and with `-n` set to that count and `--dist=load`. A parallel job
  replaces the serial one only while its runs reconcile clean, and goes
  back to serial if it flakes.

## Continuous integration

The same checks run hosted on every pull request and every push to
`main` (`.github/workflows/ci.yml`), each after a locked install
(`uv sync --locked`): `pytest` on Python 3.11 and 3.12, and `ruff check` /
`ruff format --check` / `mypy` once. A third job, `sandbox`, runs the outer
sandbox's real backend on the runner (`make test-sandbox-measured`,
[The outer sandbox](#the-outer-sandbox)). A fourth, `dist`, builds the sdist
and the wheel and installs and runs the wheel, on 3.11 and 3.12
(`make dist-check`, [The built distributions](#the-built-distributions)).
`make check` runs the same commands
plus the lockfile check, but only on the interpreter the local environment
resolves (`.python-version`); `make check-matrix` runs `pytest` on each CI
interpreter in its own isolated environment (`uv run --isolated --locked
--python <v>`), leaving `.venv` alone. The versions live in the Makefile's
`CI_PYTHON_VERSIONS`; `tests/test_ci_workflow.py` fails when that list, the
workflow's matrix, or the mirrored commands drift apart, or when the
`sandbox` job stops running exactly `make test-sandbox-measured` over every
real-backend module, or the `dist` job stops running `make dist-check` on
every CI interpreter.

The repository's other workflow, `pypi-publish`
(`.github/workflows/pypi-publish.yml`), is not CI: it never runs on a pull
request or a branch, only for a version tag `v*`, and publishes that
release to PyPI after the maintainer approved it
([Releasing: publishing to PyPI](releasing.md#publishing-to-pypi)).
`tests/test_publish_workflow.py` pins its triggers, permissions, pins and
steps.

The workflow uses no repository or organization secrets, but its jobs are
not credential-free: GitHub gives every run an automatic `GITHUB_TOKEN`, and
the workflow's `permissions: contents: read` limits it to reading the
repository. `actions/checkout` and `setup-uv` use that token by default
(checkout also leaves it in the checkout's git config for the rest of the
job, except in `dist`, which checks out with `persist-credentials: false`);
the tests themselves need no credential. The aggregate `ci` job is
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
