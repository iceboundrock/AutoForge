# Review evaluation protocol

**Status:** registered on 2026-11-24 with #355 (REV-R0 of EPIC #352), before any real run. **No
real run has been made, and none is part of this contract.** Every number below is either a
decision taken in advance or the scripted value of a fixture, and says which.

AutoForge judges a review today by what follows it: a clean round, a certificate, a merge. None of
those says whether the reviewer found the defects that were there, raised ones that were not, or
sent the fixer after a change that broke something else. EPIC #352 proposes changes to review, and
the issues that build on this one (REV-R1, REV-R2 and REV-R5) need a way to tell a better review
from a worse one that does not rest on the reviewer's own account. This protocol fixes, before
anyone has seen an outcome, how a finding is labeled, by whom and against which commit, which
numbers are computed from the labels, how uncertain they are allowed to be, and what would count as
a change to review earning its cost.

It is the review-level companion of [the evaluation protocol](evaluation-protocol.md) (#318), which
measures whole runs. It takes that protocol's provenance rule and its rule that a fixture decides
nothing, and changes no controller behaviour: no state field, prompt, phase, gate or default moves
(section 12).

## 1. What this holds and what runs it

| File, under `tests/fixtures/review_evaluation/` | Holds |
|---|---|
| `preregistration.json` | the taxonomy, the severity weights, the uncertainty method, the decision rule, and the baseline facts of section 2 |
| `corpus.json` | the cases: pinned commits, the ground-truth defects each holds and the oracles that show them |
| `fixture_runs.json` | one scripted run of today's review on each case, with an adjudicator's label for every finding and fix |
| `fixture_report.json` | what `score` writes for those runs, byte for byte |

`tests/review_evaluation.py` reads them:

```bash
uv run python -m tests.review_evaluation check    # refuse what the contract does not allow
uv run python -m tests.review_evaluation verify   # replay the commits and the oracles
uv run python -m tests.review_evaluation score [--out FILE]
uv run python -m tests.review_evaluation decide --baseline ARM --treatment ARM
```

It launches no agent, holds no credential, contacts no network and writes nothing to GitHub. `verify`
builds each run's commits in a temporary repository with git's configuration, hooks, credential
helpers and transports switched off, at the corpus's fixed date, and runs each oracle in a separate
interpreter (`python -I`) that sees only the locale variables.

## 2. Review today: the baseline

What follows is what the code on `main` does at registration. `preregistration.json` keeps the
same facts under `baseline`, and
`tests/test_review_evaluation.py::test_the_baseline_facts_are_the_codes` fails when the code moves
away from them: a change to any of them is a change to the baseline every comparison is made
against, and is recorded in section 13.

- **Routing.** Round 1 runs under profile `review_round_1`, rounds 2 to 5 under `review_round_2_5`
  and every later round under `review_round_6_plus` (`src/autoforge/profiles.py`). Each round is a
  launch of its own, bound by the controller to one PR head, base and merge base.
- **What a reviewer reports.** A `REVIEW` result: `needs_fix_round`, at most 50 findings, and six
  prose sections. A finding has an id `R<round>-F<n>`, a classification (`blocked`, `non-blocked`
  or `nit`), a title, a location and a required resolution. There is no confidence field (the
  parser drops one a reviewer adds), no severity apart from that classification and no risk score.
- **What is kept.** `review_history` holds one entry per round: its HEAD, its result (`needs_fix`,
  `clean` or `stale`), its finding count, digests of its required resolutions and, per finding, only
  the id, classification and required resolution; title and location are dropped by design
  (`src/autoforge/loop_guard.py`). The whole result is in the round's `control-result.json` in the
  run log, and the published comment on GitHub.
- **FIX outcomes.** One resolution per finding: `fixed`, `follow_up_created`,
  `no_change_with_rationale`, `acceptance_action`, `operator_acceptance` or `pr_body_update`. The
  state keeps only the last FIX round's resolutions; each FIX step's `control-result.json` keeps its
  own. The controller verifies the commits and follow-up issues a resolution names, not that a
  `fixed` finding is fixed: the kind is the fixer's claim.
- **Loops.** A PR gets at most `workflow.max_review_rounds` rounds (default 20). Two rounds with
  the same required resolutions, or three with the same count while a resolution recurs, are
  stagnant (`stagnation_identical_rounds` 2, `stagnation_unchanged_count_rounds` 3): before round 12
  the run stops `BLOCKED`; from round 12 the controller may replace the implementation instead, and
  findings at round 20 force a replan (`review.replan`). A stale round counts against the cap.
- **Risk.** No structured risk exists. The classification is the reviewer's own word and the
  `assessment` is free text; neither is read below.
- **Cost, tokens, wall clock.** A launch's run-log `execution.json` carries its start and end and,
  for Claude Code only, the provider summary's `cost_micro_usd`. No provider's run log reports
  context tokens (#154).

| Baseline value | Today |
|---|---|
| any rate of section 6 for today's review | `unknown`: no review of a real run has been labeled |
| cost, tokens and wall clock of a review round | `unknown`: no real run was made for this protocol |
| review rounds and FIX rounds per PR | `unknown` for real runs; the fixture's are scripted (section 9) |

Turning a real run into a run record means reading its state file and run log (each round's
`control-result.json`, each launch's `execution.json`) and joining an adjudicator's labels to them.
That collector does not exist yet and is not part of this contract; until it does, no real run can
be scored.

## 3. What a review certificate does and does not mean here

A `ReviewCertificate` (ADR 0006 D1.2) records that for one candidate, in one round, bound to the PR,
HEAD, base and merge base, the controller accepted a clean review result, presented exactly one
validation set to it with every record `PASS` for that candidate, and verified the published
comment. It is not a GitHub approval, a CI result, permission to merge, proof that the code is
correct, or an authorization.

So a certificate is an outcome of the review under test, never a label of it. A candidate the
controller would certify can hold a registered defect: that is exactly what
`escaped_critical_defects` counts. The fixture shows it twice: the clean round of
`redact-authorization-header` and that of `retry-attempt-bound` are certifiable, and each candidate
still holds a defect (B1, C2).

## 4. The labeling taxonomy

### 4.1 Units

- A **case** is a pinned base and candidate, and usually a reference commit holding a known-good
  change. A `defect` case holds at least one ground-truth defect; a `clean_negative` holds none.
- A **run** is one arm's review of one case: its rounds, its fixes and their commits, its launches.
  An arm has at most one run per case; a planned run is not repeated, so a second one is refused
  rather than allowed to replace the first.
- A **finding** is one entry of a round's `REVIEW` result, held to the controller's own parser.
- A **defect** is ground truth: a defect the change introduced, at a commit, shown by evidence.
- A **fix** is a FIX round. One that made a commit is labeled; one that made none has nothing to
  harm.

### 4.2 Defects

Each defect records its `defect_class`, its `severity`, its `location`, the commit it was
`introduced_at`, the commits it is `fixed_at`, when and by what it was discovered (`seeded`,
`review`, `oracle`, `adjudicator`, `post_merge`), its evidence and its adjudicator.

| Severity | Weight | Meaning |
|---|---|---|
| `critical` | 8 | a safety or security failure, data loss, or a broken controller invariant |
| `major` | 4 | wrong behaviour a user or the controller depends on |
| `minor` | 2 | a real defect with a small blast radius: a missing test, a wrong document |
| `trivial` | 1 | cosmetic, but real |

Defect classes: `correctness`, `specification` (an acceptance criterion unmet), `security_safety`,
`recovery` (crash, retry, idempotency), `test_coverage`, `standards` and `documentation`.

A defect is one the change introduced. A defect at the base is not the change's, so it cannot be
registered against it, and a finding about one is `false` or, when the issue leaves its scope open,
`unknown` (the fixture's `type-hint-cache-key`). A defect at a case's commits belongs to the case,
so every arm is scored against the same ground truth: one an arm's reviewer was the first to find is
added to the case and counts for every arm. A defect at a commit only one run made, a fixer's
regression, belongs to that run. A defect is present at a commit when that commit descends from
where it was introduced and from none of the commits that fix it.

### 4.3 Labels

Every finding has exactly one label; a finding nobody decided is labeled `unknown`, never left out.
A label names the finding and its round, the full SHA of the candidate that round reviewed, its
verdict, its adjudicator and when it was made.

- `confirmed`: the finding describes a registered defect present at the reviewed candidate, named
  by the label. It cannot predate the defect's discovery.
- `false`: it describes no defect of this change at that candidate. The label says why.
- `unknown`: the adjudicator could neither confirm nor refute it. The label says why.

A fix that made a commit is `harmful` exactly when a registered defect was introduced at that commit,
`not_harmful` when none was, and `unknown` only when none was registered but the adjudicator could
not rule one out. A fix of a `false` finding is not harmful for that alone; it is harmful when it
broke something.

Two confirmed findings of one round that name the same defect are duplicates: the first counts and
each later one is a duplicate. That is derived from the labels and never labeled.

**The discovery cutoff** (`discovery_cutoff` in `corpus.json`) freezes the ground truth. A label
must be made by then. A defect discovered later is listed with its run as a late defect and counted
nowhere, so a later discovery cannot move a registered score; only an amendment that moves the
cutoff counts it (`test_review_evaluation.py::test_a_late_defect_counts_only_once_an_amendment_moves_the_cutoff`).

**Adjudicators** are humans: `fixture_author`, `maintainer` or `human_adjudicator`. A
`reviewer`, `fixer`, `agent`, `model` or `self` is refused wherever an adjudicator appears: on a
defect, a label or a fix. A real adjudicator labels a finding without being told which arm raised
it; the scorer cannot check that, so the record of who labeled what is part of the evidence.

### 4.4 What is never a label

Nothing an agent says of its own work is a label, and no gate reads it:

- the reviewer's classification, its prose (`verification` included) and any confidence it
  states, in its prose or as a field of its result, which the controller's parser drops;
- the fixer's resolution kinds and rationales;
- the certificate, a passing validation set or a green CI run (section 3).

A round's `needs_fix_round` is read only for what it decides, which round the controller would
certify: that is the outcome under test, not a judgment of the code. A label carrying `confidence`,
`self_reported`, `reviewer_confidence`, `agent_verdict` or `classification` is refused, and every
case, defect, label and fix is held to its registered fields, so no such value can ride along under
another name. The report prints the classifications and
resolution kinds as claims, apart from every metric, and
`test_review_evaluation.py::test_what_the_agents_say_of_their_own_work_moves_no_score` rewrites all
of them, with a claim of certainty, and finds every metric unchanged.

## 5. Evidence and its provenance

A defect's evidence is one of two kinds:

- **`oracle`:** a check that fails with an `AssertionError` where the defect is present and passes
  where it is not. `verify` runs it at the commit that introduced the defect (fail), at every commit
  that fixes it and every other commit its evidence names (pass), and at every reviewed commit
  descending from the introduction (fail or pass as the ground truth says). An oracle that exits any
  other way, an import error included, shows nothing and refuses the corpus.
- **`adjudication`:** a named human's written rationale. A missing test, a wrong document or a
  standards defect rests on this alone.

A run's `provenance` is `fixture` or `real`. A fixture run is scripted and decides nothing,
whatever it looks like. A launch's usage values carry #318's provenance: `measured`, `estimated`,
`synthetic` or `unknown`. A fixture launch may not say `measured`, only a `measured` total can pass a
criterion, and an `unknown` launch value makes the total `unknown`, with the known part shown apart.

## 6. Metric formulas

Every rate is pooled over an arm's runs, the sum of their numerators over the sum of their
denominators, not a mean of per-case rates. Only registered defects, those discovered by the cutoff,
count. Excluded runs (an infrastructure failure, with its reason) are listed and left out.

- **`weighted_recall`** = Σ w(d) over detected d in E / Σ w(d) over d in E, where E is every
  registered defect present at a commit the run reviewed, w is the severity weight, and d is
  detected when a `confirmed` label names it. Its upper label bound also counts every undetected d
  in E present at a commit whose round raised an `unknown` finding.
- **`precision`** = C / (C + F), with C `confirmed` and F `false` findings; its label bounds are
  C / (C + F + U) and (C + U) / (C + F + U), with U `unknown`.
- **`clean_negative_fpr`** = FP / (FP + TN) over clean-negative runs, judged on the findings of
  rounds that reviewed the case's own commits: TN raised none, FP raised at least one `false`, and a
  run whose findings there are all `unknown` is in neither, but widens the bounds to FP / N and
  (FP + U) / N.
- **`escaped_critical_defects`**: registered `critical` defects present at the commit of a run's
  final round when that round was clean, which is the candidate the controller would certify. The
  report lists every escape, of any severity.
- **`harmful_fix_rate`** = H / (H + N) over fixes that made a commit, with `unknown` fixes widening
  the bounds as for precision.
- **`duplicate_ratio`** = duplicate confirmed findings (section 4.3) / C.
- **Validated success**: the run's final round is clean, its validation `passed`, and no registered
  `critical` or `major` defect escaped.
- **`cost_per_validated_success`** = the cost of every launch of every run (analysis, review, fix,
  failed attempts included) / validated successes. It has no value without a success.
- **`launch_wall_clock_seconds`** and **`context_tokens`**: sums over launches, with provenance. The
  first leaves out the controller's time between launches; a run's end-to-end time is #318's
  measure.

## 7. Uncertainty

Two kinds are reported and neither is folded into the other.

- **Label uncertainty** is the band between a rate's bounds: every `unknown` label put each way. A
  rate whose band is wider than 0.1 (`max_label_uncertainty`) is `unknown`: settle the labels
  before reading it.
- **Sampling uncertainty** is a 95% percentile interval from a bootstrap that resamples cases,
  10,000 resamples with seed 355, ranked by nearest rank. A resample with an empty denominator has no
  value and is counted, not filled. A comparison resamples the same cases for both arms and takes
  the treatment minus the baseline on each.

## 8. The pre-registered decision

A decision compares a `treatment` arm with a `baseline` arm, normally today's review, over the same
cases. Its sample needs at least **20 defect cases and 10 clean negatives** paired on both arms.

| Metric | Criterion (treatment minus baseline) |
|---|---|
| `weighted_recall` | at least +0.1, its paired interval above 0 (the primary) |
| `precision` | not worse than -0.05 |
| `clean_negative_fpr` | not worse than +0.05 |
| `harmful_fix_rate` | not worse than +0.05 |
| `duplicate_ratio` | not worse than +0.1 |
| `cost_per_validated_success` | at most 0.5 (+50%) above the baseline's, on point estimates, measured on both arms |

**Hard gate:** a registered critical defect that escapes the treatment's review and did not escape
the baseline's is a `NO_GO`, whatever else is shown. The gate reads every treatment run that is not
excluded, paired or not.

A later issue that needs another primary from section 6 says so in an amendment before its first
real run; otherwise this one applies.

Decided in this order:

1. `NOT_EVALUATED`: an arm has no run, or either arm holds a fixture run.
2. `NO_GO` on measured harm: the hard gate, or a margin whose whole paired interval lies past it, or
   the cost criterion broken. Harm is decided on whatever pairs exist, so a missing pair never
   excuses it.
3. `INCONCLUSIVE` when the sample is incomplete: a case with no pair of runs, more than 10% of the
   cases excluded, or fewer cases than the minimum.
4. `NO_GO` when the primary's interval lies wholly below +0.1.
5. `INCONCLUSIVE` while anything is open: a metric `unknown`, an interval reaching past a margin,
   a cost not measured on both arms, or a primary not shown.
6. `GO`.

The minimum is a floor, not a power calculation. The variance of a real review is unknown until
labeled runs exist, so this page claims no power, and a small real effect will read
`INCONCLUSIVE`.

## 9. The fixture

Six cases of a few lines each, written for this protocol; none is from this repository's history.
They exercise every verdict, both case kinds, a duplicate, a late defect, a harmful fix, an escaped
critical defect and a run stopped by stagnation.

| Case | Kind | Ground truth | The scripted review |
|---|---|---|---|
| `page-count-partial-page` | defect | A1 floors a page count (major, oracle); A2 tests no partial page (minor); A3 divides by zero, discovered after the cutoff | finds A1 twice and A2; the fix is right; round 2 is clean |
| `redact-authorization-header` | defect | B1 leaves the token of `Authorization: Bearer` visible (critical, oracle); B2 a README typo (minor) | finds B2, one `false` and one `unknown` finding; misses B1, which escapes |
| `retry-attempt-bound` | defect | C1 calls once too often (major, oracle) | finds C1; the fix swallows the last error (C2, major, oracle), which round 2 misses |
| `rename-slug-pattern` | clean negative | none | clean |
| `document-empty-chunks` | clean negative | none | one `false` finding, then clean |
| `type-hint-cache-key` | clean negative | none | the same `unknown` finding twice; stopped by stagnation |

Their scores, which `test_review_evaluation.py::test_the_fixture_scores_are_the_ones_the_protocol_states`
holds, check the scorer and say nothing about any reviewer, model or configuration:

| Metric | Value | Label band | Status |
|---|---|---|---|
| `weighted_recall` | 0.5 | 0.5 to 0.833333 | `unknown` |
| `precision` | 0.714286 | 0.5 to 0.8 | `unknown` |
| `clean_negative_fpr` | 0.5 | 0.333333 to 0.666667 | `unknown` |
| `harmful_fix_rate` | 0.333333 | none | known |
| `duplicate_ratio` | 0.2 | none | known |

One critical defect escaped (B1), and three runs are validated successes, at a synthetic $11.00 in
all, $3.67 per success. `decide` on these runs is `NOT_EVALUATED`.

The fixture is replayable: `verify` rebuilds every commit from `corpus.json` and `fixture_runs.json`
at their pinned SHAs and runs every oracle where the ground truth says it fails or passes, and
`score` writes `fixture_report.json` byte for byte
(`test_review_evaluation.py::test_the_corpus_replays_as_pinned`,
`test_review_evaluation.py::test_the_fixture_report_is_byte_identical_and_pinned`).

## 10. Tests that hold this

All in `tests/test_review_evaluation.py`, offline, in `make check`: 52 tests, which took 7.9 seconds
on one shared, loaded host.

- The contract's files: `test_the_registered_files_pass_the_check`,
  `test_the_corpus_replays_as_pinned`, `test_a_commit_that_does_not_build_to_its_pin_is_reported`,
  `test_an_oracle_that_does_not_show_its_defect_is_reported`,
  `test_an_oracle_that_crashes_shows_no_defect`,
  `test_an_oracle_runs_isolated_from_the_hosts_environment`.
- No agent's word is a label: `test_what_the_agents_say_of_their_own_work_moves_no_score`,
  `test_a_label_carrying_an_agents_confidence_or_verdict_is_refused`,
  `test_an_agent_is_no_adjudicator`, `test_an_agent_adjudicating_a_defect_or_a_fix_is_refused`,
  `test_the_scorer_holds_no_engine_github_client_or_provider`.
- What the check refuses: `test_every_finding_has_exactly_one_label`,
  `test_a_label_is_bound_to_the_candidate_it_judged`, `test_labels_are_frozen_at_the_discovery_cutoff`,
  `test_a_confirmed_label_names_a_defect_its_candidate_holds`, `test_a_clean_negative_holds_no_defect`,
  `test_a_defect_is_one_the_change_introduced`,
  `test_a_harmful_fix_is_exactly_one_that_introduced_a_defect`,
  `test_a_review_result_the_controller_would_refuse_is_refused`,
  `test_a_fixture_run_may_not_say_it_measured_anything`, `test_a_planned_run_is_not_repeated`.
- The decision, on synthetic real arms of 33 copied cases:
  `test_fixture_runs_reach_no_decision_however_good_they_look`,
  `test_go_needs_the_primary_effect_every_bound_and_the_registered_sample`,
  `test_a_new_critical_escape_is_no_go_however_large_the_benefit`,
  `test_a_margin_worse_on_its_whole_interval_is_no_go`, `test_no_effect_is_no_go_on_a_complete_sample`,
  `test_unknown_labels_rule_out_go`,
  `test_a_sample_below_the_registered_minimum_reaches_no_primary_conclusion`,
  `test_a_case_with_no_pair_or_too_many_exclusions_is_inconclusive_but_harm_still_decides`,
  `test_a_cost_no_launch_measured_rules_out_go`.
- This page and the code: `test_the_baseline_facts_are_the_codes`,
  `test_the_protocol_registers_the_digests_of_its_files`,
  `test_the_protocol_names_every_case_metric_and_registered_value`,
  `test_the_protocol_states_the_fixture_scores`, `test_no_workflow_or_package_module_runs_this_harness`.

## 11. Limitations

Decided on and left open:

- **No real run can be scored yet.** The collector of section 2 is owed; the protocol registers what
  it must produce, not the code that produces it.
- **Matching a finding to a defect is judgment.** An oracle shows that a defect is there, not that a
  finding describes it. A real evaluation's labels are as good as its adjudicators, and the fixture's
  are its author's.
- **Blinding is a procedure, not a check.** The scorer cannot tell whether an adjudicator knew the
  arm.
- **Pooled ground truth is shared.** A defect only the treatment found enlarges the baseline's
  denominator too. That is the point of pooling, but it means a single arm's recall is relative to
  what all arms found, not to every defect the code holds.
- **The weights are judgments.** 8, 4, 2, 1 make one critical miss outweigh a correct major and minor
  together; another choice would rank some arms differently. They change only by amendment.
- **`unknown` is strict.** A band wider than 0.1 leaves a rate `unknown` and the comparison
  `INCONCLUSIVE`. Real reviews may raise many findings nobody can decide; that cost is preferred to a
  decision made on guesses.
- **The fixture is small and synthetic.** Its intervals are wide and its cases far simpler than
  this repository's changes. It shows the scorer counts correctly, nothing more.

## 12. Coordination

- **#318:** this protocol takes its usage provenance, its refusal to let a fixture decide and its
  amendment discipline. Its outcomes are runs (`verified_success`, `lost_success`), these are
  findings; a later evaluation may report both on the same runs.
- **#170 to #175 (ADR 0006):** the certificate keeps its meaning (section 3). Nothing here issues,
  reads or relaxes one.
- **#338:** the new tests took 7.9 seconds on one host (section 10); `verify` runs about a hundred
  git commands and twelve oracle processes.
- **#23:** no state protocol, ADR or controller change is made, so no migration or crash-replay
  coverage is owed. Controller authority, exact-candidate validation, reviewer independence, the
  certificate and merge gate, and GitHub effect authorization are untouched, and default behaviour
  is unchanged.

## 13. Amendments

An amendment changes the registered files and these digests in one reviewed change, with its
reason, and states whether any real run had been made before it. A change to the code that moves a
baseline fact of section 2 is an amendment too.

- `preregistration.json`: `9245a89ce21d2a21a9571b0f6d34b49984e6349b14d4fd772f9e415521348cf0`
- `corpus.json`: `3e94841c82a0f4b6ed734915c4e002d29aefefd64bd2f44f475e5b1da4a5da63`

| Date | Change | Why | Real runs before it |
|---|---|---|---|
| None yet | | | |
