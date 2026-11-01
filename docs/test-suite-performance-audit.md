# Test suite performance and validation latency: EPIC #338's exit audit

- **Status:** written on 2026-11-01, after M5 (#344) merged as `40385a8`,
  the last of the EPIC's work items to land, against `main` at `d0948fb`.
  It decides nothing. Closing the EPIC is the maintainer's decision (§5).
- **Audits:** the six items of EPIC #338's exit checklist (§2), its seven
  required invariants (§3) and its provisional target (§4).
- **Measures:** the default suite at the EPIC's planning baseline,
  `1f6abad`, against `main` at `d0948fb`, serially on one host (§1).
- **Inputs:** #338's body and its four comments, which the controller
  handed to the agent that wrote this audit, and the repository from
  `1f6abad` to `d0948fb`. That agent holds no GitHub credential and read
  nothing else of GitHub: no child issue, pull request, review, CI run or
  artifact. So this audit says of those only what #338's comments and
  `main`'s commit messages say.

The verdicts for an exit criterion:

- *Met*: what the criterion asks is on `main`, and the tests named hold it;
- *Met in part*: the repository's part is met, and the rest is GitHub's or
  the maintainer's (§5);
- *Open*: nothing in the repository can show it.

The verdicts for an invariant:

- *Held*: the EPIC's commits kept it, and the tests named hold it now;
- *Held in part*: the repository's part held, and the rest is GitHub's or
  the operator's (§5);
- *Not shown*: nothing in the repository can show it.

`tests/test_suite_performance_audit.py` holds §2 and §3. It fails when
either list does not take the EPIC's items once each, in order, when an
item has no verdict, when one met or held, wholly or in part, names no
test, or when a test named here no longer exists.

## 1. The suite before and after, on one host

Each work item measured its own change on its own host. #345, M2's first
slice, took the median from 950.2 s to 530.1 s (#338's comment of
2026-10-26 23:24 UTC; the report is on PR #345). #341's second change,
`acebe52`, took it from 1,464 s to 943 s ([Development: where the serial
suite's time goes](development.md#where-the-serial-suites-time-goes)).
Nothing measured the EPIC as a whole on one host, and the checklist's last
item asks for a before and after. This section is that measurement.

### 1.1 Host and method

The host is a desktop, neither host A nor host B of `docs/development.md`,
so no absolute number below compares with theirs. It has an AMD Ryzen 5
5600G (6 cores, 12 threads, the `powersave` governor) and 27 GiB of memory,
and runs Fedora Linux 44 Server on bare metal: Linux 7.2.11, Git 2.56.0, uv
0.13.6, bubblewrap 0.12.0, no zsh. The interpreter was uv's CPython
3.12.14, since 3.12 is a CI interpreter, with pytest 9.1.1. Each commit ran
from a clone of its own outside the issue worktree, with a virtual
environment made by `uv sync --locked --python 3.12` offline. Each run got
a new, empty `TMPDIR` on the tmpfs `/tmp`, removed after it, so no run
inherited another's basetemp and the inodes never ran out. Besides the
runs, the host carried the agent session that drove them and the
controller that started it. That session edited documents throughout and
ran a few seconds of tests during some runs. The load average was at most
1.12 at the start or end of a run.

```bash
# On 2026-11-01 between 19:22 and 20:33 UTC, alternating:
# before, after, before, after, before, after.
TMPDIR=/tmp/af338/t /usr/bin/time -f '%e %U %S %M' \
    .venv/bin/python -m pytest -p no:cacheprovider --junitxml=<run>.xml
```

Before the first timed run, one `--collect-only` run per commit compiled
the bytecode. `-p no:cacheprovider` keeps pytest from writing into the
clone; it selects and orders nothing. `tests/parallel_bench.py` could not
take these runs: it measures the commit it is part of, and `1f6abad`
predates it. `tests/suite_comparison.py` reconciles the runs' JUnit
reports test by test, reading each as that tool does, and the runs'
records are in the repository (§1.4).

`1f6abad` needed one change to finish on this host. Its
`tests/test_interaction.py` helper `_pipe` writes all of a test's typed
input into a pipe before anything reads it. A new pipe holds 64 KiB, but
only 8 KiB once the user's pipes hold more than `fs.pipe-user-pages-soft`,
as this user's did. The first attempt hung in a test that types more than
9,000 bytes, with pytest blocked writing to its own pipe, and was
discarded. #259 fixed the helper on `main` (`a2fd256`). The baseline ran
with that fix applied to the helper alone: the same tests, and a helper
that writes what the pipe does not hold from a thread.

### 1.2 Results

Every run passed, and on each side the three runs' outcomes are the same
test by test. Wall seconds are the whole `pytest` process's, user and
system CPU are its own and its children's, and memory is its peak
resident size:

| commit | runs, wall seconds | median | user CPU | system CPU | memory | passed, skipped, deselected |
|---|---|---|---|---|---|---|
| `1f6abad` | 872.6, 869.5, 873.9 | 872.6 | 395.6 | 276.6 | 442 to 446 MiB | 11,407, 18, 1 |
| `d0948fb` | 545.0, 545.1, 544.7 | 545.0 | 241.1 | 130.2 | 464 to 466 MiB | 12,594, 21, 1 |

The CPU columns are medians. The suite at `main` takes 37.5% less wall
time than at the baseline while running 10.4% more tests: 12,615 test ids
against 11,425, 43 ms a test against 76. The baseline's counts equal those
of the CI run the EPIC's body cites. The skips are the
tests that need zsh, which this host lacks (13 at the baseline, 16 on
`main`), and the five opt-in probes. The real-backend sandbox tests ran in
every run.

10,352 test ids are in both suites. Their own time (setup, call and
teardown, from the JUnit reports) fell from a median of 779.5 s to 419.6 s,
46% less. The modules whose common tests gained most, by the same median:

| module | `1f6abad` | `d0948fb` |
|---|---|---|
| `tests/test_state.py` | 36.7 | 2.7 |
| `tests/test_redaction.py` | 36.7 | 4.0 |
| `tests/test_replan.py` | 47.2 | 18.7 |
| `tests/test_engine.py` | 95.5 | 69.2 |
| `tests/test_result_parser.py` | 25.4 | 2.6 |
| `tests/test_safefs.py` | 19.0 | 0.4 |
| `tests/test_doctor.py` | 40.7 | 25.6 |
| `tests/test_github.py` | 17.9 | 5.9 |

### 1.3 What the difference is, and what it is not

- **Not the EPIC's alone.** 38 commits lie between the two, EPIC #383's
  removal of LOCAL mode among them, and the suites differ. Timing the tests
  the two have in common takes out what was added and removed, but not what
  the other commits changed in the code those tests run. The pattern fits
  the EPIC's changes, though this measurement does not attribute it.
  Modules late in the alphabet, many short tests each, paid most for
  fixture setup that grew with every test already run, which #345 removed.
  Modules that launch processes and save state are the ones #341's exit
  wait and `fsync` stand-in aimed at.
- **The tests that left.** 1,073 test ids of `1f6abad` are not in
  `d0948fb`'s suite, and every one passed in the baseline. 319 are
  parametrizations of test functions that are still there, with other
  parameters: 162 in `tests/test_state.py` and 83 in
  `tests/test_result_parser.py`. The other 754 belong to 346 test functions
  that left their module. LOCAL's removal took 751 of the 754 from theirs,
  458 with #387 (`aee704e`) and 293 with #388 (`3eb68ac`). Of those 751, 14
  ids, in 12 functions, have a namesake in another module on `main`: the
  same two commits moved or rewrote them there, as #388 did with
  `tests/test_durable_run_contract.py`'s tests of the state root. Three
  went where #283 (`9a7d8f9`) and #349 (`a86b729`) changed the behaviour
  they pinned and tests of the new behaviour replaced them. No commit of
  this EPIC removed one (I1).
- **A tmpfs `TMPDIR`.** `fsync` costs next to nothing on a tmpfs, so #341's
  stand-in saves little here. With `TMPDIR` on a disk, as on a hosted
  runner, the stand-in saves more. On host A, #341's changes took 57% off
  `tests/test_engine.py`'s time with `TMPDIR` on its disk, and 21% on its
  tmpfs, none of that 21% from the stand-in.
- **One host, three runs each, alternating.** The three runs of each
  commit lie within 4.45 s of each other at the baseline and 0.40 s on
  `main`, so the noise is small next to the difference. A hosted runner is
  another machine (§4).

### 1.4 The runs' records

[`tests/fixtures/suite_performance_audit/runs.tar.xz`](../tests/fixtures/suite_performance_audit/runs.tar.xz)
holds what the six runs left, as the runner script in it, `bench.sh`,
wrote it: each run's JUnit report, GNU time's figures and pytest's output;
each commit's `--collect-only` output; every run's start, end, exit status
and load average, in the order they ran (`events.log`); and the change
`1f6abad` ran with (`pre-259.patch`). One thing differs from what the runs
wrote: the host name pytest puts into a JUnit report is removed, since
`tests/parallel_bench.py` records none either. Nothing in the archive
records §1.1's description of the host, which is this audit's, and the
clones and their virtual environments are gone.

`tests/suite_comparison.py` reads the archive without extracting it and
prints this section's numbers: the results table, each run against the
first of its commit, the test ids in common and their own time, the tests
that left, the load average and the modules' table.

```bash
uv run python -m tests.suite_comparison
uv run python -m tests.suite_comparison --git .   # in a clone holding 1f6abad..d0948fb
```

Which commit removed each test function that went needs the history
between the two commits, which a shallow clone lacks.
[`removals.tsv`](../tests/fixtures/suite_performance_audit/removals.tsv)
records it, function by function. `--git` derives it again from
`git log -p` and fails when the two differ. A function is its JUnit class
name and its name, so one moved to another module counts as gone from its
old one (§1.3).

`tests/test_suite_performance_audit.py` holds this section to the archive
and to `removals.tsv`: every number above is the one the tool computes from
them, so neither can change without the other
(`test_section_1_is_what_the_runs_records_say` and
`test_the_removals_name_each_function_that_went_and_section_1s_tallies`),
and what §1.1 says of how the runs were made is what their records say
(`test_the_records_say_how_the_runs_were_made`).

## 2. The exit checklist

- **E1. Auditable baseline completed and reviewed.** *Met in part.*
  M0 (#339) measured `1f6abad` serially and reported on #339, with gaps it
  stated itself: a single complete instrumented run (its repeat ran out of
  inodes), three failures and 60 sandbox skips particular to its host, no
  local Python 3.11 run and no durable raw artifacts (#338's comment of
  2026-10-26 17:58 UTC). That report is on GitHub, not in the repository.
  What closed its gaps is on `main`:
  - *Repeated serial controls.* #345's first arm ran `1f6abad` three times
    (#338's comment of 23:24 UTC). #340 ran the suite serially three times
    on each interpreter on host B, with provenance and median and p95, and
    reconciled each run test by test
    ([Development: the suite on several workers](development.md#the-suite-on-several-workers)).
    #341 profiled a serial run per test phase (setup, call and teardown,
    the children's CPU, each fixture and every subprocess), and its A/B
    ran two runs of each commit, alternating. §1 adds the EPIC's own
    before and after, three more serial runs of `1f6abad` among them, and
    every run's records are in the repository (§1.4).
  - *The tool that makes a baseline auditable.* `tests/parallel_bench.py`
    records the host's provenance, reconciles every test's outcome, times
    only clean runs and refuses a worktree with changes:
    `tests/test_parallel_bench.py::test_provenance_has_every_fact_a_resume_compares_and_no_machine_id`,
    `tests/test_parallel_bench.py::test_reconciliation_names_every_lost_added_and_changed_test`,
    `tests/test_parallel_bench.py::test_only_a_clean_run_is_timed`
    and
    `tests/test_parallel_bench.py::test_a_worktree_with_changes_is_refused_before_anything_runs`.
  - *CI's own baseline.* Since #344 each CI `test` run records its counts,
    skips, module times and slowest tests with the runner image:
    `tests/test_ci_durations.py::test_a_report_gives_every_count_each_modules_time_the_slowest_tests_and_the_skips`.
  - *The host-specific failures.* The plan asked for them to be filed
    outside the EPIC. Three later issues match them and are fixed: the
    login shell's `PATH` on Debian (#347, `99980c2`), the commit-graph case
    on Git 2.47 (#348, `4ec791d`) and the Landlock refusal (#349,
    `a86b729`). #349's host stays unsupported, as measured
    ([Compatibility](compatibility.md)).

  "Reviewed" is GitHub's to show. The plan comment links the maintainer's
  assessment of M0 on #339; whether #339 is closed, this audit cannot see.
  Whether that report, its gaps and what closed them make E1's completed
  and reviewed baseline is the maintainer's decision, which PR #432's note
  on #339 records before the EPIC can close (§5).
- **E2. Parallel test selection and isolation correctness demonstrated.** *Met.*
  #340 (`b14f959`) ran the suite serially and on 2, 4 and 8 workers under
  `load`, `loadfile` and `worksteal`. It reconciled every run test by test
  against a passing serial run and timed only clean runs. After its fixes,
  every run on both hosts was clean
  ([Development: the suite on several workers](development.md#the-suite-on-several-workers)).
  The runs before the fixes found tests that failed only on workers or
  under load, and #340 repaired them rather than excluding them. Among the
  causes were Git's detached auto-maintenance after a test commit, socket
  paths longer than `sun_path` under a worker's directory, and a timing
  budget read from the wall clock:
  `tests/test_run_namespace.py::test_the_fixtures_commit_without_starting_detached_maintenance`,
  `tests/test_sandbox.py::test_a_socket_is_bound_at_a_path_longer_than_sun_path`
  and
  `tests/test_redaction.py::test_redact_obj_suffix_allocation_is_linear_in_the_colliding_keys`.
  The benchmark stops on a run that did not run as its configuration names,
  and a serial and a two-worker run report the same tests:
  `tests/test_parallel_bench.py::test_a_run_that_did_not_run_as_its_configuration_stops_the_benchmark`
  and
  `tests/test_parallel_bench.py::test_a_serial_and_a_two_worker_run_report_the_same_tests`.

  The mode adopted is `load`, for local use only (`make test-parallel`).
  One failure is unexplained: a Ctrl-C test that waited 30 seconds under
  `n4-loadfile` and did not recur. CI stays serial until that failure is
  explained, the real-backend sandbox tests have run clean on workers under
  3.11, and the benchmark has run on the runner itself. Only documents held
  that every gate stays serial;
  `tests/test_suite_performance_audit.py::test_workers_stay_opt_in_and_every_gate_runs_the_suite_serially`,
  added by this audit, holds it now.
- **E3. Measured hot spots improved or explicitly rejected on evidence.** *Met.*
  Improved: #345 (`6fddbc9`) made the three credential-isolation fixtures
  allocate without listing basetemp. They were the largest setup cost M0
  measured, 321 s. #345 also stopped the loopback servers waiting up to
  0.5 s at shutdown. #341 (`acebe52`) removed four measured causes of
  waiting: the executor reaps a child on its pidfd instead of polling, the
  tests' `os.fsync` flushes nothing outside the modules that test durable
  writes, the fake GitHub reads a loose ref as a file, and `tmp_path` is
  numbered without listing basetemp. Rejected on evidence, with the reason
  for each: caching the launch preflight's login-shell probe behind a fake
  runner, a repository copied from a template (2 ms of 7.5 saved, every
  test given the same first commit), and the tests that wait on purpose
  ([Development: where the serial suite's time goes](development.md#where-the-serial-suites-time-goes)).
  The tests pin what each change does, not its time:
  `tests/test_child_env.py::test_every_test_gets_its_own_empty_private_directories_for_the_preflight`,
  `tests/test_executor.py::test_the_exit_wait_reaps_the_child_once_it_has_exited_without_polling`,
  `tests/test_executor.py::test_without_a_pidfd_the_exit_wait_is_proc_wait`,
  `tests/test_engine.py::test_the_engine_tests_save_state_without_waiting_for_the_disk`,
  `tests/test_safefs.py::test_the_fsync_stand_in_refuses_exactly_what_fsync_refuses`,
  `tests/test_tmp_path_retention.py::test_a_tmp_path_is_numbered_without_listing_the_basetemp_and_never_reused`
  and
  `tests/test_github.py::test_the_fakes_origin_reads_a_branch_head_as_git_does`.
- **E4. Fast/full validation evidence contract documented and tested.** *Met.*
  [ADR 0010](adr/0010-fast-feedback-and-full-validation-evidence.md)
  (#342, `cdb9ac0`) decides that FAST is feedback and never evidence:
  FAST has no record type, tier field or `validation.commands` definition,
  so every `ValidationRecord` is FULL by construction; the controller alone
  selects FULL; CI is unchanged. `make test-fast` is in no gate, and the
  tool says its run is not evidence:
  `tests/test_fast_targets.py::test_the_fast_target_runs_the_tool_and_is_never_a_gate`,
  `tests/test_fast_targets.py::test_without_run_it_prints_the_targets_and_runs_nothing`
  and
  `tests/test_fast_targets.py::test_every_package_file_and_test_helper_has_an_entry`.
  A narrower run never stands for the full one, in a record, a binding, a
  certificate or an implementer's claim:
  `tests/test_evidence.py::test_a_narrower_selections_pass_never_stands_for_the_full_definition`,
  `tests/test_evidence.py::test_complete_evidence_for_a_narrower_set_is_not_evidence_for_the_full_set`,
  `tests/test_evidence.py::test_a_full_failure_stays_terminal_whatever_passes_beside_it`,
  `tests/test_certificate.py::test_a_certificate_for_a_narrower_set_never_applies_under_the_full_set_and_back`
  and
  `tests/test_validation_gates.py::test_an_implementers_fast_pass_is_a_claim_that_neither_skips_nor_satisfies_the_set`.
  Stage 2, a controller FAST pre-check, is designed and gated (D11 to D13),
  not built. The criterion asks for the contract, not for stage 2.
- **E5. Timeout mechanism established and actionable operator diagnostics published.** *Met.*
  The EPIC asked whether an apparent 15-minute timeout was the provider's
  idle supervision or the controller's command limit. #343 (`3d836ee`)
  read the nine idle-timeout kills in one operator's controller records,
  2026-10-12 to 2026-10-26. Every one was the executor's agent idle timer
  (`execution.default_idle_timeout_seconds`, 900 s in the example
  configuration), and none the wall-clock ceiling, loop detection,
  containment or `execution.command_timeout_seconds`. Five were Claude
  CLIs waiting between turns, three of them on a test suite they had
  started in the background. Three were OpenCode CLIs hung after a dropped
  connection, and one a Pi review inside a bash call that printed nothing
  for thirty minutes. The records are on that machine, not in the
  repository. On `main`: a kill's last activity is the one before the
  decision to kill, and a Claude run's summary says where its stream stood;
  [Usage: reading an idle timeout](usage.md#reading-an-idle-timeout) gives
  how to place an idle kill and what helps with each cause, and the
  troubleshooting guide links to it. No limit or default changed. What a
  killed run's records now say is held by
  `tests/test_executor.py::test_what_a_child_writes_while_it_is_killed_is_not_its_last_activity`,
  `tests/test_executor_duplex.py::test_what_a_child_writes_while_it_is_killed_is_not_its_last_activity`,
  `tests/test_claude_stream.py::test_the_summary_says_where_a_stream_stood_when_it_ended`,
  `tests/test_providers.py::test_a_silent_claude_is_killed_and_its_summary_says_where_it_stood`,
  `tests/test_engine.py::test_an_idle_timeout_names_the_limit_and_the_last_activity`
  and
  `tests/test_troubleshooting_guide.py::test_the_guides_links_resolve`.
- **E6. Performance report shows pre/post comparison without weakening required CI.** *Met.*
  §1 compares the EPIC's baseline with `main`. `docs/development.md`
  compares #341's change and #340's configurations, and #345's comparison
  is on its PR. The one change the EPIC made to
  `.github/workflows/ci.yml` is #344's duration report. It is report only:
  each report step runs after pytest, continues on error under a timeout
  of its own and never decides the job or `ci`, and the job's pytest is
  `make test`'s with a JUnit report added:
  `tests/test_ci_workflow.py::test_the_duration_report_never_decides_the_job_or_the_required_check`,
  `tests/test_ci_workflow.py::test_the_test_job_runs_make_test_with_only_a_junit_report_added`
  and
  `tests/test_ci_workflow.py::test_the_required_check_aggregates_every_other_job`.
  From now on, CI keeps the comparison itself: each run of `test` is set
  against the last 20 clean pushes to main of its category:
  `tests/test_ci_durations.py::test_the_window_holds_only_clean_main_pushes_of_the_category_recent_and_one_per_commit`
  and
  `tests/test_ci_durations.py::test_the_verdicts_follow_the_median_and_p95_thresholds`.
  The policy was held by its page alone;
  `tests/test_suite_performance_audit.py::test_the_documented_regression_policy_is_the_one_the_report_applies`,
  added by this audit, holds it to the tool's constants. Whether the
  hosted jobs reach the provisional target is §4.

## 3. The required invariants

- **I1. No deletion, skipping, xfail-ing, relaxing or deselection of existing non-live tests to meet a speed target.** *Held.*
  Eight commits carry the EPIC's work items and the two fixes its
  measurements relied on, #346 (`1a9424e`) and #396 (`7fa0786`):
  `6fddbc9`, `1a9424e`, `7fa0786`, `b14f959`, `3d836ee`, `acebe52`,
  `cdb9ac0` and `40385a8`. None deletes a test function: every one a diff
  removes, it adds back, and the five #396 removed came back with another
  fixture. None adds a skip, an xfail or a deselection to an existing
  test. The skip and the xfail #396 adds are cases of its own plugin's
  test, and #341's
  `skipif` guards its new pidfd tests, whose fallback runs on every host.
  One assertion changed what it measures: #340 moved the redaction test's
  3-second budget from wall time to CPU time, so workers sharing the host
  cannot fail it, and kept the budget. The suite's options deselect only
  the live tests, and CI adds nothing to them:
  `tests/test_fast_targets.py::test_the_default_suite_deselects_nothing_but_the_live_tests`
  and
  `tests/test_ci_workflow.py::test_the_test_job_runs_make_test_with_only_a_junit_report_added`.
  Of the 1,073 test ids the baseline ran and `main` does not, 319 are
  other parametrizations of functions still there, and every test
  function that left its module was removed or moved by LOCAL's removal or
  replaced by #283's or #349's change of behaviour (§1.3).
- **I2. Preserve the full-suite Linux CPython 3.11 and 3.12 required CI, the independent measured real-backend sandbox job, the distribution checks, lint, format, types, and the aggregate `ci` check with its skip and failure semantics.** *Held.*
  Between `1f6abad` and `d0948fb` the workflow changed once, by #344's
  report-only steps (E6):
  `tests/test_ci_workflow.py::test_matrix_covers_the_declared_python_floor`,
  `tests/test_ci_workflow.py::test_make_check_matrix_runs_pytest_on_every_ci_python`,
  `tests/test_ci_workflow.py::test_make_check_runs_exactly_what_ci_runs`,
  `tests/test_ci_workflow.py::test_the_sandbox_job_measures_the_runner_and_requires_the_backend_where_it_can`,
  `tests/test_ci_workflow.py::test_the_dist_job_checks_the_installed_artifacts_on_every_ci_python`
  and
  `tests/test_ci_workflow.py::test_the_required_check_aggregates_every_other_job`.
- **I3. Preserve independent review, the immutable exact-SHA and candidate-bound ValidationRecord and ReviewCertificate semantics, crash recovery, and the controller's sole authority over merge and verification.** *Held.*
  The EPIC changed production code in four modules: `executor.py` (the
  pidfd exit wait, #341; the last activity fixed at the decision to kill,
  #343), `executor_duplex.py`, `claude_stream.py` and `runlog.py` (#343).
  None of them is the evidence, certificate, state, engine, effects or
  GitHub code, and ADR 0010 amends nothing of ADR 0006. The exit wait keeps
  its old path wherever a pidfd cannot be used:
  `tests/test_executor.py::test_the_exit_wait_opens_no_pidfd_for_a_reaped_child_or_without_a_timeout`,
  `tests/test_executor.py::test_the_exit_wait_raises_the_timeout_proc_wait_raises`
  and
  `tests/test_executor.py::test_without_a_pidfd_the_exit_wait_is_proc_wait`.
  A narrower selection never satisfies the evidence:
  `tests/test_evidence.py::test_complete_evidence_for_a_narrower_set_is_not_evidence_for_the_full_set`
  and
  `tests/test_certificate.py::test_a_certificate_for_a_narrower_set_never_applies_under_the_full_set_and_back`.
- **I4. A partial or test-selected run is never represented as proof of a full suite pass; agents may suggest tests, but cannot downgrade mandatory controller validation.** *Held.*
  ADR 0010 D1 to D4 (E4): an agent's `tests` and its FAST runs stay
  claims, and only the controller's FULL run decides:
  `tests/test_validation_gates.py::test_an_implementers_fast_pass_is_a_claim_that_neither_skips_nor_satisfies_the_set`,
  `tests/test_evidence.py::test_a_narrower_selections_pass_never_stands_for_the_full_definition`
  and
  `tests/test_fast_targets.py::test_without_run_it_prints_the_targets_and_runs_nothing`.
- **I5. Protected `.github/workflows/` merge paths still require the existing human-review process: no approval bypassed, no branch protection edited, no `tests/test_ci_workflow.py` guard weakened, and no `autoforge epic run` for this planning EPIC.** *Held in part.*
  The controller still refuses to merge a PR that touches the workflow,
  and comments cannot satisfy the guards:
  `tests/test_ci_workflow.py::test_the_merge_gate_refuses_to_merge_a_pr_that_edits_this_workflow`
  and
  `tests/test_ci_workflow.py::test_comments_cannot_satisfy_the_guards`.
  #344 changed one guard,
  `tests/test_ci_workflow.py::test_make_check_runs_exactly_what_ci_runs`,
  to accept exactly two additions to the `test` job: the JUnit option on
  `make test`'s command and the report's own command. It pins each of them
  exactly in a new test (E6), and it removed no guard. Approvals, branch
  protection and how the EPIC's issues were run are GitHub's and the
  operator's (§5).
- **I6. No detached or background processes that evade the supervisor or leak children beyond the containment model; no safety gate disabled to speed up tests.** *Held.*
  The one detached process the EPIC met, Git's auto-maintenance after a
  test commit, it removed (E2). #343 changed when a kill's last activity
  is taken, not when anything is killed, and writes no synthetic progress
  into an agent's stream. The executor still kills the whole group:
  `tests/test_providers.py::test_the_controllers_heartbeat_does_not_keep_a_silent_agent_alive`,
  `tests/test_executor.py::test_an_idle_timeout_kills_the_whole_group`
  and
  `tests/test_packaging.py::test_the_scratch_repository_starts_no_detached_maintenance`.
  The judgment call is #341's `fsync` stand-in. In the test process, outside
  the modules that test durable writes, `os.fsync` refuses what
  `fsync(2)` refuses and flushes nothing. The controller's code is
  unchanged and always flushes, and the modules that test what a write
  survives use the real call:
  `tests/test_safefs.py::test_the_fsync_stand_in_refuses_exactly_what_fsync_refuses`
  and
  `tests/test_safefs.py::test_this_module_writes_through_the_real_fsync`.
- **I7. No model-backed or live tests, real provider credentials, GitHub mutations from tests, or paid benchmarking without separate approval.** *Held.*
  The live test stays deselected and skips without its variable, and CI's
  token can only read:
  `tests/test_live_pi_support.py::test_the_live_test_is_deselected_by_default_and_skips_without_its_variable`
  and
  `tests/test_ci_workflow.py::test_workflow_is_granted_no_write_permissions`.
  Two autouse fixtures of `tests/conftest.py` guard two of the
  controller's own routes to GitHub, not the network.
  `_no_network_git_remote` gives an engine built without a local origin a
  GitHub remote whose fetch is recorded and does nothing, and whose push
  fails the test. `_no_network_exact_read` fails a test whose
  `GitHubClient`, built without `https=`, would read `api.github.com`.
  Neither stops a test that starts `gh`, opens a socket or uses another
  client. Otherwise the suite stays off GitHub because its tests use
  `FakeGitHub`, fake `gh` executables and loopback servers instead, as
  `tests/AGENTS.md` requires and review checks, not because anything
  stops them.

  That is what the tests do, not how the EPIC's measurements were run.
  For §1's six runs, the records show it (§1.4): in each, pytest deselected
  the live test, the suite's one model call, and skipped the five opt-in
  probes, and `bench.sh`, which ran them, calls none of `gh`, `git`, `uv`,
  `curl` and `wget`:
  `tests/test_suite_performance_audit.py::test_the_records_say_how_the_runs_were_made`.
  `tests/parallel_bench.py` starts pytest, git and version probes, and
  `tests/ci_durations.py` reads the files it is given; neither imports a
  network client. Of the other measurements, on hosts A and B and on CI's
  runners, this audit has only what their documents say.

## 4. The provisional target

The EPIC's target, each full pytest matrix job below 8 minutes on a
comparable hosted runner, is not one of its exit criteria. The EPIC calls it
an engineering target, to be decided from replicated measurements, not from
a single fast run. Nothing in the repository holds a hosted job's time.

The last hosted time in #338's comments is one run after #345, "an
observation, not a benchmark": 10:23 on 3.11 and 10:17 on 3.12, against
18:21 and 17:05 in run 40242685273. #341's `acebe52` merged after it. How
much it saves on GitHub's runners is not measured here, and §1's host
says nothing about theirs. Since #344, each push to main records the
suite's wall time on each interpreter, and from the fifth clean run on, the
window's median is the replicated measurement the EPIC asks for. The
report times pytest's session, not the job's setup.

So the target's verdict is not this audit's to give. It is the maintainer's
go or no-go, read from those windows.

## 5. What only GitHub holds, and closing the EPIC

- **The work items' states.** The squash commits of PRs #392, #411, #412
  and #407 say they close #340, #341, #342 and #343; #345 was #341's first
  slice. #430's commit, M5's (#344), names no issue it closes. #339 had no
  pull request. Their states, and the maintainer's assessment of M0's
  report, are on GitHub (E1).
- **Native links.** The EPIC's body says its planning connector could
  create neither sub-issue nor `blocked_by` links, and that the table
  records planned dependencies only.
- **The hosted job times** (§4).
- **How the EPIC's issues were run, and their approvals** (I5).

Follow-up work this audit found and did not create:

- Stage 2 of ADR 0010, the controller FAST pre-check, needs its own issue,
  an approved review of D13 and D12's measurement. #338's comment of
  2026-10-31 says no issue for it exists.
- Running CI on workers waits on E2's three preconditions
  ([Development: the suite on several workers](development.md#the-suite-on-several-workers)).
  The change is to a protected path.
- The target's go or no-go (§4), once the duration history holds full
  windows. The same windows tell whether the report's bounds need
  widening.

**Closing the EPIC.** PR #432, which carries this audit, ends its body with
the controller's closing line for #338, as the body of every
implementation PR does (`effects.implementation_closing_block`). A FIX
round reports no body and cannot change it, so merging PR #432 closes the
EPIC. E1 is met only in part, and PR #432's first review asked that the
EPIC not close before E1's baseline is shown completed and reviewed or the
maintainer has accepted what is outstanding. Two things keep the merge from
coming first:

- **The maintainer's decision is recorded before anything of the round is
  pushed.** The operator posts PR #432's note on #339 only if it is the
  maintainer's decision to accept M0's report, with the gaps it stated and
  what E1 lists as closing them, as the EPIC's completed and reviewed
  baseline, and to close the EPIC with PR #432's merge. The controller pushes
  none of the round's commits until it has verified that note. If the
  maintainer means #338 to stay open, for #339 or for the target (§4), the
  note is declined, and the closing line has to leave PR #432's body
  before the PR is merged. Only someone with GitHub access can remove it.
- **The merge is the maintainer's.** With `safety.allow_merge` off, the
  default, a run stops at `READY_FOR_MERGE` and a human merges the PR. The
  controller merges only when the configuration opens the gate and the
  command carries `--allow-merge`
  (`tests/test_config.py::test_safety_gate_opens_from_safety_allow_merge_only`),
  and even then only after its own pre-merge verification.

This audit is evidence for that decision, not the decision.
