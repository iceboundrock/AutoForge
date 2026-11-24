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
uv run python -m tests.review_evaluation verify --pins-only       # the commits alone, any files
uv run python -m tests.review_evaluation verify --trust-oracles   # other files' oracles too
uv run python -m tests.review_evaluation score [--out FILE]
uv run python -m tests.review_evaluation decide --baseline ARM --treatment ARM
```

It launches no agent, holds no credential and writes nothing to GitHub. `verify` builds every case's
commits, whether or not a run holds the case, and then each run's own commits, in temporary
repositories with git's configuration, hooks, credential helpers and transports switched off, at
the corpus's fixed date; building a commit runs nothing the files hold. It holds each commit to its
pinned SHA, and its tree to the files that built it, byte for byte: git leaves out a file the tree
ignores and can rewrite one an attribute names, so a SHA pins what git committed, not the files.
It then runs each oracle in a separate interpreter (`python -I`) that sees only the locale
variables, on the tree of the commit it runs at as the controller's byte-exact materializer writes
it and proves it hashes back to the commit's tree (ADR 0006 D6.2, D6.3), never on the files. Two
inputs that build the same commit therefore give its oracles the same bytes.

**An oracle is code, and nothing confines it.** It is Python from `corpus.json` or a run file, and
it imports the case's own code. `python -I` and the reduced environment keep the host's variables,
user site directory and working directory out of it; they enforce no boundary. The oracle runs as
the caller, reads and writes what the caller can and reaches whatever network the caller can. So
`verify` runs the oracles of the registered files alone, the bytes of this checkout's `corpus.json`
and `fixture_runs.json`, reviewed with the repository. Any other file is refused unless the caller
passes `--trust-oracles`, which says the caller accepts running that code as themselves, or
`--pins-only`, which replays its commits and pins and runs nothing it holds. `check`, `score` and
`decide` never execute anything a file holds.

## 2. Review today: the baseline

What follows is what the code on `main` does at registration. `preregistration.json` keeps the
same facts under `baseline`, and
`tests/test_review_evaluation.py::test_the_baseline_facts_are_the_codes` and
`tests/test_review_evaluation.py::test_the_baseline_cost_facts_are_the_codes` fail when the code
moves away from them: a change to any of them is a change to the baseline every comparison is made
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
- **Loops.** A PR gets at most `workflow.max_review_rounds` rounds (default 20); a round at the cap
  that still has findings stops the run `BLOCKED` instead of starting a FIX. Two rounds with the
  same required resolutions, or three with the same count while a resolution recurs, are stagnant
  (`stagnation_identical_rounds` 2, `stagnation_unchanged_count_rounds` 3), and a stagnant round
  stops the run `BLOCKED` unless the replan policy routes it first. A stale round counts against
  the cap.
- **Replan.** After every round with findings, and before those loop bounds, the controller applies
  `review.replan` (`src/autoforge/replan.py`, enabled by default). Below round 12
  (`soft_threshold`) nothing triggers a replan. From round 12 a replan is triggered by stagnation,
  or by 3 trailing rounds (`stagnation_window`) each with findings and at most 2 of them
  (`max_findings_per_round`); findings at round 20 (`hard_threshold`) trigger one whatever else
  holds. A trigger is not a replacement. While any recorded round's findings are truncated the run
  stops `BLOCKED` (`replan_evidence_truncated`), and so it does once the issue has had 2 completed
  replans (`max_replans_per_issue`, `replan_limit_exceeded`); only otherwise does it enter
  `REPLAN_REEXECUTE`. With the policy disabled a round's findings meet the loop bounds alone.
- **Risk.** No structured risk exists. The classification is the reviewer's own word and the
  `assessment` is free text; neither is read below.
- **Cost, tokens, wall clock.** A launch's run-log `execution.json` carries its start and end and
  the provider's summary. That summary holds `cost_micro_usd`, when the provider's result reports a
  cost, for two providers, which both read their reply through the Claude stream reducer
  (`src/autoforge/claude_stream.py`): Claude Code (`claude`) with its default `stream-json` output,
  and the Claude Agent SDK provider (`claude-sdk`), whose value through the real helper
  `test_claude_sdk.py::test_a_completed_session_through_the_real_helper` holds. Claude Code with
  `output_format: text`, OpenCode, the OpenCode SDK provider and Pi record no cost. No provider's
  run log reports context tokens (#154).

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
controller certified can hold a registered defect: that is exactly what `escaped_critical_defects`
counts. The fixture shows it twice: the clean round of `redact-authorization-header` and that of
`retry-attempt-bound` are certified, and each candidate still holds a defect (B1, C2).

Whether the controller certified a round's candidate is its own record of the certificate it
issued, never the reviewer's reply and never inferred from what issuance needs. The controller
records a round in `review_history` before it issues the round's certificate, and on an
inconsistent input (no binding, a binding of another round, a record that is not `PASS`) it blocks
with none, keeping the certificate of an earlier round if it held one (ADR 0006 D12.2). So a
`clean` history entry over a passing validation of the candidate is what a certificate needs, not
evidence that one was issued. Each round of a run record carries the controller's record, as the
collector of section 2 reads it from the run's state:

- **`disposition`**: what the controller recorded for the round in `review_history`, `clean`,
  `needs_fix` or `stale` (the revision moved during the review), or `unrecorded` when no record of
  it was found.
- **`validation`**: the validation set the controller presented to the round: `passed` (one record
  per definition, every one `PASS`), `empty_set` (the deliberately empty set: no validation command
  is configured), `failed` (a record that is not `PASS`), or `unrecorded`.
- **`validated_sha`**: the candidate those records are bound to, `null` only when `unrecorded`.
- **`certificate`**: `issued` when the controller issued a certificate for this round, `not_issued`
  when its record shows it issued none (its state held no certificate of this round when the
  round's step ended), or `unrecorded` when no record of it was found.
- **`certified_as`**: the `review_round` and `reviewed_head_sha` the issued certificate names, and
  `null` unless it is `issued`.

The round's candidate is **certified** only when its `certificate` is `issued` and names this round
and the SHA this round reviewed. `not_issued` **rules a certificate out**, and so does what the
controller never certifies: a reply that needs a fix, a disposition `needs_fix` or `stale`, a
`failed` set or another candidate's records. Otherwise no record of the certificate was found, and
whether the candidate was certified is **unknown**, whatever else the round's record says. A clean
reply alone is never a certificate, and nor is a clean round. The check refuses a record the
controller would never write: `clean` over a reply that needs a fix or `needs_fix` over a clean one,
a certificate `issued` over a round that is not `clean` or a set that is not `passed` or
`empty_set` of the reviewed candidate (D9.4, D5.5, D12.2), and one that names another round or
revision: this round did not get it. A `clean` round over a failed set, or over another candidate's,
is kept: it is a round the controller blocked on with no certificate.

The candidate a run **ended on** is its final round's only when the controller's record shows the
run stopped right after that round (section 5). A certificate is the outcome of its round, not of
the run: a certified run goes back to `REVIEW` when its revision moves after the clean review
(`src/autoforge/transitions.py`), and the controller certifies another candidate or none. So where
the record shows no such stop, whether the run ended on a certified candidate, and which candidate
it ended on, are unknown, whatever its final round's certificate says.

## 4. The labeling taxonomy

### 4.1 Units

- A **case** is a pinned base and candidate, and usually a reference commit holding a known-good
  change. A `defect` case holds at least one registered defect present at its candidate; a
  `clean_negative` holds none. A case whose only defects were discovered after the cutoff, or lie at
  another commit than the candidate, gives a review of the candidate nothing to find: it is refused
  as a defect case, so it never counts toward the minimum sample (section 8).
- A **run** is one arm's review of one case: its rounds, its fixes and their commits, its launches.
  A REVIEW or FIX launch names the round it served, which must be a round of the run (for FIX, one
  that needed a fix); an ANALYZE_EXECUTE launch precedes every round and names none. Every launch
  names the controller's launch ordinal it spent, and the run carries the controller's record of
  where those ordinals start and end (section 5).
  An arm has at most one run per case; a planned run is not repeated, so a second one is refused
  rather than allowed to replace the first. A run record that does not end on a certified round says
  why it ends there. A run an infrastructure failure stopped before its first review result is
  recorded as it happened, with its launches, no round and its exclusion reason: no review is
  invented for it, and it is an excluded run, not a case without one (section 6). A record with no
  round that is not excluded is refused. A stale round ends a run's record: the controller fixes no
  stale round and reviews the moved revision next, which is no commit a run record holds. It does
  not end the run, so unless the controller's record shows the run stopped there, the run's outcome
  and usage are unknown (section 5).
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
regression, belongs to that run: it is that run's alone, whatever its id, and never the same defect
as another run's of the same id. A defect is present at a commit when that commit descends from
where it was introduced and from none of the commits that fix it.

A commit that fixes a defect, a `fixed_at` entry of the defect or a run commit naming it in its
`fixes`, strictly descends from the commit it was introduced at and finds it at its parent. A fix
at or before the introduction, on another line, or after an earlier fix of its line is refused: it
would leave the defect present nowhere, its introduction included, and so erase the failure its
oracle must show there.

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
defect, a label or a fix. The scorer holds both lists itself, not only `preregistration.json`: a
preregistration may narrow the human roles, and one that allows any other role, or refuses fewer
than these five, is refused, and every adjudicator is held to the scorer's lists whatever a
preregistration says (`test_review_evaluation.py::test_no_preregistration_makes_an_agent_an_adjudicator`).
That an adjudicator declared human is one is the record's word, which the scorer cannot check. A
real adjudicator labels a finding without being told which arm raised it; the scorer cannot check
that either, so the record of who labeled what is part of the evidence.

### 4.4 What is never a label

Nothing an agent says of its own work is a label, and no gate reads it:

- the reviewer's classification, its prose (`verification` included) and any confidence it
  states, in its prose or as a field of its result, which the controller's parser drops;
- the fixer's resolution kinds and rationales;
- the certificate, a passing validation set or a green CI run (section 3).

A round's `needs_fix_round` is read only beside the controller's record of the round, and only to
rule a certificate out: a reply that needs a fix is never certified (section 3). Whether a
candidate was certified is the outcome under test, not a judgment of the code, and a clean reply
alone decides nothing. A label
carrying `confidence`, `self_reported`, `reviewer_confidence`, `agent_verdict` or `classification`
is refused, and every case, defect, label and fix is held to its registered fields, so no such
value can ride along under another name. The report prints the classifications, whether the final
reply was clean and the resolution kinds as claims, apart from every metric, and
`test_review_evaluation.py::test_what_the_agents_say_of_their_own_work_moves_no_score` rewrites the
classifications, prose and resolution kinds, with a claim of certainty, and finds every metric
unchanged.

## 5. Evidence and its provenance

A defect's evidence is one of two kinds:

- **`oracle`:** a check that fails with an `AssertionError` where the defect is present and passes
  where it is not. `verify` runs it at the commit that introduced the defect (fail), at every commit
  that fixes it and every other commit its evidence names (pass), and at every reviewed commit
  descending from the introduction (fail or pass as the ground truth says), each time on that
  commit's tree (section 1). An oracle that exits any other way, an import error included, shows
  nothing and refuses the corpus. Evidence that names a commit holding the defect as one the oracle
  passes at contradicts the failure there and is refused, never settled as a pass.
- **`adjudication`:** a named human's written rationale. A missing test, a wrong document or a
  standards defect rests on this alone.

A run's `provenance` is `fixture` or `real`. A fixture run is scripted and decides nothing,
whatever it looks like. A launch's usage values carry #318's provenance: `measured`, `estimated`,
`synthetic` or `unknown`. A fixture launch may not say `measured`, only a `measured` total can pass a
criterion, and an `unknown` launch value makes the total `unknown`, with the known part shown apart.

Which launches a run made is the controller's record, not its launch list. The controller spends
one launch ordinal (`launch_seq` in the run's state file) in the save before every launch, a
relaunch and a failed attempt included. That counter is a high-water mark: it gives the last
ordinal a run spent, never the first. A run record carries the controller's `launch_window`, read
from the run's state file and run log (`null` when it was not found):

- **`start`**: `run_created` when the controller created the run for the case's issue. A new run's
  state holds `launch_seq` 0, so its first launch spent ordinal 1. Otherwise `unrecorded`: a run
  that took up the case's issue after another one's, or whose creation was not found, has no known
  first ordinal.
- **`end`**: the stop the evaluation ended the run at, after which it launched nothing more for the
  case's issue: `READY_FOR_MERGE`, `BLOCKED` or `FAILED`; otherwise `unrecorded`. `DONE` is not one
  of them: it follows a merge, and an evaluation merges nothing.
- **`last`**: the state's `launch_seq` at that end, the last ordinal the run spent; `null` when the
  end is `unrecorded`.

Each launch names the ordinal it spent; an ordinal named twice, or past `last`, is refused. A run's
launch record is **complete** when its window starts at the run's creation and ends at a recorded
stop, and it holds a launch of every ordinal from 1 to `last`, an ANALYZE_EXECUTE launch, a REVIEW
launch of every round and a FIX launch of every fix. A record short of any of these is incomplete:
one that covers every phase and round but lost a failed attempt is short of that attempt's
ordinal, the first one included. A launch nobody recorded is not a launch that cost nothing, so one
incomplete run makes every usage total of its arm `unknown`, with the known part shown apart and
the runs named, and the report lists what each run's record misses.

The window also says whether a run ended where its record does, and only the window does: a record
listing no later round is no evidence that none followed. The run **ended at its record's final
round** when its window ends at a recorded stop and every ordinal after that round's last REVIEW
launch, up to `last`, is held by a FIX launch of that round: nothing the record does not hold came
after it. When the controller spent an ordinal past every launch the record holds, the run **went on
past its record**. Anything else leaves open whether it did: no window, an `unrecorded` end, no
REVIEW launch of the final round, or an ordinal after it that is missing or spent on another launch,
any of which could be a later round on a moved revision. A stale final round is no exception.
Unless the run ended at its record's final round, that round is not where its outcome was decided:
whether it ended on a certified candidate, which defects escaped and whether it is a validated
success are all unknown, whatever that round holds (sections 3 and 6). A record whose run stopped on
a round the controller certified nothing for, or on no round, and says it ended at
`READY_FOR_MERGE`, is refused: the controller enters `READY_FOR_MERGE` only with the completed
round's certificate.

## 6. Metric formulas

Every rate is pooled over an arm's runs, the sum of their numerators over the sum of their
denominators, not a mean of per-case rates. Only registered defects, those discovered by the cutoff,
count. Excluded runs (an infrastructure failure, with its reason, one stopped before its first
review result included) are listed with their reasons and left out. The report shows every arm that
holds a run, one whose every run is excluded too, and a case an arm excluded is not among the cases
it has no run of; the decision counts it in the excluded share (section 8).

- **`weighted_recall`** = Σ w(d) over detected d in E / Σ w(d) over d in E, where E is every
  registered defect present at a commit the run reviewed, w is the severity weight, and d is
  detected when a `confirmed` label names it. Its upper label bound also counts the heaviest set of
  undetected d in E the run's `unknown` findings could name, one defect per finding, each present
  at the commit its finding's round reviewed (a maximum-weight matching).
- **`precision`** = C / (C + F), with C `confirmed` and F `false` findings; its label bounds are
  C / (C + F + U) and (C + U) / (C + F + U), with U `unknown`.
- **`clean_negative_fpr`** = FP / (FP + TN) over clean-negative runs, judged on the findings of
  rounds that reviewed the case's own commits: TN raised none, FP raised at least one `false`, and a
  run whose findings there are all `unknown` is in neither, but widens the bounds to FP / N and
  (FP + U) / N.
- **`escaped_critical_defects`**: registered `critical` defects present at the candidate a run
  ended on when the controller certified it (section 3). A clean reply the controller did not
  certify lets nothing escape. Where its record does not settle that (no record of the final round's
  certificate, or no record that the run ended at its final round, section 5), the defects at the
  final round's candidate are listed as possible escapes and the count is `unknown`, its known part
  shown apart. A run whose record does not show where it ended leaves the count `unknown` even when
  its final round holds no critical defect: the candidate it ended on is another. The report lists
  every escape, of any severity.
- **`harmful_fix_rate`** = H / (H + N) over fixes that made a commit, with `unknown` fixes widening
  the bounds as for precision.
- **`duplicate_ratio`** = D / C, with D the duplicate confirmed findings (section 4.3). An `unknown`
  finding could be a duplicate or a first report of another defect, but only as the check would
  accept its label: `false`, or `confirmed` of a registered defect present at its round's commit. An
  unknown finding of a round whose commit holds no registered defect, every round of a clean
  negative's own commits included, is therefore `false` in both bounds. The lower is D / (C + R),
  with R, in each round, the unknown findings up to the number of its registered defects no finding
  of the round names: each of those a first report, the rest false. The upper is the highest ratio
  a settlement allows: an unknown finding of a round with a confirmed one could be a duplicate of
  it, and a round of k ≥ 2 unknown findings, no confirmed one and a registered defect at its commit
  could hold k - 1 duplicates of k confirmed findings; the scorer takes the first kind always (each
  raises the ratio, which is below 1) and rounds of the second, the largest first, while each
  raises it. Every other unknown finding is put false.
- Recall and the duplicate ratio count defects by identity, which only the frozen corpus gives, so
  their bounds settle an unknown finding against it. Precision, the clean-negative false-positive
  rate and the harmful-fix rate ask whether a finding or a fix is false or harmful, not which defect
  it is: their bounds put each `unknown` both ways, whether or not a registered defect could name
  it. A finding about a defect at the base in a scope the issue leaves open (section 4.2) would not
  be false and names nothing the corpus can hold. The band is the wider for it, never narrower than
  a settlement allows.
- **Validated success**: the controller certified the candidate the run ended on over a validation
  set that `passed` (not the empty set), and no registered `critical` or `major` defect is at it. It
  is `unknown` where the controller's record does not settle that and would decide it, and always
  for a run whose record does not show it ended at its final round: neither that round's defects nor
  its validation set are the candidate's it ended on, so they rule a success neither in nor out.
- **`cost_per_validated_success`** = the cost of every launch of every run (analysis, review, fix,
  failed attempts included) / validated successes. It has no value without a success, none while a
  run's success is `unknown`, and none while a run's launch record is incomplete (section 5).
- **`launch_wall_clock_seconds`** and **`context_tokens`**: sums over launches, with provenance,
  `unknown` while a run's launch record is incomplete. The first leaves out the controller's time
  between launches; a run's end-to-end time is #318's measure.

## 7. Uncertainty

Two kinds are reported and neither is folded into the other.

- **Label uncertainty** is the band between a rate's bounds: every `unknown` label put each way. A
  rate whose band is wider than 0.1 (`max_label_uncertainty`) is `unknown`: settle the labels
  before reading it.
- **Sampling uncertainty** is a 95% percentile interval from a bootstrap that resamples cases,
  10,000 resamples with seed 355, ranked by nearest rank: of the n resamples with a value, the lower
  end is the one of one-based rank ⌈0.025 n⌉ and the upper the one of rank ⌈0.975 n⌉, computed in
  exact arithmetic, so ranks 1 and 39 of 40. A resample with an empty denominator has no value and
  is counted, not filled. A comparison resamples the same cases for both arms and takes the
  treatment minus the baseline on each, as an exact ratio.

Every comparison with a registered threshold is exact and reads unrounded values: a label band
against `max_label_uncertainty`, an interval end or a point estimate against a margin or the
primary's effect, the cost per validated success against its margin, the excluded cases' share of
the planned ones against `max_excluded_case_share`. Counts and weights are integers, each rate and
share is the ratio it is, and a launch's cost and a registered value are the decimals their file
wrote: the scorer reads every JSON number with a fraction or an exponent as that decimal, digit for
digit, never as the binary float nearest it, which can round it onto a limit
(`test_review_evaluation.py::test_a_file_is_decided_on_the_decimals_it_wrote`,
`test_review_evaluation.py::test_an_excluded_share_at_its_limit_is_decided_exactly`). A report
rounds a value to six decimals for display, and no decision reads a rounded one.

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
| `cost_per_validated_success` | at most 0.5 (+50%) above the baseline's, on unrounded point estimates, measured on both arms |

**Hard gate:** a registered critical defect that escapes the treatment's review and did not escape
the baseline's is a `NO_GO`, whatever else is shown. The gate reads every treatment run that is not
excluded, paired or not. It compares defects by identity, not by id: a case's defect is the same
defect in both arms' runs of the case, and a defect a treatment run introduced is that run's alone
(section 4.2), so the baseline never held it and its critical escape is always a new one, even when
a baseline run's regression of the same id escaped too, and even when the baseline has no run of
the case. An escape is at a certified candidate (section 3): a possible escape, where either arm's
record does not settle the outcome, decides neither `NO_GO` nor `GO` and leaves the comparison open.
So an escape of a case's defect is new only against a baseline run of the case whose record settles
that it did not escape there: where the baseline has no run of the case, only an excluded one, or
one whose record does not show where it ended, whether it escaped the baseline's review is unknown,
and the treatment's escape leaves the comparison open.

A later issue that needs another primary from section 6 says so in an amendment before its first
real run; otherwise this one applies.

Every criterion is required. The check refuses a `preregistration.json` short of any of them, or
holding one of the wrong type or out of range: the primary, a higher-is-better rate, with an effect
in (0, 1]; a margin of every other metric of the table, each in its registered direction and in
[0, 1] (the cost margin any finite number from 0); the hard gate; the minimum sample, at least one
case of each kind; the excluded share and `max_label_uncertainty`, each in [0, 1]. A criterion
left out is never read as waived: only an amendment changes one, by registering another value
(section 13).

Decided in this order:

1. `NOT_EVALUATED`: an arm has no run, or either arm holds a fixture run.
2. `NO_GO` on measured harm: the hard gate, or a margin whose whole paired interval lies past it, or
   the cost criterion broken. Harm is decided on whatever pairs exist, so a missing pair never
   excuses it.
3. `INCONCLUSIVE` when the sample is incomplete: a case with no pair of runs, more than 10% of the
   cases excluded on either arm, or fewer cases than the minimum.
4. `NO_GO` when the primary's interval lies wholly below +0.1.
5. `INCONCLUSIVE` while anything is open: a metric `unknown`, an interval reaching past a margin,
   a cost not measured on both arms (an incomplete launch record included, a run that went on past
   its record among them), a possible critical escape, a critical escape whose novelty the baseline
   does not settle, a treatment run whose record does not show where it ended, or a primary not
   shown.
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
| `duplicate_ratio` | 0.2 | 0.166667 to 0.333333 | `unknown` |

B's `unknown` finding sits in a round beside its confirmed B2 and could be a second report of it or
the first of B1, which is what widens the duplicate ratio's band; `type-hint-cache-key`'s are on a
clean negative, where no label can confirm anything, and move neither bound. One critical defect
escaped (B1), and three runs are validated successes, at a synthetic $11.00 in all, $3.67 per
success. Every round carries the controller's record, its certificate included, and five runs end
on a round it issued a certificate for. Every run's launch window starts at the run's creation and
ends at its stop, `READY_FOR_MERGE` or, for the stagnant run, `BLOCKED`, right after its final
round's REVIEW launch, so every launch record is complete and every run ended at its record's final
round. `decide` on these runs is `NOT_EVALUATED`.

The fixture is replayable: `verify` rebuilds every commit from `corpus.json` and `fixture_runs.json`
at their pinned SHAs and runs every oracle where the ground truth says it fails or passes, and
`score` writes `fixture_report.json` byte for byte
(`test_review_evaluation.py::test_the_corpus_replays_as_pinned`,
`test_review_evaluation.py::test_the_fixture_report_is_byte_identical_and_pinned`).

## 10. Tests that hold this

All in `tests/test_review_evaluation.py`, offline, in `make check`: 121 tests, which took 49 to 50
seconds run serially on one shared host at a load average of 1 to 1.5 on 8 cores, and 16 seconds on
4 workers.

- The contract's files: `test_the_registered_files_pass_the_check`,
  `test_the_corpus_replays_as_pinned`,
  `test_verify_replays_every_case_whether_or_not_a_run_holds_it`,
  `test_a_commit_that_does_not_build_to_its_pin_is_reported`,
  `test_an_oracle_that_does_not_show_its_defect_is_reported`,
  `test_an_oracle_that_crashes_shows_no_defect`,
  `test_an_oracle_runs_isolated_from_the_hosts_environment`,
  `test_verify_runs_the_oracles_of_trusted_inputs_alone`,
  `test_two_inputs_that_build_one_commit_give_its_oracles_one_tree`.
- No agent's word is a label: `test_what_the_agents_say_of_their_own_work_moves_no_score`,
  `test_a_label_carrying_an_agents_confidence_or_verdict_is_refused`,
  `test_an_agent_is_no_adjudicator`, `test_an_agent_adjudicating_a_defect_or_a_fix_is_refused`,
  `test_no_preregistration_makes_an_agent_an_adjudicator`,
  `test_the_scorer_holds_no_engine_github_client_or_provider`.
- What the check refuses: `test_every_finding_has_exactly_one_label`,
  `test_a_label_is_bound_to_the_candidate_it_judged`, `test_labels_are_frozen_at_the_discovery_cutoff`,
  `test_a_confirmed_label_names_a_defect_its_candidate_holds`, `test_a_clean_negative_holds_no_defect`,
  `test_a_defect_is_one_the_change_introduced`, `test_a_fix_follows_the_defect_it_fixes`,
  `test_an_oracle_expected_to_both_fail_and_pass_is_refused`,
  `test_a_defect_case_holds_a_registered_defect_at_its_candidate`,
  `test_a_harmful_fix_is_exactly_one_that_introduced_a_defect`,
  `test_a_review_result_the_controller_would_refuse_is_refused`,
  `test_a_fixture_run_may_not_say_it_measured_anything`,
  `test_a_launch_is_bound_to_a_round_of_its_run`,
  `test_a_launch_ordinal_is_spent_once_and_within_the_runs_window`,
  `test_the_controller_record_is_held_to_what_the_controller_does`,
  `test_a_planned_run_is_not_repeated`,
  `test_a_run_excluded_before_its_first_review_is_an_exclusion`,
  `test_a_preregistration_short_of_a_usable_criterion_is_refused`.
- The label bounds and intervals:
  `test_unknown_findings_that_could_be_duplicates_bound_the_duplicate_ratio`,
  `test_unknown_findings_bound_recall_one_registered_defect_each`,
  `test_an_interval_ends_at_nearest_rank_order_statistics`,
  `test_a_label_band_at_its_limit_is_decided_exactly`.
- What the controller certified:
  `test_a_clean_reply_the_controller_would_not_certify_escapes_nothing`,
  `test_a_clean_reply_over_a_missing_record_decides_no_escape`,
  `test_a_deliberately_empty_validation_set_certifies_and_validates_nothing`,
  `test_a_missing_stop_leaves_an_issued_certificate_undecided`,
  `test_a_run_that_went_on_past_a_defective_candidate_has_no_known_success`.
- The decision, on synthetic real arms of 33 copied cases (100 for the excluded share at its
  limit):
  `test_fixture_runs_reach_no_decision_however_good_they_look`,
  `test_go_needs_the_primary_effect_every_bound_and_the_registered_sample`,
  `test_a_new_critical_escape_is_no_go_however_large_the_benefit`,
  `test_a_regression_a_run_introduced_is_its_own_whatever_its_id`,
  `test_a_shared_defect_with_no_usable_baseline_run_is_no_proven_new_escape`,
  `test_a_margin_worse_on_its_whole_interval_is_no_go`, `test_no_effect_is_no_go_on_a_complete_sample`,
  `test_unknown_labels_rule_out_go`, `test_unknown_findings_that_could_be_duplicates_rule_out_go`,
  `test_a_sample_below_the_registered_minimum_reaches_no_primary_conclusion`,
  `test_a_case_with_no_pair_or_too_many_exclusions_is_inconclusive_but_harm_still_decides`,
  `test_a_cost_no_launch_measured_rules_out_go`,
  `test_the_cost_criterion_is_decided_on_unrounded_values`,
  `test_a_file_is_decided_on_the_decimals_it_wrote`,
  `test_an_excluded_share_at_its_limit_is_decided_exactly`,
  `test_a_run_whose_launch_record_is_incomplete_has_no_measured_cost`,
  `test_an_omitted_retry_leaves_the_cost_unknown`,
  `test_an_omitted_leading_attempt_leaves_the_cost_unknown`,
  `test_a_run_that_goes_on_past_a_stale_round_has_no_known_outcome_or_cost`,
  `test_an_omitted_cost_margin_never_turns_an_open_cost_into_go`,
  `test_a_clean_reply_alone_cannot_trigger_the_hard_gate`.
- This page and the code: `test_the_baseline_facts_are_the_codes`,
  `test_the_baseline_cost_facts_are_the_codes`,
  `test_a_launch_window_is_read_in_the_controllers_terms`,
  `test_the_protocol_registers_the_digests_of_its_files`,
  `test_the_protocol_names_every_case_metric_and_registered_value`,
  `test_the_protocol_states_the_fixture_scores`, `test_no_workflow_or_package_module_runs_this_harness`.

## 11. Limitations

Decided on and left open:

- **No real run can be scored yet.** The collector of section 2 is owed; the protocol registers what
  it must produce, not the code that produces it: the labels, and from the controller's own state
  and run log each round's disposition, validation, validated candidate and certificate, and the
  run's launch window, where its ordinals start and end.
- **A run's cost is known only from its creation.** The state's `launch_seq` gives the last ordinal
  a run spent, not its first, so the window starts only at the run's creation for the case's issue.
  An EPIC run that took the case up after another issue has no record of where the case's ordinals
  start, and its usage stays `unknown`; an evaluation runs each case as a run of its own.
- **An oracle runs as its caller.** `verify` confines no oracle (section 1); it runs the registered
  files' alone unless told to trust others. A real evaluation that needs to replay oracles it did
  not write, or did not review, needs a boundary this harness does not have.
- **A stale round ends a run's record.** The record has no place for the revision a human pushed
  during a review, so a real run whose revision moved can be scored only up to that round, and,
  unless it stopped there, its outcome and cost stay `unknown`.
- **A run's outcome needs its recorded stop.** Only the controller's record of where a run stopped
  ties its outcome to its record's final round. A collector that cannot read that stop, or a run
  the controller resumed after its last recorded round, leaves the run's certificate, escapes and
  success `unknown`, and a decision on such runs `INCONCLUSIVE`.
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
- **#170 to #175 (ADR 0006):** the certificate keeps its meaning (section 3). Nothing here issues
  or relaxes one; a run record reads, through the collector, the controller's record of the
  certificate it issued for each round, and of what that round was presented. Oracles read a
  commit's tree through the same byte-exact materializer the controller exports a candidate with
  (D6.2, D6.3).
- **#338:** the new tests took 49 to 50 seconds serially on one shared host (section 10), most of it
  the case bootstrap of the decision tests; `verify` runs 228 git commands, its builds and the
  materializer's exports of each oracle's tree, and twelve oracle processes.
- **#23:** no state protocol, ADR or controller change is made, so no migration or crash-replay
  coverage is owed. Controller authority, exact-candidate validation, reviewer independence, the
  certificate and merge gate, and GitHub effect authorization are untouched, and default behaviour
  is unchanged.

## 13. Amendments

An amendment changes the registered files and these digests in one reviewed change, with its
reason, and states whether any real run had been made before it. A change to the code that moves a
baseline fact of section 2 is an amendment too. The files and digests below are those #355's own
pull request registers: what changed in them while it was under review, before any real run, is
part of the registration and no amendment.

- `preregistration.json`: `4faf1fc3b833867814dc9c9231b3684fd95b0e6aaa41aa404f3cb22a6a58cae8`
- `corpus.json`: `3e94841c82a0f4b6ed734915c4e002d29aefefd64bd2f44f475e5b1da4a5da63`

| Date | Change | Why | Real runs before it |
|---|---|---|---|
| None yet | | | |
