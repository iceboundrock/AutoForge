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

The agent makes no GitHub write in this phase (ADR 0004, #161). It commits
on its worktree's detached `HEAD` and returns the PR's title and body; the
controller pushes the commit to `autoforge/<n>` (a `push` record), then
opens the PR (`implementation_pr`) or adopts the open PR already on that
branch (`adopt_pr`), and reads it back ("Controller-owned effects" below).

The journal is read first. A persisted completion context means the
agent's commit was already accepted and the push and PR planned: the phase
is completed from the records and no agent is launched. Otherwise the
controller establishes that no open PR already implements the issue. The
PR the controller persisted is read first; then the repository's open PRs
are listed, to the end (the client walks GitHub's pages by cursor until it
reports no next page, so the listing has no ceiling to fall past, #20; a
listing the client cannot read to its end blocks, because "none exists" is
then not knowable) for the `ai-implementation` marker of `{"issue"}`, which
only the controller writes:

- exactly one, at a fresh entry: it is adopted (recorded as the issue's PR,
  `REVIEW` next) without launching the agent, whatever its branch name and
  whether or not GitHub links it to the issue. After a launch of this entry
  it is not: the entry observation recorded none, so the controller did not
  open it, and the run is `BLOCKED` naming it (D9.4); `unblock` then binds
  it at a fresh entry if the operator decides it is the implementation. The
  one exception is the one-shot re-entry of a run persisted while an agent
  of the previous contract was publishing the PR (D13.4), which recovers as
  before.
- two or more, one whose marker cannot be read, or one headed in a fork:
  `BLOCKED` without launching the agent. The controller never chooses
  which PR is the issue's, and it cannot publish to a fork's branch.
- a PR carrying another issue's marker, or no marker, is not a candidate.
  Linked issues and branch names are shape, and shape is never proof of
  provenance (a human's PR, another tool's PR).

Then the default branch and its head (the candidate's base), the head of
`autoforge/<n>` and the PRs headed at that branch are read, both heads are
fetched into the shared object store through the controller's transport,
and the entry observation (the branch head, the default branch at the base,
no marker PR, and the open PR on the branch or none) is persisted by the
pre-launch save, so a branch head, a marker PR, a PR on the branch or a
default branch a later entry finds is explained by this read or not at all:

- no branch: the agent starts from the default branch head;
- a branch with no PR: earlier work for the issue; the agent continues from
  its head and the push is a fast-forward over it;
- exactly one open PR on the branch, onto the default branch and carrying
  no marker: the PR the controller adopts; the agent continues from its
  head;
- a closed or merged PR on the branch, two open PRs, a PR onto another
  base, or one carrying another issue's (or a malformed) marker: `BLOCKED`
  without launching. The controller never opens a second PR beside a
  closed or merged one.

At a re-entry after a launch, a branch head other than the recorded one
(something else pushed, created or deleted the branch), an open PR on the
branch other than the recorded one (one opened where the entry recorded
none, or the recorded one gone), or a default branch other than the
recorded one (renamed or switched) blocks without launching. The base,
branch and start commit are rendered into the prompt
(`DEFAULT_BRANCH`, `BASE_SHA`, `BRANCH`, `START_SHA`); a dry-run plan shows
placeholders for them. Only `GitHubUnavailableError` and a failed fetch
propagate (nothing was launched; `resume` reads again).

### After ANALYZE_EXECUTE

The candidate is checked before the result is accepted, so a refusal is an
ordinary correction retry with nothing pushed or created. The controller
reads the worktree itself, never the agent's word for it: `HEAD` is
detached (a `HEAD` attached to a local branch is refused), equal to the
reported `head_sha`, different from the recorded base, descending from it
and from the branch head the entry recorded (so the push is a
fast-forward), and every commit of the published range passes the
commit-message policy ("Published content" below). `pr_title` and
`pr_body` pass the published-content policy, and the composed body (the
agent's body, `Closes #n` and the marker) passes the credential rule as a
whole. A worktree read or an ancestry proof that cannot be completed is
inconclusive (`VerificationError`, for `resume`), never a correction.

Then, in order, all from one persisted plan:

1. The precondition read: the default branch is read again, the open PRs
   are listed again and the branch and its PRs read again. A default
   branch other than the one the entry read, a marker PR that appeared
   while the agent ran, a branch head other than the recorded one, a PR
   closed on the branch meanwhile, or an open PR on the branch other than
   the one the entry recorded (an unmarked PR opened where the entry saw
   none is never adopted, ADR 0004 K3) blocks with nothing planned or
   sent. Otherwise the `push` record (the candidate, compare-and-swap
   against the recorded head) and the `implementation_pr` record (onto the
   default branch the entry read; the agent's title, its body followed by
   `Closes #n` and the marker) or the `adopt_pr` record (the PR the entry
   recorded; its current body followed by the same two lines, against its
   digest) are saved with the completion context in one save. The load
   refuses a plan whose PR the observation does not explain: a create
   beside an observed PR or onto another base, an adoption of another PR.
2. Before anything of the plan is sent, and again at every later entry
   that completes it from the journal, a PR create still pending is
   checked against the default branch: when its planned base is no longer
   the default branch it blocks with nothing more sent, the push included
   (make the planned branch the default again, then `unblock`). The push
   is driven, then the PR record. An adopted PR is written to only once
   GitHub shows it onto the default branch and headed at the candidate: a
   head still at the branch's old value is GitHub catching up (transient),
   any other head or base blocks.
3. The read-back: one complete listing of the open PRs, in which exactly
   one carries the issue's marker, and it is the PR the record observed,
   open, of this repository, headed at exactly the pushed candidate on
   `autoforge/<n>` and based on the default branch. A head still at the
   branch's old value is transient; any other mismatch blocks. Only then
   is the PR bound (`current_pr_url`, `current_head_sha`,
   `current_branch`) and the review history reset.

A replan's replacement PR is held to the same marker rule, marker and
sole-claimant alike, by the transaction's target predicates on one
complete listing at binding, on the final read before the close, at the
confirmation after it and at activation, with the PR being superseded
excluded by identity ([replan-transaction.md](replan-transaction.md)), so
the PR the controller activates is one this entry finds again after a lost
state file.

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
  object with an empty identity that no comparison could match. Where a
  listing still has a ceiling (open issues, all-states PRs), truncation is
  judged on the raw row count before decoding; the open-PR listing has none
  and is read to its end or refused
- **the prompt trust boundary**: finding ids and URLs recovered from
  markers are validated by the same rules as `CONTROL_RESULT` fields and
  escaped before they are rendered into a prompt; a marker's text never
  becomes controller or prompt syntax

### Before REVIEW

The reviewer is launched only after the controller has bound the revision
it is to review, all read from GitHub: the PR HEAD, its base branch, and
the merge base of the two (`repos/{owner}/{repo}/compare/{base}...{head}`,
`merge_base_commit.sha`, the commit the PR diff is computed from; a base
rewritten under its name moves it, ordinary commits on the base do not,
#96). A read that fails refuses the entry; nothing is launched against a
revision the controller could not name. It has then read the PR's
comments for a comment already carrying the `ai-review-result` marker of
the upcoming round at the bound HEAD, base and merge base (an earlier
invocation of the same round whose result was never recorded):

- exactly one: its URL is handed to the reviewer as
  `EXISTING_REVIEW_COMMENT_URL`, to adopt or edit in place, never duplicate
- two or more: `BLOCKED` without launching the reviewer; the controller
  never chooses which review is the round's
- a comment for the same round at another HEAD, against another base, or
  from another merge base is not this round's: the round's identity is the
  diff it decided on, and a review posted before the PR was retargeted, or
  before its base was rewritten under its name, decided on another diff
- a marker that names no base, or no merge base, was written before the
  marker recorded one; it reviewed a diff nobody recorded, so it matches
  no bound key and is never adopted, but it is not a defect (a PR
  mid-flight carries one from every earlier round, and a defect would
  block every later entry on it)
- the marker is a JSON object with exactly an integer `round` (not a
  boolean, not a float), a 40-hex `reviewed_head_sha`, a boolean
  `needs_fix_round` and optionally `reviewed_base_ref` (a branch name),
  `reviewed_merge_base_sha` (a 40-hex SHA) and `finding_ids` (distinct ids
  of that round); anything else is a defect that blocks the entry and
  rejects the read-back, never "no marker"

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
- it carries the `# AI Code Review — Round N` heading for this round
- review round marker is correct
- reviewed HEAD is correct
- it is the only comment carrying this round's marker at this HEAD against
  the bound base from the bound merge base, and it is the comment the
  result names; a second one rejects the round (the uniqueness rule is
  enforced on read-back, never trusted to the prompt), and so does a result
  naming a comment whose marker is for another base or merge base, or
  names none: the entry would not have handed it over, and the binding the
  round writes (`reviewed_base_ref`, `reviewed_merge_base_sha`) must be the
  base and merge base its comment claims
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
comment carrying the round's marker at the bound HEAD, base and merge base
can be verified. A round that went stale (HEAD, base or merge base moved while
the reviewer worked) records its comment URL in its history entry and in
`last_review_comment_url` for the next REVIEW prompt's
`PREVIOUS_REVIEW_COMMENT_URL`, but launches no fixer, so a stale comment never
becomes a FIX handoff.

The reviewer is a full coding agent launched in the issue's worktree, and
the REVIEW prompt makes the phase read-only: the one review comment is its
only write; it commits, pushes and edits nothing, and a defect it finds is a
finding for the fixer, never its own fix (#19). That is a prompt rule; what
the controller enforces is the binding above. A reviewer that pushes anyway
moves the HEAD its own round was bound to, and the controller treats that
exactly as any other push during the round: the round is stale, consumed
against the cap, its findings carried, and the next round reviews the
reviewer's commit. It is not made a hard error because the post-review
read cannot tell the reviewer's push from an operator's or a fixer's, and
blocking on every push during a round would turn ordinary concurrent work
on the PR into a human decision.

### Before FIX

The fixer is launched only while the PR HEAD, base and merge base read
from GitHub equal the reviewed HEAD, base and merge base the open findings
are bound to. A HEAD past it is an unverified push (an unrecorded fix, an
operator); a base other than `reviewed_base_ref` is a retargeted PR whose
findings were raised on a diff against another base (#95); a merge base
other than `reviewed_merge_base_sha` under the same base name is a base
rewritten under its name, whose findings were raised on the diff from the
old merge base (#96). In every case the review is stale and the phase goes
to `REVIEW` of the actual revision without launching the fixer, with the
open findings carried to that review as prior findings to re-check
(workflow.md, "Bind reviews to PR HEAD SHA and to the PR identity" and
"Stale rounds keep their findings"). A PR whose base or merge base cannot
be read is refused as a verification failure, as the review and merge
entries refuse it, never read as a retarget or a rewrite. The base is
compared only when `reviewed_base_ref` is set and the merge base only when
`reviewed_merge_base_sha` is set: a protocol-2 or protocol-3 state file
loaded in `FIX` lacks them, so its fixer is launched and the next completed
review writes the binding.

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

- the result's `previous_head_sha` is the HEAD the controller bound the
  findings to
- the resolutions cover exactly the open finding IDs, each one `fixed`,
  `follow_up_created` or `no_change_with_rationale`
- current PR HEAD matches the returned new HEAD
- a `fixed` resolution moved HEAD
- a claimed follow-up issue exists in this repository, is `OPEN`, is not the
  current issue, and is the one open issue carrying its finding's marker
- a finding resolved any other way has no open issue carrying its marker;
  the marked issue is the durable record of the decision and state never
  records a resolution GitHub contradicts

### Before UPDATE_EPIC

The agent makes no GitHub write in this phase (ADR 0004). It returns the
progress report as `progress`, and the controller posts it on the EPIC as
the phase's progress comment: the text, a blank line, then the
`ai-epic-progress` marker of `{"issue", "pr"}` (the finished issue and the
merged PR). The comment is an effect record of kind `progress_comment`
("Controller-owned effects" below). The EPIC body is written by the
controller too (below), never by the agent.

The journal is read first. A persisted completion context means the
agent's result was already accepted: the phase is completed from it (the
record reconciled, the section spliced, the selection verified) and no
agent is launched, unless a persisted rejection voided one of its inputs,
in which case the launch is a re-request for that input alone (After
UPDATE_EPIC, below). Otherwise the EPIC's comments are read for the marker:

- none: the normal case. The entry observation records that, so a comment
  found by a later entry is explained by this read or not at all.
- exactly one that no record of this phase explains: `BLOCKED` without
  launching, naming the comment (D9.7). It is never adopted and never
  duplicated: delete it (the controller then posts its own) or finish the
  EPIC update by hand. The one exception is the one-shot re-entry of a run
  persisted by the previous protocol whose agent had already been launched
  under the contract in which it posted the comment (D13.7): that comment
  is adopted with no record, and the launch asks for the selection only.
- two or more, or one whose marker cannot be read: `BLOCKED` without
  launching the agent
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

The result is validated before anything is published: `progress` is
required and bounded (`MAX_PROGRESS_CHARS`), both texts pass the
published-content policy ("Published content" below), and a roadmap section
the controller requires (EPIC updates, below) must be present. A failure is
an ordinary correction retry, with nothing posted.

Then, in order, all from one persisted completion context (D4.6):

1. The precondition read: the EPIC's comments are read again, and a
   comment for this (issue, PR) that appeared while the agent ran blocks,
   naming it; nothing is posted. Otherwise the completion context (the
   section, the selection, and the digests of the body outside the
   markers) and the planned record are saved in one save.
2. The record is driven: posted once, then read back by its marker as the
   one comment carrying it, with exactly the planned body.
3. The roadmap section is written when one is stored and merges are
   pending (EPIC updates, below).
4. `next_issue_url` is verified exactly like the first issue in
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

A rejected selection is retried once; a second rejection enters `BLOCKED`.
The rejection voids the selection in the persisted context, and the retry
is a re-request (`prompts/update_epic_rerequest.md`, D4.7): it names the
published progress comment, quotes the controller's reason, and asks for
`next_issue_url` alone (with `roadmap_section` while merges are uncounted,
because a new selection of `null` may require one). A re-request result
that carries any other key, `progress` included, is refused. A transient
GitHub failure while checking the selection takes the same bounded retry.
Any other GitHub failure (authentication, permissions, malformed data) is
conclusive and enters `BLOCKED` immediately without invoking the agent
again. Never switch to an unverified issue. A rejected selection has no
effect the controller did not already verify: the progress comment is
posted once and never again, a roadmap section already written was read
back and closed its batch, and no issue was switched.

### Before MERGE

Verify:

- PR remains open
- PR is mergeable
- every check in the status rollup passes (all checks, not only the
  required ones)
- each required check's run has the same jobs and steps as the base branch's
  own run of that workflow (`safety.verify_check_definition`): a green check
  is produced by the PR's copy of the workflow, so its name alone proves only
  that whatever the PR defined passed
- every `merge.verification_commands` command passes in a temporary export of
  the reviewed HEAD, run by the controller itself after the GitHub-side facts
  above; a hosted check runs the PR's own code and cannot say what the PR's
  tests still assert
- latest clean review applies to current HEAD *of the reviewed PR*: the
  review is bound to the PR it was posted on, its HEAD, its base and the
  merge base its diff was computed from (workflow.md, "Bind reviews to PR
  HEAD SHA and to the PR identity"); `current_pr_url` and the PR GitHub
  returns for it must be that PR by identity (`BLOCKED` otherwise, before
  any other read), the PR's base must still be the reviewed one, and the
  merge base of that base and the HEAD, read from GitHub here, must still
  be `reviewed_merge_base_sha` (stale -> `REVIEW` otherwise, like a HEAD
  move; a base rewritten under its name is caught by this read, ordinary
  commits on the base do not move the merge base and leave `BEHIND` to the
  readiness check). The merge-base read failing is classified like every
  other pre-merge read: transient -> bounded re-check, conclusive ->
  `BLOCKED`. The same comparison is made on the post-write read when the
  merge did not happen and the PR is still `OPEN` at the reviewed HEAD and
  base name, so a base rewritten in the write window goes to `REVIEW`
  rather than being retried as reviewed
- the clean review itself is re-read from GitHub, not taken from the state
  file (#94): once the PR is at the reviewed revision, the comment
  `last_review_comment_url` names is fetched (one read per gate pass,
  `GitHubClient.get_comment`, decoded strictly) and must be a comment on
  the reviewed PR as GitHub reports its parent (the comments API addresses
  a comment by id alone, so the URL's spelling of the parent proves
  nothing), carrying exactly one readable `ai-review-result` marker whose
  key is `(review_round, reviewed_head_sha, reviewed_base_ref,
  reviewed_merge_base_sha)`, read through `claims.py` exactly as the
  round's read-back read it, with `needs_fix_round: false`. Anything else
  (the comment gone, on another PR, a marker for another round, HEAD, base
  or merge base, none, unreadable or doubled, a
  marker saying a fix round is needed, or no comment URL in state at all)
  is conclusive: `BLOCKED`, or a refusal from state alone for the missing
  URL, never a merge; a transient failure of the read takes the bounded
  inconclusive path above. The check runs after the identity and revision
  checks (a drifted PR goes to `REVIEW` without it) and before the
  readiness reads and `merge.verification_commands`, so an unbacked review
  never runs the PR's code locally. A state whose review fields were
  edited after the round, by hand or by a controller bug, is caught here;
  `_apply_review` verified the same comment when it wrote the binding, and
  the gate no longer trusts that the file still says what it wrote

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
- execute an effect or run a network git operation. The plan names the
  effects the step would perform and every recorded effect it would
  reconcile first; the `UPDATE_EPIC` plan is computed from persisted state
  alone and reads nothing from GitHub.

Never rely only on telling an LLM "do not modify anything".

---

## Controller-owned effects

ADR 0004 (`docs/adr/0004-authority-boundary-and-typed-external-effects.md`)
moves every externally visible write to the controller. #160 adds the
mechanism and wires its first consumer, the `UPDATE_EPIC` progress comment;
#161 wires `ANALYZE_EXECUTE` (the push of the agent's commit and the
implementation PR, opened or adopted); the other phases keep their current
publication until #162 to #164 move them.

- **Typed writes, never retried blind.** `GitHubClient` has one method per
  write the effects need (`create_issue_comment`, `create_pr_comment`,
  `create_pull_request`, `write_pr_body`, `create_issue`,
  `write_issue_body`). Each sends its payload on stdin to the named
  repository, returns the created object's identity, and runs outside the
  transient retry loop: a timeout, a transient failure, or a reply that
  cannot be parsed or names another object is an unknown outcome
  (`GitHubUnavailableError`), which is reconciled by reading, never re-sent.
  Reads keep their transient retry. Bounds are checked before any process
  starts.
- **Identity reads are complete.** Each kind is found by its marker or
  identity in a complete listing that also sees objects no longer open
  (`list_prs_for_head`, `list_issues_above` bounded by a watermark taken
  before the create, the full comment listing), never the search API. An
  issue's or PR's comments (K4, K8, the replan close receipt) are read by
  GraphQL cursor, 100 a page, until GitHub reports no next page;
  `gh issue view` / `gh pr view --json comments` stop at the first 100 and
  are not used. A listing that cannot be read to its end (a failed page,
  a page with no next cursor, a cursor seen twice) is an error, never an
  absent comment.
- **Records before writes.** A record (`src/autoforge/effects.py`) holds
  the kind, the identity, the exact target, the precondition, the payload,
  the attempt count, the stage and the observed result. It moves from
  `intended` through `attempted`, persisted before every write, to
  `observed` (read back and matching) or `conflict` (`BLOCKED`, naming the
  object). `effect_ops.drive` reconciles before anything is issued: exactly
  one matching object is adopted as observed; none with the precondition
  holding is issued, at most `MAX_EFFECT_ATTEMPTS` (2) times; anything else
  is a conflict. An exhausted bound is a conflict that names the record and
  the manual step. `unblock` back into the phase reconciles a conflict
  again with its attempt count kept.
- **The controller git transport** (`src/autoforge/git_transport.py`) pushes
  an exact SHA to an exact controller-derived ref, compare-and-swap against
  the expected old value, never to the default branch and never a
  non-fast-forward. Every operation runs in a fresh private git directory
  over the shared object store, with no system or global configuration, so
  nothing the agent can write in the shared repository (`pushurl`,
  `insteadOf`, `core.sshCommand`, `core.hooksPath`, `include.path`, hooks,
  replacement refs, grafts, the commit-graph) is read. The remote is an
  explicit URL derived from the verified repository identity, never
  `origin`, and the credential comes from `gh auth git-credential`, never
  from argv, a URL, a log or state. A commit is read back as its own bytes
  (re-hashed). Fetches write objects only and create or move no ref. The
  pre-merge evidence fetch runs on it, and every git process goes through
  the executor.
- **Every other controller git process** runs in the operator's repository
  through `git_transport.local_git_request` (ADR 0004 D7.3, D7.5): the
  per-issue worktree's `git worktree add` and its identity and registry
  reads, the workspace reader (`rev-parse`, `symbolic-ref`, `status`), the
  lock's common-dir read and the pre-merge export. Hooks and the file-system
  monitor are off at command-line precedence, and so are replacement
  objects and the commit-graph. The child starts from
  `LOCAL_GIT_ENV_ALLOWLIST`, with no token and no `GIT_*` variable of the
  operator's shell, so nothing a hook, monitor or `GIT_CONFIG_*` planted in
  the shared repository or the shell could start runs with the controller's
  credential. A filter driver still runs during a checkout or export, with
  no more authority than the agent. `autoforge doctor` is an operator
  diagnostic outside any run and keeps its own requests: it runs only
  `git --version`, `rev-parse --show-toplevel` and `remote get-url`, none
  of which starts a hook, the monitor or a filter.

### Published content

Agent text the controller publishes under the operator's identity
(`progress`, `roadmap_section`, `pr_title`, `pr_body`, and the payloads of
later kinds) is refused by `result_parser.published_text_problem`, and the
agent is asked to correct it, when it contains:

- an HTML comment opening a controller marker (`<!-- ai-` or
  `<!-- autoforge-`, any spacing or case);
- a credential class `redaction.credential_classes` recognises (named, never
  quoted);
- a closing keyword followed by an issue reference, even inside code;
- an `@`-mention outside a code span or fenced block.

`published_payload_problem` applies the credential rule to a whole payload,
so a credential split across fields is refused too.

A commit the controller pushes is published too. Every commit of the range
from the base to the candidate is read back by the transport and refused
(`result_parser.commit_message_problem`, named by SHA, never quoted) when
its message contains a credential class, or a closing keyword naming any
issue other than the run's own (`#n`, `owner/repo#n` or the issue's URL):
GitHub would close that issue once the commit reaches the default branch.

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

The rest of this section is the operator-facing description of the gate as
it is implemented; "Before MERGE" and "After MERGE" above are the
verification contract it implements.

### Opening the gate

The `MERGE` phase is reachable only from `READY_FOR_MERGE` and only when
both `safety.allow_merge: true` is set in config and `--allow-merge` is
passed on the CLI. With the gate closed `run`/`resume` stop at
`READY_FOR_MERGE` and a human merges: the CLI prints a banner with the
issue, PR, review round and reviewed HEAD and states that automatic merge is
disabled, and `resume` without the gate open re-prints the banner and runs
nothing. `step` without the gate open refuses to advance `READY_FOR_MERGE`
or `MERGE` with an error naming both halves of the gate, before anything is
read from GitHub. `safety.allow_merge` is the *only* config key that opens the gate:
the historical `execution.allow_merge` is rejected on load rather than read,
and an unknown key under `safety` (a typo such as `allow_merges`) is a
configuration error, so the gate can never be "disabled" in one place while
still open in another. `autoforge doctor` prints the effective gate state
and the file that set it.

When the gate is open, the *controller* performs the merge itself:
`gh pr merge --<merge.method> --match-head-commit <reviewed HEAD>` through
`GitHubClient`, with no prompt and no agent invocation.

### Pre-merge verification and its failure classes

Pre-merge verification is controller-side and fails closed. It runs in
`READY_FOR_MERGE` (before `MERGE` is entered) and again in `MERGE` (before
the write), reading only controller state and GitHub, never an agent claim.

- The clean review is bound to the PR it was posted on, its HEAD, its base
  branch and its merge base; `current_pr_url` and the PR GitHub returns for
  it must be that PR by identity (repository and number), else `BLOCKED`
  before anything else is read (the same commits proposed as another PR
  against another base are a change no review decided on).
- GitHub must then report: that PR open at the reviewed HEAD and base from
  the reviewed merge base (a base rewritten under its name goes back to
  `REVIEW`; ordinary commits landing on the base do not move it), not a
  draft, no change to any `safety.protected_merge_paths` entry, every check
  in the status rollup succeeded (all checks, not only required ones),
  `mergeable = MERGEABLE`, `mergeStateStatus` in `CLEAN`/`HAS_HOOKS`, no
  auto-merge armed, and no merge queue on the base branch (`gh pr merge`
  would otherwise arm auto-merge or enqueue instead of merging, leaving an
  asynchronous merge the controller does not own). Every required check must
  also pass the definition comparison, and every
  `merge.verification_commands` command must pass (both below).
- Conclusive negatives (closed PR, conflict, failing check, branch
  protection, queue, a recorded review comment GitHub no longer backs, a
  failing local command) -> `BLOCKED`.
- Inconclusive data (checks still running, the base branch's own run still
  running, `mergeable = UNKNOWN`, an unfetchable reviewed commit, or the
  PR / changed-file / merge-queue read itself failing transiently: timeout,
  connection error, 5xx, rate limit) raises and keeps the phase so
  `resume --allow-merge` (or `step --allow-merge`) re-checks, one attempt per
  invocation, at most `merge.max_verification_attempts` times (default 5),
  then `BLOCKED`.
- A read that fails conclusively (bad credentials, missing permissions, a
  PR that no longer resolves) is `BLOCKED` immediately: re-running would not
  change it.
- HEAD, base or merge-base drift -> `REVIEW`; a PR found already merged in
  `READY_FOR_MERGE` -> `MERGE`, to reconcile and count it once.

In every one of these cases nothing is merged.

### Protected merge paths

A PR is never merged unattended if it redefines its own checks. The hosted
`ci` result the gate trusts is produced by the workflow files *in the PR*:
GitHub runs the PR's copy of `.github/workflows/` and reports it under the
same check name, and a branch ruleset cannot help, because the required
check is defined by the branch it gates. So the controller reads the PR's
changed files and `BLOCK`s when any of them matches
`safety.protected_merge_paths` (default `.github/workflows/`), naming the
paths; a human reviews and merges that PR themselves. Both ends of a rename
count, so moving a protected file *out* of the protected range is refused
like an edit to it. The listing is read in full
(`gh api --paginate --slurp`, every page), so a PR is judged on all of its
files rather than on the first hundred; GitHub itself stops the endpoint at
3,000 files, and a listing that stays short of GitHub's own `changedFiles`
count is refused: a short listing cannot prove a protected path was left
alone, and a PR that large is not one to merge unattended anyway. Setting
the list to `[]` disables the gate; leaving the key empty (`null`) is a
configuration error rather than a silent opt-out.

What this gates is the *definition* of the checks, not the trustworthiness
of a green run: the commands still execute the PR's own code, so a PR can
weaken what its tests assert without touching a protected path. The two
gates below narrow that gap; what remains is why merge stays behind
`safety.allow_merge` + `--allow-merge`.

### Check definitions

A required check is trusted only if it ran the base branch's definition.
With `safety.verify_check_definition` (default `true`) the controller
resolves every `safety.required_checks` context in the PR's status rollup to
its GitHub Actions run through the check's details URL and requires the run
to be in this repository, at the reviewed HEAD and completed; a context that
appears zero or several times, or that is not an Actions run, is refused. It
then reads the run's jobs and their steps (every page, a short listing is
refused) and compares that structure with the base branch's own `push` run
of the same workflow at the branch's current tip: same job names, same step
names in the same order, job order ignored. The first difference (a job or
step missing, added or renamed) is `BLOCKED` with the difference named. The
reference must be a completed, successful run; a base branch without one at
its tip is `BLOCKED` (make it green first), and a reference still running is
inconclusive. This catches a PR that rewrites what the check *is* through an
untouched workflow file (a reusable action, a `Makefile` target the workflow
calls by a different job or step name) but it is structural: a step whose
name stayed the same while its command changed passes. Setting the key to
`false`, or leaving `required_checks` empty, skips the comparison.

### Controller-run verification commands

`merge.verification_commands` (argv lists, empty by default) run on the
operator's machine after every GitHub-side fact above has passed, in a fresh
temporary export of the reviewed HEAD: `git read-tree` +
`git checkout-index` into `autoforge-premerge-*`, so no worktree or branch is
created, the operator's checkout is untouched and there is no `.git` for a
command to reach; the export is deleted afterwards. That also means a command
that needs git metadata (`git describe`, `setuptools-scm` and other version
stamping) or populated submodules fails in the export, so keep those out of
the list or make them tolerate a plain tree. A commit that is not local yet
is fetched from `origin` as `refs/pull/<n>/head`, objects only, without
creating a local ref. A non-zero exit or a timeout
(`execution.command_timeout_seconds`, wall clock) is `BLOCKED` with the redacted output
tail; an unfetchable or unexportable commit is inconclusive and re-checked by
`resume --allow-merge`. A pass is persisted with the HEAD and the command
list, so a resume at the same HEAD does not rerun it and a changed command
list or a new HEAD does; it is cleared when the run moves to the next issue.
Each run is logged like an agent invocation. Dry-run runs nothing and lists
the commands in the plan; `autoforge doctor` reports what is configured
without running it. Submodules are not populated in the export, and the
commands run under the same allow-listed environment as an agent
(`execution.env_allowlist`), not the operator's whole shell.

### Post-merge reconciliation

The merge is counted only after GitHub reports `MERGED` at the reviewed HEAD
into the reviewed base (idempotently, across crashes; a merge into another
base is `BLOCKED`, never counted). If `gh pr merge` returns but the PR is
still open, any auto-merge that call armed is disabled again
(`gh pr merge --disable-auto`) and the run is `BLOCKED`, unless the PR is
open at a *different* HEAD (pushed between the verification and the write,
so `--match-head-commit` refused it) or against a different base
(retargeted in that window) and no asynchronous merge is pending: then
nothing unreviewed merged, the clean review is stale and the run goes back
to `REVIEW`. If the post-merge re-read fails, the outcome is treated as
unknown: the run stays in `MERGE` and `resume --allow-merge` re-inspects
GitHub (an already-merged PR is recovered and counted once; an open one is
re-verified), bounded by the same `merge.max_verification_attempts`, then
`BLOCKED`.

### Doctor's branch-rule check

`doctor` verifies that the default branch still requires the CI check. The
pre-merge gate trusts "every check on the PR succeeded", which says nothing
about whether any check *had* to exist: with the branch ruleset gone, a PR
with no check runs at all is vacuously green. That ruleset lives in
repository settings, outside version control, so `autoforge doctor` reads
the effective rules of the default branch
(`gh api repos/{owner}/{repo}/rules/branches/{branch}`, every page) and
reports which contexts a `required_status_checks` rule names, whether each of
`safety.required_checks` (default `ci`) is among them, whether the ruleset's
enforcement is `active`, and whether it has bypass actors. Any of those
missing is a `FAIL` with the settings URL and the rule to add. A repository
still on classic branch protection is checked through that instead
(`enforce_admins` standing in for "no bypass actors"). A read the token is
not allowed to make (no credentials, a plan that hides rulesets, a non-admin
token and no ruleset) or a transient GitHub failure is `SKIP`, never a false
alarm; so is a *partial* read: GitHub returns a ruleset's `bypass_actors`
only to a token with write access to it, and a rule this token can see but
whose bypass list it cannot is `SKIP` rather than `OK` (a token that GitHub
says may itself bypass the rule is a `FAIL` either way). `doctor --json`
carries a skip as `"skipped": true`. The check is read-only and runs in
`doctor` only: the `READY_FOR_MERGE` gate itself does not yet consult branch
rules.

`doctor` as a whole is read-only apart from creating the state directory
if it is missing and a probe file it creates and removes there; its GitHub
reads (`gh repo view`, `gh api`
GETs of the default branch's rules) never write.

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
  marker in the spelling the claims scanner reads, whitespace after `<!--`
  or none, since the roadmap markers would split the body into more than
  one section and any other marker would plant a durable claim in an open
  issue the controller scans). A required section that is missing is a
  correction of the result before anything is posted
  (`ControlResultValidationError`, no reset).
- **Only the section changes.** The controller re-reads the body and
  refuses to write when the bytes outside the markers differ from the body
  the entry read: the completion context stores their digest, the section
  was composed against a stale view, so it is voided and `resume` re-reads
  the body and re-requests the section alone (the progress comment is not
  posted again). Otherwise it replaces the text between the markers or
  appends the block after the existing text when there is none, and writes
  the whole body with `gh issue edit --body-file` (the body never travels in
  argv).
- **The write is read back.** After the write the body is read again: every
  byte outside the markers must equal the body read before the write (plus
  the two marker lines when the section was appended), and the section must
  be the one written. A mismatch is rejected without resetting the counter;
  a conclusive GitHub failure or a body whose markers can no longer be read
  is `BLOCKED`; an unavailable GitHub propagates and `resume` re-enters.
- **The counter resets after the read-back, never before.** A crash between
  the write and the state save re-enters `UPDATE_EPIC`, which completes from
  the persisted context without launching the agent: the controller finds
  the spliced body equal to the current one, writes nothing, and resets.
  The splice replaces in place and never appends a second block, so a retry
  cannot double-apply.

Do not rewrite the whole EPIC body, and never edit the EPIC body from a
prompt.

---

## Related contracts

- Crash recovery around a side effect that completed before local state was
  persisted, and the transient-versus-conclusive failure classification, are
  in [state-and-recovery.md](state-and-recovery.md).
- The destructive close in `REPLAN_REEXECUTE` has its own transaction
  contract: [replan-transaction.md](replan-transaction.md).
