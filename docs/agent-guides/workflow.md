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
- A `FIX` entry that finds the PR HEAD past the reviewed HEAD goes back to `REVIEW` without launching the fixer (see **Re-entering a phase** below). That review of the actual HEAD is an ordinary round: it counts against the cap, and if it reports the same findings as the previous round it counts towards stagnation like any other repeated round. This is intended: the loop guard measures whether the PR is converging, and an unrecorded push that resolved nothing is not progress.
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

The same rule covers a review with findings. The open findings are bound to
`reviewed_head_sha`; a PR HEAD past it that the controller did not verify
(an unrecorded fix, an operator push) makes those findings findings of a
commit that is no longer the PR. Which of them the push resolved is not
knowable from controller state and is never inferred, so `FIX` is entered
only while `current_pr_head_sha == reviewed_head_sha`; otherwise the review
is marked stale and the actual HEAD is reviewed (`FIX -> REVIEW`).

### Re-entering a phase

`resume` re-enters the persisted phase, and the engine cannot tell a first
entry from a re-entry after an interrupted step (timeout, non-zero exit,
malformed result, verification failure, refused run-log write, crash). In
every one of those the agent may already have done its GitHub work. GitHub
is the source of truth, so every REMOTE phase that launches an agent reads
GitHub before launching anyone, in one place (`_remote_entry`), exactly as
`ANALYZE_EXECUTE` recovers an existing open PR. The correction relaunch
after a malformed result is a re-entry too and runs the same probe first:
an agent that posted, pushed or created and then lost its result block is
reconciled with, not relaunched unaware. The table of what each phase's
re-entry does is complete by construction: every phase with an agent prompt
has an entry, and a test holds the two tables together.

- `ANALYZE_EXECUTE` adopts the open PR carrying the issue's
  `ai-implementation` marker (the persisted PR, or the one found by a
  strict listing of the repository's open PRs), if one exists, without
  launching the agent; two candidates block, and so does a listing that
  cannot be proven complete, since "none exists" is then not knowable. A
  PR is identified by that marker alone, never by its branch name or a
  linked issue; the read-back after the agent holds the reported PR to the
  same rule (github-safety.md, "Before ANALYZE_EXECUTE").
- `REVIEW` reads the PR comments for the `ai-review-result` marker of the
  upcoming round at the bound HEAD. One such comment is handed to the
  reviewer (`EXISTING_REVIEW_COMMENT_URL`) to adopt, or to edit in place,
  instead of posting a second one. Two or more is a state the controller
  cannot resolve without choosing which review is the round's, so it enters
  `BLOCKED` without invoking anyone and names the comments. A comment for the
  same round at another HEAD is not this round's and is ignored. After the
  reviewer returns, verification enforces that the round still has exactly
  one comment at its HEAD; a reviewer that posted a second one has its round
  rejected, and the next entry blocks on the pair. The same entry lists the
  open issues carrying this PR's `ai-follow-up` marker for any finding id
  (strictly; a listing that cannot be proven complete blocks) and hands
  them to the reviewer (`EXISTING_FOLLOW_UP_ISSUES`), so a problem an
  earlier round deferred is not raised again under this round's ids.
- `FIX` re-reads the PR HEAD. Past the reviewed HEAD: `FIX -> REVIEW` of the
  actual HEAD, no fixer launched (the rule above); a fixer whose push landed
  but whose result was never recorded is therefore never relaunched against
  findings its push may have resolved. Equal to it: the repository's open
  issues are listed for the `ai-follow-up` marker of (this PR, an open
  finding), because a `follow_up_created` resolution creates an issue and
  moves no HEAD. One per finding is handed to the fixer (`FOLLOW_UP_ISSUES`)
  to report instead of recreate; two for one finding block; a listing that
  cannot be proven complete blocks, since "none exists" is then not
  knowable. Read-back holds the fixer to the same rule. The issues the
  listing found for earlier rounds' findings are handed over as well
  (`EXISTING_FOLLOW_UP_ISSUES`), so a re-raised problem is recorded on the
  issue that exists (a second marker in its body) rather than in a second
  issue.
- `REPLAN_REEXECUTE` replays its durable transaction (replan-transaction.md).
- `UPDATE_EPIC` reads the EPIC's comments for the `ai-epic-progress` marker
  of (finished issue, merged PR). One is handed to the agent
  (`EXISTING_PROGRESS_COMMENT_URL`) to adopt instead of posting a second;
  two or more block. The bounded re-selection after a rejected
  `next_issue_url` is a re-entry and adopts the comment the same way. After
  the agent returns, the EPIC must carry exactly one such comment.

No probe consumes a review round, a `review_history` entry, or an attempt.

---

## Findings and the review invariant

What counts as a Finding (versus an Observation), the stable finding-ID
convention, and the `needs_fix_round == (actionable findings > 0)` invariant
are specified with the rest of the review result contract in
[control-result-protocol.md](control-result-protocol.md).
