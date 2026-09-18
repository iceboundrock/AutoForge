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

### Before ANALYZE_EXECUTE

The implementer is launched only after the controller has established that
no open PR already implements the issue. The PR the controller persisted is
read first; then the repository's open PRs are listed (strictly: a listing
that may be truncated blocks, because "none exists" is then not knowable)
for the `ai-implementation` marker of `{"issue"}`, which the controller
renders into the implementer's prompt (`IMPLEMENTATION_MARKER`) and the
agent must put in the PR body verbatim:

- exactly one: it is adopted (recorded as the issue's PR, `REVIEW` next)
  without launching the agent, whatever its branch name and whether or not
  GitHub links it to the issue
- two or more: `BLOCKED` without launching the agent; the controller never
  chooses which PR is the issue's
- a PR carrying another issue's marker, or no marker, is not a candidate.
  Linked issues and `autoforge/<n>` branch names are shape, and shape is
  never proof of provenance (a human's PR, another tool's PR); the agent is
  told about such a PR's existence only through its own GitHub reads, and
  the prompt tells it to continue an existing PR for the issue and give it
  the marker rather than open a second one

### After ANALYZE_EXECUTE

Verify:

- PR exists
- repository is correct
- PR is open
- branch is correct
- returned HEAD SHA matches GitHub
- its body carries the issue's `ai-implementation` marker, and it is the
  only open PR that does: a PR without the marker is one no later entry
  could find again, and one of two is a choice the entry never makes. The
  read-back uses the same strict listing the entry uses (one snapshot: the
  marked PR's state, HEAD and branch are read from it), so the two cannot
  disagree about which PR is the issue's. A replan's replacement PR is held
  to the same rule, marker and sole-claimant alike, by the transaction's
  target predicates on one strict listing at binding, on the final read
  before the close, at the confirmation after it and at activation, with
  the PR being superseded excluded by identity
  ([replan-transaction.md](replan-transaction.md)), so the PR the
  controller activates is one this entry finds again after a lost state file

### Before every launch

Every launch of a REMOTE agent is a re-entry, including the correction
relaunch after a malformed result: the phase's GitHub reconciliation
(workflow.md, "Re-entering a phase") runs before the first launch and again
before each correction, so an agent that did its GitHub work and then lost
its result block is never asked to do it again unaware. The markers below
are the identities those probes and the read-backs share. They are owned
by `src/autoforge/claims.py`, which is the only code that parses or renders
one, and every probe and read-back reads them through it:

- **exact schema**: a marker's payload is a JSON object with exactly the
  documented keys, each of the documented type; URLs inside it are bounded
  by `MAX_URL_CHARS` before any parser sees them (an over-long one is a
  defect that names its length and the bound, never the value, because a
  defect's text reaches the persisted block reason), then parsed with the
  typed parsers and compared as GitHub identities (repository
  case-insensitive plus number), never as strings
- **every marker is classified, none is skipped**: a marker of the kind
  whose payload is not the schema, a second single-kind marker on one
  object, or the same follow-up marker twice on one issue is a *defect* of
  that object. A defect anywhere in the scanned set makes every question
  about the set inconclusive: the entry enters `BLOCKED` naming the object
  without launching an agent, the read-back rejects the result. "No object
  claims this key" is never concluded while an object carries a claim that
  could not be read
- **explicit cardinality**: an entry asks *at most one* (nothing yet, or the
  one write to adopt); a read-back asks *exactly one* (the write the agent
  claims exists and is the only one). Two or more is refused by both
- **strict decoding of GitHub rows**: every issue, PR and comment row a
  listing or view returns is decoded by one strict decoder per kind
  (`github.py`); a row without a usable URL of the right kind, or whose
  `number` disagrees with its URL, is a conclusive `GitHubError`, never an
  object with an empty identity that no comparison could match. Truncation
  is judged on the raw row count before decoding
- **the prompt trust boundary**: finding ids and URLs recovered from
  markers are validated by the same rules as `CONTROL_RESULT` fields and
  escaped before they are rendered into a prompt; a marker's text never
  becomes controller or prompt syntax

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
- the marker is a JSON object with exactly an integer `round` (not a
  boolean, not a float), a 40-hex `reviewed_head_sha`, a boolean
  `needs_fix_round` and optionally `finding_ids` (distinct ids of that
  round); anything else is a defect that blocks the entry and rejects the
  read-back, never "no marker"

The same entry lists the repository's open issues (strictly, as before FIX)
for every `ai-follow-up` marker naming this PR, whatever the finding id, and
hands them to the reviewer as `EXISTING_FOLLOW_UP_ISSUES` (finding id and
issue URL). Finding ids are round-scoped, so a problem an earlier round
deferred to a follow-up issue would otherwise be re-raised under a new id
and deferred again into a second issue (#90). The reviewer is told that a
deferred problem is not a finding of the round unless the deferral itself is
wrong for this PR. A listing that may be truncated blocks: a reviewer
launched on an incomplete list could re-raise a deferred problem.

### After REVIEW

Verify:

- review comment exists
- it belongs to the expected PR
- review round marker is correct
- reviewed HEAD is correct
- it is the only comment carrying this round's marker at this HEAD, and it
  is the comment the result names; a second one rejects the round (the
  uniqueness rule is enforced on read-back, never trusted to the prompt)
- the marker's `needs_fix_round` equals the result's, and its
  `finding_ids`, when present, are exactly the ids of the result's findings
  as a set (order is presentation); the marker is the durable copy of the
  round that a later entry, the fixer and a human read, so it may not tell
  a different story from the findings the controller persists

### Before FIX

The fixer is launched only while the PR HEAD read from GitHub equals the
reviewed HEAD the open findings are bound to. A HEAD past it is an
unverified push (an unrecorded fix, an operator); the review is stale and
the phase goes to `REVIEW` of the actual HEAD without launching the fixer
(workflow.md, "Bind reviews to PR HEAD SHA and to the PR identity").

A push is not the only write a fixer makes: a `follow_up_created`
resolution creates an issue and moves no HEAD. With the HEAD unchanged the
controller lists the repository's open issues (strictly: a listing that may
be truncated blocks, because "none exists" is then not knowable) for the
`ai-follow-up` marker of `{"finding_id", "pr"}`:

- exactly one per finding: its URL is handed to the fixer in
  `FOLLOW_UP_ISSUES` to report as that finding's `follow_up_issue_url`,
  never to recreate
- two or more for one finding: `BLOCKED` without launching the fixer
- a marker whose `finding_id` is not of the form `R<round>-F<n>` (or longer
  than the parser's bound), an issue carrying the same marker twice, or any
  other unreadable follow-up marker on an open issue: `BLOCKED` naming the
  issue, whatever PR the marker names
- a closed issue carrying the marker is not the finding's open follow-up
- the same listing's issues carrying this PR's marker for a finding of an
  earlier round go to the fixer as `EXISTING_FOLLOW_UP_ISSUES`; a finding
  that turns out to be one of those problems is resolved in the PR or, when
  deferring again is right, recorded by adding the new finding's marker to
  that existing issue (an issue may carry several markers), never by a
  second issue

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
- latest clean review applies to current HEAD *of the reviewed PR*: the
  review is bound to the PR it was posted on, its HEAD and its base
  (workflow.md, "Bind reviews to PR HEAD SHA and to the PR identity");
  `current_pr_url` and the PR GitHub returns for it must be that PR by
  identity (`BLOCKED` otherwise, before any other read), and the PR's base
  must still be the reviewed one (stale -> `REVIEW` otherwise, like a HEAD
  move)

### After MERGE

Verify actual GitHub PR state is `MERGED`, at the reviewed HEAD and into the
reviewed base, before updating counters or closing dependent state. A PR
merged at the reviewed HEAD into another branch is a change no review
decided on: `BLOCKED`, not counted.

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
