# AutoForge: ESCALATED REPLAN / REEXECUTE

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
7. 在本 worktree 的 detached `HEAD` 上提交新的实现，并给出 replacement PR 的
   title 和 body；push 与创建 replacement PR 由 controller 完成（section 20）

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

Default branch head (the base the replacement must be built on, already
fetched into the object store this worktree shares):

`{{BASE_SHA}}`

Replacement branch the controller will push your commit to (derived by the
controller; yours to read about, not to create):

`{{REPLACEMENT_BRANCH}}`

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
review_round >= configured threshold
```

For example:

```text
review_round >= 20
```

with additional strong evidence such as:

```text
the last 3 review rounds each contained fewer than 3 findings
```

while still failing to reach a stable clean review.

The failures at this point usually do not come from one large missing feature.

More often the implementation has accumulated structural inconsistencies where each small correction exposes another small problem.

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

Compare your solution against the historical findings only after you have independently formed it; the order matters.

Then verify that the proposed solution avoids those known failure modes.

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

A replacement implementation should generally reduce accumulated complexity.

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

The controller, not you, owns the previous PR's lifecycle. The controller
closes it without merging once the replacement is verified. You must not
close, mark, comment on, or otherwise modify the previous PR:

* Do not run `gh pr close` on the previous PR.
* Do not run `gh pr comment`, `gh pr edit`, or any other command that marks
  it as superseded.
* Do not post the "being superseded" explanation yourself; the controller
  posts the supersession link when it closes the PR.

Describe the linkage only inside the replacement PR body (section 21).

Do not merge the old PR. Never merge a pull request.

---

# 14. Drop the old local implementation

After historical evidence has been captured:

stop using the old implementation branch.

Do not:

* continue committing to it
* rebase it into the new implementation
* merge it into the new implementation
* cherry-pick its implementation commits
* copy its diff wholesale

Do not touch the old local branch or any worktree other than the one you
were launched in. Local branches and the other worktrees are operator-owned:
the operator's checkout, uncommitted work, and existing branches must be left
exactly as found.

* Do not run `git branch -D`, `git branch -d`, `git push --delete`, or any
  equivalent that deletes the previous branch.
* Do not run `git worktree add`, `git worktree remove`, `git worktree move`,
  `git worktree prune`, or otherwise create, clean up, move, or delete any
  worktree (yours included: the controller owns its lifecycle).
* Do not delete unrelated branches, unrelated worktrees, or uncommitted user
  work.

Do not create any branch either: you work on a detached `HEAD` (section 15),
and the controller pushes your commit to the replacement branch it derived.

Leave the old local branch alone and report it.

Safety is more important than cleanup.

---

# 15. Start from fresh default branch

The controller already read the head of `{{DEFAULT_BRANCH}}` and fetched it
into the object store this worktree shares. Do not fetch, pull or push, and
run no other command that contacts the git remote.

The replacement implementation must originate from exactly:

```text
{{BASE_SHA}}
```

Run `git checkout --detach {{BASE_SHA}}` and stay on the detached `HEAD`: do
not create, check out or rename a local branch (`git checkout -b`,
`git switch -c`, `git branch`), and do not reset the worktree to anything
that does not descend from `{{BASE_SHA}}`.

You choose no branch name. The controller pushes the commit you report to
`{{REPLACEMENT_BRANCH}}`, a branch it derives from the issue and the replan
transaction id, after checking that the branch does not exist yet. A branch
or a replacement PR for this transaction that the controller did not create
from its own journal stops the run: it is never adopted.

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

Fix failures before returning the result.

Commit locally on the detached `HEAD`, with clear messages referencing
`#{{ISSUE_NUMBER}}`. A commit message must not use a closing keyword
(`close`, `fix`, `resolve` in any form) followed by any issue other than
`#{{ISSUE_NUMBER}}`, and must contain nothing shaped like a credential: the
controller reads every commit it would publish and asks you to correct the
result otherwise. Leave the working tree clean.

Read the commit to report with `git rev-parse HEAD`. It must differ from
`{{BASE_SHA}}` (you must have committed something) and descend from it.

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

You publish nothing. The replacement's two GitHub writes belong to the
controller, and it journals both before either is sent:

1. it pushes the exact commit you report to `{{REPLACEMENT_BRANCH}}`,
   compared against "absent";
2. it creates the replacement PR from `{{REPLACEMENT_BRANCH}}` onto
   `{{DEFAULT_BRANCH}}`, with your title and with your body followed by its
   own `Closes #{{ISSUE_NUMBER}}` line, the issue's implementation marker and
   the replan transaction marker for transaction `{{REPLAN_TRANSACTION_ID}}`,
   which it renders from your `CONTROL_RESULT` counts.

So do not push, and do not create, edit or comment on any pull request or
issue (no `gh pr create`, no `gh pr edit`). Do not write `Closes #<n>`, the
implementation marker or the transaction marker anywhere: the controller adds
them, and a PR carrying this transaction's marker that it did not create is
refused, never adopted.

The PR title is one line of at most {{MAX_PR_TITLE_CHARS}} characters. The PR
body is Markdown of at most {{MAX_PR_BODY_CHARS}} characters, and must:

* clearly identify itself as a fresh reimplementation
* link the superseded PR
* explain why reimplementation was chosen
* summarize the independent architecture
* summarize verification
* explain how historical failure categories were prevented

Do not reproduce dozens of historical comments in the PR body.

Summarize them by root-cause category.

## What published text may contain

`pr_title` and `pr_body` are published on GitHub as you return them. Either
is rejected, and you are asked to correct it, when it contains:

- an HTML comment opening a controller marker (`<!-- ai-` or
  `<!-- autoforge-`, in any spacing or case);
- anything shaped like a credential (a token, a key, an authorization
  header, a URL with a password);
- a closing keyword followed by an issue reference (`Closes #12`,
  `fixes owner/repo#3`, `Resolves GH-4`), even inside code: GitHub would
  act on it, and the controller links this issue itself;
- an `@` that would mention a user or team outside a code span or a fenced
  block. Put such tokens in a code span (`` `@name` ``).

## The counts the transaction marker attests

The controller identifies the replacement PR by the transaction marker it
renders, never by shape, and treats that marker as the authoritative
attestation when it binds the PR, before it closes the superseded PR, and
after a crash. It renders the marker from your result:

* `execution_attempt` must be exactly `{{EXECUTION_ATTEMPT}}`.
* `historical_findings_considered` must be the real number of historical
  actionable findings you analysed, and must be at least
  `{{HISTORICAL_FINDING_COUNT}}`, the count the controller preserved. If you
  cannot honestly account for all of them, return the blocked result in
  section 27 instead of lowering the number.
* `unique_failure_constraints` must be the deduplicated count and must not
  exceed `historical_findings_considered`.
* `verification.tests_passed` must be your real verification outcome. `false`
  means the controller refuses the replacement before anything is pushed; it
  never means "close the old PR anyway".

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

Once the controller has created and verified the replacement PR:

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

A fresh implementation does not mean AutoForge should forget the issue has already failed to converge once.

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

After committing and verifying the replacement implementation, stdout must end with exactly one:

```text
<<<CONTROL_RESULT>>>
{
  "phase": "REPLAN_REEXECUTE",
  "status": "success",
  "issue_url": "{{ISSUE_URL}}",
  "previous_pr_url": "{{PREVIOUS_PR_URL}}",
  "previous_branch": "{{PREVIOUS_BRANCH}}",
  "previous_head_sha": "{{PREVIOUS_HEAD_SHA}}",
  "head_sha": "<git rev-parse HEAD in this worktree, 40 hex chars>",
  "execution_attempt": {{EXECUTION_ATTEMPT}},
  "historical_findings_considered": {{HISTORICAL_FINDING_COUNT}},
  "unique_failure_constraints": 2,
  "previous_pr_disposition": "superseded",
  "fresh_review_round": 1,
  "verification": {
    "tests_run": [],
    "tests_passed": true
  },
  "pr_title": "<one-line PR title>",
  "pr_body": "<PR body, Markdown: the sections of section 21>"
}
<<<END_CONTROL_RESULT>>>
```

Use actual values.

`head_sha` is a cross-check: the controller reads this worktree's `HEAD`
itself, and rejects the replacement when `head_sha` differs from it or when
the commit is `{{BASE_SHA}}` itself or does not descend from it. A `HEAD`
attached to a local branch is corrected, not published. There is no
`replacement_pr_url` or `replacement_branch` field: you create no PR and
choose no branch.

`historical_findings_considered` means the number of historical actionable findings actually analyzed.

`unique_failure_constraints` means the number after deduplicating/normalizing them into root constraints. It is
recorded by the controller alongside the superseded PR; report the real number (the `2` above is only an example)
and never a value greater than `historical_findings_considered`.

The controller renders these values into the replan transaction marker on
the replacement PR (section 20), and compares the marker it reads back from
GitHub with them; a disagreement rejects the replacement.

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

and execute it cleanly from the fresh default branch head.
