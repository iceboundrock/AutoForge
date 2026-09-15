# Workflow: state machine, review/fix routing, loop bounds

Read this before changing phase orchestration, legal transitions, review-round
routing, the REVIEW/FIX loop bounds, stagnation detection, replan *policy*
(when a replan is decided), or `resume` behaviour. The REPLAN_REEXECUTE
*transaction* (what happens once a replan is decided) is specified in
[replan-transaction.md](replan-transaction.md).

Owning code: `src/autoforge/transitions.py` (phases, `LEGAL_EDGES`,
`LOCAL_LEGAL_EDGES`, `next_phase`), `src/autoforge/engine.py` (phase
orchestration), `src/autoforge/profiles.py` (routing), `src/autoforge/loop_guard.py`
(loop bounds and stagnation), `src/autoforge/replan.py` (replan policy). The
edge tables in `transitions.py` are the authoritative list of legal edges,
including the re-review edges taken when a PR's HEAD drifts after a review;
the flow below is the policy those tables implement. Configuration keys quoted
here are defined in `src/autoforge/config.py` and `autoforge.example.yaml`.

---

## Core state machine

AutoForge phases currently include or are expected to include:

```text
INITIALIZING
ANALYZE_EXECUTE
REVIEW
FIX
REPLAN_REEXECUTE
READY_FOR_MERGE
MERGE
UPDATE_EPIC
DONE
BLOCKED
FAILED
```

Keep transition rules centralized and testable.

Expected flow:

```text
INITIALIZING -> ANALYZE_EXECUTE
ANALYZE_EXECUTE -> REVIEW
REVIEW -> FIX                 when needs_fix_round == true
FIX -> REVIEW
REVIEW -> REPLAN_REEXECUTE    when controller replan policy escalates
REPLAN_REEXECUTE -> REVIEW    replacement PR, fresh round 1
REVIEW -> READY_FOR_MERGE     when needs_fix_round == false
```

`REVIEW` is `REPLAN_REEXECUTE`'s only entry and its only exit: it is the one
phase where the controller closes an open PR, so a second edge in or out would
be a second way into that destructive step.

Later milestones may enable:

```text
READY_FOR_MERGE -> MERGE
MERGE -> ANALYZE_EXECUTE
MERGE -> UPDATE_EPIC
MERGE -> DONE
UPDATE_EPIC -> ANALYZE_EXECUTE
UPDATE_EPIC -> DONE
```

Illegal transitions must fail explicitly. Do not silently coerce an invalid state into a valid one.

---

## Review-round routing

The logical review routing policy is:

```text
round 1     -> OpenCode + GPT 5.6 Luna  + high
round 2-5   -> OpenCode + GPT 5.6 Terra + high
round 6+    -> OpenCode + GPT 5.6 Sol   + medium
```

Implementation and remediation use:

```text
ANALYZE_EXECUTE -> Claude Code + Fable + high
FIX             -> Claude Code + Fable + high
```

Later EPIC maintenance profiles are expected to use the configured OpenCode profile for Muse Spark 1.3 Free.

`MERGE` has no agent profile: the controller performs the merge itself behind the merge safety gate (see [github-safety.md](github-safety.md), "Merge safety").

These are logical profiles. Real provider model identifiers and CLI flags belong in configuration/provider mapping, not in state-machine logic.

Review round increments only after a valid review/fix lifecycle transition. Failed invocations must not accidentally increment it.

---

## Loop bounds

The REVIEW/FIX cycle must be bounded by the controller, never by prompt wording:

```yaml
workflow:
  max_review_rounds: 20                 # completed review rounds per PR
  stagnation_identical_rounds: 2        # identical required_resolution texts
  stagnation_unchanged_count_rounds: 3  # unchanged count + a recurring resolution
  max_total_steps: 300                  # cumulative steps of the run
```

- After a review with findings, the controller evaluates replan policy before blocking for the per-PR cap or stagnation. An eligible replan, including one caused by workflow stagnation or the cap, enters `REPLAN_REEXECUTE`; an exhausted replan limit enters `BLOCKED`. Otherwise a review round at the cap enters `BLOCKED` with a clear `block_reason`; no further FIX round is started because its result could never be reviewed. A clean round at the cap proceeds normally. Entering `REVIEW` beyond the cap (stale re-review, HEAD drift, resume) is refused before the reviewer runs.
- Stagnation is judged on the persisted per-PR `review_history` (round, reviewed SHA, result, finding count, fingerprint of the normalised `required_resolution` texts, per-finding digests of those texts). Only trailing consecutive rounds that ended with findings count; a clean or stale round breaks the streak. The unchanged-count rule also requires a `required_resolution` that recurs within the window (A/B/A ping-pong): rounds of entirely new findings, each earlier one resolved, are progress bounded by the round cap only. A value of 0 disables a rule and 1 is rejected by the config loader: both rules compare consecutive rounds, so a one-round window would silently disable the unchanged-count rule instead of bounding it. The per-round digest list is bounded (`MAX_PERSISTED_RESOLUTION_DIGESTS`) and a clipped round is marked; incomplete evidence can still prove a recurrence but never its absence, so such a window keeps the count-only behaviour. A `required_resolution` that normalises to nothing gets no digest and can never form a recurrence. Persisted `review_history` is validated on load: only a *missing* `resolutions` key is old-controller compatibility; a present malformed field is corruption and fails loudly. A detected stagnation is an eligible replan trigger only from `review.replan.soft_threshold` onwards; below that round it is an immediate block, because a short identical-resolution streak is usually one FIX round that missed a finding and is not worth discarding the PR for. `review.replan.soft_threshold` is authoritative over `workflow.stagnation_*`. The "entirely new findings are progress" scoping belongs to the `workflow.stagnation_*` rules alone: the replan window rule (`review.replan.stagnation_window` trailing rounds with findings, each holding at most `review.replan.max_findings_per_round`, at or after `soft_threshold`) deliberately counts rounds of entirely new findings too. Recurrence is what the workflow rules already detect and hand to the policy, so a recurrence requirement would leave the window rule nothing of its own; it exists for the long tail of small, fresh findings that never ends, and the threshold is what protects a productive loop from it. There is no separate recovery policy to be authoritative over: a fresh `REPLAN_REEXECUTE` step and a `resume` run the same reducer over the same persisted transaction, and a refusal is persisted as a terminal `REJECTED` stage that `resume` replays. Recovery may replay a decision, never launder one.
- The replan transaction itself (evidence completeness, causal provenance,
  the checkpointed close with compensation, ownership of the side effect,
  verified activation, rejection monotonicity and crash idempotency) is
  specified in [replan-transaction.md](replan-transaction.md). Read it before
  touching anything `REPLAN_REEXECUTE` does after the policy has decided.
- The step budget is measured on the persisted cumulative `step_count`, which is never reset by `resume` or by switching issues. CLI `--max-steps` bounds a single invocation only.
- Failed invocations consume neither a review round nor a `review_history` entry.
- Hitting any bound is `BLOCKED` (terminal). The open findings and the PR stay for a human; nothing is merged.

---

## Bind reviews to PR HEAD SHA

A clean review is valid only for the exact commit it reviewed.

Persist the reviewed HEAD SHA.

Before accepting a clean review or allowing a future merge, verify:

```text
current_pr_head_sha == reviewed_head_sha
```

If the PR HEAD changes after review, the prior clean review is stale and the PR must return to `REVIEW`.

Never merge code that has changed since the latest clean review.

---

## Findings and the review invariant

What counts as a Finding (versus an Observation), the stable finding-ID
convention, and the `needs_fix_round == (actionable findings > 0)` invariant
are specified with the rest of the review result contract in
[control-result-protocol.md](control-result-protocol.md).
