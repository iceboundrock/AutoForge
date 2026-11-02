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
make test-fast    # the tests the changed paths map to, feedback only, not a gate
make test-sandbox # the real-backend sandbox tests; fail (not skip) without a backend
make test-sandbox-measured # what CI's `sandbox` job runs: measure, then require where capable
make dist-check   # what CI's `dist` job runs: build, check, install and run the artifacts
```

Run `make check` before declaring a change complete, and `make check-matrix`
when a change could be interpreter-sensitive. `make test-fast` is for
feedback while working and replaces neither
([Fast feedback while working](#fast-feedback-while-working)). Run
`make test-sandbox` too when a change touches the outer sandbox
(`sandbox*.py`, the executor, a provider's declaration, the engine's launch
routing), and `make dist-check` when a change touches packaging, a resource
the package reads at runtime (a prompt template), or the command line's
entry point ([The built distributions](#the-built-distributions)).

The distribution is named `autoforge-cli`; the import package and the
command are `autoforge`. A `.venv` made before the rename still lists the
old name: `make sync` replaces it.

## The live Pi smoke test

`tests/test_live_pi.py` (#135) holds the only test that calls a real agent
and a real model: `test_one_remote_review_runs_on_the_operators_real_pi`
runs one REVIEW through the operator's own Pi and the credential the
operator gave it, on a REMOTE run against a GitHub stand-in (the suite's
in-memory fake and a bare repository in the test's temporary directory),
which needs no `gh` login and writes nothing to GitHub. It takes the auth
route named by `AUTOFORGE_LIVE_PI_AUTH`: `api_key` (DeepSeek first, with
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
AUTOFORGE_LIVE_PI=1 AUTOFORGE_LIVE_PI_AUTH=api_key uv run pytest -m live \
  tests/test_live_pi.py::test_one_remote_review_runs_on_the_operators_real_pi
```

The prerequisites (Pi installed and given its credential by you,
`OPENAI_API_KEY` and `DEEPSEEK_API_KEY` unset unless one is forwarded on
purpose), the seven steps (its doctor's GitHub rows fail against the
stand-in by construction, and only its Pi rows count), the cost (one small
REVIEW of the account's quota) and what the test never does are in the
[Pi operator guide](pi.md#prove-it-on-your-machine-the-live-smoke-test).
A report names the AutoForge commit, the route, the `authType` and what
the controller wrote to the stand-in, and carries no key or account
detail; post it on #135 as the evidence of a run.
`tests/test_live_pi_support.py` runs in `make check`: it checks the
deselection in a pytest process of its own, the route settings, the
credential scan and the other checks, and rehearses the flow on both
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
missing (a Python with `os.memfd_create`, `bwrap` itself, an unprivileged
user, PID or IPC namespace, a new procfs, a bind mount, an overlay mount,
Landlock ABI 6). `pytest` is
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
- the Python that measures, and whether it has `os.memfd_create`, the
  memory file through which the backend hands `bwrap` a launch's options:
  a build without it cannot launch at all (uv's CPython 3.12.13 build has
  none; #389), so the real-backend tests that build runs skip under `auto`
  instead of failing on a host that is otherwise capable;
- which `bwrap` is on `PATH`, its version, and whether it has `--overlay`;
- the user-namespace policy: `kernel.unprivileged_userns_clone`, Ubuntu's
  AppArmor `kernel.apparmor_restrict_unprivileged_userns` and
  `user.max_user_namespaces`, and whether an unprivileged `bwrap` gets new
  user, PID and IPC namespaces and a new procfs;
- the kernel's Landlock ABI or, when it gives none, its answer:
  `EOPNOTSUPP` (Landlock disabled at boot), `ENOSYS` (no Landlock call),
  either of which a seccomp filter can also return, or another errno.

It calls the host capable when it is Linux, its Python has
`os.memfd_create`, its `bwrap` can mount an overlay, the namespace launch
succeeds and the Landlock ABI is at least 6.
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
   tests out of the sdist; none of LOCAL mode's files, its `local_*.md`
   templates, `local_workspace.py` or `run_contract.py`, whatever the tree
   declares), `LICENSE` byte for byte, and the core metadata
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
6. runs a REMOTE dry run (`autoforge run --epic ... --issue ...
   --dry-run --full-prompt`) in a scratch repository, with a fake `gh`
   first on `PATH` that records and fails any call, and checks it rendered
   the ANALYZE_EXECUTE prompt, called no `gh` and left the repository
   unchanged; then meets LOCAL mode's ways in with the installed
   command (ADR 0009 D3 to D5, #389): `--help` names no `local`, and in a
   scratch repository holding a `mode: "LOCAL"` state and beside a
   configuration with a `local:` block, `autoforge local`, `status`,
   `status --json`, `--state-dir … status`, `step`, `resume` and
   `unblock`, each both as a dry run and as the ordinary invocation that
   would take the lock and launch, and `run` over that state each exit 2
   with argparse's invalid choice, the unknown key or the unknown mode.
   They run with a scratch `HOME` and `TMPDIR`, no variable of the
   caller's but `PATH`, and recording fakes of `gh`, `claude`, `opencode`
   and `pi` first on `PATH`, so that a regression reaches no service and
   no model; none may be called, and the repository, the configuration
   file, the scratch `HOME` and `TMPDIR` must be unchanged. "Unchanged"
   compares each path's bytes, inode, mode, owner, link count, size,
   `mtime` and `ctime` (not `atime`, which a read sets): a file rewritten
   with its own bytes, replaced by a copy of itself, or with its times put
   back shows, and so does a lock file taken and removed, by its
   directory's `mtime`. It cannot see a write outside those directories,
   an advisory lock on a file that already existed, or a write by a
   process left running after the command exits;
7. runs the offline half of [Getting started](getting-started.md) (#259):
   `uv tool install` of the wheel alone (`--offline --no-index`), into tool
   directories and a home of its own, then, in an environment of `HOME`,
   `PATH` and git's configuration switched off, whose `PATH` is a fake
   `gh` that fails every call, the installed `autoforge`, git's directory
   and `/bin:/usr/bin` (no credential, and no agent CLI on the CI runner):
   `autoforge --version` and `--help` outside
   any repository, and, in a repository whose `origin` is on GitHub and
   which keeps `.autoforge/` and `autoforge.yaml` out of git the way the
   guide does, `autoforge doctor --json` with the built-in routing and
   with each starter of [`examples/`](../examples) saved as
   `autoforge.yaml`. Every row that needs no credential (`config`, `merge
   gate` closed, `git available`, `GitHub remote`, `launch environment`,
   `state dir writable` and the rest of `CREDENTIAL_FREE_ROWS`) must be
   `OK`, and the agent rows must name exactly the CLIs the configuration
   routes to. With each starter it also makes a REMOTE dry run, checked as
   in step 6, and the repository's `git status` must stay empty. The rows
   that need `gh`, a sign-in or an agent CLI are not checked;
8. prints the SHA-256 of both files (and, with `--sha256sums FILE`, writes
   them in `sha256sum` format).

It runs on one interpreter, `DIST_PYTHON` (default 3.12):
`make dist-check DIST_PYTHON=3.11` checks the other one CI runs. It removes
its scratch directory when it passes and keeps it, naming it, when it fails.
`--out-dir DIR` keeps the two files, which is how a release builds them once
([Releasing](releasing.md)). `tests/test_packaging.py`, part of `make check`,
is the cheap half: it fails when a non-Python file under
`src/autoforge/` is neither declared as package data nor deliberately left
out, when code reads a package resource from a module it does not know,
when the two version strings differ, when the classifiers drift from
the Python versions CI runs, or when the starters of `examples/` and the
CLIs they route to drift from what step 7 expects.

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
(its JUnit XML) with those of the first serial run that passed, so a test
that a configuration loses, adds, skips or fails is named rather than
folded into a total. Each run loads the tool as a pytest plugin that
records how pytest-xdist actually ran it, and a run that did not run as its
configuration names stops the benchmark unrecorded: a `-n` in
`PYTEST_ADDOPTS` or after `--` applies to every configuration, `serial`
included. Only a clean run is timed: the median and p95 (nearest rank: with
fewer than 20 repeats, the slowest run) of a configuration are its clean
runs', and it has none when no run was clean. It measures a commit,
refusing a worktree with anything changed or untracked, or one git fails to
read, and records the host's provenance. The same command on the same
`--out` resumes an interrupted benchmark, running again any run that left
no JUnit report, and refuses an `--out` measured on another commit,
interpreter, host (the machine, by a keyed hash of its machine ID and host
name, and its CPU model) or set of CPUs:

```bash
uv run --isolated --locked --python 3.12 python -m tests.parallel_bench \
    --repeats 3 --config serial --config n4-load --config n8-load --out /var/tmp/bench \
    -- -o tmp_path_retention_policy=none
```

What #340 measured, on two hosts with pytest 9.1.1 and pytest-xdist 3.8.0:

- **A**, a 4-CPU, 8 GB Fedora 44 workstation that was running other work at
  the same time (Git 2.56).
- **B**, a Proxmox container allowed 8 of the 12 hardware threads of a
  6-core Intel i7-8700K, with 16 GB, Debian 13, Linux 7.0 and Git 2.47.3,
  and `TMPDIR` on a tmpfs `/tmp`; other sessions shared it, mostly idle.

Times are wall seconds of the whole `pytest` process; a run is clean when it
exits 0 with every test's outcome that of its session's first passing
serial run, and only clean runs are timed. Every run in the table, all
after #340's fixes to the tests below, was clean: 722d88f on A, and 0255ca2
on B, which differs from 722d88f only in `tests/parallel_bench.py`, its
tests and this page.

| host, Python, commit | configuration | runs | clean | median | p95 | min | speedup |
|---|---|---|---|---|---|---|---|
| B, 3.11.18, 0255ca2 | serial | 3 | 3 | 501 | 501 | 495 | 1.00x |
| B, 3.11.18, 0255ca2 | n4-load | 3 | 3 | 139 | 139 | 138 | 3.60x |
| B, 3.11.18, 0255ca2 | n8-load | 3 | 3 | 93 | 103 | 85 | 5.40x |
| B, 3.12.15, 0255ca2 | serial | 3 | 3 | 509 | 532 | 505 | 1.00x |
| B, 3.12.15, 0255ca2 | n4-load | 3 | 3 | 144 | 155 | 142 | 3.53x |
| B, 3.12.15, 0255ca2 | n8-load | 3 | 3 | 95 | 98 | 84 | 5.37x |
| A, 3.12.15, 722d88f | serial | 1 | 1 | 1431 | 1431 | 1431 | 1.00x |
| A, 3.12.15, 722d88f | n2-load | 3 | 3 | 847 | 849 | 827 | 1.69x |
| A, 3.12.15, 722d88f | n4-load | 3 | 3 | 627 | 642 | 626 | 2.28x |

Before the fixes, on A with Python 3.11.17 at 236f261, one screening run
per configuration found the defects: `n2-load`, `n4-load` and `n4-loadfile`
failed four tests between them that pass serially, while both serial runs,
`n4-worksteal` (635 seconds) and `n2-loadfile` (854) were clean.

- **The speedup depends on idle CPUs.** On B four workers give 3.6x and
  eight 5.4x, on both interpreters; on A four gave 2.3x, sharing its 4 CPUs
  with other work (the load average reached about 5). Each test runs slower
  the more workers share the cores: under 3.11 on B the tests add up to
  about 490 seconds serially, 510 on four workers and 570 on eight (on 6
  physical cores), so with each worker's start-up and the slowest test (up
  to 14 seconds) at the end, eight workers finish in 85 to 103 seconds
  rather than 71.
- **`load`, not `loadfile`.** `loadfile` keeps a module on one worker, and
  `test_engine.py` alone takes 77 of B's 490 seconds (22% of A's serial
  time), more than an even eighth of the suite: on eight workers, the one
  that gets it finishes last. On A before the fixes, `worksteal` was within
  a run's noise of `load`.
- **Isolation.** Every serial run was clean. Before the fixes, five
  real-backend sandbox tests had failed under `-n 4` with a 76-character
  `TMPDIR`, and `tests/dist_check.py`'s REMOTE dry-run check failed under
  CPU load. All but one of the failures were a race or a limit that the
  workers' load or their `popen-gw<N>/` directories exposed, not state
  shared between tests, and #340 fixed them (the rules are in
  [tests/AGENTS.md](../tests/AGENTS.md#what-a-test-may-never-do)). One
  failure has no explanation yet: under `n4-loadfile`,
  `test_ctrl_c_at_a_real_controlling_terminal_still_interrupts_and_settings_come_back`
  (`tests/test_interaction.py`) waited 30 seconds for its child to exit
  after Ctrl-C at a pseudo-terminal. 40 runs of the same child under CPU
  load all ended at once, it did not recur on A after the fixes, and on B
  it passed in under half a second in all 18 runs.
- **What B could not run.** 81 tests skip on B in every run, serial or not:
  60 real-backend sandbox tests (its call for the Landlock ABI fails with
  `EOPNOTSUPP`, [#349](https://github.com/iceboundrock/AutoForge/issues/349)), 16
  that need zsh, and the 5 opt-in probes. After the fixes, the sandbox
  tests have run on workers on A under 3.12 only; under 3.11 they run
  serially, in CI's `sandbox` job wherever its runner can enforce.
- **The temporary directory.** pytest keeps the `tmp_path` trees of the
  last three sessions, and a quarter of the way through one serial run its
  tree already held 78,000 files and directories: on B, `/tmp`'s 1,048,576
  inodes ran out during the first serial run and thousands of tests
  errored. Hence `-o tmp_path_retention_policy=none` above, which removes a
  session's tree when it ends and changes nothing else. `TMPDIR` cannot
  move just anywhere: the sandbox policy refuses paths under `/dev` and
  `/run` (with it under `/dev/shm`, 92 tests failed serially), and on B's
  disk a serial run was a quarter done after 30 minutes.

The recommendation that follows:

- **Locally**, `make test-parallel` with as many workers as the host has
  CPUs, and no more than its memory holds: a serial run's resident size
  peaked at 0.5 GB, and each worker is such a process. On a 2-CPU host,
  `PYTEST_WORKERS=2`; on B, eight. Not `-n auto`: it counts CPUs and nothing
  else.
- **In CI**, the suite stays serial. Running it on workers is a change to
  `.github/workflows/ci.yml`, a protected path that takes a human's review.
  Make that change once the Ctrl-C failure above is explained and the
  real-backend sandbox tests have run clean on workers under 3.11 too,
  after this benchmark has run on the runner itself (it records the
  runner's usable CPUs), and with `-n` set to that count and
  `--dist=load`. A parallel job replaces the serial one only while its runs
  reconcile clean, and goes back to serial if it flakes.

## Where the serial suite's time goes

#341 measured where a serial run spends its time and removed what the
measurement showed was waiting rather than testing. The profile ran the
default suite at a86b729 serially on a 4-CPU, 8 GB Fedora 44 workstation
(an Intel i5-4300U, Linux 7.2, Git 2.56), with Python 3.14.8 and `TMPDIR`
on an xfs disk, through a measuring plugin that is not in the
repository: per test, the wall and CPU time of setup, call and teardown,
the CPU time of its child processes, each fixture's own setup time and
every subprocess started, by program and caller. A second pass over 251
tests of `tests/test_engine.py` timed `Popen.wait`, `time.sleep`,
`os.fsync`, `subprocess.run` and the executor.

The 11,883 tests took 1,453 seconds of test phases. Setup was 39 of them,
so after #345 the fixtures are no longer the cost; the call phase was
1,409, against 307 seconds of CPU in the test process and 445 in its
children. Ranked by what each cost there:

| cost | measured | change |
|---|---|---|
| `os.fsync` in the test process | 9,728 calls in the engine sample, 49.8 of its 97.3 s (5.1 ms each on the disk; next to nothing on a tmpfs) | a stand-in that flushes nothing outside the durability modules ([tests/AGENTS.md](../tests/AGENTS.md#fixtures-and-fakes-testsconftestpy)) |
| `Popen.wait(timeout)` polling `waitpid` | 37,981 launches through `execute`; 14.0 s of sleeps in the sample's 3,824 waits; `git rev-parse` through `execute` 4.45 ms, 2.42 without the poll | the executor waits on the child's pidfd |
| the tests' own `git` processes | 32,480, about 3.7 ms each in the sample; 10,763 are the fake GitHub's reads of a branch head | the fake reads a loose ref as a file |
| real login shells behind a fake runner | 286 launch probes (859 shells), 227 in `tests/test_doctor.py`, about 0.19 s each | none |
| `git_repo` | 2,508 repositories, about 7.5 ms each | none |
| `tmp_path` numbering | 0.48 ms per use at the start of a session, 1.67 after 3,700, still growing | numbered without listing the basetemp |
| what tests wait for on purpose | timeouts, exit graces and real kills: `test_providers.py` 56 s, `test_executor.py` 41 s, `test_executor_duplex.py` 36 s, `test_scheduler.py` 52 s, `test_epic_recovery.py` 56 s | none: that is what they test |

Two costs stay. A fake runner hands the launch preflight's shell probe to
the real executor on purpose (it may observe the probe), so caching its
answer, as `_one_shell_probe_per_answer` does for a launch without one,
needs a rule for what the answer depends on when the runner is a test's;
and a repository copied from a template would save about 2 ms of 7.5 while
giving every test the same initial commit. Neither is worth its risk yet.

Then the default suite ran serially on the same host and `TMPDIR`, twice
at a86b729 and twice at 3259116, which has the four changes, alternating
(the commit after it waits on the pidfd with `poll` rather than a
selector, and measured the same).
Every run passed, and test by test the outcomes at 3259116 are those at
a86b729 plus the ten tests the changes add:

| commit | runs, wall seconds | median | user CPU | system CPU |
|---|---|---|---|---|
| a86b729 | 1,465.9, 1,462.7 | 1,464.3 | 553, 551 | 262, 262 |
| 3259116 | 932.2, 953.6 | 942.9 | 485, 495 | 224, 229 |

That is 36% less wall time; each run's resident size still peaked at
about 0.5 GB. The modules that launch processes and save state gained most
(median seconds: `test_engine.py` 322 to 138, `test_replan.py` 99 to 38,
`test_merge_certificate.py` 71 to 27, `test_local.py` 55 to 25), and those
that wait on purpose least (`test_providers.py` 56.5 to 55.8,
`test_executor.py` 41.8 to 41.4).

How much the stand-in saves depends on the disk under `TMPDIR`. On this
host `fsync` took 4.2 ms on the xfs disk and 0.001 ms on its tmpfs `/tmp`.
With `TMPDIR` on the tmpfs, `tests/test_engine.py` alone, twice before and
twice after, alternating, took a median of 154 seconds of tests before and
121 after: 21% less, against 57% on the disk, all of it from the other
three changes.

On a host and `TMPDIR` like this one, a serial run that takes more than
about 1,040 seconds, 10% above the median at 3259116, has lost some of
this, and is worth profiling the same way before it is accepted. Tests pin what
each change does, not its time: the executor's
`test_the_exit_wait_reaps_the_child_once_it_has_exited_without_polling`
(one `wait(0)` once the child has exited, on a host that opens and polls
a pidfd; `test_without_a_pidfd_the_exit_wait_is_proc_wait` covers the
fallback on every host),
`test_the_engine_tests_save_state_without_waiting_for_the_disk` (the
stand-in is in place there), `tests/test_tmp_path_retention.py`'s
`test_a_tmp_path_is_numbered_without_listing_the_basetemp_and_never_reused`
and `test_a_directory_another_name_numbered_into_is_not_reused_once_removed`,
and `tests/test_github.py`'s
`test_the_fakes_origin_reads_a_branch_head_as_git_does` (the fake's read
agrees with git's). `tests/test_safefs.py`'s
`test_the_fsync_stand_in_refuses_exactly_what_fsync_refuses` keeps the
stand-in from hiding an error a real flush would raise. Each change reverts
on its own; the stand-in is the one to revert if a defect turns up that
only a real flush in the test process would have caught.

[EPIC #338's exit audit](test-suite-performance-audit.md#1-the-suite-before-and-after-on-one-host)
measures the whole EPIC: the suite at its baseline, `1f6abad`, against
`main` at `d0948fb`, serially and alternating on one host, with the tests
the two have in common timed apart. The runs' reports are in the
repository, and `python -m tests.suite_comparison` reproduces its numbers
from them.

## Fast feedback while working

`make test-fast` runs the test modules that the paths you changed map to,
so a change gets feedback in seconds to a minute or two rather than after
the whole suite (535 seconds serially at a4bc60c on Python 3.14 and host
B of [The suite on several workers](#the-suite-on-several-workers)). The
changed paths are what your commits, your index and your worktree changed
since the merge base with `FAST_BASE` (`main` by default), each compared
on its own so that one undoing another's change hides neither, and your
untracked files; `FAST_PYTEST_ARGS` reach pytest:

```bash
make test-fast
make test-fast FAST_BASE=origin/main FAST_PYTEST_ARGS="-n 4 -x"
uv run python -m tests.fast_targets src/autoforge/evidence.py   # what it would select, and why
```

`tests/fast_targets.py` holds the mapping. A test module selects itself
and every test module that imports it; a path with an entry selects the
entry's test modules and every test module that imports its module; a
module almost every test runs through (`engine.py`, `state.py`,
`config.py`, the prompt templates, conftest and a few more) selects the
whole suite, and so does any path without an entry (`pyproject.toml`, a
document, a new module). A selection holding the live tests
(`tests/test_live_pi.py`), which the default options deselect, holds
their offline tests too, and never runs the live ones. On the same host
and commit, a change to `tests/test_evidence.py` ran in 3 seconds when it
selected itself alone (it now selects `test_certificate.py` too, which
imports its helpers), one to
`src/autoforge/scheduler.py` in 72 (four modules that wait on purpose),
and one to `src/autoforge/evidence.py` in 125 (ten modules,
`test_engine.py` among them). `tests/test_fast_targets.py` fails when a
file under `src/autoforge/` or a helper module under `tests/` has no
entry, so a new module needs one.

A FAST run is feedback, never evidence
([ADR 0010](adr/0010-fast-feedback-and-full-validation-evidence.md)). It
selects where a regression is likely to show first, not every test that
can observe a module: a test that reaches a module through the engine does
not import it, so a change can pass `make test-fast` and fail the full
suite. `make check`, `make check-matrix` and CI never run it, and the
controller neither runs nor reads it: its `validation.commands`, the merge
gate and the required `ci` check decide as before.

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

Each `test` job also writes a duration report, which decides nothing
([Test durations in CI](#test-durations-in-ci)).

## Pinned actions

Every `uses:` in `.github/workflows/` names its action by a full commit
SHA, with the release that commit is as a comment:

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
```

A tag such as `v7` is a name the action's owners can move to other code,
which would then run under the `ci` check the merge gate reads, or in the
release build; a commit SHA names one tree (#288).
`tests/test_workflow_pins.py` fails when a `uses:` names a tag, a branch, a
short SHA, a Docker image or a local action, when its comment is not a full
release `vX.Y.Z`, when it is spelled in a form the check's line scan cannot
read (flow style, a quoted key or value, the value on the next line, an
anchor or an alias for the value, a reference merged in by `<<`), and when
two references to one action repository, in any of the workflows, name
different commits or releases. YAML's own parse locates each job's and
step's `uses` key, and only those lines are scanned, so a line of a
multi-line `run: |` script that reads like a `uses:` (one writing a
composite action, say) is no reference. A whole step repeated by an alias
(`- *checkout`) is its anchor's step: its pin is the anchor's line, checked
and reported once. A workflow whose steps are all `run` has nothing to pin and
passes. No test can tell offline whether a SHA really is the release its
comment names; that is what the update procedure is for.

To update a pin:

1. Resolve the release's commit from the action's own repository, not a
   fork, a mirror or a Marketplace page, naming the tag and its peeled
   commit exactly:
   `git ls-remote https://github.com/<owner>/<repo> 'refs/tags/<release>' 'refs/tags/<release>^{}'`.
   Use the line whose ref is exactly `refs/tags/<release>^{}` when there
   is one (an annotated tag: the other line is the tag object, not a
   commit), and otherwise the one that is exactly `refs/tags/<release>`
   (a lightweight tag). Never a wildcard such as `'refs/tags/<release>*'`:
   it also lists every release whose name starts with `<release>`
   (`v7.0.1` matches `v7.0.10` and `v7.0.1-rc.1`), each another commit.
2. Read what changed between the old commit and the new one, release notes
   included: the pin moves trust to that review.
3. Replace the SHA and the comment at every occurrence in every workflow in
   one change: the check requires one commit per action repository. uv's
   own `version` and `checksum`, in CI's `dist` job and the release build,
   are a separate pin, which `tests/test_publish_workflow.py` keeps equal.

Such a PR touches `.github/workflows/`, one of the
`safety.protected_merge_paths`, so the controller blocks before merging it,
for a human to review and merge
([What a green `ci` does and does not prove](#what-a-green-ci-does-and-does-not-prove)).
Dependabot's `github-actions` ecosystem can propose these updates, SHA and
comment together, but is not enabled: it would open pull requests on its own
schedule, which is the maintainer's decision, and each would still be one
for a human to merge.

A pin fixes an action's code, not what that code fetches when it runs. In
`test`, `static` and `sandbox`, setup-uv installs the uv it resolves at run
time (no `version` is given there; only `dist` and the release build pin uv
by version and checksum), and the publish action pulls a container image by
a tag ([Releasing](releasing.md#the-workflow)).

## Test durations in CI

The suite's time has so far been measured by hand on two workstations
([The suite on several workers](#the-suite-on-several-workers),
[Where the serial suite's time goes](#where-the-serial-suites-time-goes)).
Nothing kept what CI's own runners take: a job's duration shows in the
Actions UI, but not its counts, its skips, or whether it is slower than
main. Since #344 every `test` job keeps that and compares it with main's
recent runs. The report is report only: it never changes a job's result or
the required `ci` check.

### The pipeline

In the `test` job of `.github/workflows/ci.yml`, on each interpreter:

1. pytest runs as `make test` does, with one more option,
   `--junitxml="${RUNNER_TEMP}/durations/junit.xml"`, which selects,
   deselects and changes nothing;
2. `actions/cache/restore` brings back main's duration history for that
   interpreter, the newest one saved under `ci-durations-v1-py<version>-`;
3. `tests/ci_durations.py report` (standard library only, on the bare
   interpreter) reads the JUnit report and the history, writes the report
   into `${RUNNER_TEMP}/durations/report/` and appends its Markdown to the
   job's summary;
4. `actions/upload-artifact` uploads that directory as
   `durations-py<version>-attempt<n>`, kept 30 days;
5. on a push to main only, `report` adds the run to the history when it is
   clean, and `actions/cache/save` saves the history under a new key.

The report holds, per run:

- **counts**: tests, passed, failed, errors, skipped and xfailed, from the
  JUnit report (a test reported twice, for an error in teardown, counts
  once, as an error), and the skips grouped by reason. The live test, which
  the default options deselect, is not in the report;
- **durations**: the suite's wall time (pytest's session), the tests' own
  time, each module's, the 20 slowest tests, and every test's in
  `tests.json`;
- **linkage**: the commit, ref and event, the run and attempt with their
  URL, the check run (`test (py3.11)`), which the required check `ci`
  aggregates, the pytest step's outcome and the runner image.

`summary.json` is the run, `tests.json` its tests, `comparison.json` the
verdict below and `summary.md` what the job summary shows.

### The regression policy

A run is compared only with runs of its own **category**: the job, the
interpreter, the test selection (`default`: the default options, serially),
the uv cache state (`warm` or `cold`, setup-uv's `cache-hit`; `unknown` when
it gave none) and the runner (`github-hosted Linux X64`). The runner image
is recorded but is no category, since GitHub replaces it every week or so;
the report notes when it differs from the newest baseline run's.

The **baseline window** is the last 20 runs of a push to main in that
category, recorded in the 30 days before the run, one per commit (the newest
attempt), and clean: a complete report, at least one test, no failure or
error, the suite's wall time, and a pytest step that succeeded. A run of a
pull request, a failed run, a cancelled run and a run without a report are
never baseline runs.
The window's median and p95 (nearest rank, as `tests/parallel_bench.py`
takes it: with 20 runs, the second slowest) of the suite's wall time decide:

| verdict | when | what to do |
|---|---|---|
| `regression` | above 1.10 × the window's p95 | look at the modules that grew most (the report lists them); rerun the job once to rule out the runner; if it stays, profile as in [Where the serial suite's time goes](#where-the-serial-suites-time-goes) before merging, or open an issue when main shows it |
| `slower` | above 1.10 × the median | nothing by itself; a trend of `slower` runs on main is worth a look |
| `ok` | within those bounds | nothing |
| `faster` | below 0.90 × the median | read the notes: fewer tests or more skips than main's newest run means less was tested, not that the suite got faster |
| `insufficient-baseline` | fewer than 5 runs in the window | nothing to compare with yet |
| `not-comparable` | the run has failures or errors, no test, no wall time for the suite, or a pytest step that did not succeed | its times are shown, without a verdict, and it never becomes a baseline run |
| `no-data` | no JUnit report, or one that does not read | the report claims nothing; the job's own result says whether the suite passed |

The `slower` bound is #341's rule that a serial run more than 10% above the
median is worth profiling. The `regression` bound sits beyond the spread the
window itself shows, so a noisy runner raises it and a steady one lowers it.
These values are a starting point, not a measurement of GitHub's runners,
whose times no run kept before this report: once the history holds full
windows, compare how often an unchanged main gets `slower` or `regression`,
and widen the bounds in `tests/ci_durations.py` (`SLOWER`, `REGRESSION`) if
it is more than rarely.

**Missing metrics** never read as `ok`: a run without a report is
`no-data`, one that failed is `not-comparable`, and a category without five
baseline runs is `insufficient-baseline`. A report without the suite's wall
time is `not-comparable` too, and its wall time stays empty: the tests' own
time leaves out collection and whatever the session does between tests, so
it is shown beside it but never takes its place. A history that was never
saved, has expired or does not read is ignored with a note, and so is each
sample in it that the tool did not write, counted; the window then starts
again from the next pushes to main.

**A cold uv cache** is compared with cold runs only. The cache mostly
speeds up `uv sync`, which the report does not time (its step's duration
is in the run's UI). The report shows the window of the other cache state
beside the verdict, for reference and never as one, so a difference between
the two shows instead of being averaged away.

### What it never does

- **Decide a job.** Each report step runs after pytest whether it passed or
  failed, never when the run was cancelled (`if: success() || failure()`),
  with `continue-on-error` and a 5-minute timeout of its own, and `report`
  exits 0 whatever it finds. The job's result is pytest's, and `ci` still
  needs every job and fails on a skipped or cancelled one. A duration gate
  would make the required check depend on how busy a shared runner is.
  `tests/test_ci_workflow.py` pins all of this, and that no other step may
  continue on error. `compare --fail-on-regression` exists for a
  maintainer's own use and is never passed in CI.
- **Upload what a test printed.** The artifact holds test ids, outcomes,
  times and skip reasons, which pytest's `-rs` prints in the job's log
  anyway. A failure's message, captured output and the raw JUnit report stay
  on the runner.
- **Need a secret or a permission.** The cache and artifact actions use the
  runner's own token for those services, not `GITHUB_TOKEN`, and the
  workflow keeps `contents: read`. They are pinned to a commit, as in
  `pypi-publish.yml`. Nothing outside GitHub Actions is involved.
- **Trust a pull request's history.** Only a push to main saves it; a pull
  request restores main's and cannot write it. The history decides nothing
  but the report.

GitHub evicts a cache entry unused for seven days, and the oldest ones once
a repository's caches pass their size limit; each push to main saves a new
entry and every run reads the newest, so the history is lost only when no
run reads it for a week. Two pushes to main whose runs overlap
restore the same history and the later save wins, losing one sample;
`concurrency` cancels the older run on main, so that is rare.

The `sandbox` and `dist` jobs have no report yet. `--selection` names the
category a `sandbox` report would take.

### Reproducing a report

The tool runs on any interpreter without the project installed. A local
run, summarized and compared with main's recent artifacts (a local runner
is its own category, so expect `insufficient-baseline` against CI's runs;
`--python` names the interpreter the suite ran on):

```bash
uv run pytest --junitxml=/tmp/durations/junit.xml
python3 -I tests/ci_durations.py summarize --python 3.14 \
    --junit /tmp/durations/junit.xml --out /tmp/durations/report
for id in $(gh run list --workflow ci.yml --branch main --event push --limit 20 \
        --json databaseId --jq '.[].databaseId'); do
    gh run download "$id" --pattern 'durations-py3.11-*' --dir "/tmp/artifacts/$id"
done
python3 -I tests/ci_durations.py compare --current /tmp/durations/report/summary.json \
    --baseline /tmp/artifacts
```

A run of CI is compared the same way, from its own artifact: here the
newest pull-request run of the branch checked out, against the main runs
downloaded above (its first attempt's report; `attempt2` names its first
re-run's):

```bash
run=$(gh run list --workflow ci.yml --branch "$(git branch --show-current)" \
    --event pull_request --limit 1 --json databaseId --jq '.[0].databaseId')
gh run download "$run" --name durations-py3.11-attempt1 --dir "/tmp/current/$run"
python3 -I tests/ci_durations.py compare --current "/tmp/current/$run/summary.json" \
    --baseline /tmp/artifacts
```

`compare` reads summaries and history files, or directories holding them at
any depth, and prints the same Markdown; `--window`, `--min-samples` and
`--max-age-days` try another policy on the same runs. Every run names its
artifacts alike (`durations-py3.11-attempt1`), so each run is downloaded
into a directory of its own. `gh run download` extracts an artifact chosen
by `--name` straight into `--dir`, which is where the second `compare`
looks for `summary.json`. One of main's downloaded runs can be compared
with the others the same way: it is left out of its own window, and so is
every other run of its commit.

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

A test's `tmp_path` is deleted as soon as the test passes
(`tmp_path_retention_policy = "failed"` in `pyproject.toml`); the directory
of a test that failed, or errored in a fixture's setup or teardown, stays
under the session's basetemp for inspection. pytest's own `failed` reads
only the call phase and would delete an errored test's, so
`tests/tmp_path_retention.py` replaces its `tmp_path` and decides after the
teardown report. Kept, a full run's `tmp_path` directories come to about
900 MB, which on a host whose `/tmp` is a tmpfs (Fedora's default, half the
RAM) is memory, and stays until a reboot or `systemd-tmpfiles` ages it out.
Directories a fixture makes with `tmp_path_factory.mktemp` last the whole
session; pytest removes the basetemp after a green run unless `--basetemp`
named it. To keep every test's directory while debugging, run
`uv run pytest -o tmp_path_retention_policy=all ...`.

Outside the modules that test the durable writes themselves
(`REAL_FSYNC_MODULES` in `tests/conftest.py`), the test process's
`os.fsync` refuses what `fsync(2)` refuses and flushes nothing, since no
test can observe a flush ([Where the serial suite's time
goes](#where-the-serial-suites-time-goes)). A test of what a write survives
belongs in one of those modules.

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
