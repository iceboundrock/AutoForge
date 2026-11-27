# Review evaluation protocol (REV-R0)

**Status: preregistration and offline fixture only.** No benchmark, paid run, or rollout is
authorized or reported here. The baseline is `main` at `e86e606b0a21782d0d99308306c7d81206f27421`.
This protocol complements [the #318 evaluation harness](evaluation-protocol.md): use its
disposable repositories, isolation, protected checkers, and paired-run discipline for future
approved trials. REV-R0 replays a normalized evidence packet, not a second run of that harness.

## Controller baseline

The controller binds REVIEW to one PR, HEAD, base ref and merge base. The reviewer is separate
from the producer and fixer, reads the exact diff, and submits a structured result with the
round, reviewed HEAD, verdict, findings, and review sections. `blocked`, `non-blocked` and
`nit` describe whether and how the reviewer asks for action, not independent severity; every
finding needs a resolution. Round-scoped IDs and `required_resolution` connect REVIEW to FIX.
Observations are not findings. The controller, not the reviewer, validates the result, publishes
and verifies the comment, and reads the revision again.

| Launch | Default route (`profiles.py`, `config.py`) |
|---|---|
| ANALYZE_EXECUTE, FIX | Claude Code / Fable / high |
| REVIEW 1 | OpenCode / GPT 5.6 Luna / high |
| REVIEW 2–5 | OpenCode / GPT 5.6 Terra / high |
| REVIEW 6+ | OpenCode / GPT 5.6 Sol / medium |

`review_round` counts completed rounds on the current PR. A refused reply, correction, or
retry consumes a launch, not a round. A completed stale round consumes its number, carries its
findings forward and does not authorize FIX. A completed needs-fix round can enter FIX, block, or
trigger replan. A completed FIX, including one with no new commit, goes to REVIEW; a published
partial FIX blocks first and re-enters REVIEW on recovery, not another FIX of the old round.
That `BLOCKED` stop waits for an explicit unblock, so it ends the first-stop slice replayed here.
A completed clean round issues its certificate and enters `READY_FOR_MERGE`, or `BLOCKED` in
the same write when the validation configuration in force no longer yields the presented set
and policy (ADR 0006 D12.6); either ends the slice, and drift while the review ran makes the
round stale instead.
A held FIX can block without publication. Deferral, follow-up creation, and no change with
rationale are dispositions, not proof that a defect was repaired. Failed producer validation
does not publish a candidate; a clean reply over failed presented validation is refused
before publication even if the revision subsequently drifts.

`loop_guard.py` bounds the loop with a default cap of 20 completed rounds, two identical
resolution fingerprints, or three unchanged finding counts with recurrence; stale and clean
rounds break the streak. The default cumulative step cap is 300. Replan is checked before
blocking: the configured defaults use soft/hard thresholds 12/20 and a trailing window of
three rounds with at most two findings each and allow up to two replacements per issue.
Replacement starts a different PR at round 1,
not another round of this study's PR. These are controller settings, not outcome thresholds.
See [workflow and recovery](agent-guides/workflow.md) for the complete routing.

The current defaults route by round, not semantic risk. For a future trial, independently
record pre-outcome risk strata from the starting diff: routine, elevated (public contract,
persistence or concurrency), or critical surface (authorization, isolation, data integrity
or external effects). Record the rationale and exact candidate; do not use the reviewer's
confidence, the number of findings, or the final result to assign risk. Report strata
separately without adapting routing or promotion on this fixture.

### What counts as proof

A [`ReviewCertificate`](adr/0006-candidate-validation-evidence-and-review-certificates.md)
records that the controller completed a clean round, verified the comment's identity, and
presented the bound FULL validation set and policy for that exact candidate. Its named
records passed; an empty configured set also permits a certificate, but is not a validation
pass. It does not establish correctness, independent defect truth, GitHub approval, CI
status, permission to merge, or current applicability. Neither FAST checks (#338), an
agent's `tests_passed`, nor similar trees from other commits are substitutes. The merge gate
still reconciles exact revision, comment, validation, live checks and authorization. No
prompts, defaults, state schema, controller decisions or GitHub effects change here. A
future persisted field needs coordination with #23 and crash-replay coverage.

## Independent labels

Freeze the case set, starting snapshots, clean negatives, classification rubric, and
discovery cutoff before comparing arms; adjudicators work blind to assignment and may not
be a run's producer, reviewer, or fixer. A label cites an adjudicator, discovery date
(UTC calendar day, inclusive cutoff), and an independently reproducible artifact such as a
protected oracle assertion, minimized input with expected/observed output, or an independently
reviewed trace proving a contract violation. Bind each label to the exact reviewed candidate
SHA/tree and finding ID, each defect to the SHA introducing it, and each FIX judgment to the
published successor. The starting inventory is every registered defect present at the
starting candidate, whether the starting tip or an earlier commit of its lineage introduced
it; the defect keeps that actual introducing SHA. A report after cutoff, absent evidence,
disagreement without an independent resolution, or a match not settled by the cutoff is
unknown. A real trial should retain original artifacts, document disagreements, and have a
second independent adjudicator settle disputed or critical labels. A name or artifact string
in a packet is not authentication.

| Finding label | Meaning |
|---|---|
| Confirmed | The finding identifies a registered, independently evidenced defect present at the reviewed candidate. |
| False | Independent evidence disproves the actionable claim at that candidate. |
| Unknown | Neither established; includes unmapped claims and evidence discovered after cutoff. |

Severity weights: **critical 8** (severe integrity, safety or authority loss), **high 4**
(major incorrect behavior), **medium 2** (limited correctness or reliability impact), **low 1**
(small but actionable). Record class separately: correctness, security, reliability,
specification, or maintainability. Use stable independent defect IDs to match repeats within
a run; do not equate similar wording, severity, or agent IDs with the same root problem.
A clean negative requires an independent pass and no registered defect on the starting
candidate; lack of reviewer findings never labels a case clean. A published FIX with an
eligible attributable regression is harmful even if its separate label says unknown.
Otherwise a publication is independently adjudicated safe, harmful or unknown. In this
version an applied publication is an endpoint, not a count of underlying commits: the
endpoint must descend from the reviewed revision, and a defect introduced by any commit of
that published range and still present at the endpoint is the FIX's regression, keeping its
actual introducing SHA. The range follows every parent, so a side branch a FIX merges is
part of it, wherever it forked.

## Registered comparison

Baseline is the existing controller with its independent reviewer. Treatment changes only
the declared review intervention; neither side changes validation or safety policy. Future
trial design must pin the intervention and effective settings before collection, including
controller commit, prompts, providers and versions, tools, sandbox, budgets, validation policy
and command set, and all case/seed assignments. Follow #318 for fresh paired sessions,
ordering, preflight and separate cost approval. No production treatment or real manifest is
provided here; later dataset and evidence work belongs to #356, #357 and #360.

Match by case and seed. Each arm starts on the registered revision and uses the same base ref
and merge base throughout. The same starting SHA against different bases is a different
diff. The starting validation is the FULL receipt of any retained REVIEW launch bound to the
starting candidate, including an accepted review after a retry that presented none; a run
with conflicting receipts is refused. A known unequal starting validation set or policy is
reported as `validation_mismatch`; no receipt for the starting candidate is
`validation_unknown`, never filled from a later candidate or the certificate. Neither
enters paired rates. Exclusions are restricted to infrastructure, unavailable credentials,
unavailable service, or a documented pre-launch skip; stop/validation failures are not
exclusions. A `skipped` run is documented by a pre-PR stop at watermark zero; any launch
slot, retained or reserved, or a later watermark contradicts it and the packet is refused.
Keep both arms and report missing, incomparable, incomplete-ledger and excluded assignments
separately. A documented exclusion of either arm precedes every other status: the pair is
`excluded` whatever its ledgers, starting validation or missing arm.
Exclusions remove pairs from rates, cost, bootstrap and the sample floor, and from the
coverage condition of the decision (a gap in an excluded pair no more bears on coverage than
on a rate), counting only against the primary rule's exclusion budget. They do not remove
independently evidenced treatment harm from the safety report. Do not replace a failed or
missing seed; duplicate assignments are refused.

### Packet and replay

`tests/fixtures/review_eval/synthetic.json` is the version 1 example. JSON fields are closed.

- `cases` registers case IDs, seeds, starting candidate, base, cutoff, defects, and observed
  snapshots (`parents`, `tree`, independent presence inventory, oracle status and
  provenance). Each snapshot lists every parent once, a merge all of them; only the pinned
  merge base lists none, and the graph is acyclic. A side branch forked below the merge
  base keeps its real parent: `base_ancestors` lists such parents, each attested like the
  rest of the graph to lie in the merge base's own history. They are not snapshots, so never
  a candidate or an introduction, and every range that ends above the merge base excludes
  them, as `git rev-list` does. Every snapshot descends from the merge base or is a
  side-branch commit in the history of one that does. The start and every revision a
  launch, a stale move or the stop binds descends from the merge base and is not it. A
  defect present at the starting candidate is introduced by it or by a registered ancestor
  outside the merge base's history, through any parent. A FIX's published range is every
  registered commit reachable from its endpoint but not from its reviewed revision
  (`git rev-list reviewed..endpoint`), and the endpoint must descend from that revision;
  register each commit of a multi-commit FIX, side branches it merges included, with its
  real parents. A defect introduced in that range and present at the endpoint is that FIX's
  regression, reported only from a complete ledger (see *Incomplete ledgers*).
  SHA/tree fields in the synthetic fixture are stand-ins, not Git objects.
- Each `runs` entry has a case/seed/arm, one fresh PR identity, and producer/reviewer/fixer
  identities in that order. Its `creation` receipt identifies ordinal zero, before any
  producer launch; no takeover or pre-existing journal is admitted. `launches[ordinal-1]` owns all facts for
  that reservation: phase, disposition, and nullable usage. A missing reservation is a
  `null` slot; a retained launch with unknown disposition has `outcome: null`. Neither is
  a `retry`, which explicitly consumed no round or publication. REVIEW binds its exact
  revision and round; `findings: null` or `complete: false` means the retained findings
  are not the complete payload, even if review completion itself is proven. Validation
  receipts explicitly declare FULL scope; FAST is refused. Round r needs an ordinal above
  r, after the producer and each earlier round. A FIX source must be an earlier accepted
  needs-fix round at the same revision, and only retries of that FIX lie between them: a
  retained review or completed FIX in between supersedes the source. `no_change` consumes
  the FIX without publication. A retained accepted `clean` review, a `held` FIX and a
  `partial` FIX are first stops whatever precedes them, a gap included: any later ordinal,
  retained or reserved, or a later watermark is refused, and the stop binds that launch's
  round, review ordinal and revision and the phases it can enter, `READY_FOR_MERGE` or
  `BLOCKED` after the clean round and `BLOCKED` after either FIX. `partial` publishes its
  endpoint, which the stop binds, and keeps its harm evidence in a complete ledger. A gap
  before a first stop leaves the run unresolved. Same-scope head drift
  is represented by a completed stale review with `next_head`, or an explicit `drift` at the
  next REVIEW before it begins, the first REVIEW included; a review of a moved revision is
  never starting evidence, and one without an explicit drift is refused.
- The separately captured `stop` states the launch high-water mark (`through`), phase,
  current revision, last completed round and its review ordinal (or zero/null before any
  completion), an ordinal above that round. `issued` requires a terminal clean review at
  that ordinal with matching comment and exact presented validation, and a separate
  certificate receipt reference. The certificate's comment and receipt identities are
  nonempty strings, refused when malformed even where the review it names is missing; a
  well-formed comment is compared with that review's whenever both are retained. It is
  written when that round completes, so the ordinal is the stop's watermark. A
  presented receipt whose scope, set and policy match but whose result is unknown leaves
  issuance unproven (never filled from the certificate) while still stating starting
  validation; a conflicting known field is refused. The certificate presents the run's fixed
  validation set and policy, which any retained FULL receipt fixes, even when the named
  review's own receipt is missing. When the stop names a retained REVIEW,
  its round, and for `issued` its outcome, revision, comment and known validation fields,
  are cross-checked even across a gap elsewhere; a consistent gapped record leaves the run
  unresolved (though it can still prove a critical escape, see *Incomplete ledgers*), a
  contradicting field is refused. Every other retained launch is checked against
  the stop the same way: no review completes after the named one, a REVIEW launch before it
  is of at most its round and one after it of the next, a FIX follows the named round or
  precedes it as a FIX of an earlier one, and the launch at the watermark, retained with a
  known disposition, leaves the stop's revision. A `READY_FOR_MERGE` or `issued` stop never
  names a review whose presented validation failed, whatever its disposition.
  Issuance is recorded apart from the stop phase: `issued` stops at `READY_FOR_MERGE`, or at
  `BLOCKED` when D12.6 found the configuration changed after presentation, and `not_issued`
  never at `READY_FOR_MERGE`. Only a completed clean round enters `READY_FOR_MERGE` (D12.3),
  so that phase binds the clean round, its revision and the watermark even when issuance is
  unknown. The fixed-policy comparison does not hold the D12.6 change: such a
  certificate stays issued and certifies its candidate's escapes, is reported
  `certificate_applicable: false`, and leaves unresolved a validated success it would
  otherwise establish. A recorder never relabels it as non-issuance.
  `not_issued` on a reconciled block/failure is explicit non-issuance, including after a
  held or failed FIX at the reviewed revision. An absent stop, unknown issuance, tail
  gap, or historical certificate is not treated as an issuance or as a clean negative.
  Pre-PR stops use a null revision and name no round, and no retained launch may have
  published or bound the PR before one. Wall time comes from the stop observation and is
  reported whenever the stop retains it, even with incomplete launch coverage; total
  launch duration and terminal proof stay unresolved then.

#### Incomplete ledgers

A complete ledger is a precondition for scoring, and the replay does not reason across a
gap. A run has a gap when any ordinal up to and including its stop's watermark has no
retained launch record and disposition (a `null` slot, `outcome: null`, or an ordinal the
watermark reserves past the last slot), or when its stop is absent. Such a run is reported
with `ledger: "unresolved: incomplete ledger"` and its `missing_ordinals`, and its pair as
`incomplete_ledger`. `missing_ordinals` lists inclusive `[first, last]` ranges; the
ordinals a watermark reserves past the last slot are one range, so the report and the work
to build it are bounded by the retained slots, never by the watermark, whose magnitude the
packet's byte cap does not limit. The run contributes to no metric (recall, precision,
clean-negative FPR, harmful-FIX rate, duplicate ratio or cost per validated success), no
bootstrap draw and no sample floor, and nothing in it is reported as a known finding, a
false alarm or a harmful FIX (each is `null`), even when part of the evidence for one is
retained. Like a missing or incomparable pair, it keeps the decision from relying on
complete coverage. Wall time, launch counts and known launch time remain descriptive
observations of the run.

The one exception is the safety report, which reads every planned run, as exclusions remove
pairs from rates but not evidenced critical escapes. A critical escape needs no complete
ledger when retained records establish it without any missing transition: the stop's
`issued` certificate, the exact terminal clean review it names, retained at the watermark
with its disposition, comment and a known presented result matching the certificate, and
the cutoff-eligible independent inventory of that candidate listing the defect. Such an
escape is `true` and enters the decision like any other: where cutoff-eligible starting
evidence shows the defect shared at the registered start and a non-excluded baseline with
equal starting validation evidences that it did not escape, it is `NO_GO`. A starting
inventory evidenced after the cutoff is not that evidence: the escape stays listed, and
its comparison unresolved. An incomplete counterpart is no comparison evidence either way:
a baseline escape proven across a gap stays listed in its own run but leaves a treatment
escape of the same defect unresolved. Nothing else follows from those records across a
gap. Issuance as the run's terminal outcome stays unproven (`certificate_issued: null`,
`terminal_bound: false`), validated success is `null`, no FIX is credited with the escape,
so one a FIX introduced is listed but not as new treatment harm, and a critical defect
absent from that inventory is not thereby shown not to have escaped. Without each of those
records (a missing or undecided terminal review, an unknown presented result or issuance,
or an inventory evidenced after the cutoff) an unresolved run's critical defects stay
listed as unresolved escapes, and an escape its complete counterpart evidences still
counts.

Structural refusal does not change. A malformed record, an ordinal out of order or one its
round cannot fit, and any contradiction within a complete ledger are refused. Across a gap
the replay checks each record on its own and the explicit bindings stated above: the run's
fixed validation policy, a FIX's source and the retries between them, a first stop and the
stop, the stop's named review, and each retained launch against the stop. It searches a gap
for no other contradiction and infers no missing record or transition, so a run those
checks do not refuse is unresolved. Unresolved marks missing evidence, never a detected
contradiction.

This rule has a cost. It discards real information that an incomplete run retains, such as
an independently labeled false alarm or a FIX regression whose source review is retained,
and how often real ledgers will have gaps is unknown. A maintainer decision on #355 set it
before any real run: the issue never asked for inference across a missing record, and
reasoning around every possible gap has no natural end. If a real corpus shows that gaps
are common, a separate issue can revisit partial-ledger scoring with that evidence.

Within a complete ledger the replay keeps findings coverage apart from a bound stop:
removing a finding payload cannot remove an independently bound certificate, nor can a
later clean round make missing findings a true negative. Findings coverage needs complete
accepted payloads, not proof of issuance: an unknown presented result or issuance leaves
finding metrics intact.
Retargets, replacements, takeovers, other issue execution and merges are out of scope.
Outside the supported first-stop slice, preserve the raw record and report the affected
comparison unresolved rather than normalizing away an unsupported transition.

The tool checks internal consistency, not the authenticity of SHA graphs, exported receipts,
actor identities, completeness attestations or oracle artifacts. For recorded data these
must come from independent read-back of exact objects, validation bindings and controller
journals (including reserved ordinals) and blinded adjudication; not from a reviewer or
fixer's prose. There is no live collector or oracle execution in REV-R0. Missing fields
must be marked unknown, never reconstructed from a run's eventual success.

### Metrics, uncertainty and decision

Pool counts over matched runs by arm. Weighted recall is over the starting inventory only,
counted once per run; later reviews can discover a surviving start defect. A case whose
starting snapshot, or each of whose starting defects, was evidenced after the cutoff has
zero eligible recall observations; it drops out of the pool and its draws rather than
counting as incomplete coverage. Track newly introduced regressions separately. Use
accepted review findings; do not score stale replies.
Only pairs whose two ledgers are complete enter a metric. Within them, finding coverage is
that of accepted payloads only: a retained stale payload is still
checked, but a missing one does not suppress a rate. A metric's population is fixed before
coverage is checked: recall observes cases with eligible starting defects and FPR evidenced
clean negatives, both known at registration, while precision and duplicates observe every
matched case. Incomplete coverage in a case of the population suppresses that metric's
contrast; a case outside it affects neither the pool nor its draws.

| Metric | Formula |
|---|---|
| Weighted recall | Weight of distinct confirmed starting defects / eligible starting defect weight. Lower bound is the confirmed share; the upper adds the heaviest undetected eligible start defects, at most one per unknown finding. This is an outer bound, not an attribution. |
| Precision | Confirmed / (confirmed + false). Bounds assign unknown findings first to false, then to confirmed. |
| Clean-negative FPR | Negative runs with at least one false finding / label-settled negative runs. In a complete ledger an evidenced false finding survives missing finding details. Absence is settled only with a complete accepted review record and no unknown findings. Bounds assign unresolved negatives first to no alarm, then to false alarm. Incomplete review coverage of a matched case in a finding metric's population prevents a usable pooled rate for that metric. |
| Critical escapes | Independently evidenced eligible critical defects still present at a bound certified stop, even with an empty validation set. A bound non-issuance means no certified escapes; unresolved endings are counted separately. In an incomplete ledger only the stop's retained exact terminal review and matching certificate bind the candidate, for the safety report alone, and only a present defect is known there. |
| Harmful FIX rate | Harmful published FIX endpoints / adjudicated published FIX endpoints. Bounds divide by all such publications, assigning unknowns first to safe, then harmful. Held and no-change attempts do not enter this denominator. |
| Duplicate ratio | Confirmed findings after the first confirmation of their defect in the run / all confirmed findings. Bounds add all unknown labels to the denominator, first as nonduplicates and then as duplicates; conservative even if individual assignments would be impossible. |
| Total cost per validated success | Sum USD across **all** launches in matched runs, including retries and unsuccessful runs / count of validated successes. Success requires the current certificate, nonempty passed FULL validation, a passing independent oracle and no eligible high or critical defect remaining. Missing charges or unresolved success make the ratio unknown. |

An empty denominator is undefined, not zero. The report also gives launch counts by phase,
completed rounds, FIX launches, known launch duration, total launch duration, and wall time
from run creation to stop. These are different clocks; the final wall time includes controller
validation and time between launches. For future collection use controller run log ordinals,
execution records (`started_at`/`finished_at`), stop state and timeline; missing bounds or
durations cannot be measured as zero. Claude's stream can supply typed `cost_micro_usd`,
but default mixed routing has no complete bill; no provider records complete context usage
for this study. Both totals remain unknown in `recorded` mode until independently attributable
accounting is available (#154, #321). Fixture amounts are synthetic arithmetic examples.

Use a paired case bootstrap: 10,000 resamples, seed 355, all seeds of a sampled case included
together. Drop cases with zero eligible observations on both arms for the given metric.
Report nearest-rank 2.5th/97.5th percentiles of defined point differences, and a separate
outer envelope (treatment-low minus baseline-high, treatment-high minus baseline-low).
Report undefined draws; do not substitute zeros. For a usable contrast both arm label bands
must be at most 0.10 wide, points must be defined, and no draw may be undefined. Use exact
fractions, not rounded report values. This is label and sampling uncertainty, not a
powered sample-size claim.

- **Primary:** weighted recall improvement at least +0.10 on the pooled conservative
  difference, with bootstrap lower envelope above zero. Non-inferiority margins:
  precision −0.05, FPR +0.05, harmful FIX rate +0.05, duplicates +0.10, and cost per
  validated success +50% (point estimate). Sample floor: 20 paired defect cases and
  10 paired negatives with accepted starting reviews and the cutoff-eligible starting
  evidence their recall or FPR observations need; exclude at most 10% of planned pairs.
- **Safety first:** recorded new critical treatment escapes are `NO_GO` even from excluded
  runs or incomplete ledgers (see *Incomplete ledgers*). A proven FIX-introduced treatment
  escape needs no counterpart; only a complete ledger proves that provenance. A defect is a
  shared starting defect only on cutoff-eligible evidence that it was present at the
  registered start: the starting snapshot's inventory and the defect's own label, both
  evidenced by the cutoff. For one require that evidence, a non-excluded baseline, equal
  starting validation, and evidence that it did not escape; absent or unresolved baseline
  evidence is not that evidence. A defect whose starting inventory was evidenced after the
  cutoff, like a later defect without attributed FIX provenance, is reported, its comparison
  unresolved, but not automatically new treatment harm.
- **Other harm:** an entire usable envelope beyond a non-inferiority margin is `NO_GO`
  when no planned pair is missing, incomparable or incomplete-ledger. An excluded pair is
  none of these, whatever its ledgers: exclusion precedes every other status (see
  *Registered comparison*), so a documented exclusion exempts its gap from this condition
  as it does from the rates, and the safety report still reads its runs. An upper recall
  envelope below +0.10 is `NO_GO` only at the sample floor and exclusion budget. A measured
  cost above margin would also be `NO_GO`, but this version cannot measure recorded cost.
- **Otherwise:** synthetic data and a missing arm without measured safety harm are
  `NOT_EVALUATED`. Any remaining recorded result is `INCONCLUSIVE` in this version.
  A later `GO` requires the registered primary and all non-inferiority limits established,
  no unresolved outcomes or safety results, complete comparable assignments within the
  sample/exclusion rules, and independently measured cost. It is intentionally unreachable
  here. A fixture result is never a rollout gate.

## Replay and verification

```sh
uv run python -m tests.review_eval
uv run pytest tests/test_review_eval.py tests/test_fast_targets.py
```

The fixture has two matched cases, a repaired defect, a harmful publication with a critical
escape despite a certificate, duplicate findings, a clean negative, and a false alarm that
leads to a no-commit FIX. Tests check the deterministic report against hand-computed values
and mutate evidence to check refused or stale replies, FIX retries and publication order,
missing launches/findings, one-PR scope, exact terminal bindings, exclusions, starting
defects introduced before the starting tip, and policy comparability, including starting
receipts across a retry. They also cover a regression from the first of two FIX commits,
a published partial FIX as a `BLOCKED` stop, missing stale payloads, drift before the first
review, documented and contradicted pre-launch skips, the cutoff of the sample floor, an
unknown presented validation result, and wall time kept across a missing launch. They
cover a partial FIX and an accepted clean review as first stops across a gap, a refused
drift after a clean round beside a valid stale one, a certificate whose comment or policy
contradicts its retained review across a gap, unknown certificate evidence beside complete
finding coverage, a cutoff-ineligible case beside an eligible one, a regression and a
starting defect from a merged side branch, a cyclic or second-rooted graph, a
`READY_FOR_MERGE` stop after a held FIX, and a certificate issued into `BLOCKED` (D12.6).
They cover the stop phases each first stop can enter, complete or gapped, a stop that a
later completed round, a launch's round, a FIX source or the watermark launch's revision
contradicts across a gap, a `READY_FOR_MERGE` stop naming a review over failed validation,
a cutoff-ineligible case whose payloads are missing, and a side branch forked below the
merge base, with the limits of a base ancestor. Each shape of incomplete ledger (a missing
producer, review, disposition, reserved tail or stop) is an unresolved run whose pair
changes no rate, draw, cost or sample floor and reports no finding, false alarm or harmful
FIX, while the safety report keeps its critical defect; a retained false alarm and a FIX
regression are among what a gap leaves unreported. A superseded FIX source, a round its
ordinal cannot hold, a pre-PR stop after a publication and a certificate off the fixed
validation policy are refused, gapped or complete. A critical starting defect that the
treatment certifies and the baseline repairs stays a `NO_GO` escape when only the
treatment's producer slot is missing, entering no rate, draw, cost or sample floor and
crediting no FIX harm or validated success; without its exact terminal review, its
disposition, a known presented result, the issuance or a cutoff-eligible inventory it stays
unresolved, gapped or complete. A watermark of any magnitude reports its missing ordinals
as bounded ranges. A critical starting defect that survives at the treatment's distinct
certified successor is a `NO_GO` escape when the starting inventory was evidenced by the
cutoff and an unresolved comparison when only a later one shows it shared, gapped or
complete, while a FIX regression gates without starting evidence. Beside a matched pair
with FPR harm, a pair missing its treatment producer slot keeps the decision
`INCONCLUSIVE` until a documented exclusion removes it, after which the decision is the
`NO_GO` an excluded complete pair gives, and an excluded gapped treatment's proven critical
escape stays `NO_GO`. A certificate whose comment identity is malformed is refused whether
or not the review it names is retained; a well-formed one whose review is missing stays
unresolved.
Existing
controller tests (`test_routing.py`, `test_loop_guard.py`, `test_validation_gates.py`,
`test_review_certificate.py`, `test_merge_certificate.py`, `test_partial_fix.py`,
`test_replan.py`) verify runtime behavior with fakes; this replay does not verify or
execute those effects. No model, candidate code, network, credentials,
or state writes are needed to replay the packet.

Fixture SHA-256: `d57351ac1fbe669548cec96c5026a6571def66058c608fe9879e35b6eec2fc5e`.
Amend the protocol, packet version (for evidence-rule changes), fixture digest and tests in
one reviewed change. State why the rule moved and whether any real runs preceded it.
