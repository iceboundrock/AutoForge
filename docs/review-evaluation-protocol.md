# Review evaluation protocol (REV-R0)

A clean review is not a measurement of review quality. To compare review
strategies, freeze the defects, label the findings independently, and measure
what was present when the controller stopped. This is an offline contract for
[#355](https://github.com/iceboundrock/AutoForge/issues/355), not a benchmark
result or a policy for running AutoForge. No paid evaluation has run.

## Baseline

Code baseline: `e86e606b0a21782d0d99308306c7d81206f27421`. The baseline arm
uses today's REVIEW/FIX path, with the provider, model, effort, prompts,
budget and validation policy recorded before a later trial. Profile routing
selects a name, not a model or a risk score.

| Evidence | What the baseline does or records |
|---|---|
| Round and routing | `review_round` starts at 0 and tracks completed rounds on the current PR. The next launch uses round + 1 (`profiles.py`): `review_round_1`, `review_round_2_5`, or `review_round_6_plus` at rounds 1, 2–5, and 6 onward. A replacement starts a fresh review sequence. |
| Review reply and saved round | `ReviewResult` checks the exact reviewed HEAD and round, `needs_fix_round`, and the finding list. Findings have an ID, classification (`blocked`, `non-blocked`, `nit`), required resolution, title and location; the prose sections are Spec, Standards, Assessment, Observations, Verification and Summary. State retains the open findings and `review_history` (`needs_fix`, `clean`, `stale`), not independent adjudication. The controller publishes the review comment. |
| FIX | The result carries previous and resulting HEAD, reported tests and one resolution per open finding: `fixed`, `follow_up_created`, `no_change_with_rationale`, `acceptance_action`, `operator_acceptance`, or `pr_body_update`. These are requests/claims. The controller checks candidate and authorization, owns publication, and can block or follow a partial-fix route. That need not produce a new commit or completed REVIEW. |
| Convergence | `workflow` defaults: 20 rounds per PR, stagnation after 2 identical resolution rounds or a 3-round unchanged-count window with recurrence, and 300 controller steps across a run. The normalized text heuristic is not semantic deduplication. `review.replan` is enabled, soft threshold 12, hard 20, 3 trailing small rounds with at most 2 findings each, and at most 2 completed replans per issue. Round 20 with findings is a replan trigger, not a guaranteed replacement: truncated history or an exhausted allowance can block. A clean round does not trigger a replan. |
| Risk | There is no Quick/Standard/Deep risk selector in this baseline. Register `low`, `standard` or `high` per case, before viewing arm output, from the reach of the change (local, shared behavior, or authority/irreversible effect). Report by case/stratum; do not route from these labels or an agent's confidence. |
| Usage | The controller reserves a `launch_seq` ordinal before launching, including retries/corrections; an interruption can leave a reserved ordinal without a completed log. `execution.json` can give launch timings and provider summaries. Claude Code stream mode and the Claude Agent SDK can report `cost_micro_usd`; availability of an individual field does not establish complete billing. Providers do not supply a common, complete context-token or billing total. |

The rate, cost, token and wall-clock baselines for **real labeled runs** are
all unknown. Launch wall time is not elapsed issue time; use the whole-run
clock in [#318's protocol](evaluation-protocol.md) for that. REV-R5 and
[#154](https://github.com/iceboundrock/AutoForge/issues/154) /
[#321](https://github.com/iceboundrock/AutoForge/issues/321) own later
accounting evidence.

## Study and labels

Before collecting either arm, record the case list, starting candidate
and merge base, risk, discovery cutoff, and arm configurations. Use the
same case and frozen ground truth for both arms. Hold implementation and
FIX profiles, tools, permissions, sandbox, validation, merge disabled,
budgets, and visible inputs equal unless one is the registered intervention.
Randomize order within each pair and keep gold data out of the review
worktree and prompt. Preserve failed launches, missing pairs and exclusions.
For real work, archive the independent evidence by reference, not raw
provider prompts or credentials.

- A **case defect** has an identity shared across arms of one case. A
  regression introduced by FIX has identity `(run id, defect id)`; a matching
  spelling in another run means nothing. Each defect carries an introduction,
  class (`correctness`, `safety`, `recovery`, `evidence`, `compatibility`,
  `other`), severity and independent evidence. The defect must be attributable
  to the change, not present at its base. Severity weights:
  **critical** (8: authority breach, irreversible loss or unusable core
  outcome), **major** (4: material wrong outcome), **minor** (2: limited
  behavioral defect), **informational** (1: independently verifiable problem
  without a material outcome). These are not reviewer classifications.
- A **finding** is keyed by run, review launch, finding ID and full candidate
  SHA. Label it `confirmed` with one registered defect present at that
  commit, `false` if the claimed problem is disproved, or `unknown` otherwise.
  No label is `unknown`, not `false`. Repeated confirmations of the same
  defect in one review are duplicates; cross-round repeat is a separate
  convergence observation. Preserve the original claim separately.
- A **FIX label** says `harmful`, `not_harmful` or `unknown` at the exact
  applied commit. An eligible registered regression introduced at that
  commit makes it harmful: a label of `not_harmful` or `unknown` there, or
  `harmful` without one, is refused as contradictory, not scored. A
  no-commit outcome is not a FIX commit in the denominator.
  A declared resolution must find the defect at the parent and remove it
  at the child. Introduction always requires failing oracle evidence.
- A label or defect attestation gives a named **independent human**, a source
  reference and adjudication time. No agent verdict, confidence, resolution
  type, test claim, clean reply, or controller certificate serves as a
  ground-truth label. The custodian checks human identity and independence;
  the scorer cannot authenticate a name. Use only defects evidenced by the
  frozen UTC cutoff and label by that snapshot. A defect case must expose
  an eligible defect at the pinned candidate; clean negatives must have none
  on their own lineage. Later discoveries are kept outside the frozen
  metrics, not retrospectively counted for the arm that found them.

## Controller evidence and usage

`ReviewCertificate` records issuance by the controller, at the full PR
revision, review round and SHA, from a clean accepted review and the
bound validation set and policy. A deliberately empty configured set can
produce a certificate; it does not say validation passed. Failed or missing
evidence does not prove issuance. The controller can record a clean round
then block without issuing a certificate. It never accepts a clean reply
while a validation record presented to the reviewer did not pass: it refuses
the reply before anything is published (`_check_review_evidence` in
`engine.py`) and asks again. An accepted clean review over a failed
validation receipt is therefore refused as contradictory, never scored as a
true negative. Record the refused reply as a REVIEW launch with outcome
`refused`, which completes no round, and the corrected reply as the review
row. The certificate is neither defect
truth nor permission to merge; live applicability, exact-candidate validation,
merge permission, CI checks, live GitHub reads and effect authorization remain
controller concerns (ADR 0006, #170–#175 and #338).

Review history, a certificate receipt, and the terminal stop are separate
observations. To score a certified ending, require an independently recorded
`READY_FOR_MERGE` stop with the revision (PR, base, merge base, round, SHA),
ordinal and certificate identity of the accepted review, plus the bound
validation receipt for the same set and policy. At a bound
`BLOCKED`/`FAILED` stop with explicit
non-issuance and no certificate on that review, no certificate escaped and
validated success is false. Otherwise keep certification, escapes and
success unknown. In particular, preserve a historical certificate when drift
causes a same-ordinal block. No stop is missing evidence, not proof of
continuation; a later observed ordinal beyond a stop proves continuation.
A stale prefix on its own establishes neither closure nor cost.

Usage has a different boundary: a run-creation receipt identifying that
run at ordinal **zero**, a separately observed stop high-water mark, and
one independently sourced record per reserved ordinal, including failed
attempts, refused replies, abandoned reservations and retries. Log only
measured values or explicit nulls: do not use zero for absent usage. A
validated success also
needs a resolved terminal outcome. Neither a set of distinct phases/rounds
nor the greatest ordinal in a log proves completeness. An existing run that
started before the evaluation window is not a fresh-run denominator.
An infrastructure exclusion can have no results, including when a first
REVIEW launch failed. Keep its launch rows, stop if observed, and exclusion
reason; do not invent a finding or drop the assignment.

## Metrics and uncertainty

Pool counts within an arm, not averages of per-case rates. For findings
and exposure use completed, accepted reviews; stale replies are observations
only. Report excluded and missing cases separately. Decision rates use
matched, non-excluded case pairs. A case counts toward the sample minimum
only if each arm accepted a review at the pinned candidate.

An exclusion keeps a run out of the rates, the paired bootstrap, the sample
floor, cost, usage and the per-arm escape totals. It never removes evidenced
harm. The report keeps each run's escapes, an excluded run's included, and
the critical-escape gate reads excluded treatment runs as it reads the others
(decision step 2). What an excluded run leaves unknown settles nothing and
unsettles nothing, so an excluded first launch that produced no reply stays
representable without blocking the gate.

| Metric | Numerator / denominator and label uncertainty |
|---|---|
| Severity-weighted recall | Sum of weights of distinct detected eligible defects divided by the weight of distinct eligible defects exposed to review, counted separately per run. The lower bound uses confirmations. The upper assigns each unknown finding at most one as-yet-undetected defect eligible at that commit, maximizing weight across the run. |
| Precision | Confirmed findings / (confirmed + false). Bounds use all findings, assigning unknowns first to false, then to true. This estimates classification uncertainty, including defects outside frozen identity coverage; it does not add defects to the recall denominator. |
| Clean-negative false-positive rate | Negative runs with ≥1 false finding on the case's own commits / label-settled negative runs. No findings means a true negative only on a completed accepted review. Other negative runs (only unknown findings, or no accepted eligible review) widen the bounds by counting first as true negatives, then false positives. |
| Escaped critical defects | Count eligible critical defects still present at the certified stop. A bound non-issuance contributes zero; an unresolved ending leaves the total unknown, with known escapes reported separately. The report also lists known escapes of other severities. Arm totals cover included runs; each run's escapes are reported, an excluded run's included. |
| Harmful-fix rate | Harmful FIX commits / adjudicated FIX commits. Use all FIX commits in the bounds, settling unknowns first as harmless, then harmful. |
| Duplicate ratio | Duplicate confirmed findings / all confirmed findings. For the lower bound each unknown can name a distinct, unnamed eligible defect until those slots run out. For the upper, add all unknowns in rounds with a confirmation as repeats. A round with no confirmation and an eligible defect can add `k−1` repeats for `k` confirmations; include these groups by decreasing `k` while they improve the pooled ratio. A round without eligible defects can add neither first reports nor repeats. |
| Total cost per validated success | Sum USD over **all** attempts (not only successful ones) in included runs / count of validated successes. A success requires certification over a nonempty passed validation set and no registered major or critical defect at the terminal candidate. Undefined if no success, unknown outcome, incomplete window or missing charge. Report launch wall seconds and context tokens separately as sums, each unknown if any constituent is unknown. |

An empty denominator is undefined. Report the point estimate and label
bounds separately; a rate is usable only with a point and a label band
no wider than **0.10**. Use a paired case bootstrap (10,000 draws, seed
355, same resampled case indices for each arm). Report the point interval
and, separately, a conservative decision envelope: use the 2.5th percentile
of treatment-low minus baseline-high and the 97.5th percentile of
treatment-high minus baseline-low. Endpoints use nearest rank `ceil(p * n)`
over defined draws (positions 1 and 39 for 40); report counts of undefined
draws, never replace them with zero. Calculations use rational arithmetic
and parse JSON decimals exactly. The report displays decimals as strings
rounded to six places; decisions do not read that display.

## Preregistered decision

The primary is **+0.10 weighted recall** (treatment minus baseline); the
lower end of its decision envelope must be strictly above zero and the
pooled label difference must support at least +0.10. Non-inferiority
margins: precision **−0.05**, clean-negative false positives **+0.05**,
harmful fixes **+0.05**, duplicate ratio **+0.10**, and **+50%** cost per
validated success (point estimate, both arms). The sample floor is **20**
paired defect cases and **10** paired negatives, not a power calculation.
At most **10%** of planned cases may be excluded (union across arms).

1. **NOT_EVALUATED** for fixture data or if either arm has no record.
2. **NO_GO** on measured harm: a new certified critical escape, an entire
   usable decision envelope beyond a non-inferiority margin, or measured
   cost above its margin. Compare shared case escapes only against a
   non-excluded baseline record that establishes no escape of that defect;
   a missing or unknown counterpart does not. A certified run-local
   treatment regression is new even without a counterpart. An excluded
   treatment run's escapes are judged the same way: an exclusion never
   hides measured harm.
3. **INCONCLUSIVE** if there is a missing pair (an exclusion is recorded,
   not missing), too many exclusions, or fewer than the sample floor.
4. **NO_GO** if the usable recall envelope lies strictly below +0.10.
5. **INCONCLUSIVE** for unresolved outcomes or critical novelty, unusable
   rates, margins not established, missing cost, or a primary effect not
   established. Otherwise **GO**.

There is no caller-supplied policy file. The scorer requires all of the
above for a GO, and R0 intentionally cannot measure real billing or context
tokens. To change a criterion, extend the evidence model, or make recorded
cost eligible, commit a dated amendment with its rationale, a separate
version/pin, independent collection requirements and tests **before** any
affected trial; never waive a criterion in the input or after seeing results.

## Replay and evidence limits

```sh
uv run python -m tests.review_evaluation verify
uv run python -m tests.review_evaluation score
uv run pytest tests/test_review_evaluation.py tests/test_fast_targets.py
```

`verify` checks all registered cases even without run rows. `score` uses
the same verification before producing JSON. Both accept `--study PATH`.
[`study.json`](../tests/fixtures/review_evaluation/study.json) and
[`report.json`](../tests/fixtures/review_evaluation/report.json) are the
synthetic conformance vector: three defects, three clean negatives,
confirmed/false/unknown findings, a repeat, failed retry, a repair, a
regression and certification with an empty set. The test regenerates the
input in memory and matches the report byte for byte. A separate Git-object
test cross-checks the snapshot hash against Git. These tests exercise
evidence admission and arithmetic, not a provider's quality.

The v1 input is a closed JSON object (`schema`, `mode`, `cutoff`, `cases`,
`runs`). The synthetic study is a worked example of each record:

- Case rows contain an ID, kind, risk, ordered `snapshots` (base to pinned
  candidate), and defect definitions. A snapshot has a full SHA, a flat
  regular UTF-8 file map and a `resolves` list; Git object bytes are
  reconstructed with the previous snapshot as parent and a fixed fixture
  identity, message and date. An oracle compares one path's blob with
  expected bytes (`null` means absence). It is evaluated on **the same map
  that was hashed**, at every relevant commit. No checkout, filters, shell,
  subprocess, source execution or network access is needed by replay.
  The first snapshot is the case's registered merge base. Every review of
  either arm diffs from it: the first at the pinned candidate and every
  later one. A review that names another merge base is refused, as is a
  review of a commit before the pin. Otherwise one arm could start from a
  base that already holds the defect while the other reviews the change
  that introduced it. R0 supports no base change (a rebase or a moved base
  branch); defining its effect on exposure needs an amendment.
- Run rows bind one arm to a case. They can extend the case's linear
  history with new snapshots and their own regression definitions; a FIX
  snapshot carries its launch ordinal and adjudicated label. That ordinal
  must follow a review, belong to no review, and precede every review of
  its commit or a later one. The review it follows is its source. The
  controller launches a fixer only from a needs-fix round it accepted, on
  that round's candidate. So the source must be a needs-fix reply that was
  not stale, and its candidate must be the FIX commit's parent. A launch row
  at the ordinal must be a FIX reply with the source's round. Admission
  refuses a contradiction, a clean or stale source included, whether or
  not launch rows are present. An absent row is missing evidence and only
  leaves the window incomplete. A source of unknown disposition leaves the
  terminal outcome unresolved.
  Review rows bind the controller disposition, reviewer reply, findings
  and optional validation and issuance receipts to the observed revision. Stop,
  creation, launch and human evidence are projections with separate opaque
  `source` references, not derived from each other.

The toy replay format is not a real GitHub or billing collector, a claim
that a human reviewed a real case, or a general test runner for arbitrary
Git histories. It enforces internal consistency, not authenticity of
source references or correctness of a human label. Real-tree oracles,
replacement histories, collection and trusted usage evidence need separate
work before using this contract on real trials (REV-R1, REV-R2, REV-R5).
Register a cutoff and holdout before collecting that corpus. Do not use
these reports as `validation.commands` evidence or to change controller
defaults, state protocol or merge gates.
