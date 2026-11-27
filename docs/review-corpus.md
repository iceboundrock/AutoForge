# Review corpus and offline reviewer benchmark (REV-R1)

**Status:** corpus format version 1, its checker, git verifier and scorer, a synthetic fixture, and
an unadjudicated seed of eight real candidates from this repository (#356). **No label is
adjudicated, no reviewer has been run on any case, and no benchmark result exists.** Nothing here
changes a prompt, a default, the state file, a controller decision or a GitHub effect.

[REV-R0](review-evaluation-protocol.md) replays the controller's REVIEW/FIX loop to judge a change
to that loop. Comparing reviewers (another model, prompt or effort) needs something REV-R0 cannot
give: the same candidates for every reviewer, each with defects known independently of any
reviewer, shown as they stood when they were reviewed. This page defines that corpus and how a
reviewer's findings on it are scored. It keeps REV-R0's label meanings, severity weights
(critical 8, high 4, medium 2, low 1), defect classes and exclusion reasons, and leaves every real
run to [#318's harness rules](evaluation-protocol.md).

## 1. A case

A case is one candidate a reviewer is asked to review, pinned so that anyone holding the
repository's objects can rebuild exactly what the reviewer was given:

- **Review binding:** base ref, merge base, head, the head's tree, and the review-time cutoff (a
  UTC instant). The reviewer reads the diff from the merge base to the head.
- **Environment:** the blob ids, at the head, of the files that define the test environment
  (`pyproject.toml`, `uv.lock`), and the validation commands a runner may run.
- **Context:** what the reviewer reads besides the diff (the issue, the PR body, comments). Each
  item is either captured, as text with its SHA-256 and the instant it reached that state, or
  explicitly `not_captured`.
- **Defects** of a defect case, or the **negative** record of a negative case (sections 3 and 4).
- **Withheld commits:** commits that would reveal an answer (the fixes, later rounds, the later
  merge). They are never part of the reviewer's input.
- **Actors:** who produced, reviewed, fixed or merged the candidate. None may adjudicate it.
- **Risk stratum:** REV-R0's routine, elevated or critical surface, decided from the diff before
  any outcome is known, with its rationale.
- **Cluster:** the unit resampled together. Cases of one issue share code and history, so treating
  them as independent would overstate the evidence.
- **Provenance:** `pr_branch` (a review round's exact head on a PR branch), `squash` (a squash
  commit on main) or `synthetic`; the issue and PR as git states them; the round; the selection
  rule; a note on what is not known.

| Stratum | A case of it | Needs a defect discovered |
|---|---|---|
| `reviewed_defect` | a head a review round read, holding defects some review found | `same_review` or `later_review` |
| `escape` | a candidate whose defect was found only after merge | `post_merge` |
| `revert` | a candidate later reverted for a defect | `revert` |
| `hard_negative` | a candidate on a defect case's surface, with no registered defect | none registered |
| `clean` | a candidate with no registered defect | none registered |

`same_review` means the case's own historical reviewer reported the defect at that head;
`later_review`, that a later round reported it while it was already present there.

## 2. Cutoff, look-ahead and leakage

A historical benchmark is only as good as its separation of what the reviewer could have known
from what was learned afterwards. These rules hold that line; each is enforced where it says.

- A captured context item was observed at or before the cutoff (inclusive), its digest matches its
  text, and its text names no withheld commit: no hexadecimal run of seven or more digits that
  abbreviates one, and none that holds one. The corpus check refuses any other.
- A case with context that is `not_captured` cannot be planned for a run: reviewing it without its
  issue would be a different task. The scorer refuses such a plan.
- The head was committed by the cutoff; every withheld commit exists, is not reachable from the
  head and was committed after the cutoff; a defect's introducing commit is reachable from the head
  and not from the merge base, and its fix descends from it. `verify` checks each against the
  object store. The corpus check refuses a fix that is not withheld, a withheld head or merge base,
  and a withheld commit named as a present defect's introduction.
- A reviewer's input is identified by its digest: the review binding, the environment and each
  context item's id, kind, reference and text digest. Nothing about the answer (defects, labels,
  withheld commits, actors, risk, case ids) enters it, and every run states the digest of the input
  it was given. A run naming another digest is refused.
- A model may have been trained on the answer. A variant whose stated knowledge cutoff falls on or
  after a case's earliest discovery, or that states none, is counted as `possible` exposure in its
  scorecard. The count is reported, not corrected for.

For a `pr_branch` case the cutoff is the head's committer time: when the review started is not in
git, and the head's time is the earliest the candidate existed, so context admitted by it is
conservative. A `squash` case takes the merge time, and its diff is the merged change, which can
differ from what the last review read.

What a future runner must still provide, and nothing here implements: a disposable repository whose
history ends at the head (as #318's runs start from a base's tree), so the reviewer cannot read
the withheld commits from the repository itself; no network route to the repository's later
history; and adjudicators who cannot see which variant produced a finding.

## 3. Labels and adjudication

Every label names who decided, their role, the verdict, the UTC day it was decided and an
independently reproducible artifact (REV-R0's examples: an oracle assertion, a minimized input, a
reviewed trace). A **curator** proposes and never settles anything. **Adjudicators** settle a
label when they agree: one suffices, two for a critical defect, either way. A disagreement stays
`unknown` unless **arbiters**, all of one verdict, settle it; an arbiter label without a
disagreement is refused. An `unknown` verdict counts for nothing. One identity labels a record
once, no actor of the case may label it, and no label is decided after the corpus's freeze. An
identity is a string, not an authentication, as in REV-R0.

A finding is matched against the case's registered defects by adjudicators who are neither a
reviewer variant nor an actor of the case: `match` to one defect, `false`, or `unknown`, two
adjudicators for a match to a critical defect. A match to a confirmed defect is a confirmed
finding, a match to a rejected one is false, and a match to an unsettled one is unknown. A real
defect the corpus does not register is `unknown` in this release, as in REV-R0, and belongs in the
next one. Similar wording, severity or a finding id never makes two findings one defect.

## 4. Censoring

Absence of evidence is not a label, and the corpus keeps the difference:

- A negative is **settled** only when an independent pass labels it clean and its observation
  window, from the cutoff to `observed_through`, is at least 30 days. A shorter window is
  **censored**, no settled pass is **unlabeled**, and a pass that finds a defect **refutes** it.
  Only settled negatives enter the false-positive rate. The 30 days is a preregistered choice, not
  a measurement: it was fixed before any reviewer ran, and changes only by section 9.
- A defect discovered after the freeze is outside the release. No case is ever "clean" because
  nothing was registered.
- An inventory is what was found, not what exists. Recall is recall of registered, confirmed
  defects; a finding outside the inventory is unknown, which bounds precision rather than
  lowering it.
- **Discovery bias.** A `same_review` defect is one the case's own historical reviewer found, so a
  reviewer resembling it is favoured on those defects. `later_review`, `post_merge` and `revert`
  defects are ones the historical reviews missed. Recall is reported per discovery source so a
  contrast resting only on `same_review` defects can be seen for what it is.

## 5. Submissions

A submissions file names the corpus by its digest (SHA-256 of its canonical JSON, so the file's
layout does not matter but every value does) and its own freeze for finding labels. It pins each
variant: identity, provider and its version, model, effort, stated knowledge cutoff, prompt
template and its SHA-256, the AutoForge commit and the sandbox. Its plan is a list of cases and
seeds; a seed is an independent repetition in a fresh session. Each planned run is `completed`
(with its findings, each classified `blocked`, `non-blocked` or `nit` as a REVIEW result is),
`failed` (no findings: missing evidence, never zero findings), or `excluded` for one of REV-R0's
reasons. One run per variant, case and seed. Synthetic submissions are scored only against the
synthetic corpus and recorded ones only against a historical corpus.

## 6. Scorecards

Each variant gets a scorecard over its completed runs, and each non-baseline variant a contrast
with the baseline over the pairs both completed.

| Metric | Observations, pooled over runs | Label bounds |
|---|---|---|
| Weighted recall | weight of distinct confirmed defects found / weight of the case's confirmed defects | the upper bound adds, per run, the heaviest missed defects, at most one per unknown finding: an outer bound, not an attribution |
| Precision | confirmed findings / (confirmed + false) | unknown findings counted first as false, then as confirmed |
| Clean-negative FPR | settled-negative runs with a false finding / settled-negative runs | a run with only unknown findings counted first as no alarm, then as an alarm |
| Recall by severity, discovery source, risk | as recall, by defect count within a stratum (by weight for risk) | each stratum bounded on its own |
| Precision by classification | as precision, within `blocked`, `non-blocked`, `nit` | as precision |

A scorecard also counts runs by status, confirmed, false and unknown findings, duplicates
(confirmed findings after the first of their defect in a run), training exposure and total cost
(unknown when any launched run's cost is). An empty denominator is undefined, never zero.

Uncertainty is reported two ways and never combined. Label bounds come from the unknowns above.
Sampling uncertainty comes from a cluster bootstrap: 10,000 resamples of the clusters that observe
the metric, seed 356, every case and seed of a drawn cluster together; the draws depend only on how
many clusters a metric observes, so both arms of a contrast share them. The report gives the
nearest-rank 2.5th and 97.5th percentiles of the point estimate (a contrast's of the point
difference), a separate outer envelope (variant low minus baseline high, variant high minus
baseline low), and the number of undefined draws, which are never replaced. Values are exact
fractions.

A contrast is `usable` only when every planned pair either completed on both arms or was
excluded, it observes at least two clusters (one cluster resamples to itself), both arms' label
bands are at most 0.10 wide, both points are defined and no draw is undefined. An excluded pair
leaves the contrast; a missing or failed one leaves it incomplete.

There is no decision rule. A scorecard is a measurement, not a `GO`: deciding to change a
reviewer needs its own preregistered thresholds and REV-R0's or #318's approval steps, and a
synthetic report is never evidence for anything.

## 7. The seed, release 1

`tests/fixtures/review_corpus/autoforge_seed.json` holds eight real candidates from this
repository, frozen on 2026-11-27.

**How they were chosen.** Rule 1: on a PR branch in the local object store
(`refs/remotes/origin/autoforge/*` on 2026-11-27), a FIX commit whose subject names round *n*'s
findings (`R<n>-F<m>`) has round *n*'s reviewed head as its first parent. Rule 2: the squash
commit on main that merged the issue of a seed defect case is a hard negative on the same surface.
Rule 1 finds many more rounds than were taken (issue #317's branch reaches round 11, #324's
round 19). Release 1 took the four rounds of issue #329's branch, because its FIX messages
describe each finding precisely enough to trace in the code, plus the first round of #414's and
#262's branches for a second critical surface and a documentation-only case. That choice is the
curator's, not a rule.

| Case | Head | Registered defects (same / later review) | Stratum, risk |
|---|---|---|---|
| `pr539-round1` | `89126296` | 1 / 4 | reviewed defect, critical |
| `pr539-round2` | `c8d96429` | 3 / 2 | reviewed defect, critical |
| `pr539-round3` | `046bbb12` | 1 / 2 | reviewed defect, critical |
| `pr539-round4` | `f47a59a3` | 2 / 0 | reviewed defect, critical |
| `pr459-round1` | `0b8eeb75` | 4 / 0 | reviewed defect, critical |
| `issue262-round1` | `fd257b28` | 1 / 0 | reviewed defect, routine |
| `pr540-squash` | `e86e606b` | none | hard negative, critical |
| `pr460-squash` | `08a2e075` | none | hard negative, critical |

**What each case registers.** Every finding a round's FIX commits name is registered at the head
that round reviewed. For #329's branch it is also registered at each earlier head into whose
history the curator traced the defective code, as a `later_review` defect: four of the seven
defects there were already in the producer's commit and missed by round 1. Two came from FIX
commits: round 1's FIX narrowed doctor's exact-read handler so that `gh_workdir`'s error escaped
it (`r2-f1-workdir`), and round 2's FIX added the caller inventory that left out K13
(`r4-f2-k13-inventory`). R2-F1 is registered as two defects because its two failures have
different causes and different introducing commits. That makes 20 registrations of 12 distinct
defects. The discovery day is the FIX commit's committer day, an upper bound on the review that
reported it.

**What is judgment and what is unknown.** Severity, class, summaries, risk strata, the split of
R2-F1 and the tracing to earlier heads are the curator's proposals, recorded as curator labels;
none is settled. The two negatives have no independent pass, so they are unlabeled, and by the
freeze their windows (3 and 21 days) would leave them censored anyway. Offline, git does not hold:
the issue and PR text at each cutoff (so no case is runnable yet), the full finding list of each
round (a finding resolved without a commit, deferred or carried forward does not appear), which
rounds were clean, and which agents and models acted. PR numbers are as the commits state them
(main merged those issues as #540 and #460), not checked against GitHub. The actors are
role names for the same reason.

**Verification.** `uv run python -m tests.review_corpus verify --repo .` was run on 2026-11-27
against this repository's object store, with git 2.47.3: all eight cases passed every check of
section 2. The PR branches are not on main, so rebuilding these cases needs their objects (the
branches, or the PRs' head refs on GitHub, which were not checked); CI's shallow checkout does not
hold them, so no test reads this repository's history.

**What would make it golden:** capturing each case's context as it stood at its cutoff (a GitHub
read of the issue, PR body and comments with their edit history), independent adjudication of
every defect (two for the critical one) and both negatives, and only then planned runs, matched
blind.

## 8. Commands and tests

```sh
uv run python -m tests.review_corpus describe           # the seed: cases, strata, label states
uv run python -m tests.review_corpus verify --repo .    # its pins against this object store
uv run python -m tests.review_corpus score              # the synthetic fixture's scorecards
uv run pytest tests/test_review_corpus.py
```

The synthetic fixture (`synthetic_corpus.json`, `synthetic_submissions.json`) covers every
stratum and label outcome: a disagreement, a critical defect with one adjudicator, a rejected
defect, a censored and two settled negatives, a duplicate, a match to a rejected defect, unknown
findings and a variant without a knowledge cutoff. The tests check its scorecards against values
computed by hand, refuse each malformed or leaking record, settle labels and change windows to show
each rule move a number, and run `verify` on a throwaway repository with fixed commit dates,
including against the operator's `GIT_*` variables and a replacement object. They pin the seed's
state: eight historical cases, 20 unsettled registrations, no runnable case.

## 9. Coordination and amendments

- REV-R0 (#355) keeps controller-loop interventions; this corpus is for reviewer variants and
  reuses REV-R0's definitions rather than restating them differently.
- #318 governs any real run: fresh sessions, paired order, preflight and separate cost approval.
  Its contamination rule (no run on this repository) becomes section 2's disposable repository,
  since this repository's own history is the corpus.
- No state protocol, schema or ADR changes, so nothing is coordinated with #23.
- Tests and CI call no model and no GitHub API.

A rule change bumps the format `version`; a content change bumps the corpus `release`. Either
updates the digests below and the tests in one reviewed change, and says whether any run preceded
it.

Fixture SHA-256:

- `autoforge_seed.json`: `f0b6197dcced013d2a0fa81cb3375b119a916af7122ff4efb7969b191efb2a9f`
- `synthetic_corpus.json`: `355f97b869b17eb56150ba70a252d37e3ac163983c13cf1741a23b383a952fa4`
- `synthetic_submissions.json`: `67361aefd900738fdc8a505a986abc8d053fd62479cbea5c3ffa7022a29cd244`
