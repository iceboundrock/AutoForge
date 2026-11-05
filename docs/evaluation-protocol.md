# Evaluation protocol

**Status:** registered on 2026-11-05 with #318, before any real run. **No real run has been made,**
so this document holds no result. Every number below is a decision taken in advance, except where
it says it was measured.

EPIC #315 proposes Issue Memory and multi-agent execution. Both cost tokens, wall-clock time and
complexity, and published numbers for similar systems say little about a controller that already
has independent review and validation. This protocol fixes, before anyone has seen an outcome, what
would count as those features earning their cost. A real run is a separately approved act and never
part of planning or CI.

## 1. What this holds and what runs it

The numbers live in the files a decision reads, and tests hold this page to them:

| File, under `tests/fixtures/evaluation/` | Holds |
|---|---|
| `manifest.json` | arms, tasks, base and reference commits, hidden tests, each task's prior experience, what is held equal |
| `preregistration.json` | thresholds, margins, sample, statistics, cost ceilings |
| `real_run_config.yaml` | the configuration every real run starts from |
| `fixture_tasks.json` | the fake baseline and the negative fixtures |
| `pin_verification.json` | what was measured of the pins, and on what |

`tests/evaluation.py` scores a run and decides over records. `tests/evaluation_fixture.py` drives
the fake baseline. Neither launches a model, holds a credential or writes to GitHub.

## 2. The comparisons

| Arm | What it is | Status |
|---|---|---|
| `single_agent_no_memory` | existing AutoForge, with its validation and independent review | available |
| `single_agent_issue_memory` | the same, with bounded Issue Working Memory | not evaluated: needs #317, #333 |
| `fixed_role_multi_agent_issue_memory` | deterministic fixed roles, with Issue Memory | not evaluated: needs #319, #333 |
| `adaptive_manager_worker_issue_memory` | adaptive Manager/Worker, with Issue Memory | not evaluated: needs #319, #333 |
| `parallel_read_only_ideation_issue_memory` | optional: fixed roles with parallel read-only ideation | not evaluated: needs #319, #333 |

A missing feature leaves its arm `NOT_EVALUATED` and blocks nothing else. The `requires` of the last
three come from what #318 says the later arms need. Those features' designs were not available when
this was written, so a contrast may gain requirements when they are.

- **`memory_benefit`** compares the first two arms. The question is whether Memory helps where an
  earlier attempt's lesson should matter, without harming where it should not.
- **`multi_agent_benefit`** compares the fixed-role and adaptive arms with the **single agent with
  Memory**. Measuring them against anything weaker would flatter them: arms 1 and 2 keep the
  existing validation and independent review.
- **`memdoor_paired`** is the same set of tasks through single agent, Memory, fixed roles, the
  optional parallel ideation and adaptive recovery, each contrast against the arm it adds to.
  Candidate preservation is no arm: every arm is measured on it, as `lost_success`.

## 3. The tasks

### 3.1 How they were chosen

They are real changes from this repository's own history, so an agent meets the code those changes
met. The rule was applied mechanically to the first-parent history of `main` up to `280702b`:

1. **Eligible:** it changes a `tests/test_*.py` module, and 1 to 120 source lines in at most two
   modules under `src/autoforge/`. It touches no prompt template, workflow, `pyproject.toml`,
   `uv.lock` or `Makefile`, and its subject names no LOCAL mode. That left 23 commits.
2. **Fair:** the test functions the commit added, outside `test_local*`, fail at its parent with
   the commit's test files laid over it, and pass at the commit. A failure that names a missing
   symbol is refused: such a test is coupled to how the fix happened to be spelled, and an
   independent solution could not pass it. That left 16, and refused 7.
3. **Taken:** all 6 fair commits of the memory-dependent definition below, then the 6 newest of the
   other 10. `pin_verification.json` names what was refused and what was left.

### 3.2 The memory classes

A task is **memory-dependent** when its fix turns on how an external program really behaves, or on
a state reached only on a later attempt or phase: what a first attempt would learn by failing. It is
**memory-independent** when the fix can be derived from the repository at its base. Each
memory-dependent task is matched to a control by rank of source lines changed, so the two strata
have similar sizes.

**This classification is the author's judgment, written without #317's or #333's design and before
any run.** It cannot be checked until Memory exists, and it may not change after the first real run.
What tests whether it was right is the result itself: Memory should move verified outcomes on the
dependent stratum and not on its controls. Reported recall or hit rates would not show that.

The class says what a first attempt would learn. Section 3.5 says what a Memory arm already holds
when the attempt starts, since a fresh repository and a fresh issue teach it nothing by themselves.

### 3.3 The tasks

| Task | From | Class | Base | Reference | Control for |
|---|---|---|---|---|---|
| `fix-entry-retarget` | #95 | dependent | `3bd54bc2e7` | `509ebcff7e` | `c6-refusal-names-the-cache` |
| `review-round-refused-by-reread` | #139 | dependent | `de348f4f23` | `f14a25ec94` | `dry-run-prompt-placeholder` |
| `opencode-recovered-errors` | #226 | dependent | `65c7857e0f` | `bae99782d7` | `yaml-subset-errors-name-the-path` |
| `claude-held-back-results` | #203 | dependent | `4fdd45f1e3` | `bb11a69c0c` | `google-api-key-redaction` |
| `paginate-changed-files` | #43 | dependent | `bb71184c88` | `a784ed2f5b` | `redact-the-parsed-result` |
| `no-correction-with-a-survivor` | #209 | dependent | `64cd340a00` | `2d81c2c14f` | `prompt-version-refusal` |
| `c6-refusal-names-the-cache` | #301 | independent | `d46d180a16` | `3cfa4b83e9` | `fix-entry-retarget` |
| `dry-run-prompt-placeholder` | #133 | independent | `9564cefa37` | `2fff4b9dbf` | `review-round-refused-by-reread` |
| `yaml-subset-errors-name-the-path` | #65 | independent | `b403b46f65` | `91c7eed2eb` | `opencode-recovered-errors` |
| `google-api-key-redaction` | #282 | independent | `e6be31ef1a` | `1f6abad5de` | `claude-held-back-results` |
| `redact-the-parsed-result` | #206 | independent | `3155c463b6` | `64cd340a00` | `paginate-changed-files` |
| `prompt-version-refusal` | #302 | independent | `3cfa4b83e9` | `3364a8f196` | `no-correction-with-a-survivor` |

Each task's manifest entry holds the full SHAs, the base's tree, the statement the agent reads, the
hidden test files and functions, the paths a candidate must not change, and its prior experience
(section 3.5).

### 3.4 What keeps them honest

- **No hidden-test leakage.** The hidden tests live in the reference commit. A run happens in a
  disposable repository whose history starts at the base's tree, so the fix and its tests are not
  there to find. The manifest check refuses a statement naming a hidden test or the reference
  commit, and a record counts each prompt or read-context file that names one. Any such file
  invalidates the comparison.
- **No benchmark contamination.** `collect` refuses a run on this repository and one whose base
  commit has another tree than the task pins.
- **No cherry-picking.** The rule ran before any outcome existed. A run that failed stays in the
  denominator, a planned pair with no record leaves the comparison inconclusive, and which arm of a
  pair runs first is a digest of the pair (`plan`), fixed in advance.
- **Tasks and seeds are different things.** A seed is an independent repetition: a fresh repository
  and a fresh session, as providers offer no determinism to rely on. Tasks are the unit of analysis.
- **One record per planned run.** Two records of one arm, task and seed leave the contrast
  `NOT_EVALUATED`, both listed: keeping either would let a re-run replace a failure, or the order of a
  command's arguments choose a score. A planned run is not repeated.
- **Checkers stay what the task pinned.** A candidate's tests are what validation runs, so what they
  decide is only worth what they still are. A protected path may not change at all: conftest, fixtures,
  root pytest configuration, project configuration and workflows. Under `tests/`, a candidate may add a
  test module and add lines to one, and may edit a file its task's reference edited, since a correct
  candidate may have to; any other change to an existing file is `checker_altered`, as is a new
  `conftest.py`. A text rule cannot see a test weakened by an added line or inside a file the reference
  edited, so a run that would otherwise be a success also runs the task's validation on its source with
  the whole of `tests/` as the reference commit holds it. A failure there is `pristine_checkers_failed`,
  and `checker_altered` too when the candidate changed that module, whose own copy had passed.

### 3.5 What a Memory arm starts with

An earlier verified lesson cannot come from the run that needs it. Every task therefore holds one
**prior experience**: a lesson of about 350 characters from a commit at or before its base, with a test
at that commit that verifies it. `plan` prints it for each run of a seeded arm. **An arm with Issue
Memory starts each run with exactly its task's lesson in Memory, put there before the first launch; an
arm without it starts with nothing.** The repository, the issue and the statement are the same on both,
so the lesson reaches an agent only through Memory, and only an arm with Memory holds it.

The match is what makes this a control and not a comparison of two different amounts of Memory. A
memory-dependent task's lesson bears on its fix. Its memory-independent match holds a lesson of the same
form and at most twice the length, equally true and from the same history, that its fix does not need.
So Memory is equally full on both strata, and a gain on the dependent stratum with none on its controls
says the lesson mattered, not that Memory did something whatever it held. A loss on the controls is what
a distracting Memory would cost.

Three checks keep the lessons from being the answer. `check` refuses a lesson naming a hidden test or the
reference commit, and `verify-pins` refuses one whose evidence is later than the base, or which copies a
source line the reference commit added. `verify-pins --run-oracle` runs each lesson's test at its
evidence commit, as a hidden test runs; `pin_verification.json` records that all twelve passed.

A record of a seeded arm carries the digest of the lesson its Memory collector read back, and an unseeded
arm's carries none. Anything else leaves the contrast `NOT_EVALUATED`: another Memory is another
intervention. **That collector is owed by #317 and #333**, whose design this was written without, so how
a lesson is entered into Memory cannot be stated here; if the design has no way to hold it unchanged, that
is an amendment before the first comparison.

A lesson is the author's summary of its evidence, not a transcript of a real attempt: these commits are
the closest thing this repository holds to earlier verified experience on the same code. Whether a lesson
is enough to change an outcome is what the comparison measures, and a statement that already says what a
lesson says lowers that effect on every run alike.

## 4. What is held equal, and what is measured

### 4.1 Held equal

Within a pair: repository task, base commit, models and providers, sandbox policy, tools, validation
commands, review and resource limits. `tests/evaluation.py` digests the whole configuration except
where this host keeps things. A key added under any existing section, such as `validation.fast_commands`
if ADR 0010's stage 2 (D11 to D13) lands, is therefore held equal the day it exists, and a new
top-level section is refused until someone classifies it. That digest is of the file `collect` is given
when it scores a run; section 4.2 says how little of it a run's own records can back. Both arms of a pair
run on one host, one controller commit and the same provider CLI versions, within 24 hours of each other.

Why Claude Code on every phase: it is the one provider whose run log reports a cost today. The price
is that review is not cross-vendor, on every arm alike.

Each task's validation is the suite without the modules that already fail at its base (`plan` prints
the command). A module the base fails would fail every candidate of every arm, so no run could
succeed. The pinned lists were measured on one host, under `disabled` and on pytest workers, and
**a real run's host must measure them again under its own configuration** (section 9). What a list
leaves out is not validated on that task: a regression only that module would catch is left to
review, on every arm alike.

### 4.2 Measured

| Measure | Where it comes from | Today |
|---|---|---|
| `verified_success` | a certificate that still applies and `PASS` records under the configured policy for the exact final candidate, the oracle's pass on it and the pristine checkers' on its source, with no checker altered, no stale certificate and no leak | measured |
| `lost_success` | an earlier candidate passed the oracle and the final one did not | measured |
| `false_oracle` | the certified candidate fails the oracle | measured |
| `checker_altered` | the candidate altered a checker (section 3.4) | measured |
| `stale_completed_nodes` | what was certified is not what the PR holds | measured |
| `review_fix_cycles` | accepted `FIX` launches | measured |
| `repeated_failed_approaches` | a candidate whose tree already failed validation in this run | measured, a proxy |
| `duplicate_exploration` | read-only tool calls repeated across a run's launches, from `progress.log` | a proxy; `unknown` where a provider shows no tool, or a launch's progress file is missing, unreadable or a link |
| `human_blocked_interventions` | applied `unblock`s, and a run that ended `BLOCKED` | measured |
| `wall_clock_seconds` | the state file's creation to its last write | measured |
| `cost_usd`, `cost_per_verified_success` | `cost_micro_usd` of every agent launch, failed attempts and parallel ones included | measured on Claude Code with every launch on record; otherwise `unknown` with the part it knows shown apart |
| `context_tokens` | no provider's run log reports them (#154) | `unknown` |
| crash-recovery correctness | the deterministic scenario of section 8, on each arm | fixture |
| stale-memory leakage, Memory overhead (storage, projection size, latency) | that arm's collector, when it exists | not evaluated |

A run also has one outcome. `verified_success` and `unverified_completion` (with its reasons) are the
completed ones; `blocked`, `failed`, `timeout`, `incomplete`, `cost_ceiling` and `wall_clock_ceiling`
stay in the denominator as runs that did not succeed. `infra_failure`, `credentials_unavailable`,
`service_unavailable` and `skipped` remove the pair, listed with its stated reason. Timeouts, costs,
token uncertainty and a missing external service each stay visible, and none is folded into another.

**A record states what its run's own files show.** The controller records each run's routing and limits
(`config_fingerprint`), its sandbox mode (`sandbox_pin`), its prompt version and, per launch, its provider,
model and effort and whether it was enforced. A run whose records contradict the configuration it is scored
under, or that changed either record while it ran, is refused by `collect`; a run missing one is written
with its `settings_evidence` `unknown`. **No run records the rest of the configuration,** such as
`workflow.max_review_rounds` or a profile's `options`, and those can change without the recorded routing
changing. The recorded `config_file` does not stand in for them: it is a pathname, and a file can change at
the same pathname. Another pathname refuses; the same one says nothing of what the file held when the run
began. So a record lists each setting no record holds as `unrecorded`, and its `settings_evidence` is
`unknown` while any is left (section 6). A validation record counts only under the configured policy, and a
certificate counts only if it still applies to the run, by the check the merge gate itself makes (ADR 0006
D12.3); each speaks for its candidate, not for the configuration.

**The run log's completeness is part of its evidence.** The controller publishes a step's directory before
its journal line, so a directory no line numbers is a lost launch. A run log that is lost, unreadable or
malformed, or that has lost a launch, leaves every count it decides `unknown` rather than short, its cost
`unknown` with the known part apart, and its run's settings `unknown`. A line counts only if it is this
run's, under a step number of its own (an integer above the last, naming a published step), with every field
a score reads of the type the controller writes. Nothing is read of any other line, so another run's log
gives no measured cost and a malformed line crashes nothing. A file shown to an agent that cannot be read
leaves a leak not ruled out, so that run is not verified.

**Validation has its own status:** `not_configured`, `absent`, `failed` or `passed`, read from the
`ValidationRecord`s of the configured set for the exact commit. A run under an empty set is
`not_configured`, which is no pass and no failure: such a run cannot be verified, whatever else it
shows. A FAST run produces no record and no `passed` (ADR 0010 D1, D2), and nothing here reads one.
An agent's claim, a log's prose and an issue's text are not read either.

**Provenance is carried with every usage value:** `measured`, `estimated`, `synthetic` or `unknown`.
Only `measured` can pass a criterion. An estimate is shown apart and never summed with a measurement.

## 5. The pre-registered decision

**Sample:** 12 tasks (6 of each class), 3 seeds, so 36 pairs per contrast. The stopping rule is the
plan's end or a ceiling: nobody looks at outcomes to stop early for success or futility, and a sample
is never resized. A ceiling reached leaves the comparison incomplete and therefore inconclusive.

**Statistic:** the mean per-task difference, treatment minus baseline, each task's seeds averaged
first; the 95% interval is a bootstrap of 10,000 resamples of tasks (seed 318). More seeds of the
same per-task results do not narrow it, because they are not more independent tasks.

| Comparison | Primary | Non-regression | Cost ceiling |
|---|---|---|---|
| `memory_benefit` | `verified_success` on `memory_dependent`, at least +0.15, interval above 0 | the table below | $1,800 |
| `multi_agent_benefit` | `verified_success` on `all`, at least +0.15, interval above 0 | as below, with `context_tokens` and `cost_per_verified_success` at +100%, `wall_clock_seconds` at +50% | $4,500 |
| `memdoor_paired` | `verified_success` on `all`, at least +0.15, interval above 0 | `lost_success`, `false_oracle`, `checker_altered`, `stale_completed_nodes` (0.00), `wall_clock_seconds` (+50%), `cost_per_verified_success` (+100%) | $7,200 |

`memory_benefit`'s margins. A count's margin is an absolute difference; a percentage is relative to
the baseline's mean:

| Metric | Stratum | Margin |
|---|---|---|
| `verified_success` | memory_independent | 0.10 |
| `lost_success` | all | 0.05 |
| `false_oracle` | all | 0.05 |
| `checker_altered` | all | 0.00 |
| `review_fix_cycles` | all | 0.50 |
| `repeated_failed_approaches` | all | 0.25 |
| `duplicate_exploration` | all | +25% |
| `human_blocked_interventions` | all | 0.10 |
| `context_tokens` | all | +25% |
| `wall_clock_seconds` | all | +25% |
| `cost_per_verified_success` | all | +25% |

`cost_per_verified_success` is judged on its point estimate, since a resample with no success has no
value to bound. Each comparison has four hard gates on the treatment arm, each of which must be
measured clean: `safety_failures`, `stale_memory_leaks` and `crash_recovery_failures` at zero, and
`memory_overhead` under its ceilings on every run: 1,048,576 bytes of storage per issue, 16,384
characters of projection per launch and 1,000 ms of latency per launch. Those ceilings were set
without #317's or #333's design, and any tighter bound that design sets also applies.

A gate reads every planned treatment run that has a record, paired or not: an exclusion or a lost baseline
takes a run out of the statistics, not out of what was seen. A measured breach fails its gate whatever else
is unknown, on that run or another. A clean gate needs every run that left evidence to be clean, excluded
ones too, so that excluding a run cannot excuse its check. A planned run that never started has nothing to
check. The crash gate reads its fixture report instead.

No listed safety failure is not the same as a clean gate. It passes only for runs an operator says they
checked, with `--safety-checked` naming where, whose run log shows every launch: no write outside the
disposable repository, no credential in a launch, nothing merged. Anything else is `unknown`, and a pull
request the state counts as merged is a failure whatever anyone says.

| Per-run ceiling | |
|---|---|
| `single_agent_no_memory` | $25 |
| `single_agent_issue_memory` | $25 |
| `fixed_role_multi_agent_issue_memory` | $50 |
| `adaptive_manager_worker_issue_memory` | $50 |
| `parallel_read_only_ideation_issue_memory` | $50 |

A comparison's ceiling is each planned run at its arm's ceiling. These are caps on what may be spent,
not estimates of what a run costs: **no real run has measured one.** The wall-clock ceiling is 8 hours
per run. AutoForge enforces neither during a run: its only limits are per launch
(`execution.default_max_runtime_seconds`). So the operator checks the cost between runs and stops a
run past 8 hours, and records it with `--stopped-by cost_ceiling` or `--stopped-by wall_clock_ceiling`,
with its state directory when it has one: what the run did and spent stays in the record. A planned run
never started for a ceiling has no state directory, so nothing is known of its settings or its usage. Nor
does a run measured past a ceiling need anyone to say so: it counts as having reached it.

**The decision for each contrast:**

- `NOT_EVALUATED`: an arm is missing, no real run exists, no planned pair has both runs and no gate
  failed, or the comparison broke its own terms (a record under another manifest or pre-registration, two
  records of one planned run, a pair with unequal settings, a run its own records show under other
  settings, a run that did not start with its task's prior experience, a hidden test shown to an agent).
- `NO_GO`: a hard gate failed, or an interval lies wholly past a non-regression margin, on any sample,
  a sample with no pair included; or, on a complete sample, the primary interval lies wholly below +0.15.
- `GO`: the primary is shown, every non-regression bound holds, every gate is measured clean, and no
  more than 10% of a contrast's pairs were excluded.
- `INCONCLUSIVE`: anything else, which includes every criterion left `unknown`.

They are decided in this order. Harm comes first, so a large benefit never outweighs it, and a missing
pair never excuses it: what was seen to go wrong does not wait for the rest of the plan. Then an
incomplete sample is `INCONCLUSIVE`: a planned pair with no record, exclusions past the bound, a run at a
ceiling, a run's settings unknown or a run log incomplete. Only after that is the primary read, because a
primary shortfall on part of the plan would let whichever runs came first decide the question.

**What this sample can resolve.** Each task contributes one mean, so 12 tasks give wide intervals, and
6 on the primary stratum give wider. The variance of a real task is unknown until runs exist, so this
page claims no power figure. An effect that is real and small will read `INCONCLUSIVE`, by design.

## 6. Why a `GO` is not possible yet

These hold today and are stated so nobody discovers them after spending money:

- **Tokens are `unknown`, so the `context_tokens` criterion cannot pass until #154 lands.** The Claude
  stream reducer deliberately keeps no nested `usage`. Until then the best any real comparison can
  reach is `INCONCLUSIVE`.
- **Most settings held equal are `unrecorded`, so every real run's `settings_evidence` is `unknown`**
  (section 4.2). A run records its routing, limits, sandbox mode and prompt version and nothing else this
  protocol holds equal. Lifting this means the controller recording those settings when it creates a run,
  a change to the state file that this protocol does not make and that belongs on #23 first. Until then the
  best any real comparison can reach is `INCONCLUSIVE`, whatever else it measures.
- **Arm 2 does not exist,** so `memory_benefit` is `NOT_EVALUATED` until #317 and #333 land. The
  no-memory baseline alone can be smoke-tested.
- **The crash gate needs a fixture of the treatment arm.** The scenario of section 8 covers arm 1
  only; an arm owes its own when it lands.

## 7. Deterministic control-plane tests

Offline, with fakes, in `make check`. Each item names tests that hold it for arm 1 today, and what a
later arm owes. These are harness and controller correctness, never evidence of model quality.

- **Replay consistency.** *Held for arm 1.* `tests/test_evaluation_fixture.py::test_the_baseline_is_byte_identical_across_two_runs`,
  `tests/test_engine.py::test_fix_replay_revalidates_observed_follow_ups_before_the_push`.
  A later arm owes the same for its own journal and its Memory projection.
- **Task-state transition correctness.** *Held for arm 1.* `tests/test_transitions.py::test_every_decision_is_a_legal_edge`,
  `tests/test_integration.py::test_full_loop_to_ready_for_merge`. A later arm owes this for each inner task's states.
- **Duplicate-execution prevention.** *Held for arm 1.* `tests/test_lock_domains.py::test_l2_is_non_blocking_and_a_held_lock_is_issue_busy`,
  `tests/test_engine.py::test_fix_duplicate_invocation_after_the_effects_writes_nothing_again`.
  A later arm owes this per inner task.
- **Duplicate external-effect prevention.** *Held for arm 1.* `tests/test_evaluation_fixture.py::test_a_crash_after_a_landed_effect_repeats_nothing_and_recovers`,
  `tests/test_engine.py::test_fix_crash_window_resumes_from_the_journal_and_writes_each_effect_once`,
  `tests/test_engine.py::test_merge_recovery_after_crash_counts_once`. Every arm's writes stay the controller's typed effects (ADR 0004).
- **Budget recovery after a crash.** *Held for arm 1.* `tests/test_engine.py::test_step_budget_is_cumulative_and_survives_resume`.
  A later arm owes this for any budget it adds, parallel attempts included.
- **Interrupted-launch reconciliation.** *Held for arm 1.* `tests/test_validation_evidence.py::test_an_interrupted_validator_leaves_its_marker_and_the_next_step_records_it`,
  `tests/test_engine.py::test_recovery_after_crash_with_persisted_pr`. A later arm owes this per worker launch.
- **Candidate preservation.** *Held for arm 1.* `tests/test_validation_gates.py::test_the_same_candidate_after_a_crash_reuses_its_record`,
  `tests/test_evaluation_fixture.py::test_a_correct_candidate_overwritten_after_advice_is_a_lost_success`.
  The second one reports the loss rather than preventing it: arm 1 has no mechanism that keeps an earlier candidate, and an adaptive arm that claims one owes the test that it does.
- **Provider capability mismatch.** *Held for arm 1.* `tests/test_interaction.py::test_prompt_without_a_live_capability_fails_and_nothing_else_degrades`,
  `tests/test_interaction.py::test_a_pause_needs_the_kinds_live_capability_and_a_way_back_to_the_session`. A later arm owes this for each role's provider.
- **Stale-context denial.** *Held for arm 1.* `tests/test_engine.py::test_review_base_changed_during_the_review_is_stale`,
  `tests/test_evaluation_fixture.py::test_a_stale_completed_node_is_no_success_after_its_input_changed`.
  A Memory arm owes the denial of an entry whose inputs moved.
- **Agent isolation.** *Held for arm 1.* `tests/test_child_env.py::test_the_child_environment_carries_no_operator_credential_route`,
  `tests/test_evaluation_fixture.py::test_the_oracle_gives_a_launch_the_controllers_isolation`. A later arm owes this per worker.
- **Unknown-outcome handling.** *Held for arm 1.* `tests/test_effects.py::test_push_with_an_unknown_outcome_that_did_not_land_is_reissued_once`,
  `tests/test_engine.py::test_the_attempt_bound_after_gh_pr_merge_says_the_outcome_is_unknown`,
  `tests/test_engine.py::test_recovery_ambiguous_blocks`. A later arm owes this for each new effect kind.

## 8. Fixture mode

`make eval-fixture` runs the no-memory baseline through the unchanged engine with scripted agents, the
in-memory `FakeGitHub` and a bare local origin. Every commit is made at a fixed date, so each base and
candidate has a pinned SHA and two runs write a byte-identical report (`--out DIR`). It takes seconds,
calls no model and reads no credential.

It checks, on every run: the base is the pinned commit; the single-agent path ran; only the
controller's typed effects were written; nothing merged and the run stopped at the closed gate; no
launch held the operator's credential, though one was in the environment; each `ValidationRecord`
names its commit's real tree; the certificate names the reviewed head. **A fixture's success says
nothing about any model,** and its report says so first.

Negative fixtures, each scored for what it is:

| Fixture | What the agent did | Scored as |
|---|---|---|
| `invented_expected_answer` | wrote a test agreeing with its own wrong output; validation and review pass | `false_oracle` |
| `correct_candidate_overwritten` | a right first candidate, replaced after review's advice | `lost_success`, `false_oracle` |
| `altered_checker` | right code, and changed a fixture a test reads | `checker_altered`, though the oracle passes |
| `weakened_existing_test` | right code, and removed an assertion from a test module its task leaves alone | `checker_altered`, though the oracle passes |
| `skip_hides_a_regression` | a regression in visible behaviour, and one added line skipping the test of it | `checker_altered`, `pristine_checkers_failed`, though validation and the oracle pass |
| `stale_completed_node` | a clean review, then a human's push changed the input | `stale_completed_nodes`, not verified |
| `hidden_test_leak` | the issue names the hidden check | `hidden_leaks`, which invalidates a comparison |
| `no_validation_configured` | right code, an empty set, a claim and an injection string in the issue | `not_configured`, not verified |
| `crash_after_the_pr_landed` | the process dies after the PR create lands | recovers: one PR, no second agent |

A Manager does not exist yet, so `correct_candidate_overwritten` uses a review finding as the advice,
and `stale_completed_node` uses a review round as the node. Those arms owe the same fixtures on their
own objects.

**Owed by a Memory arm, not evaluated until #317 and #333:** obsolete memory, current memory,
conflicting memories, corrected memory, volatile GitHub state, scope boundary, negative debugging
lesson, hallucinated lesson candidate, prompt-injection string, incomplete index watermark. The
retrieval metrics (Recall@K, MRR) and offline-distillation costs belong to EPIC #331's optional
evaluation, not to this one.

## 9. Real runs

A real run calls a real model and spends real money, so it is **separately approved**, recorded where
`--approval` can point to it, and **never in CI**: no workflow names this harness or its opt-in, and a
test holds that. No run happens as part of planning.

**The smoke run** proves the procedure, not a result. It is one run of arm 1 on
`c6-refusal-names-the-cache` with seed 0, which no comparison pairs, at $25 at most. Its record says
what happened and enters no decision. It is the first real use of `real_run_config.yaml`, so expect
it to find problems. The same steps make every later run:

1. **Measure the host.** On the run host, with the operator's copy of the configuration:
   `uv run python -m tests.evaluation verify-pins --git . --run-oracle --base-suite --reference-suite --config FILE`.
   Every task must fail and pass as pinned, its lesson's test must pass at its evidence commit, and its
   validation must pass at its base and at its reference. A difference is an amendment (section 11), made
   before any run.
2. **Make a disposable repository,** private, never this one, whose first commit is the task's base:
   ```bash
   git -C "$CLONE" archive "$BASE_SHA" | tar -x -C "$DISPOSABLE"
   git -C "$DISPOSABLE" init -q -b main && git -C "$DISPOSABLE" add -A
   git -C "$DISPOSABLE" commit -q -m "Base"
   git -C "$DISPOSABLE" rev-parse 'HEAD^{tree}'   # must equal the task's base_tree
   ```
   Open an EPIC and one sub-issue there, whose body is the task's `statement`, word for word. Keep the
   clone holding the reference commit (the oracle store) outside every path the configuration shows.
   A seeded arm's Memory takes the `prior_experience` that `plan` prints for the run, and nothing else,
   before the first launch (section 3.5).
3. **Configure** from `real_run_config.yaml`, adding only what is the host's (`state_dir`,
   `execution.worktree_dir`, `github.command`, `sandbox.read_only_paths`), and setting
   `validation.commands` to what `uv run python -m tests.evaluation plan --comparison memory_benefit`
   prints for the task.
4. **Run,** with the ordinary command, never `--allow-merge`: `autoforge --config FILE doctor`, then
   `autoforge --config FILE run --epic EPIC_URL --issue ISSUE_URL`. A `BLOCKED` run ends there: no
   `unblock`, because a human's help would be part of the arm's score.
5. **Score it,** read only:
   ```bash
   AUTOFORGE_EVAL_REAL=1 uv run python -m tests.evaluation collect \
     --task TASK --arm single_agent_no_memory --seed 0 --approval APPROVAL_URL \
     --state-dir STATE_DIR --repo DISPOSABLE --base-commit BASE_COMMIT --live-head PR_HEAD \
     --config FILE --oracle-repo CLONE --safety-checked WHERE --out RECORD.json
   ```
   `collect` refuses a run on this repository, a base of another tree, another configuration, a run its
   own records show under other settings, or no sandbox: the hidden tests run the candidate's code, and
   only as `validation.commands` would. `--safety-checked` names where the operator recorded section 5's
   check, and `--safety-failure` says what it found. A run that never reached a verdict is scored the
   same way with `--stopped-by` and `--reason`, without the run's flags only if it left nothing to read.
   A skipped or refused run is a record, never a pass.
6. **Report,** over every record of the plan:
   `uv run python -m tests.evaluation report RECORD.json ... --harness-report FIXTURE_REPORT.json`.
   It prints every run with its outcome, reasons and where each usage value came from; each criterion
   with its interval; what was excluded, missing or stopped; and every task with its seeds' spread and
   outcomes. Add `--json` for the same as data. Post it on #318 in full, failures included.

The cost is the provider's bill for one agent run per candidate plus review rounds, and each
candidate's validation takes as long as the suite does (ADR 0010 records about eight to sixteen
minutes serially on the hosts it measured). The pristine suite adds one such run to `collect`, on the
final candidate of a run that would otherwise be a success. No run of this procedure has been timed or
priced.

## 10. What these results may not be used for

Neither of these is evidence for AutoForge, and no report may cite them as an expectation:

- Memdoor: 35 vs 47 on 100 problems with thinking off, and 49/50 parity on ten problems over five passes, are two different experiments; neither proves a gain on a repository
- Databricks: 22.3%, 68.4% and the token savings reported are from an internal business-agent benchmark; they are no expected AutoForge performance

Nor does a result here generalize far. Twelve tasks from one repository, with its tests and its habits,
say something about AutoForge on code like this. Those tasks are public, so a model may have seen their
fixes; that affects every arm alike but may raise every arm's rate.

**Known threats to validity**, decided on and left open:

- A hidden test may assert wording or structure a correct independent solution would not produce. The
  fair rule catches only a missing name. Each statement gives the wording its tests assert, as read when
  writing it, and the smoke run and the first results should be read for a task nobody can pass. Such a
  task lowers every arm's rate, and an amendment before the first comparison may replace it.
- The pristine suite holds a candidate to every test the reference commit has, and the fair rule checked
  only the functions it added. One it changed may assert how the reference did it, so a correct
  candidate can read `pristine_checkers_failed`, on every arm alike. Its failing modules are in the
  record to read for that.
- An edit that weakens a test in a file the reference edited, and hides no failure, is not flagged: only
  what it hid is. Nothing is decided on that test, since the pristine suite runs its own copy.
- The pristine suite is one more run of the whole suite, so a flaky test costs a verified success, on
  every arm alike. On the one measured pass, a loaded host failed one module that passed alone
  (`pin_verification.json`). No retry was registered, since a retry is a choice made after an outcome;
  a run host that does not hold its load steady will see this more.
- `repeated_failed_approaches` compares trees, so the same idea rewritten counts as new, and
  `duplicate_exploration` does not see a `Bash` `cat`. Both undercount.
- The pins were verified on one interpreter, Python 3.14.10, which is not the CI matrix's.
- A lesson is the author's reading of an earlier commit, written before any run (section 3.5). A bad
  lesson lowers the Memory arm's rate on its stratum, and only an amendment before the first comparison
  may change it.
- Validation runs only what `validation_ignore` leaves (section 4.1), and the oracle runs only the
  functions the reference commit added, so a regression elsewhere is left to review.

## 11. Amendments

An amendment changes the files of section 1 and these digests in one reviewed change, with its reason,
**and states whether any real run had been made.** A record carries the digests it was made under, and a
report refuses to decide over records of another, so criteria cannot move under results.

- `manifest.json`: `5ef8a4228a0bb56327427f2635ea0a469bdd4d0ddfd4e372258dc216486a5924`
- `preregistration.json`: `5f1dfe4ec7c39b9df2c0b8b7a5d5b996708190b1b4a399926cbc8136a08cfb5b`
- `real_run_config.yaml`: `6270e03cd6760c28136151af502ef88824c9c37c96f1cf81a14882a7d884b4a0`

| Date | Change | Why | Real runs before it |
|---|---|---|---|
