# GitHub safety: source of truth, verification, merge, dry-run

Read this before changing any GitHub read or write (`src/autoforge/github.py`,
`GitHubClient`), any verification of an agent claim against GitHub
(`src/autoforge/validation.py`, the post-phase checks in
`src/autoforge/engine.py`), PR discovery, the merge gate and pre-merge evidence
(`src/autoforge/premerge.py`, `safety.*` and `merge.*` configuration), EPIC
maintenance, or the dry-run mode.

---

## GitHub is the source of truth

Agent `CONTROL_RESULT` output is a claim, not authoritative state.

Independently verify important facts using GitHub / git.

Examples:

### After ANALYZE_EXECUTE

Verify:

- PR exists
- repository is correct
- PR is open
- branch is correct
- returned HEAD SHA matches GitHub

### Before every launch

Every launch of a REMOTE agent is a re-entry, including the correction
relaunch after a malformed result: the phase's GitHub reconciliation
(workflow.md, "Re-entering a phase") runs before the first launch and again
before each correction, so an agent that did its GitHub work and then lost
its result block is never asked to do it again unaware. The markers below
are the identities those probes and the read-backs share; each is validated
by one strict parser used by both the entry scan and the verification, so a
comment or issue the read-back would reject is never adopted at entry.

### Before REVIEW

The reviewer is launched only after the controller has read the PR's
comments for a comment already carrying the `ai-review-result` marker of the
upcoming round at the bound HEAD (an earlier invocation of the same round
whose result was never recorded):

- exactly one: its URL is handed to the reviewer as
  `EXISTING_REVIEW_COMMENT_URL`, to adopt or edit in place, never duplicate
- two or more: `BLOCKED` without launching the reviewer; the controller
  never chooses which review is the round's
- a comment for the same round at another HEAD is not this round's
- the marker is a JSON object with an integer `round` (not a boolean, not a
  float), a 40-hex `reviewed_head_sha` and a boolean `needs_fix_round`;
  anything else is not a marker, at entry or on read-back

### After REVIEW

Verify:

- review comment exists
- it belongs to the expected PR
- review round marker is correct
- reviewed HEAD is correct
- it is the only comment carrying this round's marker at this HEAD; a
  second one rejects the round (the uniqueness rule is enforced on
  read-back, never trusted to the prompt)

### Before FIX

The fixer is launched only while the PR HEAD read from GitHub equals the
reviewed HEAD the open findings are bound to. A HEAD past it is an
unverified push (an unrecorded fix, an operator); the review is stale and
the phase goes to `REVIEW` of the actual HEAD without launching the fixer
(workflow.md, "Bind reviews to PR HEAD SHA").

A push is not the only write a fixer makes: a `follow_up_created`
resolution creates an issue and moves no HEAD. With the HEAD unchanged the
controller lists the repository's open issues (strictly: a listing that may
be truncated blocks, because "none exists" is then not knowable) for the
`ai-follow-up` marker of `{"finding_id", "pr"}`:

- exactly one per finding: its URL is handed to the fixer in
  `FOLLOW_UP_ISSUES` to report as that finding's `follow_up_issue_url`,
  never to recreate
- two or more for one finding: `BLOCKED` without launching the fixer
- a closed issue carrying the marker is not the finding's open follow-up

### After FIX

Verify:

- current PR HEAD matches the returned new HEAD
- a claimed follow-up issue exists in this repository, is `OPEN`, is not the
  current issue, and is the one open issue carrying its finding's marker
- a finding resolved any other way has no open issue carrying its marker;
  the marked issue is the durable record of the decision and state never
  records a resolution GitHub contradicts

### Before UPDATE_EPIC

The phase's writes are a progress comment on the EPIC and its task-list
edits. The comment carries the `ai-epic-progress` marker of
`{"issue", "pr"}` (the finished issue and the merged PR, rendered into the
prompt as `PROGRESS_MARKER`); before the agent is launched the EPIC's
comments are read for it:

- exactly one: its URL is handed to the agent as
  `EXISTING_PROGRESS_COMMENT_URL`, to adopt or edit in place, never
  duplicate; this covers an interrupted step and the bounded re-selection
  below equally, since both are re-entries
- two or more: `BLOCKED` without launching the agent
- a comment for another issue or PR on the same EPIC is not this entry's

The task-list edits are idempotent by nature (a checked box stays checked)
and are not read back; confining them to a managed section is #4 and #13.

### After UPDATE_EPIC

First read the EPIC back: exactly one comment carrying this entry's marker.
None means the agent did not do the phase's write (the result is rejected
and the next entry finds nothing and launches again); two means it
duplicated the one it was handed (rejected; the next entry blocks on the
pair). Only then verify `next_issue_url` exactly like the first issue in
`INITIALIZING` before switching issues:

- it parses as an issue URL of the configured repository
- it is neither the EPIC nor the issue just finished
- the issue exists and is `OPEN`

Identity checks (EPIC, just-finished issue) compare repository
case-insensitively plus issue number, never URL strings: GitHub owner and
repository names are case-insensitive.

A `next_issue_url` that is not an issue URL at all, or is longer than
`MAX_URL_CHARS`, is refused by the parser as a malformed `UPDATE_EPIC`
result and goes through the ordinary correction retry; it is not a
selection and does not spend one of the bounded re-selections below. The
engine still parses the URL itself before any GitHub read: the same check
serves `INITIALIZING`, whose URL comes from the operator, and it is the
defence for a result that reached the engine some other way.

A rejected selection is retried once (with the controller's reason in the
prompt); a second rejection enters `BLOCKED`. A transient GitHub failure
while checking the selection takes the same bounded retry. Any other GitHub
failure (authentication, permissions, malformed data) is conclusive and
enters `BLOCKED` immediately without invoking the agent again. Never switch
to an unverified issue. A rejected selection has no effect the controller
did not already verify: the progress comment is adopted on the retry, the
task-list edits are idempotent, and no issue was switched.

### Before MERGE

Verify:

- PR remains open
- PR is mergeable
- required checks pass
- each required check's run has the same jobs and steps as the base branch's
  own run of that workflow (`safety.verify_check_definition`): a green check
  is produced by the PR's copy of the workflow, so its name alone proves only
  that whatever the PR defined passed
- every `merge.verification_commands` command passes in a temporary export of
  the reviewed HEAD, run by the controller itself after the GitHub-side facts
  above; a hosted check runs the PR's own code and cannot say what the PR's
  tests still assert
- latest clean review applies to current HEAD

### After MERGE

Verify actual GitHub PR state is `MERGED` before updating counters or closing dependent state.

Never advance the workflow solely because an LLM said an operation succeeded.

---

## Dry-run safety

Dry-run is a controller-level invariant, not a prompt convention.

When dry-run is active, AutoForge may:

- inspect local state
- validate configuration
- compute routing
- render prompts
- show intended commands
- show expected transitions

It must not:

- invoke real coding/review agents
- push commits
- create or edit GitHub issues
- create PRs
- add comments
- merge
- delete branches
- delete worktrees

Never rely only on telling an LLM "do not modify anything".

---

## Merge safety

`MERGE` has no agent profile. The controller performs the merge itself (`gh pr merge` via `GitHubClient`, bound to the reviewed HEAD) behind the merge safety gate; agents are never asked to merge, and `common.md` rule "never merge a pull request" is unconditional.

Automatic merge must be controlled by an explicit safety gate, for example:

```yaml
safety:
  allow_merge: false
```

Unless the current milestone/task explicitly enables and validates real merge behavior, keep automatic merge disabled.

Do not weaken merge safety to make an integration test easier.

Tests must never merge real PRs.

---

## EPIC updates

When EPIC maintenance is implemented, AutoForge must preserve manually maintained EPIC content.

Only update a managed section:

```text
<!-- ai-controller-roadmap:start -->
...
<!-- ai-controller-roadmap:end -->
```

If it exists, replace only the managed section. If absent, append it.

Do not rewrite the whole EPIC body.

---

## Related contracts

- Crash recovery around a side effect that completed before local state was
  persisted, and the transient-versus-conclusive failure classification, are
  in [state-and-recovery.md](state-and-recovery.md).
- The destructive close in `REPLAN_REEXECUTE` has its own transaction
  contract: [replan-transaction.md](replan-transaction.md).
