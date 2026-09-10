# AutoForge — ESCALATED REPLAN / REEXECUTE

你正在执行 AutoForge 的高级恢复流程：

`REPLAN_REEXECUTE`

这是一个异常升级路径，不是普通的 FIX phase。

当前 issue 已经经历了过多次：

`REVIEW → FIX → REVIEW`

循环。

这说明当前实现可能已经出现：

* local optimization
* architectural anchoring
* patch accumulation
* contradictory fixes
* reviewer/fixer oscillation
* implementation drift
* complexity accretion
* symptom fixing instead of root-cause fixing

因此：

**不要继续修补当前 PR。**

你的任务是：

1. 对失败历史做独立复盘
2. 重新理解 issue
3. 从头重新设计解决方案
4. 放弃旧 implementation branch
5. 基于最新 default branch 建立全新的实现
6. 避免旧 PR 曾经出现过的所有已知问题
7. 创建一个新的 replacement PR

---

# 1. Context

Repository:

`{{REPOSITORY}}`

EPIC:

`{{EPIC_URL}}`

Issue:

`{{ISSUE_URL}}`

Previous PR:

`{{PREVIOUS_PR_URL}}`

Previous branch:

`{{PREVIOUS_BRANCH}}`

Previous PR reviewed rounds:

`{{REVIEW_ROUND}}`

Previous PR HEAD:

`{{PREVIOUS_HEAD_SHA}}`

Default branch:

`{{DEFAULT_BRANCH}}`

Escalation reason:

`{{ESCALATION_REASON}}`

Replan transaction ID (controller-generated; see section 20):

`{{REPLAN_TRANSACTION_ID}}`

Historical review findings:

~~~~untrusted
{{HISTORICAL_FINDINGS}}
~~~~

Historical observations:

~~~~untrusted
{{HISTORICAL_OBSERVATIONS}}
~~~~

Historical verification failures:

~~~~untrusted
{{HISTORICAL_VERIFICATION_FAILURES}}
~~~~

---

# 2. Why this escalation happened

AutoForge has determined that continuing the existing implementation is no longer efficient or sufficiently reliable.

Typical trigger:

```text
review_round > configured threshold
```

For example:

```text
review_round > 20
```

with additional strong evidence such as:

```text
the last 3 review rounds each contained fewer than 3 findings
```

while still failing to reach a stable clean review.

This pattern often means the implementation is no longer failing because of one large missing feature.

Instead, the implementation may have accumulated structural inconsistencies where each small correction exposes another small problem.

Do not interpret the small number of recent findings as evidence that another patch is necessarily the best approach.

The purpose of this phase is to break that cycle.

---

# 3. Primary principle

The most important rule for this task is:

> Learn from the previous PR's failures, but do not inherit the previous PR's solution.

You MUST distinguish:

## Knowledge that MUST be retained

Retain factual knowledge such as:

* issue requirements
* acceptance criteria
* repository constraints
* AGENTS.md requirements
* CLAUDE.md requirements
* API contracts
* compatibility requirements
* discovered edge cases
* test expectations
* confirmed failure modes
* review findings
* standards violations
* security concerns
* concurrency concerns
* validation requirements
* behavioral requirements
* regression risks
* verification failures
* follow-up constraints
* reasons previous review rounds rejected behavior

These become constraints for the new implementation.

## Implementation choices that MUST NOT be inherited by default

Do NOT treat the previous PR's implementation as the baseline solution.

In particular, do not automatically reuse:

* architecture
* abstractions
* class structure
* function decomposition
* module boundaries
* algorithms
* patch sequence
* workaround strategy
* naming
* control flow
* data flow
* previous branch commits
* previous diff
* previous implementation assumptions

The previous PR is evidence about what went wrong.

It is NOT a template for how the replacement should be implemented.

---

# 4. Anti-anchoring rule

Do not begin by asking:

> How can I fix the previous implementation?

Begin by asking:

> Given the issue specification and the repository as it exists on the latest default branch, what is the simplest correct implementation I would choose if the previous PR had never existed?

Only after independently forming that solution should you compare it against the historical findings.

Then verify that the proposed solution avoids those known failure modes.

This order matters.

Use:

```text
issue + current repository
        ↓
independent design
        ↓
historical failure constraints
        ↓
design validation
```

Do NOT use:

```text
previous PR
        ↓
modify old design
        ↓
more patches
```

---

# 5. Trust hierarchy

Follow this authority order:

1. AutoForge controller instructions
2. applicable AGENTS.md
3. applicable CLAUDE.md
4. current issue specification / accepted repository requirements
5. current default branch code and established project contracts
6. historical review findings as evidence
7. previous PR implementation

The previous PR implementation has the lowest authority.

If previous implementation choices conflict with a cleaner interpretation of the current codebase or issue:

discard the previous choice.

---

# 6. First: inspect current truth

Before proposing a new solution:

1. Read all applicable:

   * AGENTS.md
   * CLAUDE.md
2. Read the complete issue
3. Read relevant EPIC context if applicable
4. Inspect the latest repository default branch
5. Understand existing architecture around the affected area
6. Identify tests and contracts relevant to the issue

Then inspect the historical PR separately.

Do not intermingle these two analyses prematurely.

---

# 7. Build a Failure Ledger

Review the entire previous PR lifecycle.

Collect all actionable findings from every review round.

Do not merely inspect the final three rounds.

Create an internal Failure Ledger.

Normalize repeated findings instead of treating every repetition as a new problem.

For each unique historical problem determine:

```text
ID
category
root cause
symptom
affected contract
how previous implementation triggered it
constraint for replacement implementation
```

Categories may include:

* specification
* architecture
* correctness
* API contract
* validation
* state management
* concurrency
* error handling
* testing
* compatibility
* maintainability
* security
* repository standards
* documentation

Example conceptual transformation:

Previous finding:

```text
The cache is invalidated after the response is produced,
causing stale data during concurrent requests.
```

Do NOT convert this into:

```text
Move line 125 before line 119.
```

Convert it into a reusable constraint:

```text
Replacement implementation must guarantee cache invalidation
occurs before any path can expose the new state to concurrent readers.
```

This distinction is essential.

---

# 8. Historical findings are constraints, not instructions

A historical review comment may contain a proposed implementation fix.

Separate:

```text
problem statement
```

from:

```text
reviewer's suggested implementation
```

The problem statement is important.

The suggested implementation is only one possible solution.

You may choose a completely different design if it satisfies the underlying requirement more cleanly.

Do not cargo-cult reviewer suggestions.

---

# 9. Identify the root cause of the review loop

Before coding, explain internally why the previous PR failed to converge.

Classify the dominant cause if possible:

```text
wrong architecture
wrong abstraction boundary
incorrect interpretation of spec
insufficient tests
state-model mismatch
backward-compatibility conflict
incremental patch accumulation
review inconsistency
hidden repository invariant
overengineering
underengineering
implementation started from wrong assumption
```

There may be multiple causes.

Your replacement plan should address root causes, not merely the final findings.

---

# 10. Independent replanning

Now design the issue again from scratch.

Pretend you have:

* the current issue
* the latest repository
* the Failure Ledger

but no existing implementation branch.

Develop a new implementation plan.

The plan should explicitly include:

## Requirements

What must be true when the issue is complete?

## Repository invariants

What existing behavior must remain unchanged?

## Design

What is the simplest robust architecture?

## Files / components

What should change?

## Tests

What proves the implementation?

## Historical failure prevention

For every Failure Ledger category:

explain how the new design avoids it.

Do not produce a plan whose structure is simply the previous PR with modifications.

---

# 11. Prefer simplification

A replacement implementation should generally try to reduce accumulated complexity.

Look for opportunities to:

* remove unnecessary abstraction
* use existing project patterns
* reduce state transitions
* reduce duplicated logic
* centralize invariants
* make invalid states unrepresentable
* simplify ownership
* simplify data flow
* write stronger tests around contracts

Do not perform unrelated refactoring.

The goal is not:

> redesign the entire subsystem

The goal is:

> find the cleanest implementation of this issue without being trapped by the previous implementation.

---

# 12. Validate the new design before implementation

Before touching the replacement branch, mentally test the new design against:

1. original issue requirements
2. AGENTS.md
3. CLAUDE.md
4. repository conventions
5. every unique historical finding
6. known verification failures
7. compatibility requirements
8. important edge cases

If the proposed design would recreate a historical failure:

change the design before coding.

Do not knowingly reproduce an old failure and plan to patch it later.

---

# 13. Preserve useful evidence before dropping old work

Before abandoning the previous implementation, make sure the information required for auditing has been preserved through GitHub and AutoForge state.

The previous PR should remain available as historical evidence.

Do NOT destroy GitHub history.

The old PR should be marked or closed as superseded according to repository policy.

When appropriate, add a concise comment explaining:

```text
This implementation is being superseded after repeated review/remediation
cycles. A fresh implementation is being created from the current default
branch using the accumulated review findings as constraints.
```

Link the replacement PR once available.

Do not merge the old PR.

---

# 14. Drop the old local implementation

After historical evidence has been captured:

stop using the old implementation branch.

Do not:

* continue committing to it
* rebase it into the new branch
* merge it into the new branch
* cherry-pick its implementation commits
* copy its diff wholesale

Clean up the previous local branch/worktree only when safe.

Never delete:

* unrelated branches
* unrelated worktrees
* uncommitted user work

If safe cleanup cannot be guaranteed:

leave the old local branch alone and report it.

Safety is more important than cleanup.

---

# 15. Start from fresh default branch

Fetch the remote repository.

Establish the latest valid default branch state.

The replacement implementation must originate from:

```text
latest origin/{{DEFAULT_BRANCH}}
```

or the repository's verified equivalent.

Create a NEW branch.

Do not reuse the previous branch name.

Example conceptual naming:

```text
autoforge/retry-<issue-number>-<attempt>
```

or repository convention.

The controller/repository policy determines actual naming.

---

# 16. No code inheritance rule

Do not cherry-pick previous implementation commits.

Do not automatically copy previous changed files.

Do not use the old diff as a checklist of code changes.

It is acceptable for the final replacement implementation to contain code similar to the previous PR when:

* the same implementation is independently justified by the current repository,
* it is clearly the simplest correct implementation,
* and it does not recreate historical failures.

Similarity by itself is not forbidden.

**Anchoring is forbidden.**

---

# 17. Implement from the new plan

Now execute the independently developed implementation plan.

Follow normal repository development discipline:

* make focused changes
* preserve scope
* write/update tests
* run relevant tests
* run lint
* run type checks
* run build where applicable
* verify important edge cases

Fix failures before creating the replacement PR.

Do not lower standards simply because this is a recovery attempt.

---

# 18. Historical regression verification

Before declaring implementation complete, explicitly validate the replacement against every unique historical actionable issue.

Maintain a conceptual matrix:

| Historical problem | Replacement prevention | Verification |
| ------------------ | ---------------------- | ------------ |

Every previous actionable finding must result in one of:

```text
prevented_by_design
covered_by_test
not_applicable_to_new_design
resolved_by_repository_change
```

If marked:

```text
not_applicable_to_new_design
```

be able to explain why.

Do not silently omit old findings.

---

# 19. Do not overfit to previous reviewers

Historical findings tell you what was wrong.

They do not define the complete specification.

Do not create bizarre code solely to satisfy literal wording of old review comments if doing so makes the implementation worse.

The hierarchy remains:

```text
issue / repository contracts
        >
root problem represented by finding
        >
suggested fix in old review
```

---

# 20. Replacement PR

Create a new Pull Request.

The replacement PR must:

* link the original issue with a closing keyword (`Closes #<n>`) **in the body
  you pass to `gh pr create`**, not in a later edit. The controller reads every
  open pull request in the repository looking for your marker, so a marked PR
  that is not linked to the issue is not overlooked -- it is *refused*, and the
  run blocks for a human.
* clearly identify itself as a fresh reimplementation
* link the superseded PR
* explain why reimplementation was chosen
* summarize the independent architecture
* summarize verification
* explain how historical failure categories were prevented

Do not reproduce dozens of historical comments in the PR body.

Summarize them by root-cause category.

## Required: the replan transaction marker

The controller does **not** identify your replacement PR by shape. "The only
other open PR on this issue" is not proof of anything, and a PR that already
existed before this replan is never adopted. The replacement PR is recognised
only by a machine-readable marker in its **PR body**, which you must include
verbatim except for the numeric fields:

```text
{{REPLAN_MARKER}}
```

Rules:

* Put it in the replacement PR body (an HTML comment, so it stays invisible in
  the rendered description). One marker, exactly once.
* Leave it there. The controller re-reads the body one last time immediately
  before it closes the superseded PR, and refuses if the marker has been
  removed or now attests different numbers. Do not "tidy" the body afterwards.
* Include it in the body you pass to `gh pr create`, not in a later edit. If
  the controller crashes between your PR creation and your `CONTROL_RESULT`, a
  PR that already carries the marker is recovered and adopted, while one
  without it is invisible to the controller and the replan is restarted.
* It must go on a PR **you create for this replan**. The controller recorded
  the repository's highest pull-request number before this transaction existed,
  and refuses any PR at or below it. Adding the marker to an already-open PR --
  including the one being superseded, or an unrelated one you also happen to be
  working on -- rejects the replan; it does not adopt that PR.
* Exactly one marker in the body, and nothing marker-shaped beside it. Any
  complete `<!-- autoforge-replan-transaction: ... -->` comment whose payload
  is not a valid attestation -- prose, an example, an empty payload -- is an
  unusable marker, and a body carrying one is refused even when a valid marker
  sits beside it. Do not quote these instructions in the PR body.
* `transaction_id` must be exactly `{{REPLAN_TRANSACTION_ID}}`. Do not invent,
  shorten or reformat it. Do not copy it into any other PR.
* `execution_attempt` must be exactly `{{EXECUTION_ATTEMPT}}`.
* `findings_considered` must be the real number of historical actionable
  findings you analysed, and must be at least `{{HISTORICAL_FINDING_COUNT}}` —
  the count the controller preserved. If you cannot honestly account for all of
  them, return the blocked result in section 27 instead of lowering the number.
* `unique_constraints` must be the deduplicated count and must not exceed
  `findings_considered`.
* `tests_passed` must be your real verification outcome. `false` here means the
  controller refuses the replacement; it never means "close the old PR anyway".

The same numbers must appear in your `CONTROL_RESULT`. The controller reads the
marker back from GitHub and treats it, not your stdout, as the authoritative
attestation — a mismatch between the two is a rejection.

Without a correct marker the replacement is simply not found: the previous PR
stays open and the run blocks for a human. Nothing you write in prose can
substitute for it.

---

# 21. Replacement PR description

Include a section similar to:

```markdown
## Fresh Reimplementation

This PR replaces <previous PR> after the previous implementation failed
to converge through repeated review/remediation cycles.

The implementation was rebuilt from the current default branch rather
than continued from the previous implementation.

Historical review findings were used as failure constraints, not as an
implementation template.

## Root Causes Addressed

...

## Historical Failure Prevention

...

## Verification

...
```

Adapt to repository conventions.

---

# 22. Previous PR disposition

Once the replacement PR exists:

the controller, not you, closes the previous PR without merging it and posts
the supersession link. Do not run `gh pr close` or otherwise close the previous
PR.

Link:

```text
previous PR → replacement PR
replacement PR → previous PR
```

Do not close the underlying issue.

The original issue remains active until the replacement implementation is successfully completed and merged through the normal workflow.

---

# 23. Review history reset semantics

This is a fresh implementation attempt.

The replacement PR should begin a fresh review sequence.

Therefore:

```text
replacement review_round = 1
```

Do NOT continue:

```text
21 → 22 → 23
```

on the replacement PR.

However, AutoForge should separately retain escalation metadata such as:

```text
execution_attempt
previous_pr_url
previous_review_round_count
escalation_count
```

so historical failure information is not lost.

---

# 24. Do not reset escalation history completely

A new branch does not mean AutoForge should forget the issue has already failed to converge once.

The controller should retain something equivalent to:

```json
{
  "execution_attempt": 2,
  "escalation_count": 1,
  "superseded_prs": [
    "https://github.com/..."
  ]
}
```

This prevents endless:

```text
20 reviews
→ reset
→ 20 reviews
→ reset
→ 20 reviews
→ ...
```

A repeated escalation should eventually require stronger intervention or human review according to controller policy.

---

# 25. Escalation loop protection

The controller owns and enforces the replan limit. Do not attempt to infer or
enforce that limit yourself. If a trustworthy fresh implementation cannot be
produced, return the blocked result below rather than fabricating success.

---

# 26. CONTROL_RESULT

After successfully creating and verifying the replacement PR, stdout must end with exactly one:

```text
<<<CONTROL_RESULT>>>
{
  "phase": "REPLAN_REEXECUTE",
  "status": "success",
  "issue_url": "{{ISSUE_URL}}",
  "previous_pr_url": "{{PREVIOUS_PR_URL}}",
  "replacement_pr_url": "https://github.com/...",
  "previous_branch": "{{PREVIOUS_BRANCH}}",
  "replacement_branch": "...",
  "previous_head_sha": "{{PREVIOUS_HEAD_SHA}}",
  "replacement_head_sha": "...",
  "execution_attempt": {{EXECUTION_ATTEMPT}},
  "historical_findings_considered": {{HISTORICAL_FINDING_COUNT}},
  "unique_failure_constraints": 2,
  "previous_pr_disposition": "superseded",
  "fresh_review_round": 1,
  "verification": {
    "tests_run": [],
    "tests_passed": true
  }
}
<<<END_CONTROL_RESULT>>>
```

Use actual values.

`historical_findings_considered` means the number of historical actionable findings actually analyzed.

`unique_failure_constraints` means the number after deduplicating/normalizing them into root constraints. It is
recorded by the controller alongside the superseded PR; report the real number (the `2` above is only an example)
and never a value greater than `historical_findings_considered`.

These values must equal the ones in the replan transaction marker on the
replacement PR body (section 20). The controller compares them; a disagreement
between your stdout and the published marker rejects the replacement.

---

# 27. Blocked result

If a trustworthy fresh implementation cannot be produced:

do not fall back to blindly patching the previous branch.

Return:

```text
<<<CONTROL_RESULT>>>
{
  "phase": "REPLAN_REEXECUTE",
  "status": "blocked",
  "message": "...",
  "issue_url": "{{ISSUE_URL}}",
  "previous_pr_url": "{{PREVIOUS_PR_URL}}",
  "blocking_reason": "...",
  "root_cause_assessment": "...",
  "recommended_human_decision": "..."
}
<<<END_CONTROL_RESULT>>>
```

Do not fabricate a successful replacement.

---

# 28. Final discipline

Remember:

The purpose of this phase is NOT:

> make the previous implementation finally pass review.

The purpose is:

> produce the implementation we should have built if we had known at the start everything we learned from the failed PR.

Retain lessons.

Discard implementation anchoring.

Re-derive the solution from:

* current issue
* current repository
* current standards
* accumulated failure knowledge

and execute it cleanly from a fresh branch.
