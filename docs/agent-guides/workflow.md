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

Operator edges, taken only by `autoforge unblock` (never by
`decide_next_phase`, `step` or `resume`, which still treat `BLOCKED` as
terminal):

```text
BLOCKED -> ANALYZE_EXECUTE | REVIEW | FIX | READY_FOR_MERGE | UPDATE_EPIC
```

`BLOCKED -> REPLAN_REEXECUTE` is deliberately absent (see the edge rule
above); `DONE` and `FAILED` have no outgoing edges; the LOCAL table has no
unblock edge. See **Leaving BLOCKED** below.

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
- The step budget is measured on the persisted cumulative `step_count`, which is never reset by `resume` or by switching issues. CLI `--max-steps` bounds a single invocation only. It is checked before a step executes, with one exception: a `REPLAN_REEXECUTE` journal past the destructive write (`SUPERSEDE_INTENT`, `COMPENSATING`, `SUPERSEDED`) is finished first. There the persisted intent is a decision the controller has committed to, the remaining work is read-verify-activate or read-verify-reopen, no agent is invoked and nothing is closed from a resume; blocking with the plain budget text would leave a source PR the controller closed with nothing in the run's state saying so, and `resume` refuses `BLOCKED`, so raising the budget could not repair it. The finishing step still counts, so the budget ends the run at the next phase boundary. Stages before the write (`PENDING`, `PREPARED`, `VERIFIED`) block as any other phase does: nothing was closed, so blocking costs nothing and the step does not count. A journal the reducer will refuse anyway (`REJECTED`, or one that cannot be read whole and so cannot prove it is before the write) is handed to the reducer too, whose refusal names the transaction and the source PR's fate. The partition is `replan_txn.budget_may_stop`; dry-run plan notes describe the same outcome. A step is charged when it first persists: the launch checkpoint of an agent step, or the resolution of a step that needs no agent. An entry read that fails transiently (`GitHubUnavailableError`) before either persists nothing -- the count, the phase and any journal are byte-identical on disk -- so it is not a step and is not charged, in every phase alike; the operator's next `resume` re-reads with the same budget. The exemption inherits this rule: a GitHub outage while finishing a post-write journal defers the finish to the next `resume` and never repeats or extends it (no agent, no write), and the finish is counted once when it lands.
- Failed invocations consume neither a review round nor a `review_history` entry.
- A `FIX` entry that finds the PR HEAD past the reviewed HEAD goes back to `REVIEW` without launching the fixer (see **Re-entering a phase** below). That review of the actual HEAD is an ordinary round: it counts against the cap, and if it reports the same findings as the previous round it counts towards stagnation like any other repeated round. This is intended: the loop guard measures whether the PR is converging, and an unrecorded push that resolved nothing is not progress.
- A stale round (the HEAD or base moved while the reviewer worked) is a completed round too: it is consumed, recorded in `review_history` and breaks the stagnation streak, but its findings are not discarded (see **Stale rounds keep their findings** below). Consuming the round is what keeps the cap in force however often the revision moves; a round that was not consumed could be repeated without bound by pushes alone.
- Hitting any bound is `BLOCKED` (terminal). The open findings and the PR stay for a human; nothing is merged.

---

## Bind reviews to PR HEAD SHA and to the PR identity

A clean review is valid only for the exact change it reviewed: the commit,
on the PR it was posted on, against that PR's base branch at the time. The
same commit proposed as another PR (the same branch against another base)
is a change no review decided on.

Persist the reviewed revision with the review: the reviewed PR
(`reviewed_pr_url`), the reviewed HEAD SHA (`reviewed_head_sha`) and the
reviewed base branch (`reviewed_base_ref`). The controller reads all three
from GitHub right before the reviewer is launched (`current_head_sha`,
`current_base_ref`) and records them when the round is accepted. The
round's comment carries the same revision in its `ai-review-result` marker
(`reviewed_head_sha`, `reviewed_base_ref`), so the durable copy of the
review on GitHub identifies the diff it decided on and a later entry can
only ever adopt a comment for the base the PR targets now (see
**Re-entering a phase**).

The base is bound by name, not by the base branch's tip: the PR diff a
reviewer reads is HEAD against the merge base, which ordinary commits on
the base branch do not move, and the merge gate already defers to GitHub's
own `mergeStateStatus` (`BEHIND` is refused) for the "must be up to date
with the base" policy the repository configured. Pinning the merge-base
commit as well would also catch a base branch rewritten under the same
name; that is tracked as #96.

Before accepting a clean review or allowing a future merge, verify:

```text
same_target(current_pr_url, reviewed_pr_url)   # repository + number, never string equality
same_target(pr_returned_by_github, reviewed_pr_url)
current_pr_head_sha == reviewed_head_sha
current_pr_base_ref == reviewed_base_ref
```

The identity checks come first, from state alone, and fail closed: a
`current_pr_url` that is not the reviewed PR, or a GitHub read that answers
the URL with another PR, is `BLOCKED` before anything else is read from it.
The binding is never moved to the current URL, and a protocol-2 state file
parked in `READY_FOR_MERGE` / `MERGE` (written before the binding existed)
is refused at load rather than bound after the fact.

If the PR HEAD or base changes after the review, the prior clean review is
stale and the PR must return to `REVIEW`, where the next round is bound to
the actual revision. A PR that GitHub reports as `MERGED` at the reviewed
HEAD but into another base is never counted (`BLOCKED`).

The same binding governs the other decision a review can make. A review
that routes to `REPLAN_REEXECUTE` records the base it was bound to in the
replan transaction, and the source PR may be checkpointed and later closed
only while it still targets that base ([replan-transaction.md](replan-transaction.md),
"The decision point is what may be closed").

A branch name is compared as GitHub reports it and is never interpreted;
where it is written into an HTML-comment marker (the prompt's
`REVIEWED_BASE_REF_JSON`, every marker renderer) it goes through
`claims.marker_json`, which escapes `<` and `>` as JSON does so that a valid
refname such as `x-->y` cannot end the marker early and leave the round's
comment unreadable.

Never merge code that has changed since the latest clean review.

The same rule covers a review with findings. The open findings are bound to
`reviewed_head_sha`; a PR HEAD past it that the controller did not verify
(an unrecorded fix, an operator push) makes those findings findings of a
commit that is no longer the PR. Which of them the push resolved is not
knowable from controller state and is never inferred, so `FIX` is entered
only while `current_pr_head_sha == reviewed_head_sha`; otherwise the review
is marked stale and the actual HEAD is reviewed (`FIX -> REVIEW`), with the
findings carried to it as prior findings to re-check.

### Stale rounds keep their findings

A round goes stale in two places: the post-review read finds the HEAD or
base moved while the reviewer worked (`REVIEW -> REVIEW`), or the `FIX`
entry finds the HEAD past the reviewed one (`FIX -> REVIEW`). In both, a
verified review reported findings that no fixer ever resolved, and the
controller cannot tell which of them the newer commits resolved. Dropping
them would let the next reviewer, a different profile with no memory of
the round, return a clean verdict on a PR that never received a FIX for
them (#14). So the round is consumed, but its findings move from
`open_findings` (which a fixer must resolve, bound to `reviewed_head_sha`)
to `prior_findings`, which no fixer is launched against and which the next
`REVIEW` entry renders into the prompt (`PRIOR_FINDINGS`, with the round,
HEAD and comment URL they came from) as findings to re-check at the actual
HEAD. The reviewer is told to re-raise each one that still applies under
this round's ids and to account for each one that no longer does under
Observations; whether it did is a semantic judgement the controller does
not verify, the same as any other finding the reviewer chooses to raise or
not.

The carry is replaced, never accumulated: a later stale round's findings
replace the earlier ones (that reviewer was shown them and re-raised the
ones that still applied; a stale clean round therefore leaves none), and
the next completed round of the actual revision, clean or with findings,
clears them. Only a completed round replaces or clears the carry: an
operator `unblock` into `REVIEW` after a stale round (see **Leaving
BLOCKED**) is not a round and leaves it as it is. Like `open_findings` they
are per PR: a replacement PR and a new issue start with none. The
alternative, not consuming a stale round, was rejected: it would relaunch
the same round profile against a PR that may move again, and it would let
pushes alone repeat a round without the cap ever counting it.

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

Every probe below and the read-back after the agent consume one identity
model, `src/autoforge/claims.py`: one exact-schema decoder per marker kind,
one renderer, one scan that classifies *every* marker it meets, and
explicit cardinality (`at_most_one` at entry, `exactly_one` on read-back).
A marker the controller cannot read, a second single-kind marker on one
object, or the same follow-up marker twice on one issue is a *defect* of
that object, not "no marker": while it exists, "nothing claims this key" is
not provable, so the entry enters `BLOCKED` naming the object (no agent is
launched) and the read-back rejects the result. The entry never adopts
what the read-back would refuse, and the read-back never accepts what the
next entry could not find again.

- `ANALYZE_EXECUTE` adopts the open PR carrying the issue's
  `ai-implementation` marker (the persisted PR, or the one found by a
  strict listing of the repository's open PRs), if one exists, without
  launching the agent; two candidates block, and so does a listing that
  cannot be proven complete, since "none exists" is then not knowable. A
  PR is identified by that marker alone, never by its branch name or a
  linked issue; the read-back after the agent holds the reported PR to the
  same rule (github-safety.md, "Before ANALYZE_EXECUTE"), and so does the
  replan transaction for its replacement PR (replan-transaction.md).
- `REVIEW` reads the PR comments for the `ai-review-result` marker of the
  upcoming round at the bound HEAD *and base* (both re-read from GitHub by
  this entry). One such comment is handed to the reviewer
  (`EXISTING_REVIEW_COMMENT_URL`) to adopt, or to edit in place, instead of
  posting a second one. Two or more is a state the controller cannot
  resolve without choosing which review is the round's, so it enters
  `BLOCKED` without invoking anyone and names the comments. A comment for
  the same round at another HEAD, or against another base, or whose marker
  names no base (written before the marker recorded one), is not this
  round's and is ignored: a review of the diff against the old base is not
  a review of the diff against the one the PR targets now, and adopting it
  would record the new base as reviewed. After the reviewer returns,
  verification enforces that the round still has exactly one comment at
  its HEAD and base; a reviewer that posted a second one, or that adopted
  a comment for another base, has its round rejected, and the next entry
  blocks on a pair. The same entry lists the
  open issues carrying this PR's `ai-follow-up` marker for any finding id
  (strictly; a listing that cannot be proven complete blocks) and hands
  them to the reviewer (`EXISTING_FOLLOW_UP_ISSUES`), so a problem an
  earlier round deferred is not raised again under this round's ids.
- `FIX` re-reads the PR HEAD. Past the reviewed HEAD: `FIX -> REVIEW` of the
  actual HEAD, no fixer launched (the rule above), the open findings carried
  to that review as prior findings to re-check; a fixer whose push landed
  but whose result was never recorded is therefore never relaunched against
  findings its push may have resolved, and the findings its push did not
  resolve are not lost either. Equal to it: the repository's open
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
  two or more block. It also reads the EPIC body and locates the managed
  roadmap section (ambiguous markers block without launching). The bounded
  re-selection after a rejected `next_issue_url` is a re-entry and adopts
  the comment the same way. After the agent returns, the EPIC must carry
  exactly one such comment; the roadmap section is written by the
  controller when due (github-safety.md, "EPIC updates").

No probe consumes a review round, a `review_history` entry, or an attempt.

### Leaving BLOCKED: the operator's unblock

`BLOCKED` is where the controller parks a run whose safe state it could not
determine (two candidate PRs, a closed PR, an exhausted bound, a failing
check, ...). `autoforge unblock --reason ...` is the only exit that is not a
new run (issue #5). It is a controller decision, not an operator override:
the operator supplies the reason, the controller chooses the phase, and it
chooses from live GitHub, never from the operator's claim of what was fixed.

- **Precondition.** The run must be `BLOCKED` (any other phase is a
  `StateTransitionError`) and REMOTE; a LOCAL run has no unblock edge. The
  reason is required, non-empty and bounded (`MAX_UNBLOCK_REASON_CHARS`).
- **Inspection.** Before GitHub is asked, a persisted replan journal (in
  flight or `REJECTED`) refuses, because `REVIEW` is `REPLAN_REEXECUTE`'s
  only entry and the operator path never replays that transaction; an
  exhausted `workflow.max_total_steps` refuses, naming the setting to raise.
  Then the recovery inspection of the phase being re-entered runs against
  GitHub: with no PR bound, the issue must be selectable and the strict
  marker listing of `ANALYZE_EXECUTE`'s entry must resolve (one adoptable PR
  or none), and a conclusive GitHub failure of either read refuses exactly
  as it does with a PR bound; with a PR bound, the PR is re-read and its
  state, HEAD and base are compared with the persisted review binding.
- **Identity first.** A review binding (`reviewed_pr_url`) that names a PR
  other than `current_pr_url` refuses before the live PR state is even
  considered, as the merge gate does from state alone: review evidence is
  a decision about one PR and is neither a verdict to merge on nor findings
  to carry into another PR's review. An empty binding is "no completed
  review" and decides as below.
- **Decision table** (PR bound). `OPEN` at the reviewed HEAD and base with a
  clean review -> `READY_FOR_MERGE` (the merge gate re-verifies from there);
  at the reviewed HEAD and base with open findings -> `FIX` with the findings
  open, unless the next review round is past `workflow.max_review_rounds`,
  which refuses; any other revision, or no completed review -> `REVIEW`
  (past the cap: refuse), where the persisted review evidence does not
  describe the revision that round will bind: the last result, if there is
  one, is marked `stale` whatever it was (a clean verdict included), and the
  open findings, if any, replace `prior_findings` exactly as HEAD drift
  does. A carry an earlier stale round already made (`prior_findings` set,
  `open_findings` empty) is preserved untouched: an unblock is not a review
  round, so no reviewer has examined it yet and the "replace, never
  accumulate" rule of **Stale rounds keep their findings** does not apply.
  `MERGED` and already counted -> `UPDATE_EPIC`; `MERGED`, not counted,
  at the clean-reviewed HEAD into the reviewed base -> `READY_FOR_MERGE`, so
  `resume --allow-merge` reconciles and counts it once; `MERGED` at any
  other revision, or with no clean review, refuses (the controller will not
  count a merge no review decided on). `CLOSED` refuses (reopen or
  reimplement is the operator's decision). A PR that another PR answers
  for, or that cannot be read conclusively, refuses; a transient GitHub
  failure propagates and decides nothing.
- **Refusal.** A refusal leaves the state file byte-identical: the run stays
  `BLOCKED` with its reason, and only the run log records the attempt
  (`<run_id>/NNN-blocked-unblock-1/`, with the operator's reason, the block
  reason and the detail, redacted).
- **Re-entry.** The chosen phase goes through `validate_transition(BLOCKED,
  target)` like any other step; the operator edges are declared in
  `LEGAL_EDGES` (`UNBLOCK_TARGETS`), never coerced, and checked before the
  run-log record is written, so an edge the topology refuses leaves neither
  a state write nor a record claiming an applied unblock. `current_head_sha`,
  `current_base_ref` and `current_branch` are rebound from the live PR; the
  review binding (`reviewed_*`) is never rewritten by this path. The action is
  appended to `unblock_history` (timestamp, reason, cleared block reason,
  phase, detail) and to the run log, `block_reason` is cleared and `attempt`
  reset. No agent runs and no step is charged: the operator reviews the
  outcome and runs `resume`, whose entry probe for that phase runs again.
- **Dry-run** reads GitHub, reports the phase it would re-enter or the
  refusal, and writes nothing (no state, no log, no lock).

---

## Findings and the review invariant

What counts as a Finding (versus an Observation), the stable finding-ID
convention, and the `needs_fix_round == (actionable findings > 0)` invariant
are specified with the rest of the review result contract in
[control-result-protocol.md](control-result-protocol.md).
