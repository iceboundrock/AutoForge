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
upcoming round at the bound HEAD and base (an earlier invocation of the
same round whose result was never recorded):

- exactly one: its URL is handed to the reviewer as
  `EXISTING_REVIEW_COMMENT_URL`, to adopt or edit in place, never duplicate
- two or more: `BLOCKED` without launching the reviewer; the controller
  never chooses which review is the round's
- a comment for the same round at another HEAD, or against another base,
  is not this round's: the round's identity is the diff it decided on, and
  a review posted before the PR was retargeted decided on another diff
- a marker that names no base was written before the marker recorded one;
  it reviewed a base nobody recorded, so it matches no bound key and is
  never adopted, but it is not a defect (a PR mid-flight carries one from
  every earlier round, and a defect would block every later entry on it)
- the marker is a JSON object with exactly an integer `round` (not a
  boolean, not a float), a 40-hex `reviewed_head_sha`, a boolean
  `needs_fix_round` and optionally `reviewed_base_ref` (a branch name) and
  `finding_ids` (distinct ids of that round); anything else is a defect
  that blocks the entry and rejects the read-back, never "no marker"

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
- it is the only comment carrying this round's marker at this HEAD against
  the bound base, and it is the comment the result names; a second one
  rejects the round (the uniqueness rule is enforced on read-back, never
  trusted to the prompt), and so does a result naming a comment whose
  marker is for another base or none: the entry would not have handed it
  over, and the binding the round writes (`reviewed_base_ref`) must be the
  base its comment claims
- the marker's `needs_fix_round` equals the result's, and its
  `finding_ids`, when present, are exactly the ids of the result's findings
  as a set (order is presentation); the marker is the durable copy of the
  round that a later entry, the fixer and a human read, so it may not tell
  a different story from the findings the controller persists

The verified comment's URL is the REVIEW -> FIX handoff artifact (#80). The
round persists GitHub's URL of the comment the controller located
(`last_review_comment_url`, also recorded in `review_history`), never the
reviewer's spelling of it: the result's `review_comment_url` is matched to the
located comment by identity (owner and repository case-folded, number,
comment id), so a case-variant spelling of the PR, or the `issues/<n>` path
GitHub also serves a PR comment under, is accepted as naming the comment
(a comment on another number or repository is not), and the URL the FIX
prompt renders as `REVIEW_COMMENT_URL` is the one GitHub reported, in its
`pull/<n>` form. The FIX prompt names that comment as the authoritative review for
the round at the reviewed HEAD and tells the fixer not to substitute another
PR comment or round; a human comment, an earlier or stale round, or an
unrelated bot comment on the same PR is never the handoff because only the
comment carrying the round's marker at the bound HEAD and base can be
verified. A round that went stale (HEAD or base moved while the reviewer
worked) records its comment URL in its history entry and in
`last_review_comment_url` for the next REVIEW prompt's
`PREVIOUS_REVIEW_COMMENT_URL`, but launches no fixer, so a stale comment never
becomes a FIX handoff.

### Before FIX

The fixer is launched only while the PR HEAD read from GitHub equals the
reviewed HEAD the open findings are bound to. A HEAD past it is an
unverified push (an unrecorded fix, an operator); the review is stale and
the phase goes to `REVIEW` of the actual HEAD without launching the fixer,
with the open findings carried to that review as prior findings to re-check
(workflow.md, "Bind reviews to PR HEAD SHA and to the PR identity" and
"Stale rounds keep their findings").

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

The agent's one write in the phase is a progress comment on the EPIC; the
EPIC body is written by the controller (below), never by the agent. The
comment carries the `ai-epic-progress` marker of
`{"issue", "pr"}` (the finished issue and the merged PR, rendered into the
prompt as `PROGRESS_MARKER`); before the agent is launched the EPIC's
comments are read for it:

- exactly one: its URL is handed to the agent as
  `EXISTING_PROGRESS_COMMENT_URL`, to adopt or edit in place, never
  duplicate; this covers an interrupted step and the bounded re-selection
  below equally, since both are re-entries
- two or more: `BLOCKED` without launching the agent
- a comment for another issue or PR on the same EPIC is not this entry's

The EPIC body is read too, and its managed roadmap section located (see
"EPIC updates" below): its current content is rendered into the prompt as
`CURRENT_ROADMAP_SECTION` (fenced, untrusted), the PRs merged since the last
roadmap update as `MERGED_PRS_SINCE_EPIC_UPDATE`, and whether an update is
due as `ROADMAP_UPDATE_DUE`. A body whose markers are ambiguous (a marker
repeated, an end before a start, one without the other) is `BLOCKED` without
launching the agent: the controller edits only the text between the markers
and never guesses which text that is. So is a body that cannot be read for a
conclusive reason (authentication, permissions, the EPIC gone, malformed
data): an agent launched without the body would compose a section the
controller could never splice. Only `GitHubUnavailableError` propagates, so
`resume` retries the read.

### After UPDATE_EPIC

First read the EPIC back: exactly one comment carrying this entry's marker.
None means the agent did not do the phase's write (the result is rejected
and the next entry finds nothing and launches again); two means it
duplicated the one it was handed (rejected; the next entry blocks on the
pair). Then perform the roadmap write when one is due, and only then verify
`next_issue_url` exactly like the first issue in `INITIALIZING` before
switching issues:

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
did not already verify: the progress comment is adopted on the retry, a
roadmap section already written was read back and closed its batch (the
retry finds it in place and does not write it again), and no issue was
switched.

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
- create or delete worktrees (the plan names the per-issue worktree that
  execution would create)

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

The EPIC body is the operator's document. AutoForge owns exactly one part
of it, the managed roadmap section between two marker lines, and the
controller is the only party that writes the body (`autoforge.roadmap`,
`ControllerEngine._apply_roadmap_section`):

```text
<!-- ai-controller-roadmap:start -->
...
<!-- ai-controller-roadmap:end -->
```

- **Batching is the controller's decision.** `UPDATE_EPIC` runs after every
  merge, but the roadmap is written only when
  `merged_since_epic_update >= workflow.epic_update_every` (default 1), or
  when the agent reports the EPIC complete (`next_issue_url: null`) with
  merges still uncounted. The agent is told whether an update is due; it is
  never asked to decide. When not due, nothing is written, the counter is
  kept, and a `roadmap_section` the agent returned anyway is ignored.
- **The agent returns content, the controller edits.** The prompt forbids
  `gh issue edit`; the agent returns the section's new content as
  `roadmap_section` in its `CONTROL_RESULT` (bounded by
  `MAX_ROADMAP_SECTION_CHARS`, refused when it contains any `<!-- ai-`
  marker, since the roadmap markers would split the body into more than one
  section and any other marker would plant a durable claim in an open issue
  the controller scans). A required section that is missing is rejected
  (`VerificationError`, no reset, `resume` asks again).
- **Only the section changes.** The controller re-reads the body, refuses
  to write when the bytes outside the markers differ from the body the entry
  read (the section was composed against a stale view; the next entry
  re-reads), replaces the text between the markers or appends the block
  after the existing text when there is none, and writes the whole body
  with `gh issue edit --body-file` (the body never travels in argv).
- **The write is read back.** After the write the body is read again: every
  byte outside the markers must equal the body read before the write (plus
  the two marker lines when the section was appended), and the section must
  be the one written. A mismatch is rejected without resetting the counter;
  a conclusive GitHub failure or a body whose markers can no longer be read
  is `BLOCKED`; an unavailable GitHub propagates and `resume` re-enters.
- **The counter resets after the read-back, never before.** A crash between
  the write and the state save re-enters `UPDATE_EPIC`: the entry reads the
  body with the section in place, the agent (adopting its progress comment)
  returns the section again, the controller finds the spliced body equal to
  the current one, writes nothing, and resets. The splice replaces in place
  and never appends a second block, so a retry cannot double-apply.

Do not rewrite the whole EPIC body, and never edit the EPIC body from a
prompt.

---

## Related contracts

- Crash recovery around a side effect that completed before local state was
  persisted, and the transient-versus-conclusive failure classification, are
  in [state-and-recovery.md](state-and-recovery.md).
- The destructive close in `REPLAN_REEXECUTE` has its own transaction
  contract: [replan-transaction.md](replan-transaction.md).
